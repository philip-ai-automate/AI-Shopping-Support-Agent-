import os
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv

load_dotenv()


def get_db_connection():
    try:
        conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
        )
        return conn
    except Error as e:
        print("❌ WA Gateway DB connection error:", e)
        return None


def init_wa_tables():
    conn = get_db_connection()
    if not conn:
        print("⚠️ Could not init WA tables — no DB connection")
        return
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_tenants (
              id                   INT AUTO_INCREMENT PRIMARY KEY,
              tenant_id            INT NOT NULL,
              phone_number_id      VARCHAR(64) NOT NULL,
              access_token         TEXT NOT NULL,
              waba_id              VARCHAR(64),
              verify_token         VARCHAR(128) NOT NULL,
              phixtra_api_key      VARCHAR(128) NOT NULL,
              active               BOOLEAN DEFAULT TRUE,
              signup_method        ENUM('manual','embedded') DEFAULT 'manual',
              display_phone_number VARCHAR(32),
              verified_name        VARCHAR(128),
              token_expires_at     TIMESTAMP NULL,
              created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              UNIQUE KEY uq_phone_number_id (phone_number_id)
            )
        """)
        # Migrate existing tables that are missing the new columns
        for col_sql in [
            "ALTER TABLE wa_tenants ADD COLUMN signup_method ENUM('manual','embedded') DEFAULT 'manual'",
            "ALTER TABLE wa_tenants ADD COLUMN display_phone_number VARCHAR(32)",
            "ALTER TABLE wa_tenants ADD COLUMN verified_name VARCHAR(128)",
            "ALTER TABLE wa_tenants ADD COLUMN token_expires_at TIMESTAMP NULL",
        ]:
            try:
                cur.execute(col_sql)
                conn.commit()
            except Exception:
                pass  # Column already exists
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_message_log (
              id              BIGINT AUTO_INCREMENT PRIMARY KEY,
              tenant_id       INT NOT NULL,
              phone_number_id VARCHAR(64) NOT NULL,
              customer_phone  VARCHAR(32) NOT NULL,
              direction       ENUM('inbound', 'outbound') NOT NULL,
              content         TEXT,
              message_type    VARCHAR(32) DEFAULT 'text',
              meta_message_id VARCHAR(128) UNIQUE,
              created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              INDEX idx_tenant_customer (tenant_id, customer_phone),
              INDEX idx_created (created_at)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_handoff_state (
              id             BIGINT AUTO_INCREMENT PRIMARY KEY,
              session_id     VARCHAR(128) NOT NULL UNIQUE,
              tenant_id      INT NOT NULL,
              customer_phone VARCHAR(32) NOT NULL,
              escalated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              resolved_at    TIMESTAMP NULL,
              INDEX idx_session (session_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_product_cache (
              session_id   VARCHAR(128) NOT NULL,
              product_id   VARCHAR(64)  NOT NULL,
              product_name VARCHAR(512),
              product_url  TEXT,
              cart_url     TEXT,
              price        VARCHAR(64),
              created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (session_id, product_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_templates (
              id            INT AUTO_INCREMENT PRIMARY KEY,
              tenant_id     INT NOT NULL,
              template_type VARCHAR(64) NOT NULL,
              template_name VARCHAR(128) NOT NULL,
              language_code VARCHAR(16) DEFAULT 'en',
              active        BOOLEAN DEFAULT TRUE,
              created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              UNIQUE KEY uq_tenant_type (tenant_id, template_type)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_proactive_log (
              id              BIGINT AUTO_INCREMENT PRIMARY KEY,
              tenant_id       INT NOT NULL,
              phone_number_id VARCHAR(64) NOT NULL,
              customer_phone  VARCHAR(32) NOT NULL,
              event_type      VARCHAR(64) NOT NULL,
              template_name   VARCHAR(128),
              status          ENUM('sent', 'failed', 'skipped') NOT NULL,
              notes           TEXT,
              created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              INDEX idx_tenant (tenant_id),
              INDEX idx_customer (customer_phone)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_campaigns (
              id              BIGINT AUTO_INCREMENT PRIMARY KEY,
              tenant_id       INT NOT NULL,
              name            VARCHAR(128) NOT NULL,
              campaign_type   ENUM('broadcast','cart_recovery') DEFAULT 'broadcast',
              template_name   VARCHAR(128) NOT NULL,
              language_code   VARCHAR(16) DEFAULT 'en',
              status          ENUM('draft','scheduled','running','done','failed') DEFAULT 'draft',
              scheduled_at    TIMESTAMP NULL,
              completed_at    TIMESTAMP NULL,
              total_count     INT DEFAULT 0,
              sent_count      INT DEFAULT 0,
              failed_count    INT DEFAULT 0,
              recipients      MEDIUMTEXT,
              created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              INDEX idx_tenant (tenant_id),
              INDEX idx_status (status),
              INDEX idx_scheduled (scheduled_at)
            )
        """)
        conn.commit()
        print("✅ WA Gateway tables ready")
    except Exception as e:
        print("⚠️ init_wa_tables error:", e)
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
    Returns False if the meta_message_id already exists (dedup via INSERT IGNORE).
    """
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT IGNORE INTO wa_message_log
              (tenant_id, phone_number_id, customer_phone, direction, content, message_type, meta_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
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
                ON DUPLICATE KEY UPDATE
                  product_name = VALUES(product_name),
                  product_url  = VALUES(product_url),
                  cart_url     = VALUES(cart_url),
                  price        = VALUES(price)
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
    cur = conn.cursor(dictionary=True)
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
    cur = conn.cursor(dictionary=True)
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
    cur = conn.cursor(dictionary=True)
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
    """Record a new human handoff. INSERT IGNORE is safe if already exists."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT IGNORE INTO wa_handoff_state (session_id, tenant_id, customer_phone)
            VALUES (%s, %s, %s)
            """,
            (session_id, tenant_id, customer_phone),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ create_handoff error:", e)
    finally:
        cur.close()
        conn.close()
