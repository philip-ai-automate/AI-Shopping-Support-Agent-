"""
Currency conversion for PhiXtra WhatsApp gateway.

Rules:
  - £ (GBP) prices → convert to NGN using live exchange rate
  - ₦ (NGN) prices → keep as-is
  - Other symbols   → convert via live rate if known, else keep raw value

Rates are cached in memory for 1 hour to avoid hammering the API on every message.
A hardcoded fallback is used if the API is unreachable.
"""

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
    Convert a price string to NGN float.

    Examples:
      "£254.00"  → ~520,700.0  (at current GBP/NGN rate)
      "₦180,000" → 180000.0
      "305.00"   → 305.0       (no symbol — returned unchanged)
    """
    if not price_str:
        return 0.0
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
    """Convert *price_str* to NGN and return a formatted ₦X,XXX string."""
    amount = to_ngn(price_str)
    try:
        return f"₦{amount:,.0f}"
    except Exception:
        return f"₦{amount}"
