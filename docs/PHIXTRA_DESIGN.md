# Phixtra AI — Full System Design

**Last updated:** 2026-05-24  
**Status:** Active design document — update as decisions are made  
**Architect:** Claude (Anthropic) + Philip (Phixtra founder)

---

## Resume State (read this first after any reconnect)

This section contains everything needed to resume work immediately without re-reading the conversation.

### Active Codebase

| Component | Path | Service |
|-----------|------|---------|
| Merchant portal (Flask) | `/root/phixtra-app/api-key-manager/` | `phixtra-portal` (port 5055) |
| Portal routes | `/root/phixtra-app/api-key-manager/portal_routes.py` | — |
| Portal templates | `/root/phixtra-app/api-key-manager/templates/portal/` | — |
| Base template (sidebar) | `/root/phixtra-app/api-key-manager/templates/portal/base.html` | — |
| Static assets / uploads | `/root/phixtra-app/api-key-manager/static/` | — |
| AI backend (FastAPI) | `/root/phixtra-app/ai-backend/` | `phixtra-ai-backend` (port 8000) |
| WhatsApp gateway (FastAPI) | `/root/phixtra-app/whatsapp-gateway/` | `phixtra-whatsapp-gateway` (port 8001) |
| Data sync | `/root/phixtra-app/phixtra-data-sync/` | `phixtra-data-sync` (port 8010) |
| DB migrations | `/root/phixtra-app/docs/migrations/` | run manually with mysql |
| Design document | `/root/phixtra-app/docs/PHIXTRA_DESIGN.md` | this file |

### Database

```
Host:     localhost
User:     ai_user
Password: ./Admin@15365858!
Database: ai_support
Engine:   MariaDB / MySQL
```

Connect: `mysql -u ai_user -p'./Admin@15365858!' ai_support`

### Key Commands

```bash
# ⚠️  TWO Flask services exist — restart the RIGHT one:
#
#   phixtra-portal     → portal.phixtra.com  (port 5055, portal_app:app)  ← THIS ONE
#   phixtra-api-keys   → keys/API manager    (port 5000, app:app)          ← NOT this one
#
# After ANY change to portal_routes.py, templates, or base.html:
systemctl restart phixtra-portal

# Verify portal is running
systemctl is-active phixtra-portal

# Check for Python errors before restart
cd /root/phixtra-app/api-key-manager && python3 -c "from portal_routes import portal_bp; print('OK')"

# Run a migration
mysql -u ai_user -p'./Admin@15365858!' ai_support < /root/phixtra-app/docs/migrations/00X_name.sql
```

### Portal Tech Stack

- **Framework**: Flask 3.1.3 + Jinja2, served by Gunicorn
- **Auth**: Session-based (`portal_logged_in`, `customer_id` in Flask session)
- **DB access**: `from db import get_db_connection` → returns `mysql.connector` connection
- **Route pattern**: `_require_login()` guard → `_get_customer(_customer_id())` → tenant_id → logic
- **CSS design tokens**: `--ink` (dark navy), `--good` (green), `--warn` (amber), `--bad` (red), `--muted`, `--line`, `--shadow`
- **Reusable CSS classes**: `.card`, `.btn`, `.btn-primary`, `.btn-danger`, `.btn-sm`, `.pill`, `.pill-green`, `.pill-warn`, `.pill-red`, `.pill-grey`, `.table-wrap`, `.kpi`, `.muted`, `.section-title`, `.page-header`, `.form-group`, `.form-row`, `.flash`
- **Sidebar pattern**: flat `.sb-link` for high-traffic pages, `.sb-group` + `.sb-group-toggle` + `.sb-sub` for grouped items

### Important DB Facts

- `tenants.id` is **INT AUTO_INCREMENT** (not UUID) — always cast with `int()`
- `tenants.azure_search_index` is set for WooCommerce/Azure merchants, NULL for WhatsApp-only
- WhatsApp customer phones are stored in `wa_message_log.customer_phone` and `wa_handoff_state.customer_phone`
- Web widget sessions in `chat_sessions` have **no phone number** — they are anonymous browser sessions
- `orders.customer_phone` stores the WhatsApp number for all WhatsApp orders
- Products table: `is_active = 0` = soft-deleted; `stock_quantity >= 999` = unlimited stock

### Critical Operational Notes

- **Always restart `phixtra-portal`** after portal code changes — NOT `phixtra-api-keys`
- `phixtra-api-keys` (port 5000, `app:app`) is a separate internal service — restarting it does nothing to the portal
- Portal entry point is `portal_app.py` → imports `portal_routes.py` → registers `portal_bp`
- After restart, check logs: `journalctl -u phixtra-portal --no-pager -n 20`
- Test import before restart: `cd /root/phixtra-app/api-key-manager && /root/phixtra-app/api-key-manager/venv/bin/python -c "from portal_app import app; print('OK')"`

### What Has Been Built (complete log)

1. **PHIXTRA_DESIGN.md** — `/root/phixtra-app/docs/PHIXTRA_DESIGN.md` — full system design document
2. **SQL migration 001** — `orders`, `order_items`, `order_reference_seq` tables — ✅ run
3. **SQL migration 002** — `products` table — ✅ run
4. **Orders module** — `portal_routes.py` (5 routes + 4 helpers), `orders.html`, `order_detail.html`, `📦 Orders` sidebar link
5. **Products module** — `portal_routes.py` (6 routes + 5 helpers), `products.html`, `product_form.html`, `🏪 Commerce` sidebar group, image upload dir `/static/portal/product_images/`
6. **Customers module** — `portal_routes.py` (2 routes + 2 helpers), `customers.html`, `customer_detail.html`, `Customers` added to Commerce group
7. **Service restart fix** — corrected wrong service (`phixtra-portal`, not `phixtra-api-keys`) in all docs
8. **Payment Gateways module** — `portal_routes.py` (8 routes), `payment_settings.html`, `💳 Payment Gateways` sidebar link, `payment_gateways` + `merchant_bank_accounts` tables, Fernet encryption via `cryptography` package (installed in venv)
9. **Analytics module** — `portal_routes.py` (1 route + 1 helper `_analytics_data`), `analytics.html`, `📈 Analytics` sidebar link added to `base.html`. Revenue KPIs, conversion rate, avg order value, Chart.js (v4.4.0 CDN) bar charts for daily revenue and daily AI conversations, top 5 products table, AI performance card with handoff rate.
10. **Data Sources module** — `portal_routes.py` (8 routes), `data_sources.html`, `data_source_map.html`, `data_source_google_setup.html`, `🗂️ Data Sources` sidebar link. SQL migration 004. Excel/CSV upload with drag-drop, column mapping UI, `_import_rows()` upserts to products table. Google Sheets OAuth2 (requires `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` env vars; gracefully degrades when unconfigured). Packages installed: `openpyxl`, `google-auth`, `google-auth-oauthlib`, `google-api-python-client`.
11. **WhatsApp merchant provisioning + OTP login** — SQL migration 005 (`api_keys.key_type` now includes 'whatsapp'; `tenants.source_type` column; `wa_portal_otp` table). `provision_whatsapp_merchant(phone, name)` idempotent helper creates tenant + customer + api_key + balance. `POST /internal/provision-wa-merchant` endpoint (protected by `PHIXTRA_INTERNAL_TOKEN` env var) for the WA gateway to call. OTP login flow: `/wa-login` → `/wa-login/send` → `/wa-login/verify` + `/wa-login/resend`. OTP sent via Meta Cloud API using `WA_OTP_PHONE_NUMBER_ID` + `WA_OTP_ACCESS_TOKEN` env vars; if unconfigured, code appears in flash for dev testing. WhatsApp API keys display as "Managed automatically by PhiXtra" on the API Keys page. Login page has "Log in with WhatsApp" button.
13. **Admin WA onboarding panel** — `/admin/onboard-wa` — form (business name + phone) calls `provision_whatsapp_merchant()`, shows all provisioned WA merchants in a table with "📱 Send link" pre-filled WhatsApp button. Nav link added to admin topbar.
14. **Self-service WA registration** — `/register` page now has a merchant type selector (website vs WhatsApp-only). WA path collects business name + WA phone + email/password; creates tenant (`source_type='whatsapp'`, domain=NULL), customer (real email/password, email_verified=0), `key_type='whatsapp'` api_key. Sends email verification. Merchant can log in via email/password OR WhatsApp OTP.
12. **Daily WhatsApp reports** — SQL migration 006 (`tenants.daily_report_enabled`, `tenants.report_phone`, `tenants.last_report_sent_at`). New file `/whatsapp-gateway/wa_daily_report.py`: `_get_daily_stats()` queries orders/revenue/new customers/conversations/handoffs/low-stock; `_format_report()` builds plain-text message; `send_daily_report_for_tenant()` tries template then plain text; `run_daily_reports()` batch runner with double-send guard. APScheduler `AsyncIOScheduler` in `main.py` fires at 07:00 UTC (08:00 WAT) daily with 1-hour misfire grace. `POST /wa-daily-report` manual trigger (protected by `PHIXTRA_INTERNAL_TOKEN`). Portal settings: daily report toggle + report phone override field, saved via `settings_notifications` to `tenants` table. `apscheduler` added to gateway `requirements.txt`.

15. **WhatsApp-first onboarding state machine (Option 1)** — SQL migration 007 (`wa_merchant_onboarding` table). New file `/whatsapp-gateway/wa_onboarding.py`: full conversational state machine (COLLECT_BIZ_NAME → COLLECT_CATEGORY → COLLECT_SALES_NUMBER → COLLECT_BANK → PRODUCT_NAME/PRICE/STOCK/DESC/IMAGE/CONFIRM loop → ADD_MORE → COMPLETE). Handles `restart` at any step. On completion calls `POST /internal/provision-wa-merchant` (portal API), then saves bank account to `merchant_bank_accounts` and products to `products` table directly. Integrated into `meta_webhook.py` — messages to `WA_SETUP_PHONE_NUMBER_ID` are routed here before tenant lookup; dedup uses tenant_id=0 sentinel. Required env vars: `WA_SETUP_PHONE_NUMBER_ID`, `WA_SETUP_ACCESS_TOKEN`.

### Next Tasks (in priority order)

| # | Module | Notes |
|---|--------|-------|
| 1 | **Wire production OTP sending** | Set `WA_OTP_PHONE_NUMBER_ID` + `WA_OTP_ACCESS_TOKEN` in phixtra-portal systemd unit |
| 2 | **Wire gateway provisioning** | Set `PHIXTRA_INTERNAL_TOKEN` + `PORTAL_INTERNAL_URL` + `WA_SETUP_PHONE_NUMBER_ID` + `WA_SETUP_ACCESS_TOKEN` in phixtra-whatsapp-gateway systemd unit |
| 3 | **Google OAuth credentials** | Set `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` + `GOOGLE_OAUTH_REDIRECT_URI` env vars |
| 4 | **Create Meta daily report template** | Template name `phixtra_daily_report` (utility category), 6 body params: name, date, orders, revenue, conversations, handoffs |

---

## Build Status

Tracks what has been designed vs built vs deployed.

### Portal Modules

| Module | Status | Routes | Templates | DB Migration |
|--------|--------|--------|-----------|--------------|
| Auth (login/register/OTP) | ✅ Built | register, login, logout, verify, forgot, reset | ✅ | n/a |
| Dashboard | ✅ Built | /dashboard | ✅ | n/a |
| AI Assistant | ✅ Built | /api-keys, /system-instruction, /handoff-rules, /verified-specs-settings | ✅ | n/a |
| Cart Recovery | ✅ Built | /cart-recovery, /cart-recovery/settings | ✅ | n/a |
| Billing (Stripe) | ✅ Built | /billing, /billing/subscribe, /invoices | ✅ | n/a |
| Reports | ✅ Built | /reports/usage, /reports/cart, /reports/billing | ✅ | n/a |
| Chat Archive | ✅ Built | /chat-archive | ✅ | n/a |
| WhatsApp | ✅ Built | /whatsapp, /whatsapp/inbox, /whatsapp/campaigns, /whatsapp/templates | ✅ | n/a |
| Account Settings | ✅ Built | /settings | ✅ | n/a |
| **Orders** | ✅ Built | /orders, /orders/\<id\>, /orders/\<id\>/dispatch, /deliver, /cancel | ✅ | ✅ 001_orders_tables.sql |
| **Products** | ✅ Built | /products, /products/add, /products/\<id\>/edit, /delete, /toggle-stock | ✅ | ✅ 002_products_table.sql |
| **Data Sources** | ✅ Built | /data-sources + 7 sub-routes | ✅ | ✅ 004_data_sources.sql |
| **Customers** | ✅ Built | /customers, /customers/\<phone\> | ✅ | n/a (uses existing tables) |
| **Payment Gateways** | ✅ Built | /settings/payments + 6 sub-routes | ✅ | ✅ 003_payment_gateways.sql |
| **Analytics** | ✅ Built | /analytics | ✅ | n/a (reads existing tables) |

### WhatsApp Merchant Billing

| Item | Status |
|------|--------|
| API key auto-provisioning on WhatsApp onboarding | ✅ Built — `provision_whatsapp_merchant()` + `/internal/provision-wa-merchant` |
| `key_type = 'whatsapp'` in api_keys table | ✅ Built — migration 005 |
| OTP login via WhatsApp number | ✅ Built — `/wa-login` flow, 6-digit OTP, 10 min expiry, 60s rate limit |
| Token tracking at tenant_id level | ✅ Already works (tenant_balances table exists) |
| Daily WhatsApp reports | ✅ Built — migration 006, `wa_daily_report.py`, APScheduler 07:00 UTC, portal toggle |

### Env Vars Required for Full WA Provisioning

| Var | Service | Purpose |
|-----|---------|---------|
| `PHIXTRA_INTERNAL_TOKEN` | portal + gateway | Shared secret for `/internal/provision-wa-merchant` |
| `WA_OTP_PHONE_NUMBER_ID` | portal | Meta phone_number_id for OTP sends |
| `WA_OTP_ACCESS_TOKEN` | portal | Meta access token for OTP sends |

### Database Tables in `ai_support`

| Table | Purpose | Status |
|-------|---------|--------|
| tenants | Merchant accounts | ✅ Exists |
| customers | Portal login users | ✅ Exists |
| api_keys | Widget auth keys + billing | ✅ Exists |
| tenant_balances | Token balance per merchant | ✅ Exists |
| wa_tenants | WhatsApp Business connection | ✅ Exists |
| wa_handoff_state | Human agent handoffs | ✅ Exists |
| wa_message_log | All WhatsApp messages | ✅ Exists |
| wa_product_cache | WooCommerce widget product cache | ✅ Exists (not full catalog) |
| wa_campaigns | Broadcast campaigns | ✅ Exists |
| wa_templates | Message templates | ✅ Exists |
| orders | WhatsApp orders | ✅ Migrated (001) |
| order_items | Line items per order | ✅ Migrated (001) |
| order_reference_seq | ORD-XXXX numbering | ✅ Migrated (001) |
| products | Merchant product catalog | ✅ Migrated (002) |

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Business Onboarding — No-Website Merchants](#2-business-onboarding--no-website-merchants)
3. [Customer Journey on WhatsApp](#3-customer-journey-on-whatsapp)
4. [Order Management & Fulfillment](#4-order-management--fulfillment)
5. [Payment Auto-Confirmation](#5-payment-auto-confirmation)
6. [Portal Dashboard — portal.phixtra.com](#6-portal-dashboard--portalphixtracom)
7. [Database Schema Reference](#7-database-schema-reference)
8. [Infrastructure & Services](#8-infrastructure--services)

---

## 1. System Architecture

### Core Philosophy: Hybrid Knowledge Architecture

**Vector search is NOT a replacement for databases. It is an intelligence enhancement layer.**

The AI answers ONLY from retrieved data. It never invents prices, stock levels, or policies. If data is unavailable, it says: *"I don't have that information yet."*

### Three-Layer Query Routing

| Query Type | Route To | Example |
|------------|----------|---------|
| Exact facts | PostgreSQL (SQL) | "How much is the Ankara dress?" |
| Semantic/intent | Azure AI Search / pgvector | "What do you have for a wedding?" |
| Budget-based | Both SQL + Vector | "What can I get for ₦10,000?" |

### Five-Step WhatsApp Processing Flow

```
1. Receive webhook (Meta Cloud API)
        ↓
2. Tenant identification (merchant_id from webhook URL)
        ↓
3. Intent detection (LLM classifies: browse / order / track / support / handoff)
        ↓
4. Query routing:
   ├── SQL   → price, stock, order status, delivery zones
   ├── Vector → product recommendations, FAQs, policies
   └── Both  → budget queries, bundle suggestions
        ↓
5. Response generation (LLM) → send via Meta Cloud API
```

### Anti-Hallucination Rules

- AI only quotes prices retrieved from the SQL database
- AI only confirms stock if current stock_quantity > 0
- AI never invents delivery times unless a merchant-defined policy exists
- If a customer asks something the AI can't answer from data: "Let me connect you with someone from our team."

### Tech Stack

| Component | Technology |
|-----------|-----------|
| AI Backend | FastAPI + Uvicorn (port 8000) |
| WhatsApp Gateway | FastAPI + Uvicorn (port 8001) |
| Merchant Portal | Flask + Gunicorn (port 5055) |
| Data Sync | FastAPI + Uvicorn (port 8010) |
| Database | PostgreSQL (primary) + MariaDB (index sync) |
| Vector Search | Azure AI Search + pgvector |
| LLM | GPT-4.1 via Azure OpenAI |
| Message API | Meta Cloud API (WhatsApp Business) |
| File Storage | Azure Blob / local uploads |

### Multi-Tenant Architecture

Each merchant has:
- Unique `tenant_id` (UUID)
- Separate webhook URL: `https://api.phixtra.com/webhook/{merchant_id}`
- Merchant-specific secret key embedded in webhook URL for verification before any DB lookup
- Isolated product data, orders, customers, AI instructions

---

## 1.5 API Key & Billing Architecture for All Merchant Types

### The Core Question

The portal creates an API key after registration. That key tracks AI token usage and drives billing. But WhatsApp-onboarded merchants never touch the portal — so how does billing work for them?

### Answer: The API Key Is a Widget Auth Mechanism, Not the Billing Unit

The API key exists so that a website embed can authenticate calls to Phixtra's chat API. WhatsApp merchants don't call any API — Phixtra **receives** their customers' messages via Meta webhook. They never need an API key in their hands.

But token tracking and billing still must happen. The solution:

**Auto-generate an internal API key the moment WhatsApp onboarding completes.**  
The merchant never sees it. It is system-managed, tied to their tenant, and plugs into the exact same billing infrastructure as web merchants.

### Flow

```
WhatsApp onboarding completes
          ↓
System auto-creates:
  ├── tenants record (source_type = 'whatsapp')
  ├── customers record (portal login identity — phone-based, no email required)
  ├── tenant_balances record (token balance = 0, trial starts now)
  └── api_keys record:
        key_type     = 'whatsapp'     ← new type, never shown to merchant
        website      = NULL
        trial_starts = NOW()
        trial_expires = NOW() + 14 days
          ↓
System sends merchant portal link via WhatsApp:
  "Your store is live! View dashboard: portal.phixtra.com"
          ↓
WhatsApp gateway processes customer messages:
  customer phone → lookup tenant_id by WA business number → AI backend
          ↓
AI backend returns response + token_count
          ↓
Token count logged against tenant_id (tenant_balances table)
          ↓
Balance low → WhatsApp notification to merchant
Balance zero → AI pauses, merchant notified
```

### Portal Login for WhatsApp Merchants (OTP, No Password)

```
Merchant visits portal.phixtra.com
  → enters WhatsApp number
  → receives 6-digit OTP via WhatsApp
  → logs in
  → sees full dashboard (orders, products, usage, billing)
  → can buy credits directly in portal
  → sees their API key labelled "WhatsApp Integration (auto-managed)"
```

### What Changes in Code

**1. New `key_type = 'whatsapp'` value** in the `api_keys` table. Portal renders it differently — no "copy key" button, instead shows "Managed automatically by Phixtra."

**2. Auto-provision function called at end of WhatsApp onboarding:**

```python
def provision_whatsapp_merchant(wa_phone: str, business_name: str) -> str:
    tenant_id  = create_tenant(business_name, wa_phone, source_type='whatsapp')
    customer_id = create_portal_customer(phone=wa_phone, tenant_id=tenant_id)
    ensure_tenant_balance(tenant_id)
    _auto_generate_whatsapp_key(tenant_id)   # never shown to merchant
    send_whatsapp(wa_phone,
        "Setup complete! View your dashboard: portal.phixtra.com\n"
        "Log in with this WhatsApp number — we'll send an OTP.")
    return tenant_id
```

**3. WhatsApp gateway logs tokens against tenant_id:**

```python
# After every AI call in the gateway:
log_token_usage(tenant_id=tenant_id, tokens=response.token_count)
# Uses the same tenant_balances table — no API key reference needed
```

**4. Billing engine runs on `tenant_id`** — already the case (`tenant_balances` keyed by `tenant_id`). No changes to billing logic.

### Comparison Table

| | Web/WooCommerce Merchant | WhatsApp-Only Merchant |
|--|--------------------------|------------------------|
| Gets API key? | Yes — copies into their site | Yes — auto-generated, never shown |
| Portal login | Email + password | WhatsApp OTP |
| Token tracking | Per `tenant_id` | Per `tenant_id` — identical |
| Billing system | Stripe / credits | Stripe / credits — identical |
| Trial start | When they create first key | When WhatsApp onboarding completes |
| Portal value | Widget config, cart recovery | Orders, products, payments |

The billing infrastructure is fully shared. The API key for WhatsApp merchants is an internal implementation detail.

---

## 2. Business Onboarding — No-Website Merchants

### Target User
Nigerian business owner. No website. Sells via Instagram DMs, WhatsApp, or physical market. Products in Excel, Google Drive, or their head.

### Onboarding Entry Points

1. **WhatsApp-first**: Merchant messages Phixtra setup number → full onboarding via WhatsApp
2. **Web signup**: `portal.phixtra.com/register` → continue setup in portal
3. **Admin-created**: Phixtra admin provisions account → merchant gets WhatsApp welcome

### WhatsApp Onboarding State Machine

```
START
  │
  ├─ "Hi" / "Hello" / any message to setup number
  │
  ▼
[WELCOME]
  "Welcome to Phixtra! I'll help set up your AI sales assistant.
   What's your business name?"
  │
  ▼
[COLLECT_BIZ_NAME] → store business_name
  "Great! {name} sounds good. What do you sell?
   (e.g. clothes, shoes, electronics, food)"
  │
  ▼
[COLLECT_CATEGORY] → store business_category
  "What's your WhatsApp number customers will use to order?
   (This can be this same number)"
  │
  ▼
[COLLECT_SALES_NUMBER] → store wa_sales_number
  "Do you have a bank account for receiving transfers?
   Send: BankName | AccountNumber | AccountName"
  │
  ▼
[COLLECT_BANK] → store bank_name, account_number, account_name
  "Perfect! Now let's add your first product.
   What's the product name?"
  │
  ▼
[PRODUCT LOOP — repeat per product]
  ├── ask_name    → "Product name?"
  ├── ask_price   → "Price in Naira? (numbers only)"
  ├── ask_stock   → "How many do you have? (or 'unlimited')"
  ├── ask_desc    → "One line description? (or skip)"
  ├── ask_image   → "Send a photo? (or skip)"
  └── confirm     → "Save this product? Reply YES or NO"
       │
       ▼
  [NEXT_PRODUCT]
  "Product saved ✅ Add another? Reply YES or NO"
  │
  ├── YES → loop back to ask_name
  └── NO  ↓
  │
  ▼
[SETUP_COMPLETE]
  "Your store is live! 🎉
   Customers can now message +234XXXXXXXXXX to shop.
   
   View your dashboard: portal.phixtra.com
   Login with this number — we'll send an OTP."
```

### Product Data Ingestion Sources

All sources normalize to the same PostgreSQL product schema before embedding generation.

| Source | Mechanism | Column Mapping |
|--------|-----------|----------------|
| WhatsApp conversation | State machine collects fields | Native |
| Excel / CSV upload | File upload → AI maps columns | AI-assisted (GPT maps "Amount" → price, "Qty Left" → stock_quantity) |
| Google Sheets | OAuth2 + Sheets API | AI-assisted, 15-min scheduled sync |
| WooCommerce | REST API pull + webhook push | Direct WooCommerce field names |
| Manual (portal) | Portal form | Native |

### AI Column Mapping (Excel/CSV)

When a merchant uploads an Excel file with non-standard headers, GPT-4.1 maps them:

```
Input headers:  ["Item", "Amount", "Qty Left", "Colour", "Sizes Available"]
GPT mapping:    {
  "Item"            → "product_name",
  "Amount"          → "price",
  "Qty Left"        → "stock_quantity",
  "Colour"          → "attribute:color",
  "Sizes Available" → "attribute:size"
}
```

Merchant sees a preview table in the portal before confirming the import.

### OTP Authentication (Portal Login)

No passwords. Merchant logs into portal by:
1. Enter their WhatsApp number
2. Receive 6-digit OTP via WhatsApp
3. Enter OTP on portal
4. Logged in

---

## 3. Customer Journey on WhatsApp

### Entry: Customer Messages the Business WhatsApp Number

The Meta webhook fires. Phixtra identifies the merchant from the phone number, loads merchant context, begins conversation.

### Conversation Flow

```
CUSTOMER: "Hi"
AI: "Hello! Welcome to {Business Name} 👋
     I'm your AI assistant. How can I help you today?
     
     You can:
     • Browse our products
     • Check prices and availability  
     • Place an order
     • Track an existing order"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BROWSE FLOW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CUSTOMER: "What do you have?"
[AI → Vector search: semantic product catalog query]
AI: Lists top products with prices

CUSTOMER: "What can I get for 10k?"
[AI → SQL: price ≤ 10000 AND stock > 0]
AI: Returns matching products

CUSTOMER: "Do you have ankara in size 16?"
[AI → SQL: product_name ILIKE '%ankara%' AND attributes @> '{"size":"16"}' AND stock > 0]
AI: Exact availability answer

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ORDER FLOW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CUSTOMER: "I want to buy the Ankara dress"
AI: "Great choice! The Ankara Dress is ₦12,500.
     What size do you need?"
     [Stock reserved immediately]

CUSTOMER: "Size 14"
AI: "Perfect. What's your delivery address?"

CUSTOMER: [address]
AI: "Delivery to Ikeja, Lagos will be ₦1,500.
     Total: ₦14,000
     
     Ready to confirm your order?
     Reply YES to proceed or NO to cancel."

CUSTOMER: "YES"
AI: "Order confirmed! ✅ Order #ORD-0091
     
     Payment options:
     1️⃣ Bank Transfer: GTBank | 0123456789 | Phixtra Stores
     2️⃣ Pay with card: [Paystack link]
     
     Transfer ₦14,000 and send your receipt here.
     Your order is reserved for 30 minutes."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PAYMENT FLOW (Bank Transfer)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CUSTOMER: [sends receipt image]
AI: "Receipt received! 📸 Verifying..."
[Backend: extract image hash, check for duplicates, OCR amount, verify ≥ order total]

→ If valid:
AI: "Payment confirmed ✅ 
     Your order is being processed.
     We'll notify you when it ships."
[Order: PAYMENT_PENDING → PAYMENT_VERIFIED → PROCESSING]

→ If duplicate receipt hash:
AI: "This receipt has already been used for another order.
     Please send the correct receipt."
[Alert sent to merchant portal]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRACKING FLOW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CUSTOMER: "Where is my order?"
[AI → SQL: orders WHERE customer_phone = ? ORDER BY created_at DESC]
AI: "Order #ORD-0091 status: Dispatched 🚚
     Sent via GIG Express. Tracking: GIG123456
     Expected delivery: Tomorrow by 5pm"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HANDOFF FLOW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CUSTOMER: "I want to talk to a real person"
AI: "Of course! Connecting you now...
     A team member will be with you shortly.
     
     While you wait, your conversation reference is #ORD-0091."
[Handoff created → appears in portal Agent Inbox]
[Merchant gets WhatsApp notification: "Customer +234803... requested a human agent"]
```

### Pidgin English Handling

The AI understands Nigerian Pidgin automatically. Examples:
- "Abeg how much be the gown?" → treats as price query
- "E don finish?" → treats as stock availability query
- "I wan buy am" → treats as order intent
- "E never reach me" → treats as delivery status query

### Customer Ghost Protocol (No Response After Confirmation)

```
T+0:  Order confirmed, payment details sent
T+30m: "Hi! Just checking in on Order #ORD-0091. 
        Did you complete the payment? Your reservation expires soon."
T+2h:  "Your reservation for the Ankara Dress expires in 1 hour.
        Complete payment to secure your order."
T+3h:  Reservation released, stock restored, order → CANCELLED
```

---

## 4. Order Management & Fulfillment

### Order State Machine

```
INTENT_CAPTURED
      │ (customer confirms order)
      ▼
PAYMENT_PENDING ──────────────────────────────────────────┐
      │                                                    │
      │ (bank transfer receipt received)                   │ (30 min timeout)
      ▼                                                    ▼
RECEIPT_RECEIVED                                     CANCELLED
      │                                            (stock restored)
      │ (OCR + hash check pass)
      ▼
PAYMENT_VERIFIED  ←── (Paystack/Flutterwave webhook fires)
      │
      │ (merchant or auto)
      ▼
PROCESSING
      │
      │ (merchant marks dispatched)
      ▼
DISPATCHED ──→ WhatsApp notification to customer
      │
      │ (merchant marks delivered or customer confirms)
      ▼
DELIVERED
      │
      ▼
COMPLETED
```

### Stock Management

| Event | Action |
|-------|--------|
| Order intent captured | `reserved_quantity += qty` |
| Order confirmed + payment pending | Stock held as reserved |
| Payment verified | `stock_quantity -= qty`, `reserved_quantity -= qty` |
| Order cancelled / timed out | `reserved_quantity -= qty` (stock returned) |
| Merchant manual edit | Direct update to `stock_quantity` |

### Fraud Detection (Fake Receipt)

```python
# On every receipt image received:
image_hash = md5(image_bytes).hexdigest()

# Check across ALL orders, ALL tenants
existing = db.query(
    "SELECT order_id FROM orders WHERE receipt_hash = %s", [image_hash]
)
if existing:
    flag_as_fraud(order_id, customer_phone)
    notify_merchant(tenant_id)
    reply_customer("This receipt has already been used.")
else:
    db.execute("UPDATE orders SET receipt_hash = %s WHERE id = %s", [image_hash, order_id])
    proceed_to_ocr_verification()
```

### Customer Follow-Up (3-Touch Protocol)

```python
FOLLOW_UP_SCHEDULE = [
    (30,  "minutes", "gentle reminder"),
    (120, "minutes", "urgency — expiring soon"),
    (180, "minutes", "reservation released, invite to reorder"),
]
```

### Dispatch & Delivery Notifications

When merchant clicks "Mark Dispatched" in portal:
```
WhatsApp → Customer:
"Your order #ORD-0091 has been dispatched! 🚚
Courier: GIG Express
Tracking: GIG123456789
Expected delivery: [date]

Reply DELIVERED when you receive it."
```

When customer replies "DELIVERED" or merchant marks delivered:
```
WhatsApp → Customer:
"Thank you for your order! 🎉
We'd love your feedback. How was your experience?
Reply 1-5 ⭐"
```

---

## 5. Payment Auto-Confirmation

### Architecture Principle

One `auto_confirm_order()` function serves both gateways. Gateway-specific webhook handlers do verification, then call the shared function.

### Paystack Integration

**Webhook endpoint:** `POST /webhooks/paystack/{merchant_id}`

```python
def paystack_webhook(merchant_id: str):
    # 1. Load merchant secret key (AES-256 decrypted)
    secret = decrypt_key(get_merchant_paystack_secret(merchant_id))
    
    # 2. Verify HMAC-SHA512 signature
    payload = request.get_data()
    expected = hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()
    if request.headers.get('x-paystack-signature') != expected:
        return 403  # Reject immediately
    
    # 3. Parse event
    event = request.get_json()
    if event['event'] != 'charge.success':
        return 200  # Acknowledge but ignore
    
    # 4. Double-verify via Paystack API
    ref = event['data']['reference']
    verify = requests.get(
        f"https://api.paystack.co/transaction/verify/{ref}",
        headers={"Authorization": f"Bearer {secret}"}
    ).json()
    
    if verify['data']['status'] != 'success':
        return 200  # Already handled or failed
    
    amount_naira = verify['data']['amount'] / 100  # Paystack sends kobo
    order_ref = verify['data']['metadata'].get('order_id')
    
    # 5. Confirm order
    auto_confirm_order(
        order_id=order_ref,
        amount_paid=amount_naira,
        gateway='paystack',
        gateway_ref=ref,
        tenant_id=merchant_id
    )
    return 200
```

### Flutterwave Integration

**Webhook endpoint:** `POST /webhooks/flutterwave/{merchant_id}`

```python
def flutterwave_webhook(merchant_id: str):
    # 1. Verify hash header
    secret = decrypt_key(get_merchant_flutterwave_secret(merchant_id))
    if request.headers.get('verif-hash') != secret:
        return 403
    
    # 2. Parse event
    event = request.get_json()
    if event.get('event') != 'charge.completed':
        return 200
    
    # 3. Double-verify via Flutterwave API
    tx_id = event['data']['id']
    verify = requests.get(
        f"https://api.flutterwave.com/v3/transactions/{tx_id}/verify",
        headers={"Authorization": f"Bearer {secret}"}
    ).json()
    
    if verify['data']['status'] != 'successful':
        return 200
    
    amount_naira = verify['data']['amount']  # Flutterwave sends Naira directly
    order_ref = verify['data']['meta'].get('order_id')
    
    # 4. Confirm order
    auto_confirm_order(
        order_id=order_ref,
        amount_paid=amount_naira,
        gateway='flutterwave',
        gateway_ref=str(tx_id),
        tenant_id=merchant_id
    )
    return 200
```

### Shared Confirmation Function

```python
def auto_confirm_order(order_id, amount_paid, gateway, gateway_ref, tenant_id):
    order = db.get_order(order_id, tenant_id)
    
    if not order or order['status'] not in ('PAYMENT_PENDING', 'RECEIPT_RECEIVED'):
        return  # Already confirmed or cancelled
    
    if amount_paid < order['total_amount']:
        flag_underpayment(order_id, amount_paid)
        notify_merchant_underpayment(tenant_id, order_id, amount_paid)
        return
    
    db.update_order(order_id, {
        'status': 'PAYMENT_VERIFIED',
        'payment_gateway': gateway,
        'gateway_reference': gateway_ref,
        'paid_at': utcnow(),
        'amount_paid': amount_paid,
    })
    
    # Decrement stock (was reserved on intent)
    for item in order['items']:
        db.execute(
            "UPDATE products SET stock_quantity = stock_quantity - %s, "
            "reserved_quantity = reserved_quantity - %s WHERE id = %s",
            [item['qty'], item['qty'], item['product_id']]
        )
    
    # Notify customer via WhatsApp
    send_whatsapp(order['customer_phone'],
        f"Payment confirmed ✅ Order #{order['reference']} is now being processed!")
    
    # Notify merchant
    notify_merchant_new_paid_order(tenant_id, order_id)
```

### Webhook Failsafe Polling

Background job every 5 minutes checks all orders in `PAYMENT_PENDING` older than 10 minutes and verifies via API (not just waiting for webhook):

```python
@scheduler.scheduled_job('interval', minutes=5)
def poll_unconfirmed_gateway_orders():
    pending = db.query("""
        SELECT * FROM orders 
        WHERE status = 'PAYMENT_PENDING'
        AND payment_gateway IN ('paystack', 'flutterwave')
        AND created_at < NOW() - INTERVAL '10 minutes'
    """)
    for order in pending:
        verify_via_api(order)
```

### Key Security Rules

- All gateway secret keys encrypted AES-256 at rest before storage
- Merchant ID embedded in webhook URL path — verified before any DB lookup
- Every webhook verified by signature before JSON is parsed
- Every payment double-verified via gateway API after webhook passes
- Idempotency: `gateway_reference` has UNIQUE constraint — duplicate webhooks ignored

---

## 6. Portal Dashboard — portal.phixtra.com

### Decision

**Single portal** — all merchant features in `portal.phixtra.com`. Do not build a separate portal.  
Codebase: `/root/phixtra-app/api-key-manager/` (Flask + Jinja2, port 5055)

A separate consumer portal (for end-shoppers) may be considered in a future phase but is not in scope.

### What Already Exists (Do Not Rebuild)

| Module | Routes |
|--------|--------|
| Auth | register, login, logout, verify email, forgot/reset password |
| Dashboard | KPI summary, trial status banners |
| AI Assistant | API keys, system instruction, handoff rules, verified specs |
| Cart Recovery | overview/settings, email templates |
| Billing | buy credits (Stripe), invoices, card management, subscription |
| Reports | AI usage, cart recovery, billing summary |
| Chat Archive | full conversation history + per-session export |
| WhatsApp | connect (embedded signup), templates, agent inbox, campaigns |
| Settings | profile, password, avatar, notifications, plan, business details |

### Auto-Provisioning

Every merchant gets a portal on signup — whether they signed up via WhatsApp, web, or admin-created.

```python
def provision_merchant_portal(tenant_id: int, merchant_phone: str):
    _ensure_tenant_balance_row(tenant_id)   # already exists
    _provision_orders_table(tenant_id)      # new
    _provision_products_table(tenant_id)    # new
    _schedule_welcome_whatsapp(merchant_phone)  # new
```

Even if the merchant never visits the portal, data accumulates: orders, payments, customers, chat sessions. When they eventually log in, they see full history from day one.

### New Modules to Build

#### 6.1 Orders (`/orders`) — BUILD FIRST

High-traffic, highest merchant value. Every order from WhatsApp appears here.

**Sidebar:** `📦 Orders` — flat link (not grouped)

**List view (`/orders`):**
- KPI row: Total Today, Pending, Paid, Dispatched, Cancelled
- Filterable table: order ref, customer phone, items, amount, status, date
- Status pills: `pill-warn` (pending), `pill-green` (paid/delivered), `pill-grey` (processing/dispatched), `pill-red` (cancelled)
- Export CSV button

**Detail view (`/orders/<order_id>`):**
- Full item list with quantities and prices
- Payment method (bank transfer / Paystack / Flutterwave)
- Receipt image thumbnail (bank transfer)
- Status timeline: PENDING → PAID → PROCESSING → DISPATCHED → DELIVERED
- Merchant actions: Mark Dispatched, Mark Delivered, Cancel Order
- Customer WhatsApp deep-link: `https://wa.me/234...`

**Routes:**
```
GET  /orders                       # paginated list with filters
GET  /orders/<order_id>            # detail view
POST /orders/<order_id>/dispatch   # triggers WhatsApp notification to customer
POST /orders/<order_id>/deliver    # mark delivered
POST /orders/<order_id>/cancel     # cancel + restore stock
```

#### 6.2 Products (`/products`)

Smart detection: if `source_type == 'woocommerce'` → read-only sync view. Otherwise → full CRUD.

**Sidebar:** Under new `🏪 Commerce` group

**No-website merchant CRUD view:**
- Product table: image, name, price, stock, category, edit button
- Stock = 0 rows highlighted amber
- Add product form: name, price, stock, category, description, image upload
- Bulk import: Excel/CSV → AI column mapping preview → confirm

**WooCommerce merchant read-only view:**
- Sync status (live/stale), last sync time, product count
- Out-of-stock count
- "View in WooCommerce ↗" link

**Routes:**
```
GET  /products
GET  /products/add
POST /products/add
GET  /products/<id>/edit
POST /products/<id>/edit
POST /products/<id>/delete
POST /products/import             # bulk Excel/CSV → AI mapping
GET  /products/sync-status        # JSON, polled by JS
```

#### 6.3 Data Sources (`/data-sources`)

**Sidebar:** Under `🏪 Commerce` group

Manages all catalogue ingestion sources in one place:
- Google Sheets: OAuth2 connect, sheet picker, column mapping, sync interval
- Excel/CSV: upload history, re-upload, mapping review
- WooCommerce: REST connect, last sync, webhook status
- Manual: link to `/products/add`

#### 6.4 Customers (`/customers`)

**Sidebar:** Under `🏪 Commerce` group

Every unique WhatsApp number that has interacted = a customer record.

**List view:** phone, first seen, order count, lifetime spend  
**Detail view (`/customers/<phone>`):**
- Full order history
- All chat sessions (links to Chat Archive)
- Total lifetime spend
- Handoff history
- "Send Campaign" shortcut

#### 6.5 Payment Gateways (`/settings/payments`)

**Sidebar:** New `💳 Payment Gateways` flat link

Three sections on one page:
- **Paystack**: public key, secret key (masked, "Show" decrypts for 10s), webhook health indicator (green/amber/red based on last event age)
- **Flutterwave**: same structure
- **Bank Transfer**: bank name, account number, account name — used in AI payment instructions

Webhook health: green if event received <24h ago, amber 24-48h, red >48h.

**Routes:**
```
GET  /settings/payments
POST /settings/payments/paystack
POST /settings/payments/paystack/remove
POST /settings/payments/flutterwave
POST /settings/payments/flutterwave/remove
POST /settings/payments/bank
GET  /settings/payments/webhook-status   # JSON health check
```

#### 6.6 Analytics (`/analytics`)

**Sidebar:** Replace existing `📈 Reports` group → `📈 Analytics` with Overview as new top link, keep sub-links

**Overview KPIs:**
- Revenue (today / this week / this month) with trend
- Conversion rate (orders / conversations)
- Average order value
- AI resolution rate (% conversations resolved without handoff)

**Charts (Chart.js, no build step needed):**
- Revenue bar chart — last 7 days
- Top products by revenue
- Handoff rate trend
- AI response time

Data passed from Jinja2 via `data-*` attributes, initialized in `<script>` block.

### Updated Sidebar Navigation

```
📊  Dashboard
📦  Orders                          ← NEW (flat, high traffic)
💳  Payment Gateways                ← NEW (flat)

🏪  Commerce                        ← NEW GROUP
    Products
    Data Sources
    Customers

🤖  AI Assistant
    API Keys
    System Instruction
    Handoff Rules
    Verified Specs

🛒  Cart Recovery
    Overview & Settings
    Email Templates

📈  Analytics                       ← renamed from Reports
    Overview                        ← NEW
    AI Usage
    Cart Recovery
    Billing Summary

💬  Chat Archive
📱  WhatsApp
    Connect
    Templates
    Agent Inbox
    Campaigns

────────────────────────────
⚙️  Account Settings
🏠  PhiXtra Home ↗
```

### Passive Value — Merchants Who Never Log In

**Daily WhatsApp Summary** (8:00 AM WAT, via `wa_proactive.py`):

```
📊 *Phixtra Daily Report — [Business Name]*

Yesterday's summary:

🛍 Orders: 7 (↑2 from last week)
💰 Revenue: ₦94,500
⏳ Pending payment: 2 orders
✅ Completed: 5 orders

Top seller: Ankara Dress (4 units)
⚠️ Low stock: Lace Blouse (2 left)

Reply ORDERS to manage
View full dashboard: portal.phixtra.com
```

**Weekly Sunday summary** adds: revenue vs prior week, top 5 products, stock alerts for items < 5 units, AI performance (resolution rate, avg response time).

### Business-Type Adaptation

| Business Type | Products Tab | Data Sources Tab | Orders |
|---------------|-------------|-----------------|--------|
| WooCommerce | Read-only sync view | WooCommerce sync status | Full CRUD |
| No-website (manual) | Full CRUD | Excel upload history | Full CRUD |
| Google Sheets | Read-only (sync driven) | Google OAuth + sheet picker | Full CRUD |
| Excel/CSV | Read-only per-upload | Upload history + re-upload | Full CRUD |

Detection: `tenant.source_type` column controls Jinja2 render branch.

### Build Sequence

Build in this order — each phase is independently valuable:

1. **Orders** — highest daily merchant value, feeds from existing order state machine
2. **Payment Gateway Settings** — needed before payments work in production
3. **Sidebar additions** — add new nav entries to `base.html`
4. **Products CRUD** — for no-website merchants only; WooCommerce shows sync view
5. **Analytics overview** — Chart.js, data from existing orders/chat tables
6. **Customers directory** — derived from existing WhatsApp session data
7. **Data Sources page** — consolidates Google Sheets + Excel upload UI
8. **Daily WhatsApp reports** — scheduled job in `wa_proactive.py`

---

## 7. Database Schema Reference

### Core Tables

```sql
-- Tenants (merchants)
CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    business_name   VARCHAR(255),
    source_type     VARCHAR(50) DEFAULT 'manual',  
                    -- 'manual' | 'woocommerce' | 'google_sheets' | 'excel'
    wa_phone_number VARCHAR(20),
    wa_business_id  VARCHAR(100),
    wa_access_token TEXT,          -- encrypted
    domain          VARCHAR(255),
    created_at      TIMESTAMP DEFAULT NOW()
);

-- Products (universal schema — all sources normalize here)
CREATE TABLE products (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID REFERENCES tenants(id),
    name             VARCHAR(255) NOT NULL,
    description      TEXT,
    price            NUMERIC(12,2) NOT NULL,
    stock_quantity   INT DEFAULT 0,
    reserved_quantity INT DEFAULT 0,
    category         VARCHAR(100),
    attributes       JSONB DEFAULT '{}',  -- {"color":"red","size":"14"}
    image_url        TEXT,
    source_ref       VARCHAR(255),       -- WooCommerce product_id, sheet row, etc.
    is_active        BOOLEAN DEFAULT TRUE,
    embedding        VECTOR(1536),       -- pgvector for semantic search
    created_at       TIMESTAMP DEFAULT NOW(),
    updated_at       TIMESTAMP DEFAULT NOW()
);

-- Orders
CREATE TABLE orders (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID REFERENCES tenants(id),
    reference          VARCHAR(20) UNIQUE NOT NULL,  -- ORD-0091
    customer_phone     VARCHAR(20) NOT NULL,
    customer_name      VARCHAR(255),
    delivery_address   TEXT,
    delivery_fee       NUMERIC(10,2) DEFAULT 0,
    total_amount       NUMERIC(12,2) NOT NULL,
    amount_paid        NUMERIC(12,2),
    status             VARCHAR(30) DEFAULT 'INTENT_CAPTURED',
    payment_method     VARCHAR(20),    -- 'bank_transfer' | 'paystack' | 'flutterwave'
    payment_gateway    VARCHAR(20),
    gateway_reference  VARCHAR(255) UNIQUE,  -- prevents duplicate confirmation
    receipt_hash       VARCHAR(64),           -- MD5 of receipt image for fraud detection
    receipt_image_url  TEXT,
    tracking_number    VARCHAR(100),
    courier            VARCHAR(100),
    notes              TEXT,
    paid_at            TIMESTAMP,
    dispatched_at      TIMESTAMP,
    delivered_at       TIMESTAMP,
    created_at         TIMESTAMP DEFAULT NOW(),
    updated_at         TIMESTAMP DEFAULT NOW()
);

-- Order Items
CREATE TABLE order_items (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id    UUID REFERENCES orders(id),
    product_id  UUID REFERENCES products(id),
    product_name VARCHAR(255),     -- snapshot at time of order
    quantity    INT NOT NULL,
    unit_price  NUMERIC(12,2) NOT NULL,
    subtotal    NUMERIC(12,2) NOT NULL
);

-- Payment Gateways (per merchant)
CREATE TABLE payment_gateways (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID REFERENCES tenants(id),
    gateway       VARCHAR(20) NOT NULL,    -- 'paystack' | 'flutterwave'
    public_key    TEXT,
    secret_key    TEXT NOT NULL,           -- AES-256 encrypted
    is_active     BOOLEAN DEFAULT TRUE,
    last_webhook_at TIMESTAMP,
    created_at    TIMESTAMP DEFAULT NOW(),
    UNIQUE(tenant_id, gateway)
);

-- Bank Accounts (per merchant, for bank transfer)
CREATE TABLE merchant_bank_accounts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID REFERENCES tenants(id),
    bank_name      VARCHAR(100) NOT NULL,
    account_number VARCHAR(20) NOT NULL,
    account_name   VARCHAR(255) NOT NULL,
    is_primary     BOOLEAN DEFAULT TRUE
);

-- WhatsApp Sessions
CREATE TABLE wa_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id),
    customer_phone  VARCHAR(20) NOT NULL,
    session_state   VARCHAR(50) DEFAULT 'active',
    handoff_status  VARCHAR(20) DEFAULT 'none',  -- 'none' | 'pending' | 'active' | 'resolved'
    last_message_at TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(tenant_id, customer_phone)
);

-- Onboarding State (for WhatsApp-based merchant onboarding)
CREATE TABLE merchant_onboarding (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wa_phone     VARCHAR(20) UNIQUE NOT NULL,
    state        VARCHAR(50) DEFAULT 'WELCOME',
    collected    JSONB DEFAULT '{}',  -- collects fields as conversation progresses
    tenant_id    UUID REFERENCES tenants(id),
    created_at   TIMESTAMP DEFAULT NOW(),
    updated_at   TIMESTAMP DEFAULT NOW()
);
```

---

## 8. Infrastructure & Services

### Running Services

| Service | Tech | Port | Path |
|---------|------|------|------|
| `phixtra-ai-backend` | FastAPI + Uvicorn | 8000 | `/root/phixtra-app/ai-backend/` |
| `phixtra-api-keys` | Flask + Gunicorn | 5055 | `/root/phixtra-app/api-key-manager/` |
| `phixtra-whatsapp-gateway` | FastAPI + Uvicorn | 8001 | `/root/phixtra-app/whatsapp-gateway/` |
| `phixtra-data-sync` | FastAPI + Uvicorn | 8010 | `/root/phixtra-app/phixtra-data-sync/` |
| `phixtra-index-sync` | Background | — | Azure AI Search → MariaDB |
| `phixtra-portal` | Flask | — | Portal service |

### Key Files

| File | Purpose |
|------|---------|
| `whatsapp-gateway/main.py` | Webhook entrypoint |
| `whatsapp-gateway/meta_sender.py` | Send WhatsApp messages via Meta Cloud API |
| `whatsapp-gateway/meta_webhook.py` | Incoming webhook handling |
| `whatsapp-gateway/tenant_router.py` | Route message to correct merchant |
| `whatsapp-gateway/wa_proactive.py` | Outbound/scheduled messages |
| `whatsapp-gateway/interactive_handler.py` | Button/list message handling |
| `ai-backend/main.py` | AI processing entrypoint |
| `ai-backend/rag.py` | Retrieval-augmented generation |
| `ai-backend/search.py` | Azure AI Search queries |
| `ai-backend/llm.py` | LLM calls (GPT-4.1) |
| `ai-backend/handoff.py` | Human handoff logic |
| `ai-backend/memory_store.py` | Conversation memory |
| `api-key-manager/portal_routes.py` | All portal Flask routes |
| `api-key-manager/templates/portal/base.html` | Sidebar + CSS design system |

### WhatsApp-Specific Files in Gateway

```
whatsapp-gateway/
├── main.py                  # FastAPI app, webhook registration
├── meta_sender.py           # POST to Meta Graph API
├── meta_webhook.py          # Parse incoming webhook payload
├── tenant_router.py         # merchant_id lookup from phone number
├── response_formatter.py    # Format AI reply as WhatsApp message
├── interactive_handler.py   # Button/list/template message handling
├── template_sender.py       # WhatsApp Business template messages
├── wa_db.py                 # Database helpers for gateway
├── wa_proactive.py          # Scheduled/triggered outbound messages
└── message_normalizer.py    # Normalize incoming message types
```

### Environment Variables Required

```bash
# Meta / WhatsApp
META_APP_SECRET=
META_VERIFY_TOKEN=

# AI
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_KEY=
AZURE_SEARCH_ENDPOINT=
AZURE_SEARCH_KEY=

# Database
DB_HOST=
DB_NAME=
DB_USER=
DB_PASSWORD=

# Portal
PORTAL_BASE_URL=https://portal.phixtra.com
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=

# Encryption (for gateway keys)
AES_ENCRYPTION_KEY=    # 32 bytes, base64 encoded

# Google (for Sheets integration)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
```

---

*End of design document. Update this file as decisions are made or features are built.*
