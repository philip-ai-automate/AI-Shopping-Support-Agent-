import os
import json
import re
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import bcrypt
from openai import OpenAI
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────
# PhiXtra Data Sync — pgvector edition
#
# Same HTTP API contract as before — WordPress plugin unchanged.
# Products/content now stored in PostgreSQL `documents` table
# with OpenAI embeddings (text-embedding-3-small, dim=1536).
# ─────────────────────────────────────────────────────────────────────

load_dotenv()

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER")
PG_PASS = os.getenv("PG_PASSWORD")
PG_DB   = os.getenv("PG_DB")

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

SYNC_BATCH_SIZE  = int(os.getenv("SYNC_BATCH_SIZE", "500"))
INTERNAL_ADMIN_SECRET = os.getenv("INTERNAL_ADMIN_SECRET", os.getenv("INTERNAL_CRON_SECRET", "")).strip()

app = FastAPI(title="PhiXtra Data Sync (pgvector)")

SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_\-=]+$")

_progress_lock = threading.Lock()
_progress: Dict[str, Dict[str, Any]] = {}
_last_sync_error: Dict[str, str] = {}

_openai_client: Optional[OpenAI] = None


def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


# ─────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────

def _pg():
    try:
        return psycopg2.connect(
            host=PG_HOST, port=PG_PORT, user=PG_USER,
            password=PG_PASS, dbname=PG_DB,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB connection failed: {str(e)}")


def clean_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    d = d.replace("https://", "").replace("http://", "").split("/")[0]
    return d


def validate_bearer(authorization: Optional[str], x_phixtra_tenant: Optional[str] = None) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing/invalid Authorization Bearer token")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty Bearer token")

    tenant_domain = clean_domain(x_phixtra_tenant) if x_phixtra_tenant else None

    try:
        conn = _pg()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if tenant_domain:
            cur.execute("""
                SELECT k.id, k.tenant_id, k.is_active, k.api_key_hash, t.domain, t.status
                FROM api_keys k
                JOIN tenants t ON t.id = k.tenant_id
                WHERE k.is_active = TRUE AND t.status IN ('active', 'pending') AND LOWER(t.domain) = %s
                LIMIT 500
            """, (tenant_domain,))
        else:
            cur.execute("""
                SELECT k.id, k.tenant_id, k.is_active, k.api_key_hash, t.domain, t.status
                FROM api_keys k
                JOIN tenants t ON t.id = k.tenant_id
                WHERE k.is_active = TRUE AND t.status IN ('active', 'pending')
                LIMIT 2000
            """)
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB query failed: {str(e)}")

    token_bytes = token.encode("utf-8")
    for row in rows:
        h = row.get("api_key_hash") or ""
        try:
            if bcrypt.checkpw(token_bytes, h.encode("utf-8")):
                return dict(row)
        except Exception:
            continue

    raise HTTPException(status_code=401, detail="Invalid or inactive API key")


def make_safe_doc_key(original_id: str, doc_type: str, wp_id: int) -> str:
    if original_id and SAFE_KEY_RE.match(original_id):
        return original_id
    if original_id and ":" in original_id:
        candidate = original_id.replace(":", "-")
        if SAFE_KEY_RE.match(candidate):
            return candidate
    t = re.sub(r"[^A-Za-z0-9_\-=]+", "-", (doc_type or "doc"))
    if not t:
        t = "doc"
    return f"{t}-{wp_id}"


def stamp_sync_complete(tenant_id: int):
    try:
        conn = _pg()
        cur = conn.cursor()
        cur.execute("UPDATE tenants SET last_full_sync_at = NOW() WHERE id = %s", (tenant_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[SYNC] stamp_sync_complete failed (non-fatal): {e}")


def update_tenant_search_settings(tenant_id: int, index_name: str, semantic_config_name: str = "phixtra-semantic"):
    """No-op in pgvector mode — tenant uses documents table directly via tenant_id."""
    pass


# ─────────────────────────────────────────────────────────────────────
# Embedding
# ─────────────────────────────────────────────────────────────────────

def _embed_texts(texts: List[str]) -> List[List[float]]:
    """Generate embeddings for a list of texts.

    Retries up to 3 times with exponential backoff before giving up.
    On total failure returns empty vectors so documents still save (without
    semantic search) and the re-embed worker picks them up later.
    """
    if not texts:
        return []
    if not OPENAI_API_KEY:
        return [[] for _ in texts]

    safe_texts = [(t or "empty")[:8000] for t in texts]
    last_err = None
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            resp = _get_openai().embeddings.create(
                model=OPENAI_EMBED_MODEL,
                input=safe_texts,
            )
            items = sorted(resp.data, key=lambda x: x.index)
            return [[float(v) for v in item.embedding] for item in items]
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                wait = 2 ** attempt  # 1s, 2s
                print(f"[SYNC] embedding attempt {attempt + 1}/{max_attempts} failed: {e}. Retrying in {wait}s…")
                time.sleep(wait)
    print(f"[SYNC] embedding failed after {max_attempts} attempts — documents stored without embeddings. Error: {last_err}")
    return [[] for _ in texts]


def _ensure_hybrid_search_schema() -> None:
    """Idempotent migration: add search_vector column + GIN index + trigger.

    Also backfills any existing rows that have search_vector IS NULL so the
    keyword side of hybrid search works immediately after a deploy.
    """
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, user=PG_USER,
            password=PG_PASS, dbname=PG_DB,
        )
        cur = conn.cursor()
        cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS currency VARCHAR(3)")
        cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS search_vector tsvector")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_search_vector ON documents USING GIN(search_vector)"
        )
        cur.execute("""
            CREATE OR REPLACE FUNCTION documents_search_vector_trigger()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('english',
                    COALESCE(NEW.title, '')          || ' ' ||
                    COALESCE(NEW.content, '')        || ' ' ||
                    COALESCE(NEW.sku, '')            || ' ' ||
                    COALESCE(NEW.brand, '')          || ' ' ||
                    COALESCE(NEW.categories_text, '')
                );
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'tsvector_update'
                      AND tgrelid = 'documents'::regclass
                ) THEN
                    CREATE TRIGGER tsvector_update
                    BEFORE INSERT OR UPDATE ON documents
                    FOR EACH ROW EXECUTE FUNCTION documents_search_vector_trigger();
                END IF;
            END;
            $$
        """)
        # Backfill rows written before this migration
        cur.execute("""
            UPDATE documents
               SET search_vector = to_tsvector('english',
                       COALESCE(title, '')          || ' ' ||
                       COALESCE(content, '')        || ' ' ||
                       COALESCE(sku, '')            || ' ' ||
                       COALESCE(brand, '')          || ' ' ||
                       COALESCE(categories_text, ''))
             WHERE search_vector IS NULL
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("[STARTUP] hybrid search schema ready (search_vector + GIN index + trigger)")
    except Exception as e:
        print(f"[STARTUP] _ensure_hybrid_search_schema warning (non-fatal): {e}")


_ensure_hybrid_search_schema()


def _make_embed_text(d: Dict[str, Any], doc_type: str, title: str) -> str:
    """Build the text to embed for a document."""
    parts = []
    if title:
        parts.append(title)
    sku = (d.get("sku") or "").strip()
    brand = (d.get("brand") or "").strip()
    cats = (d.get("categories_text") or "").strip()
    content = (d.get("content") or "").strip()
    if brand:
        parts.append(f"Brand: {brand}")
    if sku:
        parts.append(f"SKU: {sku}")
    if cats:
        parts.append(cats)
    if content:
        parts.append(content[:2000])
    return " | ".join(parts) or "empty"


# ─────────────────────────────────────────────────────────────────────
# PostgreSQL upsert
# ─────────────────────────────────────────────────────────────────────

def _pg_upsert_docs(tenant_id: int, site_url: str, docs: List[Dict[str, Any]], tenant_domain: str) -> None:
    """Upsert documents into PostgreSQL documents table with embeddings."""
    total = len(docs)
    if total == 0:
        return

    with _progress_lock:
        _progress[tenant_domain] = {
            "status": "running",
            "total_docs": total,
            "uploaded_docs": 0,
            "total_chunks": 1,
            "done_chunks": 0,
            "failed_chunks": 0,
            "last_error": "",
            "updated_at": time.time(),
        }

    try:
        # Build embedding texts
        embed_texts = [d["_embed_text"] for d in docs]
        vectors = _embed_texts(embed_texts)

        conn = _pg()
        cur = conn.cursor()

        upsert_sql = """
            INSERT INTO documents
                (id, tenant_id, type, title, content, url, sku, brand,
                 price_min, price_max, in_stock, categories_text, site_url,
                 image_url, currency, embedding, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, NOW())
            ON CONFLICT (id) DO UPDATE SET
                type = EXCLUDED.type,
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                url = EXCLUDED.url,
                sku = EXCLUDED.sku,
                brand = EXCLUDED.brand,
                price_min = EXCLUDED.price_min,
                price_max = EXCLUDED.price_max,
                in_stock = EXCLUDED.in_stock,
                categories_text = EXCLUDED.categories_text,
                site_url = EXCLUDED.site_url,
                image_url = EXCLUDED.image_url,
                currency = EXCLUDED.currency,
                embedding = EXCLUDED.embedding,
                updated_at = NOW()
        """

        upsert_sql_no_vec = """
            INSERT INTO documents
                (id, tenant_id, type, title, content, url, sku, brand,
                 price_min, price_max, in_stock, categories_text, site_url,
                 image_url, currency, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET
                type = EXCLUDED.type,
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                url = EXCLUDED.url,
                sku = EXCLUDED.sku,
                brand = EXCLUDED.brand,
                price_min = EXCLUDED.price_min,
                price_max = EXCLUDED.price_max,
                in_stock = EXCLUDED.in_stock,
                categories_text = EXCLUDED.categories_text,
                site_url = EXCLUDED.site_url,
                image_url = EXCLUDED.image_url,
                currency = EXCLUDED.currency,
                updated_at = NOW()
        """

        for i, doc in enumerate(docs):
            vec = vectors[i] if i < len(vectors) else []
            price_min = doc.get("price_min")
            price_max = doc.get("price_max")
            in_stock = doc.get("in_stock")
            currency = (doc.get("currency") or "").strip().upper()[:3] or None

            if vec:
                vec_literal = "[" + ",".join(str(x) for x in vec) + "]"
                cur.execute(upsert_sql, (
                    doc["id"], tenant_id, doc["type"],
                    doc["title"], doc["content"], doc["url"],
                    doc["sku"], doc["brand"],
                    price_min, price_max, in_stock,
                    doc["categories_text"], site_url,
                    doc["image_url"], currency, vec_literal,
                ))
            else:
                cur.execute(upsert_sql_no_vec, (
                    doc["id"], tenant_id, doc["type"],
                    doc["title"], doc["content"], doc["url"],
                    doc["sku"], doc["brand"],
                    price_min, price_max, in_stock,
                    doc["categories_text"], site_url,
                    doc["image_url"], currency,
                ))

        conn.commit()
        cur.close()
        conn.close()

        with _progress_lock:
            st = _progress.get(tenant_domain) or {}
            st["uploaded_docs"] = total
            st["done_chunks"] = 1
            st["status"] = "completed"
            st["updated_at"] = time.time()
            _progress[tenant_domain] = st

    except Exception as e:
        with _progress_lock:
            st = _progress.get(tenant_domain) or {}
            st["status"] = "failed"
            st["failed_chunks"] = 1
            st["last_error"] = str(e)[:2000]
            st["updated_at"] = time.time()
            _progress[tenant_domain] = st
        raise HTTPException(status_code=500, detail=f"pgvector upsert failed: {e}")


def _pg_delete_docs(tenant_id: int, doc_ids: List[str]) -> None:
    """Delete documents from the documents table by id list."""
    if not doc_ids:
        return
    conn = _pg()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM documents WHERE tenant_id = %s AND id = ANY(%s)",
        (tenant_id, doc_ids),
    )
    conn.commit()
    cur.close()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────────

class SyncItem(BaseModel):
    id: str
    type: str
    wp_id: int
    updated_at_utc: str
    doc: Dict[str, Any] = Field(default_factory=dict)
    raw: Dict[str, Any] = Field(default_factory=dict)


class BatchRequest(BaseModel):
    tenant_id: str
    site_url: str
    mode: str = Field(default="upsert")
    items: List[SyncItem]


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────

@app.post("/sync/test")
def sync_test(
    authorization: Optional[str] = Header(default=None),
    x_phixtra_tenant: Optional[str] = Header(default=None),
):
    auth = validate_bearer(authorization, x_phixtra_tenant)
    return {"ok": True, "message": "Authorized", "tenant_domain": auth["domain"]}


@app.get("/health")
def health():
    """Public health check — no auth required."""
    issues = []
    pg_ok = False
    try:
        conn = _pg()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM documents")
        doc_count = cur.fetchone()[0]
        cur.close()
        conn.close()
        pg_ok = True
    except Exception as e:
        issues.append(f"PostgreSQL unavailable: {e}")
        doc_count = None

    if not OPENAI_API_KEY:
        issues.append("OPENAI_API_KEY missing — embeddings disabled")

    return {
        "ok": pg_ok,
        "status": "ready" if pg_ok else "misconfigured",
        "document_count": doc_count,
        "issues": issues,
    }


@app.get("/sync/progress")
def sync_progress(
    authorization: Optional[str] = Header(default=None),
    x_phixtra_tenant: Optional[str] = Header(default=None),
):
    auth = validate_bearer(authorization, x_phixtra_tenant)
    tenant_domain = clean_domain(auth["domain"])
    with _progress_lock:
        return _progress.get(tenant_domain, {"status": "idle"})


@app.get("/sync/status")
def sync_status(
    authorization: Optional[str] = Header(default=None),
    x_phixtra_tenant: Optional[str] = Header(default=None),
):
    """Returns document count for this tenant from the documents table."""
    auth = validate_bearer(authorization, x_phixtra_tenant)
    tenant_domain = clean_domain(auth["domain"])
    tenant_id = int(auth.get("tenant_id") or 0)

    doc_count = None
    last_error = _last_sync_error.get(tenant_domain, "")

    try:
        conn = _pg()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM documents WHERE tenant_id = %s", (tenant_id,))
        doc_count = cur.fetchone()[0]
        cur.close()
        conn.close()
    except Exception as e:
        last_error = str(e)

    return {
        "ok": True,
        "tenant_domain": tenant_domain,
        "index_name": f"pg-tenant-{tenant_id}",
        "index_exists": doc_count is not None,
        "document_count": doc_count,
        "last_error": last_error,
    }


@app.post("/sync/complete")
def sync_complete(
    authorization: Optional[str] = Header(default=None),
    x_phixtra_tenant: Optional[str] = Header(default=None),
):
    auth = validate_bearer(authorization, x_phixtra_tenant)
    tenant_id = int(auth.get("tenant_id") or 0)
    tenant_domain = clean_domain(auth["domain"])

    stamp_sync_complete(tenant_id)

    with _progress_lock:
        st = _progress.get(tenant_domain) or {}
        st["status"] = "completed"
        st["updated_at"] = time.time()
        _progress[tenant_domain] = st

    print(f"[SYNC] /sync/complete tenant_id={tenant_id} domain={tenant_domain}")
    return {"ok": True, "tenant_domain": tenant_domain}


@app.post("/sync/batch")
def sync_batch(
    req: BatchRequest,
    authorization: Optional[str] = Header(default=None),
    x_phixtra_tenant: Optional[str] = Header(default=None),
):
    auth = validate_bearer(authorization, x_phixtra_tenant)

    tenant_domain = clean_domain(auth["domain"])
    if clean_domain(req.tenant_id) != tenant_domain:
        raise HTTPException(status_code=403, detail="tenant_id mismatch")

    tenant_id = int(auth.get("tenant_id") or 0)

    if req.mode not in ("upsert", "delete"):
        raise HTTPException(status_code=400, detail="mode must be upsert or delete")

    if req.mode == "delete":
        doc_ids = [make_safe_doc_key(it.id, it.type, it.wp_id) for it in req.items]
        try:
            _pg_delete_docs(tenant_id, doc_ids)
            _last_sync_error[tenant_domain] = ""
        except Exception as e:
            _last_sync_error[tenant_domain] = str(e)[:600]
            raise HTTPException(status_code=500, detail=str(e))
        return {"ok": True, "index": f"pg-tenant-{tenant_id}", "count": len(doc_ids), "queue_has_more": False}

    # Build document rows for upsert
    pg_docs: List[Dict[str, Any]] = []
    for it in req.items:
        d = it.doc or {}

        safe_key = make_safe_doc_key(it.id, it.type, it.wp_id)
        title = (d.get("title") or it.raw.get("name") or it.raw.get("title") or "").strip()
        content = (d.get("content") or "").strip()
        categories_text = (
            d.get("categories_text")
            or ", ".join([str(c) for c in (d.get("categories") or []) if c])
        ).strip()

        price_min_raw = d.get("price_min")
        price_max_raw = d.get("price_max")
        try:
            price_min = float(price_min_raw) if price_min_raw not in (None, "", 0, "0") else None
        except Exception:
            price_min = None
        try:
            price_max = float(price_max_raw) if price_max_raw not in (None, "", 0, "0") else None
        except Exception:
            price_max = None

        in_stock = bool(d.get("in_stock")) if it.type == "product" else None

        pg_docs.append({
            "id": safe_key,
            "type": it.type,
            "title": title,
            "content": content,
            "url": (d.get("url") or it.raw.get("permalink") or "").strip(),
            "sku": (d.get("sku") or "").strip(),
            "brand": (d.get("brand") or "").strip(),
            "price_min": price_min,
            "price_max": price_max,
            "in_stock": in_stock,
            "categories_text": categories_text,
            "image_url": (d.get("image_url") or "").strip(),
            "currency": (d.get("currency") or "").strip().upper()[:3],
            "_embed_text": _make_embed_text(d, it.type, title),
        })

    try:
        _pg_upsert_docs(tenant_id, req.site_url, pg_docs, tenant_domain)
        _last_sync_error[tenant_domain] = ""
    except HTTPException as e:
        _last_sync_error[tenant_domain] = str(e.detail)[:600]
        raise

    queue_has_more = (req.mode == "upsert" and len(pg_docs) >= SYNC_BATCH_SIZE)

    if not queue_has_more:
        stamp_sync_complete(tenant_id)

    resp: Dict[str, Any] = {
        "ok": True,
        "index": f"pg-tenant-{tenant_id}",
        "count": len(pg_docs),
        "queue_has_more": queue_has_more,
    }
    if queue_has_more:
        resp["warning"] = f"Batch was very large ({len(pg_docs)} items). Consider reducing batch size."
    return resp


@app.post("/sync/clear")
def sync_clear(
    authorization: Optional[str] = Header(default=None),
    x_phixtra_tenant: Optional[str] = Header(default=None),
):
    """
    Delete all product/content documents for this tenant (keeps verified_spec rows).
    Authenticated with the same bearer token used for /sync/batch.
    Called by the export plugin's Clean Rebuild action before re-sending all products.
    """
    auth = validate_bearer(authorization, x_phixtra_tenant)
    tenant_id = int(auth.get("tenant_id") or 0)
    tenant_domain = clean_domain(auth.get("domain") or "")

    conn = _pg()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM documents WHERE tenant_id = %s AND type != 'verified_spec'",
        (tenant_id,),
    )
    conn.commit()
    deleted = cur.rowcount
    cur.close()
    conn.close()

    with _progress_lock:
        _progress[tenant_domain] = {"status": "idle", "updated_at": time.time()}

    print(f"[SYNC] /sync/clear tenant_id={tenant_id} deleted={deleted} docs")
    return {"ok": True, "deleted": deleted, "index": f"pg-tenant-{tenant_id}"}


@app.post("/sync/rebuild-index")
def rebuild_index(
    authorization: Optional[str] = Header(default=None),
    x_phixtra_tenant: Optional[str] = Header(default=None),
    x_internal_admin_secret: Optional[str] = Header(default=None),
):
    """Delete all documents for this tenant and reset progress."""
    if not INTERNAL_ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="INTERNAL_ADMIN_SECRET is not set")
    if x_internal_admin_secret != INTERNAL_ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    auth = validate_bearer(authorization, x_phixtra_tenant)
    tenant_domain = clean_domain(auth["domain"])
    tenant_id = int(auth.get("tenant_id") or 0)

    conn = _pg()
    cur = conn.cursor()
    cur.execute("DELETE FROM documents WHERE tenant_id = %s AND type != 'verified_spec'", (tenant_id,))
    conn.commit()
    deleted = cur.rowcount
    cur.close()
    conn.close()

    with _progress_lock:
        _progress[tenant_domain] = {"status": "idle", "updated_at": time.time()}

    return {"ok": True, "message": f"Deleted {deleted} documents for tenant {tenant_id}", "index": f"pg-tenant-{tenant_id}"}
