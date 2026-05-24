-- 007_wa_merchant_onboarding.sql
-- Stores state for the WhatsApp-first merchant onboarding conversation.
-- One row per merchant phone number; upserted as the state machine advances.

CREATE TABLE IF NOT EXISTS wa_merchant_onboarding (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  wa_phone    VARCHAR(32)  NOT NULL,
  state       VARCHAR(64)  NOT NULL DEFAULT 'COLLECT_BIZ_NAME',
  collected   JSON         NOT NULL,
  tenant_id   INT          NULL,       -- set after provisioning completes
  created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY  uq_phone (wa_phone),
  INDEX       idx_state    (state)
);
