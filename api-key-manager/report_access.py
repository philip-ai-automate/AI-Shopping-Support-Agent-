"""Sharing for saved reports and dashboards (Reports & Dashboards, phase R4 — 19 Nov 2026).

Approved mock-up EPpwZTeFt5iccD8SH155P1 screen 3 (Share panel):
  Who can open it      — only me / chosen team members or roles / everyone in the business
  What numbers they see — follow each person's access (default: a rep who only sees
                          their own deals sees only their own numbers) / everyone sees
                          the whole business
  Others can edit      — tick box

"Everyone sees the whole business" can only be chosen by someone who sees all
leads themselves, and it only widens what the SAVED report shows (opened as
saved, on a dashboard box, its drill-down and its export are all rebuilt on the
server from the saved settings). Anything a viewer changes by hand in the
builder runs with their own access again.
"""
from db import get_db_connection

VISIBILITY = [("private", "Only me"), ("chosen", "Chosen team members or roles"), ("everyone", "Everyone in the business")]
VIS_KEYS = {k for k, _ in VISIBILITY}
DATA_SCOPE = [("own", "Follow each person's access"), ("all", "Everyone sees the whole business")]
MAX_FOLDER = 60

_cols_ok = False


def ensure_columns():
    """Adds the sharing columns once (lock limited; the tables are new and small)."""
    global _cols_ok
    if _cols_ok:
        return
    import report_dashboards as RD
    RD.ensure_tables()
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                        WHERE table_name IN ('report_defs','report_dashboards') AND column_name='visibility'""")
        if cur.fetchone()[0] < 2:
            cur.execute("SET lock_timeout = '3s'")
            for t in ("report_defs", "report_dashboards"):
                cur.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS visibility VARCHAR(10) NOT NULL DEFAULT 'everyone'")
                cur.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS share_with JSONB NOT NULL DEFAULT '[]'::jsonb")
                cur.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS data_scope VARCHAR(5) NOT NULL DEFAULT 'own'")
                cur.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS others_edit BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("ALTER TABLE report_defs ADD COLUMN IF NOT EXISTS folder VARCHAR(60)")
            conn.commit()
        _cols_ok = True
    except Exception as e:
        conn.rollback()
        print("⚠️ report sharing columns:", e)
    finally:
        cur.close()
        conn.close()


def viewer_of(actor: dict, see_all: bool) -> dict:
    """Who is looking: key, role, and whether they see all leads themselves."""
    role = None
    if actor.get("is_team") and actor.get("team_member_id"):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT role_id FROM team_members WHERE id=%s", (actor["team_member_id"],))
            r = cur.fetchone()
            role = r[0] if r else None
        finally:
            cur.close()
            conn.close()
    return {"key": actor["key"], "role": f"role:{role}" if role else None, "see_all": bool(see_all),
            "is_owner_login": not actor.get("is_team")}


def is_mine(row, v) -> bool:
    return bool(row.get("created_by_key")) and row["created_by_key"] == v["key"]


def can_view(row, v) -> bool:
    if row is None:
        return False
    if is_mine(row, v):
        return True
    vis = row.get("visibility") or "everyone"
    if vis == "everyone":
        return True
    if vis == "chosen":
        share = row.get("share_with") or []
        return v["key"] in share or (v["role"] is not None and v["role"] in share)
    return False


def can_edit(row, v) -> bool:
    return is_mine(row, v) or (bool(row.get("others_edit")) and can_view(row, v))


def can_manage(row, v) -> bool:
    """Change sharing, delete. A record saved before R4 with no owner: the business owner login."""
    if row.get("created_by_key"):
        return is_mine(row, v)
    return v["is_owner_login"]


def whole_business(row) -> bool:
    return (row or {}).get("data_scope") == "all"


def clean_sharing(form, v, people_keys: set, role_keys: set) -> dict:
    vis = form.get("visibility") if form.get("visibility") in VIS_KEYS else "everyone"
    share = []
    if vis == "chosen":
        for k in form.getlist("share_with")[:200]:
            if k in people_keys or k in role_keys:
                share.append(k)
        if not share:
            vis = "private"
    scope = "all" if form.get("data_scope") == "all" and v["see_all"] else "own"
    return {"visibility": vis, "share_with": share, "data_scope": scope,
            "others_edit": form.get("others_edit") == "1" and vis != "private"}


def roles(tenant_id: int) -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name FROM tenant_roles WHERE tenant_id=%s ORDER BY name", (tenant_id,))
        return [(f"role:{i}", n) for i, n in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def sharing_label(row) -> str:
    vis = row.get("visibility") or "everyone"
    if vis == "private":
        return "Only the owner"
    if vis == "chosen":
        n = len(row.get("share_with") or [])
        return f"{n} chosen" if n != 1 else "1 chosen"
    return "Everyone"
