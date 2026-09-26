"""
social_calendar_routes.py — Social Posts › Overview, Calendar and Approval (2026-09-26).
Rules live in social_workflow.py; sending still goes through
buffer_routes._send_to_buffer like every other post.

  GET  /social-posts/overview                the owner's summary (?month=YYYY-MM)
  GET  /social-posts/analytics               Buffer's numbers for the month's sent posts (?month=YYYY-MM)
  POST /social-posts/analytics/refresh       ask Buffer for the latest numbers now
  GET  /social-posts/calendar                month view (?month=YYYY-MM)
  GET  /social-posts/calendar/plan           start a post for a day (?date=YYYY-MM-DD&via=ai|upload)
  GET  /social-posts/plan-month              "Plan my month with AI" form; POST makes the plan
  GET  /social-posts/plan-month/<id>         the AI's suggestions, to pick from
  POST /social-posts/plan-month/<id>/add     add the picked suggestions as drafts
  GET  /social-posts/<id>/make-picture       give a planned draft its picture (?via=ai|upload)
  GET  /social-posts/<id>/view               one post: preview, status, history, actions
  GET  /social-posts/approval                posts waiting for approval + the on/off switch
  POST /social-posts/approval/settings       switch approval on / off
  POST /social-posts/<id>/approve            approve: sends it to Buffer
  POST /social-posts/<id>/request-changes    send it back with a note
"""
import json as _json
from datetime import datetime, timezone, timedelta, date

import psycopg2.extras
from flask import Blueprint, request, render_template, redirect, url_for, flash, session, abort

from db import get_db_connection, insert_audit_log
from feature_access import team_feature
from portal_routes import (_require_login, _require_team_permission, _team_member_has_permission,
                           _current_actor, _inject_granted_features, _inject_connect_flag)
import buffer_accounts as ba
import social_workflow as W
import social_analytics as SA
import social_ai_planner as PL
from buffer_routes import _gate, _owner, _get_post, _send_to_buffer, _validate, _parse_when

socialcal_bp = Blueprint("socialcal", __name__)
socialcal_bp.context_processor(_inject_granted_features)
socialcal_bp.context_processor(_inject_connect_flag)


def _ctx():
    """After the route's own login + team-role check: plan and Buffer
    connected. Returns (response, customer)."""
    r, customer = _gate("social.posts_view", "Social Posts")
    if r:
        return r, None
    if not ba.is_connected(_owner(customer)):
        flash("Connect Buffer first on Integration › Buffer. Social Posts publishes through it.", "warning")
        return redirect(url_for("buffer.connect")), None
    return None, customer


def _channels(customer) -> dict:
    return {c["channel_id"]: c for c in ba.list_channels(_owner(customer))}


def _card(p, chans: dict, today=None) -> dict:
    """What the calendar / approval pages need about one post."""
    k = W.display_key(p)
    d = W.post_date(p)
    return {
        "owner": W.owner_name(p),
        "owner_key": p.get("owner_key") or "",
        "due": p["due_date"].isoformat() if p.get("due_date") else None,
        "overdue": bool(today and W.is_overdue(p, today)),
        "id": p["id"],
        "title": W.title_of(p),
        "key": k,
        "label": W.DISPLAY[k][0],
        "when": d.isoformat() if d else None,
        "by": p.get("submitted_by") or p.get("created_by") or "",
        "services": [chans[c]["service"] for c in (p.get("channel_ids") or []) if c in chans],
        "has_image": bool(p.get("image_filename")),
    }


# ── Calendar ───────────────────────────────────────────────────────────────

@socialcal_bp.route("/social-posts/calendar", methods=["GET"])
@team_feature("social.posts_view")
def calendar():
    r = _require_login() or _require_team_permission("social.posts_view")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    raw = request.args.get("month", "")
    try:
        first = datetime.strptime(raw, "%Y-%m").date().replace(day=1)
    except ValueError:
        first = date.today().replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    # A day either side, so posts near midnight land right in the viewer's own time zone.
    start = datetime.combine(first, datetime.min.time(), timezone.utc) - timedelta(days=1)
    end = datetime.combine(nxt, datetime.min.time(), timezone.utc) + timedelta(days=1)

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT * FROM tenant_social_posts WHERE tenant_id=%s AND (
                       (status IN ('sent','partial') AND sent_at >= %s AND sent_at < %s)
                    OR (NOT (status IN ('sent','partial') AND sent_at IS NOT NULL)
                        AND COALESCE(scheduled_for, CASE WHEN status <> 'draft' THEN created_at END) >= %s
                        AND COALESCE(scheduled_for, CASE WHEN status <> 'draft' THEN created_at END) < %s))
                   ORDER BY 1""", (tenant_id, start, end, start, end))
    dated = cur.fetchall() or []
    cur.execute("""SELECT * FROM tenant_social_posts WHERE tenant_id=%s AND status='draft' AND scheduled_for IS NULL
                   ORDER BY created_at DESC LIMIT 50""", (tenant_id,))
    undated = cur.fetchall() or []
    cur.close(); conn.close()

    chans = _channels(customer)
    today = W.today_for(customer)
    cards = [_card(p, chans, today) for p in dated]
    undated_cards = [_card(p, chans, today) for p in undated]
    owners = sorted({(c["owner_key"], c["owner"]) for c in cards + undated_cards if c["owner"]}, key=lambda o: o[1].lower())
    return render_template(
        "portal/social_calendar.html",
        customer=customer,
        month=first, prev=prev.strftime("%Y-%m"), next=nxt.strftime("%Y-%m"),
        this_month=date.today().replace(day=1) == first,
        cards=cards,
        undated=undated_cards,
        owners=owners,
        overdue_count=sum(1 for c in cards + undated_cards if c["overdue"]),
        display=W.DISPLAY,
        can_create=_team_member_has_permission("social.posts_create"),
        can_ai=_team_member_has_permission("social.ai_designs"),
        can_plan=_team_member_has_permission("social.ai_plan"),
    )


# ── Overview ───────────────────────────────────────────────────────────────

# Where a month's posts are: (bucket, label, colour). Colours match the calendar.
OV_BUCKETS = [("draft", "Draft", "#667085"), ("changes", "Changes requested", "#F04438"),
              ("waiting", "Awaiting approval", "#F79009"), ("sched", "Scheduled", "#7A5AF8"),
              ("out", "Published", "#12B76A"), ("failed", "Failed", "#D92D20")]


def _bucket(p) -> str:
    k = W.display_key(p)
    return {"publishing": "sched", "sent": "out", "partial": "out"}.get(k, k)


def _who(p) -> str:
    """Same person key the Content page's Person filter uses."""
    return p.get("owner_key") or "name:" + W.owner_name(p)


def _month(today):
    """(first day, previous month's first, next month's first, and a UTC
    window a day wider each side) for ?month=YYYY-MM, default this month.
    Callers trim to the month in the business's own time zone."""
    try:
        first = datetime.strptime(request.args.get("month", ""), "%Y-%m").date().replace(day=1)
    except ValueError:
        first = today.replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    start = datetime.combine(first, datetime.min.time(), timezone.utc) - timedelta(days=1)
    end = datetime.combine(nxt, datetime.min.time(), timezone.utc) + timedelta(days=1)
    return first, prev, nxt, start, end


@socialcal_bp.route("/social-posts/overview", methods=["GET"])
@team_feature("social.posts_view")
def overview():
    """The owner's summary: the month's posts by stage, what needs someone
    now, and each person's share."""
    r = _require_login() or _require_team_permission("social.posts_view")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    tz = W.tz_for(customer)
    today = W.today_for(customer)
    first, prev, nxt, start, end = _month(today)

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Same "which day does a post belong to" rule as the calendar, then
    # trimmed to the month in the business's own time zone.
    cur.execute("""SELECT * FROM tenant_social_posts WHERE tenant_id=%s AND (
                       (status IN ('sent','partial') AND sent_at >= %s AND sent_at < %s)
                    OR (NOT (status IN ('sent','partial') AND sent_at IS NOT NULL)
                        AND COALESCE(scheduled_for, CASE WHEN status <> 'draft' THEN created_at END) >= %s
                        AND COALESCE(scheduled_for, CASE WHEN status <> 'draft' THEN created_at END) < %s))""",
                (tenant_id, start, end, start, end))
    month_posts = [p for p in cur.fetchall() or []
                   if W.post_date(p) and W.post_date(p).astimezone(tz).date().replace(day=1) == first]
    # "Needs attention" is about right now, whatever month a post is planned for.
    cur.execute("""SELECT * FROM tenant_social_posts WHERE tenant_id=%s AND (
                       (status='draft' AND approval IN ('waiting','changes'))
                    OR status='failed'
                    OR (status='draft' AND due_date < %s AND approval IS DISTINCT FROM 'waiting'))
                   ORDER BY COALESCE(scheduled_for, submitted_at, created_at), id""", (tenant_id, today))
    open_posts = cur.fetchall() or []
    cur.execute("""SELECT * FROM tenant_social_posts WHERE tenant_id=%s AND status='scheduled' AND scheduled_for > NOW()
                   ORDER BY scheduled_for LIMIT 5""", (tenant_id,))
    upcoming = cur.fetchall() or []
    cur.execute("""SELECT COUNT(*) AS n FROM tenant_social_posts
                   WHERE tenant_id=%s AND status='draft' AND scheduled_for IS NULL""", (tenant_id,))
    undated = cur.fetchone()["n"]
    cur.close(); conn.close()

    counts = {b: 0 for b, _, _ in OV_BUCKETS}
    for p in month_posts:
        counts[_bucket(p)] += 1
    overdue = [p for p in open_posts if W.is_overdue(p, today)]
    waiting = [p for p in open_posts if p["status"] == "draft" and p.get("approval") == "waiting"]
    changes = [p for p in open_posts if p["status"] == "draft" and p.get("approval") == "changes"]
    failed = [p for p in open_posts if p["status"] == "failed"]

    # A row for everyone who can be given posts, plus anyone still holding
    # posts who has since left the team.
    blank = lambda k, name: {"key": k, "name": name, "total": 0, "draft": 0, "waiting": 0,
                             "sched": 0, "out": 0, "overdue": 0}
    team = {m["key"]: blank(m["key"], m["label"]) for m in W.people(customer)}
    def row(p):
        k = _who(p)
        return team.setdefault(k, blank(k, W.owner_name(p) or "No one"))
    for p in month_posts:
        t = row(p)
        t["total"] += 1
        b = _bucket(p)
        t[b if b in ("waiting", "sched", "out") else "draft"] += 1   # changes/failed still need work
    for p in overdue:
        row(p)["overdue"] += 1

    chans = _channels(customer)
    cards = lambda rows: [_card(p, chans, today) for p in rows]
    approval_on = W.approval_required(tenant_id)
    return render_template(
        "portal/social_overview.html",
        customer=customer, today=today,
        month=first, prev=prev.strftime("%Y-%m"), next=nxt.strftime("%Y-%m"),
        this_month=today.replace(day=1) == first,
        total=len(month_posts), counts=counts, buckets=OV_BUCKETS,
        show_approval=bool(approval_on or counts["waiting"] or counts["changes"] or waiting or changes),
        waiting=cards(waiting), changes=cards(changes), overdue=cards(overdue), failed=cards(failed),
        upcoming=cards(upcoming), undated=undated,
        team=sorted(team.values(), key=lambda t: (-t["total"], -t["overdue"], t["name"].lower())),
        can_approve=W.can_approve(),
        can_create=_team_member_has_permission("social.posts_create"),
        can_ai=_team_member_has_permission("social.ai_designs"),
    )


# ── Analytics ──────────────────────────────────────────────────────────────

@socialcal_bp.route("/social-posts/analytics", methods=["GET"])
@team_feature("social.analytics_view")
def analytics():
    """Buffer's numbers for each post that went out this month, per account."""
    r = _require_login() or _require_team_permission("social.analytics_view")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    problem = SA.refresh(tenant_id)
    if problem:
        flash(problem, "warning")
    tz = W.tz_for(customer)
    today = W.today_for(customer)
    first, prev, nxt, start, end = _month(today)

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT * FROM tenant_social_posts WHERE tenant_id=%s AND status IN ('sent','partial')
                     AND sent_at >= %s AND sent_at < %s ORDER BY sent_at DESC, id DESC""", (tenant_id, start, end))
    posts = [p for p in cur.fetchall() or [] if p["sent_at"].astimezone(tz).date().replace(day=1) == first]
    cur.close(); conn.close()

    chans = _channels(customer)
    numbers = SA.stored(tenant_id, [p["id"] for p in posts])
    rows, accounts, kinds = [], {}, set()
    for p in posts:
        results = p.get("channel_results") or {}
        if isinstance(results, str):
            results = _json.loads(results)
        lines = []
        for ch in p.get("channel_ids") or []:
            if (results.get(ch) or {}).get("status") == "failed":
                continue                      # never went out on this account
            c = chans.get(ch) or {}
            got = numbers.get((p["id"], ch))
            m = (got or {}).get("metrics") or {}
            kinds.update(m)
            a = accounts.setdefault(ch, {"id": ch, "service": c.get("service") or "", "name": c.get("name") or "Removed account",
                                         "label": ba.service_label(c.get("service") or ""), "colour": ba.service_colour(c.get("service") or ""),
                                         "posts": 0, "with_numbers": 0, "totals": {}, "rates": []})
            a["posts"] += 1
            if m:
                a["with_numbers"] += 1
                for k, v in m.items():
                    if k == SA.RATE:
                        a["rates"].append(v)
                    else:
                        a["totals"][k] = a["totals"].get(k, 0) + v
            lines.append({"account": a, "metrics": m, "updated": (got or {}).get("updated"),
                          "link": (results.get(ch) or {}).get("link")})
        if lines:
            rows.append({"post": p, "title": W.title_of(p), "lines": lines})
    for a in accounts.values():
        a["gives"] = set(a["totals"]) | ({SA.RATE} if a["rates"] else set())
        if a["rates"]:
            a["totals"][SA.RATE] = sum(a["rates"]) / len(a["rates"])
    cols = SA.order(kinds)
    updated = [ln["updated"] for r_ in rows for ln in r_["lines"] if ln["updated"]]
    return render_template(
        "portal/social_analytics.html",
        customer=customer,
        month=first, prev=prev.strftime("%Y-%m"), next=nxt.strftime("%Y-%m"),
        this_month=today.replace(day=1) == first,
        rows=rows, accounts=sorted(accounts.values(), key=lambda a: (a["label"], a["name"])),
        cols=cols, label_for=SA.label_for, explain={k: e for k, _, e in SA.METRICS}, rate=SA.RATE,
        buffer_updated=max(updated) if updated else None,
        checked=SA.checked_at(tenant_id),
    )


@socialcal_bp.route("/social-posts/analytics/refresh", methods=["POST"])
@team_feature("social.analytics_view")
def analytics_refresh():
    r = _require_login() or _require_team_permission("social.analytics_view")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    problem = SA.refresh(int(customer["tenant_id"]), force=True)
    flash(problem or "Checked with Buffer. These are the latest numbers it has.", "warning" if problem else "success")
    month = request.form.get("month", "")
    return redirect(url_for("socialcal.analytics", **({"month": month} if month else {})))


# ── Plan my month with AI ──────────────────────────────────────────────────

def _plan_form_ctx(customer, **extra):
    tid = int(customer["tenant_id"])
    today = W.today_for(customer)
    source = PL.ai_source(customer)
    chans = [dict(c, label=ba.service_label(c["service"]), colour=ba.service_colour(c["service"]))
             for c in ba.list_channels(_owner(customer), enabled_only=True) if not c.get("is_disconnected")]
    return dict(customer=customer, months=PL.month_choices(today), channels=chans,
                people=W.people(customer), me=_current_actor(customer)["key"], themes=PL.THEMES,
                weekdays=PL.WEEKDAYS, counts=PL.COUNTS, own_key=source == "own_key",
                plans_left=max(0, PL.MAX_PLANS_PER_MONTH - PL.plans_this_month(tid)),
                max_plans=PL.MAX_PLANS_PER_MONTH, recent=PL.recent_plans(tid),
                knows_little=PL.knows_little(customer), **extra)


@socialcal_bp.route("/social-posts/plan-month", methods=["GET", "POST"])
@team_feature("social.ai_plan")
def plan_month():
    r = _require_login() or _require_team_permission("social.ai_plan")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    ctx = _plan_form_ctx(customer)
    f = request.form
    if request.method == "GET":
        want = request.args.get("month", "")
        month = next((m for m in ctx["months"] if m.strftime("%Y-%m") == want), None)
        return render_template("portal/social_plan_month.html", form={}, month=month, **ctx)

    def again(msg):
        flash(msg, "warning")
        return render_template("portal/social_plan_month.html", form=f, month=None, **ctx)
    month = next((m for m in ctx["months"] if m.strftime("%Y-%m") == f.get("month")), None)
    if not month:
        return again("Pick the month to plan.")
    count = int(f.get("count")) if (f.get("count") or "").isdigit() else 0
    if count not in PL.COUNTS:
        return again("Pick how many posts.")
    picked = set(f.getlist("channel_ids"))
    channels = [c for c in ctx["channels"] if c["channel_id"] in picked]
    if not channels:
        return again("Pick at least one social account.")
    weekdays = sorted({int(d) for d in f.getlist("weekdays") if d.isdigit() and int(d) < 7})
    if not weekdays:
        return again("Pick at least one day of the week to post on.")
    try:
        time_text = datetime.strptime(f.get("time") or "10:00", "%H:%M").strftime("%H:%M")
    except ValueError:
        return again("The posting time isn't a real time.")
    themes = [k for k, _ in PL.THEMES if k in f.getlist("themes")]
    owner = next((p for p in ctx["people"] if p["key"] == f.get("owner_key")), None)
    if not owner:
        return again("Pick who the posts are for.")
    due_raw = f.get("due_days", "2")
    due_days = int(due_raw) if due_raw.isdigit() and int(due_raw) <= 14 else None
    try:
        plan_id = PL.make_plan(customer, _current_actor(customer)["label"], month=month, count=count,
                               channels=channels, weekdays=weekdays, time_text=time_text, themes=themes,
                               focus=(f.get("focus") or "").strip()[:600], owner=owner, due_days=due_days)
    except PL.PlanError as e:
        return again(str(e))
    return redirect(url_for("socialcal.plan_review", plan_id=plan_id))


@socialcal_bp.route("/social-posts/plan-month/<int:plan_id>", methods=["GET"])
@team_feature("social.ai_plan")
def plan_review(plan_id: int):
    r = _require_login() or _require_team_permission("social.ai_plan")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    plan = PL.get_plan(int(customer["tenant_id"]), plan_id)
    if not plan:
        abort(404)
    chans = _channels(customer)
    accounts = [dict(chans[c], label=ba.service_label(chans[c]["service"]), colour=ba.service_colour(chans[c]["service"]))
                for c in plan["settings"].get("channel_ids") or [] if c in chans]
    return render_template("portal/social_plan_review.html", customer=customer, plan=plan, items=plan["items"],
                           accounts=accounts, people=W.people(customer), theme_names=PL.THEME_NAMES,
                           tomorrow=W.today_for(customer) + timedelta(days=1),
                           left=sum(1 for it in plan["items"] if not it.get("post_id")))


@socialcal_bp.route("/social-posts/plan-month/<int:plan_id>/add", methods=["POST"])
@team_feature("social.ai_plan")
def plan_add(plan_id: int):
    r = _require_login() or _require_team_permission("social.ai_plan")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    plan = PL.get_plan(int(customer["tenant_id"]), plan_id)
    if not plan:
        abort(404)
    back = url_for("socialcal.plan_review", plan_id=plan_id)
    people = {p["key"]: p for p in W.people(customer)}
    tomorrow = W.today_for(customer) + timedelta(days=1)
    picks, skipped = [], 0
    for raw in request.form.getlist("pick"):
        if not raw.isdigit():
            continue
        i = int(raw)
        day = W.parse_due(request.form.get(f"date_{i}"))
        person = people.get(request.form.get(f"owner_{i}") or plan["settings"].get("owner_key"))
        if not day or day < tomorrow or not person:
            skipped += 1
            continue
        picks.append({"i": i, "date": day, "owner": person})
    if not picks:
        flash("Tick at least one post, with a day from tomorrow onwards." if not skipped else
              "None were added: each needs a day from tomorrow onwards and a person responsible.", "warning")
        return redirect(back)
    added = PL.add_drafts(customer, plan, picks, _current_actor(customer)["label"])
    msg = f"Added {added} draft{'s' if added != 1 else ''} to the calendar."
    if skipped:
        msg += f" {skipped} weren't added: each needs a day from tomorrow onwards and a person responsible."
    flash(msg, "success" if not skipped else "warning")
    first = min(p["date"] for p in picks)
    return redirect(url_for("socialcal.calendar", month=first.strftime("%Y-%m")))


@socialcal_bp.route("/social-posts/<int:post_id>/make-picture", methods=["GET"])
@team_feature("social.ai_designs", "social.posts_edit")
def make_picture(post_id: int):
    """A planned draft has no picture yet: open the AI Post Designer or Upload
    Design with it filled in; their last step fills this draft."""
    via = request.args.get("via") or "ai"
    r = (_require_login() or _require_team_permission("social.posts_edit")
         or _require_team_permission("social.ai_designs" if via == "ai" else "social.posts_create"))
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    p = _get_post(int(customer["tenant_id"]), post_id)
    if not p:
        abort(404)
    if p["status"] != "draft" or p.get("approval") == "waiting":
        flash("This post can't be changed now: it's waiting for approval or has already gone to Buffer.", "warning")
        return redirect(url_for("socialcal.post_view", post_id=post_id))
    PL.start_fill(p)
    return redirect(url_for("social.new_post") if via == "ai" else url_for("upload.start"))


@socialcal_bp.route("/social-posts/calendar/plan", methods=["GET"])
@team_feature("social.posts_create", "social.ai_designs")
def calendar_plan():
    """"+" on a calendar day: remembers the day, then opens New Post or
    Upload Design, which pre-fill "Schedule for later" with it."""
    via = request.args.get("via") or ("ai" if _team_member_has_permission("social.ai_designs") else "upload")
    r = _require_login() or _require_team_permission("social.ai_designs" if via == "ai" else "social.posts_create")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    try:
        day = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
        session["social_plan_date"] = f"{day.isoformat()}|{int(datetime.now(timezone.utc).timestamp())}"
    except ValueError:
        session.pop("social_plan_date", None)
    return redirect(url_for("social.new_post") if via == "ai" else url_for("upload.start"))


# ── One post ───────────────────────────────────────────────────────────────

@socialcal_bp.route("/social-posts/<int:post_id>/view", methods=["GET"])
@team_feature("social.posts_view")
def post_view(post_id: int):
    r = _require_login() or _require_team_permission("social.posts_view")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    p = _get_post(tenant_id, post_id)
    if not p:
        abort(404)
    chans = _channels(customer)
    today = W.today_for(customer)
    can_assign = _team_member_has_permission("social.posts_edit") and p["status"] in ("draft", "scheduled", "failed")
    return render_template(
        "portal/social_post_view.html",
        customer=customer, p=p, card=_card(p, chans, today), chans=chans,
        people=W.people(customer) if can_assign else [], can_assign=can_assign,
        history=W.events(tenant_id, post_id), event_text=W.EVENT_TEXT, display=W.DISPLAY,
        can_edit=_team_member_has_permission("social.posts_edit"),
        can_ai=_team_member_has_permission("social.ai_designs"),
        can_create=_team_member_has_permission("social.posts_create"),
        can_approve=W.can_approve(),
        now=datetime.now(timezone.utc),
    )


# ── Approval ───────────────────────────────────────────────────────────────

@socialcal_bp.route("/social-posts/approval", methods=["GET"])
@team_feature("social.posts_view")
def approval():
    r = _require_login() or _require_team_permission("social.posts_view")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT * FROM tenant_social_posts WHERE tenant_id=%s AND status='draft' AND approval='waiting'
                   ORDER BY COALESCE(scheduled_for, submitted_at), id""", (tenant_id,))
    waiting = cur.fetchall() or []
    cur.execute("""SELECT * FROM tenant_social_posts WHERE tenant_id=%s AND approval IN ('approved','changes')
                     AND decided_at > NOW() - INTERVAL '14 days'
                   ORDER BY decided_at DESC LIMIT 20""", (tenant_id,))
    recent = cur.fetchall() or []
    cur.close(); conn.close()
    chans = _channels(customer)
    return render_template(
        "portal/social_approval.html",
        customer=customer, waiting=waiting, recent=recent, chans=chans,
        cards={p["id"]: _card(p, chans, W.today_for(customer)) for p in waiting + recent},
        display=W.DISPLAY,
        required=W.approval_required(tenant_id),
        can_approve=W.can_approve(),
        now=datetime.now(timezone.utc),
    )


@socialcal_bp.route("/social-posts/approval/settings", methods=["POST"])
@team_feature(W.APPROVE_KEY)
def approval_settings():
    r = _require_login() or _require_team_permission(W.APPROVE_KEY)
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    on = request.form.get("approval_required") == "1"
    W.set_approval_required(int(customer["tenant_id"]), on, _current_actor(customer)["label"])
    flash("Approval switched on. Posts from people who can't approve now wait here until someone approves them."
          if on else "Approval switched off. Everyone who can post sends straight to Buffer again. "
                     "Posts already waiting stay here until approved or sent back.", "success")
    return redirect(url_for("socialcal.approval"))


def _waiting_post(customer, post_id):
    p = _get_post(int(customer["tenant_id"]), post_id)
    if not p or p["status"] != "draft" or p.get("approval") != "waiting":
        flash("That post isn't waiting for approval any more.", "warning")
        return None
    return p


@socialcal_bp.route("/social-posts/<int:post_id>/approve", methods=["POST"])
@team_feature(W.APPROVE_KEY)
def approve(post_id: int):
    r = _require_login() or _require_team_permission(W.APPROVE_KEY)
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    back = request.form.get("back") == "view" and url_for("socialcal.post_view", post_id=post_id) or url_for("socialcal.approval")
    p = _waiting_post(customer, post_id)
    if not p:
        return redirect(back)
    tenant_id = int(customer["tenant_id"])
    owner = _owner(customer)
    actor = _current_actor(customer)["label"]

    mode = request.form.get("mode") or "planned"   # planned | now | other
    if mode == "now":
        action, when = "now", None
    elif mode == "other":
        action, when = "schedule", _parse_when(request.form.get("scheduled_for_utc"))
    else:
        action, when = ("schedule", p["scheduled_for"]) if p.get("scheduled_for") else ("now", None)

    usable = {c["channel_id"]: c for c in ba.list_channels(owner, enabled_only=True)}
    picked = [c for c in p["channel_ids"] if c in usable]
    has_image = bool(p.get("image_filename") or p.get("media"))
    err = _validate(p["caption"], picked, action, when, usable, has_image)
    if err:
        if mode == "planned" and when:
            err = "The planned time has passed. Choose Post now or pick another time."
        flash(err, "warning")
        return redirect(back)
    p = dict(p, channel_ids=picked)

    status, ids, results, errors = _send_to_buffer(owner, p, usable, when)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""UPDATE tenant_social_posts SET status=%s, buffer_post_ids=%s, channel_results=%s, buffer_error=%s,
                          channel_ids=%s, scheduled_for=%s, approval='approved', approval_note=NULL,
                          decided_by=%s, decided_at=NOW(), updated_at=NOW()
                   WHERE id=%s AND tenant_id=%s""",
                (status, _json.dumps(ids) if ids else None, _json.dumps(results), errors, picked, when,
                 actor, post_id, tenant_id))
    conn.commit()
    cur.close(); conn.close()
    W.log_event(tenant_id, post_id, "approved", actor)
    insert_audit_log(action="social_post_approved", tenant_id=tenant_id,
                     details={"post_id": post_id, "status": status, "by": actor})
    when_text = ("It's scheduled and Buffer will publish it at the planned time." if when
                 else "It's been sent to Buffer and is publishing now.")
    if status == "failed":
        W.notify_submitter(customer, p, "approved", actor, when_text="But Buffer didn't accept it: " + (errors or ""))
        flash(f"Approved, but Buffer didn't accept the post. It's under Content as Failed so it can be tried again. {errors}", "danger")
    else:
        W.notify_submitter(customer, p, "approved", actor, when_text=when_text)
        flash(("Approved. " + when_text) if status != "partial" else f"Approved and sent to some accounts, but not all. {errors}",
              "success" if status != "partial" else "warning")
    return redirect(back)


@socialcal_bp.route("/social-posts/<int:post_id>/request-changes", methods=["POST"])
@team_feature(W.APPROVE_KEY)
def request_changes(post_id: int):
    r = _require_login() or _require_team_permission(W.APPROVE_KEY)
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    back = request.form.get("back") == "view" and url_for("socialcal.post_view", post_id=post_id) or url_for("socialcal.approval")
    note = (request.form.get("note") or "").strip()[:2000]
    if not note:
        flash("Write what needs changing, so the writer knows what to fix.", "warning")
        return redirect(back)
    p = _waiting_post(customer, post_id)
    if not p:
        return redirect(back)
    tenant_id = int(customer["tenant_id"])
    actor = _current_actor(customer)["label"]
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""UPDATE tenant_social_posts SET approval='changes', approval_note=%s, decided_by=%s,
                          decided_at=NOW(), updated_at=NOW() WHERE id=%s AND tenant_id=%s""",
                (note, actor, post_id, tenant_id))
    conn.commit()
    cur.close(); conn.close()
    W.log_event(tenant_id, post_id, "changes", actor, note)
    insert_audit_log(action="social_post_changes_requested", tenant_id=tenant_id, details={"post_id": post_id, "by": actor})
    W.notify_submitter(customer, p, "changes", actor, note=note)
    flash(f"Sent back to {p.get('submitted_by') or 'the writer'} with your comments.", "success")
    return redirect(back)


@socialcal_bp.route("/social-posts/<int:post_id>/assign", methods=["POST"])
@team_feature("social.posts_edit")
def assign(post_id: int):
    """Change only the person responsible and due date. Doesn't touch the
    copy in Buffer, so a scheduled post stays scheduled."""
    r = _require_login() or _require_team_permission("social.posts_edit")
    if r:
        return r
    r, customer = _ctx()
    if r:
        return r
    p = _get_post(int(customer["tenant_id"]), post_id)
    if not p:
        abort(404)
    back = url_for("socialcal.post_view", post_id=post_id)
    if p["status"] not in ("draft", "scheduled", "failed"):
        flash("This post has already gone out, so it can't be reassigned.", "warning")
        return redirect(back)
    actor = _current_actor(customer)
    current = {"key": p.get("owner_key"), "label": W.owner_name(p), "email": None}
    person, due, err = W.assignment_from_form(customer, request.form, actor, keep=current)
    if err:
        flash(err, "warning")
        return redirect(back)
    if W.set_assignment(customer, p, person, due, actor["label"]):
        insert_audit_log(action="social_post_assigned", tenant_id=int(customer["tenant_id"]),
                         details={"post_id": post_id, "owner": person["label"],
                                  "due": due.isoformat() if due else None, "by": actor["label"]})
        flash(f"Saved. {person['label']} is responsible" + (f", due {due.strftime('%a %d %b')}." if due else ", no due date."), "success")
    else:
        flash("Nothing changed.", "success")
    return redirect(back)


@socialcal_bp.app_context_processor
def _inject_social_workflow():
    """Approval badge in the side menu, and the pre-filled day + "Submit for
    approval" wording on the post forms."""
    out = {}
    try:
        if request.blueprint in (None, "portal_admin", "ai_admin") or not session.get("portal_logged_in"):
            return out
        from portal_routes import _customer_id, _get_customer
        cid = _customer_id()
        customer = _get_customer(cid) if cid else None
        if not customer or not customer.get("tenant_id"):
            return out
        tid = int(customer["tenant_id"])
        if not ba.is_connected(ba.tenant_owner(tid)):
            return out
        out["social_waiting_count"] = W.waiting_count(tid) if W.can_approve() else 0
        if request.blueprint in ("buffer", "social", "upload", "socialcal"):
            out["social_needs_approval"] = W.approval_needed(tid)
            if request.endpoint in ("social.finish", "upload.post"):
                from portal_routes import _current_actor as _ca
                out["social_people"] = W.people(customer)
                out["social_me"] = _ca(customer)["key"]
            if request.blueprint in ("social", "upload"):
                t = PL.fill_target(tid)
                if t:
                    out["social_fill"] = {"id": t["id"], "title": t.get("plan_topic") or W.title_of(t),
                                          "idea": PL.fill_idea(t), "channel_ids": list(t.get("channel_ids") or []),
                                          "owner_key": t.get("owner_key"),
                                          "due": t["due_date"].isoformat() if t.get("due_date") else None}
            # The day picked with "+" on the calendar; forgotten after 2 hours
            # so an abandoned plan doesn't pre-fill some later, unrelated post.
            day, _, at = (session.get("social_plan_date") or "").partition("|")
            if day and at.isdigit() and datetime.now(timezone.utc).timestamp() - int(at) < 7200:
                out["social_plan_date"] = day
    except Exception as e:
        print("⚠️ social workflow context:", e)
    return out
