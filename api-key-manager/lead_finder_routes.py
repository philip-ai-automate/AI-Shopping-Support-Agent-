"""
lead_finder_routes.py — Leads › Find New Leads (pilot, 2026-10-19; approved
mock-up https://claude.ai/artifact/B9GV6PJGUHrxnw9wYLzbVJ). Prospeo work and
the background "add" jobs live in lead_finder_core.py.

  GET  /lead-finder                 search form + results (?run=1)
  GET  /lead-finder/locations?q=    country / city suggestions (JSON)
  GET  /lead-finder/counts?…        how many each "Who to import" choice gives (JSON)
  POST /lead-finder/add             import to Leads (background job): who + what to reveal
  GET  /lead-finder/jobs/<id>       that job's progress and what was added
  POST /lead-finder/jobs/<id>/stop  stop a running import
  POST /lead-finder/reveal          reveal the email / mobile of a person already on a lead

Import to Leads (approved design 2026-10-21, https://claude.ai/artifact/DKzWwiojf7TARdnKd21WY4):
who = all / with an email / with a mobile / with either / the ticked ones; the
name, job, company, LinkedIn link, industry, size, city and website come free
from the search, and an email (1 credit) or mobile (10) is revealed only when
ticked. Someone with nothing found is still imported with LinkedIn only.

Pilot: only the PLATFORM business (PLATFORM_TENANT_ID) — everyone else gets a
404, and the menu item and Roles ticks never show for them (the module is in
PLATFORM_ONLY_MODULES). Roles ticks: leadfinder.search (search, free for the
person; uses PhiXtra's Prospeo credits) and leadfinder.add (reveal + add).

What an added lead gets: business = company, contact = person, email, the
mobile in Phone (in the notes instead when our number check won't accept it) (and WhatsApp number when "Save the mobile as WhatsApp
number too" is ticked — Meta gives no way to check a number first), website,
business category (the business's own when Prospeo's industry matches one it
uses, else Prospeo's wording), staff count, city/country, job title + LinkedIn link in notes,
source "Lead Finder", the "Lead Finder" label (+ an optional one), New Lead
stage, and the business's own "Technologies" column filled from the WhatsApp
tools Prospeo saw on the company's website (only choices that already exist).
Someone already in Leads by the same email, number or LinkedIn link is not added again.
"""
import json
import time

import psycopg2.extras

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for

import accounts_core
import portal_address as _addr
import labels_core
import lead_finder_core as LF
import staff_activity
from db import get_db_connection
from feature_access import team_feature
from portal_routes import (_current_actor, _customer_id, _get_customer, _inject_granted_features,
                           _biz_field_options, _lead_assignees, _leads_see_all, _link_lead_to_contact,
                           _normalize_website_url,
                           _phone_entry, _require_login, _require_team_permission, _team_member_has_permission)

leadfinder_bp = Blueprint("leadfinder", __name__)
leadfinder_bp.context_processor(_inject_granted_features)

SEARCH, ADD = "leadfinder.search", "leadfinder.add"
DEFAULT_SENIORITY = ["Founder/Owner", "C-Suite", "Director", "Head"]
MAX_PER_ADD = 25
MAX_ADD_ALL = 1000          # "Add all" from one search, at most (user 2026-10-21)
_account_cache = [0.0, {}]


def _enabled_for(customer) -> bool:
    return bool(customer) and accounts_core.is_platform_tenant(customer.get("tenant_id"))


def _gate(key):
    r = _require_login()
    if r:
        return None, r
    customer = _get_customer(_customer_id())
    if not _enabled_for(customer):
        abort(404)
    return customer, _require_team_permission(key)


def _account():
    if time.time() - _account_cache[0] > 60:
        _account_cache[1] = LF.account()
        _account_cache[0] = time.time()
    return _account_cache[1]


def _form_from_args(a) -> dict:
    run = bool(a.get("run"))
    return {
        "locations": [x for x in a.getlist("loc") if x.strip()][:20] if run else ["Nigeria"],
        "industries": [x for x in a.getlist("ind") if x in LF.INDUSTRIES],
        "seniority": [x for x in a.getlist("sen") if x in LF.SENIORITIES] if run else list(DEFAULT_SENIORITY),
        "sizes": [x for x in a.getlist("size") if x in LF.HEADCOUNTS],
        "wa_tools": [x for x in a.getlist("wa") if x in LF.WA_TOOLS],
        "title": (a.get("title") or "").strip()[:300],
        "keyword": (a.get("kw") or "").strip()[:100],
        "one_per_company": (a.get("one") == "1") if run else True,
        # Email only for now (user 2026-10-21), so a fresh search starts on "Has email".
        "contact": (a.get("contact") if a.get("contact") in dict(LF.CONTACT_CHOICES) else "") if run else "email",
    }


@leadfinder_bp.route("/lead-finder", methods=["GET"])
@team_feature(SEARCH)
def page():
    customer, r = _gate(SEARCH)
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    f = _form_from_args(request.args)
    pg = request.args.get("page", "1")
    pg = int(pg) if pg.isdigit() and 0 < int(pg) <= 1000 else 1
    res, added = None, {}
    if request.args.get("run") and LF.configured():
        res = LF.search(tenant_id, f, pg)
        added = LF.already_added(tenant_id, [x["person_id"] for x in res["rows"]])
        known = _leads_by_domain(tenant_id, {d for x in res["rows"] for d in x["domains"]})
        for x in res["rows"]:
            x["in_company"] = next((known[d] for d in x["domains"] if d in known), None)
    me = _current_actor(customer)
    can_assign = _team_member_has_permission("leads.assign")
    people = _lead_assignees(tenant_id, customer) if can_assign else []
    args = request.args.to_dict(flat=False)
    args.pop("page", None)
    # Extra label: the business's existing labels to pick from (user 2026-10-21), "Lead Finder" is added anyway.
    conn = get_db_connection()
    try:
        label_names = [lb["name"] for lb in labels_core.visible_labels(conn.cursor(), tenant_id)
                       if lb["name"].strip().lower() != "lead finder"]
    finally:
        conn.close()
    return render_template("portal/lead_finder.html", label_names=label_names, customer=customer, f=f, res=res, page=pg, added=added,
                           page_args=args, configured=LF.configured(), account=_account(),
                           usage=LF.usage_this_month(tenant_id), jobs=LF.recent_jobs(tenant_id),
                           industries=LF.INDUSTRIES, seniorities=LF.SENIORITIES, headcounts=LF.HEADCOUNTS,
                           wa_tools=LF.WA_TOOLS, contact_choices=LF.CONTACT_CHOICES, can_add=_team_member_has_permission(ADD),
                           who_choices=LF.WHO_CHOICES,
                           can_assign=can_assign, people=people, me_key=me["key"], max_per_add=MAX_PER_ADD,
                           max_add_all=MAX_ADD_ALL)


@leadfinder_bp.route("/lead-finder/locations", methods=["GET"])
@team_feature(SEARCH)
def locations():
    customer, r = _gate(SEARCH)
    if r or _require_team_permission(SEARCH):
        return jsonify({"items": []}), 403
    try:
        return jsonify({"items": LF.locations(request.args.get("q") or "")})
    except LF.ProspeoError as e:
        return jsonify({"items": [], "error": str(e)})


@leadfinder_bp.route("/lead-finder/counts", methods=["GET"])
@team_feature(ADD)
def counts():
    customer, r = _gate(ADD)
    r = r or _require_team_permission(ADD)
    if r or not request.args.get("run"):
        return jsonify({}), 403 if r else 400
    try:
        return jsonify(LF.counts(int(customer["tenant_id"]), _form_from_args(request.args)))
    except LF.ProspeoError as e:
        return jsonify({"error": str(e)})


@leadfinder_bp.route("/lead-finder/add", methods=["POST"])
@team_feature(ADD)
def add():
    customer, r = _gate(ADD)
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    back = request.form.get("back") or url_for("leadfinder.page")
    if not back.startswith("/lead-finder"):
        back = url_for("leadfinder.page")
    try:
        picked = json.loads(request.form.get("people") or "[]")
    except ValueError:
        picked = []
    who = request.form.get("who") or "ticked"
    if who not in dict(LF.WHO_CHOICES):
        who = "ticked"
    add_all = who != "ticked"
    search_f = None
    if add_all:
        # The search is the one on the page the button was on (its address).
        from urllib.parse import urlsplit, parse_qs
        from werkzeug.datastructures import MultiDict
        q = parse_qs(urlsplit(back).query, keep_blank_values=False)
        search_f = _form_from_args(MultiDict([(k, v) for k, vs in q.items() for v in vs]))
        if not q.get("run"):
            flash("Run a search first.", "warning")
            return redirect(back)
        picked = []
    people, seen = [], set()
    for p in picked if isinstance(picked, list) else []:
        pid = str((p or {}).get("person_id") or "")[:40]
        if not pid or pid in seen:
            continue
        seen.add(pid)
        people.append({k: str(p.get(k) or "")[:200] for k in ("name", "title", "company")}
                      | {"linkedin": str(p.get("linkedin") or "")[:500], "person_id": pid, "src": LF.clean_src(p.get("src"))})
    if not people and not add_all:
        flash("Tick at least one person first.", "warning")
        return redirect(back)
    if len(people) > MAX_PER_ADD:
        flash(f"Tick up to {MAX_PER_ADD} people at a time.", "warning")
        return redirect(back)
    me = _current_actor(customer)
    akey = (request.form.get("assign") or "").strip()
    apeople = {a["key"]: a["label"] for a in _lead_assignees(tenant_id, customer)}
    if not _team_member_has_permission("leads.assign") or akey not in apeople:
        akey = ""
    if not _leads_see_all():
        akey = me["key"]          # staff who only see their own leads: what they add is theirs
    req = {
        "people": people,
        "who": who,
        "reveal_email": request.form.get("reveal_email") == "1",
        "get_mobile": request.form.get("reveal_mobile") == "1",
        "wa_too": request.form.get("reveal_mobile") == "1" and request.form.get("wa_too") == "1",
        "assign_key": akey, "assign_label": apeople.get(akey, ""),
        "label": (request.form.get("label_new") if request.form.get("label") == "__new__"
                  else request.form.get("label") or "").strip()[:60],
        "actor_key": me["key"], "actor_name": staff_activity.current_name(me["label"]) or me["label"],
        "back": back,
    }
    if add_all:
        req["all_limit"] = MAX_ADD_ALL
        req["search_f"] = search_f
    job_id = LF.start_job(tenant_id, me["key"], req["actor_name"], req, _add_one)
    return redirect(url_for("leadfinder.job", job_id=job_id))


@leadfinder_bp.route("/lead-finder/jobs/<int:job_id>", methods=["GET"])
@team_feature(SEARCH, ADD)
def job(job_id: int):
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    if not _enabled_for(customer):
        abort(404)
    if not (_team_member_has_permission(SEARCH) or _team_member_has_permission(ADD)):
        return _require_team_permission(ADD)
    j = LF.get_job(int(customer["tenant_id"]), job_id)
    if not j:
        abort(404)
    results = j["results"] or []
    counts = {k: sum(1 for x in results if x.get("outcome") == k) for k in ("added", "merged", "skipped", "none", "failed", "stopped")}
    return render_template("portal/lead_finder_job.html", customer=customer, j=j, results=results, counts=counts,
                           req=j["request"] or {})


@leadfinder_bp.route("/lead-finder/jobs/<int:job_id>/stop", methods=["POST"])
@team_feature(ADD)
def stop(job_id: int):
    customer, r = _gate(ADD)
    r = r or _require_team_permission(ADD)
    if r:
        return r
    if LF.request_stop(int(customer["tenant_id"]), job_id):
        flash("Stopping the import. Everyone imported so far stays in your Leads.", "success")
    return redirect(url_for("leadfinder.job", job_id=job_id))


@leadfinder_bp.route("/lead-finder/reveal", methods=["POST"])
@team_feature(ADD)
def reveal():
    """Reveal email (1 credit) or mobile (10) for a Lead Finder person already
    on a lead; only empty boxes on the lead are filled (same as importing)."""
    customer, r = _gate(ADD)
    r = r or _require_team_permission(ADD)
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    lead_id = request.form.get("lead_id", type=int) or 0
    pid = (request.form.get("person_id") or "")[:40]
    mobile = request.form.get("what") == "mobile"
    back = url_for("portal.lead_detail", lead_id=lead_id)
    person = next((x for x in LF.lead_people(tenant_id, lead_id) if x["person_id"] == pid), None)
    if not person:
        abort(404)
    try:
        d = LF._call("/enrich-person", LF.enrich_body(pid, True, mobile))
    except LF.ProspeoError as e:
        flash(str(e), "error")
        return redirect(back)
    if d.get("error"):
        code = d.get("error_code") or "unknown"
        flash("Your Lead Finder credits have run out." if code == "INSUFFICIENT_CREDITS" else
              f"Prospeo has no {'mobile' if mobile else 'email'} for {person['name'] or 'this person'} (not charged)."
              if code == "NO_MATCH" else f"Prospeo didn't answer ({code}). Try again later.",
              "warning")
        return redirect(back)
    email, mob = LF.revealed(d, mobile)
    if not email and not mob:
        flash(f"Prospeo has no {'mobile' if mobile else 'email'} for {person['name'] or 'this person'} (not charged).", "warning")
        return redirect(back)
    p, c = d.get("person") or {}, d.get("company") or {}
    phone, _err = _phone_entry(mob) if mob.startswith(("+", "00")) else ("", None)
    me = _current_actor(customer)
    req = {"get_mobile": mobile, "wa_too": False, "label": "", "actor_key": me["key"],
           "actor_name": staff_activity.current_name(me["label"]) or me["label"]}
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        after = _add_to_existing(cur, tenant_id, lead_id, p, c, {"name": person["name"], "title": person["title"]},
                                 email.lower(), phone, req, mob if mob and not phone else "")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()
    LF.mark_revealed(tenant_id, pid, bool(email), bool(mob))
    try:
        after()
    except Exception as e:
        print("⚠️ lead finder reveal log:", e)
    got = " and ".join(x for x in (f"email {email}" if email else "", f"mobile {mob}" if mob else "") if x)
    flash(f"Revealed {got}. Saved on this lead.", "success")
    return redirect(back)


# ── one person → one lead (runs inside the background job) ──────────────────
def _add_one(cur, tenant_id: int, person: dict, d: dict, req: dict):
    p, c = d.get("person") or {}, d.get("company") or {}
    em, mo = p.get("email") or {}, p.get("mobile") or {}
    email = (em.get("email") or "").strip().lower() if em.get("status") == "VERIFIED" and em.get("revealed") else ""
    mobile_raw = (mo.get("mobile_international") or mo.get("mobile") or "") \
        if req.get("get_mobile") and mo.get("status") == "VERIFIED" and mo.get("revealed") else ""
    phone, _err = _phone_entry(mobile_raw) if mobile_raw.startswith(("+", "00")) else ("", None)
    # A mobile Prospeo charged for but our number check won't accept still
    # goes in the notes, so it is never lost (2026-10-19).
    odd_mobile = mobile_raw.strip() if mobile_raw.strip() and not phone else ""

    # Already in Leads by the same email or number → leave it, don't add again.
    conds, params = [], [tenant_id]
    if email:
        conds.append("lower(email)=%s"); params.append(email)
    if phone:
        conds.append("regexp_replace(COALESCE(phone,''), '\\D', '', 'g')=%s"); params.append(phone)
        conds.append("regexp_replace(COALESCE(whatsapp_number,''), '\\D', '', 'g')=%s"); params.append(phone)
    if conds:
        cur.execute(f"SELECT id FROM merchant_pipeline_leads WHERE tenant_id=%s AND ({' OR '.join(conds)}) "
                    f"ORDER BY (dropped_at IS NULL) DESC, id DESC LIMIT 1", params)
        row = cur.fetchone()
        if row:
            return row["id"], "dup", None
    # …or the same LinkedIn profile (people imported without an email or number).
    li_url = (p.get("linkedin_url") or person.get("linkedin") or "").strip().rstrip("/").lower()
    if "linkedin.com/" in li_url:
        cur.execute("""SELECT l.id FROM merchant_pipeline_leads l LEFT JOIN lead_linkedin li ON li.lead_id = l.id
                        WHERE l.tenant_id=%s AND (rtrim(lower(COALESCE(li.profile_url,'')), '/')=%s
                              OR strpos(lower(COALESCE(l.notes,'')), %s) > 0)
                        ORDER BY (l.dropped_at IS NULL) DESC, l.id DESC LIMIT 1""", (tenant_id, li_url, li_url))
        row = cur.fetchone()
        if row:
            return row["id"], "dup", None

    # The company is already a lead (same website) → this person goes on that lead.
    existing = _leads_by_domain(tenant_id, LF.company_domains(c), cur=cur)
    if existing:
        lead_id = next(iter(existing.values()))["lead_id"]
        return lead_id, "merged", _add_to_existing(cur, tenant_id, lead_id, p, c, person, email, phone, req,
                                                   odd_mobile)

    loc = c.get("location") or {}
    name = (p.get("full_name") or person.get("name") or "").strip()
    company = (c.get("name") or person.get("company") or name or "Lead Finder lead").strip()[:200]
    title = (p.get("current_job_title") or person.get("title") or "").strip()
    linkedin = (p.get("linkedin_url") or person.get("linkedin") or "").strip()
    website = _normalize_website_url(c.get("domain") or c.get("website") or "")
    country = (loc.get("country_code") or "").upper()[:2] or None
    notes = "\n".join(x for x in [
        "Found with Lead Finder (Prospeo)." + ("" if email or phone or odd_mobile else " LinkedIn only: no email or mobile revealed."),
        f"Job title: {title}" if title else "",
        f"LinkedIn: {linkedin}" if linkedin else "",
        f"Company LinkedIn: {c['linkedin_url']}" if c.get("linkedin_url") else "",
        "Mobile not yet confirmed on WhatsApp." if phone and req.get("wa_too") else "",
        _odd_mobile_note(odd_mobile),
    ] if x)
    # The business's own category when Prospeo's industry matches one it uses.
    category = LF.category_for(c.get("industry") or "", _biz_field_options(tenant_id)["business_category"])
    cur.execute("""
        INSERT INTO merchant_pipeline_leads
            (tenant_id, customer_name, contact_person, phone, whatsapp_number, email, notes,
             assigned_to, assigned_key, source, website, business_category, employees, addr_city, addr_country)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'lead_finder',%s,%s,%s,%s,%s) RETURNING id""",
                (tenant_id, company, name[:200] or None, phone or None,
                 (phone if req.get("wa_too") else None) or None, email or None, notes,
                 req.get("assign_label") or None, req.get("assign_key") or None, website or None,
                 category[:120] or None, (c.get("employee_range") or "")[:40] or None,
                 (loc.get("city") or "")[:120] or None, country))
    lead_id = cur.fetchone()["id"]
    _link_lead_to_contact(cur, tenant_id, lead_id)
    for label_name in ("Lead Finder", req.get("label")):
        lid = labels_core.find_or_create(cur, tenant_id, label_name) if label_name else None
        if lid:
            cur.execute("INSERT INTO lead_label_leads (label_id, lead_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (lid, lead_id))
    _fill_technologies(cur, tenant_id, lead_id, c)
    cur.execute("""INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                   VALUES (%s, NULL, 'new_lead', %s, 'Added from Lead Finder')""", (lead_id, req.get("actor_name")))

    def after():
        staff_activity.log("lead_created", tenant_id, lead_id=lead_id,
                           actor_key=req.get("actor_key"), actor_name=req.get("actor_name"))
    return lead_id, "added", after


def _odd_mobile_note(mobile: str) -> str:
    return (f"Mobile from Prospeo: {mobile} (our number check didn't accept it, so it isn't in the "
            "phone box. Confirm it on a call.)") if mobile else ""


def _add_to_existing(cur, tenant_id: int, lead_id: int, p: dict, c: dict, person: dict,
                     email: str, phone: str, req: dict, odd_mobile: str = ""):
    """A decision-maker at a company that's already a lead: fill only the
    lead's EMPTY boxes (contact name, email, phone, WhatsApp number when
    asked), keep a different email under Other emails, and write who they
    are in the notes. Nothing already on the lead is replaced."""
    cur.execute("""SELECT contact_person, email, phone, whatsapp_number, other_emails, notes
                     FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s""", (lead_id, tenant_id))
    lead = cur.fetchone()
    name = (p.get("full_name") or person.get("name") or "").strip()[:200]
    title = (p.get("current_job_title") or person.get("title") or "").strip()
    linkedin = (p.get("linkedin_url") or person.get("linkedin") or "").strip()
    sets, vals = [], []
    if name and not (lead["contact_person"] or "").strip():
        sets.append("contact_person=%s"); vals.append(name)
    if email and not (lead["email"] or "").strip():
        sets.append("email=%s"); vals.append(email)
    elif email and email != (lead["email"] or "").strip().lower():
        sets.append("other_emails=%s"); vals.append(_addr.merge_other_emails(lead["other_emails"], [email], lead["email"]))
    if phone and not (lead["phone"] or "").strip():
        sets.append("phone=%s"); vals.append(phone)
    if phone and req.get("wa_too") and not (lead["whatsapp_number"] or "").strip():
        sets.append("whatsapp_number=%s"); vals.append(phone)
    note = "\n".join(x for x in [
        f"Lead Finder ({time.strftime('%d %b %Y')}): decision-maker {name}" + (f", {title}" if title else ""),
        f"Email: {email}" if email else "",
        f"Mobile: {phone and '+' + phone}" if phone else "",
        _odd_mobile_note(odd_mobile),
        f"LinkedIn: {linkedin}" if linkedin else "",
    ] if x)
    sets.append("notes=%s"); vals.append(((lead["notes"] or "").rstrip() + "\n\n" + note).strip())
    cur.execute(f"UPDATE merchant_pipeline_leads SET {', '.join(sets)} WHERE id=%s AND tenant_id=%s",
                vals + [lead_id, tenant_id])
    _link_lead_to_contact(cur, tenant_id, lead_id)
    for label_name in ("Lead Finder", req.get("label")):
        lid = labels_core.find_or_create(cur, tenant_id, label_name) if label_name else None
        if lid:
            cur.execute("INSERT INTO lead_label_leads (label_id, lead_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (lid, lead_id))
    _fill_technologies(cur, tenant_id, lead_id, c)

    def after():
        staff_activity.log("lead_edited", tenant_id, lead_id=lead_id, detail="Lead Finder: decision-maker added",
                           actor_key=req.get("actor_key"), actor_name=req.get("actor_name"))
    return after


# Website address of a lead, cleaned like lead_finder_core.host_of() (www., https://, path dropped).
_LEAD_HOST_SQL = ("lower(split_part(regexp_replace(regexp_replace(COALESCE(website,''), '^[a-zA-Z]+://', ''), "
                  "'^www\\.', ''), '/', 1))")


def _leads_by_domain(tenant_id: int, domains, cur=None) -> dict:
    """{domain: {'lead_id', 'name'}} for companies already in Leads, matched by
    the lead's website or its email's domain (not Gmail and the like). An
    open lead wins over a dropped one, then the newest."""
    domains = [d for d in (domains or []) if d]
    if not domains:
        return {}
    own = cur is None
    if own:
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(f"""
            SELECT DISTINCT ON (d) d, id AS lead_id, customer_name AS name
              FROM (SELECT id, customer_name, dropped_at,
                           unnest(ARRAY[{_LEAD_HOST_SQL},
                                        CASE WHEN lower(split_part(COALESCE(email,''), '@', 2)) <> ALL(%s)
                                             THEN lower(split_part(COALESCE(email,''), '@', 2)) END]) AS d
                      FROM merchant_pipeline_leads WHERE tenant_id=%s) x
             WHERE d = ANY(%s)
             ORDER BY d, (dropped_at IS NULL) DESC, id DESC""", (list(LF.FREE_MAIL), tenant_id, domains))
        return {r["d"]: {"lead_id": r["lead_id"], "name": r["name"]} for r in cur.fetchall()}
    finally:
        if own:
            cur.close(); conn.close()


def _fill_technologies(cur, tenant_id: int, lead_id: int, company: dict):
    """The business's own 'Technologies' column (if it has one): tick the
    choices for WhatsApp tools Prospeo saw on the company's website."""
    tech = company.get("technology") or {}
    names = tech.get("technology_names") if isinstance(tech, dict) else []
    wanted = {LF.TECH_TO_CHOICE[n.lower()] for n in (names or []) if n.lower() in LF.TECH_TO_CHOICE}
    if not wanted:
        return
    cur.execute("""SELECT id, choices FROM lead_columns WHERE tenant_id=%s AND visibility='business'
                     AND col_type='multi' AND lower(name)='technologies' ORDER BY id LIMIT 1""", (tenant_id,))
    col = cur.fetchone()
    if not col:
        return
    ids = [ch["id"] for ch in (col["choices"] or []) if ch.get("name") in wanted]
    if ids:
        # Keeps whatever the lead already had and adds the new ones.
        cur.execute("""INSERT INTO lead_column_values (column_id, lead_id, value) VALUES (%s,%s,%s)
                       ON CONFLICT (column_id, lead_id) DO UPDATE SET value =
                           (SELECT COALESCE(jsonb_agg(DISTINCT v), '[]'::jsonb)
                              FROM jsonb_array_elements(lead_column_values.value || EXCLUDED.value) v)""",
                    (col["id"], lead_id, json.dumps(ids)))


def menu_visible() -> bool:
    try:
        customer = _get_customer(_customer_id())
        return _enabled_for(customer) and _team_member_has_permission(SEARCH)
    except Exception:
        return False


def lead_finder_people_for(lead_id) -> list:
    """Lead page: the Lead Finder people on this lead, for Reveal email / mobile
    (only for people allowed to import)."""
    try:
        customer = _get_customer(_customer_id())
        if not (_enabled_for(customer) and _team_member_has_permission(ADD)):
            return []
        return LF.lead_people(int(customer["tenant_id"]), int(lead_id))
    except Exception:
        return []


@leadfinder_bp.app_context_processor
def _lead_finder_menu():
    if not session.get("portal_logged_in"):
        return {}
    return {"lead_finder_menu": menu_visible(), "lead_finder_people_for": lead_finder_people_for}
