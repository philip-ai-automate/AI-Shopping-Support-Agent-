import os
import json
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

def _label_actor_options():
    """Tells the database who is asking, so label_visible() (see
    portal_migrations) only lets a person see business labels, their own
    private labels and group labels they're in. None outside a web request
    (background jobs never list labels by visibility)."""
    try:
        from flask import has_request_context, session
        if not has_request_context():
            return None
        tm = session.get("team_member_id")
        if tm:
            opts = f"-c phixtra.actor_key=team:{int(tm)} -c phixtra.actor_member={int(tm)}"
            dept = session.get("team_member_dept_id")
            return opts + (f" -c phixtra.actor_dept={int(dept)}" if dept else "")
        cid = session.get("impersonate_customer_id") or session.get("customer_id")
        if not cid:
            return None
        # A PhiXtra admin viewing the account: never recorded as the owner's own work
        imp = " -c phixtra.impersonating=1" if session.get("impersonate_customer_id") else ""
        return f"-c phixtra.actor_key=owner:{int(cid)}{imp}"
    except Exception:
        return None


# Database updates (run_migrations.py) wait at most 3 seconds for a table lock,
# then give up and are tried again, so they can't freeze the portal (16 Nov 2026).
import contextlib as _ctxlib
import contextvars as _ctxvars
_migrating = _ctxvars.ContextVar("phx_migrating", default=False)


@_ctxlib.contextmanager
def migration_lock_limit():
    tok = _migrating.set(True)
    try:
        yield
    finally:
        _migrating.reset(tok)


def _conn_options():
    # A connection left open mid-transaction for 5 minutes is closed by the
    # database, so it can't block everyone else (16 Nov 2026). 5 minutes, not 1:
    # some jobs wait on the AI Agent / Shopify with a transaction open.
    opts = ["-c idle_in_transaction_session_timeout=300s"]
    if _migrating.get():
        opts.append("-c lock_timeout=3s")
    label = _label_actor_options()
    if label:
        opts.append(label)
    return " ".join(opts)


def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=os.getenv("PG_HOST", "localhost"),
            port=int(os.getenv("PG_PORT", "5432")),
            user=os.getenv("PG_USER"),
            password=os.getenv("PG_PASSWORD"),
            dbname=os.getenv("PG_DB"),
            options=_conn_options(),
        )
        return conn
    except Exception as e:
        print("❌ Database connection error:", e)
        return None

def insert_audit_log(
    admin_username: str | None = None,
    action: str = "",
    tenant_id: int | None = None,
    website: str | None = None,
    key_type: str | None = None,
    api_key_id: int | None = None,
    api_key_last4: str | None = None,
    api_key_plain: str | None = None,
    details: dict | None = None,
):
    """Write an audit event. Never raises to the caller (to avoid breaking the GUI)."""
    api_key_plain = None  # never store full API keys in the log (last 4 only)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        details_json = json.dumps(details or {}, ensure_ascii=False)
        cur.execute(
            """
            INSERT INTO audit_logs
              (admin_username, action, tenant_id, website, key_type, api_key_id, api_key_last4, api_key_plain, details)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (admin_username, action, tenant_id, website, key_type, api_key_id, api_key_last4, api_key_plain, details_json),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        try:
            if 'cur' in locals(): cur.close()
        except Exception:
            pass
        try:
            if 'conn' in locals(): conn.close()
        except Exception:
            pass
