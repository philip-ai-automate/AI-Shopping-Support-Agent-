"""
image_search.py — Visual Product Match (Pro plan only).

Mirrors search.py's hybrid-retrieval shape but for CLIP image embeddings
instead of OpenAI text embeddings. Talks to the isolated clip-embedder
service (127.0.0.1:8020) over HTTP — this file has zero torch/CLIP
dependency itself, keeping ai-backend's venv untouched.

Three confidence tiers (tuned during rollout, not fixed forever):
  score >= CONFIDENT_THRESHOLD          -> "confident": show the product directly
  LOW_CONFIDENCE_FLOOR <= score < that  -> "suggested": ask the customer to
                                            confirm a specific named guess
                                            rather than a generic question
  score < LOW_CONFIDENCE_FLOOR (or no candidates / bad image quality)
                                         -> "none": nothing usable to offer
"""

import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

_CLIP_EMBEDDER_URL = os.getenv("CLIP_EMBEDDER_URL", "http://127.0.0.1:8020").rstrip("/")
CONFIDENT_THRESHOLD = float(os.getenv("VISUAL_MATCH_CONFIDENT_THRESHOLD", "0.85"))
LOW_CONFIDENCE_FLOOR = float(os.getenv("VISUAL_MATCH_LOW_CONFIDENCE_FLOOR", "0.65"))


def _get_pg_conn():
    from db import get_db_connection
    return get_db_connection()


def _extract_pid(doc_id: str) -> str:
    parts = (doc_id or "").split("-")
    return parts[1] if len(parts) >= 2 and parts[0] == "product" else ""


def embed_image_bytes(image_bytes: bytes, timeout: float = 15.0) -> Tuple[Optional[List[float]], Optional[str]]:
    """
    Returns (embedding, error). error is one of:
      None, "undecodable_image", "image_too_small", "service_unavailable"
    """
    try:
        resp = httpx.post(
            f"{_CLIP_EMBEDDER_URL}/embed-image",
            files={"file": ("image.jpg", image_bytes, "application/octet-stream")},
            timeout=timeout,
        )
    except Exception as exc:
        print(f"⚠️ [IMAGE_SEARCH] clip-embedder unreachable: {exc}")
        return None, "service_unavailable"

    if resp.status_code == 422:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            pass
        return None, detail or "undecodable_image"

    if resp.status_code != 200:
        print(f"⚠️ [IMAGE_SEARCH] clip-embedder returned {resp.status_code}: {resp.text[:200]}")
        return None, "service_unavailable"

    return resp.json().get("embedding"), None


def find_matching_products(tenant_id: int, image_bytes: bytes, top_k: int = 3) -> Dict[str, Any]:
    """
    Returns:
      {
        "quality_ok": bool,
        "error": str | None,        # reason when quality_ok is False
        "confident": bool,          # top match >= CONFIDENT_THRESHOLD
        "suggested": bool,          # LOW_CONFIDENCE_FLOOR <= top match < CONFIDENT_THRESHOLD
        "matches": [ {id, product_id, title, price, in_stock, image_url, url, score}, ... ]
      }
    """
    embedding, error = embed_image_bytes(image_bytes)
    if embedding is None:
        return {"quality_ok": False, "error": error, "confident": False, "suggested": False, "matches": []}

    vec_literal = "[" + ",".join(str(x) for x in embedding) + "]"

    try:
        conn = _get_pg_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT id, title, url, price_min, price_max, in_stock, image_url, currency,
                       1 - (image_embedding <=> %s::vector) AS score
                FROM documents
                WHERE tenant_id = %s AND image_embedding IS NOT NULL
                ORDER BY image_embedding <=> %s::vector
                LIMIT %s
                """,
                (vec_literal, tenant_id, vec_literal, top_k),
            )
            rows = cur.fetchall() or []
            cur.close()
        finally:
            conn.close()
    except Exception as exc:
        print(f"⚠️ [IMAGE_SEARCH] pgvector query failed: {exc}")
        return {"quality_ok": True, "error": "search_failed", "confident": False, "suggested": False, "matches": []}

    matches = []
    for row in rows:
        row = dict(row)
        score = float(row.get("score") or 0.0)
        matches.append({
            "id": row.get("id"),
            "product_id": _extract_pid(row.get("id") or ""),
            "title": (row.get("title") or "").strip(),
            "price_min": row.get("price_min"),
            "price_max": row.get("price_max"),
            "currency": row.get("currency") or "",
            "in_stock": bool(row.get("in_stock", True)),
            "image_url": row.get("image_url") or "",
            "url": row.get("url") or "",
            "score": round(score, 4),
        })

    top_score = matches[0]["score"] if matches else 0.0
    confident = bool(matches) and top_score >= CONFIDENT_THRESHOLD
    suggested = bool(matches) and LOW_CONFIDENCE_FLOOR <= top_score < CONFIDENT_THRESHOLD
    return {"quality_ok": True, "error": None, "confident": confident, "suggested": suggested, "matches": matches}


def get_visual_match_settings(tenant_id: int) -> Dict[str, Any]:
    """
    Returns {"enabled": bool, "on_uncertain": "clarify"|"handoff"}.
    "enabled" requires BOTH the plan-level gate (plans.feat_visual_match,
    Pro only) AND the merchant's own opt-in toggle
    (tenants.features.visual_product_match) — Pro alone doesn't turn it on.
    """
    try:
        conn = _get_pg_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT p.feat_visual_match, t.features
                FROM tenants t
                JOIN plans p ON p.id = t.plan_id
                WHERE t.id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            cur.close()
        finally:
            conn.close()
    except Exception as exc:
        print(f"⚠️ [IMAGE_SEARCH] get_visual_match_settings error: {exc}")
        return {"enabled": False, "on_uncertain": "clarify"}

    if not row or not row.get("feat_visual_match"):
        return {"enabled": False, "on_uncertain": "clarify"}

    features = row.get("features")
    if isinstance(features, str):
        import json
        try:
            features = json.loads(features)
        except Exception:
            features = {}
    elif not isinstance(features, dict):
        features = {}

    return {
        "enabled": bool(features.get("visual_product_match", False)),
        "on_uncertain": features.get("visual_match_on_uncertain", "clarify"),
    }
