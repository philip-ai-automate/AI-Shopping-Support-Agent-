from meta_sender import send_text, send_interactive_list, send_image_with_caption
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
    sent = await send_image_with_caption(phone_number_id, access_token, to, image_url, name)
    if not sent:
        print(f"⚠️ [DISPATCH] fashion image send failed for product_id={product_id}")


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
      no products  → send the AI text reply only
      1+ products  → send AI text reply, then show all products as an
                     interactive list ("Products for you" / "View Options").
                     When the customer selects a row, handle_list_select
                     in interactive_handler.py sends the rich detail card.
      1 product, Apparel & Fashion category → additionally send the product
                     photo inline before the list (see _maybe_send_fashion_image).
                     All other categories are unaffected.
    """
    list_products = [
        {
            "product_id": str(p.get("product_id") or p.get("id") or ""),
            "name":       p.get("name") or p.get("product_name") or "",
            "price":      fmt_ngn(str(p.get("price") or "")),
            "in_stock":   p.get("in_stock", True),
            "related":    p.get("related", False),
        }
        for p in products
    ] if products else []

    if list_products:
        # Products present — send only the interactive list, no text reply.
        # Sending both causes the customer to see the product list twice
        # (AI text + "Products for you" cards) which looks broken.
        if len(products) == 1:
            await _maybe_send_fashion_image(phone_number_id, access_token, to, products[0])
        await send_interactive_list(phone_number_id, access_token, to, list_products)
    elif reply:
        await send_text(phone_number_id, access_token, to, reply)
