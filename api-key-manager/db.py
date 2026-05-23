import os
import json
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv

load_dotenv()

def get_db_connection():
    # Simple helper used by both GUI and backend tools
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
    )

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
        # Intentionally swallow errors: audit logs should never block core operations
        try:
            if 'cur' in locals(): cur.close()
        except Exception:
            pass
        try:
            if 'conn' in locals(): conn.close()
        except Exception:
            pass
