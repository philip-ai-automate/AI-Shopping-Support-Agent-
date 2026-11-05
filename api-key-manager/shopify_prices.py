"""
shopify_prices.py — PhiXtra for Shopify, Part 5: a trade customer's own
Shopify prices (3 Nov 2026; plan https://claude.ai/artifact/Ev9UoRU1XMbjZ4zGKoZEbX,
all recommendations approved).

For one B2B company location (site) and a few products, read live from
Shopify: each variant's price for that site (its catalogue price), volume
prices, quantity rules (minimum, maximum, steps), and whether the product
is in the site's catalogue at all. Nothing is stored here; quotations keep
what they used, and the AI agent asks again per chat turn.

Product ids are PhiXtra document ids ('product-<Shopify id>', Part 2).
"""
import re

import psycopg2.extras

from db import get_db_connection

PER_CALL = 4            # products per Shopify call (keeps the query cost low)
MAX_VARIANTS = 25
MAX_BREAKS = 5

_Q = """query CompanyPrices($ids: [ID!]!, $loc: ID!) { nodes(ids: $ids) { ... on Product {
  id title publishedInContext(context: {companyLocationId: $loc})
  variants(first: 25) { nodes { id title sku
    contextualPricing(context: {companyLocationId: $loc}) {
      price { amount currencyCode } compareAtPrice { amount currencyCode }
      quantityRule { minimum maximum increment }
      quantityPriceBreaks(first: 5) { nodes { minimumQuantity price { amount currencyCode } } } } } } } } }"""


def _legacy(gid) -> str:
    return str(gid or "").rsplit("/", 1)[-1]


def is_shopify_product(pid) -> bool:
    return bool(re.match(r"^product-\d+$", str(pid or "")))


def _site(tenant_id: int, location_id: str):
    """(shop, site row) when the site belongs to this business's store."""
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT s.shop, l.location_id, l.name, l.payment_terms, l.company_id, c.name AS company
                         FROM shopify_shops s
                         JOIN shopify_b2b_locations l ON l.shop = s.shop
                         LEFT JOIN shopify_b2b_companies c ON c.shop = l.shop AND c.company_id = l.company_id
                        WHERE s.tenant_id=%s AND s.uninstalled_at IS NULL AND l.location_id=%s""",
                    (int(tenant_id), str(location_id)))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def _f(v):
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def site_prices(tenant_id: int, location_id: str, product_ids) -> dict:
    """{product_id: {"title", "published", "currency", "variants": [...]}} for
    the Shopify products among product_ids. {} when the site isn't this
    business's, or Shopify can't be reached (callers then keep list prices)."""
    ids = [p for p in dict.fromkeys(product_ids or []) if is_shopify_product(p)]
    if not ids or not location_id:
        return {}
    site = _site(tenant_id, location_id)
    if not site:
        return {}
    import shopify_app as sa
    out = {}
    loc_gid = f"gid://shopify/CompanyLocation/{site['location_id']}"
    for i in range(0, len(ids), PER_CALL):
        chunk = ids[i:i + PER_CALL]
        gids = [f"gid://shopify/Product/{p.split('-', 1)[1]}" for p in chunk]
        try:
            data = sa.graphql(site["shop"], _Q, {"ids": gids, "loc": loc_gid})
        except Exception as e:
            if "read_publications" not in str(e):
                print(f"⚠️ shopify_prices {site['shop']} site {location_id}: {e}")
                return out
            # The store hasn't approved the catalogue permission yet: prices
            # still work, the 'not in their catalogue' check doesn't.
            try:
                data = sa.graphql(site["shop"], _Q.replace(" publishedInContext(context: {companyLocationId: $loc})", ""),
                                  {"ids": gids, "loc": loc_gid})
            except Exception as e2:
                print(f"⚠️ shopify_prices {site['shop']} site {location_id}: {e2}")
                return out
        for node in data.get("nodes") or []:
            if not node or not node.get("id"):
                continue
            pid = f"product-{_legacy(node['id'])}"
            variants, currency = [], None
            for v in ((node.get("variants") or {}).get("nodes") or [])[:MAX_VARIANTS]:
                cp = v.get("contextualPricing") or {}
                price = cp.get("price") or {}
                rule = cp.get("quantityRule") or {}
                breaks = []
                for b in ((cp.get("quantityPriceBreaks") or {}).get("nodes") or [])[:MAX_BREAKS]:
                    if b.get("minimumQuantity") and (b.get("price") or {}).get("amount") is not None:
                        breaks.append([int(b["minimumQuantity"]), _f(b["price"]["amount"])])
                breaks.sort()
                currency = currency or price.get("currencyCode")
                variants.append({"id": _legacy(v.get("id")), "title": v.get("title") or "",
                                 "sku": v.get("sku") or "", "price": _f(price.get("amount")),
                                 "compare": _f((cp.get("compareAtPrice") or {}).get("amount")),
                                 "min": rule.get("minimum") or None, "max": rule.get("maximum") or None,
                                 "step": rule.get("increment") or None, "breaks": breaks})
            out[pid] = {"title": node.get("title") or "", "published": node.get("publishedInContext") is not False,
                        "currency": currency, "variants": variants}
    return out


def real_variants(info: dict) -> list:
    """Variants a salesperson chooses between ('Default Title' = no choice)."""
    vs = (info or {}).get("variants") or []
    if len(vs) == 1:
        return vs
    return [v for v in vs if v.get("title") and v["title"] != "Default Title"] or vs


def unit_price(variant: dict, qty) -> tuple:
    """(price for this quantity, volume-break minimum used or None)."""
    if not variant or variant.get("price") is None:
        return None, None
    best, used = variant["price"], None
    try:
        q = float(qty or 0)
    except (TypeError, ValueError):
        q = 0
    for mn, p in variant.get("breaks") or []:
        if p is not None and q >= mn and p < best:
            best, used = p, mn
    return best, used


def rule_text(v: dict) -> str:
    bits = []
    if v.get("min") and v["min"] > 1:
        bits.append(f"Min {v['min']}")
    if v.get("max"):
        bits.append(f"max {v['max']}")
    if v.get("step") and v["step"] > 1:
        bits.append(f"in steps of {v['step']}")
    return " · ".join(bits)


def fix_qty(v: dict, qty, company: str = "") -> tuple:
    """(allowed quantity, why it changed or "") under the variant's Shopify
    quantity rules: up to the minimum, up to the next step, down to the maximum."""
    import math
    try:
        q = float(qty or 0)
    except (TypeError, ValueError):
        return qty, ""
    if not v or q <= 0:
        return qty, ""
    mn, mx, st = int(v.get("min") or 1), (int(v["max"]) if v.get("max") else None), int(v.get("step") or 1)
    new, why = q, []
    if new != int(new):
        new = math.ceil(new); why.append("whole units")
    if new < mn:
        new = mn; why.append(f"minimum {mn}")
    if st > 1 and new % st:
        new = math.ceil(new / st) * st; why.append(f"steps of {st}")
    if mx and new > mx:
        new = (mx // st) * st if st > 1 else mx
        why = [f"maximum {mx}"]
    new = int(new)
    if new == q:
        return qty, ""
    who = f"{company}'s" if company else "the buyer's"
    return new, f"Changed {'%g' % q} \u2192 {new} to match {who} Shopify rules ({', '.join(why)})"


def rule_problems(v: dict, qty) -> list:
    """Quantity-rule warnings for a line (Part 5 checks)."""
    out = []
    try:
        q = float(qty or 0)
    except (TypeError, ValueError):
        return out
    if not v or q <= 0:
        return out
    if v.get("min") and q < v["min"]:
        out.append(f"below the minimum of {v['min']}")
    if v.get("max") and q > v["max"]:
        out.append(f"above the maximum of {v['max']}")
    st = v.get("step")
    if st and st > 1 and q % st:
        lo = int(q // st) * st
        sug = [x for x in (lo, lo + st) if x > 0 and (not v.get("min") or x >= v["min"])]
        out.append(f"not in steps of {st}" + (f" (try {' or '.join('%g' % x for x in sug)})" if sug else ""))
    return out
