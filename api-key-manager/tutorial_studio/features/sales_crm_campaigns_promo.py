"""
Beats + Playwright actions for the lead-facing marketing video: WhatsApp
Campaigns (the hero feature — bulk messaging is the main attraction for
prospects) + Sales Pipeline CRM (secondary, the payoff once a campaign
gets replies), closing on a "free while slots last" offer. Unlike the
other tutorial_studio features (which teach an existing customer how to
use a feature), this one is a sales pitch meant to be forwarded to
prospects — pacing and narration are punchier, and it opens/closes on
full-screen graphic cards (assets/promo_pain_points.html,
assets/promo_free_offer.html, assets/promo_cta.html) instead of more app
screens.

Recorded on the standard demo merchant account (demo@phixtra.com, tenant
85 — Growth plan, feat_broadcasts already unlocked), same account
campaigns.py uses. A handful of disposable Sales Pipeline leads and one
completed WhatsApp campaign are seeded directly into the DB before
recording and deleted after — the demo tenant has 0 pipeline leads and 0
campaigns between recordings, so nothing pre-existing is touched.

Never actually launches a real campaign (fake/shared Meta creds) — the
wizard is driven up to Recipients only; the "campaign sent" visual comes
from the pre-seeded 'done' row in the campaign history table instead,
same safety rule campaigns.py follows for its own Launch button.

Uses a female voice override ("Georgia - Lifelike - Broadcaster") for
this video only, per user request — doesn't change lib.py's project-wide
default, same per-feature-override mechanism discount_settings.py uses.
"""
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeoutError

from tutorial_studio.lib import click_visibly, type_visibly, highlight, move_to

BASE_URL = "https://portal.phixtra.com"
DEMO_EMAIL = "demo@phixtra.com"
DEMO_PASSWORD = "Demo1234!"

VOICE_ID = "596d780fd5874d7983847b6a0e0c49e6"  # "Georgia - Lifelike - Broadcaster" — female, confident

ASSETS_DIR = Path(__file__).parent / "assets"

MOCK_TEMPLATES = [
    {"name": "flash_sale_promo", "language": "en", "category": "MARKETING", "header_type": "IMAGE"},
    {"name": "restock_alert", "language": "en", "category": "MARKETING", "header_type": "TEXT"},
]

# Sequential-reveal graphic cards driven by page.evaluate() calls (not CSS
# delays) so each beat's visual reveal is exactly synced to its own
# HeyGen audio duration regardless of how long that turns out to be.
PAIN_CARD = f"file://{ASSETS_DIR / 'promo_pain_points.html'}"
FREE_OFFER_CARD = f"file://{ASSETS_DIR / 'promo_free_offer.html'}"
CTA_CARD = f"file://{ASSETS_DIR / 'promo_cta.html'}"

BEATS = {
    "pain_1": (
        "Got two hundred customers to tell about a sale? That's two "
        "hundred messages — one by one, by hand."
    ),
    "pain_2": (
        "Copy, paste, send. Copy, paste, send. All day."
    ),
    "pain_3": (
        "By the time you're done, the sale's half over."
    ),
    "pain_4": (
        "And once someone replies, good luck remembering who's still "
        "interested."
    ),
    "turn": "There's a better way.",
    "campaigns_hero": (
        "Meet WhatsApp Campaigns — one message, your whole list, "
        "seconds instead of hours."
    ),
    "campaigns_compose": (
        "Pick your template, add your message, and Fixtra handles the rest."
    ),
    "bridge": (
        "Send to everyone in your Sales Pipeline with one click — no "
        "copying numbers by hand."
    ),
    "history": (
        "Launch it, and it's tracked right here — sent, scheduled, or "
        "running, all in one place."
    ),
    "pipeline_intro": (
        "And every contact you message becomes a tracked deal in your "
        "Sales Pipeline CRM — automatically."
    ),
    "pipeline_advance": (
        "Move a deal forward the moment it happens. Nothing slips "
        "through the cracks."
    ),
    "free_offer": (
        "Right now, both WhatsApp Campaigns and the Sales Pipeline CRM "
        "are completely free."
    ),
    "free_offer_urgency": (
        "But free slots are limited. Once they're gone, they're gone."
    ),
    "cta": (
        "Reserve your free spot now. Send the word RESERVE on WhatsApp "
        "to the number on your screen."
    ),
}


def _mock_templates_route(route):
    import json
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

    ctx = browser.new_context(
        viewport={"width": 1440, "height": 900},
        storage_state=storage_state,
        record_video_dir=str(video_dir),
        record_video_size={"width": 1440, "height": 900},
        service_workers="block",
    )
    ctx.add_init_script(
        "localStorage.setItem('phixtra_tour_done', '1');"
        "localStorage.setItem('phixtra_portal_tour_sales_pipeline', '1');"
    )
    page = ctx.new_page()
    page.route("**/whatsapp/campaigns/templates-json", _mock_templates_route)
    return page, ctx


def record(page, hold, mark, beat_ms):
    # --- pain_1..pain_4: full-screen problem card, one point revealed per beat ---
    page.goto(PAIN_CARD)
    page.evaluate("showPoint(1)")
    hold("pain_1")
    page.evaluate("showPoint(2)")
    hold("pain_2")
    page.evaluate("showPoint(3)")
    hold("pain_3")
    page.evaluate("showPoint(4)")
    hold("pain_4")

    # --- turn: swap to the "better way" reveal on the same card ---
    page.evaluate("showTurn()")
    hold("turn")

    # --- campaigns_hero: land on Campaigns, camera settles on New Campaign ---
    page.goto(f"{BASE_URL}/whatsapp/campaigns", wait_until="networkidle", timeout=20000)
    hold("campaigns_hero", motion=["button.btn-new-camp"])

    # --- campaigns_compose: open the wizard, quick step 1, advance to Recipients ---
    click_visibly(page, "button.btn-new-camp")
    page.wait_for_selector("#f-name", state="visible", timeout=5000)
    type_visibly(page, "#f-name", "July Restock Blast")
    page.wait_for_selector("#f-template option[value='flash_sale_promo']", state="attached", timeout=5000)
    move_to(page, "#f-template")
    page.select_option("#f-template", value="flash_sale_promo")
    highlight(page, "#f-template", ms=1000)
    page.fill("#f-header-image", "https://profitbuyz.com/images/promo-banner.jpg")
    click_visibly(page, "#btnNext")
    hold("campaigns_compose", motion=["#f-segment"])

    # --- bridge: pull Sales Pipeline leads straight into the recipient list ---
    click_visibly(page, "#f-segment")
    page.select_option("#f-segment", value="__pipeline__")
    page.wait_for_function(
        "document.getElementById('f-recipients').value.trim().length > 0",
        timeout=8000,
    )
    hold("bridge", motion=["#f-recipients", "#recipCounter"])

    # --- history: close the drawer, reveal the campaign history table ---
    click_visibly(page, "button.drawer-close")
    page.wait_for_timeout(300)
    page.locator("#reports table.camp-table").scroll_into_view_if_needed()
    hold("history", motion=[
        "#reports table.camp-table tbody tr:first-child td:nth-child(1)",
        "#reports table.camp-table tbody tr:first-child .badge",
        "#reports table.camp-table tbody tr:first-child td:nth-child(5)",
    ])

    # --- pipeline_intro: land on Sales Pipeline, camera drifts across the funnel ---
    page.goto(f"{BASE_URL}/sales-pipeline", wait_until="networkidle", timeout=20000)
    hold("pipeline_intro", motion=[
        ".stage-chip:nth-of-type(1)", ".stage-chip:nth-of-type(3)", ".stage-chip:nth-of-type(6)",
    ])

    # --- pipeline_advance: move "Lagos Electronics Hub" to the next stage ---
    row_sel = "tr.lead-row:has-text('Lagos Electronics Hub')"
    click_visibly(page, f"{row_sel} .lead-actions button.btn-primary")
    page.wait_for_selector("#advanceModal.open", timeout=5000)
    # The Qualified-stage modal has a required date field — native HTML5
    # validation silently blocks submit (no page nav, no visible error)
    # if it's left empty.
    move_to(page, "#advanceModal input[name=qualified_date]")
    highlight(page, "#advanceModal input[name=qualified_date]", ms=900)
    page.fill("#advanceModal input[name=qualified_date]", "2026-07-24")
    click_visibly(page, "#advanceModal .modal-confirm")
    page.wait_for_load_state("networkidle")
    hold("pipeline_advance", motion=[
        "tr.lead-row:has-text('Lagos Electronics Hub') .stage-pill",
    ])

    # --- free_offer / free_offer_urgency: full-screen offer card (self-animating) ---
    page.goto(FREE_OFFER_CARD)
    hold("free_offer")
    hold("free_offer_urgency")

    # --- cta: full-screen reserve-your-spot card (self-animating) ---
    page.goto(CTA_CARD)
    hold("cta")
