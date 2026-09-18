"""
Beats + Playwright actions for the Reports tutorial video, on
portal.phixtra.com's demo merchant account — see tutorial_studio/lib.py for
the shared machinery this module plugs into.

Three real destinations under the Reports story, all under the sidebar's own
top-level "📈 Reports" group: Pipeline Overview, Leads & Sources, and the
Custom Report Builder — the newest and most powerful of the three, covered
in real depth including all 4 of its entities (Leads and Deals, Contacts,
Companies, and Campaigns).

Demo data note: nothing new was added for this video — every number shown
(27 active deals, ₦42,410,000 open pipeline value, 36 new leads, 20 campaign
sends, 1 real company) is whatever the standing demo account already had at
recording time. Checked live via curl before writing narration around any of
it, so nothing here asserts a number that wasn't actually on screen.

Correction while planning this video: an earlier memory note said the Custom
Report Builder's Campaigns entity (originally "Phase 4") was "not started" —
that was stale. Checked the real code and the live page before planning:
CUSTOM_REPORT_ENTITIES already has a full "campaigns" entry (columns, group-
by options, status labels) and /reports/custom/campaigns renders real data
(20 results) — it ships today, so it gets full coverage here, not a skip.
"""
from playwright.sync_api import TimeoutError as PWTimeoutError

from tutorial_studio.lib import click_visibly, type_visibly

BASE_URL = "https://portal.phixtra.com"
DEMO_EMAIL = "demo@phixtra.com"
DEMO_PASSWORD = "Demo1234!"

BEATS = {
    # ── 1. Pipeline Overview ────────────────────────────────────────────
    "b1_open": (
        "Reports — three real reports, starting with Pipeline Overview: "
        "every open deal in your Sales Pipeline, by stage, right now."
    ),
    "b1_kpis": (
        "Active deals and open pipeline value are always this instant, not "
        "a period sum. Won, Win Rate, Lost, Dropped and Average Time to "
        "Close are scoped to whichever period you pick, just below."
    ),
    "b1_period": (
        "Switch the period — Last 90 days — and Won, Lost and Win Rate "
        "recalculate for real, live, not a canned preview."
    ),
    "b1_funnel": (
        "The funnel shows exactly where deals are sitting — bar width is "
        "deal count, and the value alongside it is what's actually at "
        "stake in that stage."
    ),
    "b1_bench": (
        "And Average Time in Each Stage is measured from real stage "
        "changes, all-time — so you can see exactly where deals tend to "
        "slow down, not just where they end up."
    ),

    # ── 2. Leads & Sources ───────────────────────────────────────────────
    "b2_open": (
        "Leads and Sources — how many new leads are coming in, and where "
        "they're actually coming from."
    ),
    "b2_trend": (
        "A real trend line of new leads over time, bucketed by day or by "
        "week depending on how wide a range you're looking at."
    ),
    "b2_sources": (
        "And the source breakdown — WhatsApp, added by hand, or not "
        "recorded. No guessing where your pipeline is actually being fed "
        "from."
    ),

    # ── 3. Custom Report Builder ─────────────────────────────────────────
    "b3_picker": (
        "The Custom Report Builder — pick what you want to report on. "
        "Leads and Deals, Contacts, Companies, or Campaigns — build the "
        "table you actually need, not whatever a fixed report happened to "
        "include."
    ),
    "b3_leads_open": (
        "Start with Leads and Deals — every lead and deal in your Sales "
        "Pipeline, yours to shape from here."
    ),
    "b3_leads_columns": (
        "Columns are a real multi-select — tick exactly the fields you "
        "want in the table, nothing you don't."
    ),
    "b3_leads_filter": (
        "Filter by Stage or Source the same way — this one's narrowed "
        "straight to deals that are actually Won."
    ),
    "b3_leads_generate": (
        "Generate, and it's a real, paginated table — exportable straight "
        "to CSV, Excel, or PDF with the exact same filters applied."
    ),
    "b3_leads_group": (
        "Or skip the record list entirely — Group By turns it into totals "
        "instead. By Stage, here, each bar is a real count and a real sum "
        "of deal value, not an estimate."
    ),
    "b3_leads_save": (
        "Name it and save it, and it's one click to come back to this "
        "exact report — columns, filters, grouping, all of it — any time "
        "you need it again."
    ),
    "b3_contacts": (
        "Switch entities to Contacts — same builder, same multi-select "
        "filters, this time over every WhatsApp contact you've saved."
    ),
    "b3_companies": (
        "Companies keeps it simple — search by name, and see every "
        "business linked to your contacts and deals."
    ),
    "b3_campaigns": (
        "And Campaigns — every message sent, and what happened after, "
        "including the real revenue a reply turned into. Grouped by "
        "Status here, so you can see the whole funnel from Sent through to "
        "Converted in one look."
    ),
    "b3_close": (
        "Three reports, one shared engine underneath — a live pipeline "
        "snapshot, where your leads actually come from, and a report "
        "builder that can answer almost anything else, saved and ready to "
        "reuse. That's Reports."
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
    # Sales Pipeline runs its OWN separate guided-tour flag distinct from the
    # generic one (see crm_pipeline.py) — this video doesn't visit that page,
    # but both are set here as a harmless, consistent default across every
    # tutorial_studio recording.
    ctx.add_init_script(
        "localStorage.setItem('phixtra_tour_done', '1');"
        "localStorage.setItem('phixtra_portal_tour_sales_pipeline', '1');"
    )
    page = ctx.new_page()
    return page, ctx


def _close_msdd(page):
    """Click somewhere neutral to close whichever multi-select dropdown is
    currently open — the page's own click-outside handler does the rest.
    Real bug caught in a dry run: body's own top-left corner is actually the
    sidebar's logo link (`a.sb-brand`), so clicking position (5,5) silently
    navigated away to "/" instead of just closing the dropdown. `.report-
    title` is a plain, non-interactive div — safe to click anywhere on it."""
    page.locator(".report-title").click()


def record(page, hold, mark, beat_ms):
    # ── 1. Pipeline Overview ────────────────────────────────────────────
    page.goto(f"{BASE_URL}/reports/pipeline-overview", wait_until="networkidle", timeout=20000)
    hold("b1_open")

    hold("b1_kpis", motion=[".grid.g4", ".grid.g3"])

    click_visibly(page, "a.period-tab:has-text('Last 90 days')")
    page.wait_for_load_state("networkidle")
    hold("b1_period", motion=[".grid.g4"])

    page.locator(".funnel-card").scroll_into_view_if_needed()
    hold("b1_funnel", motion=[".funnel-card"])

    page.locator("h3:has-text('Avg. time in each stage')").scroll_into_view_if_needed()
    hold("b1_bench", motion=["h3:has-text('Deals by stage')", ".bench-card"])

    # ── 2. Leads & Sources ───────────────────────────────────────────────
    page.goto(f"{BASE_URL}/reports/leads-sources", wait_until="networkidle", timeout=20000)
    hold("b2_open", motion=[".kpi-row"])

    page.locator("h3:has-text('New leads over time')").scroll_into_view_if_needed()
    hold("b2_trend", motion=["h3:has-text('New leads over time')"])

    page.locator("h3:has-text('Lead source')").scroll_into_view_if_needed()
    hold("b2_sources", motion=["h3:has-text('Lead source')"])

    # ── 3. Custom Report Builder ─────────────────────────────────────────
    page.goto(f"{BASE_URL}/reports/custom", wait_until="networkidle", timeout=20000)
    hold("b3_picker", motion=[".entity-grid"])

    click_visibly(page, "a.entity-card:has-text('Leads and Deals')")
    page.wait_for_load_state("networkidle")
    hold("b3_leads_open")

    # 5 columns are pre-checked by default (customer_name/phone/deal_value/
    # stage/created_at) — tick 2 more that AREN'T already on, so the demo
    # shows a real additive change rather than silently unchecking defaults.
    click_visibly(page, ".msdd-trigger:has-text('Columns')")
    click_visibly(page, ".msdd-option:has-text('Company')")
    click_visibly(page, ".msdd-option:has-text('Source')")
    # ".msdd.open .msdd-panel", not bare ".msdd-panel" -- real bug caught on
    # the first real run: the page has 2+ .msdd-panel elements (Columns,
    # Stage, Source), and move_to()'s .first grabs whichever is FIRST IN DOM
    # ORDER, not whichever is actually open -- which can be a closed
    # (display:none) one. Scoping to the currently-open dropdown's own panel
    # is the only selector that's correct regardless of DOM order.
    hold("b3_leads_columns", motion=[".msdd.open .msdd-panel"])
    _close_msdd(page)

    click_visibly(page, ".msdd-trigger:has-text('Stage')")
    click_visibly(page, ".msdd-option:has-text('Won')")
    hold("b3_leads_filter", motion=[".msdd.open .msdd-panel"])
    _close_msdd(page)

    click_visibly(page, "button:has-text('Generate report')")
    page.wait_for_load_state("networkidle")
    hold("b3_leads_generate", motion=[".table-wrap"])

    # select_option()'s change handler calls this.form.submit() asynchronously
    # -- wait_for_load_state() alone can resolve against the still-old page
    # before that navigation actually starts (caught in a dry run: the URL
    # and .group-card read back stale). expect_navigation() waits on the
    # real navigation event instead.
    with page.expect_navigation(wait_until="networkidle"):
        page.select_option("select[name=group_by]", label="Stage")
    hold("b3_leads_group", motion=[".group-card"])

    click_visibly(page, "#saveViewTrigger")
    type_visibly(page, "#saveViewName", "Won Deals")
    # Saving is a fetch() call followed by a JS-driven window.location.href
    # redirect on success, not a plain form submit -- wait_for_load_state()
    # alone can resolve against the still-old page before that redirect
    # actually fires (same race as the group_by select, caught in a dry run).
    with page.expect_navigation(wait_until="networkidle"):
        click_visibly(page, "button.btn-save")
    hold("b3_leads_save", motion=[".pill-row"])

    # ── Contacts entity ──────────────────────────────────────────────────
    page.goto(f"{BASE_URL}/reports/custom/contacts", wait_until="networkidle", timeout=20000)
    click_visibly(page, ".msdd-trigger:has-text('Status')")
    click_visibly(page, ".msdd-option:has-text('Lead')")
    _close_msdd(page)
    click_visibly(page, "button:has-text('Generate report')")
    page.wait_for_load_state("networkidle")
    hold("b3_contacts", motion=[".msdd-row", ".table-wrap"])

    # ── Companies entity ─────────────────────────────────────────────────
    page.goto(f"{BASE_URL}/reports/custom/companies", wait_until="networkidle", timeout=20000)
    hold("b3_companies", motion=["input[name=q]", ".table-wrap"])

    # ── Campaigns entity ─────────────────────────────────────────────────
    page.goto(f"{BASE_URL}/reports/custom/campaigns", wait_until="networkidle", timeout=20000)
    with page.expect_navigation(wait_until="networkidle"):
        page.select_option("select[name=group_by]", label="Status")
    hold("b3_campaigns", motion=[".group-card"])

    page.evaluate("window.scrollTo(0, 0)")
    hold("b3_close")
