"""
re_embed_worker.py — backfill embeddings for documents stored without one.

Run as a cron job every 5 minutes:
    */5 * * * * cd /root/phixtra-app/phixtra-data-sync && python re_embed_worker.py >> /var/log/re_embed_worker.log 2>&1

Documents end up without embeddings when the OpenAI API was unavailable at
sync time. This script finds them, generates embeddings in batches, and writes
them back. It exits immediately if there is nothing to do.
"""

import os
import time
import psycopg2
import psycopg2.extras
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER")
PG_PASS = os.getenv("PG_PASSWORD")
PG_DB   = os.getenv("PG_DB")

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
BATCH_SIZE = int(os.getenv("RE_EMBED_BATCH_SIZE", "50"))


def _pg():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER,
        password=PG_PASS, dbname=PG_DB,
    )


def _embed_batch(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch. Returns None on failure (caller will retry next run)."""
    if not OPENAI_API_KEY:
        print("  ⚠️  OPENAI_API_KEY not set — skipping")
        return None
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        safe = [(t or "empty")[:8000] for t in texts]
        resp = client.embeddings.create(model=OPENAI_EMBED_MODEL, input=safe)
        items = sorted(resp.data, key=lambda x: x.index)
        return [[float(v) for v in item.embedding] for item in items]
    except Exception as e:
        print(f"  ⚠️  OpenAI error: {e}")
        return None


def run():
    try:
        conn = _pg()
    except Exception as e:
        print(f"  ⚠️  DB connection failed: {e}")
        return

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, title, content, sku, brand, categories_text FROM documents WHERE embedding IS NULL LIMIT %s",
            (BATCH_SIZE,),
        )
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        print(f"  ⚠️  DB fetch failed: {e}")
        conn.close()
        return

    if not rows:
        print("✅ No documents missing embeddings.")
        conn.close()
        return

    print(f"Found {len(rows)} document(s) without embeddings. Embedding now…")

    texts = []
    for row in rows:
        parts = []
        if row["title"]:
            parts.append(row["title"])
        if row["brand"]:
            parts.append(f"Brand: {row['brand']}")
        if row["sku"]:
            parts.append(f"SKU: {row['sku']}")
        if row["categories_text"]:
            parts.append(row["categories_text"])
        if row["content"]:
            parts.append((row["content"] or "")[:2000])
        texts.append(" | ".join(parts) or "empty")

    vectors = _embed_batch(texts)
    if vectors is None:
        print("  Embedding failed — will retry next run.")
        conn.close()
        return

    updated = 0
    update_cur = conn.cursor()
    try:
        for row, vec in zip(rows, vectors):
            if not vec:
                continue
            vec_literal = "[" + ",".join(str(x) for x in vec) + "]"
            update_cur.execute(
                "UPDATE documents SET embedding = %s::vector, updated_at = NOW() WHERE id = %s",
                (vec_literal, row["id"]),
            )
            updated += 1
        conn.commit()
        print(f"✅ Updated {updated}/{len(rows)} document(s) with embeddings.")
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
