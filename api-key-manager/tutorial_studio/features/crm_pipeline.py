"""
Beats + Playwright actions for the CRM & Sales Pipeline tutorial video, on
portal.phixtra.com's demo merchant account — see tutorial_studio/lib.py for
the shared machinery this module plugs into.

The most comprehensive video in the library by explicit request — full
depth on all 9 real destinations under the CRM story, not a highlight
reel: Leads, the Lead Command Centre, the Pipeline Board, All Contacts, a
Contact's own detail page, Companies, a Company's own detail page,
Segments, Tags, and Pipeline Settings.

Demo data note: demo tenant 85 had zero Companies and zero Segments before
this video was built — both are real, permanent features with nothing to
show without content. Added once, directly to the persistent demo account
(same "add anything freely" account every other tutorial video already
uses), not deleted after recording: a company "Ikorodu Plastics" (3 linked
contacts + 1 linked open deal, id 3598) and a segment "High-Value
Prospects" (4 members). Left in place on purpose — future presale demos on
this same account now have real Companies/Segments content too, not just
this video. See project_team_enablement_plan memory for the exact SQL.

Lead 3609 (Damilola Adebayo, already real, pre-existing) is used for the
Lead Command Centre / Contact Detail sections — a genuine WhatsApp-campaign-
sourced deal with real message history and a real stage-history note,
picked because it needed no setup at all.
"""
from playwright.sync_api import TimeoutError as PWTimeoutError

from tutorial_studio.lib import click_visibly

BASE_URL = "https://portal.phixtra.com"
DEMO_EMAIL = "demo@phixtra.com"
DEMO_PASSWORD = "Demo1234!"

BEATS = {
    # ── 1. Leads ─────────────────────────────────────────────────────────
    "b1_hot": (
        "This is Leads — where every deal starts. Hot Conversations "
        "surfaces WhatsApp chats that sound like a real buying signal, "
        "before anyone even turns them into a formal Lead."
    ),
    "b1_list": (
        "Below it, the real Leads list. Every one of these is scored "
        "automatically, out of a hundred — deal value against your own "
        "typical deal, plus how recently and how actively it's being "
        "worked."
    ),
    "b1_filter": (
        "Filter straight to Hot, Warm, or Cold. Hot means chase it today — "
        "nobody has to guess which leads matter most."
    ),

    # ── 2. Lead Command Centre ───────────────────────────────────────────
    "b2_open": (
        "Open any lead and you land on the Lead Command Centre — "
        "everything about this one deal, on a single page. This one came "
        "in from a real WhatsApp campaign reply."
    ),
    "b2_conversation": (
        "The Conversations panel is the actual WhatsApp thread with this "
        "customer, not a summary of it — every message, right here."
    ),
    "b2_notes": (
        "The Activity tab is where notes live — anything a member of "
        "staff needs the next person to know before they pick this deal "
        "up."
    ),
    "b2_history": (
        "And Stage History — a full, timestamped record of every move "
        "this deal has made. This one shows exactly how it got here: a "
        "customer replied to a campaign, and Fixtra logged it "
        "automatically."
    ),

    # ── 3. CRM → Pipeline Board ──────────────────────────────────────────
    "b3_list": (
        "Now, the Pipeline Board — every deal in the business, by stage. "
        "List view lays it out as a funnel: New Lead, Contacted, "
        "Qualified, Proposal Sent, Negotiating, Won."
    ),
    "b3_wonlost": (
        "A deal doesn't just disappear when it doesn't work out. Mark it "
        "Lost or Dropped with a reason, and it moves here — Closed — "
        "still on record, just out of the active pipeline."
    ),
    "b3_board": (
        "Board view is the same pipeline, laid out as columns instead — "
        "pick whichever way your team thinks."
    ),
    "b3_export": (
        "And everything on this board exports straight to CSV, whenever "
        "you need it outside Fixtra."
    ),

    # ── 4. CRM → All Contacts ────────────────────────────────────────────
    "b4_list": (
        "CRM, then All Contacts — every person who's ever messaged this "
        "business, in one searchable list, whether or not they've become "
        "a formal Lead yet."
    ),
    "b4_filters": (
        "Filters go well beyond a name search — status, tags, segment, "
        "when they were added, even whether their profile is actually "
        "complete enough to be useful."
    ),
    "b4_add": (
        "Add a contact by hand any time — useful for a walk-in customer "
        "who's never messaged on WhatsApp at all."
    ),

    # ── 5. Contact detail ────────────────────────────────────────────────
    "b5_deal": (
        "Open a contact and, if they have an open deal, it's right here "
        "as a Deal card — stage and value, without leaving the contact's "
        "own page."
    ),
    "b5_timeline": (
        "Underneath, one merged timeline — notes, stage moves, and real "
        "WhatsApp messages, all in the order they actually happened. No "
        "more checking two separate places."
    ),
    "b5_company": (
        "And when a contact belongs to a business, that business shows "
        "right here as a Company chip — one click away from everyone "
        "else who works there."
    ),

    # ── 6. CRM → Companies ───────────────────────────────────────────────
    "b6_grid": (
        "Companies groups contacts and deals by the business they "
        "actually belong to — useful the moment more than one person from "
        "the same company is talking to you."
    ),
    "b6_list": (
        "Switch to List view for the same data as a compact table instead "
        "of cards."
    ),
    "b6_detail": (
        "Open a company and see everyone who works there, every open "
        "deal tied to them, and the combined value at stake — all in one "
        "place."
    ),
    "b6_notes": (
        "Company-level notes live here too — context about the business "
        "itself, not tied to any one person."
    ),

    # ── 7. CRM → Segments ─────────────────────────────────────────────────
    "b7_list": (
        "Segments are audiences, built straight from your real Pipeline "
        "and Contacts data — no exporting a spreadsheet to build a "
        "campaign list by hand."
    ),
    "b7_detail": (
        "This one groups every contact behind a deal worth over one "
        "million naira — a real, live list, ready to send a WhatsApp "
        "campaign to directly."
    ),

    # ── 8. CRM → Tags ────────────────────────────────────────────────────
    "b8_list": (
        "Tags work across the whole CRM at once — one tag can mark both "
        "people and deals, and this page shows exactly how many of each."
    ),
    "b8_people": (
        "Open a tag and the People tab lists every contact carrying it."
    ),
    "b8_deals": (
        "Switch to Deals, and it's the same tag, but for Sales Pipeline "
        "leads instead — genuinely one shared tag vocabulary, not two "
        "separate systems that happen to look similar."
    ),

    # ── 9. CRM → Pipeline Settings ───────────────────────────────────────
    "b9_labels": (
        "Last stop, Pipeline Settings. Every stage name on that board — "
        "New Lead, Qualified, whatever you call it — is yours to rename, "
        "so the pipeline speaks your business's own language."
    ),
    "b9_close": (
        "That's the whole CRM — Leads in, a real Command Centre per deal, "
        "the Pipeline Board, Contacts, Companies, Segments, Tags, and full "
        "control over how it's all worded. One connected system, not six "
        "separate tools."
    ),
}


def login(browser, video_dir):
    login_ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
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
        viewport={"width": 1440, "height": 1000},
        storage_state=storage_state,
        record_video_dir=str(video_dir),
        record_video_size={"width": 1440, "height": 1000},
        service_workers="block",
    )
    # Sales Pipeline runs its OWN separate 13-step guided tour, keyed to a
    # different localStorage flag than the generic one — real bug caught on
    # the first real run: b3_wonlost/b3_board frames showed a "STEP 1 OF 13"
    # tour popup covering the pipeline table because only the generic flag
    # was being set. Both must be set to suppress every tour on this account.
    ctx.add_init_script(
        "localStorage.setItem('phixtra_tour_done', '1');"
        "localStorage.setItem('phixtra_portal_tour_sales_pipeline', '1');"
    )
    page = ctx.new_page()
    return page, ctx


def record(page, hold, mark, beat_ms):
    # ── 1. Leads ─────────────────────────────────────────────────────────
    page.goto(f"{BASE_URL}/leads", wait_until="networkidle", timeout=20000)
    hold("b1_hot", motion=["h2:has-text('Hot Conversations')"])

    page.locator("h2:has-text('Your Leads')").scroll_into_view_if_needed()
    hold("b1_list", motion=[".lead-score-ring"])

    click_visibly(page, "a.filter-btn:has-text('Hot')")
    page.wait_for_load_state("networkidle")
    hold("b1_filter")

    # ── 2. Lead Command Centre (real lead, real campaign-sourced deal) ──
    page.goto(f"{BASE_URL}/leads/3609", wait_until="networkidle", timeout=20000)
    hold("b2_open")

    page.locator(".lc-sec-head:has-text('Conversations')").scroll_into_view_if_needed()
    hold("b2_conversation")

    page.locator("#lcNotesPane").scroll_into_view_if_needed()
    hold("b2_notes")

    click_visibly(page, "#lcHistoryTab")
    page.locator("#lcHistoryPane").scroll_into_view_if_needed()
    hold("b2_history")

    # ── 3. CRM → Pipeline Board ──────────────────────────────────────────
    # "Closed — Lost & Dropped" only renders on List view, not Board — stay
    # on List for b3_wonlost, THEN switch to Board (real bug caught on the
    # first real run: scroll_into_view_if_needed() timed out because a
    # prior edit of this file had already navigated to ?view=board by then).
    page.goto(f"{BASE_URL}/sales-pipeline", wait_until="networkidle", timeout=20000)
    hold("b3_list")

    page.locator(".section-title:has-text('Closed')").scroll_into_view_if_needed()
    hold("b3_wonlost")

    page.goto(f"{BASE_URL}/sales-pipeline?view=board", wait_until="networkidle", timeout=20000)
    hold("b3_board")

    hold("b3_export", motion=["a:has-text('Export CSV')"])

    # ── 4. CRM → All Contacts ────────────────────────────────────────────
    page.goto(f"{BASE_URL}/whatsapp/contacts", wait_until="networkidle", timeout=20000)
    hold("b4_list")

    click_visibly(page, "button.filters-btn")
    hold("b4_filters")

    hold("b4_add", motion=["button:has-text('Add Contact')"])

    # ── 5. Contact detail — deal + timeline on 1952, Company chip on 25 ──
    page.goto(f"{BASE_URL}/whatsapp/contacts/1952", wait_until="networkidle", timeout=20000)
    hold("b5_deal")

    hold("b5_timeline")

    page.goto(f"{BASE_URL}/whatsapp/contacts/25", wait_until="networkidle", timeout=20000)
    hold("b5_company", motion=["div.profile-field-label:has-text('Company')"])

    # ── 6. CRM → Companies ───────────────────────────────────────────────
    page.goto(f"{BASE_URL}/crm/companies", wait_until="networkidle", timeout=20000)
    hold("b6_grid")

    click_visibly(page, "#listViewBtn")
    hold("b6_list")

    page.goto(f"{BASE_URL}/crm/companies/944", wait_until="networkidle", timeout=20000)
    hold("b6_detail")

    page.locator("h3:has-text('Company Activity')").scroll_into_view_if_needed()
    hold("b6_notes")

    # ── 7. CRM → Segments ─────────────────────────────────────────────────
    page.goto(f"{BASE_URL}/whatsapp/segments", wait_until="networkidle", timeout=20000)
    hold("b7_list")

    page.goto(f"{BASE_URL}/whatsapp/segments/10", wait_until="networkidle", timeout=20000)
    hold("b7_detail")

    # ── 8. CRM → Tags ────────────────────────────────────────────────────
    page.goto(f"{BASE_URL}/labels", wait_until="networkidle", timeout=20000)
    hold("b8_list")

    click_visibly(page, "button:has-text('View / Edit')")
    hold("b8_people")

    click_visibly(page, "#lmTabBtn-lead")
    hold("b8_deals")

    # ── 9. CRM → Pipeline Settings ───────────────────────────────────────
    page.goto(f"{BASE_URL}/sales-pipeline/settings", wait_until="networkidle", timeout=20000)
    hold("b9_labels")

    page.evaluate("window.scrollTo(0, 0)")
    hold("b9_close")
