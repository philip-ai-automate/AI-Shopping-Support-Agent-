"""
calls_routes.py — WhatsApp calls in the portal (2026-10-20, replaced PressOne).

Customers tap the call button in their WhatsApp chat with the business. The
gateway (whatsapp-gateway/wa_calls.py) saves the call as 'ringing'; every
team member allowed to answer sees it ring through /calls/live (polled by
static/portal/wa-calls.js on every portal page). The first to press Answer
claims it, their browser makes the WebRTC answer and this file hands it to
Meta (pre_accept + accept). Everyone else sees "Answered by …".

Outgoing calls only where Meta allows them (not +234, +1, +20, +84 business
numbers): the customer must first accept "Can we call you?".

  GET  /calls/live                 polling: ringing calls for me, my call, taken calls
  POST /calls/<id>/claim           first to answer → Meta's SDP offer
  POST /calls/<id>/accept          browser's SDP answer → Meta
  POST /calls/<id>/release         couldn't answer (no microphone) → rings again
  POST /calls/<id>/end             hang up
  POST /calls/<id>/callback        "I'll call back" on a missed call
  POST /calls/permission           send "Can we call you?"
  POST /calls/start                start an outgoing call (SDP offer)
  GET  /calls                      the Calls page
  GET  /reports/calls (+ .xlsx)    the Calls report
  GET/POST /calls/settings/<pnid>  calling settings for one WhatsApp number
"""
import io
import json
from datetime import datetime, timedelta, timezone

import psycopg2.extras
import requests as _req
from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_file, url_for

from db import get_db_connection
from feature_access import team_feature
from portal_routes import (
    _require_login, _customer_id, _get_customer, _require_team_permission, _team_member_has_permission,
    _current_actor, _inject_granted_features, _require_plan_sub_feature, _plan_grants_feature,
    _get_tenant_plan, _inbox_can_see, _link_lead_to_contact,
)

calls_bp = Blueprint("calls", __name__)
calls_bp.context_processor(_inject_granted_features)

GRAPH = "https://graph.facebook.com/v23.0"
# Meta: business-initiated calls aren't available from business numbers in these countries.
NO_OUTGOING = ("234", "1", "20", "84")
DEFAULT_MISSED = "Sorry we missed your call. How can we help? Reply here and our team will get back to you shortly."
RING_SECONDS = 60
DAYS = [(1, "Mon"), (2, "Tue"), (3, "Wed"), (4, "Thu"), (5, "Fri"), (6, "Sat"), (0, "Sun")]
META_DAYS = {0: "SUNDAY", 1: "MONDAY", 2: "TUESDAY", 3: "WEDNESDAY", 4: "THURSDAY", 5: "FRIDAY", 6: "SATURDAY"}


def _digits(p) -> str:
    return "".join(ch for ch in str(p or "") if ch.isdigit())


def _ctx():
    customer = _get_customer(_customer_id())
    return customer, int(customer["tenant_id"]), _current_actor(customer)


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _numbers(cur, tenant_id: int) -> list:
    cur.execute("""SELECT wt.phone_number_id, wt.display_phone_number, wt.label, wt.verified_name, wt.access_token,
                          s.incoming_on, s.outgoing_on
                     FROM wa_tenants wt LEFT JOIN wa_call_settings s ON s.phone_number_id = wt.phone_number_id
                    WHERE wt.tenant_id=%s AND wt.active ORDER BY wt.id""", (tenant_id,))
    return cur.fetchall()


def _outgoing_allowed_country(display: str) -> bool:
    d = _digits(display)
    return not any(d.startswith(c) for c in NO_OUTGOING)


def _graph(pnid: str, token: str, body: dict, path: str = "calls"):
    """(ok, json-or-error-text) for a POST to Meta."""
    try:
        r = _req.post(f"{GRAPH}/{pnid}/{path}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        try:
            data = r.json()
        except ValueError:
            data = {"error": {"message": r.text[:300]}}
        return r.status_code == 200, data
    except Exception as e:
        return False, {"error": {"message": str(e)}}


def _meta_error(data) -> str:
    err = (data or {}).get("error") or {}
    return (err.get("error_user_msg") or err.get("message") or "Meta didn't accept it.")[:300]


def _token(cur, pnid: str, tenant_id: int):
    cur.execute("SELECT access_token, display_phone_number FROM wa_tenants WHERE phone_number_id=%s AND tenant_id=%s AND active",
                (pnid, tenant_id))
    return cur.fetchone()


def _may_answer(tenant_id: int) -> bool:
    return _team_member_has_permission("calls.answer") and _plan_grants_feature(_get_tenant_plan(tenant_id), "calls.answer")


def _sees(tenant_id: int, phone: str) -> bool:
    d = _digits(phone)
    return _inbox_can_see(tenant_id, d, numbers=[d])


def _card(row: dict) -> dict:
    return {"id": row["id"], "name": row.get("customer_name") or ("+" + row["customer_phone"]),
            "phone": "+" + row["customer_phone"], "status": row["status"], "direction": row["direction"],
            "by": row.get("answered_by_name") or row.get("started_by_name"),
            "answered_at": row["answered_at"].isoformat() if row.get("answered_at") else None,
            "lead_id": row.get("lead_id"), "contact_id": row.get("contact_id")}


# ── Live ringing ─────────────────────────────────────────────────────────────

@calls_bp.route("/calls/live")
@team_feature("calls.answer", "calls.make")
def calls_live():
    """What wa-calls.js shows: calls ringing for me, my own call, and calls
    someone else just answered. Also records that this person has the
    portal open."""
    if _require_login():
        return jsonify({"error": "login"}), 401
    customer, tenant_id, me = _ctx()
    if not (_may_answer(tenant_id) or _team_member_has_permission("calls.make")):
        return jsonify({"enabled": False})
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO call_presence (tenant_id, actor_key, last_seen) VALUES (%s,%s,NOW())
                       ON CONFLICT (tenant_id, actor_key) DO UPDATE SET last_seen=NOW()""", (tenant_id, me["key"]))
        conn.commit()
        cur.execute(f"""SELECT c.*, COALESCE(s.ring_first_secs, 15) AS rf_secs, COALESCE(s.ring_first, TRUE) AS rf_on,
                               EXTRACT(EPOCH FROM NOW() - c.created_at) AS age
                          FROM wa_calls c LEFT JOIN wa_call_settings s ON s.phone_number_id = c.phone_number_id
                         WHERE c.tenant_id=%s AND (
                               (c.status IN ('ringing','answering') AND c.created_at > NOW() - interval '{RING_SECONDS} seconds')
                            OR (c.status IN ('answering','answered','calling') AND (c.answered_by_key=%s OR c.started_by_key=%s))
                            OR (c.status IN ('answered','completed','missed') AND c.created_at > NOW() - interval '2 minutes'))
                         ORDER BY c.created_at DESC LIMIT 20""", (tenant_id, me["key"], me["key"]))
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    ringing, taken, mine = [], [], None
    can_answer = _may_answer(tenant_id)
    for r in rows:
        if (r["answered_by_key"] == me["key"] or r["started_by_key"] == me["key"]) and r["status"] in ("answering", "answered", "calling"):
            mine = dict(_card(r), answer_sdp=r["answer_sdp"] if r["direction"] == "out" else None)
            continue
        if not can_answer or not _sees(tenant_id, r["customer_phone"]):
            continue
        if r["status"] == "ringing" and r["direction"] == "in":
            first = r["ring_first_key"]
            if first and r["rf_on"] and first != me["key"] and float(r["age"]) < int(r["rf_secs"]):
                continue
            ringing.append(dict(_card(r), first=bool(first and first == me["key"])))
        else:
            taken.append(_card(r))
    return jsonify({"enabled": True, "me": me["label"], "ringing": ringing, "mine": mine, "taken": taken})


@calls_bp.route("/calls/<int:call_id>/claim", methods=["POST"])
@team_feature("calls.answer")
def calls_claim(call_id: int):
    """First to press Answer gets the call (one UPDATE, so two people can
    never both take it). Returns Meta's SDP offer for the browser."""
    if not _team_member_has_permission("calls.answer"):
        return jsonify({"error": "Your role doesn't allow this."}), 403
    if _require_login():
        return jsonify({"error": "login"}), 401
    customer, tenant_id, me = _ctx()
    if not _may_answer(tenant_id):
        return jsonify({"error": "Your role can't answer calls."}), 403
    conn, cur = _db()
    try:
        cur.execute("SELECT customer_phone FROM wa_calls WHERE id=%s AND tenant_id=%s", (call_id, tenant_id))
        row = cur.fetchone()
        if not row or not _sees(tenant_id, row["customer_phone"]):
            return jsonify({"error": "Call not found."}), 404
        cur.execute("""UPDATE wa_calls SET status='answering', answered_by_key=%s, answered_by_name=%s
                        WHERE id=%s AND tenant_id=%s AND status='ringing' RETURNING offer_sdp""",
                    (me["key"], me["label"], call_id, tenant_id))
        got = cur.fetchone()
        conn.commit()
        if not got:
            cur.execute("SELECT status, answered_by_name FROM wa_calls WHERE id=%s", (call_id,))
            cur_row = cur.fetchone() or {}
            who = cur_row.get("answered_by_name")
            return jsonify({"error": f"Answered by {who}." if who else "This call has ended."}), 409
    finally:
        cur.close(); conn.close()
    return jsonify({"ok": True, "offer": got["offer_sdp"]})


@calls_bp.route("/calls/<int:call_id>/accept", methods=["POST"])
@team_feature("calls.answer")
def calls_accept(call_id: int):
    """The browser's SDP answer → Meta pre_accept then accept."""
    if not _team_member_has_permission("calls.answer"):
        return jsonify({"error": "Your role doesn't allow this."}), 403
    if _require_login():
        return jsonify({"error": "login"}), 401
    customer, tenant_id, me = _ctx()
    sdp = (request.get_json(silent=True) or {}).get("sdp") or ""
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM wa_calls WHERE id=%s AND tenant_id=%s AND answered_by_key=%s AND status='answering'",
                    (call_id, tenant_id, me["key"]))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "This call has ended."}), 409
        tok = _token(cur, row["phone_number_id"], tenant_id)
        body = {"messaging_product": "whatsapp", "call_id": row["wa_call_id"], "session": {"sdp_type": "answer", "sdp": sdp}}
        ok, data = _graph(row["phone_number_id"], tok["access_token"], dict(body, action="pre_accept"))
        if ok:
            ok, data = _graph(row["phone_number_id"], tok["access_token"], dict(body, action="accept"))
        if ok:
            cur.execute("UPDATE wa_calls SET status='answered', answered_at=NOW(), answer_sdp=%s WHERE id=%s", (sdp, call_id))
        else:
            cur.execute("UPDATE wa_calls SET status='failed', error=%s, ended_at=NOW() WHERE id=%s", (_meta_error(data), call_id))
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not ok:
        return jsonify({"error": "Meta didn't connect the call: " + _meta_error(data)}), 502
    return jsonify({"ok": True})


@calls_bp.route("/calls/<int:call_id>/release", methods=["POST"])
@team_feature("calls.answer")
def calls_release(call_id: int):
    """Couldn't answer after all (microphone blocked): ring the others again."""
    if not _team_member_has_permission("calls.answer"):
        return jsonify({"error": "Your role doesn't allow this."}), 403
    if _require_login():
        return jsonify({"error": "login"}), 401
    customer, tenant_id, me = _ctx()
    conn, cur = _db()
    try:
        cur.execute("""UPDATE wa_calls SET status='ringing', answered_by_key=NULL, answered_by_name=NULL
                        WHERE id=%s AND tenant_id=%s AND answered_by_key=%s AND status='answering'""",
                    (call_id, tenant_id, me["key"]))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return jsonify({"ok": True})


@calls_bp.route("/calls/<int:call_id>/end", methods=["POST"])
@team_feature("calls.answer", "calls.make")
def calls_end(call_id: int):
    """Hang up. Meta's terminate webhook then fills in the real duration."""
    if not (_team_member_has_permission("calls.answer") or _team_member_has_permission("calls.make")):
        return jsonify({"error": "Your role doesn't allow this."}), 403
    if _require_login():
        return jsonify({"error": "login"}), 401
    customer, tenant_id, me = _ctx()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM wa_calls WHERE id=%s AND tenant_id=%s AND (answered_by_key=%s OR started_by_key=%s)",
                    (call_id, tenant_id, me["key"], me["key"]))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Call not found."}), 404
        tok = _token(cur, row["phone_number_id"], tenant_id)
        if row["wa_call_id"] and tok:
            _graph(row["phone_number_id"], tok["access_token"],
                   {"messaging_product": "whatsapp", "call_id": row["wa_call_id"], "action": "terminate"})
        cur.execute("""UPDATE wa_calls SET status = CASE WHEN answered_at IS NOT NULL THEN 'completed'
                                                        WHEN direction='out' THEN 'missed' ELSE status END,
                              ended_at = COALESCE(ended_at, NOW()),
                              duration = COALESCE(duration, CASE WHEN answered_at IS NOT NULL
                                         THEN EXTRACT(EPOCH FROM NOW() - answered_at)::int END)
                        WHERE id=%s AND status IN ('answering','answered','calling')""", (call_id,))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return jsonify({"ok": True})


@calls_bp.route("/calls/<int:call_id>/callback", methods=["POST"])
@team_feature("voice.calls")
def calls_callback(call_id: int):
    """"I'll call back": one person claims a missed call."""
    r = _require_login()
    if r: return r
    r = _require_team_permission("voice.calls")
    if r: return r
    customer, tenant_id, me = _ctx()
    conn, cur = _db()
    try:
        cur.execute("""UPDATE wa_calls SET callback_by_key=%s, callback_by_name=%s, callback_at=NOW()
                        WHERE id=%s AND tenant_id=%s AND status IN ('missed','out_of_hours') AND callback_by_key IS NULL
                        RETURNING customer_phone""", (me["key"], me["label"], call_id, tenant_id))
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()
    if request.args.get("json"):
        return jsonify({"ok": bool(row)})
    flash("It's yours. Call them back from the chat." if row else "Someone has already taken that one.", "success" if row else "warning")
    return redirect(request.referrer or url_for("calls.calls_page"))


@calls_bp.route("/calls/<int:call_id>/add-lead", methods=["POST"])
@team_feature("leads.create")
def calls_add_lead(call_id: int):
    """"Add as lead" for a caller who isn't in Leads yet (from the ringing
    pop-up, the call, or the Call Log). Their earlier calls are linked to the
    new lead and put in its call history."""
    if not _team_member_has_permission("leads.create"):
        return jsonify({"error": "Your role can't add leads."}), 403
    if _require_login():
        return jsonify({"error": "login"}), 401
    customer, tenant_id, me = _ctx()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM wa_calls WHERE id=%s AND tenant_id=%s", (call_id, tenant_id))
        call = cur.fetchone()
        if not call or not _sees(tenant_id, call["customer_phone"]):
            return jsonify({"error": "Call not found."}), 404
        phone = call["customer_phone"]
        cur.execute("""SELECT id FROM merchant_pipeline_leads WHERE tenant_id=%s
                         AND (regexp_replace(COALESCE(whatsapp_number,''),'\D','','g')=%s
                           OR regexp_replace(COALESCE(phone,''),'\D','','g')=%s) ORDER BY id DESC LIMIT 1""",
                    (tenant_id, phone, phone))
        found = cur.fetchone()
        if found:
            lead_id, made = found["id"], False
        else:
            cur.execute("SELECT * FROM wa_contacts WHERE tenant_id=%s AND regexp_replace(COALESCE(whatsapp_number,''),'\D','','g')=%s LIMIT 1",
                        (tenant_id, phone))
            contact = cur.fetchone() or {}
            name = (request.get_json(silent=True) or {}).get("name") or call["customer_name"] or contact.get("display_name") or ("+" + phone)
            cur.execute("""INSERT INTO merchant_pipeline_leads
                               (tenant_id, customer_name, phone, whatsapp_number, email, stage, contact_channel,
                                wa_contact_id, company_id, source, assigned_key, assigned_to, notes)
                           VALUES (%s,%s,%s,%s,%s,'new_lead','whatsapp',%s,%s,'whatsapp',%s,%s,%s) RETURNING id""",
                        (tenant_id, name[:200], phone, phone, contact.get("email"), contact.get("id"), contact.get("company_id"),
                         me["key"], me["label"], "Added from a WhatsApp call"))
            lead_id, made = cur.fetchone()["id"], True
            _link_lead_to_contact(cur, tenant_id, lead_id)
        cur.execute("""UPDATE wa_calls SET lead_id=%s WHERE tenant_id=%s AND customer_phone=%s AND lead_id IS NULL
                       RETURNING *""", (lead_id, tenant_id, phone))
        linked = cur.fetchall()
        for c in linked:
            if c["status"] in ("completed", "answered"):
                outcome = "answered"
            elif c["status"] in ("missed", "out_of_hours", "rejected"):
                outcome = "no_answer"
            else:
                continue
            who = c["answered_by_name"] or c["started_by_name"]
            length = f" · {c['duration'] // 60}:{c['duration'] % 60:02d}" if c["duration"] else ""
            note = (f"📞 WhatsApp call ({'outgoing' if c['direction'] == 'out' else 'incoming'}){length}" if outcome == "answered"
                    else "📵 Missed WhatsApp call")
            cur.execute("""INSERT INTO lead_calls (tenant_id, lead_id, actor_key, actor_name, outcome, minutes, note, created_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (tenant_id, lead_id, c["answered_by_key"] or c["started_by_key"], who or "WhatsApp", outcome,
                         -(-c["duration"] // 60) if c["duration"] else None, note, c["created_at"]))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️ calls_add_lead error:", e)
        return jsonify({"error": "Couldn't add the lead. Please try again."}), 500
    finally:
        cur.close(); conn.close()
    if made:
        try:
            from portal_routes import pipeline_record_stage_change
            pipeline_record_stage_change(lead_id, None, "new_lead", me["label"], "Added from a WhatsApp call")
        except Exception as e:
            print("⚠️ calls_add_lead stage history:", e)
    if request.is_json or request.args.get("json"):
        return jsonify({"ok": True, "lead_id": lead_id, "new": made, "url": url_for("portal.lead_detail", lead_id=lead_id)})
    flash("Lead added. It's assigned to you." if made else "They're already a lead, now linked to this call.", "success")
    return redirect(url_for("portal.lead_detail", lead_id=lead_id))


# ── Outgoing (only where Meta allows) ────────────────────────────────────────

def _outgoing_ready(cur, tenant_id: int, pnid: str):
    tok = _token(cur, pnid, tenant_id)
    if not tok:
        return None, "That WhatsApp number isn't connected."
    if not _outgoing_allowed_country(tok["display_phone_number"]):
        return None, "Meta doesn't allow calling customers from this number's country."
    cur.execute("SELECT outgoing_on FROM wa_call_settings WHERE phone_number_id=%s", (pnid,))
    st = cur.fetchone()
    if not st or not st["outgoing_on"]:
        return None, "Calling customers is switched off for this number (Calls settings)."
    return tok, None


@calls_bp.route("/calls/can-call")
@team_feature("calls.make")
def calls_can_call():
    """For the chat's 📞 Call button: may we call this person now?"""
    if not _team_member_has_permission("calls.make"):
        return jsonify({"error": "Your role doesn't allow this."}), 403
    if _require_login():
        return jsonify({"error": "login"}), 401
    customer, tenant_id, me = _ctx()
    pnid, phone = request.args.get("pnid") or "", _digits(request.args.get("phone"))
    conn, cur = _db()
    try:
        if not pnid:
            for n in _numbers(cur, tenant_id):
                if n["outgoing_on"] and _outgoing_allowed_country(n["display_phone_number"]):
                    pnid = n["phone_number_id"]; break
        if not pnid:
            return jsonify({"show": False})
        tok, why = _outgoing_ready(cur, tenant_id, pnid)
        if not tok:
            return jsonify({"show": False, "why": why})
        status = "no_permission"
        try:
            r = _req.get(f"{GRAPH}/{pnid}/call_permissions", params={"user_wa_id": phone},
                         headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=10).json()
            status = ((r.get("permission") or {}).get("status")) or status
            acts = {a.get("action_name"): a.get("can_perform_action") for a in (r.get("actions") or [])}
        except Exception:
            acts = {}
    finally:
        cur.close(); conn.close()
    return jsonify({"show": True, "pnid": pnid, "permission": status,
                    "can_call": bool(acts.get("start_call")) or status in ("temporary", "permanent"),
                    "can_ask": acts.get("send_call_permission_request", True)})


@calls_bp.route("/calls/permission", methods=["POST"])
@team_feature("calls.make")
def calls_permission():
    """Send Meta's "Can we call you?" request inside the chat."""
    if not _team_member_has_permission("calls.make"):
        return jsonify({"error": "Your role doesn't allow this."}), 403
    if _require_login():
        return jsonify({"error": "login"}), 401
    customer, tenant_id, me = _ctx()
    d = request.get_json(silent=True) or {}
    pnid, phone = d.get("pnid") or "", _digits(d.get("phone"))
    conn, cur = _db()
    try:
        tok, why = _outgoing_ready(cur, tenant_id, pnid)
        if not tok:
            return jsonify({"error": why}), 400
        ok, data = _graph(pnid, tok["access_token"], {
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": phone, "type": "interactive",
            "interactive": {"type": "call_permission_request", "action": {"name": "call_permission_request"},
                            "body": {"text": (d.get("text") or "We'd like to call you on WhatsApp about your enquiry. Is that OK?")[:1000]}}},
            path="messages")
        if ok:
            cur.execute("""INSERT INTO wa_call_permissions (phone_number_id, customer_phone, tenant_id, status, requested_by)
                           VALUES (%s,%s,%s,'requested',%s)
                           ON CONFLICT (phone_number_id, customer_phone) DO UPDATE SET status='requested',
                               requested_by=EXCLUDED.requested_by, updated_at=NOW()""", (pnid, phone, tenant_id, me["label"]))
            conn.commit()
    finally:
        cur.close(); conn.close()
    if not ok:
        return jsonify({"error": _meta_error(data)}), 502
    return jsonify({"ok": True})


@calls_bp.route("/calls/start", methods=["POST"])
@team_feature("calls.make")
def calls_start():
    """Start an outgoing call with the browser's SDP offer."""
    if not _team_member_has_permission("calls.make"):
        return jsonify({"error": "Your role doesn't allow this."}), 403
    if _require_login():
        return jsonify({"error": "login"}), 401
    customer, tenant_id, me = _ctx()
    if not _plan_grants_feature(_get_tenant_plan(tenant_id), "calls.make"):
        return jsonify({"error": "Calling customers isn't in your plan."}), 403
    d = request.get_json(silent=True) or {}
    pnid, phone, sdp = d.get("pnid") or "", _digits(d.get("phone")), d.get("sdp") or ""
    if not _sees(tenant_id, phone):
        return jsonify({"error": "You can't see this chat."}), 403
    conn, cur = _db()
    try:
        tok, why = _outgoing_ready(cur, tenant_id, pnid)
        if not tok:
            return jsonify({"error": why}), 400
        ok, data = _graph(pnid, tok["access_token"], {"messaging_product": "whatsapp", "to": phone, "action": "connect",
                                                     "session": {"sdp_type": "offer", "sdp": sdp}})
        if not ok:
            code = ((data or {}).get("error") or {}).get("code")
            msg = "They haven't agreed to calls yet. Send \"Can we call you?\" first." if code == 138006 else _meta_error(data)
            return jsonify({"error": msg}), 400
        wa_id = ((data.get("calls") or [{}])[0]).get("id")
        cur.execute("""SELECT id, COALESCE(NULLIF(contact_person,''), customer_name) AS name FROM merchant_pipeline_leads
                        WHERE tenant_id=%s AND (regexp_replace(COALESCE(whatsapp_number,''),'\\D','','g')=%s
                                             OR regexp_replace(COALESCE(phone,''),'\\D','','g')=%s) ORDER BY id DESC LIMIT 1""",
                    (tenant_id, phone, phone))
        lead = cur.fetchone() or {}
        cur.execute("""INSERT INTO wa_calls (tenant_id, phone_number_id, wa_call_id, direction, customer_phone, customer_name,
                                              lead_id, status, offer_sdp, started_by_key, started_by_name)
                       VALUES (%s,%s,%s,'out',%s,%s,%s,'calling',%s,%s,%s)
                       ON CONFLICT (wa_call_id) DO UPDATE SET started_by_key=EXCLUDED.started_by_key,
                           started_by_name=EXCLUDED.started_by_name, offer_sdp=EXCLUDED.offer_sdp,
                           customer_name=EXCLUDED.customer_name, lead_id=EXCLUDED.lead_id
                       RETURNING id""",
                    (tenant_id, pnid, wa_id, phone, lead.get("name"), lead.get("id"), sdp, me["key"], me["label"]))
        new_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        cur.close(); conn.close()
    return jsonify({"ok": True, "id": new_id})


# ── Calls page ───────────────────────────────────────────────────────────────

_SHOW = {
    "all": "TRUE",
    "missed": "c.status IN ('missed','out_of_hours') AND c.callback_by_key IS NULL",
    "answered": "c.status IN ('answered','completed') AND c.direction='in'",
    "outgoing": "c.direction='out'",
}


@calls_bp.route("/calls")
@team_feature("voice.calls")
def calls_page():
    r = _require_login()
    if r: return r
    r = _require_team_permission("voice.calls")
    if r: return r
    customer, tenant_id, me = _ctx()
    r = _require_plan_sub_feature(customer, "voice.calls", "WhatsApp calls")
    if r: return r
    show = request.args.get("show") if request.args.get("show") in (*_SHOW, "mine") else "all"
    page = max(1, int(request.args.get("page") or 1)) if str(request.args.get("page") or "1").isdigit() else 1
    where = _SHOW.get(show, "TRUE")
    params = [tenant_id]
    if show == "mine":
        where = "(c.answered_by_key=%s OR c.started_by_key=%s OR c.callback_by_key=%s)"
        params += [me["key"]] * 3
    conn, cur = _db()
    try:
        cur.execute("""SELECT count(*) FILTER (WHERE created_at::date = CURRENT_DATE) AS today,
                              count(*) FILTER (WHERE created_at::date = CURRENT_DATE AND status IN ('answered','completed') AND direction='in') AS answered,
                              count(*) FILTER (WHERE created_at::date = CURRENT_DATE AND status IN ('missed','out_of_hours') AND direction='in') AS missed,
                              count(*) FILTER (WHERE status IN ('missed','out_of_hours') AND callback_by_key IS NULL AND direction='in') AS need_cb,
                              count(*) FILTER (WHERE status IN ('missed','out_of_hours') AND callback_by_key IS NULL AND direction='in'
                                               AND callback_due_at < NOW()) AS overdue,
                              ROUND(AVG(duration) FILTER (WHERE duration > 0 AND created_at > NOW() - interval '30 days')) AS avg_talk
                         FROM wa_calls WHERE tenant_id=%s""", (tenant_id,))
        k = cur.fetchone()
        cur.execute(f"""SELECT c.*, wt.display_phone_number FROM wa_calls c
                          LEFT JOIN wa_tenants wt ON wt.phone_number_id = c.phone_number_id
                         WHERE c.tenant_id=%s AND {where} AND c.status <> 'ringing'
                         ORDER BY c.created_at DESC LIMIT 51 OFFSET %s""", params + [(page - 1) * 50])
        now = datetime.now(timezone.utc)
        rows = cur.fetchall()
        numbers = _numbers(cur, tenant_id)
    finally:
        cur.close(); conn.close()
    rows = [dict(x, due_mins=int((x["callback_due_at"] - now).total_seconds() // 60) if x.get("callback_due_at") else None)
            for x in rows if _sees(tenant_id, x["customer_phone"])]
    has_more = len(rows) > 50
    return render_template("portal/calls.html", rows=rows[:50], k=k, show=show, page=page, has_more=has_more,
                           numbers=numbers, any_on=any(n["incoming_on"] for n in numbers),
                           can_settings=_team_member_has_permission("calls.settings"))


# ── Calls report ─────────────────────────────────────────────────────────────

def _report_range():
    days = request.args.get("days") or "30"
    today = datetime.now(timezone.utc).date()
    if days == "custom":
        try:
            start = datetime.strptime(request.args.get("from"), "%Y-%m-%d").date()
            end = datetime.strptime(request.args.get("to"), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            start, end, days = today - timedelta(days=29), today, "30"
    else:
        n = int(days) if days.isdigit() and int(days) in (7, 30, 90) else 30
        days, start, end = str(n), today - timedelta(days=n - 1), today
    return days, start, end


def _report_data(tenant_id: int, start, end, pnid: str, member: str):
    span = (end - start).days + 1
    prev_start = start - timedelta(days=span)
    conn, cur = _db()
    try:
        w, p = ["c.tenant_id=%s", "c.created_at::date BETWEEN %s AND %s", "c.status <> 'ringing'"], [tenant_id, start, end]
        if pnid:
            w.append("c.phone_number_id=%s"); p.append(pnid)
        if member:
            w.append("(c.answered_by_key=%s OR c.started_by_key=%s OR c.callback_by_key=%s)"); p += [member] * 3
        W = " AND ".join(w)
        cur.execute(f"""SELECT count(*) FILTER (WHERE direction='in') AS calls_in,
                               count(*) FILTER (WHERE direction='in' AND status IN ('answered','completed')) AS answered,
                               count(*) FILTER (WHERE direction='in' AND status IN ('missed','out_of_hours')) AS missed,
                               count(*) FILTER (WHERE direction='in' AND status IN ('missed','out_of_hours') AND callback_by_key IS NOT NULL) AS called_back,
                               count(*) FILTER (WHERE direction='in' AND callback_due_at IS NOT NULL AND callback_at IS NOT NULL
                                                AND callback_at <= callback_due_at) AS on_time,
                               count(*) FILTER (WHERE direction='in' AND callback_due_at IS NOT NULL
                                                AND (callback_at > callback_due_at OR (callback_at IS NULL AND callback_due_at < NOW()))) AS late,
                               count(*) FILTER (WHERE direction='out') AS outgoing,
                               ROUND(AVG(EXTRACT(EPOCH FROM answered_at - created_at)) FILTER (WHERE direction='in' AND answered_at IS NOT NULL)) AS to_answer,
                               ROUND(AVG(EXTRACT(EPOCH FROM callback_at - created_at)) FILTER (WHERE callback_at IS NOT NULL)) AS to_callback,
                               ROUND(AVG(duration) FILTER (WHERE duration > 0)) AS talk
                          FROM wa_calls c WHERE {W}""", p)
        k = cur.fetchone()
        p2 = [tenant_id, prev_start, start - timedelta(days=1)] + p[3:]
        cur.execute(f"SELECT count(*) FILTER (WHERE direction='in') AS n FROM wa_calls c WHERE {W}", p2)
        prev_in = cur.fetchone()["n"]
        cur.execute(f"""SELECT COALESCE(answered_by_name, started_by_name) AS who,
                               count(*) FILTER (WHERE direction='in' AND answered_at IS NOT NULL) AS answered,
                               ROUND(AVG(duration) FILTER (WHERE duration > 0)) AS talk,
                               count(*) FILTER (WHERE direction='out') AS outgoing
                          FROM wa_calls c WHERE {W} AND COALESCE(answered_by_name, started_by_name) IS NOT NULL
                         GROUP BY 1 ORDER BY 2 DESC""", p)
        people = {r["who"]: dict(r, callbacks=0) for r in cur.fetchall()}
        cur.execute(f"""SELECT callback_by_name AS who, count(*) AS n FROM wa_calls c WHERE {W} AND callback_by_name IS NOT NULL GROUP BY 1""", p)
        for r in cur.fetchall():
            people.setdefault(r["who"], {"who": r["who"], "answered": 0, "talk": None, "outgoing": 0})["callbacks"] = r["n"]
        cur.execute(f"""SELECT COALESCE(missed_reason,'no_answer') AS why, count(*) AS n FROM wa_calls c
                         WHERE {W} AND direction='in' AND status IN ('missed','out_of_hours') GROUP BY 1 ORDER BY 2 DESC""", p)
        reasons = cur.fetchall()
        cur.execute(f"""SELECT EXTRACT(ISODOW FROM c.created_at AT TIME ZONE 'Africa/Lagos')::int AS dow,
                               EXTRACT(HOUR FROM c.created_at AT TIME ZONE 'Africa/Lagos')::int AS hr, count(*) AS n
                          FROM wa_calls c WHERE {W} AND direction='in' GROUP BY 1,2""", p)
        heat = {(r["dow"], r["hr"]): r["n"] for r in cur.fetchall()}
        # What happened after: callers matched to leads, by when the lead was
        # made / became a deal / booked a demo / was won, after their first call.
        cur.execute(f"""WITH f AS (SELECT c.tenant_id, c.customer_phone, MIN(c.created_at) AS first_call
                                     FROM wa_calls c WHERE {W} AND direction='in' GROUP BY 1,2),
                             l AS (SELECT f.*, ld.id, ld.created_at, ld.is_opportunity, ld.stage, ld.outcome
                                     FROM f JOIN merchant_pipeline_leads ld ON ld.tenant_id=f.tenant_id
                                      AND (regexp_replace(COALESCE(ld.whatsapp_number,''),'\\D','','g')=f.customer_phone
                                        OR regexp_replace(COALESCE(ld.phone,''),'\\D','','g')=f.customer_phone))
                        SELECT count(DISTINCT customer_phone) AS callers,
                               count(DISTINCT customer_phone) FILTER (WHERE created_at >= first_call - interval '1 hour') AS new_leads,
                               count(DISTINCT customer_phone) FILTER (WHERE is_opportunity) AS deals,
                               count(DISTINCT customer_phone) FILTER (WHERE stage='won' OR outcome='won') AS won
                          FROM l""", p)
        after = cur.fetchone()
        cur.execute(f"SELECT count(DISTINCT customer_phone) AS n FROM wa_calls c WHERE {W} AND direction='in'", p)
        after["all_callers"] = cur.fetchone()["n"]
        try:
            cur.execute(f"""SELECT count(DISTINCT c.customer_phone) AS n FROM wa_calls c
                              JOIN merchant_pipeline_leads ld ON ld.tenant_id=c.tenant_id
                               AND (regexp_replace(COALESCE(ld.whatsapp_number,''),'\\D','','g')=c.customer_phone
                                 OR regexp_replace(COALESCE(ld.phone,''),'\\D','','g')=c.customer_phone)
                              JOIN booking_appointments b ON b.lead_id=ld.id AND COALESCE(b.booked_at, b.first_seen_at) >= c.created_at
                             WHERE {W} AND c.direction='in'""", p)
            after["demos"] = cur.fetchone()["n"]
        except Exception:
            conn.rollback(); after["demos"] = None
    finally:
        cur.close(); conn.close()
    return {"k": k, "prev_in": prev_in, "people": sorted(people.values(), key=lambda x: -(x["answered"] or 0)),
            "reasons": reasons, "heat": heat, "after": after}


@calls_bp.route("/reports/calls")
@team_feature("calls.report")
def calls_report():
    r = _require_login()
    if r: return r
    r = _require_team_permission("calls.report")
    if r: return r
    customer, tenant_id, me = _ctx()
    r = _require_plan_sub_feature(customer, "calls.report", "Calls report")
    if r: return r
    days, start, end = _report_range()
    pnid, member = request.args.get("pnid") or "", request.args.get("member") or ""
    data = _report_data(tenant_id, start, end, pnid, member)
    conn, cur = _db()
    try:
        numbers = _numbers(cur, tenant_id)
        cur.execute("""SELECT DISTINCT COALESCE(answered_by_key, started_by_key) AS k, COALESCE(answered_by_name, started_by_name) AS n
                         FROM wa_calls WHERE tenant_id=%s AND COALESCE(answered_by_key, started_by_key) IS NOT NULL ORDER BY 2""", (tenant_id,))
        members = cur.fetchall()
    finally:
        cur.close(); conn.close()
    return render_template("portal/report_calls.html", d=data, days=days, start=start, end=end, pnid=pnid, member=member,
                           numbers=numbers, members=members)


@calls_bp.route("/reports/calls/export")
@team_feature("calls.report")
def calls_report_export():
    r = _require_login()
    if r: return r
    r = _require_team_permission("calls.report")
    if r: return r
    customer, tenant_id, me = _ctx()
    r = _require_plan_sub_feature(customer, "calls.report", "Calls report")
    if r: return r
    days, start, end = _report_range()
    from openpyxl import Workbook
    conn, cur = _db()
    try:
        w, p = ["tenant_id=%s", "created_at::date BETWEEN %s AND %s", "status <> 'ringing'"], [tenant_id, start, end]
        if request.args.get("pnid"):
            w.append("phone_number_id=%s"); p.append(request.args["pnid"])
        cur.execute(f"""SELECT created_at, direction, customer_name, customer_phone, status, missed_reason,
                               COALESCE(answered_by_name, started_by_name) AS handled_by, duration, callback_by_name, callback_at
                          FROM wa_calls WHERE {' AND '.join(w)} ORDER BY created_at""", p)
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    wb = Workbook(); ws = wb.active; ws.title = "Calls"
    ws.append(["Date and time (UTC)", "Direction", "Name", "Phone", "Result", "Why missed", "Handled by",
               "Length (seconds)", "Called back by", "Called back at (UTC)"])
    for x in rows:
        if not _sees(tenant_id, x["customer_phone"]):
            continue
        ws.append([x["created_at"].replace(tzinfo=None), "Incoming" if x["direction"] == "in" else "Outgoing",
                   x["customer_name"], "+" + x["customer_phone"], x["status"], x["missed_reason"], x["handled_by"],
                   x["duration"], x["callback_by_name"], x["callback_at"].replace(tzinfo=None) if x["callback_at"] else None])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"calls-{start}-to-{end}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── Settings for one number ──────────────────────────────────────────────────

def _meta_number_info(pnid: str, token: str) -> dict:
    """Messaging limit, phone-app (coexistence) and Meta's calling settings."""
    info = {}
    try:
        r = _req.get(f"{GRAPH}/{pnid}", params={"fields": "messaging_limit_tier,is_on_biz_app,platform_type"},
                     headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        if r.get("error"):
            r = _req.get(f"{GRAPH}/{pnid}", params={"fields": "messaging_limit_tier,platform_type"},
                         headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        info.update(tier=r.get("messaging_limit_tier"), on_app=r.get("is_on_biz_app"), platform=r.get("platform_type"))
        s = _req.get(f"{GRAPH}/{pnid}/settings", headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        info["calling"] = s.get("calling") or {}
        if s.get("error"):
            info["settings_error"] = _meta_error(s)
    except Exception as e:
        info["settings_error"] = str(e)
    return info


_TIER_OK = ("TIER_2K", "TIER_10K", "TIER_100K", "TIER_UNLIMITED")


@calls_bp.route("/calls/settings")
@team_feature("calls.settings")
def calls_settings_index():
    """Calls › Call Settings: the first WhatsApp number's settings (the page
    has a switcher for the others)."""
    r = _require_login()
    if r: return r
    r = _require_team_permission("calls.settings")
    if r: return r
    customer, tenant_id, me = _ctx()
    conn, cur = _db()
    try:
        nums = _numbers(cur, tenant_id)
    finally:
        cur.close(); conn.close()
    if not nums:
        flash("Connect a WhatsApp number first (Integration › Channels), then switch calls on here.", "warning")
        return redirect(url_for("calls.calls_page"))
    return redirect(url_for("calls.calls_settings", pnid=nums[0]["phone_number_id"]))


@calls_bp.route("/calls/settings/<pnid>", methods=["GET", "POST"])
@team_feature("calls.settings")
def calls_settings(pnid: str):
    r = _require_login()
    if r: return r
    r = _require_team_permission("calls.settings")
    if r: return r
    customer, tenant_id, me = _ctx()
    r = _require_plan_sub_feature(customer, "voice.calls", "WhatsApp calls")
    if r: return r
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM wa_tenants WHERE phone_number_id=%s AND tenant_id=%s AND active", (pnid, tenant_id))
        num = cur.fetchone()
        if not num:
            flash("That WhatsApp number isn't connected.", "warning")
            return redirect(url_for("calls.calls_page"))
        cur.execute("SELECT * FROM wa_call_settings WHERE phone_number_id=%s", (pnid,))
        st = cur.fetchone() or {}
        out_country = _outgoing_allowed_country(num["display_phone_number"])
        if request.method == "POST":
            f = request.form
            days = sorted({int(d) for d in f.getlist("days") if d.isdigit() and 0 <= int(d) <= 6}) or [1, 2, 3, 4, 5]
            hfrom, hto = (f.get("from") or "0900").replace(":", ""), (f.get("to") or "1700").replace(":", "")
            if not (hfrom.isdigit() and hto.isdigit() and hfrom < hto):
                flash("The calling hours must end after they start.", "warning")
                return redirect(url_for("calls.calls_settings", pnid=pnid))
            tz = f.get("tz") or "Africa/Lagos"
            new = {"incoming_on": f.get("incoming_on") == "1", "outgoing_on": f.get("outgoing_on") == "1" and out_country,
                   "ring_first": f.get("ring_first") == "1",
                   "ring_first_secs": min(30, max(5, int(f.get("ring_first_secs") or 15))) if (f.get("ring_first_secs") or "15").isdigit() else 15,
                   "hours_on": f.get("hours_on") == "1", "hours": {"days": days, "from": hfrom, "to": hto, "tz": tz},
                   "missed_message": (f.get("missed_message") or "").strip()[:1000] or DEFAULT_MISSED,
                   "callback_on": f.get("callback_on") == "1",
                   "callback_mins": int(f.get("callback_mins")) if (f.get("callback_mins") or "") in ("15", "30", "60", "120") else 30}
            calling = {"status": "ENABLED" if new["incoming_on"] or new["outgoing_on"] else "DISABLED",
                       "call_icon_visibility": "DEFAULT" if new["incoming_on"] else "DISABLE_ALL",
                       "callback_permission_status": "ENABLED" if new["outgoing_on"] else "DISABLED"}
            if new["hours_on"]:
                calling["call_hours"] = {"status": "ENABLED", "timezone_id": tz,
                                         "weekly_operating_hours": [{"day_of_week": META_DAYS[d], "open_time": hfrom,
                                                                     "close_time": "2359" if hto == "2400" else hto} for d in days]}
            else:
                calling["call_hours"] = {"status": "DISABLED", "timezone_id": tz,
                                         "weekly_operating_hours": [{"day_of_week": "MONDAY", "open_time": "0000", "close_time": "2359"}]}
            ok, data = _graph(pnid, num["access_token"], {"calling": calling}, path="settings")
            meta_status = "saved" if ok else _meta_error(data)
            if not ok and (new["incoming_on"] or new["outgoing_on"]):
                new["incoming_on"] = new["outgoing_on"] = False
            cur.execute("""INSERT INTO wa_call_settings (phone_number_id, tenant_id, incoming_on, outgoing_on, ring_first,
                                   ring_first_secs, hours_on, hours, missed_message, meta_status, updated_at, updated_by,
                                   callback_on, callback_mins)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s)
                           ON CONFLICT (phone_number_id) DO UPDATE SET incoming_on=EXCLUDED.incoming_on,
                               outgoing_on=EXCLUDED.outgoing_on, ring_first=EXCLUDED.ring_first,
                               ring_first_secs=EXCLUDED.ring_first_secs, hours_on=EXCLUDED.hours_on, hours=EXCLUDED.hours,
                               missed_message=EXCLUDED.missed_message, meta_status=EXCLUDED.meta_status,
                               updated_at=NOW(), updated_by=EXCLUDED.updated_by,
                               callback_on=EXCLUDED.callback_on, callback_mins=EXCLUDED.callback_mins""",
                        (pnid, tenant_id, new["incoming_on"], new["outgoing_on"], new["ring_first"], new["ring_first_secs"],
                         new["hours_on"], psycopg2.extras.Json(new["hours"]), new["missed_message"], meta_status, me["label"],
                         new["callback_on"], new["callback_mins"]))
            conn.commit()
            if ok:
                flash("Call settings saved, and Meta has the new settings.", "success")
            else:
                flash("Saved here, but Meta refused the calling settings: " + meta_status
                      + " Calls stay switched off until this is fixed.", "warning")
            return redirect(url_for("calls.calls_settings", pnid=pnid))
    finally:
        cur.close(); conn.close()
    info = _meta_number_info(pnid, num["access_token"])
    conn, cur = _db()
    try:
        all_numbers = _numbers(cur, tenant_id)
    finally:
        cur.close(); conn.close()
    hours = st.get("hours") or {"days": [1, 2, 3, 4, 5], "from": "0900", "to": "1700", "tz": "Africa/Lagos"}
    if isinstance(hours, str):
        hours = json.loads(hours)
    checks = [
        ("on_app", info.get("on_app") is not True,
         "Fully on PhiXtra (not \"keep using the phone app\")",
         "This number also uses the WhatsApp Business phone app. Meta doesn't allow calls through the portal on these numbers; calls keep ringing on the phone."),
        ("tier", (info.get("tier") in _TIER_OK) if info.get("tier") else None,
         "Meta messaging limit 2,000 a day or more",
         f"Meta's messaging limit for this number is {(info.get('tier') or 'unknown').replace('TIER_', '')} a day. Calling needs 2,000 or more; it goes up as the number sends more messages with good quality."),
        ("out", out_country, "Team can call customers from this number",
         "Meta doesn't allow calling customers from business numbers in Nigeria (+234), the US and Canada (+1), Egypt (+20) or Vietnam (+84). Customers can still call you."),
    ]
    return render_template("portal/call_settings.html", num=num, st=st, hours=hours, info=info, checks=checks,
                           all_numbers=all_numbers,
                           out_country=out_country, days=DAYS, default_missed=DEFAULT_MISSED,
                           tz_choices=[("Africa/Lagos", "Nigeria (Lagos)"), ("Europe/London", "UK (London)"),
                                       ("Africa/Accra", "Ghana (Accra)"), ("Africa/Nairobi", "Kenya (Nairobi)"),
                                       ("Africa/Johannesburg", "South Africa"), ("Asia/Dubai", "UAE (Dubai)")])


# ── Helpers for other pages ──────────────────────────────────────────────────

def contact_calls(tenant_id: int, numbers) -> list:
    """WhatsApp calls with these phone numbers, for a contact's history."""
    ds = [d for d in (_digits(n) for n in numbers) if len(d) >= 7]
    if not ds:
        return []
    conn, cur = _db()
    try:
        cur.execute("""SELECT created_at, direction, status, duration, answered_by_name, started_by_name, missed_reason
                         FROM wa_calls WHERE tenant_id=%s AND customer_phone = ANY(%s) AND status <> 'ringing'
                        ORDER BY created_at DESC LIMIT 50""", (tenant_id, ds))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def channels_card(tenant_id: int) -> list:
    """WhatsApp numbers with their calling switch, for the Channels page."""
    conn, cur = _db()
    try:
        return _numbers(cur, tenant_id)
    finally:
        cur.close(); conn.close()
