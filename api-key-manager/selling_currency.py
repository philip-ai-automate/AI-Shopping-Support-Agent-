"""Selling currency — what a business's customers see (2026-10-11).

Each business has one selling currency (tenants.selling_currency: AED, GBP,
USD or NGN — the list lives in the selling_currencies table). Product prices
saved in another currency (e.g. a website that keeps prices in £ and lets
buyers switch to AED) are converted to it:
  - fx_rate_mode 'daily' → market rate, fetched once a day (fx_rates_job.py)
  - fx_rate_mode 'own'   → the rate the business typed (tenant_fx_rates);
                           a currency it gave no rate for uses the daily rate.
Converted prices are rounded to whole units. Prices already in the selling
currency are shown exactly as typed. No rate known → nothing is guessed: the
caller shows the original price.

Plan payments (naira / pounds) do NOT use this.
"""

from __future__ import annotations

import contextvars
import re
import time
from decimal import Decimal, ROUND_HALF_UP

try:                                   # portal + AI service
    from db import get_db_connection
except ImportError:                    # WhatsApp service
    from wa_db import get_db_connection

DEFAULT_CURRENCY = "NGN"

_cache: dict = {}
_TTL = 60


def _cached(key, loader):
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < _TTL:
        return hit[0]
    value = loader()
    _cache[key] = (value, time.time())
    return value


def clear_cache(tenant_id=None):
    """Call after a business changes its currency or rates."""
    if tenant_id is None:
        _cache.clear()
        return
    for k in [k for k in _cache if isinstance(k, tuple) and k[1] == int(tenant_id)]:
        _cache.pop(k, None)


def _query(sql, params=()):
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


# ── The currency list ────────────────────────────────────────────────────────

def currencies() -> list[dict]:
    """The selling currencies a business can choose, in display order."""
    def load():
        try:
            rows = _query("SELECT code, name, prefix, country FROM selling_currencies "
                          "ORDER BY sort_order, code")
        except Exception as e:
            print("⚠️ selling_currency.currencies error:", e)
            rows = []
        return [{"code": r[0], "name": r[1], "prefix": r[2], "country": r[3]} for r in rows]
    return _cached("currencies", load)


def currency_codes() -> list[str]:
    return [c["code"] for c in currencies()]


def currency_for_country(country_iso) -> str | None:
    """The currency to fill in for a business based in this country, or None
    (any other country: the business picks one itself)."""
    iso = (country_iso or "").strip().upper()
    for c in currencies():
        if c["country"] == iso:
            return c["code"]
    return None


# ── A business's settings ────────────────────────────────────────────────────

def tenant_currency(tenant_id) -> str:
    """The business's selling currency (naira when unknown)."""
    if not tenant_id:
        return DEFAULT_CURRENCY

    def load():
        try:
            rows = _query("SELECT selling_currency FROM tenants WHERE id=%s", (int(tenant_id),))
        except Exception as e:
            print("⚠️ selling_currency.tenant_currency error:", e)
            rows = []
        return ((rows[0][0] if rows else None) or DEFAULT_CURRENCY).upper()
    return _cached(("cur", int(tenant_id)), load)


def tenant_rate_mode(tenant_id) -> str:
    def load():
        try:
            rows = _query("SELECT fx_rate_mode FROM tenants WHERE id=%s", (int(tenant_id),))
        except Exception as e:
            print("⚠️ selling_currency.tenant_rate_mode error:", e)
            rows = []
        return (rows[0][0] if rows else None) or "daily"
    return _cached(("mode", int(tenant_id)), load)


def tenant_own_rates(tenant_id) -> dict:
    """{from_currency: Decimal rate} the business typed."""
    def load():
        try:
            rows = _query("SELECT from_currency, rate FROM tenant_fx_rates WHERE tenant_id=%s",
                          (int(tenant_id),))
        except Exception as e:
            print("⚠️ selling_currency.tenant_own_rates error:", e)
            rows = []
        return {r[0].upper(): Decimal(r[1]) for r in rows}
    return _cached(("own", int(tenant_id)), load)


# ── Rates ────────────────────────────────────────────────────────────────────

def _market_table() -> dict:
    def load():
        try:
            rows = _query("SELECT currency, per_usd, fetched_at FROM fx_rates")
        except Exception as e:
            print("⚠️ selling_currency market rates error:", e)
            rows = []
        return {r[0].upper(): (Decimal(r[1]), r[2]) for r in rows}
    return _cached("market", load)


def market_rate(from_code, to_code) -> Decimal | None:
    """1 unit of from_code = this many units of to_code, at the daily rate."""
    f, t = (from_code or "").upper(), (to_code or "").upper()
    if f == t:
        return Decimal(1)
    table = _market_table()
    if f not in table or t not in table or table[f][0] <= 0:
        return None
    return table[t][0] / table[f][0]


def market_rates_updated_at():
    """When the daily rates were last fetched (None = never)."""
    table = _market_table()
    return max((v[1] for v in table.values()), default=None)


def rate_for(tenant_id, from_code) -> tuple[Decimal | None, str | None]:
    """(rate, 'own'|'daily') to turn from_code into the business's selling
    currency; (None, None) when no rate is known."""
    to_code = tenant_currency(tenant_id)
    f = (from_code or "").upper()
    if f == to_code:
        return Decimal(1), None
    if tenant_rate_mode(tenant_id) == "own":
        own = tenant_own_rates(tenant_id).get(f)
        if own:
            return own, "own"
    rate = market_rate(f, to_code)
    return (rate, "daily") if rate is not None else (None, None)


def whole(amount) -> int:
    """Round half-up to a whole unit (AED 1,181, not AED 1,181.10)."""
    return int(Decimal(str(amount)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def convert_for_tenant(tenant_id, amount, from_code) -> dict | None:
    """A price in from_code, ready to show in the business's selling currency.

    Same currency → the amount unchanged (shown as typed).
    Other currency → converted and rounded to whole units.
    No rate known → None (the caller shows the original price).
    """
    if amount is None:
        return None
    to_code = tenant_currency(tenant_id)
    f = (from_code or to_code).upper()
    if f == to_code:
        return {"amount": Decimal(str(amount)), "currency": to_code, "converted": False,
                "rate": None, "source": None}
    rate, source = rate_for(tenant_id, f)
    if rate is None:
        return None
    return {"amount": Decimal(whole(Decimal(str(amount)) * rate)), "currency": to_code,
            "converted": True, "rate": rate, "source": source}


# ── Showing a price ──────────────────────────────────────────────────────────

def format_money(amount, code) -> str:
    """'AED 1,250' · '£1,250' · '$1,250' · '₦1,250'. Pence/fils are kept only
    when the amount has them (a typed £254.50 stays £254.50)."""
    if amount is None:
        return ""
    code = (code or DEFAULT_CURRENCY).upper()
    prefix = next((c["prefix"] for c in currencies() if c["code"] == code), code + " ")
    d = Decimal(str(amount))
    num = f"{d:,.0f}" if d == d.to_integral_value() else f"{d:,.2f}"
    return f"{prefix}{num}"


# ── Which currency a saved price is in ───────────────────────────────────────

# Checked in this order; "AED" before "$" so "AED 50" is never read as dollars.
_SIGNS = [
    (re.compile(r"\bAED\b|د\.إ", re.I), "AED"),
    (re.compile(r"\bNGN\b|₦|\bnaira\b", re.I), "NGN"),
    (re.compile(r"\bGBP\b|£", re.I), "GBP"),
    (re.compile(r"\bEUR\b|€", re.I), "EUR"),
    (re.compile(r"\bUSD\b|\$", re.I), "USD"),
]


def detect_price_currency(price_text=None, saved_currency=None) -> str | None:
    """The currency a product price is saved in: the currency the website
    sync recorded, else the sign written in the price text. None = no sign
    (the caller treats it as the business's selling currency)."""
    code = (saved_currency or "").strip().upper()
    if re.fullmatch(r"[A-Z]{3}", code):
        return code
    text = str(price_text or "")
    for pattern, c in _SIGNS:
        if pattern.search(text):
            return c
    return None


# ── Settings (phase 3) ───────────────────────────────────────────────────────

# Names/signs for currencies product prices may be saved in that are not
# selling currencies (shown in the Settings rate table). Anything else shows
# its code.
_OTHER_CURRENCIES = {
    "EUR": ("Euro", "€"), "AED": ("UAE dirham", "AED "), "CAD": ("Canadian dollar", "CA$"),
    "AUD": ("Australian dollar", "A$"), "GHS": ("Ghanaian cedi", "GH₵"), "KES": ("Kenyan shilling", "KSh "),
    "ZAR": ("South African rand", "R"),
}

# Records that carry their own currency once the business changes its
# selling currency. NULL = the business's current selling currency.
STAMPED_TABLES = ("merchant_pipeline_leads", "orders", "products")


def currency_label(code) -> dict:
    """{'code', 'name', 'prefix'} for any currency code."""
    code = (code or "").upper()
    for c in currencies():
        if c["code"] == code:
            return {"code": code, "name": c["name"], "prefix": c["prefix"]}
    name, prefix = _OTHER_CURRENCIES.get(code, (code, code + " "))
    return {"code": code, "name": name, "prefix": prefix}


def format_rate(from_code, rate, to_code) -> str:
    """'£1 = ₦1,760.58' · '£1 = $1.3228'."""
    if rate is None:
        return ""
    r = Decimal(str(rate))
    num = f"{r:,.2f}" if r >= 100 else f"{r:,.4f}".rstrip("0").rstrip(".")
    return f"{currency_label(from_code)['prefix']}1 = {currency_label(to_code)['prefix']}{num}"


def foreign_price_currencies(tenant_id) -> list[str]:
    """Currencies the business's product prices are saved in, other than its
    selling currency (from the website sync and products added here)."""
    selling = tenant_currency(tenant_id)
    try:
        rows = _query("""
            SELECT DISTINCT upper(currency) FROM documents
             WHERE tenant_id=%s AND type='product' AND currency ~* '^[a-z]{3}$'
            UNION
            SELECT DISTINCT upper(currency) FROM products
             WHERE tenant_id=%s AND currency ~* '^[a-z]{3}$'""", (int(tenant_id), int(tenant_id)))
    except Exception as e:
        print("⚠️ foreign_price_currencies error:", e)
        rows = []
    return sorted({r[0] for r in rows} - {selling})


def save_settings(tenant_id, new_code, rate_mode=None, own_rates=None, by=None) -> dict:
    """Save a business's selling currency and rate choice in one go.

    Changing currency first stamps the business's existing deals, orders and
    products with the OLD currency (so they keep it), and clears its own
    rates (they were written in the old currency). own_rates = {code: Decimal}
    replaces the saved own rates; a currency left out uses the daily rate.
    Returns {'changed': bool, 'old': code, 'rates_cleared': bool}."""
    tid = int(tenant_id)
    new_code = (new_code or "").upper()
    if new_code not in currency_codes():
        raise ValueError("unknown selling currency")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT selling_currency FROM tenants WHERE id=%s FOR UPDATE", (tid,))
        old = (cur.fetchone() or ["NGN"])[0] or "NGN"
        changed = old != new_code
        rates_cleared = False
        if changed:
            for tbl in STAMPED_TABLES:
                cur.execute(f"UPDATE {tbl} SET currency=%s WHERE tenant_id=%s AND currency IS NULL",
                            (old, tid))
            cur.execute("DELETE FROM tenant_fx_rates WHERE tenant_id=%s", (tid,))
            rates_cleared = cur.rowcount > 0
            # The AI's standard pricing instruction follows the new currency
            # (a business's own wording is covered by pricing_override()).
            for tbl, key in (("tenants", "id"), ("tenant_agents", "tenant_id")):
                cur.execute(f"SELECT id, system_prompt FROM {tbl} WHERE {key}=%s", (tid,))
                for rid, prompt in cur.fetchall():
                    new_prompt = rewrite_prompt_pricing(prompt, new_code)
                    if new_prompt:
                        cur.execute(f"UPDATE {tbl} SET system_prompt=%s WHERE id=%s", (new_prompt, rid))
        elif own_rates is not None:
            cur.execute("DELETE FROM tenant_fx_rates WHERE tenant_id=%s", (tid,))
            for code, rate in own_rates.items():
                cur.execute("INSERT INTO tenant_fx_rates (tenant_id, from_currency, rate) VALUES (%s,%s,%s)",
                            (tid, code.upper(), rate))
        mode = rate_mode if rate_mode in ("daily", "own") else None
        cur.execute("""UPDATE tenants SET selling_currency=%s, currency_needs_choice=FALSE,
                              fx_rate_mode=COALESCE(%s, fx_rate_mode) WHERE id=%s""",
                    (new_code, mode, tid))
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    clear_cache(tid)
    return {"changed": changed, "old": old, "rates_cleared": rates_cleared}


# ── What customers see (phase 4) ─────────────────────────────────────────────
# Used by the AI service and the WhatsApp service (this file is shared with
# them). A business on naira (the default) never takes these paths: their
# callers keep today's behaviour unless tenant_currency() is not NGN.

def is_naira(tenant_id) -> bool:
    return tenant_currency(tenant_id) == "NGN"


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_price_text(text):
    """'£254.00' → (Decimal('254.00'), 'GBP'); '1,250' → (Decimal('1250'), None);
    nothing usable → (None, None). Only the first number is read."""
    s = str(text or "")
    m = _NUM.search(s)
    if not m:
        return None, None
    try:
        amount = Decimal(m.group(0).replace(",", ""))
    except Exception:
        return None, None
    return amount, detect_price_currency(s)


def display_for_tenant(tenant_id, amount, from_code=None) -> str:
    """A price as a customer of this business should see it: converted to
    the selling currency and rounded to whole units, or shown as typed when
    already in it. from_code None = already in the selling currency. No rate
    known → the original price with its own sign (nothing guessed)."""
    if amount is None:
        return ""
    conv = convert_for_tenant(tenant_id, amount, from_code)
    if conv:
        return format_money(conv["amount"], conv["currency"])
    return format_money(amount, from_code)


def amount_for_tenant(tenant_id, amount, from_code=None):
    """Like display_for_tenant but the number (Decimal) in the selling
    currency, or None when no rate is known."""
    if amount is None:
        return None
    conv = convert_for_tenant(tenant_id, amount, from_code)
    return conv["amount"] if conv else None


# The AI's default instruction (portal DEFAULT_SYSTEM_PROMPT) assumes naira.
NAIRA_PRICING_BLOCK = (
    "PRICING:\n"
    "- Always quote prices in Nigerian Naira (₦).\n"
    "- If a product price is in GBP (£), convert to Naira at approximately ₦1,850 per £1 "
    "and display as ₦X,XXX. Example: £254 ≈ ₦469,900.\n"
    "- Never show £, $, or any foreign currency symbol to the customer — always use ₦.\n\n"
)


def pricing_block(code) -> str:
    """The PRICING part of a business's AI instruction for its currency."""
    code = (code or DEFAULT_CURRENCY).upper()
    if code == "NGN":
        return NAIRA_PRICING_BLOCK
    c = currency_label(code)
    sign = c["prefix"].strip()
    return (
        "PRICING:\n"
        f"- Always quote prices in {c['name']}s ({sign}).\n"
        f"- Prices in the store data are already in {c['name']}s. Quote them exactly as given; "
        "never convert them yourself.\n"
        f"- Never quote prices in naira (₦) or any other currency — always use {sign}.\n\n"
    )


def rewrite_prompt_pricing(prompt, code):
    """Swap a stored AI instruction's PRICING part for this currency's.
    Only the standard text is swapped; a business's own wording is left
    alone (the AI service adds pricing_override() for those). Returns the
    new prompt, or None when nothing was swapped."""
    if not prompt:
        return None
    for code_ in currency_codes() + ["NGN"]:
        block = pricing_block(code_)
        if block in prompt:
            new = prompt.replace(block, pricing_block(code))
            return new if new != prompt else None
    return None


def pricing_override(tenant_id) -> str:
    """Added to the AI's instruction on every reply for businesses not on
    naira, so a business's own (customised) wording can't make the AI quote
    naira. Empty for naira businesses."""
    code = tenant_currency(tenant_id)
    if code == "NGN":
        return ""
    c = currency_label(code)
    sign = c["prefix"].strip()
    return (
        f"\n\nPRICING (this rule replaces any earlier pricing rule): this business sells in "
        f"{c['name']}s ({sign}). Every product price in the store data is already in {c['name']}s — "
        "quote it exactly as given and never convert it yourself. Never quote prices in naira (₦) "
        "unless the store data itself shows that price in naira."
    )


# The business the WhatsApp service is answering right now (set once per
# incoming message), so price formatting deep in the message code knows
# which currency to use without passing it through every function.
_current_tenant = contextvars.ContextVar("selling_currency_tenant", default=None)


def set_current_tenant(tenant_id):
    return _current_tenant.set(int(tenant_id) if tenant_id else None)


def current_tenant():
    return _current_tenant.get()


def current_is_naira() -> bool:
    """True when no business is set (today's behaviour) or it sells in naira."""
    tid = _current_tenant.get()
    return not tid or is_naira(tid)
