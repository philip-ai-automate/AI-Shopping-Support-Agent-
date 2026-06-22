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

        # ── Incremental columns (safe to run on existing installs) ───────────
        for col, defn in [
            ("wa_display_phone",  "TEXT"),
            ("wa_verified_name",  "TEXT"),
            ("onboarding_step",   "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if not _column_exists(cur, "school_profiles", col):
                cur.execute(f"ALTER TABLE school_profiles ADD COLUMN {col} {defn}")

        conn.commit()
        print("✅ school tables ready")
    except Exception as e:
        conn.rollback()
        print(f"⚠️  school_migrations error: {e}")
        raise
    finally:
        cur.close()
        conn.close()
