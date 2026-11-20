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
import report_builder as RB
import report_dashboards as RD
import report_plain as RP
import report_subjects as RS
from db import get_db_connection
from feature_access import team_feature
from portal_routes import _current_actor
from report_builder_routes import (
    NotAllowed, _args_from_saved, _box_cfg, _chart_and_totals, _dash_filter_qs, _dash_filters, _query, _resolve, _results,
    _saved, _setup, _share_choices, _unscoped, reports_bp,
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
    boxes = [b for b in d["boxes"] if b["report_id"] is None or
             RB.subject_ok(b["subject"], b2b)]
    people, role_list = _share_choices(ctx)
    v = ctx["viewer"]
    return render_template("portal/report_dashboard.html", customer=customer, dash=d, boxes=boxes,
                           dashboards=_visible_dashboards(ctx), flt=flt, filter_qs=filter_qs, dp_presets=RB.picker_presets(),
                           people=ctx["people"], RB=RB, RA=RA, sizes=dict(RD.SIZES),
                           can_edit=RA.can_edit(d, v), can_manage=RA.can_manage(d, v), viewer=v,
                           share_people=people, share_roles=role_list)


@reports_bp.route("/reports/dashboards/<int:dash_id>/box/<int:box_id>")
@team_feature("reports.custom")
def dashboard_box(dash_id: int, box_id: int):
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
    data = _results(rctx, cfg, rscope)
    headline, chart = ("", {"kind": "table"}) if data.get("error") else _chart_and_totals(rctx, cfg, rscope, data)
    flt = _dash_filter_qs(_dash_filters(request.args))
    report = _saved(ctx["tenant_id"], box["report_id"])
    open_url = (url_for("reports.builder") + "?" + _query(cfg, id=box["report_id"])) \
        if RA.can_view(report, ctx["viewer"]) else ""
    return render_template("portal/_report_box.html", box=box, cfg=cfg, d=data, chart=chart, headline=headline,
                           subj=RB.SUBJECTS[cfg["subject"]], RB=RB, cid=f"box{box_id}",
                           compare_label=(RB.compare_range(cfg) or (None, None, ""))[2],
                           drill_base=url_for("reports.drill") + "?" + urlencode(dict(flt, did=dash_id, bid=box_id)),
                           open_url=open_url)


@reports_bp.route("/reports/dashboards/<int:dash_id>/edit")
@team_feature("reports.custom")
def dashboard_edit(dash_id: int):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    d = _dash(ctx, dash_id, "edit")
    if not d:
        flash("You can't change that dashboard.", "warning")
        return redirect(url_for("reports.dashboards"))
    v = ctx["viewer"]
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM report_defs WHERE tenant_id=%s ORDER BY name", (ctx["tenant_id"],))
        reports = [r for r in cur.fetchall() if r["subject"] in RB.SUBJECTS and
                   RB.subject_ok(r["subject"], b2b) and RA.can_view(r, v)]
    finally:
        conn.close()
    for r in reports:
        r["subject_label"] = RB.SUBJECTS[r["subject"]]["label"]
    return render_template("portal/report_dashboard_edit.html", customer=customer, dash=d, reports=reports,
                           ready=_offer(b2b),
                           sizes=RD.SIZES, dash_filters=RD.DASH_FILTERS, RB=RB, chart_names=dict(RB.CHARTS),
                           can_manage=RA.can_manage(d, v))


@reports_bp.route("/reports/dashboards/new", methods=["POST"])
@team_feature("reports.custom")
def dashboard_new():
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
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
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    if not _dash(ctx, dash_id, "edit"):
        flash("You can't change that dashboard.", "warning")
        return redirect(url_for("reports.dashboards"))
    rid = request.form.get("report_id", type=int)
    ready = request.form.get("ready") or ""
    if ready:
        r = RS.READY_BY_KEY.get(ready)
        if not r or ready not in {x["key"] for x in _offer(b2b)}:
            flash("That ready-made report is not available for your business.", "warning")
            return redirect(url_for("reports.dashboard_edit", dash_id=dash_id))
        rid = _save_report(ctx, customer, b2b, r["label"], _md(r["cfg"])) or -1
    if rid and rid != -1 and not RA.can_view(_saved(ctx["tenant_id"], rid), ctx["viewer"]):
        rid = -1
    note = (request.form.get("note") or "").strip()
    bid = None if rid == -1 else RD.add_box(ctx["tenant_id"], dash_id, report_id=rid, note=None if rid else (note or "New note"))
    if not bid:
        flash(f"Could not add that box (a dashboard holds up to {RD.MAX_BOXES}).", "warning")
    return redirect(url_for("reports.dashboard_edit", dash_id=dash_id) + (f"#box-{bid}" if bid else ""))


@reports_bp.route("/reports/dashboards/<int:dash_id>/share", methods=["POST"])
@team_feature("reports.custom")
def dashboard_share(dash_id: int):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
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
