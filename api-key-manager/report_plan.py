"""Which Reports a business's plan gets (21 Nov 2026, approved mock-up Nbf3F8K2ShDdocrnpbrTGr).

  Every plan:        Reports home, ready-made reports you can change (time, team member,
                     split by, chart or list) and save, Ask for a report, the Sales overview dashboard.
  Growth + Scale:    also the custom report builder, their own dashboards (add / move boxes),
                     sharing reports and dashboards.

Starter plans (and the free PhiXtra Connect plan) get the simple version only.
"""
from flask import g

from db import get_db_connection

SIMPLE_ONLY = {"starter", "startup_dual", "web_startup", "b2b_starter", "connect"}


def plan_slug(tenant_id) -> str:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT p.slug FROM tenants t LEFT JOIN plans p ON p.id = t.plan_id WHERE t.id = %s", (int(tenant_id),))
        row = cur.fetchone()
        return (row[0] or "") if row else ""
    finally:
        conn.close()


def custom_ok(tenant_id) -> bool:
    """Growth or Scale (anything that isn't a Starter / free plan). Worked out once per request."""
    key = f"rp_custom_{int(tenant_id)}"
    if key not in g:
        try:
            setattr(g, key, plan_slug(tenant_id) not in SIMPLE_ONLY)
        except Exception as e:
            print("⚠️ report plan:", e)
            setattr(g, key, False)
    return getattr(g, key)


def custom_ok_for_menu() -> bool:
    """For the side menu on every page: Growth / Scale business logged in? (one lookup per request, only when asked)."""
    from flask import session
    cid = session.get("customer_id")
    if not session.get("portal_logged_in") or not cid:
        return False
    if "rp_menu_tid" not in g:
        try:
            conn = get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT tenant_id FROM customers WHERE id = %s", (int(cid),))
                row = cur.fetchone()
                g.rp_menu_tid = row[0] if row else None
            finally:
                conn.close()
        except Exception as e:
            print("⚠️ report plan (menu):", e)
            g.rp_menu_tid = None
    return bool(g.rp_menu_tid) and custom_ok(g.rp_menu_tid)
