"""Daily Summary by EMAIL (2026-11-06, user: "yes to all three, go ahead with 1").

The WhatsApp Daily Report (whatsapp-gateway/wa_daily_report.py) only reaches
businesses with a connected WhatsApp number, and plain-text WhatsApp only
arrives inside Meta's 24-hour window. This sends the same summary by email
to the account owner (first login of the business) when:

  * the Daily Summary switch is on (tenants.daily_report_enabled), and
  * the business is active: someone logged in during the last 30 days, and
  * WhatsApp didn't deliver it today (no connected number, or the WhatsApp
    report was skipped/failed), and
  * something happened yesterday (quiet days are skipped).

Runs every hour (phixtra-daily-summary-email.timer); each business gets it
between 08:00 and 11:59 in ITS OWN time zone (Settings time zone, else its
business country — team_performance.tz_for), at the first run where the
WhatsApp attempt for today is known. daily_summary_email_sent stops a second
copy the same day.

The numbers use the same SQL as wa_daily_report._get_daily_stats — change
both together.

  --dry-run          build everything, send nothing, print who would get what
  --tenant N         only this business
  --to EMAIL         send to this address instead (testing)
  --save-html DIR    write each email to DIR as .html
  --any-time         don't wait for 8 am (testing / manual run)
  --force            ignore the once-a-day guard and quiet-day/active rules (only with --to)
"""
import argparse
import html as _html
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2.extras

from db import get_db_connection
import team_performance as T
import selling_currency as _sc

PORTAL = "https://portal.phixtra.com"
NAVY, INK = "#0B1D40", "#030C18"
LOW_STOCK = 3


def ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary_email_sent (
            tenant_id   INTEGER NOT NULL,
            day         DATE NOT NULL,
            email       VARCHAR(255) NOT NULL,
            sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ok          BOOLEAN,
            PRIMARY KEY (tenant_id, day)
        )""")


def candidate_tenants(cur, only=None):
    """Switch on, not cancelled, not training, not a demo (placeholder
    WhatsApp keys only), and someone logged in during the last 30 days."""
    cur.execute("""
        SELECT t.id, t.name FROM tenants t
        WHERE t.daily_report_enabled = TRUE
          AND COALESCE(t.status, '') <> 'cancelled'
          AND NOT COALESCE(t.is_training, FALSE)
          AND NOT (EXISTS (SELECT 1 FROM wa_tenants w WHERE w.tenant_id = t.id)
                   AND NOT EXISTS (SELECT 1 FROM wa_tenants w WHERE w.tenant_id = t.id AND w.access_token LIKE 'EAA%%'))
          AND (EXISTS (SELECT 1 FROM customers c WHERE c.tenant_id = t.id AND c.is_active
                         AND c.last_login_at > NOW() - INTERVAL '30 days')
               OR EXISTS (SELECT 1 FROM team_members m WHERE m.tenant_id = t.id AND m.is_active
                         AND m.last_login_at > NOW() - INTERVAL '30 days'))
        ORDER BY t.id""")
    return [r for r in cur.fetchall() if only is None or r["id"] == only]


def owner(cur, tenant_id):
    cur.execute("""SELECT email, first_name FROM customers WHERE tenant_id=%s AND is_active
                   ORDER BY id LIMIT 1""", (tenant_id,))
    o = cur.fetchone()
    if not o or not o["email"] or "@" not in o["email"] or o["email"].endswith((".invalid", ".internal", "example.com")):
        return None
    return o["email"].strip(), (o["first_name"] or "").strip() or "there"


def whatsapp_state(cur, tenant_id, local_day_start_utc):
    """'none' (no connected number), 'sent', 'failed' (skipped/failed), or
    'waiting' (connected, but today's WhatsApp attempt hasn't run yet)."""
    cur.execute("""SELECT 1 FROM wa_tenants WHERE tenant_id=%s AND active AND access_token LIKE 'EAA%%' LIMIT 1""",
                (tenant_id,))
    if not cur.fetchone():
        return "none"
    cur.execute("""SELECT status FROM wa_proactive_log WHERE tenant_id=%s AND event_type='daily_report'
                     AND created_at >= %s ORDER BY created_at DESC LIMIT 1""", (tenant_id, local_day_start_utc))
    r = cur.fetchone()
    if not r:
        return "waiting"
    return "sent" if r["status"] == "sent" else "failed"


def stats(cur, tenant_id, day):
    """Same numbers as the WhatsApp report (wa_daily_report._get_daily_stats)."""
    dt = day.isoformat()
    s = {}
    cur.execute("""
        SELECT COUNT(*) AS total_orders,
               SUM(CASE WHEN status IN ('PAYMENT_VERIFIED','PROCESSING','DISPATCHED','DELIVERED','COMPLETED')
                   THEN 1 ELSE 0 END) AS paid_orders,
               COALESCE(SUM(CASE WHEN status IN ('PAYMENT_VERIFIED','PROCESSING','DISPATCHED','DELIVERED','COMPLETED')
                   THEN fx_to_selling(total_amount, currency, tenant_id) ELSE 0 END), 0) AS revenue
        FROM orders WHERE tenant_id = %s AND DATE(created_at) = %s""", (tenant_id, dt))
    r = cur.fetchone() or {}
    s["total_orders"] = int(r.get("total_orders") or 0)
    s["paid_orders"] = int(r.get("paid_orders") or 0)
    s["revenue"] = float(r.get("revenue") or 0)
    cur.execute("""SELECT 1 FROM orders WHERE tenant_id=%s AND created_at > NOW() - INTERVAL '90 days' LIMIT 1""",
                (tenant_id,))
    s["takes_orders"] = cur.fetchone() is not None

    cur.execute("""
        SELECT COUNT(DISTINCT m.customer_phone) AS n FROM wa_message_log m
        WHERE m.tenant_id = %s AND DATE(m.created_at) = %s AND m.direction = 'inbound'
          AND NOT EXISTS (SELECT 1 FROM wa_message_log m2 WHERE m2.tenant_id = m.tenant_id
                            AND m2.customer_phone = m.customer_phone AND DATE(m2.created_at) < %s)""",
                (tenant_id, dt, dt))
    s["new_customers"] = int((cur.fetchone() or {}).get("n") or 0)
    cur.execute("""SELECT COUNT(DISTINCT customer_phone) AS n FROM wa_message_log
                   WHERE tenant_id = %s AND DATE(created_at) = %s AND direction = 'inbound'""", (tenant_id, dt))
    s["conversations"] = int((cur.fetchone() or {}).get("n") or 0)
    cur.execute("""SELECT COUNT(*) AS n FROM wa_handoff_state WHERE tenant_id = %s AND DATE(escalated_at) = %s""",
                (tenant_id, dt))
    s["handoffs"] = int((cur.fetchone() or {}).get("n") or 0)
    cur.execute("""SELECT 1 FROM wa_tenants WHERE tenant_id=%s AND active AND access_token LIKE 'EAA%%' LIMIT 1""",
                (tenant_id,))
    s["has_wa"] = cur.fetchone() is not None

    cur.execute("""SELECT 1 FROM api_keys WHERE tenant_id=%s AND key_type IN ('paid','trial')
                     AND is_active = TRUE LIMIT 1""", (tenant_id,))
    s["has_site"] = cur.fetchone() is not None
    cur.execute("""SELECT COUNT(DISTINCT session_id) AS n FROM chat_messages
                    WHERE tenant_id=%s AND role='user' AND DATE(created_at)=%s""", (tenant_id, dt))
    s["web_chats"] = int((cur.fetchone() or {}).get("n") or 0)
    cur.execute("""
        SELECT COUNT(DISTINCT x) AS n FROM (
            SELECT v.session_id AS x FROM chat_visitors v
             WHERE v.tenant_id=%s
               AND (COALESCE(v.email,'')<>'' OR COALESCE(v.phone,'')<>'' OR COALESCE(v.name,'')<>'')
               AND v.session_id IN (SELECT session_id FROM chat_messages
                                     WHERE tenant_id=%s AND role='user' AND DATE(created_at)=%s)
            UNION
            SELECT h.session_id FROM handoff_requests h
             WHERE h.tenant_id=%s AND DATE(h.created_at)=%s
               AND (COALESCE(h.visitor_email,'')<>'' OR COALESCE(h.visitor_name,'')<>'')
        ) y""", (tenant_id, tenant_id, dt, tenant_id, dt))
    s["web_details"] = int((cur.fetchone() or {}).get("n") or 0)
    cur.execute("""SELECT COUNT(*) AS n FROM handoff_requests WHERE tenant_id=%s AND DATE(created_at)=%s""",
                (tenant_id, dt))
    s["web_handoffs"] = int((cur.fetchone() or {}).get("n") or 0)

    try:
        cur.execute("""SELECT name, stock_quantity FROM products
                       WHERE tenant_id = %s AND is_active = TRUE AND NOT is_service
                         AND stock_quantity <= %s AND stock_quantity < 999
                       ORDER BY stock_quantity ASC LIMIT 5""", (tenant_id, LOW_STOCK))
        s["low_stock"] = cur.fetchall() or []
    except Exception:
        cur.connection.rollback()
        s["low_stock"] = []
    return s


def quiet(s):
    return not any((s["web_chats"], s["web_handoffs"], s["conversations"], s["new_customers"],
                    s["handoffs"], s["total_orders"]))


def _esc(v):
    return _html.escape(str(v or ""))


def build_email(tenant_id, name, first_name, day, s, tz):
    _sc.set_current_tenant(tenant_id)
    code = _sc.tenant_currency(tenant_id) if hasattr(_sc, "tenant_currency") else "NGN"
    try:
        prefix = _sc.currency_label(code)["prefix"]
    except Exception:
        prefix = ""
    rev = f"{prefix}{s['revenue']:,.0f}"
    day_words = f"{day:%A %-d %B %Y}"

    def block(title, rows):
        trs = "".join(f"<tr><td style='padding:7px 0;color:#4b5563'>{_esc(k)}</td>"
                      f"<td style='padding:7px 0;text-align:right;font-weight:700;color:{INK}'>{v}</td></tr>" for k, v in rows)
        return (f"<p style='font-weight:700;margin:16px 0 4px;color:{INK}'>{title}</p>"
                f"<table cellpadding='0' cellspacing='0' style='width:100%;font-size:14px;border-top:1px solid #e5e7eb'>{trs}</table>")

    parts, text = [], [f"Daily summary for {name}, {day_words}", ""]
    if s["has_site"]:
        rows = [("Visitors who chatted", s["web_chats"]), ("Left their details", s["web_details"]),
                ("Passed to your team", s["web_handoffs"])]
        parts.append(block("🌐 Website chat", rows))
        text += ["Website chat"] + [f"  {k}: {v}" for k, v in rows] + [""]
    if s["has_wa"] or s["conversations"]:
        rows = [("Conversations", s["conversations"]), ("New customers", s["new_customers"]),
                ("Passed to your team", s["handoffs"])]
        parts.append(block("💬 WhatsApp", rows))
        text += ["WhatsApp"] + [f"  {k}: {v}" for k, v in rows] + [""]
    if s["takes_orders"] or s["total_orders"]:
        rows = [("Orders placed", s["total_orders"]), ("Orders paid", s["paid_orders"]), ("Revenue", rev)]
        parts.append(block("📦 Orders", rows))
        text += ["Orders"] + [f"  {k}: {v}" for k, v in rows] + [""]
    if s["low_stock"]:
        items = "".join(f"<li>{_esc((it.get('name') or '')[:60])} ({it.get('stock_quantity', 0)} left)</li>"
                        for it in s["low_stock"])
        parts.append(f"<div style='border-left:3px solid #F79009;background:#fef3e2;padding:9px 12px;"
                     f"border-radius:0 8px 8px 0;margin-top:16px;font-size:13.5px'><b>Low stock</b>"
                     f"<ul style='margin:4px 0 0;padding-left:18px'>{items}</ul></div>")
        text += ["Low stock:"] + [f"  {it.get('name')} ({it.get('stock_quantity', 0)} left)" for it in s["low_stock"]] + [""]

    web, wa = s["web_chats"], s["conversations"]
    bits = []
    if web:
        bits.append(f"{web} website visitor{'s' if web != 1 else ''} chatted")
    if wa:
        bits.append(f"{wa} WhatsApp conversation{'s' if wa != 1 else ''}")
    if s["total_orders"]:
        bits.append(f"{s['total_orders']} order{'s' if s['total_orders'] != 1 else ''}")
    subject = f"Yesterday at {name}: " + (", ".join(bits) if bits else "your daily summary") + f" · {day:%a %-d %b}"

    body = f"""<div style="background:#f3f5f9;padding:24px 12px;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:{INK}">
<div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden">
<div style="background:{NAVY};color:#fff;padding:16px 24px"><div style="font-size:16px;font-weight:700">📊 Daily summary · {_esc(name)}</div>
<div style="font-size:12.5px;opacity:.8">{day_words}</div></div>
<div style="padding:20px 24px;font-size:14px;line-height:1.55">
<p style="margin:0 0 4px">Hi {_esc(first_name)}, here is what happened yesterday.</p>
{''.join(parts)}
<p style="margin:20px 0 0"><a href="{PORTAL}/inbox" style="display:inline-block;background:{NAVY};color:#fff;font-weight:700;padding:10px 16px;border-radius:9px;text-decoration:none;font-size:13.5px">See the chats in your Inbox</a></p>
<div style="font-size:12px;color:#6b7280;border-top:1px solid #e5e7eb;padding-top:12px;margin-top:18px">
Sent at 8 am {_esc(T.tz_label(tz))} time to the account owner, on days when something happened.
To stop it, switch off the daily summary in Settings › Account Settings.</div>
</div></div></div>"""
    text += [f"See the chats in your Inbox: {PORTAL}/inbox", "",
             "To stop this email, switch off the daily summary in Settings › Account Settings."]
    return subject, body, "\n".join(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tenant", type=int)
    ap.add_argument("--to")
    ap.add_argument("--save-html")
    ap.add_argument("--any-time", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.force and not a.to:
        sys.exit("--force only works with --to (a test address)")

    from portal_utils import send_email
    conn = get_db_connection()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sent = 0
    try:
        ensure_tables(cur)
        if a.force and a.tenant:
            cur.execute("SELECT id, name FROM tenants WHERE id=%s", (a.tenant,))
            tenants = cur.fetchall()
        else:
            tenants = candidate_tenants(cur, a.tenant)
        for t in tenants:
            tid, tname = t["id"], (t["name"] or "your business").strip()
            tz = T.tz_for(tid)
            now = T.now_local(tz)
            if not a.any_time and not (8 <= now.hour <= 11):
                continue
            yesterday = now.date() - timedelta(days=1)
            if not a.force:
                cur.execute("SELECT 1 FROM daily_summary_email_sent WHERE tenant_id=%s AND day=%s", (tid, now.date()))
                if cur.fetchone():
                    continue
                day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                wa = whatsapp_state(cur, tid, day_start)
                if wa == "sent":
                    print(f"tenant {tid}: WhatsApp summary delivered today, no email")
                    continue
                if wa == "waiting":
                    # The WhatsApp report runs once a day at 07:00 UTC. Before
                    # that (e.g. Asia, where 8 am local is earlier) WhatsApp
                    # will still come, so no email; after it, give it until 11.
                    from datetime import datetime as _dt, timezone as _tzu
                    if _dt.now(_tzu.utc).hour < 8 or now.hour < 11:
                        print(f"tenant {tid}: waiting for today's WhatsApp attempt")
                        continue
            o = owner(cur, tid)
            if not o:
                print(f"tenant {tid}: no owner email, skipped")
                continue
            s = stats(cur, tid, yesterday)
            if quiet(s) and not a.force:
                print(f"tenant {tid}: quiet day ({yesterday}), skipped")
                if not a.dry_run:
                    cur.execute("""INSERT INTO daily_summary_email_sent (tenant_id, day, email, ok)
                                   VALUES (%s,%s,%s,NULL) ON CONFLICT DO NOTHING""", (tid, now.date(), "quiet"))
                continue
            subject, body, text = build_email(tid, tname, o[1], yesterday, s, tz)
            to = a.to or o[0]
            if a.save_html:
                os.makedirs(a.save_html, exist_ok=True)
                with open(os.path.join(a.save_html, f"t{tid}.html"), "w") as f:
                    f.write(body)
            if a.dry_run:
                print(f"tenant {tid}: would send to {o[0]}: {subject}")
                continue
            if not a.force:
                cur.execute("""INSERT INTO daily_summary_email_sent (tenant_id, day, email) VALUES (%s,%s,%s)
                               ON CONFLICT DO NOTHING RETURNING tenant_id""", (tid, now.date(), to.lower()))
                if not cur.fetchone():
                    continue
            ok = send_email(to, subject, body, text)
            if not a.force:
                cur.execute("UPDATE daily_summary_email_sent SET ok=%s WHERE tenant_id=%s AND day=%s",
                            (ok, tid, now.date()))
            print(f"tenant {tid}: {'sent' if ok else 'FAILED'} to {to}: {subject}")
            sent += ok
    finally:
        conn.close()
    print(f"done: {sent} sent")


if __name__ == "__main__":
    main()
