"""
msg_encrypt_job.py — chat message words encrypted at rest (10 Nov 2026).

New messages are encrypted when saved (data_crypto, same data key as visitor
details). This script handles the messages saved before that:

  convert   encrypt every old plain message (safe to run again; skips done rows)
  check     count plain vs encrypted rows, and confirm every encrypted row opens
  undo      decrypt everything back to plain text (emergency only — saving new
            messages encrypted still needs the code switched back first)

Readers accept plain and encrypted text, so convert can run at any time after
the new code is live.
"""
import sys
import psycopg2
from dotenv import load_dotenv
import os

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
import data_crypto
from db import get_db_connection

# (table, id column, text column) — step 1: website chat (all platforms)
TABLES = [
    ("chat_messages", "id", "content"),
    ("re_chat_messages", "id", "content"),
    ("web_chat_replies", "id", "content"),
    ("chat_summaries", "ctid", "summary_text"),
    ("re_chat_summaries", "ctid", "summary_text"),
    # step 2: WhatsApp and Facebook
    ("wa_message_log", "id", "content"),
    ("fb_message_log", "id", "content"),
    ("re_wa_message_log", "id", "content"),
    # step 3: saved translations
    ("message_translations", "ctid", "translated"),
    ("sent_translations", "ctid", "original"),
    # hand-over details + staff alert previews (second plan, 10 Nov 2026)
    ("handoff_requests", "id", "visitor_name"),
    ("handoff_requests", "id", "visitor_email"),
    ("handoff_requests", "id", "whatsapp_number"),
    ("handoff_requests", "id", "visitor_message"),
    ("web_chat_alert_queue", "id", "label"),
    ("web_chat_alert_queue", "id", "preview"),
    ("chat_alerts", "id", "customer_label"),
    ("chat_alerts", "id", "preview"),
]


def _batches(cur, table, idc, col, where, size=500):
    last = None
    while True:
        if idc == "ctid":
            cur.execute(f"SELECT ctid::text, {col} FROM {table} WHERE {where} LIMIT %s", (size,))
        else:
            cur.execute(f"SELECT {idc}, {col} FROM {table} WHERE {where} "
                        + ("AND id > %s " if last is not None else "") + f"ORDER BY {idc} LIMIT %s",
                        ((last, size) if last is not None else (size,)))
        rows = cur.fetchall()
        if not rows:
            return
        yield rows
        if idc != "ctid":
            last = rows[-1][0]


def convert(undo=False):
    conn = get_db_connection(); cur = conn.cursor()
    for table, idc, col in TABLES:
        n = 0
        where = (f"{col} LIKE 'v1:%%'" if undo else f"{col} IS NOT NULL AND {col} <> '' AND {col} NOT LIKE 'v1:%%'")
        while True:
            done_any = False
            for rows in _batches(cur, table, idc, col, where):
                for rid, val in rows:
                    new = data_crypto.dec(val) if undo else data_crypto.enc(val)
                    if undo and new == "" and val:
                        raise SystemExit(f"{table} {rid}: cannot open — stopped, nothing more changed")
                    if not undo and data_crypto.dec(new) != val:
                        raise SystemExit(f"{table} {rid}: read-back mismatch — stopped")
                    cur.execute(f"UPDATE {table} SET {col}=%s WHERE {idc}=%s" + ("::tid" if idc == "ctid" else ""), (new, rid))
                    n += 1; done_any = True
                conn.commit()
                break   # ctid changes after UPDATE: re-select from the start
            if not done_any:
                break
        print(f"{'undo' if undo else 'convert'} {table}: {n} rows")
    cur.close(); conn.close()


def check():
    conn = get_db_connection(); cur = conn.cursor()
    bad_total = 0
    for table, idc, col in TABLES:
        cur.execute(f"""SELECT COUNT(*) FILTER (WHERE {col} LIKE 'v1:%%'),
                               COUNT(*) FILTER (WHERE {col} IS NOT NULL AND {col} <> '' AND {col} NOT LIKE 'v1:%%')
                          FROM {table}""")
        enc_n, plain_n = cur.fetchone()
        cur.execute(f"SELECT {col} FROM {table} WHERE {col} LIKE 'v1:%%'")
        bad = sum(1 for (v,) in cur.fetchall() if data_crypto.dec(v) == "")
        bad_total += bad
        print(f"{table}.{col}: encrypted {enc_n}, plain {plain_n}, cannot open {bad}")
    cur.close(); conn.close()
    return bad_total


if __name__ == "__main__":
    cmd = (sys.argv[1:] or ["check"])[0]
    if cmd == "convert":
        convert(); check()
    elif cmd == "undo":
        convert(undo=True); check()
    else:
        sys.exit(1 if check() else 0)
