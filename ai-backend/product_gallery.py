"""Product photo galleries for the AI's product cards (2026-10-25).

After the AI has answered, each recommended product gets `images`: every photo
of it, main first, with the label the business confirmed (Front, Back… and a
colour). Added after the answer, so it costs no AI tokens. The website chat
box shows them as a gallery; the WhatsApp gateway uses them for "Reply MORE"
and "show me the back". Photos and labels live in product_photos (portal).

Keys: 'd:<documents.id>' (WooCommerce / store products), and for products
from a spreadsheet the portal's 'p:<products.id>' (matched by name, which is
how the spreadsheet import names its documents)."""
import psycopg2.extras

from db import get_db_connection

VIEW_TEXT = {"front": "Front", "back": "Back", "side": "Side", "closeup": "Close-up", "worn": "Worn / In use", "other": ""}


def _label(view, colour):
    return " · ".join(x for x in (VIEW_TEXT.get(view or "", ""), colour or "") if x)


def attach(tenant_id: int, products: list) -> list:
    """Adds `images` to each product dict (in place) and returns the list."""
    ids = [p.get("id") for p in products if p.get("id")]
    by_doc = {}
    if ids:
        conn = get_db_connection()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT d.id AS doc_id, ph.url, ph.thumb_url, ph.view_label, ph.colour, ph.position
                FROM documents d
                JOIN product_photos ph ON ph.tenant_id = d.tenant_id AND (
                         ph.product_key = 'd:' || d.id
                      OR ((d.id LIKE 'xls-%%' OR d.id LIKE 'man-%%') AND ph.product_key IN (
                             SELECT 'p:' || p.id::text FROM products p
                             WHERE p.tenant_id = d.tenant_id AND p.name = d.title AND p.is_active)))
                WHERE d.tenant_id = %s AND d.id = ANY(%s)
                ORDER BY d.id, ph.position, ph.id""", (tenant_id, ids))
            for r in cur.fetchall():
                by_doc.setdefault(r["doc_id"], []).append(r)
        except Exception as e:
            print("⚠️ product_gallery.attach:", e)
            by_doc = {}
        finally:
            conn.close()
    for p in products:
        rows = by_doc.get(p.get("id") or "")
        if rows:
            p["images"] = [{"url": r["url"], "thumb": r["thumb_url"] or r["url"], "view": r["view_label"] or "",
                            "colour": r["colour"] or "", "label": _label(r["view_label"], r["colour"])} for r in rows]
            p["image_url"] = rows[0]["url"]          # the main photo, as the business ordered it
        elif p.get("image_url"):
            p["images"] = [{"url": p["image_url"], "thumb": p["image_url"], "view": "", "colour": "", "label": ""}]
        else:
            p["images"] = []
    return products
