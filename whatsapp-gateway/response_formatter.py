from meta_sender import send_text, send_product_image
from currency import fmt_ngn
from wa_db import get_document_for_product

# Free-text keywords matched against documents.categories_text (whatever the
# merchant's own store calls its categories — WooCommerce category names,
# CSV category column, etc). Covers the 8 Apparel & Fashion categories in the
# portal's catalogue taxonomy plus common Nigerian apparel terms.
_FASHION_KEYWORDS = (
    "cloth", "apparel", "wear", "dress", "footwear", "shoe", "sneaker",
    "bag", "jewel", "fashion", "ankara", "aso ebi", "aso-ebi", "agbada",
    "kaftan", "senator", "fabric", "outfit",
)


def _is_fashion_category(categories_text: str) -> bool:
    text = (categories_text or "").lower()
    return any(kw in text for kw in _FASHION_KEYWORDS)


async def _maybe_send_fashion_image(
    phone_number_id: str, access_token: str, to: str, product: dict
) -> None:
    """
    Best-effort: if this single recommended product is Apparel & Fashion,
    attach its photo inline so the customer doesn't have to tap through the
    list first. Silently does nothing for every other category, and does
    nothing if the merchant hasn't uploaded a photo for the product.
    """
    product_id      = str(product.get("product_id") or product.get("id") or "")
    image_url       = product.get("image_url") or ""
    categories_text = product.get("categories_text") or ""

    if product_id and not (image_url and categories_text):
        doc = get_document_for_product(product_id)
        if doc:
            image_url       = image_url or doc.get("image_url") or ""
            categories_text = categories_text or doc.get("categories_text") or ""

    if not image_url or not _is_fashion_category(categories_text):
        return

    name = (product.get("name") or product.get("product_name") or "").split(" - ")[0].strip()
    sent = await send_product_image(phone_number_id, access_token, to, image_url, name)
    if not sent:
        print(f"⚠️ [DISPATCH] fashion image send failed for product_id={product_id}")


def _format_product_list_text(products: list) -> str:
    """
    Plain numbered product list — replaces the old WhatsApp interactive list.
    No 24-character title limit here, so the full product name is always
    readable, and the customer can select by replying with the number
    (see the numbered-selection handling in meta_webhook.py).
    """
    lines = []
    for i, p in enumerate(products, start=1):
        name     = (p.get("name") or p.get("product_name") or "").strip()
        price    = fmt_ngn(str(p.get("price") or ""))
        in_stock = p.get("in_stock", True)
        price_part = f" — {price}" if price else ""
        stock_note = "" if in_stock else " (Out of stock)"
        lines.append(f"{i}. {name}{price_part}{stock_note}")
    lines.append("")
    lines.append('Type a number to see more, or to Order type ORDER and the product number. Example ORDER 2')
    return "\n".join(lines)


async def dispatch_response(
    phone_number_id: str,
    access_token: str,
    to: str,
    reply: str,
    products: list,
    session_id: str = "",
) -> None:
    """
    Send the AI reply and any product recommendations to the customer.

    Flow:
      always send the AI text reply (if there is one), then a plain numbered
      list of any product recommendations, in ONE combined text message —
      no WhatsApp interactive list (its 24-char title limit made same-model
      variants indistinguishable). The customer picks by replying with a
      number; meta_webhook.py resolves that against the cached session
      products (same lookup used for detail/order) and either shows the
      full detail message or starts an order.
      1 product, Apparel & Fashion category → additionally send the product
                     photo inline before the text (see _maybe_send_fashion_image).
                     All other categories are unaffected.
    """
    if products and len(products) == 1:
        await _maybe_send_fashion_image(phone_number_id, access_token, to, products[0])

    parts = []
    if reply:
        parts.append(reply)
    if products:
        parts.append(_format_product_list_text(products))

    combined = "\n\n".join(parts)
    if combined:
        await send_text(phone_number_id, access_token, to, combined)
