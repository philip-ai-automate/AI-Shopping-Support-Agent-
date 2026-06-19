"""
portal_admin_routes.py  — Phase 1 admin portal (/admin/*)
admin_users table and db.py are UNCHANGED. app.py (keys.phixtra.com) is UNTOUCHED.
"""
import psycopg2
import psycopg2.extras
import psycopg2.errors
import os
import secrets
import string
import json as _json
import csv
import io
import bcrypt
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, send_file, Response)

from db import get_db_connection, insert_audit_log
from portal_utils import money_fmt, tokens_to_credits, credits_to_tokens, send_email_with_attachment

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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # NOTE: t.features is NOT included in this JOIN.
    # Fetching it here caused the 500 error because the `features` column
    # may not exist in the database yet (migration may not have run).
    # We fetch it separately below with a safe try/except instead.
    cur.execute("""
        SELECT c.*, t.name AS tenant_name, t.domain,
               COALESCE(tb.token_balance,0) AS token_balance,
               t.plan_id, t.billing_cycle, t.plan_period_start,
               COALESCE(p.slug,'free') AS plan_slug,
               COALESCE(p.name,'Free') AS plan_name,
               COALESCE(p.ai_messages_limit,100) AS ai_messages_limit
        FROM customers c
        JOIN tenants t ON t.id=c.tenant_id
        LEFT JOIN tenant_balances tb ON tb.tenant_id=t.id
        LEFT JOIN plans p ON p.id = t.plan_id
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

    onboarding_other = None
    try:
        cur.execute("SELECT onboarding_other FROM tenants WHERE id=%s", (tenant_id,))
        ob_row = cur.fetchone() or {}
        onboarding_other = ob_row.get("onboarding_other") or None
    except Exception:
        onboarding_other = None

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

    # Document counts per type for the index management panel
    doc_counts = {}
    try:
        cur.execute("""
            SELECT type, COUNT(*) AS cnt
            FROM documents
            WHERE tenant_id=%s
            GROUP BY type
            ORDER BY type""", (tenant_id,))
        for dc_row in cur.fetchall():
            doc_counts[dc_row["type"]] = int(dc_row["cnt"])
    except Exception:
        doc_counts = {}

    # Plan data for assignment panel
    cur.execute("SELECT id, slug, name FROM plans WHERE is_active=TRUE ORDER BY sort_order")
    all_plans = cur.fetchall() or []

    # Messages used this billing period
    from datetime import date as _d
    period_start = customer.get("plan_period_start") or _d.today().replace(day=1)
    try:
        cur.execute("""
            SELECT COUNT(*) AS used FROM usage_events
            WHERE tenant_id=%s AND created_at >= %s
        """, (tenant_id, period_start))
        msgs_used = int((cur.fetchone() or {}).get("used") or 0)
    except Exception:
        msgs_used = 0

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
                           doc_counts=doc_counts,
                           all_plans=all_plans,
                           msgs_used=msgs_used,
                           onboarding_other=onboarding_other,
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id   = int(row["tenant_id"])
    delta_tokens = int(delta_credits * 5000)

    cur2 = conn.cursor()
    cur2.execute("INSERT INTO tenant_balances (tenant_id, token_balance) VALUES (%s, 0) ON CONFLICT (tenant_id) DO NOTHING", (tenant_id,))
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT is_active FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    new_val = 0 if int(row.get("is_active") or 0) else 1
    cur2 = conn.cursor()
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
    feat_wa_templates     = request.form.get("feat_wa_message_templates") == "on"

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

    # WhatsApp Message Templates — cart abandonment & order status via Meta-approved templates
    if feat_wa_templates:
        features["whatsapp_message_templates"] = True

    features_json = _json.dumps(features) if features else None

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id = int(row["tenant_id"])
    cur2 = conn.cursor()
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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

    cur2 = conn.cursor()
    cur2.execute(
        "UPDATE api_keys SET trial_expires_at=%s, is_active=TRUE WHERE id=%s",
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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

    cur2 = conn.cursor()
    cur2.execute(
        "UPDATE api_keys SET trial_activated_at=%s, trial_expires_at=%s, is_active=TRUE, tokens_used=0 WHERE id=%s",
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Use ak.api_key_plain (live value from api_keys) so admin always sees
    # the key that actually authenticates, not the audit-log snapshot which
    # can diverge if a key was regenerated after creation.
    if q:
        cur.execute("""
            SELECT al.id,
                   COALESCE(ak.api_key_plain, al.api_key_plain) AS api_key_plain,
                   al.api_key_last4, al.website, al.key_type,
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
            SELECT al.id,
                   COALESCE(ak.api_key_plain, al.api_key_plain) AS api_key_plain,
                   al.api_key_last4, al.website, al.key_type,
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, tenant_id, website, key_type FROM api_keys WHERE id=%s", (key_id,))
    k = cur.fetchone()
    if k:
        cur2 = conn.cursor()
        cur2.execute("UPDATE api_keys SET is_active=FALSE WHERE id=%s", (key_id,))
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, tenant_id, website, key_type FROM api_keys WHERE id=%s", (key_id,))
    k = cur.fetchone()
    if k:
        cur2 = conn.cursor()
        if k.get("key_type") == "trial":
            default_days = _get_trial_default_days()
            from datetime import datetime as _dt, timedelta
            new_expiry = _dt.utcnow() + timedelta(days=default_days)
            cur2.execute(
                "UPDATE api_keys SET is_active=TRUE, trial_expires_at=%s WHERE id=%s",
                (new_expiry, key_id)
            )
        else:
            cur2.execute("UPDATE api_keys SET is_active=TRUE WHERE id=%s", (key_id,))
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

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
        WHERE tenant_id=%s AND key_type='paid' AND is_active=TRUE
        LIMIT 1""", (tenant_id,))
    if cur.fetchone():
        cur.close(); conn.close()
        flash("An active paid key already exists for this tenant. Revoke it first.", "warning")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    plain_key, hashed_key = _admin_generate_api_key_and_hash()
    last4 = plain_key[-4:]

    try:
        cur2 = conn.cursor()
        cur2.execute("""
            INSERT INTO api_keys
                (tenant_id, api_key_hash, api_key_plain, is_active, website, key_type, token_limit, tokens_used)
            VALUES (%s, %s, %s, TRUE, %s, 'paid', NULL, 0)
            RETURNING id""",
            (tenant_id, hashed_key, plain_key, domain))
        api_key_id = cur2.fetchone()[0]
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        name        = (request.form.get("name")        or "").strip()
        credits     = int(request.form.get("credits")  or 0)
        price_pence = int(float(request.form.get("price_gbp") or 0) * 100)
        vat_rate    = float(request.form.get("vat_rate")   or 20.0)
        is_active = 1 if request.form.get("is_active") == "on" else 0
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
        feat_wa_templates     = request.form.get("feature_wa_message_templates") == "on"
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
        # WhatsApp Message Templates
        if feat_wa_templates:
            features_dict["whatsapp_message_templates"] = True
        # Custom features — one per line entered by admin
        custom_text = (request.form.get("custom_features_text") or "").strip()
        custom_list = [line.strip() for line in custom_text.splitlines() if line.strip()]
        if custom_list:
            features_dict["custom_features"] = custom_list
        features_json = _json.dumps(features_dict) if features_dict else None

        if not name or credits <= 0 or price_pence <= 0:
            flash("Name, credits and price are required.", "danger")
        else:
            cur2 = conn.cursor()
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT is_active FROM credit_packages WHERE id=%s", (pkg_id,))
    row = cur.fetchone() or {}
    new_val = 0 if int(row.get("is_active") or 0) else 1
    cur2 = conn.cursor()
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT name FROM credit_packages WHERE id=%s", (pkg_id,))
    row = cur.fetchone()
    if row:
        cur2 = conn.cursor()
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
    is_active=TRUE if request.form.get("is_active") == "on" else 0
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
    feat_wa_templates     = request.form.get("feature_wa_message_templates") == "on"
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
    # WhatsApp Message Templates
    if feat_wa_templates:
        features_dict["whatsapp_message_templates"] = True
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
    cur = conn.cursor()
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

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

                cur2 = conn.cursor()
                cur2.execute("""
                    INSERT INTO plugin_downloads (plugin_key, display_name, filename, file_path, version)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (plugin_key) DO UPDATE SET
                        display_name=EXCLUDED.display_name, filename=EXCLUDED.filename,
                        file_path=EXCLUDED.file_path, version=EXCLUDED.version,
                        uploaded_at=CURRENT_TIMESTAMP""",
                    (plugin_key, display_name, safe_name, dest_path, version or None))
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
                cur2 = conn.cursor()
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

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
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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


@portal_admin_bp.route("/onboarding-qr")
def onboarding_qr():
    """Serve the WhatsApp onboarding QR code card as a downloadable PNG."""
    r = _require_admin()
    if r: return r
    import os
    qr_path = os.path.join(
        os.path.dirname(__file__), "static", "portal", "whatsapp_setup_qr_card.png"
    )
    return send_file(qr_path, mimetype="image/png",
                     as_attachment=True, download_name="phixtra_whatsapp_setup_qr.png")


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
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO portal_settings (setting_key, setting_value)
            VALUES ('trial_default_days', %s)
            ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
        """, (str(days),))
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
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
    cur2 = conn.cursor()
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
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

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

    cur2 = conn.cursor()
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
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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

    cur2 = conn.cursor()
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
# SEARCH INDEX MANAGEMENT (admin-only)
# Deletes documents from the pgvector documents table for a specific tenant.
# After deleting, the customer must run Full Sync from their WordPress plugin.
# ══════════════════════════════════════════════════════════════════════════════

_VALID_DOC_TYPES = {"product", "post", "page", "order", "customer"}


@portal_admin_bp.route("/customers/<int:customer_id>/rebuild-index", methods=["POST"])
def customer_rebuild_index(customer_id: int):
    r = _require_admin()
    if r: return r

    doc_type = (request.form.get("doc_type") or "").strip().lower()

    if doc_type and doc_type not in _VALID_DOC_TYPES:
        flash(f"Invalid document type: {doc_type!r}. Must be one of: {', '.join(sorted(_VALID_DOC_TYPES))}.", "danger")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id = int(row["tenant_id"])

    cur2 = conn.cursor()
    if doc_type:
        cur2.execute(
            "DELETE FROM documents WHERE tenant_id = %s AND type = %s",
            (tenant_id, doc_type),
        )
    else:
        # Delete all types except verified_spec (same rule as /sync/rebuild-index endpoint)
        cur2.execute(
            "DELETE FROM documents WHERE tenant_id = %s AND type != 'verified_spec'",
            (tenant_id,),
        )

    deleted = cur2.rowcount
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(
        admin_username=_admin_user(),
        action="admin_rebuild_index",
        tenant_id=tenant_id,
        details={
            "customer_id": customer_id,
            "doc_type": doc_type or "all",
            "deleted_count": deleted,
        },
    )

    if doc_type:
        flash(
            f"Deleted {deleted:,} '{doc_type}' documents for this tenant. "
            f"Ask them to run Full Sync from WordPress → PhiXtra Export → PhiXtra Sync tab.",
            "success",
        )
    else:
        flash(
            f"Deleted {deleted:,} documents (all types) for this tenant. "
            f"Ask them to run Full Sync from WordPress → PhiXtra Export → PhiXtra Sync tab.",
            "success",
        )

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
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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


# ══════════════════════════════════════════════════════════════════════════════
# PHONE CATALOGUE
# ══════════════════════════════════════════════════════════════════════════════

_CAT_COLS = [
    "product_id","brand","model_name","variant_name","release_year",
    "price_category","network_type","screen_size_inches","display_type",
    "refresh_rate_hz","screen_resolution","chipset_model","ram","storage",
    "battery_capacity_mah","fast_charging_watts","rear_camera_main_mp",
    "front_camera_mp","video_recording","gaming_rating","battery_performance",
    "camera_quality_rating","wifi_version","bluetooth_version","nfc",
    "body_material","water_resistance","fingerprint_type","available_colors",
    "nigeria_market_price_naira","best_for","search_intent_tags",
    "ai_summary","ai_sales_pitch","is_active",
]

_FILTER_COLS = ["brand","price_category","network_type","display_type","nfc","is_active"]

PAGE_SIZE = 50


def _catalogue_brands(cur):
    cur.execute("SELECT DISTINCT brand FROM phone_catalogue WHERE brand IS NOT NULL ORDER BY brand")
    return [r["brand"] for r in (cur.fetchall() or [])]


@portal_admin_bp.route("/catalogue/phones")
def catalogue():
    r = _require_admin()
    if r: return r

    q       = (request.args.get("q") or "").strip()
    brand   = (request.args.get("brand") or "").strip()
    pcat    = (request.args.get("price_category") or "").strip()
    network = (request.args.get("network_type") or "").strip()
    nfc     = (request.args.get("nfc") or "").strip()
    active  = request.args.get("is_active", "")
    page    = max(1, int(request.args.get("page", 1)))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    conditions = []
    params     = []

    if q:
        conditions.append(
            "to_tsvector('english', COALESCE(brand,'') || ' ' || COALESCE(model_name,'') || ' ' || COALESCE(variant_name,'') || ' ' || COALESCE(search_intent_tags,'')) @@ plainto_tsquery('english', %s)"
        )
        params.append(q)
    if brand:
        conditions.append("brand = %s"); params.append(brand)
    if pcat:
        conditions.append("price_category = %s"); params.append(pcat)
    if network:
        conditions.append("network_type = %s"); params.append(network)
    if nfc:
        conditions.append("nfc = %s"); params.append(nfc)
    if active in ("true", "false"):
        conditions.append("is_active = %s"); params.append(active == "true")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    cur.execute(f"SELECT COUNT(*) AS n FROM phone_catalogue {where}", params)
    total = cur.fetchone()["n"]

    offset = (page - 1) * PAGE_SIZE
    cur.execute(
        f"""SELECT id, product_id, brand, model_name, variant_name, release_year,
                   price_category, network_type, nigeria_market_price_naira, is_active
            FROM phone_catalogue {where}
            ORDER BY brand, model_name, variant_name
            LIMIT %s OFFSET %s""",
        params + [PAGE_SIZE, offset],
    )
    rows = cur.fetchall() or []

    brands   = _catalogue_brands(cur)
    cur.execute("SELECT DISTINCT price_category FROM phone_catalogue WHERE price_category IS NOT NULL ORDER BY price_category")
    pcats = [r["price_category"] for r in (cur.fetchall() or [])]
    cur.execute("SELECT DISTINCT network_type FROM phone_catalogue WHERE network_type IS NOT NULL ORDER BY network_type")
    networks = [r["network_type"] for r in (cur.fetchall() or [])]

    cur.close(); conn.close()

    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    return render_template(
        "portal/admin_catalogue.html",
        rows=rows, total=total, page=page, pages=pages,
        brands=brands, pcats=pcats, networks=networks,
        q=q, brand=brand, price_category=pcat,
        network_type=network, nfc=nfc, is_active=active,
    )


@portal_admin_bp.route("/catalogue/phones/<int:phone_id>/edit", methods=["GET", "POST"])
def catalogue_edit(phone_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        data = request.form
        cur.execute(
            """UPDATE phone_catalogue SET
                brand=%s, model_name=%s, variant_name=%s, release_year=%s,
                price_category=%s, network_type=%s, screen_size_inches=%s,
                display_type=%s, refresh_rate_hz=%s, screen_resolution=%s,
                chipset_model=%s, ram=%s, storage=%s, battery_capacity_mah=%s,
                fast_charging_watts=%s, rear_camera_main_mp=%s, front_camera_mp=%s,
                video_recording=%s, gaming_rating=%s, battery_performance=%s,
                camera_quality_rating=%s, wifi_version=%s, bluetooth_version=%s,
                nfc=%s, body_material=%s, water_resistance=%s, fingerprint_type=%s,
                available_colors=%s, nigeria_market_price_naira=%s, best_for=%s,
                search_intent_tags=%s, ai_summary=%s, ai_sales_pitch=%s,
                is_active=%s, updated_at=NOW()
               WHERE id=%s""",
            (
                data.get("brand") or None,
                data.get("model_name") or None,
                data.get("variant_name") or None,
                int(data["release_year"]) if data.get("release_year") else None,
                data.get("price_category") or None,
                data.get("network_type") or None,
                float(data["screen_size_inches"]) if data.get("screen_size_inches") else None,
                data.get("display_type") or None,
                int(data["refresh_rate_hz"]) if data.get("refresh_rate_hz") else None,
                data.get("screen_resolution") or None,
                data.get("chipset_model") or None,
                data.get("ram") or None,
                data.get("storage") or None,
                int(data["battery_capacity_mah"]) if data.get("battery_capacity_mah") else None,
                int(data["fast_charging_watts"]) if data.get("fast_charging_watts") else None,
                int(data["rear_camera_main_mp"]) if data.get("rear_camera_main_mp") else None,
                int(data["front_camera_mp"]) if data.get("front_camera_mp") else None,
                data.get("video_recording") or None,
                data.get("gaming_rating") or None,
                data.get("battery_performance") or None,
                data.get("camera_quality_rating") or None,
                data.get("wifi_version") or None,
                data.get("bluetooth_version") or None,
                data.get("nfc") or None,
                data.get("body_material") or None,
                data.get("water_resistance") or None,
                data.get("fingerprint_type") or None,
                data.get("available_colors") or None,
                float(data["nigeria_market_price_naira"]) if data.get("nigeria_market_price_naira") else None,
                data.get("best_for") or None,
                data.get("search_intent_tags") or None,
                data.get("ai_summary") or None,
                data.get("ai_sales_pitch") or None,
                "is_active" in data,
                phone_id,
            ),
        )
        conn.commit()
        insert_audit_log(action="catalogue_edit", admin_username=_admin_user(),
                         details={"phone_id": phone_id})
        cur.close(); conn.close()
        flash("Phone updated.", "success")
        return redirect(url_for("portal_admin.catalogue"))

    cur.execute("SELECT * FROM phone_catalogue WHERE id=%s", (phone_id,))
    phone = cur.fetchone()
    cur.close(); conn.close()

    if not phone:
        flash("Phone not found.", "danger")
        return redirect(url_for("portal_admin.catalogue"))

    return render_template("portal/admin_catalogue_edit.html", phone=phone)


@portal_admin_bp.route("/catalogue/phones/<int:phone_id>/delete", methods=["POST"])
def catalogue_delete(phone_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM phone_catalogue WHERE id=%s", (phone_id,))
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(action="catalogue_delete", admin_username=_admin_user(),
                     details={"phone_id": phone_id})
    flash("Phone deleted.", "success")
    return redirect(url_for("portal_admin.catalogue"))


@portal_admin_bp.route("/catalogue/phones/bulk", methods=["POST"])
def catalogue_bulk():
    r = _require_admin()
    if r: return r

    action  = request.form.get("bulk_action", "")
    ids_raw = request.form.getlist("selected_ids")
    ids     = [int(i) for i in ids_raw if i.isdigit()]

    if not ids:
        flash("No rows selected.", "warning")
        return redirect(url_for("portal_admin.catalogue"))

    conn = get_db_connection()
    cur  = conn.cursor()

    if action == "delete":
        cur.execute("DELETE FROM phone_catalogue WHERE id = ANY(%s)", (ids,))
        conn.commit()
        insert_audit_log(action="catalogue_bulk_delete", admin_username=_admin_user(),
                         details={"count": len(ids)})
        flash(f"{len(ids)} phone(s) deleted.", "success")

    elif action == "activate":
        cur.execute("UPDATE phone_catalogue SET is_active=TRUE, updated_at=NOW() WHERE id = ANY(%s)", (ids,))
        conn.commit()
        flash(f"{len(ids)} phone(s) activated.", "success")

    elif action == "deactivate":
        cur.execute("UPDATE phone_catalogue SET is_active=FALSE, updated_at=NOW() WHERE id = ANY(%s)", (ids,))
        conn.commit()
        flash(f"{len(ids)} phone(s) deactivated.", "success")

    elif action == "set_field":
        field  = request.form.get("bulk_field", "").strip()
        value  = request.form.get("bulk_value", "").strip()
        allowed = {"brand","price_category","network_type","display_type","nfc",
                   "body_material","water_resistance","fingerprint_type","gaming_rating",
                   "battery_performance","camera_quality_rating","best_for"}
        if field not in allowed:
            flash("Invalid field for bulk update.", "danger")
        else:
            cur.execute(
                f"UPDATE phone_catalogue SET {field}=%s, updated_at=NOW() WHERE id = ANY(%s)",
                (value or None, ids),
            )
            conn.commit()
            insert_audit_log(action="catalogue_bulk_field_update", admin_username=_admin_user(),
                             details={"field": field, "value": value, "count": len(ids)})
            flash(f"{len(ids)} phone(s) updated — {field} set to '{value}'.", "success")

    cur.close(); conn.close()
    return redirect(url_for("portal_admin.catalogue"))


@portal_admin_bp.route("/catalogue/phones/export")
def catalogue_export():
    r = _require_admin()
    if r: return r

    q       = (request.args.get("q") or "").strip()
    brand   = (request.args.get("brand") or "").strip()
    pcat    = (request.args.get("price_category") or "").strip()
    network = (request.args.get("network_type") or "").strip()
    nfc     = (request.args.get("nfc") or "").strip()
    active  = request.args.get("is_active", "")

    conditions, params = [], []
    if q:
        conditions.append(
            "to_tsvector('english', COALESCE(brand,'') || ' ' || COALESCE(model_name,'') || ' ' || COALESCE(variant_name,'') || ' ' || COALESCE(search_intent_tags,'')) @@ plainto_tsquery('english', %s)"
        )
        params.append(q)
    if brand:
        conditions.append("brand = %s"); params.append(brand)
    if pcat:
        conditions.append("price_category = %s"); params.append(pcat)
    if network:
        conditions.append("network_type = %s"); params.append(network)
    if nfc:
        conditions.append("nfc = %s"); params.append(nfc)
    if active in ("true", "false"):
        conditions.append("is_active = %s"); params.append(active == "true")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        f"SELECT {', '.join(_CAT_COLS)} FROM phone_catalogue {where} ORDER BY brand, model_name, variant_name",
        params,
    )
    rows = cur.fetchall() or []
    cur.close(); conn.close()

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=_CAT_COLS)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in _CAT_COLS})

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=phone_catalogue_export.csv"},
    )


@portal_admin_bp.route("/catalogue/phones/import", methods=["GET", "POST"])
def catalogue_import():
    r = _require_admin()
    if r: return r

    if request.method == "GET":
        return render_template("portal/admin_catalogue_import.html")

    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "danger")
        return redirect(url_for("portal_admin.catalogue_import"))

    filename = file.filename.lower()
    inserted = updated = errors = 0

    conn = get_db_connection()
    cur  = conn.cursor()

    try:
        if filename.endswith(".csv"):
            stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
            reader = csv.DictReader(stream)
            rows_iter = reader
        elif filename.endswith((".xlsx", ".xls")):
            import openpyxl
            wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
            ws = wb.active
            headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
            rows_iter = (dict(zip(headers, [c for c in row])) for row in ws.iter_rows(min_row=2, values_only=True))
        else:
            flash("Only .csv or .xlsx files are supported.", "danger")
            cur.close(); conn.close()
            return redirect(url_for("portal_admin.catalogue_import"))

        for row in rows_iter:
            pid = str(row.get("product_id") or "").strip()
            if not pid:
                errors += 1
                continue
            try:
                cur.execute(
                    """INSERT INTO phone_catalogue (
                        product_id,brand,model_name,variant_name,release_year,price_category,
                        network_type,screen_size_inches,display_type,refresh_rate_hz,screen_resolution,
                        chipset_model,ram,storage,battery_capacity_mah,fast_charging_watts,
                        rear_camera_main_mp,front_camera_mp,video_recording,gaming_rating,
                        battery_performance,camera_quality_rating,wifi_version,bluetooth_version,
                        nfc,body_material,water_resistance,fingerprint_type,available_colors,
                        nigeria_market_price_naira,best_for,search_intent_tags,ai_summary,ai_sales_pitch
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (product_id) DO UPDATE SET
                        brand=%s,model_name=%s,variant_name=%s,updated_at=NOW()""",
                    (
                        pid,
                        row.get("brand"), row.get("model_name"), row.get("variant_name"),
                        int(row["release_year"]) if row.get("release_year") else None,
                        row.get("price_category"), row.get("network_type"),
                        float(row["screen_size_inches"]) if row.get("screen_size_inches") else None,
                        row.get("display_type"),
                        int(row["refresh_rate_hz"]) if row.get("refresh_rate_hz") else None,
                        row.get("screen_resolution"), row.get("chipset_model"),
                        row.get("ram"), row.get("storage"),
                        int(row["battery_capacity_mah"]) if row.get("battery_capacity_mah") else None,
                        int(row["fast_charging_watts"]) if row.get("fast_charging_watts") else None,
                        int(row["rear_camera_main_mp"]) if row.get("rear_camera_main_mp") else None,
                        int(row["front_camera_mp"]) if row.get("front_camera_mp") else None,
                        row.get("video_recording"), row.get("gaming_rating"),
                        row.get("battery_performance"), row.get("camera_quality_rating"),
                        row.get("wifi_version"), row.get("bluetooth_version"), row.get("nfc"),
                        row.get("body_material"), row.get("water_resistance"),
                        row.get("fingerprint_type"), row.get("available_colors"),
                        float(row["nigeria_market_price_naira"]) if row.get("nigeria_market_price_naira") else None,
                        row.get("best_for"), row.get("search_intent_tags"),
                        row.get("ai_summary"), row.get("ai_sales_pitch"),
                        # ON CONFLICT update fields
                        row.get("brand"), row.get("model_name"), row.get("variant_name"),
                    ),
                )
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    updated += 1
            except Exception:
                errors += 1

        conn.commit()
    except Exception as e:
        conn.rollback()
        flash(f"Import failed: {e}", "danger")
        cur.close(); conn.close()
        return redirect(url_for("portal_admin.catalogue_import"))

    cur.close(); conn.close()
    insert_audit_log(action="catalogue_import", admin_username=_admin_user(),
                     details={"inserted": inserted, "updated": updated, "errors": errors})
    flash(f"Import complete — {inserted} added, {updated} updated, {errors} skipped.", "success")
    return redirect(url_for("portal_admin.catalogue"))


# ══════════════════════════════════════════════════════════════════════════════
# PHONE BRANDS
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/brands")
def brands():
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT pb.id, pb.brand, pb.priority, pb.created_at, pb.updated_at,
               COUNT(pc.id) AS phone_count
        FROM phone_brands pb
        LEFT JOIN phone_catalogue pc ON pc.brand = pb.brand
        GROUP BY pb.id, pb.brand, pb.priority, pb.created_at, pb.updated_at
        ORDER BY pb.brand
    """)
    rows = cur.fetchall() or []
    cur.close(); conn.close()
    return render_template("portal/admin_brands.html", brands=rows)


@portal_admin_bp.route("/brands/add", methods=["POST"])
def brands_add():
    r = _require_admin()
    if r: return r

    brand    = (request.form.get("brand") or "").strip()
    priority = (request.form.get("priority") or "Medium").strip()

    if not brand:
        flash("Brand name is required.", "danger")
        return redirect(url_for("portal_admin.brands"))

    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO phone_brands (brand, priority) VALUES (%s, %s) ON CONFLICT (brand) DO NOTHING",
            (brand, priority),
        )
        conn.commit()
        flash(f"Brand '{brand}' added.", "success")
    except Exception as e:
        flash(f"Error: {e}", "danger")
    finally:
        cur.close(); conn.close()

    return redirect(url_for("portal_admin.brands"))


@portal_admin_bp.route("/brands/<int:brand_id>/edit", methods=["GET", "POST"])
def brands_edit(brand_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        brand    = (request.form.get("brand") or "").strip()
        priority = (request.form.get("priority") or "Medium").strip()
        cur2 = conn.cursor()
        cur2.execute(
            "UPDATE phone_brands SET brand=%s, priority=%s, updated_at=NOW() WHERE id=%s",
            (brand, priority, brand_id),
        )
        conn.commit()
        cur2.close(); cur.close(); conn.close()
        flash("Brand updated.", "success")
        return redirect(url_for("portal_admin.brands"))

    cur.execute("SELECT * FROM phone_brands WHERE id=%s", (brand_id,))
    brand_row = cur.fetchone()
    cur.close(); conn.close()

    if not brand_row:
        flash("Brand not found.", "danger")
        return redirect(url_for("portal_admin.brands"))

    return render_template("portal/admin_brands.html", edit_brand=brand_row, brands=[])


@portal_admin_bp.route("/brands/<int:brand_id>/delete", methods=["POST"])
def brands_delete(brand_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM phone_brands WHERE id=%s", (brand_id,))
    conn.commit()
    cur.close(); conn.close()
    flash("Brand deleted.", "success")
    return redirect(url_for("portal_admin.brands"))


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-CATEGORY CATALOGUE
# ══════════════════════════════════════════════════════════════════════════════

import re as _re
import json as _json_cat

def _slugify(text: str) -> str:
    return _re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _cat_get(cur, category_id: int):
    cur.execute("SELECT * FROM catalogue_categories WHERE id=%s", (category_id,))
    return cur.fetchone()


def _cat_attrs(cur, category_id: int):
    cur.execute(
        "SELECT * FROM catalogue_attribute_definitions "
        "WHERE category_id=%s ORDER BY sort_order, id",
        (category_id,)
    )
    return cur.fetchall()


# ── Dashboard ─────────────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue")
def catalogue_dashboard():
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # New dynamic categories with product counts
    cur.execute("""
        SELECT c.*, COUNT(p.id) AS product_count
        FROM catalogue_categories c
        LEFT JOIN catalogue_products p ON p.category_id = c.id
        GROUP BY c.id
        ORDER BY c.sort_order, c.name
    """)
    categories = cur.fetchall()

    # Legacy phones count
    cur.execute("SELECT COUNT(*) AS n FROM phone_catalogue")
    phones_count = (cur.fetchone() or {}).get("n", 0)

    # Recent uploads (last 10)
    cur.execute("""
        SELECT u.*, c.name AS category_name
        FROM catalogue_uploads u
        LEFT JOIN catalogue_categories c ON c.id = u.category_id
        ORDER BY u.created_at DESC LIMIT 10
    """)
    recent_uploads = cur.fetchall()

    cur.close(); conn.close()
    return render_template(
        "portal/admin_catalogue_dashboard.html",
        categories=categories,
        phones_count=phones_count,
        recent_uploads=recent_uploads,
    )


# ── Create category ───────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/new", methods=["GET", "POST"])
def catalogue_category_new():
    r = _require_admin()
    if r: return r

    ICONS = ["📱","💻","🖥","🖵","🔌","📷","🎮","📺","🎧","⌨️","🖱","🔋","📡","🖨","⌚","📻"]

    if request.method == "GET":
        return render_template("portal/admin_category_new.html", icons=ICONS)

    name = (request.form.get("name") or "").strip()
    icon = (request.form.get("icon") or "📦").strip()
    description = (request.form.get("description") or "").strip()

    if not name:
        flash("Category name is required.", "danger")
        return render_template("portal/admin_category_new.html", icons=ICONS)

    slug = _slugify(name)
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """INSERT INTO catalogue_categories (name, slug, icon, description, created_by)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (name, slug, icon, description or None, _admin_user())
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        insert_audit_log(action="catalogue_category_create", admin_username=_admin_user(),
                         details={"name": name, "slug": slug})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash(f"A category with the slug '{slug}' already exists.", "danger")
        cur.close(); conn.close()
        return render_template("portal/admin_category_new.html", icons=ICONS)
    finally:
        cur.close(); conn.close()

    flash(f"Category '{name}' created. Now define its attributes.", "success")
    return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=new_id))


# ── Edit category ─────────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/edit", methods=["GET", "POST"])
def catalogue_category_edit(category_id: int):
    r = _require_admin()
    if r: return r

    ICONS = ["📱","💻","🖥","🖵","🔌","📷","🎮","📺","🎧","⌨️","🖱","🔋","📡","🖨","⌚","📻"]
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cat  = _cat_get(cur, category_id)
    if not cat:
        cur.close(); conn.close()
        flash("Category not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        icon = (request.form.get("icon") or cat["icon"]).strip()
        description = (request.form.get("description") or "").strip()
        if not name:
            flash("Name is required.", "danger")
        else:
            try:
                cur.execute(
                    "UPDATE catalogue_categories SET name=%s, icon=%s, description=%s WHERE id=%s",
                    (name, icon, description or None, category_id)
                )
                conn.commit()
                insert_audit_log(action="catalogue_category_edit", admin_username=_admin_user(),
                                 details={"id": category_id, "name": name})
                flash("Category updated.", "success")
            except Exception as e:
                conn.rollback()
                flash(f"Error: {e}", "danger")
        cur.close(); conn.close()
        return redirect(url_for("portal_admin.catalogue_category_products", category_id=category_id))

    cur.close(); conn.close()
    return render_template("portal/admin_category_new.html", icons=ICONS, cat=cat)


# ── Toggle category active ────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/toggle", methods=["POST"])
def catalogue_category_toggle(category_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE catalogue_categories SET is_active = NOT is_active WHERE id=%s",
        (category_id,)
    )
    conn.commit()
    cur.close(); conn.close()
    return redirect(url_for("portal_admin.catalogue_dashboard"))


# ── Delete category ───────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/delete", methods=["POST"])
def catalogue_category_delete(category_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) AS n FROM catalogue_products WHERE category_id=%s", (category_id,))
    count = (cur.fetchone() or {}).get("n", 0)
    if count > 0:
        cur.close(); conn.close()
        flash(f"Cannot delete: category has {count} products. Deactivate it instead.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    cur.execute("DELETE FROM catalogue_categories WHERE id=%s", (category_id,))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="catalogue_category_delete", admin_username=_admin_user(),
                     details={"id": category_id})
    flash("Category deleted.", "success")
    return redirect(url_for("portal_admin.catalogue_dashboard"))


# ── Manage attributes ─────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/attributes", methods=["GET", "POST"])
def catalogue_category_attributes(category_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cat  = _cat_get(cur, category_id)
    if not cat:
        cur.close(); conn.close()
        flash("Category not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "add":
            label = (request.form.get("attribute_label") or "").strip()
            key   = _slugify(label).replace("-", "_") if label else ""
            dtype = request.form.get("data_type", "text")
            unit  = (request.form.get("unit") or "").strip() or None
            filterable = request.form.get("is_filterable") == "1"
            required   = request.form.get("is_required") == "1"
            if not label:
                flash("Attribute label is required.", "danger")
            else:
                # Get next sort order
                cur.execute(
                    "SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM catalogue_attribute_definitions WHERE category_id=%s",
                    (category_id,)
                )
                sort_order = (cur.fetchone() or {}).get("n", 1)
                try:
                    cur.execute(
                        """INSERT INTO catalogue_attribute_definitions
                           (category_id, attribute_key, attribute_label, data_type, unit,
                            is_filterable, is_required, sort_order)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (category_id, key, label, dtype, unit, filterable, required, sort_order)
                    )
                    conn.commit()
                    flash(f"Attribute '{label}' added.", "success")
                except psycopg2.errors.UniqueViolation:
                    conn.rollback()
                    flash(f"Attribute key '{key}' already exists in this category.", "danger")

        elif action == "delete":
            attr_id = request.form.get("attr_id")
            if attr_id:
                cur.execute("DELETE FROM catalogue_attribute_definitions WHERE id=%s AND category_id=%s",
                            (int(attr_id), category_id))
                conn.commit()
                flash("Attribute removed.", "success")

        elif action == "toggle_required":
            attr_id = request.form.get("attr_id")
            if attr_id:
                cur.execute(
                    "UPDATE catalogue_attribute_definitions SET is_required = NOT is_required WHERE id=%s AND category_id=%s",
                    (int(attr_id), category_id)
                )
                conn.commit()

        elif action == "toggle_filterable":
            attr_id = request.form.get("attr_id")
            if attr_id:
                cur.execute(
                    "UPDATE catalogue_attribute_definitions SET is_filterable = NOT is_filterable WHERE id=%s AND category_id=%s",
                    (int(attr_id), category_id)
                )
                conn.commit()

        cur.close(); conn.close()
        return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=category_id))

    attrs = _cat_attrs(cur, category_id)
    cur.close(); conn.close()
    return render_template(
        "portal/admin_category_attributes.html",
        cat=cat, attrs=attrs,
        DATA_TYPES=["text", "number", "select", "boolean"],
    )


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCTS FOR A CATEGORY
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/products")
def catalogue_category_products(category_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cat  = _cat_get(cur, category_id)
    if not cat:
        cur.close(); conn.close()
        flash("Category not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    attrs = _cat_attrs(cur, category_id)

    # Filters
    q         = (request.args.get("q") or "").strip()
    brand_f   = (request.args.get("brand") or "").strip()
    status_f  = request.args.get("is_active", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    per_page  = 50

    where  = ["p.category_id = %s"]
    params = [category_id]

    if q:
        where.append("(p.brand ILIKE %s OR p.model_name ILIKE %s OR p.model_number ILIKE %s)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if brand_f:
        where.append("p.brand = %s")
        params.append(brand_f)
    if status_f == "true":
        where.append("p.is_active = TRUE")
    elif status_f == "false":
        where.append("p.is_active = FALSE")

    where_sql = "WHERE " + " AND ".join(where)

    cur.execute(f"SELECT COUNT(*) AS n FROM catalogue_products p {where_sql}", params)
    total = (cur.fetchone() or {}).get("n", 0)
    pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page

    cur.execute(
        f"SELECT p.* FROM catalogue_products p {where_sql} "
        f"ORDER BY p.brand, p.model_name LIMIT %s OFFSET %s",
        params + [per_page, offset]
    )
    products = cur.fetchall()

    # For each product load its attribute values
    if products:
        pids = [p["id"] for p in products]
        cur.execute(
            """SELECT pa.product_id, ad.attribute_key, pa.value
               FROM catalogue_product_attributes pa
               JOIN catalogue_attribute_definitions ad ON ad.id = pa.attribute_def_id
               WHERE pa.product_id = ANY(%s)""",
            (pids,)
        )
        attr_map: dict = {}
        for row in cur.fetchall():
            attr_map.setdefault(row["product_id"], {})[row["attribute_key"]] = row["value"]
        products = [dict(p, attrs=attr_map.get(p["id"], {})) for p in products]

    # Brand list for filter dropdown
    cur.execute(
        "SELECT DISTINCT brand FROM catalogue_products WHERE category_id=%s AND brand IS NOT NULL ORDER BY brand",
        (category_id,)
    )
    brands = [r["brand"] for r in cur.fetchall()]

    cur.close(); conn.close()
    return render_template(
        "portal/admin_category_products.html",
        cat=cat, attrs=attrs, products=products, brands=brands,
        total=total, page=page, pages=pages, per_page=per_page,
        q=q, brand_f=brand_f, status_f=status_f,
    )


# ── Edit a single product ─────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/products/<int:product_id>/edit",
                        methods=["GET", "POST"])
def catalogue_product_edit(category_id: int, product_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cat  = _cat_get(cur, category_id)
    if not cat:
        cur.close(); conn.close()
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    cur.execute("SELECT * FROM catalogue_products WHERE id=%s AND category_id=%s",
                (product_id, category_id))
    product = cur.fetchone()
    if not product:
        cur.close(); conn.close()
        flash("Product not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_category_products", category_id=category_id))

    attrs = _cat_attrs(cur, category_id)

    # Load existing attribute values
    cur.execute(
        """SELECT ad.attribute_key, pa.value
           FROM catalogue_attribute_definitions ad
           LEFT JOIN catalogue_product_attributes pa
             ON pa.attribute_def_id = ad.id AND pa.product_id = %s
           WHERE ad.category_id = %s ORDER BY ad.sort_order""",
        (product_id, category_id)
    )
    attr_vals = {r["attribute_key"]: r["value"] for r in cur.fetchall()}

    if request.method == "POST":
        brand        = (request.form.get("brand") or "").strip() or None
        model_name   = (request.form.get("model_name") or "").strip()
        model_number = (request.form.get("model_number") or "").strip() or None
        sku          = (request.form.get("sku") or "").strip() or None
        description  = (request.form.get("description") or "").strip() or None
        image_url    = (request.form.get("image_url") or "").strip() or None
        is_active    = request.form.get("is_active") == "1"

        if not model_name:
            flash("Model name is required.", "danger")
            cur.close(); conn.close()
            # Re-render the form with the user's input preserved
            return render_template(
                "portal/admin_category_product_edit.html",
                cat=cat, product=product, attrs=attrs,
                attr_vals={a["attribute_key"]: request.form.get(f"attr_{a['attribute_key']}") for a in attrs},
            )

        try:
            cur.execute(
                """UPDATE catalogue_products
                   SET brand=%s, model_name=%s, model_number=%s, sku=%s,
                       description=%s, image_url=%s, is_active=%s, updated_at=NOW()
                   WHERE id=%s""",
                (brand, model_name, model_number, sku, description, image_url, is_active, product_id)
            )
            # Upsert attribute values
            for ad in attrs:
                val = (request.form.get(f"attr_{ad['attribute_key']}") or "").strip() or None
                if val is not None:
                    cur.execute(
                        """INSERT INTO catalogue_product_attributes (product_id, attribute_def_id, value)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (product_id, attribute_def_id) DO UPDATE SET value=EXCLUDED.value""",
                        (product_id, ad["id"], val)
                    )
                else:
                    cur.execute(
                        "DELETE FROM catalogue_product_attributes WHERE product_id=%s AND attribute_def_id=%s",
                        (product_id, ad["id"])
                    )
            conn.commit()
            insert_audit_log(action="catalogue_product_edit", admin_username=_admin_user(),
                             details={"product_id": product_id, "model_name": model_name})
            flash("Product updated.", "success")
        except Exception as e:
            conn.rollback()
            flash(f"Error: {e}", "danger")

        cur.close(); conn.close()
        return redirect(url_for("portal_admin.catalogue_category_products", category_id=category_id))

    cur.close(); conn.close()
    return render_template(
        "portal/admin_category_product_edit.html",
        cat=cat, product=product, attrs=attrs, attr_vals=attr_vals,
    )


# ── Delete a product ──────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/products/<int:product_id>/delete",
                        methods=["POST"])
def catalogue_product_delete(category_id: int, product_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM catalogue_products WHERE id=%s AND category_id=%s",
                (product_id, category_id))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="catalogue_product_delete", admin_username=_admin_user(),
                     details={"product_id": product_id, "category_id": category_id})
    flash("Product deleted.", "success")
    return redirect(url_for("portal_admin.catalogue_category_products", category_id=category_id))


# ── Bulk actions ──────────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/products/bulk", methods=["POST"])
def catalogue_products_bulk(category_id: int):
    r = _require_admin()
    if r: return r

    ids_raw = request.form.getlist("ids")
    action  = request.form.get("bulk_action", "")
    ids     = [int(x) for x in ids_raw if x.isdigit()]

    if not ids:
        flash("No products selected.", "warning")
        return redirect(url_for("portal_admin.catalogue_category_products", category_id=category_id))

    conn = get_db_connection()
    cur  = conn.cursor()
    if action == "activate":
        cur.execute("UPDATE catalogue_products SET is_active=TRUE WHERE id=ANY(%s) AND category_id=%s",
                    (ids, category_id))
    elif action == "deactivate":
        cur.execute("UPDATE catalogue_products SET is_active=FALSE WHERE id=ANY(%s) AND category_id=%s",
                    (ids, category_id))
    elif action == "delete":
        cur.execute("DELETE FROM catalogue_products WHERE id=ANY(%s) AND category_id=%s",
                    (ids, category_id))
    conn.commit()
    cur.close(); conn.close()
    flash(f"Bulk {action} applied to {len(ids)} products.", "success")
    return redirect(url_for("portal_admin.catalogue_category_products", category_id=category_id))


# ── Download CSV template ─────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/template.csv")
def catalogue_category_template(category_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cat  = _cat_get(cur, category_id)
    if not cat:
        cur.close(); conn.close()
        flash("Category not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    attrs = _cat_attrs(cur, category_id)
    cur.close(); conn.close()

    base_cols = ["brand", "model_name", "model_number", "sku", "description", "image_url"]
    attr_cols = [a["attribute_key"] for a in attrs]
    all_cols  = base_cols + attr_cols

    # Example row with hints
    example = {
        "brand": "Samsung",
        "model_name": "Galaxy S24 Ultra",
        "model_number": "SM-S928B",
        "sku": "SAM-S24U-256",
        "description": "Flagship phone with S Pen",
        "image_url": "https://...",
    }
    for a in attrs:
        example[a["attribute_key"]] = a["unit"] or a["data_type"]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_cols)
    writer.writeheader()
    writer.writerow(example)

    slug = cat["slug"]
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={slug}_template.csv"},
    )


# ── Export products CSV ────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/export.csv")
def catalogue_category_export(category_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cat  = _cat_get(cur, category_id)
    if not cat:
        cur.close(); conn.close()
        flash("Category not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    attrs = _cat_attrs(cur, category_id)

    cur.execute(
        "SELECT * FROM catalogue_products WHERE category_id=%s ORDER BY brand, model_name",
        (category_id,)
    )
    products = cur.fetchall()

    if products:
        pids = [p["id"] for p in products]
        cur.execute(
            """SELECT pa.product_id, ad.attribute_key, pa.value
               FROM catalogue_product_attributes pa
               JOIN catalogue_attribute_definitions ad ON ad.id = pa.attribute_def_id
               WHERE pa.product_id = ANY(%s)""",
            (pids,)
        )
        attr_map: dict = {}
        for row in cur.fetchall():
            attr_map.setdefault(row["product_id"], {})[row["attribute_key"]] = row["value"]
    else:
        attr_map = {}

    cur.close(); conn.close()

    base_cols = ["id", "brand", "model_name", "model_number", "sku", "is_active", "created_at"]
    attr_cols = [a["attribute_key"] for a in attrs]
    all_cols  = base_cols + attr_cols

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_cols, extrasaction="ignore")
    writer.writeheader()
    for p in products:
        row = {c: p.get(c, "") for c in base_cols}
        row.update(attr_map.get(p["id"], {}))
        writer.writerow(row)

    slug = cat["slug"]
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={slug}_export.csv"},
    )


# ── Upload CSV/XLSX ────────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/upload", methods=["GET", "POST"])
def catalogue_category_upload(category_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cat  = _cat_get(cur, category_id)
    if not cat:
        cur.close(); conn.close()
        flash("Category not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    attrs = _cat_attrs(cur, category_id)

    if request.method == "GET":
        cur.close(); conn.close()
        return render_template("portal/admin_category_upload.html", cat=cat, attrs=attrs)

    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "danger")
        cur.close(); conn.close()
        return redirect(url_for("portal_admin.catalogue_category_upload", category_id=category_id))

    on_conflict = request.form.get("on_conflict", "skip")  # "skip" or "update"
    filename    = file.filename.lower()
    inserted = updated = errors = 0
    error_details = []

    required_attrs = [a for a in attrs if a["is_required"]]
    attr_by_key    = {a["attribute_key"]: a for a in attrs}

    try:
        if filename.endswith(".csv"):
            stream    = io.StringIO(file.stream.read().decode("utf-8-sig"))
            rows_iter = list(csv.DictReader(stream))
        elif filename.endswith((".xlsx", ".xls")):
            import openpyxl
            wb      = openpyxl.load_workbook(file, read_only=True, data_only=True)
            ws      = wb.active
            headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
            rows_iter = [
                {k: v for k, v in zip(headers, row)}
                for row in ws.iter_rows(min_row=2, values_only=True)
            ]
        else:
            flash("Only .csv or .xlsx files are supported.", "danger")
            cur.close(); conn.close()
            return redirect(url_for("portal_admin.catalogue_category_upload", category_id=category_id))
    except Exception as e:
        flash(f"Could not read file: {e}", "danger")
        cur.close(); conn.close()
        return redirect(url_for("portal_admin.catalogue_category_upload", category_id=category_id))

    total_rows = len(rows_iter)

    for row_num, row in enumerate(rows_iter, start=2):
        model_name = str(row.get("model_name") or "").strip()
        if not model_name:
            errors += 1
            error_details.append({"row": row_num, "error": "missing model_name"})
            continue

        # Check required attributes
        missing = [a["attribute_key"] for a in required_attrs
                   if not str(row.get(a["attribute_key"]) or "").strip()]
        if missing:
            errors += 1
            error_details.append({"row": row_num, "error": f"missing required: {', '.join(missing)}"})
            continue

        brand        = str(row.get("brand") or "").strip() or None
        model_number = str(row.get("model_number") or "").strip() or None
        sku_val      = str(row.get("sku") or "").strip() or None
        description  = str(row.get("description") or "").strip() or None
        image_url    = str(row.get("image_url") or "").strip() or None

        try:
            cur.execute("SAVEPOINT sp_row")
            if on_conflict == "update" and sku_val:
                cur.execute(
                    """INSERT INTO catalogue_products
                       (category_id, brand, model_name, model_number, sku, description, image_url)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (sku) DO UPDATE SET
                         brand=EXCLUDED.brand, model_name=EXCLUDED.model_name,
                         model_number=EXCLUDED.model_number, description=EXCLUDED.description,
                         image_url=EXCLUDED.image_url, updated_at=NOW()
                       RETURNING id, xmax""",
                    (category_id, brand, model_name, model_number, sku_val, description, image_url)
                )
            else:
                cur.execute(
                    """INSERT INTO catalogue_products
                       (category_id, brand, model_name, model_number, sku, description, image_url)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       RETURNING id, xmax""",
                    (category_id, brand, model_name, model_number, sku_val, description, image_url)
                )

            result = cur.fetchone()
            if not result:
                errors += 1
                cur.execute("ROLLBACK TO SAVEPOINT sp_row")
                continue

            product_id  = result["id"]
            was_updated = result["xmax"] != 0  # xmax!=0 means UPDATE happened

            if was_updated:
                updated += 1
            else:
                inserted += 1

            # Upsert attribute values
            for key, ad in attr_by_key.items():
                val = str(row.get(key) or "").strip() or None
                if val is not None:
                    cur.execute(
                        """INSERT INTO catalogue_product_attributes (product_id, attribute_def_id, value)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (product_id, attribute_def_id) DO UPDATE SET value=EXCLUDED.value""",
                        (product_id, ad["id"], val)
                    )

        except Exception as e:
            errors += 1
            error_details.append({"row": row_num, "error": str(e)})
            cur.execute("ROLLBACK TO SAVEPOINT sp_row")
            continue

        cur.execute("RELEASE SAVEPOINT sp_row")

    try:
        conn.commit()
    except Exception as e:
        conn.rollback()
        flash(f"Commit failed: {e}", "danger")
        cur.close(); conn.close()
        return redirect(url_for("portal_admin.catalogue_category_upload", category_id=category_id))

    # Log the upload
    cur.execute(
        """INSERT INTO catalogue_uploads
           (admin_username, category_id, filename, total_rows, successful, failed, status, error_details)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            _admin_user(), category_id, file.filename,
            total_rows, inserted + updated, errors,
            "partial" if errors > 0 else "completed",
            _json_cat.dumps(error_details),
        )
    )
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(action="catalogue_upload", admin_username=_admin_user(),
                     details={"category_id": category_id, "filename": file.filename,
                               "inserted": inserted, "updated": updated, "errors": errors})

    msg = f"Import complete — {inserted} added, {updated} updated, {errors} skipped."
    flash(msg, "success" if errors == 0 else "warning")
    return redirect(url_for("portal_admin.catalogue_category_products", category_id=category_id))


# ══════════════════════════════════════════════════════════════════════════════
# PLANS MANAGEMENT — admin view/assign plans to tenants
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/plans")
def admin_plans():
    _require_admin()
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM plans ORDER BY sort_order")
    plans = cur.fetchall() or []

    cur.execute("""
        SELECT t.id AS tenant_id, t.name AS business_name,
               t.billing_cycle, t.plan_period_start,
               COALESCE(p.slug, 'free') AS plan_slug,
               COALESCE(p.name, 'Free') AS plan_name,
               (SELECT COUNT(*) FROM usage_events ue
                WHERE ue.tenant_id=t.id AND ue.created_at >= COALESCE(t.plan_period_start, CURRENT_DATE - 30))
                    AS msgs_used,
               COALESCE(p.ai_messages_limit, 100) AS msgs_limit
        FROM tenants t
        LEFT JOIN plans p ON p.id = t.plan_id
        WHERE t.status != 'cancelled'
        ORDER BY t.name
    """)
    tenants = cur.fetchall() or []
    cur.close(); conn.close()

    return render_template("portal/admin_plans.html",
                           plans=plans, tenants=tenants)


@portal_admin_bp.route("/plans/assign/<int:tenant_id>", methods=["POST"])
def admin_plans_assign(tenant_id: int):
    _require_admin()
    plan_id       = int(request.form.get("plan_id") or 1)
    billing_cycle = request.form.get("billing_cycle", "monthly")
    from datetime import date as _d
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE tenants
        SET plan_id=%s, billing_cycle=%s, plan_period_start=%s, quota_notified_at=NULL
        WHERE id=%s
    """, (plan_id, billing_cycle, _d.today(), tenant_id))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="plan_assign", admin_username=_admin_user(),
                     details={"tenant_id": tenant_id, "plan_id": plan_id, "billing_cycle": billing_cycle})
    flash(f"Plan updated for tenant #{tenant_id}.", "success")
    # Redirect back to wherever the form was submitted from
    referrer = request.referrer or ""
    if "customers" in referrer:
        # Find the customer id for this tenant and go back to their detail page
        try:
            conn2 = get_db_connection()
            cur2  = conn2.cursor()
            cur2.execute("SELECT id FROM customers WHERE tenant_id=%s AND is_active=TRUE ORDER BY id LIMIT 1", (tenant_id,))
            row = cur2.fetchone()
            cur2.close(); conn2.close()
            if row:
                return redirect(url_for("portal_admin.customer_detail", customer_id=row[0]))
        except Exception:
            pass
    return redirect(url_for("portal_admin.admin_plans"))


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP DIAGNOSTICS — admin troubleshooting tool
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/wa-diagnostics")
def wa_diagnostics():
    _require_admin()
    from datetime import datetime as _dt, timezone as _tz

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT
            wt.id,
            wt.tenant_id,
            wt.phone_number_id,
            wt.display_phone_number,
            wt.verified_name,
            wt.waba_id,
            wt.active,
            wt.token_expires_at,
            wt.signup_method,
            wt.created_at,
            t.name          AS business_name,
            t.status        AS tenant_status,
            MAX(wml.created_at) FILTER (WHERE wml.direction = 'inbound')   AS last_inbound_at,
            MAX(wml.created_at)                                             AS last_any_at,
            COUNT(wml.id)   FILTER (WHERE wml.created_at >= NOW() - INTERVAL '24 hours') AS msgs_24h,
            COUNT(wml.id)   FILTER (WHERE wml.created_at >= NOW() - INTERVAL '7 days')   AS msgs_7d,
            COUNT(DISTINCT wml.customer_phone)
                            FILTER (WHERE wml.created_at >= NOW() - INTERVAL '7 days')   AS customers_7d
        FROM wa_tenants wt
        JOIN tenants t ON t.id = wt.tenant_id
        LEFT JOIN wa_message_log wml ON wml.tenant_id = wt.tenant_id
        GROUP BY wt.id, wt.tenant_id, wt.phone_number_id, wt.display_phone_number,
                 wt.verified_name, wt.waba_id, wt.active, wt.token_expires_at,
                 wt.signup_method, wt.created_at, t.name, t.status
        ORDER BY t.name
    """)
    rows = cur.fetchall() or []
    cur.close(); conn.close()

    now = _dt.now(_tz.utc)

    tenants = []
    for r in rows:
        d = dict(r)

        # Token status
        exp = d.get("token_expires_at")
        if exp is None:
            d["token_status"] = "permanent"
            d["token_label"]  = "Permanent"
        elif exp < now:
            d["token_status"] = "expired"
            d["token_label"]  = f"Expired {exp.strftime('%-d %b %Y')}"
        elif (exp - now).days < 7:
            d["token_status"] = "expiring"
            d["token_label"]  = f"Expires {exp.strftime('%-d %b %Y')}"
        else:
            d["token_status"] = "ok"
            d["token_label"]  = f"OK until {exp.strftime('%-d %b %Y')}"

        # Webhook health (based on last inbound message)
        last = d.get("last_inbound_at")
        if last is None:
            d["webhook_status"] = "none"
            d["webhook_label"]  = "No messages yet"
        else:
            age_h = (now - last).total_seconds() / 3600
            if age_h < 1:
                d["webhook_status"] = "active"
                d["webhook_label"]  = "Active (< 1h ago)"
            elif age_h < 24:
                d["webhook_status"] = "recent"
                d["webhook_label"]  = f"Recent ({int(age_h)}h ago)"
            elif age_h < 168:
                d["webhook_status"] = "quiet"
                d["webhook_label"]  = f"Quiet ({int(age_h/24)}d ago)"
            else:
                d["webhook_status"] = "stale"
                d["webhook_label"]  = f"Stale ({int(age_h/24)}d ago)"

        tenants.append(d)

    return render_template("portal/admin_wa_diagnostics.html", tenants=tenants)


@portal_admin_bp.route("/merchant-signup-qr")
def merchant_signup_qr():
    r = _require_admin()
    if r: return r
    import qrcode
    from flask import send_file
    signup_url = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com") + "/register"
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(signup_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    as_dl = request.args.get("download") == "1"
    return send_file(buf, mimetype="image/png",
                     as_attachment=as_dl,
                     download_name="phixtra-merchant-signup-qr.png")


# ── Ambassador Approvals ───────────────────────────────────────────────────

@portal_admin_bp.route("/ambassadors", methods=["GET"])
def ambassadors():
    r = _require_admin()
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT a.*,
               (SELECT COUNT(*) FROM tenants t WHERE t.ref_code = a.ref_code AND t.status='active') AS active_clients,
               (SELECT COALESCE(SUM(commission_amount),0) FROM ambassador_commissions ac WHERE ac.ambassador_id = a.id) AS total_earned
        FROM ambassadors a
        ORDER BY CASE a.status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, a.created_at DESC
    """)
    ambs = cur.fetchall() or []
    cur.close(); conn.close()
    return render_template("portal/admin_ambassadors.html", ambassadors=ambs)


@portal_admin_bp.route("/ambassadors/report", methods=["GET"])
def ambassador_report():
    r = _require_admin()
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT a.id, a.first_name, a.last_name, a.email, a.status,
               (SELECT COUNT(*) FROM tenants t WHERE t.ref_code = a.ref_code AND t.status='active') AS active_clients,
               (SELECT COALESCE(SUM(commission_amount),0) FROM ambassador_commissions ac WHERE ac.ambassador_id = a.id) AS total_earned
        FROM ambassadors a
        ORDER BY CASE a.status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, a.last_name, a.first_name
    """)
    ambs = cur.fetchall() or []
    cur.close(); conn.close()
    counts = {
        "all":       len(ambs),
        "active":    sum(1 for a in ambs if a["status"] == "active"),
        "pending":   sum(1 for a in ambs if a["status"] == "pending"),
        "suspended": sum(1 for a in ambs if a["status"] == "suspended"),
        "rejected":  sum(1 for a in ambs if a["status"] == "rejected"),
    }
    return render_template("portal/admin_ambassador_report.html", ambassadors=ambs, counts=counts)


@portal_admin_bp.route("/ambassadors/report/download", methods=["POST"])
def ambassador_report_download():
    r = _require_admin()
    if r: return r

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from datetime import date as _date

    amb_ids_raw = request.form.getlist("amb_ids")
    if not amb_ids_raw:
        flash("No ambassadors selected.", "warning")
        return redirect(url_for("portal_admin.ambassador_report"))
    try:
        amb_ids = [int(x) for x in amb_ids_raw]
    except ValueError:
        flash("Invalid selection.", "danger")
        return redirect(url_for("portal_admin.ambassador_report"))

    include_personal  = "col_personal"  in request.form
    include_contact   = "col_contact"   in request.form
    include_location  = "col_location"  in request.form
    include_bank      = "col_bank"      in request.form
    include_id_info   = "col_id"        in request.form
    include_earnings  = "col_earnings"  in request.form
    include_referrals = "col_referrals" in request.form

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    placeholders = ",".join(["%s"] * len(amb_ids))
    cur.execute(f"""
        SELECT a.*,
               (SELECT COUNT(*) FROM tenants t WHERE t.ref_code = a.ref_code AND t.status='active') AS active_clients,
               (SELECT COUNT(*) FROM tenants t WHERE t.ref_code = a.ref_code) AS total_clients,
               (SELECT COALESCE(SUM(commission_amount),0)
                FROM ambassador_commissions ac WHERE ac.ambassador_id = a.id) AS total_earned,
               (SELECT COUNT(*)
                FROM ambassador_commissions ac WHERE ac.ambassador_id = a.id) AS commission_count
        FROM ambassadors a
        WHERE a.id IN ({placeholders})
        ORDER BY a.last_name, a.first_name
    """, amb_ids)
    ambs = cur.fetchall() or []
    cur.close(); conn.close()

    # ── Build column list ───────────────────────────────────────────────────
    headers = ["#", "Full Name", "Status", "Ref Code", "Joined"]
    if include_personal:  headers += ["Date of Birth", "Gender", "Nationality", "Qualification"]
    if include_contact:   headers += ["Email", "Phone", "WhatsApp"]
    if include_location:  headers += ["Address", "Operating Location"]
    if include_bank:      headers += ["Bank Name", "Account Number", "Account Name", "Sort Code", "SWIFT Code"]
    if include_id_info:   headers += ["ID Document Type"]
    if include_earnings:  headers += ["Total Earned (NGN)", "Commission Count"]
    if include_referrals: headers += ["Active Clients", "Total Clients"]

    # ── Workbook ────────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ambassadors"

    ink_fill    = PatternFill("solid", fgColor="030C18")
    ink_font    = Font(color="FFFFFF", bold=True, size=11, name="Calibri")
    thin_side   = Side(style="thin", color="D1D5DB")
    cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    STATUS_FILL = {
        "active":    PatternFill("solid", fgColor="D1FAE5"),
        "pending":   PatternFill("solid", fgColor="FEF9C3"),
        "suspended": PatternFill("solid", fgColor="FEE2E2"),
        "rejected":  PatternFill("solid", fgColor="F3F4F6"),
    }

    # Title row
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    tc = ws.cell(row=1, column=1, value="PhiXtra AI — Ambassador Report")
    tc.font      = Font(bold=True, size=14, color="030C18", name="Calibri")
    tc.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    # Subtitle row
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
    sc = ws.cell(row=2, column=1,
                 value=f"Generated: {_date.today().strftime('%d %B %Y')}  |  {len(ambs)} ambassador(s) selected")
    sc.font      = Font(size=10, color="6B7280", name="Calibri")
    sc.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 18

    # Header row
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=3, column=col, value=h)
        c.fill      = ink_fill
        c.font      = ink_font
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border    = cell_border
    ws.row_dimensions[3].height = 30

    # Data rows
    for ridx, amb in enumerate(ambs, 4):
        status = (amb.get("status") or "").lower()
        sfill  = STATUS_FILL.get(status)
        col    = [1]

        def wc(val, fmt=None, center=False):
            c = ws.cell(row=ridx, column=col[0], value=val)
            c.border    = cell_border
            c.alignment = Alignment(vertical="center",
                                    horizontal="center" if center else "left")
            if fmt:      c.number_format = fmt
            if sfill and col[0] == 3: c.fill = sfill   # status cell only
            col[0] += 1
            return c

        wc(ridx - 3, center=True)
        wc(f"{amb.get('first_name','')} {amb.get('last_name','')}".strip())
        wc((status or "").title(), center=True)
        wc(amb.get("ref_code") or "", center=True)
        joined = amb.get("created_at")
        wc(joined.strftime("%d %b %Y") if joined else "", center=True)

        if include_personal:
            dob = amb.get("date_of_birth")
            wc(dob.strftime("%d %b %Y") if dob else "", center=True)
            wc(amb.get("gender") or "")
            wc(amb.get("nationality") or "")
            wc(amb.get("highest_qualification") or "")
        if include_contact:
            wc(amb.get("email") or "")
            wc(amb.get("phone") or "")
            wc(amb.get("whatsapp_number") or "")
        if include_location:
            a_cell = wc(amb.get("address") or "")
            a_cell.alignment = Alignment(vertical="center", wrap_text=True)
            wc(amb.get("location") or "")
        if include_bank:
            wc(amb.get("bank_name") or "")
            wc(amb.get("account_number") or "")
            wc(amb.get("account_name") or "")
            wc(amb.get("sort_code") or "")
            wc(amb.get("swift_code") or "")
        if include_id_info:
            wc(amb.get("id_document_type") or "")
        if include_earnings:
            wc(float(amb.get("total_earned") or 0), fmt='#,##0.00', center=True)
            wc(int(amb.get("commission_count") or 0), center=True)
        if include_referrals:
            wc(int(amb.get("active_clients") or 0), center=True)
            wc(int(amb.get("total_clients") or 0), center=True)

        ws.row_dimensions[ridx].height = 20

    # Column widths
    col_widths = [5, 24, 12, 12, 12]
    if include_personal:  col_widths += [14, 10, 14, 22]
    if include_contact:   col_widths += [26, 16, 16]
    if include_location:  col_widths += [32, 20]
    if include_bank:      col_widths += [20, 16, 22, 12, 12]
    if include_id_info:   col_widths += [20]
    if include_earnings:  col_widths += [18, 16]
    if include_referrals: col_widths += [14, 12]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    ws.freeze_panes = "A4"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"phixtra-ambassadors-{_date.today().strftime('%Y%m%d')}.xlsx"
    return send_file(buf,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=fname)


@portal_admin_bp.route("/ambassadors/<int:amb_id>/approve", methods=["POST"])
def ambassador_approve(amb_id: int):
    r = _require_admin()
    if r: return r
    from datetime import date as _date
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM ambassadors WHERE id=%s", (amb_id,))
    amb = cur.fetchone()
    if amb:
        cur2 = conn.cursor()
        cur2.execute("""
            UPDATE ambassadors
               SET status='active', approved_at=NOW(), approved_by=%s, partnership_start=%s
             WHERE id=%s
        """, (_admin_user(), _date.today(), amb_id))
        conn.commit()
        cur2.close()
        # Also upsert into sales_reps so they appear in the existing QR system
        try:
            cur3 = conn.cursor()
            cur3.execute("""
                INSERT INTO sales_reps (name, ref_code, email, active)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (ref_code) DO UPDATE SET active=TRUE
            """, (f"{amb['first_name']} {amb['last_name']}", amb['ref_code'], amb['email']))
            conn.commit()
            cur3.close()
        except Exception as _e:
            conn.rollback()
            print("⚠️ ambassador_approve sales_reps sync error:", _e)
        # Create demo portal tenant for this ambassador
        try:
            from ambassador_demo import create_ambassador_demo
            create_ambassador_demo(amb['id'], amb['first_name'], amb['ref_code'])
        except Exception as _e:
            print("⚠️ ambassador approval demo tenant creation failed:", _e)
        # Send approval email
        try:
            from ambassador_routes import _send_approved_email
            _send_approved_email(amb['first_name'], amb['email'], amb['ref_code'])
        except Exception as _e:
            print("⚠️ ambassador approval email failed:", _e)
        flash(f"{amb['first_name']} {amb['last_name']} approved as ambassador.", "success")
    cur.close(); conn.close()
    return redirect(url_for("portal_admin.ambassadors"))


@portal_admin_bp.route("/ambassadors/<int:amb_id>/suspend", methods=["POST"])
def ambassador_suspend(amb_id: int):
    r = _require_admin()
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE ambassadors SET status='suspended' WHERE id=%s", (amb_id,))
    conn.commit()
    cur.close(); conn.close()
    flash("Ambassador suspended.", "warning")
    return redirect(url_for("portal_admin.ambassadors"))


@portal_admin_bp.route("/ambassadors/<int:amb_id>/reactivate", methods=["POST"])
def ambassador_reactivate(amb_id: int):
    r = _require_admin()
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE ambassadors SET status='active' WHERE id=%s", (amb_id,))
    conn.commit()
    cur.close(); conn.close()
    flash("Ambassador reactivated.", "success")
    return redirect(url_for("portal_admin.ambassadors"))


@portal_admin_bp.route("/ambassadors/<int:amb_id>/reject", methods=["POST"])
def ambassador_reject(amb_id: int):
    r = _require_admin()
    if r: return r
    reason = (request.form.get("reason") or "").strip() or None
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE ambassadors SET status='rejected', rejected_at=NOW(), rejected_reason=%s WHERE id=%s",
                (reason, amb_id))
    conn.commit()
    cur.close(); conn.close()
    flash("Ambassador application rejected.", "warning")
    return redirect(url_for("portal_admin.ambassadors"))


@portal_admin_bp.route("/ambassadors/<int:amb_id>/terminate", methods=["POST"])
def ambassador_terminate(amb_id: int):
    r = _require_admin()
    if r: return r
    reason = (request.form.get("reason") or "").strip() or None
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE ambassadors SET status='terminated', terminated_at=NOW(), terminated_reason=%s WHERE id=%s",
                (reason, amb_id))
    conn.commit()
    cur.close(); conn.close()
    flash("Ambassador terminated.", "danger")
    return redirect(url_for("portal_admin.ambassadors"))


@portal_admin_bp.route("/ambassadors/<int:amb_id>/edit", methods=["POST"])
def ambassador_edit(amb_id: int):
    r = _require_admin()
    if r: return r
    f = request.form
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE ambassadors SET
            first_name=%s, last_name=%s, email=%s, phone=%s, whatsapp_number=%s,
            date_of_birth=%s, gender=%s, nationality=%s, address=%s, location=%s,
            highest_qualification=%s, bank_name=%s, account_number=%s,
            account_name=%s, sort_code=%s, swift_code=%s
        WHERE id=%s
    """, (
        (f.get("first_name") or "").strip(),
        (f.get("last_name")  or "").strip(),
        (f.get("email")      or "").strip().lower(),
        (f.get("phone")      or "").strip() or None,
        (f.get("whatsapp_number") or "").strip() or None,
        (f.get("date_of_birth")   or "").strip() or None,
        (f.get("gender")     or "").strip() or None,
        (f.get("nationality") or "").strip() or None,
        (f.get("address")    or "").strip() or None,
        (f.get("location")   or "").strip() or None,
        (f.get("highest_qualification") or "").strip() or None,
        (f.get("bank_name")       or "").strip() or None,
        (f.get("account_number")  or "").strip() or None,
        (f.get("account_name")    or "").strip() or None,
        (f.get("sort_code")  or "").strip() or None,
        (f.get("swift_code") or "").strip() or None,
        amb_id,
    ))
    conn.commit()
    cur.close(); conn.close()
    flash("Ambassador details updated.", "success")
    return redirect(url_for("portal_admin.ambassadors"))


@portal_admin_bp.route("/ambassadors/<int:amb_id>/detail")
def ambassador_detail(amb_id: int):
    r = _require_admin()
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT a.*,
               (SELECT COUNT(*) FROM tenants t WHERE t.ref_code=a.ref_code AND t.status='active') AS active_clients,
               (SELECT COALESCE(SUM(commission_amount),0) FROM ambassador_commissions ac WHERE ac.ambassador_id=a.id) AS total_earned
        FROM ambassadors a WHERE a.id=%s
    """, (amb_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        from flask import jsonify
        return jsonify({"error": "Not found"}), 404
    amb = dict(row)
    for k, v in amb.items():
        if hasattr(v, 'isoformat'):
            amb[k] = v.isoformat()
        elif v is None:
            amb[k] = ""
    from flask import jsonify
    return jsonify(amb)


@portal_admin_bp.route("/ambassadors/<int:amb_id>/id-doc")
def ambassador_id_doc(amb_id: int):
    r = _require_admin()
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id_document_path FROM ambassadors WHERE id=%s", (amb_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row or not row.get("id_document_path"):
        return "No document found", 404
    import os as _os
    from flask import send_from_directory
    static_dir = _os.path.join(_os.path.dirname(__file__), "static")
    return send_from_directory(static_dir, row["id_document_path"])


# ── Sales Reps & QR Codes ──────────────────────────────────────────────────

@portal_admin_bp.route("/sales-reps", methods=["GET", "POST"])
def sales_reps():
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "create":
            name     = (request.form.get("name") or "").strip()
            ref_code = (request.form.get("ref_code") or "").strip().lower()
            ref_code = ''.join(c for c in ref_code if c.isalnum() or c in "-_")
            email    = (request.form.get("email") or "").strip().lower() or None
            if not name or not ref_code:
                flash("Name and ref code are required.", "danger")
            else:
                try:
                    cur2 = conn.cursor()
                    cur2.execute(
                        "INSERT INTO sales_reps (name, ref_code, email) VALUES (%s, %s, %s)",
                        (name, ref_code, email)
                    )
                    conn.commit()
                    cur2.close()
                    flash(f"Sales ambassador '{name}' created with code '{ref_code}'.", "success")
                except psycopg2.errors.UniqueViolation:
                    conn.rollback()
                    flash("That ref code is already taken. Choose a different one.", "danger")

        elif action == "toggle":
            rep_id = request.form.get("rep_id")
            cur2 = conn.cursor()
            cur2.execute("UPDATE sales_reps SET active = NOT active WHERE id=%s", (rep_id,))
            conn.commit()
            cur2.close()

        elif action == "delete":
            rep_id = request.form.get("rep_id")
            cur2 = conn.cursor()
            cur2.execute("DELETE FROM sales_reps WHERE id=%s", (rep_id,))
            conn.commit()
            cur2.close()
            flash("Sales ambassador deleted.", "success")

        cur.close(); conn.close()
        return redirect(url_for("portal_admin.sales_reps"))

    # Load reps with signup counts
    cur.execute("""
        SELECT s.id, s.name, s.ref_code, s.email, s.active, s.created_at,
               COUNT(t.id) AS signup_count
        FROM sales_reps s
        LEFT JOIN tenants t ON t.ref_code = s.ref_code
        GROUP BY s.id
        ORDER BY s.created_at DESC
    """)
    reps = cur.fetchall() or []
    cur.close(); conn.close()

    base_url = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")
    return render_template("portal/admin_sales_reps.html", reps=reps, base_url=base_url)


@portal_admin_bp.route("/sales-reps/<int:rep_id>/qr.png")
def sales_rep_qr(rep_id: int):
    r = _require_admin()
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT name, ref_code FROM sales_reps WHERE id=%s", (rep_id,))
    rep = cur.fetchone()
    cur.close(); conn.close()

    if not rep:
        return "Not found", 404

    base_url = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")
    url = f"{base_url}/register?ref={rep['ref_code']}"

    import qrcode
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    as_dl = request.args.get("download") == "1"
    safe_name = rep["name"].replace(" ", "-").lower()
    return send_file(buf, mimetype="image/png",
                     as_attachment=as_dl,
                     download_name=f"phixtra-qr-{safe_name}.png")


@portal_admin_bp.route("/sales-reps/<int:rep_id>/email-qr", methods=["POST"])
def sales_rep_email_qr(rep_id: int):
    r = _require_admin()
    if r: return r

    to_email = (request.form.get("to_email") or "").strip().lower()
    if not to_email:
        return {"error": "No email address provided."}, 400

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT name, ref_code FROM sales_reps WHERE id=%s", (rep_id,))
    rep = cur.fetchone()

    # Save the email address for future use
    cur2 = conn.cursor()
    cur2.execute("UPDATE sales_reps SET email=%s WHERE id=%s", (to_email, rep_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    if not rep:
        return {"error": "Sales ambassador not found."}, 404

    base_url = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")
    url      = f"{base_url}/register?ref={rep['ref_code']}"

    import qrcode
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    safe_name = rep["name"].replace(" ", "-").lower()
    BRAND = "#030C18"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px">
      <h2 style="color:{BRAND}">Your PhiXtra Ambassador QR Code</h2>
      <p>Hi {rep['name']},</p>
      <p>Here is your personal QR code. Share it with businesses — when they scan it and sign up,
         they'll be linked to you automatically.</p>
      <p><strong>Your referral link:</strong><br>
         <a href="{url}" style="color:{BRAND}">{url}</a></p>
      <p>The QR code PNG is attached to this email. You can print it, add it to slides,
         or share it digitally.</p>
      <p style="color:#888;font-size:12px">Questions? Contact support@phixtra.com</p>
    </div>"""

    ok = send_email_with_attachment(
        to_email=to_email,
        subject=f"Your PhiXtra Ambassador QR Code — {rep['name']}",
        html_body=html,
        attachment_bytes=png_bytes,
        attachment_filename=f"phixtra-qr-{safe_name}.png",
        text_body=f"Your PhiXtra referral link: {url}",
    )

    if ok:
        return {"success": True, "message": f"QR code emailed to {to_email}."}, 200
    else:
        return {"error": "Failed to send email. Check SMTP settings."}, 500
