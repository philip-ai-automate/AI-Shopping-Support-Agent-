# search.py
"""
pgvector-based hybrid retrieval replacing Azure AI Search.

- Generates an embedding for the user's query (OpenAI API)
- Runs vector similarity search against the `documents` table (pgvector)
- Applies SQL WHERE filters for price, stock, brand when detected in query
- Falls back to keyword-only search if embedding fails
"""

import os
import re
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
import psycopg2
import psycopg2.extras
from openai import OpenAI
from dotenv import load_dotenv

# ── Currency conversion (GBP ↔ NGN) ─────────────────────────────────────────
# Products are stored in GBP. Customers query in NGN. We convert at query time.
_GBP_NGN_RATE: float = 2050.0   # fallback
_GBP_NGN_FETCHED_AT: float = 0.0
_GBP_NGN_TTL: int = 3600        # refresh every hour


def _get_gbp_ngn_rate() -> float:
    global _GBP_NGN_RATE, _GBP_NGN_FETCHED_AT
    if time.time() - _GBP_NGN_FETCHED_AT < _GBP_NGN_TTL:
        return _GBP_NGN_RATE
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get("https://api.exchangerate-api.com/v4/latest/GBP")
            resp.raise_for_status()
            rate = float(resp.json()["rates"]["NGN"])
            _GBP_NGN_RATE = rate
            _GBP_NGN_FETCHED_AT = time.time()
            print(f"   [CURRENCY] live GBP→NGN rate: {rate:,.2f}")
            return rate
    except Exception as exc:
        print(f"   [CURRENCY] rate fetch failed ({exc}), using fallback GBP→NGN={_GBP_NGN_RATE}")
        _GBP_NGN_FETCHED_AT = time.time()   # don't hammer the API on every request
        return _GBP_NGN_RATE


def _ngn_to_gbp(ngn: float) -> float:
    return round(ngn / _get_gbp_ngn_rate(), 2)


def _gbp_to_ngn(gbp: float) -> float:
    return round(gbp * _get_gbp_ngn_rate(), 0)

load_dotenv()

_openai_client: Optional[OpenAI] = None


def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _openai_client


def _get_pg_conn():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "5432")),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
        dbname=os.getenv("PG_DB"),
    )


def _embed_query(text: str) -> List[float]:
    model = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    text = (text or "").strip() or "empty"
    resp = _get_openai_client().embeddings.create(model=model, input=[text])
    return [float(x) for x in resp.data[0].embedding]


def _format_doc(doc: Dict[str, Any], max_chars: int) -> str:
    title = (doc.get("title") or "").strip()
    sku = (doc.get("sku") or "").strip()
    brand = (doc.get("brand") or "").strip()
    url = (doc.get("url") or "").strip()

    price_min = doc.get("price_min")
    price_max = doc.get("price_max")

    parts = []
    if title:
        parts.append(f"Title: {title}")
    if sku:
        parts.append(f"SKU: {sku}")
    if brand:
        parts.append(f"Brand: {brand}")
    if price_min is not None or price_max is not None:
        try:
            pmin = float(price_min) if price_min is not None else None
            pmax = float(price_max) if price_max is not None else None
            if pmin is not None and pmin <= 0:
                pmin = None
            if pmax is not None and pmax <= 0:
                pmax = None
            # Show both GBP (stored value) and NGN equivalent so the AI can
            # accurately answer price questions from Nigerian customers
            if pmin is not None and pmax is not None and abs(pmax - pmin) > 0.01:
                ngn_lo = int(_gbp_to_ngn(pmin))
                ngn_hi = int(_gbp_to_ngn(pmax))
                parts.append(f"Price: £{pmin:,.2f}–£{pmax:,.2f} (₦{ngn_lo:,}–₦{ngn_hi:,})")
            elif pmin is not None:
                ngn = int(_gbp_to_ngn(pmin))
                parts.append(f"Price: £{pmin:,.2f} (≈ ₦{ngn:,})")
            elif pmax is not None:
                ngn = int(_gbp_to_ngn(pmax))
                parts.append(f"Price: £{pmax:,.2f} (≈ ₦{ngn:,})")
        except Exception:
            pass

    content = (doc.get("content") or "").strip()
    if content:
        if max_chars > 0 and len(content) > max_chars:
            content = content[:max_chars].rstrip() + "…"
        parts.append(f"Details: {content}")

    if url:
        parts.append(f"URL: {url}")

    return "\n".join(parts).strip()


def _parse_filters_sql(query: str) -> Tuple[List[str], List[Any]]:
    """
    Parse natural language query into SQL WHERE clause parts + params.
    Returns (parts, params) where parts are SQL condition strings with %s placeholders.
    """
    parts: List[str] = []
    params: List[Any] = []
    q = (query or "").lower()

    # Expand k shorthand before any parsing: "400k" → "400000", "1.5k" → "1500"
    q = re.sub(r'(\d+(?:\.\d+)?)\s*k\b', lambda m: str(int(float(m.group(1)) * 1000)), q)

    # Include ₦ in symbol detection so NGN budgets are captured
    _n = r'(?:[\$£€₦]\s*)?(\d[\d,]*(?:\.\d{1,2})?)'

    def _to_gbp(val: float) -> float:
        """Convert to GBP if the value looks like NGN (explicit ₦ or number > 5000).
        Products are stored in GBP (£100–£2000 range), so anything > 5000 is NGN."""
        if '₦' in q or val > 5000:
            converted = _ngn_to_gbp(val)
            print(f"   [CURRENCY] ₦{val:,.0f} → £{converted:,.2f} (budget conversion)")
            return converted
        return val

    # Price range: "between £10 and £50" | "₦100,000 - ₦300,000"
    _range = re.search(
        rf'between\s+{_n}\s+(?:and|to)\s+{_n}|{_n}\s*(?:-|–|—|to)\s*{_n}(?=\s|$)',
        q,
    )
    if _range:
        gs = [g for g in _range.groups() if g is not None]
        if len(gs) >= 2:
            try:
                lo = _to_gbp(float(gs[0].replace(',', '')))
                hi = _to_gbp(float(gs[1].replace(',', '')))
                if 0 < lo <= hi:
                    parts.append(
                        "price_min >= %s AND price_min <= %s AND price_min > 0 "
                        "AND (price_max <= %s OR price_max IS NULL)"
                    )
                    params.extend([lo, hi, hi])
            except Exception:
                pass
    else:
        # Upper bound: "under £50", "below ₦300,000", "less than 200000"
        _max_m = re.search(
            rf'(?:under|below|less than|cheaper than|up to|no more than|max(?:imum)?(?:\s+price)?(?:\s+of)?)\s+{_n}',
            q,
        )
        if _max_m:
            try:
                hi = _to_gbp(float(_max_m.group(1).replace(',', '')))
                parts.append("price_min <= %s AND price_min > 0")
                params.append(hi)
            except Exception:
                pass

        # Lower bound: "over £50", "above ₦100,000", "more than 200000"
        _min_m = re.search(
            rf'(?:over|above|more than|at least|starting (?:at|from)|from)\s+{_n}',
            q,
        )
        if _min_m:
            try:
                lo = _to_gbp(float(_min_m.group(1).replace(',', '')))
                parts.append("(price_max >= %s OR (price_max IS NULL AND price_min >= %s))")
                params.extend([lo, lo])
            except Exception:
                pass

    # Stock filter
    if re.search(
        r'\bin[\s-]?stock\b|\bonly\s+(?:items?\s+)?(?:in\s+stock|available)\b|\bavailable\s+(?:now|only|items?)\b',
        q,
    ):
        parts.append("in_stock = TRUE")
    elif re.search(r'\bout[\s-]?of[\s-]?stock\b', q):
        parts.append("in_stock = FALSE")

    # Brand filter
    _brand_m = re.search(
        r'(?:brand[:\s]+|by\s+)([a-z0-9][a-z0-9 &\-]{0,30})(?=\s|$|,|\.)',
        q,
    )
    if not _brand_m:
        _brand_m = re.search(
            r'([a-z0-9][a-z0-9 &\-]{0,30})\s+brand(?=\s|$|,|\.)',
            q,
        )
    if _brand_m:
        bv = _brand_m.group(1).strip()
        if bv and len(bv) > 1:
            parts.append("brand ILIKE %s")
            params.append(f"%{bv}%")

    return parts, params


def _run_vector_search(
    conn,
    tenant_id: int,
    q_vec: List[float],
    top_k: int,
    extra_parts: List[str],
    extra_params: List[Any],
) -> List[Dict[str, Any]]:
    """Run a pgvector cosine similarity query."""
    where = "WHERE tenant_id = %s AND embedding IS NOT NULL"
    qparams: List[Any] = [tenant_id]
    if extra_parts:
        where += " AND " + " AND ".join(extra_parts)
        qparams.extend(extra_params)

    vec_literal = "[" + ",".join(str(x) for x in q_vec) + "]"
    sql = f"""
        SELECT id, title, content, url, sku, brand,
               price_min, price_max, in_stock, type,
               categories_text, site_url, tenant_id, image_url
        FROM documents
        {where}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    qparams.extend([vec_literal, top_k])

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, qparams)
    rows = cur.fetchall() or []
    cur.close()
    return [dict(r) for r in rows]


def _run_keyword_search(
    conn,
    tenant_id: int,
    query: str,
    top_k: int,
    extra_parts: List[str],
    extra_params: List[Any],
) -> List[Dict[str, Any]]:
    """Full-text keyword search using the pre-computed search_vector GIN index."""
    where = "WHERE tenant_id = %s AND search_vector @@ plainto_tsquery('english', %s)"
    qparams: List[Any] = [tenant_id, query]
    if extra_parts:
        where += " AND " + " AND ".join(extra_parts)
        qparams.extend(extra_params)

    sql = f"""
        SELECT id, title, content, url, sku, brand,
               price_min, price_max, in_stock, type,
               categories_text, site_url, tenant_id, image_url
        FROM documents
        {where}
        ORDER BY ts_rank(search_vector, plainto_tsquery('english', %s)) DESC
        LIMIT %s
    """
    qparams.extend([query, top_k])

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, qparams)
    rows = cur.fetchall() or []
    cur.close()
    return [dict(r) for r in rows]


def _rrf_merge(
    vec_rows: List[Dict[str, Any]],
    kw_rows: List[Dict[str, Any]],
    top_k: int,
    k: int = 60,
) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion: score = 1/(k+rank_vec) + 1/(k+rank_kw).

    Docs that appear in both result sets are boosted; docs in only one list
    still get a partial score. k=60 is the standard RRF constant.
    """
    scores: Dict[str, float] = {}
    docs: Dict[str, Dict[str, Any]] = {}
    for rank, doc in enumerate(vec_rows):
        did = doc["id"]
        scores[did] = scores.get(did, 0.0) + 1.0 / (k + rank + 1)
        docs[did] = doc
    for rank, doc in enumerate(kw_rows):
        did = doc["id"]
        scores[did] = scores.get(did, 0.0) + 1.0 / (k + rank + 1)
        docs[did] = doc
    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
    return [docs[i] for i in sorted_ids[:top_k]]


def search_documents(query: str, tenant_id: int, semantic_config: Optional[str] = None) -> List[str]:
    """
    Returns a list[str] of compact context chunks.
    Wrapper kept for backward compatibility — calls the full function and discards raw docs.
    """
    chunks, _ = search_documents_with_meta(query, tenant_id, semantic_config)
    return chunks


def _clean_search_query(query: str) -> str:
    """
    Strip the gateway-injected background context before filter parsing and
    keyword search. The gateway format is:
      [Background reference only — ...]\n\n{actual customer message}
    The bracket can contain nested [] (e.g. [UK Used]) so regex won't work —
    we split on the first double-newline instead, which is the reliable separator.
    The embedding still uses the full message for better semantic recall.
    """
    q = (query or "").strip()
    if q.startswith("[Background reference"):
        parts = q.split("\n\n", 1)
        if len(parts) == 2:
            return parts[1].strip() or q
    return q


def search_documents_with_meta(
    query: str, tenant_id: int, semantic_config: Optional[str] = None,
    precomputed_embedding: Optional[List[float]] = None,
) -> tuple:
    """
    Returns (chunks: List[str], raw_docs: List[Dict])

    When OpenAI is available: runs both vector search and keyword search then
    merges with Reciprocal Rank Fusion (RRF). This catches exact-match queries
    (e.g. SKU lookups) that pure vector search can miss.

    When OpenAI is unavailable: falls back to keyword-only search so chat still
    works during an OpenAI outage.
    """
    top_k = int(os.getenv("RAG_TOP_K", "6"))
    max_chars = int(os.getenv("RAG_CHUNK_MAX_CHARS", "900"))

    # Use only the customer's actual text for filter parsing and keyword search.
    # The full query (with background context) is still used for embeddings.
    clean_query = _clean_search_query(query)

    filter_parts, filter_params = _parse_filters_sql(clean_query)
    if filter_parts:
        print(f"   🔍 pgvector filter applied: {' AND '.join(filter_parts)}")

    if precomputed_embedding:
        q_vec = precomputed_embedding
    else:
        try:
            q_vec = _embed_query(query)
        except Exception as e:
            print(f"   ⚠️ embedding failed — keyword-only search: {e}")
            q_vec = None

    try:
        conn = _get_pg_conn()
        try:
            if q_vec is not None:
                # Fetch more candidates from each leg so RRF has room to rerank
                vec_rows = _run_vector_search(conn, tenant_id, q_vec, top_k * 2, filter_parts, filter_params)
                try:
                    kw_rows = _run_keyword_search(conn, tenant_id, clean_query, top_k * 2, filter_parts, filter_params)
                except Exception as kw_err:
                    print(f"   ⚠️ keyword search failed (vector-only fallback): {kw_err}")
                    kw_rows = []
                rows = _rrf_merge(vec_rows, kw_rows, top_k)
                print(f"   🔀 hybrid RRF: {len(vec_rows)} vec + {len(kw_rows)} kw → {len(rows)} merged")
            else:
                rows = _run_keyword_search(conn, tenant_id, clean_query, top_k, filter_parts, filter_params)
        finally:
            conn.close()
    except Exception as e:
        print(f"   ⚠️ search failed: {e}")
        return [], []

    chunks: List[str] = []
    raw_docs: List[Dict[str, Any]] = []
    for row in rows:
        chunk = _format_doc(row, max_chars=max_chars)
        if chunk:
            chunks.append(chunk)
            raw_docs.append(row)

    return chunks[:top_k], raw_docs[:top_k]


def search_related_products(
    tenant_id: int,
    exclude_names: set,
    prod_type: str = "",
    category: str = "",
    top: int = 2,
    search_hint: str = "",
    max_price_gbp: Optional[float] = None,
    # Legacy kwarg — ignored but kept so existing callers don't break
    index_name: str = "",
) -> List[Dict]:
    """
    Returns up to `top` related products for cross-selling.
    Filters by type, category, and in_stock=TRUE. Never shows excluded names.
    """

    def _fmt_price(price_min_val, price_max_val) -> str:
        try:
            mn = float(price_min_val) if price_min_val is not None else None
            mx = float(price_max_val) if price_max_val is not None else None
            if mn is not None and mn <= 0:
                mn = None
            if mx is not None and mx <= 0:
                mx = None
            if mn is not None and mx is not None and abs(mx - mn) > 0.01:
                return f"£{mn:,.2f} – £{mx:,.2f}"
            elif mn is not None:
                return f"£{mn:,.2f}"
            elif mx is not None:
                return f"£{mx:,.2f}"
        except Exception:
            pass
        return ""

    def _extract_pid(doc_id: str) -> str:
        parts = (doc_id or "").split("-")
        return parts[1] if len(parts) >= 2 and parts[0] == "product" else ""

    where_parts = ["tenant_id = %s", "in_stock = TRUE", "embedding IS NOT NULL"]
    qparams: List[Any] = [tenant_id]

    # Respect the customer's budget — never show related products above it
    if max_price_gbp is not None and max_price_gbp > 0:
        where_parts.append("price_min <= %s AND price_min > 0")
        qparams.append(max_price_gbp)

    safe_type = (prod_type or "").replace("'", "")
    safe_category = (category or "").replace("'", "")

    if safe_type:
        where_parts.append("type = %s")
        qparams.append(safe_type)

    if safe_category:
        where_parts.append("categories_text ILIKE %s")
        qparams.append(f"%{safe_category}%")

    where_clause = "WHERE " + " AND ".join(where_parts)

    # Try to get embedding for semantic-like ordering
    safe_hint = (search_hint or safe_category or safe_type or "").strip()
    try:
        q_vec = _embed_query(safe_hint) if safe_hint else None
    except Exception:
        q_vec = None

    fetch_n = max(top * 3, 6)

    if q_vec is not None:
        vec_literal = "[" + ",".join(str(x) for x in q_vec) + "]"
        sql = f"""
            SELECT id, title, url, sku, brand, price_min, price_max, in_stock, type, image_url
            FROM documents
            {where_clause}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        qparams.extend([vec_literal, fetch_n])
    else:
        sql = f"""
            SELECT id, title, url, sku, brand, price_min, price_max, in_stock, type, image_url
            FROM documents
            {where_clause}
            LIMIT %s
        """
        qparams.append(fetch_n)

    try:
        conn = _get_pg_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, qparams)
            values = cur.fetchall() or []
            cur.close()
        finally:
            conn.close()

        related: List[Dict] = []
        for doc in values:
            doc = dict(doc)
            name = (doc.get("title") or "").strip()
            if not name:
                continue
            if name.lower() in exclude_names:
                continue

            doc_id = doc.get("id") or ""
            product_id = _extract_pid(doc_id)
            url = doc.get("url") or ""
            cart_url = f"{url}?add-to-cart={product_id}" if product_id and url else url
            related.append({
                "name": name,
                "price": _fmt_price(doc.get("price_min"), doc.get("price_max")),
                "url": url,
                "cart_url": cart_url,
                "in_stock": bool(doc.get("in_stock", True)),
                "sku": doc.get("sku") or "",
                "brand": doc.get("brand") or "",
                "id": doc_id,
                "product_id": product_id,
                "image_url": doc.get("image_url") or "",
            })

            if len(related) >= top:
                break

        return related

    except Exception as e:
        print(f"   ⚠️ search_related_products failed: {e}")
        return []


def upsert_verified_spec(
    tenant_id: int,
    model_hint: str,
    spec_key: str,
    spec_value: str,
    qualifier: str = "",
    sources: Optional[List[Dict[str, Any]]] = None,
    # Legacy kwarg — ignored
    index_name: str = "",
) -> None:
    """Write a small verified spec document into the documents table.

    Uses INSERT ... ON CONFLICT DO UPDATE with a stable id to avoid bloat.
    """
    from web_spec_lookup import make_verified_spec_doc_id

    doc_id = make_verified_spec_doc_id(
        tenant_id=int(tenant_id),
        model_hint=model_hint or "",
        spec_key=spec_key or "",
    )
    srcs = sources or []
    src_urls = [s.get("url") for s in srcs if isinstance(s, dict) and s.get("url")]

    text = f"Verified spec: {model_hint} {spec_key}: {spec_value}. {qualifier}".strip()
    title = f"{model_hint} — {spec_key}".strip(" —")
    url = src_urls[0] if src_urls else ""
    spec_sources_json = json.dumps(src_urls)[:2000]

    # Generate embedding for future searches (best-effort)
    embedding = None
    try:
        embedding = _embed_query(text)
    except Exception:
        pass

    try:
        conn = _get_pg_conn()
        try:
            cur = conn.cursor()
            if embedding is not None:
                vec_literal = "[" + ",".join(str(x) for x in embedding) + "]"
                cur.execute(
                    """
                    INSERT INTO documents
                        (id, tenant_id, type, title, content, url, brand, sku,
                         price_min, price_max, in_stock, site_url,
                         spec_key, spec_value, spec_sources, embedding, updated_at)
                    VALUES (%s, %s, 'verified_spec', %s, %s, %s, '', '',
                            NULL, NULL, NULL, '',
                            %s, %s, %s, %s::vector, NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        content = EXCLUDED.content,
                        spec_value = EXCLUDED.spec_value,
                        spec_sources = EXCLUDED.spec_sources,
                        embedding = EXCLUDED.embedding,
                        updated_at = NOW()
                    """,
                    (doc_id, tenant_id, title, text, url,
                     spec_key, spec_value, spec_sources_json, vec_literal),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO documents
                        (id, tenant_id, type, title, content, url, brand, sku,
                         price_min, price_max, in_stock, site_url,
                         spec_key, spec_value, spec_sources, updated_at)
                    VALUES (%s, %s, 'verified_spec', %s, %s, %s, '', '',
                            NULL, NULL, NULL, '',
                            %s, %s, %s, NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        content = EXCLUDED.content,
                        spec_value = EXCLUDED.spec_value,
                        spec_sources = EXCLUDED.spec_sources,
                        updated_at = NOW()
                    """,
                    (doc_id, tenant_id, title, text, url,
                     spec_key, spec_value, spec_sources_json),
                )
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        print(f"   ⚠️ upsert_verified_spec failed: {e}")
