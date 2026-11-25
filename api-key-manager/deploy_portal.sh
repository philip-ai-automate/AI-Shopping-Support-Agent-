#!/bin/bash
# deploy_portal.sh — put portal code changes live without cutting anyone off (16 Nov 2026).
#   1. shows who is online   2. checks the Python files compile + the Plan check
#   3. database updates only if --migrate (3-second lock limit, retries)
#   4. gentle reload: new workers start, old ones finish what they are doing
#   5. checks the portal answers
# Page templates (.html) need none of this: they go live when saved.
set -u
cd "$(dirname "$0")"
UNIT="${PORTAL_UNIT:-phixtra-portal}"
PY=venv/bin/python

echo "── Who is online"
$PY portal_presence.py 2>/dev/null | tail -10

echo "── Checking code"
$PY -m py_compile portal_app.py db.py run_migrations.py *_routes.py || { echo "❌ Code error, nothing changed."; exit 1; }

# Plans must follow the portal (plan_catalog.py, 25 Nov 2026): a menu page with
# no plan tick, or a tick with no page, stops the update here.
echo "── Plan check"
$PY plan_check_cli.py || { echo "❌ Plan check failed, nothing reloaded. See above, or Admin › Plans › Plan check."; exit 1; }

if [ "${1:-}" = "--migrate" ]; then
  echo "── Database updates"
  $PY run_migrations.py || { echo "❌ Database updates blocked, nothing reloaded."; exit 1; }
fi

echo "── Gentle reload of $UNIT"
systemctl reload "$UNIT" || { echo "❌ Reload failed."; exit 1; }

for i in $(seq 1 30); do
  sleep 2
  code=$(curl -s -o /dev/null -m 5 -w "%{http_code}" http://127.0.0.1:5055/login)
  if [ "$code" = "200" ]; then echo "✅ Portal answering (${i}×2s)."; exit 0; fi
done
echo "❌ Portal not answering after 60s — check: journalctl -u $UNIT -n 50"
exit 1
