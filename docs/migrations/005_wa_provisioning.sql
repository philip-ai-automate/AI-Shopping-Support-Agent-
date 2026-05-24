-- Migration 005: WhatsApp merchant auto-provisioning
-- Safe to re-run: ALTER uses MODIFY (idempotent for enums); CREATE uses IF NOT EXISTS

-- 1. Add 'whatsapp' variant to api_keys key_type
ALTER TABLE api_keys
  MODIFY key_type ENUM('paid','trial','whatsapp') NOT NULL DEFAULT 'paid';

-- 2. Tag tenants with their signup origin
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS source_type
      ENUM('web','whatsapp','admin') NOT NULL DEFAULT 'web';

-- 3. OTP codes for WhatsApp portal login
--    One row per attempt; old rows purged by the app after 10 minutes.
CREATE TABLE IF NOT EXISTS wa_portal_otp (
    id          INT         NOT NULL AUTO_INCREMENT PRIMARY KEY,
    phone       VARCHAR(30) NOT NULL,          -- E.164 with + prefix
    otp_code    VARCHAR(10) NOT NULL,
    expires_at  DATETIME    NOT NULL,
    used        TINYINT(1)  NOT NULL DEFAULT 0,
    created_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_wpo_phone (phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
