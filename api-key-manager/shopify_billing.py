"""
shopify_billing.py — 7B Shopify Billing (8 Nov 2026; plan approved by the CEO,
https://claude.ai/artifact/TSFKT334z93xW2DsN7Wo1M).

Shopify App Pricing: our 7 plans live in the Partner Dashboard, Shopify hosts
the plan page and bills the merchant. PhiXtra never creates charges; it only
READS which plan a store is on and sets the business's portal plan to match.

Truth comes from the Partner API's activeSubscription (needs a Partner API
client with "Manage apps"; settings SHOPIFY_PARTNER_ID, SHOPIFY_PARTNER_TOKEN,
SHOPIFY_APP_GID in the portal's shopify.conf). Until that client exists, the
HMAC-signed app_subscriptions/update webhook payload is used instead.

Checked: when the merchant comes back from the plan page (welcome link → app
session), on every app_subscriptions/update webhook, on uninstall, and nightly.

Never touched: businesses with an active Stripe / Paystack plan while Shopify
reports no plan, and businesses still inside PhiXtra's own trial.
`python3 shopify_billing.py nightly`
"""
import json
import time
from datetime import datetime, timezone

import requests

from db import get_db_connection

PARTNER_API_VERSION = "2026-07"

# Shopify plan handle → portal plan slug
PLAN_MAP = {
    "free": "connect",
    "retail_starter": "web_startup",
    "retail_growth": "web_business",
    "retail_scale": "web_enterprise",
    "b2b_starter": "b2b_starter",
    "b2b_growth": "b2b",
    "b2b_scale": "b2b_scale",
}
FREE_SLUG = "connect"
STALE_SECONDS = 300          # app session re-checks at most every 5 minutes

_ACTIVE_Q = """query ActiveSubscription($appId: ID!, $shopId: ID!) {
  activeSubscription(appId: $appId, shopId: $shopId) {
    shop { id myshopifyDomain }
    billingPeriod cancelAtEndOfCycle trialEndsAt
    currentBillingCycle { startTime endTime }
    items { handle description price { __typename active currency ... on FlatRatePrice { amount } } }
    pendingUpdate { billingPeriod items { handle } }
    legacySubscriptionId } }"""

_ready = False


def ensure(cur):
    global _ready
    if _ready:
        return
    for col, typ in (("shop_gid", "TEXT"), ("bill_plan", "TEXT"), ("bill_status", "TEXT"),
                     ("bill_period", "TEXT"), ("bill_trial_ends", "TIMESTAMPTZ"),
                     ("bill_cancel_at_end", "BOOLEAN"), ("bill_checked_at", "TIMESTAMPTZ"),
                     ("bill_source", "TEXT"), ("bill_error", "TEXT")):
        # Only ALTER when the column is really missing: even a no-op ALTER takes a table
        # lock that queues every page reading shopify_shops behind any open read (9 Nov 2026).
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='shopify_shops' AND column_name=%s",
                    (col,))
        if cur.fetchone():
            continue
        cur.execute(f"ALTER TABLE shopify_shops ADD COLUMN IF NOT EXISTS {col} {typ}")
    _ready = True


def _conn():
    conn = get_db_connection()
    if not _ready:
        ensure(conn.cursor())
        conn.commit()
    return conn


# ── Settings ─────────────────────────────────────────────────────────────
def _env(name):
    import shopify_app
    return shopify_app._env(name)


def partner_ready() -> bool:
    return all(_env(n) for n in ("SHOPIFY_PARTNER_ID", "SHOPIFY_PARTNER_TOKEN", "SHOPIFY_APP_GID"))


def app_handle() -> str:
    return _env("SHOPIFY_APP_HANDLE") or "phixtra"


def plan_page_url(shop: str) -> str:
    """Shopify-hosted plan selection page. Must open at the top level, not in the app iframe."""
    return f"https://admin.shopify.com/store/{shop.split('.')[0]}/charges/{app_handle()}/pricing_plans"


# ── Shopify reads ────────────────────────────────────────────────────────
def shop_gid(shop: str) -> str:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT shop_gid FROM shopify_shops WHERE shop=%s", (shop,))
        r = cur.fetchone()
        if r and r[0]:
            return r[0]
        import shopify_app
        gid = shopify_app.graphql(shop, "query { shop { id } }")["shop"]["id"]
        cur.execute("UPDATE shopify_shops SET shop_gid=%s WHERE shop=%s", (gid, shop))
        conn.commit()
        return gid
    finally:
        conn.close()


def fetch(shop: str):
    """Active subscription from the Partner API → dict, or None when the store has no plan.
    Raises on any failure (never treat an error as 'no plan')."""
    r = requests.post(f"https://partners.shopify.com/{_env('SHOPIFY_PARTNER_ID')}/api/{PARTNER_API_VERSION}/graphql.json",
                      json={"query": _ACTIVE_Q, "variables": {"appId": _env("SHOPIFY_APP_GID"), "shopId": shop_gid(shop)}},
                      headers={"X-Shopify-Access-Token": _env("SHOPIFY_PARTNER_TOKEN")}, timeout=20)
    r.raise_for_status()
    out = r.json()
    if out.get("errors"):
        raise RuntimeError(json.dumps(out["errors"])[:400])
    s = out["data"]["activeSubscription"]
    if not s:
        return None
    handles = [i["handle"] for i in s.get("items") or [] if i.get("handle") in PLAN_MAP]
    return {"handle": handles[0] if handles else None,
            "period": "annual" if s.get("billingPeriod") == "ANNUAL" else "monthly",
            "trial_ends": s.get("trialEndsAt"),
            "cancel_at_end": bool(s.get("cancelAtEndOfCycle")),
            "sub_id": s.get("legacySubscriptionId"),
            "status": "ACTIVE"}


# ── Apply to the business ────────────────────────────────────────────────
def _plan(cur, slug):
    cur.execute("SELECT id, price_dollars, COALESCE(annual_discount_pct, 0) FROM plans WHERE slug=%s", (slug,))
    return cur.fetchone()


def _date(v):
    return v.date() if isinstance(v, datetime) else v


def apply(shop: str, sub, source: str) -> str:
    """sub = dict from fetch() / webhook, or None (no plan). Returns what happened."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT tenant_id, uninstalled_at FROM shopify_shops WHERE shop=%s", (shop,))
        row = cur.fetchone()
        if not row:
            return "unknown_shop"
        tid = row[0]
        # Shopify suggests handles with dashes (retail-starter); accept either.
        if sub and sub.get("handle"):
            sub = dict(sub, handle=str(sub["handle"]).strip().lower().replace("-", "_"))
        active = bool(sub and sub.get("status") == "ACTIVE" and sub.get("handle") in PLAN_MAP)
        handle = sub["handle"] if active else None
        cur.execute("""UPDATE shopify_shops SET bill_plan=%s, bill_status=%s, bill_period=%s, bill_trial_ends=%s,
                              bill_cancel_at_end=%s, bill_checked_at=NOW(), bill_source=%s, bill_error=NULL
                       WHERE shop=%s""",
                    (handle, (sub or {}).get("status") or "NONE", (sub or {}).get("period"),
                     (sub or {}).get("trial_ends") if active else None,
                     bool((sub or {}).get("cancel_at_end")), source, shop))
        conn.commit()
        if not tid:
            return "no_business"
        slug = PLAN_MAP[handle] if active else FREE_SLUG
        cycle = (sub.get("period") or "monthly") if active else "monthly"
        p = _plan(cur, slug)
        if not p:
            return f"plan_missing:{slug}"
        plan_id, price, disc = p
        cur.execute("""SELECT t.plan_id, t.billing_cycle, t.trial_ends_at,
                              (SELECT payment_provider FROM plan_subscriptions s WHERE s.tenant_id=t.id AND s.status='active'
                                ORDER BY s.id DESC LIMIT 1)
                         FROM tenants t WHERE t.id=%s""", (tid,))
        cur_plan, cur_cycle, trial_ends, provider = cur.fetchone()
        if not active:
            if provider and provider != "shopify":
                return f"kept_{provider}"           # paying another way — leave it
            if trial_ends and _date(trial_ends) >= datetime.now(timezone.utc).date():
                return "kept_trial"                 # PhiXtra's own trial still running
        if cur_plan == plan_id and (cur_cycle or "monthly") == cycle and (provider == "shopify" or (not active and not provider)):
            return "unchanged"
    finally:
        conn.close()
    import portal_routes as pr
    # Same yearly discount as the website and portal (e.g. B2B Scale 20%).
    amount = 0 if slug == FREE_SLUG else (round(float(price or 0), 2) if cycle == "monthly"
                                          else pr._yearly_per_month(price, disc) * 12)   # same as portal + Shopify plan
    pr._activate_plan_subscription(tid, plan_id, cycle, "USD", "shopify",
                                   (sub or {}).get("sub_id"), shop,
                                   f"shopify:{shop}:{slug}:{cycle}:{int(time.time())}", amount)
    pr.insert_audit_log(action="shopify_plan_applied", tenant_id=tid,
                        details={"shop": shop, "plan": slug, "cycle": cycle, "source": source,
                                 "shopify_plan": handle or "none"})
    return f"applied:{slug}:{cycle}"


def check(shop: str, source: str = "check", force: bool = False) -> str:
    """Ask Shopify (Partner API) and apply. Skips if checked in the last 5 minutes unless forced."""
    if not partner_ready():
        return "partner_api_not_set_up"
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT bill_checked_at, uninstalled_at FROM shopify_shops WHERE shop=%s", (shop,))
        r = cur.fetchone()
    finally:
        conn.close()
    if not r:
        return "unknown_shop"
    if not force and r[0] and (datetime.now(timezone.utc) - r[0]).total_seconds() < STALE_SECONDS:
        return "fresh"
    try:
        return apply(shop, fetch(shop), source)
    except Exception as e:
        conn = _conn()
        try:
            conn.cursor().execute("UPDATE shopify_shops SET bill_error=%s WHERE shop=%s", (str(e)[:300], shop))
            conn.commit()
        finally:
            conn.close()
        print(time.strftime("%F %T"), f"shopify billing check {shop}: {e}")
        return "error"


def on_webhook(shop: str, payload: dict) -> str:
    """app_subscriptions/update. With the Partner API: re-check (truth). Without: use the signed payload."""
    if partner_ready():
        return check(shop, "webhook", force=True)
    s = payload.get("app_subscription") or {}
    if (s.get("status") or "").upper() in ("PENDING", "ACCEPTED"):
        return "pending"                        # merchant hasn't approved yet — nothing changes
    sub = {"status": (s.get("status") or "").upper(), "handle": s.get("plan_handle"),
           "period": "annual" if "annual" in (s.get("interval") or "") else "monthly",
           "sub_id": s.get("admin_graphql_api_id"), "trial_ends": None, "cancel_at_end": False}
    return apply(shop, sub, "webhook_payload")


def on_uninstall(shop: str) -> str:
    """Shopify stops charging on uninstall; the business goes to Free."""
    return apply(shop, None, "uninstall")


def state(row) -> dict:
    """For the Shopify Home screen."""
    trial_days = None
    if row.get("bill_trial_ends"):
        left = (row["bill_trial_ends"] - datetime.now(timezone.utc)).total_seconds()
        trial_days = max(0, int(left // 86400) + (1 if left % 86400 else 0)) if left > 0 else None
    return {"plan": row.get("bill_plan"), "status": row.get("bill_status"), "period": row.get("bill_period"),
            "trial_days_left": trial_days, "cancel_at_end": bool(row.get("bill_cancel_at_end")),
            "plan_page": plan_page_url(row["shop"])}


def _year_price(price, disc) -> int:
    """Yearly charge = yearly-per-month rounded half up × 12, same as the portal and website."""
    from decimal import Decimal, ROUND_HALF_UP
    v = Decimal(str(price)) * (Decimal(100) - Decimal(str(disc or 0))) / Decimal(100)
    return int(v.quantize(Decimal(1), rounding=ROUND_HALF_UP)) * 12


def card(tenant_id: int, row=None) -> dict | None:
    """What a Shopify-connected business is on, for the Shopify Home 'Your plan'
    card and the portal's 'Billed through Shopify' card (7B screens, 9 Nov 2026).
    kind: free | shop_trial | active | cancelling | frozen | phx_trial | other_paid.
    None when the business isn't connected to a Shopify store."""
    conn = _conn()
    try:
        cur = conn.cursor()
        if row is None:
            cur.execute("""SELECT shop, bill_plan, bill_status, bill_period, bill_trial_ends, bill_cancel_at_end
                             FROM shopify_shops WHERE tenant_id=%s AND uninstalled_at IS NULL
                            ORDER BY installed_at DESC NULLS LAST LIMIT 1""", (tenant_id,))
            r = cur.fetchone()
            if not r:
                return None
            row = dict(zip(("shop", "bill_plan", "bill_status", "bill_period", "bill_trial_ends",
                            "bill_cancel_at_end"), r))
        cur.execute("""SELECT p.name, p.slug, COALESCE(p.price_dollars, 0), COALESCE(p.annual_discount_pct, 0),
                              t.trial_ends_at,
                              (SELECT payment_provider FROM plan_subscriptions s WHERE s.tenant_id=t.id
                                  AND s.status='active' ORDER BY s.id DESC LIMIT 1)
                         FROM tenants t LEFT JOIN plans p ON p.id=t.plan_id WHERE t.id=%s""", (tenant_id,))
        r = cur.fetchone()
    finally:
        conn.close()
    if not r:
        return None
    name, slug, price, disc, trial_ends, provider = r
    st = state(row)
    yearly = (row.get("bill_period") or "") == "annual"
    price = float(price or 0)
    out = {"shop": row["shop"], "plan_page": st["plan_page"], "plan_name": name or "Free",
           "price": round(price), "yearly": yearly,
           "year_price": _year_price(price, disc) if yearly else None,
           "days_left": None, "kind": "free"}
    status = (row.get("bill_status") or "").upper()
    on_free = (slug or FREE_SLUG) == FREE_SLUG or price <= 0
    if status == "FROZEN":
        out["kind"] = "frozen"
    elif on_free and not (trial_ends and _date(trial_ends) >= datetime.now(timezone.utc).date()):
        out.update(kind="free", plan_name="Free")     # incl. Shopify's $0 "free" plan
    elif row.get("bill_plan") and status == "ACTIVE":
        if st["trial_days_left"]:
            out.update(kind="shop_trial", days_left=st["trial_days_left"])
        elif st["cancel_at_end"]:
            out["kind"] = "cancelling"
        else:
            out["kind"] = "active"
    elif provider and provider != "shopify":
        out["kind"] = "other_paid"
    elif trial_ends and _date(trial_ends) >= datetime.now(timezone.utc).date():
        left = (_date(trial_ends) - datetime.now(timezone.utc).date()).days + 1
        out.update(kind="phx_trial", days_left=left)
    elif slug and slug != FREE_SLUG and price > 0:
        out["kind"] = "other_paid"      # plan set in Admin
    else:
        out["plan_name"] = "Free"
    return out


def nightly():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT shop FROM shopify_shops WHERE tenant_id IS NOT NULL AND uninstalled_at IS NULL")
        shops = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    for s in shops:
        print(time.strftime("%F %T"), s, check(s, "nightly", force=True))


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    {"nightly": nightly}.get((sys.argv[1:] or ["nightly"])[0], nightly)()
