"""
portal_migrations.py  — pgvector/PostgreSQL edition

All tables were created by pg_schema.sql and data migrated.
ensure_portal_tables() is a no-op but kept so startup code continues to work.
_column_exists() is retained for any future migration additions.
"""
import psycopg2.extras
from db import get_db_connection


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = %s AND column_name = %s""",
        (table, column),
    )
    return int((cur.fetchone() or [0])[0]) > 0


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    return int((cur.fetchone() or [0])[0]) > 0


def ensure_portal_tables():
    """Idempotent: create multi-category catalogue tables if they don't exist."""
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        # ── catalogue_categories ──────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_categories (
                id          SERIAL PRIMARY KEY,
                name        VARCHAR(100) NOT NULL,
                slug        VARCHAR(100) NOT NULL UNIQUE,
                icon        VARCHAR(50)  NOT NULL DEFAULT 'box',
                description TEXT,
                sort_order  INT          NOT NULL DEFAULT 0,
                is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
                created_by  VARCHAR(100),
                created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)

        # ── catalogue_attribute_definitions ──────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_attribute_definitions (
                id              SERIAL PRIMARY KEY,
                category_id     INT          NOT NULL REFERENCES catalogue_categories(id) ON DELETE CASCADE,
                attribute_key   VARCHAR(50)  NOT NULL,
                attribute_label VARCHAR(100) NOT NULL,
                data_type       VARCHAR(20)  NOT NULL DEFAULT 'text',
                unit            VARCHAR(20),
                is_filterable   BOOLEAN      NOT NULL DEFAULT FALSE,
                is_required     BOOLEAN      NOT NULL DEFAULT FALSE,
                sort_order      INT          NOT NULL DEFAULT 0,
                UNIQUE (category_id, attribute_key)
            )
        """)

        # ── catalogue_products ────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_products (
                id           SERIAL PRIMARY KEY,
                category_id  INT          NOT NULL REFERENCES catalogue_categories(id) ON DELETE CASCADE,
                brand        VARCHAR(128),
                model_name   VARCHAR(256) NOT NULL,
                model_number VARCHAR(128),
                sku          VARCHAR(128) UNIQUE,
                description  TEXT,
                image_url    TEXT,
                is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
                created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_cat_products_category
                ON catalogue_products(category_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_cat_products_brand
                ON catalogue_products(brand)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_cat_products_active
                ON catalogue_products(is_active)
        """)

        # ── catalogue_product_attributes ──────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_product_attributes (
                product_id       INT  NOT NULL REFERENCES catalogue_products(id) ON DELETE CASCADE,
                attribute_def_id INT  NOT NULL REFERENCES catalogue_attribute_definitions(id) ON DELETE CASCADE,
                value            TEXT,
                PRIMARY KEY (product_id, attribute_def_id)
            )
        """)

        # ── catalogue_uploads ─────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_uploads (
                id             SERIAL PRIMARY KEY,
                admin_username VARCHAR(100),
                category_id    INT         REFERENCES catalogue_categories(id) ON DELETE SET NULL,
                filename       VARCHAR(255),
                total_rows     INT         NOT NULL DEFAULT 0,
                successful     INT         NOT NULL DEFAULT 0,
                failed         INT         NOT NULL DEFAULT 0,
                status         VARCHAR(20) NOT NULL DEFAULT 'completed',
                error_details  JSONB       NOT NULL DEFAULT '[]',
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── onboarding_state: catalogue_setup_done column ────────────────
        if not _column_exists(cur, "onboarding_state", "catalogue_setup_done"):
            cur.execute("""
                ALTER TABLE onboarding_state
                ADD COLUMN catalogue_setup_done BOOLEAN NOT NULL DEFAULT FALSE
            """)

        # ── merchant_product_catalogue ────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS merchant_product_catalogue (
                merchant_id  INT          NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                product_id   INT          NOT NULL REFERENCES catalogue_products(id) ON DELETE CASCADE,
                is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
                selected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                PRIMARY KEY (merchant_id, product_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_merchant_cat_merchant
                ON merchant_product_catalogue(merchant_id)
        """)

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️  catalogue migration error:", e)
    finally:
        cur.close()
        conn.close()
