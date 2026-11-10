"""
xero_routes.py — Integration › Xero (10 Nov 2026). See xero_link.py.

  GET  /integrations/xero                     the page (connect / status / settings)
  GET  /integrations/xero/connect             off to Xero sign-in
  GET  /integrations/xero/callback            back from Xero
  POST /integrations/xero/settings            organisation, sales account, where accepted quotes go
  POST /integrations/xero/disconnect          disconnect (also removed at Xero)
  POST /quotations/requests/<id>/xero-invoice accepted quotation → Xero draft invoice
  POST /public/xero/webhook                   Xero tells PhiXtra an invoice changed (signed)
"""
import json
import secrets
import threading

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

import quote_requests as qr
import xero_link as X
from feature_access import public_route, team_feature
from portal_routes import (
    _current_actor, _customer_id, _get_customer, _inject_granted_features, _quotes_guard, _quotes_scope,
    _require_login, _require_team_permission, _team_member_has_permission,
)

xero_bp = Blueprint("xero", __name__)
xero_bp.context_processor(_inject_granted_features)


def _guard():
    """(customer, tenant_id, None) for people who may manage Integration, B2B plans only."""
    r = _require_login()
    if r:
        return None, None, r
    r = _require_team_permission("channels.page")
    if r:
        return None, None, r
    customer = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    if not qr.allowed(tenant_id):
        flash("Xero is part of the AI Sales Agent for B2B plan.", "warning")
        return None, None, redirect(url_for("portal.channels_page"))
    return customer, tenant_id, None


@xero_bp.app_context_processor
def _xero_card():
    def xero_card():
        """For the Integration page card (None when not on a B2B plan)."""
        try:
            cust = _get_customer(_customer_id())
            if not cust or not qr.allowed(int(cust["tenant_id"])):
                return None
            link = X.link_for(int(cust["tenant_id"]))
            return {"link": link if link and link["status"] != "disconnected" else None}
        except Exception:
            return None
    return {"xero_card": xero_card}


@xero_bp.route("/integrations/xero", methods=["GET"])
@team_feature("channels.page")
def page():
    customer, tenant_id, err = _guard()
    if err:
        return err
    link = X.link_for(tenant_id)
    if link and link["status"] == "disconnected":
        link = None
    return render_template("portal/xero.html", customer=customer, link=link, configured=X.configured(),
                           has_shop=X._has_shop(tenant_id), settings=(link or {}).get("settings") or {})


@xero_bp.route("/integrations/xero/connect", methods=["GET"])
@team_feature("channels.page")
def connect():
    customer, tenant_id, err = _guard()
    if err:
        return err
    if not X.configured():
        flash("The Xero connection isn't available yet. PhiXtra is finishing its set-up.", "warning")
        return redirect(url_for("xero.page"))
    state = secrets.token_urlsafe(24)
    session["xero_state"] = state
    return redirect(X.auth_url(state))


@xero_bp.route("/integrations/xero/callback", methods=["GET"])
@team_feature("channels.page")
def callback():
    customer, tenant_id, err = _guard()
    if err:
        return err
    if not session.get("xero_state") or request.args.get("state") != session.pop("xero_state", None):
        flash("That Xero sign-in link had expired. Please press Connect again.", "warning")
        return redirect(url_for("xero.page"))
    if request.args.get("error"):
        flash("Xero sign-in was cancelled. Nothing was connected." if request.args.get("error") == "access_denied"
              else "Xero didn't connect: " + request.args.get("error", "")[:120], "warning")
        return redirect(url_for("xero.page"))
    try:
        tok = X.exchange_code(request.args.get("code", ""))
        orgs = X.connections(tok["access_token"])
    except X.XeroError as e:
        flash(str(e), "danger")
        return redirect(url_for("xero.page"))
    if not orgs:
        flash("No Xero organisation was chosen. Please connect again and tick your organisation.", "warning")
        return redirect(url_for("xero.page"))
    X.save_link(tenant_id, tok, orgs, _current_actor(customer)["label"])
    flash(f"Connected to Xero: {orgs[0].get('tenantName')}." + (" You gave access to more than one organisation; "
          "choose which one below." if len(orgs) > 1 else ""), "success")
    return redirect(url_for("xero.page"))


@xero_bp.route("/integrations/xero/settings", methods=["POST"])
@team_feature("channels.page")
def settings():
    customer, tenant_id, err = _guard()
    if err:
        return err
    if request.form.get("org"):
        X.choose_org(tenant_id, request.form.get("org"))
    X.save_settings(tenant_id, {"account_code": request.form.get("account_code"),
                                "accepted_to": request.form.get("accepted_to")})
    flash("Xero settings saved.", "success")
    return redirect(url_for("xero.page"))


@xero_bp.route("/integrations/xero/disconnect", methods=["POST"])
@team_feature("channels.page")
def disconnect():
    customer, tenant_id, err = _guard()
    if err:
        return err
    X.disconnect(tenant_id)
    flash("Xero disconnected. Invoices already made stay in Xero.", "success")
    return redirect(url_for("xero.page"))


@xero_bp.route("/quotations/requests/<int:request_id>/xero-invoice", methods=["POST"])
@team_feature("leads.page")
def create_invoice(request_id: int):
    customer, tenant_id, err = _quotes_guard()
    if err:
        return err
    r = qr.get(tenant_id, request_id, _quotes_scope(customer))
    if not r:
        flash("Request not found.", "danger")
        return redirect(url_for("portal.quotes_requests"))
    ok, msg = X.create_invoice(tenant_id, request_id, _current_actor(customer), request.form.get("po_number") or "")
    flash(msg, "success" if ok else "warning")
    if request.form.get("back") == "deal" and r.get("lead_id"):
        return redirect(url_for("portal.opportunity_detail", lead_id=r["lead_id"]))
    return redirect(url_for("portal.quotes_request_detail", request_id=request_id))


@xero_bp.route("/public/xero/webhook", methods=["POST"])
@public_route
def webhook():
    body = request.get_data(cache=True)
    if not X.webhook_ok(body, request.headers.get("x-xero-signature", "")):
        return "", 401          # Xero's "intent to receive" check expects 401 for a bad signature
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return "", 200
    threading.Thread(target=X.handle_events, args=(payload,), daemon=True).start()
    return "", 200
