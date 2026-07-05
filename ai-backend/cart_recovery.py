"""
cart_recovery.py  — Intelligent Cart Revenue Recovery orchestrator.

Responsibilities:
  1. generate_recovery_email_ai()  — calls GPT-4o mini to write a personalised email
  2. send_touch_1_popup()          — marks the queue entry as 'in_progress' so the
                                     widget JS shows a recovery popup on next page load
  3. send_touch_2_email()          — AI-personalised recovery email (T+2 hr)
  4. send_touch_3_email()          — Final urgency email (T+24 hr)
  5. start_recovery_sequence()     — runs Touch 1 immediately then spawns a daemon
                                     background thread for Touches 2 and 3

Zero new infrastructure:
  - Email delivery  →  billing.send_email()  (existing SMTP config)
  - AI text         →  llm.ask_llm()         (existing Azure OpenAI GPT-4o mini)
  - Database        →  cart_db helpers       (new tables, same MySQL)
  - Threading       →  Python stdlib         (no queue worker or Celery needed)
"""
import os
import threading
import time as _time
import json as _json
import os as _os

import requests as _requests

from billing import send_email
from llm import ask_llm
from cart_db import (
    mark_queue_status,
    increment_touches,
    log_recovery_action,
    get_queue_row,
)

_WA_GATEWAY_URL = os.getenv("WA_GATEWAY_URL", "").rstrip("/")


def _send_wa_cart_recovery(
    api_key: str,
    session_id: str,
    cart_items: list | None,
    cart_value: float | None,
    cart_url: str,
) -> None:
    """
    Best-effort call to whatsapp-gateway /wa-cart-recovery.
    Only fires when session_id starts with 'wa-meta-' and WA_GATEWAY_URL is set.
    Never raises — failures are logged and ignored so the email flow continues.
    """
    if not _WA_GATEWAY_URL:
        return
    if not (session_id or "").startswith("wa-meta-"):
        return
    try:
        r = _requests.post(
            f"{_WA_GATEWAY_URL}/wa-cart-recovery",
            json={
                "api_key":    api_key,
                "session_id": session_id,
                "cart_items": cart_items,
                "cart_value": cart_value,
                "cart_url":   cart_url,
            },
            timeout=10,
        )
        print(f"   📱 WA cart recovery: status={r.status_code} body={r.text[:80]}")
    except Exception as e:
        print(f"   ⚠️ WA cart recovery call failed: {e}")

# ── Timing for the 3-touch sequence ──────────────────────────────────────────
# These can be overridden via .env for testing (e.g. set to 60 on staging).
_TOUCH_2_DELAY_SEC   = int(os.getenv("CART_TOUCH2_DELAY_SEC",   str(2  * 60 * 60)))   # 2 hours
_TOUCH_3_DELAY_SEC   = int(os.getenv("CART_TOUCH3_DELAY_SEC",   str(24 * 60 * 60)))   # 24 hours


# ─────────────────────────────────────────────────────────────────────────────
# AI EMAIL GENERATION
# ─────────────────────────────────────────────────────────────────────────────

def generate_recovery_email_ai(
    cart_items: list | None,
    cart_value: float | None,
    store_name: str,
    store_url: str,
    incentive_pct: int = 0,
    is_final: bool = False,
) -> tuple[str, str]:
    """
    Use GPT-4o mini to write a personalised cart recovery email.

    Returns:
        (subject: str, html_body: str)

    Falls back to a simple template if AI generation fails so the email
    is always sent even if the LLM call fails.
    """
    # Build a readable description of cart contents
    items_desc = ""
    if cart_items:
        try:
            names = []
            for item in (cart_items if isinstance(cart_items, list) else []):
                name = item.get("name") or item.get("title") or ""
                if name:
                    names.append(name)
            items_desc = ", ".join(names[:5])
        except Exception:
            items_desc = ""

    cart_value_str = f"£{cart_value:.2f}" if cart_value else "unknown"

    incentive_block = ""
    if incentive_pct and int(incentive_pct) > 0:
        discount_code = f"COMEBACK{incentive_pct}"
        incentive_block = (
            f"\n- Offer a {incentive_pct}% discount with code: {discount_code}"
            f"\n- Make the discount code prominent."
        )

    final_block = ""
    if is_final:
        final_block = (
            "\n- This is the LAST email in the sequence. Create gentle urgency: "
            "the customer's saved cart expires in 24 hours."
        )

    prompt = (
        f"Write a warm, personalised cart recovery email for a WooCommerce store.\n\n"
        f"Store name: {store_name}\n"
        f"Store URL: {store_url}\n"
        f"Items left in cart: {items_desc or 'some items'}\n"
        f"Cart value: {cart_value_str}\n\n"
        f"Requirements:\n"
        f"- Friendly, conversational tone — not robotic or generic.\n"
        f"- Remind the customer what they left behind by name.\n"
        f"- Include a clear 'Return to Cart' call-to-action link: {store_url}/cart\n"
        f"- Keep the body under 180 words.\n"
        f"- Use clean, inline-styled HTML (no external CSS or <style> blocks).\n"
        f"{incentive_block}{final_block}\n\n"
        f"Respond in EXACTLY this format (two lines, then the rest is HTML):\n"
        f"SUBJECT: <subject line here>\n"
        f"HTML: <complete HTML body here>"
    )

    try:
        # Use a longer output limit for email generation
        import os as _os
        original_limit = _os.getenv("LLM_MAX_OUTPUT_TOKENS")
        _os.environ["LLM_MAX_OUTPUT_TOKENS"] = "600"

        answer, _needs_handoff, _usage = ask_llm(
            system_prompt=(
                "You are an expert ecommerce email copywriter. "
                "Write concise, warm, conversion-focused cart abandonment emails. "
                "Always use inline HTML styles. Never use Markdown."
            ),
            user_message=prompt,
            context_chunks=[],
            history=[],
        )

        # Restore original token limit
        if original_limit is not None:
            _os.environ["LLM_MAX_OUTPUT_TOKENS"] = original_limit
        else:
            _os.environ.pop("LLM_MAX_OUTPUT_TOKENS", None)

    except Exception as e:
        print("⚠️ generate_recovery_email_ai LLM call failed:", e)
        return (
            _fallback_subject(store_name, is_final),
            _fallback_html(store_name, store_url, items_desc, cart_value, incentive_pct, is_final),
        )

    # ── Parse the structured response ────────────────────────────────────────
    subject = ""
    html_body = ""
    try:
        lines = (answer or "").strip().split("\n")
        html_lines: list[str] = []
        in_html = False

        for line in lines:
            if not in_html and line.startswith("SUBJECT:"):
                subject = line[len("SUBJECT:"):].strip()
            elif not in_html and line.startswith("HTML:"):
                in_html = True
                rest = line[len("HTML:"):].strip()
                if rest:
                    html_lines.append(rest)
            elif in_html:
                html_lines.append(line)

        html_body = "\n".join(html_lines).strip()
    except Exception as parse_err:
        print("⚠️ parse recovery email response failed:", parse_err)

    # Fall back gracefully if parsing produced nothing usable
    if not subject:
        subject = _fallback_subject(store_name, is_final)
    if not html_body or len(html_body) < 50:
        html_body = _fallback_html(store_name, store_url, items_desc, cart_value, incentive_pct, is_final)

    return subject, html_body


def _fallback_subject(store_name: str, is_final: bool) -> str:
    if is_final:
        return f"Last chance — your cart at {store_name} expires soon"
    return f"You left something behind at {store_name}"


def _fallback_html(
    store_name: str,
    store_url: str,
    items_desc: str,
    cart_value: float | None,
    incentive_pct: int,
    is_final: bool,
) -> str:
    """Minimal inline-styled HTML fallback email."""
    cart_url = f"{store_url.rstrip('/')}/cart"
    value_line = f"<p style=\"margin:0 0 12px;\">Cart value: <strong>£{cart_value:.2f}</strong></p>" if cart_value else ""
    discount_code = f"COMEBACK{incentive_pct}"
    incentive_line = (
        f"<p style=\"margin:0 0 12px; color:#059669; font-weight:bold;\">"
        f"Use code <strong>{discount_code}</strong> for {incentive_pct}% off your order!</p>"
    ) if incentive_pct and int(incentive_pct) > 0 else ""
    urgency_line = (
        "<p style=\"margin:0 0 12px; color:#dc2626; font-weight:bold;\">⏰ Your cart expires in 24 hours!</p>"
    ) if is_final else ""
    items_line = items_desc or "some items"

    return f"""<div style="font-family:Arial,sans-serif;max-width:580px;margin:0 auto;padding:32px 24px;background:#ffffff;">
  <h2 style="margin:0 0 16px;color:#030C18;font-size:22px;">You left something behind!</h2>
  <p style="margin:0 0 12px;color:#374151;">Hi there,</p>
  <p style="margin:0 0 12px;color:#374151;">
    You left <strong>{items_line}</strong> in your cart at <strong>{store_name}</strong>.
  </p>
  {value_line}
  {incentive_line}
  {urgency_line}
  <p style="margin:24px 0;">
    <a href="{cart_url}"
       style="display:inline-block;background:#030C18;color:#ffffff;padding:14px 28px;
              border-radius:8px;text-decoration:none;font-weight:bold;font-size:15px;">
      Return to My Cart &rarr;
    </a>
  </p>
  <p style="margin:0 0 8px;color:#6b7280;font-size:13px;">
    If you have any questions, just reply to this email and we'll be happy to help.
  </p>
  <p style="margin:0;color:#374151;font-size:13px;">— The {store_name} Team</p>
</div>"""


# ─────────────────────────────────────────────────────────────────────────────
# PLACEHOLDER RESOLVER
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_placeholders(
    template: str,
    store_name: str,
    store_url: str,
    cart_items: list | None,
    cart_value: float | None,
    incentive_pct: int,
) -> str:
    """
    Replace {{placeholder}} tokens in a custom email template with live values.

    Supported placeholders:
        {{store_name}}    — the store's display name
        {{store_url}}     — the store's base URL
        {{cart_url}}      — direct link to the shopper's cart  (store_url/cart)
        {{cart_items}}    — comma-separated product names
        {{cart_value}}    — formatted cart total (e.g. "£49.99")
        {{discount_code}} — recovery coupon code (e.g. "COMEBACK10"), or blank
    """
    # Build cart items string
    items_str = ""
    if cart_items:
        try:
            names = [
                item.get("name") or item.get("title") or ""
                for item in (cart_items if isinstance(cart_items, list) else [])
                if item.get("name") or item.get("title")
            ]
            items_str = ", ".join(names[:5])
        except Exception:
            items_str = ""
    if not items_str:
        items_str = "your items"

    value_str     = f"£{cart_value:.2f}" if cart_value else ""
    discount_str  = f"COMEBACK{incentive_pct}" if incentive_pct and int(incentive_pct) > 0 else ""
    cart_url      = f"{store_url.rstrip('/')}/cart"

    result = template
    result = result.replace("{{store_name}}",    store_name)
    result = result.replace("{{store_url}}",     store_url)
    result = result.replace("{{cart_url}}",      cart_url)
    result = result.replace("{{cart_items}}",    items_str)
    result = result.replace("{{cart_value}}",    value_str)
    result = result.replace("{{discount_code}}", discount_str)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# INDIVIDUAL TOUCH SENDERS
# ─────────────────────────────────────────────────────────────────────────────

def send_touch_1_popup(queue_id: int) -> None:
    """
    Touch 1: Mark the queue entry 'in_progress'.
    The widget JS polls /check-recovery and shows a popup on the next page load.
    No delay — fires immediately when the recovery sequence is triggered.
    """
    mark_queue_status(queue_id, "in_progress")
    increment_touches(queue_id)
    log_recovery_action(
        queue_id=queue_id,
        action_type="popup_queued",
        channel="widget",
        message_preview="Recovery popup queued — shown on next site visit",
    )
    print(f"   🛒 Touch 1 (popup) queued — queue_id={queue_id}")


def send_touch_2_email(
    queue_id: int,
    to_email: str,
    store_name: str,
    store_url: str,
    cart_items: list | None,
    cart_value: float | None,
    incentive_pct: int = 0,
    custom_subject: str = "",
    custom_html: str = "",
) -> None:
    """Touch 2: AI-personalised recovery email at T+2 hours.

    If custom_subject and custom_html are non-empty (set by the store owner
    via the portal's Email Template editor), they are used directly and the
    AI generation step is skipped entirely.  Placeholders in the custom
    template (e.g. {{store_name}}, {{cart_items}}, {{discount_code}}) are
    resolved before sending.
    """
    try:
        if custom_subject and custom_html:
            # Use the store owner's custom template — resolve placeholders
            subject   = _resolve_placeholders(
                custom_subject, store_name, store_url,
                cart_items, cart_value, incentive_pct
            )
            html_body = _resolve_placeholders(
                custom_html, store_name, store_url,
                cart_items, cart_value, incentive_pct
            )
        else:
            subject, html_body = generate_recovery_email_ai(
                cart_items=cart_items,
                cart_value=cart_value,
                store_name=store_name,
                store_url=store_url,
                incentive_pct=incentive_pct,
                is_final=False,
            )
        text_body = (
            f"You left items in your cart at {store_name}. "
            f"Visit {store_url.rstrip('/')}/cart to complete your purchase."
        )
        send_email(to_email=to_email, subject=subject, html_body=html_body, text_body=text_body)
        increment_touches(queue_id)
        log_recovery_action(
            queue_id=queue_id,
            action_type="email_sent",
            channel="email",
            message_preview=f"Subject: {subject[:100]}",
        )
        print(f"   📧 Touch 2 (email) sent to {to_email} — queue_id={queue_id}")
    except Exception as e:
        print(f"   ⚠️ send_touch_2_email failed for queue_id={queue_id}: {e}")


def send_touch_3_email(
    queue_id: int,
    to_email: str,
    store_name: str,
    store_url: str,
    cart_items: list | None,
    cart_value: float | None,
    incentive_pct: int = 0,
    custom_subject: str = "",
    custom_html: str = "",
) -> None:
    """Touch 3: Final urgency email at T+24 hours.

    Supports a custom template identical to send_touch_2_email.
    """
    try:
        if custom_subject and custom_html:
            subject   = _resolve_placeholders(
                custom_subject, store_name, store_url,
                cart_items, cart_value, incentive_pct
            )
            html_body = _resolve_placeholders(
                custom_html, store_name, store_url,
                cart_items, cart_value, incentive_pct
            )
        else:
            subject, html_body = generate_recovery_email_ai(
                cart_items=cart_items,
                cart_value=cart_value,
                store_name=store_name,
                store_url=store_url,
                incentive_pct=incentive_pct,
                is_final=True,
            )
        text_body = (
            f"Last chance! Your saved cart at {store_name} is about to expire. "
            f"Visit {store_url.rstrip('/')}/cart to complete your purchase."
        )
        send_email(to_email=to_email, subject=subject, html_body=html_body, text_body=text_body)
        increment_touches(queue_id)
        log_recovery_action(
            queue_id=queue_id,
            action_type="final_email_sent",
            channel="email",
            message_preview=f"Subject: {subject[:100]}",
        )
        print(f"   📧 Touch 3 (final email) sent to {to_email} — queue_id={queue_id}")
    except Exception as e:
        print(f"   ⚠️ send_touch_3_email failed for queue_id={queue_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND SEQUENCE RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def _is_recovered_or_expired(queue_id: int) -> bool:
    """Return True if the queue entry has left the active recovery window."""
    row = get_queue_row(queue_id)
    if row is None:
        return True
    return row.get("status") in ("recovered", "expired")


def _run_email_sequence(
    queue_id: int,
    to_email: str,
    store_name: str,
    store_url: str,
    cart_items: list | None,
    cart_value: float | None,
    incentive_pct: int,
    custom_t2_subject: str = "",
    custom_t2_html: str = "",
    custom_t3_subject: str = "",
    custom_t3_html: str = "",
    wa_api_key: str = "",
    wa_session_id: str = "",
) -> None:
    """
    Background thread target: sends Touch 2 (email) and Touch 3 (final email)
    with the configured delays.
    Exits early if the cart is recovered or expired.

    custom_t2_* / custom_t3_* are forwarded to the send functions so they
    can use the store owner's custom template instead of AI generation.

    This runs as a daemon thread — it will be killed if the server process stops.
    On staging that is acceptable; production deployments should upgrade to a
    persistent task queue once scale demands it.
    """
    # ── Touch 2: T+2 hours ───────────────────────────────────────────────────
    _time.sleep(_TOUCH_2_DELAY_SEC)

    if _is_recovered_or_expired(queue_id):
        print(f"   ✅ queue_id={queue_id} recovered/expired — skipping Touch 2 email")
        return

    # Only send email touches when a real email address is available.
    # Guest shoppers who never provided an email skip T2 and T3 entirely —
    # the touches are NOT logged and NOT counted so the dashboard stays accurate.
    if to_email and to_email.strip():
        send_touch_2_email(
            queue_id=queue_id,
            to_email=to_email,
            store_name=store_name,
            store_url=store_url,
            cart_items=cart_items,
            cart_value=cart_value,
            incentive_pct=incentive_pct,
            custom_subject=custom_t2_subject,
            custom_html=custom_t2_html,
        )
    else:
        print(f"   ℹ️ Touch 2 skipped — no email address for queue_id={queue_id}")

    # WhatsApp Touch 2 — fires alongside the email (or on its own for WA-only customers)
    if wa_session_id and wa_api_key:
        _send_wa_cart_recovery(
            api_key=wa_api_key,
            session_id=wa_session_id,
            cart_items=cart_items,
            cart_value=cart_value,
            cart_url=f"{store_url.rstrip('/')}/cart",
        )

    # ── Touch 3: T+24 hours (wait the remaining difference from T+2) ─────────
    remaining = _TOUCH_3_DELAY_SEC - _TOUCH_2_DELAY_SEC
    if remaining > 0:
        _time.sleep(remaining)

    if _is_recovered_or_expired(queue_id):
        print(f"   ✅ queue_id={queue_id} recovered/expired — skipping Touch 3 email")
        return

    if to_email and to_email.strip():
        send_touch_3_email(
            queue_id=queue_id,
            to_email=to_email,
            store_name=store_name,
            store_url=store_url,
            cart_items=cart_items,
            cart_value=cart_value,
            incentive_pct=incentive_pct,
            custom_subject=custom_t3_subject,
            custom_html=custom_t3_html,
        )
    else:
        print(f"   ℹ️ Touch 3 skipped — no email address for queue_id={queue_id}")

    # Auto-expire the entry after the full sequence if not already recovered
    if not _is_recovered_or_expired(queue_id):
        mark_queue_status(queue_id, "expired")
        log_recovery_action(
            queue_id=queue_id,
            action_type="sequence_expired",
            channel="system",
            message_preview="Full 48-hour recovery window elapsed without conversion",
        )
        print(f"   ⏰ queue_id={queue_id} — recovery sequence complete, marked expired")


def start_recovery_sequence(
    queue_id: int,
    to_email: str | None,
    store_name: str,
    store_url: str,
    cart_items: list | None,
    cart_value: float | None,
    incentive_pct: int = 0,
    custom_t2_subject: str = "",
    custom_t2_html: str = "",
    custom_t3_subject: str = "",
    custom_t3_html: str = "",
    wa_api_key: str = "",
    wa_session_id: str = "",
) -> None:
    """
    Entry point called from main.py when an abandonment is detected.

    Touch 1  (popup)            — fires immediately (synchronous)
    Touch 2  (email + WA T+2hr) — background thread
    Touch 3  (email, T+24hr)    — background thread

    wa_api_key / wa_session_id: when provided and session_id starts with
    'wa-meta-', a WhatsApp template message is also sent at Touch 2.
    Leave empty to skip WhatsApp (email-only customers).

    custom_t2_* / custom_t3_*: pass the store owner's saved HTML template and
    subject so the background thread can use them instead of AI generation.
    Leave empty strings to use AI generation (default behaviour).
    """
    # Touch 1 — fire immediately (synchronous)
    send_touch_1_popup(queue_id)

    # Touches 2 & 3 — background thread
    t = threading.Thread(
        target=_run_email_sequence,
        kwargs={
            "queue_id":          queue_id,
            "to_email":          to_email or "",
            "store_name":        store_name,
            "store_url":         store_url,
            "cart_items":        cart_items,
            "cart_value":        cart_value,
            "incentive_pct":     incentive_pct,
            "custom_t2_subject": custom_t2_subject,
            "custom_t2_html":    custom_t2_html,
            "custom_t3_subject": custom_t3_subject,
            "custom_t3_html":    custom_t3_html,
            "wa_api_key":        wa_api_key,
            "wa_session_id":     wa_session_id,
        },
        daemon=True,
        name=f"phixtra-recovery-{queue_id}",
    )
    t.start()
    using_custom = "custom" if (custom_t2_html or custom_t3_html) else "AI-generated"
    wa_note = f" wa_session={wa_session_id}" if wa_session_id else ""
    print(
        f"   🚀 Recovery sequence started — queue_id={queue_id} "
        f"email={to_email} templates={using_custom} "
        f"email_at={_TOUCH_2_DELAY_SEC}s "
        f"final_at={_TOUCH_3_DELAY_SEC}s"
        f"{wa_note}"
    )
