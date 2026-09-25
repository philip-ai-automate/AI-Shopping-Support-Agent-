"""
feature_access.py — every portal page declares who may reach it, on the page
itself, plus the Features-vs-Roles consistency check that reads those labels.

Why this exists (2026-09-23): new features kept going live without ever
reaching the Roles screen (Team) or Modules (admin), because a team member's
access used to be decided by ~17 hand-kept endpoint lists in portal_routes.py
that nothing forced anyone to update. Those lists are gone. Instead, every
route carries exactly one of these labels, directly above its `def`:

    @team_feature("crm.contacts_view", ...)   a team member MAY reach this page
                                              if their role grants the key(s);
                                              the route still does its own
                                              _require_team_permission check.
    @any_team_member                          every logged-in team member (logout).
    @owner_only                               account owner only — team members
                                              are always sent back to the Inbox.
    @public_route                             no login at all (login, sign-up,
                                              webhooks, unsubscribe links).

check_feature_access() then compares those labels against PLAN_FEATURE_CATALOG
(the list both /admin/modules and the Roles screen read) and reports anything
that doesn't line up. It runs at every portal start (logged) and live on
/admin/modules (red "Features not yet in Roles" box). It warns, never blocks.

UPDATE POLICY — every new or changed feature, no exceptions:
  1. Label the route (above). An unlabelled route is flagged.
  2. Any new permission key goes in PLAN_FEATURE_CATALOG + ROLE_FORM_GRID.
  3. The route checks its key(s) in-route (_require_team_permission etc.).
  4. New keys are granted to every existing plan automatically (see
     sync_new_feature_keys_to_plans) but are NEVER auto-ticked on existing
     team roles — the business owner decides who gets them.
  5. Features are sold through Plans only (Plan editor tick-boxes). Credit
     Packages sell extra AI-message credits and nothing else (2026-09-23).
  6. Every "does the plan include X" check uses a Plan editor tick-box key,
     never a hidden one, so unticking it in the Plan editor really works.
  7. The feature isn't done until /admin/modules shows no warning for it.
"""
import inspect
import re

# view function -> (kind, keys). Keyed by the function object itself, not
# its name, so it works the same across every blueprint.
_ROUTE_ACCESS = {}

TEAM, TEAM_ANY, OWNER, PUBLIC = "team", "team_any", "owner", "public"

_HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
# Code scanned for plan locks: this portal, its templates, and the AI backend
# (a sibling service that reads the same plan_feature_grants table).
PLAN_LOCK_CODE_PATHS = [
    _HERE + "/*.py",
    _HERE + "/templates/**/*.html",
    _HERE + "/../ai-backend/*.py",
    _HERE + "/../whatsapp-gateway/*.py",
]

# Blueprints whose routes must all carry a label. Admin, school, estate and
# ambassador have their own separate login systems — not team roles.
CHECKED_BLUEPRINTS = ("portal", "facebook", "pressone", "buffer")

_PERMISSION_CHECK_RE = re.compile(
    r"_require_team_permission\(|_team_member_has_permission\(|_require_any_team_permission\("
)


def team_feature(*keys):
    def deco(fn):
        _ROUTE_ACCESS[fn] = (TEAM, tuple(keys))
        return fn
    return deco


def any_team_member(fn):
    _ROUTE_ACCESS[fn] = (TEAM_ANY, ())
    return fn


def owner_only(fn):
    _ROUTE_ACCESS[fn] = (OWNER, ())
    return fn


def public_route(fn):
    _ROUTE_ACCESS[fn] = (PUBLIC, ())
    return fn


def route_access(view_func):
    """(kind, keys) for a view function, or (None, ()) if it was never labelled."""
    return _ROUTE_ACCESS.get(view_func, (None, ()))


def team_member_may_enter(view_func) -> bool:
    """Used by portal_routes' before_request: may a team-member session get as
    far as this route's own permission check at all? Unlabelled = no, same
    deny-by-default the old hand-kept lists had."""
    kind, _ = route_access(view_func)
    return kind in (TEAM, TEAM_ANY)


def _has_inline_permission_check(view_func) -> bool:
    try:
        src = inspect.getsource(inspect.unwrap(view_func))
    except (OSError, TypeError):
        return True  # can't read it — don't raise a false alarm
    return bool(_PERMISSION_CHECK_RE.search(src))


# Every way code asks "does this merchant's PLAN include X?" — X must be a
# tick-box on the Plan editor (a PLAN_FEATURE_CATALOG key), or unticking it
# there would do nothing. Found 2026-09-23: 19 page locks and 34 sidebar
# padlocks used hidden keys the Plan editor never showed.
_PLAN_LOCK_PATTERNS = [
    re.compile(r"""_require_plan_sub_feature\(\s*\w+,\s*f?["']([^"']+)"""),
    re.compile(r"""_plan_grants_feature\([^,]+,\s*["']([^"']+)"""),
    re.compile(r"""_plan_feature_enabled\([^,]+,\s*["']([^"']+)"""),
    re.compile(r"""_min_plan_for_feature\(\s*["']([^"']+)"""),
    re.compile(r"""'([a-z_]+\.[a-z_]+)' (?:not )?in granted_features"""),
]


def _plan_locks_off_editor(code_paths, catalog_keys):
    import glob, os
    found = set()
    for pattern in code_paths:
        for path in glob.glob(pattern, recursive=True):
            try:
                src = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for rx in _PLAN_LOCK_PATTERNS:
                for key in rx.findall(src):
                    if key not in catalog_keys:
                        found.add((os.path.basename(path), key))
    return sorted(found)


def check_feature_access(app, catalog, role_grid, plan_only_keys, granted_keys=None,
                         code_paths=None):
    """Returns a list of problems, each {"area", "item", "fix"} written in
    plain English for /admin/modules. Empty list = everything lines up.
    `granted_keys`, if given, is every feature_key held by at least one plan."""
    problems = []
    catalog_keys = {k for feats in catalog.values() for k, _ in feats}
    labels = {k: label for feats in catalog.values() for k, label in feats}

    declared = set()
    for endpoint, view in sorted(app.view_functions.items()):
        bp = endpoint.split(".", 1)[0] if "." in endpoint else None
        if bp not in CHECKED_BLUEPRINTS:
            continue
        kind, keys = route_access(view)
        if kind is None:
            problems.append({
                "area": "Page not labelled",
                "item": f"{endpoint} ({view.__module__})",
                "fix": "Add @team_feature(...), @owner_only or @public_route above it.",
            })
            continue
        if kind == TEAM:
            if not keys:
                problems.append({
                    "area": "Team page with no permission named",
                    "item": endpoint,
                    "fix": "List the permission key(s) in @team_feature(...).",
                })
            for k in keys:
                declared.add(k)
                if k not in catalog_keys:
                    problems.append({
                        "area": "Permission missing from Modules/Roles",
                        "item": f"{k} (used by {endpoint})",
                        "fix": "Add it to PLAN_FEATURE_CATALOG and ROLE_FORM_GRID.",
                    })
            if not _has_inline_permission_check(view):
                problems.append({
                    "area": "Team page never checks the role",
                    "item": endpoint,
                    "fix": "Any team member could open it — add _require_team_permission(...) inside.",
                })

    for k in sorted(catalog_keys - declared - set(plan_only_keys)):
        if k.startswith("legacy:"):
            continue
        problems.append({
            "area": "Roles tick-box that controls nothing",
            "item": f"{k} — “{labels[k]}”",
            "fix": "Name it in the @team_feature(...) of the page it belongs to, or list it as plan-only.",
        })

    placed = set()
    for rows in role_grid.values():
        for row in rows:
            placed.update(row.get(c) for c in ("view", "create", "edit", "delete") if row.get(c))
            placed.update(row.get("other", []))
    for k in sorted(catalog_keys - placed):
        problems.append({
            "area": "Shown loose on the Roles screen",
            "item": f"{k} — “{labels[k]}”",
            "fix": "Place it in a row of ROLE_FORM_GRID (View/Create/Edit/Delete or other).",
        })

    if code_paths:
        for fname, key in _plan_locks_off_editor(code_paths, catalog_keys):
            problems.append({
                "area": "Plan lock the Plan editor can't control",
                "item": f"{key} (in {fname})",
                "fix": "Use the matching tick-box key from PLAN_FEATURE_CATALOG instead.",
            })

    if granted_keys is not None:
        for k in sorted(catalog_keys - set(granted_keys)):
            if k.startswith("legacy:"):
                continue
            problems.append({
                "area": "Not in any plan",
                "item": f"{k} — “{labels[k]}”",
                "fix": "Locked for every customer — tick it in the Plan Editor for the plans that should have it.",
            })
    return problems


def sync_new_feature_keys_to_plans(catalog, get_db_connection):
    """Policy rule 4: the first time a catalog key is ever seen, grant it to
    every existing plan (same 'don't take away what was open' rule every
    earlier catalog expansion followed by hand in portal_migrations.py).
    Remembers which keys it has seen in feature_catalog_seen, so a key an
    admin later unticks in the Plan Editor is never re-granted behind their
    back. Never touches tenant_roles — team roles are the owner's call."""
    conn = get_db_connection()
    if not conn:
        return []
    added = []
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('feature_catalog_seen')")
        first_run = cur.fetchone()[0] is None
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feature_catalog_seen (
                feature_key TEXT PRIMARY KEY,
                first_seen_at TIMESTAMP NOT NULL DEFAULT NOW()
            )""")
        if first_run:
            # Everything already in a plan was handled by the old manual
            # backfills — record it as seen without granting anything new.
            cur.execute("""
                INSERT INTO feature_catalog_seen (feature_key)
                SELECT DISTINCT feature_key FROM plan_feature_grants
                ON CONFLICT DO NOTHING""")
        cur.execute("SELECT feature_key FROM feature_catalog_seen")
        seen = {r[0] for r in cur.fetchall()}
        new_keys = [k for feats in catalog.values() for k, _ in feats
                    if not k.startswith("legacy:") and k not in seen]
        if new_keys:
            cur.execute("SELECT id FROM plans")
            plan_ids = [r[0] for r in cur.fetchall()]
            for k in new_keys:
                for pid in plan_ids:
                    cur.execute(
                        "INSERT INTO plan_feature_grants (plan_id, feature_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (pid, k))
                cur.execute("INSERT INTO feature_catalog_seen (feature_key) VALUES (%s) ON CONFLICT DO NOTHING", (k,))
                added.append(k)
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return added
