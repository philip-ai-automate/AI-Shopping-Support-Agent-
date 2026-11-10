"""
sage_routes.py — Integration › Sage (10 Nov 2026). See sage_link.py.

  GET  /integrations/sage                  the page
  GET  /integrations/sage/connect          off to Sage sign-in
  GET  /integrations/sage/callback         back from Sage
  POST /integrations/sage/settings         business, sales account, VAT rate, where accepted quotes go
  POST /integrations/sage/disconnect
  POST /quotations/requests/<id>/sage-invoice   accepted quotation → Sage draft invoice
"""
import secrets

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

import quote_requests as qr
import sage_link as S
from feature_access import team_feature
from portal_routes import (
    _current_actor, _customer_id, _get_customer, _inject_granted_features, _quotes_guard, _quotes_scope,
    _require_login, _require_team_permission,
)

sage_bp = Blueprint("sage", __name__)
sage_bp.context_processor(_inject_granted_features)
S.start_checker()


def other_accounts(tenant_id: int, besides: str) -> str:
    """The other accounts package already connected ('Xero', 'QuickBooks', 'Sage') or ''."""
    try:
        if besides != "xero":
            import xero_link as X
            l = X.link_for(tenant_id)
            if l and l["status"] != "disconnected":
                return "Xero"
        if besides != "qbo":
            import qbo_link as Q
            if Q.connected(tenant_id):
                return "QuickBooks"
        if besides != "sage" and S.connected(tenant_id):
            return "Sage"
    except Exception as e:
        print("⚠️ accounts package check:", e)
    return ""


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
        flash("Sage is part of the AI Sales Agent for B2B plan.", "warning")
        return None, None, redirect(url_for("portal.channels_page"))
    return customer, tenant_id, None


@sage_bp.app_context_processor
def _sage_card():
    def sage_card():
        try:
            cust = _get_customer(_customer_id())
            if not cust or not qr.allowed(int(cust["tenant_id"])):
                return None
            link = S.link_for(int(cust["tenant_id"]))
            return {"link": link if link and link["status"] != "disconnected" else None}
        except Exception:
            return None
    return {"sage_card": sage_card}


@sage_bp.route("/integrations/sage", methods=["GET"])
@team_feature("channels.page")
def page():
    customer, tenant_id, err = _guard()
    if err:
        return err
    link = S.link_for(tenant_id)
    if link and link["status"] == "disconnected":
        link = None
    accounts, rates, opts_err = [], [], None
    if link and link["status"] == "active" and link.get("business_id"):
        try:
            accounts, rates = S.ledger_accounts(link), S.tax_rates(link)
        except S.SageError as e:
            opts_err = str(e)
    return render_template("portal/sage.html", customer=customer, link=link, configured=S.configured(),
                           has_shop=S._has_shop(tenant_id), settings=(link or {}).get("settings") or {},
                           accounts=accounts, rates=rates, opts_err=opts_err,
                           other=other_accounts(tenant_id, "sage"))


@sage_bp.route("/integrations/sage/connect", methods=["GET"])
@team_feature("channels.page")
def connect():
    customer, tenant_id, err = _guard()
    if err:
        return err
    other = other_accounts(tenant_id, "sage")
    if other:
        flash(f"{other} is connected. PhiXtra works with one accounts package at a time: disconnect {other} first.", "warning")
        return redirect(url_for("sage.page"))
    if not S.configured():
        flash("The Sage connection isn't available yet. PhiXtra is finishing its set-up.", "warning")
        return redirect(url_for("sage.page"))
    state = secrets.token_urlsafe(24)
    session["sage_state"] = state
    return redirect(S.auth_url(state))


@sage_bp.route("/integrations/sage/callback", methods=["GET"])
@team_feature("channels.page")
def callback():
    customer, tenant_id, err = _guard()
    if err:
        return err
    if not session.get("sage_state") or request.args.get("state") != session.pop("sage_state", None):
        flash("That Sage sign-in link had expired. Please press Connect again.", "warning")
        return redirect(url_for("sage.page"))
    if request.args.get("error"):
        flash("Sage sign-in was cancelled. Nothing was connected." if request.args.get("error") == "access_denied"
              else "Sage didn't connect: " + request.args.get("error", "")[:120], "warning")
        return redirect(url_for("sage.page"))
    try:
        tok = S.exchange_code(request.args.get("code", ""))
        n = S.save_link(tenant_id, tok, _current_actor(customer)["label"])
    except S.SageError as e:
        flash(str(e), "danger")
        return redirect(url_for("sage.page"))
    flash("Connected to Sage. Now pick the sales account and VAT rate for invoices."
          + (" You have more than one Sage business: choose which one below." if n > 1 else ""), "success")
    return redirect(url_for("sage.page"))


@sage_bp.route("/integrations/sage/settings", methods=["POST"])
@team_feature("channels.page")
def settings():
    customer, tenant_id, err = _guard()
    if err:
        return err
    if request.form.get("business"):
        S.choose_business(tenant_id, request.form.get("business"))
    link = S.link_for(tenant_id)
    acc_id, rate_id = request.form.get("ledger_account_id") or "", request.form.get("tax_rate_id") or ""
    acc_name = rate_name = ""
    try:
        if link and (acc_id or rate_id):
            acc_name = next((a["name"] for a in S.ledger_accounts(link) if a["id"] == acc_id), "")
            rate_name = next((t["name"] for t in S.tax_rates(link) if t["id"] == rate_id), "")
    except S.SageError:
        pass
    S.save_settings(tenant_id, {"ledger_account_id": acc_id if acc_name else "", "ledger_account_name": acc_name,
                                "tax_rate_id": rate_id if rate_name else "", "tax_rate_name": rate_name,
                                "accepted_to": request.form.get("accepted_to")})
    flash("Sage settings saved.", "success")
    return redirect(url_for("sage.page"))


@sage_bp.route("/integrations/sage/disconnect", methods=["POST"])
@team_feature("channels.page")
def disconnect():
    customer, tenant_id, err = _guard()
    if err:
        return err
    S.disconnect(tenant_id)
    flash("Sage disconnected. Invoices already made stay in Sage.", "success")
    return redirect(url_for("sage.page"))


@sage_bp.route("/quotations/requests/<int:request_id>/sage-invoice", methods=["POST"])
@team_feature("leads.page")
def create_invoice(request_id: int):
    customer, tenant_id, err = _quotes_guard()
    if err:
        return err
    r = qr.get(tenant_id, request_id, _quotes_scope(customer))
    if not r:
        flash("Request not found.", "danger")
        return redirect(url_for("portal.quotes_requests"))
    ok, msg = S.create_invoice(tenant_id, request_id, _current_actor(customer), request.form.get("po_number") or "")
    flash(msg, "success" if ok else "warning")
    if request.form.get("back") == "deal" and r.get("lead_id"):
        return redirect(url_for("portal.opportunity_detail", lead_id=r["lead_id"]))
    return redirect(url_for("portal.quotes_request_detail", request_id=request_id))
