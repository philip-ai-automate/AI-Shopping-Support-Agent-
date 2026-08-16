"""
ambassador_inactivity_job.py — inactivity policy for ambassadors & sales managers.

Run once daily as a cron job:
    0 6 * * * cd /root/phixtra-app/api-key-manager && ./venv/bin/python3 ambassador_inactivity_job.py >> /var/log/ambassador_inactivity_job.log 2>&1

Policy (approved 2026-07-21):
  Day 3  inactive  -> reminder sent (WhatsApp template + email)
  Day 7  inactive  -> account auto-suspended (suspended_reason='inactivity')
  Day 30 inactive  -> account soft-deleted (PII scrubbed, row kept for
                      commission/lead/audit referential integrity)

Exemption: any ambassador/sales manager with at least one active referred
client (portal, school, or estate) is skipped entirely at every stage,
re-checked on every run.

An admin reactivating the account at any point before day 30 (via the
existing /admin/ambassadors/<id>/reactivate route) clears suspended_reason
and resets the clock, which cancels the day-30 deletion.
"""

import os
import secrets
from datetime import datetime, timezone

import psycopg2.extras

from db import get_db_connection, insert_audit_log
from portal_utils import send_email, hash_password
from ambassador_routes import send_ambassador_wa_template, _active_client_count

PORTAL_LOGIN_URL = "https://portal.phixtra.com/ambassador/login"
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

REMINDER_AFTER_DAYS = 3
SUSPEND_AFTER_DAYS  = 7
DELETE_AFTER_DAYS   = 30


def _has_active_clients(ref_code: str) -> bool:
    for product in ("portal", "school", "estate"):
        try:
            if _active_client_count(ref_code, product) > 0:
                return True
        except Exception as e:
            print(f"   ⚠️ _active_client_count failed for {ref_code}/{product}: {e}")
    return False


def _reminder_email(first_name: str) -> tuple[str, str, str]:
    subject = "Action needed: your PhiXtra Ambassador account is inactive"
    text_body = (
        f"Hi {first_name},\n\n"
        "We noticed you haven't logged into your PhiXtra Ambassador Hub in 3 days.\n\n"
        "Please log in within the next 4 days to keep your account active.\n\n"
        "If you don't, here's what happens:\n"
        "- Your account will be deactivated — you won't be able to log in.\n"
        "- You can email support@phixtra.com to petition for reactivation.\n"
        "- If your petition is unsuccessful, or you don't reach out, your account\n"
        "  will remain inactive and be permanently deleted 30 days from today.\n\n"
        f"Log in here: {PORTAL_LOGIN_URL}\n\n"
        "Note: this notice is only sent to accounts with no active referred\n"
        "clients. If you already have active clients, this does not apply to you."
    )
    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px">
      <h2 style="color:#030C18">Your Ambassador account needs attention</h2>
      <p>Hi {first_name},</p>
      <p>We noticed you haven't logged into your PhiXtra Ambassador Hub in 3 days.</p>
      <p><strong>Please log in within the next 4 days</strong> to keep your account active.</p>
      <p>If you don't, here's what happens:</p>
      <ul style="padding-left:18px">
        <li>Your account will be <strong>deactivated</strong> — you won't be able to log in.</li>
        <li>You can email <a href="mailto:support@phixtra.com">support@phixtra.com</a> to petition for reactivation.</li>
        <li>If your petition is unsuccessful, or you don't reach out, your account will <strong>remain inactive and be permanently deleted 30 days from today</strong>.</li>
      </ul>
      <p><a href="{PORTAL_LOGIN_URL}" style="background:#030C18;color:#fff;padding:10px 18px;border-radius:12px;text-decoration:none;display:inline-block">Log in now</a></p>
      <p style="color:#888;font-size:12px">This doesn't apply if you already have active clients — this notice is only sent to accounts with no active referrals.</p>
    </div>"""
    return subject, html_body, text_body


def _send_reminder(amb: dict) -> None:
    first_name = amb.get("first_name") or "there"
    subject, html_body, text_body = _reminder_email(first_name)
    send_email(amb["email"], subject, html_body, text_body=text_body, cc_email="support@phixtra.com")
    if amb.get("whatsapp_number"):
        send_ambassador_wa_template(
            amb["whatsapp_number"], first_name,
            "You haven't logged in for 3 days. Log in within 4 days to keep your account active, "
            "or it will be deactivated and later deleted if a reactivation petition isn't successful."
        )


def _delete_uploaded_file(rel_path: str | None) -> None:
    if not rel_path:
        return
    full_path = os.path.join(STATIC_DIR, rel_path)
    try:
        if os.path.isfile(full_path):
            os.remove(full_path)
    except Exception as e:
        print(f"   ⚠️ failed to delete file {full_path}: {e}")


def _soft_delete(conn, amb: dict) -> None:
    amb_id = int(amb["id"])
    _delete_uploaded_file(amb.get("id_document_path"))
    _delete_uploaded_file(amb.get("qual_document_path"))

    unusable_hash = hash_password(secrets.token_hex(32))
    placeholder_email = f"deleted-{amb_id}@phixtra.invalid"

    cur = conn.cursor()
    cur.execute("""
        UPDATE ambassadors
        SET status='deleted',
            deleted_at=now(),
            first_name='Deleted',
            last_name='Ambassador',
            email=%s,
            password_hash=%s,
            phone=NULL,
            whatsapp_number=NULL,
            address=NULL,
            date_of_birth=NULL,
            nationality=NULL,
            gender=NULL,
            location=NULL,
            highest_qualification=NULL,
            id_document_path=NULL,
            id_document_type=NULL,
            qual_document_path=NULL,
            bank_name=NULL,
            account_number=NULL,
            account_name=NULL,
            sort_code=NULL,
            swift_code=NULL,
            reset_token=NULL,
            reset_expires_at=NULL
        WHERE id=%s
    """, (placeholder_email, unusable_hash, amb_id))
    conn.commit()
    cur.close()

    insert_audit_log(
        admin_username="system:ambassador_inactivity_job",
        action="ambassador_auto_deleted",
        details={
            "ambassador_id": amb_id,
            "ref_code": amb.get("ref_code"),
            "reason": "30 days inactive, no active clients, petition window elapsed",
        },
    )
    print(f"   🗑️  soft-deleted ambassador id={amb_id} ref_code={amb.get('ref_code')}")


def run() -> None:
    conn = get_db_connection()
    if not conn:
        print("❌ no DB connection — aborting run")
        return

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM ambassadors
        WHERE role IN ('ambassador', 'sales_manager')
          AND status IN ('active', 'suspended')
    """)
    candidates = cur.fetchall() or []
    cur.close()

    now = datetime.now(timezone.utc)
    reminded = suspended = deleted = skipped_has_clients = 0

    for amb in candidates:
        amb = dict(amb)
        ref_code = amb.get("ref_code")

        if ref_code and _has_active_clients(ref_code):
            skipped_has_clients += 1
            continue

        reference_point = amb.get("last_login_at") or amb.get("approved_at") or amb.get("created_at")
        if reference_point is None:
            continue
        if reference_point.tzinfo is None:
            reference_point = reference_point.replace(tzinfo=timezone.utc)
        inactive_days = (now - reference_point).days

        if amb["status"] == "active":
            if inactive_days >= SUSPEND_AFTER_DAYS:
                cur2 = conn.cursor()
                cur2.execute("""
                    UPDATE ambassadors
                    SET status='suspended', suspended_at=now(), suspended_reason='inactivity'
                    WHERE id=%s
                """, (amb["id"],))
                cur2.execute("""
                    UPDATE ambassador_products SET status='suspended'
                    WHERE ambassador_id=%s AND product='portal'
                """, (amb["id"],))
                conn.commit()
                cur2.close()
                insert_audit_log(
                    admin_username="system:ambassador_inactivity_job",
                    action="ambassador_auto_suspended",
                    details={"ambassador_id": amb["id"], "ref_code": ref_code, "inactive_days": inactive_days},
                )
                suspended += 1
                print(f"   ⏸  auto-suspended ambassador id={amb['id']} ref_code={ref_code} ({inactive_days}d inactive)")
            elif inactive_days >= REMINDER_AFTER_DAYS and not amb.get("inactivity_reminder_sent_at"):
                _send_reminder(amb)
                cur2 = conn.cursor()
                cur2.execute(
                    "UPDATE ambassadors SET inactivity_reminder_sent_at=now() WHERE id=%s",
                    (amb["id"],)
                )
                conn.commit()
                cur2.close()
                reminded += 1
                print(f"   ✉️  reminder sent to ambassador id={amb['id']} ref_code={ref_code} ({inactive_days}d inactive)")

        elif amb["status"] == "suspended" and amb.get("suspended_reason") == "inactivity":
            if inactive_days >= DELETE_AFTER_DAYS:
                _soft_delete(conn, amb)
                deleted += 1

    conn.close()
    print(f"✅ ambassador_inactivity_job done: {reminded} reminded, {suspended} suspended, "
          f"{deleted} deleted, {skipped_has_clients} skipped (has active clients)")


if __name__ == "__main__":
    run()
