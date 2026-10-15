"""Team performance — the numbers behind the manager's page (user 2026-10-11,
approved mock-up https://claude.ai/artifact/TKYQdZHJAUqMyt2hLEPJ1Y).

Everything is read from staff_activity / staff_presence (staff_activity.py),
plus a few existing tables: leads (who is assigned what), email_replies and
wa_message_log (customer replies). Days follow each business's own time zone
(staff_activity.tenant_tz_name); Mon–Fri count as working days. Nothing here writes, except save_targets/log_call.
"""
import datetime as _dt
from zoneinfo import ZoneInfo

import psycopg2.extras

from db import get_db_connection
from staff_activity import tenant_tz_name
RECORDING_STARTED = _dt.date(2026, 10, 11)   # staff_activity went live

PERIODS = {"today": "Today", "yesterday": "Yesterday", "week": "This week", "month": "This month"}

MESSAGE_ACTIONS = ("wa_sent", "messenger_sent", "webchat_sent")
# Actions that are real sales work (presence alone is not).
WORK_ACTIONS = ("call", "wa_sent", "messenger_sent", "webchat_sent", "email_sent", "seq_added",
                "note_added", "lead_created", "lead_edited", "opportunity", "proposal", "won", "lost",
                "not_a_fit", "reopened", "stage_moved", "chat_claimed", "reply_logged",
                "contact_created", "contact_edited", "company_created", "leads_assigned")

CALL_OUTCOMES = [("answered", "Answered"), ("no_answer", "No answer"), ("busy", "Busy / call back"),
                 ("voicemail", "Left a voicemail"), ("wrong_number", "Wrong number"),
                 ("not_needed", "Doesn't need us")]
CALL_OUTCOME_LABELS = dict(CALL_OUTCOMES)

IDLE_ALERT_MIN = 180        # "no activity for 3 hours" during the working day
LOGIN_BY_HOUR = 10          # "no login by 10:00"
WORK_START, WORK_END = 9, 18


def tz_for(tenant_id: int):
    return ZoneInfo(tenant_tz_name(tenant_id))


def tz_label(tz) -> str:
    """"London", "Lagos", "New York" — for "updated 18:00 London time"."""
    return tz.key.split("/")[-1].replace("_", " ")


def now_local(tz):
    return _dt.datetime.now(tz)


def is_workday(d) -> bool:
    return d.weekday() < 5


def working_days(start, end) -> int:
    n, d = 0, start
    while d <= end:
        n += is_workday(d)
        d += _dt.timedelta(days=1)
    return n


def _prev_workday(d):
    d -= _dt.timedelta(days=1)
    while not is_workday(d):
        d -= _dt.timedelta(days=1)
    return d


def resolve_period(key: str, tz):
    """(key, start, end, prev_start, prev_end, label, vs_words)."""
    today = now_local(tz).date()
    if key not in PERIODS:
        key = "today"
    if key == "today":
        p = _prev_workday(today)
        return key, today, today, p, p, f"Today · {today:%A %-d %B %Y}", f"vs {p:%A}"
    if key == "yesterday":
        y = today - _dt.timedelta(days=1)
        p = _prev_workday(y)
        return key, y, y, p, p, f"Yesterday · {y:%A %-d %B %Y}", f"vs {p:%A}"
    if key == "week":
        s = today - _dt.timedelta(days=today.weekday())
        return (key, s, today, s - _dt.timedelta(days=7), today - _dt.timedelta(days=7),
                f"This week · {s:%a %-d} – {today:%a %-d %B %Y}", "vs same days last week")
    s = today.replace(day=1)
    ps = (s - _dt.timedelta(days=1)).replace(day=1)
    try:
        pe = ps.replace(day=today.day)
    except ValueError:
        pe = s - _dt.timedelta(days=1)
    return (key, s, today, ps, pe,
            f"This month · 1 – {today:%-d %B %Y} ({working_days(s, today)} of "
            f"{working_days(s, _month_end(s))} working days)", "vs same days last month")


def _month_end(first):
    nxt = (first.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
    return nxt - _dt.timedelta(days=1)


def _cur(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ── who is on the page ─────────────────────────────────────────────────────

def _all_people(cur, tenant_id):
    cur.execute("""SELECT m.id, m.name, m.is_active, m.line_manager_id, r.name AS role_name,
                          lm.name AS manager_name
                   FROM team_members m
                   LEFT JOIN tenant_roles r ON r.id = m.role_id
                   LEFT JOIN team_members lm ON lm.id = m.line_manager_id
                   WHERE m.tenant_id=%s""", (tenant_id,))
    return cur.fetchall()


def _reports_of(rows, manager_id):
    """Everyone under manager_id, directly or further down."""
    out, todo = set(), [manager_id]
    while todo:
        mid = todo.pop()
        for r in rows:
            if r["line_manager_id"] == mid and r["id"] not in out:
                out.add(r["id"])
                todo.append(r["id"])
    return out


def viewer_scope(tenant_id: int, viewer: dict):
    """Keys the viewer may see: the owner sees everyone; a staff member sees
    the people who report to them (any level down) and themselves."""
    conn = get_db_connection()
    try:
        cur = _cur(conn)
        rows = _all_people(cur, tenant_id)
    finally:
        conn.close()
    if not viewer.get("is_team"):
        return None, rows                                  # None = no limit
    me = viewer["team_member_id"]
    keys = {f"team:{i}" for i in _reports_of(rows, me)} | {f"team:{me}"}
    return keys, rows


def can_view(tenant_id: int, viewer: dict, actor_key: str) -> bool:
    """May this viewer look at actor_key's numbers and day?"""
    keys, _ = viewer_scope(tenant_id, viewer)
    return keys is None or actor_key in keys


# ── the numbers ─────────────────────────────────────────────────────────────

def _activity_totals(cur, tenant_id, start, end, tzn):
    cur.execute(f"""
        SELECT actor_key, max(actor_name) AS actor_name,
               count(DISTINCT lead_id) FILTER (WHERE action = ANY(%s)) AS leads_worked,
               coalesce(sum(qty) FILTER (WHERE action='call'), 0) AS calls,
               coalesce(sum(qty) FILTER (WHERE action='call' AND detail LIKE 'Answered%%'), 0) AS calls_answered,
               coalesce(sum(qty) FILTER (WHERE action = ANY(%s)), 0) AS messages,
               coalesce(sum(qty) FILTER (WHERE action='email_sent'), 0) AS emails,
               coalesce(sum(qty) FILTER (WHERE action='note_added'), 0) AS notes,
               coalesce(sum(qty) FILTER (WHERE action='opportunity'), 0) AS opps,
               coalesce(sum(qty) FILTER (WHERE action='proposal'), 0) AS proposals,
               coalesce(sum(qty) FILTER (WHERE action='won'), 0) AS won_count,
               -- won amounts in the business's current currency: a deal saved in
               -- another currency (before a currency change) is converted (2026-10-11)
               coalesce(sum(fx_to_selling(value, (SELECT l.currency FROM merchant_pipeline_leads l
                                                   WHERE l.id = staff_activity.lead_id), tenant_id))
                        FILTER (WHERE action='won'), 0) AS won_value,
               count(*) FILTER (WHERE action = ANY(%s)) AS work_rows
        FROM staff_activity
        WHERE tenant_id=%s AND (created_at AT TIME ZONE %s)::date BETWEEN %s AND %s
        GROUP BY actor_key""",
                (list(WORK_ACTIONS), list(MESSAGE_ACTIONS), list(WORK_ACTIONS), tenant_id, tzn, start, end))
    return {r["actor_key"]: r for r in cur.fetchall()}


def _presence(cur, tenant_id, start, end):
    cur.execute("""SELECT actor_key, max(actor_name) AS actor_name, count(*) AS days,
                          min(first_seen) AS first_seen, max(last_seen) AS last_seen,
                          sum(active_minutes) AS active_minutes, max(longest_gap_min) AS longest_gap
                   FROM staff_presence WHERE tenant_id=%s AND day BETWEEN %s AND %s
                   GROUP BY actor_key""", (tenant_id, start, end))
    return {r["actor_key"]: r for r in cur.fetchall()}


def _assigned(cur, tenant_id):
    """Open leads per assigned person, and how many have no recorded contact."""
    cur.execute("""
        SELECT l.assigned_key, count(*) AS assigned,
               count(*) FILTER (WHERE NOT EXISTS (
                   SELECT 1 FROM staff_activity a WHERE a.tenant_id=l.tenant_id AND a.lead_id=l.id
                     AND a.action <> 'leads_assigned')) AS untouched
        FROM merchant_pipeline_leads l
        WHERE l.tenant_id=%s AND l.assigned_key IS NOT NULL AND l.assigned_key <> ''
          AND l.outcome IS NULL
        GROUP BY l.assigned_key""", (tenant_id,))
    return {r["assigned_key"]: r for r in cur.fetchall()}


def _replies(cur, tenant_id, start, end, tzn):
    """Customer replies (email + WhatsApp) on each person's assigned leads."""
    out = {}
    cur.execute(f"""
        SELECT l.assigned_key, count(*) AS n FROM email_replies e
        JOIN merchant_pipeline_leads l ON l.id = e.lead_id
        WHERE e.tenant_id=%s AND l.assigned_key IS NOT NULL
          AND (e.received_at AT TIME ZONE %s)::date BETWEEN %s AND %s
        GROUP BY l.assigned_key""", (tenant_id, tzn, start, end))
    for r in cur.fetchall():
        out[r["assigned_key"]] = out.get(r["assigned_key"], 0) + int(r["n"])
    cur.execute(f"""
        SELECT l.assigned_key, count(DISTINCT (m.customer_phone, (m.created_at AT TIME ZONE %s)::date)) AS n
        FROM wa_message_log m
        JOIN merchant_pipeline_leads l ON l.tenant_id = m.tenant_id
             AND l.assigned_key IS NOT NULL AND l.whatsapp_number IS NOT NULL AND l.whatsapp_number <> ''
             AND regexp_replace(l.whatsapp_number, '[^0-9]', '', 'g') = regexp_replace(m.customer_phone, '[^0-9]', '', 'g')
        WHERE m.tenant_id=%s AND m.direction IN ('in', 'inbound') AND NOT coalesce(m.is_historical, false)
          AND (m.created_at AT TIME ZONE %s)::date BETWEEN %s AND %s
        GROUP BY l.assigned_key""", (tzn, tenant_id, tzn, start, end))
    for r in cur.fetchall():
        out[r["assigned_key"]] = out.get(r["assigned_key"], 0) + int(r["n"])
    return out


def _daily_worked(cur, tenant_id, days, tzn):
    cur.execute("""
        SELECT actor_key, (created_at AT TIME ZONE %s)::date AS d, count(DISTINCT lead_id) AS n
        FROM staff_activity
        WHERE tenant_id=%s AND action = ANY(%s)
          AND (created_at AT TIME ZONE %s)::date = ANY(%s)
        GROUP BY 1, 2""", (tzn, tenant_id, list(WORK_ACTIONS), tzn, days))
    out = {}
    for r in cur.fetchall():
        out.setdefault(r["actor_key"], {})[r["d"]] = int(r["n"])
    return out


def get_targets(cur, tenant_id, month_first):
    cur.execute("SELECT actor_key, opp_target, won_target FROM staff_targets WHERE tenant_id=%s AND month=%s",
                (tenant_id, month_first))
    return {r["actor_key"]: r for r in cur.fetchall()}


def _owner_names(cur, tenant_id):
    cur.execute("SELECT id, first_name, last_name FROM customers WHERE tenant_id=%s", (tenant_id,))
    return {f"owner:{r['id']}": (f"{(r['first_name'] or '').strip()} {(r['last_name'] or '').strip()}".strip() or "Owner")
            for r in cur.fetchall()}


def _initials(name):
    parts = [p for p in (name or "").split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def build(tenant_id: int, viewer: dict, period_key: str) -> dict:
    tz = tz_for(tenant_id)
    tzn = tz.key
    key, start, end, pstart, pend, label, vs = resolve_period(period_key, tz)
    now = now_local(tz)
    today = now.date()
    month_first = today.replace(day=1)
    scope, people_rows = viewer_scope(tenant_id, viewer)
    team_by_key = {f"team:{r['id']}": r for r in people_rows}

    conn = get_db_connection()
    try:
        cur = _cur(conn)
        act = _activity_totals(cur, tenant_id, start, end, tzn)
        prev = _activity_totals(cur, tenant_id, pstart, pend, tzn)
        pres = _presence(cur, tenant_id, start, end)
        assigned = _assigned(cur, tenant_id)
        replies = _replies(cur, tenant_id, start, end, tzn)
        month_act = act if key == "month" else _activity_totals(cur, tenant_id, month_first, today, tzn)
        targets = get_targets(cur, tenant_id, month_first)
        owners = _owner_names(cur, tenant_id)
        spark_days, d = [], end
        while len(spark_days) < 7:
            if is_workday(d):
                spark_days.append(d)
            d -= _dt.timedelta(days=1)
        spark_days.reverse()
        daily = _daily_worked(cur, tenant_id, spark_days, tzn)
        unanswered = _unanswered_replies(cur, tenant_id)
    finally:
        conn.close()

    # Who is listed: anyone with open leads assigned, or real sales work in
    # the period (presence alone doesn't list someone).
    keys = set(assigned) | {k for k, r in act.items() if r["work_rows"]}
    keys = {k for k in keys if (k in team_by_key and team_by_key[k]["is_active"]) or k in owners}
    if scope is not None:
        keys &= scope
    rec_start = max(start, RECORDING_STARTED)
    work_days_in_period = working_days(rec_start, end) if end >= RECORDING_STARTED else 0
    before_recording = end < RECORDING_STARTED
    month_wd_total = working_days(month_first, _month_end(month_first))
    month_wd_done = working_days(month_first, today)
    pace = (month_wd_done / month_wd_total) if month_wd_total else 0

    people = []
    for k in keys:
        tm = team_by_key.get(k)
        name = (tm["name"] if tm else owners.get(k)) or (act.get(k) or {}).get("actor_name") or k
        a = act.get(k) or {}
        p = pres.get(k)
        asg = assigned.get(k) or {}
        t = targets.get(k)
        m_opps = int((month_act.get(k) or {}).get("opps") or 0)
        m_won = float((month_act.get(k) or {}).get("won_value") or 0)
        person = {
            "key": k, "name": name, "initials": _initials(name),
            "role": (tm["role_name"] if tm else "Owner") or "Team member",
            "manager": tm["manager_name"] if tm else None,
            "leads_worked": int(a.get("leads_worked") or 0),
            "calls": int(a.get("calls") or 0), "calls_answered": int(a.get("calls_answered") or 0),
            "messages": int(a.get("messages") or 0), "emails": int(a.get("emails") or 0),
            "notes": int(a.get("notes") or 0),
            "replies": int(replies.get(k, 0)),
            "opps": int(a.get("opps") or 0), "proposals": int(a.get("proposals") or 0),
            "won_count": int(a.get("won_count") or 0), "won_value": float(a.get("won_value") or 0),
            "assigned": int(asg.get("assigned") or 0), "untouched": int(asg.get("untouched") or 0),
            "month_opps": m_opps, "month_won": m_won,
            "opp_target": int(t["opp_target"]) if t and t["opp_target"] else None,
            "won_target": float(t["won_target"]) if t and t["won_target"] else None,
            "spark": [daily.get(k, {}).get(dd, 0) if dd >= RECORDING_STARTED else None for dd in spark_days],
            "first_seen": p["first_seen"].astimezone(tz) if p else None,
            "last_seen": p["last_seen"].astimezone(tz) if p else None,
            "days_present": int(p["days"]) if p else 0,
            "active_minutes": int(p["active_minutes"] or 0) if p else 0,
            "longest_gap": int(p["longest_gap"] or 0) if p else 0,
            "can_set_target": _can_target(viewer, k, scope),
        }
        if person["opp_target"]:
            person["opp_pct"] = min(100, round(m_opps / person["opp_target"] * 100))
            person["on_pace"] = m_opps >= person["opp_target"] * pace
            person["pace_should_be"] = round(person["opp_target"] * pace)
        if person["won_target"]:
            person["won_pct"] = min(100, round(m_won / person["won_target"] * 100))
        person["status"] = (("Not recorded", "n", "before recording started") if before_recording
                            else _status(person, key, start, end, now, work_days_in_period))
        people.append(person)
    people.sort(key=lambda x: (-x["leads_worked"], -x["assigned"], x["name"]))

    def tot(field, src):
        return sum(int(src.get(k, {}).get(field) or 0) for k in keys)

    totals = {
        "people": len(people),
        "present": sum(1 for x in people if x["days_present"]),
        "leads_worked": sum(x["leads_worked"] for x in people),
        "calls": sum(x["calls"] for x in people),
        "opps": sum(x["opps"] for x in people),
        "won_value": sum(x["won_value"] for x in people),
        "prev_leads_worked": tot("leads_worked", prev), "prev_calls": tot("calls", prev),
        "prev_opps": tot("opps", prev),
        "prev_won_value": sum(float(prev.get(k, {}).get("won_value") or 0) for k in keys),
    }
    return {
        "period_key": key, "period_label": label, "vs": vs, "start": start, "end": end,
        "people": people, "totals": totals, "spark_days": spark_days,
        "attention": _attention(people, key, now, pace, unanswered, keys, tz),
        "before_recording": before_recording, "partly_before": start < RECORDING_STARTED <= end,
        "prev_recorded": pend >= RECORDING_STARTED,
        "pace_pct": round(pace * 100), "month_first": month_first,
        "recording_started": RECORDING_STARTED,
        "updated_at": now, "tz_label": tz_label(tz),
    }


def _can_target(viewer, key, scope):
    if not viewer.get("is_team"):
        return True
    return key in (scope or set()) and key != viewer.get("key")


def _hm(minutes):
    h, m = divmod(int(minutes or 0), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _status(p, key, start, end, now, wd):
    """(words, colour, sub-line) — colour g/a/r/n like the portal's pills."""
    if key == "today":
        if not p["last_seen"]:
            late = is_workday(now.date()) and now.hour >= LOGIN_BY_HOUR
            return ("Not in today", "r" if late else "n", "no login yet")
        since = (now - p["last_seen"]).total_seconds() / 60
        sub = f"{p['first_seen']:%H:%M} – {p['last_seen']:%H:%M}"
        if since <= 15:
            return ("Active now", "g", sub)
        if since <= 60:
            return (f"Active {p['last_seen']:%H:%M}", "g", sub)
        in_hours = WORK_START <= now.hour < WORK_END
        return (f"Idle since {p['last_seen']:%H:%M}", "r" if in_hours and since >= IDLE_ALERT_MIN else "a", sub)
    if key == "yesterday":
        if not p["last_seen"]:
            return ("Not in", "r" if is_workday(start) else "n", "no login")
        return (f"Active {_hm(p['active_minutes'])}", "g" if p["active_minutes"] >= 360 else "a",
                f"{p['first_seen']:%H:%M} – {p['last_seen']:%H:%M}")
    days = p["days_present"]
    avg = _hm(p["active_minutes"] / days) if days else "0m"
    if not wd:
        return (f"{days} day{'s' if days != 1 else ''}" if days else "No working days yet", "g" if days else "n",
                f"avg {avg} active a day" if days else "recorded so far")
    colour = "g" if days >= wd else ("a" if days else "r")
    return (f"{days} of {wd} days", colour, f"avg {avg} active a day" if days else "no logins")


def _unanswered_replies(cur, tenant_id):
    """Customer replies older than a day with no action on that lead since."""
    cur.execute("""
        SELECT l.assigned_key, l.id AS lead_id, l.customer_name, max(e.received_at) AS at
        FROM email_replies e JOIN merchant_pipeline_leads l ON l.id = e.lead_id
        WHERE e.tenant_id=%s AND l.assigned_key IS NOT NULL AND l.outcome IS NULL
          AND e.received_at BETWEEN NOW() - INTERVAL '14 days' AND NOW() - INTERVAL '1 day'
          AND e.received_at >= %s
          AND NOT EXISTS (SELECT 1 FROM staff_activity a WHERE a.tenant_id=e.tenant_id AND a.lead_id=l.id
                            AND a.created_at > e.received_at AND a.action <> 'email_sent')
        GROUP BY 1, 2, 3 ORDER BY at""", (tenant_id, RECORDING_STARTED))
    return cur.fetchall()


def _attention(people, key, now, pace, unanswered, keys, tz):
    out = []
    by_key = {p["key"]: p for p in people}
    for p in people:
        first = p["name"].split()[0]
        if key == "today" and p["assigned"] and is_workday(now.date()):
            if not p["last_seen"] and now.hour >= LOGIN_BY_HOUR:
                out.append(("r", f"<b>{p['name']}</b> hasn't logged in today.", p["key"], "Open their day"))
            elif p["last_seen"] and WORK_START <= now.hour < WORK_END:
                idle = (now - p["last_seen"]).total_seconds() / 60
                if idle >= IDLE_ALERT_MIN:
                    out.append(("r", f"<b>{p['name']}</b> has had no activity since {p['last_seen']:%H:%M} ({_hm(idle)}).",
                                p["key"], "Open their day"))
        if p.get("opp_target") and not p.get("on_pace"):
            out.append(("a", f"<b>{p['name']}</b> is behind their month target: "
                             f"{p['month_opps']} of {p['opp_target']} opportunities, where {p['pace_should_be']} would be on pace.",
                        p["key"], "See target"))
        if p["untouched"]:
            out.append(("a", f"<b>{first}</b> has {p['untouched']:,} assigned open leads with no recorded contact"
                             f" since recording started.", "leads:" + p["key"], "See leads"))
    for r in unanswered:
        if r["assigned_key"] in keys:
            who = by_key.get(r["assigned_key"], {}).get("name", "")
            out.append(("a", f"<b>{r['customer_name']}</b> replied to {who.split()[0] if who else 'us'} on "
                             f"{r['at'].astimezone(tz):%a %-d %b} and nobody has followed up yet.",
                        f"lead:{r['lead_id']}", "Open lead"))
    out.sort(key=lambda x: x[0] != "r")
    return out


# ── one person's day ────────────────────────────────────────────────────────

ACTION_WORDS = {
    "call": "Call", "wa_sent": "WhatsApp sent", "messenger_sent": "Messenger reply sent",
    "webchat_sent": "Web chat reply sent", "email_sent": "Email sent", "seq_added": "Added to an email sequence",
    "note_added": "Note added", "lead_created": "New lead", "lead_edited": "Lead details edited",
    "opportunity": "Marked as opportunity", "proposal": "Proposal sent", "won": "Won", "lost": "Lost",
    "not_a_fit": "Marked Not a Fit", "reopened": "Reopened", "stage_moved": "Stage moved",
    "chat_claimed": "Took over a chat", "reply_logged": "Marked as replied",
    "contact_created": "New contact", "contact_edited": "Contact edited", "company_created": "New company",
    "leads_assigned": "Assigned leads",
}
ACTION_ICONS = {"call": "CL", "wa_sent": "WA", "messenger_sent": "FB", "webchat_sent": "WC", "email_sent": "EM",
                "seq_added": "SQ", "note_added": "NT", "lead_created": "LD", "lead_edited": "ED",
                "opportunity": "OP", "proposal": "PR", "won": "₦", "lost": "LS", "not_a_fit": "NF",
                "reopened": "RO", "stage_moved": "ST", "chat_claimed": "CH", "reply_logged": "RE",
                "contact_created": "CT", "contact_edited": "CT", "company_created": "CO", "leads_assigned": "AS"}


def person_day(tenant_id: int, actor_key: str, day) -> dict:
    tz = tz_for(tenant_id)
    conn = get_db_connection()
    try:
        cur = _cur(conn)
        cur.execute(f"""
            SELECT a.*, l.customer_name, l.contact_person, c.name AS company_name
            FROM staff_activity a
            LEFT JOIN merchant_pipeline_leads l ON l.id = a.lead_id
            LEFT JOIN crm_companies c ON c.id = a.company_id
            WHERE a.tenant_id=%s AND a.actor_key=%s AND (a.created_at AT TIME ZONE %s)::date = %s
            ORDER BY a.created_at""", (tenant_id, actor_key, tz.key, day))
        rows = cur.fetchall()
        cur.execute("SELECT * FROM staff_presence WHERE tenant_id=%s AND actor_key=%s AND day=%s",
                    (tenant_id, actor_key, day))
        pres = cur.fetchone()
        cur.execute("""SELECT count(*) AS assigned FROM merchant_pipeline_leads
                       WHERE tenant_id=%s AND assigned_key=%s AND outcome IS NULL""", (tenant_id, actor_key))
        assigned = cur.fetchone()["assigned"]
    finally:
        conn.close()
    events, last_t = [], (pres["first_seen"].astimezone(tz) if pres else None)
    if pres:
        events.append({"t": last_t, "icon": "IN", "kind": "in", "title": "First seen in the portal", "lead_id": None})
    counts = {}
    for r in rows:
        t = r["created_at"].astimezone(tz)
        if last_t and (t - last_t).total_seconds() >= 3600:
            events.append({"t": last_t, "icon": "··", "kind": "gap",
                           "title": f"No recorded activity for {_hm((t - last_t).total_seconds() / 60)}",
                           "lead_id": None})
        last_t = t
        counts[r["action"]] = counts.get(r["action"], 0) + int(r["qty"] or 1)
        title = ACTION_WORDS.get(r["action"], r["action"])
        note = None
        if r["action"] == "call" and r["detail"]:
            # "Answered · 6 min" + newline + what was said (lead_log_call)
            head, _, note = r["detail"].partition("\n")
            title = f"Call · {head}"
        if (r["qty"] or 1) > 1:
            title = f"{title} × {r['qty']:,}"
        who = r["customer_name"] or r["company_name"]
        extra = None
        if r["action"] == "won" and r["value"]:
            extra = f"₦{float(r['value']):,.0f}"
        elif r["action"] not in ("call",) and r["detail"] and "→" not in (r["detail"] or "") and r["detail"] != "bulk":
            extra = r["detail"]
        events.append({"t": t, "icon": ACTION_ICONS.get(r["action"], "··"),
                       "kind": "won" if r["action"] == "won" else "act",
                       "title": title, "who": who, "contact": r["contact_person"], "extra": extra,
                       "note": note or None,
                       "lead_id": r["lead_id"]})
    if pres and last_t:
        lst = pres["last_seen"].astimezone(tz)
        if (lst - last_t).total_seconds() >= 3600:
            events.append({"t": last_t, "icon": "··", "kind": "gap",
                           "title": f"In the portal but no recorded activity for {_hm((lst - last_t).total_seconds() / 60)}",
                           "lead_id": None})
        events.append({"t": lst, "icon": "OUT", "kind": "in", "title": "Last seen in the portal", "lead_id": None})
    worked = len({r["lead_id"] for r in rows if r["lead_id"] and r["action"] in WORK_ACTIONS})
    return {"events": events, "presence": pres, "counts": counts, "leads_worked": worked, "assigned": assigned,
            "first_seen": pres["first_seen"].astimezone(tz) if pres else None,
            "last_seen": pres["last_seen"].astimezone(tz) if pres else None}


# ── writes ──────────────────────────────────────────────────────────────────

def save_targets(tenant_id: int, month_first, rows: list, set_by: str) -> int:
    """rows = [(actor_key, opp_target or None, won_target or None)]."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for k, opp, won in rows:
            cur.execute("""INSERT INTO staff_targets (tenant_id, actor_key, month, opp_target, won_target, set_by, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,NOW())
                           ON CONFLICT (tenant_id, actor_key, month) DO UPDATE SET
                               opp_target=EXCLUDED.opp_target, won_target=EXCLUDED.won_target,
                               set_by=EXCLUDED.set_by, updated_at=NOW()""",
                        (tenant_id, k, month_first, opp, won, set_by))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def lead_calls(tenant_id: int, lead_id: int) -> list:
    tz = tz_for(tenant_id)
    conn = get_db_connection()
    try:
        cur = _cur(conn)
        cur.execute("""SELECT * FROM lead_calls WHERE tenant_id=%s AND lead_id=%s ORDER BY created_at DESC LIMIT 100""",
                    (tenant_id, lead_id))
        rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        r["outcome_label"] = CALL_OUTCOME_LABELS.get(r["outcome"], r["outcome"])
        r["local_at"] = r["created_at"].astimezone(tz)
    return rows


def log_call(tenant_id: int, lead_id: int, actor: dict, outcome: str, minutes, note: str) -> None:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO lead_calls (tenant_id, lead_id, actor_key, actor_name, outcome, minutes, note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, lead_id, actor["key"], actor["label"], outcome, minutes, note or None))
        cur.execute("UPDATE merchant_pipeline_leads SET updated_at=NOW() WHERE id=%s AND tenant_id=%s",
                    (lead_id, tenant_id))
        conn.commit()
    finally:
        conn.close()
