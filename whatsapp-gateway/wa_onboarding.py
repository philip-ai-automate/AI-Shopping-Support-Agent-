"""
WhatsApp-first merchant onboarding.

Flow:
  1. Business name
  2. Category (what they sell)
  3. Customer-facing sales number
  4. Products — search catalogue, pick + set price, or type MANUAL, or SKIP

Bank account collection is NOT part of this flow (not yet planned).

On completion, /internal/provision-wa-merchant is called to create the
tenant + customer account, then products are saved to the products table.

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
from wa_db import get_db_connection, search_catalogue

_SETUP_PHONE_NUMBER_ID = os.getenv("WA_SETUP_PHONE_NUMBER_ID", "")
_SETUP_ACCESS_TOKEN    = os.getenv("WA_SETUP_ACCESS_TOKEN", "")
_PORTAL_URL            = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")
_INTERNAL_TOKEN        = os.getenv("PHIXTRA_INTERNAL_TOKEN", "")
_PORTAL_INTERNAL_URL   = os.getenv("PORTAL_INTERNAL_URL", "http://127.0.0.1:5055")

_NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_session(phone: str) -> dict | None:
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor)
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
            ON CONFLICT (wa_phone) DO UPDATE SET
              state      = EXCLUDED.state,
              collected  = EXCLUDED.collected,
              tenant_id  = COALESCE(EXCLUDED.tenant_id, wa_merchant_onboarding.tenant_id),
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
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


# ── Portal provisioning ───────────────────────────────────────────────────────

async def _provision_merchant(phone: str, business_name: str) -> dict | None:
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
    lower = text.strip().lower()
    if lower in ("unlimited", "plenty", "many", "lots", "infinite", "∞", "na", "no limit"):
        return 999999
    try:
        return max(0, int(re.sub(r"[,\s]", "", lower)))
    except ValueError:
        return None


def _fmt_price(amount) -> str:
    """Format a numeric price as ₦1,234,000"""
    try:
        return f"₦{float(amount):,.0f}"
    except Exception:
        return str(amount)


def _yes(text: str) -> bool:
    return text.strip().lower() in {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "✅", "👍", "1"}


def _no(text: str) -> bool:
    return text.strip().lower() in {"no", "n", "nope", "nah", "cancel", "❌", "👎", "2"}


def _wants_restart(text: str) -> bool:
    return text.strip().lower() in {"restart", "start over", "reset", "start again", "begin again"}


def _wants_manual(text: str) -> bool:
    return text.strip().lower() in {"manual", "add manual", "custom", "other"}


def _wants_skip(text: str) -> bool:
    return text.strip().lower() in {"skip", "later", "add later", "no products", "skip products"}


def _wants_new_search(text: str) -> bool:
    return text.strip().lower() in {"new search", "search again", "try again", "back", "other"}


def _wants_setup(text: str) -> bool:
    return text.strip().lower() in {"setup", "set up", "register", "start", "hi setup", "hello setup"}


# ── Reply helper ──────────────────────────────────────────────────────────────

async def _reply(phone: str, message: str):
    if not _SETUP_PHONE_NUMBER_ID or not _SETUP_ACCESS_TOKEN:
        print(f"   [ONBOARDING] (no creds) → {phone}: {message[:80]}")
        return
    await send_text(_SETUP_PHONE_NUMBER_ID, _SETUP_ACCESS_TOKEN, phone, message)


# ── Completion helper ─────────────────────────────────────────────────────────

async def _complete_registration(phone: str, collected: dict):
    """Provision the merchant, save their products, send success message."""
    biz_name = collected.get("business_name", f"WA Merchant {phone[-4:]}")
    products = collected.get("products", [])
    category = collected.get("category")

    await _reply(phone, "⏳ Setting up your store now — just a moment...")

    result = await _provision_merchant(phone, biz_name)
    if not result:
        await _reply(
            phone,
            "⚠️ There was a technical issue setting up your account.\n"
            "Please contact support@phixtra.com and mention your number.",
        )
        return

    tenant_id = int(result["tenant_id"])

    if products:
        _save_products(tenant_id, products, default_category=category)

    n_products   = len(products)
    product_line = (
        f"• {n_products} product{'s' if n_products != 1 else ''} added to your catalogue\n"
        if n_products else
        "• No products added yet — add them anytime from your dashboard\n"
    )

    await _reply(
        phone,
        f"🎉 *Your store is set up!*\n\n"
        f"{product_line}\n"
        f"📊 *Log in to your dashboard:*\n{_PORTAL_URL}\n\n"
        "Use this WhatsApp number to log in — we'll send you an OTP.\n\n"
        "*Next step:* Go to *WhatsApp Settings* in your dashboard to connect "
        "your Meta-approved WhatsApp Business number and go live.\n\n"
        "_You can also add more products, upload an Excel file, and manage "
        "your store from your dashboard at any time._",
    )

    _save_session(phone, "COMPLETE", collected, tenant_id)
    print(f"✅ [ONBOARDING] {phone} → tenant {tenant_id} | {n_products} products")


# ── Main entry point ──────────────────────────────────────────────────────────

async def handle_onboarding_message(msg: dict):
    """
    Called from meta_webhook when phone_number_id == WA_SETUP_PHONE_NUMBER_ID.
    Advances the state machine for the sending merchant's phone number.
    """
    phone     = msg["customer_phone"]
    text      = (msg.get("text") or "").strip()
    msg_type  = msg.get("message_type", "text")
    media_url = msg.get("media_url") or ""

    # Allow restart at any point
    if _wants_restart(text):
        _reset_session(phone)
        await _reply(
            phone,
            "OK, let's start fresh! 🔄\n\n"
            "Welcome to *Phixtra*! I'll help you set up your AI-powered WhatsApp store.\n\n"
            "What's your *business name*?",
        )
        _save_session(phone, "COLLECT_BIZ_NAME", {})
        return

    session = _get_session(phone)

    # ── First contact ─────────────────────────────────────────────────────────
    if session is None:
        if not _wants_setup(text):
            await _reply(
                phone,
                "👋 Hello! This is the *Phixtra* merchant registration line.\n\n"
                "To set up your AI-powered WhatsApp store, send the word:\n\n"
                "*SETUP*",
            )
            return
        await _reply(
            phone,
            "👋 Welcome to *Phixtra*!\n\n"
            "I'll help you set up your AI-powered WhatsApp store in just a few minutes. "
            "Your customers will be able to browse products, place orders, and pay — all on WhatsApp.\n\n"
            "_At any point, type *RESTART* to start over._\n\n"
            "Let's begin! What's your *business name*?",
        )
        _save_session(phone, "COLLECT_BIZ_NAME", {})
        return

    state     = session["state"]
    collected = session["collected"]

    # ── Already complete ──────────────────────────────────────────────────────
    if state == "COMPLETE":
        await _reply(
            phone,
            f"✅ Your store is already live!\n\n"
            f"Log in to your dashboard at:\n{_PORTAL_URL}\n\n"
            "Enter this WhatsApp number and we'll send you a login code.\n\n"
            "_Need help? Contact support@phixtra.com_",
        )
        return

    # ── COLLECT_BIZ_NAME ──────────────────────────────────────────────────────
    if state == "COLLECT_BIZ_NAME":
        if len(text) < 2:
            await _reply(phone, "Please enter your business name (at least 2 characters).")
            return
        collected["business_name"] = text
        await _reply(
            phone,
            f"Love it — *{text}*! 🎉\n\n"
            "What do you *sell*? _(e.g. phones, accessories, electronics, clothing)_",
        )
        _save_session(phone, "COLLECT_CATEGORY", collected)
        return

    # ── COLLECT_CATEGORY ─────────────────────────────────────────────────────
    if state == "COLLECT_CATEGORY":
        if len(text) < 2:
            await _reply(phone, "Please describe what you sell _(e.g. phones, accessories)_.")
            return
        collected["category"] = text
        await _reply(
            phone,
            "Got it! 📦\n\n"
            "Now let's add the products you sell.\n\n"
            "Search for a model — e.g. *iPhone 15* or *Samsung A54*\n\n"
            "Type *MANUAL* to add a custom product not in our catalogue.\n"
            "Type *SKIP* to finish now and add products later from your dashboard.",
        )
        _save_session(phone, "PRODUCT_SEARCH", collected)
        return

    # ── PRODUCT_SEARCH ────────────────────────────────────────────────────────
    if state == "PRODUCT_SEARCH":
        if _wants_skip(text):
            await _complete_registration(phone, collected)
            return

        if _wants_manual(text):
            await _reply(phone, "What is the *name* of the product?")
            _save_session(phone, "PRODUCT_NAME", collected)
            return

        # Run catalogue search
        results = search_catalogue(text, limit=5)

        if not results:
            await _reply(
                phone,
                f"No matches found for *{text}*.\n\n"
                "Try a different search term, or:\n"
                "• Type *MANUAL* to add the product yourself\n"
                "• Type *SKIP* to finish setup and add products from your dashboard",
            )
            return

        # Store results in session so user can pick by number
        collected["catalogue_results"] = results

        lines = [f"Here are matching phones:\n"]
        for i, r in enumerate(results):
            emoji = _NUMBER_EMOJI[i] if i < len(_NUMBER_EMOJI) else f"{i+1}."
            price_str = f" — est. {_fmt_price(r['price_min'])}" if r.get("price_min") else ""
            lines.append(f"{emoji}  {r['title']}{price_str}")

        lines.append(
            "\nReply with a *number* to select.\n"
            "Type *NEW SEARCH* to try a different model.\n"
            "Type *MANUAL* to add a product not listed."
        )

        await _reply(phone, "\n".join(lines))
        _save_session(phone, "PRODUCT_RESULTS", collected)
        return

    # ── PRODUCT_RESULTS ───────────────────────────────────────────────────────
    if state == "PRODUCT_RESULTS":
        if _wants_new_search(text):
            await _reply(
                phone,
                "Search for a model — e.g. *iPhone 15* or *Samsung A54*\n\n"
                "Type *MANUAL* to add a custom product.\n"
                "Type *SKIP* to finish now and add products later.",
            )
            collected.pop("catalogue_results", None)
            _save_session(phone, "PRODUCT_SEARCH", collected)
            return

        if _wants_manual(text):
            await _reply(phone, "What is the *name* of the product?")
            _save_session(phone, "PRODUCT_NAME", collected)
            return

        if _wants_skip(text):
            await _complete_registration(phone, collected)
            return

        # Try to parse a selection number
        try:
            choice = int(text.strip()) - 1
        except ValueError:
            await _reply(
                phone,
                "Please reply with a *number* from the list above.\n"
                "Or type *NEW SEARCH* to search again, or *MANUAL* to add manually.",
            )
            return

        results = collected.get("catalogue_results", [])
        if choice < 0 or choice >= len(results):
            await _reply(
                phone,
                f"Please pick a number between 1 and {len(results)}.",
            )
            return

        picked = results[choice]
        collected["current_product"] = {
            "name": picked["title"],
            "recommended_price": picked.get("price_min"),
            "source": "catalogue",
        }

        price_line = (
            f"\n💡 Recommended retail price: {_fmt_price(picked['price_min'])}\n"
            if picked.get("price_min") else "\n"
        )

        await _reply(
            phone,
            f"📱 *{picked['title']}*{price_line}\n"
            "What price will *you* charge your customers?\n"
            "_(e.g. 650000 or 650k)_",
        )
        _save_session(phone, "PRODUCT_PRICE_CAT", collected)
        return

    # ── PRODUCT_PRICE_CAT (catalogue path) ────────────────────────────────────
    if state == "PRODUCT_PRICE_CAT":
        price = _parse_price(text)
        if price is None:
            rec = collected.get("current_product", {}).get("recommended_price")
            hint = f" _(recommended: {_fmt_price(rec)})_" if rec else ""
            await _reply(phone, f"Please enter a valid price{hint} — e.g. *650000* or *650k*.")
            return
        collected["current_product"]["price"] = price
        await _reply(
            phone,
            "📦 How many do you have in stock?\n_(Enter a number or reply *UNLIMITED*)_",
        )
        _save_session(phone, "PRODUCT_STOCK", collected)
        return

    # ── PRODUCT_NAME (manual path) ────────────────────────────────────────────
    if state == "PRODUCT_NAME":
        if len(text) < 1:
            await _reply(phone, "Please enter the product name.")
            return
        collected["current_product"] = {"name": text, "source": "manual"}
        await _reply(
            phone,
            f"💰 What is the *price* of *{text}*?\n_(Numbers only — e.g. 12500)_",
        )
        _save_session(phone, "PRODUCT_PRICE", collected)
        return

    # ── PRODUCT_PRICE (manual path) ───────────────────────────────────────────
    if state == "PRODUCT_PRICE":
        price = _parse_price(text)
        if price is None:
            await _reply(phone, "Please enter a valid price _(numbers only — e.g. 12500)_.")
            return
        collected["current_product"]["price"] = price
        pname = collected["current_product"].get("name", "this product")
        await _reply(
            phone,
            f"📦 How many *{pname}* do you have in stock?\n"
            "_(Enter a number or reply *UNLIMITED*)_",
        )
        _save_session(phone, "PRODUCT_STOCK", collected)
        return

    # ── PRODUCT_STOCK (shared by both paths) ─────────────────────────────────
    if state == "PRODUCT_STOCK":
        stock = _parse_stock(text)
        if stock is None:
            await _reply(phone, "Please enter a number _(e.g. 50)_ or reply *UNLIMITED*.")
            return
        collected["current_product"]["stock"] = stock
        source = collected.get("current_product", {}).get("source", "manual")

        if source == "catalogue":
            # Catalogue path: skip desc/image, go straight to confirm
            cp    = collected["current_product"]
            price_str = _fmt_price(cp.get("price", 0))
            stock_str = "Unlimited" if stock == 999999 else str(stock)
            await _reply(
                phone,
                f"Product preview:\n"
                f"• Model: {cp.get('name', '—')}\n"
                f"• Your price: {price_str}\n"
                f"• Stock: {stock_str}\n\n"
                "Save this product? Reply *YES* or *NO*",
            )
            _save_session(phone, "PRODUCT_CONFIRM", collected)
        else:
            # Manual path: collect description next
            pname = collected["current_product"].get("name", "this product")
            await _reply(
                phone,
                f"📝 Add a short *description* for *{pname}*?\n"
                "_(One line — or reply *SKIP*)_",
            )
            _save_session(phone, "PRODUCT_DESC", collected)
        return

    # ── PRODUCT_DESC (manual path only) ──────────────────────────────────────
    if state == "PRODUCT_DESC":
        collected["current_product"]["description"] = None if text.lower() == "skip" else text
        pname = collected["current_product"].get("name", "this product")
        await _reply(
            phone,
            f"📸 Send a *photo* of *{pname}*?\n"
            "_(Attach an image — or reply *SKIP*)_",
        )
        _save_session(phone, "PRODUCT_IMAGE", collected)
        return

    # ── PRODUCT_IMAGE (manual path only) ─────────────────────────────────────
    if state == "PRODUCT_IMAGE":
        if msg_type == "image" and media_url:
            collected["current_product"]["image_url"] = media_url
        else:
            collected["current_product"]["image_url"] = None

        cp        = collected["current_product"]
        price_str = _fmt_price(cp.get("price", 0))
        stock_str = "Unlimited" if cp.get("stock") == 999999 else str(cp.get("stock", 0))
        photo_str = "✅ Received" if cp.get("image_url") else "—"

        await _reply(
            phone,
            f"Product preview:\n"
            f"• Name: {cp.get('name', '—')}\n"
            f"• Price: {price_str}\n"
            f"• Stock: {stock_str}\n"
            f"• Description: {cp.get('description') or '—'}\n"
            f"• Photo: {photo_str}\n\n"
            "Save this product? Reply *YES* or *NO*",
        )
        _save_session(phone, "PRODUCT_CONFIRM", collected)
        return

    # ── PRODUCT_CONFIRM ───────────────────────────────────────────────────────
    if state == "PRODUCT_CONFIRM":
        if _yes(text):
            cp       = dict(collected.pop("current_product", {}))
            cp["category"] = collected.get("category")
            products = collected.get("products", [])
            products.append(cp)
            collected["products"] = products
            collected.pop("catalogue_results", None)

            await _reply(
                phone,
                f"✅ *{cp.get('name')}* saved!\n\nAdd another product? Reply *YES* or *NO*",
            )
            _save_session(phone, "ADD_MORE", collected)

        elif _no(text):
            collected.pop("current_product", None)
            collected.pop("catalogue_results", None)
            await _reply(
                phone,
                "No problem — let's try again.\n\n"
                "Search for a model — e.g. *iPhone 15* or *Samsung A54*\n\n"
                "Type *MANUAL* to add a custom product.\n"
                "Type *SKIP* to finish now and add products later.",
            )
            _save_session(phone, "PRODUCT_SEARCH", collected)
        else:
            await _reply(phone, "Please reply *YES* to save or *NO* to try again.")
        return

    # ── ADD_MORE ──────────────────────────────────────────────────────────────
    if state == "ADD_MORE":
        if _yes(text):
            await _reply(
                phone,
                "Search for a model — e.g. *iPhone 15* or *Samsung A54*\n\n"
                "Type *MANUAL* to add a custom product.\n"
                "Type *SKIP* to finish now and add products later.",
            )
            _save_session(phone, "PRODUCT_SEARCH", collected)
            return

        if _no(text):
            await _complete_registration(phone, collected)
            return

        await _reply(phone, "Please reply *YES* to add another product or *NO* to finish setup.")
        return

    # ── Fallback ──────────────────────────────────────────────────────────────
    step_labels = {
        "COLLECT_BIZ_NAME":    "business name",
        "COLLECT_CATEGORY":    "what you sell",
        "PRODUCT_SEARCH":      "product search (type a model name, MANUAL, or SKIP)",
        "PRODUCT_RESULTS":     "a number from the list, NEW SEARCH, or MANUAL",
        "PRODUCT_PRICE_CAT":   "your selling price (e.g. 650000 or 650k)",
        "PRODUCT_NAME":        "product name",
        "PRODUCT_PRICE":       "product price",
        "PRODUCT_STOCK":       "stock quantity (a number or UNLIMITED)",
        "PRODUCT_DESC":        "product description (or SKIP)",
        "PRODUCT_IMAGE":       "product photo (or SKIP)",
        "PRODUCT_CONFIRM":     "YES or NO to confirm product",
        "ADD_MORE":            "YES or NO to add another product",
    }
    step = step_labels.get(state, state)
    await _reply(
        phone,
        f"I'm waiting for your *{step}*.\n\n"
        "Please follow the prompts above, or reply *RESTART* to begin again.",
    )
