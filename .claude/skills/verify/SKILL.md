---
name: verify
description: Verify changes to the PhiXtra Flask portal (api-key-manager) by driving the live app over real HTTP on a scratch port, without touching production.
---

# Verifying api-key-manager (Flask portal) changes

The production app runs as `phixtra-portal.service` (gunicorn, 2 workers,
bound to `127.0.0.1:5055`, serving portal.phixtra.com/school.phixtra.com/
home.phixtra.com/phixtra.com). **Never restart this service to verify a
change** — it drops real user connections. Instead run a second, temporary
instance of the same app on a scratch port against the *same* database.

## Launch a scratch instance

Write a tiny launcher script (inline `python -c` over `nohup ... &` is
unreliable in this environment — background job launches silently die; use a
script file executed directly in the background instead):

```python
# /tmp/.../run_verify_server.py
import os
os.environ["TURNSTILE_SECRET_KEY"] = ""   # disable Cloudflare Turnstile for this process only
os.chdir("/root/phixtra-app/api-key-manager")
import sys; sys.path.insert(0, "/root/phixtra-app/api-key-manager")
from portal_app import app
app.run(host="127.0.0.1", port=5098, debug=False)
```

```bash
/root/phixtra-app/api-key-manager/venv/bin/python3 /tmp/.../run_verify_server.py \
  > /tmp/.../verify_server.log 2>&1 &
disown
sleep 2
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5098/ambassador/register
```

Pick a port and confirm nothing else already owns it first (`ss -tlnp | grep
<port>`) — ports in the 5090s have been squatted by stray leftover processes
before.

`TURNSTILE_SECRET_KEY=""` only needs to be unset for registration-flow
(`/ambassador/register` POST) testing — `_verify_turnstile()` in
`ambassador_routes.py` fails open (returns True) when the secret is empty.
Every other route is unaffected.

## Auth: forge real signed session cookies for disposable test rows

`portal_admin_routes.py` gates on `session["portal_admin_logged_in"] is
True`; `ambassador_routes.py` gates on `session["ambassador_logged_in"]` +
`session["ambassador_id"]`. There's no throwaway admin/ambassador login
available for scripting, so sign a real Flask session cookie using the app's
own `PORTAL_SECRET_KEY` (from `.env`) rather than faking auth some other way:

```python
from portal_app import app
def make_cookie(d):
    s = app.session_interface.get_signing_serializer(app)
    return s.dumps(d)

admin_cookie = make_cookie({"portal_admin_logged_in": True, "portal_admin_username": "verify-script"})
amb_cookie   = make_cookie({"ambassador_logged_in": True, "ambassador_id": <disposable_id>})
```

Then `curl --cookie "session=$COOKIE" ...` against the scratch port. This is
legitimate — it's the app signing its own session for a test row you created
yourself, not forging anyone else's session.

## Test data discipline

Always use brand-new disposable rows (never reuse `test@phixtra.com` id=1 or
any other real/live account — see [[feedback memory]] on this). Ambassador
approval auto-creates a **demo tenant** (`ambassador_demo.create_ambassador_demo`)
as a side effect — clean those up too, they don't get removed by deleting the
ambassador row:

```sql
DELETE FROM tenants WHERE ref_code IN (SELECT ref_code FROM ambassadors WHERE id = ANY(%s));
DELETE FROM lead_stage_history WHERE lead_id IN (SELECT id FROM ambassador_leads WHERE ambassador_id = ANY(%s));
DELETE FROM ambassador_leads WHERE ambassador_id = ANY(%s);
DELETE FROM ambassador_commissions WHERE ambassador_id = ANY(%s);
DELETE FROM ambassador_products WHERE ambassador_id = ANY(%s);
UPDATE ambassadors SET recruited_by_id=NULL WHERE recruited_by_id = ANY(%s);  -- drop FK refs first
DELETE FROM ambassadors WHERE id = ANY(%s);
```

Verify cleanup by diffing `SELECT ambassador_id,product,status FROM
ambassador_products ORDER BY ambassador_id` against a pre-test snapshot.

## Kill the scratch server when done

`pkill -f run_verify_server.py` (the exact script path, not the port number —
`pkill -f 5098` can false-match unrelated processes with that string
anywhere in their args).

## Gotchas found in practice

- `portal_migrations.py`'s `ensure_portal_tables()` runs on every app
  startup/import (including importing `portal_app` in a test script) — any
  migration bug there reproduces just by importing the module, no HTTP
  request needed.
- Flask's dev server (`app.run()`) prints a "development server" warning to
  stderr — harmless, expected, not a finding.
