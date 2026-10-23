"""
followup_alerts.py — morning follow-up reminders (2026-10-23, every business).

Each lead has a next follow-up date (portal lead_followups.py). At 08:00 on
working days, in each business's own time zone:

  each salesperson      their follow-ups due today + any overdue (email,
                        WhatsApp, phone notification, by their alert settings)
  owner + line manager  follow-ups that are overdue (the date has passed and
                        nobody contacted the lead), per person; the owner
                        also gets unassigned leads

Every send is claimed once in followup_alert_log (kind + ref unique), so a
restart or a second worker never sends it twice. Uses autocommit: the
sends wait on email/WhatsApp, and an open transaction would block the
portal's start-up changes (see demo_alerts.py).
"""
import asyncio
from datetime import datetime, timezone

import psycopg2.extras

import push_notify
from wa_db import get_db_connection
from staff_alerts import _send_whatsapp
from demo_alerts import _email, _merge, _member, _tz, _PORTAL

REMIND_HOUR = 8
MAX_ROWS = 25


def _ensure(cur) -> bool:
    cur.execute("SELECT to_regclass('lead_followups') IS NOT NULL AS ok")
    if not cur.fetchone()["ok"]:
        return False
    cur.execute("SELECT to_regclass('followup_alert_log') IS NOT NULL AS ok")
    if not cur.fetchone()["ok"]:
        cur.execute("SET lock_timeout = '5s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS followup_alert_log (
                           id         BIGSERIAL PRIMARY KEY,
                           tenant_id  INTEGER NOT NULL,
                           kind       VARCHAR(20) NOT NULL,
                           ref        VARCHAR(120) NOT NULL,
                           created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           UNIQUE (kind, ref))""")
        cur.execute("RESET lock_timeout")
    return True


def _claim(cur, tenant_id: int, kind: str, ref: str) -> bool:
    cur.execute("""INSERT INTO followup_alert_log (tenant_id, kind, ref) VALUES (%s,%s,%s)
                   ON CONFLICT (kind, ref) DO NOTHING RETURNING id""", (tenant_id, kind, ref))
    return bool(cur.fetchone())


def _owner(cur, tenant_id: int):
    cur.execute("""SELECT id, COALESCE(NULLIF(handoff_notify_email,''), email) AS email,
                          COALESCE(NULLIF(first_name,''), 'there') AS name
                     FROM customers WHERE tenant_id=%s AND is_active ORDER BY id LIMIT 1""", (tenant_id,))
    o = cur.fetchone()
    return {"kind": "owner", "id": None, "name": o["name"], "email": o["email"], "phone": None} if o else None


def _person(cur, tenant_id: int, key: str):
    """The salesperson behind an assigned_key, and their line manager."""
    if not key:
        return None, None
    kind, _, raw = key.partition(":")
    if not raw.isdigit():
        return None, None
    if kind == "team":
        cur.execute("""SELECT id, name, email, alert_phone, alert_enabled, line_manager_id FROM team_members
                        WHERE id=%s AND tenant_id=%s AND is_active""", (int(raw), tenant_id))
        m = cur.fetchone()
        if not m:
            return None, None
        mgr = None
        if m["line_manager_id"]:
            cur.execute("""SELECT id, name, email, alert_phone, alert_enabled FROM team_members
                            WHERE id=%s AND tenant_id=%s AND is_active""", (m["line_manager_id"], tenant_id))
            lm = cur.fetchone()
            mgr = _member(lm) if lm else None
        return _member(m), mgr
    if kind == "owner":
        cur.execute("""SELECT COALESCE(NULLIF(handoff_notify_email,''), email) AS email,
                              COALESCE(NULLIF(first_name,''), 'there') AS name
                         FROM customers WHERE id=%s AND tenant_id=%s""", (int(raw), tenant_id))
        o = cur.fetchone()
        return ({"kind": "owner", "id": None, "name": o["name"], "email": o["email"], "phone": None} if o else None), None
    return None, None


async def _deliver(tenant_id: int, people: list, *, title: str, line: str, rows: list,
                   url: str, button: str, footer: str, subject: str, tag: str) -> None:
    """Email + WhatsApp + phone notification to each person (no SMS)."""
    link = _PORTAL + url
    for p in people:
        if p.get("email"):
            await asyncio.to_thread(_email, p["email"], subject, title, rows, (button, link), footer, "Follow-ups")
        if p.get("phone"):
            text = f"*{title}*\n" + "\n".join(f"• {v}" for _, v in rows[:10]) + f"\n\n👉 {link}"
            try:
                await _send_whatsapp(p["phone"], "staff_followup_alert", [title, line, link], text)
            except Exception as e:
                print(f"⚠️ [FOLLOW-UP ALERT] WhatsApp to member {p.get('id')} failed: {e}")
    member_ids = [p["id"] for p in people if p["kind"] == "member" and p.get("id")]
    include_owner = any(p["kind"] == "owner" for p in people)
    try:
        await push_notify.push_to(tenant_id, member_ids, include_owner, title=title[:120], body=line[:180],
                                  url=url, tag=tag)
    except Exception as e:
        print(f"⚠️ [FOLLOW-UP ALERT] phone notifications tenant={tenant_id} failed: {e}")


def _row(r, today) -> tuple:
    n = (today - r["due_date"]).days
    when = "today" if n == 0 else f"{n} day{'s' if n != 1 else ''} overdue"
    what = r["note"] or "follow up"
    how = {"call": "Call", "whatsapp": "WhatsApp", "email": "Email", "linkedin": "LinkedIn",
           "meeting": "Meeting"}.get(r.get("channel") or "", "Call")
    return (r["due_date"].strftime("%a %-d %b"), f"{r['customer_name']} · {how} · {what} · {when}")


async def _send_tenant(cur, tid, tz, today):
    cur.execute("""SELECT f.lead_id, f.due_date, f.note, f.channel, l.customer_name, l.assigned_key, l.assigned_to
                     FROM lead_followups f JOIN merchant_pipeline_leads l ON l.id=f.lead_id AND l.tenant_id=f.tenant_id
                    WHERE f.tenant_id=%s AND f.done_at IS NULL AND f.cancelled_at IS NULL
                      AND l.outcome IS NULL AND l.dropped_at IS NULL AND f.due_date <= %s
                    ORDER BY f.due_date, l.customer_name""", (tid, today))
    rows = cur.fetchall()
    if not rows:
        return
    by_key = {}
    for r in rows:
        by_key.setdefault(r["assigned_key"] or "", []).append(r)

    # 1. each salesperson: today + overdue
    overdue_by_mgr = {}           # manager id → (manager, [(name, overdue rows)])
    for key, items in by_key.items():
        person, mgr = _person(cur, tid, key)
        overdue = [r for r in items if r["due_date"] < today]
        due_today = [r for r in items if r["due_date"] == today]
        if person:
            bits = []
            if overdue:
                bits.append(f"{len(overdue)} overdue")
            if due_today:
                bits.append(f"{len(due_today)} due today")
            line = " · ".join(bits)
            shown = (overdue + due_today)[:MAX_ROWS]
            more = len(overdue) + len(due_today) - len(shown)
            await _deliver(tid, [person], title=f"⏰ Your follow-ups: {line}", line=line,
                           rows=[_row(r, today) for r in shown] + ([("And", f"{more} more")] if more else []),
                           url="/follow-ups?who=me", button="Open my follow-ups",
                           footer="Sent at 8am on working days. Calling, messaging or logging a LinkedIn step on the lead ticks it off.",
                           subject=f"Your follow-ups today: {line}", tag=f"followups-{today}")
        if overdue and mgr:
            overdue_by_mgr.setdefault(mgr["id"], (mgr, []))[1].append(
                ((items[0]["assigned_to"] or (person or {}).get("name") or "Someone"), overdue))

    # 2. owner (everyone's overdue, unassigned too) + each line manager (their people)
    all_overdue = [(by_key[k][0]["assigned_to"] or "Unassigned", [r for r in by_key[k] if r["due_date"] < today])
                   for k in by_key]
    all_overdue = [(n, o) for n, o in all_overdue if o]
    owner = _owner(cur, tid)
    sends = []
    if owner and all_overdue:
        sends.append((owner, all_overdue))
    for mgr, groups in overdue_by_mgr.values():
        if owner and (mgr.get("email") or "").lower() == (owner.get("email") or "").lower():
            continue
        sends.append((mgr, groups))
    for p, groups in sends:
        total = sum(len(o) for _, o in groups)
        rows_ = [(name, f"{len(o)} overdue follow-up{'s' if len(o) != 1 else ''}") for name, o in groups]
        for name, o in groups:
            rows_ += [_row(r, today) for r in o[:10]]
        line = f"{total} follow-up{'s' if total != 1 else ''} not done on time"
        await _deliver(tid, _merge([p]), title=f"⚠️ Overdue follow-ups: {line}", line=line, rows=rows_[:MAX_ROWS * 2],
                       url="/follow-ups", button="Open Follow-ups",
                       footer="Sent at 8am on working days to the owner and each person's line manager.",
                       subject=f"Overdue follow-ups: {line}", tag=f"followups-overdue-{today}")


async def run_followup_alerts():
    conn = get_db_connection()
    if not conn:
        return
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if not _ensure(cur):
            return
        cur.execute("""SELECT DISTINCT tenant_id FROM lead_followups
                        WHERE done_at IS NULL AND cancelled_at IS NULL AND due_date <= CURRENT_DATE + 1""")
        for tid in [r["tenant_id"] for r in cur.fetchall()]:
            try:
                tz = _tz(cur, tid)
                local = datetime.now(timezone.utc).astimezone(tz)
                if local.hour < REMIND_HOUR or local.weekday() >= 5:
                    continue
                today = local.date()
                if not _claim(cur, tid, "morning", f"{tid}:{today.isoformat()}"):
                    continue
                await _send_tenant(cur, tid, tz, today)
            except Exception as e:
                print(f"⚠️ [FOLLOW-UP ALERT] tenant {tid} error: {e}")
    finally:
        cur.close()
        conn.close()
