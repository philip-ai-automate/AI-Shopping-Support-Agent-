import bcrypt
from datetime import datetime, timedelta, timezone

from db import get_db_connection, insert_audit_log
from billing import ensure_billing_tables, get_token_balance

TRIAL_DAYS = 14


def _utcnow():
    return datetime.now(timezone.utc)


def _to_mysql_dt(dt: datetime):
    # Store without timezone (MySQL DATETIME). Keep it in UTC.
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

    cursor = conn.cursor(dictionary=True)

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
            t.system_prompt,
            t.azure_search_index,
            t.azure_semantic_config,
            t.status,
            t.features
        FROM api_keys ak
        JOIN tenants t ON ak.tenant_id = t.id
        WHERE ak.is_active = 1
          AND t.status IN ('active', 'pending')
    """

    cursor.execute(query)
    rows = cursor.fetchall()

    matched_row = None
    for row in rows:
        try:
            if bcrypt.checkpw(api_key.encode("utf-8"), row["api_key_hash"].encode("utf-8")):
                matched_row = row
                break
        except Exception:
            continue

    if not matched_row:
        cursor.close()
        conn.close()
        return None, "Invalid or inactive API key"

    # --- PAID credits rule ---
    if matched_row.get("key_type") == "paid":
        tenant_id = int(matched_row["tenant_id"])
        balance_tokens = int(get_token_balance(tenant_id))
        if balance_tokens <= 0:
            # IMPORTANT: Do NOT deactivate the API key automatically.
            # If we deactivate here, customers who top-up will still be blocked until an admin re-activates the key.
            # Instead, return an error and let billing/top-up restore usage immediately.

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
    if matched_row.get("key_type") == "trial":
        now = _utcnow()

        # 1) Activate trial on first valid use
        if matched_row.get("trial_activated_at") is None:
            activated_at = now
            expires_at = now + timedelta(days=TRIAL_DAYS)

            cursor2 = conn.cursor()
            cursor2.execute(
                """
                UPDATE api_keys
                SET trial_activated_at=%s, trial_expires_at=%s
                WHERE id=%s
                """,
                (_to_mysql_dt(activated_at), _to_mysql_dt(expires_at), matched_row["api_key_id"]),
            )
            conn.commit()
            cursor2.close()

            matched_row["trial_activated_at"] = _to_mysql_dt(activated_at)
            matched_row["trial_expires_at"] = _to_mysql_dt(expires_at)

            insert_audit_log(
                action="trial_activated",
                tenant_id=matched_row["tenant_id"],
                website=matched_row.get("website"),
                key_type="trial",
                api_key_id=matched_row["api_key_id"],
                api_key_last4=str(api_key)[-4:],
                details={"trial_days": TRIAL_DAYS},
            )

        # 2) Expired?
        expires_at = matched_row.get("trial_expires_at")
        if expires_at is not None:
            expires_at_utc = expires_at.replace(tzinfo=timezone.utc)
            if now >= expires_at_utc:
                # Deactivate key
                cursor2 = conn.cursor()
                cursor2.execute("UPDATE api_keys SET is_active=0 WHERE id=%s", (matched_row["api_key_id"],))
                conn.commit()
                cursor2.close()

                insert_audit_log(
                    action="trial_expired_deactivated",
                    tenant_id=matched_row["tenant_id"],
                    website=matched_row.get("website"),
                    key_type="trial",
                    api_key_id=matched_row["api_key_id"],
                    api_key_last4=str(api_key)[-4:],
                    details={"expired_at_utc": expires_at_utc.isoformat()},
                )

                cursor.close()
                conn.close()
                return None, "Trial expired. Please upgrade to a paid plan."

        # 3) Token limit reached?
        token_limit = matched_row.get("token_limit")
        tokens_used = matched_row.get("tokens_used") or 0
        if token_limit is not None and tokens_used >= token_limit:
            cursor2 = conn.cursor()
            cursor2.execute("UPDATE api_keys SET is_active=0 WHERE id=%s", (matched_row["api_key_id"],))
            conn.commit()
            cursor2.close()

            insert_audit_log(
                action="trial_token_limit_deactivated",
                tenant_id=matched_row["tenant_id"],
                website=matched_row.get("website"),
                key_type="trial",
                api_key_id=matched_row["api_key_id"],
                api_key_last4=str(api_key)[-4:],
                details={"token_limit": token_limit, "tokens_used": tokens_used},
            )

            cursor.close()
            conn.close()
            return None, "Trial token limit reached. Please upgrade to a paid plan."

    cursor.close()
    conn.close()
    return matched_row, None
