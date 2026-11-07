"""
access_log.py — who opened or exported a buyer's record (7 Nov 2026, Shopify 7A item 15).

One line per person, per record, per action:
  opened          a lead, contact, customer or company page
  opened_inbox    a conversation in the Inbox (once per 30 minutes)
  exported        any export / report download (with row count when it can be read)
  downloaded      an import file or a chat transcript

Written by after_request() for every portal page (registered in portal_app),
so new pages of these kinds are covered without touching each route. Never
breaks a page: any failure is printed and the response goes out unchanged.

Holds record NUMBERS, never buyers' names or emails. A phone number or chat
id is stored masked (last 4) plus a keyed fingerprint (data_crypto) so an
exact number can still be searched for.

Protected in the database: lines can't be changed, and can only be deleted
once older than 12 months (trigger access_log_guard). Nightly clean-up and
the unusual-activity email: `python3 access_log.py nightly|alerts`.

Who sees it: Admin › Access Log (PhiXtra owners, everything) and the
business owner's Settings › Team › Record Access (own team only — lines
written while a PhiXtra admin was viewing as the business are NEVER shown
there; user decision 7 Nov 2026).
"""
import io
import re
import time

from db import get_db_connection

KEEP_MONTHS = 12
OPEN_WINDOW_MIN = 10
INBOX_WINDOW_MIN = 30
ALERT_OPENS_PER_HOUR = 300
ALERT_EXPORT_ROWS_PER_DAY = 1000
ALERT_TO = "philip@phixtra.com"

ACTIONS = {"opened": "Opened", "opened_inbox": "Opened in Inbox", "exported": "Exported", "downloaded": "Downloaded"}
TYPES = {"lead": "Lead", "contact": "Contact", "customer": "Customer", "company": "Company",
         "conversation": "Conversation", "export": "Export", "import_file": "Import file", "chat": "Chat transcript",
         "quotation": "Quotation PDF", "quote_request": "Quotation request PDF", "voice_note": "Voice note"}

# endpoint → (record type, view arg, action)
_PAGES = {
    "portal.lead_detail":             ("lead", "lead_id", "opened"),
    "portal.opportunity_detail":      ("lead", "lead_id", "opened"),
    "portal.sales_pipeline_history":  ("lead", "lead_id", "opened"),
    "portal.whatsapp_contact_detail": ("contact", "contact_id", "opened"),
    "portal.crm_company_detail":      ("company", "company_id", "opened"),
    "portal.customer_detail":         ("customer", "phone", "opened"),
    "portal.crm_import_download":     ("import_file", "import_id", "downloaded"),
    "portal.chat_archive_session_export": ("chat", "session_id", "downloaded"),
    "portal.chat_archive_session_view":   ("chat", "session_id", "opened"),
    # Documents / recordings holding one buyer's details (step 3, 7 Nov 2026)
    "portal.quotes_quotation_pdf":    ("quotation", "qid", "opened"),
    "portal.quotes_request_pdf":      ("quote_request", "request_id", "opened"),
    "estate.voice_note_audio":        ("voice_note", "note_id", "opened"),
}
_EXPORT_NAMES = {
    "portal.report_pipeline_overview_export": "Pipeline Overview report",
    "portal.report_leads_sources_export": "Lead Sources report",
    "portal.report_not_a_fit_export": "Not a Fit report",
    "portal.report_custom_entity_export": "Custom report",
    "portal.report_crm_export": "CRM report",
    "portal.report_export": "Report",
    "portal.chat_archive_export": "Chat Archive",
    "portal.whatsapp_contacts_export": "Contacts",
    "portal.sales_pipeline_export": "Sales Pipeline",
    "portal.quotes_request_xlsx": "Quotation lines",
    "calls.calls_report_export": "Calls report",
    "portal.team_performance_contacted": "Businesses contacted",
    "portal.sms_campaign_extract_numbers": "SMS campaign numbers",
}
# Never logged as exports: our own files, not buyer data.
_SKIP = {"portal.plugin_download", "portal.invoice_download"}
_EXPORT_BLUEPRINTS = ("portal", "calls", "estate")
_EXPORT_RULE = re.compile(r"(/export\b|/export/|\.csv$|\.xlsx$)")

_ready = False


# ── Table ────────────────────────────────────────────────────────────────
def ensure(cur):
    global _ready
    if _ready:
        return
    cur.execute("""CREATE TABLE IF NOT EXISTS access_log (
                       id             BIGSERIAL PRIMARY KEY,
                       at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                       tenant_id      INTEGER,
                       actor_kind     VARCHAR(10) NOT NULL,      -- owner | team | phixtra
                       actor_id       INTEGER,                   -- customer id / team member id
                       actor_name     TEXT,
                       admin_username TEXT,                      -- PhiXtra admin when viewing as the business
                       action         VARCHAR(20) NOT NULL,
                       record_type    VARCHAR(20) NOT NULL,
                       record_id      TEXT,
                       record_fp      VARCHAR(64),
                       detail         TEXT,
                       rows_count     INTEGER,
                       ip             TEXT,
                       browser        TEXT)""")
    cur.execute("CREATE INDEX IF NOT EXISTS access_log_tenant_at ON access_log (tenant_id, at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS access_log_at ON access_log (at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS access_log_dedupe ON access_log (actor_kind, actor_id, record_type, record_id, at DESC)")
    cur.execute("""CREATE TABLE IF NOT EXISTS access_log_alerts (
                       day DATE NOT NULL, actor_key TEXT NOT NULL, kind VARCHAR(20) NOT NULL,
                       sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (day, actor_key, kind))""")
    cur.execute(f"""CREATE OR REPLACE FUNCTION access_log_guard() RETURNS trigger AS $$
                    BEGIN
                      IF TG_OP = 'UPDATE' THEN
                        RAISE EXCEPTION 'access_log lines cannot be changed';
                      ELSIF TG_OP = 'TRUNCATE' THEN
                        RAISE EXCEPTION 'access_log cannot be emptied';
                      ELSIF TG_OP = 'DELETE' AND OLD.at > NOW() - INTERVAL '{KEEP_MONTHS} months' THEN
                        RAISE EXCEPTION 'access_log lines are kept {KEEP_MONTHS} months';
                      END IF;
                      RETURN OLD;
                    END $$ LANGUAGE plpgsql""")
    cur.execute("SELECT 1 FROM pg_trigger WHERE tgname='access_log_guard_rows'")
    if not cur.fetchone():
        cur.execute("""CREATE TRIGGER access_log_guard_rows BEFORE UPDATE OR DELETE ON access_log
                       FOR EACH ROW EXECUTE FUNCTION access_log_guard()""")
        cur.execute("""CREATE TRIGGER access_log_guard_truncate BEFORE TRUNCATE ON access_log
                       FOR EACH STATEMENT EXECUTE FUNCTION access_log_guard()""")
    _ready = True


# ── Helpers ──────────────────────────────────────────────────────────────
def mask(value) -> str:
    """'+44 7700 900123' → '…0123'; 'web:abcd1234' → 'web chat …1234'."""
    v = str(value or "")
    kind = ""
    if v.startswith("web:"):
        kind, v = "web chat ", v[4:]
    elif v.startswith("fb:"):
        kind, v = "Messenger ", v[3:]
    tail = re.sub(r"\W", "", v)[-4:]
    return f"{kind}…{tail}" if tail else kind.strip()


def browser(ua: str) -> str:
    ua = ua or ""
    name = ("Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else "Chrome" if "Chrome/" in ua
            else "Firefox" if "Firefox/" in ua else "Safari" if "Safari/" in ua else "Other")
    dev = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
           else "Mac" if "Macintosh" in ua else "Windows" if "Windows" in ua else "")
    if "PhiXtra" in ua or "wv)" in ua:
        name = "PhiXtra app"
    return f"{name}, {dev}" if dev else name


def client_ip(req) -> str:
    ip = (req.headers.get("CF-Connecting-IP") or (req.headers.get("X-Forwarded-For") or "").split(",")[0]
          or req.remote_addr or "")
    return ip.strip()[:64]


def count_rows(resp):
    """Data rows in a CSV or XLSX download (header row not counted); None if unknown."""
    try:
        if resp.is_streamed and not resp.direct_passthrough:
            return None
        if resp.content_length and resp.content_length > 30_000_000:
            return None
        resp.direct_passthrough = False
        data = resp.get_data()
        resp.set_data(data)
        mt = (resp.mimetype or "").lower()
        if "csv" in mt or mt == "text/plain":
            import csv
            return _data_rows(csv.reader(io.StringIO(data.decode("utf-8-sig", "ignore"))))
        if "spreadsheet" in mt or "excel" in mt or data[:2] == b"PK":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
            n = _data_rows(wb.worksheets[0].iter_rows(values_only=True))
            wb.close()
            return n
    except Exception as e:
        print("⚠️ access_log.count_rows:", e)
    return None


def _data_rows(rows) -> int:
    """Records in a table: rows under the first header (first row with 3+ filled
    cells), skipping title lines and a summary block above it."""
    n, seen_header = 0, False
    for r in rows:
        filled = sum(1 for c in (r or ()) if c not in (None, "") and str(c).strip())
        if not seen_header:
            seen_header = filled >= 3
        elif filled >= 1:
            n += 1
    return n


def _actor(sess, cur):
    """(kind, id, name, admin_username, tenant_id) for the logged-in person, or None."""
    imp = sess.get("impersonate_customer_id")
    if imp:
        cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (int(imp),))
        r = cur.fetchone()
        admin = sess.get("portal_admin_username") or "PhiXtra admin"
        return ("phixtra", None, admin, admin, r[0] if r else None)
    tm = sess.get("team_member_id")
    if tm:
        cur.execute("""SELECT tenant_id, COALESCE(NULLIF(TRIM(COALESCE(first_name,'')||' '||COALESCE(last_name,'')),''), name, email)
                         FROM team_members WHERE id=%s""", (int(tm),))
        r = cur.fetchone()
        if r:
            return ("team", int(tm), r[1], None, r[0])
    cid = sess.get("customer_id")
    if cid:
        cur.execute("""SELECT tenant_id, COALESCE(NULLIF(TRIM(COALESCE(first_name,'')||' '||COALESCE(last_name,'')),''), email)
                         FROM customers WHERE id=%s""", (int(cid),))
        r = cur.fetchone()
        if r:
            return ("owner", int(cid), r[1], None, r[0])
    return None


def _what(req, g):
    """(record_type, record_id, record_fp, action, detail, window_min, is_export) or None."""
    ep = req.endpoint or ""
    if not ep or ep in _SKIP or req.method != "GET":
        return None
    va = req.view_args or {}
    if ep == "portal.my_inbox":
        key = getattr(g, "access_inbox_key", None)
        if not key:
            return None
        import data_crypto
        return ("conversation", mask(key), data_crypto.fingerprint("conv", key), "opened_inbox",
                "Shopify trade card shown" if getattr(g, "access_trade_card", False) else None, INBOX_WINDOW_MIN, False)
    if ep in _PAGES:
        rtype, arg, action = _PAGES[ep]
        val = va.get(arg)
        fp = None
        if rtype in ("customer", "chat"):
            import data_crypto
            fp = data_crypto.fingerprint(rtype, val)
            val = mask(val)
        detail = []
        if ep == "portal.sales_pipeline_history":
            detail.append("history")
        if va.get("fmt"):
            detail.append(str(va["fmt"]).upper())
        if va.get("kind"):
            detail.append(str(va["kind"]))
        if getattr(g, "access_trade_card", False):
            detail.append("Shopify trade card shown")
        return (rtype, str(val) if val is not None else None, fp, action, ", ".join(detail) or None,
                0 if action == "downloaded" else OPEN_WINDOW_MIN, action == "downloaded")
    bp = ep.split(".", 1)[0]
    rule = req.url_rule.rule if req.url_rule else ""
    if bp in _EXPORT_BLUEPRINTS and _EXPORT_RULE.search(rule) and "template" not in rule:
        name = _EXPORT_NAMES.get(ep) or rule
        extra = [str(va[k]) for k in ("report", "entity", "report_type") if va.get(k)]
        if va.get("fmt"):
            extra.append(str(va["fmt"]).upper())
        if va.get("request_id"):
            extra.append(f"request {va['request_id']}")
        return ("export", name + (f" ({', '.join(extra)})" if extra else ""), None, "exported", None, 0, True)
    return None


def _attachment_export(req, resp):
    """Safety net: any other file the business portal sends as a download
    (Content-Disposition: attachment) is logged as an export, so a new export
    button is covered without touching this file. Blank templates excluded."""
    ep = req.endpoint or ""
    if not ep or ep in _SKIP or req.method != "GET":
        return None
    if ep.split(".", 1)[0] not in _EXPORT_BLUEPRINTS:
        return None
    if "attachment" not in (resp.headers.get("Content-Disposition") or "").lower():
        return None
    rule = req.url_rule.rule if req.url_rule else ""
    if "template" in rule:
        return None
    return ("export", _EXPORT_NAMES.get(ep) or rule, None, "exported", None, 0, True)


def _count_numbers(resp):
    """Phone numbers in a plain-text numbers download (comma / line separated)."""
    try:
        resp.direct_passthrough = False
        data = resp.get_data(); resp.set_data(data)
        return len([t for t in re.split(r"[,;\s]+", data.decode("utf-8", "ignore")) if t.strip()])
    except Exception as e:
        print("⚠️ access_log._count_numbers:", e)
        return None


# ── Writing ──────────────────────────────────────────────────────────────
def after_request(resp):
    """Flask after_request hook. Always returns resp unchanged in meaning."""
    try:
        if resp.status_code != 200:
            return resp
        from flask import request, session, g
        what = _what(request, g) or _attachment_export(request, resp)
        if not what:
            return resp
        rtype, rid, fp, action, detail, window, is_export = what
        if request.endpoint == "portal.sms_campaign_extract_numbers":
            rows = _count_numbers(resp)
        else:
            rows = count_rows(resp) if is_export else None
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            ensure(cur)
            who = _actor(session, cur)
            if not who:
                return resp
            kind, aid, name, admin, tenant_id = who
            if window:
                cur.execute(f"""SELECT 1 FROM access_log WHERE actor_kind=%s AND actor_id IS NOT DISTINCT FROM %s
                                   AND admin_username IS NOT DISTINCT FROM %s AND tenant_id IS NOT DISTINCT FROM %s
                                   AND record_type=%s AND record_id IS NOT DISTINCT FROM %s AND action=%s
                                   AND at > NOW() - INTERVAL '{int(window)} minutes' LIMIT 1""",
                            (kind, aid, admin, tenant_id, rtype, rid, action))
                if cur.fetchone():
                    conn.commit()
                    return resp
            cur.execute("""INSERT INTO access_log (tenant_id, actor_kind, actor_id, actor_name, admin_username, action,
                                                   record_type, record_id, record_fp, detail, rows_count, ip, browser)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (tenant_id, kind, aid, name, admin, action, rtype, rid, fp or None, detail, rows,
                         client_ip(request), browser(request.headers.get("User-Agent", ""))))
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        print("⚠️ access_log:", e)
    return resp


def write_admin_export(admin_username: str, what: str, rows: int, tenant_id=None, req=None):
    """Admin › Access Log CSV download: logged like any export."""
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            ensure(cur)
            cur.execute("""INSERT INTO access_log (tenant_id, actor_kind, actor_name, admin_username, action, record_type,
                                                   record_id, rows_count, ip, browser)
                           VALUES (%s,'phixtra',%s,%s,'exported','export',%s,%s,%s,%s)""",
                        (tenant_id, admin_username, admin_username, what, rows,
                         client_ip(req) if req else None, browser(req.headers.get("User-Agent", "")) if req else None))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print("⚠️ access_log.write_admin_export:", e)


def view_rows(rows, tz):
    """Rows ready for the table: local time, who, what."""
    out = []
    for r in rows:
        r = dict(r)
        r["at_local"] = r["at"].astimezone(tz) if r.get("at") else None
        if r["actor_kind"] == "phixtra":
            r["who"] = f"PhiXtra admin ({r.get('admin_username') or '?'})"
        else:
            r["who"] = (r.get("actor_name") or "?") + (" (owner)" if r["actor_kind"] == "owner" else " (team)")
        r["action_label"] = ACTIONS.get(r["action"], r["action"])
        t = TYPES.get(r["record_type"], r["record_type"])
        rec = r.get("record_id") or ""
        r["record_label"] = rec if r["record_type"] == "export" else f"{t} {rec}".strip()
        r["link"] = None
        if r["record_type"] == "lead" and rec.isdigit():
            r["link"] = f"/leads/{rec}"
        elif r["record_type"] == "contact" and rec.isdigit():
            r["link"] = f"/whatsapp/contacts/{rec}"
        elif r["record_type"] == "company" and rec.isdigit():
            r["link"] = f"/crm/companies/{rec}"
        out.append(r)
    return out


def mark_trade_card():
    """Called by shopify_b2b when a trade card is shown on the page being built."""
    try:
        from flask import g, has_request_context
        if has_request_context():
            g.access_trade_card = True
    except Exception:
        pass


# ── Reading (Admin page + business owner page) ────────────────────────────
def search(f: dict, tenant_id=None, limit=200, offset=0):
    """Lines matching filters, newest first. tenant_id set = one business view:
    PhiXtra admin lines are never included there."""
    where, args = ["TRUE"], []
    import datetime as _d
    for k in ("date_from", "date_to"):          # ignore anything that isn't a real YYYY-MM-DD date
        if f.get(k):
            try:
                _d.date.fromisoformat(str(f[k]))
            except ValueError:
                f[k] = ""
    if tenant_id is not None:
        where.append("l.tenant_id=%s AND l.actor_kind<>'phixtra'"); args.append(int(tenant_id))
    elif f.get("tenant_id"):
        where.append("l.tenant_id=%s"); args.append(int(f["tenant_id"]))
    if f.get("date_from"):
        where.append("l.at >= %s::date"); args.append(f["date_from"])
    if f.get("date_to"):
        where.append("l.at < %s::date + 1"); args.append(f["date_to"])
    if f.get("who"):
        where.append("(l.actor_name ILIKE %s OR l.admin_username ILIKE %s)"); args += [f"%{f['who']}%"] * 2
    if f.get("kind") in ("owner", "team", "phixtra") and tenant_id is None:
        where.append("l.actor_kind=%s"); args.append(f["kind"])
    if f.get("action") in ACTIONS:
        where.append("l.action=%s"); args.append(f["action"])
    if f.get("record_type") in TYPES:
        where.append("l.record_type=%s"); args.append(f["record_type"])
    if f.get("record"):
        rec = str(f["record"]).strip()
        import data_crypto
        # A phone number can be typed any way: +44 7700 900010, 447700900010, 07700… (stored without +)
        compact = re.sub(r"[\s\-()]", "", rec)
        digits = re.sub(r"\D", "", rec)
        variants = {rec, compact, compact.lstrip("+")}
        if len(digits) >= 7:
            variants |= {digits, "+" + digits}
        fps = [data_crypto.fingerprint(k, v) for k in ("customer", "chat", "conv") for v in variants if v]
        where.append("(l.record_id ILIKE %s OR l.record_fp = ANY(%s))"); args += [f"%{rec}%", fps]
    conn = get_db_connection()
    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ensure(cur)
        w = " AND ".join(where)
        cur.execute(f"SELECT count(*) AS n FROM access_log l WHERE {w}", args)
        total = cur.fetchone()["n"]
        cur.execute(f"""SELECT l.*, t.name AS tenant_name FROM access_log l LEFT JOIN tenants t ON t.id=l.tenant_id
                         WHERE {w} ORDER BY l.at DESC, l.id DESC LIMIT %s OFFSET %s""", args + [int(limit), int(offset)])
        rows = cur.fetchall()
        conn.commit()
        return rows, total
    finally:
        conn.close()


def today_summary(tenant_id=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ensure(cur)
        w, a = ("tenant_id=%s AND actor_kind<>'phixtra' AND ", [int(tenant_id)]) if tenant_id is not None else ("", [])
        cur.execute(f"""SELECT count(*) FILTER (WHERE action IN ('opened','opened_inbox')),
                               count(*) FILTER (WHERE action IN ('exported','downloaded')),
                               COALESCE(sum(rows_count) FILTER (WHERE action='exported'),0)
                          FROM access_log WHERE {w} at >= date_trunc('day', NOW())""", a)
        o, e, r = cur.fetchone()
        conn.commit()
        return {"opens": o, "exports": e, "rows": r, "unusual": unusual(cur, tenant_id) if tenant_id is None else []}
    finally:
        conn.close()


def unusual(cur, tenant_id=None):
    """People over the limits: > ALERT_OPENS_PER_HOUR opens in the last hour,
    or > ALERT_EXPORT_ROWS_PER_DAY rows exported today."""
    out = []
    cur.execute("""SELECT actor_kind, actor_id, COALESCE(admin_username, actor_name), tenant_id, count(*)
                     FROM access_log WHERE action IN ('opened','opened_inbox') AND at > NOW() - INTERVAL '1 hour'
                    GROUP BY 1,2,3,4 HAVING count(*) > %s""", (ALERT_OPENS_PER_HOUR,))
    for k, i, n, t, c in cur.fetchall():
        out.append({"kind": "opens", "actor_key": f"{k}:{i or n}", "who": n, "tenant_id": t,
                    "text": f"{n} opened {c} records in the last hour"})
    cur.execute("""SELECT actor_kind, actor_id, COALESCE(admin_username, actor_name), tenant_id, sum(rows_count)
                     FROM access_log WHERE action='exported' AND at >= date_trunc('day', NOW())
                    GROUP BY 1,2,3,4 HAVING sum(rows_count) > %s""", (ALERT_EXPORT_ROWS_PER_DAY,))
    for k, i, n, t, c in cur.fetchall():
        out.append({"kind": "export_rows", "actor_key": f"{k}:{i or n}", "who": n, "tenant_id": t,
                    "text": f"{n} exported {c:,} rows today"})
    return out


# ── Jobs ─────────────────────────────────────────────────────────────────
def nightly():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ensure(cur)
        cur.execute(f"DELETE FROM access_log WHERE at < NOW() - INTERVAL '{KEEP_MONTHS} months'")
        n = cur.rowcount
        cur.execute("DELETE FROM access_log_alerts WHERE day < CURRENT_DATE - 30")
        conn.commit()
        if n:
            print(time.strftime("%F %T"), f"access_log: deleted {n} lines older than {KEEP_MONTHS} months")
    finally:
        conn.close()


def alerts():
    """Email PhiXtra (never the business) once per person per day per kind."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ensure(cur)
        sent = []
        for u in unusual(cur):
            cur.execute("""INSERT INTO access_log_alerts (day, actor_key, kind) VALUES (CURRENT_DATE, %s, %s)
                           ON CONFLICT DO NOTHING""", (f"{u['tenant_id']}:{u['actor_key']}", u["kind"]))
            if cur.rowcount:
                cur.execute("SELECT name FROM tenants WHERE id=%s", (u["tenant_id"],))
                t = cur.fetchone()
                u["tenant"] = t[0] if t else f"tenant {u['tenant_id']}"
                sent.append(u)
        conn.commit()
    finally:
        conn.close()
    if sent:
        body = "Unusual record access in PhiXtra:\n\n" + "\n".join(f"- {u['text']} ({u['tenant']})" for u in sent)
        body += "\n\nSee Admin › Access Log: https://portal.phixtra.com/admin/access-log\n"
        _email(f"PhiXtra: unusual record access ({len(sent)})", body)
        print(time.strftime("%F %T"), "access_log alert sent:", [u["text"] for u in sent])


def _email(subject, body):
    import os, smtplib, ssl
    from email.message import EmailMessage
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER")
    m["To"] = ALERT_TO
    m.set_content(body)
    port = int(os.getenv("SMTP_PORT", "587"))
    if port == 465:
        s = smtplib.SMTP_SSL(os.getenv("SMTP_HOST"), port, context=ssl.create_default_context(), timeout=30)
    else:
        s = smtplib.SMTP(os.getenv("SMTP_HOST"), port, timeout=30)
        if (os.getenv("SMTP_USE_TLS", "true")).lower() in ("1", "true", "yes"):
            s.starttls(context=ssl.create_default_context())
    s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
    s.send_message(m)
    s.quit()


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    {"nightly": nightly, "alerts": alerts}.get((sys.argv[1:] or ["nightly"])[0], nightly)()
