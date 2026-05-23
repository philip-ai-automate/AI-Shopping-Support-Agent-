import httpx

_GRAPH_BASE = "https://graph.facebook.com/v19.0"

# Default Meta template names — tenants must create & approve these in Meta Business Manager.
# Tenants can override via wa_templates table (Phase 4 portal will expose this as a setting).
DEFAULT_TEMPLATES = {
    "cart_recovery": "phixtra_cart_recovery",
    "order_update":  "phixtra_order_update",
}

# Expected template parameter conventions (document for tenants):
#
# phixtra_cart_recovery body params:
#   {{1}} cart items summary (e.g. "Nike Air Max, Adidas Ultraboost")
#   {{2}} cart URL          (e.g. "https://store.com/cart")
#
# phixtra_order_update body params:
#   {{1}} order ID          (e.g. "ORDER-1234")
#   {{2}} order status      (e.g. "shipped", "delivered")


async def send_template(
    phone_number_id: str,
    access_token: str,
    to: str,
    template_name: str,
    language_code: str,
    body_params: list[str],
) -> bool:
    """
    Send a Meta-approved template message to a customer.

    body_params: list of text strings for {{1}}, {{2}}, ... in the template body.
    The customer must have messaged first OR the template must be approved for
    outbound messages (Marketing/Utility category in Meta Business Manager).
    """
    components = []
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components,
        },
    }

    url = f"{_GRAPH_BASE}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code == 200:
                print(f"✅ [TEMPLATE] sent template={template_name} to={to}")
                return True
            print(f"⚠️ [TEMPLATE] failed status={r.status_code} body={r.text[:300]}")
            if r.status_code == 401:
                print(f"⚠️ [TEMPLATE] Access token expired for phone_number_id={phone_number_id}")
            return False
    except Exception as e:
        print(f"⚠️ [TEMPLATE] exception to={to}: {e}")
        return False
