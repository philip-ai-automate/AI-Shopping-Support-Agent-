"""Customise chat box (Phase 1, 9 Nov 2026).

A business's own look for the website chat box: welcome message, colour,
font, window title/subtitle, logo, AI Agent avatar, window size, emojis and
stickers. Saved in chatbox_settings (one row per business); the AI service
(ai-backend /widget/config) reads the same row. Nothing set = today's look.

Images (logo, avatar) are public — any website visitor's browser loads them —
and live on the big disk under CHATBOX_MEDIA_DIR with random names.
"""
import io
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
LIMITS = {"welcome": 200, "title": 40, "subtitle": 60}

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
    if errs:
        return errs

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ensure(cur)
        cur.execute("SELECT logo_file, avatar_file, welcome FROM chatbox_settings WHERE tenant_id=%s", (int(tenant_id),))
        r = cur.fetchone()
        old_logo, old_avatar, old_welcome = (r[0], r[1], r[2]) if r and not isinstance(r, dict) else \
            ((r or {}).get("logo_file"), (r or {}).get("avatar_file"), (r or {}).get("welcome"))
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
                                          avatar_file, size, emojis_on, ai_emojis, updated_at, updated_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
            ON CONFLICT (tenant_id) DO UPDATE SET
              welcome=EXCLUDED.welcome, colour=EXCLUDED.colour, font=EXCLUDED.font, title=EXCLUDED.title,
              subtitle=EXCLUDED.subtitle, logo_file=EXCLUDED.logo_file, avatar_mode=EXCLUDED.avatar_mode,
              avatar_file=EXCLUDED.avatar_file, size=EXCLUDED.size, emojis_on=EXCLUDED.emojis_on,
              ai_emojis=EXCLUDED.ai_emojis, updated_at=NOW(), updated_by=EXCLUDED.updated_by
        """, (int(tenant_id), welcome or None, colour.upper(), font, title or None, subtitle or None, logo,
              avatar_mode, avatar, size, emojis_on, ai_emojis, (actor or "")[:120]))
        if (welcome or None) != (old_welcome or None):
            cur.execute("DELETE FROM chatbox_welcome_tr WHERE tenant_id=%s", (int(tenant_id),))
        conn.commit()
    finally:
        conn.close()
    if logo != old_logo:
        _drop_file(tenant_id, old_logo)
    if avatar != old_avatar:
        _drop_file(tenant_id, old_avatar)
    return []
