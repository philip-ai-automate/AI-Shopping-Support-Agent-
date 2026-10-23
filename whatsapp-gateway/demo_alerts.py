"""
demo_alerts.py — Microsoft Bookings demo alerts (2026-10-22, every business).

Runs every minute from the gateway scheduler. Reads what the portal's
Bookings checker saved (booking_appointments / booking_events, portal
bookings_routes.py) and tells people, by email, WhatsApp and phone
notification (never SMS):

  booked / moved / cancelled   managers + the lead's salesperson, within minutes
  30 minutes before            managers + salesperson
  1 hour after it ends         the salesperson (managers if unassigned): "How did it go?"
  08:00 each day               each person's digest: today's demos, outcomes still
                               missing, next steps due today
  Monday 08:00                 managers: last week in numbers (email)

Managers = the account owner + team members whose role has "See all demos"
(crm.demos_all). The salesperson = the lead's assigned team member.
Every alert is claimed once in demo_alert_log (kind + ref unique), so a
restart or a second worker never sends it twice.
"""
import asyncio
import html as _html
import json
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import psycopg2.extras

import push_notify
from wa_db import get_db_connection
from staff_alerts import _send_whatsapp

_PORTAL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")
_COUNTRY_TZ = {"NG": "Africa/Lagos", "GB": "Europe/London", "GH": "Africa/Accra", "KE": "Africa/Nairobi",
               "ZA": "Africa/Johannesburg", "AE": "Asia/Dubai", "IE": "Europe/Dublin", "CM": "Africa/Douala",
               "SN": "Africa/Dakar", "CI": "Africa/Abidjan", "RW": "Africa/Kigali", "UG": "Africa/Kampala",
               "TZ": "Africa/Dar_es_Salaam", "EG": "Africa/Cairo", "IN": "Asia/Kolkata"}
DIGEST_HOUR = 8
REMIND_BEFORE = timedelta(minutes=30)
ASK_AFTER = timedelta(hours=1)
EVENT_KINDS = ("booked", "moved", "cancelled")
OUTCOME_NAMES = {"interested": "Interested", "proposal": "Proposal sent", "another": "Another meeting",
                 "not_fit": "Not a fit", "no_show": "Didn't show up", "done": "Done"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _ready(cur) -> bool:
    cur.execute("SELECT to_regclass('demo_alert_log') IS NOT NULL AS a, to_regclass('booking_appointments') IS NOT NULL AS b")
    r = cur.fetchone()
    return bool(r["a"] and r["b"])


def _claim(cur, tenant_id: int, kind: str, ref: str) -> bool:
    """True the first time (kind, ref) is seen — that caller sends the alert."""
    cur.execute("""INSERT INTO demo_alert_log (tenant_id, kind, ref) VALUES (%s,%s,%s)
                   ON CONFLICT (kind, ref) DO NOTHING RETURNING id""", (tenant_id, kind, ref))
    return bool(cur.fetchone())


def _tz(cur, tenant_id: int) -> ZoneInfo:
    cur.execute("SELECT timezone, business_country FROM customers WHERE tenant_id=%s ORDER BY created_at ASC LIMIT 1",
                (tenant_id,))
    r = cur.fetchone() or {}
    for name in (r.get("timezone"), _COUNTRY_TZ.get((r.get("business_country") or "").upper())):
        try:
            if name:
                return ZoneInfo(name)
        except Exception:
            pass
    return ZoneInfo("UTC")


def _when(dt, tz, day=True) -> str:
    if not dt:
        return ""
    t = dt.astimezone(tz)
    return t.strftime("%a %-d %b, %-I:%M %p " if day else "%-I:%M %p ").replace("AM", "am").replace("PM", "pm") + (t.tzname() or "")


def _perms(raw) -> dict:
    if isinstance(raw, str):
        try:
            return json.loads(raw or "{}")
        except Exception:
            return {}
    return raw or {}


def _managers(cur, tenant_id: int) -> list:
    out = []
    cur.execute("""SELECT COALESCE(NULLIF(c.handoff_notify_email,''), c.email) AS email,
                          COALESCE(NULLIF(c.first_name,''), 'there') AS name
                     FROM customers c WHERE c.tenant_id=%s AND c.is_active ORDER BY c.id LIMIT 1""", (tenant_id,))
    o = cur.fetchone()
    if o:
        out.append({"kind": "owner", "id": None, "name": o["name"], "email": o["email"], "phone": None})
    cur.execute("""SELECT tm.id, tm.name, tm.email, tm.alert_phone, tm.alert_enabled, r.permissions
                     FROM team_members tm LEFT JOIN tenant_roles r ON r.id=tm.role_id AND r.tenant_id=tm.tenant_id
                    WHERE tm.tenant_id=%s AND tm.is_active""", (tenant_id,))
    for m in cur.fetchall():
        if _perms(m["permissions"]).get("crm.demos_all"):
            out.append(_member(m))
    return out


def _member(m) -> dict:
    return {"kind": "member", "id": m["id"], "name": m["name"] or "", "email": m["email"],
            "phone": m["alert_phone"] if m.get("alert_enabled") else None}


def _salesperson(cur, tenant_id: int, assigned_key: str):
    if not assigned_key or not assigned_key.startswith("team:"):
        return None
    try:
        mid = int(assigned_key.split(":", 1)[1])
    except ValueError:
        return None
    cur.execute("""SELECT id, name, email, alert_phone, alert_enabled FROM team_members
                    WHERE id=%s AND tenant_id=%s AND is_active""", (mid, tenant_id))
    m = cur.fetchone()
    return _member(m) if m else None


def _merge(*groups) -> list:
    """One entry per person: members by id, the owner by email; a member
    with the owner's email isn't sent the same thing twice."""
    seen, out = set(), []
    for g in groups:
        for r in g or []:
            if not r:
                continue
            keys = {("email", (r.get("email") or "").lower())} if r.get("email") else set()
            if r["kind"] == "member":
                keys.add(("member", r["id"]))
            if keys & seen:
                continue
            seen |= keys
            out.append(r)
    return out


def _email(to: str, subject: str, heading: str, rows: list, button: tuple, footer: str, kicker: str = "Demo bookings"):
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("SMTP_FROM", user or "no-reply@phixtra.com").strip()
    if not host or not to:
        return
    trs = "".join(
        f'<tr><td style="padding:7px 10px;border:1px solid #E3E8F1;background:#F5F7FB;font-weight:700;width:140px;font-size:13px;vertical-align:top">{_html.escape(k)}</td>'
        f'<td style="padding:7px 10px;border:1px solid #E3E8F1;font-size:13.5px">{_html.escape(v)}</td></tr>' for k, v in rows)
    label, link = button
    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111E2D">
      <div style="background:#0B1D40;padding:16px 20px;border-radius:12px 12px 0 0">
        <p style="color:#9DB8E8;font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;margin:0 0 4px">{_html.escape(kicker)}</p>
        <h2 style="color:#fff;margin:0;font-size:19px">{_html.escape(heading)}</h2>
      </div>
      <div style="border:1px solid #E3E8F1;border-top:none;border-radius:0 0 12px 12px;padding:18px 20px">
        {'<table style="border-collapse:collapse;width:100%;margin:0 0 16px">' + trs + '</table>' if rows else ''}
        <a href="{link}" style="display:inline-block;background:#0B1D40;color:#fff;padding:11px 18px;border-radius:10px;text-decoration:none;font-weight:800">{_html.escape(label)}</a>
        <p style="color:#5A6B85;font-size:12.5px;margin-top:14px">{_html.escape(footer)}</p>
      </div>
    </div>"""
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content("\n".join(f"{k}: {v}" for k, v in rows) + f"\n\n{label}: {link}\n\n{footer}")
    msg.add_alternative(body, subtype="html")
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    except Exception as e:
        print(f"⚠️ [DEMO ALERT] email to {to} failed: {e}")


async def _deliver(tenant_id: int, people: list, *, title: str, line: str, rows: list,
                   url: str, button: str, footer: str, subject: str = None, tag: str = "") -> None:
    """Email + WhatsApp + phone notification to each person (no SMS)."""
    link = url if url.startswith("http") else _PORTAL + url
    for p in people:
        if p.get("email"):
            await asyncio.to_thread(_email, p["email"], subject or title, title, rows, (button, link), footer)
        if p.get("phone"):
            text = f"*{title}*\n{line}\n\n👉 {link}"
            try:
                await _send_whatsapp(p["phone"], "staff_demo_alert", [title, line, link], text)
            except Exception as e:
                print(f"⚠️ [DEMO ALERT] WhatsApp to member {p.get('id')} failed: {e}")
    member_ids = [p["id"] for p in people if p["kind"] == "member" and p.get("id")]
    include_owner = any(p["kind"] == "owner" for p in people)
    try:
        await push_notify.push_to(tenant_id, member_ids, include_owner, title=title[:120], body=line[:180],
                                  url=url, tag=tag or url)
    except Exception as e:
        print(f"⚠️ [DEMO ALERT] phone notifications tenant={tenant_id} failed: {e}")


def _appt_rows(a, tz, assigned_label: str) -> list:
    rows = [("Who", " · ".join(x for x in (a.get("customer_name"), a.get("email"), a.get("phone")) if x) or "-"),
            ("Company", a.get("company") or "-"),
            ("When", _when(a["start_at"], tz)),
            ("Salesperson", assigned_label)]
    if a.get("join_url"):
        rows.append(("Join link", a["join_url"]))
    return rows


def _assigned_label(a) -> str:
    return a.get("assigned_to") or "Unassigned: please assign this lead"


_APPT_SQL = """SELECT a.*, l.customer_name AS company, l.assigned_key, l.assigned_to
                 FROM booking_appointments a LEFT JOIN merchant_pipeline_leads l ON l.id = a.lead_id"""


# ── 1. booked / moved / cancelled ─────────────────────────────────────────────

async def _event_alerts(cur, conn):
    cur.execute("""SELECT e.id, e.kind, e.detail, e.tenant_id, e.appointment_id, e.created_at
                     FROM booking_events e
                    WHERE e.kind = ANY(%s) AND e.created_at > NOW() - INTERVAL '2 hours'
                      AND e.created_at >= (SELECT COALESCE(MIN(created_at), NOW()) FROM demo_alert_log WHERE kind='start')
                      AND NOT EXISTS (SELECT 1 FROM demo_alert_log g WHERE g.kind='event' AND g.ref = e.id::text)
                    ORDER BY e.id LIMIT 50""", (list(EVENT_KINDS),))
    for ev in cur.fetchall():
        if not _claim(cur, ev["tenant_id"], "event", str(ev["id"])):
            conn.commit()
            continue
        conn.commit()
        cur.execute(_APPT_SQL + " WHERE a.id=%s", (ev["appointment_id"],))
        a = cur.fetchone()
        if not a:
            continue
        tz = _tz(cur, ev["tenant_id"])
        people = _merge(_managers(cur, ev["tenant_id"]), [_salesperson(cur, ev["tenant_id"], a["assigned_key"])])
        name = a["customer_name"] or a["email"] or "Someone"
        if ev["kind"] == "booked":
            title = f"📅 Demo booked: {name}"
            line = f"{_when(a['start_at'], tz)} · {_assigned_label(a)}"
        elif ev["kind"] == "moved":
            title = f"🔁 Demo moved: {name}"
            line = f"Now {_when(a['start_at'], tz)}" + (f" (was {_when(a['moved_from'], tz)})" if a.get("moved_from") else "")
        else:
            title = f"❌ Demo cancelled: {name}"
            line = f"Was {_when(a['start_at'], tz)}. The lead is back on the follow-up list."
        await _deliver(ev["tenant_id"], people, title=title, line=line, rows=_appt_rows(a, tz, _assigned_label(a)),
                       url=f"/leads/{a['lead_id']}", button="Open the lead",
                       footer="You get this because you manage demos or this lead is yours.", tag=f"demo-{a['id']}")


# ── 2. 30 minutes before, 3. one hour after ───────────────────────────────────

async def _timed_alerts(cur, conn):
    now = datetime.now(timezone.utc)
    cur.execute(_APPT_SQL + """ WHERE a.status='booked' AND a.start_at > %s AND a.start_at <= %s
                  AND NOT EXISTS (SELECT 1 FROM demo_alert_log g WHERE g.kind='remind' AND g.ref=a.id::text)""",
                (now, now + REMIND_BEFORE + timedelta(minutes=1)))
    for a in cur.fetchall():
        if not _claim(cur, a["tenant_id"], "remind", str(a["id"])):
            conn.commit(); continue
        conn.commit()
        tz = _tz(cur, a["tenant_id"])
        people = _merge(_managers(cur, a["tenant_id"]), [_salesperson(cur, a["tenant_id"], a["assigned_key"])])
        name = a["customer_name"] or a["email"] or "a prospect"
        await _deliver(a["tenant_id"], people, title=f"⏰ Demo in 30 minutes: {name}",
                       line=f"{_when(a['start_at'], tz, day=False)} · {_assigned_label(a)}",
                       rows=_appt_rows(a, tz, _assigned_label(a)), url=f"/leads/{a['lead_id']}",
                       button="Open the lead", footer="A reminder 30 minutes before each demo.", tag=f"demo-{a['id']}")
    cur.execute(_APPT_SQL + """ WHERE a.status='booked' AND COALESCE(a.end_at, a.start_at + INTERVAL '30 minutes') + %s <= %s
                  AND COALESCE(a.end_at, a.start_at) > %s - INTERVAL '24 hours'
                  AND NOT EXISTS (SELECT 1 FROM demo_alert_log g WHERE g.kind='ask' AND g.ref=a.id::text)""",
                (ASK_AFTER, now, now))
    for a in cur.fetchall():
        if not _claim(cur, a["tenant_id"], "ask", str(a["id"])):
            conn.commit(); continue
        conn.commit()
        tz = _tz(cur, a["tenant_id"])
        sp = _salesperson(cur, a["tenant_id"], a["assigned_key"])
        people = [sp] if sp else _managers(cur, a["tenant_id"])
        name = a["customer_name"] or a["email"] or "the prospect"
        await _deliver(a["tenant_id"], people, title=f"❓ How did the demo go? {name}",
                       line="Record the outcome on the lead: it moves the pipeline for you.",
                       rows=_appt_rows(a, tz, _assigned_label(a)), url=f"/leads/{a['lead_id']}#demo",
                       button="Record the outcome",
                       footer="If it isn't recorded by tomorrow morning, your manager is told.", tag=f"demo-{a['id']}")


# ── 4. 08:00 daily digest, 5. Monday summary ──────────────────────────────────

def _local_now(tz):
    return datetime.now(timezone.utc).astimezone(tz)


async def _digests(cur, conn):
    cur.execute("SELECT DISTINCT tenant_id FROM booking_connections WHERE status='active'")
    for (tid,) in [(r["tenant_id"],) for r in cur.fetchall()]:
        tz = _tz(cur, tid)
        local = _local_now(tz)
        if local.hour < DIGEST_HOUR:
            continue
        day = local.date()
        start = datetime(day.year, day.month, day.day, tzinfo=tz)
        if _claim(cur, tid, "digest", f"{tid}:{day.isoformat()}"):
            conn.commit()
            await _send_digest(cur, tid, tz, start)
        else:
            conn.commit()
        if local.weekday() == 0 and _claim(cur, tid, "weekly", f"{tid}:{day.isocalendar()[0]}-{day.isocalendar()[1]}"):
            conn.commit()
            await _send_weekly(cur, tid, tz, start)
        else:
            conn.commit()


async def _send_digest(cur, tid, tz, start):
    end = start + timedelta(days=1)
    cur.execute(_APPT_SQL + " WHERE a.tenant_id=%s AND a.status='booked' AND a.start_at >= %s AND a.start_at < %s ORDER BY a.start_at",
                (tid, start, end))
    today = cur.fetchall()
    cur.execute(_APPT_SQL + """ WHERE a.tenant_id=%s AND a.status='booked' AND a.start_at < %s
                  AND a.start_at > %s - INTERVAL '60 days' ORDER BY a.start_at""", (tid, start, start))
    missing = cur.fetchall()
    nexts = []
    if _table(cur, "demo_outcomes"):
        cur.execute("""SELECT o.*, l.customer_name AS company, l.assigned_key, l.assigned_to
                         FROM demo_outcomes o JOIN merchant_pipeline_leads l ON l.id=o.lead_id
                        WHERE o.tenant_id=%s AND o.next_date = %s""", (tid, start.date()))
        nexts = cur.fetchall()
    if not (today or missing or nexts):
        return
    managers = _merge(_managers(cur, tid))
    sends = [(p, today, missing, nexts) for p in managers]       # managers see everything
    by_sp = {}
    for group, rows in (("today", today), ("missing", missing), ("nexts", nexts)):
        for a in rows:
            sp = _salesperson(cur, tid, a.get("assigned_key"))
            if sp:
                by_sp.setdefault(sp["id"], {"p": sp, "today": [], "missing": [], "nexts": []})[group].append(a)
    taken = {("member", p["id"]) for p in managers if p["kind"] == "member"} | {
        ("email", (p.get("email") or "").lower()) for p in managers if p.get("email")}
    for v in by_sp.values():                                     # salespeople see their own
        sp = v["p"]
        if ("member", sp["id"]) in taken or ("email", (sp.get("email") or "").lower()) in taken:
            continue
        sends.append((sp, v["today"], v["missing"], v["nexts"]))
    for p, t, m, n in sends:
        if not (t or m or n):
            continue
        rows = [(f"{_when(a['start_at'], tz, day=False)}", f"{a['customer_name'] or a['email'] or '-'} · {a.get('company') or ''} · {_assigned_label(a)}")
                for a in t]
        rows += [("Outcome missing", f"{a['customer_name'] or a['email'] or '-'} · demo {_when(a['start_at'], tz)} · {_assigned_label(a)}")
                 for a in m]
        rows += [("Next step today", f"{a.get('company') or '-'}: {a.get('next_step') or 'follow up'} · {a.get('assigned_to') or 'Unassigned'}")
                 for a in n]
        bits = []
        if t:
            bits.append(f"{len(t)} demo{'s' if len(t) != 1 else ''} today")
        if m:
            bits.append(f"{len(m)} outcome{'s' if len(m) != 1 else ''} missing")
        if n:
            bits.append(f"{len(n)} next step{'s' if len(n) != 1 else ''} due")
        line = " · ".join(bits)
        await _deliver(tid, [p], title=f"📋 Demos today: {line}", line=line, rows=rows, url="/demos",
                       button="Open Demos", footer="Sent at 8am on days with demos, missing outcomes or next steps.",
                       subject=f"Demos today: {line}", tag=f"demo-digest-{start.date()}")


async def _send_weekly(cur, tid, tz, start):
    wk_start = start - timedelta(days=7)
    cur.execute(_APPT_SQL + " WHERE a.tenant_id=%s AND a.start_at >= %s AND a.start_at < %s", (tid, wk_start, start))
    rows_ = cur.fetchall()
    cur.execute("SELECT count(*) AS n FROM booking_appointments WHERE tenant_id=%s AND booked_at >= %s AND booked_at < %s",
                (tid, wk_start, start))
    booked = cur.fetchone()["n"]
    attended = [a for a in rows_ if a["status"] == "done"]
    no_show = [a for a in rows_ if a["status"] == "no_show"]
    missing = [a for a in rows_ if a["status"] == "booked"]
    moved_on = 0
    if _table(cur, "demo_outcomes"):
        cur.execute("""SELECT count(*) AS n FROM demo_outcomes WHERE tenant_id=%s AND outcome IN ('interested','proposal')
                        AND created_at >= %s AND created_at < %s""", (tid, wk_start, start))
        moved_on = cur.fetchone()["n"]
    held = len(attended) + len(no_show)
    show = f"{round(100 * len(attended) / held)}%" if held else "-"
    by = {}
    for a in rows_:
        k = a.get("assigned_to") or "Unassigned"
        d = by.setdefault(k, [0, 0, 0, 0])
        d[0] += 1
        d[1] += a["status"] == "done"
        d[2] += a["status"] == "no_show"
        d[3] += a["status"] == "booked"
    rows = [("Booked last week", str(booked)), ("Demos held", f"{len(attended)} attended · {len(no_show)} no-show"),
            ("Show-up rate", show), ("Outcome missing", str(len(missing))),
            ("Moved forward", f"{moved_on} to Qualified or Proposal Sent")]
    rows += [(k, f"{v[0]} demos · {v[1]} attended · {v[2]} no-show · {v[3]} missing") for k, v in sorted(by.items())]
    day0 = wk_start.astimezone(tz).strftime("%-d %b")
    day1 = (start - timedelta(days=1)).astimezone(tz).strftime("%-d %b")
    for p in _merge(_managers(cur, tid)):
        if p.get("email"):
            await asyncio.to_thread(_email, p["email"], f"Demos last week ({day0} – {day1})",
                                    f"Demos last week: {day0} – {day1}", rows, ("Open Demos", _PORTAL + "/demos"),
                                    "Sent every Monday at 8am to the owner and managers.", "Weekly demo summary")


def _table(cur, name) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (name,))
    return bool(cur.fetchone()["ok"])


# ── entry point ───────────────────────────────────────────────────────────────

async def run_demo_alerts():
    conn = get_db_connection()
    if not conn:
        return
    # Every statement commits on its own: the alerts wait on email/WhatsApp
    # sends, and an open transaction then would block the portal's start-up
    # changes to team_members (froze the portal for 6 minutes, 22 Oct).
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if not _ready(cur):
            return
        cur.execute("INSERT INTO demo_alert_log (tenant_id, kind, ref) VALUES (0,'start','start') ON CONFLICT DO NOTHING")
        conn.commit()
        for step in (_event_alerts, _timed_alerts, _digests):
            try:
                await step(cur, conn)
                conn.commit()
            except Exception as e:
                print(f"⚠️ [DEMO ALERT] {step.__name__} error: {e}")
    finally:
        cur.close()
        conn.close()
