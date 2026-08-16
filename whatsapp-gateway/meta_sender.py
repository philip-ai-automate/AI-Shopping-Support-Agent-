import asyncio
import io

import httpx
from PIL import Image

_GRAPH_BASE = "https://graph.facebook.com/v19.0"

_MAX_ATTEMPTS   = 3
_BACKOFF_BASE   = 2   # seconds — waits 2s then 4s between attempts
_BACKOFF_CAP    = 10  # never wait more than 10s
# Status codes worth retrying (transient server/rate-limit errors)
_RETRYABLE      = {429, 500, 502, 503, 504}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _trunc(s: str, max_len: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _split_variant_title(name: str) -> tuple:
    """
    Catalogue product titles follow "{Base Name} - {attr1} / {attr2} / ...}"
    (e.g. "Apple iPhone 11 [UK Used] - black / very-good / 128gb"). WhatsApp
    list rows only show 24 characters, which cuts off right before the
    attributes — every variant of the same product then looks identical.
    Returns (short_title, base_name): short_title leads with the distinguishing
    attributes (compacted so they fit); base_name goes in the description instead.
    """
    name = (name or "").strip()
    if " - " in name:
        base, _, attrs = name.rpartition(" - ")
        if "/" in attrs:
            parts = [p.strip() for p in attrs.split("/") if p.strip()]
            return "·".join(parts), base.strip()
    return name, ""


async def _send(phone_number_id: str, access_token: str, payload: dict) -> bool:
    """
    POST one message to the Meta Graph API with up to _MAX_ATTEMPTS tries.

    Retry on: network errors, 429 (rate-limited), 5xx (transient server errors).
    Bail immediately on: 400 (bad payload — retrying won't help),
                         401 (token expired — needs human action).
    """
    url     = f"{_GRAPH_BASE}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}"}

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json=payload, headers=headers)

            if r.status_code == 200:
                if attempt > 1:
                    print(f"✅ [META] send succeeded on attempt {attempt}/{_MAX_ATTEMPTS}")
                return True

            if r.status_code == 401:
                print(f"⚠️ [META] Access token expired for phone_number_id={phone_number_id}")
                return False  # no point retrying — needs reconnection

            if r.status_code == 400:
                print(f"⚠️ [META] Bad request (payload error) body={r.text[:300]}")
                return False  # payload is wrong — retrying won't fix it

            if r.status_code in _RETRYABLE:
                # Respect Retry-After header (Meta sends it on 429)
                wait = float(r.headers.get("Retry-After", _BACKOFF_BASE ** (attempt - 1)))
                wait = min(wait, _BACKOFF_CAP)
                print(f"⚠️ [META] status={r.status_code} attempt={attempt}/{_MAX_ATTEMPTS} — retry in {wait:.0f}s")
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(wait)
                continue

            # Any other non-retryable error (403, 404, etc.)
            print(f"⚠️ [META] send failed status={r.status_code} body={r.text[:200]}")
            return False

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            wait = min(_BACKOFF_BASE ** (attempt - 1), _BACKOFF_CAP)
            print(f"⚠️ [META] network error attempt={attempt}/{_MAX_ATTEMPTS}: {e} — retry in {wait:.0f}s")
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(wait)

        except Exception as e:
            print(f"⚠️ [META] unexpected send error: {e}")
            return False

    print(f"⚠️ [META] gave up after {_MAX_ATTEMPTS} attempts (phone_number_id={phone_number_id})")
    return False


# ── Read receipt + Typing indicator ──────────────────────────────────────────

async def mark_as_read(phone_number_id: str, access_token: str, message_id: str) -> None:
    """Mark the customer's inbound message as read (shows blue double ticks)."""
    url     = f"{_GRAPH_BASE}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, headers=headers, json={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
            })
    except Exception:
        pass  # fire-and-forget — never block the main response flow



# ── Approved template (works outside the 24h session window) ──────────────────

async def send_template(
    phone_number_id: str,
    access_token: str,
    to: str,
    template_name: str,
    language_code: str,
    body_params: list[str],
) -> bool:
    return await _send(phone_number_id, access_token, {
        "messaging_product": "whatsapp",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in body_params],
            }],
        },
    })


# ── Plain text ────────────────────────────────────────────────────────────────

async def send_text(
    phone_number_id: str,
    access_token: str,
    to: str,
    text: str,
) -> bool:
    return await _send(phone_number_id, access_token, {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    })


# ── Interactive list — product recommendations ────────────────────────────────

async def send_interactive_list(
    phone_number_id: str,
    access_token: str,
    to: str,
    products: list,
    body_text: str = "Here are some matches I found:",
) -> bool:
    """
    Send up to 10 products as a WhatsApp Interactive List Message.
    Meta limits: title ≤24 chars, description ≤72 chars, max 10 rows.
    """
    rows = []
    for p in products[:10]:
        product_id = str(p.get("product_id") or p.get("id") or "")
        name = p.get("name") or ""
        price = p.get("price") or ""
        in_stock = p.get("in_stock", True)

        variant_title, base_name = _split_variant_title(name)
        title = _trunc(variant_title, 24)
        desc_parts = [base_name] if base_name else []
        if price:
            desc_parts.append(price)
        desc_parts.append("In stock" if in_stock else "Out of stock")
        description = _trunc(" · ".join(desc_parts), 72)

        rows.append({
            "id": f"prod_{product_id}" if product_id else f"prod_{_trunc(name, 20)}",
            "title": title,
            "description": description,
        })

    return await _send(phone_number_id, access_token, {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Products for you"},
            "body": {"text": body_text},
            "footer": {"text": "Tap to select or type your question"},
            "action": {
                "button": "View Options",
                "sections": [{"title": "Products", "rows": rows}],
            },
        },
    })


# ── Interactive buttons — single product actions ──────────────────────────────

async def send_interactive_buttons(
    phone_number_id: str,
    access_token: str,
    to: str,
    product: dict,
) -> bool:
    """
    Send quick-reply buttons for a single product.
    Meta limits: ≤3 buttons, title ≤20 chars, button id ≤256 chars.
    """
    product_id = str(product.get("product_id") or product.get("id") or "")
    name = product.get("name") or "This product"
    price = product.get("price") or ""

    body = _trunc(name, 60)
    if price:
        body = f"{body} — {price}"
    body += "\n\nWhat would you like to do?"

    return await _send(phone_number_id, access_token, {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": f"addcart_{product_id}", "title": "Add to Cart"}},
                    {"type": "reply", "reply": {"id": f"details_{product_id}", "title": "View Details"}},
                    {"type": "reply", "reply": {"id": "more", "title": "More Options"}},
                ]
            },
        },
    })


# ── Product image with caption ───────────────────────────────────────────────

async def send_image_with_caption(
    phone_number_id: str,
    access_token: str,
    to: str,
    image_url: str,
    caption: str,
) -> bool:
    """Send a product image with a caption containing name, price, stock, and URL.

    Only reliable for images already in a WhatsApp-supported format (JPEG/PNG) —
    WhatsApp fetches image_url itself and rejects anything else (e.g. WebP)
    with no visible error. For catalog product photos, which may be in any
    format merchants' stores happen to export, use send_product_image instead."""
    return await _send(phone_number_id, access_token, {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": {
            "link": image_url,
            "caption": _trunc(caption, 1024),
        },
    })


def _convert_to_jpeg(raw: bytes) -> bytes | None:
    """Convert arbitrary image bytes (WebP, PNG-with-alpha, etc.) to JPEG,
    which WhatsApp always accepts. Flattens transparency onto white, since
    JPEG has no alpha channel."""
    try:
        img = Image.open(io.BytesIO(raw))
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
            img = background
        else:
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        print(f"⚠️ [META] image conversion failed: {e}")
        return None


async def _upload_media(phone_number_id: str, access_token: str, jpeg_bytes: bytes) -> str | None:
    """Upload image bytes to Meta's Media API, returning a media id for use
    in a subsequent send (avoids WhatsApp having to fetch a URL itself)."""
    url = f"{_GRAPH_BASE}/{phone_number_id}/media"
    headers = {"Authorization": f"Bearer {access_token}"}
    files = {"file": ("product.jpg", jpeg_bytes, "image/jpeg")}
    data = {"messaging_product": "whatsapp", "type": "image/jpeg"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers=headers, data=data, files=files)
        if r.status_code == 200:
            return r.json().get("id")
        print(f"⚠️ [META] media upload failed status={r.status_code} body={r.text[:300]}")
        return None
    except Exception as e:
        print(f"⚠️ [META] media upload error: {e}")
        return None


async def send_product_image(
    phone_number_id: str,
    access_token: str,
    to: str,
    image_url: str,
    caption: str,
) -> bool:
    """Download a product photo from the merchant's store, convert it to
    JPEG regardless of its original format, upload it to Meta, then send it.

    Use this (not send_image_with_caption) for any catalog product photo —
    merchants' stores may export WebP, PNG, or other formats WhatsApp's
    outbound image message doesn't accept, and WhatsApp fetching the link
    itself gives no visible error when it silently rejects the format."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(image_url)
        if r.status_code != 200 or not r.content:
            print(f"⚠️ [META] product image download failed status={r.status_code} url={image_url}")
            return False
        raw = r.content
    except Exception as e:
        print(f"⚠️ [META] product image download error url={image_url}: {e}")
        return False

    jpeg_bytes = _convert_to_jpeg(raw)
    if not jpeg_bytes:
        return False

    media_id = await _upload_media(phone_number_id, access_token, jpeg_bytes)
    if not media_id:
        return False

    return await _send(phone_number_id, access_token, {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": {
            "id": media_id,
            "caption": _trunc(caption, 1024),
        },
    })


# ── Checkout confirmation ─────────────────────────────────────────────────────

async def send_checkout_message(
    phone_number_id: str,
    access_token: str,
    to: str,
    product_name: str,
    cart_url: str,
    price: str = "",
) -> bool:
    lines = ["Your cart is ready!\n", f"• {product_name}"]
    if price:
        lines.append(f"  {price}")
    lines += ["\nComplete your order here:", cart_url, "\nNeed help? Just reply here."]
    return await send_text(phone_number_id, access_token, to, "\n".join(lines))
