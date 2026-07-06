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

STUDIO_ROOT = Path(__file__).parent
WORKSPACE_ROOT = STUDIO_ROOT / "_workspace"
VIDEOS_OUT_DIR = STUDIO_ROOT.parent / "static" / "portal" / "tutorial" / "videos"
ENV_PATH = STUDIO_ROOT.parent / ".env"

VOICE_ID = "03fcf8ecb0a94b6b94e9007edb7c35f8"  # "Reassuring Rupert" — starfish-engine, calm male

# HeyGen TTS mispronounces the brand name if BEATS text spells it "PhiXtra" —
# always write it phonetically as "Fixtra" in narration text sent to the API
# (confirmed pronunciation: rhymes with "extra", starting with an F sound).
# UI text (gallery titles, subtitles) can still use the correct "PhiXtra" spelling.
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

def generate_speech(api_key, text):
    resp = requests.post(
        "https://api.heygen.com/v3/voices/speech",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        json={"text": text, "voice_id": VOICE_ID},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return data["audio_url"], data["duration"]


def generate_voice(feature: str):
    mod = load_feature(feature)
    ws = _workspace(feature)
    api_key = load_heygen_api_key()
    manifest = {}

    for beat_id, text in mod.BEATS.items():
        audio_url, duration = generate_speech(api_key, text)
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

        import time
        t0 = time.monotonic()

        def mark(beat_id):
            boundaries[f"{beat_id}_end"] = round(time.monotonic() - t0, 2)
            print(f"  [{boundaries[f'{beat_id}_end']:>6.2f}s] {beat_id}")

        def hold(beat_id, extra_wait_ms=0):
            page.wait_for_timeout(beat_ms[beat_id] + extra_wait_ms)
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


def build_video(feature: str):
    mod = load_feature(feature)
    ws = _workspace(feature)
    work_dir = ws / "work"

    boundaries_data = json.loads((ws / "boundaries.json").read_text())
    raw_video = Path(boundaries_data["video"])
    boundaries = boundaries_data["boundaries"]
    manifest = json.loads((ws / "audio" / "manifest.json").read_text())

    beat_order = list(mod.BEATS.keys())
    ends = [boundaries[f"{b}_end"] for b in beat_order]
    starts = [0.0] + ends[:-1]

    beat_files = []
    for beat_id, start, end in zip(beat_order, starts, ends):
        seg_raw = work_dir / f"{beat_id}_raw.mp4"
        seg_padded = work_dir / f"{beat_id}_padded.mp4"
        seg_final = work_dir / f"{beat_id}_final.mp4"
        audio_file = manifest[beat_id]["file"]
        audio_duration = manifest[beat_id]["duration"]

        _run([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(raw_video),
            "-ss", str(start), "-to", str(end),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
            str(seg_raw),
        ])
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

    concat_list = work_dir / "concat_list.txt"
    concat_list.write_text("\n".join(f"file '{f.resolve()}'" for f in beat_files))
    VIDEOS_OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = feature.replace("_", "-")
    out_file = VIDEOS_OUT_DIR / f"{slug}-tutorial.mp4"
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy",
        str(out_file),
    ])

    final_duration = _ffprobe_duration(out_file)
    print(f"\nFinal video: {out_file} ({final_duration:.1f}s)")
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
