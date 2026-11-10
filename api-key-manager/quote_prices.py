"""
quote_prices.py — volume prices and cost prices for quotations (10 Nov 2026).

AI Sales Agent for B2B plan only (same gate as Quotations). One row per
product the quotations use (My Products id or website product id), so it
works for every product source. Kept encrypted (data_crypto): volume steps
and the cost price are business-sensitive.

  breaks  up to 4 steps [[from_qty, price_each], ...], ascending
  cost    what the product costs the business (None = not set)

Costs are only ever read for people allowed to see them (owner, or a role
with "quotes.costs"); they are never put into a quotation, its PDF, the
online quotation or anything sent to the AI Agent.
"""
import io
import json
from decimal import Decimal

import psycopg2.extras

import data_crypto
from db import get_db_connection

MAX_STEPS = 4
_ready = False


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_tables():
    global _ready
    if _ready:
        return
    conn, cur = _db()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS quote_price_book (
                           tenant_id   INTEGER NOT NULL,
                           product_key VARCHAR(80) NOT NULL,
                           data        TEXT NOT NULL,
                           updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           updated_by  TEXT,
                           PRIMARY KEY (tenant_id, product_key))""")
        conn.commit()
        _ready = True
    finally:
        cur.close(); conn.close()


def _num(v, lo=0.0, hi=1e9):
    if v is None or str(v).strip() == "":
        return None
    try:
        x = float(str(v).replace(",", "").replace("£", "").replace("$", "").replace("€", "").strip())
    except (TypeError, ValueError):
        return None
    return round(x, 4) if lo <= x <= hi else None


def clean(breaks, cost) -> dict:
    """Steps: whole/decimal quantity > 0 and a price ≥ 0, at most 4, one per
    quantity, sorted. Cost ≥ 0 or None."""
    out, seen = [], set()
    for st in (breaks or [])[:10]:
        if isinstance(st, dict):
            q, p = _num(st.get("min")), _num(st.get("price"))
        elif isinstance(st, (list, tuple)) and len(st) >= 2:
            q, p = _num(st[0]), _num(st[1])
        else:
            continue
        if q is None or p is None or q <= 0 or q in seen:
            continue
        seen.add(q)
        out.append([q, p])
    out.sort(key=lambda s: s[0])
    return {"breaks": out[:MAX_STEPS], "cost": _num(cost)}


def all_for(tenant_id: int) -> dict:
    """{product_key: {"breaks": [...], "cost": x}}"""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT product_key, data FROM quote_price_book WHERE tenant_id=%s", (int(tenant_id),))
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    out = {}
    for r in rows:
        try:
            out[r["product_key"]] = json.loads(data_crypto.dec(r["data"]) or "{}")
        except (TypeError, ValueError):
            continue
    return out


def save(tenant_id: int, key: str, breaks, cost, by_label: str = "") -> dict:
    ensure_tables()
    key = str(key or "")[:80]
    if not key:
        return {}
    d = clean(breaks, cost)
    conn, cur = _db()
    try:
        if not d["breaks"] and d["cost"] is None:
            cur.execute("DELETE FROM quote_price_book WHERE tenant_id=%s AND product_key=%s", (int(tenant_id), key))
        else:
            cur.execute("""INSERT INTO quote_price_book (tenant_id, product_key, data, updated_by, updated_at)
                           VALUES (%s,%s,%s,%s,NOW())
                           ON CONFLICT (tenant_id, product_key) DO UPDATE
                           SET data=EXCLUDED.data, updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                        (int(tenant_id), key, data_crypto.enc(json.dumps(d)), (by_label or "")[:200]))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return d


def breaks_only(book: dict) -> dict:
    """{key: [[min, price], ...]} — volume prices without costs (for the editor of anyone)."""
    return {k: v.get("breaks") or [] for k, v in book.items() if v.get("breaks")}


def costs_only(book: dict) -> dict:
    return {k: v["cost"] for k, v in book.items() if v.get("cost") is not None}


def step_for(breaks, qty):
    """(price, from_qty) of the step this quantity reaches, or (None, None)."""
    try:
        q = float(qty or 0)
    except (TypeError, ValueError):
        return None, None
    best = (None, None)
    for mn, pr in breaks or []:
        if q >= float(mn):
            best = (float(pr), float(mn))
    return best


def margin(price_after_disc, cost):
    """Margin % of the selling price, or None when it can't be worked out."""
    if cost is None or price_after_disc is None:
        return None
    try:
        p, c = float(price_after_disc), float(cost)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        # nothing charged for something that costs money: always below a minimum
        return -100.0 if c > 0 else None
    return round((p - c) / p * 100, 1)


def qtext(v) -> str:
    v = float(v)
    return ("%d" % v) if v == int(v) else ("%g" % v)


# ── Excel ───────────────────────────────────────────────────────────────────

HEAD = ["Product ID", "Product", "SKU", "List price", "Cost price"] + \
       [x for i in range(1, MAX_STEPS + 1) for x in (f"From qty {i}", f"Price {i}")]


def to_xlsx(products: dict, book: dict) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Prices and margins"
    ws.append(HEAD)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0B1D40")
    for p in sorted(products.values(), key=lambda x: (x.get("name") or "").lower()):
        e = book.get(p["id"]) or {}
        row = [p["id"], p.get("name") or "", p.get("sku") or "",
               float(p["price"]) if isinstance(p.get("price"), (int, float, Decimal)) else None, e.get("cost")]
        for st in (e.get("breaks") or [])[:MAX_STEPS]:
            row += [st[0], st[1]]
        # text cells never start with = + - @ (no formulas from names)
        row = [("'" + v) if isinstance(v, str) and v[:1] in "=+-@" else v for v in row]
        ws.append(row)
    for col, w in zip("ABCDEFGHIJKLM", (24, 44, 14, 11, 11, 11, 10, 11, 10, 11, 10, 11, 10)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "C2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def from_xlsx(tenant_id: int, data: bytes, products: dict, by_label: str = "") -> tuple:
    """(saved, skipped_rows). Only products of this business (by Product ID)."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    head = [str(h or "").strip().lower() for h in next(rows, [])]
    try:
        i_id = head.index("product id")
    except ValueError:
        raise ValueError("The sheet has no \"Product ID\" column. Download the Excel from this page and fill that in.")
    i_cost = head.index("cost price") if "cost price" in head else None
    steps = [(head.index(f"from qty {i}"), head.index(f"price {i}")) for i in range(1, MAX_STEPS + 1)
             if f"from qty {i}" in head and f"price {i}" in head]
    saved, skipped = 0, 0
    book = all_for(tenant_id)
    for n, r in enumerate(rows):
        if n > 5000:
            break
        key = str(r[i_id] or "").strip() if i_id < len(r) else ""
        if not key:
            continue
        if key not in products:
            skipped += 1
            continue
        old = book.get(key) or {}
        br = ([[r[a] if a < len(r) else None, r[b] if b < len(r) else None] for a, b in steps]
              if steps else old.get("breaks") or [])
        cost = r[i_cost] if i_cost is not None and i_cost < len(r) else None
        if cost is None or str(cost).strip() == "":
            cost = old.get("cost")          # a blank cost cell never wipes a cost
        save(tenant_id, key, br, cost, by_label)
        saved += 1
    return saved, skipped
