"""
WhatsApp-first merchant onboarding — Option 1 of 3 entry points.

A new merchant messages the Phixtra setup number. We walk them through:
  1. Business name
  2. Category (what they sell)
  3. Customer-facing sales number
  4. Bank transfer details
  5. Product catalog (name → price → stock → description → photo, repeating)

On completion we call /internal/provision-wa-merchant via the portal API,
then persist bank account + products directly to the shared DB.

Required env vars:
  WA_SETUP_PHONE_NUMBER_ID  — Meta phone_number_id of the PhiXtra setup number
  WA_SETUP_ACCESS_TOKEN     — Access token for the setup number
  PHIXTRA_INTERNAL_TOKEN    — Shared secret for the portal internal endpoint
  PORTAL_INTERNAL_URL       — e.g. http://127.0.0.1:5055
  PORTAL_BASE_URL           — e.g. https://portal.phixtra.com
"""

import json
import os
import re
import uuid

import httpx

from meta_sender import send_text
from wa_db import get_db_connection

_SETUP_PHONE_NUMBER_ID = os.getenv("WA_SETUP_PHONE_NUMBER_ID", "")
_SETUP_ACCESS_TOKEN    = os.getenv("WA_SETUP_ACCESS_TOKEN", "")
_PORTAL_URL            = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")
_INTERNAL_TOKEN        = os.getenv("PHIXTRA_INTERNAL_TOKEN", "")
_PORTAL_INTERNAL_URL   = os.getenv("PORTAL_INTERNAL_URL", "http://127.0.0.1:5055")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_session(phone: str) -> dict | None:
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, state, collected, tenant_id FROM wa_merchant_onboarding WHERE wa_phone=%s",
            (phone,),
        )
        row = cur.fetchone()
        if row and isinstance(row["collected"], str):
            row["collected"] = json.loads(row["collected"] or "{}")
        return row
    except Exception as e:
        print("⚠️ [ONBOARDING] _get_session:", e)
        return None
    finally:
        cur.close()
        conn.close()


def _save_session(phone: str, state: str, collected: dict, tenant_id: int = None):
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO wa_merchant_onboarding (wa_phone, state, collected, tenant_id)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              state      = VALUES(state),
              collected  = VALUES(collected),
              tenant_id  = COALESCE(VALUES(tenant_id), tenant_id),
              updated_at = CURRENT_TIMESTAMP
            """,
            (phone, state, json.dumps(collected), tenant_id),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ [ONBOARDING] _save_session:", e)
    finally:
        cur.close()
        conn.close()


def _reset_session(phone: str):
    """Delete session so the merchant can start fresh."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM wa_merchant_onboarding WHERE wa_phone=%s", (phone,))
        conn.commit()
    except Exception as e:
        print("⚠️ [ONBOARDING] _reset_session:", e)
    finally:
        cur.close()
        conn.close()


def _save_bank_account(tenant_id: int, bank_name: str, account_number: str, account_name: str):
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO merchant_bank_accounts
                (tenant_id, bank_name, account_number, account_name, is_primary)
            VALUES (%s, %s, %s, %s, 1)
            ON DUPLICATE KEY UPDATE
              bank_name      = VALUES(bank_name),
              account_number = VALUES(account_number),
              account_name   = VALUES(account_name)
            """,
            (tenant_id, bank_name, account_number, account_name),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ [ONBOARDING] _save_bank_account:", e)
    finally:
        cur.close()
        conn.close()


def _save_products(tenant_id: int, products: list, default_category: str = None):
    if not products:
        return
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        for p in products:
            cur.execute(
                """
                INSERT INTO products
                    (id, tenant_id, name, description, price,
                     stock_quantity, category, image_url, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
                """,
                (
                    str(uuid.uuid4()),
                    tenant_id,
                    (p.get("name") or "")[:255],
                    p.get("description") or None,
                    float(p.get("price") or 0),
                    int(p.get("stock") or 0),
                    (p.get("category") or default_category or None),
                    p.get("image_url") or None,
                ),
            )
        conn.commit()
        print(f"✅ [ONBOARDING] Saved {len(products)} product(s) for tenant {tenant_id}")
    except Exception as e:
        print("⚠️ [ONBOARDING] _save_products:", e)
    finally:
        cur.close()
        conn.close()


# ── Portal provisioning call ──────────────────────────────────────────────────

async def _provision_merchant(phone: str, business_name: str) -> dict | None:
    """POST /internal/provision-wa-merchant. Returns {tenant_id, customer_id} or None."""
    if not _INTERNAL_TOKEN:
        print("⚠️ [ONBOARDING] PHIXTRA_INTERNAL_TOKEN not set — cannot provision")
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{_PORTAL_INTERNAL_URL}/internal/provision-wa-merchant",
                json={"phone": phone, "business_name": business_name},
                headers={"Authorization": f"Bearer {_INTERNAL_TOKEN}"},
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        print("⚠️ [ONBOARDING] _provision_merchant:", e)
        return None


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """Accept '12500', '₦12,500', '12.5k', '12.5K'."""
    cleaned = re.sub(r"[₦,\s]", "", text.strip().lower())
    k = re.fullmatch(r"(\d+(?:\.\d+)?)k", cleaned)
    if k:
        return float(k.group(1)) * 1000
    try:
        v = float(cleaned)
        return v if v >= 0 else None
    except ValueError:
        return None


def _parse_stock(text: str) -> int | None:
    """'unlimited' / 'plenty' → 999999; otherwise parse integer."""
    lower = text.strip().lower()
    if lower in ("unlimited", "plenty", "many", "lots", "infinite", "∞", "na", "no limit"):
        return 999999
    try:
        return max(0, int(re.sub(r"[,\s]", "", lower)))
    except ValueError:
        return None


def _parse_bank(text: str) -> tuple[str, str, str] | None:
    """
    Parse 'GTBank | 0123456789 | John Doe'.
    Tries pipe, comma, and semicolon as separators.
    Returns (bank_name, account_number, account_name) or None.
    """
    for sep in ("|", ",", ";"):
        parts = [p.strip() for p in text.split(sep)]
        if len(parts) >= 3:
            bank = parts[0]
            acct = re.sub(r"\s", "", parts[1])
            name = " ".join(parts[2:]).strip()
            if bank and acct and name:
                return bank, acct, name
    return None


def _yes(text: str) -> bool:
    return text.strip().lower() in {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "✅", "👍", "1"}


def _no(text: str) -> bool:
    return text.strip().lower() in {"no", "n", "nope", "nah", "cancel", "❌", "👎", "2"}


def _wants_restart(text: str) -> bool:
    return text.strip().lower() in {"restart", "start over", "reset", "start again", "begin again"}


# ── Reply helper ──────────────────────────────────────────────────────────────

async def _reply(phone: str, message: str):
    if not _SETUP_PHONE_NUMBER_ID or not _SETUP_ACCESS_TOKEN:
        print(f"   [ONBOARDING] (no creds) → {phone}: {message[:80]}")
        return
    await send_text(_SETUP_PHONE_NUMBER_ID, _SETUP_ACCESS_TOKEN, phone, message)


# ── Main entry point ──────────────────────────────────────────────────────────

async def handle_onboarding_message(msg: dict):
    """
    Called from meta_webhook when phone_number_id == WA_SETUP_PHONE_NUMBER_ID.
    Advances the state machine for the sending merchant's phone number.
    """
    phone     = msg["customer_phone"]
    text      = (msg.get("text") or "").strip()
    media_url = msg.get("media_url") or ""
    msg_type  = msg.get("message_type", "text")

    # Allow restart at any point
    if _wants_restart(text):
        _reset_session(phone)
        await _reply(phone,
            "OK, let's start fresh! 🔄\n\n"
            "Welcome to *Phixtra*! I'll help you set up your AI-powered WhatsApp store.\n\n"
            "What's your *business name*?"
        )
        _save_session(phone, "COLLECT_BIZ_NAME", {})
        return

    session = _get_session(phone)

    # ── First contact ─────────────────────────────────────────────────────────
    if session is None:
        await _reply(phone,
            "👋 Welcome to *Phixtra*!\n\n"
            "I'll help you set up your AI-powered WhatsApp store in just a few minutes. "
            "Your customers will be able to browse products, place orders, and pay — all on WhatsApp.\n\n"
            "Let's begin! What's your *business name*?"
        )
        _save_session(phone, "COLLECT_BIZ_NAME", {})
        return

    state     = session["state"]
    collected = session["collected"]

    # ── Already complete ──────────────────────────────────────────────────────
    if state == "COMPLETE":
        await _reply(phone,
            f"✅ Your store is already live!\n\n"
            f"Log in to your dashboard at:\n{_PORTAL_URL}\n\n"
            "Enter this WhatsApp number and we'll send you a login code.\n\n"
            "_Need help? Contact support@phixtra.com_"
        )
        return

    # ── COLLECT_BIZ_NAME ──────────────────────────────────────────────────────
    if state == "COLLECT_BIZ_NAME":
        if len(text) < 2:
            await _reply(phone, "Please enter your business name (at least 2 characters).")
            return
        collected["business_name"] = text
        await _reply(phone,
            f"Love it — *{text}*! 🎉\n\n"
            "What do you *sell*? _(e.g. clothes, shoes, electronics, food, cosmetics, accessories)_"
        )
        _save_session(phone, "COLLECT_CATEGORY", collected)
        return

    # ── COLLECT_CATEGORY ─────────────────────────────────────────────────────
    if state == "COLLECT_CATEGORY":
        if len(text) < 2:
            await _reply(phone, "Please describe what you sell _(e.g. clothes, food, shoes)_.")
            return
        collected["category"] = text
        await _reply(phone,
            "Got it! 📦\n\n"
            "What *WhatsApp number* will customers message to shop?\n\n"
            "_(This can be this same number — reply *same* if so)_"
        )
        _save_session(phone, "COLLECT_SALES_NUMBER", collected)
        return

    # ── COLLECT_SALES_NUMBER ──────────────────────────────────────────────────
    if state == "COLLECT_SALES_NUMBER":
        if text.lower() in ("same", "this", "this number", "same number", "my number"):
            collected["sales_number"] = phone
        else:
            digits = re.sub(r"[^\d+]", "", text)
            if len(digits) < 7:
                await _reply(phone,
                    "Please send a valid phone number _(e.g. +2348012345678 or 08012345678)_,\n"
                    "or reply *same* if customers will use this number."
                )
                return
            collected["sales_number"] = digits
        await _reply(phone,
            "💳 Do you accept *bank transfers*?\n\n"
            "Send your bank details in this format:\n"
            "*BankName | AccountNumber | AccountName*\n\n"
            "_Example: GTBank | 0123456789 | Sarah Jones_\n\n"
            "Or reply *skip* to set this up later in your dashboard."
        )
        _save_session(phone, "COLLECT_BANK", collected)
        return

    # ── COLLECT_BANK ─────────────────────────────────────────────────────────
    if state == "COLLECT_BANK":
        if text.lower() == "skip":
            collected["bank"] = None
        else:
            parsed = _parse_bank(text)
            if not parsed:
                await _reply(phone,
                    "I couldn't read that format. Please try:\n\n"
                    "*BankName | AccountNumber | AccountName*\n\n"
                    "_Example: GTBank | 0123456789 | Sarah Jones_\n\n"
                    "Or reply *skip* to do this later."
                )
                return
            collected["bank"] = {
                "bank_name":      parsed[0],
                "account_number": parsed[1],
                "account_name":   parsed[2],
            }

        await _reply(phone,
            "Now let's add your products. 🛍\n\n"
            "What is the *name* of your first product?"
        )
        _save_session(phone, "PRODUCT_NAME", collected)
        return

    # ── PRODUCT_NAME ─────────────────────────────────────────────────────────
    if state == "PRODUCT_NAME":
        if len(text) < 1:
            await _reply(phone, "Please enter the product name.")
            return
        collected["current_product"] = {"name": text}
        await _reply(phone,
            f"💰 What is the *price* of *{text}*?\n_(Numbers only — e.g. 12500)_"
        )
        _save_session(phone, "PRODUCT_PRICE", collected)
        return

    # ── PRODUCT_PRICE ─────────────────────────────────────────────────────────
    if state == "PRODUCT_PRICE":
        price = _parse_price(text)
        if price is None:
            await _reply(phone, "Please enter a valid price _(numbers only — e.g. 12500)_.")
            return
        collected["current_product"]["price"] = price
        pname = collected["current_product"].get("name", "this product")
        await _reply(phone,
            f"📦 How many *{pname}* do you have in stock?\n_(Enter a number or reply *unlimited*)_"
        )
        _save_session(phone, "PRODUCT_STOCK", collected)
        return

    # ── PRODUCT_STOCK ─────────────────────────────────────────────────────────
    if state == "PRODUCT_STOCK":
        stock = _parse_stock(text)
        if stock is None:
            await _reply(phone, "Please enter a number _(e.g. 50)_ or reply *unlimited*.")
            return
        collected["current_product"]["stock"] = stock
        pname = collected["current_product"].get("name", "this product")
        await _reply(phone,
            f"📝 Add a short *description* for *{pname}*?\n"
            "_(One line — or reply *skip*)_"
        )
        _save_session(phone, "PRODUCT_DESC", collected)
        return

    # ── PRODUCT_DESC ─────────────────────────────────────────────────────────
    if state == "PRODUCT_DESC":
        collected["current_product"]["description"] = None if text.lower() == "skip" else text
        pname = collected["current_product"].get("name", "this product")
        await _reply(phone,
            f"📸 Send a *photo* of *{pname}*?\n"
            "_(Attach an image to this message — or reply *skip*)_"
        )
        _save_session(phone, "PRODUCT_IMAGE", collected)
        return

    # ── PRODUCT_IMAGE ─────────────────────────────────────────────────────────
    if state == "PRODUCT_IMAGE":
        if msg_type == "image" and media_url:
            # media_url here is the Meta media_id — stored as reference
            collected["current_product"]["image_url"] = media_url
        else:
            collected["current_product"]["image_url"] = None

        cp        = collected["current_product"]
        price_str = f"₦{cp['price']:,.0f}" if cp.get("price") is not None else "—"
        stock_str = "Unlimited" if cp.get("stock") == 999999 else str(cp.get("stock", 0))
        photo_str = "✅ Received" if cp.get("image_url") else "—"

        await _reply(phone,
            f"*Product preview:*\n"
            f"• Name: {cp.get('name', '—')}\n"
            f"• Price: {price_str}\n"
            f"• Stock: {stock_str}\n"
            f"• Description: {cp.get('description') or '—'}\n"
            f"• Photo: {photo_str}\n\n"
            "Save this product? Reply *YES* or *NO*"
        )
        _save_session(phone, "PRODUCT_CONFIRM", collected)
        return

    # ── PRODUCT_CONFIRM ───────────────────────────────────────────────────────
    if state == "PRODUCT_CONFIRM":
        if _yes(text):
            cp = dict(collected.pop("current_product", {}))
            cp["category"] = collected.get("category")
            products = collected.get("products", [])
            products.append(cp)
            collected["products"] = products
            await _reply(phone,
                f"✅ *{cp.get('name')}* saved!\n\n"
                "Add another product? Reply *YES* or *NO*"
            )
            _save_session(phone, "ADD_MORE", collected)
        elif _no(text):
            await _reply(phone,
                "No problem — let's redo that one.\n\n"
                "What is the *product name*?"
            )
            collected.pop("current_product", None)
            _save_session(phone, "PRODUCT_NAME", collected)
        else:
            await _reply(phone, "Please reply *YES* to save or *NO* to redo this product.")
        return

    # ── ADD_MORE ──────────────────────────────────────────────────────────────
    if state == "ADD_MORE":
        if _yes(text):
            await _reply(phone, "What is the *name* of the next product?")
            _save_session(phone, "PRODUCT_NAME", collected)
            return

        if _no(text):
            biz_name  = collected.get("business_name", f"WA Merchant {phone[-4:]}")
            products  = collected.get("products", [])
            bank      = collected.get("bank")
            category  = collected.get("category")

            await _reply(phone, "⏳ Setting up your store now — just a moment...")

            result = await _provision_merchant(phone, biz_name)
            if not result:
                await _reply(phone,
                    "⚠️ There was a technical issue setting up your account.\n"
                    "Please contact support@phixtra.com and mention your number."
                )
                return

            tenant_id = int(result["tenant_id"])

            if bank:
                _save_bank_account(
                    tenant_id,
                    bank["bank_name"],
                    bank["account_number"],
                    bank["account_name"],
                )

            if products:
                _save_products(tenant_id, products, default_category=category)

            sales_num    = collected.get("sales_number") or phone
            n_products   = len(products)
            product_line = (
                f"• {n_products} product{'s' if n_products != 1 else ''} added to your catalog\n"
                if n_products else ""
            )

            await _reply(phone,
                f"🎉 *Your store is live!*\n\n"
                f"Customers can now message *{sales_num}* to shop.\n\n"
                f"{product_line}"
                f"📊 View your dashboard:\n{_PORTAL_URL}\n\n"
                "Log in with this WhatsApp number — we'll send you an OTP.\n\n"
                "_You can add more products, manage orders, and track sales from your dashboard at any time._"
            )

            _save_session(phone, "COMPLETE", collected, tenant_id)
            print(f"✅ [ONBOARDING] {phone} → tenant {tenant_id} | {n_products} products | bank={'yes' if bank else 'no'}")
            return

        await _reply(phone, "Please reply *YES* to add another product or *NO* to finish setup.")
        return

    # ── Fallback ──────────────────────────────────────────────────────────────
    step_labels = {
        "COLLECT_BIZ_NAME":    "business name",
        "COLLECT_CATEGORY":    "what you sell",
        "COLLECT_SALES_NUMBER":"sales WhatsApp number",
        "COLLECT_BANK":        "bank details",
        "PRODUCT_NAME":        "product name",
        "PRODUCT_PRICE":       "product price",
        "PRODUCT_STOCK":       "stock quantity",
        "PRODUCT_DESC":        "product description",
        "PRODUCT_IMAGE":       "product photo",
        "PRODUCT_CONFIRM":     "YES or NO to confirm product",
        "ADD_MORE":            "YES or NO to add another product",
    }
    step = step_labels.get(state, state)
    await _reply(phone,
        f"I'm waiting for your *{step}*.\n\n"
        "Please follow the prompts above, or reply *restart* to begin again."
    )
