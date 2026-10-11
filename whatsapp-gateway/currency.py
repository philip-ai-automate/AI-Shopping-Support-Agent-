"""
Currency conversion for PhiXtra WhatsApp gateway.

Rules:
  - £ (GBP) prices → convert to NGN using live exchange rate
  - ₦ (NGN) prices → keep as-is
  - Other symbols   → convert via live rate if known, else keep raw value

Rates are cached in memory for 1 hour to avoid hammering the API on every message.
A hardcoded fallback is used if the API is unreachable.
"""

import selling_currency as _sc
import re
import time

import httpx

# ── In-memory rate cache ──────────────────────────────────────────────────────
# { "GBP": (rate_to_ngn, fetched_at_epoch), ... }
_rate_cache: dict[str, tuple[float, float]] = {}
_CACHE_TTL = 3600  # seconds

# Fallback rates — update occasionally as a safety net
_FALLBACK_RATES = {
    "GBP": 2050.0,
    "USD": 1620.0,
    "EUR": 1760.0,
}

# Currency symbol → ISO code
_SYMBOL_MAP = {
    "£": "GBP",
    "$": "USD",
    "€": "EUR",
    "₦": "NGN",
}


def _fetch_rate(currency: str) -> float:
    """Fetch the live NGN rate for *currency* from exchangerate-api.com (no key needed)."""
    try:
        url = f"https://api.exchangerate-api.com/v4/latest/{currency}"
        with httpx.Client(timeout=6) as client:
            resp = client.get(url)
            resp.raise_for_status()
            rate = float(resp.json()["rates"]["NGN"])
            print(f"   [CURRENCY] live {currency}→NGN rate: {rate:,.2f}")
            return rate
    except Exception as exc:
        fallback = _FALLBACK_RATES.get(currency, 1600.0)
        print(f"   [CURRENCY] rate fetch failed ({exc}), using fallback {currency}→NGN={fallback}")
        return fallback


def _get_rate(currency: str) -> float:
    """Return the NGN conversion rate for *currency*, refreshing cache if stale."""
    if currency == "NGN":
        return 1.0
    cached = _rate_cache.get(currency)
    if cached:
        rate, fetched_at = cached
        if time.time() - fetched_at < _CACHE_TTL:
            return rate
    rate = _fetch_rate(currency)
    _rate_cache[currency] = (rate, time.time())
    return rate


def to_ngn(price_str) -> float:
    """
    Businesses not on naira (selling currency £/$, 2026-10-11): the price in
    their selling currency instead — converted (whole units) when saved in
    another currency. Naira businesses and no business set: unchanged.

    Convert a price string to NGN float.

    Examples:
      "£254.00"  → ~520,700.0  (at current GBP/NGN rate)
      "₦180,000" → 180000.0
      "305.00"   → 305.0       (no symbol — returned unchanged)
    """
    if not price_str:
        return 0.0
    if not _sc.current_is_naira():
        amount, code = _sc.parse_price_text(price_str)
        if amount is None:
            return 0.0
        converted = _sc.amount_for_tenant(_sc.current_tenant(), amount, code)
        return float(converted if converted is not None else amount)
    s = str(price_str).strip()

    # Detect currency from leading symbol
    currency = "UNKNOWN"
    for symbol, code in _SYMBOL_MAP.items():
        if s.startswith(symbol):
            currency = code
            break

    # Strip everything except digits and decimal point
    numeric = re.sub(r'[^\d.]', '', s)
    try:
        amount = float(numeric) if numeric else 0.0
    except ValueError:
        return 0.0

    if currency in ("NGN", "UNKNOWN"):
        return amount

    return round(amount * _get_rate(currency), 2)


def fmt_ngn(price_str) -> str:
    """Convert *price_str* to NGN and return a formatted ₦X,XXX string.
    Businesses not on naira: their selling currency instead."""
    if not _sc.current_is_naira():
        amount, code = _sc.parse_price_text(price_str)
        if amount is None:
            return str(price_str or "")
        return _sc.display_for_tenant(_sc.current_tenant(), amount, code)
    amount = to_ngn(price_str)
    try:
        return f"₦{amount:,.0f}"
    except Exception:
        return f"₦{amount}"


def fmt_amount(amount) -> str:
    """An amount already in the business's selling currency (cart prices,
    discounts): '₦180,000' for naira businesses (unchanged), '$1,233' or
    '£254.50' otherwise."""
    try:
        if _sc.current_is_naira():
            return f"₦{float(amount):,.0f}"
        from decimal import Decimal
        return _sc.format_money(Decimal(str(amount)), _sc.tenant_currency(_sc.current_tenant()))
    except Exception:
        return str(amount)
