"""
ambassador_demo.py — Per-ambassador private demo portal tenant.

Each approved ambassador gets their own sandbox: pre-loaded with contacts,
conversations, orders, and usage data so they can explore the portal freely
and show clients what they will experience.
"""
import secrets
import string
import random
import bcrypt
import psycopg2.extras
from datetime import datetime, timedelta, date

from db import get_db_connection


def _pw_hash(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _days_ago(n: int) -> datetime:
    return datetime.utcnow() - timedelta(days=n)


def _hours_ago(n: float) -> datetime:
    return datetime.utcnow() - timedelta(hours=n)


def _teardown_demo(cur, tenant_id: int, customer_id: int):
    """Delete all seeded data for a demo tenant (leaves tenant + customer row intact)."""
    try:
        cur.execute(
            "DELETE FROM order_items WHERE order_id IN "
            "(SELECT id FROM orders WHERE tenant_id=%s)",
            (tenant_id,),
        )
    except Exception:
        pass
    for tbl in [
        "orders",
        "wa_message_log", "wa_contacts", "wa_handoff_state",
        "handoff_requests", "usage_events", "handoff_rules",
        "api_keys", "tenant_balances", "invoices", "wa_tenants",
    ]:
        try:
            cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s", (tenant_id,))
        except Exception:
            pass
    try:
        cur.execute("DELETE FROM onboarding_state WHERE customer_id=%s", (customer_id,))
    except Exception:
        pass


def _seed_demo_data(cur, tenant_id: int, customer_id: int, first_name: str):
    """Populate a demo tenant with realistic shop conversations, orders, and stats."""
    business = f"{first_name}'s Demo Shop"
    site     = first_name.lower().replace(" ", "") + "demo.ng"
    # Each tenant needs a unique phone_number_id — encode tenant_id into it
    fake_pid = f"7726711{tenant_id:08d}"

    # Credit balance
    cur.execute("""
        INSERT INTO tenant_balances (tenant_id, token_balance)
        VALUES (%s, 250000000)
        ON CONFLICT (tenant_id) DO UPDATE SET token_balance = 250000000
    """, (tenant_id,))

    # API key
    plain_key = "px-demo-" + "".join(
        secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24)
    )
    cur.execute("""
        INSERT INTO api_keys
            (tenant_id, api_key_hash, api_key_plain, website, key_type,
             is_active, token_limit, tokens_used,
             trial_activated_at, trial_expires_at, created_at)
        VALUES (%s, %s, %s, %s, 'paid', TRUE,
                -1, 0,
                NOW() - INTERVAL '20 days',
                NOW() + INTERVAL '10 days',
                NOW() - INTERVAL '20 days')
        RETURNING id
    """, (tenant_id, _pw_hash(plain_key), plain_key, site))
    api_key_id = cur.fetchone()["id"]

    # WhatsApp connection
    cur.execute("""
        INSERT INTO wa_tenants
            (tenant_id, phone_number_id, access_token, waba_id,
             verify_token, phixtra_api_key,
             display_phone_number, verified_name, active,
             signup_method, created_at)
        VALUES (%s, %s, 'DEMO_ACCESS_TOKEN_PLACEHOLDER', '123456789012345',
                %s, 'demo-internal-key',
                '+234 801 234 5678', %s, TRUE,
                'embedded', NOW() - INTERVAL '18 days')
    """, (tenant_id, fake_pid, secrets.token_hex(16), business))

    # Contacts
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

    # Conversations
    conversations = [
        ("2348031234567", [
            ("inbound",  "Hello, do you have iPhone 15 Pro Max 256GB?", 72, "text"),
            ("outbound", f"Welcome to {business}! I'm your AI shopping assistant. May I have your name please?", 72, "ai_reply"),
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
            ("outbound", f"Welcome to {business}! I'm your AI shopping assistant. May I have your name please?", 48, "ai_reply"),
            ("inbound",  "Adaeze", 47, "text"),
            ("outbound", "Good morning Ms. Adaeze! Congratulations to your daughter. For a university student I'd recommend the HP Pavilion 15 or the Lenovo IdeaPad 3. Both have 8GB RAM and 512GB SSD. The HP is ₦425,000 and the Lenovo is ₦389,000. Which would you prefer I tell you more about?", 47, "ai_reply"),
            ("inbound",  "Tell me more about the HP", 46, "text"),
            ("outbound", "The HP Pavilion 15 features a 15.6\" Full HD display, Intel Core i5 12th Gen processor, 8GB DDR4 RAM, 512GB NVMe SSD. It's perfect for everyday university tasks. Battery lasts about 8 hours. Price: ₦425,000. Ready to order?", 46, "ai_reply"),
            ("inbound",  "Yes please, how do I pay?", 44, "text"),
            ("outbound", "To place your order, simply reply *ORDER* and I will guide you through the payment steps — we accept bank transfer, card, and USSD!", 43, "ai_reply"),
            ("inbound",  "I want to speak to a human agent please", 42, "text"),
            ("outbound", "Of course Ms. Adaeze! I'm connecting you to one of our agents right away. Please hold on for a moment.", 42, "ai_reply"),
        ]),
        ("2348051234569", [
            ("inbound",  "Do you sell Samsung Galaxy S24 Ultra?", 120, "text"),
            ("outbound", f"Welcome to {business}! I'm your AI shopping assistant. May I have your name please?", 120, "ai_reply"),
            ("inbound",  "Babatunde", 119, "text"),
            ("outbound", "Hello Mr. Babatunde! Yes, we carry the Samsung Galaxy S24 Ultra in Titanium Black and Titanium Gray. Starting at ₦999,000 for 256GB. Are you interested in the 256GB or 512GB variant?", 119, "ai_reply"),
            ("inbound",  "512GB how much?", 118, "text"),
            ("outbound", "The Samsung Galaxy S24 Ultra 512GB is ₦1,149,000. It comes with the S Pen, 200MP camera, and 12GB RAM. Would you like to place an order?", 118, "ai_reply"),
            ("inbound",  "ORDER", 117, "text"),
            ("outbound", f"Perfect Mr. Babatunde! Your order request has been received. Our team will contact you within 2 hours to confirm delivery details. Thank you for choosing {business}!", 116, "ai_reply"),
        ]),
        ("2348091234570", [
            ("inbound",  "Hi, what earbuds do you have?", 168, "text"),
            ("outbound", f"Welcome to {business}! I'm your AI shopping assistant. May I have your name please?", 168, "ai_reply"),
            ("inbound",  "Ngozi", 167, "text"),
            ("outbound", "Hello Ms. Ngozi! We have AirPods Pro 2nd Gen (₦189,000), Samsung Galaxy Buds2 Pro (₦129,000), and JBL Tune 230NC (₦45,000). What's your budget range?", 167, "ai_reply"),
            ("inbound",  "Around 100k", 166, "text"),
            ("outbound", "For ₦100,000 the Sony WF-1000XM4 at ₦95,000 is excellent — industry-leading noise cancellation. Or the Samsung Galaxy Buds2 Pro at ₦129,000 for a bit more. Which sounds good?", 165, "ai_reply"),
            ("inbound",  "I'll go with the Sony", 164, "text"),
            ("outbound", "Excellent choice Ms. Ngozi! The Sony WF-1000XM4 at ₦95,000. To place your order reply *ORDER*!", 163, "ai_reply"),
            ("inbound",  "ORDER", 163, "text"),
            ("outbound", "Order received! Our team will reach out within 2 hours to arrange delivery. Thank you!", 162, "ai_reply"),
        ]),
        ("2347031234571", [
            ("inbound",  "Assalamu alaikum, do you have Dell laptops?", 200, "text"),
            ("outbound", f"Wa alaikum salam! Welcome to {business}. May I have your name please?", 200, "ai_reply"),
            ("inbound",  "Ibrahim", 199, "text"),
            ("outbound", "Good day Mr. Ibrahim! We have the Dell XPS 15 (₦899,000), Dell Inspiron 15 (₦349,000), and Dell Latitude 5420 (₦529,000). Which price range works for you?", 199, "ai_reply"),
            ("inbound",  "The Inspiron, what's the spec?", 198, "text"),
            ("outbound", "The Dell Inspiron 15: Intel Core i5 11th Gen, 8GB RAM, 256GB SSD, 15.6\" HD display, Windows 11. Great for everyday office work at ₦349,000. Want to order?", 197, "ai_reply"),
            ("inbound",  "Yes", 196, "text"),
            ("outbound", "Great Mr. Ibrahim! Reply *ORDER* to place your order and our team will guide you through payment and delivery.", 195, "ai_reply"),
        ]),
        ("2348141234572", [
            ("inbound",  "Hello do you have power banks?", 36, "text"),
            ("outbound", f"Welcome to {business}! I'm your AI shopping assistant. May I have your name please?", 36, "ai_reply"),
            ("inbound",  "Chioma", 35, "text"),
            ("outbound", "Hello Ms. Chioma! Yes we stock Anker and Romoss power banks. The Anker PowerCore 20000 (₦28,000) and Romoss 30000mAh (₦22,000) are our bestsellers. Which capacity do you need?", 35, "ai_reply"),
            ("inbound",  "20000 is fine, I'll take the Anker", 34, "text"),
            ("outbound", "The Anker PowerCore 20000 at ₦28,000 — great choice Ms. Chioma! Reply *ORDER* and our team will confirm your delivery.", 34, "ai_reply"),
            ("inbound",  "ORDER", 33, "text"),
            ("outbound", "Order received! You'll get a confirmation call within 2 hours. Thank you Ms. Chioma!", 32, "ai_reply"),
        ]),
        ("2348061234573", [
            ("inbound",  "Good evening, any deals on PS5?", 5, "text"),
            ("outbound", f"Good evening! Welcome to {business}. May I have your name please?", 5, "ai_reply"),
            ("inbound",  "Seun", 4, "text"),
            ("outbound", "Hello Mr. Seun! We have the PS5 Disc Edition at ₦699,000 and the PS5 Digital Edition at ₦579,000. Both are in stock! Any particular one you're interested in?", 4, "ai_reply"),
            ("inbound",  "The disc one. Any bundle?", 3, "text"),
            ("outbound", "Yes Mr. Seun! PS5 Disc Edition + FIFA 24 + Extra Controller for ₦799,000 — that's ₦100,000 savings versus buying separately. Interested?", 3, "ai_reply"),
            ("inbound",  "Yes that bundle sounds good", 2, "text"),
            ("outbound", "Excellent! Reply *ORDER* and we'll lock in that bundle price for you right away.", 2, "ai_reply"),
            ("inbound",  "But I need help with payment, it's not working", 1, "text"),
            ("outbound", "I'm sorry to hear that Mr. Seun. Let me connect you to a human agent who can help you directly with payment issues.", 1, "ai_reply"),
        ]),
        ("2348171234574", [
            ("inbound",  "Hi, do you sell iPad?", 720, "text"),
            ("outbound", f"Welcome to {business}! May I have your name please?", 720, "ai_reply"),
            ("inbound",  "Fatima", 719, "text"),
            ("outbound", "Hello Ms. Fatima! Yes, we carry the iPad 10th Gen (₦379,000), iPad Air M1 (₦579,000), and iPad Pro M2 (₦879,000). Which are you looking at?", 718, "ai_reply"),
            ("inbound",  "iPad Air please", 717, "text"),
            ("outbound", "The iPad Air M1 has a 10.9\" Liquid Retina display, M1 chip, 8GB RAM, Wi-Fi 6, up to 10 hours battery. Available in 64GB (₦579,000) and 256GB (₦729,000). Which storage?", 716, "ai_reply"),
            ("inbound",  "256GB", 715, "text"),
            ("outbound", "The iPad Air M1 256GB at ₦729,000. To order, reply *ORDER*!", 714, "ai_reply"),
            ("inbound",  "ORDER", 714, "text"),
            ("outbound", f"Order confirmed Ms. Fatima! Delivery within 24–48 hours in Lagos. Our team will call you shortly. Thank you!", 713, "ai_reply"),
        ]),
    ]
    for phone, messages in conversations:
        for direction, content, hrs, mtype in messages:
            cur.execute("""
                INSERT INTO wa_message_log
                    (tenant_id, phone_number_id, customer_phone, direction, content, message_type, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (tenant_id, fake_pid, phone, direction, content, mtype,
                  datetime.utcnow() - timedelta(hours=hrs)))

    # Handoff states
    for phone, escalated, resolved in [
        ("2348121234568", _hours_ago(41),  None),
        ("2348061234573", _hours_ago(0.5), None),
        ("2348051234569", _hours_ago(115), _hours_ago(110)),
    ]:
        cur.execute("""
            INSERT INTO wa_handoff_state
                (tenant_id, customer_phone, session_id, escalated_at, resolved_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (tenant_id, phone, f"sess-{phone[-6:]}-{tenant_id}", escalated, resolved))

    # Handoff requests
    for phone, msg, vname, vemail, created in [
        ("2348121234568", "I want to speak to a human agent please",
         "Adaeze Nwosu",    "adaeze.nwosu@gmail.com", _hours_ago(42)),
        ("2348061234573", "But I need help with payment, it's not working",
         "Seun Adesanya",   "seun.adesanya@yahoo.com", _hours_ago(1)),
        ("2348031234567", "I want to confirm my order status",
         "Chukwuemeka Obi", "",                        _hours_ago(67)),
    ]:
        cur.execute("""
            INSERT INTO handoff_requests
                (tenant_id, session_id, whatsapp_number, visitor_message,
                 visitor_name, visitor_email, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
        """, (tenant_id, f"sess-{phone[-6:]}-{tenant_id}",
              phone, msg, vname, vemail, created))

    # AI usage events — 30 days, deterministic per tenant
    phones = [p for p, _ in contacts]
    rng = random.Random(tenant_id)
    for day_offset in range(30):
        dt = date.today() - timedelta(days=day_offset)
        for _ in range(rng.randint(8, 45)):
            hour   = rng.randint(8, 22)
            minute = rng.randint(0, 59)
            ts     = datetime(dt.year, dt.month, dt.day, hour, minute)
            tokens = rng.randint(800, 3200)
            phone  = rng.choice(phones)
            cur.execute("""
                INSERT INTO usage_events
                    (tenant_id, api_key_id, website, key_type, session_id, used_tokens, created_at)
                VALUES (%s, %s, %s, 'paid', %s, %s, %s)
            """, (tenant_id, api_key_id, site,
                  f"sess-{phone[-6:]}-{day_offset}-{hour}", tokens, ts))

    # Invoices
    for i, (credits, amount, paid_at) in enumerate([
        (5000,  50000,  _days_ago(25)),
        (10000, 100000, _days_ago(18)),
        (10000, 100000, _days_ago(11)),
        (20000, 200000, _days_ago(4)),
    ], 1):
        cur.execute("""
            INSERT INTO invoices
                (invoice_number, tenant_id, customer_id, credits,
                 amount_pence, vat_pence, currency, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'ngn', 'paid', %s)
        """, (f"PHX-{tenant_id}-{i:04d}", tenant_id, customer_id,
              credits, amount, amount // 10, paid_at))

    # Handoff rules
    for text, ttype, sort in [
        ("speak to a human", "visitor_initiated", 1),
        ("human agent",      "visitor_initiated", 2),
        ("speak to someone", "visitor_initiated", 3),
        ("complaint",        "visitor_initiated", 4),
        ("payment issue",    "ai_initiated",      5),
    ]:
        cur.execute("""
            INSERT INTO handoff_rules
                (tenant_id, trigger_text, trigger_type, is_active, sort_order)
            VALUES (%s, %s, %s, TRUE, %s)
        """, (tenant_id, text, ttype, sort))

    # Orders
    orders_data = [
        ("DEMO-10231", "2347031234571", "Ibrahim Musa", "INTENT_CAPTURED",
         [("Dell Inspiron 15", 1, 349000)], 5000, None, 6, None, None, None, None, None),
        ("DEMO-10232", "2348141234572", "Chioma Eze", "PAYMENT_PENDING",
         [("Anker PowerCore 20000", 1, 28000)], 2500, None, 5, None, None, None, None, None),
        ("DEMO-10233", "2348091234570", "Ngozi Okonkwo", "RECEIPT_RECEIVED",
         [("Sony WF-1000XM4", 1, 95000)], 3000, None, 4, None, None, None, None, None),
        ("DEMO-10234", "2348051234569", "Babatunde Fashola", "PAYMENT_VERIFIED",
         [("Samsung Galaxy S24 Ultra 512GB", 1, 1149000)], 5000, None, 3, 3, None, None, None, None),
        ("DEMO-10235", "2348171234574", "Fatima Bello", "DISPATCHED",
         [("iPad Air M1 256GB", 1, 729000)], 4000, None, 6, 5, 2, None, "GIG-2284910", "GIG Logistics"),
        ("DEMO-10236", "2348061234573", "Seun Adesanya", "DELIVERED",
         [("PS5 Disc Edition", 1, 699000), ("FIFA 24", 1, 35000), ("DualSense Controller", 1, 65000)],
         5000, None, 10, 9, 7, 5, "GIG-2271003", "GIG Logistics"),
        ("DEMO-10237", "2348121234568", "Adaeze Nwosu", "CANCELLED",
         [("HP Pavilion 15", 1, 425000)], 5000, "Customer changed their mind.", 4, None, None, None, None, None),
    ]
    for (ref, phone, name, status, items, delivery_fee, notes,
         created_d, paid_d, dispatched_d, delivered_d, tracking, courier) in orders_data:
        subtotal = sum(qty * price for _, qty, price in items)
        total    = subtotal + delivery_fee
        paid_statuses = ("PAYMENT_VERIFIED", "PROCESSING", "DISPATCHED", "DELIVERED")
        cur.execute("""
            INSERT INTO orders
                (tenant_id, reference, customer_phone, customer_name, delivery_fee,
                 total_amount, amount_paid, status, payment_method,
                 tracking_number, courier, notes,
                 paid_at, dispatched_at, delivered_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    'bank_transfer', %s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        """, (
            tenant_id, ref, phone, name, delivery_fee,
            total,
            total if status in paid_statuses else None,
            status,
            tracking, courier, notes,
            _days_ago(paid_d)       if paid_d       is not None else None,
            _days_ago(dispatched_d) if dispatched_d is not None else None,
            _days_ago(delivered_d)  if delivered_d  is not None else None,
            _days_ago(created_d),
        ))
        order_id = cur.fetchone()["id"]
        for product_name, qty, unit_price in items:
            cur.execute("""
                INSERT INTO order_items (order_id, product_name, quantity, unit_price, subtotal)
                VALUES (%s, %s, %s, %s, %s)
            """, (order_id, product_name, qty, unit_price, qty * unit_price))

    # Mark onboarding complete
    cur.execute("""
        INSERT INTO onboarding_state
            (customer_id, wizard_dismissed, wa_wizard_dismissed,
             ai_plugin_confirmed, export_plugin_confirmed,
             sync_configured_confirmed, catalogue_setup_done)
        VALUES (%s, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE)
        ON CONFLICT (customer_id) DO UPDATE SET
            wizard_dismissed=TRUE, wa_wizard_dismissed=TRUE,
            ai_plugin_confirmed=TRUE, export_plugin_confirmed=TRUE,
            sync_configured_confirmed=TRUE, catalogue_setup_done=TRUE
    """, (customer_id,))


def create_ambassador_demo(amb_id: int, first_name: str, ref_code: str) -> dict:
    """
    Create a demo tenant for an ambassador if one doesn't already exist.
    Returns dict: {tenant_id, customer_id, token}.
    Safe to call multiple times — idempotent.
    """
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT demo_tenant_id, demo_token FROM ambassadors WHERE id=%s",
            (amb_id,),
        )
        row = cur.fetchone()
        if row and row["demo_tenant_id"]:
            # Already exists — find the customer
            cur2 = conn.cursor()
            cur2.execute(
                "SELECT id FROM customers WHERE tenant_id=%s AND is_active=TRUE LIMIT 1",
                (int(row["demo_tenant_id"]),),
            )
            cust = cur2.fetchone()
            cur2.close()
            return {
                "tenant_id":   int(row["demo_tenant_id"]),
                "customer_id": int(cust[0]) if cust else None,
                "token":       row["demo_token"],
            }

        # Create tenant
        business   = f"{first_name}'s Demo Shop"
        demo_email = f"demo-{ref_code.lower()}@phixtra.com"
        rand_pw    = "demo-" + secrets.token_hex(12)

        cur2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur2.execute("""
            INSERT INTO tenants (name, plan_id, plan_period_start, billing_cycle, trial_ends_at, is_demo)
            VALUES (%s, 3, %s, 'monthly', NULL, TRUE)
            RETURNING id
        """, (business, date.today().replace(day=1)))
        tenant_id = int(cur2.fetchone()["id"])

        cur2.execute("""
            INSERT INTO customers
                (tenant_id, email, password_hash, first_name, last_name,
                 email_verified, is_active, created_at)
            VALUES (%s, %s, %s, %s, 'Demo', TRUE, TRUE, NOW())
            RETURNING id
        """, (tenant_id, demo_email, _pw_hash(rand_pw), first_name))
        customer_id = int(cur2.fetchone()["id"])

        _seed_demo_data(cur2, tenant_id, customer_id, first_name)

        demo_token = secrets.token_urlsafe(32)
        cur2.execute(
            "UPDATE ambassadors SET demo_tenant_id=%s, demo_token=%s WHERE id=%s",
            (tenant_id, demo_token, amb_id),
        )

        conn.commit()
        cur2.close()
        return {"tenant_id": tenant_id, "customer_id": customer_id, "token": demo_token}

    except Exception as e:
        conn.rollback()
        print(f"⚠️  create_ambassador_demo error: {e}")
        raise
    finally:
        cur.close()
        conn.close()


def reset_ambassador_demo(amb_id: int, first_name: str, ref_code: str) -> dict:
    """
    Wipe and re-seed the ambassador's demo tenant. Tenant + customer rows are kept;
    all seeded data is cleared and recreated fresh.
    Returns same dict as create_ambassador_demo.
    """
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT demo_tenant_id, demo_token FROM ambassadors WHERE id=%s",
            (amb_id,),
        )
        row = cur.fetchone()
        if not row or not row["demo_tenant_id"]:
            conn.close()
            return create_ambassador_demo(amb_id, first_name, ref_code)

        tenant_id  = int(row["demo_tenant_id"])
        demo_token = row["demo_token"]

        cur2 = conn.cursor()
        cur2.execute(
            "SELECT id FROM customers WHERE tenant_id=%s LIMIT 1",
            (tenant_id,),
        )
        cust_row = cur2.fetchone()
        if not cust_row:
            conn.close()
            return create_ambassador_demo(amb_id, first_name, ref_code)
        customer_id = int(cust_row[0])
        cur2.close()

        # Tear down seeded data
        cur3 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _teardown_demo(cur3, tenant_id, customer_id)
        conn.commit()

        # Re-seed
        _seed_demo_data(cur3, tenant_id, customer_id, first_name)
        conn.commit()
        cur3.close()

        return {"tenant_id": tenant_id, "customer_id": customer_id, "token": demo_token}

    except Exception as e:
        conn.rollback()
        print(f"⚠️  reset_ambassador_demo error: {e}")
        raise
    finally:
        cur.close()
        conn.close()
