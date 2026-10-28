"""
signup_confirm.py — no account until the WhatsApp number AND the email are
confirmed (2026-10-27, approved mock-up https://claude.ai/artifact/YA9JNRrwNURksofBSHUfc9).

  - The sign-up form's details wait in pending_signups (password already
    scrambled). A 6-digit code goes to their WhatsApp (the login-code
    template, kept in wa_portal_otp like every other WhatsApp code) and a
    6-digit code goes to their email (kept on the row here).
  - Only when both are right is the account created, already confirmed.
  - Not finished within 60 minutes → the personal details are wiped. The
    bare row stays only so Admin › Customers can count started / finished /
    didn't finish — no names or numbers, so nobody chases them.
  - kind='login': an existing owner whose number or email was never
    confirmed is asked for only what's missing before getting in. These rows
    are left out of the sign-up counts.

No Flask in here. The table was created once by hand (never DDL at request
time — see the accounts_core incident of 2026-10-10).
"""
import json
import random
import secrets

import psycopg2.extras

from db import get_db_connection

HOLD_MINUTES = 60
CODE_MINUTES = 10
MAX_WRONG    = 10     # wrong codes before the sign-up is thrown away

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_signups (
    id            SERIAL PRIMARY KEY,
    token         TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL DEFAULT 'signup',   -- signup | login
    flow          TEXT,                             -- register | wp_connect | login | wa_login
    creator       TEXT,                             -- whatsapp | web
    args          JSONB,
    form          JSONB,
    pw_hash       TEXT,
    phone         TEXT,
    email         TEXT,
    customer_id   INTEGER,
    wa_ok         BOOLEAN NOT NULL DEFAULT FALSE,
    email_ok      BOOLEAN NOT NULL DEFAULT FALSE,
    email_code    TEXT,
    email_code_at TIMESTAMPTZ,
    wrong         INTEGER NOT NULL DEFAULT 0,
    next_url      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    wiped_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS pending_signups_created ON pending_signups (created_at);
"""

_WIPE = """args=NULL, form=NULL, pw_hash=NULL, phone=NULL, email=NULL,
           email_code=NULL, next_url=NULL, wiped_at=NOW()"""


def _q(sql, params=(), one=False, fetch=True):
    conn = get_db_connection()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(sql, params)
        if not fetch:
            return None
        return cur.fetchone() if one else cur.fetchall()
    finally:
        cur.close(); conn.close()


def wipe_expired():
    _q(f"""UPDATE pending_signups SET {_WIPE}
           WHERE finished_at IS NULL AND wiped_at IS NULL
             AND created_at < NOW() - INTERVAL '{HOLD_MINUTES} minutes'""", fetch=False)


def start(*, kind, flow, phone, email, creator=None, args=None, form=None, pw_hash=None,
          customer_id=None, wa_ok=False, email_ok=False, next_url=None) -> str:
    """Hold a sign-up (or a login that needs confirming). Returns its token."""
    wipe_expired()
    token = secrets.token_urlsafe(24)
    _q("""INSERT INTO pending_signups
              (token, kind, flow, creator, args, form, pw_hash, phone, email,
               customer_id, wa_ok, email_ok, next_url)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
       (token, kind, flow, creator, json.dumps(args) if args is not None else None,
        json.dumps(form) if form is not None else None, pw_hash, phone, email,
        customer_id, wa_ok, email_ok, next_url), fetch=False)
    return token


def get(token):
    """The held sign-up, or None once it's finished, wiped or past 60 minutes."""
    if not token:
        return None
    return _q(f"""SELECT * FROM pending_signups
                  WHERE token=%s AND finished_at IS NULL AND wiped_at IS NULL
                    AND created_at > NOW() - INTERVAL '{HOLD_MINUTES} minutes'""",
              (token,), one=True)


def new_email_code(pid) -> str:
    code = f"{random.SystemRandom().randint(0, 999999):06d}"
    _q("UPDATE pending_signups SET email_code=%s, email_code_at=NOW() WHERE id=%s", (code, pid), fetch=False)
    return code


def email_code_wait(row) -> bool:
    """True while the last email code is under 60 seconds old."""
    r = _q("SELECT email_code_at > NOW() - INTERVAL '60 seconds' AS wait FROM pending_signups WHERE id=%s",
           (row["id"],), one=True)
    return bool(r and r["wait"])


def email_code_right(pid, code) -> bool:
    r = _q(f"""SELECT 1 FROM pending_signups WHERE id=%s AND email_code=%s
               AND email_code_at > NOW() - INTERVAL '{CODE_MINUTES} minutes'""", (pid, code), one=True)
    return bool(r)


def set_phone(pid, phone):
    """Existing account typed a different WhatsApp number on the confirm page."""
    _q("UPDATE pending_signups SET phone=%s WHERE id=%s", (phone, pid), fetch=False)


def mark(pid, field):
    assert field in ("wa_ok", "email_ok")
    _q(f"UPDATE pending_signups SET {field}=TRUE WHERE id=%s", (pid,), fetch=False)


def wrong_code(pid) -> bool:
    """Count a wrong code. True when that was one too many (sign-up wiped)."""
    r = _q("UPDATE pending_signups SET wrong=wrong+1 WHERE id=%s RETURNING wrong", (pid,), one=True)
    if r and r["wrong"] >= MAX_WRONG:
        drop(pid)
        return True
    return False


def finish(pid):
    """Account created (or login confirmed): keep only the bare row for the counts."""
    _q(f"UPDATE pending_signups SET {_WIPE}, finished_at=NOW() WHERE id=%s", (pid,), fetch=False)


def drop(pid, changed=False):
    """Given up (too many wrong codes), or changed=True when they went back to
    fix their number or email — that sign-up continues as a new row, so this
    one is left out of the counts."""
    _q(f"UPDATE pending_signups SET {_WIPE}" + (", flow='changed'" if changed else "") + " WHERE id=%s",
       (pid,), fetch=False)


def week_counts() -> dict:
    """Sign-ups in the last 7 days: started, finished, didn't finish. Rows left
    behind by "Change it" don't count — that person carries on in a new row."""
    r = _q(f"""SELECT COUNT(*) AS started,
                      COUNT(*) FILTER (WHERE finished_at IS NOT NULL) AS finished,
                      COUNT(*) FILTER (WHERE finished_at IS NULL
                                       AND (wiped_at IS NOT NULL
                                            OR created_at < NOW() - INTERVAL '{HOLD_MINUTES} minutes')) AS not_finished
               FROM pending_signups
               WHERE kind='signup' AND created_at > NOW() - INTERVAL '7 days'
                 AND COALESCE(flow,'') <> 'changed'""", one=True) or {}
    return {k: int(r.get(k) or 0) for k in ("started", "finished", "not_finished")}
