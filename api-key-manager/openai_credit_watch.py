"""
openai_credit_watch.py — emails PhiXtra's admins (ADMIN_EMAILS) when any
PhiXtra service reports that the OpenAI account has run out of credit
(2026-10-10, after the 9 Oct outage where customer AI replies failed).

Runs every 5 minutes from phixtra-openai-watch.timer. It only READS the
services' logs (journalctl) for OpenAI's own "out of credit" errors — it
never calls OpenAI, so it costs nothing. One email per outage at most every
6 hours (remembered in system_notices_sent).

  python3 openai_credit_watch.py            normal run (last 6 minutes of logs)
  python3 openai_credit_watch.py --since "2026-10-09 22:00" --until "2026-10-09 23:00" --dry-run
"""
import argparse
import os
import re
import subprocess
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

SERVICES = ["phixtra-ai-backend", "phixtra-whatsapp-gateway", "phixtra-school-gateway", "phixtra-portal"]
LABELS = {"phixtra-ai-backend": "AI Sales Agent (WhatsApp + website)", "phixtra-whatsapp-gateway": "WhatsApp gateway",
          "phixtra-school-gateway": "School WhatsApp gateway", "phixtra-portal": "Portal (AI designs, email writer)"}
MARKERS = re.compile(r"insufficient_quota|credit_balance_exhausted|You have no credits remaining", re.I)
REPEAT_HOURS = 6


def failures(since, until=None):
    found = {}
    for svc in SERVICES:
        cmd = ["journalctl", "-u", f"{svc}.service", "--since", since, "--no-pager", "-o", "short-iso"]
        if until:
            cmd += ["--until", until]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
        except Exception as e:
            print("journalctl failed for", svc, e)
            continue
        lines = [l for l in out.splitlines() if MARKERS.search(l)]
        if lines:
            found[svc] = {"count": len(lines), "first": lines[0][:25], "last": lines[-1][:25]}
    return found


def already_alerted():
    """True if an alert went out in the last REPEAT_HOURS (bucket key per 6-hour window)."""
    from db import get_db_connection
    now = datetime.now(timezone.utc)
    key = f"openai_credit_out_{now:%Y%m%d}_{now.hour // REPEAT_HOURS}"
    conn = get_db_connection()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO system_notices_sent (notice_key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING notice_key", (key,))
        return cur.fetchone() is None
    finally:
        conn.close()


def send_alert(found):
    from portal_utils import send_email
    rows = "".join(f"<li><b>{LABELS.get(s, s)}</b>: {v['count']} failed request(s), first {v['first']}, last {v['last']}</li>"
                   for s, v in found.items())
    html = ("<p><b>PhiXtra's OpenAI account has run out of credit.</b> AI replies to customers are failing right now.</p>"
            f"<ul>{rows}</ul>"
            "<p><b>To fix (2 minutes):</b> go to platform.openai.com › Settings › Billing and add credit. "
            "It works again straight away; no restart is needed.</p>"
            "<p>Tip: on the same Billing page you can switch on <b>auto recharge</b> so it tops itself up before it runs out.</p>"
            "<p>You'll get at most one of these emails every 6 hours while the problem lasts.</p>")
    sent = 0
    for addr in [a.strip() for a in (os.getenv("OPENAI_ALERT_EMAILS") or os.getenv("ADMIN_EMAILS", "")).split(",") if a.strip()]:
        if send_email(addr, "URGENT: OpenAI credit has run out — AI replies are failing", html):
            sent += 1
    return sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="6 minutes ago")
    ap.add_argument("--until", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    found = failures(a.since, a.until)
    if not found:
        print("ok: no OpenAI credit errors")
        return
    print("OpenAI credit errors found:", found)
    if a.dry_run:
        print("dry run: no email sent")
        return
    if already_alerted():
        print("alert already sent in this 6-hour window")
        return
    print("alert emails sent:", send_alert(found))


if __name__ == "__main__":
    main()
