"""
Shared machinery for beat-based tutorial videos.

A "beat" is one micro-action (fill a field, click a button, select an
option) paired with one short HeyGen narration line. Splitting features
into beats — rather than one narration paragraph per wizard step — is
what fixes narration/video misalignment: record_campaigns.py's original
per-step version let several actions happen in a few seconds, then froze
on the last frame for 10-20s while the narrator described actions that
had already silently happened off-screen. Pacing each individual action to
roughly match its own beat's narration length (see `record_feature` below)
keeps the on-screen state and the narration in sync throughout.

Each feature module under tutorial_studio/features/ supplies:
  - BEATS: dict[beat_id -> narration text], in playback order
  - login(page, base_url): logs into the demo/test account for this feature
  - record(page, base_url, hold): performs the Playwright actions, calling
    hold(beat_id) after each one (or immediately, for beats with no action)
  - BASE_URL: str

This module provides the feature-agnostic pieces: HeyGen TTS calls, the
ffmpeg cut/pad/mux/concat pipeline, and the CLI entry point that chains
generate_voice -> record -> build for a named feature.
"""
import importlib
import json
import subprocess
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

CURSOR_INIT_SCRIPT = """
(function() {
  var cursor = document.createElement('div');
  cursor.id = '__tutorial_cursor__';
  cursor.style.cssText = 'position:fixed;top:0;left:0;width:26px;height:26px;'
    + 'border-radius:50%;background:rgba(230,30,30,0.6);'
    + 'border:3px solid rgba(255,255,255,0.95);box-shadow:0 0 10px rgba(0,0,0,0.5);'
    + 'pointer-events:none;z-index:2147483647;transform:translate(-50%,-50%);'
    + 'display:none;';
  function attach() { if (document.body && !cursor.parentNode) document.body.appendChild(cursor); }
  document.addEventListener('DOMContentLoaded', attach);
  window.addEventListener('load', attach);
  attach();
  document.addEventListener('mousemove', function(e) {
    attach();
    cursor.style.display = 'block';
    cursor.style.left = e.clientX + 'px';
    cursor.style.top = e.clientY + 'px';
  });
})();
"""

# Injected alongside CURSOR_INIT_SCRIPT. A moving dot alone is too subtle to
# read on a recording — this draws a bright pulsing ring/glow around whatever
# element window.__tutorialHighlight(selector, ms) is called on, so viewers
# can tell exactly which button/field the action refers to, not just that
# something moved nearby.
HIGHLIGHT_INIT_SCRIPT = """
(function() {
  var style = document.createElement('style');
  style.textContent = `
    .__tutorial_highlight__ {
      outline: 3px solid #FF9500 !important;
      outline-offset: 3px !important;
      border-radius: 8px;
      animation: __tutorial_pulse__ 0.85s ease-in-out infinite !important;
      position: relative !important;
      z-index: 2147483646 !important;
    }
    @keyframes __tutorial_pulse__ {
      0%   { box-shadow: 0 0 0 0 rgba(255,149,0,0.65); }
      70%  { box-shadow: 0 0 0 14px rgba(255,149,0,0); }
      100% { box-shadow: 0 0 0 0 rgba(255,149,0,0); }
    }
    /* outline/box-shadow don't render reliably on <tr> in a
       border-collapse table — adjacent <td> boxes share/overpaint the
       edge pixels an outline would occupy — so table rows get a plain
       background pulse instead, which always renders. */
    tr.__tutorial_highlight__ {
      outline: none !important;
      box-shadow: none !important;
      animation: __tutorial_pulse_row__ 0.85s ease-in-out infinite !important;
    }
    tr.__tutorial_highlight__ > td {
      background-color: rgba(255,149,0,0.28) !important;
    }
    @keyframes __tutorial_pulse_row__ {
      0%, 100% { background-color: rgba(255,149,0,0.1); }
      50%      { background-color: rgba(255,149,0,0.32); }
    }
  `;
  function attach() { if (document.head) document.head.appendChild(style); }
  document.addEventListener('DOMContentLoaded', attach);
  attach();

  // Takes an actual element (resolved Playwright-side via Locator.evaluate,
  // not a selector string) — a plain document.querySelector(selector) here
  // would break on any Playwright-only selector syntax like :has-text(),
  // which isn't valid native CSS and throws inside browser-context JS.
  window.__tutorialHighlightEl = function(el, ms) {
    if (!el) return;
    document.querySelectorAll('.__tutorial_highlight__').forEach(function(prev) {
      prev.classList.remove('__tutorial_highlight__');
    });
    el.classList.add('__tutorial_highlight__');
    if (ms) {
      setTimeout(function() { el.classList.remove('__tutorial_highlight__'); }, ms);
    }
  };
})();
"""


STUDIO_ROOT = Path(__file__).parent
WORKSPACE_ROOT = STUDIO_ROOT / "_workspace"
VIDEOS_OUT_DIR = STUDIO_ROOT.parent / "static" / "portal" / "tutorial" / "videos"
THUMBS_OUT_DIR = STUDIO_ROOT.parent / "static" / "portal" / "tutorial" / "thumbs"
ENV_PATH = STUDIO_ROOT.parent / ".env"

VOICE_ID = "03fcf8ecb0a94b6b94e9007edb7c35f8"  # "Reassuring Rupert" — starfish-engine, calm male

# Brand name spoken-text spelling for HeyGen narration. This flipped twice
# on 2026-07-28 — see project_tutorial_studio memory for the full story.
# Net result: spell it "Fixtra" in narration/BEATS text. User gave the exact
# target pronunciation explicitly: "fiks-truh" (rhymes with "fix" + "truh",
# like the end of "extra") — that is what "PhiXtra" is supposed to sound
# like when spoken, and "Fixtra" is the spelling that reliably produces it
# from HeyGen. Spelling it "PhiXtra" in narration text does NOT produce this
# sound — it reads differently (worse). Written/on-screen text (logos,
# titles, URLs) is unaffected and stays "PhiXtra" — this constant is for
# spoken narration text only.
BRAND_NAME_SPOKEN = "Fixtra"


def load_heygen_api_key():
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("HEYGEN_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("HEYGEN_API_KEY not found in .env")


def _workspace(feature: str) -> Path:
    ws = WORKSPACE_ROOT / feature
    (ws / "audio").mkdir(parents=True, exist_ok=True)
    (ws / "raw_video").mkdir(parents=True, exist_ok=True)
    (ws / "work").mkdir(parents=True, exist_ok=True)
    return ws


def load_feature(feature: str):
    return importlib.import_module(f"tutorial_studio.features.{feature}")


# ══════════════════════════════════════════════════════════════════════════
# Stage 1 — narration
# ══════════════════════════════════════════════════════════════════════════

def generate_speech(api_key, text, voice_id=None):
    resp = requests.post(
        "https://api.heygen.com/v3/voices/speech",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        json={"text": text, "voice_id": voice_id or VOICE_ID},
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return data["audio_url"], data["duration"]


def generate_voice(feature: str):
    mod = load_feature(feature)
    ws = _workspace(feature)
    api_key = load_heygen_api_key()
    # A feature module may set its own VOICE_ID to opt into a different
    # HeyGen voice without changing the project-wide default.
    voice_id = getattr(mod, "VOICE_ID", VOICE_ID)
    manifest = {}

    for beat_id, text in mod.BEATS.items():
        audio_url, duration = generate_speech(api_key, text, voice_id)
        audio_bytes = requests.get(audio_url, timeout=30).content
        out_file = ws / "audio" / f"{beat_id}.wav"
        out_file.write_bytes(audio_bytes)
        manifest[beat_id] = {"file": str(out_file), "duration": duration, "text": text}
        print(f"  {beat_id}: {duration:.2f}s -> {out_file.name}")

    (ws / "audio" / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\n{len(manifest)} narration clips saved to {ws / 'audio'}")
    return manifest


# ══════════════════════════════════════════════════════════════════════════
# Stage 2 — recording
# ══════════════════════════════════════════════════════════════════════════

def move_to(page, selector, steps=20):
    """Smoothly move the injected on-screen cursor to `selector`'s center.

    Playwright's own click()/fill() jump the DOM state instantly with no
    visible cursor travel, which is invisible on the recording. Calling
    this first (or using click_visibly/type_visibly below) makes the
    action trackable on screen instead of an instant, unseen jump."""
    el = page.locator(selector).first
    el.scroll_into_view_if_needed()
    box = el.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=steps)


def highlight(page, selector, ms=900):
    """Draw a pulsing orange ring around `selector` via HIGHLIGHT_INIT_SCRIPT
    — the cursor dot alone is too subtle to read on a recording; this is
    what actually tells a viewer "this is the button/field in question".

    Uses Locator.evaluate() (resolves the element through Playwright's own
    selector engine) rather than page.evaluate()+document.querySelector() —
    the latter silently fails on any selector using Playwright-only syntax
    such as :has-text(), which isn't valid native CSS."""
    try:
        page.locator(selector).first.evaluate(
            "(el, ms) => window.__tutorialHighlightEl(el, ms)", ms
        )
    except Exception:
        pass


def click_visibly(page, selector, steps=20, pre_click_ms=500):
    """Move the cursor to `selector`, ring-highlight it, pause so the
    highlight is actually visible on screen, then click — instead of an
    instant, unannounced click a viewer has no way to attribute to a
    specific button."""
    move_to(page, selector, steps=steps)
    highlight(page, selector, ms=pre_click_ms + 400)
    page.wait_for_timeout(pre_click_ms)
    page.click(selector)


def type_visibly(page, selector, text, delay=70, pre_type_ms=350):
    """Character-by-character typing animation — only for short values
    (names, numbers). For a full paragraph, len(text) * delay can run to
    10+ seconds, blowing well past that beat's narration audio; build_video()'s
    final mux trims the video to the audio length with `-shortest`, so a
    long type_visibly() call would just get cut off mid-sentence. Use
    fill_visibly() below for anything longer than a short phrase."""
    move_to(page, selector)
    highlight(page, selector, ms=pre_type_ms + len(text) * delay + 500)
    page.wait_for_timeout(pre_type_ms)
    page.click(selector)
    page.fill(selector, "")
    page.locator(selector).press_sequentially(text, delay=delay)


def fill_visibly(page, selector, text, pre_fill_ms=400, highlight_ms=1000):
    """Like type_visibly but fills instantly instead of animating every
    keystroke — for paragraph-length text (textareas) where watching each
    character would make the beat run far longer than its narration."""
    move_to(page, selector)
    highlight(page, selector, ms=highlight_ms)
    page.wait_for_timeout(pre_fill_ms)
    page.fill(selector, text)


def record_feature(feature: str):
    mod = load_feature(feature)
    ws = _workspace(feature)
    manifest_path = ws / "audio" / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"{manifest_path} missing — run generate_voice('{feature}') first")
    audio_manifest = json.loads(manifest_path.read_text())
    beat_ms = {b: max(300, int(audio_manifest[b]["duration"] * 1000)) for b in mod.BEATS}

    boundaries = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page, ctx = mod.login(browser, ws / "raw_video")
        # Injected after login() so both apply to every subsequent navigation
        # in the *recorded* context — CURSOR draws a visible dot that follows
        # page.mouse.move() (Chromium renders no OS cursor of its own in a
        # headless recording); HIGHLIGHT draws a pulsing ring around whatever
        # element is currently being acted on, since the dot alone is too
        # subtle for a viewer to tell which button/field is meant.
        ctx.add_init_script(CURSOR_INIT_SCRIPT)
        ctx.add_init_script(HIGHLIGHT_INIT_SCRIPT)

        import time
        t0 = time.monotonic()

        def mark(beat_id):
            boundaries[f"{beat_id}_end"] = round(time.monotonic() - t0, 2)
            print(f"  [{boundaries[f'{beat_id}_end']:>6.2f}s] {beat_id}")

        def hold(beat_id, extra_wait_ms=0, motion=None):
            """Wait out the remainder of this beat's narration. Pass
            `motion` (a list of selectors) for beats with no state-changing
            action of their own — e.g. a camera hold on an info box — so
            the cursor visibly drifts across the referenced elements for
            the whole wait instead of the screen freezing dead while the
            narration keeps talking."""
            total = beat_ms[beat_id] + extra_wait_ms
            if motion:
                share = total / len(motion)
                for sel in motion:
                    move_to(page, sel)
                    highlight(page, sel, ms=share + 300)
                    page.wait_for_timeout(share)
            else:
                page.wait_for_timeout(total)
            mark(beat_id)

        mod.record(page, hold, mark, beat_ms)

        ctx.close()
        video_path = page.video.path() if page.video else None
        browser.close()

    print("video:", video_path)
    print("boundaries:", boundaries)
    (ws / "boundaries.json").write_text(
        json.dumps({"video": str(video_path), "boundaries": boundaries}, indent=2)
    )
    return boundaries


# ══════════════════════════════════════════════════════════════════════════
# Stage 3 — cut / pad / mux / concat
# ══════════════════════════════════════════════════════════════════════════

def _run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def _ffprobe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def _generate_thumbnail(slug: str, video_path: Path, duration: float) -> Path:
    """Grab one representative frame from the finished video for the gallery
    grid — timed a little way in so it lands past any blank/loading state,
    but capped so it still works on short videos."""
    THUMBS_OUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = min(8.0, max(1.0, duration * 0.15))
    out_file = THUMBS_OUT_DIR / f"{slug}.jpg"
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", str(timestamp), "-i", str(video_path),
        "-frames:v", "1", "-vf", "scale=480:-1", "-q:v", "4",
        str(out_file),
    ])
    return out_file


def build_video(feature: str):
    mod = load_feature(feature)
    ws = _workspace(feature)
    work_dir = ws / "work"

    boundaries_data = json.loads((ws / "boundaries.json").read_text())
    raw_video = Path(boundaries_data["video"])
    boundaries = boundaries_data["boundaries"]
    manifest = json.loads((ws / "audio" / "manifest.json").read_text())

    # A feature may define FOOTAGE_MAP (beat_id -> (start, end) seconds) to
    # cut fixed timestamps directly out of FOOTAGE_PATH — a real, pre-existing
    # video — instead of deriving boundaries from a Playwright recording.
    # Beats not listed there fall back to the normal recorded-raw_video
    # timeline exactly as before (this is what every pre-existing feature
    # still does, unaffected by this branch).
    footage_map = getattr(mod, "FOOTAGE_MAP", {})
    footage_path = getattr(mod, "FOOTAGE_PATH", None)

    # When mixing a real pre-existing video (FOOTAGE_PATH) with Playwright-
    # recorded title cards, the two sources are almost never the same pixel
    # size (a phone screen capture vs. a fixed browser viewport). The final
    # concat step uses `-c copy` (no re-encode) for speed, which requires
    # every segment to share identical stream parameters — concatenating
    # mismatched resolutions doesn't error, it silently corrupts playback
    # partway through (observed: everything after the first resolution
    # switch played the wrong/frozen content). So every segment gets scaled
    # + letterboxed to the recorded raw_video's own resolution before
    # anything is muxed. Normal single-source features (no FOOTAGE_MAP) are
    # completely unaffected — this block is a no-op for them.
    target_w = target_h = target_fps = None
    if footage_map:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(raw_video)],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        target_w, target_h = (int(x) for x in probe.split(","))
        # Also normalized to a common frame rate before the final concat —
        # see the runaway-encode comment below. Title cards are recorded at
        # 25fps; real phone/screen footage is typically 30fps. Match the
        # footage's own rate since it's usually the dominant content.
        target_fps = 30

    beat_order = list(mod.BEATS.keys())

    beat_files = []
    prev_end = 0.0
    for beat_id in beat_order:
        seg_raw = work_dir / f"{beat_id}_raw.mp4"
        seg_padded = work_dir / f"{beat_id}_padded.mp4"
        seg_final = work_dir / f"{beat_id}_final.mp4"
        audio_file = manifest[beat_id]["file"]
        audio_duration = manifest[beat_id]["duration"]

        if beat_id in footage_map:
            src_video = footage_path
            start, end = footage_map[beat_id]
        else:
            src_video = raw_video
            start = prev_end
            end = boundaries[f"{beat_id}_end"]
            prev_end = end

        cut_cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(src_video),
            "-ss", str(start), "-to", str(end),
        ]
        if target_w:
            cut_cmd += [
                "-vf",
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black",
            ]
        cut_cmd += [
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
            str(seg_raw),
        ]
        _run(cut_cmd)
        seg_duration = _ffprobe_duration(seg_raw)

        target = audio_duration + 0.4
        hold = max(0.0, target - seg_duration)
        _run([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(seg_raw),
            "-vf", f"tpad=stop_mode=clone:stop_duration={hold}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(seg_padded),
        ])

        _run([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(seg_padded), "-i", str(audio_file),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac",
            "-shortest",
            str(seg_final),
        ])
        beat_files.append(seg_final)
        print(f"  {beat_id}: screen {seg_duration:.2f}s -> held to {target:.2f}s, audio {audio_duration:.2f}s")

    VIDEOS_OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = feature.replace("_", "-")
    out_file = VIDEOS_OUT_DIR / f"{slug}-tutorial.mp4"
    # Written to a temp path and moved into place atomically so a request
    # hitting the static route mid-write (or a CDN edge caching it) can
    # never see a half-written file. -movflags +faststart moves the moov
    # atom before mdat — plain concat -c copy otherwise leaves it at the
    # end of the file, which some players/CDNs handle poorly.
    tmp_file = out_file.with_suffix(".tmp.mp4")

    if target_w:
        # Segments here came from two different encode histories (footage
        # cuts vs. tpad-padded card recordings) — the concat demuxer's
        # `-c copy` just splices packets and trusts each segment's own PTS
        # to already be continuous, which broke here (observed: "Invalid
        # pts (x) <= last (y)", and the CTA card silently never displayed
        # even though all its frames were physically present in the file).
        #
        # The obvious fix is the concat *filter* (decodes+re-encodes,
        # producing fresh monotonic timestamps) — but that hit a second,
        # worse bug: mixing 25fps title-card segments with 30fps footage
        # segments in one filter_complex concat sent ffmpeg into a runaway
        # state, "completing" only after 9+ hours and producing a
        # multi-gigabyte, still-invalid (moov atom missing) file while
        # pegging a CPU core the whole time. Reproduced twice with the
        # identical command, so it's a real ffmpeg behavior on this input
        # mix, not a fluke.
        #
        # Fix: normalize every segment to one common fps/timebase first
        # (cheap — each is a small single-input re-encode, seconds not
        # hours), THEN use the fast, safe `-c copy` concat demuxer. Clean
        # continuous PTS across segments removes the original bug's cause
        # without ever invoking the runaway filter-graph path.
        norm_dir = work_dir / "norm"
        norm_dir.mkdir(exist_ok=True)
        norm_files = []
        for f in beat_files:
            norm_f = norm_dir / f.name
            _run([
                "ffmpeg", "-y", "-v", "error",
                "-i", str(f),
                "-r", str(target_fps), "-vsync", "cfr",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "44100", "-ac", "1",
                str(norm_f),
            ])
            norm_files.append(norm_f)
        concat_list = norm_dir / "concat_list.txt"
        concat_list.write_text("\n".join(f"file '{f.resolve()}'" for f in norm_files))
        _run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", "-movflags", "+faststart",
            str(tmp_file),
        ])
    else:
        concat_list = work_dir / "concat_list.txt"
        concat_list.write_text("\n".join(f"file '{f.resolve()}'" for f in beat_files))
        _run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", "-movflags", "+faststart",
            str(tmp_file),
        ])
    tmp_file.replace(out_file)

    final_duration = _ffprobe_duration(out_file)
    thumb_file = _generate_thumbnail(slug, out_file, final_duration)
    print(f"\nFinal video: {out_file} ({final_duration:.1f}s)")
    print(f"Thumbnail: {thumb_file}")
    return out_file


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 -m tutorial_studio.build <feature>")
        sys.exit(1)
    feature = sys.argv[1]
    print(f"=== generate_voice: {feature} ===")
    generate_voice(feature)
    print(f"\n=== record: {feature} ===")
    record_feature(feature)
    print(f"\n=== build_video: {feature} ===")
    build_video(feature)


if __name__ == "__main__":
    main()
