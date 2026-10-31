"""
quote_design.py — PhiXtra quotations, Part A (31 Oct 2026): the business's
quotation design and the document itself. Approved mock-up
https://claude.ai/artifact/PKhjF22chVUgXF9mFNaomK (user: "start building").

For businesses that make their quotations in PhiXtra (Quotation settings ›
"How do you make quotations?" = In PhiXtra). PhiXtra owns the layout; the
business fills it in: two templates (Summary first: total at the top;
Traditional: total at the end), a light header or a colour band, its logo,
colour, details, numbering and wording.

One page is used for both the live preview and the PDF (Chromium prints the
same HTML), so what the business sees is exactly what the buyer gets.
Quotations are always in English.
"""
import base64
import io
import json
import re
from datetime import date, timedelta

import psycopg2.extras
from flask import render_template

from db import get_db_connection

TEMPLATES = [("summary", "Summary first"), ("traditional", "Traditional")]
HEADERS = [("light", "Light"), ("band", "Colour band")]
NUMBER_FORMATS = [("plain", "Q-1047"), ("year", "Q-2026-0047")]
MAX_LOGO = 3 * 1024 * 1024
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_PREFIX = re.compile(r"^[A-Za-z0-9-]{1,8}$")

DEFAULTS = {
    "template": "summary",
    "header": "light",
    "colour": "#0B1D40",
    "show": {"address": True, "email": True, "phone": True, "vat": True, "company_no": True, "website": False},
    "email": "", "phone": "", "website": "",
    "prefix": "Q", "number_format": "plain", "next_number": 1001,
    "valid_days": 14,
    "columns": {"code": True, "unit": True, "discount": True},
    "title": "Quotation",
    "opening": "Thank you for your enquiry. We're pleased to quote for the following:",
    "payment_terms": "",
    "deposit_pct": 0,
    "bank_details": "",
    "terms": "",
    "footer": "",
}


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


_tables_ok = False


def ensure_tables():
    global _tables_ok
    if _tables_ok:
        return
    conn, cur = _db()
    try:
        cur.execute("SELECT to_regclass('quote_design') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            cur.execute("SET lock_timeout = '5s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS quote_design (
                               tenant_id  INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
                               settings   JSONB NOT NULL DEFAULT '{}'::jsonb,
                               logo       BYTEA,
                               updated_by VARCHAR(200),
                               updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            conn.commit()
        _tables_ok = True
    except Exception as e:
        conn.rollback()
        print("⚠️ quote_design tables:", e)
    finally:
        cur.close(); conn.close()


# ── the business's details ──────────────────────────────────────────────────

def profile(tenant_id: int) -> dict:
    """Name, address, company number, VAT from the business profile."""
    # Settings › Business details (on the owner's row) first, then the
    # company profile from a B2B sign-up, then the business name.
    conn, cur = _db()
    try:
        cur.execute("""SELECT t.name, cp.registered_name, cp.trading_name, cp.address_line1, cp.city, cp.postcode,
                              cp.reg_country, cp.reg_number, cp.vat_number,
                              c.company_name, c.vat_number AS c_vat, c.billing_address_line1, c.billing_city,
                              c.billing_postcode, c.billing_country
                         FROM tenants t LEFT JOIN company_profiles cp ON cp.tenant_id = t.id
                         LEFT JOIN LATERAL (SELECT * FROM customers WHERE tenant_id = t.id ORDER BY id LIMIT 1) c ON TRUE
                        WHERE t.id=%s""", (tenant_id,))
        r = cur.fetchone() or {}
    finally:
        cur.close(); conn.close()
    billing = bool(r.get("billing_address_line1"))
    line1 = r.get("billing_address_line1") if billing else r.get("address_line1")
    city = r.get("billing_city") if billing else r.get("city")
    pc = r.get("billing_postcode") if billing else r.get("postcode")
    cc = (r.get("billing_country") if billing else r.get("reg_country")) or r.get("reg_country") or ""
    try:
        import portal_address
        country = dict(portal_address.ADDRESS_COUNTRIES).get(cc.upper(), "")
    except Exception:
        country = ""
    addr = ", ".join(x for x in (line1, " ".join(y for y in (city, pc) if y), country) if x)
    return {"name": r.get("company_name") or r.get("registered_name") or r.get("trading_name") or r.get("name") or "",
            "address": addr, "company_no": r.get("reg_number") or "", "vat": r.get("c_vat") or r.get("vat_number") or ""}


# ── settings ────────────────────────────────────────────────────────────────

def _text(v, n):
    return re.sub(r"[ \t]+", " ", str(v or "")).strip()[:n]


def clean(raw: dict, base: dict = None) -> dict:
    s = json.loads(json.dumps(base or DEFAULTS))
    raw = raw or {}
    if raw.get("template") in dict(TEMPLATES):
        s["template"] = raw["template"]
    if raw.get("header") in dict(HEADERS):
        s["header"] = raw["header"]
    if isinstance(raw.get("colour"), str) and _HEX.match(raw["colour"].strip()):
        s["colour"] = raw["colour"].strip().upper()
    for group in ("show", "columns"):
        if isinstance(raw.get(group), dict):
            for k in s[group]:
                if k in raw[group]:
                    v = raw[group][k]
                    s[group][k] = v is True or v in ("1", "true", "on")
    for k, n in (("email", 160), ("phone", 40), ("website", 160), ("title", 30), ("opening", 300),
                 ("payment_terms", 300), ("footer", 300)):
        if k in raw:
            s[k] = _text(raw[k], n)
    for k, n in (("bank_details", 600), ("terms", 12000)):
        if k in raw:
            s[k] = str(raw[k] or "").replace("\r\n", "\n").strip()[:n]
    if s.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s["email"]):
        s["email"] = ""
    if isinstance(raw.get("prefix"), str) and _PREFIX.match(raw["prefix"].strip()):
        s["prefix"] = raw["prefix"].strip().upper()
    if raw.get("number_format") in dict(NUMBER_FORMATS):
        s["number_format"] = raw["number_format"]
    for k, lo, hi in (("next_number", 1, 99_999_999), ("valid_days", 1, 365), ("deposit_pct", 0, 100)):
        try:
            if raw.get(k) not in (None, ""):
                s[k] = max(lo, min(hi, int(float(raw[k]))))
        except (TypeError, ValueError):
            pass
    return s


def get(tenant_id: int) -> dict:
    """The design, with first-time defaults from the business (Brand Kit colour,
    owner's email) when nothing has been saved yet."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT settings, logo IS NOT NULL AS has_logo, updated_at FROM quote_design WHERE tenant_id=%s",
                    (tenant_id,))
        r = cur.fetchone()
        first = None
        if not r:
            first = {}
            cur.execute("SELECT id, email FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1", (tenant_id,))
            owner = cur.fetchone() or {}
            first["email"] = owner.get("email") or ""
            try:
                cur.execute("SELECT color_main FROM brand_kits WHERE owner_key=%s", (f"owner:{owner.get('id')}",))
                k = cur.fetchone()
                if k and k.get("color_main"):
                    first["colour"] = k["color_main"]
            except Exception:
                conn.rollback()
    finally:
        cur.close(); conn.close()
    s = clean(r["settings"] if r else first)
    s["has_logo"] = bool(r and r["has_logo"]) or (not r and bool(_brand_kit_logo(tenant_id)))
    s["saved"] = bool(r)
    return s


def save(tenant_id: int, raw: dict, by_label: str = "") -> dict:
    s = clean(raw, base=get(tenant_id))
    s.pop("has_logo", None); s.pop("saved", None)
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO quote_design (tenant_id, settings, logo, updated_by, updated_at)
                       VALUES (%s,%s,%s,%s,NOW())
                       ON CONFLICT (tenant_id) DO UPDATE SET settings=EXCLUDED.settings,
                              updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                    (tenant_id, psycopg2.extras.Json(s), psycopg2.Binary(_brand_kit_logo(tenant_id) or b"") or None,
                     (by_label or "")[:200]))
        # The first save takes the Brand Kit logo (if any); an empty value means none.
        cur.execute("UPDATE quote_design SET logo=NULL WHERE tenant_id=%s AND octet_length(logo)=0", (tenant_id,))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return get(tenant_id)


# ── the logo ────────────────────────────────────────────────────────────────

def _brand_kit_logo(tenant_id: int):
    """The Brand Kit logo (Social › Brand Kit), as PNG bytes, or None."""
    try:
        import os
        import ai_designer
        conn, cur = _db()
        try:
            cur.execute("SELECT id FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1", (tenant_id,))
            o = cur.fetchone()
        finally:
            cur.close(); conn.close()
        if not o:
            return None
        p = ai_designer.logo_path(ai_designer.get_brand_kit(f"owner:{o['id']}"))
        if p and os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
    except Exception as e:
        print("⚠️ quote_design brand kit logo:", e)
    return None


def logo_bytes(tenant_id: int):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT logo FROM quote_design WHERE tenant_id=%s", (tenant_id,))
        r = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if r is None:
        return _brand_kit_logo(tenant_id)
    return bytes(r["logo"]) if r["logo"] is not None else None


def save_logo(tenant_id: int, data: bytes, by_label: str = "") -> str:
    """'' when saved, else what went wrong. Stored as a PNG at most 800px wide."""
    if not data:
        return "Choose a logo file."
    if len(data) > MAX_LOGO:
        return "That logo is over 3 MB. Use a smaller file."
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return "That file couldn't be opened as an image. Use PNG or JPG."
    img = img.convert("RGBA")
    if img.width > 800:
        img = img.resize((800, max(1, round(img.height * 800 / img.width))))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    if not get(tenant_id)["saved"]:
        save(tenant_id, {}, by_label)
    conn, cur = _db()
    try:
        cur.execute("UPDATE quote_design SET logo=%s, updated_by=%s, updated_at=NOW() WHERE tenant_id=%s",
                    (psycopg2.Binary(out.getvalue()), (by_label or "")[:200], tenant_id))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return ""


def remove_logo(tenant_id: int, by_label: str = ""):
    if not get(tenant_id)["saved"]:
        save(tenant_id, {}, by_label)
    conn, cur = _db()
    try:
        cur.execute("UPDATE quote_design SET logo=NULL, updated_by=%s, updated_at=NOW() WHERE tenant_id=%s",
                    ((by_label or "")[:200], tenant_id))
        conn.commit()
    finally:
        cur.close(); conn.close()


def colour_from_logo(tenant_id: int):
    """The logo's main colour (not white, black or grey), or None."""
    data = logo_bytes(tenant_id)
    if not data:
        return None
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGBA").resize((80, 80))
    counts = {}
    for r, g, b, a in img.getdata():
        if a < 128 or max(r, g, b) - min(r, g, b) < 24 or (r > 235 and g > 235 and b > 235):
            continue
        key = (r // 16 * 16, g // 16 * 16, b // 16 * 16)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    c = max(counts.items(), key=lambda kv: kv[1])[0]
    return "#%02X%02X%02X" % c


# ── what is missing before the first quotation ──────────────────────────────

def checklist(tenant_id: int, s: dict, prof: dict = None) -> list:
    prof = prof or profile(tenant_id)
    return [
        {"ok": bool(prof["name"] and prof["address"]), "text": "Business name and address",
         "fix": "Settings › Business details"},
        {"ok": True, "text": f"Quotation numbers: {number_text(s, s['next_number'])} next", "fix": ""},
        {"ok": bool(s.get("payment_terms")), "text": "Payment terms", "fix": "Your words, below"},
        {"ok": bool(s.get("bank_details")), "text": "Bank details", "fix": "Your words, below"},
        {"ok": bool(s.get("has_logo")), "text": "Logo (recommended, not required)", "fix": "", "soft": True},
    ]


def ready(tenant_id: int) -> bool:
    s = get(tenant_id)
    return all(c["ok"] for c in checklist(tenant_id, s) if not c.get("soft"))


def number_text(s: dict, n: int, year: int = None) -> str:
    if s.get("number_format") == "year":
        return f"{s.get('prefix') or 'Q'}-{year or date.today().year}-{n:04d}"
    return f"{s.get('prefix') or 'Q'}-{n}"


# ── the document ────────────────────────────────────────────────────────────

def money(v, code):
    try:
        import selling_currency
        prefix = selling_currency.currency_label((code or "GBP").upper())["prefix"]
    except Exception:
        prefix = ""
    return f"{prefix}{float(v):,.2f}"


def plain(v):
    return f"{float(v):,.2f}"


def sample_quote(tenant_id: int, s: dict) -> dict:
    """An example quotation from the business's own products, for the
    preview and the test PDF."""
    try:
        import selling_currency
        code = selling_currency.tenant_currency(tenant_id)
    except Exception:
        code = "GBP"
    conn, cur = _db()
    try:
        cur.execute("""SELECT name, price FROM products WHERE tenant_id=%s AND is_active AND price > 0
                        AND NOT price_on_request ORDER BY updated_at DESC NULLS LAST LIMIT 4""", (tenant_id,))
        prods = cur.fetchall()
    finally:
        cur.close(); conn.close()
    if not prods:
        prods = [{"name": "Your first product", "price": 120}, {"name": "Your second product", "price": 45}]
    qtys = [40, 20, 100, 30]
    lines = []
    for i, p in enumerate(prods):
        disc = 5 if i == 2 else 0
        lines.append({"code": "", "name": p["name"], "pack": "", "qty": qtys[i % 4], "unit": "",
                      "price": float(p["price"]), "disc": disc, "note": "Volume discount" if disc else ""})
    today = date.today()
    q = {"number": number_text(s, s["next_number"], today.year), "date": today,
         "valid_until": today + timedelta(days=s["valid_days"]), "ref": "R-1041",
         "buyer": {"company": "Example Buyer Ltd", "name": "Alex Morgan", "role": "Purchasing",
                   "email": "alex@example.com"},
         "deliver": {"place": "Cologne, Germany", "terms": "Delivered to your door"},
         "prepared_by": {"name": "Your salesperson", "phone": s.get("phone") or "", "email": s.get("email") or ""},
         "lines": lines, "charges": [{"name": "Delivery", "qty": 1, "price": 95.0}],
         "tax": {"rate": 0, "label": "VAT 0%", "reason": "export of goods"},
         "note": "", "plan": [], "currency": code, "sample": True}
    return totals(q, s)


def totals(q: dict, s: dict) -> dict:
    """Line amounts, sums, tax, total and deposit (2 decimals each step)."""
    prod = 0.0
    for ln in q["lines"]:
        unit = float(ln["price"]) * (1 - float(ln.get("disc") or 0) / 100)
        ln["amount"] = round(unit * float(ln["qty"]), 2)
        prod += ln["amount"]
    ch = 0.0
    for c in q.get("charges") or []:
        c["amount"] = round(float(c["price"]) * float(c.get("qty") or 1), 2)
        ch += c["amount"]
    q["products_total"] = round(prod, 2)
    q["charges_total"] = round(ch, 2)
    q["subtotal"] = round(prod + ch, 2)
    rate = float((q.get("tax") or {}).get("rate") or 0)
    q["tax_amount"] = round(q["subtotal"] * rate / 100, 2)
    q["total"] = round(q["subtotal"] + q["tax_amount"], 2)
    pct = int(s.get("deposit_pct") or 0)
    q["deposit"] = round(q["total"] * pct / 100, 2) if pct else 0
    return q


def _logo_uri(tenant_id: int):
    b = logo_bytes(tenant_id)
    return "data:image/png;base64," + base64.b64encode(b).decode() if b else None


def _initials(name: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", name or "") if w.lower() not in ("ltd", "limited", "plc", "llc", "inc", "gmbh")]
    return ("".join(w[0] for w in words[:2]) or "Q").upper()


def _soft(hexcol: str) -> str:
    """A very light tint of the brand colour for backgrounds."""
    h = hexcol.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: round(c + (255 - c) * 0.9)
    return "#%02X%02X%02X" % (mix(r), mix(g), mix(b))


def render_html(tenant_id: int, s: dict, q: dict, screen: bool = True, logo_uri=None) -> str:
    prof = profile(tenant_id)
    show_code = s["columns"]["code"] and any(ln.get("code") for ln in q["lines"])
    return render_template("portal/quote_doc.html", s=s, q=q, prof=prof, screen=screen,
                           logo=logo_uri if logo_uri is not None else _logo_uri(tenant_id),
                           initials=_initials(prof["name"]), soft=_soft(s["colour"]), show_code=show_code,
                           money=lambda v: money(v, q.get("currency")), plain=plain)


def render_pdf(html: str, footer_left: str = "", footer_right: str = "") -> bytes:
    """The same page, printed by Chromium on A4, page numbers in the footer."""
    import html as _h
    from playwright.sync_api import sync_playwright
    foot = ('<div style="width:100%;font-family:Arial,sans-serif;font-size:8px;color:#5D6B7C;padding:0 14mm;'
            'display:flex;justify-content:space-between"><span>' + _h.escape(footer_left) + '</span><span>'
            + _h.escape(footer_right) + ' · Page <span class="pageNumber"></span> of <span class="totalPages"></span>'
            '</span></div>')
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = b.new_page()
            page.set_content(html, wait_until="networkidle", timeout=20000)
            return page.pdf(format="A4", print_background=True, display_header_footer=True,
                            header_template="<span></span>", footer_template=foot,
                            margin={"top": "12mm", "bottom": "16mm", "left": "0", "right": "0"})
        finally:
            b.close()
