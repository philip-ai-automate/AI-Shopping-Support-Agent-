"""KeyLand estates: plot sizes, payment plans and what a buyer pays.

An estate is a Land listing with sub-type "Estate Land". It gets extra rows:
  re_estate_plot_sizes     one per plot size (label, sqm, outright price, plots left)
  re_estate_payment_plans  one per plan (deposit %, months, optional premium %)
  re_estate_allocations    the size + plan chosen for a lead, with the amounts
                           worked out at the time (kept even if the plan changes)

Amounts: total = price × (1 + premium%), deposit = total × deposit%,
monthly = (total − deposit) ÷ months. Outright = deposit 100%, 0 months.
"""
import calendar
from datetime import date

import psycopg2.extras

from db import get_db_connection

ESTATE_SUB_TYPE = "Estate Land"
MAX_ROWS = 30

_tables_ready = False


def is_estate(listing) -> bool:
    return bool(listing) and listing.get("property_type") == "land" and listing.get("sub_type") == ESTATE_SUB_TYPE


def ensure_tables():
    """Create the three tables once. New tables only, so no lock on busy ones."""
    global _tables_ready
    if _tables_ready:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""SELECT to_regclass('re_estate_plot_sizes') IS NOT NULL,
                              to_regclass('re_estate_payment_plans') IS NOT NULL,
                              to_regclass('re_estate_allocations') IS NOT NULL""")
        if not all(cur.fetchone()):
            cur.execute("SET lock_timeout = '3s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_estate_plot_sizes (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                listing_id INTEGER NOT NULL,
                label VARCHAR(60) NOT NULL,
                size_sqm NUMERIC(12,2),
                price NUMERIC(16,2) NOT NULL,
                plots_left INTEGER,
                position SMALLINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS re_estate_plot_sizes_listing ON re_estate_plot_sizes (listing_id)")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_estate_payment_plans (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                listing_id INTEGER NOT NULL,
                name VARCHAR(60) NOT NULL,
                deposit_pct NUMERIC(5,2) NOT NULL,
                months SMALLINT NOT NULL DEFAULT 0,
                premium_pct NUMERIC(5,2) NOT NULL DEFAULT 0,
                position SMALLINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS re_estate_payment_plans_listing ON re_estate_payment_plans (listing_id)")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_estate_allocations (
                contact_id INTEGER PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                listing_id INTEGER NOT NULL,
                plot_size_id INTEGER,
                plan_id INTEGER,
                size_label VARCHAR(60),
                plan_name VARCHAR(60),
                price NUMERIC(16,2),
                premium_pct NUMERIC(5,2),
                deposit_pct NUMERIC(5,2),
                months SMALLINT,
                total NUMERIC(16,2),
                deposit NUMERIC(16,2),
                monthly NUMERIC(16,2),
                start_date DATE,
                plot_counted BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.close()
        _tables_ready = True
    finally:
        conn.close()


# ── Maths ──────────────────────────────────────────────────────────────────────

def work_out(price, deposit_pct, months, premium_pct=0) -> dict:
    """{'total', 'deposit', 'monthly', 'months'} in whole naira."""
    price = float(price or 0)
    months = int(months or 0)
    total = round(price * (1 + float(premium_pct or 0) / 100))
    if months <= 0:
        return {"total": total, "deposit": total, "monthly": 0, "months": 0}
    deposit = round(total * float(deposit_pct or 0) / 100)
    monthly = round((total - deposit) / months)
    return {"total": total, "deposit": deposit, "monthly": monthly, "months": months}


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def schedule(price, deposit_pct, months, premium_pct, start: date) -> list:
    """[(name, amount, due_date)]: the deposit on the start date, then one
    payment a month. The last payment takes any rounding so the sum is exact."""
    w = work_out(price, deposit_pct, months, premium_pct)
    if w["months"] == 0:
        return [("Full payment", w["total"], start)]
    rows = [(f"Deposit ({_pct(deposit_pct)}%)", w["deposit"], start)]
    rest = w["total"] - w["deposit"]
    for i in range(1, w["months"] + 1):
        amt = w["monthly"] if i < w["months"] else rest - w["monthly"] * (w["months"] - 1)
        rows.append((f"Instalment {i} of {w['months']}", amt, add_months(start, i)))
    return rows


def _pct(v) -> str:
    v = float(v or 0)
    return str(int(v)) if v == int(v) else f"{v:g}"


# ── Form parsing (listing form) ────────────────────────────────────────────────

def _num(s):
    s = (s or "").replace(",", "").replace("₦", "").strip()
    if not s:
        return None
    return float(s)


def parse_form(form):
    """(sizes, plans, error). Rows with nothing typed are ignored."""
    sizes, plans = [], []
    try:
        ids, labels, sqms, prices, lefts = (form.getlist(k) for k in ("ps_id", "ps_label", "ps_sqm", "ps_price", "ps_left"))
        for i in range(len(labels)):
            label = (labels[i] or "").strip()[:60]
            sqm = _num(sqms[i] if i < len(sqms) else "")
            price = _num(prices[i] if i < len(prices) else "")
            left = _num(lefts[i] if i < len(lefts) else "")
            if not label and sqm and sqm > 0:
                label = f"{sqm:g} sqm"
            if not label and price is None and left is None:
                continue
            if not label:
                return [], [], "Each plot size needs a name or a size in sqm."
            if price is None or price <= 0:
                return [], [], f"Plot size \"{label}\" needs an outright price."
            if left is not None and left < 0:
                return [], [], f"Plots left for \"{label}\" can't be below 0."
            sizes.append({"id": _int_or_none(ids[i] if i < len(ids) else ""), "label": label,
                          "size_sqm": sqm if sqm and sqm > 0 else None, "price": price,
                          "plots_left": int(left) if left is not None else None})
        ids, names, deps, mons, prems = (form.getlist(k) for k in ("pp_id", "pp_name", "pp_deposit", "pp_months", "pp_premium"))
        for i in range(len(names)):
            name = (names[i] or "").strip()[:60]
            dep = _num(deps[i] if i < len(deps) else "")
            mon = _num(mons[i] if i < len(mons) else "")
            prem = _num(prems[i] if i < len(prems) else "")
            if not name and dep is None and mon is None and prem is None:
                continue
            mon = int(mon or 0)
            if mon < 0 or mon > 120:
                return [], [], "A payment plan can run for 0 to 120 months."
            if mon == 0:
                dep = 100.0
            if dep is None or dep <= 0 or dep > 100:
                return [], [], f"Plan \"{name or mon}\" needs a deposit between 1% and 100%."
            if dep == 100 and mon > 0:
                return [], [], f"Plan \"{name or mon}\": a 100% deposit leaves nothing to pay monthly. Use 0 months for outright."
            prem = prem or 0
            if prem < 0 or prem > 100:
                return [], [], "The instalment premium must be between 0% and 100%."
            if not name:
                name = "Outright" if mon == 0 else f"{mon} months"
            plans.append({"id": _int_or_none(ids[i] if i < len(ids) else ""), "name": name,
                          "deposit_pct": dep, "months": mon, "premium_pct": prem})
    except ValueError:
        return [], [], "Plot sizes and payment plans need numbers in the price, plots, deposit, months and premium boxes."
    if len(sizes) > MAX_ROWS or len(plans) > MAX_ROWS:
        return [], [], f"Up to {MAX_ROWS} plot sizes and {MAX_ROWS} plans per estate."
    return sizes, plans, None


def _int_or_none(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def save(cur, tenant_id: int, listing_id: int, sizes: list, plans: list):
    """Replace the estate's sizes and plans, keeping ids of rows that stay
    (a lead's allocation keeps its own copy of the amounts either way)."""
    ensure_tables()
    for table, rows, cols in (("re_estate_plot_sizes", sizes, ("label", "size_sqm", "price", "plots_left")),
                              ("re_estate_payment_plans", plans, ("name", "deposit_pct", "months", "premium_pct"))):
        cur.execute(f"SELECT id FROM {table} WHERE listing_id=%s AND tenant_id=%s", (listing_id, tenant_id))
        have = {r[0] for r in cur.fetchall()}
        keep = set()
        for pos, r in enumerate(rows):
            vals = [r[c] for c in cols]
            if r["id"] in have:
                keep.add(r["id"])
                cur.execute(f"UPDATE {table} SET {', '.join(c + '=%s' for c in cols)}, position=%s WHERE id=%s",
                            (*vals, pos, r["id"]))
            else:
                cur.execute(f"INSERT INTO {table} (tenant_id, listing_id, {', '.join(cols)}, position) "
                            f"VALUES (%s,%s,{','.join(['%s'] * len(cols))},%s)", (tenant_id, listing_id, *vals, pos))
        gone = have - keep
        if gone:
            cur.execute(f"DELETE FROM {table} WHERE id = ANY(%s) AND tenant_id=%s", (list(gone), tenant_id))


# ── Reading ────────────────────────────────────────────────────────────────────

def load(tenant_id: int, listing_ids) -> dict:
    """{listing_id: {'sizes': [...], 'plans': [...]}} with plain numbers."""
    ensure_tables()
    out = {int(i): {"sizes": [], "plans": []} for i in listing_ids or []}
    if not out:
        return out
    conn = get_db_connection()
    if not conn:
        return out
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT id, listing_id, label, size_sqm, price, plots_left FROM re_estate_plot_sizes
                       WHERE tenant_id=%s AND listing_id = ANY(%s) ORDER BY position, id""", (tenant_id, list(out)))
        for r in cur.fetchall():
            out[r["listing_id"]]["sizes"].append({"id": r["id"], "label": r["label"],
                "size_sqm": float(r["size_sqm"]) if r["size_sqm"] is not None else None,
                "price": float(r["price"]), "plots_left": r["plots_left"]})
        cur.execute("""SELECT id, listing_id, name, deposit_pct, months, premium_pct FROM re_estate_payment_plans
                       WHERE tenant_id=%s AND listing_id = ANY(%s) ORDER BY position, id""", (tenant_id, list(out)))
        for r in cur.fetchall():
            out[r["listing_id"]]["plans"].append({"id": r["id"], "name": r["name"],
                "deposit_pct": float(r["deposit_pct"]), "months": int(r["months"]),
                "premium_pct": float(r["premium_pct"])})
        cur.close()
    finally:
        conn.close()
    return out


def price_grid(estate: dict) -> list:
    """For the listing page: [{'plan', 'cells': [work_out per size]}]."""
    return [{"plan": p, "cells": [work_out(s["price"], p["deposit_pct"], p["months"], p["premium_pct"])
                                  for s in estate["sizes"]]} for p in estate["plans"]]


def allocation(tenant_id: int, contact_id: int):
    ensure_tables()
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM re_estate_allocations WHERE contact_id=%s AND tenant_id=%s", (contact_id, tenant_id))
        row = cur.fetchone()
        cur.close()
        return row
    finally:
        conn.close()


# ── Allocating a plot to a lead ────────────────────────────────────────────────

def allocate(cur, tenant_id: int, contact_id: int, listing_id: int, size_id: int, plan_id: int,
             start: date, staff_id=None, make_milestones=True):
    """Save the size + plan on the lead and (optionally) its payment milestones.
    Returns (number of milestones made, error)."""
    ensure_tables()
    cur.execute("SELECT id, label, price FROM re_estate_plot_sizes WHERE id=%s AND listing_id=%s AND tenant_id=%s",
                (size_id, listing_id, tenant_id))
    size = cur.fetchone()
    cur.execute("SELECT id, name, deposit_pct, months, premium_pct FROM re_estate_payment_plans "
                "WHERE id=%s AND listing_id=%s AND tenant_id=%s", (plan_id, listing_id, tenant_id))
    plan = cur.fetchone()
    if not size or not plan:
        return 0, "That plot size or payment plan isn't on this estate any more."
    _, label, price = size
    _, pname, dep, months, prem = plan
    w = work_out(price, dep, months, prem)
    cur.execute("SELECT listing_id, plot_size_id, plot_counted FROM re_estate_allocations WHERE contact_id=%s AND tenant_id=%s",
                (contact_id, tenant_id))
    old = cur.fetchone()
    if old and old[2]:
        _give_back(cur, tenant_id, old[1])
    cur.execute("DELETE FROM re_estate_allocations WHERE contact_id=%s AND tenant_id=%s", (contact_id, tenant_id))
    cur.execute("""INSERT INTO re_estate_allocations
        (contact_id, tenant_id, listing_id, plot_size_id, plan_id, size_label, plan_name, price, premium_pct,
         deposit_pct, months, total, deposit, monthly, start_date)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (contact_id, tenant_id, listing_id, size_id, plan_id, label, pname, price, prem, dep, months,
         w["total"], w["deposit"], w["monthly"], start))
    cur.execute("SELECT pipeline_stage FROM re_customers WHERE id=%s AND tenant_id=%s", (contact_id, tenant_id))
    st = cur.fetchone()
    if st and st[0] == "allocated":  # already Allocated: this size takes the plot
        on_allocated(cur, tenant_id, contact_id)
    made = 0
    if make_milestones:
        cur.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM re_payment_milestones WHERE contact_id=%s AND tenant_id=%s",
                    (contact_id, tenant_id))
        pos = cur.fetchone()[0]
        note = f"{pname} plan · {label}"
        for name, amount, due in schedule(price, dep, months, prem, start):
            cur.execute("""INSERT INTO re_payment_milestones
                (tenant_id, contact_id, name, amount, due_date, notes, position, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (tenant_id, contact_id, name, amount, due, note, pos, staff_id))
            pos += 1
            made += 1
    return made, None


def _give_back(cur, tenant_id, size_id):
    if size_id:
        cur.execute("UPDATE re_estate_plot_sizes SET plots_left = plots_left + 1 "
                    "WHERE id=%s AND tenant_id=%s AND plots_left IS NOT NULL", (size_id, tenant_id))


def on_allocated(cur, tenant_id: int, contact_id: int):
    """Lead marked Allocated: one plot of that size is taken (once)."""
    ensure_tables()
    cur.execute("""UPDATE re_estate_allocations SET plot_counted=TRUE
                   WHERE contact_id=%s AND tenant_id=%s AND plot_counted=FALSE RETURNING plot_size_id""",
                (contact_id, tenant_id))
    row = cur.fetchone()
    if row and row[0]:
        cur.execute("UPDATE re_estate_plot_sizes SET plots_left = GREATEST(plots_left - 1, 0) "
                    "WHERE id=%s AND tenant_id=%s AND plots_left IS NOT NULL", (row[0], tenant_id))


def on_unallocated(cur, tenant_id: int, contact_id: int):
    """Lead moved out of Allocated: the plot goes back on sale."""
    ensure_tables()
    cur.execute("""UPDATE re_estate_allocations SET plot_counted=FALSE
                   WHERE contact_id=%s AND tenant_id=%s AND plot_counted=TRUE RETURNING plot_size_id""",
                (contact_id, tenant_id))
    row = cur.fetchone()
    if row:
        _give_back(cur, tenant_id, row[0])


def unlink(cur, tenant_id: int, contact_id: int):
    ensure_tables()
    on_unallocated(cur, tenant_id, contact_id)
    cur.execute("DELETE FROM re_estate_allocations WHERE contact_id=%s AND tenant_id=%s", (contact_id, tenant_id))


def rows_from_form(form) -> dict:
    """What was typed, to show again when the form comes back with an error."""
    cols = lambda keys: [dict(zip(keys, vals)) for vals in zip(*(form.getlist(k) for k in keys))]
    sizes = [{"id": r["ps_id"], "label": r["ps_label"], "size_sqm": r["ps_sqm"], "price": r["ps_price"],
              "plots_left": r["ps_left"]} for r in cols(("ps_id", "ps_label", "ps_sqm", "ps_price", "ps_left"))]
    plans = [{"id": r["pp_id"], "name": r["pp_name"], "deposit_pct": r["pp_deposit"], "months": r["pp_months"],
              "premium_pct": r["pp_premium"]} for r in cols(("pp_id", "pp_name", "pp_deposit", "pp_months", "pp_premium"))]
    return {"sizes": sizes, "plans": plans}


def stage_changed(cur, tenant_id: int, contact_ids, stage: str):
    """Keep "plots left" right whenever leads change stage."""
    for cid in contact_ids:
        if stage == "allocated":
            on_allocated(cur, tenant_id, cid)
        else:
            on_unallocated(cur, tenant_id, cid)


# ── Land buyer details + readiness (filled by the AI, editable by staff) ────────
# Tables re_buyer_land / re_readiness_log are created by the AI service
# (ai-backend/estate/land.py); the portal only reads and edits them.

READINESS = [("ready", "Ready"), ("considering", "Considering"), ("early", "Early")]
LAND_PURPOSES = [("build", "To build"), ("invest", "Investment"), ("farm", "Farming"), ("commercial", "Commercial")]
LAND_TIMEFRAMES = [("now", "Now / this month"), ("1-3 months", "In 1–3 months"), ("3-6 months", "In 3–6 months"), ("later", "Later")]


def _land_tables(cur) -> bool:
    cur.execute("SELECT to_regclass('re_buyer_land') IS NOT NULL")
    return bool(cur.fetchone()[0])


def land_details(tenant_id: int, contact_id: int) -> dict:
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT to_regclass('re_buyer_land') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            return {}
        cur.execute("""SELECT bl.*, l.title AS estate_title FROM re_buyer_land bl
                       LEFT JOIN re_property_listings l ON l.id = bl.estate_listing_id AND l.tenant_id = bl.tenant_id
                       WHERE bl.customer_id=%s AND bl.tenant_id=%s""", (contact_id, tenant_id))
        row = cur.fetchone()
        land = dict(row) if row else {}
        cur.execute("""SELECT readiness, reason, set_by, created_at FROM re_readiness_log
                       WHERE customer_id=%s AND tenant_id=%s ORDER BY id DESC LIMIT 6""", (contact_id, tenant_id))
        land["log"] = cur.fetchall()
        cur.close()
        return land
    finally:
        conn.close()


def save_land_staff(tenant_id: int, contact_id: int, form, staff_name: str) -> str:
    """Staff edit of the land details. Returns the readiness choice made."""
    def num(k):
        try:
            v = float((form.get(k) or "").replace(",", "").replace("₦", "").strip())
            return v if v > 0 else None
        except ValueError:
            return None
    kind = form.get("buyer_kind") if form.get("buyer_kind") in ("land", "property") else None
    vals = {
        "buyer_kind": kind,
        "purpose": form.get("purpose") if form.get("purpose") in dict(LAND_PURPOSES) else None,
        "plot_size_sqm": num("plot_size_sqm"),
        "start_amount": num("start_amount"),
        "total_budget": num("total_budget"),
        "pay_mode": form.get("pay_mode") if form.get("pay_mode") in ("outright", "instalment") else None,
        "monthly_amount": num("monthly_amount"),
        "timeframe": form.get("timeframe") if form.get("timeframe") in dict(LAND_TIMEFRAMES) else None,
        "estate_listing_id": _int_or_none(form.get("estate_listing_id")),
    }
    choice = form.get("readiness") or "keep"
    conn = get_db_connection()
    if not conn:
        return choice
    try:
        cur = conn.cursor()
        if not _land_tables(cur):
            return choice
        cols = list(vals)
        cur.execute(f"""INSERT INTO re_buyer_land (customer_id, tenant_id, {', '.join(cols)})
                        VALUES (%s,%s,{','.join(['%s'] * len(cols))})
                        ON CONFLICT (customer_id) DO UPDATE SET {', '.join(f'{c}=EXCLUDED.{c}' for c in cols)}, updated_at=NOW()""",
                    [contact_id, tenant_id] + [vals[c] for c in cols])
        if choice in dict(READINESS):
            cur.execute("SELECT readiness FROM re_buyer_land WHERE customer_id=%s", (contact_id,))
            old = cur.fetchone()[0]
            reason = f"Set by {staff_name}."
            cur.execute("""UPDATE re_buyer_land SET readiness=%s, readiness_reason=%s, readiness_by='staff', readiness_at=NOW()
                           WHERE customer_id=%s AND tenant_id=%s""", (choice, reason, contact_id, tenant_id))
            if old != choice:
                cur.execute("INSERT INTO re_readiness_log (tenant_id, customer_id, readiness, reason, set_by) VALUES (%s,%s,%s,%s,%s)",
                            (tenant_id, contact_id, choice, reason, staff_name[:40]))
        elif choice == "auto":
            cur.execute("UPDATE re_buyer_land SET readiness_by='ai' WHERE customer_id=%s AND tenant_id=%s", (contact_id, tenant_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return choice


def refresh_readiness(ai_url: str, tenant_id: int, contact_id: int):
    """Ask the AI service to re-run the readiness rules (short wait)."""
    try:
        import requests
        requests.post(f"{ai_url}/estate-readiness-refresh",
                      json={"tenant_id": tenant_id, "customer_id": contact_id}, timeout=8)
    except Exception as e:
        print(f"⚠️ [ESTATE] readiness refresh error: {e}")
