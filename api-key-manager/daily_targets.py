"""Daily Targets — CRM › Daily Targets (user 2026-10-24, approved mock-up
https://claude.ai/artifact/P198UJSJGn1qoqHf42928P).

One daily target per person: **Leads contacted**. A lead counts once a day
when the person reached it themselves, whichever way: a call logged, a
WhatsApp / Messenger / web chat sent, a LinkedIn request or message, an email
they sent, or a WhatsApp / email / meeting logged by hand. Automatic sequence
emails and bulk stage moves never count. Read from staff_activity (the same
"contacted personally" rule as Team performance, plus emails a person sends
themselves). Nobody fills anything in for it.

Targets are kept with the day they start (daily_targets.effective_from), so a
changed number never rewrites earlier days. Mon–Fri are working days, in the
business's own time zone.
"""
import datetime as _dt

import team_performance as _tp
from db import get_db_connection

# Ticks (Roles › CRM)
OWN, TEAM, SET = "crm.targets_own", "crm.targets_team", "crm.targets_set"

DAILY_CONTACT_SQL = f"({_tp.CONTACT_SQL} OR (NOT auto AND action = 'email_sent'))"

# How a business was reached, for the split under today's number.
CHANNELS = [
    ("calls", "Calls", ("call",)),
    ("whatsapp", "WhatsApp", ("wa_sent", "wa_logged", "messenger_sent", "webchat_sent")),
    ("linkedin", "LinkedIn", ("linkedin",)),
    ("email", "Email", ("email_sent", "email_logged")),
    ("other", "Meeting / reply", ("meeting_logged", "reply_logged")),
]


def _a(sql: str) -> str:
    """DAILY_CONTACT_SQL for a table aliased a."""
    return sql.replace("auto", "a.auto").replace("action", "a.action").replace("detail", "a.detail")


def week_days(day):
    """Mon–Fri of day's week."""
    mon = day - _dt.timedelta(days=day.weekday())
    return [mon + _dt.timedelta(days=i) for i in range(5)]


def targets_for(cur, tenant_id: int, keys, days) -> dict:
    """{actor_key: {day: target}} — the target that was in force on each day."""
    if not days:
        return {}
    cur.execute("""SELECT actor_key, effective_from, target FROM daily_targets
                   WHERE tenant_id=%s AND (%s::text[] IS NULL OR actor_key = ANY(%s)) AND effective_from <= %s
                   ORDER BY actor_key, effective_from""",
                (tenant_id, None if keys is None else list(keys), None if keys is None else list(keys), max(days)))
    rows = {}
    for r in cur.fetchall():
        rows.setdefault(r["actor_key"], []).append((r["effective_from"], r["target"]))
    out = {}
    for k, hist in rows.items():
        for d in days:
            t = None
            for start, val in hist:
                if start <= d:
                    t = val
            if t:
                out.setdefault(k, {})[d] = int(t)
    return out


def current_targets(cur, tenant_id: int) -> dict:
    """{actor_key: {"target", "set_by", "updated_at"}} — the latest row per person."""
    cur.execute("""SELECT DISTINCT ON (actor_key) actor_key, target, set_by, updated_at FROM daily_targets
                   WHERE tenant_id=%s ORDER BY actor_key, effective_from DESC""", (tenant_id,))
    return {r["actor_key"]: r for r in cur.fetchall() if r["target"]}


def counts(cur, tenant_id: int, keys, days, tzn) -> dict:
    """{actor_key: {day: leads contacted}}."""
    cur.execute(f"""
        SELECT actor_key, (created_at AT TIME ZONE %s)::date AS d, count(DISTINCT {_tp.BIZ_SQL}) AS n
        FROM staff_activity
        WHERE tenant_id=%s AND (%s::text[] IS NULL OR actor_key = ANY(%s)) AND {DAILY_CONTACT_SQL}
          AND (created_at AT TIME ZONE %s)::date = ANY(%s)
        GROUP BY 1, 2""", (tzn, tenant_id, None if keys is None else list(keys), None if keys is None else list(keys),
                           tzn, list(days)))
    out = {}
    for r in cur.fetchall():
        out.setdefault(r["actor_key"], {})[r["d"]] = int(r["n"])
    return out


def channel_split(cur, tenant_id: int, keys, day, tzn) -> dict:
    """{actor_key: {"calls": n, ...}} — businesses reached each way on day (one
    business reached two ways shows under both; the total still counts it once)."""
    sel = ", ".join(f"count(DISTINCT {_tp.BIZ_SQL}) FILTER (WHERE action = ANY(%s)) AS {c}" for c, _l, _acts in CHANNELS)
    cur.execute(f"""
        SELECT actor_key, {sel}
        FROM staff_activity
        WHERE tenant_id=%s AND (%s::text[] IS NULL OR actor_key = ANY(%s)) AND {DAILY_CONTACT_SQL}
          AND (created_at AT TIME ZONE %s)::date = %s
        GROUP BY 1""", [list(acts) for _c, _l, acts in CHANNELS]
                       + [tenant_id, None if keys is None else list(keys), None if keys is None else list(keys), tzn, day])
    return {r["actor_key"]: {c: int(r[c]) for c, _l, _a2 in CHANNELS} for r in cur.fetchall()}


def contacted(tenant_id: int, actor_key: str, day) -> list:
    """The businesses actor_key reached on day, first contact first:
    [{business, person, lead_id, first (local time), what: [...]}]."""
    groups = _tp.contacted_list(tenant_id, {actor_key}, day, day, contact_sql=_a(DAILY_CONTACT_SQL))
    return groups[0]["items"] if groups else []


def status(n: int, target):
    """("reached" | "behind" | "none", words)."""
    if not target:
        return "none", "No target set"
    if n >= target:
        return "reached", "Reached"
    return "behind", f"{target - n} to go"


def _people(cur, tenant_id: int, viewer: dict, scope, rows):
    """People on the team view: active staff the viewer may see who have open
    leads or a target (the owner sees every such person)."""
    cur.execute("""SELECT assigned_key, count(*) AS n FROM merchant_pipeline_leads
                   WHERE tenant_id=%s AND assigned_key LIKE 'team:%%' AND outcome IS NULL AND dropped_at IS NULL
                   GROUP BY 1""", (tenant_id,))
    with_leads = {r["assigned_key"] for r in cur.fetchall()}
    targets = current_targets(cur, tenant_id)
    out = []
    for r in rows:
        k = f"team:{r['id']}"
        if not r["is_active"] or (scope is not None and k not in scope):
            continue
        if k not in with_leads and k not in targets:
            continue
        out.append({"key": k, "name": r["name"], "role": r["role_name"] or "", "initials": _tp._initials(r["name"]),
                    "manager": r["manager_name"]})
    out.sort(key=lambda p: (p["name"] or "").lower())
    return out


def build(tenant_id: int, viewer: dict, day=None, who: str = "me", person_key: str = None) -> dict:
    """Everything the page shows. who = "me" (one person's page: the viewer's
    own, or person_key's when a manager opens it) or "team"."""
    tz = _tp.tz_for(tenant_id)
    today = _tp.now_local(tz).date()
    day = day or today
    days = week_days(day)
    last_week = [d - _dt.timedelta(days=7) for d in days]
    conn = get_db_connection()
    try:
        cur = _tp._cur(conn)
        scope, rows = _tp.viewer_scope(tenant_id, viewer)
        if who == "team":
            people = _people(cur, tenant_id, viewer, scope, rows)
        else:
            key = person_key or viewer["key"]
            me = next((r for r in rows if f"team:{r['id']}" == key), None)
            name = (me or {}).get("name") or (viewer["label"] if key == viewer["key"] else key)
            people = [{"key": key, "name": name, "role": (me or {}).get("role_name") or "",
                       "initials": _tp._initials(name), "manager": (me or {}).get("manager_name")}]
        keys = [p["key"] for p in people]
        all_days = sorted(set(days) | set(last_week) | {day})
        n_by = counts(cur, tenant_id, keys, all_days, tz.key) if keys else {}
        t_by = targets_for(cur, tenant_id, keys, all_days) if keys else {}
        split = channel_split(cur, tenant_id, keys, day, tz.key) if keys else {}
        cur_t = current_targets(cur, tenant_id)
    finally:
        conn.close()
    for p in people:
        k = p["key"]
        p["count"] = n_by.get(k, {}).get(day, 0)
        p["target"] = t_by.get(k, {}).get(day)
        p["pct"] = min(100, round(p["count"] / p["target"] * 100)) if p["target"] else 0
        p["state"], p["state_words"] = status(p["count"], p["target"])
        p["split"] = split.get(k, {c: 0 for c, _l, _a2 in CHANNELS})
        p["week"] = []
        for d in days:
            n, t = n_by.get(k, {}).get(d, 0), t_by.get(k, {}).get(d)
            p["week"].append({"day": d, "count": n, "target": t, "future": d > today, "is_day": d == day,
                              "reached": bool(t) and n >= t, "before_recording": d < _tp.RECORDING_STARTED})
        p["week_on"] = sum(1 for w in p["week"] if w["reached"] and not w["future"])
        p["week_worked"] = sum(1 for w in p["week"] if w["target"] and not w["future"])
        p["last_week_on"] = sum(1 for d in last_week if t_by.get(k, {}).get(d) and n_by.get(k, {}).get(d, 0) >= t_by[k][d])
        p["last_week_days"] = sum(1 for d in last_week if t_by.get(k, {}).get(d))
        ct = cur_t.get(k)
        p["current_target"] = int(ct["target"]) if ct else None
        p["set_by"] = ct["set_by"] if ct else None
        p["can_set"] = can_set(viewer, k, scope)
    with_t = [p for p in people if p["target"]]
    return {
        "day": day, "today": today, "is_today": day == today, "workday": _tp.is_workday(day),
        "prev_day": _prev(day), "next_day": (min(_next(day), today) if day < today else None),
        "days": days, "people": people, "tz_label": _tp.tz_label(tz),
        "total": sum(p["count"] for p in people), "total_target": sum(p["target"] for p in with_t),
        "on_target": sum(1 for p in with_t if p["state"] == "reached"), "with_target": len(with_t),
        "channels": [(c, label) for c, label, _acts in CHANNELS],
        "before_recording": day < _tp.RECORDING_STARTED,
    }


def _prev(d):
    d -= _dt.timedelta(days=1)
    while not _tp.is_workday(d):
        d -= _dt.timedelta(days=1)
    return d


def _next(d):
    d += _dt.timedelta(days=1)
    while not _tp.is_workday(d):
        d += _dt.timedelta(days=1)
    return d


def can_set(viewer: dict, key: str, scope) -> bool:
    """Owner: anyone. Staff with the Set tick: the people they can see, never themselves."""
    if not viewer.get("is_team"):
        return True
    return bool(viewer.get("can_set")) and scope is not None and key in scope and key != viewer.get("key")


def save(tenant_id: int, rows: list, set_by: str, today) -> int:
    """rows = [(actor_key, target or None)]. A new number starts today and
    leaves earlier days alone; blank = no target from today."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        n = 0
        for key, target in rows:
            cur.execute("""INSERT INTO daily_targets (tenant_id, actor_key, effective_from, target, set_by, updated_at)
                           VALUES (%s,%s,%s,%s,%s,NOW())
                           ON CONFLICT (tenant_id, actor_key, effective_from) DO UPDATE SET
                               target=EXCLUDED.target, set_by=EXCLUDED.set_by, updated_at=NOW()""",
                        (tenant_id, key, today, target, set_by))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()
