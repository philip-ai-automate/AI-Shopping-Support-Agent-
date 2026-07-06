"""
Shared constants and helpers for the merchant-facing Sales Pipeline CRM.
Used by portal_routes.py. Deliberately separate from lead_pipeline.py
(the PhiXtra-onboarding pipeline used by ambassadors/sales managers) —
this tracks a merchant's own customers/deals, with its own dedicated
merchant_pipeline_leads / merchant_pipeline_stage_history tables.
"""
import psycopg2.extras
from db import get_db_connection

STAGE_ORDER = [
    "new_lead", "contacted", "qualified", "proposal_sent", "negotiating", "won",
]

STAGE_LABELS = {
    "new_lead":      "New Lead",
    "contacted":     "Contacted",
    "qualified":     "Qualified",
    "proposal_sent": "Proposal Sent",
    "negotiating":   "Negotiating",
    "won":           "Won",
}

STAGE_DESCRIPTIONS = {
    "new_lead":      "Customer identified, not yet contacted",
    "contacted":     "First message sent, following up",
    "qualified":     "Confirmed genuine interest, budget and needs",
    "proposal_sent": "Quote or proposal shared with the customer",
    "negotiating":   "Discussing price, terms, or final details",
    "won":           "Deal closed — customer purchased",
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
        INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
        VALUES (%s, %s, %s, %s, %s)
    """, (lead_id, from_stage, to_stage, changed_by, notes))
    conn.commit()
    cur.close(); conn.close()


def get_stage_history(lead_id: int) -> list[dict]:
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT from_stage, to_stage, changed_by, notes, created_at
        FROM merchant_pipeline_stage_history WHERE lead_id=%s ORDER BY created_at DESC
    """, (lead_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [dict(r) for r in rows]
