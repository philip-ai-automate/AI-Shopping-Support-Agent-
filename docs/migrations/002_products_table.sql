-- Migration 002: Products catalog table
-- For WhatsApp-only and no-website merchants.
-- WooCommerce merchants use azure_search_index + wa_product_cache instead.
-- Safe to re-run: uses CREATE TABLE IF NOT EXISTS

CREATE TABLE IF NOT EXISTS products (
    id                VARCHAR(36)    NOT NULL DEFAULT (UUID()),
    tenant_id         INT            NOT NULL,
    name              VARCHAR(255)   NOT NULL,
    description       TEXT           DEFAULT NULL,
    price             DECIMAL(12,2)  NOT NULL DEFAULT 0.00,
    stock_quantity    INT            NOT NULL DEFAULT 0,
    reserved_quantity INT            NOT NULL DEFAULT 0,
    category          VARCHAR(100)   DEFAULT NULL,
    attributes        JSON           DEFAULT NULL,   -- e.g. {"color":"red","size":"14"}
    image_url         TEXT           DEFAULT NULL,   -- URL or /static/portal/product_images/...
    source_ref        VARCHAR(255)   DEFAULT NULL,   -- WooCommerce ID, Sheets row ref, etc.
    is_active         TINYINT(1)     NOT NULL DEFAULT 1,
    created_at        DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    INDEX idx_products_tenant   (tenant_id),
    INDEX idx_products_active   (tenant_id, is_active),
    INDEX idx_products_category (tenant_id, category),
    INDEX idx_products_stock    (tenant_id, stock_quantity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
