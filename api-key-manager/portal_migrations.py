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

        # ── plans ─────────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                id                  SERIAL PRIMARY KEY,
                slug                VARCHAR(32)   UNIQUE NOT NULL,
                name                VARCHAR(64)   NOT NULL,
                price_ngn           INTEGER       NOT NULL DEFAULT 0,
                price_usd           NUMERIC(10,2) NOT NULL DEFAULT 0,
                ai_messages_limit   INTEGER       NOT NULL DEFAULT 100,
                agents_limit        INTEGER       NOT NULL DEFAULT 1,
                broadcasts_limit    INTEGER       NOT NULL DEFAULT 0,
                products_limit      INTEGER       NOT NULL DEFAULT 50,
                data_sources_limit  INTEGER       NOT NULL DEFAULT 1,
                feat_crm            BOOLEAN       NOT NULL DEFAULT FALSE,
                feat_advanced_ai    BOOLEAN       NOT NULL DEFAULT FALSE,
                feat_integrations   BOOLEAN       NOT NULL DEFAULT FALSE,
                feat_broadcasts     BOOLEAN       NOT NULL DEFAULT FALSE,
                feat_full_reports   BOOLEAN       NOT NULL DEFAULT FALSE,
                feat_multi_agents   BOOLEAN       NOT NULL DEFAULT FALSE,
                overage_per_msg_ngn NUMERIC(10,4) NOT NULL DEFAULT 10,
                overage_per_msg_usd NUMERIC(10,6) NOT NULL DEFAULT 0.006000,
                is_active           BOOLEAN       NOT NULL DEFAULT TRUE,
                sort_order          INTEGER       NOT NULL DEFAULT 0,
                created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
            )
        """)

        # Seed the 4 plans (idempotent — slug is UNIQUE)
        cur.execute("""
            INSERT INTO plans
                (slug, name, price_ngn, price_usd,
                 ai_messages_limit, agents_limit, broadcasts_limit,
                 products_limit, data_sources_limit,
                 feat_crm, feat_advanced_ai, feat_integrations,
                 feat_broadcasts, feat_full_reports, feat_multi_agents,
                 overage_per_msg_ngn, overage_per_msg_usd, sort_order)
            VALUES
              ('free',    'Free',    0,      0,     100,    1,  0,    50,   1,  FALSE,FALSE,FALSE,FALSE,FALSE,FALSE, 10,     0.006000, 0),
              ('starter', 'Starter', 15000,  10.00, 2000,   2,  500,  500,  3,  TRUE, FALSE,TRUE, TRUE, TRUE, TRUE,   5,     0.003000, 1),
              ('growth',  'Growth',  48000,  30.00, 10000,  5,  5000, 2000, 10, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE,   3,     0.002000, 2),
              ('pro',     'Pro',     120000, 75.00, 50000, -1,  -1,   -1,   -1, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE,   2,     0.001200, 3)
            ON CONFLICT (slug) DO NOTHING
        """)

        # ── quota_overage_log ──────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quota_overage_log (
                id          BIGSERIAL PRIMARY KEY,
                tenant_id   INTEGER      NOT NULL,
                logged_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                plan_slug   VARCHAR(32),
                msgs_used   INTEGER,
                msgs_limit  INTEGER,
                rate_ngn    NUMERIC(10,4),
                notified    BOOLEAN      NOT NULL DEFAULT FALSE
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_quota_overage_tenant
                ON quota_overage_log(tenant_id, logged_at)
        """)

        # ── tenants: add plan columns ──────────────────────────────────────────
        if not _column_exists(cur, "tenants", "plan_id"):
            cur.execute("ALTER TABLE tenants ADD COLUMN plan_id INTEGER REFERENCES plans(id) DEFAULT 1")
        if not _column_exists(cur, "tenants", "billing_cycle"):
            cur.execute("ALTER TABLE tenants ADD COLUMN billing_cycle VARCHAR(10) NOT NULL DEFAULT 'monthly'")
        if not _column_exists(cur, "tenants", "plan_period_start"):
            cur.execute("ALTER TABLE tenants ADD COLUMN plan_period_start DATE NOT NULL DEFAULT CURRENT_DATE")
        if not _column_exists(cur, "tenants", "quota_notified_at"):
            cur.execute("ALTER TABLE tenants ADD COLUMN quota_notified_at TIMESTAMPTZ DEFAULT NULL")
        if not _column_exists(cur, "tenants", "trial_ends_at"):
            cur.execute("ALTER TABLE tenants ADD COLUMN trial_ends_at DATE DEFAULT NULL")
        if not _column_exists(cur, "tenants", "is_founder"):
            cur.execute("ALTER TABLE tenants ADD COLUMN is_founder BOOLEAN NOT NULL DEFAULT FALSE")
        if not _column_exists(cur, "tenants", "founder_year"):
            cur.execute("ALTER TABLE tenants ADD COLUMN founder_year SMALLINT NOT NULL DEFAULT 0")

        # ── wa_campaign_recipients ─────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_campaign_recipients (
                id          BIGSERIAL PRIMARY KEY,
                campaign_id BIGINT      NOT NULL REFERENCES wa_campaigns(id) ON DELETE CASCADE,
                tenant_id   INTEGER     NOT NULL,
                phone       VARCHAR(30) NOT NULL,
                status      VARCHAR(20) NOT NULL DEFAULT 'pending',
                error_msg   TEXT,
                sent_at     TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_wcr_campaign
                ON wa_campaign_recipients(campaign_id)
        """)

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️  catalogue migration error:", e)
    finally:
        cur.close()
        conn.close()
