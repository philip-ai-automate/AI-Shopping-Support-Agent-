"""Products a business adds itself (My Products, or the WhatsApp sign-up chat)
reach the AI (2026-10-25).

Before this they lived only in the products table, which the WhatsApp ORDER
flow reads but the AI's answers never search (they search documents). So a
business that added products by hand still got "we don't have that".

Each hand-added product gets one document 'man-<tenant>-<products.id>'.
Products that came from an uploaded list already have their own 'xls-'
document (same name), so they are skipped here. Only rows that changed are
written, so the 5-minute run (cron: own_products_ai.py --all) is cheap.
"""
import sys

import psycopg2.extras

from db import get_db_connection
import selling_currency as _selling_currency


def _prefix(tenant_id: int) -> str:
    return f"man-{int(tenant_id)}-"


def sync(tenant_id: int) -> tuple[int, int]:
    """Bring this business's hand-added products onto the AI.
    Returns (written, removed)."""
    tenant_id = int(tenant_id)
    prefix = _prefix(tenant_id)
    currency = _selling_currency.tenant_currency(tenant_id) or None
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT p.id, p.name, p.description, p.price, p.stock_quantity, p.category,
                              p.image_url, p.currency, p.is_service, p.price_on_request
                         FROM products p
                        WHERE p.tenant_id = %s AND p.is_active
                          AND NOT EXISTS (SELECT 1 FROM documents d
                                           WHERE d.tenant_id = p.tenant_id AND d.id LIKE %s
                                             AND d.title = p.name)""",
                    (tenant_id, f"xls-{tenant_id}-%"))
        rows = cur.fetchall()
        cur.execute("""SELECT id, title, content, price_min, in_stock, categories_text, image_url, currency
                         FROM documents WHERE tenant_id = %s AND id LIKE %s""", (tenant_id, prefix + "%"))
        before = {r["id"]: r for r in cur.fetchall()}
        written, now_ids = 0, set()
        for p in rows:
            doc_id = prefix + str(p["id"])
            now_ids.add(doc_id)
            name = (p["name"] or "").strip()
            cat = (p["category"] or "").strip()
            desc = (p["description"] or "").strip()
            # Services (2026-10-25): always available; "Price: on request"
            # tells the AI to pass price questions to the team.
            lines = (name, "Type: Service" if p["is_service"] else "",
                     "Price: on request (the team confirms the price)" if p["price_on_request"] else "",
                     f"Category: {cat}" if cat else "", desc)
            want = {
                "title": name[:512],
                "content": "\n".join(x for x in lines if x),
                "price_min": (float(p["price"]) if p["price"] and float(p["price"]) > 0
                              and not p["price_on_request"] else None),
                "in_stock": bool(p["is_service"]) or int(p["stock_quantity"] or 0) > 0,
                "categories_text": cat or None,
                "image_url": p["image_url"] or None,
                "currency": p["currency"] or currency,
            }
            old = before.get(doc_id)
            if old and all((float(old[k]) if k == "price_min" and old[k] is not None else old[k]) == v
                           for k, v in want.items()):
                continue
            cur.execute("""
                INSERT INTO documents (id, tenant_id, type, title, content, price_min, price_max, in_stock,
                                       categories_text, image_url, currency, updated_by, updated_at)
                VALUES (%s, %s, 'product', %s, %s, %s, %s, %s, %s, %s, %s, 'my-products', NOW())
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title, price_min = EXCLUDED.price_min, price_max = EXCLUDED.price_max,
                    in_stock = EXCLUDED.in_stock, categories_text = EXCLUDED.categories_text,
                    image_url = EXCLUDED.image_url, currency = EXCLUDED.currency, updated_by = EXCLUDED.updated_by,
                    updated_at = NOW(),
                    content = EXCLUDED.content,
                    embedding = CASE WHEN documents.content IS DISTINCT FROM EXCLUDED.content
                                       OR documents.title IS DISTINCT FROM EXCLUDED.title
                                     THEN NULL ELSE documents.embedding END
            """, (doc_id, tenant_id, want["title"], want["content"], want["price_min"], want["price_min"],
                  want["in_stock"], want["categories_text"], want["image_url"], want["currency"]))
            written += 1
        gone = [i for i in before if i not in now_ids]
        if gone:
            cur.execute("DELETE FROM documents WHERE tenant_id = %s AND id = ANY(%s)", (tenant_id, gone))
        conn.commit()
        return written, len(gone)
    finally:
        cur.close()
        conn.close()


def sync_all() -> None:
    """Every business with hand-added products, or with old ones to remove."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT tenant_id FROM products
                   UNION SELECT DISTINCT tenant_id FROM documents WHERE id LIKE 'man-%%'""")
    ids = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    for tid in ids:
        try:
            w, g = sync(tid)
            if w or g:
                print(f"own_products_ai tenant={tid} written={w} removed={g}")
        except Exception as e:
            print(f"⚠️ own_products_ai tenant={tid}: {e}")


if __name__ == "__main__":
    if "--all" in sys.argv:
        sync_all()
    elif len(sys.argv) > 1:
        print(sync(int(sys.argv[1])))
