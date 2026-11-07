"""
chat_visitors_job.py — buyer details kept from the chat box (7 Nov 2026, Shopify 7A item 9).

  python3 chat_visitors_job.py            nightly: delete chat box details 90 days after the chat
  python3 chat_visitors_job.py convert    one-off: encrypt old plain rows with the data key, and move
                                          Shopify trade contact names off the old portal login key
  python3 chat_visitors_job.py check      counts only (nothing changed)

The details are already copied onto the lead when one is made, so the chat
row is only needed while the chat is live.
"""
import sys

from dotenv import load_dotenv
load_dotenv()

import data_crypto
from db import get_db_connection

KEEP_DAYS = 90
FIELDS = ("name", "email", "phone", "company")


def _has(cur, table):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    return cur.fetchone()[0]


def check(cur):
    out = {}
    if _has(cur, "chat_visitors"):
        cur.execute("SELECT name, email, phone, company FROM chat_visitors")
        rows = cur.fetchall()
        plain = sum(1 for r in rows if any(x and not data_crypto.is_enc(x) for x in r))
        out["chat_visitors"] = {"rows": len(rows), "plain": plain}
        cur.execute(f"SELECT count(*) FROM chat_visitors WHERE updated_at < NOW() - INTERVAL '{KEEP_DAYS} days'")
        out["chat_visitors"]["older_than_90_days"] = cur.fetchone()[0]
    if _has(cur, "shopify_b2b_contacts"):
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE name_enc LIKE 'v1:%%'),
                              count(*) FILTER (WHERE name_enc IS NOT NULL AND name_enc NOT LIKE 'v1:%%')
                         FROM shopify_b2b_contacts""")
        n, new, old = cur.fetchone()
        out["shopify_b2b_contacts"] = {"rows": n, "data_key": new, "old_key": old}
    return out


def convert(cur):
    data_crypto.enc("key check")          # stops here if the key file is missing
    done = {"chat_visitors": 0, "shopify_names": 0, "shopify_unreadable": 0}
    if _has(cur, "chat_visitors"):
        for c in FIELDS:
            cur.execute(f"ALTER TABLE chat_visitors ALTER COLUMN {c} TYPE TEXT")
        cur.execute("SELECT tenant_id, session_id, name, email, phone, company FROM chat_visitors FOR UPDATE")
        for t, sid, *vals in cur.fetchall():
            if all(not v or data_crypto.is_enc(v) for v in vals):
                continue
            cur.execute("UPDATE chat_visitors SET name=%s, email=%s, phone=%s, company=%s "
                        "WHERE tenant_id=%s AND session_id=%s",
                        (*[data_crypto.enc(v) for v in vals], t, sid))
            done["chat_visitors"] += 1
    if _has(cur, "shopify_b2b_contacts"):
        import shopify_b2b
        cur.execute("""SELECT shop, contact_id, name_enc FROM shopify_b2b_contacts
                        WHERE name_enc IS NOT NULL AND name_enc NOT LIKE 'v1:%%' FOR UPDATE""")
        for shop, cid, old in cur.fetchall():
            name = shopify_b2b._dec(old)
            if not name:
                done["shopify_unreadable"] += 1
                continue
            cur.execute("UPDATE shopify_b2b_contacts SET name_enc=%s WHERE shop=%s AND contact_id=%s",
                        (data_crypto.enc(name), shop, cid))
            done["shopify_names"] += 1
    return done


def nightly(cur):
    if not _has(cur, "chat_visitors"):
        return 0
    cur.execute(f"DELETE FROM chat_visitors WHERE updated_at < NOW() - INTERVAL '{KEEP_DAYS} days'")
    return cur.rowcount


def main():
    cmd = (sys.argv[1:] or ["nightly"])[0]
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if cmd == "check":
            print(check(cur))
        elif cmd == "convert":
            print("before:", check(cur))
            print("converted:", convert(cur))
            print("after:", check(cur))
            conn.commit()
        elif cmd == "nightly":
            n = nightly(cur)
            conn.commit()
            if n:
                print(f"chat_visitors: deleted {n} older than {KEEP_DAYS} days")
        else:
            print(__doc__)
        cur.close()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
