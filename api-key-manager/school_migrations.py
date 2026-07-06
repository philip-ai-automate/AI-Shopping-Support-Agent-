"""
school_migrations.py — PhiXtra School (school.phixtra.com)
All CREATE TABLE / ADD COLUMN calls are idempotent.
"""
from db import get_db_connection


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s", (table,)
    )
    return int((cur.fetchone() or [0])[0]) > 0


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, column)
    )
    return int((cur.fetchone() or [0])[0]) > 0


def ensure_school_tables():
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        # ── School profile (one per school, independent of merchant tenants) ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_profiles (
                id               SERIAL PRIMARY KEY,
                school_name      TEXT    NOT NULL,
                school_type      TEXT    NOT NULL DEFAULT 'secondary',
                address          TEXT,
                state            TEXT,
                lga              TEXT,
                principal_name   TEXT,
                contact_email    TEXT    NOT NULL UNIQUE,
                current_session  TEXT    NOT NULL DEFAULT '2025/2026',
                current_term     TEXT    NOT NULL DEFAULT 'First',
                wa_phone_number_id  TEXT,
                wa_access_token     TEXT,
                wa_waba_id          TEXT,
                wa_display_phone    TEXT,
                wa_verified_name    TEXT,
                onboarding_step  INTEGER NOT NULL DEFAULT 0,
                is_active        BOOLEAN NOT NULL DEFAULT TRUE,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── School staff (admin, teacher, bursar) ────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_staff (
                id              SERIAL PRIMARY KEY,
                school_id       INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                full_name       TEXT    NOT NULL,
                email           TEXT    NOT NULL UNIQUE,
                password_hash   TEXT    NOT NULL,
                role            TEXT    NOT NULL DEFAULT 'teacher',
                class_assigned  TEXT,
                whatsapp_number TEXT,
                is_active       BOOLEAN NOT NULL DEFAULT TRUE,
                last_login      TIMESTAMPTZ,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── Students ─────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_students (
                id              SERIAL PRIMARY KEY,
                school_id       INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                full_name       TEXT    NOT NULL,
                student_number  TEXT,
                gender          TEXT,
                class_name      TEXT    NOT NULL,
                arm             TEXT    NOT NULL DEFAULT 'A',
                is_active       BOOLEAN NOT NULL DEFAULT TRUE,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── Parents / guardians ───────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_parents (
                id                SERIAL PRIMARY KEY,
                school_id         INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                full_name         TEXT    NOT NULL,
                whatsapp_number   TEXT    NOT NULL,
                relationship      TEXT    NOT NULL DEFAULT 'Parent',
                is_opted_in       BOOLEAN NOT NULL DEFAULT TRUE,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── Student ↔ parent links (many-to-many) ────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_student_parents (
                student_id  INTEGER NOT NULL REFERENCES school_students(id) ON DELETE CASCADE,
                parent_id   INTEGER NOT NULL REFERENCES school_parents(id)  ON DELETE CASCADE,
                PRIMARY KEY (student_id, parent_id)
            )
        """)

        # ── Fee schedules ─────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_fee_schedules (
                id          SERIAL PRIMARY KEY,
                school_id   INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                name        TEXT    NOT NULL,
                class_name  TEXT,
                amount      NUMERIC(12,2) NOT NULL,
                due_date    DATE,
                session     TEXT,
                term        TEXT,
                is_active   BOOLEAN NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── Fee payments (per student per schedule) ───────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_fee_payments (
                id           SERIAL PRIMARY KEY,
                schedule_id  INTEGER NOT NULL REFERENCES school_fee_schedules(id) ON DELETE CASCADE,
                student_id   INTEGER NOT NULL REFERENCES school_students(id)      ON DELETE CASCADE,
                amount_paid  NUMERIC(12,2) NOT NULL DEFAULT 0,
                payment_date DATE,
                payment_ref  TEXT,
                status       TEXT NOT NULL DEFAULT 'unpaid',
                notes        TEXT,
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(schedule_id, student_id)
            )
        """)

        # ── Attendance ────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_attendance (
                id               SERIAL PRIMARY KEY,
                school_id        INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                student_id       INTEGER NOT NULL REFERENCES school_students(id) ON DELETE CASCADE,
                attendance_date  DATE    NOT NULL,
                status           TEXT    NOT NULL DEFAULT 'present',
                wa_notified      BOOLEAN NOT NULL DEFAULT FALSE,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(student_id, attendance_date)
            )
        """)

        # ── Broadcasts ────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_broadcasts (
                id              SERIAL PRIMARY KEY,
                school_id       INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                title           TEXT    NOT NULL,
                message         TEXT    NOT NULL,
                target_class    TEXT,
                sent_count      INTEGER NOT NULL DEFAULT 0,
                delivered_count INTEGER NOT NULL DEFAULT 0,
                status          TEXT    NOT NULL DEFAULT 'draft',
                sent_at         TIMESTAMPTZ,
                created_by      INTEGER REFERENCES school_staff(id),
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── School knowledge base (feeds the AI parent bot) ───────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_knowledge (
                id          SERIAL PRIMARY KEY,
                school_id   INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                category    TEXT    NOT NULL DEFAULT 'general',
                question    TEXT    NOT NULL,
                answer      TEXT    NOT NULL,
                is_active   BOOLEAN NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── RAG: uploaded source documents (handbooks, calendars, policies) ──────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_kb_documents (
                id            SERIAL PRIMARY KEY,
                school_id     INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                filename      TEXT    NOT NULL,
                file_path     TEXT    NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'processing',
                chunk_count   INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                uploaded_by   INTEGER REFERENCES school_staff(id),
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── RAG: unified retrieval index — one row per QA entry, one per document chunk ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_kb_chunks (
                id            SERIAL PRIMARY KEY,
                school_id     INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                source_type   TEXT    NOT NULL,
                source_id     INTEGER NOT NULL,
                chunk_index   INTEGER NOT NULL DEFAULT 0,
                title         TEXT,
                content       TEXT    NOT NULL,
                embedding     vector(1536),
                search_vector tsvector,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_school_kb_chunks_school
                ON school_kb_chunks(school_id, source_type)
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_school_kb_chunks_source
                ON school_kb_chunks(source_type, source_id, chunk_index)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_school_kb_chunks_embedding
                ON school_kb_chunks USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_school_kb_chunks_search_vector
                ON school_kb_chunks USING GIN(search_vector)
        """)
        cur.execute("""
            CREATE OR REPLACE FUNCTION school_kb_chunks_search_vector_trigger()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('english',
                    COALESCE(NEW.title, '') || ' ' || COALESCE(NEW.content, '')
                );
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'school_kb_chunks_tsvector_update'
                      AND tgrelid = 'school_kb_chunks'::regclass
                ) THEN
                    CREATE TRIGGER school_kb_chunks_tsvector_update
                    BEFORE INSERT OR UPDATE ON school_kb_chunks
                    FOR EACH ROW EXECUTE FUNCTION school_kb_chunks_search_vector_trigger();
                END IF;
            END;
            $$
        """)

        # One-time backfill: mirror any existing active school_knowledge rows into
        # school_kb_chunks (SQL-only, embedding left NULL — populated separately by
        # backfill_kb_embeddings.py so app startup never blocks on an OpenAI call).
        cur.execute("""
            INSERT INTO school_kb_chunks (school_id, source_type, source_id, chunk_index, title, content)
            SELECT k.school_id, 'qa', k.id, 0, k.category,
                   'Q: ' || k.question || E'\\nA: ' || k.answer
            FROM school_knowledge k
            WHERE k.is_active = TRUE
              AND NOT EXISTS (
                SELECT 1 FROM school_kb_chunks c
                WHERE c.source_type = 'qa' AND c.source_id = k.id
              )
        """)

        # ── WhatsApp message templates (one row per school + notification type) ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_wa_templates (
                id              SERIAL PRIMARY KEY,
                school_id       INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                template_type   TEXT    NOT NULL,
                template_name   TEXT    NOT NULL,
                language_code   TEXT    NOT NULL DEFAULT 'en_US',
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(school_id, template_type)
            )
        """)

        # ── Payment gateways — each school connects its OWN Paystack/Flutterwave
        #    account; PhiXtra never holds or moves school funds ─────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_payment_gateways (
                id              SERIAL PRIMARY KEY,
                school_id       INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                gateway         TEXT    NOT NULL,
                public_key      TEXT,
                secret_key_enc  TEXT,
                is_active       BOOLEAN NOT NULL DEFAULT TRUE,
                last_webhook_at TIMESTAMPTZ,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(school_id, gateway)
            )
        """)

        # ── Idempotency ledger for gateway webhooks/callbacks — a UNIQUE(gateway,
        #    tx_ref) means a retried webhook can INSERT ... ON CONFLICT DO NOTHING
        #    and we know to skip crediting the payment again ───────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_fee_gateway_txns (
                id          SERIAL PRIMARY KEY,
                school_id   INTEGER NOT NULL REFERENCES school_profiles(id)      ON DELETE CASCADE,
                payment_id  INTEGER NOT NULL REFERENCES school_fee_payments(id)  ON DELETE CASCADE,
                gateway     TEXT    NOT NULL,
                tx_ref      TEXT    NOT NULL,
                amount      NUMERIC(12,2) NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(gateway, tx_ref)
            )
        """)

        # ── Pricing plans (Free / Starter / Growing / Established / Enterprise) ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_plans (
                id                    SERIAL PRIMARY KEY,
                slug                  VARCHAR(32)   UNIQUE NOT NULL,
                name                  VARCHAR(64)   NOT NULL,
                student_min           INTEGER       NOT NULL DEFAULT 0,
                student_max           INTEGER       NOT NULL DEFAULT -1,
                price_ngn_termly      INTEGER,
                price_ngn_annual      INTEGER,
                ai_messages_limit     INTEGER       NOT NULL DEFAULT 100,
                staff_limit           INTEGER       NOT NULL DEFAULT 2,
                feat_document_rag     BOOLEAN       NOT NULL DEFAULT FALSE,
                feat_broadcasts       BOOLEAN       NOT NULL DEFAULT FALSE,
                feat_custom_templates BOOLEAN       NOT NULL DEFAULT FALSE,
                feat_priority_support BOOLEAN       NOT NULL DEFAULT FALSE,
                is_active             BOOLEAN       NOT NULL DEFAULT TRUE,
                sort_order            INTEGER       NOT NULL DEFAULT 0,
                created_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW()
            )
        """)

        # Seed the 5 plans (idempotent — slug is UNIQUE)
        cur.execute("""
            INSERT INTO school_plans
                (slug, name, student_min, student_max,
                 price_ngn_termly, price_ngn_annual,
                 ai_messages_limit, staff_limit,
                 feat_document_rag, feat_broadcasts,
                 feat_custom_templates, feat_priority_support, sort_order)
            VALUES
              ('free',        'Free',        0,    50,   0,      0,      100,  2,  FALSE, FALSE, FALSE, FALSE, 0),
              ('starter',     'Starter',     51,   200,  45000,  121500, 500,  5,  FALSE, TRUE,  FALSE, FALSE, 1),
              ('growing',     'Growing',     201,  499,  90000,  216000, 2000, -1, TRUE,  TRUE,  FALSE, FALSE, 2),
              ('established', 'Established', 500,  999,  160000, 384000, 5000, -1, TRUE,  TRUE,  TRUE,  TRUE,  3),
              ('enterprise',  'Enterprise',  1000, -1,   NULL,   NULL,   -1,   -1, TRUE,  TRUE,  TRUE,  TRUE,  4)
            ON CONFLICT (slug) DO NOTHING
        """)

        # ── school_quota_overage_log ──────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_quota_overage_log (
                id          BIGSERIAL PRIMARY KEY,
                school_id   INTEGER      NOT NULL,
                logged_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                plan_slug   VARCHAR(32),
                msgs_used   INTEGER,
                msgs_limit  INTEGER,
                notified    BOOLEAN      NOT NULL DEFAULT FALSE
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_school_quota_overage_school
                ON school_quota_overage_log(school_id, logged_at)
        """)

        # ── school_plan_subscriptions — audit trail of paid plan purchases,
        #    one row per checkout (mirrors merchant portal's plan_subscriptions) ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_plan_subscriptions (
                id                    SERIAL PRIMARY KEY,
                school_id             INTEGER NOT NULL REFERENCES school_profiles(id) ON DELETE CASCADE,
                plan_id               INTEGER NOT NULL REFERENCES school_plans(id),
                billing_cycle         VARCHAR(10) NOT NULL,
                payment_provider      TEXT NOT NULL DEFAULT 'flutterwave',
                tx_ref                TEXT UNIQUE,
                status                VARCHAR(16) NOT NULL DEFAULT 'active',
                amount                NUMERIC(12,2),
                current_period_start  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                current_period_end    TIMESTAMPTZ,
                created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_school_plan_subs_school
                ON school_plan_subscriptions(school_id, created_at)
        """)

        # ── school_billing_cycle_days — single source of truth for how many
        #    days of runway each billing cycle buys. Read by BOTH school_billing.py
        #    (api-key-manager, checkout) and school_plan_reset.py (school-wa-gateway,
        #    expiry sweep) — two separate services/processes that can't share a
        #    Python import, so the DB is the one place both agree on these numbers.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS school_billing_cycle_days (
                cycle VARCHAR(10) PRIMARY KEY,
                days  INTEGER NOT NULL
            )
        """)
        cur.execute("""
            INSERT INTO school_billing_cycle_days (cycle, days)
            VALUES ('termly', 120), ('annual', 366)
            ON CONFLICT (cycle) DO NOTHING
        """)

        # ── Incremental columns (safe to run on existing installs) ───────────
        for col, defn in [
            ("wa_display_phone",  "TEXT"),
            ("wa_verified_name",  "TEXT"),
            ("onboarding_step",   "INTEGER NOT NULL DEFAULT 0"),
            ("wa_absence_template_name", "TEXT"),
            ("wa_absence_template_lang", "TEXT NOT NULL DEFAULT 'en'"),
            ("default_payment_gateway", "TEXT"),
            ("plan_id",           "INTEGER REFERENCES school_plans(id) DEFAULT 1"),
            ("billing_cycle",     "VARCHAR(10) NOT NULL DEFAULT 'termly'"),
            ("plan_period_start", "DATE NOT NULL DEFAULT CURRENT_DATE"),
            ("quota_notified_at", "TIMESTAMPTZ DEFAULT NULL"),
            ("renewal_notified_at", "TIMESTAMPTZ DEFAULT NULL"),
        ]:
            if not _column_exists(cur, "school_profiles", col):
                cur.execute(f"ALTER TABLE school_profiles ADD COLUMN {col} {defn}")

        for col, defn in [
            ("marked_at", "TIMESTAMPTZ"),
            ("marked_by", "INTEGER REFERENCES school_staff(id)"),
        ]:
            if not _column_exists(cur, "school_attendance", col):
                cur.execute(f"ALTER TABLE school_attendance ADD COLUMN {col} {defn}")

        for col, defn in [
            ("last_reminded_at", "TIMESTAMPTZ"),
            ("last_reminded_by", "INTEGER REFERENCES school_staff(id)"),
            ("reminder_count",   "INTEGER NOT NULL DEFAULT 0"),
            ("payment_token",    "TEXT"),
        ]:
            if not _column_exists(cur, "school_fee_payments", col):
                cur.execute(f"ALTER TABLE school_fee_payments ADD COLUMN {col} {defn}")

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_school_fee_payments_token
                ON school_fee_payments(payment_token)
                WHERE payment_token IS NOT NULL
        """)

        for col, defn in [
            ("first_name",  "TEXT"),
            ("middle_name", "TEXT"),
            ("last_name",   "TEXT"),
        ]:
            if not _column_exists(cur, "school_students", col):
                cur.execute(f"ALTER TABLE school_students ADD COLUMN {col} {defn}")

        # ── One-time backfill: split existing full_name into first/middle/last ──
        cur.execute("SELECT id, full_name FROM school_students WHERE first_name IS NULL")
        for row in cur.fetchall():
            parts = row[1].split()
            first = parts[0] if parts else ""
            last  = parts[-1] if len(parts) > 1 else None
            middle = " ".join(parts[1:-1]) if len(parts) > 2 else None
            cur.execute(
                "UPDATE school_students SET first_name=%s, middle_name=%s, last_name=%s WHERE id=%s",
                (first, middle, last, row[0])
            )

        # ── One-time copy: school_profiles absence-template columns → school_wa_templates ──
        cur.execute("""
            INSERT INTO school_wa_templates (school_id, template_type, template_name, language_code)
            SELECT id, 'absence_alert', wa_absence_template_name, wa_absence_template_lang
            FROM school_profiles
            WHERE wa_absence_template_name IS NOT NULL
            ON CONFLICT (school_id, template_type) DO NOTHING
        """)

        conn.commit()
        print("✅ school tables ready")
    except Exception as e:
        conn.rollback()
        print(f"⚠️  school_migrations error: {e}")
        raise
    finally:
        cur.close()
        conn.close()
