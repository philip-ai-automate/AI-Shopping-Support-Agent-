-- ============================================================
-- PhiXtra Real Estate — Schema Patch 1
-- Fixes bugs and adds missing features found in testing
-- Run as: psql -h localhost -U phixtra_pg -d ai_support -f re_schema_patch1.sql
-- ============================================================


-- ── FIX 1: Boom 3 broadcast bug ──────────────────────────────────────────────
-- Old UNIQUE(listing_id, customer_id) blocked price_drop notifications to
-- customers already notified about the same listing via new_listing.
-- Fix: add trigger_type to the unique key so each event type can fire once.

ALTER TABLE re_listing_broadcasts
    DROP CONSTRAINT re_listing_broadcasts_listing_id_customer_id_key;

ALTER TABLE re_listing_broadcasts
    ADD CONSTRAINT uq_broadcast_per_event
    UNIQUE (listing_id, customer_id, trigger_type);


-- ── FIX 2: Boom 1 follow-up re-queue bug ─────────────────────────────────────
-- Old UNIQUE(tenant_id, customer_id, step_day) blocked re-queuing a step once
-- it was sent. A buyer who goes cold again could not re-enter the sequence.
-- Fix: remove hard unique constraint; app uses ON CONFLICT DO UPDATE to
-- reschedule a step (resetting status to 'pending' and updating send_at).

ALTER TABLE re_follow_up_queue
    DROP CONSTRAINT re_follow_up_queue_tenant_id_customer_id_step_day_key;

-- Partial index: only one PENDING entry per (tenant, customer, step) at a time.
-- Sent/cancelled rows are ignored, so re-queueing is allowed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_re_follow_up_pending_step
    ON re_follow_up_queue(tenant_id, customer_id, step_day)
    WHERE status = 'pending';


-- ── FIX 3: Missing wa_app_secret on re_tenants ───────────────────────────────
-- Needed to verify incoming Meta webhook signatures for real estate WA numbers.

ALTER TABLE re_tenants
    ADD COLUMN IF NOT EXISTS wa_app_secret VARCHAR(256);


-- ── FIX 4: No unique active subscription per tenant ──────────────────────────
-- A tenant should only have one active subscription at a time.

CREATE UNIQUE INDEX IF NOT EXISTS uq_re_active_subscription_per_tenant
    ON re_plan_subscriptions(tenant_id)
    WHERE status = 'active';


-- ── FIX 5: trial_ends_at default — new tenants get 30-day Pro trial ──────────
-- Without this, new tenants land on Free immediately with no trial.

ALTER TABLE re_tenants
    ALTER COLUMN trial_ends_at SET DEFAULT (CURRENT_DATE + INTERVAL '30 days');

-- Also set plan_id to Pro for new signups during trial
-- (app code sets plan_id=pro on registration; trial reset cron downgrades on expiry)


-- ── FIX 6: Trigger — mark slot unavailable when booking is confirmed ──────────
-- Prevents double-booking the same inspection slot.

CREATE OR REPLACE FUNCTION re_mark_slot_unavailable()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'confirmed' THEN
        UPDATE re_inspection_slots
        SET is_available = FALSE
        WHERE id = NEW.slot_id;
    END IF;
    IF NEW.status = 'cancelled' AND OLD.status = 'confirmed' THEN
        UPDATE re_inspection_slots
        SET is_available = TRUE
        WHERE id = NEW.slot_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_re_slot_availability ON re_inspection_bookings;
CREATE TRIGGER trg_re_slot_availability
    AFTER INSERT OR UPDATE OF status ON re_inspection_bookings
    FOR EACH ROW
    EXECUTE FUNCTION re_mark_slot_unavailable();


-- ── FIX 7: Trigger — update re_customers.last_seen_at on new message ─────────
-- Keeps buyer activity fresh without manual updates in app code.

CREATE OR REPLACE FUNCTION re_update_customer_last_seen()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.role = 'user' THEN
        -- Use CLOCK_TIMESTAMP() not NOW() — NOW() is frozen per transaction
        UPDATE re_customers
        SET last_seen_at = CLOCK_TIMESTAMP(), updated_at = CLOCK_TIMESTAMP()
        WHERE id = NEW.customer_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_re_customer_last_seen ON re_chat_messages;
CREATE TRIGGER trg_re_customer_last_seen
    AFTER INSERT ON re_chat_messages
    FOR EACH ROW
    EXECUTE FUNCTION re_update_customer_last_seen();


-- ── FIX 8: Full-text search on re_property_listings ──────────────────────────
-- Keyword search fallback alongside vector search (mirrors documents table).

ALTER TABLE re_property_listings
    ADD COLUMN IF NOT EXISTS search_vector tsvector;

CREATE OR REPLACE FUNCTION re_listings_search_vector_trigger()
RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        COALESCE(NEW.title, '')       || ' ' ||
        COALESCE(NEW.location, '')    || ' ' ||
        COALESCE(NEW.lga, '')         || ' ' ||
        COALESCE(NEW.state, '')       || ' ' ||
        COALESCE(NEW.property_type, '') || ' ' ||
        COALESCE(NEW.title_document, '') || ' ' ||
        COALESCE(NEW.description, '')
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS re_tsvector_update ON re_property_listings;
CREATE TRIGGER re_tsvector_update
    BEFORE INSERT OR UPDATE ON re_property_listings
    FOR EACH ROW
    EXECUTE FUNCTION re_listings_search_vector_trigger();

CREATE INDEX IF NOT EXISTS re_listings_search_vector_idx
    ON re_property_listings USING GIN(search_vector);
