"""
Beats + Playwright actions for "Onboarding Continuation — Setup Wizard":
the catalogue-selection wizard a merchant runs after connecting WhatsApp
(continuing the story from `whatsapp_merchant_onboarding.py`), so their AI
has real products to answer customer questions about. Uses the Electronics
department → Laptops + Mobile Phones categories as the worked example.

Client/tenant view throughout — logs in as the merchant, not admin.

v2: the first cut only showed picking products from the shared catalogue
and stopped there. Two real gaps, fixed here:
  1. Never showed that products can be added manually — there are two
     paths: bulk spreadsheet/Google Sheets import (`/data-sources`, linked
     from the category product page), and a one-off "+ Add product" form
     right on the review page. Both are now shown.
  2. Never visited Store Information (`/onboarding/store-info`) — the very
     next step the wizard redirects to after "Launch My Store", covering
     About Us, Delivery, Returns & Refunds, etc. Skipping it means the AI
     has nothing to go on for exactly those questions and will guess. Now
     shown with an explicit narration of why it matters.

Narration is trimmed from tutorials.html's own "Ecommerce & Catalogue" and
"Product Import" guide copy, plus the wizard pages' own copy.

Every action here is a real write against a dedicated test tenant — pure DB
inserts, no external calls. The test tenant (business name + WhatsApp
number reused from the connect-flow video for narrative continuity) is
provisioned before recording and deleted after, including a synthetic
`wa_tenants` row added purely so the dashboard shows "connected" (this
video picks up from there, it doesn't re-demonstrate the connect flow).
"""
from playwright.sync_api import TimeoutError as PWTimeoutError

from tutorial_studio.lib import click_visibly, fill_visibly, type_visibly

BASE_URL = "https://portal.phixtra.com"
TEST_BUSINESS_NAME = "Zemich Boutique"
TEST_PHONE_PLAIN = "2348033334444"
TEST_PHONE_NORMALISED = "+2348033334444"

ELECTRONICS_DEPT_ID = 1
LAPTOPS_CAT_ID = 1
MOBILE_PHONES_CAT_ID = 11

BEATS = {
    "dashboard_recap": (
        "Now that WhatsApp is connected, the next step is telling your AI "
        "what you actually sell. From your dashboard, click Run Setup Wizard."
    ),
    "click_wizard": (
        "This opens the catalogue setup wizard: three quick steps, business "
        "type, categories, then products."
    ),
    "pick_department": (
        "Start by choosing your business type. We'll pick Electronics."
    ),
    "pick_categories": (
        "Electronics breaks down into categories like Laptops and Mobile "
        "Phones. Pick every category you actually sell."
    ),
    "product_import": (
        "Prefer not to pick from the shared catalogue? Click Product Import "
        "to bulk-upload your own spreadsheet or connect Google Sheets "
        "instead. Download the sample template first to see the exact "
        "columns needed."
    ),
    "laptops_search_select": (
        "Back in the wizard, search the shared catalogue by brand or "
        "model. This is real product data your AI already understands. "
        "Click Add Product on anything you stock."
    ),
    "phones_search_select": (
        "Do the same for Mobile Phones. Search, then add the models you "
        "carry."
    ),
    "review_selections": (
        "The review page groups every product you picked by category, so "
        "you can double check everything before going live."
    ),
    "manual_add_product": (
        "You can also add a one-off product by hand right here, anything "
        "that isn't in the shared catalogue. Give it a name, price, and "
        "category, then save."
    ),
    "launch_store": (
        "Click Launch My Store, and everything you picked becomes "
        "something your AI can immediately answer questions about. No "
        "coding, no waiting on a developer."
    ),
    "store_info_intro": (
        "Launching takes you straight to Store Information. This matters "
        "just as much as your products — without it, your AI has nothing "
        "to go on for questions like delivery, returns, or who you are, "
        "and it will guess."
    ),
    "store_info_fill": (
        "Fill in About Us, Delivery Information, and Returns and Refunds "
        "at minimum, then save. The AI reads this directly the moment a "
        "customer asks anything outside your product list."
    ),
    "wrap_up": (
        "With products and store details both in place, your AI can now "
        "answer specific questions instead of guessing, and your "
        "onboarding is complete."
    ),
}


def provision_test_merchant():
    """Run once before generate_voice/record. Creates the test tenant and a
    synthetic wa_tenants row so the dashboard shows "connected" — this
    video picks up after the connect flow, it doesn't re-demonstrate it."""
    import sys
    sys.path.insert(0, "/root/phixtra-app/api-key-manager")
    from portal_routes import provision_whatsapp_merchant
    from db import get_db_connection

    result = provision_whatsapp_merchant(TEST_PHONE_NORMALISED, TEST_BUSINESS_NAME)

    conn = get_db_connection()
    cur = conn.cursor()
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
    # video) — this recording starts on the dashboard, picking up the story
    # after WhatsApp is connected.
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


def _add_products(page, category_id, count=2):
    buttons = page.locator(f"button.btn-add[data-category-id='{category_id}']")
    n = min(count, buttons.count())
    for i in range(n):
        click_visibly(page, f"button.btn-add[data-category-id='{category_id}']")
        page.wait_for_timeout(400)


def record(page, hold, mark, beat_ms):
    # --- dashboard_recap: no action — camera holds on the connected hero ---
    page.goto(f"{BASE_URL}/dashboard", wait_until="networkidle", timeout=20000)
    page.locator("#tour-wa-hero").scroll_into_view_if_needed()
    hold("dashboard_recap", motion=["#tour-wa-hero"])

    # --- click_wizard: open the catalogue setup wizard ---
    click_visibly(page, "a:has-text('Run Setup Wizard')")
    page.wait_for_load_state("networkidle")
    hold("click_wizard", motion=[f"#dc-{ELECTRONICS_DEPT_ID}"])

    # --- pick_department: choose Electronics, advance ---
    click_visibly(page, f"#dc-{ELECTRONICS_DEPT_ID}")
    click_visibly(page, "#nextBtn")
    page.wait_for_load_state("networkidle")
    hold("pick_department", motion=[f"#cl-{LAPTOPS_CAT_ID}"])

    # --- pick_categories: choose Laptops + Mobile Phones, advance (lands on Laptops product page) ---
    click_visibly(page, f"#cl-{LAPTOPS_CAT_ID}")
    click_visibly(page, f"#cl-{MOBILE_PHONES_CAT_ID}")
    click_visibly(page, "#nextBtn")
    page.wait_for_load_state("networkidle")
    hold("pick_categories", motion=["input[name=q]"])

    # --- product_import: show the bulk spreadsheet/Google Sheets import option, then return ---
    # (not clicking the in-page "Product Import" link — it collides with a
    # same-named sidebar nav entry that intercepts pointer events)
    page.goto(f"{BASE_URL}/data-sources", wait_until="networkidle", timeout=20000)
    hold("product_import", motion=["text=Download Sample Template", "#dropZone"])
    page.go_back()
    page.wait_for_load_state("networkidle")

    # --- laptops_search_select: search, add 2 products, next category ---
    type_visibly(page, "input[name=q]", "Dell")
    click_visibly(page, "button:has-text('Search')")
    page.wait_for_load_state("networkidle")
    _add_products(page, LAPTOPS_CAT_ID, count=2)
    click_visibly(page, "button:has-text('Next Category')")
    page.wait_for_load_state("networkidle")
    hold("laptops_search_select", motion=["input[name=q]"])

    # --- phones_search_select: search, add 2 products, go to review ---
    type_visibly(page, "input[name=q]", "iPhone")
    click_visibly(page, "button:has-text('Search')")
    page.wait_for_load_state("networkidle")
    _add_products(page, MOBILE_PHONES_CAT_ID, count=2)
    click_visibly(page, "button:has-text('Review My Selections')")
    page.wait_for_load_state("networkidle")
    hold("phones_search_select", motion=[".summary-bar"])

    # --- review_selections: no new action — drift across the summary bar
    # and grouped category sections instead of a fully static hold ---
    hold("review_selections", motion=[".summary-bar", ".cat-section"])

    # --- manual_add_product: one-off custom product, typed by hand ---
    click_visibly(page, "#toggleCustomForm")
    type_visibly(page, "#cpName", "HP EliteBook Refurbished 840 G5")
    type_visibly(page, "#cpPrice", "185000")
    type_visibly(page, "#cpStock", "5")
    type_visibly(page, "#cpCat", "Laptops")
    type_visibly(page, "#cpDesc", "Refurbished business laptop, 3-month warranty")
    click_visibly(page, "button:has-text('Save product')")
    page.wait_for_timeout(600)
    hold("manual_add_product")

    # --- launch_store: finish the wizard, redirects straight to Store Information ---
    click_visibly(page, "button:has-text('Launch My Store')")
    page.wait_for_load_state("networkidle")
    hold("launch_store", motion=[".wizard-header"])

    # --- store_info_intro: no action — camera holds on the "why this
    # matters" notice box instead of a dead static shot ---
    hold("store_info_intro", motion=[".ai-notice"])

    # --- store_info_fill: fill the 3 sections the merchant most needs, save.
    # fill_visibly (highlight + instant fill), not type_visibly — these are
    # full paragraphs, and character-by-character typing would run far
    # longer than the beat's narration and get cut off by build_video()'s
    # audio-length mux. ---
    fill_visibly(
        page,
        "textarea[name=about_us]",
        "Zemich Boutique is Lagos's trusted electronics retailer, specialising in "
        "laptops and mobile phones from top brands, all backed by genuine warranties.",
    )
    fill_visibly(
        page,
        "textarea[name=delivery]",
        "We deliver within Lagos in 1-2 business days and nationwide in 3-5 business "
        "days. Delivery fees are calculated at checkout based on your location.",
    )
    fill_visibly(
        page,
        "textarea[name=returns]",
        "Items can be returned within 7 days of delivery if unopened and in original "
        "packaging. Contact us first to start a return.",
    )
    click_visibly(page, "button:has-text('Save & Go to Dashboard')")
    page.wait_for_load_state("networkidle")
    # Redirects to onboarding_wa_connect, not straight to /dashboard — this
    # test tenant already has a synthetic wa_tenants row, so it renders the
    # "WhatsApp already connected" confirmation card, not the connect form.
    hold("store_info_fill", motion=[".wizard-header", "text=WhatsApp already connected"])

    # --- wrap_up: clean final shot back on the dashboard ---
    page.goto(f"{BASE_URL}/dashboard", wait_until="networkidle", timeout=20000)
    hold("wrap_up", motion=["#tour-wa-hero"])
