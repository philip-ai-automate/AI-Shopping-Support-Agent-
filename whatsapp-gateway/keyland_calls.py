"""
keyland_calls.py — WhatsApp calls to a KeyLand agency's number (2026-10-22).

Same flow as wa_calls.py (main portal), for KeyLand numbers (re_tenants):
- connect USER_INITIATED: a buyer is calling. Their lead is found or made,
  the call is saved 'ringing' with Meta's SDP offer, and the agent + managers
  (everyone, if the lead is unassigned) get a phone notification. KeyLand pages
  show the ringing card (api-key-manager estate_calls.py + keyland-calls.js).
  Calls switched off or outside calling hours: rejected, missed-call message.
- terminate: answered → logged on the lead as a call by whoever answered;
  not answered → missed-call message to the buyer, "Missed WhatsApp call" on
  the lead (Needs attention shows it until someone acts), phone notification.
Incoming only (Meta doesn't allow business-started calls from +234 numbers).
"""
import json
from datetime import datetime, timezone

import httpx
import psycopg2.extras

from wa_db import get_db_connection as _db

GRAPH = "https://graph.facebook.com/v23.0"
DEFAULT_MISSED = "Sorry we missed your call. How can we help? Reply here and our team will get back to you shortly."


def _digits(p) -> str:
    return "".join(ch for ch in str(p or "") if ch.isdigit())


def _ts(v):
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc)
    except Exception:
        return None


def _settings(cur, tenant_id: int) -> dict:
    cur.execute("SELECT to_regclass('re_call_settings') IS NOT NULL AS ok")
    if not cur.fetchone()["ok"]:
        return {}
    cur.execute("SELECT * FROM re_call_settings WHERE tenant_id=%s", (tenant_id,))
    return cur.fetchone() or {}


def _in_hours(st: dict) -> bool:
    if not st.get("hours_on"):
        return True
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(st.get("tz") or "Africa/Lagos"))
    except Exception:
        now = datetime.now(timezone.utc)
    days = [int(d) for d in (st.get("days") or "").split(",") if d.strip().isdigit()]
    if (now.isoweekday() % 7) not in days:
        return False
    hm = now.strftime("%H:%M")
    return (st.get("open_time") or "00:00") <= hm < (st.get("close_time") or "23:59")


async def _graph(pnid: str, token: str, body: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{GRAPH}/{pnid}/calls", json=body, headers={"Authorization": f"Bearer {token}"})
        return r.status_code == 200
    except Exception:
        return False


def _people(cur, tenant_id: int, assigned_to) -> list:
    """Who a call rings for: the lead's agent and the managers; everyone if unassigned."""
    cur.execute("SELECT id, role FROM re_staff WHERE tenant_id=%s AND is_active", (tenant_id,))
    ids = [0]                                           # the owner
    for r in cur.fetchall():
        if r["role"] == "admin" or not assigned_to or r["id"] == assigned_to:
            ids.append(r["id"])
    return ids


async def _push(tenant_id: int, staff_ids, title: str, body: str, url: str, tag: str):
    try:
        from keyland_alerts_gw import _devices, _send_one
        import asyncio
        devs = _devices(tenant_id, staff_ids)
        payload = {"title": title[:120], "body": body[:180], "url": url, "tag": tag}
        await asyncio.gather(*(asyncio.to_thread(_send_one, d, payload) for d in devs))
    except Exception as e:
        print("⚠️ [KL CALLS] push error:", e)


def _log_on_lead(cur, call: dict, answered: bool, duration=None):
    """The call on the lead's Interaction Log. A missed call is written by
    'WhatsApp' (no person), so it never counts as someone acting on the lead."""
    if not call.get("customer_id"):
        return
    if answered:
        length = f" · {int(duration) // 60}:{int(duration) % 60:02d}" if duration else ""
        summary = f"📞 WhatsApp call from the buyer, answered by {call.get('answered_by_name') or 'the team'}{length}"
        by = call.get("answered_by") or None
    else:
        summary = "📵 Missed WhatsApp call" + (" (outside calling hours)" if call.get("missed_reason") == "out_of_hours"
                                              else " (calls switched off)" if call.get("missed_reason") == "calls_off" else "")
        by = None
    cur.execute("""INSERT INTO re_interactions (tenant_id, contact_id, type, direction, summary, logged_by)
                   VALUES (%s,%s,'call','inbound',%s,%s)""", (call["tenant_id"], call["customer_id"], summary, by))


async def _missed(cur, conn, tenant: dict, call: dict, reason: str):
    st = _settings(cur, call["tenant_id"])
    text = (st.get("missed_message") or DEFAULT_MISSED).strip()
    ok = False
    if text:
        try:
            from meta_sender import send_text
            ok = await send_text(call["phone_number_id"], tenant["wa_access_token"], call["customer_phone"], text)
        except Exception as e:
            print("⚠️ [KL CALLS] missed message error:", e)
    status = "out_of_hours" if reason in ("out_of_hours", "calls_off") else "missed"
    cur.execute("""UPDATE re_calls SET status=%s, missed_reason=%s, ended_at=COALESCE(ended_at, NOW()), auto_reply=%s
                   WHERE id=%s""", (status, reason, "sent" if ok else ("failed" if text else None), call["id"]))
    _log_on_lead(cur, dict(call, missed_reason=reason), False)
    cur.execute("""INSERT INTO re_wa_message_log (tenant_id, phone_number_id, customer_phone, direction, content, message_type, meta_message_id)
                   VALUES (%s,%s,%s,'inbound',%s,'call',%s) ON CONFLICT DO NOTHING""",
                (call["tenant_id"], call["phone_number_id"], call["customer_phone"], "📵 Missed WhatsApp call",
                 f"call:{call['wa_call_id']}:missed"))
    if ok:
        cur.execute("""INSERT INTO re_wa_message_log (tenant_id, phone_number_id, customer_phone, direction, content, message_type, meta_message_id)
                       VALUES (%s,%s,%s,'outbound',%s,'text',%s) ON CONFLICT DO NOTHING""",
                    (call["tenant_id"], call["phone_number_id"], call["customer_phone"], text, f"call:{call['wa_call_id']}:autoreply"))
    conn.commit()
    if reason not in ("out_of_hours", "calls_off"):
        who = call.get("customer_name") or ("+" + call["customer_phone"])
        await _push(call["tenant_id"], _people(cur, call["tenant_id"], call.get("assigned_to")),
                    f"📵 Missed call from {who}", "Tap to see the lead and call back.",
                    f"https://home.phixtra.com/estate/leads/{call['customer_id']}", f"kl-call-missed-{call['id']}")


async def _on_call(cur, conn, tenant: dict, pnid: str, value: dict, c: dict):
    event, direction, wa_id = c.get("event"), c.get("direction"), c.get("id")
    tenant_id = int(tenant["id"])
    if event == "connect" and direction == "USER_INITIATED":
        from estate_webhook import _ensure_estate_customer
        caller = _digits(c.get("from"))
        prof = next((x.get("profile", {}).get("name") for x in (value.get("contacts") or [])
                     if _digits(x.get("wa_id")) == caller), None)
        cid = _ensure_estate_customer(tenant_id, caller)
        cur.execute("SELECT name, assigned_to FROM re_customers WHERE id=%s", (cid,))
        lead = cur.fetchone() or {}
        if prof and not lead.get("name"):
            cur.execute("UPDATE re_customers SET name=%s WHERE id=%s AND name IS NULL", (prof[:150], cid))
        cur.execute("""INSERT INTO re_calls (tenant_id, phone_number_id, wa_call_id, customer_phone, customer_name,
                                             customer_id, assigned_to, status, offer_sdp)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'ringing',%s) ON CONFLICT (wa_call_id) DO NOTHING RETURNING *""",
                    (tenant_id, pnid, wa_id, caller, lead.get("name") or prof, cid, lead.get("assigned_to"),
                     (c.get("session") or {}).get("sdp")))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return
        st = _settings(cur, tenant_id)
        off = not st or not st.get("incoming_on")
        if off or not _in_hours(st):
            await _graph(pnid, tenant["wa_access_token"], {"messaging_product": "whatsapp", "call_id": wa_id, "action": "reject"})
            await _missed(cur, conn, tenant, row, "calls_off" if off else "out_of_hours")
            return
        name = row["customer_name"] or ("+" + caller)
        print(f"📞 [KL CALLS] tenant={tenant_id} ringing: {name}")
        await _push(tenant_id, _people(cur, tenant_id, row["assigned_to"]), f"📞 {name} is calling on WhatsApp",
                    "Tap to answer in KeyLand.", "https://home.phixtra.com/estate/calls", f"kl-call-{row['id']}")
        return
    if event == "terminate":
        cur.execute("SELECT * FROM re_calls WHERE wa_call_id=%s", (wa_id,))
        row = cur.fetchone()
        if not row:
            return
        dur = c.get("duration")
        if row["status"] in ("answered", "answering", "completed") and row["answered_by_name"]:
            cur.execute("""UPDATE re_calls SET status='completed', duration=COALESCE(%s, duration),
                                  ended_at=COALESCE(%s, ended_at, NOW()) WHERE id=%s""", (dur, _ts(c.get("end_time")), row["id"]))
            if row["status"] != "completed":
                _log_on_lead(cur, row, True, dur or row.get("duration"))
            conn.commit()
            return
        if row["status"] in ("ringing", "answering"):
            await _missed(cur, conn, tenant, row, "no_answer")


async def handle(payload: dict, tenant: dict) -> None:
    conn = _db()
    if not conn:
        return
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        for e in payload.get("entry") or []:
            for ch in e.get("changes") or []:
                v = ch.get("value") or {}
                pnid = (v.get("metadata") or {}).get("phone_number_id") or ""
                for c in v.get("calls") or []:
                    await _on_call(cur, conn, tenant, pnid, v, c)
    except Exception as ex:
        conn.rollback()
        print("⚠️ [KL CALLS] webhook error:", ex)
    finally:
        cur.close(); conn.close()
