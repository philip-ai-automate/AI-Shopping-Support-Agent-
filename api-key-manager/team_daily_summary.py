"""Team performance — the 6 pm daily summary email (user 2026-10-11, step 4).

Run every hour by phixtra-team-summary.timer; each business gets it when
it is 18:00–18:59 on a Monday–Friday in ITS OWN time zone (Settings time
zone, else its business country — staff_activity.tenant_tz_name).
Each line manager gets their own team (people under them via
line_manager_id); the owner gets everyone. A manager only gets it if their
role has the "Team performance" tick (leads.team_performance), so the email
never shows numbers the portal itself wouldn't. The owner can switch it off,
or switch off single people, on the Team performance page.

Skipped: training accounts, demo accounts (only placeholder WhatsApp keys),
businesses with nobody on the page. team_summary_sent stops a second copy
the same day (re-runs and a manual run are safe).

  --dry-run          build everything, send nothing, print who would get what
  --tenant N         only this business
  --to EMAIL         send every email to this address instead (testing)
  --save-html DIR    write each email to DIR as an .html file
  --force            ignore the once-a-day guard (only with --to)
  --any-time         don't wait for 6 pm on a weekday (testing / manual run)
"""
import argparse
import html as _html
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2.extras

from db import get_db_connection
import team_performance as T
import selling_currency as _sc

PORTAL = "https://portal.phixtra.com"
NAVY, INK = "#0B1D40", "#030C18"


def ensure_tables(cur):
    cur.execute("SET LOCAL lock_timeout = '3s'")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_summary_settings (
            tenant_id   INTEGER PRIMARY KEY,
            enabled     BOOLEAN NOT NULL DEFAULT TRUE,
            off_keys    TEXT[] NOT NULL DEFAULT '{}',
            updated_by  VARCHAR(200),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_summary_sent (
            tenant_id   INTEGER NOT NULL,
            day         DATE NOT NULL,
            email       VARCHAR(255) NOT NULL,
            sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ok          BOOLEAN,
            PRIMARY KEY (tenant_id, day, email)
        )""")


def settings(cur, tenant_id):
    cur.execute("SELECT enabled, off_keys FROM team_summary_settings WHERE tenant_id=%s", (tenant_id,))
    r = cur.fetchone()
    return (r["enabled"], set(r["off_keys"] or [])) if r else (True, set())


def candidate_tenants(cur, only=None):
    """Businesses with staff who have open assigned leads, minus training and
    demo accounts."""
    cur.execute("""
        SELECT DISTINCT l.tenant_id FROM merchant_pipeline_leads l
        JOIN tenants t ON t.id = l.tenant_id
        WHERE l.assigned_key LIKE 'team:%%' AND l.outcome IS NULL
          AND NOT COALESCE(t.is_training, FALSE)
          AND NOT (EXISTS (SELECT 1 FROM wa_tenants w WHERE w.tenant_id = t.id)
                   AND NOT EXISTS (SELECT 1 FROM wa_tenants w WHERE w.tenant_id = t.id AND w.access_token LIKE 'EAA%%'))
        ORDER BY 1""")
    ids = [r["tenant_id"] for r in cur.fetchall()]
    return [i for i in ids if only is None or i == only]


def recipients(cur, tenant_id):
    """[(viewer dict, name, email, key)] — owner first, then managers."""
    out = []
    cur.execute("SELECT id, email, first_name, last_name FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1",
                (tenant_id,))
    o = cur.fetchone()
    if o and o["email"]:
        name = f"{(o['first_name'] or '').strip()} {(o['last_name'] or '').strip()}".strip() or "there"
        out.append(({"is_team": False, "key": f"owner:{o['id']}", "team_member_id": None}, name,
                     o["email"], f"owner:{o['id']}"))
    cur.execute("""SELECT m.id, m.name, m.email FROM team_members m
                   JOIN tenant_roles r ON r.id = m.role_id
                   WHERE m.tenant_id=%s AND m.is_active
                     AND COALESCE((r.permissions ->> 'leads.team_performance')::boolean, FALSE)
                     AND EXISTS (SELECT 1 FROM team_members x WHERE x.line_manager_id = m.id AND x.is_active)
                   ORDER BY m.id""", (tenant_id,))
    for m in cur.fetchall():
        out.append(({"is_team": True, "key": f"team:{m['id']}", "team_member_id": m["id"]},
                    m["name"], m["email"], f"team:{m['id']}"))
    return [r for r in out if r[2] and "@" in r[2] and not r[2].endswith((".invalid", ".internal", "example.com"))]


def _esc(s):
    return _html.escape(str(s or ""))


def _naira(v):
    """An amount in the business's selling currency (₦ unless it sells in £/$)."""
    tid = _sc.current_tenant()
    code = _sc.tenant_currency(tid) if tid else "NGN"
    return f"{_sc.currency_label(code)['prefix']}{float(v or 0):,.0f}"


def email_attention(d, now):
    """The page's warnings, plus one only an end-of-day email needs: someone
    who was in today but stopped well before the end of the day."""
    items = [(sev, text) for sev, text, _t, _a in d["attention"]]
    if d["period_key"] == "today" and T.is_workday(now.date()):
        for p in d["people"]:
            if p["last_seen"] and p["last_seen"].hour < 15 and p["assigned"]:
                items.append(("r", f"<b>{_esc(p['name'])}</b> had no activity after {p['last_seen']:%H:%M} "
                                   f"(logged in at {p['first_seen']:%H:%M})."))
    items.sort(key=lambda x: x[0] != "r")
    return items[:8]


def build_email(tenant_id, viewer, first_name, now):
    _sc.set_current_tenant(tenant_id)   # amounts in this business's currency
    d = T.build(tenant_id, viewer, "today")
    if not d["people"]:
        return None
    m = T.build(tenant_id, viewer, "month")
    t = d["totals"]
    people = d["people"]
    day_words = f"{now:%a %-d %b}"
    subject = (f"Your team today: {t['contacted']:,} business{'es' if t['contacted'] != 1 else ''} contacted personally, "
               f"{t['opps']} new opportunit{'ies' if t['opps'] != 1 else 'y'} · {day_words}")
    team_words = "your team" if viewer["is_team"] else "the team"
    n = len(people)

    rows = []
    for p in people:
        when = (f"{p['first_seen']:%H:%M} – {p['last_seen']:%H:%M}" if p["first_seen"] else "Not in today")
        rows.append(
            "<tr>"
            f"<td style='padding:9px 10px;border-top:1px solid #e5e7eb'><b>{_esc(p['name'])}</b>"
            f"<div style='font-size:12px;color:#6b7280'>{when}</div></td>"
            + "".join(f"<td style='padding:9px 10px;border-top:1px solid #e5e7eb;text-align:right'>{v}</td>"
                      for v in (f"<b>{p['contacted']:,}</b>", p["calls"], p["messages"], p["replies"], p["opps"], _naira(p["won_value"])))
            + "</tr>")
    th = "".join(f"<th style='padding:8px 10px;text-align:{a};font-size:11px;color:#6b7280;text-transform:uppercase;"
                 f"letter-spacing:.04em;background:#f6f7f9'>{h}</th>"
                 for h, a in (("Person", "left"), ("Contacted personally", "right"), ("Calls", "right"),
                              ("Messages", "right"), ("Replies", "right"), ("New opps", "right"), ("Won", "right")))
    table = (f"<table cellpadding='0' cellspacing='0' style='width:100%;border:1px solid #e5e7eb;border-radius:10px;"
             f"border-collapse:separate;font-size:14px;margin:6px 0 18px'><tr>{th}</tr>{''.join(rows)}</table>")

    # Who they contacted today, by name (the daily record)
    record = T.contacted_list(tenant_id, {p["key"] for p in people}, now.date(), now.date())
    by_person = {g["actor_key"]: g for g in record}
    rec_html = []
    for p in people:
        g = by_person.get(p["key"])
        if not g:
            rec_html.append(f"<p style='margin:0 0 10px;font-size:13.5px'><b>{_esc(p['name'])}</b>: "
                            "<span style='color:#b42318'>no business contacted personally today</span>"
                            + (f" <span style='color:#6b7280'>({p['marked_only']:,} marked Contacted without a call or message)</span>"
                               if p.get("marked_only") else "") + "</p>")
            continue
        items = "".join(
            f"<li style='margin:2px 0'>{it['first']:%H:%M} · <b>{_esc(it['business'])}</b>"
            f" <span style='color:#6b7280'>· {_esc(' · '.join(it['what']))[:160]}</span></li>" for it in g["items"][:25])
        more = f"<li style='color:#6b7280'>and {g['count'] - 25} more</li>" if g["count"] > 25 else ""
        rec_html.append(f"<p style='margin:0 0 4px;font-size:13.5px'><b>{_esc(p['name'])}</b>: {g['count']} "
                        f"business{'es' if g['count'] != 1 else ''}</p><ul style='margin:0 0 12px;padding-left:18px;font-size:13px'>{items}{more}</ul>")
    record_html = ("<p style='font-weight:700;margin:4px 0 8px'>Businesses contacted personally today</p>" + "".join(rec_html)
                   + (f"<p style='font-size:12px;color:#6b7280;margin:0 0 16px'>Not counted: {t['auto_emails']:,} emails sent automatically "
                      "by sequences.</p>" if t.get("auto_emails") else ""))

    attn = email_attention(d, now)
    if attn:
        flags = "".join(
            f"<div style='border-left:3px solid {'#F04438' if s == 'r' else '#F79009'};"
            f"background:{'#fdecea' if s == 'r' else '#fef3e2'};padding:9px 12px;border-radius:0 8px 8px 0;"
            f"margin-bottom:8px;font-size:13.5px'>{text}</div>" for s, text in attn)
        attn_html = f"<p style='font-weight:700;color:{INK};margin:0 0 8px'>Needs your attention</p>{flags}"
    else:
        attn_html = "<p style='color:#0d7a4d;margin:0 0 8px'><b>Nothing needs your attention today.</b></p>"

    m_opps = sum(p["month_opps"] for p in m["people"])
    m_won = sum(p["month_won"] for p in m["people"])
    m_target = sum(p["opp_target"] or 0 for p in m["people"])
    month_line = (f"<b>Month so far ({m['month_first']:%-d} – {now:%-d %b}):</b> {m_opps} new "
                  f"opportunit{'ies' if m_opps != 1 else 'y'}"
                  + (f" of {team_words}'s {m_target} target" if m_target else "")
                  + f", {_naira(m_won)} won.")

    monday = ""
    if now.weekday() == 0:
        import datetime as _dt
        ls = now.date() - _dt.timedelta(days=7)
        le = ls + _dt.timedelta(days=4)
        if le >= T.RECORDING_STARTED:
            conn = get_db_connection()
            try:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                tot = T._activity_totals(cur, tenant_id, ls, le, now.tzinfo.key)
            finally:
                conn.close()
            keys = {p["key"] for p in people}
            lw = sum(int(tot.get(k, {}).get("contacted") or 0) for k in keys)
            lo = sum(int(tot.get(k, {}).get("opps") or 0) for k in keys)
            lv = sum(float(tot.get(k, {}).get("won_value") or 0) for k in keys)
            monday = (f"<p style='margin:0 0 12px'><b>Last week ({ls:%-d} – {le:%-d %b}):</b> {lw:,} businesses contacted personally, "
                      f"{lo} new opportunit{'ies' if lo != 1 else 'y'}, {_naira(lv)} won.</p>")

    why = ("You get this because " + (", ".join(p["name"].split()[0] for p in people[:-1]) + " and " if n > 1 else "")
           + people[-1]["name"].split()[0] + (" report to you." if n > 1 else " reports to you.")
           ) if viewer["is_team"] else "You get this as the account owner."
    body = f"""<div style="background:#f3f5f9;padding:24px 12px;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:{INK}">
<div style="max-width:640px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden">
<div style="background:{NAVY};color:#fff;padding:16px 24px"><div style="font-size:16px;font-weight:700">Sales team daily summary</div>
<div style="font-size:12.5px;opacity:.8">{now:%A %-d %B %Y} · {team_words} of {n}</div></div>
<div style="padding:22px 24px;font-size:14px;line-height:1.55">
<p style="margin:0 0 12px">Hi {_esc(first_name)}, here is how {team_words} did today.</p>
{table}
{record_html}
{attn_html}
<p style="margin:14px 0 12px">{month_line}</p>
{monday}
<a href="{PORTAL}/team-performance" style="display:inline-block;background:{NAVY};color:#fff;font-weight:700;padding:10px 16px;border-radius:9px;text-decoration:none;font-size:13.5px">Open Team performance</a>
<div style="font-size:12px;color:#6b7280;border-top:1px solid #e5e7eb;padding-top:12px;margin-top:18px">
Sent Monday to Friday at 6:00 pm {T.tz_label(now.tzinfo)} time. {why} Calls count once they're logged on the lead's page.
The account owner can switch this email off on the Team performance page.</div>
</div></div></div>"""
    text = (f"Sales team daily summary, {now:%A %-d %B %Y}\n\n"
            + "\n".join(f"{p['name']}: {p['contacted']} businesses contacted personally, {p['calls']} calls, {p['replies']} replies, "
                        f"{p['opps']} new opps, {_naira(p['won_value'])} won" for p in people)
            + f"\n\nOpen Team performance: {PORTAL}/team-performance\n")
    return subject, body, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tenant", type=int)
    ap.add_argument("--to")
    ap.add_argument("--save-html")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--any-time", action="store_true")
    a = ap.parse_args()
    if a.force and not a.to:
        sys.exit("--force only works with --to (a test address)")

    from portal_utils import send_email
    conn = get_db_connection()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sent = skipped = 0
    try:
        for tid in candidate_tenants(cur, a.tenant):
            now = T.now_local(T.tz_for(tid))
            if not a.any_time and not (T.is_workday(now.date()) and now.hour == 18):
                continue
            enabled, off = settings(cur, tid)
            if not enabled:
                print(f"tenant {tid}: switched off by the owner")
                continue
            for viewer, name, email, key in recipients(cur, tid):
                if key in off:
                    print(f"tenant {tid}: {name} switched off")
                    continue
                built = build_email(tid, viewer, name.split()[0], now)
                if not built:
                    print(f"tenant {tid}: {name}: nobody on their page, skipped")
                    continue
                subject, body, text = built
                to = a.to or email
                if a.save_html:
                    os.makedirs(a.save_html, exist_ok=True)
                    with open(os.path.join(a.save_html, f"t{tid}_{key.replace(':', '_')}.html"), "w") as f:
                        f.write(body)
                if a.dry_run:
                    print(f"tenant {tid}: would send to {name} <{email}>: {subject}")
                    continue
                if not a.force:
                    cur.execute("""INSERT INTO team_summary_sent (tenant_id, day, email) VALUES (%s,%s,%s)
                                   ON CONFLICT DO NOTHING RETURNING email""", (tid, now.date(), to.lower()))
                    if not cur.fetchone():
                        print(f"tenant {tid}: {to} already sent today")
                        skipped += 1
                        continue
                ok = send_email(to, subject, body, text)
                if not a.force:
                    cur.execute("UPDATE team_summary_sent SET ok=%s WHERE tenant_id=%s AND day=%s AND email=%s",
                                (ok, tid, now.date(), to.lower()))
                print(f"tenant {tid}: {'sent' if ok else 'FAILED'} to {name} <{to}>: {subject}")
                sent += ok
    finally:
        conn.close()
    print(f"done: {sent} sent, {skipped} already sent")


if __name__ == "__main__":
    main()
