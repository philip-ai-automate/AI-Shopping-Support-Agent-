"""
run_migrations.py — the portal's database updates, run on purpose instead of on
every start (16 Nov 2026). Every table lock waits at most 3 seconds; if a lock
isn't free the update gives up, waits, and tries again (all updates are
idempotent), so it can never freeze the live portal.

    venv/bin/python run_migrations.py          # usually via deploy_portal.sh
"""
import io
import sys
import time
import contextlib

from dotenv import load_dotenv

load_dotenv()

TRIES = 5
WAIT = 20  # seconds between tries


def _steps():
    from portal_migrations import (ensure_portal_tables, ensure_b2b_tables, ensure_coexist_tables,
                                   ensure_wa_usage_column, ensure_handoff_text_columns, ensure_inbox_done_table)
    from school_migrations import ensure_school_tables
    import shopify_app
    import about_business   # AI Agent › About your business (22 Nov 2026)
    return [ensure_portal_tables, ensure_b2b_tables, ensure_coexist_tables, ensure_wa_usage_column,
            ensure_handoff_text_columns, ensure_school_tables, shopify_app.ensure_tables, ensure_inbox_done_table,
            about_business.ensure_tables, about_business.update_default_wording]


def _run_once():
    """Runs every step; returns the printed lines that mention a lock time-out."""
    from db import migration_lock_limit
    buf = io.StringIO()
    with migration_lock_limit(), contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        for step in _steps():
            try:
                step()
            except Exception as e:  # the steps catch their own errors; just in case
                print(f"{step.__name__}: {e}")
    out = buf.getvalue()
    sys.stdout.write(out)
    return [l for l in out.splitlines() if "lock timeout" in l.lower() or "deadlock" in l.lower()]


def run_all():
    for n in range(1, TRIES + 1):
        blocked = _run_once()
        if not blocked:
            print(f"✅ Database updates done (try {n}).")
            return True
        print(f"⏳ Try {n}: a table was busy, nothing was frozen. Waiting {WAIT}s…")
        if n < TRIES:
            time.sleep(WAIT)
    print("❌ Database updates still blocked after all tries. The portal is fine; run again later.")
    return False


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
