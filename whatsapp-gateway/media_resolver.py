"""
media_resolver.py — resolve an inbound WhatsApp media ID to raw bytes.

Inbound WhatsApp messages only ever carry a media `id`, never a direct
`link` (that's only present on outbound messages the gateway itself sends).
Nothing in this codebase previously resolved that ID to a downloadable file
— message_normalizer.py stores it as `media_url` but it's actually just the
opaque Meta media ID until this module turns it into bytes.

Two-step Graph API flow, both authenticated with the tenant's access_token:
  1. GET /{media-id}          -> {"url": "<short-lived signed url>", ...}
  2. GET <signed url>         -> raw bytes (must carry the same Bearer token)
"""

import httpx

_GRAPH_BASE = "https://graph.facebook.com/v19.0"

# WhatsApp media limits: images up to 5MB. Refuse anything larger to avoid
# downloading something unexpected (docs/video misrouted as image, etc.).
_MAX_BYTES = 6 * 1024 * 1024


async def resolve_and_download_media(media_id: str, access_token: str) -> bytes | None:
    """Returns raw image bytes, or None on any failure (never raises)."""
    if not media_id or not access_token:
        return None

    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            lookup = await client.get(f"{_GRAPH_BASE}/{media_id}", headers=headers)
            if lookup.status_code != 200:
                print(f"⚠️ [MEDIA] lookup failed media_id={media_id} status={lookup.status_code}: {lookup.text[:200]}")
                return None

            meta = lookup.json()
            signed_url = meta.get("url")
            if not signed_url:
                print(f"⚠️ [MEDIA] no url in lookup response media_id={media_id}")
                return None

            file_size = meta.get("file_size")
            if isinstance(file_size, int) and file_size > _MAX_BYTES:
                print(f"⚠️ [MEDIA] media_id={media_id} too large ({file_size} bytes) — skipping")
                return None

            download = await client.get(signed_url, headers=headers)
            if download.status_code != 200:
                print(f"⚠️ [MEDIA] download failed media_id={media_id} status={download.status_code}")
                return None

            content = download.content
            if len(content) > _MAX_BYTES:
                print(f"⚠️ [MEDIA] downloaded content too large media_id={media_id} ({len(content)} bytes) — discarding")
                return None

            return content
    except Exception as exc:
        print(f"⚠️ [MEDIA] resolve_and_download_media error media_id={media_id}: {exc}")
        return None
