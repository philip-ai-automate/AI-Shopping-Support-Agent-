"""KeyLand: AI brief before a call (2026-10-22).

One tap on the lead page: the AI reads everything KeyLand knows about the buyer
(WhatsApp chat incl. voice notes, logged calls and notes, readiness, land
details, inspections, plot and payments, missed calls) plus the agency's real
plot prices and plans, and writes a short brief for the agent: what they want,
what they can afford and which plan fits, what worries them, what has happened,
what to say and what to ask. Amounts come only from figures KeyLand works out
(land.options), never from the model.
"""
import json
import os
from datetime import datetime, timezone

import psycopg2.extras

from db import get_db_connection
from . import land as LAND


def _rows(cur, sql, args):
    try:
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        cur.connection.rollback()
        return []


def _when(dt, tz) -> str:
    return dt.astimezone(tz).strftime("%a %d %b %Y, %H:%M") if dt else ""


def gather(tenant_id: int, customer_id: int, tz) -> dict:
    """Everything the brief is written from, as plain text sections."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c = (_rows(cur, """SELECT c.*, TRIM(COALESCE(s.first_name,'') || ' ' || COALESCE(s.last_name,'')) AS agent
                           FROM re_customers c LEFT JOIN re_staff s ON s.id = c.assigned_to
                           WHERE c.id=%s AND c.tenant_id=%s""", (customer_id, tenant_id)) or [{}])[0]
        if not c:
            return {}
        chat = _rows(cur, """SELECT role, content, created_at FROM (SELECT * FROM re_chat_messages
                             WHERE tenant_id=%s AND customer_id=%s ORDER BY id DESC LIMIT 40) x ORDER BY created_at""",
                     (tenant_id, customer_id))
        summ = _rows(cur, "SELECT summary_text AS summary FROM re_chat_summaries WHERE tenant_id=%s AND customer_id=%s",
                     (tenant_id, customer_id))
        inter = _rows(cur, """SELECT i.type, i.summary, i.logged_at, TRIM(COALESCE(s.first_name,'') || ' ' || COALESCE(s.last_name,'')) AS who
                              FROM re_interactions i LEFT JOIN re_staff s ON s.id = i.logged_by
                              WHERE i.tenant_id=%s AND i.contact_id=%s ORDER BY i.logged_at DESC LIMIT 12""",
                      (tenant_id, customer_id))
        acts = _rows(cur, """SELECT title, status, due_at, outcome, done_at FROM re_next_actions
                             WHERE tenant_id=%s AND contact_id=%s ORDER BY created_at DESC LIMIT 4""", (tenant_id, customer_id))
        visits = _rows(cur, """SELECT b.status, s.slot_datetime, l.title FROM re_inspection_bookings b
                               JOIN re_inspection_slots s ON s.id = b.slot_id LEFT JOIN re_property_listings l ON l.id = b.listing_id
                               WHERE b.tenant_id=%s AND b.customer_id=%s ORDER BY s.slot_datetime DESC LIMIT 4""",
                       (tenant_id, customer_id))
        alloc = _rows(cur, """SELECT a.size_label, a.plan_name, a.total, a.deposit, a.monthly, a.months, l.title
                              FROM re_estate_allocations a LEFT JOIN re_property_listings l ON l.id = a.listing_id
                              WHERE a.tenant_id=%s AND a.contact_id=%s""", (tenant_id, customer_id))
        pays = _rows(cur, """SELECT COUNT(*) FILTER (WHERE paid) AS paid_n, COUNT(*) AS n,
                                    COALESCE(SUM(COALESCE(paid_amount, amount)) FILTER (WHERE paid), 0) AS paid_sum,
                                    MIN(due_date) FILTER (WHERE NOT paid) AS next_due
                             FROM re_payment_milestones WHERE tenant_id=%s AND contact_id=%s""", (tenant_id, customer_id))
        calls = _rows(cur, """SELECT status, created_at, duration, answered_by_name FROM re_calls
                              WHERE tenant_id=%s AND customer_id=%s ORDER BY created_at DESC LIMIT 4""", (tenant_id, customer_id))
        hand = _rows(cur, """SELECT action_type, created_at, status FROM re_handoff_requests
                             WHERE tenant_id=%s AND customer_id=%s ORDER BY created_at DESC LIMIT 2""", (tenant_id, customer_id))
    finally:
        cur.close(); conn.close()

    land = LAND.get_land(tenant_id, customer_id) or {}
    estates = LAND.load_estates(tenant_id)
    opts = LAND.options(estates, land, c.get("preferred_area"), float(c["budget_max"]) if c.get("budget_max") else None)
    name = (c.get("name") or "").strip() or "the buyer"

    about = [f"Name: {name}", f"Phone: +{''.join(ch for ch in c.get('phone_number') or '' if ch.isdigit())}",
             f"Came in: {_when(c.get('created_at'), tz)} via {c.get('source') or 'WhatsApp'}",
             f"Stage: {c.get('pipeline_stage') or 'new'}", f"Assigned to: {c.get('agent') or 'nobody yet'}"]
    for k, lbl in (("preferred_area", "Area"), ("property_type_pref", "Looking for"), ("transaction_pref", "To"),
                   ("bedrooms_pref", "Bedrooms")):
        if c.get(k):
            about.append(f"{lbl}: {c[k]}")
    if c.get("budget_max"):
        about.append(f"Budget up to: {LAND.naira(c['budget_max'])}")
    about += LAND.summary_lines(land, estates)
    if land.get("readiness"):
        about.append(f"Readiness: {land['readiness'].title()} ({land.get('readiness_reason') or ''})")
    if c.get("notes"):
        about.append(f"Notes on the lead: {c['notes'][:400]}")

    figures = []
    for r in opts.get("fits") or []:
        figures.append(f"FITS: {r['estate']['title']}, {r['size']['label']}: {LAND._amount_line(r)}")
    for r in opts.get("closest") or []:
        figures.append(f"CLOSEST (doesn't fit yet, short by {LAND.naira(r['gap'])}): {r['estate']['title']}, "
                       f"{r['size']['label']}: {LAND._amount_line(r)}")
    chosen = next((e for e in estates if e["id"] == land.get("estate_listing_id")), None)
    for e in ([chosen] if chosen else opts.get("in_area", [])[:2]):
        figures.append(LAND.estate_facts(e))

    history = []
    if summ and summ[0].get("summary"):
        history.append("Earlier chat, summarised: " + summ[0]["summary"][:1500])
    for m in chat:
        who = "Buyer" if m["role"] == "user" else "KeyLand AI" if m["role"] == "assistant" else m["role"]
        history.append(f"[{_when(m['created_at'], tz)}] {who}: {(m['content'] or '')[:600]}")
    team = [f"[{_when(i['logged_at'], tz)}] {i['type']}{(' by ' + i['who']) if i.get('who') else ''}: {(i['summary'] or '')[:500]}"
            for i in reversed(inter)]
    for a in reversed(acts):
        team.append(f"Next action '{a['title']}' ({a['status']}, due {_when(a['due_at'], tz)})"
                    + (f": {a['outcome']}" if a.get("outcome") else ""))
    for v in visits:
        team.append(f"Inspection {v['status']} for {_when(v['slot_datetime'], tz)} at {v.get('title') or 'the property'}")
    for h in hand:
        team.append(f"AI handed over ({h['action_type']}) on {_when(h['created_at'], tz)}, {h['status']}")
    for k in calls:
        team.append(f"WhatsApp call {_when(k['created_at'], tz)}: {k['status']}"
                    + (f", answered by {k['answered_by_name']}" if k.get("answered_by_name") else ""))
    for a in alloc:
        team.append(f"Plot allocated: {a.get('title')}, {a['size_label']}, {a['plan_name']}, total {LAND.naira(a['total'])}")
    if pays and pays[0]["n"]:
        p = pays[0]
        team.append(f"Payments: {p['paid_n']} of {p['n']} paid ({LAND.naira(p['paid_sum'])})"
                    + (f", next due {p['next_due']:%d %b %Y}" if p.get("next_due") else ""))
    return {"name": name, "about": about, "figures": figures, "history": history, "team": team}


SYSTEM = (
    "You prepare a short brief for a real-estate sales agent in Nigeria who is about to call a buyer. "
    "Use ONLY the information given. Never invent facts, names, dates or amounts. Money amounts may only come from "
    "the ABOUT and FIGURES sections, copied exactly. If something isn't known, say it isn't known. Plain, short English "
    "the agent can read in 30 seconds; the agent may speak Pidgin to the buyer, so keep the suggested words simple.\n"
    "Return ONLY a JSON object:\n"
    '  "headline": one sentence: who this buyer is and where they are in the buying journey.\n'
    '  "wants": up to 4 short points: what they want (estate, plot size, purpose, when).\n'
    '  "money": one or two sentences: what they said they can pay, and which plan fits from FIGURES (copy the amounts), '
    'or what is missing to know.\n'
    '  "worries": up to 3 objects {"point": short, "quote": the buyer\'s own words if they said it, else ""}: '
    "concerns or objections (title documents, distance, price, trust, timing). Empty list if none.\n"
    '  "so_far": up to 5 short points, oldest first: what has happened (chat, calls, inspection, payments).\n'
    '  "say": 2 or 3 short sentences the agent can open the call with, using the buyer\'s first name and the next step.\n'
    '  "ask": up to 3 questions that fill the most important gaps (money, plot size, when, inspection date).\n'
    '  "careful": up to 3 short warnings: e.g. prices are subject to confirmation, never promise a discount, '
    "a plot size is sold out, the buyer asked not to be called at a time, a missed call not yet returned.\n"
)


def write(tenant_id: int, customer_id: int, tz) -> dict:
    from openai import OpenAI
    g = gather(tenant_id, customer_id, tz)
    if not g:
        return {}
    now = datetime.now(timezone.utc).astimezone(tz).strftime("%A %d %B %Y, %H:%M")
    user = (f"Today: {now}\n\nABOUT\n" + "\n".join(g["about"])
            + "\n\nFIGURES (worked out by KeyLand from the agency's prices)\n" + ("\n".join(g["figures"]) or "None")
            + "\n\nCHAT ON WHATSAPP\n" + ("\n".join(g["history"]) or "No chat yet")
            + "\n\nTEAM ACTIVITY\n" + ("\n".join(g["team"]) or "Nothing logged yet"))
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    kw = dict(model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
              messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user[:24000]}],
              max_completion_tokens=2000, response_format={"type": "json_object"},
              reasoning_effort=os.getenv("ESTATE_REASONING_EFFORT", "low"))
    try:
        r = client.chat.completions.create(**kw)
    except Exception as e:
        if "reasoning_effort" not in str(e):
            raise
        kw.pop("reasoning_effort")
        r = client.chat.completions.create(**kw)
    raw = (r.choices[0].message.content or "").strip()
    if not raw:
        kw["max_completion_tokens"] = 3500
        raw = (client.chat.completions.create(**kw).choices[0].message.content or "").strip()
    data = json.loads(raw) if raw else {}
    return {k: data.get(k) for k in ("headline", "wants", "money", "worries", "so_far", "say", "ask", "careful")}
