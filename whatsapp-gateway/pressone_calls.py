"""
pressone_calls.py — receives PressOne (Nigerian business phone system) call
events and logs them onto the matching CRM contact (2026-09-12, CRM angle
only, bring-your-own-account — see project_phixtra_pressone_integration
memory for the full design and the confirmed real payload samples this is
built from).

Only two events are actually stored: call.completed and call.missed (both
terminal — call.started carries nothing yet worth showing on a timeline,
and voicemail.received isn't wired up in this first pass, flagged as a
fast-follow). recording.ready updates that same row with a recording link
once PressOne generates one.

A genuinely new (not a webhook retry) inbound call.missed also triggers an
automatic WhatsApp reply to the caller, IF: the business has WhatsApp
connected here too, has left the reply switched on (default on,
pressone_accounts.auto_reply_missed_calls), and has an approved Meta
template named phixtra_missed_call (or their own override in wa_templates)
— see template_sender.py's DEFAULT_TEMPLATES for the exact wording to
submit. Reuses the same send_template()/log_proactive() machinery
wa_proactive.py already uses for cart-recovery and order-update messages,
so this is one more entry in an existing pattern, not new machinery.

Signature verification follows PressOne's documented scheme exactly:
HMAC-SHA256(secret, f"{timestamp}.{raw_body}"), headers X-Webhook-Event /
X-Webhook-Timestamp / X-Webhook-Signature (lowercase hex, no prefix),
reject anything older than 5 minutes — confirmed against
https://api-docs.pressone.africa/webhooks 2026-09-12.

This has its own route (not multiplexed through meta_webhook.py's single
Meta endpoint, since this isn't a Meta webhook at all) — see main.py's
include of this router.
"""
import hashlib
import hmac
import json
import time

import psycopg2.extras
from fastapi import APIRouter, Request, Response
from wa_db import get_db_connection, get_wa_template, log_proactive
from tenant_router import get_wa_tenant_by_tenant_id
from template_sender import send_template, DEFAULT_TEMPLATES

router = APIRouter()

_MAX_AGE_SECONDS = 300


def _verify_signature(raw_body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs(time.time() - float(timestamp)) > _MAX_AGE_SECONDS:
            return False
    except ValueError:
        return False
    message = (timestamp + ".").encode("utf-8") + raw_body
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _get_tenant_for_account(account_id: str):
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT tenant_id, webhook_secret FROM pressone_accounts WHERE account_id=%s AND active=TRUE",
            (account_id,),
        )
        return cur.fetchone()
    except Exception as e:
        print("⚠️ pressone _get_tenant_for_account error:", e)
        return None
    finally:
        cur.close()
        conn.close()


def _digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _find_or_create_contact(cur, tenant_id: int, phone: str):
    """Match an existing wa_contacts row by phone (digits-only compare —
    same approach _find_matching_pipeline_lead in portal_routes.py already
    uses), or create a bare new one so the call has somewhere to attach.
    That's the actual CRM value here: a call from a brand-new number still
    shows up as a Contact, not a call that vanishes into nothing."""
    digits = _digits(phone)
    if not digits:
        return None
    cur.execute(
        """SELECT id FROM wa_contacts
           WHERE tenant_id=%s AND regexp_replace(phone, '[^0-9]', '', 'g') = %s
           LIMIT 1""",
        (tenant_id, digits),
    )
    row = cur.fetchone()
    if row:
        return row["id"]
    try:
        cur.execute(
            """INSERT INTO wa_contacts (tenant_id, phone, display_name)
               VALUES (%s, %s, %s)
               ON CONFLICT (tenant_id, phone) DO UPDATE SET phone = EXCLUDED.phone
               RETURNING id""",
            (tenant_id, phone, phone),
        )
        row = cur.fetchone()
        return row["id"] if row else None
    except Exception as e:
        print("⚠️ pressone _find_or_create_contact insert error:", e)
        return None


def _upsert_call(tenant_id, contact_id, payload, event) -> bool:
    """Returns True only if this call_id was genuinely new — never seen
    before — so the caller can gate the missed-call auto-reply on the
    first delivery only. PressOne (like most webhook senders) can retry a
    delivery; without this, a retry would re-send the WhatsApp message.
    `xmax = 0` is the standard Postgres tell for "this row was INSERTed",
    false when the ON CONFLICT UPDATE branch fired instead."""
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO pressone_calls
              (tenant_id, contact_id, call_id, call_session_id, event, direction,
               caller_number, callee_number, status, duration_seconds, end_reason,
               raw_payload, started_at, ended_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (call_id) DO UPDATE SET
              contact_id = COALESCE(EXCLUDED.contact_id, pressone_calls.contact_id),
              event = EXCLUDED.event, direction = EXCLUDED.direction,
              status = EXCLUDED.status, duration_seconds = EXCLUDED.duration_seconds,
              end_reason = EXCLUDED.end_reason,
              raw_payload = EXCLUDED.raw_payload, ended_at = EXCLUDED.ended_at
            RETURNING (xmax = 0) AS inserted
            """,
            (
                tenant_id, contact_id, payload.get("callId"), payload.get("callSessionId"), event,
                payload.get("direction"), payload.get("callerNumber"), payload.get("calleeNumber"),
                payload.get("status"), payload.get("durationSeconds") or 0, payload.get("endReason"),
                json.dumps(payload), payload.get("startedAt"), payload.get("endedAt"),
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return bool(row and row[0])
    except Exception as e:
        print("⚠️ pressone _upsert_call error:", e)
        return False
    finally:
        cur.close()
        conn.close()


def _auto_reply_enabled(tenant_id: int) -> bool:
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT auto_reply_missed_calls FROM pressone_accounts WHERE tenant_id=%s AND active=TRUE",
            (tenant_id,),
        )
        row = cur.fetchone()
        return bool(row and row[0])
    except Exception as e:
        print("⚠️ pressone _auto_reply_enabled error:", e)
        return False
    finally:
        cur.close()
        conn.close()


def _get_business_name(tenant_id: int) -> str:
    conn = get_db_connection()
    if not conn:
        return "us"
    cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM tenants WHERE id=%s", (tenant_id,))
        row = cur.fetchone()
        return (row[0] if row and row[0] else "us")
    except Exception as e:
        print("⚠️ pressone _get_business_name error:", e)
        return "us"
    finally:
        cur.close()
        conn.close()


async def _send_missed_call_reply(tenant_id: int, customer_number: str):
    """The actual feature: a WhatsApp message back to whoever's call was
    just missed. Silently does nothing (logged, not raised) whenever a
    precondition isn't met — no WhatsApp connected, switched off, or no
    approved template yet — since a missed call is still logged and shown
    either way; this is a bonus on top, not something that should ever
    break call logging if it can't fire."""
    if not customer_number:
        return
    if not _auto_reply_enabled(tenant_id):
        return

    wa = get_wa_tenant_by_tenant_id(tenant_id)
    if not wa or not wa.get("phone_number_id") or not wa.get("access_token"):
        print(f"⚠️ [PRESSONE] tenant={tenant_id} has no WhatsApp connected — skipping missed-call reply")
        return

    tpl = get_wa_template(tenant_id, "missed_call")
    template_name = tpl["template_name"] if tpl else DEFAULT_TEMPLATES["missed_call"]
    language_code = tpl["language_code"] if tpl else "en"

    business_name = _get_business_name(tenant_id)
    to = customer_number.strip().lstrip("+")

    ok = await send_template(
        phone_number_id=wa["phone_number_id"],
        access_token=wa["access_token"],
        to=to,
        template_name=template_name,
        language_code=language_code,
        body_params=[business_name],
    )
    log_proactive(
        tenant_id=tenant_id,
        phone_number_id=wa["phone_number_id"],
        customer_phone=to,
        event_type="pressone_missed_call",
        template_name=template_name,
        status="sent" if ok else "failed",
        notes="auto-reply to a PressOne missed call",
    )
    print(f"{'✅' if ok else '⚠️'} [PRESSONE] missed-call WhatsApp reply tenant={tenant_id} to={to} ok={ok}")


def _find_recording_url(payload: dict):
    """Best-effort: the exact field name on recording.ready wasn't
    confirmed against a real sample (see project_phixtra_pressone_integration
    memory) — look for any top-level string value that looks like a URL
    rather than hardcode a guessed key name. Revisit once a real
    recording.ready delivery has been seen."""
    for v in payload.values():
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


@router.post("/pressone-webhook")
async def pressone_webhook(request: Request):
    raw_body = await request.body()
    timestamp = request.headers.get("x-webhook-timestamp", "")
    signature = request.headers.get("x-webhook-signature", "")
    event_header = request.headers.get("x-webhook-event", "")

    try:
        payload = json.loads(raw_body or b"{}")
    except Exception:
        # Always 200 — a malformed body is nothing PressOne should retry.
        return Response(status_code=200)

    account_id = payload.get("accountId", "")
    event = payload.get("event") or event_header

    acct = _get_tenant_for_account(account_id) if account_id else None
    if not acct:
        print(f"⚠️ [PRESSONE] webhook for unknown account={account_id!r} — ignored")
        return Response(status_code=200)

    if not _verify_signature(raw_body, timestamp, signature, acct.get("webhook_secret") or ""):
        print(f"⚠️ [PRESSONE] signature mismatch for account={account_id!r} — ignored")
        return Response(status_code=200)

    tenant_id = acct["tenant_id"]

    if event in ("call.completed", "call.missed"):
        contact_id = None
        conn = get_db_connection()
        if conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                # Whichever side is the customer: on an inbound call the
                # caller is the customer; on outbound, the callee is —
                # the other number on an inbound call is the business's
                # own PressOne number, not a contact to create.
                customer_number = (
                    payload.get("callerNumber") if payload.get("direction") == "inbound"
                    else payload.get("calleeNumber")
                )
                contact_id = _find_or_create_contact(cur, tenant_id, customer_number)
                conn.commit()
            except Exception as e:
                print("⚠️ pressone contact-match error:", e)
            finally:
                cur.close()
                conn.close()
        is_new = _upsert_call(tenant_id, contact_id, payload, event)
        print(f"📞 [PRESSONE] tenant={tenant_id} {event} "
              f"{payload.get('callerNumber')}→{payload.get('calleeNumber')} "
              f"({payload.get('durationSeconds')}s)")

        # Missed-call auto-reply: only for a genuinely new inbound miss —
        # an outbound "missed" (the business called the customer, who
        # didn't pick up) isn't what was asked for, and `is_new` stops a
        # webhook retry from sending the WhatsApp message twice.
        if event == "call.missed" and payload.get("direction") == "inbound" and is_new:
            await _send_missed_call_reply(tenant_id, payload.get("callerNumber"))

    elif event == "recording.ready":
        url = _find_recording_url(payload)
        call_id = payload.get("callId")
        if url and call_id:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                try:
                    cur.execute(
                        "UPDATE pressone_calls SET recording_url=%s WHERE call_id=%s AND tenant_id=%s",
                        (url, call_id, tenant_id),
                    )
                    conn.commit()
                except Exception as e:
                    print("⚠️ pressone recording update error:", e)
                finally:
                    cur.close()
                    conn.close()

    return Response(status_code=200)
