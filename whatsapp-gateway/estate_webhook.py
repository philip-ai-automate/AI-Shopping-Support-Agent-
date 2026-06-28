"""
estate_webhook.py — WhatsApp message handler for PhiXtra Real Estate.

Called by meta_webhook.py when phone_number_id matches an estate tenant
in re_tenants. Calls /estate-chat on the AI backend and dispatches replies.
"""
import asyncio
import hashlib
import hmac
import os

import httpx
import psycopg2
import psycopg2.extras

from wa_db import get_db_connection
from meta_sender import send_text, mark_as_read
from estate_formatter import (
    send_estate_property_card,
    send_inspection_slot_list,
    send_slot_text_fallback,
)

_AI_BACKEND_URL = os.getenv("AI_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
_APP_SECRET     = os.getenv("META_APP_SECRET", "")


# ── Tenant lookup ─────────────────────────────────────────────────────────────

def get_estate_tenant_by_phone_number_id(phone_number_id: str) -> dict | None:
    """
    Return estate tenant dict including first active api_key_plain,
    or None if not found / no active key.
    """
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT
                t.id,
                t.business_name,
                t.wa_phone_number_id,
                t.wa_access_token,
                t.wa_waba_id,
                t.wa_app_secret,
                k.api_key_plain          AS wa_api_key
            FROM re_tenants t
            LEFT JOIN re_api_keys k
                ON k.tenant_id = t.id AND k.is_active = TRUE
            WHERE t.wa_phone_number_id = %s
              AND t.status = 'active'
            ORDER BY k.id ASC
            LIMIT 1
        """, (phone_number_id,))
        return cur.fetchone()
    except Exception as e:
        print(f"⚠️ [ESTATE WA] get_estate_tenant_by_phone_number_id error: {e}")
        return None
    finally:
        cur.close()
        conn.close()


# ── DB helpers ────────────────────────────────────────────────────────────────

def _log_estate_wa(
    tenant_id: int,
    phone_number_id: str,
    customer_phone: str,
    direction: str,
    content: str,
    message_type: str = "text",
    meta_message_id: str = None,
) -> bool:
    """
    Insert into re_wa_message_log. ON CONFLICT DO NOTHING for dedup.
    Returns True if a new row was inserted.
    """
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO re_wa_message_log
                (tenant_id, phone_number_id, customer_phone,
                 direction, content, message_type, meta_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (meta_message_id) WHERE meta_message_id IS NOT NULL
            DO NOTHING
        """, (
            tenant_id, phone_number_id, customer_phone,
            direction, (content or "")[:2000], message_type, meta_message_id,
        ))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print(f"⚠️ [ESTATE WA] _log_estate_wa error: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def _is_estate_handoff_active(tenant_id: int, phone_number: str) -> bool:
    """
    True if this buyer has a pending handoff request that the AI logged
    in re_handoff_requests. Pending means agent hasn't marked it handled yet.
    """
    conn = get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT 1
            FROM re_handoff_requests r
            JOIN re_customers c ON c.id = r.customer_id
            WHERE r.tenant_id = %s
              AND c.phone_number = %s
              AND r.status = 'pending'
            LIMIT 1
        """, (tenant_id, phone_number))
        return cur.fetchone() is not None
    except Exception as e:
        print(f"⚠️ [ESTATE WA] _is_estate_handoff_active error: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def _get_listing_by_id(tenant_id: int, listing_id: int) -> dict | None:
    """Fetch a property listing row for interactive card display."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT id, title, property_type, transaction_type,
                   location, lga, state, price, price_negotiable,
                   bedrooms, bathrooms, size_sqm,
                   title_document, features, status
            FROM re_property_listings
            WHERE tenant_id = %s AND id = %s
            LIMIT 1
        """, (tenant_id, listing_id))
        row = cur.fetchone()
        if not row:
            return None
        result = dict(row)
        # Format price the same way the AI backend does
        try:
            p = float(result["price"] or 0)
            neg = bool(result.get("price_negotiable"))
            if p >= 1_000_000_000:
                fmt = f"₦{p / 1_000_000_000:.1f}B"
            elif p >= 1_000_000:
                fmt = f"₦{p / 1_000_000:.1f}M"
            elif p > 0:
                fmt = f"₦{p:,.0f}"
            else:
                fmt = "Price on request"
            if neg and fmt != "Price on request":
                fmt += " (Neg.)"
            result["price"] = fmt
        except Exception:
            pass
        return result
    except Exception as e:
        print(f"⚠️ [ESTATE WA] _get_listing_by_id error: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def _get_slots_for_listing(tenant_id: int, listing_id: int | None) -> list[dict]:
    """Return upcoming available inspection slots for this tenant/listing."""
    from datetime import datetime, timezone, timedelta
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=21)
        if listing_id:
            cur.execute("""
                SELECT s.id, s.slot_datetime, s.duration_mins, s.listing_id,
                       l.title AS listing_title, l.location AS listing_location
                FROM re_inspection_slots s
                LEFT JOIN re_property_listings l ON l.id = s.listing_id
                WHERE s.tenant_id = %s
                  AND s.is_available = TRUE
                  AND s.slot_datetime BETWEEN %s AND %s
                  AND (s.listing_id = %s OR s.listing_id IS NULL)
                ORDER BY s.slot_datetime
                LIMIT 10
            """, (tenant_id, now, cutoff, listing_id))
        else:
            cur.execute("""
                SELECT s.id, s.slot_datetime, s.duration_mins, s.listing_id,
                       l.title AS listing_title, l.location AS listing_location
                FROM re_inspection_slots s
                LEFT JOIN re_property_listings l ON l.id = s.listing_id
                WHERE s.tenant_id = %s
                  AND s.is_available = TRUE
                  AND s.slot_datetime BETWEEN %s AND %s
                ORDER BY s.slot_datetime
                LIMIT 10
            """, (tenant_id, now, cutoff))
        return [dict(r) for r in (cur.fetchall() or [])]
    except Exception as e:
        print(f"⚠️ [ESTATE WA] _get_slots_for_listing error: {e}")
        return []
    finally:
        cur.close()
        conn.close()


def _ensure_estate_customer(tenant_id: int, phone: str) -> int | None:
    """Get or create a re_customers row. Returns customer_id or None."""
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO re_customers (tenant_id, phone_number)
            VALUES (%s, %s)
            ON CONFLICT (tenant_id, phone_number) DO UPDATE
                SET last_seen_at = NOW()
            RETURNING id
        """, (tenant_id, phone))
        conn.commit()
        row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception as e:
        print(f"⚠️ [ESTATE WA] _ensure_estate_customer error: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def _book_slot_direct(
    slot_id: int,
    tenant_id: int,
    customer_id: int,
    listing_id: int | None,
) -> dict | None:
    """
    Book an inspection slot directly from the webhook (bypasses AI).
    Returns booking dict with slot_datetime and duration_mins, or None on failure.
    The DB trigger marks the slot unavailable automatically.
    """
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT id, slot_datetime, duration_mins, listing_id
            FROM re_inspection_slots
            WHERE id = %s AND tenant_id = %s AND is_available = TRUE
        """, (slot_id, tenant_id))
        slot = cur.fetchone()
        if not slot:
            return None

        effective_listing = listing_id or slot["listing_id"]
        cur.execute("""
            INSERT INTO re_inspection_bookings
              (tenant_id, slot_id, listing_id, customer_id, status)
            VALUES (%s, %s, %s, %s, 'confirmed')
            RETURNING id, created_at
        """, (tenant_id, slot_id, effective_listing, customer_id))
        booking = dict(cur.fetchone())
        booking["slot_datetime"]  = slot["slot_datetime"]
        booking["duration_mins"]  = slot["duration_mins"]
        booking["listing_id"]     = effective_listing
        conn.commit()
        print(f"   [ESTATE WA] Booking #{booking['id']} created — slot {slot_id}")
        return booking
    except Exception as e:
        print(f"⚠️ [ESTATE WA] _book_slot_direct error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        cur.close()
        conn.close()


def _fmt_booking_confirmation(booking: dict, listing: dict | None) -> str:
    """Build a WhatsApp confirmation message for a booked inspection slot."""
    from datetime import timezone
    dt = booking["slot_datetime"]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    day  = dt.strftime("%A, %d %B %Y")
    time = dt.strftime("%I:%M %p").lstrip("0")
    dur  = booking.get("duration_mins") or 60

    lines = [
        "✅ *Viewing Appointment Confirmed!*",
        "",
        f"📅 {day}",
        f"🕐 {time} ({dur} mins)",
    ]
    if listing:
        title = listing.get("title") or ""
        loc   = listing.get("location") or listing.get("lga") or ""
        state = listing.get("state") or ""
        place = ", ".join(filter(None, [loc, state]))
        if title:
            lines.append(f"🏠 {title}" + (f", {place}" if place else ""))
        elif place:
            lines.append(f"📍 {place}")

    lines += [
        "",
        "One of our agents will contact you before the viewing with the full address and any instructions.",
        "",
        "Reply *CALLBACK* if you would like to speak to someone sooner.",
    ]
    return "\n".join(lines)


def _parse_slot_number(text: str) -> int | None:
    """
    Extract a slot number (1-10) from a buyer's text reply.
    Handles: "2", "option 2", "slot 2", "number 2", "i'll take 2", "pick 3", "#4"
    Returns None if the message doesn't look like a slot selection.
    """
    import re as _re
    t = text.strip().lower()
    # Bare digit(s): "2" or "2."
    m = _re.fullmatch(r'\s*(\d{1,2})\s*\.?\s*', t)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 10 else None
    # "option 3", "slot 3", "number 3", "no 3", "#3"
    m = _re.search(r'(?:option|slot|number|no\.?|#)\s*(\d{1,2})', t)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 10 else None
    # "take 2", "choose 2", "want 2", "pick 2", "select 2", "go with 2"
    m = _re.search(r'(?:take|choose|want|pick|select|go\s+with|prefer)\s+(\d{1,2})', t)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 10 else None
    return None


def _get_last_inspect_listing_id(
    tenant_id: int,
    customer_phone: str,
    within_minutes: int = 15,
) -> int | None:
    """
    Look up the listing_id from the most recent inspection slot list
    sent to this customer (within the timeout window).
    """
    from datetime import datetime, timezone, timedelta
    import re as _re
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
        cur.execute("""
            SELECT content FROM re_wa_message_log
            WHERE tenant_id = %s
              AND customer_phone = %s
              AND direction = 'outbound'
              AND content LIKE '[Inspection slots sent for listing%%'
              AND created_at >= %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (tenant_id, customer_phone, cutoff))
        row = cur.fetchone()
        if not row:
            return None
        m = _re.search(r'listing (\d+)', row[0])
        return int(m.group(1)) if m else None
    except Exception as e:
        print(f"⚠️ [ESTATE WA] _get_last_inspect_listing_id error: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def _get_last_property_card_listing_id(
    tenant_id: int,
    customer_phone: str,
    within_minutes: int = 60,
) -> int | None:
    """
    Look up the listing_id from the most recent property card sent to this customer.
    """
    from datetime import datetime, timezone, timedelta
    import re as _re
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
        cur.execute("""
            SELECT content FROM re_wa_message_log
            WHERE tenant_id = %s
              AND customer_phone = %s
              AND direction = 'outbound'
              AND content LIKE '[Property card: listing%%'
              AND created_at >= %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (tenant_id, customer_phone, cutoff))
        row = cur.fetchone()
        if not row:
            return None
        m = _re.search(r'listing (\d+)', row[0])
        return int(m.group(1)) if m else None
    except Exception as e:
        print(f"⚠️ [ESTATE WA] _get_last_property_card_listing_id error: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def _verify_estate_signature(body: bytes, sig_header: str, app_secret: str = "") -> bool:
    effective = app_secret or _APP_SECRET
    if not effective:
        return True
    expected = hmac.new(effective.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", sig_header or "")


# ── Main handler ──────────────────────────────────────────────────────────────

async def handle_estate_message(
    msg: dict,
    tenant: dict,
    raw_body: bytes = b"",
    sig_header: str = "",
) -> None:
    """
    Full estate WhatsApp message pipeline.
    Called from meta_webhook.py after the tenant is identified as an estate tenant.

    msg: normalized dict from message_normalizer.normalize()
    tenant: row from get_estate_tenant_by_phone_number_id()
    """
    phone_number_id   = msg["phone_number_id"]
    customer_phone    = msg["customer_phone"]
    text              = msg["text"]
    action_type       = msg["action_type"]
    action_product_id = msg["action_product_id"]
    meta_message_id   = msg["meta_message_id"]
    message_type      = msg["message_type"]

    tenant_id    = int(tenant["id"])
    access_token = (tenant.get("wa_access_token") or "").strip()
    api_key      = (tenant.get("wa_api_key") or "").strip()

    if not api_key:
        print(f"⚠️ [ESTATE WA] No API key for tenant_id={tenant_id} — cannot call AI backend")
        if access_token:
            await send_text(phone_number_id, access_token, customer_phone,
                            "Our assistant is temporarily unavailable. Please contact us directly.")
        return

    # Signature verification (uses tenant's wa_app_secret if set)
    if not _verify_estate_signature(raw_body, sig_header, tenant.get("wa_app_secret") or ""):
        print(f"⚠️ [ESTATE WA] HMAC mismatch for tenant_id={tenant_id} — ignoring")
        return

    # Dedup via re_wa_message_log unique index on meta_message_id
    logged = _log_estate_wa(
        tenant_id, phone_number_id, customer_phone,
        "inbound", text, message_type, meta_message_id,
    )
    if not logged:
        print(f"   [ESTATE WA] Duplicate message_id={meta_message_id} — skipped")
        return

    print(f"✅ [ESTATE WA] tenant={tenant_id} from={customer_phone} "
          f"action={action_type or 'text'}: {text[:80]}")

    # Mark as read — fire-and-forget
    asyncio.create_task(mark_as_read(phone_number_id, access_token, meta_message_id))

    # ── Handoff gate ──────────────────────────────────────────────────────────
    if _is_estate_handoff_active(tenant_id, customer_phone):
        print(f"   [ESTATE WA] Handoff active for tenant={tenant_id} "
              f"phone={customer_phone} — AI skipped")
        # Don't reply — let the agent handle it
        return

    # ── Interactive: buyer selected a property from the list ──────────────────
    if action_type == "estate_select" and action_product_id:
        try:
            listing = _get_listing_by_id(tenant_id, int(action_product_id))
            if listing:
                await send_estate_property_card(
                    phone_number_id, access_token, customer_phone, listing,
                )
                _log_estate_wa(tenant_id, phone_number_id, customer_phone,
                               "outbound",
                               f"[Property card: listing {action_product_id}]")
                return
        except (ValueError, TypeError) as e:
            print(f"⚠️ [ESTATE WA] estate_select parse error: {e}")
        # Listing not found — fall through to AI with the descriptive text

    # ── Interactive: buyer tapped "Book Inspection" on a property card ───────
    if action_type == "inspect" and action_product_id:
        try:
            listing_id_int = int(action_product_id)
            slots = _get_slots_for_listing(tenant_id, listing_id_int)
            if slots:
                listing = _get_listing_by_id(tenant_id, listing_id_int)
                listing_title = (listing or {}).get("title") or ""
                await send_inspection_slot_list(
                    phone_number_id, access_token, customer_phone,
                    slots, listing_title=listing_title,
                )
                await send_slot_text_fallback(
                    phone_number_id, access_token, customer_phone, slots,
                )
                _log_estate_wa(tenant_id, phone_number_id, customer_phone,
                               "outbound", f"[Inspection slots sent for listing {listing_id_int}]")
                return
            # No slots → fall through to AI (will trigger HANDOFF)
        except (ValueError, TypeError) as e:
            print(f"⚠️ [ESTATE WA] inspect parse error: {e}")

    # ── Interactive: buyer tapped a slot from the inspection slot list ────────
    if action_type == "slot_select" and action_product_id:
        try:
            slot_id_int  = int(action_product_id)
            customer_id  = _ensure_estate_customer(tenant_id, customer_phone)
            if customer_id:
                booking = _book_slot_direct(slot_id_int, tenant_id, customer_id, None)
                if booking:
                    listing = _get_listing_by_id(tenant_id, booking["listing_id"]) if booking.get("listing_id") else None
                    msg = _fmt_booking_confirmation(booking, listing)
                    await send_text(phone_number_id, access_token, customer_phone, msg)
                    _log_estate_wa(tenant_id, phone_number_id, customer_phone,
                                   "outbound", f"[Booking #{booking['id']} confirmed]")
                    return
                else:
                    # Slot was taken between list display and tap — apologise
                    await send_text(
                        phone_number_id, access_token, customer_phone,
                        "Sorry, that slot was just taken by someone else. "
                        "Let me show you what's still available — please tap *Book Inspection* again.",
                    )
                    _log_estate_wa(tenant_id, phone_number_id, customer_phone,
                                   "outbound", "[Slot collision — buyer asked to retry]")
                    return
        except (ValueError, TypeError) as e:
            print(f"⚠️ [ESTATE WA] slot_select parse error: {e}")
        # Fall through to AI on any unexpected error

    # ── Text slot selection (WhatsApp Web/Desktop — buyer types a number) ────
    if action_type is None:
        slot_number = _parse_slot_number(text)
        if slot_number is not None:
            listing_id = _get_last_inspect_listing_id(tenant_id, customer_phone)
            if listing_id is not None:
                slots = _get_slots_for_listing(tenant_id, listing_id)
                if slots and slot_number <= len(slots):
                    chosen = slots[slot_number - 1]
                    customer_id = _ensure_estate_customer(tenant_id, customer_phone)
                    if customer_id:
                        booking = _book_slot_direct(chosen["id"], tenant_id, customer_id, listing_id)
                        if booking:
                            listing = _get_listing_by_id(tenant_id, listing_id)
                            msg = _fmt_booking_confirmation(booking, listing)
                            await send_text(phone_number_id, access_token, customer_phone, msg)
                            _log_estate_wa(tenant_id, phone_number_id, customer_phone,
                                           "outbound", f"[Booking #{booking['id']} confirmed via text]")
                            return
                        else:
                            await send_text(
                                phone_number_id, access_token, customer_phone,
                                "Sorry, that slot was just taken. "
                                "Please tap *Book Inspection* again to see updated availability.",
                            )
                            _log_estate_wa(tenant_id, phone_number_id, customer_phone,
                                           "outbound", "[Slot collision on text selection]")
                            return

    # ── "BOOK" keyword → find last property card sent, trigger slot list ─────
    if action_type is None:
        import re as _re
        _t = text.strip().lower()
        _book_kw = _re.search(
            r'\b(book|booking|inspect|inspection|view|viewing|visit|appointment|arrange)\b',
            _t,
        )
        if _book_kw:
            listing_id = _get_last_inspect_listing_id(
                tenant_id, customer_phone, within_minutes=60
            )
            if listing_id is None:
                listing_id = _get_last_property_card_listing_id(
                    tenant_id, customer_phone, within_minutes=60
                )
            # Even with no specific property in context, fetch all tenant slots
            slots = _get_slots_for_listing(tenant_id, listing_id)
            if slots:
                listing = _get_listing_by_id(tenant_id, listing_id) if listing_id else None
                listing_title = (listing or {}).get("title") or ""
                await send_inspection_slot_list(
                    phone_number_id, access_token, customer_phone,
                    slots, listing_title=listing_title,
                )
                await send_slot_text_fallback(
                    phone_number_id, access_token, customer_phone, slots,
                )
                _log_estate_wa(tenant_id, phone_number_id, customer_phone,
                               "outbound",
                               f"[Inspection slots sent for listing {listing_id or 'any'}]")
                return
            # No slots at all — fall through to AI (will trigger handoff)

    # ── CALLBACK / more_props are handled as text by AI ───────────────────────
    # message_normalizer set text to a human-readable phrase for these button taps
    # so the AI backend naturally understands and logs the action_type.

    # ── Call Estate AI backend ────────────────────────────────────────────────
    chat_payload = {
        "api_key":      api_key,
        "phone_number": customer_phone,
        "message":      text,
    }

    try:
        async with httpx.AsyncClient(timeout=35) as client:
            resp = await client.post(
                f"{_AI_BACKEND_URL}/estate-chat",
                json=chat_payload,
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        print(f"⚠️ [ESTATE WA] /estate-chat failed tenant={tenant_id}: {e}")
        error_msg = (
            "I'm sorry, I'm experiencing a technical issue right now. "
            "Please try again in a moment."
        )
        await send_text(phone_number_id, access_token, customer_phone, error_msg)
        _log_estate_wa(tenant_id, phone_number_id, customer_phone, "outbound", error_msg)
        return

    # ── Quota exceeded ────────────────────────────────────────────────────────
    if result.get("quota_exceeded"):
        print(f"🚫 [ESTATE WA] Quota exceeded for tenant={tenant_id}")
        fallback = (
            "Our assistant is temporarily unavailable. "
            "Please contact us directly for assistance."
        )
        await send_text(phone_number_id, access_token, customer_phone, fallback)
        _log_estate_wa(tenant_id, phone_number_id, customer_phone, "outbound", fallback)
        return

    reply             = (result.get("reply") or "").strip()
    listings          = result.get("listings") or []
    handoff_triggered = bool(result.get("handoff_triggered"))

    # ── Dispatch response ─────────────────────────────────────────────────────
    if listings:
        # Send AI text intro first, then property detail cards directly.
        # Quick-reply buttons on cards work on WhatsApp Desktop/Web;
        # interactive list messages do not.
        if reply:
            await send_text(phone_number_id, access_token, customer_phone, reply)
        cards_sent = 0
        sent_listing_ids = []
        for listing_card in listings[:3]:
            try:
                lid = int(listing_card.get("id") or 0)
            except (TypeError, ValueError):
                continue
            full_listing = _get_listing_by_id(tenant_id, lid)
            if full_listing:
                await send_estate_property_card(
                    phone_number_id, access_token, customer_phone, full_listing,
                )
                _log_estate_wa(tenant_id, phone_number_id, customer_phone,
                               "outbound", f"[Property card: listing {lid}]")
                sent_listing_ids.append(lid)
                cards_sent += 1
        if cards_sent == 0 and not reply:
            await send_text(
                phone_number_id, access_token, customer_phone,
                "I found some properties matching your request — "
                "please reply and I'll share the details.",
            )
        log_label = f"[{cards_sent} property card{'s' if cards_sent != 1 else ''} sent directly]"
    elif reply:
        await send_text(phone_number_id, access_token, customer_phone, reply)
        log_label = reply[:500]
    else:
        log_label = "[no reply]"

    _log_estate_wa(tenant_id, phone_number_id, customer_phone, "outbound", log_label)

    if handoff_triggered:
        print(f"🙋 [ESTATE WA] Handoff triggered — tenant={tenant_id} "
              f"phone={customer_phone}")
        # re_handoff_requests already logged by AI backend; no WA session state needed
