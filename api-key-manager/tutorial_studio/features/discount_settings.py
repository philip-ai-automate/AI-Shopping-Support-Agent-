"""
Beats + Playwright actions for "Discount Settings" — `/discount-settings`
(nav label "💰 Discount Settings"), under the Ecommerce sidebar submenu.
Three independent layers: storewide Discount Mode (who's allowed to
negotiate), a Default Discount applied to every product, and Per-Product
Discount Overrides for specific items.

Client/tenant view throughout — logs in as the merchant, not admin.

Merchant-facing settings only: there is no route/worker in this repo that
actually consumes wa_merchant_settings/wa_product_discounts against a live
WhatsApp conversation (confirmed by a repo-wide grep — the "customers
reply DISCOUNT" behaviour described in the page's own copy lives outside
this codebase), so this video only demonstrates configuring the rules, not
an AI applying one in a real chat.

Section 3 (Per-Product Discount Overrides) reads from the `documents`
table (`id LIKE 'product-%'`, `price_min > 0`) — a table normally
populated only by the real WooCommerce sync path, which this synthetic
WhatsApp-only test tenant never has. No in-app route can seed it, so
`provision_test_merchant()` inserts 3 rows directly, the same
direct-DB-insert workaround already used for the synthetic `wa_tenants`
row in every other feature in this project.

Narration is trimmed from the page's own "How WhatsApp Discounts Work"
info box and tutorials.html's `guide-discount-settings` section, per this
project's convention of reusing in-app copy.

Voice: uses a different HeyGen voice (Okafor, Nigerian-accented) via the
per-feature VOICE_ID override added to lib.py — user-requested for this
video specifically, not a change to the project-wide default.
"""
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeoutError

from tutorial_studio.lib import click_visibly, move_to, type_visibly

BASE_URL = "https://portal.phixtra.com"
TEST_BUSINESS_NAME = "Zemich Boutique"
TEST_PHONE_PLAIN = "2348033334444"
TEST_PHONE_NORMALISED = "+2348033334444"

VOICE_ID = "483f791c833e425a8f561bd42719712d"  # "Okafor" — Nigerian-accented male

BEATS = {
    "intro": (
        "Discount Settings controls how discounts are offered to "
        "customers on WhatsApp. You'll find it under Ecommerce in the "
        "sidebar."
    ),
    "how_it_works": (
        "Merchant Only means your AI never offers a discount, any "
        "request goes straight to you. AI Then Merchant lets your AI "
        "offer your configured discount automatically, then bring you in "
        "if the customer wants more. Customers trigger this by simply "
        "replying DISCOUNT during checkout."
    ),
    "select_mode": (
        "Choose who's allowed to give discounts. Let's turn on AI Then "
        "Merchant, so your AI handles the first offer itself."
    ),
    "save_mode": (
        "Click Save Mode, and it applies immediately."
    ),
    "default_discount": (
        "Set a Default Discount that applies to every product unless "
        "you add an override below, ten percent off, store-wide."
    ),
    "save_default": (
        "Save Default Discount, and your AI now offers this "
        "automatically, based on the mode you picked above."
    ),
    "product_overrides_intro": (
        "Need a different rate on one item? Every product from your "
        "store shows up here for a one-off override."
    ),
    "set_override": (
        "Set a custom discount type and value for just this product, "
        "then save, it overrides the default for this item only."
    ),
    "wrap_up": (
        "Your discount rules are live. Mode, a sensible default, and "
        "per-product overrides give you full control over every "
        "WhatsApp negotiation."
    ),
}


def provision_test_merchant():
    """Run once before generate_voice/record. Creates the test tenant, a
    synthetic wa_tenants row so the dashboard shows "connected", and 3
    synthetic `documents` rows so Section 3's per-product table isn't
    empty (no in-app route populates that table for a WhatsApp-only
    tenant — see module docstring)."""
    import sys
    sys.path.insert(0, "/root/phixtra-app/api-key-manager")
    from portal_routes import provision_whatsapp_merchant
    from db import get_db_connection

    result = provision_whatsapp_merchant(TEST_PHONE_NORMALISED, TEST_BUSINESS_NAME)
    tenant_id = result["tenant_id"]

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO wa_tenants
            (tenant_id, phone_number_id, access_token, waba_id, verify_token,
             phixtra_api_key, display_phone_number, signup_method)
        VALUES (%s, 'tutorial-test-phone-id', 'tutorial-test-token', 'tutorial-test-waba',
                'tutorial-test-verify', 'tutorial-test-key', %s, 'manual')
    """, (tenant_id, TEST_PHONE_NORMALISED))

    products = [
        ("product-101", "HP EliteBook 840 G5 Refurbished", 185.00),
        ("product-102", "Dell Inspiron 15 3000", 320.00),
        ("product-103", "Samsung Galaxy A54", 210.00),
    ]
    for doc_id, title, price in products:
        cur.execute("""
            INSERT INTO documents (id, tenant_id, type, title, price_min)
            VALUES (%s, %s, 'product', %s, %s)
        """, (doc_id, tenant_id, title, price))

    conn.commit()
    cur.close()
    conn.close()
    return result


def login(browser, video_dir):
    # Off-screen login (OTP flow already shown in whatsapp_merchant_onboarding's
    # video) — this recording starts on the dashboard.
    import sys
    sys.path.insert(0, "/root/phixtra-app/api-key-manager")
    from db import get_db_connection
    import psycopg2.extras

    login_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    login_page = login_ctx.new_page()
    login_page.goto(f"{BASE_URL}/wa-login", wait_until="networkidle")
    login_page.fill("input[name=phone]", TEST_PHONE_PLAIN)
    login_page.click("button[type=submit]")
    login_page.wait_for_load_state("networkidle")

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT otp_code FROM wa_portal_otp WHERE phone=%s AND used=FALSE "
        "ORDER BY id DESC LIMIT 1",
        (TEST_PHONE_NORMALISED,),
    )
    otp = cur.fetchone()["otp_code"]
    cur.close()
    conn.close()

    login_page.fill("input[name=code]", otp)
    login_page.click("button[type=submit]")
    try:
        login_page.wait_for_url(f"{BASE_URL}/dashboard", timeout=15000)
    except PWTimeoutError:
        raise RuntimeError(f"LOGIN FAILED — current URL: {login_page.url}")
    storage_state = login_ctx.storage_state()
    login_ctx.close()

    ctx = browser.new_context(
        viewport={"width": 1440, "height": 900},
        storage_state=storage_state,
        record_video_dir=str(video_dir),
        record_video_size={"width": 1440, "height": 900},
    )
    # Brand-new tenant — skip the auto-starting guided-tour overlay.
    ctx.add_init_script("localStorage.setItem('phixtra_tour_done', '1');")
    page = ctx.new_page()
    return page, ctx


def record(page, hold, mark, beat_ms):
    # --- intro: dashboard -> Ecommerce (expand) -> Discount Settings ---
    page.goto(f"{BASE_URL}/dashboard", wait_until="networkidle", timeout=20000)
    click_visibly(page, ".sb-group-toggle[data-tour='nav-ecommerce']")
    click_visibly(page, "a:has-text('Discount Settings')")
    page.wait_for_load_state("networkidle")
    hold("intro")

    # --- how_it_works: no state change here, so instead of freezing on a
    # static shot for the full narration, the cursor drifts across the
    # Merchant Only / AI Then Merchant / DISCOUNT-keyword lines as they're
    # each mentioned, matching the "how_it_works" narration order.
    page.locator(".how-box").scroll_into_view_if_needed()
    hold(
        "how_it_works",
        motion=[".how-box p:nth-of-type(1)", ".how-box p:nth-of-type(2)", ".how-box p:nth-of-type(3)"],
    )

    # --- select_mode: move to and click the AI Then Merchant mode card ---
    page.locator(".mode-grid").scroll_into_view_if_needed()
    click_visibly(page, "label.mode-card:has-text('AI Then Merchant')")
    hold("select_mode")

    # --- save_mode: submit, page reloads with a flash confirming the save.
    # The reload wipes the cursor overlay (a fresh document = no mousemove
    # yet), so without motion here the hold would be a dead freeze even
    # though the page state genuinely changed — settle the cursor on the
    # flash message, then back on the mode card, so it stays visible.
    click_visibly(page, "button:has-text('Save Mode')")
    page.wait_for_load_state("networkidle")
    hold("save_mode", motion=[".flash", "label.mode-card:has-text('AI Then Merchant')"])

    # --- default_discount: set a 10% storewide default ---
    page.locator("select[name=default_discount_type]").scroll_into_view_if_needed()
    move_to(page, "select[name=default_discount_type]")
    page.select_option("select[name=default_discount_type]", "percent")
    type_visibly(page, "input[name=default_discount_value]", "10")
    hold("default_discount")

    # --- save_default: submit (same post-reload cursor gap as save_mode) ---
    click_visibly(page, "button:has-text('Save Default Discount')")
    page.wait_for_load_state("networkidle")
    hold("save_default", motion=[".flash", "input[name=default_discount_value]"])

    # --- product_overrides_intro: no state change — drift the cursor down
    # the seeded products table rows instead of a dead static hold ---
    page.locator(".prod-wrap").scroll_into_view_if_needed()
    hold(
        "product_overrides_intro",
        motion=[
            "tr:has-text('Dell Inspiron 15 3000')",
            "tr:has-text('HP EliteBook 840 G5 Refurbished')",
            "tr:has-text('Samsung Galaxy A54')",
        ],
    )

    # --- set_override: flat ₦15,000 off the HP EliteBook row only, save ---
    row = page.locator("tr", has_text="HP EliteBook 840 G5 Refurbished")
    move_to(page, "tr:has-text('HP EliteBook 840 G5 Refurbished') select[name=discount_type]")
    row.locator("select[name=discount_type]").select_option("flat")
    type_visibly(
        page,
        "tr:has-text('HP EliteBook 840 G5 Refurbished') input[name=discount_value]",
        "15000",
    )
    click_visibly(page, "tr:has-text('HP EliteBook 840 G5 Refurbished') button:has-text('Save')")
    page.wait_for_load_state("networkidle")
    # No .flash here — wa_discount_product_save() redirects without setting
    # one (unlike the mode/default-discount saves above), so the row itself
    # is the only confirmation on screen.
    hold("set_override", motion=["tr:has-text('HP EliteBook 840 G5 Refurbished')"])

    # --- wrap_up: no state change — drift the cursor across the three
    # settings just configured (mode, default, override row) while the
    # closing narration recaps them, instead of a fully static final shot ---
    page.locator(".ds-header").scroll_into_view_if_needed()
    hold(
        "wrap_up",
        motion=[
            ".ds-header",
            "label.mode-card:has-text('AI Then Merchant')",
            "input[name=default_discount_value]",
        ],
    )
