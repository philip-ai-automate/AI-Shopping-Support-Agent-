"""
send_prospect_campaign.py — one-off outbound WhatsApp sales campaign sender.

Sends the "PhiXtra AI Sales Agent" video pitch to prospect contacts stored in
wa_contacts, personalized per-contact via the `personalization_note` column
(added by the migration in portal_migrations.py).

Unlike the merchant-facing Campaigns feature (_send_campaign_now in
portal_routes.py), which sends one identical message to every recipient, this
script sends ONE API call PER CONTACT so each message can carry that
contact's own {{2}} body variable.

Safe to re-run: only ever selects contacts that (a) have a personalization
note ready and (b) do not already have the 'messaged' tag, so nothing is
ever sent twice.

── BEFORE THIS CAN RUN, FILL IN ──────────────────────────────────────────────
  TENANT_ID      — the dedicated sales-outreach tenant/number's tenant_id
                   (looked up from wa_tenants once that number is registered)
  TEMPLATE_NAME  — the exact name of the Meta-approved MARKETING template
                   (video header, body has 2 variables: {{1}} name, {{2}} note)
  LANGUAGE_CODE  — the language code the template was approved under (e.g. "en")
  VIDEO_URL      — permanent public HTTPS link to the MP4
Until these are set, running this script exits immediately with a clear error
instead of doing anything.
──────────────────────────────────────────────────────────────────────────────
"""
import os
import sys
import time
import argparse

import requests
import psycopg2.extras

from db import get_db_connection

# ── Fill these in before running ─────────────────────────────────────────────
TENANT_ID     = None   # e.g. 42
TEMPLATE_NAME = None   # e.g. "phixtra_ai_sales_agent_pitch"
LANGUAGE_CODE = None   # e.g. "en"
VIDEO_URL     = "https://portal.phixtra.com/static/portal/tutorial/videos/ai-sales-agent-promo-tutorial.mp4"
# ──────────────────────────────────────────────────────────────────────────────

GRAPH_URL   = os.getenv("META_GRAPH_URL", "https://graph.facebook.com/v19.0")
DEFAULT_BATCH_LIMIT = 50   # matches the ~50/week outreach cadence
SEND_DELAY_SECONDS  = 1.0  # small pacing gap between sends


def _get_tenant_credentials(cur, tenant_id: int):
    cur.execute(
        "SELECT phone_number_id, access_token FROM wa_tenants "
        "WHERE tenant_id=%s AND active=TRUE",
        (tenant_id,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"No active wa_tenants row for tenant_id={tenant_id}")
    return row["phone_number_id"], row["access_token"]


def _fetch_batch(cur, tenant_id: int, limit: int):
    cur.execute(
        """
        SELECT id, phone, display_name, personalization_note
        FROM wa_contacts
        WHERE tenant_id = %s
          AND personalization_note IS NOT NULL
          AND personalization_note <> ''
          AND NOT ('messaged' = ANY(tags))
        ORDER BY id
        LIMIT %s
        """,
        (tenant_id, limit),
    )
    return cur.fetchall()


def _send_one(phone_number_id: str, access_token: str, to_phone: str,
              display_name: str, personalization_note: str) -> tuple[bool, str]:
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone.lstrip("+"),
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": LANGUAGE_CODE},
            "components": [
                {
                    "type": "header",
                    "parameters": [{"type": "video", "video": {"link": VIDEO_URL}}],
                },
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": display_name or "there"},
                        {"type": "text", "text": personalization_note},
                    ],
                },
            ],
        },
    }
    resp = requests.post(
        f"{GRAPH_URL}/{phone_number_id}/messages",
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    if resp.status_code == 200:
        return True, ""
    return False, resp.text[:400]


def _mark_messaged(cur, conn, contact_id: int):
    cur.execute(
        "UPDATE wa_contacts SET tags = array_append(tags, 'messaged'), "
        "updated_at = NOW() WHERE id = %s AND NOT ('messaged' = ANY(tags))",
        (contact_id,),
    )
    conn.commit()


def _log_outbound(cur, conn, tenant_id: int, phone_number_id: str, to_phone: str):
    try:
        cur.execute(
            """INSERT INTO wa_message_log
                   (tenant_id, phone_number_id, customer_phone, direction, content, message_type)
               VALUES (%s, %s, %s, 'outbound', %s, 'campaign')""",
            (tenant_id, phone_number_id, to_phone,
             f"📣 Prospect campaign: \"{TEMPLATE_NAME}\""),
        )
        conn.commit()
    except Exception as e:
        print(f"⚠️  wa_message_log insert failed (non-fatal): {e}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_LIMIT,
                         help=f"max contacts to message this run (default {DEFAULT_BATCH_LIMIT})")
    parser.add_argument("--dry-run", action="store_true",
                         help="print what would be sent without calling the Graph API or tagging anything")
    args = parser.parse_args()

    missing = [n for n, v in [("TENANT_ID", TENANT_ID), ("TEMPLATE_NAME", TEMPLATE_NAME),
                               ("LANGUAGE_CODE", LANGUAGE_CODE), ("VIDEO_URL", VIDEO_URL)] if not v]
    if missing:
        sys.exit(f"❌ Not configured yet — fill in {', '.join(missing)} at the top of "
                  f"send_prospect_campaign.py before running.")

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    phone_number_id, access_token = _get_tenant_credentials(cur, TENANT_ID)
    batch = _fetch_batch(cur, TENANT_ID, args.limit)

    if not batch:
        print("Nothing to send — no contacts with a personalization_note that aren't already tagged 'messaged'.")
        cur.close(); conn.close()
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Sending to {len(batch)} contact(s)...")
    sent = failed = 0
    for contact in batch:
        to_phone = contact["phone"]
        name = contact["display_name"] or "there"
        note = contact["personalization_note"]

        if args.dry_run:
            print(f"  would send to {to_phone} ({name}) — note: {note!r}")
            continue

        ok, err = _send_one(phone_number_id, access_token, to_phone, name, note)
        if ok:
            sent += 1
            _mark_messaged(cur, conn, contact["id"])
            _log_outbound(cur, conn, TENANT_ID, phone_number_id, to_phone)
            print(f"  ✅ sent to {to_phone} ({name})")
        else:
            failed += 1
            print(f"  ⚠️  failed to {to_phone} ({name}): {err}")

        time.sleep(SEND_DELAY_SECONDS)

    cur.close(); conn.close()
    if not args.dry_run:
        print(f"Done. sent={sent} failed={failed}")


if __name__ == "__main__":
    main()
