"""
bookings_routes.py — Integration › Microsoft Bookings (2026-10-18, approved
mockup https://claude.ai/artifact/5kpdVX9PnTUz8zWU8rpEiu). A business connects
its Bookings page once; demos people book there show on their lead by
themselves. SaaS: each business its own Bookings page; PhiXtra first.

  GET  /integrations/microsoft-bookings              the page
  GET  /integrations/microsoft-bookings/connect      off to Microsoft sign-in
       (Microsoft returns to mailbox_routes.callback, the one address
        registered with Microsoft, which hands over to finish_connect)
  POST /integrations/microsoft-bookings/choose       pick the Bookings page
  POST /integrations/microsoft-bookings/check-now
  POST /integrations/microsoft-bookings/disconnect
  POST /leads/<lead_id>/demo/<appt_id>/mark          Demo done / They didn't show up

A background loop checks each connected Bookings page every 5 minutes, from
2 days ago to 120 days ahead:
  new booking            → matched to the lead by email, then by phone; else a
                           new lead (source "Demo booking", unassigned). The
                           lead's running email sequences stop; a New lead
                           moves to Contacted.
  same booking, new time → "Demo moved" on the lead.
  booking gone           → "Demo cancelled" (only when the whole list was read).
PhiXtra only reads Bookings: it never books, changes or cancels anything.
Microsoft access: outlook_mail.py (BOOKINGS_SCOPES). CRM display: bookings_crm.py.
"""
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2.extras
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

import bookings_crm
import outlook_mail as M
from db import get_db_connection
from feature_access import team_feature
from portal_routes import (
    _current_actor, _customer_id, _decrypt_key, _email_lead_contacted, _encrypt_key, _get_customer,
    _get_tenant_plan, _inject_granted_features, _link_lead_to_contact, _phone_entry, _plan_grants_feature,
    _require_login, _require_plan_sub_feature, _require_team_permission, _seq_stop_matching,
    pipeline_record_stage_change,
)

KEY = "crm.bookings_connect"
CHECK_EVERY = timedelta(minutes=5)
LOOK_BACK = timedelta(days=2)
LOOK_AHEAD = timedelta(days=120)
_FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "hotmail.com", "hotmail.co.uk",
              "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com"}

bookings_bp = Blueprint("bookings", __name__)
bookings_bp.context_processor(_inject_granted_features)


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _gate(customer):
    return _require_plan_sub_feature(customer, KEY, "Microsoft Bookings") or _require_team_permission(KEY)


def connection_for(tenant_id: int):
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM booking_connections WHERE tenant_id=%s", (tenant_id,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def card_summary(customer) -> dict:
    """For the Channels card."""
    bc = connection_for(int(customer["tenant_id"]))
    return {"conn": bc if bc and bc["status"] != "disconnected" else None}


# ── pages ──────────────────────────────────────────────────────────────────

@bookings_bp.route("/integrations/microsoft-bookings", methods=["GET"])
@team_feature(KEY)
def page():
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _gate(customer)
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    bc = connection_for(tenant_id)
    if bc and bc["status"] == "disconnected":
        bc = None
    upcoming, stats = [], {}
    conn, cur = _db()
    try:
        tz = bookings_crm.tenant_tz(cur, tenant_id)
        cur.execute("""SELECT a.*, l.customer_name AS lead_name, l.assigned_to FROM booking_appointments a
                       LEFT JOIN merchant_pipeline_leads l ON l.id = a.lead_id
                       WHERE a.tenant_id=%s AND (a.start_at >= NOW() - INTERVAL '1 day'
                             OR (a.status='cancelled' AND a.cancelled_at >= NOW() - INTERVAL '7 days'))
                       ORDER BY a.start_at ASC LIMIT 200""", (tenant_id,))
        for a in cur.fetchall():
            a["when"] = bookings_crm.when(a["start_at"], tz)
            a["was"] = bookings_crm.when(a["moved_from"], tz) if a.get("moved_from") else ""
            upcoming.append(a)
        cur.execute("""SELECT count(*) FILTER (WHERE status='booked' AND start_at > NOW()) AS upcoming,
                              count(*) FILTER (WHERE status='booked' AND start_at > NOW() AND NOT created_lead) AS matched,
                              count(*) FILTER (WHERE status='booked' AND start_at > NOW() AND created_lead) AS created,
                              count(*) FILTER (WHERE status='cancelled' AND cancelled_at >= NOW() - INTERVAL '7 days') AS cancelled
                       FROM booking_appointments WHERE tenant_id=%s""", (tenant_id,))
        stats = cur.fetchone()
    finally:
        cur.close(); conn.close()
    return render_template(
        "portal/bookings_microsoft.html", customer=customer, bc=bc, upcoming=upcoming, stats=stats,
        configured=M.configured(), expired=M.secret_expired(),
        consent_url=M.admin_consent_url(M.BOOKINGS_SCOPES),
        last_checked=bookings_crm.when(bc["last_checked_at"], tz) if bc and bc.get("last_checked_at") else "",
    )


@bookings_bp.route("/integrations/microsoft-bookings/connect", methods=["GET"])
@team_feature(KEY)
def connect():
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _gate(customer)
    if r:
        return r
    if not M.configured() or M.secret_expired():
        flash("The Microsoft connection isn't available right now. Please try again later.", "warning")
        return redirect(url_for("bookings.page"))
    state = secrets.token_urlsafe(24)
    session["ms_oauth_state"] = state
    session["ms_oauth_purpose"] = "bookings"
    return redirect(M.auth_url(state, M.BOOKINGS_SCOPES))


def finish_connect():
    """Back from Microsoft (called by mailbox_routes.callback)."""
    session.pop("ms_oauth_purpose", None)
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _gate(customer)
    if r:
        return r
    expected = session.pop("ms_oauth_state", None)
    if not expected or request.args.get("state") != expected:
        flash("That sign-in link had expired. Please press Connect again.", "warning")
        return redirect(url_for("bookings.page"))
    if request.args.get("error"):
        desc = request.args.get("error_description", "")
        if (request.args.get("error") == "consent_required" or "AADSTS65001" in desc or "AADSTS90094" in desc
                or "AADSTS90008" in desc or "admin" in desc.lower()):
            session["bk_needs_consent"] = True
            flash("Your organisation's IT admin needs to approve PhiXtra first. Send them the link below.", "warning")
        elif request.args.get("error") == "access_denied":
            flash("Microsoft sign-in was cancelled. Nothing was connected.", "warning")
        else:
            flash("Microsoft didn't connect Bookings: " + (desc.splitlines()[0][:200] if desc else request.args.get("error")), "danger")
        return redirect(url_for("bookings.page"))
    try:
        tok = M.exchange_code(request.args.get("code", ""), M.BOOKINGS_SCOPES)
        who = M.me(tok["access_token"])
        businesses = M.booking_businesses(tok["access_token"])
    except M.GraphError as e:
        if e.code == "consent":
            session["bk_needs_consent"] = True
        flash(str(e) if e.code != "reconnect" else
              "Microsoft didn't let PhiXtra read Bookings with that sign-in. Use the Microsoft login that "
              "manages your Bookings page, and accept the permission.", "danger")
        return redirect(url_for("bookings.page"))
    if not businesses:
        flash(f"{who['email']} doesn't manage any Bookings page. Sign in with the Microsoft login that does.", "warning")
        return redirect(url_for("bookings.page"))
    actor = _current_actor(customer)
    tenant_id = int(customer["tenant_id"])
    one = businesses[0] if len(businesses) == 1 else None
    conn, cur = _db()
    try:
        cur.execute("""
            INSERT INTO booking_connections (tenant_id, owner_key, owner_label, email, provider_user_id,
                   refresh_token_enc, access_token_enc, access_expires_at, business_id, business_name, businesses,
                   status, last_error, next_check_at, connected_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NOW(),NOW(),NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
                owner_key=EXCLUDED.owner_key, owner_label=EXCLUDED.owner_label, email=EXCLUDED.email,
                provider_user_id=EXCLUDED.provider_user_id, refresh_token_enc=EXCLUDED.refresh_token_enc,
                access_token_enc=EXCLUDED.access_token_enc, access_expires_at=EXCLUDED.access_expires_at,
                business_id=EXCLUDED.business_id, business_name=EXCLUDED.business_name,
                businesses=EXCLUDED.businesses, status=EXCLUDED.status, last_error=NULL,
                next_check_at=NOW(), connected_at=NOW(), updated_at=NOW()
            RETURNING id""",
            (tenant_id, actor["key"], actor["label"], who["email"], who["id"],
             _encrypt_key(tok.get("refresh_token") or ""), _encrypt_key(tok["access_token"]), tok["expires_at"],
             one["id"] if one else None, one["name"] if one else None, psycopg2.extras.Json(businesses),
             "active" if one else "choose"))
        bc_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        cur.close(); conn.close()
    session.pop("bk_needs_consent", None)
    if one:
        threading.Thread(target=check_connection, args=(bc_id,), daemon=True).start()
        flash(f"Connected {one['name']}. Demos booked there now show on the lead by themselves "
              f"(checked every 5 minutes; upcoming bookings are being picked up now).", "success")
    else:
        flash("Connected. Choose which Bookings page to follow.", "success")
    return redirect(url_for("bookings.page"))


@bookings_bp.route("/integrations/microsoft-bookings/choose", methods=["POST"])
@team_feature(KEY)
def choose():
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _gate(customer)
    if r:
        return r
    bc = connection_for(int(customer["tenant_id"]))
    pick = next((b for b in (bc or {}).get("businesses") or [] if b.get("id") == request.form.get("business_id")), None)
    if not bc or not pick:
        flash("Choose one of your Bookings pages.", "warning")
        return redirect(url_for("bookings.page"))
    conn, cur = _db()
    try:
        cur.execute("""UPDATE booking_connections SET business_id=%s, business_name=%s, status='active',
                           last_error=NULL, next_check_at=NOW(), updated_at=NOW() WHERE id=%s""",
                    (pick["id"], pick["name"], bc["id"]))
        conn.commit()
    finally:
        cur.close(); conn.close()
    threading.Thread(target=check_connection, args=(bc["id"],), daemon=True).start()
    flash(f"Following {pick['name']}. Upcoming bookings are being picked up now.", "success")
    return redirect(url_for("bookings.page"))


@bookings_bp.route("/integrations/microsoft-bookings/check-now", methods=["POST"])
@team_feature(KEY)
def check_now():
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _gate(customer)
    if r:
        return r
    bc = connection_for(int(customer["tenant_id"]))
    if not bc or bc["status"] != "active":
        flash("Bookings isn't connected.", "warning")
        return redirect(url_for("bookings.page"))
    res = check_connection(bc["id"])
    if res.get("ok"):
        n = res.get("changes", 0)
        flash(f"Checked. {n} change{'s' if n != 1 else ''} found." if n else "Checked. Nothing new.", "success")
    else:
        flash(res.get("error") or "Couldn't check Bookings.", "danger")
    return redirect(url_for("bookings.page"))


@bookings_bp.route("/integrations/microsoft-bookings/disconnect", methods=["POST"])
@team_feature(KEY)
def disconnect():
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _gate(customer)
    if r:
        return r
    conn, cur = _db()
    try:
        cur.execute("""UPDATE booking_connections SET status='disconnected', refresh_token_enc=NULL,
                           access_token_enc=NULL, updated_at=NOW() WHERE tenant_id=%s""", (int(customer["tenant_id"]),))
        conn.commit()
    finally:
        cur.close(); conn.close()
    flash("Bookings disconnected. PhiXtra stops reading it. Demos already on leads stay there.", "success")
    return redirect(url_for("bookings.page"))


@bookings_bp.route("/leads/<int:lead_id>/demo/<int:appt_id>/mark", methods=["POST"])
@team_feature("leads.page")
def mark(lead_id: int, appt_id: int):
    """Demo done / They didn't show up, from the bar on the lead page."""
    r = _require_login()
    if r:
        return r
    r = _require_team_permission("leads.page")
    if r:
        return r
    customer = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    outcome = request.form.get("outcome")
    if outcome not in ("done", "no_show"):
        return redirect(url_for("portal.lead_detail", lead_id=lead_id))
    actor = _current_actor(customer)
    conn, cur = _db()
    try:
        cur.execute("""UPDATE booking_appointments SET status=%s, marked_by=%s, updated_at=NOW()
                       WHERE id=%s AND tenant_id=%s AND lead_id=%s AND status='booked' AND start_at <= NOW()
                       RETURNING id""", (outcome, actor["label"], appt_id, tenant_id, lead_id))
        if cur.fetchone():
            _event(cur, tenant_id, appt_id, lead_id, outcome,
                   "Marked as done." if outcome == "done" else "They didn't show up.", actor["label"])
            cur.execute("UPDATE merchant_pipeline_leads SET updated_at=NOW() WHERE id=%s", (lead_id,))
        conn.commit()
    finally:
        cur.close(); conn.close()
    flash("Demo marked as done." if outcome == "done" else "Recorded: they didn't show up.", "success")
    return redirect(url_for("portal.lead_detail", lead_id=lead_id) + "#demos")


# ── checking a Bookings page ───────────────────────────────────────────────

def _event(cur, tenant_id, appt_id, lead_id, kind, detail, actor="Microsoft Bookings"):
    cur.execute("""INSERT INTO booking_events (tenant_id, appointment_id, lead_id, kind, detail, actor)
                   VALUES (%s,%s,%s,%s,%s,%s)""", (tenant_id, appt_id, lead_id, kind, detail, actor))


def _still_allowed(bc) -> str | None:
    if not _plan_grants_feature(_get_tenant_plan(bc["tenant_id"]), KEY):
        return "Paused: the business's plan doesn't include Microsoft Bookings."
    return None


def _access_token(cur, bc) -> str:
    now = datetime.now(timezone.utc)
    if bc["access_token_enc"] and bc["access_expires_at"] and bc["access_expires_at"] > now:
        tok = _decrypt_key(bc["access_token_enc"])
        if tok:
            return tok
    rt = _decrypt_key(bc["refresh_token_enc"] or "")
    if not rt:
        raise M.GraphError("reconnect", "Microsoft no longer accepts this sign-in. Connect Bookings again.")
    t = M.refresh(rt, M.BOOKINGS_SCOPES)
    cur.execute("""UPDATE booking_connections SET access_token_enc=%s, access_expires_at=%s,
                       refresh_token_enc=COALESCE(%s, refresh_token_enc), updated_at=NOW() WHERE id=%s""",
                (_encrypt_key(t["access_token"]), t["expires_at"],
                 _encrypt_key(t["refresh_token"]) if t.get("refresh_token") else None, bc["id"]))
    cur.connection.commit()
    return t["access_token"]


def _graph_time(v) -> datetime | None:
    """Graph's {"dateTime": "2026-10-22T09:00:00.0000000", "timeZone": "UTC"}."""
    if not v or not v.get("dateTime"):
        return None
    raw = re.sub(r"(\.\d{6})\d+", r"\1", v["dateTime"]).replace("Z", "")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo:
        return dt
    try:
        return dt.replace(tzinfo=ZoneInfo(v.get("timeZone") or "UTC"))
    except Exception:
        return dt.replace(tzinfo=timezone.utc)


def _iso(v) -> datetime | None:
    try:
        return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


def _parse(a: dict) -> dict:
    cust = (a.get("customers") or [{}])[0] or {}
    return {
        "appointment_id": a.get("id"),
        "start_at": _graph_time(a.get("startDateTime")),
        "end_at": _graph_time(a.get("endDateTime")),
        "name": (cust.get("name") or a.get("customerName") or "").strip()[:200],
        "email": (cust.get("emailAddress") or a.get("customerEmailAddress") or "").strip().lower()[:255],
        "phone": (cust.get("phone") or a.get("customerPhone") or "").strip()[:60],
        "notes": (cust.get("notes") or a.get("customerNotes") or "").strip()[:2000],
        "service": (a.get("serviceName") or "").strip()[:200],
        "join_url": (a.get("joinWebUrl") or "")[:1000] or None,
        "booked_at": _iso(a.get("createdDateTime")),
    }


def _match_lead(cur, tenant_id: int, email: str, phone: str):
    if email:
        cur.execute("""SELECT id FROM merchant_pipeline_leads WHERE tenant_id=%s
                         AND (lower(email)=%s OR lower(COALESCE(other_emails,'')) LIKE %s)
                       ORDER BY (dropped_at IS NULL AND outcome IS NULL) DESC, updated_at DESC NULLS LAST LIMIT 1""",
                    (tenant_id, email, f"%{email}%"))
        row = cur.fetchone()
        if row:
            return row["id"]
    # Phone only when typed with its country code (+…): never guessed.
    if phone.startswith(("+", "00")):
        digits, _err = _phone_entry(phone)
        if digits:
            cur.execute("""SELECT id FROM merchant_pipeline_leads WHERE tenant_id=%s
                             AND (regexp_replace(COALESCE(phone,''), '\\D', '', 'g')=%s
                                  OR regexp_replace(COALESCE(whatsapp_number,''), '\\D', '', 'g')=%s)
                           ORDER BY (dropped_at IS NULL AND outcome IS NULL) DESC, updated_at DESC NULLS LAST LIMIT 1""",
                        (tenant_id, digits, digits))
            row = cur.fetchone()
            if row:
                return row["id"]
    return None


def _business_name(p: dict) -> str:
    dom = p["email"].split("@", 1)[1] if "@" in p["email"] else ""
    if dom and dom not in _FREE_MAIL:
        return dom.split(".")[0].replace("-", " ").title()[:200]
    return p["name"] or p["email"] or "Demo booking"


def _new_lead(cur, tenant_id: int, p: dict, tz) -> int:
    phone = ""
    if p["phone"].startswith(("+", "00")):
        phone, _err = _phone_entry(p["phone"])
    note = f"Booked a demo on the Bookings page for {bookings_crm.when(p['start_at'], tz)}."
    if p["notes"]:
        note += f"\nTheir note: {p['notes']}"
    cur.execute("""INSERT INTO merchant_pipeline_leads (tenant_id, customer_name, contact_person, phone, email, notes, source)
                   VALUES (%s,%s,%s,%s,%s,%s,'demo_booking') RETURNING id""",
                (tenant_id, _business_name(p), p["name"] or None, phone or None, p["email"] or None, note))
    lead_id = cur.fetchone()["id"]
    _link_lead_to_contact(cur, tenant_id, lead_id)
    return lead_id


def _on_new_booking(cur, bc, p: dict, tz) -> tuple:
    """Returns (appointment row id, lead id, created_lead)."""
    tenant_id = bc["tenant_id"]
    lead_id = _match_lead(cur, tenant_id, p["email"], p["phone"])
    created = False
    if not lead_id:
        lead_id, created = _new_lead(cur, tenant_id, p, tz), True
    cur.execute("""INSERT INTO booking_appointments (tenant_id, connection_id, business_id, appointment_id,
                       customer_name, email, phone, notes, service_name, start_at, end_at, join_url, booked_at,
                       lead_id, created_lead, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'booked')
                   ON CONFLICT (tenant_id, appointment_id) DO NOTHING RETURNING id""",
                (tenant_id, bc["id"], bc["business_id"], p["appointment_id"], p["name"], p["email"] or None,
                 p["phone"] or None, p["notes"] or None, p["service"] or None, p["start_at"], p["end_at"],
                 p["join_url"], p["booked_at"], lead_id, created))
    row = cur.fetchone()
    if not row:
        return None, None, False   # the other portal worker got there first
    appt_id = row["id"]
    who = " · ".join(x for x in (p["name"], p["email"]) if x)
    _event(cur, tenant_id, appt_id, lead_id, "booked",
           f"{who or 'Someone'} booked {bookings_crm.when(p['start_at'], tz)}"
           + (f" ({p['service']})" if p["service"] else "") + ".")
    if created:
        _event(cur, tenant_id, appt_id, lead_id, "lead_created",
               "New lead made from this booking: nobody in the CRM had this email or phone number.")
    cur.execute("SELECT count(*) AS n FROM email_sequence_people WHERE tenant_id=%s AND status IN ('active','ready') "
                "AND (lower(email)=%s OR lead_id=%s)", (tenant_id, p["email"] or "", lead_id))
    running = cur.fetchone()["n"]
    if running:
        _seq_stop_matching(cur, tenant_id, "Booked a demo", email=p["email"] or None, lead_id=lead_id)
        _event(cur, tenant_id, appt_id, lead_id, "sequence_stopped",
               f"Follow-up emails stopped ({running} sequence{'s' if running != 1 else ''}): they booked a demo.")
    _email_lead_contacted(cur, {"lead_id": lead_id, "tenant_id": tenant_id, "campaign_name": "Microsoft Bookings"},
                          "Booked a demo")
    cur.execute("UPDATE merchant_pipeline_leads SET updated_at=NOW() WHERE id=%s", (lead_id,))
    return appt_id, lead_id, created


def check_connection(bc_id: int) -> dict:
    conn, cur = _db()
    new_leads = []
    try:
        cur.execute("SELECT * FROM booking_connections WHERE id=%s", (bc_id,))
        bc = cur.fetchone()
        if not bc or bc["status"] != "active" or not bc["business_id"]:
            return {"ok": False, "error": "Bookings isn't connected."}
        skip = _still_allowed(bc)
        if skip:
            cur.execute("UPDATE booking_connections SET last_error=%s, last_checked_at=NOW() WHERE id=%s", (skip, bc_id))
            conn.commit()
            return {"ok": False, "error": skip}
        if not M.configured() or M.secret_expired():
            msg = "Paused: PhiXtra's Microsoft connection key needs renewing (PhiXtra is on it)."
            cur.execute("UPDATE booking_connections SET last_error=%s, last_checked_at=NOW() WHERE id=%s", (msg, bc_id))
            conn.commit()
            return {"ok": False, "error": msg}
        token = _access_token(cur, bc)
        now = datetime.now(timezone.utc)
        start = now - LOOK_BACK
        fetched = [_parse(a) for a in M.booking_appointments(token, bc["business_id"], start, now + LOOK_AHEAD)]
        fetched = [p for p in fetched if p["appointment_id"] and p["start_at"]]
        tz = bookings_crm.tenant_tz(cur, bc["tenant_id"])
        cur.execute("""SELECT * FROM booking_appointments WHERE tenant_id=%s AND business_id=%s""",
                    (bc["tenant_id"], bc["business_id"]))
        known = {r["appointment_id"]: r for r in cur.fetchall()}
        changes = 0
        for p in fetched:
            row = known.get(p["appointment_id"])
            try:
                if not row:
                    appt_id, lead_id, created = _on_new_booking(cur, bc, p, tz)
                    if not appt_id:
                        conn.rollback()   # the other worker saved it; drop this copy (and any lead it made)
                        continue
                    changes += 1
                    if created:
                        new_leads.append(lead_id)
                elif row["status"] == "cancelled":
                    cur.execute("""UPDATE booking_appointments SET status='booked', start_at=%s, end_at=%s,
                                       cancelled_at=NULL, updated_at=NOW() WHERE id=%s""",
                                (p["start_at"], p["end_at"], row["id"]))
                    _event(cur, bc["tenant_id"], row["id"], row["lead_id"], "booked",
                           f"Booked again: {bookings_crm.when(p['start_at'], tz)}.")
                    changes += 1
                elif row["start_at"] != p["start_at"] and row["status"] == "booked":
                    cur.execute("""UPDATE booking_appointments SET moved_from=start_at, start_at=%s, end_at=%s,
                                       join_url=COALESCE(%s, join_url), updated_at=NOW() WHERE id=%s""",
                                (p["start_at"], p["end_at"], p["join_url"], row["id"]))
                    _event(cur, bc["tenant_id"], row["id"], row["lead_id"], "moved",
                           f"From {bookings_crm.when(row['start_at'], tz)} to {bookings_crm.when(p['start_at'], tz)}"
                           " — changed on the Bookings page.")
                    changes += 1
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"⚠️ bookings {bc_id} appointment {p['appointment_id'][:20]} error:", e)
        # Gone from Bookings = cancelled. Only bookings inside the window just
        # read, so one that simply moved out of view is never "cancelled".
        seen = {p["appointment_id"] for p in fetched}
        for aid, row in known.items():
            if aid in seen or row["status"] != "booked" or row["start_at"] < start:
                continue
            cur.execute("""UPDATE booking_appointments SET status='cancelled', cancelled_at=NOW(), updated_at=NOW()
                           WHERE id=%s AND status='booked'""", (row["id"],))
            if cur.rowcount:
                _event(cur, bc["tenant_id"], row["id"], row["lead_id"], "cancelled",
                       f"Demo for {bookings_crm.when(row['start_at'], tz)} was cancelled on the Bookings page.")
                if row["lead_id"]:
                    cur.execute("UPDATE merchant_pipeline_leads SET updated_at=NOW() WHERE id=%s", (row["lead_id"],))
                changes += 1
            conn.commit()
        cur.execute("""UPDATE booking_connections SET last_checked_at=NOW(), last_error=NULL,
                           found_count=found_count+%s, updated_at=NOW() WHERE id=%s""", (changes, bc_id))
        conn.commit()
    except M.GraphError as e:
        conn.rollback()
        msg = str(e) if e.code != "reconnect" else "Microsoft no longer lets PhiXtra read this Bookings page. Connect Bookings again."
        if e.code in ("reconnect", "consent"):
            cur.execute("""UPDATE booking_connections SET status='needs_reconnect', last_error=%s,
                               last_checked_at=NOW(), updated_at=NOW() WHERE id=%s""", (msg, bc_id))
        else:
            cur.execute("UPDATE booking_connections SET last_error=%s, last_checked_at=NOW() WHERE id=%s", (msg, bc_id))
        conn.commit()
        return {"ok": False, "error": msg}
    except Exception as e:
        conn.rollback()
        print(f"⚠️ bookings {bc_id} check error:", e)
        return {"ok": False, "error": "Couldn't check Bookings. Please try again."}
    finally:
        cur.close(); conn.close()
    for lead_id in new_leads:
        try:
            pipeline_record_stage_change(lead_id, None, "new_lead", "Microsoft Bookings")
        except Exception as e:
            print("⚠️ bookings stage history:", e)
    return {"ok": True, "changes": changes}


def _due_connections() -> list:
    """Same claiming as the Outlook checker: the two portal workers never
    check the same Bookings page; practice (training) businesses skipped."""
    conn, cur = _db()
    try:
        cur.execute("""UPDATE booking_connections SET next_check_at = NOW() + %s
                       WHERE id IN (SELECT b.id FROM booking_connections b
                                    JOIN tenants tn ON tn.id = b.tenant_id AND NOT COALESCE(tn.is_training, FALSE)
                                    WHERE b.status='active' AND b.business_id IS NOT NULL AND b.next_check_at <= NOW()
                                    ORDER BY b.next_check_at LIMIT 50 FOR UPDATE OF b SKIP LOCKED)
                       RETURNING id""", (CHECK_EVERY,))
        ids = [r["id"] for r in cur.fetchall()]
        conn.commit()
        return ids
    finally:
        cur.close(); conn.close()


def _loop():
    time.sleep(40)   # let the portal finish its start-up (tables) first
    while True:
        try:
            for bc_id in _due_connections():
                check_connection(bc_id)
        except Exception as e:
            print("⚠️ bookings checker error:", e)
        time.sleep(60)


if not getattr(threading, "_phixtra_bookings_checker_started", False):
    threading._phixtra_bookings_checker_started = True  # type: ignore[attr-defined]
    threading.Thread(target=_loop, daemon=True).start()
