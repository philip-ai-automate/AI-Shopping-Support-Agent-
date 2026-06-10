-- PostgreSQL schema for Phixtra ai_support database
-- Derived from actual MySQL information_schema — 2026-05-26
-- Run as: psql -h localhost -U phixtra_pg -d ai_support -f pg_schema.sql

CREATE EXTENSION IF NOT EXISTS vector;

-- ── tenants ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenants (
    id                      SERIAL PRIMARY KEY,
    name                    VARCHAR(255),
    domain                  VARCHAR(255) UNIQUE,
    system_prompt           TEXT,
    azure_search_index      VARCHAR(255),
    status                  VARCHAR(20)  NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','suspended','cancelled')),
    created_at              TIMESTAMPTZ  DEFAULT NOW(),
    azure_semantic_config   VARCHAR(255),
    features                TEXT,
    last_full_sync_at       TIMESTAMPTZ,
    source_type             VARCHAR(20)  NOT NULL DEFAULT 'web' CHECK (source_type IN ('web','whatsapp','admin')),
    daily_report_enabled    BOOLEAN      NOT NULL DEFAULT TRUE,
    report_phone            VARCHAR(30),
    last_report_sent_at     TIMESTAMPTZ
);

-- ── admins ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admins (
    id       SERIAL PRIMARY KEY,
    username VARCHAR(50)  NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL
);

-- ── admin_users ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admin_users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(50)  NOT NULL UNIQUE,
    password      VARCHAR(255) NOT NULL,
    created_at    TIMESTAMPTZ  DEFAULT NOW(),
    password_hash VARCHAR(255)
);

-- ── api_keys ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_keys (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    api_key_hash        VARCHAR(255) NOT NULL,
    website             VARCHAR(255),
    is_active           BOOLEAN      DEFAULT TRUE,
    created_at          TIMESTAMPTZ  DEFAULT NOW(),
    is_trial            BOOLEAN      NOT NULL DEFAULT FALSE,
    trial_activated_at  TIMESTAMPTZ,
    trial_expires_at    TIMESTAMPTZ,
    token_limit         INTEGER,
    tokens_used         INTEGER      NOT NULL DEFAULT 0,
    key_type            VARCHAR(20)  NOT NULL DEFAULT 'paid' CHECK (key_type IN ('paid','trial','whatsapp')),
    data_mode           VARCHAR(10)  NOT NULL DEFAULT 'index' CHECK (data_mode IN ('index','live')),
    live_types          VARCHAR(255) NOT NULL DEFAULT 'products,pages,posts',
    api_key_plain       VARCHAR(128),
    UNIQUE (tenant_id, website, key_type)
);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(is_active);

-- ── audit_logs ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_logs (
    id              SERIAL PRIMARY KEY,
    admin_username  VARCHAR(255),
    action          VARCHAR(100) NOT NULL,
    tenant_id       INTEGER,
    website         VARCHAR(255),
    key_type        VARCHAR(20)  CHECK (key_type IN ('paid','trial')),
    api_key_id      INTEGER,
    api_key_last4   VARCHAR(10),
    api_key_plain   VARCHAR(255),
    details         TEXT,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- ── abandonment_queue ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS abandonment_queue (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    session_id      VARCHAR(128) NOT NULL,
    intent_score    SMALLINT     NOT NULL DEFAULT 0,
    priority        VARCHAR(10)  NOT NULL DEFAULT 'LOW' CHECK (priority IN ('LOW','MEDIUM','HIGH')),
    cart_value      NUMERIC(10,2),
    cart_items      TEXT,
    customer_email  VARCHAR(255),
    status          VARCHAR(20)  NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','in_progress','recovered','expired')),
    touches_sent    SMALLINT     NOT NULL DEFAULT 0,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (tenant_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_aq_status  ON abandonment_queue(status);
CREATE INDEX IF NOT EXISTS idx_aq_expires ON abandonment_queue(expires_at);

-- ── cart_events ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cart_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    session_id      VARCHAR(128) NOT NULL,
    event_type      VARCHAR(64)  NOT NULL,
    cart_value      NUMERIC(10,2),
    cart_items      TEXT,
    page_url        VARCHAR(1024),
    customer_email  VARCHAR(255),
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ce_tenant_session ON cart_events(tenant_id, session_id);
CREATE INDEX IF NOT EXISTS idx_ce_tenant_time    ON cart_events(tenant_id, created_at);

-- ── chat_sessions ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id  VARCHAR(64)  NOT NULL PRIMARY KEY,
    tenant_id   INTEGER      NOT NULL,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    last_seen   TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_tenant ON chat_sessions(tenant_id);

-- ── chat_messages ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  VARCHAR(64)  NOT NULL,
    tenant_id   INTEGER      NOT NULL,
    role        VARCHAR(20)  NOT NULL CHECK (role IN ('user','assistant')),
    content     TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_tenant  ON chat_messages(tenant_id);

-- ── chat_summaries ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_summaries (
    session_id               VARCHAR(64) NOT NULL,
    tenant_id                INTEGER     NOT NULL,
    summary_text             TEXT        NOT NULL,
    summarized_message_count INTEGER     NOT NULL DEFAULT 0,
    updated_at               TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (session_id, tenant_id)
);

-- ── credit_packages ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS credit_packages (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100)   NOT NULL,
    credits         INTEGER        NOT NULL,
    price_pence     INTEGER        NOT NULL,
    currency        VARCHAR(10)    NOT NULL DEFAULT 'gbp',
    vat_rate        NUMERIC(5,2)   NOT NULL DEFAULT 20.00,
    is_active       BOOLEAN        NOT NULL DEFAULT TRUE,
    sort_order      INTEGER        NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ    DEFAULT NOW(),
    features        TEXT,
    package_type    VARCHAR(20)    NOT NULL DEFAULT 'topup' CHECK (package_type IN ('topup','subscription')),
    billing_period  VARCHAR(20)    CHECK (billing_period IN ('monthly','annual'))
);

-- ── customer_alert_state ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customer_alert_state (
    customer_id      INTEGER     NOT NULL PRIMARY KEY,
    last_alert_level VARCHAR(10),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── customers ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    id                      SERIAL PRIMARY KEY,
    tenant_id               INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    first_name              VARCHAR(100),
    last_name               VARCHAR(100),
    email                   VARCHAR(255) NOT NULL,
    phone_number            VARCHAR(30),
    phone_verified          BOOLEAN      NOT NULL DEFAULT FALSE,
    password_hash           VARCHAR(255) NOT NULL DEFAULT '',
    email_verified          BOOLEAN      NOT NULL DEFAULT FALSE,
    verify_token            VARCHAR(128),
    reset_token             VARCHAR(128),
    reset_expires_at        TIMESTAMPTZ,
    is_active               BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ  DEFAULT NOW(),
    avatar_data             TEXT,
    timezone                VARCHAR(64),
    notif_billing           BOOLEAN      NOT NULL DEFAULT TRUE,
    notif_usage             BOOLEAN      NOT NULL DEFAULT TRUE,
    notif_marketing         BOOLEAN      NOT NULL DEFAULT FALSE,
    notif_handoff           BOOLEAN      NOT NULL DEFAULT TRUE,
    handoff_notify_email    VARCHAR(254),
    stripe_customer_id      VARCHAR(255),
    company_name            VARCHAR(255),
    vat_number              VARCHAR(50),
    billing_address_line1   VARCHAR(255),
    billing_city            VARCHAR(100),
    billing_postcode        VARCHAR(20),
    billing_country         VARCHAR(10)  DEFAULT 'GB'
);

-- ── data_sources ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS data_sources (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_type     VARCHAR(20)  NOT NULL,
    display_name    VARCHAR(255),
    sheet_id        VARCHAR(255),
    sheet_tab       VARCHAR(255),
    refresh_token_enc TEXT,
    file_name       VARCHAR(255),
    file_path       VARCHAR(500),
    column_map      TEXT,
    last_synced_at  TIMESTAMPTZ,
    last_row_count  INTEGER,
    sync_status     VARCHAR(20)  NOT NULL DEFAULT 'idle',
    sync_error      TEXT,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── handoff_requests ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS handoff_requests (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL,
    session_id      VARCHAR(128) NOT NULL,
    whatsapp_number VARCHAR(50),
    visitor_message TEXT,
    status          VARCHAR(20)  NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','handled')),
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    handled_at      TIMESTAMPTZ,
    visitor_name    VARCHAR(200),
    visitor_email   VARCHAR(254)
);

-- ── handoff_rules ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS handoff_rules (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    trigger_text    VARCHAR(300) NOT NULL,
    trigger_type    VARCHAR(30)  NOT NULL DEFAULT 'ai_initiated' CHECK (trigger_type IN ('visitor_initiated','ai_initiated')),
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    sort_order      INTEGER      NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- ── invoices ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS invoices (
    id                      BIGSERIAL PRIMARY KEY,
    invoice_number          VARCHAR(50)  NOT NULL,
    tenant_id               INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id             INTEGER      NOT NULL,
    package_id              INTEGER,
    credits                 INTEGER      NOT NULL,
    amount_pence            INTEGER      NOT NULL,
    vat_pence               INTEGER      NOT NULL DEFAULT 0,
    currency                VARCHAR(10)  NOT NULL DEFAULT 'gbp',
    status                  VARCHAR(30)  NOT NULL DEFAULT 'pending',
    stripe_session_id       VARCHAR(255),
    stripe_payment_intent   VARCHAR(255),
    pdf_path                VARCHAR(512),
    created_at              TIMESTAMPTZ  DEFAULT NOW()
);

-- ── merchant_bank_accounts ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_bank_accounts (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    bank_name       VARCHAR(100) NOT NULL,
    account_number  VARCHAR(20)  NOT NULL,
    account_name    VARCHAR(255) NOT NULL,
    is_primary      BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── onboarding_state ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS onboarding_state (
    customer_id                 INTEGER     NOT NULL PRIMARY KEY,
    wizard_dismissed            BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                  TIMESTAMPTZ DEFAULT NOW(),
    ai_plugin_confirmed         BOOLEAN     NOT NULL DEFAULT FALSE,
    export_plugin_confirmed     BOOLEAN     NOT NULL DEFAULT FALSE,
    sync_configured_confirmed   BOOLEAN     NOT NULL DEFAULT FALSE,
    wa_wizard_dismissed         BOOLEAN     NOT NULL DEFAULT FALSE,
    website_wizard_dismissed    BOOLEAN     NOT NULL DEFAULT FALSE
);

-- ── orders ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id                  VARCHAR(36)    NOT NULL PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant_id           INTEGER        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    reference           VARCHAR(30)    NOT NULL,
    customer_phone      VARCHAR(25)    NOT NULL,
    customer_name       VARCHAR(255),
    delivery_address    TEXT,
    delivery_fee        NUMERIC(12,2)  NOT NULL DEFAULT 0,
    total_amount        NUMERIC(12,2)  NOT NULL DEFAULT 0,
    amount_paid         NUMERIC(12,2),
    status              VARCHAR(30)    NOT NULL DEFAULT 'INTENT_CAPTURED',
    payment_method      VARCHAR(20),
    payment_gateway     VARCHAR(20),
    gateway_reference   VARCHAR(255),
    receipt_hash        VARCHAR(64),
    receipt_image_url   TEXT,
    tracking_number     VARCHAR(100),
    courier             VARCHAR(100),
    notes               TEXT,
    paid_at             TIMESTAMPTZ,
    dispatched_at       TIMESTAMPTZ,
    delivered_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

-- ── order_items ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS order_items (
    id              BIGSERIAL PRIMARY KEY,
    order_id        VARCHAR(36)    NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id      VARCHAR(36),
    product_name    VARCHAR(255)   NOT NULL,
    quantity        INTEGER        NOT NULL DEFAULT 1,
    unit_price      NUMERIC(12,2)  NOT NULL,
    subtotal        NUMERIC(12,2)  NOT NULL
);

-- ── order_reference_seq ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS order_reference_seq (
    tenant_id   INTEGER NOT NULL PRIMARY KEY,
    last_seq    BIGINT  NOT NULL DEFAULT 0
);

-- ── payment_gateways ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS payment_gateways (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    gateway         VARCHAR(20)  NOT NULL,
    public_key      VARCHAR(255),
    secret_key_enc  TEXT,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    last_webhook_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── plugin_downloads ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plugin_downloads (
    id              SERIAL PRIMARY KEY,
    plugin_key      VARCHAR(50)  NOT NULL,
    display_name    VARCHAR(255) NOT NULL,
    filename        VARCHAR(255) NOT NULL,
    file_path       VARCHAR(512) NOT NULL,
    version         VARCHAR(50),
    uploaded_at     TIMESTAMPTZ  DEFAULT NOW()
);

-- ── portal_settings ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS portal_settings (
    id              SERIAL PRIMARY KEY,
    setting_key     VARCHAR(100) NOT NULL UNIQUE,
    setting_value   TEXT,
    updated_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- ── products ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    id              VARCHAR(36)    NOT NULL PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant_id       INTEGER        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            VARCHAR(255)   NOT NULL,
    description     TEXT,
    price           NUMERIC(12,2)  NOT NULL DEFAULT 0,
    stock_quantity  INTEGER        NOT NULL DEFAULT 0,
    reserved_quantity INTEGER      NOT NULL DEFAULT 0,
    category        VARCHAR(100),
    attributes      TEXT,
    image_url       TEXT,
    source_ref      VARCHAR(255),
    is_active       BOOLEAN        NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_products_tenant ON products(tenant_id);

-- ── push_subscriptions ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   INTEGER      NOT NULL,
    session_id  VARCHAR(128) NOT NULL,
    endpoint    TEXT         NOT NULL,
    p256dh      VARCHAR(512) NOT NULL,
    auth        VARCHAR(256) NOT NULL,
    user_agent  VARCHAR(255),
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- ── recovery_log ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS recovery_log (
    id              BIGSERIAL PRIMARY KEY,
    queue_id        INTEGER      NOT NULL,
    action_type     VARCHAR(64)  NOT NULL,
    channel         VARCHAR(32)  NOT NULL DEFAULT 'email',
    message_preview VARCHAR(255),
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- ── saved_payment_methods ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS saved_payment_methods (
    id                      SERIAL PRIMARY KEY,
    customer_id             INTEGER      NOT NULL,
    stripe_payment_method   VARCHAR(255) NOT NULL,
    card_brand              VARCHAR(30),
    card_last4              VARCHAR(4),
    card_exp_month          SMALLINT,
    card_exp_year           SMALLINT,
    is_default              BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ  DEFAULT NOW()
);

-- ── stock_notifications ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_notifications (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL,
    product_id      VARCHAR(64)  NOT NULL,
    product_name    VARCHAR(512) NOT NULL DEFAULT '',
    product_url     VARCHAR(1024) NOT NULL DEFAULT '',
    email           VARCHAR(255) NOT NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','notified','failed')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    notified_at     TIMESTAMPTZ
);

-- ── subscriptions ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscriptions (
    id                      SERIAL PRIMARY KEY,
    customer_id             INTEGER      NOT NULL,
    tenant_id               INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    package_id              INTEGER      NOT NULL,
    payment_method_id       INTEGER,
    status                  VARCHAR(20)  NOT NULL DEFAULT 'active' CHECK (status IN ('active','past_due','suspended','cancelled')),
    current_period_start    TIMESTAMPTZ  NOT NULL,
    current_period_end      TIMESTAMPTZ  NOT NULL,
    cancel_at_period_end    BOOLEAN      NOT NULL DEFAULT FALSE,
    pending_plan_id         INTEGER,
    created_at              TIMESTAMPTZ  DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  DEFAULT NOW()
);

-- ── subscription_invoices ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscription_invoices (
    id                      BIGSERIAL PRIMARY KEY,
    invoice_number          VARCHAR(50)  NOT NULL,
    subscription_id         INTEGER      NOT NULL,
    customer_id             INTEGER      NOT NULL,
    tenant_id               INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    package_id              INTEGER      NOT NULL,
    credits                 INTEGER      NOT NULL,
    amount_pence            INTEGER      NOT NULL,
    vat_pence               INTEGER      NOT NULL DEFAULT 0,
    currency                VARCHAR(10)  NOT NULL DEFAULT 'gbp',
    status                  VARCHAR(30)  NOT NULL DEFAULT 'pending',
    period_start            TIMESTAMPTZ  NOT NULL,
    period_end              TIMESTAMPTZ  NOT NULL,
    stripe_payment_intent   VARCHAR(255),
    pdf_path                VARCHAR(512),
    created_at              TIMESTAMPTZ  DEFAULT NOW()
);

-- ── system_settings ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_settings (
    setting_key     VARCHAR(100) NOT NULL PRIMARY KEY,
    setting_value   VARCHAR(255) NOT NULL,
    updated_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- ── tenant_balances ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenant_balances (
    tenant_id   INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    token_balance BIGINT  NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- ── trial_reminders ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trial_reminders (
    id          BIGSERIAL PRIMARY KEY,
    api_key_id  INTEGER      NOT NULL,
    days_before INTEGER      NOT NULL,
    sent_at     TIMESTAMPTZ  DEFAULT NOW()
);

-- ── trial_reminder_state ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trial_reminder_state (
    api_key_id  INTEGER     NOT NULL PRIMARY KEY,
    sent_3d     BOOLEAN     NOT NULL DEFAULT FALSE,
    sent_2d     BOOLEAN     NOT NULL DEFAULT FALSE,
    sent_1d     BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── trial_signups ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trial_signups (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           INTEGER      NOT NULL,
    customer_id         INTEGER      NOT NULL,
    plan_code           VARCHAR(50),
    store_domain        VARCHAR(255) NOT NULL,
    full_name           VARCHAR(255),
    mobile              VARCHAR(64),
    business_type       VARCHAR(255),
    wants_setup         BOOLEAN      NOT NULL DEFAULT FALSE,
    product_range       VARCHAR(50),
    other_requirements  TEXT,
    created_at          TIMESTAMPTZ  DEFAULT NOW()
);

-- ── usage_events ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS usage_events (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   INTEGER      NOT NULL,
    api_key_id  INTEGER      NOT NULL,
    website     VARCHAR(255),
    key_type    VARCHAR(20)  CHECK (key_type IN ('paid','trial')),
    session_id  VARCHAR(64),
    used_tokens INTEGER      NOT NULL,
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant ON usage_events(tenant_id, created_at);

-- ── wa_campaigns ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wa_campaigns (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            VARCHAR(128) NOT NULL,
    campaign_type   VARCHAR(30)  DEFAULT 'broadcast' CHECK (campaign_type IN ('broadcast','cart_recovery')),
    template_name   VARCHAR(128) NOT NULL,
    language_code   VARCHAR(16)  DEFAULT 'en',
    status          VARCHAR(20)  DEFAULT 'draft' CHECK (status IN ('draft','scheduled','running','done','failed')),
    scheduled_at    TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    total_count     INTEGER      DEFAULT 0,
    sent_count      INTEGER      DEFAULT 0,
    failed_count    INTEGER      DEFAULT 0,
    recipients      TEXT,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- ── wa_contacts ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wa_contacts (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    phone           VARCHAR(32)  NOT NULL,
    display_name    VARCHAR(200),
    notes           TEXT,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (tenant_id, phone)
);

-- ── wa_handoff_state ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wa_handoff_state (
    id              BIGSERIAL PRIMARY KEY,
    session_id      VARCHAR(128) NOT NULL,
    tenant_id       INTEGER      NOT NULL,
    customer_phone  VARCHAR(32)  NOT NULL,
    escalated_at    TIMESTAMPTZ  DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

-- ── wa_merchant_onboarding ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wa_merchant_onboarding (
    id          BIGSERIAL PRIMARY KEY,
    wa_phone    VARCHAR(32)  NOT NULL UNIQUE,
    state       VARCHAR(64)  NOT NULL DEFAULT 'COLLECT_BIZ_NAME',
    collected   TEXT         NOT NULL DEFAULT '{}',
    tenant_id   INTEGER,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- ── wa_message_log ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wa_message_log (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL,
    phone_number_id VARCHAR(64)  NOT NULL,
    customer_phone  VARCHAR(32)  NOT NULL,
    direction       VARCHAR(10)  NOT NULL CHECK (direction IN ('inbound','outbound')),
    content         TEXT,
    message_type    VARCHAR(32)  DEFAULT 'text',
    meta_message_id VARCHAR(128),
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wa_log_tenant ON wa_message_log(tenant_id, created_at);

-- ── wa_portal_otp ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wa_portal_otp (
    id          SERIAL PRIMARY KEY,
    phone       VARCHAR(30)  NOT NULL,
    otp_code    VARCHAR(10)  NOT NULL,
    expires_at  TIMESTAMPTZ  NOT NULL,
    used        BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── wa_proactive_log ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wa_proactive_log (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL,
    phone_number_id VARCHAR(64)  NOT NULL,
    customer_phone  VARCHAR(32)  NOT NULL,
    event_type      VARCHAR(64)  NOT NULL,
    template_name   VARCHAR(128),
    status          VARCHAR(20)  NOT NULL CHECK (status IN ('sent','failed','skipped')),
    notes           TEXT,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- ── wa_product_cache ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wa_product_cache (
    session_id      VARCHAR(128) NOT NULL,
    product_id      VARCHAR(64)  NOT NULL,
    product_name    VARCHAR(512),
    product_url     TEXT,
    cart_url        TEXT,
    price           VARCHAR(64),
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (session_id, product_id)
);

-- ── wa_templates ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wa_templates (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    template_type   VARCHAR(64)  NOT NULL,
    template_name   VARCHAR(128) NOT NULL,
    language_code   VARCHAR(16)  DEFAULT 'en',
    active          BOOLEAN      DEFAULT TRUE,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- ── wa_tenants ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wa_tenants (
    id                      SERIAL PRIMARY KEY,
    tenant_id               INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE UNIQUE,
    phone_number_id         VARCHAR(64)  NOT NULL,
    access_token            TEXT         NOT NULL,
    waba_id                 VARCHAR(64),
    verify_token            VARCHAR(128) NOT NULL,
    phixtra_api_key         VARCHAR(128) NOT NULL,
    active                  BOOLEAN      DEFAULT TRUE,
    signup_method           VARCHAR(20)  DEFAULT 'manual' CHECK (signup_method IN ('manual','embedded')),
    display_phone_number    VARCHAR(32),
    verified_name           VARCHAR(128),
    token_expires_at        TIMESTAMPTZ,
    created_at              TIMESTAMPTZ  DEFAULT NOW(),
    app_secret              VARCHAR(256)
);

-- ── documents (replaces Azure AI Search — pgvector store) ────────────────────
CREATE TABLE IF NOT EXISTS documents (
    id              VARCHAR(256)  PRIMARY KEY,
    tenant_id       INTEGER       NOT NULL,
    type            VARCHAR(64)   NOT NULL DEFAULT 'product',
    title           VARCHAR(512),
    content         TEXT,
    url             VARCHAR(1024),
    sku             VARCHAR(128),
    brand           VARCHAR(128),
    price_min       NUMERIC(12,2),
    price_max       NUMERIC(12,2),
    in_stock        BOOLEAN,
    categories_text TEXT,
    site_url        VARCHAR(255),
    image_url       VARCHAR(1024),
    spec_key        VARCHAR(128),
    spec_value      TEXT,
    spec_sources    TEXT,
    embedding       vector(1536),
    search_vector   tsvector,
    created_at      TIMESTAMPTZ   DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_documents_tenant        ON documents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_documents_type          ON documents(tenant_id, type);
CREATE INDEX IF NOT EXISTS idx_documents_in_stock      ON documents(tenant_id, in_stock);
CREATE INDEX IF NOT EXISTS idx_documents_embedding     ON documents
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_documents_search_vector ON documents USING GIN(search_vector);

-- Auto-populate search_vector on every insert/update
CREATE OR REPLACE FUNCTION documents_search_vector_trigger()
RETURNS trigger AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        COALESCE(NEW.title, '')         || ' ' ||
        COALESCE(NEW.content, '')       || ' ' ||
        COALESCE(NEW.sku, '')           || ' ' ||
        COALESCE(NEW.brand, '')         || ' ' ||
        COALESCE(NEW.categories_text, '')
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'tsvector_update' AND tgrelid = 'documents'::regclass
    ) THEN
        CREATE TRIGGER tsvector_update
        BEFORE INSERT OR UPDATE ON documents
        FOR EACH ROW EXECUTE FUNCTION documents_search_vector_trigger();
    END IF;
END;
$$;
