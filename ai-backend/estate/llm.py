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

    messages.append({"role": "user", "content": (user_message or "").strip()})

    max_out = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "600"))

    create_kwargs = dict(
        model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
        messages=messages,
        temperature=0.3,
        max_tokens=max_out,
    )
    if structured_handoff:
        create_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": _HANDOFF_RESPONSE_SCHEMA,
        }

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
