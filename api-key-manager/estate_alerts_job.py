"""KeyLand alerts job (cron, every minute).

For each agency with alerts or the morning summary switched on: closes alerts a
person has acted on, sends hand-over / Ready-buyer alerts and their single
reminder, and sends the morning summary once a day. Agencies with both off are
left alone. The AI backend also starts it for one agency straight after a
hand-over, so that alert doesn't wait for the next minute.

    ./venv/bin/python3 estate_alerts_job.py               # every agency
    ./venv/bin/python3 estate_alerts_job.py --tenant 8    # one agency
"""
import os
import subprocess
import sys
from datetime import datetime

import psycopg2.extras

import estate_alerts as AL
from db import get_db_connection


def _service_token():
    """Use the same internal key the portal service runs with (its systemd
    settings), so WhatsApp and phone notifications through the gateway work
    when this runs from cron or from the AI backend."""
    try:
        out = subprocess.run(["systemctl", "show", "phixtra-portal", "-p", "Environment", "--value"],
                             capture_output=True, text=True, timeout=5).stdout
        for part in out.split():
            if part.startswith("PHIXTRA_INTERNAL_TOKEN="):
                os.environ["PHIXTRA_INTERNAL_TOKEN"] = part.split("=", 1)[1]
    except Exception as e:
        print(f"could not read the portal's internal key: {e}")


def main(only=None):
    _service_token()
    AL.ensure_tables()
    conn = get_db_connection()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT t.id FROM re_tenants t JOIN re_alert_settings s ON s.tenant_id = t.id
                   WHERE t.status = 'active' AND (s.alerts_on OR s.summary_on)""" + (" AND t.id = %s" if only else ""),
                (only,) if only else None)
    tenants = [r["id"] for r in cur.fetchall()]
    cur.close(); conn.close()
    for tid in tenants:
        try:
            out = AL.run_tenant(tid)
            n = AL.run_summary(tid)
            if any(out.values()) or n:
                print(f"{datetime.now():%Y-%m-%d %H:%M:%S} tenant={tid} {out} summary={n}")
        except Exception as e:
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} tenant={tid} error: {e}")


if __name__ == "__main__":
    t = None
    if "--tenant" in sys.argv:
        t = int(sys.argv[sys.argv.index("--tenant") + 1])
    main(t)
