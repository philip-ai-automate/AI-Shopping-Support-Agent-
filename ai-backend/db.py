import os
import json
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv

load_dotenv()

def get_db_connection():
    try:
        conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
        )
        return conn
    except Error as e:
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
