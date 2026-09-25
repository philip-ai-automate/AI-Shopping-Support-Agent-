"""
ai_designer.py — the AI Post Designer engine (2026-09-25), shared by every
business ('tenant:<id>') and PhiXtra admin ('admin'). No Flask here; the
pages live in ai_design_routes.py.

How design costs are counted (approved "option 3"):
  * Each plan has its own monthly AI designs figure (plans.ai_designs_limit).
    It never touches the AI-message (customer chat) allowance.
  * Only "Create designs" and "Make new designs" use 1 AI design. Restyling,
    up to 5 word rewrites per post, quick edits and ready-made layouts are free.
  * Failed sets are never counted.
  * A business can connect its own AI key: then designs are unlimited and its
    AI provider bills it directly (source 'own_key').
  * When the monthly allowance is used up, bought "AI designs" top-up packs
    (tenant_design_balances) are used next.
  * PhiXtra admin designs on the platform key with no limit (source 'admin').

The AI writes words and (optionally) makes one picture per set; the real
product name, price and logo are drawn on top by ai_design_render.py, and
any number the AI invents is thrown away (see _clean_text).
"""
import base64
import io
import json
import os
import re
import uuid

import psycopg2.extras
import requests

from db import get_db_connection
import ai_design_render as R

ADMIN_OWNER = "admin"
FREE_REWRITES = 5
TEXT_MODEL = os.getenv("AI_DESIGN_TEXT_MODEL", "gpt-5.4-mini")
IMAGE_MODEL = os.getenv("AI_DESIGN_IMAGE_MODEL", "gpt-image-2.5-flare")
IMAGE_QUALITY = os.getenv("AI_DESIGN_IMAGE_QUALITY", "medium")

BASE = os.path.dirname(__file__)
LOGO_FOLDER = os.path.join(BASE, "static", "uploads", "brand_kits")
IMG_FOLDER = os.path.join(BASE, "static", "uploads", "ai_designs")
ALLOWED_IMG = {"jpg", "jpeg", "png", "webp"}

VOICES = {
    "warm": "Warm and friendly",
    "professional": "Professional",
    "fun_pidgin": "Fun, with some Nigerian Pidgin phrases",
    "luxury": "Calm and premium",
}
FEEDBACK = {
    "too_busy": "Too busy",
    "wrong_colours": "Wrong colours",
    "photo_small": "Photo too small",
    "words_long": "Words too long",
    "not_brand": "Doesn't look like my brand",
    "too_plain": "Too plain",
    "new_background": "Want a different background",
}
REWRITE_TONES = {
    "shorter": "Make it shorter and punchier.",
    "fun": "Make it more fun and playful.",
    "professional": "Make it more professional.",
    "urgency": "Add urgency (limited time / limited stock) without inventing numbers.",
    "pidgin": "Rewrite it in friendly Nigerian Pidgin English.",
}
SERVICE_RULES = {
    "instagram": "Instagram: 2-4 short lines, friendly, end with 3-6 relevant hashtags.",
    "facebook": "Facebook: 2-4 sentences, conversational, clear call to action.",
    "linkedin": "LinkedIn: professional, 2-3 sentences, at most 2 hashtags.",
    "twitter": "X: under 240 characters in total including hashtags.",
    "tiktok": "TikTok: 1-2 short lines plus 3-5 hashtags.",
    "threads": "Threads: 1-3 short casual sentences.",
}


class DesignError(Exception):
    """Plain-English reason a design action couldn't run."""


def tenant_owner(tenant_id) -> str:
    return f"tenant:{int(tenant_id)}"


def _db(dict_rows=True):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if dict_rows else conn.cursor()
    return conn, cur


def _crypto():
    from portal_routes import _encrypt_key, _decrypt_key
    return _encrypt_key, _decrypt_key


# ══════════════════════════════════════════════════════════════════════════
# Brand Kit
# ══════════════════════════════════════════════════════════════════════════

def get_brand_kit(owner_key: str, default_name: str = "") -> dict:
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM brand_kits WHERE owner_key=%s", (owner_key,))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    kit = dict(row) if row else {
        "owner_key": owner_key, "logo_filename": None, "color_main": "#1F2A44",
        "color_accent": "#F2B134", "color_bg": "#F7F4EE", "display_name": default_name,
        "contact_line": "", "style": "bold", "voice": "warm", "saved": False,
    }
    kit.setdefault("saved", True)
    if not kit.get("display_name"):
        kit["display_name"] = default_name
    return kit


def logo_path(kit: dict):
    fn = kit.get("logo_filename")
    return os.path.join(LOGO_FOLDER, fn) if fn else None


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def save_brand_kit(owner_key: str, form: dict, logo_file=None, actor: str = "") -> None:
    kit = get_brand_kit(owner_key)
    colours = {}
    for k in ("color_main", "color_accent", "color_bg"):
        v = (form.get(k) or "").strip()
        colours[k] = v if _HEX.match(v) else kit[k]
    style = form.get("style") if form.get("style") in R.STYLES else kit["style"]
    voice = form.get("voice") if form.get("voice") in VOICES else kit["voice"]
    logo = kit.get("logo_filename")
    old_logo = None
    if logo_file and getattr(logo_file, "filename", ""):
        ext = logo_file.filename.rsplit(".", 1)[-1].lower() if "." in logo_file.filename else ""
        if ext not in ("png", "jpg", "jpeg", "webp"):
            raise DesignError("The logo must be a PNG, JPG or WEBP image.")
        data = logo_file.read()
        if len(data) > 5 * 1024 * 1024:
            raise DesignError("The logo is over 5 MB. Use a smaller file.")
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:
            raise DesignError("That logo file couldn't be opened as an image.")
        os.makedirs(LOGO_FOLDER, exist_ok=True)
        old_logo, logo = logo, f"{uuid.uuid4().hex}.png"
        img.convert("RGBA").save(os.path.join(LOGO_FOLDER, logo), "PNG")
    elif form.get("remove_logo") == "1":
        old_logo, logo = logo, None
    conn, cur = _db()
    try:
        cur.execute("""
            INSERT INTO brand_kits (owner_key, logo_filename, color_main, color_accent, color_bg,
                                    display_name, contact_line, style, voice, updated_by, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (owner_key) DO UPDATE SET
                logo_filename=EXCLUDED.logo_filename, color_main=EXCLUDED.color_main,
                color_accent=EXCLUDED.color_accent, color_bg=EXCLUDED.color_bg,
                display_name=EXCLUDED.display_name, contact_line=EXCLUDED.contact_line,
                style=EXCLUDED.style, voice=EXCLUDED.voice, updated_by=EXCLUDED.updated_by, updated_at=NOW()
        """, (owner_key, logo, colours["color_main"], colours["color_accent"], colours["color_bg"],
              (form.get("display_name") or "").strip()[:120] or None,
              (form.get("contact_line") or "").strip()[:160] or None, style, voice, actor))
        conn.commit()
    finally:
        cur.close(); conn.close()
    if old_logo and old_logo != logo:
        try:
            os.remove(os.path.join(LOGO_FOLDER, old_logo))
        except OSError:
            pass


def colours_from_logo(owner_key: str):
    """The 3 most common non-white/non-black colours in the logo."""
    kit = get_brand_kit(owner_key)
    p = logo_path(kit)
    if not p or not os.path.exists(p):
        raise DesignError("Upload a logo first.")
    from PIL import Image
    img = Image.open(p).convert("RGBA").resize((80, 80))
    counts = {}
    for r, g, b, a in img.getdata():
        if a < 128 or (r > 235 and g > 235 and b > 235) or (r < 20 and g < 20 and b < 20):
            continue
        key = (r // 24 * 24, g // 24 * 24, b // 24 * 24)
        counts[key] = counts.get(key, 0) + 1
    top = [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:2]
    if not top:
        raise DesignError("Couldn't find colours in the logo. Pick them by hand.")
    hexes = ["#%02X%02X%02X" % c for c in top]
    main = hexes[0]
    accent = hexes[1] if len(hexes) > 1 else kit["color_accent"]
    return main, accent


# ══════════════════════════════════════════════════════════════════════════
# Own AI key
# ══════════════════════════════════════════════════════════════════════════

def get_ai_key_row(owner_key: str):
    conn, cur = _db()
    try:
        cur.execute("""SELECT owner_key, key_last4, status, last_error, last_checked_at, connected_by, connected_at
                       FROM ai_keys WHERE owner_key=%s""", (owner_key,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def _own_key(owner_key: str) -> str:
    conn, cur = _db(False)
    try:
        cur.execute("SELECT api_key_enc FROM ai_keys WHERE owner_key=%s", (owner_key,))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not row:
        return ""
    return _crypto()[1](row[0])


def check_and_save_ai_key(owner_key: str, tenant_id, api_key: str, actor: str) -> None:
    from openai import OpenAI, AuthenticationError, APIConnectionError
    try:
        OpenAI(api_key=api_key, timeout=20).models.list()
    except AuthenticationError:
        raise DesignError("OpenAI didn't accept this key. Check you copied the whole key from "
                          "platform.openai.com/api-keys, then try again.")
    except APIConnectionError:
        raise DesignError("Couldn't reach OpenAI to check the key. Try again in a minute.")
    except Exception as e:
        raise DesignError(f"OpenAI said: {e}")
    conn, cur = _db()
    try:
        cur.execute("""
            INSERT INTO ai_keys (owner_key, tenant_id, api_key_enc, key_last4, status, last_error,
                                 last_checked_at, connected_by, connected_at)
            VALUES (%s,%s,%s,%s,'ok',NULL,NOW(),%s,NOW())
            ON CONFLICT (owner_key) DO UPDATE SET api_key_enc=EXCLUDED.api_key_enc,
                key_last4=EXCLUDED.key_last4, status='ok', last_error=NULL, last_checked_at=NOW(),
                connected_by=EXCLUDED.connected_by
        """, (owner_key, tenant_id, _crypto()[0](api_key), api_key[-4:], actor))
        conn.commit()
    finally:
        cur.close(); conn.close()


def remove_ai_key(owner_key: str) -> None:
    conn, cur = _db()
    try:
        cur.execute("DELETE FROM ai_keys WHERE owner_key=%s", (owner_key,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _mark_key(owner_key: str, ok: bool, err: str = None):
    conn, cur = _db()
    try:
        cur.execute("UPDATE ai_keys SET status=%s, last_error=%s, last_checked_at=NOW() WHERE owner_key=%s",
                    ("ok" if ok else "needs_attention", err, owner_key))
        conn.commit()
    finally:
        cur.close(); conn.close()


# ══════════════════════════════════════════════════════════════════════════
# Allowance
# ══════════════════════════════════════════════════════════════════════════

def plan_design_settings(tenant_id: int) -> dict:
    conn, cur = _db()
    try:
        cur.execute("""SELECT COALESCE(p.name, 'Free') AS plan_name,
                              COALESCE(p.ai_designs_limit, 0) AS limit,
                              COALESCE(p.allow_own_ai_key, TRUE) AS allow_own_key
                       FROM tenants t LEFT JOIN plans p ON p.id = t.plan_id WHERE t.id=%s""", (tenant_id,))
        return dict(cur.fetchone() or {"plan_name": "Free", "limit": 0, "allow_own_key": True})
    finally:
        cur.close(); conn.close()


def allowance(owner_key: str, tenant_id) -> dict:
    """Where the next AI design would come from, and the numbers for the meter."""
    if owner_key == ADMIN_OWNER:
        return {"source": "admin", "unlimited": True, "limit": 0, "used": 0, "left": 0,
                "extra": 0, "own_key": False, "allow_own_key": False, "plan_name": "PhiXtra admin"}
    plan = plan_design_settings(int(tenant_id))
    conn, cur = _db()
    try:
        cur.execute("""SELECT COUNT(*) AS n FROM ai_design_usage
                       WHERE owner_key=%s AND counted AND source='allowance'
                         AND created_at >= date_trunc('month', NOW())""", (owner_key,))
        used = int(cur.fetchone()["n"])
        cur.execute("SELECT design_credits FROM tenant_design_balances WHERE tenant_id=%s", (int(tenant_id),))
        row = cur.fetchone()
        extra = int(row["design_credits"]) if row else 0
    finally:
        cur.close(); conn.close()
    key = get_ai_key_row(owner_key) if plan["allow_own_key"] else None
    own = bool(key and key["status"] == "ok")
    left = max(0, int(plan["limit"]) - used)
    if own:
        source = "own_key"
    elif left > 0:
        source = "allowance"
    elif extra > 0:
        source = "topup"
    else:
        source = None
    return {"source": source, "unlimited": own, "limit": int(plan["limit"]), "used": used, "left": left,
            "extra": extra, "own_key": own, "own_key_broken": bool(key and key["status"] != "ok"),
            "allow_own_key": plan["allow_own_key"], "plan_name": plan["plan_name"]}


def _record(owner_key, tenant_id, session_id, action, source, counted, failed, note, actor):
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO ai_design_usage (owner_key, tenant_id, session_id, action, source,
                                                    counted, failed, note, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (owner_key, tenant_id, session_id, action, source or "none", counted, failed,
                     (note or "")[:500] or None, actor))
        if counted and source == "topup" and tenant_id:
            cur.execute("""UPDATE tenant_design_balances SET design_credits = GREATEST(design_credits - 1, 0),
                                  updated_at=NOW() WHERE tenant_id=%s""", (tenant_id,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def usage_log(owner_key: str, limit: int = 100) -> list:
    conn, cur = _db()
    try:
        cur.execute("""SELECT u.*, s.product, s.idea FROM ai_design_usage u
                       LEFT JOIN ai_design_sessions s ON s.id = u.session_id
                       WHERE u.owner_key=%s ORDER BY u.created_at DESC LIMIT %s""", (owner_key, limit))
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    for r in rows:
        r["subject"] = ((r.get("product") or {}).get("name")) or (r.get("idea") or "")[:60] or "—"
    return rows


def add_design_credits(tenant_id: int, n: int) -> None:
    """Called when an "AI designs" top-up pack is paid for."""
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO tenant_design_balances (tenant_id, design_credits) VALUES (%s, %s)
                       ON CONFLICT (tenant_id) DO UPDATE SET
                           design_credits = tenant_design_balances.design_credits + EXCLUDED.design_credits,
                           updated_at=NOW()""", (tenant_id, int(n)))
        conn.commit()
    finally:
        cur.close(); conn.close()


# ══════════════════════════════════════════════════════════════════════════
# Taste (👍 / 👎)
# ══════════════════════════════════════════════════════════════════════════

def vote(owner_key: str, session: dict, idx: int, value: int) -> None:
    v = session["variants"][idx]
    conn, cur = _db()
    try:
        if value == 0:
            cur.execute("DELETE FROM ai_design_taste WHERE session_id=%s AND variant=%s", (session["id"], idx))
        else:
            cur.execute("""INSERT INTO ai_design_taste (owner_key, session_id, variant, style, layout, vote)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (session_id, variant) DO UPDATE SET vote=EXCLUDED.vote,
                               style=EXCLUDED.style, layout=EXCLUDED.layout, created_at=NOW()""",
                        (owner_key, session["id"], idx, v.get("style"), v.get("layout"), value))
        conn.commit()
    finally:
        cur.close(); conn.close()
    session["variants"][idx]["thumb"] = {1: "up", -1: "down"}.get(value)
    _save_session(session)


def taste(owner_key: str) -> dict:
    conn, cur = _db()
    try:
        cur.execute("""SELECT style, layout, vote FROM ai_design_taste WHERE owner_key=%s
                       ORDER BY created_at DESC LIMIT 300""", (owner_key,))
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    styles = {s: {"up": 0, "down": 0} for s in R.STYLES}
    big = 0
    for r in rows:
        if r["style"] in styles:
            styles[r["style"]]["up" if r["vote"] > 0 else "down"] += 1
        if r["vote"] > 0 and r["layout"] in ("big", "top"):
            big += 1
    liked = [s for s, c in styles.items() if c["up"] > c["down"] and c["up"] > 0]
    skipped = [s for s, c in styles.items() if c["down"] > c["up"] and c["down"] >= 2]
    return {"count": len(rows), "styles": styles, "liked": liked, "skipped": skipped,
            "prefers_big_photo": big >= 3 and big >= len([r for r in rows if r["vote"] > 0]) / 2}


def reset_taste(owner_key: str) -> None:
    conn, cur = _db()
    try:
        cur.execute("DELETE FROM ai_design_taste WHERE owner_key=%s", (owner_key,))
        conn.commit()
    finally:
        cur.close(); conn.close()


# ══════════════════════════════════════════════════════════════════════════
# Sessions
# ══════════════════════════════════════════════════════════════════════════

def get_session(owner_key: str, sid: int):
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM ai_design_sessions WHERE id=%s AND owner_key=%s", (sid, owner_key))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    return dict(row) if row else None


def _save_session(s: dict) -> None:
    conn, cur = _db()
    try:
        cur.execute("""UPDATE ai_design_sessions SET variants=%s, selected=%s, rewrites_used=%s, sets_made=%s,
                              feedback=%s, ai_image=%s, picture_mode=%s, post_id=%s, updated_at=NOW()
                       WHERE id=%s""",
                    (json.dumps(s["variants"]), int(s.get("selected") or 0), int(s.get("rewrites_used") or 0),
                     int(s.get("sets_made") or 0), json.dumps(s.get("feedback")) if s.get("feedback") else None,
                     s.get("ai_image"), s.get("picture_mode"), s.get("post_id"), s["id"]))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _store_subject_image(src: str = None, upload=None) -> str:
    """Copies the product photo (local path, web address or an upload) into
    the designer's own folder so later edits never depend on it."""
    os.makedirs(IMG_FOLDER, exist_ok=True)
    data = None
    if upload is not None and getattr(upload, "filename", ""):
        ext = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        if ext not in ALLOWED_IMG:
            raise DesignError("Images must be JPG, PNG or WEBP.")
        data = upload.read()
    elif src:
        if src.startswith("/static/"):
            p = os.path.join(BASE, src.lstrip("/"))
            if os.path.exists(p):
                data = open(p, "rb").read()
        elif src.startswith("http"):
            try:
                r = requests.get(src, timeout=20, headers={"User-Agent": "Mozilla/5.0 PhiXtra-Designer"})
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                    data = r.content
            except requests.RequestException:
                data = None
    if not data:
        return None
    if len(data) > 15 * 1024 * 1024:
        raise DesignError("That image is over 15 MB. Use a smaller one.")
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    img = img.convert("RGB")
    img.thumbnail((1600, 1600))
    name = f"subj_{uuid.uuid4().hex}.jpg"
    img.save(os.path.join(IMG_FOLDER, name), "JPEG", quality=90)
    return name


def image_path(name: str):
    return os.path.join(IMG_FOLDER, name) if name else None


def product_choices(tenant_id: int, q: str = "", limit: int = 24) -> list:
    """My Products (WhatsApp merchants) + the synced WooCommerce catalogue."""
    q = (q or "").strip()
    like = f"%{q}%"
    out = []
    conn, cur = _db()
    try:
        cur.execute("""SELECT 'p' || id AS ref, name, price, NULL AS currency, image_url, stock_quantity AS stock,
                              description
                       FROM products WHERE tenant_id=%s AND is_active AND (%s = '' OR name ILIKE %s)
                       ORDER BY updated_at DESC NULLS LAST LIMIT %s""", (tenant_id, q, like, limit))
        out += cur.fetchall() or []
        cur.execute("""SELECT 'd' || id AS ref, title AS name, price_min AS price, currency, image_url,
                              CASE WHEN in_stock THEN NULL ELSE 0 END AS stock, left(content, 600) AS description
                       FROM documents WHERE tenant_id=%s AND type='product' AND (%s = '' OR title ILIKE %s)
                       ORDER BY in_stock DESC NULLS LAST, updated_at DESC NULLS LAST LIMIT %s""",
                    (tenant_id, q, like, limit))
        out += cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    for p in out:
        p["price_label"] = format_price(p.get("price"), p.get("currency"))
    return out[:limit]


def get_product(tenant_id: int, ref: str):
    if not ref or ref[0] not in "pd" or not ref[1:] or len(ref) > 80:
        return None
    conn, cur = _db()
    try:
        # products.id is text; documents.id is a number — compare both as text.
        if ref[0] == "p":
            cur.execute("""SELECT name, price, NULL AS currency, image_url, description FROM products
                           WHERE id::text=%s AND tenant_id=%s""", (ref[1:], tenant_id))
        else:
            cur.execute("""SELECT title AS name, price_min AS price, currency, image_url,
                                  left(content, 600) AS description
                           FROM documents WHERE id::text=%s AND tenant_id=%s AND type='product'""", (ref[1:], tenant_id))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not row:
        return None
    row = dict(row)
    row["price_label"] = format_price(row.get("price"), row.get("currency"))
    return row


def format_price(price, currency=None) -> str:
    if price in (None, ""):
        return ""
    try:
        v = float(price)
    except (TypeError, ValueError):
        return ""
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get((currency or "NGN").upper(), "₦")
    return f"{sym}{v:,.0f}" if v == int(v) else f"{sym}{v:,.2f}"


# ══════════════════════════════════════════════════════════════════════════
# AI calls
# ══════════════════════════════════════════════════════════════════════════

def _client(owner_key: str, source: str):
    from openai import OpenAI
    key = _own_key(owner_key) if source == "own_key" else os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise DesignError("AI isn't set up on this platform yet. Contact support.")
    return OpenAI(api_key=key, timeout=120)


def _ai_error(owner_key, source, e) -> DesignError:
    from openai import AuthenticationError, RateLimitError, APIConnectionError, BadRequestError
    if isinstance(e, AuthenticationError) and source == "own_key":
        _mark_key(owner_key, False, str(e)[:300])
        return DesignError("Your AI key stopped working. Put in a new one on Integration › Your own AI key. "
                           "This wasn't counted.")
    if isinstance(e, RateLimitError):
        if source == "own_key":
            return DesignError("Your AI account is out of credit or busy. Check your OpenAI billing, then try again. "
                               "This wasn't counted.")
        return DesignError("The AI is busy right now. Wait a minute and try again. This wasn't counted.")
    if isinstance(e, APIConnectionError):
        return DesignError("Couldn't reach the AI. Try again in a minute. This wasn't counted.")
    if isinstance(e, BadRequestError) and "safety" in str(e).lower():
        return DesignError("The AI declined this picture. Try different words. This wasn't counted.")
    return DesignError("The AI couldn't finish the designs this time. This wasn't counted. Try again.")


_NUM = re.compile(r"\d[\d,.]*")


def _clean_text(text: str, allowed_source: str) -> str:
    """Drops any sentence-level text containing a number the business never
    gave us (prices/percentages must come from them, not the AI)."""
    allowed = set(n.replace(",", "") for n in _NUM.findall(allowed_source or ""))
    for n in _NUM.findall(text or ""):
        if n.replace(",", "").rstrip(".") not in allowed:
            return ""
    return text


def _words_prompt(session: dict, kit: dict, channels: list, feedback: list, taste_info: dict, extra: str = "") -> str:
    p = session.get("product") or {}
    services = sorted({c["service"] for c in channels if c.get("service")}) or ["facebook", "instagram"]
    rules = "\n".join("- " + SERVICE_RULES.get(s, f"{s}: short and friendly.") for s in services)
    lines = [
        f"Business: {kit.get('display_name') or 'the business'}.",
        f"Voice: {VOICES.get(kit.get('voice'), 'Warm and friendly')}.",
    ]
    if p:
        lines.append(f"Product: {p.get('name')}.")
        if p.get("price_label"):
            lines.append(f"Exact price (copy exactly, never change): {p['price_label']}.")
        if p.get("description"):
            lines.append(f"Product details: {re.sub(r'<[^>]+>', ' ', p['description'])[:500]}")
    if session.get("idea"):
        lines.append(f"What the post is about: {session['idea']}")
    if session.get("offer"):
        lines.append(f"Offer / highlight (copy any numbers exactly): {session['offer']}")
    if feedback:
        lines.append("The business disliked the last designs because: " + ", ".join(FEEDBACK.get(f, f) for f in feedback) + ".")
    if session.get("feedback", {}) and session["feedback"].get("note"):
        lines.append(f"They also said: {session['feedback']['note']}")
    if "words_long" in (feedback or []):
        lines.append("Keep every headline to 4 words or fewer and captions short.")
    if extra:
        lines.append(extra)
    return (
        "You write social media posts for a small business. Reply with JSON only.\n\n"
        + "\n".join(lines) + "\n\n"
        "Return {\"variants\": [4 objects], \"scene\": string}. Each variant object has:\n"
        "- \"headline\": 2-7 words for the image, catchy, different angle per variant.\n"
        "- \"price_label\": short price text for the image using ONLY the exact price/offer numbers given "
        "(e.g. \"Now ₦6,800\"), or \"\" if no price was given.\n"
        f"- \"captions\": an object with a caption for each of these networks: {', '.join(services)}.\n"
        "Caption rules:\n" + rules + "\n"
        "Never invent prices, discounts, dates, stock counts or phone numbers. Only use numbers that appear above.\n"
        "\"scene\": one sentence describing a simple photo setting for the product (no people's faces, no text)."
    )


def _ask_words(owner_key, source, prompt) -> dict:
    client = _client(owner_key, source)
    resp = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content or "{}")


def _make_picture(owner_key, source, session, kit, scene_hint: str) -> str:
    """One AI picture per set. With a product photo: the same product placed
    in a new scene. Without: a picture for the idea. Never any text in it."""
    client = _client(owner_key, source)
    no_text = "Absolutely no text, letters, numbers, logos or watermarks anywhere in the image."
    subj = image_path(session.get("subject_image"))
    if subj and os.path.exists(subj):
        prompt = (f"A bright, professional lifestyle product photograph showing exactly this product, keeping its "
                  f"shape, colours and details unchanged. Setting: {scene_hint or 'a clean, softly lit surface'}. "
                  f"Soft natural light, uncluttered, the product large and in sharp focus. {no_text}")
        with open(subj, "rb") as fh:
            res = client.images.edit(model=IMAGE_MODEL, image=fh, prompt=prompt, size="1024x1024", quality=IMAGE_QUALITY)
    else:
        idea = session.get("idea") or scene_hint or "a small business promotion"
        prompt = (f"A bright, eye-catching photograph for a social media post about: {idea}. "
                  f"Colour mood inspired by {kit.get('color_main')} and {kit.get('color_accent')}. "
                  f"Simple composition with clear space for a headline. {no_text}")
        res = client.images.generate(model=IMAGE_MODEL, prompt=prompt, size="1024x1024", quality=IMAGE_QUALITY)
    b64 = res.data[0].b64_json
    if not b64:
        raise DesignError("The AI returned no picture.")
    os.makedirs(IMG_FOLDER, exist_ok=True)
    name = f"ai_{uuid.uuid4().hex}.png"
    with open(os.path.join(IMG_FOLDER, name), "wb") as fh:
        fh.write(base64.b64decode(b64))
    return name


# ══════════════════════════════════════════════════════════════════════════
# Building a set of 4
# ══════════════════════════════════════════════════════════════════════════

def _plan_looks(kit: dict, feedback: list, taste_info: dict, sets_made: int) -> list:
    """(style, layout, palette) for the 4 designs, steered by the Brand Kit,
    the 👍/👎 history and any "what didn't you like" answers."""
    fb = set(feedback or [])
    order = list(R.STYLES)
    first = kit.get("style") if kit.get("style") in R.STYLES else "bold"
    score = {s: taste_info["styles"][s]["up"] - taste_info["styles"][s]["down"] for s in R.STYLES}
    order.sort(key=lambda s: (s != first or s in taste_info["skipped"], -score[s]))
    if "too_busy" in fb:
        order.sort(key=lambda s: s not in ("minimal", "elegant"))
    if "too_plain" in fb:
        order.sort(key=lambda s: s not in ("bold", "playful"))
    if "not_brand" in fb:
        order.sort(key=lambda s: s != first)
    layouts = ["right", "centre", "top", "big"]
    if "photo_small" in fb or taste_info.get("prefers_big_photo"):
        layouts = ["big", "top", "big", "top"]
    base_pal = 0 if "not_brand" in fb else (sets_made if "wrong_colours" in fb else 0)
    looks = []
    for i in range(4):
        looks.append((order[i % 4], layouts[(i + sets_made) % 4] if "photo_small" not in fb else layouts[i],
                      (base_pal + i) % 4))
    return looks


def _variants_from_words(words: dict, session, kit, looks, use_ai_for) -> list:
    p = session.get("product") or {}
    allowed = " ".join([p.get("price_label") or "", session.get("offer") or "", p.get("name") or "",
                        session.get("idea") or ""])
    fallback_head = (p.get("name") or session.get("idea") or kit.get("display_name") or "").strip()[:60]
    raw = (words or {}).get("variants") or []
    out = []
    for i, (style, layout, pal) in enumerate(looks):
        w = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
        head = _clean_text((w.get("headline") or "").strip()[:80], allowed) or fallback_head
        price = (w.get("price_label") or "").strip()[:40]
        if p.get("price_label"):
            if p["price_label"] not in price or not _clean_text(price, allowed):
                price = p["price_label"]
        else:
            price = _clean_text(price, allowed)
        caps = {}
        for svc, text in (w.get("captions") or {}).items():
            if isinstance(text, str) and _clean_text(text, allowed + " " + (kit.get("contact_line") or "")):
                caps[svc] = text.strip()[:2200]
        out.append({"style": style, "layout": layout, "palette": pal, "headline": head, "price_label": price,
                    "captions": caps, "use_ai_image": i in use_ai_for, "show_logo": True,
                    "show_contact": bool(kit.get("contact_line")), "thumb": None})
    return out


def _template_captions(session, kit, channels) -> dict:
    p = session.get("product") or {}
    bits = [p.get("name") or session.get("idea") or ""]
    if session.get("offer"):
        bits.append(session["offer"])
    if p.get("price_label"):
        bits.append(p["price_label"])
    if kit.get("contact_line"):
        bits.append(kit["contact_line"])
    text = " · ".join(b for b in bits if b)
    return {c["service"]: text for c in channels if c.get("service")} or {"facebook": text}


def create_session(owner_key, tenant_id, actor, *, source, product=None, idea="", offer="", channels=None,
                   picture_mode="product", upload=None, use_ai=True) -> dict:
    channels = channels or []
    subject = None
    if picture_mode == "upload":
        subject = _store_subject_image(upload=upload)
        if not subject:
            raise DesignError("Choose an image to upload.")
    elif product and product.get("image_url"):
        subject = _store_subject_image(src=product["image_url"])
    if picture_mode == "product" and not subject:
        picture_mode = "ai_scene" if use_ai else "none"
    if source == "idea" and not idea.strip():
        raise DesignError("Type what the post is about.")
    if source == "product" and not product:
        raise DesignError("Pick a product.")
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO ai_design_sessions (owner_key, tenant_id, source, product, idea, offer, channels,
                                                       picture_mode, subject_image, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (owner_key, tenant_id, source, json.dumps(product, default=str) if product else None,
                     idea.strip()[:600] or None, offer.strip()[:300] or None, json.dumps(channels),
                     picture_mode, subject, actor))
        s = dict(cur.fetchone())
        conn.commit()
    finally:
        cur.close(); conn.close()
    kit = get_brand_kit(owner_key)
    if not use_ai:
        looks = _plan_looks(kit, [], taste(owner_key), 0)
        s["variants"] = _variants_from_words({}, s, kit, looks, set())
        caps = _template_captions(s, kit, channels)
        for v in s["variants"]:
            v["captions"] = dict(caps)
        _save_session(s)
        _record(owner_key, tenant_id, s["id"], "ready_made", "free", False, False, None, actor)
        return s
    make_set(owner_key, tenant_id, s, actor, feedback=[], action="create")
    return s


def make_set(owner_key, tenant_id, s, actor, feedback=None, note="", action="new_set") -> None:
    """Asks the AI for 4 new designs. Uses 1 AI design only if it works."""
    allow = allowance(owner_key, tenant_id)
    source = allow["source"]
    if not source:
        raise DesignError("You've used this month's AI designs. Connect your own AI key or buy extra AI designs.")
    kit = get_brand_kit(owner_key)
    t = taste(owner_key)
    s["feedback"] = {"reasons": feedback or [], "note": (note or "")[:200]} if (feedback or note) else None
    looks = _plan_looks(kit, feedback, t, int(s.get("sets_made") or 0))
    try:
        words = _ask_words(owner_key, source, _words_prompt(s, kit, s.get("channels") or [], feedback, t))
        need_picture = s["picture_mode"] in ("ai_scene", "none") or not s.get("subject_image") \
            or "new_background" in (feedback or [])
        if need_picture:
            s["ai_image"] = _make_picture(owner_key, source, s, kit, (words or {}).get("scene") or note)
            if s["picture_mode"] in ("product", "none"):
                s["picture_mode"] = "ai_scene"
    except DesignError as e:
        _record(owner_key, tenant_id, s["id"], action, source, False, True, str(e), actor)
        raise
    except Exception as e:
        err = _ai_error(owner_key, source, e)
        _record(owner_key, tenant_id, s["id"], action, source, False, True, f"{type(e).__name__}: {e}", actor)
        raise err
    if s.get("ai_image") and s.get("subject_image"):
        use_ai_for = {0, 2}           # mix: 2 with the AI scene, 2 with the real photo
    elif s.get("ai_image"):
        use_ai_for = {0, 1, 2, 3}
    else:
        use_ai_for = set()
    s["variants"] = _variants_from_words(words, s, kit, looks, use_ai_for)
    s["selected"] = 0
    s["sets_made"] = int(s.get("sets_made") or 0) + 1
    _save_session(s)
    _record(owner_key, tenant_id, s["id"], action, source, source in ("allowance", "topup"), False, None, actor)
    if source == "own_key":
        _mark_key(owner_key, True)


def rewrite(owner_key, tenant_id, s, actor, tone: str) -> None:
    """Free (up to 5 per post): new headline + captions for the chosen design."""
    if int(s.get("rewrites_used") or 0) >= FREE_REWRITES:
        raise DesignError("You've used the 5 free rewrites for this post. Edit the words yourself, or make new designs.")
    idx = int(s.get("selected") or 0)
    v = s["variants"][idx]
    allow = allowance(owner_key, tenant_id)
    source = "own_key" if allow.get("own_key") else ("admin" if owner_key == ADMIN_OWNER else "platform")
    kit = get_brand_kit(owner_key)
    instruction = REWRITE_TONES.get(tone) or "Improve it."
    extra = (f"Current headline: {v.get('headline')}\nCurrent captions: {json.dumps(v.get('captions') or {})}\n"
             f"Rewrite request: {instruction} Return 4 variants anyway; only the first is used.")
    try:
        words = _ask_words(owner_key, "own_key" if source == "own_key" else "platform",
                           _words_prompt(s, kit, s.get("channels") or [], [], taste(owner_key), extra))
    except Exception as e:
        _record(owner_key, tenant_id, s["id"], "rewrite", source, False, True, f"{type(e).__name__}: {e}", actor)
        raise _ai_error(owner_key, source, e) if not isinstance(e, DesignError) else e
    fresh = _variants_from_words(words, s, kit, [(v["style"], v["layout"], v["palette"])], set())[0]
    v["headline"] = fresh["headline"] or v["headline"]
    if fresh["captions"]:
        v["captions"] = fresh["captions"]
    s["rewrites_used"] = int(s.get("rewrites_used") or 0) + 1
    _save_session(s)
    _record(owner_key, tenant_id, s["id"], "rewrite", source, False, False, tone, actor)


def restyle(s, style=None, palette=None, layout=None) -> None:
    v = s["variants"][int(s.get("selected") or 0)]
    if style in R.STYLES:
        v["style"] = style
    if palette is not None and str(palette).isdigit():
        v["palette"] = int(palette) % 4
    if layout in R.LAYOUTS:
        v["layout"] = layout
    _save_session(s)


def picture_for(s, v):
    if v.get("use_ai_image") and s.get("ai_image"):
        return image_path(s["ai_image"])
    if s.get("subject_image"):
        return image_path(s["subject_image"])
    return image_path(s.get("ai_image"))


def render_variant(owner_key, s, idx, size="square") -> bytes:
    kit = get_brand_kit(owner_key)
    v = s["variants"][idx]
    return R.render(v, kit, picture_for(s, v), logo_path(kit), size)


def save_final_images(owner_key, s, dest_folder) -> tuple:
    """Renders the chosen design at both sizes into `dest_folder`."""
    idx = int(s.get("selected") or 0)
    os.makedirs(dest_folder, exist_ok=True)
    names = []
    for size in ("square", "wide"):
        name = f"{uuid.uuid4().hex}.png"
        with open(os.path.join(dest_folder, name), "wb") as fh:
            fh.write(render_variant(owner_key, s, idx, size))
        names.append(name)
    return names[0], names[1]
