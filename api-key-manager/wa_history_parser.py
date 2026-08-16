"""
Parser for WhatsApp's native "Export Chat" .txt format (Android and iOS).

Handles both plain-text messages and media references. A media reference
(`<attached: filename.jpg>`, or the older "filename.jpg (file attached)"
format) is captured as `media_filename` + optional caption on the message
dict — resolving that filename against an actual "With Media" .zip export
(matching bytes, saving them, setting message_type) is the caller's job, not
this module's; this parser only extracts what the text names. A bare
"<Media omitted>" line (WhatsApp never had the file — the sender's phone had
already deleted it before exporting) has no filename to resolve and is
counted separately.

Two header styles are supported:
  Android: "12/07/2024, 14:23 - Chidi: Hello, is this available?"
  iOS:     "[12/07/2024, 14:23:05] Chidi: Hello, is this available?"

Date order (day-first vs month-first) is ambiguous in the format itself —
callers must supply `day_first` (the merchant picks this at upload time).
"""

import re
from datetime import datetime

_HEADER_ANDROID = re.compile(
    r"^‎?(?P<date>\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}),\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\s*-\s*(?P<rest>.*)$"
)
_HEADER_IOS = re.compile(
    r"^‎?\[(?P<date>\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}),\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\]\s*(?P<rest>.*)$"
)
_SENDER_SPLIT = re.compile(r"^(?P<sender>[^:]{1,60}?):\s(?P<text>.*)$")

# Newer WhatsApp export format: "<attached: 00000012-PHOTO-....jpg>" with an
# optional caption following the closing bracket.
_ATTACHED_RE = re.compile(r"^<attached:\s*(?P<filename>[^>]+)>\s*(?P<caption>.*)$", re.IGNORECASE)
# Older WhatsApp export format: "IMG-20210712-WA0001.jpg (file attached)"
_OLD_ATTACHED_RE = re.compile(r"^(?P<filename>\S+)\s+\(file attached\)\s*(?P<caption>.*)$", re.IGNORECASE)

_OMITTED_MARKERS = (
    "<media omitted>", "image omitted", "video omitted", "audio omitted",
    "sticker omitted", "gif omitted", "document omitted",
)

_DATE_FORMATS = {
    True:  ["%d/%m/%Y", "%d/%m/%y"],   # day_first
    False: ["%m/%d/%Y", "%m/%d/%y"],   # month_first
}
_TIME_FORMATS = ["%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"]


def _normalize_time(t: str) -> str:
    t = t.replace(" ", " ").replace("\xa0", " ").strip()
    return re.sub(r"(?i)\b(am|pm)\b", lambda m: m.group(1).upper(), t)


def _parse_datetime(date_str: str, time_str: str, day_first: bool):
    date_str = date_str.replace(".", "/").replace("-", "/")
    time_str = _normalize_time(time_str)
    for df in _DATE_FORMATS[day_first]:
        for tf in _TIME_FORMATS:
            try:
                return datetime.strptime(f"{date_str} {time_str}", f"{df} {tf}")
            except ValueError:
                continue
    return None


def _parse_media_reference(text: str):
    """
    If `text` is a media line, return a dict describing it, else None:
      {"filename": str|None, "caption": str|None, "omitted": bool}
    `omitted=True` means WhatsApp itself never had the file (nothing to
    resolve); otherwise `filename` names the file to look for in a "With
    Media" .zip export.
    """
    t = text.strip()

    m = _ATTACHED_RE.match(t)
    if m:
        caption = (m.group("caption") or "").strip() or None
        return {"filename": m.group("filename").strip(), "caption": caption, "omitted": False}

    m = _OLD_ATTACHED_RE.match(t)
    if m:
        caption = (m.group("caption") or "").strip() or None
        return {"filename": m.group("filename").strip(), "caption": caption, "omitted": False}

    lowered = t.lower()
    if any(lowered.startswith(marker) for marker in _OMITTED_MARKERS):
        return {"filename": None, "caption": None, "omitted": True}

    return None


def parse_whatsapp_export(raw_text: str, day_first: bool = True) -> dict:
    """
    Returns {"messages": [...], "skipped_system": int, "skipped_media": int,
             "unparsed_lines": int}

    Each message dict: {"timestamp": datetime, "sender": str, "text": str,
    "media_filename": str|None}. `media_filename` is set when the line named
    an attachment — resolving it against a .zip's contents is the caller's
    job. `skipped_media` counts only messages WhatsApp marked "<Media
    omitted>" (no filename ever existed to resolve).
    """
    lines = raw_text.splitlines()
    messages = []
    skipped_system = 0
    skipped_media = 0
    unparsed_lines = 0
    current = None

    for raw_line in lines:
        line = raw_line.replace("‎", "").rstrip("\r")

        if not line.strip():
            if current:
                current["text"] += "\n"
            continue

        m = _HEADER_ANDROID.match(line) or _HEADER_IOS.match(line)
        if m:
            dt = _parse_datetime(m.group("date"), m.group("time"), day_first)
            rest = m.group("rest")
            sm = _SENDER_SPLIT.match(rest) if rest else None

            if dt is None:
                unparsed_lines += 1
                if current:
                    messages.append(current)
                    current = None
                continue

            if not sm:
                # No "Sender: " split → a system/notification line
                # (e.g. "Messages are end-to-end encrypted", "X changed the subject").
                skipped_system += 1
                if current:
                    messages.append(current)
                    current = None
                continue

            if current:
                messages.append(current)
                current = None

            sender = sm.group("sender").strip()
            text = sm.group("text").strip()

            media = _parse_media_reference(text)
            if media:
                if media["omitted"]:
                    skipped_media += 1
                    continue
                current = {
                    "timestamp": dt, "sender": sender,
                    "text": media["caption"] or "",
                    "media_filename": media["filename"],
                }
            else:
                current = {"timestamp": dt, "sender": sender, "text": text, "media_filename": None}
        else:
            if current:
                current["text"] += "\n" + line
            # else: stray line before any recognised header — ignore silently

    if current:
        messages.append(current)

    return {
        "messages": messages,
        "skipped_system": skipped_system,
        "skipped_media": skipped_media,
        "unparsed_lines": unparsed_lines,
    }


def classify_direction(sender: str, business_name: str) -> str:
    """Return 'outbound' if `sender` looks like the business, else 'inbound'."""
    s = (sender or "").strip().casefold()
    b = (business_name or "").strip().casefold()
    if not b:
        return "inbound"
    if s == b:
        return "outbound"
    if len(b) >= 3 and (b in s or s in b):
        return "outbound"
    return "inbound"
