"""
report_dashboard_routes.py — Reports › Dashboards + drill-down (R3 19 Nov 2026; sharing R4).
Routes sit on reports_bp (report_builder_routes imports this module at its end).

  GET  /reports/dashboards                         first dashboard you can see (or "make your first")
  GET  /reports/dashboards/<id>                    a dashboard; filters in the query string
  GET  /reports/dashboards/<id>/box/<box_id>       one box's contents (loaded by the page, in parallel)
  GET  /reports/dashboards/<id>/edit               edit mode (owner, or "others can edit")
  POST /reports/dashboards/new                     new dashboard
  POST /reports/dashboards/<id>/layout             save edit mode (order, sizes, filters, name)
  POST /reports/dashboards/<id>/add                add a saved report, a ready-made question or a text note
  POST /reports/dashboards/<id>/add-new            save a report built in the wizard and add it (20 Nov fix round)
  POST /reports/dashboards/<id>/fill               fill an empty dashboard with the ready-made Sales overview
  POST /reports/dashboards/<id>/share              who can open it / what numbers they see / others can edit
  POST /reports/dashboards/<id>/delete             delete a dashboard (owner only; reports stay)
  POST /reports/builder/<rid>/add-to-dashboard     from the report builder
  GET  /reports/drill                              the records behind a number (drawer)

A dashboard shared with someone shows them its boxes even when the report
behind a box is not shared with them; they just don't get the "Open" link.
"""
import json
from urllib.parse import urlencode

import psycopg2.extras
from flask import flash, jsonify, redirect, render_template, request, url_for

import report_access as RA
import report_plan as PLAN
import report_widgets as RW
import report_builder as RB
import report_dashboards as RD
import report_plain as RP
import report_subjects as RS
from db import get_db_connection
from feature_access import team_feature
from portal_routes import _current_actor
from report_builder_routes import (
    NotAllowed, _args_from_saved, _box_cfg, _chart_and_totals, _dash_filter_qs, _dash_filters, _query, _resolve, _results,
    _custom_only, _saved, _setup, _share_choices, _unscoped, reports_bp,
)


def _dash(ctx, dash_id, need="view"):
    d = RD.get_dashboard(ctx["tenant_id"], dash_id)
    v = ctx["viewer"]
    ok = {"view": RA.can_view, "edit": RA.can_edit, "manage": RA.can_manage}[need]
    return d if d and ok(d, v) else None


def _visible_dashboards(ctx):
    v = ctx["viewer"]
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM report_dashboards WHERE tenant_id=%s ORDER BY position, id", (ctx["tenant_id"],))
        return [d for d in cur.fetchall() if RA.can_view(d, v)]
    finally:
        conn.close()


@reports_bp.route("/reports/dashboards")
@team_feature("reports.custom")
def dashboards():
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    ds = _visible_dashboards(ctx)
    if not ds and not RD.list_dashboards(ctx["tenant_id"]):
        # First visit for this business: start with a filled-in Sales overview (20 Nov 2026 fix round)
        did = RD.create_dashboard(ctx["tenant_id"], "Sales overview", _current_actor(customer))
        _fill_overview(ctx, customer, b2b, did)
        return redirect(url_for("reports.dashboard", dash_id=did))
    if ds:
        return redirect(url_for("reports.dashboard", dash_id=ds[0]["id"]))
    return render_template("portal/report_dashboard.html", customer=customer, dash=None, dashboards=[],
                           flt=_dash_filters({}), people=ctx["people"], RB=RB, RA=RA, filter_qs="")


@reports_bp.route("/reports/dashboards/<int:dash_id>")
@team_feature("reports.custom")
def dashboard(dash_id: int):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    d = _dash(ctx, dash_id)
    if not d:
        flash("That dashboard was not found, or it has not been shared with you.", "warning")
        return redirect(url_for("reports.dashboards"))
    flt = _dash_filters(request.args)
    filter_qs = urlencode(_dash_filter_qs(flt))
    boxes = [b for b in RW.place_all(d["boxes"])
             if b["kind_v"] in ("heading", "number", "list", "board") or RB.subject_ok(b["subject"], b2b)]
    for b in boxes:
        if b["kind_v"] == "heading":
            b["head"], b["more"] = RW.heading_parts(b.get("note"))
    people, role_list = _share_choices(ctx)
    v = ctx["viewer"]
    custom = PLAN.custom_ok(ctx["tenant_id"])     # Starter: the Sales overview is fixed (21 Nov 2026)
    return render_template("portal/report_dashboard.html", customer=customer, dash=d, boxes=boxes,
                           dashboards=_visible_dashboards(ctx), flt=flt, filter_qs=filter_qs, dp_presets=RB.picker_presets(),
                           people=ctx["people"], RB=RB, RA=RA, sizes=dict(RD.SIZES),
                           can_edit=custom and RA.can_edit(d, v), can_manage=custom and RA.can_manage(d, v), viewer=v,
                           can_fill=RA.can_edit(d, v), custom_ok=custom,
                           share_people=people, share_roles=role_list)


@reports_bp.route("/reports/dashboards/<int:dash_id>/box/<int:box_id>")
@team_feature("reports.custom")
def dashboard_box(dash_id: int, box_id: int):
    """One widget's contents (loaded by the page, in parallel): a report, a number, an action list or a leaderboard."""
    customer, ctx, b2b, quote_scope, err = _setup()
    if err:
        return "", 403
    args = request.args.copy()
    args["did"], args["bid"] = str(dash_id), str(box_id)
    try:
        cfg, rctx, rscope, source = _resolve(args, ctx, b2b, quote_scope)
    except NotAllowed:
        return "", 404
    box = source["box"]
    if request.args.get("pv") == "1":                # Settings preview: the choices on screen, not saved yet
        d = _dash(ctx, dash_id, "edit")
        if not d or _custom_only(ctx, json_ok=True):
            return "", 403
        box = _preview_box(ctx, customer, b2b, box, request.args)
        if box is None:
            return render_template("portal/_report_widget.html", kind="error", box={})
        active = set(d["filters"] or [])
        flt0 = _dash_filters({})      # the form's own "period" / "member" fields are the widget's, not dashboard filters
        if RW.kind_of(box) == "report":
            cfg = _box_cfg(box, b2b, flt0, active)
        else:
            cfg = RW.cfg_for(box, b2b, flt0, active)
            if cfg is None:
                return render_template("portal/_report_widget.html", kind="error", box=box)
        rctx, rscope = ctx, quote_scope
    kind = RW.kind_of(box)
    flt = _dash_filter_qs(_dash_filters(request.args))
    drill_base = url_for("reports.drill") + "?" + urlencode(dict(flt, did=dash_id, bid=box_id))
    subj = RB.SUBJECTS[cfg["subject"]]
    try:
        h = max(140, min(1200, int(request.args.get("h") or 240)))
    except ValueError:
        h = 240
    if kind == "number":
        sp = RW.spec(box)
        conn = get_db_connection()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            n, val = RB.run_total(cur, rctx, cfg, rscope)
            m = RB.measure_of(cfg)
            value = RB.fmt(val or 0, m[3], rctx) if m[2] else f"{(n or 0):,}"
            warn = RW.WARN.get(sp[0]) if (n or 0) > 0 else None
        except Exception as e:
            conn.rollback()
            print("⚠️ widget number:", e)
            return render_template("portal/_report_widget.html", kind="error", box=box)
        finally:
            conn.close()
        return render_template("portal/_report_widget.html", kind="number", box=box, title=RW.title_of(box), value=value, warn=warn,
                               sub="Right now" if sp[4] else cfg["period_label"], drill_base=drill_base)
    if kind == "board":
        sp = RW.spec(box)
        conn = get_db_connection()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            summ = RB.run_summary(cur, rctx, cfg, rscope)
        except Exception as e:
            conn.rollback()
            print("⚠️ widget leaderboard:", e)
            return render_template("portal/_report_widget.html", kind="error", box=box)
        finally:
            conn.close()
        use_m = bool(RB.measure_of(cfg)[2])
        rows = []
        for r in summ["rows"]:
            num = r.get("mv") if use_m else r.get("n")
            rows.append({"label": r["labels"][0], "v": r["cells"][1] if use_m and len(r["cells"]) > 1 else r["cells"][0],
                         "num": float(num or 0), "rk": r["rk"]})
        rows.sort(key=lambda x: -x["num"])
        rows = rows[:RW.rows_for(box)]
        top = max([x["num"] for x in rows] or [0]) or 1
        for i, x in enumerate(rows, 1):
            x["rank"], x["pct"] = i, round(x["num"] * 100 / top)
        return render_template("portal/_report_widget.html", kind="board", box=box, title=RW.title_of(box), rows=rows,
                               sub=cfg["period_label"], drill_base=drill_base)
    if kind == "list":
        sp = RW.spec(box)
        conn = get_db_connection()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            rows, total = RB.run_detailed(cur, rctx, dict(cfg, page=1), quote_scope=rscope)
            rows = rows[:RW.rows_for(box)]
            links = {}
            if sp[4] == "followup" and rows:
                cur.execute("SELECT id, lead_id FROM lead_followups WHERE id = ANY(%s)", ([r["id"] for r in rows],))
                links = {x["id"]: url_for("portal.opportunity_detail", lead_id=x["lead_id"]) for x in cur.fetchall()}
        except Exception as e:
            conn.rollback()
            print("⚠️ widget action list:", e)
            return render_template("portal/_report_widget.html", kind="error", box=box)
        finally:
            conn.close()
        for r in rows:
            if sp[4] == "deal":
                r["url"] = url_for("portal.opportunity_detail", lead_id=r["id"])
            elif sp[4] == "quote":
                r["url"] = url_for("portal.quotes_request_detail", request_id=r["id"])
            elif sp[4] == "inbox":
                r["url"] = url_for("portal.my_inbox", show="waiting")
            else:
                r["url"] = links.get(r["id"]) or url_for("portal.follow_ups_page")
        return render_template("portal/_report_widget.html", kind="list", box=box, title=RW.title_of(box), rows=rows, total=total,
                               cols=[subj["columns"][k]["label"] for k in cfg["col"]], drill_base=drill_base)
    # a report (chart or table)
    data = _results(rctx, cfg, rscope)
    headline, chart = ("", {"kind": "table"}) if data.get("error") else _chart_and_totals(rctx, cfg, rscope, data)
    box = dict(box, report_name=RW.title_of(box, box.get("report_name") or ""))
    report = _saved(ctx["tenant_id"], box["report_id"])
    if not RA.can_view(report, ctx["viewer"]):
        open_url = ""
    elif not PLAN.custom_ok(ctx["tenant_id"]):
        open_url = url_for("reports.view", id=box["report_id"], **{k: v for k, v in flt.items() if k in ("period", "date_from", "date_to", "member")})
    else:
        open_url = url_for("reports.builder") + "?" + _query(cfg, id=box["report_id"])
    return render_template("portal/_report_box.html", box=box, cfg=cfg, d=data, chart=chart, headline=headline,
                           subj=subj, RB=RB, cid=("pv" if request.args.get("pv") == "1" else "box") + str(box_id), chart_h=h,
                           compare_label=(RB.compare_range(cfg) or (None, None, ""))[2],
                           drill_base=drill_base, open_url=open_url)

def _settings_wcfg(f, kind, old=None):
    """The widget's own settings from the Settings form (or the preview's query string)."""
    w = dict(old or {})
    w["title"] = " ".join((f.get("title") or "").split())[:80]
    if f.get("time") in ("dash", "own"):
        w["time"] = f.get("time")
        if w["time"] == "own":
            p = f.get("period") or ""
            w["period"] = p if RB.period_ok(p) else "this_month"
            w["date_from"], w["date_to"] = (f.get("date_from") or "")[:10], (f.get("date_to") or "")[:10]
    if f.get("member") in ("dash", "own"):
        w["member"] = f.get("member")
        w["member_key"] = (f.get("member_key") or "")[:64] if w["member"] == "own" else ""
    if kind in RW.ROW_CHOICES:
        try:
            n = int(f.get("rows") or 0)
        except ValueError:
            n = 0
        if n in RW.ROW_CHOICES[kind]:
            w["rows"] = n
    return w


def _preview_box(ctx, customer, b2b, box, f):
    """A copy of the widget with the Settings choices on screen applied (nothing saved)."""
    kind = RW.kind_of(box)
    b = dict(box)
    if kind in ("number", "list", "board") and f.get("pick"):
        if f.get("pick") not in {a for a, _l, _h in RW.choices(kind, b2b)}:
            return None
        b["wcfg"] = dict(b.get("wcfg") or {}, pick=f.get("pick"))
    if kind == "report":
        rid = f.get("report_id", type=int)
        ready = f.get("ready") or ""
        if ready and ready in {x["key"] for x in _offer(b2b)}:
            r = RS.READY_BY_KEY[ready]
            b.update(config=r["cfg"], subject=r["cfg"]["subject"], report_name=RP.QUESTIONS.get(ready, r["label"]))
        elif rid:
            row = _saved(ctx["tenant_id"], rid)
            if not row or not RA.can_view(row, ctx["viewer"]):
                return None
            b.update(config=row["config"], subject=row["subject"], report_name=row["name"], report_id=rid)
    b["wcfg"] = _settings_wcfg(f, kind, b.get("wcfg"))
    b["use_filters"] = True
    return b


@reports_bp.route("/reports/dashboards/<int:dash_id>/edit")
@team_feature("reports.custom")
def dashboard_edit(dash_id: int):
    """Edit a dashboard: widgets on a grid — drag, resize, + in an empty space (approved mock-up screen 7)."""
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    d = _dash(ctx, dash_id, "edit")
    if not d:
        flash("You can't change that dashboard.", "warning")
        return redirect(url_for("reports.dashboards"))
    v = ctx["viewer"]
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM report_defs WHERE tenant_id=%s ORDER BY COALESCE(last_opened_at, updated_at) DESC",
                    (ctx["tenant_id"],))
        reports = [r for r in cur.fetchall() if r["subject"] in RB.SUBJECTS and r.get("folder") != OVERVIEW_FOLDER and
                   RB.subject_ok(r["subject"], b2b) and RA.can_view(r, v)]
    finally:
        conn.close()
    mine = []
    for r in reports:
        try:
            what = RP.plain_sentence(RB.read_config(_args_from_saved(r), b2b))
        except Exception:
            what = RB.SUBJECTS[r["subject"]]["label"]
        mine.append({"id": r["id"], "name": r["name"], "what": what,
                     "who": "Saved by you" if RA.is_mine(r, v) else "Saved by " + (r.get("created_by") or "a team member")})
    offered = {x["key"]: x for x in _offer(b2b)}
    topics = []
    for key, label, keys in RP.TOPICS:
        qs = [offered[k] for k in keys if k in offered]
        if qs:
            topics.append({"key": key, "label": label, "qs": qs})
    widgets = []
    for b in RW.place_all(d["boxes"]):
        k = b["kind_v"]
        if k == "report" and not RB.subject_ok(b.get("subject") or "", b2b):
            continue
        sp = RW.spec(b)
        title = (b.get("report_name") if k == "report" else RW.heading_parts(b.get("note"))[0] if k == "heading"
                 else (sp[1] if sp else "Not available"))
        o = RW.opts(b)
        head, more = RW.heading_parts(b.get("note"))
        widgets.append({"id": b["id"], "kind": k, "kind_name": RW.TYPE_NAMES[k],
                        "title": RW.title_of(b, title or "Heading") if k != "heading" else (head or "Heading"),
                        "default_title": (sp[1] if sp else (b.get("report_name") or "")),
                        "own_title": (b.get("wcfg") or {}).get("title") or "",
                        "head": head, "more": more, "pick": (b.get("wcfg") or {}).get("pick") or "",
                        "report_id": b.get("report_id"), "use_filters": b.get("use_filters", True),
                        "has_time": RW.has_time(b), "right_now": bool(sp and k == "number" and sp[4]),
                        "has_member": RW.has_member(b),
                        "time": o["time"], "period": o.get("period") or "this_month",
                        "date_from": o.get("date_from") or "", "date_to": o.get("date_to") or "",
                        "member": o["member"], "member_key": o.get("member_key") or "",
                        "rows": RW.rows_for(b), "row_choices": RW.ROW_CHOICES.get(k, []),
                        "x": b["gx"], "y": b["gy"], "w": b["gw"], "h": b["gh"]})
    picks = {k: [{"key": a, "label": l, "help": h} for a, l, h in RW.choices(k, b2b)] for k in ("number", "list", "board")}
    return render_template("portal/report_dashboard_edit.html", customer=customer, dash=d, widgets=widgets,
                           types=RW.TYPES, picks=picks, topics=topics, mine=mine, sizes=RW.SIZE, mins=RW.MIN,
                           people=ctx["people"], dp_presets=RB.picker_presets(),
                           pick_help={k: {a: h for a, _l, h in RW.choices(k, b2b)} for k in ("number", "list", "board")},
                           right_now=[x[0] for x in RW.NUMBERS if x[4]],
                           dash_filters=RD.DASH_FILTERS, can_manage=RA.can_manage(d, v), RB=RB)


def _widget_from_form(ctx, customer, b2b, f):
    """(kind, pick, report_id, note) from the Add / Settings form, or None when it isn't allowed."""
    kind = f.get("kind") or ""
    if kind in ("number", "list", "board"):
        pick = f.get("pick") or ""
        if pick not in {a for a, _l, _h in RW.choices(kind, b2b)}:
            return None
        return kind, pick, None, None
    if kind == "heading":
        note = (f.get("note") or "").strip()[:2000] or "New heading"
        return kind, None, None, note
    if kind == "report":
        ready = f.get("ready") or ""
        if ready:
            r = RS.READY_BY_KEY.get(ready)
            if not r or ready not in {x["key"] for x in _offer(b2b)}:
                return None
            rid = _save_report(ctx, customer, b2b, r["label"], _md(r["cfg"]), OVERVIEW_FOLDER)
            return (kind, None, rid, None) if rid else None
        rid = f.get("report_id", type=int)
        if rid and RA.can_view(_saved(ctx["tenant_id"], rid), ctx["viewer"]):
            return kind, None, rid, None
    return None


@reports_bp.route("/reports/dashboards/new", methods=["POST"])
@team_feature("reports.custom")
def dashboard_new():
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    name = " ".join((request.form.get("name") or "").split())[:80]
    if not name:
        flash("Please give the dashboard a name.", "warning")
        return redirect(url_for("reports.dashboards"))
    did = RD.create_dashboard(ctx["tenant_id"], name, _current_actor(customer))
    return redirect(url_for("reports.dashboard_edit", dash_id=did))


@reports_bp.route("/reports/dashboards/<int:dash_id>/layout", methods=["POST"])
@team_feature("reports.custom")
def dashboard_layout(dash_id: int):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return jsonify({"error": "Please log in again."}), 401
    if _custom_only(ctx, json_ok=True):
        return jsonify({"error": "Your own dashboards come with Growth and Scale."}), 403
    if not _dash(ctx, dash_id, "edit"):
        return jsonify({"error": "That dashboard was not found."}), 404
    body = request.get_json(silent=True) or {}
    name = " ".join(str(body.get("name") or "").split())[:80] or None
    ok = RD.save_layout(ctx["tenant_id"], dash_id, body.get("boxes") or [], body.get("filters") or [], name)
    if not ok:
        return jsonify({"error": "That dashboard was not found."}), 404
    return jsonify({"ok": True, "url": url_for("reports.dashboard", dash_id=dash_id)})


@reports_bp.route("/reports/dashboards/<int:dash_id>/add", methods=["POST"])
@team_feature("reports.custom")
def dashboard_add(dash_id: int):
    """Add a widget where the person clicked "+ Add a widget here"."""
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    if not _dash(ctx, dash_id, "edit"):
        flash("You can't change that dashboard.", "warning")
        return redirect(url_for("reports.dashboards"))
    w = _widget_from_form(ctx, customer, b2b, request.form)
    bid = None
    if w:
        kind, pick, rid, note = w
        dw, dh = RW.SIZE[kind]
        sw = request.form.get("w", type=int) or dw
        place = RW.clean_place(request.form.get("x") or 0, request.form.get("y") or 0,
                               dw if kind != "heading" else max(dw, sw), dh, kind)
        if place and place[0] + place[2] > RW.COLS:
            place = (RW.COLS - place[2],) + place[1:]
        bid = RD.add_widget(ctx["tenant_id"], dash_id, kind, place, pick=pick, report_id=rid, note=note) if place else None
    if not bid:
        flash(f"Could not add that widget (a dashboard holds up to {RD.MAX_BOXES}).", "warning")
    return redirect(url_for("reports.dashboard_edit", dash_id=dash_id) + (f"#box-{bid}" if bid else ""))


@reports_bp.route("/reports/dashboards/<int:dash_id>/box/<int:box_id>/settings", methods=["POST"])
@team_feature("reports.custom")
def dashboard_widget_settings(dash_id: int, box_id: int):
    """A widget's Settings: what it shows, and whether it follows the dashboard's filters."""
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    d = _dash(ctx, dash_id, "edit")
    box = next((b for b in (d or {}).get("boxes", []) if b["id"] == box_id), None)
    if not box:
        flash("You can't change that dashboard.", "warning")
        return redirect(url_for("reports.dashboards"))
    f = request.form.copy()
    kind = RW.kind_of(box)
    f["kind"] = kind
    rid, wcfg = None, dict(box.get("wcfg") or {})
    if kind == "heading":
        head = " ".join((f.get("heading") or "").split())[:200] or "Heading"
        more = (f.get("explanation") or "").strip()[:1800]
        RD.set_widget(ctx["tenant_id"], dash_id, box_id, wcfg, note=head + ("\n" + more if more else ""))
        flash("Widget updated.", "success")
        return redirect(url_for("reports.dashboard_edit", dash_id=dash_id) + f"#box-{box_id}")
    if kind in ("number", "list", "board"):
        if f.get("pick") not in {a for a, _l, _h in RW.choices(kind, b2b)}:
            flash("That choice is not available for your business.", "warning")
            return redirect(url_for("reports.dashboard_edit", dash_id=dash_id) + f"#box-{box_id}")
        wcfg["pick"] = f.get("pick")
    elif kind == "report" and (f.get("ready") or f.get("report_id")):
        w = _widget_from_form(ctx, customer, b2b, f)
        if not w:
            flash("That report is not available.", "warning")
            return redirect(url_for("reports.dashboard_edit", dash_id=dash_id) + f"#box-{box_id}")
        rid = w[2]
    wcfg = _settings_wcfg(f, kind, wcfg)
    RD.set_widget(ctx["tenant_id"], dash_id, box_id, wcfg, report_id=rid)
    flash("Widget updated.", "success")
    return redirect(url_for("reports.dashboard_edit", dash_id=dash_id) + f"#box-{box_id}")


@reports_bp.route("/reports/dashboards/<int:dash_id>/share", methods=["POST"])
@team_feature("reports.custom")
def dashboard_share(dash_id: int):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    v = ctx["viewer"]
    if not _dash(ctx, dash_id, "manage"):
        flash("Only the person who made this dashboard can change who sees it.", "warning")
        return redirect(url_for("reports.dashboards"))
    people, role_list = _share_choices(ctx)
    s = RA.clean_sharing(request.form, v, {k for k, _ in people}, {k for k, _ in role_list})
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE report_dashboards SET visibility=%s, share_with=%s, data_scope=%s, others_edit=%s, "
                    "created_by_key=COALESCE(created_by_key, %s), updated_at=NOW() WHERE id=%s AND tenant_id=%s",
                    (s["visibility"], json.dumps(s["share_with"]), s["data_scope"], s["others_edit"], v["key"],
                     dash_id, ctx["tenant_id"]))
        conn.commit()
    finally:
        conn.close()
    flash("Sharing saved.", "success")
    return redirect(url_for("reports.dashboard", dash_id=dash_id))


@reports_bp.route("/reports/dashboards/<int:dash_id>/delete", methods=["POST"])
@team_feature("reports.custom")
def dashboard_delete(dash_id: int):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    if not _dash(ctx, dash_id, "manage"):
        flash("Only the person who made this dashboard can delete it.", "warning")
        return redirect(url_for("reports.dashboards"))
    ok = RD.delete_dashboard(ctx["tenant_id"], dash_id)
    flash("Dashboard deleted. The reports in it are still in All reports." if ok else "That dashboard was not found.",
          "success" if ok else "warning")
    return redirect(url_for("reports.dashboards"))


@reports_bp.route("/reports/builder/<int:report_id>/add-to-dashboard", methods=["POST"])
@team_feature("reports.custom")
def add_to_dashboard(report_id: int):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    if not RA.can_view(_saved(ctx["tenant_id"], report_id), ctx["viewer"]):
        flash("That report was not found.", "warning")
        return redirect(url_for("reports.library"))
    did = request.form.get("dash_id", type=int)
    if did and not _dash(ctx, did, "edit"):
        flash("You can't change that dashboard.", "warning")
        return redirect(url_for("reports.builder", id=report_id))
    if not did:
        name = " ".join((request.form.get("new_name") or "").split())[:80]
        if not name:
            flash("Choose a dashboard, or name a new one.", "warning")
            return redirect(url_for("reports.builder", id=report_id))
        did = RD.create_dashboard(ctx["tenant_id"], name, _current_actor(customer))
    bid = RD.add_box(ctx["tenant_id"], did, report_id=report_id)
    if not bid:
        flash("Could not add the report to that dashboard.", "warning")
        return redirect(url_for("reports.builder", id=report_id))
    flash("Added to the dashboard. Drag it where you want it.", "success")
    return redirect(url_for("reports.dashboard_edit", dash_id=did) + f"#box-{bid}")


@reports_bp.route("/reports/drill")
@team_feature("reports.custom")
def drill():
    customer, ctx, b2b, quote_scope, err = _setup()
    if err:
        return "", 403
    try:
        dcfg, rctx, rscope, source = _resolve(request.args, ctx, b2b, quote_scope, drill=True)
    except NotAllowed:
        return "", 404
    base_cfg, _, _, _ = _resolve(request.args, ctx, b2b, quote_scope, drill=False)
    data = _results(rctx, dcfg, rscope)
    subj = RB.SUBJECTS[dcfg["subject"]]
    headline = ""
    if not data.get("error"):
        conn = get_db_connection()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            n, mv = RB.run_total(cur, rctx, dict(dcfg, m=base_cfg["m"]), rscope)
            m = RB.measure_of(base_cfg)
            headline = f"{n:,} {subj['noun']}" + (f" · {m[1]} {RB.fmt(mv, m[3], ctx)}" if m[2] and mv is not None else "")
        except Exception as e:
            conn.rollback()
            print("⚠️ report drill total:", e)
        finally:
            conn.close()
    # Saved / box drills keep their own links so whole-business sharing carries to the export.
    keep = {k: request.args.get(k) for k in ("rid", "did", "bid", "period", "date_from", "date_to", "member", "cmp",
                                             "k1", "k2") if k in request.args}
    if source:
        export_url = url_for("reports.export", fmt="xlsx") + "?" + urlencode(dict(keep, drill="1"))
    else:
        export_url = url_for("reports.export", fmt="xlsx") + "?" + _query(dcfg)
    return render_template("portal/_report_drill.html", d=data, cfg=dcfg, subj=subj, headline=headline, RB=RB,
                           chips=[RB.filter_label(dcfg, f, ctx) for f in dcfg["f"]],
                           open_url=url_for("reports.builder") + "?" + _query(dcfg),
                           export_url=export_url, whole=rctx is not ctx)


# ── 20 Nov 2026 fix round: ready-made boxes, build-and-add, filled-in Sales overview ─────────────

OVERVIEW_FOLDER = "Sales overview"


def _plan_kinds(b2b):
    return "quotes" in b2b, bool({"orders", "discounts", "carts"} & b2b)


def _offer(b2b) -> list:
    """The ready-made questions this business can add as a box."""
    is_b2b, is_b2c = _plan_kinds(b2b)
    out = []
    for r in RS.ready_for(b2b, is_b2b, is_b2c):
        ch = r["cfg"].get("chart", "auto")
        out.append({"key": r["key"], "q": RP.QUESTIONS.get(r["key"], r["label"]), "label": r["label"],
                    "how": RP.CHART_WORD.get(ch, "Chart") + " · " + r["desc"], "icon": RP.CHART_ICON.get(ch, "▮▮")})
    return out


def _save_report(ctx, customer, b2b, name, args, folder=None):
    """Saves a report from report-page settings; returns its id (None if it can't be read)."""
    try:
        cfg = RB.read_config(args, b2b)
    except Exception as e:
        print("⚠️ dashboard save report:", e)
        return None
    actor = _current_actor(customer)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO report_defs (tenant_id, name, subject, config, folder, created_by_key, created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (ctx["tenant_id"], (name or RB.SUBJECTS[cfg["subject"]]["label"])[:120], cfg["subject"],
                     json.dumps(RB.config_of(cfg)), folder, actor["key"], actor["label"]))
        rid = cur.fetchone()[0]
        conn.commit()
        return rid
    except Exception as e:
        conn.rollback()
        print("⚠️ dashboard save report:", e)
        return None
    finally:
        conn.close()


def _md(cfg: dict):
    return _args_from_saved({"config": cfg})


# Boxes of the ready-made Sales overview: (name, size, config). Each is skipped if the business can't use its subject.
_KPI_B2B = [
    ("Enquiries", "s", {"subject": "leads", "period": "this_quarter", "chart": "number"}),
    ("Quotes sent", "s", {"subject": "quotes", "datef": "firstsent", "period": "this_quarter", "chart": "number"}),
    ("Quotes won (value)", "s", {"subject": "quotes", "f": "qstatus|is|won", "m": "sum_qvalue", "datef": "qclosed",
                                 "period": "this_quarter", "chart": "number"}),
]
_KPI_B2C = [
    ("Orders", "s", {"subject": "orders", "period": "this_quarter", "chart": "number"}),
    ("Sales (order value)", "s", {"subject": "orders", "m": "sum_tot", "period": "this_quarter", "chart": "number"}),
    ("Enquiries", "s", {"subject": "leads", "period": "this_quarter", "chart": "number"}),
]
_READY_B2B = [("quotes_by_member", "m"), ("where_enquiries", "m"), ("quotes_waiting", "w"), ("deal_funnel", "m"), ("missed_replies", "m")]
_READY_B2C = [("orders_week", "w"), ("orders_status", "m"), ("top_products", "m"), ("where_enquiries", "m"), ("cart_recovery", "m")]


def _fill_overview(ctx, customer, b2b, dash_id) -> int:
    is_b2b, is_b2c = _plan_kinds(b2b)
    kpis = _KPI_B2B if is_b2b else (_KPI_B2C if is_b2c else _KPI_B2B[:1])
    ready = _READY_B2B if is_b2b else (_READY_B2C if is_b2c else [("where_enquiries", "m"), ("missed_replies", "m")])
    offered = {r["key"] for r in RS.ready_for(b2b, is_b2b, is_b2c)}
    n = 0
    for name, size, cfg in kpis:
        if not RB.subject_ok(cfg["subject"], b2b):
            continue
        rid = _save_report(ctx, customer, b2b, name, _md(cfg), OVERVIEW_FOLDER)
        if rid and RD.add_box(ctx["tenant_id"], dash_id, report_id=rid, size=size):
            n += 1
    for key, size in ready:
        r = RS.READY_BY_KEY.get(key)
        if not r or key not in offered:
            continue
        rid = _save_report(ctx, customer, b2b, r["label"], _md(r["cfg"]), OVERVIEW_FOLDER)
        if rid and RD.add_box(ctx["tenant_id"], dash_id, report_id=rid, size=size):
            n += 1
    return n


@reports_bp.route("/reports/dashboards/<int:dash_id>/fill", methods=["POST"])
@team_feature("reports.custom")
def dashboard_fill(dash_id: int):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    d = _dash(ctx, dash_id, "edit")
    if not d:
        flash("You can't change that dashboard.", "warning")
        return redirect(url_for("reports.dashboards"))
    if d["boxes"]:
        return redirect(url_for("reports.dashboard", dash_id=dash_id))
    n = _fill_overview(ctx, customer, b2b, dash_id)
    flash(f"Added {n} boxes. Change anything with Edit dashboard." if n else "Nothing to add yet.", "success" if n else "warning")
    return redirect(url_for("reports.dashboard", dash_id=dash_id))


@reports_bp.route("/reports/dashboards/<int:dash_id>/add-new", methods=["POST"])
@team_feature("reports.custom")
def dashboard_add_new(dash_id: int):
    """The wizard's "Create and add to dashboard": save the report, add it as a box, back to editing."""
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    if not _dash(ctx, dash_id, "edit"):
        flash("You can't change that dashboard.", "warning")
        return redirect(url_for("reports.dashboards"))
    args = request.form.copy()
    name = " ".join((args.pop("name", "") or "").split())[:120]
    rid = _save_report(ctx, customer, b2b, name, args)
    bid = RD.add_box(ctx["tenant_id"], dash_id, report_id=rid) if rid else None
    if not bid:
        flash("Could not add that report to the dashboard. Please try again.", "warning")
        return redirect(url_for("reports.dashboard_edit", dash_id=dash_id))
    flash(f"“{name or 'New report'}” saved and added to the dashboard.", "success")
    return redirect(url_for("reports.dashboard_edit", dash_id=dash_id) + f"#box-{bid}")
