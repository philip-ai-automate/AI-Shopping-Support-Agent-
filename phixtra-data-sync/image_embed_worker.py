"""
image_embed_worker.py — backfill CLIP image embeddings for documents that
have a photo (image_url) but no image_embedding yet (Visual Product Match,
Pro plan only).

Run as a cron job every 5 minutes, same pattern as re_embed_worker.py:
    */5 * * * * cd /root/phixtra-app/phixtra-data-sync && python image_embed_worker.py >> /var/log/image_embed_worker.log 2>&1

Deliberately decoupled from the live WooCommerce sync request path
(_pg_upsert_docs in main.py) rather than embedding inline there — that sync
already handles many products per request and a synchronous image
download + CLIP call per row would add real latency to a live merchant
sync. This worker picks up anything left behind, on its own schedule, and
exits immediately if there's nothing to do.

Only processes documents belonging to a tenant on the Pro plan
(plans.feat_visual_match) — no point embedding photos nobody can query.
"""

import os
import time

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER")
PG_PASS = os.getenv("PG_PASSWORD")
PG_DB   = os.getenv("PG_DB")

CLIP_EMBEDDER_URL = os.getenv("CLIP_EMBEDDER_URL", "http://127.0.0.1:8020").rstrip("/")
BATCH_SIZE = int(os.getenv("IMAGE_EMBED_BATCH_SIZE", "25"))


def _pg():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER,
        password=PG_PASS, dbname=PG_DB,
    )


def _embed_image_url(image_url: str) -> list[float] | None:
    try:
        img_resp = httpx.get(image_url, timeout=15.0, follow_redirects=True)
        img_resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠️  download failed for {image_url}: {e}")
        return None

    try:
        resp = httpx.post(
            f"{CLIP_EMBEDDER_URL}/embed-image",
            files={"file": ("image.jpg", img_resp.content, "application/octet-stream")},
            timeout=15.0,
        )
    except Exception as e:
        print(f"  ⚠️  clip-embedder unreachable: {e}")
        return None

    if resp.status_code != 200:
        print(f"  ⚠️  embed failed for {image_url}: {resp.status_code} {resp.text[:150]}")
        return None

    return resp.json().get("embedding")


def run():
    try:
        conn = _pg()
    except Exception as e:
        print(f"  ⚠️  DB connection failed: {e}")
        return

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT d.id, d.tenant_id, d.image_url
            FROM documents d
            JOIN tenants t ON t.id = d.tenant_id
            JOIN plans p   ON p.id = t.plan_id
            WHERE d.image_url IS NOT NULL
              AND d.image_url <> ''
              AND d.image_embedding IS NULL
              AND p.feat_visual_match = TRUE
            LIMIT %s
            """,
            (BATCH_SIZE,),
        )
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        print(f"  ⚠️  DB fetch failed: {e}")
        conn.close()
        return

    if not rows:
        print("✅ No product images pending embedding.")
        conn.close()
        return

    print(f"Found {len(rows)} product image(s) to embed…")

    updated = 0
    update_cur = conn.cursor()
    try:
        for row in rows:
            vec = _embed_image_url(row["image_url"])
            if not vec:
                continue
            vec_literal = "[" + ",".join(str(x) for x in vec) + "]"
            update_cur.execute(
                "UPDATE documents SET image_embedding = %s::vector WHERE id = %s AND tenant_id = %s",
                (vec_literal, row["id"], row["tenant_id"]),
            )
            updated += 1
        conn.commit()
        print(f"✅ Embedded {updated}/{len(rows)} product image(s).")
    except Exception as e:
        print(f"  ⚠️  DB update failed (rolled back): {e}")
        conn.rollback()
    finally:
        update_cur.close()
        conn.close()


if __name__ == "__main__":
    start = time.time()
    run()
    print(f"   Completed in {time.time() - start:.1f}s")
