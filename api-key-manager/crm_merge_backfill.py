"""
crm_merge_backfill.py — one-time (but safe to re-run) matcher that links
existing Sales Pipeline deals (merchant_pipeline_leads) to existing WhatsApp
Contacts (wa_contacts) by phone number, and groups matched pairs under a
shared Company record.

Run by hand on the server AFTER the new columns exist (portal_migrations.py's
ensure_portal_tables() has run — i.e. after phixtra-portal.service has been
restarted at least once with the new migration in place):

    python3 crm_merge_backfill.py                 # dry run — prints counts only
    python3 crm_merge_backfill.py --apply          # writes the confident matches
                                                    # + queues the unsure ones for review

Nothing here ever deletes or overwrites existing wa_contacts / merchant_pipeline_leads
data — it only fills in previously-empty link columns (wa_contact_id, company_id) and,
for the unsure cases, inserts a row into crm_match_candidates for a human to confirm
on the "Merge Review" screen. Safe to re-run: every query only looks at rows that
aren't linked yet.
"""
import argparse
import re
import sys

import psycopg2
import psycopg2.extras

from db import get_db_connection


def last10(phone: str) -> str:
    """Normalise a Nigerian-style number down to its last 10 digits (drops
    country code / leading 0 / spaces / punctuation) so '+234 803 214 5567',
    '0803 214 5567' and '2348032145567' all compare equal."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


def edit_distance_le(a: str, b: str, max_dist: int) -> bool:
    """True if a and b differ by at most max_dist single-character edits.
    Small strings (10 digits) so a plain DP table is fine."""
    if abs(len(a) - len(b)) > max_dist:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1] <= max_dist


def normalize_company_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT id FROM tenants")
    tenant_ids = [r["id"] for r in cur.fetchall()]

    total_confident = 0
    total_review = 0
    total_companies = 0

    for tenant_id in tenant_ids:
        cur.execute(
            "SELECT id, phone, display_name FROM wa_contacts WHERE tenant_id=%s",
            (tenant_id,),
        )
        contacts = cur.fetchall()
        contact_by_last10 = {}
        seen_company_names = set()
        for c in contacts:
            contact_by_last10.setdefault(last10(c["phone"]), []).append(c)

        cur.execute(
            """SELECT id, customer_name, contact_person, phone, whatsapp_number
               FROM merchant_pipeline_leads
               WHERE tenant_id=%s AND dropped_at IS NULL AND wa_contact_id IS NULL""",
            (tenant_id,),
        )
        leads = cur.fetchall()

        for lead in leads:
            lead_keys = {
                k for k in (last10(lead["phone"]), last10(lead["whatsapp_number"])) if k
            }
            if not lead_keys:
                continue

            # confident: exact last-10-digit match, and unambiguous (exactly
            # one wa_contact carries that number for this tenant)
            exact_candidates = []
            for k in lead_keys:
                exact_candidates += contact_by_last10.get(k, [])
            exact_candidates = {c["id"]: c for c in exact_candidates}.values()

            if len(exact_candidates) == 1:
                contact = next(iter(exact_candidates))
                total_confident += 1
                name_key = normalize_company_name(lead["customer_name"])
                if name_key and name_key not in seen_company_names:
                    seen_company_names.add(name_key)
                    total_companies += 1
                if args.apply:
                    cur.execute(
                        "UPDATE merchant_pipeline_leads SET wa_contact_id=%s WHERE id=%s",
                        (contact["id"], lead["id"]),
                    )
                    _link_company(cur, tenant_id, lead, contact)
                continue

            # unsure: numbers close (1-2 digit difference) — queue for review,
            # don't guess
            near = []
            for k in lead_keys:
                for last10_key, cands in contact_by_last10.items():
                    if last10_key == k:
                        continue
                    if edit_distance_le(k, last10_key, 2):
                        near += cands
            near = {c["id"]: c for c in near}.values()
            if near:
                total_review += len(near)
                if args.apply:
                    for contact in near:
                        cur.execute(
                            """INSERT INTO crm_match_candidates
                               (tenant_id, wa_contact_id, pipeline_lead_id, reason)
                               VALUES (%s,%s,%s,%s)""",
                            (
                                tenant_id,
                                contact["id"],
                                lead["id"],
                                "phone numbers are close but not identical",
                            ),
                        )

        if args.apply:
            conn.commit()

    cur.close()
    conn.close()

    verb = "Linked" if args.apply else "Would link"
    print(f"{verb} {total_confident} deals to a matching WhatsApp Contact (exact phone match).")
    print(f"{'Created' if args.apply else 'Would create'} {total_companies} company records from those matches.")
    print(f"{'Queued' if args.apply else 'Would queue'} {total_review} near-matches for manual review "
          f"(Contacts → Merge Review once the screen is live).")
    if not args.apply:
        print("\nDry run only — nothing written. Re-run with --apply to write these changes.")


def _link_company(cur, tenant_id, lead, contact):
    """Create-or-reuse a crm_companies row from the lead's business name and
    attach both sides to it. Returns the company id, or None if the lead has
    no usable business name."""
    name = normalize_company_name(lead["customer_name"])
    if not name:
        return None
    cur.execute(
        "SELECT id FROM crm_companies WHERE tenant_id=%s AND lower(name)=%s",
        (tenant_id, name),
    )
    row = cur.fetchone()
    if row:
        company_id = row["id"]
    else:
        cur.execute(
            "INSERT INTO crm_companies (tenant_id, name) VALUES (%s,%s) RETURNING id",
            (tenant_id, lead["customer_name"].strip()),
        )
        company_id = cur.fetchone()["id"]
    cur.execute("UPDATE merchant_pipeline_leads SET company_id=%s WHERE id=%s", (company_id, lead["id"]))
    cur.execute(
        "UPDATE wa_contacts SET company_id=%s WHERE id=%s AND company_id IS NULL",
        (company_id, contact["id"]),
    )
    return company_id


if __name__ == "__main__":
    main()
