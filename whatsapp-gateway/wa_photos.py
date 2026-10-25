"""Product photos on WhatsApp (2026-10-25, approved mock-up
https://claude.ai/artifact/117JTXRzEwCjZSZk5yWnNn).

When the AI shows one product, its main photo goes first and the customer is
told how many more there are ("Reply MORE"). This module remembers which
product's photos the customer is looking at (wa_photo_context) and answers,
without calling the AI (no tokens):

  MORE                        → the next 4 photos
  "show me the back" / "red"  → the photos the business labelled that way
  a view nobody labelled      → all the photos (never guessed), or, if the
                                product has no other photo, the team is asked

Labels come from the business (portal › My Products › Photos); a photo the
business hasn't confirmed is never presented as "the back".
"""
import json
import re

import psycopg2.extras

from wa_db import get_db_connection
from meta_sender import send_text, send_product_image

BATCH = 4
CONTEXT_HOURS = 24

_MORE_RE = re.compile(r"^\s*(?:more|more\s+(?:photos?|pictures?|pics?|images?)|see\s+more|show\s+more|"
                      r"other\s+(?:photos?|pictures?|pics?)|more\s+please|send\s+more)\s*[.!?]*\s*$", re.I)
_PHOTO_WORDS = re.compile(r"\b(show|see|send|photo|photos|picture|pictures|pic|pics|image|images|look|view)\b", re.I)
_VIEWS = [("back", r"\b(back|rear|behind)\b"), ("side", r"\b(side|profile)\b"), ("front", r"\bfront\b"),
          ("closeup", r"\b(close[\s-]?up|closer|detail|details|zoom|fabric|texture)\b"),
          ("worn", r"\b(wear|wearing|worn|on\s+(?:a\s+)?(?:model|person|body)|in\s+use)\b")]
_SHOWING = {"back": "the back of {n}", "side": "the side of {n}", "front": "the front of {n}",
            "closeup": "a close-up of {n}", "worn": "{n} being worn"}


def _showing(view, name):
    return _SHOWING[view].format(n=name)


# ── what the customer is looking at ─────────────────────────────────────────

def _q(sql, args=(), fetch=False):
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, args)
        out = cur.fetchone() if fetch else None
        conn.commit()
        return out
    except Exception as e:
        print("⚠️ wa_photos db:", e)
        return None
    finally:
        conn.close()


def remember(session_id: str, name: str, images: list, sent_urls: list):
    if not session_id or not images:
        return
    _q("""INSERT INTO wa_photo_context (session_id, product_name, images, sent, updated_at)
          VALUES (%s, %s, %s::jsonb, %s::jsonb, NOW())
          ON CONFLICT (session_id) DO UPDATE SET product_name=EXCLUDED.product_name, images=EXCLUDED.images,
              sent=EXCLUDED.sent, updated_at=NOW()""",
       (session_id, (name or "")[:300], json.dumps(images), json.dumps(sent_urls)))


def _context(session_id: str):
    return _q(f"""SELECT product_name, images, sent FROM wa_photo_context
                  WHERE session_id=%s AND updated_at > NOW() - INTERVAL '{CONTEXT_HOURS} hours'""", (session_id,), fetch=True)


def _mark_sent(session_id: str, sent: list):
    _q("UPDATE wa_photo_context SET sent=%s::jsonb, updated_at=NOW() WHERE session_id=%s", (json.dumps(sent), session_id))


def more_note(images: list, name: str) -> str:
    """The line added under a product: how many more photos there are."""
    n = max(0, len(images or []) - 1)
    if not n:
        return ""
    return f"📷 {n} more photo{'s' if n != 1 else ''} of this product. Reply *MORE* to see {'them' if n != 1 else 'it'}."


async def send_main(phone_number_id, access_token, to, session_id, product: dict) -> bool:
    """The main photo of one product (any category), and remember its photos."""
    images = product.get("images") or ([{"url": product["image_url"], "label": ""}] if product.get("image_url") else [])
    if not images:
        return False
    name = (product.get("name") or product.get("product_name") or "").split(" - ")[0].strip()
    ok = await send_product_image(phone_number_id, access_token, to, images[0]["url"], _caption(name, images[0]))
    remember(session_id, name, images, [images[0]["url"]] if ok else [])
    return ok


def _caption(name: str, img: dict) -> str:
    return f"{name} · {img['label']}" if img.get("label") else name


async def _send_batch(phone_number_id, access_token, to, name, imgs) -> list:
    sent = []
    for img in imgs[:BATCH]:
        if await send_product_image(phone_number_id, access_token, to, img["url"], _caption(name, img)):
            sent.append(img["url"])
    return sent


# ── the customer's message ──────────────────────────────────────────────────

async def handle(phone_number_id, access_token, to, session_id, tenant_id, text) -> bool:
    """True if this message was a photo request and has been answered."""
    t = (text or "").strip()
    if not t or len(t) > 160:
        return False
    is_more = bool(_MORE_RE.match(t))
    view = next((v for v, rx in _VIEWS if re.search(rx, t, re.I)), None)
    ctx = _context(session_id)
    if not ctx:
        return False
    name, images = ctx["product_name"] or "this product", ctx["images"] or []
    sent = list(ctx["sent"] or [])
    if not images:
        return False

    if is_more:
        rest = [i for i in images if i["url"] not in sent]
        if not rest:
            await send_text(phone_number_id, access_token, to, f"That's all the photos I have of {name}.")
            return True
        got = await _send_batch(phone_number_id, access_token, to, name, rest)
        sent += got
        _mark_sent(session_id, sent)
        left = len([i for i in images if i["url"] not in sent])
        if left:
            await send_text(phone_number_id, access_token, to, f"Reply *MORE* for {left} more photo{'s' if left != 1 else ''}.")
        return True

    # a colour the business labelled, named in the message
    colours = {}
    for i in images:
        c = (i.get("colour") or "").strip()
        if c:
            colours.setdefault(c, []).append(i)
    colour = next((c for c in sorted(colours, key=len, reverse=True)
                   if re.search(r"(?<![a-z])" + re.escape(c.lower()) + r"(?![a-z])", t.lower())), None)
    if colour and (view is None or any(i.get("view") == view for i in colours[colour])):
        imgs = [i for i in colours[colour] if view is None or i.get("view") == view]
        got = await _send_batch(phone_number_id, access_token, to, name, imgs)
        if got:
            await send_text(phone_number_id, access_token, to, f"Here's {name} in {colour}.")
            _mark_sent(session_id, sent + [u for u in got if u not in sent])
            return True
        return False

    # a view ("show me the back"), only when they're asking to see something
    if view and _PHOTO_WORDS.search(t):
        labelled = [i for i in images if i.get("view") == view]
        if labelled:
            got = await _send_batch(phone_number_id, access_token, to, name, labelled)
            _mark_sent(session_id, sent + [u for u in got if u not in sent])
            return bool(got)
        if len(images) > 1:
            await send_text(phone_number_id, access_token, to,
                            f"I don't have a photo showing {_showing(view, name)}, so here are all its photos:")
            got = await _send_batch(phone_number_id, access_token, to, name, images)
            sent = sent + [u for u in got if u not in sent]
            _mark_sent(session_id, sent)
            left = len([i for i in images if i["url"] not in sent])
            if left:
                await send_text(phone_number_id, access_token, to, f"Reply *MORE* for {left} more photo{'s' if left != 1 else ''}.")
            return True
        # one photo or none: ask the team
        await send_text(phone_number_id, access_token, to,
                        f"I don't have a photo showing {_showing(view, name)} yet. I'll ask the team to send you one.")
        try:
            from wa_db import create_handoff
            create_handoff(session_id, tenant_id, to)
            from meta_webhook import notify_merchant_handoff
            await notify_merchant_handoff(tenant_id, phone_number_id, access_token, to, text)
        except Exception as e:
            print("⚠️ wa_photos handoff:", e)
        return True
    return False
