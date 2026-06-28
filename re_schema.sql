-- ============================================================
-- PhiXtra Real Estate Schema
-- home.phixtra.com
-- Run as: psql -h localhost -U phixtra_pg -d ai_support -f re_schema.sql
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ── re_plans ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_plans (
    id                      SERIAL PRIMARY KEY,
    slug                    VARCHAR(32)     NOT NULL UNIQUE,
    name                    VARCHAR(64)     NOT NULL,
    price_ngn               INTEGER         NOT NULL DEFAULT 0,
    price_usd               NUMERIC(10,2)   NOT NULL DEFAULT 0,
    ai_messages_limit       INTEGER         NOT NULL DEFAULT 100,
    ai_agents_limit         INTEGER         NOT NULL DEFAULT 1,
    listings_limit          INTEGER         NOT NULL DEFAULT 10,
    broadcasts_limit        INTEGER         NOT NULL DEFAULT 0,
    feat_advanced_ai        BOOLEAN         NOT NULL DEFAULT FALSE,
    feat_broadcasts         BOOLEAN         NOT NULL DEFAULT FALSE,
    feat_follow_up          BOOLEAN         NOT NULL DEFAULT FALSE,
    feat_full_reports       BOOLEAN         NOT NULL DEFAULT FALSE,
    feat_multi_agents       BOOLEAN         NOT NULL DEFAULT FALSE,
    overage_per_msg_ngn     NUMERIC(10,4)   NOT NULL DEFAULT 10,
    overage_per_msg_usd     NUMERIC(10,6)   NOT NULL DEFAULT 0.006,
    annual_discount_pct     INTEGER         NOT NULL DEFAULT 5,
    fw_plan_id_monthly      VARCHAR(100),
    fw_plan_id_annual       VARCHAR(100),
    stripe_price_id_monthly VARCHAR(100),
    stripe_price_id_annual  VARCHAR(100),
    is_active               BOOLEAN         NOT NULL DEFAULT TRUE,
    sort_order              INTEGER         NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

INSERT INTO re_plans
    (slug, name, price_ngn, price_usd, ai_messages_limit, ai_agents_limit,
     listings_limit, broadcasts_limit, feat_advanced_ai, feat_broadcasts,
     feat_follow_up, feat_full_reports, feat_multi_agents,
     annual_discount_pct, sort_order)
VALUES
    ('free',    'Free',    0,      0,   100,  1,  10,  0,     FALSE, FALSE, FALSE, FALSE, FALSE, 0,  1),
    ('starter', 'Starter', 25000,  15,  500,  1,  50,  500,   FALSE, TRUE,  FALSE, FALSE, FALSE, 10, 2),
    ('growth',  'Growth',  75000,  45,  2000, 3,  200, 2000,  TRUE,  TRUE,  TRUE,  TRUE,  FALSE, 20, 3),
    ('pro',     'Pro',     200000, 99,  0,    10, 0,   0,     TRUE,  TRUE,  TRUE,  TRUE,  TRUE,  20, 4)
ON CONFLICT (slug) DO NOTHING;


-- ── re_tenants ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_tenants (
    id                      SERIAL PRIMARY KEY,
    email                   VARCHAR(255)    NOT NULL UNIQUE,
    password_hash           VARCHAR(255)    NOT NULL,
    business_name           VARCHAR(255),
    first_name              VARCHAR(100),
    last_name               VARCHAR(100),
    phone                   VARCHAR(30),
    report_phone            VARCHAR(30),
    system_prompt           TEXT,
    plan_id                 INTEGER         REFERENCES re_plans(id) DEFAULT 1,
    billing_cycle           VARCHAR(10)     NOT NULL DEFAULT 'monthly',
    plan_period_start       DATE            NOT NULL DEFAULT CURRENT_DATE,
    trial_ends_at           DATE,
    quota_notified_at       TIMESTAMPTZ,
    wa_phone_number_id      VARCHAR(100),
    wa_access_token         TEXT,
    wa_waba_id              VARCHAR(100),
    wa_verify_token         VARCHAR(128),
    wa_display_phone        VARCHAR(32),
    wa_verified_name        VARCHAR(128),
    status                  VARCHAR(20)     NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','suspended','cancelled')),
    reset_token             VARCHAR(128),
    reset_expires_at        TIMESTAMPTZ,
    email_verified          BOOLEAN         NOT NULL DEFAULT FALSE,
    verify_token            VARCHAR(128),
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- ── re_tenant_agents ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_tenant_agents (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    name            VARCHAR(100)    NOT NULL DEFAULT 'Property Assistant',
    description     TEXT,
    system_prompt   TEXT            NOT NULL DEFAULT '',
    is_active       BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_re_agent_per_tenant
    ON re_tenant_agents(tenant_id) WHERE is_active = TRUE;


-- ── re_api_keys ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_api_keys (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    api_key_hash    VARCHAR(255)    NOT NULL UNIQUE,
    api_key_plain   VARCHAR(128),
    label           VARCHAR(100),
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ
);


-- ── re_property_listings ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_property_listings (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    title               TEXT            NOT NULL,
    property_type       VARCHAR(50)     CHECK (property_type IN
                            ('land','residential','commercial','mixed_use','industrial')),
    transaction_type    VARCHAR(20)     CHECK (transaction_type IN ('sale','rent','lease','shortlet')),
    location            TEXT,
    lga                 VARCHAR(100),
    state               VARCHAR(100)    NOT NULL DEFAULT 'Lagos',
    price               NUMERIC(15,2),
    price_negotiable    BOOLEAN         NOT NULL DEFAULT FALSE,
    bedrooms            INTEGER,
    bathrooms           INTEGER,
    toilets             INTEGER,
    size_sqm            NUMERIC(10,2),
    title_document      VARCHAR(50)     CHECK (title_document IN
                            ('C_of_O','Governors_Consent','Survey','Excision',
                             'Freehold','Deed_of_Assignment','None','Other')),
    features            JSONB           NOT NULL DEFAULT '[]',
    status              VARCHAR(20)     NOT NULL DEFAULT 'available'
                            CHECK (status IN ('available','under_offer','sold','let','off_market')),
    images              JSONB           NOT NULL DEFAULT '[]',
    description         TEXT,
    embedding           vector(1536),
    view_count          INTEGER         NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS re_listings_tenant_idx
    ON re_property_listings(tenant_id);
CREATE INDEX IF NOT EXISTS re_listings_status_idx
    ON re_property_listings(tenant_id, status);
CREATE INDEX IF NOT EXISTS re_listings_type_idx
    ON re_property_listings(tenant_id, property_type, transaction_type);
CREATE INDEX IF NOT EXISTS re_listings_embedding_idx
    ON re_property_listings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);


-- ── re_customers (buyers / leads) ────────────────────────────────────────────
-- Boom 2: AI extracts qualification fields from conversation and stores here
CREATE TABLE IF NOT EXISTS re_customers (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    phone_number        VARCHAR(32)     NOT NULL,
    name                VARCHAR(200),
    -- Boom 2: buyer qualification
    budget_min          NUMERIC(15,2),
    budget_max          NUMERIC(15,2),
    preferred_area      VARCHAR(255),
    property_type_pref  VARCHAR(50),
    transaction_pref    VARCHAR(20),    -- sale | rent | lease
    payment_method      VARCHAR(30),    -- outright | installment | mortgage
    urgency             VARCHAR(20)     CHECK (urgency IN ('urgent','planning','browsing')),
    bedrooms_pref       INTEGER,
    -- lifecycle
    lead_status         VARCHAR(30)     NOT NULL DEFAULT 'new'
                            CHECK (lead_status IN
                                ('new','qualified','inspection_booked',
                                 'negotiating','closed','lost')),
    last_seen_at        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, phone_number)
);

CREATE INDEX IF NOT EXISTS re_customers_tenant_idx
    ON re_customers(tenant_id);
CREATE INDEX IF NOT EXISTS re_customers_lead_status_idx
    ON re_customers(tenant_id, lead_status);


-- ── re_chat_messages ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_chat_messages (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    customer_id     INTEGER         NOT NULL REFERENCES re_customers(id) ON DELETE CASCADE,
    role            VARCHAR(20)     NOT NULL CHECK (role IN ('user','assistant','system')),
    content         TEXT            NOT NULL,
    embedding       vector(1536),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS re_chat_messages_tenant_customer_idx
    ON re_chat_messages(tenant_id, customer_id);
CREATE INDEX IF NOT EXISTS re_chat_messages_embedding_idx
    ON re_chat_messages USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);


-- ── re_chat_summaries ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_chat_summaries (
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    customer_id     INTEGER         NOT NULL REFERENCES re_customers(id) ON DELETE CASCADE,
    summary_text    TEXT            NOT NULL,
    message_count   INTEGER         NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, customer_id)
);


-- ── re_wa_message_log ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_wa_message_log (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    phone_number_id     VARCHAR(64)     NOT NULL,
    customer_phone      VARCHAR(32)     NOT NULL,
    direction           VARCHAR(10)     NOT NULL CHECK (direction IN ('inbound','outbound')),
    content             TEXT,
    message_type        VARCHAR(32)     DEFAULT 'text',
    meta_message_id     VARCHAR(128),
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_re_wa_message_meta_id
    ON re_wa_message_log(meta_message_id)
    WHERE meta_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS re_wa_log_tenant_idx
    ON re_wa_message_log(tenant_id, created_at);


-- ── re_handoff_rules ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_handoff_rules (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    trigger_text    VARCHAR(300)    NOT NULL,
    trigger_type    VARCHAR(30)     NOT NULL DEFAULT 'ai_initiated'
                        CHECK (trigger_type IN ('visitor_initiated','ai_initiated')),
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    sort_order      INTEGER         NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- ── re_handoff_requests ──────────────────────────────────────────────────────
-- Boom 2: buyer_summary gives agent full context at handoff
CREATE TABLE IF NOT EXISTS re_handoff_requests (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    customer_id         INTEGER         NOT NULL REFERENCES re_customers(id) ON DELETE CASCADE,
    trigger_type        VARCHAR(30)     NOT NULL DEFAULT 'ai_initiated'
                            CHECK (trigger_type IN ('visitor_initiated','ai_initiated')),
    action_type         VARCHAR(20)     CHECK (action_type IN ('INSPECT','CALLBACK','general')),
    listing_id          INTEGER         REFERENCES re_property_listings(id) ON DELETE SET NULL,
    buyer_summary       TEXT,
    visitor_message     TEXT,
    status              VARCHAR(20)     NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','handled')),
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    handled_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS re_handoff_tenant_status_idx
    ON re_handoff_requests(tenant_id, status);


-- ── re_usage_events ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_usage_events (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    event_type  VARCHAR(50)     NOT NULL DEFAULT 'ai_message',
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS re_usage_tenant_idx
    ON re_usage_events(tenant_id, created_at);


-- ── re_quota_overage_log ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_quota_overage_log (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    overage_count   INTEGER         NOT NULL DEFAULT 1,
    period_start    DATE            NOT NULL,
    logged_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- ── re_plan_subscriptions ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_plan_subscriptions (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    plan_id             INTEGER         NOT NULL REFERENCES re_plans(id),
    provider            VARCHAR(20)     CHECK (provider IN ('flutterwave','stripe','manual')),
    subscription_id     VARCHAR(255),
    status              VARCHAR(20)     NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','past_due','cancelled','paused')),
    next_billing_date   DATE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- ── BOOM 1: Follow-up Sequences ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS re_follow_up_templates (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    step_day        INTEGER         NOT NULL,   -- 2 | 5 | 10
    message_text    TEXT            NOT NULL,
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, step_day)
);

-- Default follow-up templates seeded on new tenant creation (handled in app)
-- step 2: re-engage, step 5: new listing hook, step 10: last chance

CREATE TABLE IF NOT EXISTS re_follow_up_queue (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    customer_id     INTEGER         NOT NULL REFERENCES re_customers(id) ON DELETE CASCADE,
    step_day        INTEGER         NOT NULL,
    send_at         TIMESTAMPTZ     NOT NULL,
    status          VARCHAR(20)     NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','sent','cancelled','failed')),
    sent_at         TIMESTAMPTZ,
    message_sent    TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, customer_id, step_day)
);

CREATE INDEX IF NOT EXISTS re_follow_up_send_at_idx
    ON re_follow_up_queue(send_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS re_follow_up_tenant_idx
    ON re_follow_up_queue(tenant_id, status);


-- ── BOOM 3: Listing Broadcast ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS re_listing_broadcasts (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    listing_id      INTEGER         NOT NULL REFERENCES re_property_listings(id) ON DELETE CASCADE,
    customer_id     INTEGER         NOT NULL REFERENCES re_customers(id) ON DELETE CASCADE,
    trigger_type    VARCHAR(30)     NOT NULL DEFAULT 'new_listing'
                        CHECK (trigger_type IN ('new_listing','price_drop','status_change')),
    status          VARCHAR(20)     NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','sent','failed','skipped')),
    sent_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (listing_id, customer_id)
);

CREATE INDEX IF NOT EXISTS re_broadcasts_pending_idx
    ON re_listing_broadcasts(tenant_id, status)
    WHERE status = 'pending';


-- ── BOOM 4: Inspection Booking ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS re_inspection_slots (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    listing_id      INTEGER         REFERENCES re_property_listings(id) ON DELETE CASCADE,
    slot_datetime   TIMESTAMPTZ     NOT NULL,
    duration_mins   INTEGER         NOT NULL DEFAULT 60,
    is_available    BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS re_slots_listing_idx
    ON re_inspection_slots(listing_id, is_available);
CREATE INDEX IF NOT EXISTS re_slots_tenant_idx
    ON re_inspection_slots(tenant_id, slot_datetime);

CREATE TABLE IF NOT EXISTS re_inspection_bookings (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER         NOT NULL REFERENCES re_tenants(id) ON DELETE CASCADE,
    slot_id         INTEGER         NOT NULL REFERENCES re_inspection_slots(id),
    listing_id      INTEGER         REFERENCES re_property_listings(id) ON DELETE SET NULL,
    customer_id     INTEGER         NOT NULL REFERENCES re_customers(id) ON DELETE CASCADE,
    status          VARCHAR(20)     NOT NULL DEFAULT 'confirmed'
                        CHECK (status IN ('confirmed','cancelled','completed','no_show')),
    agent_notified  BOOLEAN         NOT NULL DEFAULT FALSE,
    notes           TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS re_bookings_tenant_idx
    ON re_inspection_bookings(tenant_id, status);
CREATE INDEX IF NOT EXISTS re_bookings_slot_idx
    ON re_inspection_bookings(slot_id);
