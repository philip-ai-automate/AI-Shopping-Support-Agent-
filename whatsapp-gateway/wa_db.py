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
    """No-op: all tables already exist in PostgreSQL from pg_schema.sql."""
    pass


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
            ON CONFLICT (meta_message_id) DO NOTHING
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
    """
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        for p in products:
            product_id = str(p.get("product_id") or p.get("id") or "").strip()
            if not product_id:
                continue
            cur.execute(
                """
                INSERT INTO wa_product_cache
                  (session_id, product_id, product_name, product_url, cart_url, price)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id, product_id) DO UPDATE SET
                  product_name = EXCLUDED.product_name,
                  product_url  = EXCLUDED.product_url,
                  cart_url     = EXCLUDED.cart_url,
                  price        = EXCLUDED.price
                """,
                (
                    session_id,
                    product_id,
                    (p.get("name") or "")[:512],
                    (p.get("url") or "")[:1024],
                    (p.get("cart_url") or "")[:1024],
                    (p.get("price") or "")[:64],
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
            SELECT product_name, product_url, cart_url, price
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


def is_handoff_active(session_id: str) -> bool:
    """Return True if this session has an unresolved human handoff."""
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT id FROM wa_handoff_state WHERE session_id = %s AND resolved_at IS NULL",
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


def get_wa_merchant_settings(tenant_id: int) -> dict:
    conn = get_db_connection()
    if not conn:
        return {"discount_mode": "merchant_only"}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT discount_mode FROM wa_merchant_settings WHERE tenant_id = %s",
            (tenant_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else {"discount_mode": "merchant_only"}
    except Exception as e:
        print("⚠️ get_wa_merchant_settings error:", e)
        return {"discount_mode": "merchant_only"}
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
              AND LOWER(name) LIKE LOWER(%s)
            ORDER BY name ASC
            LIMIT 5
            """,
            (tenant_id, f"%{query}%"),
        )
        return [dict(r) for r in (cur.fetchall() or [])]
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
