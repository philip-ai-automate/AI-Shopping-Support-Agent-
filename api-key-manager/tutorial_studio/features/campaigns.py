"""
Beats + Playwright actions for the Campaigns "New Campaign" wizard tutorial
video, on portal.phixtra.com's demo merchant account. Ported from the
heygen_poc scratchpad (v2, beat-paced) — see tutorial_studio/lib.py for the
shared machinery this module plugs into.

Never clicks "Launch Campaign" — mirrors screenshot_tutorial.py's safety
rule of not firing a real send against fake Meta credentials; the final
beat hovers the button instead of clicking it.
"""
import json

from playwright.sync_api import TimeoutError as PWTimeoutError

BASE_URL = "https://portal.phixtra.com"
DEMO_EMAIL = "demo@phixtra.com"
DEMO_PASSWORD = "Demo1234!"

MOCK_TEMPLATES = [
    {"name": "flash_sale_promo", "language": "en", "category": "MARKETING", "header_type": "IMAGE"},
    {"name": "order_confirmation", "language": "en", "category": "UTILITY", "header_type": ""},
    {"name": "restock_alert", "language": "en", "category": "MARKETING", "header_type": "TEXT"},
]

# Beat order = playback order. Narration text = tutorials.html's
# guide-campaigns copy, chunked to one micro-action per beat instead of
# one wizard-step per block (v1 had 4 blocks; that's what caused the
# alignment problem this v2 pacing fixes).
BEATS = {
    "b1_landing": (
        "This is the Campaigns page. The tiles above track your total "
        "campaigns, completions, and messages sent."
    ),
    "b1_open": (
        "Click New Campaign in the top right. A setup panel slides in with "
        "three steps: Campaign Info, Recipients, and Schedule and Launch."
    ),
    "b2_name": (
        "Give the campaign a clear name, like May Flash Sale, so you can "
        "find it later."
    ),
    "b2_template": (
        "Then pick one of your Meta-approved WhatsApp templates from the "
        "dropdown. Only templates already approved in Meta Business Suite "
        "show up here."
    ),
    "b2_header": (
        "If the template has an image, video, document, or text header, a "
        "matching field appears automatically. Fill it in or upload the "
        "file before continuing."
    ),
    "b2_next": "Once everything looks good, click Continue.",
    "b3_recipients": (
        "Paste one phone number per line, including the country code, with "
        "no plus sign or spaces. The counter in the corner confirms how "
        "many numbers you've entered."
    ),
    "b3_compliance": (
        "Only message customers who've opted in to hear from your business "
        "on WhatsApp. Sending to unverified numbers can get your account "
        "flagged by Meta."
    ),
    "b3_next": "Click Continue to move to the final step.",
    "b4_timing": (
        "Choose Send Now to launch right away, or Schedule to pick a "
        "specific date and time."
    ),
    "b4_review": (
        "The review and confirm summary repeats your campaign name, "
        "template, recipient count, and send time, so you can double-check "
        "everything."
    ),
    "b4_launch": (
        "Click Launch Campaign once it looks right. There's no further "
        "confirmation after that, so this is the step to slow down on."
    ),
}


def _mock_templates_route(route):
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(MOCK_TEMPLATES),
    )


def login(browser, video_dir):
    login_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    login_page = login_ctx.new_page()
    login_page.goto(f"{BASE_URL}/login", wait_until="networkidle")
    login_page.fill("input[name=email]", DEMO_EMAIL)
    login_page.fill("input[name=password]", DEMO_PASSWORD)
    login_page.click("button[type=submit]")
    try:
        login_page.wait_for_url(f"{BASE_URL}/dashboard", timeout=15000)
    except PWTimeoutError:
        raise RuntimeError(f"LOGIN FAILED — current URL: {login_page.url}")
    storage_state = login_ctx.storage_state()
    login_ctx.close()

    # service_workers="block" is required: the portal registers an active
    # sw.js (scope "/") that otherwise intercepts fetches before Playwright's
    # network layer sees them, silently defeating page.route() mocks.
    ctx = browser.new_context(
        viewport={"width": 1440, "height": 900},
        storage_state=storage_state,
        record_video_dir=str(video_dir),
        record_video_size={"width": 1440, "height": 900},
        service_workers="block",
    )
    ctx.add_init_script("localStorage.setItem('phixtra_tour_done', '1');")
    page = ctx.new_page()
    page.route("**/whatsapp/campaigns/templates-json", _mock_templates_route)
    return page, ctx


def record(page, hold, mark, beat_ms):
    # --- b1_landing: land on Campaigns page, camera holds on the tiles ---
    page.goto(f"{BASE_URL}/whatsapp/campaigns", wait_until="networkidle", timeout=20000)
    hold("b1_landing")

    # --- b1_open: open the New Campaign drawer ---
    page.click("button.btn-new-camp")
    hold("b1_open")

    # --- b2_name: name the campaign ---
    page.fill("#f-name", "May Flash Sale")
    hold("b2_name")

    # --- b2_template: pick a template ---
    page.wait_for_selector(
        "#f-template option[value='flash_sale_promo']", state="attached", timeout=5000
    )
    page.select_option("#f-template", value="flash_sale_promo")
    hold("b2_template")

    # --- b2_header: fill the header field the template requires ---
    page.fill("#f-header-image", "https://profitbuyz.com/images/promo-banner.jpg")
    hold("b2_header")

    # --- b2_next: advance to Recipients ---
    page.click("#btnNext")
    hold("b2_next")

    # --- b3_recipients: paste recipient numbers ---
    page.fill(
        "#f-recipients",
        "2348012345678\n2347098765432\n2348123456789\n2348198765432",
    )
    hold("b3_recipients")

    # --- b3_compliance: no new action, camera holds on the opt-in warning ---
    page.locator(".warn-box").last.scroll_into_view_if_needed()
    hold("b3_compliance")

    # --- b3_next: advance to Schedule & Launch ---
    page.click("#btnNext")
    hold("b3_next")

    # --- b4_timing: demonstrate both send-timing options ---
    half = beat_ms["b4_timing"] // 2
    page.click("#optSched")
    page.wait_for_timeout(half)
    page.click("#optNow")
    page.wait_for_timeout(beat_ms["b4_timing"] - half)
    mark("b4_timing")

    # --- b4_review: no new action, camera holds on the review summary ---
    page.locator("#rv-time").scroll_into_view_if_needed()
    hold("b4_review")

    # --- b4_launch: hover the Launch button, never click it (fake Meta creds) ---
    page.hover("#btnLaunch")
    hold("b4_launch")
