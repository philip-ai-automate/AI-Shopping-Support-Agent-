"""live_fetch.py

Live Mode (No Data Sync)
------------------------
In Live Mode, PhiXtra does NOT export/sync/store products/pages/posts.
Instead, we fetch relevant content on-demand from the customer WordPress site
via a lightweight PhiXtra plugin REST endpoint.

This keeps data where it belongs (on the customer's site) while still giving
the AI agent fresh, real-time context.
"""

from __future__ import annotations

import os
from typing import List

import requests


def _normalize_site_url(site_domain: str) -> str:
    d = (site_domain or "").strip()
    if not d:
        return ""
    if d.startswith("http://") or d.startswith("https://"):
        return d.rstrip("/")
    return ("https://" + d).rstrip("/")


def live_fetch_context(
    query: str,
    site_domain: str,
    api_key: str,
    live_types: str = "products,pages,posts",
) -> List[str]:
    """Fetch live context chunks from the customer's site.

    Returns a list[str] of compact chunks similar to Azure Search chunks.

    Never raises (to avoid breaking /chat). If anything fails, returns [].
    """

    base = _normalize_site_url(site_domain)
    if not base:
        return []

    # The plugin registers: /wp-json/phixtra/v1/live-search
    path = os.getenv("PHIXTRA_LIVE_ENDPOINT_PATH", "/wp-json/phixtra/v1/live-search")
    url = base + path

    timeout = float(os.getenv("PHIXTRA_LIVE_TIMEOUT", "6"))

    def _fetch(q: str) -> List[str]:
        try:
            r = requests.get(
                url,
                params={
                    "api_key": api_key,
                    "q": (q or "").strip(),
                    "types": (live_types or "").strip(),
                },
                timeout=timeout,
                headers={"Accept": "application/json"},
            )
            if r.status_code != 200:
                return []
            data = r.json() if r.content else {}
            chunks = data.get("chunks") or []
            if not isinstance(chunks, list):
                return []
            out: List[str] = []
            for c in chunks:
                if isinstance(c, str) and c.strip():
                    out.append(c.strip())
            top_k = int(os.getenv("RAG_TOP_K", "4"))
            return out[: max(0, top_k)]
        except Exception:
            return []

    q0 = (query or "").strip()
    out = _fetch(q0)
    if out:
        return out

    # Retry with simplified queries (helps Woo/WP search match better)
    # e.g. "iphone 12" -> "iphone"
    if q0 and (" " in q0 or any(ch.isdigit() for ch in q0)):
        # remove multiple spaces
        q1 = " ".join(q0.split())
        # first word
        q_first = q1.split(" ")[0] if " " in q1 else q1
        out = _fetch(q_first)
        if out:
            return out
        # remove digits
        q_nodigits = "".join([ch for ch in q1 if not ch.isdigit()]).strip()
        if q_nodigits and q_nodigits != q_first:
            out = _fetch(q_nodigits)
            if out:
                return out

    return []
