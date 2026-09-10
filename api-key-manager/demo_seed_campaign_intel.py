"""
demo_seed_campaign_intel.py — Adds WhatsApp Campaign Intelligence sample data
to the EXISTING demo@phixtra.com account (does NOT touch/reset the rest of
the demo tenant — unlike demo_seed.py, this never deletes the tenant/customer).

Usage:
    cd /root/phixtra-app/api-key-manager
    ./venv/bin/python3 demo_seed_campaign_intel.py

What it creates (all additive, safe to re-run — it only clears out its own
previous run first, by campaign name):
  - 20 new WhatsApp Contacts (fresh phone numbers, don't collide with
    demo_seed.py's 8 contacts)
  - 1 WhatsApp Campaign ("September Flash Sale — 20% Off Electronics") with
    all 20 as recipients, spread across every funnel stage:
      2 sent only, 2 failed, 2 delivered, 4 read, 2 replied (neutral),
      2 not interested, 2 interested (sitting in the Needs Review queue),
      2 opportunity (already a Sales Pipeline deal), 2 converted (deal Won)
  - The matching Sales Pipeline deals for the opportunity/converted contacts
  - 2 pending rows in the Needs Review queue, ready to Approve/Reject

Works on both portal.phixtra.com and connect.phixtra.com — same login
(demo@phixtra.com / Demo1234!), same tenant/database, see
project_phixtra_connect_design memory.
"""

import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras

load_dotenv()

conn = psycopg2.connect(
    host=os.getenv("PG_HOST", "localhost"),
    port=int(os.getenv("PG_PORT", "5432")),
    user=os.getenv("PG_USER"),
    password=os.getenv("PG_PASSWORD"),
    dbname=os.getenv("PG_DB"),
)
conn.autocommit = False
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

DEMO_EMAIL     = "demo@phixtra.com"
CAMPAIGN_NAME  = "September Flash Sale — 20% Off Electronics"

def days_ago(n):
    return datetime.utcnow() - timedelta(days=n)

# ── Find the existing demo tenant (never create/delete the tenant itself) ──
cur.execute("SELECT tenant_id FROM customers WHERE email=%s", (DEMO_EMAIL,))
row = cur.fetchone()
if not row:
    raise SystemExit(f"❌ {DEMO_EMAIL} not found — run demo_seed.py first to create the demo account.")
tenant_id = row["tenant_id"]
print(f"🏢  Using existing demo tenant_id = {tenant_id}")

# ── Clear out only this script's own previous run (safe to re-run) ────────
cur.execute("SELECT id FROM wa_campaigns WHERE tenant_id=%s AND name=%s", (tenant_id, CAMPAIGN_NAME))
old = cur.fetchone()
if old:
    print("🧹  Removing previous run's campaign data...")
    old_campaign_id = old["id"]
    cur.execute("SELECT pipeline_lead_id FROM wa_campaign_recipients WHERE campaign_id=%s AND pipeline_lead_id IS NOT NULL", (old_campaign_id,))
    old_lead_ids = [r["pipeline_lead_id"] for r in cur.fetchall()]
    cur.execute("DELETE FROM wa_campaign_reply_reviews WHERE campaign_id=%s", (old_campaign_id,))
    cur.execute("DELETE FROM wa_campaign_recipients WHERE campaign_id=%s", (old_campaign_id,))
    cur.execute("DELETE FROM wa_campaigns WHERE id=%s", (old_campaign_id,))
    if old_lead_ids:
        cur.execute("DELETE FROM merchant_pipeline_leads WHERE id = ANY(%s)", (old_lead_ids,))
conn.commit()

# ── Contacts + their funnel outcome ─────────────────────────────────────────
# (phone, name, recipient_status, reply_text or None, pipeline_stage or None)
PEOPLE = [
    ("2348020000001", "Tunde Bakare",       "sent",      None, None),
    ("2348020000002", "Grace Okoro",        "sent",      None, None),
    ("2348020000003", "Emeka Chukwu",       "failed",    None, None),
    ("2348020000004", "Blessing Yakubu",    "failed",    None, None),
    ("2348020000005", "Yusuf Aliyu",        "delivered", None, None),
    ("2348020000006", "Kemi Adeyemi",       "delivered", None, None),
    ("2348020000007", "Chidinma Okafor",    "read",      None, None),
    ("2348020000008", "Femi Ogunleye",      "read",      None, None),
    ("2348020000009", "Amaka Nwachukwu",    "read",      None, None),
    ("2348020000010", "Suleiman Bello",     "read",      None, None),
    ("2348020000011", "Uche Eze",           "replied",   "What time is your shop open on Saturday?", None),
    ("2348020000012", "Halima Sani",        "replied",   "Can I pay in installments?", None),
    ("2348020000013", "Obinna Igwe",        "not_interested", "No thanks, not interested at this time.", None),
    ("2348020000014", "Zainab Lawal",       "not_interested", "Please remove me, I already bought elsewhere.", None),
    ("2348020000015", "Chinedu Uzo",        "interested_pending", "Yes I'm very interested! What's the best price you can do?", None),
    ("2348020000016", "Aisha Garba",        "interested_pending", "I'm interested, please send me more details.", None),
    ("2348020000017", "Ifeoma Nwankwo",     "opportunity", "Yes definitely interested, please call me to discuss quantity.", "new_lead"),
    ("2348020000018", "Damilola Adebayo",   "opportunity", "I want to place a bulk order, let's talk.", "qualified"),
    ("2348020000019", "Musa Ibrahim",       "converted", "I'll confirm the quantity tomorrow.", "won"),
    ("2348020000020", "Ngozi Chukwuma",     "converted", "Yes, go ahead and send the invoice.", "won"),
]

print("👥  Creating contacts...")
contact_ids = {}
for phone, name, *_ in PEOPLE:
    cur.execute("""
        INSERT INTO wa_contacts (tenant_id, phone, display_name, source, created_at)
        VALUES (%s, %s, %s, 'whatsapp', %s)
        ON CONFLICT (tenant_id, phone) DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING id
    """, (tenant_id, phone, name, days_ago(6)))
    contact_ids[phone] = cur.fetchone()["id"]

print("📣  Creating the campaign...")
cur.execute("""
    INSERT INTO wa_campaigns (tenant_id, name, campaign_type, template_name, language_code,
                               status, total_count, sent_count, completed_at, created_at)
    VALUES (%s, %s, 'broadcast', 'flash_sale_promo', 'en', 'done', %s, %s, %s, %s)
    RETURNING id
""", (tenant_id, CAMPAIGN_NAME, len(PEOPLE), len(PEOPLE) - 2, days_ago(4), days_ago(5)))
campaign_id = cur.fetchone()["id"]

print("📨  Creating recipients across every funnel stage...")
recipient_ids = {}
for phone, name, outcome, reply_text, _stage in PEOPLE:
    sent_at = days_ago(5) + timedelta(hours=1)
    replied_at = days_ago(4) if reply_text else None
    if outcome in ("sent", "failed", "delivered", "read"):
        status = outcome
    else:
        # interested_pending / not_interested / replied / opportunity / converted
        # all reached 'read' first, then a reply moved them further.
        status = "interested" if outcome in ("interested_pending", "opportunity", "converted") else outcome
    error_msg = "Phone number not on WhatsApp" if outcome == "failed" else None
    cur.execute("""
        INSERT INTO wa_campaign_recipients
            (campaign_id, tenant_id, phone, status, error_msg, sent_at, reply_text, replied_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (campaign_id, tenant_id, phone, status, error_msg, sent_at, reply_text, replied_at))
    recipient_ids[phone] = cur.fetchone()["id"]

print("💼  Creating the Sales Pipeline deals for Opportunity/Converted contacts...")
for phone, name, outcome, reply_text, stage in PEOPLE:
    if outcome not in ("opportunity", "converted"):
        continue
    notes = f'Auto-created from a WhatsApp campaign reply ("{CAMPAIGN_NAME}"): "{reply_text}"'
    won_date = days_ago(1).date() if stage == "won" else None
    cur.execute("""
        INSERT INTO merchant_pipeline_leads
            (tenant_id, customer_name, phone, whatsapp_number, notes, stage,
             contact_channel, contact_date, wa_contact_id, won_date, deal_value, source)
        VALUES (%s, %s, %s, %s, %s, %s, 'whatsapp', %s, %s, %s, %s, 'whatsapp')
        RETURNING id
    """, (tenant_id, name, phone, phone, notes, stage, days_ago(4),
          contact_ids[phone], won_date, 145000 if stage == "won" else 85000))
    lead_id = cur.fetchone()["id"]
    cur.execute("""
        INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes, created_at)
        VALUES (%s, NULL, %s, 'WhatsApp Campaign (auto)', %s, %s)
    """, (lead_id, stage, notes, days_ago(4)))
    cur.execute("""
        UPDATE wa_campaign_recipients SET status=%s, pipeline_lead_id=%s WHERE id=%s
    """, (outcome, lead_id, recipient_ids[phone]))

print("📋  Queueing the two pending Needs Review items...")
for phone, name, outcome, reply_text, _stage in PEOPLE:
    if outcome != "interested_pending":
        continue
    cur.execute("""
        INSERT INTO wa_campaign_reply_reviews
            (tenant_id, campaign_id, recipient_id, phone, reply_text, sentiment, confidence, status, created_at)
        VALUES (%s, %s, %s, %s, %s, 'interested', 0.88, 'pending', %s)
    """, (tenant_id, campaign_id, recipient_ids[phone], phone, reply_text, days_ago(4)))

conn.commit()
cur.close()
conn.close()

print()
print("=" * 60)
print("✅  Campaign Intelligence demo data added!")
print("=" * 60)
print(f"   Login    : demo@phixtra.com / Demo1234!")
print(f"   Works on : https://portal.phixtra.com and https://connect.phixtra.com")
print(f"   Campaign : {CAMPAIGN_NAME}")
print(f"   See it at: WhatsApp Campaigns → Reports → {CAMPAIGN_NAME}")
print(f"   Review at: WhatsApp Campaigns → Needs Review (2 pending items)")
print("=" * 60)
