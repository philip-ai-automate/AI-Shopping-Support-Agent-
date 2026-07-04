"""
Shared constants and helpers for the ambassador/sales-manager CRM lead pipeline.
Used by ambassador_routes.py (self-service) and portal_admin_routes.py (admin/oversight).
"""
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


def next_stage(current: str) -> str | None:
    if current not in STAGE_ORDER:
        return None
    idx = STAGE_ORDER.index(current)
    if idx + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[idx + 1]


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
