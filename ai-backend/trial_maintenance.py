"""Trial maintenance job.

Runs safely as a cron job:
- Deactivates trial keys after trial_expires_at
- Sends reminder emails 3 / 2 / 1 days before expiry

This script NEVER raises (so cron won't spam failure loops).

Usage (example):
  python3 -m trial_maintenance
"""

from __future__ import annotations

from datetime import datetime, timezone

from db import get_db_connection, insert_audit_log
from billing import send_email

BRAND_PRIMARY = "#030C18"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_trial_maintenance():
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return

        cur = conn.cursor(dictionary=True)

        # Fetch active trial keys with expiry
        cur.execute(
            """
            SELECT
              ak.id AS api_key_id,
              ak.tenant_id,
              ak.website,
              ak.is_active,
              ak.key_type,
              ak.trial_expires_at,
              t.domain AS tenant_domain
            FROM api_keys ak
            JOIN tenants t ON t.id = ak.tenant_id
            WHERE ak.key_type='trial'
              AND ak.is_active=1
              AND ak.trial_expires_at IS NOT NULL
              AND t.status='active'
            """
        )
        keys = cur.fetchall() or []

        now = _utcnow()

        for k in keys:
            api_key_id = int(k["api_key_id"])
            tenant_id = int(k["tenant_id"])
            expires_at = k.get("trial_expires_at")
            if not expires_at:
                continue

            # MySQL DATETIME comes back naive -> treat as UTC
            expires_utc = expires_at.replace(tzinfo=timezone.utc)
            remaining = expires_utc - now
            remaining_days = int(remaining.total_seconds() // 86400)

            # Ensure reminder state row exists
            cur2 = conn.cursor()
            cur2.execute(
                "INSERT IGNORE INTO trial_reminder_state (api_key_id, sent_3d, sent_2d, sent_1d) VALUES (%s, 0, 0, 0)",
                (api_key_id,),
            )
            conn.commit()
            cur2.close()

            # Load reminder flags
            cur3 = conn.cursor(dictionary=True)
            cur3.execute("SELECT * FROM trial_reminder_state WHERE api_key_id=%s", (api_key_id,))
            st = cur3.fetchone() or {}
            cur3.close()

            # Expired -> deactivate
            if now >= expires_utc:
                cur2 = conn.cursor()
                cur2.execute("UPDATE api_keys SET is_active=0 WHERE id=%s", (api_key_id,))
                conn.commit()
                cur2.close()

                insert_audit_log(
                    action="trial_expired_deactivated_cron",
                    tenant_id=tenant_id,
                    website=k.get("website"),
                    key_type="trial",
                    api_key_id=api_key_id,
                    details={"expired_at_utc": expires_utc.isoformat()},
                )
                continue

            # Reminder windows (3, 2, 1 days before)
            remind_map = {
                3: ("sent_3d", "3 days"),
                2: ("sent_2d", "2 days"),
                1: ("sent_1d", "1 day"),
            }

            if remaining_days in remind_map:
                flag, label = remind_map[remaining_days]
                if int(st.get(flag) or 0) == 1:
                    continue

                # Notify all verified, active customers of the tenant
                cur4 = conn.cursor(dictionary=True)
                cur4.execute(
                    """
                    SELECT email
                    FROM customers
                    WHERE tenant_id=%s AND email_verified=1 AND is_active=1
                    """,
                    (tenant_id,),
                )
                recipients = [r.get("email") for r in (cur4.fetchall() or []) if r.get("email")]
                cur4.close()

                if recipients:
                    upgrade_url = "https://phixtra.com/subscription-plans/"
                    html = f"""
                    <div style=\"font-family:Arial,sans-serif\">
                      <h2 style=\"color:{BRAND_PRIMARY}\">Your PhiXtra trial ends in {label} ⏳</h2>
                      <p>This is a reminder that your PhiXtra trial API key will expire on <b>{expires_utc.strftime('%Y-%m-%d')}</b> (UTC).</p>
                      <p>To keep your AI assistant running without interruption, please upgrade:</p>
                      <p><a href=\"{upgrade_url}\" style=\"background:{BRAND_PRIMARY};color:#fff;padding:10px 14px;border-radius:12px;text-decoration:none\">Upgrade Now</a></p>
                    </div>
                    """
                    for to_email in recipients:
                        send_email(to_email, f"PhiXtra Trial Reminder: {label} left", html, text_body=f"Trial ends in {label}. Upgrade: {upgrade_url}")

                cur2 = conn.cursor()
                cur2.execute(f"UPDATE trial_reminder_state SET {flag}=1 WHERE api_key_id=%s", (api_key_id,))
                conn.commit()
                cur2.close()

                insert_audit_log(
                    action="trial_reminder_sent",
                    tenant_id=tenant_id,
                    website=k.get("website"),
                    key_type="trial",
                    api_key_id=api_key_id,
                    details={"days_remaining": remaining_days, "recipients": len(recipients) if recipients else 0},
                )

        cur.close()

    except Exception as e:
        print("⚠️ trial_maintenance failed:", e)
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    run_trial_maintenance()
