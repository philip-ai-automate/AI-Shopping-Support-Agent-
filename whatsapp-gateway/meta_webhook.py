import asyncio
import hashlib
import hmac
import json
import os
import re
import smtplib
from email.message import EmailMessage
from typing import Optional

import httpx
import psycopg2.extras
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import PlainTextResponse

from tenant_router import get_tenant_by_phone_number_id
from wa_db import get_db_connection as _get_db
from message_normalizer import normalize, extract_statuses
from meta_sender import send_text, send_template, mark_as_read
from response_formatter import dispatch_response
from interactive_handler import handle_addcart, handle_details, handle_list_select
from wa_db import log_message, is_handoff_active, is_campaign_recipient, create_handoff, cache_products, get_wa_shop_session, delete_wa_shop_session, get_viewed_products, get_active_template, update_campaign_recipient_status, get_session_products, mark_product_viewed, record_cross_channel_optout, get_latest_campaign_recipient_for_reply, record_campaign_reply_flag, queue_campaign_reply_for_review, create_pipeline_opportunity_from_reply
from wa_onboarding import handle_onboarding_message
from wa_shopping import handle_shopping_message
from visual_match import evaluate_visual_match

router = APIRouter()

_APP_SECRET             = os.getenv("META_APP_SECRET", "")
_VERIFY_TOKEN           = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
_SETUP_PHONE_NUMBER_ID  = os.getenv("WA_SETUP_PHONE_NUMBER_ID", "")
_AI_BACKEND_URL = os.getenv("AI_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


def _get_agent_system_prompt(agent_id: int) -> str | None:
    """Fetch the system_prompt for a specific tenant_agent row. Returns None on any error."""
    try:
        conn = _get_db()
        if not conn:
            return None
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT system_prompt FROM tenant_agents WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return (row["system_prompt"] or "").strip() or None
    except Exception as e:
        print(f"⚠️ [META] _get_agent_system_prompt error agent_id={agent_id}: {e}")
        return None

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

# Exact-match only (not substring) — "stop" as a bare word in normal shopping
# chat ("please stop, show me the red one instead") must NOT trigger this.
_OPT_OUT_KEYWORDS = frozenset({
    "stop", "unsubscribe", "cancel", "opt out", "optout",
    "stop messages", "remove me", "unsub",
})


def _wants_opt_out(text: str) -> bool:
    return (text or "").strip().lower() in _OPT_OUT_KEYWORDS


async def _handle_opt_out(tenant_id: int, customer_phone: str, phone_number_id: str, access_token: str) -> None:
    """Records the opt-out (upsert — the customer may not have an existing
    wa_contacts row yet), then extends it to Email and SMS too — a "STOP"
    on WhatsApp means stop everywhere, not just here — before confirming it
    back to the customer. Called instead of all normal AI/shopping/handoff
    processing for this message."""
    phone = "+" + customer_phone.lstrip("+")
    try:
        conn = _get_db()
        if conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO wa_contacts (tenant_id, phone, opted_out, opted_out_at, source)
                       VALUES (%s, %s, TRUE, NOW(), 'whatsapp')
                   ON CONFLICT (tenant_id, phone) DO UPDATE
                       SET opted_out=TRUE, opted_out_at=NOW()""",
                (tenant_id, phone),
            )
            conn.commit()
            cur.close(); conn.close()
    except Exception as e:
        print(f"⚠️ [META] opt-out DB write error tenant={tenant_id} phone={customer_phone}: {e}")

    record_cross_channel_optout(tenant_id, phone, reason='Replied "STOP" to a WhatsApp campaign')


async def _handle_campaign_reply_flag(tenant: dict, tenant_id: int, customer_phone: str, text: str) -> None:
    """Campaign Intelligence: classifies a reply from a past campaign
    recipient (Interested / Not interested / neutral) and always records that
    flag on the campaign report — this "flagging" step is never gated. Only
    the follow-on action for an Interested reply (creating a Sales Pipeline
    opportunity) is gated by the business's own campaign_reply_auto_actions
    switch: on (default) creates it immediately, off queues it for a staff
    member to approve/reject on the new Needs Review screen instead.
    Fire-and-forget from the webhook — never blocks or affects the AI reply
    the customer actually receives."""
    if not text or not text.strip():
        return
    recipient = get_latest_campaign_recipient_for_reply(tenant_id, customer_phone)
    if not recipient:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_AI_BACKEND_URL}/classify-campaign-reply",
                json={"api_key": tenant["phixtra_api_key"], "message": text},
            )
            resp.raise_for_status()
            result = resp.json()
        sentiment  = result.get("sentiment", "neutral")
        confidence = float(result.get("confidence", 0.0) or 0.0)
    except Exception as e:
        print(f"⚠️ [CAMPAIGN_INTEL] classify-campaign-reply failed, treating as neutral: {e}")
        sentiment, confidence = "neutral", 0.0

    new_status = record_campaign_reply_flag(
        recipient["id"], recipient["status"], sentiment, confidence, text,
    )
    print(f"   [CAMPAIGN_INTEL] recipient_id={recipient['id']} tenant={tenant_id} "
          f"phone={customer_phone} sentiment={sentiment} confidence={confidence} -> status={new_status}")

    if new_status != "interested":
        return

    if tenant.get("campaign_reply_auto_actions", True):
        create_pipeline_opportunity_from_reply(
            tenant_id, recipient["campaign_id"], recipient["id"], customer_phone, text,
        )
    else:
        queue_campaign_reply_for_review(
            tenant_id, recipient["campaign_id"], recipient["id"], customer_phone, text, confidence,
        )

    try:
        await send_text(
            phone_number_id, access_token, customer_phone,
            "You've been unsubscribed from marketing messages on WhatsApp, email and SMS, and won't "
            "receive further campaign texts. This won't affect your ability to message us directly.",
        )
    except Exception as e:
        print(f"⚠️ [META] opt-out confirmation send error: {e}")


def _send_handoff_email(
    to_email: str,
    biz_name: str,
    customer_phone: str,
    last_message: str,
) -> None:
    """Send a handoff alert email to the merchant. Fire-and-forget — never raises."""
    host     = os.getenv("SMTP_HOST", "").strip()
    port     = int(os.getenv("SMTP_PORT", "587"))
    user     = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender   = os.getenv("SMTP_FROM", user or "noreply@phixtra.com").strip()

    if not host or not to_email:
        print(f"⚠️ [HANDOFF EMAIL] SMTP not configured or no recipient — skipping")
        return

    portal_url   = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")
    digits_only  = re.sub(r'[^\d]', '', customer_phone)
    wa_url       = f"https://wa.me/{digits_only}"
    preview    = (last_message or "")[:200]

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px">
      <h2 style="color:#030C18;margin:0 0 4px">🙋 Customer Needs Your Attention</h2>
      <p style="color:#888;font-size:13px;margin:0 0 20px">{biz_name}</p>
      <table style="border-collapse:collapse;width:100%;margin-bottom:16px">
        <tr>
          <td style="padding:8px 12px;font-weight:700;background:#f3f4f6;border:1px solid #e5e7eb;width:140px;font-size:13px">WhatsApp</td>
          <td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:14px;font-weight:700;color:#030C18">+{customer_phone}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;font-weight:700;background:#f3f4f6;border:1px solid #e5e7eb;font-size:13px">Last message</td>
          <td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:14px">{preview}</td>
        </tr>
      </table>
      <p style="margin:0 0 10px">
        <a href="{wa_url}" style="background:#25D366;color:#fff;padding:10px 20px;border-radius:10px;text-decoration:none;font-weight:700;display:inline-block;margin-right:10px">💬 Open WhatsApp</a>
        <a href="{portal_url}/inbox" style="background:#030C18;color:#fff;padding:10px 20px;border-radius:10px;text-decoration:none;font-weight:700;display:inline-block">📋 View in Portal</a>
      </p>
      <p style="color:#aaa;font-size:12px;margin-top:22px">Sent automatically by PhiXtra. Mark as handled in your portal inbox.</p>
    </div>"""

    text = (
        f"Customer needs your attention — {biz_name}\n\n"
        f"WhatsApp: +{customer_phone}\n"
        f"Last message: {preview}\n\n"
        f"Reply on WhatsApp: {wa_url}\n"
        f"View in portal: {portal_url}/inbox\n"
    )

    msg = EmailMessage()
    msg["From"]    = sender
    msg["To"]      = to_email
    msg["Subject"] = f"🙋 Customer needs your attention — {biz_name}"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            if user and password:
                server.login(user, password)
            server.send_message(msg)
        print(f"✅ [HANDOFF EMAIL] Alert sent to {to_email}")
    except Exception as e:
        print(f"⚠️ [HANDOFF EMAIL] Failed to send to {to_email}: {e}")


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
                   c.phone_number AS fallback_phone,
                   COALESCE(c.handoff_notify_email, c.email) AS alert_email
            FROM tenants t
            LEFT JOIN customers c ON c.tenant_id = t.id AND c.is_active = TRUE
            WHERE t.id = %s
            ORDER BY c.id ASC LIMIT 1
        """, (tenant_id,))
        row = cur.fetchone()
        if not row:
            return

        biz_name    = (row[0] or f"Business {tenant_id}").strip()
        to_phone    = ((row[1] or row[2] or "")).strip().lstrip("+")
        alert_email = (row[3] or "").strip()

        if not to_phone:
            print(f"ℹ️ [HANDOFF NOTIFY] tenant={tenant_id} — no personal phone configured, skipping WA alert")

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

        if to_phone:
            # Prefer the approved template — works even outside the 24h session
            # window (see phixtra_daily_report incident). Falls back to plain
            # text, which only delivers if the merchant messaged the business
            # number in the last 24h.
            tmpl = get_active_template(tenant_id, "handoff_alert")
            sent = False
            if tmpl:
                sent = await send_template(
                    phone_number_id, access_token, to_phone,
                    tmpl["template_name"], tmpl["language_code"],
                    [biz_name, customer_phone, preview, now_str],
                )
            if not sent:
                await send_text(phone_number_id, access_token, to_phone, msg)
            print(f"✅ [HANDOFF NOTIFY] WA sent to merchant {to_phone} for tenant={tenant_id} (via {'template' if sent else 'text'})")

        # Email fallback — always fires if an alert email is configured
        if alert_email:
            _send_handoff_email(alert_email, biz_name, customer_phone, last_customer_message)
        else:
            print(f"ℹ️ [HANDOFF EMAIL] No alert email for tenant={tenant_id} — skipping")

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


def _is_school_phone(phone_number_id: str) -> bool:
    """Return True if phone_number_id is registered in school_profiles."""
    from wa_db import get_db_connection as _gdb
    conn = _gdb()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT 1 FROM school_profiles WHERE wa_phone_number_id=%s AND is_active=TRUE LIMIT 1",
            (phone_number_id,)
        )
        return cur.fetchone() is not None
    except Exception:
        return False
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

    # Delivery-status callbacks (sent/delivered/read/failed) are a distinct
    # payload shape from inbound messages — handle and return early so they
    # never fall through to message-handling logic below.
    statuses = extract_statuses(payload)
    if statuses:
        status_pnid = statuses[0]["phone_number_id"]
        status_tenant = get_tenant_by_phone_number_id(status_pnid) if status_pnid else None
        status_secret = (status_tenant or {}).get("app_secret") or ""
        if not _verify_signature(body, x_hub_signature_256 or "", status_secret):
            print("⚠️ [META] HMAC mismatch on status webhook — ignoring")
            return {"status": "ok"}
        for s in statuses:
            if s.get("meta_message_id"):
                update_campaign_recipient_status(
                    s["meta_message_id"], s["status"],
                    s.get("error_code"), s.get("error_title"), s.get("error_message"),
                )
        return {"status": "ok", "reason": "status_update"}

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

    # ── Route to School Gateway (port 8002) if phone_number_id belongs to a school ──
    _school_check = _is_school_phone(phone_number_id)
    if _school_check:
        print(f"🏫 [META] Forwarding to school gateway — phone_number_id={phone_number_id}")
        try:
            async with httpx.AsyncClient(timeout=10.0) as _client:
                await _client.post("http://127.0.0.1:8002/webhook", json=payload)
        except Exception as _e:
            print(f"⚠️ [META] School gateway forward error: {_e}")
        return {"status": "ok"}

    # ── Route to Estate handler if phone_number_id belongs to an estate tenant ──
    from estate_webhook import (
        get_estate_tenant_by_phone_number_id as _get_estate_tenant,
        handle_estate_message as _handle_estate,
    )
    _estate_tenant = _get_estate_tenant(phone_number_id)
    if _estate_tenant:
        print(f"🏠 [META] Estate tenant={_estate_tenant['id']} — phone_number_id={phone_number_id}")
        await _handle_estate(msg, _estate_tenant, body, x_hub_signature_256 or "")
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

    # ── STOP opt-out — takes priority over everything else, including an
    # active handoff. A compliance-relevant request shouldn't get stuck
    # behind unrelated conversation state. ──────────────────────────────────
    if _wants_opt_out(text):
        print(f"🛑 [META] opt-out from={customer_phone} tenant={tenant_id}")
        await _handle_opt_out(tenant_id, customer_phone, phone_number_id, access_token)
        return {"status": "ok", "reason": "opted_out"}

    # ── PhiXtra Connect / AI-off gate — a PhiXtra-admin-only switch per
    # business (tenants.ai_enabled). Off means this business's WhatsApp
    # number is staff-only: the message is already logged above so it shows
    # in the Inbox, but nothing past this point (AI, shopping journey,
    # handoff, Campaign Intelligence classification) may fire. Staff replies
    # go out via the portal's own inbox_reply() send path, which never
    # touches this file. Moved ABOVE Campaign Intelligence on 2026-09-09 —
    # the reply classifier below is itself an AI (LLM) call and billed as
    # AI usage, so a Connect business with AI off must never trigger it
    # either, not just the shopping/chat AI. This stays TRUE (unaffected)
    # for portal.phixtra.com Sales AI tenants, which default ai_enabled=True
    # at signup. ─────────────────────────────────────────────────────────
    if not tenant.get("ai_enabled", True):
        print(f"   [META] AI off for tenant={tenant_id} — staff-only, no AI reply")
        return {"status": "ok", "reason": "ai_disabled"}

    # ── Campaign Intelligence — classify + flag a reply on the campaign
    # funnel (Replied/Interested/Not interested). Runs for every campaign
    # recipient's reply on an AI-enabled tenant, including one currently in
    # a human handoff (handoff only pauses the AI's own chat reply, not this
    # classification). Fire-and-forget so it never delays the customer's
    # actual reply or the staff Inbox. ───────────────────────────────────
    _is_campaign_reply = is_campaign_recipient(tenant_id, customer_phone)
    if _is_campaign_reply and msg.get("message_type") == "text":
        asyncio.create_task(_handle_campaign_reply_flag(tenant, tenant_id, customer_phone, text))

    # ── Handoff gate ──────────────────────────────────────────────────────────
    if is_handoff_active(session_id):
        # Allow HANDOFF_DISCOUNT sessions through so the customer can proceed or cancel
        _hs = get_wa_shop_session(session_id)
        if _hs and _hs.get("state") == "HANDOFF_DISCOUNT":
            print(f"   [META] HANDOFF_DISCOUNT session — routing to shopping handler")
            await handle_shopping_message(msg, tenant, _hs)
            return {"status": "ok", "reason": "shopping"}
        print(f"   [META] Handoff active for session={session_id} — AI skipped")
        return {"status": "ok", "reason": "handoff_active"}

    # ── Campaign reply gate — AI handles normally ─────────────────────────────
    # Campaign recipients are served by AI like any other customer.
    # Escalation is handled by the merchant's configured handoff rules (portal →
    # Handoff Settings), which the AI enforces via its system prompt. No separate
    # notification needed here.
    if not is_handoff_active(session_id) and _is_campaign_reply:
        print(f"   [META] Campaign recipient reply — AI handling session={session_id}")

    # ── Numbered product selection — replaces WhatsApp's old interactive list,
    # whose 24-character title limit made same-model variants indistinguishable.
    # The AI now sends a plain numbered list; the customer replies with a bare
    # number for full details, or "ORDER <number>" / "BUY <number>" to buy.
    # Resolves against the cached session products (same list_order the
    # numbered text was built from).
    _order_num = None
    if msg.get("message_type") == "text":
        _bare_num = re.fullmatch(r"(\d{1,2})", text.strip())
        _order_num = re.fullmatch(r"(?:order|buy)\s+(\d{1,2})", text.strip(), re.IGNORECASE)

        if _bare_num:
            _session_products = get_session_products(session_id)
            _idx = int(_bare_num.group(1))
            if 1 <= _idx <= len(_session_products):
                _product_id = _session_products[_idx - 1]["product_id"]
                _handled = await handle_list_select(
                    phone_number_id, access_token, customer_phone, _product_id, session_id
                )
                if _handled:
                    log_message(tenant_id, phone_number_id, customer_phone, "outbound",
                                f"[Product detail sent for numbered selection {_idx}]")
                    return {"status": "ok", "reason": "numbered_detail"}
                # not resolvable — fall through to AI

        elif _order_num:
            _session_products = get_session_products(session_id)
            _idx = int(_order_num.group(1))
            if 1 <= _idx <= len(_session_products):
                mark_product_viewed(session_id, _session_products[_idx - 1]["product_id"])
                print(f"   [META] Numbered order selection idx={_idx} marked viewed for session={session_id}")
            else:
                _order_num = None  # out of range — don't force the shopping route
            # falls through — _order_num truthy routes this to the shopping handler below

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
        _shopping_state in ("PAYMENT_PENDING", "PAYMENT_REVIEW", "AWAITING_FW_PAYMENT")
        and msg.get("message_type") == "text"
        and text.strip().lower() not in {"cancel", "stop", "exit", "quit", "cancel order", "abort"}
    )
    if (shop_session and not _override_to_ai) or _wants_order(text) or _wants_discount_start(text) or _order_num:
        state_label = shop_session["state"] if shop_session else "new"
        print(f"   [META] Shopping route session={session_id} state={state_label}")
        await handle_shopping_message(msg, tenant, shop_session)
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

    # ── Visual Product Match (Pro plan, opt-in) ────────────────────────────────
    # "media_url" on an inbound image is actually the WhatsApp media ID
    # (Meta only ever gives a `link` on outbound messages the gateway sends).
    visual_match_ctx = ""
    media_caption = msg.get("media_caption", "")
    if msg.get("message_type") == "image":
        print(f"   [META] inbound image message_type={msg.get('message_type')!r} media_url={msg.get('media_url')!r} media_caption={media_caption!r}")
    if msg.get("message_type") == "image" and msg.get("media_url"):
        try:
            vm_result = await evaluate_visual_match(
                tenant_id, api_key, access_token, msg["media_url"], media_caption,
            )
        except Exception as _vm_err:
            print(f"⚠️ [META] visual match error: {_vm_err}")
            vm_result = {"branch": "skip"}

        if vm_result["branch"] == "confident":
            print(f"   [VISUAL_MATCH] confident match session={session_id} product={vm_result['product_title']!r} score={vm_result['score']} in_stock={vm_result['in_stock']}")
            if vm_result["in_stock"]:
                visual_match_ctx = (
                    f"[System note: The customer just sent a photo. Image analysis matched it to "
                    f"\"{vm_result['product_title']}\" in your catalog with high confidence, and it IS "
                    f"in stock. Confirm this is the item they mean and recommend it using the normal "
                    f"product tag format — per your existing rules, do NOT state price, stock status, or "
                    f"any other detail in your text reply, the automatic product card handles that "
                    f"accurately. Just write one short confirming sentence and the tag.]"
                )
            else:
                # search_documents_with_meta (which builds the product tag/card) only
                # returns in-stock items, so this exact product will never resolve to a
                # card via the tag — must be stated in plain text or the customer never
                # learns it's unavailable.
                visual_match_ctx = (
                    f"[System note: The customer's photo matched \"{vm_result['product_title']}\", but "
                    f"that exact item is out of stock. Tell them it's out of stock and suggest similar "
                    f"in-stock alternatives using your normal product tag.]"
                )
            text = f"{vm_result['product_title']} — is this available?" if not media_caption.strip() else text

        elif vm_result["branch"] == "suggested":
            print(f"   [VISUAL_MATCH] suggested match session={session_id} product={vm_result['product_title']!r} score={vm_result['score']}")
            visual_match_ctx = (
                f"[System note: The customer just sent a photo. Image analysis suggests it might be "
                f"\"{vm_result['product_title']}\" in your catalog, but it is not a certain match. "
                f"Ask the customer to confirm this is the item they mean (name it specifically in your "
                f"question) — do NOT use the product tag yet and do NOT state price or stock status "
                f"until they confirm, since you don't yet know for certain which item this is.]"
            )
            text = f"{vm_result['product_title']} — is this available?" if not media_caption.strip() else text

        elif vm_result["branch"] == "uncertain" and vm_result["action"] == "handoff":
            print(f"   [VISUAL_MATCH] uncertain match, handing off session={session_id}")
            handoff_msg = (
                "Thanks for the photo! I'm not 100% sure which item that is from our catalog, "
                "so I'm getting a team member to help confirm — they'll be with you shortly."
            )
            await send_text(phone_number_id, access_token, customer_phone, handoff_msg)
            log_message(tenant_id, phone_number_id, customer_phone, "outbound", handoff_msg)
            create_handoff(session_id, tenant_id, customer_phone)
            await notify_merchant_handoff(tenant_id, phone_number_id, access_token, customer_phone, text)
            return {"status": "ok", "reason": "visual_match_handoff"}

        elif vm_result["branch"] == "uncertain":
            visual_match_ctx = (
                "[System note: The customer just sent a photo but it could not be confidently "
                "matched to a specific item in your catalog. Ask them to tell you the product "
                "name, or briefly describe it (color/type), so you can help — do not guess "
                "which product it is.]"
            )

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

    if visual_match_ctx:
        ai_message = f"{visual_match_ctx}\n\n{ai_message}"

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
        if _shopping_state == "PAYMENT_PENDING":
            _state_hint  = "awaiting payment proof photo"
            _reminder_ln = "reminding them to send their payment proof photo for this pending order"
        elif _shopping_state == "AWAITING_FW_PAYMENT":
            _state_hint  = "awaiting payment via the checkout link already sent"
            _reminder_ln = "reminding them to tap the payment link already sent above to complete this order"
        else:
            _state_hint  = "under store payment review"
            _reminder_ln = "reminding them their payment is being reviewed and they'll be notified once confirmed"
        _order_ctx = (
            f"[System note: This customer has an ACTIVE ORDER for {_pname} ({_price_str}) "
            f"that is currently {_state_hint}. "
            f"Respond to their question normally, then end with ONE brief sentence "
            f"{_reminder_ln}.]"
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
    # Per-number agent: fetch assigned agent's system prompt and override the tenant default
    _agent_id = tenant.get("agent_id")
    if _agent_id:
        _agent_prompt = _get_agent_system_prompt(int(_agent_id))
        if _agent_prompt:
            _chat_payload["override_system_prompt"] = _agent_prompt
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
