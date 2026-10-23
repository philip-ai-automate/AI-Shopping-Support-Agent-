"""KeyLand "Needs attention", staff alerts and the morning summary (Phase 4).

Needs attention (always on, no switch): open leads that are slipping, most
urgent first:
  ready     Ready buyer nobody has contacted for N hours (agency sets N, 2)
  chat      AI hand-over still waiting for a person after N minutes (30)
  visit     inspection / viewing in the next 2 days, not confirmed since booking
  overdue   next action past its due time
  noaction  open lead with no next action

Alerts (optional, switched on per agency in Automation): the assigned agent and
the managers (owner + admins) are told at the same moment. Unassigned leads go
to every agent with alerts on, plus the managers.
  handoff   AI hands a buyer over: straight away, one reminder after N minutes
  ready     Ready buyer not contacted after N hours, one reminder after N more
Only real action stops an alert: a logged contact or call, a next action marked
done, the hand-over marked handled, or the lead closed (Allocated / Lost).
Opening the lead doesn't count. Overdue and "no next action" are never alerted
one by one; they go into the panel and the morning summary.

Channels per person (Team page): email (login email), WhatsApp (their own
number, sent from PhiXtra's alert number by the WhatsApp gateway) and phone
notifications (devices they turned on in My alerts, also sent by the gateway).
No SMS.

Tables (new, created once):
  re_alert_settings  per agency: switch, limits, hours, morning summary
  re_alert_people    per person: alerts on, WhatsApp number, channels
                     (staff_id 0 = the account owner)
  re_alerts          each alert: reason, who was told, reminder, what stopped it
  re_push_devices    phones / computers with phone notifications on
Attended visits are bookings marked Attended (status 'completed') on the Inspections page.
"""
import html as _html
import json
import os
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

import psycopg2.extras

from db import get_db_connection
import estate_actions as EA

PORTAL = "https://home.phixtra.com"
GATEWAY = os.getenv("WA_GATEWAY_INTERNAL_URL", "http://127.0.0.1:8001")
OPEN_STAGES_SQL = EA.OPEN_STAGES_SQL
REASONS = [("ready", "Ready, not contacted"), ("chat", "Chat waiting"), ("call", "Missed call"), ("visit", "Visit not confirmed"),
           ("overdue", "Action overdue"), ("noaction", "No next action")]
REASON_ORDER = {k: i for i, (k, _) in enumerate(REASONS)}
DEFAULTS = {"alerts_on": False, "alerts_on_at": None, "ready_hours": 2, "chat_minutes": 30, "always": True,
            "days": [0, 1, 2, 3, 4], "start_time": "08:00", "end_time": "18:00", "tz": "Africa/Lagos",
            "summary_on": False, "summary_time": "08:00", "summary_last": None,
            "updated_by": None, "updated_at": None}
PERSON_DEFAULTS = {"alerts_on": True, "wa_phone": None, "by_email": True, "by_wa": True, "by_push": True}
# A note the follow-up job writes itself; not a person getting in touch
SYSTEM_NOTE = "No reply to %%automatic WhatsApp follow-up%%"
MISSED_CALL = "📵%%"          # a missed WhatsApp call on the log: the buyer tried, nobody acted

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""SELECT to_regclass('re_alert_settings') IS NOT NULL, to_regclass('re_alert_people') IS NOT NULL,
                              to_regclass('re_alerts') IS NOT NULL, to_regclass('re_push_devices') IS NOT NULL""")
        if not all(cur.fetchone()):
            cur.execute("SET lock_timeout = '3s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_alert_settings (
                tenant_id INTEGER PRIMARY KEY,
                alerts_on BOOLEAN NOT NULL DEFAULT FALSE,
                alerts_on_at TIMESTAMPTZ,
                ready_hours SMALLINT NOT NULL DEFAULT 2,
                chat_minutes SMALLINT NOT NULL DEFAULT 30,
                always BOOLEAN NOT NULL DEFAULT TRUE,
                alert_days VARCHAR(20) NOT NULL DEFAULT '0,1,2,3,4',
                start_time VARCHAR(5) NOT NULL DEFAULT '08:00',
                end_time VARCHAR(5) NOT NULL DEFAULT '18:00',
                tz VARCHAR(40) NOT NULL DEFAULT 'Africa/Lagos',
                summary_on BOOLEAN NOT NULL DEFAULT FALSE,
                summary_time VARCHAR(5) NOT NULL DEFAULT '08:00',
                summary_last DATE,
                updated_by VARCHAR(120),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_alert_people (
                tenant_id INTEGER NOT NULL,
                staff_id INTEGER NOT NULL,
                alerts_on BOOLEAN NOT NULL DEFAULT TRUE,
                wa_phone VARCHAR(32),
                by_email BOOLEAN NOT NULL DEFAULT TRUE,
                by_wa BOOLEAN NOT NULL DEFAULT TRUE,
                by_push BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (tenant_id, staff_id))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_alerts (
                id BIGSERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                reason VARCHAR(10) NOT NULL,
                ref_key VARCHAR(40) NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                agent_id INTEGER,
                first_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                recipients JSONB NOT NULL DEFAULT '[]',
                reminder_at TIMESTAMPTZ,
                reminder_recipients JSONB,
                stopped_at TIMESTAMPTZ,
                stop_note TEXT,
                UNIQUE (contact_id, reason, ref_key))""")
            cur.execute("CREATE INDEX IF NOT EXISTS re_alerts_tenant ON re_alerts (tenant_id, first_at)")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_push_devices (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                staff_id INTEGER NOT NULL DEFAULT 0,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh VARCHAR(200) NOT NULL,
                auth VARCHAR(100) NOT NULL,
                device_label VARCHAR(40),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_sent_at TIMESTAMPTZ,
                fail_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT)""")
        _tables_ready = True
    except Exception as e:
        print(f"⚠️ [KL ALERTS] ensure_tables: {e}")
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc)


def _db(autocommit=False):
    ensure_tables()
    conn = get_db_connection()
    if conn and autocommit:
        conn.autocommit = True
    return conn


def _name(c) -> str:
    return (c.get("name") or "").strip() or c.get("phone_number") or "A buyer"


def wait_text(td: timedelta) -> str:
    m = max(0, int(td.total_seconds() // 60))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m:02d} min" if m else f"{h} h"
    return f"{h // 24} days"


# ── Settings ──────────────────────────────────────────────────────────────────

def settings(tenant_id: int) -> dict:
    out = dict(DEFAULTS)
    conn = _db()
    if not conn:
        return out
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM re_alert_settings WHERE tenant_id=%s", (tenant_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        out.update({k: row[k] for k in ("alerts_on", "alerts_on_at", "ready_hours", "chat_minutes", "always",
                                         "start_time", "end_time", "tz", "summary_on", "summary_time",
                                         "summary_last", "updated_by", "updated_at")})
        out["days"] = [int(d) for d in (row["alert_days"] or "").split(",") if d.strip().isdigit()]
    else:
        out["tz"] = EA.settings(tenant_id).get("tz") or DEFAULTS["tz"]
    return out


def _hm(v, fallback):
    v = (v or "")[:5]
    return v if len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() else fallback


def save(tenant_id: int, form, who: str) -> list:
    """Save the Alerts card on the Automation page. Returns problems (empty = saved)."""
    errors = []
    alerts_on = form.get("alerts_on") == "on"
    summary_on = form.get("summary_on") == "on"
    always = form.get("alert_when", "always") == "always"
    try:
        ready_hours = int(form.get("ready_hours") or 2)
        chat_minutes = int(form.get("chat_minutes") or 30)
    except ValueError:
        ready_hours, chat_minutes = 0, 0
    if not 1 <= ready_hours <= 48:
        errors.append("Ready buyer limit: choose between 1 and 48 hours.")
    if not 5 <= chat_minutes <= 240:
        errors.append("Chat waiting limit: choose between 5 and 240 minutes.")
    days = sorted({int(d) for d in form.getlist("alert_days") if d.isdigit() and 0 <= int(d) <= 6})
    start, end = _hm(form.get("alert_start"), "08:00"), _hm(form.get("alert_end"), "18:00")
    if not always:
        if not days:
            errors.append("Alert hours: tick at least one working day, or choose 24/7.")
        if start >= end:
            errors.append("Alert hours: the start time must be before the end time.")
    tz = form.get("alert_tz", "Africa/Lagos")
    if tz not in dict(EA.TIMEZONES):
        tz = "Africa/Lagos"
    summary_time = _hm(form.get("summary_time"), "08:00")
    if errors:
        return errors
    conn = _db()
    if not conn:
        return ["Database unavailable."]
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO re_alert_settings (tenant_id, alerts_on, alerts_on_at, ready_hours, chat_minutes, always,
                                       alert_days, start_time, end_time, tz, summary_on, summary_time,
                                       updated_by, updated_at)
        VALUES (%(t)s, %(on)s, CASE WHEN %(on)s THEN NOW() END, %(rh)s, %(cm)s, %(al)s, %(d)s, %(s)s, %(e)s,
                %(tz)s, %(so)s, %(st)s, %(who)s, NOW())
        ON CONFLICT (tenant_id) DO UPDATE SET
          alerts_on_at = CASE WHEN %(on)s AND NOT re_alert_settings.alerts_on THEN NOW()
                              WHEN %(on)s THEN re_alert_settings.alerts_on_at END,
          alerts_on = %(on)s, ready_hours = %(rh)s, chat_minutes = %(cm)s, always = %(al)s, alert_days = %(d)s,
          start_time = %(s)s, end_time = %(e)s, tz = %(tz)s, summary_on = %(so)s, summary_time = %(st)s,
          updated_by = %(who)s, updated_at = NOW()""",
        {"t": tenant_id, "on": alerts_on, "rh": ready_hours, "cm": chat_minutes, "al": always,
         "d": ",".join(map(str, days or [0, 1, 2, 3, 4])), "s": start, "e": end, "tz": tz,
         "so": summary_on, "st": summary_time, "who": who})
    conn.commit(); cur.close(); conn.close()
    return []


def in_hours(st: dict, when) -> bool:
    if st["always"]:
        return True
    return EA.in_window(st, when)


# ── People ────────────────────────────────────────────────────────────────────

def people(tenant_id: int, cur=None) -> list:
    """The owner (staff_id 0) and every active team member, with their alert settings."""
    own = cur is None
    conn = None
    if own:
        conn = _db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT 0 AS staff_id, 'owner' AS role, TRUE AS is_active, email,
                          TRIM(COALESCE(first_name,'') || ' ' || COALESCE(last_name,'')) AS name,
                          COALESCE(NULLIF(first_name,''), 'Owner') AS first_name
                   FROM re_tenants WHERE id=%s
                   UNION ALL
                   SELECT id, role, is_active, email, TRIM(COALESCE(first_name,'') || ' ' || COALESCE(last_name,'')),
                          first_name
                   FROM re_staff WHERE tenant_id=%s AND is_active=TRUE""", (tenant_id, tenant_id))
    rows = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM re_alert_people WHERE tenant_id=%s", (tenant_id,))
    saved = {r["staff_id"]: r for r in cur.fetchall()}
    cur.execute("SELECT staff_id, COUNT(*) AS n FROM re_push_devices WHERE tenant_id=%s GROUP BY staff_id", (tenant_id,))
    devices = {r["staff_id"]: r["n"] for r in cur.fetchall()}
    if own:
        cur.close(); conn.close()
    for r in rows:
        s = saved.get(r["staff_id"]) or {}
        for k, v in PERSON_DEFAULTS.items():
            r[k] = s.get(k, v) if s else v
        r["name"] = r["name"] or r["first_name"]
        r["devices"] = devices.get(r["staff_id"], 0)
        r["manager"] = r["role"] in ("owner", "admin")
    return rows


def person(tenant_id: int, staff_id: int) -> dict:
    for p in people(tenant_id):
        if p["staff_id"] == staff_id:
            return p
    return dict(PERSON_DEFAULTS, staff_id=staff_id, devices=0)


def save_person(tenant_id: int, staff_id: int, form, prefix=""):
    """Save one person's alert settings. Returns a problem or ''."""
    raw = (form.get(prefix + "wa_phone") or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw and (not raw.startswith("+") or not 8 <= len(digits) <= 15):
        return "Write the WhatsApp number with its country code, for example +234 803 123 4567."
    conn = _db()
    cur = conn.cursor()
    cur.execute("""INSERT INTO re_alert_people (tenant_id, staff_id, alerts_on, wa_phone, by_email, by_wa, by_push, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                   ON CONFLICT (tenant_id, staff_id) DO UPDATE SET alerts_on=EXCLUDED.alerts_on, wa_phone=EXCLUDED.wa_phone,
                     by_email=EXCLUDED.by_email, by_wa=EXCLUDED.by_wa, by_push=EXCLUDED.by_push, updated_at=NOW()""",
                (tenant_id, staff_id, form.get(prefix + "alerts_on") == "on", ("+" + digits) if digits else None,
                 form.get(prefix + "by_email") == "on", form.get(prefix + "by_wa") == "on",
                 form.get(prefix + "by_push") == "on"))
    conn.commit(); cur.close(); conn.close()
    return ""


def recipients(all_people: list, assigned_to) -> list:
    """Assigned: that agent + the managers. Unassigned: every agent + the managers.
    Only people with alerts on."""
    out = []
    for p in all_people:
        if not p["alerts_on"]:
            continue
        if p["manager"] or (assigned_to and p["staff_id"] == assigned_to) or (not assigned_to and p["role"] == "staff"):
            out.append(p)
    return out


# ── What counts as a person acting ────────────────────────────────────────────

def acted_sql(since: str, contact="c.id") -> str:
    """SQL that is true when a person has acted on the lead after `since`."""
    return f"""(EXISTS (SELECT 1 FROM re_interactions i WHERE i.contact_id = {contact} AND i.logged_at > {since}
                        AND i.summary NOT LIKE '{SYSTEM_NOTE}' AND i.summary NOT LIKE '{MISSED_CALL}')
             OR EXISTS (SELECT 1 FROM re_next_actions na WHERE na.contact_id = {contact} AND na.status = 'done'
                        AND na.done_at > {since}))"""


def first_action(cur, contact_id: int, since):
    """The first thing a person did on the lead after `since`: (when, what) or None."""
    cur.execute(f"""
        SELECT at, what FROM (
          SELECT logged_at AS at, 'logged ' || CASE type WHEN 'note' THEN 'a note' WHEN 'call' THEN 'a call'
                 WHEN 'visit' THEN 'a visit' WHEN 'meeting' THEN 'a meeting' ELSE type END AS what
          FROM re_interactions WHERE contact_id=%(c)s AND logged_at > %(s)s AND summary NOT LIKE '{SYSTEM_NOTE}'
            AND summary NOT LIKE '{MISSED_CALL}'
          UNION ALL
          SELECT done_at, 'marked "' || left(title, 60) || '" done' FROM re_next_actions
          WHERE contact_id=%(c)s AND status='done' AND done_at > %(s)s
        ) x ORDER BY at LIMIT 1""", {"c": contact_id, "s": since})
    r = cur.fetchone()
    if not r:
        return None
    return (r["at"], r["what"]) if isinstance(r, dict) else (r[0], r[1])


# ── Needs attention ───────────────────────────────────────────────────────────

_ROW_SQL = f"""
    SELECT c.id, c.name, c.phone_number, c.assigned_to, COALESCE(c.pipeline_stage,'new') AS stage,
           TRIM(COALESCE(s.first_name,'') || ' ' || COALESCE(s.last_name,'')) AS agent_name,
           bl.readiness, bl.readiness_at, bl.plot_size_sqm, bl.pay_mode,
           (bl.buyer_kind = 'land' OR lp.property_type = 'land') AS is_land,
           COALESCE(el.title, lp.title, CASE WHEN c.preferred_area IS NOT NULL THEN 'a property in ' || c.preferred_area END) AS interest,
           na.title AS action_title, na.due_at AS action_due, c.created_at
    FROM re_customers c
    LEFT JOIN re_staff s ON s.id = c.assigned_to
    LEFT JOIN re_buyer_land bl ON bl.customer_id = c.id AND bl.tenant_id = c.tenant_id
    LEFT JOIN re_property_listings el ON el.id = bl.estate_listing_id
    LEFT JOIN re_property_listings lp ON lp.id = c.linked_listing_id
    LEFT JOIN re_next_actions na ON na.contact_id = c.id AND na.status = 'open'
    WHERE c.tenant_id = %(t)s AND {OPEN_STAGES_SQL}"""


def attention(tenant_id: int, viewer_id=None, is_admin=True, agent="", now=None, st=None) -> dict:
    """Leads that need attention. Agents (is_admin False) see their own and
    unassigned leads; managers see everyone's, optionally one agent's
    (agent = staff id, or 'none' for unassigned).
    Returns {counts: {reason: n}, rows: [one per lead, most urgent first], ids: set}."""
    now = now or _now()
    st = st or settings(tenant_id)
    conn = _db()
    if not conn:
        return {"counts": {k: 0 for k, _ in REASONS}, "rows": [], "ids": set()}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    p = {"t": tenant_id, "now": now, "me": viewer_id or -1,
         "ready_since": now - timedelta(hours=int(st["ready_hours"])),
         "chat_since": now - timedelta(minutes=int(st["chat_minutes"])),
         "visit_until": now + timedelta(days=2)}
    scope = ""
    if not is_admin:
        scope = " AND (c.assigned_to = %(me)s OR c.assigned_to IS NULL)"
    elif agent == "none":
        scope = " AND c.assigned_to IS NULL"
    elif str(agent).isdigit():
        scope = " AND c.assigned_to = %(agent)s"
        p["agent"] = int(agent)
    found = {}

    def add(rows, reason, since_key, text):
        for r in rows:
            r = dict(r)
            since = r.get(since_key)
            item = {"reason": reason, "since": since, "text": text(r, since)}
            if r["id"] not in found:
                r["reasons"] = []
                found[r["id"]] = r
            found[r["id"]]["reasons"].append(item)
            for k in ("handoff_id", "slot_at"):
                if r.get(k) and not found[r["id"]].get(k):
                    found[r["id"]][k] = r[k]

    cur.execute(_ROW_SQL + scope + f""" AND bl.readiness = 'ready' AND bl.readiness_at <= %(ready_since)s
                AND NOT {acted_sql('bl.readiness_at')}""", p)
    add(cur.fetchall(), "ready", "readiness_at",
        lambda r, s: f"Ready {wait_text(now - s)}, not contacted")

    cur.execute(_ROW_SQL.replace("SELECT c.id,", "SELECT h.id AS handoff_id, h.created_at AS waited_since, c.id,", 1)
                .replace("WHERE c.tenant_id", """JOIN LATERAL (SELECT id, created_at FROM re_handoff_requests
                          WHERE customer_id = c.id AND tenant_id = c.tenant_id AND status = 'pending'
                          ORDER BY created_at LIMIT 1) h ON TRUE
                    WHERE c.tenant_id""", 1)
                + scope + f" AND h.created_at <= %(chat_since)s AND NOT {acted_sql('h.created_at')}", p)
    add(cur.fetchall(), "chat", "waited_since",
        lambda r, s: f"Chat waiting for a salesperson {wait_text(now - s)}")

    cur.execute("SELECT to_regclass('re_calls') IS NOT NULL AS ok")
    if cur.fetchone()["ok"]:
        cur.execute(_ROW_SQL.replace("SELECT c.id,", "SELECT k.called_at, c.id,", 1)
                    .replace("WHERE c.tenant_id", """JOIN LATERAL (SELECT created_at AS called_at FROM re_calls
                              WHERE customer_id = c.id AND tenant_id = c.tenant_id AND status IN ('missed','out_of_hours')
                                AND created_at > %(now)s - INTERVAL '7 days'
                              ORDER BY created_at DESC LIMIT 1) k ON TRUE
                        WHERE c.tenant_id""", 1)
                    + scope + f" AND NOT {acted_sql('k.called_at')}", p)
        add(cur.fetchall(), "call", "called_at",
            lambda r, s: f"Missed WhatsApp call {wait_text(now - s)} ago, not called back")

    cur.execute(_ROW_SQL.replace("SELECT c.id,", "SELECT b.slot_at, b.booked_at, c.id,", 1)
                .replace("WHERE c.tenant_id", """JOIN LATERAL (SELECT sl.slot_datetime AS slot_at, bk.created_at AS booked_at
                          FROM re_inspection_bookings bk JOIN re_inspection_slots sl ON sl.id = bk.slot_id
                          WHERE bk.customer_id = c.id AND bk.tenant_id = c.tenant_id AND bk.status = 'confirmed'
                            AND sl.slot_datetime > %(now)s AND sl.slot_datetime <= %(visit_until)s
                          ORDER BY sl.slot_datetime LIMIT 1) b ON TRUE
                    WHERE c.tenant_id""", 1)
                + scope + f" AND NOT {acted_sql('b.booked_at')}", p)
    tz = ZoneInfo(st["tz"]) if st.get("tz") else ZoneInfo("Africa/Lagos")
    add(cur.fetchall(), "visit", "booked_at",
        lambda r, s: f"{'Site inspection' if r.get('is_land') else 'Viewing'} {r['slot_at'].astimezone(tz):%a %H:%M} not confirmed")

    cur.execute(_ROW_SQL + scope + " AND na.due_at < %(now)s", p)
    add(cur.fetchall(), "overdue", "action_due",
        lambda r, s: f"“{r['action_title']}” was due {wait_text(now - s)} ago")

    cur.execute(_ROW_SQL + scope + " AND na.id IS NULL", p)
    add(cur.fetchall(), "noaction", "created_at", lambda r, s: "No next action")
    cur.close(); conn.close()

    counts = {k: 0 for k, _ in REASONS}
    rows = []
    for r in found.values():
        r["reasons"].sort(key=lambda x: REASON_ORDER[x["reason"]])
        for x in r["reasons"]:
            counts[x["reason"]] += 1
        top = r["reasons"][0]
        r["reason"], r["text"], r["since"] = top["reason"], top["text"], top["since"]
        r["also"] = [x["text"] for x in r["reasons"][1:]]
        rows.append(r)
    rows.sort(key=lambda r: (REASON_ORDER[r["reason"]], r["since"] or now))
    return {"counts": counts, "rows": rows, "ids": set(found)}


# ── Sending (email here; WhatsApp and phone notifications through the gateway) ─

def _gateway(path: str, payload: dict, http=None):
    import requests
    http = http or requests
    try:
        r = http.post(GATEWAY + path, json=payload, timeout=25,
                      headers={"Authorization": "Bearer " + os.getenv("PHIXTRA_INTERNAL_TOKEN", "")})
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        print(f"⚠️ [KL ALERTS] gateway {path}: {e}")
        return {}


def _email_html(business, heading, lines, url, button="Open the lead in KeyLand"):
    rows = "".join(f'<p style="margin:0 0 8px;font-size:14px;line-height:1.5">{_html.escape(l)}</p>' for l in lines)
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;color:#101828">
      <div style="background:#030C18;padding:16px 20px;border-radius:12px 12px 0 0">
        <p style="color:#25D366;font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;margin:0 0 4px">KeyLand · {_html.escape(business or '')}</p>
        <h2 style="color:#fff;margin:0;font-size:19px">{_html.escape(heading)}</h2>
      </div>
      <div style="border:1px solid #E5E7EB;border-top:none;border-radius:0 0 12px 12px;padding:18px 20px">
        {rows}
        <a href="{url}" style="display:inline-block;margin-top:8px;background:#25D366;color:#03240F;padding:11px 18px;border-radius:10px;text-decoration:none;font-weight:800">{button}</a>
        <p style="color:#98A2B3;font-size:12px;margin:18px 0 0">Your agency switched on KeyLand alerts. You can change who gets them on the Team page.</p>
      </div>
    </div>"""


def deliver(tenant_id: int, business: str, to: list, *, heading: str, line: str, assigned: str, url: str,
            tag: str, http=None, mailer=None) -> list:
    """Send one alert to these people on their channels. Returns what went where."""
    if mailer is None:
        from portal_utils import send_email as mailer
    out = []
    push_ids = []
    for p in to:
        got = []
        if p["by_email"] and p.get("email"):
            body = [line, f"Assigned to: {assigned}"]
            if mailer(p["email"], f"KeyLand: {heading}", _email_html(business, heading, body, url),
                      "\n".join(body + [url])):
                got.append("email")
        if p["by_wa"] and p.get("wa_phone"):
            res = _gateway("/internal/keyland/whatsapp",
                           {"to": p["wa_phone"], "params": [business or "your agency", line, assigned],
                            "text": f"KeyLand alert for {business}: {line} Assigned to {assigned}. {url}"}, http)
            if res.get("ok"):
                got.append("whatsapp")
        if p["by_push"] and p.get("devices"):
            push_ids.append(p["staff_id"])
        out.append({"staff_id": p["staff_id"], "name": p["name"], "got": got})
    if push_ids:
        res = _gateway("/internal/keyland/push", {"tenant_id": tenant_id, "staff_ids": push_ids,
                                                  "title": heading[:120], "body": line[:180], "url": url, "tag": tag}, http)
        reached = set(res.get("reached") or [])
        for o in out:
            if o["staff_id"] in reached:
                o["got"].append("phone")
    return out


def _fallback_email(cur, tenant_id, business, heading, line, url, mailer=None):
    """Alerts are on but nobody can receive one: the agency's main email, so nothing is missed silently."""
    if mailer is None:
        from portal_utils import send_email as mailer
    cur.execute("SELECT COALESCE(NULLIF(contact_email,''), email) AS e FROM re_tenants WHERE id=%s", (tenant_id,))
    e = (cur.fetchone() or {}).get("e")
    if e and mailer(e, f"KeyLand: {heading}", _email_html(business, heading, [line, "Nobody on your team has alerts switched on, so this came to the main email."], url), line):
        return [{"staff_id": None, "name": "Main email", "got": ["email"]}]
    return []


# ── The alert run ─────────────────────────────────────────────────────────────

def _alert_text(reason, c, st, reminder, now, started):
    n = _name(c)
    if reason == "handoff":
        if reminder:
            return (f"Reminder: {n} is still waiting",
                    f"{n} is still waiting for a salesperson after the AI handed them over {wait_text(now - started)} ago.")
        what = f" about {c['interest']}" if c.get("interest") else ""
        return f"Buyer waiting: {n}", f"The AI handed {n} over{what}. They want to speak to a person now."
    ready_for = wait_text(now - started)
    return ((f"Reminder: {n} is Ready, still not contacted" if reminder else f"Ready buyer not contacted: {n}"),
            f"{n} is Ready and nobody has contacted them for {ready_for}.")


def run_tenant(tenant_id: int, now=None, http=None, mailer=None, dry=False) -> dict:
    """Send the alerts and reminders that are due for one agency. Returns counts."""
    now = now or _now()
    st = settings(tenant_id)
    out = {"alerts": 0, "reminders": 0, "stopped": 0, "held": 0}
    if not st["alerts_on"]:
        return out
    conn = _db(autocommit=True)   # never hold a transaction while emails / WhatsApp go out
    if not conn:
        return out
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT pg_try_advisory_lock(hashtext('keyland_alerts'), %s) AS ok", (tenant_id,))
    if not cur.fetchone()["ok"]:
        cur.close(); conn.close()
        return out
    try:
        cur.execute("SELECT business_name FROM re_tenants WHERE id=%s", (tenant_id,))
        business = (cur.fetchone() or {}).get("business_name") or ""
        on_at = st["alerts_on_at"] or now

        # 1. Close alerts a person has acted on (or the lead was closed / hand-over handled)
        cur.execute("""SELECT a.*, COALESCE(c.pipeline_stage,'new') AS stage, h.status AS h_status, h.handled_at,
                              bl.readiness
                       FROM re_alerts a JOIN re_customers c ON c.id = a.contact_id
                       LEFT JOIN re_buyer_land bl ON bl.customer_id = c.id AND bl.tenant_id = c.tenant_id
                       LEFT JOIN re_handoff_requests h ON a.reason = 'handoff' AND h.id::text = a.ref_key
                       WHERE a.tenant_id=%s AND a.stopped_at IS NULL""", (tenant_id,))
        for a in cur.fetchall():
            stop = None
            act = first_action(cur, a["contact_id"], a["started_at"])
            if act:
                stop = (act[0], act[1])
            elif a["stage"] in ("allocated", "lost"):
                stop = (now, f"lead moved to {a['stage'].title()}")
            elif a["reason"] == "handoff" and a["h_status"] and a["h_status"] != "pending":
                stop = (a["handled_at"] or now, "hand-over marked handled")
            elif a["reason"] == "ready" and a["readiness"] != "ready":
                stop = (now, "no longer Ready")
            if stop:
                cur.execute("UPDATE re_alerts SET stopped_at=%s, stop_note=%s WHERE id=%s", (stop[0], stop[1], a["id"]))
                out["stopped"] += 1

        # 2. What is due now
        due = []
        cur.execute(f"""SELECT h.id AS ref, h.created_at AS started, c.id, c.name, c.phone_number, c.assigned_to,
                               COALESCE(el.title, lp.title) AS interest
                        FROM re_handoff_requests h JOIN re_customers c ON c.id = h.customer_id AND c.tenant_id = h.tenant_id
                        LEFT JOIN re_buyer_land bl ON bl.customer_id = c.id AND bl.tenant_id = c.tenant_id
                        LEFT JOIN re_property_listings el ON el.id = bl.estate_listing_id
                        LEFT JOIN re_property_listings lp ON lp.id = c.linked_listing_id
                        WHERE h.tenant_id=%s AND h.status='pending' AND h.created_at >= %s AND {OPEN_STAGES_SQL}""",
                    (tenant_id, on_at))
        for r in cur.fetchall():
            due.append(("handoff", str(r["ref"]), r, r["started"], timedelta(minutes=int(st["chat_minutes"]))))
        ready_wait = timedelta(hours=int(st["ready_hours"]))
        cur.execute(f"""SELECT bl.readiness_at AS started, c.id, c.name, c.phone_number, c.assigned_to,
                               COALESCE(el.title, lp.title) AS interest
                        FROM re_buyer_land bl JOIN re_customers c ON c.id = bl.customer_id AND c.tenant_id = bl.tenant_id
                        LEFT JOIN re_property_listings el ON el.id = bl.estate_listing_id
                        LEFT JOIN re_property_listings lp ON lp.id = c.linked_listing_id
                        WHERE bl.tenant_id=%s AND bl.readiness='ready' AND bl.readiness_at IS NOT NULL
                          AND bl.readiness_at + %s <= %s AND bl.readiness_at + %s >= %s AND {OPEN_STAGES_SQL}""",
                    (tenant_id, ready_wait, now, ready_wait, on_at))
        for r in cur.fetchall():
            due.append(("ready", r["started"].strftime("%Y%m%d%H%M%S"), r, r["started"], ready_wait))

        everyone = None
        for reason, ref, c, started, gap in due:
            if first_action(cur, c["id"], started):
                continue
            cur.execute("SELECT * FROM re_alerts WHERE contact_id=%s AND reason=%s AND ref_key=%s", (c["id"], reason, ref))
            a = cur.fetchone()
            if a and (a["stopped_at"] or a["reminder_at"]):
                continue                                   # once, plus one reminder at most
            reminder = bool(a)
            if reminder and now < a["first_at"] + gap:
                continue
            if not in_hours(st, now):
                out["held"] += 1                           # goes at the next start time
                continue
            if everyone is None:
                everyone = people(tenant_id, cur)
            to = recipients(everyone, c["assigned_to"])
            agent = next((p["name"] for p in everyone if p["staff_id"] == c["assigned_to"]), None)
            assigned = agent or "nobody yet"
            heading, line = _alert_text(reason, c, st, reminder, now, started)
            # Claim it before sending, so a second run can never send it twice
            if reminder:
                cur.execute("UPDATE re_alerts SET reminder_at=%s WHERE id=%s AND reminder_at IS NULL RETURNING id",
                            (now, a["id"]))
            else:
                cur.execute("""INSERT INTO re_alerts (tenant_id, contact_id, reason, ref_key, started_at, agent_id, first_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (contact_id, reason, ref_key) DO NOTHING
                               RETURNING id""", (tenant_id, c["id"], reason, ref, started, c["assigned_to"], now))
            claimed = cur.fetchone()
            if not claimed:
                continue
            url = f"{PORTAL}/estate/leads/{c['id']}"
            if dry:
                sent = [{"staff_id": p["staff_id"], "name": p["name"], "got": ["dry-run"]} for p in to]
            else:
                sent = deliver(tenant_id, business, to, heading=heading, line=line, assigned=assigned, url=url,
                               tag=f"keyland-{reason}-{c['id']}", http=http, mailer=mailer)
                if not any(s["got"] for s in sent):
                    sent += _fallback_email(cur, tenant_id, business, heading, line, url, mailer)
            col = "reminder_recipients" if reminder else "recipients"
            cur.execute(f"UPDATE re_alerts SET {col}=%s WHERE id=%s", (json.dumps(sent), claimed["id"]))
            out["reminders" if reminder else "alerts"] += 1
            print(f"🔔 [KL ALERTS] tenant={tenant_id} lead={c['id']} {reason}{' reminder' if reminder else ''} → "
                  + ", ".join(f"{s['name']}({'+'.join(s['got']) or 'nothing'})" for s in sent))
    finally:
        cur.execute("SELECT pg_advisory_unlock(hashtext('keyland_alerts'), %s)", (tenant_id,))
        cur.close(); conn.close()
    return out


# ── Morning summary ───────────────────────────────────────────────────────────

def summary_due(st: dict, now) -> bool:
    if not st["summary_on"]:
        return False
    loc = now.astimezone(ZoneInfo(st["tz"]))
    hh, mm = map(int, st["summary_time"].split(":"))
    at = loc.replace(hour=hh, minute=mm, second=0, microsecond=0)
    # In the 3 hours after the chosen time, once a day
    return at <= loc < at + timedelta(hours=3) and st["summary_last"] != loc.date()


def summary_data(tenant_id: int, now=None, st=None) -> dict:
    now = now or _now()
    st = st or settings(tenant_id)
    tz = ZoneInfo(st["tz"])
    loc = now.astimezone(tz)
    today0 = loc.replace(hour=0, minute=0, second=0, microsecond=0)
    yday0 = today0 - timedelta(days=1)
    att = attention(tenant_id, is_admin=True, now=now, st=st)
    conn = _db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT COALESCE(NULLIF(TRIM(COALESCE(s.first_name,'') || ' ' || COALESCE(s.last_name,'')),''), 'Unassigned') AS agent,
                          COUNT(*) AS n
                   FROM re_next_actions a JOIN re_customers c ON c.id = a.contact_id
                   LEFT JOIN re_staff s ON s.id = c.assigned_to
                   WHERE a.tenant_id=%s AND a.status='open' AND a.due_at < %s
                   GROUP BY 1 ORDER BY 2 DESC""", (tenant_id, now))
    overdue_by_agent = cur.fetchall()
    cur.execute("""SELECT c.id, c.name, c.phone_number, sl.slot_datetime, l.title AS listing, l.property_type,
                          TRIM(COALESCE(s.first_name,'') || ' ' || COALESCE(s.last_name,'')) AS agent
                   FROM re_inspection_bookings b JOIN re_inspection_slots sl ON sl.id = b.slot_id
                   JOIN re_customers c ON c.id = b.customer_id
                   LEFT JOIN re_property_listings l ON l.id = b.listing_id
                   LEFT JOIN re_staff s ON s.id = c.assigned_to
                   WHERE b.tenant_id=%s AND b.status='confirmed' AND sl.slot_datetime >= %s AND sl.slot_datetime < %s
                   ORDER BY sl.slot_datetime""", (tenant_id, today0, today0 + timedelta(days=1)))
    visits = cur.fetchall()
    cur.execute("""SELECT a.*, c.name, c.phone_number,
                          TRIM(COALESCE(s.first_name,'') || ' ' || COALESCE(s.last_name,'')) AS agent
                   FROM re_alerts a JOIN re_customers c ON c.id = a.contact_id
                   LEFT JOIN re_staff s ON s.id = a.agent_id
                   WHERE a.tenant_id=%s AND a.first_at >= %s AND a.first_at < %s ORDER BY a.first_at""",
                (tenant_id, yday0, today0))
    alerts = []
    for a in cur.fetchall():
        a = dict(a)
        a["who"] = ", ".join(r["name"] for r in (a["recipients"] or []) if r.get("got")) or "nobody reached"
        a["took"] = wait_text(a["stopped_at"] - a["first_at"]) if a["stopped_at"] else None
        a["late"] = bool(a["reminder_at"]) or not a["stopped_at"]
        alerts.append(a)
    cur.close(); conn.close()
    return {"att": att, "overdue_by_agent": overdue_by_agent, "visits": visits, "alerts": alerts,
            "date": loc, "tz": tz}


def summary_email(business: str, d: dict) -> tuple:
    tz = d["tz"]
    c = d["att"]["counts"]
    E = _html.escape
    head = "".join(
        f'<td style="padding:10px;background:#F6F8FB;border-radius:8px;text-align:center"><b style="display:block;font-size:20px">{c[k]}</b>'
        f'<span style="font-size:11.5px;color:#667085">{E(lbl)}</span></td>' for k, lbl in REASONS)
    rows = "".join(
        f'<tr><td style="padding:6px 8px;border-bottom:1px solid #EEF0F3"><a href="{PORTAL}/estate/leads/{r["id"]}" style="color:#101828;font-weight:700">{E(_name(r))}</a></td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid #EEF0F3">{E(r["text"])}</td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid #EEF0F3;color:#667085">{E(r["agent_name"] or "Unassigned")}</td></tr>'
        for r in d["att"]["rows"] if r["reason"] != "noaction")
    waiting = (f'<table style="border-collapse:collapse;width:100%;font-size:13px">{rows}</table>' if rows
               else '<p style="font-size:13px;color:#667085">Nothing waiting. Well done.</p>')
    od = "".join(f'<li>{E(r["agent"])}: {r["n"]}</li>' for r in d["overdue_by_agent"]) or "<li>None</li>"
    vs = "".join(f'<li>{r["slot_datetime"].astimezone(tz):%H:%M} · {E(_name(r))} · {E(r["listing"] or "")} · {E(r["agent"] or "Unassigned")}</li>'
                 for r in d["visits"]) or "<li>None today</li>"
    al = "".join(
        f'<li>{E(_name(a))} ({"hand-over" if a["reason"] == "handoff" else "Ready buyer"}) · told {E(a["who"])} at {a["first_at"].astimezone(tz):%H:%M} · '
        + (f'acted in {a["took"]}' if a["took"] else '<b style="color:#B42318">not acted on yet</b>')
        + (' · <b style="color:#B42318">late</b>' if a["late"] and a["took"] else '') + '</li>'
        for a in d["alerts"]) or "<li>No alerts yesterday</li>"
    sec = lambda t: f'<h3 style="font-size:14px;margin:20px 0 6px">{t}</h3>'
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;color:#101828">
      <div style="background:#030C18;padding:16px 20px;border-radius:12px 12px 0 0">
        <p style="color:#25D366;font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;margin:0 0 4px">KeyLand · {E(business)}</p>
        <h2 style="color:#fff;margin:0;font-size:19px">Morning summary, {d['date']:%A %d %B}</h2>
      </div>
      <div style="border:1px solid #E5E7EB;border-top:none;border-radius:0 0 12px 12px;padding:18px 20px">
        <table style="width:100%;border-collapse:separate;border-spacing:6px"><tr>{head}</tr></table>
        {sec('Still waiting')}{waiting}
        {sec('Overdue next actions by salesperson')}<ul style="font-size:13px;margin:0;padding-left:18px">{od}</ul>
        {sec('Leads with no next action')}<p style="font-size:13px;margin:0">{c['noaction']}</p>
        {sec("Today's site inspections and viewings")}<ul style="font-size:13px;margin:0;padding-left:18px">{vs}</ul>
        {sec('Yesterday: alerts and how fast people acted')}<ul style="font-size:13px;margin:0;padding-left:18px">{al}</ul>
        <a href="{PORTAL}/estate/dashboard" style="display:inline-block;margin-top:18px;background:#25D366;color:#03240F;padding:11px 18px;border-radius:10px;text-decoration:none;font-weight:800">Open Needs attention</a>
        <p style="color:#98A2B3;font-size:12px;margin:18px 0 0">Sent to the owner and admins because the morning summary is on in KeyLand › Automation.</p>
      </div>
    </div>"""
    text = (f"KeyLand morning summary for {business}, {d['date']:%A %d %B}\n"
            + "\n".join(f"{lbl}: {c[k]}" for k, lbl in REASONS) + f"\n{PORTAL}/estate/dashboard")
    return f"KeyLand morning summary: {c['ready']} Ready, {c['chat']} chats waiting, {c['overdue']} overdue", html, text


def run_summary(tenant_id: int, now=None, mailer=None, force=False) -> int:
    """Send today's morning summary if it's time. Returns how many emails went."""
    now = now or _now()
    st = settings(tenant_id)
    if not force and not summary_due(st, now):
        return 0
    today = now.astimezone(ZoneInfo(st["tz"])).date()
    conn = _db(autocommit=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""UPDATE re_alert_settings SET summary_last=%s WHERE tenant_id=%s
                   AND summary_last IS DISTINCT FROM %s RETURNING tenant_id""", (today, tenant_id, today))
    claimed = cur.fetchone()
    cur.execute("SELECT business_name FROM re_tenants WHERE id=%s", (tenant_id,))
    business = (cur.fetchone() or {}).get("business_name") or ""
    managers = [p for p in people(tenant_id, cur) if p["manager"] and p["alerts_on"] and p["by_email"] and p.get("email")]
    cur.close(); conn.close()
    if not claimed and not force:
        return 0
    if mailer is None:
        from portal_utils import send_email as mailer
    subject, html, text = summary_email(business, summary_data(tenant_id, now, st))
    n = sum(1 for p in managers if mailer(p["email"], subject, html, text))
    print(f"📋 [KL ALERTS] tenant={tenant_id} morning summary → {n} manager(s)")
    return n
