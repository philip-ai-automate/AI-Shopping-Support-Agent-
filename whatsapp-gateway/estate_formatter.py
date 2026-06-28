"""
estate_formatter.py — WhatsApp message formatters for PhiXtra Real Estate.

Sends property listings as interactive list messages and rich detail cards
with INSPECT / CALLBACK quick-reply buttons.
"""
from meta_sender import send_text, _send, _trunc


# ── Interactive property list ─────────────────────────────────────────────────

async def send_estate_property_list(
    phone_number_id: str,
    access_token: str,
    to: str,
    listings: list,
    body_text: str = "Tap a property to see full details:",
) -> bool:
    """
    Send up to 10 properties as a WhatsApp Interactive List Message.

    listings: list of card dicts from /estate-chat (id, title, price, bedrooms,
              property_type, transaction_type, state, etc.)
    Row ID format: elst_{listing_id}
    """
    rows = []
    for listing in listings[:10]:
        lid   = str(listing.get("id") or "")
        title = _trunc(listing.get("title") or "Property", 24)
        price = listing.get("price") or ""          # already formatted e.g. "₦85M"
        beds  = listing.get("bedrooms") or ""
        # lga is more specific than state; location is most specific
        area  = (listing.get("lga") or listing.get("location") or
                 listing.get("state") or "")

        desc_parts = []
        if beds:
            desc_parts.append(f"{beds}BR")
        if price:
            desc_parts.append(price)
        if area:
            desc_parts.append(area)
        description = _trunc(" · ".join(desc_parts), 72)

        rows.append({
            "id":          f"elst_{lid}",
            "title":       title,
            "description": description,
        })

    if not rows:
        return False

    return await _send(phone_number_id, access_token, {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "interactive",
        "interactive": {
            "type":   "list",
            "header": {"type": "text", "text": "Properties for you"},
            "body":   {"text": body_text or "Tap a property to see full details:"},
            "footer": {"text": "More info including photos sent on selection"},
            "action": {
                "button": "View Properties",
                "sections": [{"title": "Listings", "rows": rows}],
            },
        },
    })


# ── Rich property detail card ─────────────────────────────────────────────────

_TITLE_DOC_LABELS = {
    "C_of_O":             "C of O",
    "Governors_Consent":  "Governors Consent",
    "Deed_of_Assignment": "Deed of Assignment",
    "Survey":             "Survey Plan",
    "Excision":           "Excision",
    "Freehold":           "Freehold",
    "Other":              "Other",
    "None":               "",   # skip display
}


def _build_property_card_text(listing: dict) -> str:
    """
    Build a rich WhatsApp text card from a listing card dict.
    Works for both AI-backend card dicts and direct DB rows from _get_listing_by_id.
    """
    title     = listing.get("title") or "Property"
    ptype     = (listing.get("property_type") or "").replace("_", " ").title()
    trans     = (listing.get("transaction_type") or "").title()
    # Use most specific location info available
    location  = listing.get("location") or listing.get("lga") or ""
    state_val = listing.get("state") or ""
    full_loc  = ", ".join(filter(None, [location, state_val]))
    price     = listing.get("price") or "Price on request"
    beds      = listing.get("bedrooms")
    baths     = listing.get("bathrooms")
    size      = listing.get("size_sqm")
    # Format title_document from raw DB values to human-readable labels
    raw_doc   = listing.get("title_document") or ""
    title_doc = _TITLE_DOC_LABELS.get(raw_doc, raw_doc.replace("_", " "))
    status    = (listing.get("status") or "available").replace("_", " ").title()
    features  = listing.get("features") or []

    lines = [f"*{title}*", ""]

    if full_loc:
        lines.append(f"📍 Location: {full_loc}")
    type_parts = [p for p in [ptype, f"for {trans}" if trans else ""] if p]
    if type_parts:
        lines.append(f"🏠 Type: {' '.join(type_parts)}")
    if beds is not None:
        lines.append(f"🛏 Bedrooms: {beds}")
    if baths is not None:
        lines.append(f"🚿 Bathrooms: {baths}")
    if size:
        lines.append(f"📐 Size: {size} sqm")
    if title_doc:
        lines.append(f"📜 Title: {title_doc}")
    lines.append(f"💰 Price: *{price}*")
    lines.append(f"🏷️ Status: {status}")

    if features:
        feat_str = " · ".join(str(f) for f in features[:6])
        if feat_str:
            lines += ["", f"✅ Features: {feat_str}"]

    lines += ["", "─────────────────", "💬 Reply *BOOK* to schedule a viewing for this property."]
    return "\n".join(lines)


async def send_inspection_slot_list(
    phone_number_id: str,
    access_token: str,
    to: str,
    slots: list,
    listing_title: str = "",
) -> bool:
    """
    Send available inspection slots as a WhatsApp Interactive List Message.
    Each row ID is islot_{slot_id} so the normalizer routes taps to slot_select.
    Meta limits: title ≤24 chars, description ≤72 chars, max 10 rows.
    """
    from datetime import timezone

    rows = []
    for s in slots[:10]:
        dt = s["slot_datetime"]
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day  = dt.strftime("%a %d %b")
        time = dt.strftime("%I:%M %p").lstrip("0")
        dur  = s.get("duration_mins") or 60

        title       = _trunc(f"{day} · {time}", 24)
        description = _trunc(f"{dur} min viewing", 72)

        rows.append({
            "id":          f"islot_{s['id']}",
            "title":       title,
            "description": description,
        })

    if not rows:
        return False

    header = _trunc(listing_title or "Property Viewing", 60)

    return await _send(phone_number_id, access_token, {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "interactive",
        "interactive": {
            "type":   "list",
            "header": {"type": "text", "text": header},
            "body":   {"text": "Tap a time slot to confirm your viewing appointment:"},
            "footer": {"text": "An agent will contact you before the viewing"},
            "action": {
                "button":   "Choose a Slot",
                "sections": [{"title": "Available Times", "rows": rows}],
            },
        },
    })


async def send_slot_text_fallback(
    phone_number_id: str,
    access_token: str,
    to: str,
    slots: list,
) -> bool:
    """
    Send a numbered text list of slots immediately after the interactive list.
    For WhatsApp Web / Desktop users who cannot tap interactive lists.
    """
    from datetime import timezone

    numbers = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines = ["💻 *On desktop or web?* Reply with the slot number:\n"]
    for i, s in enumerate(slots[:10]):
        dt = s["slot_datetime"]
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day  = dt.strftime("%a %d %b")
        time = dt.strftime("%I:%M %p").lstrip("0")
        dur  = s.get("duration_mins") or 60
        lines.append(f"{numbers[i]}  {day} · {time} ({dur} min)")

    lines.append("\nE.g. reply *1* to book the first slot")
    return await send_text(phone_number_id, access_token, to, "\n".join(lines))


async def send_estate_property_card(
    phone_number_id: str,
    access_token: str,
    to: str,
    listing: dict,
) -> bool:
    """
    Send a rich property detail card as plain text.
    Works on WhatsApp Desktop, Web, and mobile.
    Buyer replies BOOK to trigger the slot booking flow.
    """
    card_text = _build_property_card_text(listing)
    return await send_text(phone_number_id, access_token, to, card_text)
