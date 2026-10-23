"""
estate/land.py — land and estate buyers (KeyLand Phase 2).

What the AI learns about a land buyer is kept in re_buyer_land (one row per
lead); every readiness change is logged in re_readiness_log. Estates are
Land › "Estate Land" listings with plot sizes and payment plans
(re_estate_plot_sizes / re_estate_payment_plans, set up in the portal).

The system, not the AI, picks the estates that fit and works out every
amount, so the AI only ever copies figures it is given. Readiness comes from
fixed rules (compute_readiness), never from the AI's own opinion.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import psycopg2.extras
from db import get_db_connection

ESTATE_SUB_TYPE = "Estate Land"
LEVELS = {"early": 1, "considering": 2, "ready": 3}
LEVEL_NAMES = {"early": "Early", "considering": "Considering", "ready": "Ready"}
SCARCE_AT = 5          # "only a few plots left" when this many or fewer (user decision 22 Oct)
FIELDS = ("buyer_kind", "purpose", "plot_size_sqm", "start_amount", "total_budget", "pay_mode",
          "monthly_amount", "timeframe", "estate_listing_id", "wants_inspection", "asks_how_to_pay",
          "wants_agent")
TIMEFRAMES = ("now", "1-3 months", "3-6 months", "later")
PURPOSES = ("build", "invest", "farm", "commercial")

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('re_buyer_land') IS NOT NULL, to_regclass('re_readiness_log') IS NOT NULL")
        if not all(cur.fetchone()):
            cur.execute("SET LOCAL lock_timeout = '3s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_buyer_land (
                customer_id INTEGER PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                buyer_kind VARCHAR(10),
                purpose VARCHAR(20),
                plot_size_sqm NUMERIC(12,2),
                start_amount NUMERIC(16,2),
                total_budget NUMERIC(16,2),
                pay_mode VARCHAR(12),
                monthly_amount NUMERIC(16,2),
                timeframe VARCHAR(12),
                estate_listing_id INTEGER,
                wants_inspection BOOLEAN NOT NULL DEFAULT FALSE,
                asks_how_to_pay BOOLEAN NOT NULL DEFAULT FALSE,
                wants_agent BOOLEAN NOT NULL DEFAULT FALSE,
                readiness VARCHAR(12),
                readiness_reason TEXT,
                readiness_by VARCHAR(10),
                readiness_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS re_buyer_land_tenant ON re_buyer_land (tenant_id, readiness)")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_readiness_log (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                readiness VARCHAR(12) NOT NULL,
                reason TEXT,
                set_by VARCHAR(40),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS re_readiness_log_customer ON re_readiness_log (customer_id)")
        conn.commit()
        cur.close()
        _tables_ready = True
    finally:
        conn.close()


# ── Buyer land details ─────────────────────────────────────────────────────────

def get_land(tenant_id: int, customer_id: int) -> dict:
    ensure_tables()
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM re_buyer_land WHERE customer_id=%s AND tenant_id=%s", (customer_id, tenant_id))
        row = cur.fetchone()
        cur.close()
        out = {}
        for k, v in (dict(row) if row else {}).items():
            out[k] = float(v) if k in ("plot_size_sqm", "start_amount", "total_budget", "monthly_amount") and v is not None else v
        return out
    finally:
        conn.close()


def save_land(tenant_id: int, customer_id: int, fields: dict):
    """Upsert the given land fields (None/empty values are skipped)."""
    safe = {k: v for k, v in (fields or {}).items() if k in FIELDS and v is not None and v != ""}
    if not safe:
        return
    ensure_tables()
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cols = list(safe)
        cur.execute(
            f"""INSERT INTO re_buyer_land (customer_id, tenant_id, {', '.join(cols)})
                VALUES (%s, %s, {', '.join(['%s'] * len(cols))})
                ON CONFLICT (customer_id) DO UPDATE SET
                  {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)}, updated_at = NOW()""",
            [customer_id, tenant_id] + [safe[c] for c in cols])
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"⚠️ [ESTATE LAND] save_land error: {e}")
    finally:
        conn.close()


def clean_extracted(x: dict) -> dict:
    """The extractor's land keys → re_buyer_land fields (only what is valid)."""
    out = {}
    kind = (x.get("buyer_kind") or "").lower()
    if kind in ("land", "property"):
        out["buyer_kind"] = kind
    p = (x.get("purpose") or "").lower()
    if p in PURPOSES:
        out["purpose"] = p
    for k in ("plot_size_sqm", "start_amount", "monthly_amount", "total_budget"):
        try:
            v = float(x.get(k)) if x.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            v = None
        if v and v > 0:
            out[k] = v
    pm = (x.get("pay_mode") or "").lower()
    if pm in ("outright", "instalment"):
        out["pay_mode"] = pm
    tf = (x.get("timeframe") or "").lower()
    if tf in TIMEFRAMES:
        out["timeframe"] = tf
    for k in ("wants_inspection", "asks_how_to_pay", "wants_agent"):
        if x.get(k) is True:
            out[k] = True          # sticky: once asked, it stays asked
    return out


# ── Estates and amounts ────────────────────────────────────────────────────────

def work_out(price, deposit_pct, months, premium_pct=0) -> dict:
    """Same sums as the portal (estate_plans.work_out), in whole naira."""
    price = float(price or 0)
    months = int(months or 0)
    total = round(price * (1 + float(premium_pct or 0) / 100))
    if months <= 0:
        return {"total": total, "deposit": total, "monthly": 0, "months": 0}
    deposit = round(total * float(deposit_pct or 0) / 100)
    return {"total": total, "deposit": deposit, "monthly": round((total - deposit) / months), "months": months}


def load_estates(tenant_id: int) -> list:
    """Available estates with their plot sizes and plans, as listing rows
    (same columns the search returns, so cards can be built from them)."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, title, property_type, sub_type, transaction_type, location, lga, state, landmark,
                   price, price_negotiable, bedrooms, bathrooms, toilets, size_sqm, title_document,
                   land_use, features, amenities, status, images, description
            FROM re_property_listings
            WHERE tenant_id=%s AND status='available' AND property_type='land' AND sub_type=%s
            ORDER BY id""", (tenant_id, ESTATE_SUB_TYPE))
        estates = [dict(r) for r in cur.fetchall()]
        if not estates:
            cur.close()
            return []
        ids = [e["id"] for e in estates]
        try:
            cur.execute("""SELECT listing_id, id, label, size_sqm, price, plots_left FROM re_estate_plot_sizes
                           WHERE tenant_id=%s AND listing_id = ANY(%s) ORDER BY position, id""", (tenant_id, ids))
            sizes = cur.fetchall()
            cur.execute("""SELECT listing_id, id, name, deposit_pct, months, premium_pct FROM re_estate_payment_plans
                           WHERE tenant_id=%s AND listing_id = ANY(%s) ORDER BY position, id""", (tenant_id, ids))
            plans = cur.fetchall()
        except Exception:                      # portal hasn't created the tables yet
            conn.rollback()
            sizes, plans = [], []
        cur.close()
        by = {e["id"]: e for e in estates}
        for e in estates:
            e["sizes"], e["plans"] = [], []
        for s in sizes:
            by[s["listing_id"]]["sizes"].append({"id": s["id"], "label": s["label"],
                "size_sqm": float(s["size_sqm"]) if s["size_sqm"] is not None else None,
                "price": float(s["price"]), "plots_left": s["plots_left"]})
        for p in plans:
            by[p["listing_id"]]["plans"].append({"id": p["id"], "name": p["name"], "deposit_pct": float(p["deposit_pct"]),
                "months": int(p["months"]), "premium_pct": float(p["premium_pct"])})
        return [e for e in estates if e["sizes"] and e["plans"]]
    except Exception as e:
        print(f"⚠️ [ESTATE LAND] load_estates error: {e}")
        return []
    finally:
        conn.close()


def _in_area(estate: dict, area: str) -> bool:
    if not area:
        return True
    hay = " ".join(str(estate.get(k) or "") for k in ("title", "location", "lga", "state", "landmark")).lower()
    return any(a.strip().lower() in hay for a in area.split(",") if a.strip())


def estate_by_name(estates: list, name: str):
    n = (name or "").strip().lower()
    if len(n) < 3:
        return None
    for e in estates:
        t = (e.get("title") or "").lower()
        if n in t or t in n:
            return e
    return None


def options(estates: list, land: dict, area: str = None, budget_max: float = None) -> dict:
    """{'fits': [...], 'closest': [...], 'in_area': [...estates]}.
    Each option: estate, size, plan, amounts, why. Best fit first."""
    in_area = [e for e in estates if _in_area(e, area)]
    if land.get("estate_listing_id"):
        chosen = [e for e in in_area if e["id"] == land["estate_listing_id"]]
        in_area = chosen + [e for e in in_area if e["id"] != land["estate_listing_id"]]
    start = land.get("start_amount")
    total_budget = land.get("total_budget") or budget_max
    monthly_cap = land.get("monthly_amount")
    want_sqm = land.get("plot_size_sqm")
    mode = land.get("pay_mode")
    rows = []
    for e in in_area:
        sizes = [s for s in e["sizes"] if s["plots_left"] != 0]
        if want_sqm:
            with_sqm = [s for s in sizes if s["size_sqm"]]
            if with_sqm:
                best = min(abs(s["size_sqm"] - want_sqm) for s in with_sqm)
                sizes = [s for s in with_sqm if abs(s["size_sqm"] - want_sqm) == best]
        for s in sizes:
            for p in e["plans"]:
                if mode == "outright" and p["months"]:
                    continue
                if mode == "instalment" and not p["months"]:
                    continue
                w = work_out(s["price"], p["deposit_pct"], p["months"], p["premium_pct"])
                need_now = w["deposit"]
                fits = True
                gap = 0
                if p["months"] == 0:
                    cap = total_budget or start
                    if cap and w["total"] > cap * 1.0:
                        fits, gap = False, w["total"] - cap
                else:
                    cap = start or total_budget
                    if cap and need_now > cap:
                        fits, gap = False, need_now - cap
                    if fits and monthly_cap and w["monthly"] > monthly_cap * 1.05:
                        fits, gap = False, w["monthly"] - monthly_cap
                    if fits and total_budget and not start and w["total"] > total_budget * 1.10:
                        fits, gap = False, w["total"] - total_budget
                rows.append({"estate": e, "size": s, "plan": p, "w": w, "fits": fits, "gap": gap,
                             "known_money": bool(start or total_budget or monthly_cap)})
    fits = [r for r in rows if r["fits"]]
    # Chosen estate first, then bigger plots; per plot the plan with the lowest monthly payment
    fits.sort(key=lambda r: (r["estate"]["id"] != land.get("estate_listing_id"), -(r["size"]["price"]), -r["plan"]["months"]))
    closest = sorted([r for r in rows if not r["fits"]], key=lambda r: r["gap"])
    return {"fits": _one_per_size(fits)[:3], "closest": closest[:2], "in_area": in_area}


def _one_per_size(rows):
    """Keep the best plan per estate + plot size, so 3 options aren't 3 plans of one plot."""
    seen, out = set(), []
    for r in rows:
        k = (r["estate"]["id"], r["size"]["id"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def naira(v) -> str:
    return "₦{:,.0f}".format(float(v or 0))


def _amount_line(r) -> str:
    w = r["w"]
    if not w["months"]:
        return f"{naira(w['total'])} outright"
    return (f"{r['plan']['name']} plan: {naira(w['deposit'])} deposit, then {naira(w['monthly'])} a month "
            f"for {w['months']} months (total {naira(w['total'])})")


def estate_facts(e: dict) -> str:
    """Every size × plan for one estate, for the AI to quote from."""
    head = [f"ESTATE #{e['id']}: {e['title']}",
            "Location: " + ", ".join(x for x in (e.get("location"), e.get("lga"), e.get("state")) if x)]
    if e.get("landmark"):
        head.append(f"Landmark: {e['landmark']}")
    if e.get("title_document"):
        head.append("Title: " + e["title_document"].replace("_", " "))
    lines = []
    for s in e["sizes"]:
        if s["plots_left"] == 0:
            lines.append(f"  - {s['label']}: SOLD OUT, never offer it")
            continue
        scarce = ""
        if s["plots_left"] is not None and s["plots_left"] <= SCARCE_AT:
            scarce = " (only a few left: you may say 'only a few plots left', never a number)"
        parts = []
        for p in e["plans"]:
            w = work_out(s["price"], p["deposit_pct"], p["months"], p["premium_pct"])
            parts.append(f"{p['name']}: {naira(w['total'])}" if not w["months"] else
                         f"{p['name']}: {naira(w['deposit'])} deposit then {naira(w['monthly'])}/month x{w['months']} (total {naira(w['total'])})")
        lines.append(f"  - {s['label']}{scarce}: " + "; ".join(parts))
    return "\n".join(head + ["  Plot sizes and payment plans:"] + lines)


def missing_questions(land: dict, area: str) -> list:
    q = []
    if not area:
        q.append("which area or location they want")
    if not land.get("purpose"):
        q.append("whether it is to build a home or as an investment")
    if not land.get("plot_size_sqm"):
        q.append("the plot size they have in mind (for example 300, 450 or 600 sqm)")
    if not (land.get("start_amount") or land.get("total_budget")):
        q.append("how much they would like to start with, or their total budget")
    if not land.get("pay_mode"):
        q.append("whether they will pay outright or spread it over months")
    elif land.get("pay_mode") == "instalment" and not land.get("monthly_amount"):
        q.append("about how much they could pay each month")
    if not land.get("timeframe"):
        q.append("when they hope to buy (this month, in the next few months, or later)")
    return q


# ── Readiness ──────────────────────────────────────────────────────────────────

def compute_readiness(land: dict, area: str, opts: dict) -> tuple:
    """(level, reason) from fixed rules. Only for land buyers."""
    chosen = land.get("estate_listing_id")
    picked = bool(chosen) and bool(land.get("plot_size_sqm") or land.get("pay_mode"))
    fits_chosen = any(r["estate"]["id"] == chosen for r in opts.get("fits", [])) if chosen else False
    has_money = bool(land.get("start_amount") or land.get("total_budget") or land.get("monthly_amount"))
    signal = None
    if land.get("wants_inspection"):
        signal = "asked for a site inspection"
    elif land.get("asks_how_to_pay"):
        signal = "asked how to pay"
    elif land.get("timeframe") == "now":
        signal = "wants to buy now"
    elif land.get("wants_agent"):
        signal = "asked to speak to someone"
    if picked and fits_chosen and signal:
        return "ready", f"Picked an estate and plot, the amount fits what they have, and {signal}."
    if (area and has_money) or land.get("timeframe") in ("1-3 months", "3-6 months") or (chosen and signal):
        bits = []
        if area and has_money:
            bits.append("gave an area and how much they have")
        if land.get("timeframe") in ("1-3 months", "3-6 months"):
            bits.append(f"plans to buy in {land['timeframe']}")
        if chosen and signal and not fits_chosen:
            bits.append(f"{signal}, but the amount doesn't fit what they said yet")
        elif picked and not signal:
            bits.append("picked an estate but hasn't asked to inspect or pay yet")
        reason = "; ".join(bits) or "comparing options"
        return "considering", reason[0].upper() + reason[1:] + "."
    missing = []
    if not area:
        missing.append("area")
    if not has_money:
        missing.append("budget")
    reason = "Just asking so far" + (f"; no {' or '.join(missing)} given yet." if missing else ".")
    if land.get("timeframe") == "later":
        reason = "Plans to buy later."
    return "early", reason


def set_readiness(tenant_id: int, customer_id: int, level: str, reason: str, by: str = "ai") -> bool:
    """Save a level. The AI can raise a level staff set, never lower it.
    Returns True when it changed."""
    ensure_tables()
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT readiness, readiness_by, readiness_reason FROM re_buyer_land WHERE customer_id=%s AND tenant_id=%s FOR UPDATE",
                    (customer_id, tenant_id))
        row = cur.fetchone()
        old, old_by, old_reason = (row or (None, None, None))
        if by == "ai" and old_by == "staff" and LEVELS.get(level, 0) <= LEVELS.get(old, 0):
            conn.rollback()
            return False
        if old == level and old_reason == reason:
            conn.rollback()
            return False
        cur.execute("""INSERT INTO re_buyer_land (customer_id, tenant_id, readiness, readiness_reason, readiness_by, readiness_at)
                       VALUES (%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT (customer_id) DO UPDATE SET readiness=EXCLUDED.readiness,
                         readiness_reason=EXCLUDED.readiness_reason, readiness_by=EXCLUDED.readiness_by,
                         readiness_at=NOW(), updated_at=NOW()""",
                    (customer_id, tenant_id, level, reason, "staff" if by != "ai" else "ai"))
        if old != level:
            cur.execute("INSERT INTO re_readiness_log (tenant_id, customer_id, readiness, reason, set_by) VALUES (%s,%s,%s,%s,%s)",
                        (tenant_id, customer_id, level, reason, by))
        conn.commit()
        cur.close()
        return old != level
    except Exception as e:
        print(f"⚠️ [ESTATE LAND] set_readiness error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


# ── Text for the hand-over summary and cards ───────────────────────────────────

def summary_lines(land: dict, estates: list = None) -> list:
    if land.get("buyer_kind") != "land":
        return []
    est = None
    if land.get("estate_listing_id") and estates:
        est = next((e for e in estates if e["id"] == land["estate_listing_id"]), None)
    out = ["Buyer type: Land"]
    if land.get("readiness"):
        out.append(f"Readiness: {LEVEL_NAMES.get(land['readiness'], land['readiness'])} — {land.get('readiness_reason') or ''}".rstrip(" —"))
    if est:
        out.append(f"Estate: {est['title']}")
    if land.get("plot_size_sqm"):
        out.append(f"Plot size: {land['plot_size_sqm']:g} sqm")
    if land.get("pay_mode"):
        out.append(f"Pays: {land['pay_mode']}")
    if land.get("start_amount"):
        out.append(f"Starts with: {naira(land['start_amount'])}")
    if land.get("monthly_amount"):
        out.append(f"Monthly: {naira(land['monthly_amount'])}")
    if land.get("purpose"):
        out.append(f"Purpose: {land['purpose']}")
    if land.get("timeframe"):
        out.append(f"Timeframe: {land['timeframe']}")
    return out


def card_summary(e: dict) -> str:
    """'From ₦8,500,000 · 300–600 sqm · plans up to 12 months' for an estate card."""
    open_sizes = [s for s in e.get("sizes", []) if s["plots_left"] != 0] or e.get("sizes", [])
    if not open_sizes:
        return ""
    low = min(s["price"] for s in open_sizes)
    sq = sorted(s["size_sqm"] for s in open_sizes if s["size_sqm"])
    bits = [f"From {naira(low)}"]
    if sq:
        bits.append(f"{sq[0]:g} sqm" if sq[0] == sq[-1] else f"{sq[0]:g}–{sq[-1]:g} sqm")
    longest = max((p["months"] for p in e.get("plans", [])), default=0)
    if longest:
        bits.append(f"plans up to {longest} months")
    return " · ".join(bits)
