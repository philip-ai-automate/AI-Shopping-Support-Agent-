"""
setup_help_routes.py — portal routes for "Set it up for me" (14 Nov 2026).
Rules and storage: setup_requests.py. Registered on the portal blueprint at
the end of portal_routes.py (register(...)).

  GET  /setup-help/slots          free times (JSON), shown in the visitor's zone
  POST /setup-help                book (JSON) — any portal page, logged in or not
  GET  /setup-help/b/<token>      change or cancel (link in the invite email)
  POST /setup-help/b/<token>      JSON {action: move|cancel, slot}

Template global setup_help(place, route=..., ...) → what the shared box needs
(portal/_setup_help.html).
"""
import re

from flask import jsonify, render_template, request, session

from feature_access import public_route
from db import get_db_connection
import setup_requests as SR

SESSION_KEY = "setup_help_ids"


def _ip():
    return (request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", request.remote_addr or "")
            ).split(",")[0].strip()


def _clean(s, n=160):
    return re.sub(r"\s+", " ", (s or "")).strip()[:n]


def _valid_email(s):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", s or ""))


def validate(body, phone_check):
    """Shared by the portal and the Shopify app. Returns (data, errors{field: msg})."""
    errs = {}
    method = body.get("method")
    if method not in SR.METHODS:
        errs["method"] = "Choose how we should contact you."
    phone, e = phone_check(_clean(body.get("phone"), 40), _clean(body.get("phone_cc"), 4))
    if e:
        errs["phone"] = e
    wa, e = phone_check(_clean(body.get("wa"), 40), _clean(body.get("wa_cc"), 4))
    if e:
        errs["wa"] = e
    email = _clean(body.get("email"), 200)
    if email and not _valid_email(email):
        errs["email"] = "Please enter a valid email address."
    need = {"voice": ("phone", phone, "Enter your phone number for the call."),
            "wa": ("wa", wa, "Enter your WhatsApp number."),
            "email": ("email", email, "Enter your email address.")}.get(method)
    if need and not need[1] and need[0] not in errs:
        errs[need[0]] = need[2]
    name = _clean(body.get("name"), 120)
    if not name:
        errs["name"] = "Please enter your name."
    at = SR.parse_slot(body.get("slot"))
    if not at:
        errs["slot"] = "Choose a day and a time on the calendar."
    return {"method": method, "phone": phone, "wa": wa, "email": email, "name": name, "slot": at,
            "tz": SR.safe_tz(_clean(body.get("tz"), 60))}, errs


def booked_json(row, is_new):
    t = SR.thanks_text(row)
    return {"ok": True, "new": is_new, "title": t["title"], "text": t["text"], "manage": t["manage"]}


def register(bp, phone_check, customer_id):
    """phone_check(raw, country) -> (digits, error); customer_id() -> int|None."""

    def _account():
        cid = customer_id()
        if not cid:
            return None
        conn = get_db_connection(); cur = conn.cursor()
        try:
            cur.execute("""SELECT c.id, c.tenant_id, c.first_name, c.last_name, c.email, c.company_name,
                                  c.business_country, c.phone_number, t.name, t.domain, t.signup_channel
                             FROM customers c LEFT JOIN tenants t ON t.id=c.tenant_id WHERE c.id=%s""", (int(cid),))
            r = cur.fetchone()
        finally:
            cur.close(); conn.close()
        if not r:
            return None
        k = ("customer_id", "tenant_id", "first_name", "last_name", "email", "company", "country", "phone",
             "tenant_name", "domain", "signup_channel")
        return dict(zip(k, r))

    @bp.app_template_global("setup_help")
    def _setup_help(place, route="portal", choice="", store_url="", email="", name="", company=""):
        """Everything the shared box needs on one page. Never raises."""
        try:
            acc = _account()
            ids = session.get(SESSION_KEY) or []
            if acc and ids:
                SR.link_session(ids, acc["tenant_id"], acc["customer_id"])
            row = SR.open_for(acc["tenant_id"] if acc else None, ids)
            cc = (acc or {}).get("country") or (request.headers.get("CF-IPCountry") or "").upper()[:2]
            return {
                "place": place, "route": route, "choice": choice,
                "store_url": store_url or ((acc or {}).get("domain") or ""),
                "name": name or " ".join(x for x in ((acc or {}).get("first_name"), (acc or {}).get("last_name")) if x),
                "email": email or (acc or {}).get("email") or "",
                "company": company or (acc or {}).get("company") or (acc or {}).get("tenant_name") or "",
                "phone": (acc or {}).get("phone") or "", "country": cc if len(cc) == 2 else "",
                "booked": SR.thanks_text(row) if row else None,
            }
        except Exception as e:
            print("⚠️ setup_help ctx:", e)
            return {"place": place, "route": route, "choice": choice, "store_url": store_url, "name": name,
                    "email": email, "company": company, "phone": "", "country": "", "booked": None}

    @bp.route("/setup-help/slots")
    @public_route
    def setup_help_slots():
        ex = None
        tok = request.args.get("b")
        if tok:
            r = SR.get(token=tok)
            ex = r["id"] if r else None
        resp = jsonify({"ok": True, "days": SR.slots(exclude_id=ex), "uk_hours": f"{SR.OPEN_HOUR}:00–{SR.CLOSE_HOUR}:00"})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/setup-help", methods=["POST"])
    @public_route
    def setup_help_book():
        if not request.is_json:
            return jsonify({"ok": False, "error": "Please use the form."}), 400
        body = request.get_json(silent=True) or {}
        if body.get("website"):            # honeypot
            return jsonify({"ok": True, "title": "Thanks.", "text": "", "manage": ""})
        data, errs = validate(body, phone_check)
        if errs:
            return jsonify({"ok": False, "errors": errs}), 400
        acc = _account()
        route = body.get("route") if body.get("route") in ("portal", "woocommerce") else "portal"
        data.update(route=route, place=body.get("place") if body.get("place") in SR.PLACES else None,
                    signup_choice=_clean(body.get("choice"), 120), company=_clean(body.get("company")),
                    store_url=_clean(body.get("store_url"), 200),
                    tenant_id=(acc or {}).get("tenant_id"), customer_id=(acc or {}).get("customer_id"))
        if acc:
            data["company"] = data["company"] or acc.get("company") or acc.get("tenant_name") or ""
            data["store_url"] = data["store_url"] or acc.get("domain") or ""
            data["email"] = data["email"] or acc.get("email") or ""
        row, err, is_new = SR.create(data, _ip())
        if err:
            return jsonify({"ok": False, "error": err}), 409 if "booked" in err else 429
        ids = [i for i in (session.get(SESSION_KEY) or []) if i != row["id"]][-4:] + [row["id"]]
        session[SESSION_KEY] = ids
        return jsonify(booked_json(row, is_new))

    @bp.route("/setup-help/b/<token>", methods=["GET", "POST"])
    @public_route
    def setup_help_manage(token):
        r = SR.get(token=token[:80])
        if request.method == "GET":
            return render_template("portal/setup_help_manage.html", r=r, sr=SR,
                                   when=(SR.when_text(r["slot_at"], r.get("tz"), True) if r else ""),
                                   can_change=bool(r and r["status"] in SR.OPEN))
        if not r or not request.is_json:
            return jsonify({"ok": False, "error": "This booking can't be found."}), 404
        body = request.get_json(silent=True) or {}
        if body.get("action") == "cancel":
            row = SR.cancel(token)
            return jsonify({"ok": bool(row), "title": "Cancelled.",
                            "text": "Your set-up call is cancelled. You can book a new time from your PhiXtra account."})
        at = SR.parse_slot(body.get("slot"))
        if not at:
            return jsonify({"ok": False, "error": "Choose a day and a time on the calendar."}), 400
        row, err = SR.reschedule(token, at)
        if err:
            return jsonify({"ok": False, "error": err}), 409
        return jsonify(booked_json(row, False))
