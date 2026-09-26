"""
upload_design.py — Social Posts › Upload Design (2026-09-25). A business
posts its own finished images or video (made in Canva, Photoshop, by a
designer…). No AI, nothing counted.

This file holds the rules and the file work; the pages are in
upload_design_routes.py:
  * SPECS — each network's shapes, sizes, limits. Sizes follow Buffer's own
    guide (buffer.com/resources/social-media-image-sizes, May 2026) and
    Buffer's caption limits (developers.buffer.com/guides/character-limits).
    Change them here and every check, preview and guide follows.
  * probe() — the real size / shape / length of an uploaded file (PIL for
    images, ffprobe for video). Nothing trusts the file name.
  * check() — per network: ok / warn ("may be cropped", never blocks) /
    block ("too small", "shape not allowed", "too long"…).
  * fixes — crop, fit with borders, or a separate version for one network.
    The original is never changed. Images are made at once with PIL; video
    runs ffmpeg in a background thread (nice'd, time-limited) and the page
    polls until it's ready.
"""
import json
import os
import subprocess
import threading
import uuid
from fractions import Fraction

import psycopg2.extras

from db import get_db_connection

BASE = os.path.dirname(__file__)
FOLDER = os.path.join(BASE, "static", "uploads", "tenant_social_posts")

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}
VIDEO_EXTS = {"mp4", "mov", "m4v"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 300 * 1024 * 1024
MAX_ITEMS = 10
MIN_IMAGE_WIDTH = 600          # below this: blocks ("looks blurry")
GOOD_IMAGE_WIDTH = 1080        # below this: warning only
FFMPEG_TIMEOUT = 900

# ratio = width / height
R = {"1:1": 1.0, "4:5": 0.8, "3:4": 0.75, "9:16": 0.5625, "16:9": 16 / 9, "1.91:1": 1.91, "2:3": 2 / 3, "4:3": 4 / 3}

# Per network. shapes: (ratio name, "W x H", note). allowed: image ratio
# range the network accepts at all (outside = blocked until fixed). show:
# range shown uncropped in the feed (outside = "may be cropped", warning).
SPECS = {
    "instagram": {
        "label": "Instagram", "best": "4:5",
        "shapes": [("4:5", "1080 × 1350", "Best"), ("3:4", "1080 × 1440", "Tall grid"), ("1:1", "1080 × 1080", ""), ("1.91:1", "1080 × 566", "")],
        "allowed": (0.75, 1.91), "show": (0.75, 1.91), "max_images": 10, "caption": 2196, "hashtags": 30,
        "video": {"best": "9:16", "size": "1080 × 1920", "min_s": 3, "max_s": 15 * 60, "show": (0.5625, 0.5625)},
        "canva": "Instagram Post (4:5)",
    },
    "facebook": {
        "label": "Facebook", "best": "4:5",
        "shapes": [("4:5", "1080 × 1350", "Best"), ("1:1", "1080 × 1080", ""), ("1.91:1", "1200 × 630", "")],
        "allowed": None, "show": (0.8, 1.91), "max_images": 10, "caption": 5000,
        "video": {"best": "4:5", "size": "1080 × 1350", "min_s": 1, "max_s": 240 * 60, "show": (0.5625, 1.78)},
        "canva": "Facebook Post",
    },
    "linkedin": {
        "label": "LinkedIn", "best": "4:5",
        "shapes": [("4:5", "1080 × 1350", "Best"), ("1:1", "1200 × 1200", ""), ("1.91:1", "1200 × 627", "")],
        "allowed": None, "show": (0.8, 1.91), "max_images": 9, "caption": 3000,
        "video": {"best": "1:1", "size": "1080 × 1080", "min_s": 3, "max_s": 15 * 60, "show": (0.5625, 1.78)},
        "canva": "LinkedIn Post",
    },
    "twitter": {
        "label": "X", "best": "16:9",
        "shapes": [("16:9", "1600 × 900", "Best"), ("1:1", "1080 × 1080", ""), ("4:5", "1080 × 1350", "")],
        "allowed": None, "show": (0.8, 1.78), "max_images": 4, "caption": 280,
        "video": {"best": "16:9", "size": "1920 × 1080", "min_s": 1, "max_s": 140, "show": (0.8, 1.78)},
        "canva": "Twitter Post",
    },
    "tiktok": {
        "label": "TikTok", "best": "9:16",
        "shapes": [("9:16", "1080 × 1920", "Best"), ("4:5", "1080 × 1350", "")],
        "allowed": None, "show": (0.5625, 0.8), "max_images": 35, "caption": 4000, "caption_video": 2200,
        "video": {"best": "9:16", "size": "1080 × 1920", "min_s": 3, "max_s": 10 * 60, "show": (0.5625, 0.5625)},
        "canva": "TikTok Video",
    },
    "threads": {
        "label": "Threads", "best": "4:5",
        "shapes": [("4:5", "1080 × 1350", "Best"), ("1:1", "1080 × 1080", "")],
        "allowed": None, "show": None, "max_images": 10, "caption": 500,
        "video": {"best": "9:16", "size": "1080 × 1920", "min_s": 1, "max_s": 5 * 60, "show": None},
        "canva": "Instagram Post (4:5)",
    },
    "googleBusiness": {
        "label": "Google Business", "best": "4:3",
        "shapes": [("4:3", "1200 × 900", "Best")],
        "allowed": None, "show": (1.2, 1.45), "max_images": 1, "caption": 4000,
        "video": None, "canva": "Custom size 1200 × 900",
    },
    "mastodon": {"label": "Mastodon", "best": "16:9", "shapes": [("16:9", "1600 × 900", "Best"), ("1:1", "1080 × 1080", "")],
                 "allowed": None, "show": None, "max_images": 4, "caption": 500,
                 "video": {"best": "16:9", "size": "1920 × 1080", "min_s": 1, "max_s": 60 * 60, "show": None}, "canva": "Twitter Post"},
    "bluesky": {"label": "Bluesky", "best": "1:1", "shapes": [("1:1", "1080 × 1080", "Best"), ("4:5", "1080 × 1350", ""), ("16:9", "1600 × 900", "")],
                "allowed": None, "show": None, "max_images": 4, "caption": 300,
                "video": {"best": "16:9", "size": "1920 × 1080", "min_s": 1, "max_s": 3 * 60, "show": None}, "canva": "Instagram Post (Square)"},
}
# Buffer needs extra details for these (a board, a title) — not offered here yet.
UNSUPPORTED = {"pinterest": "Pinterest needs a board chosen for every Pin, which Upload Design doesn't ask for yet.",
               "youtube": "YouTube needs a video title, which Upload Design doesn't ask for yet."}

# Extra rows only for the size guide page.
GUIDE_EXTRA = [
    ("Instagram Story / Reel", "9:16", "1080 × 1920", "—", "Instagram Story"),
    ("Pinterest", "2:3", "1000 × 1500", "—", "Pinterest Pin"),
    ("YouTube (video)", "16:9", "1920 × 1080", "9:16 for Shorts", "YouTube Thumbnail / Video"),
]


def ratio_name(w, h) -> str:
    if not w or not h:
        return "?"
    r = w / h
    best = min(R.items(), key=lambda kv: abs(kv[1] - r))
    if abs(best[1] - r) < 0.02:
        return best[0]
    f = Fraction(w, h).limit_denominator(20)
    return f"{f.numerator}:{f.denominator}"


def fmt_bytes(n) -> str:
    n = int(n or 0)
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, n // 1024)} KB"


def fmt_dur(s) -> str:
    """A video's length, e.g. 2:25."""
    s = int(round(float(s or 0)))
    return f"{s // 60}:{s % 60:02d}"


def fmt_limit(s) -> str:
    """A time limit in words, e.g. 2 min 20 s, 15 min, 4 hours."""
    s = int(s or 0)
    if s >= 3600 and s % 3600 == 0:
        return f"{s // 3600} hour{'s' if s >= 7200 else ''}"
    if s >= 60:
        m, r = divmod(s, 60)
        return f"{m} min" + (f" {r} s" if r else "")
    return f"{s} s"


# ══════════════════════════════════════════════════════════════════════════
# drafts
# ══════════════════════════════════════════════════════════════════════════

def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def create_draft(tenant_id, channels, actor) -> int:
    cleanup_old(tenant_id)
    conn, cur = _db()
    try:
        cur.execute("INSERT INTO upload_drafts (tenant_id, channels, created_by) VALUES (%s,%s,%s) RETURNING id",
                    (tenant_id, json.dumps(channels), actor))
        did = cur.fetchone()["id"]
        conn.commit()
        return did
    finally:
        cur.close(); conn.close()


def get_draft(tenant_id, did):
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM upload_drafts WHERE id=%s AND tenant_id=%s", (did, tenant_id))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        cur.close(); conn.close()


def save_draft(d) -> None:
    conn, cur = _db()
    try:
        cur.execute("UPDATE upload_drafts SET channels=%s, items=%s, fixes=%s, post_id=%s, updated_at=NOW() WHERE id=%s",
                    (json.dumps(d["channels"]), json.dumps(d["items"]), json.dumps(d["fixes"]), d.get("post_id"), d["id"]))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _draft_files(d) -> set:
    names = set()
    for it in d.get("items") or []:
        names.update(x for x in (it.get("file"), it.get("thumb")) if x)
    for fx in (d.get("fixes") or {}).values():
        for out in (fx.get("outputs") or {}).values():
            names.update(x for x in (out.get("file"), out.get("thumb")) if x)
        for it in fx.get("alt_items") or []:
            names.update(x for x in (it.get("file"), it.get("thumb")) if x)
    return names


def _post_files(tenant_id) -> set:
    conn, cur = _db()
    try:
        cur.execute("SELECT image_filename, media FROM tenant_social_posts WHERE tenant_id=%s AND media IS NOT NULL", (tenant_id,))
        keep = set()
        for r in cur.fetchall() or []:
            keep.add(r["image_filename"])
            for lst in (r["media"] or {}).values():
                for m in lst:
                    keep.update(x for x in (m.get("file"), m.get("thumb")) if x)
        return keep
    finally:
        cur.close(); conn.close()


def remove_files(names, keep=()) -> None:
    for n in names:
        if n and n not in keep and "/" not in n:
            for p in (os.path.join(FOLDER, n), os.path.join(FOLDER, n + ".job")):
                try:
                    os.remove(p)
                except OSError:
                    pass


def cleanup_old(tenant_id) -> None:
    """Drafts nobody posted within 14 days are deleted with their files."""
    try:
        conn, cur = _db()
        cur.execute("""SELECT * FROM upload_drafts WHERE tenant_id=%s AND post_id IS NULL
                       AND updated_at < NOW() - INTERVAL '14 days'""", (tenant_id,))
        old = cur.fetchall() or []
        cur.close(); conn.close()
        if not old:
            return
        keep = _post_files(tenant_id)
        for d in old:
            remove_files(_draft_files(d), keep)
        conn, cur = _db()
        cur.execute("DELETE FROM upload_drafts WHERE id = ANY(%s)", ([d["id"] for d in old],))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ upload_design cleanup:", e)


# ══════════════════════════════════════════════════════════════════════════
# reading files
# ══════════════════════════════════════════════════════════════════════════

class UploadError(Exception):
    pass


def _ffprobe(path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise UploadError("That video couldn't be read. Export it again as MP4 and retry.")
    return json.loads(out.stdout or "{}")


def _video_info(path) -> dict:
    info = _ffprobe(path)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise UploadError("That file has no video in it.")
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = abs(int(float(sd["rotation"])))
    if str((v.get("tags") or {}).get("rotate", "0")).lstrip("-") in ("90", "270"):
        rot = 90
    if rot in (90, 270):
        w, h = h, w
    dur = float((info.get("format") or {}).get("duration") or v.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return {"w": w, "h": h, "dur": dur, "codec": v.get("codec_name"), "fmt": (info.get("format") or {}).get("format_name", ""),
            "audio": has_audio}


def _video_thumb(path) -> str:
    name = f"{uuid.uuid4().hex}_thumb.jpg"
    subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", path, "-frames:v", "1",
                    "-vf", "scale='min(720,iw)':-2", os.path.join(FOLDER, name)], capture_output=True, timeout=120)
    if not os.path.exists(os.path.join(FOLDER, name)):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-frames:v", "1", "-vf", "scale='min(720,iw)':-2",
                        os.path.join(FOLDER, name)], capture_output=True, timeout=120)
    return name if os.path.exists(os.path.join(FOLDER, name)) else None


def store_upload(fs) -> dict:
    """Saves one uploaded file (Flask FileStorage) and reads its real details."""
    fname = fs.filename or "upload"
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in IMAGE_EXTS | VIDEO_EXTS:
        raise UploadError(f"“{fname}” isn't a supported file. Use JPG, PNG or WEBP images, or MP4 / MOV video.")
    os.makedirs(FOLDER, exist_ok=True)
    kind = "video" if ext in VIDEO_EXTS else "image"
    limit = MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES
    tmp = os.path.join(FOLDER, f"{uuid.uuid4().hex}.{ext}")
    size = 0
    with open(tmp, "wb") as fh:
        while True:
            chunk = fs.stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                fh.close()
                os.remove(tmp)
                raise UploadError(f"“{fname}” is over {limit // 1048576} MB. Export a smaller file and retry.")
            fh.write(chunk)
    item = {"id": uuid.uuid4().hex[:10], "kind": kind, "orig": fname[:120], "size": size, "status": "ready"}
    try:
        if kind == "image":
            from PIL import Image, ImageOps
            with Image.open(tmp) as im:
                im.load()
                im = ImageOps.exif_transpose(im)
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
                item["w"], item["h"] = im.size
                # Re-save: fixes phone rotation and drops hidden data. PNG stays PNG.
                out_ext = "png" if ext == "png" else "jpg"
                out = f"{uuid.uuid4().hex}.{out_ext}"
                if out_ext == "jpg":
                    im.convert("RGB").save(os.path.join(FOLDER, out), "JPEG", quality=92, optimize=True)
                else:
                    im.save(os.path.join(FOLDER, out), "PNG", optimize=True)
            os.remove(tmp)
            item["file"] = out
            item["size"] = os.path.getsize(os.path.join(FOLDER, out))
        else:
            vi = _video_info(tmp)
            item.update(w=vi["w"], h=vi["h"], dur=round(vi["dur"], 2), codec=vi["codec"], audio=vi["audio"])
            item["thumb"] = _video_thumb(tmp)
            needs_convert = vi["codec"] != "h264" or "mp4" not in vi["fmt"] or ext != "mp4"
            if needs_convert:
                # Networks want H.264 MP4. iPhone HEVC / MOV get converted in the background.
                out = f"{uuid.uuid4().hex}.mp4"
                item["file"] = out
                item["status"] = "processing"
                item["note"] = "Converting to MP4 so every network accepts it…"
                _start_ffmpeg(tmp, out, vf=None, copy_ok=(vi["codec"] == "h264"), remove_src=True, item_ref=None)
                item["_job"] = out
            else:
                final = f"{uuid.uuid4().hex}.mp4"
                os.rename(tmp, os.path.join(FOLDER, final))
                item["file"] = final
    except UploadError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        print("⚠️ upload_design store:", e)
        raise UploadError(f"“{fname}” couldn't be opened. Export it again and retry.")
    item["ratio"] = ratio_name(item.get("w"), item.get("h"))
    return item


# ── background video jobs ───────────────────────────────────────────────────
# State lives in a marker file next to the output (<out>.job: "running" or
# "failed: …") so every gunicorn worker sees the same thing; the finished
# file only appears (atomic rename) when ffmpeg succeeded.
_JOB_SEM = threading.Semaphore(1)     # one ffmpeg at a time per portal worker


def _marker(out):
    return os.path.join(FOLDER, out + ".job")


def _start_ffmpeg(src, out, vf=None, copy_ok=False, remove_src=False, item_ref=None):
    with open(_marker(out), "w") as fh:
        fh.write("running")

    def encode_cmd(partial):
        cmd = ["nice", "-n", "15", "ffmpeg", "-v", "error", "-y", "-i", src]
        if vf:
            cmd += ["-vf", vf]
        return cmd + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                      "-threads", "2", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", partial]

    def run():
        with _JOB_SEM:
            dst = os.path.join(FOLDER, out)
            partial = dst + ".part.mp4"
            try:
                attempts = []
                if copy_ok and not vf:
                    attempts.append(["ffmpeg", "-v", "error", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", partial])
                attempts.append(encode_cmd(partial))
                err = "ffmpeg failed"
                for cmd in attempts:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
                    if res.returncode == 0 and os.path.exists(partial) and os.path.getsize(partial) > 0:
                        os.replace(partial, dst)
                        try:
                            os.remove(_marker(out))
                        except OSError:
                            pass
                        return
                    err = (res.stderr or err)[-300:]
                raise RuntimeError(err)
            except Exception as e:
                try:
                    os.remove(partial)
                except OSError:
                    pass
                with open(_marker(out), "w") as fh:
                    fh.write(f"failed: {str(e)[:250]}")
                print("⚠️ upload_design ffmpeg:", e)
            finally:
                if remove_src:
                    try:
                        os.remove(src)
                    except OSError:
                        pass

    threading.Thread(target=run, daemon=True).start()


def job_state(out_name) -> str:
    if os.path.exists(os.path.join(FOLDER, out_name)):
        return "done"
    try:
        with open(_marker(out_name)) as fh:
            st = fh.read().strip()
    except OSError:
        return "failed: the portal restarted while processing. Upload again."
    if st == "running":
        import time
        if time.time() - os.path.getmtime(_marker(out_name)) > FFMPEG_TIMEOUT + 120:
            return "failed: processing took too long. Try a shorter or smaller video."
    return st


def refresh_jobs(d) -> bool:
    """Marks finished background video work as ready. Returns True if anything changed."""
    changed = False
    def settle(obj):
        nonlocal changed
        if obj.get("status") == "processing" and obj.get("_job"):
            st = job_state(obj["_job"])
            if st == "done":
                obj["status"] = "ready"
                obj.pop("note", None)
                obj.pop("_job", None)
                p = os.path.join(FOLDER, obj["file"])
                obj["size"] = os.path.getsize(p) if os.path.exists(p) else obj.get("size")
                changed = True
            elif st.startswith("failed"):
                obj["status"] = "failed"
                obj["note"] = "Processing failed. " + st.split(":", 1)[-1].strip()[:200]
                obj.pop("_job", None)
                changed = True
    for it in d.get("items") or []:
        settle(it)
    for fx in (d.get("fixes") or {}).values():
        for out in (fx.get("outputs") or {}).values():
            settle(out)
        for it in fx.get("alt_items") or []:
            settle(it)
    return changed


# ══════════════════════════════════════════════════════════════════════════
# checks
# ══════════════════════════════════════════════════════════════════════════

def media_for(d, service) -> list:
    """What would actually be posted to this network, fixes applied."""
    fx = (d.get("fixes") or {}).get(service)
    if fx and fx.get("mode") == "alt" and fx.get("alt_items"):
        return fx["alt_items"]
    items = d.get("items") or []
    if fx and fx.get("mode") in ("crop", "fit"):
        outs = fx.get("outputs") or {}
        return [dict(it, **outs[it["id"]]) if it["id"] in outs else it for it in items]
    return items


def _in(r, rng, tol=0.02):
    return rng is None or (rng[0] - tol <= r <= rng[1] + tol)


def check(d, service) -> dict:
    """{"level": ok|warn|block|processing, "lines": [...], "fixable": bool}"""
    spec = SPECS.get(service)
    if not spec:
        return {"level": "block", "lines": [UNSUPPORTED.get(service, "This network isn't supported by Upload Design yet.")], "fixable": False}
    media = media_for(d, service)
    if not media:
        return {"level": "block", "lines": ["Upload a design first."], "fixable": False}
    lines, level = [], "ok"
    def bump(l):
        nonlocal level
        order = {"ok": 0, "warn": 1, "processing": 2, "block": 3}
        if order[l] > order[level]:
            level = l
    videos = [m for m in media if m["kind"] == "video"]
    if videos and len(media) > 1:
        return {"level": "block", "lines": ["A post can have one video, or up to 10 images, not both."], "fixable": False}
    if any(m.get("status") == "processing" for m in media):
        bump("processing")
        lines.append("Still processing the video. This page updates by itself.")
    if any(m.get("status") == "failed" for m in media):
        bump("block")
        lines.append(next(m.get("note") for m in media if m.get("status") == "failed") or "Processing failed. Upload again.")
    if videos:
        v, vs = videos[0], spec.get("video")
        if not vs:
            return {"level": "block", "lines": [f"{spec['label']} posts here can't include video. Untick it or use an image."], "fixable": False}
        r = v["w"] / v["h"] if v.get("h") else 1
        if v.get("dur", 0) < vs["min_s"]:
            bump("block"); lines.append(f"Too short for {spec['label']}: at least {vs['min_s']} seconds.")
        if v.get("dur", 0) > vs["max_s"]:
            bump("block"); lines.append(f"Too long for {spec['label']}: {fmt_dur(v['dur'])}, the limit is {fmt_limit(vs['max_s'])}. Trim it and upload again.")
        if min(v.get("w") or 0, v.get("h") or 0) < 480:
            bump("warn"); lines.append(f"Low resolution ({v['w']} × {v['h']}). It may look blurry; {vs['size']} is best.")
        if not _in(r, vs.get("show")):
            bump("warn"); lines.append(f"{spec['label']} shows video best at {vs['best']} ({vs['size']}). Yours is {ratio_name(v['w'], v['h'])}, so it may be cropped or shown with bars.")
        if level == "ok":
            lines.append(f"{ratio_name(v['w'], v['h'])} video, {fmt_dur(v['dur'])}. Good for {spec['label']}.")
        return {"level": level, "lines": lines, "fixable": True}
    if len(media) > spec["max_images"]:
        bump("warn"); lines.append(f"{spec['label']} takes up to {spec['max_images']} image{'s' if spec['max_images'] > 1 else ''}; only the first {spec['max_images']} will be sent.")
    for i, m in enumerate(media):
        if m.get("status") != "ready":
            continue
        tag = f"Image {i + 1}: " if len(media) > 1 else ""
        r = m["w"] / m["h"] if m.get("h") else 1
        if m["w"] < MIN_IMAGE_WIDTH:
            bump("block"); lines.append(f"{tag}{m['w']} × {m['h']} is too small and will look blurry. Download it again at least {GOOD_IMAGE_WIDTH} pixels wide (Canva: Size ×1 or more).")
        elif m["w"] < GOOD_IMAGE_WIDTH and min(m["w"], m["h"]) < GOOD_IMAGE_WIDTH:
            bump("warn"); lines.append(f"{tag}{m['w']} × {m['h']} is a bit small. {GOOD_IMAGE_WIDTH} pixels wide looks sharper.")
        if spec["allowed"] and not _in(r, spec["allowed"], 0.01):
            bump("block"); lines.append(f"{tag}{ratio_name(m['w'], m['h'])} isn't a shape {spec['label']} allows ({spec['allowed'][0]:.2f} to {spec['allowed'][1]:.2f} wide per tall). Crop it or add borders.")
        elif not _in(r, spec["show"]):
            bump("warn"); lines.append(f"{tag}{spec['label']} may trim a {ratio_name(m['w'], m['h'])} image in the feed. Best is {spec['best']} ({dict((s[0], s[1]) for s in spec['shapes'])[spec['best']]}).")
    if level == "ok":
        m = media[0]
        best = spec["best"]
        lines.append(f"{ratio_name(m['w'], m['h'])} {'is ' + spec['label'] + chr(39) + 's best shape' if ratio_name(m['w'], m['h']) == best else 'shows full size'}.")
    return {"level": level, "lines": lines, "fixable": True}


def display_ratio(service, item) -> float:
    """The frame the network's feed shows this media in (for previews)."""
    spec = SPECS.get(service) or {}
    r = item["w"] / item["h"] if item.get("h") else 1
    rng = (spec.get("video") or {}).get("show") if item.get("kind") == "video" else spec.get("show")
    if not rng:
        return r
    return max(rng[0], min(rng[1], r))


# ══════════════════════════════════════════════════════════════════════════
# fixes
# ══════════════════════════════════════════════════════════════════════════

def target_size(service, ratio_key, kind="image"):
    """Output pixel size for a ratio: the network's own recommended size when
    it lists one for that shape (e.g. LinkedIn 1:1 = 1200 × 1200), else 1080
    wide (or 1920 tall for 9:16)."""
    spec = SPECS.get(service) or {}
    sizes = {r_: px for r_, px, _n in spec.get("shapes", [])}
    if kind == "video" and spec.get("video"):
        sizes = {spec["video"]["best"]: spec["video"]["size"]}
    if ratio_key in sizes:
        w, h = [int(n) for n in sizes[ratio_key].replace(" ", "").split("×")]
        return w - w % 2, h - h % 2
    r = R.get(ratio_key, 1.0)
    if r >= 1:
        w = 1600 if r > 1.7 else 1200 if r > 1.2 else 1080
        return w, int(round(w / r / 2) * 2)
    h = 1920 if r < 0.6 else 1350 if r < 0.85 else 1080
    return int(round(h * r / 2) * 2), h


def _hex_rgb(hx):
    hx = (hx or "#FFFFFF").lstrip("#")
    return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4)) if len(hx) == 6 else (255, 255, 255)


def make_fix(d, service, mode, ratio_key, crops=None, colour="#FFFFFF") -> None:
    """Builds the crop / fit-with-borders versions for one network.
    crops: {item_id: [x, y, w, h]} as fractions of the original."""
    from PIL import Image, ImageFilter
    fx = {"mode": mode, "ratio": ratio_key, "colour": colour, "crops": crops or {}, "outputs": {}}
    old = (d.get("fixes") or {}).get(service)
    is_video = any(it.get("kind") == "video" for it in d["items"])
    tw, th = target_size(service, ratio_key, "video" if is_video else "image")
    for it in d["items"]:
        if it.get("status") != "ready":
            continue
        src = os.path.join(FOLDER, it["file"])
        c = (crops or {}).get(it["id"])
        if it["kind"] == "image":
            with Image.open(src) as im:
                im = im.convert("RGB")
                if mode == "crop":
                    if not c:
                        c = centre_crop(it["w"], it["h"], R[ratio_key])
                    x, y, w, h = c
                    box = (int(x * it["w"]), int(y * it["h"]), int((x + w) * it["w"]), int((y + h) * it["h"]))
                    out_im = im.crop(box).resize((tw, th), Image.LANCZOS)
                else:
                    if colour == "blur":
                        bg = im.resize((tw, th), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
                    else:
                        bg = Image.new("RGB", (tw, th), _hex_rgb(colour))
                    scale = min(tw / it["w"], th / it["h"])
                    fg = im.resize((max(1, int(it["w"] * scale)), max(1, int(it["h"] * scale))), Image.LANCZOS)
                    bg.paste(fg, ((tw - fg.width) // 2, (th - fg.height) // 2))
                    out_im = bg
                name = f"{uuid.uuid4().hex}.jpg"
                out_im.save(os.path.join(FOLDER, name), "JPEG", quality=92, optimize=True)
            fx["outputs"][it["id"]] = {"file": name, "w": tw, "h": th, "ratio": ratio_key, "status": "ready",
                                       "size": os.path.getsize(os.path.join(FOLDER, name))}
        else:
            if mode == "crop":
                if not c:
                    c = centre_crop(it["w"], it["h"], R[ratio_key])
                x, y, w, h = c
                vf = (f"crop=trunc(iw*{w:.4f}/2)*2:trunc(ih*{h:.4f}/2)*2:trunc(iw*{x:.4f}):trunc(ih*{y:.4f}),"
                      f"scale={tw}:{th},setsar=1")
            else:
                col = "black" if colour == "blur" else "0x" + (colour or "#000000").lstrip("#")
                vf = (f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                      f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color={col},setsar=1")
            name = f"{uuid.uuid4().hex}.mp4"
            _start_ffmpeg(src, name, vf=vf)
            fx["outputs"][it["id"]] = {"file": name, "w": tw, "h": th, "ratio": ratio_key, "status": "processing",
                                       "_job": name, "thumb": it.get("thumb"), "note": "Making the video for this network…"}
    d.setdefault("fixes", {})[service] = fx
    if old:
        remove_files(_fix_files(old))


def _fix_files(fx) -> set:
    names = set()
    for out in (fx.get("outputs") or {}).values():
        if out.get("file"):
            names.add(out["file"])
    for it in fx.get("alt_items") or []:
        names.update(x for x in (it.get("file"), it.get("thumb")) if x)
    return names


def set_alt(d, service, items) -> None:
    old = (d.get("fixes") or {}).get(service)
    d.setdefault("fixes", {})[service] = {"mode": "alt", "alt_items": items}
    if old:
        remove_files(_fix_files(old))


def clear_fix(d, service) -> None:
    old = (d.get("fixes") or {}).pop(service, None)
    if old:
        remove_files(_fix_files(old))


def centre_crop(w, h, r):
    """Largest centred box of ratio r, as fractions [x, y, w, h]."""
    if w / h > r:
        cw = h * r / w
        return [round((1 - cw) / 2, 4), 0.0, round(cw, 4), 1.0]
    ch = w / r / h
    return [0.0, round((1 - ch) / 2, 4), 1.0, round(ch, 4)]


# ══════════════════════════════════════════════════════════════════════════
# captions
# ══════════════════════════════════════════════════════════════════════════

def caption_limit(service, has_video=False) -> int:
    spec = SPECS.get(service) or {}
    if has_video and spec.get("caption_video"):
        return spec["caption_video"]
    return spec.get("caption", 2200)


def caption_length(service, text) -> int:
    """Counts the way the network does (approximately): Instagram counts a
    line break as 2, X counts links as 23 and emoji as 2."""
    import re
    text = text or ""
    if service == "instagram":
        return len(text) + text.count("\n")
    if service == "twitter":
        t = re.sub(r"https?://\S+", "x" * 23, text)
        return sum(2 if ord(ch) > 0x2FFF else 1 for ch in t)
    return len(text)


def caption_problems(service, text, has_video=False) -> list:
    probs = []
    n, lim = caption_length(service, text), caption_limit(service, has_video)
    label = (SPECS.get(service) or {}).get("label", service)
    if n > lim:
        probs.append(f"Caption too long for {label}: {n:,} / {lim:,}.")
    if service == "instagram" and (text or "").count("#") > 30:
        probs.append("Instagram allows at most 30 hashtags.")
    return probs
