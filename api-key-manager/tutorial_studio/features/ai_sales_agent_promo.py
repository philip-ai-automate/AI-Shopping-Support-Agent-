"""
Sales-focused narrated cut of the real WhatsApp AI Sales Agent demo clip —
built for the "PhiXtra AI Sales Agent" WhatsApp campaign's "Watch Demo"
button (see project_wa_ai_sales_agent_campaign memory). Unlike every other
tutorial_studio feature, the middle six beats are NOT Playwright-recorded —
they're fixed-timestamp cuts from a real, pre-recorded 60s screen capture
of a genuine WhatsApp exchange on the live ProfitBuyz number
(/home/profitbuyz.com/PHIXTRA AI SALES AGENT.mp4). Only the hook and CTA
title cards are Playwright-recorded, same self-animating-HTML-card
technique as sales_crm_campaigns_promo.py.

Approved 2026-07-27: Okafor voice (Nigerian-accented), narration replaces
original phone-recording audio entirely, this becomes the asset uploaded
to YouTube for the campaign's Watch Demo button (replacing the raw clip).

Real footage timestamps were chosen by reviewing extracted frames of the
actual clip, not guessed — each beat boundary lands on a real conversation
transition (product not in stock -> alternatives shown -> full spec card
-> discount negotiated -> checkout details -> payment/order summary).
"""
from pathlib import Path

VOICE_ID = "483f791c833e425a8f561bd42719712d"  # "Okafor" — Nigerian-accented male

ASSETS_DIR = Path(__file__).parent / "assets"
HOOK_CARD = f"file://{ASSETS_DIR / 'ai_sales_hook.html'}"
CTA_CARD = f"file://{ASSETS_DIR / 'ai_sales_cta.html'}"

# The real, pre-recorded footage this video is built around — not touched,
# only read from.
FOOTAGE_PATH = Path("/home/profitbuyz.com/PHIXTRA AI SALES AGENT.mp4")

# beat_id -> (start_seconds, end_seconds) into FOOTAGE_PATH. lib.py's
# build_video() cuts these directly instead of deriving boundaries from a
# Playwright recording, for any beat_id listed here.
FOOTAGE_MAP = {
    "beat_no_stock": (0.0, 10.0),
    "beat_browse":   (10.0, 20.0),
    "beat_specs":    (20.0, 30.0),
    "beat_discount": (30.0, 40.0),
    "beat_checkout": (40.0, 50.0),
    "beat_payment":  (50.0, 60.0),
}

BEATS = {
    # ── Hook: Playwright-recorded title card (real Nigeria pain points) ──
    "hook_1":     "A customer messages you on WhatsApp right now.",
    "hook_2":     "But you're busy. Your battery's low. Or you're fast asleep.",
    "hook_3":     "By the time you reply, they've already bought from someone else.",
    "hook_turn":  "Unless your AI Sales Agent replies for you. Watch this.",

    # ── Real footage: cut directly from FOOTAGE_PATH via FOOTAGE_MAP ──
    "beat_no_stock": (
        "A real customer asks for a phone Fixtra doesn't have in stock. No "
        "dead air, no silence — it says so straight away, and asks what "
        "else it can help find."
    ),
    "beat_browse": (
        "The customer names another phone instead — and Fixtra instantly "
        "pulls up real matches, real stock, real prices. No hold music. No "
        "\"let me check and get back to you.\""
    ),
    "beat_specs": (
        "Full specs, real price, even why the phone's worth buying — "
        "pulled straight from your catalogue, automatically."
    ),
    "beat_discount": (
        "The customer asks for a discount. Fixtra negotiates it — five "
        "percent off — and closes, without waking you up."
    ),
    "beat_checkout": (
        "It takes the order itself — name, pickup or delivery — like a "
        "real sales rep would."
    ),
    "beat_payment": (
        "Then hands over your real payment details and waits for the "
        "receipt. One sale, closed, start to finish — zero typing from you."
    ),

    # ── CTA: Playwright-recorded title card ──
    "cta": (
        "This is Fixtra's AI Sales Agent — WhatsApp that sells while you "
        "sleep. Get started free at portal dot phixtra dot com, forward "
        "slash register."
    ),
}


def login(browser, video_dir):
    ctx = browser.new_context(
        viewport={"width": 1440, "height": 900},
        record_video_dir=str(video_dir),
        record_video_size={"width": 1440, "height": 900},
    )
    page = ctx.new_page()
    return page, ctx


def record(page, hold, mark, beat_ms):
    # --- hook: sequential-reveal pain-point card ---
    page.goto(HOOK_CARD)
    page.evaluate("showPoint(1)")
    hold("hook_1")
    page.evaluate("showPoint(2)")
    hold("hook_2")
    page.evaluate("showPoint(3)")
    hold("hook_3")
    page.evaluate("showTurn()")
    hold("hook_turn")

    # beat_no_stock..beat_payment are NOT recorded here — build_video()
    # pulls them directly from FOOTAGE_PATH via FOOTAGE_MAP.

    # --- cta: self-animating closing card ---
    page.goto(CTA_CARD)
    hold("cta")
