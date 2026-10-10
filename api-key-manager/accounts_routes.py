"""
accounts_routes.py — the "Accounts" page in the PLATFORM business's portal
(the business PhiXtra's own staff log into). Lists the signed-up businesses
an admin assigned to the logged-in staff member, the setup step each one
stopped at, contact details, and notes. Logic lives in accounts_core.py.

  GET  /accounts                     list (To onboard / Onboarded)
  GET  /accounts/<tenant_id>         one account: steps, contact, notes
  POST /accounts/<tenant_id>/note    add a note / log a call

Only reachable when the logged-in business IS the platform business (setting
PLATFORM_TENANT_ID); everyone else gets a 404, and the menu item and Roles
ticks never show for them. Staff need the "accounts.view" Roles tick and see
only accounts assigned to them unless their role has "accounts.see_all".
The owner sees every account.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

import accounts_core as A
from feature_access import team_feature
from portal_routes import (_current_actor, _customer_id, _get_customer, _inject_granted_features,
                           _require_login, _require_team_permission, _team_member_has_permission)

accounts_bp = Blueprint("accounts", __name__)
accounts_bp.context_processor(_inject_granted_features)

VIEW, NOTES, SEE_ALL = "accounts.view", "accounts.notes", "accounts.see_all"


def _gate():
    """Logged in AND the platform business (everyone else: 404). Each route
    then checks its own Roles tick with _require_team_permission."""
    r = _require_login()
    if r:
        return None, r
    customer = _get_customer(_customer_id())
    if not customer or not A.is_platform_tenant(customer.get("tenant_id")):
        abort(404)
    return customer, None


def _my_scope():
    """None = every assigned account; otherwise the team member id to filter on."""
    tm_id = session.get("team_member_id")
    if not tm_id or _team_member_has_permission(SEE_ALL):
        return None
    return int(tm_id)


def _rows(scope, only_tenant=None):
    A.ensure_tables()
    conn, cur = A._db()
    try:
        where, params = ["TRUE"], []
        if scope is not None:
            where.append("a.team_member_id = %s"); params.append(scope)
        if only_tenant is not None:
            where.append("a.tenant_id = %s"); params.append(int(only_tenant))
        cur.execute(f"""
            SELECT a.tenant_id, a.team_member_id, a.assigned_at, a.assigned_by, a.onboarded_at,
                   COALESCE(NULLIF(TRIM(CONCAT(tm.first_name, ' ', tm.last_name)), ''), tm.name) AS staff_name,
                   t.name AS business, t.created_at AS signed_up, t.trial_granted_at, t.trial_ends_at,
                   t.website_platform, t.domain, COALESCE(p.name, 'Free') AS plan_name, p.slug AS plan_slug,
                   c.first_name, c.last_name, c.email, c.phone_number, c.last_login_at
              FROM platform_account_assignments a
              JOIN tenants t ON t.id = a.tenant_id
              LEFT JOIN plans p ON p.id = t.plan_id
              LEFT JOIN team_members tm ON tm.id = a.team_member_id
              LEFT JOIN LATERAL (SELECT * FROM customers WHERE tenant_id = t.id ORDER BY id LIMIT 1) c ON TRUE
             WHERE {' AND '.join(where)}
             ORDER BY t.created_at DESC""", params)
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    steps = A.setup_steps_many([r["tenant_id"] for r in rows])
    newly = [r["tenant_id"] for r in rows if steps.get(r["tenant_id"], {}).get("complete") and not r["onboarded_at"]]
    A.mark_onboarded(newly)
    notes = A.last_notes([r["tenant_id"] for r in rows])
    for r in rows:
        r["setup"] = steps.get(r["tenant_id"]) or {"type": "WhatsApp", "steps": [], "done": 0, "total": 0, "complete": False, "stuck": None}
        r["onboarded"] = bool(r["onboarded_at"]) or r["setup"]["complete"]
        r["contact"] = " ".join(x for x in [(r["first_name"] or "").strip(), (r["last_name"] or "").strip()] if x)
        r["last_note"] = notes.get(r["tenant_id"])
        r["wa_number"] = "".join(ch for ch in (r["phone_number"] or "") if ch.isdigit())
    return rows


@accounts_bp.route("/accounts", methods=["GET"])
@team_feature(VIEW)
def accounts_list():
    customer, r = _gate()
    r = r or _require_team_permission(VIEW)
    if r:
        return r
    scope = _my_scope()
    rows = _rows(scope)
    tab = "onboarded" if request.args.get("tab") == "onboarded" else "todo"
    todo = [x for x in rows if not x["onboarded"]]
    done = [x for x in rows if x["onboarded"]]
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    stuck_long = sum(1 for x in todo if x["signed_up"] and (now - (x["signed_up"] if x["signed_up"].tzinfo else x["signed_up"].replace(tzinfo=timezone.utc))).days > 5)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    done_month = sum(1 for x in done if x["onboarded_at"] and x["onboarded_at"] >= month_start)
    return render_template("portal/accounts_list.html", customer=customer,
                           rows=(done if tab == "onboarded" else todo), tab=tab,
                           n_todo=len(todo), n_done=len(done), stuck_long=stuck_long, done_month=done_month,
                           show_staff=(scope is None), now=now)


@accounts_bp.route("/accounts/<int:tenant_id>", methods=["GET"])
@team_feature(VIEW)
def account_detail(tenant_id: int):
    customer, r = _gate()
    r = r or _require_team_permission(VIEW)
    if r:
        return r
    rows = _rows(_my_scope(), only_tenant=tenant_id)
    if not rows:
        flash("That account isn't assigned to you.", "warning")
        return redirect(url_for("accounts.accounts_list"))
    return render_template("portal/accounts_detail.html", customer=customer, a=rows[0],
                           notes=A.notes_for(tenant_id),
                           can_note=_team_member_has_permission(NOTES))


@accounts_bp.route("/accounts/<int:tenant_id>/note", methods=["POST"])
@team_feature(NOTES)
def account_note(tenant_id: int):
    customer, r = _gate()
    r = r or _require_team_permission(NOTES)
    if r:
        return r
    if not _rows(_my_scope(), only_tenant=tenant_id):
        flash("That account isn't assigned to you.", "warning")
        return redirect(url_for("accounts.accounts_list"))
    text = (request.form.get("note") or "").strip()
    if not text:
        flash("Write the note first.", "warning")
        return redirect(url_for("accounts.account_detail", tenant_id=tenant_id))
    me = _current_actor(customer)
    A.add_note(tenant_id, me["key"], me["label"], text)
    flash("Note saved.", "success")
    return redirect(url_for("accounts.account_detail", tenant_id=tenant_id))


def assigned_count_for_menu():
    """Number of not-yet-onboarded accounts for the sidebar badge (cheap: no step checks)."""
    try:
        customer = _get_customer(_customer_id())
        if not customer or not A.is_platform_tenant(customer.get("tenant_id")):
            return None
        if not _team_member_has_permission(VIEW):
            return None
        A.ensure_tables()
        conn, cur = A._db()
        try:
            scope = _my_scope()
            if scope is None:
                cur.execute("SELECT COUNT(*) AS n FROM platform_account_assignments WHERE onboarded_at IS NULL")
            else:
                cur.execute("SELECT COUNT(*) AS n FROM platform_account_assignments WHERE onboarded_at IS NULL AND team_member_id=%s", (scope,))
            return int(cur.fetchone()["n"])
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ accounts menu count:", e)
        return None


@accounts_bp.app_context_processor
def _accounts_menu():
    if not session.get("portal_logged_in"):
        return {}
    return {"accounts_menu_count": assigned_count_for_menu()}
