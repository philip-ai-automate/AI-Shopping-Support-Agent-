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

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️  catalogue migration error:", e)
    finally:
        cur.close()
        conn.close()
