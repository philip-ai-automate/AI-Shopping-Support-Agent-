"""
WhatsApp customer shopping journey — Steps 3–7.

Step 3 — Negotiate:      customer asks for discount
Step 4 — Order details:  collect name + delivery preference + address
Step 5 — Payment:        show bank account, collect payment-proof image
Step 6 — Confirmation:   merchant confirms payment via portal → WA notification
Step 7 — Follow-up:      dispatched / delivered status via portal → WA notification

Discount modes (set per tenant in wa_merchant_settings):
  merchant_only    — AI never offers discounts; any request → immediate handoff
  ai_then_merchant — AI offers the per-product configured discount first;
                     if customer asks for more → mandatory handoff

Entry: meta_webhook.py routes here when customer has an active wa_shop_session
       OR their message matches order-intent keywords (new order start).
"""

import json
import os
import re

import httpx

from meta_sender import send_text
from currency import to_ngn, fmt_ngn
from wa_db import (
    cancel_handoff,
    cancel_wa_order,
    create_handoff,
    create_wa_order,
    create_wa_order_pending,
    delete_wa_shop_session,
    get_active_gateway,
    get_document_for_product,
    get_manual_product_in_stock,
    get_merchant_bank,
    get_product_by_id,
    get_product_discount_override,
    get_viewed_products,
    get_wa_merchant_settings,
    get_wa_shop_session,
    log_message,
    save_wa_shop_session,
    search_tenant_products,
)

_NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

_PORTAL_INTERNAL_URL = os.getenv("PORTAL_INTERNAL_URL", "http://127.0.0.1:5055")
_INTERNAL_TOKEN      = os.getenv("PHIXTRA_INTERNAL_TOKEN", "")


def _resolve_discount(tenant_id: int, product_id: str, def_type: str, def_value: float) -> tuple[str, float]:
    """
    Return (discount_type, discount_value) for a product.
    Priority: per-product override → tenant global default.
    """
    override = get_product_discount_override(tenant_id, product_id)
    if override and float(override.get("discount_value") or 0) > 0:
        return override["discount_type"], float(override["discount_value"])
    return def_type, def_value


# ── Formatting ────────────────────────────────────────────────────────────────

def _fmt_price(amount) -> str:
    try:
        return f"₦{float(amount):,.0f}"
    except Exception:
        return str(amount)


def _calc_discounted(unit_price: float, discount_type: str, discount_value: float) -> float:
    if discount_type == "percent":
        return max(0, round(unit_price * (1 - discount_value / 100), 2))
    if discount_type == "flat":
        return max(0, round(unit_price - discount_value, 2))
    return unit_price


def _parse_price(price_str) -> float:
    """Convert a price string to NGN float (handles £, $, ₦ symbols)."""
    return to_ngn(price_str)


# ── Intent helpers ────────────────────────────────────────────────────────────

def _yes(text: str) -> bool:
    return text.strip().lower() in {
        "yes", "y", "yeah", "yep", "ok", "okay", "sure",
        "✅", "👍", "1", "accept", "agreed", "confirm",
    }


def _no(text: str) -> bool:
    return text.strip().lower() in {
        "no", "n", "nope", "nah", "cancel", "❌", "👎", "2",
        "decline", "no thanks",
    }


def _wants_discount(text: str) -> bool:
    lower = text.strip().lower()
    return any(kw in lower for kw in [
        "discount", "reduce", "lower the price", "better price", "negotiate",
        "cheaper", "too expensive", "can you do", "best price", "cut the price",
        "drop the price", "price down", "come down", "reduce the price",
    ])


def _wants_more_discount(text: str) -> bool:
    lower = text.strip().lower()
    return any(kw in lower for kw in [
        "more discount", "more off", "still expensive", "not enough",
        "better deal", "further reduction", "additional discount",
        "still too high", "need more", "reduce more",
    ])


def _wants_pickup(text: str) -> bool:
    return text.strip().lower() in {
        "pickup", "pick up", "collect", "i'll collect", "collection",
        "come pick", "self pickup", "i will pick", "i'll pick up",
    }


def _wants_delivery(text: str) -> bool:
    return text.strip().lower() in {
        "delivery", "deliver", "send it", "ship", "shipping",
        "bring it", "deliver to me", "home delivery",
    }


def _wants_cancel(text: str) -> bool:
    return text.strip().lower() in {
        "cancel", "stop", "exit", "quit", "cancel order", "abort",
    }


def _wants_proceed_anyway(text: str) -> bool:
    """Customer wants to drop the discount request and proceed with the order."""
    lower = text.strip().lower()
    return lower in {
        "proceed", "go ahead", "continue", "yes", "y", "ok", "okay", "sure",
        "no discount", "forget it", "never mind", "nevermind",
    } or any(kw in lower for kw in [
        "no discount", "without discount", "full price", "just order",
        "forget discount", "changed my mind", "dont want discount",
        "don't want discount", "proceed without", "i'll pay full",
    ])


# ── Reply helper ──────────────────────────────────────────────────────────────

async def _reply(tenant_id: int, phone_number_id: str, access_token: str, customer_phone: str, text: str):
    await send_text(phone_number_id, access_token, customer_phone, text)
    log_message(tenant_id, phone_number_id, customer_phone, "outbound", text)


async def _notify_handoff(tenant_id: int, phone_number_id: str, access_token: str,
                           customer_phone: str, last_customer_message: str):
    """
    Alert the merchant (WA + email) when the shopping flow escalates to
    HANDOFF_DISCOUNT. Lazy import — meta_webhook.py imports this module at
    load time, so importing it back at module level would be circular.
    """
    from meta_webhook import notify_merchant_handoff
    await notify_merchant_handoff(tenant_id, phone_number_id, access_token,
                                   customer_phone, last_customer_message)


# ── Payment details message ───────────────────────────────────────────────────

async def _send_payment_details(
    tenant_id: int,
    phone_number_id: str,
    access_token: str,
    customer_phone: str,
    cart: dict,
    bank: dict | None,
    reminder: bool = False,
):
    final_price   = _fmt_price(cart.get("final_price") or cart.get("unit_price") or 0)
    pname         = cart.get("product_name", "your order")
    delivery_type = cart.get("delivery_type", "pickup")
    delivery_line = (
        f"• Delivery to: {cart.get('delivery_address', '—')}\n"
        if delivery_type == "delivery"
        else "• Collection: Pickup\n"
    )

    if bank:
        bank_block = (
            f"🏦 *Payment Details:*\n"
            f"• Bank: {bank.get('bank_name', '—')}\n"
            f"• Account Number: *{bank.get('account_number', '—')}*\n"
            f"• Account Name: {bank.get('account_name', '—')}\n"
        )
    else:
        bank_block = "_(The store will send payment details shortly)_\n"

    prefix = "⏰ *Reminder* — we're waiting for your payment proof.\n\n" if reminder else ""

    await _reply(
        tenant_id, phone_number_id, access_token, customer_phone,
        f"{prefix}"
        f"🛍️ *Order Summary*\n"
        f"• Product: {pname}\n"
        f"• Amount: *{final_price}*\n"
        f"{delivery_line}\n"
        f"{bank_block}\n"
        f"Please transfer *{final_price}* and send a *photo of your payment receipt* "
        f"here to confirm your order.\n\n"
        f"_(Reply *CANCEL* to cancel this order)_",
    )


async def _request_fw_checkout_link(order_id: str) -> str | None:
    """Ask the portal to generate a Flutterwave checkout link for this order."""
    if not _INTERNAL_TOKEN:
        print("⚠️ [SHOPPING] PHIXTRA_INTERNAL_TOKEN not set — cannot request FW link")
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{_PORTAL_INTERNAL_URL}/internal/orders/{order_id}/fw-checkout-link",
                headers={"Authorization": f"Bearer {_INTERNAL_TOKEN}"},
            )
            data = r.json()
            return data.get("link")
    except Exception as e:
        print("⚠️ [SHOPPING] _request_fw_checkout_link:", e)
        return None


async def _send_fw_payment_link(
    tenant_id: int,
    phone_number_id: str,
    access_token: str,
    customer_phone: str,
    cart: dict,
    link: str,
    reminder: bool = False,
):
    final_price = _fmt_price(cart.get("final_price") or cart.get("unit_price") or 0)
    pname       = cart.get("product_name", "your order")
    reference   = cart.get("reference", "")
    prefix      = "⏰ *Reminder* — your payment link is still waiting.\n\n" if reminder else ""

    await _reply(
        tenant_id, phone_number_id, access_token, customer_phone,
        f"{prefix}"
        f"🛍️ *Order Summary*\n"
        f"• Product: {pname}\n"
        f"• Amount: *{final_price}*\n"
        f"• Reference: *{reference}*\n\n"
        f"💳 Tap to pay securely:\n{link}\n\n"
        "You'll get a message here the moment your payment is confirmed — "
        "no need to send a receipt.\n\n"
        "_(Reply *CANCEL* to cancel this order)_",
    )


async def _use_flutterwave(
    tenant_id: int,
    phone_number_id: str,
    access_token: str,
    customer_phone: str,
    session_id: str,
    cart: dict,
) -> bool:
    """Create the order, request a checkout link, and message it to the customer. False on failure."""
    order_id, reference = create_wa_order_pending(
        tenant_id        = tenant_id,
        customer_phone   = customer_phone,
        customer_name    = cart.get("customer_name", ""),
        cart             = cart,
        delivery_type    = cart.get("delivery_type", "pickup"),
        delivery_address = cart.get("delivery_address"),
    )
    link = await _request_fw_checkout_link(order_id)
    if link:
        cart["order_id"]  = order_id
        cart["reference"] = reference
        cart["fw_link"]   = link
        await _send_fw_payment_link(tenant_id, phone_number_id, access_token, customer_phone, cart, link)
        save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAITING_FW_PAYMENT", cart, order_id)
        return True
    # Flutterwave call failed — don't strand the customer
    cancel_wa_order(order_id)
    print(f"⚠️ [SHOPPING] FW checkout link failed for order {reference}")
    return False


async def _use_bank_transfer(
    tenant_id: int,
    phone_number_id: str,
    access_token: str,
    customer_phone: str,
    session_id: str,
    cart: dict,
):
    bank = get_merchant_bank(tenant_id)
    await _send_payment_details(tenant_id, phone_number_id, access_token, customer_phone, cart, bank)
    save_wa_shop_session(session_id, tenant_id, customer_phone, "PAYMENT_PENDING", cart)


async def _start_payment(
    tenant_id: int,
    phone_number_id: str,
    access_token: str,
    customer_phone: str,
    session_id: str,
    cart: dict,
):
    """
    Offer both payment methods when the merchant has both configured;
    otherwise go straight to whichever one is available.
    """
    # Final stock check — the product may have sold out since it was first
    # shown earlier in the conversation. Re-verify against live data before
    # any payment details are sent. A cart's product_id belongs to exactly
    # one of two catalogs (WooCommerce-synced `documents`, or the manual
    # WA-only `products` table) — check both; whichever doesn't recognise
    # the id simply returns "not found" and is ignored.
    doc = get_document_for_product(cart.get("product_id", ""))
    woo_out_of_stock = doc is not None and doc.get("in_stock") is False

    manual_in_stock = get_manual_product_in_stock(tenant_id, cart.get("product_id", ""))
    manual_out_of_stock = manual_in_stock is False

    if woo_out_of_stock or manual_out_of_stock:
        delete_wa_shop_session(session_id)
        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            f"I'm sorry — *{cart.get('product_name', 'this item')}* just went out of stock, "
            "so I can't process this order.\n\n"
            "Feel free to browse and order something else anytime! 😊",
        )
        return

    fw_enabled = get_active_gateway(tenant_id, "flutterwave")
    bank       = get_merchant_bank(tenant_id)

    if fw_enabled and bank:
        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            "How would you like to pay?\n\n"
            "1️⃣ Pay online (card/bank/USSD) — instant confirmation\n"
            "2️⃣ Bank transfer — send receipt photo\n\n"
            "Reply with a *number*.",
        )
        save_wa_shop_session(session_id, tenant_id, customer_phone, "CHOOSE_PAYMENT_METHOD", cart)
        return

    if fw_enabled:
        if await _use_flutterwave(tenant_id, phone_number_id, access_token, customer_phone, session_id, cart):
            return
        # fall through to bank transfer if Flutterwave failed, even with no bank on file
        # (get_merchant_bank returns None and _send_payment_details handles that gracefully)

    await _use_bank_transfer(tenant_id, phone_number_id, access_token, customer_phone, session_id, cart)


# ── Main entry point ──────────────────────────────────────────────────────────

async def handle_shopping_message(
    msg: dict,
    tenant: dict,
    shop_session: dict | None,
):
    """
    Called from meta_webhook.py when:
      - shop_session is not None: customer has an active order in progress
      - shop_session is None:     order-intent keyword detected; start new order

    tenant dict must include: tenant_id, access_token, phone_number_id (str).
    """
    phone_number_id = msg["phone_number_id"]
    access_token    = tenant["access_token"]
    customer_phone  = msg["customer_phone"]
    session_id      = msg["session_id"]
    text            = (msg.get("text") or "").strip()
    msg_type        = msg.get("message_type", "text")
    media_url       = msg.get("media_url") or ""
    tenant_id       = int(tenant["tenant_id"])

    # Load tenant discount settings once — used throughout this function
    settings             = get_wa_merchant_settings(tenant_id)
    _def_disc_type       = settings.get("default_discount_type", "percent")
    _def_disc_value      = float(settings.get("default_discount_value") or 0)

    # ── New order — create session and prompt ─────────────────────────────────
    if shop_session is None:
        # If the customer already selected/viewed products this session, skip
        # straight to AWAIT_CONFIRM (single) or a numbered choice (multiple).
        # Out-of-stock items are dropped here so they can never enter a cart.
        viewed_all   = get_viewed_products(session_id)
        oos_viewed   = [v for v in viewed_all if not v.get("in_stock", True)]
        viewed       = [v for v in viewed_all if v.get("in_stock", True)]
        direct_disc  = _wants_discount(text)   # customer typed DISCOUNT directly

        if not viewed and oos_viewed:
            names = ", ".join(v["product_name"] for v in oos_viewed[:3])
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                f"I'm sorry, *{names}* {'is' if len(oos_viewed) == 1 else 'are'} currently out of stock.\n\n"
                "Which other product would you like to order?\n"
                "_(Type the product name or model)_",
            )
            save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_PRODUCT", {})
            return

        if len(viewed) == 1:
            v = viewed[0]
            # Prefer products table (has discount info); fall back to session cache
            p_data = get_product_by_id(tenant_id, v["product_id"])
            if p_data:
                unit_price     = p_data["price"]          # already float
                product_name   = p_data["name"]
                discount_type  = p_data.get("discount_type", "percent")
                discount_value = p_data["discount_value"]  # already float
            else:
                unit_price    = _parse_price(v["price"])
                product_name  = v["product_name"]
                discount_type, discount_value = _resolve_discount(
                    tenant_id, v["product_id"], _def_disc_type, _def_disc_value
                )
            new_cart = {
                "product_id":     v["product_id"],
                "product_name":   product_name,
                "unit_price":     unit_price,
                "quantity":       1,
                "discount_type":  discount_type,
                "discount_value": discount_value,
                "final_price":    unit_price,
            }
            # Customer typed DISCOUNT directly → skip AWAIT_CONFIRM and offer discount now
            if direct_disc:
                mode = settings.get("discount_mode", "merchant_only")
                if mode == "merchant_only":
                    save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_CONFIRM", new_cart)
                    await _reply(
                        tenant_id, phone_number_id, access_token, customer_phone,
                        "I'm passing your discount request to the store — they'll be in touch shortly! 🤝\n\n"
                        f"The product is *{product_name}* at *{_fmt_price(unit_price)}*.\n"
                        "Reply *CANCEL* to cancel.",
                    )
                    create_handoff(session_id, tenant_id, customer_phone)
                    await _notify_handoff(tenant_id, phone_number_id, access_token, customer_phone, text)
                    return
                dv = float(new_cart.get("discount_value") or 0)
                dt = new_cart.get("discount_type", "percent")
                if dv == 0:
                    await _reply(
                        tenant_id, phone_number_id, access_token, customer_phone,
                        f"I'm sorry — no additional discount is available on *{product_name}* at this time.\n\n"
                        f"The best price is *{_fmt_price(unit_price)}*.\n"
                        "Reply *YES* to order at this price, or *NO* to cancel.",
                    )
                    save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_CONFIRM", new_cart)
                    return
                discounted = _calc_discounted(unit_price, dt, dv)
                new_cart["final_price"] = discounted
                new_cart["discount_applied"] = round(unit_price - discounted, 2)
                disc_label = f"{dv:.0f}% off → *{_fmt_price(discounted)}*" if dt == "percent" else f"₦{dv:,.0f} off → *{_fmt_price(discounted)}*"
                await _reply(
                    tenant_id, phone_number_id, access_token, customer_phone,
                    f"Great news! Here's the best I can do for *{product_name}*:\n\n"
                    f"💰 Original price: ~~{_fmt_price(unit_price)}~~\n"
                    f"🎉 Discount: {disc_label}\n\n"
                    "Reply *YES* to accept this discount and place your order.\n"
                    "Reply *NO* to pay the full price.\n"
                    "Reply *CANCEL* to cancel.",
                )
                save_wa_shop_session(session_id, tenant_id, customer_phone, "NEGOTIATING", new_cart)
                return

            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                f"📦 *{product_name}*\n"
                f"💰 Price: *{_fmt_price(unit_price)}*\n\n"
                "Reply *YES* to confirm this order.\n"
                "Reply *DISCOUNT* to ask for a discount.\n"
                "Reply *NO* to cancel.",
            )
            save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_CONFIRM", new_cart)
            return

        elif len(viewed) > 1:
            search_results = []
            for v in viewed[:5]:  # cap at 5 to match _NUMBER_EMOJI
                p_data = get_product_by_id(tenant_id, v["product_id"])
                if p_data:
                    search_results.append({
                        "id":             v["product_id"],
                        "name":           p_data["name"],
                        "price":          p_data["price"],
                        "discount_type":  p_data.get("discount_type", "percent"),
                        "discount_value": p_data["discount_value"],
                    })
                else:
                    _dt, _dv = _resolve_discount(
                        tenant_id, v["product_id"], _def_disc_type, _def_disc_value
                    )
                    search_results.append({
                        "id":             v["product_id"],
                        "name":           v["product_name"],
                        "price":          _parse_price(v["price"]),
                        "discount_type":  _dt,
                        "discount_value": _dv,
                    })
            if direct_disc:
                lines = ["Which product would you like a discount on?\n"]
            else:
                lines = ["Which of the products you viewed would you like to order?\n"]
            for i, p in enumerate(search_results):
                emoji = _NUMBER_EMOJI[i] if i < len(_NUMBER_EMOJI) else f"{i + 1}."
                lines.append(f"{emoji}  {p['name']} — {_fmt_price(p['price'])}")
            lines.append("\nReply with a *number* to select.")
            save_wa_shop_session(
                session_id, tenant_id, customer_phone,
                "AWAIT_PRODUCT", {
                    "search_results":    search_results,
                    "discount_requested": direct_disc,   # remember the intent
                },
            )
            await _reply(tenant_id, phone_number_id, access_token, customer_phone, "\n".join(lines))
            return

        # No prior selection — ask for product name as normal
        save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_PRODUCT", {})
        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            "Sure! Let's get your order started. 🛍️\n\n"
            "Which product would you like to order?\n"
            "_(Type the product name or model — e.g. iPhone 14 or Samsung A54)_",
        )
        return

    state = shop_session["state"]
    raw   = shop_session.get("cart", {})
    cart  = raw if isinstance(raw, dict) else json.loads(raw or "{}")

    # ── Cancel at any pre-payment state ──────────────────────────────────────
    if _wants_cancel(text) and state not in ("PAYMENT_REVIEW", "COMPLETE"):
        if state == "AWAITING_FW_PAYMENT" and cart.get("order_id"):
            cancel_wa_order(cart["order_id"])
        delete_wa_shop_session(session_id)
        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            "Order cancelled. No problem — feel free to browse and order anytime! 😊",
        )
        return

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 3 — Negotiate
    # ─────────────────────────────────────────────────────────────────────────

    # ── AWAIT_PRODUCT — identify what the customer wants ─────────────────────
    if state == "AWAIT_PRODUCT":
        search_results = cart.get("search_results", [])

        # Customer is picking from a numbered list shown previously
        disc_was_requested = bool(cart.get("discount_requested", False))
        if search_results:
            try:
                choice = int(text.strip()) - 1
                if 0 <= choice < len(search_results):
                    picked   = search_results[choice]
                    new_cart = {
                        "product_id":     picked["id"],
                        "product_name":   picked["name"],
                        "unit_price":     float(picked["price"]),
                        "quantity":       1,
                        "discount_type":  picked.get("discount_type", "percent"),
                        "discount_value": float(picked.get("discount_value") or 0),
                        "final_price":    float(picked["price"]),
                    }

                    # If customer originally asked for DISCOUNT, offer it now
                    if disc_was_requested:
                        mode = settings.get("discount_mode", "merchant_only")
                        dv   = float(new_cart.get("discount_value") or 0)
                        dt   = new_cart.get("discount_type", "percent")
                        unit = float(picked["price"])
                        if mode == "merchant_only":
                            save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_CONFIRM", new_cart)
                            create_handoff(session_id, tenant_id, customer_phone)
                            await _notify_handoff(tenant_id, phone_number_id, access_token, customer_phone, text)
                            await _reply(
                                tenant_id, phone_number_id, access_token, customer_phone,
                                f"I'm passing your discount request to the store for *{picked['name']}* — they'll be in touch! 🤝\n"
                                "Reply *CANCEL* to cancel.",
                            )
                            return
                        if dv == 0:
                            await _reply(
                                tenant_id, phone_number_id, access_token, customer_phone,
                                f"I'm sorry — no additional discount is available on *{picked['name']}* at this time.\n\n"
                                f"Best price: *{_fmt_price(unit)}*\n"
                                "Reply *YES* to order at this price, or *NO* to cancel.",
                            )
                            save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_CONFIRM", new_cart)
                            return
                        discounted = _calc_discounted(unit, dt, dv)
                        new_cart["final_price"]      = discounted
                        new_cart["discount_applied"] = round(unit - discounted, 2)
                        disc_label = (f"{dv:.0f}% off → *{_fmt_price(discounted)}*"
                                      if dt == "percent" else f"₦{dv:,.0f} off → *{_fmt_price(discounted)}*")
                        await _reply(
                            tenant_id, phone_number_id, access_token, customer_phone,
                            f"Great news! Here's the best I can do for *{picked['name']}*:\n\n"
                            f"💰 Original price: {_fmt_price(unit)}\n"
                            f"🎉 Discount: {disc_label}\n\n"
                            "Reply *YES* to accept and place your order.\n"
                            "Reply *NO* to pay full price.\n"
                            "Reply *CANCEL* to cancel.",
                        )
                        save_wa_shop_session(session_id, tenant_id, customer_phone, "NEGOTIATING", new_cart)
                        return

                    await _reply(
                        tenant_id, phone_number_id, access_token, customer_phone,
                        f"📦 *{picked['name']}*\n"
                        f"💰 Price: *{_fmt_price(picked['price'])}*\n\n"
                        "Reply *YES* to confirm this order.\n"
                        "Reply *DISCOUNT* to ask for a discount.\n"
                        "Reply *NO* to cancel.",
                    )
                    save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_CONFIRM", new_cart)
                    return
                await _reply(
                    tenant_id, phone_number_id, access_token, customer_phone,
                    f"Please pick a number between 1 and {len(search_results)}.",
                )
                return
            except ValueError:
                pass  # Not a number — treat as a new search

        # Search merchant's product catalogue
        products = search_tenant_products(tenant_id, text)

        if not products:
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                f"I couldn't find *{text}* in this store.\n\n"
                "Please type the product name or model exactly.\n"
                "_(Reply CANCEL to stop)_",
            )
            return

        if len(products) == 1:
            p = products[0]
            new_cart = {
                "product_id":     p["id"],
                "product_name":   p["name"],
                "unit_price":     float(p["price"]),
                "quantity":       1,
                "discount_type":  p.get("discount_type", "percent"),
                "discount_value": float(p.get("discount_value") or 0),
                "final_price":    float(p["price"]),
            }
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                f"📦 *{p['name']}*\n"
                f"💰 Price: *{_fmt_price(p['price'])}*\n\n"
                "Reply *YES* to confirm this order.\n"
                "Reply *DISCOUNT* to ask for a discount.\n"
                "Reply *NO* to cancel.",
            )
            save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_CONFIRM", new_cart)
            return

        # Multiple matches — show numbered list
        cart["search_results"] = products
        lines = ["Here are matching products:\n"]
        for i, p in enumerate(products):
            emoji = _NUMBER_EMOJI[i] if i < len(_NUMBER_EMOJI) else f"{i + 1}."
            lines.append(f"{emoji}  {p['name']} — {_fmt_price(p['price'])}")
        lines.append("\nReply with a *number* to select.")
        await _reply(tenant_id, phone_number_id, access_token, customer_phone, "\n".join(lines))
        save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_PRODUCT", cart)
        return

    # ── AWAIT_CONFIRM — product shown, waiting for YES/DISCOUNT/NO ────────────
    if state == "AWAIT_CONFIRM":
        if _wants_discount(text):
            settings = get_wa_merchant_settings(tenant_id)
            mode     = settings.get("discount_mode", "merchant_only")

            if mode == "merchant_only":
                await _reply(
                    tenant_id, phone_number_id, access_token, customer_phone,
                    "I'm passing your discount request to the store — they'll be in touch shortly! 🤝\n\n"
                    "In the meantime:\n"
                    "• Reply *PROCEED* to order at the listed price\n"
                    "• Reply *CANCEL* to cancel your order",
                )
                create_handoff(session_id, tenant_id, customer_phone)
                await _notify_handoff(tenant_id, phone_number_id, access_token, customer_phone, text)
                cart["pending_handoff"] = True
                save_wa_shop_session(session_id, tenant_id, customer_phone, "HANDOFF_DISCOUNT", cart)
                return

            # Mode: ai_then_merchant
            dv = float(cart.get("discount_value") or 0)
            dt = cart.get("discount_type", "percent")

            if dv <= 0:
                await _reply(
                    tenant_id, phone_number_id, access_token, customer_phone,
                    "I'm sorry — no discounts are available on this item at this time.\n\n"
                    f"The price remains *{_fmt_price(cart['unit_price'])}*.\n"
                    "Reply *YES* to order at this price or *NO* to cancel.",
                )
                return

            discounted            = _calc_discounted(cart["unit_price"], dt, dv)
            cart["final_price"]   = discounted
            cart["discount_applied"] = round(cart["unit_price"] - discounted, 2)

            offer_line = (
                f"{dv:.0f}% off → *{_fmt_price(discounted)}*"
                if dt == "percent"
                else f"₦{dv:,.0f} off → *{_fmt_price(discounted)}*"
            )
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                f"💡 I can offer you {offer_line}.\n\n"
                "Reply *YES* to accept this price.\n"
                "Reply *NO* to order at the full price.\n"
                "Reply *MORE* if you need a further reduction.",
            )
            save_wa_shop_session(session_id, tenant_id, customer_phone, "NEGOTIATING", cart)
            return

        if _yes(text):
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                "Great! What is your *name* for this order?",
            )
            save_wa_shop_session(session_id, tenant_id, customer_phone, "COLLECT_NAME", cart)
            return

        if _no(text):
            delete_wa_shop_session(session_id)
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                "Order cancelled. No worries — browse and order anytime! 😊",
            )
            return

        pname = cart.get("product_name", "this product")
        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            f"Reply *YES* to order *{pname}* at "
            f"*{_fmt_price(cart.get('final_price') or cart.get('unit_price', 0))}*.\n"
            "Reply *DISCOUNT* to ask for a discount.\n"
            "Reply *NO* to cancel.",
        )
        return

    # ── NEGOTIATING — AI has offered a discount, waiting for response ─────────
    if state == "NEGOTIATING":
        if _wants_more_discount(text):
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                "I understand — let me connect you to the store for further discussion. 🤝\n\n"
                "In the meantime:\n"
                "• Reply *PROCEED* to order at the discounted price I offered\n"
                "• Reply *CANCEL* to cancel your order",
            )
            create_handoff(session_id, tenant_id, customer_phone)
            await _notify_handoff(tenant_id, phone_number_id, access_token, customer_phone, text)
            cart["pending_handoff"] = True
            save_wa_shop_session(session_id, tenant_id, customer_phone, "HANDOFF_DISCOUNT", cart)
            return

        if _yes(text):
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                f"✅ Discount applied! Your price: *{_fmt_price(cart['final_price'])}*\n\n"
                "What is your *name* for this order?",
            )
            save_wa_shop_session(session_id, tenant_id, customer_phone, "COLLECT_NAME", cart)
            return

        if _no(text):
            cart["final_price"] = cart["unit_price"]
            cart.pop("discount_applied", None)
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                f"No problem — your price is *{_fmt_price(cart['unit_price'])}*.\n\n"
                "What is your *name* for this order?",
            )
            save_wa_shop_session(session_id, tenant_id, customer_phone, "COLLECT_NAME", cart)
            return

        # Catch "still expensive" and similar outside the strict _wants_more_discount set
        if _wants_discount(text):
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                "I'm connecting you to the store for further discussion. 🤝\n\n"
                "• Reply *PROCEED* to order at the discounted price I offered\n"
                "• Reply *CANCEL* to cancel your order",
            )
            create_handoff(session_id, tenant_id, customer_phone)
            await _notify_handoff(tenant_id, phone_number_id, access_token, customer_phone, text)
            cart["pending_handoff"] = True
            save_wa_shop_session(session_id, tenant_id, customer_phone, "HANDOFF_DISCOUNT", cart)
            return

        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            "Reply *YES* to accept the discount, *NO* to pay full price, "
            "or *MORE* to speak to the store about a further reduction.",
        )
        return

    # ── HANDOFF_DISCOUNT — waiting on merchant discount, customer can proceed ──
    if state == "HANDOFF_DISCOUNT":
        if _wants_proceed_anyway(text):
            cancel_handoff(session_id)
            cart.pop("pending_handoff", None)
            pname      = cart.get("product_name", "this product")
            show_price = cart.get("final_price") or cart.get("unit_price", 0)
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                f"No problem! Here's your order summary:\n\n"
                f"📦 *{pname}*\n"
                f"💰 Price: *{_fmt_price(show_price)}*\n\n"
                "Reply *YES* to confirm.\n"
                "Reply *NO* to cancel.",
            )
            save_wa_shop_session(session_id, tenant_id, customer_phone, "AWAIT_CONFIRM", cart)
            return

        if _wants_cancel(text):
            cancel_handoff(session_id)
            delete_wa_shop_session(session_id)
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                "Order cancelled. No worries — browse and order anytime! 😊",
            )
            return

        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            "Your discount request is with the store — they'll be in touch soon! 🤝\n\n"
            "• Reply *PROCEED* to order at the current price\n"
            "• Reply *CANCEL* to cancel your order",
        )
        return

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 4 — Order details
    # ─────────────────────────────────────────────────────────────────────────

    # ── COLLECT_NAME ─────────────────────────────────────────────────────────
    if state == "COLLECT_NAME":
        if len(text) < 2:
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                "Please enter your name (at least 2 characters).",
            )
            return
        cart["customer_name"] = text
        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            f"Thanks, *{text}*! 📦\n\n"
            "Would you like *DELIVERY* to your address or will you *PICKUP* from the store?",
        )
        save_wa_shop_session(session_id, tenant_id, customer_phone, "COLLECT_DELIVERY", cart)
        return

    # ── COLLECT_DELIVERY ─────────────────────────────────────────────────────
    if state == "COLLECT_DELIVERY":
        if _wants_pickup(text):
            cart["delivery_type"] = "pickup"
            await _start_payment(tenant_id, phone_number_id, access_token, customer_phone, session_id, cart)
            return

        if _wants_delivery(text):
            cart["delivery_type"] = "delivery"
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                "Please send your *full delivery address*.\n"
                "_(Include street, area, city, and any landmark)_",
            )
            save_wa_shop_session(session_id, tenant_id, customer_phone, "COLLECT_ADDRESS", cart)
            return

        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            "Please reply *DELIVERY* for home delivery or *PICKUP* to collect in person.",
        )
        return

    # ── COLLECT_ADDRESS ───────────────────────────────────────────────────────
    if state == "COLLECT_ADDRESS":
        if len(text) < 5:
            await _reply(
                tenant_id, phone_number_id, access_token, customer_phone,
                "Please enter your full delivery address.",
            )
            return
        cart["delivery_address"] = text
        await _start_payment(tenant_id, phone_number_id, access_token, customer_phone, session_id, cart)
        return

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 5 — Payment
    # ─────────────────────────────────────────────────────────────────────────

    # ── CHOOSE_PAYMENT_METHOD — both options configured, waiting on 1/2 ───────
    if state == "CHOOSE_PAYMENT_METHOD":
        lower = text.strip().lower()
        if lower in {"1", "online", "card", "pay online", "flutterwave"}:
            if await _use_flutterwave(tenant_id, phone_number_id, access_token, customer_phone, session_id, cart):
                return
            await _use_bank_transfer(tenant_id, phone_number_id, access_token, customer_phone, session_id, cart)
            return
        if lower in {"2", "bank", "transfer", "bank transfer"}:
            await _use_bank_transfer(tenant_id, phone_number_id, access_token, customer_phone, session_id, cart)
            return
        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            "Please reply *1* to pay online or *2* for bank transfer.\n\n"
            "_(Reply *CANCEL* to cancel this order)_",
        )
        return

    # ── AWAITING_FW_PAYMENT — Flutterwave link sent, waiting on webhook ────────
    if state == "AWAITING_FW_PAYMENT":
        link = cart.get("fw_link", "")
        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            f"Your payment link is still waiting:\n{link}\n\n"
            "You'll get a message here the moment it's confirmed — no need to send anything else.\n\n"
            "_(Reply *CANCEL* to cancel this order)_",
        )
        return

    # ── PAYMENT_PENDING — waiting for bank transfer proof image ───────────────
    if state == "PAYMENT_PENDING":
        if msg_type == "image" and media_url:
            cart["payment_proof_url"] = media_url
            try:
                order_id, reference = create_wa_order(
                    tenant_id        = tenant_id,
                    customer_phone   = customer_phone,
                    customer_name    = cart.get("customer_name", ""),
                    cart             = cart,
                    delivery_type    = cart.get("delivery_type", "pickup"),
                    delivery_address = cart.get("delivery_address"),
                    receipt_image_url= media_url,
                )
                cart["order_id"]   = order_id
                cart["reference"]  = reference
                await _reply(
                    tenant_id, phone_number_id, access_token, customer_phone,
                    f"✅ *Payment proof received!*\n\n"
                    f"Your order reference: *{reference}*\n\n"
                    "The store will verify your payment and confirm shortly. "
                    "You'll receive a WhatsApp message once it's confirmed.\n\n"
                    "_Need help? Just reply here._",
                )
                save_wa_shop_session(
                    session_id, tenant_id, customer_phone,
                    "PAYMENT_REVIEW", cart, order_id,
                )
                print(f"✅ [SHOPPING] Order {reference} created for {customer_phone} tenant={tenant_id}")
            except Exception as e:
                print(f"⚠️ [SHOPPING] create_wa_order failed: {e}")
                await _reply(
                    tenant_id, phone_number_id, access_token, customer_phone,
                    "⚠️ There was an issue recording your order. Please try again or contact the store.",
                )
            return

        # Customer sent text instead of photo — remind them
        bank = get_merchant_bank(tenant_id)
        await _send_payment_details(
            tenant_id, phone_number_id, access_token, customer_phone, cart, bank, reminder=True,
        )
        return

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 6 — Confirmation (merchant confirms via portal → proactive notify)
    # ─────────────────────────────────────────────────────────────────────────

    # ── PAYMENT_REVIEW — order created, awaiting merchant confirmation ────────
    if state == "PAYMENT_REVIEW":
        ref = cart.get("reference", "your order")
        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            f"Your order *{ref}* is with the store for payment review.\n"
            "We'll send you a message as soon as it's confirmed. 😊",
        )
        return

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 7 — Follow-up (status updates sent from portal via /internal/notify-order)
    # ─────────────────────────────────────────────────────────────────────────

    # ── COMPLETE — order confirmed/dispatched/delivered ────────────────────────
    if state == "COMPLETE":
        ref = cart.get("reference", "your order")
        await _reply(
            tenant_id, phone_number_id, access_token, customer_phone,
            f"Your order *{ref}* has been completed. Thank you for shopping with us! 🎉\n\n"
            "Feel free to browse and order again anytime.",
        )
        return

    # ── Fallback ──────────────────────────────────────────────────────────────
    await _reply(
        tenant_id, phone_number_id, access_token, customer_phone,
        "I'm not sure what you mean. Reply *CANCEL* to stop this order, "
        "or follow the prompts above.",
    )
