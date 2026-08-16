"""
wa_fw_payment_reminder.py — nudges customers who have an unpaid Flutterwave
checkout link sitting in their WhatsApp chat, and expires stale ones.

Run every 15 minutes via cron (matches re_embed_worker.py / image_embed_worker.py):
  */15 * * * * cd /root/phixtra-app/whatsapp-gateway && ./venv/bin/python3 wa_fw_payment_reminder.py >> /var/log/wa_fw_payment_reminder.log 2>&1

Logic:
  - AWAITING_FW_PAYMENT sessions untouched for longer than the tenant's own
    reminder_after_hours (payment_gateways.reminder_after_hours, gateway=
    'flutterwave'; default 0.5h = 30min if the merchant never set one),
    not yet reminded: resend the payment link with a reminder note, mark
    cart.fw_reminded=true.
  - AWAITING_FW_PAYMENT sessions untouched for longer than the tenant's own
    cancel_after_hours (default 24h): cancel the order, delete the session,
    tell the customer the link expired.

Each business sets its own two numbers on the Payment Gateways settings page.
"""

import asyncio
import json

import psycopg2.extras

from wa_db import get_db_connection, cancel_wa_order
from meta_sender import send_text

_DEFAULT_REMIND_AFTER_HOURS = 0.5
_DEFAULT_CANCEL_AFTER_HOURS = 24


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
              LEFT JOIN payment_gateways pg
                     ON pg.tenant_id = s.tenant_id AND pg.gateway = 'flutterwave'
             WHERE s.state = 'AWAITING_FW_PAYMENT'
               AND s.updated_at < NOW() - (COALESCE(pg.reminder_after_hours, %s) * INTERVAL '1 hour')
            """,
            (_DEFAULT_REMIND_AFTER_HOURS,),
        )
        due = cur.fetchall() or []

        cur.execute(
            """
            SELECT s.session_id, s.tenant_id, s.customer_phone, s.state, s.cart, s.order_id, s.updated_at
              FROM wa_shop_session s
              LEFT JOIN payment_gateways pg
                     ON pg.tenant_id = s.tenant_id AND pg.gateway = 'flutterwave'
             WHERE s.state = 'AWAITING_FW_PAYMENT'
               AND s.updated_at < NOW() - (COALESCE(pg.cancel_after_hours, %s) * INTERVAL '1 hour')
            """,
            (_DEFAULT_CANCEL_AFTER_HOURS,),
        )
        expired = cur.fetchall() or []
        return due, expired
    finally:
        cur.close()
        conn.close()


def _mark_reminded(session_id: str, cart: dict):
    cart["fw_reminded"] = True
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


def _delete_session(session_id: str):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM wa_shop_session WHERE session_id=%s", (session_id,))
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
        if cart.get("fw_reminded"):
            continue
        link = cart.get("fw_link")
        if not link:
            continue
        creds = _get_wa_creds(int(row["tenant_id"]))
        if not creds:
            continue
        pname   = cart.get("product_name", "your order")
        ref     = cart.get("reference", "")
        try:
            await send_text(
                creds["phone_number_id"], creds["access_token"], row["customer_phone"],
                f"⏰ *Reminder* — your payment for *{pname}* ({ref}) is still waiting.\n\n"
                f"💳 Tap to pay securely:\n{link}\n\n"
                "_(Reply *CANCEL* to cancel this order)_",
            )
            _mark_reminded(row["session_id"], cart)
            print(f"✅ [FW-REMINDER] reminded session={row['session_id']} order={ref}")
        except Exception as e:
            print(f"⚠️ [FW-REMINDER] send failed session={row['session_id']}: {e}")

    # Expirations
    for row in expired:
        cart = _load_cart(row)
        creds = _get_wa_creds(int(row["tenant_id"]))
        if row.get("order_id"):
            cancel_wa_order(row["order_id"])
        if creds:
            pname = cart.get("product_name", "your order")
            ref   = cart.get("reference", "")
            try:
                await send_text(
                    creds["phone_number_id"], creds["access_token"], row["customer_phone"],
                    f"Your payment link for *{pname}* ({ref}) has expired and the order was cancelled.\n\n"
                    "Feel free to browse and order again anytime! 😊",
                )
            except Exception as e:
                print(f"⚠️ [FW-REMINDER] expiry notice failed session={row['session_id']}: {e}")
        _delete_session(row["session_id"])
        print(f"🗑️ [FW-REMINDER] expired session={row['session_id']} order={row.get('order_id')}")


if __name__ == "__main__":
    asyncio.run(_run())
