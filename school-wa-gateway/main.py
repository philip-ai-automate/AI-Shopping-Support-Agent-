"""
main.py — PhiXtra School WhatsApp Gateway
Port 8002. Handles inbound parent messages forwarded from the merchant gateway.
"""
import os
import asyncio
import logging
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import PlainTextResponse

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("school-gateway")

app = FastAPI(title="PhiXtra School WA Gateway")

VERIFY_TOKEN = os.getenv("SCHOOL_WA_VERIFY_TOKEN", "school-phixtra-webhook-2025")


# ── Webhook verification (Meta GET handshake) ──────────────────────────────────

@app.get("/webhook")
async def webhook_verify(request: Request):
    params    = dict(request.query_params)
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("✅ [SCHOOL] Webhook verified")
        return PlainTextResponse(challenge)

    log.warning("⚠️ [SCHOOL] Webhook verification failed — token mismatch")
    return Response(status_code=403)


# ── Incoming message handler (Meta POST) ──────────────────────────────────────

@app.post("/webhook")
async def webhook_receive(request: Request, background: BackgroundTasks):
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)

    background.add_task(_process_payload, body)
    return Response(status_code=200)


def _extract_text(msg: dict) -> str | None:
    """
    Extract a plain-text representation from any supported message type.
    Returns None for types we cannot meaningfully process (image, video, etc.).
    """
    msg_type = msg.get("type", "")

    if msg_type == "text":
        return (msg.get("text") or {}).get("body", "").strip() or None

    if msg_type == "interactive":
        interactive = msg.get("interactive", {})
        itype = interactive.get("type", "")
        if itype == "button_reply":
            return interactive.get("button_reply", {}).get("title", "").strip() or None
        if itype == "list_reply":
            item = interactive.get("list_reply", {})
            title = item.get("title", "").strip()
            desc  = item.get("description", "").strip()
            return f"{title}: {desc}" if desc else title or None

    if msg_type == "button":
        # Quick-reply button tap
        return (msg.get("button") or {}).get("text", "").strip() or None

    return None


async def _process_payload(body: dict):
    """Extract messages from Meta payload and dispatch to AI handler."""
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
                messages        = value.get("messages", [])

                for msg in messages:
                    sender = msg.get("from", "")
                    text   = _extract_text(msg)

                    if text is None:
                        log.info(f"ℹ️ [SCHOOL] Skipping unsupported type={msg.get('type')} from {sender}")
                        continue

                    log.info(f"📨 [SCHOOL] {sender}: {text[:80]}")

                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None,
                        _handle_message_sync,
                        phone_number_id, sender, text,
                    )
    except Exception as e:
        log.error(f"⚠️ [SCHOOL] _process_payload error: {e}")


def _handle_message_sync(phone_number_id: str, sender: str, text: str):
    try:
        from ai_handler import handle_message
        ok = handle_message(phone_number_id, sender, text)
        log.info(f"{'✅' if ok else '❌'} [SCHOOL] Reply {'sent' if ok else 'failed'} to {sender}")
    except Exception as e:
        log.error(f"⚠️ [SCHOOL] _handle_message_sync error: {e}")


# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "school-wa-gateway"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8002)), reload=False)
