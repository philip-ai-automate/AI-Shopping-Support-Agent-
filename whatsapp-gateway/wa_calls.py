"""
wa_calls.py — WhatsApp calls arriving from Meta (2026-10-20, replaced PressOne).

Meta sends call events on the same /meta-webhook as messages, with
"field": "calls". This file records them in wa_calls; the portal does the
rest (calls_routes.py + static/portal/wa-calls.js):

- connect, USER_INITIATED: a customer is calling. Saved as 'ringing' with
  Meta's SDP offer; every team member allowed to answer gets a phone
  notification. The browser that answers sends Meta the SDP answer
  (pre_accept + accept) through the portal. Outside the number's calling
  hours (or with calls switched off) it's rejected straight away and the
  customer gets the "missed call" WhatsApp message.
- connect, BUSINESS_INITIATED: Meta's SDP answer for a call a team member
  started; the browser picks it up from the portal.
- statuses (type call): RINGING / ACCEPTED / REJECTED for outgoing calls.
- terminate: the call ended. Unanswered → 'missed', the missed-call message
  goes to the customer and the team gets a call-back alert.
- messages[].interactive.type == call_permission_reply: the customer said
  yes/no to "Can we call you?" (outgoing calls, UK and other allowed
  countries — Meta doesn't allow business-initiated calls from +234).

Meta reference: developers.facebook.com/docs/whatsapp/cloud-api/calling
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import psycopg2.extras

from wa_db import get_db_connection as _db, log_message
from tenant_router import get_tenant_by_phone_number_id

GRAPH = "https://graph.facebook.com/v23.0"
DEFAULT_MISSED = "Sorry we missed your call. How can we help? Reply here and our team will get back to you shortly."


def _digits(p) -> str:
    return "".join(ch for ch in str(p or "") if ch.isdigit())


def _ts(v):
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc)
    except Exception:
        return None


def is_call_payload(payload: dict) -> bool:
    """Calls, call statuses, or a reply to "Can we call you?"."""
    for e in payload.get("entry") or []:
        for ch in e.get("changes") or []:
            v = ch.get("value") or {}
            if ch.get("field") == "calls":
                return True
            for m in v.get("messages") or []:
                if (m.get("interactive") or {}).get("type") == "call_permission_reply":
                    return True
    return False


def first_phone_number_id(payload: dict) -> str:
    for e in payload.get("entry") or []:
        for ch in e.get("changes") or []:
            pnid = ((ch.get("value") or {}).get("metadata") or {}).get("phone_number_id")
            if pnid:
                return pnid
    return ""


def _settings(cur, pnid: str) -> dict:
    cur.execute("SELECT * FROM wa_call_settings WHERE phone_number_id=%s", (pnid,))
    return cur.fetchone() or {}


def _plan_has_calls(cur, tenant_id: int) -> bool:
    """WhatsApp calls are an Enterprise feature (voice.calls on the plan)."""
    cur.execute("""SELECT 1 FROM tenants t JOIN plan_feature_grants g ON g.plan_id = t.plan_id
                   WHERE t.id=%s AND g.feature_key='voice.calls' LIMIT 1""", (tenant_id,))
    return bool(cur.fetchone())


def _in_hours(st: dict) -> bool:
    if not st.get("hours_on"):
        return True
    h = st.get("hours") or {}
    if isinstance(h, str):
        h = json.loads(h or "{}")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(h.get("tz") or "Africa/Lagos"))
    except Exception:
        now = datetime.now(timezone.utc)
    if (now.isoweekday() % 7) not in (h.get("days") or [1, 2, 3, 4, 5]):
        return False
    hm = now.strftime("%H%M")
    return (h.get("from") or "0000") <= hm < (h.get("to") or "2400")


def _hours(st: dict) -> dict:
    h = st.get("hours") or {}
    if isinstance(h, str):
        h = json.loads(h or "{}")
    return h


def callback_due(st: dict, start: datetime) -> datetime | None:
    """When a missed call must be called back by: the number's call-back
    minutes, counted only inside its calling hours (a call at 4:50pm with
    30 minutes and 5pm closing is due 9:20am on the next open day)."""
    if st.get("callback_on") is False:
        return None
    mins = int(st.get("callback_mins") or 30)
    if not st.get("hours_on"):
        return start + timedelta(minutes=mins)
    h = _hours(st)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(h.get("tz") or "Africa/Lagos")
    except Exception:
        tz = timezone.utc
    days = h.get("days") or [1, 2, 3, 4, 5]
    fh, fm = int((h.get("from") or "0900")[:2]), int((h.get("from") or "0900")[2:])
    th, tm = int((h.get("to") or "1700")[:2]), int((h.get("to") or "1700")[2:])
    t, left = start.astimezone(tz), mins
    for _ in range(30):
        open_t = t.replace(hour=fh, minute=fm, second=0, microsecond=0)
        close_t = t.replace(hour=min(th, 23), minute=tm if th < 24 else 59, second=0, microsecond=0)
        if (t.isoweekday() % 7) in days and t < close_t:
            t = max(t, open_t)
            room = (close_t - t).total_seconds() / 60
            if room >= left:
                return (t + timedelta(minutes=left)).astimezone(timezone.utc)
            left -= room
        t = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start + timedelta(minutes=mins)


def _person(cur, tenant_id: int, phone: str) -> dict:
    """Lead / contact for this caller, and who the lead belongs to."""
    d = _digits(phone)
    out = {"lead_id": None, "contact_id": None, "assigned_key": None, "name": None}
    if len(d) < 7:
        return out
    cur.execute("""SELECT id, assigned_key, COALESCE(NULLIF(contact_person,''), customer_name) AS name
                     FROM merchant_pipeline_leads
                    WHERE tenant_id=%s AND (regexp_replace(COALESCE(whatsapp_number,''),'\\D','','g')=%s
                                         OR regexp_replace(COALESCE(phone,''),'\\D','','g')=%s)
                    ORDER BY (outcome IS NULL) DESC, id DESC LIMIT 1""", (tenant_id, d, d))
    r = cur.fetchone()
    if r:
        out.update(lead_id=r["id"], assigned_key=r["assigned_key"], name=r["name"])
    cur.execute("""SELECT id, display_name AS name FROM wa_contacts
                    WHERE tenant_id=%s AND (regexp_replace(COALESCE(whatsapp_number,''),'\\D','','g')=%s
                                         OR regexp_replace(COALESCE(phone,''),'\\D','','g')=%s)
                    ORDER BY id DESC LIMIT 1""", (tenant_id, d, d))
    c = cur.fetchone()
    if c:
        out["contact_id"] = c["id"]
        out["name"] = out["name"] or c["name"]
    return out


def _answer_member_ids(cur, tenant_id: int, phone: str) -> list:
    """Team members whose role may answer calls and who may see this chat."""
    from staff_alerts import _member_sees_chat
    cur.execute("""SELECT tm.id, r.permissions FROM team_members tm
                     LEFT JOIN tenant_roles r ON r.id = tm.role_id AND r.tenant_id = tm.tenant_id
                    WHERE tm.tenant_id=%s AND tm.is_active""", (tenant_id,))
    ids = []
    for r in cur.fetchall():
        perms = r["permissions"] or {}
        if isinstance(perms, str):
            perms = json.loads(perms or "{}")
        if perms.get("calls.answer") and _member_sees_chat(cur, tenant_id, r["id"], numbers=[phone]):
            ids.append(r["id"])
    return ids


async def _graph(pnid: str, token: str, body: dict) -> tuple:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{GRAPH}/{pnid}/calls", json=body, headers={"Authorization": f"Bearer {token}"})
        return r.status_code == 200, r.text[:400]
    except Exception as e:
        return False, str(e)


async def _send_text(pnid: str, token: str, to: str, text: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{GRAPH}/{pnid}/messages", headers={"Authorization": f"Bearer {token}"},
                             json={"messaging_product": "whatsapp", "recipient_type": "individual",
                                   "to": to, "type": "text", "text": {"body": text}})
        return r.status_code == 200
    except Exception:
        return False


async def _push(tenant_id: int, member_ids, title: str, body: str, tag: str) -> None:
    try:
        from push_notify import push_to
        await push_to(tenant_id, member_ids, True, title=title, body=body, url="/calls", tag=tag)
    except Exception as e:
        print("⚠️ [CALLS] push error:", e)


def lead_history(cur, call: dict, outcome: str, duration=None) -> None:
    """Put a WhatsApp call in the lead's call history (the lead page's Calls
    list), count it for whoever handled it in Team performance (an "Answered"
    call in staff_activity, like Log a call), and mark the lead as worked."""
    if not call.get("lead_id"):
        return
    who_key = call.get("answered_by_key") or call.get("started_by_key")
    who = call.get("answered_by_name") or call.get("started_by_name")
    mins = -(-int(duration) // 60) if duration else None
    length = f" · {int(duration) // 60}:{int(duration) % 60:02d}" if duration else ""
    kind = "outgoing" if call.get("direction") == "out" else "incoming"
    if outcome == "answered":
        note = f"📞 WhatsApp call ({kind}){length}"
    elif call.get("direction") == "out":
        note = "📞 WhatsApp call (outgoing) · not answered"
    else:
        note = "📵 Missed WhatsApp call" + (" (outside calling hours)" if call.get("missed_reason") == "out_of_hours" else "")
    cur.execute("""INSERT INTO lead_calls (tenant_id, lead_id, actor_key, actor_name, outcome, minutes, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (call["tenant_id"], call["lead_id"], who_key, who or "WhatsApp", outcome, mins, note))
    if who_key:
        cur.execute("""INSERT INTO staff_activity (tenant_id, actor_key, actor_name, action, lead_id, qty, detail)
                       VALUES (%s,%s,%s,'call',%s,1,%s)""",
                    (call["tenant_id"], who_key, who, call["lead_id"],
                     ("Answered" if outcome == "answered" else "No answer") + (f" · {mins} min" if mins else "") + " · WhatsApp call"))
    cur.execute("UPDATE merchant_pipeline_leads SET updated_at=NOW() WHERE id=%s AND tenant_id=%s",
                (call["lead_id"], call["tenant_id"]))


async def _missed(cur, conn, tenant: dict, call: dict, reason: str) -> None:
    """Unanswered call: mark missed, message the customer, alert the team."""
    st = _settings(cur, call["phone_number_id"])
    text = (st.get("missed_message") or DEFAULT_MISSED).strip()
    ok = await _send_text(call["phone_number_id"], tenant["access_token"], call["customer_phone"], text) if text else False
    cur.execute("""UPDATE wa_calls SET status=%s, missed_reason=%s, ended_at=COALESCE(ended_at, NOW()), auto_reply=%s,
                                       callback_due_at=%s
                    WHERE id=%s""", ("out_of_hours" if reason in ("out_of_hours", "calls_off") else "missed", reason,
                                      "sent" if ok else ("failed" if text else None),
                                      callback_due(st, call["created_at"]) if st else None, call["id"]))
    lead_history(cur, dict(call, missed_reason=reason), "no_answer")
    conn.commit()
    who = call.get("customer_name") or ("+" + _digits(call["customer_phone"]))
    log_message(tenant["tenant_id"], call["phone_number_id"], call["customer_phone"], "inbound",
                "📵 Missed WhatsApp call" + {"out_of_hours": " (outside calling hours)", "calls_off": " (calls switched off)"}.get(reason, ""),
                "call", f"call:{call['wa_call_id']}:missed")
    if ok:
        log_message(tenant["tenant_id"], call["phone_number_id"], call["customer_phone"], "outbound", text,
                    "agent_reply", f"call:{call['wa_call_id']}:autoreply")
    if reason not in ("out_of_hours", "calls_off"):
        await _push(tenant["tenant_id"], _answer_member_ids(cur, tenant["tenant_id"], call["customer_phone"]),
                    f"📵 Missed call from {who}", "Tap to see it and call back.", f"call-missed-{call['id']}")


async def _on_call(cur, conn, tenant: dict, pnid: str, value: dict, c: dict) -> None:
    event, direction, wa_id = c.get("event"), c.get("direction"), c.get("id")
    tenant_id = int(tenant["tenant_id"])
    if event == "connect" and direction == "USER_INITIATED":
        caller = c.get("from") or ""
        prof = next((x.get("profile", {}).get("name") for x in (value.get("contacts") or [])
                     if _digits(x.get("wa_id")) == _digits(caller)), None)
        who = _person(cur, tenant_id, caller)
        st = _settings(cur, pnid)
        cur.execute("""INSERT INTO wa_calls (tenant_id, phone_number_id, wa_call_id, direction, customer_phone,
                                              customer_name, lead_id, contact_id, ring_first_key, status, offer_sdp)
                       VALUES (%s,%s,%s,'in',%s,%s,%s,%s,%s,'ringing',%s)
                       ON CONFLICT (wa_call_id) DO NOTHING RETURNING *""",
                    (tenant_id, pnid, wa_id, _digits(caller), who["name"] or prof, who["lead_id"], who["contact_id"],
                     who["assigned_key"] if st.get("ring_first", True) else None,
                     (c.get("session") or {}).get("sdp")))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return
        # The portal's Call Settings page decides: a number only rings when
        # "Incoming calls" is saved as on there. Never saved = off.
        # Plans below Enterprise don't get calls at all.
        off = not st or not st.get("incoming_on") or not _plan_has_calls(cur, tenant_id)
        if off or not _in_hours(st):
            await _graph(pnid, tenant["access_token"], {"messaging_product": "whatsapp", "call_id": wa_id, "action": "reject"})
            await _missed(cur, conn, tenant, row, "calls_off" if off else "out_of_hours")
            return
        name = row["customer_name"] or ("+" + row["customer_phone"])
        print(f"📞 [CALLS] tenant={tenant_id} ringing: {name}")
        await _push(tenant_id, _answer_member_ids(cur, tenant_id, caller),
                    f"📞 {name} is calling on WhatsApp", "Tap to answer. Ringing for your team.", f"call-{row['id']}")
        return
    if event == "connect" and direction == "BUSINESS_INITIATED":
        cur.execute("""INSERT INTO wa_calls (tenant_id, phone_number_id, wa_call_id, direction, customer_phone, status, answer_sdp)
                       VALUES (%s,%s,%s,'out',%s,'calling',%s)
                       ON CONFLICT (wa_call_id) DO UPDATE SET answer_sdp = EXCLUDED.answer_sdp""",
                    (tenant_id, pnid, wa_id, _digits(c.get("to")), (c.get("session") or {}).get("sdp")))
        conn.commit()
        return
    if event == "terminate":
        cur.execute("SELECT * FROM wa_calls WHERE wa_call_id=%s", (wa_id,))
        row = cur.fetchone()
        if not row:
            return
        dur = c.get("duration")
        if row["direction"] == "out":
            status = "completed" if dur else ("rejected" if row["status"] == "rejected" else "missed")
            cur.execute("""UPDATE wa_calls SET status=%s, duration=%s, ended_at=COALESCE(%s, NOW()),
                                  answered_at=COALESCE(answered_at, %s) WHERE id=%s""",
                        (status, dur, _ts(c.get("end_time")), _ts(c.get("start_time")), row["id"]))
            lead_history(cur, row, "answered" if dur else "no_answer", dur)
            conn.commit()
            log_message(tenant_id, pnid, row["customer_phone"], "outbound",
                        f"📞 WhatsApp call by {row['started_by_name'] or 'the team'}"
                        + (f" · {int(dur) // 60}:{int(dur) % 60:02d}" if dur else " · not answered"),
                        "call", f"call:{wa_id}:end")
            return
        if row["status"] in ("answered", "answering", "completed") and row["answered_by_key"]:
            cur.execute("""UPDATE wa_calls SET status='completed', duration=COALESCE(%s, duration),
                                  ended_at=COALESCE(%s, ended_at, NOW()) WHERE id=%s""",
                        (dur, _ts(c.get("end_time")), row["id"]))
            lead_history(cur, row, "answered", dur or row.get("duration"))
            conn.commit()
            log_message(tenant_id, pnid, row["customer_phone"], "outbound",
                        f"📞 WhatsApp call answered by {row['answered_by_name'] or 'the team'}"
                        + (f" · {int(dur) // 60}:{int(dur) % 60:02d}" if dur else ""),
                        "call", f"call:{wa_id}:end")
            return
        if row["status"] in ("ringing", "answering"):
            cur.execute("""SELECT 1 FROM call_presence WHERE tenant_id=%s AND last_seen >= %s - interval '45 seconds' LIMIT 1""",
                        (tenant_id, row["created_at"]))
            await _missed(cur, conn, tenant, row, "no_answer" if cur.fetchone() else "nobody_online")


async def _on_status(cur, conn, s: dict) -> None:
    st = (s.get("status") or "").upper()
    wa_id = s.get("id")
    if st == "ACCEPTED":
        cur.execute("UPDATE wa_calls SET status='answered', answered_at=COALESCE(answered_at, NOW()) WHERE wa_call_id=%s AND direction='out'", (wa_id,))
    elif st == "REJECTED":
        cur.execute("UPDATE wa_calls SET status='rejected', ended_at=NOW() WHERE wa_call_id=%s AND direction='out'", (wa_id,))
    conn.commit()


def _on_permission(cur, conn, tenant: dict, pnid: str, m: dict) -> None:
    rep = (m.get("interactive") or {}).get("call_permission_reply") or {}
    ok = rep.get("response") == "accept"
    phone = _digits(m.get("from"))
    cur.execute("""INSERT INTO wa_call_permissions (phone_number_id, customer_phone, tenant_id, status, permanent, expires_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,NOW())
                   ON CONFLICT (phone_number_id, customer_phone) DO UPDATE SET status=EXCLUDED.status,
                       permanent=EXCLUDED.permanent, expires_at=EXCLUDED.expires_at, updated_at=NOW()""",
                (pnid, phone, int(tenant["tenant_id"]), "accepted" if ok else "rejected",
                 bool(rep.get("is_permanent")), _ts(rep.get("expiration_timestamp"))))
    conn.commit()
    log_message(int(tenant["tenant_id"]), pnid, phone, "inbound",
                "✅ Agreed to WhatsApp calls" if ok else "🚫 Said no to WhatsApp calls", "call", m.get("id"))


async def handle_call_webhook(payload: dict) -> None:
    """Every call event in the payload. Signature is checked by the caller."""
    conn = _db()
    if not conn:
        return
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        for e in payload.get("entry") or []:
            for ch in e.get("changes") or []:
                v = ch.get("value") or {}
                pnid = (v.get("metadata") or {}).get("phone_number_id") or ""
                tenant = get_tenant_by_phone_number_id(pnid) if pnid else None
                if not tenant:
                    continue
                for c in v.get("calls") or []:
                    await _on_call(cur, conn, tenant, pnid, v, c)
                if ch.get("field") == "calls":
                    for s in v.get("statuses") or []:
                        await _on_status(cur, conn, s)
                for m in v.get("messages") or []:
                    if (m.get("interactive") or {}).get("type") == "call_permission_reply":
                        _on_permission(cur, conn, tenant, pnid, m)
    except Exception as ex:
        conn.rollback()
        print("⚠️ [CALLS] webhook error:", ex)
    finally:
        cur.close(); conn.close()


# ── Call-back deadline (every minute) ───────────────────────────────────────

def _manager_targets(cur, tenant_id: int):
    """Owner + team members whose role has the Calls report tick: (member ids, emails)."""
    cur.execute("""SELECT tm.id, tm.email, r.permissions FROM team_members tm
                     LEFT JOIN tenant_roles r ON r.id = tm.role_id AND r.tenant_id = tm.tenant_id
                    WHERE tm.tenant_id=%s AND tm.is_active""", (tenant_id,))
    ids, emails = [], []
    for r in cur.fetchall():
        perms = r["permissions"] or {}
        if isinstance(perms, str):
            perms = json.loads(perms or "{}")
        if perms.get("calls.report"):
            ids.append(r["id"])
            if r["email"]:
                emails.append(r["email"])
    cur.execute("""SELECT c.email, t.name AS business FROM customers c JOIN tenants t ON t.id = c.tenant_id
                    WHERE c.tenant_id=%s ORDER BY c.id LIMIT 1""", (tenant_id,))
    owner = cur.fetchone() or {}
    if owner.get("email"):
        emails.append(owner["email"])
    return ids, sorted(set(e.strip().lower() for e in emails if e)), owner.get("business") or "Your business"


async def run_call_deadlines() -> int:
    """Missed calls nobody has claimed with "I'll call back" by their
    deadline: alert the managers once (phone notification + email)."""
    conn = _db()
    if not conn:
        return 0
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    n = 0
    try:
        cur.execute("""UPDATE wa_calls SET overdue_alerted_at=NOW()
                        WHERE direction='in' AND status IN ('missed','out_of_hours') AND callback_by_key IS NULL
                          AND callback_due_at <= NOW() AND overdue_alerted_at IS NULL
                          AND created_at > NOW() - interval '7 days'
                    RETURNING *""")
        due = cur.fetchall()
        conn.commit()
        for c in due:
            ids, emails, biz = _manager_targets(cur, c["tenant_id"])
            who = c["customer_name"] or ("+" + c["customer_phone"])
            late = int((datetime.now(timezone.utc) - c["callback_due_at"]).total_seconds() // 60)
            mins = int((c["callback_due_at"] - c["created_at"]).total_seconds() // 60)
            await _push(c["tenant_id"], ids, f"⏰ Missed call not called back: {who}",
                        f"Called {c['created_at'].astimezone(timezone.utc).strftime('%H:%M')} UTC. Nobody has pressed I'll call back.",
                        f"call-overdue-{c['id']}")
            try:
                from staff_alerts import _send_email
                import asyncio as _a
                for e in emails:
                    await _a.to_thread(_send_email, e, f"Missed call not called back: {who}", "A missed WhatsApp call is overdue",
                                       biz, [("Caller", who), ("Number", "+" + c["customer_phone"]),
                                             ("Called", c["created_at"].strftime("%d %b %Y, %H:%M UTC")),
                                             ("Overdue by", f"{max(late, 0)} min (deadline was {mins} min of calling time)")],
                                       "Open Calls › Call Log in the portal and press I'll call back. You get this because you can see the Calls report.")
            except Exception as ex:
                print("⚠️ [CALLS] overdue email error:", ex)
            n += 1
        if n:
            print(f"⏰ [CALLS] overdue call-back alerts: {n}")
    except Exception as ex:
        conn.rollback()
        print("⚠️ [CALLS] deadline check error:", ex)
    finally:
        cur.close(); conn.close()
    return n
