"""
lead_tags_unify_backfill.py — one-time (but safe to re-run) carry-over of the
old free-text Contact tags (wa_contacts.tags, a text[] array) into the shared
tag system Contacts now use alongside Sales Pipeline deals (lead_labels +
the new lead_label_contacts link table). See _sync_contact_tags() in
portal_routes.py for how new tagging is written going forward.

Run by hand on the server AFTER the new lead_label_contacts table exists
(portal_migrations.py's ensure_portal_tables() has run — i.e. after
phixtra-portal.service has been restarted at least once with the new
migration in place):

    python3 lead_tags_unify_backfill.py            # dry run — prints counts only
    python3 lead_tags_unify_backfill.py --apply     # writes the links

Nothing here ever touches or clears wa_contacts.tags — it's left in place,
frozen, as a safety net. This only reads it once to create matching
lead_labels rows (reusing one by the same name, case-insensitive, if it
already exists) and link each tagged contact to it. Safe to re-run: link
inserts are ON CONFLICT DO NOTHING, and a value already carried over is
just re-confirmed, not duplicated.
"""
import argparse

import psycopg2.extras

from db import get_db_connection


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT tenant_id, unnest(tags) AS tag_name, array_agg(id) AS contact_ids
        FROM wa_contacts
        WHERE tags IS NOT NULL AND array_length(tags, 1) > 0
        GROUP BY tenant_id, tag_name
        ORDER BY tenant_id, tag_name
    """)
    rows = cur.fetchall()

    if not rows:
        print("Nothing to carry over — no wa_contacts.tags values found.")
        cur.close(); conn.close()
        return

    total_tags = 0
    total_links = 0
    for row in rows:
        tenant_id, tag_name, contact_ids = row["tenant_id"], row["tag_name"], row["contact_ids"]
        print(f"Tenant {tenant_id}: tag '{tag_name}' — {len(contact_ids)} contact(s)")
        total_tags += 1
        total_links += len(contact_ids)

        if not args.apply:
            continue

        cur.execute(
            "SELECT id FROM lead_labels WHERE tenant_id=%s AND lower(name)=lower(%s)",
            (tenant_id, tag_name),
        )
        existing = cur.fetchone()
        if existing:
            label_id = existing["id"]
        else:
            cur.execute(
                "INSERT INTO lead_labels (tenant_id, name) VALUES (%s, %s) RETURNING id",
                (tenant_id, tag_name),
            )
            label_id = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO lead_label_contacts (label_id, contact_id) "
            "SELECT %s, unnest(%s::int[]) "
            "ON CONFLICT (label_id, contact_id) DO NOTHING",
            (label_id, contact_ids),
        )

    print(f"\n{total_tags} distinct tag(s) across all tenants, {total_links} contact-tag link(s) total.")
    if args.apply:
        conn.commit()
        print("Applied.")
    else:
        print("Dry run only — re-run with --apply to write these links.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
