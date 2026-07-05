"""
estate/handoff.py — Human handoff for PhiXtra Real Estate AI.

Reads re_handoff_rules, checks the model's structured needs_handoff decision
(see estate/llm.py's structured_handoff mode — more reliable than scanning
free text for a hidden tag), logs to re_handoff_requests, and emails the
estate agent.
"""
import os
import sys
import smtplib
import psycopg2.extras
from email.message import EmailMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from db import get_db_connection

_PORTAL_BASE   = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")
BRAND          = "#030C18"


# ── System prompt block ───────────────────────────────────────────────────────

def build_handoff_instruction(tenant_id: int) -> str:
    """Read re_handoff_rules and return a ready-to-append system prompt block."""
    try:
        conn = get_db_connection()
        if not conn:
            return ""
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT trigger_text, trigger_type
            FROM re_handoff_rules
            WHERE tenant_id = %s AND is_active = TRUE
            ORDER BY sort_order ASC, id ASC
        """, (tenant_id,))
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ [ESTATE HANDOFF] build_handoff_instruction error: {e}")
        return ""

    if not rows:
        return ""

    visitor_rules = [r["trigger_text"] for r in rows if r["trigger_type"] == "visitor_initiated"]
    ai_rules      = [r["trigger_text"] for r in rows if r["trigger_type"] == "ai_initiated"]

    lines = [
        "", "",
        "[HANDOFF RULES — READ CAREFULLY]",
        "Your response includes a separate needs_handoff field (true/false). Set it to true",
        "when this buyer needs a human agent — evaluate this on EVERY reply, not just when",
        "asked directly. When you set needs_handoff to true, ALSO warmly tell the buyer in",
        "your reply text that an agent will contact them shortly.",
        "",
        "Set needs_handoff to true when ANY of the following occur:",
    ]
    if visitor_rules:
        lines += ["", "BUYER SAYS OR ASKS (buyer-initiated):"]
        for r in visitor_rules:
            lines.append(f"- {r}")
    if ai_rules:
        lines += ["", "YOU DECIDE (AI-initiated — fire even if you could answer yourself):"]
        for r in ai_rules:
            lines.append(f"- {r}")

    lines += ["", "Do NOT trigger for routine property questions or general browsing."]
    return "\n".join(lines)


# ── Buyer summary for agent alerts ───────────────────────────────────────────

def build_buyer_summary(tenant_id: int, customer_id: int) -> str:
    """Build structured buyer profile text from re_customers for agent email."""
    conn = get_db_connection()
    if not conn:
        return ""
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT name, phone_number, budget_min, budget_max,
                   preferred_area, property_type_pref, transaction_pref,
                   payment_method, urgency, bedrooms_pref, lead_status
            FROM re_customers
            WHERE tenant_id = %s AND id = %s
        """, (tenant_id, customer_id))
        c = cur.fetchone() or {}
        cur.close()
    except Exception:
        return ""
    finally:
        conn.close()

    def _price(v):
        if v is None:
            return "?"
        try:
            p = float(v)
            return f"₦{p / 1_000_000:.1f}M" if p >= 1_000_000 else f"₦{p:,.0f}"
        except Exception:
            return str(v)

    return "\n".join([
        f"Buyer: {c.get('name') or 'Unknown'} ({c.get('phone_number', '')})",
        f"Budget: {_price(c.get('budget_min'))} – {_price(c.get('budget_max'))}",
        f"Area: {c.get('preferred_area') or '?'}",
        f"Looking for: {(c.get('property_type_pref') or '?').replace('_', ' ').title()} — {c.get('transaction_pref') or '?'}",
        f"Bedrooms: {c.get('bedrooms_pref') or '?'}",
        f"Payment: {c.get('payment_method') or '?'}",
        f"Urgency: {c.get('urgency') or '?'}",
        f"Lead status: {c.get('lead_status') or 'new'}",
    ])


# ── DB log ────────────────────────────────────────────────────────────────────

def _log_handoff(tenant_id: int, customer_id: int, action_type: str,
                 listing_id: int | None, buyer_summary: str,
                 visitor_message: str) -> int | None:
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO re_handoff_requests
                (tenant_id, customer_id, trigger_type, action_type,
                 listing_id, buyer_summary, visitor_message, status)
            VALUES (%s, %s, 'ai_initiated', %s, %s, %s, %s, 'pending')
            RETURNING id
        """, (
            tenant_id, customer_id,
            action_type or "general",
            listing_id,
            (buyer_summary or "")[:2000],
            (visitor_message or "")[:1000],
        ))
        conn.commit()
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else None
    except Exception as e:
        print(f"⚠️ [ESTATE HANDOFF] _log_handoff DB error: {e}")
        return None
    finally:
        conn.close()


# ── Email alert ───────────────────────────────────────────────────────────────

def _get_tenant_email(tenant_id: int) -> str:
    conn = get_db_connection()
    if not conn:
        return ""
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Prefer contact_email (patch2); fall back to login email if column missing
        try:
            cur.execute(
                "SELECT COALESCE(NULLIF(contact_email,''), email) AS alert_email FROM re_tenants WHERE id=%s",
                (tenant_id,),
            )
        except Exception:
            cur.execute("SELECT email AS alert_email FROM re_tenants WHERE id=%s", (tenant_id,))
        row = cur.fetchone() or {}
        cur.close()
        return row.get("alert_email") or ""
    except Exception as e:
        print(f"⚠️ [ESTATE HANDOFF] _get_tenant_email error: {e}")
        return ""
    finally:
        conn.close()


def _send_alert_email(
    to_email: str,
    buyer_summary: str,
    visitor_message: str,
    action_type: str,
):
    host      = os.getenv("SMTP_HOST", "").strip()
    port      = int(os.getenv("SMTP_PORT", "587"))
    user      = os.getenv("SMTP_USER", "").strip()
    password  = os.getenv("SMTP_PASSWORD", "").strip()
    from_addr = os.getenv("SMTP_FROM", user or "noreply@phixtra.com").strip()
    use_tls   = os.getenv("SMTP_USE_TLS", "1").strip() == "1"
    use_ssl   = os.getenv("SMTP_USE_SSL", "0").strip() == "1"

    if not host or not to_email:
        print("⚠️ [ESTATE HANDOFF] SMTP not configured — skipping email")
        return

    inbox_link = f"{_PORTAL_BASE}/estate/inbox"
    icons = {"INSPECT": "🏠", "CALLBACK": "📞", "general": "🙋"}
    labels = {
        "INSPECT":  "Inspection Request",
        "CALLBACK": "Callback Request",
        "general":  "Human Agent Requested",
    }
    icon  = icons.get(action_type, "🙋")
    label = labels.get(action_type, "Human Agent Requested")
    subject = f"{icon} {label} — PhiXtra Real Estate"

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px">
      <h2 style="color:{BRAND};margin:0 0 16px">{icon} {label}</h2>

      <div style="background:#f3f4f6;border-radius:10px;padding:14px 16px;margin-bottom:14px">
        <strong style="font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#555">
          Buyer Profile
        </strong>
        <pre style="margin:8px 0 0;color:#1f2937;font-size:13px;white-space:pre-wrap;font-family:inherit">
{buyer_summary}</pre>
      </div>

      <div style="background:#fff7ed;border-radius:10px;padding:14px 16px;
                  margin-bottom:18px;border:1px solid #fed7aa">
        <strong style="font-size:12px;text-transform:uppercase;color:#92400e">Their Message</strong>
        <p style="margin:8px 0 0;color:#1f2937;font-size:14px">{visitor_message or "—"}</p>
      </div>

      <a href="{inbox_link}"
         style="background:{BRAND};color:#fff;padding:11px 22px;border-radius:10px;
                text-decoration:none;font-weight:700;font-size:14px;display:inline-block">
        📋 View in Portal Inbox
      </a>

      <p style="color:#aaa;font-size:12px;margin-top:22px">
        Sent automatically by PhiXtra Real Estate AI.
      </p>
    </div>"""

    text = (
        f"{icon} {label}\n\n"
        f"Buyer Profile:\n{buyer_summary}\n\n"
        f"Their Message: {visitor_message or '—'}\n\n"
        f"View in portal: {inbox_link}"
    )

    msg            = EmailMessage()
    msg["From"]    = from_addr
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        if use_ssl:
            import ssl as _ssl
            ctx = _ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                if use_tls:
                    s.starttls()
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
        print(f"✅ [ESTATE HANDOFF] Alert sent to {to_email}")
    except Exception as e:
        print(f"⚠️ [ESTATE HANDOFF] Email send failed: {e}")


# ── Main public function ──────────────────────────────────────────────────────

def detect_and_process_handoff(
    answer: str,
    needs_handoff: bool,
    user_message: str,
    tenant_id: int,
    customer_id: int,
    action_type: str = "general",
    listing_id: int | None = None,
) -> tuple:
    """
    Given the model's structured needs_handoff decision (see estate/llm.py's
    structured_handoff mode), if true:
    - Build buyer summary from re_customers
    - Log to re_handoff_requests
    - Send email alert to tenant

    Returns (answer, handoff_triggered_bool). Never raises.
    """
    if not needs_handoff:
        return answer, False

    print(f"🙋 [ESTATE HANDOFF] tenant={tenant_id} customer={customer_id} action={action_type}")

    buyer_summary = build_buyer_summary(tenant_id, customer_id)

    _log_handoff(
        tenant_id=tenant_id,
        customer_id=customer_id,
        action_type=action_type,
        listing_id=listing_id,
        buyer_summary=buyer_summary,
        visitor_message=user_message,
    )

    to_email = _get_tenant_email(tenant_id)
    if to_email:
        _send_alert_email(
            to_email=to_email,
            buyer_summary=buyer_summary,
            visitor_message=(user_message or "")[:500],
            action_type=action_type,
        )

    return answer, True
