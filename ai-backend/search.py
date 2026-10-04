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
    from db import get_db_connection
    return get_db_connection()


def _embed_query(text: str) -> List[float]:
    model = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    text = (text or "").strip() or "empty"
    resp = _get_openai_client().embeddings.create(model=model, input=[text])
    return [float(x) for x in resp.data[0].embedding]


_TENANT_CURRENCY_CACHE: Dict[int, Tuple[str, float]] = {}
_TENANT_CURRENCY_TTL: int = 3600


def _get_tenant_currency(tenant_id: int) -> str:
    """Fetch the sell currency for a tenant from their stored products (cached 1 hour)."""
    now = time.time()
    cached = _TENANT_CURRENCY_CACHE.get(tenant_id)
    if cached is not None:
        currency, fetched_at = cached
        if now - fetched_at < _TENANT_CURRENCY_TTL:
            return currency
    try:
        conn = _get_pg_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT currency FROM documents WHERE tenant_id = %s "
                "AND currency IS NOT NULL AND currency != '' LIMIT 1",
                (tenant_id,),
            )
            row = cur.fetchone()
            cur.close()
        finally:
            conn.close()
        currency = (row[0] or "").upper() if row else ""
    except Exception:
        currency = ""
    _TENANT_CURRENCY_CACHE[tenant_id] = (currency, now)
    return currency


def _fmt_currency_val(val: float, currency: str) -> str:
    """Format a price value with the correct symbol and decimal places."""
    _sym = {"GBP": "£", "USD": "$", "EUR": "€", "NGN": "₦"}
    cur = (currency or "").upper()
    if cur:
        sym = _sym.get(cur, cur + " ")
        return f"{sym}{val:,.0f}" if cur == "NGN" else f"{sym}{val:,.2f}"
    # Fallback heuristic for docs without currency field
    return f"₦{val:,.0f}" if val > 5000 else f"£{val:,.2f}"


# Information the AI should read in full and answer from in its own words
# (website pages, blog posts, Store Information) — as opposed to products,
# which the customer is shown as cards (2026-10-01).
INFO_DOC_TYPES = ("page", "post", "store_info")


def _is_product_doc(doc: Dict[str, Any]) -> bool:
    return (doc.get("type") or "product") not in INFO_DOC_TYPES


def _is_correction_doc(doc: Dict[str, Any]) -> bool:
    """A "Fix this answer" correction saved by the business from the Inbox
    or Chat Archive (Store Information entry id store_info-<tenant>-fix-…)."""
    return "-fix-" in str(doc.get("id") or "") and doc.get("type") == "store_info"


def _find_corrections(tenant_id: int, q_vec: Optional[List[float]], clean_query: str) -> List[Dict[str, Any]]:
    """The business's own corrections closest to this question. They are
    looked up on their own so they always reach the AI when they match,
    instead of competing with website pages for the website engine's places.
    CORRECTION_MAX_DISTANCE (cosine distance, default 0.5) keeps unrelated
    corrections out."""
    top_k = int(os.getenv("CORRECTION_TOP_K", "2"))
    max_dist = float(os.getenv("CORRECTION_MAX_DISTANCE", "0.5"))
    conn = _get_pg_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cols = """id, title, content, url, sku, brand, price_min, price_max, in_stock, type,
                  categories_text, site_url, tenant_id, image_url, COALESCE(currency, '') AS currency"""
        if q_vec is not None:
            vec_literal = "[" + ",".join(str(x) for x in q_vec) + "]"
            cur.execute(f"""
                SELECT {cols}, embedding <=> %s::vector AS dist
                FROM documents
                WHERE tenant_id = %s AND type = 'store_info' AND id LIKE %s AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (vec_literal, tenant_id, f"store_info-{tenant_id}-fix-%", vec_literal, top_k))
            rows = [r for r in cur.fetchall() if r["dist"] is not None and float(r["dist"]) <= max_dist]
        else:
            cur.execute(f"""
                SELECT {cols}
                FROM documents
                WHERE tenant_id = %s AND type = 'store_info' AND id LIKE %s
                  AND to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(content,''))
                      @@ plainto_tsquery('simple', %s)
                LIMIT %s
            """, (tenant_id, f"store_info-{tenant_id}-fix-%", clean_query, top_k))
            rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    finally:
        conn.close()


_TITLE_STOPWORDS = {
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how", "does", "do", "did", "have", "has",
    "your", "yours", "you", "with", "from", "this", "that", "these", "those", "there", "their", "they", "them",
    "about", "into", "than", "then", "will", "would", "could", "should", "can", "please", "tell", "give",
    "want", "need", "like", "just", "also", "much", "many", "more", "most", "some", "any", "for", "and", "the",
    "are", "is", "was", "were", "been", "being", "get", "got", "know", "help", "page", "website",
}
# How much of the top of a page counts as its heading (the small label above
# the main heading + the main heading itself), 2026-10-03.
_TOP_OF_PAGE_CHARS = int(os.getenv("TITLE_MATCH_TOP_CHARS", "200"))


def _business_name_words(cur, tenant_id: int) -> set:
    """Words of the business's own name and web address (e.g. "phixtra"):
    they are on almost every page, so they never pick a page."""
    cur.execute("""SELECT t.name, t.domain,
                          (SELECT site_url FROM documents WHERE tenant_id=t.id AND site_url IS NOT NULL LIMIT 1) AS site
                   FROM tenants t WHERE t.id=%s""", (tenant_id,))
    row = cur.fetchone() or {}
    text = " ".join(str(v or "") for v in row.values()).lower()
    return {w for w in re.findall(r"[a-z]{4,}", text)
            if w not in {"https", "http", "www", "com", "limited", "ltd"}}


_TITLE_SYNONYMS = [
    (r"\b(phone|call|email|e-mail|address|location|located|office|reach|whatsapp number|number)\b", ["contact"]),
    (r"\b(deliver|delivery|shipping|ship|dispatch|courier)\b", ["delivery", "shipping"]),
    (r"\b(refund|refunds|return|returns|exchange)\b", ["return", "refund"]),
    (r"\b(price|prices|pricing|cost|costs|how much|fee|fees|plan|plans|package|packages)\b", ["pricing", "price", "plans"]),
    (r"\b(warranty|guarantee)\b", ["warranty"]),
    (r"\b(who are you|about you|your company|your business)\b", ["about"]),
    (r"\b(open|opening|hours|close|closing)\b", ["hours", "opening"]),
    (r"\b(pay|payment|payments|bank transfer|card)\b", ["payment"]),
]


def _find_title_matches(tenant_id: int, query: str) -> List[Dict[str, Any]]:
    """Website pages whose TITLE — or the top of the page (its label + main
    heading) — contains an important word of the question ("contact",
    "delivery", "pricing", "connect"…), so e.g. the Contact page always reaches
    the AI for a contact question even when other pages read similarly
    (2026-10-03). The top of the page counts because page titles are often
    written for Google ("WhatsApp Business API") while the heading names the
    product ("PhiXtra Connect"). Words of the business's own name never count.
    Title matches come first. At most TITLE_MATCH_TOP_K (default 2)."""
    q = (query or "").lower()
    words = [w for w in re.findall(r"[a-zA-Z]{4,}", q) if w not in _TITLE_STOPWORDS]
    # Everyday words for the same page as a usual page title.
    for pattern, title_words in _TITLE_SYNONYMS:
        if re.search(pattern, q):
            words += title_words
    words = list(dict.fromkeys(words))
    if not words:
        return []
    top_k = int(os.getenv("TITLE_MATCH_TOP_K", "2"))
    conn = _get_pg_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        own = _business_name_words(cur, tenant_id)
        words = [w for w in words if w not in own]
        if not words:
            cur.close()
            return []
        pattern = r"\m(" + "|".join(re.escape(w) for w in words) + ")"
        cur.execute("""
            SELECT id, title, content, url, sku, brand, price_min, price_max, in_stock, type,
                   categories_text, site_url, tenant_id, image_url, COALESCE(currency, '') AS currency
            FROM documents
            WHERE tenant_id = %s AND type IN ('page', 'post', 'store_info')
              AND id NOT LIKE %s
              AND (title ~* %s OR left(coalesce(content, ''), %s) ~* %s)
            LIMIT 60
        """, [tenant_id, f"store_info-{tenant_id}-fix-%", pattern, _TOP_OF_PAGE_CHARS, pattern])
        cands = [dict(r) for r in cur.fetchall()]
        cur.close()

        def _score(d):
            # Title word = 3; word in the page's first 60 characters (its label
            # / heading) = 2; further down the top of the page = 1. More of the
            # question's words matched = higher.
            title = (d.get("title") or "").lower()
            top = (d.get("content") or "")[:_TOP_OF_PAGE_CHARS].lower()
            score = 0
            for w in words:
                if re.search(r"\b" + re.escape(w), title):
                    score += 3
                else:
                    m = re.search(r"\b" + re.escape(w), top)
                    if m:
                        score += 2 if m.start() < 60 else 1
            return score

        scored = sorted(((_score(d), d) for d in cands), key=lambda x: (-x[0], len(x[1].get("title") or "")))
        return [d for sc, d in scored if sc >= 2][:top_k]

    finally:
        conn.close()


def _format_doc(doc: Dict[str, Any], max_chars: int) -> str:
    """One search result as the AI sees it. Products keep the short
    RAG_CHUNK_MAX_CHARS limit (their cards carry the detail); information
    items get up to RAG_INFO_MAX_CHARS, since a pricing page or uploaded
    price list cut at 900 characters loses most of its plans (2026-10-01)."""
    title = (doc.get("title") or "").strip()
    is_product = _is_product_doc(doc)
    if not is_product:
        max_chars = max(max_chars, int(os.getenv("RAG_INFO_MAX_CHARS", "6000")))
    sku = (doc.get("sku") or "").strip()
    brand = (doc.get("brand") or "").strip()
    url = (doc.get("url") or "").strip()

    price_min = doc.get("price_min")
    price_max = doc.get("price_max")

    if _is_correction_doc(doc):
        doc_type_label = "Correction from the business (use this over anything else)"
    elif is_product:
        doc_type_label = "Product"
    else:
        doc_type_label = "Information (website page or store information)"
    parts = [f"Type: {doc_type_label}"]
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
            currency = (doc.get("currency") or "").upper()
            ref = pmin if pmin is not None else pmax
            if ref is not None:
                if pmin is not None and pmax is not None and abs(pmax - pmin) > 0.01:
                    parts.append(f"Price: {_fmt_currency_val(pmin, currency)}–{_fmt_currency_val(pmax, currency)}")
                elif pmin is not None:
                    parts.append(f"Price: {_fmt_currency_val(pmin, currency)}")
                elif pmax is not None:
                    parts.append(f"Price: {_fmt_currency_val(pmax, currency)}")
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


def _parse_filters_sql(query: str, tenant_currency: str = "") -> Tuple[List[str], List[Any]]:
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

    def _normalise_budget(val: float) -> float:
        """Convert the user's stated budget to match the currency stored in the DB."""
        stored = (tenant_currency or "").upper()
        # Detect user's stated currency from symbols in the query
        if '£' in q:
            user_cur = 'GBP'
        elif '₦' in q:
            user_cur = 'NGN'
        elif '$' in q:
            user_cur = 'USD'
        elif '€' in q:
            user_cur = 'EUR'
        else:
            return val  # no symbol — assume same currency as stored

        if not stored or user_cur == stored:
            return val

        # Convert between known pairs
        if user_cur == 'GBP' and stored == 'NGN':
            converted = _gbp_to_ngn(val)
            print(f"   [CURRENCY] £{val:,.2f} → ₦{converted:,.0f} (GBP budget → NGN for filter)")
            return converted
        elif user_cur == 'NGN' and stored == 'GBP':
            converted = _ngn_to_gbp(val)
            print(f"   [CURRENCY] ₦{val:,.0f} → £{converted:,.2f} (NGN budget → GBP for filter)")
            return converted
        return val

    # Price range: "between ₦100,000 and ₦300,000" | "between £10 and £50"
    _range = re.search(
        rf'between\s+{_n}\s+(?:and|to)\s+{_n}|{_n}\s*(?:-|–|—|to)\s*{_n}(?=\s|$)',
        q,
    )
    if _range:
        gs = [g for g in _range.groups() if g is not None]
        if len(gs) >= 2:
            try:
                lo = _normalise_budget(float(gs[0].replace(',', '')))
                hi = _normalise_budget(float(gs[1].replace(',', '')))
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
                hi = _normalise_budget(float(_max_m.group(1).replace(',', '')))
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
                lo = _normalise_budget(float(_min_m.group(1).replace(',', '')))
                parts.append("(price_max >= %s OR (price_max IS NULL AND price_min >= %s))")
                params.extend([lo, lo])
            except Exception:
                pass

    # Stock filter — default to in-stock products only, unless the customer
    # explicitly asks about out-of-stock items. Scoped to type='product' so
    # FAQ/policy pages (in_stock IS NULL) are never excluded by this filter.
    if re.search(r'\bout[\s-]?of[\s-]?stock\b', q):
        parts.append("(type <> 'product' OR in_stock = FALSE)")
    else:
        parts.append("(type <> 'product' OR in_stock = TRUE)")

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
               categories_text, site_url, tenant_id, image_url,
               COALESCE(currency, '') AS currency
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
               categories_text, site_url, tenant_id, image_url,
               COALESCE(currency, '') AS currency
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


def _price_filter_parts(filter_parts: List[str]) -> List[str]:
    """Return only the price-related SQL parts from a filter list."""
    return [p for p in filter_parts if "price_min" in p or "price_max" in p]


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

    Price-filter fallback: if a price filter is active but returns 0 results
    (common when price_min is NULL for products not yet re-synced), we retry
    without the price SQL filter so the AI can still find semantically-matching
    products and reason about prices from the context text.
    """
    top_k = int(os.getenv("RAG_TOP_K", "6"))
    max_chars = int(os.getenv("RAG_CHUNK_MAX_CHARS", "900"))

    # Use only the customer's actual text for filter parsing and keyword search.
    # The full query (with background context) is still used for embeddings.
    clean_query = _clean_search_query(query)

    tenant_currency = _get_tenant_currency(tenant_id)
    filter_parts, filter_params = _parse_filters_sql(clean_query, tenant_currency)
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

    def _run_search(fp: List[str], pp: List[Any]) -> List[Dict[str, Any]]:
        conn = _get_pg_conn()
        try:
            if q_vec is not None:
                vec_rows = _run_vector_search(conn, tenant_id, q_vec, top_k * 2, fp, pp)
                try:
                    kw_rows = _run_keyword_search(conn, tenant_id, clean_query, top_k * 2, fp, pp)
                except Exception as kw_err:
                    print(f"   ⚠️ keyword search failed (vector-only fallback): {kw_err}")
                    kw_rows = []
                merged = _rrf_merge(vec_rows, kw_rows, top_k)
                print(f"   🔀 hybrid RRF: {len(vec_rows)} vec + {len(kw_rows)} kw → {len(merged)} merged")
                return merged
            else:
                return _run_keyword_search(conn, tenant_id, clean_query, top_k, fp, pp)
        finally:
            conn.close()

    try:
        rows = _run_search(filter_parts, filter_params)

        # If a price filter was active but returned nothing, products may have
        # NULL price_min (not yet re-synced). Retry without the price filter so
        # the AI can still find semantically-matching products.
        has_price_filter = any("price_min" in p or "price_max" in p for p in filter_parts)
        if not rows and has_price_filter:
            non_price_parts = [p for p in filter_parts if "price_min" not in p and "price_max" not in p]
            # Rebuild params: count %s placeholders in removed parts to drop correct params
            removed_parts = [p for p in filter_parts if "price_min" in p or "price_max" in p]
            n_removed = sum(p.count("%s") for p in removed_parts)
            non_price_params = filter_params[n_removed:] if n_removed else filter_params
            print(f"   ⚡ price filter returned 0 — retrying without price filter (products may need re-sync)")
            rows = _run_search(non_price_parts, non_price_params)

    except Exception as e:
        print(f"   ⚠️ search failed: {e}")
        return [], []

    # Guarantee FAQ/policy pages (payment terms, shipping, returns, etc.) get a
    # chance to reach the LLM even when informal phrasing or a strong product
    # context causes the mixed hybrid search above to return only products.
    page_top_k = int(os.getenv("RAG_PAGE_TOP_K", "2"))
    if q_vec is not None and not any(r.get("type") == "page" for r in rows):
        try:
            conn = _get_pg_conn()
            try:
                page_rows = _run_vector_search(conn, tenant_id, q_vec, page_top_k, ["type = %s"], ["page"])
            finally:
                conn.close()
            existing_ids = {r["id"] for r in rows}
            rows.extend(r for r in page_rows if r["id"] not in existing_ids)
        except Exception as e:
            print(f"   ⚠️ page-supplement search failed: {e}")

    chunks: List[str] = []
    raw_docs: List[Dict[str, Any]] = []
    for row in rows:
        chunk = _format_doc(row, max_chars=max_chars)
        if chunk:
            chunks.append(chunk)
            raw_docs.append(row)

    return chunks[:top_k + page_top_k], raw_docs[:top_k + page_top_k]


def search_two_engines(
    query: str, tenant_id: int,
    precomputed_embedding: Optional[List[float]] = None,
) -> Dict[str, list]:
    """Two separate searches so products and website information never
    compete for the same places (2026-10-01, under test):
      - product engine: products only, with the usual price/brand/stock
        filters; its results still go through the relevance check and
        become product cards.
      - website engine: website pages, posts and Store Information only,
        no product filters; its best matches go straight to the AI.
    Returns {"product_chunks", "product_docs", "info_chunks", "info_docs"}."""
    product_top_k = int(os.getenv("RAG_TOP_K", "6"))
    info_top_k = int(os.getenv("RAG_INFO_TOP_K", "5"))
    max_chars = int(os.getenv("RAG_CHUNK_MAX_CHARS", "900"))
    clean_query = _clean_search_query(query)
    out: Dict[str, list] = {"product_chunks": [], "product_docs": [], "info_chunks": [], "info_docs": []}

    q_vec = precomputed_embedding
    if q_vec is None:
        try:
            q_vec = _embed_query(query)
        except Exception as e:
            print(f"   ⚠️ embedding failed — keyword-only search: {e}")

    def _hybrid(fp: List[str], pp: List[Any], k: int) -> List[Dict[str, Any]]:
        conn = _get_pg_conn()
        try:
            if q_vec is None:
                return _run_keyword_search(conn, tenant_id, clean_query, k, fp, pp)
            vec_rows = _run_vector_search(conn, tenant_id, q_vec, k * 2, fp, pp)
            try:
                kw_rows = _run_keyword_search(conn, tenant_id, clean_query, k * 2, fp, pp)
            except Exception as kw_err:
                print(f"   ⚠️ keyword search failed (vector-only fallback): {kw_err}")
                kw_rows = []
            return _rrf_merge(vec_rows, kw_rows, k)
        finally:
            conn.close()

    # Product engine
    try:
        tenant_currency = _get_tenant_currency(tenant_id)
        fp, pp = _parse_filters_sql(clean_query, tenant_currency)
        rows = _hybrid(fp + ["type = 'product'"], pp, product_top_k)
        if not rows and any("price_min" in p or "price_max" in p for p in fp):
            removed = [p for p in fp if "price_min" in p or "price_max" in p]
            n_removed = sum(p.count("%s") for p in removed)
            keep = [p for p in fp if p not in removed]
            rows = _hybrid(keep + ["type = 'product'"], pp[n_removed:] if n_removed else pp, product_top_k)
        for row in rows:
            chunk = _format_doc(row, max_chars=max_chars)
            if chunk:
                out["product_chunks"].append(chunk)
                out["product_docs"].append(row)
    except Exception as e:
        print(f"   ⚠️ product engine failed: {e}")

    # Website engine
    try:
        rows = _hybrid(["type IN ('page', 'post', 'store_info')"], [], info_top_k)
        # The business's own corrections ("Fix this answer") go first.
        try:
            fixes = _find_corrections(tenant_id, q_vec, clean_query)
        except Exception as fix_err:
            print(f"   ⚠️ correction lookup failed: {fix_err}")
            fixes = []
        if fixes:
            fix_ids = {f["id"] for f in fixes}
            rows = fixes + [r for r in rows if r.get("id") not in fix_ids]
            print(f"   ✏️ {len(fixes)} correction(s) matched")
        # Pages whose title names what was asked (Contact, Delivery, Pricing…).
        try:
            titled = [t for t in _find_title_matches(tenant_id, query) if t["id"] not in {r.get("id") for r in rows}]
        except Exception as title_err:
            print(f"   ⚠️ title match failed: {title_err}")
            titled = []
        if titled:
            n_fix = len(fixes)
            rows = rows[:n_fix] + titled + rows[n_fix:]
            print(f"   🏷️ {len(titled)} page(s) added by title: {[t.get('title') for t in titled]}")
        for row in rows:
            chunk = _format_doc(row, max_chars=max_chars)
            if chunk:
                out["info_chunks"].append(chunk)
                out["info_docs"].append(row)
    except Exception as e:
        print(f"   ⚠️ website engine failed: {e}")

    print(f"   🔀 two engines: {len(out['product_docs'])} products + {len(out['info_docs'])} website items")
    return out


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

    def _fmt_price(price_min_val, price_max_val, currency: str = "") -> str:
        try:
            mn = float(price_min_val) if price_min_val is not None else None
            mx = float(price_max_val) if price_max_val is not None else None
            if mn is not None and mn <= 0:
                mn = None
            if mx is not None and mx <= 0:
                mx = None
            ref = mn if mn is not None else mx
            if ref is not None:
                if mn is not None and mx is not None and abs(mx - mn) > 0.01:
                    return f"{_fmt_currency_val(mn, currency)} – {_fmt_currency_val(mx, currency)}"
                elif mn is not None:
                    return _fmt_currency_val(mn, currency)
                elif mx is not None:
                    return _fmt_currency_val(mx, currency)
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
            SELECT id, title, url, sku, brand, price_min, price_max, in_stock, type, image_url,
                   COALESCE(currency, '') AS currency
            FROM documents
            {where_clause}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        qparams.extend([vec_literal, fetch_n])
    else:
        sql = f"""
            SELECT id, title, url, sku, brand, price_min, price_max, in_stock, type, image_url,
                   COALESCE(currency, '') AS currency
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
                "price": _fmt_price(doc.get("price_min"), doc.get("price_max"), doc.get("currency") or ""),
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
