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

    # Run DB migrations on startup (all idempotent — safe)
    ensure_portal_tables()

    flask_app.register_blueprint(portal_bp)
    flask_app.register_blueprint(portal_admin_bp, url_prefix="/admin")

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
