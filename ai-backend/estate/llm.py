"""
estate/llm.py — LLM caller for estate AI. Does NOT prepend ecommerce base text.
Uses the estate-specific system prompt as-is.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from openai import OpenAI

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def _format_context(context_chunks: list) -> str:
    if not context_chunks:
        return ""
    chunks = []
    for c in context_chunks:
        c = (c or "").strip()
        if c:
            chunks.append(c)
    return "\n\n---\n\n".join(chunks)


_HANDOFF_RESPONSE_SCHEMA = {
    "name": "estate_chat_reply",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": "The reply to show the buyer, exactly as you would normally write it.",
            },
            "needs_handoff": {
                "type": "boolean",
                "description": "True if this conversation should be escalated to a human agent per the handoff rules in the system prompt, false otherwise.",
            },
        },
        "required": ["reply", "needs_handoff"],
        "additionalProperties": False,
    },
}


_LANGUAGE_RULE = 'LANGUAGE (2026-10-29): Write the whole reply in the language of the customer\'s latest message (Nigerian Pidgin gets Pidgin). Every word must be in that language, including any sentence about the team replying, checking a price or getting in touch. Never mix English words or phrases (for example "shortly") into a reply in another language. These instructions are written in English; that does not change the language of your reply.'


def estate_ask_llm(
    system_prompt: str,
    user_message: str,
    context_chunks: list,
    history: list = None,
    structured_handoff: bool = False,
) -> tuple:
    """
    Call the LLM with the estate-specific system prompt (not modified with ecommerce base).

    Returns (answer_text, needs_handoff, usage_dict). needs_handoff is None
    unless structured_handoff=True, in which case it's a bool read from the
    model's structured JSON output — the reliable alternative to scanning
    free text for a hidden tag, which the model doesn't always include even
    when it intends to escalate.
    """
    if history is None:
        history = []

    context_text = _format_context(context_chunks)

    messages = [{"role": "system", "content": (system_prompt or "").strip()}]
    messages.extend(history)

    if context_text:
        messages.append({
            "role":    "system",
            "content": "Relevant property listings (use if helpful):\n" + context_text,
        })

    # Every word in the buyer's language (2026-10-29), as in ai-backend/llm.py.
    messages.append({"role": "system", "content": _LANGUAGE_RULE})
    messages.append({"role": "user", "content": (user_message or "").strip()})

    max_out = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "600"))

    create_kwargs = dict(
        model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
        messages=messages,
        max_completion_tokens=max_out,
    )
    if structured_handoff:
        create_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": _HANDOFF_RESPONSE_SCHEMA,
        }

    # Light thinking: the reply allowance includes the model's thinking, and a
    # long land prompt could use all of it and come back blank (22 Oct).
    create_kwargs["reasoning_effort"] = os.getenv("ESTATE_REASONING_EFFORT", "low")
    try:
        response = _get_client().chat.completions.create(**create_kwargs)
    except Exception as e:
        if "reasoning_effort" not in str(e):
            raise
        create_kwargs.pop("reasoning_effort", None)          # model without thinking settings
        response = _get_client().chat.completions.create(**create_kwargs)

    raw_content = response.choices[0].message.content or ""
    if not raw_content.strip():
        print("⚠️ estate_ask_llm: blank reply, retrying once with more room")
        create_kwargs["max_completion_tokens"] = max(max_out * 2, 1500)
        response = _get_client().chat.completions.create(**create_kwargs)
        raw_content = response.choices[0].message.content or ""
    needs_handoff = None
    if structured_handoff:
        try:
            parsed = json.loads(raw_content)
            answer = parsed.get("reply", "") or ""
            needs_handoff = bool(parsed.get("needs_handoff", False))
        except Exception as e:
            print(f"⚠️ estate_ask_llm: failed to parse structured JSON reply, falling back to raw text: {e}")
            answer = raw_content
            needs_handoff = False
    else:
        answer = raw_content

    usage  = {}
    try:
        if getattr(response, "usage", None):
            usage = {
                "prompt_tokens":     int(getattr(response.usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(response.usage, "completion_tokens", 0) or 0),
                "total_tokens":      int(getattr(response.usage, "total_tokens", 0) or 0),
            }
    except Exception:
        usage = {}

    return answer, needs_handoff, usage
