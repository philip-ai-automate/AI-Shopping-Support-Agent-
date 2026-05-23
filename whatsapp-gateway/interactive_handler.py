from meta_sender import send_text, send_checkout_message
from wa_db import get_cached_product


async def handle_addcart(
    phone_number_id: str,
    access_token: str,
    customer_phone: str,
    product_id: str,
    session_id: str,
) -> bool:
    """
    Customer tapped "Add to Cart". Look up the cart URL from the product cache
    and send a checkout confirmation message.

    Returns True if handled (product found in cache), False to fall through to AI.
    """
    product = get_cached_product(session_id, product_id)
    if not product or not product.get("cart_url"):
        print(f"⚠️ [INTERACTIVE] addcart: product_id={product_id} not in cache for session={session_id}")
        return False

    await send_checkout_message(
        phone_number_id=phone_number_id,
        access_token=access_token,
        to=customer_phone,
        product_name=product.get("product_name") or "Your selected product",
        cart_url=product["cart_url"],
        price=product.get("price") or "",
    )
    return True


async def handle_details(
    phone_number_id: str,
    access_token: str,
    customer_phone: str,
    product_id: str,
    session_id: str,
) -> bool:
    """
    Customer tapped "View Details". Look up the product page URL from cache
    and send it as a plain text message.

    Returns True if handled, False to fall through to AI.
    """
    product = get_cached_product(session_id, product_id)
    if not product or not product.get("product_url"):
        print(f"⚠️ [INTERACTIVE] details: product_id={product_id} not in cache for session={session_id}")
        return False

    name = product.get("product_name") or "Product"
    text = f"Here's the full details page for {name}:\n{product['product_url']}"
    await send_text(phone_number_id, access_token, customer_phone, text)
    return True
