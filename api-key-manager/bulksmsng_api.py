"""
Thin wrapper around BulkSMSNigeria's HTTP API (https://www.bulksmsnigeria.com/api-documentation).
Replaces ebulksms_api.py as the SMS Campaign feature's provider (2026-08-06) —
same public function names/signatures so portal_routes.py didn't need to
change anything except which module it imports.

Credentials are a single BulkSMSNigeria account (sales@profitbuyz.com), read
from .env — not per-tenant, since this is only ever used for PhiXtra's own
outbound sales texts, never on behalf of a customer.
"""
import csv
import io
import os

import openpyxl
import requests

API_URL = "https://www.bulksmsnigeria.com/api/v2/sms"
TIMEOUT = 30
NG_COUNTRY_CODE = "234"

# BulkSMSNigeria's docs don't publish a hard per-request recipient cap either;
# same reasoning as the old eBulkSMS wrapper — keep each HTTP call to a size
# that reliably finishes within TIMEOUT and gives partial progress if a later
# batch fails.
BATCH_SIZE = 500

# GSM 03.38 basic character set — this is an SMS protocol fact, not specific
# to any one provider, so it's unchanged from the eBulkSMS wrapper. A message
# using only these characters is billed/split as GSM-7 (160 chars single /
# 153 per part when concatenated); anything else (emoji, curly quotes, most
# accented letters) forces UCS-2 (70 chars single / 67 per part).
_GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_SET = set(_GSM7_BASIC)


def count_sms_parts(message: str) -> tuple[int, int]:
    """Returns (parts, chars_per_part) for `message` as it would be split and
    billed. Used to warn before sending: total SMS units sent = parts *
    recipient_count."""
    if not message:
        return 0, 160
    is_gsm7 = all(ch in _GSM7_SET for ch in message)
    length = len(message)
    if is_gsm7:
        if length <= 160:
            return 1, 160
        return -(-length // 153), 153  # ceil division
    if length <= 70:
        return 1, 70
    return -(-length // 67), 67


def normalize_number(raw: str) -> str:
    """0803... -> 234803..., +234803... -> 234803..., else left as-is."""
    number = raw.strip().replace(" ", "").replace("-", "")
    if number.startswith("+"):
        return number[1:]
    if number.startswith("0"):
        return NG_COUNTRY_CODE + number[1:]
    return number


def parse_phone_upload(filename: str, raw_bytes: bytes) -> tuple[list[str], str | None]:
    """Extracts phone numbers from an uploaded .csv/.xlsx/.xls file. Looks for
    a column named phone/number/mobile/tel (case-insensitive); falls back to
    the first column if no header matches. Returns (numbers, error) — numbers
    is a deduplicated, normalized list."""
    name = (filename or "").lower()
    rows: list[str] = []

    try:
        if name.endswith(".csv"):
            text = raw_bytes.decode("utf-8-sig", errors="ignore")
            reader = list(csv.reader(io.StringIO(text)))
            if not reader:
                return [], "The file is empty."
            header = [h.strip().lower() for h in reader[0]]
            col = next((i for i, h in enumerate(header)
                        if h in ("phone", "number", "mobile", "phone number", "tel", "telephone")), None)
            body = reader[1:] if col is not None else reader
            col = col if col is not None else 0
            for row in body:
                if col < len(row) and row[col].strip():
                    rows.append(row[col].strip())
        elif name.endswith(".xlsx") or name.endswith(".xls"):
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
            ws = wb.active
            all_rows = list(ws.iter_rows(values_only=True))
            if not all_rows:
                return [], "The file is empty."
            header = [str(h).strip().lower() if h is not None else "" for h in all_rows[0]]
            col = next((i for i, h in enumerate(header)
                        if h in ("phone", "number", "mobile", "phone number", "tel", "telephone")), None)
            body = all_rows[1:] if col is not None else all_rows
            col = col if col is not None else 0
            for row in body:
                if col < len(row) and row[col] is not None and str(row[col]).strip():
                    rows.append(str(row[col]).strip())
        else:
            return [], "Unsupported file type — upload a .csv, .xlsx, or .xls file."
    except Exception as e:
        return [], f"Could not read the file: {e}"

    numbers = []
    seen = set()
    for raw in rows:
        # Excel often turns "08031234567" into a float like "8031234567.0"
        if raw.endswith(".0") and raw.replace(".0", "").replace("-", "").isdigit():
            raw = raw[:-2]
        num = normalize_number(raw)
        if num and num not in seen and any(c.isdigit() for c in num):
            seen.add(num)
            numbers.append(num)

    if not numbers:
        return [], "No phone numbers found in that file."
    return numbers, None


def _send_one_batch(token: str, sender: str, numbers: list[str], message: str) -> tuple[int, int, str | None]:
    payload = {"from": sender, "to": ",".join(numbers), "body": message}
    try:
        r = requests.post(
            API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return 0, len(numbers), f"Could not reach BulkSMSNigeria: {e}"

    try:
        data = r.json()
    except ValueError:
        return 0, len(numbers), f"BulkSMSNigeria returned an unreadable response: {r.text[:200]}"

    if data.get("status") != "success":
        err = data.get("message") or data.get("code") or "UNKNOWN_ERROR"
        return 0, len(numbers), f"BulkSMSNigeria error: {err}"

    sent = int((data.get("data") or {}).get("recipients_count") or len(numbers))
    return sent, len(numbers) - sent, None


def get_sender_name() -> str:
    """The sender ID SMS currently go out under — shared across every send,
    but recorded on each sms_campaigns row at send time so history stays
    accurate even if BULKSMSNG_SENDER_NAME is ever changed later."""
    return os.getenv("BULKSMSNG_SENDER_NAME", "PHIXTRA")[:11]


def send_bulk_sms(recipients: list[str], message: str) -> tuple[int, int, str | None]:
    """Sends `message` to every phone number in `recipients`, in batches of
    BATCH_SIZE. Returns (sent_count, failed_count, error) — error is only set
    when EVERY batch failed (e.g. bad token); if some batches succeeded and a
    later one failed (e.g. ran out of credit partway through), the partial
    sent_count/failed_count are still returned alongside the error so the
    caller can record what actually went out."""
    token  = os.getenv("BULKSMSNG_API_TOKEN")
    sender = os.getenv("BULKSMSNG_SENDER_NAME", "PHIXTRA")[:11]

    if not token:
        return 0, 0, "BulkSMSNigeria is not configured (missing BULKSMSNG_API_TOKEN in .env)."

    valid_numbers = [normalize_number(r) for r in recipients if (r or "").strip()]
    if not valid_numbers:
        return 0, 0, "No valid phone numbers to send to."

    total_sent = total_failed = 0
    last_error = None
    for i in range(0, len(valid_numbers), BATCH_SIZE):
        batch = valid_numbers[i:i + BATCH_SIZE]
        sent, failed, error = _send_one_batch(token, sender, batch, message)
        total_sent += sent
        total_failed += failed
        if error:
            last_error = error

    if last_error and total_sent == 0:
        return 0, 0, last_error
    return total_sent, total_failed, last_error
