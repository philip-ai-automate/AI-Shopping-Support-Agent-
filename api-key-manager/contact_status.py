"""Automatic contact Status (2026-10-23).

Status on All Contacts is no longer picked by hand: it follows what has
actually happened with the person, so it always agrees with the Sales
Pipeline, Not a Fit and replies. Stored values stay the same; only the
names shown changed (lead = New, inactive = Not interested).

  customer  a deal is Won, or the business became a paying customer
  prospect  they showed interest: replied on WhatsApp or email (automatic
            and support-ticket replies don't count), booked a demo, a
            reply or meeting was logged, or an open deal is at Qualified
            or later
  inactive  every deal is closed (Not a Fit / Lost) or they unsubscribed,
            and nothing came from them after that
  lead      everyone else

Run every 2 minutes via cron (all businesses):

  cd /root/phixtra-app/api-key-manager && ./venv/bin/python3 contact_status.py

The portal also calls refresh() for one contact when its page is opened.
"""

from __future__ import annotations

import sys

from db import get_db_connection

STATUSES = ("lead", "prospect", "customer", "inactive")
STATUS_LABELS = {"lead": "New", "prospect": "Prospect", "customer": "Customer", "inactive": "Not interested"}
INTEREST_STAGES = ("qualified", "proposal_sent", "negotiating")
AUTO_NOTE = "Status updates by itself from replies, demos, Not a Fit and the deal, so it can't be changed by hand."

# Support desks and out-of-office answers that slipped past the mailbox's own
# automatic-reply check are not interest
_AUTO_REPLY_RE = r"ticket id|support ticket|auto.?reply|automatic reply|out of (the )?office|thank you for contacting our support"

_REFRESH_SQL = """
WITH c AS (
    SELECT id, tenant_id, status,
           GREATEST(CASE WHEN opted_out THEN COALESCE(opted_out_at, 'epoch') END,
                    CASE WHEN email_opted_out THEN COALESCE(email_opted_out_at, 'epoch') END) AS opt_at,
           NULLIF(regexp_replace(COALESCE(phone, ''), '\\D', '', 'g'), '') AS p1,
           NULLIF(regexp_replace(COALESCE(whatsapp_number, ''), '\\D', '', 'g'), '') AS p2
    FROM wa_contacts
    WHERE tenant_id = %(t)s AND (%(ids)s::int[] IS NULL OR id = ANY(%(ids)s::int[]))
),
lm AS (
    SELECT m.*, c.id AS cid FROM merchant_pipeline_leads m
    JOIN c ON c.id = m.wa_contact_id AND m.tenant_id = c.tenant_id
),
l AS (
    SELECT cid,
           bool_or(stage = 'won' OR outcome = 'won' OR converted_portal_tenant_id IS NOT NULL
                   OR converted_school_id IS NOT NULL OR converted_estate_tenant_id IS NOT NULL) AS won,
           bool_or(dropped_at IS NULL) AS has_open,
           bool_or(dropped_at IS NULL AND stage = ANY(%(stages)s)) AS open_interest,
           max(dropped_at) AS closed_at
    FROM lm GROUP BY cid
),
wa AS (
    SELECT regexp_replace(customer_phone, '\\D', '', 'g') AS p, max(created_at) AS at
    FROM wa_message_log WHERE tenant_id = %(t)s AND direction = 'inbound' GROUP BY 1
),
sig AS (
    SELECT c.id AS cid, wa.at FROM c JOIN wa ON wa.p = c.p1
    UNION ALL
    SELECT c.id, wa.at FROM c JOIN wa ON wa.p = c.p2 AND c.p2 IS DISTINCT FROM c.p1
    UNION ALL
    SELECT COALESCE(lm.cid, c.id), r.received_at FROM email_replies r
    LEFT JOIN lm ON lm.id = r.lead_id
    LEFT JOIN c ON c.id = r.contact_id
    WHERE r.tenant_id = %(t)s AND COALESCE(lm.cid, c.id) IS NOT NULL
      AND COALESCE(r.subject, '') !~* %(auto)s AND COALESCE(r.snippet, '') !~* %(auto)s
    UNION ALL
    SELECT COALESCE(lm.cid, c.id), a.created_at FROM staff_activity a
    LEFT JOIN lm ON lm.id = a.lead_id
    LEFT JOIN c ON c.id = a.contact_id
    WHERE a.tenant_id = %(t)s AND a.action IN ('reply_logged', 'meeting_logged')
      AND COALESCE(lm.cid, c.id) IS NOT NULL
    UNION ALL
    SELECT lm.cid, COALESCE(b.booked_at, b.first_seen_at) FROM booking_appointments b
    JOIN lm ON lm.id = b.lead_id
    WHERE b.tenant_id = %(t)s AND b.cancelled_at IS NULL
    UNION ALL
    SELECT lm.cid, d.created_at FROM demo_outcomes d JOIN lm ON lm.id = d.lead_id
    WHERE d.tenant_id = %(t)s
),
i AS (SELECT cid, max(at) AS at FROM sig GROUP BY cid),
s AS (
    SELECT c.id,
           CASE WHEN l.won THEN 'customer'
                WHEN l.open_interest THEN 'prospect'
                WHEN GREATEST(CASE WHEN NOT l.has_open THEN l.closed_at END, c.opt_at) IS NOT NULL
                     AND (i.at IS NULL OR i.at <= GREATEST(CASE WHEN NOT l.has_open THEN l.closed_at END, c.opt_at))
                     THEN 'inactive'
                WHEN i.at IS NOT NULL THEN 'prospect'
                ELSE 'lead' END AS new_status
    FROM c LEFT JOIN l ON l.cid = c.id LEFT JOIN i ON i.cid = c.id
)
UPDATE wa_contacts w SET status = s.new_status
FROM s WHERE w.id = s.id AND w.status IS DISTINCT FROM s.new_status
"""


def refresh(cur, tenant_id: int, contact_ids: list[int] | None = None) -> int:
    """Bring Status up to date for one business (or some of its contacts).
    Returns how many contacts changed. The caller commits."""
    cur.execute(_REFRESH_SQL, {"t": int(tenant_id), "ids": [int(x) for x in contact_ids] if contact_ids else None,
                               "stages": list(INTEREST_STAGES), "auto": _AUTO_REPLY_RE})
    return cur.rowcount


def refresh_all() -> dict:
    """Every business, one short transaction each so nothing is held open."""
    conn = get_db_connection()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT tenant_id FROM wa_contacts")
    changed = {}
    for (tid,) in cur.fetchall():
        try:
            cur.execute("BEGIN")
            cur.execute("SET LOCAL lock_timeout = '5s'")
            n = refresh(cur, tid)
            cur.execute("COMMIT")
            if n:
                changed[tid] = n
        except Exception as e:
            cur.execute("ROLLBACK")
            print(f"tenant {tid}: {e}", file=sys.stderr)
    cur.close(); conn.close()
    return changed


if __name__ == "__main__":
    from datetime import datetime
    result = refresh_all()
    if result:
        print(datetime.now().isoformat(timespec="seconds"), result)
