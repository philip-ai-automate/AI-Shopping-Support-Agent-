"""
Beats + Playwright actions for "Store Information" — the standalone
`/store-info` settings page (not the brief stop inside the onboarding
wizard already shown in `catalogue_onboarding.py`'s `store_info_intro`/
`store_info_fill` beats). This is the permanent, always-available page a
merchant returns to any time to update what their AI knows: About Us,
Delivery, Returns & Refunds, Contact, Payment Methods, FAQs, and Other —
plus the document-upload path (PDF/Word/text) as an alternative to typing.

Client/tenant view throughout — logs in as the merchant, not admin.

Narration is trimmed from the page's own "How this works" AI-notice copy
and from tutorials.html's "Store Information" guide section
(`guide-store-info`), per this project's convention of reusing existing
in-app copy rather than writing new narration from scratch.

Uses the same disposable "Zemich Boutique" test tenant identity as
`whatsapp_merchant_onboarding.py` and `catalogue_onboarding.py` for visual
continuity across the tutorial library — provisioned fresh before
recording (including a synthetic `wa_tenants` row so the dashboard shows
"connected") and deleted after, this video does not depend on either of
those other videos having been recorded first.
"""
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeoutError

from tutorial_studio.lib import click_visibly, fill_visibly, move_to, type_visibly

BASE_URL = "https://portal.phixtra.com"
TEST_BUSINESS_NAME = "Zemich Boutique"
TEST_PHONE_PLAIN = "2348033334444"
TEST_PHONE_NORMALISED = "+2348033334444"

ASSET_DIR = Path(__file__).parent / "assets"

BEATS = {
    "intro": (
        "Store Information is what your Fixtra AI Sales Agent reads to "
        "answer customer questions accurately, no guessing, no "
        "hallucinating. You'll find it any time from the sidebar."
    ),
    "how_it_works": (
        "Everything you add here goes straight into your AI's knowledge "
        "base. Ask it something like, do you deliver to Lagos, or what's "
        "your return policy, and it answers from your real store data, "
        "not a guess. Type information directly, or upload a document."
    ),
    "upload_doc": (
        "Drop in a PDF, Word, or text file, and Fixtra extracts the "
        "details automatically. Here's a warranty policy document being "
        "uploaded and indexed."
    ),
    "doc_indexed": (
        "Within a few minutes it's automatically indexed, that green "
        "badge confirms your AI can already read it."
    ),
    "fill_about_us": (
        "Start with About Us: tell customers who you are and what makes "
        "your store special."
    ),
    "fill_delivery": (
        "Add Delivery Information: the areas you cover, estimated times, "
        "and any delivery costs."
    ),
    "fill_returns": (
        "Then Returns and Refunds: how long customers have, what's "
        "eligible, and how to start a return."
    ),
    "fill_contact": (
        "Contact Information: your phone number, email, address, and "
        "opening hours."
    ),
    "fill_payment": (
        "Payment Methods: every option you accept, bank transfer, card, "
        "or cash on delivery."
    ),
    "fill_faqs": (
        "FAQs: the common questions your customers actually ask."
    ),
    "fill_custom": (
        "And Other Information, for anything else your AI or your "
        "customers should know."
    ),
    "save": (
        "Click Save Store Information, and your AI starts using every "
        "detail on its very next reply, no waiting, no redeploying."
    ),
    "wrap_up": (
        "With your store details in place, your AI answers specific "
        "questions instead of guessing, and it updates instantly every "
        "time you come back and change something here."
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


def _fill_section(page, hold, field, text, beat_id):
    fill_visibly(page, f"textarea[name={field}]", text)
    hold(beat_id)


def record(page, hold, mark, beat_ms):
    # --- intro: land on dashboard, navigate to Store Information from the sidebar ---
    page.goto(f"{BASE_URL}/dashboard", wait_until="networkidle", timeout=20000)
    click_visibly(page, "a.sb-link:has-text('Store Information')")
    page.wait_for_load_state("networkidle")
    page.locator(".si-header").scroll_into_view_if_needed()
    hold("intro", motion=[".si-header"])

    # --- how_it_works: no action — camera holds on the AI-notice explainer box ---
    page.locator(".ai-notice").scroll_into_view_if_needed()
    hold("how_it_works", motion=[".ai-notice"])

    # --- upload_doc: fill title + pick file, submit ---
    type_visibly(page, "input[name=doc_title]", "Warranty Policy")
    move_to(page, "input[name=doc_file]")
    page.set_input_files("input[name=doc_file]", str(ASSET_DIR / "warranty-policy.txt"))
    click_visibly(page, "button:has-text('Upload & Index')")
    page.wait_for_load_state("networkidle")
    hold("upload_doc", motion=[".doc-row"])

    # --- doc_indexed: no new action — camera holds on the uploaded doc row
    # with its indexed badge ---
    page.locator(".doc-row", has_text="Warranty Policy").scroll_into_view_if_needed()
    hold("doc_indexed", motion=[".doc-row:has-text('Warranty Policy')"])

    # --- fill each text section in page order, save at the end ---
    _fill_section(
        page, hold, "about_us",
        "Zemich Boutique is Lagos's trusted electronics retailer, specialising in "
        "laptops and mobile phones from top brands, all backed by genuine warranties.",
        "fill_about_us",
    )
    _fill_section(
        page, hold, "delivery",
        "We deliver within Lagos in 1-2 business days and nationwide in 3-5 business "
        "days. Delivery fees are calculated at checkout based on your location.",
        "fill_delivery",
    )
    _fill_section(
        page, hold, "returns",
        "Items can be returned within 7 days of delivery if unopened and in original "
        "packaging. Contact us first to start a return.",
        "fill_returns",
    )
    _fill_section(
        page, hold, "contact",
        "Reach us on WhatsApp any time, or call +234 803 333 4444 between 9am and 6pm, "
        "Monday to Saturday. Showroom: 14 Adeola Odeku Street, Victoria Island, Lagos.",
        "fill_contact",
    )
    _fill_section(
        page, hold, "payment",
        "We accept bank transfer, debit or credit card, and cash on delivery within "
        "Lagos. Orders outside Lagos require full payment before dispatch.",
        "fill_payment",
    )
    _fill_section(
        page, hold, "faqs",
        "Q: New or refurbished? Both, clearly labelled on every listing. Q: Can I "
        "inspect before paying? Yes, for Lagos pickup orders only.",
        "fill_faqs",
    )
    _fill_section(
        page, hold, "custom",
        "We offer a 10% trade-in discount when you swap an old laptop or phone "
        "toward a new purchase. Message us for a free valuation.",
        "fill_custom",
    )

    # --- save: submit all sections at once ---
    click_visibly(page, "button:has-text('Save Store Information')")
    page.wait_for_load_state("networkidle")
    hold("save", motion=[".si-header"])

    # --- wrap_up: no new action — clean final shot back on the saved page ---
    page.locator(".si-header").scroll_into_view_if_needed()
    hold("wrap_up", motion=[".si-header", ".ai-notice"])
