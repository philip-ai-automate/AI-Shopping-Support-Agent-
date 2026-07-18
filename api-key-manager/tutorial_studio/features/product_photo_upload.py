"""
Beats + Playwright actions for "Add a Product Photo" — the one-off manual
"+ Add Product" form (`/products/add`), specifically its image upload field.

This is the answer for merchants with no existing website or spreadsheet of
image URLs (e.g. most fashion/apparel sellers): upload a photo straight from
your device, no hosting, no CSV column mapping. Distinct from
`product_import.py` (bulk spreadsheet import — needs image URLs already
hosted somewhere) and `catalogue_onboarding.py`'s in-wizard manual-add beat
(name/price/category only, no photo). The uploaded photo is what the AI later
attaches inline on WhatsApp for Apparel & Fashion product recommendations.

Client/tenant view throughout — logs in as the merchant, not admin.

Narration is trimmed from product_form.html's own field labels/placeholders
and the products.html page copy, per this project's convention of reusing
in-app copy rather than writing new narration from scratch.

Uses the same disposable "Zemich Boutique" test tenant identity as the other
client-view videos for visual continuity — provisioning is idempotent
(returns the existing tenant if already created by another video's run), so
no teardown is required here; the test product added during recording is
deleted at the end of `record()` so re-running this script stays repeatable.
"""
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeoutError

from tutorial_studio.lib import click_visibly, fill_visibly, highlight, move_to, type_visibly

BASE_URL = "https://portal.phixtra.com"
TEST_BUSINESS_NAME = "Zemich Boutique"
TEST_PHONE_PLAIN = "2348033334444"
TEST_PHONE_NORMALISED = "+2348033334444"
TEST_PRODUCT_NAME = "Ankara Midi Dress"

ASSET_DIR = Path(__file__).parent / "assets"

BEATS = {
    "intro": (
        "Adding a single product only takes a minute, and it's the easiest "
        "way to get a real photo in front of customers. You'll find My "
        "Products under Ecommerce in the sidebar."
    ),
    "click_add": (
        "Click Add Product to open a blank product form."
    ),
    "name_field": (
        "Give it a clear name. Customers see this exact text on WhatsApp."
    ),
    "price_stock": (
        "Set your price in Naira, and how many you currently have in stock."
    ),
    "category": (
        "Pick a category so Fixtra organizes it correctly in your catalogue."
    ),
    "description": (
        "A short description helps your AI answer questions the photo "
        "alone can't."
    ),
    "upload_photo": (
        "Now the part that matters most for anything customers judge by "
        "look — upload an actual photo from your device. No website, no "
        "hosting, no spreadsheet needed."
    ),
    "submit": (
        "Click Add Product to save, and it's instantly part of your live "
        "catalogue."
    ),
    "result": (
        "Your product is saved, photo included, and ready for your AI to "
        "show customers directly inside WhatsApp."
    ),
}


def provision_test_merchant():
    """Run once before generate_voice/record. Creates the test tenant and a
    synthetic wa_tenants row so the dashboard shows "connected". Idempotent —
    safe to call even if another video already provisioned this tenant."""
    import sys
    sys.path.insert(0, "/root/phixtra-app/api-key-manager")
    from portal_routes import provision_whatsapp_merchant
    from db import get_db_connection

    result = provision_whatsapp_merchant(TEST_PHONE_NORMALISED, TEST_BUSINESS_NAME)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM wa_tenants WHERE tenant_id = %s", (result["tenant_id"],))
    if not cur.fetchone():
        cur.execute("""
            INSERT INTO wa_tenants
                (tenant_id, phone_number_id, access_token, waba_id, verify_token,
                 phixtra_api_key, display_phone_number, signup_method)
            VALUES (%s, 'tutorial-test-phone-id', 'tutorial-test-token', 'tutorial-test-waba',
                    'tutorial-test-verify', 'tutorial-test-key', %s, 'manual')
        """, (result["tenant_id"], TEST_PHONE_NORMALISED))
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


def _cleanup_test_product():
    """Delete the product this recording adds, so re-running the script
    doesn't pile up duplicate "Ankara Midi Dress" rows in the test tenant."""
    import sys
    sys.path.insert(0, "/root/phixtra-app/api-key-manager")
    from db import get_db_connection
    from portal_routes import _synthetic_email, _normalise_phone

    conn = get_db_connection()
    cur = conn.cursor()
    synth_email = _synthetic_email(_normalise_phone(TEST_PHONE_NORMALISED))
    cur.execute("SELECT tenant_id FROM customers WHERE email = %s LIMIT 1", (synth_email,))
    row = cur.fetchone()
    if row:
        cur.execute(
            "DELETE FROM products WHERE tenant_id = %s AND name = %s",
            (row[0], TEST_PRODUCT_NAME),
        )
        conn.commit()
    cur.close()
    conn.close()


def record(page, hold, mark, beat_ms):
    _cleanup_test_product()  # in case a previous run's product is still there

    # --- intro: dashboard -> Ecommerce (expand) -> My Products ---
    page.goto(f"{BASE_URL}/dashboard", wait_until="networkidle", timeout=20000)
    click_visibly(page, ".sb-group-toggle[data-tour='nav-ecommerce']")
    click_visibly(page, "a:has-text('My Products')")
    page.wait_for_load_state("networkidle")
    hold("intro")

    # --- click_add: the header "+ Add Product" button, newly added to the
    # page so merchants actually have a way in ---
    click_visibly(page, "a:has-text('+ Add Product')")
    page.wait_for_load_state("networkidle")
    hold("click_add")

    # --- name_field ---
    type_visibly(page, "#name", TEST_PRODUCT_NAME)
    hold("name_field")

    # --- price_stock: two fields, one beat ---
    type_visibly(page, "#price", "18500")
    type_visibly(page, "#stock_quantity", "12")
    hold("price_stock")

    # --- category: datalist-suggested text input ---
    type_visibly(page, "#category", "Clothing")
    hold("category")

    # --- description ---
    fill_visibly(page, "#description", "Vibrant ankara print midi dress, true to size.")
    hold("description")

    # --- upload_photo: highlight the file input, then inject the file
    # (Playwright's file chooser has no visible dialog to animate) ---
    move_to(page, "input[name=image_file]")
    highlight(page, "input[name=image_file]", ms=900)
    page.wait_for_timeout(500)
    page.set_input_files("input[name=image_file]", str(ASSET_DIR / "sample-product-photo.jpg"))
    page.wait_for_timeout(400)  # lets the onchange preview image render
    hold("upload_photo", motion=["#uploadPreview"])

    # --- submit: saves, redirects to My Products ---
    click_visibly(page, "button:has-text('Add Product')")
    page.wait_for_load_state("networkidle")
    hold("submit", motion=[".flash.success"])

    # --- result: camera holds on the new row, thumbnail + name ---
    page.locator(f"tr:has-text('{TEST_PRODUCT_NAME}')").first.scroll_into_view_if_needed()
    hold("result", motion=[f"tr:has-text('{TEST_PRODUCT_NAME}')"])

    _cleanup_test_product()
