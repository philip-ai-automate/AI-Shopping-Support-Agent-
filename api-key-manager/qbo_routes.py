"""
qbo_routes.py — Integration › QuickBooks (10 Nov 2026). See qbo_link.py.

  GET  /integrations/quickbooks                     the page
  GET  /integrations/quickbooks/connect             off to Intuit sign-in
  GET  /integrations/quickbooks/callback            back from Intuit (code + realmId)
  POST /integrations/quickbooks/settings            item for unmatched lines, where accepted quotes go
  POST /integrations/quickbooks/disconnect
  POST /quotations/requests/<id>/qbo-invoice        accepted quotation → QuickBooks invoice (not emailed)
  POST /public/quickbooks/webhook                   Intuit tells PhiXtra an invoice changed (signed)
"""
import json
import secrets
import threading

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

import qbo_link as Q
import quote_requests as qr
import xero_link as X
from feature_access import public_route, team_feature
from portal_routes import (
    _current_actor, _customer_id, _get_customer, _inject_granted_features, _quotes_guard, _quotes_scope,
    _require_login, _require_team_permission,
)

qbo_bp = Blueprint("qbo", __name__)
qbo_bp.context_processor(_inject_granted_features)


def _guard():
    r = _require_login()
    if r:
        return None, None, r
    r = _require_team_permission("channels.page")
    if r:
        return None, None, r
    customer = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    if not qr.allowed(tenant_id):
        flash("QuickBooks is part of the AI Sales Agent for B2B plan.", "warning")
        return None, None, redirect(url_for("portal.channels_page"))
    return customer, tenant_id, None


def _xero_on(tenant_id: int) -> bool:
    link = X.link_for(tenant_id)
    return bool(link and link["status"] != "disconnected")


@qbo_bp.app_context_processor
def _qbo_card():
    def qbo_card():
        try:
            cust = _get_customer(_customer_id())
            if not cust or not qr.allowed(int(cust["tenant_id"])):
                return None
            link = Q.link_for(int(cust["tenant_id"]))
            return {"link": link if link and link["status"] != "disconnected" else None}
        except Exception:
            return None
    return {"qbo_card": qbo_card}


@qbo_bp.route("/integrations/quickbooks", methods=["GET"])
@team_feature("channels.page")
def page():
    customer, tenant_id, err = _guard()
    if err:
        return err
    link = Q.link_for(tenant_id)
    if link and link["status"] == "disconnected":
        link = None
    item_list, items_err = [], None
    if link and link["status"] == "active":
        try:
            item_list = Q.items(link)
        except Q.QboError as e:
            items_err = str(e)
    return render_template("portal/quickbooks.html", customer=customer, link=link, configured=Q.configured(),
                           has_shop=Q._has_shop(tenant_id), settings=(link or {}).get("settings") or {},
                           items=item_list, items_err=items_err, xero_on=_xero_on(tenant_id))


@qbo_bp.route("/integrations/quickbooks/connect", methods=["GET"])
@team_feature("channels.page")
def connect():
    customer, tenant_id, err = _guard()
    if err:
        return err
    if _xero_on(tenant_id):
        flash("Xero is connected. PhiXtra works with one accounts package at a time: disconnect Xero first.", "warning")
        return redirect(url_for("qbo.page"))
    if not Q.configured():
        flash("The QuickBooks connection isn't available yet. PhiXtra is finishing its set-up.", "warning")
        return redirect(url_for("qbo.page"))
    state = secrets.token_urlsafe(24)
    session["qbo_state"] = state
    return redirect(Q.auth_url(state))


@qbo_bp.route("/integrations/quickbooks/callback", methods=["GET"])
@team_feature("channels.page")
def callback():
    customer, tenant_id, err = _guard()
    if err:
        return err
    if not session.get("qbo_state") or request.args.get("state") != session.pop("qbo_state", None):
        flash("That QuickBooks sign-in link had expired. Please press Connect again.", "warning")
        return redirect(url_for("qbo.page"))
    if request.args.get("error"):
        flash("QuickBooks sign-in was cancelled. Nothing was connected." if request.args.get("error") == "access_denied"
              else "QuickBooks didn't connect: " + request.args.get("error", "")[:120], "warning")
        return redirect(url_for("qbo.page"))
    realm = request.args.get("realmId", "")
    if not realm.isdigit():
        flash("QuickBooks didn't say which company to connect. Please try again.", "warning")
        return redirect(url_for("qbo.page"))
    try:
        tok = Q.exchange_code(request.args.get("code", ""))
    except Q.QboError as e:
        flash(str(e), "danger")
        return redirect(url_for("qbo.page"))
    Q.save_link(tenant_id, tok, realm, _current_actor(customer)["label"])
    flash("Connected to QuickBooks. Now pick the item for quotation lines that aren't in your QuickBooks.", "success")
    return redirect(url_for("qbo.page"))


@qbo_bp.route("/integrations/quickbooks/settings", methods=["POST"])
@team_feature("channels.page")
def settings():
    customer, tenant_id, err = _guard()
    if err:
        return err
    item_id = request.form.get("fallback_item_id") or ""
    name = ""
    link = Q.link_for(tenant_id)
    if item_id and link:
        try:
            name = next((i["name"] for i in Q.items(link) if i["id"] == item_id), "")
        except Q.QboError:
            name = ""
    Q.save_settings(tenant_id, {"fallback_item_id": item_id if name else "", "fallback_item_name": name,
                                "accepted_to": request.form.get("accepted_to")})
    flash("QuickBooks settings saved.", "success")
    return redirect(url_for("qbo.page"))


@qbo_bp.route("/integrations/quickbooks/disconnect", methods=["POST"])
@team_feature("channels.page")
def disconnect():
    customer, tenant_id, err = _guard()
    if err:
        return err
    Q.disconnect(tenant_id)
    flash("QuickBooks disconnected. Invoices already made stay in QuickBooks.", "success")
    return redirect(url_for("qbo.page"))


@qbo_bp.route("/quotations/requests/<int:request_id>/qbo-invoice", methods=["POST"])
@team_feature("leads.page")
def create_invoice(request_id: int):
    customer, tenant_id, err = _quotes_guard()
    if err:
        return err
    r = qr.get(tenant_id, request_id, _quotes_scope(customer))
    if not r:
        flash("Request not found.", "danger")
        return redirect(url_for("portal.quotes_requests"))
    ok, msg = Q.create_invoice(tenant_id, request_id, _current_actor(customer), request.form.get("po_number") or "")
    flash(msg, "success" if ok else "warning")
    if request.form.get("back") == "deal" and r.get("lead_id"):
        return redirect(url_for("portal.opportunity_detail", lead_id=r["lead_id"]))
    return redirect(url_for("portal.quotes_request_detail", request_id=request_id))


@qbo_bp.route("/public/quickbooks/webhook", methods=["POST"])
@public_route
def webhook():
    body = request.get_data(cache=True)
    if not Q.webhook_ok(body, request.headers.get("intuit-signature", "")):
        return "", 401
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return "", 200
    threading.Thread(target=Q.handle_events, args=(payload,), daemon=True).start()
    return "", 200
