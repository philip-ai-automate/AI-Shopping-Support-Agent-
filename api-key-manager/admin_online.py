"""Accounts › Who's online — page view (rebuilt 13 Nov 2026).

Presentation only: the tracking itself lives in portal_presence.py.
"""
from zoneinfo import ZoneInfo

import portal_presence as pp
from db import get_db_connection

UK = ZoneInfo(pp.CHART_TZ)

# Plain names for the first parts of a page address.
_NAMES = {"ai": "AI Agent", "crm": "CRM", "wa": "WhatsApp", "whatsapp": "WhatsApp", "inbox": "Inbox",
          "quotes": "Quotations", "woo": "Store", "shopify": "Shopify", "leads": "Leads", "settings": "Settings",
          "campaigns": "Campaigns", "reports": "Reports", "dashboard": "Dashboard", "billing": "Billing",
          "estate": "KeyLand", "school": "School", "pipeline": "Sales Pipeline", "contacts": "Contacts"}


def page_name(path: str) -> str:
    if not path:
        return "—"
    parts = [p for p in path.split("?")[0].split("/") if p]
    out = []
    for p in parts:
        if p.isdigit() or len(p) > 24:
            continue        # ids and tokens say nothing to a person
        out.append(_NAMES.get(p) or p.replace("-", " ").replace("_", " ").capitalize())
    return " › ".join(out) or "Home"


def last_customer():
    """(UK datetime, person, business) of the last customer action, or None."""
    pp.ensure_tables()
    import psycopg2.extras
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT p.last_action_at, " + pp._NAME_COLS + " FROM portal_presence p " + pp._NAME_JOINS +
                    " WHERE NOT p.internal ORDER BY p.last_action_at DESC LIMIT 1")
        r = cur.fetchone()
    finally:
        cur.close(); conn.close()
    return dict(r, at=r["last_action_at"].astimezone(UK)) if r else None


def best_window(hours: list):
    """Longest run of hours with nobody on (wrapping past midnight) →
    (start hour, length) or None."""
    z = [h["avg"] == 0 for h in hours]
    if all(z):
        return (0, 24)
    best, cur = (0, 0), None
    for i in range(48):
        if z[i % 24]:
            if cur is None:
                cur = i
            ln = i - cur + 1
            if ln > best[1] and ln <= 24:
                best = (cur, ln)
        else:
            cur = None
    return (best[0] % 24, best[1]) if best[1] else None


def day_json(day, include_internal):
    rows, per_hour = pp.day_detail(day, include_internal=include_internal)
    return {"per_hour": per_hour, "rows": [{
        "person": r.get("person") or r["actor_key"], "email": r.get("email") or "", "business": r.get("business") or "—",
        "app": pp.APP_LABEL.get(r["app"], r["app"]), "team": bool(r["is_team"]), "internal": bool(r["internal"]),
        "first": r["first_at"].astimezone(UK).strftime("%H:%M"), "last": r["last_at"].astimezone(UK).strftime("%H:%M"),
        "hours": r["hours"] or "—", "minutes": int(r["minutes"] or 0)} for r in rows]}
