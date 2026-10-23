"""KeyLand: turn a buyer's WhatsApp voice note into text (2026-10-22).

The WhatsApp gateway downloads the voice note and posts it to /estate-transcribe;
this sends it to OpenAI speech-to-text with a hint of the words this agency's
buyers use (estate names, plot sizes, naira), so names and amounts come out
right more often. The text then goes through /estate-chat like a typed message,
flagged as a voice note so the AI repeats any amounts back for the buyer to confirm.

Env: OPENAI_API_KEY, ESTATE_TRANSCRIBE_MODEL (default gpt-4o-transcribe).
"""
import io
import os

from . import land as LAND

MAX_BYTES = 20 * 1024 * 1024     # OpenAI accepts up to 25 MB

# Added to the system prompt when the buyer's latest message was a voice note
VOICE_RULE = (
    "VOICE NOTE\n"
    "The buyer's latest message was a WhatsApp voice note, turned into text automatically, so a word, "
    "name or number may have been heard wrongly. If it gives an amount of money, a plot size, a number of "
    "months or a date, repeat those figures back in your reply and ask the buyer to confirm them "
    "(for example: \"Just to confirm, you can pay ₦3,000,000 now?\"). If part of it makes no sense, "
    "politely ask them to say it again or type it. Never mention transcription or that a machine listened."
)


def _hint(tenant_id: int) -> str:
    words = []
    try:
        for e in LAND.load_estates(tenant_id):
            if e.get("title"):
                words.append(e["title"])
    except Exception as ex:
        print(f"⚠️ [ESTATE VOICE] hint error: {ex}")
    base = ("A buyer in Nigeria talking about buying land or property, in Nigerian English or Pidgin. "
            "Amounts in naira (₦), e.g. 3 million, 500k. Plot sizes in sqm, e.g. 300 sqm, 450 sqm, 600 sqm. "
            "Words like plot, estate, deposit, instalment, C of O, survey, site inspection.")
    if words:
        base += " Estate names: " + ", ".join(words[:20]) + "."
    return base


def transcribe(tenant_id: int, audio: bytes, filename: str = "voice.ogg") -> str:
    """Returns the text, or '' if nothing could be heard. Raises on service errors."""
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    f = io.BytesIO(audio)
    f.name = filename
    r = client.audio.transcriptions.create(
        model=os.getenv("ESTATE_TRANSCRIBE_MODEL", "gpt-4o-transcribe"),
        file=f,
        prompt=_hint(tenant_id),
    )
    return (getattr(r, "text", "") or "").strip()


# ── Agent's voice note on a lead (2026-10-22) ─────────────────────────────────
# After meeting or calling a buyer, the agent records what happened. The text is
# read here into a proposal (summary for the log, buyer details, next action);
# the portal shows it to the agent to check, and saves only what they confirm.

LAND_KEYS = ("buyer_kind", "purpose", "plot_size_sqm", "start_amount", "total_budget", "pay_mode",
             "monthly_amount", "timeframe")


def _agent_prompt(now_local: str, estates: list) -> str:
    names = ", ".join(e["title"] for e in estates) or "none listed"
    return (
        "You read a sales agent's spoken note about one buyer, for a Nigerian real-estate company. "
        "The agent may speak Nigerian English or Pidgin and talks about the buyer in the third person. "
        f"Right now it is {now_local}. The company's estates: {names}.\n"
        "Return ONLY a JSON object with these keys (omit any key the note doesn't give):\n"
        "  summary: one or two short sentences in plain English for the lead's log: what happened and what was agreed. "
        "Past tense. Only what the agent said, never invent.\n"
        "  interaction_type: 'call' | 'visit' | 'meeting' | 'whatsapp' | 'note' (how the agent was in touch; 'visit' = site visit or inspection).\n"
        "  area: city or area the buyer wants (title case).\n"
        "  budget_max: integer naira, for a house/property budget.\n"
        "  buyer_kind: 'land' (plots, land, estate) or 'property' (house, flat, rent).\n"
        "  purpose: 'build' | 'invest' | 'farm' | 'commercial'.\n"
        "  plot_size_sqm: number. 1 plot = 600 sqm, half plot = 300 sqm, two plots = 1200.\n"
        "  start_amount: integer naira the buyer can pay now / as deposit ('3 million' → 3000000, '500k' → 500000).\n"
        "  total_budget: integer naira for the whole purchase, only when clearly the total.\n"
        "  pay_mode: 'outright' | 'instalment'.\n"
        "  monthly_amount: integer naira per month.\n"
        "  timeframe: 'now' | '1-3 months' | '3-6 months' | 'later'.\n"
        "  estate_name: one of the company's estate names exactly as listed above, if the buyer chose or asked about it.\n"
        "  wants_inspection: true if the buyer wants or booked a site inspection / viewing.\n"
        "  asks_how_to_pay: true if the buyer asked for account details or how to pay / reserve.\n"
        "  next_action: {\"title\": short, starts with a verb, names the buyer, e.g. 'Confirm Chidi's Saturday inspection'; "
        "\"due\": local date-time 'YYYY-MM-DDTHH:MM'}. Use what the agent said; a visit on a day → confirm it the day before at 10:00; "
        "'call him tomorrow' → tomorrow 10:00; nothing said → the next working day (Mon–Sat) at 10:00.\n"
        "Never guess numbers that were not said. No explanation, only JSON."
    )


def read_agent_note(tenant_id: int, transcript: str, now_local: str) -> dict:
    import json
    from openai import OpenAI
    estates = LAND.load_estates(tenant_id)
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    kw = dict(model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
              messages=[{"role": "system", "content": _agent_prompt(now_local, estates)},
                        {"role": "user", "content": "Agent's note: " + transcript}],
              max_completion_tokens=1500, response_format={"type": "json_object"},
              reasoning_effort=os.getenv("ESTATE_REASONING_EFFORT", "low"))
    try:
        r = client.chat.completions.create(**kw)
    except Exception as e:
        if "reasoning_effort" not in str(e):
            raise
        kw.pop("reasoning_effort")
        r = client.chat.completions.create(**kw)
    raw = (r.choices[0].message.content or "").strip()
    data = json.loads(raw) if raw else {}
    # Estate name → the agency's estate (exact, then contained)
    name = (data.pop("estate_name", "") or "").strip().lower()
    if name:
        hit = next((e for e in estates if e["title"].lower() == name), None) or \
              next((e for e in estates if name in e["title"].lower() or e["title"].lower() in name), None)
        if hit:
            data["estate_listing_id"] = hit["id"]
            data["estate_title"] = hit["title"]
    return data
