"""Connect Website (2026-10-03): read a business's own public website so its AI
can answer from it.

Connect only checks the website can be reached (reads nothing):
    /usr/bin/python3 website_reader.py --check example.com     (prints JSON)
Run by the portal when Sync now is clicked:
    /usr/bin/python3 website_reader.py --tenant 123
and hourly by cron for scheduled re-reads (daily / weekly / monthly — paid plans):
    /usr/bin/python3 website_reader.py --due

Each readable page becomes a documents row (type 'page', id site-<tenant>-<hash>)
with its web address, so the AI can answer from it and link to it. Menus,
headers, footers and cookie notices are stripped; product, cart, checkout and
login pages are skipped (products come from the catalogue / WooCommerce sync).
Pages the business removed stay removed. At most MAX_PAGES pages.
Embeddings are added by phixtra-data-sync/re_embed_worker.py, started at the end.
"""
import argparse
import hashlib
import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urldefrag
from urllib import robotparser

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

MAX_PAGES = int(os.getenv("WEBSITE_READER_MAX_PAGES", "100"))
MAX_CANDIDATES = 400            # addresses looked at to find MAX_PAGES readable pages
PAGE_TIMEOUT_MS = 30000
# Websites with a firewall allow PhiXtra by these two (Help › Website & Firewall guide).
READER_NAME = "PhiXtraWebsiteReader"
READER_IP = os.getenv("WEBSITE_READER_PUBLIC_IP", "185.180.220.227")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 PhiXtraWebsiteReader/1.0 (+https://phixtra.com)")
ROBOTS_UA = "PhiXtraWebsiteReader"
FREQ_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}   # 'manual' = only when Sync now is clicked
STALE_READING = timedelta(minutes=45)   # a run that died: let the next one start

SKIP_PATH = re.compile(
    r"/(cart|basket|checkout|my-account|account|login|log-in|signin|sign-in|register|signup|sign-up|"
    r"wp-admin|wp-login\.php|admin|wp-json|feed|xmlrpc\.php|search|tag|author|category|page/\d+|"
    r"product|products|product-page|product-category|item|items|shop/p|collections/[^/]+/products)(/|$)", re.I)
SKIP_QUERY = re.compile(r"(add-to-cart|add_to_cart|replytocom|wc-ajax|orderby=|filter_|min_price|max_price|(^|&)s=|share=|nb=|utm_|fbclid|gclid|amp=|(^|&)p=\d)", re.I)
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|ico|zip|rar|mp4|mp3|mov|avi|docx?|xlsx?|pptx?|csv|xml|json|js|css|woff2?|ttf)$", re.I)

# Runs in the page: remove what repeats on every page, return the main text.
EXTRACT_JS = r"""
() => {
  // NB: never remove [aria-hidden=true] — a popup marks the whole page behind
  // it hidden (phixtra.com, 2026-10-03), which emptied the page.
  const kill = 'script,style,noscript,svg,iframe,template,nav,header,footer,aside,form,' +
               '[role=navigation],[role=banner],[role=contentinfo],[role=dialog],' +
               '.menu,.nav,.navbar,.site-header,.site-footer,.breadcrumb,.breadcrumbs,.sidebar,.widget-area,' +
               '.elementor-location-header,.elementor-location-footer,#header,#footer,#masthead,#colophon,' +
               '[id^="phixtra-welcome"],[id^="phixtra-chat"],[class*="phixtra-widget"],[class*="phixtra-chat"],[class*="phixtra-msg"],' +
               // PhiXtra AI plugin chat bubble (ids phixaish-…) + breadcrumb trails ("Home / Digital /"), 2026-10-03
               '[id^="phixaish"],[class*="phixaish"],[class*="breadcrumb"],.woocommerce-breadcrumb,[aria-label="breadcrumb" i],[aria-label="breadcrumbs" i]';
  const doc = document.cloneNode(true);
  doc.querySelectorAll(kill).forEach(e => e.remove());
  doc.querySelectorAll('[id],[class]').forEach(e => {
    const k = ((e.id || '') + ' ' + (typeof e.className === 'string' ? e.className : '')).toLowerCase();
    if (/(cookie|consent|gdpr|newsletter-popup|chat-widget)/.test(k)) e.remove();
  });
  // The main content is the candidate box holding the most text (the first
  // <article> can be a "related posts" card); none big enough → whole page.
  const bodyLen = (doc.body.innerText || doc.body.textContent || '').length;
  let main = null, best = 0;
  doc.querySelectorAll('main, [role=main], article, #content, .site-content, #main, .entry-content, .post-content, .page-content, #page-wrap').forEach(e => {
    const n = (e.innerText || e.textContent || '').length;
    if (n > best) { best = n; main = e; }
  });
  if (!main || best < bodyLen * 0.35) main = doc.body;
  let product = false;
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    if (/"@type"\s*:\s*"Product"/.test(s.textContent || '')) product = true;
  });
  const og = document.querySelector('meta[property="og:type"]');
  if (og && /product/i.test(og.content || '')) product = true;
  const links = Array.from(document.querySelectorAll('a[href]')).map(a => a.href);
  return {
    title: (document.title || '').trim(),
    h1: ((document.querySelector('h1') || {}).innerText || '').trim(),
    text: main ? (main.innerText || main.textContent || '') : '',
    login: !!document.querySelector('input[type=password]'),
    product: product,
    links: links,
  };
}
"""


def db():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"), port=os.getenv("PG_PORT", "5432"),
        user=os.getenv("PG_USER"), password=os.getenv("PG_PASSWORD"), dbname=os.getenv("PG_DB", "ai_support"),
    )


def now():
    return datetime.now(timezone.utc)


# ── Safety: public websites only ────────────────────────────────────────────
def host_is_public(host: str) -> bool:
    """Refuse localhost, private and internal addresses, so the reader can
    never be pointed at PhiXtra's own servers or a private network."""
    if not host or host.endswith((".local", ".internal", ".localhost")) or host == "localhost":
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def normalise_start(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path or '/'}"


def same_site(host_a: str, host_b: str) -> bool:
    strip = lambda h: (h or "").lower().removeprefix("www.")
    return strip(host_a) == strip(host_b)


def clean_url(u: str):
    u, _ = urldefrag(u)
    p = urlparse(u)
    if p.scheme not in ("http", "https"):
        return None
    path = re.sub(r"/{2,}", "/", p.path or "/")
    q = "" if not p.query else "?" + p.query
    return f"{p.scheme}://{p.netloc.lower()}{path}{q}"


def skip_reason_for_url(u: str):
    p = urlparse(u)
    if SKIP_EXT.search(p.path):
        return "file, not a page"
    if SKIP_PATH.search(p.path):
        return "product, cart or account page"
    if p.query and SKIP_QUERY.search(p.query):
        return "shop filter or cart link"
    return None


# ── Finding pages ───────────────────────────────────────────────────────────
def sitemap_urls(base: str, host: str, robots_txt: str) -> list:
    """Page addresses from the site's sitemap(s), if it has any."""
    found, queue, seen = [], [], set()
    for line in robots_txt.splitlines():
        if line.lower().startswith("sitemap:"):
            queue.append(line.split(":", 1)[1].strip())
    queue += [urljoin(base, p) for p in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/page-sitemap.xml")]
    while queue and len(seen) < 25 and len(found) < MAX_CANDIDATES:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            r = requests.get(sm, headers={"User-Agent": UA}, timeout=15)
            if r.status_code != 200 or b"<" not in r.content[:200]:
                continue
            root = ET.fromstring(r.content)
        except Exception:
            continue
        for loc in root.iter():
            if not loc.tag.endswith("loc") or not (loc.text or "").strip():
                continue
            u = loc.text.strip()
            if root.tag.endswith("sitemapindex"):
                # product / category sitemaps hold no information pages
                if not re.search(r"(product|category|tag|author|collection)", u, re.I):
                    queue.append(u)
            else:
                cu = clean_url(u)
                if cu and same_site(urlparse(cu).hostname, host):
                    found.append(cu)
    return list(dict.fromkeys(found))


def read_site(tenant_id: int, start_url: str, removed: set, progress) -> tuple:
    """Returns (pages, skipped): pages = [{url,title,text}], skipped = {url: reason}."""
    from playwright.sync_api import sync_playwright

    start = normalise_start(start_url)
    host = urlparse(start).hostname
    if not host_is_public(host):
        raise ValueError("That address is not a public website we can read.")

    robots = robotparser.RobotFileParser()
    robots_txt = ""
    try:
        r = requests.get(urljoin(start, "/robots.txt"), headers={"User-Agent": UA}, timeout=15)
        if r.status_code == 200:
            robots_txt = r.text
        robots.parse(robots_txt.splitlines())
    except Exception:
        robots.parse([])

    candidates = [start] + [u for u in sitemap_urls(start, host, robots_txt) if u != start]
    queued = set(candidates)
    pages, skipped = [], {}
    looked = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(user_agent=UA, ignore_https_errors=False, locale="en-GB",
                                  viewport={"width": 1280, "height": 900})
        ctx.route("**/*", lambda route: route.abort()
                  if route.request.resource_type in ("image", "media", "font") else route.continue_())
        page = ctx.new_page()
        i = 0
        while i < len(candidates) and len(pages) < MAX_PAGES and looked < MAX_CANDIDATES:
            url = candidates[i]
            i += 1
            if url in removed:
                continue
            reason = skip_reason_for_url(url)
            if reason:
                skipped[url] = reason
                continue
            if robots_txt and not robots.can_fetch(ROBOTS_UA, url):
                skipped[url] = "the website asks readers not to read it"
                continue
            if not host_is_public(urlparse(url).hostname):
                continue
            looked += 1
            try:
                resp = page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                final = clean_url(page.url) or url
                if not same_site(urlparse(final).hostname, host):
                    skipped[url] = "goes to a different website"
                    continue
                if resp is not None and resp.status >= 400:
                    skipped[url] = f"page shows an error ({resp.status})"
                    continue
                data = page.evaluate(EXTRACT_JS)
            except Exception as e:
                msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                skipped[url] = f"page would not open ({msg[:160]})"
                print(f"   page failed {url}: {type(e).__name__}", flush=True)
                continue
            if final != url and final in {p["url"] for p in pages}:
                continue
            if data.get("login"):
                skipped[final] = "needs a login"
                continue
            if data.get("product"):
                skipped[final] = "product page (products come from your catalogue)"
                continue
            text = re.sub(r"[ \t]+", " ", data.get("text") or "")
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
            if len(text) < 80:
                skipped[final] = "almost no text on the page"
                continue
            pages.append({"url": final, "title": (data.get("title") or data.get("h1") or final)[:250], "text": text})
            progress(len(pages))
            # follow links that stay on this website
            for link in data.get("links") or []:
                cu = clean_url(link)
                if cu and cu not in queued and same_site(urlparse(cu).hostname, host) and len(candidates) < MAX_CANDIDATES:
                    queued.add(cu)
                    candidates.append(cu)
            time.sleep(0.4)   # be gentle with the business's website
        browser.close()

    # Most sites end every title with the site name ("Contact Us | PhiXtra"):
    # drop that shared ending so titles say what the page is about.
    if len(pages) >= 3:
        sep = re.compile(r"\s+[|–—\-:·•]\s+")
        ends = Counter()
        for p in pages:
            parts = sep.split(p["title"])
            if len(parts) > 1:
                ends[parts[-1].strip().lower()] += 1
        for end, n in ends.items():
            if n >= max(2, int(len(pages) * 0.5)):
                for p in pages:
                    parts = sep.split(p["title"])
                    while len(parts) > 1 and parts[-1].strip().lower() == end:
                        parts = parts[:-1]
                    p["title"] = " | ".join(x.strip() for x in parts if x.strip()) or p["title"]

    # Lines that appear on most pages are leftovers of the menu/footer: drop them.
    if len(pages) >= 4:
        counts = Counter()
        for p in pages:
            counts.update({ln.strip() for ln in p["text"].splitlines() if ln.strip()})
        common = {ln for ln, n in counts.items() if n >= max(3, int(len(pages) * 0.6)) and len(ln) < 200}
        for p in pages:
            p["text"] = "\n".join(ln for ln in p["text"].splitlines() if ln.strip() not in common).strip()
    return pages, skipped


# ── Connection check (Connect Website, 2026-10-03) ─────────────────────────
# Connect only checks that we can reach the website — it reads nothing. The
# business then starts reading from Sync Website. Every failure gives the
# plain reason plus the real technical error, for troubleshooting.
CHECK_TIMEOUT_MS = 20000

_NET_ERRORS = [   # Chrome error code fragment → plain reason
    ("ERR_NAME_NOT_RESOLVED", "This website address doesn't exist. No DNS record was found for it — check the spelling."),
    ("ERR_CERT", "The website's security certificate (HTTPS) isn't valid, so browsers would warn visitors too. Ask whoever looks after your website to renew or fix it."),
    ("ERR_SSL", "The website's secure connection (HTTPS) failed. Ask whoever looks after your website to check its SSL certificate."),
    ("ERR_CONNECTION_REFUSED", "The website's server refused the connection. The site may be down, or the server isn't accepting web visits."),
    ("ERR_CONNECTION_TIMED_OUT", "The website didn't answer within 20 seconds. It may be down or very slow — try again later."),
    ("ERR_TIMED_OUT", "The website didn't answer within 20 seconds. It may be down or very slow — try again later."),
    ("ERR_CONNECTION_RESET", "The website's server or firewall cut the connection off."),
    ("ERR_CONNECTION_CLOSED", "The website's server or firewall cut the connection off."),
    ("ERR_EMPTY_RESPONSE", "The website's server sent back nothing."),
    ("ERR_TOO_MANY_REDIRECTS", "The website keeps redirecting in a loop, so no page ever opens. Ask whoever looks after your website to check its redirect settings."),
    ("ERR_ADDRESS_UNREACHABLE", "The website's server can't be reached."),
    ("ERR_INTERNET_DISCONNECTED", "We couldn't reach the internet to check your website. Please try again in a minute."),
]

_HTTP_REASONS = {
    401: "The website asks for a username and password, so we can't read it.",
    403: "The website refused our visit (403 Forbidden). A firewall or security plugin on the website is blocking us.",
    404: "That page doesn't exist on the website (404 Not Found). Check the address.",
    410: "That page has been removed from the website (410 Gone). Check the address.",
    429: "The website says we've visited too often (429 Too Many Requests). Try again later.",
}


# Failures a firewall / bot blocker on the website usually causes — the
# Connect page then shows our address + reader name to allow, and the guide.
_FIREWALL_NET = ("ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_EMPTY_RESPONSE", "ERR_CONNECTION_REFUSED")


def _fail(res: dict, reason: str, detail: str, kind: str = "other") -> dict:
    """kind: firewall | cloudflare | robots | other."""
    res.update(ok=False, reason=reason, detail=(detail or "").strip()[:500], kind=kind)
    return res


def check_connection(raw_url: str) -> dict:
    """Can we reach this website? Never raises. Returns
    {ok, url, final_url, title, pages_found, http_status, reason, detail}."""
    start = normalise_start(raw_url)
    host = urlparse(start).hostname or ""
    res = {"ok": False, "url": start, "final_url": "", "title": "", "pages_found": 0,
           "http_status": None, "reason": "", "detail": "", "kind": ""}
    if "." not in host:
        return _fail(res, "That doesn't look like a website address. Example: example.com", f"No domain in '{raw_url}'")

    # 1. Does the name exist, and is it a public address?
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return _fail(res, "This website address doesn't exist. No DNS record was found for it — check the spelling.",
                     f"DNS lookup for {host} failed: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return _fail(res, "This address points to a private network, not a public website.",
                         f"{host} resolves to {ip}")

    # 2. Open the home page the same way the reader will.
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                ctx = browser.new_context(user_agent=UA, ignore_https_errors=False, locale="en-GB",
                                          viewport={"width": 1280, "height": 900})
                ctx.route("**/*", lambda route: route.abort()
                          if route.request.resource_type in ("image", "media", "font") else route.continue_())
                page = ctx.new_page()
                try:
                    resp = page.goto(start, timeout=CHECK_TIMEOUT_MS, wait_until="domcontentloaded")
                except Exception as e:
                    msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                    if "Timeout" in type(e).__name__ or "Timeout" in msg:
                        return _fail(res, "The website didn't answer within 20 seconds. It may be down or very slow — try again later.", msg)
                    for code, reason in _NET_ERRORS:
                        if code in msg:
                            return _fail(res, reason, msg,
                                         "firewall" if any(c in msg for c in _FIREWALL_NET) else "other")
                    return _fail(res, "We couldn't open the website.", msg)
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                status = resp.status if resp is not None else None
                headers = {k.lower(): v for k, v in (resp.headers.items() if resp is not None else [])}
                server = headers.get("server", "")
                res["http_status"] = status
                res["final_url"] = page.url
                title = (page.title() or "").strip()
                res["title"] = title[:200]
                tech = f"HTTP {status} from {page.url}" + (f" · server: {server}" if server else "")

                cf_challenge = headers.get("cf-mitigated") == "challenge" or \
                    title.lower().startswith(("just a moment", "attention required"))
                if cf_challenge:
                    return _fail(res, "Cloudflare's bot protection on your website stopped us. Ask whoever looks after "
                                      "your website to allow PhiXtra in Cloudflare.", tech, "cloudflare")
                if status and status >= 400:
                    reason = _HTTP_REASONS.get(status) or (
                        f"The website's server had an error ({status}). It may be broken or down — try again later."
                        if status >= 500 else f"The website answered with an error ({status}).")
                    kind = "cloudflare" if status in (403, 429, 503) and "cloudflare" in server.lower() else \
                        "firewall" if status in (403, 406, 429) else "other"
                    if kind == "cloudflare":
                        reason = (f"Cloudflare's protection on your website refused our visit (error {status}). "
                                  "Ask whoever looks after your website to allow PhiXtra in Cloudflare.")
                    return _fail(res, reason, tech, kind)
                final_host = urlparse(page.url).hostname or ""
                if not same_site(final_host, host):
                    return _fail(res, f"This address sends visitors to a different website ({final_host}). "
                                      f"Connect {final_host} instead.", f"Redirected from {start} to {page.url}")
                data = page.evaluate(EXTRACT_JS)
                text = (data.get("text") or "").strip()
                if data.get("login") and len(text) < 200:
                    return _fail(res, "Your home page asks for a login or password, so we can't read it.",
                                 f"Password field found on {page.url}")
                links = data.get("links") or []
            finally:
                browser.close()
    except Exception as e:
        return _fail(res, "We couldn't start the website check. Please try again in a minute.",
                     f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")

    # 3. robots.txt: does the website ask readers to keep out?
    robots_txt = ""
    try:
        r = requests.get(urljoin(start, "/robots.txt"), headers={"User-Agent": UA}, timeout=15)
        if r.status_code == 200:
            robots_txt = r.text
    except Exception:
        pass
    if robots_txt:
        rp = robotparser.RobotFileParser()
        rp.parse(robots_txt.splitlines())
        if not rp.can_fetch(ROBOTS_UA, res["final_url"] or start):
            return _fail(res, "Your website's robots.txt file asks readers not to read it. Remove the 'Disallow: /' "
                              "rule (or allow PhiXtraWebsiteReader) and try again.",
                         f"robots.txt at {urljoin(start, '/robots.txt')} disallows {urlparse(res['final_url'] or start).path or '/'}",
                         "robots")

    # 4. Roughly how many readable pages are there?
    found = set()
    for u in sitemap_urls(start, host, robots_txt) + [clean_url(l) for l in links]:
        if u and same_site(urlparse(u).hostname, host) and not skip_reason_for_url(u):
            found.add(u.rstrip("/"))
    res["pages_found"] = len(found)
    res["ok"] = True
    return res


# ── Saving ──────────────────────────────────────────────────────────────────
def doc_id_for(tenant_id: int, url: str) -> str:
    return f"site-{tenant_id}-{hashlib.md5(url.encode()).hexdigest()[:16]}"


def schedule_allowed(tenant_id: int) -> bool:
    """Automatic re-reading (daily/weekly/monthly) is a plan feature
    (Plan editor key store.website_schedule — paid plans). Same plan lookup
    as the portal (_get_tenant_plan: no plan → plan 1)."""
    conn = db()
    cur = conn.cursor()
    cur.execute("""SELECT 1 FROM tenants t JOIN plan_feature_grants g
                   ON g.plan_id = COALESCE(t.plan_id, 1) AND g.feature_key = ANY(%s)
                   WHERE t.id = %s""", (["store.website_schedule", "store.website_connect"], tenant_id))
    ok = len(cur.fetchall()) == 2   # both: re-reading AND Connect Website still in the plan
    conn.close()
    return ok


def _next_days(tenant_id: int, frequency: str):
    """Days until the next automatic read, or None (no automatic read)."""
    if frequency not in FREQ_DAYS or not schedule_allowed(tenant_id):
        return None
    return str(FREQ_DAYS[frequency])


def run_tenant(tenant_id: int) -> None:
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM website_sources WHERE tenant_id=%s", (tenant_id,))
    src = cur.fetchone()
    if not src:
        print(f"tenant {tenant_id}: no website connected")
        return
    if src["status"] == "reading" and src["started_at"] and now() - src["started_at"] < STALE_READING:
        print(f"tenant {tenant_id}: already reading")
        return
    cur.execute("UPDATE website_sources SET status='reading', status_detail=NULL, pages_read=0, started_at=NOW() WHERE tenant_id=%s",
                (tenant_id,))
    conn.commit()
    cur.execute("SELECT url FROM website_pages WHERE tenant_id=%s AND status='removed'", (tenant_id,))
    removed = {r["url"] for r in cur.fetchall()}

    def progress(n):
        c2 = conn.cursor()
        c2.execute("UPDATE website_sources SET pages_read=%s WHERE tenant_id=%s", (n, tenant_id))
        conn.commit()
        c2.close()

    print(f"tenant {tenant_id}: reading {src['url']}", flush=True)
    try:
        pages, skipped = read_site(tenant_id, src["url"], removed, progress)
    except Exception as e:
        msg = str(e) if isinstance(e, ValueError) else \
            f"We could not open your website. Technical detail: {type(e).__name__}: {(str(e).splitlines() or [''])[0][:300]}"
        print(f"tenant {tenant_id}: failed: {e}", flush=True)
        cur.execute("""UPDATE website_sources SET status='error', status_detail=%s,
                       next_read_at=NOW() + (%s || ' days')::interval WHERE tenant_id=%s""",
                    (msg, _next_days(tenant_id, src["frequency"]), tenant_id))
        conn.commit()
        conn.close()
        return

    site = urlparse(normalise_start(src["url"]))
    site_url = f"{site.scheme}://{site.netloc}"
    keep_ids = set()
    for p in pages:
        did = doc_id_for(tenant_id, p["url"])
        keep_ids.add(did)
        cur.execute("""
            INSERT INTO documents (id, tenant_id, type, title, content, url, site_url, updated_by, updated_at, embedding)
            VALUES (%s, %s, 'page', %s, %s, %s, %s, 'website_reader', NOW(), NULL)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title, url = EXCLUDED.url, site_url = EXCLUDED.site_url,
                updated_by = EXCLUDED.updated_by, updated_at = NOW(),
                embedding = CASE WHEN documents.content IS DISTINCT FROM EXCLUDED.content
                                   OR documents.title IS DISTINCT FROM EXCLUDED.title THEN NULL ELSE documents.embedding END,
                content = EXCLUDED.content
        """, (did, tenant_id, p["title"], p["text"], p["url"], site_url))
        cur.execute("""
            INSERT INTO website_pages (tenant_id, url, title, doc_id, status, skip_reason, last_read_at)
            VALUES (%s, %s, %s, %s, 'read', NULL, NOW())
            ON CONFLICT (tenant_id, url) DO UPDATE SET title=EXCLUDED.title, doc_id=EXCLUDED.doc_id,
                status='read', skip_reason=NULL, last_read_at=NOW()
        """, (tenant_id, p["url"], p["title"], did))
    # Skipped pages worth showing (not plain files/filters), at most 50.
    shown = [(u, r) for u, r in skipped.items() if r not in ("file, not a page", "shop filter or cart link")][:50]
    cur.execute("DELETE FROM website_pages WHERE tenant_id=%s AND status='skipped'", (tenant_id,))
    for u, r in shown:
        cur.execute("""
            INSERT INTO website_pages (tenant_id, url, title, status, skip_reason, last_read_at)
            VALUES (%s, %s, NULL, 'skipped', %s, NOW())
            ON CONFLICT (tenant_id, url) DO NOTHING
        """, (tenant_id, u, r))
    # Pages no longer on the website (or now skipped): the AI stops using them.
    cur.execute("SELECT id FROM documents WHERE tenant_id=%s AND id LIKE %s", (tenant_id, f"site-{tenant_id}-%"))
    gone = [r["id"] for r in cur.fetchall() if r["id"] not in keep_ids]
    if gone:
        cur.execute("DELETE FROM documents WHERE tenant_id=%s AND id = ANY(%s)", (tenant_id, gone))
        cur.execute("DELETE FROM website_pages WHERE tenant_id=%s AND status='read' AND doc_id = ANY(%s)", (tenant_id, gone))
    start_reason = skipped.get(normalise_start(src["url"]))
    detail = None if pages else "We could not find any readable pages on your website." + (
        f" Your home page: {start_reason}." if start_reason else "")
    cur.execute("""UPDATE website_sources SET status=%s, status_detail=%s, pages_read=%s, last_read_at=NOW(),
                   next_read_at=NOW() + (%s || ' days')::interval WHERE tenant_id=%s""",
                ("done" if pages else "error", detail, len(pages), _next_days(tenant_id, src["frequency"]), tenant_id))
    conn.commit()
    conn.close()
    print(f"tenant {tenant_id}: {len(pages)} pages read, {len(skipped)} skipped, {len(gone)} removed", flush=True)
    # Index the new/changed pages for the AI now (re_embed_worker handles rows with no embedding).
    subprocess.run("cd /root/phixtra-app/phixtra-data-sync && ./venv/bin/python re_embed_worker.py >> /var/log/re_embed_worker.log 2>&1",
                   shell=True, timeout=900)


def run_due() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("""SELECT tenant_id FROM website_sources
                   WHERE next_read_at IS NOT NULL AND next_read_at <= NOW()
                     AND (status <> 'reading' OR started_at < NOW() - INTERVAL '45 minutes')
                   ORDER BY next_read_at LIMIT 20""")
    due = [r[0] for r in cur.fetchall()]
    conn.close()
    for t in due:
        if not schedule_allowed(t):     # plan changed: back to Sync now only
            c = db(); cc = c.cursor()
            cc.execute("UPDATE website_sources SET next_read_at=NULL WHERE tenant_id=%s", (t,))
            c.commit(); c.close()
            print(f"tenant {t}: automatic re-reading not in plan — skipped", flush=True)
            continue
        run_tenant(t)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int)
    ap.add_argument("--due", action="store_true")
    ap.add_argument("--check", metavar="URL", help="connection check only; prints JSON")
    a = ap.parse_args()
    if a.check:
        import json
        print(json.dumps(check_connection(a.check)))
    elif a.tenant:
        run_tenant(a.tenant)
    elif a.due:
        run_due()
    else:
        ap.print_help()
        sys.exit(1)
