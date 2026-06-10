"""
wa_plan_reset.py — Monthly/annual billing period reset.

Scheduled daily at 00:05 UTC by the gateway scheduler.

Logic:
  - Monthly tenants: if plan_period_start is in a past month, reset to
    the 1st of the current month and clear quota_notified_at.
  - Annual tenants: if plan_period_start + 1 year <= today, reset to
    today and clear quota_notified_at.

This is what makes the AI message quota "refresh" each billing period.
"""

from datetime import date, datetime
from fastapi import APIRouter, Header, HTTPException
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2
import psycopg2.extras

from wa_db import get_db_connection

router = APIRouter()

_INTERNAL_TOKEN  = os.getenv("PHIXTRA_INTERNAL_TOKEN", "")
_PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")
_BRAND           = "#030C18"


def _send_plain_email(to_email: str, subject: str, html: str, text: str = "") -> None:
    """Best-effort SMTP send — never raises."""
    try:
        smtp_host = os.getenv("SMTP_HOST", "")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_pass = os.getenv("SMTP_PASS", "")
        from_addr = os.getenv("SMTP_FROM", smtp_user)
        if not smtp_host or not smtp_user:
            return
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = to_email
        if text:
            msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, [to_email], msg.as_string())
    except Exception as e:
        print(f"⚠️ [EMAIL] send failed to {to_email}: {e}")


def _send_founder_year2_email(email: str, first_name: str, business_name: str) -> None:
    """Notify a founder that their free Year 1 has ended and Year 2 rates now apply."""
    greeting    = first_name.strip() if first_name and first_name.strip() else "there"
    subscribe_url = f"{_PORTAL_BASE_URL}/billing/subscribe"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
      <div style="background:{_BRAND};padding:20px 24px;border-radius:12px 12px 0 0">
        <p style="color:#25D366;font-size:11px;font-weight:800;letter-spacing:.1em;
                  text-transform:uppercase;margin:0 0 6px">Founder&#39;s Offer — Year 2</p>
        <h2 style="color:#fff;margin:0;font-size:22px">Your free year has ended.</h2>
      </div>
      <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;padding:24px">
        <p>Hi {greeting},</p>
        <p>Your Founder Year 1 for <b>{business_name}</b> has now ended. As a Founder,
           your Year 2 is at <b>50% off</b> the standard annual rate.</p>
        <table style="width:100%;border-collapse:collapse;margin:16px 0">
          <tr>
            <td style="padding:9px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700;width:110px">Starter</td>
            <td style="padding:9px 12px;border:1px solid #e5e7eb">
                &#8358;7,125/mo &mdash; billed as &#8358;85,500/yr</td>
          </tr>
          <tr>
            <td style="padding:9px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700">Growth</td>
            <td style="padding:9px 12px;border:1px solid #e5e7eb">
                &#8358;22,800/mo &mdash; billed as &#8358;273,600/yr</td>
          </tr>
        </table>
        <p>Your account has been moved to the Free plan while you choose your Year 2 plan.
           Your data and settings are fully intact.</p>
        <p>
          <a href="{subscribe_url}"
             style="display:inline-block;background:{_BRAND};color:#fff;padding:12px 22px;
                    border-radius:12px;text-decoration:none;font-weight:700;font-size:15px">
            Subscribe to Year 2
          </a>
        </p>
        <p style="color:#6b7280;font-size:13px;margin-top:20px">
          Questions? Contact
          <a href="mailto:support@phixtra.com" style="color:{_BRAND}">support@phixtra.com</a>
        </p>
      </div>
    </div>"""
    _send_plain_email(
        to_email=email,
        subject="Your PhiXtra Founder Year 1 has ended — Year 2 at 50% off",
        html=html,
        text=(
            f"Hi {greeting},\n\n"
            f"Your free Founder Year 1 for {business_name} has ended.\n\n"
            f"Year 2 Founder rates (50% off):\n"
            f"  Starter: ₦7,125/mo (₦85,500/yr)\n"
            f"  Growth:  ₦22,800/mo (₦273,600/yr)\n\n"
            f"Your account is on the Free plan until you subscribe.\n"
            f"Subscribe: {subscribe_url}\n\n"
            f"Questions? support@phixtra.com"
        ),
    )


def run_plan_resets() -> dict:
    """
    Roll forward plan_period_start for all tenants whose period has lapsed.
    Returns {"monthly_reset": n, "annual_reset": n}.
    """
    conn = get_db_connection()
    if not conn:
        print("⚠️ [PLAN RESET] DB unavailable — skipping")
        return {"monthly_reset": 0, "annual_reset": 0}

    cur = conn.cursor()
    results = {"monthly_reset": 0, "annual_reset": 0, "trials_expired": 0}

    try:
        # ── Monthly: reset if period_start is before the 1st of this month ─────
        cur.execute("""
            UPDATE tenants
            SET plan_period_start = DATE_TRUNC('month', NOW())::DATE,
                quota_notified_at  = NULL
            WHERE billing_cycle = 'monthly'
              AND plan_id IS NOT NULL
              AND plan_period_start < DATE_TRUNC('month', NOW())::DATE
        """)
        results["monthly_reset"] = cur.rowcount

        # ── Annual: reset if period_start + 1 year <= today ──────────────────
        cur.execute("""
            UPDATE tenants
            SET plan_period_start = CURRENT_DATE,
                quota_notified_at  = NULL
            WHERE billing_cycle = 'annual'
              AND plan_id IS NOT NULL
              AND plan_period_start + INTERVAL '1 year' <= CURRENT_DATE
        """)
        results["annual_reset"] = cur.rowcount

        conn.commit()

        # ── Founder Year 1 expiry: move to Year 2, downgrade to Free ────────
        # Fetch founders first so we can send transition emails
        cur.execute("""
            SELECT id, name, trial_ends_at
            FROM tenants
            WHERE is_founder = TRUE
              AND founder_year = 1
              AND trial_ends_at IS NOT NULL
              AND trial_ends_at <= CURRENT_DATE
        """)
        founders_transitioning = cur.fetchall() or []

        if founders_transitioning:
            founder_ids = [int(r[0]) for r in founders_transitioning]
            cur.execute("""
                UPDATE tenants
                SET founder_year      = 2,
                    plan_id           = (SELECT id FROM plans WHERE slug='free' LIMIT 1),
                    trial_ends_at     = NULL,
                    quota_notified_at = NULL
                WHERE id = ANY(%s)
            """, (founder_ids,))
            conn.commit()
            print(f"✅ [PLAN RESET] {len(founder_ids)} founder(s) moved to Year 2")

            # Notify each founder that their free year has ended and Year 2 rates apply
            import psycopg2.extras as _extras
            for row in founders_transitioning:
                tenant_id_f = int(row[0])
                business_name_f = row[1] or "your business"
                try:
                    cur_e = conn.cursor(cursor_factory=_extras.RealDictCursor)
                    cur_e.execute("""
                        SELECT c.email, c.first_name
                        FROM customers c
                        WHERE c.tenant_id = %s AND c.email_verified = TRUE AND c.is_active = TRUE
                        LIMIT 1
                    """, (tenant_id_f,))
                    contact = cur_e.fetchone()
                    cur_e.close()
                    if contact and contact.get("email"):
                        _send_founder_year2_email(
                            email=contact["email"],
                            first_name=contact.get("first_name") or "",
                            business_name=business_name_f,
                        )
                except Exception as email_err:
                    print(f"⚠️ [PLAN RESET] founder year2 email failed tenant {tenant_id_f}: {email_err}")

        results["founders_year2"] = len(founders_transitioning)

        # ── Regular trial expiry: downgrade Pro trial tenants to Free ────────
        cur.execute("""
            UPDATE tenants
            SET plan_id       = (SELECT id FROM plans WHERE slug='free' LIMIT 1),
                trial_ends_at = NULL,
                quota_notified_at = NULL
            WHERE (is_founder = FALSE OR is_founder IS NULL)
              AND trial_ends_at IS NOT NULL
              AND trial_ends_at <= CURRENT_DATE
        """)
        results["trials_expired"] = cur.rowcount
        conn.commit()

        total = results["monthly_reset"] + results["annual_reset"] + results.get("trials_expired", 0) + results.get("founders_year2", 0)
        if total:
            print(f"✅ [PLAN RESET] {results['monthly_reset']} monthly + {results['annual_reset']} annual + {results['trials_expired']} trials expired + {results.get('founders_year2', 0)} founders→year2")
        else:
            print("ℹ️ [PLAN RESET] No periods due for reset today")

    except Exception as e:
        conn.rollback()
        print(f"⚠️ [PLAN RESET] Error: {e}")
    finally:
        cur.close()
        conn.close()

    return results


# ── Manual trigger endpoint (admin / testing) ─────────────────────────────────

@router.post("/wa-plan-reset")
def trigger_plan_reset(authorization: str = Header(default="")):
    """
    Manually trigger billing period resets.
    Header: Authorization: Bearer {PHIXTRA_INTERNAL_TOKEN}
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not _INTERNAL_TOKEN or token != _INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorised")

    results = run_plan_resets()
    return {"status": "ok", "results": results, "run_at": datetime.utcnow().isoformat()}
