"""
shopify_sync.py — PhiXtra for Shopify, Part 2: product sync (2 Nov 2026;
plan https://claude.ai/artifact/EXePiufBVnMBtseuxcGSPh).

A linked store's products reach the AI agent the same way WooCommerce
products do: as documents written by phixtra-data-sync (/sync/batch), so
AI answers, product cards and photo search work unchanged. Photos go into
the product photo gallery (product_photos, source 'shopify', up to the
plan's limit), labelled from short Shopify alt text only.

  First read / nightly check: a Shopify bulk operation reads every product;
      products no longer on sale are removed.
  Live updates: product + stock webhooks queue product ids; each run of
      this script (cron, every minute) re-reads only those products.
  On sale = status ACTIVE and published to the Online Store (publishedAt).

Run:  python shopify_sync.py           (queue + reads; cron every minute)
      python shopify_sync.py nightly   (queue a full read for every store)
"""
import datetime as _dt
import fcntl
import hashlib
import json
import sys
import time

import psycopg2.extras
import requests

from db import get_db_connection
import shopify_app as sa

DATA_SYNC_URL = "http://127.0.0.1:8010/sync/batch"
BATCH = 40
MAX_OPTIONS_IN_TEXT = 30
PHOTO_FINGERPRINTS_PER_RUN = 40

_PRODUCT_FIELDS = """
  id legacyResourceId title handle description status publishedAt vendor productType tags
"""

_BULK_Q = """{ products { edges { node { %s
  variants { edges { node { id title price sku availableForSale } } }
  media { edges { node { ... on MediaImage { id image { url altText } } } } }
  collections { edges { node { id title } } }
} } } }""" % _PRODUCT_FIELDS

_ONE_Q = """query($id: ID!) { product(id: $id) { %s
  variants(first: 250) { nodes { id title price sku availableForSale } }
  media(first: 20) { nodes { ... on MediaImage { id image { url altText } } } }
  collections(first: 25) { nodes { id title } }
} }""" % _PRODUCT_FIELDS

_INV_Q = """query($id: ID!) { inventoryItem(id: $id) { variant { product { id } } } }"""

_BULK_RUN = """mutation($q: String!) { bulkOperationRunQuery(query: $q) {
  bulkOperation { id status } userErrors { field message } } }"""

_BULK_GET = """query($id: ID!) { node(id: $id) { ... on BulkOperation {
  id status errorCode objectCount url } } }"""


# ── Tables ───────────────────────────────────────────────────────────────
def ensure_tables():
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""SELECT column_name FROM information_schema.columns WHERE table_name='shopify_shops'""")
        have = {r[0] for r in cur.fetchall()}
        for col, typ in (("sync_status", "TEXT"),        # queued | reading | importing | done | error | list_in_use
                         ("sync_bulk_id", "TEXT"),
                         ("sync_total", "INTEGER"),       # products on sale (last read)
                         ("sync_done", "INTEGER"),
                         ("sync_skipped", "INTEGER"),     # drafts / archived / hidden
                         ("sync_started_at", "TIMESTAMPTZ"),
                         ("sync_last_at", "TIMESTAMPTZ"),  # last product change applied
                         ("sync_error", "TEXT")):
            if col not in have:
                cur.execute(f"ALTER TABLE shopify_shops ADD COLUMN {col} {typ}")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shopify_products (
                shop        TEXT NOT NULL,
                product_id  TEXT NOT NULL,          -- legacy numeric id
                tenant_id   INTEGER NOT NULL,
                doc_id      TEXT NOT NULL,          -- documents.id ('product-<id>')
                content_hash TEXT,
                synced_at   TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (shop, product_id)
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS shopify_products_tenant ON shopify_products(tenant_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shopify_sync_queue (
                id        BIGSERIAL PRIMARY KEY,
                shop      TEXT NOT NULL,
                kind      TEXT NOT NULL,            -- product | delete | inventory_item
                ref       TEXT NOT NULL,            -- legacy numeric id
                queued_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS shopify_sync_queue_once
                       ON shopify_sync_queue(shop, kind, ref)""")
        # "Offer this product" (2 Nov 2026): store products the business has
        # switched off for the AI agent. phixtra-data-sync keeps them as
        # type 'product_off' on every update, so a re-sync never turns one back on.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS store_product_off (
                tenant_id INTEGER NOT NULL,
                doc_id    TEXT NOT NULL,
                set_by    TEXT,
                set_at    TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (tenant_id, doc_id)
            )""")
        conn.commit()
    finally:
        cur.close(); conn.close()


# ── Small helpers ────────────────────────────────────────────────────────
def _legacy(gid_or_id) -> str:
    s = str(gid_or_id or "")
    return s.rsplit("/", 1)[-1] if s else ""


def doc_id_for(product_id) -> str:
    return f"product-{_legacy(product_id)}"


def admin_url(shop: str, product_id) -> str:
    handle = shop.replace(".myshopify.com", "")
    return f"https://admin.shopify.com/store/{handle}/products/{_legacy(product_id)}"


def queue(shop: str, kind: str, ref) -> None:
    ref = _legacy(ref)
    if not ref:
        return
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO shopify_sync_queue (shop, kind, ref) VALUES (%s,%s,%s)
                       ON CONFLICT (shop, kind, ref) DO NOTHING""", (shop, kind, ref))
        conn.commit()
    finally:
        cur.close(); conn.close()


def request_full_read(shop: str) -> None:
    """First read after linking, the nightly check, and "Read products again"."""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE shopify_shops SET sync_status='queued', sync_error=NULL
                        WHERE shop=%s AND tenant_id IS NOT NULL AND uninstalled_at IS NULL
                          AND COALESCE(sync_status,'') NOT IN ('reading','importing')""", (shop,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _set(shop: str, **cols) -> None:
    if not cols:
        return
    conn = get_db_connection(); cur = conn.cursor()
    try:
        sets = ", ".join(f"{k}=%s" for k in cols)
        cur.execute(f"UPDATE shopify_shops SET {sets} WHERE shop=%s", (*cols.values(), shop))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _tenant(tenant_id: int):
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT t.id, t.domain,
                              (SELECT api_key_plain FROM api_keys k WHERE k.tenant_id=t.id AND k.is_active
                                 AND k.api_key_plain IS NOT NULL ORDER BY k.id DESC LIMIT 1) AS api_key
                         FROM tenants t WHERE t.id=%s""", (tenant_id,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


# ── Shopify product → PhiXtra document ───────────────────────────────────
def on_sale(p: dict) -> bool:
    return (p.get("status") or "").upper() == "ACTIVE" and bool(p.get("publishedAt"))


def _money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return str(v)


def to_item(p: dict, shop_row: dict) -> dict:
    """One product in the shape the WordPress plugin sends to /sync/batch."""
    pid = _legacy(p.get("legacyResourceId") or p.get("id"))
    variants = p.get("variants") or []
    prices = []
    for v in variants:
        try:
            prices.append(float(v.get("price")))
        except Exception:
            pass
    in_stock = any(bool(v.get("availableForSale")) for v in variants)
    cur = (shop_row.get("currency") or "").upper()[:3]
    text = (p.get("description") or "").strip()
    real_options = [v for v in variants if (v.get("title") or "").strip().lower() != "default title"]
    if real_options:
        lines = [f"{v.get('title')}: {cur} {_money(v.get('price'))}"
                 + ("" if v.get("availableForSale") else " (sold out)")
                 for v in real_options[:MAX_OPTIONS_IN_TEXT]]
        more = len(real_options) - MAX_OPTIONS_IN_TEXT
        text = (text + "\n\nOptions:\n" + "\n".join(lines)
                + (f"\n…and {more} more options" if more > 0 else "")).strip()
    skus = []
    for v in variants:
        s = (v.get("sku") or "").strip()
        if s and s not in skus:
            skus.append(s)
    cats = [c for c in [p.get("productType")] + [c.get("title") for c in (p.get("collections") or [])]
            if c and c.lower() != "home page"]
    images = [m for m in (p.get("media") or []) if (m.get("image") or {}).get("url")]
    host = shop_row.get("primary_domain") or shop_row["shop"]
    doc = {
        "title": (p.get("title") or "").strip(),
        "content": text,
        "url": f"https://{host}/products/{p.get('handle')}",
        "sku": ", ".join(skus[:5]),
        "brand": (p.get("vendor") or "").strip(),
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "in_stock": in_stock,
        "categories": list(dict.fromkeys(cats)),
        "image_url": images[0]["image"]["url"] if images else "",
        "currency": cur,
    }
    return {"id": f"product:{pid}", "type": "product", "wp_id": int(pid),
            "updated_at_utc": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "doc": doc, "raw": {"name": doc["title"]},
            "_photos": [((m["image"].get("altText") or "").strip(), m["image"]["url"]) for m in images],
            "_pid": pid}


def _hash(item: dict) -> str:
    body = json.dumps({"doc": item["doc"], "photos": item["_photos"]}, sort_keys=True, default=str)
    return hashlib.sha1(body.encode()).hexdigest()


def photo_label(alt: str, title: str):
    """Short alt text the owner wrote ("Back", "Red front") becomes the photo
    label. Long descriptions (or the product name) stay unlabelled: guessing
    a label from a sentence could send the wrong photo."""
    import product_photos as pp
    alt = " ".join((alt or "").split())
    if not alt or alt.lower() == (title or "").strip().lower() or len(alt.split()) > 4:
        return None, None
    return pp.label_from_text(alt)


# ── Sending to phixtra-data-sync ─────────────────────────────────────────
class ListInUse(Exception):
    pass


def _post(tenant: dict, shop_row: dict, mode: str, items: list) -> None:
    host = shop_row.get("primary_domain") or shop_row["shop"]
    body = {"tenant_id": tenant["domain"], "site_url": f"https://{host}", "mode": mode,
            "items": [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]}
    r = requests.post(DATA_SYNC_URL, json=body, timeout=180,
                      headers={"Authorization": f"Bearer {tenant['api_key']}",
                               "X-Phixtra-Tenant": tenant["domain"]})
    if r.status_code == 409 and "product_list_in_use" in r.text:
        raise ListInUse()
    if r.status_code != 200:
        raise RuntimeError(f"data-sync {r.status_code}: {r.text[:300]}")


def _save_photos(cur, tenant_id: int, item: dict) -> None:
    import product_photos as pp
    items = []
    for alt, url in item["_photos"]:
        view, colour = photo_label(alt, item["doc"]["title"])
        items.append((view, colour, url[:1024]))
    pp.sync_links(cur, tenant_id, f"d:{doc_id_for(item['_pid'])}", items, "shopify", "Shopify")


def apply(shop_row: dict, tenant: dict, items: list, removed: list, force: bool = False) -> int:
    """Upsert `items` (only changed ones unless force) and remove `removed`
    (legacy ids). Returns how many products were written."""
    tid = int(tenant["id"]); shop = shop_row["shop"]
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT product_id, content_hash, tenant_id FROM shopify_products WHERE shop=%s", (shop,))
        known = {r["product_id"]: r for r in cur.fetchall()}
    finally:
        cur.close(); conn.close()

    todo = []
    for it in items:
        h = _hash(it)
        k = known.get(it["_pid"])
        if force or not k or k["content_hash"] != h or int(k["tenant_id"]) != tid:
            it["_hash"] = h
            todo.append(it)

    written = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        _post(tenant, shop_row, "upsert", chunk)
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            for it in chunk:
                k = known.get(it["_pid"])
                if k and int(k["tenant_id"]) != tid:
                    # Store linked to another PhiXtra account since: clear the old copy.
                    _remove_from_tenant(cur, int(k["tenant_id"]), [it["_pid"]])
                _save_photos(cur, tid, it)
                cur.execute("""INSERT INTO shopify_products (shop, product_id, tenant_id, doc_id, content_hash, synced_at)
                               VALUES (%s,%s,%s,%s,%s,NOW())
                               ON CONFLICT (shop, product_id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id,
                                   doc_id=EXCLUDED.doc_id, content_hash=EXCLUDED.content_hash, synced_at=NOW()""",
                            (shop, it["_pid"], tid, doc_id_for(it["_pid"]), it["_hash"]))
            cur.execute("""UPDATE shopify_shops SET sync_last_at=NOW(),
                              sync_done = CASE WHEN sync_status='importing' THEN COALESCE(sync_done,0)+%s ELSE sync_done END
                            WHERE shop=%s""", (len(chunk), shop))
            conn.commit()
        finally:
            cur.close(); conn.close()
        written += len(chunk)

    gone = [pid for pid in removed if pid in known]
    if gone:
        remove(shop, gone)
    return written


def _remove_from_tenant(cur, tenant_id: int, pids: list) -> None:
    ids = [doc_id_for(p) for p in pids]
    cur.execute("DELETE FROM documents WHERE tenant_id=%s AND id = ANY(%s)", (tenant_id, ids))
    cur.execute("DELETE FROM product_photos WHERE tenant_id=%s AND product_key = ANY(%s)",
                (tenant_id, ["d:" + i for i in ids]))
    cur.execute("DELETE FROM store_product_off WHERE tenant_id=%s AND doc_id = ANY(%s)", (tenant_id, ids))


def remove(shop: str, pids: list) -> int:
    """Products deleted in Shopify, or no longer on sale: off the AI agent."""
    if not pids:
        return 0
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT product_id, tenant_id FROM shopify_products WHERE shop=%s AND product_id = ANY(%s)",
                    (shop, [str(p) for p in pids]))
        by_tenant = {}
        for r in cur.fetchall():
            by_tenant.setdefault(int(r["tenant_id"]), []).append(r["product_id"])
        for tid, ps in by_tenant.items():
            _remove_from_tenant(cur, tid, ps)
        cur.execute("DELETE FROM shopify_products WHERE shop=%s AND product_id = ANY(%s)", (shop, [str(p) for p in pids]))
        cur.execute("UPDATE shopify_shops SET sync_last_at=NOW() WHERE shop=%s", (shop,))
        conn.commit()
        return sum(len(v) for v in by_tenant.values())
    finally:
        cur.close(); conn.close()


def forget_shop(shop: str) -> int:
    """shop/redact (48 h after uninstall): remove every product we hold."""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT product_id FROM shopify_products WHERE shop=%s", (shop,))
        pids = [r[0] for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    n = remove(shop, pids)
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM shopify_sync_queue WHERE shop=%s", (shop,))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return n


# ── Full read (bulk operation) ───────────────────────────────────────────
def _parse_bulk(lines) -> list:
    products, order = {}, []
    for line in lines:
        if not line.strip():
            continue
        o = json.loads(line)
        gid = o.get("id") or ""
        parent = o.get("__parentId")
        if not parent:
            if "/Product/" in gid:
                o.update(variants=[], media=[], collections=[])
                products[gid] = o
                order.append(gid)
            continue
        p = products.get(parent)
        if not p:
            continue
        if "/ProductVariant/" in gid:
            p["variants"].append(o)
        elif "/MediaImage/" in gid and (o.get("image") or {}).get("url"):
            p["media"].append(o)
        elif "/Collection/" in gid:
            p["collections"].append(o)
    return [products[g] for g in order]


def _start_bulk(shop_row: dict) -> None:
    shop = shop_row["shop"]
    d = sa.graphql(shop, _BULK_RUN, {"q": _BULK_Q})["bulkOperationRunQuery"]
    if d.get("userErrors"):
        raise RuntimeError(json.dumps(d["userErrors"])[:300])
    _set(shop, sync_status="reading", sync_bulk_id=d["bulkOperation"]["id"],
         sync_started_at=_dt.datetime.now(_dt.timezone.utc), sync_error=None)


def _finish_bulk(shop_row: dict, tenant: dict) -> None:
    shop = shop_row["shop"]
    op = sa.graphql(shop, _BULK_GET, {"id": shop_row["sync_bulk_id"]})["node"] or {}
    status = op.get("status")
    if status in ("CREATED", "RUNNING"):
        return
    if status != "COMPLETED":
        _set(shop, sync_status="error", sync_error=f"Shopify read {status} {op.get('errorCode') or ''}".strip())
        return
    lines = []
    if op.get("url"):  # no url = the store has no products
        r = requests.get(op["url"], timeout=120)
        r.raise_for_status()
        lines = r.text.splitlines()
    products = _parse_bulk(lines)
    sale = [p for p in products if on_sale(p)]
    items = [to_item(p, shop_row) for p in sale]
    keep = {it["_pid"] for it in items}
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT product_id FROM shopify_products WHERE shop=%s", (shop,))
        removed = [r[0] for r in cur.fetchall() if r[0] not in keep]
    finally:
        cur.close(); conn.close()
    _set(shop, sync_status="importing", sync_total=len(items), sync_done=0,
         sync_skipped=len(products) - len(sale))
    try:
        apply(shop_row, tenant, items, removed)
    except ListInUse:
        _set(shop, sync_status="list_in_use")
        return
    _set(shop, sync_status="done", sync_done=len(items), sync_last_at=_dt.datetime.now(_dt.timezone.utc))


# ── Live updates (webhook queue) ─────────────────────────────────────────
def _fetch_product(shop: str, pid: str):
    p = sa.graphql(shop, _ONE_Q, {"id": f"gid://shopify/Product/{pid}"}).get("product")
    if not p:
        return None
    p["variants"] = (p.get("variants") or {}).get("nodes") or []
    p["media"] = [m for m in ((p.get("media") or {}).get("nodes") or []) if m and m.get("image")]
    p["collections"] = (p.get("collections") or {}).get("nodes") or []
    return p


def _run_queue(shop_row: dict, tenant: dict) -> int:
    shop = shop_row["shop"]
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""DELETE FROM shopify_sync_queue WHERE id IN (
                         SELECT id FROM shopify_sync_queue WHERE shop=%s ORDER BY id LIMIT 200)
                       RETURNING kind, ref""", (shop,))
        jobs = cur.fetchall()
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not jobs:
        return 0
    pids, gone = [], []
    for j in jobs:
        if j["kind"] == "delete":
            gone.append(j["ref"])
        elif j["kind"] == "product":
            pids.append(j["ref"])
        elif j["kind"] == "inventory_item":
            try:
                d = sa.graphql(shop, _INV_Q, {"id": f"gid://shopify/InventoryItem/{j['ref']}"})
                prod = (((d.get("inventoryItem") or {}).get("variant") or {}).get("product") or {})
                if prod.get("id"):
                    pids.append(_legacy(prod["id"]))
            except Exception as e:
                print(f"⚠️ shopify inventory item {shop} {j['ref']}: {e}")
    items = []
    for pid in dict.fromkeys(pids):
        if pid in gone:
            continue
        p = _fetch_product(shop, pid)
        if p and on_sale(p):
            items.append(to_item(p, shop_row))
        else:
            gone.append(pid)
    try:
        apply(shop_row, tenant, items, list(dict.fromkeys(gone)))
    except ListInUse:
        _set(shop, sync_status="list_in_use")
    return len(jobs)


def _fingerprints(tenant_id: int) -> int:
    """Photo search: fingerprint a few new Shopify photos per run."""
    import product_photos as pp
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    done = 0
    try:
        cur.execute("""SELECT DISTINCT product_key FROM product_photos WHERE tenant_id=%s AND source='shopify'
                         AND image_embedding IS NULL AND suggested_view IS NULL LIMIT 20""", (tenant_id,))
        for r in cur.fetchall():
            done += pp.fill_suggestions(cur, tenant_id, r["product_key"], most=8) or 0
            conn.commit()
            if done >= PHOTO_FINGERPRINTS_PER_RUN:
                break
    finally:
        cur.close(); conn.close()
    return done


# ── One run (cron) ───────────────────────────────────────────────────────
def run_once() -> dict:
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT * FROM shopify_shops WHERE tenant_id IS NOT NULL AND uninstalled_at IS NULL
                        ORDER BY shop""")
        shops = cur.fetchall()
    finally:
        cur.close(); conn.close()
    out = {}
    for row in shops:
        shop = row["shop"]
        tenant = _tenant(int(row["tenant_id"]))
        if not tenant or not tenant.get("api_key") or not tenant.get("domain"):
            _set(shop, sync_status="error", sync_error="PhiXtra account has no active key")
            continue
        try:
            st = row.get("sync_status") or ""
            if st == "list_in_use":
                # Retry once an hour: the business may have switched back to store products.
                last = row.get("sync_started_at")
                if not last or (time.time() - last.timestamp()) > 3600:
                    st = "queued"
            if st == "queued" or not st:
                _start_bulk(row)
                out[shop] = "read started"
                continue
            if st == "reading":
                _finish_bulk(row, tenant)
                out[shop] = "read checked"
            elif st == "importing":
                # A run stopped part-way: start the read again.
                started = row.get("sync_started_at")
                if not started or (time.time() - started.timestamp()) > 1800:
                    _set(shop, sync_status="queued")
                continue
            n = _run_queue(_shop_fresh(shop), tenant)
            if n:
                out[shop] = f"{n} updates"
            _fingerprints(int(tenant["id"]))
        except Exception as e:
            msg = str(e)[:300]
            print(f"⚠️ shopify sync {shop}: {msg}")
            if msg == "no_token":
                _set(shop, sync_error="Store needs to open the PhiXtra app again")
            else:
                _set(shop, sync_status="error" if (row.get("sync_status") in ("queued", "reading", None, "")) else row.get("sync_status"),
                     sync_error=msg)
    # Part 4: Shopify B2B companies, locations and contacts.
    try:
        import shopify_b2b
        for shop, res in shopify_b2b.run_due():
            out[shop + " b2b"] = res
    except Exception as e:
        print(f"⚠️ shopify b2b run: {e}")
    return out


def _shop_fresh(shop: str) -> dict:
    return sa._shop_row(shop)


def nightly() -> int:
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE shopify_shops SET sync_status='queued', sync_error=NULL
                        WHERE tenant_id IS NOT NULL AND uninstalled_at IS NULL
                          AND COALESCE(sync_status,'') NOT IN ('reading','importing')""")
        n = cur.rowcount
        conn.commit()
        try:
            import shopify_b2b
            shopify_b2b.nightly()
        except Exception as e:
            print(f"⚠️ shopify b2b nightly: {e}")
        return n
    finally:
        cur.close(); conn.close()


if __name__ == "__main__":
    lock = open("/tmp/phixtra_shopify_sync.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)  # previous run still busy
    ensure_tables()
    if len(sys.argv) > 1 and sys.argv[1] == "nightly":
        print(_dt.datetime.now().isoformat(timespec="seconds"), "nightly queued", nightly())
    else:
        res = run_once()
        if res:
            print(_dt.datetime.now().isoformat(timespec="seconds"), res)
