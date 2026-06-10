"""Trial maintenance jobs.

Run this daily via cron (recommended):

  cd /path/to/api-key-manager
  /path/to/venv/bin/python trial_jobs.py

This will:
  - Deactivate expired trial API keys
  - Send reminder emails 3/2/1 days before trial expiry (once per key/day)
"""

from __future__ import annotations

from datetime import datetime

import psycopg2.extras

from db import get_db_connection, insert_audit_log
from portal_utils import send_email


UPGRADE_LINK = "https://phixtra.com/subscription-plans/"


def _pick_customer_email(cur, tenant_id: int) -> str | None:
    cur.execute(
        """
        SELECT email
        FROM customers
        WHERE tenant_id=%s AND email_verified=TRUE AND is_active=TRUE
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (tenant_id,),
    )
    row = cur.fetchone() or {}
    email = (row.get("email") or "").strip().lower()
    return email or None


def run():
    conn = get_db_connection()
    if not conn:
        print("DB unavailable")
        return

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 1) Deactivate expired trials
    cur.execute(
        """
        SELECT id, tenant_id, website, trial_expires_at
        FROM api_keys
        WHERE key_type='trial'
          AND is_active=TRUE
          AND trial_expires_at IS NOT NULL
          AND trial_expires_at <= NOW()
        """
    )
    expired = cur.fetchall() or []
    for r in expired:
        api_key_id = int(r["id"])
        tenant_id = int(r["tenant_id"])
        website = r.get("website")

        cur2 = conn.cursor()
        cur2.execute("UPDATE api_keys SET is_active=FALSE WHERE id=%s", (api_key_id,))
        conn.commit()
        cur2.close()

        insert_audit_log(
            admin_username=None,
            action="trial_expired_deactivated_cron",
            tenant_id=tenant_id,
            website=website,
            key_type="trial",
            api_key_id=api_key_id,
            api_key_last4=None,
            api_key_plain=None,
            details={"trial_expires_at": str(r.get("trial_expires_at"))},
        )

        # Send trial expiry email
        email = _pick_customer_email(cur, tenant_id)
        if email:
            exp_str = (
                r["trial_expires_at"].strftime("%Y-%m-%d")
                if hasattr(r.get("trial_expires_at"), "strftime")
                else str(r.get("trial_expires_at", ""))
            )
            subject = "Your PhiXtra trial has ended — here's how to continue"
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
              <h2 style="color:#030C18">Your PhiXtra free trial has ended</h2>
              <p>Hi there,</p>
              <p>Your 14-day free trial for <b>{website}</b> ended on <b>{exp_str}</b>.</p>
              <p>Your AI assistant has been <b>paused</b> — your shoppers will no longer see it until you upgrade.</p>
              <p style="margin:6px 0 4px"><b>When you upgrade:</b></p>
              <ul style="margin:0 0 16px;padding-left:20px;line-height:1.8">
                <li>Your AI assistant reactivates <b>immediately</b></li>
                <li>All your setup and instructions are preserved</li>
                <li>Credits never expire</li>
              </ul>
              <p>
                <a href="{UPGRADE_LINK}"
                   style="display:inline-block;background:#030C18;color:#fff;padding:12px 22px;
                          border-radius:12px;text-decoration:none;font-weight:700;font-size:15px">
                  Upgrade Now →
                </a>
              </p>
              <p style="margin-top:20px;color:#6b7280;font-size:13px">
                Questions? Reply to this email or contact
                <a href="mailto:support@phixtra.com" style="color:#030C18">support@phixtra.com</a>
                — we're happy to help.
              </p>
            </div>
            """
            send_email(
                email,
                subject,
                html,
                text_body=(
                    f"Your PhiXtra free trial for {website} has ended.\n"
                    f"Upgrade to reactivate your AI assistant: {UPGRADE_LINK}"
                ),
            )

    # 2) Reminder emails (3,2,1 days before expiry)
    for days_before in (3, 2, 1):
        cur.execute(
            """
            SELECT ak.id, ak.tenant_id, ak.website, ak.trial_expires_at
            FROM api_keys ak
            LEFT JOIN trial_reminders tr
              ON tr.api_key_id = ak.id AND tr.days_before = %s
            WHERE ak.key_type='trial'
              AND ak.is_active=TRUE
              AND ak.trial_expires_at IS NOT NULL
              AND DATE(ak.trial_expires_at) = DATE(NOW() + (INTERVAL '1 day' * %s))
              AND tr.id IS NULL
            """,
            (days_before, days_before),
        )
        rows = cur.fetchall() or []
        for r in rows:
            api_key_id = int(r["id"])
            tenant_id = int(r["tenant_id"])
            website = r.get("website")
            expires_at = r.get("trial_expires_at")
            email = _pick_customer_email(cur, tenant_id)
            if not email:
                continue

            exp_str = expires_at.strftime("%Y-%m-%d") if hasattr(expires_at, "strftime") else str(expires_at)
            subject = f"Your PhiXtra free trial ends in {days_before} day{'s' if days_before != 1 else ''}"
            html = f"""
            <div style="font-family:Arial,sans-serif">
              <h2 style="color:#030C18">Trial reminder</h2>
              <p>Your PhiXtra <b>14-day free trial</b> for <b>{website}</b> ends in <b>{days_before}</b> day{'s' if days_before != 1 else ''}.</p>
              <p><b>Expiry date:</b> {exp_str}</p>
              <p>To continue using PhiXtra after the trial, please upgrade here:</p>
              <p><a href="{UPGRADE_LINK}" style="background:#030C18;color:#fff;padding:10px 14px;border-radius:12px;text-decoration:none">Upgrade</a></p>
            </div>
            """
            send_email(email, subject, html, text_body=f"Trial ends in {days_before} day(s). Upgrade: {UPGRADE_LINK}")

            cur2 = conn.cursor()
            cur2.execute(
                "INSERT INTO trial_reminders (api_key_id, days_before) VALUES (%s, %s)",
                (api_key_id, days_before),
            )
            conn.commit()
            cur2.close()

            insert_audit_log(
                admin_username=None,
                action="trial_reminder_sent",
                tenant_id=tenant_id,
                website=website,
                key_type="trial",
                api_key_id=api_key_id,
                api_key_last4=None,
                api_key_plain=None,
                details={"days_before": days_before, "to": email, "trial_expires_at": exp_str},
            )

    cur.close()
    conn.close()
    print(f"OK {datetime.utcnow().isoformat()}Z")


if __name__ == "__main__":
    run()
