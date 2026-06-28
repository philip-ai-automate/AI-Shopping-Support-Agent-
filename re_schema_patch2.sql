-- ============================================================
-- PhiXtra Real Estate — Schema Patch 2
-- Adds profile columns used by the settings page
-- Run as: psql -h localhost -U phixtra_pg -d ai_support -f re_schema_patch2.sql
-- ============================================================

-- Add contact_email and contact_phone (separate from login email / main phone)
ALTER TABLE re_tenants
    ADD COLUMN IF NOT EXISTS contact_email VARCHAR(255),
    ADD COLUMN IF NOT EXISTS contact_phone VARCHAR(30),
    ADD COLUMN IF NOT EXISTS state         VARCHAR(100) DEFAULT 'Lagos';

-- Backfill: copy existing phone → contact_phone, email → contact_email
UPDATE re_tenants
    SET contact_phone = phone,
        contact_email = email
    WHERE contact_phone IS NULL OR contact_email IS NULL;

-- Add handoff_rules columns to match the UI form fields
ALTER TABLE re_handoff_rules
    ADD COLUMN IF NOT EXISTS rule_name       VARCHAR(100),
    ADD COLUMN IF NOT EXISTS trigger_keyword VARCHAR(80),
    ADD COLUMN IF NOT EXISTS notify_channel  VARCHAR(20) DEFAULT 'whatsapp',
    ADD COLUMN IF NOT EXISTS notify_target   VARCHAR(255);

-- Backfill from old column names
UPDATE re_handoff_rules
    SET rule_name       = COALESCE(trigger_text, 'Rule'),
        trigger_keyword = COALESCE(trigger_text, 'CALLBACK')
    WHERE rule_name IS NULL;


-- Add features JSONB column to re_plans (needed for billing_plans.html feature gating)
ALTER TABLE re_plans
    ADD COLUMN IF NOT EXISTS features JSONB NOT NULL DEFAULT '{}';

-- Seed plan features
UPDATE re_plans SET features = '{"follow_up":true,"broadcasts":true,"inspections":true,"custom_prompt":true}'
    WHERE slug = 'pro';
UPDATE re_plans SET features = '{"follow_up":true,"broadcasts":true,"inspections":true,"custom_prompt":true}'
    WHERE slug = 'growth';
UPDATE re_plans SET features = '{"follow_up":true,"broadcasts":false,"inspections":false,"custom_prompt":false}'
    WHERE slug = 'starter';
UPDATE re_plans SET features = '{"follow_up":false,"broadcasts":false,"inspections":false,"custom_prompt":false}'
    WHERE slug = 'free';
