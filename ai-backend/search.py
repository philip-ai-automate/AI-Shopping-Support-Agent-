# search.py
"""
Hybrid retrieval (Option B):
- Generates an embedding for the user's query (Azure OpenAI)
- Runs hybrid search against Azure AI Search:
    - keyword search over searchable text fields
    - vector search over content_vector
- Optionally uses semantic query type if a semantic config name is provided.

This module returns "context chunks" as short, human-readable snippets for the LLM.
"""

import os
import re
import json
from typing import Any, Dict, List, Optional

import requests
from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv()


def _client() -> AzureOpenAI:
    return AzureOpenAI(
        api_key=os.getenv("AZURE_OPENAI_KEY"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01"),
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    )


def _embed_query(text: str) -> List[float]:
    deployment = os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT") or os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT") or ""
    if not deployment:
        raise RuntimeError("AZURE_OPENAI_EMBED_DEPLOYMENT is not set")

    text = (text or "").strip()
    if not text:
        text = "empty"

    resp = _client().embeddings.create(model=deployment, input=[text])
    emb = resp.data[0].embedding
    return [float(x) for x in emb]


def _search_endpoint(index_name: str) -> str:
    ep = (os.getenv("AZURE_SEARCH_ENDPOINT") or "").rstrip("/")
    if not ep:
        raise RuntimeError("AZURE_SEARCH_ENDPOINT is not set")
    return f"{ep}/indexes/{index_name}/docs/search"


def _search_headers() -> Dict[str, str]:
    key = os.getenv("AZURE_SEARCH_KEY") or os.getenv("AZURE_SEARCH_ADMIN_KEY") or ""
    if not key:
        raise RuntimeError("AZURE_SEARCH_KEY is not set")
    return {"Content-Type": "application/json", "api-key": key}


def _format_doc(doc: Dict[str, Any], max_chars: int) -> str:
    # Build a compact snippet for the LLM context
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
            if pmin is not None and pmax is not None and pmin != pmax:
                parts.append(f"Price: {pmin}–{pmax}")
            elif pmin is not None:
                parts.append(f"Price: {pmin}")
            elif pmax is not None:
                parts.append(f"Price: {pmax}")
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


def _parse_filters(query: str) -> Optional[str]:
    """
    Extracts OData $filter expressions from a natural language query.
    Handles: price (min/max), stock/quantity (in_stock), brand, category, tag.
    Returns a combined OData filter string, or None if nothing was detected.
    """
    parts: List[str] = []
    q = (query or "").lower()

    # ── Price ─────────────────────────────────────────────────────────────────
    # Match a monetary number, optionally preceded by a currency symbol
    _n = r'(?:[\$£€]\s*)?(\d[\d,]*(?:\.\d{1,2})?)'

    # Range: "between £10 and £50" | "£10 - £50" | "10 to 50"
    _range = re.search(
        rf'between\s+{_n}\s+(?:and|to)\s+{_n}|{_n}\s*(?:-|–|—|to)\s*{_n}(?=\s|$)',
        q,
    )
    if _range:
        gs = [g for g in _range.groups() if g is not None]
        if len(gs) >= 2:
            try:
                lo = float(gs[0].replace(',', ''))
                hi = float(gs[1].replace(',', ''))
                if 0 < lo <= hi:
                    # Variable products: price_min = cheapest variant, price_max = most expensive.
                    # Simple products:   price_min = the price,         price_max = NULL.
                    # Require price_min gt 0 to exclude WooCommerce products where price = 0
                    # (unpriced products). Include if cheapest variant is within range AND
                    # the most expensive variant doesn't exceed the upper bound (or no price_max).
                    parts.append(
                        f"price_min ge {lo} and price_min le {hi} and price_min gt 0 and "
                        f"(price_max le {hi} or price_max eq null)"
                    )
            except Exception:
                pass
    else:
        # Upper bound: "under £50", "below 100", "less than £30", "up to £200", "max £50"
        _max_m = re.search(
            rf'(?:under|below|less than|cheaper than|up to|no more than|max(?:imum)?(?:\s+price)?(?:\s+of)?)\s+{_n}',
            q,
        )
        if _max_m:
            try:
                hi = float(_max_m.group(1).replace(',', ''))
                # Use price_min as the anchor for upper-bound queries:
                # - Variable product: price_min is the cheapest variant price.
                #   If price_min <= hi, at least one variant is within budget.
                # - Simple product:   price_min is the only price.
                # Require price_min gt 0 to exclude products where WooCommerce
                # stored 0 because no real price was set (those slip through otherwise).
                parts.append(f"price_min le {hi} and price_min gt 0")
            except Exception:
                pass

        # Lower bound: "over £50", "above £100", "more than £30", "at least £20", "from £50"
        _min_m = re.search(
            rf'(?:over|above|more than|at least|starting (?:at|from)|from)\s+{_n}',
            q,
        )
        if _min_m:
            try:
                lo = float(_min_m.group(1).replace(',', ''))
                # Use price_max as the anchor for lower-bound queries:
                # - Variable product: price_max is the most expensive variant.
                #   If price_max >= lo, at least one variant meets the minimum.
                # - Simple product (price_max NULL): fall back to price_min.
                parts.append(
                    f"(price_max ge {lo} or (price_max eq null and price_min ge {lo}))"
                )
            except Exception:
                pass

    # ── Stock / Quantity ──────────────────────────────────────────────────────
    if re.search(
        r'\bin[\s-]?stock\b|\bonly\s+(?:items?\s+)?(?:in\s+stock|available)\b|\bavailable\s+(?:now|only|items?)\b',
        q,
    ):
        parts.append("in_stock eq true")
    elif re.search(r'\bout[\s-]?of[\s-]?stock\b', q):
        parts.append("in_stock eq false")

    # ── Brand ─────────────────────────────────────────────────────────────────
    # "brand: Samsung" | "Samsung brand" | "by Samsung"
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
        bv = _brand_m.group(1).strip().replace("'", "").strip()
        if bv and len(bv) > 1:
            parts.append(f"search.ismatch('{bv}', 'brand')")

    # ── Category ──────────────────────────────────────────────────────────────
    # "category: Electronics" | "in category laptops"
    _cat_m = re.search(
        r'(?:category[:\s]+|in\s+category\s+)([a-z0-9][a-z0-9 \-]{1,40})(?=\s|$|,|\.)',
        q,
    )
    if _cat_m:
        cv = _cat_m.group(1).strip().replace("'", "")
        if cv:
            parts.append(f"search.ismatch('{cv}', 'title,content')")

    # ── Tag ───────────────────────────────────────────────────────────────────
    # "tagged as sale" | "tag: wireless" | "tagged with gaming"
    _tag_m = re.search(
        r'(?:tag(?:ged)?(?:\s+(?:as|with))?[:\s]+)([a-z0-9][a-z0-9 \-]{1,40})(?=\s|$|,|\.)',
        q,
    )
    if _tag_m:
        tv = _tag_m.group(1).strip().replace("'", "")
        if tv:
            parts.append(f"search.ismatch('{tv}', 'title,content')")

    return " and ".join(parts) if parts else None


def search_documents(query: str, index_name: str, semantic_config: Optional[str] = None) -> List[str]:
    """
    Returns a list[str] of compact context chunks.
    Wrapper kept for backward compatibility — calls the full function and discards raw docs.
    """
    chunks, _ = search_documents_with_meta(query, index_name, semantic_config)
    return chunks


def search_documents_with_meta(
    query: str, index_name: str, semantic_config: Optional[str] = None
) -> tuple:
    """
    Returns (chunks: List[str], raw_docs: List[Dict])

    raw_docs contains the actual Azure Search document fields needed to build
    product cards: id, title, url, price_min, price_max, in_stock, type, sku, brand.

    IMPORTANT:
    - Requires documents in the index to have content_vector populated.
    - If vector search fails for any reason, we fall back to keyword+semantic.
    """
    top_k = int(os.getenv("RAG_TOP_K", "6"))
    max_chars = int(os.getenv("RAG_CHUNK_MAX_CHARS", "900"))
    api_version = os.getenv("AZURE_SEARCH_API_VERSION", "2025-09-01")

    # 1) Build query embedding
    try:
        q_vec = _embed_query(query)
    except Exception:
        q_vec = None

    def do_request(payload: Dict[str, Any]) -> Dict[str, Any]:
        url = _search_endpoint(index_name) + f"?api-version={api_version}"
        r = requests.post(url, headers=_search_headers(), data=json.dumps(payload), timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Azure Search query failed: {r.status_code} {r.text}")
        return r.json() or {}

    # 2) Parse any filter constraints from the user's natural language query
    filter_expr = _parse_filters(query)
    if filter_expr:
        print(f"   🔍 Azure Search filter applied: {filter_expr}")

    # 3) Hybrid (vector + keyword)
    results_json: Dict[str, Any] = {}
    try:
        payload: Dict[str, Any] = {
            "search": query or "*",
            "top": top_k,
            "select": "id,title,content,url,sku,brand,price_min,price_max,in_stock,type,categories_text,site_url,tenant_id,image_url",
        }

        if filter_expr:
            payload["filter"] = filter_expr

        if semantic_config:
            payload["queryType"] = "semantic"
            payload["semanticConfiguration"] = semantic_config

        if q_vec is not None:
            payload["vectors"] = [
                {"value": q_vec, "fields": "content_vector", "k": max(top_k, 10)}
            ]

        results_json = do_request(payload)
    except Exception:
        # 4) Fallback: keyword/semantic only (keep filter if we have one)
        payload = {
            "search": query or "*",
            "top": top_k,
            "select": "id,title,content,url,sku,brand,price_min,price_max,in_stock,type,categories_text,site_url,tenant_id,image_url",
        }
        if filter_expr:
            payload["filter"] = filter_expr
        if semantic_config:
            payload["queryType"] = "semantic"
            payload["semanticConfiguration"] = semantic_config
        try:
            results_json = do_request(payload)
        except Exception:
            # If both hybrid and fallback search fail, return empty results
            # rather than crashing the /chat endpoint entirely.
            return [], []

    values = results_json.get("value") or []
    chunks: List[str] = []
    raw_docs: List[Dict[str, Any]] = []

    for v in values:
        if not isinstance(v, dict):
            continue
        chunk = _format_doc(v, max_chars=max_chars)
        if chunk:
            chunks.append(chunk)
            raw_docs.append(v)

    return chunks[: max(0, top_k)], raw_docs[: max(0, top_k)]


def search_related_products(
    index_name: str,
    exclude_names: set,
    prod_type: str = "",
    category: str = "",
    top: int = 2,
    search_hint: str = "",
) -> List[Dict]:
    """
    Returns up to `top` related products for cross-selling.
    Used by Feature 5 (Automated Cross-selling / Related Product Cards).

    Cross-sells by CATEGORY and TYPE — NOT by price.

    - prod_type: hard equality filter (e.g. type eq 'Laptop') — the primary
      fence that prevents phones appearing when the user asked about laptops.
    - category: additional search.ismatch filter on categories_text when available.
    - search_hint: the first recommended product's title, used as the semantic
      search query so Azure finds genuinely similar products by name.
    - Always filters to in_stock eq true.
    - Excludes products already recommended (exclude_names — lowercased set).
    - Falls back gracefully: returns [] if the search fails for any reason.
    """

    def _fmt_price(price_min_val, price_max_val) -> str:
        try:
            mn = float(price_min_val) if price_min_val is not None else None
            mx = float(price_max_val) if price_max_val is not None else None
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
        """Extract WooCommerce product ID from Azure doc key e.g. 'product-123' → '123'."""
        parts = (doc_id or "").split("-")
        return parts[1] if len(parts) >= 2 and parts[0] == "product" else ""

    api_version = os.getenv("AZURE_SEARCH_API_VERSION", "2025-09-01")

    # ── Build filter: type + category + in_stock. NO price filtering. ────────
    # Cross-selling is about relevance (same kind of product), not price proximity.
    # Price-based filtering was causing phones to appear for laptop queries because
    # simple products have price_max=null which broke the range filter entirely.

    # Strip single quotes from all user-controlled strings to prevent filter injection
    safe_type     = (prod_type or "").replace("'", "")
    safe_category = (category  or "").replace("'", "")

    # in_stock is always required — never show out-of-stock cross-sell suggestions
    filter_parts = ["in_stock eq true"]

    # type is the primary hard fence: 'Laptop' can never match 'Mobile Phone'
    if safe_type:
        filter_parts.append(f"type eq '{safe_type}'")

    # categories_text as additional refinement when the field is populated
    if safe_category:
        filter_parts.append(f"search.ismatch('{safe_category}', 'categories_text')")

    filter_expr = " and ".join(filter_parts)

    # Use the first recommended product's title as the search query.
    # This drives Azure semantic matching toward genuinely similar products
    # (e.g. other laptops when the first result is a ThinkPad).
    # searchFields restricts matching to title only so a phone description
    # that mentions "laptop" in its body text cannot cause a false match.
    safe_hint = (search_hint or safe_category or safe_type or "").strip()
    payload: Dict[str, Any] = {
        "search": safe_hint or "*",
        "searchFields": "title",
        "top": max(top * 3, 6),   # fetch extras so we have room to exclude
        "select": "id,title,url,sku,brand,price_min,price_max,in_stock,type,image_url",
        "filter": filter_expr,
    }

    try:
        url = _search_endpoint(index_name) + f"?api-version={api_version}"
        r = requests.post(url, headers=_search_headers(), data=json.dumps(payload), timeout=15)
        if r.status_code != 200:
            print(f"   ⚠️ search_related_products: Azure Search returned {r.status_code}")
            return []

        values = (r.json() or {}).get("value") or []
        related: List[Dict] = []

        for doc in values:
            if not isinstance(doc, dict):
                continue
            name = (doc.get("title") or "").strip()
            if not name:
                continue
            # Skip products already in the main recommendation set
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
    index_name: str,
    tenant_id: int,
    model_hint: str,
    spec_key: str,
    spec_value: str,
    qualifier: str = "",
    sources: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Write a small verified spec document into Azure AI Search.

    This allows future queries to be answered from RAG without web lookup.
    Uses mergeOrUpload with a stable id to avoid index bloat.

    Requires AZURE_SEARCH_ENDPOINT + AZURE_SEARCH_KEY (admin key).
    """
    from web_spec_lookup import make_verified_spec_doc_id

    api_version = os.getenv("AZURE_SEARCH_API_VERSION", "2025-09-01")
    ep = (os.getenv("AZURE_SEARCH_ENDPOINT") or "").rstrip("/")
    if not ep:
        return

    doc_id = make_verified_spec_doc_id(tenant_id=int(tenant_id), model_hint=model_hint or "", spec_key=spec_key or "")
    srcs = sources or []
    src_urls = [s.get("url") for s in srcs if isinstance(s, dict) and s.get("url")]

    # Put the important info in 'content' for hybrid retrieval
    text = f"Verified spec: {model_hint} {spec_key}: {spec_value}. {qualifier}".strip()

    doc = {
        "id": doc_id,
        "tenant_id": int(tenant_id),
        "type": "verified_spec",
        "title": f"{model_hint} — {spec_key}".strip(" —"),
        "content": text,
        "url": (src_urls[0] if src_urls else ""),
        "brand": "",
        "sku": "",
        "price_min": None,
        "price_max": None,
        "in_stock": None,
        "site_url": "",
        "spec_key": spec_key,
        "spec_value": spec_value,
        "spec_sources": json.dumps(src_urls)[:2000],
    }

    # NOTE: content_vector will be auto-generated by your existing sync pipeline.
    # If your index requires it at write-time, you can extend this later.

    url = f"{ep}/indexes/{index_name}/docs/index?api-version={api_version}"
    payload = {"value": [{"@search.action": "mergeOrUpload", **doc}]}
    try:
        requests.post(url, headers=_search_headers(), data=json.dumps(payload), timeout=30)
    except Exception:
        return
