import os
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone

from db import get_db_connection, insert_audit_log

# Display rule only:
#   1 credit = 5000 tokens
# Storage uses tokens (BIGINT) to avoid rounding.


def ensure_billing_tables():
    """Create billing/usage tables if missing. Safe to run every startup."""
    conn = get_db_connection()
    if not conn:
        return

    cur = conn.cursor()
    try:
        # Tenant token balance (credits are derived: credits = token_balance / 5000)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_balances (
                tenant_id INT PRIMARY KEY,
                token_balance BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                CONSTRAINT fk_tb_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
            ) ENGINE=InnoDB
            """
        )

        # Per-request usage rows used by portal charts & breakdown.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                tenant_id INT NOT NULL,
                api_key_id INT NOT NULL,
                website VARCHAR(255) NULL,
                key_type ENUM('paid','trial') NULL,
                session_id VARCHAR(64) NULL,
                used_tokens INT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_usage_tenant_time (tenant_id, created_at),
                INDEX idx_usage_session (session_id),
                CONSTRAINT fk_ue_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
                CONSTRAINT fk_ue_key FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
            ) ENGINE=InnoDB
            """
        )

        conn.commit()
    except Exception as e:
        print("⚠️ ensure_billing_tables failed:", e)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def get_token_balance(tenant_id: int) -> int:
    conn = get_db_connection()
    if not conn:
        return 0

    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT token_balance FROM tenant_balances WHERE tenant_id=%s", (tenant_id,))
        row = cur.fetchone()
        if not row:
            cur2 = conn.cursor()
            cur2.execute(
                "INSERT IGNORE INTO tenant_balances (tenant_id, token_balance) VALUES (%s, 0)",
                (tenant_id,),
            )
            conn.commit()
            cur2.close()
            return 0
        return int(row.get("token_balance") or 0)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def deduct_tokens(tenant_id: int, used_tokens: int) -> tuple[bool, int]:
    """
    Deducts tokens from tenant balance (paid keys only).

    Returns:
      (ok, new_balance_tokens)

    If the balance would go below 0, it is clamped to 0 and ok=False.
    """
    if used_tokens <= 0:
        return True, get_token_balance(tenant_id)

    conn = get_db_connection()
    if not conn:
        # Conservative: do not block chat if DB is temporarily unavailable
        return True, 0

    cur = conn.cursor(dictionary=True)
    try:
        # Lock row to avoid races
        cur.execute(
            "SELECT token_balance FROM tenant_balances WHERE tenant_id=%s FOR UPDATE",
            (tenant_id,),
        )
        row = cur.fetchone()
        if not row:
            cur2 = conn.cursor()
            cur2.execute(
                "INSERT INTO tenant_balances (tenant_id, token_balance) VALUES (%s, 0)",
                (tenant_id,),
            )
            conn.commit()
            cur2.close()
            balance = 0
        else:
            balance = int(row.get("token_balance") or 0)

        new_balance = balance - int(used_tokens)
        ok = new_balance >= 0
        if new_balance < 0:
            new_balance = 0

        cur2 = conn.cursor()
        cur2.execute(
            "UPDATE tenant_balances SET token_balance=%s WHERE tenant_id=%s",
            (int(new_balance), tenant_id),
        )
        conn.commit()
        cur2.close()

        return ok, int(new_balance)
    except Exception as e:
        print("⚠️ deduct_tokens failed:", e)
        return True, 0
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def send_email(to_email: str, subject: str, html_body: str, text_body: str | None = None):
    """
    Sends email via SMTP.

    Configure in .env (same as your DB env):
      SMTP_HOST=smtp.yourprovider.com
      SMTP_PORT=587
      SMTP_USER=...
      SMTP_PASSWORD=...
      SMTP_FROM=no-reply@phixtra.com
      SMTP_USE_TLS=1

    This function never raises.
    """
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_email = os.getenv("SMTP_FROM", user or "no-reply@phixtra.com").strip()
    use_tls = os.getenv("SMTP_USE_TLS", "1") == "1"

    if not host or not from_email:
        print("⚠️ SMTP not configured; skipping email:", subject)
        return

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text_body or "Please view this email in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            if use_tls:
                server.starttls()
            if user and password:
                server.login(user, password)
            server.send_message(msg)
    except Exception as e:
        print("⚠️ send_email failed:", e)


def maybe_send_low_balance_alert(tenant_id: int, new_balance_tokens: int):
    """
    Sends low-balance alerts to all verified, active customers of the tenant.

    Default thresholds (credits):
      - 20 credits
      - 10 credits
      - 0 credits

    Uses table customer_alert_state to avoid spamming.
    """
    credits = new_balance_tokens / 5000.0

    level = None
    if credits <= 0:
        level = "0"
    elif credits <= 10:
        level = "10"
    elif credits <= 20:
        level = "20"
    else:
        return

    conn = get_db_connection()
    if not conn:
        return

    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, email
            FROM customers
            WHERE tenant_id=%s AND email_verified=1 AND is_active=1
            """,
            (tenant_id,),
        )
        customers = cur.fetchall() or []

        for c in customers:
            cid = int(c["id"])
            email = c["email"]

            cur2 = conn.cursor(dictionary=True)
            cur2.execute(
                "SELECT last_alert_level FROM customer_alert_state WHERE customer_id=%s",
                (cid,),
            )
            st = cur2.fetchone()
            cur2.close()

            last_level = (st or {}).get("last_alert_level")
            if last_level == level:
                continue

            cur3 = conn.cursor()
            cur3.execute(
                """
                INSERT INTO customer_alert_state (customer_id, last_alert_level)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE last_alert_level=VALUES(last_alert_level), updated_at=CURRENT_TIMESTAMP
                """,
                (cid, level),
            )
            conn.commit()
            cur3.close()

            subject = "PhiXtra credits low"
            html = f"""
            <div style=\"font-family:Arial,sans-serif\">
              <h2 style=\"color:#030C18;margin:0 0 10px 0\">Your PhiXtra credits are running low</h2>
              <p style=\"margin:0 0 12px 0\">Remaining balance: <b>{credits:.2f} credits</b> (1 credit = 5,000 tokens)</p>
              <p style=\"margin:18px 0\">
                <a href=\"https://portal.phixtra.com/billing\" style=\"background:#030C18;color:#fff;padding:10px 14px;border-radius:12px;text-decoration:none;display:inline-block\">Top up credits</a>
              </p>
              <p style=\"color:#666;font-size:12px\">If you already topped up, you can ignore this email.</p>
            </div>
            """
            send_email(
                email,
                subject,
                html,
                text_body=f"Remaining credits: {credits:.2f}. Top up at https://portal.phixtra.com/billing",
            )

    except Exception as e:
        print("⚠️ maybe_send_low_balance_alert failed:", e)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
