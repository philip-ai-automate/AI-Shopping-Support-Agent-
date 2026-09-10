"""
Meta App Review demo #1 — "message creation and delivery via the WhatsApp
client". Records the portal.phixtra.com (merchant dashboard) side only,
against the REAL tenant 17 (sales@profitbuyz.com / ProfitBuyz), whose
WhatsApp Business number (+44 7778 391737) is a genuine, live Meta Cloud
API connection — not the seeded demo@phixtra.com account, which has fake
placeholder WABA/phone IDs and can't send or receive anything real.

This is NOT a tutorial_studio "feature" (no BEATS/narration) — App Review
wants a plain, honest capture of real actions, not a produced voiceover.
It reuses tutorial_studio.lib's visible-cursor helpers for clarity only.

Requires a LIVE human to actually be sending a WhatsApp message from a real
phone (07503094646) to +44 7778 391737 while this script is running — it
polls the Inbox for that message to arrive rather than driving both sides
itself (Playwright can't operate a physical phone). Run this, THEN send the
WhatsApp message within the poll window below.

The account password is never hardcoded here (this file lives in a
git-tracked folder) — pass it as an env var each run:

    PORTAL_LOGIN_PASSWORD='...' python3 -m tutorial_studio.app_review.record_inbox_demo

Output: tutorial_studio/app_review/_workspace/inbox_demo/raw_video/*.webm
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/root/phixtra-app/api-key-manager")

import psycopg2.extras
from db import get_db_connection
from playwright.sync_api import sync_playwright

from tutorial_studio.lib import CURSOR_INIT_SCRIPT, HIGHLIGHT_INIT_SCRIPT, click_visibly, move_to, highlight

BASE_URL = "https://portal.phixtra.com"
LOGIN_EMAIL = "sales@profitbuyz.com"
TENANT_ID = 17
TEST_CUSTOMER_PHONE_SUFFIX = "7503094646"  # last digits of 07503094646, country-code-agnostic
REPLY_TEXT = "Thanks for reaching out — one of our team will be right with you!"
POLL_TIMEOUT_S = 240   # how long to wait for the live WhatsApp message to arrive
POLL_INTERVAL_S = 5


def _find_new_inbound_message(since):
    """Query the DB directly for an inbound message from the test phone that
    arrived AFTER `since` — this phone already has message history from
    earlier testing, so checking the Inbox DOM for "any conversation exists"
    (the first version of this script) matches that stale history instantly
    instead of waiting for a genuinely new, live message."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT id, created_at FROM wa_message_log
        WHERE tenant_id = %s
          AND customer_phone LIKE %s
          AND direction = 'inbound'
          AND created_at > %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (TENANT_ID, f"%{TEST_CUSTOMER_PHONE_SUFFIX}%", since),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

OUT_DIR = Path(__file__).parent / "_workspace" / "inbox_demo" / "raw_video"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def run():
    password = os.environ.get("PORTAL_LOGIN_PASSWORD")
    if not password:
        raise SystemExit("Set PORTAL_LOGIN_PASSWORD in the environment before running this script.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(OUT_DIR),
            record_video_size={"width": 1440, "height": 900},
        )
        ctx.add_init_script(CURSOR_INIT_SCRIPT)
        ctx.add_init_script(HIGHLIGHT_INIT_SCRIPT)
        page = ctx.new_page()

        # --- log in as the real merchant ---
        page.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=20000)
        page.fill("input[name=email]", LOGIN_EMAIL)
        page.fill("input[name=password]", password)
        click_visibly(page, "button[type=submit]")
        page.wait_for_load_state("networkidle")

        # --- go to the Inbox and show what's already there ---
        page.goto(f"{BASE_URL}/inbox", wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(2000)

        # DB's own clock, not this machine's — avoids any clock-skew edge case
        _conn = get_db_connection()
        _cur = _conn.cursor()
        _cur.execute("SELECT NOW()")
        start_time = _cur.fetchone()[0]
        _cur.close(); _conn.close()

        print(f">>> Ready. Send a WhatsApp message from {TEST_CUSTOMER_PHONE_SUFFIX} "
              f"to +44 7778 391737 now — waiting up to {POLL_TIMEOUT_S}s for a NEW message "
              f"(after {start_time})...")

        found = False
        waited = 0
        while waited < POLL_TIMEOUT_S:
            if _find_new_inbound_message(start_time):
                found = True
                break
            page.wait_for_timeout(POLL_INTERVAL_S * 1000)
            waited += POLL_INTERVAL_S

        if found:
            # Give the AI a moment to post its own reply before the dashboard
            # reloads, so the transcript shown includes both sides.
            page.wait_for_timeout(4000)
            page.reload(wait_until="networkidle")

        if not found:
            print("!!! Timed out waiting for the WhatsApp message to arrive. "
                  "No conversation found — check the number was messaged correctly.")
            ctx.close()
            video_path = page.video.path() if page.video else None
            browser.close()
            print("video (incomplete):", video_path)
            return

        # --- open the conversation, show the AI's reply already in the thread ---
        conv_selector = f'.inbox-item[data-phone*="{TEST_CUSTOMER_PHONE_SUFFIX}"]'
        click_visibly(page, conv_selector)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        move_to(page, "#chatMessages")
        highlight(page, "#chatMessages", ms=2500)
        page.wait_for_timeout(2500)

        # --- claim, only if this tenant has an active team (button only renders then) ---
        claim_btn = page.locator("button:has-text('Claim & reply')")
        if claim_btn.count() > 0:
            click_visibly(page, "button:has-text('Claim & reply')")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)

        # --- type and send a real reply from the dashboard ---
        click_visibly(page, "#replyBox")
        page.locator("#replyBox").fill(REPLY_TEXT)
        page.wait_for_timeout(800)
        click_visibly(page, ".send-btn")
        page.wait_for_timeout(3000)

        ctx.close()
        video_path = page.video.path() if page.video else None
        browser.close()
        print("video:", video_path)


if __name__ == "__main__":
    run()
