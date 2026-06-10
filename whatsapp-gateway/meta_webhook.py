import asyncio
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
from meta_sender import send_text, mark_as_read
from response_formatter import dispatch_response
from interactive_handler import handle_addcart, handle_details, handle_list_select
from wa_db import log_message, is_handoff_active, create_handoff, cache_products, get_wa_shop_session, delete_wa_shop_session, get_viewed_products
from wa_onboarding import handle_onboarding_message
from wa_shopping import handle_shopping_message

router = APIRouter()

_APP_SECRET             = os.getenv("META_APP_SECRET", "")
_VERIFY_TOKEN           = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
_SETUP_PHONE_NUMBER_ID  = os.getenv("WA_SETUP_PHONE_NUMBER_ID", "")
_AI_BACKEND_URL = os.getenv("AI_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

_HUMAN_KEYWORDS = frozenset({
    "agent", "human", "real person", "speak to someone", "talk to someone",
    "speak to a person", "talk to a person", "connect me to", "live agent",
    "speak to agent", "talk to agent", "call me", "phone me",
    "speak to a human", "talk to a human",
})

_ORDER_KEYWORDS = frozenset({
    "i want to order", "place an order", "i'd like to order",
    "i'll take", "i will take", "i want to buy",
    "how do i order", "how to order", "can i order",
    "i want to purchase", "i'd like to buy",
})

_DISCOUNT_KEYWORDS = frozenset({
    "discount", "get a discount", "want a discount", "any discount",
    "give me a discount", "can i get a discount", "offer a discount",
    "better price", "lower price", "negotiate",
})


def _wants_human(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _HUMAN_KEYWORDS)


async def notify_merchant_handoff(
    tenant_id: int,
    phone_number_id: str,
    access_token: str,
    customer_phone: str,
    last_customer_message: str,
) -> None:
    """
    Send a real-time WhatsApp alert to the merchant's personal phone when
    a customer requests a human agent.
    Sends FROM the tenant's own WhatsApp Business number.
    Silently skips if no personal phone is configured.
    """
    from wa_db import get_db_connection as _gdb
    from datetime import datetime as _dt

    conn = _gdb()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT t.name AS biz_name, t.report_phone,
                   c.phone_number AS fallback_phone
            FROM tenants t
            LEFT JOIN customers c ON c.tenant_id = t.id
                AND c.is_active = TRUE AND c.phone_number IS NOT NULL
            WHERE t.id = %s
            ORDER BY c.id ASC LIMIT 1
        """, (tenant_id,))
        row = cur.fetchone()
        if not row:
            return

        biz_name = (row[0] or f"Merchant {tenant_id}").strip()
        to_phone = ((row[1] or row[2] or "")).strip().lstrip("+")
        if not to_phone:
            print(f"ℹ️ [HANDOFF NOTIFY] tenant={tenant_id} — no personal phone configured, skipping")
            return

        now_str = _dt.now().strftime("%-d %b %Y, %H:%M")
        preview = (last_customer_message or "")[:80]

        msg = (
            f"🙋 *New Handoff — {biz_name}*\n\n"
            f"A customer needs your attention:\n\n"
            f"👤 Customer: +{customer_phone}\n"
            f"💬 Last message: \"{preview}\"\n"
            f"🕐 Time: {now_str}\n\n"
            f"👉 Reply here: portal.phixtra.com/inbox"
        )

        await send_text(phone_number_id, access_token, to_phone, msg)
        print(f"✅ [HANDOFF NOTIFY] sent to merchant {to_phone} for tenant={tenant_id}")

    except Exception as e:
        print(f"⚠️ notify_merchant_handoff error: {e}")
    finally:
        cur.close()
        conn.close()


def _notify_merchant_quota(tenant_id: int, phone_number_id: str, access_token: str,
                            msgs_used: int, msgs_limit: int, plan_slug: str) -> None:
    """Send a one-time WhatsApp alert to the merchant when quota is exceeded."""
    import asyncio as _aio
    from wa_db import get_db_connection as _gdb

    conn = _gdb()
    if not conn:
        return
    cur = conn.cursor()
    try:
        # Only notify once per billing period — check quota_notified_at
        cur.execute("""
            SELECT t.quota_notified_at, t.plan_period_start,
                   c.phone_number AS merchant_phone
            FROM tenants t
            LEFT JOIN customers c ON c.tenant_id = t.id AND c.is_active = TRUE
            WHERE t.id = %s
            ORDER BY c.id ASC LIMIT 1
        """, (tenant_id,))
        row = cur.fetchone()
        if not row:
            return

        notified_at   = row[0]
        period_start  = row[1]
        merchant_phone = row[2]

        # Skip if already notified this period
        if notified_at and period_start and notified_at.date() >= period_start:
            return
        if not merchant_phone:
            return

        msg = (
            f"⚠️ *PhiXtra Alert — Quota Reached*\n\n"
            f"Your {plan_slug.title()} plan has used all {msgs_limit} AI messages "
            f"for this billing period.\n\n"
            f"Your AI assistant has been paused until you upgrade or your period resets.\n\n"
            f"👉 Upgrade now: portal.phixtra.com/billing/plans"
        )

        to = merchant_phone.lstrip("+")
        # Fire-and-forget in a new event loop thread
        import threading
        def _send():
            import asyncio as _aio2
            from meta_sender import send_text as _st
            _aio2.run(_st(phone_number_id, access_token, to, msg))
        threading.Thread(target=_send, daemon=True).start()

        # Stamp notified_at
        cur.execute("UPDATE tenants SET quota_notified_at=NOW() WHERE id=%s", (tenant_id,))
        conn.commit()
    except Exception as e:
        print(f"⚠️ _notify_merchant_quota error: {e}")
    finally:
        cur.close()
        conn.close()


def _wants_order(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _ORDER_KEYWORDS)


def _wants_discount_start(text: str) -> bool:
    lower = text.lower().strip()
    return any(kw in lower for kw in _DISCOUNT_KEYWORDS)


def _verify_signature(body: bytes, sig_header: str, secret: str = "") -> bool:
    """
    Verify Meta HMAC-SHA256 signature from X-Hub-Signature-256 header.
    Uses the provided secret (tenant-level), falling back to the platform
    META_APP_SECRET env var. If neither is set, passes to allow initial setup.
    """
    effective_secret = secret or _APP_SECRET
    if not effective_secret:
        return True
    expected = hmac.new(effective_secret.encode(), body, hashlib.sha256).hexdigest()
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

    # ── Route to onboarding handler if this is the setup number ──────────────
    if _SETUP_PHONE_NUMBER_ID and phone_number_id == _SETUP_PHONE_NUMBER_ID:
        if not _verify_signature(body, x_hub_signature_256 or ""):
            print("⚠️ [META] HMAC mismatch on setup number")
            return {"status": "ok"}
        # Dedup using tenant_id=0 (sentinel for pre-provisioning messages)
        logged = log_message(
            tenant_id=0,
            phone_number_id=phone_number_id,
            customer_phone=customer_phone,
            direction="inbound",
            content=text,
            message_type=msg["message_type"],
            meta_message_id=meta_message_id,
        )
        if not logged:
            return {"status": "ok", "reason": "duplicate"}
        print(f"📋 [ONBOARDING] {customer_phone}: {text[:80]}")
        await handle_onboarding_message(msg)
        return {"status": "ok"}

    tenant = get_tenant_by_phone_number_id(phone_number_id)
    if not tenant:
        print(f"⚠️ [META] No active tenant for phone_number_id={phone_number_id}")
        return {"status": "ok", "reason": "no_tenant"}

    # Verify HMAC signature using the tenant's app_secret (falls back to platform secret)
    if not _verify_signature(body, x_hub_signature_256 or "", tenant.get("app_secret") or ""):
        print("⚠️ [META] HMAC mismatch — possible spoofed request, ignoring")
        return {"status": "ok"}

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

    # Mark message as read (blue ticks) — fire-and-forget
    asyncio.create_task(mark_as_read(phone_number_id, access_token, meta_message_id))

    # ── Handoff gate ──────────────────────────────────────────────────────────
    if is_handoff_active(session_id):
        # Allow HANDOFF_DISCOUNT sessions through so the customer can proceed or cancel
        _hs = get_wa_shop_session(session_id)
        if _hs and _hs.get("state") == "HANDOFF_DISCOUNT":
            print(f"   [META] HANDOFF_DISCOUNT session — routing to shopping handler")
            await handle_shopping_message(msg, tenant, _hs)
            log_message(tenant_id, phone_number_id, customer_phone, "outbound",
                        "[shopping:HANDOFF_DISCOUNT]")
            return {"status": "ok", "reason": "shopping"}
        print(f"   [META] Handoff active for session={session_id} — AI skipped")
        return {"status": "ok", "reason": "handoff_active"}

    # ── Shopping journey routing ──────────────────────────────────────────────
    shop_session = get_wa_shop_session(session_id)
    # COMPLETE sessions are finished — delete so customer can start a fresh order
    if shop_session and shop_session.get("state") == "COMPLETE":
        delete_wa_shop_session(session_id)
        shop_session = None
    # PAYMENT_PENDING / PAYMENT_REVIEW: if the customer asks a new question (not CANCEL,
    # not a payment photo), let the AI answer it — shopping session stays open in the DB.
    _shopping_state = shop_session.get("state") if shop_session else None
    _override_to_ai = (
        _shopping_state in ("PAYMENT_PENDING", "PAYMENT_REVIEW")
        and msg.get("message_type") == "text"
        and text.strip().lower() not in {"cancel", "stop", "exit", "quit", "cancel order", "abort"}
    )
    if (shop_session and not _override_to_ai) or _wants_order(text) or _wants_discount_start(text):
        state_label = shop_session["state"] if shop_session else "new"
        print(f"   [META] Shopping route session={session_id} state={state_label}")
        await handle_shopping_message(msg, tenant, shop_session)
        log_message(tenant_id, phone_number_id, customer_phone, "outbound",
                    f"[shopping:{state_label}]")
        return {"status": "ok", "reason": "shopping"}

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

    if action_type == "list_select" and action_product_id:
        handled = await handle_list_select(
            phone_number_id, access_token, customer_phone, action_product_id, session_id
        )
        if handled:
            log_message(tenant_id, phone_number_id, customer_phone, "outbound",
                        f"[Product card sent for product {action_product_id}]")
            return {"status": "ok", "reason": "list_select_handled"}
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
        await notify_merchant_handoff(tenant_id, phone_number_id, access_token, customer_phone, text)
        return {"status": "ok", "reason": "escalated_keyword"}

    # ── Inject viewed-product context so AI knows the customer journey ────────
    viewed = get_viewed_products(session_id)
    if viewed:
        if len(viewed) == 1:
            p = viewed[0]
            stock = "In Stock" if p.get("in_stock", True) else "Out of Stock"
            ctx = (
                f"[Background reference only — do not let this override the customer's current request: "
                f"The customer viewed {p['product_name']} ({stock}) earlier today. "
                f"If they are asking about something different now, respond to their current request. "
                f"If they want to order, tell them to reply ORDER.]"
            )
        else:
            lines = []
            for i, p in enumerate(viewed, 1):
                stock = "In Stock" if p.get("in_stock", True) else "Out of Stock"
                lines.append(f"{p['product_name']} ({stock})")
            ctx = (
                f"[Background reference only — do not let this override the customer's current request: "
                f"The customer viewed these products earlier today: "
                + ", ".join(lines)
                + ". If they are now asking about a different product, respond to that. "
                + "If they want to order, tell them to reply ORDER.]"
            )
        ai_message = f"{ctx}\n\n{text}"
    else:
        ai_message = text

    # When bypassing the shopping handler, tell the AI about the pending order so it
    # can answer the customer's question AND append a one-line payment reminder.
    if _override_to_ai and shop_session:
        _cart = shop_session.get("cart") or {}
        if isinstance(_cart, str):
            try:
                _cart = json.loads(_cart)
            except Exception:
                _cart = {}
        _pname = _cart.get("product_name", "your order")
        _price = _cart.get("final_price") or _cart.get("unit_price") or 0
        try:
            _price_str = f"₦{float(_price):,.0f}"
        except Exception:
            _price_str = str(_price)
        _state_hint = (
            "awaiting payment proof photo"
            if _shopping_state == "PAYMENT_PENDING"
            else "under merchant payment review"
        )
        _order_ctx = (
            f"[System note: This customer has an ACTIVE ORDER for {_pname} ({_price_str}) "
            f"that is currently {_state_hint}. "
            f"Respond to their question normally, then end with ONE brief sentence "
            f"reminding them to send their payment proof photo for this pending order.]"
        )
        print(f"   [META] Pending-order system_addon set for state={_shopping_state}")

    # Send immediate ack so customer sees a response before AI finishes thinking
    _ack = (tenant.get("typing_ack_text") or "").strip()
    if _ack:
        await send_text(phone_number_id, access_token, customer_phone, _ack)

    # ── Call Phixtra AI backend ───────────────────────────────────────────────
    _chat_payload: dict = {"api_key": api_key, "message": ai_message, "session_id": session_id}
    if _override_to_ai and shop_session:
        _chat_payload["system_addon"] = _order_ctx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_AI_BACKEND_URL}/chat",
                json=_chat_payload,
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

    # ── Quota exceeded — stop AI, notify customer + merchant (once) ──────────
    if result.get("quota_exceeded"):
        print(f"🚫 [META] quota_exceeded for tenant={tenant_id} — sending fallback")
        fallback = (
            "Our assistant is temporarily unavailable. "
            "Please contact us directly for assistance."
        )
        await send_text(phone_number_id, access_token, customer_phone, fallback)
        log_message(tenant_id, phone_number_id, customer_phone, "outbound", fallback)

        # One-time merchant alert per billing period
        try:
            _notify_merchant_quota(tenant_id, phone_number_id, access_token,
                                   result.get("messages_used", 0),
                                   result.get("messages_limit", 0),
                                   result.get("plan_slug", "free"))
        except Exception as _qe:
            print(f"⚠️ [META] quota merchant notify error: {_qe}")

        return {"status": "ok", "reason": "quota_exceeded"}
    # ─────────────────────────────────────────────────────────────────────────

    reply             = (result.get("reply") or "").strip()
    handoff_triggered = bool(result.get("handoff_triggered"))
    products          = result.get("product_recommendations") or []

    # Cache product data so "Add to Cart" / "View Details" taps can resolve URLs
    if products:
        cache_products(session_id, products)

    # Send reply + interactive messages (list or buttons depending on product count)
    await dispatch_response(phone_number_id, access_token, customer_phone, reply, products, session_id)

    # Log the outbound reply for analytics
    log_label = f"[{len(products)} product(s) + reply]" if products else reply
    log_message(tenant_id, phone_number_id, customer_phone, "outbound", log_label)

    if handoff_triggered:
        print(f"🙋 [META] AI handoff triggered — escalating session={session_id}")
        create_handoff(session_id, tenant_id, customer_phone)
        await notify_merchant_handoff(tenant_id, phone_number_id, access_token, customer_phone, text)

    return {"status": "ok", "handoff_triggered": handoff_triggered}
