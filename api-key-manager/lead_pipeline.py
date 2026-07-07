"""
Shared constants and helpers for the ambassador/sales-manager CRM lead pipeline.
Used by ambassador_routes.py (self-service) and portal_admin_routes.py (admin/oversight).
"""
import datetime
import psycopg2.extras
from db import get_db_connection

STAGE_ORDER = [
    "lead", "contacted", "demo_done", "requirements_confirmed",
    "onboarding", "active_client", "support",
]

STAGE_LABELS = {
    "lead":                   "Lead",
    "contacted":              "Contacted",
    "demo_done":              "Demo Done",
    "requirements_confirmed": "Requirements Confirmed",
    "onboarding":             "Onboarding",
    "active_client":          "Active Client",
    "support":                "Support",
}

STAGE_DESCRIPTIONS = {
    "lead":                   "Business identified, not yet contacted",
    "contacted":              "First message sent, following up",
    "demo_done":              "QR code scanned, AI demonstrated live",
    "requirements_confirmed": "All 4 checklist items verified",
    "onboarding":             "Setup in progress, platform being configured",
    "active_client":          "Live on platform, paying subscription",
    "support":                "Ongoing client success and retention",
}


# Stages that carry a "planned date" separate from their "completed date" —
# scheduling one sets *_scheduled_date only and never touches `stage`.
SCHEDULE_FIELD = {
    "contacted":  "contact_scheduled_date",
    "demo_done":  "demo_scheduled_date",
    "onboarding": "onboarding_scheduled_date",
}

# Fields always editable regardless of stage (basic lead info).
EDIT_CORE_FIELDS = ["business_name", "industry", "contact_name", "phone", "email", "notes"]

# Extra fields editable depending on which stage the lead is currently sitting
# at — mirrors exactly what was captured to *reach* that stage, so Edit lets
# you correct a mistake without re-running the whole pipeline.
EDIT_STAGE_FIELDS = {
    "contacted":              ["contact_channel", "contact_date", "contact_response"],
    "demo_done":               ["demo_date", "demo_reaction"],
    "requirements_confirmed": ["req_phone", "req_meta_account", "req_whatsapp_connected",
                                "req_product_list", "requirements_notes"],
    "onboarding":              ["onboarding_date", "onboarding_notes"],
    "active_client":           ["tenant_id", "school_id", "estate_tenant_id",
                                 "onboard_products_uploaded", "onboard_whatsapp_connected",
                                 "onboard_login_sent", "onboard_client_trained",
                                 "onboarding_checklist_notes"],
}


def lead_edit_payload(lead: dict) -> dict:
    """Flatten a lead row into a JSON-safe dict for prefilling the Edit modal."""
    payload = {k: lead.get(k) for k in EDIT_CORE_FIELDS}
    payload["stage"] = lead.get("stage")
    for field in EDIT_STAGE_FIELDS.get(lead.get("stage"), []):
        val = lead.get(field)
        if isinstance(val, datetime.date):
            val = val.isoformat()
        payload[field] = val
    return payload


def next_stage(current: str) -> str | None:
    if current not in STAGE_ORDER:
        return None
    idx = STAGE_ORDER.index(current)
    if idx + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[idx + 1]


def _parse_date(raw: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


def build_stage_advance_updates(target: str, form) -> tuple[dict, str | None]:
    """Validate + build the SET-clause dict for advancing a lead to `target`.

    Returns (updates, error). `error` is a ready-to-flash message; when it is
    set, `updates` is empty and the caller must not write to the DB.
    Covers contacted/demo_done/requirements_confirmed/onboarding fully, and
    active_client's onboarding-checklist validation — but NOT active_client's
    tenant/school/estate link, which needs a DB lookup the caller already has
    a connection open for and stays in the route handlers. `support` has no
    fields of its own and falls through to the bare `{"stage": target}`.
    """
    updates: dict = {"stage": target}
    today = datetime.date.today()

    if target == "contacted":
        contact_channel  = (form.get("contact_channel") or "").strip()
        contact_date_raw = (form.get("contact_date") or "").strip()
        contact_response = (form.get("contact_response") or "").strip()
        if not contact_channel or not contact_date_raw:
            return {}, "Contact channel and date are required."
        contact_date = _parse_date(contact_date_raw)
        if not contact_date:
            return {}, "Invalid contact date."
        if contact_date > today:
            return {}, "Contact date can't be in the future — that's still a scheduled contact. Use Schedule instead, then mark it done once it happens."
        updates.update(contact_channel=contact_channel, contact_date=contact_date_raw,
                       contact_response=contact_response or None, contact_scheduled_date=None)

    elif target == "demo_done":
        demo_date_raw = (form.get("demo_date") or "").strip()
        demo_reaction = (form.get("demo_reaction") or "").strip()
        if not demo_date_raw:
            return {}, "Demo date is required."
        demo_date = _parse_date(demo_date_raw)
        if not demo_date:
            return {}, "Invalid demo date."
        if demo_date > today:
            return {}, "Demo date can't be in the future — that's still a scheduled demo. Use Schedule instead, then mark it done once it happens."
        updates.update(demo_date=demo_date_raw, demo_reaction=demo_reaction or None,
                       demo_scheduled_date=None)

    elif target == "requirements_confirmed":
        req_phone    = form.get("req_phone") == "1"
        req_meta     = form.get("req_meta_account") == "1"
        req_whatsapp = form.get("req_whatsapp_connected") == "1"
        req_products = form.get("req_product_list") == "1"
        if not (req_phone and req_meta and req_whatsapp and req_products):
            return {}, "All 4 requirements must be confirmed before advancing."
        updates.update(req_phone=True, req_meta_account=True,
                       req_whatsapp_connected=True, req_product_list=True,
                       requirements_notes=(form.get("requirements_notes") or "").strip() or None)

    elif target == "onboarding":
        onboarding_date_raw = (form.get("onboarding_date") or "").strip()
        onboarding_notes    = (form.get("onboarding_notes") or "").strip()
        if not onboarding_date_raw:
            return {}, "Onboarding date is required."
        onboarding_date = _parse_date(onboarding_date_raw)
        if not onboarding_date:
            return {}, "Invalid onboarding date."
        if onboarding_date > today:
            return {}, "Onboarding date can't be in the future — that's still a scheduled onboarding. Use Schedule instead, then mark it done once it starts."
        updates.update(onboarding_date=onboarding_date_raw, onboarding_notes=onboarding_notes or None,
                       onboarding_scheduled_date=None)

    elif target == "active_client":
        onboard_products = form.get("onboard_products_uploaded") == "1"
        onboard_whatsapp = form.get("onboard_whatsapp_connected") == "1"
        onboard_login    = form.get("onboard_login_sent") == "1"
        onboard_trained  = form.get("onboard_client_trained") == "1"
        if not (onboard_products and onboard_whatsapp and onboard_login and onboard_trained):
            return {}, "All 4 onboarding checklist items must be confirmed before marking as Active Client."
        updates.update(onboard_products_uploaded=True, onboard_whatsapp_connected=True,
                       onboard_login_sent=True, onboard_client_trained=True,
                       onboarding_checklist_notes=(form.get("onboarding_checklist_notes") or "").strip() or None)
        # tenant/school/estate linking still handled by the caller — needs a
        # DB lookup this function doesn't have a connection for.

    return updates, None


def build_requirements_progress_update(form) -> dict:
    """Build a partial-save update for the requirements-confirmed checklist —
    unlike build_stage_advance_updates(), this never requires all 4 to be
    true and never touches `stage`. Lets an ambassador save "2 of 4 done,
    waiting on X" instead of losing that progress until every box is ticked."""
    return {
        "req_phone":              form.get("req_phone") == "1",
        "req_meta_account":       form.get("req_meta_account") == "1",
        "req_whatsapp_connected": form.get("req_whatsapp_connected") == "1",
        "req_product_list":       form.get("req_product_list") == "1",
        "requirements_notes":     (form.get("requirements_notes") or "").strip() or None,
    }


def build_onboarding_checklist_update(form) -> dict:
    """Build a partial-save update for the onboarding checklist — same
    incremental-progress pattern as build_requirements_progress_update(),
    but for the work done *during* onboarding (products uploaded, WhatsApp
    connected, login sent, client trained), gating the eventual move to
    active_client rather than entry into onboarding itself."""
    return {
        "onboard_products_uploaded":  form.get("onboard_products_uploaded") == "1",
        "onboard_whatsapp_connected": form.get("onboard_whatsapp_connected") == "1",
        "onboard_login_sent":         form.get("onboard_login_sent") == "1",
        "onboard_client_trained":     form.get("onboard_client_trained") == "1",
        "onboarding_checklist_notes": (form.get("onboarding_checklist_notes") or "").strip() or None,
    }


def build_schedule_update(target: str, scheduled_date_raw: str) -> tuple[dict, str | None]:
    """Validate + build the SET-clause dict for *scheduling* (not completing)
    the next activity on a lead. Does not touch `stage`."""
    field = SCHEDULE_FIELD.get(target)
    if not field:
        return {}, "That stage can't be scheduled."
    scheduled_date_raw = (scheduled_date_raw or "").strip()
    if not scheduled_date_raw or not _parse_date(scheduled_date_raw):
        return {}, "A valid date is required."
    return {field: scheduled_date_raw}, None


def compute_due_items(leads: list[dict], within_days: int = 7) -> list[dict]:
    """Scan a list of lead dicts for a scheduled-but-not-yet-completed activity
    due within `within_days` (overdue items included). Sorted soonest first."""
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=within_days)
    due = []
    for lead in leads:
        target = next_stage(lead.get("stage"))
        field = SCHEDULE_FIELD.get(target)
        if not field:
            continue
        sched = lead.get(field)
        if not sched or sched > horizon:
            continue
        due.append({
            "lead_id":   lead["id"],
            "business_name": lead["business_name"],
            "ambassador_name": lead.get("ambassador_name"),
            "target_stage":  target,
            "date":      sched,
            "overdue":   sched < today,
        })
    due.sort(key=lambda d: d["date"])
    return due


def format_relative_activity(dt: "datetime.datetime | None") -> str:
    """Human-readable "how long ago" for an account's last chat/WhatsApp
    activity timestamp, used on the Active Client account-health badge."""
    if not dt:
        return "No activity yet"
    now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
    seconds = (now - dt).total_seconds()
    if seconds < 300:
        return "Just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    days = (now - dt).days
    if days < 30:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


def is_activity_stale(dt: "datetime.datetime | None", threshold_days: int = 14) -> bool:
    """True if an account has gone quiet — no activity ever, or none within
    `threshold_days` — a soft churn-risk signal for the account-health badge."""
    if not dt:
        return True
    now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
    return (now - dt).days >= threshold_days


def record_stage_change(lead_id: int, from_stage: str | None, to_stage: str,
                         changed_by: str, notes: str | None = None) -> None:
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
        VALUES (%s, %s, %s, %s, %s)
    """, (lead_id, from_stage, to_stage, changed_by, notes))
    conn.commit()
    cur.close(); conn.close()


def get_stage_history(lead_id: int) -> list[dict]:
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT from_stage, to_stage, changed_by, notes, created_at
        FROM lead_stage_history WHERE lead_id=%s ORDER BY created_at DESC
    """, (lead_id,))
    rows = cur.fetchall() or []
    cur.close(); conn.close()
    return rows
