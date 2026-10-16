"""
labels_core.py — shared rules for CRM Labels (2026-10-16).

Every label is one of three kinds:
  business — everyone in the business sees it (all labels before today)
  private  — only the person who made it sees it
  group    — the person who made it, plus chosen departments / staff
             (lead_label_access rows)

Seeing a label is decided in ONE place, the database function
label_visible(id) (portal_migrations.py), which reads who is asking from
the connection (db.get_db_connection). Every query that shows labels adds
`AND label_visible(<alias>.id)` via vis(). The owner does not see staff's
private or group labels (user, 2026-10-16).

Changing a label: business labels need the role ticks (Edit / Delete a
Label); private and group labels can only be changed by whoever made them,
including turning them into another kind. All labels are added by hand.
"""
import psycopg2.extras
from flask import has_request_context, session

# key, name, background, text — the 8 colours offered on the Labels page.
COLOURS = [
    ("grey",   "Grey",   "#EEF1F5", "#475467"),
    ("red",    "Red",    "#FEE4E2", "#B42318"),
    ("orange", "Orange", "#FFEAD5", "#B93815"),
    ("amber",  "Amber",  "#FEF0C7", "#93370D"),
    ("green",  "Green",  "#DCFAE6", "#067647"),
    ("blue",   "Blue",   "#D1E9FF", "#175CD3"),
    ("navy",   "Navy",   "#E8EEF8", "#0B1D40"),
    ("purple", "Purple", "#EBE9FE", "#5925DC"),
]
_COLOUR = {k: (bg, fg) for k, _, bg, fg in COLOURS}
_DEFAULT = ("#fee2e2", "#991b1b")          # how every label looked before colours
KINDS = ("private", "group", "business")

LABEL_COLS = "lb.id, lb.name, lb.colour, lb.description, lb.visibility, lb.created_by_key"


def vis(alias: str = "lb") -> str:
    """SQL condition: the person asking may see this label."""
    return f"label_visible({alias}.id)"


def actor_key() -> str:
    """'team:<id>' / 'owner:<customer id>' of whoever is asking; '' in a
    background job (imports), where only business labels are used."""
    if not has_request_context():
        return ""
    tm = session.get("team_member_id")
    if tm:
        return f"team:{int(tm)}"
    cid = session.get("impersonate_customer_id") or session.get("customer_id")
    return f"owner:{int(cid)}" if cid else ""


def chip_style(colour) -> str:
    bg, fg = _COLOUR.get(colour or "", _DEFAULT)
    return f"background:{bg};color:{fg}"


def decorate(row: dict) -> dict:
    """Adds what the pages need to draw a label: style, icon, hover text."""
    row = dict(row)
    row["style"] = chip_style(row.get("colour"))
    row["icon"] = {"private": "🔒 ", "group": "👥 "}.get(row.get("visibility"), "")
    row["mine"] = bool(row.get("created_by_key")) and row.get("created_by_key") == actor_key()
    return row


def _dict_cur(cur):
    return cur.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def get_label(cur, tenant_id: int, label_id: int):
    """The label if it's this business's AND the person may see it, else None."""
    c = _dict_cur(cur)
    c.execute(f"SELECT {LABEL_COLS} FROM lead_labels lb WHERE lb.id=%s AND lb.tenant_id=%s AND {vis()}",
              (label_id, tenant_id))
    row = c.fetchone()
    c.close()
    return decorate(row) if row else None


def visible_labels(cur, tenant_id: int) -> list:
    """Every label this person may see, for pickers and filters."""
    c = _dict_cur(cur)
    c.execute(f"SELECT {LABEL_COLS} FROM lead_labels lb WHERE lb.tenant_id=%s AND {vis()} "
              f"ORDER BY lower(lb.name), lb.id", (tenant_id,))
    rows = [decorate(r) for r in c.fetchall()]
    c.close()
    return rows


def labels_on(cur, tenant_id: int, ids, link: str = "lead") -> dict:
    """{lead_id or contact_id: [label, ...]} — only labels this person may see."""
    if not ids:
        return {}
    table, col = ("lead_label_leads", "lead_id") if link == "lead" else ("lead_label_contacts", "contact_id")
    c = _dict_cur(cur)
    c.execute(f"SELECT x.{col} AS owner_id, {LABEL_COLS} FROM {table} x "
              f"JOIN lead_labels lb ON lb.id = x.label_id "
              f"WHERE x.{col} = ANY(%s) AND lb.tenant_id=%s AND {vis()} ORDER BY lower(lb.name)",
              (list(ids), tenant_id))
    out = {}
    for r in c.fetchall():
        out.setdefault(r.pop("owner_id"), []).append(decorate(r))
    c.close()
    return out


def find_or_create(cur, tenant_id: int, name: str):
    """Label id for a typed name (contact Tags box, imports): a label this
    person can see with that name, else a new business label. Never picks
    up someone else's private or group label."""
    name = (name or "").strip()[:60]
    if not name:
        return None
    c = _dict_cur(cur)
    c.execute(f"SELECT lb.id FROM lead_labels lb WHERE lb.tenant_id=%s AND lower(lb.name)=lower(%s) AND {vis()} "
              f"ORDER BY (lb.visibility='business') DESC, lb.id LIMIT 1", (tenant_id, name))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO lead_labels (tenant_id, name, visibility, created_by_key) "
                  "VALUES (%s, %s, 'business', %s) "
                  "ON CONFLICT (tenant_id, name) WHERE visibility='business' DO UPDATE SET name=EXCLUDED.name "
                  "RETURNING id", (tenant_id, name, actor_key() or None))
        row = c.fetchone()
    c.close()
    return row["id"]


def can_change(label: dict, has_perm) -> bool:
    """Rename / colour / meaning / who sees it."""
    if label.get("visibility") == "business":
        return has_perm("crm.tags_edit")
    return label.get("mine", False)


def can_delete(label: dict, has_perm) -> bool:
    if label.get("visibility") == "business":
        return has_perm("crm.tags_delete")
    return label.get("mine", False)


def name_taken(cur, tenant_id: int, name: str, visibility: str, skip_id=None) -> bool:
    """Business names are unique in the business; a person's own private and
    group labels must not repeat a name they can already see."""
    c = _dict_cur(cur)
    if visibility == "business":
        c.execute("SELECT 1 FROM lead_labels WHERE tenant_id=%s AND lower(name)=lower(%s) "
                  "AND visibility='business' AND id <> %s", (tenant_id, name, skip_id or 0))
    else:
        c.execute(f"SELECT 1 FROM lead_labels lb WHERE lb.tenant_id=%s AND lower(lb.name)=lower(%s) "
                  f"AND {vis()} AND lb.id <> %s", (tenant_id, name, skip_id or 0))
    taken = c.fetchone() is not None
    c.close()
    return taken


def save_access(cur, label_id: int, visibility: str, dept_ids, member_ids):
    """Who a group label is shared with; cleared for other kinds."""
    cur.execute("DELETE FROM lead_label_access WHERE label_id=%s", (label_id,))
    if visibility != "group":
        return
    for d in dept_ids:
        cur.execute("INSERT INTO lead_label_access (label_id, kind, ref_id) VALUES (%s,'dept',%s) "
                    "ON CONFLICT DO NOTHING", (label_id, int(d)))
    for m in member_ids:
        cur.execute("INSERT INTO lead_label_access (label_id, kind, ref_id) VALUES (%s,'member',%s) "
                    "ON CONFLICT DO NOTHING", (label_id, int(m)))


def access_of(cur, label_id: int) -> dict:
    c = _dict_cur(cur)
    c.execute("SELECT kind, ref_id FROM lead_label_access WHERE label_id=%s", (label_id,))
    out = {"dept": [], "member": []}
    for r in c.fetchall():
        out[r["kind"]].append(r["ref_id"])
    c.close()
    return out
