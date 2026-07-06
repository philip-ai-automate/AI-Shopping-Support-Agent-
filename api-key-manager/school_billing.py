"""
school_billing.py — Subscription payments for PhiXtra School (school.phixtra.com).

Unlike school_payments.py (each school's OWN Paystack/Flutterwave account,
used to collect fees FROM parents), this module uses PhiXtra's OWN
Flutterwave account (FW_SECRET_KEY / FW_PUBLIC_KEY / FW_WEBHOOK_HASH — the
same keys already used for portal.phixtra.com merchant billing) to collect
subscription payments FROM schools.

Nigerian school terms don't map to fixed monthly/annual cycles, so this is a
one-time NGN payment per checkout (not a recurring Flutterwave payment plan)
— "termly" buys ~1 term of runway, "annual" buys ~1 year. school_plan_reset.py
downgrades a school back to Free once its paid period lapses.
"""
import os
import time
from datetime import date, datetime, timedelta

import psycopg2.extras
from db import get_db_connection

SCHOOL_BASE_URL = os.getenv("SCHOOL_BASE_URL", "https://school.phixtra.com").rstrip("/")

# Fallback only — the real source of truth is the school_billing_cycle_days
# table, read via cycle_days() below. Kept here so a checkout still works
# (with the documented default) if that table is ever unreachable.
_CYCLE_DAYS_FALLBACK = {"termly": 120, "annual": 366}


def cycle_days(cycle: str) -> int:
    """Days of runway a billing cycle buys — read from school_billing_cycle_days,
    the single source of truth shared with school-wa-gateway/school_plan_reset.py
    (a separate service that can't share this Python module directly)."""
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT days FROM school_billing_cycle_days WHERE cycle=%s", (cycle,))
        row = cur.fetchone()
        return int(row[0]) if row else _CYCLE_DAYS_FALLBACK.get(cycle, 120)
    finally:
        cur.close(); conn.close()


def _fw_ok() -> bool:
    return bool(os.getenv("FW_SECRET_KEY"))


def _fw_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('FW_SECRET_KEY')}",
            "Content-Type": "application/json"}


def get_plan_by_slug(plan_slug: str) -> dict | None:
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM school_plans WHERE slug=%s AND is_active=TRUE", (plan_slug,))
    plan = cur.fetchone()
    cur.close(); conn.close()
    return dict(plan) if plan else None


def plan_amount_ngn(plan: dict, cycle: str) -> int:
    col = "price_ngn_termly" if cycle == "termly" else "price_ngn_annual"
    return int(plan.get(col) or 0)


def init_plan_checkout(school_id: int, school_name: str, admin_email: str,
                       plan_slug: str, cycle: str) -> tuple[str | None, str | None]:
    """Start a Flutterwave checkout for a school plan purchase.
    Returns (checkout_url, error_message) — exactly one will be set."""
    import requests as _req

    if cycle not in ("termly", "annual"):
        return None, "Invalid billing cycle."
    if not _fw_ok():
        return None, "Payments are not configured yet. Contact support."

    plan = get_plan_by_slug(plan_slug)
    if not plan or plan["slug"] == "free":
        return None, "Invalid plan selected."

    amount_ngn = plan_amount_ngn(plan, cycle)
    if amount_ngn <= 0:
        return None, "This plan requires a custom quote — contact support@phixtra.com."

    tx_ref = f"SCHOOLPHIX-{school_id}-{plan_slug}-{cycle}-{int(time.time())}"

    try:
        resp = _req.post(
            "https://api.flutterwave.com/v3/payments",
            headers=_fw_headers(),
            json={
                "tx_ref": tx_ref,
                "amount": amount_ngn,
                "currency": "NGN",
                "redirect_url": f"{SCHOOL_BASE_URL}/school/billing/plan-upgrade/callback",
                "customer": {"email": admin_email, "name": school_name},
                "customizations": {
                    "title": "PhiXtra School Subscription",
                    "description": f"{plan['name']} Plan — {cycle.title()}",
                },
                "meta": {
                    "school_id": str(school_id),
                    "plan_id": str(plan["id"]),
                    "plan_slug": plan_slug,
                    "cycle": cycle,
                    "amount_ngn": str(amount_ngn),
                },
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["link"], None
        print("FW school checkout init error:", data)
        return None, "Payment initialisation failed. Please try again."
    except Exception as e:
        print(f"⚠️ [SCHOOL BILLING] FW init error: {e}")
        return None, "Could not reach payment provider. Please try again."


def activate_plan_subscription(school_id: int, plan_id: int, cycle: str,
                               tx_ref: str | None, amount, provider: str = "flutterwave",
                               provider_customer_id: str | None = None) -> None:
    """Set the school's active plan + record the purchase for audit/renewal tracking."""
    conn = get_db_connection()
    cur  = conn.cursor()
    period_start = date.today()

    cur.execute("""
        UPDATE school_profiles
           SET plan_id=%s, billing_cycle=%s, plan_period_start=%s,
               quota_notified_at=NULL, renewal_notified_at=NULL
         WHERE id=%s
    """, (plan_id, cycle, period_start, school_id))

    cur.execute("""
        UPDATE school_plan_subscriptions SET status='superseded', updated_at=NOW()
         WHERE school_id=%s AND status='active'
    """, (school_id,))

    now = datetime.utcnow()
    period_end = now + timedelta(days=cycle_days(cycle))

    cur.execute("""
        INSERT INTO school_plan_subscriptions
            (school_id, plan_id, billing_cycle, payment_provider, tx_ref,
             status, amount, current_period_start, current_period_end)
        VALUES (%s,%s,%s,%s,%s,'active',%s,%s,%s)
        ON CONFLICT (tx_ref) DO UPDATE
            SET status='active', updated_at=NOW()
    """, (school_id, plan_id, cycle, provider, tx_ref, amount, now, period_end))

    conn.commit(); cur.close(); conn.close()

    # Record ambassador commission if this school was referred by an ambassador
    try:
        from ambassador_routes import record_school_ambassador_commission
        record_school_ambassador_commission(school_id=school_id, amount=amount, currency="NGN")
    except Exception as _ce:
        print("⚠️ ambassador commission hook error (school):", _ce)


def verify_transaction(transaction_id: str) -> dict | None:
    """Verify a Flutterwave transaction by ID. Returns the txn dict on success."""
    import requests as _req
    if not _fw_ok():
        return None
    try:
        resp = _req.get(
            f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify",
            headers=_fw_headers(), timeout=15,
        )
        data = resp.json()
        if data.get("status") == "success" and data["data"].get("status") == "successful":
            return data["data"]
    except Exception as e:
        print(f"⚠️ [SCHOOL BILLING] verify error: {e}")
    return None


def tx_ref_already_processed(tx_ref: str) -> bool:
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT id FROM school_plan_subscriptions WHERE tx_ref=%s", (tx_ref,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return bool(row)
