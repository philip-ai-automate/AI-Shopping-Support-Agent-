"""Next follow-up date on every lead (user 2026-10-23).

The Team performance report showed leads nobody followed up, and the CRM
had no reminders: nothing saved *when* a lead should be contacted next.

Each open lead has at most one open follow-up (a date). Whenever someone
contacts the lead personally (call, WhatsApp / Messenger / web chat reply,
LinkedIn step, "They replied") the open one is marked done and a new one is
opened. On the lead page the date is required (a calendar); anywhere else
(e.g. a reply from the Inbox) it is set 3 working days ahead automatically,
so no open lead is left without a date. Closing a lead (Won / Lost /
Not a Fit) cancels it.

The follow-up belongs to whoever the lead is assigned to now, so moving a
lead to someone else moves its follow-ups too. New table only; nothing here
ALTERs merchant_pipeline_leads.
"""
import datetime as _dt

import psycopg2.extras

from db import get_db_connection

AUTO_WORKING_DAYS = 3
MAX_DAYS_AHEAD = 366
# Contact that answers a follow-up (same as "Contacted personally" in Team
# performance, plus every LinkedIn step the person clicks).
CONTACT_ACTIONS = ("call", "wa_sent", "messenger_sent", "webchat_sent", "reply_logged", "linkedin",
                   "wa_logged", "email_logged", "meeting_logged")
CLOSE_ACTIONS = ("won", "lost", "not_a_fit")
SOURCE_WORDS = {"call": "Call", "wa_sent": "WhatsApp", "messenger_sent": "Messenger", "webchat_sent": "Web chat",
                "reply_logged": "Email reply", "linkedin": "LinkedIn", "demo": "Demo", "manual": "Date changed",
                "reopened": "Reopened", "demo_held": "Demo", "wa_logged": "WhatsApp", "email_logged": "Email",
                "meeting_logged": "Meeting"}
# "Follow up after …" wording on the lead card and the Follow-ups list
AFTER_WORDS = {"call": "the call", "wa_sent": "the WhatsApp message", "messenger_sent": "the Messenger reply",
               "webchat_sent": "the web chat reply", "reply_logged": "their email reply", "linkedin": "the LinkedIn step",
               "demo": "the demo", "demo_held": "the demo", "wa_logged": "the WhatsApp", "email_logged": "the email",
               "meeting_logged": "the meeting"}
# How the next follow-up will be done (redesign 2026-10-23). Optional; Call when not chosen.
CHANNELS = [("call", "Call"), ("whatsapp", "WhatsApp"), ("email", "Email"), ("linkedin", "LinkedIn"), ("meeting", "Meeting")]
CHANNEL_LABELS = dict(CHANNELS)
RESCHEDULED_OFTEN = 3
_tables_ready = False


def ensure_tables():
    """Created on first use; DDL only when the table is really missing."""
    global _tables_ready
    if _tables_ready:
        return
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('lead_followups') IS NOT NULL")
        if not cur.fetchone()[0]:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS lead_followups (
                               id            BIGSERIAL PRIMARY KEY,
                               tenant_id     INTEGER NOT NULL,
                               lead_id       INTEGER NOT NULL,
                               due_date      DATE NOT NULL,
                               note          TEXT,
                               source        VARCHAR(20),
                               auto          BOOLEAN NOT NULL DEFAULT FALSE,   -- set 3 working days ahead for them
                               set_by_key    VARCHAR(40),
                               set_by_name   VARCHAR(200),
                               created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                               updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                               done_at       TIMESTAMPTZ,
                               done_by_key   VARCHAR(40),
                               done_by_name  VARCHAR(200),
                               done_action   VARCHAR(40),
                               cancelled_at  TIMESTAMPTZ)""")
            cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS lead_followups_one_open
                           ON lead_followups (lead_id) WHERE done_at IS NULL AND cancelled_at IS NULL""")
            cur.execute("CREATE INDEX IF NOT EXISTS lead_followups_tenant_due ON lead_followups (tenant_id, due_date)")
        cur.execute("SELECT to_regclass('lead_contact_notes') IS NOT NULL")
        if not cur.fetchone()[0]:
            # What was said / what happened on each logged contact (user 2026-10-23:
            # required, so a colleague can carry on). New, small table.
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS lead_contact_notes (
                               id          BIGSERIAL PRIMARY KEY,
                               tenant_id   INTEGER NOT NULL,
                               lead_id     INTEGER NOT NULL,
                               kind        VARCHAR(12) NOT NULL,
                               result      VARCHAR(80),
                               note        TEXT NOT NULL,
                               actor_key   VARCHAR(40),
                               actor_name  VARCHAR(200),
                               created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS lead_contact_notes_lead ON lead_contact_notes (lead_id, created_at DESC)")
        cur.execute("""SELECT count(*) FROM information_schema.columns
                       WHERE table_name='lead_followups' AND column_name IN ('channel', 'reschedule_count')""")
        if cur.fetchone()[0] < 2:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("ALTER TABLE lead_followups ADD COLUMN IF NOT EXISTS channel VARCHAR(12)")
            cur.execute("ALTER TABLE lead_followups ADD COLUMN IF NOT EXISTS reschedule_count INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        _tables_ready = True
    finally:
        conn.close()


def _today(tenant_id: int) -> _dt.date:
    from team_performance import tz_for
    return _dt.datetime.now(tz_for(tenant_id)).date()


def add_working_days(d: _dt.date, n: int) -> _dt.date:
    while n > 0:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def parse_date(raw: str, tenant_id: int):
    """The calendar box → (date, None) or (None, why not)."""
    try:
        d = _dt.datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None, "Choose the next follow-up date on the calendar."
    today = _today(tenant_id)
    if d < today:
        return None, "The next follow-up date can't be in the past."
    if d > today + _dt.timedelta(days=MAX_DAYS_AHEAD):
        return None, "Choose a follow-up date within the next 12 months."
    return d, None


def _is_open_lead(cur, tenant_id: int, lead_id: int) -> bool:
    cur.execute("""SELECT 1 FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s
                     AND outcome IS NULL AND dropped_at IS NULL""", (lead_id, tenant_id))
    return cur.fetchone() is not None


def on_contact(cur, tenant_id: int, lead_id: int, action: str, actor_key: str, actor_name: str) -> None:
    """Called by staff_activity.log for each personal contact: the open
    follow-up is done, and the next one is opened 3 working days ahead.
    The lead page then replaces that date with the one the person chose."""
    ensure_tables()
    if not _is_open_lead(cur, tenant_id, lead_id):
        return
    cur.execute("""UPDATE lead_followups SET done_at=NOW(), done_by_key=%s, done_by_name=%s, done_action=%s
                   WHERE lead_id=%s AND tenant_id=%s AND done_at IS NULL AND cancelled_at IS NULL""",
                (actor_key, (actor_name or "")[:200] or None, action[:40], lead_id, tenant_id))
    due = add_working_days(_today(tenant_id), AUTO_WORKING_DAYS)
    cur.execute("""INSERT INTO lead_followups (tenant_id, lead_id, due_date, source, auto, set_by_key, set_by_name)
                   VALUES (%s,%s,%s,%s,TRUE,%s,%s) ON CONFLICT DO NOTHING""",
                (tenant_id, lead_id, due, action[:20], actor_key, (actor_name or "")[:200] or None))


def record_contact(tenant_id: int, lead_id: int, action: str, actor: dict, due=None,
                   source: str = None, note: str = None, channel: str = None) -> None:
    """Contact that isn't in staff_activity (a demo held): tick the open
    follow-up off, then save the chosen date (or keep 3 working days)."""
    import staff_activity
    name = staff_activity.current_name(actor["label"]) or actor["label"]
    conn = get_db_connection()
    try:
        conn.autocommit = True
        on_contact(conn.cursor(), tenant_id, lead_id, action, actor["key"], name)
    finally:
        conn.close()
    if due:
        set_next(tenant_id, lead_id, due, actor, source or action, note=note, channel=channel)


def on_close(cur, lead_id: int) -> None:
    """Won / Lost / Not a Fit: nothing left to follow up."""
    ensure_tables()
    cur.execute("""UPDATE lead_followups SET cancelled_at=NOW()
                   WHERE lead_id=%s AND done_at IS NULL AND cancelled_at IS NULL""", (lead_id,))


def set_next(tenant_id: int, lead_id: int, due: _dt.date, actor: dict, source: str,
             note: str = None, auto: bool = False, channel: str = None, reschedule: bool = False) -> None:
    """The date someone chose (or a default): replaces the open follow-up's
    date, or opens one. reschedule=True (the Reschedule box) counts a move
    when the date really changes, for "rescheduled often"."""
    channel = channel if channel in CHANNEL_LABELS else None
    import staff_activity
    name = staff_activity.current_name(actor["label"]) or actor["label"]
    ensure_tables()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if not _is_open_lead(cur, tenant_id, lead_id):
            conn.commit()
            return
        cur.execute("""UPDATE lead_followups SET reschedule_count = reschedule_count + (CASE WHEN %s AND due_date <> %s THEN 1 ELSE 0 END),
                              due_date=%s, note=COALESCE(%s, note), source=%s, auto=%s,
                              channel=COALESCE(%s, channel), set_by_key=%s, set_by_name=%s, updated_at=NOW()
                       WHERE lead_id=%s AND tenant_id=%s AND done_at IS NULL AND cancelled_at IS NULL""",
                    (reschedule, due, due, note or None, source[:20], auto, channel, actor["key"], name, lead_id, tenant_id))
        if not cur.rowcount:
            cur.execute("""INSERT INTO lead_followups (tenant_id, lead_id, due_date, note, source, auto, channel, set_by_key, set_by_name)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (tenant_id, lead_id, due, note or None, source[:20], auto, channel, actor["key"], name))
        conn.commit()
    finally:
        conn.close()


def _state(due: _dt.date, today: _dt.date) -> str:
    if due < today:
        return "overdue"
    return "today" if due == today else "later"


def _when_words(due: _dt.date, today: _dt.date) -> str:
    n = (due - today).days
    if n < 0:
        return f"{-n} day{'s' if n != -1 else ''} overdue"
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    return f"in {n} days"


def lead_view(tenant_id: int, lead_id: int, tz) -> dict:
    """What the follow-up strip and tab on the lead page need."""
    ensure_tables()
    today = _dt.datetime.now(tz).date()
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT * FROM lead_followups WHERE tenant_id=%s AND lead_id=%s
                       ORDER BY (done_at IS NULL AND cancelled_at IS NULL) DESC, created_at DESC, id DESC LIMIT 30""",
                    (tenant_id, lead_id))
        rows = cur.fetchall()
    finally:
        conn.close()
    current = None
    for r in rows:
        r["source_label"] = SOURCE_WORDS.get(r["source"] or "", "")
        r["done_label"] = SOURCE_WORDS.get(r["done_action"] or "", r["done_action"] or "")
        r["channel_label"] = CHANNEL_LABELS.get(r.get("channel") or "", "")
        r["after_words"] = AFTER_WORDS.get(r["source"] or "", "")
        r["updated_local"] = r["updated_at"].astimezone(tz)
        if r["done_at"]:
            done_day = r["done_at"].astimezone(tz).date()
            r["status"] = "Done on time" if done_day <= r["due_date"] else f"Done {(done_day - r['due_date']).days} day{'s' if (done_day - r['due_date']).days != 1 else ''} late"
            r["done_local"] = r["done_at"].astimezone(tz)
        elif r["cancelled_at"]:
            r["status"] = "Not needed, lead closed"
        else:
            r["status"] = "Open"
            r["state"] = _state(r["due_date"], today)
            r["when_words"] = _when_words(r["due_date"], today)
            current = r
    return {"current": current, "history": [r for r in rows if r is not current], "today": today,
            "min_date": today.isoformat(), "max_date": (today + _dt.timedelta(days=MAX_DAYS_AHEAD)).isoformat()}


def attach(cur, tenant_id: int, leads: list):
    """Gives each lead dict a `followup` entry for its list badge (None if none open)."""
    ensure_tables()
    ids = [l["id"] for l in leads if l.get("id")]
    for l in leads:
        l["followup"] = None
    if not ids:
        return
    today = _today(tenant_id)
    c = cur.connection.cursor()
    try:
        c.execute("""SELECT lead_id, due_date FROM lead_followups
                     WHERE tenant_id=%s AND lead_id = ANY(%s) AND done_at IS NULL AND cancelled_at IS NULL""",
                  (tenant_id, ids))
        rows = dict(c.fetchall())
    finally:
        c.close()
    for l in leads:
        d = rows.get(l.get("id"))
        if d:
            l["followup"] = {"state": _state(d, today), "when": d.strftime("%a %-d %b"), "words": _when_words(d, today)}


# ── Leads / Pipeline filter: "Follow-up" box ────────────────────────────────
FILTER_OPTIONS = [("overdue", "Overdue"), ("today", "Due today"), ("week", "Due in the next 7 days"),
                  ("later", "Due later"), ("none", "No follow-up date")]


def _today_sql(tenant_id: int):
    """Today in the business's own time zone, as SQL (the zone name is checked first)."""
    import re
    from staff_activity import tenant_tz_name
    name = tenant_tz_name(int(tenant_id))
    if not re.fullmatch(r"[A-Za-z0-9_/+-]{1,60}", name or ""):
        name = "UTC"
    return f"(NOW() AT TIME ZONE '{name}')::date"


def _state_sql(due_sql, today_sql):
    return {"overdue": f"{due_sql} < {today_sql}",
            "today": f"{due_sql} = {today_sql}",
            "week": f"({due_sql} > {today_sql} AND {due_sql} <= {today_sql} + 7)",
            "later": f"{due_sql} > {today_sql} + 7",
            "none": f"{due_sql} IS NULL"}


def _due_sql(alias):
    return (f"(SELECT f.due_date FROM lead_followups f WHERE f.lead_id={alias}.id AND f.tenant_id={alias}.tenant_id "
            f"AND f.done_at IS NULL AND f.cancelled_at IS NULL LIMIT 1)")


def filter_clause(alias: str, picked: list, tenant_id: int):
    ensure_tables()
    exprs = _state_sql(_due_sql(alias), _today_sql(tenant_id))
    ors = [exprs[v] for v in picked if v in exprs]
    return ("(" + " OR ".join(ors) + ")") if ors else None


def option_counts(cur, where_sql: str, params: list) -> dict:
    """How many leads each "Follow-up" box has, over the page's own leads."""
    ensure_tables()
    e = _state_sql("f.due_date", _today_sql(params[0]))
    keys = [k for k, _ in FILTER_OPTIONS]
    c = cur.connection.cursor()
    try:
        c.execute(f"""SELECT {', '.join(f'count(*) FILTER (WHERE {e[k]})' for k in keys)}
                      FROM merchant_pipeline_leads mpl
                      LEFT JOIN lead_followups f ON f.lead_id=mpl.id AND f.tenant_id=mpl.tenant_id
                                                 AND f.done_at IS NULL AND f.cancelled_at IS NULL
                      WHERE {where_sql}""", list(params))
        out = dict(zip(keys, c.fetchone()))
    finally:
        c.close()
    return out


# ── "Follow-ups" page and the morning reminder ──────────────────────────────

def due_list(tenant_id: int, scope_sql: str = "", scope_params=(), assigned: str = None, days_ahead: int = 7) -> dict:
    """Open follow-ups on open leads, overdue first: {'overdue': [...], 'today': [...], 'soon': [...]}."""
    ensure_tables()
    today = _today(tenant_id)
    sql = f"""SELECT f.id, f.lead_id, f.due_date, f.note, f.source, f.auto, f.set_by_name, f.channel, f.reschedule_count,
                     mpl.customer_name, mpl.contact_person AS contact_name, mpl.phone, mpl.whatsapp_number, mpl.email,
                     mpl.assigned_key, mpl.assigned_to, mpl.is_opportunity, mpl.stage,
                     (SELECT max(a.created_at) FROM staff_activity a WHERE a.tenant_id=mpl.tenant_id AND a.lead_id=mpl.id
                         AND NOT a.auto AND a.action = ANY(%s)) AS last_contact
                FROM lead_followups f JOIN merchant_pipeline_leads mpl ON mpl.id=f.lead_id AND mpl.tenant_id=f.tenant_id
               WHERE f.tenant_id=%s AND f.done_at IS NULL AND f.cancelled_at IS NULL
                 AND mpl.outcome IS NULL AND mpl.dropped_at IS NULL AND f.due_date <= %s{scope_sql}"""
    params = [list(CONTACT_ACTIONS), tenant_id, today + _dt.timedelta(days=days_ahead)] + list(scope_params)
    if assigned == "none":
        sql += " AND (mpl.assigned_key IS NULL OR mpl.assigned_key='')"
    elif assigned:
        sql += " AND mpl.assigned_key=%s"
        params.append(assigned)
    sql += " ORDER BY f.due_date, mpl.customer_name LIMIT 2000"
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        conn.close()
    out = {"overdue": [], "today": [], "soon": [], "today_date": today}
    for r in rows:
        r["source_label"] = SOURCE_WORDS.get(r["source"] or "", "")
        r["when_words"] = _when_words(r["due_date"], today)
        r["channel_label"] = CHANNEL_LABELS.get(r["channel"] or "", "")
        r["after_words"] = AFTER_WORDS.get(r["source"] or "", "")
        st = _state(r["due_date"], today)
        out["soon" if st == "later" else st].append(r)
    return out


# ── Team performance: follow-ups per person ─────────────────────────────────

def report_counts(cur, tenant_id: int, start: _dt.date, end: _dt.date, today: _dt.date, tzn: str) -> dict:
    """Per assigned person, follow-ups due in the period: due, done on time,
    done late, missed (still not done and the day has passed), plus how many
    are overdue right now on their open leads, and how many open ones have
    been moved RESCHEDULED_OFTEN times or more."""
    ensure_tables()
    cur.execute("""
        SELECT mpl.assigned_key AS k,
               count(*) FILTER (WHERE f.due_date BETWEEN %(s)s AND %(e)s AND f.cancelled_at IS NULL) AS due,
               count(*) FILTER (WHERE f.due_date BETWEEN %(s)s AND %(e)s AND f.done_at IS NOT NULL
                                  AND (f.done_at AT TIME ZONE %(tz)s)::date <= f.due_date) AS on_time,
               count(*) FILTER (WHERE f.due_date BETWEEN %(s)s AND %(e)s AND f.done_at IS NOT NULL
                                  AND (f.done_at AT TIME ZONE %(tz)s)::date > f.due_date) AS late,
               count(*) FILTER (WHERE f.due_date BETWEEN %(s)s AND %(e)s AND f.due_date < %(t)s
                                  AND f.done_at IS NULL AND f.cancelled_at IS NULL
                                  AND mpl.outcome IS NULL AND mpl.dropped_at IS NULL) AS missed,
               count(*) FILTER (WHERE f.due_date < %(t)s AND f.done_at IS NULL AND f.cancelled_at IS NULL
                                  AND mpl.outcome IS NULL AND mpl.dropped_at IS NULL) AS overdue_now,
               count(*) FILTER (WHERE f.reschedule_count >= %(often)s AND f.done_at IS NULL AND f.cancelled_at IS NULL
                                  AND mpl.outcome IS NULL AND mpl.dropped_at IS NULL) AS rescheduled_often
          FROM lead_followups f JOIN merchant_pipeline_leads mpl ON mpl.id=f.lead_id AND mpl.tenant_id=f.tenant_id
         WHERE f.tenant_id=%(tid)s AND mpl.assigned_key IS NOT NULL
         GROUP BY 1""", {"s": start, "e": end, "t": today, "tz": tzn, "tid": tenant_id, "often": RESCHEDULED_OFTEN})
    return {r["k"]: r for r in cur.fetchall()}


# ── Redesign (2026-10-23): calendar busy days, work-list tabs, team summary ──

def workload(tenant_id: int, assigned_key: str, skip_lead: int = None, days: int = 120) -> dict:
    """{'YYYY-MM-DD': n} open follow-ups the person already has each day, for
    the dots under the calendar days."""
    ensure_tables()
    today = _today(tenant_id)
    if not assigned_key:
        return {}
    conn = get_db_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""SELECT f.due_date, count(*) FROM lead_followups f
                         JOIN merchant_pipeline_leads mpl ON mpl.id=f.lead_id AND mpl.tenant_id=f.tenant_id
                        WHERE f.tenant_id=%s AND mpl.assigned_key=%s AND f.done_at IS NULL AND f.cancelled_at IS NULL
                          AND mpl.outcome IS NULL AND mpl.dropped_at IS NULL
                          AND f.due_date BETWEEN %s AND %s AND f.lead_id <> %s
                        GROUP BY 1""", (tenant_id, assigned_key, today, today + _dt.timedelta(days=days), skip_lead or 0))
        return {d.isoformat(): n for d, n in cur.fetchall()}
    finally:
        conn.close()


def _assigned_sql(assigned):
    if assigned == "none":
        return " AND (mpl.assigned_key IS NULL OR mpl.assigned_key='')", []
    if assigned:
        return " AND mpl.assigned_key=%s", [assigned]
    return "", []


def no_date_list(tenant_id: int, scope_sql: str = "", scope_params=(), assigned: str = None, limit: int = 100) -> dict:
    """Open leads with no open follow-up: {'count': n, 'rows': [...newest first]}."""
    ensure_tables()
    a_sql, a_params = _assigned_sql(assigned)
    where = f"""mpl.tenant_id=%s AND mpl.outcome IS NULL AND mpl.dropped_at IS NULL{scope_sql}{a_sql}
                AND NOT EXISTS (SELECT 1 FROM lead_followups f WHERE f.lead_id=mpl.id AND f.tenant_id=mpl.tenant_id
                                AND f.done_at IS NULL AND f.cancelled_at IS NULL)"""
    params = [tenant_id] + list(scope_params) + a_params
    conn = get_db_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"SELECT count(*) AS n FROM merchant_pipeline_leads mpl WHERE {where}", params)
        n = cur.fetchone()["n"]
        cur.execute(f"""SELECT mpl.id AS lead_id, mpl.customer_name, mpl.contact_person AS contact_name, mpl.phone,
                               mpl.whatsapp_number, mpl.email, mpl.assigned_key, mpl.assigned_to, mpl.is_opportunity, mpl.stage
                          FROM merchant_pipeline_leads mpl WHERE {where}
                         ORDER BY mpl.updated_at DESC NULLS LAST, mpl.id DESC LIMIT %s""", params + [limit])
        return {"count": n, "rows": cur.fetchall()}
    finally:
        conn.close()


def done_list(tenant_id: int, scope_sql: str = "", scope_params=(), assigned: str = None, days: int = 7) -> list:
    """Follow-ups ticked off in the last `days` days, newest first."""
    ensure_tables()
    import team_performance
    tz = team_performance.tz_for(tenant_id)
    a_sql, a_params = _assigned_sql(assigned)
    conn = get_db_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"""SELECT f.lead_id, f.due_date, f.note, f.channel, f.done_at, f.done_by_name, f.done_action,
                               mpl.customer_name, mpl.contact_person AS contact_name, mpl.assigned_key, mpl.assigned_to
                          FROM lead_followups f JOIN merchant_pipeline_leads mpl ON mpl.id=f.lead_id AND mpl.tenant_id=f.tenant_id
                         WHERE f.tenant_id=%s AND f.done_at >= NOW() - (%s || ' days')::interval{scope_sql}{a_sql}
                         ORDER BY f.done_at DESC LIMIT 300""", [tenant_id, str(days)] + list(scope_params) + a_params)
        rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        r["done_local"] = r["done_at"].astimezone(tz)
        late = (r["done_local"].date() - r["due_date"]).days
        r["on_time"] = late <= 0
        r["late_days"] = max(late, 0)
        r["done_label"] = SOURCE_WORDS.get(r["done_action"] or "", r["done_action"] or "")
        r["channel_label"] = CHANNEL_LABELS.get(r["channel"] or "", "")
    return rows


def _week_start(today: _dt.date) -> _dt.date:
    return today - _dt.timedelta(days=today.weekday())


def team_summary(tenant_id: int, scope_sql: str = "", scope_params=(), assigned: str = None) -> dict:
    """Per assigned person: overdue, due today, next 7 days, on time this week
    (due Mon..today: done by its day / all due), open leads with no date.
    Key '' = unassigned."""
    ensure_tables()
    today = _today(tenant_id)
    ws = _week_start(today)
    from team_performance import tz_for
    tzn = str(tz_for(tenant_id))
    a_sql, a_params = _assigned_sql(assigned)
    conn = get_db_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"""
            SELECT COALESCE(mpl.assigned_key, '') AS k,
                   count(f.id) FILTER (WHERE f.done_at IS NULL AND f.cancelled_at IS NULL AND f.due_date < %(t)s) AS overdue,
                   count(f.id) FILTER (WHERE f.done_at IS NULL AND f.cancelled_at IS NULL AND f.due_date = %(t)s) AS today,
                   count(f.id) FILTER (WHERE f.done_at IS NULL AND f.cancelled_at IS NULL
                                         AND f.due_date > %(t)s AND f.due_date <= %(t)s + 7) AS week,
                   count(*) FILTER (WHERE f.id IS NULL) AS no_date
              FROM merchant_pipeline_leads mpl
              LEFT JOIN lead_followups f ON f.lead_id=mpl.id AND f.tenant_id=mpl.tenant_id
                                         AND f.done_at IS NULL AND f.cancelled_at IS NULL
             WHERE mpl.tenant_id=%(tid)s AND mpl.outcome IS NULL AND mpl.dropped_at IS NULL{scope_sql.replace('%s', '%(s0)s')}{a_sql.replace('%s', '%(a0)s')}
             GROUP BY 1""", {"t": today, "tid": tenant_id, "s0": (list(scope_params) or [None])[0],
                             "a0": (a_params or [None])[0]})
        out = {r["k"]: dict(r) for r in cur.fetchall()}
        # On time this week counts closed leads too: the work was due either way.
        cur.execute(f"""
            SELECT COALESCE(mpl.assigned_key, '') AS k,
                   count(*) FILTER (WHERE f.done_at IS NOT NULL AND (f.done_at AT TIME ZONE %(tz)s)::date <= f.due_date) AS on_time,
                   count(*) AS due
              FROM lead_followups f JOIN merchant_pipeline_leads mpl ON mpl.id=f.lead_id AND mpl.tenant_id=f.tenant_id
             WHERE f.tenant_id=%(tid)s AND f.cancelled_at IS NULL AND f.due_date BETWEEN %(ws)s AND %(t)s
               AND (f.done_at IS NOT NULL OR f.due_date < %(t)s){scope_sql.replace('%s', '%(s0)s')}{a_sql.replace('%s', '%(a0)s')}
             GROUP BY 1""", {"t": today, "ws": ws, "tz": tzn, "tid": tenant_id,
                             "s0": (list(scope_params) or [None])[0], "a0": (a_params or [None])[0]})
        for r in cur.fetchall():
            out.setdefault(r["k"], {"k": r["k"], "overdue": 0, "today": 0, "week": 0, "no_date": 0})
            out[r["k"]].update(on_time=r["on_time"], due_week=r["due"])
    finally:
        conn.close()
    for v in out.values():
        v.setdefault("on_time", 0); v.setdefault("due_week", 0)
        v["pct"] = round(100 * v["on_time"] / v["due_week"]) if v["due_week"] else None
    return out


# ── What was said (2026-10-23): one history per lead, newest first ──────────
KIND_WORDS = {"call": "Call", "whatsapp": "WhatsApp", "email": "Email", "linkedin": "LinkedIn", "meeting": "Meeting"}


def add_note(tenant_id: int, lead_id: int, kind: str, result: str, note: str, actor: dict) -> None:
    import staff_activity
    ensure_tables()
    name = staff_activity.current_name(actor["label"]) or actor["label"]
    conn = get_db_connection()
    try:
        conn.autocommit = True
        conn.cursor().execute("""INSERT INTO lead_contact_notes (tenant_id, lead_id, kind, result, note, actor_key, actor_name)
                                 VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                              (tenant_id, lead_id, kind[:12], (result or "")[:80] or None, note, actor["key"], name[:200]))
    finally:
        conn.close()


def lead_notes(tenant_id: int, lead_id: int, tz, limit: int = 100) -> list:
    ensure_tables()
    conn = get_db_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT kind, result, note, actor_name, created_at FROM lead_contact_notes
                       WHERE tenant_id=%s AND lead_id=%s ORDER BY created_at DESC LIMIT %s""", (tenant_id, lead_id, limit))
        rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        r["kind_label"] = KIND_WORDS.get(r["kind"], r["kind"])
        r["local_at"] = r["created_at"].astimezone(tz)
    return rows


def latest_notes(tenant_id: int, lead_ids: list, tz) -> dict:
    """{lead_id: newest note row} for the Follow-ups list."""
    ensure_tables()
    if not lead_ids:
        return {}
    conn = get_db_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT DISTINCT ON (lead_id) lead_id, kind, result, note, actor_name, created_at
                         FROM lead_contact_notes WHERE tenant_id=%s AND lead_id = ANY(%s)
                        ORDER BY lead_id, created_at DESC""", (tenant_id, list(lead_ids)))
        rows = cur.fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        r["kind_label"] = KIND_WORDS.get(r["kind"], r["kind"])
        r["local_at"] = r["created_at"].astimezone(tz)
        out[r["lead_id"]] = r
    return out
