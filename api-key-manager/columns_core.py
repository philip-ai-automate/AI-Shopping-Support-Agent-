"""
columns_core.py — columns people add themselves to Leads + Sales Pipeline
(2026-10-16). Same three kinds as labels (labels_core.py):
  private  — only whoever made it sees the column and its values
  group    — whoever made it + chosen departments / staff
  business — everyone
Seeing a column is decided by the database function column_visible(id).

Types and how a value is stored (lead_column_values.value, JSONB):
  text "…" · number 12.5 · date "2026-10-30" · yesno true/false ·
  choice "<choice id>" · multi ["<choice id>", …]
Choices are [{id, name, colour}] on the column, so renaming a choice
changes nothing in the values.
"""
import json
import re
import secrets
from datetime import date

import psycopg2.extras

import labels_core as _lbl

TYPES = [
    ("text",   "Text",            '"Uses Zoho CRM"'),
    ("number", "Number",          "250000"),
    ("date",   "Date",            "30 Oct 2026"),
    ("yesno",  "Yes / No",        "✓"),
    ("choice", "One choice",      "Hot / Warm / Cold"),
    ("multi",  "Several choices", "WhatsApp, Facebook"),
]
TYPE_KEYS = {t[0] for t in TYPES}
_PALETTE = [c[0] for c in _lbl.COLOURS if c[0] != "grey"] + ["grey"]
COLS = "c.id, c.name, c.col_type, c.choices, c.visibility, c.created_by_key"


def vis(alias: str = "c") -> str:
    return f"column_visible({alias}.id)"


def _dict_cur(cur):
    return cur.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def decorate(col: dict) -> dict:
    col = dict(col)
    col["choices"] = [dict(ch, style=_lbl.chip_style(ch.get("colour"))) for ch in (col.get("choices") or [])]
    col["icon"] = {"private": "🔒", "group": "👥"}.get(col.get("visibility"), "")
    col["mine"] = bool(col.get("created_by_key")) and col.get("created_by_key") == _lbl.actor_key()
    col["type_name"] = dict((k, n) for k, n, _ in TYPES).get(col.get("col_type"), "")
    return col


def visible_columns(cur, tenant_id: int, include_hidden: bool = True) -> list:
    """Columns this person may see, oldest first. `hidden` = they chose to hide it."""
    c = _dict_cur(cur)
    c.execute(f"SELECT {COLS}, EXISTS (SELECT 1 FROM lead_column_hidden h WHERE h.column_id=c.id AND h.actor_key=%s) AS hidden "
              f"FROM lead_columns c WHERE c.tenant_id=%s AND {vis()} ORDER BY c.id",
              (_lbl.actor_key(), tenant_id))
    rows = [decorate(r) for r in c.fetchall()]
    c.close()
    return rows if include_hidden else [r for r in rows if not r["hidden"]]


def get_column(cur, tenant_id: int, column_id: int):
    c = _dict_cur(cur)
    c.execute(f"SELECT {COLS} FROM lead_columns c WHERE c.id=%s AND c.tenant_id=%s AND {vis()}", (column_id, tenant_id))
    row = c.fetchone()
    c.close()
    return decorate(row) if row else None


def values_for(cur, column_ids, lead_ids) -> dict:
    """{lead_id: {column_id: value}}"""
    if not column_ids or not lead_ids:
        return {}
    c = _dict_cur(cur)
    c.execute("SELECT lead_id, column_id, value FROM lead_column_values WHERE column_id = ANY(%s) AND lead_id = ANY(%s)",
              (list(column_ids), list(lead_ids)))
    out = {}
    for r in c.fetchall():
        out.setdefault(r["lead_id"], {})[r["column_id"]] = r["value"]
    c.close()
    return out


def display(col: dict, value):
    """Plain text for exports; None when empty."""
    if value is None or value == "" or value == []:
        return None
    t = col["col_type"]
    names = {ch["id"]: ch["name"] for ch in col["choices"]}
    if t == "yesno":
        return "Yes" if value else "No"
    if t == "date":
        try:
            return date.fromisoformat(value).strftime("%d %b %Y")
        except (TypeError, ValueError):
            return str(value)
    if t == "number":
        return f"{value:,}".rstrip("0").rstrip(".") if isinstance(value, float) else f"{value:,}"
    if t == "choice":
        return names.get(value)
    if t == "multi":
        return ", ".join(names[v] for v in value if v in names) or None
    return str(value)


def clean_value(col: dict, raw):
    """A value from the page → what gets stored, or None to clear it.
    Raises ValueError with a message the person can act on."""
    t = col["col_type"]
    if raw is None or raw == "" or raw == []:
        return None
    if t == "text":
        return str(raw).strip()[:500] or None
    if t == "number":
        try:
            n = float(str(raw).replace(",", "").strip())
        except ValueError:
            raise ValueError("Enter a number, for example 250000.")
        return int(n) if n.is_integer() else n
    if t == "date":
        try:
            return date.fromisoformat(str(raw)[:10]).isoformat()
        except ValueError:
            raise ValueError("Pick a date.")
    if t == "yesno":
        return str(raw).lower() in ("1", "true", "yes", "on")
    ids = {ch["id"] for ch in col["choices"]}
    if t == "choice":
        if raw not in ids:
            raise ValueError("Pick one of the choices.")
        return raw
    if t == "multi":
        vals = raw if isinstance(raw, list) else [raw]
        vals = [v for v in vals if v in ids]
        return vals or None
    return None


def clean_choices(raw_json, old=None) -> list:
    """Choices from the column window: [{id?, name}] → [{id, name, colour}].
    Keeps ids (and colours) of existing choices so values stay attached."""
    try:
        items = json.loads(raw_json or "[]")
    except ValueError:
        items = []
    old_by_id = {c["id"]: c for c in (old or [])}
    out, seen = [], set()
    for i, it in enumerate(items):
        name = str((it or {}).get("name") or "").strip()[:40]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        cid = it.get("id") if it.get("id") in old_by_id else "c" + secrets.token_hex(4)
        colour = old_by_id.get(cid, {}).get("colour") or _PALETTE[len(out) % len(_PALETTE)]
        out.append({"id": cid, "name": name, "colour": colour})
    return out


def can_change(col: dict, has_perm) -> bool:
    if col["visibility"] == "business":
        return has_perm("crm.columns_edit")
    return col["mine"]


def can_delete(col: dict, has_perm) -> bool:
    if col["visibility"] == "business":
        return has_perm("crm.columns_delete")
    return col["mine"]


def can_fill(col: dict, has_perm) -> bool:
    """Fill in values: business columns need 'Edit a lead'; private ones only
    whoever made them; group ones anyone in the group."""
    if col["visibility"] == "business":
        return has_perm("crm.pipeline_board_edit")
    return True   # they can only reach it if column_visible() let them see it


_COL_ARG = re.compile(r"^cf(\d+)(_from|_to)?$")


def filter_clauses(alias: str, args, columns: list):
    """WHERE clauses for filters on people's own columns (cf<id>=…,
    cf<id>_from / _to). Returns (clauses, params, fargs, state)."""
    by_id = {c["id"]: c for c in columns}
    clauses, params, fargs, state = [], [], {}, {}
    wanted = {}
    for k in args.keys():
        m = _COL_ARG.match(k)
        if m and int(m.group(1)) in by_id:
            wanted.setdefault(int(m.group(1)), {})[m.group(2) or ""] = [v for v in args.getlist(k) if v != ""]
    for cid, parts in wanted.items():
        col = by_id[cid]
        t = col["col_type"]
        vals = parts.get("", [])
        if t in ("choice", "multi"):   # links carry several choices as one "a,b" value
            vals = [x for v in vals for x in v.split(",") if x]
        sub = f"{alias}.id IN (SELECT v.lead_id FROM lead_column_values v WHERE v.column_id=%s AND "
        if vals == ["__empty__"]:
            clauses.append(f"{alias}.id NOT IN (SELECT v.lead_id FROM lead_column_values v WHERE v.column_id=%s)")
            params.append(cid)
        elif vals == ["__any__"]:
            clauses.append(sub + "TRUE)"); params.append(cid)
        elif t in ("choice", "multi") and vals:
            if t == "choice":
                clauses.append(sub + "v.value #>> '{}' = ANY(%s))")
            else:
                clauses.append(sub + "EXISTS (SELECT 1 FROM jsonb_array_elements_text(v.value) x WHERE x = ANY(%s)))")
            params += [cid, vals]
        elif t == "yesno" and vals and vals[0] in ("yes", "no"):
            clauses.append(sub + "v.value = %s::jsonb)"); params += [cid, "true" if vals[0] == "yes" else "false"]
        elif t == "text" and vals:
            clauses.append(sub + "v.value #>> '{}' ILIKE %s)"); params += [cid, f"%{vals[0]}%"]
        elif t in ("number", "date"):
            cast = "numeric" if t == "number" else "date"
            lo, hi = (parts.get("_from") or [None])[0], (parts.get("_to") or [None])[0]
            conds, cparams = [], []
            for bound, op in ((lo, ">="), (hi, "<=")):
                if bound:
                    try:
                        cparams.append(float(bound) if t == "number" else date.fromisoformat(bound).isoformat())
                        conds.append(f"(v.value #>> '{{}}')::{cast} {op} %s")
                    except ValueError:
                        pass
            if conds:
                clauses.append(sub + " AND ".join(conds) + ")"); params += [cid] + cparams
            if lo: fargs[f"cf{cid}_from"] = lo
            if hi: fargs[f"cf{cid}_to"] = hi
            state[cid] = {"from": lo, "to": hi}
            continue
        else:
            continue
        fargs[f"cf{cid}"] = ",".join(vals)
        state[cid] = {"values": vals}
    return clauses, params, fargs, state


def sort_sql(sort_arg: str, alias: str, columns: list):
    """ORDER BY piece for sort=cc<id>_asc / cc<id>_desc, else None. Empty cells last."""
    m = re.match(r"^cc(\d+)_(asc|desc)$", sort_arg or "")
    if not m:
        return None
    col = next((c for c in columns if c["id"] == int(m.group(1))), None)
    if not col:
        return None
    d = m.group(2).upper()
    v = f"(SELECT v.value FROM lead_column_values v WHERE v.column_id={col['id']} AND v.lead_id={alias}.id)"
    t = col["col_type"]
    if t == "number":
        expr = f"({v} #>> '{{}}')::numeric"
    elif t == "date":
        expr = f"({v} #>> '{{}}')::date"
    elif t == "yesno":
        expr = f"({v} #>> '{{}}')::boolean"
    elif t in ("choice", "multi"):
        # sort by the choice's position in the column's list
        order = {ch["id"]: i for i, ch in enumerate(col["choices"]) if re.fullmatch(r"c[0-9a-f]{8}", ch["id"])}
        cases = " ".join(f"WHEN '{cid_}' THEN {i}" for cid_, i in order.items()) or "WHEN '' THEN 0"
        first = f"({v} #>> '{{}}')" if t == "choice" else f"({v} ->> 0)"
        expr = f"(CASE {first} {cases} END)"
    else:
        expr = f"lower({v} #>> '{{}}')"
    return f"{expr} {d} NULLS LAST"


def save_access(cur, column_id: int, visibility: str, dept_ids, member_ids):
    cur.execute("DELETE FROM lead_column_access WHERE column_id=%s", (column_id,))
    if visibility != "group":
        return
    for d in dept_ids:
        cur.execute("INSERT INTO lead_column_access VALUES (%s,'dept',%s) ON CONFLICT DO NOTHING", (column_id, int(d)))
    for m in member_ids:
        cur.execute("INSERT INTO lead_column_access VALUES (%s,'member',%s) ON CONFLICT DO NOTHING", (column_id, int(m)))


def access_of(cur, column_id: int) -> dict:
    c = _dict_cur(cur)
    c.execute("SELECT kind, ref_id FROM lead_column_access WHERE column_id=%s", (column_id,))
    out = {"dept": [], "member": []}
    for r in c.fetchall():
        out[r["kind"]].append(r["ref_id"])
    c.close()
    return out
