"""Accounts › Inactive accounts — page view (rebuilt 13 Nov 2026).

Groups businesses by stage and works out what happens next. The rules
themselves live in inactivity_core.py and are not changed here.
"""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from markupsafe import escape

import inactivity_core as ic

_UK = ZoneInfo("Europe/London")

STAGES = [
    ("warned", "Warned · deletion booked",
     "These businesses have had at least one warning email. They're deleted on the date shown unless they log in or use PhiXtra."),
    ("soon", "Warning in next 14 days",
     "Not used for a while. Their first warning email goes out on the date shown."),
    ("later", "Not due yet",
     "Used recently enough. Nothing happens until the date shown."),
    ("cant", "Can't be warned",
     "No confirmed email, so they can't be warned. Without a warning they're never deleted."),
    ("never", "Never deleted",
     "PhiXtra's own accounts, paying businesses and any you marked Never delete."),
    ("deleted", "Deleted",
     "Deleted accounts. Their invoices and billing records are kept."),
]

MODE_TEXT = {
    "off": ("Pause", "Paused: no warning emails go to businesses and nobody is deleted. Switch back to Warn and delete any time."),
    "test": ("Pause", "Paused: no warning emails go to businesses and nobody is deleted. Switch back to Warn and delete any time."),
    "on": ("Warn and delete", "Businesses get the 3 warning emails on their dates, and anyone still unused is deleted on day 60."
                              " You get an email each morning with what was done."),
}

EMAIL_LABEL = {"warn1": "First warning", "warn2": "Second warning", "warn3": "Final warning",
               "delete": "Deleted + goodbye email", "reset": "Back in use · warnings cancelled"}

STEP_KIND = {"First warning": "w1", "Second warning": "w2", "Final warning": "w3", "Deletion": "del"}
KIND_LABEL = {"w1": "first warning", "w2": "second warning", "w3": "final warning", "del": "deleted"}


def _why_cant(email: str) -> str:
    e = (email or "").lower()
    if e.endswith("@wa.phixtra.internal") or not e:
        return "Signed up with WhatsApp only, no email"
    if e.split("@")[-1] in ic.NO_EMAIL_DOMAINS:
        return "PhiXtra email address"
    return "Email never confirmed"


def _why_never(a: dict) -> str:
    lbl = a["status_label"]
    if "kept" in lbl:
        return "You chose Never delete"
    if "paying" in lbl:
        return "Paying business"
    return "PhiXtra's own account"


def build(accts: list) -> dict:
    today = date.today()
    for a in accts:
        st = a["status"]
        if st == "deleted":
            a["stage"] = "deleted"
        elif st == "protected":
            a["stage"], a["why"] = "never", _why_never(a)
        elif st == "no_email":
            a["stage"], a["why"] = "cant", _why_cant(a["email"])
        elif st == "warned":
            a["stage"] = "warned"
        else:
            a["stage"] = "soon" if a["next_on"] and (a["next_on"] - today).days <= 14 else "later"
        # Dates of every step still to come (deletion date is fixed once the
        # first warning goes; before that it is first warning + notice days).
        a["events"] = []
        if a["stage"] in ("soon", "later"):
            d = a["next_on"] + timedelta(days=ic.NOTICE_DAYS)
            a["del_on"] = d
            a["events"] = [(a["next_on"], "w1"), (d - timedelta(days=ic.SECOND_WARNING_DAYS), "w2"),
                           (d - timedelta(days=ic.FINAL_WARNING_DAYS), "w3"), (d, "del")]
        elif a["stage"] == "warned":
            d = a["deletion_on"]
            a["del_on"] = d
            steps = [("w2", d - timedelta(days=ic.SECOND_WARNING_DAYS), a.get("warn2_at")),
                     ("w3", d - timedelta(days=ic.FINAL_WARNING_DAYS), a.get("warn3_at")), ("del", d, None)]
            a["events"] = [(max(on, today), k) for k, on, sent in steps if not sent]
        if a["events"]:
            a["next_kind"] = a["events"][0][1]
            a["next_date"] = a["events"][0][0]
        a["track_pct"] = min(100, round(a["days_inactive"] / 60 * 100, 1))

    by = {k: [] for k, _, _ in STAGES}
    for a in accts:
        by[a["stage"]].append(a)
    for k in ("warned", "soon", "later"):
        by[k].sort(key=lambda a: (a.get("next_date") or date.max, -a["days_inactive"], (a["business_name"] or "").lower()))
    for k in ("cant", "never"):
        by[k].sort(key=lambda a: (a["business_name"] or "").lower())
    by["deleted"].sort(key=lambda a: a["deleted_at"], reverse=True)

    # Calendar: next 30 days.
    cal = {}
    for a in by["warned"] + by["soon"] + by["later"]:
        for on, k in a["events"]:
            if 0 <= (on - today).days <= 30:
                cal.setdefault(on, {"w1": 0, "w2": 0, "w3": 0, "del": 0})[k] += 1
    calendar = [{"on": d, "days": (d - today).days,
                 "parts": [(k, n, KIND_LABEL[k]) for k, n in cal[d].items() if n]} for d in sorted(cal)]

    # Headline: the next thing that happens.
    headline = sub = None
    if calendar:
        first = calendar[0]
        bits = []
        for k, n, lbl in first["parts"]:
            who = f"{n} business{'es' if n > 1 else ''}"
            bits.append(f"{who} {'are' if n > 1 else 'is'} deleted" if k == "del"
                        else f"{who} get{'' if n > 1 else 's'} their {lbl} email")
        headline = f"{first['on'].strftime('%a %-d %b')}: " + " and ".join(bits) + "."
        w1 = [a for a in by["soon"] + by["later"] if a["next_on"] == first["on"]]
        if w1:
            headline = headline[:-1] + f". If they don't come back, they're deleted on {w1[0]['del_on'].strftime('%-d %b')}."
        if not by["warned"]:
            sub = "No business has been warned yet."
    return {"by": by, "calendar": calendar[:12], "headline": headline, "sub": sub}


# ── Emails view + History (13 Nov 2026) ──────────────────────────────────────

EMAILS = [  # (log action, events key, name, when)
    ("warn1", "w1", "First warning", f"Day {ic.WARN_AFTER_DAYS}"),
    ("warn2", "w2", "Second warning", f"{ic.SECOND_WARNING_DAYS} days before deletion"),
    ("warn3", "w3", "Final warning", "1 day before deletion"),
    ("delete", "del", "Goodbye", "Day 60, after deletion"),
]


def email_stats(view: dict, log: list) -> list:
    """Per email: how many sent, who got it, and when the next ones go."""
    nxt = {}
    for a in view["by"]["warned"] + view["by"]["soon"] + view["by"]["later"]:
        for on, k in a["events"]:
            nxt.setdefault(k, {}).setdefault(on, 0)
            nxt[k][on] += 1
    out = []
    for action, key, name, when in EMAILS:
        got = [l for l in log if l["mode"] == "on" and l["action"] in (action, action + "_failed")]
        sent = sum(1 for l in got if l["action"] == action)
        first = min(nxt.get(key, {}) or [None], key=lambda d: d or date.max)
        out.append({"action": action, "name": name, "when": when, "sent": sent, "got": got,
                    "next_on": first, "next_n": nxt[key][first] if first else 0})
    return out


def history(log: list) -> list:
    """One timeline: emails, deletions, businesses that came back, admin changes."""
    from db import get_db_connection
    import psycopg2.extras
    ev = []
    for l in log:
        act = l["action"].split("_")[0]
        failed = l["action"].endswith("_failed")
        biz = l["business_name"] or f"#{l['tenant_id']}"
        if l["mode"] != "on":
            continue
        if act in ("warn1", "warn2", "warn3"):
            name = {"warn1": "First warning", "warn2": "Second warning", "warn3": "Final warning"}[act]
            ev.append({"at": l["created_at"], "kind": "email", "by": "PhiXtra",
                       "text": f"{name} email {'FAILED to send' if failed else 'sent'} to <b>{escape(biz)}</b>",
                       "detail": l["detail"]})
        elif act == "delete":
            ev.append({"at": l["created_at"], "kind": "del", "by": "PhiXtra",
                       "text": f"<b>{escape(biz)}</b> {'could not be deleted' if failed else 'deleted, goodbye email sent'}",
                       "detail": l["detail"]})
        elif act == "reset":
            ev.append({"at": l["created_at"], "kind": "back", "by": "PhiXtra",
                       "text": f"<b>{escape(biz)}</b> came back. Warnings cancelled.", "detail": ""})
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT a.created_at, a.admin_username, a.action, a.details, t.name AS biz, a.tenant_id
                         FROM audit_logs a LEFT JOIN tenants t ON t.id = a.tenant_id
                        WHERE a.action LIKE 'inactivity%%' AND a.admin_username NOT LIKE 'zz\\_test%%'
                        ORDER BY a.created_at DESC LIMIT 500""")
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    names = {"off": "Pause", "test": "Test mode (now removed)", "on": "Warn and delete"}
    for r in rows:
        d = r["details"] if isinstance(r["details"], dict) else {}
        if isinstance(r["details"], str):
            import json
            try:
                d = json.loads(r["details"])
            except ValueError:
                d = {}
        biz = escape(r["biz"] or f"#{r['tenant_id']}")
        text = {"inactivity_mode": f"Switch changed to <b>{names.get(d.get('mode'), d.get('mode'))}</b>",
                "inactivity_keep": f"<b>{biz}</b> marked Never delete",
                "inactivity_unkeep": f"<b>{biz}</b>: deletion allowed again",
                "inactivity_delay": f"<b>{biz}</b> given 30 more days"}.get(r["action"])
        if text:
            ev.append({"at": r["created_at"], "kind": "admin", "by": r["admin_username"], "text": text, "detail": ""})
    from datetime import datetime, timezone
    ev.append({"at": ic.TRACKING_START, "kind": "admin", "by": "PhiXtra", "notime": True, "detail": "",
               "text": "Tracking started. Activity is counted from this day, so nobody is treated as unused from before it."})
    ev.sort(key=lambda e: e["at"], reverse=True)
    for e in ev:
        e["at_uk"] = e["at"].astimezone(_UK)
        e["day"] = e["at_uk"].date()
    return ev
