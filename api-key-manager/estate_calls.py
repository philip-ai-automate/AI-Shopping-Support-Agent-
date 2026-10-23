"""KeyLand WhatsApp calls (2026-10-22), ported from the main portal's calls
(calls_routes.py + whatsapp-gateway/wa_calls.py).

A buyer taps the call button in WhatsApp on the agency's number. The WhatsApp
gateway (keyland_calls.py) saves the call as 'ringing' with Meta's offer and
sends phone notifications; every KeyLand page polls /estate/calls/live
(keyland-calls.js) and the first person to press Answer takes it. Audio runs in
the browser (WebRTC). Unanswered: the buyer gets the missed-call message, the
lead shows "Missed WhatsApp call" in Needs attention until someone acts.

Incoming only: Meta doesn't let business numbers in Nigeria (+234), the US and
Canada, Egypt or Vietnam start calls.

Tables (new, created once):
  re_calls          each call
  re_call_settings  per agency: calls on, hours, missed-call message, Meta result
"""
import json
from datetime import datetime, timezone

import psycopg2.extras

from db import get_db_connection

GRAPH = "https://graph.facebook.com/v23.0"
DEFAULT_MISSED = "Sorry we missed your call. How can we help? Reply here and our team will get back to you shortly."
RING_SECONDS = 60
NO_OUTGOING = ("234", "1", "20", "84")
META_DAYS = ["SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]
DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
TIER_OK = ("TIER_2K", "TIER_10K", "TIER_100K", "TIER_UNLIMITED")

_ready = False


def ensure_tables():
    global _ready
    if _ready:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('re_calls') IS NOT NULL, to_regclass('re_call_settings') IS NOT NULL")
        if not all(cur.fetchone()):
            cur.execute("SET lock_timeout = '3s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_calls (
                id BIGSERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                phone_number_id VARCHAR(64) NOT NULL,
                wa_call_id VARCHAR(200) UNIQUE,
                customer_phone VARCHAR(40) NOT NULL,
                customer_name VARCHAR(200),
                customer_id INTEGER,
                assigned_to INTEGER,
                status VARCHAR(16) NOT NULL,
                missed_reason VARCHAR(20),
                offer_sdp TEXT,
                answer_sdp TEXT,
                answered_by INTEGER,
                answered_by_name VARCHAR(120),
                answered_at TIMESTAMPTZ,
                ended_at TIMESTAMPTZ,
                duration INTEGER,
                auto_reply VARCHAR(10),
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS re_calls_tenant ON re_calls (tenant_id, created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS re_calls_customer ON re_calls (customer_id, created_at)")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_call_settings (
                tenant_id INTEGER PRIMARY KEY,
                incoming_on BOOLEAN NOT NULL DEFAULT FALSE,
                hours_on BOOLEAN NOT NULL DEFAULT FALSE,
                days VARCHAR(20) NOT NULL DEFAULT '1,2,3,4,5,6',
                open_time VARCHAR(5) NOT NULL DEFAULT '08:00',
                close_time VARCHAR(5) NOT NULL DEFAULT '18:00',
                tz VARCHAR(40) NOT NULL DEFAULT 'Africa/Lagos',
                missed_message TEXT,
                meta_status TEXT,
                updated_by VARCHAR(120),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        _ready = True
    except Exception as e:
        print(f"⚠️ [KL CALLS] ensure_tables: {e}")
    finally:
        conn.close()


def settings(tenant_id: int) -> dict:
    ensure_tables()
    out = {"incoming_on": False, "hours_on": False, "days": [1, 2, 3, 4, 5, 6], "open_time": "08:00",
           "close_time": "18:00", "tz": "Africa/Lagos", "missed_message": DEFAULT_MISSED, "meta_status": None,
           "updated_by": None, "updated_at": None}
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM re_call_settings WHERE tenant_id=%s", (tenant_id,))
    r = cur.fetchone()
    cur.close(); conn.close()
    if r:
        out.update({k: r[k] for k in ("incoming_on", "hours_on", "open_time", "close_time", "tz", "meta_status",
                                       "updated_by", "updated_at")})
        out["days"] = [int(d) for d in (r["days"] or "").split(",") if d.strip().isdigit()]
        out["missed_message"] = r["missed_message"] or DEFAULT_MISSED
    return out


def meta_calling_body(st: dict) -> dict:
    """Meta's /settings 'calling' block for these settings."""
    on = st["incoming_on"]
    calling = {"status": "ENABLED" if on else "DISABLED", "call_icon_visibility": "DEFAULT" if on else "DISABLE_ALL",
               "callback_permission_status": "DISABLED"}
    if st["hours_on"]:
        calling["call_hours"] = {"status": "ENABLED", "timezone_id": st["tz"],
                                 "weekly_operating_hours": [{"day_of_week": META_DAYS[d],
                                                             "open_time": st["open_time"].replace(":", ""),
                                                             "close_time": st["close_time"].replace(":", "")}
                                                            for d in st["days"]]}
    else:
        calling["call_hours"] = {"status": "DISABLED", "timezone_id": st["tz"],
                                 "weekly_operating_hours": [{"day_of_week": "MONDAY", "open_time": "0000", "close_time": "2359"}]}
    return {"calling": calling}


def graph(pnid: str, token: str, body: dict, path: str = "calls", http=None):
    import requests
    http = http or requests
    try:
        r = http.post(f"{GRAPH}/{pnid}/{path}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        try:
            data = r.json()
        except ValueError:
            data = {"error": {"message": r.text[:300]}}
        return r.status_code == 200, data
    except Exception as e:
        return False, {"error": {"message": str(e)}}


def meta_error(data) -> str:
    err = (data or {}).get("error") or {}
    return (err.get("error_user_msg") or err.get("message") or "Meta didn't accept it.")[:300]


def number_info(pnid: str, token: str) -> dict:
    """Meta's messaging limit, phone-app use and current calling settings."""
    import requests
    info = {}
    try:
        h = {"Authorization": f"Bearer {token}"}
        r = requests.get(f"{GRAPH}/{pnid}", params={"fields": "messaging_limit_tier,is_on_biz_app,platform_type,display_phone_number"},
                         headers=h, timeout=10).json()
        if r.get("error"):
            r = requests.get(f"{GRAPH}/{pnid}", params={"fields": "messaging_limit_tier,display_phone_number"}, headers=h, timeout=10).json()
        info.update(tier=r.get("messaging_limit_tier"), on_app=r.get("is_on_biz_app"), display=r.get("display_phone_number"))
        s = requests.get(f"{GRAPH}/{pnid}/settings", headers=h, timeout=10).json()
        info["calling"] = s.get("calling") or {}
        if s.get("error"):
            info["settings_error"] = meta_error(s)
    except Exception as e:
        info["settings_error"] = str(e)
    return info


def card(r: dict) -> dict:
    return {"id": r["id"], "name": r.get("customer_name") or ("+" + r["customer_phone"]),
            "phone": "+" + r["customer_phone"], "status": r["status"], "by": r.get("answered_by_name"),
            "answered_at": r["answered_at"].isoformat() if r.get("answered_at") else None,
            "lead_id": r.get("customer_id"), "readiness": r.get("readiness")}


def recent(tenant_id: int, viewer_id=None, is_admin=True, limit=100) -> list:
    ensure_tables()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    scope = "" if is_admin else " AND (c.assigned_to = %s OR c.assigned_to IS NULL OR c.answered_by = %s)"
    args = [tenant_id] + ([] if is_admin else [viewer_id or -1, viewer_id or -1])
    cur.execute(f"""SELECT c.*, TRIM(COALESCE(s.first_name,'') || ' ' || COALESCE(s.last_name,'')) AS agent_name
                    FROM re_calls c LEFT JOIN re_staff s ON s.id = c.assigned_to
                    WHERE c.tenant_id=%s{scope} ORDER BY c.created_at DESC LIMIT {int(limit)}""", args)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows
