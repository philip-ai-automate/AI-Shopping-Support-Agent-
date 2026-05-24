-- Migration 006: Daily WhatsApp report settings
-- Safe to re-run: uses ADD COLUMN IF NOT EXISTS

-- Enable/disable daily report per tenant (default on)
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS daily_report_enabled TINYINT(1) NOT NULL DEFAULT 1;

-- Optional override phone for report delivery.
-- If NULL, falls back to customers.phone_number for that tenant.
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS report_phone VARCHAR(30) DEFAULT NULL;

-- Track last successful send to avoid duplicates on restart
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS last_report_sent_at DATETIME DEFAULT NULL;
