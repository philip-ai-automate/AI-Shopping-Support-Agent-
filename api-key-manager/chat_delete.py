"""
chat_delete.py — delete a chat by hand (12 Nov 2026, user decision: chats are
deleted manually, website AND WhatsApp; review page UePNYhf32XsiCW1ycdwRga).

Used by:
  - Inbox "🗑 Delete chat" (owner, or a team member with the "Delete chats" tick)
  - Admin › Privacy Requests (Shopify buyer delete requests)

What goes:
  website chat  'web:<session_id>' — messages + AI memory (chat_messages,
      chat_summaries, chat_sessions), staff replies, the buyer's details
      (chat_visitors), hand-over requests, take-over / presence / alerts,
      claim, push subscription, abandoned-cart entry for that chat.
  WhatsApp chat '<digits>' — every message with that number (wa_message_log
      + delivery ticks), the AI memory of the chat ('wa-meta-<number id>-<phone>'
      sessions), hand-over state, claim, shop session, product/photo context.

What stays: leads, contacts (WhatsApp Contacts), quotes, discount codes,
call log, campaigns, access log, "Fix this answer" corrections, usage counts.
A lead linked to the chat gets one History line (no message text).
"""
import re

from db import get_db_connection

WEB_PREFIX = "web:"


def kind_of(key: str) -> str:
    """'web', 'whatsapp' or '' (not deletable here)."""
    key = (key or "").strip()
    if key.startswith(WEB_PREFIX) and len(key) > len(WEB_PREFIX):
        return "web"
    if re.fullmatch(r"\d{7,16}", key):
        return "whatsapp"
    return ""


def _sessions_web(cur, tid, sid):
    """[sid] if this chat really belongs to the business, else []."""
    for t in ("chat_messages", "handoff_requests", "web_chat_replies", "chat_visitors", "chat_sessions"):
        cur.execute(f"SELECT 1 FROM {t} WHERE tenant_id=%s AND session_id=%s LIMIT 1", (tid, sid))
        if cur.fetchone():
            return [sid]
    return []


def _sessions_wa(cur, tid, phone):
    """The AI memory sessions of this WhatsApp chat: 'wa-meta-<number id>-<phone>'."""
    out = set()
    for t in ("chat_messages", "chat_sessions", "chat_summaries", "handoff_requests", "wa_handoff_state", "wa_shop_session"):
        cur.execute(f"SELECT DISTINCT session_id FROM {t} WHERE tenant_id=%s AND session_id LIKE %s",
                    (tid, "wa-meta-%-" + phone))
        out |= {r[0] for r in cur.fetchall() if r[0] and r[0].endswith("-" + phone)}
    return sorted(out)


def _lead_for(cur, tid, kind, ident):
    if kind == "web":
        cur.execute("""SELECT q.lead_id FROM lead_qual_sources q JOIN merchant_pipeline_leads l ON l.id=q.lead_id
                        WHERE q.tenant_id=%s AND q.source_key=%s AND l.tenant_id=%s LIMIT 1""",
                    (tid, "chat:" + ident, tid))
    else:
        cur.execute("""SELECT id FROM merchant_pipeline_leads
                        WHERE tenant_id=%s AND regexp_replace(COALESCE(phone,''), '\\D', '', 'g') = %s
                        ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 1""", (tid, ident))
    r = cur.fetchone()
    return r[0] if r else None


def count(tenant_id: int, key: str) -> int:
    """Messages in the chat (for the confirm box)."""
    k = kind_of(key)
    conn = get_db_connection(); cur = conn.cursor()
    try:
        if k == "web":
            sid = key[len(WEB_PREFIX):]
            cur.execute("""SELECT (SELECT COUNT(*) FROM chat_messages WHERE tenant_id=%s AND session_id=%s)
                                + (SELECT COUNT(*) FROM web_chat_replies WHERE tenant_id=%s AND session_id=%s)""",
                        (tenant_id, sid, tenant_id, sid))
        elif k == "whatsapp":
            cur.execute("SELECT COUNT(*) FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s", (tenant_id, key))
        else:
            return 0
        return int(cur.fetchone()[0] or 0)
    finally:
        cur.close(); conn.close()


def delete(tenant_id: int, key: str, by: str) -> dict:
    """Delete one chat of this business. Returns {'ok', 'kind', 'messages', 'lead_id'}."""
    tid = int(tenant_id)
    k = kind_of(key)
    if not k:
        return {"ok": False, "error": "This kind of chat can't be deleted here."}
    conn = get_db_connection(); cur = conn.cursor()
    try:
        n = 0
        if k == "web":
            sid = key[len(WEB_PREFIX):]
            sessions = _sessions_web(cur, tid, sid)
            if not sessions:
                return {"ok": False, "error": "Chat not found."}
            lead_id = _lead_for(cur, tid, "web", sid)
            cur.execute("DELETE FROM chat_messages WHERE tenant_id=%s AND session_id=%s", (tid, sid)); n += cur.rowcount
            cur.execute("DELETE FROM web_chat_replies WHERE tenant_id=%s AND session_id=%s", (tid, sid)); n += cur.rowcount
            for t in ("chat_summaries", "chat_sessions", "chat_visitors", "handoff_requests", "web_chat_presence",
                      "web_chat_takeover", "web_chat_alert_queue", "push_subscriptions", "abandonment_queue"):
                cur.execute(f"DELETE FROM {t} WHERE tenant_id=%s AND session_id=%s", (tid, sid))
            cur.execute("DELETE FROM wa_conversation_assignments WHERE tenant_id=%s AND customer_phone=%s", (tid, key))
            what = "Website chat"
        else:
            phone = key
            sessions = _sessions_wa(cur, tid, phone)
            cur.execute("SELECT 1 FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s LIMIT 1", (tid, phone))
            if not cur.fetchone() and not sessions:
                return {"ok": False, "error": "Chat not found."}
            lead_id = _lead_for(cur, tid, "whatsapp", phone)
            cur.execute("""DELETE FROM wa_msg_status WHERE meta_message_id IN
                             (SELECT meta_message_id FROM wa_message_log
                               WHERE tenant_id=%s AND customer_phone=%s AND meta_message_id IS NOT NULL)""", (tid, phone))
            cur.execute("DELETE FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s", (tid, phone)); n += cur.rowcount
            if sessions:
                for t in ("chat_messages", "chat_summaries", "chat_sessions", "handoff_requests",
                          "wa_handoff_state", "wa_shop_session"):
                    cur.execute(f"DELETE FROM {t} WHERE tenant_id=%s AND session_id = ANY(%s)", (tid, sessions))
                # No tenant column: session ids are this business's own ('wa-meta-<its number id>-<phone>').
                for t in ("wa_photo_context", "wa_product_cache"):
                    cur.execute(f"DELETE FROM {t} WHERE session_id = ANY(%s)", (sessions,))
            cur.execute("DELETE FROM wa_handoff_state WHERE tenant_id=%s AND customer_phone=%s", (tid, phone))
            cur.execute("DELETE FROM wa_conversation_assignments WHERE tenant_id=%s AND customer_phone=%s", (tid, phone))
            what = "WhatsApp chat"
        if lead_id:
            cur.execute("SELECT stage FROM merchant_pipeline_leads WHERE id=%s", (lead_id,))
            st = (cur.fetchone() or [None])[0]
            cur.execute("""INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                           VALUES (%s,%s,%s,%s,%s)""", (lead_id, st, st, (by or "")[:200], f"{what} deleted by {by}"[:2000]))
        conn.commit()
        return {"ok": True, "kind": k, "messages": n, "lead_id": lead_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()
