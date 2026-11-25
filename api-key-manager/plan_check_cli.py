"""plan_check_cli.py — run the Plan check before a portal update goes live
(deploy_portal.sh calls it; 25 Nov 2026).

Exit 1 when a menu page has no plan tick, or a tick has no page entry
(plan_catalog.KEY_HOME): the update is stopped and nothing is reloaded.
Warnings are printed but don't stop anything.

Loads the portal code WITHOUT starting its background loops and without
writing anything (PLAN_CATALOG_READONLY=1)."""
import contextlib
import io
import os
import sys
import threading

os.environ["PLAN_CATALOG_READONLY"] = "1"
threading.Thread.start = lambda self: None     # no background loops in a check


def main():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        from portal_app import app
        import plan_catalog
        from portal_routes import PLAN_EDITOR_CATALOG
        from feature_access import PLAN_LOCK_CODE_PATHS
        res = plan_catalog.check(app, PLAN_EDITOR_CATALOG, PLAN_LOCK_CODE_PATHS)
    for w in res["warnings"]:
        print(f"⚠️  {w['area']}: {w['item']}")
    if res["blocking"]:
        for b in res["blocking"]:
            print(f"⛔ {b['area']}: {b['item']}\n     → {b['fix']}")
        print(f"❌ Plan check: {len(res['blocking'])} problem(s). Plans would be out of step with the portal.")
        return 1
    print(f"✅ Plan check: every menu page has a plan tick ({len(res['warnings'])} warning(s) to look at in Admin › Plans › Plan check).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
