"""Discounts in Chat (Phase 1, 11 Nov 2026).

The AI Agent may give a buyer ONE single-use discount code in the website
chat, only inside the limits the business sets on AI Agent › Discounts
in Chat. Phase 1 = the rules page + codes made in the business's Shopify
store (discountCodeBasicCreate, needs the write_discounts permission).

Who gets it: Shopify businesses on a paid Retail plan (plan grant
ai.discounts_view). Never B2B businesses (trade buyers get a quotation),
never the Free plan. Off until the business switches it on.

The AI backend asks the portal (server to server, PHIXTRA_INTERNAL_TOKEN):
  /shopify/internal/discount/brief  → the rules line for the AI's instruction
  /shopify/internal/discount/make   → make the code when the AI offers one
The AI writes <<<PHIXTRA_DISCOUNT>>> where the code goes; the backend
swaps it for the real code. Every rule is checked again here when the code
is made (on, permission, plan, B2B, one per buyer, codes per day).
"""

import json
import secrets
from datetime import datetime, timedelta, timezone

import psycopg2.extras

from db import get_db_connection

FEATURE_VIEW = "ai.discounts_view"
FEATURE_EDIT = "ai.discounts_edit"
FEATURE_SEND = "ai.discounts_send"     # Phase 2: staff send a code from the Inbox
GIVEN = ("active", "used", "expired")   # a code the buyer really got
RETAIL_SLUGS = ("starter", "growth", "pro", "startup_dual", "business_dual", "enterprise_dual",
                "custom", "web_startup", "web_business", "web_enterprise")
SCOPE = "write_discounts"
PREFIX = "PHX-"
TAG = "<<<PHIXTRA_DISCOUNT>>>"
HOURS = (24, 48, 168)
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L

DEFAULTS = {"enabled": False, "kind": "percent", "value": 5, "min_order": None, "trigger": "asked",
            "hours": 48, "products": "all", "collections": [], "daily_max": 20, "note": ""}


# ── Tables ──────────────────────────────────────────────────────────────
def ensure_tables():
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_discount_settings (
                tenant_id   INTEGER PRIMARY KEY,
                enabled     BOOLEAN NOT NULL DEFAULT FALSE,
                kind        TEXT NOT NULL DEFAULT 'percent',
                value       NUMERIC(10,2) NOT NULL DEFAULT 5,
                min_order   NUMERIC(12,2),
                trigger     TEXT NOT NULL DEFAULT 'asked',
                hours       INTEGER NOT NULL DEFAULT 48,
                products    TEXT NOT NULL DEFAULT 'all',
                collections JSONB NOT NULL DEFAULT '[]',
                daily_max   INTEGER NOT NULL DEFAULT 20,
                note        TEXT NOT NULL DEFAULT '',
                updated_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_by  TEXT
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_discount_codes (
                id          SERIAL PRIMARY KEY,
                tenant_id   INTEGER NOT NULL,
                shop        TEXT,
                session_id  TEXT NOT NULL,
                buyer_email TEXT,
                code        TEXT NOT NULL,
                shopify_id  TEXT,
                kind        TEXT NOT NULL,
                value       NUMERIC(10,2) NOT NULL,
                currency    TEXT,
                min_order   NUMERIC(12,2),
                ends_at     TIMESTAMPTZ NOT NULL,
                status      TEXT NOT NULL DEFAULT 'making',
                error       TEXT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS chat_discount_codes_tenant ON chat_discount_codes(tenant_id, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS chat_discount_codes_session ON chat_discount_codes(tenant_id, session_id)")
        # Phase 2 (11 Nov 2026): who gave it, the buyer's lead, Used / Expired.
        cur.execute("""SELECT column_name FROM information_schema.columns WHERE table_name='chat_discount_codes'""")
        have = {r[0] for r in cur.fetchall()}
        for col, typ in (("given_by", "TEXT NOT NULL DEFAULT 'ai'"), ("given_by_name", "TEXT"),
                         ("lead_id", "INTEGER"), ("used_at", "TIMESTAMPTZ"), ("checked_at", "TIMESTAMPTZ"),
                         # Phase 4: 'shopify' or 'woo' (shopify_id then holds the WooCommerce coupon id)
                         ("platform", "TEXT NOT NULL DEFAULT 'shopify'")):
            if col not in have:
                cur.execute(f"ALTER TABLE chat_discount_codes ADD COLUMN {col} {typ}")
        cur.execute("CREATE INDEX IF NOT EXISTS chat_discount_codes_lead ON chat_discount_codes(lead_id) WHERE lead_id IS NOT NULL")
        # Plan grant: every Retail plan (incl. Custom), not Free, not B2B.
        # Granted once; the admin Plan editor decides after that.
        from portal_migrations import _GRANT_ONCE_SQL
        cur.execute("SELECT id FROM plans WHERE slug = ANY(%s)", (list(RETAIL_SLUGS),))
        for (pid,) in cur.fetchall():
            for k in (FEATURE_VIEW, FEATURE_EDIT, FEATURE_SEND):
                cur.execute(_GRANT_ONCE_SQL, (pid, k))
        for k in (FEATURE_VIEW, FEATURE_EDIT, FEATURE_SEND):
            cur.execute("INSERT INTO feature_catalog_seen (feature_key) VALUES (%s) ON CONFLICT DO NOTHING", (k,))
        conn.commit()
    finally:
        cur.close(); conn.close()


# ── Settings ────────────────────────────────────────────────────────────
def get_settings(tenant_id: int) -> dict:
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM chat_discount_settings WHERE tenant_id=%s", (int(tenant_id),))
        r = cur.fetchone()
    finally:
        cur.close(); conn.close()
    s = dict(DEFAULTS)
    if r:
        for k in DEFAULTS:
            s[k] = r[k]
        s["value"] = _num(r["value"])
        s["min_order"] = _num(r["min_order"]) if r["min_order"] is not None else None
        s["collections"] = r["collections"] if isinstance(r["collections"], list) else json.loads(r["collections"] or "[]")
    return s


def _num(v):
    f = float(v)
    return int(f) if f == int(f) else round(f, 2)


def parse_form(form, collections_known: list, platform: str = "shopify") -> tuple:
    """(settings, error) from the page's form. error = wording to show."""
    s = {}
    s["enabled"] = form.get("enabled") == "1"
    s["kind"] = "amount" if form.get("kind") == "amount" else "percent"
    try:
        s["value"] = round(float((form.get("value") or "").replace(",", "").strip()), 2)
    except ValueError:
        return None, "Please enter the most your AI Agent can give as a number."
    if s["value"] <= 0:
        return None, "The most your AI Agent can give must be more than 0."
    if s["kind"] == "percent" and s["value"] > 50:
        return None, "The most your AI Agent can give is 50% off."
    if s["kind"] == "amount" and s["value"] > 100000:
        return None, "That fixed amount is too high."
    mo = (form.get("min_order") or "").replace(",", "").strip()
    if mo:
        try:
            s["min_order"] = round(float(mo), 2)
        except ValueError:
            return None, "Please enter the minimum order as a number, or leave it empty."
        if s["min_order"] <= 0:
            s["min_order"] = None
    else:
        s["min_order"] = None
    s["trigger"] = "hesitate" if form.get("trigger") == "hesitate" else "asked"
    try:
        s["hours"] = int(form.get("hours") or 48)
    except ValueError:
        s["hours"] = 48
    if s["hours"] not in HOURS:
        s["hours"] = 48
    s["products"] = "collections" if form.get("products") == "collections" else "all"
    known = {c["id"]: c["title"] for c in (collections_known or [])}
    picked = [i for i in form.getlist("collections") if i in known][:50]
    s["collections"] = [{"id": i, "title": known[i]} for i in picked]
    if s["products"] == "collections" and not s["collections"]:
        return None, f"Please pick at least one {words(platform)['group']}, or choose All products."
    try:
        s["daily_max"] = int(form.get("daily_max") or 20)
    except ValueError:
        return None, "Please enter the most codes per day as a whole number."
    if not 1 <= s["daily_max"] <= 500:
        return None, "Most codes per day must be between 1 and 500."
    s["note"] = " ".join((form.get("note") or "").split())[:120]
    return s, None


def save_settings(tenant_id: int, s: dict, who: str = None):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO chat_discount_settings (tenant_id, enabled, kind, value, min_order, trigger, hours,
                                                products, collections, daily_max, note, updated_at, updated_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
            ON CONFLICT (tenant_id) DO UPDATE SET enabled=EXCLUDED.enabled, kind=EXCLUDED.kind,
                value=EXCLUDED.value, min_order=EXCLUDED.min_order, trigger=EXCLUDED.trigger,
                hours=EXCLUDED.hours, products=EXCLUDED.products, collections=EXCLUDED.collections,
                daily_max=EXCLUDED.daily_max, note=EXCLUDED.note, updated_at=NOW(), updated_by=EXCLUDED.updated_by""",
            (int(tenant_id), s["enabled"], s["kind"], s["value"], s["min_order"], s["trigger"], s["hours"],
             s["products"], json.dumps(s["collections"]), s["daily_max"], s["note"], who))
        conn.commit()
    finally:
        cur.close(); conn.close()


# ── Who can use it ──────────────────────────────────────────────────────
def _shop(tenant_id: int):
    """The business's store: a Shopify store ({platform: 'shopify', shop, scope,
    currency}) or, Phase 4, a WooCommerce shop with the PhiXtra plugin
    ({platform: 'woo', shop: its address, currency, coupons, route, key})."""
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT shop, scope, currency FROM shopify_shops
                        WHERE tenant_id=%s AND uninstalled_at IS NULL
                        ORDER BY linked_at DESC NULLS LAST LIMIT 1""", (int(tenant_id),))
        r = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if r:
        return dict(r, platform="shopify")
    try:
        import woo_orders
        w = woo_orders.shop_for(tenant_id)
        if w:
            from urllib.parse import urlparse
            return {"platform": "woo", "shop": (urlparse(w.get("site") or "").hostname or w.get("site") or "")[:200],
                    "currency": w.get("currency") or "", "coupons": bool(w.get("coupons")),
                    "route": w["route"], "key": w["api_key_plain"], "plugin": w.get("plugin") or ""}
        if woo_orders.has_shop(tenant_id):
            # A WooCommerce shop whose plugin hasn't said hello yet (older plugin).
            return {"platform": "woo", "shop": "", "currency": "", "coupons": False, "route": "", "key": ""}
    except Exception as e:
        print(f"⚠️ chat_discounts woo shop {tenant_id}: {e}")
    return None


def has_scope(shop_row) -> bool:
    """Shopify: the write_discounts permission. WooCommerce: a plugin with the chat-coupon route."""
    if not shop_row:
        return False
    if shop_row.get("platform") == "woo":
        return bool(shop_row.get("coupons")) and bool(_woo_route(shop_row))
    return SCOPE in (shop_row.get("scope") or "").split(",")


# ── WooCommerce (Phase 4): signed calls to the plugin's chat-coupon route ─
def _woo_route(row) -> str:
    import woo_orders
    route = (row or {}).get("route") or ""
    if not route or not woo_orders._safe_route(route):
        return ""
    return route.replace("phixtra/v1/create-order", "phixtra/v1/chat-coupon")


def _woo_call(row, payload: dict, timeout: int = 15) -> dict:
    import requests, woo_orders
    route = _woo_route(row)
    if not route:
        raise RuntimeError("no_route")
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", **woo_orders.sign(row["key"], body)}
    r = requests.post(route, data=body, headers=headers, timeout=timeout, allow_redirects=False)
    if r.status_code == 404:
        raise RuntimeError("update_plugin")
    if r.status_code in (401, 403):
        raise RuntimeError("refused")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"http_{r.status_code}")


def words(platform: str) -> dict:
    """Store words for the screens."""
    if platform == "woo":
        return {"store": "WooCommerce", "find": "WooCommerce › Marketing › Coupons",
                "groups": "categories", "group": "category", "Groups": "Categories"}
    return {"store": "Shopify", "find": "Shopify › Discounts",
            "groups": "collections", "group": "collection", "Groups": "Collections"}


def _is_b2b(cur, tenant_id: int) -> bool:
    cur.execute("SELECT COALESCE(is_b2b, FALSE) AS b2b FROM company_profiles WHERE tenant_id=%s", (int(tenant_id),))
    r = cur.fetchone()
    if not r:
        return False
    # 13 Nov 2026: works with both plain and dict cursors (the chat path uses a dict cursor).
    return bool(r["b2b"] if isinstance(r, dict) else r[0])


def _plan_ok(cur, tenant_id: int) -> bool:
    cur.execute("""SELECT 1 FROM tenants t JOIN plan_feature_grants g ON g.plan_id = t.plan_id
                    WHERE t.id=%s AND g.feature_key=%s""", (int(tenant_id), FEATURE_VIEW))
    return cur.fetchone() is not None


def page_state(tenant_id: int, plan_ok: bool) -> tuple:
    """('no_shop' | 'b2b' | 'free' | 'permission' | 'ok', shop_row)."""
    row = _shop(tenant_id)
    if not row:
        return "no_shop", None
    conn = get_db_connection(); cur = conn.cursor()
    try:
        b2b = _is_b2b(cur, tenant_id)
    finally:
        cur.close(); conn.close()
    if b2b:
        return "b2b", row
    if not plan_ok:
        return "free", row
    if row.get("platform") == "woo":
        return ("ok" if has_scope(row) else "update"), row
    if not has_scope(row):
        # The owner may have approved it since the token was saved: ask Shopify.
        try:
            import shopify_app
            shopify_app.refresh_shop_details(row["shop"])
            row = _shop(tenant_id)
        except Exception as e:
            print(f"⚠️ chat_discounts scope check {row['shop']}: {e}")
        if not has_scope(row):
            return "permission", row
    return "ok", row


def _ready(cur, tenant_id: int):
    """(settings, shop_row) when the AI Agent may offer codes, else (None, None)."""
    s = get_settings(tenant_id)
    if not s["enabled"]:
        return None, None
    row = _shop(tenant_id)
    if not has_scope(row) or _is_b2b(cur, tenant_id) or not _plan_ok(cur, tenant_id):
        return None, None
    return s, row


# ── Collections (for "Only these collections") ──────────────────────────
_COLLECTIONS_Q = "{ collections(first: 100, sortKey: TITLE) { nodes { id title } } }"


def store_collections(shop) -> list:
    """Shopify collections, or WooCommerce product categories (Phase 4).
    `shop` is the store row (or a Shopify shop name, as before)."""
    if isinstance(shop, dict) and shop.get("platform") == "woo":
        try:
            d = _woo_call(shop, {"action": "categories"})
        except Exception as e:
            print(f"⚠️ chat_discounts woo categories {shop.get('shop')}: {e}")
            return []
        return [{"id": str(c["id"]), "title": str(c["title"])[:120]} for c in (d.get("categories") or [])
                if c.get("id") and c.get("title")]
    if isinstance(shop, dict):
        shop = shop["shop"]
    import shopify_app
    try:
        d = shopify_app.graphql(shop, _COLLECTIONS_Q)
    except Exception as e:
        print(f"⚠️ chat_discounts collections {shop}: {e}")
        return []
    return [{"id": n["id"], "title": n["title"]} for n in ((d.get("collections") or {}).get("nodes") or [])
            if n.get("id") and n.get("title")]


# ── Words ───────────────────────────────────────────────────────────────
_SYM = {"USD": "$", "GBP": "£", "EUR": "€", "NGN": "₦", "CAD": "CA$", "AUD": "A$"}


def money(v, cur) -> str:
    v = float(v)
    txt = f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    sym = _SYM.get((cur or "").upper())
    return f"{sym}{txt}" if sym else f"{txt} {cur or ''}".strip()


def value_text(kind, value, cur) -> str:
    return f"{_num(value)}%" if kind == "percent" else money(value, cur)


def _tz(tenant_id: int):
    from zoneinfo import ZoneInfo
    try:
        import staff_activity
        return ZoneInfo(staff_activity.tenant_tz_name(int(tenant_id)))
    except Exception:
        return ZoneInfo("Europe/London")


def ends_text(tenant_id: int, ends_at) -> str:
    t = ends_at.astimezone(_tz(tenant_id))
    return t.strftime("%a %-d %b, %H:%M")


# ── Codes ───────────────────────────────────────────────────────────────
def _buyer_code(cur, tenant_id: int, session_id: str, email: str):
    cur.execute("""SELECT code, kind, value, currency, min_order, ends_at, status FROM chat_discount_codes
                    WHERE tenant_id=%s AND status = ANY(%s)
                      AND (session_id=%s OR (%s <> '' AND lower(buyer_email)=lower(%s)))
                    ORDER BY id DESC LIMIT 1""",
                (int(tenant_id), list(GIVEN), session_id, email or "", email or ""))
    return cur.fetchone()


def _made_today(cur, tenant_id: int) -> int:
    tz = _tz(tenant_id)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    cur.execute("""SELECT COUNT(*) AS n FROM chat_discount_codes
                    WHERE tenant_id=%s AND created_at >= %s
                      AND (status = ANY(%s) OR (status='making' AND created_at > NOW() - INTERVAL '5 minutes'))""",
                (int(tenant_id), start, list(GIVEN)))
    return int(cur.fetchone()["n"])


def brief(tenant_id: int, session_id: str, email: str = "") -> str:
    """The DISCOUNTS rules for this chat's AI instruction ('' = say nothing,
    the AI's normal rules apply: it never invents discounts)."""
    if not session_id:
        return ""
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        s, row = _ready(cur, tenant_id)
        if not s:
            return ""
        have = _buyer_code(cur, tenant_id, session_id, email)
        if have:
            v = value_text(have["kind"], have["value"], have["currency"])
            if have["status"] == "used":
                return ("\n\nDISCOUNTS: This buyer already used their single-use discount code " + have["code"]
                        + ". Do not offer another discount. If they ask, say you can't offer another discount and ask "
                        "if they would like you to ask a member of the team.")
            if have["status"] == "active" and have["ends_at"] > datetime.now(timezone.utc):
                return ("\n\nDISCOUNT CODE: This buyer already has the single-use code " + have["code"] + f" ({v} off"
                        + (f" orders of {money(have['min_order'], have['currency'])} or more" if have["min_order"] else "")
                        + f", works until {ends_text(tenant_id, have['ends_at'])}). Remind them of it if it helps. "
                        "Never make or promise another code or a bigger discount. If they ask for more, say "
                        f"{v} off is the best you can offer, that their code still works, and ask if they would like "
                        "you to ask a member of the team.")
            return ("\n\nDISCOUNTS: This buyer already had a discount code and it has ended. Do not offer another "
                    "discount. If they ask, say you can't offer another discount and ask if they would like you to "
                    "ask a member of the team.")
        if _made_today(cur, tenant_id) >= s["daily_max"]:
            return ("\n\nDISCOUNTS: You cannot give any discount code today. If the buyer asks for a discount, say "
                    "you can't offer one right now and ask if they would like you to ask a member of the team.")
    finally:
        cur.close(); conn.close()
    cur_code = row.get("currency") or ""
    v = value_text(s["kind"], s["value"], cur_code)
    when = ("ONLY when the buyer asks for a discount, a coupon, a deal or a better price. Never bring it up yourself."
            if s["trigger"] == "asked" else
            "when the buyer asks for a discount, a coupon, a deal or a better price, or clearly hesitates over the "
            "price (for example says it is too expensive or that they will think about it). Never offer it "
            "unprompted otherwise.")
    lasts = {24: "24 hours", 48: "48 hours", 168: "7 days"}[s["hours"]]
    terms = [f"{v} off", "works once", f"lasts {lasts}"]
    if s["min_order"]:
        terms.append(f"only on orders of {money(s['min_order'], cur_code)} or more")
    if s["products"] == "collections" and s["collections"]:
        terms.append("only on products in: " + ", ".join(c["title"] for c in s["collections"]))
    line = ("\n\nDISCOUNTS: You may give this buyer ONE single-use discount code (" + "; ".join(terms) + "). "
            f"Offer it {when} "
            f"{v} off is the most you can ever give: never offer, promise or hint at more, never make up a code, "
            "and never give a discount any other way. To give the code, write one short sentence in the buyer's "
            f"language such as 'I can offer you {v} off today. Use this code at checkout:' and put exactly {TAG} "
            "where the code goes. The real code is put there for you, and its end time is shown with it. "
            "Use the tag at most once in this chat. If the buyer asks for more than this, say "
            f"{v} off is the best you can offer and ask if they would like you to ask a member of the team.")
    if s["note"]:
        line += f" The business's note about discounts (it never raises the limit): {s['note']}"
    return line


def _new_code() -> str:
    return PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(6))


_CREATE = """mutation($d: DiscountCodeBasicInput!) { discountCodeBasicCreate(basicCodeDiscount: $d) {
  codeDiscountNode { id } userErrors { field message code } } }"""


def make(tenant_id: int, session_id: str, email: str = "", given_by: str = "ai", given_by_name: str = None) -> dict:
    """Make this buyer's code in Shopify. {"ok": True, code, value_text, ends, ...}
    or {"ok": False, "why": ...}. All rules are checked again here."""
    if not session_id:
        return {"ok": False, "why": "no_session"}
    tenant_id = int(tenant_id)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        s, row = _ready(cur, tenant_id)
        if not s:
            return {"ok": False, "why": "off"}
        # One at a time per business, so two chats can't both pass the limits.
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (771100, tenant_id))
        have = _buyer_code(cur, tenant_id, session_id, email)
        if have:
            conn.rollback()
            return {"ok": False, "why": "has_code"}
        cur.execute("""SELECT 1 FROM chat_discount_codes WHERE tenant_id=%s AND session_id=%s
                         AND (status = ANY(%s) OR (status='making' AND created_at > NOW() - INTERVAL '5 minutes'))
                        LIMIT 1""", (tenant_id, session_id, list(GIVEN)))
        if cur.fetchone():
            conn.rollback()
            return {"ok": False, "why": "has_code"}
        if _made_today(cur, tenant_id) >= s["daily_max"]:
            conn.rollback()
            return {"ok": False, "why": "daily_max"}
        ends = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=s["hours"])
        code = _new_code()
        cur.execute("""INSERT INTO chat_discount_codes (tenant_id, shop, session_id, buyer_email, code, kind, value,
                                                         currency, min_order, ends_at, status, given_by, given_by_name, platform)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'making',%s,%s,%s) RETURNING id""",
                    (tenant_id, row["shop"], session_id[:80], (email or "")[:200] or None, code, s["kind"],
                     s["value"], row.get("currency"), s["min_order"], ends,
                     "staff" if given_by == "staff" else "ai", (given_by_name or "")[:120] or None,
                     row.get("platform") or "shopify"))
        rid = cur.fetchone()["id"]
        conn.commit()   # the reservation counts towards the limits from now
    finally:
        cur.close(); conn.close()

    if row.get("platform") == "woo":
        node_id, err, code = _woo_make(row, s, code, ends)
        return _made(tenant_id, rid, row, s, code, ends, node_id, err)

    import shopify_app
    gets = ({"percentage": round(float(s["value"]) / 100.0, 4)} if s["kind"] == "percent"
            else {"discountAmount": {"amount": f"{float(s['value']):.2f}", "appliesOnEachItem": False}})
    items = ({"collections": {"add": [c["id"] for c in s["collections"]]}}
             if s["products"] == "collections" and s["collections"] else {"all": True})
    d = {"title": f"PhiXtra chat {code}", "code": code,
         "startsAt": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
         "endsAt": ends.isoformat(), "context": {"all": "ALL"},
         "customerGets": {"value": gets, "items": items},
         "usageLimit": 1, "appliesOncePerCustomer": True,
         "combinesWith": {"orderDiscounts": False, "productDiscounts": False, "shippingDiscounts": True}}
    if s["min_order"]:
        d["minimumRequirement"] = {"subtotal": {"greaterThanOrEqualToSubtotal": f"{float(s['min_order']):.2f}"}}
    node_id, err = None, None
    for _ in range(3):
        try:
            out = shopify_app.graphql(row["shop"], _CREATE, {"d": d})["discountCodeBasicCreate"]
        except Exception as e:
            err = str(e)[:500]
            break
        ue = out.get("userErrors") or []
        if not ue and (out.get("codeDiscountNode") or {}).get("id"):
            node_id = out["codeDiscountNode"]["id"]
            break
        err = json.dumps(ue)[:500]
        if any("code" in (",".join(u.get("field") or [])).lower() and "taken" in (u.get("message") or "").lower()
               for u in ue):
            code = _new_code(); d["code"] = code; d["title"] = f"PhiXtra chat {code}"
            continue
        break
    return _made(tenant_id, rid, row, s, code, ends, node_id, err)


def _woo_make(row, s, code, ends):
    """(coupon id or None, error, code) — the coupon made in the WooCommerce shop."""
    err = None
    for _ in range(3):
        payload = {"action": "make", "code": code, "kind": s["kind"], "value": float(s["value"]),
                   "ends_at": ends.isoformat(), "min_order": float(s["min_order"]) if s["min_order"] else 0,
                   "categories": [c["id"] for c in s["collections"]] if s["products"] == "collections" else []}
        try:
            res = _woo_call(row, payload, timeout=20)
        except Exception as e:
            return None, str(e)[:500], code
        if res.get("ok") and res.get("id"):
            return str(res["id"]), None, code
        err = str(res.get("error") or "not made")[:500]
        if res.get("error") == "taken":
            code = _new_code()
            continue
        break
    return None, err, code


def _made(tenant_id, rid, row, s, code, ends, node_id, err) -> dict:
    """Save how making the code went; the answer for the chat."""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        if node_id:
            cur.execute("UPDATE chat_discount_codes SET status='active', code=%s, shopify_id=%s WHERE id=%s",
                        (code, node_id, rid))
        else:
            cur.execute("UPDATE chat_discount_codes SET status='failed', error=%s WHERE id=%s", (err, rid))
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not node_id:
        print(f"⚠️ chat_discounts make tenant={tenant_id} {row['shop']}: {err}")
        return {"ok": False, "why": "shopify"}
    try:
        link_lead(rid)
    except Exception as e:
        print(f"⚠️ chat_discounts lead link {rid}: {e}")
    return {"ok": True, "code": code, "kind": s["kind"], "value": _num(s["value"]),
            "value_text": value_text(s["kind"], s["value"], row.get("currency")),
            "min_order_text": money(s["min_order"], row.get("currency")) if s["min_order"] else "",
            "ends_at": ends.isoformat(), "ends_text": ends_text(tenant_id, ends)}


# ═══════════════════════════════════════════════════════════════════════
# Phase 2 (11 Nov 2026): follow-through — the buyer's lead, Used / Expired,
# the "Codes given" list, the Inbox and the lead page.
# ═══════════════════════════════════════════════════════════════════════

def _given_words(r) -> str:
    return "AI Agent" if (r.get("given_by") or "ai") == "ai" else (r.get("given_by_name") or "your team")


def _history(cur, lead_id: int, note: str, by: str):
    """A History line on the lead (same stage = a note, the pattern quotes use)."""
    cur.execute("SELECT stage FROM merchant_pipeline_leads WHERE id=%s", (lead_id,))
    st = (cur.fetchone() or {}).get("stage")
    cur.execute("""INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                   VALUES (%s,%s,%s,%s,%s)""", (lead_id, st, st, (by or "")[:200], note[:2000]))


def _chat_email(cur, tenant_id: int, session_id: str) -> str:
    """The email the buyer gave in this chat (hand-over form or logged-in store visitor)."""
    import data_crypto
    for q in ("""SELECT visitor_email AS e FROM handoff_requests WHERE tenant_id=%s AND session_id=%s
                   AND COALESCE(visitor_email,'') <> '' ORDER BY id DESC LIMIT 1""",
              """SELECT email AS e FROM chat_visitors WHERE tenant_id=%s AND session_id=%s
                   AND COALESCE(email,'') <> '' LIMIT 1"""):
        try:
            cur.execute("SAVEPOINT ce")
            cur.execute(q, (tenant_id, session_id))
            r = cur.fetchone()
            cur.execute("RELEASE SAVEPOINT ce")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT ce")
            r = None
        e = (data_crypto.dec((r or {}).get("e")) or "").strip()
        if "@" in e:
            return e[:200]
    return ""


def link_lead(code_id: int):
    """Link a code to the buyer's lead: the chat already belongs to a lead, or
    the buyer's email matches an open lead. Never makes a lead. Writes the
    'given' History line once."""
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM chat_discount_codes WHERE id=%s", (int(code_id),))
        c = cur.fetchone()
        if not c or c["lead_id"] or c["status"] not in GIVEN:
            return c and c["lead_id"]
        tid = c["tenant_id"]
        lead_id = None
        cur.execute("SELECT to_regclass('lead_qual_sources') IS NOT NULL AS ok")
        if cur.fetchone()["ok"]:
            cur.execute("""SELECT q.lead_id FROM lead_qual_sources q JOIN merchant_pipeline_leads l ON l.id=q.lead_id
                            WHERE q.tenant_id=%s AND q.source_key=%s AND l.tenant_id=%s""",
                        (tid, "chat:" + c["session_id"], tid))
            r = cur.fetchone()
            lead_id = r and r["lead_id"]
        email = c["buyer_email"] or _chat_email(cur, tid, c["session_id"])
        if email and not c["buyer_email"]:
            cur.execute("UPDATE chat_discount_codes SET buyer_email=%s WHERE id=%s", (email, c["id"]))
        if not lead_id and email:
            cur.execute("""SELECT id FROM merchant_pipeline_leads
                            WHERE tenant_id=%s AND dropped_at IS NULL AND COALESCE(outcome,'')=''
                              AND (lower(email)=lower(%s)
                                   OR lower(%s) = ANY(string_to_array(lower(COALESCE(other_emails,'')), ', ')))
                            ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 1""", (tid, email, email))
            r = cur.fetchone()
            lead_id = r and r["id"]
        if not lead_id:
            conn.commit()
            return None
        cur.execute("UPDATE chat_discount_codes SET lead_id=%s WHERE id=%s AND lead_id IS NULL", (lead_id, c["id"]))
        if cur.rowcount:
            by = _given_words(c)
            _history(cur, lead_id, f"Discount code {c['code']} ({value_text(c['kind'], c['value'], c['currency'])} off) "
                                   f"given by {by} in the website chat", by)
            if c["status"] == "used":
                st = words(c.get("platform"))["store"]
                _history(cur, lead_id, f"Discount code {c['code']} was used in your {st} store", st)
        conn.commit()
        return lead_id
    finally:
        cur.close(); conn.close()


# ── Used / Expired: Shopify checked every 30 minutes ────────────────────
_USAGE_Q = """query($ids:[ID!]!){ nodes(ids:$ids){ ... on DiscountCodeNode { id codeDiscount {
  ... on DiscountCodeBasic { asyncUsageCount status endsAt } } } } }"""
CHECK_EVERY = 30 * 60
LATE_DAYS = 3     # Shopify's use count can arrive late: keep looking at ended codes this long


def check_usage():
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT id, tenant_id, shop, shopify_id, code, status, ends_at, lead_id, platform
                         FROM chat_discount_codes
                        WHERE shopify_id IS NOT NULL
                          AND (status='active' OR (status='expired' AND ends_at > NOW() - %s * INTERVAL '1 day'))
                        ORDER BY shop, id""", (LATE_DAYS,))
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    by_shop = {}
    for r in rows:
        k = ("woo", r["tenant_id"]) if r["platform"] == "woo" else ("shopify", r["shop"])
        by_shop.setdefault(k, []).append(r)
    import shopify_app
    now = datetime.now(timezone.utc)
    for (platform, shop), codes in by_shop.items():
        woo_row = _shop(shop) if platform == "woo" else None
        for i in range(0, len(codes), 100):
            part = codes[i:i + 100]
            try:
                if platform == "woo":
                    if not woo_row or woo_row.get("platform") != "woo":
                        raise RuntimeError("shop not connected")
                    res = _woo_call(woo_row, {"action": "usage", "ids": [c["shopify_id"] for c in part]})
                    if not res.get("ok"):
                        raise RuntimeError(res.get("error") or "no answer")
                    use = res.get("usage") or {}
                    d = {"nodes": [{"id": c["shopify_id"], "codeDiscount": {"asyncUsageCount": use.get(str(c["shopify_id"]), 0)}}
                                   for c in part if str(c["shopify_id"]) in use]}
                else:
                    d = shopify_app.graphql(shop, _USAGE_Q, {"ids": [c["shopify_id"] for c in part]})
            except Exception as e:
                print(f"⚠️ chat_discounts usage {shop}: {str(e)[:300]}")
                # Shopify didn't answer (or the permission isn't approved yet):
                # ended codes still show Expired; they're looked at again for 3 days.
                conn = get_db_connection(); cur = conn.cursor()
                try:
                    cur.execute("""UPDATE chat_discount_codes SET status='expired'
                                    WHERE id = ANY(%s) AND status='active' AND ends_at <= NOW()""",
                                ([c["id"] for c in codes],))
                    conn.commit()
                finally:
                    cur.close(); conn.close()
                break
            seen = {}
            for n in (d.get("nodes") or []):
                if n and n.get("id"):
                    seen[n["id"]] = int(((n.get("codeDiscount") or {}).get("asyncUsageCount")) or 0)
            conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                for c in part:
                    used = seen.get(c["shopify_id"], 0) > 0
                    if used:
                        cur.execute("""UPDATE chat_discount_codes SET status='used', used_at=NOW(), checked_at=NOW()
                                        WHERE id=%s AND status<>'used'""", (c["id"],))
                        if cur.rowcount and c["lead_id"]:
                            st = words(c["platform"])["store"]
                            _history(cur, c["lead_id"], f"Discount code {c['code']} was used in your {st} store", st)
                    elif c["status"] == "active" and c["ends_at"] <= now:
                        cur.execute("UPDATE chat_discount_codes SET status='expired', checked_at=NOW() WHERE id=%s",
                                    (c["id"],))
                    else:
                        cur.execute("UPDATE chat_discount_codes SET checked_at=NOW() WHERE id=%s", (c["id"],))
                conn.commit()
            finally:
                cur.close(); conn.close()
    # Codes given before the buyer's lead existed (or before an email was given).
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""SELECT id FROM chat_discount_codes WHERE lead_id IS NULL AND status = ANY(%s)
                         AND created_at > NOW() - INTERVAL '14 days'""", (list(GIVEN),))
        ids = [r[0] for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    for cid in ids:
        try:
            link_lead(cid)
        except Exception as e:
            print(f"⚠️ chat_discounts relink {cid}: {e}")


def _loop():
    import time
    time.sleep(90)
    while True:
        lock = None
        try:
            lock = get_db_connection(); lock.autocommit = True
            lc = lock.cursor()
            lc.execute("SELECT pg_try_advisory_lock(771101)")
            if lc.fetchone()[0]:      # one portal worker at a time
                try:
                    check_usage()
                finally:
                    lc.execute("SELECT pg_advisory_unlock(771101)")
        except Exception as e:
            print("⚠️ chat_discounts loop:", e)
        finally:
            if lock:
                try:
                    lock.close()
                except Exception:
                    pass
        time.sleep(CHECK_EVERY)


def start():
    import threading
    if not getattr(threading, "_phixtra_chat_discounts_started", False):
        threading._phixtra_chat_discounts_started = True  # type: ignore[attr-defined]
        threading.Thread(target=_loop, daemon=True).start()


# ── What the screens show ───────────────────────────────────────────────
def _card(tenant_id: int, r: dict) -> dict:
    tz = _tz(tenant_id)
    v = value_text(r["kind"], r["value"], r["currency"])
    disc = f"{v} off" + (f" · orders {money(r['min_order'], r['currency'])}+" if r.get("min_order") else "")
    st = r["status"]
    if st == "active" and r["ends_at"] <= datetime.now(timezone.utc):
        st = "expired"       # ended since the last check
    if st == "used":
        when = "seen " + (r["used_at"].astimezone(tz).strftime("%a %-d %b, %H:%M") if r.get("used_at") else "")
    elif st == "expired":
        when = "not used, ended " + r["ends_at"].astimezone(tz).strftime("%a %-d %b")
    else:
        when = "ends " + ends_text(tenant_id, r["ends_at"])
    return {"id": r["id"], "code": r["code"], "discount": disc, "value_text": v, "status": st,
            "status_label": st.capitalize(), "when": when.strip(),
            "given": r["created_at"].astimezone(tz).strftime("%a %-d %b, %H:%M"),
            "given_by": _given_words(r), "lead_id": r.get("lead_id"), "session_id": r["session_id"],
            "email": r.get("buyer_email") or "",
            "summary": f"{v} off · works once · " + (when if st != "active" else "ends " + ends_text(tenant_id, r["ends_at"]))}


def list_codes(tenant_id: int, status: str = "", limit: int = 50) -> dict:
    """{"rows": [...], "counts": {...}, "totals": {...}} for the Discounts page."""
    import data_crypto
    tenant_id = int(tenant_id)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT CASE WHEN status='active' AND ends_at <= NOW() THEN 'expired' ELSE status END AS st,
                              COUNT(*) AS n FROM chat_discount_codes
                        WHERE tenant_id=%s AND status = ANY(%s) GROUP BY 1""", (tenant_id, list(GIVEN)))
        counts = {k: 0 for k in GIVEN}
        for r in cur.fetchall():
            counts[r["st"]] = int(r["n"])
        counts["all"] = sum(counts[k] for k in GIVEN)
        cur.execute("""SELECT COUNT(*) AS n,
                              COUNT(*) FILTER (WHERE status='used') AS used,
                              COUNT(*) FILTER (WHERE given_by='ai') AS ai,
                              COUNT(*) FILTER (WHERE given_by='staff') AS staff
                         FROM chat_discount_codes
                        WHERE tenant_id=%s AND status = ANY(%s) AND created_at > NOW() - INTERVAL '30 days'""",
                    (tenant_id, list(GIVEN)))
        totals = {k: int(v or 0) for k, v in cur.fetchone().items()}
        # 13 Nov 2026 (page redesign): codes given today (against the daily limit)
        # and the share of codes used.
        totals["today"] = _made_today(cur, tenant_id)
        totals["used_pct"] = round(100 * totals["used"] / totals["n"]) if totals.get("n") else 0
        where = ""
        if status == "active":
            where = " AND c.status='active' AND c.ends_at > NOW()"
        elif status == "expired":
            where = " AND (c.status='expired' OR (c.status='active' AND c.ends_at <= NOW()))"
        elif status == "used":
            where = " AND c.status='used'"
        cur.execute(f"""SELECT c.*,
                               (SELECT visitor_name FROM handoff_requests h WHERE h.tenant_id=c.tenant_id
                                   AND h.session_id=c.session_id AND COALESCE(h.visitor_name,'')<>''
                                 ORDER BY h.id DESC LIMIT 1) AS h_name,
                               EXISTS (SELECT 1 FROM handoff_requests h2 WHERE h2.tenant_id=c.tenant_id
                                   AND h2.session_id=c.session_id) AS in_inbox,
                               l.contact_person AS l_name, l.customer_name AS l_company
                          FROM chat_discount_codes c
                          LEFT JOIN merchant_pipeline_leads l ON l.id=c.lead_id AND l.tenant_id=c.tenant_id
                         WHERE c.tenant_id=%s AND c.status = ANY(%s){where}
                         ORDER BY c.id DESC LIMIT %s""", (tenant_id, list(GIVEN), int(limit)))
        rows = []
        for r in cur.fetchall():
            x = _card(tenant_id, r)
            x["name"] = (data_crypto.dec(r.get("h_name")) or r.get("l_name") or r.get("l_company") or "Website visitor")
            # Every website chat is in the Inbox since 13 Nov 2026.
            x["in_inbox"] = bool(r.get("session_id")) and not str(r.get("session_id")).startswith("wa-")
            rows.append(x)
    finally:
        cur.close(); conn.close()
    return {"rows": rows, "counts": counts, "totals": totals}


def chat_code(tenant_id: int, session_id: str):
    """The code this chat's buyer got (newest), for the Inbox — or None."""
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT * FROM chat_discount_codes WHERE tenant_id=%s AND session_id=%s AND status = ANY(%s)
                        ORDER BY id DESC LIMIT 1""", (int(tenant_id), session_id, list(GIVEN)))
        r = cur.fetchone()
    finally:
        cur.close(); conn.close()
    return _card(int(tenant_id), r) if r else None


def lead_codes(tenant_id: int, lead_id: int) -> list:
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT to_regclass('chat_discount_codes') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            return []
        cur.execute("""SELECT c.*, EXISTS (SELECT 1 FROM handoff_requests h WHERE h.tenant_id=c.tenant_id
                                                AND h.session_id=c.session_id) AS in_inbox
                         FROM chat_discount_codes c WHERE c.tenant_id=%s AND c.lead_id=%s AND c.status = ANY(%s)
                        ORDER BY c.id DESC LIMIT 5""", (int(tenant_id), int(lead_id), list(GIVEN)))
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    return [dict(_card(int(tenant_id), r), in_inbox=bool(r["in_inbox"])) for r in rows]


def send_box(tenant_id: int, session_id: str) -> dict:
    """What the Inbox 'Send discount code' button needs, or {} when it must not show
    (not switched on, permission not approved, B2B, plan)."""
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        s, row = _ready(cur, tenant_id)
        if not s:
            return {}
        have = None
        if session_id:
            cur.execute("""SELECT code FROM chat_discount_codes WHERE tenant_id=%s AND session_id=%s
                             AND status = ANY(%s) ORDER BY id DESC LIMIT 1""", (int(tenant_id), session_id, list(GIVEN)))
            have = (cur.fetchone() or {}).get("code")
        today = _made_today(cur, tenant_id)
    finally:
        cur.close(); conn.close()
    cur_code = row.get("currency") or ""
    v = value_text(s["kind"], s["value"], cur_code)
    lasts = {24: "24 hours", 48: "48 hours", 168: "7 days"}[s["hours"]]
    where = ("only on: " + ", ".join(c["title"] for c in s["collections"])
             if s["products"] == "collections" and s["collections"] else "all products")
    rules = f"{v} off, works once, lasts {lasts}, {where}"
    if s["min_order"]:
        rules += f", on orders of {money(s['min_order'], cur_code)} or more"
    return {"value_text": v, "rules": rules, "today": today, "daily_max": s["daily_max"],
            "store": words(row.get("platform"))["store"],
            "has_code": have, "full": today >= s["daily_max"],
            "default_message": f"Here is a code for {v} off your order:"}


def _chat_email_for(tenant_id: int, session_id: str) -> str:
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        e = _chat_email(cur, int(tenant_id), session_id)
        conn.commit()
        return e
    finally:
        cur.close(); conn.close()
