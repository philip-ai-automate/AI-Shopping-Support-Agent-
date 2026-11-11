"""
turned_away.py — a record of every time the portal refuses something a
business types (11 Nov 2026, approved mock-up
https://claude.ai/artifact/MV8cnR2wMvLeGwUXiNSKcA).

  - kind 'phone'  : a phone number refused by _phone_entry on any portal page
  - kind 'signup' : a sign-up refused (email already used, missing field…)
  - kind 'login'  : a login refused (wrong password, disabled, paused).
                    The password itself is NEVER recorded.

Shown to Admin only (Admin › Customers › Detail, "Turned away" box) and kept
for 90 days. The business never sees it. Recording must never break the page
it's called from, so every error here is swallowed.

No Flask in here. The table was created once by hand (never DDL at request
time — see the accounts_core incident of 2026-10-10).
"""
import psycopg2.extras

from db import get_db_connection

KEEP_DAYS = 90

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS turned_away (
    id           SERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind         TEXT NOT NULL,          -- phone | signup | login
    tenant_id    INTEGER,
    customer_id  INTEGER,
    email        TEXT,
    page         TEXT,                   -- path of the page the form was on
    field        TEXT,                   -- form box name (phone only)
    typed        TEXT,                   -- what was typed (never a password)
    country      TEXT,                   -- country chosen next to the number
    message      TEXT                    -- what the person was shown
);
CREATE INDEX IF NOT EXISTS turned_away_customer ON turned_away (customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS turned_away_tenant   ON turned_away (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS turned_away_email    ON turned_away (LOWER(email), created_at DESC);
CREATE INDEX IF NOT EXISTS turned_away_created  ON turned_away (created_at);
"""

# Short label shown as a red pill in Admin, worked out from the message.
_LABELS = [
    ("no country code", "No country code"),
    ("premium-rate", "Special or personal line"),
    ("doesn't look like", "Not a real number"),
    ("is not a phone number", "Not a number"),
    ("already exists", "Already has an account"),
    ("already has a phixtra account", "Already has an account"),
    ("already used", "Number already used"),
    ("already has an account", "Already has an account"),
    ("incorrect email or password", "Wrong email or password"),
    ("disabled", "Account disabled"),
    ("too many", "Too many attempts"),
    ("robot", "Robot check"),
    ("recaptcha", "Robot check"),
    ("required", "Missing details"),
    ("please choose", "Missing details"),
    ("please tell us", "Missing details"),
    ("please enter", "Missing details"),
    ("password", "Password not accepted"),
]


_FIELD_NAMES = {"report_phone": "Alerts number", "alert_phone": "Alert number",
                "phone_number": "Their own number", "phone": "Phone number",
                "whatsapp_number": "WhatsApp number", "customer_phone": "Customer's number"}


def page_name(page: str, field: str = None) -> str:
    """'/whatsapp' + 'report_phone' → 'whatsapp › Alerts number'."""
    p = (page or "").strip("/").replace("-", " ").replace("_", " ").replace("/", " › ") or "—"
    p = (p[:1].upper() + p[1:]).replace("Whatsapp", "WhatsApp").replace("whatsapp", "WhatsApp")
    f = _FIELD_NAMES.get(field or "", (field or "").replace("_", " "))
    return f"{p} › {f}" if f else p


def label(message: str) -> str:
    m = (message or "").lower()
    for key, lbl in _LABELS:
        if key in m:
            return lbl
    return "Refused"


def record(kind, message, *, tenant_id=None, customer_id=None, email=None,
           page=None, field=None, typed=None, country=None):
    """Save one refusal. Never raises."""
    try:
        conn = get_db_connection(); conn.autocommit = True
        cur = conn.cursor()
        try:
            if customer_id and not tenant_id:
                cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (int(customer_id),))
                row = cur.fetchone()
                tenant_id = row[0] if row else None
            # Link sign-up / login refusals to the account when the email matches.
            if not customer_id and email:
                cur.execute("""SELECT id, tenant_id FROM customers WHERE LOWER(email)=LOWER(%s)
                               ORDER BY id LIMIT 1""", (email.strip(),))
                row = cur.fetchone()
                if row:
                    customer_id, tenant_id = row[0], tenant_id or row[1]
            cur.execute("""INSERT INTO turned_away
                             (kind, tenant_id, customer_id, email, page, field, typed, country, message)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (kind, tenant_id, customer_id, (email or None) and email.strip()[:200],
                         (page or "")[:200] or None, (field or "")[:80] or None,
                         (typed or "")[:200] or None, (country or "")[:8] or None,
                         (message or "")[:600] or None))
            # Keep 90 days (cheap: uses the created_at index).
            cur.execute(f"DELETE FROM turned_away WHERE created_at < NOW() - INTERVAL '{KEEP_DAYS} days'")
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ turned_away.record error:", e)


def for_customer(customer_id: int, tenant_id: int = None, email: str = None, limit: int = 100):
    """Rows for Admin › Customer Detail, newest first: the customer's own rows,
    their business's rows (team members), and sign-up/login rows with their email."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(f"""
                SELECT * FROM turned_away
                WHERE created_at >= NOW() - INTERVAL '{KEEP_DAYS} days'
                  AND (customer_id = %s
                       OR (%s::int IS NOT NULL AND tenant_id = %s)
                       OR (%s::text IS NOT NULL AND LOWER(email) = LOWER(%s)))
                ORDER BY created_at DESC LIMIT %s""",
                (customer_id, tenant_id, tenant_id, email, email, limit))
            rows = cur.fetchall()
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ turned_away.for_customer error:", e)
        return []
    for r in rows:
        r["label"] = label(r.get("message"))
        r["page_name"] = page_name(r.get("page"), r.get("field"))
    return rows
