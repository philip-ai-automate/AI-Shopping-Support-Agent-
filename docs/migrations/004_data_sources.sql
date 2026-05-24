-- Migration 004: Data sources (Google Sheets + file uploads)
-- Safe to re-run: uses CREATE TABLE IF NOT EXISTS

CREATE TABLE IF NOT EXISTS data_sources (
    id                INT            NOT NULL AUTO_INCREMENT PRIMARY KEY,
    tenant_id         INT            NOT NULL,
    source_type       VARCHAR(20)    NOT NULL,   -- 'google_sheet' | 'excel' | 'csv'
    display_name      VARCHAR(255)   DEFAULT NULL,

    -- Google Sheets specific
    sheet_id          VARCHAR(255)   DEFAULT NULL,  -- Google Sheet document ID
    sheet_tab         VARCHAR(255)   DEFAULT NULL,  -- worksheet/tab name (default: first sheet)
    refresh_token_enc TEXT           DEFAULT NULL,  -- Fernet-encrypted refresh token

    -- File upload specific
    file_name         VARCHAR(255)   DEFAULT NULL,
    file_path         VARCHAR(500)   DEFAULT NULL,  -- server-side absolute path

    -- Column mapping (JSON)  keys: name, price, description, category, stock, image_url
    column_map        JSON           DEFAULT NULL,

    -- Sync state
    last_synced_at    DATETIME       DEFAULT NULL,
    last_row_count    INT            DEFAULT NULL,
    sync_status       VARCHAR(20)    NOT NULL DEFAULT 'idle',  -- idle | syncing | success | error
    sync_error        TEXT           DEFAULT NULL,

    is_active         TINYINT(1)     NOT NULL DEFAULT 1,
    created_at        DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_ds_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
