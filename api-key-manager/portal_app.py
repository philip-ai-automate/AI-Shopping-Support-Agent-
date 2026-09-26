"""
portal_app.py — Phase 1 portal entry point (portal.phixtra.com)
This file only wires blueprints together. No business logic here.
app.py (keys.phixtra.com) is completely separate and untouched.
"""
import os
import re
import html as _html
import psycopg2.extras
from flask import Flask
from markupsafe import Markup
from dotenv import load_dotenv

load_dotenv()


def create_app():
    flask_app = Flask(__name__, template_folder="templates", static_folder="static")
    flask_app.secret_key = os.getenv("PORTAL_SECRET_KEY", "change-this-secret-in-env")

    from portal_migrations import ensure_portal_tables
    from portal_routes import portal_bp
    from portal_admin_routes import portal_admin_bp
    from portal_facebook_routes import facebook_bp
    from pressone_routes import pressone_bp
    from buffer_routes import buffer_bp
    from ai_design_routes import social_bp, ai_admin_bp
    from upload_design_routes import upload_bp
    from ambassador_routes import ambassador_bp
    from school_routes import school_bp
    from school_migrations import ensure_school_tables
    from portal_routes_estate import estate_bp

    # Run DB migrations on startup (all idempotent — safe)
    ensure_portal_tables()
    ensure_school_tables()

    flask_app.register_blueprint(portal_bp)
    flask_app.register_blueprint(portal_admin_bp, url_prefix="/admin")
    flask_app.register_blueprint(facebook_bp)
    flask_app.register_blueprint(pressone_bp)
    flask_app.register_blueprint(buffer_bp)
    flask_app.register_blueprint(social_bp)
    flask_app.register_blueprint(ai_admin_bp)
    flask_app.register_blueprint(upload_bp)
    flask_app.register_blueprint(ambassador_bp)
    flask_app.register_blueprint(school_bp, url_prefix="/school")
    flask_app.register_blueprint(estate_bp)

    # Feature -> Roles/Modules update policy (feature_access.py): grant any
    # brand-new catalog key to every plan once, then log anything that
    # doesn't line up. Warns only — never stops the portal starting.
    try:
        from db import get_db_connection
        from feature_access import (check_feature_access, sync_new_feature_keys_to_plans,
                                    PLAN_LOCK_CODE_PATHS)
        from portal_routes import PLAN_FEATURE_CATALOG, ROLE_FORM_GRID, PLAN_ONLY_FEATURE_KEYS
        added = sync_new_feature_keys_to_plans(PLAN_FEATURE_CATALOG, get_db_connection)
        if added:
            print(f"✅ New features granted to every plan: {', '.join(added)}")
        problems = check_feature_access(flask_app, PLAN_FEATURE_CATALOG, ROLE_FORM_GRID,
                                        PLAN_ONLY_FEATURE_KEYS, code_paths=PLAN_LOCK_CODE_PATHS)
        for p in problems:
            print(f"⚠️ FEATURES NOT IN ROLES — {p['area']}: {p['item']} — {p['fix']}")
    except Exception as e:
        print("⚠️ Feature/Roles check could not run:", e)

    @flask_app.template_filter("with_plus")
    def _with_plus(value):
        """Prefix '+' only if not already present. Meta's display_phone_number
        comes back already formatted with a leading '+' — templates that did
        `+{{ display_phone_number }}` produced a literal double '++'."""
        s = str(value or "")
        return s if s.startswith("+") else f"+{s}"

    @flask_app.template_filter("richtext")
    def _richtext(value):
        """Plain-text -> safe HTML for admin-authored copy (e.g. feature
        release pitch notes/demo instructions). All text is HTML-escaped
        first, then a tiny markup subset is applied line-by-line:
          - blank line   -> paragraph break
          - '- ' / '* '  -> bullet list item
          - '### '       -> small subheading
        Lets admins write '- point one' / '### Talking points' in a plain
        <textarea> and have it render as real headings/bullets, without
        allowing arbitrary HTML injection."""
        text = (value or "").strip()
        if not text:
            return Markup("")
        parts = []
        list_buf = []

        def _flush_list():
            if list_buf:
                items = "".join(f"<li>{_html.escape(li)}</li>" for li in list_buf)
                parts.append(f"<ul class='rt-list'>{items}</ul>")
                list_buf.clear()

        for block in re.split(r"\n\s*\n", text):
            for line in block.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("### "):
                    _flush_list()
                    parts.append(f"<div class='rt-subhead'>{_html.escape(line[4:])}</div>")
                elif line.startswith(("- ", "* ")):
                    list_buf.append(line[2:])
                else:
                    _flush_list()
                    parts.append(f"<p>{_html.escape(line)}</p>")
            _flush_list()
        return Markup("".join(parts))

    from flask import request, redirect

    @flask_app.before_request
    def _subdomain_redirect():
        """Redirect subdomains to their blueprint prefix."""
        host = request.host.split(":")[0]
        if host == "school.phixtra.com" and not request.path.startswith("/school"):
            new_path = "/school" + request.path
            qs = ("?" + request.query_string.decode()) if request.query_string else ""
            return redirect(new_path + qs, code=302)
        if host == "home.phixtra.com" and not request.path.startswith("/estate"):
            new_path = "/estate" + request.path
            qs = ("?" + request.query_string.decode()) if request.query_string else ""
            return redirect(new_path + qs, code=302)

    @flask_app.before_request
    def _refresh_team_member_access():
        """A team member's role permissions used to be copied into the session
        once, at login — so unticking a permission, changing their role,
        deactivating or removing them only took effect after they logged out
        (found 2026-09-26). Re-read them on every request instead, for every
        blueprint. Deactivated/removed → logged out straight away. A database
        error keeps the session as it was rather than logging everyone out."""
        from flask import session, flash, url_for
        tm_id = session.get("team_member_id")
        if not tm_id or request.endpoint == "static":
            return None
        try:
            from db import get_db_connection
            from portal_routes import _parse_json_maybe_role
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT tm.is_active, tm.role_id, r.permissions
                FROM team_members tm
                JOIN customers c ON c.id = %s AND c.tenant_id = tm.tenant_id
                LEFT JOIN tenant_roles r ON r.id = tm.role_id AND r.tenant_id = tm.tenant_id
                WHERE tm.id = %s
            """, (int(session.get("customer_id") or 0), int(tm_id)))
            row = cur.fetchone()
            cur.close(); conn.close()
        except Exception as e:
            print("⚠️ _refresh_team_member_access error:", e)
            return None
        if not row or not row["is_active"]:
            session.clear()
            flash("Your team account is no longer active. Contact your business owner.", "warning")
            return redirect(url_for("portal.login"))
        session["team_member_role_id"] = row["role_id"]
        session["team_member_permissions"] = _parse_json_maybe_role(row["permissions"]) if row["role_id"] else {}
        return None

    @flask_app.context_processor
    def _inject_staff_can_open():
        """staff_can_open('portal.orders') for the side menu: True for the
        account owner, and for a team member only if their role holds one of
        the permissions that page is labelled with (feature_access.py) — the
        same labels the page itself enforces, so the menu can never show a
        staff member a link that just bounces them back to the Inbox."""
        from flask import session
        from feature_access import route_access, TEAM, TEAM_ANY

        def staff_can_open(endpoint):
            if not session.get("team_member_id"):
                return True
            view = flask_app.view_functions.get(endpoint)
            kind, keys = route_access(view)
            if kind == TEAM_ANY:
                return True
            if kind != TEAM:
                return False
            perms = session.get("team_member_permissions") or {}
            return any(perms.get(k) for k in keys)
        return {"staff_can_open": staff_can_open}

    # ── Global template context: inject current customer so every template,
    #    including base.html, can access avatar_data, first_name, etc.
    from flask import session as _session, g as _g

    @flask_app.context_processor
    def inject_current_customer():
        """Make `_portal_customer` available in every template when logged in.

        A Shared Team Inbox login keeps session["customer_id"] pointed at the
        ACCOUNT OWNER's row (so every other route's tenant_id resolution
        works unchanged — see login()/portal_routes.py). Without the check
        below, a logged-in team member would see the OWNER's name/avatar in
        their own sidebar. When session["team_member_id"] is set, build the
        identity from team_members instead, keeping tenant_domain/tenant_name
        from the shared tenant."""
        if not _session.get("portal_logged_in"):
            return {"_portal_customer": None}
        cid = _session.get("impersonate_customer_id") or _session.get("customer_id")
        if not cid:
            return {"_portal_customer": None}
        tm_id = _session.get("team_member_id")
        # Cache on g so we only hit the DB once per request
        if not hasattr(_g, "_cached_portal_customer"):
            try:
                from db import get_db_connection
                conn = get_db_connection()
                cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                if tm_id:
                    cur.execute("""
                        SELECT tm.id, tm.name AS first_name, '' AS last_name, tm.email,
                               NULL AS avatar_data, NULL AS phone_number, NULL AS timezone,
                               FALSE AS notif_billing, FALSE AS notif_usage, FALSE AS notif_marketing,
                               TRUE AS email_verified, tm.is_active, tm.created_at,
                               t.domain AS tenant_domain, t.name AS tenant_name, t.id AS tenant_id
                        FROM team_members tm
                        JOIN tenants t ON t.id = tm.tenant_id
                        WHERE tm.id = %s
                    """, (int(tm_id),))
                else:
                    cur.execute("""
                        SELECT c.id, c.first_name, c.last_name, c.email,
                               c.avatar_data, c.phone_number, c.timezone,
                               c.notif_billing, c.notif_usage, c.notif_marketing,
                               c.email_verified, c.is_active, c.created_at,
                               t.domain AS tenant_domain, t.name AS tenant_name, t.id AS tenant_id
                        FROM customers c
                        JOIN tenants t ON t.id = c.tenant_id
                        WHERE c.id = %s
                    """, (int(cid),))
                row = cur.fetchone()
                cur.close(); conn.close()
                _g._cached_portal_customer = row
            except Exception as e:
                print("⚠️ inject_current_customer error:", e)
                _g._cached_portal_customer = None
        return {"_portal_customer": _g._cached_portal_customer}

    @flask_app.context_processor
    def inject_support_whatsapp():
        """The "Need help?" button's WhatsApp number (PHIXTRA_SUPPORT_WHATSAPP,
        the one place it's set). Empty when unset, or on the PhiXtra Support
        account itself, so the button never tells support staff to message
        their own number."""
        digits = "".join(ch for ch in os.getenv("PHIXTRA_SUPPORT_WHATSAPP", "") if ch.isdigit())
        if not digits or not _session.get("portal_logged_in"):
            return {"support_whatsapp": ""}
        cid = _session.get("impersonate_customer_id") or _session.get("customer_id")
        if not hasattr(_g, "_cached_support_whatsapp"):
            _g._cached_support_whatsapp = digits
            try:
                from db import get_db_connection
                conn = get_db_connection()
                cur  = conn.cursor()
                cur.execute("""
                    SELECT 1 FROM wa_tenants w JOIN customers c ON c.tenant_id = w.tenant_id
                     WHERE c.id = %s AND regexp_replace(COALESCE(w.display_phone_number, ''), '\\D', '', 'g') = %s
                     LIMIT 1
                """, (int(cid or 0), digits))
                if cur.fetchone():
                    _g._cached_support_whatsapp = ""
                cur.close(); conn.close()
            except Exception as e:
                print("⚠️ inject_support_whatsapp error:", e)
        return {"support_whatsapp": _g._cached_support_whatsapp}

    @flask_app.context_processor
    def inject_has_woocommerce():
        """Detect whether tenant has a WooCommerce plugin connected.
        Tenants with key_type='paid' or 'trial' have a WooCommerce site (Profile B).
        WhatsApp-only tenants (key_type='whatsapp' only) get Profile A — simplified sidebar.
        """
        if not _session.get("portal_logged_in"):
            return {"_has_woocommerce": False}
        cid = _session.get("impersonate_customer_id") or _session.get("customer_id")
        if not cid:
            return {"_has_woocommerce": False}
        if not hasattr(_g, "_cached_has_woocommerce"):
            try:
                from db import get_db_connection
                conn = get_db_connection()
                cur  = conn.cursor()
                cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (int(cid),))
                row = cur.fetchone()
                if not row:
                    cur.close(); conn.close()
                    _g._cached_has_woocommerce = False
                else:
                    tenant_id = int(row[0])
                    cur.execute("""
                        SELECT 1 FROM api_keys
                        WHERE tenant_id=%s AND key_type IN ('paid','trial') AND is_active=TRUE
                        LIMIT 1
                    """, (tenant_id,))
                    _g._cached_has_woocommerce = cur.fetchone() is not None
                    cur.close(); conn.close()
            except Exception as e:
                print("⚠️ inject_has_woocommerce error:", e)
                _g._cached_has_woocommerce = False
        return {"_has_woocommerce": _g._cached_has_woocommerce}

    @flask_app.context_processor
    def inject_tenant_features():
        """Inject _tenant_features dict into every template so the nav can gate feature links."""
        if not _session.get("portal_logged_in"):
            return {"_tenant_features": {}}
        cid = _session.get("impersonate_customer_id") or _session.get("customer_id")
        if not cid:
            return {"_tenant_features": {}}
        if not hasattr(_g, "_cached_tenant_features"):
            try:
                import json as _json
                from db import get_db_connection
                conn = get_db_connection()
                cur  = conn.cursor()
                cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (int(cid),))
                row = cur.fetchone()
                if not row:
                    cur.close(); conn.close()
                    _g._cached_tenant_features = {}
                else:
                    cur.execute("SELECT features FROM tenants WHERE id=%s", (int(row[0]),))
                    feat_row = cur.fetchone()
                    cur.close(); conn.close()
                    raw = (feat_row or [None])[0]
                    if isinstance(raw, str):
                        _g._cached_tenant_features = _json.loads(raw) if raw else {}
                    elif isinstance(raw, dict):
                        _g._cached_tenant_features = dict(raw)
                    else:
                        _g._cached_tenant_features = {}
                    # WooCommerce Plugin features: the plan decides, not the
                    # stored flags (see PLUGIN_FEATURE_PLAN_KEYS).
                    from portal_routes import _plugin_features_for_tenant
                    _g._cached_tenant_features.update(_plugin_features_for_tenant(int(row[0])))
            except Exception as e:
                print("⚠️ inject_tenant_features error:", e)
                _g._cached_tenant_features = {}
        return {"_tenant_features": _g._cached_tenant_features}

    @flask_app.context_processor
    def inject_turnstile():
        return {"turnstile_site_key": os.getenv("TURNSTILE_SITE_KEY", "")}

    # Estate portal context processor (inject _re_tenant + _re_inbox_count)
    from portal_routes_estate import inject_re_tenant as _estate_ctx
    flask_app.context_processor(_estate_ctx)

    @flask_app.context_processor
    def inject_is_demo_tenant():
        """True when the logged-in customer belongs to a demo (ambassador sandbox) tenant."""
        if not _session.get("portal_logged_in"):
            return {"_is_demo_tenant": False}
        cid = _session.get("impersonate_customer_id") or _session.get("customer_id")
        if not cid:
            return {"_is_demo_tenant": False}
        if not hasattr(_g, "_cached_is_demo_tenant"):
            try:
                from db import get_db_connection
                conn = get_db_connection()
                cur  = conn.cursor()
                cur.execute("""
                    SELECT t.is_demo FROM tenants t
                    JOIN customers c ON c.tenant_id = t.id
                    WHERE c.id = %s
                """, (int(cid),))
                row = cur.fetchone()
                cur.close(); conn.close()
                _g._cached_is_demo_tenant = bool(row[0]) if row else False
            except Exception as e:
                print("⚠️ inject_is_demo_tenant error:", e)
                _g._cached_is_demo_tenant = False
        return {"_is_demo_tenant": _g._cached_is_demo_tenant}

    @flask_app.context_processor
    def inject_inbox_unread():
        """Count unread inbound WhatsApp messages for the sidebar badge."""
        if not _session.get('portal_logged_in'):
            return {'_inbox_unread_count': 0}
        cid = _session.get('impersonate_customer_id') or _session.get('customer_id')
        if not cid:
            return {'_inbox_unread_count': 0}
        last_seen = _session.get('inbox_last_seen')  # ISO string or None
        try:
            from db import get_db_connection
            import datetime
            conn = get_db_connection()
            cur  = conn.cursor()
            # Resolve tenant_id from customer
            cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (int(cid),))
            row = cur.fetchone()
            if not row:
                cur.close(); conn.close()
                return {'_inbox_unread_count': 0}
            tenant_id = int(row[0])
            if last_seen:
                cur.execute("""
                    SELECT COUNT(*) FROM wa_message_log
                    WHERE tenant_id=%s AND direction='inbound'
                      AND created_at > %s
                """, (tenant_id, last_seen))
            else:
                cur.execute("""
                    SELECT COUNT(*) FROM wa_message_log
                    WHERE tenant_id=%s AND direction='inbound'
                """, (tenant_id,))
            count = int((cur.fetchone() or [0])[0])
            cur.close(); conn.close()
            return {'_inbox_unread_count': count}
        except Exception as e:
            print("⚠️ inject_inbox_unread error:", e)
            return {'_inbox_unread_count': 0}

    @flask_app.context_processor
    def inject_campaign_review_pending():
        """Count pending Interested-reply reviews for the WhatsApp Campaigns
        sidebar badge (Campaign Intelligence — see
        project_wa_campaign_intelligence_proposal memory)."""
        if not _session.get('portal_logged_in'):
            return {'_campaign_review_pending_count': 0}
        cid = _session.get('impersonate_customer_id') or _session.get('customer_id')
        if not cid:
            return {'_campaign_review_pending_count': 0}
        try:
            from db import get_db_connection
            conn = get_db_connection()
            cur  = conn.cursor()
            cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (int(cid),))
            row = cur.fetchone()
            if not row:
                cur.close(); conn.close()
                return {'_campaign_review_pending_count': 0}
            tenant_id = int(row[0])
            cur.execute(
                "SELECT COUNT(*) FROM wa_campaign_reply_reviews WHERE tenant_id=%s AND status='pending'",
                (tenant_id,),
            )
            count = int((cur.fetchone() or [0])[0])
            cur.close(); conn.close()
            return {'_campaign_review_pending_count': count}
        except Exception as e:
            print("⚠️ inject_campaign_review_pending error:", e)
            return {'_campaign_review_pending_count': 0}

    return flask_app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5055, debug=False)
