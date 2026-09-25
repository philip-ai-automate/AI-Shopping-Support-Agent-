"""
staff_alerts.py — instant staff alerts when a chat needs a person (2026-09-25).

When a message arrives that no AI will answer (a new chat on an account with
the AI switched off, or a chat the AI hands over), every active team member
of that account who has "Chat alerts" switched on — and who is allowed to see
that chat — gets a WhatsApp alert on their personal number plus an email,
within seconds. If no staff member has replied 10 minutes later, the ones who
asked for it get a single reminder.

Alert settings are team-member properties (team_members.alert_enabled,
alert_reminder, alert_phone), set on the portal's Team page. If nobody on the
account has alerts on, the account owner's email is alerted instead so no
chat is missed silently.

Every WhatsApp alert is sent from ONE PhiXtra number (env
PHIXTRA_ALERT_SENDER_PHONE_NUMBER_ID) using ONE approved template on that
number (wa_templates types 'staff_chat_alert' / 'staff_chat_reminder'), so no
merchant needs a Meta template of their own. Until the template is approved,
a plain-text message is tried, which Meta only delivers if the staff member
messaged the sender number in the last 24 hours — the email always goes.
"""

import asyncio
import html as _html
import os
import re
import smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

import psycopg2.extras

from wa_db import get_db_connection, get_active_template
from meta_sender import send_text, send_template

NEW_CHAT_WINDOW = timedelta(hours=2)   # one alert per chat per 2 hours
REMINDER_AFTER  = timedelta(minutes=10)
_PORTAL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")
_WAT = timezone(timedelta(hours=1))


def _digits(p) -> str:
    return re.sub(r"\D", "", p or "")


def _sender_id() -> str:
    return (os.getenv("PHIXTRA_ALERT_SENDER_PHONE_NUMBER_ID") or "").strip()


def _support_ids() -> set:
    """PhiXtra's own support number(s): only there do we look the sender up
    as a PhiXtra merchant — never on a merchant's account, where it would
    leak one business's details to another."""
    raw = os.getenv("PHIXTRA_SUPPORT_PHONE_NUMBER_IDS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _ensure_schema(cur) -> None:
    """Idempotent. The portal's migrations create these too; the gateway
    creates them itself so an alert never fails on a fresh start order."""
    for col, typ in (("alert_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
                     ("alert_reminder", "BOOLEAN NOT NULL DEFAULT TRUE"),
                     ("alert_phone", "VARCHAR(32)")):
        cur.execute(f"ALTER TABLE team_members ADD COLUMN IF NOT EXISTS {col} {typ}")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_alerts (
            id               BIGSERIAL PRIMARY KEY,
            tenant_id        INTEGER NOT NULL,
            channel          VARCHAR(20) NOT NULL DEFAULT 'whatsapp',
            chat_key         VARCHAR(128) NOT NULL,
            phone_number_id  VARCHAR(64),
            reason           VARCHAR(20) NOT NULL,
            customer_label   TEXT,
            preview          TEXT,
            recipients       JSONB NOT NULL DEFAULT '[]',
            first_alert_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reminder_sent_at TIMESTAMPTZ,
            replied_at       TIMESTAMPTZ
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS chat_alerts_open_idx ON chat_alerts (tenant_id, channel, chat_key, first_alert_at)")


def init_staff_alert_tables() -> None:
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        _ensure_schema(cur)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"⚠️ [STAFF ALERT] schema error: {e}")
    finally:
        cur.close(); conn.close()


# ── Who is alerted ───────────────────────────────────────────────────────────

def _recipients(cur, tenant_id: int, phone_number_id: str) -> list:
    """Active team members with alerts on who can see chats on this number.
    A number tied to an AI agent is only visible to members assigned that
    agent (same "zero rows = zero access" rule as the Inbox)."""
    cur.execute("SELECT agent_id FROM wa_tenants WHERE tenant_id=%s AND phone_number_id=%s LIMIT 1",
                (tenant_id, phone_number_id))
    row = cur.fetchone()
    agent_id = row["agent_id"] if row else None
    cur.execute("""
        SELECT tm.id, tm.name, tm.email, tm.alert_phone, tm.alert_reminder
          FROM team_members tm
         WHERE tm.tenant_id = %s AND tm.is_active AND tm.alert_enabled
           AND (%s::int IS NULL OR EXISTS (
                 SELECT 1 FROM team_member_agents a
                  WHERE a.team_member_id = tm.id AND a.tenant_agent_id = %s::int))
         ORDER BY tm.id
    """, (tenant_id, agent_id, agent_id))
    return [dict(r, kind="member") for r in cur.fetchall() or []]


def _owner_fallback(cur, tenant_id: int) -> list:
    cur.execute("""
        SELECT COALESCE(c.handoff_notify_email, c.email) AS email,
               COALESCE(NULLIF(c.first_name, ''), 'there') AS name
          FROM customers c
         WHERE c.tenant_id = %s AND c.is_active
         ORDER BY c.id LIMIT 1
    """, (tenant_id,))
    r = cur.fetchone()
    if not r or not r.get("email"):
        return []
    return [{"id": None, "kind": "owner", "name": r["name"], "email": r["email"],
             "alert_phone": None, "alert_reminder": True}]


def _customer_label(cur, phone_number_id: str, customer_phone: str, profile_name: str) -> str:
    if phone_number_id in _support_ids():
        tail = _digits(customer_phone)[-10:]
        if tail:
            cur.execute("""
                SELECT t.name FROM customers c JOIN tenants t ON t.id = c.tenant_id
                 WHERE RIGHT(regexp_replace(COALESCE(c.phone_number, ''), '\\D', '', 'g'), 10) = %s
                 ORDER BY c.id LIMIT 1
            """, (tail,))
            r = cur.fetchone()
            if r and r.get("name"):
                return r["name"]
    return (profile_name or "").strip() or f"+{_digits(customer_phone)}"


# ── Sending ──────────────────────────────────────────────────────────────────

def _send_email(to_email: str, subject: str, heading: str, account: str,
                rows: list, footer: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("SMTP_FROM", user or "no-reply@phixtra.com").strip()
    if not host or not to_email:
        return
    trs = "".join(
        f'<tr><td style="padding:7px 10px;border:1px solid #E5E7EB;background:#F9FAFB;font-weight:700;width:130px;font-size:13px">{_html.escape(k)}</td>'
        f'<td style="padding:7px 10px;border:1px solid #E5E7EB;font-size:13.5px">{_html.escape(v)}</td></tr>'
        for k, v in rows)
    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;color:#101828">
      <div style="background:#030C18;padding:16px 20px;border-radius:12px 12px 0 0">
        <p style="color:#25D366;font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;margin:0 0 4px">{_html.escape(account)}</p>
        <h2 style="color:#fff;margin:0;font-size:19px">{_html.escape(heading)}</h2>
      </div>
      <div style="border:1px solid #E5E7EB;border-top:none;border-radius:0 0 12px 12px;padding:18px 20px">
        <table style="border-collapse:collapse;width:100%;margin:0 0 16px">{trs}</table>
        <a href="{_PORTAL}/inbox" style="display:inline-block;background:#25D366;color:#03240F;padding:11px 18px;border-radius:10px;text-decoration:none;font-weight:800">Open the Inbox and reply</a>
        <p style="color:#667085;font-size:12.5px;margin-top:14px">{_html.escape(footer)}</p>
      </div>
    </div>"""
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to_email, subject
    msg.set_content("\n".join(f"{k}: {v}" for k, v in rows) + f"\n\nReply: {_PORTAL}/inbox\n\n{footer}")
    msg.add_alternative(body, subtype="html")
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    except Exception as e:
        print(f"⚠️ [STAFF ALERT] email to {to_email} failed: {e}")


async def _send_whatsapp(to_phone: str, template_type: str, params: list, text: str) -> bool:
    to = _digits(to_phone)
    sid = _sender_id()
    if not to or not sid:
        return False
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT tenant_id, access_token FROM wa_tenants WHERE phone_number_id=%s AND active LIMIT 1", (sid,))
        s = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not s:
        print(f"⚠️ [STAFF ALERT] alert sender {sid} not connected")
        return False
    tmpl = get_active_template(int(s["tenant_id"]), template_type)
    if tmpl and await send_template(sid, s["access_token"], to, tmpl["template_name"],
                                    tmpl["language_code"], params):
        return True
    return await send_text(sid, s["access_token"], to, text)


async def _deliver(recipients: list, *, account: str, label: str, preview: str,
                   when: str, reminder: bool) -> None:
    preview80 = (preview or "")[:80]
    for r in recipients:
        if reminder and not r.get("alert_reminder"):
            continue
        if reminder:
            text = (f"⏰ *Still waiting: 10 minutes, no reply*\nAccount: *{account}*\n"
                    f"👤 *{label}* is still waiting for a reply.\n\n👉 Reply now: {_PORTAL}/inbox")
            params = [account, label]
            subject = f"⏰ Still waiting for a reply: {label}"
            heading = "A chat has waited 10 minutes"
            footer = "Nobody on your team has replied yet. This is the only reminder for this chat."
        else:
            text = (f"🆘 *New chat needs a reply*\nAccount: *{account}*\n\n👤 From: *{label}*\n"
                    f"💬 \"{preview80}\"\n🕐 {when}\n\n👉 Reply now: {_PORTAL}/inbox")
            params = [account, label, preview80 or "-", when]
            subject = f"🆘 New chat needs a reply: {label}"
            heading = "A chat is waiting for a reply"
            footer = "If nobody replies within 10 minutes, you'll get one reminder."
        if r.get("alert_phone"):
            try:
                await _send_whatsapp(r["alert_phone"],
                                     "staff_chat_reminder" if reminder else "staff_chat_alert",
                                     params, text)
            except Exception as e:
                print(f"⚠️ [STAFF ALERT] WhatsApp to member {r.get('id')} failed: {e}")
        if r.get("email"):
            await asyncio.to_thread(_send_email, r["email"], subject, heading, account,
                                    [("From", label), ("Channel", "WhatsApp"),
                                     ("Message", preview or "-"), ("Received", when)], footer)


# ── Entry points ─────────────────────────────────────────────────────────────

async def alert_chat_needs_reply(tenant_id: int, phone_number_id: str, customer_phone: str,
                                 text: str, profile_name: str = "", reason: str = "new_chat",
                                 allow_owner_fallback: bool = True) -> bool:
    """Alert staff that a WhatsApp chat needs a person. Once per chat per
    NEW_CHAT_WINDOW. Returns True if anyone (member or owner) was alerted —
    callers with their own older fallback (the AI hand-over alert) pass
    allow_owner_fallback=False and use their own path when this is False."""
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        _ensure_schema(cur)
        cur.execute("""
            SELECT 1 FROM chat_alerts
             WHERE tenant_id=%s AND channel='whatsapp' AND chat_key=%s
               AND first_alert_at > NOW() - %s
             LIMIT 1
        """, (tenant_id, _digits(customer_phone), NEW_CHAT_WINDOW))
        if cur.fetchone():
            conn.commit()
            return True
        recips = _recipients(cur, tenant_id, phone_number_id)
        if not recips and allow_owner_fallback:
            recips = _owner_fallback(cur, tenant_id)
        if not recips:
            conn.commit()
            return False
        cur.execute("SELECT name FROM tenants WHERE id=%s", (tenant_id,))
        account = ((cur.fetchone() or {}).get("name") or "Your business").strip()
        label = _customer_label(cur, phone_number_id, customer_phone, profile_name)
        cur.execute("""
            INSERT INTO chat_alerts (tenant_id, channel, chat_key, phone_number_id, reason,
                                     customer_label, preview, recipients)
            VALUES (%s, 'whatsapp', %s, %s, %s, %s, %s, %s)
        """, (tenant_id, _digits(customer_phone), phone_number_id, reason, label,
              (text or "")[:500], psycopg2.extras.Json(recips)))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"⚠️ [STAFF ALERT] tenant={tenant_id} error: {e}")
        return False
    finally:
        cur.close(); conn.close()
    when = datetime.now(_WAT).strftime("%-d %b, %H:%M WAT")
    await _deliver(recips, account=account, label=label, preview=text, when=when, reminder=False)
    print(f"✅ [STAFF ALERT] tenant={tenant_id} chat={_digits(customer_phone)} → {len(recips)} recipient(s) ({reason})")
    return True


async def run_alert_reminders() -> int:
    """Every minute: one reminder for chats alerted 10+ minutes ago that no
    staff member has replied to. A staff reply is any non-AI, non-campaign
    outbound message logged after the alert."""
    conn = get_db_connection()
    if not conn:
        return 0
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    due = []
    try:
        _ensure_schema(cur)
        cur.execute("""
            SELECT a.id, a.tenant_id, a.chat_key, a.customer_label, a.preview, a.recipients,
                   a.first_alert_at, t.name AS account,
                   EXISTS (SELECT 1 FROM wa_message_log m
                            WHERE m.tenant_id = a.tenant_id
                              AND regexp_replace(m.customer_phone, '\\D', '', 'g') = a.chat_key
                              AND m.direction = 'outbound'
                              AND COALESCE(m.is_historical, FALSE) = FALSE
                              AND COALESCE(m.message_type, '') NOT IN ('ai_reply', 'campaign')
                              AND m.created_at > a.first_alert_at) AS replied
              FROM chat_alerts a JOIN tenants t ON t.id = a.tenant_id
             WHERE a.reminder_sent_at IS NULL AND a.replied_at IS NULL
               AND a.first_alert_at <= NOW() - %s
               AND a.first_alert_at > NOW() - %s
        """, (REMINDER_AFTER, NEW_CHAT_WINDOW))
        for a in cur.fetchall() or []:
            if a["replied"]:
                cur.execute("UPDATE chat_alerts SET replied_at=NOW() WHERE id=%s", (a["id"],))
            else:
                cur.execute("UPDATE chat_alerts SET reminder_sent_at=NOW() WHERE id=%s", (a["id"],))
                due.append(a)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"⚠️ [STAFF ALERT] reminder sweep error: {e}")
        return 0
    finally:
        cur.close(); conn.close()
    for a in due:
        await _deliver(a["recipients"] or [], account=(a["account"] or "Your business"),
                       label=a["customer_label"] or f"+{a['chat_key']}", preview=a["preview"] or "",
                       when="", reminder=True)
    if due:
        print(f"✅ [STAFF ALERT] {len(due)} reminder(s) sent")
    return len(due)


def is_staff_alert_reply(phone_number_id: str, customer_phone: str) -> bool:
    """True when a team member replies to an alert on the alert-sender number.
    Those messages must not reach the AI or raise alerts of their own."""
    if not phone_number_id or phone_number_id != _sender_id():
        return False
    tail = _digits(customer_phone)[-10:]
    if not tail:
        return False
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT 1 FROM team_members
             WHERE is_active AND alert_enabled AND alert_phone IS NOT NULL
               AND RIGHT(regexp_replace(alert_phone, '\\D', '', 'g'), 10) = %s
             LIMIT 1
        """, (tail,))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        cur.close(); conn.close()
