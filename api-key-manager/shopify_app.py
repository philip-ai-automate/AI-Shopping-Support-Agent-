"""
shopify_app.py — PhiXtra for Shopify, Step 1 Part 1: install & connect
(1 Nov 2026; plan https://claude.ai/artifact/MHRo5rJNqTTUJQ7u1cyZh1).

Embedded app (runs inside Shopify admin). Shopify handles the install and
the permissions; we never ask for a myshopify address or a password.

  GET  /shopify/app          App home inside Shopify admin (App Bridge).
  POST /shopify/api/session  App Bridge ID token -> checked here -> swapped
                             for an expiring offline token (1 h, refresh 90 d).
  GET  /shopify/callback     Not used by managed install; sends the browser
                             back to the app inside Shopify admin.
  POST /shopify/webhooks     app/uninstalled + the 3 privacy (GDPR) webhooks,
                             HMAC-checked.

Keys live only in the service environment (SHOPIFY_API_KEY / _SECRET).
Store tokens stay on the server, never sent to the browser.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import time

import psycopg2.extras
import requests
from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request

from db import get_db_connection
from feature_access import public_route

shopify_bp = Blueprint("shopify", __name__, url_prefix="/shopify")

API_VERSION = "2026-10"
_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")


_CONF = "/etc/systemd/system/phixtra-portal.service.d/shopify.conf"
_conf_cache: dict = {}


def _env(name: str) -> str:
    """From the portal service's environment; scripts run by cron (product
    sync, 2026-11-02) don't have it, so they read the same root-only file."""
    v = os.getenv(name, "")
    if v:
        return v
    if not _conf_cache:
        try:
            for line in open(_CONF, encoding="utf-8"):
                m = re.match(r'\s*Environment="?([A-Z_]+)=(.*?)"?\s*$', line)
                if m:
                    _conf_cache[m.group(1)] = m.group(2)
        except OSError:
            pass
    return _conf_cache.get(name, "")


def _key() -> str:
    return _env("SHOPIFY_API_KEY")


def _secret() -> str:
    return _env("SHOPIFY_API_SECRET")


def valid_shop(shop: str) -> bool:
    return bool(shop) and bool(_SHOP_RE.match(shop.lower()))


# ── Tables ──────────────────────────────────────────────────────────────
def ensure_tables():
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shopify_shops (
                shop               TEXT PRIMARY KEY,
                tenant_id          INTEGER,
                access_token       TEXT,
                access_expires_at  TIMESTAMPTZ,
                refresh_token      TEXT,
                refresh_expires_at TIMESTAMPTZ,
                scope              TEXT,
                shop_name          TEXT,
                shop_email         TEXT,
                currency           TEXT,
                country            TEXT,
                plan_name          TEXT,
                is_plus            BOOLEAN DEFAULT FALSE,
                primary_domain     TEXT,
                installed_at       TIMESTAMPTZ DEFAULT NOW(),
                uninstalled_at     TIMESTAMPTZ,
                updated_at         TIMESTAMPTZ DEFAULT NOW()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shopify_webhook_log (
                id          SERIAL PRIMARY KEY,
                shop        TEXT,
                topic       TEXT,
                webhook_id  TEXT,
                payload     JSONB,
                received_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS shopify_shops_tenant ON shopify_shops(tenant_id)")
        cur.execute("""SELECT column_name FROM information_schema.columns
                        WHERE table_name='shopify_shops'""")
        have = {r[0] for r in cur.fetchall()}
        for col, typ in (("owner_user_id", "TEXT"), ("linked_at", "TIMESTAMPTZ"), ("address1", "TEXT"),
                         ("city", "TEXT"), ("zip", "TEXT"),
                         # Part 3 (2026-11-02): store chat
                         ("visitor_secret", "TEXT"), ("embed_key_set_for", "INTEGER"), ("chat_seen_at", "TIMESTAMPTZ")):
            if col not in have:
                cur.execute(f"ALTER TABLE shopify_shops ADD COLUMN {col} {typ}")
        conn.commit()
        import shopify_sync
        shopify_sync.ensure_tables()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shopify_sso (
                token_hash  TEXT PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                next_path   TEXT,
                expires_at  TIMESTAMPTZ NOT NULL,
                used_at     TIMESTAMPTZ
            )""")
        conn.commit()
    finally:
        cur.close(); conn.close()


# ── Signatures ──────────────────────────────────────────────────────────
def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_id_token(token: str):
    """App Bridge ID token (JWT, HS256 with our client secret). Returns the
    shop (xxx.myshopify.com) or None."""
    try:
        h, p, s = token.split(".")
        header = json.loads(_b64url_decode(h))
        if header.get("alg") != "HS256":
            return None
        want = hmac.new(_secret().encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _b64url_decode(s)):
            return None
        c = json.loads(_b64url_decode(p))
        now = time.time()
        if c.get("exp", 0) < now - 5 or c.get("nbf", 0) > now + 5:
            return None
        aud = c.get("aud")
        if (aud if isinstance(aud, str) else (aud or [None])[0]) != _key():
            return None
        from urllib.parse import urlparse
        dest = urlparse(c.get("dest") or "").hostname or ""
        iss = urlparse(c.get("iss") or "").hostname or ""
        if not dest or dest != iss or not valid_shop(dest):
            return None
        return dest.lower(), str(c.get("sub") or "")
    except Exception:
        return None


def verify_query_hmac(args) -> bool:
    """Shopify signs the app's address when it opens it (hex HMAC of the
    other parameters, sorted)."""
    given = args.get("hmac") or ""
    if not given:
        return False
    msg = "&".join(f"{k}={v}" for k, v in sorted((k, v) for k, v in args.items(multi=False) if k != "hmac"))
    want = hmac.new(_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, given)


def verify_webhook(raw: bytes, header: str) -> bool:
    if not header:
        return False
    want = base64.b64encode(hmac.new(_secret().encode(), raw, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, header)


# ── Tokens ──────────────────────────────────────────────────────────────
def _save_tokens(shop: str, data: dict):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO shopify_shops (shop, access_token, access_expires_at, refresh_token,
                                       refresh_expires_at, scope, installed_at, uninstalled_at, updated_at)
            VALUES (%s, %s, NOW() + make_interval(secs => %s), %s,
                    NOW() + make_interval(secs => %s), %s, NOW(), NULL, NOW())
            ON CONFLICT (shop) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                access_expires_at = EXCLUDED.access_expires_at,
                refresh_token = EXCLUDED.refresh_token,
                refresh_expires_at = EXCLUDED.refresh_expires_at,
                scope = EXCLUDED.scope,
                installed_at = CASE WHEN shopify_shops.uninstalled_at IS NOT NULL
                                    THEN NOW() ELSE shopify_shops.installed_at END,
                uninstalled_at = NULL,
                updated_at = NOW()""",
            (shop, data["access_token"], int(data.get("expires_in") or 3600),
             data.get("refresh_token"), int(data.get("refresh_token_expires_in") or 7776000),
             data.get("scope")))
        conn.commit()
    finally:
        cur.close(); conn.close()


def exchange_id_token(shop: str, id_token: str) -> bool:
    r = requests.post(f"https://{shop}/admin/oauth/access_token", data={
        "client_id": _key(), "client_secret": _secret(),
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": id_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
        "requested_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
        "expiring": "1",
    }, timeout=15)
    if r.status_code != 200:
        print(f"⚠️ shopify token exchange {shop}: {r.status_code} {r.text[:300]}")
        return False
    _save_tokens(shop, r.json())
    return True


def _shop_row(shop: str):
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM shopify_shops WHERE shop=%s", (shop,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def access_token(shop: str):
    """A working access token for background jobs (refreshes when needed).
    None = the shop must open the app again (uninstalled / refresh expired)."""
    row = _shop_row(shop)
    if not row or row["uninstalled_at"] or not row["access_token"]:
        return None
    if row["access_expires_at"] and row["access_expires_at"].timestamp() > time.time() + 120:
        return row["access_token"]
    if not row["refresh_token"]:
        return None
    r = requests.post(f"https://{shop}/admin/oauth/access_token", data={
        "client_id": _key(), "client_secret": _secret(),
        "grant_type": "refresh_token", "refresh_token": row["refresh_token"],
    }, timeout=15)
    if r.status_code != 200:
        print(f"⚠️ shopify refresh {shop}: {r.status_code} {r.text[:300]}")
        return None
    _save_tokens(shop, r.json())
    return r.json()["access_token"]


def graphql(shop: str, query: str, variables=None):
    tok = access_token(shop)
    if not tok:
        raise RuntimeError("no_token")
    r = requests.post(f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
                      json={"query": query, "variables": variables or {}},
                      headers={"X-Shopify-Access-Token": tok}, timeout=30)
    r.raise_for_status()
    out = r.json()
    if out.get("errors"):
        raise RuntimeError(json.dumps(out["errors"])[:500])
    return out["data"]


_SHOP_Q = """{ shop { name email currencyCode
  plan { displayName shopifyPlus }
  billingAddress { countryCodeV2 address1 city zip }
  primaryDomain { host } } }"""

_WEBHOOK_SUB = """mutation($topic: WebhookSubscriptionTopic!, $uri: String!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: {uri: $uri}) {
    userErrors { field message } } }"""


def refresh_shop_details(shop: str):
    s = graphql(shop, _SHOP_Q)["shop"]
    conn = get_db_connection(); cur = conn.cursor()
    try:
        ba = s.get("billingAddress") or {}
        cur.execute("""UPDATE shopify_shops SET shop_name=%s, shop_email=%s, currency=%s,
                          country=%s, plan_name=%s, is_plus=%s, primary_domain=%s,
                          address1=%s, city=%s, zip=%s, updated_at=NOW()
                        WHERE shop=%s""",
                    (s.get("name"), s.get("email"), s.get("currencyCode"),
                     ba.get("countryCodeV2"),
                     (s.get("plan") or {}).get("displayName"),
                     bool((s.get("plan") or {}).get("shopifyPlus")),
                     (s.get("primaryDomain") or {}).get("host"),
                     ba.get("address1"), ba.get("city"), ba.get("zip"), shop))
        conn.commit()
    finally:
        cur.close(); conn.close()


_APP_INSTALL_Q = "{ currentAppInstallation { id } }"
_METAFIELDS_SET = """mutation($m: [MetafieldsSetInput!]!) { metafieldsSet(metafields: $m) {
  userErrors { field message } } }"""


def set_embed_data(shop: str) -> bool:
    """Part 3: save the business's chat key and the store's signing secret as
    app data on the store, for the PhiXtra chat app embed. Done once per
    linked business (again if the store is linked to another account)."""
    row = _shop_row(shop)
    if not row or not row.get("tenant_id") or row.get("uninstalled_at"):
        return False
    tid = int(row["tenant_id"])
    if row.get("embed_key_set_for") == tid and row.get("visitor_secret"):
        return True
    import secrets as _s
    import portal_routes as pr
    site_key = pr._site_widget_key(tid)
    secret = row.get("visitor_secret") or _s.token_hex(24)
    owner = graphql(shop, _APP_INSTALL_Q)["currentAppInstallation"]["id"]
    d = graphql(shop, _METAFIELDS_SET, {"m": [
        {"ownerId": owner, "namespace": "phixtra", "key": "site_key", "type": "single_line_text_field", "value": site_key},
        {"ownerId": owner, "namespace": "phixtra", "key": "visitor_secret", "type": "single_line_text_field", "value": secret},
    ]})["metafieldsSet"]
    if d.get("userErrors"):
        print(f"⚠️ shopify embed data {shop}: {d['userErrors']}")
        return False
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE shopify_shops SET visitor_secret=%s, embed_key_set_for=%s WHERE shop=%s", (secret, tid, shop))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return True


def subscribe_uninstall(shop: str):
    try:
        d = graphql(shop, _WEBHOOK_SUB, {"topic": "APP_UNINSTALLED",
                                         "uri": "https://portal.phixtra.com/shopify/webhooks"})
        errs = d["webhookSubscriptionCreate"]["userErrors"]
        if errs and not any("taken" in (e.get("message") or "").lower() for e in errs):
            print(f"⚠️ shopify uninstall webhook {shop}: {errs}")
    except Exception as e:
        print(f"⚠️ shopify uninstall webhook {shop}: {e}")


# ── Routes ──────────────────────────────────────────────────────────────
@shopify_bp.after_request
def _frame_headers(resp):
    """Shopify admin shows the app in a frame: allow only Shopify admin and
    the shop itself to frame these pages."""
    shop = (request.args.get("shop") or "").lower()
    ancestors = "https://admin.shopify.com" + (f" https://{shop}" if valid_shop(shop) else "")
    resp.headers["Content-Security-Policy"] = f"frame-ancestors {ancestors};"
    return resp


@shopify_bp.route("/app")
@public_route
def app_home():
    shop = (request.args.get("shop") or "").lower()
    if not valid_shop(shop) or not verify_query_hmac(request.args):
        # Opened outside Shopify admin: send people to the website.
        return redirect("https://phixtra.com/")
    return render_template("shopify/app_home.html", api_key=_key(), shop=shop)


@shopify_bp.route("/api/session", methods=["POST"])
@public_route
def api_session():
    auth = request.headers.get("Authorization", "")
    id_token = auth[7:] if auth.startswith("Bearer ") else ""
    v = verify_id_token(id_token)
    if not v:
        return _bad_token()
    shop, _user = v
    row = _shop_row(shop)
    fresh = False
    if not row or row["uninstalled_at"] or not access_token(shop):
        if not exchange_id_token(shop, id_token):
            return jsonify({"error": "reauthenticate"}), 401
        fresh = True
    # Re-read store details (email, currency, plan...) on install, while the
    # store is not linked yet (Welcome screen), and at most hourly after that,
    # so changes made in Shopify settings show up.
    stale = True
    if row and row.get("updated_at"):
        import datetime as _dt
        stale = (_dt.datetime.now(_dt.timezone.utc) - row["updated_at"]).total_seconds() > 3600
    if fresh or not (row or {}).get("shop_name") or not (row or {}).get("tenant_id") or stale:
        try:
            refresh_shop_details(shop)
        except Exception as e:
            print(f"⚠️ shopify shop details {shop}: {e}")
        if fresh or not (row or {}).get("shop_name"):
            subscribe_uninstall(shop)
    cur_row = _shop_row(shop)
    if cur_row.get("tenant_id") and cur_row.get("embed_key_set_for") != cur_row["tenant_id"]:
        try:
            set_embed_data(shop)
        except Exception as e:
            print(f"⚠️ shopify embed data {shop}: {e}")
    return jsonify(_state(_shop_row(shop)))


@shopify_bp.route("/callback")
@public_route
def callback():
    shop = (request.args.get("shop") or "").lower()
    if valid_shop(shop):
        return redirect(f"https://{shop}/admin/apps/{_key()}")
    return redirect("https://phixtra.com/")


@shopify_bp.route("/webhooks", methods=["POST"])
@public_route
def webhooks():
    raw = request.get_data()
    if not verify_webhook(raw, request.headers.get("X-Shopify-Hmac-Sha256", "")):
        abort(401)
    topic = (request.headers.get("X-Shopify-Topic") or "").lower()
    shop = (request.headers.get("X-Shopify-Shop-Domain") or "").lower()
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        payload = {}
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO shopify_webhook_log (shop, topic, webhook_id, payload)
                       VALUES (%s, %s, %s, %s)""",
                    (shop, topic, request.headers.get("X-Shopify-Webhook-Id"),
                     json.dumps(payload if topic.startswith(("customers/", "shop/")) else {})))
        if topic == "app/uninstalled":
            cur.execute("""UPDATE shopify_shops SET uninstalled_at=NOW(), access_token=NULL,
                              refresh_token=NULL, updated_at=NOW() WHERE shop=%s""", (shop,))
        elif topic == "shop/redact":
            # 48 h after uninstall: remove what we hold about the shop,
            # including its products on the AI agent (Part 2).
            try:
                import shopify_sync
                shopify_sync.forget_shop(shop)
            except Exception as e:
                print(f"⚠️ shopify forget products {shop}: {e}")
            cur.execute("DELETE FROM shopify_shops WHERE shop=%s", (shop,))
        elif topic in ("products/create", "products/update", "products/delete", "inventory_levels/update"):
            # Part 2: re-read just this product on the next sync run (cron, every minute).
            cur.execute("SELECT 1 FROM shopify_shops WHERE shop=%s AND tenant_id IS NOT NULL AND uninstalled_at IS NULL", (shop,))
            if cur.fetchone():
                kind, ref = {"products/create": ("product", payload.get("id")),
                             "products/update": ("product", payload.get("id")),
                             "products/delete": ("delete", payload.get("id")),
                             "inventory_levels/update": ("inventory_item", payload.get("inventory_item_id"))}[topic]
                if ref:
                    cur.execute("""INSERT INTO shopify_sync_queue (shop, kind, ref) VALUES (%s,%s,%s)
                                   ON CONFLICT (shop, kind, ref) DO NOTHING""", (shop, kind, str(ref)))
        # customers/data_request + customers/redact: logged for PhiXtra to act
        # on (we hold no Shopify customer data in Step 1).
        conn.commit()
    finally:
        cur.close(); conn.close()
    return Response(status=200)


# ── Step 1 Part 1 screens (approved 1 Nov 2026) ─────────────────────────
# Welcome (create account, email code) · Link my account (email code) ·
# Home (set-up steps). Every call carries the App Bridge ID token.
import secrets as _secrets

_CODE_FLOW_NEW  = "shopify"
_CODE_FLOW_LINK = "shopify_link"


def _bad_token():
    resp = jsonify({"error": "Invalid ID token"})
    resp.headers["X-Shopify-Retry-Invalid-Session-Request"] = "1"
    return resp, 401


def _who():
    """(shop, shopify_user_id, shop_row) from the request's ID token, or None."""
    auth = request.headers.get("Authorization", "")
    v = verify_id_token(auth[7:] if auth.startswith("Bearer ") else "")
    if not v:
        return None
    row = _shop_row(v[0])
    if not row or row["uninstalled_at"]:
        return None
    return v[0], v[1], row


def _tenant_info(tenant_id):
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT t.name, COALESCE(cp.is_b2b, FALSE) AS is_b2b
                         FROM tenants t LEFT JOIN company_profiles cp ON cp.tenant_id = t.id
                        WHERE t.id=%s""", (tenant_id,))
        return cur.fetchone() or {}
    finally:
        cur.close(); conn.close()


def _state(row):
    out = {"shop": row["shop"], "shop_name": row.get("shop_name") or row["shop"],
           "email": row.get("shop_email") or "", "currency": row.get("currency") or "",
           "linked": bool(row.get("tenant_id"))}
    if row.get("tenant_id"):
        t = _tenant_info(int(row["tenant_id"]))
        out.update(business=t.get("name") or "", is_b2b=bool(t.get("is_b2b")))
        out["products"] = _products_state(row)
        out["quotes"] = _quotes_state(int(row["tenant_id"]))
        seen = row.get("chat_seen_at")
        out["chat"] = {"seen_minutes_ago": None if not seen else max(0, int((time.time() - seen.timestamp()) // 60)),
                       "embed_url": f"shopify://admin/themes/current/editor?context=apps&activateAppId={_key()}/chat"}
    return out


def _quotes_state(tenant_id):
    """Home screen Quotations line (2026-11-02): 'open' on a B2B plan,
    'trial' while the free 2-week trial can still be started, else 'plans'."""
    try:
        import quote_requests
        if quote_requests.allowed(tenant_id):
            return "open"
    except Exception:
        pass
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT trial_granted_at IS NULL FROM tenants WHERE id=%s", (tenant_id,))
        r = cur.fetchone()
        return "trial" if (r and r[0]) else "plans"
    finally:
        cur.close(); conn.close()


def _products_state(row):
    """Home screen, step 1 (Part 2): where the product read is."""
    st = row.get("sync_status") or "queued"
    last = row.get("sync_last_at")
    mins = None
    if last:
        mins = max(0, int((time.time() - last.timestamp()) // 60))
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""SELECT count(*) FROM store_product_off o JOIN shopify_products p
                         ON p.tenant_id=o.tenant_id AND p.doc_id=o.doc_id WHERE p.shop=%s""", (row["shop"],))
        off = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM shopify_products WHERE shop=%s", (row["shop"],))
        held = cur.fetchone()[0]
    finally:
        cur.close(); conn.close()
    return {"status": st, "total": row.get("sync_total"), "done": row.get("sync_done") or 0,
            "skipped": row.get("sync_skipped") or 0, "count": held, "off": off,
            "minutes_ago": mins, "error": bool(row.get("sync_error")) and st == "error"}


def _link(shop, tenant_id, user_id):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE shopify_shops SET tenant_id=%s, owner_user_id=%s, linked_at=NOW(),
                          updated_at=NOW() WHERE shop=%s""", (int(tenant_id), user_id or None, shop))
        cur.execute("UPDATE tenants SET website_platform='shopify' WHERE id=%s AND COALESCE(website_platform,'')=''", (int(tenant_id),))
        # Part 2: read the store's products straight away.
        cur.execute("""UPDATE shopify_shops SET sync_status='queued', sync_total=NULL, sync_done=0,
                          sync_skipped=NULL, sync_error=NULL WHERE shop=%s""", (shop,))
        conn.commit()
    finally:
        cur.close(); conn.close()
    # Part 3: the chat embed needs the business's chat key on the store.
    try:
        set_embed_data(shop)
    except Exception as e:
        print(f"⚠️ shopify embed data {shop}: {e}")


def _send_code(pending_row) -> bool:
    import portal_routes as pr
    return pr._confirm_send_email(pending_row)


def _held(token, flow, shop):
    import signup_confirm
    r = signup_confirm.get(token)
    if not r or r.get("flow") != flow or (r.get("next_url") or "") != shop:
        return None
    return r


def _check_code(r, code):
    """'ok' | 'wrong' | 'stopped'."""
    import signup_confirm
    code = re.sub(r"\D", "", code or "")
    if len(code) == 6 and signup_confirm.email_code_right(r["id"], code):
        signup_confirm.mark(r["id"], "email_ok")
        return "ok"
    return "stopped" if signup_confirm.wrong_code(r["id"]) else "wrong"


@shopify_bp.route("/api/signup", methods=["POST"])
@public_route
def api_signup():
    w = _who()
    if not w:
        return _bad_token()
    shop, user_id, row = w
    if row.get("tenant_id"):
        return jsonify(_state(row))
    d = request.get_json(silent=True) or {}
    first = (d.get("first_name") or "").strip()[:80]
    last = (d.get("last_name") or "").strip()[:80]
    sell_to = "b2b" if d.get("sell_to") == "b2b" else ("retail" if d.get("sell_to") == "retail" else "")
    email = (row.get("shop_email") or "").strip().lower()
    if not sell_to:
        return jsonify({"error": "Please choose who you sell to."}), 400
    if not first or not last:
        return jsonify({"error": "Please enter your first and last name."}), 400
    if not email:
        return jsonify({"error": "Your Shopify store has no email address. Add one in Shopify settings, then try again."}), 400

    import portal_routes as pr
    import signup_confirm
    domain = (row.get("primary_domain") or shop).lower()
    conn = get_db_connection(); cur = conn.cursor()
    try:
        in_use = pr._email_in_use(cur, email)
        cur.execute("SELECT 1 FROM tenants WHERE regexp_replace(lower(domain), '^www\\.', '')"
                    " = regexp_replace(%s, '^www\\.', '') LIMIT 1", (domain,))
        taken = cur.fetchone() is not None
    finally:
        cur.close(); conn.close()
    if in_use or taken:
        return jsonify({"exists": True, "email": email}), 409

    country = (row.get("country") or "").upper() or None
    args = dict(first_name=first, last_name=last, email=email, phone_number="",
                tenant_domain=domain, hear_about_us="shopify_app", business_country=country,
                signup_channel="website" if pr.NEW_WEBSITE_SIGNUP else None,
                business_name=row.get("shop_name") or None,
                website_platform="shopify", selling_currency=(row.get("currency") or "USD").upper())
    if sell_to == "b2b":
        args.update(signup_channel="both", company=dict(
            registered_name=row.get("shop_name") or domain, reg_country=country,
            address_line1=row.get("address1") or "", city=row.get("city") or "",
            postcode=row.get("zip") or ""))
    token = signup_confirm.start(kind="signup", flow=_CODE_FLOW_NEW, creator="web", args=args,
                                 form={"sell_to": sell_to}, pw_hash=pr.hash_password(_secrets.token_urlsafe(24)),
                                 phone="", email=email, wa_ok=True, next_url=shop)
    if not _send_code(signup_confirm.get(token)):
        return jsonify({"error": f"We couldn't send the code to {email}. Please try again."}), 502
    return jsonify({"token": token, "email": email})


@shopify_bp.route("/api/signup/confirm", methods=["POST"])
@public_route
def api_signup_confirm():
    w = _who()
    if not w:
        return _bad_token()
    shop, user_id, row = w
    d = request.get_json(silent=True) or {}
    r = _held(d.get("token"), _CODE_FLOW_NEW, shop)
    if not r:
        return jsonify({"error": "This code has run out. Please start again.", "restart": True}), 410
    res = _check_code(r, d.get("code"))
    if res != "ok":
        return jsonify({"error": "That code isn't right. Please check the email and try again." if res == "wrong"
                        else "Too many wrong codes, so we stopped here. Please start again.",
                        "restart": res == "stopped"}), 400

    import portal_routes as pr
    import signup_confirm
    args = dict(r["args"] or {})
    company = args.pop("company", None)
    made = pr._register_web_merchant(**args, password="", pw_hash=r["pw_hash"], confirmed=True)
    if not isinstance(made, dict) or not made.get("ok"):
        signup_confirm.drop(r["id"])
        return jsonify({"exists": True, "email": args.get("email")}), 409
    tid = int(made["tenant_id"])
    if company:
        pr._save_company_profile(tid, company)
    signup_confirm.finish(r["id"])
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1", (tid,))
    cid = int(cur.fetchone()[0])
    cur.close(); conn.close()
    pr._send_welcome_after_confirm(cid)
    _link(shop, tid, user_id)
    pr.insert_audit_log(action="shopify_store_linked", tenant_id=tid,
                        details={"shop": shop, "how": "new_account"})
    return jsonify(_state(_shop_row(shop)))


@shopify_bp.route("/api/link", methods=["POST"])
@public_route
def api_link():
    w = _who()
    if not w:
        return _bad_token()
    shop, user_id, row = w
    if row.get("tenant_id"):
        return jsonify(_state(row))
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()[:255]
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "Please enter the email you use to log in to PhiXtra."}), 400
    import signup_confirm
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT id, tenant_id, first_name FROM customers WHERE lower(email)=%s ORDER BY id LIMIT 1",
                    (email,))
        c = cur.fetchone()
    finally:
        cur.close(); conn.close()
    # Same answer whether or not the email has an account (no account fishing).
    token = signup_confirm.start(kind="login", flow=_CODE_FLOW_LINK, phone="", email=email,
                                 args={"first_name": (c or {}).get("first_name") or ""},
                                 customer_id=(c or {}).get("id"), wa_ok=True, next_url=shop)
    if c:
        _send_code(signup_confirm.get(token))
    return jsonify({"token": token, "email": email})


@shopify_bp.route("/api/link/confirm", methods=["POST"])
@public_route
def api_link_confirm():
    w = _who()
    if not w:
        return _bad_token()
    shop, user_id, row = w
    d = request.get_json(silent=True) or {}
    r = _held(d.get("token"), _CODE_FLOW_LINK, shop)
    if not r:
        return jsonify({"error": "This code has run out. Please start again.", "restart": True}), 410
    if not r.get("customer_id"):
        import signup_confirm
        stopped = signup_confirm.wrong_code(r["id"])
        return jsonify({"error": "That code isn't right. Please check the email and try again.",
                        "restart": stopped}), 400
    res = _check_code(r, d.get("code"))
    if res != "ok":
        return jsonify({"error": "That code isn't right. Please check the email and try again." if res == "wrong"
                        else "Too many wrong codes, so we stopped here. Please start again.",
                        "restart": res == "stopped"}), 400
    import portal_routes as pr
    import signup_confirm
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (int(r["customer_id"]),))
    tid = int(cur.fetchone()[0])
    cur.execute("""SELECT shop FROM shopify_shops WHERE tenant_id=%s AND shop<>%s
                     AND uninstalled_at IS NULL LIMIT 1""", (tid, shop))
    other = cur.fetchone()
    cur.close(); conn.close()
    signup_confirm.finish(r["id"])
    if other:
        return jsonify({"error": f"This PhiXtra account is already linked to another Shopify store "
                                 f"({other[0]}). Contact support@phixtra.com to move it."}), 409
    _link(shop, tid, user_id)
    pr.insert_audit_log(action="shopify_store_linked", tenant_id=tid,
                        details={"shop": shop, "how": "existing_account"})
    return jsonify(_state(_shop_row(shop)))


@shopify_bp.route("/api/sync", methods=["POST"])
@public_route
def api_sync():
    """"Read products again" on the Home screen (Part 2)."""
    w = _who()
    if not w:
        return _bad_token()
    shop, _user, row = w
    if not row.get("tenant_id"):
        return jsonify({"error": "Link this store to a PhiXtra account first."}), 400
    import shopify_sync
    shopify_sync.request_full_read(shop)
    return jsonify(_state(_shop_row(shop)))


@shopify_bp.route("/api/trial", methods=["POST"])
@public_route
def api_trial():
    """"Start free trial" on the Home screen: the business's one 2-week trial
    (B2B Growth for B2B businesses), same as Billing › Start my AI trial.
    Only the Shopify user who created or linked the account can start it."""
    w = _who()
    if not w:
        return _bad_token()
    shop, user_id, row = w
    if not row.get("tenant_id"):
        return jsonify({"error": "Link this store to a PhiXtra account first."}), 400
    if not user_id or user_id != (row.get("owner_user_id") or ""):
        return jsonify({"error": "Only the person who connected this store to PhiXtra can start the trial."}), 403
    import portal_routes as pr
    tid = int(row["tenant_id"])
    if pr._start_wa_ai_trial(tid):
        pr.insert_audit_log(action="trial_started", tenant_id=tid, details={"from": "shopify_app", "shop": shop})
        out = _state(_shop_row(shop)); out["trial_started"] = True
        return jsonify(out)
    return jsonify({"error": "The free trial has already been used on this account. Choose a plan in PhiXtra to continue."}), 409


@shopify_bp.route("/api/open", methods=["POST"])
@public_route
def api_open():
    """'Open PhiXtra': a one-time sign-in link (60 s) for the Shopify user who
    created or linked the account. Anyone else gets the normal log-in page."""
    w = _who()
    if not w:
        return _bad_token()
    shop, user_id, row = w
    if not row.get("tenant_id"):
        return jsonify({"error": "not_linked"}), 400
    d = request.get_json(silent=True) or {}
    nxt = d.get("next") if d.get("next") in ("/dashboard", "/quotations/requests", "/data-sources",
                                             "/products/store?offer=off", "/billing/plans") else "/dashboard"
    if not user_id or user_id != (row.get("owner_user_id") or ""):
        return jsonify({"url": "https://portal.phixtra.com/login"})
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1", (int(row["tenant_id"]),))
        cid = cur.fetchone()
        if not cid:
            return jsonify({"url": "https://portal.phixtra.com/login"})
        tok = _secrets.token_urlsafe(32)
        cur.execute("DELETE FROM shopify_sso WHERE expires_at < NOW() - INTERVAL '1 day'")
        cur.execute("""INSERT INTO shopify_sso (token_hash, customer_id, next_path, expires_at)
                       VALUES (%s, %s, %s, NOW() + INTERVAL '60 seconds')""",
                    (hashlib.sha256(tok.encode()).hexdigest(), int(cid[0]), nxt))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return jsonify({"url": f"https://portal.phixtra.com/shopify/sso?t={tok}"})


@shopify_bp.route("/sso")
@public_route
def sso():
    tok = request.args.get("t") or ""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE shopify_sso SET used_at=NOW()
                        WHERE token_hash=%s AND used_at IS NULL AND expires_at > NOW()
                    RETURNING customer_id, next_path""", (hashlib.sha256(tok.encode()).hexdigest(),))
        r = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not r:
        return redirect("/login")
    import portal_routes as pr
    pr._log_in_owner(int(r[0]))
    return redirect(pr._safe_next(r[1]) or "/dashboard")
