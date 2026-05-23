"""
portal_migrations.py  — runs on every startup (all statements are idempotent).
DO NOT edit existing CREATE TABLE blocks — only add new ALTER / CREATE below.
"""
from db import get_db_connection


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s""",
        (table, column),
    )
    return int((cur.fetchone() or {}).get("c") or 0) > 0


def ensure_portal_tables():
    conn = get_db_connection()
    if not conn:
        return
    cur  = conn.cursor(dictionary=True, buffered=True)
    cur2 = conn.cursor(buffered=True)
    try:
        # ── EXISTING TABLES (unchanged) ────────────────────────────────────
        cur2.execute("""
            CREATE TABLE IF NOT EXISTS tenant_balances (
                tenant_id     INT PRIMARY KEY,
                token_balance BIGINT NOT NULL DEFAULT 0,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                CONSTRAINT fk_tb_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        cur2.execute("""
            CREATE TABLE IF NOT EXISTS usage_events (
                id          BIGINT PRIMARY KEY AUTO_INCREMENT,
                tenant_id   INT NOT NULL,
                api_key_id  INT NOT NULL,
                website     VARCHAR(255) NULL,
                key_type    ENUM('paid','trial') NULL,
                session_id  VARCHAR(64) NULL,
                used_tokens INT NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_usage_tenant_time (tenant_id, created_at),
                INDEX idx_usage_session (session_id),
                CONSTRAINT fk_ue_tenant FOREIGN KEY (tenant_id)   REFERENCES tenants(id)   ON DELETE CASCADE,
                CONSTRAINT fk_ue_key    FOREIGN KEY (api_key_id)  REFERENCES api_keys(id)  ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        cur2.execute("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id         INT PRIMARY KEY AUTO_INCREMENT,
                username   VARCHAR(255) NOT NULL UNIQUE,
                password   VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB""")

        cur2.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id               INT PRIMARY KEY AUTO_INCREMENT,
                tenant_id        INT NOT NULL,
                email            VARCHAR(255) NOT NULL,
                password_hash    VARCHAR(255) NOT NULL,
                email_verified   TINYINT(1) NOT NULL DEFAULT 0,
                verify_token     VARCHAR(128) NULL,
                reset_token      VARCHAR(128) NULL,
                reset_expires_at DATETIME NULL,
                is_active        TINYINT(1) NOT NULL DEFAULT 1,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_customers_email (email),
                INDEX idx_customers_tenant (tenant_id),
                CONSTRAINT fk_customers_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        cur2.execute("""
            CREATE TABLE IF NOT EXISTS customer_alert_state (
                customer_id      INT PRIMARY KEY,
                last_alert_level VARCHAR(10) NULL,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                CONSTRAINT fk_alert_customer FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        cur2.execute("""
            CREATE TABLE IF NOT EXISTS credit_packages (
                id          INT PRIMARY KEY AUTO_INCREMENT,
                name        VARCHAR(100) NOT NULL,
                credits     INT NOT NULL,
                price_pence INT NOT NULL,
                currency    VARCHAR(10) NOT NULL DEFAULT 'gbp',
                vat_rate    DECIMAL(5,2) NOT NULL DEFAULT 20.00,
                is_active   TINYINT(1) NOT NULL DEFAULT 1,
                sort_order  INT NOT NULL DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB""")

        cur2.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id                    BIGINT PRIMARY KEY AUTO_INCREMENT,
                invoice_number        VARCHAR(50) NOT NULL,
                tenant_id             INT NOT NULL,
                customer_id           INT NOT NULL,
                package_id            INT NULL,
                credits               INT NOT NULL,
                amount_pence          INT NOT NULL,
                vat_pence             INT NOT NULL DEFAULT 0,
                currency              VARCHAR(10) NOT NULL DEFAULT 'gbp',
                status                VARCHAR(30) NOT NULL DEFAULT 'pending',
                stripe_session_id     VARCHAR(255) NULL,
                stripe_payment_intent VARCHAR(255) NULL,
                pdf_path              VARCHAR(512) NULL,
                created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_invoices_customer (customer_id, created_at),
                INDEX idx_invoices_tenant   (tenant_id,   created_at),
                UNIQUE KEY uq_invoice_number (invoice_number),
                CONSTRAINT fk_invoice_tenant   FOREIGN KEY (tenant_id)   REFERENCES tenants(id)   ON DELETE CASCADE,
                CONSTRAINT fk_invoice_customer FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        conn.commit()

        # ── NEW COLUMNS on customers (safe — only added if missing) ───────────
        new_cols = [
            ("first_name",     "ALTER TABLE customers ADD COLUMN first_name     VARCHAR(100) NULL                    AFTER tenant_id"),
            ("last_name",      "ALTER TABLE customers ADD COLUMN last_name      VARCHAR(100) NULL                    AFTER first_name"),
            ("phone_number",   "ALTER TABLE customers ADD COLUMN phone_number   VARCHAR(30)  NULL                    AFTER email"),
            ("phone_verified", "ALTER TABLE customers ADD COLUMN phone_verified TINYINT(1)   NOT NULL DEFAULT 0      AFTER phone_number"),
            ("email_verified", "ALTER TABLE customers ADD COLUMN email_verified TINYINT(1)   NOT NULL DEFAULT 0      AFTER email"),
            ("verify_token",   "ALTER TABLE customers ADD COLUMN verify_token   VARCHAR(200) NULL                    AFTER email_verified"),
            ("is_active",      "ALTER TABLE customers ADD COLUMN is_active      TINYINT(1)   NOT NULL DEFAULT 1"),
        ]
        for col, sql in new_cols:
            if not _column_exists(cur, "customers", col):
                cur2.execute(sql)
                conn.commit()

        # ── NEW COLUMNS: features on credit_packages and tenants ──────────────
        # Stores which plugin features are included in a package or active for a tenant.
        # JSON column — e.g. {"product_recommendation": true}
        # Uses the identical safe _column_exists() pattern already used above.
        if not _column_exists(cur, "credit_packages", "features"):
            cur2.execute(
                "ALTER TABLE credit_packages ADD COLUMN features JSON NULL DEFAULT NULL"
            )
            conn.commit()

        if not _column_exists(cur, "tenants", "features"):
            cur2.execute(
                "ALTER TABLE tenants ADD COLUMN features JSON NULL DEFAULT NULL"
            )
            conn.commit()

        # system_prompt — used by portal register() and ai-backend auth.py
        if not _column_exists(cur, "tenants", "system_prompt"):
            cur2.execute(
                "ALTER TABLE tenants ADD COLUMN system_prompt TEXT NULL DEFAULT NULL"
            )
            conn.commit()

        # azure_search_index / azure_semantic_config — used by ai-backend auth.py
        if not _column_exists(cur, "tenants", "azure_search_index"):
            cur2.execute(
                "ALTER TABLE tenants ADD COLUMN azure_search_index VARCHAR(255) NULL DEFAULT NULL"
            )
            conn.commit()

        if not _column_exists(cur, "tenants", "azure_semantic_config"):
            cur2.execute(
                "ALTER TABLE tenants ADD COLUMN azure_semantic_config VARCHAR(255) NULL DEFAULT NULL"
            )
            conn.commit()

        if not _column_exists(cur, "tenants", "last_full_sync_at"):
            cur2.execute(
                "ALTER TABLE tenants ADD COLUMN last_full_sync_at DATETIME NULL DEFAULT NULL"
            )
            conn.commit()

        # ── NEW TABLES ─────────────────────────────────────────────────────────
        cur2.execute("""
            CREATE TABLE IF NOT EXISTS onboarding_state (
                customer_id               INT PRIMARY KEY,
                wizard_dismissed          TINYINT(1) NOT NULL DEFAULT 0,
                ai_plugin_confirmed       TINYINT(1) NOT NULL DEFAULT 0,
                export_plugin_confirmed   TINYINT(1) NOT NULL DEFAULT 0,
                sync_configured_confirmed TINYINT(1) NOT NULL DEFAULT 0,
                updated_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                CONSTRAINT fk_ob_customer FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        conn.commit()

        # ADD new tracking columns to onboarding_state if missing (for existing installs)
        ob_cols = [
            ("ai_plugin_confirmed",      "ALTER TABLE onboarding_state ADD COLUMN ai_plugin_confirmed      TINYINT(1) NOT NULL DEFAULT 0"),
            ("export_plugin_confirmed",  "ALTER TABLE onboarding_state ADD COLUMN export_plugin_confirmed  TINYINT(1) NOT NULL DEFAULT 0"),
            ("sync_configured_confirmed","ALTER TABLE onboarding_state ADD COLUMN sync_configured_confirmed TINYINT(1) NOT NULL DEFAULT 0"),
        ]
        for col, sql in ob_cols:
            if not _column_exists(cur, "onboarding_state", col):
                cur2.execute(sql)
                conn.commit()

        # Plugin downloads table — admin uploads zips, customers download them
        cur2.execute("""
            CREATE TABLE IF NOT EXISTS plugin_downloads (
                id           INT PRIMARY KEY AUTO_INCREMENT,
                plugin_key   VARCHAR(50) NOT NULL UNIQUE,
                display_name VARCHAR(255) NOT NULL,
                filename     VARCHAR(255) NOT NULL,
                file_path    VARCHAR(512) NOT NULL,
                version      VARCHAR(50) NULL,
                uploaded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB""")

        conn.commit()

        # ── CART REVENUE RECOVERY TABLES ──────────────────────────────────────
        # These three tables power the Intelligent Cart Revenue Recovery feature.
        # All CREATE statements use IF NOT EXISTS so re-running is safe.

        cur2.execute("""
            CREATE TABLE IF NOT EXISTS cart_events (
                id             BIGINT       PRIMARY KEY AUTO_INCREMENT,
                tenant_id      INT          NOT NULL,
                session_id     VARCHAR(128) NOT NULL,
                event_type     VARCHAR(64)  NOT NULL,
                cart_value     DECIMAL(10,2) NULL,
                cart_items     JSON         NULL,
                page_url       VARCHAR(1024) NULL,
                customer_email VARCHAR(255) NULL,
                created_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_ce_tenant_session (tenant_id, session_id),
                INDEX idx_ce_tenant_time    (tenant_id, created_at),
                CONSTRAINT fk_ce_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        cur2.execute("""
            CREATE TABLE IF NOT EXISTS abandonment_queue (
                id             INT          PRIMARY KEY AUTO_INCREMENT,
                tenant_id      INT          NOT NULL,
                session_id     VARCHAR(128) NOT NULL,
                intent_score   TINYINT      NOT NULL DEFAULT 0,
                priority       ENUM('LOW','MEDIUM','HIGH') NOT NULL DEFAULT 'LOW',
                cart_value     DECIMAL(10,2) NULL,
                cart_items     JSON         NULL,
                customer_email VARCHAR(255) NULL,
                status         ENUM('pending','in_progress','recovered','expired')
                               NOT NULL DEFAULT 'pending',
                touches_sent   TINYINT      NOT NULL DEFAULT 0,
                expires_at     DATETIME     NULL,
                created_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                updated_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_aq_tenant_session (tenant_id, session_id),
                INDEX idx_aq_status    (status),
                INDEX idx_aq_expires   (expires_at),
                CONSTRAINT fk_aq_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        cur2.execute("""
            CREATE TABLE IF NOT EXISTS recovery_log (
                id              BIGINT       PRIMARY KEY AUTO_INCREMENT,
                queue_id        INT          NOT NULL,
                action_type     VARCHAR(64)  NOT NULL,
                channel         VARCHAR(32)  NOT NULL DEFAULT 'email',
                message_preview VARCHAR(255) NULL,
                created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_rl_queue (queue_id),
                CONSTRAINT fk_rl_queue FOREIGN KEY (queue_id) REFERENCES abandonment_queue(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        conn.commit()

        # ── push_subscriptions (Web Push Notification subscriptions) ──────────
        cur2.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id          BIGINT        PRIMARY KEY AUTO_INCREMENT,
                tenant_id   INT           NOT NULL,
                session_id  VARCHAR(128)  NOT NULL,
                endpoint    TEXT          NOT NULL,
                p256dh      VARCHAR(512)  NOT NULL,
                auth        VARCHAR(256)  NOT NULL,
                user_agent  VARCHAR(255)  NULL,
                created_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_ps_tenant_session (tenant_id, session_id),
                INDEX idx_ps_tenant (tenant_id),
                CONSTRAINT fk_ps_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        conn.commit()

        # ── handoff_requests — human handoff alerts ────────────────────────────
        # Created by the AI backend when a visitor requests human assistance.
        # Displayed in the portal dashboard as the "Needs Attention" panel.
        cur2.execute("""
            CREATE TABLE IF NOT EXISTS handoff_requests (
                id               BIGINT        PRIMARY KEY AUTO_INCREMENT,
                tenant_id        INT           NOT NULL,
                session_id       VARCHAR(128)  NOT NULL,
                whatsapp_number  VARCHAR(50)   NULL,
                visitor_message  TEXT          NULL,
                status           ENUM('pending','handled') NOT NULL DEFAULT 'pending',
                created_at       TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                handled_at       DATETIME      NULL,
                INDEX idx_hr_tenant_status (tenant_id, status),
                INDEX idx_hr_tenant_time   (tenant_id, created_at),
                CONSTRAINT fk_hr_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        conn.commit()

        # Add contact-capture columns to handoff_requests (collected via widget form)
        for _col, _sql in [
            ("visitor_name",  "ALTER TABLE handoff_requests ADD COLUMN visitor_name  VARCHAR(200) NULL DEFAULT NULL"),
            ("visitor_email", "ALTER TABLE handoff_requests ADD COLUMN visitor_email VARCHAR(254) NULL DEFAULT NULL"),
        ]:
            if not _column_exists(cur, "handoff_requests", _col):
                cur2.execute(_sql)
        conn.commit()

        # ── handoff_rules — configurable handoff trigger rules per tenant ────────
        # Each row is one rule (e.g. "visitor asks about promos → trigger handoff").
        # trigger_type: visitor_initiated = visitor asked; ai_initiated = AI decides.
        # The AI backend reads active rules at chat time and injects them into the
        # system prompt automatically — the store owner never edits raw prompt text.
        cur2.execute("""
            CREATE TABLE IF NOT EXISTS handoff_rules (
                id           INT          PRIMARY KEY AUTO_INCREMENT,
                tenant_id    INT          NOT NULL,
                trigger_text VARCHAR(300) NOT NULL,
                trigger_type ENUM('visitor_initiated','ai_initiated')
                             NOT NULL DEFAULT 'ai_initiated',
                is_active    TINYINT(1)   NOT NULL DEFAULT 1,
                sort_order   INT          NOT NULL DEFAULT 0,
                created_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_hrules_tenant (tenant_id),
                CONSTRAINT fk_hrules_tenant FOREIGN KEY (tenant_id)
                    REFERENCES tenants(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        conn.commit()

        # ── customers: avatar, timezone, notification preferences ─────────────
        for col, sql in [
            ("avatar_data",      "ALTER TABLE customers ADD COLUMN avatar_data     MEDIUMTEXT  NULL DEFAULT NULL"),
            ("timezone",         "ALTER TABLE customers ADD COLUMN timezone        VARCHAR(64) NULL DEFAULT NULL"),
            ("notif_billing",    "ALTER TABLE customers ADD COLUMN notif_billing   TINYINT(1)  NOT NULL DEFAULT 1"),
            ("notif_usage",      "ALTER TABLE customers ADD COLUMN notif_usage     TINYINT(1)  NOT NULL DEFAULT 1"),
            ("notif_marketing",  "ALTER TABLE customers ADD COLUMN notif_marketing TINYINT(1)  NOT NULL DEFAULT 0"),
            # Handoff alert preferences — added to fix missing handoff emails
            ("notif_handoff",        "ALTER TABLE customers ADD COLUMN notif_handoff        TINYINT(1)   NOT NULL DEFAULT 1"),
            ("handoff_notify_email", "ALTER TABLE customers ADD COLUMN handoff_notify_email VARCHAR(254) NULL DEFAULT NULL"),
        ]:
            if not _column_exists(cur, "customers", col):
                cur2.execute(sql)
        conn.commit()

        # ── admin_users: password_hash for bcrypt-secured admin passwords ──────
        if not _column_exists(cur, "admin_users", "password_hash"):
            cur2.execute(
                "ALTER TABLE admin_users ADD COLUMN password_hash VARCHAR(255) NULL DEFAULT NULL"
            )
        conn.commit()

        # ── SEED credit packages ───────────────────────────────────────────────
        cur.execute("SELECT COUNT(*) AS c FROM credit_packages")
        if int((cur.fetchone() or {}).get("c") or 0) == 0:
            cur2.execute("""
                INSERT INTO credit_packages (name, credits, price_pence, sort_order) VALUES
                    ('Starter',  50,  1500, 1),
                    ('Growth',  200,  4500, 2),
                    ('Scale',  1000, 18000, 3)""")
            conn.commit()

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 1 — Subscription & billing schema additions
        # All additions use _column_exists() or CREATE TABLE IF NOT EXISTS so
        # they are 100 % idempotent and safe to run on every startup.
        # NO existing table or column is modified below.
        # ══════════════════════════════════════════════════════════════════════

        # ── credit_packages: two new columns to distinguish subscriptions ─────
        # package_type  : 'topup' = existing one-time purchase (default, keeps
        #                 all current behaviour unchanged)
        #                 'subscription' = recurring monthly / annual plan
        # billing_period: only meaningful when package_type='subscription'
        for col, sql in [
            ("package_type",   "ALTER TABLE credit_packages ADD COLUMN package_type   ENUM('topup','subscription') NOT NULL DEFAULT 'topup'"),
            ("billing_period", "ALTER TABLE credit_packages ADD COLUMN billing_period ENUM('monthly','annual')     NULL     DEFAULT NULL"),
        ]:
            if not _column_exists(cur, "credit_packages", col):
                cur2.execute(sql)
        conn.commit()

        # ── customers: Stripe identity + business / billing info ──────────────
        # stripe_customer_id  : set when a Stripe Customer object is first created
        #                       for this customer (Stage 2). NULL until then —
        #                       all existing checkout code still works fine.
        # company_name        : optional business name shown on invoices
        # vat_number          : optional VAT reg number shown on invoices
        # billing_address_*   : optional billing address fields
        for col, sql in [
            ("stripe_customer_id",    "ALTER TABLE customers ADD COLUMN stripe_customer_id    VARCHAR(255) NULL DEFAULT NULL"),
            ("company_name",          "ALTER TABLE customers ADD COLUMN company_name          VARCHAR(255) NULL DEFAULT NULL"),
            ("vat_number",            "ALTER TABLE customers ADD COLUMN vat_number            VARCHAR(50)  NULL DEFAULT NULL"),
            ("billing_address_line1", "ALTER TABLE customers ADD COLUMN billing_address_line1 VARCHAR(255) NULL DEFAULT NULL"),
            ("billing_city",          "ALTER TABLE customers ADD COLUMN billing_city          VARCHAR(100) NULL DEFAULT NULL"),
            ("billing_postcode",      "ALTER TABLE customers ADD COLUMN billing_postcode      VARCHAR(20)  NULL DEFAULT NULL"),
            ("billing_country",       "ALTER TABLE customers ADD COLUMN billing_country       VARCHAR(10)  NULL DEFAULT 'GB'"),
        ]:
            if not _column_exists(cur, "customers", col):
                cur2.execute(sql)
        conn.commit()

        # ── saved_payment_methods — one row per card saved by a customer ──────
        # Populated in Stage 4 when a customer saves a card via Stripe Elements.
        # is_default=1 means this card is used for subscription renewals and
        # inline top-ups. Only one row per customer can have is_default=1.
        cur2.execute("""
            CREATE TABLE IF NOT EXISTS saved_payment_methods (
                id                     INT          PRIMARY KEY AUTO_INCREMENT,
                customer_id            INT          NOT NULL,
                stripe_payment_method  VARCHAR(255) NOT NULL,
                card_brand             VARCHAR(30)  NULL,
                card_last4             VARCHAR(4)   NULL,
                card_exp_month         TINYINT      NULL,
                card_exp_year          SMALLINT     NULL,
                is_default             TINYINT(1)   NOT NULL DEFAULT 0,
                created_at             TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_spm_method (stripe_payment_method),
                INDEX idx_spm_customer (customer_id),
                CONSTRAINT fk_spm_customer FOREIGN KEY (customer_id)
                    REFERENCES customers(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        conn.commit()

        # ── subscriptions — one active row per subscribing customer ───────────
        # status values:
        #   active     — subscription is live and current
        #   past_due   — latest renewal payment failed, grace period running
        #   suspended  — grace period expired, API key deactivated
        #   cancelled  — customer cancelled; access ends at current_period_end
        # cancel_at_period_end=1 means do not renew; let access run to period end.
        # pending_plan_id is set on a downgrade so the switch happens at renewal.
        cur2.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id                   INT          PRIMARY KEY AUTO_INCREMENT,
                customer_id          INT          NOT NULL,
                tenant_id            INT          NOT NULL,
                package_id           INT          NOT NULL,
                payment_method_id    INT          NULL,
                status               ENUM('active','past_due','suspended','cancelled')
                                     NOT NULL DEFAULT 'active',
                current_period_start DATETIME     NOT NULL,
                current_period_end   DATETIME     NOT NULL,
                cancel_at_period_end TINYINT(1)   NOT NULL DEFAULT 0,
                pending_plan_id      INT          NULL,
                created_at           TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                updated_at           TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_sub_customer (customer_id),
                INDEX idx_sub_tenant   (tenant_id),
                INDEX idx_sub_status   (status),
                INDEX idx_sub_period   (current_period_end),
                CONSTRAINT fk_sub_customer FOREIGN KEY (customer_id)
                    REFERENCES customers(id) ON DELETE CASCADE,
                CONSTRAINT fk_sub_tenant FOREIGN KEY (tenant_id)
                    REFERENCES tenants(id) ON DELETE CASCADE,
                CONSTRAINT fk_sub_package FOREIGN KEY (package_id)
                    REFERENCES credit_packages(id),
                CONSTRAINT fk_sub_payment_method FOREIGN KEY (payment_method_id)
                    REFERENCES saved_payment_methods(id) ON DELETE SET NULL
            ) ENGINE=InnoDB""")

        conn.commit()

        # ── subscription_invoices — one row per recurring billing charge ───────
        # Separate from the existing `invoices` table (which handles top-ups)
        # so neither table needs to change shape and all existing top-up logic
        # stays completely untouched.
        # stripe_payment_intent : set once Stripe confirms the charge succeeded
        # pdf_path              : generated by invoice_pdf.py same as top-ups
        cur2.execute("""
            CREATE TABLE IF NOT EXISTS subscription_invoices (
                id                    BIGINT       PRIMARY KEY AUTO_INCREMENT,
                invoice_number        VARCHAR(50)  NOT NULL,
                subscription_id       INT          NOT NULL,
                customer_id           INT          NOT NULL,
                tenant_id             INT          NOT NULL,
                package_id            INT          NOT NULL,
                credits               INT          NOT NULL,
                amount_pence          INT          NOT NULL,
                vat_pence             INT          NOT NULL DEFAULT 0,
                currency              VARCHAR(10)  NOT NULL DEFAULT 'gbp',
                status                VARCHAR(30)  NOT NULL DEFAULT 'pending',
                period_start          DATETIME     NOT NULL,
                period_end            DATETIME     NOT NULL,
                stripe_payment_intent VARCHAR(255) NULL,
                pdf_path              VARCHAR(512) NULL,
                created_at            TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_subinv_number (invoice_number),
                INDEX idx_subinv_customer    (customer_id, created_at),
                INDEX idx_subinv_tenant      (tenant_id,   created_at),
                INDEX idx_subinv_subscription(subscription_id),
                CONSTRAINT fk_subinv_subscription FOREIGN KEY (subscription_id)
                    REFERENCES subscriptions(id) ON DELETE CASCADE,
                CONSTRAINT fk_subinv_customer FOREIGN KEY (customer_id)
                    REFERENCES customers(id) ON DELETE CASCADE,
                CONSTRAINT fk_subinv_tenant FOREIGN KEY (tenant_id)
                    REFERENCES tenants(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        conn.commit()

        # ── portal_settings — admin-configurable key/value pairs ─────────────
        # Used by admin_save_trial_days and any future admin settings.
        # setting_key is unique so ON DUPLICATE KEY UPDATE works safely.
        cur2.execute("""
            CREATE TABLE IF NOT EXISTS portal_settings (
                id            INT          PRIMARY KEY AUTO_INCREMENT,
                setting_key   VARCHAR(100) NOT NULL UNIQUE,
                setting_value TEXT         NULL,
                updated_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB""")

        conn.commit()

        # ── api_keys: store the plain-text key so the portal can always show it ─
        # The key is already secret (bearer token). Storing it here lets the
        # tenant view their own key at any time on the API Keys page instead of
        # the one-time session reveal that breaks on cross-device verification.
        try:
            cur2.execute(
                "ALTER TABLE api_keys ADD COLUMN api_key_plain VARCHAR(128) NULL DEFAULT NULL"
            )
            conn.commit()
        except Exception as _e:
            if "Duplicate column name" not in str(_e):
                raise

        # ── END STAGE 1 ────────────────────────────────────────────────────────

    finally:
        for obj in (cur, cur2):
            try: obj.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass
