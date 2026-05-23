"""
handoff.py  —  Human handoff detection for the PhiXtra AI chat endpoint.

When the AI includes [HANDOFF REQUESTED] in its reply (triggered by the
system instruction), this module:
  1. Strips the hidden tag from the customer-facing answer
  2. Extracts a WhatsApp/phone number if one was shared in the conversation
  3. Logs a row to the handoff_requests table
  4. Sends an immediate email alert to the store owner

All operations are best-effort — failures are logged but never crash /chat.
"""

import os
import re
import smtplib
from email.message import EmailMessage
from db import get_db_connection

# ── The trigger tag the AI embeds when handoff is needed ─────────────────────
HANDOFF_TAG = "[HANDOFF REQUESTED]"

# Regex to extract phone / WhatsApp numbers from text.
# Matches international and local formats:  +44 7911 123456  /  07911123456  /  +1-800-555-0100
_PHONE_PATTERN = re.compile(
    r"(?<!\d)"                    # not preceded by a digit
    r"(\+?\d[\d\s\-\(\)\.]{7,18}\d)"  # the number itself
    r"(?!\d)",                    # not followed by a digit
    re.ASCII,
)

_PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")
BRAND = "#030C18"


# ── Phone extraction ──────────────────────────────────────────────────────────

def _extract_phone(text: str) -> str:
    """Return the first phone-like number found in text, or empty string."""
    if not text:
        return ""
    m = _PHONE_PATTERN.search(text)
    if m:
        # Normalise: strip spaces and dashes for storage
        raw = m.group(1).strip()
        return raw
    return ""


# ── Email sending (same SMTP config as portal_utils.py) ──────────────────────



# ── DB helpers ────────────────────────────────────────────────────────────────

def _log_handoff_db(
    tenant_id: int,
    session_id: str,
    whatsapp_number: str,
    visitor_message: str,
) -> int | None:
    """Insert a row into handoff_requests. Returns new row id or None on error."""
    try:
        conn = get_db_connection()
        if not conn:
            return None
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO handoff_requests
                (tenant_id, session_id, whatsapp_number, visitor_message, status)
            VALUES (%s, %s, %s, %s, 'pending')
            """,
            (
                tenant_id,
                session_id,
                whatsapp_number or None,
                (visitor_message or "")[:1000],
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
        cur.close()
        conn.close()
        return new_id
    except Exception as e:
        print(f"⚠️ [HANDOFF] DB log failed: {e}")
        return None


def _get_tenant_contact_email(tenant_id: int) -> str:
    """Look up the store owner's email so we know where to send the alert.

    Prefers:
      1. A custom handoff_notify_email if the column exists and is filled in.
      2. The account email of the earliest active customer for this tenant.

    The email_verified gate has been intentionally removed — store owners who
    have not clicked their verification link would otherwise never receive
    handoff alerts at all.
    """
    try:
        conn = get_db_connection()
        if not conn:
            print(f"⚠️ [HANDOFF] DB connection failed — cannot fetch tenant email for tenant_id={tenant_id}")
            return ""
        cur = conn.cursor(dictionary=True)

        # First try: dedicated handoff notification email column (may not exist yet)
        try:
            cur.execute(
                """
                SELECT handoff_notify_email, email FROM customers
                WHERE tenant_id = %s AND is_active = 1
                ORDER BY email_verified DESC, id ASC LIMIT 1
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row:
                result = row.get("handoff_notify_email") or row.get("email") or ""
                cur.close()
                conn.close()
                if result:
                    print(f"✅ [HANDOFF] Sending alert to: {result}")
                else:
                    print(f"⚠️ [HANDOFF] Customer row found but email is empty for tenant_id={tenant_id}")
                return result
        except Exception:
            # handoff_notify_email column doesn't exist yet — fall back to basic query
            try:
                cur.execute(
                    """
                    SELECT email FROM customers
                    WHERE tenant_id = %s AND is_active = 1
                    ORDER BY email_verified DESC, id ASC LIMIT 1
                    """,
                    (tenant_id,),
                )
                row = cur.fetchone()
                cur.close()
                conn.close()
                result = (row or {}).get("email") or ""
                if result:
                    print(f"✅ [HANDOFF] Sending alert to: {result}")
                else:
                    print(f"⚠️ [HANDOFF] No active customer found for tenant_id={tenant_id}")
                return result
            except Exception as inner_e:
                print(f"⚠️ [HANDOFF] Fallback email query failed: {inner_e}")
                return ""

        return ""
    except Exception as e:
        print(f"⚠️ [HANDOFF] Could not fetch tenant email: {e}")
        return ""


def _get_chat_summary(tenant_id: int, session_id: str) -> str:
    """Fetch any stored AI summary for this session (from chat_summaries table)."""
    try:
        conn = get_db_connection()
        if not conn:
            return ""
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT summary_text FROM chat_summaries WHERE session_id=%s AND tenant_id=%s",
            (session_id, tenant_id),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return (row or {}).get("summary_text") or ""
    except Exception:
        return ""


# ── Main public function called from main.py ──────────────────────────────────

def build_handoff_instruction(tenant_id: int) -> str:
    """
    Reads the active handoff rules for this tenant from the DB and returns
    a ready-to-append system prompt instruction block.

    Returns an empty string if there are no active rules — in that case
    main.py should not append anything to the system prompt.

    This function never raises.
    """
    try:
        conn = get_db_connection()
        if not conn:
            return ""
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT trigger_text, trigger_type
            FROM handoff_rules
            WHERE tenant_id = %s AND is_active = 1
            ORDER BY sort_order ASC, id ASC
        """, (tenant_id,))
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ [HANDOFF] build_handoff_instruction DB error: {e}")
        return ""

    if not rows:
        return ""

    visitor_rules = [r["trigger_text"] for r in rows if r["trigger_type"] == "visitor_initiated"]
    ai_rules      = [r["trigger_text"] for r in rows if r["trigger_type"] == "ai_initiated"]

    lines = [
        "",
        "",
        "[HANDOFF RULES — READ CAREFULLY]",
        f"The hidden tag {HANDOFF_TAG} signals that this visitor needs a human team member.",
        "When you include it, ALSO warmly tell the visitor that a team member will be in touch",
        "shortly. Never show the tag text itself to the visitor — it is stripped automatically.",
        "Place " + HANDOFF_TAG + " on the very last line of your reply, after your message to the visitor.",
        "",
        "Trigger a handoff (add " + HANDOFF_TAG + " to your reply) when ANY of the following occur:",
    ]

    if visitor_rules:
        lines.append("")
        lines.append("VISITOR SAYS OR ASKS (visitor-initiated):")
        for rule in visitor_rules:
            lines.append(f"- {rule}")

    if ai_rules:
        lines.append("")
        lines.append("YOU DECIDE (AI-initiated — trigger even if visitor has not asked for a human):")
        for rule in ai_rules:
            lines.append(f"- {rule}")

    lines += [
        "",
        "Important: only trigger a handoff when one of the above situations is clearly present.",
        "Do not trigger it for general product questions you can answer yourself.",
    ]

    return "\n".join(lines)


def detect_and_process_handoff(
    answer: str,
    user_message: str,
    tenant_id: int,
    session_id: str,
    store_domain: str = "",
) -> tuple:
    """
    Checks if the AI included [HANDOFF REQUESTED] in its reply.

    If yes:
      - Strips the tag from the answer (visitor never sees it)
      - Extracts a phone number if one was already mentioned in the conversation
      - Logs to handoff_requests table

    NO email is sent here. The single alert email is sent later by
    update_handoff_contact() — once the visitor either submits their
    contact details or clicks Skip on the in-widget form. This guarantees
    staff receive exactly ONE email, containing everything available.

    Returns (clean_answer_str, handoff_triggered_bool).
    This function never raises.
    """
    if HANDOFF_TAG not in answer:
        print(f"ℹ️ [HANDOFF] Tag not present in AI reply for session={session_id} — no handoff triggered (this is normal for non-handoff messages)")
        return answer, False

    print(f"🙋 [HANDOFF] Triggered for tenant_id={tenant_id} session={session_id}")

    # 1. Strip the hidden tag from the reply the visitor sees
    clean_answer = answer.replace(HANDOFF_TAG, "").strip()

    # 2. Try to extract a phone number already mentioned in the conversation
    whatsapp_number = _extract_phone(user_message) or _extract_phone(clean_answer)

    # 3. Log to DB — email will be sent when the contact form is submitted or skipped
    _log_handoff_db(
        tenant_id=tenant_id,
        session_id=session_id,
        whatsapp_number=whatsapp_number,
        visitor_message=(user_message or "")[:500],
    )

    print(f"✅ [HANDOFF] Logged to DB for session={session_id} — waiting for contact form")
    return clean_answer, True


# ── Contact capture ─────────────────────────────────────────────────────────
# Called when the visitor submits the in-widget contact form after a handoff.

def update_handoff_contact(
    tenant_id: int,
    session_id: str,
    visitor_name: str,
    visitor_phone: str,
    visitor_email: str,
    store_domain: str = "",
) -> bool:
    """
    Called when the visitor submits OR skips the in-widget contact form.

    1. Updates the handoff_requests row with whatever contact details were provided
       (all fields may be empty if the visitor clicked Skip — that is fine).
    2. Sends the ONE alert email to the store owner, containing:
       - The visitor's original message (from the DB)
       - Their name, mobile and email (if provided)
       - A WhatsApp button (if a phone number is available)
       - A link to the full conversation

    This is the only place an email is sent — detect_and_process_handoff
    deliberately does NOT send one, so staff always receive exactly one email.

    Returns True if the DB update succeeded. Never raises.
    """
    # ── 1. Fetch the existing handoff row so we have the original visitor message
    #       and any phone number already extracted from the conversation
    original_message  = ""
    existing_phone    = ""
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT visitor_message, whatsapp_number
                FROM handoff_requests
                WHERE session_id = %s AND tenant_id = %s AND status = 'pending'
                ORDER BY id DESC LIMIT 1
                """,
                (session_id, tenant_id),
            )
            row = cur.fetchone() or {}
            original_message = (row.get("visitor_message") or "")[:300]
            existing_phone   = row.get("whatsapp_number") or ""
            cur.close()
            conn.close()
    except Exception as e:
        print(f"⚠️ [HANDOFF] Could not fetch original handoff row: {e}")

    # Use the form phone if provided, otherwise keep whatever was already in the DB
    final_phone = (visitor_phone or "").strip() or existing_phone

    # ── 2. Update the DB row with the contact details from the form
    updated = False
    try:
        conn = get_db_connection()
        if not conn:
            print(f"⚠️ [HANDOFF] update_handoff_contact: no DB connection")
        else:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    UPDATE handoff_requests
                    SET visitor_name    = %s,
                        visitor_email   = %s,
                        whatsapp_number = COALESCE(NULLIF(%s,''), whatsapp_number)
                    WHERE session_id = %s AND tenant_id = %s AND status = 'pending'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        (visitor_name  or "")[:200],
                        (visitor_email or "")[:254],
                        final_phone,
                        session_id,
                        tenant_id,
                    ),
                )
            except Exception:
                # visitor_name / visitor_email columns not yet migrated — update phone only
                cur.execute(
                    """
                    UPDATE handoff_requests
                    SET whatsapp_number = COALESCE(NULLIF(%s,''), whatsapp_number)
                    WHERE session_id = %s AND tenant_id = %s AND status = 'pending'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        final_phone,
                        session_id,
                        tenant_id,
                    ),
                )
            conn.commit()
            updated = cur.rowcount > 0
            cur.close()
            conn.close()
            print(f"✅ [HANDOFF] Contact details saved for session={session_id}")
    except Exception as e:
        print(f"⚠️ [HANDOFF] update_handoff_contact DB error: {e}")

    # ── 3. Send the ONE alert email with everything we know
    contact_email = _get_tenant_contact_email(tenant_id)
    if contact_email:
        chat_summary = _get_chat_summary(tenant_id, session_id)
        _send_handoff_email_full(
            to_email=contact_email,
            store_domain=store_domain or str(tenant_id),
            session_id=session_id,
            visitor_name=visitor_name,
            visitor_phone=final_phone,
            visitor_email=visitor_email,
            visitor_message=original_message,
            chat_summary=chat_summary[:400] if chat_summary else "",
        )
    else:
        print(f"⚠️ [HANDOFF] No contact email found for tenant_id={tenant_id} — email skipped")

    return updated


def _send_handoff_email_full(
    to_email: str,
    store_domain: str,
    session_id: str,
    visitor_name: str,
    visitor_phone: str,
    visitor_email: str,
    visitor_message: str,
    chat_summary: str,
) -> bool:
    """
    Sends the single handoff alert email to the store owner.
    Contains the visitor's contact details (name, phone, email) and their message.
    """
    host       = os.getenv("SMTP_HOST",     "").strip()
    port       = int(os.getenv("SMTP_PORT", "587"))
    user       = os.getenv("SMTP_USER",     "").strip()
    password   = os.getenv("SMTP_PASSWORD", "").strip()
    from_email = os.getenv("SMTP_FROM",     user or "no-reply@phixtra.com").strip()
    use_tls    = os.getenv("SMTP_USE_TLS",  "1").strip() == "1"
    use_ssl    = os.getenv("SMTP_USE_SSL",  "0").strip() == "1"

    if not host or not from_email or not to_email:
        print("⚠️ [HANDOFF] SMTP not configured or no recipient — skipping email")
        return False

    archive_link = f"{_PORTAL_BASE_URL}/chat-archive?open={session_id}"

    # WhatsApp button
    wa_btn = ""
    wa_display = visitor_phone or "Not provided"
    if visitor_phone:
        digits = re.sub(r"[^\d+]", "", visitor_phone)
        wa_url = f"https://wa.me/{digits.lstrip('+')}"
        wa_btn = (
            f'<p style="margin:16px 0 0">'
            f'<a href="{wa_url}" style="background:#25D366;color:#fff;padding:10px 20px;'
            f'border-radius:10px;text-decoration:none;font-weight:700;display:inline-block">'
            f'💬 Open WhatsApp Chat</a></p>'
        )

    # Summary block
    summary_block = ""
    if chat_summary:
        summary_block = (
            f'<div style="background:#f3f4f6;border-radius:10px;padding:14px 16px;margin-top:14px">'
            f'<strong style="color:#555;font-size:12px;text-transform:uppercase;letter-spacing:.05em">'
            f'Conversation Summary</strong>'
            f'<p style="margin:8px 0 0;color:#374151;font-size:14px">{chat_summary}</p>'
            f'</div>'
        )

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px">
      <h2 style="color:{BRAND};margin:0 0 4px">🙋 Visitor Requesting Human Help</h2>
      <p style="color:#888;font-size:13px;margin:0 0 20px">{store_domain}</p>

      <table style="border-collapse:collapse;width:100%;margin-bottom:16px">
        <tr>
          <td style="padding:8px 12px;font-weight:700;background:#f3f4f6;border:1px solid #e5e7eb;width:140px;font-size:13px">Name</td>
          <td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:14px;font-weight:700;color:{BRAND}">{visitor_name or "Not provided"}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;font-weight:700;background:#f3f4f6;border:1px solid #e5e7eb;font-size:13px">Mobile / WhatsApp</td>
          <td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:14px;font-weight:700;color:{BRAND}">{wa_display}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;font-weight:700;background:#f3f4f6;border:1px solid #e5e7eb;font-size:13px">Email Address</td>
          <td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:14px;font-weight:700;color:{BRAND}">{visitor_email or "Not provided"}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;font-weight:700;background:#f3f4f6;border:1px solid #e5e7eb;font-size:13px">Their Message</td>
          <td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:14px">{visitor_message or "—"}</td>
        </tr>
      </table>

      {summary_block}
      {wa_btn}

      <p style="margin:18px 0 0">
        <a href="{archive_link}"
           style="background:{BRAND};color:#fff;padding:10px 20px;border-radius:10px;
                  text-decoration:none;font-weight:700;display:inline-block">
          📋 Read Full Conversation
        </a>
      </p>

      <p style="color:#aaa;font-size:12px;margin-top:22px">
        This alert was sent automatically by PhiXtra. Mark it as handled in your portal dashboard.
      </p>
    </div>"""

    text = (
        f"VISITOR REQUESTING HUMAN HELP — {store_domain}\n\n"
        f"Name:    {visitor_name   or 'Not provided'}\n"
        f"Mobile:  {wa_display}\n"
        f"Email:   {visitor_email  or 'Not provided'}\n"
        f"Message: {visitor_message or '—'}\n\n"
        f"Read the full conversation: {archive_link}\n"
    )
    if visitor_phone:
        digits = re.sub(r"[^\d+]", "", visitor_phone)
        text += f"Open WhatsApp: https://wa.me/{digits.lstrip('+')}\n"

    msg            = EmailMessage()
    msg["From"]    = from_email
    msg["To"]      = to_email
    msg["Subject"] = f"🙋 Visitor wants human help — {store_domain}"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        if use_ssl:
            import ssl as _ssl
            ctx = _ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as server:
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                if use_tls:
                    server.starttls()
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        print(f"✅ [HANDOFF] Alert email sent to {to_email}")
        return True
    except Exception as e:
        print(f"⚠️ [HANDOFF] Email send failed: {e}")
        return False


