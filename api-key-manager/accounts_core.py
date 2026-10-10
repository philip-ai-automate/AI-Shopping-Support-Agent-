"""
accounts_core.py — "Accounts": businesses that signed up to the portal and
haven't finished setting up, handed to PhiXtra's own staff to help over the
line (2026-10-10, approved mock-up; named "Accounts" by the user).

  - Admin › Customers assigns a portal/Connect business to a staff member of
    the PLATFORM business (the business account PhiXtra's own team logs into).
    Which business that is comes from one setting, PLATFORM_TENANT_ID in .env
    — never a number written into the code.
  - That staff member sees it under "Accounts" in their normal portal login,
    with the setup step it stopped at, contact details, and notes.
  - Setup steps tick themselves from real data. Website and "WhatsApp and
    website" sign-ups use the same steps as their Dashboard "Get set up" box;
    WhatsApp sign-ups: Account → Connect WhatsApp → Products → AI trial
    (approved 2026-10-10). Older website-plugin sign-ups: Account → Products
    → AI trial.
  - When every step is done the account moves to "Onboarded" by itself.

No Flask in here, so the admin pages and the staff pages share it.
"""
import os

import psycopg2.extras

from db import get_db_connection

# Same free plans as portal_routes.AI_OFF_FREE_PLAN_SLUGS (kept in step by hand;
# importing portal_routes here would be circular for the admin blueprint).
FREE_PLAN_SLUGS = ("connect", "web_free")

_tables_ready = False


def platform_tenant_id():
    raw = (os.environ.get("PLATFORM_TENANT_ID") or "").strip()
    return int(raw) if raw.isdigit() else None


def is_platform_tenant(tenant_id) -> bool:
    pid = platform_tenant_id()
    return pid is not None and tenant_id is not None and int(tenant_id) == pid


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_tables():
    """Created on first use (kept out of ensure_portal_tables so a problem
    here can never stop the other migrations running)."""
    global _tables_ready
    if _tables_ready:
        return
    conn, cur = _db()
    try:
        cur.execute("SELECT to_regclass('platform_account_notes') IS NOT NULL")
        if cur.fetchone()["?column?"]:
            cur.execute("""SELECT 1 FROM information_schema.columns
                            WHERE table_name='customers' AND column_name='last_login_at'""")
            if cur.fetchone():
                _tables_ready = True
                return
        cur.execute("""
            CREATE TABLE IF NOT EXISTS platform_account_assignments (
                tenant_id       INTEGER PRIMARY KEY,
                team_member_id  INTEGER NOT NULL,
                assigned_by     TEXT,
                assigned_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                onboarded_at    TIMESTAMPTZ
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS platform_account_notes (
                id              BIGSERIAL PRIMARY KEY,
                tenant_id       INTEGER NOT NULL,
                author_key      TEXT,
                author_label    TEXT,
                note            TEXT NOT NULL,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS platform_account_notes_t_idx ON platform_account_notes (tenant_id, created_at DESC)")
        # Only ALTER when the column is really missing: even "IF NOT EXISTS" takes
        # a full table lock and queues every customers read behind any open
        # transaction (froze staff page loads for ~2 min on 2026-10-10).
        cur.execute("""SELECT 1 FROM information_schema.columns
                        WHERE table_name='customers' AND column_name='last_login_at'""")
        if not cur.fetchone():
            cur.execute("SET LOCAL lock_timeout = '3s'")
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ")
        conn.commit()
        _tables_ready = True
    finally:
        cur.close(); conn.close()


# ── staff ─────────────────────────────────────────────────────────────────

def platform_staff() -> list:
    """Active staff of the platform business — who an account can go to."""
    pid = platform_tenant_id()
    if pid is None:
        return []
    conn, cur = _db()
    try:
        cur.execute("""
            SELECT id, COALESCE(NULLIF(TRIM(CONCAT(first_name, ' ', last_name)), ''), name, email) AS name
              FROM team_members
             WHERE tenant_id=%s AND is_active
             ORDER BY 2""", (pid,))
        return cur.fetchall() or []
    finally:
        cur.close(); conn.close()


def assignments_for(tenant_ids) -> dict:
    """tenant_id -> {team_member_id, staff_name, assigned_at, onboarded_at}."""
    ensure_tables()
    ids = [int(t) for t in tenant_ids or []]
    if not ids:
        return {}
    conn, cur = _db()
    try:
        cur.execute("""
            SELECT a.tenant_id, a.team_member_id, a.assigned_at, a.onboarded_at,
                   COALESCE(NULLIF(TRIM(CONCAT(tm.first_name, ' ', tm.last_name)), ''), tm.name) AS staff_name
              FROM platform_account_assignments a
              LEFT JOIN team_members tm ON tm.id = a.team_member_id
             WHERE a.tenant_id = ANY(%s)""", (ids,))
        return {r["tenant_id"]: r for r in cur.fetchall() or []}
    finally:
        cur.close(); conn.close()


def assign(tenant_id: int, team_member_id, by_label: str):
    """Assign (or with None, unassign) a business. Returns the staff name or None.
    Raises ValueError if the staff member isn't active in the platform business."""
    ensure_tables()
    conn, cur = _db()
    try:
        if team_member_id is None:
            cur.execute("DELETE FROM platform_account_assignments WHERE tenant_id=%s", (tenant_id,))
            _note(cur, tenant_id, None, by_label, "Unassigned")
            conn.commit()
            return None
        pid = platform_tenant_id()
        cur.execute("""SELECT id, COALESCE(NULLIF(TRIM(CONCAT(first_name, ' ', last_name)), ''), name) AS name
                         FROM team_members WHERE id=%s AND tenant_id=%s AND is_active""",
                    (int(team_member_id), pid))
        tm = cur.fetchone()
        if not tm:
            raise ValueError("That staff member isn't available.")
        cur.execute("""
            INSERT INTO platform_account_assignments (tenant_id, team_member_id, assigned_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE
               SET team_member_id = EXCLUDED.team_member_id, assigned_by = EXCLUDED.assigned_by,
                   assigned_at = NOW()""", (tenant_id, tm["id"], by_label))
        _note(cur, tenant_id, None, by_label, f"Assigned to {tm['name']}")
        conn.commit()
        return tm["name"]
    finally:
        cur.close(); conn.close()


def _note(cur, tenant_id, author_key, author_label, text):
    cur.execute("""INSERT INTO platform_account_notes (tenant_id, author_key, author_label, note)
                   VALUES (%s, %s, %s, %s)""", (tenant_id, author_key, author_label, text))


def add_note(tenant_id: int, author_key: str, author_label: str, text: str):
    ensure_tables()
    conn, cur = _db()
    try:
        _note(cur, tenant_id, author_key, author_label, text[:2000])
        conn.commit()
    finally:
        cur.close(); conn.close()


def notes_for(tenant_id: int) -> list:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT author_label, note, created_at, author_key FROM platform_account_notes
                        WHERE tenant_id=%s ORDER BY created_at DESC, id DESC LIMIT 200""", (tenant_id,))
        return cur.fetchall() or []
    finally:
        cur.close(); conn.close()


def last_notes(tenant_ids) -> dict:
    """tenant_id -> latest note written by a person (not the assign/unassign lines)."""
    ids = [int(t) for t in tenant_ids or []]
    if not ids:
        return {}
    conn, cur = _db()
    try:
        cur.execute("""SELECT DISTINCT ON (tenant_id) tenant_id, note, created_at, author_label
                         FROM platform_account_notes
                        WHERE tenant_id = ANY(%s) AND author_key IS NOT NULL
                        ORDER BY tenant_id, created_at DESC, id DESC""", (ids,))
        return {r["tenant_id"]: r for r in cur.fetchall() or []}
    finally:
        cur.close(); conn.close()


# ── setup steps ───────────────────────────────────────────────────────────

def signup_type(t: dict) -> str:
    ch = t.get("signup_channel")
    if ch == "both":
        return "WhatsApp and website"
    if ch == "website":
        return "Website"
    if (t.get("source_type") or "") == "web":
        return "Website plugin"
    return "WhatsApp"


def setup_steps_many(tenant_ids) -> dict:
    """tenant_id -> {"type", "steps": [{"title", "done"}], "done", "total",
    "complete", "stuck"} from real data. Same facts as the Dashboard
    "Get set up" box (portal_routes._setup_checklist) for website sign-ups."""
    ids = [int(t) for t in tenant_ids or []]
    if not ids:
        return {}
    conn, cur = _db()
    try:
        cur.execute("""SELECT t.id, t.signup_channel, t.source_type, t.website_platform, t.trial_granted_at,
                              t.chatbox_seen_at, p.slug AS plan_slug
                         FROM tenants t LEFT JOIN plans p ON p.id = t.plan_id
                        WHERE t.id = ANY(%s)""", (ids,))
        tenants = {r["id"]: r for r in cur.fetchall() or []}
        cur.execute("SELECT DISTINCT tenant_id FROM wa_tenants WHERE tenant_id = ANY(%s) AND active", (ids,))
        wa_on = {r["tenant_id"] for r in cur.fetchall() or []}
        cur.execute("SELECT tenant_id, last_read_at FROM website_sources WHERE tenant_id = ANY(%s)", (ids,))
        site_read = {r["tenant_id"] for r in cur.fetchall() or [] if r["last_read_at"]}
        cur.execute("""SELECT tenant_id, COUNT(*) AS n FROM documents
                        WHERE tenant_id = ANY(%s) AND type='product' GROUP BY tenant_id""", (ids,))
        products = {r["tenant_id"]: int(r["n"]) for r in cur.fetchall() or []}
        cur.execute("SELECT tenant_id, COUNT(*) AS n FROM data_sources WHERE tenant_id = ANY(%s) GROUP BY tenant_id", (ids,))
        lists = {r["tenant_id"]: int(r["n"]) for r in cur.fetchall() or []}
    finally:
        cur.close(); conn.close()

    out = {}
    for tid, t in tenants.items():
        has_products = products.get(tid, 0) > 0 or lists.get(tid, 0) > 0
        trial = bool(t["trial_granted_at"]) or (t["plan_slug"] not in FREE_PLAN_SLUGS and t["plan_slug"] is not None)
        kind = signup_type(t)
        if t["signup_channel"] in ("website", "both"):
            steps = []
            if t["signup_channel"] == "both":
                steps.append(("Connect WhatsApp", tid in wa_on))
            wp = t["website_platform"] == "wordpress"
            steps += [("Connect website", tid in site_read),
                      ("Add chat box", bool(t["chatbox_seen_at"])),
                      ("Products", products.get(tid, 0) > 0 if wp else has_products),
                      ("AI trial", trial)]
        elif kind == "Website plugin":
            steps = [("Account", True), ("Products", has_products), ("AI trial", trial)]
        else:
            steps = [("Account", True), ("Connect WhatsApp", tid in wa_on), ("Products", has_products), ("AI trial", trial)]
        done = sum(1 for _, d in steps if d)
        stuck = next((title for title, d in steps if not d), None)
        out[tid] = {"type": kind, "steps": [{"title": s, "done": d} for s, d in steps],
                    "done": done, "total": len(steps), "complete": done == len(steps), "stuck": stuck}
    return out


def mark_onboarded(tenant_ids_complete):
    """Stamp onboarded_at the first time an assigned account's steps are all done."""
    ids = [int(t) for t in tenant_ids_complete or []]
    if not ids:
        return
    conn, cur = _db()
    try:
        cur.execute("""UPDATE platform_account_assignments SET onboarded_at = NOW()
                        WHERE tenant_id = ANY(%s) AND onboarded_at IS NULL""", (ids,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def record_owner_login(customer_id: int):
    """Called at owner log-in, so Accounts can show "last login"."""
    try:
        ensure_tables()
        conn, cur = _db()
        try:
            cur.execute("UPDATE customers SET last_login_at = NOW() WHERE id=%s", (int(customer_id),))
            conn.commit()
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ record_owner_login:", e)
