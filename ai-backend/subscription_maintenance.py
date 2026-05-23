"""
subscription_maintenance.py — recurring subscription billing cron job.

Mirrors trial_maintenance.py exactly in style and error handling.
This script NEVER raises (so cron won't spam failure loops).

Run once daily via cron:
  python3 -m subscription_maintenance

What it does each run:
  1. Charge any subscription whose current_period_end is within 24 hours.
     - Success → extend period, add credits, create invoice, send receipt email.
     - Failure → mark past_due, send payment-failed email.

  2. Suspend any past_due subscription whose current_period_end has passed
     the 3-day grace period. Deactivates the tenant's API key.

  3. Cancel any subscription with cancel_at_period_end=1 whose period has ended.
     Deactivates the tenant's API key.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

try:
    import stripe as _stripe
except Exception:
    _stripe = None

from db import get_db_connection, insert_audit_log
from invoice_pdf import generate_invoice_pdf
from portal_utils import (
    send_email, next_invoice_number,
    credits_to_tokens, money_fmt,
)

BRAND_PRIMARY   = "#030C18"
PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")
GRACE_DAYS      = 3      # days past_due before suspension
RENEW_AHEAD_HRS = 24     # charge this many hours before period_end


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive_to_utc(dt) -> datetime:
    """MySQL DATETIME comes back naive — treat it as UTC."""
    if dt is None:
        return _utcnow()
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _stripe_ok() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY")) and _stripe is not None


# ── STEP 1: charge renewals due within the next 24 hours ─────────────────────

def _process_renewals(conn) -> None:
    """Find active subscriptions due for renewal and charge them."""
    now      = _utcnow()
    deadline = now + timedelta(hours=RENEW_AHEAD_HRS)

    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT
            s.id              AS sub_id,
            s.customer_id,
            s.tenant_id,
            s.package_id,
            s.payment_method_id,
            s.current_period_end,
            s.status,
            cp.name           AS plan_name,
            cp.credits        AS plan_credits,
            cp.price_pence    AS plan_price_pence,
            cp.billing_period AS billing_period,
            cp.currency       AS currency,
            c.email           AS customer_email,
            c.first_name      AS customer_first_name,
            c.stripe_customer_id,
            spm.stripe_payment_method AS pm_stripe_id,
            t.name            AS tenant_name
        FROM subscriptions s
        JOIN credit_packages cp  ON cp.id  = s.package_id
        JOIN customers c         ON c.id   = s.customer_id
        JOIN tenants t           ON t.id   = s.tenant_id
        LEFT JOIN saved_payment_methods spm ON spm.id = s.payment_method_id
        WHERE s.status IN ('active', 'past_due')
          AND s.cancel_at_period_end = 0
          AND s.current_period_end <= %s
    """, (deadline.strftime("%Y-%m-%d %H:%M:%S"),))
    subs = cur.fetchall() or []
    cur.close()

    for sub in subs:
        sub_id      = int(sub["sub_id"])
        tenant_id   = int(sub["tenant_id"])
        customer_id = int(sub["customer_id"])

        period_end_utc = _naive_to_utc(sub["current_period_end"])

        # Skip if period hasn't actually ended yet and we are checking early
        if period_end_utc > now:
            pass  # charge ahead of time is fine — prevents missed renewals

        _attempt_renewal(conn, sub, now)


def _attempt_renewal(conn, sub: dict, now: datetime) -> None:
    """Try to charge a single subscription renewal."""
    sub_id          = int(sub["sub_id"])
    tenant_id       = int(sub["tenant_id"])
    customer_id     = int(sub["customer_id"])
    credits         = int(sub["plan_credits"])
    amount_pence    = int(sub["plan_price_pence"])
    currency        = sub.get("currency") or "gbp"
    billing_period  = sub.get("billing_period") or "monthly"
    pm_stripe_id    = sub.get("pm_stripe_id") or ""
    stripe_cus_id   = sub.get("stripe_customer_id") or ""
    email           = sub.get("customer_email") or ""
    name            = (sub.get("customer_first_name") or "there").strip()
    plan_name       = sub.get("plan_name") or "Plan"
    inv_num         = next_invoice_number()

    # Cannot charge without Stripe or a saved payment method
    if not _stripe_ok() or not pm_stripe_id or not stripe_cus_id:
        print(f"⚠️ sub {sub_id}: cannot charge — missing Stripe config or payment method")
        return

    # ── Charge the card ───────────────────────────────────────────────────────
    try:
        _stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
        pi = _stripe.PaymentIntent.create(
            amount               = amount_pence,
            currency             = currency,
            customer             = stripe_cus_id,
            payment_method       = pm_stripe_id,
            description          = f"PhiXtra {plan_name} renewal",
            metadata             = {
                "subscription_id": str(sub_id),
                "customer_id":     str(customer_id),
                "tenant_id":       str(tenant_id),
                "invoice_num":     inv_num,
            },
            confirm              = True,
            off_session          = True,
            payment_method_types = ["card"],
        )
    except Exception as charge_err:
        print(f"⚠️ sub {sub_id}: charge failed — {charge_err}")
        _handle_renewal_failure(conn, sub, str(charge_err), now)
        return

    # ── Payment succeeded — update subscription period ────────────────────────
    if billing_period == "annual":
        new_period_end = _naive_to_utc(sub["current_period_end"]) + timedelta(days=365)
    else:
        new_period_end = _naive_to_utc(sub["current_period_end"]) + timedelta(days=30)

    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE subscriptions
            SET status='active',
                current_period_start = current_period_end,
                current_period_end   = %s,
                updated_at           = UTC_TIMESTAMP()
            WHERE id=%s
        """, (new_period_end.strftime("%Y-%m-%d %H:%M:%S"), sub_id))

        # Create subscription invoice row
        cur.execute("""
            INSERT INTO subscription_invoices
                (invoice_number, subscription_id, customer_id, tenant_id,
                 package_id, credits, amount_pence, vat_pence, currency,
                 status, period_start, period_end, stripe_payment_intent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, 'paid', %s, %s, %s)
        """, (
            inv_num, sub_id, customer_id, tenant_id,
            sub["package_id"], credits, amount_pence, currency,
            sub["current_period_end"],
            new_period_end.strftime("%Y-%m-%d %H:%M:%S"),
            pi["id"],
        ))

        # Top up credit balance
        tokens_add = credits_to_tokens(credits)
        cur.execute(
            "INSERT IGNORE INTO tenant_balances (tenant_id, token_balance) VALUES (%s, 0)",
            (tenant_id,)
        )
        cur.execute(
            "UPDATE tenant_balances SET token_balance = token_balance + %s WHERE tenant_id=%s",
            (tokens_add, tenant_id)
        )

        # Ensure API key is active (re-activate if it was suspended)
        cur.execute(
            "UPDATE api_keys SET is_active=1 WHERE tenant_id=%s AND key_type='paid'",
            (tenant_id,)
        )

        conn.commit()
    except Exception as db_err:
        conn.rollback()
        print(f"⚠️ sub {sub_id}: DB update failed after successful charge — {db_err}")
        insert_audit_log(
            action="subscription_renewal_db_error",
            tenant_id=tenant_id,
            details={"error": str(db_err), "payment_intent": pi.get("id"),
                     "invoice": inv_num, "sub_id": sub_id},
        )
        cur.close()
        return
    finally:
        try: cur.close()
        except Exception: pass

    # Generate invoice PDF
    pdf_path = None
    try:
        pdf_path = generate_invoice_pdf(
            invoice_number = inv_num,
            customer_email = email,
            tenant_name    = sub.get("tenant_name") or "",
            credits        = credits,
            amount_pence   = amount_pence,
            vat_pence      = 0,
            currency       = currency,
            created_at     = now.replace(tzinfo=None),
        )
        cur2 = conn.cursor()
        cur2.execute(
            "UPDATE subscription_invoices SET pdf_path=%s WHERE invoice_number=%s",
            (pdf_path, inv_num)
        )
        conn.commit()
        cur2.close()
    except Exception as pdf_err:
        print(f"⚠️ sub {sub_id}: PDF generation failed — {pdf_err}")

    # Audit log
    insert_audit_log(
        action    = "subscription_renewed",
        tenant_id = tenant_id,
        details   = {
            "sub_id":        sub_id,
            "plan":          plan_name,
            "credits":       credits,
            "amount_pence":  amount_pence,
            "invoice":       inv_num,
            "new_period_end": new_period_end.isoformat(),
        },
    )

    # Send receipt email
    try:
        if email:
            end_str = new_period_end.strftime("%d %B %Y")
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:520px">
              <h2 style="color:{BRAND_PRIMARY}">Subscription renewed ✅</h2>
              <p>Hi {name},</p>
              <p>Your <b>{plan_name}</b> plan has been renewed successfully.</p>
              <table style="width:100%;border-collapse:collapse;margin:16px 0">
                <tr>
                  <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;
                              font-weight:700;width:140px">Credits added</td>
                  <td style="padding:8px 12px;border:1px solid #e5e7eb">{credits} credits</td>
                </tr>
                <tr>
                  <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;
                              font-weight:700">Amount charged</td>
                  <td style="padding:8px 12px;border:1px solid #e5e7eb">
                      {money_fmt(amount_pence, currency)}</td>
                </tr>
                <tr>
                  <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;
                              font-weight:700">Next renewal</td>
                  <td style="padding:8px 12px;border:1px solid #e5e7eb">{end_str}</td>
                </tr>
              </table>
              <p>
                <a href="{PORTAL_BASE_URL}/billing/subscribe"
                   style="background:{BRAND_PRIMARY};color:#fff;padding:10px 18px;
                          border-radius:12px;text-decoration:none;display:inline-block">
                  View subscription
                </a>
              </p>
            </div>"""
            send_email(email, "PhiXtra subscription renewed ✅", html)
    except Exception as email_err:
        print(f"⚠️ sub {sub_id}: renewal email failed — {email_err}")


def _handle_renewal_failure(conn, sub: dict, error_msg: str, now: datetime) -> None:
    """Mark subscription as past_due and notify customer."""
    sub_id      = int(sub["sub_id"])
    tenant_id   = int(sub["tenant_id"])
    email       = sub.get("customer_email") or ""
    name        = (sub.get("customer_first_name") or "there").strip()
    plan_name   = sub.get("plan_name") or "Plan"

    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE subscriptions SET status='past_due', updated_at=UTC_TIMESTAMP() WHERE id=%s",
            (sub_id,)
        )
        conn.commit()
    except Exception as e:
        print(f"⚠️ sub {sub_id}: could not set past_due — {e}")
    finally:
        try: cur.close()
        except Exception: pass

    insert_audit_log(
        action    = "subscription_payment_failed",
        tenant_id = tenant_id,
        details   = {"sub_id": sub_id, "error": error_msg[:300]},
    )

    try:
        if email:
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:520px">
              <h2 style="color:#b91c1c">⚠️ Payment failed — action required</h2>
              <p>Hi {name},</p>
              <p>We could not renew your <b>{plan_name}</b> subscription because your
                 payment was declined.</p>
              <p>Please update your payment card within <b>{GRACE_DAYS} days</b> to keep
                 your AI assistant running.</p>
              <p>
                <a href="{PORTAL_BASE_URL}/billing/add-card"
                   style="background:#b91c1c;color:#fff;padding:10px 18px;
                          border-radius:12px;text-decoration:none;display:inline-block">
                  Update payment card
                </a>
              </p>
              <p style="color:#6b7280;font-size:12px">
                Questions? Contact
                <a href="mailto:support@phixtra.com">support@phixtra.com</a>
              </p>
            </div>"""
            send_email(email, "⚠️ PhiXtra payment failed — please update your card", html)
    except Exception as email_err:
        print(f"⚠️ sub {sub_id}: payment-failed email error — {email_err}")


# ── STEP 2: suspend past_due subscriptions past grace period ──────────────────

def _process_suspensions(conn) -> None:
    """Suspend any past_due subscription whose grace period has expired."""
    now           = _utcnow()
    grace_cutoff  = now - timedelta(days=GRACE_DAYS)

    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT s.id AS sub_id, s.tenant_id, s.customer_id,
               s.current_period_end,
               c.email AS customer_email,
               c.first_name AS customer_first_name,
               cp.name AS plan_name
        FROM subscriptions s
        JOIN customers c    ON c.id  = s.customer_id
        JOIN credit_packages cp ON cp.id = s.package_id
        WHERE s.status = 'past_due'
          AND s.current_period_end <= %s
    """, (grace_cutoff.strftime("%Y-%m-%d %H:%M:%S"),))
    subs = cur.fetchall() or []
    cur.close()

    for sub in subs:
        sub_id      = int(sub["sub_id"])
        tenant_id   = int(sub["tenant_id"])
        email       = sub.get("customer_email") or ""
        name        = (sub.get("customer_first_name") or "there").strip()
        plan_name   = sub.get("plan_name") or "Plan"

        cur2 = conn.cursor()
        try:
            cur2.execute(
                "UPDATE subscriptions SET status='suspended', updated_at=UTC_TIMESTAMP() WHERE id=%s",
                (sub_id,)
            )
            cur2.execute(
                "UPDATE api_keys SET is_active=0 WHERE tenant_id=%s AND key_type='paid'",
                (tenant_id,)
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"⚠️ sub {sub_id}: suspension DB error — {e}")
            cur2.close()
            continue
        finally:
            try: cur2.close()
            except Exception: pass

        insert_audit_log(
            action    = "subscription_suspended",
            tenant_id = tenant_id,
            details   = {"sub_id": sub_id, "grace_days": GRACE_DAYS},
        )

        try:
            if email:
                html = f"""
                <div style="font-family:Arial,sans-serif;max-width:520px">
                  <h2 style="color:#b91c1c">Your PhiXtra subscription has been suspended</h2>
                  <p>Hi {name},</p>
                  <p>Your <b>{plan_name}</b> subscription has been suspended because we
                     were unable to collect payment within the {GRACE_DAYS}-day grace period.</p>
                  <p>Your AI assistant is now paused. To reactivate, please
                     update your payment card and contact support.</p>
                  <p>
                    <a href="{PORTAL_BASE_URL}/billing/add-card"
                       style="background:{BRAND_PRIMARY};color:#fff;padding:10px 18px;
                              border-radius:12px;text-decoration:none;display:inline-block">
                      Update card
                    </a>
                  </p>
                </div>"""
                send_email(email, "PhiXtra subscription suspended", html)
        except Exception: pass


# ── STEP 3: cancel subscriptions flagged for end-of-period cancellation ───────

def _process_cancellations(conn) -> None:
    """Cancel subscriptions where cancel_at_period_end=1 and period has ended."""
    now = _utcnow()

    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT s.id AS sub_id, s.tenant_id, s.customer_id,
               c.email AS customer_email,
               c.first_name AS customer_first_name,
               cp.name AS plan_name
        FROM subscriptions s
        JOIN customers c        ON c.id  = s.customer_id
        JOIN credit_packages cp ON cp.id = s.package_id
        WHERE s.cancel_at_period_end = 1
          AND s.status IN ('active','past_due')
          AND s.current_period_end <= %s
    """, (now.strftime("%Y-%m-%d %H:%M:%S"),))
    subs = cur.fetchall() or []
    cur.close()

    for sub in subs:
        sub_id    = int(sub["sub_id"])
        tenant_id = int(sub["tenant_id"])
        email     = sub.get("customer_email") or ""
        name      = (sub.get("customer_first_name") or "there").strip()
        plan_name = sub.get("plan_name") or "Plan"

        cur2 = conn.cursor()
        try:
            cur2.execute(
                "UPDATE subscriptions SET status='cancelled', updated_at=UTC_TIMESTAMP() WHERE id=%s",
                (sub_id,)
            )
            cur2.execute(
                "UPDATE api_keys SET is_active=0 WHERE tenant_id=%s AND key_type='paid'",
                (tenant_id,)
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"⚠️ sub {sub_id}: cancellation DB error — {e}")
            cur2.close()
            continue
        finally:
            try: cur2.close()
            except Exception: pass

        insert_audit_log(
            action    = "subscription_cancelled_end_of_period",
            tenant_id = tenant_id,
            details   = {"sub_id": sub_id},
        )

        try:
            if email:
                html = f"""
                <div style="font-family:Arial,sans-serif;max-width:520px">
                  <h2 style="color:{BRAND_PRIMARY}">Your PhiXtra subscription has ended</h2>
                  <p>Hi {name},</p>
                  <p>Your <b>{plan_name}</b> subscription has now ended as requested.</p>
                  <p>Your AI assistant has been paused. You can resubscribe at any time.</p>
                  <p>
                    <a href="{PORTAL_BASE_URL}/billing/subscribe"
                       style="background:{BRAND_PRIMARY};color:#fff;padding:10px 18px;
                              border-radius:12px;text-decoration:none;display:inline-block">
                      View plans
                    </a>
                  </p>
                </div>"""
                send_email(email, "Your PhiXtra subscription has ended", html)
        except Exception: pass


# ── Main entry point ──────────────────────────────────────────────────────────

def run_subscription_maintenance() -> None:
    """Run all three maintenance steps. Never raises."""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            print("⚠️ subscription_maintenance: could not connect to DB")
            return

        print("subscription_maintenance: starting")
        _process_renewals(conn)
        _process_suspensions(conn)
        _process_cancellations(conn)
        print("subscription_maintenance: done")

    except Exception as e:
        print("⚠️ subscription_maintenance: unexpected error —", e)
    finally:
        try:
            if conn: conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    run_subscription_maintenance()
