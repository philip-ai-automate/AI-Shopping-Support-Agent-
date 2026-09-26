"""
social_analytics.py — Social Posts › Analytics (2026-09-26).

Buffer keeps numbers (reach, impressions, reactions, …) for each post it
sent, per account. We copy them into social_post_metrics so the page opens
without waiting on Buffer, and ask Buffer again when they're more than
STALE old or when someone clicks "Get the latest numbers".

Only posts made in PhiXtra are matched (by the Buffer post ids saved on
tenant_social_posts.buffer_post_ids). Which numbers exist depends on the
network — a missing type means "that network doesn't give it", never zero.
Buffer refreshes them on its own schedule (often days later), not live.
"""
import json
from datetime import datetime, timezone, timedelta

import psycopg2.extras

import buffer_accounts as ba
from buffer_client import BufferAPIError, buffer_sent_posts
from db import get_db_connection

STALE = timedelta(hours=6)
MAX_PAGES = 10          # 1,000 sent posts back through Buffer's list, newest first

# Buffer's metric types in the order shown, with the words used on the page.
METRICS = [
    ("reach", "Reach", "How many different people saw it"),
    ("impressions", "Impressions", "How many times it was seen, counting repeat views"),
    ("views", "Views", "Video views"),
    ("reactions", "Reactions", "Likes and other reactions"),
    ("likes", "Likes", "Likes"),
    ("comments", "Comments", "Comments"),
    ("shares", "Shares", "Shares or reposts"),
    ("saves", "Saves", "Times people saved it"),
    ("clicks", "Clicks", "Clicks on the post or its link"),
    ("engagementRate", "Engagement rate", "Reactions, comments, shares and clicks as a share of the people who saw it"),
]
RATE = "engagementRate"


def label_for(kind: str) -> str:
    for k, label, _ in METRICS:
        if k == kind:
            return label
    # A type Buffer adds later: "linkClicks" -> "Link clicks"
    out = "".join(" " + c.lower() if c.isupper() else c for c in kind).strip()
    return out[:1].upper() + out[1:]


def order(kinds) -> list:
    known = [k for k, _, _ in METRICS if k in kinds]
    return known + sorted(k for k in kinds if k not in known)


def checked_at(tenant_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT metrics_checked_at FROM social_settings WHERE tenant_id=%s", (tenant_id,))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    return row[0] if row else None


def _stamp(tenant_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO social_settings (tenant_id, metrics_checked_at) VALUES (%s, NOW())
                       ON CONFLICT (tenant_id) DO UPDATE SET metrics_checked_at=NOW()""", (tenant_id,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _wanted(tenant_id: int) -> dict:
    """{buffer post id: (our post id, channel id)} for every post that went out."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT id, buffer_post_ids FROM tenant_social_posts
                       WHERE tenant_id=%s AND status IN ('sent','partial') AND buffer_post_ids IS NOT NULL""",
                    (tenant_id,))
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    out = {}
    for r in rows:
        ids = r["buffer_post_ids"]
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except ValueError:
                ids = {}
        for channel_id, buf_id in (ids or {}).items():
            if buf_id:
                out[str(buf_id)] = (r["id"], channel_id)
    return out


def refresh(tenant_id: int, force: bool = False):
    """Copies Buffer's latest numbers for this business's sent posts.
    Returns None when fine (or not due yet), else a message to show."""
    if not force:
        last = checked_at(tenant_id)
        if last and datetime.now(timezone.utc) - last < STALE:
            return None
    wanted = _wanted(tenant_id)
    if not wanted:
        _stamp(tenant_id)
        return None
    owner = ba.tenant_owner(tenant_id)
    acct = ba.get_account(owner)
    if not acct or not acct.get("organization_id"):
        return "Buffer isn't connected, so the numbers couldn't be updated."
    channels = sorted({c for _, c in wanted.values()})
    found, after = {}, None
    try:
        for _ in range(MAX_PAGES):
            posts, after = ba.call(owner, buffer_sent_posts, acct["organization_id"], channels, after)
            for p in posts:
                if p.get("id") in wanted:
                    found[p["id"]] = p
            if not after or len(found) == len(wanted):
                break
    except BufferAPIError as e:
        if found:
            _save(tenant_id, wanted, found)
        return f"Buffer didn't answer, so these are the numbers from the last check. ({e})"
    _save(tenant_id, wanted, found)
    _stamp(tenant_id)
    return None


def _save(tenant_id: int, wanted: dict, found: dict):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        for buf_id, p in found.items():
            post_id, channel_id = wanted[buf_id]
            metrics = {m["type"]: m["value"] for m in (p.get("metrics") or [])
                       if m.get("type") and m.get("value") is not None}
            cur.execute("""INSERT INTO social_post_metrics (tenant_id, buffer_post_id, post_id, channel_id, service,
                                                            metrics, metrics_updated_at, fetched_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                           ON CONFLICT (tenant_id, buffer_post_id) DO UPDATE SET
                               post_id=EXCLUDED.post_id, channel_id=EXCLUDED.channel_id, service=EXCLUDED.service,
                               metrics=EXCLUDED.metrics, metrics_updated_at=EXCLUDED.metrics_updated_at, fetched_at=NOW()""",
                        (tenant_id, buf_id, post_id, channel_id, p.get("channelService"),
                         json.dumps(metrics), p.get("metricsUpdatedAt")))
        conn.commit()
    finally:
        cur.close(); conn.close()


def stored(tenant_id: int, post_ids) -> dict:
    """{(post id, channel id): {"metrics": {...}, "updated": datetime}}"""
    if not post_ids:
        return {}
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT post_id, channel_id, metrics, metrics_updated_at FROM social_post_metrics
                       WHERE tenant_id=%s AND post_id = ANY(%s)""", (tenant_id, list(post_ids)))
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    return {(r["post_id"], r["channel_id"]): {"metrics": r["metrics"] or {}, "updated": r["metrics_updated_at"]}
            for r in rows}
