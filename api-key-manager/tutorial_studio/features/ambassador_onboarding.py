"""
Beats + Playwright actions for "Onboarding a Client" — the lead pipeline
both a sales ambassador (/ambassador/leads) and a sales manager
(/ambassador/team/pipeline) use to take a prospect from first contact to
paying, active client. Recorded on the ambassador view; the sales-manager
view (team_pipeline.html) is structurally identical (same stage funnel,
same Add Lead fields, same advance-modal per stage) so this one recording
serves both audiences.

Narration is trimmed from templates/ambassador/leads.html's own in-app
guided tour (the `ambTourInit([...])` script at the bottom of that file),
reusing its worked example ("Sunrise Fashion Store") for continuity between
the tour a user can replay in-app and this video.

Every action here is a real, safe write against the dedicated test
ambassador's own row (id=1, test@phixtra.com) — no external API (Meta,
payment gateway) is touched. The one field intentionally left alone: the
Active Client stage's optional tenant-link dropdown, kept on "Not linked
yet" so no real tenant record is touched. The test lead created during
recording is deleted afterward (see tutorial_studio's cleanup step) so the
account stays at 0 leads for the next re-recording.
"""
from playwright.sync_api import TimeoutError as PWTimeoutError

BASE_URL = "https://portal.phixtra.com"
DEMO_EMAIL = "test@phixtra.com"
DEMO_PASSWORD = "Demo1234!"

BEATS = {
    "landing": (
        "This is My Pipeline, where you track every business you bring onto "
        "Fixtra, from the moment you spot them to the day they become a "
        "paying, active client. Every lead moves left to right through seven "
        "stages: Lead, Contacted, Demo Done, Requirements Confirmed, "
        "Onboarding, Active Client, and Support."
    ),
    "open_form": (
        "Click Add Lead the moment you find a new prospect, don't wait "
        "until after you've contacted them."
    ),
    "fill_business": (
        "Give the business a name and industry, like Sunrise Fashion Store, "
        "Fashion Retail."
    ),
    "fill_contact": (
        "Add a contact name and phone number so you can follow up."
    ),
    "fill_notes": (
        "Add a quick note, then click Add to Pipeline. The lead appears at "
        "the top of your table, tagged as Lead."
    ),
    "advance_contacted": (
        "Once you've reached out, click the stage button and log how, "
        "WhatsApp, phone call, email, or in person, along with the date and "
        "a quick note on their response."
    ),
    "advance_demo": (
        "After showing the AI live, log the demo date and the prospect's "
        "reaction, so you remember exactly how the conversation went."
    ),
    "advance_requirements": (
        "Before onboarding, confirm all four requirements: a business-only "
        "phone number, a Meta Business Account, WhatsApp connected to Meta, "
        "and a product list ready to upload."
    ),
    "advance_onboarding": (
        "Once their account setup is underway, log the onboarding date and "
        "what you've done so far, like uploading products or sending login "
        "details."
    ),
    "advance_active": (
        "When they're live and paying, you can optionally link their "
        "Fixtra account from the dropdown. Commission is already automatic "
        "the moment they pay, so this step is purely for your own records."
    ),
    "advance_support": (
        "The final stage needs no extra info, just confirm, and the client "
        "moves into ongoing support tracking. You'll keep earning commission "
        "for as long as they stay subscribed."
    ),
    "history": (
        "Click History any time to see a full timeline of every stage "
        "change, who changed it, when, and any notes attached."
    ),
    "drop": (
        "If a business decides not to proceed, click Drop and optionally "
        "note why. They move to your Dropped list below, so you keep the "
        "record without cluttering your active pipeline."
    ),
}


def login(browser, video_dir):
    login_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    login_page = login_ctx.new_page()
    login_page.goto(f"{BASE_URL}/ambassador/login", wait_until="networkidle")
    login_page.fill("input[name=email]", DEMO_EMAIL)
    login_page.fill("input[name=password]", DEMO_PASSWORD)
    login_page.click("button[type=submit]")
    try:
        login_page.wait_for_url(f"{BASE_URL}/ambassador/dashboard", timeout=15000)
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
    # Skip this page's auto-starting in-app tour so it doesn't pop up mid-recording.
    ctx.add_init_script("localStorage.setItem('phixtra_amb_tour_my_pipeline', '1');")
    page = ctx.new_page()
    return page, ctx


def _confirm_advance_modal(page):
    page.wait_for_selector("#advanceModal.open", timeout=5000)
    page.click("#advanceModal .modal-confirm")
    page.wait_for_load_state("networkidle")


def record(page, hold, mark, beat_ms):
    # --- landing: land on My Pipeline, camera holds on the stage funnel ---
    page.goto(f"{BASE_URL}/ambassador/leads", wait_until="networkidle", timeout=20000)
    hold("landing")

    # --- open_form: reveal the Add Lead form ---
    page.click('[data-tour="add-lead-btn"]')
    hold("open_form")

    # --- fill_business: name + industry ---
    page.fill("#addLeadForm input[name=business_name]", "Sunrise Fashion Store")
    page.fill("#addLeadForm input[name=industry]", "Fashion Retail")
    hold("fill_business")

    # --- fill_contact: contact name, phone, email ---
    page.fill("#addLeadForm input[name=contact_name]", "Amaka Eze")
    page.fill("#addLeadForm input[name=phone]", "2348012345678")
    page.fill("#addLeadForm input[name=email]", "amaka@sunrisefashion.ng")
    hold("fill_contact")

    # --- fill_notes: note + submit, page reloads with the new lead at top ---
    page.fill("#addLeadForm textarea[name=notes]", "Interested, wants pricing info")
    page.click("#addLeadForm button[type=submit]")
    page.wait_for_load_state("networkidle")
    hold("fill_notes")

    # --- advance_contacted ---
    page.click('[data-tour="advance-btn"]')
    page.wait_for_selector("#advanceModal.open", timeout=5000)
    page.select_option("#advFields select[name=contact_channel]", value="whatsapp")
    page.fill("#advFields input[name=contact_date]", "2026-07-05")
    page.fill(
        "#advFields textarea[name=contact_response]",
        "Sent an intro message on WhatsApp, they replied asking for a demo next week.",
    )
    _confirm_advance_modal(page)
    hold("advance_contacted")

    # --- advance_demo ---
    page.click('[data-tour="advance-btn"]')
    page.wait_for_selector("#advanceModal.open", timeout=5000)
    page.fill("#advFields input[name=demo_date]", "2026-07-08")
    page.fill(
        "#advFields textarea[name=demo_reaction]",
        "Loved the AI replying instantly to customer questions, asked how much it costs.",
    )
    _confirm_advance_modal(page)
    hold("advance_demo")

    # --- advance_requirements ---
    page.click('[data-tour="advance-btn"]')
    page.wait_for_selector("#advanceModal.open", timeout=5000)
    for name in ("req_phone", "req_meta_account", "req_whatsapp_connected", "req_product_list"):
        page.check(f"#advFields input[name={name}]")
    _confirm_advance_modal(page)
    hold("advance_requirements")

    # --- advance_onboarding ---
    page.click('[data-tour="advance-btn"]')
    page.wait_for_selector("#advanceModal.open", timeout=5000)
    page.fill("#advFields input[name=onboarding_date]", "2026-07-12")
    page.fill(
        "#advFields textarea[name=onboarding_notes]",
        "Uploaded 40 products, connected WhatsApp number, sent login details to the owner.",
    )
    _confirm_advance_modal(page)
    hold("advance_onboarding")

    # --- advance_active: leave the tenant-link dropdown on "Not linked yet" ---
    page.click('[data-tour="advance-btn"]')
    page.wait_for_selector("#advanceModal.open", timeout=5000)
    _confirm_advance_modal(page)
    hold("advance_active")

    # --- advance_support: final stage, no extra fields ---
    page.click('[data-tour="advance-btn"]')
    _confirm_advance_modal(page)
    hold("advance_support")

    # --- history: open the stage timeline ---
    page.click('[data-tour="history-btn"]')
    page.wait_for_selector("#historyModal.open", timeout=5000)
    hold("history")
    page.click("#historyModal .modal-cancel")

    # --- drop: drop the lead with a reason (cleanup deletes the row after) ---
    page.click('[data-tour="drop-btn"]')
    page.wait_for_selector("#dropModal.open", timeout=5000)
    page.fill("#dropForm textarea[name=reason]", "Demo lead for the tutorial video")
    page.click("#dropForm .modal-confirm")
    page.wait_for_load_state("networkidle")
    hold("drop")
