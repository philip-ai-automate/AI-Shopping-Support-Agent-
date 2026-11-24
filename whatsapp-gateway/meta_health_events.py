"""
meta_health_events.py: keep Meta's WhatsApp account-health webhooks (24 Nov 2026).

Meta sends warnings (account banned or restricted, quality rating changes,
review decisions, alerts) to the same /meta-webhook as messages. Until now
they were dropped as "ignored". They are stored in wa_meta_events; the portal
job (wa_health.py, every minute) picks them up, asks Meta for the number's
real state, and tells the business and PhiXtra staff.

An event never decides anything by itself: it only triggers a fresh check with
Meta, so a forged event can at most cause one extra check. sig_ok records
whether the signature matched, and only signed events are quoted on the
account's timeline.
"""
import hashlib
import hmac
import json
import os

from wa_db import get_db_connection

HEALTH_FIELDS = {
    "account_update", "account_alerts", "account_review_update", "phone_number_quality_update",
    "phone_number_name_update", "business_capability_update", "security",
}
_SECRET = os.getenv("FB_APP_SECRET") or os.getenv("META_APP_SECRET") or ""
_ready = False


def _changes(payload: dict):
    for e in (payload or {}).get("entry") or []:
        for ch in e.get("changes") or []:
            yield str(e.get("id") or ""), ch.get("field") or "", ch.get("value") or {}


def only_health(payload: dict) -> bool:
    """True when every change in the post is a health field (nothing else to handle)."""
    fields = [f for _, f, _ in _changes(payload)]
    return bool(fields) and all(f in HEALTH_FIELDS for f in fields)


def _small(value) -> str:
    """The event as JSON; a very large one is kept as a note so the insert can't fail."""
    s = json.dumps(value)
    return s if len(s) <= 20000 else json.dumps({"too_large": True, "event": str(value.get("event") or "")[:100]})


def is_health_payload(payload: dict) -> bool:
    return isinstance(payload, dict) and any(f in HEALTH_FIELDS for _, f, _ in _changes(payload))


def _sig_ok(body: bytes, sig_header: str) -> bool:
    if not _SECRET:
        return False
    expected = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", sig_header or "")


def _ensure(cur):
    global _ready
    if _ready:
        return
    cur.execute("SET lock_timeout = '3s'")
    cur.execute("""CREATE TABLE IF NOT EXISTS wa_meta_events (
        id BIGSERIAL PRIMARY KEY,
        received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        field VARCHAR(60) NOT NULL,
        waba_id VARCHAR(40),
        phone_number_id VARCHAR(40),
        display_phone VARCHAR(40),
        value JSONB NOT NULL,
        sig_ok BOOLEAN NOT NULL DEFAULT FALSE,
        processed_at TIMESTAMPTZ)""")
    cur.execute("CREATE INDEX IF NOT EXISTS wa_meta_events_todo ON wa_meta_events (processed_at) WHERE processed_at IS NULL")
    _ready = True


def store_health_events(payload: dict, body: bytes, sig_header: str) -> int:
    ok = _sig_ok(body, sig_header)
    conn = get_db_connection()
    if not conn:
        return 0
    n = 0
    try:
        cur = conn.cursor()
        _ensure(cur)
        for waba_id, field, value in _changes(payload):
            if field not in HEALTH_FIELDS:
                continue
            pnid = str((value.get("metadata") or {}).get("phone_number_id") or value.get("phone_number_id") or "")
            disp = str(value.get("display_phone_number") or "")
            cur.execute("""INSERT INTO wa_meta_events (field, waba_id, phone_number_id, display_phone, value, sig_ok)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (field, waba_id or None, pnid or None, disp or None, _small(value), ok))
            n += 1
        conn.commit()
        cur.close()
        print(f"🩺 [META] health event stored: {n} change(s), signature {'ok' if ok else 'NOT verified'}")
    except Exception as e:
        conn.rollback()
        print("⚠️ [META] health event store error:", e)
    finally:
        conn.close()
    return n
