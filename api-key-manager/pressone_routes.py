"""
pressone_routes.py — PressOne (Nigerian business phone system) connection,
CRM angle only (2026-09-12): pull a business's own PressOne calls into
PhiXtra Connect's CRM. Bring-your-own-account model — PhiXtra never
resells PressOne numbers or bills for them; a business that already has a
PressOne account just links it here. See project_phixtra_pressone_integration
memory for the full design and the confirmed real webhook payload samples
this file (and pressone_calls.py, on the other service) are built from.

  GET  /pressone/connect     — the connect screen (enter Account ID + API
                                key, shows connection status once linked)
  POST /pressone/connect     — save the account, register PhiXtra's
                                webhook with PressOne
  POST /pressone/test        — fire PressOne's own test event at the saved
                                webhook, to prove the connection end to end
  POST /pressone/disconnect
  GET  /pressone/calls       — the Call Log (2026-09-12 follow-up): every
                                call across all contacts in one list, since
                                the contact-timeline view only ever shows
                                one customer at a time
  POST /pressone/auto-reply  — turn the missed-call WhatsApp auto-reply on
                                or off (default on) — the actual sending
                                happens in pressone_calls.py on the other
                                service, this just flips the switch it reads

The actual call events land on the OTHER service (whatsapp-gateway — see
pressone_calls.py there, the same service that already receives Meta's
webhooks) — this file only handles the one-time "link my account" step and
stores the credentials that receiver needs.

API reference: https://api-docs.pressone.africa/ . Base URL and auth
confirmed 2026-09-12: https://api.bedrock.pressone.co , Bearer auth
(`cak_...` key). Webhook registration confirmed as
POST /accounts/{accountId}/webhooks -> returns a one-time `secret`; the
exact request-body field names below (`url` + `events`, standard REST
shape) were NOT shown in the docs snippet available when this was written
— if the very first real connect attempt fails, PressOne's own error text
is shown directly on the screen so this can be corrected against a real
account rather than guessed a second time.
"""
import os

import psycopg2.extras
import requests as _req
from flask import Blueprint, request, render_template, redirect, url_for, flash

from db import get_db_connection, insert_audit_log
from portal_routes import (
    _require_login, _customer_id, _get_customer, _get_wa_connection, _require_plan_sub_feature,
    _require_team_permission,
)

pressone_bp = Blueprint("pressone", __name__, url_prefix="/pressone")

_API_BASE = "https://api.bedrock.pressone.co"
_EVENTS = ["call.completed", "call.missed", "recording.ready"]


def _webhook_target_url() -> str:
    """Public URL PressOne should POST call events to — lives on the
    WhatsApp gateway service (whatsapp.profitbuyz.com), the same service
    that already receives Meta's webhooks, not this portal app."""
    return os.getenv("PRESSONE_WEBHOOK_TARGET", "https://whatsapp.profitbuyz.com/pressone-webhook")


def get_account(tenant_id: int):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """SELECT id, account_id, api_key, webhook_id, webhook_secret, active,
                      auto_reply_missed_calls, connected_at
               FROM pressone_accounts WHERE tenant_id=%s""",
            (tenant_id,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


def _save_account(tenant_id, account_id, api_key, webhook_id, webhook_secret):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO pressone_accounts (tenant_id, account_id, api_key, webhook_id, webhook_secret, active)
            VALUES (%s,%s,%s,%s,%s,TRUE)
            ON CONFLICT (tenant_id) DO UPDATE SET
                account_id=EXCLUDED.account_id, api_key=EXCLUDED.api_key,
                webhook_id=EXCLUDED.webhook_id, webhook_secret=EXCLUDED.webhook_secret,
                active=TRUE
            """,
            (tenant_id, account_id, api_key, webhook_id, webhook_secret),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_contact_calls(tenant_id: int, contact_id: int, limit: int = 50):
    """Used by portal_routes.py's contact-detail timeline builder to fold
    calls into the same merged Activity & Notes timeline as notes,
    deal-stage moves and WhatsApp messages."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT event, direction, caller_number, callee_number, status,
                   duration_seconds, end_reason, recording_url, started_at
            FROM pressone_calls
            WHERE tenant_id=%s AND contact_id=%s
            ORDER BY started_at DESC LIMIT %s
            """,
            (tenant_id, contact_id, limit),
        )
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()


@pressone_bp.route("/connect", methods=["GET"])
def connect():
    r = _require_login()
    if r:
        return r
    r3 = _require_team_permission("channels.connect_pressone")
    if r3:
        return r3
    customer = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    r2 = _require_plan_sub_feature(customer, "voice.calls", "Voice Calls")
    if r2:
        return r2
    return render_template(
        "portal/pressone_connect.html",
        customer=customer,
        account=get_account(tenant_id),
        wa_connection=_get_wa_connection(tenant_id),
    )


@pressone_bp.route("/connect", methods=["POST"])
def connect_submit():
    r = _require_login()
    if r:
        return r
    r3 = _require_team_permission("channels.connect_pressone")
    if r3:
        return r3
    customer = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    r2 = _require_plan_sub_feature(customer, "voice.calls", "Voice Calls")
    if r2:
        return r2

    account_id = (request.form.get("account_id") or "").strip()
    api_key = (request.form.get("api_key") or "").strip()
    if not account_id or not api_key:
        flash("Both your PressOne Account ID and API Key are needed — copy them from your PressOne dashboard.", "danger")
        return redirect(url_for("pressone.connect"))

    try:
        resp = _req.post(
            f"{_API_BASE}/accounts/{account_id}/webhooks",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"url": _webhook_target_url(), "events": _EVENTS},
            timeout=15,
        )
    except Exception as e:
        flash(f"Could not reach PressOne: {e}", "danger")
        return redirect(url_for("pressone.connect"))

    if resp.status_code not in (200, 201):
        flash(f"PressOne rejected the connection (status {resp.status_code}): {resp.text[:300]}", "danger")
        return redirect(url_for("pressone.connect"))

    data = resp.json() if resp.content else {}
    webhook_id = data.get("id") or data.get("webhookId") or ""
    webhook_secret = data.get("secret") or ""
    if not webhook_secret:
        flash("PressOne accepted the connection but did not return a signing secret — calls won't be trusted until this is confirmed with PressOne support.", "warning")

    _save_account(tenant_id, account_id, api_key, webhook_id, webhook_secret)
    insert_audit_log(action="pressone_connected", tenant_id=tenant_id, details={"account_id": account_id})
    flash("PressOne connected! Calls will now start showing up on your contacts.", "success")
    return redirect(url_for("pressone.connect"))


@pressone_bp.route("/test", methods=["POST"])
def test_event():
    r = _require_login()
    if r:
        return r
    r3 = _require_team_permission("channels.connect_pressone")
    if r3:
        return r3
    customer = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    r2 = _require_plan_sub_feature(customer, "voice.calls", "Voice Calls")
    if r2:
        return r2
    acct = get_account(tenant_id)
    if not acct or not acct.get("webhook_id"):
        flash("Connect PressOne first.", "danger")
        return redirect(url_for("pressone.connect"))
    try:
        resp = _req.post(
            f"{_API_BASE}/webhooks/{acct['webhook_id']}/test",
            headers={"Authorization": f"Bearer {acct['api_key']}"},
            timeout=15,
        )
        if resp.status_code in (200, 201, 202):
            flash("Test event sent to PressOne — check back here in a few seconds.", "success")
        else:
            flash(f"PressOne test event failed (status {resp.status_code}): {resp.text[:300]}", "danger")
    except Exception as e:
        flash(f"Could not reach PressOne: {e}", "danger")
    return redirect(url_for("pressone.connect"))


@pressone_bp.route("/disconnect", methods=["POST"])
def disconnect():
    r = _require_login()
    if r:
        return r
    r3 = _require_team_permission("channels.connect_pressone")
    if r3:
        return r3
    customer = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    r2 = _require_plan_sub_feature(customer, "voice.calls", "Voice Calls")
    if r2:
        return r2
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE pressone_accounts SET active=FALSE WHERE tenant_id=%s", (tenant_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    insert_audit_log(action="pressone_disconnected", tenant_id=tenant_id, details={})
    flash("PressOne disconnected.", "success")
    return redirect(url_for("pressone.connect"))


@pressone_bp.route("/auto-reply", methods=["POST"])
def toggle_auto_reply():
    """Flip the missed-call WhatsApp auto-reply on/off (default on). The
    actual sending lives in pressone_calls.py on the whatsapp-gateway
    service — this only writes the switch it reads before sending."""
    r = _require_login()
    if r:
        return r
    r3 = _require_team_permission("channels.connect_pressone")
    if r3:
        return r3
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    r2 = _require_plan_sub_feature(customer, "voice.calls", "Voice Calls")
    if r2:
        return r2
    turn_on = request.form.get("enabled") == "1"
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE pressone_accounts SET auto_reply_missed_calls=%s WHERE tenant_id=%s",
            (turn_on, tenant_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    flash(
        "Missed-call WhatsApp replies turned " + ("on." if turn_on else "off."),
        "success",
    )
    return redirect(url_for("pressone.connect"))


@pressone_bp.route("/calls", methods=["GET"])
def call_log():
    """Every PressOne call across all contacts, most recent first — the
    contact-detail timeline only ever shows one customer's calls, this is
    the "scan everything at a glance" view asked for on 2026-09-12."""
    r = _require_login()
    if r:
        return r
    r3 = _require_team_permission("voice.calls")
    if r3:
        return r3
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    r2 = _require_plan_sub_feature(customer, "voice.calls", "Voice Calls")
    if r2:
        return r2

    status_filter = request.args.get("status_filter") or ""  # '', 'missed', 'completed'
    page = request.args.get("page", "1")
    page = int(page) if page.isdigit() and int(page) > 0 else 1
    per_page = 30
    offset = (page - 1) * per_page

    clauses = ["pc.tenant_id = %s"]
    params = [tenant_id]
    if status_filter == "missed":
        clauses.append("pc.event = 'call.missed'")
    elif status_filter == "completed":
        clauses.append("pc.event = 'call.completed'")
    where_sql = " AND ".join(clauses)

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(f"SELECT COUNT(*) AS c FROM pressone_calls pc WHERE {where_sql}", params)
        total = cur.fetchone()["c"]

        cur.execute(
            f"""
            SELECT pc.id, pc.contact_id, pc.event, pc.direction, pc.caller_number,
                   pc.callee_number, pc.status, pc.duration_seconds, pc.recording_url,
                   pc.started_at, wc.display_name, wc.phone AS contact_phone
            FROM pressone_calls pc
            LEFT JOIN wa_contacts wc ON wc.id = pc.contact_id
            WHERE {where_sql}
            ORDER BY pc.started_at DESC NULLS LAST, pc.id DESC
            LIMIT %s OFFSET %s
            """,
            params + [per_page, offset],
        )
        calls = cur.fetchall() or []

        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE event='call.missed')    AS missed,
              COUNT(*) FILTER (WHERE event='call.completed') AS completed,
              COALESCE(SUM(duration_seconds) FILTER (WHERE event='call.completed'), 0) AS total_seconds
            FROM pressone_calls WHERE tenant_id=%s
            """,
            (tenant_id,),
        )
        stats = cur.fetchone() or {"missed": 0, "completed": 0, "total_seconds": 0}
    finally:
        cur.close()
        conn.close()

    total_pages = max(1, (total + per_page - 1) // per_page)
    account = get_account(tenant_id)

    return render_template(
        "portal/pressone_call_log.html",
        customer=customer,
        calls=calls,
        status_filter=status_filter,
        page=page, total_pages=total_pages, total=total,
        stats=stats,
        connected=bool(account and account.get("active")),
    )
