"""
visual_match.py — Visual Product Match orchestration for the WhatsApp gateway.

Called from meta_webhook.py when a customer sends an image. Resolves the
media, calls ai-backend's /visual-match endpoint, and returns which branch
applies:

  "skip"      — feature off, image doesn't look like a product ask, or the
                media/CLIP pipeline failed for an infra reason (not a
                quality/match problem) — fall through to normal AI chat
                with whatever caption text exists.
  "confident" — a product cleared the confident threshold. Caller should
                fold the matched title into the AI message so the existing
                search + Tap-to-Browse pipeline picks it up naturally.
  "suggested" — a specific product is plausible but not certain (between
                the low-confidence floor and the confident threshold).
                Caller should have the AI confirm this exact guess with the
                customer ("did you mean X?") rather than assume it or ask a
                blind question. Always tries this — does NOT consult the
                merchant's clarify/handoff setting, because there's a
                concrete, cheap-to-verify guess on the table, unlike the
                "uncertain" case below where there's genuinely nothing to
                offer.
  "uncertain" — no usable candidate (score below the floor, or bad image
                quality). Caller checks `action` ("clarify" or "handoff",
                merchant-configured) to decide what happens next.

Guard rail: only treated as a product-availability ask when there's no
caption at all, or the caption contains an availability-intent keyword.
Otherwise a random image (meme, receipt already handled upstream, etc.)
with an unrelated caption is left alone — visual matching on every image
regardless of intent would spam merchants with false handoff alerts.
"""

import os

import httpx

from media_resolver import resolve_and_download_media
from wa_db import get_visual_match_settings

_AI_BACKEND_URL = os.getenv("AI_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

_AVAILABILITY_KEYWORDS = (
    "available", "availability", "have this", "have it", "in stock",
    "stock", "price", "cost", "how much", "sell this", "buy this",
    "do you sell", "do you have", "is this",
)


def _looks_like_product_ask(caption: str) -> bool:
    caption = (caption or "").strip().lower()
    if not caption:
        return True
    if len(caption.split()) > 15:
        return False
    return any(kw in caption for kw in _AVAILABILITY_KEYWORDS)


async def evaluate_visual_match(
    tenant_id: int,
    api_key: str,
    access_token: str,
    media_id: str,
    caption: str,
) -> dict:
    print(f"   [VISUAL_MATCH] evaluate tenant_id={tenant_id} media_id={media_id!r} caption={caption!r}")

    settings = get_visual_match_settings(tenant_id)
    if not settings["enabled"]:
        print(f"   [VISUAL_MATCH] skip — feature not enabled for tenant_id={tenant_id} (settings={settings})")
        return {"branch": "skip"}

    if not _looks_like_product_ask(caption):
        print(f"   [VISUAL_MATCH] skip — caption {caption!r} didn't pass the product-ask guard")
        return {"branch": "skip"}

    image_bytes = await resolve_and_download_media(media_id, access_token)
    if image_bytes is None:
        # Infra hiccup resolving media — not a quality/match problem, don't
        # escalate over it. Let the normal AI flow handle whatever text exists.
        print(f"   [VISUAL_MATCH] skip — media resolution returned no bytes for media_id={media_id!r}")
        return {"branch": "skip"}

    print(f"   [VISUAL_MATCH] resolved {len(image_bytes)} bytes for media_id={media_id!r}, calling ai-backend")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_AI_BACKEND_URL}/visual-match",
                data={"api_key": api_key},
                files={"file": ("image.jpg", image_bytes, "application/octet-stream")},
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as exc:
        print(f"⚠️ [VISUAL_MATCH] /visual-match call failed: {exc}")
        return {"branch": "skip"}

    print(f"   [VISUAL_MATCH] /visual-match result: {result}")

    if not result.get("allowed"):
        return {"branch": "skip"}

    if result.get("confident") and result.get("matches"):
        top = result["matches"][0]
        return {"branch": "confident", "product_title": top["title"], "score": top["score"], "in_stock": top.get("in_stock", True)}

    if result.get("suggested") and result.get("matches"):
        top = result["matches"][0]
        return {"branch": "suggested", "product_title": top["title"], "score": top["score"], "in_stock": top.get("in_stock", True)}

    if not result.get("quality_ok") and result.get("error") == "service_unavailable":
        # CLIP embedding service was down — an infra hiccup, not a photo
        # quality/match problem. Don't escalate to clarify/handoff over it.
        print("⚠️ [VISUAL_MATCH] clip-embedder unavailable — skipping instead of escalating")
        return {"branch": "skip"}

    return {"branch": "uncertain", "action": result.get("on_uncertain", "clarify")}
