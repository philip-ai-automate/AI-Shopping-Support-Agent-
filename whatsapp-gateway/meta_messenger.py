"""
meta_messenger.py — Facebook Messenger webhook handling (omnichannel Phase
2, 2026-09-11).

Meta's Messenger payload has a different shape from WhatsApp's
("object": "page", entry[].messaging[] — vs entry[].changes[].value for
WhatsApp), so it gets its own small parser here rather than being squeezed
into message_normalizer.py, which is WhatsApp end to end. Dispatched from
meta_webhook.py's receive_webhook() when payload["object"] == "page".
"""
import hashlib
import hmac
import os

import psycopg2.extras
from wa_db import get_db_connection

_APP_SECRET = os.getenv("META_APP_SECRET", "") or os.getenv("FB_APP_SECRET", "")


def _verify_signature(body: bytes, sig_header: str) -> bool:
    """Same HMAC check as WhatsApp's — same app, same shared secret."""
    if not _APP_SECRET:
        return True
    expected = hmac.new(_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", sig_header or "")


def _get_page(page_id: str):
    """The tenant that owns this connected Page, or None — a webhook for a
    Page we don't (or no longer) have connected is dropped quietly rather
    than treated as an error; Meta retries on non-200, and a stray/late
    delivery after a disconnect is an expected, harmless case."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT tenant_id FROM fb_pages WHERE page_id=%s AND active=TRUE LIMIT 1",
            (page_id,),
        )
        return cur.fetchone()
    except Exception as e:
        print("⚠️ meta_messenger _get_page error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def _log_message(tenant_id: int, page_id: str, psid: str, direction: str,
                  content: str, message_type: str = "text",
                  meta_message_id: str = None) -> bool:
    """Insert into fb_message_log. Same dedup shape as wa_db.log_message —
    ON CONFLICT against the partial unique index on meta_message_id."""
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO fb_message_log
              (tenant_id, page_id, psid, direction, content, message_type, meta_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (meta_message_id) WHERE meta_message_id IS NOT NULL DO NOTHING
            """,
            (tenant_id, page_id, psid, direction, content, message_type, meta_message_id),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print("⚠️ meta_messenger _log_message error:", e)
        return False
    finally:
        cur.close()
        conn.close()


async def handle_messenger_webhook(payload: dict, body: bytes, sig_header: str) -> None:
    """
    Parse and store inbound Messenger events. Best-effort throughout — the
    caller (meta_webhook.py) always returns HTTP 200 to Meta regardless of
    what happens in here, so problems are logged, never raised.
    """
    if not _verify_signature(body, sig_header):
        print("⚠️ [MESSENGER] HMAC mismatch — ignoring")
        return

    for entry in payload.get("entry", []) or []:
        page_id = entry.get("id", "")
        if not page_id:
            continue
        page = _get_page(page_id)
        if not page:
            continue
        tenant_id = page["tenant_id"]

        for event in entry.get("messaging", []) or []:
            # Delivery/read receipts carry no message content to store.
            if "delivery" in event or "read" in event:
                continue
            message = event.get("message")
            if not message:
                continue
            if message.get("is_echo"):
                # Our own outbound message, reflected back by Meta — already
                # logged when it was sent (see inbox_reply's Messenger
                # branch in portal_routes.py), so skip to avoid a duplicate.
                continue

            psid = (event.get("sender") or {}).get("id", "")
            if not psid:
                continue

            text = message.get("text", "")
            attachments = message.get("attachments") or []
            message_type = "text"
            if not text and attachments:
                # Media/sticker/location — Phase 2 stores a readable
                # placeholder rather than downloading and re-hosting the
                # attachment. That's the same media-handling work WhatsApp
                # already has for its own attachments; revisit here if
                # Messenger needs the same treatment.
                atype = attachments[0].get("type", "attachment")
                text = f"[{atype}]"
                if atype in ("image", "video", "audio"):
                    message_type = atype

            mid = message.get("mid") or None
            ok = _log_message(tenant_id, page_id, psid, "inbound", text, message_type, mid)
            if ok:
                print(f"📩 [MESSENGER] tenant={tenant_id} page={page_id} psid={psid}: {text[:80]}")
