"""
portal_admin_routes.py  — Phase 1 admin portal (/admin/*)
admin_users table and db.py are UNCHANGED. app.py (keys.phixtra.com) is UNTOUCHED.
"""
import psycopg2
import psycopg2.extras
import psycopg2.errors
import os
import uuid
import secrets
import string
import json as _json
import csv
import io
import bcrypt
from urllib.parse import urlencode
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, send_file, send_from_directory, Response,
                   current_app)

from db import get_db_connection, insert_audit_log
from feature_access import check_feature_access, PLAN_LOCK_CODE_PATHS
from meta_business_agent import sync_all_to_meta, check_eligibility, go_live_with_meta, revert_to_phixtra_ai
from lead_pipeline import record_stage_change
from portal_utils import (money_fmt, tokens_to_credits, credits_to_tokens,
                          send_email_with_attachment, TUTORIAL_VIDEOS)
import accounts_core
import signup_confirm
from portal_routes import (PLAN_EDITOR_CATALOG, PLATFORM_ONLY_FEATURE_KEYS)
from portal_routes import (_COMEBACK_VARIANTS, _render_comeback_email_html, PLAN_FEATURE_CATALOG,
                           ROLE_FORM_GRID, PLAN_ONLY_FEATURE_KEYS,
                           PLUGIN_FEATURE_PLAN_KEYS, _plugin_features_for_tenant)
from buffer_client import buffer_create_post, buffer_get_post_status, BufferAPIError
import buffer_accounts as _ba

portal_admin_bp = Blueprint("portal_admin", __name__)

# ══════════════════════════════════════════════════════════════════════════════
# SUPPORT-TEAM PERMISSIONS — module catalog
#
# Each module lists only the actions that actually exist for it (e.g. Invoices
# has no create/modify/delete route, so it only ever offers "view"). "Settings"
# and "Team" are intentionally NOT in this catalog — they stay owner-only and
# can never be delegated to a support login, to prevent privilege escalation.
# ══════════════════════════════════════════════════════════════════════════════
ADMIN_MODULES = {
    "customers":                {"label": "Customers",             "actions": ["view", "modify"]},
    "impersonate_customer":     {"label": "Impersonate Customers", "actions": ["modify"]},
    "onboard_wa":                {"label": "Onboard WhatsApp",      "actions": ["create"]},
    "catalogue":                 {"label": "Catalogue",             "actions": ["view", "create", "modify", "delete"]},
    "api_keys":                  {"label": "API Keys",              "actions": ["view", "create", "modify", "delete"]},
    "invoices":                  {"label": "Invoices",              "actions": ["view"]},
    "credit_packages":           {"label": "Credit Top-ups",         "actions": ["view", "create", "modify", "delete"]},
    "plugins":                   {"label": "Plugins",               "actions": ["view", "create", "delete"]},
    "plans":                     {"label": "Plans",                 "actions": ["view", "modify", "delete"]},
    "school_plans":              {"label": "School Plans",          "actions": ["view", "modify"]},
    "wa_diagnostics":            {"label": "WA Diagnostics",        "actions": ["view"]},
    "recovery_queue":            {"label": "Recovery Queue",        "actions": ["view"]},
    "video_tutorials":           {"label": "Video Tutorials",       "actions": ["view", "modify"]},
    "social_media":               {"label": "Social Media Posts",    "actions": ["view", "create", "modify", "delete"]},
    "comeback_sequence":          {"label": "Onboarding Drip Campaign", "actions": ["view", "modify"]},
    "modules":                    {"label": "Modules",                "actions": ["view"]},
}

# Which table / ambassador_leads column holds each product's accounts
# (Customers page: Sales Manager assignment).
LEAD_LINK_COL  = {"portal": "tenant_id", "school": "school_id", "estate": "estate_tenant_id"}
LEAD_REF_TABLE = {"portal": "tenants",   "school": "school_profiles", "estate": "re_tenants"}


# Ambassador document library — shared PDF/Word/Excel/PowerPoint/image files.
AMB_DOC_UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads", "ambassador_documents")
AMB_DOC_ALLOWED_EXTS  = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "jpg", "jpeg", "png"}
AMB_DOC_MAX_BYTES     = 25 * 1024 * 1024  # 25 MB
AMB_DOC_ICONS = {
    "pdf": "📕", "doc": "📘", "docx": "📘",
    "xls": "📗", "xlsx": "📗", "ppt": "📙", "pptx": "📙",
    "jpg": "🖼️", "jpeg": "🖼️", "png": "🖼️",
}

# Social Media Posts — internal content queue (post images + captions ready
# for the social media executive to publish manually on each platform).
SM_UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads", "social_media")
SM_ALLOWED_EXTS  = {"jpg", "jpeg", "png", "webp", "gif"}
SM_MAX_BYTES     = 15 * 1024 * 1024  # 15 MB
SM_PLATFORMS = {
    "facebook":  {"label": "Facebook",  "icon": "📘", "service": "facebook"},
    "instagram": {"label": "Instagram", "icon": "📸", "service": "instagram"},
    "linkedin":  {"label": "LinkedIn",  "icon": "💼", "service": "linkedin"},
    "tiktok":    {"label": "TikTok",    "icon": "🎵", "service": "tiktok"},
    "x":         {"label": "X",         "icon": "✖️", "service": "twitter"},
}


def _admin_logged_in() -> bool:
    return session.get("portal_admin_logged_in") is True

def _is_owner() -> bool:
    return session.get("portal_admin_role", "owner") == "owner"

def _require_admin(module: str = None, action: str = "view"):
    """Gate a route. With no args: just require login (legacy behaviour).
    With `module`/`action`: also require the permission on support logins —
    owners always pass. `module` must be a key in ADMIN_MODULES; Settings and
    Team have no module key and so can never be reached by a support login."""
    if not _admin_logged_in():
        return redirect(url_for("portal_admin.login"))
    if module and not _is_owner():
        granted = session.get("portal_admin_permissions", {}).get(module, {})
        if not granted.get(action):
            return redirect(url_for("portal_admin.forbidden"))
    return None

def _require_owner():
    """Team management and Settings are never delegable — owner-only, always."""
    if not _admin_logged_in():
        return redirect(url_for("portal_admin.login"))
    if not _is_owner():
        return redirect(url_for("portal_admin.forbidden"))
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
        if not admin.get("active", True):
            flash("This admin login has been deactivated.", "danger")
            return redirect(url_for("portal_admin.login"))
        session["portal_admin_logged_in"]  = True
        session["portal_admin_username"]   = username
        session["portal_admin_role"]        = admin.get("role") or "owner"
        session["portal_admin_permissions"] = _parse_json_maybe(admin.get("permissions"))
        return redirect(url_for("portal_admin.customers"))

    flash("Invalid admin login.", "danger")
    return redirect(url_for("portal_admin.login"))


@portal_admin_bp.route("/logout")
def logout():
    session.pop("portal_admin_logged_in", None)
    session.pop("portal_admin_username",  None)
    session.pop("portal_admin_role",       None)
    session.pop("portal_admin_permissions", None)
    session.pop("impersonate_customer_id", None)
    session.pop("impersonate_ambassador_id", None)
    session.pop("ambassador_logged_in", None)
    return redirect(url_for("portal_admin.login"))


@portal_admin_bp.route("/forbidden")
def forbidden():
    if not _admin_logged_in():
        return redirect(url_for("portal_admin.login"))
    return render_template("portal/admin_forbidden.html"), 403


# ══════════════════════════════════════════════════════════════════════════════
# TEAM MANAGEMENT — owner-only. Create/edit support logins and their
# per-module View/Create/Modify/Delete permissions. Never delegable itself.
# ══════════════════════════════════════════════════════════════════════════════

def _parse_json_maybe(val) -> dict:
    """psycopg2 sometimes returns JSONB already decoded, sometimes as a raw string
    depending on connection setup — normalise to a dict either way."""
    if isinstance(val, str):
        try:
            return _json.loads(val) or {}
        except (ValueError, TypeError):
            return {}
    return val or {}


def _parse_permissions_form(form) -> dict:
    """Read the module×action checkbox matrix (name='perm__<module>__<action>')."""
    perms = {}
    for mod_key, mod in ADMIN_MODULES.items():
        granted = {a for a in mod["actions"] if form.get(f"perm__{mod_key}__{a}") == "on"}
        if not granted:
            continue
        # "view" is implicit prerequisite for create/modify/delete
        if granted - {"view"}:
            granted.add("view")
        perms[mod_key] = {a: True for a in granted}
    return perms


@portal_admin_bp.route("/team")
def admin_team():
    r = _require_owner()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, username, role, permissions, active, created_at FROM admin_users ORDER BY id")
    admins = cur.fetchall() or []
    cur.close(); conn.close()

    for a in admins:
        perms = _parse_json_maybe(a.get("permissions"))
        a["permissions"] = perms
        a["module_count"] = len(perms)
        a["module_labels"] = [ADMIN_MODULES[m]["label"] for m in perms if m in ADMIN_MODULES]

    return render_template("portal/admin_team.html", admins=admins, modules=ADMIN_MODULES,
                            my_username=_admin_user())


@portal_admin_bp.route("/team/new", methods=["GET", "POST"])
def admin_team_new():
    r = _require_owner()
    if r: return r

    if request.method == "GET":
        return render_template("portal/admin_team_form.html", modules=ADMIN_MODULES, admin=None)

    username = (request.form.get("username") or "").strip()
    if not username:
        flash("Username is required.", "danger")
        return redirect(url_for("portal_admin.admin_team_new"))

    alphabet = string.ascii_letters + string.digits
    password = ''.join(secrets.choice(alphabet) for _ in range(12))
    permissions = _parse_permissions_form(request.form)

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id FROM admin_users WHERE username=%s", (username,))
    if cur.fetchone():
        cur.close(); conn.close()
        flash("That username already exists.", "danger")
        return redirect(url_for("portal_admin.admin_team_new"))

    cur.execute("""
        INSERT INTO admin_users (username, password, role, permissions, active)
        VALUES (%s, %s, 'support', %s, TRUE) RETURNING id""",
        (username, password, _json.dumps(permissions)))
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(), action="admin_team_create_support",
                      tenant_id=None, website=None, key_type=None, api_key_id=None)
    flash(f"Support login '{username}' created. Password: {password} (shown once — share it securely).", "success")
    return redirect(url_for("portal_admin.admin_team"))


@portal_admin_bp.route("/team/<int:admin_id>/edit", methods=["GET", "POST"])
def admin_team_edit(admin_id: int):
    r = _require_owner()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM admin_users WHERE id=%s", (admin_id,))
    admin = cur.fetchone()

    if not admin:
        cur.close(); conn.close()
        flash("Admin login not found.", "danger")
        return redirect(url_for("portal_admin.admin_team"))

    if admin["role"] == "owner":
        cur.close(); conn.close()
        flash("Owner accounts are not managed here.", "danger")
        return redirect(url_for("portal_admin.admin_team"))

    if request.method == "GET":
        cur.close(); conn.close()
        admin["permissions"] = _parse_json_maybe(admin.get("permissions"))
        return render_template("portal/admin_team_form.html", modules=ADMIN_MODULES, admin=admin)

    permissions = _parse_permissions_form(request.form)
    cur.execute("UPDATE admin_users SET permissions=%s WHERE id=%s",
                (_json.dumps(permissions), admin_id))
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(), action="admin_team_update_permissions",
                      tenant_id=None, website=None, key_type=None, api_key_id=None)
    flash(f"Permissions updated for '{admin['username']}'.", "success")
    return redirect(url_for("portal_admin.admin_team"))


@portal_admin_bp.route("/team/<int:admin_id>/toggle-active", methods=["POST"])
def admin_team_toggle_active(admin_id: int):
    r = _require_owner()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT username, role, active FROM admin_users WHERE id=%s", (admin_id,))
    admin = cur.fetchone()
    if not admin or admin["role"] == "owner":
        cur.close(); conn.close()
        flash("That login cannot be modified here.", "danger")
        return redirect(url_for("portal_admin.admin_team"))

    new_active = not admin["active"]
    cur.execute("UPDATE admin_users SET active=%s WHERE id=%s", (new_active, admin_id))
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(),
                      action="admin_team_activate" if new_active else "admin_team_deactivate",
                      tenant_id=None, website=None, key_type=None, api_key_id=None)
    flash(f"'{admin['username']}' {'reactivated' if new_active else 'deactivated'}.", "success")
    return redirect(url_for("portal_admin.admin_team"))


@portal_admin_bp.route("/team/<int:admin_id>/reset-password", methods=["POST"])
def admin_team_reset_password(admin_id: int):
    r = _require_owner()
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT username, role FROM admin_users WHERE id=%s", (admin_id,))
    admin = cur.fetchone()
    if not admin or admin["role"] == "owner":
        cur.close(); conn.close()
        flash("That login cannot be modified here.", "danger")
        return redirect(url_for("portal_admin.admin_team"))

    alphabet = string.ascii_letters + string.digits
    new_password = ''.join(secrets.choice(alphabet) for _ in range(12))
    cur.execute("UPDATE admin_users SET password=%s WHERE id=%s", (new_password, admin_id))
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(), action="admin_team_reset_password",
                      tenant_id=None, website=None, key_type=None, api_key_id=None)
    flash(f"New password for '{admin['username']}': {new_password} (shown once — share it securely).", "success")
    return redirect(url_for("portal_admin.admin_team"))


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

# ── Customer → Sales Manager assignment ──────────────────────────────────────
# Lets Admin hand a signed-up business to a Sales Manager for onboarding
# follow-up even when it didn't come through an ambassador referral (company
# campaign/promo signups). Piggybacks on the existing ambassador_leads table
# via its `sales_manager_id` column so the assignment shows up automatically
# in that manager's Team Pipeline — see [[project_sales_manager_customer_assignment]].

def _portal_sales_managers() -> list:
    """Active Sales Managers who run the Portal product — the only ones
    eligible to be assigned a Portal customer."""
    return _sales_managers_for("portal")


def _amb_manager_assignments(ref_ids: list, product: str = "portal") -> dict:
    """ref_id -> {'manager_id', 'manager_name'} for every account of the given
    product that currently has a live (non-dropped) Sales Manager assignment.
    `ref_id` is the tenants.id / school_profiles.id / re_tenants.id for that
    product — ambassador_leads links to a different column per product
    (LEAD_LINK_COL), because Portal, School and Estate accounts each live in
    their own table."""
    if not ref_ids:
        return {}
    link_col = LEAD_LINK_COL.get(product, "tenant_id")
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
        SELECT DISTINCT ON (al.{link_col})
               al.{link_col} AS ref_id, al.sales_manager_id AS manager_id,
               a.first_name || ' ' || a.last_name AS manager_name
        FROM ambassador_leads al
        JOIN ambassadors a ON a.id = al.sales_manager_id
        WHERE al.{link_col} = ANY(%s) AND al.product=%s AND al.dropped_at IS NULL
        ORDER BY al.{link_col}, al.created_at DESC
    """, (ref_ids, product))
    out = {row["ref_id"]: row for row in (cur.fetchall() or [])}
    cur.close(); conn.close()
    return out


def _sales_managers_for(product: str) -> list:
    """Active Sales Managers who run the given product — the only ones
    eligible to be assigned an account of that product."""
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, first_name, last_name FROM ambassadors
        WHERE role='sales_manager' AND status='active' AND managed_product=%s
        ORDER BY first_name, last_name
    """, (product,))
    rows = cur.fetchall() or []
    cur.close(); conn.close()
    return rows


@portal_admin_bp.route("/comeback-sequence")
def comeback_sequence():
    r = _require_admin("comeback_sequence", "view")
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT enabled FROM onboarding_nudge_settings WHERE id=1")
    settings_row = cur.fetchone()
    enabled = bool(settings_row["enabled"]) if settings_row else True

    cur.execute("SELECT email_key, COUNT(*) AS c FROM onboarding_nudge_log GROUP BY email_key")
    counts = {row["email_key"]: row["c"] for row in cur.fetchall()}

    cur.execute("""
        SELECT l.sent_at, l.email_key, c.first_name, c.last_name, c.email, t.name AS tenant_name
        FROM onboarding_nudge_log l
        JOIN customers c ON c.id = l.customer_id
        JOIN tenants t   ON t.id = l.tenant_id
        ORDER BY l.sent_at DESC LIMIT 100
    """)
    recent = cur.fetchall()
    for row in recent:
        fn = (row.get("first_name") or "").strip()
        ln = (row.get("last_name") or "").strip()
        row["full_name"] = f"{fn} {ln}".strip() or "—"

    cur.execute("SELECT COUNT(*) AS c FROM onboarding_nudge_unsubscribes")
    unsub_count = cur.fetchone()["c"]

    cur.execute("""
        SELECT COUNT(*) AS c FROM onboarding_nudge_log
        WHERE sent_at::date = CURRENT_DATE
    """)
    sent_today = cur.fetchone()["c"]

    # Live funnel — every real, non-cancelled, active account (not just the
    # "eligible" subset the scheduler emails), bucketed the same way
    # _comeback_stage() classifies them, so "activated" also shows up as a
    # real destination rather than just silently disappearing from the count.
    cur.execute("""
        SELECT
          CASE
            WHEN t.trial_granted_at IS NOT NULL THEN 'activated'
            WHEN os.customer_id IS NULL THEN 'never_started'
            WHEN os.catalogue_setup_done OR os.products_step_skipped THEN 'near_done'
            ELSE 'stuck_products'
          END AS stage,
          COUNT(*) AS c
        FROM customers c
        JOIN tenants t ON t.id = c.tenant_id
        LEFT JOIN onboarding_state os ON os.customer_id = c.id
        WHERE t.is_demo = FALSE AND t.status NOT IN ('cancelled', 'suspended') AND c.is_active = TRUE
        GROUP BY 1
    """)
    funnel = {"never_started": 0, "stuck_products": 0, "near_done": 0, "activated": 0}
    funnel.update({row["stage"]: row["c"] for row in cur.fetchall()})
    total_real = sum(funnel.values())
    eligible_count = total_real - funnel["activated"]

    cur.close(); conn.close()
    total_sent = sum(counts.values())

    timeline = [
        {"day": 1,  "situations": [("never", "Never started", counts.get("day1_never_started", 0))]},
        {"day": 3,  "situations": [("never", "Never started", counts.get("day3_never_started", 0)),
                                    ("stuck", "Stuck", counts.get("day3_stuck_products", 0))]},
        {"day": 5,  "situations": [("near",  "Near done", counts.get("day5_near_done_web", 0) + counts.get("day5_near_done_whatsapp", 0))]},
        {"day": 7,  "situations": [("never", "Never started", counts.get("day7_never_started", 0)),
                                    ("stuck", "Stuck", counts.get("day7_stuck_products", 0))]},
        {"day": 10, "situations": [("near",  "Near done", counts.get("day10_near_done", 0))]},
        {"day": 21, "situations": [("final", "Catch-all", counts.get("day21_final", 0))]},
    ]
    for step in timeline:
        step["has_sends"] = any(c for _tag, _label, c in step["situations"])

    return render_template("portal/admin_comeback_sequence.html",
                           enabled=enabled, counts=counts, recent=recent,
                           unsub_count=unsub_count, eligible_count=eligible_count,
                           total_sent=total_sent, sent_today=sent_today,
                           funnel=funnel, total_real=total_real, timeline=timeline)


@portal_admin_bp.route("/comeback-sequence/toggle", methods=["POST"])
def comeback_sequence_toggle():
    r = _require_admin("comeback_sequence", "modify")
    if r: return r

    enabled = request.form.get("enabled") == "1"
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE onboarding_nudge_settings SET enabled=%s WHERE id=1", (enabled,))
    conn.commit()
    cur.close(); conn.close()
    flash(f"The Onboarding Drip Campaign is now {'ON' if enabled else 'OFF'}.", "success")
    return redirect(url_for("portal_admin.comeback_sequence"))


_COMEBACK_SCHEDULE_META = [
    ("day1_never_started",      "Day 1",  "Never started onboarding"),
    ("day3_never_started",      "Day 3",  "Still never started"),
    ("day3_stuck_products",     "Day 3",  "Started, stuck on products"),
    ("day5_near_done_web",      "Day 5",  "Catalogue done · Web merchant"),
    ("day5_near_done_whatsapp", "Day 5",  "Catalogue done · WhatsApp merchant"),
    ("day7_never_started",      "Day 7",  "Still never started"),
    ("day7_stuck_products",     "Day 7",  "Started, stuck on products"),
    ("day10_near_done",         "Day 10", "Catalogue done, not connected"),
    ("day21_final",             "Day 21", "Catch-all — still not live"),
]
_COMEBACK_PREVIEW_CTX = {
    "first_name": "Ngozi", "business_name": "Ngozi's Kitchen",
    "channel": "your website", "store_domain": "ngozis-kitchen.com",
    "final_step_label": "Sync My Store",
}
_COMEBACK_PREVIEW_CTX_WA = {**_COMEBACK_PREVIEW_CTX, "channel": "WhatsApp",
                            "final_step_label": "Connect WhatsApp"}


@portal_admin_bp.route("/comeback-sequence/templates")
def comeback_sequence_templates():
    """Renders each fixed email through the exact same function that sends
    the real thing, with sample placeholder data — so this preview can
    never drift from what a customer actually receives."""
    r = _require_admin("comeback_sequence", "view")
    if r: return r

    previews = []
    for key, day_label, situation in _COMEBACK_SCHEDULE_META:
        variant = _COMEBACK_VARIANTS[key]
        ctx = _COMEBACK_PREVIEW_CTX_WA if "whatsapp" in key else _COMEBACK_PREVIEW_CTX
        subject = variant["subject"].format(**ctx)
        heading = variant["heading"].format(**ctx)
        paragraphs = [p.format(**ctx) for p in variant["paragraphs"]]
        checklist = [i.format(**ctx) for i in variant["checklist"]] if variant.get("checklist") else None
        html = _render_comeback_email_html(
            heading=heading, paragraphs=paragraphs, cta_text=variant["cta"], cta_url="#",
            unsubscribe_url="#", checklist=checklist, final_note=variant.get("final_note"),
        )
        previews.append(dict(key=key, day=day_label, situation=situation, subject=subject, html=html))

    return render_template("portal/admin_comeback_templates.html", previews=previews)


# Every filter group on the Customers page that's a plain checkbox list
# (Plan is built separately below, from whatever plan names are actually in
# use — Portal/Connect, School and Estate each keep their own plan catalog).
CUSTOMER_FILTER_GROUPS = [
    {"key": "source",   "label": "Signup Source",  "options": [("whatsapp", "WhatsApp-only"), ("web", "Web signup")]},
    {"key": "verified", "label": "Email Verified",  "options": [("yes", "Verified"), ("no", "Unverified")]},
    {"key": "active",   "label": "Account Status",  "options": [("yes", "Active"), ("no", "Disabled")]},
    {"key": "assigned", "label": "Assigned To",     "options": [("yes", "Assigned"), ("no", "Unassigned")]},
    {"key": "product",  "label": "Product",         "options": [("portal", "Portal / CRM"), ("connect", "Connect"),
                                                                  ("school", "SchoolCompass"), ("estate", "KeyLand")]},
]

CUSTOMER_PRODUCT_LABELS = {"portal": "Portal / CRM", "connect": "Connect", "school": "SchoolCompass", "estate": "KeyLand"}

# "How did you hear about us?" — the sign-up form's own answers, plus the few
# set by code rather than chosen (plugin sign-ups, test accounts) and "Not
# answered" for blank ones (older sign-ups, School and KeyLand never ask).
CUSTOMER_HEARD_NONE = "none"
CUSTOMER_HEARD_EXTRA = [("wordpress_plugin", "WordPress plugin"), ("internal_test", "Internal test")]


def _heard_options():
    from portal_routes import HEAR_ABOUT_US_OPTIONS
    return list(HEAR_ABOUT_US_OPTIONS) + CUSTOMER_HEARD_EXTRA + [(CUSTOMER_HEARD_NONE, "Not answered")]


def _heard_label(value) -> str:
    if not value:
        return "Not answered"
    return dict(_heard_options()).get(value) or value.replace("_", " ").capitalize()


def _customer_facet(x, key):
    """A Customers-page row's value for one filter group's checkboxes."""
    if key == "verified": return "yes" if x["verified"] else "no"
    if key == "active":   return "yes" if x["active"] else "no"
    if key == "assigned": return "yes" if x["assigned_manager_id"] is not None else "no"
    if key == "plan":     return x["plan_name"]
    if key == "heard":    return x["heard_value"]
    if key == "inactivity": return x["inactivity"]
    return x[key]  # source, product


CUSTOMER_INACTIVITY_OPTIONS = [("warned", "Warned"), ("active", "Active"), ("no_email", "Can't warn · no email"),
                               ("protected", "Protected"), ("not_included", "School / KeyLand (not included)")]


CUSTOMERS_PER_PAGE_OPTIONS = ["25", "50", "100", "200"]
CUSTOMERS_PER_PAGE_DEFAULT = "50"


def _customers_per_page() -> int:
    """Customers page "Per page", remembered per admin login across visits and
    devices. Admin logins have no business of their own, so the choice is kept
    in user_list_prefs under PhiXtra's own tenant with an "admin:" key."""
    from portal_routes import PHIXTRA_SUPPORT_TENANT_ID
    chosen = (request.args.get("per_page") or "").strip()
    actor_key = f"admin:{_admin_user()}"[:64]
    try:
        conn = get_db_connection(); cur = conn.cursor()
        if chosen in CUSTOMERS_PER_PAGE_OPTIONS:
            cur.execute("""INSERT INTO user_list_prefs (tenant_id, actor_key, page_key, per_page)
                           VALUES (%s,%s,'admin_customers',%s)
                           ON CONFLICT (tenant_id, actor_key, page_key)
                           DO UPDATE SET per_page=EXCLUDED.per_page, updated_at=NOW()""",
                        (PHIXTRA_SUPPORT_TENANT_ID, actor_key, chosen))
            conn.commit()
            value = chosen
        else:
            cur.execute("""SELECT per_page FROM user_list_prefs
                           WHERE tenant_id=%s AND actor_key=%s AND page_key='admin_customers'""",
                        (PHIXTRA_SUPPORT_TENANT_ID, actor_key))
            row = cur.fetchone()
            value = row[0] if row and row[0] in CUSTOMERS_PER_PAGE_OPTIONS else CUSTOMERS_PER_PAGE_DEFAULT
        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ _customers_per_page error:", e)
        value = chosen if chosen in CUSTOMERS_PER_PAGE_OPTIONS else CUSTOMERS_PER_PAGE_DEFAULT
    return int(value)


def _customers_page_url(page: int) -> str:
    """Current /admin/customers URL (filters kept) on another page number."""
    args = request.args.to_dict(flat=False)
    args.pop("per_page", None)
    args["page"] = [str(page)]
    return f"{url_for('portal_admin.customers')}?{urlencode(args, doseq=True)}"

# Industry has no real data behind it yet — no product's account record
# captures a business's industry today. It stays visible (so the gap isn't
# hidden) but disabled, rather than pretending to filter on nothing.
CUSTOMER_INDUSTRY_OPTIONS = [
    "Retail & Fashion", "Electronics", "Food & Beverage", "Beauty & Health",
    "Real Estate", "Education", "Professional Services", "Other",
]


def _remove_filter_url(remove_key: str, remove_value: str) -> str:
    """Current /admin/customers URL with one value taken out of one filter
    (or the key dropped entirely if that was its only value) — powers each
    active-filter chip's × ."""
    args = request.args.to_dict(flat=False)
    if remove_key in args:
        args[remove_key] = [v for v in args[remove_key] if v != remove_value]
        if not args[remove_key]:
            del args[remove_key]
    qs = urlencode(args, doseq=True)
    base = url_for("portal_admin.customers")
    return f"{base}?{qs}" if qs else base


@portal_admin_bp.route("/customers")
def customers():
    r = _require_admin("customers", "view")
    if r: return r

    q          = (request.args.get("q") or "").strip().lower()
    date_from  = (request.args.get("date_from") or "").strip()
    date_to    = (request.args.get("date_to") or "").strip()
    f_source   = request.args.getlist("source")
    f_verified = request.args.getlist("verified")
    f_active   = request.args.getlist("active")
    f_assigned = request.args.getlist("assigned")
    f_plan     = request.args.getlist("plan")
    f_product  = request.args.getlist("product")
    f_heard    = request.args.getlist("heard")
    f_inactive = request.args.getlist("inactivity")

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    rows = []

    # ── Portal + Connect (share the tenants/customers tables) ──────────────
    where  = ["t.is_demo = FALSE"]
    params = []

    if q:
        where.append("""(LOWER(c.email) LIKE %s OR LOWER(t.domain) LIKE %s
                          OR LOWER(t.name) LIKE %s
                          OR LOWER(CONCAT(COALESCE(c.first_name,''),' ',COALESCE(c.last_name,''))) LIKE %s)""")
        params += [f"%{q}%"] * 4
    if date_from:
        where.append("c.created_at >= %s"); params.append(date_from)
    if date_to:
        where.append("c.created_at < (%s::date + INTERVAL '1 day')"); params.append(date_to)

    cur.execute(f"""
        SELECT c.id, c.first_name, c.last_name, c.email, c.phone_number, c.phone_verified,
               c.email_verified, c.is_active, c.created_at, c.hear_about_us,
               t.id AS tenant_id, t.name AS tenant_name, t.domain, t.signup_product, t.source_type,
               COALESCE(p.name,'Free') AS plan_name,
               COALESCE(tb.token_balance,0) AS token_balance
        FROM customers c
        JOIN tenants t ON t.id=c.tenant_id
        LEFT JOIN tenant_balances tb ON tb.tenant_id=t.id
        LEFT JOIN plans p ON p.id=t.plan_id
        WHERE {' AND '.join(where)}
        ORDER BY c.created_at DESC LIMIT 5000
    """, params)
    pc_rows = cur.fetchall() or []

    # Portal/Connect businesses are assigned to the platform business's own
    # staff (Accounts page, 2026-10-10), not to ambassador Sales Managers.
    pc_ids = [row["tenant_id"] for row in pc_rows]
    acct = accounts_core.assignments_for(pc_ids)
    setup = accounts_core.setup_steps_many(pc_ids)
    for row in pc_rows:
        fn = (row.get("first_name") or "").strip()
        ln = (row.get("last_name")  or "").strip()
        a_ = acct.get(row["tenant_id"])
        assignment = {"manager_id": a_["team_member_id"], "manager_name": a_["staff_name"]} if a_ else None
        rows.append({
            "product": row["signup_product"], "ref_id": row["tenant_id"], "customer_id": row["id"],
            "full_name": f"{fn} {ln}".strip() or "—", "business_name": row["tenant_name"] or "—",
            "email": row["email"], "phone": row["phone_number"] or "—", "domain": row["domain"],
            "phone_ok": bool(row.get("phone_verified") and row["phone_number"]),
            "verified": row["email_verified"], "active": row["is_active"],
            "created_at": row["created_at"], "plan_name": row["plan_name"],
            "balance_credits": tokens_to_credits(int(row.get("token_balance") or 0)),
            "assigned_manager_id":   assignment["manager_id"]   if assignment else None,
            "assigned_manager_name": assignment["manager_name"] if assignment else None,
            "setup": setup.get(row["tenant_id"]),
            "heard": _heard_label(row.get("hear_about_us")),
            "heard_value": row.get("hear_about_us") or CUSTOMER_HEARD_NONE,
            "source": row.get("source_type"),
            "inactivity": "active",
        })

    # ── School (school_profiles — its own table, no "customers" split) ─────
    where  = [
        "LOWER(s.contact_email) NOT LIKE '%%@phixtra.com'",
        "LOWER(s.contact_email) NOT LIKE '%%demo%%'",
    ]
    params = []
    if q:
        where.append("(LOWER(s.contact_email) LIKE %s OR LOWER(s.school_name) LIKE %s OR LOWER(COALESCE(s.principal_name,'')) LIKE %s)")
        params += [f"%{q}%"] * 3
    if date_from:
        where.append("s.created_at >= %s"); params.append(date_from)
    if date_to:
        where.append("s.created_at < (%s::date + INTERVAL '1 day')"); params.append(date_to)

    cur.execute(f"""
        SELECT s.id, s.school_name, s.principal_name, s.contact_email,
               s.is_active, s.created_at, COALESCE(sp.name,'Free') AS plan_name
        FROM school_profiles s
        LEFT JOIN school_plans sp ON sp.id = s.plan_id
        WHERE {' AND '.join(where)}
        ORDER BY s.created_at DESC LIMIT 5000
    """, params)
    school_rows = cur.fetchall() or []

    assignments = _amb_manager_assignments([row["id"] for row in school_rows], "school")
    for row in school_rows:
        assignment = assignments.get(row["id"])
        rows.append({
            "product": "school", "ref_id": row["id"], "customer_id": None,
            "full_name": row["principal_name"] or "—", "business_name": row["school_name"] or "—",
            "email": row["contact_email"], "phone": "—", "domain": None,
            "verified": True, "active": row["is_active"],
            "created_at": row["created_at"], "plan_name": row["plan_name"],
            "balance_credits": None,
            "assigned_manager_id":   assignment["manager_id"]   if assignment else None,
            "assigned_manager_name": assignment["manager_name"] if assignment else None,
            "heard": "Not answered", "heard_value": CUSTOMER_HEARD_NONE, "source": "web",
            "inactivity": "not_included",
        })

    # ── Estate (re_tenants — its own table, tenant + owner in one row) ──────
    where  = [
        "LOWER(r.email) NOT LIKE '%%@phixtra.com'",
        "LOWER(r.email) NOT LIKE '%%demo%%'",
        "LOWER(COALESCE(r.business_name,'')) NOT LIKE '%%test%%'",
    ]
    params = []
    if q:
        where.append("(LOWER(r.email) LIKE %s OR LOWER(COALESCE(r.business_name,'')) LIKE %s OR LOWER(CONCAT(COALESCE(r.first_name,''),' ',COALESCE(r.last_name,''))) LIKE %s)")
        params += [f"%{q}%"] * 3
    if date_from:
        where.append("r.created_at >= %s"); params.append(date_from)
    if date_to:
        where.append("r.created_at < (%s::date + INTERVAL '1 day')"); params.append(date_to)

    cur.execute(f"""
        SELECT r.id, r.business_name, r.first_name, r.last_name, r.email, r.phone,
               r.status, r.created_at, COALESCE(rp.name,'Free') AS plan_name
        FROM re_tenants r
        LEFT JOIN re_plans rp ON rp.id = r.plan_id
        WHERE {' AND '.join(where)}
        ORDER BY r.created_at DESC LIMIT 5000
    """, params)
    estate_rows = cur.fetchall() or []

    assignments = _amb_manager_assignments([row["id"] for row in estate_rows], "estate")
    for row in estate_rows:
        fn = (row.get("first_name") or "").strip()
        ln = (row.get("last_name")  or "").strip()
        assignment = assignments.get(row["id"])
        rows.append({
            "product": "estate", "ref_id": row["id"], "customer_id": None,
            "full_name": f"{fn} {ln}".strip() or "—", "business_name": row["business_name"] or "—",
            "email": row["email"], "phone": row["phone"] or "—", "domain": None,
            "verified": True, "active": (row["status"] == "active"),
            "created_at": row["created_at"], "plan_name": row["plan_name"],
            "balance_credits": None,
            "assigned_manager_id":   assignment["manager_id"]   if assignment else None,
            "assigned_manager_name": assignment["manager_name"] if assignment else None,
            "heard": "Not answered", "heard_value": CUSTOMER_HEARD_NONE, "source": "web",
            "inactivity": "not_included",
        })

    import inactivity_core
    inact = {a["tenant_id"]: a["status"] for a in inactivity_core.accounts()}
    rows = [x for x in rows if not (x["product"] in ("portal", "connect") and inact.get(x["ref_id"]) == "deleted")]
    for x in rows:
        if x["product"] in ("portal", "connect"):
            x["inactivity"] = inact.get(x["ref_id"], "active")

    selected = {k: v for k, v in {
        "inactivity": f_inactive, "source": f_source, "verified": f_verified, "active": f_active, "assigned": f_assigned,
        "product": f_product, "plan": f_plan, "heard": f_heard,
    }.items() if v}

    def _passes(x, skip=None):
        return all(_customer_facet(x, k) in v for k, v in selected.items() if k != skip)

    facet_counts = {}
    for key in ("source", "verified", "active", "assigned", "product", "plan", "heard", "inactivity"):
        counts = facet_counts.setdefault(key, {})
        for x in rows:
            if _passes(x, key):
                val = _customer_facet(x, key)
                counts[val] = counts.get(val, 0) + 1
    rows = [x for x in rows if _passes(x)]

    rows.sort(key=lambda x: x["created_at"], reverse=True)

    per_page = _customers_per_page()
    total = len(rows)
    pages = max(1, -(-total // per_page))
    try:
        page = min(max(1, int(request.args.get("page") or 1)), pages)
    except ValueError:
        page = 1
    rows = rows[(page - 1) * per_page: page * per_page]
    paging = {
        "page": page, "pages": pages, "total": total, "per_page": per_page,
        "first": (page - 1) * per_page + 1 if total else 0, "last": min(page * per_page, total),
        "prev_url": _customers_page_url(page - 1) if page > 1 else None,
        "next_url": _customers_page_url(page + 1) if page < pages else None,
    }

    # Plan checkboxes: only names actually in use right now, pooled across
    # all three plan catalogs (Portal/Connect, School and Estate each keep
    # their own).
    plan_cur = conn.cursor()
    plan_cur.execute("""
        SELECT DISTINCT name FROM plans
        UNION SELECT DISTINCT name FROM school_plans
        UNION SELECT DISTINCT name FROM re_plans
        ORDER BY name
    """)
    plan_names = [row[0] for row in plan_cur.fetchall()]
    plan_cur.close()
    cur.close(); conn.close()

    filter_groups = []
    for g in CUSTOMER_FILTER_GROUPS:
        selected = request.args.getlist(g["key"])
        filter_groups.append({
            "key": g["key"], "label": g["label"],
            "options": [{"value": v, "label": lbl, "checked": v in selected,
                         "n": facet_counts[g["key"]].get(v, 0)} for v, lbl in g["options"]],
            "count": len(selected),
        })
    filter_groups.append({
        "key": "inactivity", "label": "Inactivity",
        "options": [{"value": v, "label": lbl, "checked": v in f_inactive, "n": facet_counts["inactivity"].get(v, 0)}
                    for v, lbl in CUSTOMER_INACTIVITY_OPTIONS],
        "count": len(f_inactive),
    })
    filter_groups.append({
        "key": "heard", "label": "Heard About Us",
        "options": [{"value": v, "label": lbl, "checked": v in f_heard, "n": facet_counts["heard"].get(v, 0)}
                    for v, lbl in _heard_options()],
        "count": len(f_heard),
    })
    plan_selected = f_plan
    plan_group = {
        "key": "plan", "label": "Plan",
        "options": [{"value": n, "label": n, "checked": n in plan_selected,
                     "n": facet_counts["plan"].get(n, 0)} for n in plan_names],
        "count": len(plan_selected),
    }

    chips = []
    for g in CUSTOMER_FILTER_GROUPS:
        label_map = dict(g["options"])
        for val in request.args.getlist(g["key"]):
            if val in label_map:
                chips.append({"label": f"{g['label']}: {label_map[val]}", "href": _remove_filter_url(g["key"], val)})
    for val in f_inactive:
        chips.append({"label": f"Inactivity: {dict(CUSTOMER_INACTIVITY_OPTIONS).get(val, val)}",
                      "href": _remove_filter_url("inactivity", val)})
    for val in f_heard:
        chips.append({"label": f"Heard About Us: {_heard_label(None if val == CUSTOMER_HEARD_NONE else val)}",
                      "href": _remove_filter_url("heard", val)})
    for val in f_plan:
        chips.append({"label": f"Plan: {val}", "href": _remove_filter_url("plan", val)})
    if date_from:
        chips.append({"label": f"From {date_from}", "href": _remove_filter_url("date_from", date_from)})
    if date_to:
        chips.append({"label": f"To {date_to}", "href": _remove_filter_url("date_to", date_to)})

    clear_all_url = url_for("portal_admin.customers", q=q) if q else url_for("portal_admin.customers")

    sales_managers_by_product = {
        "portal": _sales_managers_for("portal"),
        "school": _sales_managers_for("school"),
        "estate": _sales_managers_for("estate"),
    }

    return render_template(
        "portal/admin_customers.html", customers=rows, q=q,
        sales_managers=sales_managers_by_product["portal"],
        platform_staff=accounts_core.platform_staff(),
        sales_managers_by_product=sales_managers_by_product,
        filter_groups=filter_groups, plan_group=plan_group,
        industry_options=CUSTOMER_INDUSTRY_OPTIONS, PRODUCT_LABELS=CUSTOMER_PRODUCT_LABELS,
        date_from=date_from, date_to=date_to,
        chips=chips, active_count=len(chips), clear_all_url=clear_all_url,
        paging=paging, per_page_options=CUSTOMERS_PER_PAGE_OPTIONS,
        signup_week=signup_confirm.week_counts(),
    )


@portal_admin_bp.route("/customers/<int:customer_id>")
def customer_detail(customer_id: int):
    r = _require_admin("customers", "view")
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
               COALESCE(p.ai_messages_limit,100) AS ai_messages_limit,
               COALESCE(t.ai_enabled, TRUE) AS ai_enabled,
               COALESCE(t.crm_enabled, FALSE) AS crm_enabled,
               COALESCE(t.meta_ai_enabled, FALSE) AS meta_ai_enabled,
               t.meta_ai_last_synced_at, t.meta_ai_sync_status,
               t.meta_ai_eligible, t.meta_ai_eligibility_checked_at
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

    # WooCommerce Plugin features — read-only here; the plan decides.
    _PLUGIN_LABELS = [
        ("product_recommendation",     "🛍️ Product Recommendation"),
        ("related_products",           "🔗 Automated Cross-selling"),
        ("cart_recovery",              "🛒 Intelligent Cart Revenue Recovery"),
        ("verified_specs_web_lookup",  "🔎 Verified Specs Lookup (Web)"),
        ("chat_archive_30days",        "📅 Chat Archive — 30 days"),
        ("chat_archive_unlimited",     "♾️ Chat Archive — Unlimited"),
        ("whatsapp_message_templates", "📩 WhatsApp Message Templates"),
    ]
    plugin_status = []
    try:
        _eff = _plugin_features_for_tenant(tenant_id)
        cur.execute("SELECT feature_key FROM plan_feature_grants g JOIN tenants t ON t.plan_id=g.plan_id WHERE t.id=%s", (tenant_id,))
        _grants = {r["feature_key"] for r in cur.fetchall()}
        for _flag, _label in _PLUGIN_LABELS:
            _in_plan = PLUGIN_FEATURE_PLAN_KEYS[_flag] in _grants
            plugin_status.append({"label": _label, "in_plan": _in_plan,
                                  "kept": bool(_eff.get(_flag)) and not _in_plan})
    except Exception as e:
        print("⚠️ customer_detail plugin_status error:", e)

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

    # System prompt — prefer active agent, fall back to tenant row
    tenant_system_prompt = ""
    try:
        cur.execute("""
            SELECT COALESCE(ta.system_prompt, t.system_prompt) AS system_prompt
            FROM tenants t
            LEFT JOIN tenant_agents ta ON ta.tenant_id = t.id AND ta.is_active = TRUE
            WHERE t.id = %s
        """, (tenant_id,))
        sp_row = cur.fetchone() or {}
        tenant_system_prompt = (sp_row.get("system_prompt") or "").strip()
    except Exception:
        tenant_system_prompt = ""

    # Plan data for assignment panel
    cur.execute("SELECT id, slug, name, annual_discount_pct FROM plans WHERE is_active=TRUE ORDER BY sort_order")
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

    # "How did you hear about us" is stored as a slug (e.g. "sales_ambassador") —
    # map it to its human-readable label for display.
    from portal_routes import HEAR_ABOUT_US_OPTIONS
    customer["hear_about_us_label"] = dict(HEAR_ABOUT_US_OPTIONS).get(
        customer.get("hear_about_us"), customer.get("hear_about_us")) or "—"

    import inactivity_core
    _ia = inactivity_core.accounts([tenant_id])
    customer["inactivity"] = _ia[0] if _ia else None

    a_ = accounts_core.assignments_for([tenant_id]).get(tenant_id)
    customer["assigned_manager_id"]   = a_["team_member_id"] if a_ else None
    customer["assigned_manager_name"] = a_["staff_name"]     if a_ else None
    customer["setup"] = accounts_core.setup_steps_many([tenant_id]).get(tenant_id)

    for inv in invs:
        inv["total_fmt"] = money_fmt(
            int(inv.get("amount_pence") or 0) + int(inv.get("vat_pence") or 0),
            inv.get("currency") or "gbp")

    conn3 = get_db_connection()
    cur3 = conn3.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur3.execute("SELECT domain, from_email, from_name, status FROM email_domains WHERE tenant_id=%s",
                 (tenant_id,))
    email_sender = cur3.fetchone()
    cur3.close(); conn3.close()

    conn4 = get_db_connection()
    cur4 = conn4.cursor()
    cur4.execute("""
        SELECT id, display_phone_number, phone_number_id, COALESCE(ai_enabled, TRUE) AS ai_enabled
          FROM wa_tenants WHERE tenant_id=%s AND active=TRUE ORDER BY id ASC
    """, (tenant_id,))
    wa_numbers = [{"id": r[0], "display": r[1] or r[2], "ai_enabled": r[3]} for r in cur4.fetchall()]
    wa_connected = bool(wa_numbers)
    cur4.close(); conn4.close()

    # Meta Business AI migration wizard step — one number so the admin page
    # can show a single "what's next" instead of scattered independent
    # buttons. See project_phixtra_meta_business_agent_connector memory.
    if not wa_connected:
        meta_wizard_step = 0        # nothing possible until WhatsApp is connected
    elif not customer["meta_ai_enabled"]:
        meta_wizard_step = 1        # turn on knowledge sync
    elif customer["meta_ai_eligible"] is None:
        meta_wizard_step = 2        # check eligibility
    elif not customer["ai_enabled"]:
        meta_wizard_step = 4        # already live on Meta
    else:
        meta_wizard_step = 3        # ready to go live

    return render_template("portal/admin_customer_detail.html",
                           customer=customer, keys=keys,
                           invoices=invs, audit=audit,
                           tenant_features=tenant_features,
                           plugin_status=plugin_status,
                           doc_counts=doc_counts,
                           all_plans=all_plans,
                           msgs_used=msgs_used,
                           onboarding_other=onboarding_other,
                           tenant_system_prompt=tenant_system_prompt,
                           email_sender=email_sender,
                           sales_managers=_portal_sales_managers(),
                           platform_staff=accounts_core.platform_staff(),
                           wa_connected=wa_connected,
                           wa_numbers=wa_numbers,
                           meta_wizard_step=meta_wizard_step,
                           admin_new_plain_key=session.pop("admin_new_plain_key", None))


@portal_admin_bp.route("/customers/<int:customer_id>/assign-manager", methods=["POST"])
def customer_assign_manager(customer_id: int):
    """Assign a Portal/Connect business to a staff member of the platform
    business (their Accounts page). 2026-10-10: replaces the old ambassador
    Sales Manager hand-off for these businesses; School/Estate below unchanged."""
    r = _require_admin("customers", "modify")
    if r: return r

    fallback = request.referrer or url_for("portal_admin.customers")

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.tenant_id, t.name AS tenant_name
        FROM customers c JOIN tenants t ON t.id=c.tenant_id
        WHERE c.id=%s
    """, (customer_id,))
    customer = cur.fetchone()
    cur.close(); conn.close()
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(fallback)

    tenant_id = int(customer["tenant_id"])
    if accounts_core.is_platform_tenant(tenant_id):
        flash("That's PhiXtra's own business account.", "warning")
        return redirect(fallback)
    raw = (request.form.get("staff_id") or "").strip()
    if raw and not raw.isdigit():
        flash("Choose a staff member from the list.", "danger")
        return redirect(fallback)
    try:
        name = accounts_core.assign(tenant_id, int(raw) if raw else None, f"{_admin_user()} (Admin)")
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(fallback)

    insert_audit_log(admin_username=_admin_user(),
                     action="customer_assign_staff" if name else "customer_unassign_staff",
                     tenant_id=tenant_id,
                     details={"customer_id": customer_id, "tenant_id": tenant_id,
                              "team_member_id": int(raw) if raw else None, "staff_name": name})
    if name:
        flash(f"{customer['tenant_name'] or 'Customer'} assigned to {name}. It's now on their Accounts page.", "success")
    else:
        flash("Assignment removed.", "success")
    return redirect(fallback)


@portal_admin_bp.route("/customers/<product>/<int:ref_id>/assign-manager", methods=["POST"])
def customer_assign_manager_x(product: str, ref_id: int):
    """Same job as customer_assign_manager() above, for School and Estate
    accounts — they don't have a `customers` row to key off, so this is
    keyed by (product, ref_id) straight into their own tenant table."""
    r = _require_admin("customers", "modify")
    if r: return r

    fallback = request.referrer or url_for("portal_admin.customers")

    if product not in ("school", "estate"):
        flash("Unknown product.", "danger")
        return redirect(fallback)

    ref_table = LEAD_REF_TABLE[product]
    link_col  = LEAD_LINK_COL[product]
    name_col  = "school_name"    if product == "school" else "business_name"
    email_col = "contact_email"  if product == "school" else "email"

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT id, {name_col} AS account_name, {email_col} AS email FROM {ref_table} WHERE id=%s",
                (ref_id,))
    account = cur.fetchone()
    if not account:
        cur.close(); conn.close()
        flash("Account not found.", "danger")
        return redirect(fallback)

    mgr_id_raw = (request.form.get("sales_manager_id") or "").strip()
    new_manager = None
    if mgr_id_raw:
        if not mgr_id_raw.isdigit():
            cur.close(); conn.close()
            flash("Invalid Sales Manager.", "danger")
            return redirect(fallback)
        cur.execute("""
            SELECT id, first_name, last_name FROM ambassadors
            WHERE id=%s AND role='sales_manager' AND status='active' AND managed_product=%s
        """, (int(mgr_id_raw), product))
        new_manager = cur.fetchone()
        if not new_manager:
            cur.close(); conn.close()
            flash(f"That Sales Manager is not available for {product.title()} customers.", "danger")
            return redirect(fallback)

    cur.execute(f"""
        SELECT id FROM ambassador_leads
        WHERE {link_col}=%s AND product=%s AND dropped_at IS NULL
        ORDER BY created_at DESC LIMIT 1
    """, (ref_id, product))
    existing = cur.fetchone()

    if new_manager:
        if existing:
            cur.execute("UPDATE ambassador_leads SET sales_manager_id=%s WHERE id=%s",
                        (new_manager["id"], existing["id"]))
            conn.commit()
        else:
            cur.execute(f"""
                INSERT INTO ambassador_leads
                  (ambassador_id, sales_manager_id, business_name, contact_name, email,
                   notes, stage, product, {link_col}, onboarding_date, onboarding_notes)
                VALUES (NULL, %s, %s, %s, %s, %s, 'onboarding', %s, %s, CURRENT_DATE, %s)
                RETURNING id
            """, (new_manager["id"], account["account_name"], account["account_name"], account.get("email"),
                  "Signed up directly (not via ambassador referral).",
                  product, ref_id, "Assigned by admin for onboarding follow-up."))
            new_id = cur.fetchone()["id"]
            conn.commit()
            record_stage_change(new_id, None, "onboarding", f"{_admin_user()} (Admin)",
                                 "Directly assigned to Sales Manager — no ambassador referral.")

        insert_audit_log(admin_username=_admin_user(), action="customer_assign_manager", details={
            "product": product, "ref_id": ref_id, "sales_manager_id": new_manager["id"],
            "sales_manager_name": f"{new_manager['first_name']} {new_manager['last_name']}"})
        flash(f"{account['account_name'] or 'Customer'} assigned to "
              f"{new_manager['first_name']} {new_manager['last_name']} for follow-up.", "success")
    else:
        if existing:
            cur.execute("UPDATE ambassador_leads SET sales_manager_id=NULL WHERE id=%s", (existing["id"],))
            conn.commit()
            insert_audit_log(admin_username=_admin_user(), action="customer_unassign_manager",
                              details={"product": product, "ref_id": ref_id})
        flash("Sales Manager assignment removed.", "success")

    cur.close(); conn.close()
    return redirect(fallback)


@portal_admin_bp.route("/customers/<int:customer_id>/system-prompt", methods=["POST"])
def customer_system_prompt_save(customer_id: int):
    r = _require_admin("customers", "modify")
    if r: return r

    new_prompt = (request.form.get("system_prompt") or "").strip()

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id = int(row["tenant_id"])

    # Write to active agent if one exists, otherwise fall back to tenant row
    cur.execute("""
        UPDATE tenant_agents SET system_prompt=%s, updated_at=NOW()
        WHERE tenant_id=%s AND is_active=TRUE
    """, (new_prompt, tenant_id))
    if cur.rowcount == 0:
        cur.execute("UPDATE tenants SET system_prompt=%s WHERE id=%s",
                    (new_prompt, tenant_id))
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(
        admin_username=session.get("portal_admin_username", "admin"),
        action="admin_update_system_prompt",
        tenant_id=tenant_id,
        details={"customer_id": customer_id},
    )

    flash("System prompt updated successfully.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id) + "#system-prompt")


@portal_admin_bp.route("/customers/<int:customer_id>/credit-adjust", methods=["POST"])
def customer_credit_adjust(customer_id: int):
    r = _require_admin("customers", "modify")
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
    r = _require_admin("customers", "modify")
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT is_active FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    new_val = not bool(row.get("is_active"))
    cur2 = conn.cursor()
    cur2.execute("UPDATE customers SET is_active=%s WHERE id=%s", (new_val, customer_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(),
                     action="admin_toggle_customer",
                     details={"customer_id": customer_id, "new_is_active": new_val})
    flash(f"Customer {'activated' if new_val else 'disabled'}.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


@portal_admin_bp.route("/customers/<int:customer_id>/toggle-ai", methods=["POST"])
def customer_toggle_ai(customer_id: int):
    """PhiXtra-admin-only switch: turn AI-generated WhatsApp replies on/off
    for this business's tenant. Never exposed to the business itself —
    Connect signups start with this off automatically at registration;
    this button is only for moving a business onto AI later (or back off)."""
    r = _require_admin("customers", "modify")
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

    cur.execute("SELECT ai_enabled FROM tenants WHERE id=%s", (tenant_id,))
    trow = cur.fetchone() or {}
    new_val = not bool(trow.get("ai_enabled", True))

    cur2 = conn.cursor()
    cur2.execute("UPDATE tenants SET ai_enabled=%s WHERE id=%s", (new_val, tenant_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(),
                     action="admin_toggle_ai",
                     tenant_id=tenant_id,
                     details={"customer_id": customer_id, "new_ai_enabled": new_val})
    flash(f"AI replies {'turned on' if new_val else 'turned off'} for this business.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


@portal_admin_bp.route("/customers/<int:customer_id>/numbers/<int:wa_id>/toggle-ai", methods=["POST"])
def customer_toggle_number_ai(customer_id: int, wa_id: int):
    """PhiXtra-admin-only switch, per WhatsApp number (2026-10-01): e.g. keep
    AI on a sales number and make a support number staff-only. The business
    switch above still wins — AI replies only when both are on."""
    r = _require_admin("customers", "modify")
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

    cur.execute("SELECT display_phone_number, COALESCE(ai_enabled, TRUE) AS ai_enabled "
                "FROM wa_tenants WHERE id=%s AND tenant_id=%s", (wa_id, tenant_id))
    wrow = cur.fetchone()
    if not wrow:
        cur.close(); conn.close()
        flash("That number isn't connected to this business.", "danger")
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))
    new_val = not bool(wrow["ai_enabled"])

    cur2 = conn.cursor()
    cur2.execute("UPDATE wa_tenants SET ai_enabled=%s WHERE id=%s AND tenant_id=%s",
                 (new_val, wa_id, tenant_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(),
                     action="admin_toggle_number_ai",
                     tenant_id=tenant_id,
                     details={"customer_id": customer_id, "wa_id": wa_id,
                              "number": wrow["display_phone_number"], "new_ai_enabled": new_val})
    flash(f"AI replies {'turned on' if new_val else 'turned off'} for {wrow['display_phone_number']}.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


@portal_admin_bp.route("/customers/<int:customer_id>/toggle-meta-ai", methods=["POST"])
def customer_toggle_meta_ai(customer_id: int):
    """PhiXtra-admin-only switch: start mirroring this business's Store
    Information/System Instruction to Meta's own Business AI (Business Info/
    FAQ/Skills APIs). Never exposed to the business itself. NOTE: this only
    syncs knowledge — it does NOT make Meta's AI the one replying to
    customers, and does NOT turn off PhiXtra's own AI Replies switch above.
    Turning both this on and "AI Replies" off for the same business is a
    separate, deliberate decision an admin makes once Meta is confirmed
    live for that number — this switch alone does not cause that."""
    r = _require_admin("customers", "modify")
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

    cur.execute("SELECT meta_ai_enabled FROM tenants WHERE id=%s", (tenant_id,))
    trow = cur.fetchone() or {}
    new_val = not bool(trow.get("meta_ai_enabled", False))

    cur2 = conn.cursor()
    cur2.execute("UPDATE tenants SET meta_ai_enabled=%s WHERE id=%s", (new_val, tenant_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(),
                     action="admin_toggle_meta_ai",
                     tenant_id=tenant_id,
                     details={"customer_id": customer_id, "new_meta_ai_enabled": new_val})

    if new_val:
        sync_all_to_meta(tenant_id)  # push current content immediately rather than waiting for their next save
        flash("Meta Business AI sync turned on — pushing current content to Meta now.", "success")
    else:
        flash("Meta Business AI sync turned off for this business.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


@portal_admin_bp.route("/customers/<int:customer_id>/sync-meta-ai", methods=["POST"])
def customer_sync_meta_ai_now(customer_id: int):
    """Re-push this business's current content to Meta on demand, without
    waiting for their next Store Information/System Instruction save."""
    r = _require_admin("customers", "modify")
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    sync_all_to_meta(int(row["tenant_id"]))
    flash("Sync attempted — check the status below for the result.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


@portal_admin_bp.route("/customers/toggle-meta-ai-all", methods=["POST"])
def customers_toggle_meta_ai_all():
    """Bulk version of the per-business switch above: turn Meta Business AI
    sync on (or off) for every active tenant at once, then attempt an
    immediate sync for each. Each tenant's sync is independent and
    best-effort — one tenant failing (e.g. no WhatsApp connected, or Meta
    rejects the call) does not stop or roll back the others; check each
    business's own status afterward for who actually succeeded."""
    r = _require_admin("customers", "modify")
    if r: return r

    turn_on = request.form.get("enable", "1") == "1"

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("UPDATE tenants SET meta_ai_enabled=%s WHERE status != 'suspended' RETURNING id", (turn_on,))
    tenant_ids = [r["id"] for r in cur.fetchall()]
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(),
                     action="admin_toggle_meta_ai_bulk",
                     details={"enable": turn_on, "tenant_count": len(tenant_ids)})

    if turn_on:
        failures = 0
        for tid in tenant_ids:
            sync_all_to_meta(tid)
        conn2 = get_db_connection()
        cur2 = conn2.cursor()
        cur2.execute("SELECT COUNT(*) FROM tenants WHERE id = ANY(%s) AND meta_ai_last_sync_error IS NOT NULL", (tenant_ids,))
        failures = cur2.fetchone()[0]
        cur2.close(); conn2.close()
        flash(f"Meta AI sync turned on for {len(tenant_ids)} businesses. "
              f"{failures} did not sync cleanly — check each business's status.", "warning" if failures else "success")
    else:
        flash(f"Meta AI sync turned off for {len(tenant_ids)} businesses.", "success")
    return redirect(url_for("portal_admin.customers"))


@portal_admin_bp.route("/customers/<int:customer_id>/check-meta-eligibility", methods=["POST"])
def customer_check_meta_eligibility(customer_id: int):
    """Read-only: asks Meta whether this business's WhatsApp number is
    allowed to use Business Agent. Changes nothing either side."""
    r = _require_admin("customers", "view")
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    result = check_eligibility(int(row["tenant_id"]))
    if result["error"]:
        flash(f"Could not check eligibility: {result['error']}", "danger")
    elif result["eligible"]:
        flash("✅ Meta confirms this number IS eligible for Business Agent.", "success")
    else:
        flash("❌ Meta says this number is NOT currently eligible for Business Agent.", "warning")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


@portal_admin_bp.route("/customers/<int:customer_id>/go-live-meta", methods=["POST"])
def customer_go_live_meta(customer_id: int):
    """The real switch-over: Meta starts answering this business's real
    customers, and PhiXtra's own AI turns off for the same tenant in the
    same action — never both, never neither."""
    r = _require_admin("customers", "modify")
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))
    tenant_id = int(row["tenant_id"])

    ai_audience = "ALLOWLISTED_ONLY" if request.form.get("test_only") == "1" else "EVERYONE"
    result = go_live_with_meta(tenant_id, ai_audience=ai_audience)

    insert_audit_log(admin_username=_admin_user(), action="admin_go_live_meta",
                     tenant_id=tenant_id, details={"customer_id": customer_id,
                     "ai_audience": ai_audience, "ok": result["ok"], "error": result["error"]})

    if result["ok"]:
        flash(f"Meta is now live for this business ({ai_audience}). PhiXtra's own AI has been turned off for them.",
              "success")
    else:
        flash(f"Could not go live with Meta — PhiXtra's own AI was left ON. Error: {result['error']}", "danger")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


@portal_admin_bp.route("/customers/<int:customer_id>/revert-phixtra-ai", methods=["POST"])
def customer_revert_phixtra_ai(customer_id: int):
    """Undo button: tells Meta to stop responding and turns PhiXtra's own AI
    back on for this tenant, regardless of whether the Meta call succeeds."""
    r = _require_admin("customers", "modify")
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))
    tenant_id = int(row["tenant_id"])

    result = revert_to_phixtra_ai(tenant_id)
    insert_audit_log(admin_username=_admin_user(), action="admin_revert_phixtra_ai",
                     tenant_id=tenant_id, details={"customer_id": customer_id,
                     "ok": result["ok"], "meta_error": result["error"]})

    if result["error"]:
        flash(f"PhiXtra's own AI is back on. Meta may not have confirmed the switch-off: {result['error']}", "warning")
    else:
        flash("Reverted — PhiXtra's own AI is answering again, Meta has been told to stop.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE MANAGEMENT (NEW)
# Sets which plugin features are active for a specific customer's tenant.
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/customers/<int:customer_id>/set-features", methods=["POST"])
def customer_set_features(customer_id: int):
    r = _require_admin("customers", "modify")
    if r: return r

    # WooCommerce Plugin features are decided by the customer's plan now
    # (2026-09-23) — this form only saves the per-store settings below and
    # leaves every other key in tenants.features untouched.
    feat_visual_match     = request.form.get("feat_visual_match") == "on"
    visual_match_on_uncertain = (request.form.get("visual_match_on_uncertain") or "clarify").strip()
    if visual_match_on_uncertain not in ("clarify", "handoff"):
        visual_match_on_uncertain = "clarify"

    # Cart recovery sub-settings
    recovery_popup_message = (request.form.get("cart_recovery_popup_message") or "").strip()
    recovery_incentive_pct = 0
    try:
        recovery_incentive_pct = max(0, min(50, int(request.form.get("cart_recovery_incentive_pct") or 0)))
    except (ValueError, TypeError):
        recovery_incentive_pct = 0

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.tenant_id, COALESCE(p.slug, 'free') AS plan_slug
        FROM customers c
        JOIN tenants t ON t.id = c.tenant_id
        LEFT JOIN plans p ON p.id = t.plan_id
        WHERE c.id = %s
    """, (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))

    tenant_id = int(row["tenant_id"])

    cur.execute("SELECT features FROM tenants WHERE id=%s", (tenant_id,))
    _raw = (cur.fetchone() or {}).get("features")
    features = (_json.loads(_raw) if isinstance(_raw, str) and _raw else (dict(_raw) if isinstance(_raw, dict) else {}))

    if recovery_popup_message:
        features["cart_recovery_popup_message"] = recovery_popup_message
    else:
        features.pop("cart_recovery_popup_message", None)
    if recovery_incentive_pct > 0:
        features["cart_recovery_incentive_pct"] = recovery_incentive_pct
    else:
        features.pop("cart_recovery_incentive_pct", None)

    # Visual Product Match — Pro plan only, ignore the checkbox otherwise even
    # if somehow submitted (e.g. stale form from before a downgrade)
    if feat_visual_match and row["plan_slug"] == "pro":
        features["visual_product_match"] = True
        features["visual_match_on_uncertain"] = visual_match_on_uncertain
    else:
        features.pop("visual_product_match", None)
        features.pop("visual_match_on_uncertain", None)

    features_json = _json.dumps(features) if features else None
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
    r = _require_admin("customers", "modify")
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
    r = _require_admin("customers", "modify")
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
    r = _require_admin("impersonate_customer", "modify")
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
    # impersonate() sets both portal_logged_in and impersonate_customer_id
    # (portal.py:_require_login gates on both) — clear both here too, or the
    # session is left half-logged-in-as-nobody: portal_logged_in stays True
    # with no customer_id and no impersonate_customer_id behind it, which
    # makes any login-required portal page redirect to /login and /login
    # think it's already signed in, bouncing straight back — an infinite
    # redirect loop the next time this same browser opens a customer link.
    session.pop("impersonate_customer_id", None)
    session.pop("portal_logged_in", None)
    flash("Impersonation stopped.", "success")
    return redirect(url_for("portal_admin.customers"))



# ══════════════════════════════════════════════════════════════════════════════
# API KEY REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/api-keys")
def api_keys():
    r = _require_admin("api_keys", "view")
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

    from datetime import datetime as _dt
    _now = _dt.utcnow()
    for row in rows:
        row["credits_used"] = tokens_to_credits(int(row.get("tokens_used") or 0))
        # Status — must mirror the merchant-facing /api-keys logic (portal_routes.py),
        # which also checks trial expiry, not just is_active. A trial key stays
        # is_active=TRUE until the (separately scheduled) cron job deactivates it,
        # so checking is_active alone shows "Active" for trials that expired long ago.
        is_active = row.get("is_active")
        trial_expires_at = row.get("trial_expires_at")
        if is_active is None:
            row["status"] = "Unknown"
        elif int(is_active) == 0:
            row["status"] = "Revoked"
        elif row.get("key_type") == "trial" and trial_expires_at and trial_expires_at.replace(tzinfo=None) < _now:
            row["status"] = "Expired"
        elif row.get("key_type") == "trial":
            row["status"] = "Trial"
        else:
            row["status"] = "Active"

    return render_template("portal/admin_api_keys.html", keys=rows, q=q)


@portal_admin_bp.route("/api-keys/<int:key_id>/revoke", methods=["POST"])
def api_keys_revoke(key_id: int):
    r = _require_admin("api_keys", "delete")
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
    r = _require_admin("api_keys", "modify")
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
    r = _require_admin("api_keys", "create")
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
    r = _require_admin("credit_packages", "view")
    if r: return r

    if request.method == "POST":
        r = _require_admin("credit_packages", "create")
        if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        name        = (request.form.get("name")        or "").strip()
        credits     = int(request.form.get("credits")  or 0)
        price_pence = int(float(request.form.get("price_gbp") or 0) * 100)
        price_ngn   = int(float(request.form.get("price_ngn") or 0)) or None
        vat_rate    = float(request.form.get("vat_rate")   or 20.0)
        is_active = request.form.get("is_active") == "on"
        sort_order  = int(request.form.get("sort_order")   or 0)

        # Credit Packages sell extra AI-message credits, or (2026-09-25) extra
        # AI Post Designer designs, bought once — no subscriptions, no
        # features (those come from Plans).
        package_type = "design_topup" if request.form.get("kind") == "designs" else "topup"
        billing_period, features_json = None, None


        if not name or credits <= 0 or price_pence <= 0:
            flash("Name, credits and price are required.", "danger")
        else:
            cur2 = conn.cursor()
            cur2.execute("""
                INSERT INTO credit_packages (name, credits, price_pence, price_ngn, vat_rate, is_active, sort_order, features, package_type, billing_period)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (name, credits, price_pence, price_ngn, vat_rate, is_active, sort_order, features_json, package_type, billing_period))
            conn.commit()
            cur2.close()
            flash("Top-up added.", "success")
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
    r = _require_admin("credit_packages", "modify")
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT is_active FROM credit_packages WHERE id=%s", (pkg_id,))
    row = cur.fetchone() or {}
    new_val = not bool(row.get("is_active"))
    cur2 = conn.cursor()
    cur2.execute("UPDATE credit_packages SET is_active=%s WHERE id=%s", (new_val, pkg_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()
    flash("Top-up updated.", "success")
    return redirect(url_for("portal_admin.credit_packages"))


@portal_admin_bp.route("/credit-packages/<int:pkg_id>/delete", methods=["POST"])
def credit_packages_delete(pkg_id: int):
    r = _require_admin("credit_packages", "delete")
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
        flash(f"Top-up '{row.get('name')}' deleted.", "success")
    else:
        flash("Top-up not found.", "danger")
    cur.close(); conn.close()
    return redirect(url_for("portal_admin.credit_packages"))


@portal_admin_bp.route("/credit-packages/<int:pkg_id>/edit", methods=["POST"])
def credit_packages_edit(pkg_id: int):
    r = _require_admin("credit_packages", "modify")
    if r: return r

    name        = (request.form.get("name")        or "").strip()
    credits     = int(request.form.get("credits")  or 0)
    price_pence = int(float(request.form.get("price_gbp") or 0) * 100)
    price_ngn   = int(float(request.form.get("price_ngn") or 0)) or None
    vat_rate    = float(request.form.get("vat_rate")   or 20.0)
    is_active = request.form.get("is_active") == "on"
    sort_order  = int(request.form.get("sort_order")   or 0)

    # One-time top-ups only (2026-09-23) — see credit_packages().
    package_type_e = "design_topup" if request.form.get("kind") == "designs" else "topup"
    billing_period_e, features_json = None, None


    if not name or credits <= 0 or price_pence <= 0:
        flash("Name, credits and price are required.", "danger")
        return redirect(url_for("portal_admin.credit_packages"))

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE credit_packages
        SET name=%s, credits=%s, price_pence=%s, price_ngn=%s, vat_rate=%s,
            is_active=%s, sort_order=%s, features=%s,
            package_type=%s, billing_period=%s
        WHERE id=%s""",
        (name, credits, price_pence, price_ngn, vat_rate, is_active, sort_order, features_json,
         package_type_e, billing_period_e, pkg_id))
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(
        admin_username=_admin_user(),
        action="admin_edit_package",
        details={"package_id": pkg_id, "name": name, "credits": credits,
                 "price_pence": price_pence,
                 "package_type": package_type_e, "billing_period": billing_period_e},
    )
    flash(f"Top-up '{name}' updated.", "success")
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
    r = _require_admin("plugins", "view")
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "upload":
            r = _require_admin("plugins", "create")
            if r: return r
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
            r = _require_admin("plugins", "delete")
            if r: return r
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
    r = _require_admin("invoices", "view")
    if r: return r

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT i.id, i.invoice_number, i.credits, i.amount_pence, i.vat_pence,
               i.currency, i.status, i.created_at,
               (SELECT cp.package_type FROM credit_packages cp WHERE cp.id = i.package_id) AS package_type,
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
    r = _require_admin("invoices", "view")
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
    r = _require_admin("recovery_queue", "view")
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
    r = _require_owner()
    if r: return r
    username          = _admin_user()
    trial_default_days = _get_trial_default_days()
    return render_template("portal/admin_settings.html",
                           username=username,
                           trial_default_days=trial_default_days)


@portal_admin_bp.route("/onboarding-qr")
def onboarding_qr():
    """Serve the WhatsApp onboarding QR code card as a downloadable PNG."""
    r = _require_owner()
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
    r = _require_owner()
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

    from portal_routes import _password_problem
    if _password_problem(new_pw):
        flash(_password_problem(new_pw), "danger")
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
    r = _require_admin("customers", "modify")
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
    r = _require_admin("customers", "modify")
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
    r = _require_admin("customers", "modify")
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
    r = _require_admin("onboard_wa", "create")
    if r: return r

    if request.method == "POST":
        from portal_routes import provision_whatsapp_merchant, _normalise_phone
        raw_phone     = (request.form.get("phone")         or "").strip()
        business_name = (request.form.get("business_name") or "").strip()
        notes         = (request.form.get("notes")         or "").strip()

        if not raw_phone or not business_name:
            flash("Phone number and business name are both required.", "danger")
            return redirect(url_for("portal_admin.onboard_wa"))

        # Country code forced (2026-10-02): picker or +number, never guessed.
        from portal_routes import _phone_entry
        phone, _perr = _phone_entry(raw_phone, request.form.get("phone_country"), plus=True)
        if _perr or not phone:
            flash(_perr or "Please enter the WhatsApp number.", "danger")
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
    r = _require_admin("catalogue", "view")
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
    r = _require_admin("catalogue", "modify")
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
    r = _require_admin("catalogue", "delete")
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
    r = _require_admin("catalogue", "modify")
    if r: return r

    action  = request.form.get("bulk_action", "")
    ids_raw = request.form.getlist("selected_ids")
    ids     = [int(i) for i in ids_raw if i.isdigit()]

    if action == "delete":
        r = _require_admin("catalogue", "delete")
        if r: return r

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
    r = _require_admin("catalogue", "view")
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
    r = _require_admin("catalogue", "create")
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
    r = _require_admin("catalogue", "view")
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
    r = _require_admin("catalogue", "create")
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
    r = _require_admin("catalogue", "modify")
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
    r = _require_admin("catalogue", "delete")
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


def _cat_variant_types(cur, category_id: int):
    cur.execute("""
        SELECT vt.*, json_agg(
            json_build_object('id', vo.id, 'value', vo.value, 'sort_order', vo.sort_order)
            ORDER BY vo.sort_order, vo.id
        ) FILTER (WHERE vo.id IS NOT NULL) AS options
        FROM catalogue_variant_types vt
        LEFT JOIN catalogue_variant_options vo ON vo.variant_type_id = vt.id
        WHERE vt.category_id = %s
        GROUP BY vt.id
        ORDER BY vt.sort_order, vt.id
    """, (category_id,))
    return cur.fetchall()


def _product_variants(cur, product_id: int):
    cur.execute("""
        SELECT * FROM catalogue_product_variants
        WHERE product_id = %s
        ORDER BY variant_combo::text
    """, (product_id,))
    return cur.fetchall()


# ── Department helpers ────────────────────────────────────────────────────────

def _dept_get(cur, dept_id: int):
    cur.execute("SELECT * FROM catalogue_departments WHERE id=%s", (dept_id,))
    return cur.fetchone()

def _all_departments(cur):
    cur.execute("SELECT * FROM catalogue_departments ORDER BY sort_order, name")
    return cur.fetchall()

DEPT_ICONS = ["🏪","📱","💊","💄","🛒","🖨","🛋","👗","🔧","🍔","🚗","🏋","📚","🌿","🐾","🧸","⚗️","🏠","🎓","💈"]


# ── Department CRUD ───────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/departments/new", methods=["GET", "POST"])
def catalogue_department_new():
    r = _require_admin("catalogue", "create")
    if r: return r

    if request.method == "GET":
        return render_template("portal/admin_department_new.html", icons=DEPT_ICONS)

    name        = (request.form.get("name") or "").strip()
    icon        = (request.form.get("icon") or "🏪").strip()
    description = (request.form.get("description") or "").strip()

    if not name:
        flash("Department name is required.", "danger")
        return render_template("portal/admin_department_new.html", icons=DEPT_ICONS)

    slug = _slugify(name)
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """INSERT INTO catalogue_departments (name, slug, icon, description, created_by)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (name, slug, icon, description or None, _admin_user())
        )
        conn.commit()
        insert_audit_log(action="catalogue_department_create", admin_username=_admin_user(),
                         details={"name": name, "slug": slug})
        flash(f"Department '{name}' created.", "success")
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash(f"A department with the slug '{slug}' already exists.", "danger")
        cur.close(); conn.close()
        return render_template("portal/admin_department_new.html", icons=DEPT_ICONS)
    finally:
        cur.close(); conn.close()
    return redirect(url_for("portal_admin.catalogue_dashboard"))


@portal_admin_bp.route("/catalogue/departments/<int:dept_id>/edit", methods=["GET", "POST"])
def catalogue_department_edit(dept_id: int):
    r = _require_admin("catalogue", "modify")
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    dept = _dept_get(cur, dept_id)
    if not dept:
        cur.close(); conn.close()
        flash("Department not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    if request.method == "POST":
        name        = (request.form.get("name") or "").strip()
        icon        = (request.form.get("icon") or dept["icon"]).strip()
        description = (request.form.get("description") or "").strip()
        if not name:
            flash("Name is required.", "danger")
        else:
            cur.execute(
                "UPDATE catalogue_departments SET name=%s, icon=%s, description=%s WHERE id=%s",
                (name, icon, description or None, dept_id)
            )
            conn.commit()
            insert_audit_log(action="catalogue_department_edit", admin_username=_admin_user(),
                             details={"id": dept_id, "name": name})
            flash("Department updated.", "success")
        cur.close(); conn.close()
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    cur.close(); conn.close()
    return render_template("portal/admin_department_new.html", icons=DEPT_ICONS, dept=dept)


@portal_admin_bp.route("/catalogue/departments/<int:dept_id>/toggle", methods=["POST"])
def catalogue_department_toggle(dept_id: int):
    r = _require_admin("catalogue", "modify")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE catalogue_departments SET is_active = NOT is_active WHERE id=%s", (dept_id,))
    conn.commit()
    cur.close(); conn.close()
    return redirect(url_for("portal_admin.catalogue_dashboard"))


@portal_admin_bp.route("/catalogue/departments/<int:dept_id>/delete", methods=["POST"])
def catalogue_department_delete(dept_id: int):
    r = _require_admin("catalogue", "delete")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Only allow delete if no categories assigned
    cur.execute("SELECT COUNT(*) AS n FROM catalogue_categories WHERE department_id=%s", (dept_id,))
    count = (cur.fetchone() or {}).get("n", 0)
    if count > 0:
        flash(f"Cannot delete: {count} category/categories are still assigned to this department.", "danger")
    else:
        cur.execute("DELETE FROM catalogue_departments WHERE id=%s", (dept_id,))
        conn.commit()
        insert_audit_log(action="catalogue_department_delete", admin_username=_admin_user(),
                         details={"id": dept_id})
        flash("Department deleted.", "success")
    cur.close(); conn.close()
    return redirect(url_for("portal_admin.catalogue_dashboard"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue")
def catalogue_dashboard():
    r = _require_admin("catalogue", "view")
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # All departments ordered
    cur.execute("SELECT * FROM catalogue_departments ORDER BY sort_order, name")
    departments = cur.fetchall()

    # All categories with product counts and department info
    cur.execute("""
        SELECT c.*, d.name AS dept_name, COUNT(p.id) AS product_count
        FROM catalogue_categories c
        LEFT JOIN catalogue_departments d ON d.id = c.department_id
        LEFT JOIN catalogue_products p ON p.category_id = c.id
        GROUP BY c.id, d.name
        ORDER BY c.sort_order, c.name
    """)
    all_categories = cur.fetchall()

    # Group categories by department_id for template rendering
    import collections
    cats_by_dept = collections.defaultdict(list)
    ungrouped    = []
    for cat in all_categories:
        if cat["department_id"]:
            cats_by_dept[cat["department_id"]].append(cat)
        else:
            ungrouped.append(cat)

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
        departments=departments,
        cats_by_dept=dict(cats_by_dept),
        ungrouped=ungrouped,
        phones_count=phones_count,
        recent_uploads=recent_uploads,
    )


# ── Create category ───────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/new", methods=["GET", "POST"])
def catalogue_category_new():
    r = _require_admin("catalogue", "create")
    if r: return r

    ICONS = ["📱","💻","🖥","🖵","🔌","📷","🎮","📺","🎧","⌨️","🖱","🔋","📡","🖨","⌚","📻",
             "💊","💄","🛒","🛋","👗","🔧","🍔","🏋","📚","🌿"]

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    departments = _all_departments(cur)
    cur.close(); conn.close()

    if request.method == "GET":
        return render_template("portal/admin_category_new.html", icons=ICONS, departments=departments)

    name          = (request.form.get("name") or "").strip()
    icon          = (request.form.get("icon") or "📦").strip()
    description   = (request.form.get("description") or "").strip()
    dept_id_raw   = request.form.get("department_id") or ""
    department_id = int(dept_id_raw) if dept_id_raw.isdigit() else None

    if not name:
        flash("Category name is required.", "danger")
        return render_template("portal/admin_category_new.html", icons=ICONS, departments=departments)

    slug = _slugify(name)
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """INSERT INTO catalogue_categories (name, slug, icon, description, department_id, created_by)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (name, slug, icon, description or None, department_id, _admin_user())
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        insert_audit_log(action="catalogue_category_create", admin_username=_admin_user(),
                         details={"name": name, "slug": slug, "department_id": department_id})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash(f"A category with the slug '{slug}' already exists.", "danger")
        cur.close(); conn.close()
        return render_template("portal/admin_category_new.html", icons=ICONS, departments=departments)
    finally:
        cur.close(); conn.close()

    flash(f"Category '{name}' created. Now define its attributes.", "success")
    return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=new_id))


# ── Edit category ─────────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/edit", methods=["GET", "POST"])
def catalogue_category_edit(category_id: int):
    r = _require_admin("catalogue", "modify")
    if r: return r

    ICONS = ["📱","💻","🖥","🖵","🔌","📷","🎮","📺","🎧","⌨️","🖱","🔋","📡","🖨","⌚","📻",
             "💊","💄","🛒","🛋","👗","🔧","🍔","🏋","📚","🌿"]
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cat  = _cat_get(cur, category_id)
    if not cat:
        cur.close(); conn.close()
        flash("Category not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    departments = _all_departments(cur)

    if request.method == "POST":
        name          = (request.form.get("name") or "").strip()
        icon          = (request.form.get("icon") or cat["icon"]).strip()
        description   = (request.form.get("description") or "").strip()
        dept_id_raw   = request.form.get("department_id") or ""
        department_id = int(dept_id_raw) if dept_id_raw.isdigit() else None
        if not name:
            flash("Name is required.", "danger")
        else:
            try:
                cur.execute(
                    "UPDATE catalogue_categories SET name=%s, icon=%s, description=%s, department_id=%s WHERE id=%s",
                    (name, icon, description or None, department_id, category_id)
                )
                conn.commit()
                insert_audit_log(action="catalogue_category_edit", admin_username=_admin_user(),
                                 details={"id": category_id, "name": name, "department_id": department_id})
                flash("Category updated.", "success")
            except Exception as e:
                conn.rollback()
                flash(f"Error: {e}", "danger")
        cur.close(); conn.close()
        return redirect(url_for("portal_admin.catalogue_category_products", category_id=category_id))

    cur.close(); conn.close()
    return render_template("portal/admin_category_new.html", icons=ICONS, cat=cat, departments=departments)


# ── Toggle category active ────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/toggle", methods=["POST"])
def catalogue_category_toggle(category_id: int):
    r = _require_admin("catalogue", "modify")
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
    r = _require_admin("catalogue", "delete")
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
    r = _require_admin("catalogue", "view")
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

        if action in ("add",):
            r = _require_admin("catalogue", "create")
            if r: return r
        elif action == "delete":
            r = _require_admin("catalogue", "delete")
            if r: return r
        elif action in ("toggle_required", "toggle_filterable"):
            r = _require_admin("catalogue", "modify")
            if r: return r

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

    attrs         = _cat_attrs(cur, category_id)
    variant_types = _cat_variant_types(cur, category_id)

    # Load templates — prefer those matching the category's department
    conn2 = get_db_connection()
    cur2  = conn2.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur2.execute("""
        SELECT t.id, t.name, t.slug, t.is_builtin,
               jsonb_array_length(t.attributes) AS attr_count
        FROM catalogue_industry_templates t
        ORDER BY
            CASE WHEN t.department_id = %s THEN 0 ELSE 1 END,
            t.is_builtin DESC, t.name
    """, (cat.get("department_id"),))
    templates = cur2.fetchall()
    cur2.close(); conn2.close()

    cur.close(); conn.close()
    return render_template(
        "portal/admin_category_attributes.html",
        cat=cat, attrs=attrs,
        DATA_TYPES=["text", "number", "select", "boolean"],
        templates=templates,
        variant_types=variant_types,
    )


# ── Apply template to category ────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/apply-template", methods=["POST"])
def catalogue_apply_template(category_id: int):
    r = _require_admin("catalogue", "modify")
    if r: return r

    tpl_id_raw = request.form.get("template_id") or ""
    if not tpl_id_raw.isdigit():
        flash("Please select a template.", "warning")
        return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=category_id))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cat = _cat_get(cur, category_id)
    if not cat:
        cur.close(); conn.close()
        flash("Category not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    cur.execute("SELECT * FROM catalogue_industry_templates WHERE id=%s", (int(tpl_id_raw),))
    tpl = cur.fetchone()
    if not tpl:
        cur.close(); conn.close()
        flash("Template not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=category_id))

    # Get current max sort_order
    cur.execute(
        "SELECT COALESCE(MAX(sort_order),0) AS n FROM catalogue_attribute_definitions WHERE category_id=%s",
        (category_id,)
    )
    base_sort = (cur.fetchone() or {}).get("n", 0)

    # Get existing attribute keys to avoid duplicates
    cur.execute(
        "SELECT attribute_key FROM catalogue_attribute_definitions WHERE category_id=%s",
        (category_id,)
    )
    existing_keys = {r["attribute_key"] for r in cur.fetchall()}

    import json as _json
    attrs = tpl["attributes"] if isinstance(tpl["attributes"], list) else _json.loads(tpl["attributes"])
    added = 0
    for a in attrs:
        key = a.get("key", "")
        if not key or key in existing_keys:
            continue
        cur.execute("""
            INSERT INTO catalogue_attribute_definitions
                (category_id, attribute_key, attribute_label, data_type, unit,
                 is_filterable, is_required, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (category_id, attribute_key) DO NOTHING
        """, (
            category_id, key, a.get("label", key),
            a.get("data_type", "text"), a.get("unit") or None,
            bool(a.get("is_filterable", False)), bool(a.get("is_required", False)),
            base_sort + a.get("sort_order", 99)
        ))
        added += 1

    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="catalogue_apply_template", admin_username=_admin_user(),
                     details={"category_id": category_id, "template_id": int(tpl_id_raw),
                              "template_name": tpl["name"], "added": added})
    flash(f"Template '{tpl['name']}' applied — {added} attribute(s) added.", "success")
    return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=category_id))


# ── Industry templates management ─────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/templates")
def catalogue_templates():
    r = _require_admin("catalogue", "view")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT t.*, d.name AS dept_name, d.icon AS dept_icon,
               jsonb_array_length(t.attributes) AS attr_count
        FROM catalogue_industry_templates t
        LEFT JOIN catalogue_departments d ON d.id = t.department_id
        ORDER BY t.is_builtin DESC, t.name
    """)
    templates = cur.fetchall()
    cur.close(); conn.close()
    return render_template("portal/admin_templates.html", templates=templates)


@portal_admin_bp.route("/catalogue/templates/new", methods=["GET", "POST"])
def catalogue_template_new():
    r = _require_admin("catalogue", "create")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    departments = _all_departments(cur)

    if request.method == "POST":
        name       = (request.form.get("name") or "").strip()
        dept_raw   = request.form.get("department_id") or ""
        dept_id    = int(dept_raw) if dept_raw.isdigit() else None
        # Collect attribute rows from form
        import json as _json
        keys    = request.form.getlist("attr_key")
        labels  = request.form.getlist("attr_label")
        dtypes  = request.form.getlist("attr_data_type")
        units   = request.form.getlist("attr_unit")
        filts   = set(request.form.getlist("attr_filterable"))
        reqs    = set(request.form.getlist("attr_required"))
        attrs   = []
        for i, (k, l) in enumerate(zip(keys, labels)):
            k = k.strip(); l = l.strip()
            if not k or not l:
                continue
            attrs.append({
                "key": k, "label": l,
                "data_type": dtypes[i] if i < len(dtypes) else "text",
                "unit": units[i].strip() if i < len(units) else "",
                "is_filterable": str(i) in filts,
                "is_required": str(i) in reqs,
                "sort_order": i + 1,
            })
        if not name:
            flash("Template name is required.", "danger")
        elif not attrs:
            flash("Add at least one attribute.", "danger")
        else:
            slug = _slugify(name)
            try:
                cur.execute("""
                    INSERT INTO catalogue_industry_templates
                        (name, slug, department_id, attributes, is_builtin, created_by)
                    VALUES (%s,%s,%s,%s,FALSE,%s) RETURNING id
                """, (name, slug, dept_id, _json.dumps(attrs), _admin_user()))
                conn.commit()
                insert_audit_log(action="catalogue_template_create", admin_username=_admin_user(),
                                 details={"name": name})
                flash(f"Template '{name}' created.", "success")
                cur.close(); conn.close()
                return redirect(url_for("portal_admin.catalogue_templates"))
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                flash(f"A template named '{name}' already exists.", "danger")

    cur.close(); conn.close()
    return render_template("portal/admin_template_edit.html",
                           departments=departments, tpl=None,
                           DATA_TYPES=["text", "number", "select", "boolean"])


@portal_admin_bp.route("/catalogue/templates/<int:tpl_id>/edit", methods=["GET", "POST"])
def catalogue_template_edit(tpl_id: int):
    r = _require_admin("catalogue", "modify")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM catalogue_industry_templates WHERE id=%s", (tpl_id,))
    tpl = cur.fetchone()
    if not tpl:
        cur.close(); conn.close()
        flash("Template not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_templates"))
    departments = _all_departments(cur)

    if request.method == "POST":
        if tpl["is_builtin"]:
            cur.close(); conn.close()
            flash("Built-in templates cannot be edited.", "danger")
            return redirect(url_for("portal_admin.catalogue_templates"))
        import json as _json
        name     = (request.form.get("name") or "").strip()
        dept_raw = request.form.get("department_id") or ""
        dept_id  = int(dept_raw) if dept_raw.isdigit() else None
        keys    = request.form.getlist("attr_key")
        labels  = request.form.getlist("attr_label")
        dtypes  = request.form.getlist("attr_data_type")
        units   = request.form.getlist("attr_unit")
        filts   = set(request.form.getlist("attr_filterable"))
        reqs    = set(request.form.getlist("attr_required"))
        attrs   = []
        for i, (k, l) in enumerate(zip(keys, labels)):
            k = k.strip(); l = l.strip()
            if not k or not l:
                continue
            attrs.append({
                "key": k, "label": l,
                "data_type": dtypes[i] if i < len(dtypes) else "text",
                "unit": units[i].strip() if i < len(units) else "",
                "is_filterable": str(i) in filts,
                "is_required": str(i) in reqs,
                "sort_order": i + 1,
            })
        if not name:
            flash("Template name is required.", "danger")
        elif not attrs:
            flash("Add at least one attribute.", "danger")
        else:
            cur.execute("""
                UPDATE catalogue_industry_templates
                SET name=%s, department_id=%s, attributes=%s
                WHERE id=%s
            """, (name, dept_id, _json.dumps(attrs), tpl_id))
            conn.commit()
            insert_audit_log(action="catalogue_template_edit", admin_username=_admin_user(),
                             details={"id": tpl_id, "name": name})
            flash(f"Template '{name}' updated.", "success")
            cur.close(); conn.close()
            return redirect(url_for("portal_admin.catalogue_templates"))

    cur.close(); conn.close()
    return render_template("portal/admin_template_edit.html",
                           departments=departments, tpl=tpl,
                           DATA_TYPES=["text", "number", "select", "boolean"])


@portal_admin_bp.route("/catalogue/templates/<int:tpl_id>/delete", methods=["POST"])
def catalogue_template_delete(tpl_id: int):
    r = _require_admin("catalogue", "delete")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT name, is_builtin FROM catalogue_industry_templates WHERE id=%s", (tpl_id,))
    tpl = cur.fetchone()
    if tpl and tpl["is_builtin"]:
        flash("Built-in templates cannot be deleted.", "danger")
    elif tpl:
        cur.execute("DELETE FROM catalogue_industry_templates WHERE id=%s", (tpl_id,))
        conn.commit()
        flash(f"Template '{tpl['name']}' deleted.", "success")
    cur.close(); conn.close()
    return redirect(url_for("portal_admin.catalogue_templates"))


# ══════════════════════════════════════════════════════════════════════════════
# VARIANT TYPES & OPTIONS (per category)
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/variants/add", methods=["POST"])
def catalogue_variant_type_add(category_id: int):
    r = _require_admin("catalogue", "create")
    if r: return r
    name = (request.form.get("variant_name") or "").strip()
    if not name:
        flash("Variant type name is required.", "danger")
        return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=category_id))
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO catalogue_variant_types (category_id, name, sort_order)
            VALUES (%s, %s, (SELECT COALESCE(MAX(sort_order),0)+1 FROM catalogue_variant_types WHERE category_id=%s))
        """, (category_id, name, category_id))
        conn.commit()
        flash(f"Variant type '{name}' added.", "success")
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash(f"Variant type '{name}' already exists in this category.", "danger")
    finally:
        cur.close(); conn.close()
    return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=category_id))


@portal_admin_bp.route("/catalogue/categories/<int:category_id>/variants/<int:type_id>/delete", methods=["POST"])
def catalogue_variant_type_delete(category_id: int, type_id: int):
    r = _require_admin("catalogue", "delete")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM catalogue_variant_types WHERE id=%s AND category_id=%s", (type_id, category_id))
    # Purge saved variant rows for all products in this category — JSONB combos are now stale
    cur.execute("""
        DELETE FROM catalogue_product_variants
        WHERE product_id IN (SELECT id FROM catalogue_products WHERE category_id=%s)
    """, (category_id,))
    conn.commit()
    cur.close(); conn.close()
    flash("Variant type and all its options deleted. Saved variant rows cleared — please re-save product variants.", "success")
    return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=category_id))


@portal_admin_bp.route("/catalogue/categories/<int:category_id>/variants/<int:type_id>/options/add", methods=["POST"])
def catalogue_variant_option_add(category_id: int, type_id: int):
    r = _require_admin("catalogue", "create")
    if r: return r
    value = (request.form.get("option_value") or "").strip()
    if not value:
        flash("Option value is required.", "danger")
        return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=category_id))
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO catalogue_variant_options (variant_type_id, value, sort_order)
            VALUES (%s, %s, (SELECT COALESCE(MAX(sort_order),0)+1 FROM catalogue_variant_options WHERE variant_type_id=%s))
        """, (type_id, value, type_id))
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash(f"Option '{value}' already exists.", "danger")
    finally:
        cur.close(); conn.close()
    return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=category_id))


@portal_admin_bp.route("/catalogue/categories/<int:category_id>/variants/<int:type_id>/options/<int:opt_id>/delete",
                        methods=["POST"])
def catalogue_variant_option_delete(category_id: int, type_id: int, opt_id: int):
    r = _require_admin("catalogue", "delete")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM catalogue_variant_options WHERE id=%s AND variant_type_id=%s", (opt_id, type_id))
    conn.commit()
    cur.close(); conn.close()
    return redirect(url_for("portal_admin.catalogue_category_attributes", category_id=category_id))


# ── Product variant matrix save ───────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/products/<int:product_id>/variants",
                        methods=["POST"])
def catalogue_product_variants_save(category_id: int, product_id: int):
    r = _require_admin("catalogue", "modify")
    if r: return r

    import json as _json, itertools as _it

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Verify product belongs to category
    cur.execute("SELECT id FROM catalogue_products WHERE id=%s AND category_id=%s", (product_id, category_id))
    if not cur.fetchone():
        cur.close(); conn.close()
        flash("Product not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_category_products", category_id=category_id))

    # Collect variant rows from form: indices 0..N
    indices = sorted(set(
        k.split("_", 2)[2] for k in request.form.keys()
        if k.startswith("v_combo_")
    ), key=lambda x: int(x) if x.isdigit() else 0)

    # Replace the full variant set for this product
    cur.execute("DELETE FROM catalogue_product_variants WHERE product_id=%s", (product_id,))

    saved = 0
    for idx in indices:
        combo_raw = request.form.get(f"v_combo_{idx}", "{}")
        sku_val   = (request.form.get(f"v_sku_{idx}") or "").strip() or None
        price_raw = (request.form.get(f"v_price_{idx}") or "0").strip()
        is_active = request.form.get(f"v_active_{idx}") == "1"
        stock     = request.form.get(f"v_stock_{idx}") or "in_stock"
        try:
            combo = _json.loads(combo_raw)
            price = float(price_raw) if price_raw else 0.0
        except (ValueError, TypeError):
            continue

        cur.execute("SAVEPOINT sp_var")
        try:
            cur.execute("""
                INSERT INTO catalogue_product_variants
                    (product_id, sku, price_modifier, stock_status, is_active, variant_combo)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (product_id, sku_val, price, stock, is_active, _json.dumps(combo)))
            cur.execute("RELEASE SAVEPOINT sp_var")
            saved += 1
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_var")

    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="catalogue_variants_save", admin_username=_admin_user(),
                     details={"product_id": product_id, "saved": saved})
    flash(f"{saved} variant row(s) saved.", "success")
    return redirect(url_for("portal_admin.catalogue_product_edit",
                            category_id=category_id, product_id=product_id))


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCTS FOR A CATEGORY
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/products")
def catalogue_category_products(category_id: int):
    r = _require_admin("catalogue", "view")
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

    # Department slug for extended column display
    dept_slug = None
    if cat.get("department_id"):
        cur.execute("SELECT slug FROM catalogue_departments WHERE id=%s", (cat["department_id"],))
        dept_row = cur.fetchone()
        dept_slug = (dept_row or {}).get("slug")

    cur.close(); conn.close()
    return render_template(
        "portal/admin_category_products.html",
        cat=cat, attrs=attrs, products=products, brands=brands,
        total=total, page=page, pages=pages, per_page=per_page,
        q=q, brand_f=brand_f, status_f=status_f,
        dept_slug=dept_slug,
    )


# ── Edit a single product ─────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/products/<int:product_id>/edit",
                        methods=["GET", "POST"])
def catalogue_product_edit(category_id: int, product_id: int):
    r = _require_admin("catalogue", "modify")
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

    # Load department slug for field visibility
    dept_slug = None
    if cat.get("department_id"):
        cur.execute("SELECT slug FROM catalogue_departments WHERE id=%s", (cat["department_id"],))
        dept_row = cur.fetchone()
        dept_slug = (dept_row or {}).get("slug")

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

    EXTENDED_FIELDS = ["barcode", "unit_of_measure", "weight_value", "weight_unit",
                       "shelf_life_days", "requires_rxn", "regulatory_ref", "dimensions_cm"]

    if request.method == "POST":
        brand        = (request.form.get("brand") or "").strip() or None
        model_name   = (request.form.get("model_name") or "").strip()
        model_number = (request.form.get("model_number") or "").strip() or None
        sku          = (request.form.get("sku") or "").strip() or None
        description  = (request.form.get("description") or "").strip() or None
        image_url    = (request.form.get("image_url") or "").strip() or None
        is_active    = request.form.get("is_active") == "1"

        # Extended fields
        barcode        = (request.form.get("barcode") or "").strip() or None
        unit_of_measure= (request.form.get("unit_of_measure") or "").strip() or None
        weight_value_s = (request.form.get("weight_value") or "").strip()
        try:
            weight_value = float(weight_value_s) if weight_value_s else None
        except ValueError:
            weight_value = None
        weight_unit    = (request.form.get("weight_unit") or "").strip() or None
        shelf_life_s   = (request.form.get("shelf_life_days") or "").strip()
        shelf_life_days= int(shelf_life_s) if shelf_life_s.isdigit() else None
        requires_rxn_s = request.form.get("requires_rxn") or ""
        requires_rxn   = True if requires_rxn_s == "1" else (False if requires_rxn_s == "0" else None)
        regulatory_ref = (request.form.get("regulatory_ref") or "").strip() or None
        dimensions_cm  = (request.form.get("dimensions_cm") or "").strip() or None

        if not model_name:
            flash("Model name is required.", "danger")
            cur.close(); conn.close()
            return render_template(
                "portal/admin_category_product_edit.html",
                cat=cat, product=product, attrs=attrs, dept_slug=dept_slug,
                attr_vals={a["attribute_key"]: request.form.get(f"attr_{a['attribute_key']}") for a in attrs},
            )

        try:
            cur.execute(
                """UPDATE catalogue_products
                   SET brand=%s, model_name=%s, model_number=%s, sku=%s,
                       description=%s, image_url=%s, is_active=%s,
                       barcode=%s, unit_of_measure=%s, weight_value=%s, weight_unit=%s,
                       shelf_life_days=%s, requires_rxn=%s, regulatory_ref=%s,
                       dimensions_cm=%s, updated_at=NOW()
                   WHERE id=%s""",
                (brand, model_name, model_number, sku, description, image_url, is_active,
                 barcode, unit_of_measure, weight_value, weight_unit,
                 shelf_life_days, requires_rxn, regulatory_ref, dimensions_cm,
                 product_id)
            )
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

    variant_types    = _cat_variant_types(cur, category_id)
    product_variants = _product_variants(cur, product_id)
    cur.close(); conn.close()

    # Build cartesian matrix from variant types × options
    import itertools as _it, json as _json
    existing_by_combo = {
        _json.dumps(dict(sorted(v["variant_combo"].items())), sort_keys=True): v
        for v in product_variants
        if v["variant_combo"]
    }

    matrix = []
    if variant_types:
        type_options = [(vt["name"], vt["options"] or []) for vt in variant_types]
        all_names    = [t[0] for t in type_options]
        all_opts     = [[(o["value"]) for o in t[1]] for t in type_options]
        if all(all_opts):
            for combo_vals in _it.product(*all_opts):
                combo = dict(zip(all_names, combo_vals))
                key   = _json.dumps(combo, sort_keys=True)
                existing = existing_by_combo.get(key)
                matrix.append({
                    "combo":      combo,
                    "combo_json": key,
                    "label":      " / ".join(combo_vals),
                    "sku":        existing["sku"] if existing else "",
                    "price":      float(existing["price_modifier"]) if existing else 0.0,
                    "stock":      existing["stock_status"] if existing else "in_stock",
                    "is_active":  existing["is_active"] if existing else True,
                })

    return render_template(
        "portal/admin_category_product_edit.html",
        cat=cat, product=product, attrs=attrs, attr_vals=attr_vals,
        dept_slug=dept_slug,
        variant_types=variant_types,
        matrix=matrix,
    )


# ── Delete a product ──────────────────────────────────────────────────────────

@portal_admin_bp.route("/catalogue/categories/<int:category_id>/products/<int:product_id>/delete",
                        methods=["POST"])
def catalogue_product_delete(category_id: int, product_id: int):
    r = _require_admin("catalogue", "delete")
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
    r = _require_admin("catalogue", "modify")
    if r: return r

    ids_raw = request.form.getlist("ids")
    action  = request.form.get("bulk_action", "")
    ids     = [int(x) for x in ids_raw if x.isdigit()]

    if action == "delete":
        r = _require_admin("catalogue", "delete")
        if r: return r

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
    r = _require_admin("catalogue", "view")
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cat  = _cat_get(cur, category_id)
    if not cat:
        cur.close(); conn.close()
        flash("Category not found.", "danger")
        return redirect(url_for("portal_admin.catalogue_dashboard"))

    attrs = _cat_attrs(cur, category_id)

    # Determine department slug for extended field inclusion
    dept_slug = None
    if cat.get("department_id"):
        cur.execute("SELECT slug FROM catalogue_departments WHERE id=%s", (cat["department_id"],))
        dept_row = cur.fetchone()
        dept_slug = (dept_row or {}).get("slug")

    cur.close(); conn.close()

    base_cols = ["brand", "model_name", "model_number", "sku", "description", "image_url"]

    # Extended cols selected by department
    _ext_map = {
        "pharmacy":    ["barcode", "unit_of_measure", "shelf_life_days", "regulatory_ref", "requires_rxn"],
        "supermarket": ["barcode", "unit_of_measure", "weight_value", "weight_unit", "shelf_life_days", "regulatory_ref"],
        "beauty":      ["barcode", "unit_of_measure", "weight_value", "weight_unit", "shelf_life_days", "regulatory_ref"],
        "fashion":     ["weight_value", "weight_unit"],
        "furniture":   ["dimensions_cm", "weight_value", "weight_unit"],
        "office":      ["barcode", "dimensions_cm", "weight_value", "weight_unit"],
        "electronics": ["barcode", "dimensions_cm", "weight_value", "weight_unit"],
    }
    ext_cols  = _ext_map.get(dept_slug, []) if dept_slug else []
    attr_cols = [a["attribute_key"] for a in attrs]
    all_cols  = base_cols + ext_cols + attr_cols

    example = {
        "brand": "Brand Name",
        "model_name": "Product Name",
        "model_number": "Model/Part No.",
        "sku": "SKU-001",
        "description": "Short product description",
        "image_url": "https://example.com/image.jpg",
    }
    for col in ext_cols:
        hints = {
            "barcode": "6001234567890", "unit_of_measure": "each|kg|litre|ml|pack",
            "weight_value": "1.5", "weight_unit": "kg|g|lb",
            "shelf_life_days": "730", "regulatory_ref": "A1-1234",
            "requires_rxn": "Yes|No", "dimensions_cm": "120x60x75",
        }
        example[col] = hints.get(col, "")
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
    r = _require_admin("catalogue", "view")
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
    ext_cols  = ["barcode", "unit_of_measure", "weight_value", "weight_unit",
                 "shelf_life_days", "requires_rxn", "regulatory_ref", "dimensions_cm"]
    attr_cols = [a["attribute_key"] for a in attrs]
    all_cols  = base_cols + ext_cols + attr_cols

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_cols, extrasaction="ignore")
    writer.writeheader()
    for p in products:
        row = {c: p.get(c, "") for c in base_cols + ext_cols}
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
    r = _require_admin("catalogue", "create")
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

        # Extended fields from CSV
        barcode         = str(row.get("barcode") or "").strip() or None
        unit_of_measure = str(row.get("unit_of_measure") or "").strip() or None
        wv              = str(row.get("weight_value") or "").strip()
        try:
            weight_value = float(wv) if wv else None
        except ValueError:
            weight_value = None
        weight_unit     = str(row.get("weight_unit") or "").strip() or None
        sl              = str(row.get("shelf_life_days") or "").strip()
        shelf_life_days = int(sl) if sl.isdigit() else None
        rxn_s           = str(row.get("requires_rxn") or "").strip().lower()
        requires_rxn    = True if rxn_s in ("yes","true","1") else (False if rxn_s in ("no","false","0") else None)
        regulatory_ref  = str(row.get("regulatory_ref") or "").strip() or None
        dimensions_cm   = str(row.get("dimensions_cm") or "").strip() or None

        try:
            cur.execute("SAVEPOINT sp_row")
            if on_conflict == "update" and sku_val:
                cur.execute(
                    """INSERT INTO catalogue_products
                       (category_id, brand, model_name, model_number, sku, description, image_url,
                        barcode, unit_of_measure, weight_value, weight_unit,
                        shelf_life_days, requires_rxn, regulatory_ref, dimensions_cm)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (sku) DO UPDATE SET
                         brand=EXCLUDED.brand, model_name=EXCLUDED.model_name,
                         model_number=EXCLUDED.model_number, description=EXCLUDED.description,
                         image_url=EXCLUDED.image_url,
                         barcode=EXCLUDED.barcode, unit_of_measure=EXCLUDED.unit_of_measure,
                         weight_value=EXCLUDED.weight_value, weight_unit=EXCLUDED.weight_unit,
                         shelf_life_days=EXCLUDED.shelf_life_days, requires_rxn=EXCLUDED.requires_rxn,
                         regulatory_ref=EXCLUDED.regulatory_ref, dimensions_cm=EXCLUDED.dimensions_cm,
                         updated_at=NOW()
                       RETURNING id, xmax""",
                    (category_id, brand, model_name, model_number, sku_val, description, image_url,
                     barcode, unit_of_measure, weight_value, weight_unit,
                     shelf_life_days, requires_rxn, regulatory_ref, dimensions_cm)
                )
            else:
                cur.execute(
                    """INSERT INTO catalogue_products
                       (category_id, brand, model_name, model_number, sku, description, image_url,
                        barcode, unit_of_measure, weight_value, weight_unit,
                        shelf_life_days, requires_rxn, regulatory_ref, dimensions_cm)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING id, xmax""",
                    (category_id, brand, model_name, model_number, sku_val, description, image_url,
                     barcode, unit_of_measure, weight_value, weight_unit,
                     shelf_life_days, requires_rxn, regulatory_ref, dimensions_cm)
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
# MODULES — read-only reference of every module/feature in PLAN_FEATURE_CATALOG.
# Not editable here on purpose: each key is a literal string a route or
# template checks for elsewhere in the codebase (plan gating today; the
# future Team Role permission system next) — adding/renaming one here with no
# matching code change would either do nothing or silently break a real page.
# This page always reads live from the same dict the app itself enforces
# against, so it can never drift the way a hand-maintained reference could.
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/modules")
def admin_modules_catalog():
    r = _require_admin("modules", "view")
    if r: return r

    module_count = len(PLAN_FEATURE_CATALOG)
    feature_count = sum(len(feats) for feats in PLAN_FEATURE_CATALOG.values())
    legacy_count = sum(
        1 for feats in PLAN_FEATURE_CATALOG.values()
        for key, _ in feats if key.startswith("legacy:")
    )

    # Features-vs-Roles check (update policy, see feature_access.py) — run
    # live on every visit so the red box reflects the code actually running.
    granted_keys = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT feature_key FROM plan_feature_grants")
        granted_keys = {r[0] for r in cur.fetchall()}
        cur.close(); conn.close()
    except Exception as e:
        print("⚠️ admin_modules_catalog plan grants error:", e)
    if granted_keys is not None:
        granted_keys |= PLATFORM_ONLY_FEATURE_KEYS   # never sold through plans (Accounts)
    access_problems = check_feature_access(
        current_app, PLAN_FEATURE_CATALOG, ROLE_FORM_GRID, PLAN_ONLY_FEATURE_KEYS, granted_keys,
        code_paths=PLAN_LOCK_CODE_PATHS,
    )

    return render_template(
        "portal/admin_modules.html",
        feature_catalog=PLAN_FEATURE_CATALOG,
        module_count=module_count,
        feature_count=feature_count,
        legacy_count=legacy_count,
        access_problems=access_problems,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PLANS MANAGEMENT — admin view/assign plans to tenants
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/plans")
def admin_plans():
    r = _require_admin("plans", "view")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM plans ORDER BY sort_order")
    plans = cur.fetchall() or []

    # Features-enabled summary for the plan list (replaces the old flat
    # CRM/Custom AI/Integrations/Visual Match columns, which only ever showed
    # 4 of the now-21 real catalog items). Counts new-style grants from
    # plan_feature_grants plus however many legacy feat_* flags are TRUE.
    _total_catalog_items = len(_ALL_CATALOG_ITEMS)
    _legacy_cols = [key.split(":", 1)[1] for key, _label in _ALL_CATALOG_ITEMS if key.startswith("legacy:")]
    # Every tenant counts here, cancelled ones too — they still point at the
    # plan, so a delete has to move them as well.
    cur.execute("SELECT plan_id, COUNT(*) AS n FROM tenants WHERE plan_id IS NOT NULL GROUP BY plan_id")
    _tenant_counts = {row["plan_id"]: row["n"] for row in (cur.fetchall() or [])}
    for p in plans:
        p["tenant_count"] = _tenant_counts.get(p["id"], 0)
        p["is_system"] = p["slug"] in SYSTEM_PLAN_SLUGS
        p["system_reason"] = SYSTEM_PLAN_SLUGS.get(p["slug"], "")
    cur.execute("SELECT plan_id, COUNT(*) AS n FROM plan_feature_grants GROUP BY plan_id")
    _grant_counts = {row["plan_id"]: row["n"] for row in (cur.fetchall() or [])}
    for p in plans:
        legacy_on = sum(1 for col in _legacy_cols if p.get(col))
        p["features_enabled_count"] = _grant_counts.get(p["id"], 0) + legacy_on
        p["features_total_count"] = _total_catalog_items

    cur.execute("""
        SELECT t.id AS tenant_id, t.name AS business_name,
               t.billing_cycle, t.plan_period_start, t.dual_agent_grandfathered,
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
                           plans=plans, tenants=tenants,
                           can_delete_plans=_is_owner() or bool(
                               session.get("portal_admin_permissions", {}).get("plans", {}).get("delete")))


# Flattened (feature_key, label) pairs across every group in the catalog —
# used to walk form fields without caring which group a key belongs to.
# Platform-only modules (Accounts) are never sold, so the Plan editor leaves them out.
_ALL_CATALOG_ITEMS = [item for _group, items in PLAN_EDITOR_CATALOG.items() for item in items]


def _plan_form_to_dict(form) -> dict:
    def _int(name, default=0):
        raw = (form.get(name) or "").strip()
        if raw == "":
            return default
        return int(raw)

    def _num(name, default=0):
        raw = (form.get(name) or "").strip()
        if raw == "":
            return default
        return float(raw)

    data = {
        "slug":                (form.get("slug") or "").strip().lower().replace(" ", "_"),
        "name":                (form.get("name") or "").strip(),
        "price_ngn":           _int("price_ngn", 0),
        "price_usd":           _num("price_usd", 0),
        "price_dollars":       _num("price_dollars", 0),
        "ai_messages_limit":   _int("ai_messages_limit", 100),
        "ai_designs_limit":    max(0, _int("ai_designs_limit", 0)),
        "ai_agents_limit":     _int("ai_agents_limit", 1),
        "broadcasts_limit":    _int("broadcasts_limit", 0),
        "products_limit":      _int("products_limit", 50),
        "data_sources_limit":  _int("data_sources_limit", 1),
        "staff_limit":         _int("staff_limit", 0),
        "overage_per_msg_ngn": _num("overage_per_msg_ngn", 10),
        "overage_per_msg_usd": _num("overage_per_msg_usd", 0.006),
        "annual_discount_pct": _int("annual_discount_pct", 5),
        "sort_order":          _int("sort_order", 0),
        "is_active":           form.get("is_active") == "on",
        "is_custom":           form.get("is_custom") == "on",
        "channel_mode":        form.get("channel_mode") if form.get("channel_mode") in ("whatsapp", "woocommerce", "dual", "both") else "whatsapp",
        "parent_plan_id":      int(form["parent_plan_id"]) if (form.get("parent_plan_id") or "").strip() else None,
        # Shown on the merchant's plan card only (2026-10-02).
        "feat_integrations":   form.get("feat_integrations") == "on",
        "is_recommended":      form.get("is_recommended") == "on",
        "support_line":        (form.get("support_line") or "").strip()[:120] or None,
    }
    # Legacy feat_* columns are still the real gate for WhatsApp Campaigns /
    # Email Campaigns / Checkout / Custom AI / Visual Match — see
    # PLAN_FEATURE_CATALOG's docstring in portal_routes.py.
    for feature_key, _label in _ALL_CATALOG_ITEMS:
        if feature_key.startswith("legacy:"):
            col = feature_key.split(":", 1)[1]
            data[col] = form.get(feature_key) == "on"
    return data


def _granted_feature_keys(form) -> list:
    """New-style (non-legacy) catalog keys checked on the submitted form —
    what gets written into plan_feature_grants for this plan."""
    return [key for key, _label in _ALL_CATALOG_ITEMS
            if not key.startswith("legacy:") and form.get(key) == "on"]


def _save_plan_feature_grants(conn, plan_id: int, granted_keys: list) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM plan_feature_grants WHERE plan_id=%s", (plan_id,))
    for key in granted_keys:
        cur.execute(
            "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (plan_id, key),
        )
    conn.commit()
    cur.close()


@portal_admin_bp.route("/plans/new", methods=["GET", "POST"])
def admin_plans_new():
    r = _require_admin("plans", "modify")
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, slug FROM plans WHERE channel_mode != 'dual' ORDER BY sort_order")
    parent_candidates = cur.fetchall() or []

    if request.method == "POST":
        data = _plan_form_to_dict(request.form)
        if not data["slug"] or not data["name"]:
            flash("Plan name and slug are required.", "danger")
            cur.close(); conn.close()
            return render_template("portal/admin_plan_form.html", plan=data,
                                    parent_candidates=parent_candidates,
                                    feature_catalog=PLAN_EDITOR_CATALOG,
                                    granted_keys=_granted_feature_keys(request.form), is_new=True)
        try:
            cols = list(data.keys())
            cur2 = conn.cursor()
            cur2.execute(
                f"INSERT INTO plans ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                [data[c] for c in cols],
            )
            new_id = cur2.fetchone()[0]
            conn.commit()
            cur2.close()
            _save_plan_feature_grants(conn, new_id, _granted_feature_keys(request.form))
            cur.close(); conn.close()
            insert_audit_log(action="plan_create", admin_username=_admin_user(),
                             details={"plan_id": new_id, "slug": data["slug"]})
            flash(f"Plan \"{data['name']}\" created.", "success")
            return redirect(url_for("portal_admin.admin_plans"))
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            cur.close(); conn.close()
            flash(f"A plan with slug \"{data['slug']}\" already exists — choose a different slug.", "danger")
            return render_template("portal/admin_plan_form.html", plan=data,
                                    parent_candidates=parent_candidates,
                                    feature_catalog=PLAN_EDITOR_CATALOG,
                                    granted_keys=_granted_feature_keys(request.form), is_new=True)

    cur.close(); conn.close()
    blank = {"slug": "", "name": "", "price_ngn": 0, "price_usd": 0, "price_dollars": 0,
             "ai_messages_limit": 100, "ai_designs_limit": 0,
             "ai_agents_limit": 1, "broadcasts_limit": 0,
             "products_limit": 50, "data_sources_limit": 1, "staff_limit": 0,
             "overage_per_msg_ngn": 10, "overage_per_msg_usd": 0.006,
             "annual_discount_pct": 5, "sort_order": 0, "is_active": True,
             "is_custom": False, "channel_mode": "whatsapp", "parent_plan_id": None,
             "feat_integrations": False, "is_recommended": False, "support_line": ""}
    for feature_key, _label in _ALL_CATALOG_ITEMS:
        if feature_key.startswith("legacy:"):
            blank[feature_key.split(":", 1)[1]] = False
    return render_template("portal/admin_plan_form.html", plan=blank,
                            parent_candidates=parent_candidates,
                            feature_catalog=PLAN_EDITOR_CATALOG,
                            granted_keys=set(), is_new=True)


@portal_admin_bp.route("/plans/<int:plan_id>/edit", methods=["GET", "POST"])
def admin_plans_edit(plan_id: int):
    r = _require_admin("plans", "modify")
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, slug FROM plans WHERE channel_mode != 'dual' AND id != %s ORDER BY sort_order", (plan_id,))
    parent_candidates = cur.fetchall() or []

    if request.method == "POST":
        data = _plan_form_to_dict(request.form)
        if not data["slug"] or not data["name"]:
            flash("Plan name and slug are required.", "danger")
            cur.close(); conn.close()
            return render_template("portal/admin_plan_form.html", plan=dict(data, id=plan_id),
                                    parent_candidates=parent_candidates,
                                    feature_catalog=PLAN_EDITOR_CATALOG,
                                    granted_keys=_granted_feature_keys(request.form), is_new=False)
        try:
            cur2 = conn.cursor()
            set_clause = ", ".join(f"{c}=%s" for c in data.keys())
            cur2.execute(
                f"UPDATE plans SET {set_clause} WHERE id=%s",
                [*data.values(), plan_id],
            )
            conn.commit()
            cur2.close()
            _save_plan_feature_grants(conn, plan_id, _granted_feature_keys(request.form))
            cur.close(); conn.close()
            insert_audit_log(action="plan_update", admin_username=_admin_user(),
                             details={"plan_id": plan_id, "slug": data["slug"]})
            flash(f"Plan \"{data['name']}\" updated.", "success")
            return redirect(url_for("portal_admin.admin_plans"))
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            cur.close(); conn.close()
            flash(f"A plan with slug \"{data['slug']}\" already exists — choose a different slug.", "danger")
            return render_template("portal/admin_plan_form.html", plan=dict(data, id=plan_id),
                                    parent_candidates=parent_candidates,
                                    feature_catalog=PLAN_EDITOR_CATALOG,
                                    granted_keys=_granted_feature_keys(request.form), is_new=False)

    cur.execute("SELECT * FROM plans WHERE id=%s", (plan_id,))
    plan = cur.fetchone()
    cur.execute("SELECT feature_key FROM plan_feature_grants WHERE plan_id=%s", (plan_id,))
    granted_keys = {row["feature_key"] for row in (cur.fetchall() or [])}
    cur.close(); conn.close()
    if not plan:
        flash("Plan not found.", "danger")
        return redirect(url_for("portal_admin.admin_plans"))
    return render_template("portal/admin_plan_form.html", plan=plan,
                            parent_candidates=parent_candidates,
                            feature_catalog=PLAN_EDITOR_CATALOG,
                            granted_keys=granted_keys, is_new=False,
                            can_delete_plan=plan["slug"] not in SYSTEM_PLAN_SLUGS and (_is_owner() or bool(
                                session.get("portal_admin_permissions", {}).get("plans", {}).get("delete"))))


# Plans the system itself relies on (wa_plan_reset.py, portal_routes.py):
# every sign-up (WhatsApp and website) starts on 'connect', the only free
# plan since 2026-10-06, and drops back to it when a trial or plan ends; the
# trial plans are granted from here. Deleting any of them breaks those steps.
SYSTEM_PLAN_SLUGS = {"connect":         "The only free plan: every sign-up starts here, and every trial and plan ends here",
                     "pro":             "The 2-week WhatsApp AI trial uses this plan",
                     "web_enterprise":  "The 2-week website AI trial uses this plan",
                     "enterprise_dual": "The 2-week AI trial for WhatsApp and website businesses uses this plan"}


def _trim_staff_to_connect_seats_sql(cur, tenant_ids) -> None:
    """Same rule as portal_routes._trim_staff_to_connect_seats: back on
    PhiXtra Connect, only the longest-serving staff up to its seats stay
    active; the rest are switched off, never deleted."""
    cur.execute("""
        UPDATE team_members tm SET is_active = FALSE
          FROM (SELECT m.id,
                       ROW_NUMBER() OVER (PARTITION BY m.tenant_id
                                          ORDER BY m.created_at NULLS LAST, m.id) AS rn,
                       COALESCE(p.staff_limit, 0) AS lim
                  FROM team_members m
                  JOIN tenants t ON t.id = m.tenant_id
                  JOIN plans   p ON p.id = t.plan_id
                 WHERE m.is_active AND t.id = ANY(%s)
                   AND p.slug IN ('connect', 'web_free')) r
         WHERE tm.id = r.id AND r.rn > r.lim
        RETURNING tm.tenant_id, tm.id
    """, (list(tenant_ids),))


def _sync_wa_ai_to_plan_sql(cur, where_sql: str, params) -> None:
    """Same rule as portal_routes._sync_wa_ai_to_plan: the AI is off on the
    free AI-off plans (PhiXtra Connect, Website Free) and on for any other
    plan. Older website businesses (no signup_channel) are not touched."""
    cur.execute(f"""
        UPDATE tenants t
           SET ai_enabled = NOT (p.slug IN ('connect', 'web_free'))
          FROM plans p
         WHERE p.id = t.plan_id
           AND (t.source_type = 'whatsapp' OR t.signup_channel IS NOT NULL) AND {where_sql}
    """, params)
PLAN_DELETE_BACKUP_DIR = "/root/backups/plan_deletes"


@portal_admin_bp.route("/plans/<int:plan_id>/delete", methods=["POST"])
def admin_plans_delete(plan_id: int):
    """Delete a plan. Any merchants on it are first moved to the plan the
    admin picked, all in one transaction, with a JSON backup written first."""
    r = _require_admin("plans", "delete")
    if r: return r
    import json as _json, os as _os
    from datetime import datetime as _dt
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM plans WHERE id=%s", (plan_id,))
        plan = cur.fetchone()
        if not plan:
            flash("Plan not found.", "danger")
            return redirect(url_for("portal_admin.admin_plans"))
        if plan["slug"] in SYSTEM_PLAN_SLUGS:
            flash(f"\"{plan['name']}\" can't be deleted — {SYSTEM_PLAN_SLUGS[plan['slug']].lower()}.", "danger")
            return redirect(url_for("portal_admin.admin_plans"))

        cur.execute("SELECT id, name FROM tenants WHERE plan_id=%s ORDER BY id", (plan_id,))
        moved = cur.fetchall() or []
        cur.execute("SELECT COUNT(*) AS n FROM plan_subscriptions WHERE plan_id=%s", (plan_id,))
        if (cur.fetchone() or {}).get("n"):
            flash(f"\"{plan['name']}\" has subscription payment records, so it can't be deleted. "
                  "Untick Active on the plan to hide it instead.", "danger")
            return redirect(url_for("portal_admin.admin_plans"))

        target = None
        if moved:
            raw = (request.form.get("move_to_plan_id") or "").strip()
            if raw.isdigit() and int(raw) != plan_id:
                cur.execute("SELECT id, name, slug FROM plans WHERE id=%s", (int(raw),))
                target = cur.fetchone()
            if not target:
                flash(f"Choose which plan the {len(moved)} merchant(s) on \"{plan['name']}\" should move to.", "danger")
                return redirect(url_for("portal_admin.admin_plans"))

        cur.execute("SELECT feature_key FROM plan_feature_grants WHERE plan_id=%s ORDER BY feature_key", (plan_id,))
        grants = [row["feature_key"] for row in (cur.fetchall() or [])]
        cur.execute("SELECT id, name FROM plans WHERE parent_plan_id=%s", (plan_id,))
        children = cur.fetchall() or []

        _os.makedirs(PLAN_DELETE_BACKUP_DIR, exist_ok=True)
        backup_path = _os.path.join(
            PLAN_DELETE_BACKUP_DIR,
            f"plan_{plan_id}_{plan['slug']}_{_dt.utcnow().strftime('%Y%m%d_%H%M%S')}.json")
        with open(backup_path, "w") as fh:
            _json.dump({"plan": plan, "feature_grants": grants,
                        "moved_tenants": moved, "moved_to": target,
                        "unlinked_children": children,
                        "deleted_by": _admin_user()}, fh, indent=2, default=str)

        cur2 = conn.cursor()
        if moved:
            cur2.execute("UPDATE tenants SET plan_id=%s WHERE plan_id=%s", (target["id"], plan_id))
            _sync_wa_ai_to_plan_sql(cur2, "t.id = ANY(%s)", ([t["id"] for t in moved],))
            _trim_staff_to_connect_seats_sql(cur2, [t["id"] for t in moved])
        cur2.execute("UPDATE plans SET parent_plan_id=NULL WHERE parent_plan_id=%s", (plan_id,))
        cur2.execute("DELETE FROM plans WHERE id=%s", (plan_id,))   # feature grants cascade
        conn.commit()
        cur2.close()
    except Exception as e:
        conn.rollback()
        print("⚠️ admin_plans_delete error:", e)
        flash("The plan could not be deleted — nothing was changed.", "danger")
        return redirect(url_for("portal_admin.admin_plans"))
    finally:
        cur.close(); conn.close()

    insert_audit_log(action="plan_delete", admin_username=_admin_user(),
                     details={"plan_id": plan_id, "slug": plan["slug"], "name": plan["name"],
                              "moved_tenant_ids": [t["id"] for t in moved],
                              "moved_to_plan_id": target["id"] if target else None,
                              "backup": backup_path})
    msg = f"Plan \"{plan['name']}\" deleted."
    if moved:
        msg += f" {len(moved)} merchant(s) moved to \"{target['name']}\"."
    flash(msg, "success")
    return redirect(url_for("portal_admin.admin_plans"))


@portal_admin_bp.route("/plans/<int:plan_id>/preview")
def admin_plan_preview(plan_id: int):
    """Standalone 'what will a business on this plan actually see' page —
    deliberately does NOT extend admin_base.html (no admin sidebar around
    it) so it reads as a clean mock of the real merchant experience, not an
    admin screen. Shows the pricing card plus the full sidebar menu with the
    same 🔒 lock logic the real portal sidebar uses, driven only by this
    plan's own grants — not tied to any specific real business/tenant."""
    r = _require_admin("plans", "view")
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM plans WHERE id=%s", (plan_id,))
    plan = cur.fetchone()
    if not plan:
        cur.close(); conn.close()
        flash("Plan not found.", "danger")
        return redirect(url_for("portal_admin.admin_plans"))

    cur.execute("SELECT feature_key FROM plan_feature_grants WHERE plan_id=%s", (plan_id,))
    granted = {row["feature_key"] for row in (cur.fetchall() or [])}
    cur.close(); conn.close()

    for col in ("feat_advanced_ai", "feat_visual_match", "feat_broadcasts",
                "feat_email_campaigns", "feat_fw_checkout"):
        if plan.get(col):
            granted.add(f"legacy:{col}")

    return render_template("portal/admin_plan_preview.html",
                            plan=plan, granted_features=granted,
                            feature_catalog=PLAN_EDITOR_CATALOG)


@portal_admin_bp.route("/plans/assign/<int:tenant_id>", methods=["POST"])
def admin_plans_assign(tenant_id: int):
    r = _require_admin("plans", "modify")
    if r: return r
    plan_id       = int(request.form.get("plan_id") or 1)
    billing_cycle = request.form.get("billing_cycle", "monthly")
    from datetime import date as _d
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE tenants
        SET plan_id=%s, billing_cycle=%s, plan_period_start=%s, quota_notified_at=NULL,
            trial_ends_at=NULL
        WHERE id=%s
    """, (plan_id, billing_cycle, _d.today(), tenant_id))
    # An admin-assigned plan is a deliberate plan, not a trial — clearing
    # trial_ends_at stops the daily check moving them back when an old
    # trial date passes.
    _sync_wa_ai_to_plan_sql(cur, "t.id = %s", (tenant_id,))
    _trim_staff_to_connect_seats_sql(cur, [tenant_id])
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


@portal_admin_bp.route("/plans/grandfather/<int:tenant_id>", methods=["POST"])
def admin_plans_grandfather(tenant_id: int):
    """Toggle whether a merchant keeps their single-channel price even if
    they run both WhatsApp + Website AI agents. See project_dual_agent_pricing
    memory — as of launch (2026-09-18) no real merchant needed this, it's a
    manual admin override for future use since there's no reliable automatic
    way to detect a tenant is actively using both channels."""
    r = _require_admin("plans", "modify")
    if r: return r
    grandfathered = request.form.get("dual_agent_grandfathered") == "on"
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE tenants SET dual_agent_grandfathered=%s WHERE id=%s", (grandfathered, tenant_id))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="plan_dual_grandfather_toggle", admin_username=_admin_user(),
                     details={"tenant_id": tenant_id, "grandfathered": grandfathered})
    flash("Dual Agent grandfather status updated.", "success")
    return redirect(url_for("portal_admin.admin_plans"))


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL CAMPAIGNS — admin-configured per-tenant ZeptoMail sending identity
# ══════════════════════════════════════════════════════════════════════════════
# The Mail Agent + domain (SPF/DKIM) are created manually in the ZeptoMail
# dashboard under the platform's own ZeptoMail account — ZeptoMail's
# domain/Mail Agent management API needs a separate OAuth2 grant, not worth
# building for this volume. An admin does that setup once, then pastes the
# resulting Send Mail Token here per tenant. Tenants without a row here fall
# back to the shared ZEPTOMAIL_FALLBACK_* identity at send time.

@portal_admin_bp.route("/customers/<int:customer_id>/email-sender", methods=["POST"])
def customer_email_sender_save(customer_id: int):
    r = _require_admin("customers", "modify")
    if r: return r

    from portal_routes import _encrypt_key

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (customer_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("Customer not found.", "danger")
        return redirect(url_for("portal_admin.customers"))
    tenant_id = int(row["tenant_id"])

    domain     = (request.form.get("domain") or "").strip().lower()
    from_email = (request.form.get("from_email") or "").strip().lower()
    from_name  = (request.form.get("from_name") or "").strip()
    token      = (request.form.get("send_mail_token") or "").strip()
    # ZeptoMail's dashboard displays the token as part of the full header value
    # ("Authorization: Zoho-enczapikey <token>") — admins pasting that whole
    # string would otherwise get it double-prefixed at send time, since
    # zeptomail_api.send_email() already adds "Zoho-enczapikey " itself.
    if token.lower().startswith("zoho-enczapikey "):
        token = token[len("Zoho-enczapikey "):].strip()

    if not domain or not from_email:
        flash("Domain and from-email are required.", "danger")
        cur.close(); conn.close()
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    cur.execute("SELECT id, zeptomail_token_enc FROM email_domains WHERE tenant_id=%s", (tenant_id,))
    existing = cur.fetchone()

    # A blank token field on an existing row means "keep the current token" —
    # the form never echoes the decrypted token back, so it's always blank on
    # edit unless the admin is deliberately rotating it.
    if token:
        token_enc = _encrypt_key(token)
    elif existing:
        token_enc = existing["zeptomail_token_enc"]
    else:
        flash("A Send Mail Token is required to add a new sending identity.", "danger")
        cur.close(); conn.close()
        return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))

    if existing:
        cur.execute("""
            UPDATE email_domains
            SET domain=%s, from_email=%s, from_name=%s, zeptomail_token_enc=%s,
                status='active', updated_at=NOW()
            WHERE tenant_id=%s
        """, (domain, from_email, from_name, token_enc, tenant_id))
    else:
        cur.execute("""
            INSERT INTO email_domains (tenant_id, domain, from_email, from_name, zeptomail_token_enc)
            VALUES (%s, %s, %s, %s, %s)
        """, (tenant_id, domain, from_email, from_name, token_enc))
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(action="email_sender_save", admin_username=_admin_user(),
                     details={"tenant_id": tenant_id, "domain": domain, "from_email": from_email})
    flash(f"Email sending identity saved for tenant #{tenant_id}.", "success")
    return redirect(url_for("portal_admin.customer_detail", customer_id=customer_id))


# ══════════════════════════════════════════════════════════════════════════════
# SCHOOL PLANS MANAGEMENT — admin view/assign plans to schools (school.phixtra.com)
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/school-plans")
def admin_school_plans():
    r = _require_admin("school_plans", "view")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM school_plans ORDER BY sort_order")
    plans = cur.fetchall() or []

    cur.execute("""
        SELECT s.id AS school_id, s.school_name,
               s.billing_cycle, s.plan_period_start,
               COALESCE(p.slug, 'free') AS plan_slug,
               COALESCE(p.name, 'Free') AS plan_name,
               (SELECT COUNT(*) FROM school_students st
                WHERE st.school_id=s.id AND st.is_active=TRUE) AS student_count,
               (SELECT COUNT(*) FROM school_chat_history ch
                WHERE ch.school_id=s.id AND ch.role='assistant'
                  AND ch.created_at >= COALESCE(s.plan_period_start, CURRENT_DATE - 30)) AS msgs_used,
               COALESCE(p.ai_messages_limit, 100) AS msgs_limit
        FROM school_profiles s
        LEFT JOIN school_plans p ON p.id = s.plan_id
        ORDER BY s.school_name
    """)
    schools = cur.fetchall() or []
    cur.close(); conn.close()

    return render_template("portal/admin_school_plans.html",
                           plans=plans, schools=schools)


@portal_admin_bp.route("/school-plans/assign/<int:school_id>", methods=["POST"])
def admin_school_plans_assign(school_id: int):
    r = _require_admin("school_plans", "modify")
    if r: return r
    plan_id       = int(request.form.get("plan_id") or 1)
    billing_cycle = request.form.get("billing_cycle", "termly")
    from datetime import date as _d
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE school_profiles
        SET plan_id=%s, billing_cycle=%s, plan_period_start=%s,
            quota_notified_at=NULL, renewal_notified_at=NULL
        WHERE id=%s
    """, (plan_id, billing_cycle, _d.today(), school_id))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="school_plan_assign", admin_username=_admin_user(),
                     details={"school_id": school_id, "plan_id": plan_id, "billing_cycle": billing_cycle})
    flash(f"Plan updated for school #{school_id}.", "success")
    return redirect(url_for("portal_admin.admin_school_plans"))


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP DIAGNOSTICS — admin troubleshooting tool
# ══════════════════════════════════════════════════════════════════════════════

@portal_admin_bp.route("/wa-diagnostics")
def wa_diagnostics():
    r = _require_admin("wa_diagnostics", "view")
    if r: return r
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
    r = _require_admin("wa_diagnostics", "view")
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


# ── Social Media Posts ───────────────────────────────────────────────────────
# Internal content queue: owner (or a scoped support login) uploads an image +
# caption tagged for one or more platforms. Publishing goes out through Buffer
# (see buffer_client.py) — either reviewed in the "Ready to Post" queue and
# sent/scheduled manually, or fired immediately via the Urgent Post path.
# The social media executive's login has view+create+modify (no delete) so
# she can both queue normal posts and fire urgent ones herself; delete stays
# owner-only so the audit trail can't be wiped by that account.

def _sm_platforms_from_form() -> list:
    submitted = request.form.getlist("platforms")
    return [p for p in submitted if p in SM_PLATFORMS]


def _sm_public_image_url(public_token: str) -> str:
    base_url = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")
    return f"{base_url}/admin/social-media/public/{public_token}/image"


def _sm_save_post(caption: str, platforms: list, file, is_urgent: bool = False):
    """Shared validation + save for both the normal upload and the Urgent
    Post path. Returns (post_id, error_message) — error_message is set (and
    post_id is None) if validation failed, so callers just flash it."""
    if not caption:
        return None, "Please add a caption for the post."
    if not platforms:
        return None, "Please select at least one platform."
    if not file or not file.filename:
        return None, "Please choose an image to upload."

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in SM_ALLOWED_EXTS:
        return None, "Only JPG, PNG, WEBP, and GIF images are allowed."

    file.seek(0, 2)
    size_bytes = file.tell()
    file.seek(0)
    if size_bytes > SM_MAX_BYTES:
        return None, "Image is too large — 15 MB maximum."

    stored_filename = f"{uuid.uuid4().hex}.{ext}"
    os.makedirs(SM_UPLOAD_FOLDER, exist_ok=True)
    file.save(os.path.join(SM_UPLOAD_FOLDER, stored_filename))
    public_token = uuid.uuid4().hex

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        INSERT INTO social_media_posts
            (caption, image_filename, original_filename, platforms, status,
             created_by, is_urgent, public_token)
        VALUES (%s, %s, %s, %s, 'ready', %s, %s, %s)
        RETURNING id
    """, (caption, stored_filename, file.filename, platforms, _admin_user(), is_urgent, public_token))
    post_id = cur.fetchone()["id"]
    conn.commit()
    cur.close(); conn.close()
    return post_id, None


def _sm_send_to_buffer(post_id: int, scheduled_for_iso: str = None):
    """Sends one post to Buffer on every mapped platform it's tagged with.
    Returns (ok, message) for the caller to flash. On success with no
    schedule, marks the post posted immediately (Buffer's "Share Now" is
    synchronous from our side — it either publishes or errors)."""
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM social_media_posts WHERE id=%s", (post_id,))
    post = cur.fetchone()
    if not post:
        cur.close(); conn.close()
        return False, "Post not found."

    cur.execute("SELECT * FROM buffer_channel_map")
    channel_map = {row["platform"]: row["buffer_channel_id"] for row in (cur.fetchall() or [])}

    unmapped = [p for p in post["platforms"] if p not in channel_map]
    if unmapped:
        cur.close(); conn.close()
        labels = ", ".join(SM_PLATFORMS.get(p, {}).get("label", p) for p in unmapped)
        return False, (f"No Buffer channel mapped for: {labels}. "
                        f"Set it up on the Buffer Channels settings page first.")

    image_url = _sm_public_image_url(post["public_token"])
    buffer_post_ids = dict(post.get("buffer_post_ids") or {})
    errors = []
    for platform in post["platforms"]:
        try:
            buf_id, _status = _ba.call(
                _ba.ADMIN_OWNER, buffer_create_post, channel_map[platform], post["caption"], image_url,
                scheduled_for_iso, service=SM_PLATFORMS.get(platform, {}).get("service"))
            buffer_post_ids[platform] = buf_id
        except BufferAPIError as e:
            errors.append(f"{SM_PLATFORMS.get(platform, {}).get('label', platform)}: {e}")

    if errors:
        cur.execute("""UPDATE social_media_posts
                        SET buffer_post_ids=%s, buffer_status='failed', buffer_error=%s
                        WHERE id=%s""",
                    (_json.dumps(buffer_post_ids), " | ".join(errors), post_id))
        conn.commit()
        cur.close(); conn.close()
        insert_audit_log(admin_username=_admin_user(), action="social_media_post_buffer_error",
                          details={"post_id": post_id, "errors": errors})
        return False, f"Sent to some platforms, but failed on: {' | '.join(errors)}"

    if scheduled_for_iso:
        cur.execute("""UPDATE social_media_posts
                        SET buffer_post_ids=%s, buffer_status='scheduled', buffer_error=NULL,
                            scheduled_for=%s
                        WHERE id=%s""",
                    (_json.dumps(buffer_post_ids), scheduled_for_iso, post_id))
        msg = f"Scheduled via Buffer for {scheduled_for_iso}."
    else:
        cur.execute("""UPDATE social_media_posts
                        SET buffer_post_ids=%s, buffer_status='sent', buffer_error=NULL,
                            status='posted', posted_at=NOW(), posted_by='Buffer'
                        WHERE id=%s""",
                    (_json.dumps(buffer_post_ids), post_id))
        msg = "Sent to Buffer and published now."
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(), action="social_media_post_send_to_buffer",
                      details={"post_id": post_id, "scheduled_for": scheduled_for_iso})
    return True, msg


@portal_admin_bp.route("/social-media", methods=["GET"])
def social_media_posts():
    r = _require_admin("social_media", "view")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM social_media_posts ORDER BY status ASC, created_at DESC")
    posts = cur.fetchall() or []
    cur.execute("SELECT platform FROM buffer_channel_map")
    mapped_platforms = {row["platform"] for row in (cur.fetchall() or [])}
    cur.close(); conn.close()
    ready_count = sum(1 for p in posts if p["status"] == "ready")
    unmapped_platforms = [p for p in SM_PLATFORMS if p not in mapped_platforms]
    return render_template("portal/admin_social_media.html", posts=posts, platforms=SM_PLATFORMS,
                            ready_count=ready_count, unmapped_platforms=unmapped_platforms)


@portal_admin_bp.route("/social-media/upload", methods=["POST"])
def social_media_post_upload():
    r = _require_admin("social_media", "create")
    if r: return r

    caption   = (request.form.get("caption") or "").strip()
    platforms = _sm_platforms_from_form()
    file      = request.files.get("image_file")

    post_id, err = _sm_save_post(caption, platforms, file)
    if err:
        flash(err, "warning")
        return redirect(url_for("portal_admin.social_media_posts"))

    insert_audit_log(admin_username=_admin_user(), action="social_media_post_upload",
                      details={"platforms": platforms, "post_id": post_id})
    flash("Post added — ready to publish.", "success")
    return redirect(url_for("portal_admin.social_media_posts"))


@portal_admin_bp.route("/social-media/urgent", methods=["POST"])
def social_media_post_urgent():
    """Fast path: caption + image + platforms, published via Buffer the
    moment it's submitted — no "Ready to Post" review step."""
    r = _require_admin("social_media", "create")
    if r: return r

    caption   = (request.form.get("caption") or "").strip()
    platforms = _sm_platforms_from_form()
    file      = request.files.get("image_file")

    post_id, err = _sm_save_post(caption, platforms, file, is_urgent=True)
    if err:
        flash(err, "warning")
        return redirect(url_for("portal_admin.social_media_posts"))

    insert_audit_log(admin_username=_admin_user(), action="social_media_post_urgent",
                      details={"platforms": platforms, "post_id": post_id})

    ok, msg = _sm_send_to_buffer(post_id, scheduled_for_iso=None)
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("portal_admin.social_media_posts"))


@portal_admin_bp.route("/social-media/<int:post_id>/send-to-buffer", methods=["POST"])
def social_media_post_send_to_buffer(post_id: int):
    r = _require_admin("social_media", "modify")
    if r: return r

    # <input type="datetime-local"> gives "2026-09-10T15:00" with no timezone —
    # the form label tells staff to enter it in UTC, so we just normalize to
    # the ISO8601 shape Buffer's API expects.
    raw = (request.form.get("scheduled_for") or "").strip()
    scheduled_for_iso = None
    if raw:
        from datetime import datetime as _dt
        try:
            scheduled_for_iso = _dt.strptime(raw, "%Y-%m-%dT%H:%M").strftime("%Y-%m-%dT%H:%M:00.000Z")
        except ValueError:
            flash("Invalid schedule time.", "warning")
            return redirect(url_for("portal_admin.social_media_posts"))

    ok, msg = _sm_send_to_buffer(post_id, scheduled_for_iso)
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("portal_admin.social_media_posts"))


@portal_admin_bp.route("/social-media/<int:post_id>/check-buffer-status", methods=["POST"])
def social_media_post_check_buffer_status(post_id: int):
    r = _require_admin("social_media", "modify")
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM social_media_posts WHERE id=%s", (post_id,))
    post = cur.fetchone()
    if not post or not post.get("buffer_post_ids"):
        cur.close(); conn.close()
        flash("Nothing to check — this post hasn't been sent to Buffer.", "warning")
        return redirect(url_for("portal_admin.social_media_posts"))

    statuses = {}
    try:
        for platform, buf_id in post["buffer_post_ids"].items():
            statuses[platform] = _ba.call(_ba.ADMIN_OWNER, buffer_get_post_status, buf_id)
    except BufferAPIError as e:
        cur.close(); conn.close()
        flash(f"Could not reach Buffer: {e}", "danger")
        return redirect(url_for("portal_admin.social_media_posts"))

    if statuses and all(s == "sent" for s in statuses.values()):
        cur.execute("""UPDATE social_media_posts
                        SET status='posted', buffer_status='sent', posted_at=NOW(), posted_by='Buffer'
                        WHERE id=%s""", (post_id,))
        conn.commit()
        msg, level = "Confirmed live — marked Posted.", "success"
    elif any(s == "failed" for s in statuses.values()):
        cur.execute("UPDATE social_media_posts SET buffer_status='failed' WHERE id=%s", (post_id,))
        conn.commit()
        msg, level = "Buffer reports this post failed to publish.", "danger"
    else:
        msg, level = f"Still pending in Buffer ({', '.join(sorted(set(statuses.values())))}).", "warning"
    cur.close(); conn.close()
    flash(msg, level)
    return redirect(url_for("portal_admin.social_media_posts"))


@portal_admin_bp.route("/social-media/public/<token>/image", methods=["GET"])
def social_media_post_public_image(token: str):
    """Unauthenticated by design — Buffer's servers fetch the post image from
    here when publishing, and can't carry an admin session cookie. Safe
    because `token` is an unguessable 32-byte hex value, one per post, never
    exposed anywhere the admin-only image/download routes aren't already."""
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT image_filename FROM social_media_posts WHERE public_token=%s", (token,))
    post = cur.fetchone()
    cur.close(); conn.close()
    if not post:
        return "Not found", 404
    return send_from_directory(SM_UPLOAD_FOLDER, post["image_filename"], as_attachment=False)


@portal_admin_bp.route("/social-media/<int:post_id>/edit", methods=["POST"])
def social_media_post_edit(post_id: int):
    r = _require_admin("social_media", "modify")
    if r: return r

    caption   = (request.form.get("caption") or "").strip()
    platforms = _sm_platforms_from_form()
    file      = request.files.get("image_file")

    if not caption:
        flash("Please add a caption for the post.", "warning")
        return redirect(url_for("portal_admin.social_media_posts"))
    if not platforms:
        flash("Please select at least one platform.", "warning")
        return redirect(url_for("portal_admin.social_media_posts"))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM social_media_posts WHERE id=%s", (post_id,))
    post = cur.fetchone()
    if not post:
        cur.close(); conn.close()
        flash("Post not found.", "warning")
        return redirect(url_for("portal_admin.social_media_posts"))

    new_stored_filename = post["image_filename"]
    new_original_name   = post["original_filename"]
    old_stored_filename = None

    if file and file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in SM_ALLOWED_EXTS:
            cur.close(); conn.close()
            flash("Only JPG, PNG, WEBP, and GIF images are allowed.", "warning")
            return redirect(url_for("portal_admin.social_media_posts"))
        file.seek(0, 2)
        size_bytes = file.tell()
        file.seek(0)
        if size_bytes > SM_MAX_BYTES:
            cur.close(); conn.close()
            flash("Image is too large — 15 MB maximum.", "warning")
            return redirect(url_for("portal_admin.social_media_posts"))

        new_stored_filename = f"{uuid.uuid4().hex}.{ext}"
        os.makedirs(SM_UPLOAD_FOLDER, exist_ok=True)
        file.save(os.path.join(SM_UPLOAD_FOLDER, new_stored_filename))
        old_stored_filename = post["image_filename"]
        new_original_name   = file.filename

    cur.execute("""
        UPDATE social_media_posts
        SET caption=%s, platforms=%s, image_filename=%s, original_filename=%s
        WHERE id=%s
    """, (caption, platforms, new_stored_filename, new_original_name, post_id))
    conn.commit()
    cur.close(); conn.close()

    if old_stored_filename:
        try:
            os.remove(os.path.join(SM_UPLOAD_FOLDER, old_stored_filename))
        except OSError:
            pass

    insert_audit_log(admin_username=_admin_user(), action="social_media_post_edit",
                      details={"post_id": post_id, "platforms": platforms})
    flash("Post updated.", "success")
    return redirect(url_for("portal_admin.social_media_posts"))


@portal_admin_bp.route("/social-media/<int:post_id>/mark-posted", methods=["POST"])
def social_media_post_mark_posted(post_id: int):
    r = _require_admin("social_media", "modify")
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT status FROM social_media_posts WHERE id=%s", (post_id,))
    post = cur.fetchone()
    if not post:
        cur.close(); conn.close()
        flash("Post not found.", "warning")
        return redirect(url_for("portal_admin.social_media_posts"))

    new_status = "ready" if post["status"] == "posted" else "posted"
    if new_status == "posted":
        cur.execute("""UPDATE social_media_posts
                        SET status='posted', posted_at=NOW(), posted_by=%s WHERE id=%s""",
                    (_admin_user(), post_id))
    else:
        cur.execute("""UPDATE social_media_posts
                        SET status='ready', posted_at=NULL, posted_by=NULL WHERE id=%s""",
                    (post_id,))
    conn.commit()
    cur.close(); conn.close()

    insert_audit_log(admin_username=_admin_user(), action="social_media_post_status",
                      details={"post_id": post_id, "status": new_status})
    flash("Marked as posted." if new_status == "posted" else "Moved back to Ready to Post.", "success")
    return redirect(url_for("portal_admin.social_media_posts"))


@portal_admin_bp.route("/social-media/<int:post_id>/delete", methods=["POST"])
def social_media_post_delete(post_id: int):
    r = _require_admin("social_media", "delete")
    if r: return r

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM social_media_posts WHERE id=%s", (post_id,))
    post = cur.fetchone()
    if not post:
        cur.close(); conn.close()
        flash("Post not found.", "warning")
        return redirect(url_for("portal_admin.social_media_posts"))

    cur.execute("DELETE FROM social_media_posts WHERE id=%s", (post_id,))
    conn.commit()
    cur.close(); conn.close()

    try:
        os.remove(os.path.join(SM_UPLOAD_FOLDER, post["image_filename"]))
    except OSError:
        pass

    insert_audit_log(admin_username=_admin_user(), action="social_media_post_delete",
                      details={"post_id": post_id})
    flash("Post deleted.", "success")
    return redirect(url_for("portal_admin.social_media_posts"))


@portal_admin_bp.route("/social-media/<int:post_id>/image", methods=["GET"])
def social_media_post_image(post_id: int):
    r = _require_admin("social_media", "view")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT image_filename FROM social_media_posts WHERE id=%s", (post_id,))
    post = cur.fetchone()
    cur.close(); conn.close()
    if not post:
        return "Post not found", 404
    return send_from_directory(SM_UPLOAD_FOLDER, post["image_filename"], as_attachment=False)


@portal_admin_bp.route("/social-media/<int:post_id>/download", methods=["GET"])
def social_media_post_download(post_id: int):
    r = _require_admin("social_media", "view")
    if r: return r
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT image_filename, original_filename FROM social_media_posts WHERE id=%s", (post_id,))
    post = cur.fetchone()
    cur.close(); conn.close()
    if not post:
        return "Post not found", 404
    return send_from_directory(SM_UPLOAD_FOLDER, post["image_filename"],
                                as_attachment=True,
                                download_name=post["original_filename"] or post["image_filename"])


@portal_admin_bp.route("/settings/buffer-channels", methods=["GET", "POST"])
def buffer_channels_settings():
    """PhiXtra's own Buffer (2026-09-25): the key is entered here instead of
    on the server, and each Social Media Posts platform is pointed at one of
    the real Buffer accounts from a dropdown. Owner-only, same as Team
    management. Businesses connect their own Buffer separately on
    Integration › Buffer (buffer_routes.py)."""
    r = _require_owner()
    if r: return r
    owner = _ba.ADMIN_OWNER

    if request.method == "POST":
        action = request.form.get("action")
        if action == "save_key":
            api_key = (request.form.get("api_key") or "").strip()
            if not api_key:
                flash("Paste the Buffer API key first.", "warning")
                return redirect(url_for("portal_admin.buffer_channels_settings"))
            try:
                orgs = _ba.check_key(api_key)
                _ba.save_account(owner, None, api_key, orgs[0], _admin_user())
            except BufferAPIError as e:
                msg = ("Buffer didn't accept this key. Check you copied the whole key from Buffer › Settings › API, "
                       "then try again.") if e.is_auth_error else f"Buffer said: {e}"
                flash(msg, "danger")
                return redirect(url_for("portal_admin.buffer_channels_settings"))
            insert_audit_log(admin_username=_admin_user(), action="buffer_admin_key_saved",
                              details={"organization": orgs[0].get("name")})
            flash(f"Buffer accepted the key. Connected to “{orgs[0].get('name')}”.", "success")
        elif action == "refresh":
            try:
                chans = _ba.refresh_channels(owner)
                flash(f"Connection working. {len(chans)} Buffer account(s) found.", "success")
            except BufferAPIError as e:
                flash(f"Buffer said: {e}", "danger")
        elif action == "disconnect":
            _ba.disconnect(owner)
            insert_audit_log(admin_username=_admin_user(), action="buffer_admin_disconnected", details={})
            flash("PhiXtra's Buffer disconnected and the key deleted.", "success")
        elif action == "save_map":
            known = {c["channel_id"]: c for c in _ba.list_channels(owner)}
            conn = get_db_connection()
            cur  = conn.cursor()
            for platform in SM_PLATFORMS:
                channel_id = (request.form.get(f"channel__{platform}") or "").strip()
                if channel_id in known:
                    cur.execute("""
                        INSERT INTO buffer_channel_map (platform, buffer_channel_id, channel_label, updated_by)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (platform) DO UPDATE
                            SET buffer_channel_id=EXCLUDED.buffer_channel_id,
                                channel_label=EXCLUDED.channel_label,
                                updated_at=NOW(), updated_by=EXCLUDED.updated_by
                    """, (platform, channel_id, known[channel_id]["title"], _admin_user()))
                else:
                    cur.execute("DELETE FROM buffer_channel_map WHERE platform=%s", (platform,))
            conn.commit()
            cur.close(); conn.close()
            insert_audit_log(admin_username=_admin_user(), action="buffer_channels_update", details={})
            flash("Saved which Buffer account each platform posts to.", "success")
        return redirect(url_for("portal_admin.buffer_channels_settings"))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM buffer_channel_map")
    current = {row["platform"]: row for row in (cur.fetchall() or [])}
    cur.close(); conn.close()

    _ba.refresh_channels_if_stale(owner)
    account = _ba.get_account(owner)
    channels = _ba.list_channels(owner) if account else []
    return render_template("portal/admin_buffer_channels.html", platforms=SM_PLATFORMS,
                            current=current, account=account, channels=channels)


# Which product(s) a tutorial video is published to (Portal, Connect, or
# both) — set by an admin on this page, read by portal_routes.video_tutorials()
# to filter each product's own gallery. A video with no row here defaults to
# ["portal"] only, matching reality for every video built before this
# feature (2026-09-11): none were ever shown on Connect until an admin
# explicitly adds it here.
VIDEO_TUTORIAL_PRODUCTS = {"portal": "Portal", "connect": "Connect"}

def _video_products_map():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT slug, products FROM tutorial_video_products")
    rows = dict(cur.fetchall())
    cur.close(); conn.close()
    return rows


@portal_admin_bp.route("/video-tutorials")
def video_tutorials():
    r = _require_admin("video_tutorials", "view")
    if r: return r
    products_by_slug = _video_products_map()
    videos = [dict(v, products=products_by_slug.get(v["slug"], ["portal"])) for v in TUTORIAL_VIDEOS]
    can_modify = _is_owner() or session.get("portal_admin_permissions", {}).get("video_tutorials", {}).get("modify")
    return render_template("portal/admin_video_tutorials.html", videos=videos,
                            product_options=VIDEO_TUTORIAL_PRODUCTS, can_modify=can_modify)


@portal_admin_bp.route("/video-tutorials/<slug>/products", methods=["POST"])
def video_tutorials_products(slug):
    r = _require_admin("video_tutorials", "modify")
    if r: return r
    if slug not in {v["slug"] for v in TUTORIAL_VIDEOS}:
        flash("That video doesn't exist.", "danger")
        return redirect(url_for("portal_admin.video_tutorials"))

    products = [p for p in request.form.getlist("products") if p in VIDEO_TUTORIAL_PRODUCTS]
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tutorial_video_products (slug, products, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (slug) DO UPDATE SET products = EXCLUDED.products, updated_at = NOW()
    """, (slug, products))
    conn.commit()
    cur.close(); conn.close()

    if products:
        flash(f"Saved — now showing on: {', '.join(VIDEO_TUTORIAL_PRODUCTS[p] for p in products)}.", "success")
    else:
        flash("Saved — this video is hidden from every product's gallery until you tag it again.", "warning")
    return redirect(url_for("portal_admin.video_tutorials"))



# ══════════════════════════════════════════════════════════════════════════════
# INACTIVE ACCOUNTS — warn, then delete businesses unused for 60 days
# (approved 2026-10-18; rules in inactivity_core.py, daily job inactivity_jobs.py)
# ══════════════════════════════════════════════════════════════════════════════

_INACTIVE_ORDER = {"warned": 0, "active": 1, "no_email": 2, "protected": 3}


@portal_admin_bp.route("/inactive-accounts")
def inactive_accounts():
    r = _require_admin("customers", "view")
    if r: return r
    import inactivity_core as ic
    from datetime import date, timedelta
    accts = ic.accounts()
    live = [a for a in accts if a["status"] != "deleted"]
    live.sort(key=lambda a: (_INACTIVE_ORDER[a["status"]], a["next_on"] or date.max, -a["days_inactive"]))
    deleted = sorted([a for a in accts if a["status"] == "deleted"], key=lambda a: a["deleted_at"], reverse=True)
    counts = {k: sum(1 for a in live if a["status"] == k) for k in _INACTIVE_ORDER}

    sample = {"first_name": "Ada", "business_name": "Ada's Fashion House", "days_inactive": 45,
              "deletion_on": date.today() + timedelta(days=ic.NOTICE_DAYS)}
    previews = []
    for kind, when in (("warn1", "Day 45"), ("warn2", "7 days before"), ("warn3", "1 day before"), ("deleted", "Day 60")):
        subject, html = ic.email_html(kind, sample)
        previews.append({"when": when, "subject": subject, "html": html})

    return render_template("portal/admin_inactive_accounts.html", accounts=live, deleted=deleted,
                           counts=counts, mode=ic.get_mode(), modes=ic.MODES, mode_labels=ic.MODE_LABELS,
                           log=ic.recent_log(60), previews=previews,
                           warn_after=ic.WARN_AFTER_DAYS, notice=ic.NOTICE_DAYS,
                           tracking_start=ic.TRACKING_START.date())


@portal_admin_bp.route("/inactive-accounts/mode", methods=["POST"])
def inactive_accounts_mode():
    r = _require_owner()
    if r: return r
    import inactivity_core as ic
    mode = request.form.get("mode")
    if mode not in ic.MODES:
        flash("Pick Off, Test mode or On.", "danger")
        return redirect(url_for("portal_admin.inactive_accounts"))
    ic.set_mode(mode, _admin_user())
    insert_audit_log(admin_username=_admin_user(), action="inactivity_mode", details={"mode": mode})
    flash({"off": "Inactive accounts is OFF — nothing will be sent or deleted.",
           "test": "Test mode — businesses get nothing; you get a daily email of what would happen.",
           "on": "Inactive accounts is ON — warnings go out and accounts are deleted on their dates."}[mode], "success")
    return redirect(url_for("portal_admin.inactive_accounts"))


def _inactive_back(tenant_id):
    nxt = request.form.get("next") or ""
    return redirect(nxt if nxt.startswith("/admin/") else url_for("portal_admin.inactive_accounts"))


@portal_admin_bp.route("/inactive-accounts/<int:tenant_id>/keep", methods=["POST"])
def inactive_accounts_keep(tenant_id: int):
    r = _require_admin("customers", "modify")
    if r: return r
    import inactivity_core as ic
    keep = request.form.get("keep") == "1"
    ic.set_keep(tenant_id, keep, _admin_user())
    insert_audit_log(admin_username=_admin_user(), action="inactivity_keep" if keep else "inactivity_unkeep",
                     tenant_id=tenant_id)
    flash("Kept — this account will never be deleted for inactivity." if keep
          else "No longer kept — normal inactivity rules apply again.", "success")
    return _inactive_back(tenant_id)


@portal_admin_bp.route("/inactive-accounts/<int:tenant_id>/delay", methods=["POST"])
def inactive_accounts_delay(tenant_id: int):
    r = _require_admin("customers", "modify")
    if r: return r
    import inactivity_core as ic
    ic.delay(tenant_id, _admin_user())
    flash("Delayed by 30 days. Any warnings already sent start again later, with the new date.", "success")
    return _inactive_back(tenant_id)
