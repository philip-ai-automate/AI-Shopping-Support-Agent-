"""KeyLand next actions, automatic WhatsApp follow-ups and STOP opt-outs.

Every open lead has one next action (title, due time, owner). Agents set it by
hand. Two optional automations, each switched on per agency in Automation:
  auto_actions   the system fills in the next action when something happens
                 (new lead, AI hand-over, readiness, booking, visit, plot, deposit)
                 and only ever replaces an action it set itself
  followups_on   up to 3 WhatsApp follow-ups after the buyer's last message; the
                 agency chooses the waits, wording, days, hours and time zone.
                 A wait under 24 hours goes as a plain message; longer ones need
                 wording Meta approved (submitted when the agency switches on).

Tables (new, created once):
  re_next_actions    one row per action; at most one 'open' per lead
  re_automation      per agency settings
  re_followup_steps  per agency follow-ups (wait, wording, Meta template state)
  re_followup_log    each follow-up sent / skipped / failed, keyed to the buyer
                     message it follows (anchor_at), so nothing is sent twice
  re_wa_optouts      buyers who replied STOP (also left out of broadcasts)
"""
import re
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

import psycopg2.extras

from db import get_db_connection

GRAPH = "https://graph.facebook.com/v19.0"
OPEN_STAGES_SQL = "COALESCE(c.pipeline_stage,'new') NOT IN ('allocated','lost')"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TIMEZONES = [("Africa/Lagos", "Lagos (WAT)"), ("Africa/Accra", "Accra (GMT)"), ("Europe/London", "London (UK)"),
             ("Africa/Nairobi", "Nairobi (EAT)"), ("Africa/Johannesburg", "Johannesburg (SAST)"),
             ("Asia/Dubai", "Dubai (GST)"), ("America/New_York", "New York (ET)")]
MAX_STEPS = 3
PLAIN_LIMIT_H = 24          # a wait under this goes as a plain message
PLAIN_SAFE = timedelta(hours=23, minutes=50)   # never send plain text later than this after the buyer
FIELDS = ("{name}", "{interest}", "{business}")
DEFAULTS = {"auto_actions": False, "followups_on": False, "days": [0, 1, 2, 3, 4, 5],
            "start_time": "08:00", "end_time": "20:00", "tz": "Africa/Lagos"}
DEFAULT_STEPS = [
    (20, "Hello {name}, just checking in. Do you have any other questions about {interest}? "
         "I can also send you the inspection dates."),
    (72, "Hello {name}, this is {business}. Are you still interested in {interest}? "
         "Reply YES and we will send the latest prices and inspection dates."),
    (168, "Hello {name}, a last note from {business}. If {interest} is still on your mind, reply and we will help. "
          "Reply STOP if you would rather not get these messages."),
]
STOP_WORDS = {"stop", "unsubscribe", "stop messages", "stop promotions", "opt out", "optout"}

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
        cur.execute("""SELECT to_regclass('re_next_actions') IS NOT NULL, to_regclass('re_automation') IS NOT NULL,
                              to_regclass('re_followup_steps') IS NOT NULL, to_regclass('re_followup_log') IS NOT NULL,
                              to_regclass('re_wa_optouts') IS NOT NULL""")
        if not all(cur.fetchone()):
            cur.execute("SET lock_timeout = '3s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_next_actions (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                title VARCHAR(200) NOT NULL,
                due_at TIMESTAMPTZ NOT NULL,
                owner_id INTEGER,
                set_by VARCHAR(10) NOT NULL DEFAULT 'person',
                set_by_name VARCHAR(120),
                event_key VARCHAR(60),
                status VARCHAR(10) NOT NULL DEFAULT 'open',
                outcome TEXT,
                done_at TIMESTAMPTZ,
                done_by_name VARCHAR(120),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS re_next_actions_one_open ON re_next_actions (contact_id) WHERE status = 'open'")
            cur.execute("CREATE INDEX IF NOT EXISTS re_next_actions_tenant ON re_next_actions (tenant_id, status, due_at)")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_automation (
                tenant_id INTEGER PRIMARY KEY,
                auto_actions BOOLEAN NOT NULL DEFAULT FALSE,
                auto_actions_on_at TIMESTAMPTZ,
                followups_on BOOLEAN NOT NULL DEFAULT FALSE,
                followups_on_at TIMESTAMPTZ,
                send_days VARCHAR(20) NOT NULL DEFAULT '0,1,2,3,4,5',
                start_time VARCHAR(5) NOT NULL DEFAULT '08:00',
                end_time VARCHAR(5) NOT NULL DEFAULT '20:00',
                tz VARCHAR(40) NOT NULL DEFAULT 'Africa/Lagos',
                updated_by VARCHAR(120),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_followup_steps (
                tenant_id INTEGER NOT NULL,
                position SMALLINT NOT NULL,
                wait_hours INTEGER NOT NULL,
                body TEXT NOT NULL,
                tpl_name VARCHAR(80),
                tpl_body TEXT,
                tpl_fields VARCHAR(60),
                tpl_status VARCHAR(20),
                tpl_reason TEXT,
                tpl_id VARCHAR(40),
                tpl_version SMALLINT NOT NULL DEFAULT 0,
                tpl_checked_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (tenant_id, position))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_followup_log (
                id BIGSERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                anchor_at TIMESTAMPTZ NOT NULL,
                position SMALLINT NOT NULL,
                status VARCHAR(10) NOT NULL,
                note TEXT,
                message TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (contact_id, anchor_at, position))""")
            cur.execute("CREATE INDEX IF NOT EXISTS re_followup_log_tenant ON re_followup_log (tenant_id, created_at)")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_wa_optouts (
                tenant_id INTEGER NOT NULL,
                phone VARCHAR(40) NOT NULL,
                opted_out_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                source VARCHAR(20) NOT NULL DEFAULT 'reply',
                PRIMARY KEY (tenant_id, phone))""")
        _tables_ready = True
    except Exception as e:
        print(f"⚠️ [KL ACTIONS] ensure_tables: {e}")
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc)


def _db():
    ensure_tables()
    return get_db_connection()


# ── Settings ──────────────────────────────────────────────────────────────────

def settings(tenant_id: int) -> dict:
    out = dict(DEFAULTS, auto_actions_on_at=None, followups_on_at=None, updated_by=None, updated_at=None)
    conn = _db()
    if not conn:
        return out
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM re_automation WHERE tenant_id=%s", (tenant_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        out.update({k: row[k] for k in ("auto_actions", "auto_actions_on_at", "followups_on", "followups_on_at",
                                         "start_time", "end_time", "tz", "updated_by", "updated_at")})
        out["days"] = [int(d) for d in (row["send_days"] or "").split(",") if d.strip().isdigit()]
    return out


def steps(tenant_id: int, with_defaults=True) -> list:
    conn = _db()
    rows = []
    if conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM re_followup_steps WHERE tenant_id=%s ORDER BY position", (tenant_id,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
    if not rows and with_defaults:
        rows = [{"position": i + 1, "wait_hours": w, "body": b, "tpl_status": None, "tpl_body": None,
                 "tpl_reason": None, "is_default": True} for i, (w, b) in enumerate(DEFAULT_STEPS)]
    for r in rows:
        r["needs_template"] = needs_template(r["wait_hours"])
        r["wait_label"] = wait_label(r["wait_hours"])
        r["tpl_current"] = bool(r.get("tpl_body")) and r.get("tpl_body") == r["body"]
    return rows


def needs_template(wait_hours) -> bool:
    return int(wait_hours) >= PLAIN_LIMIT_H


def wait_label(h) -> str:
    h = int(h)
    if h % 24 == 0:
        d = h // 24
        return f"{d} day{'s' if d != 1 else ''}"
    return f"{h} hour{'s' if h != 1 else ''}"


def check_body(body: str, template: bool) -> str:
    """Return a problem with the wording, or ''."""
    b = (body or "").strip()
    if len(b) < 15:
        return "is too short"
    if len(b) > 900:
        return "is too long (900 characters at most)"
    unknown = [f for f in re.findall(r"\{[^}]*\}", b) if f not in FIELDS]
    if unknown:
        return f"has {unknown[0]}, which isn't one of {{name}}, {{interest}} or {{business}}"
    if template:
        meta = re.sub(r"\{business\}", "x", b).strip()
        if meta.startswith("{") or meta.rstrip(".!?").endswith("}"):
            return "can't start or end with {name} or {interest} (a WhatsApp rule for approved wording)"
        if re.search(r"\}\s*\{", meta):
            return "can't have two fill-ins next to each other (a WhatsApp rule)"
        if "\n\n\n" in b:
            return "can't have more than one empty line in a row"
    return ""


def save(tenant_id: int, form, who: str) -> tuple:
    """Save the Automation page. Returns (errors, steps_to_submit)."""
    errors = []
    auto_actions = form.get("auto_actions") == "on"
    followups_on = form.get("followups_on") == "on"
    days = sorted({int(d) for d in form.getlist("send_days") if d.isdigit() and 0 <= int(d) <= 6})
    start, end = form.get("start_time", "08:00")[:5], form.get("end_time", "20:00")[:5]
    tz = form.get("tz", "Africa/Lagos")
    if tz not in dict(TIMEZONES):
        tz = "Africa/Lagos"
    if not re.fullmatch(r"\d\d:\d\d", start) or not re.fullmatch(r"\d\d:\d\d", end) or start >= end:
        errors.append("Sending hours: the start time must be before the end time.")
    if not days:
        errors.append("Sending days: tick at least one day.")

    new_steps = []
    for i in range(1, MAX_STEPS + 1):
        if form.get(f"s{i}_on") != "on":
            continue
        body = (form.get(f"s{i}_body") or "").strip().replace("\r\n", "\n")
        try:
            n = int(form.get(f"s{i}_wait") or 0)
        except ValueError:
            n = 0
        unit = form.get(f"s{i}_unit", "hours")
        hours = n * 24 if unit == "days" else n
        if hours < 1 or hours > 30 * 24:
            errors.append(f"Follow-up {i}: the wait must be between 1 hour and 30 days.")
            continue
        prob = check_body(body, needs_template(hours))
        if prob:
            errors.append(f"Follow-up {i}: the wording {prob}.")
            continue
        new_steps.append((hours, body))
    waits = [h for h, _ in new_steps]
    if waits != sorted(waits) or len(set(waits)) != len(waits):
        errors.append("Each follow-up must wait longer than the one before it.")
    if followups_on and not new_steps:
        errors.append("Switch on at least one follow-up, or switch automatic follow-ups off.")
    if errors:
        return errors, []

    conn = _db()
    if not conn:
        return ["Database unavailable."], []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM re_followup_steps WHERE tenant_id=%s ORDER BY position", (tenant_id,))
    old = {r["position"]: r for r in cur.fetchall()}
    cur.execute("""
        INSERT INTO re_automation (tenant_id, auto_actions, auto_actions_on_at, followups_on, followups_on_at,
                                   send_days, start_time, end_time, tz, updated_by, updated_at)
        VALUES (%(t)s, %(aa)s, CASE WHEN %(aa)s THEN NOW() END, %(fo)s, CASE WHEN %(fo)s THEN NOW() END,
                %(d)s, %(s)s, %(e)s, %(tz)s, %(who)s, NOW())
        ON CONFLICT (tenant_id) DO UPDATE SET
          auto_actions_on_at = CASE WHEN %(aa)s AND NOT re_automation.auto_actions THEN NOW()
                                    WHEN %(aa)s THEN re_automation.auto_actions_on_at END,
          followups_on_at = CASE WHEN %(fo)s AND NOT re_automation.followups_on THEN NOW()
                                 WHEN %(fo)s THEN re_automation.followups_on_at END,
          auto_actions = %(aa)s, followups_on = %(fo)s, send_days = %(d)s, start_time = %(s)s,
          end_time = %(e)s, tz = %(tz)s, updated_by = %(who)s, updated_at = NOW()""",
        {"t": tenant_id, "aa": auto_actions, "fo": followups_on, "d": ",".join(map(str, days)),
         "s": start, "e": end, "tz": tz, "who": who})
    cur.execute("DELETE FROM re_followup_steps WHERE tenant_id=%s AND position > %s", (tenant_id, len(new_steps)))
    for pos, (hours, body) in enumerate(new_steps, 1):
        o = old.get(pos) or {}
        cur.execute("""
            INSERT INTO re_followup_steps (tenant_id, position, wait_hours, body, tpl_name, tpl_body, tpl_fields,
                                           tpl_status, tpl_reason, tpl_id, tpl_version, tpl_checked_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (tenant_id, position) DO UPDATE SET wait_hours=EXCLUDED.wait_hours, body=EXCLUDED.body,
              updated_at=NOW()""",
            (tenant_id, pos, hours, body, o.get("tpl_name"), o.get("tpl_body"), o.get("tpl_fields"),
             o.get("tpl_status"), o.get("tpl_reason"), o.get("tpl_id"), o.get("tpl_version") or 0,
             o.get("tpl_checked_at")))
    conn.commit()
    cur.close(); conn.close()
    to_submit = []
    if followups_on:
        to_submit = [s for s in steps(tenant_id, with_defaults=False)
                     if s["needs_template"] and not (s["tpl_current"] and s.get("tpl_status") in ("pending", "approved"))]
    return [], to_submit


# ── Meta templates ────────────────────────────────────────────────────────────

def to_meta(body: str, business: str) -> tuple:
    """Turn {name}/{interest} into {{1}}, {{2}} in order; {business} is written in."""
    text = body.replace("{business}", business or "us")
    order = []

    def rep(m):
        f = m.group(1)
        if f not in order:
            order.append(f)
        return "{{" + str(order.index(f) + 1) + "}}"
    text = re.sub(r"\{(name|interest)\}", rep, text)
    return text, order


SAMPLES = {"name": "Funke", "interest": "Emerald Gardens Estate"}


def submit_template(tenant: dict, step: dict, http=None) -> str:
    """Send one follow-up's wording to Meta. Returns '' or a problem."""
    import requests
    http = http or requests
    waba, token = tenant.get("wa_waba_id"), tenant.get("wa_access_token")
    if not waba or not token:
        return "WhatsApp isn't fully connected (no WhatsApp Business Account), so the wording can't be sent to Meta."
    text, order = to_meta(step["body"], tenant.get("business_name"))
    version = int(step.get("tpl_version") or 0) + 1
    name = f"keyland_followup_{tenant['id']}_{step['position']}_v{version}"
    comp = {"type": "BODY", "text": text}
    if order:
        comp["example"] = {"body_text": [[SAMPLES[f] for f in order]]}
    payload = {"name": name, "language": "en", "category": "MARKETING", "components": [comp]}
    try:
        r = http.post(f"{GRAPH}/{waba}/message_templates", params={"access_token": token}, json=payload, timeout=30)
        data = r.json() if r.content else {}
    except Exception as e:
        print(f"⚠️ [KL FOLLOWUP] template submit tenant={tenant['id']}: {e}")
        return "Could not reach Meta. Nothing was sent, try saving again."
    conn = _db()
    cur = conn.cursor()
    if r.status_code != 200:
        err = ((data.get("error") or {}).get("error_user_msg") or (data.get("error") or {}).get("message")
               or "unknown reason")
        cur.execute("""UPDATE re_followup_steps SET tpl_status='rejected', tpl_reason=%s, tpl_body=%s, tpl_version=%s
                       WHERE tenant_id=%s AND position=%s""",
                    (f"Meta did not accept it: {err}", step["body"], version, tenant["id"], step["position"]))
        conn.commit(); cur.close(); conn.close()
        return f"Follow-up {step['position']}: Meta did not accept the wording ({err})."
    cur.execute("""UPDATE re_followup_steps SET tpl_name=%s, tpl_body=%s, tpl_fields=%s, tpl_status=%s, tpl_reason=NULL,
                          tpl_id=%s, tpl_version=%s, tpl_checked_at=NOW()
                   WHERE tenant_id=%s AND position=%s""",
                (name, step["body"], ",".join(order), "approved" if data.get("status") == "APPROVED" else "pending",
                 str(data.get("id") or ""), version, tenant["id"], step["position"]))
    conn.commit(); cur.close(); conn.close()
    return ""


def refresh_templates(tenant: dict, http=None, force=False):
    """Ask Meta how pending wording is doing (at most every 10 minutes)."""
    import requests
    http = http or requests
    token = tenant.get("wa_access_token")
    if not token:
        return
    conn = _db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT position, tpl_id FROM re_followup_steps WHERE tenant_id=%s AND tpl_status='pending'
                   AND tpl_id <> '' AND (%s OR tpl_checked_at IS NULL OR tpl_checked_at < NOW() - INTERVAL '10 minutes')""",
                (tenant["id"], force))
    for s in cur.fetchall():
        try:
            t = http.get(f"{GRAPH}/{s['tpl_id']}", params={"access_token": token, "fields": "status,rejected_reason"},
                         timeout=10).json()
        except Exception:
            continue
        st = (t.get("status") or "").upper()
        new = {"APPROVED": "approved", "REJECTED": "rejected", "PAUSED": "paused", "DISABLED": "rejected"}.get(st, "pending")
        reason = (t.get("rejected_reason") or "").replace("_", " ").lower()
        cur.execute("UPDATE re_followup_steps SET tpl_status=%s, tpl_reason=%s, tpl_checked_at=NOW() WHERE tenant_id=%s AND position=%s",
                    (new, ("Meta: " + reason) if new == "rejected" and reason and reason != "none" else None,
                     tenant["id"], s["position"]))
    conn.commit(); cur.close(); conn.close()


# ── Wording ───────────────────────────────────────────────────────────────────

def interest_of(cur, tenant_id: int, c: dict) -> str:
    cur.execute("""SELECT el.title AS estate, ll.title AS listing
                   FROM re_customers x
                   LEFT JOIN re_buyer_land bl ON bl.customer_id = x.id AND bl.tenant_id = x.tenant_id
                   LEFT JOIN re_property_listings el ON el.id = bl.estate_listing_id
                   LEFT JOIN re_property_listings ll ON ll.id = x.linked_listing_id
                   WHERE x.id=%s AND x.tenant_id=%s""", (c["id"], tenant_id))
    r = cur.fetchone() or {}
    r = dict(r) if not isinstance(r, dict) else r
    if r.get("estate") or r.get("listing"):
        return r.get("estate") or r.get("listing")
    if c.get("preferred_area"):
        return f"a property in {c['preferred_area']}"
    return "the property you asked about"


def first_name(c: dict) -> str:
    n = (c.get("name") or "").strip()
    return n.split()[0] if n else "there"


def fill(body: str, c: dict, business: str, interest: str) -> str:
    return (body.replace("{name}", first_name(c)).replace("{interest}", interest)
                .replace("{business}", business or "us"))


# ── Next actions ──────────────────────────────────────────────────────────────

def current(tenant_id: int, contact_id: int):
    conn = _db()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT a.*, TRIM(COALESCE(s.first_name,'') || ' ' || COALESCE(s.last_name,'')) AS owner_name
                   FROM re_next_actions a LEFT JOIN re_staff s ON s.id = a.owner_id
                   WHERE a.tenant_id=%s AND a.contact_id=%s AND a.status='open'""", (tenant_id, contact_id))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def history(tenant_id: int, contact_id: int, limit=5) -> list:
    conn = _db()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT title, status, outcome, done_at, done_by_name, set_by, created_at FROM re_next_actions
                   WHERE tenant_id=%s AND contact_id=%s AND status='done' ORDER BY done_at DESC LIMIT %s""",
                (tenant_id, contact_id, limit))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def set_action(cur, tenant_id: int, contact_id: int, title: str, due_at, owner_id, set_by="person",
               who=None, event_key=None, replaced_status="replaced"):
    """Make this the lead's one open action. Keeps the old follow-up date in step."""
    cur.execute("UPDATE re_next_actions SET status=%s WHERE tenant_id=%s AND contact_id=%s AND status='open'",
                (replaced_status, tenant_id, contact_id))
    cur.execute("""INSERT INTO re_next_actions (tenant_id, contact_id, title, due_at, owner_id, set_by, set_by_name, event_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, contact_id, title[:200], due_at, owner_id, set_by, who, event_key))
    cur.execute("UPDATE re_customers SET follow_up_at=%s WHERE id=%s AND tenant_id=%s", (due_at, contact_id, tenant_id))


def mark_done(cur, tenant_id: int, contact_id: int, outcome: str, who: str, staff_id, itype="note"):
    cur.execute("""UPDATE re_next_actions SET status='done', outcome=%s, done_at=NOW(), done_by_name=%s
                   WHERE tenant_id=%s AND contact_id=%s AND status='open' RETURNING title""",
                (outcome, who, tenant_id, contact_id))
    row = cur.fetchone()
    title = (row[0] if row and not isinstance(row, dict) else (row or {}).get("title")) if row else None
    cur.execute("""INSERT INTO re_interactions (tenant_id, contact_id, type, summary, outcome, logged_by)
                   VALUES (%s,%s,%s,%s,NULL,%s)""",
                (tenant_id, contact_id, itype, (f"Done: {title}. " if title else "") + outcome, staff_id))
    cur.execute("UPDATE re_customers SET follow_up_at=NULL WHERE id=%s AND tenant_id=%s", (contact_id, tenant_id))


def stage_changed(cur, tenant_id: int, contact_ids, stage: str):
    """Allocated and Lost close the open action."""
    if stage not in ("allocated", "lost"):
        return
    ensure_tables()
    cur.execute("UPDATE re_next_actions SET status='closed', done_at=NOW() WHERE tenant_id=%s AND contact_id = ANY(%s) AND status='open'",
                (tenant_id, list(contact_ids)))
    cur.execute("UPDATE re_customers SET follow_up_at=NULL WHERE tenant_id=%s AND id = ANY(%s)", (tenant_id, list(contact_ids)))


def local_tz(tenant_id: int) -> ZoneInfo:
    try:
        return ZoneInfo(settings(tenant_id)["tz"])
    except Exception:
        return ZoneInfo("Africa/Lagos")


def _at_local(day_dt, hh, mm, tz):
    d = day_dt.astimezone(tz).date()
    return datetime.combine(d, dtime(hh, mm), tz)


def latest_event(cur, tenant_id: int, c: dict, tz, now=None):
    """What has most recently happened to this lead, and the action it suggests."""
    now = now or _now()
    name = (c.get("name") or "").strip() or "the buyer"
    is_land = bool(c.get("is_land"))
    visit = "site inspection" if is_land else "viewing"
    ev = []
    if c.get("created_at"):
        ev.append((c["created_at"], "new", f"Reply to {name} and find out what they want",
                   c["created_at"] + timedelta(hours=2)))
    cur.execute("""SELECT id, created_at FROM re_handoff_requests WHERE tenant_id=%s AND customer_id=%s AND status='pending'
                   ORDER BY created_at DESC LIMIT 1""", (tenant_id, c["id"]))
    h = cur.fetchone()
    if h:
        ev.append((h["created_at"], f"handoff:{h['id']}", f"Call {name} about {c.get('interest') or 'what they asked'}",
                   h["created_at"] + timedelta(hours=1)))
    if c.get("readiness") == "ready" and c.get("readiness_at"):
        at = c["readiness_at"]
        due = _at_local(at, 18, 0, tz)
        if due < at + timedelta(hours=2):
            due = at + timedelta(hours=2)
        ev.append((at, f"ready:{at:%Y%m%d%H%M}", f"Call {name} to fix the {visit}", due))
    cur.execute("""SELECT b.id, b.created_at, s.slot_datetime, COALESCE(s.duration_mins, 60) AS mins
                   FROM re_inspection_bookings b JOIN re_inspection_slots s ON s.id = b.slot_id
                   WHERE b.tenant_id=%s AND b.customer_id=%s AND b.status='confirmed'
                   ORDER BY s.slot_datetime DESC LIMIT 1""", (tenant_id, c["id"]))
    b = cur.fetchone()
    if b and b["slot_datetime"]:
        slot = b["slot_datetime"]
        end = slot + timedelta(minutes=b["mins"])
        if end > now:
            due = _at_local(slot - timedelta(days=1), 10, 0, tz)
            if due < b["created_at"]:
                due = b["created_at"] + timedelta(hours=1)
            ev.append((b["created_at"], f"booking:{b['id']}",
                       f"Confirm the {visit} on {slot.astimezone(tz):%a %d %b, %H:%M} and send directions", due))
        else:
            ev.append((end, f"visited:{b['id']}", f"Ask {name} for their decision after the {visit}",
                       end + timedelta(days=2)))
    if (c.get("pipeline_stage") or "new") != "allocated":
        cur.execute("SELECT created_at, listing_id FROM re_estate_allocations WHERE tenant_id=%s AND contact_id=%s",
                    (tenant_id, c["id"]))
        a = cur.fetchone()
        if a and a["created_at"]:
            ev.append((a["created_at"], f"plot:{a['listing_id']}", f"Collect the deposit from {name}",
                       a["created_at"] + timedelta(days=3)))
        cur.execute("""SELECT id, updated_at FROM re_payment_milestones WHERE tenant_id=%s AND contact_id=%s
                       ORDER BY position, due_date LIMIT 1""", (tenant_id, c["id"]))
        m = cur.fetchone()
        if m:
            cur.execute("SELECT paid FROM re_payment_milestones WHERE id=%s", (m["id"],))
            if cur.fetchone()["paid"]:
                ev.append((m["updated_at"], f"deposit:{m['id']}",
                           f"Allocate the plot and send the allocation letter to {name}", m["updated_at"] + timedelta(days=2)))
    if not ev:
        return None
    at, key, title, due = max(ev, key=lambda x: x[0])
    return {"at": at, "key": key, "title": title, "due": due}


_LEAD_SQL = f"""
    SELECT c.id, c.name, c.created_at, c.assigned_to, c.pipeline_stage, c.preferred_area, c.linked_listing_id,
           bl.readiness, bl.readiness_at,
           (bl.buyer_kind = 'land' OR lp.property_type = 'land') AS is_land,
           COALESCE(el.title, lp.title, CASE WHEN c.preferred_area IS NOT NULL THEN 'a property in ' || c.preferred_area END) AS interest
    FROM re_customers c
    LEFT JOIN re_buyer_land bl ON bl.customer_id = c.id AND bl.tenant_id = c.tenant_id
    LEFT JOIN re_property_listings el ON el.id = bl.estate_listing_id
    LEFT JOIN re_property_listings lp ON lp.id = c.linked_listing_id
    WHERE c.tenant_id=%s AND {OPEN_STAGES_SQL}"""


def suggestion(tenant_id: int, contact_id: int, action, now=None):
    """A newer system suggestion to show beside an action a person set."""
    conn = _db()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(_LEAD_SQL + " AND c.id=%s", (tenant_id, contact_id))
    c = cur.fetchone()
    ev = latest_event(cur, tenant_id, c, local_tz(tenant_id), now) if c else None
    cur.close(); conn.close()
    if not ev:
        return None
    if action and (ev["key"] == action.get("event_key") or ev["at"] <= action["created_at"]):
        return None
    return ev


def sync(tenant_id: int, contact_id=None, now=None) -> int:
    """For agencies with automatic next actions on: fill in or update system actions.
    Never touches an action a person set. Returns how many actions were set."""
    st = settings(tenant_id)
    if not st["auto_actions"]:
        return 0
    now = now or _now()
    tz = ZoneInfo(st["tz"]) if st["tz"] else ZoneInfo("Africa/Lagos")
    on_at = st["auto_actions_on_at"] or now
    conn = _db()
    if not conn:
        return 0
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(_LEAD_SQL + (" AND c.id=%s" if contact_id else ""), (tenant_id, contact_id) if contact_id else (tenant_id,))
    leads = cur.fetchall()
    n = 0
    for c in leads:
        cur.execute("""SELECT * FROM re_next_actions WHERE tenant_id=%s AND contact_id=%s
                       ORDER BY created_at DESC LIMIT 1""", (tenant_id, c["id"]))
        last = cur.fetchone()
        open_a = last if last and last["status"] == "open" else None
        if open_a and open_a["set_by"] != "system":
            continue
        ev = latest_event(cur, tenant_id, c, tz, now)
        if not ev:
            continue
        if open_a and (ev["key"] == open_a["event_key"] or ev["at"] <= open_a["created_at"]):
            continue
        if not open_a and last and ev["at"] <= last["created_at"]:
            continue
        title, due, key = ev["title"], ev["due"], ev["key"]
        if ev["at"] < on_at:
            # Happened before the switch was turned on: a gentle catch-up, not an overdue alarm
            name = (c.get("name") or "").strip() or "the buyer"
            title, key = f"Follow up with {name}", "catch-up"
            due = on_at + timedelta(days=1)
        set_action(cur, tenant_id, c["id"], title, due, c["assigned_to"], "system", "KeyLand", key)
        n += 1
    conn.commit()
    cur.close(); conn.close()
    return n


# ── STOP opt-outs ─────────────────────────────────────────────────────────────

def _digits(p):
    return re.sub(r"\D", "", p or "")


def _key(p):
    """Last 10 digits: matches +234 803…, 234803… and 0803… as the same number."""
    return _digits(p)[-10:]


def opted_out(tenant_id: int, phone: str):
    conn = _db()
    if not conn:
        return None
    cur = conn.cursor()
    cur.execute("SELECT opted_out_at FROM re_wa_optouts WHERE tenant_id=%s AND right(phone, 10)=%s", (tenant_id, _key(phone)))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row else None


def optout_phones(tenant_id: int) -> set:
    conn = _db()
    if not conn:
        return set()
    cur = conn.cursor()
    cur.execute("SELECT phone FROM re_wa_optouts WHERE tenant_id=%s", (tenant_id,))
    out = {r[0][-10:] for r in cur.fetchall()}
    cur.close(); conn.close()
    return out


def set_optout(tenant_id: int, phone: str, on: bool, source="staff"):
    conn = _db()
    cur = conn.cursor()
    if on:
        cur.execute("""INSERT INTO re_wa_optouts (tenant_id, phone, source) VALUES (%s,%s,%s)
                       ON CONFLICT (tenant_id, phone) DO NOTHING""", (tenant_id, _digits(phone), source))
    else:
        cur.execute("DELETE FROM re_wa_optouts WHERE tenant_id=%s AND right(phone, 10)=%s", (tenant_id, _key(phone)))
    conn.commit(); cur.close(); conn.close()


# ── Follow-up sending ─────────────────────────────────────────────────────────

def in_window(st: dict, when) -> bool:
    tz = ZoneInfo(st["tz"])
    loc = when.astimezone(tz)
    if loc.weekday() not in st["days"]:
        return False
    hm = loc.strftime("%H:%M")
    return st["start_time"] <= hm < st["end_time"]


def next_window(st: dict, when):
    """The first moment at or after `when` inside the sending days/hours."""
    tz = ZoneInfo(st["tz"])
    loc = when.astimezone(tz)
    sh, sm = map(int, st["start_time"].split(":"))
    for d in range(0, 8):
        day = (loc + timedelta(days=d)).date()
        if day.weekday() not in st["days"]:
            continue
        start = datetime.combine(day, dtime(sh, sm), tz)
        cand = max(start, loc) if d == 0 else start
        if in_window(st, cand):
            return cand
    return None


def _send_text(tenant, to, body, http):
    r = http.post(f"{GRAPH}/{tenant['wa_phone_number_id']}/messages",
                  headers={"Authorization": f"Bearer {tenant['wa_access_token']}"},
                  json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": _digits(to),
                        "type": "text", "text": {"body": body}}, timeout=15)
    return r.status_code == 200, (r.text[:300] if r.status_code != 200 else (r.json().get("messages") or [{}])[0].get("id"))


def _send_template(tenant, to, step, values, http):
    params = [{"type": "text", "text": values[f]} for f in (step.get("tpl_fields") or "").split(",") if f]
    tpl = {"name": step["tpl_name"], "language": {"code": "en"}}
    if params:
        tpl["components"] = [{"type": "body", "parameters": params}]
    r = http.post(f"{GRAPH}/{tenant['wa_phone_number_id']}/messages",
                  headers={"Authorization": f"Bearer {tenant['wa_access_token']}"},
                  json={"messaging_product": "whatsapp", "to": _digits(to), "type": "template", "template": tpl},
                  timeout=15)
    return r.status_code == 200, (r.text[:300] if r.status_code != 200 else (r.json().get("messages") or [{}])[0].get("id"))


def _log(cur, tenant_id, cid, anchor, pos, status, note=None, message=None, at=None):
    cur.execute("""INSERT INTO re_followup_log (tenant_id, contact_id, anchor_at, position, status, note, message, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, NOW())) ON CONFLICT (contact_id, anchor_at, position) DO NOTHING""",
                (tenant_id, cid, anchor, pos, status, note, message, at))
    return cur.rowcount > 0


def plan_for(cur, tenant_id: int, c: dict, st: dict, stp: list, now):
    """Where this lead is in its follow-ups. Returns dict(state, ...) without sending."""
    cur.execute("""SELECT created_at AS at, customer_phone FROM re_wa_message_log
                   WHERE tenant_id=%s AND direction='inbound'
                     AND right(regexp_replace(customer_phone, '\\D', '', 'g'), 10) = %s
                   ORDER BY created_at DESC LIMIT 1""", (tenant_id, _key(c["phone_number"])))
    last_in = cur.fetchone() or {}
    anchor = last_in.get("at")
    if not anchor:
        return {"state": "none", "why": "hasn't messaged on WhatsApp"}
    c["wa_phone"] = last_in["customer_phone"]
    if st.get("followups_on_at") and anchor < st["followups_on_at"]:
        return {"state": "none", "why": "last message was before follow-ups were switched on", "anchor": anchor}
    cur.execute("SELECT 1 FROM re_wa_optouts WHERE tenant_id=%s AND right(phone, 10)=%s", (tenant_id, _key(c["phone_number"])))
    if cur.fetchone():
        return {"state": "stopped", "why": "replied STOP", "anchor": anchor}
    cur.execute("SELECT 1 FROM re_interactions WHERE tenant_id=%s AND contact_id=%s AND logged_at > %s LIMIT 1",
                (tenant_id, c["id"], anchor))
    if cur.fetchone():
        return {"state": "stopped", "why": "an agent has been in touch since", "anchor": anchor}
    cur.execute("SELECT 1 FROM re_next_actions WHERE tenant_id=%s AND contact_id=%s AND status='done' AND done_at > %s LIMIT 1",
                (tenant_id, c["id"], anchor))
    if cur.fetchone():
        return {"state": "stopped", "why": "an agent has been in touch since", "anchor": anchor}
    cur.execute("SELECT 1 FROM re_handoff_requests WHERE tenant_id=%s AND customer_id=%s AND status='pending' LIMIT 1",
                (tenant_id, c["id"]))
    if cur.fetchone():
        return {"state": "stopped", "why": "handed to an agent", "anchor": anchor}
    cur.execute("SELECT position, status, created_at FROM re_followup_log WHERE contact_id=%s AND anchor_at=%s",
                (c["id"], anchor))
    logged = cur.fetchall()
    done = {r["position"] for r in logged}
    todo = [s for s in stp if s["position"] not in done]
    if not todo:
        sent = [r["created_at"] for r in logged if r["status"] == "sent"]
        return {"state": "finished", "anchor": anchor, "ended": 99 in done,
                "sent": len(sent), "last_sent": max(sent) if sent else None}
    s = todo[0]
    due = anchor + timedelta(hours=s["wait_hours"])
    return {"state": "waiting" if due > now else "due", "anchor": anchor, "step": s, "due": due,
            "later": todo[1:], "total": len(stp)}


def run_tenant(tenant: dict, now=None, http=None, dry=False) -> dict:
    """Send what is due for one agency. Returns counts."""
    import requests
    http = http or requests
    now = now or _now()
    tid = tenant["id"]
    st = settings(tid)
    out = {"sent": 0, "skipped": 0, "failed": 0, "actions": 0}
    if not st["followups_on"] or not tenant.get("wa_phone_number_id") or not tenant.get("wa_access_token"):
        return out
    stp = steps(tid, with_defaults=False)
    if not stp:
        return out
    conn = _db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""SELECT c.id, c.name, c.phone_number, c.preferred_area, c.assigned_to, c.linked_listing_id
                    FROM re_customers c WHERE c.tenant_id=%s AND {OPEN_STAGES_SQL} AND c.phone_number IS NOT NULL""", (tid,))
    leads = cur.fetchall()
    for c in leads:
        for _ in range(MAX_STEPS + 1):
            p = plan_for(cur, tid, c, st, stp, now)
            if p["state"] == "finished":
                # A day after the last follow-up with no reply, hand the lead back to a person
                if p["sent"] and not p["ended"] and now > p["last_sent"] + timedelta(hours=24):
                    if _log(cur, tid, c["id"], p["anchor"], 99, "ended", "no reply to follow-ups"):
                        _no_reply(cur, tid, c, p["sent"], st)
                        out["actions"] += 1
                break
            if p["state"] != "due":
                break
            s = p["step"]
            # A later follow-up is already due too (switched on late, job paused): skip this one
            if any(p["anchor"] + timedelta(hours=x["wait_hours"]) <= now for x in p["later"]):
                _log(cur, tid, c["id"], p["anchor"], s["position"], "skipped", "a later follow-up was already due")
                out["skipped"] += 1
                continue
            plain = not s["needs_template"]
            if plain and now > p["anchor"] + PLAIN_SAFE:
                _log(cur, tid, c["id"], p["anchor"], s["position"], "skipped", "outside WhatsApp's 24-hour window")
                out["skipped"] += 1
                continue
            if not in_window(st, now):
                nw = next_window(st, now)
                if plain and (not nw or nw > p["anchor"] + PLAIN_SAFE):
                    _log(cur, tid, c["id"], p["anchor"], s["position"], "skipped",
                         "sending hours start after WhatsApp's 24-hour window closes")
                    out["skipped"] += 1
                    continue
                break   # wait for sending hours
            if not plain and not (s.get("tpl_status") == "approved" and s.get("tpl_current")):
                _log(cur, tid, c["id"], p["anchor"], s["position"], "skipped", "wording not approved by Meta yet")
                out["skipped"] += 1
                continue
            interest = interest_of(cur, tid, c)
            body = fill(s["body"], c, tenant.get("business_name"), interest)
            if dry:
                ok, info = True, "dry-run"
            else:
                try:
                    if plain:
                        ok, info = _send_text(tenant, c["wa_phone"], body, http)
                    else:
                        ok, info = _send_template(tenant, c["wa_phone"], s,
                                                  {"name": first_name(c), "interest": interest}, http)
                except Exception as e:
                    ok, info = False, str(e)[:300]
            if ok:
                _log(cur, tid, c["id"], p["anchor"], s["position"], "sent", None if dry else info, body, at=now)
                cur.execute("""INSERT INTO re_wa_message_log (tenant_id, phone_number_id, customer_phone, direction,
                               content, message_type, meta_message_id) VALUES (%s,%s,%s,'outbound',%s,%s,%s)
                               ON CONFLICT DO NOTHING""",
                            (tid, tenant["wa_phone_number_id"], c["wa_phone"], body[:2000],
                             "text" if plain else "template", None if dry else info))
                cur.execute("INSERT INTO re_chat_messages (tenant_id, customer_id, role, content) VALUES (%s,%s,'assistant',%s)",
                            (tid, c["id"], body))
                out["sent"] += 1
            else:
                _log(cur, tid, c["id"], p["anchor"], s["position"], "failed", info)
                out["failed"] += 1
                print(f"⚠️ [KL FOLLOWUP] tenant={tid} lead={c['id']} step={s['position']} failed: {info}")
            if not dry:
                conn.commit()
            break   # one message per lead per run
    if dry:
        conn.rollback()   # dry run: nothing is kept
    else:
        conn.commit()
    cur.close(); conn.close()
    return out


def _no_reply(cur, tid, c, n, st):
    name = (c.get("name") or "").strip() or "the buyer"
    cur.execute("""INSERT INTO re_interactions (tenant_id, contact_id, type, summary)
                   VALUES (%s,%s,'note',%s)""",
                (tid, c["id"], f"No reply to {n} automatic WhatsApp follow-up{'s' if n != 1 else ''}."))
    cur.execute("SELECT set_by FROM re_next_actions WHERE tenant_id=%s AND contact_id=%s AND status='open'", (tid, c["id"]))
    a = cur.fetchone()
    if st["auto_actions"] and (a is None or a["set_by"] == "system"):
        set_action(cur, tid, c["id"], f"Call {name}: no reply to follow-ups", _now() + timedelta(hours=4),
                   c["assigned_to"], "system", "KeyLand", "no-reply")


def lead_status(tenant_id: int, contact: dict, now=None) -> str:
    """One line for the buyer card: what the follow-ups are doing for this lead."""
    st = settings(tenant_id)
    if (contact.get("pipeline_stage") or "new") in ("allocated", "lost"):
        return ""
    if opted_out(tenant_id, contact.get("phone_number")):
        return "Opted out: replied STOP, so no follow-ups or broadcasts"
    if not st["followups_on"]:
        return ""
    stp = steps(tenant_id, with_defaults=False)
    if not stp:
        return ""
    now = now or _now()
    conn = _db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    p = plan_for(cur, tenant_id, contact, st, stp, now)
    cur.close(); conn.close()
    tz = ZoneInfo(st["tz"])
    if p["state"] in ("waiting", "due"):
        when = p["due"] if p["state"] == "waiting" else now
        if not in_window(st, when):
            when = next_window(st, when) or when
        return f"WhatsApp follow-up {p['step']['position']} of {p['total']} due {when.astimezone(tz):%a %d %b, %H:%M}"
    if p["state"] == "finished":
        return "No reply to the automatic follow-ups" if p["sent"] else ""
    if p["state"] == "stopped":
        return f"Follow-ups stopped: {p['why']}"
    return ""


def report(tenant_id: int, days=30) -> dict:
    conn = _db()
    if not conn:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT COUNT(*) AS sent,
               COUNT(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM re_wa_message_log m JOIN re_customers c ON c.id = f.contact_id
                   WHERE m.tenant_id = f.tenant_id AND m.direction='inbound'
                     AND right(regexp_replace(m.customer_phone,'\\D','','g'), 10) = right(regexp_replace(c.phone_number,'\\D','','g'), 10)
                     AND m.created_at > f.created_at AND m.created_at < f.created_at + INTERVAL '7 days')) AS replied,
               COUNT(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM re_inspection_bookings b WHERE b.customer_id = f.contact_id
                     AND b.created_at > f.created_at AND b.created_at < f.created_at + INTERVAL '14 days')) AS booked
        FROM re_followup_log f
        WHERE f.tenant_id=%s AND f.status='sent' AND f.created_at >= NOW() - %s * INTERVAL '1 day'""", (tenant_id, days))
    r = dict(cur.fetchone())
    cur.execute("SELECT COUNT(*) AS n FROM re_wa_optouts WHERE tenant_id=%s", (tenant_id,))
    r["optouts"] = cur.fetchone()["n"]
    cur.close(); conn.close()
    return r
