"""
Logs into portal.phixtra.com as the demo account and screenshots every
major nav section, saving PNGs into static/portal/tutorial/ for use on
the Help & Tutorials page.

Usage:
    python3 screenshot_tutorial.py [--headed] [--base-url URL]
"""
import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

DEMO_EMAIL = "demo@phixtra.com"
DEMO_PASSWORD = "Demo1234!"
OUT_DIR = Path(__file__).parent / "static" / "portal" / "tutorial"

# (slug, path) — slug becomes the PNG filename and the eventual guide id.
# Routes with a required path param (e.g. catalogue category <id>) are
# left out here; shoot those manually if needed.
ROUTES = [
    ("dashboard", "/dashboard"),
    ("inbox", "/inbox"),
    ("contacts", "/whatsapp/contacts"),
    ("leads", "/leads"),
    ("campaigns", "/whatsapp/campaigns"),
    ("campaigns-reports", "/whatsapp/campaigns/reports"),
    ("orders", "/orders"),
    ("whatsapp-connect", "/whatsapp"),
    ("whatsapp-reports", "/whatsapp/reports"),
    ("products", "/products"),
    ("catalogue", "/catalogue"),
    ("customers", "/customers"),
    ("discount-settings", "/discount-settings"),
    ("data-sources", "/data-sources"),
    ("store-info", "/store-info"),
    ("payment-settings", "/settings/payments"),
    ("ai-instruction", "/system-instruction"),
    ("handoff-rules", "/handoff-rules"),
    ("verified-specs", "/verified-specs-settings"),
    ("billing-subscribe", "/billing/subscribe"),
    ("billing", "/billing"),
    ("invoices", "/invoices"),
    ("settings", "/settings"),
]

# Skip the guided-tour overlay on every page, since a fresh browser
# context has no localStorage and would otherwise trigger it on the
# first dashboard visit and show up in the screenshot.
SKIP_TOUR_INIT_SCRIPT = "localStorage.setItem('phixtra_tour_done', '1');"


def login(page, base_url):
    page.goto(f"{base_url}/login", wait_until="networkidle")
    page.fill("input[name=email]", DEMO_EMAIL)
    page.fill("input[name=password]", DEMO_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base_url}/dashboard", timeout=15000)


def shoot(page, base_url, slug, path):
    out_file = OUT_DIR / f"{slug}.png"
    page.goto(f"{base_url}{path}", wait_until="networkidle", timeout=20000)
    page.wait_for_timeout(500)  # let charts/JS settle
    page.screenshot(path=str(out_file), full_page=True)
    return out_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--base-url", default="https://portal.phixtra.com")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_init_script(SKIP_TOUR_INIT_SCRIPT)
        page = context.new_page()

        try:
            login(page, args.base_url)
        except PWTimeoutError:
            print(f"LOGIN FAILED — could not reach dashboard. Current URL: {page.url}")
            browser.close()
            sys.exit(1)

        for slug, path in ROUTES:
            try:
                out_file = shoot(page, args.base_url, slug, path)
                results.append((slug, path, "ok", out_file.name))
                print(f"  ok    {slug:<22} {path}")
            except Exception as e:
                results.append((slug, path, "FAIL", str(e)))
                print(f"  FAIL  {slug:<22} {path}  -> {e}")

        browser.close()

    failures = [r for r in results if r[2] == "FAIL"]
    print(f"\n{len(results) - len(failures)}/{len(results)} screenshots saved to {OUT_DIR}")
    if failures:
        print("Failed routes:")
        for slug, path, _, err in failures:
            print(f"  - {slug} ({path}): {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
