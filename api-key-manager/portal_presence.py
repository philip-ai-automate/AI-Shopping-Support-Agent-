"""Who's Online — who is really using the portal right now (5 Nov 2026).

Purpose: know when it is safe to restart phixtra-portal (one gunicorn process
serves the customer portal, Estate/KeyLand and School, so all three count).

What counts as "using": opening a page (browser navigation) or doing something
(POST/PUT/PATCH/DELETE — saving, sending, clicking a button that changes
something). Background refreshes — the inbox poll, calls poll, badge counts —
are plain fetch() GETs and do NOT count, so a tab left open goes idle.

Saved at most once a minute per person. Admin impersonation never counts.
PhiXtra's own / test / demo businesses are flagged `internal` so they can be
hidden (they should not block a restart).

Tables:
  portal_presence       one row per person: last action, page, app, business
  portal_activity_hours one row per person per hour they did something
                        (for the quiet-hours chart)
  portal_activity_days  one row per person per day: first + last action,
                        active minutes (Daily view, 5 Nov 2026).
All purged after 90 days.
"""
import time as _time
import threading

from db import get_db_connection

ACTIVE_MIN = 5      # active now = did something in the last 5 minutes
IDLE_MIN = 30       # idle = 5–30 minutes
KEEP_DAYS = 90
CHART_TZ = "Europe/London"

# PhiXtra's own, test and demo businesses (customer portal tenants):
# 17 ProfitBuyz, 19 PhiXtra AI, 85 TechMart demo, 388 Harbour Supplies (B2B demo),
# 392 Phixtra Test Store, 393 Test Corp (David), 394 Ashby Row (B2C demo).
# Any business with a demo chat link is added automatically.
INTERNAL_TENANT_IDS = (17, 19, 85, 388, 392, 393, 394)
# KeyLand (Estate): 8 Test Estate Agency, 9 Demo Agency, 11 PROFITBUYZ LIMITED.
INTERNAL_ESTATE_IDS = (8, 9, 11)

_ready = False
_ready_lock = threading.Lock()
_last_purge = 0.0


def _db():
    conn = get_db_connection()
    return conn, conn.cursor()


def ensure_tables():
    global _ready
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        conn, cur = _db()
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS portal_presence (
                    actor_key       TEXT PRIMARY KEY,
                    app             TEXT NOT NULL,
                    tenant_id       INTEGER,
                    actor_id        INTEGER,
                    is_team         BOOLEAN NOT NULL DEFAULT FALSE,
                    internal        BOOLEAN NOT NULL DEFAULT FALSE,
                    path            TEXT,
                    last_action_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    first_today_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
            cur.execute("CREATE INDEX IF NOT EXISTS portal_presence_last_idx ON portal_presence (last_action_at DESC)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS portal_activity_hours (
                    hour       TIMESTAMPTZ NOT NULL,
                    actor_key  TEXT NOT NULL,
                    app        TEXT NOT NULL,
                    internal   BOOLEAN NOT NULL DEFAULT FALSE,
                    PRIMARY KEY (hour, actor_key)
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS portal_activity_days (
                    day        DATE NOT NULL,
                    actor_key  TEXT NOT NULL,
                    app        TEXT NOT NULL,
                    tenant_id  INTEGER,
                    actor_id   INTEGER,
                    is_team    BOOLEAN NOT NULL DEFAULT FALSE,
                    internal   BOOLEAN NOT NULL DEFAULT FALSE,
                    first_at   TIMESTAMPTZ NOT NULL,
                    last_at    TIMESTAMPTZ NOT NULL,
                    minutes    INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (day, actor_key)
                )""")
            conn.commit()
            _ready = True
        finally:
            cur.close(); conn.close()


# ── Recording ────────────────────────────────────────────────────────────────

def _is_real_action(request) -> bool:
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        return True
    if request.method != "GET":
        return False
    dest = request.headers.get("Sec-Fetch-Dest")
    if dest:
        return dest in ("document", "iframe")
    # Old browsers without Sec-Fetch-*: a page load asks for HTML, fetch() asks */*
    return "text/html" in (request.headers.get("Accept") or "") and \
        request.headers.get("X-Requested-With") != "XMLHttpRequest"


def _who(session):
    """(actor_key, app, tenant_sql_value, actor_id, is_team) or None."""
    if session.get("impersonate_customer_id"):
        return None
    if session.get("portal_logged_in") and session.get("customer_id"):
        cid = int(session["customer_id"])
        tm = session.get("team_member_id")
        if tm:
            return (f"team:{int(tm)}", "portal", ("team", int(tm)), int(tm), True)
        return (f"owner:{cid}", "portal", ("customer", cid), cid, False)
    if session.get("re_logged_in") and session.get("re_tenant_id"):
        tid = int(session["re_tenant_id"])
        sid = session.get("re_staff_id")
        if sid:
            return (f"re_staff:{int(sid)}", "estate", ("direct", tid), int(sid), True)
        return (f"re_owner:{tid}", "estate", ("direct", tid), tid, False)
    if session.get("school_logged_in") and session.get("school_id"):
        sch = int(session["school_id"])
        sid = session.get("school_staff_id")
        return (f"school_staff:{int(sid or 0)}", "school", ("direct", sch), int(sid or 0), True)
    return None


def record(request, session):
    """App-wide before_request hook. Never raises, never blocks a page."""
    try:
        if request.endpoint == "static" or request.path.startswith(("/admin", "/static")):
            return None
        if not _is_real_action(request):
            return None
        who = _who(session)
        if not who:
            return None
        now = int(_time.time())
        if now - int(session.get("_presence_at") or 0) < 60 and session.get("_presence_key") == who[0]:
            return None
        session["_presence_at"] = now
        session["_presence_key"] = who[0]
        _save(who, request.path[:300], bool(session.get("demo_visitor") or session.get("demo_tenant")))
    except Exception as e:
        print("⚠️ presence record:", e)
    return None


def _save(who, path, demo_session):
    global _last_purge
    ensure_tables()
    key, app, (kind, ref), actor_id, is_team = who
    if kind == "customer":
        tenant_sql, tenant_arg = "(SELECT tenant_id FROM customers WHERE id=%s)", ref
    elif kind == "team":
        tenant_sql, tenant_arg = "(SELECT tenant_id FROM team_members WHERE id=%s)", ref
    else:
        tenant_sql, tenant_arg = "%s", ref
    if app == "portal":
        internal_sql = f"""(%s OR {tenant_sql} IN %s OR EXISTS
                            (SELECT 1 FROM chat_links WHERE is_demo AND tenant_id={tenant_sql}))"""
        internal_args = (demo_session, tenant_arg, INTERNAL_TENANT_IDS, tenant_arg)
    elif app == "estate":
        internal_sql, internal_args = "(%s OR %s IN %s)", (demo_session, ref, INTERNAL_ESTATE_IDS)
    else:
        internal_sql, internal_args = "%s", (demo_session,)
    conn, cur = _db()
    try:
        cur.execute(f"""
            INSERT INTO portal_presence (actor_key, app, tenant_id, actor_id, is_team, internal, path,
                                         last_action_at, first_today_at)
            VALUES (%s, %s, {tenant_sql}, %s, %s, {internal_sql}, %s, NOW(), NOW())
            ON CONFLICT (actor_key) DO UPDATE SET
                app=EXCLUDED.app, tenant_id=EXCLUDED.tenant_id, internal=EXCLUDED.internal,
                path=EXCLUDED.path,
                first_today_at = CASE
                    WHEN (portal_presence.last_action_at AT TIME ZONE %s)::date
                         < (NOW() AT TIME ZONE %s)::date
                    THEN NOW() ELSE portal_presence.first_today_at END,
                last_action_at=NOW()
            RETURNING internal, tenant_id""",
            (key, app, tenant_arg, actor_id, is_team, *internal_args, path, CHART_TZ, CHART_TZ))
        internal, tenant_id = cur.fetchone()
        # Daily: saved at most once a minute per person (see record), so
        # minutes = number of minutes they did something that day.
        cur.execute("""INSERT INTO portal_activity_days
                         (day, actor_key, app, tenant_id, actor_id, is_team, internal, first_at, last_at, minutes)
                       VALUES ((NOW() AT TIME ZONE %s)::date, %s, %s, %s, %s, %s, %s, NOW(), NOW(), 1)
                       ON CONFLICT (day, actor_key) DO UPDATE SET
                         app=EXCLUDED.app, tenant_id=EXCLUDED.tenant_id, internal=EXCLUDED.internal,
                         last_at=NOW(), minutes=portal_activity_days.minutes + 1""",
                    (CHART_TZ, key, app, tenant_id, actor_id, is_team, internal))
        cur.execute("""INSERT INTO portal_activity_hours (hour, actor_key, app, internal)
                       VALUES (date_trunc('hour', NOW()), %s, %s, %s) ON CONFLICT DO NOTHING""",
                    (key, app, internal))
        if _time.time() - _last_purge > 3600:
            _last_purge = _time.time()
            cur.execute("DELETE FROM portal_activity_hours WHERE hour < NOW() - make_interval(days => %s)", (KEEP_DAYS,))
            cur.execute("DELETE FROM portal_activity_days WHERE day < CURRENT_DATE - %s", (KEEP_DAYS,))
            cur.execute("DELETE FROM portal_presence WHERE last_action_at < NOW() - make_interval(days => %s)", (KEEP_DAYS,))
        conn.commit()
    finally:
        cur.close(); conn.close()


# ── Reading ──────────────────────────────────────────────────────────────────

_NAME_COLS = """
       CASE p.app
         WHEN 'portal' THEN COALESCE(t.name, cu.company_name)
         WHEN 'estate' THEN rt.business_name
         WHEN 'school' THEN sp.school_name END AS business,
       CASE
         WHEN p.actor_key LIKE 'team:%%'
              THEN COALESCE(NULLIF(TRIM(CONCAT_WS(' ', tm.first_name, tm.last_name)), ''), tm.name, tm.email)
         WHEN p.actor_key LIKE 'owner:%%'
              THEN COALESCE(NULLIF(TRIM(CONCAT_WS(' ', cu.first_name, cu.last_name)), ''), cu.email)
         WHEN p.actor_key LIKE 're_staff:%%'
              THEN COALESCE(NULLIF(TRIM(CONCAT_WS(' ', rs.first_name, rs.last_name)), ''), rs.email)
         WHEN p.actor_key LIKE 're_owner:%%'
              THEN COALESCE(NULLIF(TRIM(CONCAT_WS(' ', rt.first_name, rt.last_name)), ''), rt.email)
         WHEN p.actor_key LIKE 'school_staff:%%'
              THEN COALESCE(ss.full_name, ss.email) END AS person,
       COALESCE(tm.email, cu.email, rs.email, rt.email, ss.email) AS email
"""

_NAME_JOINS = """
  LEFT JOIN team_members tm   ON p.actor_key LIKE 'team:%%'         AND tm.id = p.actor_id
  LEFT JOIN customers cu      ON p.actor_key LIKE 'owner:%%'        AND cu.id = p.actor_id
  LEFT JOIN tenants t         ON p.app = 'portal'                   AND t.id  = p.tenant_id
  LEFT JOIN re_tenants rt     ON p.app = 'estate'                   AND rt.id = p.tenant_id
  LEFT JOIN re_staff rs       ON p.actor_key LIKE 're_staff:%%'     AND rs.id = p.actor_id
  LEFT JOIN school_profiles sp ON p.app = 'school'                  AND sp.id = p.tenant_id
  LEFT JOIN school_staff ss   ON p.actor_key LIKE 'school_staff:%%' AND ss.id = p.actor_id
"""

_PEOPLE_SQL = """
SELECT p.actor_key, p.app, p.tenant_id, p.is_team, p.internal, p.path,
       p.last_action_at, p.first_today_at,
       EXTRACT(EPOCH FROM (NOW() - p.last_action_at))::int AS secs_ago,
""" + _NAME_COLS + """  FROM portal_presence p
""" + _NAME_JOINS + """ WHERE (p.last_action_at AT TIME ZONE %s)::date = (NOW() AT TIME ZONE %s)::date
    OR p.last_action_at > NOW() - make_interval(mins => %s)
 ORDER BY p.last_action_at DESC
"""


def people(include_internal=False) -> dict:
    """{'active': [...], 'idle': [...], 'today': [...]} — today = signed in
    and used the portal earlier today (London time) but not in the last 30 min."""
    ensure_tables()
    import psycopg2.extras
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(_PEOPLE_SQL, (CHART_TZ, CHART_TZ, IDLE_MIN))
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    out = {"active": [], "idle": [], "today": [], "hidden_internal": 0}
    for r in rows:
        if r["internal"] and not include_internal:
            if r["secs_ago"] < ACTIVE_MIN * 60:
                out["hidden_internal"] += 1
            continue
        if r["secs_ago"] < ACTIVE_MIN * 60:
            out["active"].append(r)
        elif r["secs_ago"] < IDLE_MIN * 60:
            out["idle"].append(r)
        else:
            out["today"].append(r)
    return out


def active_count() -> int:
    """Customers (not PhiXtra/test/demo) who did something in the last 5 min."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT COUNT(*) FROM portal_presence
                        WHERE NOT internal AND last_action_at > NOW() - make_interval(mins => %s)""",
                    (ACTIVE_MIN,))
        return int(cur.fetchone()[0])
    finally:
        cur.close(); conn.close()


def hourly(days=14, include_internal=False):
    """Average number of distinct people active in each hour of the day
    (London time) over the last `days` days (or since tracking started)
    → (24 rows, tracking-started datetime)."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT MIN(hour) FROM portal_activity_hours
                        WHERE hour > NOW() - make_interval(days => %s)""", (days,))
        since = cur.fetchone()[0]
        cur.execute("""
            SELECT EXTRACT(HOUR FROM hour AT TIME ZONE %s)::int AS h, SUM(cnt), MAX(cnt)
              FROM (SELECT hour, COUNT(*) AS cnt FROM portal_activity_hours
                     WHERE hour > NOW() - make_interval(days => %s) AND (%s OR NOT internal)
                     GROUP BY hour) x
             GROUP BY 1""", (CHART_TZ, days, include_internal))
        rows = cur.fetchall()
        cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - %s))", (since,)) if since else None
        span_days = (float(cur.fetchone()[0]) / 86400) if since else 0
    finally:
        cur.close(); conn.close()
    n_days = max(1, min(days, int(span_days) + 1))
    got = {int(h): (round(float(total) / n_days, 1), int(peak)) for h, total, peak in rows}
    return [{"hour": h, "avg": got.get(h, (0, 0))[0], "peak": got.get(h, (0, 0))[1]} for h in range(24)], since


def daily(days=KEEP_DAYS, include_internal=False):
    """One row per day (UK date), newest first: people, first in / last out,
    busiest hour, quiet hours. Only days since tracking started."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""
            SELECT day, COUNT(*), MIN(first_at), MAX(last_at), SUM(minutes)
              FROM portal_activity_days
             WHERE day > CURRENT_DATE - %s AND (%s OR NOT internal)
             GROUP BY day""", (days, include_internal))
        per_day = {r[0]: r for r in cur.fetchall()}
        cur.execute("""
            SELECT (hour AT TIME ZONE %s)::date AS d,
                   EXTRACT(HOUR FROM hour AT TIME ZONE %s)::int AS h, COUNT(*)
              FROM portal_activity_hours
             WHERE hour > NOW() - make_interval(days => %s) AND (%s OR NOT internal)
             GROUP BY 1, 2""", (CHART_TZ, CHART_TZ, days + 1, include_internal))
        hrs = {}
        for d, h, n in cur.fetchall():
            hrs.setdefault(d, {})[int(h)] = int(n)
        cur.execute("SELECT MIN(day) FROM portal_activity_days")
        started = cur.fetchone()[0]
        cur.execute("SELECT (NOW() AT TIME ZONE %s)::date, EXTRACT(HOUR FROM NOW() AT TIME ZONE %s)::int",
                    (CHART_TZ, CHART_TZ))
        today, now_h = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not started:
        return [], None
    import datetime as _dt
    out, d = [], today
    while d >= started and d > today - _dt.timedelta(days=days):
        h = hrs.get(d, {})
        last_h = now_h if d == today else 23
        busiest = max(h.items(), key=lambda kv: (kv[1], -kv[0])) if h else None
        r = per_day.get(d)
        out.append({"day": d, "people": int(r[1]) if r else 0,
                    "first_at": r[2] if r else None, "last_at": r[3] if r else None,
                    "minutes": int(r[4]) if r else 0,
                    "busiest": busiest, "quiet": _ranges([x for x in range(last_h + 1) if not h.get(x)]),
                    "is_today": d == today})
        d -= _dt.timedelta(days=1)
    return out, started


def _ranges(hours):
    """[0,1,2,3,7,23] → '00–03, 07, 23'."""
    out, i = [], 0
    while i < len(hours):
        j = i
        while j + 1 < len(hours) and hours[j + 1] == hours[j] + 1:
            j += 1
        out.append(f"{hours[i]:02d}" if i == j else f"{hours[i]:02d}–{hours[j]:02d}")
        i = j + 1
    return ", ".join(out)


def day_detail(day, include_internal=False):
    """(people rows, 24 hour counts) for one UK date."""
    ensure_tables()
    import psycopg2.extras
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT p.actor_key, p.app, p.tenant_id, p.is_team, p.internal,
                   p.first_at, p.last_at, p.minutes, """ + _NAME_COLS + """
              FROM portal_activity_days p """ + _NAME_JOINS + """
             WHERE p.day = %s AND (%s OR NOT p.internal)
             ORDER BY p.first_at""", (day, include_internal))
        rows = cur.fetchall() or []
        cur.execute("""
            SELECT actor_key, EXTRACT(HOUR FROM hour AT TIME ZONE %s)::int AS h
              FROM portal_activity_hours
             WHERE (hour AT TIME ZONE %s)::date = %s AND (%s OR NOT internal)""",
                    (CHART_TZ, CHART_TZ, day, include_internal))
        per_hour, by_person = [0] * 24, {}
        for r in cur.fetchall():
            per_hour[int(r["h"])] += 1
            by_person.setdefault(r["actor_key"], []).append(int(r["h"]))
    finally:
        cur.close(); conn.close()
    for r in rows:
        r["hours"] = _ranges(sorted(by_person.get(r["actor_key"], [])))
    return rows, per_hour


APP_LABEL = {"portal": "Portal", "estate": "KeyLand", "school": "School"}


if __name__ == "__main__":
    # Restart check: `venv/bin/python portal_presence.py`
    import sys
    p = people(include_internal="--all" in sys.argv)
    act = p["active"]
    if not act:
        print(f"SAFE TO RESTART — nobody active in the last {ACTIVE_MIN} minutes"
              + (f" ({p['hidden_internal']} PhiXtra/test/demo hidden)" if p["hidden_internal"] else ""))
    else:
        print(f"⚠ {len(act)} active now:")
        for r in act:
            print(f"  - {r['person'] or r['actor_key']} · {r['business'] or ''} · {APP_LABEL.get(r['app'])}"
                  f" · {r['path']} · {r['secs_ago'] // 60} min ago")
    if p["idle"]:
        print(f"{len(p['idle'])} idle (5–30 min)")
