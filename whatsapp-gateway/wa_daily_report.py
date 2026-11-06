"""
wa_daily_report.py — Daily merchant summary sent via WhatsApp at 08:00 WAT.

Scheduler: AsyncIOScheduler fires run_daily_reports() every hour (2026-11-06);
each business gets its report at 08:00 in ITS OWN time zone (Settings time
zone, else its business country, else London — same rule as the portal's
staff_activity.tenant_tz_name), covering yesterday in that time zone.
Manual trigger: POST /wa-daily-report  (protected by PHIXTRA_INTERNAL_TOKEN)

Report covers the calendar day that just ended (yesterday in WAT / UTC+1).
Sent FROM the tenant's own WhatsApp Business number TO the merchant's
personal phone (tenants.report_phone or customers.phone_number).

Delivery strategy (in order of preference):
  1. Template  — uses the 'phixtra_daily_report' Meta-approved template if the
                 tenant has configured it in wa_templates.
  2. Plain text — works within the 24-hour customer-service window.  Active
                 merchants who messaged their own WA number recently will receive
                 it.  Others get a wa_proactive_log entry with status='failed'.

Meta template expected (utility category, no header):
  Body (6 params):
    {{1}} tenant / business name
    {{2}} report date  (e.g. "23 May 2026")
    {{3}} orders count
    {{4}} revenue      (₦ formatted)
    {{5}} conversations
    {{6}} handoffs
"""

import asyncio
import os
from datetime import date, timedelta, datetime

from fastapi import APIRouter, Header, HTTPException
import httpx

import psycopg2
import psycopg2.extras

from wa_db import get_db_connection, log_proactive
import selling_currency as _sc
from meta_sender import send_text

router = APIRouter()

_GRAPH_BASE          = "https://graph.facebook.com/v19.0"
_INTERNAL_TOKEN      = os.getenv("PHIXTRA_INTERNAL_TOKEN", "")
_LOW_STOCK_THRESHOLD = 5   # items at or below this qty are flagged (< 999 = not unlimited)


# Every real Meta access token starts with "EAA". Demo shops, the presale
# sandbox and tutorial accounts are seeded with placeholders
# ("DEMO_ACCESS_TOKEN_PLACEHOLDER", "tutorial-test-token") that Meta always
# rejects — they're skipped for template submission and daily reports
# (2026-10-02; they were retried, and rejected, on every gateway restart).
def _is_real_meta_token(access_token) -> bool:
    return bool(access_token) and str(access_token).startswith("EAA")


# ─── DB queries ───────────────────────────────────────────────────────────────

def _get_active_wa_tenants() -> list[dict]:
    """All WA tenants where daily_report_enabled = 1."""
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # One report per BUSINESS (2026-11-06): a business with several
        # WhatsApp numbers used to get the same report once per number, and
        # the "already sent today" guard read a list loaded before sending.
        # DISTINCT ON keeps the business's first connected number as sender.
        cur.execute("""
            SELECT DISTINCT ON (wt.tenant_id)
                wt.tenant_id,
                wt.phone_number_id,
                wt.access_token,
                t.name              AS tenant_name,
                t.report_phone,
                t.last_report_sent_at
            FROM wa_tenants wt
            JOIN tenants t ON t.id = wt.tenant_id
            WHERE wt.active = TRUE
              AND wt.access_token LIKE 'EAA%%'
              AND t.daily_report_enabled = TRUE
              AND t.status != 'cancelled'
            ORDER BY wt.tenant_id, wt.id ASC
        """)
        return cur.fetchall() or []
    except Exception as e:
        print("⚠️ [DAILY] _get_active_wa_tenants error:", e)
        return []
    finally:
        cur.close()
        conn.close()


def _get_merchant_phone(tenant_id: int, report_phone_override: str | None) -> str | None:
    """
    Return the phone to send the report TO.
    Priority: tenants.report_phone → customers.phone_number.
    """
    if report_phone_override:
        return report_phone_override.strip()
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT phone_number FROM customers
            WHERE tenant_id = %s AND is_active = TRUE AND phone_number IS NOT NULL
            ORDER BY id ASC LIMIT 1
        """, (tenant_id,))
        row = cur.fetchone()
        return row["phone_number"].strip() if row and row.get("phone_number") else None
    except Exception as e:
        print("⚠️ [DAILY] _get_merchant_phone error:", e)
        return None
    finally:
        cur.close()
        conn.close()


_DEFAULT_TZ = "Europe/London"
_country_zone: dict | None = None


def _zone_for_country(cc: str | None) -> str | None:
    global _country_zone
    if _country_zone is None:
        _country_zone = {}
        try:
            with open("/usr/share/zoneinfo/zone.tab") as f:
                for line in f:
                    if line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        _country_zone.setdefault(parts[0].upper(), parts[2].strip())
        except Exception as e:
            print("⚠️ [DAILY] zone.tab error:", e)
    return _country_zone.get((cc or "").strip().upper())


def _tenant_tz_name(tenant_id: int) -> str:
    """The business's own time zone — same rule as the portal
    (staff_activity.tenant_tz_name): Settings time zone ("UTC" = not chosen),
    else the main zone of its business country, else London."""
    from zoneinfo import ZoneInfo
    conn = get_db_connection()
    if not conn:
        return _DEFAULT_TZ
    try:
        cur = conn.cursor()
        cur.execute("""SELECT timezone, business_country FROM customers
                       WHERE tenant_id=%s ORDER BY id LIMIT 1""", (tenant_id,))
        r = cur.fetchone()
        cur.close()
    except Exception:
        r = None
    finally:
        conn.close()
    if r:
        tz, cc = (r[0] or "").strip(), r[1]
        for cand in ((tz if tz and tz.upper() != "UTC" else None), _zone_for_country(cc)):
            if cand:
                try:
                    ZoneInfo(cand)
                    return cand
                except Exception:
                    continue
    return _DEFAULT_TZ


def _get_daily_stats(tenant_id: int, report_date: date, tz_name: str | None = None) -> dict:
    """Pull yesterday's metrics for one tenant. All queries hit the same DB.
    With tz_name, DATE(...) means the day in the business's own time zone."""
    conn = get_db_connection()
    if not conn:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    stats: dict = {}

    try:
        if tz_name:
            cur.execute("SET TIME ZONE %s", (tz_name,))
        dt = report_date.isoformat()   # "YYYY-MM-DD"

        # ── Orders ──────────────────────────────────────────────────────────
        cur.execute("""
            SELECT
                COUNT(*) AS total_orders,
                SUM(CASE WHEN status IN
                    ('PAYMENT_VERIFIED','PROCESSING','DISPATCHED','DELIVERED','COMPLETED')
                    THEN 1 ELSE 0 END) AS paid_orders,
                COALESCE(SUM(CASE WHEN status IN
                    ('PAYMENT_VERIFIED','PROCESSING','DISPATCHED','DELIVERED','COMPLETED')
                    THEN fx_to_selling(total_amount, currency, tenant_id) ELSE 0 END), 0) AS revenue
            FROM orders
            WHERE tenant_id = %s AND DATE(created_at) = %s
        """, (tenant_id, dt))
        row = cur.fetchone() or {}
        stats["total_orders"] = int(row.get("total_orders") or 0)
        stats["paid_orders"]  = int(row.get("paid_orders")  or 0)
        stats["revenue"]      = float(row.get("revenue")    or 0)

        # ── New WhatsApp customers ───────────────────────────────────────────
        # A "new" customer first appeared yesterday (no earlier messages)
        cur.execute("""
            SELECT COUNT(DISTINCT m.customer_phone) AS new_customers
            FROM wa_message_log m
            WHERE m.tenant_id = %s
              AND DATE(m.created_at) = %s
              AND m.direction = 'inbound'
              AND NOT EXISTS (
                  SELECT 1 FROM wa_message_log m2
                  WHERE m2.tenant_id = m.tenant_id
                    AND m2.customer_phone = m.customer_phone
                    AND DATE(m2.created_at) < %s
              )
        """, (tenant_id, dt, dt))
        row = cur.fetchone() or {}
        stats["new_customers"] = int(row.get("new_customers") or 0)

        # ── AI conversations (unique customer phones that messaged) ──────────
        cur.execute("""
            SELECT COUNT(DISTINCT customer_phone) AS conversations
            FROM wa_message_log
            WHERE tenant_id = %s AND DATE(created_at) = %s AND direction = 'inbound'
        """, (tenant_id, dt))
        row = cur.fetchone() or {}
        stats["conversations"] = int(row.get("conversations") or 0)

        # ── Human handoffs ───────────────────────────────────────────────────
        cur.execute("""
            SELECT COUNT(*) AS handoffs
            FROM wa_handoff_state
            WHERE tenant_id = %s AND DATE(escalated_at) = %s
        """, (tenant_id, dt))
        row = cur.fetchone() or {}
        stats["handoffs"] = int(row.get("handoffs") or 0)

        # ── AI token usage ───────────────────────────────────────────────────
        try:
            cur.execute("""
                SELECT COALESCE(SUM(tokens_used), 0) AS tokens
                FROM usage_events
                WHERE tenant_id = %s AND DATE(created_at) = %s
            """, (tenant_id, dt))
            row = cur.fetchone() or {}
            stats["tokens"] = int(row.get("tokens") or 0)
        except Exception:
            conn.rollback()       # 2026-11-06: without this every later query failed
            stats["tokens"] = 0   # table may not exist on some installs

        # ── Website chat (2026-11-06) ────────────────────────────────────────
        try:
            cur.execute("""SELECT 1 FROM api_keys WHERE tenant_id=%s AND key_type IN ('paid','trial')
                             AND is_active = TRUE LIMIT 1""", (tenant_id,))
            stats["has_site"] = cur.fetchone() is not None
            cur.execute("""SELECT COUNT(DISTINCT session_id) AS n FROM chat_messages
                            WHERE tenant_id=%s AND role='user' AND DATE(created_at)=%s""", (tenant_id, dt))
            stats["web_chats"] = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute("""
                SELECT COUNT(DISTINCT s) AS n FROM (
                    SELECT v.session_id AS s FROM chat_visitors v
                     WHERE v.tenant_id=%s
                       AND (COALESCE(v.email,'')<>'' OR COALESCE(v.phone,'')<>'' OR COALESCE(v.name,'')<>'')
                       AND v.session_id IN (SELECT session_id FROM chat_messages
                                             WHERE tenant_id=%s AND role='user' AND DATE(created_at)=%s)
                    UNION
                    SELECT h.session_id FROM handoff_requests h
                     WHERE h.tenant_id=%s AND DATE(h.created_at)=%s
                       AND (COALESCE(h.visitor_email,'')<>'' OR COALESCE(h.visitor_name,'')<>'')
                ) x""", (tenant_id, tenant_id, dt, tenant_id, dt))
            stats["web_details"] = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute("""SELECT COUNT(*) AS n FROM handoff_requests
                            WHERE tenant_id=%s AND DATE(created_at)=%s""", (tenant_id, dt))
            stats["web_handoffs"] = int((cur.fetchone() or {}).get("n") or 0)
        except Exception as e:
            conn.rollback()
            print(f"⚠️ [DAILY] website stats tenant={tenant_id}:", e)
            stats.setdefault("has_site", False)
        # Order lines only for businesses that take orders (any in 90 days).
        try:
            cur.execute("""SELECT 1 FROM orders WHERE tenant_id=%s
                             AND created_at > NOW() - INTERVAL '90 days' LIMIT 1""", (tenant_id,))
            stats["takes_orders"] = cur.fetchone() is not None
        except Exception:
            conn.rollback()
            stats["takes_orders"] = True

        # ── Low-stock products ───────────────────────────────────────────────
        try:
            cur.execute("""
                SELECT name, stock_quantity
                FROM products
                WHERE tenant_id = %s
                  AND is_active = TRUE
                  AND NOT is_service
                  AND stock_quantity <= %s
                  AND stock_quantity < 999
                ORDER BY stock_quantity ASC
                LIMIT 5
            """, (tenant_id, _LOW_STOCK_THRESHOLD))
            stats["low_stock"] = cur.fetchall() or []
        except Exception:
            conn.rollback()
            stats["low_stock"] = []

    except Exception as e:
        print(f"⚠️ [DAILY] _get_daily_stats tenant={tenant_id} error:", e)
    finally:
        cur.close()
        conn.close()

    return stats


def _format_report(tenant_name: str, report_date: date, stats: dict, tenant_id: int = None) -> str:
    """Format the plain-text WhatsApp daily report message."""
    date_str  = report_date.strftime("%-d %B %Y")   # e.g. "23 May 2026"
    revenue   = stats.get("revenue", 0)
    # The business's selling currency (₦ unless it sells in £/$, 2026-10-11)
    _code     = _sc.tenant_currency(tenant_id) if tenant_id else "NGN"
    rev_str   = f"{_sc.currency_label(_code)['prefix']}{revenue:,.0f}"

    lines = [
        f"📊 *Daily Report — {tenant_name}*",
        f"🗓 {date_str}",
        "",
    ]
    # 2026-11-06: orders only for businesses that take orders; website chat
    # for businesses with a website; WhatsApp grouped under its own heading.
    if stats.get("takes_orders", True) or stats.get("total_orders"):
        lines += [
            f"📦 Orders placed:  {stats.get('total_orders', 0)}",
            f"✅ Orders paid:    {stats.get('paid_orders', 0)}",
            f"💰 Revenue:        {rev_str}",
            "",
        ]
    if stats.get("has_site"):
        lines += [
            "🌐 *Website chat*",
            f"   Visitors who chatted:  {stats.get('web_chats', 0)}",
            f"   Left their details:    {stats.get('web_details', 0)}",
            f"   Passed to your team:   {stats.get('web_handoffs', 0)}",
            "",
        ]
    lines += [
        "💬 *WhatsApp*",
        f"   Conversations:         {stats.get('conversations', 0)}",
        f"   New customers:         {stats.get('new_customers', 0)}",
        f"   Passed to your team:   {stats.get('handoffs', 0)}",
    ]

    low = stats.get("low_stock", [])
    if low:
        lines.append("")
        lines.append("⚠️ *Low stock alert:*")
        for item in low:
            qty  = item.get("stock_quantity", 0)
            name = (item.get("name") or "")[:40]
            lines.append(f"  • {name} ({qty} left)")

    lines += [
        "",
        "📲 View dashboard: portal.phixtra.com",
    ]
    return "\n".join(lines)


# ─── Automatic template management ───────────────────────────────────────────
#
# Each tenant has their own WhatsApp Business Account (WABA).  Templates are
# WABA-scoped, so every tenant needs their own approved copy of the daily report
# template.  These helpers handle the full lifecycle automatically:
#
#   ensure_templates_submitted()   — called at startup; submits for any tenant
#                                    that is missing a template record.
#   poll_and_activate_templates()  — called every 2 h by scheduler; detects when
#                                    Meta approves a pending template and flips
#                                    wa_templates.active = TRUE automatically.
#   submit_template_for_tenant()   — called from onboarding when a new tenant
#                                    connects their WhatsApp Business number.

_DAILY_REPORT_TEMPLATE_NAME = "phixtra_daily_report"
_DAILY_REPORT_TEMPLATE_BODY = (
    "📊 Daily Report — {{1}}\n"
    "🗓 {{2}}\n\n"
    "📦 Orders placed: {{3}}\n"
    "💰 Revenue: ₦{{4}}\n"
    "💬 Conversations: {{5}}\n"
    "🤝 Handoffs: {{6}}\n\n"
    "📲 View dashboard: portal.phixtra.com"
)


async def _submit_template_to_meta(waba_id: str, access_token: str) -> bool:
    """POST the phixtra_daily_report template to a tenant's WABA. Returns True on success."""
    url = f"{_GRAPH_BASE}/{waba_id}/message_templates"
    payload = {
        "name": _DAILY_REPORT_TEMPLATE_NAME,
        "language": "en",
        "category": "UTILITY",
        "components": [{
            "type": "BODY",
            "text": _DAILY_REPORT_TEMPLATE_BODY,
            "example": {
                "body_text": [["My Business", "27 June 2026", "3", "45,000", "12", "2"]]
            },
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = r.json()
            if r.status_code in (200, 201) and data.get("id"):
                return True
            if "already exists" in str(data).lower():
                return True
            print(f"⚠️ [TEMPLATE] Meta rejected submission for WABA {waba_id}: {data}")
            return False
    except Exception as e:
        print(f"⚠️ [TEMPLATE] submit error for WABA {waba_id}: {e}")
        return False


def _upsert_template_record(tenant_id: int) -> None:
    """Insert an inactive wa_templates row if one doesn't already exist."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO wa_templates (tenant_id, template_name, language_code, template_type, active)
            VALUES (%s, %s, 'en', 'daily_report', FALSE)
            ON CONFLICT (tenant_id, template_type) DO NOTHING
        """, (tenant_id, _DAILY_REPORT_TEMPLATE_NAME))
        conn.commit()
    except Exception as e:
        print(f"⚠️ [TEMPLATE] DB insert error for tenant {tenant_id}: {e}")
    finally:
        cur.close()
        conn.close()


async def submit_template_for_tenant(tenant_id: int, waba_id: str, access_token: str) -> None:
    """Submit the daily report template to a single tenant's WABA and record it."""
    if not _is_real_meta_token(access_token):
        return
    ok = await _submit_template_to_meta(waba_id, access_token)
    if ok:
        _upsert_template_record(tenant_id)
        print(f"✅ [TEMPLATE] submitted {_DAILY_REPORT_TEMPLATE_NAME} for tenant={tenant_id}")
    else:
        print(f"⚠️ [TEMPLATE] submission failed for tenant={tenant_id}")


async def ensure_templates_submitted() -> None:
    """
    Startup check: find every tenant with daily reporting enabled but no
    template record, and submit the template to their WABA.
    """
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT wt.tenant_id, wt.waba_id, wt.access_token
            FROM wa_tenants wt
            JOIN tenants t ON wt.tenant_id = t.id
            WHERE t.daily_report_enabled = TRUE
              AND wt.waba_id IS NOT NULL
              AND wt.access_token LIKE 'EAA%%'
              AND NOT EXISTS (
                  SELECT 1 FROM wa_templates tmpl
                  WHERE tmpl.tenant_id = wt.tenant_id
                    AND tmpl.template_type = 'daily_report'
              )
        """)
        rows = cur.fetchall() or []
    except Exception as e:
        print(f"⚠️ [TEMPLATE] ensure_templates_submitted query error: {e}")
        rows = []
    finally:
        cur.close()
        conn.close()

    if not rows:
        return

    print(f"🔧 [TEMPLATE] Submitting daily report template for {len(rows)} tenant(s) missing it…")
    for row in rows:
        await submit_template_for_tenant(row["tenant_id"], row["waba_id"], row["access_token"])


async def _check_template_status(waba_id: str, access_token: str, template_name: str) -> str | None:
    """Query Meta for the current approval status of a named template in this WABA."""
    url = f"{_GRAPH_BASE}/{waba_id}/message_templates"
    params = {
        "name": template_name,
        "fields": "name,status",
        "access_token": access_token,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
            data = r.json().get("data", [])
            return data[0]["status"] if data else None
    except Exception as e:
        print(f"⚠️ [TEMPLATE] status check error: {e}")
        return None


def _activate_template_record(tenant_id: int, template_type: str) -> None:
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE wa_templates SET active = TRUE WHERE tenant_id = %s AND template_type = %s",
            (tenant_id, template_type)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        cur.close()
        conn.close()


async def poll_and_activate_templates() -> None:
    """
    Scheduler job (every 2 h): check Meta API for pending templates (any
    template_type — daily_report, handoff_alert, etc.) and activate any that
    have been approved since last check.
    """
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT tmpl.tenant_id, tmpl.template_type, tmpl.template_name,
                   wt.waba_id, wt.access_token
            FROM wa_templates tmpl
            JOIN wa_tenants wt ON tmpl.tenant_id = wt.tenant_id
            WHERE tmpl.active = FALSE
              AND wt.waba_id IS NOT NULL
        """)
        rows = cur.fetchall() or []
    except Exception as e:
        print(f"⚠️ [TEMPLATE] poll query error: {e}")
        rows = []
    finally:
        cur.close()
        conn.close()

    if not rows:
        return

    print(f"🔍 [TEMPLATE] Polling approval for {len(rows)} pending template(s)…")
    for row in rows:
        status = await _check_template_status(row["waba_id"], row["access_token"], row["template_name"])
        tid = row["tenant_id"]
        if status == "APPROVED":
            _activate_template_record(tid, row["template_type"])
            print(f"✅ [TEMPLATE] tenant={tid} {row['template_name']} approved — activated")
        elif status in ("REJECTED", "PAUSED", "DISABLED"):
            print(f"⚠️ [TEMPLATE] tenant={tid} {row['template_name']} → {status}")
        else:
            print(f"   [TEMPLATE] tenant={tid} still {status or 'unknown'}")


def _get_wa_template_for_report(tenant_id: int) -> dict | None:
    """Check if tenant has a phixtra_daily_report template configured."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT template_name, language_code FROM wa_templates
            WHERE tenant_id = %s AND template_type = 'daily_report' AND active = TRUE
            LIMIT 1
        """, (tenant_id,))
        return cur.fetchone()
    except Exception:
        return None
    finally:
        cur.close()
        conn.close()


async def _send_via_template(
    phone_number_id: str,
    access_token: str,
    to: str,
    template_name: str,
    language_code: str,
    tenant_name: str,
    report_date: date,
    stats: dict,
) -> bool:
    """Send via Meta-approved template (6 body params)."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": tenant_name},
                    {"type": "text", "text": report_date.strftime("%-d %B %Y")},
                    {"type": "text", "text": str(stats.get("total_orders", 0))},
                    {"type": "text", "text": f"{stats.get('revenue', 0):,.0f}"},
                    {"type": "text", "text": str(stats.get("conversations", 0))},
                    {"type": "text", "text": str(stats.get("handoffs", 0))},
                ],
            }],
        },
    }
    url     = f"{_GRAPH_BASE}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=headers)
            return r.status_code == 200
    except Exception as e:
        print(f"⚠️ [DAILY] template send error: {e}")
        return False


def _mark_report_sent(tenant_id: int) -> None:
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE tenants SET last_report_sent_at = NOW() WHERE id = %s
        """, (tenant_id,))
        conn.commit()
    except Exception:
        pass
    finally:
        cur.close()
        conn.close()


# ─── Per-tenant pipeline ──────────────────────────────────────────────────────

async def send_daily_report_for_tenant(
    wa_tenant: dict,
    report_date: date | None = None,
    tz_name: str | None = None,
) -> str:
    """
    Gather stats and send the daily report for a single tenant.
    Returns 'sent', 'failed', or 'skipped'.
    """
    if report_date is None:
        # Default: yesterday in WAT (UTC+1).  We subtract 1 day from today UTC.
        report_date = date.today() - timedelta(days=1)

    tenant_id       = int(wa_tenant["tenant_id"])
    phone_number_id = wa_tenant["phone_number_id"]
    access_token    = wa_tenant["access_token"]
    tenant_name     = (wa_tenant.get("tenant_name") or f"Business {tenant_id}").strip()

    # ── Find the recipient phone ──────────────────────────────────────────
    to_phone = _get_merchant_phone(tenant_id, wa_tenant.get("report_phone"))
    if not to_phone:
        print(f"⏭ [DAILY] tenant={tenant_id} skipped — no recipient phone")
        log_proactive(
            tenant_id=tenant_id,
            phone_number_id=phone_number_id,
            customer_phone="",
            event_type="daily_report",
            template_name="",
            status="skipped",
            notes="no recipient phone configured",
        )
        return "skipped"

    # ── Pull stats ────────────────────────────────────────────────────────
    stats = _get_daily_stats(tenant_id, report_date, tz_name)

    # ── Try template first, fall back to plain text ───────────────────────
    tpl = _get_wa_template_for_report(tenant_id)
    ok  = False
    # The approved phixtra_daily_report template has "₦" written into it, so
    # businesses selling in £/$ get the plain-text report (in their own
    # currency) until a template without the sign is approved (2026-10-11).
    if tpl and tpl.get("template_name") == _DAILY_REPORT_TEMPLATE_NAME and not _sc.is_naira(tenant_id):
        tpl = None

    if tpl:
        ok = await _send_via_template(
            phone_number_id=phone_number_id,
            access_token=access_token,
            to=to_phone,
            template_name=tpl["template_name"],
            language_code=tpl.get("language_code", "en"),
            tenant_name=tenant_name,
            report_date=report_date,
            stats=stats,
        )

    if not ok:
        # Plain text (works within the 24-hour customer-service window)
        msg  = _format_report(tenant_name, report_date, stats, tenant_id)
        to_e164 = to_phone.lstrip("+")   # Meta expects E.164 without leading +
        ok = await send_text(phone_number_id, access_token, to_e164, msg)

    status = "sent" if ok else "failed"
    template_used = tpl["template_name"] if (tpl and ok) else ("text" if ok else "")

    log_proactive(
        tenant_id=tenant_id,
        phone_number_id=phone_number_id,
        customer_phone=to_phone,
        event_type="daily_report",
        template_name=template_used,
        status=status,
        notes=f"date={report_date} orders={stats.get('total_orders',0)} revenue={stats.get('revenue',0):.0f} web={stats.get('web_chats',0)}",
    )

    icon = "✅" if ok else "⚠️"
    print(f"{icon} [DAILY] tenant={tenant_id} ({tenant_name}) date={report_date} status={status}")

    if ok:
        _mark_report_sent(tenant_id)

    return status


# ─── Batch runner (called by scheduler) ───────────────────────────────────────

async def run_daily_reports(report_date: date | None = None) -> dict:
    """
    Send daily reports to all eligible tenants.
    Returns a summary dict {"sent": n, "failed": n, "skipped": n}.
    """
    # Scheduled (hourly): only businesses where it's 08:xx now in their own
    # time zone, for yesterday in that zone. Manual run with a date: everyone.
    from zoneinfo import ZoneInfo
    from datetime import timezone as _utc
    manual = report_date is not None
    tenants = _get_active_wa_tenants()

    results = {"sent": 0, "failed": 0, "skipped": 0}
    for tenant in tenants:
        tz_name = _tenant_tz_name(int(tenant["tenant_id"]))
        local_now = datetime.now(_utc.utc).astimezone(ZoneInfo(tz_name))
        if not manual and local_now.hour != 8:
            continue
        rd = report_date or (local_now.date() - timedelta(days=1))
        # Guard: don't double-send if already sent today (business's own day)
        last_sent = tenant.get("last_report_sent_at")
        if last_sent and isinstance(last_sent, datetime):
            if last_sent.astimezone(ZoneInfo(tz_name)).date() >= local_now.date():
                print(f"⏭ [DAILY] tenant={tenant['tenant_id']} already sent today")
                results["skipped"] += 1
                continue

        print(f"🚀 [DAILY] tenant={tenant['tenant_id']} {tz_name} 08:00 → report for {rd}")
        status = await send_daily_report_for_tenant(tenant, rd, tz_name)
        results[status] = results.get(status, 0) + 1
        await asyncio.sleep(0.3)   # gentle Meta API rate throttle

    if any(results.values()):
        print(f"✅ [DAILY] Done — {results}")
    return results


# ─── Manual trigger endpoint ──────────────────────────────────────────────────

@router.post("/wa-daily-report")
async def trigger_daily_report(
    authorization: str = Header(default=""),
    report_date_str: str | None = None,
):
    """
    Manually trigger daily reports (admin / cron / testing).

    Header: Authorization: Bearer {PHIXTRA_INTERNAL_TOKEN}
    Query : ?report_date_str=2026-05-22  (optional; defaults to yesterday)
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not _INTERNAL_TOKEN or token != _INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorised")

    if report_date_str:
        try:
            rd = date.fromisoformat(report_date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid report_date_str, use YYYY-MM-DD")
    else:
        rd = date.today() - timedelta(days=1)   # manual run: everyone, yesterday (as before 2026-11-06)

    results = await run_daily_reports(rd)
    return {"status": "ok", "results": results, "report_date": str(rd or (date.today() - timedelta(days=1)))}
