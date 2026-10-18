"""Inactive accounts — warn, then delete Portal/Connect businesses nobody uses.

Approved by the user 2026-10-18:
  * 60 days with no activity → the business is deleted.
  * Three warning emails first (day 45, 7 days before, 1 day before). The
    deletion date is set when the first warning goes out and only ever moves
    later, so every account holder gets all three warnings with real notice.
  * Paying businesses, PhiXtra's own and demo accounts, and accounts an admin
    marks "Keep" are never touched. Accounts with no working email can't be
    warned, so they are never deleted either.
  * Email only. Activity counted from 10 Oct 2026, when logins started being
    recorded, so nobody is treated as inactive from before that.

"Deleted" means everything the business owns is removed, and the account row
itself is blanked to "Deleted account #id". Invoices, subscriptions, usage
and audit records stay, as UK rules need financial records kept; the "How did
you hear about us" answer stays too (it holds no personal data).

Used by inactivity_jobs.py (daily cron) and the admin Inactive Accounts page.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2.extras

from db import get_db_connection, insert_audit_log

TRACKING_START = datetime(2026, 10, 10, tzinfo=timezone.utc)
WARN_AFTER_DAYS = 45
NOTICE_DAYS = 15          # first warning → deletion
SECOND_WARNING_DAYS = 7   # days before deletion
FINAL_WARNING_DAYS = 1
DELAY_DAYS = 30

# PhiXtra's own businesses (17 ProfitBuyz, 19 PhiXtra AI) are never touched.
PROTECTED_TENANT_IDS = (17, 19)

# Emails nobody reads: placeholders made at WhatsApp sign-up and our own.
NO_EMAIL_DOMAINS = ("wa.phixtra.internal", "phixtra.com", "profitbuyz.com")

MODES = ("off", "test", "on")
MODE_LABELS = {"off": "Off", "test": "Test mode", "on": "On"}

LOGIN_URL = "https://portal.phixtra.com/login"
REGISTER_URL = "https://portal.phixtra.com/register"

# Tables whose rows stay when a business is deleted (financial and audit
# records, PhiXtra's own ambassador/sales records), plus the account rows
# that are blanked instead. re_* tables use KeyLand's own tenant ids.
KEEP_TABLES = {
    "tenants", "customers", "invoices", "subscription_invoices", "subscriptions",
    "plan_subscriptions", "audit_logs", "usage_events", "quota_overage_log",
    "tenant_balances", "ambassador_commissions", "ambassador_leads", "ambassadors",
    "account_inactivity",
}

# Files the business uploaded or had made (posts, designs, campaign pictures)
# are named with a random 32-character id (rows often hold just the name), so
# a match can only be theirs. Looked for in each of these upload folders.
_UPLOAD_RE = re.compile(r"\b((?:ai_)?[0-9a-f]{32}\.(?:png|jpe?g|webp|gif|pdf|mp4|mov))\b")
_UPLOAD_FOLDERS = ("ai_designs", "brand_kits", "campaign_images", "tenant_social_posts", "email_campaign_images")
_UPLOAD_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    conn, cur = _db()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS account_inactivity (
                tenant_id         INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
                last_seen_at      TIMESTAMPTZ,
                keep              BOOLEAN NOT NULL DEFAULT FALSE,
                keep_by           TEXT,
                delayed_to        TIMESTAMPTZ,
                cycle_activity_at TIMESTAMPTZ,
                warn1_at          TIMESTAMPTZ,
                warn2_at          TIMESTAMPTZ,
                warn3_at          TIMESTAMPTZ,
                deletion_on       DATE,
                deleted_at        TIMESTAMPTZ,
                deleted_by        TEXT,
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS account_inactivity_settings (
                id         SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                mode       VARCHAR(8) NOT NULL DEFAULT 'test',
                changed_by TEXT,
                changed_at TIMESTAMPTZ
            )""")
        cur.execute("INSERT INTO account_inactivity_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS account_inactivity_log (
                id            SERIAL PRIMARY KEY,
                tenant_id     INTEGER NOT NULL,
                business_name TEXT,
                email         TEXT,
                action        VARCHAR(20) NOT NULL,
                detail        TEXT,
                mode          VARCHAR(8) NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        conn.commit()
        _tables_ready = True
    finally:
        cur.close(); conn.close()


def get_mode() -> str:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT mode FROM account_inactivity_settings WHERE id=1")
        row = cur.fetchone()
        return row["mode"] if row and row["mode"] in MODES else "test"
    finally:
        cur.close(); conn.close()


def set_mode(mode: str, by: str):
    if mode not in MODES:
        raise ValueError(mode)
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("UPDATE account_inactivity_settings SET mode=%s, changed_by=%s, changed_at=NOW() WHERE id=1",
                    (mode, by))
        conn.commit()
    finally:
        cur.close(); conn.close()


def mark_seen(tenant_id: int):
    """Someone used the portal for this business (called at most hourly)."""
    try:
        ensure_tables()
        conn, cur = _db()
        try:
            cur.execute("""INSERT INTO account_inactivity (tenant_id, last_seen_at) VALUES (%s, NOW())
                           ON CONFLICT (tenant_id) DO UPDATE SET last_seen_at=NOW(), updated_at=NOW()""",
                        (int(tenant_id),))
            conn.commit()
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ inactivity mark_seen:", e)


def _usable_email(email, verified) -> bool:
    email = (email or "").strip().lower()
    if not verified or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
        return False
    return email.split("@", 1)[1] not in NO_EMAIL_DOMAINS


def accounts(tenant_ids=None) -> list[dict]:
    """Every Portal/Connect business with its inactivity status and next step."""
    ensure_tables()
    conn, cur = _db()
    try:
        where = "t.is_demo = FALSE"
        params = [TRACKING_START]
        if tenant_ids is not None:
            where += " AND t.id = ANY(%s)"
            params.append(list(tenant_ids))
        cur.execute(f"""
            SELECT t.id AS tenant_id, t.name AS business_name, t.status,
                   COALESCE(p.price_dollars,0) + COALESCE(p.price_ngn,0) + COALESCE(p.price_usd,0) AS plan_price,
                   COALESCE(p.name,'Free') AS plan_name,
                   o.id AS customer_id, o.first_name, o.email, o.email_verified, o.created_at AS signed_up,
                   EXISTS (SELECT 1 FROM invoices i WHERE i.tenant_id=t.id AND i.status='paid'
                                                    AND COALESCE(i.amount_pence,0) > 0) AS has_paid,
                   EXISTS (SELECT 1 FROM subscriptions s WHERE s.tenant_id=t.id
                                                         AND s.status IN ('active','trialing','past_due')) AS has_sub,
                   a.keep, a.keep_by, a.cycle_activity_at, a.warn1_at, a.warn2_at, a.warn3_at,
                   a.deletion_on, a.deleted_at, a.deleted_by,
                   GREATEST(
                       %s::timestamptz,
                       (SELECT MAX(created_at)    FROM customers c2      WHERE c2.tenant_id=t.id),
                       (SELECT MAX(last_login_at) FROM customers c3      WHERE c3.tenant_id=t.id),
                       (SELECT MAX(last_login_at) FROM team_members tm   WHERE tm.tenant_id=t.id),
                       a.last_seen_at, a.delayed_to,
                       (SELECT MAX(created_at) FROM usage_events u        WHERE u.tenant_id=t.id),
                       (SELECT MAX(created_at) FROM wa_message_log w      WHERE w.tenant_id=t.id),
                       (SELECT MAX(created_at) FROM wa_campaigns wc       WHERE wc.tenant_id=t.id),
                       (SELECT MAX(created_at) FROM email_campaigns ec    WHERE ec.tenant_id=t.id),
                       (SELECT MAX(created_at) FROM tenant_social_posts sp WHERE sp.tenant_id=t.id)
                   ) AS last_activity
            FROM tenants t
            JOIN LATERAL (SELECT * FROM customers c WHERE c.tenant_id=t.id
                          ORDER BY c.created_at ASC LIMIT 1) o ON TRUE
            LEFT JOIN plans p ON p.id=t.plan_id
            LEFT JOIN account_inactivity a ON a.tenant_id=t.id
            WHERE {where}
        """, params)
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    now = datetime.now(timezone.utc)
    return [_assess(dict(r), now) for r in rows]


def _assess(r: dict, now: datetime) -> dict:
    """Adds status, status_label, days_inactive, next_step, next_on, due."""
    today = now.date()
    la = r["last_activity"]
    r["days_inactive"] = max(0, (now - la).days)
    r["can_email"] = _usable_email(r["email"], r["email_verified"])
    r["due"] = None  # action the daily job should take today
    # Activity since the warnings started → the warnings no longer apply.
    r["reset"] = bool(r["warn1_at"] and r["cycle_activity_at"] and la > r["cycle_activity_at"])
    if r["reset"]:
        r["warn1_at"] = r["warn2_at"] = r["warn3_at"] = r["deletion_on"] = None

    def done(status, label, next_step=None, next_on=None):
        r.update(status=status, status_label=label, next_step=next_step, next_on=next_on)
        return r

    if r["deleted_at"]:
        return done("deleted", "Deleted")
    if r["tenant_id"] in PROTECTED_TENANT_IDS:
        return done("protected", "Protected · PhiXtra")
    if r["plan_price"] > 0 or r["has_paid"] or r["has_sub"]:
        return done("protected", "Protected · paying")
    if r["keep"]:
        return done("protected", "Protected · kept by admin")
    if not r["can_email"]:
        return done("no_email", "Can't warn · no working email")

    if not r["warn1_at"]:
        first_on = (la + timedelta(days=WARN_AFTER_DAYS)).date()
        if today >= first_on:
            r["due"] = "warn1"
            r["deletion_on"] = max((la + timedelta(days=WARN_AFTER_DAYS + NOTICE_DAYS)).date(),
                                   today + timedelta(days=NOTICE_DAYS))
        return done("active", "Active", "First warning", max(first_on, today))

    d = r["deletion_on"]
    if not r["warn2_at"]:
        on = d - timedelta(days=SECOND_WARNING_DAYS)
        if today >= on:
            r["due"] = "warn2"
            r["deletion_on"] = max(d, today + timedelta(days=SECOND_WARNING_DAYS))
        return done("warned", "Warned", "Second warning", max(on, today))
    if not r["warn3_at"]:
        on = d - timedelta(days=FINAL_WARNING_DAYS)
        if today >= on:
            r["due"] = "warn3"
            r["deletion_on"] = max(d, today + timedelta(days=FINAL_WARNING_DAYS))
        return done("warned", "Warned", "Final warning", max(on, today))
    if today >= d and (now - r["warn3_at"]) >= timedelta(hours=20):
        r["due"] = "delete"
    return done("warned", "Warned", "Deletion", max(d, today))


# ── Admin actions ────────────────────────────────────────────────────────────

def set_keep(tenant_id: int, keep: bool, by: str):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO account_inactivity (tenant_id, keep, keep_by) VALUES (%s,%s,%s)
                       ON CONFLICT (tenant_id) DO UPDATE SET keep=EXCLUDED.keep, keep_by=EXCLUDED.keep_by,
                           warn1_at=NULL, warn2_at=NULL, warn3_at=NULL, deletion_on=NULL, updated_at=NOW()""",
                    (int(tenant_id), keep, by if keep else None))
        conn.commit()
    finally:
        cur.close(); conn.close()


def delay(tenant_id: int, by: str):
    """Moves everything 30 days later: counted as activity 30 days after the
    current last activity, so any warnings already sent start again."""
    acct = accounts([tenant_id])
    if not acct:
        return
    to = acct[0]["last_activity"] + timedelta(days=DELAY_DAYS)
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO account_inactivity (tenant_id, delayed_to) VALUES (%s,%s)
                       ON CONFLICT (tenant_id) DO UPDATE SET delayed_to=EXCLUDED.delayed_to, updated_at=NOW()""",
                    (int(tenant_id), to))
        conn.commit()
    finally:
        cur.close(); conn.close()
    insert_audit_log(admin_username=by, action="inactivity_delay", tenant_id=int(tenant_id),
                     details={"counted_active_until": to.isoformat()})


def log(tenant_id, business_name, email, action, detail, mode):
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO account_inactivity_log (tenant_id, business_name, email, action, detail, mode)
                       VALUES (%s,%s,%s,%s,%s,%s)""", (tenant_id, business_name, email, action, detail, mode))
        conn.commit()
    finally:
        cur.close(); conn.close()


def purge_old_log():
    """The warning history (proof of notice) is kept 3 years — Privacy Policy,
    Data Retention table."""
    conn, cur = _db()
    try:
        cur.execute("DELETE FROM account_inactivity_log WHERE created_at < NOW() - INTERVAL '3 years'")
        conn.commit()
    finally:
        cur.close(); conn.close()


def recent_log(limit=200) -> list[dict]:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM account_inactivity_log ORDER BY created_at DESC LIMIT %s", (limit,))
        return cur.fetchall() or []
    finally:
        cur.close(); conn.close()


def record_warning(r: dict, n: int):
    """Saves that warning n (1-3) went out, with the deletion date it gave."""
    if n not in (1, 2, 3):
        raise ValueError(n)
    conn, cur = _db()
    try:
        cur.execute("INSERT INTO account_inactivity (tenant_id) VALUES (%s) ON CONFLICT (tenant_id) DO NOTHING",
                    (int(r["tenant_id"]),))
        if n == 1:
            cur.execute("""UPDATE account_inactivity SET warn1_at=NOW(), warn2_at=NULL, warn3_at=NULL,
                               deletion_on=%s, cycle_activity_at=%s, updated_at=NOW() WHERE tenant_id=%s""",
                        (r["deletion_on"], r["last_activity"], int(r["tenant_id"])))
        else:
            cur.execute(f"""UPDATE account_inactivity SET warn{n}_at=NOW(), deletion_on=%s, updated_at=NOW()
                            WHERE tenant_id=%s""", (r["deletion_on"], int(r["tenant_id"])))
        conn.commit()
    finally:
        cur.close(); conn.close()


def clear_warnings(tenant_id: int):
    conn, cur = _db()
    try:
        cur.execute("""UPDATE account_inactivity SET warn1_at=NULL, warn2_at=NULL, warn3_at=NULL,
                           deletion_on=NULL, cycle_activity_at=NULL, updated_at=NOW() WHERE tenant_id=%s""",
                    (int(tenant_id),))
        conn.commit()
    finally:
        cur.close(); conn.close()


# ── Deleting a business ──────────────────────────────────────────────────────

def _tenant_tables(cur) -> list[str]:
    cur.execute("""SELECT c.table_name FROM information_schema.columns c
                   JOIN information_schema.tables t ON t.table_schema=c.table_schema
                        AND t.table_name=c.table_name AND t.table_type='BASE TABLE'
                   WHERE c.table_schema='public' AND c.column_name='tenant_id'
                   ORDER BY c.table_name""")
    return [row["table_name"] for row in cur.fetchall()
            if row["table_name"] not in KEEP_TABLES and not row["table_name"].startswith("re_")]


def delete_business(tenant_id: int, by: str) -> dict:
    """Removes everything the business owns and blanks the account. All or
    nothing: if any table can't be cleared the whole delete is undone."""
    tenant_id = int(tenant_id)
    if tenant_id in PROTECTED_TENANT_IDS:
        raise ValueError("PhiXtra's own business can't be deleted")
    conn, cur = _db()
    files, removed = set(), {}
    try:
        cur.execute("SELECT is_demo FROM tenants WHERE id=%s FOR UPDATE", (tenant_id,))
        t = cur.fetchone()
        if not t or t["is_demo"]:
            raise ValueError("not a deletable business")
        tables = _tenant_tables(cur)
        for tbl in tables:
            cur.execute(f'SELECT row_to_json(x)::text AS j FROM "{tbl}" x WHERE tenant_id=%s', (tenant_id,))
            for row in cur.fetchall():
                files.update(_UPLOAD_RE.findall(row["j"]))
        # Tables point at each other, so keep going round until every one is
        # empty for this business; stop if a pass makes no progress.
        pending = list(tables)
        while pending:
            left = []
            for tbl in pending:
                cur.execute("SAVEPOINT del_tbl")
                try:
                    cur.execute(f'DELETE FROM "{tbl}" WHERE tenant_id=%s', (tenant_id,))
                    if cur.rowcount:
                        removed[tbl] = removed.get(tbl, 0) + cur.rowcount
                    cur.execute("RELEASE SAVEPOINT del_tbl")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT del_tbl")
                    left.append(tbl)
            if len(left) == len(pending):
                raise RuntimeError("couldn't clear: " + ", ".join(left))
            pending = left

        cur.execute("""UPDATE tenants SET name=%s, domain=%s, status='cancelled', system_prompt=NULL,
                           web_system_prompt=NULL, onboarding_other=NULL, report_phone=NULL,
                           daily_report_enabled=FALSE, ai_enabled=FALSE, crm_enabled=FALSE,
                           meta_ai_enabled=FALSE, ref_code=NULL
                       WHERE id=%s""",
                    (f"Deleted account #{tenant_id}", f"deleted-{tenant_id}.invalid", tenant_id))
        locked = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt()).decode()
        cur.execute("""UPDATE customers SET first_name='Deleted', last_name='account',
                           email='deleted-' || id || '@deleted.invalid', phone_number=NULL,
                           password_hash=%s, is_active=FALSE, email_verified=FALSE, verify_token=NULL,
                           reset_token=NULL, phone_verified=FALSE, avatar_data=NULL, company_name=NULL,
                           handoff_notify_email=NULL, billing_address_line1=NULL, billing_city=NULL,
                           billing_postcode=NULL, vat_number=NULL
                       WHERE tenant_id=%s""", (locked, tenant_id))
        cur.execute("""INSERT INTO account_inactivity (tenant_id, deleted_at, deleted_by) VALUES (%s, NOW(), %s)
                       ON CONFLICT (tenant_id) DO UPDATE SET deleted_at=NOW(), deleted_by=EXCLUDED.deleted_by,
                           updated_at=NOW()""", (tenant_id, by))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()

    gone = 0
    for name in files:
        for folder in _UPLOAD_FOLDERS:
            try:
                os.remove(os.path.join(_UPLOAD_ROOT, folder, name))
                gone += 1
            except FileNotFoundError:
                pass
            except Exception as e:
                print("⚠️ inactivity file delete:", folder, name, e)
    insert_audit_log(admin_username=by, action="inactive_account_deleted", tenant_id=tenant_id,
                     details={"rows_removed": removed, "files_removed": gone})
    return {"rows_removed": removed, "files_removed": gone}


# ── Emails ───────────────────────────────────────────────────────────────────

def _d(day) -> str:
    return f"{day.day} {day.strftime('%B %Y')}"


def email_content(kind: str, r: dict) -> tuple[str, str, list, str, str]:
    """(subject, heading, paragraphs, button text, button link) for one email."""
    from html import escape
    first = escape((r.get("first_name") or "").strip() or "there")
    biz = escape(r.get("business_name") or "your business")
    when = _d(r["deletion_on"]) if r.get("deletion_on") else ""
    what = "all its contacts, leads, chats and its WhatsApp connection"
    if kind == "warn1":
        return (f"Your PhiXtra account will be deleted on {when}",
                "We haven't seen you for a while",
                [f"Hi {first}, your PhiXtra account for <b>{biz}</b> hasn't been used for {r['days_inactive']} days.",
                 f"Accounts that aren't used for 60 days are deleted. Yours will be deleted on <b>{when}</b>, "
                 f"with {what}. This can't be undone.",
                 "To keep your account, just log in before then."],
                "Log in to keep my account", LOGIN_URL)
    if kind == "warn2":
        return (f"Reminder: your PhiXtra account will be deleted on {when}",
                "One week left to keep your account",
                [f"Hi {first}, this is a reminder that your PhiXtra account for <b>{biz}</b> "
                 f"will be deleted on <b>{when}</b> because it hasn't been used.",
                 "Log in before then and nothing will be deleted."],
                "Log in to keep my account", LOGIN_URL)
    if kind == "warn3":
        return ("Final notice: your PhiXtra account will be deleted tomorrow",
                "Your account will be deleted tomorrow",
                [f"Hi {first}, your PhiXtra account for <b>{biz}</b> will be deleted on <b>{when}</b>, "
                 f"with {what}.",
                 "If you'd like to keep it, log in today."],
                "Log in to keep my account", LOGIN_URL)
    return ("Your PhiXtra account has been deleted",
            "Your account has been deleted",
            [f"Hi {first}, as we let you know by email, your PhiXtra account for <b>{biz}</b> "
             "was deleted today because it wasn't used for 60 days.",
             "You're welcome to sign up again any time."],
            "Sign up again", REGISTER_URL)


def email_html(kind: str, r: dict) -> tuple[str, str]:
    from portal_routes import _render_comeback_email_html
    subject, heading, paragraphs, cta, url = email_content(kind, r)
    html = _render_comeback_email_html(
        heading, paragraphs, cta, url, unsubscribe_url=None,
        final_note="Questions? Reply to this email or contact support@phixtra.com.")
    return subject, html
