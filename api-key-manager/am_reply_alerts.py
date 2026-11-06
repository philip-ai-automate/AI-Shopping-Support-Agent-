"""Admin › Inbox reply alerts (2026-11-06).

phixtra-am-reply-alerts.timer wakes every minute; the real interval, on/off,
look-back window and fallback recipients come from Admin › Accounts ›
Settings (am_settings), never from this file. When a signed-up customer
writes to PhiXtra's WhatsApp number, email their Account Manager (or, if no
one is assigned, every admin login that is an email address) once per new
reply, with a link straight to the chat in Admin › Inbox, and move the
account's status to "Replied". An alert for a pending task (a customer
waiting), never a report. Only replies from the last 2 hours are alerted,
so old conversations never flood anyone.

  --dry-run   print who would be emailed, send nothing
"""
import argparse
import html as _h
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta, timezone

import admin_accounts as M

ADMIN_URL = (os.getenv("PORTAL_BASE_URL") or "https://portal.phixtra.com").rstrip("/") + "/admin"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    M.ensure_tables()
    st = M.get_settings()
    if st["alerts_on"] != "1" and not a.dry_run:
        print("reply alerts are switched off in Admin › Accounts › Settings")
        return
    now = datetime.now(timezone.utc)
    last = st.get("alert_last_run")
    if last and not a.dry_run:
        try:
            if now - datetime.fromisoformat(last) < timedelta(minutes=int(st["alert_every_min"])) - timedelta(seconds=20):
                return
        except ValueError:
            pass
    if not a.dry_run:
        M.save_settings({"alert_last_run": now.isoformat()}, "reply-alerts timer")
    conn, cur = M._db()
    conn.autocommit = True
    cur.execute("""CREATE TABLE IF NOT EXISTS am_reply_alert (
                     phone VARCHAR(20) PRIMARY KEY, last_in TIMESTAMPTZ NOT NULL, sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    cur.execute("SELECT username FROM admin_users WHERE active AND username LIKE '%%@%%' AND username <> 'tutorial-bot'")
    everyone = [r["username"] for r in cur.fetchall()]
    picked = [u for u in (st["alert_fallback"] or "").split(",") if u in everyone]
    fallback = picked or everyone
    since = now - timedelta(hours=int(st["alert_window_hours"]))
    from portal_utils import send_email
    sent = 0
    for c in M.conversations():
        if not c["needs_reply"] or not c["last_in"] or c["last_in"] < since:
            continue
        cur.execute("SELECT last_in FROM am_reply_alert WHERE phone=%s", (c["d"],))
        r = cur.fetchone()
        if r and r["last_in"] >= c["last_in"]:
            continue
        to = [c["assigned_admin"]] if c["assigned_admin"] and "@" in c["assigned_admin"] else fallback
        who = c["contact"] or c["business"]
        link = f"{ADMIN_URL}/inbox/{c['tenant_id']}"
        subject = f"{who} replied on WhatsApp"
        body = (f"<div style=\"font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;font-size:15px;line-height:1.6;color:#111827\">"
                f"<p><b>{_h.escape(who)}</b> ({_h.escape(c['business'] or '')}) replied on WhatsApp:</p>"
                f"<blockquote style=\"margin:0 0 14px;padding:8px 12px;background:#f3f4f6;border-left:3px solid #25D366\">{_h.escape((c['last_text'] or '')[:500])}</blockquote>"
                f"<p><a href=\"{link}\" style=\"display:inline-block;background:#0B1D40;color:#fff;font-weight:700;padding:10px 16px;border-radius:9px;text-decoration:none\">Reply in Admin › Inbox</a></p>"
                f"<p style=\"font-size:12px;color:#6b7280\">You can write freely for 24 hours after their message. PhiXtra's AI doesn't answer this customer while you're handling the chat.</p></div>")
        text = f"{who} replied on WhatsApp:\n\n{(c['last_text'] or '')[:500]}\n\nReply in Admin > Inbox: {link}\n"
        if a.dry_run:
            print(f"would email {to}: {subject}")
            continue
        ok_any = False
        for addr in to:
            ok_any = send_email(addr, subject, body, text) or ok_any
        cur.execute("""INSERT INTO am_reply_alert (phone, last_in) VALUES (%s,%s)
                       ON CONFLICT (phone) DO UPDATE SET last_in=EXCLUDED.last_in, sent_at=NOW()""", (c["d"], c["last_in"]))
        cur.execute("""UPDATE am_account_state SET status='replied', updated_at=NOW(), updated_by='WhatsApp reply'
                        WHERE tenant_id=%s AND status IN ('not_contacted','contacted')""", (c["tenant_id"],))
        print(f"tenant {c['tenant_id']}: {'emailed' if ok_any else 'EMAIL FAILED'} {to}: {subject}")
        sent += 1
    conn.close()
    print(f"done: {sent} alert(s)")


if __name__ == "__main__":
    main()
