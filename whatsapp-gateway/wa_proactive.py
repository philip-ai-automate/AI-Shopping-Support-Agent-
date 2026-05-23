"""
wa_proactive.py — Proactive WhatsApp message endpoints.

POST /wa-cart-recovery  — Send a cart abandonment template message.
                          Called by ai-backend's cart recovery background thread
                          when the customer's session is a WhatsApp session.

POST /wa-order-update   — Send an order status update template message.
                          Called by the WooCommerce plugin when an order status changes.

Both endpoints use Meta-approved message templates. Templates must be created
and approved in Meta Business Manager before use. See template_sender.py for
the expected parameter conventions.
"""

from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel

from tenant_router import get_tenant_by_phone_number_id, get_tenant_by_api_key
from template_sender import send_template, DEFAULT_TEMPLATES
from wa_db import get_wa_template, log_proactive

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_wa_session(session_id: str) -> tuple[str, str] | None:
    """
    Extract (phone_number_id, customer_phone) from a wa-meta session ID.
    Returns None for non-WhatsApp sessions (widget-, cw- etc.).
    """
    prefix = "wa-meta-"
    if not session_id.startswith(prefix):
        return None
    remainder = session_id[len(prefix):]
    # Format: {phone_number_id}-{customer_phone}
    # Both are numeric — split on first hyphen
    parts = remainder.split("-", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def _build_cart_summary(cart_items: list | None, cart_value: float | None) -> str:
    names = []
    if cart_items:
        for item in cart_items[:3]:
            name = item.get("name") or item.get("title") or ""
            if name:
                names.append(name)
    summary = ", ".join(names) if names else "your items"
    if cart_value:
        summary += f" (£{cart_value:.2f})"
    return summary


# ── /wa-cart-recovery ─────────────────────────────────────────────────────────

class CartRecoveryRequest(BaseModel):
    api_key:       str
    session_id:    str
    cart_items:    Optional[list]  = None
    cart_value:    Optional[float] = None
    cart_url:      Optional[str]   = None
    customer_name: Optional[str]   = None


@router.post("/wa-cart-recovery")
async def wa_cart_recovery(req: CartRecoveryRequest):
    """
    Send a WhatsApp cart abandonment template message.

    Called by ai-backend's cart_recovery.py from the background recovery thread
    when the session_id starts with 'wa-meta-', indicating a WhatsApp customer.

    The template (phixtra_cart_recovery or tenant-configured) receives:
      {{1}} — cart summary (item names + value)
      {{2}} — cart URL
    """
    parsed = _parse_wa_session(req.session_id)
    if not parsed:
        return {"status": "skipped", "reason": "not_a_whatsapp_session"}

    phone_number_id, customer_phone = parsed

    tenant = get_tenant_by_phone_number_id(phone_number_id)
    if not tenant:
        return {"status": "skipped", "reason": "no_tenant_for_phone_number_id"}

    # Verify the api_key matches this tenant
    if tenant["phixtra_api_key"] != req.api_key:
        return {"status": "error", "reason": "api_key_mismatch"}

    tenant_id    = int(tenant["tenant_id"])
    access_token = tenant["access_token"]

    # Resolve template: tenant-configured or default
    tpl = get_wa_template(tenant_id, "cart_recovery")
    template_name = tpl["template_name"] if tpl else DEFAULT_TEMPLATES["cart_recovery"]
    language_code = tpl["language_code"] if tpl else "en"

    cart_summary = _build_cart_summary(req.cart_items, req.cart_value)
    cart_url     = (req.cart_url or "").strip() or "your store cart"

    ok = await send_template(
        phone_number_id=phone_number_id,
        access_token=access_token,
        to=customer_phone,
        template_name=template_name,
        language_code=language_code,
        body_params=[cart_summary, cart_url],
    )

    log_proactive(
        tenant_id=tenant_id,
        phone_number_id=phone_number_id,
        customer_phone=customer_phone,
        event_type="cart_recovery",
        template_name=template_name,
        status="sent" if ok else "failed",
        notes=f"cart_url={cart_url} items={cart_summary[:80]}",
    )

    print(f"{'✅' if ok else '⚠️'} [PROACTIVE] cart_recovery session={req.session_id} ok={ok}")
    return {"status": "sent" if ok else "failed", "template": template_name}


# ── /wa-order-update ──────────────────────────────────────────────────────────

class OrderUpdateRequest(BaseModel):
    api_key:        str
    customer_phone: str
    order_id:       str
    order_status:   str
    store_name:     Optional[str] = None


@router.post("/wa-order-update")
async def wa_order_update(req: OrderUpdateRequest):
    """
    Send a WhatsApp order status update template message.

    Called by the WooCommerce plugin (or ai-backend /stock-back-in) when
    an order status changes to a notify-worthy state (e.g. 'shipped', 'delivered').

    The template (phixtra_order_update or tenant-configured) receives:
      {{1}} — order ID
      {{2}} — order status
    """
    tenant = get_tenant_by_api_key(req.api_key)
    if not tenant:
        return {"status": "error", "reason": "invalid_api_key"}

    tenant_id       = int(tenant["tenant_id"])
    phone_number_id = tenant["phone_number_id"]
    access_token    = tenant["access_token"]

    # Normalise customer phone — strip leading + for Meta (E.164 without +)
    customer_phone = req.customer_phone.strip().lstrip("+")

    tpl = get_wa_template(tenant_id, "order_update")
    template_name = tpl["template_name"] if tpl else DEFAULT_TEMPLATES["order_update"]
    language_code = tpl["language_code"] if tpl else "en"

    ok = await send_template(
        phone_number_id=phone_number_id,
        access_token=access_token,
        to=customer_phone,
        template_name=template_name,
        language_code=language_code,
        body_params=[req.order_id, req.order_status],
    )

    log_proactive(
        tenant_id=tenant_id,
        phone_number_id=phone_number_id,
        customer_phone=customer_phone,
        event_type="order_update",
        template_name=template_name,
        status="sent" if ok else "failed",
        notes=f"order_id={req.order_id} status={req.order_status}",
    )

    print(f"{'✅' if ok else '⚠️'} [PROACTIVE] order_update order={req.order_id} status={req.order_status} ok={ok}")
    return {"status": "sent" if ok else "failed", "template": template_name}
