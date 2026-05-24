-- Migration 003: Payment gateway keys + bank account details
-- Safe to re-run: uses CREATE TABLE IF NOT EXISTS

-- Gateway keys (Paystack / Flutterwave) — secret keys stored Fernet-encrypted
CREATE TABLE IF NOT EXISTS payment_gateways (
    id              INT            NOT NULL AUTO_INCREMENT PRIMARY KEY,
    tenant_id       INT            NOT NULL,
    gateway         VARCHAR(20)    NOT NULL,   -- 'paystack' | 'flutterwave'
    public_key      VARCHAR(255)   DEFAULT NULL,
    secret_key_enc  TEXT           DEFAULT NULL,  -- Fernet-encrypted secret key
    is_active       TINYINT(1)     NOT NULL DEFAULT 1,
    last_webhook_at DATETIME       DEFAULT NULL,  -- updated on every valid webhook received
    created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_tenant_gateway (tenant_id, gateway),
    INDEX idx_pg_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- Bank accounts for bank-transfer payments
CREATE TABLE IF NOT EXISTS merchant_bank_accounts (
    id             INT            NOT NULL AUTO_INCREMENT PRIMARY KEY,
    tenant_id      INT            NOT NULL,
    bank_name      VARCHAR(100)   NOT NULL,
    account_number VARCHAR(20)    NOT NULL,
    account_name   VARCHAR(255)   NOT NULL,
    is_primary     TINYINT(1)     NOT NULL DEFAULT 1,
    created_at     DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_mba_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
