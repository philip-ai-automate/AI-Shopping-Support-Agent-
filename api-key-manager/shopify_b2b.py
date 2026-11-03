"""
shopify_b2b.py — PhiXtra for Shopify, Part 4: recognise B2B buyers
(3 Nov 2026; plan https://claude.ai/artifact/SCxHeR8eo9SDJ5CFfsurzq, all
recommendations approved).

PhiXtra keeps a small private copy of each linked store's Shopify B2B
companies, their locations (with payment terms) and their contacts, so an
enquiry from a contact is recognised as an existing trade customer.

Kept: company + location names, payment terms, the contact's role per
location, the contact's name (encrypted), and keyed fingerprints of their
email and phone (HMAC with the store's own secret) for matching. Raw email
addresses and phone numbers are NOT stored; addresses are not read.

  Full read: on link, nightly, and a minute after any company / location /
      contact / role / customer change webhook (small stores: re-read all).
  Deleted: a contact on customers/redact or customers/delete; everything
      for a store on shop/redact (shopify_sync.forget_shop calls forget()).
  Matching: match() — certain (signed, logged-in chat buyer with their
      selected location) or likely (typed email, email sender, WhatsApp).

Needs the read_customers + read_companies permissions and Shopify's
protected customer data access (Name, Email, Phone). Without them the read
stops with status 'no_access' and is retried daily; nothing else changes.
"""
import base64
import hashlib
import hmac
import os
import re
import time

import psycopg2.extras

from db import get_db_connection

PAGE_COMPANIES = 50
PAGE_LOCATIONS = 20
PAGE_CONTACTS = 50
MAX_COMPANIES = 5000            # a safety cap for one read

_Q_COMPANIES = """query Companies($after: String) { companies(first: 50, after: $after) {
  pageInfo { hasNextPage endCursor } nodes { id name externalId } } }"""

_Q_LOCATIONS = """query CompanyLocations($id: ID!, $after: String) { company(id: $id) {
  locations(first: 20, after: $after) { pageInfo { hasNextPage endCursor } nodes {
    id name buyerExperienceConfiguration { paymentTermsTemplate { name } }
    roleAssignments(first: 25) { nodes { role { name } companyContact { id } } } } } } }"""

_Q_CONTACTS = """query CompanyContacts($id: ID!, $after: String) { company(id: $id) {
  contacts(first: 50, after: $after) { pageInfo { hasNextPage endCursor } nodes {
    id isMainContact customer { id firstName lastName
      defaultEmailAddress { emailAddress } defaultPhoneNumber { phoneNumber } } } } } }"""

B2B_TOPICS = ("companies/create", "companies/update", "companies/delete",
              "company_locations/create", "company_locations/update", "company_locations/delete",
              "company_contacts/create", "company_contacts/update", "company_contacts/delete",
              "company_contact_roles/assign", "company_contact_roles/revoke",
              "customers/update")


# ── Tables ───────────────────────────────────────────────────────────────
def ensure_tables():
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""SELECT column_name FROM information_schema.columns WHERE table_name='shopify_shops'""")
        have = {r[0] for r in cur.fetchall()}
        for col, typ in (("b2b_status", "TEXT"),          # queued | reading | done | none | no_access | error
                         ("b2b_dirty_at", "TIMESTAMPTZ"),  # a change webhook came in
                         ("b2b_read_at", "TIMESTAMPTZ"),
                         ("b2b_companies", "INTEGER"),
                         ("b2b_contacts", "INTEGER"),
                         ("b2b_error", "TEXT"),
                         ("b2b_tried_at", "TIMESTAMPTZ")):
            if col not in have:
                cur.execute(f"ALTER TABLE shopify_shops ADD COLUMN {col} {typ}")
        cur.execute("""CREATE TABLE IF NOT EXISTS shopify_b2b_companies (
                           shop        TEXT NOT NULL,
                           company_id  TEXT NOT NULL,
                           name        TEXT,
                           external_id TEXT,
                           seen_at     TIMESTAMPTZ DEFAULT NOW(),
                           PRIMARY KEY (shop, company_id))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS shopify_b2b_locations (
                           shop          TEXT NOT NULL,
                           location_id   TEXT NOT NULL,
                           company_id    TEXT NOT NULL,
                           name          TEXT,
                           payment_terms TEXT,
                           seen_at       TIMESTAMPTZ DEFAULT NOW(),
                           PRIMARY KEY (shop, location_id))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS shopify_b2b_contacts (
                           shop        TEXT NOT NULL,
                           contact_id  TEXT NOT NULL,
                           company_id  TEXT NOT NULL,
                           customer_id TEXT,
                           name_enc    TEXT,
                           email_fp    TEXT,
                           phone_fp    TEXT,
                           is_main     BOOLEAN DEFAULT FALSE,
                           seen_at     TIMESTAMPTZ DEFAULT NOW(),
                           PRIMARY KEY (shop, contact_id))""")
        cur.execute("CREATE INDEX IF NOT EXISTS shopify_b2b_contacts_email ON shopify_b2b_contacts (shop, email_fp)")
        cur.execute("CREATE INDEX IF NOT EXISTS shopify_b2b_contacts_phone ON shopify_b2b_contacts (shop, phone_fp)")
        cur.execute("CREATE INDEX IF NOT EXISTS shopify_b2b_contacts_cust ON shopify_b2b_contacts (shop, customer_id)")
        cur.execute("""CREATE TABLE IF NOT EXISTS shopify_b2b_roles (
                           shop        TEXT NOT NULL,
                           contact_id  TEXT NOT NULL,
                           location_id TEXT NOT NULL,
                           role        TEXT,
                           PRIMARY KEY (shop, contact_id, location_id))""")
        # Which Shopify company (and location) a PhiXtra lead belongs to.
        cur.execute("""CREATE TABLE IF NOT EXISTS shopify_b2b_lead_links (
                           lead_id     INTEGER PRIMARY KEY,
                           tenant_id   INTEGER NOT NULL,
                           shop        TEXT NOT NULL,
                           company_id  TEXT NOT NULL,
                           location_id TEXT,
                           contact_id  TEXT,
                           how         TEXT,            -- chat_login | email | phone
                           certain     BOOLEAN DEFAULT FALSE,
                           linked_at   TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("CREATE INDEX IF NOT EXISTS shopify_b2b_lead_links_co ON shopify_b2b_lead_links (shop, company_id)")
        conn.commit()
    finally:
        cur.close(); conn.close()


# ── Fingerprints + encryption ───────────────────────────────────────────
def norm_email(e) -> str:
    e = (e or "").strip().lower()
    return e if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e) else ""


def norm_phone(p, region: str = None) -> str:
    """+<country><number>, or '' when it can't be read safely."""
    p = str(p or "").strip()
    if len(re.sub(r"\D", "", p)) < 7:
        return ""
    try:
        import phonenumbers
        if p.startswith("+"):
            n = phonenumbers.parse(p, None)
        elif p.startswith("00"):
            n = phonenumbers.parse("+" + re.sub(r"\D", "", p)[2:], None)
        else:
            n = None
            if region:
                try:
                    n = phonenumbers.parse(p, region.upper())
                except phonenumbers.NumberParseException:
                    n = None
            if n is None or not phonenumbers.is_valid_number(n):
                # e.g. a WhatsApp number written 447911123456 (country code, no +)
                n = phonenumbers.parse("+" + re.sub(r"\D", "", p), None)
        if phonenumbers.is_valid_number(n):
            return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        pass
    return ""


def fingerprint(secret: str, kind: str, value: str) -> str:
    """Keyed fingerprint: equal values match, the value itself is not kept.
    The same function is used by the AI backend (chat), so keep it in step."""
    if not value or not secret:
        return ""
    return hmac.new(secret.encode(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


def _fernet():
    from cryptography.fernet import Fernet
    raw = os.getenv("PORTAL_SECRET_KEY", "fallback-insecure-key-change-me")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def _enc(s: str):
    return _fernet().encrypt(s.encode()).decode() if s else None


def _dec(s: str) -> str:
    if not s:
        return ""
    try:
        return _fernet().decrypt(s.encode()).decode()
    except Exception:
        return ""


def _legacy(gid) -> str:
    return str(gid or "").rsplit("/", 1)[-1]


# ── Read ────────────────────────────────────────────────────────────────
def _set(shop: str, **cols):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        sets = ", ".join(f"{k}=%s" for k in cols)
        cur.execute(f"UPDATE shopify_shops SET {sets} WHERE shop=%s", (*cols.values(), shop))
        conn.commit()
    finally:
        cur.close(); conn.close()


def queue_read(shop: str, now: bool = True):
    """A change in Shopify (or a link): read the companies again shortly."""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE shopify_shops SET b2b_dirty_at=NOW() WHERE shop=%s", (shop,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _pages(shop: str, query: str, path: tuple, variables: dict):
    """Every node of a paged connection. path = keys from data to the connection."""
    import shopify_app as sa
    after = None
    while True:
        data = sa.graphql(shop, query, {**variables, "after": after})
        conn = data
        for k in path:
            conn = (conn or {}).get(k)
        if not conn:
            return
        for n in conn.get("nodes") or []:
            yield n
        pi = conn.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            return
        after = pi.get("endCursor")


def _access_error(e: Exception) -> bool:
    t = str(e).lower()
    return any(w in t for w in ("access denied", "access_denied", "not approved", "protected customer data",
                                "required access", "read_customers", "read_companies", "unauthorized", "403"))


def read_all(shop: str) -> dict:
    """Read every company, location and contact of the store; replace our copy."""
    row = _shop(shop)
    if not row or not row.get("tenant_id") or row.get("uninstalled_at"):
        return {"skipped": True}
    secret = row.get("visitor_secret") or ""
    region = row.get("country") or None
    _set(shop, b2b_status="reading", b2b_error=None, b2b_dirty_at=None, b2b_tried_at=time.strftime("%Y-%m-%d %H:%M:%S%z"))
    companies, locations, contacts, roles = [], [], [], []
    try:
        for c in _pages(shop, _Q_COMPANIES, ("companies",), {}):
            companies.append(c)
            if len(companies) >= MAX_COMPANIES:
                break
        for c in companies:
            for loc in _pages(shop, _Q_LOCATIONS, ("company", "locations"), {"id": c["id"]}):
                terms = (((loc.get("buyerExperienceConfiguration") or {}).get("paymentTermsTemplate") or {}).get("name"))
                locations.append((_legacy(loc["id"]), _legacy(c["id"]), loc.get("name"), terms))
                for ra in ((loc.get("roleAssignments") or {}).get("nodes") or []):
                    cc = ra.get("companyContact") or {}
                    if cc.get("id"):
                        roles.append((_legacy(cc["id"]), _legacy(loc["id"]), (ra.get("role") or {}).get("name")))
            for ct in _pages(shop, _Q_CONTACTS, ("company", "contacts"), {"id": c["id"]}):
                cu = ct.get("customer") or {}
                email = norm_email((cu.get("defaultEmailAddress") or {}).get("emailAddress"))
                phone = norm_phone((cu.get("defaultPhoneNumber") or {}).get("phoneNumber"), region)
                name = " ".join(x for x in (cu.get("firstName"), cu.get("lastName")) if x).strip()
                contacts.append((_legacy(ct["id"]), _legacy(c["id"]), _legacy(cu.get("id")) or None, _enc(name),
                                 fingerprint(secret, "email", email) or None,
                                 fingerprint(secret, "phone", phone) or None, bool(ct.get("isMainContact"))))
    except Exception as e:
        if _access_error(e):
            _set(shop, b2b_status="no_access", b2b_error=str(e)[:500], b2b_read_at=None)
            return {"no_access": True}
        _set(shop, b2b_status="error", b2b_error=str(e)[:500])
        print(f"⚠️ shopify b2b read {shop}: {e}")
        return {"error": str(e)}

    conn = get_db_connection(); cur = conn.cursor()
    try:
        for t in ("shopify_b2b_roles", "shopify_b2b_contacts", "shopify_b2b_locations", "shopify_b2b_companies"):
            cur.execute(f"DELETE FROM {t} WHERE shop=%s", (shop,))
        if companies:
            psycopg2.extras.execute_values(cur, """INSERT INTO shopify_b2b_companies (shop, company_id, name, external_id)
                                                   VALUES %s ON CONFLICT DO NOTHING""",
                                           [(shop, _legacy(c["id"]), c.get("name"), c.get("externalId")) for c in companies])
        if locations:
            psycopg2.extras.execute_values(cur, """INSERT INTO shopify_b2b_locations (shop, location_id, company_id, name, payment_terms)
                                                   VALUES %s ON CONFLICT DO NOTHING""",
                                           [(shop, *l) for l in locations])
        if contacts:
            psycopg2.extras.execute_values(cur, """INSERT INTO shopify_b2b_contacts (shop, contact_id, company_id, customer_id,
                                                          name_enc, email_fp, phone_fp, is_main)
                                                   VALUES %s ON CONFLICT DO NOTHING""",
                                           [(shop, *c) for c in contacts])
        if roles:
            psycopg2.extras.execute_values(cur, """INSERT INTO shopify_b2b_roles (shop, contact_id, location_id, role)
                                                   VALUES %s ON CONFLICT DO NOTHING""",
                                           [(shop, *r) for r in roles])
        # Lead links to companies that are gone stay as history, but lose their location if it went.
        cur.execute("""UPDATE shopify_b2b_lead_links l SET location_id=NULL WHERE l.shop=%s AND l.location_id IS NOT NULL
                         AND NOT EXISTS (SELECT 1 FROM shopify_b2b_locations x WHERE x.shop=l.shop AND x.location_id=l.location_id)""",
                    (shop,))
        cur.execute("""UPDATE shopify_shops SET b2b_status=%s, b2b_read_at=NOW(), b2b_companies=%s,
                              b2b_contacts=%s, b2b_error=NULL WHERE shop=%s""",
                    ("done" if companies else "none", len(companies), len(contacts), shop))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return {"companies": len(companies), "locations": len(locations), "contacts": len(contacts)}


def _shop(shop: str):
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM shopify_shops WHERE shop=%s", (shop,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def run_due() -> list:
    """Cron (every minute, from shopify_sync.py): read stores that are new,
    changed a minute ago, or whose 'no access' / error is a day old."""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""SELECT shop FROM shopify_shops
                        WHERE tenant_id IS NOT NULL AND uninstalled_at IS NULL AND access_token IS NOT NULL
                          AND (b2b_status IS NULL OR b2b_status='queued'
                               OR (b2b_dirty_at IS NOT NULL AND b2b_dirty_at < NOW() - INTERVAL '1 minute')
                               OR (b2b_status IN ('no_access','error')
                                     AND COALESCE(b2b_tried_at, 'epoch') < NOW() - INTERVAL '1 day')
                               OR (b2b_status='reading' AND COALESCE(b2b_tried_at, 'epoch') < NOW() - INTERVAL '2 hours'))
                        ORDER BY shop LIMIT 5""")
        shops = [r[0] for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    out = []
    for s in shops:
        out.append((s, read_all(s)))
    return out


def nightly():
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE shopify_shops SET b2b_dirty_at=NOW() - INTERVAL '2 minutes'
                        WHERE tenant_id IS NOT NULL AND uninstalled_at IS NULL""")
        conn.commit()
    finally:
        cur.close(); conn.close()


# ── Privacy ─────────────────────────────────────────────────────────────
def forget_customer(shop: str, customer_id) -> int:
    """customers/redact or customers/delete: remove that person."""
    cid = _legacy(customer_id)
    if not cid:
        return 0
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT contact_id FROM shopify_b2b_contacts WHERE shop=%s AND customer_id=%s", (shop, cid))
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM shopify_b2b_roles WHERE shop=%s AND contact_id = ANY(%s)", (shop, ids))
            cur.execute("DELETE FROM shopify_b2b_contacts WHERE shop=%s AND contact_id = ANY(%s)", (shop, ids))
            cur.execute("UPDATE shopify_b2b_lead_links SET contact_id=NULL WHERE shop=%s AND contact_id = ANY(%s)", (shop, ids))
        conn.commit()
        return len(ids)
    finally:
        cur.close(); conn.close()


def customer_data(shop: str, customer_id) -> dict:
    """customers/data_request: what we hold about one customer."""
    cid = _legacy(customer_id)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT c.contact_id, c.name_enc, co.name AS company, c.is_main
                         FROM shopify_b2b_contacts c LEFT JOIN shopify_b2b_companies co
                           ON co.shop=c.shop AND co.company_id=c.company_id
                        WHERE c.shop=%s AND c.customer_id=%s""", (shop, cid))
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    return {"contacts": [{"name": _dec(r["name_enc"]), "company": r["company"], "main_contact": r["is_main"]}
                         for r in rows],
            "note": "Email and phone are kept only as one-way fingerprints for recognising enquiries."}


def forget(shop: str):
    """shop/redact: everything for the store."""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        for t in ("shopify_b2b_roles", "shopify_b2b_contacts", "shopify_b2b_locations",
                  "shopify_b2b_companies", "shopify_b2b_lead_links"):
            cur.execute(f"DELETE FROM {t} WHERE shop=%s", (shop,))
        conn.commit()
    finally:
        cur.close(); conn.close()


# ── Matching ────────────────────────────────────────────────────────────
def _tenant_shop(cur, tenant_id: int):
    cur.execute("""SELECT shop, visitor_secret, country, b2b_status FROM shopify_shops
                    WHERE tenant_id=%s AND uninstalled_at IS NULL ORDER BY linked_at DESC NULLS LAST LIMIT 1""",
                (tenant_id,))
    return cur.fetchone()


def match(tenant_id: int, emails=(), phones=(), location_id=None, logged_in_email=None, cur=None) -> dict:
    """The Shopify company a buyer belongs to, or {}.
    logged_in_email + location_id: the signed, logged-in chat buyer → certain.
    Otherwise emails / phones → likely."""
    own = cur is None
    if own:
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        s = _tenant_shop(cur, tenant_id)
        if not s or not s["visitor_secret"] or s["b2b_status"] not in ("done",):
            return {}
        shop, secret = s["shop"], s["visitor_secret"]
        hit, how, certain = None, None, False
        if logged_in_email:
            fp = fingerprint(secret, "email", norm_email(logged_in_email))
            cur.execute("SELECT * FROM shopify_b2b_contacts WHERE shop=%s AND email_fp=%s LIMIT 1", (shop, fp))
            hit = cur.fetchone()
            if hit:
                how, certain = "chat_login", True
        if not hit:
            for e in emails or ():
                fp = fingerprint(secret, "email", norm_email(e))
                if not fp:
                    continue
                cur.execute("SELECT * FROM shopify_b2b_contacts WHERE shop=%s AND email_fp=%s LIMIT 1", (shop, fp))
                hit = cur.fetchone()
                if hit:
                    how = "email"
                    break
        if not hit:
            for p in phones or ():
                fp = fingerprint(secret, "phone", norm_phone(p, s["country"]))
                if not fp:
                    continue
                cur.execute("SELECT * FROM shopify_b2b_contacts WHERE shop=%s AND phone_fp=%s LIMIT 1", (shop, fp))
                hit = cur.fetchone()
                if hit:
                    how = "phone"
                    break
        if not hit:
            return {}
        cur.execute("SELECT name FROM shopify_b2b_companies WHERE shop=%s AND company_id=%s", (shop, hit["company_id"]))
        co = cur.fetchone() or {}
        cur.execute("""SELECT l.location_id, l.name, l.payment_terms, r.role
                         FROM shopify_b2b_roles r JOIN shopify_b2b_locations l
                           ON l.shop=r.shop AND l.location_id=r.location_id
                        WHERE r.shop=%s AND r.contact_id=%s ORDER BY l.name""", (shop, hit["contact_id"]))
        locs = [dict(x) for x in cur.fetchall()]
        chosen = None
        loc_id = _legacy(location_id) if location_id else None
        if certain and loc_id:
            chosen = next((l for l in locs if l["location_id"] == loc_id), None)
        if not chosen and len(locs) == 1:
            chosen = locs[0]
        return {"shop": shop, "company_id": hit["company_id"], "company": co.get("name") or "",
                "contact_id": hit["contact_id"], "contact_name": _dec(hit["name_enc"]),
                "locations": locs, "location": chosen, "how": how, "certain": certain}
    except Exception as e:
        print(f"⚠️ shopify_b2b.match tenant={tenant_id}: {e}")
        return {}
    finally:
        if own:
            cur.close(); conn.close()


def link_lead(cur, tenant_id: int, lead_id: int, m: dict):
    """Remember which Shopify company (and location) a lead belongs to.
    A certain match replaces a likely one; a likely one never replaces a certain one."""
    if not m or not lead_id:
        return
    loc = (m.get("location") or {}).get("location_id")
    cur.execute("""INSERT INTO shopify_b2b_lead_links (lead_id, tenant_id, shop, company_id, location_id, contact_id, how, certain)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (lead_id) DO UPDATE SET
                       company_id=EXCLUDED.company_id, contact_id=EXCLUDED.contact_id, how=EXCLUDED.how,
                       certain=EXCLUDED.certain,
                       location_id=COALESCE(EXCLUDED.location_id, shopify_b2b_lead_links.location_id),
                       linked_at=NOW()
                   WHERE EXCLUDED.certain OR NOT shopify_b2b_lead_links.certain""",
                (lead_id, tenant_id, m["shop"], m["company_id"], loc, m.get("contact_id"), m.get("how"),
                 bool(m.get("certain"))))


def company_owner(cur, tenant_id: int, shop: str, company_id: str, not_lead: int = None):
    """(key, label) of the salesperson who last dealt with this company."""
    cur.execute("""SELECT l.assigned_key, l.assigned_to FROM shopify_b2b_lead_links k
                     JOIN merchant_pipeline_leads l ON l.id=k.lead_id AND l.tenant_id=k.tenant_id
                    WHERE k.tenant_id=%s AND k.shop=%s AND k.company_id=%s AND l.assigned_key IS NOT NULL
                      AND l.id <> COALESCE(%s, -1)
                    ORDER BY l.updated_at DESC NULLS LAST, l.id DESC LIMIT 1""",
                (tenant_id, shop, company_id, not_lead))
    r = cur.fetchone()
    if not r:
        return None
    r = dict(r) if not isinstance(r, tuple) else {"assigned_key": r[0], "assigned_to": r[1]}
    return (r["assigned_key"], r["assigned_to"])


def set_lead_location(tenant_id: int, lead_id: int, location_id: str) -> bool:
    """The salesperson picks the site on a quote request / lead."""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE shopify_b2b_lead_links k SET location_id=%s
                        WHERE k.lead_id=%s AND k.tenant_id=%s AND EXISTS (
                              SELECT 1 FROM shopify_b2b_locations l
                               WHERE l.shop=k.shop AND l.company_id=k.company_id AND l.location_id=%s)""",
                    (str(location_id), int(lead_id), int(tenant_id), str(location_id)))
        ok = cur.rowcount > 0
        conn.commit()
        return ok
    finally:
        cur.close(); conn.close()


def lead_card(tenant_id: int, lead_id: int) -> dict:
    """The 'Shopify trade customer' card for a lead (portal), or {}."""
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT k.*, co.name AS company, c.name_enc, c.is_main
                         FROM shopify_b2b_lead_links k
                         LEFT JOIN shopify_b2b_companies co ON co.shop=k.shop AND co.company_id=k.company_id
                         LEFT JOIN shopify_b2b_contacts c ON c.shop=k.shop AND c.contact_id=k.contact_id
                        WHERE k.lead_id=%s AND k.tenant_id=%s""", (int(lead_id), int(tenant_id)))
        k = cur.fetchone()
        if not k:
            return {}
        cur.execute("""SELECT l.location_id, l.name, l.payment_terms,
                              (SELECT r.role FROM shopify_b2b_roles r WHERE r.shop=l.shop AND r.location_id=l.location_id
                                  AND r.contact_id=%s LIMIT 1) AS role
                         FROM shopify_b2b_locations l WHERE l.shop=%s AND l.company_id=%s ORDER BY l.name""",
                    (k["contact_id"], k["shop"], k["company_id"]))
        locs = [dict(x) for x in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    mine = [l for l in locs if l["role"]] or locs
    chosen = next((l for l in locs if l["location_id"] == k["location_id"]), None)
    if not chosen and len(mine) == 1:
        chosen = mine[0]
    store = k["shop"].replace(".myshopify.com", "")
    return {"company": k["company"] or "(company removed in Shopify)", "company_id": k["company_id"],
            "contact_name": _dec(k["name_enc"]), "main_contact": bool(k["is_main"]),
            "how": k["how"], "certain": bool(k["certain"]),
            "location": chosen, "locations": mine,
            "admin_url": f"https://admin.shopify.com/store/{store}/companies/{k['company_id']}"}


def home_state(shop_row: dict) -> dict:
    """The Shopify Home 'Business buyers' line."""
    return {"status": shop_row.get("b2b_status") or "queued",
            "companies": shop_row.get("b2b_companies") or 0,
            "contacts": shop_row.get("b2b_contacts") or 0}


def inbox_card(tenant_id: int, key: str, webchat_conv: dict = None) -> dict:
    """The Inbox strip for the open conversation: WhatsApp number, or a
    website chat (signed logged-in Shopify buyer, else the email / number
    the visitor gave). {} = not a trade customer."""
    if not key or key.startswith("fb:"):
        return {}
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT 1 FROM shopify_shops WHERE tenant_id=%s AND uninstalled_at IS NULL AND b2b_status='done'",
                    (tenant_id,))
        if not cur.fetchone():
            return {}
        if key.startswith("web:"):
            sid = key.split(":", 1)[1]
            v = None
            cur.execute("SELECT to_regclass('chat_visitors') IS NOT NULL AS ok")
            if cur.fetchone()["ok"]:
                cur.execute("SELECT * FROM chat_visitors WHERE tenant_id=%s AND session_id=%s", (tenant_id, sid[:64]))
                v = cur.fetchone()
            wc = webchat_conv or {}
            emails = [e for e in (wc.get("visitor_email"), (v or {}).get("email")) if e]
            phones = [p for p in (wc.get("whatsapp_number"), (v or {}).get("phone")) if p]
            signed = bool(v and v.get("shopify_signed") and v.get("email"))
            m = match(tenant_id, emails, phones, location_id=(v or {}).get("shopify_location_id") if signed else None,
                      logged_in_email=v["email"] if signed else None, cur=cur)
        else:
            digits = re.sub(r"\D", "", key)
            m = match(tenant_id, (), ["+" + digits] if digits else (), cur=cur)
    finally:
        cur.close(); conn.close()
    if not m:
        return {}
    store = m["shop"].replace(".myshopify.com", "")
    locs = [{"location_id": l["location_id"], "name": l["name"], "payment_terms": l["payment_terms"]} for l in m["locations"]]
    loc = m.get("location")
    return {"company": m["company"], "certain": m["certain"], "how": m["how"], "locations": locs,
            "location": {"name": loc["name"], "payment_terms": loc["payment_terms"]} if loc else None,
            "admin_url": f"https://admin.shopify.com/store/{store}/companies/{m['company_id']}"}
