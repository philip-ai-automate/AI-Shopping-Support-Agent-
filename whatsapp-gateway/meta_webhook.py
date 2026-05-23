import hashlib
import hmac
import json
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import PlainTextResponse

from tenant_router import get_tenant_by_phone_number_id
from message_normalizer import normalize
from meta_sender import send_text
from response_formatter import dispatch_response
from interactive_handler import handle_addcart, handle_details
from wa_db import log_message, is_handoff_active, create_handoff, cache_products

router = APIRouter()

_APP_SECRET = os.getenv("META_APP_SECRET", "")
_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
_AI_BACKEND_URL = os.getenv("AI_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

_HUMAN_KEYWORDS = frozenset({
    "agent", "human", "real person", "speak to someone", "talk to someone",
    "speak to a person", "talk to a person", "connect me to", "live agent",
    "speak to agent", "talk to agent", "call me", "phone me",
    "speak to a human", "talk to a human",
})


def _wants_human(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _HUMAN_KEYWORDS)


def _verify_signature(body: bytes, sig_header: str) -> bool:
    """
    Verify Meta HMAC-SHA256 signature from X-Hub-Signature-256 header.
    If META_APP_SECRET is not set, all requests pass (allows initial setup).
    """
    if not _APP_SECRET:
        return True
    expected = hmac.new(_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", sig_header or "")


@router.get("/meta-webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """
    One-time Meta webhook verification handshake.
    Called by Meta when you register the webhook URL in the App dashboard.
    """
    if hub_mode == "subscribe" and hub_verify_token == _VERIFY_TOKEN and hub_challenge:
        print("✅ [META] Webhook verified — challenge returned")
        return PlainTextResponse(hub_challenge)
    print(f"⚠️ [META] Verification failed: mode={hub_mode} token_match={hub_verify_token == _VERIFY_TOKEN}")
    return PlainTextResponse("Verification failed", status_code=403)


@router.post("/meta-webhook")
async def receive_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None),
):
    """
    Meta webhook receiver — handles all inbound WhatsApp events.
    Always returns HTTP 200; non-200 causes Meta to retry indefinitely.
    """
    body = await request.body()

    if not _verify_signature(body, x_hub_signature_256 or ""):
        print("⚠️ [META] HMAC mismatch — possible spoofed request, ignoring")
        return {"status": "ok"}

    try:
        payload = json.loads(body)
    except Exception:
        return {"status": "ok"}

    msg = normalize(payload)
    if msg is None:
        return {"status": "ok", "reason": "ignored"}

    phone_number_id   = msg["phone_number_id"]
    customer_phone    = msg["customer_phone"]
    session_id        = msg["session_id"]
    text              = msg["text"]
    meta_message_id   = msg["meta_message_id"]
    action_type       = msg["action_type"]
    action_product_id = msg["action_product_id"]

    tenant = get_tenant_by_phone_number_id(phone_number_id)
    if not tenant:
        print(f"⚠️ [META] No active tenant for phone_number_id={phone_number_id}")
        return {"status": "ok", "reason": "no_tenant"}

    tenant_id    = int(tenant["tenant_id"])
    api_key      = tenant["phixtra_api_key"]
    access_token = tenant["access_token"]

    # Dedup: INSERT IGNORE returns rowcount=0 if meta_message_id already seen
    logged = log_message(
        tenant_id=tenant_id,
        phone_number_id=phone_number_id,
        customer_phone=customer_phone,
        direction="inbound",
        content=text,
        message_type=msg["message_type"],
        meta_message_id=meta_message_id,
    )
    if not logged:
        print(f"   [META] Duplicate message_id={meta_message_id} — skipped")
        return {"status": "ok", "reason": "duplicate"}

    print(f"✅ [META] session={session_id} from={customer_phone} action={action_type or 'text'}: {text[:80]}")

    # ── Handoff gate ──────────────────────────────────────────────────────────
    if is_handoff_active(session_id):
        print(f"   [META] Handoff active for session={session_id} — AI skipped")
        return {"status": "ok", "reason": "handoff_active"}

    # ── Interactive button actions that bypass the AI ─────────────────────────
    if action_type == "addcart" and action_product_id:
        handled = await handle_addcart(
            phone_number_id, access_token, customer_phone, action_product_id, session_id
        )
        if handled:
            log_message(tenant_id, phone_number_id, customer_phone, "outbound",
                        f"[Checkout message sent for product {action_product_id}]")
            return {"status": "ok", "reason": "addcart_handled"}
        # Not in cache — fall through to AI

    if action_type == "details" and action_product_id:
        handled = await handle_details(
            phone_number_id, access_token, customer_phone, action_product_id, session_id
        )
        if handled:
            log_message(tenant_id, phone_number_id, customer_phone, "outbound",
                        f"[Product details sent for product {action_product_id}]")
            return {"status": "ok", "reason": "details_handled"}
        # Not in cache — fall through to AI

    # ── Keyword-triggered human escalation ────────────────────────────────────
    if _wants_human(text):
        print(f"🙋 [META] Human keyword → escalating session={session_id}")
        escalation_msg = (
            "I'm connecting you to a member of our team right now. "
            "Someone will be with you shortly."
        )
        await send_text(phone_number_id, access_token, customer_phone, escalation_msg)
        log_message(tenant_id, phone_number_id, customer_phone, "outbound", escalation_msg)
        create_handoff(session_id, tenant_id, customer_phone)
        return {"status": "ok", "reason": "escalated_keyword"}

    # ── Call Phixtra AI backend ───────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_AI_BACKEND_URL}/chat",
                json={"api_key": api_key, "message": text, "session_id": session_id},
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        print(f"⚠️ [META] /chat failed session={session_id}: {e}")
        error_msg = (
            "I'm sorry, I'm experiencing a technical issue right now. "
            "Please try again in a moment."
        )
        await send_text(phone_number_id, access_token, customer_phone, error_msg)
        log_message(tenant_id, phone_number_id, customer_phone, "outbound", error_msg)
        return {"status": "ok", "reason": "chat_error"}

    reply             = (result.get("reply") or "").strip()
    handoff_triggered = bool(result.get("handoff_triggered"))
    products          = result.get("product_recommendations") or []

    # Cache product data so "Add to Cart" / "View Details" taps can resolve URLs
    if products:
        cache_products(session_id, products)

    # Send reply + interactive messages (list or buttons depending on product count)
    await dispatch_response(phone_number_id, access_token, customer_phone, reply, products)

    # Log the outbound reply for analytics
    log_label = f"[{len(products)} product(s) + reply]" if products else reply
    log_message(tenant_id, phone_number_id, customer_phone, "outbound", log_label)

    if handoff_triggered:
        print(f"🙋 [META] AI handoff triggered — escalating session={session_id}")
        create_handoff(session_id, tenant_id, customer_phone)

    return {"status": "ok", "handoff_triggered": handoff_triggered}
