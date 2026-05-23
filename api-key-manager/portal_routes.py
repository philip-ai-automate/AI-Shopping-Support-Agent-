"""
portal_routes.py  — Phase 1 customer portal (portal.phixtra.com)
Extends the existing Flask app. db.py, app.py, invoice_pdf.py, portal_utils.py are UNCHANGED.
"""
import os, secrets, string, json as _json
from datetime import datetime, timedelta

import bcrypt
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, send_file, jsonify)

from db import get_db_connection, insert_audit_log
from portal_utils import (
    hash_password, verify_password, make_token, utc_now_naive,
    next_invoice_number, credits_to_tokens, tokens_to_credits,
    calc_vat, money_fmt, send_email,
)
from invoice_pdf import generate_invoice_pdf

try:
    import stripe
except Exception:
    stripe = None

portal_bp = Blueprint("portal", __name__)
BRAND = "#030C18"
TRIAL_DAYS = 14          # mirrors app.py — keep in sync

# Base URL used in all email links.
# Set PORTAL_BASE_URL in your .env file:
#   production : PORTAL_BASE_URL=https://portal.phixtra.com
#   staging    : PORTAL_BASE_URL=https://stagingportal.phixtra.com
# Defaults to production so existing deployments are unaffected.
_PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")


# ── Key generation (mirrors app.py exactly — same alphabet, same length) ──────
def _generate_api_key_and_hash(length: int = 28):
    alphabet = string.ascii_letters + string.digits
    plain_key = ''.join(secrets.choice(alphabet) for _ in range(length))
    hashed_key = bcrypt.hashpw(plain_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    return plain_key, hashed_key


# ── Session helpers ────────────────────────────────────────────────────────────
def _logged_in() -> bool:
    return session.get("portal_logged_in") is True

def _customer_id():
    if session.get("impersonate_customer_id"):
        return int(session["impersonate_customer_id"])
    cid = session.get("customer_id")
    return int(cid) if cid else None

def _require_login():
    if not _logged_in() or not _customer_id():
        return redirect(url_for("portal.login"))
    return None


# ── DB helpers ─────────────────────────────────────────────────────────────────
def _get_customer(customer_id: int):
    """Fetch the customer row joined with its tenant.
    Returns None (does NOT raise) if the row is not found or the DB is unavailable."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT c.*, t.name AS tenant_name, t.domain AS tenant_domain
            FROM customers c
            JOIN tenants t ON t.id = c.tenant_id
            WHERE c.id=%s""", (customer_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return row
    except Exception as e:
        print("⚠️ _get_customer error:", e)
        return None

def _get_tenant_balance_tokens(tenant_id: int) -> int:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT token_balance FROM tenant_balances WHERE tenant_id=%s", (tenant_id,))
    row = cur.fetchone() or {}
    cur.close(); conn.close()
    return int(row.get("token_balance") or 0)

def _ensure_tenant_balance_row(tenant_id: int):
    conn = get_db_connection()
    cur = conn.cursor(buffered=True)
    cur.execute("INSERT IGNORE INTO tenant_balances (tenant_id, token_balance) VALUES (%s, 0)", (tenant_id,))
    conn.commit()
    cur.close(); conn.close()

def _get_api_keys(tenant_id: int):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("""
        SELECT id, website, key_type, is_active, token_limit, tokens_used,
               trial_activated_at, trial_expires_at, created_at, api_key_plain
        FROM api_keys WHERE tenant_id=%s ORDER BY created_at DESC""", (tenant_id,))
    rows = cur.fetchall() or []
    cur.close(); conn.close()
    return rows

def _usage_summary(tenant_id: int, days: int = 30):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("""
        SELECT COALESCE(SUM(used_tokens),0) AS tokens
        FROM usage_events WHERE tenant_id=%s AND created_at >= UTC_DATE()""", (tenant_id,))
    today_tokens = int((cur.fetchone() or {}).get("tokens") or 0)
    cur.execute("""
        SELECT COALESCE(SUM(used_tokens),0) AS tokens
        FROM usage_events WHERE tenant_id=%s AND created_at >= (UTC_TIMESTAMP() - INTERVAL %s DAY)""",
        (tenant_id, days))
    range_tokens = int((cur.fetchone() or {}).get("tokens") or 0)
    cur.execute("""
        SELECT COUNT(DISTINCT session_id) AS c
        FROM usage_events WHERE tenant_id=%s AND created_at >= (UTC_TIMESTAMP() - INTERVAL 30 DAY)""",
        (tenant_id,))
    sessions_30d = int((cur.fetchone() or {}).get("c") or 0)
    cur.close(); conn.close()
    return {"today_tokens": today_tokens, "range_tokens": range_tokens, "sessions_30d": sessions_30d}

def _usage_timeseries(tenant_id: int, days: int = 30):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("""
        SELECT DATE(created_at) AS d, COALESCE(SUM(used_tokens),0) AS tokens
        FROM usage_events
        WHERE tenant_id=%s AND created_at >= (UTC_TIMESTAMP() - INTERVAL %s DAY)
        GROUP BY DATE(created_at) ORDER BY d ASC""", (tenant_id, days))
    rows = cur.fetchall() or []
    cur.close(); conn.close()
    return rows

def _onboarding_status(tenant_id: int, customer_id: int):
    """Returns dict of completed booleans for each onboarding step.
    Wrapped in try/except so a missing DB column NEVER causes a 500 —
    the page loads and shows zeroed-out status instead of crashing."""

    # Safe defaults — returned if anything fails
    _safe = {
        "account_verified":           True,
        "key_active":                 False,
        "ai_plugin_confirmed":        False,
        "export_plugin_confirmed":    False,
        "sync_configured_confirmed":  False,
        "synced":                     False,
        "kb_configured":              False,
        "ai_live":                    False,
        "wizard_dismissed":           False,
        "complete":                   False,
    }

    conn = None
    cur  = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)

        # Step 2: has any active api key
        cur.execute("SELECT COUNT(*) AS c FROM api_keys WHERE tenant_id=%s AND is_active=1", (tenant_id,))
        has_key = int((cur.fetchone() or {}).get("c") or 0) > 0

        # Step 6: full sync completed — last_full_sync_at is stamped by phixtra-data-sync
        # when the WordPress plugin finishes pushing all batches to Azure AI Search.
        # Step 7: admin has completed KB setup — azure_search_index is set on tenant.
        # Both columns live on the tenants row — one query, no extra round-trip.
        # Wrapped in its OWN try/except because last_full_sync_at may not exist yet on
        # databases created before the latest migration ran.
        kb_configured = False
        sync_done     = False
        try:
            cur.execute(
                "SELECT azure_search_index, last_full_sync_at FROM tenants WHERE id=%s",
                (tenant_id,)
            )
            t_row = cur.fetchone() or {}
            kb_configured = bool((t_row.get("azure_search_index") or "").strip())
            sync_done     = bool(t_row.get("last_full_sync_at"))
        except Exception as e:
            print("⚠️ _onboarding_status: tenants query failed (column may be missing):", e)
            kb_configured = False
            sync_done     = False

        # Steps 3,4,5 — plugin install / configure confirmations
        dismissed              = False
        ai_plugin_confirmed    = False
        export_plugin_confirmed = False
        sync_configured_confirmed = False
        try:
            cur.execute("""SELECT wizard_dismissed, ai_plugin_confirmed,
                                  export_plugin_confirmed, sync_configured_confirmed
                           FROM onboarding_state WHERE customer_id=%s""", (customer_id,))
            row = cur.fetchone() or {}
            dismissed               = bool(int(row.get("wizard_dismissed") or 0))
            ai_plugin_confirmed     = bool(int(row.get("ai_plugin_confirmed") or 0))
            export_plugin_confirmed = bool(int(row.get("export_plugin_confirmed") or 0))
            sync_configured_confirmed = bool(int(row.get("sync_configured_confirmed") or 0))
        except Exception as e:
            print("⚠️ _onboarding_status: onboarding_state query failed:", e)

        all_done = (has_key and ai_plugin_confirmed and export_plugin_confirmed
                    and sync_configured_confirmed and sync_done and kb_configured)

        return {
            "account_verified":           True,
            "key_active":                 has_key,
            "ai_plugin_confirmed":        ai_plugin_confirmed,
            "export_plugin_confirmed":    export_plugin_confirmed,
            "sync_configured_confirmed":  sync_configured_confirmed,
            "synced":                     sync_done,
            "kb_configured":              kb_configured,
            "ai_live":                    sync_done and kb_configured,
            "wizard_dismissed":           dismissed,
            "complete":                   all_done,
        }

    except Exception as e:
        print("⚠️ _onboarding_status: unexpected error:", e)
        return _safe
    finally:
        try:
            if cur:  cur.close()
        except Exception:
            pass
        try:
            if conn: conn.close()
        except Exception:
            pass

def _stripe_ok() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY")) and stripe is not None


def _get_or_create_stripe_customer(customer: dict) -> str | None:
    """
    Stage 2 — Stripe Customer identity.

    Returns the Stripe Customer ID (cus_xxx) for this customer, creating one
    in Stripe if it does not exist yet.  The ID is persisted to
    customers.stripe_customer_id so it is only created once per customer.

    Returns None (never raises) if Stripe is not configured or the API call
    fails — callers fall back to the old customer_email= behaviour so the
    existing top-up flow keeps working even if this step fails.
    """
    if not _stripe_ok():
        return None

    # Already have a Stripe Customer ID — return it immediately.
    existing_id = (customer.get("stripe_customer_id") or "").strip()
    if existing_id:
        return existing_id

    # No ID yet — create a Stripe Customer and save it.
    try:
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
        cus = stripe.Customer.create(
            email=customer["email"],
            name=(
                f"{customer.get('first_name','').strip()} "
                f"{customer.get('last_name','').strip()}"
            ).strip() or None,
            metadata={"phixtra_customer_id": str(customer["id"])},
        )
        stripe_cus_id = cus["id"]

        # Persist so we never create a duplicate.
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute(
            "UPDATE customers SET stripe_customer_id=%s WHERE id=%s",
            (stripe_cus_id, int(customer["id"])),
        )
        conn.commit()
        cur.close(); conn.close()

        return stripe_cus_id

    except Exception as e:
        print("⚠️ _get_or_create_stripe_customer failed:", e)
        return None


# ── EMAIL helpers ──────────────────────────────────────────────────────────────
def _greeting(customer) -> str:
    fn = (customer.get("first_name") or "").strip()
    return fn if fn else "there"

def _send_verify_email(email: str, token: str, greeting: str) -> bool:
    # Use the actual server URL from the current request so staging always
    # sends staging links and production always sends production links.
    # Falls back to _PORTAL_BASE_URL if called outside a request context.
    try:
        from flask import request as _req
        base = _req.host_url.rstrip("/")
    except Exception:
        base = _PORTAL_BASE_URL
    link = f"{base}/verify?token={token}"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px">
      <h2 style="color:{BRAND}">Verify your email</h2>
      <p>Hi {greeting},</p>
      <p>Welcome to PhiXtra. Click below to verify your email and activate your account.</p>
      <p><a href="{link}" style="background:{BRAND};color:#fff;padding:10px 18px;border-radius:12px;text-decoration:none;display:inline-block">Verify Email</a></p>
      <p style="color:#888;font-size:12px">If you didn't create this account, ignore this email.</p>
    </div>"""
    return send_email(email, "Verify your PhiXtra email", html, text_body=f"Verify: {link}")

def _send_reset_email(email: str, token: str, greeting: str) -> bool:
    try:
        from flask import request as _req
        base = _req.host_url.rstrip("/")
    except Exception:
        base = _PORTAL_BASE_URL
    link = f"{base}/reset?token={token}"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px">
      <h2 style="color:{BRAND}">Reset your password</h2>
      <p>Hi {greeting},</p>
      <p>Click below to reset your password. This link expires in 2 hours.</p>
      <p><a href="{link}" style="background:{BRAND};color:#fff;padding:10px 18px;border-radius:12px;text-decoration:none;display:inline-block">Reset password</a></p>
      <p style="color:#888;font-size:12px">If you didn't request this, ignore this email.</p>
    </div>"""
    return send_email(email, "Reset your PhiXtra password", html, text_body=f"Reset: {link}")


def _send_admin_new_signup_email(customer_name: str, customer_email: str,
                                  domain: str, ai_instructions: str,
                                  ai_requirements: str):
    """Notify admin (support@phixtra.com) of a new trial sign-up so they
    can complete the KB setup: set azure_search_index, azure_semantic_config,
    and review/refine the system_prompt."""
    admin_portal_link = f"{_PORTAL_BASE_URL}/admin/customers"
    req_block = f"<p><strong>Other requirements:</strong></p><p style='white-space:pre-wrap'>{ai_requirements}</p>" if ai_requirements else ""
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px">
      <h2 style="color:{BRAND}">&#128226; New PhiXtra Trial Sign-up</h2>
      <table style="border-collapse:collapse;width:100%;margin-bottom:16px">
        <tr><td style="padding:6px 10px;font-weight:700;background:#f3f4f6;border:1px solid #e5e7eb;width:160px">Name</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{customer_name}</td></tr>
        <tr><td style="padding:6px 10px;font-weight:700;background:#f3f4f6;border:1px solid #e5e7eb">Email</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{customer_email}</td></tr>
        <tr><td style="padding:6px 10px;font-weight:700;background:#f3f4f6;border:1px solid #e5e7eb">Store domain</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{domain}</td></tr>
      </table>
      <p><strong>AI instructions (system_prompt):</strong></p>
      <p style="background:#f3f4f6;padding:12px;border-radius:8px;white-space:pre-wrap">{ai_instructions}</p>
      {req_block}
      <p style="margin-top:16px;color:#6b7280;font-size:13px">
        Action required: log in to the admin portal, find this customer, and set
        <strong>azure_search_index</strong> and <strong>azure_semantic_config</strong>
        in the tenants table to complete their knowledge base setup.
        Then send them the confirmation email.
      </p>
      <p style="margin-top:12px">
        <a href="{admin_portal_link}" style="background:{BRAND};color:#fff;padding:10px 18px;border-radius:12px;text-decoration:none;display:inline-block">
          Open Admin Portal
        </a>
      </p>
    </div>"""
    send_email(
        "support@phixtra.com",
        f"New trial sign-up: {customer_name} ({domain})",
        html,
        text_body=f"New trial: {customer_name} <{customer_email}> domain={domain}\n\nAI instructions:\n{ai_instructions}\n\nOther requirements:\n{ai_requirements}"
    )
def _send_welcome_trial_email(
    email: str,
    first_name: str,
    website: str,
    trial_expires_at,
) -> None:
    """Send the Day-0 welcome email when a trial account is created."""
    greeting = first_name.strip() if first_name and first_name.strip() else "there"
    exp_str = (
        trial_expires_at.strftime("%d %B %Y")
        if hasattr(trial_expires_at, "strftime")
        else str(trial_expires_at)
    )
    portal_link    = _PORTAL_BASE_URL
    upgrade_link   = "https://phixtra.com/subscription-plans/"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
      <h2 style="color:#030C18">Your PhiXtra 14-day free trial is now live 🎉</h2>
      <p>Hi {greeting},</p>
      <p>Your trial AI assistant for <b>{website}</b> has been created and is ready to go.</p>
      <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
        <tr>
          <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700;width:130px">Store</td>
          <td style="padding:8px 12px;border:1px solid #e5e7eb">{website}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700">Trial ends</td>
          <td style="padding:8px 12px;border:1px solid #e5e7eb">{exp_str}</td>
        </tr>
      </table>
      <p style="margin:0 0 6px"><b>What to do next:</b></p>
      <ol style="margin:0 0 20px;padding-left:20px;line-height:1.9">
        <li>Verify your email (click the link in the separate verification email)</li>
        <li>Log in to your portal and follow the setup guide</li>
        <li>Install the PhiXtra plugins on your store</li>
        <li>Watch your AI assistant go live</li>
        <li>Plan your upgrade before the trial ends</li>
      </ol>
      <p style="margin-bottom:20px">
        <a href="{portal_link}"
           style="display:inline-block;background:#030C18;color:#fff;padding:12px 22px;
                  border-radius:12px;text-decoration:none;font-weight:700;font-size:15px;margin-right:10px">
          Go to Portal
        </a>
        <a href="{upgrade_link}"
           style="display:inline-block;background:#fff;color:#030C18;padding:12px 22px;
                  border-radius:12px;text-decoration:none;font-weight:700;font-size:15px;
                  border:2px solid #030C18">
          View Plans
        </a>
      </p>
      <p style="color:#6b7280;font-size:13px">
        We'll send you a reminder as your trial end date approaches.<br>
        Questions? Contact <a href="mailto:support@phixtra.com" style="color:#030C18">support@phixtra.com</a>
      </p>
    </div>"""
    send_email(
        email,
        "Your PhiXtra 14-day free trial is now live 🎉",
        html,
        text_body=(
            f"Hi {greeting},\n\n"
            f"Your PhiXtra trial for {website} is now active. Trial ends: {exp_str}\n\n"
            f"Log in: {portal_link}\nView plans: {upgrade_link}"
        ),
    )




@portal_bp.route("/", methods=["GET"])
def home():
    if _logged_in() and _customer_id():
        return redirect(url_for("portal.dashboard"))
    return render_template("portal/home.html")



# Bot protection
import time as _time
from collections import defaultdict as _defaultdict
_reg_attempts = _defaultdict(list)
_REG_MAX = 3
_REG_WINDOW = 3600

def _reg_rate_ok(ip):
    now = _time.time()
    attempts = [t for t in _reg_attempts[ip] if now - t < _REG_WINDOW]
    _reg_attempts[ip] = attempts
    if len(attempts) >= _REG_MAX:
        return False
    _reg_attempts[ip].append(now)
    return True

@portal_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("portal/register.html")

    if request.form.get("website"): return redirect(url_for("portal.register"))
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    if not _reg_rate_ok(client_ip):
        flash("Too many attempts. Try later.", "danger")
        return redirect(url_for("portal.register"))

    # ── reCAPTCHA verification ──────────────────────────────────────────────
    import requests as _req
    recaptcha_response = request.form.get("g-recaptcha-response", "")
    if not recaptcha_response:
        flash("Please complete the reCAPTCHA check.", "danger")
        return redirect(url_for("portal.register"))
    try:
        rv = _req.post("https://www.google.com/recaptcha/api/siteverify",
                       data={"secret": "123456789012345", "response": recaptcha_response},
                       timeout=5)
        if not rv.json().get("success"):
            flash("reCAPTCHA failed. Please try again.", "danger")
            return redirect(url_for("portal.register"))
    except Exception:
        pass  # If Google is unreachable, allow through
    # ── end reCAPTCHA ───────────────────────────────────────────────────────
    first_name      = (request.form.get("first_name")      or "").strip()
    last_name       = (request.form.get("last_name")       or "").strip()
    email           = (request.form.get("email")           or "").strip().lower()
    password        = (request.form.get("password")        or "").strip()
    phone_number    = (request.form.get("phone_number")    or "").strip()
    tenant_domain   = (request.form.get("tenant_domain")   or "").strip().lower()
    ai_instructions = (request.form.get("ai_instructions") or "").strip()
    ai_requirements = (request.form.get("ai_requirements") or "").strip()

    if not first_name or not last_name or not email or not password or not tenant_domain:
        flash("First name, last name, email, password and store domain are all required.", "danger")
        return redirect(url_for("portal.register"))

    if not ai_instructions:
        flash("Please tell us what you want the AI agent to do — this field is required.", "danger")
        return redirect(url_for("portal.register"))

    if len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return redirect(url_for("portal.register"))

    # Strip https:// or http:// if user pastes full URL
    tenant_domain = tenant_domain.replace("https://","").replace("http://","").rstrip("/")

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)

    # ── Find or auto-create the tenant for this domain ─────────────────────
    # Customers arrive from phixtra.com and register themselves — no admin
    # pre-setup is required. If no tenant exists for this domain we create
    # one automatically, exactly the same way app.py does it.
    cur.execute("SELECT id, name FROM tenants WHERE domain=%s", (tenant_domain,))
    tenant = cur.fetchone()
    if not tenant:
        tenant_name = f"{first_name} {last_name}".strip() or tenant_domain
        # All trial sign-ups get all plugin features enabled automatically,
        # including Chat Archive Unlimited so they can experience the full product.
        trial_features = _json.dumps({
            "product_recommendation":    True,
            "related_products":          True,
            "cart_recovery":             True,
            "verified_specs_web_lookup": True,
            "chat_archive_unlimited":    True,
        })
        # Store AI instructions + other requirements as the initial system_prompt.
        # Admin will review and refine these before completing the KB setup.
        system_prompt_text = ai_instructions
        if ai_requirements:
            system_prompt_text += f"\n\n[Additional requirements]\n{ai_requirements}"
        cur2 = conn.cursor(buffered=True)
        cur2.execute(
            "INSERT INTO tenants (name, domain, status, features, system_prompt) VALUES (%s, %s, 'pending', %s, %s)",
            (tenant_name, tenant_domain, trial_features, system_prompt_text)
        )
        new_tenant_id = cur2.lastrowid
        conn.commit()
        cur2.close()
        tenant = {"id": new_tenant_id, "name": tenant_name}
        insert_audit_log(action="tenant_auto_created",
                         tenant_id=new_tenant_id,
                         website=tenant_domain,
                         details={"created_by": email, "name": tenant_name,
                                  "features": {"product_recommendation": True, "related_products": True,
                                               "cart_recovery": True, "verified_specs_web_lookup": True,
                                               "chat_archive_unlimited": True}})

        # Notify admin so they can complete the KB setup (azure_search_index etc.)
        _send_admin_new_signup_email(
            customer_name=f"{first_name} {last_name}".strip(),
            customer_email=email,
            domain=tenant_domain,
            ai_instructions=ai_instructions,
            ai_requirements=ai_requirements,
        )

    verify_token = make_token(24)
    pw_hash      = hash_password(password)

    # ── Create the customer account ────────────────────────────────────────
    try:
        cur2 = conn.cursor(buffered=True)
        cur2.execute("""
            INSERT INTO customers
                (tenant_id, first_name, last_name, email, password_hash,
                 phone_number, email_verified, verify_token)
            VALUES (%s, %s, %s, %s, %s, %s, 0, %s)""",
            (int(tenant["id"]), first_name, last_name, email,
             pw_hash, phone_number or None, verify_token))
        conn.commit()
        cur2.close()
    except Exception:
        conn.rollback()
        cur.close(); conn.close()
        flash("An account with that email already exists. Please log in.", "warning")
        return redirect(url_for("portal.login"))

    # ── Auto-create a 14-day trial API key ────────────────────────────────
    # Key is active immediately. token_limit=50000 = 50 credits shown to customer.
    plain_key, hashed_key = _generate_api_key_and_hash()
    last4 = plain_key[-4:]
    trial_activated_at = datetime.utcnow()
    trial_expires_at   = trial_activated_at + timedelta(days=TRIAL_DAYS)
    TRIAL_TOKEN_LIMIT  = 250000  # 50 credits (1 credit = 5,000 tokens)

    cur3 = conn.cursor(buffered=True)
    cur3.execute("""
        INSERT INTO api_keys
            (tenant_id, api_key_hash, api_key_plain, is_active, website, key_type,
             trial_activated_at, trial_expires_at, token_limit, tokens_used)
        VALUES (%s, %s, %s, 1, %s, 'trial', %s, %s, %s, 0)""",
        (int(tenant["id"]), hashed_key, plain_key, tenant_domain,
         trial_activated_at, trial_expires_at, TRIAL_TOKEN_LIMIT))
    api_key_id = cur3.lastrowid
    conn.commit()
    cur3.close()

    cur.close(); conn.close()

    _ensure_tenant_balance_row(int(tenant["id"]))

    # Store plain key in session — transferred through email verification
    # and into the login session so it can be shown once on the API keys page.
    session["pending_plain_key"] = plain_key

    insert_audit_log(
        admin_username=f"self-register:{email}",
        action="create_key",
        tenant_id=int(tenant["id"]),
        website=tenant_domain,
        key_type="trial",
        api_key_id=api_key_id,
        api_key_last4=last4,
        api_key_plain=plain_key,
        details={"created_from": "self-register"},
    )
    insert_audit_log(action="customer_registered", tenant_id=int(tenant["id"]),
                     website=tenant_domain, details={"email": email, "first_name": first_name})

    email_sent = _send_verify_email(email, verify_token, first_name)

    # Send Day-0 welcome email (separate from verification email)
    try:
        _send_welcome_trial_email(
            email=email,
            first_name=first_name,
            website=tenant_domain,
            trial_expires_at=trial_expires_at,
        )
    except Exception as _we:
        print("⚠️ welcome trial email failed:", _we)

    if email_sent:
        flash("Account created! ✅ Check your email and click the verification link to activate your account.", "success")
    else:
        # SMTP failed — the account is created and the token is stored.
        # The customer can request the email again from the login page.
        flash(
            "Account created! However we could not send the verification email right now — "
            "please use the "
            "<a href=\"" + url_for("portal.resend_verify") + "\" style=\"text-decoration:underline\">Resend verification email</a>"
            " link on this page.",
            "warning"
        )
    return redirect(url_for("portal.login"))


@portal_bp.route("/verify", methods=["GET"])
def verify_email():
    token = (request.args.get("token") or "").strip()
    if not token:
        flash("Invalid verification link.", "danger")
        return redirect(url_for("portal.login"))

    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT id, email, email_verified FROM customers WHERE verify_token=%s", (token,))
        c = cur.fetchone()
        if not c:
            cur.close(); conn.close()
            print(f"[VERIFY] Token not found or already used: {token[:8]}...")
            flash("Verification link is invalid or has already been used.", "danger")
            return redirect(url_for("portal.login"))

        customer_id = int(c["id"])
        print(f"[VERIFY] Found customer id={customer_id} email={c.get('email')} already_verified={c.get('email_verified')}")

        cur2 = conn.cursor(buffered=True)
        cur2.execute("UPDATE customers SET email_verified=1, verify_token=NULL WHERE id=%s", (customer_id,))
        conn.cursor(buffered=True).execute("UPDATE tenants SET status='active' WHERE id=(SELECT tenant_id FROM customers WHERE id=%s)", (customer_id,))
        rows_affected = cur2.rowcount
        conn.commit()
        print(f"[VERIFY] UPDATE rows_affected={rows_affected} for customer id={customer_id}")

        # Confirm the update actually took effect
        cur3 = conn.cursor(dictionary=True, buffered=True)
        cur3.execute("SELECT email_verified FROM customers WHERE id=%s", (customer_id,))
        confirm = cur3.fetchone()
        print(f"[VERIFY] Confirmation SELECT: email_verified={confirm.get('email_verified') if confirm else 'NO ROW FOUND'}")
        cur3.close()
        cur2.close(); cur.close(); conn.close()

    except Exception as e:
        print(f"[VERIFY] ERROR during email verification: {e}")
        flash("An error occurred during verification. Please try again or contact support.", "danger")
        return redirect(url_for("portal.login"))

    # If the plain key was stored during registration (same browser session),
    # keep it alive so it can be shown once after the customer logs in.
    pending_key = session.pop("pending_plain_key", None)
    if pending_key:
        session["new_plain_key"] = pending_key

    flash("Email verified ✅  You can now log in.", "success")
    return redirect(url_for("portal.login"))


@portal_bp.route("/resend-verify", methods=["GET", "POST"])
def resend_verify():
    """Let customers who never received (or lost) their verification email request a new one."""
    if request.method == "GET":
        return render_template("portal/resend_verify.html")

    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("Please enter your email address.", "danger")
        return redirect(url_for("portal.resend_verify"))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT id, first_name, email_verified, verify_token FROM customers WHERE email=%s", (email,))
    c = cur.fetchone()

    if not c:
        # Don't reveal whether the email is registered (security best practice)
        cur.close(); conn.close()
        flash("If that email is registered and unverified, a new link is on its way.", "success")
        return redirect(url_for("portal.login"))

    if int(c.get("email_verified") or 0):
        cur.close(); conn.close()
        flash("That email is already verified. Please log in.", "info")
        return redirect(url_for("portal.login"))

    # Re-generate a fresh token so old links (from previous emails) stop working
    new_token = make_token(24)
    cur2 = conn.cursor(buffered=True)
    cur2.execute("UPDATE customers SET verify_token=%s WHERE id=%s", (new_token, int(c["id"])))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    greeting = (c.get("first_name") or "").strip() or "there"
    email_sent = _send_verify_email(email, new_token, greeting)

    if email_sent:
        flash("Verification email sent! ✅ Please check your inbox (and spam folder).", "success")
    else:
        flash(
            "We couldn\'t send the email right now — our mail server may be temporarily unavailable. "
            "Please try again in a few minutes or contact support@phixtra.com.",
            "danger"
        )
    return redirect(url_for("portal.login"))


@portal_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("portal/login.html")

    email    = (request.form.get("email")    or "").strip().lower()
    password = (request.form.get("password") or "").strip()

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT * FROM customers WHERE email=%s ORDER BY email_verified DESC, id DESC LIMIT 1", (email,))
    c = cur.fetchone()
    cur.close(); conn.close()

    if not c or not verify_password(password, c.get("password_hash") or ""):
        flash("Incorrect email or password.", "danger")
        return redirect(url_for("portal.login"))

    if not int(c.get("is_active") or 0):
        flash("Your account has been disabled. Contact support.", "danger")
        return redirect(url_for("portal.login"))

    if not int(c.get("email_verified") or 0):
        flash("Please verify your email before logging in. Check your inbox.", "warning")
        return redirect(url_for("portal.login"))

    # Rescue any plain key saved during the registration/verify flow
    # BEFORE session.clear() wipes it.
    pending_key = session.pop("new_plain_key", None)

    session.clear()
    session["portal_logged_in"] = True
    session["customer_id"]      = int(c["id"])

    # If a plain key was saved during email verification (e.g. self-registration
    # trial flow), keep it so it can be shown once on the API keys page.
    if pending_key:
        session["new_plain_key"] = pending_key

    return redirect(url_for("portal.dashboard"))


@portal_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("portal.home"))


@portal_bp.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("portal/forgot.html")

    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("Enter your email address.", "danger")
        return redirect(url_for("portal.forgot_password"))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT id, first_name FROM customers WHERE email=%s", (email,))
    c = cur.fetchone()

    if c:
        token   = make_token(24)
        expires = utc_now_naive() + timedelta(hours=2)
        cur2 = conn.cursor(buffered=True)
        cur2.execute("UPDATE customers SET reset_token=%s, reset_expires_at=%s WHERE id=%s",
                     (token, expires, int(c["id"])))
        conn.commit()
        cur2.close()
        _send_reset_email(email, token, (c.get("first_name") or "there"))

    cur.close(); conn.close()
    flash("If that email is registered, a reset link is on its way.", "success")
    return redirect(url_for("portal.login"))


@portal_bp.route("/reset", methods=["GET", "POST"])
def reset_password():
    token = (request.args.get("token") or request.form.get("token") or "").strip()
    if request.method == "GET":
        return render_template("portal/reset.html", token=token)

    password = (request.form.get("password") or "").strip()
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return redirect(url_for("portal.reset_password", token=token))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT id, reset_expires_at FROM customers WHERE reset_token=%s", (token,))
    c = cur.fetchone()
    if not c:
        cur.close(); conn.close()
        flash("Reset link is invalid or has expired.", "danger")
        return redirect(url_for("portal.login"))

    exp = c.get("reset_expires_at")
    if not exp or utc_now_naive() > exp:
        cur2 = conn.cursor(buffered=True)
        cur2.execute("UPDATE customers SET reset_token=NULL, reset_expires_at=NULL WHERE id=%s", (int(c["id"]),))
        conn.commit()
        cur2.close(); cur.close(); conn.close()
        flash("Reset link expired. Request a new one.", "warning")
        return redirect(url_for("portal.forgot_password"))

    cur2 = conn.cursor(buffered=True)
    cur2.execute("UPDATE customers SET password_hash=%s, reset_token=NULL, reset_expires_at=NULL WHERE id=%s",
                 (hash_password(password), int(c["id"])))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    flash("Password updated ✅  Please log in.", "success")
    return redirect(url_for("portal.login"))


# ══════════════════════════════════════════════════════════════════════════════
# HUMAN HANDOFF — helpers and mark-handled route
# ══════════════════════════════════════════════════════════════════════════════

def _get_pending_handoffs(tenant_id: int) -> list:
    """Fetch all pending handoff requests for the dashboard panel.
    Returns an empty list on any error — never crashes the dashboard."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        # Select visitor_name and visitor_email too — filled in when the visitor
        # submits the in-widget contact form after a handoff is triggered.
        # Use COALESCE so the query still works if the columns don't exist yet.
        try:
            cur.execute("""
                SELECT id, session_id, whatsapp_number, visitor_message, created_at,
                       visitor_name, visitor_email
                FROM handoff_requests
                WHERE tenant_id = %s AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 50
            """, (tenant_id,))
        except Exception:
            # Columns not yet migrated — fall back to original query
            cur.execute("""
                SELECT id, session_id, whatsapp_number, visitor_message, created_at
                FROM handoff_requests
                WHERE tenant_id = %s AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 50
            """, (tenant_id,))
        rows = cur.fetchall() or []
        cur.close(); conn.close()
        return rows
    except Exception as e:
        print("⚠️ _get_pending_handoffs error:", e)
        return []


@portal_bp.route("/handoff/<int:handoff_id>/handled", methods=["POST"])
def handoff_mark_handled(handoff_id: int):
    """Mark a handoff request as handled. Only the owning tenant can do this."""
    r = _require_login()
    if r: return r

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        # Security: only update rows belonging to this tenant
        cur.execute("""
            UPDATE handoff_requests
            SET status = 'handled', handled_at = UTC_TIMESTAMP()
            WHERE id = %s AND tenant_id = %s AND status = 'pending'
        """, (handoff_id, tenant_id))
        conn.commit()
        cur.close(); conn.close()
        flash("Marked as handled ✅", "success")
    except Exception as e:
        print("⚠️ handoff_mark_handled error:", e)
        flash("Could not update status. Please try again.", "danger")

    return redirect(url_for("portal.dashboard"))


# ══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATED — DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@portal_bp.route("/dashboard")
def dashboard():
    r = _require_login()
    if r: return r

    customer = _get_customer(_customer_id())
    if not customer:
        session.clear()
        flash("Your account could not be loaded. Please log in again or contact support@phixtra.com.", "danger")
        return redirect(url_for("portal.login"))
    tenant_id = int(customer["tenant_id"])
    _ensure_tenant_balance_row(tenant_id)

    balance_tokens  = _get_tenant_balance_tokens(tenant_id)
    balance_credits = tokens_to_credits(balance_tokens)
    summary         = _usage_summary(tenant_id, days=30)
    series          = _usage_timeseries(tenant_id, days=30)
    ob              = _onboarding_status(tenant_id, int(customer["id"]))
    keys            = _get_api_keys(tenant_id)

    chart_points = [{"d": str(r["d"]), "credits": tokens_to_credits(int(r["tokens"]))} for r in series]

    # ── Pending handoff requests for the "Needs Attention" panel ─────────────
    handoffs = _get_pending_handoffs(tenant_id)

    # ── Trial status for the banner ────────────────────────────────────────
    trial_info = {
        "is_trial": False,
        "days_left": None,
        "expired":   False,
        "website":   None,
    }
    _now = datetime.utcnow()
    for k in keys:
        if k.get("key_type") == "trial":
            trial_info["is_trial"] = True
            trial_info["website"]  = k.get("website", "")
            exp = k.get("trial_expires_at")
            if k.get("is_active") and exp:
                diff = exp - _now
                trial_info["days_left"] = max(0, diff.days)
                trial_info["expired"]   = False
            else:
                trial_info["days_left"] = 0
                trial_info["expired"]   = True
            break

    return render_template(
        "portal/dashboard.html",
        customer        = customer,
        balance_credits = balance_credits,
        today_credits   = tokens_to_credits(summary["today_tokens"]),
        month_credits   = tokens_to_credits(summary["range_tokens"]),
        sessions_30d    = summary["sessions_30d"],
        chart_points    = chart_points,
        onboarding      = ob,
        keys            = keys,
        handoffs        = handoffs,
        trial_info      = trial_info,
    )


# ── Dismiss onboarding wizard ──────────────────────────────────────────────────
@portal_bp.route("/onboarding/dismiss", methods=["POST"])
def onboarding_dismiss():
    r = _require_login()
    if r: return r
    cid = _customer_id()
    conn = get_db_connection()
    cur = conn.cursor(buffered=True)
    cur.execute("""
        INSERT INTO onboarding_state (customer_id, wizard_dismissed) VALUES (%s, 1)
        ON DUPLICATE KEY UPDATE wizard_dismissed=1""", (cid,))
    conn.commit()
    cur.close(); conn.close()
    return redirect(url_for("portal.dashboard"))


@portal_bp.route("/onboarding/confirm-step", methods=["POST"])
def onboarding_confirm_step():
    """Customer manually confirms a setup step is done."""
    r = _require_login()
    if r: return r
    cid  = _customer_id()
    step = (request.form.get("step") or "").strip()

    # Map step names to column names — only allow known steps
    allowed = {
        "ai_plugin":    "ai_plugin_confirmed",
        "export_plugin":"export_plugin_confirmed",
        "sync_config":  "sync_configured_confirmed",
    }
    col = allowed.get(step)
    if not col:
        flash("Unknown step.", "danger")
        return redirect(url_for("portal.onboarding"))

    conn = get_db_connection()
    cur = conn.cursor(buffered=True)
    cur.execute(f"""
        INSERT INTO onboarding_state (customer_id, {col}) VALUES (%s, 1)
        ON DUPLICATE KEY UPDATE {col}=1""", (cid,))
    conn.commit()
    cur.close(); conn.close()

    flash("Step marked as done ✅", "success")
    return redirect(url_for("portal.onboarding"))


@portal_bp.route("/plugins/download/<plugin_key>")
def plugin_download(plugin_key: str):
    """Authenticated customers download a plugin zip."""
    r = _require_login()
    if r: return r

    import os as _os
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT * FROM plugin_downloads WHERE plugin_key=%s", (plugin_key,))
    row = cur.fetchone()
    cur.close(); conn.close()

    if not row:
        flash("Plugin not found or not yet uploaded. Contact support@phixtra.com.", "warning")
        return redirect(url_for("portal.onboarding"))

    file_path = row.get("file_path") or ""
    if not _os.path.exists(file_path):
        flash("Plugin file is missing on the server. Please contact support@phixtra.com.", "danger")
        return redirect(url_for("portal.onboarding"))

    return send_file(file_path, as_attachment=True,
                     download_name=row.get("filename") or f"{plugin_key}.zip")


# ── Onboarding wizard detail page ──────────────────────────────────────────────
@portal_bp.route("/onboarding")
def onboarding():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    ob        = _onboarding_status(tenant_id, int(customer["id"]))
    keys      = _get_api_keys(tenant_id)

    # Load available plugin downloads so the template can show download buttons
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT * FROM plugin_downloads")
    rows = cur.fetchall() or []
    cur.close(); conn.close()
    plugins_map = {r["plugin_key"]: r for r in rows}

    return render_template("portal/onboarding.html",
                           customer=customer, onboarding=ob, keys=keys,
                           plugins=plugins_map)


# ══════════════════════════════════════════════════════════════════════════════
# API KEY MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@portal_bp.route("/api-keys")
def api_keys():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    keys      = _get_api_keys(tenant_id)
    # Attach usage per key
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    now = datetime.utcnow()
    for k in keys:
        kid = int(k["id"])
        cur.execute("""
            SELECT COALESCE(SUM(used_tokens),0) AS t30
            FROM usage_events
            WHERE api_key_id=%s AND created_at >= (UTC_TIMESTAMP() - INTERVAL 30 DAY)""", (kid,))
        k["credits_30d"] = tokens_to_credits(int((cur.fetchone() or {}).get("t30") or 0))

        # Trial days remaining
        if k.get("key_type") == "trial" and k.get("trial_expires_at"):
            diff = k["trial_expires_at"] - now
            k["trial_days_left"] = max(0, diff.days)
        else:
            k["trial_days_left"] = None

        # Status label
        if not k.get("is_active"):
            k["status"] = "Revoked"
        elif k.get("key_type") == "trial" and k.get("trial_expires_at") and k["trial_expires_at"] < now:
            k["status"] = "Expired"
        elif k.get("key_type") == "trial":
            k["status"] = "Trial"
        else:
            k["status"] = "Active"

    cur.close(); conn.close()

    return render_template("portal/api_keys.html",
                           customer=customer, keys=keys)



@portal_bp.route("/api-keys/<int:key_id>/revoke", methods=["POST"])
def api_keys_revoke(key_id: int):
    r = _require_login()
    if r: return r

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    # Security: only revoke keys that belong to this tenant
    cur.execute("SELECT id, website, key_type FROM api_keys WHERE id=%s AND tenant_id=%s",
                (key_id, tenant_id))
    k = cur.fetchone()
    if not k:
        cur.close(); conn.close()
        flash("Key not found.", "danger")
        return redirect(url_for("portal.api_keys"))

    cur2 = conn.cursor(buffered=True)
    cur2.execute("UPDATE api_keys SET is_active=0 WHERE id=%s", (key_id,))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(
        admin_username=f"customer:{customer['email']}",
        action="revoke_key",
        tenant_id=tenant_id,
        website=k.get("website"),
        key_type=k.get("key_type"),
        api_key_id=key_id,
        details={"revoked_from": "portal"},
    )

    flash("API key revoked.", "success")
    return redirect(url_for("portal.api_keys"))


# ══════════════════════════════════════════════════════════════════════════════
# BILLING
# ══════════════════════════════════════════════════════════════════════════════

@portal_bp.route("/billing")
def billing():
    r = _require_login()
    if r: return r

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    _ensure_tenant_balance_row(tenant_id)
    balance_credits = tokens_to_credits(_get_tenant_balance_tokens(tenant_id))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    # Stage 7: only show one-time top-up packages on this page.
    # Subscription plans are shown on /billing/subscribe.
    # The OR handles existing packages created before Stage 3 added package_type.
    cur.execute("""
        SELECT * FROM credit_packages
        WHERE is_active=1
          AND (package_type='topup' OR package_type IS NULL)
        ORDER BY sort_order ASC, id ASC
    """)
    packages = cur.fetchall() or []
    cur.close(); conn.close()

    import json as _json
    for pkg in packages:
        raw_feat = pkg.get("features")
        if raw_feat:
            try:
                pkg["features_parsed"] = _json.loads(raw_feat) if isinstance(raw_feat, str) else raw_feat
            except Exception:
                pkg["features_parsed"] = {}
        else:
            pkg["features_parsed"] = {}

    # ── Trial status for billing page banner ──────────────────────────────
    billing_keys        = _get_api_keys(tenant_id)
    is_trial_customer   = False
    trial_days_left     = None
    trial_expired_billing = False
    for k in billing_keys:
        if k.get("key_type") == "trial":
            is_trial_customer = True
            exp = k.get("trial_expires_at")
            if k.get("is_active") and exp:
                diff = exp - datetime.utcnow()
                trial_days_left = max(0, diff.days)
            elif not k.get("is_active"):
                trial_expired_billing = True
                trial_days_left = 0
            break

    # Stage 7: pass subscription + card state so the template can show them
    active_sub    = _get_active_subscription(int(customer["id"]))
    saved_methods = _get_saved_payment_methods(int(customer["id"]))

    return render_template("portal/billing.html",
                           customer=customer,
                           balance_credits=balance_credits,
                           packages=packages,
                           stripe_ready=_stripe_ok(),
                           is_trial_customer=is_trial_customer,
                           trial_days_left=trial_days_left,
                           trial_expired_billing=trial_expired_billing,
                           active_sub=active_sub,
                           saved_methods=saved_methods)


@portal_bp.route("/billing/checkout", methods=["POST"])
def billing_checkout():
    r = _require_login()
    if r: return r

    if not _stripe_ok():
        flash("Online payments are not configured yet. Contact support to top up.", "warning")
        return redirect(url_for("portal.billing"))

    pkg_id   = int(request.form.get("package_id") or 0)
    add_vat  = request.form.get("add_vat") == "on"
    vat_num  = (request.form.get("vat_number") or "").strip()

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT * FROM credit_packages WHERE id=%s AND is_active=1", (pkg_id,))
    pkg = cur.fetchone()
    if not pkg:
        cur.close(); conn.close()
        flash("Invalid package selected.", "danger")
        return redirect(url_for("portal.billing"))

    credits      = int(pkg["credits"])
    amount_pence = int(pkg["price_pence"])
    vat_rate     = float(pkg.get("vat_rate") or 20.0)
    vat_pence    = calc_vat(amount_pence, vat_rate) if add_vat else 0
    total_pence  = amount_pence + vat_pence
    inv_num      = next_invoice_number()

    cur2 = conn.cursor(buffered=True)
    cur2.execute("""
        INSERT INTO invoices
            (invoice_number, tenant_id, customer_id, package_id, credits,
             amount_pence, vat_pence, currency, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')""",
        (inv_num, tenant_id, int(customer["id"]), pkg_id, credits,
         amount_pence, vat_pence, pkg.get("currency") or "gbp"))
    invoice_id = cur2.lastrowid
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

    # Stage 2: resolve (or create) the Stripe Customer for this customer.
    # If this returns None (Stripe down / not configured) the fallback below
    # keeps the existing customer_email= behaviour so nothing breaks.
    stripe_cus_id = _get_or_create_stripe_customer(customer)

    # Pass customer= when we have a Stripe Customer ID so the card is attached
    # to their account in Stripe.  Fall back to customer_email= (original
    # behaviour) if customer creation failed — checkout still works either way.
    _cus_param = (
        {"customer": stripe_cus_id}
        if stripe_cus_id
        else {"customer_email": customer["email"]}
    )

    sess = stripe.checkout.Session.create(
        mode="payment",
        **_cus_param,
        line_items=[{
            "price_data": {
                "currency": pkg.get("currency") or "gbp",
                "product_data": {"name": f"{credits} PhiXtra credits",
                                 "description": "1 credit = 5,000 AI tokens"},
                "unit_amount": total_pence,
            },
            "quantity": 1,
        }],
        success_url=f"{_PORTAL_BASE_URL}/billing?success=1",
        cancel_url =f"{_PORTAL_BASE_URL}/billing?canceled=1",
        metadata={
            "invoice_id":   str(invoice_id),
            "invoice_number": inv_num,
            "tenant_id":    str(tenant_id),
            "customer_id":  str(customer["id"]),
            "credits":      str(credits),
            "vat_pence":    str(vat_pence),
            "vat_number":   vat_num,
        },
    )

    conn = get_db_connection()
    cur = conn.cursor(buffered=True)
    cur.execute("UPDATE invoices SET stripe_session_id=%s WHERE id=%s", (sess.id, invoice_id))
    conn.commit()
    cur.close(); conn.close()

    return redirect(sess.url)


@portal_bp.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not _stripe_ok():
        return "not configured", 400

    stripe.api_key      = os.getenv("STRIPE_SECRET_KEY")
    endpoint_secret     = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    payload             = request.data
    sig                 = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, endpoint_secret)
    except Exception as e:
        print("webhook verify failed:", e)
        return "bad sig", 400

    if event.get("type") != "checkout.session.completed":
        return "ok", 200

    sess_obj = event["data"]["object"]
    meta     = sess_obj.get("metadata") or {}

    invoice_id  = int(meta.get("invoice_id")  or 0)
    tenant_id   = int(meta.get("tenant_id")   or 0)
    customer_id = int(meta.get("customer_id") or 0)
    credits     = int(meta.get("credits")     or 0)
    vat_pence   = int(meta.get("vat_pence")   or 0)
    pi          = sess_obj.get("payment_intent")

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT * FROM invoices WHERE id=%s", (invoice_id,))
    inv = cur.fetchone()
    if not inv or inv.get("status") == "paid":
        cur.close(); conn.close()
        return "ok", 200

    tokens_add = credits_to_tokens(credits)

    cur2 = conn.cursor(buffered=True)
    cur2.execute("INSERT IGNORE INTO tenant_balances (tenant_id, token_balance) VALUES (%s, 0)", (tenant_id,))
    cur2.execute("UPDATE tenant_balances SET token_balance = token_balance + %s WHERE tenant_id=%s",
                 (tokens_add, tenant_id))

    # Convert any trial key to paid on first purchase — this is the critical
    # step that upgrades a trial customer. We change key_type to 'paid',
    # reactivate the key, and clear the trial expiry date.
    cur2.execute("""
        UPDATE api_keys
        SET key_type='paid', is_active=1, trial_expires_at=NULL
        WHERE tenant_id=%s AND key_type='trial'
    """, (tenant_id,))
    was_trial = cur2.rowcount > 0

    # Also reactivate any existing paid keys (handles non-trial top-ups)
    cur2.execute("UPDATE api_keys SET is_active=1 WHERE tenant_id=%s AND key_type='paid'", (tenant_id,))

    # ── Apply the package's features to the tenant ────────────────────────────
    # Look up the package that was purchased via the invoice, then merge its
    # features JSON into the tenant's existing features so that any premium
    # features included in the package are activated immediately on payment.
    package_id = int(inv.get("package_id") or 0)
    if package_id:
        cur3 = conn.cursor(dictionary=True, buffered=True)
        cur3.execute("SELECT features FROM credit_packages WHERE id=%s", (package_id,))
        pkg_row = cur3.fetchone()
        cur3.close()
        if pkg_row and pkg_row.get("features"):
            try:
                pkg_features = _json_mod.loads(pkg_row["features"]) if isinstance(pkg_row["features"], str) else pkg_row["features"]
            except Exception:
                pkg_features = {}
            if pkg_features:
                # Load the tenant's current features, merge the package features in,
                # then save back. This preserves any features already on the tenant.
                cur4 = conn.cursor(dictionary=True, buffered=True)
                cur4.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
                tenant_row = cur4.fetchone()
                cur4.close()
                try:
                    existing = _json_mod.loads(tenant_row["features"]) if (tenant_row and tenant_row.get("features")) else {}
                except Exception:
                    existing = {}
                # Merge: package features are added on top of existing features
                existing.update(pkg_features)
                cur5 = conn.cursor(buffered=True)
                cur5.execute("UPDATE tenants SET features=%s WHERE id=%s",
                             (_json_mod.dumps(existing), tenant_id))
                cur5.close()
    # ─────────────────────────────────────────────────────────────────────────

    cur.execute("""
        SELECT c.email, c.first_name, t.name AS tenant_name
        FROM customers c JOIN tenants t ON t.id=c.tenant_id
        WHERE c.id=%s""", (customer_id,))
    info = cur.fetchone() or {}

    created_at = inv.get("created_at") or datetime.utcnow()
    pdf_path = generate_invoice_pdf(
        invoice_number=inv["invoice_number"],
        customer_email=info.get("email") or "",
        tenant_name=info.get("tenant_name") or "",
        credits=int(inv.get("credits") or 0),
        amount_pence=int(inv.get("amount_pence") or 0),
        vat_pence=int(inv.get("vat_pence") or 0),
        currency=inv.get("currency") or "gbp",
        created_at=created_at,
    )

    cur2.execute("""
        UPDATE invoices SET status='paid', stripe_payment_intent=%s, pdf_path=%s
        WHERE id=%s""", (pi, pdf_path, invoice_id))
    conn.commit()
    cur2.close()

    insert_audit_log(action="credits_topped_up", tenant_id=tenant_id,
                     details={"credits": credits, "tokens_added": tokens_add,
                              "invoice": inv.get("invoice_number")})

    if was_trial:
        insert_audit_log(
            action="trial_converted_to_paid",
            tenant_id=tenant_id,
            details={"converted_by": "stripe_webhook", "invoice": inv.get("invoice_number")},
        )

    try:
        email = info.get("email")
        name  = info.get("first_name") or "there"
        if email:
            total = int(inv.get("amount_pence") or 0) + int(inv.get("vat_pence") or 0)
            if was_trial:
                subject    = "Welcome to PhiXtra — you're now on a paid plan ✅"
                headline   = "You're on a paid plan! 🎉"
                extra_para = (
                    "<p>Your free trial has been successfully upgraded. "
                    "Your AI assistant is now fully active and running on your new credits.</p>"
                )
            else:
                subject    = "PhiXtra payment received"
                headline   = "Payment received ✅"
                extra_para = ""
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:520px">
              <h2 style="color:{BRAND}">{headline}</h2>
              <p>Hi {name},</p>
              {extra_para}
              <p>We received your payment for <b>{credits} credits</b>.</p>
              <p>Total: <b>{money_fmt(total, inv.get('currency') or 'gbp')}</b></p>
              <p><a href="{_PORTAL_BASE_URL}/invoices"
                 style="background:{BRAND};color:#fff;padding:10px 18px;border-radius:12px;text-decoration:none;display:inline-block">
                 Download invoice</a></p>
            </div>"""
            send_email(email, subject, html)
    except Exception:
        pass

    cur.close(); conn.close()
    return "ok", 200


# ══════════════════════════════════════════════════════════════════════════════
# INVOICES
# ══════════════════════════════════════════════════════════════════════════════

@portal_bp.route("/invoices")
def invoices():
    r = _require_login()
    if r: return r

    customer    = _get_customer(_customer_id())
    customer_id = int(customer["id"])
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)

    # Original top-up invoices
    cur.execute("""
        SELECT id, invoice_number, credits, amount_pence, vat_pence,
               currency, status, created_at, 'topup' AS invoice_type,
               pdf_path
        FROM invoices WHERE customer_id=%s ORDER BY created_at DESC""",
        (customer_id,))
    topup_rows = cur.fetchall() or []

    # Stage 10: subscription invoices from the new table
    sub_rows = []
    try:
        cur.execute("""
            SELECT si.id, si.invoice_number, si.credits,
                   si.amount_pence, 0 AS vat_pence,
                   si.currency, si.status, si.created_at,
                   'subscription' AS invoice_type,
                   si.pdf_path
            FROM subscription_invoices si
            WHERE si.customer_id=%s ORDER BY si.created_at DESC""",
            (customer_id,))
        sub_rows = cur.fetchall() or []
    except Exception as _sub_e:
        print("⚠️ invoices(): subscription_invoices query failed:", _sub_e)

    cur.close(); conn.close()

    # Merge and sort newest first
    rows = topup_rows + sub_rows
    rows.sort(key=lambda r: (r.get("created_at") or datetime.min), reverse=True)

    for row in rows:
        row["total_pence"] = int(row.get("amount_pence") or 0) + int(row.get("vat_pence") or 0)
        row["total_fmt"]   = money_fmt(row["total_pence"], row.get("currency") or "gbp")

    return render_template("portal/invoices.html", customer=customer, invoices=rows)


@portal_bp.route("/invoice/<int:invoice_id>/download")
def invoice_download(invoice_id: int):
    r = _require_login()
    if r: return r

    customer    = _get_customer(_customer_id())
    customer_id = int(customer["id"])
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)

    # Try the original top-up invoices table first
    cur.execute("SELECT * FROM invoices WHERE id=%s AND customer_id=%s",
                (invoice_id, customer_id))
    inv = cur.fetchone()

    # Stage 10: if not found there, try subscription_invoices
    if not inv:
        try:
            cur.execute("""
                SELECT id, invoice_number, status, pdf_path
                FROM subscription_invoices
                WHERE id=%s AND customer_id=%s
            """, (invoice_id, customer_id))
            inv = cur.fetchone()
        except Exception as _si_e:
            print("⚠️ invoice_download sub lookup failed:", _si_e)

    cur.close(); conn.close()

    if not inv or inv.get("status") != "paid" or not inv.get("pdf_path"):
        flash("Invoice PDF is not available yet.", "warning")
        return redirect(url_for("portal.invoices"))

    if not os.path.exists(inv["pdf_path"]):
        flash("Invoice file is missing. Contact support.", "danger")
        return redirect(url_for("portal.invoices"))

    return send_file(inv["pdf_path"], as_attachment=True,
                     download_name=f"{inv['invoice_number']}.pdf")


# ══════════════════════════════════════════════════════════════════════════════
# CART REVENUE RECOVERY DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

def _get_cart_recovery_data(tenant_id: int, days: int = 30) -> dict:
    """
    Fetch all cart recovery stats for the customer-facing dashboard.
    Never raises — returns safe defaults on any DB error or if feature is disabled.
    All monetary values are in pounds (float).
    action_types in recovery_log: popup_queued | email_sent | final_email_sent |
                                   sequence_expired | recovered | recovered_via_chat
    """
    _safe: dict = {
        "enabled": False,
        "stats": {
            "total": 0, "recovered": 0, "in_progress": 0,
            "pending": 0, "expired": 0, "active_now": 0,
            "recovery_rate": 0.0,
            "revenue_recovered": 0.0,
            "avg_recovered_value": 0.0,
        },
        "touches": {"popup_queued": 0, "email_sent": 0, "final_email_sent": 0},
        "queue_rows": [],
        "trend": [],
    }
    conn = None
    cur  = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)

        # ── 1. Feature flag ────────────────────────────────────────────────
        cur.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
        t_row    = cur.fetchone() or {}
        features = {}
        try:
            features = _json.loads(t_row.get("features") or "{}")
        except Exception:
            pass
        if not features.get("cart_recovery"):
            return _safe
        _safe["enabled"] = True

        # ── 2. KPI aggregates from abandonment_queue ───────────────────────
        cur.execute("""
            SELECT
                COUNT(*)                                                              AS total,
                SUM(status = 'recovered')                                             AS recovered,
                SUM(status = 'in_progress')                                           AS in_progress,
                SUM(status = 'pending')                                               AS pending,
                SUM(status = 'expired')                                               AS expired,
                COALESCE(SUM(CASE WHEN status='recovered' THEN cart_value ELSE 0 END),0)
                                                                                      AS revenue_recovered,
                COALESCE(AVG(CASE WHEN status='recovered' THEN cart_value END),0)
                                                                                      AS avg_recovered_value
            FROM abandonment_queue
            WHERE tenant_id = %s
              AND created_at >= (UTC_TIMESTAMP() - INTERVAL %s DAY)
        """, (tenant_id, days))
        s = cur.fetchone() or {}

        recovered = int(s.get("recovered")   or 0)
        expired   = int(s.get("expired")     or 0)
        concluded = recovered + expired
        rate      = round(recovered / concluded * 100, 1) if concluded > 0 else 0.0
        in_prog   = int(s.get("in_progress") or 0)
        pending   = int(s.get("pending")     or 0)

        _safe["stats"] = {
            "total":               int(s.get("total") or 0),
            "recovered":           recovered,
            "in_progress":         in_prog,
            "pending":             pending,
            "expired":             expired,
            "active_now":          in_prog + pending,
            "recovery_rate":       rate,
            "revenue_recovered":   float(s.get("revenue_recovered")   or 0),
            "avg_recovered_value": float(s.get("avg_recovered_value") or 0),
        }

        # ── 3. Touch performance (recovery_log joined to queue) ────────────
        cur.execute("""
            SELECT rl.action_type, COUNT(*) AS cnt
            FROM recovery_log rl
            JOIN abandonment_queue aq ON aq.id = rl.queue_id
            WHERE aq.tenant_id = %s
              AND rl.created_at >= (UTC_TIMESTAMP() - INTERVAL %s DAY)
            GROUP BY rl.action_type
        """, (tenant_id, days))
        touches: dict = {"popup_queued": 0, "push_sent": 0, "email_sent": 0, "final_email_sent": 0}
        for row in (cur.fetchall() or []):
            at = row.get("action_type") or ""
            if at in touches:
                touches[at] = int(row.get("cnt") or 0)
        _safe["touches"] = touches

        # ── 4. Queue rows — last 100 sessions, most recent first ─────────────
        cur.execute("""
            SELECT id, session_id, customer_email, cart_value, cart_items,
                   intent_score, priority, status, touches_sent,
                   expires_at, created_at, updated_at
            FROM abandonment_queue
            WHERE tenant_id = %s
            ORDER BY updated_at DESC
            LIMIT 100
        """, (tenant_id,))
        all_rows = cur.fetchall() or []

        # Parse cart_items JSON so the template can iterate product names directly.
        # MySQL JSON columns may come back as a string or already-parsed list depending
        # on the connector version — handle both safely.
        for _r in all_rows:
            raw_items = _r.get("cart_items")
            if raw_items:
                try:
                    _r["cart_items"] = (
                        _json.loads(raw_items) if isinstance(raw_items, str) else raw_items
                    )
                    if not isinstance(_r["cart_items"], list):
                        _r["cart_items"] = []
                except Exception:
                    _r["cart_items"] = []
            else:
                _r["cart_items"] = []

        _safe["queue_rows"] = all_rows

        # ── 5. Daily recovery trend (group by date recovered) ─────────────
        cur.execute("""
            SELECT DATE(updated_at)             AS d,
                   COUNT(*)                     AS recovered_count,
                   COALESCE(SUM(cart_value), 0) AS revenue
            FROM abandonment_queue
            WHERE tenant_id = %s
              AND status     = 'recovered'
              AND updated_at >= (UTC_TIMESTAMP() - INTERVAL %s DAY)
            GROUP BY DATE(updated_at)
            ORDER BY d ASC
        """, (tenant_id, days))
        _safe["trend"] = [
            {
                "d":         str(r["d"]),
                "recovered": int(r["recovered_count"]),
                "revenue":   float(r["revenue"]),
            }
            for r in (cur.fetchall() or [])
        ]

        return _safe

    except Exception as e:
        print("⚠️ _get_cart_recovery_data error:", e)
        return _safe
    finally:
        try:
            if cur:  cur.close()
        except Exception:
            pass
        try:
            if conn: conn.close()
        except Exception:
            pass


@portal_bp.route("/cart-recovery/settings", methods=["POST"])
def cart_recovery_save_settings():
    """
    Allows the store owner (customer) to update their own cart recovery settings
    from the portal — specifically the discount incentive % and popup message.
    The admin still controls whether cart_recovery is ON/OFF for the tenant.
    This route only updates sub-settings; it never enables or disables the feature.
    """
    r = _require_login()
    if r: return r

    customer = _get_customer(_customer_id())
    if not customer:
        flash("Your account could not be loaded.", "danger")
        return redirect(url_for("portal.login"))

    tenant_id = int(customer["tenant_id"])

    # Read and validate the incentive percentage from the form
    try:
        incentive_pct = max(0, min(50, int(request.form.get("cart_recovery_incentive_pct") or 0)))
    except (ValueError, TypeError):
        incentive_pct = 0

    popup_message = (request.form.get("cart_recovery_popup_message") or "").strip()

    conn = None
    cur  = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)

        # Read the existing features JSON — we must NOT overwrite unrelated keys
        cur.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
        t_row = cur.fetchone() or {}
        features = {}
        try:
            features = _json.loads(t_row.get("features") or "{}")
        except Exception:
            pass

        # Only allow changes if cart_recovery is already enabled for this tenant
        if not features.get("cart_recovery"):
            flash("Cart Recovery is not yet enabled for your account. Contact PhiXtra support.", "warning")
            return redirect(url_for("portal.cart_recovery_dashboard"))

        # Update only the sub-settings — leave all other feature flags untouched
        if incentive_pct > 0:
            features["cart_recovery_incentive_pct"] = incentive_pct
        else:
            # 0 means no discount — remove the key so the backend sends no code
            features.pop("cart_recovery_incentive_pct", None)

        if popup_message:
            features["cart_recovery_popup_message"] = popup_message
        else:
            features.pop("cart_recovery_popup_message", None)

        cur2 = conn.cursor(buffered=True)
        cur2.execute(
            "UPDATE tenants SET features=%s WHERE id=%s",
            (_json.dumps(features), tenant_id)
        )
        conn.commit()
        cur2.close()

        flash("Cart recovery settings saved.", "success")

    except Exception as e:
        print("⚠️ cart_recovery_save_settings error:", e)
        flash("Could not save settings. Please try again.", "danger")
    finally:
        try:
            if cur:  cur.close()
        except Exception:
            pass
        try:
            if conn: conn.close()
        except Exception:
            pass

    return redirect(url_for("portal.cart_recovery_dashboard"))


@portal_bp.route("/cart-recovery")
def cart_recovery_dashboard():
    r = _require_login()
    if r: return r

    customer = _get_customer(_customer_id())
    if not customer:
        session.clear()
        flash("Your account could not be loaded. Please log in again.", "danger")
        return redirect(url_for("portal.login"))

    tenant_id = int(customer["tenant_id"])

    # Period selector: 7 / 30 / 90 days, default 30
    try:
        days = int(request.args.get("days") or 30)
        if days not in (7, 30, 90):
            days = 30
    except Exception:
        days = 30

    data = _get_cart_recovery_data(tenant_id, days)

    # Pre-format monetary values (money_fmt takes pence)
    revenue_fmt = money_fmt(int(data["stats"]["revenue_recovered"] * 100), "gbp")
    avg_fmt     = money_fmt(int(data["stats"]["avg_recovered_value"] * 100), "gbp")

    # Pass the current cart recovery sub-settings so the settings form is pre-filled.
    # We read them fresh from the DB (same query already ran inside _get_cart_recovery_data
    # but we need them as individual template variables).
    recovery_settings: dict = {}
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
        t_row = cur.fetchone() or {}
        cur.close(); conn.close()
        recovery_settings = _json.loads(t_row.get("features") or "{}")
    except Exception:
        recovery_settings = {}

    return render_template(
        "portal/cart_recovery.html",
        customer          = customer,
        days              = days,
        enabled           = data["enabled"],
        stats             = data["stats"],
        touches           = data["touches"],
        rows              = data["queue_rows"],
        rows_recent       = data["queue_rows"][:25],
        trend             = data["trend"],
        revenue_fmt       = revenue_fmt,
        avg_fmt           = avg_fmt,
        recovery_settings = recovery_settings,
    )


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM INSTRUCTION
# ══════════════════════════════════════════════════════════════════════════════

_AI_REQ_MARKER = "\n\n[Additional requirements]\n"


def _split_system_prompt(system_prompt: str):
    """Split the stored system_prompt back into (ai_instructions, ai_requirements)."""
    if _AI_REQ_MARKER in system_prompt:
        idx = system_prompt.index(_AI_REQ_MARKER)
        return system_prompt[:idx], system_prompt[idx + len(_AI_REQ_MARKER):]
    return system_prompt, ""


@portal_bp.route("/system-instruction", methods=["GET", "POST"])
def ai_instruction():
    r = _require_login()
    if r: return r

    customer = _get_customer(_customer_id())
    if not customer:
        session.clear()
        flash("Your account could not be loaded. Please log in again.", "danger")
        return redirect(url_for("portal.login"))

    tenant_id = int(customer["tenant_id"])

    if request.method == "GET":
        # Load current system_prompt from the tenants table
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True, buffered=True)
            cur.execute("SELECT system_prompt FROM tenants WHERE id=%s", (tenant_id,))
            row = cur.fetchone() or {}
            cur.close(); conn.close()
        except Exception as e:
            print("⚠️ ai_instruction GET error:", e)
            row = {}

        current_prompt = (row.get("system_prompt") or "").strip()
        ai_instructions, ai_requirements = _split_system_prompt(current_prompt)

        return render_template(
            "portal/ai_instruction.html",
            customer        = customer,
            ai_instructions = ai_instructions,
            ai_requirements = ai_requirements,
        )

    # ── POST: save updated instructions ────────────────────────────────────
    ai_instructions = (request.form.get("ai_instructions") or "").strip()
    ai_requirements = (request.form.get("ai_requirements") or "").strip()

    if not ai_instructions:
        flash("The AI instructions field is required.", "danger")
        return redirect(url_for("portal.ai_instruction"))

    # Build system_prompt exactly the same way registration does
    system_prompt_text = ai_instructions
    if ai_requirements:
        system_prompt_text += f"{_AI_REQ_MARKER}{ai_requirements}"

    try:
        conn = get_db_connection()
        cur = conn.cursor(buffered=True)
        cur.execute(
            "UPDATE tenants SET system_prompt=%s WHERE id=%s",
            (system_prompt_text, tenant_id)
        )
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ ai_instruction POST error:", e)
        flash("An error occurred while saving. Please try again.", "danger")
        return redirect(url_for("portal.ai_instruction"))

    insert_audit_log(
        admin_username=f"customer:{customer['email']}",
        action="update_system_prompt",
        tenant_id=tenant_id,
        website=customer.get("tenant_domain") or "",
        details={"updated_by": customer.get("email")},
    )

    flash("System instruction updated ✅", "success")
    return redirect(url_for("portal.ai_instruction"))


# ══════════════════════════════════════════════════════════════════════════════
# VERIFIED SPECS SETTINGS — per-tenant trusted domains & custom spec types
# ══════════════════════════════════════════════════════════════════════════════

def _load_spec_settings(tenant_id: int) -> dict:
    """Load verified-spec settings from the tenant's features JSON.

    Returns a dict with:
      domains  : list of str  (verified_specs_trusted_domains)
      specs    : list of dict (verified_specs_custom_specs)
    Never raises — returns empty lists on any error.
    """
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
        row  = cur.fetchone() or {}
        cur.close(); conn.close()
        import json as _j
        feat = _j.loads(row.get("features") or "{}") if isinstance(row.get("features"), str) else (row.get("features") or {})
        domains = feat.get("verified_specs_trusted_domains") or []
        specs   = feat.get("verified_specs_custom_specs")   or []
        if not isinstance(domains, list): domains = []
        if not isinstance(specs, list):   specs   = []
        return {"domains": domains, "specs": specs}
    except Exception as e:
        print("⚠️ _load_spec_settings error:", e)
        return {"domains": [], "specs": []}


def _save_spec_settings(tenant_id: int, domains: list, specs: list) -> None:
    """Persist domains and specs back into the tenant's features JSON.

    Only touches the two verified-spec keys — all other feature flags are preserved.
    """
    import json as _j
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
    row  = cur.fetchone() or {}
    cur.close()
    feat = _j.loads(row.get("features") or "{}") if isinstance(row.get("features"), str) else (row.get("features") or {})
    feat["verified_specs_trusted_domains"] = domains
    feat["verified_specs_custom_specs"]    = specs
    cur2 = conn.cursor(buffered=True)
    cur2.execute("UPDATE tenants SET features=%s WHERE id=%s", (_j.dumps(feat), tenant_id))
    conn.commit()
    cur2.close(); conn.close()


@portal_bp.route("/verified-specs-settings", methods=["GET"])
def verified_specs_settings():
    """Render the Verified Specs settings page."""
    r = _require_login()
    if r: return r
    customer = _get_customer(_customer_id())
    if not customer:
        session.clear()
        flash("Your account could not be loaded. Please log in again.", "danger")
        return redirect(url_for("portal.login"))
    tenant_id = int(customer["tenant_id"])

    # Only available when the feature is enabled for this tenant
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
        row  = cur.fetchone() or {}
        cur.close(); conn.close()
        import json as _j
        feat = _j.loads(row.get("features") or "{}") if isinstance(row.get("features"), str) else (row.get("features") or {})
        feature_enabled = bool(feat.get("verified_specs_web_lookup", False))
    except Exception:
        feature_enabled = False

    settings = _load_spec_settings(tenant_id)
    return render_template(
        "portal/verified_specs_settings.html",
        customer        = customer,
        feature_enabled = feature_enabled,
        domains         = settings["domains"],
        specs           = settings["specs"],
    )


@portal_bp.route("/verified-specs-settings/domain-add", methods=["POST"])
def verified_specs_domain_add():
    """Add a custom trusted domain for this tenant."""
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    if not customer:
        return redirect(url_for("portal.login"))
    tenant_id = int(customer["tenant_id"])

    raw = (request.form.get("domain") or "").strip().lower()
    # Basic sanity check: must contain a dot and no spaces
    if not raw or " " in raw or "." not in raw:
        flash("Please enter a valid domain (e.g. johnlewis.com).", "danger")
        return redirect(url_for("portal.verified_specs_settings"))

    settings = _load_spec_settings(tenant_id)
    if raw not in settings["domains"]:
        settings["domains"].append(raw)
        try:
            _save_spec_settings(tenant_id, settings["domains"], settings["specs"])
            insert_audit_log(
                admin_username=f"customer:{customer['email']}",
                action="verified_specs_domain_added",
                tenant_id=tenant_id,
                details={"domain": raw},
            )
            flash(f"Domain '{raw}' added ✅", "success")
        except Exception as e:
            print("⚠️ verified_specs_domain_add error:", e)
            flash("An error occurred. Please try again.", "danger")
    else:
        flash(f"'{raw}' is already in your list.", "warning")

    return redirect(url_for("portal.verified_specs_settings"))


@portal_bp.route("/verified-specs-settings/domain-delete", methods=["POST"])
def verified_specs_domain_delete():
    """Remove a custom trusted domain for this tenant."""
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    if not customer:
        return redirect(url_for("portal.login"))
    tenant_id = int(customer["tenant_id"])

    raw = (request.form.get("domain") or "").strip().lower()
    settings = _load_spec_settings(tenant_id)
    if raw in settings["domains"]:
        settings["domains"].remove(raw)
        try:
            _save_spec_settings(tenant_id, settings["domains"], settings["specs"])
            insert_audit_log(
                admin_username=f"customer:{customer['email']}",
                action="verified_specs_domain_deleted",
                tenant_id=tenant_id,
                details={"domain": raw},
            )
            flash(f"Domain '{raw}' removed.", "success")
        except Exception as e:
            print("⚠️ verified_specs_domain_delete error:", e)
            flash("An error occurred. Please try again.", "danger")

    return redirect(url_for("portal.verified_specs_settings"))


@portal_bp.route("/verified-specs-settings/spec-add", methods=["POST"])
def verified_specs_spec_add():
    """Add a custom spec type for this tenant."""
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    if not customer:
        return redirect(url_for("portal.login"))
    tenant_id = int(customer["tenant_id"])

    name      = (request.form.get("spec_name")      or "").strip()
    keywords  = (request.form.get("spec_keywords")  or "").strip()
    unit      = (request.form.get("spec_unit")       or "").strip()
    qualifier = (request.form.get("spec_qualifier")  or "").strip()

    if not name or not keywords or not unit:
        flash("Name, keywords, and unit are all required.", "danger")
        return redirect(url_for("portal.verified_specs_settings"))

    import uuid as _uuid
    new_spec = {
        "id":        _uuid.uuid4().hex[:8],
        "name":      name[:120],
        "keywords":  keywords[:500],
        "unit":      unit[:40],
        "qualifier": qualifier[:200],
    }

    settings = _load_spec_settings(tenant_id)
    settings["specs"].append(new_spec)
    try:
        _save_spec_settings(tenant_id, settings["domains"], settings["specs"])
        insert_audit_log(
            admin_username=f"customer:{customer['email']}",
            action="verified_specs_spec_added",
            tenant_id=tenant_id,
            details={"spec_name": name, "unit": unit},
        )
        flash(f"Custom spec '{name}' added ✅", "success")
    except Exception as e:
        print("⚠️ verified_specs_spec_add error:", e)
        flash("An error occurred. Please try again.", "danger")

    return redirect(url_for("portal.verified_specs_settings"))


@portal_bp.route("/verified-specs-settings/spec-delete", methods=["POST"])
def verified_specs_spec_delete():
    """Remove a custom spec type for this tenant."""
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    if not customer:
        return redirect(url_for("portal.login"))
    tenant_id = int(customer["tenant_id"])

    spec_id   = (request.form.get("spec_id") or "").strip()
    settings  = _load_spec_settings(tenant_id)
    before    = len(settings["specs"])
    settings["specs"] = [s for s in settings["specs"] if s.get("id") != spec_id]

    if len(settings["specs"]) < before:
        try:
            _save_spec_settings(tenant_id, settings["domains"], settings["specs"])
            insert_audit_log(
                admin_username=f"customer:{customer['email']}",
                action="verified_specs_spec_deleted",
                tenant_id=tenant_id,
                details={"spec_id": spec_id},
            )
            flash("Custom spec removed.", "success")
        except Exception as e:
            print("⚠️ verified_specs_spec_delete error:", e)
            flash("An error occurred. Please try again.", "danger")

    return redirect(url_for("portal.verified_specs_settings"))


# ══════════════════════════════════════════════════════════════════════════════
# CART RECOVERY — EMAIL TEMPLATE EDITOR
# ══════════════════════════════════════════════════════════════════════════════

# Default email templates shown in the editor when no custom template is saved.
# Using Python string literals here means the {{placeholder}} tokens are passed
# to the browser as JSON data — they NEVER pass through Jinja2 template rendering
# so they arrive in the editor 100% intact.

_DEFAULT_T2_SUBJECT = "You left something behind at {{store_name}} \U0001f6d2"

_DEFAULT_T2_HTML = """\
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:32px 24px;background:#ffffff;">
  <h2 style="margin:0 0 20px;color:#030C18;font-size:24px;">You left something behind! \U0001f6d2</h2>
  <p style="margin:0 0 14px;color:#374151;font-size:15px;">Hi there,</p>
  <p style="margin:0 0 14px;color:#374151;font-size:15px;">
    We noticed you left <strong>{{cart_items}}</strong> in your cart at <strong>{{store_name}}</strong>.
    Don&#39;t worry &mdash; we&#39;ve saved everything for you!
  </p>
  <p style="margin:0 0 14px;color:#374151;font-size:15px;">
    Your cart total: <strong style="color:#030C18;">{{cart_value}}</strong>
  </p>
  <p style="margin:0 0 20px;color:#059669;font-size:15px;font-weight:bold;">
    \U0001f3f7&#xfe0f; Use code <strong>{{discount_code}}</strong> for an exclusive discount on your order.
  </p>
  <p style="margin:24px 0;">
    <a href="{{cart_url}}"
       style="display:inline-block;background:#030C18;color:#ffffff;padding:14px 32px;
              border-radius:8px;text-decoration:none;font-weight:bold;font-size:16px;">
      Return to My Cart &rarr;
    </a>
  </p>
  <p style="margin:0 0 10px;color:#6b7280;font-size:13px;">
    If you have any questions, just reply to this email &mdash; we&#39;re happy to help.
  </p>
  <p style="margin:0;color:#374151;font-size:14px;">
    Warm regards,<br/><strong>The {{store_name}} Team</strong>
  </p>
</div>"""

_DEFAULT_T3_SUBJECT = "Last chance \u23f0 your cart at {{store_name}} expires soon"

_DEFAULT_T3_HTML = """\
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:32px 24px;background:#ffffff;">
  <h2 style="margin:0 0 20px;color:#dc2626;font-size:24px;">\u23f0 Last Chance &mdash; Your Cart Expires Soon!</h2>
  <p style="margin:0 0 14px;color:#374151;font-size:15px;">Hi there,</p>
  <p style="margin:0 0 14px;color:#374151;font-size:15px;">
    This is your final reminder that your saved cart at <strong>{{store_name}}</strong> is about to expire.
  </p>
  <p style="margin:0 0 14px;color:#374151;font-size:15px;">
    You left <strong>{{cart_items}}</strong> &mdash; worth <strong style="color:#030C18;">{{cart_value}}</strong> &mdash; behind.
  </p>
  <p style="margin:0 0 14px;color:#dc2626;font-size:15px;font-weight:bold;">
    &#x26a0;&#xfe0f; Your cart expires in 24 hours &mdash; don&#39;t miss out!
  </p>
  <p style="margin:0 0 20px;color:#059669;font-size:15px;font-weight:bold;">
    \U0001f3f7&#xfe0f; Use code <strong>{{discount_code}}</strong> for an exclusive discount on your order.
  </p>
  <p style="margin:24px 0;">
    <a href="{{cart_url}}"
       style="display:inline-block;background:#dc2626;color:#ffffff;padding:14px 32px;
              border-radius:8px;text-decoration:none;font-weight:bold;font-size:16px;">
      Complete My Order Now &rarr;
    </a>
  </p>
  <p style="margin:0 0 10px;color:#6b7280;font-size:13px;">
    If you no longer want these items you can simply ignore this email.
  </p>
  <p style="margin:0;color:#374151;font-size:14px;">
    Warm regards,<br/><strong>The {{store_name}} Team</strong>
  </p>
</div>"""


def _get_recovery_features(tenant_id: int) -> dict:
    """
    Helper: safely read the tenant features JSON from the database.
    Returns an empty dict on any error — callers must treat missing keys as defaults.
    """
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
        row  = cur.fetchone() or {}
        cur.close(); conn.close()
        return _json.loads(row.get("features") or "{}")
    except Exception:
        return {}


def _has_feature(tenant_id: int, key: str) -> bool:
    """
    Return True if the tenant's features JSON contains the given key set to a truthy value.
    Returns False on any error — callers must treat missing as 'not enabled'.
    """
    return bool(_get_recovery_features(tenant_id).get(key))


def _save_recovery_features(tenant_id: int, features: dict) -> None:
    """
    Helper: write the full features dict back to the tenants table.
    Raises on DB error so callers can catch and flash a message.
    """
    conn = get_db_connection()
    cur  = conn.cursor(buffered=True)
    cur.execute(
        "UPDATE tenants SET features=%s WHERE id=%s",
        (_json.dumps(features), tenant_id)
    )
    conn.commit()
    cur.close(); conn.close()


@portal_bp.route("/cart-recovery/email-templates", methods=["GET", "POST"])
def cart_recovery_email_template():
    r = _require_login()
    if r: return r

    customer = _get_customer(_customer_id())
    if not customer:
        session.clear()
        flash("Your account could not be loaded. Please log in again.", "danger")
        return redirect(url_for("portal.login"))

    tenant_id = int(customer["tenant_id"])

    # ── POST: save or reset a template ──────────────────────────────────────
    if request.method == "POST":
        touch     = (request.form.get("touch")     or "").strip()   # "t2" or "t3"
        subject   = (request.form.get("subject")   or "").strip()
        html_body = (request.form.get("html_body") or "").strip()

        if touch not in ("t2", "t3"):
            flash("Invalid request.", "danger")
            return redirect(url_for("portal.cart_recovery_email_template"))

        # Read current features so we never overwrite unrelated keys
        features = _get_recovery_features(tenant_id)

        if html_body == "__RESET__":
            # Customer chose "Reset to AI default" — remove both keys for this touch
            features.pop(f"cart_recovery_{touch}_subject", None)
            features.pop(f"cart_recovery_{touch}_html",    None)
            touch_label = "T2 Recovery Email" if touch == "t2" else "T3 Final Reminder"
            try:
                _save_recovery_features(tenant_id, features)
                flash(f"{touch_label} reset to AI-generated. ✅", "success")
            except Exception as e:
                print(f"⚠️ email template reset error: {e}")
                flash("Could not reset template. Please try again.", "danger")
        else:
            # Save the custom template — only if html_body is non-trivial
            min_len = 20   # Quill emits at least "<p><br></p>" for empty editors
            html_is_empty = (
                len(html_body) < min_len
                or html_body.replace("<p>", "").replace("</p>", "")
                              .replace("<br>", "").replace("\n", "").strip() == ""
            )
            if html_is_empty:
                flash("Email body cannot be empty. Please write some content.", "warning")
            else:
                if subject:
                    features[f"cart_recovery_{touch}_subject"] = subject
                else:
                    features.pop(f"cart_recovery_{touch}_subject", None)

                # Wrap Quill's innerHTML in a standard email outer shell
                # so it renders consistently in email clients
                cart_url_placeholder = "{{cart_url}}"
                wrapped_html = (
                    '<div style="font-family:Arial,sans-serif;max-width:600px;'
                    'margin:0 auto;padding:32px 24px;background:#ffffff;">'
                    + html_body
                    + '</div>'
                )
                features[f"cart_recovery_{touch}_html"] = wrapped_html

                touch_label = "T2 Recovery Email" if touch == "t2" else "T3 Final Reminder"
                try:
                    _save_recovery_features(tenant_id, features)
                    flash(f"{touch_label} saved. It will be used for all future recovery emails. ✅", "success")
                except Exception as e:
                    print(f"⚠️ email template save error: {e}")
                    flash("Could not save template. Please try again.", "danger")

        return redirect(url_for("portal.cart_recovery_email_template"))

    # ── GET: render the editor pre-filled with any saved templates ───────────
    features = _get_recovery_features(tenant_id)

    return render_template(
        "portal/email_template.html",
        customer          = customer,
        t2_subject        = features.get("cart_recovery_t2_subject", ""),
        t2_html           = features.get("cart_recovery_t2_html",    ""),
        t3_subject        = features.get("cart_recovery_t3_subject", ""),
        t3_html           = features.get("cart_recovery_t3_html",    ""),
        default_t2_subject = _DEFAULT_T2_SUBJECT,
        default_t2_html    = _DEFAULT_T2_HTML,
        default_t3_subject = _DEFAULT_T3_SUBJECT,
        default_t3_html    = _DEFAULT_T3_HTML,
    )


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ══════════════════════════════════════════════════════════════════════════════

import io
import tempfile
from collections import defaultdict

# ── Report helpers ─────────────────────────────────────────────────────────────

def _get_usage_report_data(tenant_id: int, days: int) -> dict:
    """Fetch AI usage report data for a tenant over the given number of days."""
    safe = {
        "daily_rows": [], "chart_points": [],
        "total_sessions": 0, "total_credits": 0.0,
        "today_credits": 0.0, "avg_credits_per_session": 0.0,
        "peak_day": None, "peak_credits": 0.0,
    }
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)

        # Daily breakdown — sessions + tokens per day
        cur.execute("""
            SELECT
                DATE(created_at)              AS d,
                COUNT(DISTINCT session_id)    AS sessions,
                COALESCE(SUM(used_tokens), 0) AS tokens
            FROM usage_events
            WHERE tenant_id = %s
              AND created_at >= (UTC_TIMESTAMP() - INTERVAL %s DAY)
            GROUP BY DATE(created_at)
            ORDER BY d ASC
        """, (tenant_id, days))
        rows = cur.fetchall() or []

        daily_rows = []
        for r in rows:
            credits = tokens_to_credits(int(r["tokens"] or 0))
            daily_rows.append({
                "d":        str(r["d"]),
                "sessions": int(r["sessions"] or 0),
                "tokens":   int(r["tokens"]   or 0),
                "credits":  credits,
            })

        total_credits  = sum(r["credits"]  for r in daily_rows)
        total_sessions = sum(r["sessions"] for r in daily_rows)

        # Today's credits
        cur.execute("""
            SELECT COALESCE(SUM(used_tokens), 0) AS t
            FROM usage_events
            WHERE tenant_id = %s AND created_at >= UTC_DATE()
        """, (tenant_id,))
        today_tokens  = int((cur.fetchone() or {}).get("t") or 0)
        today_credits = tokens_to_credits(today_tokens)

        # Peak day
        peak_row     = max(daily_rows, key=lambda r: r["credits"], default=None)
        peak_day     = peak_row["d"]     if peak_row else None
        peak_credits = peak_row["credits"] if peak_row else 0.0

        avg_credits = round(total_credits / total_sessions, 4) if total_sessions > 0 else 0.0

        cur.close(); conn.close()

        safe.update({
            "daily_rows":             daily_rows,
            "chart_points":           [{"d": r["d"], "credits": r["credits"]} for r in daily_rows],
            "total_sessions":         total_sessions,
            "total_credits":          total_credits,
            "today_credits":          today_credits,
            "avg_credits_per_session": avg_credits,
            "peak_day":               peak_day,
            "peak_credits":           peak_credits,
        })
    except Exception as e:
        print("⚠️ _get_usage_report_data error:", e)
    return safe


def _get_billing_report_data(tenant_id: int, customer_id: int, days: int) -> dict:
    """Fetch billing report data."""
    safe = {
        "invoices": [], "chart_points": [],
        "total_spend_pence": 0, "total_credits_purchased": 0,
        "total_vat_pence": 0, "invoices_paid": 0,
        "balance_credits": 0,
        "total_spend_fmt": "£0.00", "total_vat_fmt": "£0.00",
        "period_label": f"Last {days} days",
    }
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)

        # Balance
        cur.execute("SELECT token_balance FROM tenant_balances WHERE tenant_id=%s", (tenant_id,))
        bal_row = cur.fetchone() or {}
        safe["balance_credits"] = tokens_to_credits(int(bal_row.get("token_balance") or 0))

        # Invoices in period
        if days >= 9999:
            cur.execute("""
                SELECT * FROM invoices WHERE customer_id=%s ORDER BY created_at DESC
            """, (customer_id,))
            safe["period_label"] = "All time"
        else:
            cur.execute("""
                SELECT * FROM invoices
                WHERE customer_id=%s AND created_at >= (UTC_TIMESTAMP() - INTERVAL %s DAY)
                ORDER BY created_at DESC
            """, (customer_id, days))
            safe["period_label"] = f"Last {days} days"

        rows = cur.fetchall() or []
        cur.close(); conn.close()

        invoices = []
        for inv in rows:
            amt   = int(inv.get("amount_pence") or 0)
            vat   = int(inv.get("vat_pence")    or 0)
            total = amt + vat
            inv["amount_fmt"] = money_fmt(amt,   inv.get("currency") or "gbp")
            inv["vat_fmt"]    = money_fmt(vat,   inv.get("currency") or "gbp")
            inv["total_fmt"]  = money_fmt(total, inv.get("currency") or "gbp")
            invoices.append(inv)

        paid_invs = [i for i in invoices if i.get("status") == "paid"]
        total_pence = sum(int(i.get("amount_pence") or 0) + int(i.get("vat_pence") or 0) for i in paid_invs)
        total_vat   = sum(int(i.get("vat_pence") or 0)    for i in paid_invs)
        total_cred  = sum(int(i.get("credits")   or 0)    for i in paid_invs)

        # Monthly spend chart
        monthly = defaultdict(int)
        for inv in paid_invs:
            ca = inv.get("created_at")
            if ca:
                key = ca.strftime("%Y-%m")
                monthly[key] += int(inv.get("amount_pence") or 0) + int(inv.get("vat_pence") or 0)
        chart_points = [{"month": k, "spend": round(v / 100, 2)} for k, v in sorted(monthly.items())]

        safe.update({
            "invoices":               invoices,
            "chart_points":           chart_points,
            "total_spend_pence":      total_pence,
            "total_credits_purchased": total_cred,
            "total_vat_pence":        total_vat,
            "invoices_paid":          len(paid_invs),
            "total_spend_fmt":        money_fmt(total_pence, "gbp"),
            "total_vat_fmt":          money_fmt(total_vat,   "gbp"),
        })
    except Exception as e:
        print("⚠️ _get_billing_report_data error:", e)
    return safe


# ── Report pages ───────────────────────────────────────────────────────────────

@portal_bp.route("/reports/usage")
def report_usage():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    try:
        days = int(request.args.get("days") or 30)
        if days not in (7, 30, 90):
            days = 30
    except Exception:
        days = 30

    data = _get_usage_report_data(tenant_id, days)
    return render_template(
        "portal/report_usage.html",
        customer                = customer,
        days                    = days,
        daily_rows              = data["daily_rows"],
        chart_points            = data["chart_points"],
        total_sessions          = data["total_sessions"],
        total_credits           = data["total_credits"],
        today_credits           = data["today_credits"],
        avg_credits_per_session = data["avg_credits_per_session"],
        peak_day                = data["peak_day"],
        peak_credits            = data["peak_credits"],
    )


@portal_bp.route("/reports/cart-recovery")
def report_cart():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    try:
        days = int(request.args.get("days") or 30)
        if days not in (7, 30, 90):
            days = 30
    except Exception:
        days = 30

    data        = _get_cart_recovery_data(tenant_id, days)
    revenue_fmt = money_fmt(int(data["stats"]["revenue_recovered"] * 100), "gbp")
    avg_fmt     = money_fmt(int(data["stats"]["avg_recovered_value"] * 100), "gbp")

    return render_template(
        "portal/report_cart.html",
        customer    = customer,
        days        = days,
        enabled     = data["enabled"],
        stats       = data["stats"],
        touches     = data["touches"],
        trend       = data["trend"],
        revenue_fmt = revenue_fmt,
        avg_fmt     = avg_fmt,
    )


@portal_bp.route("/reports/billing")
def report_billing():
    r = _require_login()
    if r: return r
    customer    = _get_customer(_customer_id())
    tenant_id   = int(customer["tenant_id"])
    customer_id = int(customer["id"])

    try:
        days = int(request.args.get("days") or 90)
        if days not in (30, 90, 365, 9999):
            days = 90
    except Exception:
        days = 90

    data = _get_billing_report_data(tenant_id, customer_id, days)
    return render_template(
        "portal/report_billing.html",
        customer               = customer,
        days                   = days,
        invoices               = data["invoices"],
        chart_points           = data["chart_points"],
        total_spend_fmt        = data["total_spend_fmt"],
        total_credits_purchased = data["total_credits_purchased"],
        total_vat_fmt          = data["total_vat_fmt"],
        invoices_paid          = data["invoices_paid"],
        balance_credits        = data["balance_credits"],
        period_label           = data["period_label"],
    )


# ── Report export (PDF / Excel / Word) ────────────────────────────────────────

@portal_bp.route("/reports/export/<report>/<fmt>")
def report_export(report: str, fmt: str):
    r = _require_login()
    if r: return r

    if report not in ("usage", "cart", "billing") or fmt not in ("pdf", "xlsx", "docx"):
        flash("Invalid export request.", "danger")
        return redirect(url_for("portal.report_usage"))

    customer    = _get_customer(_customer_id())
    tenant_id   = int(customer["tenant_id"])
    customer_id = int(customer["id"])

    try:
        days = int(request.args.get("days") or 30)
    except Exception:
        days = 30

    store = customer.get("tenant_domain") or customer.get("tenant_name") or "Your Store"
    from datetime import date
    generated = date.today().strftime("%d %b %Y")

    # ── Build data ─────────────────────────────────────────────────────────
    if report == "usage":
        data = _get_usage_report_data(tenant_id, days)
        title    = "AI Usage Report"
        subtitle = f"{store} · Last {days} days · Generated {generated}"
        headers  = ["Date", "Sessions", "Tokens Used", "Credits Used"]
        rows_out = [[r["d"], r["sessions"], "{:,}".format(r["tokens"]), "{:.4f}".format(r["credits"])]
                    for r in data["daily_rows"]]
        summary_pairs = [
            ("Total Sessions",         str(data["total_sessions"])),
            ("Total Credits Used",     "{:.4f}".format(data["total_credits"])),
            ("Today's Credits",        "{:.4f}".format(data["today_credits"])),
            ("Avg Credits / Session",  "{:.4f}".format(data["avg_credits_per_session"])),
            ("Peak Day",               data["peak_day"] or "N/A"),
            ("Peak Day Credits",       "{:.4f}".format(data["peak_credits"])),
        ]

    elif report == "cart":
        data = _get_cart_recovery_data(tenant_id, days)
        revenue_fmt = money_fmt(int(data["stats"]["revenue_recovered"] * 100), "gbp")
        avg_fmt     = money_fmt(int(data["stats"]["avg_recovered_value"] * 100), "gbp")
        title    = "Cart Recovery Report"
        subtitle = f"{store} · Last {days} days · Generated {generated}"
        headers  = ["Date", "Carts Recovered", "Revenue (£)"]
        rows_out = [[r["d"], r["recovered"], "£{:.2f}".format(r["revenue"])]
                    for r in data["trend"]]
        summary_pairs = [
            ("Feature Enabled",       "Yes" if data["enabled"] else "No"),
            ("Total Abandoned Carts", str(data["stats"]["total"])),
            ("Recovered",             str(data["stats"]["recovered"])),
            ("Recovery Rate",         "{}%".format(data["stats"]["recovery_rate"])),
            ("Revenue Recovered",     revenue_fmt),
            ("Avg Recovered Value",   avg_fmt),
            ("Carts In Progress",     str(data["stats"]["in_progress"])),
            ("Carts Expired",         str(data["stats"]["expired"])),
            ("Popups Shown",          str(data["touches"].get("popup_queued", 0))),
            ("Recovery Emails Sent",  str(data["touches"].get("email_sent", 0))),
            ("Final Reminder Emails", str(data["touches"].get("final_email_sent", 0))),
        ]

    else:  # billing
        data = _get_billing_report_data(tenant_id, customer_id, days)
        title    = "Billing Summary Report"
        subtitle = f"{store} · {data['period_label']} · Generated {generated}"
        headers  = ["Invoice #", "Date", "Credits", "Subtotal", "VAT", "Total", "Status"]
        rows_out = [
            [
                inv.get("invoice_number") or "",
                inv["created_at"].strftime("%d %b %Y") if inv.get("created_at") else "N/A",
                str(inv.get("credits") or 0),
                inv.get("amount_fmt") or "",
                inv.get("vat_fmt")    or "",
                inv.get("total_fmt")  or "",
                (inv.get("status") or "").title(),
            ]
            for inv in data["invoices"]
        ]
        summary_pairs = [
            ("Total Spend",           data["total_spend_fmt"]),
            ("Credits Purchased",     str(data["total_credits_purchased"])),
            ("VAT Paid",              data["total_vat_fmt"]),
            ("Invoices Paid",         str(data["invoices_paid"])),
            ("Current Credit Balance", str(data["balance_credits"])),
        ]

    # ── Render format ──────────────────────────────────────────────────────
    if fmt == "xlsx":
        return _export_xlsx(title, subtitle, summary_pairs, headers, rows_out)
    elif fmt == "docx":
        return _export_docx(title, subtitle, summary_pairs, headers, rows_out)
    else:  # pdf
        return _export_pdf(title, subtitle, summary_pairs, headers, rows_out)


# ── Export renderers ───────────────────────────────────────────────────────────

def _export_pdf(title: str, subtitle: str, summary_pairs: list, headers: list, rows: list):
    """Generate a PDF and return as a Flask response."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib import colors

    buf = io.BytesIO()
    width, height = A4
    c = rl_canvas.Canvas(buf, pagesize=A4)
    MARGIN = 20 * mm
    y = height - MARGIN

    def new_page():
        nonlocal y
        c.showPage()
        y = height - MARGIN

    def check_y(needed=14):
        nonlocal y
        if y < MARGIN + needed:
            new_page()

    # ── Header ────────────────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 18)
    c.drawString(MARGIN, y, "PhiXtra")
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.gray)
    c.drawRightString(width - MARGIN, y, "portal.phixtra.com")
    c.setFillColor(colors.black)
    y -= 8 * mm

    c.setFont("Helvetica-Bold", 14)
    c.drawString(MARGIN, y, title)
    y -= 6 * mm
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#555555"))
    c.drawString(MARGIN, y, subtitle)
    c.setFillColor(colors.black)
    y -= 4 * mm
    c.line(MARGIN, y, width - MARGIN, y)
    y -= 6 * mm

    # ── Summary ───────────────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 10)
    c.drawString(MARGIN, y, "Summary")
    y -= 5 * mm
    col_w = (width - 2 * MARGIN) / 2
    for i, (k, v) in enumerate(summary_pairs):
        check_y(10)
        x_off = MARGIN if i % 2 == 0 else MARGIN + col_w
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#555555"))
        c.drawString(x_off, y, k + ":")
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x_off + col_w * 0.42, y, str(v))
        if i % 2 == 1:
            y -= 5 * mm
    if len(summary_pairs) % 2 == 1:
        y -= 5 * mm
    y -= 4 * mm

    c.line(MARGIN, y, width - MARGIN, y)
    y -= 6 * mm

    # ── Table ─────────────────────────────────────────────────────────────
    if rows:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(MARGIN, y, "Detail Table")
        y -= 5 * mm

        col_count = len(headers)
        usable_w  = width - 2 * MARGIN
        col_widths = [usable_w / col_count] * col_count

        # Header row
        check_y(12)
        c.setFillColor(colors.HexColor("#030C18"))
        c.rect(MARGIN, y - 4, usable_w, 14, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 8)
        x = MARGIN + 2
        for i, h in enumerate(headers):
            c.drawString(x, y + 1, str(h))
            x += col_widths[i]
        y -= 14
        c.setFillColor(colors.black)

        # Data rows
        for ri, row in enumerate(rows):
            check_y(11)
            if ri % 2 == 0:
                c.setFillColor(colors.HexColor("#f9fafb"))
                c.rect(MARGIN, y - 3, usable_w, 12, fill=1, stroke=0)
                c.setFillColor(colors.black)
            c.setFont("Helvetica", 8)
            x = MARGIN + 2
            for ci, cell in enumerate(row):
                cell_str = str(cell)
                if len(cell_str) > 22:
                    cell_str = cell_str[:21] + "…"
                c.drawString(x, y, cell_str)
                x += col_widths[ci]
            y -= 11
    else:
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#888888"))
        c.drawString(MARGIN, y, "No data available for this period.")
        c.setFillColor(colors.black)

    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor("#aaaaaa"))
    c.drawString(MARGIN, MARGIN / 2, "Generated by PhiXtra Portal · support@phixtra.com")
    c.showPage()
    c.save()
    buf.seek(0)

    fname = title.lower().replace(" ", "_") + ".pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)


def _export_xlsx(title: str, subtitle: str, summary_pairs: list, headers: list, rows: list):
    """Generate an Excel workbook and return as a Flask response."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title[:31]

    INK = "030C18"
    GOOD = "12B76A"
    thin = Border(
        left=Side(style="thin", color="E5E7EB"),
        right=Side(style="thin", color="E5E7EB"),
        top=Side(style="thin", color="E5E7EB"),
        bottom=Side(style="thin", color="E5E7EB"),
    )

    # Title row
    ws.merge_cells("A1:{}1".format(chr(64 + max(len(headers), 2))))
    ws["A1"] = title
    ws["A1"].font = Font(name="Calibri", bold=True, size=16, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor=INK)
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 28

    # Subtitle
    ws.merge_cells("A2:{}2".format(chr(64 + max(len(headers), 2))))
    ws["A2"] = subtitle
    ws["A2"].font = Font(name="Calibri", size=9, color="888888")
    ws["A2"].alignment = Alignment(horizontal="left")
    ws.row_dimensions[2].height = 16

    row_idx = 4

    # Summary section
    ws.cell(row=row_idx, column=1, value="Summary").font = Font(bold=True, size=11, name="Calibri")
    row_idx += 1
    for k, v in summary_pairs:
        ws.cell(row=row_idx, column=1, value=k).font = Font(name="Calibri", color="555555")
        cell = ws.cell(row=row_idx, column=2, value=v)
        cell.font = Font(name="Calibri", bold=True)
        row_idx += 1

    row_idx += 1

    # Table header
    if headers:
        ws.cell(row=row_idx, column=1, value="Detail Data").font = Font(bold=True, size=11, name="Calibri")
        row_idx += 1
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(row=row_idx, column=ci, value=h)
            cell.font = Font(bold=True, color="FFFFFF", name="Calibri", size=10)
            cell.fill = PatternFill("solid", fgColor=INK)
            cell.alignment = Alignment(horizontal="center")
            cell.border = thin
        row_idx += 1

        # Data rows
        for ri, row in enumerate(rows):
            fill_clr = "F9FAFB" if ri % 2 == 0 else "FFFFFF"
            for ci, cell_val in enumerate(row, 1):
                cell = ws.cell(row=row_idx, column=ci, value=cell_val)
                cell.font = Font(name="Calibri", size=10)
                cell.fill = PatternFill("solid", fgColor=fill_clr)
                cell.border = thin
            row_idx += 1

    # Auto-column widths
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                max_len = max(max_len, len(str(cell.value or "")))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 4, 40)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    fname = title.lower().replace(" ", "_") + ".xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=fname,
    )


def _export_docx(title: str, subtitle: str, summary_pairs: list, headers: list, rows: list):
    """Generate a Word document and return as a Flask response."""
    from docx import Document
    from docx.shared import Pt, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = Document()

    # Page margins
    for section in doc.sections:
        section.top_margin    = Cm(1.8)
        section.bottom_margin = Cm(1.8)
        section.left_margin   = Cm(2.0)
        section.right_margin  = Cm(2.0)

    def set_cell_bg(cell, hex_color):
        tc   = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd  = OxmlElement("w:shd")
        shd.set(qn("w:val"),   "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"),  hex_color)
        tcPr.append(shd)

    # Brand header
    brand_p = doc.add_paragraph()
    brand_r = brand_p.add_run("PhiXtra")
    brand_r.bold = True
    brand_r.font.size = Pt(18)
    brand_r.font.color.rgb = RGBColor(0x03, 0x0C, 0x18)

    # Title
    title_p = doc.add_heading(title, level=1)
    title_p.runs[0].font.size = Pt(16)

    # Subtitle
    sub_p = doc.add_paragraph(subtitle)
    sub_p.runs[0].font.size = Pt(9)
    sub_p.runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    doc.add_paragraph()

    # Summary table
    sum_heading = doc.add_heading("Summary", level=2)
    sum_heading.runs[0].font.size = Pt(12)

    if summary_pairs:
        tbl = doc.add_table(rows=len(summary_pairs), cols=2)
        tbl.style = "Table Grid"
        for i, (k, v) in enumerate(summary_pairs):
            row = tbl.rows[i]
            row.cells[0].text = k
            row.cells[0].paragraphs[0].runs[0].bold = True
            row.cells[0].paragraphs[0].runs[0].font.size = Pt(10)
            row.cells[1].text = str(v)
            row.cells[1].paragraphs[0].runs[0].font.size = Pt(10)
            set_cell_bg(row.cells[0], "F3F4F6")

    doc.add_paragraph()

    # Data table
    if rows:
        data_heading = doc.add_heading("Detail Data", level=2)
        data_heading.runs[0].font.size = Pt(12)

        tbl2 = doc.add_table(rows=1 + len(rows), cols=len(headers))
        tbl2.style = "Table Grid"

        # Header row
        hdr_row = tbl2.rows[0]
        for ci, h in enumerate(headers):
            cell = hdr_row.cells[ci]
            cell.text = h
            run = cell.paragraphs[0].runs[0]
            run.bold = True
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            set_cell_bg(cell, "030C18")

        # Data rows
        for ri, row in enumerate(rows):
            tbl_row = tbl2.rows[ri + 1]
            bg = "F9FAFB" if ri % 2 == 0 else "FFFFFF"
            for ci, val in enumerate(row):
                cell = tbl_row.cells[ci]
                cell.text = str(val)
                cell.paragraphs[0].runs[0].font.size = Pt(9)
                set_cell_bg(cell, bg)

    doc.add_paragraph()
    footer_p = doc.add_paragraph("Generated by PhiXtra Portal · support@phixtra.com")
    footer_p.runs[0].font.size = Pt(8)
    footer_p.runs[0].font.color.rgb = RGBColor(0xAA, 0xAA, 0xAA)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    fname = title.lower().replace(" ", "_") + ".docx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=fname,
    )


# ══════════════════════════════════════════════════════════════════════════════
# CHAT ARCHIVE
# ══════════════════════════════════════════════════════════════════════════════

import json as _json_mod

def _get_chat_sessions(tenant_id: int, date_from=None, date_to=None, q=None, limit=200, days_limit=None):
    """Fetch chat sessions with optional filters. Returns list of session dicts.
    days_limit: when set (integer), restricts results to sessions created within
    the last N days — used to enforce retention window for non-premium tenants.
    """
    safe = []
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)

        where  = ["cs.tenant_id = %s"]
        params = [tenant_id]

        # Retention-window enforcement — applied unconditionally when set so
        # free-tier users cannot bypass it via the date_from query parameter.
        if days_limit is not None:
            where.append("cs.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)")
            params.append(int(days_limit))

        if date_from:
            where.append("cs.created_at >= %s")
            params.append(date_from + " 00:00:00")
        if date_to:
            where.append("cs.created_at <= %s")
            params.append(date_to + " 23:59:59")
        if q:
            where.append("cs.session_id LIKE %s")
            params.append(f"%{q}%")

        where_sql = " AND ".join(where)

        cur.execute(f"""
            SELECT
                cs.session_id,
                cs.created_at,
                cs.last_seen,
                COUNT(cm.id) AS msg_count,
                (
                  SELECT cm2.content FROM chat_messages cm2
                  WHERE cm2.session_id = cs.session_id AND cm2.tenant_id = cs.tenant_id
                    AND cm2.role = 'user'
                  ORDER BY cm2.created_at ASC LIMIT 1
                ) AS first_msg
            FROM chat_sessions cs
            LEFT JOIN chat_messages cm ON cm.session_id = cs.session_id AND cm.tenant_id = cs.tenant_id
            WHERE {where_sql}
            GROUP BY cs.session_id, cs.created_at, cs.last_seen
            ORDER BY cs.last_seen DESC
            LIMIT %s
        """, params + [limit])

        rows = cur.fetchall() or []

        # If keyword search — also search inside messages
        if q and q.strip():
            cur.execute(f"""
                SELECT DISTINCT cm.session_id
                FROM chat_messages cm
                JOIN chat_sessions cs ON cs.session_id = cm.session_id AND cs.tenant_id = cm.tenant_id
                WHERE cm.tenant_id = %s AND cm.content LIKE %s
            """, (tenant_id, f"%{q}%"))
            extra_sids = {r["session_id"] for r in (cur.fetchall() or [])}
            existing   = {r["session_id"] for r in rows}
            missing    = extra_sids - existing
            if missing:
                fmt_in = ",".join(["%s"] * len(missing))
                cur.execute(f"""
                    SELECT cs.session_id, cs.created_at, cs.last_seen,
                           COUNT(cm.id) AS msg_count,
                           (
                             SELECT cm2.content FROM chat_messages cm2
                             WHERE cm2.session_id=cs.session_id AND cm2.tenant_id=cs.tenant_id
                               AND cm2.role='user' ORDER BY cm2.created_at ASC LIMIT 1
                           ) AS first_msg
                    FROM chat_sessions cs
                    LEFT JOIN chat_messages cm ON cm.session_id=cs.session_id AND cm.tenant_id=cs.tenant_id
                    WHERE cs.tenant_id=%s AND cs.session_id IN ({fmt_in})
                    GROUP BY cs.session_id, cs.created_at, cs.last_seen
                    ORDER BY cs.last_seen DESC
                """, [tenant_id] + list(missing))
                rows += (cur.fetchall() or [])

        cur.close(); conn.close()
        safe = rows
    except Exception as e:
        print("⚠️ _get_chat_sessions error:", e)
    return safe


def _get_session_messages(tenant_id: int, session_id: str):
    """Fetch all messages for a specific session."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT role, content, created_at
            FROM chat_messages
            WHERE session_id = %s AND tenant_id = %s
            ORDER BY created_at ASC
        """, (session_id, tenant_id))
        msgs = cur.fetchall() or []
        for m in msgs:
            if m.get("created_at"):
                m["created_at"] = m["created_at"].strftime("%d %b %Y %H:%M")
        cur.close(); conn.close()
        return msgs
    except Exception as e:
        print("⚠️ _get_session_messages error:", e)
        return []


def _get_session_summary(tenant_id: int, session_id: str):
    """Fetch AI-generated summary for a session if it exists."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT summary_text FROM chat_summaries
            WHERE session_id = %s AND tenant_id = %s
        """, (session_id, tenant_id))
        row = cur.fetchone()
        cur.close(); conn.close()
        return (row.get("summary_text") or "") if row else ""
    except Exception as e:
        print("⚠️ _get_session_summary error:", e)
        return ""


@portal_bp.route("/chat-archive")
def chat_archive():
    r = _require_login()
    if r: return r

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    # ── Tier detection ────────────────────────────────────────────────────────
    # Three tiers, checked in priority order (unlimited wins over 30days):
    #   "unlimited"  — chat_archive_unlimited: no day limit, all exports, search, summaries
    #   "30days"     — chat_archive_30days:    30-day window, PDF export only, search, summaries
    #   "free"       — no feature key:         3-day window, no export, no search, no summaries
    FREE_DAYS = 3

    if _has_feature(tenant_id, "chat_archive_unlimited"):
        tier = "unlimited"
    elif _has_feature(tenant_id, "chat_archive_30days"):
        tier = "30days"
    else:
        tier = "free"

    # What each tier unlocks
    search_allowed   = tier in ("unlimited", "30days")
    summaries_allowed = tier in ("unlimited", "30days")
    # exports_allowed: "none", "pdf_only", or "all"
    if tier == "unlimited":
        exports_allowed = "all"
    elif tier == "30days":
        exports_allowed = "pdf_only"
    else:
        exports_allowed = "none"

    # Retention window
    if tier == "unlimited":
        days_limit = None
    elif tier == "30days":
        days_limit = 30
    else:
        days_limit = FREE_DAYS

    date_from = (request.args.get("date_from") or "").strip() or None
    date_to          = (request.args.get("date_to")   or "").strip() or None
    q                = (request.args.get("q")         or "").strip() or None
    # open_session_id: passed in the URL by the handoff email link (?open=SESSION_ID)
    # so the page auto-opens that specific conversation immediately.
    open_session_id  = (request.args.get("open")      or "").strip() or None

    # Non-search tiers: ignore q from the database query but keep it so the
    # template can show what the user typed alongside the locked message.
    q_for_db = q if search_allowed else None

    sessions = _get_chat_sessions(
        tenant_id,
        date_from=date_from if search_allowed else None,
        date_to=date_to if search_allowed else None,
        q=q_for_db,
        days_limit=days_limit,
    )

    # Count totals
    total_sessions = len(sessions)
    total_messages = sum(int(s.get("msg_count") or 0) for s in sessions)

    # Build session_data_json for the JS inline viewer.
    # Summaries only fetched for paid tiers.
    session_data = {}
    for s in sessions[:50]:
        sid  = s["session_id"]
        msgs = _get_session_messages(tenant_id, sid)
        summ = _get_session_summary(tenant_id, sid) if summaries_allowed else ""
        session_data[sid] = {"messages": msgs, "summary": summ}

    # If a specific session was requested via ?open= (e.g. from a handoff email link)
    # and it wasn't in the first 50 sessions, fetch it directly so the modal can open.
    if open_session_id and open_session_id not in session_data:
        try:
            msgs = _get_session_messages(tenant_id, open_session_id)
            summ = _get_session_summary(tenant_id, open_session_id) if summaries_allowed else ""
            session_data[open_session_id] = {"messages": msgs, "summary": summ}
        except Exception as _oe:
            print(f"⚠️ chat_archive: could not pre-load open session {open_session_id}: {_oe}")

    # Build filter query string for export links
    qs_parts = []
    if date_from: qs_parts.append(f"date_from={date_from}")
    if date_to:   qs_parts.append(f"date_to={date_to}")
    if q:         qs_parts.append(f"q={q}")
    filter_qs = ("?" + "&".join(qs_parts)) if qs_parts else ""

    return render_template(
        "portal/chat_archive.html",
        customer          = customer,
        sessions          = sessions,
        total_sessions    = total_sessions,
        total_messages    = total_messages,
        date_from         = date_from,
        date_to           = date_to,
        q                 = q,
        filter_qs         = filter_qs,
        open_session_id   = open_session_id or "",
        session_data_json = _json_mod.dumps(session_data, default=str),
        tier              = tier,
        free_days         = FREE_DAYS,
        exports_allowed   = exports_allowed,
        search_allowed    = search_allowed,
        summaries_allowed = summaries_allowed,
        days_limit        = days_limit,
    )


@portal_bp.route("/chat-archive/export/<fmt>")
def chat_archive_export(fmt: str):
    """Export filtered chat archive as PDF / Excel / Word. Requires paid tier."""
    r = _require_login()
    if r: return r

    if fmt not in ("pdf", "xlsx", "docx"):
        flash("Invalid export format.", "danger")
        return redirect(url_for("portal.chat_archive"))

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    # ── Tier gate ─────────────────────────────────────────────────────────────
    # Determine what this tenant is allowed to export.
    if _has_feature(tenant_id, "chat_archive_unlimited"):
        exports_allowed = "all"
    elif _has_feature(tenant_id, "chat_archive_30days"):
        exports_allowed = "pdf_only"
    else:
        exports_allowed = "none"

    if exports_allowed == "none":
        flash("📦 Export is a premium feature. Upgrade your plan to export your Chat Archive.", "warning")
        return redirect(url_for("portal.chat_archive"))

    if exports_allowed == "pdf_only" and fmt in ("xlsx", "docx"):
        flash("📄 Your plan includes PDF export only. Upgrade to Chat Archive Unlimited for Excel and Word exports.", "warning")
        return redirect(url_for("portal.chat_archive"))
    # ─────────────────────────────────────────────────────────────────────────

    # 30-day tier: enforce the same 30-day window on exports
    days_limit = 30 if exports_allowed == "pdf_only" else None

    date_from = (request.args.get("date_from") or "").strip() or None
    date_to   = (request.args.get("date_to")   or "").strip() or None
    q         = (request.args.get("q")         or "").strip() or None

    sessions = _get_chat_sessions(tenant_id, date_from=date_from, date_to=date_to, q=q, days_limit=days_limit)

    from datetime import date as _date
    generated = _date.today().strftime("%d %b %Y")
    store     = customer.get("tenant_domain") or "Your Store"

    period_parts = []
    if date_from: period_parts.append(f"From {date_from}")
    if date_to:   period_parts.append(f"To {date_to}")
    period_str = " · ".join(period_parts) if period_parts else "All dates"

    title    = "Chat Archive"
    subtitle = f"{store} · {period_str} · Generated {generated}"

    headers = ["Session ID", "Started", "Last Message", "Messages", "First Visitor Message"]
    rows_out = []
    for s in sessions:
        first = str(s.get("first_msg") or "")
        if len(first) > 80:
            first = first[:79] + "…"
        rows_out.append([
            s["session_id"],
            s["created_at"].strftime("%d %b %Y %H:%M") if s.get("created_at") else "N/A",
            s["last_seen"].strftime("%d %b %Y %H:%M")  if s.get("last_seen")  else "N/A",
            str(s.get("msg_count") or 0),
            first,
        ])

    summary_pairs = [
        ("Total Conversations", str(len(sessions))),
        ("Total Messages",      str(sum(int(s.get("msg_count") or 0) for s in sessions))),
        ("Date Filter",         period_str),
    ]

    if fmt == "xlsx":
        return _export_xlsx(title, subtitle, summary_pairs, headers, rows_out)
    elif fmt == "docx":
        return _export_docx(title, subtitle, summary_pairs, headers, rows_out)
    else:
        return _export_pdf(title, subtitle, summary_pairs, headers, rows_out)


@portal_bp.route("/chat-archive/session/<session_id>/export/<fmt>")
def chat_archive_session_export(session_id: str, fmt: str):
    """Export a single chat session as PDF / Excel / Word. Requires paid tier."""
    r = _require_login()
    if r: return r

    if fmt not in ("pdf", "xlsx", "docx"):
        flash("Invalid export format.", "danger")
        return redirect(url_for("portal.chat_archive"))

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    # ── Tier gate ─────────────────────────────────────────────────────────────
    if _has_feature(tenant_id, "chat_archive_unlimited"):
        exports_allowed = "all"
    elif _has_feature(tenant_id, "chat_archive_30days"):
        exports_allowed = "pdf_only"
    else:
        exports_allowed = "none"

    if exports_allowed == "none":
        flash("📦 Export is a premium feature. Upgrade your plan to export chat transcripts.", "warning")
        return redirect(url_for("portal.chat_archive"))

    if exports_allowed == "pdf_only" and fmt in ("xlsx", "docx"):
        flash("📄 Your plan includes PDF export only. Upgrade to Chat Archive Unlimited for Excel and Word exports.", "warning")
        return redirect(url_for("portal.chat_archive"))
    # ─────────────────────────────────────────────────────────────────────────

    msgs = _get_session_messages(tenant_id, session_id)
    summ = _get_session_summary(tenant_id, session_id)

    from datetime import date as _date
    generated = _date.today().strftime("%d %b %Y")
    store     = customer.get("tenant_domain") or "Your Store"

    title    = "Chat Transcript"
    subtitle = f"{store} · Session: {session_id[:24]}… · Generated {generated}"

    summary_pairs = [
        ("Session ID",     session_id),
        ("Total Messages", str(len(msgs))),
        ("Store",          store),
    ]
    if summ:
        summary_pairs.append(("AI Summary", summ[:200] + ("…" if len(summ) > 200 else "")))

    headers  = ["Role", "Timestamp", "Message"]
    rows_out = []
    for m in msgs:
        role = "Visitor" if m.get("role") == "user" else "AI Agent"
        msg  = str(m.get("content") or "")
        if len(msg) > 500:
            msg = msg[:499] + "…"
        rows_out.append([role, str(m.get("created_at") or ""), msg])

    if fmt == "xlsx":
        return _export_xlsx(title, subtitle, summary_pairs, headers, rows_out)
    elif fmt == "docx":
        return _export_docx(title, subtitle, summary_pairs, headers, rows_out)
    else:
        return _export_pdf(title, subtitle, summary_pairs, headers, rows_out)


# ══════════════════════════════════════════════════════════════════════════════
# HANDOFF RULES EDITOR
# ══════════════════════════════════════════════════════════════════════════════

# Default rules seeded the first time a tenant visits the Handoff Rules page.
# sort_order controls display order (lower = shown first).
_DEFAULT_HANDOFF_RULES = [
    ("Visitor asks to speak to a human, agent, or real person",         "visitor_initiated", 1, 0),
    ("Visitor provides their phone number or WhatsApp number",          "visitor_initiated", 1, 1),
    ("Visitor asks about promotions, discount codes, or special offers","ai_initiated",      1, 2),
    ("Visitor expresses unhappiness, frustration, or makes a complaint","ai_initiated",      1, 3),
    ("Visitor asks about bulk orders or trade accounts",                "ai_initiated",      0, 4),
    ("Visitor asks about a price match or price negotiation",           "ai_initiated",      0, 5),
    ("Visitor asks about returns, refunds, or exchanges",               "ai_initiated",      0, 6),
]


def _get_handoff_rules(tenant_id: int) -> list:
    """Fetch all handoff rules for a tenant ordered by sort_order."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT id, trigger_text, trigger_type, is_active, sort_order
            FROM handoff_rules
            WHERE tenant_id = %s
            ORDER BY sort_order ASC, id ASC
        """, (tenant_id,))
        rows = cur.fetchall() or []
        cur.close(); conn.close()
        return rows
    except Exception as e:
        print("⚠️ _get_handoff_rules error:", e)
        return []


def _seed_default_rules(tenant_id: int) -> None:
    """Insert the default rule set for a tenant that has no rules yet."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        for text, ttype, active, order in _DEFAULT_HANDOFF_RULES:
            cur.execute("""
                INSERT INTO handoff_rules
                    (tenant_id, trigger_text, trigger_type, is_active, sort_order)
                VALUES (%s, %s, %s, %s, %s)
            """, (tenant_id, text, ttype, active, order))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ _seed_default_rules error:", e)


@portal_bp.route("/handoff-rules", methods=["GET"])
def handoff_rules():
    r = _require_login()
    if r: return r

    customer  = _get_customer(_customer_id())
    if not customer:
        session.clear()
        flash("Your account could not be loaded. Please log in again.", "danger")
        return redirect(url_for("portal.login"))

    tenant_id = int(customer["tenant_id"])

    # Seed defaults if this tenant has never visited the page
    rules = _get_handoff_rules(tenant_id)
    if not rules:
        _seed_default_rules(tenant_id)
        rules = _get_handoff_rules(tenant_id)

    # Split for display
    visitor_rules = [r for r in rules if r["trigger_type"] == "visitor_initiated"]
    ai_rules      = [r for r in rules if r["trigger_type"] == "ai_initiated"]

    return render_template(
        "portal/handoff_rules.html",
        customer      = customer,
        visitor_rules = visitor_rules,
        ai_rules      = ai_rules,
    )


@portal_bp.route("/handoff-rules/add", methods=["POST"])
def handoff_rules_add():
    r = _require_login()
    if r: return r

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    trigger_text = (request.form.get("trigger_text") or "").strip()
    trigger_type = (request.form.get("trigger_type") or "ai_initiated").strip()

    if not trigger_text:
        flash("Please enter a trigger description.", "danger")
        return redirect(url_for("portal.handoff_rules"))

    if trigger_type not in ("visitor_initiated", "ai_initiated"):
        trigger_type = "ai_initiated"

    # Cap length for safety
    trigger_text = trigger_text[:280]

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        # Place new rule at the end
        cur.execute("SELECT COALESCE(MAX(sort_order),0) AS m FROM handoff_rules WHERE tenant_id=%s",
                    (tenant_id,))
        max_order = int((conn.cursor(dictionary=True, buffered=True) and 0) or 0)
        # Simpler: just use 999 so it always goes to the bottom
        cur.execute("""
            INSERT INTO handoff_rules
                (tenant_id, trigger_text, trigger_type, is_active, sort_order)
            VALUES (%s, %s, %s, 1, 999)
        """, (tenant_id, trigger_text, trigger_type))
        conn.commit()
        cur.close(); conn.close()
        flash("Rule added ✅", "success")
    except Exception as e:
        print("⚠️ handoff_rules_add error:", e)
        flash("Could not add rule. Please try again.", "danger")

    return redirect(url_for("portal.handoff_rules"))


@portal_bp.route("/handoff-rules/<int:rule_id>/toggle", methods=["POST"])
def handoff_rules_toggle(rule_id: int):
    r = _require_login()
    if r: return r

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        # Security: only update rules that belong to this tenant
        cur.execute("""
            UPDATE handoff_rules
            SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
            WHERE id = %s AND tenant_id = %s
        """, (rule_id, tenant_id))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ handoff_rules_toggle error:", e)
        flash("Could not update rule.", "danger")

    return redirect(url_for("portal.handoff_rules"))


@portal_bp.route("/handoff-rules/<int:rule_id>/delete", methods=["POST"])
def handoff_rules_delete(rule_id: int):
    r = _require_login()
    if r: return r

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        # Security: only delete rules that belong to this tenant
        cur.execute(
            "DELETE FROM handoff_rules WHERE id = %s AND tenant_id = %s",
            (rule_id, tenant_id)
        )
        conn.commit()
        cur.close(); conn.close()
        flash("Rule deleted.", "success")
    except Exception as e:
        print("⚠️ handoff_rules_delete error:", e)
        flash("Could not delete rule.", "danger")

    return redirect(url_for("portal.handoff_rules"))


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

import base64 as _base64

ALLOWED_TIMEZONES = [
    "UTC", "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid",
    "Europe/Rome", "Europe/Amsterdam", "Europe/Brussels", "Europe/Zurich",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Toronto", "America/Vancouver", "America/Sao_Paulo", "America/Mexico_City",
    "Asia/Dubai", "Asia/Riyadh", "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo",
    "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Seoul", "Australia/Sydney",
    "Australia/Melbourne", "Pacific/Auckland", "Africa/Lagos", "Africa/Johannesburg",
]


@portal_bp.route("/settings", methods=["GET"])
def settings():
    r = _require_login()
    if r: return r
    customer = _get_customer(_customer_id())
    if not customer:
        session.clear()
        flash("Your account could not be loaded.", "danger")
        return redirect(url_for("portal.login"))

    tenant_id = int(customer["tenant_id"])
    keys      = _get_api_keys(tenant_id)
    balance_credits = tokens_to_credits(_get_tenant_balance_tokens(tenant_id))

    # Find the most relevant active key for the plan tab
    plan_key     = None
    plan_days_left = None
    _now = datetime.utcnow()
    for k in keys:
        if k.get("is_active"):
            plan_key = k
            if k.get("key_type") == "trial" and k.get("trial_expires_at"):
                diff = k["trial_expires_at"] - _now
                plan_days_left = max(0, diff.days)
            break
    # Fall back to most recent key even if inactive
    if not plan_key and keys:
        plan_key = keys[0]

    # Build feature labels from tenant features JSON
    _FEATURE_LABELS = {
        "product_recommendation":    "AI Product Recommendations",
        "related_products":          "Related Products",
        "cart_recovery":             "Cart Recovery Emails",
        "verified_specs_web_lookup": "Verified Specs Web Lookup",
        "chat_archive_unlimited":    "Chat Archive (Unlimited)",
        "chat_archive_30days":       "Chat Archive (30 days)",
    }
    plan_features = []
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
        row = cur.fetchone() or {}
        cur.close(); conn.close()
        import json as _j
        feat = _j.loads(row.get("features") or "{}") if isinstance(row.get("features"), str) else (row.get("features") or {})
        for k, label in _FEATURE_LABELS.items():
            if feat.get(k):
                plan_features.append(label)
    except Exception:
        pass

    return render_template("portal/settings.html",
                           customer=customer,
                           timezones=ALLOWED_TIMEZONES,
                           plan_key=plan_key,
                           plan_days_left=plan_days_left,
                           balance_credits=balance_credits,
                           plan_features=plan_features,
                           active_sub=_get_active_subscription(int(customer["id"])),
                           saved_methods=_get_saved_payment_methods(int(customer["id"])))


@portal_bp.route("/settings/profile", methods=["POST"])
def settings_profile():
    """Update first name, last name, phone number."""
    r = _require_login()
    if r: return r

    cid        = _customer_id()
    first_name = (request.form.get("first_name") or "").strip()
    last_name  = (request.form.get("last_name")  or "").strip()
    phone      = (request.form.get("phone_number") or "").strip()
    timezone   = (request.form.get("timezone") or "").strip()

    if not first_name or not last_name:
        flash("First and last name are required.", "danger")
        return redirect(url_for("portal.settings"))

    if not phone:
        flash("Mobile phone number is required and cannot be left blank.", "danger")
        return redirect(url_for("portal.settings"))

    if timezone not in ALLOWED_TIMEZONES:
        timezone = "UTC"

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute("""
            UPDATE customers
            SET first_name=%s, last_name=%s, phone_number=%s, timezone=%s
            WHERE id=%s
        """, (first_name, last_name, phone, timezone, cid))
        conn.commit()
        cur.close(); conn.close()
        insert_audit_log(action="settings_profile_updated", details={
            "customer_id": cid, "fields": ["first_name", "last_name", "phone_number", "timezone"]
        })
        flash("Profile updated successfully ✅", "success")
    except Exception as e:
        print("⚠️ settings_profile error:", e)
        flash("Could not save profile. Please try again.", "danger")

    return redirect(url_for("portal.settings"))


@portal_bp.route("/settings/password", methods=["POST"])
def settings_password():
    """Change customer password (requires current password verification)."""
    r = _require_login()
    if r: return r

    cid          = _customer_id()
    current_pw   = (request.form.get("current_password") or "").strip()
    new_pw       = (request.form.get("new_password")     or "").strip()
    confirm_pw   = (request.form.get("confirm_password") or "").strip()

    if not current_pw or not new_pw or not confirm_pw:
        flash("All password fields are required.", "danger")
        return redirect(url_for("portal.settings"))

    if new_pw != confirm_pw:
        flash("New passwords do not match.", "danger")
        return redirect(url_for("portal.settings"))

    if len(new_pw) < 8:
        flash("New password must be at least 8 characters.", "danger")
        return redirect(url_for("portal.settings"))

    # Verify current password
    customer = _get_customer(cid)
    if not verify_password(current_pw, customer.get("password_hash") or ""):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("portal.settings"))

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute("UPDATE customers SET password_hash=%s WHERE id=%s",
                    (hash_password(new_pw), cid))
        conn.commit()
        cur.close(); conn.close()
        insert_audit_log(action="settings_password_changed",
                         details={"customer_id": cid})
        flash("Password changed successfully ✅", "success")
    except Exception as e:
        print("⚠️ settings_password error:", e)
        flash("Could not update password. Please try again.", "danger")

    return redirect(url_for("portal.settings"))


@portal_bp.route("/settings/avatar", methods=["POST"])
def settings_avatar():
    """Upload or remove profile avatar (stored as base64 in DB)."""
    r = _require_login()
    if r: return r

    cid    = _customer_id()
    action = (request.form.get("action") or "upload").strip()

    if action == "remove":
        try:
            conn = get_db_connection()
            cur  = conn.cursor(buffered=True)
            cur.execute("UPDATE customers SET avatar_data=NULL WHERE id=%s", (cid,))
            conn.commit()
            cur.close(); conn.close()
            flash("Avatar removed.", "success")
        except Exception as e:
            print("⚠️ settings_avatar remove error:", e)
            flash("Could not remove avatar.", "danger")
        return redirect(url_for("portal.settings"))

    # Upload
    f = request.files.get("avatar")
    if not f or not f.filename:
        flash("Please select an image file.", "danger")
        return redirect(url_for("portal.settings"))

    # Validate type
    allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if f.content_type not in allowed_types:
        flash("Only JPEG, PNG, GIF, or WebP images are allowed.", "danger")
        return redirect(url_for("portal.settings"))

    data = f.read()
    # Limit to 2MB
    if len(data) > 2 * 1024 * 1024:
        flash("Avatar image must be under 2 MB.", "danger")
        return redirect(url_for("portal.settings"))

    b64 = _base64.b64encode(data).decode("utf-8")
    data_uri = f"data:{f.content_type};base64,{b64}"

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute("UPDATE customers SET avatar_data=%s WHERE id=%s", (data_uri, cid))
        conn.commit()
        cur.close(); conn.close()
        flash("Avatar updated ✅", "success")
    except Exception as e:
        print("⚠️ settings_avatar upload error:", e)
        flash("Could not save avatar. Please try again.", "danger")

    return redirect(url_for("portal.settings"))


@portal_bp.route("/settings/notifications", methods=["POST"])
def settings_notifications():
    """Update notification preferences."""
    r = _require_login()
    if r: return r

    cid             = _customer_id()
    notif_billing   = 1 if request.form.get("notif_billing")   else 0
    notif_usage     = 1 if request.form.get("notif_usage")     else 0
    notif_marketing = 1 if request.form.get("notif_marketing") else 0
    notif_handoff   = 1 if request.form.get("notif_handoff")   else 0

    # Custom handoff alert email — strip whitespace, store NULL if blank
    import re as _re_email_notif
    raw_handoff_email = (request.form.get("handoff_notify_email") or "").strip().lower()
    if raw_handoff_email and not _re_email_notif.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", raw_handoff_email):
        flash("Please enter a valid email address for handoff alerts, or leave it blank.", "danger")
        return redirect(url_for("portal.settings") + "#notifications")
    handoff_notify_email = raw_handoff_email or None

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)

        # Try saving with new handoff columns; if migration hasn't run yet,
        # fall back gracefully to saving just the original three.
        try:
            cur.execute("""
                UPDATE customers
                SET notif_billing=%s, notif_usage=%s, notif_marketing=%s,
                    notif_handoff=%s, handoff_notify_email=%s
                WHERE id=%s
            """, (notif_billing, notif_usage, notif_marketing,
                  notif_handoff, handoff_notify_email, cid))
        except Exception:
            cur.execute("""
                UPDATE customers
                SET notif_billing=%s, notif_usage=%s, notif_marketing=%s
                WHERE id=%s
            """, (notif_billing, notif_usage, notif_marketing, cid))

        conn.commit()
        cur.close(); conn.close()
        flash("Notification preferences saved ✅", "success")
    except Exception as e:
        print("⚠️ settings_notifications error:", e)
        flash("Could not save notification preferences.", "danger")

    return redirect(url_for("portal.settings") + "#notifications")


@portal_bp.route("/settings/plan")
def settings_plan():
    """Package plan info page — redirects to settings with #plan tab."""
    return redirect(url_for("portal.settings") + "#plan")


@portal_bp.route("/settings/cancel-plan", methods=["POST"])
def settings_cancel_plan():
    """
    Customer requests plan cancellation.
    - Trial keys: deactivated immediately.
    - Paid keys: flagged as cancellation-requested (admin can action).
      We do NOT immediately revoke paid keys so the customer keeps access
      until the end of their paid period.
    """
    r = _require_login()
    if r: return r

    cid      = _customer_id()
    customer = _get_customer(cid)
    if not customer:
        flash("Account not found.", "danger")
        return redirect(url_for("portal.settings"))

    tenant_id = int(customer["tenant_id"])
    reason    = (request.form.get("cancel_reason") or "").strip()[:500]

    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)
    cur.execute("""
        SELECT id, key_type, is_active, trial_expires_at
        FROM api_keys WHERE tenant_id=%s AND is_active=1
        ORDER BY created_at DESC LIMIT 1
    """, (tenant_id,))
    key_row = cur.fetchone()

    if not key_row:
        cur.close(); conn.close()
        flash("No active plan found to cancel.", "warning")
        return redirect(url_for("portal.settings") + "#plan")

    key_id   = int(key_row["id"])
    key_type = key_row.get("key_type") or "paid"

    if key_type == "trial":
        # Deactivate trial immediately
        cur2 = conn.cursor(buffered=True)
        cur2.execute("UPDATE api_keys SET is_active=0 WHERE id=%s", (key_id,))
        conn.commit()
        cur2.close()
        insert_audit_log(
            admin_username=f"customer:{customer['email']}",
            action="trial_cancelled_by_customer",
            tenant_id=tenant_id,
            api_key_id=key_id,
            details={"reason": reason},
        )
        flash("Your free trial has been cancelled. You can still log in but AI features are now disabled.", "success")
    else:
        # Stage 9: if the customer has an active subscription, set
        # cancel_at_period_end=1 so subscription_maintenance.py handles
        # deactivation automatically at period end.  The existing admin-email
        # path and audit log are preserved in all cases.
        active_sub_s9 = _get_active_subscription(int(customer["id"]))
        if active_sub_s9:
            sub_id_s9 = int(active_sub_s9["id"])
            period_end_s9 = active_sub_s9.get("current_period_end")
            try:
                conn2_s9 = get_db_connection()
                cur_s9   = conn2_s9.cursor(buffered=True)
                cur_s9.execute(
                    "UPDATE subscriptions SET cancel_at_period_end=1, "
                    "updated_at=UTC_TIMESTAMP() WHERE id=%s",
                    (sub_id_s9,)
                )
                conn2_s9.commit()
                cur_s9.close(); conn2_s9.close()
            except Exception as _sub_e:
                print("⚠️ settings_cancel_plan: subscription update failed:", _sub_e)

            insert_audit_log(
                admin_username=f"customer:{customer['email']}",
                action="subscription_cancel_at_period_end_set",
                tenant_id=tenant_id,
                api_key_id=key_id,
                details={"reason": reason, "sub_id": sub_id_s9},
            )

            # Send cancellation confirmation to customer
            try:
                end_str = period_end_s9.strftime("%d %B %Y") if period_end_s9 else "your next renewal date"
                _cancel_html = f"""
                <div style="font-family:Arial,sans-serif;max-width:520px">
                  <h2 style="color:#030C18">Cancellation confirmed</h2>
                  <p>Hi {customer.get('first_name') or 'there'},</p>
                  <p>Your subscription will remain active until <b>{end_str}</b>,
                     after which your AI assistant will be paused automatically.</p>
                  <p>If you change your mind before then, you can resubscribe from your
                     <a href="{_PORTAL_BASE_URL}/billing/subscribe">billing page</a>.</p>
                  <p style="color:#6b7280;font-size:12px">
                    Questions? Contact
                    <a href="mailto:support@phixtra.com">support@phixtra.com</a>
                  </p>
                </div>"""
                send_email(
                    customer["email"],
                    "PhiXtra subscription cancellation confirmed",
                    _cancel_html,
                )
            except Exception as _em:
                print("⚠️ cancellation customer email failed:", _em)

            # Also notify admin (preserved from original)
            try:
                send_email(
                    "support@phixtra.com",
                    f"Subscription cancellation: {customer['email']}",
                    f"""<div style="font-family:Arial,sans-serif;max-width:520px">
                    <h2 style="color:#030C18">⚠️ Subscription Cancellation Requested</h2>
                    <p><b>Customer:</b> {customer.get('first_name','')} {customer.get('last_name','')}</p>
                    <p><b>Email:</b> {customer['email']}</p>
                    <p><b>Domain:</b> {customer.get('tenant_domain','')}</p>
                    <p><b>Reason:</b> {reason or '(no reason given)'}</p>
                    <p style="color:#6b7280;font-size:12px">
                      Access ends automatically at period end — no manual action needed.
                    </p></div>""",
                )
            except Exception as _ae:
                print("⚠️ cancellation admin email failed:", _ae)

            period_str = period_end_s9.strftime("%d %B %Y") if period_end_s9 else "your renewal date"
            flash(
                f"Cancellation confirmed ✅ Your plan will remain active until "
                f"{period_str}, then cancel automatically. "
                f"No further charges will be made.",
                "success",
            )
        else:
            # No subscription row — fall back to the original manual flow
            insert_audit_log(
                admin_username=f"customer:{customer['email']}",
                action="paid_plan_cancellation_requested",
                tenant_id=tenant_id,
                api_key_id=key_id,
                details={"reason": reason, "email": customer["email"]},
            )
            try:
                send_email(
                    "support@phixtra.com",
                    f"Plan cancellation request: {customer['email']}",
                    f"""<div style="font-family:Arial,sans-serif;max-width:520px">
                    <h2 style="color:#030C18">⚠️ Plan Cancellation Request</h2>
                    <p><b>Customer:</b> {customer.get('first_name','')} {customer.get('last_name','')}</p>
                    <p><b>Email:</b> {customer['email']}</p>
                    <p><b>Domain:</b> {customer.get('tenant_domain','')}</p>
                    <p><b>Reason:</b> {reason or '(no reason given)'}</p>
                    <p style="color:#888;font-size:12px">
                      Action required: review and process cancellation in admin portal.
                    </p></div>""",
                )
            except Exception as _e:
                print("⚠️ cancellation admin email failed:", _e)

            flash(
                "Cancellation request submitted ✅ Our team will process it within 1–2 business days. "
                "You will continue to have full access until then.",
                "success",
            )

    cur.close(); conn.close()
    return redirect(url_for("portal.settings") + "#plan")


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 4 — SAVE CARD (Stripe Elements embedded, no redirect)
# New routes only. Nothing above this line is touched.
# ══════════════════════════════════════════════════════════════════════════════

def _get_saved_payment_methods(customer_id: int) -> list:
    """Return all saved cards for a customer, default card first.
    Never raises — returns [] on any error."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT id, stripe_payment_method, card_brand, card_last4,
                   card_exp_month, card_exp_year, is_default
            FROM saved_payment_methods
            WHERE customer_id=%s
            ORDER BY is_default DESC, created_at DESC
        """, (customer_id,))
        rows = cur.fetchall() or []
        cur.close(); conn.close()
        return rows
    except Exception as e:
        print("⚠️ _get_saved_payment_methods error:", e)
        return []


def _get_default_payment_method(customer_id: int) -> dict | None:
    """Return the default saved card row, or None if none saved."""
    methods = _get_saved_payment_methods(customer_id)
    for m in methods:
        if int(m.get("is_default") or 0):
            return m
    return methods[0] if methods else None


@portal_bp.route("/billing/add-card", methods=["GET"])
def billing_add_card():
    """
    Stage 4 — Show the embedded Stripe card-save form.
    Creates a Stripe SetupIntent and passes the client_secret to the template
    so Stripe Elements can collect and save the card without any redirect.
    """
    r = _require_login()
    if r: return r

    if not _stripe_ok():
        flash("Card saving is not available right now. Contact support.", "warning")
        return redirect(url_for("portal.billing"))

    customer = _get_customer(_customer_id())
    if not customer:
        session.clear()
        return redirect(url_for("portal.login"))

    # Ensure this customer has a Stripe Customer object (Stage 2 helper)
    stripe_cus_id = _get_or_create_stripe_customer(customer)
    if not stripe_cus_id:
        flash("Could not initialise payment setup. Please try again.", "danger")
        return redirect(url_for("portal.billing"))

    try:
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
        setup_intent = stripe.SetupIntent.create(
            customer=stripe_cus_id,
            payment_method_types=["card"],
            usage="off_session",   # card will be used for future charges
        )
        client_secret = setup_intent["client_secret"]
    except Exception as e:
        print("⚠️ billing_add_card SetupIntent error:", e)
        flash("Could not start card setup. Please try again.", "danger")
        return redirect(url_for("portal.billing"))

    stripe_pub_key = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    saved_methods  = _get_saved_payment_methods(int(customer["id"]))

    return render_template(
        "portal/add_card.html",
        customer        = customer,
        client_secret   = client_secret,
        stripe_pub_key  = stripe_pub_key,
        saved_methods   = saved_methods,
    )


@portal_bp.route("/billing/save-card", methods=["POST"])
def billing_save_card():
    """
    Stage 4 — Called by the Stripe Elements JS after the card is confirmed.
    The JS sends the PaymentMethod ID here; we retrieve it from Stripe,
    save the card details to saved_payment_methods, and set it as default.
    """
    r = _require_login()
    if r: return r

    if not _stripe_ok():
        return jsonify({"ok": False, "error": "Not configured"}), 400

    customer    = _get_customer(_customer_id())
    if not customer:
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    customer_id = int(customer["id"])
    pm_id       = (request.json or {}).get("payment_method_id", "").strip()

    if not pm_id:
        return jsonify({"ok": False, "error": "No payment method provided"}), 400

    try:
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

        # Retrieve the PaymentMethod to get card details
        pm = stripe.PaymentMethod.retrieve(pm_id)
        card        = pm.get("card") or {}
        brand       = card.get("brand", "")
        last4       = card.get("last4", "")
        exp_month   = card.get("exp_month")
        exp_year    = card.get("exp_year")

        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)

        # If this is the customer's first card, make it default
        cur.execute(
            "SELECT COUNT(*) AS c FROM saved_payment_methods WHERE customer_id=%s",
            (customer_id,)
        )
        existing_count = int((cur.fetchone() or (0,))[0])
        is_default = 1 if existing_count == 0 else 0

        # Upsert — if same PaymentMethod is somehow submitted twice, update it
        cur.execute("""
            INSERT INTO saved_payment_methods
                (customer_id, stripe_payment_method, card_brand, card_last4,
                 card_exp_month, card_exp_year, is_default)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                card_brand=%s, card_last4=%s,
                card_exp_month=%s, card_exp_year=%s
        """, (
            customer_id, pm_id, brand, last4, exp_month, exp_year, is_default,
            brand, last4, exp_month, exp_year,
        ))
        conn.commit()
        cur.close(); conn.close()

        insert_audit_log(
            action="card_saved",
            details={"customer_id": customer_id, "brand": brand, "last4": last4},
        )

        return jsonify({"ok": True, "redirect": url_for("portal.billing_add_card")})

    except Exception as e:
        print("⚠️ billing_save_card error:", e)
        return jsonify({"ok": False, "error": "Could not save card. Please try again."}), 500


@portal_bp.route("/billing/remove-card/<int:method_id>", methods=["POST"])
def billing_remove_card(method_id: int):
    """
    Stage 4 — Remove a saved card.
    Only the owning customer can remove their own cards.
    If the removed card was the default, the next card (if any) becomes default.
    """
    r = _require_login()
    if r: return r

    customer    = _get_customer(_customer_id())
    customer_id = int(customer["id"])

    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)

    # Security: only touch rows belonging to this customer
    cur.execute(
        "SELECT id, stripe_payment_method, is_default FROM saved_payment_methods "
        "WHERE id=%s AND customer_id=%s",
        (method_id, customer_id)
    )
    row = cur.fetchone()

    if not row:
        cur.close(); conn.close()
        flash("Card not found.", "danger")
        return redirect(url_for("portal.billing_add_card"))

    was_default  = int(row.get("is_default") or 0)
    pm_id        = row.get("stripe_payment_method", "")

    cur2 = conn.cursor(buffered=True)
    cur2.execute(
        "DELETE FROM saved_payment_methods WHERE id=%s AND customer_id=%s",
        (method_id, customer_id)
    )
    conn.commit()

    # If it was the default, promote the next card
    if was_default:
        cur2.execute("""
            UPDATE saved_payment_methods SET is_default=1
            WHERE customer_id=%s
            ORDER BY created_at DESC LIMIT 1
        """, (customer_id,))
        conn.commit()

    cur2.close(); cur.close(); conn.close()

    # Also detach from Stripe so it cannot be charged again
    if pm_id and _stripe_ok():
        try:
            stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
            stripe.PaymentMethod.detach(pm_id)
        except Exception as e:
            print("⚠️ billing_remove_card detach error:", e)

    insert_audit_log(
        action="card_removed",
        details={"customer_id": customer_id, "method_id": method_id},
    )
    flash("Card removed.", "success")
    return redirect(url_for("portal.billing_add_card"))


@portal_bp.route("/billing/set-default-card/<int:method_id>", methods=["POST"])
def billing_set_default_card(method_id: int):
    """
    Stage 4 — Set a saved card as the default for future charges.
    """
    r = _require_login()
    if r: return r

    customer    = _get_customer(_customer_id())
    customer_id = int(customer["id"])

    conn = get_db_connection()
    cur  = conn.cursor(buffered=True)

    # Verify ownership
    cur.execute(
        "SELECT id FROM saved_payment_methods WHERE id=%s AND customer_id=%s",
        (method_id, customer_id)
    )
    if not cur.fetchone():
        cur.close(); conn.close()
        flash("Card not found.", "danger")
        return redirect(url_for("portal.billing_add_card"))

    # Clear current default, then set new one
    cur.execute(
        "UPDATE saved_payment_methods SET is_default=0 WHERE customer_id=%s",
        (customer_id,)
    )
    cur.execute(
        "UPDATE saved_payment_methods SET is_default=1 WHERE id=%s AND customer_id=%s",
        (method_id, customer_id)
    )
    conn.commit()
    cur.close(); conn.close()

    flash("Default card updated ✅", "success")
    return redirect(url_for("portal.billing_add_card"))


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 5 — SUBSCRIPTION PURCHASE
# New routes only. Nothing above this line is touched.
# ══════════════════════════════════════════════════════════════════════════════

def _get_active_subscription(customer_id: int) -> dict | None:
    """Return the customer's active subscription row (joined with plan name),
    or None if they have no active subscription. Never raises."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT s.*, cp.name AS plan_name, cp.credits AS plan_credits,
                   cp.price_pence AS plan_price_pence, cp.billing_period,
                   cp.currency
            FROM subscriptions s
            JOIN credit_packages cp ON cp.id = s.package_id
            WHERE s.customer_id=%s
              AND s.status IN ('active','past_due')
            ORDER BY s.created_at DESC
            LIMIT 1
        """, (customer_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return row
    except Exception as e:
        print("⚠️ _get_active_subscription error:", e)
        return None


def _charge_saved_card(stripe_cus_id: str, pm_id: str,
                       amount_pence: int, currency: str,
                       description: str, metadata: dict) -> dict:
    """
    Create and confirm a Stripe PaymentIntent against a saved card.
    Returns the PaymentIntent object on success.
    Raises on failure — callers must catch.
    """
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    pi = stripe.PaymentIntent.create(
        amount              = amount_pence,
        currency            = currency,
        customer            = stripe_cus_id,
        payment_method      = pm_id,
        description         = description,
        metadata            = metadata,
        confirm             = True,
        off_session         = True,   # customer is not present
        payment_method_types= ["card"],
    )
    return pi


@portal_bp.route("/billing/subscribe", methods=["GET"])
def billing_subscribe():
    """
    Stage 5 — Show available subscription plans and current subscription status.
    """
    r = _require_login()
    if r: return r

    customer    = _get_customer(_customer_id())
    if not customer:
        session.clear()
        return redirect(url_for("portal.login"))

    customer_id = int(customer["id"])
    tenant_id   = int(customer["tenant_id"])

    # Load subscription plans (active only)
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)
    cur.execute("""
        SELECT * FROM credit_packages
        WHERE is_active=1 AND package_type='subscription'
        ORDER BY sort_order ASC, price_pence ASC
    """)
    plans = cur.fetchall() or []
    cur.close(); conn.close()

    import json as _j
    for p in plans:
        raw = p.get("features")
        try:
            p["features_parsed"] = _j.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            p["features_parsed"] = {}
        p["price_fmt"] = money_fmt(int(p.get("price_pence") or 0), p.get("currency") or "gbp")

    active_sub    = _get_active_subscription(customer_id)
    saved_methods = _get_saved_payment_methods(customer_id)
    balance_credits = tokens_to_credits(_get_tenant_balance_tokens(tenant_id))
    stripe_pub_key  = os.getenv("STRIPE_PUBLISHABLE_KEY", "")

    return render_template(
        "portal/subscribe.html",
        customer        = customer,
        plans           = plans,
        active_sub      = active_sub,
        saved_methods   = saved_methods,
        balance_credits = balance_credits,
        stripe_ready    = _stripe_ok(),
        stripe_pub_key  = stripe_pub_key,
    )


@portal_bp.route("/billing/subscribe", methods=["POST"])
def billing_subscribe_post():
    """
    Stage 5 — Process subscription purchase.

    Flow:
    1. Validate plan and saved card exist
    2. Charge the card for the first period via PaymentIntent
    3. On success: create subscriptions row, top up credits, convert trial key
    4. Generate subscription invoice PDF, send receipt email
    5. On failure: show error, customer keeps current state
    """
    r = _require_login()
    if r: return r

    if not _stripe_ok():
        flash("Payments are not configured. Contact support.", "warning")
        return redirect(url_for("portal.billing_subscribe"))

    customer    = _get_customer(_customer_id())
    if not customer:
        session.clear()
        return redirect(url_for("portal.login"))

    customer_id = int(customer["id"])
    tenant_id   = int(customer["tenant_id"])

    plan_id   = int(request.form.get("plan_id") or 0)
    method_id = int(request.form.get("payment_method_id") or 0)

    # ── Validate plan ────────────────────────────────────────────────────────
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)
    cur.execute("""
        SELECT * FROM credit_packages
        WHERE id=%s AND is_active=1 AND package_type='subscription'
    """, (plan_id,))
    plan = cur.fetchone()

    if not plan:
        cur.close(); conn.close()
        flash("Invalid plan selected. Please try again.", "danger")
        return redirect(url_for("portal.billing_subscribe"))

    # ── Validate saved card ──────────────────────────────────────────────────
    cur.execute("""
        SELECT id, stripe_payment_method
        FROM saved_payment_methods
        WHERE id=%s AND customer_id=%s
    """, (method_id, customer_id))
    pm_row = cur.fetchone()

    if not pm_row:
        cur.close(); conn.close()
        flash("Payment card not found. Please add a card first.", "danger")
        return redirect(url_for("portal.billing_add_card"))

    cur.close(); conn.close()

    # ── Check not already subscribed to this exact plan ──────────────────────
    active_sub = _get_active_subscription(customer_id)
    if active_sub and int(active_sub.get("package_id") or 0) == plan_id:
        flash("You are already subscribed to this plan.", "info")
        return redirect(url_for("portal.billing_subscribe"))

    # ── Resolve Stripe Customer ──────────────────────────────────────────────
    stripe_cus_id = _get_or_create_stripe_customer(customer)
    if not stripe_cus_id:
        flash("Could not verify your billing account. Please try again.", "danger")
        return redirect(url_for("portal.billing_subscribe"))

    credits      = int(plan["credits"])
    amount_pence = int(plan["price_pence"])
    currency     = plan.get("currency") or "gbp"
    billing_period = plan.get("billing_period") or "monthly"
    pm_stripe_id = pm_row["stripe_payment_method"]
    inv_num      = next_invoice_number()

    # ── Charge the card ──────────────────────────────────────────────────────
    try:
        pi = _charge_saved_card(
            stripe_cus_id = stripe_cus_id,
            pm_id         = pm_stripe_id,
            amount_pence  = amount_pence,
            currency      = currency,
            description   = f"PhiXtra {plan['name']} subscription",
            metadata      = {
                "customer_id":  str(customer_id),
                "tenant_id":    str(tenant_id),
                "plan_id":      str(plan_id),
                "credits":      str(credits),
                "invoice_num":  inv_num,
            },
        )
    except Exception as e:
        print("⚠️ billing_subscribe_post charge failed:", e)
        flash(
            "Payment failed — your card was declined or an error occurred. "
            "Please check your card details and try again.",
            "danger",
        )
        return redirect(url_for("portal.billing_subscribe"))

    # Payment succeeded — now record everything
    # ── Calculate subscription period ────────────────────────────────────────
    now = datetime.utcnow()
    if billing_period == "annual":
        period_end = now + timedelta(days=365)
    else:
        period_end = now + timedelta(days=30)

    conn = get_db_connection()
    cur  = conn.cursor(buffered=True)

    try:
        # ── Create subscription row ──────────────────────────────────────────
        cur.execute("""
            INSERT INTO subscriptions
                (customer_id, tenant_id, package_id, payment_method_id,
                 status, current_period_start, current_period_end,
                 cancel_at_period_end)
            VALUES (%s, %s, %s, %s, 'active', %s, %s, 0)
        """, (customer_id, tenant_id, plan_id, method_id, now, period_end))
        subscription_id = cur.lastrowid

        # ── Create subscription invoice row ──────────────────────────────────
        cur.execute("""
            INSERT INTO subscription_invoices
                (invoice_number, subscription_id, customer_id, tenant_id,
                 package_id, credits, amount_pence, vat_pence, currency,
                 status, period_start, period_end, stripe_payment_intent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, 'paid', %s, %s, %s)
        """, (
            inv_num, subscription_id, customer_id, tenant_id,
            plan_id, credits, amount_pence, currency,
            now, period_end, pi["id"],
        ))

        # ── Top up credit balance ─────────────────────────────────────────────
        tokens_add = credits_to_tokens(credits)
        cur.execute(
            "INSERT IGNORE INTO tenant_balances (tenant_id, token_balance) VALUES (%s, 0)",
            (tenant_id,)
        )
        cur.execute(
            "UPDATE tenant_balances SET token_balance = token_balance + %s WHERE tenant_id=%s",
            (tokens_add, tenant_id)
        )

        # ── Convert trial key to paid (same logic as existing top-up webhook) ─
        # api_key_plain is already stored from trial creation; no change needed.
        cur.execute("""
            UPDATE api_keys
            SET key_type='paid', is_active=1, trial_expires_at=NULL
            WHERE tenant_id=%s AND key_type='trial'
        """, (tenant_id,))
        was_trial = cur.rowcount > 0

        # Reactivate any existing paid keys too
        cur.execute(
            "UPDATE api_keys SET is_active=1 WHERE tenant_id=%s AND key_type='paid'",
            (tenant_id,)
        )

        conn.commit()

    except Exception as e:
        conn.rollback()
        cur.close(); conn.close()
        print("⚠️ billing_subscribe_post DB error:", e)
        # Payment went through but DB failed — log it prominently
        insert_audit_log(
            action="subscription_db_error_after_charge",
            tenant_id=tenant_id,
            details={
                "error": str(e),
                "payment_intent": pi.get("id"),
                "customer_id": customer_id,
                "plan_id": plan_id,
            },
        )
        flash(
            "Payment was taken but we encountered an error activating your plan. "
            "Please contact support@phixtra.com immediately with reference: "
            f"{inv_num}",
            "danger",
        )
        return redirect(url_for("portal.billing_subscribe"))

    cur.close(); conn.close()

    # ── Generate invoice PDF ─────────────────────────────────────────────────
    try:
        pdf_path = generate_invoice_pdf(
            invoice_number = inv_num,
            customer_email = customer.get("email") or "",
            tenant_name    = customer.get("tenant_name") or "",
            credits        = credits,
            amount_pence   = amount_pence,
            vat_pence      = 0,
            currency       = currency,
            created_at     = now,
        )
        # Save PDF path back to the subscription invoice row
        conn2 = get_db_connection()
        cur2  = conn2.cursor(buffered=True)
        cur2.execute(
            "UPDATE subscription_invoices SET pdf_path=%s WHERE invoice_number=%s",
            (pdf_path, inv_num)
        )
        conn2.commit()
        cur2.close(); conn2.close()
    except Exception as e:
        print("⚠️ billing_subscribe_post PDF error:", e)
        pdf_path = None

    # ── Audit log ────────────────────────────────────────────────────────────
    insert_audit_log(
        action    = "subscription_created",
        tenant_id = tenant_id,
        details   = {
            "plan": plan.get("name"),
            "billing_period": billing_period,
            "credits": credits,
            "amount_pence": amount_pence,
            "invoice": inv_num,
            "was_trial": was_trial,
        },
    )
    if was_trial:
        insert_audit_log(
            action    = "trial_converted_to_paid",
            tenant_id = tenant_id,
            details   = {"converted_by": "subscription", "invoice": inv_num},
        )

    # ── Send receipt email ───────────────────────────────────────────────────
    try:
        email = customer.get("email")
        name  = (customer.get("first_name") or "there").strip()
        if email:
            period_label = "year" if billing_period == "annual" else "month"
            end_str = period_end.strftime("%d %B %Y")
            subject = "PhiXtra subscription activated ✅"
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:520px">
              <h2 style="color:{BRAND}">Subscription activated 🎉</h2>
              <p>Hi {name},</p>
              <p>Your <b>{plan['name']}</b> plan is now active.</p>
              <table style="width:100%;border-collapse:collapse;margin:16px 0">
                <tr>
                  <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700;width:140px">Plan</td>
                  <td style="padding:8px 12px;border:1px solid #e5e7eb">{plan['name']} ({billing_period})</td>
                </tr>
                <tr>
                  <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700">Credits added</td>
                  <td style="padding:8px 12px;border:1px solid #e5e7eb">{credits} credits</td>
                </tr>
                <tr>
                  <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700">Amount charged</td>
                  <td style="padding:8px 12px;border:1px solid #e5e7eb">{money_fmt(amount_pence, currency)}</td>
                </tr>
                <tr>
                  <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700">Next renewal</td>
                  <td style="padding:8px 12px;border:1px solid #e5e7eb">{end_str}</td>
                </tr>
              </table>
              <p>
                <a href="{_PORTAL_BASE_URL}/billing/subscribe"
                   style="background:{BRAND};color:#fff;padding:10px 18px;border-radius:12px;
                          text-decoration:none;display:inline-block">
                  View subscription
                </a>
              </p>
            </div>"""
            send_email(email, subject, html)
    except Exception:
        pass

    flash(
        f"✅ Subscription activated! {credits} credits have been added to your account.",
        "success",
    )
    return redirect(url_for("portal.billing_subscribe"))


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 8 — PLAN SWITCHING
# New route only. Nothing above this line is touched.
# ══════════════════════════════════════════════════════════════════════════════

@portal_bp.route("/billing/switch-plan", methods=["POST"])
def billing_switch_plan():
    """
    Stage 8 — Switch a customer from their current subscription plan to a new one.

    Design decisions (safe and simple):
    - No immediate charge when switching. The new plan price takes effect at
      the NEXT renewal — handled automatically by subscription_maintenance.py.
    - If switching to a plan with MORE credits than the current one (upgrade),
      the extra credits are added to the balance immediately.
    - If switching to a plan with FEWER credits (downgrade), no credits are
      removed. The lower credit allocation simply applies at next renewal.
    - The subscription row is updated immediately so the customer sees the
      new plan name on their billing page right away.
    """
    r = _require_login()
    if r: return r

    customer    = _get_customer(_customer_id())
    if not customer:
        session.clear()
        return redirect(url_for("portal.login"))

    customer_id = int(customer["id"])
    tenant_id   = int(customer["tenant_id"])
    new_plan_id = int(request.form.get("new_plan_id") or 0)

    # ── Must have an active subscription to switch ────────────────────────────
    active_sub = _get_active_subscription(customer_id)
    if not active_sub:
        flash("You don't have an active subscription to switch.", "warning")
        return redirect(url_for("portal.billing_subscribe"))

    sub_id = int(active_sub["id"])

    # ── Validate the new plan ─────────────────────────────────────────────────
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)
    cur.execute("""
        SELECT * FROM credit_packages
        WHERE id=%s AND is_active=1 AND package_type='subscription'
    """, (new_plan_id,))
    new_plan = cur.fetchone()
    cur.close(); conn.close()

    if not new_plan:
        flash("Invalid plan selected.", "danger")
        return redirect(url_for("portal.billing_subscribe"))

    # ── Already on this plan? ─────────────────────────────────────────────────
    if int(active_sub.get("package_id") or 0) == new_plan_id:
        flash("You are already on this plan.", "info")
        return redirect(url_for("portal.billing_subscribe"))

    current_credits = int(active_sub.get("plan_credits") or 0)
    new_credits     = int(new_plan.get("credits") or 0)
    extra_credits   = max(0, new_credits - current_credits)   # 0 on downgrade

    conn = get_db_connection()
    cur  = conn.cursor(buffered=True)
    try:
        # Update the subscription to the new plan
        cur.execute("""
            UPDATE subscriptions
            SET package_id=%s, updated_at=UTC_TIMESTAMP()
            WHERE id=%s AND customer_id=%s
        """, (new_plan_id, sub_id, customer_id))

        # On upgrade: immediately top up with the extra credits
        if extra_credits > 0:
            extra_tokens = credits_to_tokens(extra_credits)
            cur.execute(
                "UPDATE tenant_balances SET token_balance = token_balance + %s WHERE tenant_id=%s",
                (extra_tokens, tenant_id)
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close(); conn.close()
        print("⚠️ billing_switch_plan DB error:", e)
        flash("Could not switch plan. Please try again or contact support.", "danger")
        return redirect(url_for("portal.billing_subscribe"))

    cur.close(); conn.close()

    old_plan_name = active_sub.get("plan_name") or "previous plan"
    new_plan_name = new_plan.get("name") or "new plan"

    insert_audit_log(
        action    = "subscription_plan_switched",
        tenant_id = tenant_id,
        details   = {
            "from_plan": old_plan_name,
            "to_plan":   new_plan_name,
            "extra_credits_added": extra_credits,
            "sub_id": sub_id,
        },
    )

    # Send confirmation email
    try:
        email = customer.get("email")
        name  = (customer.get("first_name") or "there").strip()
        if email:
            billing_period = new_plan.get("billing_period") or "monthly"
            price_fmt = money_fmt(
                int(new_plan.get("price_pence") or 0),
                new_plan.get("currency") or "gbp"
            )
            renew_str = ""
            if active_sub.get("current_period_end"):
                renew_str = active_sub["current_period_end"].strftime("%d %B %Y")

            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:520px">
              <h2 style="color:{BRAND}">Plan switched ✅</h2>
              <p>Hi {name},</p>
              <p>Your subscription has been switched to <b>{new_plan_name}</b>.</p>
              <table style="width:100%;border-collapse:collapse;margin:16px 0">
                <tr>
                  <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;
                              font-weight:700;width:160px">New plan</td>
                  <td style="padding:8px 12px;border:1px solid #e5e7eb">
                    {new_plan_name} ({billing_period})</td>
                </tr>
                <tr>
                  <td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;
                              font-weight:700">New price</td>
                  <td style="padding:8px 12px;border:1px solid #e5e7eb">
                    {price_fmt}/{billing_period}</td>
                </tr>
                {'<tr><td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700">Credits added now</td><td style="padding:8px 12px;border:1px solid #e5e7eb">+' + str(extra_credits) + ' credits (upgrade bonus)</td></tr>' if extra_credits > 0 else ''}
                {('<tr><td style="padding:8px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700">Next renewal</td><td style="padding:8px 12px;border:1px solid #e5e7eb">' + renew_str + '</td></tr>') if renew_str else ''}
              </table>
              <p style="font-size:13px;color:#6b7280;">
                The new price applies from your next renewal date.
              </p>
              <p>
                <a href="{_PORTAL_BASE_URL}/billing/subscribe"
                   style="background:{BRAND};color:#fff;padding:10px 18px;
                          border-radius:12px;text-decoration:none;display:inline-block">
                  View subscription
                </a>
              </p>
            </div>"""
            send_email(email, f"PhiXtra plan switched to {new_plan_name}", html)
    except Exception:
        pass

    if extra_credits > 0:
        flash(
            f"✅ Switched to {new_plan_name}. "
            f"{extra_credits} bonus credits have been added to your balance immediately. "
            f"The new price applies from your next renewal.",
            "success",
        )
    else:
        flash(
            f"✅ Switched to {new_plan_name}. "
            f"The new price applies from your next renewal date.",
            "success",
        )
    return redirect(url_for("portal.billing_subscribe"))


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 10 — BUSINESS INFORMATION SAVE
# New route only. Nothing above this line is touched.
# ══════════════════════════════════════════════════════════════════════════════

@portal_bp.route("/settings/business", methods=["POST"])
def settings_business():
    """
    Stage 10 — Save business/billing information.
    Fields: company_name, vat_number, billing_address_line1,
            billing_city, billing_postcode, billing_country.
    All fields are optional. Stored on the customers row (columns
    added in Stage 1 migration). Never raises to the customer.
    """
    r = _require_login()
    if r: return r

    cid          = _customer_id()
    company_name = (request.form.get("company_name")          or "").strip()[:255]
    vat_number   = (request.form.get("vat_number")            or "").strip()[:50]
    addr_line1   = (request.form.get("billing_address_line1") or "").strip()[:255]
    addr_city    = (request.form.get("billing_city")          or "").strip()[:100]
    addr_post    = (request.form.get("billing_postcode")       or "").strip()[:20]
    addr_country = (request.form.get("billing_country")       or "GB").strip()[:10]

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute("""
            UPDATE customers
            SET company_name          = %s,
                vat_number            = %s,
                billing_address_line1 = %s,
                billing_city          = %s,
                billing_postcode      = %s,
                billing_country       = %s
            WHERE id = %s
        """, (
            company_name or None,
            vat_number   or None,
            addr_line1   or None,
            addr_city    or None,
            addr_post    or None,
            addr_country or "GB",
            cid,
        ))
        conn.commit()
        cur.close(); conn.close()
        insert_audit_log(
            action  = "settings_business_updated",
            details = {"customer_id": cid,
                       "fields": ["company_name", "vat_number",
                                  "billing_address_line1", "billing_city",
                                  "billing_postcode", "billing_country"]},
        )
        flash("Business information saved ✅", "success")
    except Exception as e:
        print("⚠️ settings_business error:", e)
        flash("Could not save business information. Please try again.", "danger")

    return redirect(url_for("portal.settings") + "#business")


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP — Connect, Agent Inbox, Template Management
# ══════════════════════════════════════════════════════════════════════════════

def _get_wa_connection(tenant_id: int) -> dict | None:
    """Return the active wa_tenants row for this tenant, or None."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT id, phone_number_id, waba_id, verify_token, active, created_at,
                   signup_method, display_phone_number, verified_name, token_expires_at,
                   app_secret
            FROM wa_tenants WHERE tenant_id = %s AND active = TRUE LIMIT 1
        """, (tenant_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return row
    except Exception as e:
        print("⚠️ _get_wa_connection error:", e)
        return None


def _get_wa_templates(tenant_id: int) -> dict:
    """Return tenant's configured wa_templates keyed by template_type."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT template_type, template_name, language_code
            FROM wa_templates WHERE tenant_id = %s AND active = TRUE
        """, (tenant_id,))
        rows = cur.fetchall() or []
        cur.close(); conn.close()
        return {r["template_type"]: r for r in rows}
    except Exception as e:
        print("⚠️ _get_wa_templates error:", e)
        return {}


def _send_wa_text_from_portal(phone_number_id: str, access_token: str,
                               to: str, text: str) -> bool:
    """Send a plain text WhatsApp message via Meta Graph API."""
    import requests as _req
    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    try:
        r = _req.post(
            url,
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print("⚠️ _send_wa_text_from_portal error:", e)
        return False


@portal_bp.route("/whatsapp")
def whatsapp_connect():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    connection = _get_wa_connection(tenant_id)
    templates  = _get_wa_templates(tenant_id)

    meta_app_id    = os.getenv("META_APP_ID", "")
    meta_config_id = os.getenv("META_CONFIG_ID", "")
    embedded_enabled = bool(meta_app_id and meta_config_id)

    webhook_url = os.getenv("META_WEBHOOK_URL", "")

    # Platform-wide verify token — same for all tenants.
    # Meta calls one webhook URL; routing is by phone_number_id in the payload.
    suggested_verify_token = os.getenv("WEBHOOK_VERIFY_TOKEN", "")

    # Token expiry warning state
    token_expiry_status = None  # None | 'ok' | 'expiring' | 'expired'
    if connection and connection.get("token_expires_at"):
        exp = connection["token_expires_at"]
        delta = exp - datetime.utcnow() if hasattr(exp, "year") else None
        if delta is not None:
            if delta.total_seconds() <= 0:
                token_expiry_status = "expired"
            elif delta.days < 14:
                token_expiry_status = "expiring"
            else:
                token_expiry_status = "ok"
    elif connection:
        token_expiry_status = "ok"  # Manual connections have no expiry

    return render_template("portal/whatsapp.html",
                           customer=customer,
                           connection=connection,
                           templates=templates,
                           embedded_enabled=embedded_enabled,
                           meta_app_id=meta_app_id,
                           meta_config_id=meta_config_id,
                           token_expiry_status=token_expiry_status,
                           webhook_url=webhook_url,
                           suggested_verify_token=suggested_verify_token)


@portal_bp.route("/whatsapp/connect", methods=["POST"])
def whatsapp_save_connection():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    phone_number_id = (request.form.get("phone_number_id") or "").strip()
    access_token    = (request.form.get("access_token")    or "").strip()
    waba_id         = (request.form.get("waba_id")         or "").strip()
    app_secret      = (request.form.get("app_secret")      or "").strip()
    phixtra_api_key = (request.form.get("phixtra_api_key") or "").strip()
    # Always use the platform-wide verify token — tenants don't manage this
    verify_token    = os.getenv("WEBHOOK_VERIFY_TOKEN", "")

    if not phone_number_id or not access_token:
        flash("Phone Number ID and Access Token are required.", "danger")
        return redirect(url_for("portal.whatsapp_connect"))

    # Require API key for new connections; allow blank on updates to keep existing
    if not phixtra_api_key and not connection:
        flash("PhiXtra API Key is required.", "danger")
        return redirect(url_for("portal.whatsapp_connect"))

    api_key = phixtra_api_key

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute("""
            INSERT INTO wa_tenants
              (tenant_id, phone_number_id, access_token, waba_id, verify_token,
               phixtra_api_key, active, signup_method, app_secret)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'manual', %s)
            ON DUPLICATE KEY UPDATE
              tenant_id       = VALUES(tenant_id),
              access_token    = VALUES(access_token),
              waba_id         = VALUES(waba_id),
              verify_token    = VALUES(verify_token),
              phixtra_api_key = VALUES(phixtra_api_key),
              active          = TRUE,
              signup_method   = 'manual',
              token_expires_at = NULL,
              app_secret       = IF(VALUES(app_secret) IS NOT NULL, VALUES(app_secret), app_secret),
              phixtra_api_key  = IF(VALUES(phixtra_api_key) != '', VALUES(phixtra_api_key), phixtra_api_key)
        """, (tenant_id, phone_number_id, access_token, waba_id or None, verify_token, api_key or '', app_secret or None))
        conn.commit()
        cur.close(); conn.close()
        insert_audit_log(action="wa_connected", tenant_id=tenant_id,
                         details={"phone_number_id": phone_number_id, "method": "manual"})
        flash("WhatsApp connected successfully! ✅", "success")
    except Exception as e:
        print("⚠️ whatsapp_save_connection error:", e)
        flash("Could not save connection. Please try again.", "danger")

    return redirect(url_for("portal.whatsapp_connect"))


@portal_bp.route("/whatsapp/disconnect", methods=["POST"])
def whatsapp_disconnect():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute("UPDATE wa_tenants SET active = FALSE WHERE tenant_id = %s", (tenant_id,))
        conn.commit()
        cur.close(); conn.close()
        insert_audit_log(action="wa_disconnected", tenant_id=tenant_id)
        flash("WhatsApp disconnected.", "success")
    except Exception as e:
        print("⚠️ whatsapp_disconnect error:", e)
        flash("Could not disconnect. Please try again.", "danger")

    return redirect(url_for("portal.whatsapp_connect"))


_GRAPH = "https://graph.facebook.com/v19.0"


def _exchange_code_for_tokens(code: str, app_id: str, app_secret: str) -> tuple[str | None, datetime | None]:
    """
    Exchange an Embedded Signup auth code for a long-lived access token.
    Returns (token, expires_at) or (None, None) on failure.
    """
    import requests as _req

    # Step 1: code → short-lived user token
    r1 = _req.get(f"{_GRAPH}/oauth/access_token", params={
        "client_id": app_id,
        "client_secret": app_secret,
        "code": code,
    }, timeout=15)
    if r1.status_code != 200:
        print("⚠️ wa token exchange step1 failed:", r1.text[:300])
        return None, None
    short_token = r1.json().get("access_token")
    if not short_token:
        return None, None

    # Step 2: short-lived → long-lived (60 days)
    r2 = _req.get(f"{_GRAPH}/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_token,
    }, timeout=15)
    if r2.status_code != 200:
        print("⚠️ wa token exchange step2 failed — using short-lived token")
        return short_token, datetime.utcnow() + timedelta(hours=1)

    resp2       = r2.json()
    long_token  = resp2.get("access_token", short_token)
    expires_in  = int(resp2.get("expires_in") or 5184000)  # 60 days default
    expires_at  = datetime.utcnow() + timedelta(seconds=expires_in)
    return long_token, expires_at


def _discover_phone_numbers(token: str, waba_id: str = "") -> list:
    """
    Return list of phone number dicts for the given WABA (or all WABAs if waba_id is blank).
    Each dict: {waba_id, waba_name, phone_number_id, display_phone_number, verified_name, status}
    """
    import requests as _req
    results = []

    if waba_id:
        wabas = [{"id": waba_id, "name": ""}]
    else:
        r = _req.get(f"{_GRAPH}/me/whatsapp_business_accounts",
                     params={"access_token": token, "fields": "id,name"}, timeout=15)
        wabas = r.json().get("data", []) if r.status_code == 200 else []

    for waba in wabas:
        wid = waba["id"]
        r2 = _req.get(f"{_GRAPH}/{wid}/phone_numbers",
                      params={"access_token": token,
                              "fields": "id,display_phone_number,verified_name,status"},
                      timeout=15)
        if r2.status_code == 200:
            for pn in r2.json().get("data", []):
                results.append({
                    "waba_id":              wid,
                    "waba_name":            waba.get("name", ""),
                    "phone_number_id":      pn["id"],
                    "display_phone_number": pn.get("display_phone_number", ""),
                    "verified_name":        pn.get("verified_name", ""),
                    "status":               pn.get("status", ""),
                })
    return results


def _auto_register_webhook(waba_id: str, token: str) -> bool:
    """Subscribe PhiXtra's app to receive webhooks for this WABA."""
    import requests as _req
    webhook_url = os.getenv("META_WEBHOOK_URL", "")
    if not webhook_url:
        return False
    try:
        r = _req.post(f"{_GRAPH}/{waba_id}/subscribed_apps",
                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
        ok = r.status_code == 200
        if not ok:
            print(f"⚠️ wa webhook subscription failed for waba={waba_id}: {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"⚠️ _auto_register_webhook error: {e}")
        return False


def _save_wa_embedded_connection(tenant_id: int, phone_number_id: str, waba_id: str,
                                  token: str, token_expires_at, api_key: str,
                                  display_phone: str = "", verified_name: str = "") -> bool:
    """Upsert the wa_tenants row for an Embedded Signup connection."""
    verify_token = secrets.token_urlsafe(20)
    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute("""
            INSERT INTO wa_tenants
              (tenant_id, phone_number_id, access_token, waba_id, verify_token,
               phixtra_api_key, active, signup_method,
               display_phone_number, verified_name, token_expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'embedded', %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              tenant_id            = VALUES(tenant_id),
              access_token         = VALUES(access_token),
              waba_id              = VALUES(waba_id),
              verify_token         = VALUES(verify_token),
              phixtra_api_key      = VALUES(phixtra_api_key),
              active               = TRUE,
              signup_method        = 'embedded',
              display_phone_number = VALUES(display_phone_number),
              verified_name        = VALUES(verified_name),
              token_expires_at     = VALUES(token_expires_at)
        """, (tenant_id, phone_number_id, token, waba_id, verify_token,
              api_key, display_phone or None, verified_name or None,
              token_expires_at))
        conn.commit()
        cur.close(); conn.close()
        return True
    except Exception as e:
        print("⚠️ _save_wa_embedded_connection error:", e)
        return False


@portal_bp.route("/whatsapp/embedded-callback", methods=["POST"])
def whatsapp_embedded_callback():
    """
    Receives the auth code + optional session info from Meta Embedded Signup JS.
    Exchanges code for a long-lived token, discovers phone numbers, auto-registers webhook.
    Returns JSON consumed by the frontend.
    """
    r = _require_login()
    if r:
        return jsonify({"error": "not_logged_in"}), 401

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    data            = request.get_json(silent=True) or {}
    code            = (data.get("code")            or "").strip()
    phone_number_id = (data.get("phone_number_id") or "").strip()
    waba_id         = (data.get("waba_id")         or "").strip()

    if not code:
        return jsonify({"error": "No auth code received. Please try again."}), 400

    app_id     = os.getenv("META_APP_ID", "")
    app_secret = os.getenv("META_APP_SECRET", "")
    if not app_id or not app_secret:
        return jsonify({"error": "Meta App credentials are not configured on this server. Contact support."}), 500

    # Exchange code → long-lived token
    token, token_expires_at = _exchange_code_for_tokens(code, app_id, app_secret)
    if not token:
        return jsonify({"error": "Failed to exchange auth code for access token. The code may have expired — please try again."}), 400

    # Discover phone numbers (use waba_id from JS event if available)
    phones = _discover_phone_numbers(token, waba_id=waba_id)
    if not phones:
        return jsonify({"error": "No WhatsApp phone numbers found. Ensure you selected a WABA with an active phone number during signup."}), 400

    # Fetch tenant API key
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT api_key_plain FROM api_keys WHERE tenant_id=%s AND is_active=1 LIMIT 1", (tenant_id,))
    key_row = cur.fetchone()
    cur.close(); conn.close()
    if not key_row:
        return jsonify({"error": "No active PhiXtra API key found. Contact support."}), 400

    api_key = key_row["api_key_plain"]

    # Store token info in session for the complete step
    session["wa_pending_token"]      = token
    session["wa_pending_expires"]    = token_expires_at.isoformat() if token_expires_at else None
    session["wa_pending_api_key"]    = api_key
    session["wa_pending_phones"]     = phones

    if len(phones) == 1:
        pn = phones[0]
        # Auto-register webhook and save immediately (single phone — no selection needed)
        _auto_register_webhook(pn["waba_id"], token)
        saved = _save_wa_embedded_connection(
            tenant_id=tenant_id,
            phone_number_id=pn["phone_number_id"],
            waba_id=pn["waba_id"],
            token=token,
            token_expires_at=token_expires_at,
            api_key=api_key,
            display_phone=pn["display_phone_number"],
            verified_name=pn["verified_name"],
        )
        if not saved:
            return jsonify({"error": "Could not save connection to database. Please try again."}), 500
        insert_audit_log(action="wa_embedded_connected", tenant_id=tenant_id,
                         details={"phone_number_id": pn["phone_number_id"],
                                  "waba_id": pn["waba_id"], "via": "embedded_signup"})
        return jsonify({
            "status": "connected",
            "display_phone": pn["display_phone_number"],
            "verified_name": pn["verified_name"],
        })

    # Multiple phones — let the user pick
    return jsonify({"status": "select_phone", "phone_options": phones})


@portal_bp.route("/whatsapp/embedded-complete", methods=["POST"])
def whatsapp_embedded_complete():
    """
    Second step of Embedded Signup when tenant has multiple phone numbers.
    Receives the chosen phone_number_id + waba_id and finalises the connection.
    """
    r = _require_login()
    if r: return r

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    phone_number_id = (request.form.get("phone_number_id") or "").strip()
    waba_id         = (request.form.get("waba_id")         or "").strip()
    display_phone   = (request.form.get("display_phone")   or "").strip()
    verified_name   = (request.form.get("verified_name")   or "").strip()

    token       = session.pop("wa_pending_token",   None)
    expires_str = session.pop("wa_pending_expires", None)
    api_key     = session.pop("wa_pending_api_key", None)
    session.pop("wa_pending_phones", None)

    if not all([token, phone_number_id, waba_id, api_key]):
        flash("Session expired. Please start the WhatsApp connection again.", "danger")
        return redirect(url_for("portal.whatsapp_connect"))

    token_expires_at = datetime.fromisoformat(expires_str) if expires_str else None

    _auto_register_webhook(waba_id, token)
    saved = _save_wa_embedded_connection(
        tenant_id=tenant_id,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
        token=token,
        token_expires_at=token_expires_at,
        api_key=api_key,
        display_phone=display_phone,
        verified_name=verified_name,
    )
    if saved:
        insert_audit_log(action="wa_embedded_connected", tenant_id=tenant_id,
                         details={"phone_number_id": phone_number_id,
                                  "waba_id": waba_id, "via": "embedded_signup"})
        flash(f"WhatsApp connected! ✅  {display_phone or phone_number_id}", "success")
    else:
        flash("Could not save connection. Please try again.", "danger")

    return redirect(url_for("portal.whatsapp_connect"))


@portal_bp.route("/whatsapp/check-token", methods=["POST"])
def whatsapp_check_token():
    """
    Validate the stored access token by calling Meta's /me endpoint.
    Returns JSON: {valid: bool, name: str, error: str}.
    """
    r = _require_login()
    if r:
        return jsonify({"valid": False, "error": "not_logged_in"}), 401

    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT access_token, phone_number_id FROM wa_tenants WHERE tenant_id=%s AND active=TRUE LIMIT 1",
                    (tenant_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
    except Exception:
        return jsonify({"valid": False, "error": "Database error"}), 500

    if not row:
        return jsonify({"valid": False, "error": "No connection found"})

    import requests as _req
    try:
        r2 = _req.get(f"{_GRAPH}/me",
                      params={"access_token": row["access_token"],
                              "fields": "id,name"},
                      timeout=10)
        if r2.status_code == 200:
            name = r2.json().get("name") or r2.json().get("id", "")
            return jsonify({"valid": True, "name": name})
        err = r2.json().get("error", {}).get("message", "Token invalid or expired")
        return jsonify({"valid": False, "error": err})
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)})


@portal_bp.route("/whatsapp/templates", methods=["GET"])
def whatsapp_templates():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    connection = _get_wa_connection(tenant_id)
    templates  = _get_wa_templates(tenant_id)
    return render_template(
        "portal/whatsapp_templates.html",
        connection=connection,
        templates=templates,
    )


@portal_bp.route("/whatsapp/templates", methods=["POST"])
def whatsapp_save_templates():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    for ttype in ("cart_recovery", "order_update"):
        tname = (request.form.get(f"template_{ttype}") or "").strip()
        lang  = (request.form.get(f"lang_{ttype}")     or "en").strip() or "en"
        if not tname:
            continue
        try:
            conn = get_db_connection()
            cur  = conn.cursor(buffered=True)
            cur.execute("""
                INSERT INTO wa_templates
                  (tenant_id, template_type, template_name, language_code, active)
                VALUES (%s, %s, %s, %s, TRUE)
                ON DUPLICATE KEY UPDATE
                  template_name = VALUES(template_name),
                  language_code = VALUES(language_code),
                  active        = TRUE
            """, (tenant_id, ttype, tname, lang))
            conn.commit()
            cur.close(); conn.close()
        except Exception as e:
            print(f"⚠️ whatsapp_save_templates ({ttype}) error:", e)

    flash("Template settings saved. ✅", "success")
    return redirect(url_for("portal.whatsapp_templates"))


@portal_bp.route("/whatsapp/inbox")
def whatsapp_inbox():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    connection = _get_wa_connection(tenant_id)

    handoffs = []
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT session_id, customer_phone, escalated_at
            FROM wa_handoff_state
            WHERE tenant_id = %s AND resolved_at IS NULL
            ORDER BY escalated_at DESC
        """, (tenant_id,))
        handoffs = cur.fetchall() or []

        for h in handoffs:
            cur.execute("""
                SELECT direction, content, message_type, created_at
                FROM wa_message_log
                WHERE tenant_id = %s AND customer_phone = %s
                ORDER BY created_at DESC LIMIT 30
            """, (tenant_id, h["customer_phone"]))
            msgs = cur.fetchall() or []
            h["messages"] = list(reversed(msgs))

        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ whatsapp_inbox error:", e)

    return render_template("portal/whatsapp_inbox.html",
                           customer=customer,
                           connection=connection,
                           handoffs=handoffs)


@portal_bp.route("/whatsapp/inbox/<path:session_id>/reply", methods=["POST"])
def whatsapp_inbox_reply(session_id: str):
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    reply_text = (request.form.get("reply") or "").strip()
    if not reply_text:
        flash("Reply cannot be empty.", "danger")
        return redirect(url_for("portal.whatsapp_inbox"))

    # Verify handoff belongs to this tenant and pull credentials
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT h.customer_phone, t.phone_number_id, t.access_token
            FROM wa_handoff_state h
            JOIN wa_tenants t ON t.tenant_id = h.tenant_id AND t.active = TRUE
            WHERE h.session_id = %s AND h.tenant_id = %s AND h.resolved_at IS NULL
        """, (session_id, tenant_id))
        row = cur.fetchone()
        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ whatsapp_inbox_reply lookup error:", e)
        row = None

    if not row:
        flash("Conversation not found or already resolved.", "danger")
        return redirect(url_for("portal.whatsapp_inbox"))

    ok = _send_wa_text_from_portal(
        row["phone_number_id"], row["access_token"],
        row["customer_phone"], reply_text
    )

    if ok:
        try:
            conn = get_db_connection()
            cur  = conn.cursor(buffered=True)
            cur.execute("""
                INSERT INTO wa_message_log
                  (tenant_id, phone_number_id, customer_phone, direction, content, message_type)
                VALUES (%s, %s, %s, 'outbound', %s, 'agent_reply')
            """, (tenant_id, row["phone_number_id"], row["customer_phone"], reply_text))
            conn.commit()
            cur.close(); conn.close()
        except Exception as e:
            print("⚠️ whatsapp_inbox_reply log error:", e)
        flash("Message sent. ✅", "success")
    else:
        flash("Failed to send via WhatsApp — check your credentials.", "danger")

    return redirect(url_for("portal.whatsapp_inbox"))


@portal_bp.route("/whatsapp/inbox/<path:session_id>/resolve", methods=["POST"])
def whatsapp_inbox_resolve(session_id: str):
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute("""
            UPDATE wa_handoff_state
            SET resolved_at = UTC_TIMESTAMP()
            WHERE session_id = %s AND tenant_id = %s AND resolved_at IS NULL
        """, (session_id, tenant_id))
        conn.commit()
        affected = cur.rowcount
        cur.close(); conn.close()
        if affected:
            flash("Conversation resolved — AI assistant will resume. ✅", "success")
        else:
            flash("Conversation not found or already resolved.", "warning")
    except Exception as e:
        print("⚠️ whatsapp_inbox_resolve error:", e)
        flash("Could not resolve conversation. Please try again.", "danger")

    return redirect(url_for("portal.whatsapp_inbox"))


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP CAMPAIGNS
# ══════════════════════════════════════════════════════════════════════════════

import threading as _threading
import time as _time


def _send_campaign_now(campaign_id: int, tenant_id: int):
    """Run a campaign immediately in a background thread."""
    try:
        import requests as _req
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)

        cur.execute(
            "UPDATE wa_campaigns SET status='running', completed_at=NULL "
            "WHERE id=%s AND tenant_id=%s AND status IN ('draft','scheduled')",
            (campaign_id, tenant_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            cur.close(); conn.close()
            return

        cur.execute(
            "SELECT c.*, t.phone_number_id, t.access_token "
            "FROM wa_campaigns c "
            "JOIN wa_tenants t ON t.tenant_id=c.tenant_id AND t.active=TRUE "
            "WHERE c.id=%s",
            (campaign_id,),
        )
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            return

        phones = [p.strip() for p in (row["recipients"] or "").splitlines() if p.strip()]
        sent = failed = 0
        graph = os.getenv("META_GRAPH_URL", "https://graph.facebook.com/v19.0")
        for phone in phones:
            try:
                resp = _req.post(
                    f"{graph}/{row['phone_number_id']}/messages",
                    headers={"Authorization": f"Bearer {row['access_token']}",
                             "Content-Type": "application/json"},
                    json={
                        "messaging_product": "whatsapp",
                        "to": phone,
                        "type": "template",
                        "template": {
                            "name": row["template_name"],
                            "language": {"code": row["language_code"]},
                        },
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        conn2 = get_db_connection()
        cur2  = conn2.cursor(buffered=True)
        cur2.execute(
            "UPDATE wa_campaigns SET status='done', completed_at=UTC_TIMESTAMP(), "
            "sent_count=%s, failed_count=%s WHERE id=%s",
            (sent, failed, campaign_id),
        )
        conn2.commit()
        cur2.close(); conn2.close()
    except Exception as e:
        print(f"⚠️ _send_campaign_now error (campaign {campaign_id}):", e)
        try:
            conn3 = get_db_connection()
            cur3  = conn3.cursor(buffered=True)
            cur3.execute("UPDATE wa_campaigns SET status='failed' WHERE id=%s", (campaign_id,))
            conn3.commit()
            cur3.close(); conn3.close()
        except Exception:
            pass


def _campaign_scheduler_loop():
    """Background thread: fire scheduled campaigns when their time arrives."""
    while True:
        try:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor(dictionary=True, buffered=True)
                cur.execute(
                    "SELECT id, tenant_id FROM wa_campaigns "
                    "WHERE status='scheduled' AND scheduled_at <= UTC_TIMESTAMP()"
                )
                due = cur.fetchall()
                cur.close(); conn.close()
                for c in due:
                    t = _threading.Thread(
                        target=_send_campaign_now,
                        args=(c["id"], c["tenant_id"]),
                        daemon=True,
                    )
                    t.start()
        except Exception as e:
            print("⚠️ campaign scheduler error:", e)
        _time.sleep(60)


_sched_started = getattr(_threading, "_phixtra_campaign_sched_started", False)
if not _sched_started:
    _threading._phixtra_campaign_sched_started = True  # type: ignore[attr-defined]
    _sched_thread = _threading.Thread(target=_campaign_scheduler_loop, daemon=True)
    _sched_thread.start()


@portal_bp.route("/whatsapp/campaigns")
def whatsapp_campaigns():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])
    connection = _get_wa_connection(tenant_id)

    campaigns = []
    proactive_log = []
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute(
            "SELECT * FROM wa_campaigns WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 100",
            (tenant_id,),
        )
        campaigns = cur.fetchall()
        cur.execute(
            "SELECT * FROM wa_proactive_log WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 50",
            (tenant_id,),
        )
        proactive_log = cur.fetchall()
        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ whatsapp_campaigns fetch error:", e)

    return render_template(
        "portal/whatsapp_campaigns.html",
        connection=connection,
        campaigns=campaigns,
        proactive_log=proactive_log,
    )


@portal_bp.route("/whatsapp/campaigns/create", methods=["POST"])
def whatsapp_campaigns_create():
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    name          = (request.form.get("name") or "").strip()
    template_name = (request.form.get("template_name") or "").strip()
    language_code = (request.form.get("language_code") or "en").strip()
    recipients    = (request.form.get("recipients") or "").strip()
    schedule_str  = (request.form.get("scheduled_at") or "").strip()
    send_now      = request.form.get("send_now") == "1"

    if not name or not template_name or not recipients:
        flash("Campaign name, template name, and at least one recipient are required.", "danger")
        return redirect(url_for("portal.whatsapp_campaigns"))

    phones = [p.strip() for p in recipients.splitlines() if p.strip()]
    if not phones:
        flash("No valid phone numbers found.", "danger")
        return redirect(url_for("portal.whatsapp_campaigns"))

    scheduled_at = None
    status = "draft"
    if schedule_str and not send_now:
        try:
            scheduled_at = datetime.strptime(schedule_str, "%Y-%m-%dT%H:%M")
            status = "scheduled"
        except ValueError:
            flash("Invalid schedule date/time format.", "danger")
            return redirect(url_for("portal.whatsapp_campaigns"))
    elif send_now:
        status = "draft"

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute(
            """
            INSERT INTO wa_campaigns
              (tenant_id, name, template_name, language_code, status,
               scheduled_at, total_count, recipients)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (tenant_id, name, template_name, language_code, status,
             scheduled_at, len(phones), "\n".join(phones)),
        )
        conn.commit()
        campaign_id = cur.lastrowid
        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ whatsapp_campaigns_create error:", e)
        flash("Could not save campaign. Please try again.", "danger")
        return redirect(url_for("portal.whatsapp_campaigns"))

    if send_now:
        t = _threading.Thread(
            target=_send_campaign_now, args=(campaign_id, tenant_id), daemon=True
        )
        t.start()
        flash(f"Campaign '{name}' started — sending to {len(phones)} recipients.", "success")
    else:
        when = scheduled_at.strftime('%d %b %Y %H:%M') if scheduled_at else 'draft'
        flash(f"Campaign '{name}' saved ({when}).", "success")

    return redirect(url_for("portal.whatsapp_campaigns"))


@portal_bp.route("/whatsapp/campaigns/<int:campaign_id>/send", methods=["POST"])
def whatsapp_campaigns_send(campaign_id: int):
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    t = _threading.Thread(
        target=_send_campaign_now, args=(campaign_id, tenant_id), daemon=True
    )
    t.start()
    flash("Campaign sending started.", "success")
    return redirect(url_for("portal.whatsapp_campaigns"))


@portal_bp.route("/whatsapp/campaigns/<int:campaign_id>/delete", methods=["POST"])
def whatsapp_campaigns_delete(campaign_id: int):
    r = _require_login()
    if r: return r
    customer  = _get_customer(_customer_id())
    tenant_id = int(customer["tenant_id"])

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute(
            "DELETE FROM wa_campaigns WHERE id=%s AND tenant_id=%s AND status IN ('draft','scheduled')",
            (campaign_id, tenant_id),
        )
        conn.commit()
        if cur.rowcount:
            flash("Campaign deleted.", "success")
        else:
            flash("Cannot delete a running or completed campaign.", "warning")
        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ whatsapp_campaigns_delete error:", e)
        flash("Delete failed.", "danger")

    return redirect(url_for("portal.whatsapp_campaigns"))
