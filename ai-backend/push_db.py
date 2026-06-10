"""
push_db.py  — Database helpers for Web Push Notification subscriptions.

Table used (created by portal_migrations.py):
  push_subscriptions — stores each visitor's browser push subscription
                       keyed by (tenant_id, session_id)

This module NEVER raises to the caller — failures are printed and swallowed.
"""
import psycopg2.extras
from db import get_db_connection


def save_push_subscription(
    tenant_id: int,
    session_id: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None = None,
) -> int | None:
    """
    Insert a push subscription if one with the same p256dh key doesn't already
    exist for this (tenant_id, session_id) pair.

    Returns:
        int   — the row id of the new (or existing) subscription
        None  — on DB error
    """
    conn = get_db_connection()
    if not conn:
        return None
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur2 = conn.cursor()
    try:
        p256dh_key = p256dh[:255]
        cur.execute(
            """
            SELECT id FROM push_subscriptions
            WHERE tenant_id = %s AND session_id = %s AND p256dh = %s
            LIMIT 1
            """,
            (tenant_id, session_id, p256dh_key),
        )
        existing = cur.fetchone()
        if existing:
            return int(existing["id"])

        cur2.execute(
            """
            INSERT INTO push_subscriptions
                (tenant_id, session_id, endpoint, p256dh, auth, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (tenant_id, session_id, endpoint, p256dh, auth, user_agent),
        )
        conn.commit()
        row = cur2.fetchone()
        return int(row[0]) if row else None

    except Exception as e:
        print("⚠️ save_push_subscription failed:", e)
        return None
    finally:
        for obj in (cur, cur2):
            try: obj.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass


def get_push_subscriptions_for_session(
    tenant_id: int,
    session_id: str,
) -> list[dict]:
    """
    Return all push subscriptions for a (tenant_id, session_id) pair.
    Used by cart_recovery.py to send Touch 1.5 push notifications.

    Returns list of dicts with keys: endpoint, p256dh, auth
    Returns [] on error or no subscriptions.
    """
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT endpoint, p256dh, auth
            FROM push_subscriptions
            WHERE tenant_id = %s AND session_id = %s
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (tenant_id, session_id),
        )
        return [dict(r) for r in (cur.fetchall() or [])]
    except Exception as e:
        print("⚠️ get_push_subscriptions_for_session failed:", e)
        return []
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
