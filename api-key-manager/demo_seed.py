"""
demo_seed.py — Populate the demo@phixtra.com tenant account with realistic data.

Usage:
    cd /root/phixtra-app/api-key-manager
    python demo_seed.py

What it creates:
  - A fresh tenant + customer (demo@phixtra.com / Demo1234!)
  - Active WhatsApp connection (fake but valid-looking)
  - 8 contacts with realistic Nigerian names + phones
  - 60+ WhatsApp conversations spanning 30 days
  - 3 pending handoff requests on the dashboard
  - 30 days of AI usage events (usage chart)
  - ₦50,000 credit balance
  - 4 invoices (paid)
  - 5 handoff rules
  - 7 orders spanning every status (pending through delivered + 1 cancelled)
  - Onboarding marked complete
  - API key (active, trial)

Run it again to reset: it deletes the demo tenant first, then re-creates everything.
"""

import os, sys, secrets, string, bcrypt
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras

load_dotenv()

# ── Connection ────────────────────────────────────────────────────────────────
conn = psycopg2.connect(
    host=os.getenv("PG_HOST", "localhost"),
    port=int(os.getenv("PG_PORT", "5432")),
    user=os.getenv("PG_USER"),
    password=os.getenv("PG_PASSWORD"),
    dbname=os.getenv("PG_DB"),
)
conn.autocommit = False
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

DEMO_EMAIL    = "demo@phixtra.com"
DEMO_PASSWORD = "Demo1234!"
DEMO_BUSINESS = "TechMart Nigeria"

def pw_hash(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

def days_ago(n: int) -> datetime:
    return datetime.utcnow() - timedelta(days=n)

def hours_ago(n: int) -> datetime:
    return datetime.utcnow() - timedelta(hours=n)

# ── 1. Remove any existing demo data ─────────────────────────────────────────
print("🧹  Cleaning up old demo data...")
cur.execute("SELECT id FROM customers WHERE email=%s", (DEMO_EMAIL,))
old_cust = cur.fetchone()
if old_cust:
    old_cid = old_cust["id"]
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (old_cid,))
    old_tid_row = cur.fetchone()
    if old_tid_row:
        old_tid = old_tid_row["tenant_id"]
        try:
            cur.execute("DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE tenant_id=%s)", (old_tid,))
        except Exception:
            conn.rollback()
        for tbl in [
            "orders",
            "wa_message_log", "wa_contacts", "wa_handoff_state",
            "handoff_requests", "usage_events", "handoff_rules",
            "api_keys", "tenant_balances", "invoices", "wa_tenants",
        ]:
            try:
                cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s", (old_tid,))
            except Exception:
                conn.rollback()
        try:
            cur.execute("DELETE FROM onboarding_state WHERE customer_id=%s", (old_cid,))
        except Exception:
            conn.rollback()
        cur.execute("DELETE FROM tenants WHERE id=%s", (old_tid,))
    cur.execute("DELETE FROM customers WHERE email=%s", (DEMO_EMAIL,))
conn.commit()
print("   Done.")

# ── 2. Create tenant ──────────────────────────────────────────────────────────
print("🏢  Creating demo tenant...")
cur.execute("""
    INSERT INTO tenants (name, plan_id, plan_period_start, billing_cycle, trial_ends_at)
    VALUES (%s, 3, %s, 'monthly', NULL)
    RETURNING id
""", (DEMO_BUSINESS, date.today().replace(day=1)))
tenant_id = cur.fetchone()["id"]
print(f"   tenant_id = {tenant_id}")

# ── 3. Create customer ────────────────────────────────────────────────────────
print("👤  Creating demo customer...")
cur.execute("""
    INSERT INTO customers (tenant_id, email, password_hash, first_name, last_name,
                           email_verified, is_active, created_at)
    VALUES (%s, %s, %s, 'Demo', 'Account', TRUE, TRUE, NOW())
    RETURNING id
""", (tenant_id, DEMO_EMAIL, pw_hash(DEMO_PASSWORD)))
customer_id = cur.fetchone()["id"]
print(f"   customer_id = {customer_id}")

# ── 4. Credit balance ─────────────────────────────────────────────────────────
print("💰  Setting credit balance...")
cur.execute("""
    INSERT INTO tenant_balances (tenant_id, token_balance)
    VALUES (%s, 250000000)
    ON CONFLICT (tenant_id) DO UPDATE SET token_balance = 250000000
""", (tenant_id,))

# ── 5. API key ────────────────────────────────────────────────────────────────
print("🔑  Creating API key...")
plain_key = "px-demo-" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))
cur.execute("""
    INSERT INTO api_keys (tenant_id, api_key_hash, api_key_plain, website, key_type,
                          is_active, token_limit, tokens_used,
                          trial_activated_at, trial_expires_at, created_at)
    VALUES (%s, %s, %s, 'techmart.ng', 'paid', TRUE,
            -1, 0, NOW() - INTERVAL '20 days', NOW() + INTERVAL '10 days', NOW() - INTERVAL '20 days')
    RETURNING id
""", (tenant_id, pw_hash(plain_key), plain_key))
api_key_id = cur.fetchone()["id"]

# ── 6. WhatsApp connection ────────────────────────────────────────────────────
print("📱  Inserting WhatsApp connection...")
cur.execute("""
    INSERT INTO wa_tenants (tenant_id, phone_number_id, access_token, waba_id,
                            verify_token, phixtra_api_key,
                            display_phone_number, verified_name, active,
                            signup_method, created_at)
    VALUES (%s, '772671100000001', 'DEMO_ACCESS_TOKEN_PLACEHOLDER', '123456789012345',
            %s, 'demo-internal-key',
            '+234 801 234 5678', 'TechMart Nigeria', TRUE,
            'embedded', NOW() - INTERVAL '18 days')
""", (tenant_id, secrets.token_hex(16)))

# ── 7. Contacts ───────────────────────────────────────────────────────────────
print("👥  Creating contacts...")
contacts = [
    ("2348031234567", "Chukwuemeka Obi"),
    ("2348121234568", "Adaeze Nwosu"),
    ("2348051234569", "Babatunde Fashola"),
    ("2348091234570", "Ngozi Okonkwo"),
    ("2347031234571", "Ibrahim Musa"),
    ("2348141234572", "Chioma Eze"),
    ("2348061234573", "Seun Adesanya"),
    ("2348171234574", "Fatima Bello"),
]
for phone, name in contacts:
    cur.execute("""
        INSERT INTO wa_contacts (tenant_id, phone, display_name, notes, created_at)
        VALUES (%s, %s, %s, '', NOW() - INTERVAL '15 days')
        ON CONFLICT DO NOTHING
    """, (tenant_id, phone, name))

# ── 8. WhatsApp conversations (30 days of messages) ──────────────────────────
print("💬  Seeding WhatsApp conversations...")

FAKE_PID = "772671100000001"

conversations = [
    # (phone, [(direction, content, hours_ago_value, message_type), ...])
    ("2348031234567", [
        ("inbound",  "Hello, do you have iPhone 15 Pro Max 256GB?", 72, "text"),
        ("outbound", "Welcome to TechMart Nigeria! I'm your AI shopping assistant. May I have your name please?", 72, "ai_reply"),
        ("inbound",  "My name is Chukwuemeka", 71, "text"),
        ("outbound", "Thank you Mr. Chukwuemeka! Yes, we have the iPhone 15 Pro Max 256GB in Black Titanium and Natural Titanium. It's priced at ₦1,249,000. Would you like to know more?", 71, "ai_reply"),
        ("inbound",  "What about 512GB?", 71, "text"),
        ("outbound", "The iPhone 15 Pro Max 512GB is available at ₦1,449,000. Both storage variants are in stock. Shall I help you place an order? Just reply ORDER when ready!", 70, "ai_reply"),
        ("inbound",  "I want to buy the 512GB black one", 70, "text"),
        ("outbound", "Great choice Mr. Chukwuemeka! To place your order, simply reply *ORDER* and I will guide you through the steps!", 69, "ai_reply"),
        ("inbound",  "ORDER", 68, "text"),
    ]),
    ("2348121234568", [
        ("inbound",  "Good morning, I need a laptop for my daughter starting university", 48, "text"),
        ("outbound", "Welcome to TechMart Nigeria! I'm your AI shopping assistant. May I have your name please?", 48, "ai_reply"),
        ("inbound",  "Adaeze", 47, "text"),
        ("outbound", "Good morning Ms. Adaeze! Congratulations to your daughter. For a university student I'd recommend the HP Pavilion 15 or the Lenovo IdeaPad 3. Both have 8GB RAM and 512GB SSD. The HP is ₦425,000 and the Lenovo is ₦389,000. Which would you prefer I tell you more about?", 47, "ai_reply"),
        ("inbound",  "Tell me more about the HP", 46, "text"),
        ("outbound", "The HP Pavilion 15 features a 15.6\" Full HD display, Intel Core i5 12th Gen processor, 8GB DDR4 RAM, 512GB NVMe SSD. It's perfect for note-taking, research, and everyday university tasks. Battery lasts about 8 hours. Price: ₦425,000. Ready to order?", 46, "ai_reply"),
        ("inbound",  "Does it come with Microsoft Office?", 45, "text"),
        ("outbound", "Great question Ms. Adaeze! The HP Pavilion 15 comes with a trial version of Microsoft Office. You can activate a full student license for free using your daughter's university email. Would you like to go ahead with the order?", 44, "ai_reply"),
        ("inbound",  "Yes please, how do I pay?", 44, "text"),
        ("outbound", "To place your order, simply reply *ORDER* and I will guide you through the payment steps — we accept bank transfer, card, and USSD!", 43, "ai_reply"),
        ("inbound",  "I want to speak to a human agent please", 42, "text"),
        ("outbound", "Of course Ms. Adaeze! I'm connecting you to one of our agents right away. Please hold on for a moment.", 42, "ai_reply"),
    ]),
    ("2348051234569", [
        ("inbound",  "Do you sell Samsung Galaxy S24 Ultra?", 120, "text"),
        ("outbound", "Welcome to TechMart Nigeria! I'm your AI shopping assistant. May I have your name please?", 120, "ai_reply"),
        ("inbound",  "Babatunde", 119, "text"),
        ("outbound", "Hello Mr. Babatunde! Yes, we carry the Samsung Galaxy S24 Ultra in Titanium Black and Titanium Gray. Starting at ₦999,000 for 256GB. Are you interested in the 256GB or 512GB variant?", 119, "ai_reply"),
        ("inbound",  "512GB how much?", 118, "text"),
        ("outbound", "The Samsung Galaxy S24 Ultra 512GB is ₦1,149,000. It comes with the S Pen, 200MP camera, and 12GB RAM. Would you like to place an order?", 118, "ai_reply"),
        ("inbound",  "ORDER", 117, "text"),
        ("outbound", "Perfect Mr. Babatunde! Your order request has been received. Our team will contact you within 2 hours to confirm delivery details. Thank you for choosing TechMart Nigeria!", 116, "ai_reply"),
    ]),
    ("2348091234570", [
        ("inbound",  "Hi, what earbuds do you have?", 168, "text"),
        ("outbound", "Welcome to TechMart Nigeria! I'm your AI shopping assistant. May I have your name please?", 168, "ai_reply"),
        ("inbound",  "Ngozi", 167, "text"),
        ("outbound", "Hello Ms. Ngozi! We have a great selection of earbuds: AirPods Pro 2nd Gen (₦189,000), Samsung Galaxy Buds2 Pro (₦129,000), and JBL Tune 230NC (₦45,000). What's your budget range?", 167, "ai_reply"),
        ("inbound",  "Around 100k", 166, "text"),
        ("outbound", "For ₦100,000 I'd recommend the Samsung Galaxy Buds2 Pro at ₦129,000 — slightly above but worth it for the noise cancellation and sound quality. Or the Sony WF-1000XM4 at ₦95,000 which has industry-leading noise cancellation. Which sounds good to you?", 165, "ai_reply"),
        ("inbound",  "I'll go with the Sony", 164, "text"),
        ("outbound", "Excellent choice Ms. Ngozi! The Sony WF-1000XM4 at ₦95,000. To place your order reply *ORDER*!", 163, "ai_reply"),
        ("inbound",  "ORDER", 163, "text"),
        ("outbound", "Order received! Our team will reach out within 2 hours to arrange delivery. Thank you!", 162, "ai_reply"),
    ]),
    ("2347031234571", [
        ("inbound",  "Assalamu alaikum, do you have Dell laptops?", 200, "text"),
        ("outbound", "Wa alaikum salam! Welcome to TechMart Nigeria. May I have your name please?", 200, "ai_reply"),
        ("inbound",  "Ibrahim", 199, "text"),
        ("outbound", "Good day Mr. Ibrahim! We have several Dell models — the Dell XPS 15 (₦899,000), Dell Inspiron 15 (₦349,000), and Dell Latitude 5420 (₦529,000). Which price range works for you?", 199, "ai_reply"),
        ("inbound",  "The Inspiron, what's the spec?", 198, "text"),
        ("outbound", "The Dell Inspiron 15 features: Intel Core i5 11th Gen, 8GB RAM, 256GB SSD, 15.6\" HD display, Windows 11. Great for everyday use and office work at ₦349,000. Want to order?", 197, "ai_reply"),
        ("inbound",  "Yes", 196, "text"),
        ("outbound", "Great Mr. Ibrahim! Reply *ORDER* to place your order and our team will guide you through payment and delivery.", 195, "ai_reply"),
    ]),
    ("2348141234572", [
        ("inbound",  "Hello do you have power banks?", 36, "text"),
        ("outbound", "Welcome to TechMart Nigeria! I'm your AI shopping assistant. May I have your name please?", 36, "ai_reply"),
        ("inbound",  "Chioma", 35, "text"),
        ("outbound", "Hello Ms. Chioma! Yes we stock Anker and Romoss power banks. The Anker PowerCore 20000 (₦28,000) and Romoss 30000mAh (₦22,000) are our bestsellers. Which capacity do you need?", 35, "ai_reply"),
        ("inbound",  "20000 is fine, I'll take the Anker", 34, "text"),
        ("outbound", "The Anker PowerCore 20000 at ₦28,000 — great choice Ms. Chioma! Reply *ORDER* and our team will confirm your delivery.", 34, "ai_reply"),
        ("inbound",  "ORDER", 33, "text"),
        ("outbound", "Order received! You'll get a confirmation call within 2 hours. Thank you Ms. Chioma! 😊", 32, "ai_reply"),
    ]),
    ("2348061234573", [
        ("inbound",  "Good evening, any deals on PS5?", 5, "text"),
        ("outbound", "Good evening! Welcome to TechMart Nigeria. May I have your name please?", 5, "ai_reply"),
        ("inbound",  "Seun", 4, "text"),
        ("outbound", "Hello Mr. Seun! We have the PS5 Disc Edition at ₦699,000 and the PS5 Digital Edition at ₦579,000. Both are in stock! Any particular one you're interested in?", 4, "ai_reply"),
        ("inbound",  "The disc one. Any bundle?", 3, "text"),
        ("outbound", "Yes Mr. Seun! We have a bundle: PS5 Disc Edition + FIFA 24 + Extra Controller for ₦799,000. That's ₦100,000 savings versus buying separately. Interested?", 3, "ai_reply"),
        ("inbound",  "Yes that bundle sounds good", 2, "text"),
        ("outbound", "Excellent choice! Reply *ORDER* and we'll lock in that bundle price for you right away.", 2, "ai_reply"),
        ("inbound",  "But I need help with payment, it's not working", 1, "text"),
        ("outbound", "I'm sorry to hear that Mr. Seun. Let me connect you to a human agent who can help you directly with payment issues.", 1, "ai_reply"),
    ]),
    ("2348171234574", [
        ("inbound",  "Hi, do you sell iPad?", 720, "text"),
        ("outbound", "Welcome to TechMart Nigeria! May I have your name please?", 720, "ai_reply"),
        ("inbound",  "Fatima", 719, "text"),
        ("outbound", "Hello Ms. Fatima! Yes, we carry the iPad 10th Gen (₦379,000), iPad Air M1 (₦579,000), and iPad Pro M2 (₦879,000). Which are you looking at?", 718, "ai_reply"),
        ("inbound",  "iPad Air please", 717, "text"),
        ("outbound", "The iPad Air M1 has a 10.9\" Liquid Retina display, M1 chip, 8GB RAM, Wi-Fi 6, up to 10 hours battery. Available in 64GB (₦579,000) and 256GB (₦729,000). Which storage?", 716, "ai_reply"),
        ("inbound",  "256GB", 715, "text"),
        ("outbound", "The iPad Air M1 256GB at ₦729,000. To order, reply *ORDER*!", 714, "ai_reply"),
        ("inbound",  "ORDER", 714, "text"),
        ("outbound", "Order confirmed Ms. Fatima! Delivery within 24–48 hours in Lagos. Our team will call you shortly. Thank you!", 713, "ai_reply"),
    ]),
]

for phone, messages in conversations:
    for direction, content, hrs, mtype in messages:
        cur.execute("""
            INSERT INTO wa_message_log
                (tenant_id, phone_number_id, customer_phone, direction, content, message_type, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (tenant_id, FAKE_PID, phone, direction, content, mtype,
              datetime.utcnow() - timedelta(hours=hrs)))

# ── 9. Handoff states ─────────────────────────────────────────────────────────
print("🔀  Creating handoff states...")
# Adaeze and Seun requested human agent — create handoff state entries
handoff_convos = [
    ("2348121234568", hours_ago(41), None),   # Adaeze — still open
    ("2348061234573", hours_ago(0.5), None),  # Seun — open (recent)
    ("2348051234569", hours_ago(115), hours_ago(110)),  # Babatunde — resolved
]
for phone, escalated, resolved in handoff_convos:
    cur.execute("""
        INSERT INTO wa_handoff_state
            (tenant_id, customer_phone, session_id, escalated_at, resolved_at)
        VALUES (%s, %s, %s, %s, %s)
    """, (tenant_id, phone,
          f"sess-{phone[-6:]}-{tenant_id}",
          escalated, resolved))

# ── 10. Pending handoff requests (dashboard panel) ────────────────────────────
print("🚨  Creating handoff requests...")
handoff_reqs = [
    ("2348121234568", "I want to speak to a human agent please",
     "Adaeze Nwosu", "adaeze.nwosu@gmail.com", hours_ago(42)),
    ("2348061234573", "But I need help with payment, it's not working",
     "Seun Adesanya", "seun.adesanya@yahoo.com", hours_ago(1)),
    ("2348031234567", "I want to confirm my order status",
     "Chukwuemeka Obi", "", hours_ago(67)),
]
for phone, msg, vname, vemail, created in handoff_reqs:
    cur.execute("""
        INSERT INTO handoff_requests
            (tenant_id, session_id, whatsapp_number, visitor_message,
             visitor_name, visitor_email, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
    """, (tenant_id, f"sess-{phone[-6:]}-{tenant_id}",
          phone, msg, vname, vemail, created))

# ── 11. AI usage events (30 days) ─────────────────────────────────────────────
print("📊  Seeding AI usage events...")
import random
random.seed(42)
for day_offset in range(30):
    dt = date.today() - timedelta(days=day_offset)
    msgs_today = random.randint(8, 45)
    for _ in range(msgs_today):
        hour = random.randint(8, 22)
        minute = random.randint(0, 59)
        ts = datetime(dt.year, dt.month, dt.day, hour, minute)
        tokens = random.randint(800, 3200)
        phone = random.choice([p for p, _ in contacts])
        cur.execute("""
            INSERT INTO usage_events (tenant_id, api_key_id, website, key_type, session_id, used_tokens, created_at)
            VALUES (%s, %s, 'techmart.ng', 'paid', %s, %s, %s)
        """, (tenant_id, api_key_id, f"sess-{phone[-6:]}-{day_offset}-{hour}", tokens, ts))

# ── 12. Invoices ──────────────────────────────────────────────────────────────
print("🧾  Creating invoices...")
invoice_data = [
    ("PHX-DEMO-0001", 5000,  500_00,  50_00,  "gbp", days_ago(25)),
    ("PHX-DEMO-0002", 10000, 1000_00, 100_00, "gbp", days_ago(18)),
    ("PHX-DEMO-0003", 10000, 1000_00, 100_00, "gbp", days_ago(11)),
    ("PHX-DEMO-0004", 20000, 2000_00, 200_00, "gbp", days_ago(4)),
]
for inv_num, credits, amount, vat, currency, paid_at in invoice_data:
    cur.execute("""
        INSERT INTO invoices
            (invoice_number, tenant_id, customer_id, credits,
             amount_pence, vat_pence, currency, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'paid', %s)
    """, (inv_num, tenant_id, customer_id, credits,
          amount, vat, currency, paid_at))

# ── 13. Handoff rules ─────────────────────────────────────────────────────────
print("⚙️   Creating handoff rules...")
handoff_rules = [
    ("speak to a human",  "visitor_initiated", True,  1),
    ("human agent",       "visitor_initiated", True,  2),
    ("speak to someone",  "visitor_initiated", True,  3),
    ("complaint",         "visitor_initiated", True,  4),
    ("payment issue",     "ai_initiated",      True,  5),
]
for text, ttype, active, order in handoff_rules:
    cur.execute("""
        INSERT INTO handoff_rules (tenant_id, trigger_text, trigger_type, is_active, sort_order)
        VALUES (%s, %s, %s, %s, %s)
    """, (tenant_id, text, ttype, active, order))

# ── 14. Orders ────────────────────────────────────────────────────────────────
print("🧾  Creating orders...")
orders_data = [
    # (reference, phone, name, status, items, delivery_fee, notes,
    #  created_days_ago, paid_days_ago, dispatched_days_ago, delivered_days_ago,
    #  tracking_number, courier)
    ("TM-10231", "2347031234571", "Ibrahim Musa", "INTENT_CAPTURED",
     [("Dell Inspiron 15", 1, 349_000)], 5_000, None,
     6, None, None, None, None, None),
    ("TM-10232", "2348141234572", "Chioma Eze", "PAYMENT_PENDING",
     [("Anker PowerCore 20000", 1, 28_000)], 2_500, None,
     5, None, None, None, None, None),
    ("TM-10233", "2348091234570", "Ngozi Okonkwo", "RECEIPT_RECEIVED",
     [("Sony WF-1000XM4", 1, 95_000)], 3_000, None,
     4, None, None, None, None, None),
    ("TM-10234", "2348051234569", "Babatunde Fashola", "PAYMENT_VERIFIED",
     [("Samsung Galaxy S24 Ultra 512GB", 1, 1_149_000)], 5_000, None,
     3, 3, None, None, None, None),
    ("TM-10235", "2348171234574", "Fatima Bello", "DISPATCHED",
     [("iPad Air M1 256GB", 1, 729_000)], 4_000, None,
     6, 5, 2, None, "GIG-2284910", "GIG Logistics"),
    ("TM-10236", "2348061234573", "Seun Adesanya", "DELIVERED",
     [("PS5 Disc Edition", 1, 699_000), ("FIFA 24", 1, 35_000), ("Extra DualSense Controller", 1, 65_000)], 5_000, None,
     10, 9, 7, 5, "GIG-2271003", "GIG Logistics"),
    ("TM-10237", "2348121234568", "Adaeze Nwosu", "CANCELLED",
     [("HP Pavilion 15", 1, 425_000)], 5_000, "Customer changed their mind — opted for in-store pickup instead.",
     4, None, None, None, None, None),
]
for (ref, phone, name, status, items, delivery_fee, notes,
     created_d, paid_d, dispatched_d, delivered_d, tracking, courier) in orders_data:
    subtotal = sum(qty * price for _, qty, price in items)
    total = subtotal + delivery_fee
    cur.execute("""
        INSERT INTO orders
            (tenant_id, reference, customer_phone, customer_name, delivery_fee,
             total_amount, amount_paid, status, payment_method, tracking_number,
             courier, notes, paid_at, dispatched_at, delivered_at, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'bank_transfer', %s, %s, %s, %s, %s, %s, %s, NOW())
        RETURNING id
    """, (
        tenant_id, ref, phone, name, delivery_fee,
        total, (total if status in ("PAYMENT_VERIFIED", "PROCESSING", "DISPATCHED", "DELIVERED") else None), status,
        tracking, courier, notes,
        days_ago(paid_d) if paid_d is not None else None,
        days_ago(dispatched_d) if dispatched_d is not None else None,
        days_ago(delivered_d) if delivered_d is not None else None,
        days_ago(created_d),
    ))
    order_id = cur.fetchone()["id"]
    for product_name, qty, unit_price in items:
        cur.execute("""
            INSERT INTO order_items (order_id, product_name, quantity, unit_price, subtotal)
            VALUES (%s, %s, %s, %s, %s)
        """, (order_id, product_name, qty, unit_price, qty * unit_price))

# ── 15. Onboarding — mark all steps complete ──────────────────────────────────
print("✅  Marking onboarding complete...")
cur.execute("""
    INSERT INTO onboarding_state
        (customer_id, wizard_dismissed, wa_wizard_dismissed,
         ai_plugin_confirmed, export_plugin_confirmed, sync_configured_confirmed,
         catalogue_setup_done)
    VALUES (%s, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE)
    ON CONFLICT (customer_id) DO UPDATE SET
        wizard_dismissed=TRUE, wa_wizard_dismissed=TRUE,
        ai_plugin_confirmed=TRUE, export_plugin_confirmed=TRUE,
        sync_configured_confirmed=TRUE, catalogue_setup_done=TRUE
""", (customer_id,))

# ── Commit all ────────────────────────────────────────────────────────────────
conn.commit()
cur.close()
conn.close()

print()
print("=" * 60)
print("✅  Demo account seeded successfully!")
print("=" * 60)
print(f"   URL      : https://portal.phixtra.com/login")
print(f"   Email    : {DEMO_EMAIL}")
print(f"   Password : {DEMO_PASSWORD}")
print(f"   Business : {DEMO_BUSINESS}")
print(f"   Tenant ID: {tenant_id}")
print("=" * 60)
