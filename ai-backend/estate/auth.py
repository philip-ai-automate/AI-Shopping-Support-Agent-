"""Verify re_api_keys and return tenant + plan info for estate portal."""
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import psycopg2.extras
from db import get_db_connection


def verify_re_api_key(api_key: str) -> tuple:
    """Returns (tenant_dict, error_msg). tenant_dict is None on error."""
    if not api_key:
        return None, "API key required"

    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    conn = get_db_connection()
    if not conn:
        return None, "Database unavailable"

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT
                k.id                        AS api_key_id,
                k.tenant_id,
                k.is_active                 AS key_active,
                t.status                    AS tenant_status,
                t.email,
                t.business_name,
                t.system_prompt,
                t.plan_id,
                t.plan_period_start,
                t.trial_ends_at,
                t.wa_phone_number_id,
                t.wa_access_token,
                t.wa_waba_id,
                t.wa_display_phone,
                t.wa_app_secret,
                p.slug                      AS plan_slug,
                COALESCE(p.ai_messages_limit, 100) AS ai_messages_limit,
                p.overage_per_msg_ngn,
                p.feat_advanced_ai,
                p.feat_broadcasts,
                p.feat_follow_up,
                p.feat_full_reports,
                p.feat_multi_agents
            FROM re_api_keys k
            JOIN re_tenants  t ON t.id = k.tenant_id
            LEFT JOIN re_plans p ON p.id = t.plan_id
            WHERE k.api_key_hash = %s
        """, (key_hash,))
        row = cur.fetchone()
        cur.close()

        if not row:
            return None, "Invalid API key"
        if not row["key_active"]:
            return None, "API key is inactive"
        if row["tenant_status"] != "active":
            return None, "Account is suspended"

        return dict(row), None

    except Exception as e:
        print(f"⚠️ [ESTATE AUTH] verify_re_api_key error: {e}")
        return None, "Auth error"
    finally:
        try:
            conn.close()
        except Exception:
            pass
