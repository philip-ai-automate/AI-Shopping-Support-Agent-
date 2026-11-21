"""
report_simple_routes.py — the simple report page, Ask for a report, home numbers (21 Nov 2026).
Approved mock-up https://claude.ai/artifact/Nbf3F8K2ShDdocrnpbrTGr (screens 2 and 3). Every plan.

  GET  /reports/view          a ready-made (?ready=key) or saved (?id=) report with four choices:
                              Time · Team member · Split by · Chart or List
  POST /reports/view/save     "Save as my report" / "Save changes"
  GET  /reports/ask           Ask for a report (?q=…): the AI Agent picks a ready-made report and its choices
  GET  /reports/home-numbers  the three numbers on Reports home's Sales overview strip (loaded after the page)

Routes sit on reports_bp (report_builder_routes imports this module at its end).
"""
import json
from datetime import date
from urllib.parse import urlencode

import psycopg2.extras
from flask import flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.datastructures import MultiDict

import report_access as RA
import report_ask as RK
import report_builder as RB
import report_plain as RP
import report_plan as PLAN
import report_subjects as RS
from db import get_db_connection
from feature_access import team_feature
from portal_routes import _current_actor
from report_builder_routes import (
    _args_from_saved, _chart_and_totals, _conn, _custom_only, _query, _results, _saved, _setup, _unscoped,
    _visible_reports, reports_bp,
)

SIMPLE = "simple"           # report_defs.config["kind"] for reports saved from the simple page


def _member_col(subject):
    return RB.DASH_MEMBER_COL.get(subject) or RB.SUBJECTS[subject].get("member")


def _split_options(cfg0, cfg):
    """[(value, label)] for "Split by": the plain choices for the subject, plus the report's own split."""
    subj = RB.SUBJECTS[cfg["subject"]]
    opts = [(k, label) for k, label, _h in RP.SPLITS.get(cfg["subject"], []) if RB._group_ok(subj, k)]
    for g in (cfg0["g1"], cfg["g1"]):
        if g and g not in {k for k, _ in opts}:
            opts.insert(0, (g, RP.split_words(dict(cfg, g1=g), g).capitalize()))
    return opts


def _base(ctx, b2b, args):
    """(base cfg, title, ready key, saved row) for ?ready= or ?id=, or None when not found / not allowed."""
    rid = args.get("id", type=int) if hasattr(args, "get") else None
    if rid:
        saved = _saved(ctx["tenant_id"], rid)
        if not saved or not RA.can_view(saved, ctx["viewer"]) or not RB.subject_ok(saved["subject"], b2b):
            return None
        ready = (saved["config"] or {}).get("ready") or ""
        return RB.read_config(_args_from_saved(saved), b2b), saved["name"], ready, saved
    key = args.get("ready") or ""
    r = RS.READY_BY_KEY.get(key)
    if not r or not RB.subject_ok(r["cfg"]["subject"], b2b):
        return None
    return RB.read_config(_args_from_saved({"config": r["cfg"]}), b2b), RP.QUESTIONS.get(key, r["label"]), key, None


def choices_of(args):
    """The four choices as they arrive (query string or form)."""
    return {"period": args.get("period") or "", "date_from": args.get("date_from") or "",
            "date_to": args.get("date_to") or "", "member": (args.get("member") or "")[:64],
            "split": args.get("split"), "show": args.get("show") if args.get("show") in ("chart", "list") else ""}


def apply_choices(cfg0, ch):
    """The report with the person's choices applied. Never adds columns or filters of its own."""
    cfg = dict(cfg0, f=list(cfg0["f"]), page=1)
    if ch["period"] and RB.period_ok(ch["period"]):
        cfg = RB.apply_dashboard(cfg, period=ch["period"], d_from=RB._iso(ch["date_from"]), d_to=RB._iso(ch["date_to"]))
    if ch["member"] and _member_col(cfg["subject"]):
        cfg = RB.apply_dashboard(cfg, member=ch["member"])
    subj = RB.SUBJECTS[cfg["subject"]]
    if ch["split"] is not None:
        g = ch["split"] if ch["split"] == "" else RB._group_ok(subj, ch["split"])
        if ch["split"] == "":
            cfg.update(g1="", g2="", chart="number" if cfg0.get("chart") != "table" else "table")
        elif g and g != cfg0["g1"]:
            cfg.update(g1=g, g2="", chart=_kept_chart(cfg0.get("chart") or "auto", g))
        elif g == cfg0["g1"]:
            cfg.update(g1=cfg0["g1"], g2=cfg0["g2"], chart=cfg0["chart"])
    show = ch["show"] or ("list" if (not cfg["g1"] and cfg.get("chart") == "table") else "chart")
    if show == "list":
        cfg["view"] = "detailed"
    else:
        cfg["view"] = "summary" if cfg["g1"] else "detailed"
        if not cfg["g1"]:
            cfg["chart"] = "number"
    if not cfg["col"]:
        cfg["col"] = list(subj["default_cols"])
    return cfg, show


def _kept_chart(was, g):
    """Same kind of chart when "Show per" changes (21 Nov 2026 fix): bars stay bars, a pie stays a pie.
    Charts that only fit the old grouping (funnel = stages, stacked / pivot = two groupings) become bars."""
    by_date = "@" in g
    if by_date:
        return was if was in ("bar", "line", "area", "combo") else "bar"
    return was if was in ("bar", "hbar", "pie", "donut") else "hbar"


def title_for(cfg, cfg0, base_title, split_label):
    """(title, from_question). The title follows the person's "Show per" choice, so it always says what the
    chart shows; the ready-made question it started from is shown small above it."""
    if cfg["g1"] == cfg0["g1"]:
        return base_title, ""
    m = RB.measure_of(cfg)
    which = RP.which_of(cfg)[0][2]
    if which.startswith("all ") and m[0] == "count":
        which = which[4:]                      # "Deals by team member", not "All deals by team member"
    what = which[:1].upper() + which[1:] if m[0] == "count" else \
        m[1].replace("Sum of ", "").replace("Number of records", "Number") + " of " + which
    what = what[:1].upper() + what[1:]
    tail = (" by " + split_label[:1].lower() + split_label[1:]) if cfg["g1"] else " in total"
    return what + tail, base_title


GROUP_MAX = 20            # List view: at most this many groups on the page (Download gives everything)


def grouped_list(ctx, cfg, scope):
    """List view with "Show per" chosen (21 Nov 2026, approved mock-up screen 5): the records grouped under
    the choice — one heading and count per group, the chosen column first, up to PER_PAGE rows a group.
    Returns (groups, columns, number of groups) or None when it can't be worked out."""
    subj = RB.SUBJECTS[cfg["subject"]]
    gcol = cfg["g1"].partition("@")[0]
    rest = [c for c in (cfg["col"] or subj["default_cols"]) if c != gcol]
    cols = ([gcol] if gcol in subj["columns"] else []) + rest
    base = dict(cfg, g2="", view="summary", col=cols, chart="auto")
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        summ = RB.run_summary(cur, ctx, base, scope)
        out = []
        for r in summ["rows"][:GROUP_MAX]:
            dc = RB.drill_cfg(base, r["rk"][0])
            dc["col"] = cols
            rows, total = RB.run_detailed(cur, ctx, dc, quote_scope=scope)
            out.append({"label": r["labels"][0], "n": total, "rows": rows, "rk": r["rk"][:1],
                        "more": max(0, total - len(rows))})
        return out, cols, len(summ["rows"])
    except Exception as e:
        conn.rollback()
        print("⚠️ report simple grouped list:", e)
        return None
    finally:
        conn.close()


def _tiles(ctx, cfg, scope):
    subj = RB.SUBJECTS[cfg["subject"]]
    m = RB.measure_of(cfg)
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        n, val = RB.run_total(cur, ctx, dict(cfg, view="summary" if cfg["g1"] else "detailed"), scope)
    except Exception as e:
        conn.rollback()
        print("⚠️ report simple totals:", e)
        return []
    finally:
        conn.close()
    out = [{"label": "Number of " + subj["noun"], "value": f"{(n or 0):,}"}]
    if m[2] and val is not None:
        out.append({"label": m[1].replace("Sum of ", "Total ").replace("Number of records", "Number"),
                    "value": RB.fmt(val, m[3], ctx)})
    return out


def _view_url(ready, rid, ch, **over):
    c = dict(ch, **over)
    q = {k: v for k, v in c.items() if k != "split" and v not in (None, "")}
    if c.get("split") is not None:
        q["split"] = c["split"]
    if rid:
        q["id"] = rid
    elif ready:
        q["ready"] = ready
    return url_for("reports.view") + "?" + urlencode(q)


@reports_bp.route("/reports/view")
@team_feature("reports.custom")
def view():
    customer, ctx, b2b, quote_scope, err = _setup()
    if err:
        return err
    base = _base(ctx, b2b, request.args)
    if not base:
        flash("That report was not found, or it has not been shared with you.", "warning")
        return redirect(url_for("reports.library"))
    cfg0, title, ready, saved = base
    ch = choices_of(request.args)
    cfg, show = apply_choices(cfg0, ch)
    try:
        cfg["page"] = max(1, int(request.args.get("page") or 1))
    except ValueError:
        cfg["page"] = 1
    whole = bool(saved) and RA.whole_business(saved) and not any(ch.values())
    rctx, rscope = (_unscoped(ctx), None) if whole else (ctx, quote_scope)
    if saved:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE report_defs SET last_opened_at=NOW() WHERE id=%s", (saved["id"],))
            conn.commit()
        finally:
            conn.close()

    subj = RB.SUBJECTS[cfg["subject"]]
    groups = None
    if show == "list" and cfg["g1"]:
        got = grouped_list(rctx, cfg, rscope)
        data = {"error": "This report could not be worked out. Try another choice."} if got is None else {}
        if got:
            groups, list_cols, n_groups = got
    elif show == "list":
        data = _results(rctx, dict(cfg, view="detailed"), rscope)
    elif cfg["g1"]:
        data = _results(rctx, dict(cfg, view="summary"), rscope)
    else:
        data = {}
    headline, chart = ("", {"kind": "table"}) if data.get("error") or show == "list" else \
        _chart_and_totals(rctx, cfg, rscope, data)
    tiles = [] if data.get("error") else _tiles(rctx, cfg, rscope)
    total_pages = max(1, (data.get("total", 0) + RB.PER_PAGE - 1) // RB.PER_PAGE) if "total" in data else 1

    rid = saved["id"] if saved else None
    qs = _query(cfg)
    mcol = _member_col(cfg["subject"])
    mine = bool(saved) and (saved["config"] or {}).get("kind") == SIMPLE and RA.can_edit(saved, ctx["viewer"])
    member_val = ch["member"]
    if not member_val and mcol:
        member_val = next((f["val"] for f in cfg0["f"] if f["key"] == mcol and f["op"] == "is"), "")
    period_words = RP.period_words(cfg)
    split_opts = _split_options(cfg0, cfg)
    split_label = next((l for v_, l in split_opts if v_ == cfg["g1"]), "")
    shown_title, from_q = title_for(cfg, cfg0, title, split_label)
    who = "Everyone"
    if member_val == "__none__":
        who = "Unassigned"
    elif member_val:
        who = next((p["display"] for p in ctx["people"] if p["key"] == member_val), "One team member")
    m = RB.measure_of(cfg)
    n_main = 2 if m[2] else 1          # the table shows the count and the chosen number only
    # What you're looking at, in one line, and which groups "Show per" makes (approved mock-up screen 5)
    n_records = tiles[0]["value"] if tiles else "0"
    if groups is not None:
        labels, n_grp = [g["label"] for g in groups], n_groups
    elif data.get("summary"):
        labels = [r["labels"][0] for r in data["summary"]["rows"]]
        n_grp = len(labels)
    else:
        labels, n_grp = [], 0
    split_lower = split_label[:1].lower() + split_label[1:]
    if cfg["g1"]:
        look_line = f"{n_records} {subj['noun']} {period_words}, shown per {split_lower}: " \
                    f"{n_grp} group{'' if n_grp == 1 else 's'}"
        names = ", ".join(str(x) for x in labels[:3]) + ("…" if len(labels) > 3 else "")
        per_hint = (("One group for each: " if show == "list" else "One bar and row for each: ") + names) if labels \
            else "No groups in this time"
    else:
        look_line = f"{n_records} {subj['noun']} {period_words} in total"
        per_hint = "Just the total, no groups"
    return render_template(
        "portal/report_simple.html", customer=customer, RB=RB, cfg=cfg, subj=subj, d=data, chart=chart,
        title=shown_title, from_q=from_q, ready=ready, saved=saved, rid=rid, mine=mine, show=show, tiles=tiles,
        subline=cfg["period_label"] + (" · " + who if mcol else ""), period_words=period_words, n_main=n_main,
        look_line=look_line, per_hint=per_hint, split_label=split_label,
        n_records=n_records, n_groups=n_grp,
        col_words={k: l for k, l, _h in RP.SPLITS.get(cfg["subject"], []) if "@" not in k},
        groups=groups, list_cols=list_cols if groups is not None else [],
        dp_presets=RB.picker_presets(), dp_value=RB.picker_value(cfg),
        member_on=bool(mcol), member_val=member_val, people=ctx["people"],
        split_val=cfg["g1"], split_opts=split_opts,
        ch=ch, chart_url=_view_url(ready, rid, ch, show="chart"), list_url=_view_url(ready, rid, ch, show="list"),
        page=cfg["page"], total_pages=total_pages,
        prev_url=_view_url(ready, rid, ch, show="list", page=cfg["page"] - 1) if cfg["page"] > 1 else None,
        next_url=_view_url(ready, rid, ch, show="list", page=cfg["page"] + 1) if cfg["page"] < total_pages else None,
        drill_base=url_for("reports.drill") + "?" + qs,
        export_qs=_query(cfg, name=title),
        builder_url=(url_for("reports.builder") + "?" + _query(cfg, name=title, ready=ready))
        if PLAN.custom_ok(ctx["tenant_id"]) else "",
        custom_ok=PLAN.custom_ok(ctx["tenant_id"]),
        save_name=saved["name"] if mine else shown_title,
    )


@reports_bp.route("/reports/view/save", methods=["POST"])
@team_feature("reports.custom")
def view_save():
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    f = request.form
    base = _base(ctx, b2b, f)
    if not base:
        flash("That report was not found, or it has not been shared with you.", "warning")
        return redirect(url_for("reports.library"))
    cfg0, title, ready, saved = base
    ch = choices_of(f)
    cfg, show = apply_choices(cfg0, ch)
    name = " ".join((f.get("name") or "").split())[:120] or title
    config = dict(RB.config_of(cfg), kind=SIMPLE)
    if ready:
        config["ready"] = ready
    v = ctx["viewer"]
    update = bool(saved) and f.get("as_new") != "1" and (saved["config"] or {}).get("kind") == SIMPLE \
        and RA.can_edit(saved, v)
    actor = _current_actor(customer)
    conn = _conn()
    try:
        cur = conn.cursor()
        if update:
            cur.execute("UPDATE report_defs SET name=%s, subject=%s, config=%s, updated_at=NOW() "
                        "WHERE id=%s AND tenant_id=%s RETURNING id",
                        (name, cfg["subject"], json.dumps(config), saved["id"], ctx["tenant_id"]))
            rid = cur.fetchone()[0]
        else:
            cur.execute("INSERT INTO report_defs (tenant_id, name, subject, config, created_by_key, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                        (ctx["tenant_id"], name, cfg["subject"], json.dumps(config), actor["key"], actor["label"]))
            rid = cur.fetchone()[0]
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️ report simple save:", e)
        flash("Could not save this report. Please try again.", "danger")
        return redirect(_view_url(ready, saved["id"] if saved else None, ch))
    finally:
        conn.close()
    flash(f"Saved as “{name}”. You’ll find it under Your saved reports.", "success")
    return redirect(url_for("reports.view", id=rid))


def open_url(row, custom_ok: bool) -> str:
    """Where a saved report opens: the simple page for Starter and for reports saved there, else the builder."""
    if not custom_ok or (row.get("config") or {}).get("kind") == SIMPLE:
        return url_for("reports.view", id=row["id"])
    return url_for("reports.builder", id=row["id"])


# ── Ask for a report ─────────────────────────────────────────────────────────

@reports_bp.route("/reports/ask")
@team_feature("reports.custom")
def ask():
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    q = " ".join((request.args.get("q") or "").split())[:300]
    is_b2b, is_b2c = "quotes" in b2b, bool({"orders", "discounts", "carts"} & b2b)
    offered = RS.ready_for(b2b, is_b2b, is_b2c)
    result = None
    if q:
        result = RK.answer(q, offered, ctx["people"], session_key=str(ctx["viewer"]["key"]))
        if result.get("ready"):
            r = RS.READY_BY_KEY[result["ready"]]
            result["question"] = RP.QUESTIONS.get(r["key"], r["label"])
            result["url"] = url_for("reports.view") + "?" + urlencode(dict(result["choices"], ready=r["key"]))
        result["closest"] = [{"q": RP.QUESTIONS.get(k, RS.READY_BY_KEY[k]["label"]),
                              "url": url_for("reports.view", ready=k)} for k in result.get("closest", [])]
    examples = RK.examples(offered)
    return render_template("portal/report_ask.html", customer=customer, q=q, result=result, examples=examples,
                           custom_ok=PLAN.custom_ok(ctx["tenant_id"]))


# ── Reports home: the Sales overview strip's numbers ─────────────────────────

HOME_NUMBERS = [
    ("Quotes won", {"subject": "quotes", "f": "qstatus|is|won", "m": "sum_qvalue", "datef": "qclosed", "period": "this_month"}),
    ("Orders", {"subject": "orders", "m": "sum_tot", "period": "this_month"}),
    ("New enquiries", {"subject": "leads", "period": "this_month"}),
    ("Waiting for our reply", {"subject": "conversations", "f": "vwait|is|true", "period": "30", "datef": "vlast"}),
]


@reports_bp.route("/reports/home-numbers")
@team_feature("reports.custom")
def home_numbers():
    customer, ctx, b2b, quote_scope, err = _setup()
    if err:
        return jsonify([]), 403
    out = []
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for label, c in HOME_NUMBERS:
            if len(out) >= 3 or not RB.subject_ok(c["subject"], b2b):
                continue
            if label == "Orders" and "quotes" in b2b:
                continue
            try:
                cfg = RB.read_config(_args_from_saved({"config": c}), b2b)
                n, val = RB.run_total(cur, ctx, cfg, quote_scope)
                m = RB.measure_of(cfg)
                out.append({"label": label, "value": RB.fmt(val or 0, m[3], ctx) if m[2] else f"{(n or 0):,}",
                            "warn": label == "Waiting for our reply" and (n or 0) > 0})
            except Exception as e:
                conn.rollback()
                print("⚠️ report home number:", e)
    finally:
        conn.close()
    return jsonify(out)


# ── Reports › Custom reports (Growth + Scale, 21 Nov 2026, approved mock-up screen 6) ─────────────

@reports_bp.app_context_processor
def _menu_flags():
    return {"reports_custom_ok": PLAN.custom_ok_for_menu}


def _custom_list(which):
    customer, ctx, b2b, _scope, err = _setup()
    if err:
        return err
    stop = _custom_only(ctx)
    if stop:
        return stop
    v = ctx["viewer"]
    rows = []
    for r in _visible_reports(ctx, b2b):
        if (r.get("config") or {}).get("kind") == SIMPLE or r.get("folder") == "Sales overview":
            continue                      # saved from a ready-made question, or a Sales overview box
        if RA.is_mine(r, v) != (which == "mine"):
            continue
        try:
            what = RP.plain_sentence(RB.read_config(_args_from_saved(r), b2b))
        except Exception:
            what = RB.SUBJECTS[r["subject"]]["label"]
        rows.append({"name": r["name"], "what": what, "url": url_for("reports.builder", id=r["id"]),
                     "delete_url": url_for("reports.delete", report_id=r["id"]) if RA.can_manage(r, v) else "",
                     "who": RA.sharing_label(r) if which == "mine" else "Made by " + (r.get("created_by") or "a team member"),
                     "when": (r.get("last_opened_at") or r.get("updated_at"))})
    return render_template("portal/report_custom_list.html", customer=customer, which=which, rows=rows)


@reports_bp.route("/reports/custom-reports")
@team_feature("reports.custom")
def custom_mine():
    return _custom_list("mine")


@reports_bp.route("/reports/custom-reports/shared")
@team_feature("reports.custom")
def custom_shared():
    return _custom_list("shared")
