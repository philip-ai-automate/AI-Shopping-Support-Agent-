import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=os.getenv("PG_HOST", "localhost"),
            port=int(os.getenv("PG_PORT", "5432")),
            user=os.getenv("PG_USER"),
            password=os.getenv("PG_PASSWORD"),
            dbname=os.getenv("PG_DB"),
        )
        return conn
    except Exception as e:
        print("❌ WA Gateway DB connection error:", e)
        return None


def init_wa_tables():
    """Ensure wa_product_cache has all required columns (idempotent)."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE wa_product_cache ADD COLUMN IF NOT EXISTS image_url TEXT DEFAULT ''")
        cur.execute("ALTER TABLE wa_product_cache ADD COLUMN IF NOT EXISTS in_stock BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE wa_product_cache ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''")
        cur.execute("ALTER TABLE wa_product_cache ADD COLUMN IF NOT EXISTS list_order INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE wa_product_cache ADD COLUMN IF NOT EXISTS is_related BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE wa_product_cache ADD COLUMN IF NOT EXISTS last_viewed_at TIMESTAMP DEFAULT NULL")
        conn.commit()
    except Exception as e:
        print("⚠️ init_wa_tables migration warning:", e)
    finally:
        cur.close()
        conn.close()


def search_catalogue(keyword: str, limit: int = 5) -> list[dict]:
    """
    Full-text search on the shared phone_catalogue table (no tenant scope —
    this is a global reference catalogue used during merchant onboarding only).
    Returns up to `limit` results with title and price_min.
    """
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT
                model_name || COALESCE(' ' || variant_name, '')  AS title,
                nigeria_market_price_naira                        AS price_min
            FROM phone_catalogue
            WHERE is_active = TRUE
              AND to_tsvector('english',
                      COALESCE(brand,'') || ' ' ||
                      COALESCE(model_name,'') || ' ' ||
                      COALESCE(variant_name,'') || ' ' ||
                      COALESCE(search_intent_tags,'')
                  ) @@ plainto_tsquery('english', %s)
            ORDER BY ts_rank(
                to_tsvector('english',
                    COALESCE(brand,'') || ' ' ||
                    COALESCE(model_name,'') || ' ' ||
                    COALESCE(variant_name,'') || ' ' ||
                    COALESCE(search_intent_tags,'')
                ),
                plainto_tsquery('english', %s)
            ) DESC
            LIMIT %s
            """,
            (keyword, keyword, limit),
        )
        rows = cur.fetchall() or []
        return [
            {
                "title":     r["title"],
                "price_min": float(r["price_min"]) if r["price_min"] is not None else None,
            }
            for r in rows
        ]
    except Exception as e:
        print("⚠️ search_catalogue error:", e)
        return []
    finally:
        cur.close()
        conn.close()


def log_message(
    tenant_id: int,
    phone_number_id: str,
    customer_phone: str,
    direction: str,
    content: str,
    message_type: str = "text",
    meta_message_id: str = None,
) -> bool:
    """
    Insert a message into wa_message_log.
    Returns False if the meta_message_id already exists (dedup via ON CONFLICT DO NOTHING).
    """
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO wa_message_log
              (tenant_id, phone_number_id, customer_phone, direction, content, message_type, meta_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (meta_message_id) WHERE meta_message_id IS NOT NULL DO NOTHING
            """,
            (tenant_id, phone_number_id, customer_phone, direction, content, message_type, meta_message_id),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print("⚠️ log_message error:", e)
        return False
    finally:
        cur.close()
        conn.close()


def cache_products(session_id: str, products: list):
    """
    Store product data for a session so interactive_handler can look up
    cart and detail URLs when a customer taps a button.

    Every row written in this call shares the exact same updated_at (SQL
    NOW() is transaction-start time, identical across every statement in
    this one commit) — that's what lets get_session_products() identify
    "the list just shown" as a group, rather than mixing rows in from
    older, unrelated lists that happen to share the same list_order.
    """
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        for idx, p in enumerate(products):
            product_id = str(p.get("product_id") or p.get("id") or "").strip()
            if not product_id:
                continue
            cur.execute(
                """
                INSERT INTO wa_product_cache
                  (session_id, product_id, product_name, product_url, cart_url, price,
                   image_url, in_stock, description, list_order, is_related, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (session_id, product_id) DO UPDATE SET
                  product_name = EXCLUDED.product_name,
                  product_url  = EXCLUDED.product_url,
                  cart_url     = EXCLUDED.cart_url,
                  price        = EXCLUDED.price,
                  image_url    = EXCLUDED.image_url,
                  in_stock     = EXCLUDED.in_stock,
                  description  = EXCLUDED.description,
                  list_order   = EXCLUDED.list_order,
                  is_related   = EXCLUDED.is_related,
                  updated_at   = EXCLUDED.updated_at
                """,
                (
                    session_id,
                    product_id,
                    (p.get("name") or "")[:512],
                    (p.get("url") or "")[:1024],
                    (p.get("cart_url") or "")[:1024],
                    (p.get("price") or "")[:64],
                    (p.get("image_url") or "")[:1024],
                    bool(p.get("in_stock", True)),
                    (p.get("description") or "")[:600],
                    idx,
                    bool(p.get("related", False)),
                ),
            )
        conn.commit()
    except Exception as e:
        print("⚠️ cache_products error:", e)
    finally:
        cur.close()
        conn.close()


def get_cached_product(session_id: str, product_id: str) -> dict | None:
    """Retrieve a cached product by session + product_id."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT product_name, product_url, cart_url, price, image_url, in_stock, description
            FROM wa_product_cache
            WHERE session_id = %s AND product_id = %s
            """,
            (session_id, product_id),
        )
        return cur.fetchone()
    except Exception as e:
        print("⚠️ get_cached_product error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def get_session_products(session_id: str) -> list:
    """Return the products from the most recently shown list for this
    session, in list order — never rows from an earlier, different list
    that happens to share the same session (see cache_products docstring)."""
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT product_id, product_name, product_url, cart_url, price,
                   image_url, in_stock, is_related
            FROM wa_product_cache
            WHERE session_id = %s
              AND updated_at = (
                    SELECT MAX(updated_at) FROM wa_product_cache WHERE session_id = %s
                  )
            ORDER BY list_order ASC
            """,
            (session_id, session_id),
        )
        rows = cur.fetchall() or []
        return [dict(r) for r in rows]
    except Exception as e:
        print("⚠️ get_session_products error:", e)
        return []
    finally:
        cur.close()
        conn.close()


def mark_product_viewed(session_id: str, product_id: str) -> None:
    """Record that the customer viewed/selected this product in the session."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE wa_product_cache
               SET last_viewed_at = NOW()
             WHERE session_id = %s AND product_id = %s
            """,
            (session_id, product_id),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ mark_product_viewed error:", e)
    finally:
        cur.close()
        conn.close()


def get_viewed_products(session_id: str) -> list:
    """Return products the customer has selected/viewed, most recent first."""
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT product_id, product_name, price, in_stock
            FROM wa_product_cache
            WHERE session_id = %s
              AND last_viewed_at IS NOT NULL
              AND last_viewed_at > NOW() - INTERVAL '24 hours'
            ORDER BY last_viewed_at ASC
            """,
            (session_id,),
        )
        rows = cur.fetchall() or []
        return [dict(r) for r in rows]
    except Exception as e:
        print("⚠️ get_viewed_products error:", e)
        return []
    finally:
        cur.close()
        conn.close()


def get_document_for_product(product_id: str) -> dict | None:
    """Look up the documents table entry matching a wa_product_cache product_id."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT title, image_url, content, categories_text,
                   price_min, price_max, spec_key, spec_value, in_stock
            FROM documents
            WHERE id = %s
            """,
            (f"product-{product_id}",),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        print("⚠️ get_document_for_product error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def get_manual_product_in_stock(tenant_id: int, product_id) -> bool | None:
    """
    Live stock check for the manual (WA-only merchant) `products` catalog,
    used as a final pre-payment gate — separate from get_product_by_id so
    that function's None can keep meaning one thing ("no row"), not two.

    Returns True/False if this id belongs to that catalog, or None if it
    doesn't (e.g. it's a WooCommerce-synced product — checked separately via
    get_document_for_product's `in_stock` field).
    """
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT stock_quantity, reserved_quantity
            FROM products
            WHERE tenant_id = %s AND id = %s AND is_active = TRUE
            """,
            (tenant_id, product_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        return row["stock_quantity"] > row["reserved_quantity"]
    except Exception as e:
        print("⚠️ get_manual_product_in_stock error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def get_product_by_id(tenant_id: int, product_id) -> dict | None:
    """Return full product row from the products table by id."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, name, price, stock_quantity, discount_type, discount_value
            FROM products
            WHERE tenant_id = %s AND id = %s AND is_active = TRUE
            """,
            (tenant_id, product_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {**dict(row), "price": float(row["price"]), "discount_value": float(row["discount_value"] or 0)}
    except Exception as e:
        print("⚠️ get_product_by_id error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def get_wa_template(tenant_id: int, template_type: str) -> dict | None:
    """
    Return the tenant's configured Meta template for a given type,
    or None if not configured (caller should use a default template name).
    """
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT template_name, language_code
            FROM wa_templates
            WHERE tenant_id = %s AND template_type = %s AND active = TRUE
            """,
            (tenant_id, template_type),
        )
        return cur.fetchone()
    except Exception as e:
        print("⚠️ get_wa_template error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def log_proactive(
    tenant_id: int,
    phone_number_id: str,
    customer_phone: str,
    event_type: str,
    template_name: str,
    status: str,
    notes: str = "",
):
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO wa_proactive_log
              (tenant_id, phone_number_id, customer_phone, event_type, template_name, status, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (tenant_id, phone_number_id, customer_phone, event_type, template_name, status, notes),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ log_proactive error:", e)
    finally:
        cur.close()
        conn.close()


def is_campaign_recipient(tenant_id: int, customer_phone: str) -> bool:
    """Return True if this phone was ever successfully sent a campaign by this
    tenant. Matches any status the row can advance to after the async Meta
    delivery webhook updates it (sent/delivered/read), not just the initial
    'sent' state — otherwise this flips back to False the moment a message
    is confirmed delivered, which is the opposite of what it should do."""
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT 1 FROM wa_campaign_recipients
            WHERE tenant_id = %s AND phone = %s AND status IN ('sent', 'delivered', 'read')
            LIMIT 1
            """,
            (tenant_id, customer_phone),
        )
        return cur.fetchone() is not None
    except Exception as e:
        print("⚠️ is_campaign_recipient error:", e)
        return False
    finally:
        cur.close()
        conn.close()


def update_campaign_recipient_status(
    meta_message_id: str,
    status: str,
    error_code=None,
    error_title: str = None,
    error_message: str = None,
) -> bool:
    """Advance a campaign recipient row using Meta's async delivery-status
    webhook (sent → delivered → read, or failed with the real rejection
    reason, e.g. 131049 = marketing-message engagement throttling). Ignores
    out-of-order/duplicate webhooks that would downgrade a more-advanced
    status, since Meta doesn't guarantee delivery order."""
    rank = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT status FROM wa_campaign_recipients WHERE meta_message_id = %s",
            (meta_message_id,),
        )
        row = cur.fetchone()
        if not row:
            return False
        if rank.get(status, 0) < rank.get(row[0], 0):
            return True

        error_msg = None
        if error_code or error_title or error_message:
            label = f"[{error_code}] " if error_code else ""
            detail = " — ".join(p for p in (error_title, error_message) if p)
            error_msg = (label + detail)[:400]

        cur.execute(
            """
            UPDATE wa_campaign_recipients
            SET status = %s, error_msg = COALESCE(%s, error_msg), updated_at = NOW()
            WHERE meta_message_id = %s
            """,
            (status, error_msg, meta_message_id),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print("⚠️ update_campaign_recipient_status error:", e)
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


def record_cross_channel_optout(tenant_id: int, phone: str, reason: str) -> None:
    """A WhatsApp STOP reply means stop everywhere, not just WhatsApp — this
    marks the contact opted out on Email and SMS too (their own send paths
    check these), mirrors it into email_suppressions (the table the email
    send path actually queries — see _send_email_campaign_now — so that
    already-live check needs no change), and writes one row to
    contact_consent_log so the opt-out has a visible source and reason.
    Best-effort: called after wa_contacts.opted_out is already set, so a
    failure here never blocks the WhatsApp-side opt-out itself."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE wa_contacts
            SET email_opted_out=TRUE, email_opted_out_at=NOW(),
                sms_opted_out=TRUE, sms_opted_out_at=NOW()
            WHERE tenant_id=%s AND phone=%s
            RETURNING id, email
            """,
            (tenant_id, phone),
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return
        contact_id, email = row

        if email:
            cur.execute(
                """INSERT INTO email_suppressions (tenant_id, email, reason)
                       VALUES (%s, %s, 'whatsapp_optout')
                   ON CONFLICT (tenant_id, email) DO NOTHING""",
                (tenant_id, email),
            )

        cur.execute(
            """INSERT INTO contact_consent_log
                   (tenant_id, contact_id, channel, action, reason, source)
               VALUES (%s, %s, 'all', 'opted_out', %s, 'whatsapp_reply')""",
            (tenant_id, contact_id, reason),
        )
        conn.commit()
    except Exception as e:
        print(f"⚠️ record_cross_channel_optout error tenant={tenant_id} phone={phone}: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def get_visitor_handoff_keywords(tenant_id: int) -> list:
    """
    Return visitor_initiated handoff trigger texts for this tenant from the portal's
    handoff_rules table. The gateway uses these for fast keyword matching before
    calling the AI — same rules the merchant configured in the portal.
    Returns empty list if none configured (caller should use a fallback).
    """
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT trigger_text FROM handoff_rules
            WHERE tenant_id = %s AND trigger_type = 'visitor_initiated' AND is_active = TRUE
            ORDER BY sort_order ASC, id ASC
            """,
            (tenant_id,),
        )
        rows = cur.fetchall() or []
        return [r[0] for r in rows if r[0]]
    except Exception as e:
        print("⚠️ get_visitor_handoff_keywords error:", e)
        return []
    finally:
        cur.close()
        conn.close()


def is_handoff_active(session_id: str) -> bool:
    """Return True if this session has an unresolved human handoff within the last 4 hours."""
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id FROM wa_handoff_state
            WHERE session_id = %s
              AND resolved_at IS NULL
              AND escalated_at > NOW() - INTERVAL '4 hours'
            """,
            (session_id,),
        )
        return cur.fetchone() is not None
    except Exception as e:
        print("⚠️ is_handoff_active error:", e)
        return False
    finally:
        cur.close()
        conn.close()


def create_handoff(session_id: str, tenant_id: int, customer_phone: str):
    """Record a new human handoff. ON CONFLICT DO NOTHING is safe if already exists."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO wa_handoff_state (session_id, tenant_id, customer_phone)
            VALUES (%s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
            """,
            (session_id, tenant_id, customer_phone),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ create_handoff error:", e)
    finally:
        cur.close()
        conn.close()


def get_active_template(tenant_id: int, template_type: str) -> dict | None:
    """Return {template_name, language_code} for an approved+active template, or None."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT template_name, language_code FROM wa_templates
            WHERE tenant_id = %s AND template_type = %s AND active = TRUE
            LIMIT 1
            """,
            (tenant_id, template_type),
        )
        return cur.fetchone()
    except Exception as e:
        print("⚠️ get_active_template error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def auto_close_stale_handoffs() -> int:
    """
    Auto-resolve handoffs where the customer has not sent a message in 24 hours.
    Returns the number of handoffs closed.
    Runs every hour via the scheduler in main.py.
    """
    conn = get_db_connection()
    if not conn:
        return 0
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE wa_handoff_state
            SET resolved_at = NOW()
            WHERE resolved_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM wa_message_log
                WHERE wa_message_log.customer_phone = wa_handoff_state.customer_phone
                  AND wa_message_log.tenant_id      = wa_handoff_state.tenant_id
                  AND wa_message_log.direction      = 'inbound'
                  AND wa_message_log.created_at     > NOW() - INTERVAL '24 hours'
              )
        """)
        conn.commit()
        closed = cur.rowcount
        if closed:
            print(f"🕐 [AUTO-CLOSE] Resolved {closed} stale handoff(s) — no customer message in 24h")
        return closed
    except Exception as e:
        print(f"⚠️ auto_close_stale_handoffs error: {e}")
        return 0
    finally:
        cur.close()
        conn.close()


def cancel_handoff(session_id: str):
    """Mark an active handoff as resolved (customer cancelled the discount request)."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE wa_handoff_state
               SET resolved_at = NOW()
             WHERE session_id = %s AND resolved_at IS NULL
            """,
            (session_id,),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ cancel_handoff error:", e)
    finally:
        cur.close()
        conn.close()


# ── WhatsApp shopping session ─────────────────────────────────────────────────

def get_wa_shop_session(session_id: str) -> dict | None:
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT * FROM wa_shop_session WHERE session_id = %s",
            (session_id,),
        )
        return cur.fetchone()
    except Exception as e:
        print("⚠️ get_wa_shop_session error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def save_wa_shop_session(
    session_id: str,
    tenant_id: int,
    customer_phone: str,
    state: str,
    cart: dict,
    order_id: str = None,
):
    import json as _json
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO wa_shop_session
                (session_id, tenant_id, customer_phone, state, cart, order_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO UPDATE SET
                state      = EXCLUDED.state,
                cart       = EXCLUDED.cart,
                order_id   = COALESCE(EXCLUDED.order_id, wa_shop_session.order_id),
                updated_at = NOW()
            """,
            (session_id, tenant_id, customer_phone, state, _json.dumps(cart), order_id),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ save_wa_shop_session error:", e)
    finally:
        cur.close()
        conn.close()


def delete_wa_shop_session(session_id: str):
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM wa_shop_session WHERE session_id = %s", (session_id,))
        conn.commit()
    except Exception as e:
        print("⚠️ delete_wa_shop_session error:", e)
    finally:
        cur.close()
        conn.close()


def get_merchant_bank(tenant_id: int) -> dict | None:
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT bank_name, account_number, account_name
            FROM merchant_bank_accounts
            WHERE tenant_id = %s AND is_primary = TRUE
            LIMIT 1
            """,
            (tenant_id,),
        )
        return cur.fetchone()
    except Exception as e:
        print("⚠️ get_merchant_bank error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def get_active_gateway(tenant_id: int, gateway: str) -> bool:
    """
    True only if the tenant has a connected AND key-configured gateway of this
    type, AND has explicitly turned it on for WhatsApp checkout. Connecting
    keys alone (via /settings/payments) never enables this by itself — the
    merchant must flip wa_checkout_enabled on, and it defaults to off.

    For Flutterwave, also re-verifies the tenant's plan has feat_fw_checkout
    (Pro only) at the moment of checkout — defense in depth, same pattern as
    the Visual Product Match gate below, so a plan downgrade after enabling
    takes effect immediately without needing a manual sweep of stored flags.
    """
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if gateway == "flutterwave":
            cur.execute(
                """
                SELECT 1 FROM payment_gateways pg
                JOIN tenants t ON t.id = pg.tenant_id
                JOIN plans   p ON p.id = t.plan_id
                WHERE pg.tenant_id = %s AND pg.gateway = %s
                  AND pg.is_active = TRUE AND pg.secret_key_enc IS NOT NULL
                  AND pg.wa_checkout_enabled = TRUE
                  AND p.feat_fw_checkout = TRUE
                """,
                (tenant_id, gateway),
            )
        else:
            cur.execute(
                """
                SELECT 1 FROM payment_gateways
                WHERE tenant_id = %s AND gateway = %s
                  AND is_active = TRUE AND secret_key_enc IS NOT NULL
                  AND wa_checkout_enabled = TRUE
                """,
                (tenant_id, gateway),
            )
        return cur.fetchone() is not None
    except Exception as e:
        print(f"⚠️ get_active_gateway({gateway}) error:", e)
        return False
    finally:
        cur.close()
        conn.close()


def create_wa_order_pending(
    tenant_id: int,
    customer_phone: str,
    customer_name: str,
    cart: dict,
    delivery_type: str,
    delivery_address: str | None,
) -> tuple[str, str]:
    """
    Create order + order_items rows for a gateway-checkout order, ahead of
    payment (unlike create_wa_order, which is only called after bank-transfer
    proof arrives). Status stays at the default INTENT_CAPTURED until the
    Flutterwave webhook confirms payment. Returns (order_id, reference).
    """
    import uuid as _uuid
    conn = get_db_connection()
    if not conn:
        raise RuntimeError("DB unavailable")
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO order_reference_seq (tenant_id, last_seq) VALUES (%s, 1)
            ON CONFLICT (tenant_id) DO UPDATE SET last_seq = order_reference_seq.last_seq + 1
            RETURNING last_seq
            """,
            (tenant_id,),
        )
        seq = cur.fetchone()[0]
        reference   = f"PHX-{seq:06d}"
        order_id    = str(_uuid.uuid4())
        final_price = float(cart.get("final_price") or cart.get("unit_price") or 0)

        cur.execute(
            """
            INSERT INTO orders
                (id, tenant_id, reference, customer_phone, customer_name,
                 delivery_address, total_amount, payment_gateway)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'flutterwave')
            """,
            (
                order_id, tenant_id, reference,
                customer_phone, customer_name,
                delivery_address, final_price,
            ),
        )
        cur.execute(
            """
            INSERT INTO order_items
                (order_id, product_id, product_name, quantity, unit_price, subtotal)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                order_id,
                cart.get("product_id"),
                cart.get("product_name", ""),
                int(cart.get("quantity", 1)),
                float(cart.get("unit_price", 0)),
                final_price,
            ),
        )
        conn.commit()
        return order_id, reference
    except Exception as e:
        print("⚠️ create_wa_order_pending error:", e)
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def cancel_wa_order(order_id: str):
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE orders SET status='CANCELLED', updated_at=NOW() WHERE id=%s AND status='INTENT_CAPTURED'",
            (order_id,),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ cancel_wa_order error:", e)
    finally:
        cur.close()
        conn.close()


def get_wa_merchant_settings(tenant_id: int) -> dict:
    conn = get_db_connection()
    if not conn:
        return {"discount_mode": "merchant_only"}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT discount_mode, default_discount_type, default_discount_value
            FROM wa_merchant_settings WHERE tenant_id = %s
            """,
            (tenant_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else {
            "discount_mode":           "merchant_only",
            "default_discount_type":   "percent",
            "default_discount_value":  0.0,
        }
    except Exception as e:
        print("⚠️ get_wa_merchant_settings error:", e)
        return {"discount_mode": "merchant_only"}
    finally:
        cur.close()
        conn.close()


def get_product_discount_override(tenant_id: int, product_id: str) -> dict | None:
    """Return per-product discount override, or None if not set."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT discount_type, discount_value
            FROM wa_product_discounts
            WHERE tenant_id = %s AND product_id = %s
            """,
            (tenant_id, str(product_id)),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        print("⚠️ get_product_discount_override error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def search_tenant_products(tenant_id: int, query: str) -> list[dict]:
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, name, price, stock_quantity, discount_type, discount_value, description
            FROM products
            WHERE tenant_id = %s AND is_active = TRUE
              AND stock_quantity > reserved_quantity
              AND LOWER(name) LIKE LOWER(%s)
            ORDER BY name ASC
            LIMIT 5
            """,
            (tenant_id, f"%{query}%"),
        )
        rows = cur.fetchall() or []
        return [
            {**dict(r), "price": float(r["price"]), "discount_value": float(r["discount_value"] or 0)}
            for r in rows
        ]
    except Exception as e:
        print("⚠️ search_tenant_products error:", e)
        return []
    finally:
        cur.close()
        conn.close()


def create_wa_order(
    tenant_id: int,
    customer_phone: str,
    customer_name: str,
    cart: dict,
    delivery_type: str,
    delivery_address: str | None,
    receipt_image_url: str | None,
) -> tuple[str, str]:
    """Create order + order_items rows. Returns (order_id, reference)."""
    import uuid as _uuid
    conn = get_db_connection()
    if not conn:
        raise RuntimeError("DB unavailable")
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO order_reference_seq (tenant_id, last_seq) VALUES (%s, 1)
            ON CONFLICT (tenant_id) DO UPDATE SET last_seq = order_reference_seq.last_seq + 1
            RETURNING last_seq
            """,
            (tenant_id,),
        )
        seq = cur.fetchone()[0]
        reference   = f"PHX-{seq:06d}"
        order_id    = str(_uuid.uuid4())
        final_price = float(cart.get("final_price") or cart.get("unit_price") or 0)

        cur.execute(
            """
            INSERT INTO orders
                (id, tenant_id, reference, customer_phone, customer_name,
                 delivery_address, total_amount, status,
                 payment_method, receipt_image_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'RECEIPT_RECEIVED', 'bank_transfer', %s)
            """,
            (
                order_id, tenant_id, reference,
                customer_phone, customer_name,
                delivery_address, final_price, receipt_image_url,
            ),
        )
        cur.execute(
            """
            INSERT INTO order_items
                (order_id, product_id, product_name, quantity, unit_price, subtotal)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                order_id,
                cart.get("product_id"),
                cart.get("product_name", ""),
                int(cart.get("quantity", 1)),
                float(cart.get("unit_price", 0)),
                final_price,
            ),
        )
        conn.commit()
        return order_id, reference
    except Exception as e:
        print("⚠️ create_wa_order error:", e)
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ── Visual Product Match (Pro plan, opt-in) ─────────────────────────────────

def get_visual_match_settings(tenant_id: int) -> dict:
    """
    Cheap gate check before the gateway bothers downloading/embedding a
    customer photo. ai-backend's /visual-match endpoint re-checks this
    independently (defense in depth) — this is just a fast bail for the
    99%+ of tenants who don't have the feature.
    Returns {"enabled": bool, "on_uncertain": "clarify"|"handoff"}.
    """
    conn = get_db_connection()
    if not conn:
        return {"enabled": False, "on_uncertain": "clarify"}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT p.feat_visual_match, t.features
            FROM tenants t
            JOIN plans p ON p.id = t.plan_id
            WHERE t.id = %s
            """,
            (tenant_id,),
        )
        row = cur.fetchone()
        if not row or not row.get("feat_visual_match"):
            return {"enabled": False, "on_uncertain": "clarify"}

        features = row.get("features")
        if isinstance(features, str):
            import json
            try:
                features = json.loads(features)
            except Exception:
                features = {}
        elif not isinstance(features, dict):
            features = {}

        return {
            "enabled": bool(features.get("visual_product_match", False)),
            "on_uncertain": features.get("visual_match_on_uncertain", "clarify"),
        }
    except Exception as e:
        print("⚠️ get_visual_match_settings error:", e)
        return {"enabled": False, "on_uncertain": "clarify"}
    finally:
        cur.close()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP CAMPAIGN INTELLIGENCE (2026-09-09) — reply → funnel stage, and an
# "interested" reply optionally auto-creating a Sales Pipeline opportunity.
# See project_wa_campaign_intelligence_proposal memory for the full design.
# ══════════════════════════════════════════════════════════════════════════════

# Rank of each funnel status a reply can put a recipient into. A neutral reply
# never downgrades a recipient that's already been flagged Interested/Not
# interested back to a bare "Replied" — but Interested and Not interested CAN
# replace each other (a customer can change their mind either way in a later
# message). Rows already at 'opportunity'/'converted' are never fetched by
# get_latest_campaign_recipient_for_reply below, so they're never touched here.
_REPLY_RANK = {"sent": 0, "delivered": 0, "read": 0, "replied": 1,
               "interested": 2, "not_interested": 2}


def get_latest_campaign_recipient_for_reply(tenant_id: int, phone: str) -> dict | None:
    """Return the most recent campaign-recipient row for this phone that a
    reply can still update — i.e. it hasn't already become a real Sales
    Pipeline opportunity or been marked Converted. Returns None if this phone
    was never sent a campaign, or its only campaign(s) already progressed
    past that point."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, campaign_id, tenant_id, phone, status
            FROM wa_campaign_recipients
            WHERE tenant_id = %s AND phone = %s
              AND status IN ('sent', 'delivered', 'read', 'replied', 'interested', 'not_interested')
            ORDER BY COALESCE(sent_at, updated_at) DESC
            LIMIT 1
            """,
            (tenant_id, phone),
        )
        return cur.fetchone()
    except Exception as e:
        print("⚠️ get_latest_campaign_recipient_for_reply error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def record_campaign_reply_flag(recipient_id: int, current_status: str, sentiment: str,
                                confidence: float, reply_text: str) -> str:
    """Records the reply text on the campaign recipient row and moves its
    status forward per _REPLY_RANK. Returns the status actually written
    (which may just be current_status unchanged, for a neutral reply on an
    already-flagged recipient)."""
    if sentiment in ("interested", "not_interested"):
        new_status = sentiment
    else:
        new_status = "replied" if _REPLY_RANK.get(current_status, 0) < 1 else current_status

    conn = get_db_connection()
    if not conn:
        return current_status
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE wa_campaign_recipients
            SET status = %s, reply_text = %s, replied_at = NOW(), updated_at = NOW()
            WHERE id = %s
            """,
            (new_status, (reply_text or "")[:2000], recipient_id),
        )
        conn.commit()
        return new_status
    except Exception as e:
        print("⚠️ record_campaign_reply_flag error:", e)
        conn.rollback()
        return current_status
    finally:
        cur.close()
        conn.close()


def queue_campaign_reply_for_review(tenant_id: int, campaign_id: int, recipient_id: int,
                                     phone: str, reply_text: str, confidence: float) -> None:
    """Business has 'needs review' switched on (tenants.campaign_reply_auto_actions =
    FALSE) — queue this Interested reply for a staff member to approve/reject
    instead of creating the opportunity automatically. A second Interested reply
    from the same recipient while one is still pending just refreshes the
    existing pending row rather than creating a duplicate (partial unique index
    on recipient_id WHERE status='pending' is the arbiter)."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO wa_campaign_reply_reviews
                (tenant_id, campaign_id, recipient_id, phone, reply_text, sentiment, confidence, status)
            VALUES (%s, %s, %s, %s, %s, 'interested', %s, 'pending')
            ON CONFLICT (recipient_id) WHERE status = 'pending'
            DO UPDATE SET reply_text = EXCLUDED.reply_text, confidence = EXCLUDED.confidence, created_at = NOW()
            """,
            (tenant_id, campaign_id, recipient_id, phone, (reply_text or "")[:2000], confidence),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ queue_campaign_reply_for_review error:", e)
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def _find_matching_pipeline_lead_gw(cur, tenant_id: int, phone: str, contact_id: int | None):
    """Mirror of portal_routes.py's _find_matching_pipeline_lead — duplicated
    here because the gateway and portal are separate services/processes with
    no shared import path. Keep both in sync if the matching rule changes."""
    if contact_id:
        cur.execute(
            """
            SELECT id FROM merchant_pipeline_leads
            WHERE tenant_id=%s AND dropped_at IS NULL AND wa_contact_id=%s
            LIMIT 1
            """,
            (tenant_id, contact_id),
        )
        row = cur.fetchone()
        if row:
            return row["id"]
    if not phone:
        return None
    cur.execute(
        """
        SELECT id FROM merchant_pipeline_leads
        WHERE tenant_id=%s AND dropped_at IS NULL
          AND regexp_replace(COALESCE(whatsapp_number, phone), '[^0-9]', '', 'g')
              = regexp_replace(%s, '[^0-9]', '', 'g')
        LIMIT 1
        """,
        (tenant_id, phone),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def create_pipeline_opportunity_from_reply(tenant_id: int, campaign_id: int, recipient_id: int,
                                            phone: str, reply_text: str) -> int | None:
    """Turns an Interested campaign reply into a real Sales Pipeline opportunity
    (or links to an existing open deal for this phone, rather than creating a
    duplicate — same dedupe rule as the portal's own 'Add to Sales Pipeline'
    button). Stamps wa_campaign_recipients with the resulting lead id and moves
    it to the 'opportunity' funnel stage. Returns the lead id, or None on error."""
    import re as _re
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM wa_contacts WHERE tenant_id=%s AND phone=%s", (tenant_id, phone))
        contact = cur.fetchone()
        contact_id = contact["id"] if contact else None

        lead_id = _find_matching_pipeline_lead_gw(cur, tenant_id, phone, contact_id)
        created = False
        if not lead_id:
            cur.execute("SELECT name FROM wa_campaigns WHERE id=%s", (campaign_id,))
            _camp = cur.fetchone()
            campaign_name = (_camp["name"] if _camp else None) or "a WhatsApp campaign"
            digits_phone = _re.sub(r"[^\d]", "", phone or "")
            label = (contact.get("display_name") if contact else None) or \
                    (contact.get("contact_person") if contact else None) or phone
            notes = f'Auto-created from a WhatsApp campaign reply ("{campaign_name}"): "{(reply_text or "")[:300]}"'
            cur.execute(
                """
                INSERT INTO merchant_pipeline_leads
                  (tenant_id, customer_name, phone, whatsapp_number, email, notes, stage,
                   contact_channel, contact_date, wa_contact_id, company_id, source)
                VALUES (%s, %s, %s, %s, %s, %s, 'new_lead', 'whatsapp', CURRENT_DATE, %s, %s, 'whatsapp')
                RETURNING id
                """,
                (tenant_id, label, digits_phone, digits_phone,
                 contact.get("email") if contact else None, notes,
                 contact_id, contact.get("company_id") if contact else None),
            )
            lead_id = cur.fetchone()["id"]
            created = True
            cur.execute(
                """
                INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                VALUES (%s, NULL, 'new_lead', 'WhatsApp Campaign (auto)', %s)
                """,
                (lead_id, notes),
            )
        elif contact_id:
            # Backfill the CRM link if this pair predates it, same as the portal's own helper.
            cur.execute(
                "UPDATE merchant_pipeline_leads SET wa_contact_id=%s WHERE id=%s AND wa_contact_id IS NULL",
                (contact_id, lead_id),
            )

        cur.execute(
            """
            UPDATE wa_campaign_recipients
            SET status = 'opportunity', pipeline_lead_id = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (lead_id, recipient_id),
        )
        conn.commit()
        print(f"   [CAMPAIGN_INTEL] {'created' if created else 'linked existing'} opportunity lead_id={lead_id} "
              f"tenant={tenant_id} phone={phone} campaign_id={campaign_id}")
        return lead_id
    except Exception as e:
        print("⚠️ create_pipeline_opportunity_from_reply error:", e)
        conn.rollback()
        return None
    finally:
        cur.close()
        conn.close()
