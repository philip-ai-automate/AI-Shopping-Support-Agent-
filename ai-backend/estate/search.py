"""
estate/search.py — pgvector semantic search over re_property_listings.

Falls back to tsvector keyword search when no embedding is available.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import psycopg2.extras
from db import get_db_connection

try:
    from openai import OpenAI as _OAI
    _oai = _OAI(api_key=os.getenv("OPENAI_API_KEY", ""))
except Exception:
    _oai = None


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed_query(text: str) -> list | None:
    if not _oai or not text:
        return None
    try:
        resp = _oai.embeddings.create(
            model=os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
            input=text.strip()[:8000],
        )
        return resp.data[0].embedding
    except Exception as e:
        print(f"⚠️ [ESTATE SEARCH] embed_query error: {e}")
        return None


# ── Listing search ────────────────────────────────────────────────────────────

def search_listings(
    query: str,
    tenant_id: int,
    top_k: int = 5,
    precomputed_embedding: list = None,
    max_price: float = None,
    area: str = None,
) -> list[dict]:
    """
    Search re_property_listings for this tenant using pgvector cosine similarity.
    Falls back to tsvector keyword search when no embedding is available.
    Only returns status='available' listings.
    area filters on state / location / lga (case-insensitive); max_price filters on price.
    """
    embedding = precomputed_embedding
    if embedding is None:
        embedding = embed_query(query)

    conn = get_db_connection()
    if not conn:
        return []

    _SELECT = """
        SELECT
            id, title, property_type, transaction_type,
            location, lga, state, price, price_negotiable,
            bedrooms, bathrooms, toilets, size_sqm,
            title_document, features, status, images, description
    """

    def _run(cur) -> list:
        extra_where = ""
        extra_params: list = []

        if area:
            parts = [a.strip() for a in area.split(",") if a.strip()]
            clauses = []
            for a in parts:
                like = f"%{a}%"
                clauses.append("(state ILIKE %s OR location ILIKE %s OR lga ILIKE %s)")
                extra_params.extend([like, like, like])
            extra_where += " AND (" + " OR ".join(clauses) + ")"

        if max_price:
            extra_where += " AND (price IS NULL OR price <= %s)"
            extra_params.append(float(max_price) * 1.10)

        if embedding:
            vec = "[" + ",".join(str(x) for x in embedding) + "]"
            cur.execute(
                _SELECT + f"""
                , embedding <=> %s::vector AS distance
                FROM re_property_listings
                WHERE tenant_id = %s AND status = 'available'{extra_where}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                [vec, tenant_id] + extra_params + [vec, top_k],
            )
        else:
            cur.execute(
                _SELECT + f"""
                , ts_rank(search_vector, plainto_tsquery('english', %s)) AS distance
                FROM re_property_listings
                WHERE tenant_id = %s
                  AND status = 'available'
                  AND search_vector @@ plainto_tsquery('english', %s){extra_where}
                ORDER BY distance DESC
                LIMIT %s
                """,
                [query, tenant_id, query] + extra_params + [top_k],
            )
        return [dict(r) for r in (cur.fetchall() or [])]

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        rows = _run(cur)
        cur.close()
        return rows

    except Exception as e:
        print(f"⚠️ [ESTATE SEARCH] search_listings error: {e}")
        return []
    finally:
        conn.close()


# ── Formatting helpers ────────────────────────────────────────────────────────

def format_listing_price(price, negotiable: bool = False) -> str:
    if price is None:
        return "Price on request"
    try:
        p = float(price)
        if p >= 1_000_000_000:
            s = f"₦{p / 1_000_000_000:.2g}B"
        elif p >= 1_000_000:
            s = f"₦{p / 1_000_000:.2g}M"
        else:
            s = f"₦{p:,.0f}"
        return s + (" (negotiable)" if negotiable else "")
    except Exception:
        return str(price)


def format_listings_for_context(listings: list[dict]) -> list[str]:
    """Format listing dicts as text chunks for the LLM context window."""
    chunks = []
    for L in listings:
        feats = L.get("features") or []
        if isinstance(feats, str):
            try:
                feats = json.loads(feats)
            except Exception:
                feats = []

        feat_str = ", ".join(str(f) for f in feats[:8]) if feats else "—"
        price_str = format_listing_price(L.get("price"), bool(L.get("price_negotiable")))

        lines = [
            f"[Listing ID #{L['id']}] {L.get('title', '')}",
            f"  Type: {(L.get('property_type') or '?').replace('_', ' ').title()} | {(L.get('transaction_type') or '?').title()}",
            f"  Location: {L.get('location') or '?'}, {L.get('lga') or ''}, {L.get('state') or 'Lagos'}",
            f"  Price: {price_str}",
        ]
        if L.get("bedrooms"):
            lines.append(
                f"  Beds: {L['bedrooms']} | Baths: {L.get('bathrooms', '?')} | Toilets: {L.get('toilets', '?')}"
            )
        if L.get("size_sqm"):
            lines.append(f"  Size: {L['size_sqm']} sqm")
        if L.get("title_document"):
            lines.append(f"  Title: {(L['title_document'] or '').replace('_', ' ')}")
        lines.append(f"  Features: {feat_str}")
        if L.get("description"):
            lines.append(f"  Info: {(L['description'] or '')[:300]}")

        chunks.append("\n".join(lines))
    return chunks
