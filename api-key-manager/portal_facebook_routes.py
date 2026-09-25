"""
portal_facebook_routes.py — Facebook platform compliance endpoints, plus the
Messenger channel connect flow (omnichannel Phase 1, 2026-09-11).

Facebook Data Deletion Callback (required for all Facebook apps):
  POST /facebook/deletion   — Facebook calls this when a user removes the app
  GET  /facebook/deletion/status — status page linked from the deletion confirmation

Signed-request verification follows:
  https://developers.facebook.com/docs/facebook-login/manually-build-a-login-flow
  #confirm-token

Messenger connect (mirrors the WhatsApp Embedded Signup flow in
portal_routes.py — same code-exchange helper, same session-handoff pattern
for a multi-choice picker):
  GET  /facebook/messenger/connect   — the connect page
  POST /facebook/messenger/callback  — exchange the login code, list Pages
  POST /facebook/messenger/complete  — save the chosen Page (2+ Pages case)

Now that Facebook Login is actually used (a business owner authorizes the
app to connect their Page), the deletion callback below does real work: it
looks up fb_pages by fb_user_id and removes what it finds, instead of always
reporting "no data".
"""
import base64
import hashlib
import hmac
import json
import os
import secrets

import psycopg2.extras
import requests as _req
from flask import (Blueprint, request, jsonify, render_template,
                    render_template_string, session, redirect, url_for, flash)
from db import get_db_connection, insert_audit_log
from feature_access import team_feature, public_route
from portal_routes import (
    _require_login, _customer_id, _get_customer, _exchange_code_for_tokens, _GRAPH,
    _require_team_permission, _team_member_has_permission,
    _inject_granted_features, _inject_connect_flag,
)

facebook_bp = Blueprint("facebook", __name__, url_prefix="/facebook")
# The side menu's padlocks read these; they're registered on portal_bp only,
# so pages on this blueprint need them too or every menu item shows locked.
facebook_bp.context_processor(_inject_granted_features)
facebook_bp.context_processor(_inject_connect_flag)

# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_signed_request(signed_request: str, app_secret: str) -> dict | None:
    """
    Parse and verify a Facebook signed_request string.
    Returns the decoded payload dict on success, None on failure.
    """
    try:
        encoded_sig, payload = signed_request.split(".", 1)
    except ValueError:
        return None

    def _b64_decode(s: str) -> bytes:
        # Facebook uses URL-safe base64 without padding
        s += "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s)

    try:
        sig = _b64_decode(encoded_sig)
        data = json.loads(_b64_decode(payload).decode("utf-8"))
    except Exception:
        return None

    if data.get("algorithm", "").upper() != "HMAC-SHA256":
        return None

    expected = hmac.new(
        app_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    if not hmac.compare_digest(sig, expected):
        return None

    return data


def _ensure_deletion_table():
    """Create the deletion log table if it does not exist (idempotent)."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS facebook_deletion_requests (
            id                SERIAL PRIMARY KEY,
            fb_user_id        TEXT,
            confirmation_code TEXT NOT NULL,
            requested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status            TEXT NOT NULL DEFAULT 'no_data'
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ── routes ───────────────────────────────────────────────────────────────────

@facebook_bp.route("/deletion", methods=["POST"])
@public_route
def deletion_callback():
    """
    Facebook Data Deletion Request Callback.

    Facebook POSTs a signed_request form field.  We verify the signature,
    record the request, and return the required JSON:
      { "url": "<status_page>", "confirmation_code": "<code>" }
    """
    app_secret = os.getenv("FB_APP_SECRET", "")
    if not app_secret:
        # Misconfigured — refuse to accept unverifiable requests
        return jsonify({"error": "Server not configured"}), 503

    signed_request = request.form.get("signed_request", "")
    if not signed_request:
        return jsonify({"error": "Missing signed_request"}), 400

    payload = _parse_signed_request(signed_request, app_secret)
    if payload is None:
        return jsonify({"error": "Invalid signed_request"}), 403

    fb_user_id = payload.get("user_id") or payload.get("psid") or "unknown"
    confirmation_code = secrets.token_hex(16)

    try:
        _ensure_deletion_table()
        conn = get_db_connection()
        cur = conn.cursor()

        # Real removal: if this Facebook account connected a Page (the
        # Messenger connect flow), delete that connection now rather than
        # just logging the request — this is the "look up by fb_user_id and
        # wipe them" step the earlier version of this file was waiting on.
        removed = 0
        if fb_user_id and fb_user_id != "unknown":
            cur.execute("DELETE FROM fb_pages WHERE fb_user_id = %s", (fb_user_id,))
            removed = cur.rowcount or 0

        cur.execute(
            """
            INSERT INTO facebook_deletion_requests
                (fb_user_id, confirmation_code, status)
            VALUES (%s, %s, %s)
            """,
            (fb_user_id, confirmation_code, "deleted" if removed else "no_data"),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as exc:
        print("⚠️  facebook_deletion_requests insert error:", exc)
        # Still return a valid response — logging failure must not break compliance

    base_url = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")
    status_url = f"{base_url}/facebook/deletion/status?code={confirmation_code}"

    return jsonify({"url": status_url, "confirmation_code": confirmation_code})


_STATUS_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Data Deletion — PhiXtra</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 540px; margin: 80px auto;
           padding: 0 20px; color: #1a1a2e; }
    h1   { font-size: 1.4rem; margin-bottom: .5rem; }
    p    { line-height: 1.6; color: #444; }
    .code{ font-family: monospace; background: #f4f4f4; padding: 4px 8px;
           border-radius: 4px; }
    .ok  { color: #1a7a4a; font-weight: 600; }
  </style>
</head>
<body>
  <h1>Data Deletion Request</h1>
  {% if valid %}
    <p class="ok">Your data deletion request has been received.</p>
    {% if status == 'deleted' %}
    <p>
      The Facebook Page connection linked to your account has been removed
      from PhiXtra, along with its access token. No further action is needed.
    </p>
    {% else %}
    <p>
      PhiXtra did not find any Facebook Page connection linked to your
      account. No further action is needed.
    </p>
    {% endif %}
    <p>Confirmation code: <span class="code">{{ code }}</span></p>
  {% else %}
    <p>No deletion request found for that confirmation code.</p>
    <p>If you believe this is an error, please contact
       <a href="mailto:hello@phixtra.com">hello@phixtra.com</a>.</p>
  {% endif %}
</body>
</html>"""


@facebook_bp.route("/deletion/status", methods=["GET"])
@public_route
def deletion_status():
    """Status page that users land on after a data deletion request."""
    code = request.args.get("code", "").strip()
    valid = False
    status = "no_data"

    if code:
        try:
            _ensure_deletion_table()
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT status FROM facebook_deletion_requests WHERE confirmation_code = %s",
                (code,),
            )
            row = cur.fetchone()
            valid = row is not None
            if row:
                status = row[0]
            cur.close()
            conn.close()
        except Exception as exc:
            print("⚠️  deletion_status lookup error:", exc)

    return render_template_string(_STATUS_PAGE, valid=valid, code=code, status=status), (200 if valid else 404)


# ── Messenger connect (omnichannel Phase 1) + live receiving (Phase 2) ─────

def _subscribe_page(page_id: str, page_access_token: str) -> bool:
    """Turn on live message delivery for a connected Page — the webhook
    handler (meta_messenger.py, in the whatsapp-gateway service) now exists
    to receive it, so this can run at connect time instead of waiting."""
    try:
        r = _req.post(f"{_GRAPH}/{page_id}/subscribed_apps",
                      params={"subscribed_fields": "messages"},
                      headers={"Authorization": f"Bearer {page_access_token}"}, timeout=10)
        ok = r.status_code == 200
        if not ok:
            print(f"⚠️ fb page subscribe failed for page={page_id}: {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"⚠️ _subscribe_page error: {e}")
        return False


def _discover_pages(token: str) -> list:
    """Return the Facebook Pages the authorizing user manages.
    Each dict: {page_id, page_name, page_access_token}."""
    r = _req.get(f"{_GRAPH}/me/accounts",
                 params={"access_token": token, "fields": "id,name,access_token"},
                 timeout=15)
    if r.status_code != 200:
        print("⚠️ fb page discovery failed:", r.text[:300])
        return []
    out = []
    for p in r.json().get("data", []):
        out.append({
            "page_id": p["id"],
            "page_name": p.get("name", ""),
            "page_access_token": p.get("access_token", ""),
        })
    return out


def _save_fb_page_connection(tenant_id: int, page_id: str, page_name: str,
                              page_access_token: str, fb_user_id: str = "") -> bool:
    """Upsert the fb_pages row for a connected Facebook Page, and turn on
    live receiving for it."""
    subscribed = _subscribe_page(page_id, page_access_token)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO fb_pages
              (tenant_id, page_id, page_name, access_token, fb_user_id, active, subscribed)
            VALUES (%s, %s, %s, %s, %s, TRUE, %s)
            ON CONFLICT (page_id) DO UPDATE SET
              tenant_id    = EXCLUDED.tenant_id,
              page_name    = EXCLUDED.page_name,
              access_token = EXCLUDED.access_token,
              fb_user_id   = EXCLUDED.fb_user_id,
              active       = TRUE,
              subscribed   = EXCLUDED.subscribed
        """, (tenant_id, page_id, page_name, page_access_token, fb_user_id or None, subscribed))
        conn.commit()
        cur.close(); conn.close()
        return True
    except Exception as e:
        print("⚠️ _save_fb_page_connection error:", e)
        return False


def _get_fb_pages(tenant_id: int) -> list:
    """Return the tenant's active connected Pages, most recent first."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM fb_pages WHERE tenant_id=%s AND active=TRUE ORDER BY connected_at DESC",
                    (tenant_id,))
        rows = cur.fetchall() or []
        cur.close(); conn.close()
        return rows
    except Exception as e:
        print("⚠️ _get_fb_pages error:", e)
        return []


@facebook_bp.route("/messenger/connect", methods=["GET"])
@team_feature("channels.connect_messenger")
def messenger_connect():
    r = _require_login()
    if r: return r
    r2 = _require_team_permission("channels.connect_messenger")
    if r2: return r2
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    meta_app_id = os.getenv("META_APP_ID", "")
    return render_template(
        "portal/messenger_connect.html",
        customer=customer,
        meta_app_id=meta_app_id,
        embedded_enabled=bool(meta_app_id),
        pages=_get_fb_pages(tenant_id),
    )


@facebook_bp.route("/messenger/callback", methods=["POST"])
@team_feature("channels.connect_messenger")
def messenger_callback():
    """
    Receives the Facebook Login auth code from the connect page's JS.
    Exchanges it for a token, lists the Pages the user manages, and either
    saves the one Page directly or asks the front end to show a picker.
    """
    r = _require_login()
    if r:
        return jsonify({"error": "not_logged_in"}), 401
    if not _team_member_has_permission("channels.connect_messenger"):
        return jsonify({"error": "forbidden"}), 403

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "No auth code received. Please try again."}), 400

    app_id     = os.getenv("META_APP_ID", "")
    app_secret = os.getenv("META_APP_SECRET", "")
    if not app_id or not app_secret:
        return jsonify({"error": "Meta App credentials are not configured on this server. Contact support."}), 500

    token, _expires = _exchange_code_for_tokens(code, app_id, app_secret)
    if not token:
        return jsonify({"error": "Failed to exchange auth code for access token. The code may have expired — please try again."}), 400

    # Who authorized this — kept so the data-deletion callback above can
    # find and remove this connection if the owner ever requests it.
    fb_user_id = ""
    try:
        me = _req.get(f"{_GRAPH}/me", params={"access_token": token, "fields": "id"}, timeout=10)
        if me.status_code == 200:
            fb_user_id = me.json().get("id", "")
    except Exception:
        pass

    pages = _discover_pages(token)
    if not pages:
        return jsonify({"error": "No Facebook Pages found. Make sure you're an admin of the Page and try again."}), 400

    if len(pages) == 1:
        pg = pages[0]
        saved = _save_fb_page_connection(tenant_id, pg["page_id"], pg["page_name"],
                                          pg["page_access_token"], fb_user_id)
        if not saved:
            return jsonify({"error": "Could not save connection to database. Please try again."}), 500
        insert_audit_log(action="fb_page_connected", tenant_id=tenant_id,
                         details={"page_id": pg["page_id"], "page_name": pg["page_name"]})
        return jsonify({"status": "connected", "page_name": pg["page_name"]})

    # Multiple Pages — remember them server-side, keyed by page_id, so the
    # picker step only ever trusts an id the browser sends back, never a
    # page_access_token round-tripped through the client.
    session["fb_pending_pages"]      = {p["page_id"]: p for p in pages}
    session["fb_pending_fb_user_id"] = fb_user_id
    return jsonify({
        "status": "select_page",
        "page_options": [{"page_id": p["page_id"], "page_name": p["page_name"]} for p in pages],
    })


@facebook_bp.route("/messenger/complete", methods=["POST"])
@team_feature("channels.connect_messenger")
def messenger_complete():
    """Second step when the account manages 2+ Pages — saves the chosen one."""
    r = _require_login()
    if r: return r
    r2 = _require_team_permission("channels.connect_messenger")
    if r2: return r2

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    page_id    = (request.form.get("page_id") or "").strip()
    pending    = session.pop("fb_pending_pages", None) or {}
    fb_user_id = session.pop("fb_pending_fb_user_id", "")

    chosen = pending.get(page_id)
    if not chosen:
        flash("Session expired. Please connect Messenger again.", "danger")
        return redirect(url_for("facebook.messenger_connect"))

    saved = _save_fb_page_connection(tenant_id, chosen["page_id"], chosen["page_name"],
                                      chosen["page_access_token"], fb_user_id)
    if saved:
        insert_audit_log(action="fb_page_connected", tenant_id=tenant_id,
                         details={"page_id": chosen["page_id"], "page_name": chosen["page_name"]})
        flash(f"Facebook Page connected! ✅  {chosen['page_name']}", "success")
    else:
        flash("Could not save connection. Please try again.", "danger")

    return redirect(url_for("facebook.messenger_connect"))
