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
        # ── admin_users: role/permissions for scoped support-team logins ──
        if not _column_exists(cur, "admin_users", "role"):
            cur.execute("ALTER TABLE admin_users ADD COLUMN role VARCHAR NOT NULL DEFAULT 'owner'")
        if not _column_exists(cur, "admin_users", "permissions"):
            cur.execute("ALTER TABLE admin_users ADD COLUMN permissions JSONB NOT NULL DEFAULT '{}'")
        if not _column_exists(cur, "admin_users", "active"):
            cur.execute("ALTER TABLE admin_users ADD COLUMN active BOOLEAN NOT NULL DEFAULT TRUE")
        conn.commit()

        # ── tenants.signup_product: which storefront (portal.phixtra.com vs
        # connect.phixtra.com) a merchant actually signed up through, captured
        # once at registration so the admin Customers filter can tell Portal
        # and Connect accounts apart later — ai_enabled alone isn't reliable
        # for this since staff can flip it any time after signup.
        if not _column_exists(cur, "tenants", "signup_product"):
            cur.execute("ALTER TABLE tenants ADD COLUMN signup_product VARCHAR(20) NOT NULL DEFAULT 'portal'")
            # One-off backfill for accounts that existed before this column:
            # ai_enabled was set to `not is_connect_host()` at signup time, so
            # it's the best available guess for rows we didn't tag directly.
            cur.execute("UPDATE tenants SET signup_product='connect' WHERE ai_enabled=FALSE")
            conn.commit()

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
            # Scheduled-vs-completed split: these hold a *planned* date for the
            # next activity (e.g. "demo booked for 9 Jul") without touching
            # `stage` or the completion date columns above. Cleared once the
            # matching completion date is actually logged.
            ("contact_scheduled_date",     "DATE"),
            ("demo_scheduled_date",        "DATE"),
            ("onboarding_scheduled_date",  "DATE"),
            # Free-text context for partial requirements-checklist progress,
            # e.g. "waiting on dedicated phone number" — lets the 4 req_*
            # checkboxes be saved incrementally before all 4 are true.
            ("requirements_notes",         "TEXT"),
            # Onboarding checklist — this is the work done *during* the
            # onboarding stage, so unlike req_*/requirements_confirmed (which
            # gates entry into a stage) these gate the EXIT into active_client.
            ("onboard_products_uploaded",  "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("onboard_whatsapp_connected", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("onboard_login_sent",         "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("onboard_client_trained",     "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("onboarding_checklist_notes", "TEXT"),
            # Direct admin→Sales Manager assignment, independent of the normal
            # ambassador_id/recruited_by_id chain — used for customers who signed
            # up on their own (company campaign/promo, not an ambassador referral)
            # but still need a manager chasing them for onboarding follow-up.
            # ambassador_id stays NULL on these rows; team_pipeline() matches on
            # this column as well as recruited_by_id so they show up in that
            # manager's queue like any other lead.
            ("sales_manager_id",           "INTEGER REFERENCES ambassadors(id)"),
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

        # ── sales_manager_targets — admin-set monthly KPI targets per Sales
        # Manager, measured automatically against their real Team Pipeline
        # activity (see lead_pipeline.sales_manager_month_progress). One row
        # per manager per calendar month (period_month always the 1st).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sales_manager_targets (
                id                     SERIAL PRIMARY KEY,
                ambassador_id          INTEGER NOT NULL REFERENCES ambassadors(id) ON DELETE CASCADE,
                period_month           DATE NOT NULL,
                target_new_leads       INTEGER NOT NULL DEFAULT 0,
                target_demos_done      INTEGER NOT NULL DEFAULT 0,
                target_active_clients  INTEGER NOT NULL DEFAULT 0,
                notes                  TEXT,
                created_by             TEXT,
                created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (ambassador_id, period_month)
            )
        """)
        conn.commit()

        # ── merchant_pipeline_leads — merchant-facing Sales Pipeline CRM ──────
        # Deliberately a SEPARATE table (and separate history table below) from
        # ambassador_leads/lead_stage_history: this tracks a merchant's own
        # customers/deals, not PhiXtra onboarding leads. Keeping them fully
        # isolated avoids any lead_id collision between the two systems.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS merchant_pipeline_leads (
                id                  SERIAL PRIMARY KEY,
                tenant_id           INTEGER NOT NULL REFERENCES tenants(id),
                customer_name       TEXT NOT NULL,
                contact_person      TEXT,
                phone               TEXT,
                email               TEXT,
                notes               TEXT,
                deal_value          NUMERIC,
                stage               VARCHAR(30) NOT NULL DEFAULT 'new_lead',
                contact_channel     TEXT,
                contact_date        DATE,
                contact_notes       TEXT,
                qualified_date      DATE,
                qualified_notes     TEXT,
                proposal_date       DATE,
                proposal_notes      TEXT,
                negotiation_notes   TEXT,
                won_date            DATE,
                dropped_at          TIMESTAMPTZ,
                dropped_reason      TEXT,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS merchant_pipeline_stage_history (
                id          SERIAL PRIMARY KEY,
                lead_id     INTEGER NOT NULL REFERENCES merchant_pipeline_leads(id) ON DELETE CASCADE,
                from_stage  VARCHAR(30),
                to_stage    VARCHAR(30) NOT NULL,
                changed_by  TEXT,
                notes       TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()

        # ── brevo_tenants — per-tenant Brevo API key + synced list ─────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brevo_tenants (
                id              SERIAL PRIMARY KEY,
                tenant_id       INTEGER NOT NULL UNIQUE REFERENCES tenants(id),
                api_key         TEXT NOT NULL,
                folder_id       INTEGER,
                list_id         INTEGER,
                list_name       TEXT,
                connected_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_synced_at  TIMESTAMPTZ,
                last_sync_count INTEGER
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
        if not _column_exists(cur, "tenants", "trial_granted_at"):
            cur.execute("ALTER TABLE tenants ADD COLUMN trial_granted_at TIMESTAMPTZ DEFAULT NULL")
        if not _column_exists(cur, "tenants", "ai_enabled"):
            # PhiXtra-admin-controlled switch: whether this tenant's WhatsApp
            # number is allowed to receive AI-generated replies at all.
            # Defaults TRUE so every existing AI-product tenant is unaffected;
            # PhiXtra Connect signups explicitly set this FALSE at registration.
            cur.execute("ALTER TABLE tenants ADD COLUMN ai_enabled BOOLEAN NOT NULL DEFAULT TRUE")
        if not _column_exists(cur, "tenants", "crm_enabled"):
            # PhiXtra-admin-controlled switch: whether this tenant's Sales
            # Pipeline (CRM) pages are unlocked on PhiXtra Connect. Defaults
            # TRUE as of 2026-09-08 — CRM ships free to every Connect
            # business by default; admin can still turn it off per business
            # if ever needed. This flag is ONLY ever checked when the
            # request is on connect.phixtra.com — it has zero effect on
            # portal.phixtra.com, where Sales Pipeline is already available
            # to every tenant regardless of plan.
            cur.execute("ALTER TABLE tenants ADD COLUMN crm_enabled BOOLEAN NOT NULL DEFAULT TRUE")

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
        # meta_message_id: lets the async delivery-status webhook (sent →
        # delivered → read, or failed with Meta's real rejection reason) match
        # back to the recipient row it belongs to — the initial send only
        # knows "accepted by Meta", not what happens to the message after.
        if not _column_exists(cur, "wa_campaign_recipients", "meta_message_id"):
            cur.execute("ALTER TABLE wa_campaign_recipients ADD COLUMN meta_message_id VARCHAR(128)")
        if not _column_exists(cur, "wa_campaign_recipients", "updated_at"):
            cur.execute("ALTER TABLE wa_campaign_recipients ADD COLUMN updated_at TIMESTAMPTZ DEFAULT NOW()")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_wcr_meta_message_id
                ON wa_campaign_recipients(meta_message_id)
        """)

        # ── email_domains: per-tenant ZeptoMail sending identity ────────────────
        # Admin-configured, not tenant self-serve: the Mail Agent + domain
        # verification (SPF/DKIM) happen manually in the ZeptoMail dashboard
        # under the platform's own ZeptoMail account (ZeptoMail's domain/agent
        # management API requires a separate OAuth2 grant, not worth building
        # for this volume yet). An admin pastes the resulting Send Mail Token
        # here per tenant after doing that setup. Token is encrypted at rest
        # with the same Fernet helper Zoho/Google Sheets use (_encrypt_ds).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_domains (
                id                  SERIAL PRIMARY KEY,
                tenant_id           INTEGER      NOT NULL UNIQUE,
                domain              VARCHAR(255) NOT NULL,
                from_email          VARCHAR(255) NOT NULL,
                from_name           VARCHAR(120),
                zeptomail_token_enc TEXT         NOT NULL,
                status              VARCHAR(20)  NOT NULL DEFAULT 'active',
                created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)

        # ── email_campaigns ───────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_campaigns (
                id             BIGSERIAL PRIMARY KEY,
                tenant_id      INTEGER      NOT NULL,
                name           VARCHAR(255) NOT NULL,
                subject        VARCHAR(255) NOT NULL,
                preheader      VARCHAR(255),
                html_body      TEXT         NOT NULL,
                status         VARCHAR(20)  NOT NULL DEFAULT 'draft',
                scheduled_at   TIMESTAMPTZ,
                segment_id     INTEGER,
                recipients     TEXT,
                total_count    INTEGER      NOT NULL DEFAULT 0,
                sent_count     INTEGER      NOT NULL DEFAULT 0,
                failed_count   INTEGER      NOT NULL DEFAULT 0,
                from_domain_id INTEGER REFERENCES email_domains(id),
                created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                completed_at   TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_campaigns_tenant
                ON email_campaigns(tenant_id)
        """)
        if not _column_exists(cur, "email_campaigns", "exclude_label_ids"):
            cur.execute("ALTER TABLE email_campaigns ADD COLUMN exclude_label_ids INTEGER[]")

        # ── email_campaign_recipients ────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_campaign_recipients (
                id          BIGSERIAL PRIMARY KEY,
                campaign_id BIGINT       NOT NULL REFERENCES email_campaigns(id) ON DELETE CASCADE,
                tenant_id   INTEGER      NOT NULL,
                email       VARCHAR(255) NOT NULL,
                status      VARCHAR(20)  NOT NULL DEFAULT 'pending',
                error_msg   TEXT,
                sent_at     TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ecr_campaign
                ON email_campaign_recipients(campaign_id)
        """)

        # ── email_suppressions: unsubscribe/bounce/complaint do-not-email list ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_suppressions (
                id         BIGSERIAL PRIMARY KEY,
                tenant_id  INTEGER      NOT NULL,
                email      VARCHAR(255) NOT NULL,
                reason     VARCHAR(20)  NOT NULL DEFAULT 'unsubscribe',
                created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                UNIQUE(tenant_id, email)
            )
        """)

        # ── email_segments / email_segment_leads: reusable named groups of Sales
        # Pipeline contacts, so a campaign can target a saved subset instead of
        # only "all pipeline contacts" or a one-off pasted list ──────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_segments (
                id         SERIAL PRIMARY KEY,
                tenant_id  INTEGER      NOT NULL,
                name       VARCHAR(120) NOT NULL,
                created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_segments_tenant
                ON email_segments(tenant_id)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_segment_leads (
                segment_id INTEGER NOT NULL REFERENCES email_segments(id) ON DELETE CASCADE,
                lead_id    INTEGER NOT NULL REFERENCES merchant_pipeline_leads(id) ON DELETE CASCADE,
                added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (segment_id, lead_id)
            )
        """)

        # ── lead_labels / lead_label_leads: freeform status tags on a Sales Pipeline
        # lead (e.g. "Bounced", "VIP"). Deliberately a separate system from
        # email_segments — a segment is an audience you'd email; a label is a fact
        # about the lead itself, and must never show up as something you can pick as
        # a campaign's send-to audience. ─────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lead_labels (
                id         SERIAL PRIMARY KEY,
                tenant_id  INTEGER      NOT NULL,
                name       VARCHAR(60)  NOT NULL,
                created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                UNIQUE(tenant_id, name)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_lead_labels_tenant
                ON lead_labels(tenant_id)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lead_label_leads (
                label_id INTEGER NOT NULL REFERENCES lead_labels(id) ON DELETE CASCADE,
                lead_id  INTEGER NOT NULL REFERENCES merchant_pipeline_leads(id) ON DELETE CASCADE,
                added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (label_id, lead_id)
            )
        """)

        # ── The Comeback Sequence: automatic onboarding win-back emails ───────────
        # onboarding_nudge_log's UNIQUE(customer_id, email_key) is the concurrency
        # guard — two gunicorn workers scanning at the same moment both try the
        # INSERT, only one wins, so a customer can never receive the same fixed
        # email twice regardless of how many workers/scans overlap.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS onboarding_nudge_log (
                id          BIGSERIAL PRIMARY KEY,
                customer_id INTEGER      NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                tenant_id   INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                email_key   VARCHAR(40)  NOT NULL,
                sent_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                UNIQUE(customer_id, email_key)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_onboarding_nudge_log_sent_at
                ON onboarding_nudge_log(sent_at DESC)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS onboarding_nudge_unsubscribes (
                id          SERIAL PRIMARY KEY,
                customer_id INTEGER      NOT NULL UNIQUE REFERENCES customers(id) ON DELETE CASCADE,
                email       VARCHAR(255) NOT NULL,
                created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS onboarding_nudge_settings (
                id      SMALLINT PRIMARY KEY DEFAULT 1,
                enabled BOOLEAN  NOT NULL DEFAULT TRUE,
                CHECK (id = 1)
            )
        """)
        cur.execute("""
            INSERT INTO onboarding_nudge_settings (id, enabled)
            VALUES (1, TRUE)
            ON CONFLICT (id) DO NOTHING
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

        # ── ambassador_audit_log: permanent login audit trail (success/fail/logout) ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ambassador_audit_log (
                id              SERIAL       PRIMARY KEY,
                ambassador_id   INTEGER      REFERENCES ambassadors(id),
                email_attempted VARCHAR(255),
                event_type      VARCHAR(20)  NOT NULL,
                failure_reason  VARCHAR(50),
                ip_address      VARCHAR(45),
                user_agent      TEXT,
                created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_amb_audit_ambassador
            ON ambassador_audit_log(ambassador_id, created_at)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_amb_audit_event
            ON ambassador_audit_log(event_type, created_at)
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
            if not _column_exists(cur, "ambassadors", "managed_product"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN managed_product VARCHAR(20)"
                )
                # Backfill (2026-07-06 redesign): infer each existing sales
                # manager's product from whichever product their own recruits
                # are enrolled in most. Ambiguous/no-recruit managers are left
                # NULL — admin must set managed_product manually before that
                # manager can recruit again.
                if _table_exists(cur, "ambassador_products"):
                    cur.execute("""
                        UPDATE ambassadors sm
                        SET managed_product = sub.product
                        FROM (
                            SELECT DISTINCT ON (a.recruited_by_id)
                                   a.recruited_by_id, ap.product
                            FROM ambassadors a
                            JOIN ambassador_products ap ON ap.ambassador_id = a.id
                            WHERE a.recruited_by_id IS NOT NULL
                            GROUP BY a.recruited_by_id, ap.product
                            ORDER BY a.recruited_by_id, COUNT(*) DESC
                        ) sub
                        WHERE sm.id = sub.recruited_by_id
                          AND sm.role = 'sales_manager'
                          AND sm.managed_product IS NULL
                    """)

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

        # ── ambassador_products: per-product membership/approval ──────────
        # Extends the ambassador program from Portal-only to Portal + School +
        # Estate. One ambassador identity/login/ref_code (unchanged), but each
        # product is approved and tiered independently via this table.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ambassador_products (
                id                 SERIAL PRIMARY KEY,
                ambassador_id      INT NOT NULL REFERENCES ambassadors(id) ON DELETE CASCADE,
                product            VARCHAR(20) NOT NULL,
                status             VARCHAR(20) NOT NULL DEFAULT 'pending',
                partnership_start  DATE,
                approved_at        TIMESTAMPTZ,
                approved_by        VARCHAR(100),
                rejected_at        TIMESTAMPTZ,
                rejected_reason    TEXT,
                terminated_at      TIMESTAMPTZ,
                terminated_reason  TEXT,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (ambassador_id, product)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_amb_products_ambassador
                ON ambassador_products(ambassador_id)
        """)

        # One-time backfill (2026-07-06): every ambassador that existed BEFORE
        # the multi-product redesign gets their legacy Portal status carried
        # over as a 'portal' row. This must never fire for ambassadors created
        # after the cutoff below — this function runs on every app startup,
        # and product enrollment now comes solely from a recruiting sales
        # manager (or an explicit admin assignment), never an automatic
        # Portal grant. Without this cutoff, restarting the app would
        # silently re-enroll every ambassador who happens to have zero
        # ambassador_products rows (including brand-new organic signups
        # awaiting admin assignment) into Portal on the next restart — a real
        # bug caught during the 2026-07-07 sales-manager-scoping redesign.
        cur.execute("""
            INSERT INTO ambassador_products
                (ambassador_id, product, status, partnership_start, approved_at, approved_by)
            SELECT id, 'portal', status, partnership_start, approved_at, approved_by
            FROM ambassadors
            WHERE created_at < '2026-07-07'::timestamptz
            ON CONFLICT (ambassador_id, product) DO NOTHING
        """)

        # ── ref_code capture on School + Estate ────────────────────────────
        # Referral tracking parity with portal `tenants.ref_code` — School and
        # Estate registration previously had no way to record who referred them.
        if not _column_exists(cur, "school_profiles", "ref_code"):
            cur.execute("ALTER TABLE school_profiles ADD COLUMN ref_code VARCHAR(30)")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_school_profiles_ref_code
                    ON school_profiles(ref_code) WHERE ref_code IS NOT NULL
            """)
        if not _column_exists(cur, "re_tenants", "ref_code"):
            cur.execute("ALTER TABLE re_tenants ADD COLUMN ref_code VARCHAR(30)")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_re_tenants_ref_code
                    ON re_tenants(ref_code) WHERE ref_code IS NOT NULL
            """)

        # ── ambassador_commissions / ambassador_leads: product-aware ──────
        # Both tables could only ever link to a merchant `tenants` row. Add
        # sibling nullable FKs so a School or Estate referral can be recorded
        # too, disambiguated by the new `product` column.
        for _tbl in ("ambassador_commissions", "ambassador_leads"):
            if not _column_exists(cur, _tbl, "product"):
                cur.execute(
                    f"ALTER TABLE {_tbl} ADD COLUMN product VARCHAR(20) NOT NULL DEFAULT 'portal'"
                )
            if not _column_exists(cur, _tbl, "school_id"):
                cur.execute(
                    f"ALTER TABLE {_tbl} ADD COLUMN school_id INT REFERENCES school_profiles(id) ON DELETE SET NULL"
                )
            if not _column_exists(cur, _tbl, "estate_tenant_id"):
                cur.execute(
                    f"ALTER TABLE {_tbl} ADD COLUMN estate_tenant_id INT REFERENCES re_tenants(id) ON DELETE SET NULL"
                )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_tbl}_school ON {_tbl}(school_id)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_tbl}_estate ON {_tbl}(estate_tenant_id)"
            )
            _check_name = f"{_tbl}_one_product_link"
            if not _constraint_exists(cur, _check_name):
                cur.execute(f"""
                    ALTER TABLE {_tbl} ADD CONSTRAINT {_check_name} CHECK (
                        (CASE WHEN tenant_id IS NOT NULL THEN 1 ELSE 0 END +
                         CASE WHEN school_id IS NOT NULL THEN 1 ELSE 0 END +
                         CASE WHEN estate_tenant_id IS NOT NULL THEN 1 ELSE 0 END) <= 1
                    )
                """)

            # Fix-up for DBs where the school_id/estate_tenant_id FKs were already
            # created without ON DELETE SET NULL (bug found 2026-07-06: deleting a
            # school/estate tenant with any commission/lead history raised a FK
            # violation — e.g. blocked Estate's self-serve "delete my account").
            for _fk_col, _fk_table in (("school_id", "school_profiles"), ("estate_tenant_id", "re_tenants")):
                cur.execute("""
                    SELECT confdeltype FROM pg_constraint
                    WHERE conrelid = %s::regclass AND conname = %s
                """, (_tbl, f"{_tbl}_{_fk_col}_fkey"))
                _row = cur.fetchone()
                if _row and _row[0] != 'n':  # 'n' = ON DELETE SET NULL
                    cur.execute(f"ALTER TABLE {_tbl} DROP CONSTRAINT {_tbl}_{_fk_col}_fkey")
                    cur.execute(f"""
                        ALTER TABLE {_tbl} ADD CONSTRAINT {_tbl}_{_fk_col}_fkey
                            FOREIGN KEY ({_fk_col}) REFERENCES {_fk_table}(id) ON DELETE SET NULL
                    """)

        # ── ambassador_documents: admin-shared files (PDF/Word/Excel/PPT) ──
        # Admin uploads a document from /admin and tags it with which
        # product(s) (portal/school/estate) it applies to. Ambassadors see
        # it on their own /ambassador/documents page, gated by which
        # products they are 'active' on in ambassador_products.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ambassador_documents (
                id                 SERIAL PRIMARY KEY,
                title              VARCHAR(255) NOT NULL,
                description        TEXT,
                original_filename  VARCHAR(255) NOT NULL,
                stored_filename    VARCHAR(255) NOT NULL,
                file_ext           VARCHAR(10) NOT NULL,
                file_size_bytes    BIGINT,
                products           TEXT[] NOT NULL,
                uploaded_by        VARCHAR(255),
                created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ambassador_documents_products
                ON ambassador_documents USING GIN(products)
        """)

        # ── social_media_posts: internal content queue for the social media
        # team. Admin (owner) creates a post with an image + caption tagged
        # for one or more platforms; the social media executive's scoped
        # login sees it on /admin/social-media and marks it posted once
        # she's actually published it manually on each platform.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS social_media_posts (
                id                 SERIAL PRIMARY KEY,
                caption            TEXT NOT NULL,
                image_filename     VARCHAR(255) NOT NULL,
                original_filename  VARCHAR(255),
                platforms          TEXT[] NOT NULL,
                status             VARCHAR(20) NOT NULL DEFAULT 'ready',
                created_by         VARCHAR(255),
                posted_by          VARCHAR(255),
                created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                posted_at          TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_social_media_posts_status
                ON social_media_posts(status)
        """)

        # ── Buffer publishing fields on social_media_posts — added when the
        # Canva → PhiXtra → Buffer flow was wired up. scheduled_for/buffer_*
        # track a post sent to Buffer; public_token lets the unauthenticated
        # image route (Buffer must fetch the image itself, no API upload
        # exists) serve the file without exposing the admin-gated route.
        if not _column_exists(cur, "social_media_posts", "scheduled_for"):
            cur.execute("ALTER TABLE social_media_posts ADD COLUMN scheduled_for TIMESTAMPTZ")
        if not _column_exists(cur, "social_media_posts", "buffer_status"):
            cur.execute("ALTER TABLE social_media_posts ADD COLUMN buffer_status VARCHAR(20)")
        if not _column_exists(cur, "social_media_posts", "buffer_post_ids"):
            cur.execute("ALTER TABLE social_media_posts ADD COLUMN buffer_post_ids JSONB")
        if not _column_exists(cur, "social_media_posts", "buffer_error"):
            cur.execute("ALTER TABLE social_media_posts ADD COLUMN buffer_error TEXT")
        if not _column_exists(cur, "social_media_posts", "is_urgent"):
            cur.execute("ALTER TABLE social_media_posts ADD COLUMN is_urgent BOOLEAN NOT NULL DEFAULT FALSE")
        if not _column_exists(cur, "social_media_posts", "public_token"):
            cur.execute("ALTER TABLE social_media_posts ADD COLUMN public_token VARCHAR(64) UNIQUE")

        # ── buffer_channel_map: one-time admin mapping of each Social Media
        # Posts platform key (facebook/instagram/linkedin/tiktok/x) to the
        # PhiXtra Buffer account's channel id for that platform.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS buffer_channel_map (
                platform          VARCHAR(20) PRIMARY KEY,
                buffer_channel_id VARCHAR(64) NOT NULL,
                channel_label     VARCHAR(255),
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by        VARCHAR(255)
            )
        """)

        # ── ambassador_broadcasts: admin WhatsApp broadcasts to ambassadors ──
        # Log of each admin-sent WhatsApp update (via one reusable Meta
        # template) — who it targeted, recipient ids, and delivery counts.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ambassador_broadcasts (
                id             SERIAL PRIMARY KEY,
                message_body   TEXT NOT NULL,
                target_product VARCHAR(20) NOT NULL,
                recipient_ids  JSONB NOT NULL,
                total_count    INT NOT NULL DEFAULT 0,
                sent_count     INT NOT NULL DEFAULT 0,
                failed_count   INT NOT NULL DEFAULT 0,
                skipped_count  INT NOT NULL DEFAULT 0,
                status         VARCHAR(20) NOT NULL DEFAULT 'sending',
                created_by     VARCHAR(255),
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                sent_at        TIMESTAMPTZ
            )
        """)
        if not _column_exists(cur, "ambassador_broadcasts", "failed_recipients"):
            cur.execute("""
                ALTER TABLE ambassador_broadcasts
                ADD COLUMN failed_recipients JSONB NOT NULL DEFAULT '[]'::jsonb
            """)

        # ── Visual Product Match: Pro-plan feature gate ─────────────────────
        # Mirrors feat_crm/feat_advanced_ai — a plan-level capability flag.
        # Only 'pro' gets TRUE; per-tenant opt-in still lives in
        # tenants.features JSON (visual_product_match key), same pattern as
        # product_recommendation/related_products.
        if not _column_exists(cur, "plans", "feat_visual_match"):
            cur.execute("""
                ALTER TABLE plans
                ADD COLUMN feat_visual_match BOOLEAN NOT NULL DEFAULT FALSE
            """)
            cur.execute("UPDATE plans SET feat_visual_match = TRUE WHERE slug = 'pro'")

        # ── Email Campaigns: Pro-plan-only feature gate ─────────────────────
        # Unlike feat_broadcasts (Starter+), this is Pro-only — native ZeptoMail
        # bulk email has a real per-message send cost, so it's reserved for the
        # top tier rather than opened up during a "product discovery" period.
        # Gating always applies regardless of WhatsApp connection status (see
        # _require_email_campaigns_plan in portal_routes.py) — unlike
        # _require_plan_feature, there is no web-only-tenant bypass here.
        if not _column_exists(cur, "plans", "feat_email_campaigns"):
            cur.execute("""
                ALTER TABLE plans
                ADD COLUMN feat_email_campaigns BOOLEAN NOT NULL DEFAULT FALSE
            """)
            cur.execute("UPDATE plans SET feat_email_campaigns = TRUE WHERE slug = 'pro'")

        # ── Visual Product Match: image embedding column on documents ──────
        # Additive, nullable — existing text `embedding` column and all
        # search.py queries are untouched. Populated by ai-backend/image_search.py
        # (sync-on-write) and a one-off backfill for existing rows.
        if not _column_exists(cur, "documents", "image_embedding"):
            cur.execute("ALTER TABLE documents ADD COLUMN image_embedding vector(512)")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_documents_image_embedding ON documents
                    USING hnsw (image_embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64)
            """)

        # ── feature_releases: Ambassador "What's New" board ─────────────────
        # Lets admins publish new sellable capabilities (e.g. Visual Product
        # Match) as sales-enablement cards ambassadors/sales managers see in
        # their Hub. Publishing optionally fires a WhatsApp broadcast reusing
        # the existing ambassador_broadcasts send path (send_ambassador_wa_template).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feature_releases (
                id                 SERIAL PRIMARY KEY,
                title              VARCHAR(200) NOT NULL,
                summary            TEXT NOT NULL,
                pitch_notes        TEXT,
                demo_instructions  TEXT,
                playbook_note      VARCHAR(255),
                product            VARCHAR(20) NOT NULL DEFAULT 'all',
                min_plan           VARCHAR(40) NOT NULL DEFAULT 'All plans',
                status             VARCHAR(20) NOT NULL DEFAULT 'draft',
                notify_whatsapp    BOOLEAN NOT NULL DEFAULT TRUE,
                broadcast_id       INT REFERENCES ambassador_broadcasts(id) ON DELETE SET NULL,
                created_by         VARCHAR(255),
                published_at       TIMESTAMPTZ,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── Ambassador password reset ─────────────────────────────────────────
        if _table_exists(cur, "ambassadors"):
            if not _column_exists(cur, "ambassadors", "reset_token"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN reset_token VARCHAR(64) UNIQUE"
                )
            if not _column_exists(cur, "ambassadors", "reset_expires_at"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN reset_expires_at TIMESTAMP"
                )

        # ── Ambassador inactivity policy (day 3 reminder / day 7 deactivate /
        # day 30 soft-delete) ─────────────────────────────────────────────────
        if _table_exists(cur, "ambassadors"):
            if not _column_exists(cur, "ambassadors", "last_login_at"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN last_login_at TIMESTAMPTZ"
                )
            if not _column_exists(cur, "ambassadors", "inactivity_reminder_sent_at"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN inactivity_reminder_sent_at TIMESTAMPTZ"
                )
            if not _column_exists(cur, "ambassadors", "suspended_at"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN suspended_at TIMESTAMPTZ"
                )
            if not _column_exists(cur, "ambassadors", "suspended_reason"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN suspended_reason VARCHAR(50)"
                )
            if not _column_exists(cur, "ambassadors", "deleted_at"):
                cur.execute(
                    "ALTER TABLE ambassadors ADD COLUMN deleted_at TIMESTAMPTZ"
                )

        # ── Sales Pipeline → Brevo sync UX: per-lead sync tracking ─────────
        # updated_at lets sync tell "changed since last push" apart from
        # "never changed" so the default sync can skip already-current leads.
        if _table_exists(cur, "merchant_pipeline_leads"):
            if not _column_exists(cur, "merchant_pipeline_leads", "updated_at"):
                cur.execute(
                    "ALTER TABLE merchant_pipeline_leads ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
                )
            if not _column_exists(cur, "merchant_pipeline_leads", "brevo_synced_at"):
                cur.execute(
                    "ALTER TABLE merchant_pipeline_leads ADD COLUMN brevo_synced_at TIMESTAMPTZ"
                )

        # ── Sales Pipeline → Brevo sync UX: background sync progress ───────
        # Large syncs (hundreds+ of leads) run in a background thread rather
        # than blocking the request, since one HTTP call per lead to Brevo
        # can take minutes — these columns let the poll endpoint report
        # progress from any gunicorn worker, not just the one running the sync.
        if _table_exists(cur, "brevo_tenants"):
            if not _column_exists(cur, "brevo_tenants", "sync_status"):
                cur.execute(
                    "ALTER TABLE brevo_tenants ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'idle'"
                )
            if not _column_exists(cur, "brevo_tenants", "sync_total"):
                cur.execute(
                    "ALTER TABLE brevo_tenants ADD COLUMN sync_total INTEGER NOT NULL DEFAULT 0"
                )
            if not _column_exists(cur, "brevo_tenants", "sync_progress"):
                cur.execute(
                    "ALTER TABLE brevo_tenants ADD COLUMN sync_progress INTEGER NOT NULL DEFAULT 0"
                )
            if not _column_exists(cur, "brevo_tenants", "sync_started_at"):
                cur.execute(
                    "ALTER TABLE brevo_tenants ADD COLUMN sync_started_at TIMESTAMPTZ"
                )
            if not _column_exists(cur, "brevo_tenants", "sync_skipped"):
                cur.execute(
                    "ALTER TABLE brevo_tenants ADD COLUMN sync_skipped INTEGER NOT NULL DEFAULT 0"
                )
            if not _column_exists(cur, "brevo_tenants", "sync_synced_ids"):
                cur.execute(
                    "ALTER TABLE brevo_tenants ADD COLUMN sync_synced_ids TEXT"
                )
            if not _column_exists(cur, "brevo_tenants", "sync_failed_json"):
                cur.execute(
                    "ALTER TABLE brevo_tenants ADD COLUMN sync_failed_json TEXT"
                )
            if not _column_exists(cur, "brevo_tenants", "sync_error"):
                cur.execute(
                    "ALTER TABLE brevo_tenants ADD COLUMN sync_error TEXT"
                )

        # ── zoho_campaigns_tenants — per-tenant Zoho Campaigns OAuth + synced
        # list. Unlike Brevo (static API key), Zoho requires OAuth2: each
        # tenant authorizes their own Zoho account, and we store their
        # refresh token (encrypted) plus the accounts-server host Zoho
        # returned for their data center (US/EU/IN/etc — refresh calls must
        # go back to that same DC, never a hardcoded accounts.zoho.com).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS zoho_campaigns_tenants (
                id                SERIAL PRIMARY KEY,
                tenant_id         INTEGER NOT NULL UNIQUE REFERENCES tenants(id),
                refresh_token_enc TEXT NOT NULL,
                accounts_server   TEXT NOT NULL DEFAULT 'https://accounts.zoho.com',
                list_key          TEXT,
                list_name         TEXT,
                connected_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_synced_at    TIMESTAMPTZ,
                last_sync_count   INTEGER,
                sync_status       TEXT NOT NULL DEFAULT 'idle',
                sync_total        INTEGER NOT NULL DEFAULT 0,
                sync_progress     INTEGER NOT NULL DEFAULT 0,
                sync_started_at   TIMESTAMPTZ,
                sync_skipped      INTEGER NOT NULL DEFAULT 0,
                sync_synced_ids   TEXT,
                sync_failed_json  TEXT,
                sync_error        TEXT
            )
        """)

        if _table_exists(cur, "merchant_pipeline_leads"):
            if not _column_exists(cur, "merchant_pipeline_leads", "zoho_synced_at"):
                cur.execute(
                    "ALTER TABLE merchant_pipeline_leads ADD COLUMN zoho_synced_at TIMESTAMPTZ"
                )
            # website — a proper field of its own (rendered as a clickable link
            # in the pipeline table) rather than a URL buried in free-text notes.
            if not _column_exists(cur, "merchant_pipeline_leads", "website"):
                cur.execute(
                    "ALTER TABLE merchant_pipeline_leads ADD COLUMN website TEXT"
                )

        # ── wa_contacts: personalization phrase for outbound sales campaigns ──
        if _table_exists(cur, "wa_contacts"):
            if not _column_exists(cur, "wa_contacts", "personalization_note"):
                cur.execute(
                    "ALTER TABLE wa_contacts ADD COLUMN personalization_note TEXT"
                )
            # STOP opt-out: set by the WhatsApp gateway when a customer replies
            # STOP/UNSUBSCRIBE/etc; checked by the portal's campaign sender so
            # opted-out contacts are never sent a marketing template again.
            if not _column_exists(cur, "wa_contacts", "opted_out"):
                cur.execute(
                    "ALTER TABLE wa_contacts ADD COLUMN opted_out BOOLEAN NOT NULL DEFAULT FALSE"
                )
            if not _column_exists(cur, "wa_contacts", "opted_out_at"):
                cur.execute(
                    "ALTER TABLE wa_contacts ADD COLUMN opted_out_at TIMESTAMPTZ"
                )

        # ── wa_history_imports — tracks each chat-history upload batch, so an
        # import can be listed and undone as a unit (delete by batch id) ──────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_history_imports (
                id              SERIAL PRIMARY KEY,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
                wa_tenant_id    INTEGER REFERENCES wa_tenants(id),
                customer_phone  VARCHAR(32) NOT NULL,
                customer_label  TEXT,
                source_filename TEXT,
                message_count   INTEGER NOT NULL DEFAULT 0,
                skipped_media   INTEGER NOT NULL DEFAULT 0,
                skipped_system  INTEGER NOT NULL DEFAULT 0,
                imported_by     TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── wa_message_log: flag imported rows so they never get counted in
        # live dashboard stats (today/month/active-conversation aggregates),
        # and can be traced back to / deleted with their import batch ────────
        if not _column_exists(cur, "wa_message_log", "is_historical"):
            cur.execute(
                "ALTER TABLE wa_message_log ADD COLUMN is_historical BOOLEAN NOT NULL DEFAULT FALSE"
            )
        if not _column_exists(cur, "wa_message_log", "import_batch_id"):
            cur.execute(
                "ALTER TABLE wa_message_log ADD COLUMN import_batch_id INTEGER "
                "REFERENCES wa_history_imports(id) ON DELETE CASCADE"
            )
        # media_url: populated only by history-import media extraction (a
        # "With Media" .zip export) — live Meta webhook messages never set
        # this, since Meta's inbound media is a short-lived ID, not a
        # permanent URL; content there stays caption-only as before.
        if not _column_exists(cur, "wa_message_log", "media_url"):
            cur.execute("ALTER TABLE wa_message_log ADD COLUMN media_url TEXT")

        if not _column_exists(cur, "wa_history_imports", "media_extracted"):
            cur.execute(
                "ALTER TABLE wa_history_imports ADD COLUMN media_extracted INTEGER NOT NULL DEFAULT 0"
            )

        # ── wa_campaigns: which connected number a campaign sends from ──────
        # Nullable — existing campaigns predate this column. _send_campaign_now
        # falls back to the tenant's oldest active connection (deterministic,
        # matching the old accidental behavior) when it's NULL.
        if not _column_exists(cur, "wa_campaigns", "wa_tenant_id"):
            cur.execute(
                "ALTER TABLE wa_campaigns ADD COLUMN wa_tenant_id INTEGER REFERENCES wa_tenants(id)"
            )

        # ── customers.hear_about_us: "How did you hear about us?" — captured at
        # registration for marketing-channel attribution. Nullable since existing
        # customers registered before this field existed; new signups are required
        # to answer it (enforced in the /register route, not the DB).
        if not _column_exists(cur, "customers", "hear_about_us"):
            cur.execute("ALTER TABLE customers ADD COLUMN hear_about_us VARCHAR(30)")

        # ── team_members: Shared Team Inbox — staff logins scoped to a tenant ──
        # Single flat 'agent' role for v1 (Inbox-only, enforced by a
        # before_request allowlist in portal_routes.py). The owner's login
        # stays the `customers` row; team members are additional logins that
        # share the same tenant_id.
        if not _table_exists(cur, "team_members"):
            cur.execute("""
                CREATE TABLE team_members (
                    id                SERIAL PRIMARY KEY,
                    tenant_id         INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name              VARCHAR(200) NOT NULL,
                    email             VARCHAR(255) NOT NULL UNIQUE,
                    password_hash     VARCHAR(255),
                    role              VARCHAR(30) NOT NULL DEFAULT 'agent',
                    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
                    invite_token      VARCHAR(64),
                    invite_expires_at TIMESTAMPTZ,
                    invited_by        INTEGER REFERENCES customers(id),
                    last_login_at     TIMESTAMPTZ,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

        # ── team_member_agents: which AI Agent(s) a team member may see ────────
        # A team member with ZERO rows here sees NOTHING in the Inbox — this
        # is a deny-by-default access-control table, not a soft label like
        # wa_conversation_assignments above. Real FK on purpose (unlike the
        # assignment table) since this genuinely gates data access.
        if not _table_exists(cur, "team_member_agents"):
            cur.execute("""
                CREATE TABLE team_member_agents (
                    team_member_id INTEGER NOT NULL REFERENCES team_members(id) ON DELETE CASCADE,
                    tenant_agent_id INTEGER NOT NULL REFERENCES tenant_agents(id) ON DELETE CASCADE,
                    PRIMARY KEY (team_member_id, tenant_agent_id)
                )
            """)

        # ── wa_conversation_assignments: "who's handling this chat" ────────────
        # assigned_to_key/label are denormalized strings ("owner:<id>" /
        # "team:<id>") rather than a polymorphic FK, since the owner and team
        # members live in two different tables and this is a soft
        # coordination record, not an access-control one.
        if not _table_exists(cur, "wa_conversation_assignments"):
            cur.execute("""
                CREATE TABLE wa_conversation_assignments (
                    id               SERIAL PRIMARY KEY,
                    tenant_id        INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    customer_phone   VARCHAR(40) NOT NULL,
                    assigned_to_key  VARCHAR(60) NOT NULL,
                    assigned_to_label VARCHAR(200) NOT NULL,
                    assigned_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (tenant_id, customer_phone)
                )
            """)

        # ── plans.staff_limit: extra team seats beyond the owner ───────────────
        # Defaults to 0 (COALESCE at read time) — a tenant on a plan with no
        # staff_limit set does not silently get a free team seat.
        if not _column_exists(cur, "plans", "staff_limit"):
            cur.execute("ALTER TABLE plans ADD COLUMN staff_limit INTEGER")
            cur.execute("UPDATE plans SET staff_limit = 0  WHERE slug = 'free'")
            cur.execute("UPDATE plans SET staff_limit = 1  WHERE slug = 'starter'")
            cur.execute("UPDATE plans SET staff_limit = 3  WHERE slug = 'growth'")
            cur.execute("UPDATE plans SET staff_limit = 10 WHERE slug = 'pro'")

        # ── wa_message_log.sent_by_label: which human sent a manual reply ──────
        # Set only on outbound 'agent_reply' rows sent via the Inbox, so the
        # chat bubble can show the actual staff member's name instead of a
        # generic "You" once more than one human can reply on an account.
        if not _column_exists(cur, "wa_message_log", "sent_by_label"):
            cur.execute("ALTER TABLE wa_message_log ADD COLUMN sent_by_label VARCHAR(200)")

        # ── sms_campaigns: Sales Pipeline bulk SMS (support@phixtra.com only) ──
        # Sends via the single shared BulkSMSNigeria account (see bulksmsng_api.py;
        # was eBulkSMS until the 2026-08-06 switch).
        # Recipients are resolved once at send time (Sales Pipeline selection
        # and/or an uploaded CSV/Excel list, merged and de-duplicated) and
        # stored as a newline-joined snapshot, same shape as email_campaigns.
        if not _table_exists(cur, "sms_campaigns"):
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sms_campaigns (
                    id           BIGSERIAL PRIMARY KEY,
                    tenant_id    INTEGER      NOT NULL,
                    message      TEXT         NOT NULL,
                    recipients   TEXT,
                    total_count  INTEGER      NOT NULL DEFAULT 0,
                    sent_count   INTEGER      NOT NULL DEFAULT 0,
                    failed_count INTEGER      NOT NULL DEFAULT 0,
                    status       VARCHAR(20)  NOT NULL DEFAULT 'sending',
                    error        TEXT,
                    created_by   VARCHAR(200),
                    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    completed_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sms_campaigns_tenant
                    ON sms_campaigns(tenant_id)
            """)
        if not _column_exists(cur, "sms_campaigns", "sender"):
            cur.execute("ALTER TABLE sms_campaigns ADD COLUMN sender VARCHAR(20)")

        # ── sms_pipeline_segments / sms_pipeline_segment_leads: reusable named
        # groups of Sales Pipeline contacts for the SMS Campaign tool, mirroring
        # wa_pipeline_segments/wa_pipeline_segment_leads exactly (same shape).
        # SMS Campaign is support@phixtra.com-only, but these tables carry a
        # tenant_id like the others in case that ever changes.
        if not _table_exists(cur, "sms_pipeline_segments"):
            cur.execute("""
                CREATE TABLE sms_pipeline_segments (
                    id         SERIAL PRIMARY KEY,
                    tenant_id  INTEGER      NOT NULL,
                    name       VARCHAR(120) NOT NULL,
                    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sms_pipeline_segments_tenant
                    ON sms_pipeline_segments(tenant_id)
            """)
        if not _table_exists(cur, "sms_pipeline_segment_leads"):
            cur.execute("""
                CREATE TABLE sms_pipeline_segment_leads (
                    segment_id INTEGER NOT NULL REFERENCES sms_pipeline_segments(id) ON DELETE CASCADE,
                    lead_id    INTEGER NOT NULL REFERENCES merchant_pipeline_leads(id) ON DELETE CASCADE,
                    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (segment_id, lead_id)
                )
            """)

        # ── wa_pipeline_segments / wa_pipeline_segment_leads: reusable named
        # groups of Sales Pipeline contacts for WhatsApp Campaign, mirroring
        # email_segments/email_segment_leads exactly (same shape, phone instead
        # of email). Deliberately separate from the older wa_segments/
        # wa_segment_members pair, which groups wa_contacts (WhatsApp Contacts
        # page) — a different, unrelated contact table not tied to the CRM.
        if not _table_exists(cur, "wa_pipeline_segments"):
            cur.execute("""
                CREATE TABLE wa_pipeline_segments (
                    id         SERIAL PRIMARY KEY,
                    tenant_id  INTEGER      NOT NULL,
                    name       VARCHAR(120) NOT NULL,
                    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_wa_pipeline_segments_tenant
                    ON wa_pipeline_segments(tenant_id)
            """)
        if not _table_exists(cur, "wa_pipeline_segment_leads"):
            cur.execute("""
                CREATE TABLE wa_pipeline_segment_leads (
                    segment_id INTEGER NOT NULL REFERENCES wa_pipeline_segments(id) ON DELETE CASCADE,
                    lead_id    INTEGER NOT NULL REFERENCES merchant_pipeline_leads(id) ON DELETE CASCADE,
                    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (segment_id, lead_id)
                )
            """)

        # ── wa_campaigns.pipeline_segment_id: which WhatsApp Segment (if any)
        # a campaign was sent to. Separate column from the older segment_id
        # (wa_segments), left untouched for backward compatibility with
        # campaigns created before this feature existed.
        if not _column_exists(cur, "wa_campaigns", "pipeline_segment_id"):
            cur.execute(
                "ALTER TABLE wa_campaigns ADD COLUMN pipeline_segment_id "
                "INTEGER REFERENCES wa_pipeline_segments(id) ON DELETE SET NULL"
            )

        # ══════════════════════════════════════════════════════════════════
        # CRM merge (2026-09-09) — one "CRM" contact record instead of two
        # disconnected ones (WhatsApp Contacts vs Sales Pipeline leads).
        # Purely additive: no existing table/column is touched or dropped.
        #   - crm_companies: new, optional "company" a contact can belong to.
        #   - wa_contacts.company_id / merchant_pipeline_leads.company_id:
        #     link each side to the same company.
        #   - merchant_pipeline_leads.wa_contact_id: the canonical link from a
        #     deal to the WhatsApp Contact it belongs to (wa_contacts stays
        #     the "person" record — it already has the note log, tags,
        #     segments, message history the merged profile is built on).
        #   - crm_match_candidates: near-matches the automatic phone-number
        #     matching wasn't sure about, held here for a human to confirm —
        #     see crm_merge_backfill.py, run once by hand after this deploys.
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "crm_companies"):
            cur.execute("""
                CREATE TABLE crm_companies (
                    id         SERIAL PRIMARY KEY,
                    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name       TEXT NOT NULL,
                    website    TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX idx_crm_companies_tenant ON crm_companies(tenant_id)")

        if not _column_exists(cur, "wa_contacts", "company_id"):
            cur.execute(
                "ALTER TABLE wa_contacts ADD COLUMN company_id "
                "INTEGER REFERENCES crm_companies(id) ON DELETE SET NULL"
            )
            cur.execute("CREATE INDEX idx_wa_contacts_company ON wa_contacts(company_id)")

        if not _column_exists(cur, "merchant_pipeline_leads", "company_id"):
            cur.execute(
                "ALTER TABLE merchant_pipeline_leads ADD COLUMN company_id "
                "INTEGER REFERENCES crm_companies(id) ON DELETE SET NULL"
            )
            cur.execute("CREATE INDEX idx_mpl_company ON merchant_pipeline_leads(company_id)")

        if not _column_exists(cur, "merchant_pipeline_leads", "wa_contact_id"):
            cur.execute(
                "ALTER TABLE merchant_pipeline_leads ADD COLUMN wa_contact_id "
                "INTEGER REFERENCES wa_contacts(id) ON DELETE SET NULL"
            )
            cur.execute("CREATE INDEX idx_mpl_wa_contact ON merchant_pipeline_leads(wa_contact_id)")

        if not _table_exists(cur, "crm_company_notes"):
            cur.execute("""
                CREATE TABLE crm_company_notes (
                    id         SERIAL PRIMARY KEY,
                    company_id INTEGER NOT NULL REFERENCES crm_companies(id) ON DELETE CASCADE,
                    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    author_id  INTEGER,
                    body       TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX idx_crm_company_notes_company ON crm_company_notes(company_id)")

        if not _table_exists(cur, "crm_match_candidates"):
            cur.execute("""
                CREATE TABLE crm_match_candidates (
                    id               SERIAL PRIMARY KEY,
                    tenant_id        INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    wa_contact_id    INTEGER REFERENCES wa_contacts(id) ON DELETE CASCADE,
                    pipeline_lead_id INTEGER REFERENCES merchant_pipeline_leads(id) ON DELETE CASCADE,
                    reason           TEXT,
                    status           VARCHAR(20) NOT NULL DEFAULT 'pending',
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    resolved_at      TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE INDEX idx_crm_match_candidates_tenant
                    ON crm_match_candidates(tenant_id) WHERE status = 'pending'
            """)

        # ══════════════════════════════════════════════════════════════════
        # Tags unification (2026-09-09) — one shared tag vocabulary for both
        # WhatsApp Contacts and Sales Pipeline deals, instead of two separate
        # systems (wa_contacts.tags free-text array vs lead_labels/
        # lead_label_leads). lead_labels stays the tag-definition table (it
        # already has real, actively-used data — Bounced/High Lead/HOT
        # LEADS — and campaigns' bounce-suppression exclude-picker reads it;
        # untouched, no rename, to avoid re-testing that path). This just
        # adds the missing other half: a Contact-to-label link table,
        # mirroring lead_label_leads. wa_contacts.tags is left in place,
        # frozen/unread going forward — no data dropped, see
        # lead_tags_unify_backfill.py for the one-time carry-over.
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "lead_label_contacts"):
            cur.execute("""
                CREATE TABLE lead_label_contacts (
                    label_id   INTEGER NOT NULL REFERENCES lead_labels(id) ON DELETE CASCADE,
                    contact_id INTEGER NOT NULL REFERENCES wa_contacts(id) ON DELETE CASCADE,
                    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (label_id, contact_id)
                )
            """)
            cur.execute("CREATE INDEX idx_lead_label_contacts_contact ON lead_label_contacts(contact_id)")

        # ══════════════════════════════════════════════════════════════════
        # Saved filter views (2026-09-09) — the Contacts filter panel's
        # "Save this filter as a view" action. Tenant-wide (any staff login
        # sees and can use every saved view), not per-user — matches how
        # Segments/Tags/Labels already work here. `filters` stores just the
        # filter fields (status_filter/tag_filter/segment_filter/date_from/
        # date_to/has_phone/has_email/has_pers) — never the free-text search
        # box, so a view is a reusable filter combo, not a one-off search.
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "contact_filter_views"):
            cur.execute("""
                CREATE TABLE contact_filter_views (
                    id         SERIAL PRIMARY KEY,
                    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name       TEXT NOT NULL,
                    filters    JSONB NOT NULL DEFAULT '{}',
                    created_by INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX idx_contact_filter_views_tenant ON contact_filter_views(tenant_id)")

        # ══════════════════════════════════════════════════════════════════
        # Custom Report Builder saved reports (2026-09-10, Phase 3) — same
        # pattern as contact_filter_views above, extended to cover a whole
        # report definition (entity + columns + filters + grouping), not
        # just a filter set. Tenant-wide, same reasoning as Segments/Tags/
        # contact_filter_views.
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "custom_report_views"):
            cur.execute("""
                CREATE TABLE custom_report_views (
                    id         SERIAL PRIMARY KEY,
                    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    entity     TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    config     JSONB NOT NULL DEFAULT '{}',
                    created_by INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX idx_custom_report_views_tenant ON custom_report_views(tenant_id, entity)")

        # ══════════════════════════════════════════════════════════════════
        # Contact Person on All Contacts (2026-09-09) — mirrors the field
        # Sales Pipeline deals already have. Needed because a Contact's
        # "Display Name" is often the BUSINESS name in practice (real data
        # confirmed this — e.g. "Ojasweb Digital Academy"), same as a deal's
        # "Customer Name" — so there was nowhere to record the actual human
        # you deal with there, same gap Sales Pipeline already solved.
        # ══════════════════════════════════════════════════════════════════
        if not _column_exists(cur, "wa_contacts", "contact_person"):
            cur.execute("ALTER TABLE wa_contacts ADD COLUMN contact_person TEXT")

        # ══════════════════════════════════════════════════════════════════
        # Unified consent (2026-09-09) — today "opted out" only ever meant
        # WhatsApp (wa_contacts.opted_out); Email had its own, separate
        # suppression list (email_suppressions, keyed by email) and SMS had
        # NO opt-out at all. wa_contacts is the one real contact identity
        # here (see the crm_companies/company_id block above), so it also
        # becomes the one place a channel-specific opt-out is recorded —
        # email_opted_out / sms_opted_out sit next to the existing
        # opted_out (left untouched: the WhatsApp gateway service and the
        # campaign-send opted_out check both already depend on its exact
        # name and meaning). email_suppressions stays the source of truth
        # the email send path checks — a cross-channel opt-out writes INTO
        # it rather than replacing it, so that already-live check needs no
        # change. contact_consent_log is the audit trail: every opt-out/
        # opt-in, on any channel, from any source (a WhatsApp STOP reply,
        # an email unsubscribe click, or a staff member toggling it by
        # hand), gets one row.
        # ══════════════════════════════════════════════════════════════════
        if not _column_exists(cur, "wa_contacts", "email_opted_out"):
            cur.execute("ALTER TABLE wa_contacts ADD COLUMN email_opted_out BOOLEAN NOT NULL DEFAULT FALSE")
        if not _column_exists(cur, "wa_contacts", "email_opted_out_at"):
            cur.execute("ALTER TABLE wa_contacts ADD COLUMN email_opted_out_at TIMESTAMPTZ")
        if not _column_exists(cur, "wa_contacts", "sms_opted_out"):
            cur.execute("ALTER TABLE wa_contacts ADD COLUMN sms_opted_out BOOLEAN NOT NULL DEFAULT FALSE")
        if not _column_exists(cur, "wa_contacts", "sms_opted_out_at"):
            cur.execute("ALTER TABLE wa_contacts ADD COLUMN sms_opted_out_at TIMESTAMPTZ")

        if not _table_exists(cur, "contact_consent_log"):
            cur.execute("""
                CREATE TABLE contact_consent_log (
                    id         SERIAL PRIMARY KEY,
                    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    contact_id INTEGER NOT NULL REFERENCES wa_contacts(id) ON DELETE CASCADE,
                    channel    VARCHAR(20) NOT NULL,  -- 'whatsapp' | 'email' | 'sms' | 'all'
                    action     VARCHAR(20) NOT NULL,  -- 'opted_out' | 'opted_in'
                    reason     TEXT,
                    source     VARCHAR(30) NOT NULL,  -- 'whatsapp_reply' | 'email_unsubscribe' | 'manual_staff' | 'bounce'
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX idx_consent_log_contact ON contact_consent_log(contact_id, created_at DESC)")

        # ══════════════════════════════════════════════════════════════════
        # WhatsApp Campaign Intelligence (2026-09-09) — extends the existing
        # Sent/Delivered/Read/Failed campaign tracking with what happens
        # AFTER delivery: Replied, Interested, Not interested, Opportunity,
        # Converted. A reply is always classified and flagged automatically
        # (wa_campaign_recipients.reply_text/replied_at, status moved to
        # 'replied'/'interested'/'not_interested') — that part is never
        # gated. Only the follow-on ACTION (auto-creating a Sales Pipeline
        # opportunity from an "interested" reply) is gated by
        # tenants.campaign_reply_auto_actions: TRUE (default) creates the
        # opportunity immediately; FALSE queues it in
        # wa_campaign_reply_reviews for a staff member to approve/reject
        # first. See project_wa_campaign_intelligence_proposal memory.
        # ══════════════════════════════════════════════════════════════════
        if not _column_exists(cur, "tenants", "campaign_reply_auto_actions"):
            cur.execute("ALTER TABLE tenants ADD COLUMN campaign_reply_auto_actions BOOLEAN NOT NULL DEFAULT TRUE")

        if not _column_exists(cur, "wa_campaign_recipients", "reply_text"):
            cur.execute("ALTER TABLE wa_campaign_recipients ADD COLUMN reply_text TEXT")
        if not _column_exists(cur, "wa_campaign_recipients", "replied_at"):
            cur.execute("ALTER TABLE wa_campaign_recipients ADD COLUMN replied_at TIMESTAMPTZ")
        if not _column_exists(cur, "wa_campaign_recipients", "pipeline_lead_id"):
            cur.execute(
                "ALTER TABLE wa_campaign_recipients ADD COLUMN pipeline_lead_id "
                "INTEGER REFERENCES merchant_pipeline_leads(id) ON DELETE SET NULL"
            )

        if not _table_exists(cur, "wa_campaign_reply_reviews"):
            cur.execute("""
                CREATE TABLE wa_campaign_reply_reviews (
                    id            SERIAL PRIMARY KEY,
                    tenant_id     INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    campaign_id   BIGINT REFERENCES wa_campaigns(id) ON DELETE CASCADE,
                    recipient_id  BIGINT NOT NULL REFERENCES wa_campaign_recipients(id) ON DELETE CASCADE,
                    phone         VARCHAR(30) NOT NULL,
                    reply_text    TEXT,
                    sentiment     VARCHAR(20) NOT NULL,   -- currently only 'interested'
                    confidence    NUMERIC,
                    status        VARCHAR(20) NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'rejected'
                    pipeline_lead_id INTEGER REFERENCES merchant_pipeline_leads(id) ON DELETE SET NULL,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    resolved_at   TIMESTAMPTZ,
                    resolved_by   TEXT
                )
            """)
            cur.execute("CREATE INDEX idx_reply_reviews_tenant_pending ON wa_campaign_reply_reviews(tenant_id, status)")
            cur.execute("CREATE UNIQUE INDEX idx_reply_reviews_recipient_pending ON wa_campaign_reply_reviews(recipient_id) WHERE status = 'pending'")

        # ══════════════════════════════════════════════════════════════════
        # Sales Pipeline / Leads redesign (2026-09-09) — approved via design
        # canvas after user feedback on the Lead Scoring build. See
        # project_sales_pipeline_leads_redesign memory for the full design.
        # Three additions:
        #   - merchant_pipeline_leads.outcome distinguishes WHY a deal is
        #     closed-without-winning: 'lost' (pursued it, customer chose
        #     someone else) vs 'dropped' (decided not to pursue at all).
        #     dropped_at/dropped_reason (existing columns) stay the generic
        #     "closed unsuccessfully" timestamp+reason for BOTH — every
        #     existing "dropped_at IS NULL" = active-pipeline check across
        #     the app keeps working unchanged for either outcome.
        #   - tenants.pipeline_stage_labels / lead_score_labels let a
        #     business rename the 6 stage names / 3 score tiers to their own
        #     words — defaults (New Lead/Contacted/.../Won, Hot/Warm/Cold)
        #     apply whenever a key is missing, so an empty '{}' means
        #     "using every default."
        # ══════════════════════════════════════════════════════════════════
        if not _column_exists(cur, "merchant_pipeline_leads", "outcome"):
            cur.execute("ALTER TABLE merchant_pipeline_leads ADD COLUMN outcome VARCHAR(10)")
        if not _column_exists(cur, "tenants", "pipeline_stage_labels"):
            cur.execute("ALTER TABLE tenants ADD COLUMN pipeline_stage_labels JSONB NOT NULL DEFAULT '{}'")
        if not _column_exists(cur, "tenants", "lead_score_labels"):
            cur.execute("ALTER TABLE tenants ADD COLUMN lead_score_labels JSONB NOT NULL DEFAULT '{}'")
        if not _column_exists(cur, "merchant_pipeline_leads", "product_interest"):
            # From the Lead record field list the user specified — what
            # product/service this Lead is actually interested in, distinct
            # from free-text notes.
            cur.execute("ALTER TABLE merchant_pipeline_leads ADD COLUMN product_interest TEXT")
        if not _column_exists(cur, "merchant_pipeline_leads", "source"):
            # The CHANNEL this Lead came from — 'whatsapp', 'facebook',
            # 'instagram', or 'manual' (typed in directly). Facebook/Instagram
            # aren't wired to any creation path yet (those channels don't
            # exist in the product) but the field accepts them for when they
            # are. NULL for every pre-existing row — real, not guessed, going
            # forward. Which specific WhatsApp CAMPAIGN, if any, is a separate
            # thing ("Campaign source") derived via wa_campaign_recipients,
            # not stored redundantly here.
            cur.execute("ALTER TABLE merchant_pipeline_leads ADD COLUMN source VARCHAR(20)")
        else:
            # 2026-09-09 correction: source used to record HOW the row was
            # created (manual/contact/campaign) — redefined to record the
            # CHANNEL instead (whatsapp/facebook/instagram/manual), per
            # project_leads_page_redesign memory. Both old non-manual values
            # meant a WhatsApp contact/reply either way, so this is a safe,
            # lossless one-time relabel, not a guess.
            cur.execute("UPDATE merchant_pipeline_leads SET source='whatsapp' WHERE source IN ('contact', 'campaign')")
        if not _column_exists(cur, "merchant_pipeline_leads", "assigned_to"):
            # Real "Assigned salesperson" field on the Lead record itself —
            # free text (no team-member table linkage yet, so no permissions
            # model to half-build) rather than the earlier ambassador-only
            # or derived-from-history stand-ins.
            cur.execute("ALTER TABLE merchant_pipeline_leads ADD COLUMN assigned_to TEXT")

        # ══════════════════════════════════════════════════════════════════
        # Facebook Messenger — Phase 1 of the omnichannel plan (2026-09-11):
        # connecting a Page and remembering it. Mirrors wa_tenants' shape
        # (one row per connected channel identity, its own access token,
        # an `active` flag) rather than inventing a new pattern. Multiple
        # active Pages per tenant are allowed on purpose, same as wa_tenants
        # allows multiple numbers — no reason a business runs only one Page.
        # `subscribed` starts FALSE: Phase 1 only stores the connection, it
        # does not turn on live message delivery yet (that's Phase 2 — the
        # Inbox handling has to exist first, or messages would arrive and be
        # silently dropped). `fb_user_id` is the Facebook account that did
        # the login/authorization, kept so the Facebook Data Deletion
        # callback (portal_facebook_routes.py) can actually find and remove
        # a business owner's connected Pages on request, instead of always
        # reporting "no data" — see that file's own long-standing comment
        # about this being the moment to wire it up.
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "fb_pages"):
            cur.execute("""
                CREATE TABLE fb_pages (
                    id               SERIAL PRIMARY KEY,
                    tenant_id        INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    page_id          TEXT NOT NULL UNIQUE,
                    page_name        TEXT,
                    access_token     TEXT NOT NULL,
                    fb_user_id       TEXT,
                    active           BOOLEAN NOT NULL DEFAULT TRUE,
                    subscribed       BOOLEAN NOT NULL DEFAULT FALSE,
                    connected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX idx_fb_pages_tenant ON fb_pages(tenant_id) WHERE active")
            cur.execute("CREATE INDEX idx_fb_pages_fb_user ON fb_pages(fb_user_id)")

        # ══════════════════════════════════════════════════════════════════
        # Facebook Messenger — Phase 2 (2026-09-11): messages actually flow
        # into the shared Inbox now. fb_message_log mirrors wa_message_log's
        # shape (same dedup pattern: a partial unique index on
        # meta_message_id, exactly like log_message() in wa_db.py already
        # does for WhatsApp). Conversations are identified by (page_id,
        # psid) — Facebook never gives us a phone number or name, only this
        # anonymous per-Page id, so there is no "customer_phone" here.
        #
        # Reuses wa_conversation_assignments — the WhatsApp claim-before-
        # reply table — for Messenger's claim system too, rather than
        # building a second one: a Messenger conversation's claim key is
        # the text "fb:<page_id>:<psid>" stored in that same
        # customer_phone column. The column is genuinely just an opaque
        # per-conversation string key already (nothing in that table reads
        # it as a phone number), so this is a real reuse, not a hack — it's
        # widened from VARCHAR(40) to VARCHAR(80) purely for headroom, since
        # a Page id + a PSID together run longer than a phone number.
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "fb_message_log"):
            cur.execute("""
                CREATE TABLE fb_message_log (
                    id              SERIAL PRIMARY KEY,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    page_id         TEXT NOT NULL,
                    psid            TEXT NOT NULL,
                    direction       TEXT NOT NULL,
                    content         TEXT,
                    message_type    TEXT NOT NULL DEFAULT 'text',
                    meta_message_id TEXT,
                    sent_by_label   TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX idx_fb_message_log_conv ON fb_message_log(tenant_id, page_id, psid, created_at)")
            cur.execute("""
                CREATE UNIQUE INDEX idx_fb_message_log_dedup ON fb_message_log(meta_message_id)
                WHERE meta_message_id IS NOT NULL
            """)

        cur.execute("""
            SELECT character_maximum_length FROM information_schema.columns
            WHERE table_name='wa_conversation_assignments' AND column_name='customer_phone'
        """)
        _cp_len = (cur.fetchone() or [40])[0]
        if _cp_len and _cp_len < 80:
            cur.execute("ALTER TABLE wa_conversation_assignments ALTER COLUMN customer_phone TYPE VARCHAR(80)")

        # ══════════════════════════════════════════════════════════════════
        # Messenger team access (2026-09-11, urgent — flagged same day
        # Phase 2 shipped). Facebook Pages have no per-agent concept the way
        # WhatsApp numbers do (team_member_agents scopes WHICH AI Agent a
        # team member sees), so Messenger gets its own explicit switch
        # instead of being folded into that table. Deny-by-default, same
        # philosophy as team_member_agents: FALSE until the owner turns it
        # on for that person.
        # ══════════════════════════════════════════════════════════════════
        if not _column_exists(cur, "team_members", "messenger_access"):
            cur.execute("ALTER TABLE team_members ADD COLUMN messenger_access BOOLEAN NOT NULL DEFAULT FALSE")

        # ══════════════════════════════════════════════════════════════════
        # Web Chat team access (2026-09-18, follow-up requested right after
        # the Web Chat channel shipped). Same reasoning as Messenger's
        # switch above — a website-chat session isn't tied to any one AI
        # Agent persona the way a WhatsApp number is, so it gets its own
        # explicit switch instead of being folded into team_member_agents.
        # Deny-by-default: FALSE until the owner turns it on for that person.
        # ══════════════════════════════════════════════════════════════════
        if not _column_exists(cur, "team_members", "webchat_access"):
            cur.execute("ALTER TABLE team_members ADD COLUMN webchat_access BOOLEAN NOT NULL DEFAULT FALSE")

        # ══════════════════════════════════════════════════════════════════
        # Web Chat in the shared Inbox (2026-09-18). The AI website chat
        # widget's human-handoff requests (handoff_requests table) used to
        # only ever show as a "pending" card on the Dashboard — invisible
        # from the Inbox where every other channel lives, which is how a
        # real visitor's handoff sat unanswered without anyone noticing.
        # web_chat_replies stores a staff member's real replies. Unlike
        # WhatsApp/Messenger there's no live API session to push a message
        # back into once the visitor has left the site, so a reply here
        # goes out by email to whatever address they left on the handoff
        # contact form — this table is purely a log of what was sent, for
        # the Inbox thread to display. Claim system reuses
        # wa_conversation_assignments again, same pattern as Messenger's
        # "fb:<page_id>:<psid>" key: key = "web:<session_id>".
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "web_chat_replies"):
            cur.execute("""
                CREATE TABLE web_chat_replies (
                    id              SERIAL PRIMARY KEY,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    session_id      VARCHAR(64) NOT NULL,
                    content         TEXT NOT NULL,
                    sent_by_label   TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX idx_web_chat_replies_session ON web_chat_replies(tenant_id, session_id, created_at)")

        # ══════════════════════════════════════════════════════════════════
        # Meta Business AI connector (2026-09-12). Lets a tenant hand their
        # WhatsApp number's replies over to Meta's own built-in AI instead of
        # PhiXtra's, while PhiXtra stays the CRM underneath: Store Information/
        # System Instruction saves are mirrored to Meta's Business Info/FAQ/
        # Skills APIs, and a new connector lets Meta's AI push leads back into
        # the Sales Pipeline. Deny-by-default — FALSE changes nothing for any
        # existing tenant until they opt in. See project_phixtra_meta_business_
        # agent_connector memory.
        # ══════════════════════════════════════════════════════════════════
        if not _column_exists(cur, "tenants", "meta_ai_enabled"):
            cur.execute("ALTER TABLE tenants ADD COLUMN meta_ai_enabled BOOLEAN NOT NULL DEFAULT FALSE")
        if not _column_exists(cur, "tenants", "meta_ai_last_synced_at"):
            cur.execute("ALTER TABLE tenants ADD COLUMN meta_ai_last_synced_at TIMESTAMPTZ")
        if not _column_exists(cur, "tenants", "meta_ai_last_sync_error"):
            cur.execute("ALTER TABLE tenants ADD COLUMN meta_ai_last_sync_error TEXT")
        # Maps a local FAQ/skill fragment to the id Meta gave it back, so a
        # re-save updates the same Meta-side entry (PUT) instead of creating
        # a duplicate every time. key identifies which local fragment this is
        # (e.g. "faq" for the single Store Info FAQ block, or a wizard
        # behaviour id) since Meta has no id of its own to match back to.
        # Replaces the single bundled meta_ai_last_sync_error: one failure
        # (e.g. Business Info) used to abort Skills/FAQ too and overwrite any
        # earlier per-item detail with one vague message, leaving the admin
        # unable to tell which of the three actually went through. Each of
        # business_info/faq/skills now runs independently and writes its own
        # {ok, error, synced_at} into this JSON instead of sharing one field.
        if not _column_exists(cur, "tenants", "meta_ai_sync_status"):
            cur.execute("ALTER TABLE tenants ADD COLUMN meta_ai_sync_status JSONB NOT NULL DEFAULT '{}'")
        if not _column_exists(cur, "tenants", "meta_ai_eligible"):
            cur.execute("ALTER TABLE tenants ADD COLUMN meta_ai_eligible BOOLEAN")
        if not _column_exists(cur, "tenants", "meta_ai_eligibility_checked_at"):
            cur.execute("ALTER TABLE tenants ADD COLUMN meta_ai_eligibility_checked_at TIMESTAMPTZ")
        # The original file a merchant uploads on Store Information used to
        # be discarded after text extraction — only the extracted text was
        # kept. That meant Files sync to Meta had to rebuild a lossy
        # synthetic .docx from the text instead of sending the real file.
        # Now the original bytes + filename are kept too, so Meta gets the
        # actual PDF/image/etc. unchanged. NULL for anything uploaded before
        # this change (falls back to the old text-rebuild in that case).
        if not _column_exists(cur, "documents", "file_bytes"):
            cur.execute("ALTER TABLE documents ADD COLUMN file_bytes BYTEA")
        if not _column_exists(cur, "documents", "file_name"):
            cur.execute("ALTER TABLE documents ADD COLUMN file_name TEXT")
        if not _table_exists(cur, "meta_ai_synced_items"):
            cur.execute("""
                CREATE TABLE meta_ai_synced_items (
                    id          SERIAL PRIMARY KEY,
                    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    kind        VARCHAR(20) NOT NULL,   -- 'faq' | 'skill'
                    key         VARCHAR(100) NOT NULL,  -- local fragment identifier
                    meta_id     TEXT NOT NULL,           -- id Meta returned
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (tenant_id, kind, key)
                )
            """)

        # ══════════════════════════════════════════════════════════════════
        # PressOne integration (2026-09-12) — CRM angle only, bring-your-own-
        # account: a business that already has a PressOne (Nigerian business
        # phone system) account links it here so their calls show up on the
        # matching Contact's timeline. PhiXtra never resells PressOne
        # numbers or bills for them. See project_phixtra_pressone_integration
        # memory for the full design and the confirmed real webhook payload
        # samples this schema is built from.
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "pressone_accounts"):
            cur.execute("""
                CREATE TABLE pressone_accounts (
                    id                       SERIAL PRIMARY KEY,
                    tenant_id                INTEGER NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
                    account_id               TEXT NOT NULL UNIQUE,
                    api_key                  TEXT NOT NULL,
                    webhook_id               TEXT,
                    webhook_secret           TEXT,
                    active                   BOOLEAN NOT NULL DEFAULT TRUE,
                    auto_reply_missed_calls  BOOLEAN NOT NULL DEFAULT TRUE,
                    connected_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        # Added after the table already existed on this server (2026-09-12,
        # missed-call auto-reply follow-up) — the column above only helps a
        # brand-new install; this covers the live one.
        if not _column_exists(cur, "pressone_accounts", "auto_reply_missed_calls"):
            cur.execute("ALTER TABLE pressone_accounts ADD COLUMN auto_reply_missed_calls BOOLEAN NOT NULL DEFAULT TRUE")
        if not _table_exists(cur, "pressone_calls"):
            cur.execute("""
                CREATE TABLE pressone_calls (
                    id               SERIAL PRIMARY KEY,
                    tenant_id        INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    contact_id       INTEGER REFERENCES wa_contacts(id) ON DELETE SET NULL,
                    call_id          TEXT NOT NULL UNIQUE,
                    call_session_id  TEXT,
                    event            TEXT NOT NULL,
                    direction        TEXT,
                    caller_number    TEXT,
                    callee_number    TEXT,
                    status           TEXT,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    end_reason       TEXT,
                    recording_url    TEXT,
                    raw_payload      JSONB,
                    started_at       TIMESTAMPTZ,
                    ended_at         TIMESTAMPTZ,
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX idx_pressone_calls_contact ON pressone_calls(tenant_id, contact_id, started_at)")

        # ══════════════════════════════════════════════════════════════════
        # Dual Agent Plan pricing (2026-09-18) — see project_dual_agent_pricing
        # memory. The WhatsApp AI Sales Agent and Website (WooCommerce) AI
        # Sales Agent share one account/API key/quota, which made running
        # both cost the same as running just one. Fix: a parallel "dual"
        # tier per plan level, priced at single-channel price x1.8 (the
        # x1.5 then x1.2 formula agreed with the user), applied on top of
        # TODAY's live single-channel prices (not the older figures an
        # earlier design mockup assumed). Existing single-channel plans keep
        # their slugs (only their display name changes) so nothing that
        # already references slug='pro' etc. breaks.
        # ══════════════════════════════════════════════════════════════════
        if not _column_exists(cur, "plans", "channel_mode"):
            cur.execute("ALTER TABLE plans ADD COLUMN channel_mode VARCHAR(10) NOT NULL DEFAULT 'single'")
        if not _column_exists(cur, "plans", "parent_plan_id"):
            cur.execute("ALTER TABLE plans ADD COLUMN parent_plan_id INTEGER REFERENCES plans(id)")
        if not _column_exists(cur, "plans", "is_custom"):
            cur.execute("ALTER TABLE plans ADD COLUMN is_custom BOOLEAN NOT NULL DEFAULT FALSE")
        if not _column_exists(cur, "tenants", "dual_agent_grandfathered"):
            # Manually set by an admin for a merchant who was already running
            # both channels before the dual pricing launched, so they keep
            # their existing single-channel price instead of being pushed
            # onto the new dual price. Checked live on 2026-09-18: no real
            # merchant qualifies today (only 2 internal PhiXtra accounts run
            # both channels) — this exists for future admin use.
            cur.execute("ALTER TABLE tenants ADD COLUMN dual_agent_grandfathered BOOLEAN NOT NULL DEFAULT FALSE")

        # Rename display names only (slugs unchanged) — guarded so a later
        # admin rename via the new plan editor is never overwritten by a re-run.
        cur.execute("UPDATE plans SET name='Startup'    WHERE slug='starter' AND name='Starter'")
        cur.execute("UPDATE plans SET name='Business'   WHERE slug='growth'  AND name='Growth'")
        cur.execute("UPDATE plans SET name='Enterprise' WHERE slug='pro'     AND name='Pro'")

        # Seed the 3 Dual Agent tiers (idempotent — slug is UNIQUE). Limits and
        # feature flags mirror the parent single-channel tier exactly — this
        # fixes the unfair PRICE, it does not change what a tier includes.
        cur.execute("""
            INSERT INTO plans
                (slug, name, price_ngn, price_usd,
                 ai_messages_limit, ai_agents_limit, broadcasts_limit,
                 products_limit, data_sources_limit,
                 feat_crm, feat_advanced_ai, feat_integrations,
                 feat_broadcasts, feat_full_reports, feat_multi_agents,
                 feat_visual_match, feat_fw_checkout, feat_email_campaigns,
                 overage_per_msg_ngn, overage_per_msg_usd,
                 annual_discount_pct, staff_limit, sort_order,
                 channel_mode, parent_plan_id)
            SELECT 'startup_dual', 'Startup — Dual Agent', 45000, 27.00,
                   ai_messages_limit, ai_agents_limit, broadcasts_limit,
                   products_limit, data_sources_limit,
                   feat_crm, feat_advanced_ai, feat_integrations,
                   feat_broadcasts, feat_full_reports, feat_multi_agents,
                   feat_visual_match, feat_fw_checkout, feat_email_campaigns,
                   overage_per_msg_ngn, overage_per_msg_usd,
                   annual_discount_pct, staff_limit, 11,
                   'dual', id
            FROM plans WHERE slug='starter'
            ON CONFLICT (slug) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO plans
                (slug, name, price_ngn, price_usd,
                 ai_messages_limit, ai_agents_limit, broadcasts_limit,
                 products_limit, data_sources_limit,
                 feat_crm, feat_advanced_ai, feat_integrations,
                 feat_broadcasts, feat_full_reports, feat_multi_agents,
                 feat_visual_match, feat_fw_checkout, feat_email_campaigns,
                 overage_per_msg_ngn, overage_per_msg_usd,
                 annual_discount_pct, staff_limit, sort_order,
                 channel_mode, parent_plan_id)
            SELECT 'business_dual', 'Business — Dual Agent', 135000, 81.00,
                   ai_messages_limit, ai_agents_limit, broadcasts_limit,
                   products_limit, data_sources_limit,
                   feat_crm, feat_advanced_ai, feat_integrations,
                   feat_broadcasts, feat_full_reports, feat_multi_agents,
                   feat_visual_match, feat_fw_checkout, feat_email_campaigns,
                   overage_per_msg_ngn, overage_per_msg_usd,
                   annual_discount_pct, staff_limit, 12,
                   'dual', id
            FROM plans WHERE slug='growth'
            ON CONFLICT (slug) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO plans
                (slug, name, price_ngn, price_usd,
                 ai_messages_limit, ai_agents_limit, broadcasts_limit,
                 products_limit, data_sources_limit,
                 feat_crm, feat_advanced_ai, feat_integrations,
                 feat_broadcasts, feat_full_reports, feat_multi_agents,
                 feat_visual_match, feat_fw_checkout, feat_email_campaigns,
                 overage_per_msg_ngn, overage_per_msg_usd,
                 annual_discount_pct, staff_limit, sort_order,
                 channel_mode, parent_plan_id)
            SELECT 'enterprise_dual', 'Enterprise — Dual Agent', 360000, 178.20,
                   ai_messages_limit, ai_agents_limit, broadcasts_limit,
                   products_limit, data_sources_limit,
                   feat_crm, feat_advanced_ai, feat_integrations,
                   feat_broadcasts, feat_full_reports, feat_multi_agents,
                   feat_visual_match, feat_fw_checkout, feat_email_campaigns,
                   overage_per_msg_ngn, overage_per_msg_usd,
                   annual_discount_pct, staff_limit, 13,
                   'dual', id
            FROM plans WHERE slug='pro'
            ON CONFLICT (slug) DO NOTHING
        """)
        # Custom tier — no fixed price, "Talk to Sales". Shown regardless of
        # which channel-mode toggle the merchant has selected on the billing
        # page (channel_mode='both').
        cur.execute("""
            INSERT INTO plans
                (slug, name, price_ngn, price_usd, is_custom, channel_mode,
                 sort_order, is_active,
                 feat_crm, feat_advanced_ai, feat_integrations,
                 feat_broadcasts, feat_full_reports, feat_multi_agents,
                 feat_visual_match, feat_fw_checkout, feat_email_campaigns)
            VALUES ('custom', 'Custom', 0, 0, TRUE, 'both', 99, TRUE,
                    TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE)
            ON CONFLICT (slug) DO NOTHING
        """)

        # ══════════════════════════════════════════════════════════════════
        # Granular plan feature grants (2026-09-18) — the admin Plan Editor
        # previously only had 9 flat feature checkboxes (feat_crm etc.), and
        # most of them (feat_crm, feat_integrations, feat_visual_match) were
        # decorative: shown as ✓/✗ on the billing page but never actually
        # checked by any route, per the audit done before this migration was
        # written. This table lets admin gate real sub-pages individually
        # (e.g. CRM → Companies, Segments, Pipeline Board, Tags, Contacts)
        # instead of one all-or-nothing "CRM" switch.
        #
        # IMPORTANT — every one of these sub-pages is LIVE and fully open to
        # every tenant today (no gate existed before this migration). So this
        # seed grants every existing plan every key below, preserving exactly
        # today's behaviour. Nobody loses access at migration time — the new
        # checkboxes only start doing something the next time an admin
        # unchecks one for a specific plan. Plans created AFTER this
        # migration via the new editor start with nothing granted (an
        # explicit admin choice), which is normal/expected for a brand new
        # plan and is not a live-tenant regression.
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "plan_feature_grants"):
            cur.execute("""
                CREATE TABLE plan_feature_grants (
                    plan_id     INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    feature_key VARCHAR(60) NOT NULL,
                    PRIMARY KEY (plan_id, feature_key)
                )
            """)
            _new_gate_keys = [
                "leads.page", "team.manage",
                "crm.contacts", "crm.companies", "crm.pipeline_board",
                "crm.segments", "crm.tags", "crm.merge_review", "crm.pipeline_settings",
                "ai.handoff_rules",
                "reports.pipeline_overview", "reports.leads_sources", "reports.custom",
                "reports.usage", "reports.cart", "reports.billing",
                "ecom.woo_sync", "ecom.data_sources",
                "ecom.products", "ecom.orders", "ecom.customers", "ecom.discount_settings",
                "woo.product_recommendation", "woo.cross_selling", "woo.cart_recovery",
                "woo.verified_specs", "woo.chat_archive", "woo.message_templates",
                "store.info", "analytics.page",
                "inbox.page", "channels.page", "voice.calls",
                "wa.connect", "wa.handoff_reports",
            ]
            cur.execute("SELECT id FROM plans")
            _all_plan_ids = [r[0] for r in cur.fetchall()]
            for _pid in _all_plan_ids:
                for _key in _new_gate_keys:
                    cur.execute(
                        "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (_pid, _key),
                    )
        else:
            # Table already existed (created before ecom.products/orders/
            # customers/discount_settings/leads.page/team.manage were added
            # to the catalog) — backfill just these newer keys for every
            # existing plan so nobody loses access to pages that were fully
            # open before this change either.
            cur.execute("SELECT id FROM plans")
            _all_plan_ids = [r[0] for r in cur.fetchall()]
            for _pid in _all_plan_ids:
                for _key in ("ecom.products", "ecom.orders", "ecom.customers", "ecom.discount_settings",
                             "leads.page", "team.manage",
                             "woo.product_recommendation", "woo.cross_selling", "woo.cart_recovery",
                             "woo.verified_specs", "woo.chat_archive", "woo.message_templates",
                             "store.info", "analytics.page",
                             "inbox.page", "channels.page", "voice.calls",
                             "wa.connect", "wa.handoff_reports"):
                    cur.execute(
                        "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (_pid, _key),
                    )

        # ══════════════════════════════════════════════════════════════════
        # Module/feature tagging pass (2026-09-19) — closing the gap where
        # several real sidebar screens (Dashboard, Billing, Settings, API
        # Keys, AI Agent Profiles, My Catalogue, Help/Video Tutorials,
        # WhatsApp Report) had NO feature_key at all, so PLAN_FEATURE_CATALOG
        # couldn't be reused as the module list for team-member role
        # permissions (found while scoping real access control). This backfill
        # grants every one of these new keys to every EXISTING plan, same
        # "don't take away something that was already open" rule as every
        # earlier key-catalog expansion above — purely additive, no visible
        # change to any customer from this pass alone.
        # ══════════════════════════════════════════════════════════════════
        cur.execute("SELECT id FROM plans")
        _all_plan_ids_tagging = [r[0] for r in cur.fetchall()]
        for _pid in _all_plan_ids_tagging:
            for _key in ("dashboard.page", "billing.subscription", "billing.credits",
                         "billing.invoices", "billing.payment_gateways", "settings.account",
                         "help.tutorials", "help.videos", "wa.report",
                         "ai.api_keys", "ai.agent_profiles", "ecom.catalogue"):
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        # ── Follow-up same day: three groups (WhatsApp Campaigns, Email
        # Campaigns, Cart Recovery) only had ONE key covering the whole
        # group — every sub-item inside shared it, same coarse-grain problem
        # as team.manage. Flagged by the user after reviewing the first
        # module map. Splitting each sub-item its own key, same non-breaking
        # universal backfill as above (the existing group-level legacy/woo
        # key is untouched and still controls the group header's own lock).
        for _pid in _all_plan_ids_tagging:
            for _key in ("campaigns_wa.all", "campaigns_wa.segments", "campaigns_wa.reports",
                         "campaigns_wa.needs_review",
                         "campaigns_email.all", "campaigns_email.segments", "campaigns_email.reports",
                         "woo.cart_recovery_settings", "woo.cart_recovery_templates"):
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        # ── Second follow-up same day: Inbox and Channels each had exactly
        # ONE key covering everything inside them — a restricted team member
        # role couldn't separate "can view the Inbox" from "can reply/resolve/
        # takeover/edit contacts", or "can view Channels" from "can connect a
        # new Messenger Page / PressOne number" (a real admin-level action).
        # Flagged by the user. inbox.page/channels.page keep their existing
        # meaning (the page itself); these are additive siblings.
        for _pid in _all_plan_ids_tagging:
            for _key in ("inbox.reply", "inbox.claim_release", "inbox.resolve",
                         "inbox.takeover", "inbox.manage_contact",
                         "channels.connect_messenger", "channels.connect_pressone"):
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        # ── Third follow-up same day: Store Information was also one flat
        # key. Its route (store_info()) actually handles 3 distinct actions
        # via a hidden `action` form field — confirmed by reading the route
        # body, not guessed: default save (edit business details/AI knowledge
        # text), `upload_doc` and `delete_doc` (add/remove a document the AI
        # indexes for its own knowledge). Uploading/deleting what the AI
        # learns from is a meaningfully bigger action than editing a text
        # blurb, so it gets its own key rather than folding into "edit".
        # store.info keeps its existing meaning (view); these are additive.
        for _pid in _all_plan_ids_tagging:
            for _key in ("store.info_edit", "store.info_documents"):
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        # ── Fourth follow-up same day: Settings and Leads.
        # Settings' real page (`settings()`, /settings) fans out into 6 real
        # POST routes (settings_profile/_password/_avatar/_notifications/
        # _business/_cancel_plan) confirmed by reading each — grouped into 4
        # keys: password and cancel-plan kept apart from the rest since
        # they're the two genuinely higher-stakes ones (account security,
        # and cancelling a paid plan) — everything else (profile/avatar/
        # notifications) is low-stakes personal preference, grouped together.
        # settings.account keeps its existing meaning (view).
        # (`/settings/payments` is a DIFFERENT route — already tagged
        # billing.payment_gateways in the Billing module, not touched here.)
        # Leads: `leads_page()` handles both view and create (POST) on the
        # same URL; `/leads/create-from-conversation` is the same create
        # action from a different entry point, folded into one key.
        # `/leads/<id>` (Lead Command Centre) is view-only — no new key
        # needed, covered by the existing leads.page. Actually editing a
        # lead's pipeline stage happens via /sales-pipeline/<id>/... routes,
        # already covered by the CRM module — out of scope here.
        for _pid in _all_plan_ids_tagging:
            for _key in ("settings.profile_edit", "settings.business_edit",
                         "settings.password", "settings.cancel_plan",
                         "leads.create"):
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        # ══════════════════════════════════════════════════════════════════
        # tenant_roles (2026-09-19) — real, named, reusable custom roles a
        # business defines for its own team. permissions is keyed by the
        # SAME feature_key strings as PLAN_FEATURE_CATALOG (the catalog
        # above, also the /admin/modules source of truth) — user explicitly
        # chose to keep the 75 real named features as individual on/off
        # toggles rather than a generic View/Create/Modify/Delete per module,
        # after being shown that several features (Inbox's "Resolve"/"Take
        # over", Settings' "Cancel plan") don't map cleanly onto those 4
        # generic verbs. See project_team_access_control memory for the full
        # design history.
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "tenant_roles"):
            cur.execute("""
                CREATE TABLE tenant_roles (
                    id          SERIAL PRIMARY KEY,
                    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name        VARCHAR(100) NOT NULL,
                    permissions JSONB NOT NULL DEFAULT '{}',
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (tenant_id, name)
                )
            """)

        if not _column_exists(cur, "team_members", "role_id"):
            cur.execute(
                "ALTER TABLE team_members ADD COLUMN role_id INTEGER "
                "REFERENCES tenant_roles(id) ON DELETE SET NULL"
            )
            # Backfill EXISTING team members only (tenants that already use
            # this feature today) with a "Support Agent" role that grants
            # EXACTLY today's real behavior — full Inbox access, nothing
            # else — so when enforcement actually gets wired later, nobody's
            # access silently changes on the day this ships. New tenants
            # get their default role lazily created on first visit to the
            # Team/Roles page (_ensure_default_role() in portal_routes.py),
            # not here — no reason to pre-create an unused role for every
            # tenant in the system that has never touched Team.
            _default_support_agent_perms = (
                '{"inbox.page": true, "inbox.reply": true, '
                '"inbox.claim_release": true, "inbox.resolve": true, '
                '"inbox.takeover": true, "inbox.manage_contact": true}'
            )
            cur.execute("SELECT DISTINCT tenant_id FROM team_members")
            _tenant_ids_with_team = [r[0] for r in cur.fetchall()]
            for _tid in _tenant_ids_with_team:
                cur.execute(
                    "INSERT INTO tenant_roles (tenant_id, name, permissions) "
                    "VALUES (%s, %s, %s::jsonb) RETURNING id",
                    (_tid, "Support Agent", _default_support_agent_perms),
                )
                _role_id = cur.fetchone()[0]
                cur.execute(
                    "UPDATE team_members SET role_id=%s WHERE tenant_id=%s AND role_id IS NULL",
                    (_role_id, _tid),
                )

        # ══════════════════════════════════════════════════════════════════
        # team_members profile fields (2026-09-19, follow-up right after the
        # Roles system shipped — user tried creating a user and found the
        # form too thin). first_name/last_name mirror customers' exact
        # types (VARCHAR(100) each, confirmed by reading that table before
        # adding these, not guessed). The existing `name` column is kept
        # as-is and NOT removed — it's read in many places already (chat
        # attribution "Sent by X", avatar initials, invite emails) and is
        # set to "first_name last_name" at creation time in portal_routes.py,
        # so none of those existing call sites need to change.
        # line_manager_id is self-referential (another team_members row),
        # not free text — nullable, SET NULL on delete so removing a manager
        # never blocks or cascades into deleting their reports.
        # ══════════════════════════════════════════════════════════════════
        if not _column_exists(cur, "team_members", "first_name"):
            cur.execute("ALTER TABLE team_members ADD COLUMN first_name VARCHAR(100)")
        if not _column_exists(cur, "team_members", "last_name"):
            cur.execute("ALTER TABLE team_members ADD COLUMN last_name VARCHAR(100)")
        if not _column_exists(cur, "team_members", "department"):
            cur.execute("ALTER TABLE team_members ADD COLUMN department VARCHAR(100)")
        if not _column_exists(cur, "team_members", "position_title"):
            cur.execute("ALTER TABLE team_members ADD COLUMN position_title VARCHAR(100)")
        if not _column_exists(cur, "team_members", "avatar_data"):
            cur.execute("ALTER TABLE team_members ADD COLUMN avatar_data TEXT")
        if not _column_exists(cur, "team_members", "line_manager_id"):
            cur.execute(
                "ALTER TABLE team_members ADD COLUMN line_manager_id INTEGER "
                "REFERENCES team_members(id) ON DELETE SET NULL"
            )

        # ══════════════════════════════════════════════════════════════════
        # Departments & Positions (2026-09-19, same-day follow-up) — user
        # correctly pointed out department/position_title were free-text
        # fields with no way to ever CREATE a department or position, so the
        # dropdown had nothing real to offer. Replaced with real, named,
        # reusable, owner-managed lists — same pattern as tenant_roles, not
        # a new pattern invented for this. Confirmed both free-text columns
        # were still empty for every real row (only 1 real team member
        # existed, department/position_title both blank) before dropping
        # them, so this is a clean replace, not a lossy migration.
        # ══════════════════════════════════════════════════════════════════
        if not _table_exists(cur, "tenant_departments"):
            cur.execute("""
                CREATE TABLE tenant_departments (
                    id         SERIAL PRIMARY KEY,
                    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name       VARCHAR(100) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (tenant_id, name)
                )
            """)
        if not _table_exists(cur, "tenant_positions"):
            cur.execute("""
                CREATE TABLE tenant_positions (
                    id         SERIAL PRIMARY KEY,
                    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name       VARCHAR(100) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (tenant_id, name)
                )
            """)
        if not _column_exists(cur, "team_members", "department_id"):
            cur.execute(
                "ALTER TABLE team_members ADD COLUMN department_id INTEGER "
                "REFERENCES tenant_departments(id) ON DELETE SET NULL"
            )
        if not _column_exists(cur, "team_members", "position_id"):
            cur.execute(
                "ALTER TABLE team_members ADD COLUMN position_id INTEGER "
                "REFERENCES tenant_positions(id) ON DELETE SET NULL"
            )
        # Free-text columns from the previous pass — replaced by the FK
        # columns above, confirmed empty everywhere first (see comment
        # above), safe to drop rather than leave two parallel/confusing
        # systems sitting side by side.
        if _column_exists(cur, "team_members", "department"):
            cur.execute("ALTER TABLE team_members DROP COLUMN department")
        if _column_exists(cur, "team_members", "position_title"):
            cur.execute("ALTER TABLE team_members DROP COLUMN position_title")

        # Location — staff can be in a different city/country from the
        # business itself. Country reuses the EXACT same code/name list
        # already used for billing_country in settings.html (read that
        # template first, not guessed), for a consistent dropdown across the
        # app rather than a second, different country list.
        if not _column_exists(cur, "team_members", "location_city"):
            cur.execute("ALTER TABLE team_members ADD COLUMN location_city VARCHAR(100)")
        if not _column_exists(cur, "team_members", "location_country"):
            cur.execute("ALTER TABLE team_members ADD COLUMN location_country VARCHAR(10)")

        # ── "Super User" design (2026-09-19): Team's single "team.manage" key
        # had the same flat-key problem already fixed for Inbox/Channels/
        # Store Information/Settings — split into granular keys so a
        # delegated, capped role (e.g. a "Super User" that can create team
        # members and assign roles, but can never touch Payment/Billing or
        # hold/hand out delete permissions — enforced in portal_routes.py's
        # _cap_delegated_role_permissions / _feature_catalog_for_actor /
        # _assignable_roles_for_actor) has something real to be granted.
        # team.manage itself is kept (still the plan-tier gate + base "can
        # see the Team page" grant, same pattern as inbox.page) — these are
        # additive, non-breaking, same backfill method as every prior split.
        cur.execute("SELECT id FROM plans")
        _all_plan_ids_tagging = [r[0] for r in cur.fetchall()]
        for _pid in _all_plan_ids_tagging:
            for _key in ("team.members_create", "team.members_assign_role",
                         "team.members_deactivate", "team.members_remove",
                         "team.members_channel_access", "team.roles_manage",
                         "team.roles_delete", "team.departments_manage",
                         "team.departments_delete", "team.positions_manage",
                         "team.positions_delete"):
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        # ── CRM granularity split (2026-09-19): the user pointed out there
        # was no way to give a staff member "view only" — CRM's 7 keys each
        # covered an entire area (view+create+edit+delete all bundled into
        # one switch). Split into 22 real keys (view/create/edit/delete
        # where those actions genuinely exist; named actions — confirm/
        # reject a merge — where they don't, same principle already used
        # for Inbox/Team). Backfill the new keys into plan_feature_grants
        # for every plan (same non-breaking pattern as every prior split),
        # THEN — the part that matters for not silently taking access away
        # — expand every EXISTING role that has an old flat key ticked so
        # it also gets every corresponding new split key ticked. The old
        # key strings are left sitting harmlessly in the permissions JSONB
        # (same as every other superseded key in this app) — nothing reads
        # them anymore, but they're not stripped out.
        cur.execute("SELECT id FROM plans")
        _all_plan_ids_tagging = [r[0] for r in cur.fetchall()]
        _CRM_SPLIT_KEYS = (
            "crm.contacts_view", "crm.contacts_create", "crm.contacts_edit", "crm.contacts_delete",
            "crm.companies_view", "crm.companies_create", "crm.companies_edit",
            "crm.pipeline_board_view", "crm.pipeline_board_edit",
            "crm.segments_view", "crm.segments_create", "crm.segments_edit", "crm.segments_delete",
            "crm.tags_view", "crm.tags_create", "crm.tags_edit", "crm.tags_delete",
            "crm.merge_review_view", "crm.merge_review_confirm", "crm.merge_review_reject",
            "crm.pipeline_settings_view", "crm.pipeline_settings_edit",
        )
        for _pid in _all_plan_ids_tagging:
            for _key in _CRM_SPLIT_KEYS:
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        _CRM_OLD_TO_NEW = {
            "crm.contacts":          ["crm.contacts_view", "crm.contacts_create", "crm.contacts_edit", "crm.contacts_delete"],
            "crm.companies":         ["crm.companies_view", "crm.companies_create", "crm.companies_edit"],
            "crm.pipeline_board":    ["crm.pipeline_board_view", "crm.pipeline_board_edit"],
            "crm.segments":          ["crm.segments_view", "crm.segments_create", "crm.segments_edit", "crm.segments_delete"],
            "crm.tags":              ["crm.tags_view", "crm.tags_create", "crm.tags_edit", "crm.tags_delete"],
            "crm.merge_review":      ["crm.merge_review_view", "crm.merge_review_confirm", "crm.merge_review_reject"],
            "crm.pipeline_settings": ["crm.pipeline_settings_view", "crm.pipeline_settings_edit"],
        }
        try:
            cur.execute("SELECT id, permissions FROM tenant_roles")
            for _role_id, _perms_raw in cur.fetchall():
                _perms = _perms_raw if isinstance(_perms_raw, dict) else (_json.loads(_perms_raw) if _perms_raw else {})
                _changed = False
                for _old_key, _new_keys in _CRM_OLD_TO_NEW.items():
                    if _perms.get(_old_key):
                        for _nk in _new_keys:
                            if not _perms.get(_nk):
                                _perms[_nk] = True
                                _changed = True
                if _changed:
                    cur.execute(
                        "UPDATE tenant_roles SET permissions=%s WHERE id=%s",
                        (_json.dumps(_perms), _role_id),
                    )
        except Exception as e:
            print("⚠️  CRM permissions granularity migration error:", e)

        # ── Ecommerce & Integrations granularity split (2026-09-19, same
        # ask as CRM above) — 7 flat keys -> 18 real ones. Companies had no
        # delete route so none was invented there; same principle applies
        # here: Orders/Customers/Woo Sync only get the split their real
        # routes support (no "create an order" or "delete a customer"
        # action exists, so no such key exists either).
        _ECOM_SPLIT_KEYS = (
            "ecom.products_view", "ecom.products_create", "ecom.products_edit", "ecom.products_delete",
            "ecom.orders_view", "ecom.orders_manage", "ecom.orders_cancel",
            "ecom.customers_view",
            "ecom.woo_sync_view", "ecom.woo_sync_delete",
            "ecom.data_sources_view", "ecom.data_sources_create", "ecom.data_sources_edit", "ecom.data_sources_delete",
            "ecom.discount_settings_view", "ecom.discount_settings_edit",
            "ecom.catalogue_view", "ecom.catalogue_edit",
        )
        for _pid in _all_plan_ids_tagging:
            for _key in _ECOM_SPLIT_KEYS:
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        _ECOM_OLD_TO_NEW = {
            "ecom.products":          ["ecom.products_view", "ecom.products_create", "ecom.products_edit", "ecom.products_delete"],
            "ecom.orders":            ["ecom.orders_view", "ecom.orders_manage", "ecom.orders_cancel"],
            "ecom.customers":         ["ecom.customers_view"],
            "ecom.woo_sync":          ["ecom.woo_sync_view", "ecom.woo_sync_delete"],
            "ecom.data_sources":      ["ecom.data_sources_view", "ecom.data_sources_create", "ecom.data_sources_edit", "ecom.data_sources_delete"],
            "ecom.discount_settings": ["ecom.discount_settings_view", "ecom.discount_settings_edit"],
            "ecom.catalogue":         ["ecom.catalogue_view", "ecom.catalogue_edit"],
        }
        try:
            cur.execute("SELECT id, permissions FROM tenant_roles")
            for _role_id, _perms_raw in cur.fetchall():
                _perms = _perms_raw if isinstance(_perms_raw, dict) else (_json.loads(_perms_raw) if _perms_raw else {})
                _changed = False
                for _old_key, _new_keys in _ECOM_OLD_TO_NEW.items():
                    if _perms.get(_old_key):
                        for _nk in _new_keys:
                            if not _perms.get(_nk):
                                _perms[_nk] = True
                                _changed = True
                if _changed:
                    cur.execute(
                        "UPDATE tenant_roles SET permissions=%s WHERE id=%s",
                        (_json.dumps(_perms), _role_id),
                    )
        except Exception as e:
            print("⚠️  Ecommerce permissions granularity migration error:", e)

        # ── Campaigns granularity split (2026-09-19, same ask) — WhatsApp
        # Campaigns' 4 real keys -> 8, Email Campaigns' 3 real keys -> 10.
        # "Send" (incl. test-sends) kept separate from create/edit for both
        # channels — sending has a real external effect (uses send quota,
        # reaches real customers) distinct from composing.
        _CAMPAIGNS_SPLIT_KEYS = (
            "campaigns_wa.all_view", "campaigns_wa.all_create", "campaigns_wa.all_send", "campaigns_wa.all_delete",
            "campaigns_wa.segments_view", "campaigns_wa.reports_view",
            "campaigns_wa.needs_review_view", "campaigns_wa.needs_review_manage",
            "campaigns_email.all_view", "campaigns_email.all_create", "campaigns_email.all_edit",
            "campaigns_email.all_send", "campaigns_email.all_delete",
            "campaigns_email.segments_view", "campaigns_email.segments_create",
            "campaigns_email.segments_edit", "campaigns_email.segments_delete",
            "campaigns_email.reports_view",
        )
        for _pid in _all_plan_ids_tagging:
            for _key in _CAMPAIGNS_SPLIT_KEYS:
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        _CAMPAIGNS_OLD_TO_NEW = {
            "campaigns_wa.all":          ["campaigns_wa.all_view", "campaigns_wa.all_create", "campaigns_wa.all_send", "campaigns_wa.all_delete"],
            "campaigns_wa.segments":     ["campaigns_wa.segments_view"],
            "campaigns_wa.reports":      ["campaigns_wa.reports_view"],
            "campaigns_wa.needs_review": ["campaigns_wa.needs_review_view", "campaigns_wa.needs_review_manage"],
            "campaigns_email.all":       ["campaigns_email.all_view", "campaigns_email.all_create", "campaigns_email.all_edit", "campaigns_email.all_send", "campaigns_email.all_delete"],
            "campaigns_email.segments":  ["campaigns_email.segments_view", "campaigns_email.segments_create", "campaigns_email.segments_edit", "campaigns_email.segments_delete"],
            "campaigns_email.reports":   ["campaigns_email.reports_view"],
        }
        try:
            cur.execute("SELECT id, permissions FROM tenant_roles")
            for _role_id, _perms_raw in cur.fetchall():
                _perms = _perms_raw if isinstance(_perms_raw, dict) else (_json.loads(_perms_raw) if _perms_raw else {})
                _changed = False
                for _old_key, _new_keys in _CAMPAIGNS_OLD_TO_NEW.items():
                    if _perms.get(_old_key):
                        for _nk in _new_keys:
                            if not _perms.get(_nk):
                                _perms[_nk] = True
                                _changed = True
                if _changed:
                    cur.execute(
                        "UPDATE tenant_roles SET permissions=%s WHERE id=%s",
                        (_json.dumps(_perms), _role_id),
                    )
        except Exception as e:
            print("⚠️  Campaigns permissions granularity migration error:", e)

        # ── WooCommerce Plugin granularity split (2026-09-19, same ask).
        # `woo.cart_recovery` itself (the plan-tier gate) is untouched —
        # only the two role-permission keys built alongside it
        # (`woo.cart_recovery_settings`, `woo.cart_recovery_templates`) get
        # split, same as every module so far.
        _WOO_SPLIT_KEYS = (
            "woo.cart_recovery_view", "woo.cart_recovery_edit",
            "woo.cart_recovery_templates_view", "woo.cart_recovery_templates_edit",
            "woo.verified_specs_view", "woo.verified_specs_create", "woo.verified_specs_delete",
            "woo.chat_archive_view",
            "woo.message_templates_view", "woo.message_templates_edit",
        )
        for _pid in _all_plan_ids_tagging:
            for _key in _WOO_SPLIT_KEYS:
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        _WOO_OLD_TO_NEW = {
            "woo.cart_recovery_settings":  ["woo.cart_recovery_view", "woo.cart_recovery_edit"],
            "woo.cart_recovery_templates": ["woo.cart_recovery_templates_view", "woo.cart_recovery_templates_edit"],
            "woo.verified_specs":          ["woo.verified_specs_view", "woo.verified_specs_create", "woo.verified_specs_delete"],
            "woo.chat_archive":            ["woo.chat_archive_view"],
            "woo.message_templates":       ["woo.message_templates_view", "woo.message_templates_edit"],
        }
        try:
            cur.execute("SELECT id, permissions FROM tenant_roles")
            for _role_id, _perms_raw in cur.fetchall():
                _perms = _perms_raw if isinstance(_perms_raw, dict) else (_json.loads(_perms_raw) if _perms_raw else {})
                _changed = False
                for _old_key, _new_keys in _WOO_OLD_TO_NEW.items():
                    if _perms.get(_old_key):
                        for _nk in _new_keys:
                            if not _perms.get(_nk):
                                _perms[_nk] = True
                                _changed = True
                if _changed:
                    cur.execute(
                        "UPDATE tenant_roles SET permissions=%s WHERE id=%s",
                        (_json.dumps(_perms), _role_id),
                    )
        except Exception as e:
            print("⚠️  WooCommerce permissions granularity migration error:", e)

        # ── Billing granularity split (2026-09-19, same ask) — 4 keys ->
        # 9. Payment Gateways got a 4-way split on purpose: revealing a
        # live secret key is meaningfully more sensitive than just seeing
        # a gateway is connected, so it's its own permission
        # (`_reveal_secret`), separate from viewing connection status
        # (`_view`), connecting/configuring one (`_manage`), and
        # disconnecting one (`_remove`). Nothing here was marked
        # destructive — disconnecting a gateway or removing a saved card
        # doesn't delete a business record, it's reversible.
        _BILLING_SPLIT_KEYS = (
            "billing.subscription_view", "billing.subscription_manage",
            "billing.credits_view", "billing.credits_manage",
            "billing.invoices_view",
            "billing.payment_gateways_view", "billing.payment_gateways_manage",
            "billing.payment_gateways_remove", "billing.payment_gateways_reveal_secret",
        )
        for _pid in _all_plan_ids_tagging:
            for _key in _BILLING_SPLIT_KEYS:
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        _BILLING_OLD_TO_NEW = {
            "billing.subscription":     ["billing.subscription_view", "billing.subscription_manage"],
            "billing.credits":          ["billing.credits_view", "billing.credits_manage"],
            "billing.invoices":         ["billing.invoices_view"],
            "billing.payment_gateways": ["billing.payment_gateways_view", "billing.payment_gateways_manage",
                                          "billing.payment_gateways_remove", "billing.payment_gateways_reveal_secret"],
        }
        try:
            cur.execute("SELECT id, permissions FROM tenant_roles")
            for _role_id, _perms_raw in cur.fetchall():
                _perms = _perms_raw if isinstance(_perms_raw, dict) else (_json.loads(_perms_raw) if _perms_raw else {})
                _changed = False
                for _old_key, _new_keys in _BILLING_OLD_TO_NEW.items():
                    if _perms.get(_old_key):
                        for _nk in _new_keys:
                            if not _perms.get(_nk):
                                _perms[_nk] = True
                                _changed = True
                if _changed:
                    cur.execute(
                        "UPDATE tenant_roles SET permissions=%s WHERE id=%s",
                        (_json.dumps(_perms), _role_id),
                    )
        except Exception as e:
            print("⚠️  Billing permissions granularity migration error:", e)

        # ── AI Assistant granularity split (2026-09-19, same ask). The
        # role-permission use of `legacy:feat_advanced_ai` is replaced by
        # two brand-new keys (`ai.instructions_view`/`_edit`) — the legacy
        # key ITSELF is left untouched in the catalog, since it's also the
        # Admin Plan editor's toggle for whether a plan includes Custom AI
        # Instructions at all (a completely separate mechanism, reading a
        # real boolean column via `_plan_grants_feature`) — removing it
        # would have broken that, not just today's ask.
        _AI_SPLIT_KEYS = (
            "ai.instructions_view", "ai.instructions_edit",
            "ai.handoff_rules_view", "ai.handoff_rules_create", "ai.handoff_rules_edit", "ai.handoff_rules_delete",
            "ai.api_keys_view", "ai.api_keys_revoke",
            "ai.agent_profiles_view", "ai.agent_profiles_create", "ai.agent_profiles_edit", "ai.agent_profiles_delete",
        )
        for _pid in _all_plan_ids_tagging:
            for _key in _AI_SPLIT_KEYS:
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        _AI_OLD_TO_NEW = {
            "legacy:feat_advanced_ai": ["ai.instructions_view", "ai.instructions_edit"],
            "ai.handoff_rules":        ["ai.handoff_rules_view", "ai.handoff_rules_create", "ai.handoff_rules_edit", "ai.handoff_rules_delete"],
            "ai.api_keys":             ["ai.api_keys_view", "ai.api_keys_revoke"],
            "ai.agent_profiles":       ["ai.agent_profiles_view", "ai.agent_profiles_create", "ai.agent_profiles_edit", "ai.agent_profiles_delete"],
        }
        try:
            cur.execute("SELECT id, permissions FROM tenant_roles")
            for _role_id, _perms_raw in cur.fetchall():
                _perms = _perms_raw if isinstance(_perms_raw, dict) else (_json.loads(_perms_raw) if _perms_raw else {})
                _changed = False
                for _old_key, _new_keys in _AI_OLD_TO_NEW.items():
                    if _perms.get(_old_key):
                        for _nk in _new_keys:
                            if not _perms.get(_nk):
                                _perms[_nk] = True
                                _changed = True
                if _changed:
                    cur.execute(
                        "UPDATE tenant_roles SET permissions=%s WHERE id=%s",
                        (_json.dumps(_perms), _role_id),
                    )
        except Exception as e:
            print("⚠️  AI Assistant permissions granularity migration error:", e)

        # ── Channels granularity split (2026-09-19, same ask) — only
        # PressOne had anything to split (Messenger has no disconnect
        # route at all, confirmed by grep, so `channels.connect_messenger`
        # stays a single key — nothing invented for an action that
        # doesn't exist). `channels.connect_pressone` -> view/manage/remove,
        # same 3-way shape as Billing's Payment Gateways.
        _CHANNELS_SPLIT_KEYS = (
            "channels.connect_pressone_view", "channels.connect_pressone_manage", "channels.connect_pressone_remove",
        )
        for _pid in _all_plan_ids_tagging:
            for _key in _CHANNELS_SPLIT_KEYS:
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        try:
            cur.execute("SELECT id, permissions FROM tenant_roles")
            for _role_id, _perms_raw in cur.fetchall():
                _perms = _perms_raw if isinstance(_perms_raw, dict) else (_json.loads(_perms_raw) if _perms_raw else {})
                if _perms.get("channels.connect_pressone"):
                    _changed = False
                    for _nk in _CHANNELS_SPLIT_KEYS:
                        if not _perms.get(_nk):
                            _perms[_nk] = True
                            _changed = True
                    if _changed:
                        cur.execute(
                            "UPDATE tenant_roles SET permissions=%s WHERE id=%s",
                            (_json.dumps(_perms), _role_id),
                        )
        except Exception as e:
            print("⚠️  Channels permissions granularity migration error:", e)

        # ── Final granularity pass, 2026-09-19 — Team's remaining bundled
        # sub-actions (channel access was 3 channel types in 1 switch;
        # roles/departments/positions bundled create+edit) and Store
        # Information's `store.info_documents` (upload+delete bundled).
        # Settings' `settings.profile_edit` was checked and deliberately
        # left bundled — profile/avatar/notifications are all "edit my own
        # low-stakes account stuff" with no real view/create/delete
        # distinction to make, not an oversight.
        #
        # IMPORTANT: `TEAM_MANAGEMENT_FEATURE_KEYS` in portal_routes.py
        # (which decides what makes a role "Super User"-shaped for the
        # delegation cap) was updated to the new key names in the same
        # commit as this migration — if that set is ever out of sync with
        # a Team catalog rename, a delegated role could hold Payment/
        # delete permissions the cap was supposed to block. Checked.
        _TEAM2_SPLIT_KEYS = (
            "team.members_agent_access", "team.members_messenger_access", "team.members_webchat_access",
            "team.roles_create", "team.roles_edit",
            "team.departments_create", "team.departments_edit",
            "team.positions_create", "team.positions_edit",
        )
        for _pid in _all_plan_ids_tagging:
            for _key in _TEAM2_SPLIT_KEYS:
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )
        _STORE_INFO_SPLIT_KEYS = ("store.info_documents_upload", "store.info_documents_delete")
        for _pid in _all_plan_ids_tagging:
            for _key in _STORE_INFO_SPLIT_KEYS:
                cur.execute(
                    "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (_pid, _key),
                )

        _TEAM2_OLD_TO_NEW = {
            "team.members_channel_access": ["team.members_agent_access", "team.members_messenger_access", "team.members_webchat_access"],
            "team.roles_manage":           ["team.roles_create", "team.roles_edit"],
            "team.departments_manage":     ["team.departments_create", "team.departments_edit"],
            "team.positions_manage":       ["team.positions_create", "team.positions_edit"],
            "store.info_documents":        ["store.info_documents_upload", "store.info_documents_delete"],
        }
        try:
            cur.execute("SELECT id, permissions FROM tenant_roles")
            for _role_id, _perms_raw in cur.fetchall():
                _perms = _perms_raw if isinstance(_perms_raw, dict) else (_json.loads(_perms_raw) if _perms_raw else {})
                _changed = False
                for _old_key, _new_keys in _TEAM2_OLD_TO_NEW.items():
                    if _perms.get(_old_key):
                        for _nk in _new_keys:
                            if not _perms.get(_nk):
                                _perms[_nk] = True
                                _changed = True
                if _changed:
                    cur.execute(
                        "UPDATE tenant_roles SET permissions=%s WHERE id=%s",
                        (_json.dumps(_perms), _role_id),
                    )
        except Exception as e:
            print("⚠️  Team/Store-Info final granularity migration error:", e)

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️  catalogue migration error:", e)
    finally:
        cur.close()
        conn.close()
