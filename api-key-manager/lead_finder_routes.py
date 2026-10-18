"""
lead_finder_routes.py — Leads › Find New Leads (pilot, 2026-10-19; approved
mock-up https://claude.ai/artifact/B9GV6PJGUHrxnw9wYLzbVJ). Prospeo work and
the background "add" jobs live in lead_finder_core.py.

  GET  /lead-finder                 search form + results (?run=1)
  GET  /lead-finder/locations?q=    country / city suggestions (JSON)
  POST /lead-finder/add             add the ticked people to Leads (background job)
  GET  /lead-finder/jobs/<id>       that job's progress and what was added

Pilot: only the PLATFORM business (PLATFORM_TENANT_ID) — everyone else gets a
404, and the menu item and Roles ticks never show for them (the module is in
PLATFORM_ONLY_MODULES). Roles ticks: leadfinder.search (search, free for the
person; uses PhiXtra's Prospeo credits) and leadfinder.add (reveal + add).

What an added lead gets: business = company, contact = person, email, the
mobile in Phone (and WhatsApp number when "Save the mobile as WhatsApp
number too" is ticked — Meta gives no way to check a number first), website,
industry, staff count, city/country, job title + LinkedIn link in notes,
source "Lead Finder", the "Lead Finder" label (+ an optional one), New Lead
stage, and the business's own "Technologies" column filled from the WhatsApp
tools Prospeo saw on the company's website (only choices that already exist).
Someone already in Leads by the same email or number is not added again.
"""
import json
import time

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for

import accounts_core
import labels_core
import lead_finder_core as LF
import staff_activity
from feature_access import team_feature
from portal_routes import (_current_actor, _customer_id, _get_customer, _inject_granted_features,
                           _lead_assignees, _leads_see_all, _link_lead_to_contact, _normalize_website_url,
                           _phone_entry, _require_login, _require_team_permission, _team_member_has_permission)

leadfinder_bp = Blueprint("leadfinder", __name__)
leadfinder_bp.context_processor(_inject_granted_features)

SEARCH, ADD = "leadfinder.search", "leadfinder.add"
DEFAULT_SENIORITY = ["Founder/Owner", "C-Suite", "Director", "Head"]
MAX_PER_ADD = 25
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
    me = _current_actor(customer)
    can_assign = _team_member_has_permission("leads.assign")
    people = _lead_assignees(tenant_id, customer) if can_assign else []
    args = request.args.to_dict(flat=False)
    args.pop("page", None)
    return render_template("portal/lead_finder.html", customer=customer, f=f, res=res, page=pg, added=added,
                           page_args=args, configured=LF.configured(), account=_account(),
                           usage=LF.usage_this_month(tenant_id), jobs=LF.recent_jobs(tenant_id),
                           industries=LF.INDUSTRIES, seniorities=LF.SENIORITIES, headcounts=LF.HEADCOUNTS,
                           wa_tools=LF.WA_TOOLS, can_add=_team_member_has_permission(ADD),
                           can_assign=can_assign, people=people, me_key=me["key"], max_per_add=MAX_PER_ADD)


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
    people, seen = [], set()
    for p in picked if isinstance(picked, list) else []:
        pid = str((p or {}).get("person_id") or "")[:40]
        if not pid or pid in seen:
            continue
        seen.add(pid)
        people.append({k: str(p.get(k) or "")[:200] for k in ("name", "title", "company", "linkedin")} | {"person_id": pid})
    if not people:
        flash("Tick at least one person first.", "warning")
        return redirect(back)
    if len(people) > MAX_PER_ADD:
        flash(f"Add up to {MAX_PER_ADD} people at a time.", "warning")
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
        "get_mobile": request.form.get("mobile") == "1",
        "wa_too": request.form.get("wa_too") == "1",
        "assign_key": akey, "assign_label": apeople.get(akey, ""),
        "label": (request.form.get("label") or "").strip()[:60],
        "actor_key": me["key"], "actor_name": staff_activity.current_name(me["label"]) or me["label"],
        "back": back,
    }
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
    counts = {k: sum(1 for x in results if x.get("outcome") == k) for k in ("added", "skipped", "none", "failed", "stopped")}
    return render_template("portal/lead_finder_job.html", customer=customer, j=j, results=results, counts=counts,
                           req=j["request"] or {})


# ── one person → one lead (runs inside the background job) ──────────────────
def _add_one(cur, tenant_id: int, person: dict, d: dict, req: dict):
    p, c = d.get("person") or {}, d.get("company") or {}
    em, mo = p.get("email") or {}, p.get("mobile") or {}
    email = (em.get("email") or "").strip().lower() if em.get("status") == "VERIFIED" and em.get("revealed") else ""
    mobile_raw = (mo.get("mobile_international") or mo.get("mobile") or "") \
        if req.get("get_mobile") and mo.get("status") == "VERIFIED" and mo.get("revealed") else ""
    phone, _err = _phone_entry(mobile_raw) if mobile_raw.startswith(("+", "00")) else ("", None)

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
            return row["id"], True, None

    loc = c.get("location") or {}
    name = (p.get("full_name") or person.get("name") or "").strip()
    company = (c.get("name") or person.get("company") or name or "Lead Finder lead").strip()[:200]
    title = (p.get("current_job_title") or person.get("title") or "").strip()
    linkedin = (p.get("linkedin_url") or person.get("linkedin") or "").strip()
    website = _normalize_website_url(c.get("domain") or c.get("website") or "")
    country = (loc.get("country_code") or "").upper()[:2] or None
    notes = "\n".join(x for x in [
        "Found with Lead Finder (Prospeo).",
        f"Job title: {title}" if title else "",
        f"LinkedIn: {linkedin}" if linkedin else "",
        f"Company LinkedIn: {c['linkedin_url']}" if c.get("linkedin_url") else "",
        "Mobile not yet confirmed on WhatsApp." if phone and req.get("wa_too") else "",
    ] if x)
    cur.execute("""
        INSERT INTO merchant_pipeline_leads
            (tenant_id, customer_name, contact_person, phone, whatsapp_number, email, notes,
             assigned_to, assigned_key, source, website, business_category, employees, addr_city, addr_country)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'lead_finder',%s,%s,%s,%s,%s) RETURNING id""",
                (tenant_id, company, name[:200] or None, phone or None,
                 (phone if req.get("wa_too") else None) or None, email or None, notes,
                 req.get("assign_label") or None, req.get("assign_key") or None, website or None,
                 (c.get("industry") or "")[:120] or None, (c.get("employee_range") or "")[:40] or None,
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
    return lead_id, False, after


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
        cur.execute("""INSERT INTO lead_column_values (column_id, lead_id, value) VALUES (%s,%s,%s)
                       ON CONFLICT (column_id, lead_id) DO NOTHING""", (col["id"], lead_id, json.dumps(ids)))


def menu_visible() -> bool:
    try:
        customer = _get_customer(_customer_id())
        return _enabled_for(customer) and _team_member_has_permission(SEARCH)
    except Exception:
        return False


@leadfinder_bp.app_context_processor
def _lead_finder_menu():
    if not session.get("portal_logged_in"):
        return {}
    return {"lead_finder_menu": menu_visible()}
