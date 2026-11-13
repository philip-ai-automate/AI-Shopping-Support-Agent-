"""chat_products_shown.py — "AI Agent showed …" lines on a buyer's lead (13 Nov 2026).

User decision: when a website chat is linked to a lead, the lead's History
gets one line for each AI Agent reply that showed products, so the team sees
what the buyer was offered, e.g.
  "AI Agent showed in the website chat: The Complete Snowboard, The Collection Snowboard: Liquid"

The product titles come from the hidden <<<PHIXTRA_PRODUCTS:[...]>>> tag the
AI Agent's reply is saved with (the chat box turns it into product cards).
Chat messages are encrypted at rest, so they are read and decrypted here, only
for chats linked to a lead (lead_qual_sources 'chat:<session_id>').

Every 5 minutes, one portal worker at a time (advisory lock 771113). Each
chat remembers the last message it was read up to (chat_products_logged), so
a line is never written twice. Looks back 30 days.
"""

import json
import re
import threading
import time

import psycopg2.extras

from db import get_db_connection

CHECK_EVERY = 300
LOOK_BACK = "30 days"
_TAG = re.compile(r"<<<PHIXTRA_PRODUCTS:(\[.*?\])>>>", re.S)


def ensure_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS chat_products_logged (
                       tenant_id   INTEGER NOT NULL,
                       source_key  VARCHAR(120) NOT NULL,
                       last_msg_id BIGINT NOT NULL DEFAULT 0,
                       updated_at  TIMESTAMPTZ DEFAULT NOW(),
                       PRIMARY KEY (tenant_id, source_key))""")


def titles_in(text: str) -> list:
    m = _TAG.search(text or "")
    if not m:
        return []
    try:
        return [str(t).strip() for t in json.loads(m.group(1)) if str(t).strip()][:6]
    except Exception:
        return []


def _history(cur, lead_id: int, note: str):
    cur.execute("SELECT stage FROM merchant_pipeline_leads WHERE id=%s", (lead_id,))
    st = (cur.fetchone() or {}).get("stage")
    cur.execute("""INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                   VALUES (%s,%s,%s,%s,%s)""", (lead_id, st, st, "AI Agent", note[:2000]))


def run_once() -> int:
    """Write any new lines. Returns how many were written."""
    import data_crypto
    written = 0
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        ensure_table(cur)
        cur.execute("SELECT to_regclass('lead_qual_sources') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            conn.commit()
            return 0
        # Website chats linked to a live lead, with AI Agent replies newer than
        # what was already read.
        cur.execute(f"""
            SELECT q.tenant_id, q.source_key, q.lead_id, COALESCE(g.last_msg_id, 0) AS seen
              FROM lead_qual_sources q
              JOIN merchant_pipeline_leads l ON l.id = q.lead_id AND l.tenant_id = q.tenant_id
              LEFT JOIN chat_products_logged g ON g.tenant_id = q.tenant_id AND g.source_key = q.source_key
             WHERE q.source_key LIKE 'chat:%%' AND q.lead_id IS NOT NULL
               AND q.source_key NOT LIKE 'chat:wa-%%'
               AND EXISTS (SELECT 1 FROM chat_messages m
                            WHERE m.tenant_id = q.tenant_id AND m.session_id = substr(q.source_key, 6)
                              AND m.role = 'assistant' AND m.id > COALESCE(g.last_msg_id, 0)
                              AND m.created_at > NOW() - INTERVAL '{LOOK_BACK}')
             LIMIT 200""")
        for s in cur.fetchall():
            sid = s["source_key"][5:]
            cur.execute(f"""SELECT id, content FROM chat_messages
                             WHERE tenant_id=%s AND session_id=%s AND role='assistant' AND id > %s
                               AND created_at > NOW() - INTERVAL '{LOOK_BACK}'
                             ORDER BY id LIMIT 200""", (s["tenant_id"], sid, s["seen"]))
            last = s["seen"]
            for m in cur.fetchall():
                last = max(last, m["id"])
                names = titles_in(data_crypto.dec(m["content"]) or "")
                if names:
                    _history(cur, s["lead_id"], "AI Agent showed in the website chat: " + ", ".join(names))
                    written += 1
            cur.execute("""INSERT INTO chat_products_logged (tenant_id, source_key, last_msg_id, updated_at)
                           VALUES (%s,%s,%s,NOW())
                           ON CONFLICT (tenant_id, source_key) DO UPDATE SET last_msg_id=EXCLUDED.last_msg_id, updated_at=NOW()""",
                        (s["tenant_id"], s["source_key"], last))
        conn.commit()
        return written
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


def _loop():
    time.sleep(120)
    while True:
        lock = None
        try:
            lock = get_db_connection(); lock.autocommit = True
            lc = lock.cursor()
            lc.execute("SELECT pg_try_advisory_lock(771113)")
            if lc.fetchone()[0]:      # one portal worker at a time
                try:
                    run_once()
                finally:
                    lc.execute("SELECT pg_advisory_unlock(771113)")
        except Exception as e:
            print("⚠️ chat_products_shown loop:", e)
        finally:
            if lock:
                try:
                    lock.close()
                except Exception:
                    pass
        time.sleep(CHECK_EVERY)


def start():
    if not getattr(threading, "_phixtra_chat_products_shown_started", False):
        threading._phixtra_chat_products_shown_started = True  # type: ignore[attr-defined]
        threading.Thread(target=_loop, daemon=True).start()
