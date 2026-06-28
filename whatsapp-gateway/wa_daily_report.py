"""
wa_daily_report.py — Daily merchant summary sent via WhatsApp at 08:00 WAT.

Scheduler: AsyncIOScheduler fires run_daily_reports() at 07:00 UTC every day.
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
from meta_sender import send_text

router = APIRouter()

_GRAPH_BASE          = "https://graph.facebook.com/v19.0"
_INTERNAL_TOKEN      = os.getenv("PHIXTRA_INTERNAL_TOKEN", "")
_LOW_STOCK_THRESHOLD = 5   # items at or below this qty are flagged (< 999 = not unlimited)


# ─── DB queries ───────────────────────────────────────────────────────────────

def _get_active_wa_tenants() -> list[dict]:
    """All WA tenants where daily_report_enabled = 1."""
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT
                wt.tenant_id,
                wt.phone_number_id,
                wt.access_token,
                t.name              AS tenant_name,
                t.report_phone,
                t.last_report_sent_at
            FROM wa_tenants wt
            JOIN tenants t ON t.id = wt.tenant_id
            WHERE wt.active = TRUE
              AND t.daily_report_enabled = TRUE
              AND t.status != 'cancelled'
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


def _get_daily_stats(tenant_id: int, report_date: date) -> dict:
    """Pull yesterday's metrics for one tenant. All queries hit the same DB."""
    conn = get_db_connection()
    if not conn:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    stats: dict = {}

    try:
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
                    THEN total_amount ELSE 0 END), 0) AS revenue
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
            stats["tokens"] = 0   # table may not exist on some installs

        # ── Low-stock products ───────────────────────────────────────────────
        try:
            cur.execute("""
                SELECT name, stock_quantity
                FROM products
                WHERE tenant_id = %s
                  AND is_active = TRUE
                  AND stock_quantity <= %s
                  AND stock_quantity < 999
                ORDER BY stock_quantity ASC
                LIMIT 5
            """, (tenant_id, _LOW_STOCK_THRESHOLD))
            stats["low_stock"] = cur.fetchall() or []
        except Exception:
            stats["low_stock"] = []

    except Exception as e:
        print(f"⚠️ [DAILY] _get_daily_stats tenant={tenant_id} error:", e)
    finally:
        cur.close()
        conn.close()

    return stats


def _format_report(tenant_name: str, report_date: date, stats: dict) -> str:
    """Format the plain-text WhatsApp daily report message."""
    date_str  = report_date.strftime("%-d %B %Y")   # e.g. "23 May 2026"
    revenue   = stats.get("revenue", 0)
    rev_str   = f"₦{revenue:,.0f}"

    lines = [
        f"📊 *Daily Report — {tenant_name}*",
        f"🗓 {date_str}",
        "",
        f"📦 Orders placed:  {stats.get('total_orders', 0)}",
        f"✅ Orders paid:    {stats.get('paid_orders', 0)}",
        f"💰 Revenue:        {rev_str}",
        "",
        f"👥 New customers:  {stats.get('new_customers', 0)}",
        f"💬 Conversations:  {stats.get('conversations', 0)}",
        f"🤝 Handoffs:       {stats.get('handoffs', 0)}",
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
              AND wt.access_token IS NOT NULL
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


async def _check_template_status(waba_id: str, access_token: str) -> str | None:
    """Query Meta for the current approval status of phixtra_daily_report in this WABA."""
    url = f"{_GRAPH_BASE}/{waba_id}/message_templates"
    params = {
        "name": _DAILY_REPORT_TEMPLATE_NAME,
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


def _activate_template_record(tenant_id: int) -> None:
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE wa_templates SET active = TRUE WHERE tenant_id = %s AND template_type = 'daily_report'",
            (tenant_id,)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        cur.close()
        conn.close()


async def poll_and_activate_templates() -> None:
    """
    Scheduler job (every 2 h): check Meta API for pending templates and
    activate any that have been approved since last check.
    """
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT tmpl.tenant_id, wt.waba_id, wt.access_token
            FROM wa_templates tmpl
            JOIN wa_tenants wt ON tmpl.tenant_id = wt.tenant_id
            WHERE tmpl.active = FALSE
              AND tmpl.template_type = 'daily_report'
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
        status = await _check_template_status(row["waba_id"], row["access_token"])
        tid = row["tenant_id"]
        if status == "APPROVED":
            _activate_template_record(tid)
            print(f"✅ [TEMPLATE] tenant={tid} {_DAILY_REPORT_TEMPLATE_NAME} approved — activated")
        elif status in ("REJECTED", "PAUSED", "DISABLED"):
            print(f"⚠️ [TEMPLATE] tenant={tid} {_DAILY_REPORT_TEMPLATE_NAME} → {status}")
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
    tenant_name     = (wa_tenant.get("tenant_name") or f"Merchant {tenant_id}").strip()

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
    stats = _get_daily_stats(tenant_id, report_date)

    # ── Try template first, fall back to plain text ───────────────────────
    tpl = _get_wa_template_for_report(tenant_id)
    ok  = False

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
        msg  = _format_report(tenant_name, report_date, stats)
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
        notes=f"date={report_date} orders={stats.get('total_orders',0)} revenue={stats.get('revenue',0):.0f}",
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
    if report_date is None:
        report_date = date.today() - timedelta(days=1)

    print(f"🚀 [DAILY] Starting daily reports for {report_date}")
    tenants = _get_active_wa_tenants()
    print(f"📋 [DAILY] {len(tenants)} tenant(s) eligible")

    results = {"sent": 0, "failed": 0, "skipped": 0}
    for tenant in tenants:
        # Guard: don't double-send if already sent today
        last_sent = tenant.get("last_report_sent_at")
        if last_sent and isinstance(last_sent, datetime):
            if last_sent.date() >= date.today():
                print(f"⏭ [DAILY] tenant={tenant['tenant_id']} already sent today")
                results["skipped"] += 1
                continue

        status = await send_daily_report_for_tenant(tenant, report_date)
        results[status] = results.get(status, 0) + 1
        await asyncio.sleep(0.3)   # gentle Meta API rate throttle

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
        rd = None   # defaults to yesterday inside run_daily_reports

    results = await run_daily_reports(rd)
    return {"status": "ok", "results": results, "report_date": str(rd or (date.today() - timedelta(days=1)))}
