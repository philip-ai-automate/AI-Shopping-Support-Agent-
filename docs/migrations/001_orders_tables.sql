-- Migration 001: Orders & Order Items tables
-- Run once against the portal database (same DB used by api-key-manager)
-- Safe to re-run: uses CREATE TABLE IF NOT EXISTS

-- ── Orders ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id                VARCHAR(36)    NOT NULL PRIMARY KEY DEFAULT (UUID()),
    tenant_id         INT            NOT NULL,
    reference         VARCHAR(30)    NOT NULL UNIQUE,          -- e.g. ORD-0091
    customer_phone    VARCHAR(25)    NOT NULL,
    customer_name     VARCHAR(255)   DEFAULT NULL,
    delivery_address  TEXT           DEFAULT NULL,
    delivery_fee      DECIMAL(12,2)  NOT NULL DEFAULT 0.00,
    total_amount      DECIMAL(12,2)  NOT NULL DEFAULT 0.00,
    amount_paid       DECIMAL(12,2)  DEFAULT NULL,
    status            VARCHAR(30)    NOT NULL DEFAULT 'INTENT_CAPTURED',
    -- status values: INTENT_CAPTURED | PAYMENT_PENDING | RECEIPT_RECEIVED |
    --                PAYMENT_VERIFIED | PROCESSING | DISPATCHED |
    --                DELIVERED | COMPLETED | CANCELLED | FAILED
    payment_method    VARCHAR(20)    DEFAULT NULL,
    -- payment_method: bank_transfer | paystack | flutterwave
    payment_gateway   VARCHAR(20)    DEFAULT NULL,
    gateway_reference VARCHAR(255)   DEFAULT NULL UNIQUE,      -- prevents duplicate confirmation
    receipt_hash      VARCHAR(64)    DEFAULT NULL,             -- MD5 of receipt image (fraud check)
    receipt_image_url TEXT           DEFAULT NULL,
    tracking_number   VARCHAR(100)   DEFAULT NULL,
    courier           VARCHAR(100)   DEFAULT NULL,
    notes             TEXT           DEFAULT NULL,
    paid_at           DATETIME       DEFAULT NULL,
    dispatched_at     DATETIME       DEFAULT NULL,
    delivered_at      DATETIME       DEFAULT NULL,
    created_at        DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_orders_tenant   (tenant_id),
    INDEX idx_orders_phone    (customer_phone),
    INDEX idx_orders_status   (tenant_id, status),
    INDEX idx_orders_created  (tenant_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ── Order Items ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS order_items (
    id            BIGINT         NOT NULL AUTO_INCREMENT PRIMARY KEY,
    order_id      VARCHAR(36)    NOT NULL,
    product_id    VARCHAR(36)    DEFAULT NULL,                  -- NULL if product deleted
    product_name  VARCHAR(255)   NOT NULL,                     -- snapshot at time of order
    quantity      INT            NOT NULL DEFAULT 1,
    unit_price    DECIMAL(12,2)  NOT NULL,
    subtotal      DECIMAL(12,2)  NOT NULL,

    INDEX idx_oi_order (order_id),
    CONSTRAINT fk_oi_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ── Sequence table for order reference numbers (ORD-0001, ORD-0002, ...) ──────
CREATE TABLE IF NOT EXISTS order_reference_seq (
    tenant_id  INT     NOT NULL PRIMARY KEY,
    last_seq   BIGINT  NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
