"""
setup_requests.py — "Set it up for me" (14 Nov 2026, approved mock-up
https://claude.ai/artifact/1panZL64jYfXEjf1HgLnDE, plan doc
https://claude.ai/artifact/XzmA699A7ZyUNsmYeV8za2).

A business on any sign-up route (portal sign-up, code page, WooCommerce
/connect, Shopify app, Dashboard set-up box, Connect WhatsApp steps, Settings ›
WhatsApp) asks PhiXtra to set it up for them:
  - how to contact them: voice call, WhatsApp call or chat, or email
  - phone and WhatsApp numbers (separate, each with its country), email
  - a booked time from PhiXtra's own booking calendar: every day
    10:00–18:00 UK, half-hour slots, 2 bookings per slot (Philip + David)

Each booking:
  - alert email + calendar invite (.ics) to ALERT_EMAILS
  - WhatsApp alert to ALERT_PHONES from the Account Manager number, once the
    Utility template TEMPLATE is approved by Meta
  - calendar invite to the business (when it gave an email), with a link to
    change or cancel the time
  - a row in Admin › Accounts › Set-up Requests

No Flask in here (the routes live in portal_routes / portal_admin_routes /
shopify_app). The table is created once by hand (TABLE_SQL) — never DDL at
request time (see the accounts_core incident of 2026-10-10).
"""
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2.extras
import requests

from db import get_db_connection

UK = ZoneInfo("Europe/London")
OPEN_HOUR, CLOSE_HOUR = 10, 18      # UK time, every day (user 14 Nov 2026)
STEP_MIN = 30
PER_SLOT = 2                        # Philip + David
DAYS_AHEAD = 21
MIN_LEAD_MIN = 30                   # a slot must start at least 30 min from now
RATE_PER_HOUR = 3                   # requests per IP per hour
GRAPH = "https://graph.facebook.com/v21.0"
PORTAL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")


def _list_env(name, default):
    return [x.strip() for x in (os.getenv(name) or default).split(",") if x.strip()]


ALERT_EMAILS = _list_env("SETUP_ALERT_EMAILS", "philip@phixtra.com,david.o@phixtra.com")
ALERT_PHONES = _list_env("SETUP_ALERT_PHONES", "447503094646,2347060720545")
TEMPLATE = "phixtra_setup_request"

METHODS = {"voice": "Voice call", "wa": "WhatsApp call or chat", "email": "Email"}
ROUTES = {"portal": "Portal", "woocommerce": "WooCommerce", "shopify": "Shopify"}
PLACES = {
    "register": "Sign-up page", "code": "Code page", "connect": "Connect page",
    "shopify_welcome": "Welcome screen", "shopify_home": "App home",
    "dashboard": "Dashboard", "wa_connect": "Connect WhatsApp step",
    "wa_failed": "Meta window closed", "wa_settings": "Settings › WhatsApp",
    "manage": "Change link",
}
STATUSES = [("new", "New"), ("called", "Called"), ("no_answer", "No answer"),
            ("done", "Set up done"), ("not_interested", "Not interested"), ("cancelled", "Cancelled by them")]
STATUS_LABEL = dict(STATUSES)
OPEN = ("new", "called", "no_answer")

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS setup_requests (
    id            SERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status        TEXT NOT NULL DEFAULT 'new',
    route         TEXT NOT NULL,             -- portal | woocommerce | shopify
    place         TEXT,                      -- see PLACES
    signup_choice TEXT,                      -- e.g. 'Website + WhatsApp · B2B'
    name          TEXT,
    company       TEXT,
    email         TEXT,
    method        TEXT NOT NULL,             -- voice | wa | email
    phone         TEXT,                      -- digits, country code first
    wa            TEXT,                      -- digits, country code first
    slot_at       TIMESTAMPTZ NOT NULL,      -- booked start (30 min)
    tz            TEXT,                      -- the business's time zone (browser)
    store_url     TEXT,
    tenant_id     INTEGER,
    customer_id   INTEGER,
    notes         TEXT,
    handled_by    TEXT,
    ip            TEXT,
    token         TEXT NOT NULL UNIQUE,      -- change / cancel link
    seq           INTEGER NOT NULL DEFAULT 0 -- calendar invite version
);
CREATE INDEX IF NOT EXISTS setup_requests_slot    ON setup_requests (slot_at);
CREATE INDEX IF NOT EXISTS setup_requests_tenant  ON setup_requests (tenant_id);
CREATE INDEX IF NOT EXISTS setup_requests_email   ON setup_requests (LOWER(email));
CREATE INDEX IF NOT EXISTS setup_requests_created ON setup_requests (created_at DESC);
"""


def _db(dict_rows=True):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if dict_rows else conn.cursor()
    return conn, cur


def _now():
    return datetime.now(timezone.utc)


def _tz(name):
    try:
        return ZoneInfo(name) if name else UK
    except Exception:
        return UK


def safe_tz(name) -> str:
    try:
        ZoneInfo(name or "")
        return name
    except Exception:
        return "Europe/London"


# ── wording ───────────────────────────────────────────────────────────────

def when_text(at, tz_name=None, with_zone=False) -> str:
    """'Tue 17 Nov, 10:00' in the given zone (UK by default)."""
    if not at:
        return ""
    z = _tz(tz_name)
    t = at.astimezone(z)
    s = f"{t:%a} {t.day} {t:%b}, {t:%H:%M}"
    if with_zone:
        s += " (UK)" if z == UK or tz_name in (None, "", "Europe/London") else f" ({tz_name.split('/')[-1].replace('_', ' ')} time)"
    return s


def plus(d) -> str:
    """Digits → '+44 7700 900123'-ish (spaces from phonenumbers when it can)."""
    d = "".join(ch for ch in (d or "") if ch.isdigit())
    if not d:
        return ""
    try:
        import phonenumbers as pn
        return pn.format_number(pn.parse("+" + d, None), pn.PhoneNumberFormat.INTERNATIONAL)
    except Exception:
        return "+" + d


def contact_value(r) -> str:
    """The number or email to use for the chosen method."""
    if r["method"] == "wa":
        return plus(r.get("wa"))
    if r["method"] == "voice":
        return plus(r.get("phone"))
    return r.get("email") or ""


def thanks_text(r) -> dict:
    """What replaces the offer box once booked (the business's own time zone)."""
    how = {"voice": "We'll phone you then.", "wa": "We'll contact you on WhatsApp then.",
           "email": "We'll email you then."}[r["method"]]
    extra = " A calendar invite is on its way to your email." if r.get("email") else ""
    return {"title": f"Booked: {when_text(r['slot_at'], r.get('tz'))}.", "text": how + extra,
            "manage": f"{PORTAL}/setup-help/b/{r['token']}"}


# ── calendar ──────────────────────────────────────────────────────────────

def _slot_starts(day: date):
    for h in range(OPEN_HOUR, CLOSE_HOUR):
        for m in range(0, 60, STEP_MIN):
            yield datetime(day.year, day.month, day.day, h, m, tzinfo=UK).astimezone(timezone.utc)


def _taken(cur, start, end, exclude_id=None):
    cur.execute("""SELECT slot_at, COUNT(*) AS n FROM setup_requests
                    WHERE slot_at >= %s AND slot_at < %s AND status <> 'cancelled'
                      AND (%s IS NULL OR id <> %s)
                    GROUP BY slot_at""", (start, end, exclude_id, exclude_id))
    return {r["slot_at"]: r["n"] for r in cur.fetchall()}


def slots(exclude_id=None) -> list:
    """[{date:'2026-11-15', slots:[{at: ISO UTC, uk:'10:00', free: bool}]}] for
    the next DAYS_AHEAD UK days. Times are shown in the visitor's zone by the page."""
    now = _now()
    first = now.astimezone(UK).date()
    start = datetime(first.year, first.month, first.day, tzinfo=UK).astimezone(timezone.utc)
    end = start + timedelta(days=DAYS_AHEAD + 1)
    conn, cur = _db()
    try:
        taken = _taken(cur, start, end, exclude_id)
    finally:
        cur.close(); conn.close()
    earliest = now + timedelta(minutes=MIN_LEAD_MIN)
    out = []
    for i in range(DAYS_AHEAD):
        d = first + timedelta(days=i)
        row = []
        for s in _slot_starts(d):
            if s < earliest:
                continue
            row.append({"at": s.isoformat().replace("+00:00", "Z"), "uk": s.astimezone(UK).strftime("%H:%M"),
                        "free": taken.get(s, 0) < PER_SLOT})
        if row:
            out.append({"date": d.isoformat(), "slots": row})
    return out


def parse_slot(raw):
    """A slot string from the page → aware UTC datetime on the grid, or None."""
    try:
        t = datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    t = t.astimezone(timezone.utc)
    uk = t.astimezone(UK)
    if uk.minute % STEP_MIN or uk.second or not (OPEN_HOUR <= uk.hour < CLOSE_HOUR):
        return None
    now = _now()
    if t < now + timedelta(minutes=MIN_LEAD_MIN) - timedelta(minutes=1) or t > now + timedelta(days=DAYS_AHEAD + 1):
        return None
    return t


def _slot_free(cur, at, exclude_id=None) -> bool:
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (int(at.timestamp()) % 2_000_000_000,))
    return _taken(cur, at, at + timedelta(seconds=1), exclude_id).get(at, 0) < PER_SLOT


# ── create / change / cancel ──────────────────────────────────────────────

def open_for(tenant_id=None, ids=()):
    """The business's open booking (if any): by account, or by request ids kept
    in the browser session."""
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not tenant_id and not ids:
        return None
    conn, cur = _db()
    try:
        cur.execute(f"""SELECT * FROM setup_requests
                         WHERE status IN %s AND slot_at > NOW() - INTERVAL '2 hours'
                           AND ((%s IS NOT NULL AND tenant_id=%s) OR id = ANY(%s))
                         ORDER BY created_at DESC LIMIT 1""", (OPEN, tenant_id, tenant_id, ids or [0]))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def create(d: dict, ip: str):
    """Save a booking. d: route, place, signup_choice, name, company, email,
    method, phone, wa (digits), slot (datetime), tz, store_url, tenant_id,
    customer_id. Returns (row, error, is_new)."""
    conn, cur = _db()
    try:
        if ip:
            cur.execute("""SELECT COUNT(*) AS n FROM setup_requests
                            WHERE ip=%s AND created_at > NOW() - INTERVAL '1 hour'""", (ip,))
            if cur.fetchone()["n"] >= RATE_PER_HOUR:
                return None, "You've sent a few requests already. Please try again in an hour, or email support@phixtra.com.", False
        # The same business asking again → keep the one open booking, no second alert.
        cur.execute(f"""SELECT * FROM setup_requests
                         WHERE status IN %s AND slot_at > NOW() - INTERVAL '2 hours'
                           AND ((%s IS NOT NULL AND tenant_id=%s)
                                OR (%s <> '' AND LOWER(email)=LOWER(%s))
                                OR (%s <> '' AND (phone=%s OR wa=%s))
                                OR (%s <> '' AND (phone=%s OR wa=%s)))
                         ORDER BY created_at DESC LIMIT 1""",
                    (OPEN, d.get("tenant_id"), d.get("tenant_id"),
                     d.get("email") or "", d.get("email") or "",
                     d.get("phone") or "", d.get("phone") or "", d.get("phone") or "",
                     d.get("wa") or "", d.get("wa") or "", d.get("wa") or ""))
        old = cur.fetchone()
        if old:
            return old, None, False
        at = d["slot"]
        if not _slot_free(cur, at):
            conn.rollback()
            return None, "Sorry, that time has just been booked. Please choose another time.", False
        cur.execute("""INSERT INTO setup_requests
                         (route, place, signup_choice, name, company, email, method, phone, wa, slot_at, tz,
                          store_url, tenant_id, customer_id, ip, token)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (d["route"], d.get("place"), (d.get("signup_choice") or "")[:120] or None,
                     (d.get("name") or "")[:120] or None, (d.get("company") or "")[:160] or None,
                     (d.get("email") or "")[:200] or None, d["method"], d.get("phone") or None, d.get("wa") or None,
                     at, safe_tz(d.get("tz")), (d.get("store_url") or "")[:200] or None,
                     d.get("tenant_id"), d.get("customer_id"), (ip or "")[:64] or None,
                     secrets.token_urlsafe(24)))
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()
    link_pending()
    row = get(row["id"]) or row
    _notify_new(row)
    return row, None, True


def get(rid=None, token=None):
    conn, cur = _db()
    try:
        if token:
            cur.execute("SELECT * FROM setup_requests WHERE token=%s", (token,))
        else:
            cur.execute("SELECT * FROM setup_requests WHERE id=%s", (int(rid),))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def reschedule(token, at):
    """The business moved its booking (change link). Returns (row, error)."""
    r = get(token=token)
    if not r or r["status"] not in OPEN:
        return None, "This booking can't be changed any more."
    conn, cur = _db()
    try:
        if not _slot_free(cur, at, exclude_id=r["id"]):
            conn.rollback()
            return None, "Sorry, that time has just been booked. Please choose another time."
        cur.execute("""UPDATE setup_requests SET slot_at=%s, seq=seq+1, updated_at=NOW(),
                              notes=CONCAT_WS(E'\\n', NULLIF(notes,''), %s)
                        WHERE id=%s RETURNING *""",
                    (at, f"{when_text(_now())}: they moved the time from {when_text(r['slot_at'])} to {when_text(at)}.", r["id"]))
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()
    _notify_changed(row, old_at=r["slot_at"])
    return row, None


def cancel(token):
    r = get(token=token)
    if not r or r["status"] not in OPEN:
        return None
    conn, cur = _db()
    try:
        cur.execute("""UPDATE setup_requests SET status='cancelled', seq=seq+1, updated_at=NOW()
                        WHERE id=%s RETURNING *""", (r["id"],))
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()
    _notify_cancelled(row)
    return row


def link_session(ids, tenant_id, customer_id=None):
    """A request made before sign-up, in the same browser, joins the account."""
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not ids or not tenant_id:
        return
    try:
        conn, cur = _db()
        try:
            cur.execute("""UPDATE setup_requests SET tenant_id=%s, customer_id=COALESCE(customer_id,%s), updated_at=NOW()
                            WHERE id = ANY(%s) AND tenant_id IS NULL""", (tenant_id, customer_id, ids))
            conn.commit()
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ setup_requests.link_session:", e)


def link_pending():
    """Requests without an account join the account with the same email
    (sign-up finished later). Cheap; run on save and on the Admin list."""
    try:
        conn, cur = _db()
        try:
            cur.execute("""UPDATE setup_requests sr SET tenant_id=c.tenant_id, customer_id=c.id
                             FROM customers c
                            WHERE sr.tenant_id IS NULL AND sr.email IS NOT NULL
                              AND LOWER(c.email)=LOWER(sr.email)""")
            conn.commit()
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ setup_requests.link_pending:", e)


# ── Admin ─────────────────────────────────────────────────────────────────

FILTERS = {"open": OPEN, "new": ("new",), "called": ("called", "no_answer"), "done": ("done",),
           "closed": ("done", "not_interested", "cancelled")}


def for_admin(view="open", route=None, rid=None):
    link_pending()
    conn, cur = _db()
    try:
        where, args = [], []
        if rid:
            where.append("sr.id=%s"); args.append(int(rid))
        elif view in FILTERS:
            where.append("sr.status IN %s"); args.append(FILTERS[view])
        if route in ROUTES:
            where.append("sr.route=%s"); args.append(route)
        order = "sr.slot_at ASC" if view in ("open", "new", "called") else "sr.created_at DESC"
        cur.execute(f"""SELECT sr.*, t.name AS tenant_name
                          FROM setup_requests sr
                          LEFT JOIN tenants t ON t.id = sr.tenant_id
                          LEFT JOIN customers c ON c.id = sr.customer_id
                         {('WHERE ' + ' AND '.join(where)) if where else ''}
                         ORDER BY {order} LIMIT 300""", args)
        rows = cur.fetchall()
        cur.execute("SELECT status, COUNT(*) AS n FROM setup_requests GROUP BY status")
        counts = {r["status"]: r["n"] for r in cur.fetchall()}
    finally:
        cur.close(); conn.close()
    counts["open"] = sum(counts.get(s, 0) for s in OPEN)
    return rows, counts


def new_count() -> int:
    try:
        conn, cur = _db(False)
        try:
            cur.execute("SELECT COUNT(*) FROM setup_requests WHERE status='new'")
            return cur.fetchone()[0]
        finally:
            cur.close(); conn.close()
    except Exception:
        return 0


def update(rid, status=None, notes=None, by=""):
    conn, cur = _db()
    try:
        sets, args = ["updated_at=NOW()", "handled_by=%s"], [by[:80] or None]
        if status in STATUS_LABEL:
            sets.append("status=%s"); args.append(status)
        if notes is not None:
            sets.append("notes=%s"); args.append(notes[:2000] or None)
        cur.execute(f"UPDATE setup_requests SET {', '.join(sets)} WHERE id=%s RETURNING *", args + [int(rid)])
        row = cur.fetchone()
        conn.commit()
        return row
    finally:
        cur.close(); conn.close()


def for_customer(customer_id=None, tenant_id=None, email=None):
    conn, cur = _db()
    try:
        cur.execute("""SELECT * FROM setup_requests
                        WHERE (%s IS NOT NULL AND customer_id=%s) OR (%s IS NOT NULL AND tenant_id=%s)
                           OR (%s <> '' AND LOWER(email)=LOWER(%s))
                        ORDER BY created_at DESC LIMIT 20""",
                    (customer_id, customer_id, tenant_id, tenant_id, email or "", email or ""))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


# ── calendar invites (.ics) ───────────────────────────────────────────────

def _ics_text(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics(r, for_team: bool, method="REQUEST") -> bytes:
    start = r["slot_at"].astimezone(timezone.utc)
    end = start + timedelta(minutes=STEP_MIN)
    f = "%Y%m%dT%H%M%SZ"
    how = METHODS[r["method"]]
    if for_team:
        summary = f"Set-up: {r.get('company') or r.get('name') or 'business'} ({how})"
        desc = (f"Contact by: {how}\n{('WhatsApp' if r['method'] == 'wa' else 'Phone' if r['method'] == 'voice' else 'Email')}: "
                f"{contact_value(r)}\nName: {r.get('name') or ''}\nCompany: {r.get('company') or ''}\n"
                f"Route: {ROUTES.get(r['route'], r['route'])}\nAdmin: {PORTAL}/admin/setup-requests?id={r['id']}")
        attendees = ALERT_EMAILS
    else:
        summary = "PhiXtra set-up call"
        desc = {"voice": f"We'll phone you on {plus(r.get('phone'))}.",
                "wa": f"We'll contact you on WhatsApp on {plus(r.get('wa'))}.",
                "email": f"We'll email you at {r.get('email')}."}[r["method"]]
        desc += f"\nChange or cancel: {PORTAL}/setup-help/b/{r['token']}"
        attendees = [r["email"]]
    org = os.getenv("SMTP_REPLY_TO", "support@phixtra.com")
    lines = ["BEGIN:VCALENDAR", "PRODID:-//PhiXtra//Set-up calls//EN", "VERSION:2.0", "CALSCALE:GREGORIAN",
             f"METHOD:{method}", "BEGIN:VEVENT",
             f"UID:setup-{r['id']}-{'team' if for_team else 'biz'}@phixtra.com", f"SEQUENCE:{r.get('seq') or 0}",
             f"DTSTAMP:{_now().strftime(f)}", f"DTSTART:{start.strftime(f)}", f"DTEND:{end.strftime(f)}",
             f"SUMMARY:{_ics_text(summary)}", f"DESCRIPTION:{_ics_text(desc)}",
             f"ORGANIZER;CN=PhiXtra:mailto:{org}"]
    lines += [f"ATTENDEE;ROLE=REQ-PARTICIPANT;RSVP=FALSE:mailto:{a}" for a in attendees if a]
    lines += [f"STATUS:{'CANCELLED' if method == 'CANCEL' else 'CONFIRMED'}", "END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(_fold(l) for l in lines) + "\r\n").encode()


def _fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets continue on the next line after a space."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode()); cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


def _esc(s):
    import html
    return html.escape(str(s or ""))


def _send(to, subject, html_body, text_body, ics: bytes = None, method="REQUEST"):
    try:
        from portal_utils import send_email, send_email_with_attachment
        if ics:
            return send_email_with_attachment(to, subject, html_body, ics, "invite.ics",
                                              "text/calendar", text_body=text_body, from_name="PhiXtra",
                                              reply_to=os.getenv("SMTP_REPLY_TO", "support@phixtra.com"),
                                              calendar_method=method)
        return send_email(to, subject, html_body, text_body)
    except Exception as e:
        print("⚠️ setup_requests email:", e)
        return False


def _team_email(r, headline, note=""):
    how = METHODS[r["method"]]
    val = contact_value(r)
    who = r.get("company") or r.get("name") or "A business"
    rows = [("Business", r.get("company")), ("Person", r.get("name")),
            ("Phone", plus(r.get("phone"))), ("WhatsApp", plus(r.get("wa"))), ("Email", r.get("email")),
            ("Route", f"{ROUTES.get(r['route'], r['route'])}" + (f" · {r['signup_choice']}" if r.get("signup_choice") else "")
                      + (f" · {PLACES.get(r.get('place'), '')}" if r.get("place") in PLACES else "")),
            ("Store", r.get("store_url")),
            ("Account", f"t{r['tenant_id']}" if r.get("tenant_id") else "Not signed up yet"),
            ("Their time", when_text(r["slot_at"], r.get("tz"), True) if (r.get("tz") or "Europe/London") != "Europe/London" else "")]
    tr = "".join(f"<tr><td style='color:#64748b;padding:4px 14px 4px 0;vertical-align:top'>{k}</td>"
                 f"<td style='font-weight:600;color:#0f172a;padding:4px 0'>{_esc(v)}</td></tr>" for k, v in rows if v)
    link = f"{PORTAL}/admin/setup-requests?id={r['id']}"
    html_body = f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#1f2933">
<div style="background:#ecfdf3;border:1px solid #a7e3c1;border-radius:10px;padding:14px 16px;font-size:16px;color:#064e2b;line-height:1.5">
Contact by <b>{_esc(how)}</b> on <b>{_esc(val)}</b><br>{_esc(when_text(r['slot_at'], None, True))}</div>
<h2 style="font-size:18px;margin:18px 0 6px">{_esc(headline)}</h2>
{f'<p style="margin:0 0 10px;color:#334155">{_esc(note)}</p>' if note else ''}
<table style="border-collapse:collapse;font-size:14px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;padding:10px;width:100%">{tr}</table>
<p style="margin:18px 0"><a href="{link}" style="background:#0D2540;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600">Open in Admin</a></p>
<p style="font-size:12px;color:#94a3b8">The calendar invite is attached. Both of you get it; one of you takes the call.</p></div>"""
    text_body = (f"Contact by {how} on {val}\n{when_text(r['slot_at'], None, True)}\n\n{headline}\n{note}\n\n"
                 + "\n".join(f"{k}: {v}" for k, v in rows if v) + f"\n\nOpen in Admin: {link}")
    verb = {"voice": "Call", "wa": "WhatsApp", "email": "Email"}[r["method"]]
    return who, verb, html_body, text_body


def _biz_email(r, title, text):
    t = thanks_text(r)
    html_body = f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;padding:20px;color:#1f2933">
<h2 style="font-size:20px;margin:0 0 8px">{_esc(title)}</h2>
<p style="font-size:15px;margin:0 0 14px">{_esc(text)}</p>
<p style="margin:0 0 14px;font-size:14px">Add it to your calendar with the attached invite (Google, Outlook or Apple).</p>
<p style="margin:0 0 18px"><a href="{t['manage']}" style="color:#0b6b43;font-weight:600">Change or cancel the time</a></p>
<p style="font-size:13px;color:#64748b">PhiXtra · we set it up with you, free, so you start catching every enquiry.</p></div>"""
    return html_body, f"{title}\n{text}\n\nChange or cancel: {t['manage']}"


def _notify_new(r):
    who, verb, h, t = _team_email(r, f"{r.get('company') or r.get('name') or 'A business'} wants us to set it up")
    subj = f"{verb} them {when_text(r['slot_at'])}: {who} wants us to set it up ({ROUTES.get(r['route'], r['route'])})"
    ics = _ics(r, True)
    for to in ALERT_EMAILS:
        _send(to, subj, h, t, ics)
    if r.get("email"):
        how = thanks_text(r)["text"].split(" A calendar")[0]
        bh, bt = _biz_email(r, f"Your PhiXtra set-up call: {when_text(r['slot_at'], r.get('tz'), True)}", how)
        _send(r["email"], f"Your PhiXtra set-up call: {when_text(r['slot_at'], r.get('tz'))}", bh, bt, _ics(r, False))
    send_whatsapp_alerts(r)


def _notify_changed(r, old_at):
    who, verb, h, t = _team_email(r, f"{who_of(r)} moved their set-up call",
                                  f"Was {when_text(old_at)}. Now {when_text(r['slot_at'])}.")
    subj = f"Moved: {verb} them {when_text(r['slot_at'])}: {who}"
    ics = _ics(r, True)
    for to in ALERT_EMAILS:
        _send(to, subj, h, t, ics)
    if r.get("email"):
        bh, bt = _biz_email(r, f"Your set-up call is now {when_text(r['slot_at'], r.get('tz'), True)}",
                            thanks_text(r)["text"].split(" A calendar")[0])
        _send(r["email"], f"Your PhiXtra set-up call is now {when_text(r['slot_at'], r.get('tz'))}", bh, bt, _ics(r, False))


def _notify_cancelled(r):
    who, verb, h, t = _team_email(r, f"{who_of(r)} cancelled their set-up call",
                                  "They cancelled from the link in their invite. Nothing to do unless you want to follow up.")
    ics = _ics(r, True, "CANCEL")
    for to in ALERT_EMAILS:
        _send(to, f"Cancelled: set-up call {when_text(r['slot_at'])}: {who}", h, t, ics, "CANCEL")
    if r.get("email"):
        bh, bt = _biz_email(r, "Your set-up call is cancelled",
                            "You can book a new time whenever you like from your PhiXtra account.")
        _send(r["email"], "Your PhiXtra set-up call is cancelled", bh, bt, _ics(r, False, "CANCEL"), "CANCEL")


def who_of(r):
    return r.get("company") or r.get("name") or "A business"


# ── WhatsApp alert (Account Manager number, Utility template) ─────────────

def template_definition() -> dict:
    return {
        "name": TEMPLATE, "language": "en", "category": "UTILITY",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "Set-up help wanted"},
            {"type": "BODY",
             "text": ("*Contact by: {{1}}*\n*{{2}}*\n*When: {{3}} (UK)*\n\n"
                      "Business: {{4}}\nRoute: {{5}}\n\nPlease contact them at the booked time."),
             "example": {"body_text": [["WhatsApp call or chat", "WhatsApp: +44 7700 900123", "Tue 17 Nov, 10:00",
                                        "Ade Foods Ltd (Ade Bello)", "Shopify · B2B"]]}},
            {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "Open in Admin",
                                             "url": PORTAL + "/admin/setup-requests?id={{1}}",
                                             "example": [PORTAL + "/admin/setup-requests?id=12"]}]},
        ],
    }


def _am_number():
    try:
        import admin_accounts
        return admin_accounts.am_number()
    except Exception as e:
        print("⚠️ setup_requests am_number:", e)
        return None


_tpl_cache = {"at": None, "status": None}


def template_status():
    """Meta's status for TEMPLATE on the Account Manager number (cached 10 min)."""
    now = _now()
    if _tpl_cache["at"] and now - _tpl_cache["at"] < timedelta(minutes=10):
        return _tpl_cache["status"]
    st = None
    n = _am_number()
    if n:
        try:
            r = requests.get(f"{GRAPH}/{n['waba_id']}/message_templates", params={"name": TEMPLATE, "fields": "name,status"},
                             headers={"Authorization": f"Bearer {n['access_token']}"}, timeout=8).json()
            st = next((t.get("status") for t in r.get("data", []) if t.get("name") == TEMPLATE), None)
        except Exception as e:
            print("⚠️ setup_requests template check:", e)
            return _tpl_cache["status"]
    _tpl_cache.update(at=now, status=st)
    return st


def submit_template():
    """Submit TEMPLATE to Meta once (run by hand). Returns Meta's reply."""
    n = _am_number()
    if not n:
        return {"error": "No Account Manager number."}
    r = requests.post(f"{GRAPH}/{n['waba_id']}/message_templates", json=template_definition(),
                      headers={"Authorization": f"Bearer {n['access_token']}"}, timeout=20)
    _tpl_cache.update(at=None)
    return r.json()


def send_whatsapp_alerts(r):
    """Never raises. Skipped (email still goes) until Meta approves TEMPLATE."""
    try:
        if template_status() != "APPROVED":
            return
        n = _am_number()
        if not n:
            return
        label = {"wa": "WhatsApp", "voice": "Phone", "email": "Email"}[r["method"]]
        biz = r.get("company") or r.get("name") or "A business"
        if r.get("company") and r.get("name"):
            biz = f"{r['company']} ({r['name']})"
        route = ROUTES.get(r["route"], r["route"]) + (f" · {r['signup_choice']}" if r.get("signup_choice") else "")
        params = [METHODS[r["method"]], f"{label}: {contact_value(r)}", when_text(r["slot_at"]), biz, route]
        comps = [{"type": "body", "parameters": [{"type": "text", "text": (p or "-")[:200]} for p in params]},
                 {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": str(r["id"])}]}]
        for to in ALERT_PHONES:
            try:
                resp = requests.post(f"{GRAPH}/{n['phone_number_id']}/messages",
                                     json={"messaging_product": "whatsapp", "to": to, "type": "template",
                                           "template": {"name": TEMPLATE, "language": {"code": "en"}, "components": comps}},
                                     headers={"Authorization": f"Bearer {n['access_token']}"}, timeout=15)
                if resp.status_code != 200:
                    print(f"⚠️ setup_requests WA alert {to}: {resp.text[:300]}")
            except Exception as e:
                print(f"⚠️ setup_requests WA alert {to}: {e}")
    except Exception as e:
        print("⚠️ setup_requests send_whatsapp_alerts:", e)
