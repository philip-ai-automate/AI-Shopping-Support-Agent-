"""web_spec_lookup.py

Verified web lookup for numeric/spec questions.

Design goals:
- Never guess a number.
- Only return a spec value when a trusted web result is found.
- Keep the implementation provider-agnostic via environment variables.

Supported providers (set WEB_SEARCH_PROVIDER):
- serper  (SERPER_API_KEY)
- bing    (BING_SEARCH_KEY) uses https://api.bing.microsoft.com/v7.0/search

If no provider is configured, functions return found=False so the caller can
respond safely ("can't verify").
"""

from __future__ import annotations

import os
import re
import json
import hashlib
from urllib.parse import urlparse
from typing import Dict, List, Optional, Tuple

import requests


DEFAULT_TRUSTED_DOMAINS = [
    # HP
    "hp.com",
    "support.hp.com",
    "h20195.www2.hp.com",
    # Dell
    "dell.com",
    "www.dell.com",
    # Lenovo
    "lenovo.com",
    "psref.lenovo.com",
    # Apple
    "apple.com",
    "support.apple.com",
    # Samsung
    "samsung.com",
    "www.samsung.com",
    # ASUS
    "asus.com",
    "www.asus.com",
    # Acer
    "acer.com",
    "www.acer.com",
    # Microsoft (Surface)
    "microsoft.com",
    "www.microsoft.com",
    # LG
    "lg.com",
    "www.lg.com",
    # Razer
    "razer.com",
    "www.razer.com",
    # MSI
    "msi.com",
    "www.msi.com",
    # Huawei
    "consumer.huawei.com",
    # Google (Chromebook / Pixel)
    "store.google.com",
    # Sony / VAIO
    "sony.com",
    "vaio.com",
    # Panasonic (Toughbook)
    "panasonic.com",
    # Fujitsu
    "fujitsu.com",
    # Toshiba / Dynabook
    "dynabook.com",
    "toshiba.com",
    # Intel (CPU specs)
    "ark.intel.com",
    # AMD (CPU specs)
    "amd.com",
    "www.amd.com",
]


def _get_trusted_domains() -> List[str]:
    raw = (os.getenv("WEB_SPEC_TRUSTED_DOMAINS") or "").strip()
    if raw:
        # comma-separated env override
        return [d.strip().lower() for d in raw.split(",") if d.strip()]
    return DEFAULT_TRUSTED_DOMAINS


def _build_trusted_domains(extra: Optional[List[str]] = None) -> List[str]:
    """Return the final allowlist, merging system defaults with per-tenant extras.

    Per-tenant domains (stored in features JSON under 'verified_specs_trusted_domains')
    are appended after the system defaults so they never replace them — they only add to them.
    Duplicates are silently ignored.
    """
    base = list(_get_trusted_domains())  # copy so we never mutate the original
    if extra:
        seen = set(base)
        for d in extra:
            d = (d or "").strip().lower()
            if d and d not in seen:
                base.append(d)
                seen.add(d)
    return base


def _domain_ok(url: str, allowlist: List[str]) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        for d in allowlist:
            d = (d or "").lower().strip()
            if not d:
                continue
            if host == d or host.endswith("." + d):
                return True
        return False
    except Exception:
        return False


def _provider() -> str:
    return (os.getenv("WEB_SEARCH_PROVIDER") or "").strip().lower()


def _serper_search(query: str, num: int = 5) -> List[Dict]:
    key = (os.getenv("SERPER_API_KEY") or "").strip()
    if not key:
        return []
    r = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        data=json.dumps({"q": query, "num": num}),
        timeout=25,
    )
    if r.status_code != 200:
        return []
    data = r.json() or {}
    out = []
    for item in (data.get("organic") or [])[:num]:
        out.append(
            {
                "title": item.get("title") or "",
                "url": item.get("link") or "",
                "snippet": item.get("snippet") or "",
            }
        )
    return out


def _bing_search(query: str, num: int = 5) -> List[Dict]:
    key = (os.getenv("BING_SEARCH_KEY") or "").strip()
    if not key:
        return []
    endpoint = (os.getenv("BING_SEARCH_ENDPOINT") or "https://api.bing.microsoft.com/v7.0/search").strip()
    params = {"q": query, "count": num, "mkt": os.getenv("BING_SEARCH_MARKET", "en-GB")}
    r = requests.get(endpoint, headers={"Ocp-Apim-Subscription-Key": key}, params=params, timeout=25)
    if r.status_code != 200:
        return []
    data = r.json() or {}
    out = []
    for item in ((data.get("webPages") or {}).get("value") or [])[:num]:
        out.append(
            {
                "title": item.get("name") or "",
                "url": item.get("url") or "",
                "snippet": item.get("snippet") or "",
            }
        )
    return out


def web_search(query: str, num: int = 5) -> List[Dict]:
    """Returns a list of {title,url,snippet}. Empty list if not configured."""
    p = _provider()
    if p == "serper":
        return _serper_search(query, num=num)
    if p == "bing":
        return _bing_search(query, num=num)
    return []


_SPEC_INTENT_RE = re.compile(
    r"\b(weight|weighs|mass|dimensions|size|height|width|depth|thickness|battery|wh|mah|watt|wattage|screen|inches|ports|usb[- ]?c|hdmi|cpu|processor|ram|memory|storage|ssd|hdd)\b",
    re.IGNORECASE,
)


def is_spec_question(text: str) -> bool:
    return bool(_SPEC_INTENT_RE.search(text or ""))


def _extract_weight(text: str) -> Optional[str]:
    """Extract a weight token from text. Returns the first plausible weight string."""
    if not text:
        return None
    # common laptop weight patterns, kg or lb
    patterns = [
        r"(\d+(?:\.\d+)?)\s*(kg|kilograms?)\b",
        r"(\d+(?:\.\d+)?)\s*(lb|lbs|pounds?)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            num, unit = m.group(1), m.group(2)
            unit_norm = "kg" if unit.lower().startswith("k") else "lb"
            return f"{num} {unit_norm}"
    return None


def _extract_dimensions(text: str) -> Optional[str]:
    """Extract W x D x H dimensions from text, in mm or inches."""
    if not text:
        return None
    # e.g. "305.41 x 220.76 x 17.9 mm" or "12.01 x 8.69 x 0.70 in"
    patterns = [
        (r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(mm|millimeters?)\b", "mm"),
        (r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(in(?:ches?)?)\b", "in"),
    ]
    for pat, unit_label in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return f"{m.group(1)} × {m.group(2)} × {m.group(3)} {unit_label}"
    return None


def _extract_battery(text: str) -> Optional[str]:
    """Extract battery capacity from text. Returns Wh or mAh value."""
    if not text:
        return None
    # Wh first (more common on laptops): "56 Wh", "56Wh", "56-Wh"
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-]?\s*(Wh|watt[-\s]?hours?)\b", text, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1)} Wh"
    # mAh (common on phones/tablets): "5000 mAh", "5000mAh"
    m = re.search(r"(\d{3,5}(?:\.\d+)?)\s*(mAh|milliamp(?:ere)?[-\s]?hours?)\b", text, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1)} mAh"
    return None


def _extract_screen_size(text: str) -> Optional[str]:
    """Extract screen diagonal size from text. Returns a value in inches."""
    if not text:
        return None
    patterns = [
        r'(\d+(?:\.\d+)?)["\u201c\u201d]',          # 15.6" or curly-quote variant
        r"(\d+(?:\.\d+)?)\s*[-\s]?(inch(?:es?)?)\b", # 15.6-inch / 15.6 inches
        r"(\d+(?:\.\d+)?)\s*[-\s]?in\b",             # 15.6 in (followed by space/end)
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            # Screen sizes are realistically 5–40 inches; reject anything outside that
            if 5.0 <= val <= 40.0:
                return f"{m.group(1)} inches"
    return None


def _extract_ram(text: str) -> Optional[str]:
    """Extract RAM capacity from text. Returns a value in GB."""
    if not text:
        return None
    patterns = [
        # "16 GB RAM", "16GB DDR5", "16GB LPDDR4X", "32GB unified memory"
        r"(\d+)\s*(GB|GiB)\s*(?:RAM|DDR\d*|LPDDR\d*|unified\s+memory|SDRAM)\b",
        # "16 GB of RAM / memory"
        r"(\d+)\s*(GB|GiB)\s+(?:of\s+)?(?:RAM|memory)\b",
        # "RAM: 16 GB" or "Memory: 16GB"
        r"(?:RAM|memory)\s*:?\s*(\d+)\s*(GB|GiB)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            try:
                val = int(m.group(1))
            except ValueError:
                continue
            # Sanity check: laptop RAM is realistically 2–512 GB
            if 2 <= val <= 512:
                return f"{m.group(1)} GB"
    return None


def _extract_storage(text: str) -> Optional[str]:
    """Extract primary storage capacity from text. Returns a value in GB or TB."""
    if not text:
        return None
    patterns = [
        # TB values: "1 TB SSD", "2TB NVMe", "1 TB HDD"
        r"(\d+(?:\.\d+)?)\s*(TB|TiB)\s*(?:SSD|HDD|NVMe|PCIe|hard\s+drive|storage)?\b",
        # GB values — require a storage-type keyword to avoid matching RAM
        r"(\d+(?:\.\d+)?)\s*(GB|GiB)\s*(?:SSD|HDD|NVMe|PCIe|eMMC|storage)\b",
        # Keyword-first: "SSD: 512 GB", "Storage: 1 TB"
        r"(?:SSD|HDD|NVMe|storage)\s*:?\s*(\d+(?:\.\d+)?)\s*(GB|TB|GiB|TiB)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            num = m.group(1)
            raw_unit = m.group(2).upper()
            # Normalise GiB→GB and TiB→TB
            unit = raw_unit.replace("IB", "B")
            return f"{num} {unit}"
    return None


def _extract_processor(text: str) -> Optional[str]:
    """Extract processor model name from text."""
    if not text:
        return None
    patterns = [
        # Intel Core Ultra / Core i-series: "Intel Core Ultra 5 125H", "Intel Core i7-1355U"
        r"(Intel\s+Core(?:\s+Ultra)?\s+(?:i\d|[A-Z]\d+)[-\s]\w+(?:\s+\w+)?)",
        # AMD Ryzen: "AMD Ryzen 5 7530U", "AMD Ryzen 9 6900HX"
        r"(AMD\s+Ryzen\s+\d+\s+\w+(?:\s+\w+)?)",
        # Apple Silicon: "Apple M3 Pro", "Apple M2"
        r"(Apple\s+M\d+(?:\s+(?:Pro|Max|Ultra))?)",
        # Apple Silicon short form with chip keyword: "M3 Pro chip", "M2 chip"
        r"(M\d+(?:\s+(?:Pro|Max|Ultra))?\s+chip)\b",
        # Qualcomm Snapdragon: "Snapdragon X Elite", "Qualcomm Snapdragon 8cx"
        r"((?:Qualcomm\s+)?Snapdragon\s+\w+(?:\s+\w+)?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _extract_ports(text: str) -> Optional[str]:
    """Extract a list of connectivity ports found in text."""
    if not text:
        return None
    port_patterns = [
        r"Thunderbolt\s*\d+",
        r"USB\s*4",
        r"USB[-\s]?C",
        r"USB[-\s]?A",
        r"USB\s+3\.\d+",
        r"HDMI(?:\s+\d+\.\d+)?",
        r"DisplayPort(?:\s+\d+\.\d+)?",
        r"SD\s*(?:card)?\s*(?:reader)?",
        r"3\.5\s*mm(?:\s+audio(?:\s+jack)?)?",
        r"RJ[-]?45",
        r"Ethernet",
    ]
    found = []
    for pat in port_patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            found.append(m.group(0).strip())
    if not found:
        return None
    # Deduplicate while preserving order
    seen: set = set()
    unique = []
    for p in found:
        key = re.sub(r"\s+", "", p.lower())
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return ", ".join(unique)


def _extract_custom_spec(text: str, unit: str) -> Optional[str]:
    """Generic extractor for tenant-defined custom specs.

    Looks for a number followed by the unit the tenant specified.
    Example: unit="kg" → matches "150 kg", "150kg", "150 Kg".

    If the unit is blank we cannot reliably extract a value, so we return None
    rather than guessing.
    """
    if not text or not unit:
        return None
    unit_clean = unit.strip()
    if not unit_clean:
        return None
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*" + re.escape(unit_clean),
        text,
        flags=re.IGNORECASE,
    )
    if m:
        return f"{m.group(1)} {unit_clean}"
    return None


def _match_custom_spec(query: str, custom_specs: Optional[List[Dict]]) -> Optional[Dict]:
    """Return the first custom spec whose keywords appear in the query, or None.

    Each custom spec dict (stored in features JSON) is expected to have:
      - 'name'     : display label, e.g. "Load Capacity"
      - 'keywords' : comma-separated trigger words, e.g. "load capacity, weight limit"
      - 'unit'     : unit to extract, e.g. "kg"
      - 'qualifier': optional caveat text shown with the answer
    """
    if not custom_specs:
        return None
    q = (query or "").lower()
    for spec in custom_specs:
        raw_kw = (spec.get("keywords") or "").strip()
        if not raw_kw:
            continue
        for kw in [k.strip().lower() for k in raw_kw.split(",") if k.strip()]:
            if re.search(r"\b" + re.escape(kw) + r"\b", q):
                return spec
    return None



_SPEC_EXTRACTOR_MAP: Dict[str, tuple] = {
    "weight":      (_extract_weight,      "May vary by configuration."),
    "dimensions":  (_extract_dimensions,  "May vary by configuration."),
    "battery":     (_extract_battery,     "May vary by model variant."),
    "screen_size": (_extract_screen_size, "Measured diagonally."),
    "ram":         (_extract_ram,         "May vary by configuration."),
    "storage":     (_extract_storage,     "May vary by configuration."),
    "processor":   (_extract_processor,   "May vary by configuration."),
    "ports":       (_extract_ports,       "May vary by configuration. List may not be exhaustive."),
}


def _detect_spec_type(query: str) -> str:
    """Return the most likely spec type key from the customer's query text.

    Checks are ordered from most-specific to least-specific so that a query
    like 'how much RAM does this have?' maps to 'ram' rather than the generic
    fallback.  Returns 'weight' as the default because that was the original
    behaviour before multi-spec support was added.

    Uses standard \\b word boundaries so that short tokens like 'wh' only match
    when they appear as a complete word (not as a prefix inside 'what', etc.).
    Plural and variant forms are listed explicitly where needed.
    """
    q = (query or "").lower()

    def _wm(word: str) -> bool:
        """True if *word* (or phrase) appears as a whole word/phrase in q."""
        return bool(re.search(r"\b" + re.escape(word) + r"\b", q))

    def _any(*words: str) -> bool:
        return any(_wm(w) for w in words)

    if _any("weight", "weighs", "weigh", "heavy", "heaviness", "mass"):
        return "weight"
    if _any("dimension", "dimensions", "height", "width", "depth",
            "thickness", "thick", "thin", "millimeter", "millimeters", "mm"):
        return "dimensions"
    if _any("battery", "wh", "mah", "watt-hour", "wattage",
            "battery life", "charge", "charging"):
        return "battery"
    if _any("screen size", "display size", "screen", "display",
            "diagonal", "inches", "inch"):
        return "screen_size"
    if _any("ram", "memory", "ddr", "lpddr", "unified memory"):
        return "ram"
    if _any("storage", "ssd", "hdd", "hard drive", "disk space", "hard disk"):
        return "storage"
    if _any("cpu", "processor", "intel", "amd", "ryzen", "snapdragon", "chip"):
        return "processor"
    if _any("port", "ports", "usb", "hdmi", "thunderbolt", "displayport",
            "connectivity"):
        return "ports"

    # Default: weight was the original only spec; keeps existing behaviour as fallback
    return "weight"


def lookup_spec_verified(
    query: str,
    extra_trusted_domains: Optional[List[str]] = None,
    custom_specs: Optional[List[Dict]] = None,
) -> Dict:
    """Attempt to find a verified numeric spec for a spec-like query.

    Per-tenant customisation:
    - extra_trusted_domains : additional domains beyond the system defaults
                              (stored in tenant features as 'verified_specs_trusted_domains')
    - custom_specs          : tenant-defined spec types
                              (stored in tenant features as 'verified_specs_custom_specs')

    Custom specs are checked FIRST.  If no custom spec matches, the built-in
    spec types (weight, dimensions, battery, screen size, RAM, storage,
    processor, ports) are tried.

    Returns:
      {
        found: bool,
        spec_key: str,
        spec_value: str,
        qualifier: str,
        sources: [{title, url, snippet}],
      }
    """
    # ── 1. Build the trusted-domain allowlist (system defaults + tenant extras) ──
    allowlist = _build_trusted_domains(extra_trusted_domains)

    # ── 2. Run the web search ────────────────────────────────────────────────────
    results = web_search(query, num=8)
    trusted = [r for r in results if r.get("url") and _domain_ok(r["url"], allowlist)]

    # ── 3. Try tenant custom specs first ────────────────────────────────────────
    matched_custom = _match_custom_spec(query, custom_specs)
    if matched_custom:
        custom_unit = (matched_custom.get("unit") or "").strip()
        custom_name = (matched_custom.get("name") or "custom_spec").strip()
        custom_qualifier = (matched_custom.get("qualifier") or "May vary by configuration.").strip()
        for r in trusted:
            blob = " ".join([(r.get("title") or ""), (r.get("snippet") or "")]).strip()
            value = _extract_custom_spec(blob, custom_unit)
            if value:
                return {
                    "found": True,
                    "spec_key": custom_name,
                    "spec_value": value,
                    "qualifier": custom_qualifier,
                    "sources": trusted[:3],
                }
        # Custom spec matched the query but no value found in trusted sources
        return {
            "found": False,
            "spec_key": "",
            "spec_value": "",
            "qualifier": "",
            "sources": trusted[:3],
        }

    # ── 4. Built-in spec detection ───────────────────────────────────────────────
    spec_type = _detect_spec_type(query)
    extractor_fn, qualifier = _SPEC_EXTRACTOR_MAP.get(
        spec_type, (_extract_weight, "May vary by configuration.")
    )

    for r in trusted:
        blob = " ".join([(r.get("title") or ""), (r.get("snippet") or "")]).strip()
        value = extractor_fn(blob)
        if value:
            return {
                "found": True,
                "spec_key": spec_type,
                "spec_value": value,
                "qualifier": qualifier,
                "sources": trusted[:3],
            }

    return {
        "found": False,
        "spec_key": "",
        "spec_value": "",
        "qualifier": "",
        "sources": trusted[:3],
    }


def make_verified_spec_doc_id(tenant_id: int, model_hint: str, spec_key: str) -> str:
    """Stable doc id so repeated lookups overwrite rather than bloat the index."""
    base = f"{tenant_id}|{(model_hint or '').strip().lower()}|{(spec_key or '').strip().lower()}"
    h = hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]
    return f"spec-{h}"
