# Phixtra Migration: Azure + MySQL → PostgreSQL + pgvector + OpenAI API

**File location:** `/root/phixtra-app/MIGRATION.md`  
**Started:** 2026-05-26  
**Goal:** Eliminate Azure OpenAI + Azure AI Search costs. Migrate MySQL → PostgreSQL with pgvector for local vector search. Replace AzureOpenAI SDK calls with standard OpenAI API calls.

---

## Services Being Migrated

| Service | Port | Systemd Unit |
|---|---|---|
| ai-backend (FastAPI) | 8000 | phixtra-ai-backend.service |
| phixtra-data-sync (FastAPI) | 8010 | phixtra-data-sync.service |
| phixtra-index (daemon) | — | phixtra-index-sync.service |
| api-key-manager (Flask) | 5000 | phixtra-api-keys.service |
| portal (Flask) | 5055 | phixtra-portal.service |
| whatsapp-gateway (FastAPI) | 8001 | phixtra-whatsapp-gateway.service |

---

## What Is Changing

| From | To | Reason |
|---|---|---|
| Azure OpenAI (gpt-4o-mini) | OpenAI API (gpt-4o-mini) | Remove Azure markup (~20% cheaper) |
| Azure OpenAI (text-embedding-3-small) | OpenAI API (text-embedding-3-small) | Same model, direct billing |
| Azure AI Search | PostgreSQL + pgvector | Eliminate ~$75–250/month Azure Search cost |
| MySQL (ai_support) | PostgreSQL (ai_support) | Required for pgvector; better JSON support |

**WordPress plugins are NOT changed** — they proxy through chat.phixtra.com:8000.

---

## Environment Variables Reference

### OLD (Azure) — keep for rollback reference
```
AZURE_OPENAI_ENDPOINT=https://profitbuyz-openai.openai.azure.com/
AZURE_OPENAI_KEY=<redacted>
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
AZURE_OPENAI_API_VERSION=2024-02-15-preview
AZURE_OPENAI_EMBED_DEPLOYMENT=text-embedding-3-small
AZURE_SEARCH_ENDPOINT=https://profitbuyz-search.search.windows.net
AZURE_SEARCH_KEY=<redacted>
AZURE_SEARCH_API_VERSION=2025-09-01
DB_HOST=localhost / DB_USER=ai_user / DB_NAME=ai_support (MySQL)
```

### NEW (OpenAI + PostgreSQL)
```
OPENAI_API_KEY=<set in each .env — get from platform.openai.com>
OPENAI_EMBED_MODEL=text-embedding-3-small
OPENAI_CHAT_MODEL=gpt-4o-mini
PG_HOST=localhost
PG_PORT=5432
PG_USER=phixtra_pg
PG_PASSWORD=LLt0ejNgB7EpOtylB4dgEYaMlaE7wrb3
PG_DB=ai_support
```

---

## Migration Phases & Status

### PHASE 1 — Install PostgreSQL 14 + pgvector ✅ COMPLETE
- [x] Install postgresql-14 (apt)
- [x] Build + install pgvector v0.8.0 from source (apt package unavailable for pg14)
- [x] Start & enable postgresql service
- [x] Create database user `phixtra_pg`
- [x] Create database `ai_support`
- [x] Enable pgvector extension
- [x] Password stored in `/root/phixtra-app/.pg_password` (chmod 600)

### PHASE 2 — Migrate MySQL Schema + Data → PostgreSQL ✅ COMPLETE
- [x] Dump MySQL ai_support schema → `/root/phixtra-app/mysql_backup_20260526.sql` (safety backup)
- [x] Build accurate `pg_schema.sql` from MySQL `information_schema.COLUMNS` (not assumed columns)
- [x] Apply schema to PostgreSQL (DROP SCHEMA + re-apply to fix first-run column mismatches)
- [x] Write `migrate_data.py` — reads MySQL, writes PostgreSQL, never touches MySQL
- [x] Fix boolean type mismatch (MySQL TINYINT(1) → Python int → PostgreSQL BOOLEAN via `get_bool_cols()`)
- [x] Fix ORDER BY columns for tables with non-`id` PKs (customer_alert_state, onboarding_state, etc.)
- [x] Add `UNIQUE (tenant_id, product_id, email)` constraint to `stock_notifications`
- [x] Verify row counts: **1554 rows migrated, all tables match MySQL**
- [x] `documents` table (pgvector) created and ready — empty until Phase 8

**Row counts verified:**
```
tenants=6, admin_users=2, admins=1, api_keys=6, audit_logs=573,
chat_sessions=91, chat_messages=409, chat_summaries=12,
cart_events=187, abandonment_queue=34, credit_packages=4,
customers=4, handoff_requests=8, handoff_rules=7, recovery_log=18,
plugin_downloads=2, stock_notifications=1, system_settings=1,
tenant_balances=5, trial_reminder_state=2, usage_events=154,
wa_message_log=13, wa_portal_otp=1, wa_proactive_log=3,
wa_product_cache=7, wa_tenants=1, onboarding_state=2
```

### PHASE 3 — Migrate ai-backend ✅ COMPLETE
- [x] Backup original files → `/root/phixtra-app/ai-backend-backup-20260526/`
- [x] `azure_clients.py` → lazy `OpenAI` client (replaces `AzureOpenAI`)
- [x] `llm.py` → `OpenAI` client, `OPENAI_CHAT_MODEL` env var
- [x] `db.py` → `psycopg2` replaces `mysql.connector`
- [x] `auth.py` → `RealDictCursor`, `is_active = TRUE/FALSE`
- [x] `billing.py` → `psycopg2`, `ON CONFLICT DO NOTHING`, `ensure_billing_tables()` is no-op
- [x] `cart_db.py` → `psycopg2`, `RETURNING id`, `INTERVAL '48 hours'`
- [x] `push_db.py` → `psycopg2`, `RETURNING id`
- [x] `memory_store.py` → `psycopg2`, `OpenAI`, `ON CONFLICT DO UPDATE`, fixed `DELETE ... ORDER BY LIMIT`
- [x] `search.py` → full rewrite: pgvector SQL replaces Azure AI Search REST API
- [x] `handoff.py` → `psycopg2`, `RETURNING id`, fixed `UPDATE ... ORDER BY LIMIT` (subquery form)
- [x] `trial_maintenance.py` → `psycopg2`, `is_active=TRUE/FALSE`, `ON CONFLICT DO NOTHING`
- [x] `subscription_maintenance.py` → `psycopg2` cursors, `is_active=FALSE`
- [x] `main.py` → `psycopg2.extras`, `ON CONFLICT DO UPDATE`, `is_active=FALSE`, passes `tenant_id` to search
- [x] `.env` → PG + OpenAI vars added; Azure vars commented out for rollback
- [x] `requirements.txt` → `psycopg2-binary` replaces `mysql-connector-python`
- [x] `psycopg2-binary` installed in venv
- [x] `OPENAI_API_KEY` set in `.env`
- [x] Service restarted — active (running)

### PHASE 4 — Migrate phixtra-data-sync ✅ COMPLETE
- [x] `main.py` → `psycopg2`; Azure Search upsert → pgvector `documents` INSERT/ON CONFLICT
- [x] `_embed_texts()` → standard OpenAI embeddings API
- [x] `.env` → PG + OpenAI vars; Azure vars commented for rollback
- [x] `requirements.txt` → `psycopg2-binary`
- [x] Service restarted — active (running)

### PHASE 5 — Migrate phixtra-index ✅ COMPLETE
- [x] Original `phixtra_sync.py` backed up as `.bak`
- [x] Rewritten as lightweight PostgreSQL health monitor (no more Azure index mapping)
- [x] `.env` → PG vars; Azure vars commented for rollback
- [x] Service restarted — active (running)

### PHASE 6 — Migrate api-key-manager + portal ✅ COMPLETE
- [x] `db.py` → `psycopg2` (`get_db_connection` uses PG_HOST/PG_USER/PG_PASSWORD/PG_DB)
- [x] `portal_migrations.py` → no-op (all tables exist in pg_schema.sql); `_column_exists()` updated for PG
- [x] `app.py` → `psycopg2`; `RealDictCursor`; `RETURNING id`; `is_active=TRUE/FALSE`; `UniqueViolation`
- [x] `trial_jobs.py` → `psycopg2`; `RealDictCursor`; `is_active=TRUE`; `INTERVAL '1 day' * %s`
- [x] `portal_app.py` → `RealDictCursor`; removed `buffered=True`
- [x] `portal_routes.py` → bulk replacements + targeted fixes for all `lastrowid`, `ON DUPLICATE KEY`, `INSERT IGNORE`, `DATE_SUB`, `INTERVAL` patterns
- [x] `portal_admin_routes.py` → same bulk replacements + targeted fixes
- [x] Added missing PG UNIQUE constraints: `saved_payment_methods(stripe_payment_method)`, `wa_templates(tenant_id,template_type)`, `payment_gateways(tenant_id,gateway)`, `products(tenant_id,name)`, `wa_tenants(phone_number_id)`, `plugin_downloads(plugin_key)`
- [x] `.env` → PG vars; MySQL vars commented for rollback
- [x] `requirements.txt` → `psycopg2-binary`
- [x] Both services restarted — HTTP 200 on port 5000 and 5055

### PHASE 7 — Migrate whatsapp-gateway ✅ COMPLETE
- [x] `wa_db.py` → psycopg2; `init_wa_tables()` is no-op; `INSERT IGNORE` → `ON CONFLICT DO NOTHING`; `ON DUPLICATE KEY` → `ON CONFLICT DO UPDATE`; `cursor(dictionary=True)` → `RealDictCursor`
- [x] `tenant_router.py` → `RealDictCursor`
- [x] `wa_daily_report.py` → `RealDictCursor`
- [x] `wa_onboarding.py` → `ON CONFLICT DO UPDATE` for `wa_merchant_onboarding` and `merchant_bank_accounts`; `is_active=TRUE`
- [x] Added UNIQUE constraint `merchant_bank_accounts(tenant_id)`
- [x] `.env` → PG vars; MySQL vars commented for rollback
- [x] `requirements.txt` → `psycopg2-binary`
- [x] Service restarted — active (running), HTTP 200 on port 8001

### PHASE 8 — Load PhiXtra_5000_Phone_Catalogue into pgvector
- [ ] Parse `/home/profitbuyz.com/PhiXtra_5000_Phone_Catalogue.xlsx`
- [ ] Generate embeddings for all 5,000 products (OpenAI text-embedding-3-small)
- [ ] Insert into PostgreSQL `documents` table with vectors
- [ ] Verify search works end-to-end

### PHASE 9 — Decommission Azure
- [ ] Verify all services healthy for 48+ hours
- [ ] Delete Azure AI Search resource
- [ ] Delete Azure OpenAI resource
- [ ] Remove all AZURE_* env vars from all .env files

---

## Key Files

| File | Purpose |
|---|---|
| `/root/phixtra-app/.pg_password` | PostgreSQL password for phixtra_pg (chmod 600) |
| `/root/phixtra-app/pg_schema.sql` | PostgreSQL schema (all 47 tables + documents/pgvector) |
| `/root/phixtra-app/migrate_data.py` | MySQL → PostgreSQL data migration script |
| `/root/phixtra-app/mysql_backup_20260526.sql` | Full MySQL dump (safety backup, 577KB) |
| `/root/phixtra-app/ai-backend-backup-20260526/` | ai-backend files before Phase 3 changes |

---

## Rollback Instructions

If anything breaks before Phase 9:
```bash
# Restore ai-backend originals
cp -r /root/phixtra-app/ai-backend-backup-20260526/* /root/phixtra-app/ai-backend/
systemctl restart phixtra-ai-backend

# Restore data-sync originals (after Phase 4)
cp -r /root/phixtra-app/phixtra-data-sync/backup_pre_migration/* /root/phixtra-app/phixtra-data-sync/
systemctl restart phixtra-data-sync

# Restore index sync originals (after Phase 5)
cp -r /root/phixtra-index/backup_pre_migration/* /root/phixtra-index/
systemctl restart phixtra-index-sync
```

MySQL is untouched throughout — rollback is always available.

---

## Log

| Date | Action | Notes |
|---|---|---|
| 2026-05-26 | Migration file created | Assessment complete, starting Phase 1 |
| 2026-05-26 | Phase 1 complete | PostgreSQL 14 + pgvector v0.8.0 installed from source |
| 2026-05-26 | Phase 2 complete | 1554 rows migrated; all table counts verified against MySQL |
| 2026-05-26 | Phase 3 complete | ai-backend fully migrated; service running |
| 2026-05-26 | Phase 4 complete | phixtra-data-sync fully migrated; service running |
| 2026-05-26 | Phase 5 complete | phixtra-index rewritten as PG health monitor; service running |
| 2026-05-26 | Phase 6 complete | api-key-manager + portal fully migrated; HTTP 200 on ports 5000 and 5055 |
| 2026-05-26 | Phase 7 complete | whatsapp-gateway fully migrated; HTTP 200 on port 8001 |

---

## NEXT STEP

**Phase 8** — Load product catalogue into pgvector:
- Parse `/home/profitbuyz.com/PhiXtra_5000_Phone_Catalogue.xlsx` (5,000 phones, 34 columns)
- Generate OpenAI embeddings (`text-embedding-3-small`, dim=1536)
- Insert into PostgreSQL `documents` table
- Verify vector search works end-to-end via `/chat` endpoint
