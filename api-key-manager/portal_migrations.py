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


def _constraint_exists(cur, constraint_name: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM pg_constraint WHERE conname=%s",
        (constraint_name,),
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
        # ── ambassador_leads ──────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ambassador_leads (
                id                  SERIAL PRIMARY KEY,
                ambassador_id       INT REFERENCES ambassadors(id),
                business_name       TEXT NOT NULL,
                contact_name        TEXT,
                phone               TEXT,
                email               TEXT,
                notes               TEXT,
                status              TEXT DEFAULT 'new',
                closed_at           TIMESTAMPTZ,
                commission_triggered BOOLEAN DEFAULT FALSE,
                created_at          TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # ambassador_commissions.tenant_id must allow NULL for lead commissions
        # (leads don't have a corresponding tenant record)
        if _column_exists(cur, "ambassador_commissions", "tenant_id"):
            cur.execute("""
                ALTER TABLE ambassador_commissions
                ALTER COLUMN tenant_id DROP NOT NULL
            """)

        # ── CRM pipeline columns on ambassador_leads (replaces flat status model) ──
        _lead_pipeline_columns = [
            ("industry",               "TEXT"),
            ("stage",                  "VARCHAR(30) NOT NULL DEFAULT 'lead'"),
            ("contact_channel",        "TEXT"),
            ("contact_date",           "DATE"),
            ("contact_response",       "TEXT"),
            ("demo_date",              "DATE"),
            ("demo_reaction",          "TEXT"),
            ("req_phone",              "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("req_meta_account",       "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("req_whatsapp_connected", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("req_product_list",       "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("onboarding_date",        "DATE"),
            ("onboarding_notes",       "TEXT"),
            ("tenant_id",              "INTEGER REFERENCES tenants(id)"),
            ("dropped_at",             "TIMESTAMPTZ"),
            ("dropped_reason",         "TEXT"),
            ("last_reviewed_at",       "TIMESTAMPTZ"),
        ]
        for col_name, col_def in _lead_pipeline_columns:
            if not _column_exists(cur, "ambassador_leads", col_name):
                cur.execute(f"ALTER TABLE ambassador_leads ADD COLUMN {col_name} {col_def}")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS lead_stage_history (
                id          SERIAL PRIMARY KEY,
                lead_id     INTEGER NOT NULL REFERENCES ambassador_leads(id) ON DELETE CASCADE,
                from_stage  VARCHAR(30),
                to_stage    VARCHAR(30) NOT NULL,
                changed_by  TEXT,
                notes       TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS lead_support_tickets (
                id          SERIAL PRIMARY KEY,
                lead_id     INTEGER NOT NULL REFERENCES ambassador_leads(id) ON DELETE CASCADE,
                subject     TEXT NOT NULL,
                notes       TEXT,
                created_by  TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at TIMESTAMPTZ
            )
        """)
        conn.commit()

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
                ai_agents_limit     INTEGER       NOT NULL DEFAULT 1,
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
                 ai_messages_limit, ai_agents_limit, broadcasts_limit,
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

        # ── login_attempts: rate-limit failed ambassador logins ───────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                id           SERIAL       PRIMARY KEY,
                ip_address   VARCHAR(45)  NOT NULL,
                email        VARCHAR(255),
                attempted_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time
            ON login_attempts(ip_address, attempted_at)
        """)

        # ── Unique constraints on ambassadors: block duplicate phone / whatsapp ──
        if _table_exists(cur, "ambassadors"):
            if not _constraint_exists(cur, "ambassadors_phone_key"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD CONSTRAINT ambassadors_phone_key UNIQUE (phone)"
                )
            if not _constraint_exists(cur, "ambassadors_whatsapp_key"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD CONSTRAINT ambassadors_whatsapp_key UNIQUE (whatsapp_number)"
                )

        # ── Per-ambassador demo portal tenant ─────────────────────────────────────
        if not _column_exists(cur, "tenants", "is_demo"):
            cur.execute(
                "ALTER TABLE tenants ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT FALSE"
            )
        if _table_exists(cur, "ambassadors"):
            if not _column_exists(cur, "ambassadors", "demo_tenant_id"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN demo_tenant_id INTEGER REFERENCES tenants(id)"
                )
            if not _column_exists(cur, "ambassadors", "demo_token"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN demo_token VARCHAR(64) UNIQUE"
                )

        # ── Sales Manager role + recruitment hierarchy ────────────────────────────
        if _table_exists(cur, "ambassadors"):
            if not _column_exists(cur, "ambassadors", "role"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'ambassador'"
                )
            if not _column_exists(cur, "ambassadors", "recruited_by_id"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN recruited_by_id INTEGER REFERENCES ambassadors(id)"
                )

        # ── tenant_agents: AI agent profiles per tenant ──────────────────────────
        if not _table_exists(cur, "tenant_agents"):
            cur.execute("""
                CREATE TABLE tenant_agents (
                    id            SERIAL PRIMARY KEY,
                    tenant_id     INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name          VARCHAR(100) NOT NULL DEFAULT 'Default Agent',
                    description   TEXT,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    is_active     BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE UNIQUE INDEX uq_one_active_agent_per_tenant
                    ON tenant_agents (tenant_id) WHERE is_active = TRUE
            """)
            cur.execute("""
                INSERT INTO tenant_agents (tenant_id, name, system_prompt, is_active)
                SELECT t.id, 'Default Agent', COALESCE(t.system_prompt, ''), TRUE
                FROM tenants t
            """)

        # ── wa_tenants: multi-number support ────────────────────────────────────
        # Drop old 1-number-per-tenant unique constraint if still present
        cur.execute("""
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name='wa_tenants' AND constraint_name='wa_tenants_tenant_id_key'
        """)
        if cur.fetchone():
            cur.execute("ALTER TABLE wa_tenants DROP CONSTRAINT wa_tenants_tenant_id_key")

        # Add agent_id FK column to wa_tenants if missing
        if not _column_exists(cur, "wa_tenants", "agent_id"):
            cur.execute("""
                ALTER TABLE wa_tenants
                ADD COLUMN agent_id INTEGER REFERENCES tenant_agents(id) ON DELETE SET NULL
            """)

        # Rename agents_limit → ai_agents_limit if old column still exists
        if _column_exists(cur, "plans", "agents_limit"):
            cur.execute("ALTER TABLE plans RENAME COLUMN agents_limit TO ai_agents_limit")
            cur.execute("UPDATE plans SET ai_agents_limit = 1 WHERE slug = 'free'")
            cur.execute("UPDATE plans SET ai_agents_limit = 1 WHERE slug = 'starter'")
            cur.execute("UPDATE plans SET ai_agents_limit = 3 WHERE slug = 'growth'")
            cur.execute("UPDATE plans SET ai_agents_limit = 10 WHERE slug = 'pro'")

        # ── catalogue_departments ─────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_departments (
                id          SERIAL PRIMARY KEY,
                name        VARCHAR(100) NOT NULL,
                slug        VARCHAR(100) NOT NULL UNIQUE,
                icon        VARCHAR(50)  NOT NULL DEFAULT '🏪',
                description TEXT,
                sort_order  INT          NOT NULL DEFAULT 0,
                is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
                created_by  VARCHAR(100),
                created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)

        # Seed 7 built-in departments (idempotent)
        cur.execute("""
            INSERT INTO catalogue_departments (name, slug, icon, description, sort_order)
            VALUES
              ('Electronics',          'electronics',   '📱', 'Phones, laptops, TVs, gadgets and accessories',            1),
              ('Pharmacy',             'pharmacy',      '💊', 'Medications, supplements, medical devices and health aids', 2),
              ('Beauty & Cosmetics',   'beauty',        '💄', 'Skincare, haircare, makeup and personal care products',     3),
              ('Supermarket / FMCG',   'supermarket',   '🛒', 'Food, beverages, household items and everyday consumables', 4),
              ('Office Equipment',     'office',        '🖨', 'Printers, furniture, stationery and office supplies',       5),
              ('Furniture',            'furniture',     '🛋', 'Home and office furniture, décor and fixtures',             6),
              ('Apparel & Fashion',    'fashion',       '👗', 'Clothing, footwear, bags and fashion accessories',          7)
            ON CONFLICT (slug) DO NOTHING
        """)

        # ── catalogue_subcategories ───────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_subcategories (
                id          SERIAL PRIMARY KEY,
                category_id INT          NOT NULL REFERENCES catalogue_categories(id) ON DELETE CASCADE,
                name        VARCHAR(100) NOT NULL,
                slug        VARCHAR(100) NOT NULL,
                sort_order  INT          NOT NULL DEFAULT 0,
                is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
                UNIQUE (category_id, slug)
            )
        """)

        # ── catalogue_categories: add department_id column ────────────────
        if not _column_exists(cur, "catalogue_categories", "department_id"):
            cur.execute("""
                ALTER TABLE catalogue_categories
                ADD COLUMN department_id INT REFERENCES catalogue_departments(id) ON DELETE SET NULL
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_cat_categories_department
                    ON catalogue_categories(department_id)
            """)

        # ── onboarding_state: default_department_id for admin-assigned dept
        if not _column_exists(cur, "onboarding_state", "default_department_id"):
            cur.execute("""
                ALTER TABLE onboarding_state
                ADD COLUMN default_department_id INT REFERENCES catalogue_departments(id) ON DELETE SET NULL
            """)

        # ── catalogue_variant_types ───────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_variant_types (
                id          SERIAL PRIMARY KEY,
                category_id INT          NOT NULL REFERENCES catalogue_categories(id) ON DELETE CASCADE,
                name        VARCHAR(50)  NOT NULL,
                sort_order  INT          NOT NULL DEFAULT 0,
                UNIQUE (category_id, name)
            )
        """)

        # ── catalogue_variant_options ─────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_variant_options (
                id              SERIAL PRIMARY KEY,
                variant_type_id INT          NOT NULL REFERENCES catalogue_variant_types(id) ON DELETE CASCADE,
                value           VARCHAR(100) NOT NULL,
                sort_order      INT          NOT NULL DEFAULT 0,
                UNIQUE (variant_type_id, value)
            )
        """)

        # ── catalogue_product_variants ────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_product_variants (
                id             SERIAL PRIMARY KEY,
                product_id     INT           NOT NULL REFERENCES catalogue_products(id) ON DELETE CASCADE,
                sku            VARCHAR(128)  UNIQUE,
                price_modifier NUMERIC(10,2) NOT NULL DEFAULT 0,
                stock_status   VARCHAR(20)   NOT NULL DEFAULT 'in_stock',
                is_active      BOOLEAN       NOT NULL DEFAULT TRUE,
                variant_combo  JSONB         NOT NULL DEFAULT '{}',
                created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_prod_variants_product
                ON catalogue_product_variants(product_id)
        """)

        # ── merchant_product_variants ─────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS merchant_product_variants (
                merchant_id INT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                variant_id  INT NOT NULL REFERENCES catalogue_product_variants(id) ON DELETE CASCADE,
                is_active   BOOLEAN NOT NULL DEFAULT TRUE,
                selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (merchant_id, variant_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_merchant_prod_variants_merchant
                ON merchant_product_variants(merchant_id)
        """)

        # ── catalogue_products: extended fields (Phase 3) ────────────────
        for col, ddl in [
            ("barcode",         "VARCHAR(64)"),
            ("unit_of_measure", "VARCHAR(20)"),
            ("weight_value",    "NUMERIC(10,3)"),
            ("weight_unit",     "VARCHAR(10)"),
            ("shelf_life_days", "INT"),
            ("requires_rxn",    "BOOLEAN"),
            ("regulatory_ref",  "VARCHAR(128)"),
            ("dimensions_cm",   "VARCHAR(64)"),
        ]:
            if not _column_exists(cur, "catalogue_products", col):
                cur.execute(f"ALTER TABLE catalogue_products ADD COLUMN {col} {ddl}")

        # ── catalogue_industry_templates ──────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_industry_templates (
                id            SERIAL PRIMARY KEY,
                name          VARCHAR(100) NOT NULL,
                slug          VARCHAR(100) NOT NULL UNIQUE,
                department_id INT REFERENCES catalogue_departments(id) ON DELETE SET NULL,
                attributes    JSONB        NOT NULL DEFAULT '[]',
                is_builtin    BOOLEAN      NOT NULL DEFAULT FALSE,
                created_by    VARCHAR(100),
                created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)

        # Seed 7 built-in templates (idempotent via slug UNIQUE)
        import json as _json

        _templates = [
            ("Electronics", "electronics", "electronics", [
                {"key": "storage",       "label": "Storage",          "data_type": "text",   "unit": "GB",  "is_required": False, "is_filterable": True,  "sort_order": 1},
                {"key": "ram",           "label": "RAM",              "data_type": "text",   "unit": "GB",  "is_required": False, "is_filterable": True,  "sort_order": 2},
                {"key": "display",       "label": "Display Size",     "data_type": "text",   "unit": "inch","is_required": False, "is_filterable": False, "sort_order": 3},
                {"key": "processor",     "label": "Processor",        "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": False, "sort_order": 4},
                {"key": "battery",       "label": "Battery",          "data_type": "text",   "unit": "mAh", "is_required": False, "is_filterable": False, "sort_order": 5},
                {"key": "connectivity",  "label": "Connectivity",     "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 6},
                {"key": "colour",        "label": "Colour",           "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 7},
                {"key": "warranty",      "label": "Warranty",         "data_type": "text",   "unit": "months","is_required": False,"is_filterable": False,"sort_order": 8},
            ]),
            ("Pharmacy", "pharmacy", "pharmacy", [
                {"key": "generic_name",  "label": "Generic Name",     "data_type": "text",   "unit": "",    "is_required": True,  "is_filterable": True,  "sort_order": 1},
                {"key": "dosage_form",   "label": "Dosage Form",      "data_type": "text",   "unit": "",    "is_required": True,  "is_filterable": True,  "sort_order": 2},
                {"key": "strength",      "label": "Strength",         "data_type": "text",   "unit": "mg/ml","is_required": True, "is_filterable": True,  "sort_order": 3},
                {"key": "pack_size",     "label": "Pack Size",        "data_type": "text",   "unit": "",    "is_required": True,  "is_filterable": False, "sort_order": 4},
                {"key": "nafdac_no",     "label": "NAFDAC No.",       "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": False, "sort_order": 5},
                {"key": "requires_rxn",  "label": "Prescription Required","data_type":"boolean","unit":"","is_required": True,  "is_filterable": True,  "sort_order": 6},
                {"key": "controlled",    "label": "Controlled Substance","data_type":"boolean","unit":"",  "is_required": False, "is_filterable": False, "sort_order": 7},
                {"key": "manufacturer",  "label": "Manufacturer",     "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 8},
            ]),
            ("Beauty & Cosmetics", "beauty", "beauty", [
                {"key": "skin_type",     "label": "Skin Type",        "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 1},
                {"key": "ingredients",   "label": "Key Ingredients",  "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": False, "sort_order": 2},
                {"key": "volume",        "label": "Volume / Weight",  "data_type": "text",   "unit": "ml/g","is_required": True,  "is_filterable": True,  "sort_order": 3},
                {"key": "shade",         "label": "Shade / Variant",  "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 4},
                {"key": "spf",           "label": "SPF",              "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": False, "sort_order": 5},
                {"key": "cruelty_free",  "label": "Cruelty-Free",     "data_type": "boolean","unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 6},
                {"key": "usage",         "label": "Usage Instructions","data_type": "text",  "unit": "",    "is_required": False, "is_filterable": False, "sort_order": 7},
            ]),
            ("Supermarket / FMCG", "supermarket", "supermarket", [
                {"key": "weight_volume", "label": "Weight / Volume",  "data_type": "text",   "unit": "",    "is_required": True,  "is_filterable": False, "sort_order": 1},
                {"key": "uom",           "label": "Unit of Measure",  "data_type": "text",   "unit": "",    "is_required": True,  "is_filterable": True,  "sort_order": 2},
                {"key": "pack_size",     "label": "Pack Size",        "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 3},
                {"key": "barcode",       "label": "Barcode / UPC",    "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": False, "sort_order": 4},
                {"key": "shelf_life",    "label": "Shelf Life",       "data_type": "number", "unit": "days","is_required": False, "is_filterable": False, "sort_order": 5},
                {"key": "country_origin","label": "Country of Origin","data_type": "text",   "unit": "",    "is_required": False, "is_filterable": False, "sort_order": 6},
            ]),
            ("Apparel & Fashion", "fashion", "fashion", [
                {"key": "gender",        "label": "Gender",           "data_type": "text",   "unit": "",    "is_required": True,  "is_filterable": True,  "sort_order": 1},
                {"key": "material",      "label": "Material",         "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 2},
                {"key": "size_range",    "label": "Size Range",       "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 3},
                {"key": "colour",        "label": "Colour",           "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 4},
                {"key": "care",          "label": "Care Instructions","data_type": "text",   "unit": "",    "is_required": False, "is_filterable": False, "sort_order": 5},
                {"key": "style",         "label": "Style",            "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 6},
            ]),
            ("Furniture", "furniture", "furniture", [
                {"key": "material",      "label": "Material",         "data_type": "text",   "unit": "",    "is_required": True,  "is_filterable": True,  "sort_order": 1},
                {"key": "dimensions",    "label": "Dimensions (L×W×H)","data_type": "text",  "unit": "cm",  "is_required": False, "is_filterable": False, "sort_order": 2},
                {"key": "finish",        "label": "Finish / Colour",  "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 3},
                {"key": "weight_cap",    "label": "Weight Capacity",  "data_type": "number", "unit": "kg",  "is_required": False, "is_filterable": False, "sort_order": 4},
                {"key": "assembly",      "label": "Assembly Required","data_type": "boolean","unit": "",    "is_required": False, "is_filterable": False, "sort_order": 5},
                {"key": "room_type",     "label": "Room Type",        "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 6},
            ]),
            ("Office Equipment", "office", "office", [
                {"key": "equipment_type","label": "Equipment Type",   "data_type": "text",   "unit": "",    "is_required": True,  "is_filterable": True,  "sort_order": 1},
                {"key": "connectivity",  "label": "Connectivity",     "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": True,  "sort_order": 2},
                {"key": "power_watts",   "label": "Power Consumption","data_type": "number", "unit": "W",   "is_required": False, "is_filterable": False, "sort_order": 3},
                {"key": "warranty",      "label": "Warranty",         "data_type": "number", "unit": "months","is_required": False,"is_filterable": False,"sort_order": 4},
                {"key": "colour",        "label": "Colour",           "data_type": "text",   "unit": "",    "is_required": False, "is_filterable": False, "sort_order": 5},
            ]),
        ]

        for (tpl_name, tpl_slug, dept_slug, attrs) in _templates:
            cur.execute(
                "SELECT id FROM catalogue_departments WHERE slug=%s", (dept_slug,)
            )
            dept_row = cur.fetchone()
            dept_id  = dept_row[0] if dept_row else None
            cur.execute("""
                INSERT INTO catalogue_industry_templates
                    (name, slug, department_id, attributes, is_builtin)
                VALUES (%s, %s, %s, %s, TRUE)
                ON CONFLICT (slug) DO NOTHING
            """, (tpl_name, tpl_slug, dept_id, _json.dumps(attrs)))

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️  catalogue migration error:", e)
    finally:
        cur.close()
        conn.close()
