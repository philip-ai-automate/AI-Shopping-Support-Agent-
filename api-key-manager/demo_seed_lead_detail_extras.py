"""
demo_seed_lead_detail_extras.py — fills in the Lead Command Centre gaps on
the EXISTING demo@phixtra.com account (tenant found by email, never
recreated/deleted). Two groups of leads were missing data the Lead page
needs:
  - The 30 "Lead Scoring" showcase leads (business-named: Lagos Foodmart,
    Chioma Fashion, ...) had ZERO WhatsApp messages, ZERO stage history,
    and ZERO notes — every section of their Lead page rendered its empty
    state ("No WhatsApp messages yet" / "No stage moves recorded yet" /
    "No notes yet").
  - The 4 Campaign-Intelligence leads (Ifeoma Nwankwo, Damilola Adebayo,
    Musa Ibrahim, Ngozi Chukwuma from demo_seed_campaign_intel.py) already
    have notes + one stage-history row, but no WhatsApp conversation.

Purely additive and idempotent: for each lead, only fills a section that is
actually empty (checked via a COUNT/notes-blank check before inserting), so
re-running this after other demo work never duplicates rows.

Usage:
    cd /root/phixtra-app/api-key-manager
    ./venv/bin/python3 demo_seed_lead_detail_extras.py
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

DEMO_EMAIL = "demo@phixtra.com"
FAKE_PID   = "772671100000001"   # same fake WhatsApp phone_number_id demo_seed.py uses
STAFF      = ["David", "Amaka", "Tunde", "Ngozi"]
STAGE_ORDER = ["new_lead", "contacted", "qualified", "proposal_sent", "negotiating", "won"]

def days_ago(n):
    return datetime.utcnow() - timedelta(days=n)

def naira(v):
    return f"₦{float(v):,.0f}" if v else "the order"

STEP_MESSAGES = {
    "new_lead": [
        ("inbound",  "Hi, I saw your WhatsApp catalogue, do you deliver to us?"),
        ("outbound", "Hello! Yes we do, thanks for reaching out — what are you looking to order?"),
    ],
    "contacted": [
        ("inbound",  "We usually order in bulk for the business, can you send your price list?"),
        ("outbound", "Sure, sending our current price list now — let me know if anything stands out."),
    ],
    "qualified": [
        ("inbound",  "This looks good, we have a real need for this on a regular basis."),
        ("outbound", "Great to hear — let's put together a proper quote for you."),
    ],
    "proposal_sent": [
        ("outbound", "Here's our proposal — {deal} for the full order, valid for 7 days."),
        ("inbound",  "Received, let us review internally and come back to you."),
    ],
    "negotiating": [
        ("inbound",  "The price is a bit high for us, any room to move?"),
        ("outbound", "We can look at a small discount if the order is confirmed this week."),
    ],
    "won": [
        ("inbound",  "Okay, we're happy to proceed — please send the invoice."),
        ("outbound", "Fantastic! Invoice sent, thank you for your business 🙏"),
    ],
}

STAGE_MOVE_NOTES = {
    "new_lead":      "Lead created — {name} reached out about our products.",
    "contacted":     "Made contact with {name}, gauging interest.",
    "qualified":     "Confirmed {name} has a genuine, ongoing need — qualified.",
    "proposal_sent": "Sent proposal/quote to {name}.",
    "negotiating":   "In active price/terms discussion with {name}.",
    "won":           "Deal closed — {name} confirmed and proceeded.",
}

NOTE_TEMPLATES = [
    "{name} came through as a warm inquiry — following up regularly.",
    "Good potential here — {staff} is handling this one directly.",
    "{name} has ordered similar products before through other channels; worth prioritising.",
    "Keep an eye on {name} — budget cycle suggests a decision within the month.",
]

cur.execute("SELECT tenant_id FROM customers WHERE email=%s", (DEMO_EMAIL,))
row = cur.fetchone()
if not row:
    raise SystemExit(f"❌ {DEMO_EMAIL} not found — run demo_seed.py first.")
tenant_id = row["tenant_id"]
print(f"🏢  Using existing demo tenant_id = {tenant_id}")

cur.execute("SELECT * FROM merchant_pipeline_leads WHERE tenant_id=%s ORDER BY id", (tenant_id,))
leads = cur.fetchall()
print(f"📋  {len(leads)} leads found")

n_hist = n_notes = n_convo = 0

for lead in leads:
    lid   = lead["id"]
    stage = lead["stage"]
    name  = lead["customer_name"]
    phone = lead["whatsapp_number"] or lead["phone"]
    staff = lead["assigned_to"] or STAFF[lid % len(STAFF)]
    idx   = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else None

    # ── Stage History — backfill the full realistic chain up to current stage ──
    if idx is not None:
        cur.execute("SELECT count(*) c FROM merchant_pipeline_stage_history WHERE lead_id=%s", (lid,))
        if cur.fetchone()["c"] == 0:
            steps = STAGE_ORDER[: idx + 1]
            total = len(steps)
            for s_i, s in enumerate(steps):
                from_stage = steps[s_i - 1] if s_i > 0 else None
                ts = days_ago((total - s_i) * 2)
                note = STAGE_MOVE_NOTES[s].format(name=name)
                cur.execute("""
                    INSERT INTO merchant_pipeline_stage_history
                        (lead_id, from_stage, to_stage, changed_by, notes, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (lid, from_stage, s, staff, note, ts))
            n_hist += 1

    # ── Notes ────────────────────────────────────────────────────────────────
    if not (lead["notes"] or "").strip():
        note_text = NOTE_TEMPLATES[lid % len(NOTE_TEMPLATES)].format(name=name, staff=staff)
        cur.execute("UPDATE merchant_pipeline_leads SET notes=%s WHERE id=%s", (note_text, lid))
        n_notes += 1

    # ── WhatsApp Conversation ───────────────────────────────────────────────
    if phone and idx is not None:
        cur.execute("""
            SELECT count(*) c FROM wa_message_log
            WHERE tenant_id=%s AND regexp_replace(customer_phone,'[^0-9]','','g')
                                = regexp_replace(%s,'[^0-9]','','g')
        """, (tenant_id, phone))
        if cur.fetchone()["c"] == 0:
            steps = STAGE_ORDER[: idx + 1]
            total = len(steps)
            for s_i, s in enumerate(steps):
                day_base = (total - s_i) * 2
                for m_i, (direction, text) in enumerate(STEP_MESSAGES[s]):
                    ts = days_ago(day_base) + timedelta(hours=m_i * 3)
                    content = text.format(deal=naira(lead["deal_value"]))
                    cur.execute("""
                        INSERT INTO wa_message_log
                            (tenant_id, phone_number_id, customer_phone, direction,
                             content, message_type, created_at, sent_by_label)
                        VALUES (%s, %s, %s, %s, %s, 'text', %s, %s)
                    """, (tenant_id, FAKE_PID, phone, direction, content, ts,
                          None if direction == "inbound" else staff))
            n_convo += 1

conn.commit()
cur.close()
conn.close()

print()
print("=" * 60)
print("✅  Lead Command Centre demo data filled in!")
print("=" * 60)
print(f"   Stage history added for : {n_hist} leads")
print(f"   Notes added for         : {n_notes} leads")
print(f"   Conversations added for : {n_convo} leads")
print("=" * 60)
