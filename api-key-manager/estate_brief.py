"""KeyLand: AI brief before a call (2026-10-22). The AI backend writes it
(/estate-brief, estate/brief.py); this keeps the last one per lead so it isn't
paid for again until something new happens on the lead.

Table (new, created once):
  re_ai_briefs  one per lead: the brief, when, who asked, and the lead's
                activity fingerprint at that moment
"""
import json
import os

import psycopg2.extras

from db import get_db_connection

AI_URL = os.getenv("AI_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
_ready = False


def ensure_tables():
    global _ready
    if _ready:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('re_ai_briefs') IS NOT NULL")
        if not cur.fetchone()[0]:
            cur.execute("SET lock_timeout = '3s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_ai_briefs (
                contact_id INTEGER PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                brief JSONB NOT NULL,
                fingerprint TEXT,
                made_by VARCHAR(120),
                made_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        _ready = True
    except Exception as e:
        print(f"⚠️ [KL BRIEF] ensure_tables: {e}")
    finally:
        conn.close()


def fingerprint(tenant_id: int, contact_id: int) -> str:
    """Changes whenever something new happens on the lead."""
    conn = get_db_connection()
    cur = conn.cursor()
    parts = []
    for sql in ("SELECT MAX(id) FROM re_chat_messages WHERE tenant_id=%s AND customer_id=%s",
                "SELECT MAX(id) FROM re_interactions WHERE tenant_id=%s AND contact_id=%s",
                "SELECT MAX(id) FROM re_next_actions WHERE tenant_id=%s AND contact_id=%s",
                "SELECT MAX(updated_at)::text FROM re_buyer_land WHERE tenant_id=%s AND customer_id=%s",
                "SELECT MAX(updated_at)::text FROM re_customers WHERE tenant_id=%s AND id=%s",
                "SELECT MAX(id) FROM re_inspection_bookings WHERE tenant_id=%s AND customer_id=%s",
                "SELECT MAX(updated_at)::text FROM re_payment_milestones WHERE tenant_id=%s AND contact_id=%s"):
        try:
            cur.execute(sql, (tenant_id, contact_id))
            parts.append(str(cur.fetchone()[0]))
        except Exception:
            conn.rollback()
            parts.append("-")
    cur.close(); conn.close()
    return "|".join(parts)


def saved(tenant_id: int, contact_id: int):
    ensure_tables()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM re_ai_briefs WHERE contact_id=%s AND tenant_id=%s", (contact_id, tenant_id))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        row = dict(row)
        row["fresh"] = row["fingerprint"] == fingerprint(tenant_id, contact_id)
    return row


def make(tenant_id: int, contact_id: int, tz: str, who: str, http=None):
    """Ask the AI for a new brief and keep it. Returns the saved row or None."""
    import requests
    http = http or requests
    ensure_tables()
    fp = fingerprint(tenant_id, contact_id)
    try:
        r = http.post(AI_URL + "/estate-brief", json={"tenant_id": tenant_id, "customer_id": contact_id, "tz": tz}, timeout=120)
        data = r.json() if r.status_code == 200 else {}
    except Exception as e:
        print(f"⚠️ [KL BRIEF] lead={contact_id}: {e}")
        data = {}
    if not data.get("ok"):
        return None
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""INSERT INTO re_ai_briefs (contact_id, tenant_id, brief, fingerprint, made_by, made_at)
                   VALUES (%s,%s,%s,%s,%s,NOW())
                   ON CONFLICT (contact_id) DO UPDATE SET brief=EXCLUDED.brief, fingerprint=EXCLUDED.fingerprint,
                     made_by=EXCLUDED.made_by, made_at=NOW()""",
                (contact_id, tenant_id, json.dumps(data["brief"]), fp, who))
    conn.commit(); cur.close(); conn.close()
    return saved(tenant_id, contact_id)
