"""KeyLand automation job (cron, every 5 minutes).

For each agency: fills in automatic next actions (if switched on), checks Meta
approval of follow-up wording, and sends WhatsApp follow-ups that are due (if
switched on). Agencies with both switches off are left alone.

    ./venv/bin/python3 estate_followups_job.py          # normal run
    ./venv/bin/python3 estate_followups_job.py --dry    # work out what would be sent, send nothing
"""
import fcntl
import sys
from datetime import datetime

import psycopg2.extras

import estate_actions as EA
from db import get_db_connection


def main(dry=False):
    lock = open("/tmp/estate_followups_job.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another run is still going, skipped")
        return
    EA.ensure_tables()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT t.id, t.business_name, t.wa_phone_number_id, t.wa_access_token, t.wa_waba_id
                   FROM re_tenants t JOIN re_automation a ON a.tenant_id = t.id
                   WHERE t.status = 'active' AND (a.auto_actions OR a.followups_on)""")
    tenants = cur.fetchall()
    cur.close(); conn.close()
    for t in tenants:
        try:
            n = EA.sync(t["id"])
            if t.get("wa_access_token"):
                EA.refresh_templates(t)
            out = EA.run_tenant(t, dry=dry)
            if n or any(out.values()):
                print(f"{datetime.now():%Y-%m-%d %H:%M} tenant={t['id']} actions_set={n} {out}{' DRY' if dry else ''}")
        except Exception as e:
            print(f"{datetime.now():%Y-%m-%d %H:%M} tenant={t['id']} error: {e}")


if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
