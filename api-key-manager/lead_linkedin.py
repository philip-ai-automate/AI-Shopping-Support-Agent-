"""LinkedIn on the lead page (user 2026-10-21).

The sales team connects with a lead on LinkedIn first, then emails once the
person accepts. The lead page shows an "Open LinkedIn" button and one-click
steps (Request sent / Accepted / Messaged / Not accepted), each saved with
the date and who clicked, so the CRM keeps up without typing notes.

The profile link is the one saved here, else the first linkedin.com/in/ link
in the lead's notes (Lead Finder writes "LinkedIn: <link>" there). Tables
are new and small; nothing here ALTERs merchant_pipeline_leads.
"""
import re

import psycopg2.extras

from db import get_db_connection

STEPS = [
    ("request_sent", "Request sent"),
    ("accepted", "Accepted"),
    ("messaged", "Messaged"),
    ("not_accepted", "Not accepted"),
]
STEP_LABELS = dict(STEPS)

_PROFILE_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[^\s<>\"')]+", re.I)
_tables_ready = False


def ensure_tables():
    """Created on first use; DDL only when a table is really missing."""
    global _tables_ready
    if _tables_ready:
        return
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('lead_linkedin') IS NOT NULL AND to_regclass('lead_linkedin_steps') IS NOT NULL")
        if not cur.fetchone()[0]:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS lead_linkedin (
                               lead_id     INTEGER PRIMARY KEY,
                               tenant_id   INTEGER NOT NULL,
                               profile_url TEXT,
                               updated_by  VARCHAR(200),
                               updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS lead_linkedin_steps (
                               id          BIGSERIAL PRIMARY KEY,
                               tenant_id   INTEGER NOT NULL,
                               lead_id     INTEGER NOT NULL,
                               step        VARCHAR(20) NOT NULL,
                               actor_key   VARCHAR(40),
                               actor_name  VARCHAR(200),
                               created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS lead_linkedin_steps_lead ON lead_linkedin_steps (tenant_id, lead_id, created_at)")
        conn.commit()
        _tables_ready = True
    finally:
        conn.close()


def clean_url(raw: str) -> str:
    """'linkedin.com/in/jane' → 'https://www.linkedin.com/in/jane'; '' when it isn't a LinkedIn link."""
    s = (raw or "").strip()
    if not s:
        return ""
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    m = re.match(r"^https?://([^/?#]+)(.*)$", s, re.I)
    if not m:
        return ""
    host = m.group(1).lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    if host == "linkedin.com":
        host = "www.linkedin.com"
    return ("https://" + host + m.group(2))[:500]


def link_from_notes(notes: str) -> str:
    m = _PROFILE_RE.search(notes or "")
    return clean_url(m.group(0).rstrip(".,;")) if m else ""


def lead_view(tenant_id: int, lead: dict, tz) -> dict:
    """What the LinkedIn strip on the lead page needs."""
    ensure_tables()
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT profile_url FROM lead_linkedin WHERE lead_id=%s AND tenant_id=%s", (lead["id"], tenant_id))
        row = cur.fetchone()
        cur.execute("""SELECT id, step, actor_key, actor_name, created_at FROM lead_linkedin_steps
                       WHERE tenant_id=%s AND lead_id=%s ORDER BY created_at DESC, id DESC LIMIT 50""",
                    (tenant_id, lead["id"]))
        steps = cur.fetchall()
    finally:
        conn.close()
    saved = row is not None
    url = (row["profile_url"] or "") if saved else link_from_notes(lead.get("notes"))
    for s in steps:
        s["label"] = STEP_LABELS.get(s["step"], s["step"])
        s["local_at"] = s["created_at"].astimezone(tz)
    return {"url": url, "from_notes": bool(url) and not saved, "steps": steps,
            "current": steps[0] if steps else None, "choices": STEPS}


def save_url(tenant_id: int, lead_id: int, url: str, actor_name: str) -> None:
    """An empty url is saved too, so a wrong link found in the notes can be removed."""
    ensure_tables()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO lead_linkedin (lead_id, tenant_id, profile_url, updated_by)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (lead_id) DO UPDATE
                         SET profile_url=EXCLUDED.profile_url, updated_by=EXCLUDED.updated_by, updated_at=NOW()
                       WHERE lead_linkedin.tenant_id=EXCLUDED.tenant_id""",
                    (lead_id, tenant_id, url or None, actor_name))
        conn.commit()
    finally:
        conn.close()


def add_step(tenant_id: int, lead_id: int, step: str, actor: dict) -> None:
    import staff_activity
    name = staff_activity.current_name(actor["label"]) or actor["label"]
    ensure_tables()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO lead_linkedin_steps (tenant_id, lead_id, step, actor_key, actor_name)
                       VALUES (%s,%s,%s,%s,%s)""", (tenant_id, lead_id, step, actor["key"], name))
        cur.execute("UPDATE merchant_pipeline_leads SET updated_at=NOW() WHERE id=%s AND tenant_id=%s",
                    (lead_id, tenant_id))
        conn.commit()
    finally:
        conn.close()


def undo_step(tenant_id: int, lead_id: int, step_id: int, actor_key: str):
    """Remove the newest step, only by the person who clicked it. Returns its label, or None."""
    ensure_tables()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""DELETE FROM lead_linkedin_steps
                       WHERE id=%s AND tenant_id=%s AND lead_id=%s AND actor_key=%s
                         AND id = (SELECT id FROM lead_linkedin_steps WHERE tenant_id=%s AND lead_id=%s
                                   ORDER BY created_at DESC, id DESC LIMIT 1)
                       RETURNING step""", (step_id, tenant_id, lead_id, actor_key, tenant_id, lead_id))
        r = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    return STEP_LABELS.get(r[0], r[0]) if r else None


# ── Leads filter: "LinkedIn" box (2026-10-21) ──────────────────────────────
# A lead's LinkedIn status is its newest step. "Waiting 7+ days" = the newest
# step is still Request sent, clicked 7 or more days ago.
FILTER_OPTIONS = [("request_sent", "Request sent"), ("accepted", "Accepted"), ("messaged", "Messaged"),
                  ("not_accepted", "Not accepted"), ("waiting7", "Waiting 7+ days, not accepted"),
                  ("none", "No LinkedIn step yet")]
WAIT_DAYS = 7


def _latest(alias, col):
    return (f"(SELECT s.{col} FROM lead_linkedin_steps s WHERE s.tenant_id={alias}.tenant_id "
            f"AND s.lead_id={alias}.id ORDER BY s.created_at DESC, s.id DESC LIMIT 1)")


def _state_sql(step_sql, at_sql):
    out = {k: f"{step_sql}='{k}'" for k, _ in STEPS}
    out["waiting7"] = f"({step_sql}='request_sent' AND {at_sql} <= NOW() - interval '{WAIT_DAYS} days')"
    out["none"] = f"{step_sql} IS NULL"
    return out


def filter_clause(alias: str, picked: list):
    """SQL for the Filters panel's "LinkedIn" boxes (any of them)."""
    ensure_tables()
    exprs = _state_sql(_latest(alias, "step"), _latest(alias, "created_at"))
    ors = [exprs[v] for v in picked if v in exprs]
    return ("(" + " OR ".join(ors) + ")") if ors else None


def option_counts(cur, where_sql: str, params: list) -> dict:
    """How many leads each "LinkedIn" box has, over the page's own leads.
    'any' = whether this business has ever clicked a LinkedIn step (the
    section only shows then)."""
    ensure_tables()
    e = _state_sql("ls.step", "ls.created_at")
    keys = [k for k, _ in FILTER_OPTIONS]
    c = cur.connection.cursor()
    try:
        c.execute(f"""SELECT {', '.join(f'count(*) FILTER (WHERE {e[k]})' for k in keys)}
                      FROM merchant_pipeline_leads mpl
                      LEFT JOIN LATERAL (SELECT s.step, s.created_at FROM lead_linkedin_steps s
                                          WHERE s.tenant_id=mpl.tenant_id AND s.lead_id=mpl.id
                                          ORDER BY s.created_at DESC, s.id DESC LIMIT 1) ls ON TRUE
                      WHERE {where_sql}""", list(params))
        out = dict(zip(keys, c.fetchone()))
        c.execute("SELECT EXISTS (SELECT 1 FROM lead_linkedin_steps WHERE tenant_id=%s)", [params[0]])
        out["any"] = c.fetchone()[0]
    finally:
        c.close()
    return out
