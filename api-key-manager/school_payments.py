"""
school_payments.py — WhatsApp fee payments for PhiXtra School.

Each school connects its OWN Paystack/Flutterwave account (secret keys Fernet-
encrypted at rest, same scheme as the merchant portal's payment_gateways table
in portal_routes.py). PhiXtra never holds or moves school funds — money goes
straight from parent to school.
"""
import os
import secrets
import psycopg2.extras
from db import get_db_connection
from portal_routes import _encrypt_key, _decrypt_key, _webhook_health

webhook_health = _webhook_health  # re-exported for school_routes.py

SCHOOL_BASE_URL = os.getenv("SCHOOL_BASE_URL", "https://school.phixtra.com").rstrip("/")
SCHOOL_PAY_BASE_URL = f"{SCHOOL_BASE_URL}/school"

_PAYSTACK_API = "https://api.paystack.co"
_FLUTTERWAVE_API = "https://api.flutterwave.com/v3"


def _row(cur):
    row = cur.fetchone()
    return row


def verify_paystack_signature(secret: str, raw_body: bytes, signature: str | None) -> bool:
    import hmac, hashlib
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_flutterwave_signature(secret: str, signature: str | None) -> bool:
    import hmac
    return hmac.compare_digest(secret, signature or "")


def get_gateway(school_id: int, gateway: str) -> dict:
    """Return the raw (encrypted) row for one gateway, or {} if not connected."""
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM school_payment_gateways WHERE school_id=%s AND gateway=%s",
        (school_id, gateway),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    return row or {}


def mask_secret(row: dict) -> dict:
    """Add a 'secret_masked' key showing only the last 6 chars, for display."""
    if not row or not row.get("secret_key_enc"):
        return row
    try:
        plain = _decrypt_key(row["secret_key_enc"])
        row["secret_masked"] = "•" * max(len(plain) - 6, 0) + plain[-6:] if len(plain) > 6 else "••••••"
    except Exception:
        row["secret_masked"] = "••••••••••••••••"
    return row


def get_active_gateway(school_id: int) -> dict | None:
    """Return {'gateway','public_key','secret_key'} for the gateway that should be
    used to charge parents — the school's chosen default when both are connected,
    otherwise whichever one is connected. None if neither is connected."""
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT gateway, public_key, secret_key_enc FROM school_payment_gateways "
        "WHERE school_id=%s AND is_active=TRUE",
        (school_id,),
    )
    rows = cur.fetchall()
    cur.execute("SELECT default_payment_gateway FROM school_profiles WHERE id=%s", (school_id,))
    default = (cur.fetchone() or {}).get("default_payment_gateway")
    cur.close(); conn.close()

    if not rows:
        return None
    chosen = next((r for r in rows if r["gateway"] == default), rows[0])
    return {
        "gateway": chosen["gateway"],
        "public_key": chosen["public_key"],
        "secret_key": _decrypt_key(chosen["secret_key_enc"]),
    }


def get_or_create_payment_token(schedule_id: int, student_id: int) -> str:
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "SELECT payment_token FROM school_fee_payments WHERE schedule_id=%s AND student_id=%s",
        (schedule_id, student_id),
    )
    row = cur.fetchone()
    token = row[0] if row else None
    if not token:
        token = secrets.token_urlsafe(24)
        cur.execute(
            "UPDATE school_fee_payments SET payment_token=%s "
            "WHERE schedule_id=%s AND student_id=%s",
            (token, schedule_id, student_id),
        )
        conn.commit()
    cur.close(); conn.close()
    return token


def get_payment_by_token(token: str) -> dict | None:
    """Return the fee_payments row joined with schedule/student/school info, or None."""
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT fp.id AS payment_id, fp.schedule_id, fp.student_id, fp.amount_paid,
               fp.status, fp.payment_token,
               fs.name AS fee_name, fs.amount AS total_amount, fs.due_date, fs.school_id,
               s.full_name AS student_name,
               sp.school_name
        FROM school_fee_payments fp
        JOIN school_fee_schedules fs ON fs.id = fp.schedule_id
        JOIN school_students s ON s.id = fp.student_id
        JOIN school_profiles sp ON sp.id = fs.school_id
        WHERE fp.payment_token = %s
    """, (token,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def init_checkout(token: str) -> tuple[str | None, str | None]:
    """Start a hosted checkout for this payment token.
    Returns (checkout_url, error_message) — exactly one will be set."""
    import requests as _req

    payment = get_payment_by_token(token)
    if not payment:
        return None, "This payment link is invalid."
    if payment["status"] == "paid":
        return None, "This fee has already been paid in full."

    balance = float(payment["total_amount"]) - float(payment["amount_paid"])
    if balance <= 0:
        return None, "This fee has already been paid in full."

    gw = get_active_gateway(payment["school_id"])
    if not gw:
        return None, "Online payment is not set up for this school yet."

    if gw["gateway"] == "paystack":
        try:
            resp = _req.post(
                f"{_PAYSTACK_API}/transaction/initialize",
                headers={"Authorization": f"Bearer {gw['secret_key']}"},
                json={
                    "email": f"parent.{token[:10]}@phixtra.com",
                    "amount": int(round(balance * 100)),
                    "currency": "NGN",
                    "callback_url": f"{SCHOOL_PAY_BASE_URL}/pay/{token}/callback",
                    "metadata": {
                        "school_id": payment["school_id"],
                        "schedule_id": payment["schedule_id"],
                        "student_id": payment["student_id"],
                        "payment_token": token,
                    },
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("status"):
                return data["data"]["authorization_url"], None
            return None, "Could not start payment. Please try again."
        except Exception as e:
            print(f"⚠️ school Paystack init error: {e}")
            return None, "Could not reach payment provider. Please try again."

    # Flutterwave
    try:
        resp = _req.post(
            f"{_FLUTTERWAVE_API}/payments",
            headers={"Authorization": f"Bearer {gw['secret_key']}", "Content-Type": "application/json"},
            json={
                "tx_ref": f"SCHOOLFEE-{token}",
                "amount": balance,
                "currency": "NGN",
                "redirect_url": f"{SCHOOL_PAY_BASE_URL}/pay/{token}/callback",
                "customer": {"email": f"parent.{token[:10]}@phixtra.com",
                             "name": payment["student_name"]},
                "customizations": {
                    "title": payment["school_name"],
                    "description": f"{payment['fee_name']} — {payment['student_name']}",
                },
                "meta": {
                    "school_id": payment["school_id"],
                    "schedule_id": payment["schedule_id"],
                    "student_id": payment["student_id"],
                    "payment_token": token,
                },
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["link"], None
        return None, "Could not start payment. Please try again."
    except Exception as e:
        print(f"⚠️ school Flutterwave init error: {e}")
        return None, "Could not reach payment provider. Please try again."


def verify_and_record_payment(gateway: str, school_id: int, tx_ref: str) -> bool:
    """Double-verify a transaction via the provider's API (never trust a webhook
    payload alone), then credit the fee payment exactly once. Returns True if the
    payment is now recorded as paid/partial (including if it already was)."""
    import requests as _req

    gw_row = get_gateway(school_id, gateway)
    if not gw_row or not gw_row.get("secret_key_enc"):
        return False
    secret = _decrypt_key(gw_row["secret_key_enc"])

    if gateway == "paystack":
        try:
            resp = _req.get(
                f"{_PAYSTACK_API}/transaction/verify/{tx_ref}",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=15,
            )
            data = resp.json().get("data", {})
        except Exception as e:
            print(f"⚠️ Paystack verify error: {e}")
            return False
        if data.get("status") != "success":
            return False
        amount_paid_now = float(data.get("amount", 0)) / 100
        token = (data.get("metadata") or {}).get("payment_token")
        provider_ref = data.get("reference")
    else:
        try:
            resp = _req.get(
                f"{_FLUTTERWAVE_API}/transactions/{tx_ref}/verify",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=15,
            )
            data = resp.json().get("data", {})
        except Exception as e:
            print(f"⚠️ Flutterwave verify error: {e}")
            return False
        if data.get("status") != "successful":
            return False
        amount_paid_now = float(data.get("amount", 0))
        token = (data.get("meta") or {}).get("payment_token")
        provider_ref = str(data.get("id"))

    if not token:
        return False
    payment = get_payment_by_token(token)
    if not payment or payment["school_id"] != school_id:
        return False

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # Idempotency: a replayed webhook/callback for the same tx_ref is a no-op.
        cur.execute(
            "INSERT INTO school_fee_gateway_txns (school_id, payment_id, gateway, tx_ref, amount) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (gateway, tx_ref) DO NOTHING RETURNING id",
            (school_id, payment["payment_id"], gateway, provider_ref, amount_paid_now),
        )
        already_processed = cur.fetchone() is None
        if already_processed:
            conn.commit()
            return True

        new_amount_paid = float(payment["amount_paid"]) + amount_paid_now
        new_status = "paid" if new_amount_paid >= float(payment["total_amount"]) else "partial"
        cur.execute("""
            UPDATE school_fee_payments
            SET amount_paid=%s, status=%s, payment_ref=%s, payment_date=NOW()
            WHERE id=%s
        """, (new_amount_paid, new_status, provider_ref, payment["payment_id"]))
        cur.execute(
            "UPDATE school_payment_gateways SET last_webhook_at=NOW() "
            "WHERE school_id=%s AND gateway=%s",
            (school_id, gateway),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"⚠️ verify_and_record_payment error: {e}")
        return False
    finally:
        cur.close(); conn.close()

    _notify_parent_paid(school_id, payment, amount_paid_now, new_amount_paid, new_status)
    return True


def _notify_parent_paid(school_id, payment, amount_paid_now, new_amount_paid, new_status):
    try:
        from school_wa import send_fee_payment_confirmation
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT p.whatsapp_number
            FROM school_student_parents ssp
            JOIN school_parents p ON p.id = ssp.parent_id
            WHERE ssp.student_id=%s AND p.is_opted_in=TRUE
        """, (payment["student_id"],))
        parents = cur.fetchall()
        cur.close(); conn.close()

        balance = max(float(payment["total_amount"]) - new_amount_paid, 0)
        for p in parents:
            send_fee_payment_confirmation(
                school_id=school_id,
                parent_wa=p["whatsapp_number"],
                student_name=payment["student_name"],
                fee_name=payment["fee_name"],
                amount_paid=amount_paid_now,
                balance=balance,
                school_name=payment["school_name"],
            )
    except Exception as e:
        print(f"⚠️ _notify_parent_paid error: {e}")
