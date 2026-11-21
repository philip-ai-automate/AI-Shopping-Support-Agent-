"""Dashboard widgets (21 Nov 2026, approved mock-up Nbf3F8K2ShDdocrnpbrTGr screen 7).

A dashboard is a 12-column grid of widgets. Each widget is a row of report_dashboard_boxes:
  kind  'report'  a ready-made or saved report as a chart or table (report_id) — what boxes were before
        'number'  one big number                 wcfg {"pick": key in NUMBERS}
        'list'    an action list you click into  wcfg {"pick": key in LISTS}
        'board'   a leaderboard of team members  wcfg {"pick": key in BOARDS}
        'heading' a heading / short text         note (first line = the heading)
  gx, gy, gw, gh  place and size on the grid (columns 0–11, rows of GRID_ROW px). Older boxes have none:
                  place_all() lays them out from their order and size the first time.
Every number still comes from report_builder — a widget is a report config plus how to show it.
"""
from datetime import date

import report_builder as RB

COLS = 12
GRID_ROW = 64                  # px per grid row (plus a 12 px gap)
TYPES = [
    ("number", "Number tile", "#", "One big number: quotes won, new enquiries, chats waiting, follow-ups overdue…"),
    ("report", "Chart or table", "▮▮", "Any ready-made question or saved report, as a chart or a table."),
    ("list", "Action list", "☰", "A live to-do list you can click into: follow-ups due, chats waiting, quotes waiting, deals gone quiet."),
    ("board", "Leaderboard", "🏆", "Your team ranked by won value, deals won, calls made or chats answered."),
    ("heading", "Heading", "T", "A title or a few words that split the dashboard into sections."),
]
TYPE_NAMES = {k: n for k, n, _i, _d in TYPES}
SIZE = {"number": (3, 2), "report": (6, 5), "list": (6, 4), "board": (4, 4), "heading": (12, 1)}
MIN = {"number": (2, 2), "report": (3, 3), "list": (3, 3), "board": (3, 3), "heading": (2, 1)}

# Number tiles: (key, label, help, config, "now" = right now, ignores the dashboard's Time)
NUMBERS = [
    ("quotes_won", "Quotes won (value)", "Won quotes, in money",
     {"subject": "quotes", "f": "qstatus|is|won", "m": "sum_qvalue", "datef": "qclosed", "period": "this_month"}, False),
    ("quotes_sent", "Quotes sent", "How many quotes went out",
     {"subject": "quotes", "datef": "firstsent", "period": "this_month"}, False),
    ("new_enquiries", "New enquiries", "All channels", {"subject": "leads", "period": "this_month"}, False),
    ("chats_waiting", "Chats waiting for a reply", "Right now",
     {"subject": "conversations", "f": "vwait|is|true", "period": "all"}, True),
    ("followups_overdue", "Follow-ups overdue", "Right now",
     {"subject": "followups", "f": "fstatus|is|overdue", "period": "all"}, True),
    ("open_deals", "Open deals (value)", "Your pipeline in money",
     {"subject": "deals", "f": "outcome|is|open_deal", "m": "sum_value", "period": "all"}, True),
    ("orders", "Orders", "Online shop orders", {"subject": "orders", "period": "this_month"}, False),
    ("sales_value", "Sales (order value)", "Online shop orders, in money",
     {"subject": "orders", "m": "sum_tot", "period": "this_month"}, False),
]
WARN = {"chats_waiting": "#B45309", "followups_overdue": "#B91C1C"}

# Action lists: (key, label, help, config, link kind). Right now — they follow Team member, not Time.
LISTS = [
    ("followups_today", "Follow-ups due today", "Due today or overdue — click one to open the deal",
     {"subject": "followups", "f": ["fstatus|not|done", "fstatus|not|cancelled", "fstatus|not|upcoming"],
      "col": ["fcust", "fdue", "fstatus", "fassigned"], "cols_set": "1", "sort": "fdue", "dir": "asc", "period": "all"},
     "followup"),
    ("chats_waiting", "Chats waiting for our reply", "Oldest first",
     {"subject": "conversations", "f": "vwait|is|true", "col": ["vwho", "vch", "vlast"], "cols_set": "1",
      "sort": "vlast", "dir": "asc", "period": "all"}, "inbox"),
    ("quotes_waiting", "Quotes waiting on the buyer", "Sent, no answer yet — oldest first",
     {"subject": "quotes", "f": "qstatus|is|quote_sent", "col": ["ref", "bcompany", "qvalue", "waiting"], "cols_set": "1",
      "sort": "waiting", "dir": "desc", "period": "all"}, "quote"),
    ("deals_quiet", "Deals gone quiet", "Open deals with no activity for 14 days or more",
     {"subject": "deals", "f": ["outcome|is|open_deal", "quietdays|gte|14"], "col": ["customer", "stage", "quietdays"],
      "cols_set": "1", "sort": "quietdays", "dir": "desc", "period": "all"}, "deal"),
]
LIST_ROWS = 8

# Leaderboards: (key, label, help, config). Follow Time, not Team member (it ranks everyone).
BOARDS = [
    ("won_value", "Won quote value", "Who brought in the most",
     {"subject": "quotes", "f": "qstatus|is|won", "g1": "qassigned", "m": "sum_qvalue", "datef": "qclosed",
      "view": "summary", "period": "this_month"}),
    ("deals_won", "Deals won", "Count of won deals",
     {"subject": "deals", "f": "outcome|is|won", "g1": "assigned", "datef": "won", "view": "summary", "period": "this_month"}),
    ("calls", "Calls made", "Logged and WhatsApp calls", {"subject": "calls", "g1": "kwho", "view": "summary", "period": "this_month"}),
    ("chats_answered", "Chats answered", "Messages each person sent to customers",
     {"subject": "activity", "f": "areply|is|true", "g1": "awho", "m": "sum_qty", "view": "summary", "period": "this_month"}),
]
BOARD_ROWS = 5

BY_KIND = {"number": {x[0]: x for x in NUMBERS}, "list": {x[0]: x for x in LISTS}, "board": {x[0]: x for x in BOARDS}}


def kind_of(box) -> str:
    k = box.get("kind") or ""
    if k in TYPE_NAMES:
        return k
    return "report" if box.get("report_id") else "heading"


def choices(kind, b2b) -> list:
    """What a widget of this kind can show for this business: [(key, label, help)]."""
    allowed = RB.allowed_set(b2b)
    if kind == "number":
        out = [(k, l, h) for k, l, h, c, _now in NUMBERS if c["subject"] in allowed]
        if "quotes" in allowed:      # B2B: quotes, not shop orders
            out = [x for x in out if x[0] not in ("orders", "sales_value")]
        return out
    if kind == "list":
        return [(k, l, h) for k, l, h, c, _link in LISTS if c["subject"] in allowed]
    if kind == "board":
        return [(k, l, h) for k, l, h, c in BOARDS if c["subject"] in allowed]
    return []


def _md(cfg):
    from werkzeug.datastructures import MultiDict
    md = MultiDict()
    for k, v in cfg.items():
        for item in (v if isinstance(v, list) else [v]):
            md.add(k, item)
    return md


def spec(box):
    """The NUMBERS / LISTS / BOARDS entry a widget shows, or None."""
    return BY_KIND.get(kind_of(box), {}).get((box.get("wcfg") or {}).get("pick") or "")


ROW_CHOICES = {"list": [5, 8, 10], "board": [3, 5, 10]}


def has_time(box) -> bool:
    """Does this widget depend on a time? (Action lists and "right now" numbers don't.)"""
    k = kind_of(box)
    if k == "number":
        sp = spec(box)
        return bool(sp) and not sp[4]
    return k in ("board", "report")


def has_member(box) -> bool:
    return kind_of(box) in ("number", "list", "report")


def opts(box) -> dict:
    """The widget's own settings (21 Nov 2026 Settings redesign). Older widgets: use_filters off = own
    time is the widget's default and everyone's numbers."""
    w = dict(box.get("wcfg") or {})
    if not box.get("use_filters", True) and "time" not in w:
        w["time"], w["member"] = "own", "own"
    w.setdefault("time", "dash")
    w.setdefault("member", "dash")
    return w


def rows_for(box) -> int:
    k = kind_of(box)
    default = {"list": LIST_ROWS, "board": BOARD_ROWS}.get(k, 5)
    try:
        n = int((box.get("wcfg") or {}).get("rows") or default)
    except (TypeError, ValueError):
        n = default
    return n if n in ROW_CHOICES.get(k, [n]) else default


def title_of(box, fallback=""):
    t = " ".join(str((box.get("wcfg") or {}).get("title") or "").split())[:80]
    if t:
        return t
    sp = spec(box)
    return sp[1] if sp else fallback


def apply_filters(cfg, box, flt, active):
    """The dashboard's Time / Team member, or the widget's own ("Always …"), whichever its Settings say."""
    o = opts(box)
    if has_time(box):
        if o["time"] == "own":
            if RB.period_ok(o.get("period") or ""):
                cfg = RB.apply_dashboard(cfg, period=o["period"], d_from=RB._iso(o.get("date_from")), d_to=RB._iso(o.get("date_to")))
        elif "period" in active and flt.get("period"):
            cfg = RB.apply_dashboard(cfg, period=flt["period"], d_from=flt["date_from"], d_to=flt["date_to"])
    if has_member(box):
        if o["member"] == "own":
            if o.get("member_key"):
                cfg = RB.apply_dashboard(cfg, member=o["member_key"])
        elif "member" in active and flt.get("member"):
            cfg = RB.apply_dashboard(cfg, member=flt["member"])
    return cfg


def cfg_for(box, b2b, flt, active):
    """The report settings behind a number / list / board widget, with the filters it follows.
    None when this business can't use it."""
    sp = spec(box)
    if not sp:
        return None
    raw = sp[3]
    if not RB.subject_ok(raw["subject"], b2b):
        return None
    return apply_filters(RB.read_config(_md(raw), b2b), box, flt, active)


# ── Layout ───────────────────────────────────────────────────────────────────

_OLD_W = {"s": 3, "m": 6, "l": 9, "w": 12}


def _old_h(box):
    k = kind_of(box)
    if k == "heading":
        return 1 if len((box.get("note") or "").strip()) <= 80 and "\n" not in (box.get("note") or "").strip() else 2
    if k == "report":
        c = box.get("config") or {}
        if c.get("chart") == "number" or (not c.get("g1") and c.get("chart") != "table"):
            return 3
    return SIZE.get(k, (6, 5))[1]


def place_all(boxes) -> list:
    """Every widget with a place on the grid. Widgets saved before 21 Nov 2026 have none: they are laid out
    left to right, top to bottom, in their old order and size."""
    placed, x, y, row_h = [], 0, 0, 0
    bottom = 0
    for b in boxes:
        if b.get("gx") is not None and b.get("gw"):
            bottom = max(bottom, int(b["gy"]) + int(b["gh"]))
    y = bottom
    for b in boxes:
        b = dict(b)
        k = kind_of(b)
        b["kind_v"] = k
        if b.get("gx") is not None and b.get("gw"):
            b.update(gx=int(b["gx"]), gy=int(b["gy"]), gw=int(b["gw"]), gh=int(b["gh"]))
        else:
            w = _OLD_W.get(b.get("size") or "", SIZE.get(k, (6, 5))[0]) if k != "heading" else 12
            h = _old_h(b)
            if x + w > COLS:
                x, y, row_h = 0, y + row_h, 0
            b.update(gx=x, gy=y, gw=w, gh=h)
            x += w
            row_h = max(row_h, h)
        placed.append(b)
    return placed


def clean_place(x, y, w, h, kind):
    mw, mh = MIN.get(kind, (2, 1))
    try:
        x, y, w, h = int(x), int(y), int(w), int(h)
    except (TypeError, ValueError):
        return None
    w = max(mw, min(COLS, w))
    x = max(0, min(COLS - w, x))
    y = max(0, min(400, y))
    h = max(mh, min(20, h))
    return x, y, w, h


def heading_parts(note):
    lines = (note or "").strip().split("\n", 1)
    return lines[0].strip(), (lines[1].strip() if len(lines) > 1 else "")


def today():
    return date.today()
