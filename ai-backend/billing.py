import os
import smtplib
import psycopg2.extras
from email.message import EmailMessage
from datetime import datetime, timezone

from db import get_db_connection, insert_audit_log


def ensure_billing_tables():
    """No-op: billing tables are created by pg_schema.sql at deploy time."""
    pass


def get_token_balance(tenant_id: int) -> int:
    conn = get_db_connection()
    if not conn:
        return 0

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT token_balance FROM tenant_balances WHERE tenant_id=%s", (tenant_id,))
        row = cur.fetchone()
        if not row:
            cur2 = conn.cursor()
            cur2.execute(
                "INSERT INTO tenant_balances (tenant_id, token_balance) VALUES (%s, 0) ON CONFLICT DO NOTHING",
                (tenant_id,),
            )
            conn.commit()
            cur2.close()
            return 0
        return int(row.get("token_balance") or 0)
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


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
        return True, 0

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
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
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass


def send_email(to_email: str, subject: str, html_body: str, text_body: str | None = None):
    """Sends email via SMTP. Never raises."""
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

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, email
            FROM customers
            WHERE tenant_id=%s AND email_verified=TRUE AND is_active=TRUE
            """,
            (tenant_id,),
        )
        customers = cur.fetchall() or []

        for c in customers:
            cid = int(c["id"])
            email = c["email"]

            cur2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
                ON CONFLICT (customer_id) DO UPDATE SET
                    last_alert_level = EXCLUDED.last_alert_level,
                    updated_at = NOW()
                """,
                (cid, level),
            )
            conn.commit()
            cur3.close()

            subject = "PhiXtra credits low"
            html = f"""
            <div style="font-family:Arial,sans-serif">
              <h2 style="color:#030C18;margin:0 0 10px 0">Your PhiXtra credits are running low</h2>
              <p style="margin:0 0 12px 0">Remaining balance: <b>{credits:.2f} credits</b> (1 credit = 5,000 tokens)</p>
              <p style="margin:18px 0">
                <a href="https://portal.phixtra.com/billing" style="background:#030C18;color:#fff;padding:10px 14px;border-radius:12px;text-decoration:none;display:inline-block">Top up credits</a>
              </p>
              <p style="color:#666;font-size:12px">If you already topped up, you can ignore this email.</p>
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
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
