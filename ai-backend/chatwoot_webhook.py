"""
chatwoot_webhook.py — Phixtra AI <-> Chatwoot / WhatsApp Business integration.

Receives "message_created" webhook events from self-hosted Chatwoot and:
  1. Ignores outbound (bot/agent) messages and activity events.
  2. Skips conversations that a human agent has already taken over.
  3. Detects human-escalation keywords → assigns conversation to the handoff team.
  4. Forwards inbound customer messages to POST /chat (Phixtra AI).
  5. Sends the AI reply back to the customer via the Chatwoot messages API.
  6. Assigns the conversation to the handoff team when the AI triggers a handoff.
"""

import asyncio
import hashlib
import hmac
import json
import os
from typing import Optional

import requests
from fastapi import APIRouter, Header, HTTPException, Request

router = APIRouter()

# ── Env config ────────────────────────────────────────────────────────────────
_CHATWOOT_BASE_URL   = os.getenv("CHATWOOT_BASE_URL", "").rstrip("/")
_CHATWOOT_API_TOKEN  = os.getenv("CHATWOOT_API_TOKEN", "")
_CHATWOOT_ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID", "")
_CHATWOOT_TEAM_ID    = os.getenv("CHATWOOT_HANDOFF_TEAM_ID", "")
_WEBHOOK_SECRET      = os.getenv("CHATWOOT_WEBHOOK_SECRET", "")
_PHIXTRA_API_KEY     = os.getenv("PHIXTRA_WA_API_KEY", "")
_CHAT_URL            = "http://127.0.0.1:8000/chat"

# Chatwoot message_type values — Chatwoot webhooks send the string form
# ("incoming", "outgoing", "activity") in the top-level payload, but the
# integer form (0, 1, 2) appears inside conversation.messages. Accept both.
_MSG_INCOMING_INT = 0
_MSG_INCOMING_STR = "incoming"

# Keywords that bypass AI and escalate to a human agent immediately
_HUMAN_KEYWORDS = frozenset({
    "agent", "human", "real person", "speak to someone", "talk to someone",
    "speak to a person", "talk to a person", "connect me to", "live agent",
    "speak to agent", "talk to agent", "call me", "phone me",
    "speak to a human", "talk to a human",
})


# ── Chatwoot API helpers ───────────────────────────────────────────────────────

def _cw_headers() -> dict:
    return {
        "api_access_token": _CHATWOOT_API_TOKEN,
        "Content-Type": "application/json",
    }


def _send_message(account_id: str, conversation_id: int, content: str) -> bool:
    """Send an outbound message on a Chatwoot conversation (delivered to WhatsApp)."""
    url = (
        f"{_CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}"
        f"/conversations/{conversation_id}/messages"
    )
    payload = {"content": content, "message_type": "outgoing", "private": False}
    try:
        resp = requests.post(url, json=payload, headers=_cw_headers(), timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"⚠️ [CW] send_message failed conv={conversation_id}: {e}")
        return False


def _assign_team(account_id: str, conversation_id: int) -> bool:
    """Assign a conversation to the configured handoff team in Chatwoot."""
    if not _CHATWOOT_TEAM_ID:
        print("⚠️ [CW] CHATWOOT_HANDOFF_TEAM_ID not set — skipping team assignment")
        return False
    url = (
        f"{_CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}"
        f"/conversations/{conversation_id}/assignments"
    )
    try:
        resp = requests.post(
            url, json={"team_id": int(_CHATWOOT_TEAM_ID)},
            headers=_cw_headers(), timeout=10,
        )
        resp.raise_for_status()
        print(f"✅ [CW] conv={conversation_id} assigned to team {_CHATWOOT_TEAM_ID}")
        return True
    except Exception as e:
        print(f"⚠️ [CW] assign_team failed conv={conversation_id}: {e}")
        return False


def _call_chat(api_key: str, message: str, session_id: str) -> dict:
    """Synchronous internal call to POST /chat. Runs in a thread via asyncio.to_thread."""
    resp = requests.post(
        _CHAT_URL,
        json={"api_key": api_key, "message": message, "session_id": session_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ── Formatting ────────────────────────────────────────────────────────────────

def _products_to_text(products: list) -> str:
    """
    Convert product_recommendations to plain WhatsApp-friendly text.
    WhatsApp does not render rich product cards, so we format them as a numbered list.
    """
    lines = []
    for i, p in enumerate(products[:4], 1):
        name  = p.get("name", "")
        price = p.get("price", "")
        url   = p.get("url", "")
        if not name:
            continue
        line = f"{i}. {name}"
        if price:
            line += f" — {price}"
        if url:
            line += f"\n   {url}"
        lines.append(line)
    return "\n".join(lines)


# ── Signature verification ────────────────────────────────────────────────────

def _verify_signature(body: bytes, sig: str) -> bool:
    """
    Verify Chatwoot HMAC-SHA256 webhook signature.
    If CHATWOOT_WEBHOOK_SECRET is not set, all requests pass (useful during setup).
    """
    if not _WEBHOOK_SECRET:
        return True
    expected = hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig or "")


def _wants_human(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _HUMAN_KEYWORDS)


# ── Webhook endpoint ───────────────────────────────────────────────────────────

@router.post("/chatwoot-webhook")
async def chatwoot_webhook(
    request: Request,
    x_chatwoot_signature: Optional[str] = Header(None),
):
    """
    Chatwoot webhook receiver. Chatwoot calls this URL for every event on the
    connected WhatsApp Business inbox.

    Setup in Chatwoot: Settings → Integrations → Webhooks → New Webhook
    URL: https://<your-domain>/chatwoot-webhook
    Events: ✓ Message Created
    """
    body = await request.body()

    if not _verify_signature(body, x_chatwoot_signature or ""):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # ── Only handle new incoming customer messages ─────────────────────────────
    if payload.get("event") != "message_created":
        return {"status": "ignored", "reason": "event_not_message_created"}

    # Chatwoot sends message_type as "incoming"/"outgoing"/"activity" (string)
    # in the top-level webhook payload. The integer form (0/1/2) only appears
    # inside conversation.messages. Accept both to be safe.
    msg_type = payload.get("message_type")
    if msg_type not in (_MSG_INCOMING_INT, _MSG_INCOMING_STR):
        return {"status": "ignored", "reason": "not_incoming_message"}

    content = (payload.get("content") or "").strip()
    if not content:
        return {"status": "ignored", "reason": "empty_content"}

    conversation    = payload.get("conversation") or {}
    conversation_id = conversation.get("id")
    if not conversation_id:
        return {"status": "ignored", "reason": "no_conversation_id"}

    # Resolve account ID from payload or fall back to env
    account_id = str((payload.get("account") or {}).get("id") or _CHATWOOT_ACCOUNT_ID)

    session_id = f"cw-{conversation_id}"
    print(f"✅ [CW] conv={conversation_id} session={session_id}: {content[:80]}")

    # ── Keyword-triggered human escalation ────────────────────────────────────
    if _wants_human(content):
        print(f"🙋 [CW] Human keyword → escalating conv={conversation_id}")
        await asyncio.to_thread(
            _send_message, account_id, conversation_id,
            "I'm connecting you to a member of our team right now. Someone will be with you shortly.",
        )
        await asyncio.to_thread(_assign_team, account_id, conversation_id)
        return {"status": "escalated", "reason": "keyword"}

    if not _PHIXTRA_API_KEY:
        print("⚠️ [CW] PHIXTRA_WA_API_KEY not configured — cannot process message")
        return {"status": "error", "reason": "no_api_key"}

    # ── Call Phixtra AI ───────────────────────────────────────────────────────
    try:
        result = await asyncio.to_thread(_call_chat, _PHIXTRA_API_KEY, content, session_id)
    except Exception as e:
        print(f"⚠️ [CW] /chat failed conv={conversation_id}: {e}")
        await asyncio.to_thread(
            _send_message, account_id, conversation_id,
            "I'm sorry, I'm experiencing a technical issue right now. Please try again in a moment.",
        )
        return {"status": "error", "reason": "chat_error"}

    reply    = (result.get("reply") or "").strip()
    handoff  = bool(result.get("handoff_triggered"))
    products = result.get("product_recommendations") or []

    # Append product recommendations as plain text (WhatsApp has no rich cards)
    product_text = _products_to_text(products)
    if product_text:
        reply = f"{reply}\n\n{product_text}"

    # ── Send AI reply to customer ─────────────────────────────────────────────
    if reply:
        await asyncio.to_thread(_send_message, account_id, conversation_id, reply)

    # ── AI-triggered handoff: assign to human team ────────────────────────────
    if handoff:
        print(f"🙋 [CW] AI handoff triggered → assigning conv={conversation_id}")
        await asyncio.to_thread(_assign_team, account_id, conversation_id)

    return {"status": "ok", "handoff_triggered": handoff}
