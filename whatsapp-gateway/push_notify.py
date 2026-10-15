"""
push_notify.py — phone notifications (web push) for staff chat alerts (2026-10-11).

A third way to deliver the chat alerts in staff_alerts.py, beside WhatsApp and
email. Each person turns it on per device in the portal (My Settings for staff,
Settings › Notifications for the owner); the portal saves the device in
push_devices. Team members get notifications for the same chats they get
chat alerts for; the owner's devices get every chat alert. Free to run: the
notifications go through the browser makers' push services (Google, Apple,
Mozilla, Microsoft). No OpenAI.

Env (gateway .env): VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY (base64url, raw),
VAPID_SUBJECT (mailto:). The private key lives only here.
"""

import asyncio
import json
import os

import psycopg2.extras

from wa_db import get_db_connection

_PORTAL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")


def _keys():
    return (os.getenv("VAPID_PRIVATE_KEY", "").strip(),
            os.getenv("VAPID_SUBJECT", "mailto:support@phixtra.com").strip())


def enabled() -> bool:
    return bool(_keys()[0])


def has_owner_devices(cur, tenant_id: int) -> bool:
    """Has the account owner turned on phone notifications on any device?"""
    try:
        cur.execute("SELECT 1 FROM push_devices WHERE tenant_id=%s AND team_member_id IS NULL LIMIT 1",
                    (tenant_id,))
        return bool(cur.fetchone())
    except Exception:
        return False


def _devices(tenant_id: int, member_ids, include_owner: bool, only_endpoint: str = None) -> list:
    conn = get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        ids = [int(i) for i in (member_ids or []) if i]
        q = """SELECT id, endpoint, p256dh, auth FROM push_devices
                WHERE tenant_id=%s AND (team_member_id = ANY(%s) OR (%s AND team_member_id IS NULL))"""
        args = [tenant_id, ids, include_owner]
        if only_endpoint:
            q += " AND endpoint = %s"
            args.append(only_endpoint)
        cur.execute(q, args)
        return [dict(r) for r in cur.fetchall() or []]
    except Exception as e:
        print(f"⚠️ [PUSH] device lookup tenant={tenant_id}: {e}")
        return []
    finally:
        cur.close(); conn.close()


def _record(device_id: int, ok: bool, error: str = "", gone: bool = False) -> None:
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        if gone:
            # The browser says this device no longer accepts notifications
            # (uninstalled, permission removed, expired): forget it.
            cur.execute("DELETE FROM push_devices WHERE id=%s", (device_id,))
        elif ok:
            cur.execute("UPDATE push_devices SET last_sent_at=NOW(), fail_count=0, last_error=NULL WHERE id=%s",
                        (device_id,))
        else:
            cur.execute("UPDATE push_devices SET fail_count=fail_count+1, last_error=%s WHERE id=%s",
                        ((error or "")[:300], device_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"⚠️ [PUSH] record device={device_id}: {e}")
    finally:
        cur.close(); conn.close()


_PUSH_HOSTS = ("fcm.googleapis.com", "push.apple.com", "push.services.mozilla.com",
               "notify.windows.com", "updates.push.services.mozilla.com")


def endpoint_ok(endpoint: str) -> bool:
    """Only the browser makers' notification services (same list as the
    portal) — never send a request anywhere else."""
    from urllib.parse import urlparse
    try:
        u = urlparse(endpoint or "")
    except Exception:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in _PUSH_HOSTS)


def _send_one(dev: dict, payload: dict) -> bool:
    from pywebpush import webpush, WebPushException
    if not endpoint_ok(dev.get("endpoint")):
        print(f"⚠️ [PUSH] device={dev.get('id')} skipped: not a known notification service")
        return False
    priv, subject = _keys()
    try:
        webpush(subscription_info={"endpoint": dev["endpoint"],
                                   "keys": {"p256dh": dev["p256dh"], "auth": dev["auth"]}},
                data=json.dumps(payload),
                vapid_private_key=priv,
                vapid_claims={"sub": subject},
                ttl=6 * 3600,
                headers={"Urgency": "high"},
                timeout=10)
        _record(dev["id"], True)
        return True
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        _record(dev["id"], False, f"{status} {e}", gone=status in (404, 410))
        print(f"⚠️ [PUSH] device={dev['id']} failed ({status}): {str(e)[:160]}")
        return False
    except Exception as e:
        _record(dev["id"], False, str(e))
        print(f"⚠️ [PUSH] device={dev['id']} error: {str(e)[:160]}")
        return False


async def push_to(tenant_id: int, member_ids, include_owner: bool, *, title: str, body: str,
                  url: str, tag: str, only_endpoint: str = None) -> int:
    """Send one notification to these people's devices. Returns how many
    devices accepted it. Never raises."""
    if not enabled():
        return 0
    devs = _devices(tenant_id, member_ids, include_owner, only_endpoint)
    if not devs:
        return 0
    payload = {"title": (title or "PhiXtra")[:120], "body": (body or "")[:180],
               "url": url if url.startswith("http") else _PORTAL + url, "tag": tag[:120]}
    results = await asyncio.gather(*(asyncio.to_thread(_send_one, d, payload) for d in devs))
    sent = sum(1 for r in results if r)
    print(f"✅ [PUSH] tenant={tenant_id} '{payload['title'][:50]}' → {sent}/{len(devs)} device(s)")
    return sent


def chat_url(channel: str, chat_key: str) -> str:
    """Inbox with this conversation open."""
    from urllib.parse import quote
    key = ("web:" + chat_key) if channel == "web" else chat_key
    return "/inbox?phone=" + quote(key, safe="")


# ── "Send me a test" from the portal ─────────────────────────────────────────
# The portal has no push library or private key; it asks the gateway.
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter()


class _TestReq(BaseModel):
    tenant_id: int
    team_member_id: int | None = None   # None = the owner
    endpoint: str | None = None         # just this device, else all of theirs


@router.post("/internal/push-test")
async def push_test(req: _TestReq, authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    expected = os.getenv("PHIXTRA_INTERNAL_TOKEN", "")
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    sent = await push_to(req.tenant_id,
                         [req.team_member_id] if req.team_member_id else [],
                         req.team_member_id is None,
                         title="PhiXtra test notification",
                         body="Phone notifications are working on this device.",
                         url="/inbox", tag="phixtra-test",
                         only_endpoint=req.endpoint or None)
    return {"sent": sent}
