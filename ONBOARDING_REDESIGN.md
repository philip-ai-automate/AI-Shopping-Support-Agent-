# WhatsApp Merchant Onboarding Redesign

**File location:** `/root/phixtra-app/ONBOARDING_REDESIGN.md`
**Created:** 2026-05-26
**Status:** APPROVED — ready to build

---

## What Changed and Why

The original `wa_onboarding.py` required sellers to manually type every product detail
(name, price, stock, description, photo) via WhatsApp — slow and error-prone on mobile.

### Changes approved by user:
1. **Product search from catalogue** — sellers search the Phixtra phone catalogue and pick models
2. **Price recommendation** — system shows catalogue retail price, seller sets their own price
3. **Skip products** — seller can skip product addition during onboarding and add later via portal
4. **Bank collection removed** — `COLLECT_BANK` step removed entirely (not planned yet)
5. **Excel upload** — portal-only feature (WhatsApp cannot process .xlsx); already mostly built in data sources

---

## State Machine

### Kept (unchanged)
| State | Purpose |
|---|---|
| `COLLECT_BIZ_NAME` | Collect business name |
| `COLLECT_CATEGORY` | What they sell |
| `COLLECT_SALES_NUMBER` | Customer-facing WhatsApp number |
| `COMPLETE` | Registration done |

### Removed
| State | Reason |
|---|---|
| `COLLECT_BANK` | Not planned yet — removed entirely |
| `COLLECT_SALES_NUMBER` | WhatsApp Business number is connected via portal Meta settings after registration |

### Kept (manual fallback path only)
| State | Purpose |
|---|---|
| `PRODUCT_NAME` | Manual product name entry |
| `PRODUCT_PRICE` | Manual price entry |
| `PRODUCT_STOCK` | Stock quantity (shared with catalogue path) |
| `PRODUCT_DESC` | Manual description entry |
| `PRODUCT_IMAGE` | Manual photo upload |
| `PRODUCT_CONFIRM` | Confirm and save product |
| `ADD_MORE` | Add another or finish |

### New states
| State | Purpose |
|---|---|
| `PRODUCT_SEARCH` | Seller types a keyword to search the catalogue |
| `PRODUCT_RESULTS` | System shows numbered list of matches; seller picks by number |
| `PRODUCT_PRICE_CAT` | System shows recommended price; seller enters their price |

---

## Full Conversation Flow

```
─────────────────────────────────────────────────────────────────────
ENTRY — merchant sends "SETUP" to the Phixtra onboarding number
─────────────────────────────────────────────────────────────────────
Any other message before SETUP:
SYSTEM: "Hello! This is the Phixtra merchant registration line.
To set up your store, send the word: SETUP"

USER: "SETUP"

─────────────────────────────────────────────────────────────────────
COLLECT_BIZ_NAME
─────────────────────────────────────────────────────────────────────
SYSTEM: "👋 Welcome to Phixtra!
I'll help you set up your AI-powered WhatsApp store.
What's your business name?"

USER: "TechHub Lagos"

─────────────────────────────────────────────────────────────────────
COLLECT_CATEGORY
─────────────────────────────────────────────────────────────────────
SYSTEM: "Love it — TechHub Lagos! 🎉
What do you sell? (e.g. phones, accessories, electronics)"

USER: "phones and accessories"

─────────────────────────────────────────────────────────────────────
PRODUCT_SEARCH  ← entry point for products (WhatsApp number collected via portal)
─────────────────────────────────────────────────────────────────────
SYSTEM: "Now let's add the phones you sell. 📱

Search for a model — e.g. 'iPhone 15' or 'Samsung A54'
Type MANUAL to add a custom product not in our catalogue.
Type SKIP to finish now and add products later from your dashboard."

USER: "iPhone 14"

─────────────────────────────────────────────────────────────────────
PRODUCT_RESULTS  ← after search
─────────────────────────────────────────────────────────────────────
SYSTEM: "Here are matching phones:

1️⃣  iPhone 14 128GB — est. ₦680,000
2️⃣  iPhone 14 256GB — est. ₦750,000
3️⃣  iPhone 14 Plus 128GB — est. ₦720,000

Reply with a number to select.
Type NEW SEARCH to try a different model.
Type MANUAL to add a product not listed."

USER: "1"

─────────────────────────────────────────────────────────────────────
PRODUCT_PRICE_CAT  ← after picking from catalogue
─────────────────────────────────────────────────────────────────────
SYSTEM: "📱 iPhone 14 128GB

💡 Recommended retail price: ₦680,000

What price will YOU charge your customers?
(e.g. 650000 or 650k)"

USER: "660k"

─────────────────────────────────────────────────────────────────────
PRODUCT_STOCK  ← shared by catalogue and manual paths
─────────────────────────────────────────────────────────────────────
SYSTEM: "How many do you have in stock?
(Enter a number or reply UNLIMITED)"

USER: "12"

─────────────────────────────────────────────────────────────────────
PRODUCT_CONFIRM
─────────────────────────────────────────────────────────────────────
SYSTEM: "Product preview:
• Model: iPhone 14 128GB
• Your price: ₦660,000
• Stock: 12

Save this product? Reply YES or NO"

USER: "YES"

─────────────────────────────────────────────────────────────────────
ADD_MORE
─────────────────────────────────────────────────────────────────────
SYSTEM: "✅ iPhone 14 128GB added!
Add another product? Reply YES or NO"

→ YES  loops back to PRODUCT_SEARCH
→ NO   completes onboarding
```

---

## Edge Cases

| Situation | Handling |
|---|---|
| No results from catalogue search | "No match found. Try a different search, or type MANUAL to add it yourself." |
| Documents table empty (before Phase 8 catalogue load) | "Catalogue not available yet. Type MANUAL to add products, or type SKIP." |
| Seller types MANUAL at any product step | Goes to existing manual PRODUCT_NAME → PRODUCT_PRICE → PRODUCT_STOCK → PRODUCT_DESC → PRODUCT_IMAGE → PRODUCT_CONFIRM flow |
| Seller types SKIP | Registration completes, portal link shown, no products saved |
| Seller types NEW SEARCH in PRODUCT_RESULTS | Returns to PRODUCT_SEARCH prompt |
| Seller types NO at PRODUCT_CONFIRM | Returns to PRODUCT_SEARCH |
| Price below recommendation | Allowed — no block. Seller sets their own price. |
| Seller types RESTART at any time | Existing behaviour — clears session, starts over |

---

## Technical Implementation

### Search
```sql
SELECT title, price_min
FROM documents
WHERE to_tsvector('english', title) @@ plainto_tsquery('english', %s)
ORDER BY ts_rank(to_tsvector('english', title), plainto_tsquery('english', %s)) DESC
LIMIT 5
```
- Searches the `documents` table (loaded in Phase 8)
- Returns up to 5 results
- Falls back gracefully if table is empty

### Session storage (in `collected` JSON)
```json
{
  "catalogue_results": [
    {"title": "iPhone 14 128GB", "price_min": 680000},
    {"title": "iPhone 14 256GB", "price_min": 750000},
    {"title": "iPhone 14 Plus 128GB", "price_min": 720000}
  ],
  "current_product": {
    "name": "iPhone 14 128GB",
    "recommended_price": 680000
  }
}
```

### Product saved to
- Table: `products` (tenant-scoped, same table used by the rest of the system)
- Fields set: `id` (uuid), `tenant_id`, `name`, `price`, `stock_quantity`, `category`, `is_active=TRUE`

### Database connection
- Uses `wa_db.get_db_connection()` (already migrated to psycopg2 in Phase 7)

---

## Excel Upload (Portal — post-registration)

- **Location:** Portal → Products page → Import button
- **File types:** `.xlsx`, `.xls`, `.csv`
- **Required columns:** Name, Price
- **Optional columns:** Stock, Category, Description
- **Column mapping:** Flexible — seller maps their column headers to system fields
- **Price formats accepted:** `660000`, `₦660,000`, `660k`
- **Stock blank:** defaults to 0
- **Downloadable template:** blank `.xlsx` with correct headers provided on portal

Status: Existing data sources upload in portal already handles file parsing and column
mapping. Needs to be wired up to the Products page with a downloadable template.

---

## Files to Modify

| File | Change |
|---|---|
| `/root/phixtra-app/whatsapp-gateway/wa_onboarding.py` | New states; remove COLLECT_BANK; add catalogue search; add SKIP |
| `/root/phixtra-app/whatsapp-gateway/wa_db.py` | Add `search_catalogue(keyword)` function |
| Portal products page template | Add Import button + downloadable blank template link |
| `portal_routes.py` | Wire existing data-source upload to Products page |

---

## Build Status

- [x] `wa_db.py` — `search_catalogue()` added (full-text search on documents table)
- [x] `wa_onboarding.py` — rewritten with new states; COLLECT_BANK removed; SKIP added
- [x] `COLLECT_SALES_NUMBER` removed — WhatsApp number connected via portal after registration
- [x] SETUP keyword trigger — onboarding only starts when merchant sends "SETUP"
- [x] `WA_SETUP_PHONE_NUMBER_ID=806773045854725` (+2349018948038) set in whatsapp-gateway `.env`
- [x] `WA_SETUP_ACCESS_TOKEN` set (uses same token as portal OTP sender)
- [ ] Portal products import — wire up + add template download (deferred)
- [ ] Test full flow end-to-end (requires Phase 8 catalogue load for search results)
- [x] Customer shopping journey — steps 3–7 (negotiate, order, payment, confirm, follow-up)
  - [x] `wa_shopping.py` — full state machine (AWAIT_PRODUCT → NEGOTIATING → COLLECT_NAME → COLLECT_DELIVERY → COLLECT_ADDRESS → PAYMENT_PENDING → PAYMENT_REVIEW → COMPLETE)
  - [x] `meta_webhook.py` — routes to shopping flow when active session or order-intent keyword detected
  - [x] Portal `/orders/<id>/verify-payment` — RECEIPT_RECEIVED → PAYMENT_VERIFIED + WA notify
  - [x] Portal dispatch + deliver routes — WA customer notifications on status change

---

## Dependencies

- Phase 8 (catalogue load into `documents` table) should run before or after this —
  the search degrades gracefully if `documents` is empty (falls back to MANUAL prompt).
  Safe to build and deploy now.
