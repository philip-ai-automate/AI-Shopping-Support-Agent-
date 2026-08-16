"""
wa_product_cache_cleanup.py — deletes wa_product_cache rows untouched for
72+ hours. This table is ephemeral shopping-session context (product lists
shown, so a numbered reply like "1" can be resolved, plus "viewed in the
last 24h" history) — nothing in it is meant to be kept long-term.

Run hourly via cron:
  0 * * * * cd /root/phixtra-app/whatsapp-gateway && ./venv/bin/python3 wa_product_cache_cleanup.py >> /var/log/wa_product_cache_cleanup.log 2>&1
"""

from wa_db import get_db_connection

_MAX_AGE_HOURS = 72


def run():
    conn = get_db_connection()
    if not conn:
        print("⚠️ [CACHE-CLEANUP] no DB connection")
        return
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM wa_product_cache WHERE updated_at < NOW() - (%s * INTERVAL '1 hour')",
            (_MAX_AGE_HOURS,),
        )
        deleted = cur.rowcount
        conn.commit()
        print(f"🗑️ [CACHE-CLEANUP] deleted {deleted} row(s) older than {_MAX_AGE_HOURS}h")
    except Exception as e:
        print(f"⚠️ [CACHE-CLEANUP] error: {e}")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    run()
