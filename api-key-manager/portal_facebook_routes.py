"""
portal_facebook_routes.py — Facebook platform compliance endpoints.

Facebook Data Deletion Callback (required for all Facebook apps):
  POST /facebook/deletion   — Facebook calls this when a user removes the app
  GET  /facebook/deletion/status — status page linked from the deletion confirmation

Signed-request verification follows:
  https://developers.facebook.com/docs/facebook-login/manually-build-a-login-flow
  #confirm-token

The app does not currently use Facebook Login, so there is no linked user data
to erase. The endpoint still verifies the request, records it, and returns the
required JSON so Facebook considers the obligation fulfilled. When Facebook Login
is added later, look up customers by fb_user_id and wipe them here.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets

from flask import Blueprint, request, jsonify, render_template_string
from db import get_db_connection

facebook_bp = Blueprint("facebook", __name__, url_prefix="/facebook")

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
        cur.execute(
            """
            INSERT INTO facebook_deletion_requests
                (fb_user_id, confirmation_code, status)
            VALUES (%s, %s, 'no_data')
            """,
            (fb_user_id, confirmation_code),
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
    <p>
      Since you connected via Facebook, PhiXtra does not store any personal
      data linked to your Facebook account. No further action is needed.
    </p>
    <p>Confirmation code: <span class="code">{{ code }}</span></p>
  {% else %}
    <p>No deletion request found for that confirmation code.</p>
    <p>If you believe this is an error, please contact
       <a href="mailto:hello@phixtra.com">hello@phixtra.com</a>.</p>
  {% endif %}
</body>
</html>"""


@facebook_bp.route("/deletion/status", methods=["GET"])
def deletion_status():
    """Status page that users land on after a data deletion request."""
    code = request.args.get("code", "").strip()
    valid = False

    if code:
        try:
            _ensure_deletion_table()
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM facebook_deletion_requests WHERE confirmation_code = %s",
                (code,),
            )
            valid = cur.fetchone() is not None
            cur.close()
            conn.close()
        except Exception as exc:
            print("⚠️  deletion_status lookup error:", exc)

    return render_template_string(_STATUS_PAGE, valid=valid, code=code), (200 if valid else 404)
