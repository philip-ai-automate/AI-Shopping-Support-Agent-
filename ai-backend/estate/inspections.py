"""
estate/inspections.py — Inspection slot fetching and booking for the AI backend.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import psycopg2.extras
from db import get_db_connection


def get_available_slots(tenant_id: int, listing_id: int | None = None, days_ahead: int = 21) -> list[dict]:
    """Return upcoming available inspection slots for this tenant."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days_ahead)

        if listing_id:
            cur.execute("""
                SELECT s.id, s.slot_datetime, s.duration_mins, s.listing_id,
                       l.title AS listing_title, l.location AS listing_location
                FROM re_inspection_slots s
                LEFT JOIN re_property_listings l ON l.id = s.listing_id
                WHERE s.tenant_id = %s
                  AND s.is_available = TRUE
                  AND s.slot_datetime >= %s
                  AND s.slot_datetime <= %s
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
                  AND s.slot_datetime >= %s
                  AND s.slot_datetime <= %s
                ORDER BY s.slot_datetime
                LIMIT 10
            """, (tenant_id, now, cutoff))

        rows = [dict(r) for r in (cur.fetchall() or [])]
        cur.close()
        return rows
    except Exception as e:
        print(f"⚠️ [INSPECTIONS] get_available_slots error: {e}")
        return []
    finally:
        conn.close()


def format_slots_for_prompt(slots: list[dict]) -> str:
    """Format slots as numbered text list for injection into the system prompt."""
    if not slots:
        return ""
    lines = []
    for i, s in enumerate(slots, 1):
        dt: datetime = s["slot_datetime"]
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day = dt.strftime("%A, %d %B %Y")
        time = dt.strftime("%I:%M %p").lstrip("0")
        dur = s.get("duration_mins") or 60
        listing_label = ""
        if s.get("listing_title"):
            loc = s.get("listing_location") or ""
            listing_label = f" — {s['listing_title']}" + (f", {loc}" if loc else "")
        lines.append(f"  Slot {i} (ID:{s['id']}): {day} at {time} ({dur} mins){listing_label}")
    return "\n".join(lines)


def book_slot(
    slot_id: int,
    tenant_id: int,
    customer_id: int,
    listing_id: int | None,
    notes: str = "",
) -> dict | None:
    """
    Create an inspection booking. The DB trigger marks the slot unavailable automatically.
    Returns the booking row or None on failure.
    """
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Confirm slot is still available and belongs to this tenant
        cur.execute("""
            SELECT id, slot_datetime, duration_mins, listing_id
            FROM re_inspection_slots
            WHERE id = %s AND tenant_id = %s AND is_available = TRUE
        """, (slot_id, tenant_id))
        slot = cur.fetchone()
        if not slot:
            cur.close()
            return None  # slot taken or doesn't exist

        effective_listing = listing_id or slot.get("listing_id")

        cur.execute("""
            INSERT INTO re_inspection_bookings
              (tenant_id, slot_id, listing_id, customer_id, status, notes)
            VALUES (%s, %s, %s, %s, 'confirmed', %s)
            RETURNING id, created_at
        """, (tenant_id, slot_id, effective_listing, customer_id, notes or ""))

        booking = dict(cur.fetchone())
        booking["slot_datetime"] = slot["slot_datetime"]
        booking["duration_mins"] = slot["duration_mins"]
        booking["listing_id"]    = effective_listing
        conn.commit()
        cur.close()
        print(f"   [INSPECTIONS] Booking #{booking['id']} created — slot {slot_id}")
        return booking
    except Exception as e:
        print(f"⚠️ [INSPECTIONS] book_slot error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        conn.close()
