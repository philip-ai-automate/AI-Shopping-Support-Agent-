"""
web_team_chat.py — website chat answered by the business's own team (2026-10-06).

On PhiXtra Connect (or whenever the AI is switched off) a website visitor's
message goes to the Inbox's Web Chat instead of the AI. Staff reply from the
Inbox; the chat box picks the reply up live while the visitor is on the page
(web_chat_presence), otherwise the portal emails it.

Used by the paste-in chat box (/widget/message, /widget/poll) and by the
WordPress plugin's /chat call. Everything here is best-effort and never raises
into the request — a visitor's message must never get an error because of it.
"""

import re
import psycopg2.extras
from db import get_db_connection

AI_OFF_PLAN_SLUGS = ("connect", "web_free")
ONLINE_SECONDS = 30

_EMAIL_RE = re.compile(r"[^@\s<>()\[\],;:\"']+@[^@\s<>()\[\],;:\"']+\.[A-Za-z]{2,}")


def ai_is_off(tenant_id: int) -> bool:
    """True when this business's plan has no AI (PhiXtra Connect) or the AI
    switch is off — the same rule the paste-in chat box uses."""
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute("""SELECT COALESCE(p.slug, ''), t.ai_enabled FROM tenants t
                       LEFT JOIN plans p ON p.id = t.plan_id WHERE t.id = %s""", (tenant_id,))
        r = cur.fetchone()
        return bool(r) and (r[0] in AI_OFF_PLAN_SLUGS or r[1] is False)
    except Exception as e:
        print(f"⚠️ [TEAM CHAT] ai_is_off tenant={tenant_id}: {e}")
        return False
    finally:
        cur.close(); conn.close()


def contact_for(cur, tenant_id: int, session_id: str) -> dict:
    cur.execute("""
        SELECT (ARRAY_AGG(visitor_name ORDER BY id DESC) FILTER (WHERE COALESCE(visitor_name,'') <> ''))[1] AS name,
               (ARRAY_AGG(visitor_email ORDER BY id DESC) FILTER (WHERE COALESCE(visitor_email,'') <> ''))[1] AS email,
               (ARRAY_AGG(whatsapp_number ORDER BY id DESC) FILTER (WHERE COALESCE(whatsapp_number,'') <> ''))[1] AS phone
          FROM handoff_requests WHERE tenant_id = %s AND session_id = %s
    """, (tenant_id, session_id))
    r = cur.fetchone() or {}
    return {"name": r.get("name") or "", "email": r.get("email") or "", "phone": r.get("phone") or ""}


def queue_alert(cur, tenant_id: int, session_id: str, label: str, preview: str,
                reason: str = "new_chat", allow_owner: bool = True) -> None:
    """Hand the chat to the WhatsApp gateway's staff alerts (it checks the
    queue every few seconds and sends one alert per chat)."""
    cur.execute("""
        INSERT INTO web_chat_alert_queue (tenant_id, session_id, label, preview, reason, allow_owner)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (tenant_id, session_id[:64], (label or "Website visitor")[:200], (preview or "")[:500],
          reason, allow_owner))


def visitor_label(contact: dict) -> str:
    name = (contact or {}).get("name") or ""
    return f"Website visitor ({name})" if name else "Website visitor"


def save_visitor_message(tenant_id: int, session_id: str, text: str) -> dict:
    """Store a visitor's message for the team. Keeps one 'waiting for a
    reply' row per chat (a new one after staff have answered), picks up an
    email or phone number typed into the message, and queues a staff alert.
    Returns {"contact": {...}, "first": bool}."""
    from handoff import _extract_phone, save_web_visitor_contact
    out = {"contact": {"name": "", "email": "", "phone": ""}, "first": False}
    conn = get_db_connection()
    if not conn:
        return out
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT 1 FROM chat_messages WHERE tenant_id=%s AND session_id=%s LIMIT 1",
                    (tenant_id, session_id))
        out["first"] = cur.fetchone() is None
        cur.execute("INSERT INTO chat_messages (session_id, tenant_id, role, content) VALUES (%s, %s, 'user', %s)",
                    (session_id, tenant_id, text))
        m = _EMAIL_RE.search(text or "")
        typed_email = m.group(0).strip(".") if m else ""
        typed_phone = _extract_phone(re.sub(r"\S*@\S*", " ", text or ""))
        cur.execute("""SELECT id FROM handoff_requests WHERE tenant_id=%s AND session_id=%s AND status='pending'
                       ORDER BY id DESC LIMIT 1""", (tenant_id, session_id))
        pending = cur.fetchone()
        if pending:
            if typed_email or typed_phone:
                cur.execute("""UPDATE handoff_requests
                                  SET visitor_email   = COALESCE(NULLIF(%s,''), visitor_email),
                                      whatsapp_number = COALESCE(NULLIF(%s,''), whatsapp_number)
                                WHERE id = %s""", (typed_email, typed_phone, pending["id"]))
        else:
            cur.execute("""INSERT INTO handoff_requests
                               (tenant_id, session_id, whatsapp_number, visitor_message, status, visitor_email)
                           VALUES (%s, %s, %s, %s, 'pending', %s)""",
                        (tenant_id, session_id, typed_phone or None, (text or "")[:1000], typed_email or None))
        contact = contact_for(cur, tenant_id, session_id)
        out["contact"] = contact
        queue_alert(cur, tenant_id, session_id, visitor_label(contact), text)
        conn.commit()
        if typed_email or typed_phone:
            save_web_visitor_contact(tenant_id, contact["name"], typed_phone, typed_email)
    except Exception as e:
        conn.rollback()
        print(f"⚠️ [TEAM CHAT] save_visitor_message tenant={tenant_id}: {e}")
    finally:
        cur.close(); conn.close()
    return out


def touch(cur, tenant_id: int, session_id: str) -> None:
    cur.execute("""
        INSERT INTO web_chat_presence (tenant_id, session_id, last_seen_at) VALUES (%s, %s, NOW())
        ON CONFLICT (tenant_id, session_id) DO UPDATE SET last_seen_at = NOW()
    """, (tenant_id, session_id[:64]))


def _who(label: str) -> str:
    """First name only for the visitor (never an email address)."""
    word = (label or "").strip().split(" ")[0] if label else ""
    return "" if (not word or "@" in word) else word[:40]


def poll(tenant_id: int, session_id: str, after: int, business_name: str, history: bool = False) -> dict:
    """The chat box checks in: marks the visitor as on the page, returns
    staff replies newer than `after` (and marks them shown). history=True
    (the box's first check-in on a page) returns the whole conversation
    instead, so a returning visitor sees it again."""
    out = {"replies": [], "history": [], "last_id": after}
    conn = get_db_connection()
    if not conn:
        return out
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT 1 FROM chat_messages WHERE tenant_id=%s AND session_id=%s LIMIT 1",
                    (tenant_id, session_id))
        has_chat = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM web_chat_replies WHERE tenant_id=%s AND session_id=%s LIMIT 1",
                    (tenant_id, session_id))
        has_chat = has_chat or cur.fetchone() is not None
        if not has_chat:
            conn.commit()
            return out
        touch(cur, tenant_id, session_id)
        if history:
            cur.execute("""
                SELECT kind, id, content, label, created_at FROM (
                    SELECT CASE WHEN role='user' THEN 'visitor' ELSE 'ai' END AS kind, 0 AS id, content,
                           NULL AS label, created_at
                      FROM chat_messages WHERE tenant_id=%s AND session_id=%s
                    UNION ALL
                    SELECT 'staff', id, content, sent_by_label, created_at
                      FROM web_chat_replies WHERE tenant_id=%s AND session_id=%s
                ) x ORDER BY created_at, id LIMIT 200
            """, (tenant_id, session_id, tenant_id, session_id))
            for r in cur.fetchall() or []:
                item = {"kind": r["kind"], "text": r["content"]}
                if r["kind"] == "staff":
                    item["by"] = _who(r["label"])
                    out["last_id"] = max(out["last_id"], int(r["id"]))
                out["history"].append(item)
        else:
            cur.execute("""SELECT id, content, sent_by_label FROM web_chat_replies
                            WHERE tenant_id=%s AND session_id=%s AND id > %s ORDER BY id""",
                        (tenant_id, session_id, after))
            for r in cur.fetchall() or []:
                out["replies"].append({"id": int(r["id"]), "text": r["content"], "by": _who(r["sent_by_label"])})
                out["last_id"] = max(out["last_id"], int(r["id"]))
        cur.execute("""UPDATE web_chat_replies SET delivered_at = NOW()
                        WHERE tenant_id=%s AND session_id=%s AND delivered_at IS NULL AND id <= %s""",
                    (tenant_id, session_id, out["last_id"]))
        out["business"] = business_name
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"⚠️ [TEAM CHAT] poll tenant={tenant_id}: {e}")
    finally:
        cur.close(); conn.close()
    return out


def plugin_reply(tenant_id: int, session_id: str, text: str) -> dict:
    """The WordPress plugin's /chat call while the AI is off: keep the
    message for the team and answer politely. handoff_triggered makes the
    plugin show its own name / phone / email form (it does so once)."""
    saved = save_visitor_message(tenant_id, session_id, text)
    c = saved["contact"]
    if c["email"] or c["phone"]:
        reply = "Thanks for your message. Our team will get back to you soon."
    else:
        reply = ("Thanks for your message. Our team will get back to you soon. "
                 "Please leave your email address or phone number so we can reach you.")
    return {"reply": reply, "session_id": session_id, "product_recommendations": [],
            "handoff_triggered": not (c["email"] or c["phone"]), "team_chat": True}
