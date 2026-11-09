"""Keep-your-number (WhatsApp Business app + PhiXtra on one number), 2026-11-09.

Meta calls this "onboarding WhatsApp Business app users" (coexistence).
Three webhook fields only come for numbers that keep the Business app:

- history            : the business's chats from the last 180 days, sent in
                       chunks after we ask for them (portal: smb_app_data).
                       Saved as old messages (is_historical) so the AI Agent,
                       reports and counts ignore them.
- smb_app_state_sync : contacts saved in the Business app (add / remove).
                       'add' fills All Contacts; 'remove' is ignored, we never
                       delete the business's own records.
- smb_message_echoes : a reply the owner typed in the Business app on the
                       phone. Shown in the Inbox as "Phone app" and the AI
                       Agent pauses on that chat, the same as a team takeover.

Progress is kept in wa_coexist_sync (one row per number, made by the portal
when the number connects). Never raises: Meta must always get HTTP 200.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from wa_db import get_db_connection, ensure_contact_from_inbound

COEXIST_FIELDS = {"history", "smb_app_state_sync", "smb_message_echoes"}
PHONE_APP_LABEL = "Phone app"
HISTORY_DECLINED = 2593109


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _values(payload: dict):
    """(field, value) for every change. The 'history declined' webhook comes
    without the usual object/entry wrapper, so accept that shape too."""
    if not isinstance(payload, dict):
        return
    if "entry" not in payload and "history" in payload and "metadata" in payload:
        yield "history", payload
        return
    for e in payload.get("entry") or []:
        for ch in e.get("changes") or []:
            yield ch.get("field"), ch.get("value") or {}


def is_coexist_payload(payload: dict) -> bool:
    return any(f in COEXIST_FIELDS for f, _ in _values(payload))


def first_phone_number_id(payload: dict) -> str:
    for _, v in _values(payload):
        pnid = (v.get("metadata") or {}).get("phone_number_id")
        if pnid:
            return str(pnid)
    return ""


def _content(m: dict) -> tuple[str, str]:
    """(text shown in the Inbox, message_type) for one WhatsApp message."""
    t = m.get("type") or "text"
    if t == "text":
        return ((m.get("text") or {}).get("body") or ""), "text"
    if t in ("image", "video", "document", "audio", "sticker"):
        cap = (m.get(t) or {}).get("caption") or ""
        names = {"image": "Photo", "video": "Video", "document": "File", "audio": "Voice note", "sticker": "Sticker"}
        return (f"[{names[t]}] {cap}".strip()), t
    if t == "media_placeholder":
        return "[Photo or file sent before PhiXtra]", "media"
    if t == "location":
        loc = m.get("location") or {}
        return f"[Location] {loc.get('name') or ''} {loc.get('address') or ''}".strip(), "location"
    if t == "contacts":
        return "[Contact card]", "contacts"
    if t in ("edit", "revoke"):
        return "", t
    return f"[{t}]", t


def _ts(m: dict):
    try:
        return datetime.fromtimestamp(int(m.get("timestamp")), tz=timezone.utc)
    except Exception:
        return None


# ── Phone-app replies ───────────────────────────────────────────────────────

def _echoes(cur, tenant_id: int, pnid: str, value: dict) -> int:
    n = 0
    for m in value.get("message_echoes") or []:
        to = _digits(m.get("to"))
        text, mtype = _content(m)
        if not to or not text:          # edits / deletions / no number: skip
            continue
        cur.execute(
            """INSERT INTO wa_message_log
                 (tenant_id, phone_number_id, customer_phone, direction, content,
                  message_type, meta_message_id, sent_by_label, created_at)
               VALUES (%s, %s, %s, 'outbound', %s, 'agent_reply', %s, %s, COALESCE(%s, NOW()))
               ON CONFLICT (meta_message_id) WHERE meta_message_id IS NOT NULL DO NOTHING""",
            (tenant_id, pnid, to, text, m.get("id"), PHONE_APP_LABEL, _ts(m)))
        # Someone answered from the phone: pause the AI Agent on this chat,
        # exactly like "Take over" in the Inbox.
        cur.execute(
            """INSERT INTO wa_handoff_state (session_id, tenant_id, customer_phone, takeover)
               VALUES (%s, %s, %s, TRUE)
               ON CONFLICT (session_id) DO UPDATE
                 SET resolved_at = NULL, escalated_at = NOW(), takeover = TRUE""",
            (f"wa-meta-{pnid}-{to}", tenant_id, to))
        n += 1
    return n


# ── Chat history ────────────────────────────────────────────────────────────

def _history(cur, tenant_id: int, pnid: str, value: dict) -> int:
    biz = _digits((value.get("metadata") or {}).get("display_phone_number"))
    n = 0
    for chunk in value.get("history") or []:
        errs = chunk.get("errors") or []
        if any(int(e.get("code") or 0) == HISTORY_DECLINED for e in errs):
            cur.execute("""UPDATE wa_coexist_sync SET history_declined=TRUE, history_done_at=NOW(), updated_at=NOW()
                           WHERE phone_number_id=%s""", (pnid,))
            continue
        if errs:
            cur.execute("""UPDATE wa_coexist_sync SET last_error=%s, updated_at=NOW() WHERE phone_number_id=%s""",
                        (str(errs[0].get("message") or errs[0])[:500], pnid))
            continue
        for th in chunk.get("threads") or []:
            cust = _digits((th.get("context") or {}).get("wa_id") or th.get("id"))
            if not cust:
                continue
            for m in th.get("messages") or []:
                text, mtype = _content(m)
                if not text:
                    continue
                outbound = _digits(m.get("from")) == biz if biz else _digits(m.get("from")) != cust
                cur.execute(
                    """INSERT INTO wa_message_log
                         (tenant_id, phone_number_id, customer_phone, direction, content, message_type,
                          meta_message_id, sent_by_label, created_at, is_historical)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()), TRUE)
                       ON CONFLICT (meta_message_id) WHERE meta_message_id IS NOT NULL DO NOTHING""",
                    (tenant_id, pnid, cust, "outbound" if outbound else "inbound", text,
                     "agent_reply" if outbound else mtype, m.get("id"),
                     PHONE_APP_LABEL if outbound else None, _ts(m)))
                n += cur.rowcount
            ensure_contact_from_inbound(tenant_id, cust)
        md = chunk.get("metadata") or {}
        try:
            prog = int(md.get("progress") or 0)
        except Exception:
            prog = 0
        cur.execute(
            """UPDATE wa_coexist_sync
                  SET history_progress = GREATEST(COALESCE(history_progress,0), %s),
                      history_messages = COALESCE(history_messages,0) + %s,
                      history_done_at  = CASE WHEN %s >= 100 THEN COALESCE(history_done_at, NOW()) ELSE history_done_at END,
                      updated_at = NOW()
                WHERE phone_number_id=%s""", (prog, n, prog, pnid))
    return n


# ── Contacts ────────────────────────────────────────────────────────────────

def _contacts(cur, tenant_id: int, pnid: str, value: dict) -> int:
    # The owner chose not to copy contacts: Meta may still send later
    # contact changes, so ignore them all.
    cur.execute("SELECT want_contacts FROM wa_coexist_sync WHERE phone_number_id=%s", (pnid,))
    row = cur.fetchone()
    if not row or not row[0]:
        return 0
    n = 0
    for s in value.get("state_sync") or []:
        if s.get("type") != "contact" or s.get("action") != "add":
            continue
        c = s.get("contact") or {}
        num = _digits(c.get("phone_number"))
        if len(num) < 8:
            continue
        name = (c.get("full_name") or c.get("first_name") or "").strip()[:200] or None
        cur.execute("SELECT id, display_name FROM wa_contacts WHERE tenant_id=%s AND whatsapp_number=%s LIMIT 1",
                    (tenant_id, num))
        row = cur.fetchone()
        if row:
            if name and not row[1]:
                cur.execute("UPDATE wa_contacts SET display_name=%s, updated_at=NOW() WHERE id=%s", (name, row[0]))
            continue
        cur.execute("SELECT 1 FROM wa_contacts WHERE tenant_id=%s AND phone=%s LIMIT 1", (tenant_id, num))
        if cur.fetchone():
            continue
        cur.execute("""INSERT INTO wa_contacts (tenant_id, whatsapp_number, display_name, source)
                       VALUES (%s, %s, %s, 'whatsapp')
                       ON CONFLICT (tenant_id, whatsapp_number) DO NOTHING""", (tenant_id, num, name))
        n += cur.rowcount
    if n:
        cur.execute("""UPDATE wa_coexist_sync SET contacts_count = COALESCE(contacts_count,0) + %s,
                              contacts_done_at = COALESCE(contacts_done_at, NOW()), updated_at = NOW()
                       WHERE phone_number_id=%s""", (n, pnid))
    return n


def handle(payload: dict, tenant: dict) -> dict:
    """Save everything in one coexistence webhook for this tenant's number."""
    out = {"echoes": 0, "history": 0, "contacts": 0}
    tenant_id = int(tenant["tenant_id"])
    conn = get_db_connection()
    if not conn:
        return out
    cur = conn.cursor()
    try:
        for field, v in _values(payload):
            pnid = str((v.get("metadata") or {}).get("phone_number_id") or "")
            if not pnid:
                continue
            if field == "smb_message_echoes":
                out["echoes"] += _echoes(cur, tenant_id, pnid, v)
            elif field == "history":
                out["history"] += _history(cur, tenant_id, pnid, v)
            elif field == "smb_app_state_sync":
                out["contacts"] += _contacts(cur, tenant_id, pnid, v)
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"⚠️ [COEXIST] tenant={tenant_id} error:", e)
    finally:
        cur.close(); conn.close()
    print(f"ℹ️ [COEXIST] tenant={tenant_id} {out}")
    return out
