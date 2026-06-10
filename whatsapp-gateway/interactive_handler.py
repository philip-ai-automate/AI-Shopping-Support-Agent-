import re

from meta_sender import send_text, send_checkout_message, send_image_with_caption, send_interactive_list
from wa_db import get_cached_product, get_session_products, mark_product_viewed, get_document_for_product
from currency import fmt_ngn


# ── Grade labels and what they mean ───────────────────────────────────────────
_GRADE_INFO = {
    "excellent":  "Looks almost new — no visible scratches on screen or body.",
    "very good":  "Light signs of use — screen is perfect, minor marks on body only.",
    "good":       "Some visible wear on the body — screen is clear and all functions work perfectly.",
    "fair":       "Visible wear on body and possibly screen — fully functional, best value option.",
}


def _parse_product_name(name: str) -> dict:
    """
    Extract color, storage, grade, condition and short model name from a
    WooCommerce variant product name.

    Handles formats:
      "Apple iPhone 12 - [UK Used] - black / 256gb / very-good"
      "Apple iPhone 12 Pro Max - [UK Used] - pacific-blue / very-good / 128gb"
      "Apple iPhone 11 [UK Used] - white / excellent / 256gb"
      "Apple iPhone 13 128gb [UK Used] Midnight"
    """
    result = {"short_name": name, "color": "", "storage": "", "grade": "", "condition": "UK Used"}

    # Condition from inside brackets
    cond = re.search(r'\[([^\]]+)\]', name)
    if cond:
        result["condition"] = cond.group(1).strip()

    if " - " in name:
        segments = [s.strip() for s in name.split(" - ")]
        # Clean brackets from short_name (e.g. "Apple iPhone 11 [UK Used]" → "Apple iPhone 11")
        result["short_name"] = re.sub(r'\s*\[[^\]]*\]', '', segments[0]).strip()
        # Last segment holds the variant details — skip if it's just a bracket condition
        variant_str = segments[-1] if not segments[-1].startswith("[") else ""
        if variant_str:
            parts = [p.strip() for p in variant_str.split("/")]
            for p in parts:
                p_clean = p.replace("-", " ").strip()
                if re.search(r'\d+\s*[gt]b', p, re.IGNORECASE):
                    result["storage"] = re.sub(r'\s+', '', p).upper()
                elif p_clean.lower() in {"very good", "excellent", "good", "fair"}:
                    result["grade"] = p_clean.title()
                elif p_clean:
                    result["color"] = p_clean.title()
    else:
        # No " - " — strip condition and trailing text to get base model name
        result["short_name"] = re.sub(r'\s*\[[^\]]*\].*$', '', name).strip()
        # Extract storage anywhere in the name
        s_match = re.search(r'(\d+\s*[gt]b)', name, re.IGNORECASE)
        if s_match:
            result["storage"] = re.sub(r'\s+', '', s_match.group(1)).upper()
        # Color is the last word after stripping known tokens
        remainder = re.sub(r'\s*\[[^\]]*\]', '', name)
        remainder = re.sub(r'\d+\s*[gt]b', '', remainder, flags=re.IGNORECASE)
        words = remainder.split()
        # Drop words that are part of the base model name
        model_words = set(result["short_name"].split())
        color_candidates = [w for w in words if w not in model_words and len(w) > 2]
        if color_candidates:
            result["color"] = color_candidates[-1].title()

    return result


def _build_product_detail_message(
    name: str,
    price: str,
    in_stock: bool,
    url: str,
    description: str,
) -> str:
    """
    Build a rich, persuasive WhatsApp product detail message designed for
    Nigerian buyers of UK Used electronics.
    """
    parsed = _parse_product_name(name)
    short_name   = parsed["short_name"] or name
    color        = parsed["color"]
    storage      = parsed["storage"]
    grade        = parsed["grade"]
    condition    = parsed["condition"]

    grade_key  = grade.lower()
    grade_blurb = _GRADE_INFO.get(grade_key, "")

    stock_line = "✅ In Stock — ready to ship" if in_stock else "⚠️ Currently out of stock"

    # Build spec line
    specs = []
    if color:
        specs.append(f"🎨 Colour: *{color}*")
    if storage:
        specs.append(f"💾 Storage: *{storage}*")
    if grade:
        specs.append(f"⭐ Grade: *{grade}*")
    if condition:
        specs.append(f"🏷️ Condition: *{condition}*")
    spec_block = "\n".join(specs)

    # Grade explanation block
    grade_block = f"\n📋 *What \"{grade}\" means:*\n_{grade_blurb}_\n" if grade_blurb else ""

    # Description block
    desc_block = f"\n{description.strip()}\n" if description and description.strip() else ""

    # Key buying points — relevant for Nigerian customers buying UK phones
    buying_points = (
        "📦 *Why buy this phone:*\n"
        "✅ Genuine original Apple hardware\n"
        "✅ Factory unlocked — works on *MTN, Airtel, Glo & 9mobile*\n"
        "✅ Fully tested: battery, cameras, Face ID, speakers & sensors\n"
        "✅ Sourced from the UK — high quality pre-owned market\n"
    )

    # CTA
    cta = (
        "💬 *Ready to order?*\n"
        "Reply *ORDER* to buy now\n"
        "Reply *DISCOUNT* to ask for a discount\n"
        "or just ask — I'm here to help! 🙌"
    )

    parts = [
        f"*{short_name}*",
        "",
        spec_block,
        "",
        f"💰 *Price: {fmt_ngn(price)}*",
        stock_line,
        grade_block,
        desc_block,
        buying_points,
        f"🔗 Full details & photos:\n{url}" if url else None,
        "",
        "─────────────────",
        cta,
    ]

    return "\n".join(p for p in parts if p is not None)


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


async def handle_list_select(
    phone_number_id: str,
    access_token: str,
    customer_phone: str,
    product_id: str,
    session_id: str,
) -> bool:
    """
    Customer selected a product from the interactive list.
    Sends a rich product detail message, the product image, then the list again.
    Returns True if handled, False to fall through to AI.
    """
    product = get_cached_product(session_id, product_id)
    if not product:
        print(f"⚠️ [INTERACTIVE] list_select: product_id={product_id} not in cache for session={session_id}")
        return False

    name        = product.get("product_name") or "Product"
    price       = product.get("price") or ""
    url         = product.get("product_url") or ""
    in_stock    = product.get("in_stock", True)
    description = (product.get("description") or "").strip()

    # Try to get image from documents table (cache image_url is often empty)
    image_url = product.get("image_url") or ""
    if not image_url:
        doc = get_document_for_product(product_id)
        if doc:
            image_url = doc.get("image_url") or ""

    if not (url or price or description or image_url):
        return False

    mark_product_viewed(session_id, product_id)

    # Build rich detail message
    detail_text = _build_product_detail_message(name, price, in_stock, url, description)
    await send_text(phone_number_id, access_token, customer_phone, detail_text)

    # Send product image (best-effort, after the text so text arrives first)
    if image_url:
        sent = await send_image_with_caption(
            phone_number_id, access_token, customer_phone, image_url,
            (product.get("product_name") or "").split(" - ")[0].strip(),
        )
        if not sent:
            print(f"⚠️ [INTERACTIVE] image send failed for product_id={product_id}")

    # Re-display the list so customer can compare other options
    session_prods = get_session_products(session_id)
    if session_prods:
        list_products = [
            {
                "product_id": p["product_id"],
                "name":       p["product_name"],
                "price":      p["price"],
                "in_stock":   p["in_stock"],
                "related":    p["is_related"],
            }
            for p in session_prods
        ]
        await send_interactive_list(
            phone_number_id, access_token, customer_phone, list_products,
            body_text="Compare with other options — tap to view:",
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
