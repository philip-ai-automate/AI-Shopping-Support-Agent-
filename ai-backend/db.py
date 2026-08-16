import os
import json
import queue
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# Idle-connection pool: a bounded queue of already-open, already-authenticated
# connections. get_db_connection() reuses one if available, otherwise opens a
# fresh one (no blocking/waiting — a full queue just means "reuse less this
# time", never a hard failure). This intentionally avoids psycopg2's own
# ThreadedConnectionPool: that pool keys connections by calling-thread-identity
# and deadlocks when growing past its pre-warmed minimum under a custom
# connection_factory (reproduced independently while building this).
_IDLE_POOL_SIZE = int(os.getenv("PG_POOL_MAX", "20"))
_idle_pool = queue.Queue(maxsize=_IDLE_POOL_SIZE)


def _connect_raw():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "5432")),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
        dbname=os.getenv("PG_DB"),
    )


class _PooledConnWrapper:
    """Delegates everything to a real psycopg2 connection except close(),
    which returns the connection to the idle pool instead of tearing it
    down — every existing `conn.close()` call site keeps working unmodified
    while the underlying connection gets reused instead of rebuilt."""

    __slots__ = ("_conn", "_released")

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_released", False)

    def close(self):
        if object.__getattribute__(self, "_released"):
            return
        object.__setattr__(self, "_released", True)
        conn = object.__getattribute__(self, "_conn")
        try:
            if conn.closed:
                return
            conn.rollback()
            _idle_pool.put_nowait(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)


def get_db_connection():
    try:
        conn = None
        while True:
            try:
                candidate = _idle_pool.get_nowait()
            except queue.Empty:
                conn = _connect_raw()
                break
            if candidate.closed:
                continue  # dead idle connection (e.g. server-side restart) — discard, try next
            conn = candidate
            break
        return _PooledConnWrapper(conn)
    except Exception as e:
        print("❌ Database connection error:", e)
        return None


def insert_audit_log(action: str, tenant_id=None, website=None, key_type=None, api_key_id=None,
                     api_key_last4=None, api_key_plain=None, admin_username=None, details=None):
    """
    Lightweight audit logger used by BOTH:
    - keys.phixtra.com (Flask GUI)
    - API backend (FastAPI) for automated trial events

    It never throws: failures are printed and ignored so your API does not crash.
    """
    try:
        conn = get_db_connection()
        if not conn:
            return
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO audit_logs
              (admin_username, action, tenant_id, website, key_type, api_key_id, api_key_last4, api_key_plain, details)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (admin_username, action, tenant_id, website, key_type, api_key_id, api_key_last4, api_key_plain,
             json.dumps(details) if isinstance(details, (dict, list)) else details)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("⚠️ audit log failed:", e)
