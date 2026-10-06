"""Addresses for leads, contacts and companies (2026-10-04).

Five boxes everywhere: street, city / town, state / county / province,
country (ISO code, e.g. 'NG'), postcode. Country + state are required on
forms. A company's address belongs to the company: saving it on a lead or
contact that has a company saves it on the company and copies it to every
lead and contact of that company (save_address_family), so each list can
filter on its own row. Records without a company keep their own address.
"""
import re

import phonenumbers as _pn
try:
    from phonenumbers.geocoder import _region_display_name as _pn_region_name
except Exception:  # pragma: no cover
    _pn_region_name = None

ADDR_FIELDS = ("addr_street", "addr_city", "addr_state", "addr_country", "addr_postcode")
_MAX = {"addr_street": 255, "addr_city": 120, "addr_state": 120, "addr_country": 2, "addr_postcode": 20}

# Countries with a fixed list to pick from; every other country is typed.
ADDRESS_STATES = {
    "NG": ["Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue", "Borno",
           "Cross River", "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu", "Abuja (FCT)", "Gombe", "Imo",
           "Jigawa", "Kaduna", "Kano", "Katsina", "Kebbi", "Kogi", "Kwara", "Lagos", "Nasarawa",
           "Niger", "Ogun", "Ondo", "Osun", "Oyo", "Plateau", "Rivers", "Sokoto", "Taraba", "Yobe",
           "Zamfara"],
    "GH": ["Ahafo", "Ashanti", "Bono", "Bono East", "Central", "Eastern", "Greater Accra",
           "North East", "Northern", "Oti", "Savannah", "Upper East", "Upper West", "Volta",
           "Western", "Western North"],
    "US": ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
           "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
           "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland",
           "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana",
           "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
           "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
           "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
           "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming"],
    # England's ceremonial counties, Wales's preserved counties, Scotland's
    # council areas, Northern Ireland's counties.
    "GB": ["Bedfordshire", "Berkshire", "Bristol", "Buckinghamshire", "Cambridgeshire", "Cheshire",
           "City of London", "Cornwall", "County Durham", "Cumbria", "Derbyshire", "Devon", "Dorset",
           "East Riding of Yorkshire", "East Sussex", "Essex", "Gloucestershire", "Greater London",
           "Greater Manchester", "Hampshire", "Herefordshire", "Hertfordshire", "Isle of Wight",
           "Kent", "Lancashire", "Leicestershire", "Lincolnshire", "Merseyside", "Norfolk",
           "North Yorkshire", "Northamptonshire", "Northumberland", "Nottinghamshire",
           "Oxfordshire", "Rutland", "Shropshire", "Somerset", "South Yorkshire", "Staffordshire",
           "Suffolk", "Surrey", "Tyne and Wear", "Warwickshire", "West Midlands", "West Sussex",
           "West Yorkshire", "Wiltshire", "Worcestershire",
           "Clwyd", "Dyfed", "Gwent", "Gwynedd", "Mid Glamorgan", "Powys", "South Glamorgan",
           "West Glamorgan",
           "Aberdeen City", "Aberdeenshire", "Angus", "Argyll and Bute", "Clackmannanshire",
           "Dumfries and Galloway", "Dundee City", "East Ayrshire", "East Dunbartonshire",
           "East Lothian", "East Renfrewshire", "Edinburgh", "Falkirk", "Fife", "Glasgow City",
           "Highland", "Inverclyde", "Midlothian", "Moray", "Na h-Eileanan Siar", "North Ayrshire",
           "North Lanarkshire", "Orkney Islands", "Perth and Kinross", "Renfrewshire",
           "Scottish Borders", "Shetland Islands", "South Ayrshire", "South Lanarkshire",
           "Stirling", "West Dunbartonshire", "West Lothian",
           "County Antrim", "County Armagh", "County Down", "County Fermanagh",
           "County Londonderry", "County Tyrone"],
}
for _k in ADDRESS_STATES:
    ADDRESS_STATES[_k] = sorted(ADDRESS_STATES[_k])

# Other ways people write the same place (imports, typing).
_STATE_ALIASES = {
    "NG": {"fct": "Abuja (FCT)", "abuja": "Abuja (FCT)", "federal capital territory": "Abuja (FCT)",
           "fct abuja": "Abuja (FCT)", "abuja fct": "Abuja (FCT)", "lagos state": "Lagos",
           "akwa-ibom": "Akwa Ibom", "cross-river": "Cross River", "nassarawa": "Nasarawa"},
    "GB": {"london": "Greater London", "manchester": "Greater Manchester", "durham": "County Durham",
           "glasgow": "Glasgow City", "aberdeen": "Aberdeen City", "dundee": "Dundee City",
           "city of edinburgh": "Edinburgh", "antrim": "County Antrim", "armagh": "County Armagh",
           "down": "County Down", "fermanagh": "County Fermanagh", "londonderry": "County Londonderry",
           "derry": "County Londonderry", "tyrone": "County Tyrone", "birmingham": "West Midlands"},
    "GH": {"accra": "Greater Accra", "kumasi": "Ashanti"},
    "US": {"dc": "District of Columbia", "washington dc": "District of Columbia"},
}

_STATE_LABELS = {"NG": "State", "US": "State", "GB": "County", "GH": "Region", "KE": "County",
                 "IE": "County", "ZA": "Province", "CA": "Province", "CN": "Province"}

_POPULAR = ["NG", "GB", "GH", "US", "KE", "ZA"]


def _build_countries():
    rows = []
    for iso in _pn.SUPPORTED_REGIONS:
        name = (_pn_region_name(iso, "en") if _pn_region_name else "") or iso
        rows.append((iso, name))
    rows.sort(key=lambda r: r[1])
    popular = [r for p in _POPULAR for r in rows if r[0] == p]
    return popular, rows


ADDRESS_COUNTRIES_POPULAR, ADDRESS_COUNTRIES = _build_countries()
_COUNTRY_NAMES = {iso: name for iso, name in ADDRESS_COUNTRIES}
_COUNTRY_BY_NAME = {name.lower(): iso for iso, name in ADDRESS_COUNTRIES}
_COUNTRY_BY_NAME.update({"uk": "GB", "united kingdom": "GB", "great britain": "GB", "england": "GB",
                         "scotland": "GB", "wales": "GB", "northern ireland": "GB", "usa": "US",
                         "united states of america": "US", "america": "US", "naija": "NG"})


def country_name(iso) -> str:
    return _COUNTRY_NAMES.get((iso or "").upper(), "") if iso else ""


def state_label(iso) -> str:
    return _STATE_LABELS.get((iso or "").upper(), "State / Province")


def match_country(text):
    """'Nigeria' / 'NG' / 'uk' → ISO code, or None when not recognised."""
    t = re.sub(r"\s+", " ", (text or "").strip()).lower().strip(".")
    if not t:
        return None
    if len(t) == 2 and t.upper() in _COUNTRY_NAMES:
        return t.upper()
    return _COUNTRY_BY_NAME.get(t)


def match_state(iso, text):
    """The listed spelling for a typed state ('lagos state' → 'Lagos'), the
    typed text as written for countries without a list, or None when a
    listed country's state isn't recognised."""
    t = re.sub(r"\s+", " ", (text or "").strip()).strip(" ,.")
    if not t:
        return None
    iso = (iso or "").upper()
    if iso not in ADDRESS_STATES:
        return t[:120]
    low = t.lower()
    for s in ADDRESS_STATES[iso]:
        if s.lower() == low:
            return s
    hit = _STATE_ALIASES.get(iso, {}).get(low)
    if hit:
        return hit
    low2 = re.sub(r"\s+(state|county|region|province)$", "", low)
    for s in ADDRESS_STATES[iso]:
        if s.lower() == low2:
            return s
    return _STATE_ALIASES.get(iso, {}).get(low2)


def address_from_form(form, required=True, prefix="addr_"):
    """(address dict, error message or None) from the five form boxes.
    required: country + state must be filled (forms). An empty optional box
    is saved as None."""
    raw = {f: re.sub(r"\s+", " ", (form.get(prefix + f[5:]) or "")).strip() for f in ADDR_FIELDS}
    if not raw["addr_state"]:   # countries without a list: the typed box
        raw["addr_state"] = re.sub(r"\s+", " ", (form.get(prefix + "state_text") or "")).strip()
    iso = match_country(raw["addr_country"])
    if raw["addr_country"] and not iso:
        return None, "Choose the country from the list."
    state = None
    if raw["addr_state"]:
        state = match_state(iso, raw["addr_state"]) if iso else raw["addr_state"][:120]
        if iso and state is None:
            return None, f"Choose the {state_label(iso).lower()} from the list for {country_name(iso)}."
    if required and not (iso and state):
        return None, "Choose the country and state / county / province before saving."
    addr = {"addr_street": raw["addr_street"][:255] or None, "addr_city": raw["addr_city"][:120] or None,
            "addr_state": state, "addr_country": iso, "addr_postcode": raw["addr_postcode"][:20] or None}
    return addr, None


def location_text(row, with_street=False) -> str:
    """'Ikeja, Lagos, Nigeria' for lists (street too when asked)."""
    if not row:
        return ""
    get = row.get if hasattr(row, "get") else (lambda k: None)
    parts = []
    if with_street and get("addr_street"):
        parts.append(get("addr_street"))
    for k in ("addr_city", "addr_state"):
        if get(k):
            parts.append(get(k))
    if get("addr_country"):
        parts.append(country_name(get("addr_country")) or get("addr_country"))
    return ", ".join(parts)


def has_address(row) -> bool:
    return bool(row and row.get("addr_country") and row.get("addr_state"))


def _rowval(r, k, i):
    return r[k] if isinstance(r, dict) else r[i]


def save_address_family(cur, tenant_id, addr, lead_id=None, contact_id=None, company_id=None):
    """Save one address on the record it was typed for and on the records
    that are the same business: its company, every lead and contact of that
    company, the lead's contact and the contact's leads. Never touches
    merchant_pipeline_leads.updated_at (it drives the lead score).
    Caller commits. Returns the company id used (or None)."""
    tenant_id = int(tenant_id)
    lead_ids, contact_ids = set(), set()
    if lead_id:
        lead_ids.add(int(lead_id))
        cur.execute("SELECT company_id, wa_contact_id FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s",
                    (int(lead_id), tenant_id))
        r = cur.fetchone()
        if r:
            company_id = company_id or _rowval(r, "company_id", 0)
            if _rowval(r, "wa_contact_id", 1):
                contact_ids.add(_rowval(r, "wa_contact_id", 1))
    if contact_id:
        contact_ids.add(int(contact_id))
    if contact_ids and not company_id:
        cur.execute("SELECT company_id FROM wa_contacts WHERE tenant_id=%s AND id = ANY(%s) AND company_id IS NOT NULL LIMIT 1",
                    (tenant_id, list(contact_ids)))
        r = cur.fetchone()
        if r:
            company_id = _rowval(r, "company_id", 0)
    vals = [addr.get(f) for f in ADDR_FIELDS]
    sets = ", ".join(f"{f}=%s" for f in ADDR_FIELDS)
    if company_id:
        cur.execute(f"UPDATE crm_companies SET {sets}, updated_at=NOW() WHERE id=%s AND tenant_id=%s",
                    vals + [company_id, tenant_id])
        cur.execute("SELECT id FROM merchant_pipeline_leads WHERE tenant_id=%s AND company_id=%s", (tenant_id, company_id))
        lead_ids.update(_rowval(r, "id", 0) for r in cur.fetchall())
        cur.execute("SELECT id FROM wa_contacts WHERE tenant_id=%s AND company_id=%s", (tenant_id, company_id))
        contact_ids.update(_rowval(r, "id", 0) for r in cur.fetchall())
    if contact_ids:
        cur.execute("SELECT id FROM merchant_pipeline_leads WHERE tenant_id=%s AND wa_contact_id = ANY(%s)",
                    (tenant_id, list(contact_ids)))
        lead_ids.update(_rowval(r, "id", 0) for r in cur.fetchall())
    if lead_ids:
        cur.execute(f"UPDATE merchant_pipeline_leads SET {sets} WHERE tenant_id=%s AND id = ANY(%s)",
                    vals + [tenant_id, list(lead_ids)])
    if contact_ids:
        cur.execute(f"UPDATE wa_contacts SET {sets}, updated_at=NOW() WHERE tenant_id=%s AND id = ANY(%s)",
                    vals + [tenant_id, list(contact_ids)])
    return company_id


def copy_address_if_missing(cur, tenant_id, lead_id):
    """After a lead is joined to a contact / company: whichever side has an
    address gives it to the other, never overwriting an address."""
    cur.execute("""SELECT l.addr_country l_c, l.addr_state l_s, l.company_id, l.wa_contact_id,
                          co.addr_country co_c, co.addr_state co_s, w.addr_country w_c, w.addr_state w_s
                     FROM merchant_pipeline_leads l
                     LEFT JOIN crm_companies co ON co.id=l.company_id
                     LEFT JOIN wa_contacts w ON w.id=l.wa_contact_id
                    WHERE l.id=%s AND l.tenant_id=%s""", (int(lead_id), int(tenant_id)))
    r = cur.fetchone()
    if not r:
        return
    r = r if isinstance(r, dict) else dict(zip([d[0] for d in cur.description], r))
    src = None
    if r["co_c"]:
        src = ("crm_companies", r["company_id"])
    elif r["l_c"]:
        src = ("merchant_pipeline_leads", lead_id)
    elif r["w_c"]:
        src = ("wa_contacts", r["wa_contact_id"])
    if not src:
        return
    cur.execute(f"SELECT {', '.join(ADDR_FIELDS)} FROM {src[0]} WHERE id=%s AND tenant_id=%s", (src[1], int(tenant_id)))
    a = cur.fetchone()
    a = a if isinstance(a, dict) else dict(zip(ADDR_FIELDS, a))
    sets = ", ".join(f"{f}=%s" for f in ADDR_FIELDS)
    vals = [a.get(f) for f in ADDR_FIELDS]
    if r["company_id"] and not r["co_c"]:
        cur.execute(f"UPDATE crm_companies SET {sets} WHERE id=%s AND tenant_id=%s", vals + [r["company_id"], int(tenant_id)])
    if not r["l_c"]:
        cur.execute(f"UPDATE merchant_pipeline_leads SET {sets} WHERE id=%s AND tenant_id=%s", vals + [int(lead_id), int(tenant_id)])
    if r["wa_contact_id"] and not r["w_c"]:
        cur.execute(f"UPDATE wa_contacts SET {sets} WHERE id=%s AND tenant_id=%s", vals + [r["wa_contact_id"], int(tenant_id)])


def address_filter_clauses(alias, args):
    """WHERE pieces for the Country / State / City / Address-missing filters.
    Returns (clauses, params, state dict, fargs dict)."""
    country = match_country(args.get("country")) or ""
    state = (args.get("state") or "").strip()[:120]
    city = (args.get("city") or "").strip()[:120]
    missing = args.get("addr_missing") == "1"
    clauses, params = [], []
    if country:
        clauses.append(f"{alias}.addr_country=%s"); params.append(country)
    if state:
        clauses.append(f"lower({alias}.addr_state)=lower(%s)"); params.append(state)
    if city:
        clauses.append(f"{alias}.addr_city ILIKE %s"); params.append(f"%{city}%")
    if missing:
        clauses.append(f"(COALESCE({alias}.addr_country,'')='' OR COALESCE({alias}.addr_state,'')='')")
    fargs = {k: v for k, v in {"country": country, "state": state, "city": city,
                               "addr_missing": "1" if missing else ""}.items() if v}
    return clauses, params, {"country": country, "state": state, "city": city, "addr_missing": missing}, fargs


def address_filter_options(cur, table, tenant_id, extra_where="TRUE", alias="t"):
    """Countries and states that actually occur (with counts) for the filter
    pick-lists, plus how many records have no address."""
    cur.execute(f"""SELECT {alias}.addr_country, {alias}.addr_state, count(*) AS n FROM {table} {alias}
                     WHERE {alias}.tenant_id=%s AND {extra_where} AND COALESCE({alias}.addr_country,'')<>''
                     GROUP BY 1, 2""", (int(tenant_id),))
    by_country, states = {}, {}
    for r in cur.fetchall():
        c = _rowval(r, "addr_country", 0); s = _rowval(r, "addr_state", 1); n = _rowval(r, "n", 2)
        by_country[c] = by_country.get(c, 0) + n
        if s:
            states.setdefault(c, {})
            states[c][s] = states[c].get(s, 0) + n
    cur.execute(f"""SELECT count(*) AS n FROM {table} {alias} WHERE {alias}.tenant_id=%s AND {extra_where}
                     AND (COALESCE({alias}.addr_country,'')='' OR COALESCE({alias}.addr_state,'')='')""",
                (int(tenant_id),))
    r = cur.fetchone()
    missing = _rowval(r, "n", 0)
    countries = sorted(((c, country_name(c) or c, n) for c, n in by_country.items()), key=lambda x: (-x[2], x[1]))
    return {"countries": countries,
            "states": {c: sorted(v.items(), key=lambda x: (-x[1], x[0])) for c, v in states.items()},
            "missing": missing}


# ── Other emails (2026-10-04) ───────────────────────────────────────────────
_EMAIL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,12}$")


def parse_other_emails(text, main_email=None):
    """'a@x.com, b@y.com' / one per line → (cleaned comma list or None,
    list of entries that aren't emails). Drops repeats and the main email."""
    seen, bad = [], []
    main = (main_email or "").strip().lower()
    for part in re.split(r"[,;\s]+", text or ""):
        e = part.strip().strip(".").lower()
        if not e:
            continue
        if not _EMAIL_RE.match(e):
            bad.append(part.strip())
            continue
        if e != main and e not in seen:
            seen.append(e)
    return (", ".join(seen) or None), bad


def merge_other_emails(existing, new_list, main_email=None):
    """existing comma list + new emails → comma list (no repeats, no main)."""
    cur, _ = parse_other_emails(existing or "", main_email)
    items = cur.split(", ") if cur else []
    main = (main_email or "").strip().lower()
    for e in new_list or []:
        e = (e or "").strip().lower()
        if e and e != main and e not in items and _EMAIL_RE.match(e):
            items.append(e)
    return ", ".join(items) or None


def other_emails_list(value):
    return [e for e in (value or "").split(", ") if e]


def save_other_emails(cur, tenant_id, value, lead_id=None, contact_id=None):
    """Set Other emails on the record that was edited, and ADD any new ones to
    the same business's linked lead(s) / contact (never removes there)."""
    tenant_id = int(tenant_id)
    new = other_emails_list(value)
    if lead_id:
        cur.execute("UPDATE merchant_pipeline_leads SET other_emails=%s WHERE id=%s AND tenant_id=%s",
                    (value, int(lead_id), tenant_id))
        cur.execute("SELECT wa_contact_id FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s", (int(lead_id), tenant_id))
        r = cur.fetchone()
        cid = _rowval(r, "wa_contact_id", 0) if r else None
        if cid:
            cur.execute("SELECT email, other_emails FROM wa_contacts WHERE id=%s AND tenant_id=%s", (cid, tenant_id))
            c = cur.fetchone()
            if c:
                merged = merge_other_emails(_rowval(c, "other_emails", 1), new, _rowval(c, "email", 0))
                cur.execute("UPDATE wa_contacts SET other_emails=%s WHERE id=%s AND tenant_id=%s", (merged, cid, tenant_id))
    if contact_id:
        cur.execute("UPDATE wa_contacts SET other_emails=%s WHERE id=%s AND tenant_id=%s",
                    (value, int(contact_id), tenant_id))
        cur.execute("SELECT id, email, other_emails FROM merchant_pipeline_leads WHERE tenant_id=%s AND wa_contact_id=%s",
                    (tenant_id, int(contact_id)))
        for r in cur.fetchall():
            merged = merge_other_emails(_rowval(r, "other_emails", 2), new, _rowval(r, "email", 1))
            cur.execute("UPDATE merchant_pipeline_leads SET other_emails=%s WHERE id=%s AND tenant_id=%s",
                        (merged, _rowval(r, "id", 0), tenant_id))
