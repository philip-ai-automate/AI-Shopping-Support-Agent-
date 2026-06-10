import os
import json
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=os.getenv("PG_HOST", "localhost"),
            port=int(os.getenv("PG_PORT", "5432")),
            user=os.getenv("PG_USER"),
            password=os.getenv("PG_PASSWORD"),
            dbname=os.getenv("PG_DB"),
        )
        return conn
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
