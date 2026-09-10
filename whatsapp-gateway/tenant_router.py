import psycopg2
import psycopg2.extras

from wa_db import get_db_connection


def get_tenant_by_api_key(api_key: str) -> dict | None:
    """
    Look up a tenant by phixtra_api_key — used by proactive endpoints
    where we have an api_key but no phone_number_id.
    Returns the first active matching row.
    """
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT tenant_id, phone_number_id, phixtra_api_key, access_token, verify_token, waba_id, typing_ack_text
            FROM wa_tenants
            WHERE phixtra_api_key = %s AND active = TRUE
            LIMIT 1
            """,
            (api_key,),
        )
        return cur.fetchone()
    except Exception as e:
        print("⚠️ get_tenant_by_api_key error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def get_tenant_by_phone_number_id(phone_number_id: str) -> dict | None:
    """
    Look up the tenant record for an incoming Meta webhook by phone_number_id.
    Returns the full tenant row or None if not found / inactive.
    """
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT wt.tenant_id, wt.phixtra_api_key, wt.access_token, wt.verify_token, wt.waba_id,
                   wt.app_secret, wt.typing_ack_text, wt.agent_id,
                   COALESCE(t.ai_enabled, TRUE) AS ai_enabled,
                   COALESCE(t.campaign_reply_auto_actions, TRUE) AS campaign_reply_auto_actions
            FROM wa_tenants wt
            JOIN tenants t ON t.id = wt.tenant_id
            WHERE wt.phone_number_id = %s AND wt.active = TRUE
            """,
            (phone_number_id,),
        )
        return cur.fetchone()
    except Exception as e:
        print("⚠️ get_tenant_by_phone_number_id error:", e)
        return None
    finally:
        cur.close()
        conn.close()
