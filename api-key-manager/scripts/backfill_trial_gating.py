"""
backfill_trial_gating.py — one-off correction for tenants created before
trial gating was fixed (see _grant_trial_upgrade in portal_routes.py).

Run from api-key-manager/ so `db` resolves:
    python3 scripts/backfill_trial_gating.py --dry-run
    python3 scripts/backfill_trial_gating.py --apply

What it does:
  1. Strips `cart_recovery` from `features` for every source_type='whatsapp'
     tenant — it can never have functioned without a website widget, so this
     is safe to apply unconditionally.
  2. For WhatsApp tenants sitting on a Pro trial (plan_id=pro or
     trial_ends_at set) with NO active wa_tenants row — i.e. registered but
     never actually connected a number, like the account that surfaced this
     bug — reverts plan_id to free and clears trial_ends_at, leaving
     trial_granted_at NULL so a real trial is granted the moment they do
     connect.
  3. For WhatsApp tenants that DO have an active wa_tenants row, leaves
     plan/trial untouched but backfills trial_granted_at (using
     plan_period_start as the best available proxy for when the trial
     started) so the idempotency guard in _grant_trial_upgrade can't be
     bypassed by a future disconnect/reconnect.
  4. Web tenants are NOT auto-downgraded — some may have been onboarded
     manually by an admin without ever tripping a catalogue sync. Prints a
     report of web tenants on a trial/pro plan with no last_full_sync_at for
     manual review instead.

Always run --dry-run first and read the output before --apply.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db_connection


def _fetch_all(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    group.add_argument("--apply", action="store_true", help="Actually write the changes")
    args = parser.parse_args()

    conn = get_db_connection()
    if not conn:
        print("Could not connect to the database.")
        sys.exit(1)
    cur = conn.cursor()

    # ── 1. Strip cart_recovery from all WhatsApp tenants ────────────────────
    cur.execute("""
        SELECT id, features FROM tenants
        WHERE source_type = 'whatsapp' AND features LIKE '%%cart_recovery%%'
    """)
    wa_with_cart_recovery = cur.fetchall()
    print(f"[1] WhatsApp tenants with cart_recovery in features: {len(wa_with_cart_recovery)}")
    for tid, _features in wa_with_cart_recovery:
        print(f"    tenant {tid}")

    # ── 2 & 3. WhatsApp tenants on a trial, split by connection state ──────
    cur.execute("""
        SELECT t.id, t.name, t.plan_id, t.trial_ends_at, t.plan_period_start, t.trial_granted_at,
               EXISTS (
                   SELECT 1 FROM wa_tenants w WHERE w.tenant_id = t.id AND w.active = TRUE
               ) AS connected
        FROM tenants t
        WHERE t.source_type = 'whatsapp'
          AND (t.trial_ends_at IS NOT NULL
               OR t.plan_id = (SELECT id FROM plans WHERE slug = 'pro' LIMIT 1))
    """)
    wa_trial_tenants = cur.fetchall()

    never_connected = [r for r in wa_trial_tenants if not r[6]]
    connected       = [r for r in wa_trial_tenants if r[6]]

    print(f"\n[2] WhatsApp tenants granted Pro/trial but NEVER connected WhatsApp: {len(never_connected)}")
    for tid, name, plan_id, trial_ends_at, _pps, trial_granted_at, _c in never_connected:
        print(f"    tenant {tid} ({name}) plan_id={plan_id} trial_ends_at={trial_ends_at} -> will revert to free")

    print(f"\n[3] WhatsApp tenants granted Pro/trial AND connected: {len(connected)}")
    for tid, name, plan_id, trial_ends_at, plan_period_start, trial_granted_at, _c in connected:
        action = "already has trial_granted_at" if trial_granted_at else f"will backfill trial_granted_at={plan_period_start}"
        print(f"    tenant {tid} ({name}) plan_id={plan_id} trial_ends_at={trial_ends_at} -> {action}")

    # ── 4. Web tenants — report only, no changes ────────────────────────────
    cur.execute("""
        SELECT id, name, domain, plan_id, trial_ends_at, azure_search_index, last_full_sync_at
        FROM tenants
        WHERE source_type != 'whatsapp'
          AND (trial_ends_at IS NOT NULL
               OR plan_id = (SELECT id FROM plans WHERE slug = 'pro' LIMIT 1))
          AND last_full_sync_at IS NULL
    """)
    web_report = cur.fetchall()
    print(f"\n[4] Web tenants on trial/pro with no catalogue sync yet (REPORT ONLY, not auto-changed): {len(web_report)}")
    for tid, name, domain, plan_id, trial_ends_at, azure_idx, sync_at in web_report:
        print(f"    tenant {tid} ({name}, {domain}) plan_id={plan_id} trial_ends_at={trial_ends_at} "
              f"azure_search_index={'set' if azure_idx else 'unset'} -- review manually")

    if args.dry_run:
        print("\nDry run only — no changes written. Re-run with --apply to write.")
        cur.close(); conn.close()
        return

    # ── Apply ────────────────────────────────────────────────────────────────
    if wa_with_cart_recovery:
        # tenants.features is TEXT, not jsonb — strip via python-side json.
        import json as _json
        for tid, features_raw in wa_with_cart_recovery:
            try:
                features = _json.loads(features_raw or "{}")
            except Exception:
                features = {}
            features.pop("cart_recovery", None)
            cur.execute("UPDATE tenants SET features=%s WHERE id=%s", (_json.dumps(features), tid))
        conn.commit()
        print(f"Applied: stripped cart_recovery from {len(wa_with_cart_recovery)} WhatsApp tenant(s).")

    if never_connected:
        ids = [r[0] for r in never_connected]
        cur.execute("""
            UPDATE tenants
            SET plan_id = (SELECT id FROM plans WHERE slug='free' LIMIT 1),
                trial_ends_at = NULL,
                quota_notified_at = NULL
            WHERE id = ANY(%s)
        """, (ids,))
        conn.commit()
        print(f"Applied: reverted {len(ids)} never-connected WhatsApp tenant(s) to free plan.")

    to_backfill = [r for r in connected if not r[5]]
    if to_backfill:
        for tid, _name, _plan_id, _trial_ends_at, plan_period_start, _trial_granted_at, _c in to_backfill:
            cur.execute(
                "UPDATE tenants SET trial_granted_at=%s WHERE id=%s AND trial_granted_at IS NULL",
                (plan_period_start, tid),
            )
        conn.commit()
        print(f"Applied: backfilled trial_granted_at for {len(to_backfill)} connected WhatsApp tenant(s).")

    cur.close(); conn.close()
    print("\nDone. Web tenants were left untouched — review the [4] report manually.")


if __name__ == "__main__":
    main()
