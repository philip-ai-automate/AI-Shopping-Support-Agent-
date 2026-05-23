import os
import json
import re
import time
import threading
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import pymysql
import requests
import bcrypt
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────
# PhiXtra Data Sync — PURE PUSH MODE (NO BLOB, NO INDEXER)
#
# ✅ Keeps your existing tenant auth + /sync/batch contract
# ✅ Creates Azure AI Search index with Semantic + Vector profiles
# ✅ Parallel upload (much faster)
# ✅ Auto-retry on transient upload errors
# ✅ Live progress monitor: GET /sync/progress
# ✅ Optional (protected) rebuild: POST /sync/rebuild-index
# ─────────────────────────────────────────────────────────────────────

load_dotenv()

MYSQL_HOST = os.getenv("DB_HOST", "127.0.0.1")
MYSQL_USER = os.getenv("DB_USER", "root")
MYSQL_PASS = os.getenv("DB_PASSWORD", "")
MYSQL_DB   = os.getenv("DB_NAME", "ai_support")

AZ_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
AZ_ADMIN_KEY = os.getenv("AZURE_SEARCH_ADMIN_KEY", "")
AZ_API_VERSION = os.getenv("AZURE_SEARCH_API_VERSION", "2025-09-01")

# Azure OpenAI (optional; used for integrated vectorization config and/or dim probe)
AOAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AOAI_KEY = os.getenv("AZURE_OPENAI_KEY", "")
AOAI_EMBED_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT", "")
AOAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")

# Embedding dimension (recommended to set explicitly in .env)
AZURE_SEARCH_EMBED_DIM = os.getenv("AZURE_SEARCH_EMBED_DIM", "").strip()

# Batch handling
SYNC_BATCH_SIZE = int(os.getenv("SYNC_BATCH_SIZE", "5000"))  # used only for queue warnings
AZURE_UPLOAD_CHUNK_SIZE = int(os.getenv("AZURE_UPLOAD_CHUNK_SIZE", "500"))  # keep <=1000
AZURE_UPLOAD_WORKERS = int(os.getenv("AZURE_UPLOAD_WORKERS", "10"))
AZURE_UPLOAD_RETRIES = int(os.getenv("AZURE_UPLOAD_RETRIES", "3"))

# Protected rebuild endpoint (optional)
INTERNAL_ADMIN_SECRET = os.getenv("INTERNAL_ADMIN_SECRET", "").strip()

app = FastAPI(title="PhiXtra Data Sync (Pure Push)")

SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_\-=]+$")
_EMBED_DIM_CACHE: Optional[int] = None

# Progress store (in-memory, keyed by tenant domain)
_progress_lock = threading.Lock()
_progress: Dict[str, Dict[str, Any]] = {}

# Last error store per tenant — shown by Check Sync Status button
_last_sync_error: Dict[str, str] = {}


# ─────────────────────── DB / Auth helpers ───────────────────────────

def db():
    try:
        return pymysql.connect(
            host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASS,
            database=MYSQL_DB, cursorclass=pymysql.cursors.DictCursor, autocommit=True,
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
        with db().cursor() as cur:
            if tenant_domain:
                cur.execute("""
                    SELECT k.id, k.tenant_id, k.is_active, k.api_key_hash, t.domain, t.status
                    FROM api_keys k
                    JOIN tenants t ON t.id = k.tenant_id
                    WHERE k.is_active = 1 AND t.status IN ('active', 'pending') AND LOWER(t.domain) = %s
                    LIMIT 500
                """, (tenant_domain,))
            else:
                cur.execute("""
                    SELECT k.id, k.tenant_id, k.is_active, k.api_key_hash, t.domain, t.status
                    FROM api_keys k
                    JOIN tenants t ON t.id = k.tenant_id
                    WHERE k.is_active = 1 AND t.status IN ('active', 'pending')
                    LIMIT 2000
                """)
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB query failed: {str(e)}")

    token_bytes = token.encode("utf-8")
    for row in rows:
        h = row.get("api_key_hash") or ""
        try:
            if bcrypt.checkpw(token_bytes, h.encode("utf-8")):
                return row
        except Exception:
            continue

    raise HTTPException(status_code=401, detail="Invalid or inactive API key")


def tenant_index_name(domain: str) -> str:
    safe = clean_domain(domain)
    safe = re.sub(r"[^a-z0-9\-]", "-", safe)
    safe = re.sub(r"-+", "-", safe).strip("-")
    if not safe or not safe[0].isalpha():
        safe = "t-" + (safe or "site")
    return f"phixtra-{safe}"[:128]


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


def az_headers():
    return {"Content-Type": "application/json", "api-key": AZ_ADMIN_KEY}


def aoai_headers():
    return {"Content-Type": "application/json", "api-key": AOAI_KEY}


def _strip_readonly_index_props(index_def: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = {}
    for k, v in index_def.items():
        if k.startswith("@odata.") or k.lower() in ("etag",):
            continue
        cleaned[k] = v
    return cleaned


def _as_text_list(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, list):
        return [s for x in values if x is not None and (s := str(x).strip())]
    s = str(values).strip()
    return [s] if s else []


def _join_for_semantic(values: Any) -> str:
    return ", ".join(_as_text_list(values))


# ─────────────────────── Embedding dimension ─────────────────────────

def _embed_texts_direct(texts: List[str]) -> List[List[float]]:
    if not (AOAI_ENDPOINT and AOAI_KEY and AOAI_EMBED_DEPLOYMENT):
        raise HTTPException(
            status_code=500,
            detail=(
                "Cannot determine embedding dimensions. "
                "Set AZURE_SEARCH_EMBED_DIM in .env (recommended), OR configure "
                "AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_KEY / AZURE_OPENAI_EMBED_DEPLOYMENT."
            ),
        )

    url = f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_EMBED_DEPLOYMENT}/embeddings?api-version={AOAI_API_VERSION}"
    r = requests.post(url, headers=aoai_headers(), data=json.dumps({"input": texts}), timeout=60)
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Azure OpenAI embeddings failed: {r.status_code} {r.text}")

    data = r.json()
    items_sorted = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
    vectors: List[List[float]] = []
    for it in items_sorted:
        emb = it.get("embedding")
        if not isinstance(emb, list) or not emb:
            raise HTTPException(status_code=500, detail="Azure OpenAI returned empty embedding")
        vectors.append([float(x) for x in emb])

    if len(vectors) != len(texts):
        raise HTTPException(status_code=500, detail="Azure OpenAI returned unexpected embeddings count")
    return vectors


def _ensure_embed_dim() -> int:
    """Returns the embedding dimension — never raises. Falls back to 1536 (ada-002)."""
    global _EMBED_DIM_CACHE
    if _EMBED_DIM_CACHE is not None:
        return _EMBED_DIM_CACHE

    if AZURE_SEARCH_EMBED_DIM:
        try:
            d = int(AZURE_SEARCH_EMBED_DIM)
            if d > 0:
                _EMBED_DIM_CACHE = d
                return d
        except Exception:
            pass

    if AOAI_ENDPOINT and AOAI_KEY and AOAI_EMBED_DEPLOYMENT:
        try:
            vec = _embed_texts_direct(["probe"])[0]
            _EMBED_DIM_CACHE = len(vec)
            return _EMBED_DIM_CACHE
        except Exception:
            pass

    # Safe default — works for ada-002 and text-embedding-3-small
    _EMBED_DIM_CACHE = 1536
    return _EMBED_DIM_CACHE


# ─────────────────────── Index schema ────────────────────────────────

def build_index_schema(index_name: str, embed_dim: int) -> Dict[str, Any]:
    schema: Dict[str, Any] = {
        "name": index_name,
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "type", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "tenant_id", "type": "Edm.String", "filterable": True},
            {"name": "site_url", "type": "Edm.String", "filterable": True},
            {"name": "title", "type": "Edm.String", "searchable": True, "sortable": True, "retrievable": True},
            {"name": "content", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "url", "type": "Edm.String", "filterable": True, "retrievable": True},
            {"name": "updated_at_utc", "type": "Edm.DateTimeOffset", "filterable": True, "sortable": True, "retrievable": True},
            {"name": "sku", "type": "Edm.String", "searchable": True, "filterable": True, "sortable": True, "retrievable": True},
            {"name": "brand", "type": "Edm.String", "searchable": True, "filterable": True, "facetable": True, "retrievable": True},
            {"name": "categories", "type": "Collection(Edm.String)", "searchable": True, "filterable": True, "facetable": True, "retrievable": True},
            {"name": "tags", "type": "Collection(Edm.String)", "searchable": True, "filterable": True, "facetable": True, "retrievable": True},
            {"name": "categories_text", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "tags_text", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "attributes_text", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "variants_text", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "product_type", "type": "Edm.String", "filterable": True, "facetable": True, "retrievable": True},
            {"name": "in_stock", "type": "Edm.Boolean", "filterable": True, "facetable": True, "retrievable": True},
            {"name": "price_min", "type": "Edm.Double", "filterable": True, "sortable": True, "facetable": True, "retrievable": True},
            {"name": "price_max", "type": "Edm.Double", "filterable": True, "sortable": True, "facetable": True, "retrievable": True},
            {"name": "order_number", "type": "Edm.String", "searchable": True, "filterable": True, "retrievable": True},
            {"name": "order_status", "type": "Edm.String", "searchable": True, "filterable": True, "facetable": True, "retrievable": True},
            {"name": "order_total", "type": "Edm.Double", "filterable": True, "sortable": True, "facetable": True, "retrievable": True},
            {"name": "customer_email", "type": "Edm.String", "searchable": True, "filterable": True, "retrievable": True},
            {"name": "customer_name", "type": "Edm.String", "searchable": True, "filterable": True, "retrievable": True},
            {"name": "image_url", "type": "Edm.String", "retrievable": True},
            {"name": "doc_json", "type": "Edm.String", "retrievable": True},
            {"name": "raw_json", "type": "Edm.String", "retrievable": True},
            {
                "name": "content_vector",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": True,
                "dimensions": embed_dim,
                "vectorSearchProfile": "phixtra-vprof",
            },
        ],
        "suggesters": [
            {
                "name": "sg",
                "searchMode": "analyzingInfixMatching",
                "sourceFields": ["title", "sku", "brand", "categories_text", "tags_text"],
            }
        ],
        "semantic": {
            "configurations": [
                {
                    "name": "phixtra-semantic",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "prioritizedContentFields": [
                            {"fieldName": "content"},
                            {"fieldName": "attributes_text"},
                            {"fieldName": "variants_text"},
                        ],
                        "prioritizedKeywordsFields": [
                            {"fieldName": "brand"},
                            {"fieldName": "categories_text"},
                            {"fieldName": "tags_text"},
                            {"fieldName": "sku"},
                            {"fieldName": "order_status"},
                        ],
                    },
                }
            ]
        },
        "vectorSearch": {
            "profiles": [
                {"name": "phixtra-vprof", "algorithm": "phixtra-hnsw", "vectorizer": "phixtra-aoai-vec"}
            ],
            "algorithms": [
                {"name": "phixtra-hnsw", "kind": "hnsw"}
            ],
        },
    }

    if AOAI_ENDPOINT and AOAI_KEY and AOAI_EMBED_DEPLOYMENT:
        schema["vectorSearch"]["vectorizers"] = [
            {
                "name": "phixtra-aoai-vec",
                "kind": "azureOpenAI",
                "azureOpenAIParameters": {
                    "resourceUri": AOAI_ENDPOINT,
                    "deploymentId": AOAI_EMBED_DEPLOYMENT,
                    "apiKey": AOAI_KEY,
                    "modelName": AOAI_EMBED_DEPLOYMENT,
                },
            }
        ]
    else:
        schema["vectorSearch"]["profiles"][0].pop("vectorizer", None)

    return schema


# ─────────────────────── Index CRUD ──────────────────────────────────

def _azure_get_index(index_name: str) -> Tuple[int, str, Optional[Dict[str, Any]]]:
    r = requests.get(
        f"{AZ_ENDPOINT}/indexes/{index_name}?api-version={AZ_API_VERSION}",
        headers=az_headers(), timeout=30,
    )
    if r.status_code == 200:
        try:
            return r.status_code, r.text, r.json()
        except Exception:
            return r.status_code, r.text, None
    return r.status_code, r.text, None


def _schema_without_vectorizer(index_name: str, embed_dim: int) -> Dict[str, Any]:
    """Index schema with no vectorizer — text + semantic search only. Used as fallback."""
    schema = build_index_schema(index_name, embed_dim)
    schema["fields"] = [f for f in schema["fields"] if f["name"] != "content_vector"]
    vs = schema.get("vectorSearch") or {}
    vs.pop("vectorizers", None)
    if vs.get("profiles"):
        for p in vs["profiles"]:
            p.pop("vectorizer", None)
    schema["vectorSearch"] = vs
    return schema


def ensure_index(index_name: str):
    """
    Creates or updates the Azure AI Search index for this tenant.
    Always succeeds — falls back to text-only index if vectorizer config is wrong.
    """
    if not AZ_ENDPOINT or not AZ_ADMIN_KEY:
        raise HTTPException(
            status_code=500,
            detail="Azure Search not configured. Check AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_ADMIN_KEY in .env"
        )

    status, body, current = _azure_get_index(index_name)
    embed_dim = _ensure_embed_dim()

    if status == 200:
        # Index exists — add any missing fields and refresh semantic/vector config
        if not isinstance(current, dict):
            raise HTTPException(status_code=500, detail="Azure returned invalid index definition")
        desired = build_index_schema(index_name, embed_dim)
        current_clean = _strip_readonly_index_props(current)
        existing_fields = {f.get("name") for f in (current_clean.get("fields") or []) if isinstance(f, dict)}
        fields = list(current_clean.get("fields") or [])
        for f in desired["fields"]:
            if f["name"] not in existing_fields:
                fields.append(f)
        current_clean["fields"] = fields
        current_clean["semantic"] = desired["semantic"]
        current_clean["vectorSearch"] = desired["vectorSearch"]
        r_put = requests.put(
            f"{AZ_ENDPOINT}/indexes/{index_name}?api-version={AZ_API_VERSION}",
            headers=az_headers(), data=json.dumps(current_clean), timeout=60,
        )
        if r_put.status_code not in (200, 201, 204):
            raise HTTPException(status_code=500, detail=f"Azure index update failed: {r_put.status_code} {r_put.text[:400]}")
        return

    if status != 404:
        raise HTTPException(status_code=500, detail=f"Azure index check failed: {status} {body[:400]}")

    # Index does not exist — create it
    schema = build_index_schema(index_name, embed_dim)
    r2 = requests.post(
        f"{AZ_ENDPOINT}/indexes?api-version={AZ_API_VERSION}",
        headers=az_headers(), data=json.dumps(schema), timeout=60,
    )
    if r2.status_code in (200, 201):
        print(f"[INDEX] Created with vectorizer: {index_name}")
        return

    # First attempt failed — if 400, the vectorizer config is wrong, try without it
    if r2.status_code == 400:
        print(f"[INDEX] Vectorizer rejected (400) — retrying without vectorizer: {r2.text[:200]}")
        schema_plain = _schema_without_vectorizer(index_name, embed_dim)
        r3 = requests.post(
            f"{AZ_ENDPOINT}/indexes?api-version={AZ_API_VERSION}",
            headers=az_headers(), data=json.dumps(schema_plain), timeout=60,
        )
        if r3.status_code in (200, 201):
            print(f"[INDEX] Created without vectorizer (check AOAI config in .env): {index_name}")
            return
        raise HTTPException(
            status_code=500,
            detail=f"Azure index create failed. With vectorizer: {r2.status_code} {r2.text[:300]}. Without vectorizer: {r3.status_code} {r3.text[:300]}. Check AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_ADMIN_KEY in .env."
        )

    raise HTTPException(
        status_code=500,
        detail=f"Azure index create failed: {r2.status_code} {r2.text[:400]}"
    )


def delete_index(index_name: str):
    r = requests.delete(
        f"{AZ_ENDPOINT}/indexes/{index_name}?api-version={AZ_API_VERSION}",
        headers=az_headers(), timeout=60,
    )
    if r.status_code not in (200, 204, 404):
        raise HTTPException(status_code=500, detail=f"Azure index delete failed: {r.status_code} {r.text}")


# ─────────────────────── Upload (parallel + retry) ───────────────────

def _chunk_list(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _should_retry(status_code: int) -> bool:
    return status_code in (408, 409, 429, 500, 502, 503, 504)


def _az_upload_once(index_name: str, docs: List[Dict[str, Any]]) -> requests.Response:
    url = f"{AZ_ENDPOINT}/indexes/{index_name}/docs/index?api-version={AZ_API_VERSION}"
    payload = {"value": docs}
    return requests.post(url, headers=az_headers(), data=json.dumps(payload), timeout=60)


def _az_upload_with_retry(index_name: str, docs: List[Dict[str, Any]], retries: int) -> Tuple[bool, str]:
    last_err = ""
    for attempt in range(0, retries + 1):
        try:
            r = _az_upload_once(index_name, docs)
            if r.status_code in (200, 201):
                return True, ""
            if r.status_code == 207:
                try:
                    data = r.json()
                except Exception:
                    data = {"raw": r.text}
                return False, f"Partial failure (207): {json.dumps(data)[:2000]}"
            if _should_retry(r.status_code) and attempt < retries:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                continue
            last_err = f"{r.status_code} {r.text}"
            break
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                continue
            break
    return False, last_err


def az_upload_parallel(index_name: str, docs: List[Dict[str, Any]], tenant_domain: str) -> None:
    total = len(docs)
    if total == 0:
        return

    chunks = _chunk_list(docs, max(1, min(1000, AZURE_UPLOAD_CHUNK_SIZE)))
    total_chunks = len(chunks)

    with _progress_lock:
        _progress[tenant_domain] = {
            "status": "running",
            "total_docs": total,
            "uploaded_docs": 0,
            "total_chunks": total_chunks,
            "done_chunks": 0,
            "failed_chunks": 0,
            "last_error": "",
            "updated_at": time.time(),
        }

    def worker(chunk: List[Dict[str, Any]]) -> Tuple[int, bool, str]:
        ok, err = _az_upload_with_retry(index_name, chunk, AZURE_UPLOAD_RETRIES)
        return len(chunk), ok, err

    with ThreadPoolExecutor(max_workers=max(1, AZURE_UPLOAD_WORKERS)) as ex:
        futures = [ex.submit(worker, c) for c in chunks]
        for fut in as_completed(futures):
            chunk_len, ok, err = fut.result()
            with _progress_lock:
                st = _progress.get(tenant_domain) or {}
                st["done_chunks"] = int(st.get("done_chunks", 0)) + 1
                if ok:
                    st["uploaded_docs"] = int(st.get("uploaded_docs", 0)) + chunk_len
                else:
                    st["failed_chunks"] = int(st.get("failed_chunks", 0)) + 1
                    st["last_error"] = (err or "")[:2000]
                st["updated_at"] = time.time()
                _progress[tenant_domain] = st

            if not ok:
                raise HTTPException(status_code=500, detail=f"Azure upload failed: {err}")

    with _progress_lock:
        st = _progress.get(tenant_domain) or {}
        st["status"] = "completed"
        st["updated_at"] = time.time()
        _progress[tenant_domain] = st


# ─────────────────────── Tenant helpers ──────────────────────────────

def update_tenant_search_settings(tenant_id: int, index_name: str, semantic_config_name: str = "phixtra-semantic"):
    try:
        conn = db()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tenants SET azure_search_index=%s, azure_semantic_config=%s WHERE id=%s",
                (index_name, semantic_config_name, int(tenant_id)),
            )
        try:
            conn.close()
        except Exception:
            pass
    except Exception:
        pass


def stamp_sync_complete(tenant_id: int):
    """
    Writes last_full_sync_at = NOW() to the tenants row so the onboarding
    portal can mark Step 6 (Run your first full sync) as complete.
    Same pattern as update_tenant_search_settings — never raises to the caller.
    """
    try:
        conn = db()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tenants SET last_full_sync_at = NOW() WHERE id = %s",
                (int(tenant_id),)
            )
        try:
            conn.close()
        except Exception:
            pass
    except Exception as e:
        print(f"[SYNC] stamp_sync_complete failed (non-fatal): {e}")


# ─────────────────────── Request models ──────────────────────────────

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


# ─────────────────────── Endpoints ───────────────────────────────────

@app.post("/sync/test")
def sync_test(
    authorization: Optional[str] = Header(default=None),
    x_phixtra_tenant: Optional[str] = Header(default=None),
):
    auth = validate_bearer(authorization, x_phixtra_tenant)
    return {"ok": True, "message": "Authorized", "tenant_domain": auth["domain"]}


@app.get("/health")
def health():
    """Public health check — no auth required. Use to verify server is running."""
    az_ok = bool(AZ_ENDPOINT and AZ_ADMIN_KEY)
    aoai_ok = bool(AOAI_ENDPOINT and AOAI_KEY and AOAI_EMBED_DEPLOYMENT)
    issues = []
    if not az_ok:
        issues.append("AZURE_SEARCH_ENDPOINT or AZURE_SEARCH_ADMIN_KEY missing from .env")
    if not aoai_ok:
        issues.append("Azure OpenAI not configured — vector search disabled")
    if not AZURE_SEARCH_EMBED_DIM:
        issues.append("AZURE_SEARCH_EMBED_DIM not set — add AZURE_SEARCH_EMBED_DIM=1536 to .env")
    return {"ok": az_ok, "status": "ready" if az_ok else "misconfigured", "issues": issues}


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
    """
    Returns index status for this tenant — called by the WordPress
    'Check Sync Status' button to show document count and any errors.
    """
    auth = validate_bearer(authorization, x_phixtra_tenant)
    tenant_domain = clean_domain(auth["domain"])
    index_name = tenant_index_name(tenant_domain)

    az_status, _, az_index = _azure_get_index(index_name)
    index_exists = az_status == 200
    doc_count = None

    if index_exists:
        try:
            r = requests.get(
                f"{AZ_ENDPOINT}/indexes/{index_name}/stats?api-version={AZ_API_VERSION}",
                headers=az_headers(), timeout=15,
            )
            if r.status_code == 200:
                doc_count = r.json().get("documentCount")
        except Exception:
            pass

    # Retrieve last stored error for this tenant (stored by sync_batch on failure)
    last_error = _last_sync_error.get(tenant_domain, "")

    return {
        "ok": True,
        "tenant_domain": tenant_domain,
        "index_name": index_name,
        "index_exists": index_exists,
        "document_count": doc_count,
        "last_error": last_error,
    }


@app.post("/sync/complete")
def sync_complete(
    authorization: Optional[str] = Header(default=None),
    x_phixtra_tenant: Optional[str] = Header(default=None),
):
    """
    Called by the WordPress PhiXtra Export plugin after it has finished
    queuing all sync batches. Stamps last_full_sync_at on the tenant row
    so the onboarding portal marks Step 6 (Run your first full sync) as
    complete with a green tick — without relying on chat activity.
    """
    auth = validate_bearer(authorization, x_phixtra_tenant)
    tenant_id = int(auth.get("tenant_id") or 0)
    tenant_domain = clean_domain(auth["domain"])

    stamp_sync_complete(tenant_id)

    # Update in-memory progress to reflect explicit completion
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

    index_name = tenant_index_name(tenant_domain)

    try:
        ensure_index(index_name)
    except HTTPException as e:
        _last_sync_error[tenant_domain] = f"Index error: {str(e.detail)[:600]}"
        raise
    update_tenant_search_settings(int(auth.get("tenant_id") or 0), index_name, "phixtra-semantic")

    if req.mode not in ("upsert", "delete"):
        raise HTTPException(status_code=400, detail="mode must be upsert or delete")

    azure_docs: List[Dict[str, Any]] = []
    for it in req.items:
        d = it.doc or {}
        r = it.raw or {}

        safe_key = make_safe_doc_key(it.id, it.type, it.wp_id)
        action = "delete" if req.mode == "delete" else "mergeOrUpload"

        categories = d.get("categories") or []
        tags = d.get("tags") or []

        doc_out: Dict[str, Any] = {
            "@search.action": action,
            "id": safe_key,
            "type": it.type,
            "tenant_id": req.tenant_id,
            "site_url": req.site_url,
            "updated_at_utc": it.updated_at_utc,
            "url": d.get("url") or r.get("permalink") or "",
            "title": d.get("title") or r.get("name") or r.get("title") or "",
            "content": d.get("content") or "",
            "sku": d.get("sku") or "",
            "brand": d.get("brand") or "",
            "categories": categories,
            "tags": tags,
            "categories_text": d.get("categories_text") or _join_for_semantic(categories),
            "tags_text": d.get("tags_text") or _join_for_semantic(tags),
            "attributes_text": d.get("attributes_text") or "",
            "variants_text": d.get("variants_text") or "",
            "product_type": d.get("product_type") or "",
            "in_stock": bool(d.get("in_stock")) if it.type == "product" else False,
            "price_min": float(d.get("price_min") or 0.0),
            "price_max": float(d.get("price_max") or 0.0),
            "order_number": d.get("order_number") or "",
            "order_status": d.get("order_status") or "",
            "order_total": float(d.get("order_total") or 0.0),
            "customer_email": d.get("customer_email") or "",
            "customer_name": d.get("customer_name") or "",
            "image_url": d.get("image_url") or "",
            "doc_json": json.dumps({"_source_id": it.id, **d}, ensure_ascii=False),
            "raw_json": json.dumps(r, ensure_ascii=False),
        }

        vec = d.get("content_vector")
        if isinstance(vec, list) and len(vec) > 0:
            try:
                doc_out["content_vector"] = [float(x) for x in vec]
            except Exception:
                pass

        azure_docs.append(doc_out)

    try:
        az_upload_parallel(index_name, azure_docs, tenant_domain)
        _last_sync_error[tenant_domain] = ""  # clear on success
    except HTTPException as e:
        _last_sync_error[tenant_domain] = str(e.detail)[:600]
        raise

    queue_has_more = (req.mode == "upsert" and len(azure_docs) >= SYNC_BATCH_SIZE)

    # ── Passive completion stamp ──────────────────────────────────────────────
    # If queue_has_more is False this batch was smaller than SYNC_BATCH_SIZE,
    # meaning it was the final (or only) batch. Stamp last_full_sync_at now as
    # a safety net in case the WordPress plugin does not call /sync/complete.
    if not queue_has_more:
        stamp_sync_complete(int(auth.get("tenant_id") or 0))
    # ─────────────────────────────────────────────────────────────────────────

    resp: Dict[str, Any] = {"ok": True, "index": index_name, "count": len(azure_docs), "queue_has_more": queue_has_more}
    if queue_has_more:
        resp["warning"] = (
            f"Batch was very large ({len(azure_docs)} items). If your WordPress host times out, reduce batch size slightly."
        )
    return resp


@app.post("/sync/rebuild-index")
def rebuild_index(
    authorization: Optional[str] = Header(default=None),
    x_phixtra_tenant: Optional[str] = Header(default=None),
    x_internal_admin_secret: Optional[str] = Header(default=None),
):
    if not INTERNAL_ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="INTERNAL_ADMIN_SECRET is not set")

    if x_internal_admin_secret != INTERNAL_ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    auth = validate_bearer(authorization, x_phixtra_tenant)
    tenant_domain = clean_domain(auth["domain"])
    index_name = tenant_index_name(tenant_domain)

    delete_index(index_name)
    ensure_index(index_name)

    return {"ok": True, "message": "Index rebuilt", "index": index_name}
