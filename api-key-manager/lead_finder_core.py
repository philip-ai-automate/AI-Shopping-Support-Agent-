"""
lead_finder_core.py — Lead Finder (pilot, 2026-10-19; approved mock-up
https://claude.ai/artifact/B9GV6PJGUHrxnw9wYLzbVJ). Finds business
decision-makers through Prospeo (one PhiXtra account, key PROSPEO_API_KEY in
.env) and adds the ones a person picks to Leads.

Pilot: only the PLATFORM business (PLATFORM_TENANT_ID) sees it — see
lead_finder_routes.py. Built per business (tenant_id everywhere) so other
businesses can be switched on later as a paid add-on.

Prospeo costs (their rules): a search page of up to 25 people = 1 credit
(free again for 30 days); an email = 1 credit, a mobile = 10; nothing
found = free; revealing someone already revealed = free.

Tables (made on first use):
  lead_finder_people  one row per person added to Leads (stops paying twice)
  lead_finder_jobs    one row per "Add to Leads" click, with its progress
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

import psycopg2.extras

from db import get_db_connection

API = "https://api.prospeo.io"

# Prospeo's own values (api-docs filters documentation, 2026-10-19).
SENIORITIES = ["Founder/Owner", "C-Suite", "Director", "Head", "Vice President", "Partner",
               "Manager", "Senior", "Entry", "Intern"]
HEADCOUNTS = ["1-10", "11-20", "21-50", "51-100", "101-200", "201-500", "501-1000",
              "1001-2000", "2001-5000", "5001-10000", "10000+"]
# WhatsApp tools Prospeo spots on company websites (Search Suggestions, 2026-10-18).
WA_TOOLS = ["WhatsApp Business Chat", "Wati", "Interakt", "Respond.io", "Zoko", "PushDaddy Whatsapp Chat"]
# Which choice of a business's own "Technologies" column each tool fills
# (same rules as the 17 Oct website scan: an API provider widget = Business API).
TECH_TO_CHOICE = {
    "whatsapp business chat": "WhatsApp", "pushdaddy whatsapp chat": "WhatsApp",
    "wati": "WhatsApp Business API", "interakt": "WhatsApp Business API",
    "respond.io": "WhatsApp Business API", "zoko": "WhatsApp Business API",
    "tawk.to": "Tawk.to",
}

INDUSTRIES = sorted("""IT Services and IT Consulting|Construction|Business Consulting and Services|General Retail|Advertising Services|Real Estate|Software Development|Medical Practices|Financial Services|Technology, Information and Internet|Restaurants|Hospitals and Health Care|Wellness and Fitness Services|Design Services|Non-profit Organizations|Individual and Family Services|Hospitality|Appliances, Electrical, and Electronics Manufacturing|Motor Vehicle Manufacturing|Professional Training and Coaching|Food and Beverage Services|Education Administration Programs|Consumer Services|Accounting|General Wholesale|Civic and Social Organizations|Architecture and Planning|Truck and Railroad Transportation|Spectator Sports|Entertainment Providers|Retail Apparel and Fashion|Higher Education|Industrial Machinery Manufacturing|Insurance|Machinery Manufacturing|Legal Services|General Manufacturing|Events Services|Travel Arrangements|Human Resources Services|Law Practice|Media Production and Publishing|Food and Beverage Manufacturing|Staffing and Recruiting|Marketing Services|Renewable Energy|Research Services|Facilities Services|Environmental Services|Telecommunications|Transportation, Logistics, Supply Chain and Storage|Electric Power Generation|E-Learning Providers|Oil, Gas, and Mining|Arts and Crafts|Photography|Venture Capital and Private Equity Principals|Music|Retail|Office Equipment|Farming|Wholesale Building Materials|Furniture and Home Furnishings Manufacturing|Government Administration|Investment Management|Civil Engineering|Primary and Secondary Education|Medical Equipment Manufacturing|Textile Manufacturing|Public Relations and Communications Services|Printing Services|Religious Institutions|Artists and Writers|Personal Care Product Manufacturing|International Trade and Development|Mental Health Care|Chemical Manufacturing|Pharmaceutical Manufacturing|Utilities|Information Services|Security and Investigations|Wholesale Import and Export|Banking|Biotechnology Research|Recreational Facilities|Automation Machinery Manufacturing|Plastics Manufacturing|Automotive Retail|Groceries|Freight and Package Transportation|Computer and Network Security|Computer Games|Veterinary Services|Airlines and Aviation|Maritime Transportation|Executive Offices|Sporting Goods Manufacturing|Market Research|Mechanical or Industrial Engineering|Aviation and Aerospace Component Manufacturing|Strategic Management Services|Consumer Goods|Engineering Services|Museums, Historical Sites, and Zoos|Investment, Funds and Trusts|Philanthropic Fundraising Services|Human Resources|Computer Hardware|E-Learning|Translation and Localization|Public Safety|Business Supplies and Equipment|Defense and Space Manufacturing|Glass, Ceramics and Concrete Manufacturing|Warehousing and Storage|Alternative Medicine|Animation and Post-production|Think Tanks|Semiconductor Manufacturing|Building Materials|Capital Markets|Interior Design|Political Organizations|Fundraising|Law Enforcement|Cosmetics|Wireless Services|Sports Teams and Clubs|Retail Health and Personal Care Products|Professional Services|Social Networking Platforms|Dairy Product Manufacturing|Gambling Facilities and Casinos|Internet Marketplace Platforms|Shipbuilding|Luxury Goods and Jewelry|Libraries|Ranching and Fisheries|Online and Mail Order Retail|Community Services|Blockchain Services|Food and Beverage Retail|Retail Motor Vehicles|Data Infrastructure and Analytics|Wine and Spirits|Sports and Recreation Instruction|Paper and Forest Products|Agriculture, Construction, Mining Machinery Manufacturing|Home Health Care Services|Vehicle Repair and Maintenance|Business Content|General Repair and Maintenance|Maritime|Packaging and Containers|Industry Associations|Retail Furniture and Home Furnishings|Tobacco Manufacturing|Pet Services|Landscaping Services|Alternative Dispute Resolution|Dentists|Wholesale Food and Beverage|Fabricated Metal Products|Aviation & Aerospace|Railroad Equipment Manufacturing|Mobile Gaming Apps|Administrative and Support Services|Apparel Manufacturing|Nanotechnology Research|Specialty Trade Contractors|Janitorial Services|Physical, Occupational and Speech Therapists|Medical and Diagnostic Laboratories|Professional Organizations|Philanthropy|Digital Accessibility Services|Fashion Accessories Manufacturing|Semiconductors|Wholesale Motor Vehicles and Parts|HVAC and Refrigeration Equipment Manufacturing|Schools|Wood Product Manufacturing|Fire Protection|Retail Office Supplies and Gifts|Equipment Rental Services|Movies, Videos and Sound|Ground Passenger Transportation|Wholesale Hardware, Plumbing, Heating Equipment|Housing and Community Development|Robotics Engineering|Wholesale Chemical and Allied Products|Building Finishing Contractors|Child Day Care Services|Radio and Television Broadcasting|Paint, Coating, and Adhesive Manufacturing|Building Structure and Exterior Contractors|Retail Pharmacies|Retail Building Materials and Garden Equipment|Waste Treatment and Disposal|Wholesale Machinery|Metal Treatments|Accommodation Services|Horticulture|Water Supply and Irrigation Systems|Building Equipment Contractors|Waste Collection|Executive Search Services|Telephone Call Centers|Mobile Food Services|Data Security Software Products|Taxi and Limousine Services|Retail Florists|Metalworking Machinery Manufacturing|Plastics and Rubber Product Manufacturing|Climate Technology Product Manufacturing|Golf Courses and Country Clubs|Loan Brokers|Water, Waste, Steam, and Air Conditioning Services|Office Administration|Wholesale Metals and Minerals|Construction Hardware Manufacturing|Surveying and Mapping Services|Air, Water, and Waste Program Management|Measuring and Control Instrument Manufacturing|Leather Product Manufacturing|Animal Feed Manufacturing|Wholesale Furniture and Home Furnishings|Wholesale Recyclable Materials|Wholesale Drugs and Sundries|Theater Companies|Wholesale Apparel and Sewing Supplies|Wholesale Computer Equipment|Household, Laundry and Drycleaning Services|Distilleries|Breweries|Wineries|Optometrists|Dance Companies|Retail Recyclable Materials & Used Merchandise|Utility System Construction|Vocational Rehabilitation Services|Amusement Parks and Arcades|Retail Musical Instruments|Turned Products and Fastener Manufacturing|Engines and Power Transmission Equipment Manufacturing|Transportation Equipment Manufacturing|Collection Agencies|Utilities Administration|Credit Intermediation|Housing and Socio-Economic Programs|Securities and Commodity Exchanges|Boilers, Tanks, and Shipping Container Manufacturing|Claims Adjusting, Actuarial Services|Ambulance Services|Mattress and Blinds Manufacturing|Regenerative Design|Primary Metal Manufacturing""".split("|"), key=str.lower)

# Plans whose filters Prospeo refuses on the free plan, as people should read them.
_PLAN_FILTER_NAMES = {"company_technology": "Uses WhatsApp tools", "company_industry": "Industry",
                      "company_headcount_range": "Company size", "person_job_title": "Job title"}

_tables_ready = False
_lock = threading.Lock()          # Prospeo allows about 1 request a second on the free plan
_last_call = [0.0]
_search_cache = {}                # (tenant, filters, page) -> (time, result): a refresh never pays twice


class ProspeoError(Exception):
    pass


def configured() -> bool:
    return bool((os.environ.get("PROSPEO_API_KEY") or "").strip())


def _call(path: str, body: dict, tries: int = 4) -> dict:
    key = (os.environ.get("PROSPEO_API_KEY") or "").strip()
    if not key:
        raise ProspeoError("The Prospeo key isn't set up yet.")
    data = json.dumps(body).encode()
    for attempt in range(tries):
        with _lock:
            wait = 1.05 - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        req = urllib.request.Request(API + path, data=data, method="POST",
                                     headers={"X-KEY": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                out = json.load(e)
            except Exception:
                out = {"error": True, "error_code": f"HTTP {e.code}"}
            if e.code == 429 or out.get("error_code") == "Rate limit exceeded":
                if attempt < tries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise ProspeoError("Prospeo is busy (too many requests). Try again in a minute.")
            return out
        except Exception as e:
            if attempt < tries - 1:
                time.sleep(1)
                continue
            raise ProspeoError(f"Couldn't reach Prospeo ({e.__class__.__name__}). Try again in a minute.")
    raise ProspeoError("Couldn't reach Prospeo. Try again in a minute.")


def account() -> dict:
    """{'plan': 'FREE', 'credits_left': 81, 'renews': '2026-11-18'} or {}."""
    try:
        d = _call("/account-information", {})
        r = d.get("response") or {}
        return {"plan": r.get("current_plan"), "credits_left": r.get("remaining_credits"),
                "used": r.get("used_credits"), "renews": (r.get("next_quota_renewal_date") or "")[:10]}
    except ProspeoError:
        return {}


def locations(q: str) -> list:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    d = _call("/search-suggestions", {"location_search": q[:80]})
    return [s["name"] for s in (d.get("location_suggestions") or []) if s.get("name")][:12]


def build_filters(f: dict) -> dict:
    """The search form → Prospeo filters. f: lists for locations / industries /
    seniority / sizes / wa_tools, text for title / keyword, bool one_per_company."""
    out = {}
    if f.get("locations"):
        out["company_location_search"] = {"include": f["locations"][:20]}
    if f.get("industries"):
        out["company_industry"] = {"include": f["industries"][:50]}
    if f.get("seniority"):
        out["person_seniority"] = {"include": [s for s in f["seniority"] if s in SENIORITIES]}
    if f.get("title"):
        titles = [t.strip() for t in f["title"].split(",") if len(t.strip()) >= 2][:20]
        if titles:
            out["person_job_title"] = {"include": titles, "match_mode": "CONTAINS"}
    if f.get("sizes"):
        out["company_headcount_range"] = [s for s in f["sizes"] if s in HEADCOUNTS]
    if f.get("wa_tools"):
        out["company_technology"] = {"include": [t for t in f["wa_tools"] if t in WA_TOOLS]}
    if f.get("keyword") and len(f["keyword"].strip()) >= 3:
        out["company_keywords"] = {"include": [f["keyword"].strip()[:100]]}
    if f.get("one_per_company"):
        out["max_person_per_company"] = 1
    return out


def _row(r: dict) -> dict:
    p, c = r.get("person") or {}, r.get("company") or {}
    loc = c.get("location") or {}
    tech = c.get("technology") or {}
    names = tech.get("technology_names") if isinstance(tech, dict) else []
    return {
        "person_id": p.get("person_id"),
        "name": p.get("full_name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
        "title": p.get("current_job_title") or p.get("headline") or "",
        "linkedin": p.get("linkedin_url") or "",
        "company": c.get("name") or "",
        "industry": c.get("industry") or "",
        "size": c.get("employee_range") or "",
        "city": loc.get("city") or "",
        "country": loc.get("country") or "",
        "domain": c.get("domain") or "",
        "email": ((p.get("email") or {}).get("status") == "VERIFIED"),
        "mobile": ((p.get("mobile") or {}).get("status") == "VERIFIED"),
        "wa_tools": [t for t in (names or []) if t.lower() in TECH_TO_CHOICE and TECH_TO_CHOICE[t.lower()] != "Tawk.to"],
    }


def search(tenant_id: int, f: dict, page: int = 1) -> dict:
    """{'total', 'pages', 'rows', 'error'}. Uses 1 Prospeo credit per page that
    has people in it (same page again within 30 days: free)."""
    filters = build_filters(f)
    if not any(k in filters for k in ("company_location_search", "company_industry", "person_job_title",
                                       "company_technology", "company_keywords")):
        return {"total": 0, "pages": 0, "rows": [], "error": "Choose at least a country, an industry, a job title or a keyword."}
    ck = (tenant_id, json.dumps(filters, sort_keys=True), page)
    hit = _search_cache.get(ck)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        d = _call("/search-person", {"page": max(1, min(int(page), 1000)), "filters": filters})
    except ProspeoError as e:
        return {"total": 0, "pages": 0, "rows": [], "error": str(e)}
    if d.get("error"):
        code = d.get("error_code") or ""
        if code == "PLAN_REQUIRED":
            fe = d.get("filter_error") or ""
            names = [v for k, v in _PLAN_FILTER_NAMES.items() if k in fe]
            msg = (f"PhiXtra's Prospeo plan doesn't include the filter{'s' if len(names) > 1 else ''} "
                   f"{', '.join(names) or fe}. Remove it and search again, or upgrade the Prospeo plan.")
        elif code == "NO_RESULTS":
            msg = None
        else:
            msg = f"Prospeo didn't accept this search ({code or 'unknown error'})."
        out = {"total": 0, "pages": 0, "rows": [], "error": msg}
        if msg is None:
            _search_cache[ck] = (time.time(), out)
        return out
    pg = d.get("pagination") or {}
    out = {"total": pg.get("total_count") or 0, "pages": pg.get("total_page") or 0,
           "rows": [_row(r) for r in (d.get("results") or [])], "error": None}
    _search_cache[ck] = (time.time(), out)
    if len(_search_cache) > 300:
        for k in sorted(_search_cache, key=lambda k: _search_cache[k][0])[:100]:
            _search_cache.pop(k, None)
    return out


# ── tables ──────────────────────────────────────────────────────────────────
def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_tables():
    """Created on first use; DDL only when a table is really missing."""
    global _tables_ready
    if _tables_ready:
        return
    conn, cur = _db()
    try:
        cur.execute("SELECT to_regclass('lead_finder_people') IS NOT NULL AS a, to_regclass('lead_finder_jobs') IS NOT NULL AS b")
        r = cur.fetchone()
        if not (r["a"] and r["b"]):
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS lead_finder_people (
                               tenant_id  INTEGER NOT NULL,
                               person_id  VARCHAR(40) NOT NULL,
                               lead_id    INTEGER,
                               email_found  BOOLEAN NOT NULL DEFAULT FALSE,
                               mobile_found BOOLEAN NOT NULL DEFAULT FALSE,
                               added_by   VARCHAR(120),
                               added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                               PRIMARY KEY (tenant_id, person_id))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS lead_finder_jobs (
                               id         SERIAL PRIMARY KEY,
                               tenant_id  INTEGER NOT NULL,
                               actor_key  VARCHAR(40),
                               actor_name VARCHAR(120),
                               request    JSONB NOT NULL,
                               status     VARCHAR(12) NOT NULL DEFAULT 'running',
                               done       INTEGER NOT NULL DEFAULT 0,
                               total      INTEGER NOT NULL DEFAULT 0,
                               results    JSONB NOT NULL DEFAULT '[]'::jsonb,
                               credits    INTEGER NOT NULL DEFAULT 0,
                               error      TEXT,
                               created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                               finished_at TIMESTAMPTZ)""")
            cur.execute("CREATE INDEX IF NOT EXISTS lead_finder_jobs_tenant_idx ON lead_finder_jobs (tenant_id, created_at DESC)")
        conn.commit()
        _tables_ready = True
    finally:
        cur.close(); conn.close()


def already_added(tenant_id: int, person_ids: list) -> dict:
    """{person_id: lead_id} for people this business already added."""
    if not person_ids:
        return {}
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT person_id, lead_id FROM lead_finder_people WHERE tenant_id=%s AND person_id = ANY(%s)",
                    (tenant_id, list(person_ids)))
        return {r["person_id"]: r["lead_id"] for r in cur.fetchall()}
    finally:
        cur.close(); conn.close()


def usage_this_month(tenant_id: int) -> dict:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT COUNT(*) AS added, COUNT(*) FILTER (WHERE email_found) AS emails,
                              COUNT(*) FILTER (WHERE mobile_found) AS mobiles
                         FROM lead_finder_people
                        WHERE tenant_id=%s AND added_at >= date_trunc('month', NOW())""", (tenant_id,))
        return dict(cur.fetchone())
    finally:
        cur.close(); conn.close()


def recent_jobs(tenant_id: int, limit: int = 5) -> list:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT id, actor_name, status, done, total, credits, created_at,
                              (SELECT COUNT(*) FROM jsonb_array_elements(results) x WHERE x->>'outcome'='added') AS added
                         FROM lead_finder_jobs WHERE tenant_id=%s ORDER BY id DESC LIMIT %s""", (tenant_id, limit))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def get_job(tenant_id: int, job_id: int):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM lead_finder_jobs WHERE id=%s AND tenant_id=%s", (job_id, tenant_id))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def start_job(tenant_id: int, actor_key: str, actor_name: str, req: dict, add_one) -> int:
    """Saves the job and works through it in the background. add_one(cur,
    tenant_id, person_row, enriched, req) creates the lead (portal code) and
    returns (lead_id, was_already_there, run_after_commit or None)."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO lead_finder_jobs (tenant_id, actor_key, actor_name, request, total)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (tenant_id, actor_key, actor_name, json.dumps(req), len(req["people"])))
        job_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        cur.close(); conn.close()
    threading.Thread(target=_run_job, args=(tenant_id, job_id, actor_name, req, add_one),
                     daemon=True, name=f"lead-finder-{job_id}").start()
    return job_id


def _run_job(tenant_id: int, job_id: int, actor_name: str, req: dict, add_one):
    results, credits = [], 0
    start = account().get("credits_left")      # credits used = Prospeo's own count before/after
    try:
        for person in req["people"]:
            outcome = {"person_id": person["person_id"], "name": person.get("name"),
                       "company": person.get("company"), "title": person.get("title")}
            try:
                if already_added(tenant_id, [person["person_id"]]):
                    outcome.update(outcome="skipped", why="Already in your Leads")
                else:
                    body = {"only_verified_email": True, "data": {"person_id": person["person_id"]}}
                    if req.get("get_mobile"):
                        # Only when asked: "only_verified_mobile" on its own makes Prospeo reveal
                        # (and charge for) the mobile too — found 2026-10-19.
                        body.update(enrich_mobile=True, only_verified_mobile=True)
                    d = _call("/enrich-person", body)
                    if d.get("error"):
                        code = d.get("error_code") or "unknown"
                        if code == "NO_MATCH":
                            outcome.update(outcome="none", why="No email or mobile found (not charged)")
                        elif code == "INSUFFICIENT_CREDITS":
                            outcome.update(outcome="stopped", why="PhiXtra's Prospeo credits ran out")
                            results.append(outcome)
                            break
                        else:
                            outcome.update(outcome="failed", why=f"Prospeo: {code}")
                    else:
                        p = d.get("person") or {}
                        em = (p.get("email") or {})
                        mo = (p.get("mobile") or {})
                        email = em.get("email") if em.get("status") == "VERIFIED" and em.get("revealed") else None
                        mobile = (mo.get("mobile_international") or mo.get("mobile")) \
                            if req.get("get_mobile") and mo.get("status") == "VERIFIED" and mo.get("revealed") else None
                        if not email and not mobile:
                            outcome.update(outcome="none", why="No email or mobile found (not charged)")
                        else:
                            conn, cur = _db()
                            after = None
                            try:
                                lead_id, dup, after = add_one(cur, tenant_id, person, d, req)
                                if not dup:
                                    cur.execute("""INSERT INTO lead_finder_people
                                                       (tenant_id, person_id, lead_id, email_found, mobile_found, added_by)
                                                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                                                (tenant_id, person["person_id"], lead_id, bool(email), bool(mobile), actor_name))
                                conn.commit()
                            except Exception:
                                conn.rollback()
                                raise
                            finally:
                                cur.close(); conn.close()
                            if after:
                                try:
                                    after()
                                except Exception as e:
                                    print("⚠️ lead finder after-save:", e)
                            outcome.update(outcome="skipped" if dup else "added", lead_id=lead_id,
                                           email=email, mobile=mobile,
                                           why="Already in your Leads (same email or number)" if dup else "")
            except ProspeoError as e:
                outcome.update(outcome="failed", why=str(e))
            except Exception as e:
                print("⚠️ lead finder add:", e)
                outcome.update(outcome="failed", why="Couldn't save this one")
            results.append(outcome)
            _save_progress(job_id, results, credits)
        end = account().get("credits_left")
        if isinstance(start, int) and isinstance(end, int):
            credits = max(0, start - end)
        _save_progress(job_id, results, credits, finished=True)
    except Exception as e:
        print("⚠️ lead finder job:", e)
        _save_progress(job_id, results, credits, finished=True, error=str(e)[:300])


def _save_progress(job_id, results, credits, finished=False, error=None):
    conn, cur = _db()
    try:
        cur.execute("""UPDATE lead_finder_jobs SET done=%s, results=%s, credits=%s,
                              status=CASE WHEN %s THEN 'finished' ELSE status END,
                              finished_at=CASE WHEN %s THEN NOW() ELSE finished_at END,
                              error=COALESCE(%s, error)
                        WHERE id=%s""",
                    (len(results), json.dumps(results), credits, finished, finished, error, job_id))
        conn.commit()
    finally:
        cur.close(); conn.close()
