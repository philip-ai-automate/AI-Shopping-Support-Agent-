"""
keyland_alerts_gw.py — KeyLand (home.phixtra.com) staff alerts, sent for the
portal (2026-10-22, Phase 4).

The portal works out who to alert (api-key-manager/estate_alerts.py) and asks
the gateway to deliver the two channels only the gateway can send:
  WhatsApp       from PhiXtra's alert number (PHIXTRA_ALERT_SENDER_PHONE_NUMBER_ID),
                 with the approved wording of type 'keyland_staff_alert' once the
                 user's Meta submission is approved; plain text until then (Meta
                 only delivers that inside a 24-hour window)
  Phone notif.   to devices in re_push_devices (KeyLand's own; the main portal's
                 are in push_devices), same VAPID keys and push services
Bearer PHIXTRA_INTERNAL_TOKEN on both.
"""

import asyncio
import json
import os

import psycopg2.extras
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import push_notify
import staff_alerts
from wa_db import get_db_connection

router = APIRouter()
TEMPLATE_TYPE = "keyland_staff_alert"


def _check(authorization: str):
    token = authorization.removeprefix("Bearer ").strip()
    expected = os.getenv("PHIXTRA_INTERNAL_TOKEN", "")
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


class _WaReq(BaseModel):
    to: str
    params: list[str]
    text: str


@router.post("/internal/keyland/whatsapp")
async def keyland_whatsapp(req: _WaReq, authorization: str = Header(default="")):
    _check(authorization)
    ok = await staff_alerts._send_whatsapp(req.to, TEMPLATE_TYPE, req.params, req.text)
    return {"ok": bool(ok)}


class _PushReq(BaseModel):
    tenant_id: int
    staff_ids: list[int]           # 0 = the agency owner
    title: str
    body: str
    url: str
    tag: str
    endpoint: str | None = None    # just this device (the "Send me a test" button)


def _devices(tenant_id, staff_ids, endpoint=None):
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        q = "SELECT id, staff_id, endpoint, p256dh, auth FROM re_push_devices WHERE tenant_id=%s AND staff_id = ANY(%s)"
        args = [tenant_id, [int(i) for i in staff_ids]]
        if endpoint:
            q += " AND endpoint=%s"
            args.append(endpoint)
        cur.execute(q, args)
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()


def _record(device_id, ok, error="", gone=False):
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        if gone:
            cur.execute("DELETE FROM re_push_devices WHERE id=%s", (device_id,))
        elif ok:
            cur.execute("UPDATE re_push_devices SET last_sent_at=NOW(), fail_count=0, last_error=NULL WHERE id=%s", (device_id,))
        else:
            cur.execute("UPDATE re_push_devices SET fail_count=fail_count+1, last_error=%s WHERE id=%s",
                        ((error or "")[:300], device_id))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _send_one(dev, payload) -> bool:
    from pywebpush import webpush, WebPushException
    if not push_notify.endpoint_ok(dev.get("endpoint")):
        return False
    priv, subject = push_notify._keys()
    try:
        webpush(subscription_info={"endpoint": dev["endpoint"], "keys": {"p256dh": dev["p256dh"], "auth": dev["auth"]}},
                data=json.dumps(payload), vapid_private_key=priv, vapid_claims={"sub": subject},
                ttl=6 * 3600, headers={"Urgency": "high"}, timeout=10)
        _record(dev["id"], True)
        return True
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        _record(dev["id"], False, f"{status} {e}", gone=status in (404, 410))
        print(f"⚠️ [KL PUSH] device={dev['id']} failed ({status}): {str(e)[:160]}")
        return False
    except Exception as e:
        _record(dev["id"], False, str(e))
        print(f"⚠️ [KL PUSH] device={dev['id']} error: {str(e)[:160]}")
        return False


@router.post("/internal/keyland/push")
async def keyland_push(req: _PushReq, authorization: str = Header(default="")):
    _check(authorization)
    if not push_notify.enabled():
        return {"sent": 0, "reached": []}
    devs = _devices(req.tenant_id, req.staff_ids, req.endpoint)
    payload = {"title": req.title[:120], "body": req.body[:180], "url": req.url, "tag": req.tag[:120]}
    results = await asyncio.gather(*(asyncio.to_thread(_send_one, d, payload) for d in devs))
    reached = sorted({d["staff_id"] for d, ok in zip(devs, results) if ok})
    print(f"✅ [KL PUSH] tenant={req.tenant_id} '{payload['title'][:50]}' → {sum(results)}/{len(devs)} device(s)")
    return {"sent": sum(1 for r in results if r), "reached": reached}
