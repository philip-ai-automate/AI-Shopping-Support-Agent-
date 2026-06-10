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
