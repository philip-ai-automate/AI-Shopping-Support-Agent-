import bcrypt
import hashlib
import psycopg2.extras
from datetime import datetime, timedelta, timezone

from db import get_db_connection, insert_audit_log
from billing import ensure_billing_tables, get_token_balance

TRIAL_DAYS = 14


def _utcnow():
    return datetime.now(timezone.utc)


def _to_pg_dt(dt: datetime):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def verify_api_key(api_key: str):
    """
    Verifies API key against api_keys table.

    Adds:
      - trial activation (first use starts the 14-day window)
      - trial expiry enforcement
      - token-limit enforcement (trial)
      - PAID credits enforcement (tenant-level)

    Returns (tenant_row, None) if valid else (None, error_message)
    """

    ensure_billing_tables()

    conn = get_db_connection()
    if not conn:
        return None, "Database unavailable"

    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    query = """
        SELECT
            ak.id AS api_key_id,
            ak.api_key_hash,
            ak.is_active,
            ak.website,
            ak.key_type,
            ak.trial_activated_at,
            ak.trial_expires_at,
            ak.token_limit,
            ak.tokens_used,

            t.id AS tenant_id,
            t.name,
            t.domain,
            COALESCE(ta.system_prompt, t.system_prompt) AS system_prompt,
            t.status,
            t.features
        FROM api_keys ak
        JOIN tenants t ON ak.tenant_id = t.id
        LEFT JOIN tenant_agents ta ON ta.tenant_id = t.id AND ta.is_active = TRUE
        WHERE ak.is_active = TRUE
          AND t.status IN ('active', 'pending')
    """

    def _first_bcrypt_match(rows):
        for row in rows:
            try:
                if bcrypt.checkpw(api_key.encode("utf-8"), row["api_key_hash"].encode("utf-8")):
                    return dict(row)
            except Exception:
                continue
        return None

    # Find the key in one step by its quick-lookup label (sha256, filled by
    # the api_keys_set_lookup trigger) and bcrypt-check just that key. This
    # used to bcrypt-check every active key on the platform in turn, ~20s
    # per message with 86 keys (2026-10-01). Keys without a label yet (no
    # readable copy stored) fall back to the old one-by-one check, and get
    # their label on first use.
    lookup = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    cursor.execute(query + " AND ak.api_key_lookup = %s", (lookup,))
    matched_row = _first_bcrypt_match(cursor.fetchall())

    if not matched_row:
        cursor.execute(query + " AND ak.api_key_lookup IS NULL")
        matched_row = _first_bcrypt_match(cursor.fetchall())
        if matched_row:
            try:
                cursor.execute("UPDATE api_keys SET api_key_lookup=%s WHERE id=%s",
                               (lookup, matched_row["api_key_id"]))
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"⚠️ verify_api_key: could not store lookup label: {e}")

    if not matched_row:
        cursor.close()
        conn.close()
        return None, "Invalid or inactive API key"

    # --- PAID credits rule ---
    if matched_row.get("key_type") == "paid":
        tenant_id = int(matched_row["tenant_id"])
        balance_tokens = int(get_token_balance(tenant_id))
        if balance_tokens <= 0:
            insert_audit_log(
                action="paid_no_credits_blocked",
                tenant_id=tenant_id,
                website=matched_row.get("website"),
                key_type="paid",
                api_key_id=matched_row["api_key_id"],
                api_key_last4=str(api_key)[-4:],
                details={"token_balance": balance_tokens},
            )

            cursor.close()
            conn.close()
            return None, "Please accept my apologies for the delay in my response. It appears that our current service credits have been fully utilized, and I am working to resolve this immediately. I apologize for any inconvenience this may cause."

    # --- Trial rules ---
    # Quota is now enforced by plan ai_messages_limit via _check_quota() in main.py.
    # We only record the first-use activation timestamp; no hard expiry or token-limit cutoff.
    if matched_row.get("key_type") == "trial":
        if matched_row.get("trial_activated_at") is None:
            now = _utcnow()
            cursor2 = conn.cursor()
            cursor2.execute(
                "UPDATE api_keys SET trial_activated_at=%s WHERE id=%s",
                (_to_pg_dt(now), matched_row["api_key_id"]),
            )
            conn.commit()
            cursor2.close()
            matched_row["trial_activated_at"] = _to_pg_dt(now)
            insert_audit_log(
                action="trial_activated",
                tenant_id=matched_row["tenant_id"],
                website=matched_row.get("website"),
                key_type="trial",
                api_key_id=matched_row["api_key_id"],
                api_key_last4=str(api_key)[-4:],
                details={"plan_based": True},
            )

    cursor.close()
    conn.close()
    return matched_row, None
