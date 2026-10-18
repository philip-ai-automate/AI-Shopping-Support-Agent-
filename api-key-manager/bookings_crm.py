"""
bookings_crm.py — what Microsoft Bookings demos look like inside the CRM
(2026-10-18, approved mockup https://claude.ai/artifact/5kpdVX9PnTUz8zWU8rpEiu):
the "📅 Demo booked" badge on Pipeline / Leads rows, the "Demo booking"
filter, the bar and Activity tab on the lead page, and the email report's
"Booked a demo". No Flask and no portal_routes import, so portal_routes can
use it; bookings_routes.py does the Microsoft side and writes the rows.

A lead's demo state:
  booked — a booking still to come
  due    — the booking time has passed and nobody marked it done / no-show
  done   — someone pressed "Demo done"
  none   — no booking, or only cancelled / no-show ones
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2.extras

DEMO_FILTER_OPTIONS = [("booked", "Demo booked"), ("due", "Demo due — not marked done"),
                       ("done", "Demo done"), ("none", "No demo booked")]
REPORT_WINDOW = timedelta(days=30)   # "Booked a demo" counts bookings within 30 days of the email

# Times show in the business's own time: the owner's chosen timezone, else
# its country's. Countries with more than one timezone fall back to UTC.
_COUNTRY_TZ = {"NG": "Africa/Lagos", "GB": "Europe/London", "GH": "Africa/Accra", "KE": "Africa/Nairobi",
               "ZA": "Africa/Johannesburg", "AE": "Asia/Dubai", "IE": "Europe/Dublin", "CM": "Africa/Douala",
               "SN": "Africa/Dakar", "CI": "Africa/Abidjan", "RW": "Africa/Kigali", "UG": "Africa/Kampala",
               "TZ": "Africa/Dar_es_Salaam", "EG": "Africa/Cairo", "IN": "Asia/Kolkata"}


def _exists(alias, cond):
    return (f"EXISTS (SELECT 1 FROM booking_appointments ba WHERE ba.tenant_id={alias}.tenant_id "
            f"AND ba.lead_id={alias}.id AND {cond})")


def _state_sql(alias):
    return {
        "booked": _exists(alias, "ba.status='booked' AND ba.start_at > NOW()"),
        "due":    _exists(alias, "ba.status='booked' AND ba.start_at <= NOW()"),
        "done":   _exists(alias, "ba.status='done'"),
        "none":   "NOT " + _exists(alias, "ba.status IN ('booked','done')"),
    }


def filter_clause(alias: str, picked: list):
    """SQL for the Filters panel's "Demo booking" boxes (any of them)."""
    exprs = _state_sql(alias)
    ors = [exprs[v] for v in picked if v in exprs]
    return ("(" + " OR ".join(ors) + ")") if ors else None


def option_counts(cur, where_sql: str, params: list) -> dict:
    """How many records each "Demo booking" box has, over the page's own
    records. 'any' = whether this business has ever had a booking (the
    section only shows then)."""
    e = _state_sql("mpl")
    c = cur.connection.cursor()
    try:
        c.execute(f"""SELECT count(*) FILTER (WHERE {e['booked']}), count(*) FILTER (WHERE {e['due']}),
                             count(*) FILTER (WHERE {e['done']}), count(*) FILTER (WHERE {e['none']})
                      FROM merchant_pipeline_leads mpl WHERE {where_sql}""", list(params))
        b, d, dn, n = c.fetchone()
        c.execute("SELECT EXISTS (SELECT 1 FROM booking_appointments WHERE tenant_id=%s)", [params[0]])
        anyb = c.fetchone()[0]
    finally:
        c.close()
    return {"booked": b, "due": d, "done": dn, "none": n, "any": anyb}


def tenant_tz(cur, tenant_id: int) -> ZoneInfo:
    c = cur.connection.cursor()
    try:
        c.execute("""SELECT timezone, business_country FROM customers WHERE tenant_id=%s
                     ORDER BY created_at ASC LIMIT 1""", (tenant_id,))
        row = c.fetchone()
    finally:
        c.close()
    for name in ((row[0] if row else None), _COUNTRY_TZ.get(((row[1] if row else "") or "").upper())):
        try:
            if name:
                return ZoneInfo(name)
        except Exception:
            pass
    return ZoneInfo("UTC")


def when(dt, tz, with_day=True) -> str:
    """'Tue 22 Oct, 10:00 WAT'."""
    if not dt:
        return ""
    t = dt.astimezone(tz)
    return t.strftime("%a %-d %b, %H:%M " if with_day else "%H:%M ") + (t.tzname() or "")


def attach(cur, tenant_id: int, leads: list):
    """Gives each lead dict a `demo` entry for its list badge (None if no
    booking to show)."""
    ids = [l["id"] for l in leads if l.get("id")]
    for l in leads:
        l["demo"] = None
    if not ids:
        return
    c = cur.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c.execute("""SELECT DISTINCT ON (lead_id) lead_id, start_at
                     FROM booking_appointments WHERE tenant_id=%s AND lead_id = ANY(%s) AND status='booked'
                     ORDER BY lead_id, (start_at > NOW()) DESC,
                              CASE WHEN start_at > NOW() THEN start_at END ASC, start_at DESC""",
                  (tenant_id, ids))
        rows = {r["lead_id"]: r for r in c.fetchall()}
    finally:
        c.close()
    if not rows:
        return
    tz, now = tenant_tz(cur, tenant_id), datetime.now(timezone.utc)
    for l in leads:
        r = rows.get(l.get("id"))
        if r:
            future = r["start_at"] > now
            l["demo"] = {"state": "booked" if future else "due", "when": when(r["start_at"], tz)}


def lead_view(cur, tenant_id: int, lead_id: int) -> dict:
    """Everything the lead page shows: the bar (current booking) and the
    Activity tab's history, newest first."""
    c = cur.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c.execute("""SELECT * FROM booking_appointments WHERE tenant_id=%s AND lead_id=%s
                     ORDER BY start_at DESC""", (tenant_id, lead_id))
        appts = c.fetchall()
        c.execute("""SELECT * FROM booking_events WHERE tenant_id=%s AND lead_id=%s
                     ORDER BY created_at DESC, id DESC LIMIT 100""", (tenant_id, lead_id))
        events = c.fetchall()
    finally:
        c.close()
    if not appts and not events:
        return {"current": None, "events": [], "tz": None}
    tz, now = tenant_tz(cur, tenant_id), datetime.now(timezone.utc)
    upcoming = sorted([a for a in appts if a["status"] == "booked" and a["start_at"] > now], key=lambda a: a["start_at"])
    due = [a for a in appts if a["status"] == "booked" and a["start_at"] <= now]
    current = None
    if due:
        current = dict(due[0], state="due")
    elif upcoming:
        current = dict(upcoming[0], state="booked")
    elif appts and appts[0]["status"] == "cancelled" and appts[0]["start_at"] > now - timedelta(days=14):
        current = dict(appts[0], state="cancelled")
    if current:
        current["when"] = when(current["start_at"], tz)
        current["until"] = when(current["end_at"], tz, with_day=False) if current.get("end_at") else ""
    for ev in events:
        ev["at"] = ev["created_at"].astimezone(tz).strftime("%-d %b, %H:%M")
    return {"current": current, "events": events, "tz": tz}


def report_bookings(cur, tenant_id: int, recipients: list) -> dict:
    """email (lowercase) → the booking that person made within 30 days after
    this campaign's email reached them."""
    sent = {(r["email"] or "").lower(): r["sent_at"] for r in recipients if r.get("sent_at") and r.get("email")}
    if not sent:
        return {}
    c = cur.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c.execute("""SELECT lower(email) AS email, start_at, COALESCE(booked_at, first_seen_at) AS booked_at,
                            lead_id, status
                     FROM booking_appointments WHERE tenant_id=%s AND lower(email) = ANY(%s)
                     ORDER BY 3 ASC""", (tenant_id, list(sent)))
        rows = c.fetchall()
    finally:
        c.close()
    tz, out = None, {}
    for r in rows:
        s = sent.get(r["email"])
        if r["email"] in out or not s or not (s <= r["booked_at"] <= s + REPORT_WINDOW):
            continue
        tz = tz or tenant_tz(cur, tenant_id)
        out[r["email"]] = {"when": when(r["start_at"], tz), "lead_id": r["lead_id"], "status": r["status"]}
    return out
