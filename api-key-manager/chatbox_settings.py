"""Customise chat box (Phase 1, 9 Nov 2026).

A business's own look for the website chat box: welcome message, colour,
font, window title/subtitle, logo, AI Agent avatar, window size, emojis and
stickers. Saved in chatbox_settings (one row per business); the AI service
(ai-backend /widget/config) reads the same row. Nothing set = today's look.

Images (logo, avatar) are public — any website visitor's browser loads them —
and live on the big disk under CHATBOX_MEDIA_DIR with random names.
"""
import io
import json
import os
import re
import uuid

from PIL import Image, ImageOps

from db import get_db_connection

MEDIA_DIR = os.getenv("CHATBOX_MEDIA_DIR", "/vault/phixtra-media/chatbox")
MEDIA_URL = "/media/chatbox"
BASE_URL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")

FONTS = ["store", "Inter", "Roboto", "Lato", "Merriweather"]
AVATARS = ["logo", "bot", "upload", "none"]
DEFAULT_COLOUR = "#0B1D40"
MAX_IMG_BYTES = 5 * 1024 * 1024
IMG_EDGE = 256
LIMITS = {"welcome": 200, "title": 40, "subtitle": 60, "btn_words": 24, "question": 40}
# Phase 2 (9 Nov 2026): chat button + quick-question buttons.
_P2_COLS = {"btn_position": "VARCHAR(5) NOT NULL DEFAULT 'right'", "btn_style": "VARCHAR(6) NOT NULL DEFAULT 'round'",
            "btn_words": "VARCHAR(24)", "show_on_phones": "BOOLEAN NOT NULL DEFAULT TRUE", "quick_qs": "JSONB",
            # Phase 3 (10 Nov 2026): business hours, Monday first, in the business's own time zone.
            "hours_on": "BOOLEAN NOT NULL DEFAULT FALSE", "hours": "JSONB", "hours_tz": "VARCHAR(64)",
            # WooCommerce in one place (10 Nov 2026): every position the plugin had,
            # for every platform, and the plugin's text colours / size. NULL = automatic.
            "btn_vpos": "VARCHAR(6) NOT NULL DEFAULT 'bottom'", "agent_text": "VARCHAR(7)",
            "visitor_text": "VARCHAR(7)", "visitor_size": "SMALLINT"}
VPOS = ["bottom", "middle", "top"]
TEXT_SIZES = list(range(10, 25))
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEFAULT_HOURS = [{"on": i < 5, "from": "09:00", "to": "17:30"} for i in range(7)]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[03]0$")


def tz_name(tenant_id: int) -> str:
    try:
        import staff_activity
        return staff_activity.tenant_tz_name(int(tenant_id))
    except Exception:
        return "Europe/London"


def _clean_hours(raw) -> list:
    out = []
    for i in range(7):
        d = raw[i] if isinstance(raw, list) and i < len(raw) and isinstance(raw[i], dict) else DEFAULT_HOURS[i]
        f, t = str(d.get("from") or "09:00"), str(d.get("to") or "17:30")
        out.append({"on": bool(d.get("on")), "from": f if _TIME_RE.match(f) else "09:00", "to": t if _TIME_RE.match(t) else "17:30"})
    return out

_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_LINK_RE = re.compile(r"(https?://|www\.)", re.I)
_NAME_RE = re.compile(r"^[0-9a-f]{32}\.png$")


def ensure(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chatbox_settings (
            tenant_id    INTEGER PRIMARY KEY,
            welcome      TEXT,
            colour       VARCHAR(7),
            font         VARCHAR(20),
            title        VARCHAR(40),
            subtitle     VARCHAR(60),
            logo_file    VARCHAR(40),
            avatar_mode  VARCHAR(10) NOT NULL DEFAULT 'logo',
            avatar_file  VARCHAR(40),
            size         VARCHAR(10) NOT NULL DEFAULT 'standard',
            emojis_on    BOOLEAN NOT NULL DEFAULT TRUE,
            ai_emojis    BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by   VARCHAR(120)
        )""")
    # Phase 2 columns: added only when missing (an ALTER on every page load
    # would queue behind any open read of the table).
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='chatbox_settings'")
    have = {(r[0] if not isinstance(r, dict) else r["column_name"]) for r in cur.fetchall()}
    for col, typ in _P2_COLS.items():
        if col not in have:
            cur.execute(f"ALTER TABLE chatbox_settings ADD COLUMN IF NOT EXISTS {col} {typ}")
    # Welcome message in the visitor's language, translated once by the AI
    # Agent and reused (tenant 0 = the standard welcome).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chatbox_welcome_tr (
            tenant_id  INTEGER NOT NULL,
            lang       VARCHAR(12) NOT NULL,
            src        TEXT NOT NULL,
            text       TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, lang)
        )""")


def file_url(tenant_id: int, fname: str | None) -> str | None:
    return f"{BASE_URL}{MEDIA_URL}/{int(tenant_id)}/{fname}" if fname else None


def safe_name(fname: str) -> bool:
    return bool(_NAME_RE.match(fname or ""))


def get(tenant_id: int) -> dict:
    """The business's settings with defaults filled in (never None)."""
    row = {}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ensure(cur); conn.commit()
        cur.execute("SELECT * FROM chatbox_settings WHERE tenant_id=%s", (int(tenant_id),))
        r = cur.fetchone()
        if r:
            cols = [d[0] for d in cur.description]
            row = r if isinstance(r, dict) else dict(zip(cols, r))
            # Keep the hours' time zone in step with Settings (the AI service reads hours_tz).
            if row.get("hours_on") and row.get("hours_tz") != tz_name(tenant_id):
                cur.execute("UPDATE chatbox_settings SET hours_tz=%s WHERE tenant_id=%s", (tz_name(tenant_id), int(tenant_id)))
                conn.commit()
    finally:
        conn.close()
    return {
        "welcome": row.get("welcome") or "",
        "colour": row.get("colour") or DEFAULT_COLOUR,
        "font": row.get("font") or "Inter",
        "title": row.get("title") or "",
        "subtitle": row.get("subtitle") or "",
        "logo_url": file_url(tenant_id, row.get("logo_file")),
        "avatar_mode": row.get("avatar_mode") or "logo",
        "avatar_url": file_url(tenant_id, row.get("avatar_file")),
        "size": row.get("size") or "standard",
        "emojis_on": True if row.get("emojis_on") is None else bool(row.get("emojis_on")),
        "ai_emojis": bool(row.get("ai_emojis")),
        "btn_position": "left" if row.get("btn_position") == "left" else "right",
        "btn_vpos": row.get("btn_vpos") if row.get("btn_vpos") in VPOS else "bottom",
        "agent_text": row.get("agent_text") or "",
        "visitor_text": row.get("visitor_text") or "",
        "visitor_size": int(row["visitor_size"]) if row.get("visitor_size") else None,
        "btn_style": "words" if row.get("btn_style") == "words" else "round",
        "btn_words": row.get("btn_words") or "",
        "show_on_phones": True if row.get("show_on_phones") is None else bool(row.get("show_on_phones")),
        "quick_qs": [q for q in (row.get("quick_qs") or []) if isinstance(q, str)][:4],
        "hours_on": bool(row.get("hours_on")),
        "hours": _clean_hours(row.get("hours")),
        "tz": tz_name(tenant_id),
        "saved": bool(row),
    }


def _square_png(raw: bytes) -> bytes:
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
        im.load()
    except Exception:
        raise ValueError("isn't an image we can read. Use a PNG, JPG or WebP file.")
    im = im.convert("RGBA")
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    im = im.resize((IMG_EDGE, IMG_EDGE), Image.LANCZOS) if side > IMG_EDGE else im
    out = io.BytesIO(); im.save(out, "PNG", optimize=True)
    return out.getvalue()


def _save_image(tenant_id: int, fs) -> str:
    name = (fs.filename or "").lower()
    if not name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise ValueError("Use a PNG, JPG or WebP image.")
    raw = fs.read(MAX_IMG_BYTES + 1)
    if len(raw) > MAX_IMG_BYTES:
        raise ValueError("That image is bigger than 5 MB.")
    png = _square_png(raw)
    folder = os.path.join(MEDIA_DIR, str(int(tenant_id)))
    os.makedirs(folder, exist_ok=True)
    fname = uuid.uuid4().hex + ".png"
    with open(os.path.join(folder, fname), "wb") as f:
        f.write(png)
    return fname


def _drop_file(tenant_id: int, fname: str | None):
    if fname and safe_name(fname):
        try:
            os.remove(os.path.join(MEDIA_DIR, str(int(tenant_id)), fname))
        except OSError:
            pass


def save(tenant_id: int, form, files, actor: str) -> list:
    """Checks and saves the form. Returns a list of problems (empty = saved)."""
    errs = []
    reset = form.get("reset") == "1"
    welcome = " ".join((form.get("welcome") or "").split())
    title = (form.get("title") or "").strip()
    subtitle = (form.get("subtitle") or "").strip()
    for k, v in (("welcome", welcome), ("title", title), ("subtitle", subtitle)):
        if len(v) > LIMITS[k]:
            errs.append(f"The {'welcome message' if k == 'welcome' else 'window ' + k} can be up to {LIMITS[k]} characters.")
        if _LINK_RE.search(v):
            errs.append(f"Please take the web link out of the {'welcome message' if k == 'welcome' else 'window ' + k}.")
    colour = (form.get("colour") or DEFAULT_COLOUR).strip()
    if not _COLOUR_RE.match(colour):
        errs.append("Pick a colour from the colour box.")
    font = form.get("font") or "Inter"
    if font not in FONTS:
        font = "Inter"
    avatar_mode = form.get("avatar_mode") or "logo"
    if avatar_mode not in AVATARS:
        avatar_mode = "logo"
    size = "large" if form.get("size") == "large" else "standard"
    emojis_on = form.get("emojis_on") == "1"
    ai_emojis = form.get("ai_emojis") == "1"
    btn_position = "left" if form.get("btn_position") == "left" else "right"
    btn_vpos = form.get("btn_vpos") if form.get("btn_vpos") in VPOS else "bottom"
    agent_text = (form.get("agent_text") or "").strip() if form.get("agent_text_on") == "1" else ""
    visitor_text = (form.get("visitor_text") or "").strip() if form.get("visitor_text_on") == "1" else ""
    for v in (agent_text, visitor_text):
        if v and not _COLOUR_RE.match(v):
            errs.append("Pick the text colour from the colour box.")
    try:
        visitor_size = int(form.get("visitor_size") or 0) or None
    except ValueError:
        visitor_size = None
    if visitor_size is not None and visitor_size not in TEXT_SIZES:
        visitor_size = None
    btn_style = "words" if form.get("btn_style") == "words" else "round"
    btn_words = " ".join((form.get("btn_words") or "").split())
    show_on_phones = form.get("show_on_phones") == "1"
    quick_qs = [" ".join((q or "").split()) for q in form.getlist("quick_q")][:4]
    quick_qs = [q for q in quick_qs if q]
    if len(btn_words) > LIMITS["btn_words"]:
        errs.append("The words on the button can be up to 24 characters.")
    for i, q in enumerate(quick_qs, 1):
        if len(q) > LIMITS["question"]:
            errs.append(f"Quick question {i} can be up to 40 characters.")
    hours_on = form.get("hours_on") == "1"
    hours = []
    for i in range(7):
        f, t = form.get(f"from_{i}") or "09:00", form.get(f"to_{i}") or "17:30"
        on = form.get(f"open_{i}") == "1"
        if on and (not _TIME_RE.match(f) or not _TIME_RE.match(t) or f >= t):
            errs.append(f"On {DAYS[i]}, the closing time must be after the opening time.")
        hours.append({"on": on, "from": f if _TIME_RE.match(f) else "09:00", "to": t if _TIME_RE.match(t) else "17:30"})
    if hours_on and not any(h["on"] for h in hours):
        errs.append("Tick at least one day you're open, or untick Set business hours.")
    if any(_LINK_RE.search(v) for v in [btn_words] + quick_qs):
        errs.append("Please take web links out of the button words and quick questions.")
    if errs:
        return errs

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ensure(cur)
        cur.execute("SELECT logo_file, avatar_file, welcome, btn_words, quick_qs FROM chatbox_settings WHERE tenant_id=%s", (int(tenant_id),))
        r = cur.fetchone()
        if r and not isinstance(r, dict):
            r = dict(zip(["logo_file", "avatar_file", "welcome", "btn_words", "quick_qs"], r))
        r = r or {}
        old_logo, old_avatar, old_welcome = r.get("logo_file"), r.get("avatar_file"), r.get("welcome")
        old_texts = (old_welcome or None, r.get("btn_words") or None, list(r.get("quick_qs") or []))
        if reset:
            cur.execute("DELETE FROM chatbox_settings WHERE tenant_id=%s", (int(tenant_id),))
            cur.execute("DELETE FROM chatbox_welcome_tr WHERE tenant_id=%s", (int(tenant_id),))
            conn.commit()
            _drop_file(tenant_id, old_logo); _drop_file(tenant_id, old_avatar)
            return []
        logo, avatar = old_logo, old_avatar
        new_files = []
        try:
            if files.get("logo") and files["logo"].filename:
                logo = _save_image(tenant_id, files["logo"]); new_files.append(logo)
            elif form.get("logo_remove") == "1":
                logo = None
            if files.get("avatar") and files["avatar"].filename:
                avatar = _save_image(tenant_id, files["avatar"]); new_files.append(avatar)
            elif form.get("avatar_remove") == "1":
                avatar = None
        except ValueError as e:
            for f in new_files:
                _drop_file(tenant_id, f)
            return [str(e)]
        if avatar_mode == "upload" and not avatar:
            avatar_mode = "logo"
        cur.execute("""
            INSERT INTO chatbox_settings (tenant_id, welcome, colour, font, title, subtitle, logo_file, avatar_mode,
                                          avatar_file, size, emojis_on, ai_emojis, btn_position, btn_style, btn_words,
                                          show_on_phones, quick_qs, hours_on, hours, hours_tz, btn_vpos, agent_text,
                                          visitor_text, visitor_size, updated_at, updated_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
            ON CONFLICT (tenant_id) DO UPDATE SET
              welcome=EXCLUDED.welcome, colour=EXCLUDED.colour, font=EXCLUDED.font, title=EXCLUDED.title,
              subtitle=EXCLUDED.subtitle, logo_file=EXCLUDED.logo_file, avatar_mode=EXCLUDED.avatar_mode,
              avatar_file=EXCLUDED.avatar_file, size=EXCLUDED.size, emojis_on=EXCLUDED.emojis_on,
              ai_emojis=EXCLUDED.ai_emojis, btn_position=EXCLUDED.btn_position, btn_style=EXCLUDED.btn_style,
              btn_words=EXCLUDED.btn_words, show_on_phones=EXCLUDED.show_on_phones, quick_qs=EXCLUDED.quick_qs,
              hours_on=EXCLUDED.hours_on, hours=EXCLUDED.hours, hours_tz=EXCLUDED.hours_tz,
              btn_vpos=EXCLUDED.btn_vpos, agent_text=EXCLUDED.agent_text, visitor_text=EXCLUDED.visitor_text,
              visitor_size=EXCLUDED.visitor_size,
              updated_at=NOW(), updated_by=EXCLUDED.updated_by
        """, (int(tenant_id), welcome or None, colour.upper(), font, title or None, subtitle or None, logo,
              avatar_mode, avatar, size, emojis_on, ai_emojis, btn_position, btn_style, btn_words or None,
              show_on_phones, json.dumps(quick_qs) if quick_qs else None, hours_on, json.dumps(hours),
              tz_name(tenant_id), btn_vpos, agent_text.upper() or None, visitor_text.upper() or None, visitor_size,
              (actor or "")[:120]))
        if old_texts != (welcome or None, btn_words or None, quick_qs):
            cur.execute("DELETE FROM chatbox_welcome_tr WHERE tenant_id=%s", (int(tenant_id),))
        conn.commit()
    finally:
        conn.close()
    if logo != old_logo:
        _drop_file(tenant_id, old_logo)
    if avatar != old_avatar:
        _drop_file(tenant_id, old_avatar)
    return []
