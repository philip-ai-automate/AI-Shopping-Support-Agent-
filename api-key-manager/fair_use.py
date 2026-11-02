"""
fair_use.py — B2B fair-use warning (31 Oct 2026).

The B2B plans are sold as "Unlimited AI replies (fair use)". Each has a hidden
safety limit (plans.ai_messages_limit: Starter 10,000, Growth 20,000,
Scale 50,000). The AI service already stops AI replies at the limit (messages
then go to the business's team). This job warns PhiXtra — not the customer —
once a B2B business passes 80% of its limit in the current period, so PhiXtra
can call them about upgrading before anything is blocked.

One email per business per period (tenants.quota_notified_at). Runs every
30 minutes in the portal process, one server at a time (advisory lock).
"""
import threading
import time

import psycopg2.extras

from db import get_db_connection
from portal_utils import send_email

ALERT_TO = "support@phixtra.com"
WARN_AT = 0.8
_LOCK = 80311031
_EVERY = 30 * 60


def due() -> list:
    """B2B businesses at or over 80% of their limit, not yet warned this period."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT t.id AS tenant_id, t.name, p.name AS plan_name, p.ai_messages_limit AS lim,
                   COALESCE(t.plan_period_start, date_trunc('month', now())::date) AS period_start,
                   t.quota_notified_at,
                   (SELECT COUNT(*) FROM usage_events u
                     WHERE u.tenant_id = t.id
                       AND u.created_at >= COALESCE(t.plan_period_start, date_trunc('month', now())::date)) AS used
              FROM tenants t JOIN plans p ON p.id = t.plan_id
             WHERE p.slug LIKE 'b2b%%' AND p.ai_messages_limit > 0""")
        out = []
        for r in cur.fetchall():
            if r["used"] < WARN_AT * r["lim"]:
                continue
            n = r["quota_notified_at"]
            if n is not None and n.date() >= r["period_start"]:
                continue
            out.append(dict(r))
        return out
    finally:
        cur.close(); conn.close()


def _warn(r: dict) -> bool:
    pct = round(r["used"] * 100 / r["lim"])
    subject = f"Fair use: {r['name']} has used {pct}% of its AI replies"
    text = (f"{r['name']} ({r['plan_name']}) has used {r['used']:,} of {r['lim']:,} AI replies "
            f"({pct}%) since {r['period_start']:%d %b %Y}.\n\n"
            "At the limit the AI stops answering and new messages go to their team. "
            "Call them about upgrading before that happens.")
    html = (f"<p><b>{r['name']}</b> ({r['plan_name']}) has used <b>{r['used']:,}</b> of "
            f"{r['lim']:,} AI replies (<b>{pct}%</b>) since {r['period_start']:%d %b %Y}.</p>"
            "<p>At the limit the AI stops answering and new messages go to their team. "
            "Call them about upgrading before that happens.</p>")
    return send_email(ALERT_TO, subject, html, text_body=text)


def run_once() -> int:
    sent = 0
    for r in due():
        if _warn(r):
            conn = get_db_connection(); cur = conn.cursor()
            try:
                cur.execute("UPDATE tenants SET quota_notified_at = now() WHERE id = %s", (r["tenant_id"],))
                conn.commit()
            finally:
                cur.close(); conn.close()
            sent += 1
    return sent


def _loop():
    time.sleep(90)
    while True:
        conn = None
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK,))
            if cur.fetchone()[0]:
                try:
                    n = run_once()
                    if n:
                        print(f"📣 fair_use: {n} warning(s) sent")
                finally:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK,))
            cur.close()
        except Exception as e:
            print("⚠️ fair_use loop:", e)
        finally:
            try:
                conn and conn.close()
            except Exception:
                pass
        time.sleep(_EVERY)


def start():
    if not getattr(threading, "_phixtra_fair_use_started", False):
        threading._phixtra_fair_use_started = True  # type: ignore[attr-defined]
        threading.Thread(target=_loop, daemon=True).start()
