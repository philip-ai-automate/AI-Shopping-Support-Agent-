"""
showcase_seed.py — the made-up business used for app-store screenshots and
for Google Play's reviewers (built 2026-10-25 for the Android app).

    cd /root/phixtra-app/api-key-manager
    venv/bin/python showcase_seed.py            # create (first time) — prints the login
    venv/bin/python showcase_seed.py --reset    # wipe its data and load it fresh

"Cedarline Home", a UK furniture shop selling in pounds. Like the staff
training account (training_seed.py) it is is_demo + is_training: kept out of
customer reports, email sending blocked, training banner shown. On top of
that it has WhatsApp chats, products and orders so every main page has
something to show.

Nothing can reach a real person:
  - phone numbers are Ofcom's drama range +44 7700 900xxx (never issued),
  - emails are @example.com (reserved),
  - the WhatsApp "connection" has a placeholder number id and no Meta key,
    so any send is refused by Meta, and jobs that need a real key
    (access_token LIKE 'EAA%') skip it,
  - AI is off, staff alerts are off, the owner gets no hand-over emails.
"""
import argparse, json, secrets, string, sys
from datetime import datetime, timedelta

import psycopg2.extras

from db import get_db_connection
from portal_routes import _link_lead_to_contact, hash_password
from training_seed import wipe_practice_data

SOURCE_TENANT = 19          # copy plan / features / stage names from PhiXtra AI
OWNER_EMAIL = "playreview@phixtra.com"
BUSINESS = "Cedarline Home"
PNID = "showcase-cedarline-0001"     # placeholder WhatsApp number id (never a Meta id)
SHOP_NUMBER = "+44 7700 900100"


def ph(n: int) -> str:
    """Ofcom drama-range UK mobile, digits only, e.g. 447700900123."""
    return f"4477009{n:05d}"[:12] if n < 1000 else sys.exit("drama range is 900000-900999")


def db():
    conn = get_db_connection()
    conn.autocommit = False
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def gen_password() -> str:
    a = string.ascii_letters + string.digits
    return "".join(secrets.choice(a) for _ in range(12))


def create_account(cur):
    cur.execute("SELECT 1 FROM customers WHERE email=%s UNION SELECT 1 FROM team_members WHERE email=%s",
                (OWNER_EMAIL, OWNER_EMAIL))
    if cur.fetchone():
        sys.exit(f"{OWNER_EMAIL} is already registered — run with --reset instead.")
    cur.execute("SELECT * FROM tenants WHERE id=%s", (SOURCE_TENANT,))
    src = cur.fetchone()
    cur.execute("""
        INSERT INTO tenants (name, status, source_type, features, plan_id, billing_cycle, plan_period_start,
                             is_demo, is_training, daily_report_enabled, ai_enabled, crm_enabled,
                             pipeline_stage_labels, lead_score_labels, signup_product, campaign_reply_auto_actions,
                             selling_currency)
        VALUES (%s, 'active', 'whatsapp', %s, %s, %s, %s, TRUE, TRUE, FALSE, FALSE, %s, %s, %s, %s, FALSE, 'GBP')
        RETURNING id
    """, (BUSINESS, (src["features"] if isinstance(src["features"], str) else json.dumps(src["features"] or {})),
          src["plan_id"], src["billing_cycle"], src["plan_period_start"], src["crm_enabled"],
          json.dumps(src["pipeline_stage_labels"] or {}), json.dumps(src["lead_score_labels"] or {}),
          src["signup_product"]))
    tid = cur.fetchone()["id"]
    pw = gen_password()
    cur.execute("""
        INSERT INTO customers (tenant_id, email, password_hash, first_name, last_name, email_verified, is_active,
                               company_name, business_country, timezone, notif_handoff, notif_marketing, created_at)
        VALUES (%s,%s,%s,'Sam','Carter',TRUE,TRUE,%s,'GB','Europe/London',FALSE,FALSE,NOW()) RETURNING id
    """, (tid, OWNER_EMAIL, hash_password(pw), BUSINESS))
    owner_id = cur.fetchone()["id"]
    for first, last in (("Priya", "Shah"), ("Tom", "Reed")):
        cur.execute("""
            INSERT INTO team_members (tenant_id, name, first_name, last_name, email, password_hash, is_active,
                                      invited_by, location_city, location_country, alert_enabled, alert_reminder)
            VALUES (%s,%s,%s,%s,%s,%s,TRUE,%s,'Leeds','GB',FALSE,FALSE)
        """, (tid, f"{first} {last}", first, last, f"{first.lower()}.{last.lower()}@example.com",
              hash_password(gen_password()), owner_id))
    return tid, pw


# ── WhatsApp chats: (customer no., name, minutes ago of last message, assigned staff, messages)
#    messages: (who, text) — who = "c" customer, "ai" AI assistant, or a staff name
CHATS = [
    (101, "Hannah Brooks", 6, None, [
        ("c", "Hi, is the Oslo 3-seater sofa available in the sage green?"),
        ("ai", "Hi Hannah! Yes, the Oslo 3-seater is in stock in sage green at £899. Delivery is 5–7 working days. Would you like me to reserve one?"),
        ("c", "Yes please. Can it come on a Saturday?"),
        ("ai", "Saturday delivery is available for £25. I've reserved one for you. Shall I send the payment link?"),
        ("c", "Perfect, send it over 👍"),
    ]),
    (102, "James Whitfield", 22, "Priya Shah", [
        ("c", "Morning. We're fitting out a new office, 12 desks and chairs. Do you do trade prices?"),
        ("ai", "Good morning James! We do. For 10+ items our sales team can quote trade pricing. I'll pass you to Priya now."),
        ("Priya Shah", "Hi James, Priya here. For 12 Hatton desks and 12 Arlo chairs I can do £6,480 delivered and assembled. Want me to email a quote?"),
        ("c", "That works. Please send it to my email and we'll confirm this week."),
    ]),
    (103, "Olivia Grant", 48, None, [
        ("c", "Do you have the Ember oak dining table in 6-seater?"),
        ("ai", "We do! The Ember oak dining table (6-seater) is £1,150, or £1,595 with six matching chairs."),
        ("c", "Can I see it in the showroom first?"),
        ("ai", "Of course. Our Leeds showroom is open Mon–Sat, 9am–6pm. Would you like to book a time?"),
    ]),
    (104, "Daniel Hughes", 95, "Tom Reed", [
        ("c", "My order CL-1042 hasn't arrived yet, it was due yesterday"),
        ("Tom Reed", "Sorry about that Daniel. I've checked with the courier: it's on the van today and will arrive between 2 and 4pm."),
        ("c", "Great, thanks for checking"),
    ]),
    (105, "Amelia Clarke", 180, None, [
        ("c", "What's the price of the Linden bed frame, king size?"),
        ("ai", "The Linden king-size bed frame is £649. It also comes with a storage base for £749."),
        ("c", "Storage one please. Do you take Klarna?"),
        ("ai", "Yes, you can pay in 3 with Klarna at checkout. Here's your link to order the Linden storage bed."),
    ]),
    (106, "Riverside Hotel", 260, "Priya Shah", [
        ("c", "Hello, we're refurbishing 40 bedrooms in January. Can you supply beds, wardrobes and bedside tables?"),
        ("Priya Shah", "Hello! Yes we can. Could you share room sizes and your budget per room? I'll put a proposal together."),
        ("c", "I'll send the floor plans today. Budget is around £1,500 per room."),
    ]),
    (107, "Noah Patel", 420, None, [
        ("c", "Hi do you deliver to York?"),
        ("ai", "Yes, we deliver across the UK. York delivery is free on orders over £500."),
    ]),
    (108, "Grace Turner", 600, None, [
        ("c", "Is the Bay bookcase in white still on offer?"),
        ("ai", "Yes! The Bay bookcase in white is £179 this week (was £229)."),
        ("c", "Lovely, I'll take two"),
        ("ai", "Great choice. That's £358 for two, with free delivery. Here's your payment link."),
    ]),
    (109, "Liam O'Connor", 1500, "Tom Reed", [
        ("c", "Can I return a chair if the colour doesn't match?"),
        ("Tom Reed", "Yes, you can return any item within 30 days in its original packaging. We'll collect it for free."),
    ]),
    (110, "Sophie Martin", 2900, None, [
        ("c", "Do you have any corner sofas under £1,200?"),
        ("ai", "Yes, two: the Oslo corner sofa at £1,149 and the Bramley corner sofa at £1,099. Both come in 6 colours."),
        ("c", "Thank you, I'll come and see them on Saturday"),
    ]),
]

PRODUCTS = [  # name, category, price, stock, description
    ("Oslo 3-seater sofa", "Sofas", 899, 14, "Deep-seat 3-seater sofa with removable covers. Sage, oat or charcoal."),
    ("Oslo corner sofa", "Sofas", 1149, 6, "Left or right-hand corner sofa with feather-blend cushions."),
    ("Bramley corner sofa", "Sofas", 1099, 8, "Compact corner sofa for smaller living rooms, 6 colours."),
    ("Ember oak dining table (6-seater)", "Dining", 1150, 5, "Solid oak table, 180 x 90 cm."),
    ("Ember dining chair", "Dining", 89, 48, "Oak chair with woven seat."),
    ("Linden king-size bed frame", "Bedroom", 649, 9, "Upholstered king-size bed frame."),
    ("Linden storage bed (king)", "Bedroom", 749, 7, "Linden bed with lift-up storage base."),
    ("Bay bookcase, white", "Storage", 179, 22, "5-shelf bookcase, 180 cm tall."),
    ("Hatton office desk", "Office", 329, 30, "140 cm desk with cable tray."),
    ("Arlo office chair", "Office", 219, 40, "Ergonomic chair with lumbar support."),
    ("Nook bedside table", "Bedroom", 119, 25, "Two-drawer bedside table in oak or white."),
    ("Haven wardrobe", "Bedroom", 599, 6, "Double wardrobe with hanging rail and shelves."),
]

ORDERS = [  # ref, customer no., name, items [(product, qty)], status, days ago, delivery fee
    ("CL-1048", 101, "Hannah Brooks", [("Oslo 3-seater sofa", 1)], "PAYMENT_PENDING", 0, 25),
    ("CL-1047", 108, "Grace Turner", [("Bay bookcase, white", 2)], "PAYMENT_VERIFIED", 0, 0),
    ("CL-1046", 105, "Amelia Clarke", [("Linden storage bed (king)", 1)], "PROCESSING", 1, 0),
    ("CL-1045", 111, "Ethan Lewis", [("Ember oak dining table (6-seater)", 1), ("Ember dining chair", 6)], "DISPATCHED", 2, 0),
    ("CL-1044", 112, "Chloe Evans", [("Nook bedside table", 2)], "DELIVERED", 4, 15),
    ("CL-1043", 113, "Mia Robinson", [("Haven wardrobe", 1)], "RECEIPT_RECEIVED", 1, 0),
    ("CL-1042", 104, "Daniel Hughes", [("Bramley corner sofa", 1)], "DISPATCHED", 3, 0),
    ("CL-1041", 114, "Jack Wood", [("Arlo office chair", 2)], "DELIVERED", 6, 15),
    ("CL-1040", 115, "Isla Walker", [("Linden king-size bed frame", 1)], "CANCELLED", 7, 0),
]

DEALS = [  # name, contact, phone no., stage, value, product interest, assigned, days ago
    ("Riverside Hotel", "Karen Mills", 106, "proposal_sent", 60000, "40 bedroom sets", "Priya Shah", 3),
    ("Whitfield & Co", "James Whitfield", 102, "negotiating", 6480, "12 desks + chairs", "Priya Shah", 1),
    ("Northbank Dental", "Dr Ravi Kumar", 116, "qualified", 4200, "Waiting room seating", "Priya Shah", 6),
    ("Aire Valley Lettings", "Sarah Fox", 117, "contacted", None, "Furnished flats package", "Tom Reed", 2),
    ("The Crown Inn", "Mark Doyle", 118, "won", 8900, "Dining tables and chairs", "Priya Shah", 20),
    ("Studio Nine Design", "Ella Park", 119, "qualified", 3150, "Sofas for client homes", "Tom Reed", 8),
    ("Leeds Co-Work", "Adam Price", 120, "proposal_sent", 12400, "30 desks + chairs", "Priya Shah", 10),
    ("Harrogate B&B", "Lucy Bell", 121, "new_lead", None, "Bedroom refresh", None, 1),
    ("Greenway School", "Mr P. Shaw", 122, "contacted", None, "Staff room furniture", "Tom Reed", 4),
    ("Moorside Care Home", "Joanne Lee", 123, "new_lead", None, "Lounge chairs", None, 0),
    ("Kirkstall Offices", "Ben Hart", 124, "lost", 5200, "Meeting room tables", "Priya Shah", 25),
    ("Olivia Grant", "Olivia Grant", 103, "new_lead", None, "Ember dining set", None, 0),
]


def seed_data(cur, tid: int) -> dict:
    plain = cur.connection.cursor()
    now = datetime.now()
    cur.execute("SELECT id, name FROM team_members WHERE tenant_id=%s", (tid,))
    staff = {r["name"]: r["id"] for r in cur.fetchall()}

    # Placeholder WhatsApp connection (no Meta key — see module notes).
    cur.execute("""
        INSERT INTO wa_tenants (tenant_id, phone_number_id, access_token, verify_token, phixtra_api_key, active,
                                signup_method, display_phone_number, verified_name, ai_enabled, label)
        VALUES (%s,%s,'showcase-no-meta-token',%s,%s,TRUE,'manual',%s,%s,FALSE,'Shop')
    """, (tid, PNID, secrets.token_hex(16), "showcase-" + secrets.token_hex(24), SHOP_NUMBER, BUSINESS))

    for no, name, mins_ago, who, msgs in CHATS:
        phone = ph(no)
        cur.execute("""INSERT INTO wa_contacts (tenant_id, phone, whatsapp_number, display_name, source, status,
                                                addr_city, addr_country, created_at)
                       VALUES (%s,%s,%s,%s,'whatsapp','prospect','Leeds','GB',%s)""",
                    (tid, phone, phone, name, now - timedelta(minutes=mins_ago + 600)))
        t = now - timedelta(minutes=mins_ago + 2 * (len(msgs) - 1))
        for sender, text in msgs:
            if sender == "c":
                direction, mtype, label = "inbound", "text", None
            elif sender == "ai":
                direction, mtype, label = "outbound", "text", None
            else:
                direction, mtype, label = "outbound", "agent_reply", sender
            cur.execute("""INSERT INTO wa_message_log (tenant_id, phone_number_id, customer_phone, direction, content,
                                                       message_type, created_at, sent_by_label)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (tid, PNID, phone, direction, text, mtype, t, label))
            t += timedelta(minutes=2)
        if who:
            cur.execute("""INSERT INTO wa_conversation_assignments (tenant_id, customer_phone, assigned_to_key, assigned_to_label)
                           VALUES (%s,%s,%s,%s)""", (tid, phone, f"team:{staff[who]}", who))

    pid = {}
    for name, cat, price, stock, desc in PRODUCTS:
        cur.execute("""INSERT INTO products (tenant_id, name, description, price, stock_quantity, category, currency, is_active)
                       VALUES (%s,%s,%s,%s,%s,%s,'GBP',TRUE) RETURNING id""", (tid, name, desc, price, stock, cat))
        pid[name] = (cur.fetchone()["id"], price)

    for ref, no, name, items, status, days, fee in ORDERS:
        created = now - timedelta(days=days, hours=2)
        total = sum(pid[p][1] * q for p, q in items) + fee
        paid = status in ("PAYMENT_VERIFIED", "PROCESSING", "DISPATCHED", "DELIVERED")
        cur.execute("""INSERT INTO orders (tenant_id, reference, customer_phone, customer_name, delivery_address,
                                           delivery_fee, total_amount, amount_paid, status, payment_method, currency,
                                           paid_at, dispatched_at, delivered_at, created_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'card','GBP',%s,%s,%s,%s,%s) RETURNING id""",
                    (tid, ref, ph(no), name, "Leeds, United Kingdom", fee, total, total if paid else None, status,
                     created + timedelta(minutes=20) if paid else None,
                     created + timedelta(days=1) if status in ("DISPATCHED", "DELIVERED") else None,
                     created + timedelta(days=3) if status == "DELIVERED" else None, created, created))
        oid = cur.fetchone()["id"]
        for p, q in items:
            cur.execute("""INSERT INTO order_items (order_id, product_id, product_name, quantity, unit_price, subtotal)
                           VALUES (%s,%s,%s,%s,%s,%s)""", (oid, pid[p][0], p, q, pid[p][1], pid[p][1] * q))

    opps = 0
    for name, person, no, stage, value, interest, who, days in DEALS:
        created = now - timedelta(days=days, hours=3)
        outcome = stage if stage in ("won", "lost") else None
        real_stage = {"won": "won", "lost": "negotiating"}.get(stage, stage)
        is_opp = stage != "new_lead"
        opps += is_opp
        cur.execute("""
            INSERT INTO merchant_pipeline_leads
                (tenant_id, customer_name, contact_person, phone, whatsapp_number, email, stage, is_opportunity,
                 opportunity_at, outcome, won_date, dropped_at, dropped_reason, deal_value, currency, product_interest,
                 source, assigned_to, assigned_key, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'GBP',%s,'whatsapp',%s,%s,%s,%s) RETURNING id
        """, (tid, name, person, ph(no), ph(no),
              "hello@" + "".join(ch for ch in name.lower() if ch.isalnum())[:20] + ".example.com",
              real_stage, is_opp, created + timedelta(hours=6) if is_opp else None, outcome,
              (created + timedelta(days=10)).date() if outcome == "won" else None,
              created + timedelta(days=12) if outcome == "lost" else None,
              "Chose a cheaper supplier" if outcome == "lost" else None,
              value, interest, who, f"team:{staff[who]}" if who else None, created, created))
        lid = cur.fetchone()["id"]
        cur.execute("INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, created_at) "
                    "VALUES (%s,NULL,'new_lead','Demo data',%s)", (lid, created))
        if real_stage != "new_lead":
            cur.execute("INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, created_at) "
                        "VALUES (%s,'new_lead',%s,'Demo data',%s)", (lid, real_stage, created + timedelta(hours=6)))
        _link_lead_to_contact(plain, tid, lid)
    return {"chats": len(CHATS), "products": len(PRODUCTS), "orders": len(ORDERS),
            "deals": len(DEALS), "opportunities": opps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe and reload the demo data only")
    a = ap.parse_args()
    conn, cur = db()
    try:
        cur.execute("SELECT tenant_id FROM customers WHERE email=%s", (OWNER_EMAIL,))
        existing = cur.fetchone()
        if a.reset:
            if not existing:
                sys.exit("No showcase account yet — run without --reset first.")
            tid, pw = existing["tenant_id"], None
            wipe_practice_data(cur, tid)   # keeps the account, its logins and staff
        else:
            if existing:
                sys.exit("Showcase account already exists — use --reset to refresh its data.")
            tid, pw = create_account(cur)
        counts = seed_data(cur, tid)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()
    print(f"SHOWCASE_TENANT {tid} {json.dumps(counts)}")
    if pw:
        print(f"LOGIN\t{OWNER_EMAIL}\t{pw}")


if __name__ == "__main__":
    main()
