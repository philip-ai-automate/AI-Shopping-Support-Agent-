"""Report builder (Reports & Dashboards, phase R1 — 19 Nov 2026).

Approved mock-up: https://claude.ai/artifact/EPpwZTeFt5iccD8SH155P1
One engine for every report: pick what the report is about (a SUBJECT),
tick columns, filter, group (Summary) or list every record (Detailed),
save, export. Dashboards (R3) are saved reports laid out as boxes, so
everything a box shows comes from here.

R1 subjects: Deals and Enquiries & leads (both read the Sales Pipeline with
the CRM Report's 122 columns, crm_report.py) and Quotes (B2B plans only).

Nothing here writes business data. Lead subjects run through ctx["ls"]
(portal_routes._ls_exec) so staff without "See all leads / contacts" only
count their own records; Quotes keep to the requests handed to them
(same rule as Quotations). Every column, filter field and grouping is
looked up in a fixed list — no SQL text ever comes from the browser.
"""
import datetime as _dt
import re

import crm_report as crm

EXPORT_MAX_ROWS = 20000
PER_PAGE = 50
MAX_GROUP_ROWS = 500

# ── Time periods ─────────────────────────────────────────────────────────────
# QUICK = the quick picks the date picker shows (in this order).
QUICK = [
    ("today", "Today"), ("yesterday", "Yesterday"), ("this_week", "This week"), ("last_week", "Last week"),
    ("this_month", "This month"), ("last_month", "Last month"),
    ("this_quarter", "This quarter"), ("last_quarter", "Last quarter"),
    ("this_year", "This year"), ("last_year", "Last year"), ("all", "All time"),
]
# Older keys still accepted (saved reports, ready-made ones): 7 / 30 / 90 days.
PERIODS = QUICK + [("7", "Last 7 days"), ("30", "Last 30 days"), ("90", "Last 90 days"), ("custom", "Your own dates")]
PERIOD_KEYS = {k for k, _ in PERIODS}
# "The last N days / weeks / months" — moves on every day: last45d, last6w, last3m.
ROLL_RE = re.compile(r"^last([1-9][0-9]{0,3})([dwm])$")
ROLL_MAX = {"d": 3660, "w": 520, "m": 120}
ROLL_UNIT = {"d": ("day", "days"), "w": ("week", "weeks"), "m": ("month", "months")}


def roll_parts(period):
    """(n, unit) for a rolling key like 'last45d', else None."""
    m = ROLL_RE.match(period or "")
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2)
    return (n, u) if n <= ROLL_MAX[u] else None


def period_ok(period) -> bool:
    return period in PERIOD_KEYS or roll_parts(period) is not None


def _months_back(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    m += 1
    last = (_dt.date(y + (m == 12), m % 12 + 1, 1) - _dt.timedelta(days=1)).day
    return _dt.date(y, m, min(d.day, last))


def _span(a, b):
    if a.year == b.year:
        if a == b:
            return a.strftime("%d %b %Y")
        return a.strftime("%d %b") + " – " + b.strftime("%d %b %Y")
    return a.strftime("%d %b %Y") + " – " + b.strftime("%d %b %Y")


def _q_start(d):
    return d.replace(month=(d.month - 1) // 3 * 3 + 1, day=1)


def period_range(period, d_from=None, d_to=None, today=None):
    """(date_from, date_to, label) — (None, None, 'All time') for all time."""
    t = today or _dt.date.today()
    if period == "custom" and d_from and d_to and d_from <= d_to:
        return d_from, d_to, d_from.strftime("%d %b %Y") + " – " + d_to.strftime("%d %b %Y")
    if period == "all":
        return None, None, "All time"
    rp = roll_parts(period)
    if rp:
        n, u = rp
        a = (t - _dt.timedelta(days=n - 1) if u == "d" else t - _dt.timedelta(days=7 * n - 1) if u == "w"
             else _months_back(t, n) + _dt.timedelta(days=1))
        return a, t, f"Last {n} {ROLL_UNIT[u][0 if n == 1 else 1]} ({_span(a, t)})"
    if period == "today":
        return t, t, "Today (" + _span(t, t) + ")"
    if period == "yesterday":
        y = t - _dt.timedelta(days=1)
        return y, y, "Yesterday (" + _span(y, y) + ")"
    if period == "this_week":
        s = t - _dt.timedelta(days=t.weekday())
        return s, t, "This week (" + _span(s, t) + ")"
    if period == "last_week":
        s = t - _dt.timedelta(days=t.weekday() + 7)
        e = s + _dt.timedelta(days=6)
        return s, e, "Last week (" + _span(s, e) + ")"
    if period == "this_month":
        return t.replace(day=1), t, "This month (" + t.strftime("%B %Y") + ")"
    if period == "last_month":
        end = t.replace(day=1) - _dt.timedelta(days=1)
        return end.replace(day=1), end, "Last month (" + end.strftime("%B %Y") + ")"
    if period == "this_quarter":
        s = _q_start(t)
        return s, t, f"This quarter ({s.strftime('%d %b')} – {t.strftime('%d %b %Y')})"
    if period == "last_quarter":
        end = _q_start(t) - _dt.timedelta(days=1)
        s = _q_start(end)
        return s, end, f"Last quarter ({s.strftime('%d %b')} – {end.strftime('%d %b %Y')})"
    if period == "this_year":
        return t.replace(month=1, day=1), t, "This year (" + str(t.year) + ")"
    if period == "last_year":
        return _dt.date(t.year - 1, 1, 1), _dt.date(t.year - 1, 12, 31), "Last year (" + str(t.year - 1) + ")"
    n = int(period) if period in ("7", "30", "90") else 30
    return t - _dt.timedelta(days=n - 1), t, f"Last {n} days"


def picker_presets(today=None):
    """Quick picks with their dates today, for the date picker."""
    out = []
    for k, label in QUICK:
        a, b, _l = period_range(k, today=today)
        out.append({"key": k, "label": label, "from": a.isoformat() if a else "", "to": b.isoformat() if b else ""})
    return out


def picker_value(cfg_or_flt):
    """What the date picker starts on: {period, from, to}."""
    p = cfg_or_flt.get("period") or ""
    a, b = cfg_or_flt.get("date_from"), cfg_or_flt.get("date_to")
    return {"period": p, "from": a.isoformat() if a else "", "to": b.isoformat() if b else ""}


# ── Column catalogue helpers ─────────────────────────────────────────────────
# A column: key, label, expr, fmt, joins. Extra flags worked out here:
#   group  — may be a grouping (Summary rows)
#   filter — filter type: text | num | date | choice | None (encrypted words)
GROUP_FMTS = {"stage", "outcome", "source", "assigned", "channel", "contact_status", "country", "title",
              "quote_status", "quote_channel", "yesno", "campaign_status", "person", "text"}
CHOICE_FMTS = {"stage", "outcome", "source", "assigned", "channel", "contact_status", "quote_status",
               "quote_channel", "yesno", "campaign_status"}
NUM_FMTS = {"money", "money_own", "int", "pct"}
DATE_BUCKETS = [("month", "by month"), ("week", "by week"), ("quarter", "by quarter"), ("year", "by year"),
                ("day", "by day")]


LABEL_MAPS = {}      # fmt "lm_<name>" → {value: label}; filled by report_subjects.py (and lm_inv below)


def _flags(col):
    f = col["fmt"]
    col = dict(col)
    col["filter"] = (None if f == "long" else "date" if f == "date" else "num" if f in NUM_FMTS
                     else "choice" if (f in CHOICE_FMTS or f.startswith("lm_")) else "text")
    col["group"] = f in GROUP_FMTS or f == "date" or f.startswith("lm_")
    return col


def _col(key, label, expr, fmt="text", joins=()):
    return _flags({"key": key, "label": label, "expr": expr, "fmt": fmt, "joins": tuple(joins)})


# ── Subject: Deals / Enquiries & leads (the CRM Report's catalogue) ──────────
CRM_GROUPS = [(g, [_flags(c) for c in cols]) for g, cols in crm.COL_GROUPS]
CRM_COLUMNS = {c["key"]: c for _, cols in CRM_GROUPS for c in cols}
# Reason closed / company / campaign / product are free text but people group by them.
for _k in ("reason", "company", "campaign", "product", "category", "isdeal", "currency", "city", "cocity",
           "cocountry", "camptemplate", "ecamp", "seq", "seqstatus", "impfile", "lastacttype", "lastmoveby"):
    if _k in CRM_COLUMNS:
        CRM_COLUMNS[_k]["group"] = True
# Never group by a per-record name / number / free note.
for _k in ("customer", "contact", "phone", "wa", "email", "otheremails", "website", "dealnotes", "contactnotes",
           "qualnotes", "propnotes", "negnotes", "personal", "lastnote", "conotes", "colastnote", "street",
           "postcode", "cname", "cowebsite", "replytext", "esubject", "failreason", "seqstop"):
    if _k in CRM_COLUMNS:
        CRM_COLUMNS[_k]["group"] = False

CRM_MEASURES = [
    ("count", "Number of records", None, "int"),
    ("sum_value", "Sum of Deal value", "SUM(fx_to_selling(l.deal_value, l.currency, l.tenant_id))", "money"),
    ("avg_value", "Average Deal value", "AVG(fx_to_selling(l.deal_value, l.currency, l.tenant_id))", "money"),
    ("avg_daystoclose", "Average days to close",
     "AVG(CASE WHEN l.outcome IN ('won','lost','dropped','not_a_fit') "
     "THEN COALESCE(l.won_date, l.dropped_at::date, l.updated_at::date) - l.created_at::date END)", "int"),
    ("avg_daysopen", "Average days open", "AVG(CURRENT_DATE - l.created_at::date)", "int"),
]
CRM_EXTRAS = [
    ("Won", "COUNT(*) FILTER (WHERE l.outcome = 'won')", "int"),
    ("Won value", "COALESCE(SUM(fx_to_selling(l.deal_value, l.currency, l.tenant_id)) "
                  "FILTER (WHERE l.outcome = 'won'), 0)", "money"),
    ("Win rate", "100.0 * COUNT(*) FILTER (WHERE l.outcome = 'won') / "
                 "NULLIF(COUNT(*) FILTER (WHERE l.outcome IN ('won','lost','dropped')), 0)", "pct"),
]
CRM_DATES = [
    ("activity", "Anything happened in the period", None),
    ("created", "Created date", "l.created_at::date"),
    ("becamedeal", "Became a deal", "l.opportunity_at::date"),
    ("won", "Won date", "l.won_date"),
    ("closed", "Closed date", "COALESCE(l.won_date, l.dropped_at::date)"),
]


def _crm_from(tenant_id, joins):
    return crm.base_from(tenant_id, joins)


# ── Subject: Quotes (quote requests and the quotation sent) ──────────────────
Q_STATUS = {"to_quote": "To quote", "quote_sent": "Quote sent", "won": "Won", "lost": "Lost"}
Q_CHANNEL = {"manual": "Typed in", "email": "Email", "whatsapp": "WhatsApp", "chat": "Website chat",
             "messenger": "Messenger", "instagram": "Instagram", "phone": "Phone call", "shopify": "Shopify",
             "woo": "WooCommerce"}
QUOTE_JOINS = {
    "qt": "LEFT JOIN LATERAL (SELECT x.number, x.version FROM quotations x WHERE x.request_id = q.id "
          "ORDER BY x.version DESC LIMIT 1) qt ON TRUE",
    "qv": "LEFT JOIN LATERAL (SELECT COUNT(*) AS n FROM quotations x WHERE x.request_id = q.id "
          "AND x.sent_at IS NOT NULL) qv ON TRUE",
    "ql": "LEFT JOIN (SELECT request_id, SUM(open_count) AS opens, MIN(first_opened_at) AS first_open, "
          "MAX(last_opened_at) AS last_open FROM quote_links WHERE tenant_id = {t} GROUP BY request_id) ql "
          "ON ql.request_id = q.id",
}
# R6: the invoice made from a won quote, read back from Xero / QuickBooks / Sage
# (quotations.xero_invoice / qbo_invoice / sage_invoice on the latest sent version).
# Read only — PhiXtra never chases payment. One status for all three tools.
QUOTE_JOINS["qinv"] = ("LEFT JOIN LATERAL (SELECT x.xero_invoice AS xi, x.qbo_invoice AS qbi, x.sage_invoice AS si "
                       "FROM quotations x WHERE x.request_id = q.id AND x.status = 'sent' "
                       "ORDER BY x.version DESC, x.id DESC LIMIT 1) qinv ON TRUE")
QUOTE_JOIN_ORDER = list(QUOTE_JOINS)
_TODAY = "to_char(CURRENT_DATE, 'YYYY-MM-DD')"
INV_STATUS = f"""(CASE
  WHEN qinv.xi IS NOT NULL THEN CASE
    WHEN qinv.xi->>'status' IN ('VOIDED','DELETED') THEN 'deleted'
    WHEN qinv.xi->>'status' = 'PAID' THEN 'paid'
    WHEN qinv.xi->>'status' = 'AUTHORISED' AND qinv.xi->>'due' < {_TODAY}
         AND COALESCE(NULLIF(qinv.xi->>'amount_due', '')::numeric, 0) > 0 THEN 'overdue'
    WHEN qinv.xi->>'status' = 'AUTHORISED' THEN 'awaiting'
    ELSE 'draft' END
  WHEN qinv.qbi IS NOT NULL THEN CASE
    WHEN COALESCE(qinv.qbi->>'deleted', '') = 'true' THEN 'deleted'
    WHEN COALESCE(NULLIF(qinv.qbi->>'total', '')::numeric, 0) > 0
         AND COALESCE(NULLIF(qinv.qbi->>'balance', '')::numeric, NULLIF(qinv.qbi->>'total', '')::numeric) <= 0.004 THEN 'paid'
    WHEN qinv.qbi->>'due' < {_TODAY}
         AND COALESCE(NULLIF(qinv.qbi->>'balance', '')::numeric, NULLIF(qinv.qbi->>'total', '')::numeric, 0) > 0 THEN 'overdue'
    WHEN COALESCE(qinv.qbi->>'email_status', '') IN ('', 'NotSet', 'NeedToSend') THEN 'draft'
    ELSE 'awaiting' END
  WHEN qinv.si IS NOT NULL THEN CASE
    WHEN COALESCE(qinv.si->>'deleted', '') = 'true' OR upper(COALESCE(qinv.si->>'status', '')) IN ('VOID','DELETED') THEN 'deleted'
    WHEN upper(qinv.si->>'status') = 'PAID' THEN 'paid'
    WHEN upper(COALESCE(qinv.si->>'status', '')) IN ('UNPAID','PART_PAID') AND qinv.si->>'due' < {_TODAY}
         AND COALESCE(NULLIF(qinv.si->>'outstanding', '')::numeric, 0) > 0 THEN 'overdue'
    WHEN upper(COALESCE(qinv.si->>'status', 'DRAFT')) = 'DRAFT' THEN 'draft'
    ELSE 'awaiting' END
  ELSE 'none' END)"""
_INV_TOTAL = ("COALESCE(NULLIF(qinv.xi->>'total', ''), NULLIF(qinv.qbi->>'total', ''), "
              "NULLIF(qinv.si->>'total', ''))::numeric")
_INV_DUE_AMT = ("COALESCE(NULLIF(qinv.xi->>'amount_due', ''), NULLIF(qinv.qbi->>'balance', ''), "
                "NULLIF(qinv.si->>'outstanding', ''))::numeric")
_INV_CUR = "COALESCE(qinv.xi->>'currency', qinv.qbi->>'currency', qinv.si->>'currency', q.quote_currency)"
INV_AMOUNT = f"(CASE WHEN {INV_STATUS} IN ('none','deleted') THEN NULL ELSE fx_to_selling({_INV_TOTAL}, {_INV_CUR}, q.tenant_id) END)"
INV_PAID = (f"(CASE WHEN {INV_STATUS} IN ('none','deleted') THEN NULL WHEN {INV_STATUS} = 'paid' "
            f"THEN fx_to_selling({_INV_TOTAL}, {_INV_CUR}, q.tenant_id) "
            f"ELSE fx_to_selling(GREATEST(0, {_INV_TOTAL} - COALESCE({_INV_DUE_AMT}, {_INV_TOTAL})), {_INV_CUR}, q.tenant_id) END)")
INV_WORDS = {"none": "Not invoiced yet", "draft": "Draft (not sent)", "awaiting": "Awaiting payment", "paid": "Paid",
             "overdue": "Overdue", "deleted": "Voided or deleted"}
_QVAL = "fx_to_selling(q.quote_value, q.quote_currency, q.tenant_id)"
QUOTE_GROUPS = [
    ("Quote request", [
        _col("ref", "Request number", "'R-' || q.ref_no"),
        _col("qstatus", "Status", "q.status", "quote_status"),
        _col("qchannel", "Where the request came from", "q.channel", "quote_channel"),
        _col("requested", "Requested on", "q.created_at::date", "date"),
        _col("neededby", "Needed by", "q.needed_by", "date"),
        _col("qassigned", "Assigned to", "q.assigned_key", "assigned"),
        _col("lines", "Number of products", "jsonb_array_length(q.lines)", "int"),
        _col("estimate", "Estimate", "fx_to_selling(q.estimate, q.currency, q.tenant_id)", "money"),
        _col("qcreatedby", "Added by", "q.created_by", "person"),
    ]),
    ("Quote", [
        _col("qnumber", "Quote number", "COALESCE(qt.number, q.quote_number)", "text", ["qt"]),
        _col("qvalue", "Quote value", _QVAL, "money"),
        _col("qcurrency", "Quote currency", "q.quote_currency"),
        _col("firstsent", "First sent on", "q.first_quote_at::date", "date"),
        _col("lastsent", "Last sent on", "q.quote_sent_at::date", "date"),
        _col("sentvia", "Sent by (how)", "q.quote_sent_via", "title"),
        _col("sentby", "Sent by (who)", "q.quote_sent_by", "person"),
        _col("validuntil", "Valid until", "q.quote_valid_until", "date"),
        _col("versions", "Versions sent", "qv.n", "int", ["qv"]),
        _col("opens", "Times opened by buyer", "COALESCE(ql.opens, 0)", "int", ["ql"]),
        _col("firstopen", "First opened on", "ql.first_open::date", "date", ["ql"]),
        _col("lastopen", "Last opened on", "ql.last_open::date", "date", ["ql"]),
        _col("waiting", "Days waiting on buyer",
             "CASE WHEN q.status = 'quote_sent' THEN CURRENT_DATE - q.quote_sent_at::date END", "int"),
    ]),
    ("Outcome", [
        _col("qclosed", "Won or lost on", "q.closed_at::date", "date"),
        _col("qwon", "Won on", "CASE WHEN q.status = 'won' THEN q.closed_at::date END", "date"),
        _col("qreason", "Reason", "q.close_reason"),
        _col("qclosedby", "Closed by", "q.closed_by", "person"),
        _col("daystowin", "Days to win",
             "CASE WHEN q.status = 'won' THEN q.closed_at::date - q.created_at::date END", "int"),
    ]),
    ("Invoice (read from Xero, QuickBooks or Sage)", [
        _col("ivst", "Invoice status", INV_STATUS, "lm_inv", ["qinv"]),
        _col("ivsys", "Invoiced in", "CASE WHEN qinv.xi IS NOT NULL THEN 'Xero' WHEN qinv.qbi IS NOT NULL THEN 'QuickBooks' "
                                     "WHEN qinv.si IS NOT NULL THEN 'Sage' END", "text", ["qinv"]),
        _col("ivnum", "Invoice number", "COALESCE(qinv.xi->>'number', qinv.qbi->>'number', qinv.si->>'number')", "text", ["qinv"]),
        _col("ivamt", "Invoiced amount", INV_AMOUNT, "money", ["qinv"]),
        _col("ivpaid", "Paid so far", INV_PAID, "money", ["qinv"]),
        _col("ivdue", "Invoice due on", "NULLIF(COALESCE(qinv.xi->>'due', qinv.qbi->>'due', qinv.si->>'due'), '')::date",
             "date", ["qinv"]),
        _col("ivlate", "Days overdue", f"CASE WHEN {INV_STATUS} = 'overdue' THEN CURRENT_DATE - "
                                       "NULLIF(COALESCE(qinv.xi->>'due', qinv.qbi->>'due', qinv.si->>'due'), '')::date END",
             "int", ["qinv"]),
    ]),
    ("Buyer", [
        _col("bcompany", "Buyer company", "q.buyer_company"),
        _col("bname", "Buyer name", "q.buyer_name"),
        _col("bemail", "Buyer email", "q.buyer_email"),
        _col("bphone", "Buyer phone", "q.buyer_phone"),
        _col("bcountry", "Buyer country", "q.buyer_country", "country"),
        _col("deliverto", "Deliver to", "q.deliver_to"),
    ]),
    ("Deal on the Sales Pipeline", [
        _col("dstage", "Deal stage", "l.stage", "stage"),
        _col("doutcome", "Deal outcome", "CASE WHEN l.id IS NULL THEN NULL ELSE " + crm.OUTCOME_EXPR + " END",
             "outcome"),
        _col("dsource", "Lead source",
             "CASE WHEN l.id IS NULL THEN NULL WHEN COALESCE(l.source, '') = '' THEN 'none' ELSE l.source END",
             "source"),
        _col("dcompany", "Company (CRM)", "co.name"),
        _col("dcategory", "Business category", "l.business_category"),
    ]),
]
LABEL_MAPS["lm_inv"] = INV_WORDS
QUOTE_COLUMNS = {c["key"]: c for _, cols in QUOTE_GROUPS for c in cols}
for _k in ("bcompany", "dcompany", "qreason", "qcurrency", "dcategory", "sentvia", "ivsys"):
    QUOTE_COLUMNS[_k]["group"] = True
for _k in ("ref", "qnumber", "bname", "bemail", "bphone", "deliverto"):
    QUOTE_COLUMNS[_k]["group"] = False

QUOTE_MEASURES = [
    ("count", "Number of quotes", None, "int"),
    ("sum_qvalue", "Sum of Quote value", f"SUM({_QVAL})", "money"),
    ("avg_qvalue", "Average Quote value", f"AVG({_QVAL})", "money"),
    ("sum_ivamt", "Sum of Invoiced amount", f"SUM({INV_AMOUNT})", "money"),
    ("sum_ivpaid", "Sum of Paid so far", f"SUM({INV_PAID})", "money"),
    ("avg_daystowin", "Average days to win",
     "AVG(CASE WHEN q.status = 'won' THEN q.closed_at::date - q.created_at::date END)", "int"),
]
QUOTE_EXTRAS = [
    ("Won", "COUNT(*) FILTER (WHERE q.status = 'won')", "int"),
    ("Won value", f"COALESCE(SUM({_QVAL}) FILTER (WHERE q.status = 'won'), 0)", "money"),
    ("Win rate", "100.0 * COUNT(*) FILTER (WHERE q.status = 'won') / "
                 "NULLIF(COUNT(*) FILTER (WHERE q.status IN ('won','lost')), 0)", "pct"),
]
QUOTE_DATES = [
    ("requested", "Requested on", "q.created_at::date"),
    ("firstsent", "First sent on", "q.first_quote_at::date"),
    ("qclosed", "Won or lost on", "q.closed_at::date"),
]


# Some tables / columns are made the first time a feature is used (quote links,
# the QuickBooks and Sage invoice columns, …). A business database without them
# gets "none yet" instead of an error. Looked up once every 10 minutes.
_SCHEMA = {"at": 0.0, "tables": set(), "cols": set()}


def _schema():
    import time
    if time.time() - _SCHEMA["at"] > 600:
        from db import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
            _SCHEMA["tables"] = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT table_name || '.' || column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name IN ('quotations')")
            _SCHEMA["cols"] = {r[0] for r in cur.fetchall()}
            _SCHEMA["at"] = time.time()
        except Exception as e:
            print("⚠️ report schema check:", e)
        finally:
            conn.close()
    return _SCHEMA


def table_exists(name: str) -> bool:
    return name in _schema()["tables"]


def _quote_from(tenant_id, joins):
    t = int(tenant_id)
    sql = ["quote_requests q",
           "LEFT JOIN merchant_pipeline_leads l ON l.id = q.lead_id",
           "LEFT JOIN crm_companies co ON co.id = l.company_id"]
    sch = _schema()
    for j in QUOTE_JOIN_ORDER:
        if j not in joins:
            continue
        if j == "ql" and "quote_links" not in sch["tables"]:
            sql.append("LEFT JOIN (SELECT NULL::bigint AS request_id, 0 AS opens, NULL::timestamptz AS first_open, "
                       "NULL::timestamptz AS last_open) ql ON FALSE")
        elif j == "qinv":
            pick = ", ".join(f"x.{c} AS {a}" if f"quotations.{c}" in sch["cols"] else f"NULL::jsonb AS {a}"
                             for c, a in (("xero_invoice", "xi"), ("qbo_invoice", "qbi"), ("sage_invoice", "si")))
            sql.append(f"LEFT JOIN LATERAL (SELECT {pick} FROM quotations x WHERE x.request_id = q.id "
                       "AND x.status = 'sent' ORDER BY x.version DESC, x.id DESC LIMIT 1) qinv ON TRUE")
        else:
            sql.append(QUOTE_JOINS[j].format(t=t))
    return "\n".join(sql)


# ── The subjects ─────────────────────────────────────────────────────────────
SUBJECTS = {
    "quotes": {
        "label": "Quotes", "noun": "quotes", "b2b": True, "id": "q.id", "tenant": "q.tenant_id",
        "groups": QUOTE_GROUPS, "columns": QUOTE_COLUMNS, "from": _quote_from, "fixed": None,
        "measures": QUOTE_MEASURES, "extras": QUOTE_EXTRAS, "dates": QUOTE_DATES, "date_default": "requested",
        "default_cols": ["ref", "bcompany", "qassigned", "qstatus", "qvalue", "firstsent", "qwon"],
        "default_sort": ("requested", "desc"), "scope": "own", "own": "q.assigned_key", "cur": "q.quote_currency",
        "measure_joins": {"sum_ivamt": ["qinv"], "sum_ivpaid": ["qinv"]},
        "hint": "Every quote request and the quotation sent for it.",
    },
    "deals": {
        "label": "Deals", "noun": "deals", "b2b": False, "id": "l.id", "tenant": "l.tenant_id",
        "groups": CRM_GROUPS, "columns": CRM_COLUMNS, "from": _crm_from,
        "fixed": "(l.is_opportunity OR l.outcome = 'won')",
        "measures": CRM_MEASURES, "extras": CRM_EXTRAS, "dates": CRM_DATES, "date_default": "activity",
        "default_cols": ["customer", "company", "stage", "outcome", "assigned", "value", "becamedeal", "closed"],
        "default_sort": ("becamedeal", "desc"), "scope": "leads", "cur": "l.currency",
        "hint": "Sales Pipeline records that became a deal.",
    },
    "leads": {
        "label": "Enquiries & leads", "noun": "leads", "b2b": False, "id": "l.id", "tenant": "l.tenant_id",
        "groups": CRM_GROUPS, "columns": CRM_COLUMNS, "from": _crm_from, "fixed": None,
        "measures": CRM_MEASURES, "extras": CRM_EXTRAS, "dates": CRM_DATES, "date_default": "created",
        "default_cols": ["customer", "contact", "phone", "source", "channel", "assigned", "stage", "created"],
        "default_sort": ("created", "desc"), "scope": "leads", "cur": "l.currency",
        "hint": "Every enquiry and lead on the Sales Pipeline, deal or not.",
    },
}
SUBJECT_ORDER = ["quotes", "deals", "leads"]


def allowed_set(b2b_allowed) -> set:
    """Old callers pass True/False for the B2B plan; new ones pass the set of subjects."""
    if isinstance(b2b_allowed, (set, frozenset)):
        return set(b2b_allowed)
    return {k for k in SUBJECT_ORDER if (b2b_allowed or not SUBJECTS[k]["b2b"]) and not SUBJECTS[k].get("gate")}


def subject_ok(key, allowed) -> bool:
    return key in SUBJECTS and key in allowed_set(allowed)


def subjects_for(b2b_allowed) -> list:
    allowed = allowed_set(b2b_allowed)
    return [(k, SUBJECTS[k]) for k in SUBJECT_ORDER if k in allowed]


# ── Reading a report's settings (query string, form or saved config) ─────────
FILTER_OPS = {
    "text": [("has", "contains"), ("is", "is"), ("not", "is not"), ("empty", "is empty"), ("set", "is not empty")],
    "choice": [("is", "is"), ("not", "is not"), ("empty", "is empty"), ("set", "is not empty")],
    "num": [("gte", "at least"), ("lte", "at most"), ("eq", "equals"), ("empty", "is empty"), ("set", "is not empty")],
    "date": [("after", "on or after"), ("before", "on or before"), ("empty", "is empty"), ("set", "is not empty")],
}
NO_VALUE_OPS = {"empty", "set"}


def _iso(s):
    try:
        return _dt.date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def _getlist(args, k):
    if hasattr(args, "getlist"):
        return args.getlist(k)
    v = args.get(k)
    return v if isinstance(v, list) else ([] if v in (None, "") else [v])


def _group_ok(subj, key):
    col_key, _, bucket = (key or "").partition("@")
    col = subj["columns"].get(col_key)
    if not col or not col["group"]:
        return ""
    if col["fmt"] == "date":
        return col_key + "@" + (bucket if bucket in dict(DATE_BUCKETS) else "month")
    return col_key


def read_config(args, b2b_allowed, today=None) -> dict:
    allowed = allowed_set(b2b_allowed)
    subject = args.get("subject") or ("quotes" if "quotes" in allowed else "deals")
    if subject not in allowed:
        subject = "deals"
    subj = SUBJECTS[subject]
    cols_all = subj["columns"]

    cols, seen = [], set()
    for c in _getlist(args, "col"):
        if c in cols_all and c not in seen:
            seen.add(c)
            cols.append(c)
    if not cols and args.get("cols_set") != "1":
        cols = list(subj["default_cols"])

    period = args.get("period") or "this_quarter"
    period = period if period_ok(period) else "this_quarter"
    d_from, d_to, plabel = period_range(period, _iso(args.get("date_from")), _iso(args.get("date_to")), today)
    if period == "custom" and d_from is None:
        period = "this_quarter"
        d_from, d_to, plabel = period_range(period, today=today)
    datef = args.get("datef") or subj["date_default"]
    if datef not in {k for k, _, _ in subj["dates"]}:
        datef = subj["date_default"]

    filters = []
    for raw in _getlist(args, "f")[:20]:
        key, _, rest = str(raw).partition("|")
        op, _, val = rest.partition("|")
        col = cols_all.get(key)
        if not col or not col["filter"] or op not in dict(FILTER_OPS[col["filter"]]):
            continue
        val = val.strip()[:200]
        if op not in NO_VALUE_OPS:
            if not val:
                continue
            if col["filter"] == "num":
                try:
                    float(val)
                except ValueError:
                    continue
            if col["filter"] == "date" and not _iso(val):
                continue
        filters.append({"key": key, "op": op, "val": "" if op in NO_VALUE_OPS else val})

    g1 = _group_ok(subj, args.get("g1"))
    g2 = _group_ok(subj, args.get("g2")) if g1 else ""
    if g2 == g1:
        g2 = ""
    measure = args.get("m") or "count"
    if measure not in {k for k, _, _, _ in subj["measures"]}:
        measure = "count"
    view = args.get("view") or ("summary" if g1 else "detailed")
    view = view if view in ("summary", "detailed") else "detailed"
    if view == "summary" and not g1:
        view = "detailed"

    chart = args.get("chart") or "auto"
    chart = chart if chart in CHART_KEYS else "auto"
    compare = ""            # 20 Nov: comparisons removed (PhiXtra is a sales system, not accounting)
    compare = compare if compare in ("prev", "year") and period != "all" else ""

    sort = args.get("sort") or ""
    sort = sort if sort in cols_all else ""
    sdir = "asc" if args.get("dir") == "asc" else "desc"
    try:
        page = max(1, int(args.get("page") or 1))
    except ValueError:
        page = 1
    return {"subject": subject, "col": cols, "period": period, "date_from": d_from, "date_to": d_to,
            "period_label": plabel, "datef": datef, "f": filters, "g1": g1, "g2": g2, "m": measure,
            "view": view, "sort": sort, "dir": sdir, "page": page, "chart": chart, "cmp": compare}


def config_of(cfg: dict) -> dict:
    """What a saved report stores — also the query string that reopens it."""
    out = {"subject": cfg["subject"], "col": cfg["col"], "cols_set": "1", "period": cfg["period"],
           "datef": cfg["datef"], "f": [f"{f['key']}|{f['op']}|{f['val']}" for f in cfg["f"]],
           "g1": cfg["g1"], "g2": cfg["g2"], "m": cfg["m"], "view": cfg["view"],
           "sort": cfg["sort"], "dir": cfg["dir"] if cfg["sort"] else "",
           "chart": "" if cfg.get("chart", "auto") == "auto" else cfg["chart"], "cmp": cfg.get("cmp", "")}
    if cfg["period"] == "custom":
        out["date_from"], out["date_to"] = cfg["date_from"].isoformat(), cfg["date_to"].isoformat()
    return {k: v for k, v in out.items() if v not in ("", [], None)}


# ── SQL ──────────────────────────────────────────────────────────────────────

def _where(cfg, tenant_id, quote_scope=None):
    """(clauses, params, joins) for business + fixed rule + period + filters."""
    subj = SUBJECTS[cfg["subject"]]
    cols = subj["columns"]
    clauses, params, joins = [subj["tenant"] + " = %s"], [int(tenant_id)], set()
    if subj["fixed"]:
        clauses.append(subj["fixed"])
    if subj["scope"] == "own" and quote_scope:
        clauses.append(f"{subj['own']} = %s")         # staff without "See all leads": their own records
        params.append(quote_scope)
    if cfg["date_from"] is not None:
        a, b = cfg["date_from"], cfg["date_to"]
        if cfg["datef"] == "activity":
            pc, pp = crm.period_clause({"date_from": a, "date_to": b})
            clauses.append(pc)
            params += pp
        else:
            expr = dict((k, e) for k, _, e in subj["dates"])[cfg["datef"]]
            clauses.append(f"{expr} BETWEEN %s AND %s")
            params += [a, b]
    for f in cfg["f"]:
        col = cols[f["key"]]
        joins |= set(col["joins"])
        e, op, v, typ = col["expr"], f["op"], f["val"], col["filter"]
        if op == "empty":
            clauses.append(f"(({e}) IS NULL OR ({e})::text = '')")
        elif op == "set":
            clauses.append(f"(({e}) IS NOT NULL AND ({e})::text <> '')")
        elif typ == "num":
            sym = {"gte": ">=", "lte": "<=", "eq": "="}[op]
            clauses.append(f"({e}) {sym} %s")
            params.append(float(v))
        elif typ == "date":
            clauses.append(f"({e})::date {'>=' if op == 'after' else '<='} %s")
            params.append(_iso(v))
        elif op == "has":
            clauses.append(f"({e})::text ILIKE %s")
            params.append("%" + v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        elif op == "is":
            clauses.append(f"lower(({e})::text) = lower(%s)")
            params.append(v)
        else:   # not
            clauses.append(f"(({e}) IS NULL OR lower(({e})::text) <> lower(%s))")
            params.append(v)
    return clauses, params, joins


def _group_expr(subj, gkey):
    col_key, _, bucket = gkey.partition("@")
    col = subj["columns"][col_key]
    if col["fmt"] == "date":
        return f"date_trunc('{bucket}', ({col['expr']})::timestamp)::date", col
    return col["expr"], col


def _exec(ctx, cur, subj, sql, params):
    if subj["scope"] == "leads":
        ctx["ls"](cur, sql, params)
    else:
        cur.execute(sql, params)


def run_detailed(cur, ctx, cfg, export=False, quote_scope=None):
    """(rows, total). rows: [{id, cells}] in the chosen column order."""
    subj = SUBJECTS[cfg["subject"]]
    cols = subj["columns"]
    t = ctx["tenant_id"]
    clauses, params, joins = _where(cfg, t, quote_scope)
    where = " AND ".join(clauses)
    _exec(ctx, cur, subj, f"SELECT COUNT(*) AS n FROM {subj['from'](t, joins)} WHERE {where}", params)
    total = int(cur.fetchone()["n"] or 0)
    if not cfg["col"]:
        return [], total
    specs = [cols[k] for k in cfg["col"]]
    joins |= {j for s in specs for j in s["joins"]}
    sort_key, sdir = cfg["sort"], cfg["dir"]
    if sort_key:
        joins |= set(cols[sort_key]["joins"])
        order = f"({cols[sort_key]['expr']}) {sdir.upper()} NULLS LAST, {subj['id']} DESC"
    else:
        dkey, ddir = subj["default_sort"]
        dexpr = cols[dkey]["expr"] if dkey in cols else subj["id"]
        order = f"({dexpr}) {ddir.upper()} NULLS LAST, {subj['id']} DESC"
    cur_expr = subj.get("cur") or "NULL"
    select = ", ".join([f"{subj['id']} AS _id", f"{cur_expr} AS _cur"] +
                       [f"{s['expr']} AS c_{s['key']}" for s in specs])
    sql = f"SELECT {select} FROM {subj['from'](t, joins)} WHERE {where} ORDER BY {order}"
    if export:
        sql += " LIMIT %s"
        params = params + [EXPORT_MAX_ROWS]
    else:
        sql += " LIMIT %s OFFSET %s"
        params = params + [PER_PAGE, (cfg["page"] - 1) * PER_PAGE]
    _exec(ctx, cur, subj, sql, params)
    rows = []
    for r in cur.fetchall():
        rows.append({"id": r["_id"], "cells": [fmt(r["c_" + s["key"]], s["fmt"], ctx, r) for s in specs]})
    return rows, total


def run_summary(cur, ctx, cfg, quote_scope=None):
    """{headers, rows, total_row}. One row per group (and sub-group)."""
    subj = SUBJECTS[cfg["subject"]]
    t = ctx["tenant_id"]
    clauses, params, joins = _where(cfg, t, quote_scope)
    joins |= set(SUBJECTS[cfg["subject"]].get("measure_joins", {}).get(cfg["m"], ()))
    keys = [g for g in (cfg["g1"], cfg["g2"]) if g]
    gexprs, gcols = [], []
    for g in keys:
        e, c = _group_expr(subj, g)
        gexprs.append(e)
        gcols.append(c)
        joins |= set(c["joins"])
    m_key, m_label, m_sql, m_fmt = next(m for m in subj["measures"] if m[0] == cfg["m"])
    aggs = ["COUNT(*) AS n"]
    if m_sql:
        aggs.append(f"{m_sql} AS mv")
    for i, (_, sql_e, _) in enumerate(subj["extras"]):
        aggs.append(f"{sql_e} AS x{i}")
    sel_g = [f"{e} AS g{i}" for i, e in enumerate(gexprs)]
    where = " AND ".join(clauses)
    frm = subj["from"](t, joins)
    order = "n DESC" if not m_sql else "mv DESC NULLS LAST"
    if gcols and gcols[0]["fmt"] == "date":
        order = "g0 ASC"
    sql = (f"SELECT {', '.join(sel_g + aggs)} FROM {frm} WHERE {where} "
           f"GROUP BY {', '.join(str(i + 1) for i in range(len(gexprs)))} "
           f"ORDER BY {order} LIMIT {MAX_GROUP_ROWS}")
    _exec(ctx, cur, subj, sql, params)
    raw = cur.fetchall()
    _exec(ctx, cur, subj, f"SELECT {', '.join(aggs)} FROM {subj['from'](t, joins)} WHERE {where}", params)
    tot = cur.fetchone()

    headers = []
    for g, c in zip(keys, gcols):
        _, _, bucket = g.partition("@")
        headers.append(c["label"] + (" (" + dict(DATE_BUCKETS)[bucket] + ")" if bucket else ""))
    headers.append(subj["measures"][0][1])
    if m_sql:
        headers.append(m_label)
    headers += [x[0] for x in subj["extras"]]

    def nums(r):
        out = [fmt(r["n"], "int", ctx)]
        if m_sql:
            out.append(fmt(r["mv"], m_fmt, ctx) if r["mv"] is not None else "—")
        for i, (_, _, xf) in enumerate(subj["extras"]):
            out.append(fmt(r[f"x{i}"], xf, ctx) if r[f"x{i}"] is not None else "—")
        return out

    rows = []
    for r in raw:
        labels = []
        for i, (g, c) in enumerate(zip(keys, gcols)):
            v = r[f"g{i}"]
            _, _, bucket = g.partition("@")
            labels.append(date_label(v, bucket) if bucket else group_label(v, c, ctx))
        rows.append({"labels": labels, "cells": nums(r), "n": int(r["n"]),
                     "mv": float(r["mv"]) if m_sql and r["mv"] is not None else None,
                     "keys": [r[f"g{i}"] for i in range(len(keys))],
                     "rk": [raw_key(r[f"g{i}"]) for i in range(len(keys))]})
    total_row = {"labels": ["Total"] + [""] * (len(keys) - 1), "cells": nums(tot), "n": int(tot["n"])}
    return {"headers": headers, "rows": rows, "total_row": total_row, "ngroups": len(keys),
            "truncated": len(raw) >= MAX_GROUP_ROWS}


# ── Formatting ───────────────────────────────────────────────────────────────

def date_label(v, bucket):
    if v is None:
        return "No date"
    if bucket == "month":
        return v.strftime("%b %Y")
    if bucket == "quarter":
        return f"Q{(v.month - 1) // 3 + 1} {v.year}"
    if bucket == "year":
        return str(v.year)
    if bucket == "week":
        return "Week of " + v.strftime("%d %b %Y")
    return v.strftime("%d %b %Y")


def group_label(v, col, ctx):
    if v is None or v == "":
        return {"assigned": "Unassigned", "country": "No country"}.get(col["fmt"], "Not set")
    return fmt(v, col["fmt"], ctx)


def fmt(val, f, ctx, row=None):
    if f.startswith("lm_"):
        if val is None or val == "":
            return "—"
        return LABEL_MAPS[f].get(str(val), str(val).replace("_", " ").capitalize())
    if f == "pct":
        return "—" if val is None else f"{float(val):.1f}%"
    if f == "quote_status":
        return "—" if not val else Q_STATUS.get(val, str(val).title())
    if f == "source" and val and val not in crm.SOURCE_LABELS:
        return str(val).replace("_", " ").title()
    if f == "quote_channel":
        return "—" if not val else Q_CHANNEL.get(val, str(val).replace("_", " ").title())
    if f == "int" and val is not None and not isinstance(val, int):
        try:
            return f"{round(float(val)):,}"
        except (TypeError, ValueError):
            return str(val)
    return crm.fmt_cell(val, f, ctx, row)


def choice_options(subj_key, col_key, ctx) -> list:
    """[(value, label)] for a choice filter."""
    col = SUBJECTS[subj_key]["columns"][col_key]
    f = col["fmt"]
    if f == "stage":
        return list(ctx["stage_labels"].items())
    if f == "outcome":
        return list(crm.OUTCOME_LABELS.items())
    if f == "source":
        return [(k, crm.SOURCE_LABELS[k]) for k in crm.SOURCE_ORDER]
    if f == "assigned":
        return [(p["key"], p["display"]) for p in ctx["people"]]
    if f == "channel":
        return list(crm.CHANNEL_LABELS.items())
    if f == "contact_status":
        return list(crm.STATUS_LABELS.items())
    if f == "quote_status":
        return list(Q_STATUS.items())
    if f == "quote_channel":
        return list(Q_CHANNEL.items())
    if f == "campaign_status":
        return list(crm.CAMPAIGN_STATUS_LABELS.items())
    if f == "yesno":
        return [("true", "Yes"), ("false", "No")]
    if f.startswith("lm_"):
        return list(LABEL_MAPS[f].items())
    return []


def filter_label(cfg, f, ctx) -> str:
    col = SUBJECTS[cfg["subject"]]["columns"][f["key"]]
    op = dict(FILTER_OPS[col["filter"]])[f["op"]]
    if f["op"] in NO_VALUE_OPS:
        return f"{col['label']} {op}"
    v = f["val"]
    if col["filter"] == "choice":
        v = dict(choice_options(cfg["subject"], f["key"], ctx)).get(v, v)
    elif col["filter"] == "date":
        d = _iso(v)
        v = d.strftime("%d %b %Y") if d else v
    return f"{col['label']} {op} {v}"


# ── Charts (phase R2) ────────────────────────────────────────────────────────
CHARTS = [("table", "Table only"), ("pie", "Pie"), ("donut", "Donut"), ("bar", "Bar"), ("hbar", "Horizontal bar"),
          ("stacked", "Stacked bar"), ("line", "Line"), ("area", "Area"), ("combo", "Bar + line"),
          ("funnel", "Funnel"), ("number", "Big number"), ("heatmap", "Heatmap"), ("pivot", "Pivot")]
PIVOT_COLS = 12      # more columns than this: the rest become "Other" (counts and sums only)
CHART_KEYS = {k for k, _ in CHARTS} | {"auto"}
# Approved colour rule (20 Nov 2026): distinct colours per bar/slice; grey for Unassigned/Other.
PALETTE = ["#2563EB", "#F59E0B", "#16A34A", "#7C3AED", "#0D9488", "#F97316", "#0B1D40", "#DC2626"]
GREY = "#94A3B8"
_MEANING = {"won": "#16A34A", "accepted": "#16A34A", "paid": "#16A34A", "lost": "#DC2626", "overdue": "#DC2626",
            "declined": "#DC2626", "unassigned": GREY, "other": GREY, "not set": GREY, "no country": GREY,
            "expired": GREY, "whatsapp": "#25D366", "email": "#2563EB", "website chat": "#F97316",
            "website": "#F97316", "phone call": "#F59E0B", "ai agent": "#7C3AED"}


def colours_for(labels) -> list:
    """One colour per label: meaning colours first (Won green, Lost red, WhatsApp green…), then the palette."""
    out, i = [], 0
    for lab in labels:
        c = _MEANING.get(str(lab).strip().lower())
        if not c:
            c = PALETTE[i % len(PALETTE)]
            i += 1
        out.append(c)
    return out


def currency_prefix(ctx) -> str:
    """The selling-currency symbol as money cells show it, e.g. '£'."""
    try:
        import re as _re
        return _re.match(r"^[^\d\-]*", str(fmt(0, "money", ctx)) or "").group(0)
    except Exception:
        return ""
PIE_MAX = 6          # more slices than this are unreadable: the rest become "Other"
SERIES_MAX = 8       # stacked bar / heatmap columns


def compare_range(cfg):
    """(date_from, date_to, label) of the period to compare with, or None."""
    a, b = cfg["date_from"], cfg["date_to"]
    if not cfg.get("cmp") or a is None:
        return None
    if cfg["cmp"] == "year":
        def back(d):
            try:
                return d.replace(year=d.year - 1)
            except ValueError:            # 29 Feb
                return d.replace(year=d.year - 1, day=28)
        pa, pb = back(a), back(b)
        return pa, pb, "Same period last year"
    days = (b - a).days + 1
    pb = a - _dt.timedelta(days=1)
    return pb - _dt.timedelta(days=days - 1), pb, "Previous period"


def shifted(cfg):
    r = compare_range(cfg)
    if not r:
        return None
    return dict(cfg, date_from=r[0], date_to=r[1], period="custom", period_label=r[2])


def run_total(cur, ctx, cfg, quote_scope=None):
    """(number of records, measure value or None) for the whole report."""
    subj = SUBJECTS[cfg["subject"]]
    clauses, params, joins = _where(cfg, ctx["tenant_id"], quote_scope)
    joins |= set(SUBJECTS[cfg["subject"]].get("measure_joins", {}).get(cfg["m"], ()))
    m = next(x for x in subj["measures"] if x[0] == cfg["m"])
    agg = "COUNT(*) AS n" + (f", {m[2]} AS mv" if m[2] else "")
    _exec(ctx, cur, subj, f"SELECT {agg} FROM {subj['from'](ctx['tenant_id'], joins)} "
                          f"WHERE {' AND '.join(clauses)}", params)
    r = cur.fetchone()
    return int(r["n"] or 0), (float(r["mv"]) if m[2] and r.get("mv") is not None else None)


def measure_of(cfg):
    subj = SUBJECTS[cfg["subject"]]
    return next(x for x in subj["measures"] if x[0] == cfg["m"])


def chart_rules(cfg) -> dict:
    """{chart: reason it can't be used ('' = fine)}."""
    m = measure_of(cfg)
    additive = m[0] == "count" or m[0].startswith("sum")
    out = {}
    for k, _ in CHARTS:
        why = ""
        if k not in ("table", "number") and not cfg["g1"]:
            why = "Choose a split first"
        elif k in ("stacked", "heatmap", "pivot") and not cfg["g2"]:
            why = "Needs “And also split by” (More options)"
        elif k in ("pie", "donut", "funnel") and not additive:
            why = "Shows parts of a total — count a number or a total, not an average"
        elif k == "combo" and m[0] == "count":
            why = "Count a total or an average, not just the number"
        out[k] = why
    return out


def suggest_chart(cfg, ngroups: int) -> str:
    if not cfg["g1"]:
        return "number"
    rules = chart_rules(cfg)
    if cfg["g2"]:
        return "stacked"
    if "@" in cfg["g1"]:
        return "line"
    if ngroups <= PIE_MAX and not rules["pie"]:
        return "pie"
    return "hbar" if ngroups > 8 else "bar"


def chosen_chart(cfg, ngroups: int) -> str:
    rules = chart_rules(cfg)
    c = cfg.get("chart") or "auto"
    if c == "auto" or rules.get(c):
        return suggest_chart(cfg, ngroups)
    return c


def _val(row, cfg):
    m = measure_of(cfg)
    if m[2]:
        return float(row["mv"]) if row.get("mv") is not None else 0.0
    return float(row["n"])


def _key(row, cfg):
    """Rows of two periods are matched by group; date groups by position."""
    return None if "@" in (cfg["g1"] or "") else tuple(str(k) for k in row["keys"])


def compare_rows(summary, prev, cfg):
    """Adds 'prev' / 'change' to each summary row (and the total)."""
    if not prev:
        return
    by_key = {_key(r, cfg): r for r in prev["rows"]} if "@" not in cfg["g1"] else None
    for i, r in enumerate(summary["rows"]):
        p = (prev["rows"][i] if i < len(prev["rows"]) else None) if by_key is None else by_key.get(_key(r, cfg))
        r["prev"] = _val(p, cfg) if p else 0.0


def change_text(cur, prev):
    if prev is None:
        return ""
    if not prev:
        return "new" if cur else "—"
    pct = (cur - prev) / abs(prev) * 100
    return ("+" if pct >= 0 else "−") + f"{abs(pct):.0f}%"


def _pct(v, tot):
    p = v / tot * 100 if tot else 0
    return "<1%" if 0 < p < 1 else f"{p:.0f}%"


def build_pivot(cfg, summary, rows_tot, cols_tot, total, ctx) -> dict:
    """Rows = Group by, columns = Then by, cells = the measure; totals on both edges.
    Totals come from their own queries, so averages are true averages."""
    m = measure_of(cfg)
    ffmt = m[3] if m[2] else "int"
    additive = m[0] == "count" or m[0].startswith("sum")
    val = (lambda r: r.get("mv")) if m[2] else (lambda r: r["n"])
    order = [r["labels"][0] for r in cols_tot["rows"]]
    top = order[:PIVOT_COLS]
    has_other = additive and len(order) > PIVOT_COLS
    ckeys = {r["labels"][0]: raw_key(r["keys"][0]) for r in cols_tot["rows"]}
    cell = {}
    for r in summary["rows"]:
        c = r["labels"][1] if r["labels"][1] in top else ("Other" if has_other else None)
        if c is None:
            continue
        k = (r["labels"][0], c)
        v = val(r)
        if c == "Other":
            cell[k] = (cell.get(k) or 0) + float(v or 0)
        else:
            cell[k] = v
    cols = top + (["Other"] if has_other else [])
    other_tot = sum(float(val(r) or 0) for r in cols_tot["rows"][PIVOT_COLS:]) if has_other else None
    out_rows = []
    for r in rows_tot["rows"]:
        g = r["labels"][0]
        out_rows.append({"label": g, "keys": [raw_key(r["keys"][0])],
                         "cells": [{"v": fmt(cell[(g, c)], ffmt, ctx) if cell.get((g, c)) is not None else "",
                                    "keys": [raw_key(r["keys"][0]), ckeys[c]] if c in ckeys else None} for c in cols],
                         "total": fmt(val(r), ffmt, ctx) if val(r) is not None else "—"})
    col_tot = [{"v": fmt(val(r), ffmt, ctx) if val(r) is not None else "—", "keys": [None, raw_key(r["keys"][0])]}
               for r in cols_tot["rows"][:PIVOT_COLS]]
    if has_other:
        col_tot.append({"v": fmt(other_tot, ffmt, ctx), "keys": None})
    n, mv = total
    return {"kind": "pivot", "row_label": summary["headers"][0], "col_label": summary["headers"][1], "cols": cols,
            "rows": out_rows, "col_totals": col_tot, "grand": fmt(mv if m[2] else n, ffmt, ctx),
            "measure_label": m[1] if m[2] else SUBJECTS[cfg["subject"]]["measures"][0][1],
            "truncated": summary.get("truncated") or len(order) > PIVOT_COLS and not additive}


def build_chart(cfg, summary, prev, ctx, total=None, prev_total=None) -> dict:
    """What the page needs to draw the chart (Chart.js config or HTML parts)."""
    m = measure_of(cfg)
    money = m[3] == "money"
    kind = chosen_chart(cfg, len(summary["rows"]) if summary else 0)
    out = {"kind": kind, "money": money, "measure_label": m[1] if m[2] else SUBJECTS[cfg["subject"]]["measures"][0][1]}
    if money:
        out["cur"] = currency_prefix(ctx)
    if kind == "table":
        return out
    if kind == "number":
        n, mv = total or (0, None)
        v = mv if m[2] else n
        out["value"] = fmt(v, m[3], ctx) if v is not None else "—"
        out["label"] = out["measure_label"]
        if prev_total is not None:
            pn, pmv = prev_total
            pv = pmv if m[2] else pn
            out["change"] = change_text(v or 0, pv or 0)
            out["up"] = (v or 0) >= (pv or 0)
            out["prev"] = fmt(pv, m[3], ctx) if pv is not None else "—"
        return out
    rows = summary["rows"]
    if kind in ("stacked", "heatmap"):
        g1_labels, g2_tot, k1, k2 = [], {}, {}, {}
        for r in rows:
            k1.setdefault(r["labels"][0], raw_key(r["keys"][0]))
            k2.setdefault(r["labels"][1], raw_key(r["keys"][1]))
            if r["labels"][0] not in g1_labels:
                g1_labels.append(r["labels"][0])
            g2_tot[r["labels"][1]] = g2_tot.get(r["labels"][1], 0) + _val(r, cfg)
        top = [k for k, _ in sorted(g2_tot.items(), key=lambda x: -x[1])][:SERIES_MAX]
        matrix = {s: [0.0] * len(g1_labels) for s in top + ["Other"]}
        for r in rows:
            s = r["labels"][1] if r["labels"][1] in top else "Other"
            matrix[s][g1_labels.index(r["labels"][0])] += _val(r, cfg)
        series = [s for s in top + ["Other"] if any(matrix[s])]
        if kind == "heatmap":
            hi = max([max(matrix[s]) for s in series] + [1])
            out["cols"] = series
            out["rows"] = [{"label": g, "cells": [{"v": fmt(matrix[s][i], m[3] if m[2] else "int", ctx),
                                                   "a": round(0.08 + 0.92 * matrix[s][i] / hi, 2),
                                                   "keys": [k1[g], k2[s]] if s in k2 else None} for s in series]}
                           for i, g in enumerate(g1_labels)]
            return out
        out["labels"] = g1_labels
        out["keys"] = [[k1[g]] for g in g1_labels]
        out["series_keys"] = [k2.get(s) for s in series]
        scol = colours_for(series)
        out["datasets"] = [{"label": s, "data": matrix[s], "backgroundColor": scol[i]}
                           for i, s in enumerate(series)]
        return out
    labels = [" · ".join(r["labels"]) for r in rows]
    vals = [_val(r, cfg) for r in rows]
    if kind in ("pie", "donut"):
        pairs = sorted(zip(labels, vals), key=lambda x: -x[1])
        if any(v > 0 for _, v in pairs):
            pairs = [p for p in pairs if p[1] > 0]      # empty slices only clutter the legend
        if len(pairs) > PIE_MAX:
            pairs = pairs[:PIE_MAX - 1] + [("Other", sum(v for _, v in pairs[PIE_MAX - 1:]))]
        tot = sum(v for _, v in pairs) or 1
        out["labels"] = [p[0] for p in pairs]
        cols = colours_for([p[0] for p in pairs])
        out["datasets"] = [{"data": [p[1] for p in pairs], "backgroundColor": cols}]
        kmap = {" · ".join(r["labels"]): r for r in rows}
        out["keys"] = [[raw_key(k) for k in kmap[p[0]]["keys"]] if p[0] in kmap else None for p in pairs]
        out["legend"] = [{"label": p[0], "color": cols[i], "pct": _pct(p[1], tot),
                          "v": fmt(p[1], m[3] if m[2] else "int", ctx)} for i, p in enumerate(pairs)]
        out["centre"] = fmt(sum(v for _, v in pairs), m[3] if m[2] else "int", ctx)
        return out
    if kind == "funnel":
        prev_v = None
        out["steps"] = []
        top = max(vals + [1])
        for lab, v, r in zip(labels, vals, rows):
            out["steps"].append({"label": lab, "v": fmt(v, m[3] if m[2] else "int", ctx),
                                 "keys": [raw_key(k) for k in r["keys"]],
                                 "w": max(2, round(v / top * 100)),
                                 "carried": (f"{v / prev_v * 100:.0f}%" if prev_v else "")})
            prev_v = v or None
        return out
    out["labels"] = labels
    out["keys"] = [[raw_key(k) for k in r["keys"]] for r in rows]
    one = kind in ("bar", "hbar") and prev is None
    ds = [{"label": cfg["period_label"].split(" (")[0], "data": vals,
           "backgroundColor": colours_for(labels) if one else PALETTE[0], "borderColor": PALETTE[0]}]
    if kind == "combo":
        ds = [{"type": "bar", "label": "Number of records", "data": [float(r["n"]) for r in rows],
               "backgroundColor": PALETTE[3], "yAxisID": "y1"},
              {"type": "line", "label": m[1], "data": vals, "borderColor": PALETTE[2], "backgroundColor": PALETTE[2],
               "yAxisID": "y"}]
    elif prev is not None:
        pv = [r.get("prev", 0.0) for r in rows]
        ds.append({"label": compare_range(cfg)[2], "data": pv, "backgroundColor": PALETTE[1],
                   "borderColor": PALETTE[1]})
    out["datasets"] = ds
    return out


# ── Drill-down and dashboard filters (phase R3) ──────────────────────────────

def raw_key(v) -> str:
    """A group value as text the browser can send back ('' = not set)."""
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _bucket_end(start, bucket):
    if bucket == "day":
        return start
    if bucket == "week":
        return start + _dt.timedelta(days=6)
    if bucket == "month":
        nxt = (start.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        return nxt - _dt.timedelta(days=1)
    if bucket == "quarter":
        m = start.month + 3
        nxt = _dt.date(start.year + (m - 1) // 12, (m - 1) % 12 + 1, 1)
        return nxt - _dt.timedelta(days=1)
    return _dt.date(start.year, 12, 31)


def drill_cfg(cfg, k1=None, k2=None):
    """The report narrowed to one group (or group pair), listing every record."""
    out = dict(cfg, view="detailed", page=1, cmp="", chart="auto", g1="", g2="", f=list(cfg["f"]))
    if not out["col"]:
        out["col"] = list(SUBJECTS[cfg["subject"]]["default_cols"])
    for gkey, val in ((cfg["g1"], k1), (cfg["g2"], k2)):
        if not gkey or val is None:
            continue
        col_key, _, bucket = gkey.partition("@")
        if bucket:
            start = _iso(val)
            if not start:
                out["f"].append({"key": col_key, "op": "empty", "val": ""})
                continue
            out["f"].append({"key": col_key, "op": "after", "val": start.isoformat()})
            out["f"].append({"key": col_key, "op": "before", "val": _bucket_end(start, bucket).isoformat()})
        elif val == "":
            out["f"].append({"key": col_key, "op": "empty", "val": ""})
        else:
            out["f"].append({"key": col_key, "op": "is", "val": val[:200]})
    return out


# Dashboard filters map onto each subject's own column.
DASH_MEMBER_COL = {"quotes": "qassigned", "deals": "assigned", "leads": "assigned"}


def apply_dashboard(cfg, period=None, d_from=None, d_to=None, member=None, cmp=None, today=None):
    """The report as it shows on a dashboard whose filters are set."""
    out = dict(cfg, f=list(cfg["f"]), page=1)
    if period and period_ok(period):
        a, b, label = period_range(period, d_from, d_to, today)
        if not (period == "custom" and a is None):
            out.update(period=period, date_from=a, date_to=b, period_label=label)
            if period == "all":
                out["cmp"] = ""
    mcol = DASH_MEMBER_COL.get(cfg["subject"]) or SUBJECTS[cfg["subject"]].get("member")
    if member and mcol:
        out["f"] = [f for f in out["f"] if f["key"] != mcol]
        if member == "__none__":
            out["f"].append({"key": mcol, "op": "empty", "val": ""})
        else:
            out["f"].append({"key": mcol, "op": "is", "val": member[:64]})
    out["cmp"] = ""
    return out


# ── Saved reports ────────────────────────────────────────────────────────────
_tables_ok = False


def ensure_tables(conn):
    global _tables_ok
    if _tables_ok:
        return
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('report_defs') IS NOT NULL")
        if not cur.fetchone()[0]:
            cur.execute("SET lock_timeout = '3s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS report_defs (
                               id              BIGSERIAL PRIMARY KEY,
                               tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                               name            VARCHAR(120) NOT NULL,
                               subject         VARCHAR(30) NOT NULL,
                               config          JSONB NOT NULL DEFAULT '{}'::jsonb,
                               created_by_key  VARCHAR(64),
                               created_by      VARCHAR(200),
                               created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                               updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                               last_opened_at  TIMESTAMPTZ)""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_report_defs_tenant ON report_defs (tenant_id, updated_at DESC)")
            conn.commit()
        _tables_ok = True
    except Exception as e:
        conn.rollback()
        print("⚠️ report_defs table:", e)
    finally:
        cur.close()


import report_subjects  # noqa: E402,F401  (R5 subjects register themselves in SUBJECTS)
