"""
Shared constants and helpers for the merchant-facing Sales Pipeline CRM.
Used by portal_routes.py. Deliberately separate from lead_pipeline.py
(the PhiXtra-onboarding pipeline used by ambassadors/sales managers) —
this tracks a merchant's own customers/deals, with its own dedicated
merchant_pipeline_leads / merchant_pipeline_stage_history tables.

2026-09-09 redesign (see project_sales_pipeline_leads_redesign memory):
stage MEANINGS and the 6 default stage names are fixed and shared by every
business — what a business CAN change is the wording (pipeline_stage_labels)
and, separately, the Lead Score tier wording (lead_score_labels). Multiple
pipelines was considered and deliberately shelved — one pipeline per
business, same as before.
"""
import psycopg2.extras
from db import get_db_connection

# ── Active stages — a Lead moves through these in order ─────────────────────
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

# Rewritten 2026-09-09 for clearer, harder-to-misread business meaning —
# "Qualified" in particular used to read as "seems interested," now it's
# explicit that a genuine opportunity has been established.
STAGE_DESCRIPTIONS = {
    "new_lead":      "Prospect has entered the system.",
    "contacted":     "Someone has actually communicated with the prospect.",
    "qualified":     "The business has established there is a genuine sales opportunity.",
    "proposal_sent": "A quotation or proposal has actually been sent.",
    "negotiating":   "Price, quantity, terms, delivery, etc. are being discussed.",
    "won":           "Deal completed.",
}

# ── Outcomes — a Lead LEAVES the active list into exactly one of these ──────
# 'won' is also in STAGE_ORDER above (it's both the last active step and an
# outcome). 'lost' and 'dropped' are never in STAGE_ORDER — they're recorded
# via merchant_pipeline_leads.outcome + dropped_at/dropped_reason, alongside
# whichever active stage the lead was at when it closed (see
# sales_pipeline_close() in portal_routes.py).
OUTCOME_LABELS = {
    "won":     "Won",
    "lost":    "Lost",
    "dropped": "Dropped",
}
OUTCOME_DESCRIPTIONS = {
    "won":     "Deal completed.",
    "lost":    "We pursued the opportunity, but the customer chose another supplier.",
    "dropped": "We decided not to pursue this lead.",
}

# Default reason choices for the two outcomes that require one. Not yet
# business-editable (flagged as a later idea, same as multiple pipelines) —
# "Other" always included so nothing is ever a forced mismatch.
LOST_REASONS = [
    "Price too high", "Customer chose a competitor", "Budget unavailable",
    "No longer interested", "Other",
]
DROPPED_REASONS = [
    "No response", "Invalid lead", "Wrong contact", "Duplicate", "Other",
]

# ── Lead Score tier wording (scoring itself — colors, thresholds, the 0-100
# math — is fixed; see _pipeline_scored_from_sql in portal_routes.py) ───────
SCORE_TIER_DEFAULTS = {"hot": "Hot", "warm": "Warm", "cold": "Cold"}


def next_stage(current: str) -> str | None:
    if current not in STAGE_ORDER:
        return None
    idx = STAGE_ORDER.index(current)
    if idx + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[idx + 1]


def get_effective_stage_labels(tenant_id: int) -> dict:
    """STAGE_LABELS (+ OUTCOME_LABELS) with this tenant's custom wording
    (tenants.pipeline_stage_labels) laid on top — a missing/blank key just
    falls back to the default, so an empty '{}' (every business starts here)
    means 'using every default name.'"""
    labels = dict(STAGE_LABELS)
    labels.update(OUTCOME_LABELS)  # lost/dropped join the renameable set too
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("SELECT pipeline_stage_labels FROM tenants WHERE id=%s", (tenant_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        overrides = (row[0] if row else None) or {}
        for key, val in overrides.items():
            if key in labels and (val or "").strip():
                labels[key] = val.strip()
    except Exception as e:
        print("⚠️ get_effective_stage_labels error:", e)
    return labels


def get_effective_score_labels(tenant_id: int) -> dict:
    """SCORE_TIER_DEFAULTS with this tenant's custom wording
    (tenants.lead_score_labels) laid on top. Same fallback rule as stage labels."""
    labels = dict(SCORE_TIER_DEFAULTS)
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("SELECT lead_score_labels FROM tenants WHERE id=%s", (tenant_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        overrides = (row[0] if row else None) or {}
        for key, val in overrides.items():
            if key in labels and (val or "").strip():
                labels[key] = val.strip()
    except Exception as e:
        print("⚠️ get_effective_score_labels error:", e)
    return labels


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
