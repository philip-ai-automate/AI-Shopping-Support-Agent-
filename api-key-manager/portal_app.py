"""
portal_app.py — Phase 1 portal entry point (portal.phixtra.com)
This file only wires blueprints together. No business logic here.
app.py (keys.phixtra.com) is completely separate and untouched.
"""
import os
import psycopg2.extras
from flask import Flask
from dotenv import load_dotenv

load_dotenv()


def create_app():
    flask_app = Flask(__name__, template_folder="templates", static_folder="static")
    flask_app.secret_key = os.getenv("PORTAL_SECRET_KEY", "change-this-secret-in-env")

    from portal_migrations import ensure_portal_tables
    from portal_routes import portal_bp
    from portal_admin_routes import portal_admin_bp
    from portal_facebook_routes import facebook_bp
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
    flask_app.register_blueprint(ambassador_bp)
    flask_app.register_blueprint(school_bp, url_prefix="/school")
    flask_app.register_blueprint(estate_bp)

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

    # ── Global template context: inject current customer so every template,
    #    including base.html, can access avatar_data, first_name, etc.
    from flask import session as _session, g as _g

    @flask_app.context_processor
    def inject_current_customer():
        """Make `_portal_customer` available in every template when logged in."""
        if not _session.get("portal_logged_in"):
            return {"_portal_customer": None}
        cid = _session.get("impersonate_customer_id") or _session.get("customer_id")
        if not cid:
            return {"_portal_customer": None}
        # Cache on g so we only hit the DB once per request
        if not hasattr(_g, "_cached_portal_customer"):
            try:
                from db import get_db_connection
                conn = get_db_connection()
                cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("""
                    SELECT c.id, c.first_name, c.last_name, c.email,
                           c.avatar_data, c.phone_number, c.timezone,
                           c.notif_billing, c.notif_usage, c.notif_marketing,
                           c.email_verified, c.is_active, c.created_at,
                           t.domain AS tenant_domain, t.name AS tenant_name
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
                        _g._cached_tenant_features = raw
                    else:
                        _g._cached_tenant_features = {}
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

    return flask_app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5055, debug=False)
