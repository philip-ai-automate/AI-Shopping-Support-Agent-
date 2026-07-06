"""
Beats + Playwright actions for "WhatsApp Merchant Onboarding" — this is the
CLIENT/tenant's own experience, not the admin provisioning tool. A merchant
was already provisioned (business name + WhatsApp number, e.g. via the
admin Onboard WA tool) and this video picks up from there: their first
login at portal.phixtra.com/wa-login, landing on a dashboard that isn't
connected yet, and the WhatsApp Connect page where they finish setup.

(An earlier version of this file drove the *admin* provisioning tool
instead and was wrong — tutorials are for clients, not staff. Corrected to
log in as the tenant throughout.)

Narration is trimmed from tutorials.html's own "WhatsApp Connect" guide
section (steps 1-2: Connect with WhatsApp button, manual fallback).

Login OTP: `wa_login_send()` calls the real Meta Cloud API
(`_send_wa_otp`), but a synthetic test phone number isn't a real WhatsApp
account, so the send fails and the route falls back to showing the code
directly in the page's flash message (`portal_routes.py` — "WhatsApp
delivery not yet configured — your code for testing is: ..."). This
recording reads that code straight off the page instead of a real
WhatsApp message, so it never depends on an actual delivery.

Never clicks "Connect with WhatsApp" (real Meta OAuth popup) or submits
the manual-connect form (would attempt to validate fake credentials
against Meta) — only hovers/expands to show what's there, same safety
pattern as never clicking Campaigns' "Launch Campaign".

Requires a pre-provisioned test WA merchant tenant (see
`provision_test_merchant()` below, run once before generate_voice/record;
tutorial_studio's cleanup step deletes it afterward).
"""
from playwright.sync_api import TimeoutError as PWTimeoutError

BASE_URL = "https://portal.phixtra.com"
TEST_BUSINESS_NAME = "Zemich Boutique"
TEST_PHONE_PLAIN = "2348033334444"       # what a merchant types into the phone field
TEST_PHONE_NORMALISED = "+2348033334444"  # how _normalise_phone stores it

BEATS = {
    "login_arrive": (
        "This is how a new WhatsApp merchant gets into portal dot Fixtra "
        "dot com for the first time. Enter the WhatsApp number you signed "
        "up with, no password needed."
    ),
    "request_code": (
        "Click Send Code, and a 6 digit verification code is sent straight "
        "to that WhatsApp number."
    ),
    "verify_code": (
        "Enter the code you received, then verify to log in."
    ),
    "dashboard_status": (
        "Since this is a fresh account, the dashboard shows WhatsApp isn't "
        "connected yet. That's the one thing left before their AI assistant "
        "goes live."
    ),
    "click_connect": (
        "Click Connect WhatsApp to open the setup page."
    ),
    "signup_button": (
        "The one-click Connect with WhatsApp button opens a Meta popup that "
        "walks them through choosing their Business Account and phone "
        "number, no copying and pasting credentials."
    ),
    "manual_expand": (
        "If Embedded Signup isn't available yet, Having Trouble, Connect "
        "Manually Instead reveals fields for the Phone Number ID, WABA ID, "
        "and access token, found in Meta's own dashboard."
    ),
    "webhook_info": (
        "The Webhook URL and Verify Token just below are ready to copy "
        "straight into Meta, no guessing what to paste where."
    ),
    "wrap_up": (
        "Once connected, the dashboard turns green and live, and their AI "
        "assistant starts replying to customers automatically."
    ),
}


def provision_test_merchant():
    """Run once before generate_voice/record — creates the test tenant this
    recording logs into. Cleanup (tutorial_studio's usual pattern) deletes
    it by business name/phone afterward."""
    import sys
    sys.path.insert(0, "/root/phixtra-app/api-key-manager")
    from portal_routes import provision_whatsapp_merchant
    return provision_whatsapp_merchant(TEST_PHONE_NORMALISED, TEST_BUSINESS_NAME)


def login(browser, video_dir):
    # No pre-authenticated storage_state here — the login itself (OTP flow)
    # is the first thing this video shows, so recording starts before login.
    ctx = browser.new_context(
        viewport={"width": 1440, "height": 900},
        record_video_dir=str(video_dir),
        record_video_size={"width": 1440, "height": 900},
    )
    # This is a brand-new tenant, so the 13-step guided-tour overlay
    # (project_demo_system) would auto-start on first dashboard visit and
    # intercept every click behind it — skip it, same as campaigns.py does.
    ctx.add_init_script("localStorage.setItem('phixtra_tour_done', '1');")
    page = ctx.new_page()
    return page, ctx


def _read_latest_otp():
    # The dev-fallback flash (code shown on-page when WhatsApp delivery
    # fails) doesn't always fire — Meta's API can accept the send call even
    # for a synthetic test number. Reading wa_portal_otp directly is robust
    # either way: it's the same code the merchant would read off WhatsApp.
    import sys
    sys.path.insert(0, "/root/phixtra-app/api-key-manager")
    from db import get_db_connection
    import psycopg2.extras

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT otp_code FROM wa_portal_otp WHERE phone=%s AND used=FALSE "
        "ORDER BY id DESC LIMIT 1",
        (TEST_PHONE_NORMALISED,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise RuntimeError(f"No unused OTP found for {TEST_PHONE_NORMALISED}")
    return row["otp_code"]


def record(page, hold, mark, beat_ms):
    # --- login_arrive: land on wa-login, enter the WhatsApp number ---
    page.goto(f"{BASE_URL}/wa-login", wait_until="networkidle", timeout=20000)
    page.fill("input[name=phone]", TEST_PHONE_PLAIN)
    hold("login_arrive")

    # --- request_code: submit, land on the verify page ---
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    otp = _read_latest_otp()
    hold("request_code")

    # --- verify_code: enter the code, submit, arrive at the dashboard ---
    page.fill("input[name=code]", otp)
    page.click("button[type=submit]")
    try:
        page.wait_for_url(f"{BASE_URL}/dashboard", timeout=15000)
    except PWTimeoutError:
        raise RuntimeError(f"OTP verification failed — current URL: {page.url}")
    hold("verify_code")

    # --- dashboard_status: camera holds on the "not connected" WhatsApp hero ---
    page.locator("#tour-wa-hero").scroll_into_view_if_needed()
    hold("dashboard_status")

    # --- click_connect: go to the WhatsApp Connect page ---
    page.click("a:has-text('Connect WhatsApp')")
    page.wait_for_load_state("networkidle")
    hold("click_connect")

    # --- signup_button: hover the one-click button, never click (real Meta OAuth) ---
    page.locator("#signupBtn").hover()
    hold("signup_button")

    # --- manual_expand: expand the manual-connect fallback ---
    page.click("#manualSetupDetails summary")
    hold("manual_expand")

    # --- webhook_info: camera holds on the webhook URL / verify token fields ---
    page.locator("#webhookUrlBox").scroll_into_view_if_needed()
    hold("webhook_info")

    # --- wrap_up: no new action, closing narration ---
    hold("wrap_up")
