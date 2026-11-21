"""
report_builder_routes.py — Reports › report builder (R1 19 Nov 2026; charts R2; sharing R4).
See report_builder.py (engine), report_access.py (sharing), report_dashboard_routes.py (R3).

  GET  /reports/all                        Reports home: saved reports, folders, mine / shared with me
  GET  /reports/builder                    build / open a report (?id=<saved report>)
  POST /reports/builder/save               save (update, or save as a new report)
  POST /reports/builder/<id>/share         who can open it, what numbers they see, others can edit
  POST /reports/builder/<id>/delete        delete a saved report (its owner only)
  GET  /reports/builder/export/<fmt>       csv | xlsx | pdf (free settings, ?rid= saved report, or ?did=&bid= box)

Old report pages stay where they are until phase R5 (user decision pending).
"""
import json
from datetime import date
from urllib.parse import urlencode

import psycopg2.extras
from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

import quote_requests as qr
import report_access as RA
import report_plan as PLAN
import report_plain as RP
import report_subjects as RS
import report_builder as RB
import report_dashboards as RD
from db import get_db_connection
from feature_access import team_feature
from portal_routes import (
    _crm_report_ctx, _current_actor, _customer_id, _export_csv, _get_customer, _inbox_see_all,
    _inject_granted_features, _leads_see_all, _quotes_scope, _require_login, _require_team_permission,
)

reports_bp = Blueprint("reports", __name__)
reports_bp.context_processor(_inject_granted_features)

CONFIG_KEYS = ("subject", "col", "cols_set", "period", "date_from", "date_to", "datef", "f", "g1", "g2", "m",
               "view", "sort", "dir", "page", "chart", "cmp")


class NotAllowed(Exception):
    pass


def _guard():
    """Login + the role's Reports permission. Open to every plan (user 19 Nov 2026:
    "open to all plans") — unlike the old Custom Report, no plan lock."""
    r = _require_login()
    if r:
        return None, r
    r = _require_team_permission("reports.custom")
    if r:
        return None, r
    customer = _get_customer(_customer_id())
    if not customer:
        return None, redirect(url_for("portal.login"))
    return customer, None


# Old report pages retired 19 Nov 2026 (user): their links and bookmarks open the matching
# ready-made report (or Create a report). Their Excel/PDF downloads and the per-campaign
# email report are left as they were.
RETIRED = {
    "portal.reports_page": "new_wa_customers",          # WhatsApp Report (user 19 Nov: retire, drop products browsed)
    "portal.whatsapp_reports": "handovers", "portal.report_usage": "ai_usage",
    "portal.report_leads_sources": "where_enquiries", "portal.report_not_a_fit": "not_a_fit",
    "calls.calls_report": "calls", "portal.report_cart": "cart_recovery",
    "portal.email_campaigns_reports": "campaigns",
    "portal.report_custom_picker": None, "portal.report_custom_entity": None, "portal.report_crm": None,
}


@reports_bp.before_app_request
def _retired_reports():
    if request.method != "GET" or request.endpoint not in RETIRED:
        return None
    key = RETIRED[request.endpoint]
    r = RS.READY_BY_KEY.get(key) if key else None
    if not r:
        return redirect(url_for("reports.wizard"))
    return redirect(url_for("reports.view", ready=key))


def _custom_only(ctx, json_ok=False):
    """Growth + Scale only (21 Nov 2026): the report builder, own dashboards, sharing.
    Starter gets the simple reports; this sends them back with a plain note. None when allowed."""
    if PLAN.custom_ok(ctx["tenant_id"]):
        return None
    if json_ok:
        return ("", 403)
    flash("Custom reports and your own dashboards come with Growth and Scale.", "info")
    return redirect(url_for("reports.library"))


def _setup():
    """(customer, ctx, allowed_subjects, quote_scope, None) or (…, response)."""
    customer, err = _guard()
    if err:
        return None, None, False, None, err
    RA.ensure_columns()
    ctx = _crm_report_ctx(customer)
    ctx["viewer"] = RA.viewer_of(_current_actor(customer), _leads_see_all())
    allowed = _allowed(ctx["tenant_id"])
    ctx["allowed"] = allowed
    return customer, ctx, allowed, _quotes_scope(customer), None


def _allowed(tenant_id: int) -> set:
    """The report subjects this business (and this viewer) may use. Passed around
    where older code passed the B2B-plan flag — report_builder.allowed_set takes both."""
    out = {k for k, s in RB.SUBJECTS.items() if not s["b2b"] and not s.get("gate")}
    if qr.allowed(tenant_id):
        out.add("quotes")
    gates = {"inbox": _inbox_see_all(), "manager": _leads_see_all()}
    missing = {k for k, tables in RS.NEEDS.items() if not all(RB.table_exists(t) for t in tables)}
    try:
        import xero_link as X
        has_shop = X._has_shop(tenant_id)
    except Exception:
        has_shop = False
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        def any_row(table):
            if not RB.table_exists(table):
                return False
            cur.execute(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE tenant_id=%s)", (tenant_id,))
            return cur.fetchone()[0]
        has_orders, has_codes, has_carts = any_row("orders"), any_row("chat_discount_codes"), any_row("cart_events")
    except Exception as e:
        conn.rollback()
        print("⚠️ report subjects check:", e)
        has_orders = has_codes = has_carts = False
    finally:
        conn.close()
    gates.update({"orders": has_orders or has_shop, "shop": has_shop or has_codes, "carts": has_carts})
    for k, s in RB.SUBJECTS.items():
        if s.get("gate") and gates.get(s["gate"]):
            out.add(k)
    return out - missing


def _unscoped(ctx):
    """Same names and money, without the viewer's own-records limit (whole-business sharing)."""
    return dict(ctx, ls=lambda cur, sql, params=None: cur.execute(sql, params))


def _conn():
    conn = get_db_connection()
    RB.ensure_tables(conn)
    return conn


def _saved(tenant_id: int, report_id: int):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM report_defs WHERE id=%s AND tenant_id=%s", (report_id, tenant_id))
        return cur.fetchone()
    finally:
        conn.close()


def _args_from_saved(row) -> MultiDict:
    md = MultiDict()
    for k, v in (row["config"] or {}).items():
        for item in (v if isinstance(v, list) else [v]):
            md.add(k, item)
    return md


def _query(cfg: dict, **extra) -> str:
    q = dict(RB.config_of(cfg))
    q.update({k: v for k, v in extra.items() if v not in (None, "")})
    return urlencode(q, doseq=True)


# ── Dashboard filters (used by boxes, their drill-down and export) ───────────

def _dash_filters(args):
    period = args.get("period") or ""
    return {"period": period if RB.period_ok(period) else "",
            "date_from": RB._iso(args.get("date_from")), "date_to": RB._iso(args.get("date_to")),
            "member": (args.get("member") or "")[:64],
            "cmp": ""}


def _dash_filter_qs(flt) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in flt.items() if v}


def _box_cfg(box, b2b, flt, active):
    cfg = RB.read_config(_args_from_saved({"config": box["config"]}), b2b)
    if box["use_filters"]:
        cfg = RB.apply_dashboard(cfg, period=flt["period"] if "period" in active else None,
                                 d_from=flt["date_from"], d_to=flt["date_to"],
                                 member=flt["member"] if "member" in active else None)
    return cfg


def _resolve(args, ctx, b2b, quote_scope, drill=False):
    """(cfg, ctx, quote_scope, source) for free settings, a saved report (?rid=) or a
    dashboard box (?did=&bid=). Whole-business sharing only for the saved forms."""
    v = ctx["viewer"]
    whole, source = False, None
    did, bid, rid = args.get("did", type=int), args.get("bid", type=int), args.get("rid", type=int)
    if did and bid:
        d = RD.get_dashboard(ctx["tenant_id"], did)
        if not RA.can_view(d, v):
            raise NotAllowed()
        box = next((b for b in d["boxes"] if b["id"] == bid), None)
        import report_widgets as RW
        if box and RW.kind_of(box) in ("number", "list", "board"):     # widgets (21 Nov 2026)
            cfg = RW.cfg_for(box, b2b, _dash_filters(args), set(d["filters"] or []))
            if not cfg:
                raise NotAllowed()
        elif not box or not box["report_id"] or not RB.subject_ok(box["subject"], b2b):
            raise NotAllowed()
        else:
            cfg = _box_cfg(box, b2b, _dash_filters(args), set(d["filters"] or []))
        whole, source = RA.whole_business(d), {"dash": d, "box": box}
    elif rid:
        saved = _saved(ctx["tenant_id"], rid)
        if not RA.can_view(saved, v):
            raise NotAllowed()
        cfg = RB.read_config(_args_from_saved(saved), b2b)
        whole, source = RA.whole_business(saved), {"report": saved}
    else:
        cfg = RB.read_config(args, b2b)
    if drill:
        cfg = RB.drill_cfg(cfg, args.get("k1") if "k1" in args else None, args.get("k2") if "k2" in args else None)
    if whole:
        return cfg, _unscoped(ctx), None, source
    return cfg, ctx, quote_scope, source


def _results(ctx, cfg, quote_scope, export=False):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if cfg["view"] == "summary":
            return {"summary": RB.run_summary(cur, ctx, cfg, quote_scope)}
        rows, total = RB.run_detailed(cur, ctx, cfg, export=export, quote_scope=quote_scope)
        return {"rows": rows, "total": total}
    except Exception as e:
        conn.rollback()
        print("⚠️ report builder query:", e)
        return {"error": "This report could not be worked out. Try removing the last change."}
    finally:
        conn.close()


def _chart_and_totals(ctx, cfg, quote_scope, data):
    """(headline, chart) — totals, the period compared with, and what the chart draws."""
    subj = RB.SUBJECTS[cfg["subject"]]
    m = RB.measure_of(cfg)
    prev_cfg = RB.shifted(cfg)
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        total = RB.run_total(cur, ctx, cfg, quote_scope)
        prev_total = RB.run_total(cur, ctx, prev_cfg, quote_scope) if prev_cfg else None
        summary = data.get("summary")
        if cfg["g1"] and summary is None:
            summary = RB.run_summary(cur, ctx, dict(cfg, view="summary"), quote_scope)
        prev = RB.run_summary(cur, ctx, dict(prev_cfg, view="summary"), quote_scope) if (prev_cfg and cfg["g1"]) else None
        if summary is not None and prev is not None:
            RB.compare_rows(summary, prev, cfg)
        if data.get("summary") and prev_cfg:
            s = data["summary"]
            ffmt = m[3] if m[2] else "int"
            s["headers"] += [RB.compare_range(cfg)[2], "Change"]
            for r in s["rows"]:
                cur_v = r["mv"] if m[2] else r["n"]
                r["cells"] += [RB.fmt(r.get("prev", 0), ffmt, ctx), RB.change_text(cur_v or 0, r.get("prev", 0))]
            pv = prev_total[1] if m[2] else prev_total[0]
            cv = total[1] if m[2] else total[0]
            s["total_row"]["cells"] += [RB.fmt(pv, ffmt, ctx) if pv is not None else "—",
                                        RB.change_text(cv or 0, pv or 0)]
        kind = RB.chosen_chart(cfg, len(summary["rows"]) if summary else 0)
        if kind == "pivot" and summary is not None:
            rows_tot = RB.run_summary(cur, ctx, dict(cfg, g2="", view="summary"), quote_scope)
            cols_tot = RB.run_summary(cur, ctx, dict(cfg, g1=cfg["g2"], g2="", view="summary"), quote_scope)
            chart = RB.build_pivot(cfg, summary, rows_tot, cols_tot, total, ctx)
        else:
            chart = RB.build_chart(cfg, summary, prev, ctx, total, prev_total)
    except Exception as e:
        conn.rollback()
        print("⚠️ report builder chart:", e)
        return "", {"kind": "table", "error": True}
    finally:
        conn.close()
    headline = f"{total[0]:,} {subj['noun']}"
    if m[2] and total[1] is not None:
        headline += " · " + m[1] + " " + RB.fmt(total[1], m[3], ctx)
    if prev_total is not None:
        headline += " · " + RB.change_text((total[1] if m[2] else total[0]) or 0,
                                           (prev_total[1] if m[2] else prev_total[0]) or 0) + " vs " + \
                    RB.compare_range(cfg)[2].lower()
    return headline, chart


def _visible_reports(ctx, b2b):
    v = ctx["viewer"]
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM report_defs WHERE tenant_id=%s ORDER BY COALESCE(last_opened_at, updated_at) DESC",
                    (ctx["tenant_id"],))
        return [r for r in cur.fetchall() if r["subject"] in RB.SUBJECTS and
                RB.subject_ok(r["subject"], b2b) and RA.can_view(r, v)]
    finally:
        conn.close()


def _share_choices(ctx):
    people = [(p["key"], p["display"]) for p in ctx["people"] if p["key"] != ctx["viewer"]["key"]]
    return people, RA.roles(ctx["tenant_id"])


@reports_bp.route("/reports/all")
@team_feature("reports.custom")
def library():
    """Reports home (approved mock-up screen 11): dashboards, your reports first, ready-made questions."""
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    v = ctx["viewer"]
    show = request.args.get("show") if request.args.get("show") in ("mine", "shared") else "all"
    folder = (request.args.get("folder") or "").strip()[:RA.MAX_FOLDER]
    custom = PLAN.custom_ok(ctx["tenant_id"])
    every = _visible_reports(ctx, b2b)
    if not custom:    # Starter: the fixed Sales overview's own box reports stay out of "Your saved reports"
        every = [r for r in every if r.get("folder") != "Sales overview"]
    folders = sorted({r["folder"] for r in every if r.get("folder")}, key=str.lower)
    counts = {"all": len(every), "mine": sum(1 for r in every if RA.is_mine(r, v))}
    counts["shared"] = counts["all"] - counts["mine"]
    reports = []
    for r in every:
        if show == "mine" and not RA.is_mine(r, v):
            continue
        if show == "shared" and RA.is_mine(r, v):
            continue
        if folder and r.get("folder") != folder:
            continue
        try:
            rcfg = RB.read_config(_args_from_saved(r), b2b)
            sentence = RP.plain_sentence(rcfg)
            kind = (r["config"] or {}).get("chart") or ("auto" if rcfg["g1"] else "number")
        except Exception:
            sentence, kind = RB.SUBJECTS[r["subject"]]["label"], "auto"
        reports.append({**r, "sentence": sentence, "icon": RP.CHART_ICON.get(kind, "▮▮"),
                        "sharing": RA.sharing_label(r), "mine": RA.is_mine(r, v), "url": RSR.open_url(r, custom)})
    dashboards = [d for d in RD.list_dashboards(ctx["tenant_id"]) if RA.can_view(d, v)]
    is_b2b = "quotes" in b2b
    is_b2c = bool({"orders", "discounts", "carts"} & b2b)
    offered = {r["key"]: r for r in RS.ready_for(b2b, is_b2b, is_b2c)}
    topics = []
    for key, label, keys in RP.TOPICS:
        items = []
        for k in keys:
            r = offered.get(k)
            if not r:
                continue
            items.append({"key": k, "q": RP.QUESTIONS.get(k, r["label"]), "label": r["label"], "desc": r["desc"],
                          "how": RP.CHART_WORD.get(r["cfg"].get("chart", "auto"), "Chart") + " · " + r["desc"],
                          "icon": RP.CHART_ICON.get(r["cfg"].get("chart", "auto"), "▮▮"),
                          "url": url_for("reports.view", ready=k)})
        if items:
            topics.append({"key": key, "label": label, "qs": items})
    return render_template("portal/report_home.html", customer=customer, reports=reports, topics=topics,
                           show=show, folder=folder, folders=folders, counts=counts, dashboards=dashboards, RA=RA,
                           custom_ok=custom, can_new_dashboard=custom)


@reports_bp.route("/reports/new")
@team_feature("reports.custom")
def wizard():
    """Create a report — the wizard (approved mock-up screen 8)."""
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    spec = RP.wizard_spec(b2b, ctx)
    start = request.args.get("subject") or ""
    dash = None
    did = request.args.get("dash", type=int)
    if did:   # "Build new" from a dashboard: Create saves the report and adds it there (20 Nov fix round)
        d = RD.get_dashboard(ctx["tenant_id"], did)
        if d and RA.can_edit(d, ctx["viewer"]):
            dash = {"id": d["id"], "name": d["name"],
                    "url": url_for("reports.dashboard_add_new", dash_id=d["id"]),
                    "back": url_for("reports.dashboard_edit", dash_id=d["id"])}
    return render_template("portal/report_wizard.html", customer=customer, spec=spec, dash=dash,
                           start=start if any(x["key"] == start for x in spec["subjects"]) else "")


@reports_bp.route("/reports/preview")
@team_feature("reports.custom")
def preview():
    """The wizard's live preview: the chart (or number / first rows) for the settings so far."""
    customer, ctx, b2b, quote_scope, err = _setup()
    if err or _custom_only(ctx, json_ok=True):
        return "", 403
    cfg = RB.read_config(request.args, b2b)
    data = _results(ctx, dict(cfg, view="summary" if cfg["g1"] else "detailed", page=1), quote_scope)
    headline, chart = ("", {"kind": "table"}) if data.get("error") else _chart_and_totals(ctx, cfg, quote_scope, data)
    return render_template("portal/_report_preview.html", cfg=cfg, d=data, chart=chart, headline=headline,
                           subj=RB.SUBJECTS[cfg["subject"]], RB=RB,
                           compare_label=(RB.compare_range(cfg) or (None, None, ""))[2])


def _sentence(cfg, url_with):
    """The report as a sentence; each pick has its choices as links (mock-up screen 9)."""
    subj = RB.SUBJECTS[cfg["subject"]]
    m = RB.measure_of(cfg)
    which, rest = RP.which_of(cfg)
    rest_raw = [f"{f['key']}|{f['op']}|{f['val']}" for f in rest]

    def fl(raw):
        out = []
        for x in raw:
            k, _, r = x.partition("|"); op, _, v = r.partition("|")
            out.append({"key": k, "op": op, "val": v})
        return out

    parts = [{"t": "Show "},
             {"pick": "count", "text": RP.measure_words(cfg["subject"], m), "label": "What to count",
              "options": [{"label": RP.measure_card(cfg["subject"], x)[0], "on": x[0] == cfg["m"],
                           "url": url_with(m=x[0])} for x in subj["measures"]]},
             {"t": " "},
             {"pick": "which", "text": which[2], "label": "Which " + subj["noun"],
              "options": [{"label": o[1], "on": o[0] == which[0], "url": url_with(f=fl(rest_raw + o[3]))}
                          for o in RP.WHICH.get(cfg["subject"], [])]}]
    splits = [s for s in RP.SPLITS.get(cfg["subject"], []) if RB._group_ok(subj, s[0])]
    split_opts = [{"label": s[1], "on": s[0] == cfg["g1"], "url": url_with(g1=s[0], g2="" if s[0] == cfg["g2"] else cfg["g2"],
                                                                           view="summary", chart="auto")} for s in splits]
    split_opts.append({"label": "Nothing (one number)", "on": not cfg["g1"], "url": url_with(g1="", g2="", view="detailed", chart="auto")})
    parts += [{"t": ", split by " if cfg["g1"] else ", as "},
              {"pick": "split", "text": RP.split_words(cfg, cfg["g1"]) if cfg["g1"] else "one number", "label": "Split it by",
               "options": split_opts,
               "more": [{"label": l, "url": url_with(g1=v, view="summary", chart="auto"), "on": v == cfg["g1"]}
                        for v, l in RP.split_choices(cfg["subject"])]}]
    if cfg["g2"]:
        parts += [{"t": " and "}, {"pick": "split2", "text": RP.split_words(cfg, cfg["g2"]), "label": "And also split by",
                                   "options": [{"label": "Nothing", "on": False, "url": url_with(g2="", chart="auto")}],
                                   "more": [{"label": l, "url": url_with(g2=v), "on": v == cfg["g2"]}
                                            for v, l in RP.split_choices(cfg["subject"]) if v != cfg["g1"]]}]
    parts += [{"t": ", for " if cfg["period"] != "all" else ", for "},
              {"pick": "period", "text": RP.period_words(cfg), "label": "Time", "picker": RB.picker_value(cfg), "options": []}]
    parts.append({"t": "."})
    return parts, rest


def _builder_to_simple(report_id):
    """Starter: a builder link (old bookmark, ready-made link, retired page) opens the simple report page."""
    if report_id:
        return redirect(url_for("reports.view", id=report_id))
    key = request.args.get("ready") or ""
    if key in RS.READY_BY_KEY:
        return redirect(url_for("reports.view", ready=key))
    flash("Custom reports come with Growth and Scale. Pick a ready-made report below.", "info")
    return redirect(url_for("reports.library"))


@reports_bp.route("/reports/builder")
@team_feature("reports.custom")
def builder():
    customer, ctx, b2b, quote_scope, err = _setup()
    if err:
        return err
    v = ctx["viewer"]
    report_id = request.args.get("id", type=int)
    if not PLAN.custom_ok(ctx["tenant_id"]):
        return _builder_to_simple(report_id)
    if not report_id and not any(k in request.args for k in CONFIG_KEYS):
        return redirect(url_for("reports.wizard"))          # a brand-new report starts in the wizard
    saved = _saved(ctx["tenant_id"], report_id) if report_id else None
    if report_id and not RA.can_view(saved, v):
        flash("That saved report was not found, or it has not been shared with you.", "warning")
        return redirect(url_for("reports.library"))
    args = request.args
    as_saved = bool(saved) and not any(k in request.args for k in CONFIG_KEYS)
    if as_saved:
        args = _args_from_saved(saved)
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE report_defs SET last_opened_at=NOW() WHERE id=%s", (saved["id"],))
            conn.commit()
        finally:
            conn.close()
    cfg = RB.read_config(args, b2b)
    whole = as_saved and RA.whole_business(saved)
    rctx, rscope = (_unscoped(ctx), None) if whole else (ctx, quote_scope)
    subj = RB.SUBJECTS[cfg["subject"]]

    # Which tab: "Chart and totals" or "List of …"
    chart_kind_wanted = cfg.get("chart") or "auto"
    tab = "list" if (cfg["view"] == "detailed" and (cfg["g1"] or chart_kind_wanted == "table")) else "chart"
    if tab == "list":
        data = _results(rctx, dict(cfg, view="detailed"), rscope)
    elif cfg["g1"]:
        data = _results(rctx, dict(cfg, view="summary"), rscope)
    else:
        data = {}
    headline, chart = ("", {"kind": "table"}) if data.get("error") else _chart_and_totals(rctx, dict(cfg, view="summary" if cfg["g1"] else cfg["view"]), rscope, data)
    chart_rules = RB.chart_rules(cfg)
    suggested = RB.suggest_chart(cfg, len(data["summary"]["rows"]) if data.get("summary") else 0)

    total_pages = 1
    if "total" in data:
        total_pages = max(1, (data["total"] + RB.PER_PAGE - 1) // RB.PER_PAGE)

    keep_name = "" if saved else ((RS.READY_BY_KEY.get(request.args.get("ready") or "") or {}).get("label", "")
                                  or request.args.get("name", "")[:120])

    def url_with(**changes):
        c = dict(cfg, page=1)
        c.update(changes)
        return url_for("reports.builder") + "?" + _query(c, id=report_id, name=keep_name)

    def link(**extra):
        return url_for("reports.builder") + "?" + _query(cfg, id=report_id, name=keep_name, **extra)

    def sort_link(key):
        d = "asc" if cfg["sort"] == key and cfg["dir"] == "desc" else "desc"
        return url_for("reports.builder") + "?" + _query(dict(cfg, sort=key, dir=d, view="detailed"), id=report_id, name=keep_name)

    filter_fields = []
    for gname, cols in subj["groups"]:
        for c in cols:
            if c["filter"]:
                filter_fields.append({"key": c["key"], "label": c["label"], "group": gname, "type": c["filter"],
                                      "options": RB.choice_options(cfg["subject"], c["key"], ctx)
                                      if c["filter"] == "choice" else []})
    columns = [{"key": c["key"], "label": c["label"], "group": gname} for gname, cols in subj["groups"] for c in cols]
    sentence, other_filters = _sentence(cfg, url_with)

    qs = _query(cfg)
    if as_saved:
        drill_base = url_for("reports.drill") + "?" + urlencode({"rid": saved["id"]})
        export_qs = urlencode({"rid": saved["id"]})
    else:
        drill_base = url_for("reports.drill") + "?" + qs
        export_qs = qs
    people, role_list = _share_choices(ctx)
    every = _visible_reports(ctx, b2b)
    shown_chart = chart.get("kind")
    main_charts = ["bar", "pie", "line", "table", "number"]
    return render_template(
        "portal/report_builder.html", customer=customer, RB=RB, RA=RA, RP=RP, cfg=cfg, subj=subj, d=data, headline=headline,
        saved=saved, report_id=report_id, tab=tab,
        sentence=sentence, dp_presets=RB.picker_presets(), dp_value=RB.picker_value(cfg),
        other_filters=[{"label": RB.filter_label(cfg, f, ctx),
                        "remove": url_with(f=[x for x in cfg["f"] if x is not f])} for f in other_filters],
        filter_fields_json=json.dumps(filter_fields), filter_ops_json=json.dumps(RB.FILTER_OPS),
        columns_json=json.dumps(columns), chosen_cols_json=json.dumps(cfg["col"]),
        default_cols_json=json.dumps(subj["default_cols"]),
        base_qs=_query(dict(cfg, page=1), id=report_id, name=keep_name),
        sort_link=sort_link, page=cfg["page"], total_pages=total_pages,
        prev_url=link(page=cfg["page"] - 1, view="detailed") if cfg["page"] > 1 else None,
        next_url=link(page=cfg["page"] + 1, view="detailed") if cfg["page"] < total_pages else None,
        chart_tab_url=url_with(view="summary") if cfg["g1"] else url_with(view="detailed", chart="number"),
        list_tab_url=url_with(view="detailed", **({} if cfg["g1"] else {"chart": "table"})),
        g2_options=[(v_, l_) for v_, l_ in RP.split_choices(cfg["subject"]) if v_ != cfg["g1"]],
        g2_url=lambda val: url_with(g2=val), datef_url=lambda val: url_with(datef=val),
        export_qs=export_qs, viewer=v,
        chart=chart, chart_rules=chart_rules, suggested=suggested, shown_chart=shown_chart, main_charts=main_charts,
        chart_link=lambda k: url_with(chart=k, view="summary" if cfg["g1"] else ("detailed")),
        compare_label=(RB.compare_range(cfg) or (None, None, ""))[2],
        drill_base=drill_base, whole=whole,
        whole_lost=bool(saved) and not as_saved and RA.whole_business(saved),
        can_edit=RA.can_edit(saved, v) if saved else True,
        can_manage=RA.can_manage(saved, v) if saved else True,
        dashboards=[d for d in RD.list_dashboards(ctx["tenant_id"]) if RA.can_edit(d, v)] if saved else [],
        used_in=RD.dashboards_using(ctx["tenant_id"], saved["id"]) if saved else [],
        folders=sorted({r["folder"] for r in every if r.get("folder")}, key=str.lower),
        share_people=people, share_roles=role_list,
        suggest_name=(RS.READY_BY_KEY.get(request.args.get("ready") or "") or {}).get("label", "")
                     or request.args.get("name", "")[:120],
    )


@reports_bp.route("/reports/builder/save", methods=["POST"])
@team_feature("reports.custom")
def save():
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    v = ctx["viewer"]
    cfg = RB.read_config(request.form, b2b)
    name = " ".join((request.form.get("name") or "").split())[:120]
    if not name:
        flash("Please give the report a name before saving.", "warning")
        return redirect(url_for("reports.builder") + "?" + _query(cfg))
    folder = " ".join((request.form.get("new_folder") or request.form.get("folder") or "").split())[:RA.MAX_FOLDER] or None
    report_id = request.form.get("id", type=int)
    as_new = request.form.get("as_new") == "1"
    if report_id and not as_new:
        existing = _saved(ctx["tenant_id"], report_id)
        if not existing or not RA.can_edit(existing, v):
            flash("You can't change this report. Use “Save as new” to keep your own copy.", "warning")
            return redirect(url_for("reports.builder") + "?" + _query(cfg, id=report_id))
    actor = _current_actor(customer)
    config = RB.config_of(cfg)
    conn = _conn()
    try:
        cur = conn.cursor()
        if report_id and not as_new:
            cur.execute("UPDATE report_defs SET name=%s, subject=%s, config=%s, folder=%s, updated_at=NOW() "
                        "WHERE id=%s AND tenant_id=%s RETURNING id",
                        (name, cfg["subject"], json.dumps(config), folder, report_id, ctx["tenant_id"]))
            if not cur.fetchone():
                report_id = None
        if not report_id or as_new:
            cur.execute("INSERT INTO report_defs (tenant_id, name, subject, config, folder, created_by_key, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        (ctx["tenant_id"], name, cfg["subject"], json.dumps(config), folder, actor["key"], actor["label"]))
            report_id = cur.fetchone()[0]
        conn.commit()
        flash(f"Report “{name}” saved.", "success")
    except Exception as e:
        conn.rollback()
        print("⚠️ report builder save:", e)
        flash("Could not save this report. Please try again.", "danger")
        return redirect(url_for("reports.builder") + "?" + _query(cfg))
    finally:
        conn.close()
    return redirect(url_for("reports.builder", id=report_id))


@reports_bp.route("/reports/builder/<int:report_id>/share", methods=["POST"])
@team_feature("reports.custom")
def share(report_id: int):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    v = ctx["viewer"]
    saved = _saved(ctx["tenant_id"], report_id)
    if not saved or not RA.can_manage(saved, v):
        flash("Only the person who saved this report can change who sees it.", "warning")
        return redirect(url_for("reports.library"))
    people, role_list = _share_choices(ctx)
    s = RA.clean_sharing(request.form, v, {k for k, _ in people}, {k for k, _ in role_list})
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE report_defs SET visibility=%s, share_with=%s, data_scope=%s, others_edit=%s, "
                    "created_by_key=COALESCE(created_by_key, %s) WHERE id=%s AND tenant_id=%s",
                    (s["visibility"], json.dumps(s["share_with"]), s["data_scope"], s["others_edit"], v["key"],
                     report_id, ctx["tenant_id"]))
        conn.commit()
    finally:
        conn.close()
    flash("Sharing saved.", "success")
    return redirect(url_for("reports.builder", id=report_id))


@reports_bp.route("/reports/builder/<int:report_id>/delete", methods=["POST"])
@team_feature("reports.custom")
def delete(report_id: int):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    saved = _saved(ctx["tenant_id"], report_id)
    if not saved or not RA.can_manage(saved, ctx["viewer"]):
        flash("Only the person who saved this report can delete it.", "warning")
        return redirect(url_for("reports.library"))
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM report_defs WHERE id=%s AND tenant_id=%s", (report_id, ctx["tenant_id"]))
        ok = cur.rowcount > 0
        conn.commit()
    finally:
        conn.close()
    flash(f"“{saved['name']}” deleted." if ok else "That report was not found.", "success" if ok else "warning")
    if request.form.get("next") == "custom":      # from Reports › Custom reports › My custom reports (21 Nov 2026)
        return redirect(url_for("reports.custom_mine"))
    return redirect(url_for("reports.library"))


@reports_bp.route("/reports/builder/export/<fmt>")
@team_feature("reports.custom")
def export(fmt: str):
    customer, ctx, b2b, quote_scope, err = _setup()
    if err:
        return err
    if fmt not in ("csv", "xlsx", "pdf"):
        flash("Invalid export request.", "danger")
        return redirect(url_for("reports.builder"))
    try:
        cfg, rctx, rscope, source = _resolve(request.args, ctx, b2b, quote_scope, drill=request.args.get("drill") == "1")
    except NotAllowed:
        flash("That report was not found, or it has not been shared with you.", "warning")
        return redirect(url_for("reports.library"))
    subj = RB.SUBJECTS[cfg["subject"]]
    data = _results(rctx, cfg, rscope, export=True)
    if data.get("error"):
        flash(data["error"], "danger")
        return redirect(url_for("reports.builder") + "?" + _query(cfg))
    title = " ".join((request.args.get("name") or "").split())[:120]
    if not title and source:
        title = (source.get("report") or {}).get("name") or (source.get("box") or {}).get("report_name") or ""
    title = title or subj["label"] + " report"
    store = customer.get("tenant_domain") or customer.get("tenant_name") or "Your business"
    subtitle = f"{store} · {cfg['period_label']} · Generated {date.today().strftime('%d %b %Y')}"
    if cfg["f"]:
        subtitle += " · Filters: " + "; ".join(RB.filter_label(cfg, f, ctx) for f in cfg["f"])
    pivot = None
    if cfg["g1"] and cfg["g2"] and cfg.get("chart") == "pivot":
        _, pivot = _chart_and_totals(rctx, cfg, rscope, dict(data))
        if pivot.get("kind") != "pivot":
            pivot = None
    elif "summary" in data and cfg["cmp"]:
        _chart_and_totals(rctx, cfg, rscope, data)      # adds the compared period's columns
    if pivot:
        headers = [pivot["row_label"] + " / " + pivot["col_label"]] + pivot["cols"] + ["Total"]
        rows = [[r["label"]] + [c["v"] for c in r["cells"]] + [r["total"]] for r in pivot["rows"]]
        rows.append(["Total"] + [c["v"] for c in pivot["col_totals"]] + [pivot["grand"]])
        subtitle += " · " + pivot["measure_label"]
    elif "summary" in data:
        s = data["summary"]
        headers = s["headers"]
        rows = [r["labels"] + r["cells"] for r in s["rows"]] + [s["total_row"]["labels"] + s["total_row"]["cells"]]
    else:
        headers = [subj["columns"][k]["label"] for k in cfg["col"]]
        rows = [r["cells"] for r in data["rows"]]
        if data["total"] > RB.EXPORT_MAX_ROWS:
            subtitle += f" · first {RB.EXPORT_MAX_ROWS:,} of {data['total']:,} rows"
    fname = "".join(ch if ch.isalnum() else "_" for ch in title.lower()).strip("_")[:60] + "_" + date.today().isoformat()
    if fmt == "csv":
        resp = _export_csv(title, subtitle, [], headers, rows)
        resp.headers["Content-Disposition"] = f"attachment; filename={fname}.csv"
        return resp
    import crm_report as crm
    if fmt == "xlsx":
        body = crm.export_xlsx(title, subtitle, headers, rows, {})
        return Response(body, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={fname}.xlsx"})
    body = crm.export_pdf(title, subtitle, headers, rows, {})
    return Response(body, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={fname}.pdf"})


import report_dashboard_routes  # noqa: E402,F401  (R3 routes live on reports_bp)
import report_simple_routes as RSR  # noqa: E402  (simple report page + Ask for a report, 21 Nov 2026)
