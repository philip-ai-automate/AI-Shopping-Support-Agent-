"""
school_plan_reset.py — Termly/annual billing period expiry sweep for
PhiXtra School (school.phixtra.com).

Scheduled daily by the school gateway's own scheduler (main.py, port 8002).

Unlike the merchant portal (recurring Flutterwave subscriptions that
auto-renew via webhook), a school's paid plan is a one-time payment that
buys ~1 term or ~1 year of runway. This job downgrades a school back to
Free once that runway has expired and no new payment has come in.

The number of days each cycle buys lives in the school_billing_cycle_days
table (not a local constant) — it's the single source of truth shared with
api-key-manager/school_billing.py's checkout code, a separate service this
one can't import from directly.
"""
import os
from datetime import datetime
from fastapi import APIRouter, Header, HTTPException

from db import get_db

router = APIRouter()

_INTERNAL_TOKEN = os.getenv("PHIXTRA_INTERNAL_TOKEN", "")

# Fallback only, used if the school_billing_cycle_days table is unreachable.
_CYCLE_DAYS_FALLBACK = {"termly": 120, "annual": 366}


def _load_cycle_days(cur) -> dict:
    try:
        cur.execute("SELECT cycle, days FROM school_billing_cycle_days")
        rows = cur.fetchall() or []
        loaded = {r["cycle"]: int(r["days"]) for r in rows}
        return loaded or dict(_CYCLE_DAYS_FALLBACK)
    except Exception as e:
        print(f"⚠️ [SCHOOL PLAN RESET] Could not load cycle days, using fallback: {e}")
        return dict(_CYCLE_DAYS_FALLBACK)


def run_school_plan_resets() -> dict:
    """
    Downgrade schools to Free once their paid billing period has lapsed.
    Returns {"termly_expired": n, "annual_expired": n}.
    """
    conn = get_db()
    cur  = conn.cursor()
    results = {"termly_expired": 0, "annual_expired": 0}

    try:
        cycle_days = _load_cycle_days(cur)
        for cycle, days in cycle_days.items():
            cur.execute(f"""
                UPDATE school_profiles
                SET plan_id = (SELECT id FROM school_plans WHERE slug='free' LIMIT 1),
                    quota_notified_at = NULL,
                    renewal_notified_at = NULL
                WHERE billing_cycle = %s
                  AND plan_id != (SELECT id FROM school_plans WHERE slug='free' LIMIT 1)
                  AND plan_period_start + INTERVAL '{days} days' <= CURRENT_DATE
            """, (cycle,))
            results[f"{cycle}_expired"] = cur.rowcount
        conn.commit()

        total = sum(results.values())
        if total:
            print(f"✅ [SCHOOL PLAN RESET] {results['termly_expired']} termly + "
                  f"{results['annual_expired']} annual expired -> downgraded to Free")
        else:
            print("ℹ️ [SCHOOL PLAN RESET] No school plans due for expiry today")
    except Exception as e:
        conn.rollback()
        print(f"⚠️ [SCHOOL PLAN RESET] Error: {e}")
    finally:
        cur.close()
        conn.close()

    return results


# ── Manual trigger endpoint (admin / testing) ─────────────────────────────────

@router.post("/school-plan-reset")
def trigger_school_plan_reset(authorization: str = Header(default="")):
    """
    Manually trigger the school plan expiry sweep.
    Header: Authorization: Bearer {PHIXTRA_INTERNAL_TOKEN}
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not _INTERNAL_TOKEN or token != _INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorised")

    results = run_school_plan_resets()
    return {"status": "ok", "results": results, "run_at": datetime.utcnow().isoformat()}
