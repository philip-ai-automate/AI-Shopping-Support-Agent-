"""
csrf_protect.py — CSRF protection for the PhiXtra portal (P0-2B.2, 17 Nov 2026).

Every POST / PUT / PATCH / DELETE to the portal must carry this session's CSRF
token — as the form field "csrf_token" or the header "X-CSRF-Token" — and must
not come from another site (Origin / Sec-Fetch-Site / Referer). Covers every
blueprint: portal, team, admin, school, estate, integrations (Xero invoice
creation and reconciliation, settings, disconnect), login and sign-up forms.

Only callers that prove who they are WITHOUT the browser's cookies are exempt,
each named below with the reason (signed webhooks, server-to-server calls with
a shared token, the Shopify embedded app's signed session tokens, the public
website widget's site key). Anything not listed is protected, so a new route
is protected by default.

How the token reaches the browser, without editing the 255 templates that
have forms:
  * HTML responses: a hidden csrf_token input is added inside every same-site
    POST form, and <meta name=csrf-token> plus a small script are added to
    <head>. The script adds X-CSRF-Token to same-origin fetch() / XHR calls
    and to forms built or submitted from JavaScript.
  * Templates can also use {{ csrf_token() }} directly.
The token in each page is masked with fresh random bytes (so it never repeats
in compressed responses); the secret behind it lives in the signed session.

Cookies: HttpOnly, SameSite=Lax, Secure (PORTAL_COOKIE_SECURE=0 only for
local plain-HTTP development).
"""
import base64
import hmac
import os
import re
import secrets
from urllib.parse import urlparse

from flask import Response, g, jsonify, request, session
from markupsafe import Markup

SESSION_KEY = "_csrf"
FIELD = "csrf_token"
HEADERS = ("X-CSRF-Token", "X-CSRFToken")
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MESSAGE = ("This page was open too long, or the request came from another website, so it was not carried out. "
           "Go back, reload the page and try again.")

# Endpoints whose callers authenticate WITHOUT the portal's session cookie.
# endpoint → how the caller is authenticated (checked in that view).
EXEMPT = {
    # Signed webhooks from providers
    "facebook.deletion_callback": "Facebook signed_request (HMAC, FB_APP_SECRET)",
    "portal.stripe_webhook": "Stripe-Signature (STRIPE_WEBHOOK_SECRET)",
    "portal.zeptomail_bounce_webhook": "secret key in the URL (ZEPTOMAIL_WEBHOOK_KEY, compare_digest)",
    "portal.billing_flutterwave_webhook": "Flutterwave verif-hash header",
    "portal._confirm_fw_order_payment": "Flutterwave order webhook; re-verifies the transaction with Flutterwave",
    "xero.webhook": "x-xero-signature (HMAC, XERO_WEBHOOK_KEY)",
    "qbo.webhook": "intuit-signature (HMAC, QBO_WEBHOOK_TOKEN)",
    "shopify.webhooks": "X-Shopify-Hmac-Sha256",
    # Server-to-server calls from PhiXtra's own services
    "portal.internal_provision_wa_merchant": "Bearer PHIXTRA_INTERNAL_TOKEN",
    "portal.internal_grant_trial_upgrade": "Bearer PHIXTRA_INTERNAL_TOKEN",
    "portal.internal_fw_checkout_link": "Bearer PHIXTRA_INTERNAL_TOKEN",
    "shopify.internal_prices": "Bearer PHIXTRA_INTERNAL_TOKEN",
    "shopify.internal_discount_brief": "Bearer PHIXTRA_INTERNAL_TOKEN",
    "shopify.internal_discount_make": "Bearer PHIXTRA_INTERNAL_TOKEN",
    "portal.wp_connect_exchange": "WordPress plugin server swaps a one-time code (hashed, bound to the site)",
    # Shopify embedded app (inside Shopify admin): App Bridge session token in Authorization
    "shopify.api_session": "Shopify session token (Bearer, verified)",
    "shopify.api_setup_help": "Shopify session token (Bearer, verified)",
    "shopify.api_signup": "Shopify session token (Bearer, verified)",
    "shopify.api_signup_confirm": "Shopify session token (Bearer, verified)",
    "shopify.api_link": "Shopify session token (Bearer, verified)",
    "shopify.api_link_confirm": "Shopify session token (Bearer, verified)",
    "shopify.api_sync": "Shopify session token (Bearer, verified)",
    "shopify.api_trial": "Shopify session token (Bearer, verified)",
    "shopify.api_open": "Shopify session token (Bearer, verified)",
    # Public website widget / WooCommerce plugin on the merchant's own site (cross-site by design)
    "quote_public.quote_config": "website key + Origin must be the business's domain, or API key",
    "quote_public.quote_submit": "website key + Origin must be the business's domain, or API key",
    "quote_public.woo_hello": "WooCommerce plugin signature (woo_orders.verify)",
    "quote_public.woo_order_status": "WooCommerce plugin signature (woo_orders.verify)",
}

# Pages that are not given the token: framed inside Shopify admin (they call
# the exempt Shopify APIs with session tokens, never the portal session).
NO_INJECT_BLUEPRINTS = frozenset({"shopify", "shopify_root"})

_FORM_RE = re.compile(r"<form\b[^>]*>", re.I)
_POST_RE = re.compile(r"""\bmethod\s*=\s*["']?\s*post\b""", re.I)
_ACTION_RE = re.compile(r"""\baction\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_HEAD_RE = re.compile(r"<head\b[^>]*>", re.I)
MAX_INJECT_BYTES = 8 * 1024 * 1024

# Runs in every portal page before the page's own scripts. No quotes are
# needed around the token (URL-safe base64), so it is safe wherever it lands.
SHIM = ("<script>(function(){var m=document.querySelector('meta[name=csrf-token]');if(!m)return;"
        "var k=m.getAttribute('content');"
        "function same(u){try{return new URL(u,location.href).origin===location.origin}catch(e){return false}}"
        "function unsafe(x){return !/^(GET|HEAD|OPTIONS|TRACE)$/i.test(x||'GET')}"
        "var f=window.fetch;if(f){window.fetch=function(i,o){try{"
        "var u=(typeof i==='string'||(window.URL&&i instanceof URL))?String(i):(i&&i.url);"
        "var x=(o&&o.method)||(i&&i.method)||'GET';"
        "if(unsafe(x)&&same(u)){o=Object.assign({},o||{});"
        "var h=new Headers(o.headers||(i&&i.headers)||{});if(!h.has('X-CSRF-Token'))h.set('X-CSRF-Token',k);o.headers=h;}"
        "}catch(e){}return f.call(this,i,o)}}"
        "var X=window.XMLHttpRequest;if(X){var op=X.prototype.open,sd=X.prototype.send;"
        "X.prototype.open=function(x,u){this.__phxc=unsafe(x)&&same(u);return op.apply(this,arguments)};"
        "X.prototype.send=function(){if(this.__phxc){try{this.setRequestHeader('X-CSRF-Token',k)}catch(e){}}"
        "return sd.apply(this,arguments)}}"
        "function add(fm){try{if(!fm||!unsafe(fm.getAttribute('method')))return;"
        "if(!same(fm.getAttribute('action')||location.href))return;"
        "if(fm.querySelector('input[name=csrf_token]'))return;"
        "var i=document.createElement('input');i.type='hidden';i.name='csrf_token';i.value=k;fm.appendChild(i)}catch(e){}}"
        "document.addEventListener('submit',function(e){add(e.target)},true);"
        "var P=window.HTMLFormElement&&HTMLFormElement.prototype;if(P&&P.submit){var ps=P.submit;"
        "P.submit=function(){add(this);return ps.apply(this,arguments)}}"
        "})();</script>")


# ── tokens ─────────────────────────────────────────────────────────────────

def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret(create: bool = True):
    s = session.get(SESSION_KEY)
    if not s and create:
        s = _b64e(secrets.token_bytes(32))
        session[SESSION_KEY] = s
    return _b64d(s) if s else None


def generate_token() -> str:
    """The token for this response (masked; one value per response)."""
    if getattr(g, "_phx_csrf", None):
        return g._phx_csrf
    raw = _secret(create=True)
    pad = secrets.token_bytes(len(raw))
    g._phx_csrf = _b64e(pad + bytes(a ^ b for a, b in zip(raw, pad)))
    return g._phx_csrf


def valid(token: str) -> bool:
    raw = _secret(create=False)
    if not raw or not token or len(token) > 200:
        return False
    try:
        b = _b64d(token)
    except (ValueError, TypeError):
        return False
    if len(b) != 2 * len(raw):
        return False
    pad, masked = b[:len(raw)], b[len(raw):]
    return hmac.compare_digest(bytes(a ^ c for a, c in zip(pad, masked)), raw)


def rotate():
    """New secret (e.g. after signing in); pages opened before it stop working."""
    session.pop(SESSION_KEY, None)
    g.pop("_phx_csrf", None)


# Who is signed in (portal owner / team member / viewing as a customer /
# PhiXtra admin / school / estate staff). When it changes during a request —
# sign-in, sign-out, start or stop viewing as a customer — the token is
# replaced, so a token learned before signing in is worth nothing after.
IDENTITY_KEYS = ("customer_id", "team_member_id", "impersonate_customer_id", "portal_admin_username",
                 "admin_username", "school_staff_id", "school_id", "re_staff_id")


def _identity() -> tuple:
    return tuple(session.get(k) for k in IDENTITY_KEYS)


# ── checking requests ──────────────────────────────────────────────────────

def _own_hosts() -> set:
    hosts = {(request.host or "").lower()}
    hosts |= {h.strip().lower() for h in os.getenv("PORTAL_TRUSTED_HOSTS", "").split(",") if h.strip()}
    return hosts


def _foreign(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return True
    return bool(host) and host not in _own_hosts()


def _site_problem():
    """Why the request looks cross-site, or None."""
    if (request.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        return "cross-site"
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        return "origin" if _foreign(origin) or not urlparse(origin).netloc else None
    ref = request.headers.get("Referer")
    if not origin and ref and _foreign(ref):
        return "referer"
    return None


def _token_from_request() -> str:
    for h in HEADERS:
        if request.headers.get(h):
            return request.headers[h]
    try:
        return request.form.get(FIELD) or ""
    except Exception:
        return ""


def _wants_json() -> bool:
    accept = (request.headers.get("Accept") or "").lower()
    return (request.is_json or "application/json" in accept or request.headers.get("X-Requested-With")
            or any(request.headers.get(h) for h in HEADERS))


def _reject(reason: str):
    # Never log the token or the full path (paths can carry their own secrets).
    print(f"⚠️ CSRF refused: {request.method} {request.endpoint} ({reason})", flush=True)
    if _wants_json():
        resp = jsonify({"ok": False, "error": "csrf", "message": MESSAGE})
    else:
        back = request.headers.get("Referer") or "/"
        if _foreign(back):
            back = "/"
        from markupsafe import escape
        resp = Response(
            "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Please reload the page</title></head><body style='font-family:Arial,sans-serif;max-width:560px;"
            f"margin:60px auto;padding:0 16px;line-height:1.5'><h1 style='font-size:20px'>Please reload the page</h1>"
            f"<p>{escape(MESSAGE)}</p><p><a href='{escape(back)}'>Go back</a></p></body></html>", mimetype="text/html")
    resp.status_code = 400
    resp.headers["X-CSRF-Rejected"] = reason
    resp.headers["Cache-Control"] = "no-store"
    return resp


def protect():
    """before_request: refuse unsafe requests without a valid token or from another site."""
    g._phx_identity = _identity()
    if request.method not in UNSAFE:
        return None
    if request.endpoint is None:          # no such route: Flask answers 404/405 itself
        return None
    if request.endpoint in EXEMPT:
        return None
    problem = _site_problem()
    if problem:
        return _reject(problem)
    token = _token_from_request()
    if not token:
        return _reject("missing")
    if not valid(token):
        return _reject("invalid")
    return None


# ── giving pages the token ──────────────────────────────────────────────────

def _injectable(resp) -> bool:
    if request.endpoint in EXEMPT or (request.blueprint or "") in NO_INJECT_BLUEPRINTS:
        return False
    if resp.direct_passthrough or resp.is_streamed or resp.mimetype != "text/html":
        return False
    if not (200 <= resp.status_code < 500) or resp.headers.get("X-CSRF-Rejected"):
        return False
    if "script-src" in (resp.headers.get("Content-Security-Policy") or ""):
        return False                      # e.g. email previews that forbid all scripts
    length = resp.calculate_content_length()
    return length is None or length <= MAX_INJECT_BYTES


def inject_into(html: str, token: str) -> str:
    def add(m):
        tag = m.group(0)
        if not _POST_RE.search(tag):
            return tag
        a = _ACTION_RE.search(tag)
        action = next((x for x in a.groups() if x is not None), "") if a else ""
        if action.startswith(("http://", "https://", "//")) and _foreign(action if "://" in action else "http:" + action):
            return tag                     # never send the token to another site
        return tag + f"<input type=hidden name={FIELD} value={token}>"

    html = _FORM_RE.sub(add, html)
    if "name=csrf-token" not in html:
        html = _HEAD_RE.sub(lambda m: m.group(0) + f"<meta name=csrf-token content={token}>" + SHIM, html, count=1)
    return html


def inject(resp):
    """after_request: put the token into HTML pages (forms, <meta>, script)."""
    try:
        before = getattr(g, "_phx_identity", None)
        if before is not None and before != _identity() and session.get(SESSION_KEY):
            rotate()
        if not _injectable(resp):
            return resp
        body = resp.get_data(as_text=True)
        if "<form" not in body.lower() and "<head" not in body.lower():
            return resp
        resp.set_data(inject_into(body, generate_token()))
    except Exception as e:                 # never break a page because of this
        print("⚠️ CSRF token injection:", type(e).__name__, flush=True)
    return resp


def init_app(app):
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("PORTAL_COOKIE_SECURE", "1") != "0",
    )
    # First of all before_request hooks: nothing else runs for a refused request.
    app.before_request_funcs.setdefault(None, []).insert(0, protect)
    app.after_request(inject)
    app.jinja_env.globals["csrf_token"] = lambda: Markup(generate_token())
    return app
