"""
Beats + Playwright actions for "Product Import" — the standalone bulk
catalogue-import feature under the Ecommerce sidebar menu (`/data-sources`,
nav label "🗂️ Product Import"). Distinct from the per-category catalogue
picker wizard (`catalogue_onboarding.py`) and the one-off manual "+ Add
product" form — this is the spreadsheet upload -> column mapping -> sync
flow, reachable any time from Ecommerce -> Product Import.

Client/tenant view throughout — logs in as the merchant, not admin.

CSV/Excel-only walkthrough: Google Sheets import is skipped deliberately.
`_google_oauth_configured()` requires GOOGLE_OAUTH_CLIENT_ID/SECRET env
vars that aren't set in this environment, and even when configured it's a
real OAuth redirect to Google's consent screen — not something a synthetic
test tenant can drive headlessly. The current `data_sources.html` template
also has no rendered "Connect Google Sheets" entry point at all (the
`.connect-card` CSS exists but is unused markup), so there's nothing to
click through to on this page anyway.

Narration is trimmed from the page's own header copy, the File Format
Guide card, the "How it works" 3-step card, and tutorials.html's
`guide-data-sources` section, per this project's convention of reusing
in-app copy rather than writing new narration from scratch.

Uses the same disposable "Zemich Boutique" test tenant identity as the
other client-view videos for visual continuity — provisioned fresh before
recording (synthetic wa_tenants row so the dashboard shows "connected")
and deleted after, self-contained and independent of any other video.
"""
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeoutError

from tutorial_studio.lib import click_visibly, highlight, move_to, type_visibly

BASE_URL = "https://portal.phixtra.com"
TEST_BUSINESS_NAME = "Zemich Boutique"
TEST_PHONE_PLAIN = "2348033334444"
TEST_PHONE_NORMALISED = "+2348033334444"

ASSET_DIR = Path(__file__).parent / "assets"

BEATS = {
    "intro": (
        "Product Import brings your entire catalogue in from a "
        "spreadsheet, keeping your AI's pricing and stock accurate. "
        "You'll find it under Ecommerce in the sidebar."
    ),
    "format_guide": (
        "Your spreadsheet needs five columns in the first row: Name, "
        "Price, Description, Category, and Stock."
    ),
    "sample_download": (
        "Not sure where to start? Download the sample template and fill "
        "it in with your own products."
    ),
    "choose_file": (
        "Choose your file, Excel or CSV, it doesn't matter. Fixtra reads "
        "your headers the moment you pick it."
    ),
    "upload_click": (
        "Click Upload and Map Columns, and it jumps straight into "
        "matching your file to the right fields."
    ),
    "map_columns": (
        "Match each column in your file to a product field. Product Name "
        "and Price are required, everything else is optional."
    ),
    "preview_check": (
        "The preview on the right confirms Fixtra is reading your file "
        "correctly before anything gets imported."
    ),
    "save_import": (
        "Click Save Mapping and Import, and your products sync "
        "immediately, no extra confirmation step."
    ),
    "synced_result": (
        "Your catalogue is live. Re-run the sync any time you update the "
        "file, and your AI always has accurate stock and pricing to work "
        "from."
    ),
}


def provision_test_merchant():
    """Run once before generate_voice/record. Creates the test tenant and a
    synthetic wa_tenants row so the dashboard shows "connected"."""
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
    # --- intro: dashboard -> Ecommerce (expand) -> Product Import ---
    page.goto(f"{BASE_URL}/dashboard", wait_until="networkidle", timeout=20000)
    click_visibly(page, ".sb-group-toggle[data-tour='nav-ecommerce']")
    click_visibly(page, "a:has-text('Product Import')")
    page.wait_for_load_state("networkidle")
    hold("intro")

    # --- format_guide: no action of its own — drift across the guide's own
    # column rows in narration order instead of a dead static hold ---
    page.locator("text=File Format Guide").scroll_into_view_if_needed()
    hold(
        "format_guide",
        motion=["tr:has-text('Name')", "tr:has-text('Price')", "tr:has-text('Stock')"],
    )

    # --- sample_download: hover the download link (not clicked — real file
    # download, would leave a stray file in the recording environment) ---
    page.locator("text=Download Sample Template").scroll_into_view_if_needed()
    hold("sample_download", motion=["text=Download Sample Template"])

    # --- choose_file: highlight the drop zone, then inject the file
    # (Playwright's file chooser has no visible dialog to animate) ---
    move_to(page, "#dropZone")
    highlight(page, "#dropZone", ms=900)
    page.wait_for_timeout(500)
    page.set_input_files("#fileInput", str(ASSET_DIR / "product-import-sample.csv"))
    hold("choose_file")

    # --- upload_click: submit, redirects to the column-mapping page —
    # motion re-establishes the cursor on the new document after the reload ---
    click_visibly(page, "#uploadBtn")
    page.wait_for_load_state("networkidle")
    hold("upload_click", motion=[".page-header", ".map-card:has-text('Column Mapping')"])

    # --- map_columns: name the source, then move/highlight/select each
    # column mapping in turn instead of instant, invisible select_option calls ---
    type_visibly(page, "input[name=display_name]", "Zemich Boutique Catalogue")
    for field, value in [
        ("col_name", "name"),
        ("col_price", "price"),
        ("col_category", "category"),
        ("col_description", "description"),
        ("col_stock", "stock"),
    ]:
        selector = f"select[name={field}]"
        move_to(page, selector)
        highlight(page, selector, ms=550)
        page.wait_for_timeout(250)
        page.select_option(selector, value)
    hold("map_columns")

    # --- preview_check: no action — drift across the 3 sample rows ---
    page.locator("text=File Preview").scroll_into_view_if_needed()
    hold(
        "preview_check",
        motion=[
            ".preview-table tbody tr:nth-child(1)",
            ".preview-table tbody tr:nth-child(2)",
            ".preview-table tbody tr:nth-child(3)",
        ],
    )

    # --- save_import: submit — saves mapping, triggers sync, redirects back.
    # No .flash on this page (data_sources.html has none) — the new source
    # card itself is the only on-screen confirmation, so that's the motion
    # target for the post-reload hold. ---
    click_visibly(page, "button:has-text('Save Mapping')")
    page.wait_for_load_state("networkidle")
    hold("save_import", motion=[".source-card:has-text('Zemich Boutique Catalogue')"])

    # --- synced_result: camera holds on the new source card, ✓ Synced badge ---
    page.locator("text=Zemich Boutique Catalogue").scroll_into_view_if_needed()
    hold(
        "synced_result",
        motion=[".source-card:has-text('Zemich Boutique Catalogue')", ".status-badge"],
    )
