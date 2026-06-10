import asyncio
import httpx

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

        title = _trunc(name, 24)
        desc_parts = [price] if price else []
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
    """Send a product image with a caption containing name, price, stock, and URL."""
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
