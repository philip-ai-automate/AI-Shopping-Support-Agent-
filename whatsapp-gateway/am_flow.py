"""Account Manager check-in buttons (2026-11-06).

The check-in template phixtra_setup_help (sent by Kelvin from Admin ›
Accounts / Admin › Inbox) has three quick-reply buttons. A tap arrives here
(see message_normalizer: action_type "am_button") after the tap is already
logged in wa_message_log, so it shows in Admin › Inbox. Then:

  am_stuck     → instant list "Which part was the problem?" (7 choices)
  am_stuck_*   → their choice is saved as the reason; "Something else" asks
                 them to describe it
  am_question  → instant "What would you like to know? Type it here"
  am_later     → no message; next follow-up after later_days (Settings, default 14)

The AI never answers (the customer is in am_wa_quiet). The Account Manager
is emailed by am_reply_alerts.py. Automatic messages are logged with
message_type 'am_auto' so the chat still counts as "Needs reply".
"""
import re
from datetime import date, timedelta

import httpx

from wa_db import get_db_connection

GRAPH = "https://graph.facebook.com/v19.0"
AM_NAME = "Kelvin"

STUCK_OPTIONS = [
    ("am_stuck_whatsapp", "Connecting WhatsApp", ""),
    ("am_stuck_meta", "Meta verification", "Facebook / Meta business check"),
    ("am_stuck_products", "Adding my products", ""),
    ("am_stuck_website", "Website or plugin set-up", ""),
    ("am_stuck_plan", "Choosing a plan / price", ""),
    ("am_stuck_how", "Not sure how it works", ""),
    ("am_stuck_other", "Something else", ""),
]
STUCK_LABEL = {k: t for k, t, _ in STUCK_OPTIONS}


def _d(s):
    return re.sub(r"\D", "", s or "")


def _account(cur, phone):
    """The signed-up business whose owner has this WhatsApp number."""
    cur.execute("""SELECT c.tenant_id, c.first_name FROM customers c
                     JOIN tenants t ON t.id = c.tenant_id
                    WHERE regexp_replace(COALESCE(c.phone_number,''),'\\D','','g') = %s
                    ORDER BY t.created_at DESC LIMIT 1""", (_d(phone),))
    r = cur.fetchone()
    return (r[0], (r[1] or "").strip()) if r else (None, "")


def _state(cur, tid, **cols):
    cur.execute("INSERT INTO am_account_state (tenant_id, updated_by) VALUES (%s,'WhatsApp button') ON CONFLICT DO NOTHING",
                (tid,))
    sets = ", ".join(f"{k}=%s" for k in cols)
    cur.execute(f"UPDATE am_account_state SET {sets}, updated_at=NOW(), updated_by='WhatsApp button' WHERE tenant_id=%s",
                (*cols.values(), tid))


def _activity(cur, tid, body, follow_up_on=None):
    cur.execute("""INSERT INTO am_activity (tenant_id, kind, channel, body, follow_up_on, by_admin, ok)
                   VALUES (%s,'note','whatsapp',%s,%s,'WhatsApp button',TRUE)""", (tid, body, follow_up_on))


async def _send(tenant_id, phone_number_id, token, to, payload, logged):
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": _d(to), **payload}
    mid = None
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{GRAPH}/{phone_number_id}/messages", json=payload,
                             headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            mid = ((r.json().get("messages") or [{}])[0]).get("id")
        else:
            print(f"⚠️ [AM] send refused: {r.text[:300]}")
            return False
    except Exception as e:
        print("⚠️ [AM] send error:", e)
        return False
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO wa_message_log (tenant_id, phone_number_id, customer_phone, direction, content,
                                                   message_type, sent_by_label, meta_message_id)
                       VALUES (%s,%s,%s,'outbound',%s,'am_auto',%s,%s)""",
                    (tenant_id, phone_number_id, _d(to), logged, "Automatic (Account Manager)", mid))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return True


async def handle(tenant_id, phone_number_id, token, customer_phone, key, text) -> bool:
    """Handle one Account Manager button tap / list choice. True = handled."""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        tid, first = _account(cur, customer_phone)
        if tid:
            if key == "am_stuck":
                _state(cur, tid, status="replied")
                _activity(cur, tid, "Tapped “I got stuck” on the check-in message.")
            elif key.startswith("am_stuck_"):
                label = STUCK_LABEL.get(key, text)
                _state(cur, tid, status="replied", reason="setup_unclear", reason_note=f"Stuck at: {label}")
                _activity(cur, tid, f"Got stuck at: {label}")
            elif key == "am_question":
                cur.execute("SELECT reason FROM am_account_state WHERE tenant_id=%s", (tid,))
                r = cur.fetchone()
                _state(cur, tid, status="replied")
                if not r or not r[0]:
                    _state(cur, tid, reason="other", reason_note="Has a question")
                _activity(cur, tid, "Tapped “I have a question” on the check-in message.")
            elif key == "am_later":
                cur.execute("SELECT value FROM am_settings WHERE key='later_days'")
                r = cur.fetchone()  # Admin › Accounts › Settings (default 14)
                fu = date.today() + timedelta(days=int(r[0]) if r and str(r[0]).isdigit() else 14)
                _state(cur, tid, status="replied", reason="no_time", reason_note="Not right now", next_follow_up=fu)
                _activity(cur, tid, f"Tapped “Not right now”. Follow up on {fu:%-d %b %Y}.", follow_up_on=fu)
            conn.commit()
    finally:
        cur.close(); conn.close()

    hi = f"Thanks, {first}." if first else "Thanks."
    if key == "am_stuck":
        body = f"{hi} Which part was the problem?"
        await _send(tenant_id, phone_number_id, token, customer_phone, {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body},
                "footer": {"text": "PhiXtra · Enquiry-to-Revenue"},
                "action": {"button": "Choose an option", "sections": [{
                    "title": "Where did you get stuck",
                    "rows": [dict({"id": k, "title": t}, **({"description": d} if d else {}))
                             for k, t, d in STUCK_OPTIONS]}]},
            }}, body + "\n[List: " + " · ".join(t for _, t, _ in STUCK_OPTIONS) + "]")
    elif key == "am_stuck_other":
        msg = "No problem. Tell me in a few words what happened, and I'll help you sort it out."
        await _send(tenant_id, phone_number_id, token, customer_phone,
                    {"type": "text", "text": {"body": msg}}, msg)
    elif key == "am_question":
        name = f", {first}" if first else ""
        msg = (f"Of course{name}. What would you like to know?\n\n"
               f"Just type your question here and I'll reply personally.\n\n{AM_NAME}, PhiXtra")
        await _send(tenant_id, phone_number_id, token, customer_phone,
                    {"type": "text", "text": {"body": msg}}, msg)
    print(f"   [AM] button {key} from {customer_phone} (account {tid})")
    return True
