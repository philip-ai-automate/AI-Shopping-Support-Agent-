"""
woo_orders.py — accepted quotation → WooCommerce order (10 Nov 2026).

AI Sales Agent for B2B (Quotations), WooCommerce businesses. The salesperson
presses "Create WooCommerce order" on an Accepted quotation; PhiXtra calls the
shop's PhiXtra plugin (3.10.0+, phixtra/v1/create-order), which makes a normal
WooCommerce order at the quoted prices, Pending payment, attached to the
buyer's customer account (made if they have none) and sends WooCommerce's own
"pay for this order" email. The plugin tells PhiXtra when the order changes
status, so the deal shows "Order #1234 · paid".

Every call both ways is signed with the shop's own PhiXtra key: HMAC-SHA256
of "<unix ts>.<body>", plus SHA-256 of the key (= api_keys.api_key_lookup) so
PhiXtra knows which business it is. The key itself never travels.

Where the shop is: the plugin says hello (/public/woo/hello) with its route;
no hello = an older plugin, and the button says to update it.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg2.extras
import requests

from db import get_db_connection
import quote_requests as qr

MAX_AGE = 300            # a signed call older than 5 minutes is refused
STATUS_WORDS = {
    "pending": "Pending payment", "processing": "Paid", "completed": "Completed", "on-hold": "On hold",
    "cancelled": "Cancelled", "refunded": "Refunded", "failed": "Payment failed", "draft": "Draft",
}
PAID = ("processing", "completed")
MIN_PLUGIN = (3, 10, 0)


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


_ready = False


def ensure_tables():
    global _ready
    if _ready:
        return
    conn, cur = _db()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS woo_shops (
                           tenant_id   INTEGER PRIMARY KEY,
                           api_key_id  INTEGER NOT NULL,
                           route       TEXT NOT NULL,
                           site        TEXT,
                           plugin      VARCHAR(20),
                           woocommerce VARCHAR(20),
                           last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("ALTER TABLE quotations ADD COLUMN IF NOT EXISTS woo_order JSONB")
        conn.commit()
        _ready = True
    except Exception as e:
        conn.rollback()
        print("⚠️ woo_orders tables:", e)
    finally:
        cur.close(); conn.close()


# ── signatures ──────────────────────────────────────────────────────────────

def sign(key: str, body: bytes, ts: str = None) -> dict:
    ts = ts or str(int(time.time()))
    sig = hmac.new(key.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Phixtra-Key": hashlib.sha256(key.encode()).hexdigest(), "X-Phixtra-Ts": ts, "X-Phixtra-Sig": sig}


def verify(headers, body: bytes):
    """The api_keys row (id, tenant_id) for a correctly signed, fresh call; else None."""
    lookup = (headers.get("X-Phixtra-Key") or "").strip().lower()
    ts = (headers.get("X-Phixtra-Ts") or "").strip()
    sig = (headers.get("X-Phixtra-Sig") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", lookup) or not ts.isdigit() or not re.fullmatch(r"[0-9a-f]{64}", sig):
        return None
    if abs(time.time() - int(ts)) > MAX_AGE:
        return None
    conn, cur = _db()
    try:
        cur.execute("""SELECT ak.id, ak.tenant_id, ak.api_key_plain FROM api_keys ak JOIN tenants t ON t.id = ak.tenant_id
                        WHERE ak.api_key_lookup=%s AND ak.is_active AND t.status IN ('active', 'pending') LIMIT 1""",
                    (lookup,))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not row or not row["api_key_plain"]:
        return None
    want = hmac.new(row["api_key_plain"].encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig):
        return None
    return {"id": row["id"], "tenant_id": row["tenant_id"]}


def _safe_route(url: str) -> bool:
    """https, a public host, the plugin's own route path. Tests may allow a
    local shop with WOO_ALLOW_LOCAL=1 (never set on the live server)."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if not u.hostname or "phixtra/v1/create-order" not in (u.path + "?" + (u.query or "")):
        return False
    local_ok = os.environ.get("WOO_ALLOW_LOCAL") == "1"
    if u.scheme != "https" and not (local_ok and u.scheme == "http"):
        return False
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(u.hostname, None)}
    except OSError:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved) and not local_ok:
            return False
    return True


def _version(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


# ── the plugin says hello ───────────────────────────────────────────────────

def hello(key_row: dict, data: dict) -> tuple:
    ensure_tables()
    route = str(data.get("route") or "").strip()[:500]
    if not _safe_route(route):
        return False, "That shop address can't be used."
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO woo_shops (tenant_id, api_key_id, route, site, plugin, woocommerce, last_seen)
                       VALUES (%s,%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT (tenant_id) DO UPDATE SET api_key_id=EXCLUDED.api_key_id, route=EXCLUDED.route,
                             site=EXCLUDED.site, plugin=EXCLUDED.plugin, woocommerce=EXCLUDED.woocommerce, last_seen=NOW()""",
                    (key_row["tenant_id"], key_row["id"], route, str(data.get("site") or "")[:300],
                     str(data.get("plugin") or "")[:20], str(data.get("woocommerce") or "")[:20]))
        conn.commit()
        return True, "ok"
    finally:
        cur.close(); conn.close()


def shop_for(tenant_id: int):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT s.*, ak.api_key_plain FROM woo_shops s JOIN api_keys ak ON ak.id = s.api_key_id
                        WHERE s.tenant_id=%s AND ak.is_active""", (int(tenant_id),))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def _shopify_on(tenant_id: int) -> bool:
    conn, cur = _db()
    try:
        cur.execute("SELECT to_regclass('shopify_shops') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            return False
        cur.execute("SELECT 1 FROM shopify_shops WHERE tenant_id=%s AND uninstalled_at IS NULL LIMIT 1", (int(tenant_id),))
        return cur.fetchone() is not None
    finally:
        cur.close(); conn.close()


def has_shop(tenant_id: int) -> bool:
    """A WooCommerce business: the plugin is connected (a website key, not a
    Shopify store). Used to decide whether to offer the button at all."""
    try:
        if shop_for(tenant_id):
            return True
        conn, cur = _db()
        try:
            cur.execute("""SELECT COALESCE(t.website_platform,'') AS wp,
                                  EXISTS(SELECT 1 FROM shopify_shops s WHERE s.tenant_id=t.id AND s.uninstalled_at IS NULL) AS shop,
                                  EXISTS(SELECT 1 FROM api_keys k WHERE k.tenant_id=t.id AND k.is_active
                                          AND k.key_type IN ('paid','trial')) AS site
                             FROM tenants t WHERE t.id=%s""", (int(tenant_id),))
            r = cur.fetchone() or {}
        finally:
            cur.close(); conn.close()
        return bool(r.get("site")) and not r.get("shop") and (r.get("wp") or "").lower() in ("", "wordpress")
    except Exception as e:
        print("⚠️ woo has_shop:", e)
        return False


# ── the order for a quotation ───────────────────────────────────────────────

def _latest_sent(cur, tenant_id: int, request_id: int):
    cur.execute("""SELECT id, number, version, data, currency, total, sent_at, woo_order FROM quotations
                    WHERE tenant_id=%s AND request_id=%s AND status='sent'
                    ORDER BY version DESC, id DESC LIMIT 1""", (tenant_id, request_id))
    return cur.fetchone()


def for_request(tenant_id: int, request_id: int) -> dict:
    """What the deal page / request page show: {state, order?, why?}.
    state: none (not a WooCommerce business or not accepted) · ready · update
    (old plugin) · made."""
    ensure_tables()
    req = qr.get(tenant_id, request_id)
    if not req or req["status"] != "won":
        return {"state": "none"}
    conn, cur = _db()
    try:
        q = _latest_sent(cur, tenant_id, request_id)
    finally:
        cur.close(); conn.close()
    if not q:
        return {"state": "none"}          # made in the business's own software: no lines to send
    if q.get("woo_order"):
        o = dict(q["woo_order"])
        o["word"] = STATUS_WORDS.get(o.get("status"), (o.get("status") or "").capitalize())
        o["paid"] = o.get("status") in PAID
        return {"state": "made", "order": o, "qid": q["id"]}
    if (q.get("data") or {}).get("shopify_site"):
        return {"state": "none"}          # Shopify trade customer: Shopify draft order instead
    if _shopify_on(tenant_id):
        return {"state": "none"}          # a Shopify store: its own draft order flow
    try:
        import xero_link as _xl
        if _xl.chosen(tenant_id) == "xero":
            return {"state": "none"}      # the business sends accepted quotes to Xero instead
        import qbo_link as _ql
        if _ql.chosen(tenant_id) == "qbo":
            return {"state": "none"}      # … or to QuickBooks
    except Exception as e:
        print("⚠️ woo xero choice:", e)
    if not has_shop(tenant_id):
        return {"state": "none"}
    shop = shop_for(tenant_id)
    if not shop or _version(shop.get("plugin")) < MIN_PLUGIN:
        return {"state": "update", "qid": q["id"]}
    return {"state": "ready", "qid": q["id"]}


def _wc_id(product_id):
    m = re.fullmatch(r"product-(\d+)", str(product_id or ""))
    return int(m.group(1)) if m else None


def _payload(tenant_id: int, req: dict, q: dict) -> dict:
    d = q.get("data") or {}
    lines = []
    for ln in d.get("lines") or []:
        if ln.get("price") is None or not ln.get("qty"):
            continue
        price = float(ln["price"]) * (1 - float(ln.get("disc") or 0) / 100)
        lines.append({"wc_id": _wc_id(ln.get("product_id")), "name": ln.get("name") or "",
                      "qty": float(ln["qty"]), "price": round(price, 4)})
    charges = []
    for c in d.get("charges") or []:
        amt = float(c.get("price") or 0) * float(c.get("qty") or 1)
        if amt:
            charges.append({"name": c.get("name") or "Charge", "amount": round(amt, 2)})
    name = (req.get("buyer_name") or "").strip().split()
    po = ((req.get("ai_answer") or {}).get("po") or "").strip()
    num = q["number"] + (f" v{q['version']}" if (q.get("version") or 1) > 1 else "")
    return {"ref": f"phx-{tenant_id}-{q['id']}", "number": num, "po": po[:60],
            "accepted": req["closed_at"].strftime("%d %b %Y") if req.get("closed_at") else "",
            "currency": q.get("currency") or req.get("quote_currency") or "",
            "lines": lines, "charges": charges,
            "buyer": {"company": req.get("buyer_company") or "", "first_name": name[0] if name else "",
                      "last_name": " ".join(name[1:]), "email": req.get("buyer_email") or "",
                      "phone": req.get("buyer_phone") or "", "address": req.get("deliver_to") or "",
                      "country": c if re.fullmatch(r"[A-Za-z]{2}", c := (req.get("buyer_country") or "").strip()) else ""}}


def create(tenant_id: int, request_id: int, actor: dict, po: str = "") -> tuple:
    """(ok, message). One order per quotation; a second press shows the first."""
    st = for_request(tenant_id, request_id)
    if st["state"] == "made":
        return True, f"WooCommerce order #{st['order']['number']} already exists for this quotation."
    if st["state"] == "update":
        return False, "Update the PhiXtra plugin in your WordPress (version 3.10 or newer) to create WooCommerce orders from quotations."
    if st["state"] != "ready":
        return False, "This quotation can't be turned into a WooCommerce order."
    shop = shop_for(tenant_id)
    if not _safe_route(shop["route"]):
        return False, "Your shop's address can't be used. Open your WordPress admin once so the plugin can send it again."
    req = qr.get(tenant_id, request_id)
    conn, cur = _db()
    try:
        q = _latest_sent(cur, tenant_id, request_id)
    finally:
        cur.close(); conn.close()
    payload = _payload(tenant_id, req, q)
    if (po or "").strip():
        payload["po"] = " ".join(po.split())[:60]
    if not payload["lines"]:
        return False, "The quotation has no priced lines to put in an order."
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", **sign(shop["api_key_plain"], body)}
    try:
        r = requests.post(shop["route"], data=body, headers=headers, timeout=25, allow_redirects=False)
        res = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except (requests.RequestException, ValueError):
        return False, "Your shop didn't answer. Nothing was created. Try again in a minute."
    if r.status_code == 404:
        return False, "Update the PhiXtra plugin in your WordPress (version 3.10 or newer) to create WooCommerce orders from quotations."
    if r.status_code in (401, 403):
        return False, "Your shop refused the request. Check that the PhiXtra plugin uses this business's key."
    if not res.get("ok"):
        return False, (res.get("error") or "Your shop couldn't make the order.") + " Nothing was created in PhiXtra."
    order = {"order_id": res.get("order_id"), "number": str(res.get("number") or res.get("order_id")),
             "status": res.get("status") or "pending", "total": res.get("total"), "currency": res.get("currency"),
             "admin_url": res.get("admin_url") or "", "account": res.get("account") or "",
             "quote_total": float(q.get("total") or 0), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "by": actor.get("label") or ""}
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET woo_order=%s WHERE id=%s AND tenant_id=%s",
                    (psycopg2.extras.Json(order), q["id"], tenant_id))
        what = f"WooCommerce order #{order['number']} created from {payload['number']}"
        if order["account"] == "created":
            what += "; a customer account was made for the buyer"
        qr._event(cur, request_id, what, actor.get("label") or "")
        conn.commit()
    finally:
        cur.close(); conn.close()
    msg = f"WooCommerce order #{order['number']} created. The buyer has been emailed a link to pay."
    if res.get("existing"):
        msg = f"WooCommerce order #{order['number']} already existed for this quotation."
    return True, msg


def on_status(key_row: dict, data: dict) -> bool:
    """The plugin reports a status change for one of PhiXtra's orders."""
    ensure_tables()
    m = re.fullmatch(r"phx-(\d+)-(\d+)", str(data.get("ref") or ""))
    if not m or int(m.group(1)) != int(key_row["tenant_id"]):
        return False
    tid, qid = int(m.group(1)), int(m.group(2))
    status = re.sub(r"[^a-z\-]", "", str(data.get("status") or "").lower())[:20]
    conn, cur = _db()
    try:
        cur.execute("SELECT id, request_id, woo_order FROM quotations WHERE id=%s AND tenant_id=%s", (qid, tid))
        q = cur.fetchone()
        if not q or not q.get("woo_order"):
            return False
        o = dict(q["woo_order"])
        if o.get("status") == status:
            return True
        o.update({"status": status, "total": data.get("total", o.get("total")),
                  "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if status in PAID and not o.get("paid_at"):
            o["paid_at"] = o["updated_at"]
        cur.execute("UPDATE quotations SET woo_order=%s WHERE id=%s", (psycopg2.extras.Json(o), qid))
        word = STATUS_WORDS.get(status, status)
        qr._event(cur, q["request_id"], f"WooCommerce order #{o.get('number')}: {word}", "WooCommerce")
        conn.commit()
        return True
    finally:
        cur.close(); conn.close()


def orders_for_requests(tenant_id: int, request_ids: list) -> dict:
    """{request_id: order dict} for the Sales Pipeline Quotation column."""
    if not request_ids:
        return {}
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT DISTINCT ON (request_id) request_id, woo_order FROM quotations
                        WHERE tenant_id=%s AND request_id = ANY(%s) AND status='sent'
                        ORDER BY request_id, version DESC, id DESC""", (tenant_id, list(request_ids)))
        out = {}
        for r in cur.fetchall():
            if r["woo_order"]:
                o = dict(r["woo_order"])
                o["word"] = STATUS_WORDS.get(o.get("status"), o.get("status") or "")
                o["paid"] = o.get("status") in PAID
                out[r["request_id"]] = o
        return out
    finally:
        cur.close(); conn.close()
