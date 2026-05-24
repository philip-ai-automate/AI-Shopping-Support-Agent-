"""
portal_admin_routes.py  — Phase 1 admin portal (/admin/*)
admin_users table and db.py are UNCHANGED. app.py (keys.phixtra.com) is UNTOUCHED.
"""
import os
import secrets
import string
import json as _json
import bcrypt
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, send_file)

from db import get_db_connection, insert_audit_log
from portal_utils import money_fmt, tokens_to_credits, credits_to_tokens

portal_admin_bp = Blueprint("portal_admin", __name__)


def _admin_logged_in() -> bool:
    return session.get("portal_admin_logged_in") is True

def _require_admin():
    if not _admin_logged_in():
        return redirect(url_for("portal_admin.login"))
    return None

def _admin_user() -> str:
    return session.get("portal_admin_username") or "admin"


# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("portal/admin_login.html")

    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT * FROM admin_users WHERE username=%s", (username,))
    admin = cur.fetchone()
    cur.close(); conn.close()

    # Plain-text check — same as app.py (admin_users.password is plaintext by design)
    if admin and password == admin.get("password"):
        session["portal_admin_logged_in"]  = True
        session["portal_admin_username"]   = username
        return redirect(url_for("portal_admin.customers"))

    flash("Invalid admin login.", "danger")
    return redirect(url_for("portal_admin.login"))


@portal_admin_bp.route("/logout")
def logout():
    session.pop("portal_admin_logged_in", None)
    session.pop("portal_admin_username",  None)
    session.pop("impersonate_customer_id", None)
    return redirect(url_for("portal_admin.login"))


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/customers")
def customers():
    r = _require_admin()
    if r: return r

    q = (request.args.get("q") or "").strip().lower()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)

    if q:
        cur.execute("""
            SELECT c.id, c.first_name, c.last_name, c.email, c.phone_number,
                   c.email_verified, c.is_active, c.created_at,
                   t.id AS tenant_id, t.name AS tenant_name, t.domain,
                   COALESCE(tb.token_balance,0) AS token_balance
            FROM customers c
            JOIN tenants t ON t.id=c.tenant_id
            LEFT JOIN tenant_balances tb ON tb.tenant_id=t.id
            WHERE LOWER(c.email) LIKE %s
               OR LOWER(t.domain) LIKE %s
               OR LOWER(t.name) LIKE %s
               OR LOWER(CONCAT(COALESCE(c.first_name,''),' ',COALESCE(c.last_name,''))) LIKE %s
            ORDER BY c.created_at DESC LIMIT 300""",
            (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        cur.execute("""
            SELECT c.id, c.first_name, c.last_name, c.email, c.phone_number,
                   c.email_verified, c.is_active, c.created_at,
                   t.id AS tenant_id, t.name AS tenant_name, t.domain,
                   COALESCE(tb.token_balance,0) AS token_balance
            FROM customers c
            JOIN tenants t ON t.id=c.tenant_id
            LEFT JOIN tenant_balances tb ON tb.tenant_id=t.id
            ORDER BY c.created_at DESC LIMIT 300""")

    rows = cur.fetchall() or []
    cur.close(); conn.close()

    for row in rows:
        row["balance_credits"] = tokens_to_credits(int(row.get("token_balance") or 0))
        fn = (row.get("first_name") or "").strip()
        ln = (row.get("last_name")  or "").strip()
        row["full_name"] = f"{fn} {ln}".strip() or "—"

    return render_template("portal/admin_customers.html", customers=rows, q=q)


@portal_admin_bp.route("/customers/<int:customer_id>")
def customer_detail(customer_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)

    # NOTE: t.features is NOT included in this JOIN.
    # Fetching it here caused the 500 error because the `features` column
    # may not exist in the database yet (migration may not have run).
    # We fetch it separately below with a safe try/except instead.
    cur.execute("""
        SELECT c.*, t.name AS tenant_name, t.domain,
               COALESCE(tb.token_balance,0) AS token_balance
        FROM customers c
        JOIN tenants t ON t.id=c.tenant_id
        LEFT JOIN tenant_balances tb ON tb.tenant_id=t.id
        WHERE c.id=%s""", (customer_id,))
    customer = cur.fetchone()
    if not customer:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id = int(customer["tenant_id"])

    # Fetch tenant features safely — works even if the features column
    # does not exist yet in the database (e.g. migration not run).
    # If anything goes wrong the page still loads; features just show as off.
    tenant_features = {}
    try:
        cur.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
        feat_row = cur.fetchone() or {}
        raw_features = feat_row.get("features")
        if raw_features:
            if isinstance(raw_features, str):
                tenant_features = _json.loads(raw_features)
            elif isinstance(raw_features, dict):
                tenant_features = raw_features
    except Exception:
        tenant_features = {}

    cur.execute("""
        SELECT id, website, key_type, is_active, token_limit, tokens_used,
               trial_activated_at, trial_expires_at, created_at
        FROM api_keys WHERE tenant_id=%s ORDER BY created_at DESC""", (tenant_id,))
    keys = cur.fetchall() or []

    cur.execute("""
        SELECT id, invoice_number, credits, amount_pence, vat_pence, currency, status, created_at
        FROM invoices WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 50""", (tenant_id,))
    invs = cur.fetchall() or []

    cur.execute("""
        SELECT action, website, key_type, api_key_last4, details, created_at, admin_username
        FROM audit_logs WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 100""", (tenant_id,))
    audit = cur.fetchall() or []

    cur.close(); conn.close()

    customer["balance_credits"] = tokens_to_credits(int(customer.get("token_balance") or 0))
    fn = (customer.get("first_name") or "").strip()
    ln = (customer.get("last_name")  or "").strip()
    customer["full_name"] = f"{fn} {ln}".strip() or "—"

    for inv in invs:
        inv["total_fmt"] = money_fmt(
            int(inv.get("amount_pence") or 0) + int(inv.get("vat_pence") or 0),
            inv.get("currency") or "gbp")

    return render_template("portal/admin_customer_detail.html",
                           customer=customer, keys=keys,
                           invoices=invs, audit=audit,
                           tenant_features=tenant_features,
                           admin_new_plain_key=session.pop("admin_new_plain_key", None))


@portal_admin_bp.route("/customers/<int:customer_id>/credit-adjust", methods=["POST"])
def customer_credit_adjust(customer_id: int):
    r = _require_admin()
    if r: return r

    delta_credits = float(request.form.get("delta_credits") or 0)
    reason        = (request.form.get("reason") or "").strip()

    if delta_credits == 0:
        flash("Enter a non-zero credit amount.", "warning")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id   = int(row["tenant_id"])
    delta_tokens = int(delta_credits * 5000)

    cur2 = conn.cursor(buffered=True)
    cur2.execute("INSERT IGNORE INTO tenant_balances (tenant_id, token_balance) VALUES (%s, 0)", (tenant_id,))
    cur2.execute("""
        UPDATE tenant_balances
        SET token_balance = GREATEST(0, token_balance + %s)
        WHERE tenant_id=%s""", (delta_tokens, tenant_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(
        admin_username=_admin_user(),
        action="admin_credit_adjust",
        tenant_id=tenant_id,
        details={"delta_credits": delta_credits, "delta_tokens": delta_tokens, "reason": reason},
    )

    direction = "Added" if delta_credits > 0 else "Deducted"
    flash(f"{direction} {abs(delta_credits):.0f} credits.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


@portal_admin_bp.route("/customers/<int:customer_id>/toggle-active", methods=["POST"])
def customer_toggle_active(customer_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT is_active FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    new_val = 0 if int(row.get("is_active") or 0) else 1
    cur2 = conn.cursor(buffered=True)
    cur2.execute("UPDATE customers SET is_active=%s WHERE id=%s", (new_val, customer_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(),
                     action="admin_toggle_customer",
                     details={"customer_id": customer_id, "new_is_active": new_val})
    flash(f"Customer {'activated' if new_val else 'disabled'}.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE MANAGEMENT (NEW)
# Sets which plugin features are active for a specific customer's tenant.
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/customers/<int:customer_id>/set-features", methods=["POST"])
def customer_set_features(customer_id: int):
    r = _require_admin()
    if r: return r

    # Read the feature checkboxes from the form
    feat_product_rec      = request.form.get("feat_product_recommendation") == "on"
    feat_related_products = request.form.get("feat_related_products") == "on"
    feat_cart_recovery    = request.form.get("feat_cart_recovery") == "on"
    feat_verified_specs   = request.form.get("feat_verified_specs_web_lookup") == "on"
    feat_chat_archive_30d = request.form.get("feat_chat_archive_30days") == "on"
    feat_chat_archive_unl = request.form.get("feat_chat_archive_unlimited") == "on"

    # Cart recovery sub-settings
    recovery_popup_message = (request.form.get("cart_recovery_popup_message") or "").strip()
    recovery_incentive_pct = 0
    try:
        recovery_incentive_pct = max(0, min(50, int(request.form.get("cart_recovery_incentive_pct") or 0)))
    except (ValueError, TypeError):
        recovery_incentive_pct = 0

    # Build the features dict — add more keys here as new features are added
    features = {}
    if feat_product_rec:
        features["product_recommendation"] = True
    # Related products requires product_recommendation to also be active
    if feat_product_rec and feat_related_products:
        features["related_products"] = True
    # Cart Revenue Recovery
    if feat_cart_recovery:
        features["cart_recovery"] = True
        if recovery_popup_message:
            features["cart_recovery_popup_message"] = recovery_popup_message
        if recovery_incentive_pct > 0:
            features["cart_recovery_incentive_pct"] = recovery_incentive_pct

    # Verified Specs Lookup (Web) — allows the AI backend to browse trusted sources for numeric specs
    if feat_verified_specs:
        features["verified_specs_web_lookup"] = True

    # Chat Archive — 30 Days: search, PDF export, AI summaries, 30-day window
    # Chat Archive — Unlimited: search, all exports, AI summaries, no day limit
    # Only one tier should be active at a time; if both are ticked, Unlimited wins.
    if feat_chat_archive_unl:
        features["chat_archive_unlimited"] = True
    elif feat_chat_archive_30d:
        features["chat_archive_30days"] = True

    features_json = _json.dumps(features) if features else None

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id = int(row["tenant_id"])
    cur2 = conn.cursor(buffered=True)
    cur2.execute(
        "UPDATE tenants SET features=%s WHERE id=%s",
        (features_json, tenant_id)
    )
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(
        admin_username=_admin_user(),
        action="admin_set_tenant_features",
        tenant_id=tenant_id,
        details={"features": features, "customer_id": customer_id},
    )

    flash("Feature settings saved. Changes take effect on the customer's next chat message.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


# ══════════════════════════════════════════════════════════════════════════════
# TRIAL MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/customers/<int:customer_id>/trial-adjust", methods=["POST"])
def customer_trial_adjust(customer_id: int):
    r = _require_admin()
    if r: return r

    try:
        trial_days = max(1, min(3650, int(request.form.get("trial_days") or 0)))
    except (ValueError, TypeError):
        flash("Invalid number of days.", "danger")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id = int(row["tenant_id"])

    cur.execute("""
        SELECT id, trial_activated_at FROM api_keys
        WHERE tenant_id=%s AND key_type='trial'
        ORDER BY created_at ASC LIMIT 1""", (tenant_id,))
    key = cur.fetchone()
    if not key:
        cur.close(); conn.close()
        flash("No trial key found for this customer.", "warning")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    from datetime import timedelta
    activated = key.get("trial_activated_at")
    if not activated:
        from datetime import datetime as _dt
        activated = _dt.utcnow()

    new_expiry = activated + timedelta(days=trial_days)

    cur2 = conn.cursor(buffered=True)
    cur2.execute(
        "UPDATE api_keys SET trial_expires_at=%s, is_active=1 WHERE id=%s",
        (new_expiry, key["id"])
    )
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(
        admin_username=_admin_user(),
        action="admin_trial_adjusted",
        tenant_id=tenant_id,
        details={"trial_days": trial_days, "new_expiry": str(new_expiry), "customer_id": customer_id},
    )

    flash(f"Trial extended to {trial_days} days from activation. Expires {new_expiry.strftime('%Y-%m-%d')} ✅", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


@portal_admin_bp.route("/customers/<int:customer_id>/trial-reset", methods=["POST"])
def customer_trial_reset(customer_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id = int(row["tenant_id"])

    default_days = _get_trial_default_days()

    cur.execute("""
        SELECT id FROM api_keys
        WHERE tenant_id=%s AND key_type='trial'
        ORDER BY created_at ASC LIMIT 1""", (tenant_id,))
    key = cur.fetchone()
    if not key:
        cur.close(); conn.close()
        flash("No trial key found for this customer.", "warning")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    from datetime import datetime as _dt, timedelta
    now = _dt.utcnow()
    new_expiry = now + timedelta(days=default_days)

    cur2 = conn.cursor(buffered=True)
    cur2.execute(
        "UPDATE api_keys SET trial_activated_at=%s, trial_expires_at=%s, is_active=1, tokens_used=0 WHERE id=%s",
        (now, new_expiry, key["id"])
    )
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(
        admin_username=_admin_user(),
        action="admin_trial_reset",
        tenant_id=tenant_id,
        details={"default_days": default_days, "new_expiry": str(new_expiry), "customer_id": customer_id},
    )

    flash(f"Trial reset from today. New expiry: {new_expiry.strftime('%Y-%m-%d')} ({default_days} days) ✅", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


# ══════════════════════════════════════════════════════════════════════════════
# IMPERSONATION
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/impersonate/<int:customer_id>")
def impersonate(customer_id: int):
    r = _require_admin()
    if r: return r
    session["portal_logged_in"]         = True
    session["impersonate_customer_id"]  = int(customer_id)
    insert_audit_log(admin_username=_admin_user(), action="impersonate_start",
                     details={"customer_id": customer_id})
    flash("Impersonating customer — you see the portal as they do.", "warning")
    return redirect(url_for("portal.dashboard"))


@portal_admin_bp.route("/stop-impersonate")
def stop_impersonate():
    r = _require_admin()
    if r: return r
    session.pop("impersonate_customer_id", None)
    flash("Impersonation stopped.", "success")
    return redirect(url_for("portal_admin.customers"))


# ══════════════════════════════════════════════════════════════════════════════
# API KEY REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/api-keys")
def api_keys():
    r = _require_admin()
    if r: return r

    q = (request.args.get("q") or "").strip().lower()

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)

    # Read api_key_plain from audit_logs WHERE action='create_key'
    # This is the same table that app.py (keys.phixtra.com) writes to.
    if q:
        cur.execute("""
            SELECT al.id, al.api_key_plain, al.api_key_last4, al.website, al.key_type,
                   al.tenant_id, al.api_key_id, al.created_at, al.admin_username, al.details,
                   t.name AS tenant_name, t.domain,
                   ak.is_active, ak.tokens_used, ak.token_limit,
                   ak.trial_expires_at
            FROM audit_logs al
            JOIN tenants t ON t.id = al.tenant_id
            LEFT JOIN api_keys ak ON ak.id = al.api_key_id
            WHERE al.action='create_key'
              AND (LOWER(al.website) LIKE %s
                OR LOWER(t.name) LIKE %s
                OR LOWER(al.api_key_last4) LIKE %s)
            ORDER BY al.created_at DESC
            LIMIT 500""",
            (f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        cur.execute("""
            SELECT al.id, al.api_key_plain, al.api_key_last4, al.website, al.key_type,
                   al.tenant_id, al.api_key_id, al.created_at, al.admin_username, al.details,
                   t.name AS tenant_name, t.domain,
                   ak.is_active, ak.tokens_used, ak.token_limit,
                   ak.trial_expires_at
            FROM audit_logs al
            JOIN tenants t ON t.id = al.tenant_id
            LEFT JOIN api_keys ak ON ak.id = al.api_key_id
            WHERE al.action='create_key'
            ORDER BY al.created_at DESC
            LIMIT 500""")

    rows = cur.fetchall() or []
    cur.close(); conn.close()

    for row in rows:
        row["credits_used"] = tokens_to_credits(int(row.get("tokens_used") or 0))
        # Status
        is_active = row.get("is_active")
        if is_active is None:
            row["status"] = "Unknown"
        elif int(is_active) == 0:
            row["status"] = "Revoked"
        else:
            row["status"] = "Active"

    return render_template("portal/admin_api_keys.html", keys=rows, q=q)


@portal_admin_bp.route("/api-keys/<int:key_id>/revoke", methods=["POST"])
def api_keys_revoke(key_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT id, tenant_id, website, key_type FROM api_keys WHERE id=%s", (key_id,))
    k = cur.fetchone()
    if k:
        cur2 = conn.cursor(buffered=True)
        cur2.execute("UPDATE api_keys SET is_active=0 WHERE id=%s", (key_id,))
        conn.commit()
        cur2.close()
        insert_audit_log(admin_username=_admin_user(), action="admin_revoke_key",
                         tenant_id=k.get("tenant_id"), website=k.get("website"),
                         key_type=k.get("key_type"), api_key_id=key_id)
    cur.close(); conn.close()
    flash("Key revoked.", "success")
    return redirect(url_for("portal_admin.api_keys"))


@portal_admin_bp.route("/api-keys/<int:key_id>/reactivate", methods=["POST"])
def api_keys_reactivate(key_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT id, tenant_id, website, key_type FROM api_keys WHERE id=%s", (key_id,))
    k = cur.fetchone()
    if k:
        cur2 = conn.cursor(buffered=True)
        cur2.execute("UPDATE api_keys SET is_active=1 WHERE id=%s", (key_id,))
        conn.commit()
        cur2.close()
        insert_audit_log(admin_username=_admin_user(), action="admin_reactivate_key",
                         tenant_id=k.get("tenant_id"), website=k.get("website"),
                         key_type=k.get("key_type"), api_key_id=key_id)
    cur.close(); conn.close()
    flash("Key reactivated.", "success")
    return redirect(url_for("portal_admin.api_keys"))


def _admin_generate_api_key_and_hash(length: int = 28):
    """Same algorithm as portal_routes.py — keep in sync."""
    alphabet = string.ascii_letters + string.digits
    plain_key = ''.join(secrets.choice(alphabet) for _ in range(length))
    hashed_key = bcrypt.hashpw(plain_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    return plain_key, hashed_key


@portal_admin_bp.route("/customers/<int:customer_id>/api-keys/create", methods=["POST"])
def customer_api_key_create(customer_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)

    # Look up customer → tenant
    cur.execute("""
        SELECT c.id, c.email, t.id AS tenant_id, t.domain
        FROM customers c
        JOIN tenants t ON t.id = c.tenant_id
        WHERE c.id=%s""", (customer_id,))
    customer = cur.fetchone()
    if not customer:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id = int(customer["tenant_id"])
    domain    = customer.get("domain") or ""

    # Safety: refuse if an active paid key already exists for this tenant
    cur.execute("""
        SELECT id FROM api_keys
        WHERE tenant_id=%s AND key_type='paid' AND is_active=1
        LIMIT 1""", (tenant_id,))
    if cur.fetchone():
        cur.close(); conn.close()
        flash("An active paid key already exists for this tenant. Revoke it first.", "warning")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    plain_key, hashed_key = _admin_generate_api_key_and_hash()
    last4 = plain_key[-4:]

    try:
        cur2 = conn.cursor(buffered=True)
        cur2.execute("""
            INSERT INTO api_keys
                (tenant_id, api_key_hash, api_key_plain, is_active, website, key_type, token_limit, tokens_used)
            VALUES (%s, %s, %s, 1, %s, 'paid', NULL, 0)""",
            (tenant_id, hashed_key, plain_key, domain))
        api_key_id = cur2.lastrowid
        conn.commit()
        cur2.close()
    except Exception as e:
        conn.rollback()
        cur.close(); conn.close()
        flash(f"Could not create key: {e}", "danger")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    cur.close(); conn.close()

    insert_audit_log(
        admin_username=_admin_user(),
        action="create_key",
        tenant_id=tenant_id,
        website=domain,
        key_type="paid",
        api_key_id=api_key_id,
        api_key_last4=last4,
        api_key_plain=plain_key,
        details={"created_from": "admin_portal"},
    )

    # Store plain key in session — shown once on the customer detail page
    session["admin_new_plain_key"] = plain_key
    flash("API key created. Copy it now — it will not be shown again.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


# ══════════════════════════════════════════════════════════════════════════════
# CREDIT PACKAGES (admin)
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/credit-packages", methods=["GET", "POST"])
def credit_packages():
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)

    if request.method == "POST":
        name        = (request.form.get("name")        or "").strip()
        credits     = int(request.form.get("credits")  or 0)
        price_pence = int(float(request.form.get("price_gbp") or 0) * 100)
        vat_rate    = float(request.form.get("vat_rate")   or 20.0)
        is_active   = 1 if request.form.get("is_active") == "on" else 0
        sort_order  = int(request.form.get("sort_order")   or 0)

        # Stage 3 — package type and billing period
        # package_type : 'topup' (default, existing behaviour) or 'subscription'
        # billing_period: 'monthly' or 'annual' — only used for subscriptions
        raw_pkg_type    = (request.form.get("package_type") or "topup").strip()
        package_type    = raw_pkg_type if raw_pkg_type in ("topup", "subscription") else "topup"
        raw_billing     = (request.form.get("billing_period") or "").strip()
        billing_period  = raw_billing if raw_billing in ("monthly", "annual") else None
        # billing_period is only meaningful for subscription packages
        if package_type == "topup":
            billing_period = None

        # Read feature checkboxes
        feat_product_rec      = request.form.get("feature_product_recommendation") == "on"
        feat_related_products = request.form.get("feature_related_products") == "on"
        feat_cart_recovery    = request.form.get("feature_cart_recovery") == "on"
        feat_verified_specs  = request.form.get("feature_verified_specs_web_lookup") == "on"
        feat_chat_archive_30d = request.form.get("feature_chat_archive_30days") == "on"
        feat_chat_archive_unl = request.form.get("feature_chat_archive_unlimited") == "on"
        features_dict = {}
        if feat_product_rec:
            features_dict["product_recommendation"] = True
        # Related products requires product_recommendation to also be active
        if feat_product_rec and feat_related_products:
            features_dict["related_products"] = True
        # Intelligent Cart Revenue Recovery
        if feat_cart_recovery:
            features_dict["cart_recovery"] = True
        # Verified Specs Lookup (Web)
        if feat_verified_specs:
            features_dict["verified_specs_web_lookup"] = True
        # Chat Archive tiers — only one can be set; Unlimited wins if both ticked
        if feat_chat_archive_unl:
            features_dict["chat_archive_unlimited"] = True
        elif feat_chat_archive_30d:
            features_dict["chat_archive_30days"] = True
        # Custom features — one per line entered by admin
        custom_text = (request.form.get("custom_features_text") or "").strip()
        custom_list = [line.strip() for line in custom_text.splitlines() if line.strip()]
        if custom_list:
            features_dict["custom_features"] = custom_list
        features_json = _json.dumps(features_dict) if features_dict else None

        if not name or credits <= 0 or price_pence <= 0:
            flash("Name, credits and price are required.", "danger")
        else:
            cur2 = conn.cursor(buffered=True)
            cur2.execute("""
                INSERT INTO credit_packages (name, credits, price_pence, vat_rate, is_active, sort_order, features, package_type, billing_period)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (name, credits, price_pence, vat_rate, is_active, sort_order, features_json, package_type, billing_period))
            conn.commit()
            cur2.close()
            flash("Package added.", "success")
        return redirect(url_for("portal_admin.credit_packages"))

    cur.execute("SELECT * FROM credit_packages ORDER BY sort_order ASC, id ASC")
    packages = cur.fetchall() or []
    cur.close(); conn.close()

    for p in packages:
        p["price_fmt"] = money_fmt(int(p.get("price_pence") or 0), p.get("currency") or "gbp")
        # Parse the features JSON for the template
        raw_feat = p.get("features")
        if raw_feat:
            try:
                p["features_parsed"] = _json.loads(raw_feat) if isinstance(raw_feat, str) else raw_feat
            except Exception:
                p["features_parsed"] = {}
        else:
            p["features_parsed"] = {}

    return render_template("portal/admin_packages.html", packages=packages)


@portal_admin_bp.route("/credit-packages/<int:pkg_id>/toggle")
def credit_packages_toggle(pkg_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT is_active FROM credit_packages WHERE id=%s", (pkg_id,))
    row = cur.fetchone() or {}
    new_val = 0 if int(row.get("is_active") or 0) else 1
    cur2 = conn.cursor(buffered=True)
    cur2.execute("UPDATE credit_packages SET is_active=%s WHERE id=%s", (new_val, pkg_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()
    flash("Package updated.", "success")
    return redirect(url_for("portal_admin.credit_packages"))


@portal_admin_bp.route("/credit-packages/<int:pkg_id>/delete", methods=["POST"])
def credit_packages_delete(pkg_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT name FROM credit_packages WHERE id=%s", (pkg_id,))
    row = cur.fetchone()
    if row:
        cur2 = conn.cursor(buffered=True)
        cur2.execute("DELETE FROM credit_packages WHERE id=%s", (pkg_id,))
        conn.commit()
        cur2.close()
        insert_audit_log(
            admin_username=_admin_user(),
            action="admin_delete_package",
            details={"package_id": pkg_id, "package_name": row.get("name")},
        )
        flash(f"Package '{row.get('name')}' deleted.", "success")
    else:
        flash("Package not found.", "danger")
    cur.close(); conn.close()
    return redirect(url_for("portal_admin.credit_packages"))


@portal_admin_bp.route("/credit-packages/<int:pkg_id>/edit", methods=["POST"])
def credit_packages_edit(pkg_id: int):
    r = _require_admin()
    if r: return r

    name        = (request.form.get("name")        or "").strip()
    credits     = int(request.form.get("credits")  or 0)
    price_pence = int(float(request.form.get("price_gbp") or 0) * 100)
    vat_rate    = float(request.form.get("vat_rate")   or 20.0)
    is_active   = 1 if request.form.get("is_active") == "on" else 0
    sort_order  = int(request.form.get("sort_order")   or 0)

    # Stage 3 — package type and billing period
    raw_pkg_type_e   = (request.form.get("package_type") or "topup").strip()
    package_type_e   = raw_pkg_type_e if raw_pkg_type_e in ("topup", "subscription") else "topup"
    raw_billing_e    = (request.form.get("billing_period") or "").strip()
    billing_period_e = raw_billing_e if raw_billing_e in ("monthly", "annual") else None
    if package_type_e == "topup":
        billing_period_e = None

    feat_product_rec      = request.form.get("feature_product_recommendation") == "on"
    feat_related_products = request.form.get("feature_related_products") == "on"
    feat_cart_recovery    = request.form.get("feature_cart_recovery") == "on"
    feat_verified_specs   = request.form.get("feature_verified_specs_web_lookup") == "on"
    feat_chat_archive_30d = request.form.get("feature_chat_archive_30days") == "on"
    feat_chat_archive_unl = request.form.get("feature_chat_archive_unlimited") == "on"
    features_dict = {}
    if feat_product_rec:
        features_dict["product_recommendation"] = True
    if feat_product_rec and feat_related_products:
        features_dict["related_products"] = True
    if feat_cart_recovery:
        features_dict["cart_recovery"] = True
    if feat_verified_specs:
        features_dict["verified_specs_web_lookup"] = True
    # Chat Archive tiers — only one can be set; Unlimited wins if both ticked
    if feat_chat_archive_unl:
        features_dict["chat_archive_unlimited"] = True
    elif feat_chat_archive_30d:
        features_dict["chat_archive_30days"] = True
    # Custom features — one per line entered by admin
    custom_text = (request.form.get("custom_features_text") or "").strip()
    custom_list = [line.strip() for line in custom_text.splitlines() if line.strip()]
    if custom_list:
        features_dict["custom_features"] = custom_list
    features_json = _json.dumps(features_dict) if features_dict else None

    if not name or credits <= 0 or price_pence <= 0:
        flash("Name, credits and price are required.", "danger")
        return redirect(url_for("portal_admin.credit_packages"))

    conn = get_db_connection()
    cur = conn.cursor(buffered=True)
    cur.execute("""
        UPDATE credit_packages
        SET name=%s, credits=%s, price_pence=%s, vat_rate=%s,
            is_active=%s, sort_order=%s, features=%s,
            package_type=%s, billing_period=%s
        WHERE id=%s""",
        (name, credits, price_pence, vat_rate, is_active, sort_order, features_json,
         package_type_e, billing_period_e, pkg_id))
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(
        admin_username=_admin_user(),
        action="admin_edit_package",
        details={"package_id": pkg_id, "name": name, "credits": credits,
                 "price_pence": price_pence, "features": features_dict,
                 "package_type": package_type_e, "billing_period": billing_period_e},
    )
    flash(f"Package '{name}' updated.", "success")
    return redirect(url_for("portal_admin.credit_packages"))


# ══════════════════════════════════════════════════════════════════════════════
# PLUGIN DOWNLOADS (admin upload / manage)
# Customers download plugins from the onboarding wizard.
# Admin uploads zip files here and can replace them when new versions are ready.
# ══════════════════════════════════════════════════════════════════════════════

import os as _os
import werkzeug.utils as _wu

PLUGIN_UPLOAD_DIR = "/root/api-key-manager/static/plugin_zips"

@portal_admin_bp.route("/plugins", methods=["GET", "POST"])
def plugins():
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "upload":
            plugin_key   = (request.form.get("plugin_key")   or "").strip().lower()
            display_name = (request.form.get("display_name") or "").strip()
            version      = (request.form.get("version")      or "").strip()
            f = request.files.get("plugin_file")

            if not plugin_key or not display_name or not f or not f.filename:
                flash("Plugin key, display name, and zip file are all required.", "danger")
            elif not f.filename.lower().endswith(".zip"):
                flash("Only .zip files are allowed.", "danger")
            else:
                _os.makedirs(PLUGIN_UPLOAD_DIR, exist_ok=True)
                safe_name = _wu.secure_filename(f.filename)
                dest_path = _os.path.join(PLUGIN_UPLOAD_DIR, safe_name)
                f.save(dest_path)

                cur2 = conn.cursor(buffered=True)
                cur2.execute("""
                    INSERT INTO plugin_downloads (plugin_key, display_name, filename, file_path, version)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        display_name=%s, filename=%s, file_path=%s, version=%s,
                        uploaded_at=CURRENT_TIMESTAMP""",
                    (plugin_key, display_name, safe_name, dest_path, version or None,
                     display_name, safe_name, dest_path, version or None))
                conn.commit()
                cur2.close()

                insert_audit_log(admin_username=_admin_user(), action="plugin_upload",
                                 details={"plugin_key": plugin_key, "filename": safe_name, "version": version})
                flash(f"Plugin '{display_name}' uploaded successfully.", "success")

        elif action == "delete":
            plugin_key = (request.form.get("plugin_key") or "").strip()
            cur.execute("SELECT * FROM plugin_downloads WHERE plugin_key=%s", (plugin_key,))
            row = cur.fetchone()
            if row:
                # Remove file from disk
                try:
                    if _os.path.exists(row["file_path"]):
                        _os.remove(row["file_path"])
                except Exception:
                    pass
                cur2 = conn.cursor(buffered=True)
                cur2.execute("DELETE FROM plugin_downloads WHERE plugin_key=%s", (plugin_key,))
                conn.commit()
                cur2.close()
                insert_audit_log(admin_username=_admin_user(), action="plugin_delete",
                                 details={"plugin_key": plugin_key})
                flash("Plugin removed.", "success")

        return redirect(url_for("portal_admin.plugins"))

    cur.execute("SELECT * FROM plugin_downloads ORDER BY uploaded_at DESC")
    plugins_list = cur.fetchall() or []
    cur.close(); conn.close()

    return render_template("portal/admin_plugins.html", plugins=plugins_list)


# ══════════════════════════════════════════════════════════════════════════════
# INVOICES (admin)
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/invoices")
def invoices():
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("""
        SELECT i.id, i.invoice_number, i.credits, i.amount_pence, i.vat_pence,
               i.currency, i.status, i.created_at,
               c.email AS customer_email,
               CONCAT(COALESCE(c.first_name,''),' ',COALESCE(c.last_name,'')) AS customer_name,
               t.name AS tenant_name, t.domain
        FROM invoices i
        JOIN customers c ON c.id=i.customer_id
        JOIN tenants   t ON t.id=i.tenant_id
        ORDER BY i.created_at DESC LIMIT 500""")
    rows = cur.fetchall() or []
    cur.close(); conn.close()

    for row in rows:
        total = int(row.get("amount_pence") or 0) + int(row.get("vat_pence") or 0)
        row["total_fmt"] = money_fmt(total, row.get("currency") or "gbp")

    return render_template("portal/admin_invoices.html", invoices=rows)


@portal_admin_bp.route("/invoice/<int:invoice_id>/download")
def invoice_download(invoice_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT * FROM invoices WHERE id=%s", (invoice_id,))
    inv = cur.fetchone()
    cur.close(); conn.close()

    if not inv or inv.get("status") != "paid" or not inv.get("pdf_path"):
        flash("Invoice PDF not available.", "warning")
        return redirect(url_for("portal_admin.invoices"))

    if not os.path.exists(inv["pdf_path"]):
        flash("File missing.", "danger")
        return redirect(url_for("portal_admin.invoices"))

    return send_file(inv["pdf_path"], as_attachment=True,
                     download_name=f"{inv['invoice_number']}.pdf")


# ══════════════════════════════════════════════════════════════════════════════
# CART REVENUE RECOVERY DASHBOARD (admin)
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/recovery-queue")
def recovery_queue():
    r = _require_admin()
    if r: return r

    # Optional filters from query string
    status_filter  = (request.args.get("status")    or "").strip() or None
    tenant_filter  = (request.args.get("tenant_id") or "").strip()
    tenant_id_filter = int(tenant_filter) if tenant_filter.isdigit() else None

    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)

    try:
        # ── KPI counts ────────────────────────────────────────────────────────
        if tenant_id_filter:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='pending'     THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
                    SUM(CASE WHEN status='recovered'   THEN 1 ELSE 0 END) AS recovered,
                    SUM(CASE WHEN status='expired'     THEN 1 ELSE 0 END) AS expired
                FROM abandonment_queue
                WHERE tenant_id = %s
            """, (tenant_id_filter,))
        else:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='pending'     THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
                    SUM(CASE WHEN status='recovered'   THEN 1 ELSE 0 END) AS recovered,
                    SUM(CASE WHEN status='expired'     THEN 1 ELSE 0 END) AS expired
                FROM abandonment_queue
            """)
        stats = cur.fetchone() or {}
        for k in ("total", "pending", "in_progress", "recovered", "expired"):
            stats[k] = int(stats.get(k) or 0)

        # Conversion rate (recovered / total active sessions that reached recovery)
        eligible = stats["in_progress"] + stats["recovered"] + stats["expired"]
        stats["conversion_rate"] = (
            round(stats["recovered"] / eligible * 100, 1) if eligible > 0 else 0.0
        )

        # ── Queue rows ────────────────────────────────────────────────────────
        where_parts  = []
        params: list = []

        if tenant_id_filter:
            where_parts.append("q.tenant_id = %s")
            params.append(tenant_id_filter)

        if status_filter and status_filter in ("pending", "in_progress", "recovered", "expired"):
            where_parts.append("q.status = %s")
            params.append(status_filter)

        where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        params.append(200)  # LIMIT

        cur.execute(f"""
            SELECT q.id, q.tenant_id, q.session_id, q.intent_score, q.priority,
                   q.cart_value, q.customer_email, q.status, q.touches_sent,
                   q.expires_at, q.created_at, q.updated_at,
                   t.name AS tenant_name, t.domain
            FROM abandonment_queue q
            LEFT JOIN tenants t ON t.id = q.tenant_id
            {where_clause}
            ORDER BY q.updated_at DESC
            LIMIT %s
        """, params)
        rows = cur.fetchall() or []

        # ── Tenant list for filter dropdown ───────────────────────────────────
        cur.execute("""
            SELECT DISTINCT t.id, t.name, t.domain
            FROM tenants t
            INNER JOIN abandonment_queue q ON q.tenant_id = t.id
            ORDER BY t.name ASC LIMIT 200
        """)
        tenants_list = cur.fetchall() or []

    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

    # Format cart values for display
    for row in rows:
        cv = row.get("cart_value")
        row["cart_value_fmt"] = f"£{float(cv):.2f}" if cv else "—"

    return render_template(
        "portal/admin_recovery_queue.html",
        stats=stats,
        rows=rows,
        tenants_list=tenants_list,
        status_filter=status_filter or "",
        tenant_id_filter=tenant_id_filter or "",
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN SETTINGS — Password Reset & Customer Email Change
# ══════════════════════════════════════════════════════════════════════════════

import bcrypt as _bcrypt_admin


def _verify_admin_password(plain: str, stored_admin: dict) -> bool:
    """
    Supports both legacy plain-text passwords and new bcrypt hashes.
    If password_hash column exists and is set, use bcrypt.
    Otherwise fall back to plain-text comparison (legacy).
    """
    pw_hash = stored_admin.get("password_hash")
    if pw_hash:
        try:
            return _bcrypt_admin.checkpw(plain.encode("utf-8"), pw_hash.encode("utf-8"))
        except Exception:
            return False
    # Legacy plain-text fallback
    return plain == stored_admin.get("password", "")


def _hash_admin_password(plain: str) -> str:
    return _bcrypt_admin.hashpw(plain.encode("utf-8"), _bcrypt_admin.gensalt()).decode("utf-8")


def _get_trial_default_days() -> int:
    """Read trial_default_days from portal_settings table.
    Falls back to 14 if the table doesn't exist yet or the key is missing."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True, buffered=True)
        cur.execute(
            "SELECT setting_value FROM portal_settings WHERE setting_key='trial_default_days'")
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row.get("setting_value"):
            return int(row["setting_value"])
    except Exception:
        pass
    return 14  # default


@portal_admin_bp.route("/settings", methods=["GET"])
def admin_settings():
    r = _require_admin()
    if r: return r
    username          = _admin_user()
    trial_default_days = _get_trial_default_days()
    return render_template("portal/admin_settings.html",
                           username=username,
                           trial_default_days=trial_default_days)


@portal_admin_bp.route("/settings/trial-days", methods=["POST"])
def admin_save_trial_days():
    """Save the default trial duration (days) into portal_settings."""
    r = _require_admin()
    if r: return r

    raw = (request.form.get("trial_default_days") or "").strip()
    try:
        days = int(raw)
        if not (1 <= days <= 365):
            raise ValueError
    except ValueError:
        flash("Please enter a number between 1 and 365.", "danger")
        return redirect(url_for("portal_admin.admin_settings"))

    try:
        conn = get_db_connection()
        cur  = conn.cursor(buffered=True)
        cur.execute("""
            INSERT INTO portal_settings (setting_key, setting_value)
            VALUES ('trial_default_days', %s)
            ON DUPLICATE KEY UPDATE setting_value = %s
        """, (str(days), str(days)))
        conn.commit()
        cur.close(); conn.close()
        insert_audit_log(
            admin_username=_admin_user(),
            action="admin_trial_days_updated",
            details={"trial_default_days": days},
        )
        flash(f"Default trial duration updated to {days} days ✅", "success")
    except Exception as e:
        print("⚠️ admin_save_trial_days error:", e)
        flash("Could not save setting. Please try again.", "danger")

    return redirect(url_for("portal_admin.admin_settings"))


@portal_admin_bp.route("/settings/password", methods=["POST"])
def admin_change_password():
    """Allow an admin to change their own login password."""
    r = _require_admin()
    if r: return r

    username    = _admin_user()
    current_pw  = (request.form.get("current_password") or "").strip()
    new_pw      = (request.form.get("new_password")     or "").strip()
    confirm_pw  = (request.form.get("confirm_password") or "").strip()

    if not current_pw or not new_pw or not confirm_pw:
        flash("All password fields are required.", "danger")
        return redirect(url_for("portal_admin.admin_settings"))

    if new_pw != confirm_pw:
        flash("New passwords do not match.", "danger")
        return redirect(url_for("portal_admin.admin_settings"))

    if len(new_pw) < 8:
        flash("New password must be at least 8 characters.", "danger")
        return redirect(url_for("portal_admin.admin_settings"))

    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT * FROM admin_users WHERE username=%s", (username,))
    admin = cur.fetchone()

    if not admin:
        cur.close(); conn.close()
        flash("Admin account not found.", "danger")
        return redirect(url_for("portal_admin.admin_settings"))

    if not _verify_admin_password(current_pw, admin):
        cur.close(); conn.close()
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("portal_admin.admin_settings"))

    new_hash = _hash_admin_password(new_pw)
    cur2 = conn.cursor(buffered=True)
    # Update both password (legacy) and password_hash (bcrypt)
    cur2.execute(
        "UPDATE admin_users SET password=%s, password_hash=%s WHERE username=%s",
        (new_pw, new_hash, username)
    )
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(action="admin_password_changed",
                     admin_username=username,
                     details={"changed_by": username})
    flash("Admin password changed successfully ✅", "success")
    return redirect(url_for("portal_admin.admin_settings"))


@portal_admin_bp.route("/customers/<int:customer_id>/change-email", methods=["POST"])
def customer_change_email(customer_id: int):
    """Admin can update a customer's email address."""
    r = _require_admin()
    if r: return r

    new_email = (request.form.get("new_email") or "").strip().lower()
    if not new_email or "@" not in new_email or "." not in new_email.split("@")[-1]:
        flash("Please enter a valid email address.", "danger")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)

    # Check the customer exists
    cur.execute("SELECT id, email, tenant_id FROM customers WHERE id=%s", (customer_id,))
    c = cur.fetchone()
    if not c:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    old_email = c.get("email") or ""

    # Check for duplicate email
    cur.execute("SELECT id FROM customers WHERE email=%s AND id != %s", (new_email, customer_id))
    dup = cur.fetchone()
    if dup:
        cur.close(); conn.close()
        flash(f"Email address {new_email} is already in use by another account.", "danger")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    cur2 = conn.cursor(buffered=True)
    cur2.execute("UPDATE customers SET email=%s WHERE id=%s", (new_email, customer_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(
        action="admin_customer_email_changed",
        admin_username=_admin_user(),
        tenant_id=int(c.get("tenant_id") or 0),
        details={"customer_id": customer_id, "old_email": old_email, "new_email": new_email},
    )
    flash(f"Customer email updated from {old_email} → {new_email} ✅", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


@portal_admin_bp.route("/customers/<int:customer_id>/send-reset", methods=["POST"])
def customer_send_reset(customer_id: int):
    """Admin triggers a password reset email for a customer."""
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT id, first_name, email FROM customers WHERE id=%s", (customer_id,))
    c = cur.fetchone()

    if not c:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    from portal_utils import make_token, utc_now_naive, send_email
    from datetime import timedelta

    token   = make_token(24)
    expires = utc_now_naive() + timedelta(hours=2)

    cur2 = conn.cursor(buffered=True)
    cur2.execute("UPDATE customers SET reset_token=%s, reset_expires_at=%s WHERE id=%s",
                 (token, expires, customer_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")
    link    = f"{PORTAL_BASE_URL}/reset?token={token}"
    greeting = (c.get("first_name") or "there").strip()
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px">
      <h2 style="color:#030C18">Reset your password</h2>
      <p>Hi {greeting},</p>
      <p>A PhiXtra admin has sent you a password reset link.</p>
      <p>Click below to set a new password. This link expires in 2 hours.</p>
      <p><a href="{link}" style="background:#030C18;color:#fff;padding:10px 18px;border-radius:12px;
                                  text-decoration:none;display:inline-block">Reset password</a></p>
      <p style="color:#888;font-size:12px">If you didn't request this, please ignore this email.</p>
    </div>"""
    sent = send_email(c["email"], "Reset your PhiXtra password", html, text_body=f"Reset: {link}")

    insert_audit_log(
        action="admin_sent_customer_password_reset",
        admin_username=_admin_user(),
        details={"customer_id": customer_id, "email": c["email"], "email_sent": sent},
    )

    if sent:
        flash(f"Password reset email sent to {c['email']} ✅", "success")
    else:
        flash(f"Reset token created but email could not be sent. Link: {link}", "warning")

    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP MERCHANT ONBOARDING
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/onboard-wa", methods=["GET", "POST"])
def onboard_wa():
    r = _require_admin()
    if r: return r

    if request.method == "POST":
        from portal_routes import provision_whatsapp_merchant, _normalise_phone
        raw_phone     = (request.form.get("phone")         or "").strip()
        business_name = (request.form.get("business_name") or "").strip()
        notes         = (request.form.get("notes")         or "").strip()

        if not raw_phone or not business_name:
            flash("Phone number and business name are both required.", "danger")
            return redirect(url_for("portal_admin.onboard_wa"))

        phone = _normalise_phone(raw_phone)
        if not phone:
            flash(f"Could not parse phone number: {raw_phone!r}. Use +234… or 0801… format.", "danger")
            return redirect(url_for("portal_admin.onboard_wa"))

        try:
            result = provision_whatsapp_merchant(phone, business_name)
            insert_audit_log(
                action="admin_onboarded_wa_merchant",
                admin_username=_admin_user(),
                tenant_id=result["tenant_id"],
                details={"phone": phone, "business_name": business_name, "notes": notes},
            )
            flash(
                f"✅ <strong>{business_name}</strong> provisioned. "
                f"Tenant #{result['tenant_id']} · Customer #{result['customer_id']}. "
                f"They can log in at <a href='https://portal.phixtra.com/wa-login' target='_blank'>"
                f"portal.phixtra.com/wa-login</a> using <strong>{phone}</strong>.",
                "success"
            )
        except Exception as e:
            flash(f"Provisioning failed: {e}", "danger")

        return redirect(url_for("portal_admin.onboard_wa"))

    # ── GET: load existing WA merchants ──────────────────────────────────────
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT
            t.id            AS tenant_id,
            t.name          AS business_name,
            t.status,
            t.created_at    AS provisioned_at,
            c.phone_number,
            c.id            AS customer_id,
            c.is_active,
            COALESCE(tb.token_balance, 0) AS token_balance
        FROM tenants t
        JOIN customers c        ON c.tenant_id = t.id
        LEFT JOIN tenant_balances tb ON tb.tenant_id = t.id
        WHERE t.source_type = 'whatsapp'
        ORDER BY t.created_at DESC
        LIMIT 100
    """)
    merchants = cur.fetchall() or []
    cur.close(); conn.close()

    return render_template("portal/admin_onboard_wa.html",
                           merchants=merchants)
