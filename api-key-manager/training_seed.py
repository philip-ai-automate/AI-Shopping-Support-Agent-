"""
training_seed.py — a private TRAINING account where a business's staff can
practise without touching the real account (built 2026-10-02 for PhiXtra AI).

    cd /root/phixtra-app/api-key-manager
    venv/bin/python training_seed.py --source 19            # create (first time)
    venv/bin/python training_seed.py --source 19 --reset    # wipe practice data, reload fresh sample data

What it does (first run):
  - New tenant "<source name> – Training": is_demo (kept out of customer
    reports / nudge emails) + is_training (training banner, email sending blocked).
    Same plan, billing cycle, stage names and score labels as the source.
    No WhatsApp number, email domain, Facebook Page or PressOne set up, so
    nothing can reach a real person.
  - Owner login: the source owner's email with "+training".
  - Copies the source's roles (same ticks), departments and positions.
  - One training login per ACTIVE source staff member, same name and role,
    email "<name>+training@<domain>", a one-time password printed at the end.
  - Made-up practice data shaped like a real account (see seed_data()).

--reset keeps the tenant, owner, roles and staff logins (passwords unchanged)
and deletes every other row the training tenant owns, then reloads the data.
It refuses to touch any tenant that isn't flagged is_training.
"""
import argparse, json, random, secrets, string, sys
from datetime import date, datetime, timedelta

import psycopg2.extras

from db import get_db_connection
from portal_routes import _link_lead_to_contact, hash_password

KEEP_TABLES = {   # tenant_id tables a reset must never empty
    "tenants", "customers", "team_members", "tenant_roles",
    "tenant_departments", "tenant_positions", "feature_catalog_seen",
}

FIRST = ["Adaeze", "Chinedu", "Emeka", "Ngozi", "Tunde", "Bisi", "Ifeoma", "Segun", "Kelechi", "Funke",
         "Uche", "Yetunde", "Obinna", "Halima", "Musa", "Aisha", "Femi", "Zainab", "Ikenna", "Bola",
         "Chiamaka", "Kunle", "Amaka", "Ibrahim", "Nkechi", "Tobi", "Efosa", "Ebere", "Sola", "Hauwa"]
LAST = ["Okafor", "Adeyemi", "Nwosu", "Balogun", "Eze", "Ogunleye", "Okonkwo", "Bello", "Adebayo", "Obi",
        "Lawal", "Chukwu", "Afolabi", "Danjuma", "Okeke", "Salami", "Nnamdi", "Oyelaran", "Uzor", "Abubakar"]
BRAND = ["Golden", "Royal", "Prime", "Divine", "Eko", "Naija", "Grace", "Zenith", "Crystal", "Unity",
         "Bright Star", "Supreme", "Heritage", "Kings", "Oasis", "Vintage", "Silver Line", "Mega", "Bluewave", "Harvest",
         "Adeola", "Chuks", "Mama Nkechi", "Ike", "Ola", "Emeka", "Bisola", "Femi", "Halima", "Tunde"]
TRADE = ["Ventures", "Enterprises", "Stores", "Global Services", "Logistics", "Pharmacy", "Fashion House",
         "Electronics", "Foods", "Supermarket", "Hotels", "Autos", "Properties", "Beauty Spa", "Tech Hub",
         "Furniture", "Bakery", "Travels", "Agro Allied", "Building Materials"]
CITY = ["Lagos", "Abuja", "Port Harcourt", "Ibadan", "Kano", "Enugu", "Benin City", "Abeokuta", "Owerri", "Uyo"]
PRODUCTS = ["AI WhatsApp Sales Agent", "WooCommerce AI Assistant", "Dual Agent", "WhatsApp Campaigns",
            "CRM + Sales Pipeline", "Email Campaigns"]
LABELS = ["VIP", "Follow up", "Lagos", "Abuja", "Referral", "Price sensitive"]


def db():
    conn = get_db_connection()
    conn.autocommit = False
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def plus_email(email: str, tag: str = "training") -> str:
    local, _, domain = email.partition("@")
    return f"{local.split('+')[0]}+{tag}@{domain}".lower()


def gen_password() -> str:
    a = string.ascii_letters + string.digits
    return "".join(secrets.choice(a) for _ in range(12))


def create_account(cur, source_id: int) -> tuple:
    cur.execute("SELECT * FROM tenants WHERE id=%s", (source_id,))
    src = cur.fetchone()
    cur.execute("SELECT * FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1", (source_id,))
    owner = cur.fetchone()
    owner_email = plus_email(owner["email"])
    cur.execute("SELECT 1 FROM customers WHERE email=%s UNION SELECT 1 FROM team_members WHERE email=%s",
                (owner_email, owner_email))
    if cur.fetchone():
        sys.exit(f"{owner_email} is already registered — run with --reset instead.")

    cur.execute("""
        INSERT INTO tenants (name, status, source_type, features, plan_id, billing_cycle, plan_period_start,
                             is_demo, is_training, daily_report_enabled, ai_enabled, crm_enabled,
                             pipeline_stage_labels, lead_score_labels, signup_product, campaign_reply_auto_actions)
        VALUES (%s, 'active', %s, %s, %s, %s, %s, TRUE, TRUE, FALSE, FALSE, %s, %s, %s, %s, FALSE)
        RETURNING id
    """, (f"{src['name']} – Training", src["source_type"], json.dumps(src["features"] or {}),
          src["plan_id"], src["billing_cycle"], src["plan_period_start"], src["crm_enabled"],
          json.dumps(src["pipeline_stage_labels"] or {}), json.dumps(src["lead_score_labels"] or {}),
          src["signup_product"]))
    tid = cur.fetchone()["id"]

    owner_pw = gen_password()
    cur.execute("""
        INSERT INTO customers (tenant_id, email, password_hash, first_name, last_name, email_verified, is_active,
                               company_name, business_country, timezone, created_at)
        VALUES (%s, %s, %s, %s, %s, TRUE, TRUE, %s, %s, %s, NOW()) RETURNING id
    """, (tid, owner_email, hash_password(owner_pw), owner["first_name"], owner["last_name"],
          f"{src['name']} – Training", owner.get("business_country"), owner.get("timezone")))
    owner_id = cur.fetchone()["id"]

    # Roles, departments, positions — same names/ticks as the real account.
    role_map, dept_map, pos_map = {}, {}, {}
    cur.execute("SELECT id, name, permissions FROM tenant_roles WHERE tenant_id=%s", (source_id,))
    for r in cur.fetchall():
        cur.execute("INSERT INTO tenant_roles (tenant_id, name, permissions) VALUES (%s,%s,%s) RETURNING id",
                    (tid, r["name"], json.dumps(r["permissions"] or {})))
        role_map[r["id"]] = cur.fetchone()["id"]
    for tbl, m in (("tenant_departments", dept_map), ("tenant_positions", pos_map)):
        cur.execute(f"SELECT id, name FROM {tbl} WHERE tenant_id=%s", (source_id,))
        for r in cur.fetchall():
            cur.execute(f"INSERT INTO {tbl} (tenant_id, name) VALUES (%s,%s) RETURNING id", (tid, r["name"]))
            m[r["id"]] = cur.fetchone()["id"]

    logins = [("Owner", f"{owner['first_name']} {owner['last_name']}".strip(), owner_email, "Business owner", owner_pw)]
    cur.execute("""SELECT tm.*, r.name AS role_name FROM team_members tm LEFT JOIN tenant_roles r ON r.id=tm.role_id
                   WHERE tm.tenant_id=%s AND tm.is_active ORDER BY tm.id""", (source_id,))
    for m in cur.fetchall():
        email = plus_email(m["email"])
        cur.execute("SELECT 1 FROM customers WHERE email=%s UNION SELECT 1 FROM team_members WHERE email=%s", (email, email))
        if cur.fetchone():
            logins.append(("SKIPPED", m["name"], email, "email already used", "")); continue
        pw = gen_password()
        cur.execute("""
            INSERT INTO team_members (tenant_id, name, first_name, last_name, email, password_hash, is_active,
                                      invited_by, role_id, department_id, position_id, location_city, location_country,
                                      alert_enabled, alert_reminder)
            VALUES (%s,%s,%s,%s,%s,%s,TRUE,%s,%s,%s,%s,%s,%s,FALSE,FALSE)
        """, (tid, m["name"], m["first_name"], m["last_name"], email, hash_password(pw), owner_id,
              role_map.get(m["role_id"]), dept_map.get(m["department_id"]), pos_map.get(m["position_id"]),
              m["location_city"], m["location_country"]))
        logins.append(("Staff", m["name"], email, m["role_name"] or "—", pw))
    return tid, logins


def wipe_practice_data(cur, tid: int):
    """Delete every row the training tenant owns except the account itself,
    its roles and its logins. Retries in rounds so foreign-key order sorts
    itself out."""
    cur.execute("SELECT is_training FROM tenants WHERE id=%s", (tid,))
    row = cur.fetchone()
    if not row or not row["is_training"]:
        sys.exit(f"Tenant {tid} is not a training account — refusing to wipe it.")
    cur.execute("""
        SELECT c.table_name FROM information_schema.columns c
        JOIN information_schema.tables t ON t.table_name=c.table_name AND t.table_schema=c.table_schema
        WHERE c.column_name='tenant_id' AND c.table_schema='public' AND t.table_type='BASE TABLE'
    """)
    tables = sorted({r["table_name"] for r in cur.fetchall()} - KEEP_TABLES)
    for _round in range(6):
        left = []
        for t in tables:
            cur.execute("SAVEPOINT w")
            try:
                cur.execute(f'DELETE FROM "{t}" WHERE tenant_id=%s', (tid,))
                cur.execute("RELEASE SAVEPOINT w")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT w")
                left.append(t)
        if not left:
            return
        tables = left
    sys.exit(f"Could not clear: {', '.join(tables)}")


def seed_data(cur, tid: int, seed: int = 2026):
    """Made-up data shaped like a real account: ~300 leads (a third with a
    company, most with a phone, most with an email, a few WhatsApp), 45
    opportunities spread across the stages (incl. Won/Lost/Dropped), 60
    contacts without a lead, labels, notes and one CRM segment. Emails use
    example.com (reserved — can never reach anyone)."""
    rnd = random.Random(seed)
    plain = cur.connection.cursor()
    used_phones, used_names = set(), set()

    def phone():
        while True:
            p = "234" + rnd.choice(["803", "805", "806", "807", "810", "813", "816", "703", "706", "803", "902", "908"]) \
                + "".join(rnd.choice(string.digits) for _ in range(7))
            if p not in used_phones:
                used_phones.add(p); return p

    def business():
        while True:
            n = f"{rnd.choice(BRAND)} {rnd.choice(TRADE)}"
            if n not in used_names:
                used_names.add(n); return n

    def slug(n): return "".join(ch for ch in n.lower() if ch.isalnum())[:24]

    companies = []
    for _ in range(90):
        name = business() + " Ltd"
        cur.execute("INSERT INTO crm_companies (tenant_id, name, website) VALUES (%s,%s,%s) RETURNING id",
                    (tid, name, f"https://{slug(name)}.example.com"))
        companies.append((cur.fetchone()["id"], name))

    label_ids = {}
    for n in LABELS:
        cur.execute("INSERT INTO lead_labels (tenant_id, name) VALUES (%s,%s) RETURNING id", (tid, n))
        label_ids[n] = cur.fetchone()["id"]

    stage_plan = (["new_lead"] * 15 + ["contacted"] * 9 + ["qualified"] * 7 + ["proposal_sent"] * 6 +
                  ["negotiating"] * 4 + ["won"] * 2 + ["lost"] * 1 + ["dropped"] * 1)
    lead_ids, opp_count = [], 0
    for i in range(300):
        is_opp = i < len(stage_plan)
        has_company = rnd.random() < (0.6 if is_opp else 0.3)
        co = rnd.choice(companies) if has_company else None
        name = co[1].replace(" Ltd", "") if co else business()
        person = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}" if (is_opp or rnd.random() < 0.15) else None
        ph = phone() if rnd.random() < 0.85 else None
        em = f"{rnd.choice(['info', 'sales', 'hello', 'admin', 'contact'])}@{slug(name)}.example.com" if rnd.random() < 0.75 else None
        if not ph and not em:
            ph = phone()
        wa = (ph if ph and rnd.random() < 0.5 else phone()) if rnd.random() < 0.05 else None
        created = datetime.now() - timedelta(days=rnd.randint(1, 120), hours=rnd.randint(0, 23))
        stage = stage_plan[i] if is_opp else "new_lead"
        outcome = stage if stage in ("won", "lost", "dropped") else None
        real_stage = {"won": "won", "lost": "negotiating", "dropped": "contacted"}.get(stage, stage)
        value = rnd.choice([150000, 250000, 450000, 675000, 900000, 1200000, 2500000, 4800000]) \
            if is_opp and real_stage in ("qualified", "proposal_sent", "negotiating", "won") else None
        cur.execute("""
            INSERT INTO merchant_pipeline_leads
                (tenant_id, customer_name, contact_person, phone, whatsapp_number, email, company_id,
                 stage, is_opportunity, opportunity_at, outcome, won_date, dropped_at, dropped_reason,
                 deal_value, product_interest, source, notes, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (tid, name, person, ph, wa, em, co[0] if co else None, real_stage, is_opp,
              created + timedelta(days=2) if is_opp else None, outcome,
              (created + timedelta(days=20)).date() if outcome == "won" else None,
              created + timedelta(days=15) if outcome in ("lost", "dropped") else None,
              {"lost": "Customer chose competitor", "dropped": "No response"}.get(outcome),
              value, rnd.choice(PRODUCTS) if is_opp else None,
              rnd.choice(["manual", "whatsapp", "manual", None]),
              f"Met at {rnd.choice(CITY)} trade fair." if rnd.random() < 0.1 else None,
              created, created + timedelta(days=rnd.randint(0, 10))))
        lid = cur.fetchone()["id"]
        lead_ids.append(lid)
        cur.execute("INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes, created_at) "
                    "VALUES (%s,NULL,'new_lead','Training data',NULL,%s)", (lid, created))
        if is_opp:
            opp_count += 1
            cur.execute("INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes, created_at) "
                        "VALUES (%s,'new_lead','opportunity','Training data','Qualified — became an Opportunity',%s)",
                        (lid, created + timedelta(days=2)))
            if real_stage != "new_lead":
                cur.execute("INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes, created_at) "
                            "VALUES (%s,'new_lead',%s,'Training data',NULL,%s)", (lid, real_stage, created + timedelta(days=6)))
        _link_lead_to_contact(plain, tid, lid)   # the shared helper expects a plain cursor
        if rnd.random() < 0.12:
            cur.execute("INSERT INTO lead_label_leads (label_id, lead_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (label_ids[rnd.choice(LABELS)], lid))

    # Contacts without a lead (e.g. an imported customer list).
    for _ in range(60):
        n = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
        cur.execute("""INSERT INTO wa_contacts (tenant_id, phone, whatsapp_number, display_name, email, source, status, created_at)
                       VALUES (%s,%s,%s,%s,%s,'csv','prospect',NOW() - (%s || ' days')::interval)""",
                    (tid, phone(), None, n, f"{slug(n)}@example.com" if rnd.random() < 0.6 else None, str(rnd.randint(1, 90))))

    cur.execute("SELECT id FROM wa_contacts WHERE tenant_id=%s ORDER BY id", (tid,))
    contact_ids = [r["id"] for r in cur.fetchall()]
    for cid in rnd.sample(contact_ids, min(40, len(contact_ids))):
        cur.execute("INSERT INTO lead_label_contacts (label_id, contact_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (label_ids[rnd.choice(LABELS)], cid))
    for cid in rnd.sample(contact_ids, min(15, len(contact_ids))):
        cur.execute("INSERT INTO wa_contact_notes (contact_id, tenant_id, body) VALUES (%s,%s,%s)",
                    (cid, tid, rnd.choice(["Asked for a price list.", "Call back next week.",
                                           "Interested in a demo.", "Prefers email to calls."])))
    cur.execute("INSERT INTO wa_segments (tenant_id, name, description, module) VALUES (%s,'Practice segment','Made-up contacts to practise with','crm') RETURNING id", (tid,))
    seg = cur.fetchone()["id"]
    for cid in contact_ids[:25]:
        cur.execute("INSERT INTO wa_segment_members (segment_id, contact_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (seg, cid))
    return {"leads": len(lead_ids) - opp_count, "opportunities": opp_count,
            "contacts": len(contact_ids), "companies": len(companies)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=int, required=True, help="real tenant id to copy the setup from")
    ap.add_argument("--reset", action="store_true", help="wipe and reload the practice data only")
    a = ap.parse_args()
    conn, cur = db()
    try:
        cur.execute("SELECT email FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1", (a.source,))
        src_owner = cur.fetchone()
        cur.execute("SELECT tenant_id FROM customers WHERE email=%s", (plus_email(src_owner["email"]),))
        existing = cur.fetchone()
        if a.reset:
            if not existing:
                sys.exit("No training account yet — run without --reset first.")
            tid, logins = existing["tenant_id"], []
            wipe_practice_data(cur, tid)
        else:
            if existing:
                sys.exit("Training account already exists — use --reset to refresh its data.")
            tid, logins = create_account(cur, a.source)
        counts = seed_data(cur, tid)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()
    print(f"TRAINING_TENANT {tid} {json.dumps(counts)}")
    for kind, name, email, role, pw in logins:
        print(f"LOGIN\t{kind}\t{name}\t{email}\t{role}\t{pw}")


if __name__ == "__main__":
    main()
