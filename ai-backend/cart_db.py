"""
cart_db.py  — Database helpers for the Intelligent Cart Revenue Recovery feature.

Tables used (created by portal_migrations.py):
  cart_events        — raw event log per session
  abandonment_queue  — one active recovery row per (tenant_id, session_id)
  recovery_log       — immutable audit trail of all recovery actions

This module NEVER raises to the caller — failures are printed and swallowed.
"""
import json as _json
import psycopg2.extras
from db import get_db_connection


# ──────────────────────────────────────────────────────────────────────────────
# CART EVENTS
# ──────────────────────────────────────────────────────────────────────────────

def log_cart_event(
    tenant_id: int,
    session_id: str,
    event_type: str,
    cart_value: float | None,
    cart_items: list | None,
    page_url: str | None,
    customer_email: str | None,
) -> int | None:
    """
    Insert a single cart event row.
    Returns the new row id, or None on failure.
    """
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor()
    try:
        items_json = _json.dumps(cart_items) if cart_items is not None else None
        cur.execute(
            """
            INSERT INTO cart_events
                (tenant_id, session_id, event_type, cart_value, cart_items, page_url, customer_email)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (tenant_id, session_id, event_type, cart_value, items_json, page_url, customer_email),
        )
        conn.commit()
        row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception as e:
        print("⚠️ log_cart_event failed:", e)
        return None
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


def get_session_events(tenant_id: int, session_id: str) -> list[dict]:
    """
    Return all cart_events rows for a (tenant_id, session_id) pair,
    ordered by created_at ascending. Used to compute cumulative intent score.
    """
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT event_type, cart_value, cart_items, customer_email, created_at
            FROM cart_events
            WHERE tenant_id = %s AND session_id = %s
            ORDER BY created_at ASC
            """,
            (tenant_id, session_id),
        )
        return [dict(r) for r in (cur.fetchall() or [])]
    except Exception as e:
        print("⚠️ get_session_events failed:", e)
        return []
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


# ──────────────────────────────────────────────────────────────────────────────
# ABANDONMENT QUEUE
# ──────────────────────────────────────────────────────────────────────────────

def upsert_abandonment_queue(
    tenant_id: int,
    session_id: str,
    intent_score: int,
    priority: str,
    cart_value: float | None,
    cart_items: list | None,
    customer_email: str | None,
) -> int | None:
    """
    Insert or update the abandonment_queue row for (tenant_id, session_id).

    Rules:
      - No existing row  →  INSERT with status='pending'
      - Existing, status='pending'  →  UPDATE score/priority/cart data
      - Existing, status in ('in_progress', 'recovered', 'expired')  →  do not touch,
        just return the existing id so the caller knows the queue_id
    Returns queue row id or None on DB failure.
    """
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur2 = conn.cursor()
    try:
        items_json = _json.dumps(cart_items) if cart_items is not None else None

        cur.execute(
            "SELECT id, status FROM abandonment_queue WHERE tenant_id=%s AND session_id=%s",
            (tenant_id, session_id),
        )
        existing = cur.fetchone()

        if not existing:
            cur2.execute(
                """
                INSERT INTO abandonment_queue
                    (tenant_id, session_id, intent_score, priority,
                     cart_value, cart_items, customer_email,
                     status, touches_sent, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', 0, NOW() + INTERVAL '48 hours')
                RETURNING id
                """,
                (tenant_id, session_id, intent_score, priority,
                 cart_value, items_json, customer_email),
            )
            conn.commit()
            row = cur2.fetchone()
            return int(row[0]) if row else None

        queue_id = int(existing["id"])
        status   = existing.get("status") or "pending"

        if status == "pending":
            cur2.execute(
                """
                UPDATE abandonment_queue
                SET intent_score    = %s,
                    priority        = %s,
                    cart_value      = COALESCE(%s, cart_value),
                    cart_items      = COALESCE(%s, cart_items),
                    customer_email  = COALESCE(%s, customer_email),
                    updated_at      = NOW()
                WHERE id = %s
                """,
                (intent_score, priority, cart_value, items_json, customer_email, queue_id),
            )
            conn.commit()

        return queue_id

    except Exception as e:
        print("⚠️ upsert_abandonment_queue failed:", e)
        return None
    finally:
        for obj in (cur, cur2):
            try: obj.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass


def get_queue_row(queue_id: int) -> dict | None:
    """Fetch a single abandonment_queue row by id. Returns None on failure."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM abandonment_queue WHERE id=%s", (queue_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        print("⚠️ get_queue_row failed:", e)
        return None
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


def get_queue_row_by_session(tenant_id: int, session_id: str) -> dict | None:
    """Fetch abandonment_queue row by (tenant_id, session_id). Returns None on failure."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT * FROM abandonment_queue WHERE tenant_id=%s AND session_id=%s",
            (tenant_id, session_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        print("⚠️ get_queue_row_by_session failed:", e)
        return None
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


def mark_queue_status(queue_id: int, status: str) -> None:
    """
    Update status field on an abandonment_queue row.
    Valid values: 'pending', 'in_progress', 'recovered', 'expired'
    """
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE abandonment_queue SET status=%s, updated_at=NOW() WHERE id=%s",
            (status, queue_id),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ mark_queue_status failed:", e)
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


def increment_touches(queue_id: int) -> None:
    """Increment touches_sent counter by 1."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE abandonment_queue SET touches_sent = touches_sent + 1, updated_at=NOW() WHERE id=%s",
            (queue_id,),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ increment_touches failed:", e)
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


def expire_stale_queue_entries() -> int:
    """
    Mark as 'expired' any rows where expires_at < NOW() and status is
    'pending' or 'in_progress'. Called at startup and on each /cart-event call.
    Returns count of rows updated.
    """
    conn = get_db_connection()
    if not conn:
        return 0
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE abandonment_queue
            SET status='expired', updated_at=NOW()
            WHERE expires_at < NOW()
              AND status IN ('pending', 'in_progress')
            """,
        )
        conn.commit()
        return cur.rowcount
    except Exception as e:
        print("⚠️ expire_stale_queue_entries failed:", e)
        return 0
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


# ──────────────────────────────────────────────────────────────────────────────
# RECOVERY LOG
# ──────────────────────────────────────────────────────────────────────────────

def log_recovery_action(
    queue_id: int,
    action_type: str,
    channel: str,
    message_preview: str | None = None,
) -> None:
    """
    Append an immutable record to recovery_log.
    action_type examples: 'popup_queued', 'email_sent', 'final_email_sent', 'recovered'
    channel examples: 'widget', 'email', 'sms'
    """
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO recovery_log (queue_id, action_type, channel, message_preview)
            VALUES (%s, %s, %s, %s)
            """,
            (queue_id, action_type, channel, message_preview),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ log_recovery_action failed:", e)
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


# ──────────────────────────────────────────────────────────────────────────────
# ADMIN DASHBOARD QUERIES
# ──────────────────────────────────────────────────────────────────────────────

def get_recovery_queue_for_admin(
    tenant_id: int | None = None,
    status_filter: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    Fetch abandonment_queue rows for the admin dashboard.
    If tenant_id is None, returns all tenants (super-admin view).
    If status_filter is provided (e.g. 'pending'), filters by status.
    """
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        params: list = []
        where_parts: list[str] = []

        if tenant_id is not None:
            where_parts.append("q.tenant_id = %s")
            params.append(tenant_id)

        if status_filter:
            where_parts.append("q.status = %s")
            params.append(status_filter)

        where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        params.append(limit)

        cur.execute(
            f"""
            SELECT q.id, q.tenant_id, q.session_id, q.intent_score, q.priority,
                   q.cart_value, q.customer_email, q.status, q.touches_sent,
                   q.expires_at, q.created_at, q.updated_at,
                   t.name AS tenant_name, t.domain
            FROM abandonment_queue q
            LEFT JOIN tenants t ON t.id = q.tenant_id
            {where_clause}
            ORDER BY q.updated_at DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in (cur.fetchall() or [])]
    except Exception as e:
        print("⚠️ get_recovery_queue_for_admin failed:", e)
        return []
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


def get_recovery_stats(tenant_id: int | None = None) -> dict:
    """
    Return summary counts for the admin dashboard KPI cards.
    Returns dict with keys: total, pending, in_progress, recovered, expired.
    """
    conn = get_db_connection()
    if not conn:
        return {"total": 0, "pending": 0, "in_progress": 0, "recovered": 0, "expired": 0}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        where = "WHERE q.tenant_id = %s" if tenant_id is not None else ""
        params = [tenant_id] if tenant_id is not None else []
        cur.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='pending'     THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
                SUM(CASE WHEN status='recovered'   THEN 1 ELSE 0 END) AS recovered,
                SUM(CASE WHEN status='expired'     THEN 1 ELSE 0 END) AS expired
            FROM abandonment_queue q
            {where}
            """,
            params,
        )
        row = dict(cur.fetchone() or {})
        return {
            "total":       int(row.get("total")       or 0),
            "pending":     int(row.get("pending")     or 0),
            "in_progress": int(row.get("in_progress") or 0),
            "recovered":   int(row.get("recovered")   or 0),
            "expired":     int(row.get("expired")     or 0),
        }
    except Exception as e:
        print("⚠️ get_recovery_stats failed:", e)
        return {"total": 0, "pending": 0, "in_progress": 0, "recovered": 0, "expired": 0}
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
