"""
wa_bank_payment_reminder.py — nudges customers who started a bank-transfer
order and never sent their payment proof photo, and expires stale ones.

Run every 15 minutes via cron (matches wa_fw_payment_reminder.py):
  */15 * * * * cd /root/phixtra-app/whatsapp-gateway && ./venv/bin/python3 wa_bank_payment_reminder.py >> /var/log/wa_bank_payment_reminder.log 2>&1

Logic:
  - PAYMENT_PENDING sessions untouched for longer than the tenant's own
    reminder_after_hours (merchant_bank_accounts.reminder_after_hours;
    default 4h if the merchant never set one), not yet reminded: resend
    the bank details with a reminder note, mark cart.bank_reminded=true.
  - PAYMENT_PENDING sessions untouched for longer than the tenant's own
    cancel_after_hours (default 48h): cancel the order and delete the
    session (no order row exists yet at this stage — it's only created
    once the proof photo arrives — so there's nothing to cancel there,
    just the session).

Each business sets its own two numbers on the Payment Gateways settings page.
"""

import asyncio
import json

import psycopg2.extras

from wa_db import get_db_connection, get_merchant_bank, delete_wa_shop_session
from meta_sender import send_text

_DEFAULT_REMIND_AFTER_HOURS = 4
_DEFAULT_CANCEL_AFTER_HOURS = 48


def _load_cart(row: dict) -> dict:
    cart = row.get("cart")
    if isinstance(cart, str):
        try:
            return json.loads(cart or "{}")
        except Exception:
            return {}
    return cart or {}


def _get_wa_creds(tenant_id: int) -> dict | None:
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT phone_number_id, access_token FROM wa_tenants WHERE tenant_id=%s AND active=TRUE LIMIT 1",
            (tenant_id,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


def _fetch_due_sessions() -> tuple[list[dict], list[dict]]:
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT s.session_id, s.tenant_id, s.customer_phone, s.state, s.cart, s.order_id, s.updated_at
              FROM wa_shop_session s
              LEFT JOIN merchant_bank_accounts mba
                     ON mba.tenant_id = s.tenant_id AND mba.is_primary = TRUE
             WHERE s.state = 'PAYMENT_PENDING'
               AND s.updated_at < NOW() - (COALESCE(mba.reminder_after_hours, %s) * INTERVAL '1 hour')
            """,
            (_DEFAULT_REMIND_AFTER_HOURS,),
        )
        due = cur.fetchall() or []

        cur.execute(
            """
            SELECT s.session_id, s.tenant_id, s.customer_phone, s.state, s.cart, s.order_id, s.updated_at
              FROM wa_shop_session s
              LEFT JOIN merchant_bank_accounts mba
                     ON mba.tenant_id = s.tenant_id AND mba.is_primary = TRUE
             WHERE s.state = 'PAYMENT_PENDING'
               AND s.updated_at < NOW() - (COALESCE(mba.cancel_after_hours, %s) * INTERVAL '1 hour')
            """,
            (_DEFAULT_CANCEL_AFTER_HOURS,),
        )
        expired = cur.fetchall() or []
        return due, expired
    finally:
        cur.close()
        conn.close()


def _mark_reminded(session_id: str, cart: dict):
    cart["bank_reminded"] = True
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE wa_shop_session SET cart=%s, updated_at=NOW() WHERE session_id=%s",
            (json.dumps(cart), session_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


async def _run():
    due, expired = _fetch_due_sessions()
    expired_ids = {r["session_id"] for r in expired}

    # Reminders — skip anything that's about to be expired instead
    for row in due:
        if row["session_id"] in expired_ids:
            continue
        cart = _load_cart(row)
        if cart.get("bank_reminded"):
            continue
        creds = _get_wa_creds(int(row["tenant_id"]))
        if not creds:
            continue
        pname = cart.get("product_name", "your order")
        try:
            final_price = float(cart.get("final_price") or cart.get("unit_price") or 0)
            price_str = f"₦{final_price:,.0f}"
        except Exception:
            price_str = str(cart.get("final_price") or cart.get("unit_price") or "")
        bank = get_merchant_bank(int(row["tenant_id"]))
        bank_block = (
            f"🏦 Bank: {bank.get('bank_name', '—')}\n"
            f"Account Number: *{bank.get('account_number', '—')}*\n"
            f"Account Name: {bank.get('account_name', '—')}\n"
            if bank else "_(The store will send payment details shortly)_\n"
        )
        try:
            await send_text(
                creds["phone_number_id"], creds["access_token"], row["customer_phone"],
                f"⏰ *Reminder* — we're still waiting for your payment proof for *{pname}* ({price_str}).\n\n"
                f"{bank_block}\n"
                "Please send a *photo of your payment receipt* here to confirm your order.\n\n"
                "_(Reply *CANCEL* to cancel this order)_",
            )
            _mark_reminded(row["session_id"], cart)
            print(f"✅ [BANK-REMINDER] reminded session={row['session_id']}")
        except Exception as e:
            print(f"⚠️ [BANK-REMINDER] send failed session={row['session_id']}: {e}")

    # Expirations — no order row exists yet at PAYMENT_PENDING (it's only
    # created once the proof photo arrives), so there's nothing to cancel
    # beyond the session itself.
    for row in expired:
        cart = _load_cart(row)
        creds = _get_wa_creds(int(row["tenant_id"]))
        if creds:
            pname = cart.get("product_name", "your order")
            try:
                await send_text(
                    creds["phone_number_id"], creds["access_token"], row["customer_phone"],
                    f"Your order for *{pname}* has been cancelled since we didn't receive payment proof.\n\n"
                    "Feel free to browse and order again anytime! 😊",
                )
            except Exception as e:
                print(f"⚠️ [BANK-REMINDER] expiry notice failed session={row['session_id']}: {e}")
        delete_wa_shop_session(row["session_id"])
        print(f"🗑️ [BANK-REMINDER] expired session={row['session_id']}")


if __name__ == "__main__":
    asyncio.run(_run())
