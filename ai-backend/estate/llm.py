"""
estate/llm.py — LLM caller for estate AI. Does NOT prepend ecommerce base text.
Uses the estate-specific system prompt as-is.
"""
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


def estate_ask_llm(
    system_prompt: str,
    user_message: str,
    context_chunks: list,
    history: list = None,
) -> tuple:
    """
    Call the LLM with the estate-specific system prompt (not modified with ecommerce base).

    Returns (answer_text, usage_dict).
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

    response = _get_client().chat.completions.create(
        model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
        messages=messages,
        temperature=0.3,
        max_tokens=max_out,
    )

    answer = response.choices[0].message.content or ""
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

    return answer, usage
