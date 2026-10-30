"""
quote_requests.py — Quotations › Requests (Intelligent RFQ, Step 1 Phase 1,
30 Oct 2026). Approved design v5 https://claude.ai/artifact/1cF1TzQiVMfM3wNCv43mZJ
(user: "group in phases to start building").

PhiXtra never makes the quotation. A request is what the buyer asked for;
the salesperson makes the quotation in the business's own software (Xero,
QuickBooks, Sage, Excel …), sends it, and clicks "Quote sent" here so
PhiXtra can follow up and record Won or Lost.

  to_quote   → waiting for the salesperson (clock running)
  quote_sent → with the buyer (deal on the Sales Pipeline at Proposal sent)
  won        → buyer said yes (deal Won)
  lost       → buyer said no (deal Lost) or not quoted (reason kept)

Phase 1: requests typed in by the team, the handover page, Copy lines /
Download Excel, Quote sent, Won / Lost, alerts when a request is close to or
past its time. Phase 2 adds requests the AI makes from email / WhatsApp /
chat; Phase 3 the AI reading the buyer's answer; Phase 4 the website list.

Same plan rule as AI Lead Qualification (B2B plan + PhiXtra's own account).
"""
import io

import re
import threading
import time
from datetime import datetime, timedelta, time as dtime

import psycopg2.extras

from db import get_db_connection

STATUSES = [("to_quote", "To quote"), ("quote_sent", "Quote sent"), ("won", "Won"), ("lost", "Lost")]
STATUS_WORDS = dict(STATUSES)
CHANNELS = [("manual", "Typed in"), ("email", "Email"), ("whatsapp", "WhatsApp"), ("chat", "Website chat"),
            ("website", "Website quote list"), ("phone", "Phone call")]
CHANNEL_WORDS = dict(CHANNELS)
SENT_BY = [("email", "Email"), ("whatsapp", "WhatsApp"), ("software", "From our quoting software"),
           ("post", "Post or by hand"), ("other", "Other")]
SENT_BY_WORDS = dict(SENT_BY)
SOFTWARE = [("xero", "Xero"), ("quickbooks", "QuickBooks"), ("sage", "Sage"), ("zoho", "Zoho Books"),
            ("excel", "Excel or Word"), ("other", "Other")]
TARGETS = [("30m", "30 minutes"), ("1h", "1 hour"), ("2h", "2 hours"), ("4h", "4 hours"),
           ("same_day", "The same working day"), ("next_day", "By the end of the next working day")]
TARGET_HOURS = {"30m": 0.5, "1h": 1, "2h": 2, "4h": 4}
# Not quoting a request (it never reached the buyer as a quotation).
NOT_QUOTED_REASONS = ["Out of stock", "Not a product we sell", "Order too small", "Can't deliver there",
                      "Buyer stopped answering", "Duplicate request"]
LOST_REASONS = ["Price too high", "Bought elsewhere", "Delivery time too long", "Delivery cost too high",
                "No longer needed", "No answer"]
MAX_LINES = 200
STAGE_AFTER_PROPOSAL = ("proposal_sent", "negotiating")
ACTIVE = ("to_quote", "quote_sent")
MAX_PDF = 10 * 1024 * 1024

DEFAULTS = {
    "target": "1h",
    "work_start": "08:00", "work_end": "18:00",
    "software": "",
    "alert_new": True,          # tell the salesperson when a request is handed to them
    "alert_due": True,          # ask the salesperson "did you send it?" when the time is up
    "alert_manager": True,      # tell the account owner one hour after that
}


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


_tables_ok = False


def ensure_tables():
    global _tables_ok
    if _tables_ok:
        return
    conn, cur = _db()
    try:
        # Everything already there (the usual case): no DDL at all, so a page
        # never waits for a table lock behind someone else's open transaction.
        cur.execute("""SELECT to_regclass('quote_request_events') IS NOT NULL AND to_regclass('quote_settings') IS NOT NULL
                              AND to_regclass('idx_quote_requests_source') IS NOT NULL
                              AND (SELECT count(*) FROM information_schema.columns WHERE table_schema='public'
                                    AND table_name='quote_requests'
                                    AND column_name IN ('first_quote_at','source_key','edited','ai_answer')) = 4 AS ok""")
        if cur.fetchone()["ok"]:
            _tables_ok = True
            return
        cur.execute("SET lock_timeout = '5s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS quote_requests (
                           id             BIGSERIAL PRIMARY KEY,
                           tenant_id      INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                           ref_no         INTEGER NOT NULL,
                           lead_id        INTEGER,
                           status         VARCHAR(12) NOT NULL DEFAULT 'to_quote',
                           channel        VARCHAR(12) NOT NULL DEFAULT 'manual',
                           buyer_company  VARCHAR(300),
                           buyer_name     VARCHAR(200),
                           buyer_email    VARCHAR(255),
                           buyer_phone    VARCHAR(40),
                           buyer_country  VARCHAR(2),
                           deliver_to     VARCHAR(300),
                           needed_by      DATE,
                           notes          TEXT,
                           buyer_words    TEXT,
                           lines          JSONB NOT NULL DEFAULT '[]'::jsonb,
                           estimate       NUMERIC(14,2),
                           currency       VARCHAR(3),
                           assigned_key   VARCHAR(64),
                           assigned_to    VARCHAR(200),
                           due_at         TIMESTAMPTZ,
                           quote_value    NUMERIC(14,2),
                           quote_currency VARCHAR(3),
                           quote_valid_until DATE,
                           quote_number   VARCHAR(60),
                           quote_sent_via VARCHAR(12),
                           quote_sent_at  TIMESTAMPTZ,
                           first_quote_at TIMESTAMPTZ,
                           quote_sent_by  VARCHAR(200),
                           quote_pdf      BYTEA,
                           quote_pdf_name VARCHAR(200),
                           close_reason   VARCHAR(300),
                           closed_at      TIMESTAMPTZ,
                           closed_by      VARCHAR(200),
                           created_by     VARCHAR(200),
                           created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           UNIQUE (tenant_id, ref_no))""")
        cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS first_quote_at TIMESTAMPTZ")
        # Phase 2: what the AI read it from (one request per email / chat) and
        # whether a person has changed it since (then the AI never overwrites it).
        cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS source_key VARCHAR(200)")
        cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS edited BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_quote_requests_source ON quote_requests (tenant_id, source_key)")
        # Phase 3: the AI's last decision on the buyer's answer (for Undo).
        cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS ai_answer JSONB")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_quote_requests_tenant ON quote_requests (tenant_id, status, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_quote_requests_lead ON quote_requests (lead_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_quote_requests_due ON quote_requests (due_at) WHERE status='to_quote'")
        cur.execute("""CREATE TABLE IF NOT EXISTS quote_request_events (
                           id          BIGSERIAL PRIMARY KEY,
                           request_id  BIGINT NOT NULL REFERENCES quote_requests(id) ON DELETE CASCADE,
                           what        TEXT NOT NULL,
                           by_label    VARCHAR(200),
                           created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_quote_request_events ON quote_request_events (request_id, created_at)")
        cur.execute("""CREATE TABLE IF NOT EXISTS quote_settings (
                           tenant_id  INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
                           settings   JSONB NOT NULL DEFAULT '{}'::jsonb,
                           updated_by VARCHAR(200),
                           updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.commit()
        _tables_ok = True
    except Exception as e:
        conn.rollback()
        print("⚠️ quote_requests tables:", e)
    finally:
        cur.close(); conn.close()


def allowed(tenant_id) -> bool:
    import lead_qualify
    return lead_qualify.allowed(tenant_id)


# ── settings ────────────────────────────────────────────────────────────────

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _clean_settings(raw: dict) -> dict:
    s = dict(DEFAULTS)
    raw = raw or {}
    if raw.get("target") in dict(TARGETS):
        s["target"] = raw["target"]
    for k in ("work_start", "work_end"):
        if isinstance(raw.get(k), str) and _HHMM.match(raw[k].strip()):
            s[k] = raw[k].strip()
    if s["work_end"] <= s["work_start"]:
        s["work_start"], s["work_end"] = DEFAULTS["work_start"], DEFAULTS["work_end"]
    if raw.get("software") in dict(SOFTWARE) or raw.get("software") == "":
        s["software"] = raw["software"]
    for k in ("alert_new", "alert_due", "alert_manager"):
        if k in raw:
            v = raw[k]
            s[k] = v is True or v in ("1", "true", "on")
    return s


def settings(tenant_id) -> dict:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT settings FROM quote_settings WHERE tenant_id=%s", (int(tenant_id),))
        r = cur.fetchone()
    finally:
        cur.close(); conn.close()
    return _clean_settings((r or {}).get("settings") or {})


def save_settings(tenant_id, raw: dict, by_label: str = "") -> dict:
    s = _clean_settings(raw)
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO quote_settings (tenant_id, settings, updated_by, updated_at)
                       VALUES (%s,%s,%s,NOW())
                       ON CONFLICT (tenant_id) DO UPDATE SET settings=EXCLUDED.settings,
                              updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                    (int(tenant_id), psycopg2.extras.Json(s), (by_label or "")[:200]))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return s


# ── the clock ───────────────────────────────────────────────────────────────

def _hm(v: str) -> dtime:
    h, m = v.split(":")
    return dtime(int(h), int(m))


def _next_working_start(t: datetime, ws: dtime, we: dtime) -> datetime:
    """t itself if inside working hours, else the next working-day start."""
    while True:
        if t.weekday() < 5:
            if t.time() < ws:
                return t.replace(hour=ws.hour, minute=ws.minute, second=0, microsecond=0)
            if t.time() < we:
                return t
        t = (t + timedelta(days=1)).replace(hour=ws.hour, minute=ws.minute, second=0, microsecond=0)


def due_at(created: datetime, s: dict, tz) -> datetime:
    """When the quotation should be sent, counting working hours only
    (Mon–Fri, the business's own hours and time zone)."""
    ws, we = _hm(s["work_start"]), _hm(s["work_end"])
    t = _next_working_start(created.astimezone(tz), ws, we)
    if s["target"] in TARGET_HOURS:
        left = timedelta(hours=TARGET_HOURS[s["target"]])
        while True:
            end = t.replace(hour=we.hour, minute=we.minute, second=0, microsecond=0)
            if t + left <= end:
                return t + left
            left -= end - t
            t = _next_working_start(end + timedelta(minutes=1), ws, we)
    end_today = t.replace(hour=we.hour, minute=we.minute, second=0, microsecond=0)
    if s["target"] == "same_day":
        return end_today
    nxt = _next_working_start(end_today + timedelta(minutes=1), ws, we)
    return nxt.replace(hour=we.hour, minute=we.minute, second=0, microsecond=0)


def time_left(r: dict, now: datetime) -> dict:
    """{"text", "tone"} for the clock: ok / soon / late (to_quote only)."""
    if r.get("status") != "to_quote" or not r.get("due_at"):
        return {"text": "", "tone": ""}
    secs = (r["due_at"] - now).total_seconds()
    if secs > 10 * 3600:
        # Far off (e.g. asked at night): say when, not how long.
        d = r["due_at"].astimezone(now.tzinfo)
        when = d.strftime("%H:%M") if d.date() == now.date() else d.strftime("%a %H:%M")
        return {"text": f"by {when}", "tone": "ok"}
    mins = int(abs(secs) // 60)
    if mins < 60:
        txt = f"{mins} min"
    elif mins < 48 * 60:
        txt = f"{mins // 60} h {mins % 60:02d} min" if mins < 10 * 60 else f"{mins // 60} h"
    else:
        txt = f"{mins // (24 * 60)} days"
    if secs < 0:
        return {"text": f"Overdue {txt}", "tone": "late"}
    return {"text": txt, "tone": "soon" if secs <= 15 * 60 else "ok"}


# ── lines ───────────────────────────────────────────────────────────────────

def _num(v, lo=0, hi=1e12):
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if x != x or x < lo or x > hi:
        return None
    return round(x, 2)


def clean_lines(raw, products: dict) -> list:
    """[{product_id, name, qty, unit, price, note}] from the form. A line
    with a product from the list takes its name and list price from it; a
    free-text line (not on the list) keeps what was typed, price unknown."""
    out = []
    for ln in raw or []:
        if not isinstance(ln, dict):
            continue
        pid = str(ln.get("product_id") or "").strip()[:36] or None
        p = products.get(pid) if pid else None
        name = re.sub(r"\s+", " ", str(ln.get("name") or "")).strip()[:300]
        if p:
            name = p["name"]
        elif pid:
            pid = None
        qty = _num(ln.get("qty"), 0, 1e9)
        if not name and qty is None:
            continue
        if not name:
            continue
        price = float(p["price"]) if p and p.get("price") is not None and not p.get("price_on_request") else None
        out.append({"product_id": pid, "name": name, "qty": qty,
                    "unit": re.sub(r"\s+", " ", str(ln.get("unit") or "")).strip()[:40],
                    "price": price,
                    "note": re.sub(r"\s+", " ", str(ln.get("note") or "")).strip()[:300]})
        if len(out) >= MAX_LINES:
            break
    return out


def estimate(lines: list):
    tot, any_price = 0.0, False
    for ln in lines:
        if ln.get("price") is not None and ln.get("qty") is not None:
            tot += ln["price"] * ln["qty"]
            any_price = True
    return round(tot, 2) if any_price else None


def products_for(tenant_id: int, ids=None) -> dict:
    conn, cur = _db()
    try:
        if ids is not None:
            ids = [i for i in ids if i]
            if not ids:
                return {}
            cur.execute("""SELECT id, name, price, currency, stock_quantity, reserved_quantity, category,
                                  is_service, price_on_request
                             FROM products WHERE tenant_id=%s AND id = ANY(%s)""", (tenant_id, ids))
        else:
            cur.execute("""SELECT id, name, price, currency, stock_quantity, reserved_quantity, category,
                                  is_service, price_on_request
                             FROM products WHERE tenant_id=%s AND is_active ORDER BY lower(name) LIMIT 5000""",
                        (tenant_id,))
        return {r["id"]: r for r in cur.fetchall()}
    finally:
        cur.close(); conn.close()


# ── reading ─────────────────────────────────────────────────────────────────

_LIST_COLS = """id, tenant_id, ref_no, lead_id, status, channel, buyer_company, buyer_name, buyer_email, buyer_phone,
                buyer_country, deliver_to, needed_by, notes, buyer_words, lines, estimate, currency, assigned_key,
                assigned_to, due_at, quote_value, quote_currency, quote_valid_until, quote_number, quote_sent_via,
                quote_sent_at, first_quote_at, quote_sent_by, quote_pdf_name, (quote_pdf IS NOT NULL) AS has_pdf, close_reason,
                closed_at, closed_by, created_by, created_at, updated_at, ai_answer"""


def ref(r) -> str:
    return f"R-{r['ref_no']}"


def list_requests(tenant_id: int, status: str = "to_quote", scope_key: str = None, q: str = "",
                  limit: int = 500) -> list:
    ensure_tables()
    where, params = ["tenant_id=%s"], [tenant_id]
    if status in STATUS_WORDS:
        where.append("status=%s"); params.append(status)
    if scope_key:
        where.append("assigned_key=%s"); params.append(scope_key)
    q = (q or "").strip()
    if q:
        like = f"%{q.lower()}%"
        m = re.fullmatch(r"(?i)r-?(\d{1,9})", q)
        where.append("""(lower(COALESCE(buyer_company,'')) LIKE %s OR lower(COALESCE(buyer_name,'')) LIKE %s
                          OR lower(COALESCE(buyer_email,'')) LIKE %s OR lower(COALESCE(quote_number,'')) LIKE %s
                          OR ref_no = %s)""")
        params += [like, like, like, like, int(m.group(1)) if m else -1]
    order = "due_at ASC NULLS LAST, id" if status == "to_quote" else "COALESCE(quote_sent_at, created_at) DESC, id DESC"
    conn, cur = _db()
    try:
        cur.execute(f"SELECT {_LIST_COLS} FROM quote_requests WHERE {' AND '.join(where)} ORDER BY {order} LIMIT %s",
                    params + [limit])
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def counts(tenant_id: int, scope_key: str = None) -> dict:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT status, COUNT(*) AS n FROM quote_requests WHERE tenant_id=%s
                          AND (%s::text IS NULL OR assigned_key=%s) GROUP BY status""", (tenant_id, scope_key, scope_key))
        c = {r["status"]: r["n"] for r in cur.fetchall()}
        return {k: c.get(k, 0) for k, _ in STATUSES}
    finally:
        cur.close(); conn.close()


def kpis(tenant_id: int, since: datetime, scope_key: str = None) -> dict:
    """This month's numbers for the top of the Requests page."""
    conn, cur = _db()
    try:
        cur.execute("""
            SELECT COUNT(*) AS requests,
                   COUNT(*) FILTER (WHERE first_quote_at IS NOT NULL) AS quoted,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM first_quote_at - created_at))
                       FILTER (WHERE first_quote_at IS NOT NULL) AS median_secs,
                   COUNT(*) FILTER (WHERE first_quote_at IS NOT NULL AND due_at IS NOT NULL AND first_quote_at <= due_at) AS on_time,
                   COUNT(*) FILTER (WHERE status='won') AS won
              FROM quote_requests WHERE tenant_id=%s AND created_at >= %s
               AND (%s::text IS NULL OR assigned_key=%s)""", (tenant_id, since, scope_key, scope_key))
        k = dict(cur.fetchone() or {})
        cur.execute("""SELECT channel, COUNT(*) AS n FROM quote_requests WHERE tenant_id=%s AND created_at >= %s
                          AND (%s::text IS NULL OR assigned_key=%s) GROUP BY channel ORDER BY n DESC""",
                    (tenant_id, since, scope_key, scope_key))
        k["by_channel"] = [(CHANNEL_WORDS.get(r["channel"], r["channel"]), r["n"]) for r in cur.fetchall()]
        # Quotes with buyers now (not period-bound), in the business's currency.
        cur.execute("""SELECT COUNT(*) AS n,
                              COALESCE(SUM(fx_to_selling(quote_value, quote_currency, tenant_id)), 0) AS v
                         FROM quote_requests WHERE tenant_id=%s AND status='quote_sent'
                          AND (%s::text IS NULL OR assigned_key=%s)""", (tenant_id, scope_key, scope_key))
        w = cur.fetchone()
        k["with_buyers_n"], k["with_buyers_v"] = w["n"], float(w["v"] or 0)
        return k
    finally:
        cur.close(); conn.close()


def sync_from_deals(tenant_id: int):
    """A deal marked Won or Lost on the Sales Pipeline (not from here) closes
    its request with a quotation out the same way."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""UPDATE quote_requests q SET status = CASE WHEN l.outcome='won' THEN 'won' ELSE 'lost' END,
                              close_reason = CASE WHEN l.outcome='won' THEN 'Won on the Sales Pipeline'
                                                  ELSE left('Lost: ' || COALESCE(l.dropped_reason, 'on the Sales Pipeline'), 300) END,
                              closed_at = NOW(), closed_by = 'Sales Pipeline', updated_at = NOW()
                         FROM merchant_pipeline_leads l
                        WHERE q.tenant_id=%s AND q.status='quote_sent' AND l.id=q.lead_id AND l.outcome IN ('won','lost')
                        RETURNING q.id, q.status""", (tenant_id,))
        for row in cur.fetchall():
            _event(cur, row["id"], "Marked " + ("Won" if row["status"] == "won" else "Lost") + " on the Sales Pipeline",
                   "Sales Pipeline")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️ quote_requests sync:", e)
    finally:
        cur.close(); conn.close()


def get(tenant_id: int, request_id: int, scope_key: str = None):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute(f"SELECT {_LIST_COLS} FROM quote_requests WHERE id=%s AND tenant_id=%s"
                    + (" AND assigned_key=%s" if scope_key else ""),
                    [request_id, tenant_id] + ([scope_key] if scope_key else []))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def events(request_id: int) -> list:
    conn, cur = _db()
    try:
        cur.execute("""SELECT what, by_label, created_at FROM quote_request_events WHERE request_id=%s
                        ORDER BY created_at DESC, id DESC LIMIT 100""", (request_id,))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def pdf(tenant_id: int, request_id: int, scope_key: str = None):
    conn, cur = _db()
    try:
        cur.execute("SELECT quote_pdf, quote_pdf_name FROM quote_requests WHERE id=%s AND tenant_id=%s"
                    + (" AND assigned_key=%s" if scope_key else ""),
                    [request_id, tenant_id] + ([scope_key] if scope_key else []))
        r = cur.fetchone()
        return (bytes(r["quote_pdf"]), r["quote_pdf_name"]) if r and r["quote_pdf"] is not None else (None, None)
    finally:
        cur.close(); conn.close()


def open_for_lead(tenant_id: int, lead_id: int) -> list:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT id, ref_no, status, quote_value, quote_currency, estimate, currency, created_at
                         FROM quote_requests WHERE tenant_id=%s AND lead_id=%s ORDER BY created_at DESC LIMIT 20""",
                    (tenant_id, lead_id))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


# ── writing ─────────────────────────────────────────────────────────────────

def _event_once(cur, request_id, what, by_label):
    """The same note twice in a row is written once (a chat is re-read at every pause)."""
    cur.execute("SELECT what FROM quote_request_events WHERE request_id=%s ORDER BY id DESC LIMIT 1", (request_id,))
    last = cur.fetchone()
    if not last or last["what"] != what[:2000]:
        _event(cur, request_id, what, by_label)


def _event(cur, request_id, what, by_label):
    cur.execute("INSERT INTO quote_request_events (request_id, what, by_label) VALUES (%s,%s,%s)",
                (request_id, what[:2000], (by_label or "")[:200]))


_tl = threading.local()


def _logs_moves(fn):
    """Deal moves made from Quotations count like Sales Pipeline moves: Team
    performance (quotes sent, won, lost) and the lead's follow-ups. Logged
    after the change is saved; a person's moves only (the AI has no session)."""
    import functools

    @functools.wraps(fn)
    def run(*a, **k):
        outer = getattr(_tl, "moves", None) is not None
        if not outer:
            _tl.moves = []
        try:
            res = fn(*a, **k)
        except Exception:
            if not outer:
                _tl.moves = None
            raise
        if not outer:
            moves, _tl.moves = _tl.moves, None
            ok = res[0] if isinstance(res, tuple) else bool(res)
            if ok and moves:
                try:
                    import staff_activity
                    for lead_id, frm, to, notes in moves:
                        staff_activity.log_stage_change(lead_id, frm, to, notes)
                except Exception as e:
                    print("⚠️ quote_requests activity log:", e)
        return res
    return run


def _history(cur, lead_id, from_stage, to_stage, by_label, notes):
    if getattr(_tl, "moves", None) is not None and from_stage != to_stage:
        _tl.moves.append((lead_id, from_stage, to_stage, notes))
    cur.execute("""INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                   VALUES (%s,%s,%s,%s,%s)""", (lead_id, from_stage, to_stage, (by_label or "")[:200], (notes or "")[:2000]))


def _lead(cur, tenant_id, lead_id):
    cur.execute("""SELECT id, tenant_id, customer_name, contact_person, email, phone, whatsapp_number, addr_country,
                          stage, outcome, dropped_at, is_opportunity, company_id, assigned_key, assigned_to,
                          deal_value, currency
                     FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s""", (lead_id, tenant_id))
    return cur.fetchone()


def _has_active(cur, lead_id) -> bool:
    cur.execute("SELECT 1 FROM quote_requests WHERE lead_id=%s AND status IN ('to_quote','quote_sent') LIMIT 1", (lead_id,))
    return cur.fetchone() is not None


def _ensure_lead(cur, tenant_id: int, d: dict, by_label: str, like: dict = None, force_new: bool = False) -> int:
    """The buyer's open lead (same email or number), else a new one. A lead
    that already has a request in progress keeps it: a second request is a
    separate deal (its own value, Won or Lost), joined to the same company."""
    import lead_qualify as _lq
    phones = _lq._norm_phones([d.get("buyer_phone")] if d.get("buyer_phone") else [], d.get("buyer_country"))
    lead = None
    if not force_new:
        m = _lq.find_buyer(cur, tenant_id, [d.get("buyer_email")] if d.get("buyer_email") else [],
                           phones or ([d["buyer_phone"]] if d.get("buyer_phone") else []), d.get("buyer_company"))
        lead = m.get("lead")
        if lead and lead.get("dropped_at") is None and not lead.get("outcome") and not _has_active(cur, lead["id"]):
            return lead["id"]
        like = lead or m.get("company_lead")
    name = d.get("buyer_company") or d.get("buyer_name") or d.get("buyer_email") or d.get("buyer_phone") or "Quote request"
    cur.execute("""INSERT INTO merchant_pipeline_leads
                     (tenant_id, customer_name, contact_person, email, phone, addr_country, stage, source,
                      company_id, product_interest, currency)
                   VALUES (%s,%s,%s,%s,%s,%s,'new_lead','quote_request',%s,%s,%s) RETURNING id""",
                (tenant_id, name[:300], d.get("buyer_name"), d.get("buyer_email"),
                 phones[0] if phones else d.get("buyer_phone"),
                 d.get("buyer_country"), (like or {}).get("company_id"),
                 ", ".join(ln["name"] for ln in d["lines"])[:500] or None, d.get("currency")))
    lead_id = cur.fetchone()["id"]
    _history(cur, lead_id, None, "new_lead", by_label, "Created from a quote request")
    return lead_id


def _make_opportunity(cur, lead: dict, by_label: str, why: str):
    """A buyer who gets a quotation is a real opportunity: on the Sales Pipeline."""
    if lead.get("is_opportunity"):
        return
    company_id = lead.get("company_id")
    name = (lead.get("customer_name") or "").strip()
    if not company_id and name and not name.startswith("+") and "@" not in name:
        cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (lead["tenant_id"], name.lower()))
        cur.execute("SELECT id FROM crm_companies WHERE tenant_id=%s AND lower(name)=lower(%s)", (lead["tenant_id"], name))
        row = cur.fetchone()
        if row:
            company_id = row["id"]
        else:
            cur.execute("INSERT INTO crm_companies (tenant_id, name, added_by_key) VALUES (%s,%s,%s) RETURNING id",
                        (lead["tenant_id"], name[:200], "quote"))
            company_id = cur.fetchone()["id"]
    cur.execute("""UPDATE merchant_pipeline_leads SET is_opportunity=TRUE, opportunity_at=NOW(), updated_at=NOW(),
                          company_id=COALESCE(company_id,%s) WHERE id=%s AND NOT is_opportunity""",
                (company_id, lead["id"]))
    if cur.rowcount:
        _history(cur, lead["id"], lead["stage"], "opportunity", by_label, why)
        lead["is_opportunity"] = True


@_logs_moves
def create(tenant_id: int, d: dict, actor: dict, s: dict, tz) -> dict:
    """d: buyer_company, buyer_name, buyer_email, buyer_phone, buyer_country,
    deliver_to, needed_by (date), notes, buyer_words, lines (clean), channel,
    lead_id (optional), assigned_key/assigned_to, currency."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT pg_advisory_xact_lock(80301031, %s)", (tenant_id,))
        lead_id = d.get("lead_id")
        if d.get("source_key") and not d.get("parallel"):
            # The AI: another read (e.g. an email and a WhatsApp at the same
            # moment) may have made this buyer's request a moment ago.
            cur.execute("""SELECT id FROM quote_requests WHERE tenant_id=%s AND status IN ('to_quote','quote_sent')
                             AND (source_key=%s OR lead_id=%s) ORDER BY id DESC LIMIT 1""",
                        (tenant_id, d["source_key"], lead_id))
            dup = cur.fetchone()
            if dup:
                conn.commit()
                return dict(get(tenant_id, dup["id"]), _existing=True)
        if lead_id:
            given = _lead(cur, tenant_id, lead_id)
            if not given or given.get("outcome") or given.get("dropped_at"):
                lead_id = None          # a closed deal: this is a new requirement
            elif _has_active(cur, lead_id):
                lead_id = _ensure_lead(cur, tenant_id, d, actor["label"], like=given, force_new=True)
        if not lead_id:
            lead_id = _ensure_lead(cur, tenant_id, d, actor["label"])
        lead = _lead(cur, tenant_id, lead_id)
        if not d.get("assigned_key") and lead and lead.get("assigned_key"):
            d["assigned_key"], d["assigned_to"] = lead["assigned_key"], lead["assigned_to"]
        if lead and not lead.get("assigned_key") and d.get("assigned_key"):
            cur.execute("""UPDATE merchant_pipeline_leads SET assigned_key=%s, assigned_to=%s
                           WHERE id=%s AND assigned_key IS NULL""", (d["assigned_key"], d["assigned_to"], lead_id))
        cur.execute("SELECT COALESCE(MAX(ref_no), 1000) + 1 AS n FROM quote_requests WHERE tenant_id=%s", (tenant_id,))
        ref_no = cur.fetchone()["n"]
        now = datetime.now(tz)
        cur.execute("""INSERT INTO quote_requests
                         (tenant_id, ref_no, lead_id, status, channel, buyer_company, buyer_name, buyer_email,
                          buyer_phone, buyer_country, deliver_to, needed_by, notes, buyer_words, lines, estimate,
                          currency, assigned_key, assigned_to, due_at, created_by, source_key)
                       VALUES (%s,%s,%s,'to_quote',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING id""",
                    (tenant_id, ref_no, lead_id, d.get("channel") or "manual", d.get("buyer_company"),
                     d.get("buyer_name"), d.get("buyer_email"), d.get("buyer_phone"), d.get("buyer_country"),
                     d.get("deliver_to"), d.get("needed_by"), d.get("notes"), d.get("buyer_words"),
                     psycopg2.extras.Json(d["lines"]), estimate(d["lines"]), d.get("currency"),
                     d.get("assigned_key"), d.get("assigned_to"), due_at(now, s, tz), actor["label"],
                     d.get("source_key")))
        rid = cur.fetchone()["id"]
        _event(cur, rid, f"Request created ({CHANNEL_WORDS.get(d.get('channel') or 'manual')})"
                         + (f", for {d['assigned_to']}" if d.get("assigned_to") else ""), actor["label"])
        _history(cur, lead_id, (lead or {}).get("stage"), (lead or {}).get("stage") or "new_lead", actor["label"],
                 f"Quote request R-{ref_no}: {len(d['lines'])} line(s)")
        cur.execute("UPDATE merchant_pipeline_leads SET updated_at=NOW() WHERE id=%s", (lead_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()
    r = get(tenant_id, rid)
    if s.get("alert_new") and r.get("assigned_key") != actor["key"]:
        _alert(tenant_id, r, "new")     # nobody assigned → the account owner
    return r


def update_details(tenant_id: int, request_id: int, d: dict, actor: dict) -> bool:
    conn, cur = _db()
    try:
        cur.execute("""UPDATE quote_requests SET buyer_company=%s, buyer_name=%s, buyer_email=%s, buyer_phone=%s,
                              buyer_country=%s, deliver_to=%s, needed_by=%s, notes=%s, lines=%s, estimate=%s,
                              assigned_key=%s, assigned_to=%s, edited=TRUE, updated_at=NOW()
                        WHERE id=%s AND tenant_id=%s""",
                    (d.get("buyer_company"), d.get("buyer_name"), d.get("buyer_email"), d.get("buyer_phone"),
                     d.get("buyer_country"), d.get("deliver_to"), d.get("needed_by"), d.get("notes"),
                     psycopg2.extras.Json(d["lines"]), estimate(d["lines"]), d.get("assigned_key"),
                     d.get("assigned_to"), request_id, tenant_id))
        ok = cur.rowcount == 1
        if ok:
            _event(cur, request_id, "Request details changed", actor["label"])
        conn.commit()
        return ok
    finally:
        cur.close(); conn.close()


@_logs_moves
def mark_sent(tenant_id: int, request_id: int, f: dict, actor: dict) -> tuple:
    """f: value, currency, valid_until (date|None), number, via, pdf (bytes|None), pdf_name."""
    conn, cur = _db()
    try:
        cur.execute(f"SELECT {_LIST_COLS} FROM quote_requests WHERE id=%s AND tenant_id=%s FOR UPDATE",
                    (request_id, tenant_id))
        r = cur.fetchone()
        if not r:
            return False, "Request not found."
        again = r["status"] != "to_quote"
        if r["status"] == "won":
            return False, "This request is already Won."
        sets = ["status='quote_sent'", "quote_value=%s", "quote_currency=%s", "quote_valid_until=%s",
                "quote_number=%s", "quote_sent_via=%s", "quote_sent_at=NOW()", "quote_sent_by=%s",
                "first_quote_at=COALESCE(first_quote_at, NOW())",
                "close_reason=NULL", "closed_at=NULL", "closed_by=NULL", "updated_at=NOW()"]
        vals = [f["value"], f["currency"], f.get("valid_until"), f.get("number"), f.get("via"), actor["label"]]
        if f.get("pdf"):
            sets += ["quote_pdf=%s", "quote_pdf_name=%s"]
            vals += [psycopg2.Binary(f["pdf"]), f.get("pdf_name") or "quotation.pdf"]
        cur.execute(f"UPDATE quote_requests SET {', '.join(sets)} WHERE id=%s", vals + [request_id])
        sym = f["currency"]
        what = (f"{'New quote sent' if again else 'Quote sent'}: {money(f['value'], sym)}"
                + (f", {f['number']}" if f.get("number") else "")
                + (f", valid until {f['valid_until'].strftime('%d %b %Y')}" if f.get("valid_until") else "")
                + (f", by {SENT_BY_WORDS.get(f.get('via'), '')}" if f.get("via") else ""))
        _event(cur, request_id, what, actor["label"])
        lead = _lead(cur, tenant_id, r["lead_id"]) if r["lead_id"] else None
        if lead:
            lead = dict(lead)
            if lead.get("outcome") in ("lost", "dropped", "not_a_fit"):
                cur.execute("""UPDATE merchant_pipeline_leads SET dropped_at=NULL, dropped_reason=NULL, outcome=NULL
                               WHERE id=%s""", (lead["id"],))
                _history(cur, lead["id"], lead["outcome"], lead["stage"], actor["label"],
                         f"Reopened: a quotation was sent (R-{r['ref_no']})")
                lead["outcome"] = None
            if not lead.get("outcome"):
                _make_opportunity(cur, lead, actor["label"], f"Quotation sent (R-{r['ref_no']})")
                notes = f"Quotation {f.get('number') or ''} for request R-{r['ref_no']}".replace("  ", " ")
                if f.get("valid_until"):
                    notes += f", valid until {f['valid_until'].strftime('%d %b %Y')}"
                # A new quote never moves a deal backwards (Negotiating stays Negotiating).
                later = lead["stage"] in STAGE_AFTER_PROPOSAL
                new_stage = lead["stage"] if later else "proposal_sent"
                cur.execute("""UPDATE merchant_pipeline_leads SET stage=%s, proposal_date=COALESCE(proposal_date, CURRENT_DATE),
                                      deal_value=%s, currency=%s, proposal_notes=%s, updated_at=NOW() WHERE id=%s""",
                            (new_stage, f["value"], f["currency"], notes[:1000], lead["id"]))
                if lead["stage"] != new_stage:
                    _history(cur, lead["id"], lead["stage"], new_stage, actor["label"], notes)
                elif again:
                    _history(cur, lead["id"], lead["stage"], new_stage, actor["label"], "New " + notes[0].lower() + notes[1:])
        conn.commit()
        return True, (f"{'New quote' if again else 'Quote'} for R-{r['ref_no']} recorded. "
                      "The deal is on the Sales Pipeline at Proposal sent.")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


@_logs_moves
def mark_won(tenant_id: int, request_id: int, value, actor: dict, note: str = "") -> tuple:
    conn, cur = _db()
    try:
        cur.execute("SELECT id, ref_no, status, lead_id, quote_value, quote_currency, currency FROM quote_requests "
                    "WHERE id=%s AND tenant_id=%s FOR UPDATE", (request_id, tenant_id))
        r = cur.fetchone()
        if not r:
            return False, "Request not found.", None
        if r["status"] == "won":
            return False, "Already Won.", None
        if r["status"] != "quote_sent":
            return False, "Click Quote sent first: a request is won once the buyer accepts a quotation.", None
        val = value if value is not None else r["quote_value"]
        cur.execute("""UPDATE quote_requests SET status='won', quote_value=%s, closed_at=NOW(), closed_by=%s,
                              close_reason=%s, updated_at=NOW() WHERE id=%s""",
                    (val, actor["label"], (note or "")[:300] or None, request_id))
        _event(cur, request_id, f"Won: {money(val, r['quote_currency'])}" + (f". {note}" if note else ""), actor["label"])
        lead_id = None
        lead = _lead(cur, tenant_id, r["lead_id"]) if r["lead_id"] else None
        if lead and not lead.get("outcome"):
            cur.execute("""UPDATE merchant_pipeline_leads SET stage='won', outcome='won', won_date=CURRENT_DATE,
                                  deal_value=%s, currency=COALESCE(%s, currency), updated_at=NOW() WHERE id=%s""",
                        (val, r["quote_currency"], lead["id"]))
            _history(cur, lead["id"], lead["stage"], "won", actor["label"], f"Quotation accepted (R-{r['ref_no']})")
            lead_id = lead["id"]
            try:
                import lead_followups
                lead_followups.on_close(cur, lead["id"])
            except Exception as e:
                print("⚠️ quote_requests won follow-up close:", e)
        conn.commit()
        return True, f"R-{r['ref_no']} Won.", lead_id
    finally:
        cur.close(); conn.close()


@_logs_moves
def close(tenant_id: int, request_id: int, reason: str, actor: dict) -> tuple:
    """Lost (after a quote: the deal is Lost too) or Not quoted (before one:
    the deal is left as it is, the salesperson decides about the lead)."""
    conn, cur = _db()
    try:
        cur.execute("SELECT id, ref_no, status, lead_id FROM quote_requests WHERE id=%s AND tenant_id=%s FOR UPDATE",
                    (request_id, tenant_id))
        r = cur.fetchone()
        if not r:
            return False, "Request not found."
        if r["status"] in ("won", "lost"):
            return False, f"Already {STATUS_WORDS[r['status']]}."
        quoted = r["status"] == "quote_sent"
        word = "Lost" if quoted else "Not quoted"
        cur.execute("""UPDATE quote_requests SET status='lost', close_reason=%s, closed_at=NOW(), closed_by=%s,
                              updated_at=NOW() WHERE id=%s""", (f"{word}: {reason}"[:300], actor["label"], request_id))
        _event(cur, request_id, f"{word}: {reason}", actor["label"])
        if quoted and r["lead_id"]:
            lead = _lead(cur, tenant_id, r["lead_id"])
            if lead and not lead.get("outcome") and lead.get("is_opportunity"):
                cur.execute("""UPDATE merchant_pipeline_leads SET dropped_at=NOW(), dropped_reason=%s, outcome='lost',
                                      updated_at=NOW() WHERE id=%s""", (reason[:300], lead["id"]))
                _history(cur, lead["id"], lead["stage"], "lost", actor["label"], f"{reason} (R-{r['ref_no']})")
                try:
                    import lead_followups
                    lead_followups.on_close(cur, lead["id"])
                except Exception as e:
                    print("⚠️ quote_requests follow-up close:", e)
        conn.commit()
        return True, f"R-{r['ref_no']} closed: {word}."
    finally:
        cur.close(); conn.close()


@_logs_moves
def reopen(tenant_id: int, request_id: int, actor: dict, tz, s: dict) -> tuple:
    """Lost / Not quoted → back where it was. A Lost deal set by this
    request goes back on the Sales Pipeline too."""
    conn, cur = _db()
    try:
        cur.execute("""SELECT id, ref_no, status, lead_id, quote_sent_at, close_reason FROM quote_requests
                        WHERE id=%s AND tenant_id=%s FOR UPDATE""", (request_id, tenant_id))
        r = cur.fetchone()
        if not r or r["status"] != "lost":
            return False, "Only a Lost or Not quoted request can be reopened."
        back = "quote_sent" if r["quote_sent_at"] else "to_quote"
        cur.execute("""UPDATE quote_requests SET status=%s, close_reason=NULL, closed_at=NULL, closed_by=NULL,
                              due_at=CASE WHEN %s='to_quote' THEN %s ELSE due_at END, updated_at=NOW() WHERE id=%s""",
                    (back, back, due_at(datetime.now(tz), s, tz), request_id))
        _event(cur, request_id, f"Reopened (was {r['close_reason'] or 'closed'})", actor["label"])
        if back == "quote_sent" and r["lead_id"]:
            lead = _lead(cur, tenant_id, r["lead_id"])
            if lead and lead.get("outcome") == "lost":
                cur.execute("""UPDATE merchant_pipeline_leads SET dropped_at=NULL, dropped_reason=NULL, outcome=NULL,
                                      updated_at=NOW() WHERE id=%s""", (lead["id"],))
                _history(cur, lead["id"], "lost", lead["stage"], actor["label"], f"Reopened with request R-{r['ref_no']}")
        conn.commit()
        return True, f"R-{r['ref_no']} reopened: {STATUS_WORDS[back]}."
    finally:
        cur.close(); conn.close()


# ── money, files ────────────────────────────────────────────────────────────

def money(v, code=None) -> str:
    """'£6,430.00' — quotes keep pennies (staff screens elsewhere round)."""
    if v is None:
        return "—"
    try:
        import selling_currency
        prefix = selling_currency.currency_label((code or "GBP").upper())["prefix"]
    except Exception:
        prefix = (code or "") + " "
    return f"{prefix}{float(v):,.2f}"


def lines_text(r: dict) -> str:
    """Copy lines: one line per product, tab between columns, so it pastes
    into a spreadsheet or reads cleanly in an email."""
    rows = ["Product\tQuantity\tUnit\tList price\tNote"]
    for ln in r.get("lines") or []:
        q = ln.get("qty")
        rows.append("\t".join([ln.get("name") or "", ("%g" % q) if q is not None else "", ln.get("unit") or "",
                               ("%.2f" % ln["price"]) if ln.get("price") is not None else "", ln.get("note") or ""]))
    return "\n".join(rows)


def lines_xlsx(r: dict, business: str = "") -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"R-{r['ref_no']}"[:31]
    bold = Font(bold=True)
    info = [("Request", f"R-{r['ref_no']}"), ("Business", business), ("Buyer", r.get("buyer_company") or ""),
            ("Contact", r.get("buyer_name") or ""), ("Email", r.get("buyer_email") or ""),
            ("Phone", r.get("buyer_phone") or ""), ("Deliver to", r.get("deliver_to") or ""),
            ("Needed by", r["needed_by"].strftime("%d %b %Y") if r.get("needed_by") else ""),
            ("Notes", r.get("notes") or "")]
    def text(row, col, v):
        # Typed / buyer text is never run as a spreadsheet formula.
        c = ws.cell(row=row, column=col, value=v)
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
            c.data_type = "s"
        return c
    for i, (k, v) in enumerate(info, start=1):
        ws.cell(row=i, column=1, value=k).font = bold
        text(i, 2, v)
    start = len(info) + 2
    head = ["Product", "Quantity", "Unit", f"List price ({r.get('currency') or ''})".replace(" ()", ""),
            "Line total", "Note"]
    fill = PatternFill("solid", fgColor="E8EEF8")
    for c, h in enumerate(head, start=1):
        cell = ws.cell(row=start, column=c, value=h)
        cell.font, cell.fill = bold, fill
    for i, ln in enumerate(r.get("lines") or [], start=start + 1):
        text(i, 1, ln.get("name"))
        ws.cell(row=i, column=2, value=ln.get("qty"))
        text(i, 3, ln.get("unit") or None)
        ws.cell(row=i, column=4, value=ln.get("price"))
        if ln.get("price") is not None and ln.get("qty") is not None:
            ws.cell(row=i, column=5, value=f"=B{i}*D{i}")
        text(i, 6, ln.get("note") or None)
        for c in (4, 5):
            ws.cell(row=i, column=c).number_format = "#,##0.00"
    for col, w in zip("ABCDEF", (44, 11, 12, 16, 14, 40)):
        ws.column_dimensions[col].width = w
    ws.cell(row=1, column=2).alignment = Alignment(horizontal="left")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ── requests the AI makes (Phase 2) ─────────────────────────────────────────

AI_ACTOR = {"key": "ai", "label": "AI"}
AI_CHANNEL = {"email": "email", "reply": "email", "whatsapp": "whatsapp", "web": "chat"}
QUOTE_INTENTS = ("full_rfq", "price_request")
_STOP = {"the", "a", "an", "of", "for", "and", "with", "in", "x", "pcs", "pc", "piece", "pieces", "box", "boxes",
         "case", "cases", "pack", "packs", "unit", "units", "please", "price"}


def _tokens(name: str) -> set:
    """Words that identify a product: "1,400ml" = "1400ml", "cups" = "cup"."""
    text = re.sub(r"(\d),(\d)", r"\1\2", (name or "").lower())
    out = set()
    for t in re.findall(r"[a-z0-9]+", text):
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss") and not t[-2].isdigit():
            t = t[:-1]
        if t not in _STOP:
            out.add(t)
    return out


def match_product(name: str, products: dict):
    """The product on the business's list this line is, or None. Exact name
    first; otherwise clearly the closest by its words (numbers like 12oz
    must agree, so 8oz never becomes 12oz)."""
    low = (name or "").strip().lower()
    for p in products.values():
        if p["name"].strip().lower() == low:
            return p
    want = _tokens(name)
    if not want:
        return None
    scored = []
    for p in products.values():
        have = _tokens(p["name"])
        if not have:
            continue
        nums_w = {t for t in want if any(ch.isdigit() for ch in t)}
        nums_h = {t for t in have if any(ch.isdigit() for ch in t)}
        if nums_w and nums_h and not (nums_w & nums_h):
            continue
        sc = len(want & have) / len(want | have)
        if want <= have:
            sc = max(sc, 0.6)
        scored.append((sc, p))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.5:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.15:
        return None          # two products just as likely: let the salesperson pick
    return scored[0][1]


def lines_from_items(items: list, products: dict) -> list:
    out = []
    for it in items or []:
        p = match_product(it.get("product"), products)
        out.append({"product_id": p["id"] if p else None, "name": p["name"] if p else it.get("product"),
                    "qty": it.get("qty"), "unit": (it.get("unit") or "")[:40],
                    "price": (float(p["price"]) if p and p.get("price") is not None and not p.get("price_on_request") else None),
                    "note": (it.get("note") or "")[:300]})
        if len(out) >= MAX_LINES:
            break
    return out


def _parse_day(v):
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date() if v else None
    except ValueError:
        return None


def from_ai(tenant_id: int, lead_id: int, channel: str, r: dict, source_key: str, buyer_words: str = ""):
    """After the AI read an enquiry: a request for the products the buyer
    asked prices for. Returns {"id", "ref", "action"} or None."""
    if r.get("intent") not in QUOTE_INTENTS or not r.get("items") or not allowed(tenant_id):
        return None
    ensure_tables()
    products = products_for(tenant_id)
    if not products:
        return None     # no product list (e.g. PhiXtra's own plans): the AI takes no quote requests
    lines = lines_from_items(r["items"], products)
    if not lines:
        return None
    deliver_to = r.get("deliver_to") or r.get("delivery_country")
    needed_by = _parse_day(r.get("needed_by")) or _parse_day(r.get("deadline_date"))
    words = (buyer_words or "").strip()[:8000] or None
    conn, cur = _db()
    try:
        cur.execute("SELECT pg_advisory_xact_lock(80301031, %s)", (tenant_id,))
        # 1. The same chat read again: the request it already made follows the conversation.
        cur.execute(f"""SELECT {_LIST_COLS}, edited FROM quote_requests WHERE tenant_id=%s AND source_key=%s
                         ORDER BY id DESC LIMIT 1""", (tenant_id, source_key))
        same = cur.fetchone()
        if same and not (source_key or "").startswith("chat:"):
            # An email / reply is read once; reading it again changes nothing.
            conn.commit()
            return {"id": same["id"], "ref": f"R-{same['ref_no']}", "action": "same"}
        if (source_key or "").startswith("chat:") and not (same and same["status"] in ACTIVE):
            # A WhatsApp chat is one conversation per number, forever: a new
            # order comes in the same chat. Lines of this buyer's recently
            # closed requests (from any channel) that the AI still sees in the
            # conversation are left out, so a Won order is never asked again.
            # The same buyer may be on more than one lead (a closed deal, then a new one): by email / number.
            cur.execute("""SELECT x.id FROM merchant_pipeline_leads l JOIN merchant_pipeline_leads x ON x.tenant_id = l.tenant_id
                            WHERE l.id=%s AND l.tenant_id=%s AND (x.id = l.id
                               OR (COALESCE(l.email,'') <> '' AND lower(x.email) = lower(l.email))
                               OR (length(regexp_replace(COALESCE(l.whatsapp_number, l.phone, ''), '\\D', '', 'g')) >= 7 AND
                                   right(regexp_replace(COALESCE(l.whatsapp_number, l.phone, ''), '\\D', '', 'g'), 9) IN
                                   (right(regexp_replace(COALESCE(x.whatsapp_number,''), '\\D', '', 'g'), 9),
                                    right(regexp_replace(COALESCE(x.phone,''), '\\D', '', 'g'), 9))))
                            LIMIT 50""", (lead_id, tenant_id))
            same_buyer = [row["id"] for row in cur.fetchall()] or [lead_id]
            cur.execute("""SELECT id, ref_no, lines FROM quote_requests WHERE tenant_id=%s AND status IN ('won','lost')
                             AND (lead_id = ANY(%s) OR source_key=%s) AND COALESCE(closed_at, updated_at) > NOW() - INTERVAL '30 days'
                            ORDER BY id DESC LIMIT 5""", (tenant_id, same_buyer, source_key))
            closed = cur.fetchall()
            old = {(ln.get("product_id") or (ln.get("name") or "").lower(), ln.get("qty"))
                   for c in closed for ln in c["lines"]}
            lines = [ln for ln in lines if (ln.get("product_id") or ln["name"].lower(), ln.get("qty")) not in old]
            if not lines:
                conn.commit()
                c0 = closed[0] if closed else same
                return {"id": c0["id"], "ref": f"R-{c0['ref_no']}", "action": "same"} if c0 else None
            if same and same["status"] in ("won", "lost"):
                same = None
        if same and same["status"] == "to_quote":
            if same["edited"]:
                _event_once(cur, same["id"], "The buyer wrote more in the chat; not changed, because someone edited "
                                             "this request. Check the chat.", AI_ACTOR["label"])
                conn.commit()
                return {"id": same["id"], "ref": f"R-{same['ref_no']}", "action": "kept"}
            if same["lines"] == lines and (same["deliver_to"] or None) == deliver_to and same["needed_by"] == needed_by:
                conn.commit()
                return {"id": same["id"], "ref": f"R-{same['ref_no']}", "action": "same"}
            cur.execute("""UPDATE quote_requests SET lines=%s, estimate=%s, deliver_to=COALESCE(%s, deliver_to),
                                  needed_by=COALESCE(%s, needed_by), buyer_words=COALESCE(%s, buyer_words), updated_at=NOW()
                            WHERE id=%s""", (psycopg2.extras.Json(lines), estimate(lines), deliver_to, needed_by,
                                             words, same["id"]))
            _event_once(cur, same["id"], f"Updated from the chat: {len(lines)} line(s)", AI_ACTOR["label"])
            conn.commit()
            return {"id": same["id"], "ref": f"R-{same['ref_no']}", "action": "updated"}
        # 2. The buyer already has a request in progress.
        cur.execute(f"""SELECT {_LIST_COLS}, edited FROM quote_requests WHERE tenant_id=%s AND lead_id=%s
                          AND status IN ('to_quote','quote_sent') ORDER BY id DESC LIMIT 1""", (tenant_id, lead_id))
        act_r = cur.fetchone()
        if act_r and not r.get("new_requirement"):
            if act_r["status"] == "to_quote":
                have = {(ln.get("product_id") or ln["name"].lower()) for ln in act_r["lines"]}
                add = [ln for ln in lines if (ln.get("product_id") or ln["name"].lower()) not in have]
                if add:
                    merged = (act_r["lines"] + add)[:MAX_LINES]
                    cur.execute("""UPDATE quote_requests SET lines=%s, estimate=%s, deliver_to=COALESCE(deliver_to, %s),
                                          needed_by=COALESCE(needed_by, %s), updated_at=NOW() WHERE id=%s""",
                                (psycopg2.extras.Json(merged), estimate(merged), deliver_to, needed_by, act_r["id"]))
                    _event(cur, act_r["id"], "The buyer asked for more by " + CHANNEL_WORDS.get(AI_CHANNEL.get(channel), channel).lower()
                           + ": " + ", ".join(ln["name"] for ln in add)[:500], AI_ACTOR["label"])
                else:
                    _event_once(cur, act_r["id"], "The buyer wrote again about this request by "
                           + CHANNEL_WORDS.get(AI_CHANNEL.get(channel), channel).lower(), AI_ACTOR["label"])
                conn.commit()
                return {"id": act_r["id"], "ref": f"R-{act_r['ref_no']}", "action": "added" if add else "noted"}
            _event_once(cur, act_r["id"], "The buyer wrote again about prices after the quote was sent ("
                   + CHANNEL_WORDS.get(AI_CHANNEL.get(channel), channel).lower() + "). Check the conversation.",
                   AI_ACTOR["label"])
            conn.commit()
            return {"id": act_r["id"], "ref": f"R-{act_r['ref_no']}", "action": "noted"}
        cur.execute("""SELECT customer_name, contact_person, email, phone, whatsapp_number, addr_country,
                              assigned_key, assigned_to FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s""",
                    (lead_id, tenant_id))
        L = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not L:
        return None
    company = L["customer_name"] if L["customer_name"] and not L["customer_name"].startswith("+") and "@" not in L["customer_name"] else None
    phone = ("+" + L["whatsapp_number"].lstrip("+")) if L.get("whatsapp_number") else L.get("phone")
    try:
        import selling_currency
        cur_code = selling_currency.tenant_currency(tenant_id)
    except Exception:
        cur_code = None
    from team_performance import tz_for
    d = {"lead_id": lead_id, "buyer_company": company or r.get("company"), "buyer_name": L["contact_person"] or r.get("contact_person"),
         "buyer_email": L["email"], "buyer_phone": phone, "buyer_country": L["addr_country"],
         "deliver_to": deliver_to, "needed_by": needed_by, "notes": None, "buyer_words": words, "lines": lines,
         "channel": AI_CHANNEL.get(channel, "email"), "assigned_key": L["assigned_key"], "assigned_to": L["assigned_to"],
         "currency": cur_code, "source_key": source_key, "parallel": bool(r.get("new_requirement"))}
    req = create(tenant_id, d, AI_ACTOR, settings(tenant_id), tz_for(tenant_id))
    if req.get("_existing"):
        return {"id": req["id"], "ref": f"R-{req['ref_no']}", "action": "same"}
    print(f"   [QUOTE] tenant={tenant_id} lead={lead_id} → R-{req['ref_no']} ({len(lines)} lines, {channel})")
    return {"id": req["id"], "ref": f"R-{req['ref_no']}", "action": "created"}


# ── after the quote (Phase 3) ───────────────────────────────────────────────

ANSWER_WORDS = {"accept": "accepted the quotation", "change": "asked for a change",
                "decline": "declined the quotation", "question": "asked about the quotation"}
SURE = 0.7      # below this the AI asks a person (Check these) instead of deciding


def context_for_lead(tenant_id: int, lead_id: int):
    """What the AI is told about this buyer's quotation, or None."""
    if not lead_id or not allowed(tenant_id):
        return None
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT ref_no, quote_number, quote_value, quote_currency, quote_valid_until, quote_sent_at, lines
                         FROM quote_requests WHERE tenant_id=%s AND lead_id=%s AND status='quote_sent'
                        ORDER BY quote_sent_at DESC LIMIT 1""", (tenant_id, lead_id))
        q = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not q:
        return None
    items = ", ".join(f"{('%g ' % ln['qty']) if ln.get('qty') is not None else ''}{ln.get('name')}" for ln in q["lines"][:12])
    return (f"quotation {q['quote_number'] or 'R-%s' % q['ref_no']} for {money(q['quote_value'], q['quote_currency'])}"
            f", sent {q['quote_sent_at'].strftime('%d %b %Y')}"
            + (f", valid until {q['quote_valid_until'].strftime('%d %b %Y')}" if q.get("quote_valid_until") else "")
            + f" ({items})")


def _active_for_lead(cur, tenant_id, lead_id, status="quote_sent"):
    cur.execute(f"""SELECT {_LIST_COLS} FROM quote_requests WHERE tenant_id=%s AND lead_id=%s AND status=%s
                     ORDER BY id DESC LIMIT 1""", (tenant_id, lead_id, status))
    return cur.fetchone()


def on_answer(tenant_id: int, lead_id: int, channel: str, r: dict, source_key: str = ""):
    """The buyer answered their quotation. Accept → Won, decline → Lost,
    change → back to To quote, question → the salesperson is told. Not
    sure → Check these. Returns {"id", "ref", "action"} or None."""
    ans = r.get("quote_answer")
    if ans not in ANSWER_WORDS or not lead_id or not allowed(tenant_id):
        return None
    conn, cur = _db()
    try:
        q = _active_for_lead(cur, tenant_id, lead_id)
        if not q:
            return None
        lead = _lead(cur, tenant_id, lead_id)
    finally:
        cur.close(); conn.close()
    detail = (r.get("quote_detail") or r.get("reason") or "").strip()[:300]
    by = ANSWER_WORDS[ans]
    via = CHANNEL_WORDS.get(AI_CHANNEL.get(channel), channel).lower()
    conf = r.get("confidence")
    out = {"id": q["id"], "ref": f"R-{q['ref_no']}"}
    if ans in ("accept", "decline") and (conf is None or conf < SURE):
        # Never Won or Lost on a guess.
        conn, cur = _db()
        try:
            cur.execute("""INSERT INTO lead_check_items (tenant_id, lead_id, kind, reason)
                           SELECT %s,%s,'unsure',%s WHERE NOT EXISTS (SELECT 1 FROM lead_check_items
                                  WHERE lead_id=%s AND kind='unsure' AND status='open' AND reason=%s)""",
                        (tenant_id, lead_id, f"Quote R-{q['ref_no']}: the buyer may have {by} ({via}). {detail}"[:1000],
                         lead_id, f"Quote R-{q['ref_no']}: the buyer may have {by} ({via}). {detail}"[:1000]))
            _event_once(cur, q["id"], f"The buyer may have {by} ({via}): {detail}. The AI wasn't sure, so it's in "
                                      "Check these for a person to decide.", AI_ACTOR["label"])
            conn.commit()
        finally:
            cur.close(); conn.close()
        _answer_alert(tenant_id, q, "unsure", detail, via, source_key)
        return {**out, "action": "unsure"}
    before = {"status": q["status"], "lead_stage": (lead or {}).get("stage"), "lead_outcome": (lead or {}).get("outcome"),
              "at": datetime.utcnow().isoformat(timespec="seconds"), "answer": ans, "detail": detail, "via": via,
              "po": r.get("quote_po")}
    if ans == "accept":
        note = (f"Their order number {r['quote_po']}" if r.get("quote_po") else "") or detail
        ok, _msg, won_lead = mark_won(tenant_id, q["id"], None, AI_ACTOR, note[:300])
        if not ok:
            return None
        if won_lead:
            try:
                from portal_routes import _on_lead_won, _stamp_campaign_converted
                _stamp_campaign_converted(won_lead)
                _on_lead_won(tenant_id, won_lead)
            except Exception as e:
                print("⚠️ quote_requests won hooks:", e)
    elif ans == "decline":
        ok, _msg = close(tenant_id, q["id"], ("Declined: " + detail) if detail else "Declined by the buyer", AI_ACTOR)
        if not ok:
            return None
    conn, cur = _db()
    try:
        if ans == "change":
            from team_performance import tz_for
            tz = tz_for(tenant_id)
            line = f"Change asked {datetime.now(tz).strftime('%d %b')} ({via}): {detail or 'see the conversation'}"
            cur.execute("""UPDATE quote_requests SET status='to_quote', due_at=%s, updated_at=NOW(),
                                  notes=CASE WHEN notes IS NULL OR notes='' THEN %s ELSE notes || E'\n' || %s END
                            WHERE id=%s AND status='quote_sent'""",
                        (due_at(datetime.now(tz), settings(tenant_id), tz), line, line, q["id"]))
            if not cur.rowcount:
                conn.rollback()
                return None
            _event(cur, q["id"], f"The buyer asked for a change ({via}): {detail}. Back to To quote.", AI_ACTOR["label"])
        elif ans == "question":
            _event_once(cur, q["id"], f"The buyer asked about the quotation ({via}): {detail}", AI_ACTOR["label"])
        else:
            _event(cur, q["id"], f"Read by the AI: the buyer {by} ({via})" + (f": {detail}" if detail else "")
                   + ". Undo on this page if that's wrong.", AI_ACTOR["label"])
        if ans != "question":
            cur.execute("UPDATE quote_requests SET ai_answer=%s WHERE id=%s", (psycopg2.extras.Json(before), q["id"]))
        conn.commit()
    finally:
        cur.close(); conn.close()
    _answer_alert(tenant_id, q, ans, detail, via, source_key, po=r.get("quote_po"))
    return {**out, "action": ans}


def _answer_alert(tenant_id, q, ans, detail, via, source_key, po=None):
    import auto_followups as _af
    import hashlib
    buyer = q.get("buyer_company") or q.get("buyer_name") or "The buyer"
    val = money(q.get("quote_value"), q.get("quote_currency"))
    num = q.get("quote_number") or f"R-{q['ref_no']}"
    subject, lines = {
        "accept": (f"Won: {buyer} accepted {num}",
                   [f"{buyer} accepted quotation {num} ({val}) by {via}." + (f" Their order number: {po}." if po else ""),
                    "The deal is marked Won. Raise the order in your own software as usual.",
                    "If the AI got this wrong, open the request and press Undo."]),
        "decline": (f"Lost: {buyer} declined {num}",
                    [f"{buyer} declined quotation {num} ({val}) by {via}: {detail or 'no reason given'}.",
                     "The deal is marked Lost and its follow-ups have stopped.",
                     "If the AI got this wrong, open the request and press Undo."]),
        "change": (f"{buyer} wants a change to {num}",
                   [f"{buyer} asked for a change to quotation {num} by {via}: {detail or 'see the conversation'}.",
                    "The request is back in To quote. Send a new quotation, then click Quote sent."]),
        "question": (f"{buyer} asked about {num}",
                     [f"{buyer} asked about quotation {num} by {via}: {detail or 'see the conversation'}.",
                      "The AI doesn't discuss prices or terms: please reply to them."]),
        "unsure": (f"Check this: {buyer} replied about {num}",
                   [f"{buyer} replied about quotation {num} by {via}: {detail or 'see the conversation'}.",
                    "The AI wasn't sure whether they accepted or declined, so nothing was changed.",
                    "It's in Leads › Check these."]),
    }[ans]
    key = hashlib.sha1(f"{source_key}|{ans}|{detail}".encode()).hexdigest()[:12]
    conn, cur = _db()
    try:
        _af.notify(cur, tenant_id, q.get("assigned_key") or "owner", f"quote_ans:{q['id']}:{key}", subject, lines,
                   lead_id=q.get("lead_id"), link=f"{_af.PORTAL}/quotations/requests/{q['id']}")
        conn.commit()
    except Exception as e:
        print(f"⚠️ quote_requests answer alert {q['id']}:", e)
    finally:
        cur.close(); conn.close()


@_logs_moves
def undo_ai(tenant_id: int, request_id: int, actor: dict) -> tuple:
    """Put the request (and its deal) back as they were before the AI read the answer."""
    conn, cur = _db()
    try:
        cur.execute(f"SELECT {_LIST_COLS} FROM quote_requests WHERE id=%s AND tenant_id=%s FOR UPDATE",
                    (request_id, tenant_id))
        q = cur.fetchone()
        a = (q or {}).get("ai_answer") or {}
        if not q or not a or a.get("undone"):
            return False, "Nothing to undo."
        if a["answer"] == "accept" and q["status"] != "won" or a["answer"] == "decline" and q["status"] != "lost" \
                or a["answer"] == "change" and q["status"] != "to_quote":
            return False, "Someone has changed this request since, so there's nothing to undo."
        cur.execute("""UPDATE quote_requests SET status='quote_sent', closed_at=NULL, closed_by=NULL, close_reason=NULL,
                              ai_answer = ai_answer || '{"undone": true}'::jsonb, updated_at=NOW() WHERE id=%s""",
                    (request_id,))
        if q.get("lead_id") and a["answer"] in ("accept", "decline"):
            lead = _lead(cur, tenant_id, q["lead_id"])
            if lead and lead.get("outcome") in ("won", "lost"):
                cur.execute("""UPDATE merchant_pipeline_leads SET outcome=%s, stage=%s, won_date=NULL, dropped_at=NULL,
                                      dropped_reason=NULL, updated_at=NOW() WHERE id=%s""",
                            (a.get("lead_outcome"), a.get("lead_stage") or "proposal_sent", lead["id"]))
                note = f"Undo: the AI had marked this {lead['outcome'].capitalize()} (R-{q['ref_no']})"
                if lead["outcome"] == "lost":
                    _history(cur, lead["id"], "lost", a.get("lead_stage") or "proposal_sent", actor["label"], note)
                else:
                    # Undoing a Won is not a new quote: history only, no Team performance count.
                    cur.execute("""INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                                   VALUES (%s,'won',%s,%s,%s)""", (lead["id"], a.get("lead_stage") or "proposal_sent",
                                                                  actor["label"][:200], note))
        _event(cur, request_id, f"Undo: the AI's reading ({ANSWER_WORDS[a['answer']]}) was wrong. Back to Quote sent.",
               actor["label"])
        conn.commit()
        return True, f"Undone: R-{q['ref_no']} is back to Quote sent."
    finally:
        cur.close(); conn.close()


def extend(tenant_id: int, request_id: int, new_date, actor: dict) -> tuple:
    conn, cur = _db()
    try:
        cur.execute("""UPDATE quote_requests SET quote_valid_until=%s, updated_at=NOW()
                        WHERE id=%s AND tenant_id=%s AND status='quote_sent' RETURNING ref_no""",
                    (new_date, request_id, tenant_id))
        r = cur.fetchone()
        if not r:
            return False, "Only a quote with the buyer can be extended."
        _event(cur, request_id, f"Valid until changed to {new_date.strftime('%d %b %Y')}", actor["label"])
        conn.commit()
        return True, f"R-{r['ref_no']} now valid until {new_date.strftime('%d %b %Y')}. Tell the buyer."
    finally:
        cur.close(); conn.close()


def _expiry_once():
    """2 days before a quote expires: remind the salesperson. On the day
    after: ask them to extend it or close it as Lost (PhiXtra never extends
    a price by itself)."""
    import auto_followups as _af
    from team_performance import tz_for
    conn, cur = _db()
    try:
        cur.execute(f"""SELECT {_LIST_COLS} FROM quote_requests WHERE status='quote_sent' AND quote_valid_until IS NOT NULL
                          AND quote_valid_until BETWEEN CURRENT_DATE - 7 AND CURRENT_DATE + 3 LIMIT 500""")
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    for q in rows:
        try:
            if not allowed(q["tenant_id"]):
                continue
            today = datetime.now(tz_for(q["tenant_id"])).date()
            left = (q["quote_valid_until"] - today).days
            buyer = q.get("buyer_company") or q.get("buyer_name") or "the buyer"
            num = q.get("quote_number") or f"R-{q['ref_no']}"
            link = f"{_af.PORTAL}/quotations/requests/{q['id']}"
            vu = q["quote_valid_until"]
            if left == 2:
                ref, subj = f"quote_exp2:{q['id']}:{vu}", f"{num} for {buyer} expires in 2 days"
                lines = [f"Your quotation {num} to {buyer} ({money(q['quote_value'], q['quote_currency'])}) is valid until "
                         f"{vu.strftime('%A %d %B')}, with no answer yet.", "A call now often decides it."]
            elif left < 0:
                ref, subj = f"quote_expired:{q['id']}:{vu}", f"{num} for {buyer} has expired: extend or close?"
                lines = [f"Your quotation {num} to {buyer} expired on {vu.strftime('%d %B')} with no answer.",
                         "Open the request to extend it or close it as Lost. PhiXtra never extends a price by itself."]
            else:
                continue
            conn, cur = _db()
            try:
                if _af.notify(cur, q["tenant_id"], q.get("assigned_key") or "owner", ref, subj, lines,
                              lead_id=q.get("lead_id"), link=link) and left < 0:
                    _event_once(cur, q["id"], f"Expired on {vu.strftime('%d %b %Y')}: the salesperson was asked to extend "
                                         "or close it", "PhiXtra")
                conn.commit()
            finally:
                cur.close(); conn.close()
        except Exception as e:
            print(f"⚠️ quote_requests expiry {q['id']}:", e)


# ── alerts ──────────────────────────────────────────────────────────────────

def _alert(tenant_id: int, r: dict, kind: str) -> bool:
    import auto_followups as _af
    who_buyer = r.get("buyer_company") or r.get("buyer_name") or "A buyer"
    link = f"{_af.PORTAL}/quotations/requests/{r['id']}"
    n = len(r.get("lines") or [])
    if kind == "new":
        ref_key, who = f"quote_new:{r['id']}", (r.get("assigned_key") or "owner")
        subject = f"Quote request R-{r['ref_no']} for you: {who_buyer}"
        lines = [f"{who_buyer} asked for a quotation: {n} product{'s' if n != 1 else ''}"
                 + (f", by {CHANNEL_WORDS.get(r.get('channel'), r.get('channel')).lower()}." if r.get("channel") != "manual" else "."),
                 "Make the quotation in your own software, send it, then open the request and click Quote sent."]
    elif kind == "due":
        ref_key, who = f"quote_due:{r['id']}:{int(r['due_at'].timestamp())}", (r.get("assigned_key") or "owner")
        subject = f"Did you send the quotation for R-{r['ref_no']}?"
        lines = [f"The quotation for {who_buyer} (R-{r['ref_no']}) was due now.",
                 "If you've sent it, open the request and click Quote sent so the follow-ups start.",
                 "If not, the buyer is waiting: every hour lowers the chance of the order."]
    else:
        ref_key, who = f"quote_late:{r['id']}:{int(r['due_at'].timestamp())}", "account_owner"
        subject = f"Quote request R-{r['ref_no']} is overdue"
        lines = [f"{who_buyer} is still waiting for a quotation (R-{r['ref_no']}), handed to "
                 f"{r.get('assigned_to') or 'nobody'}.", "It was due over an hour ago."]
    conn, cur = _db()
    try:
        _af.notify(cur, tenant_id, who, ref_key, subject, lines, lead_id=r.get("lead_id"), link=link)
        conn.commit()
        return True
    except Exception as e:
        print(f"⚠️ quote_requests alert {kind} {r.get('id')}:", e)
        return False
    finally:
        cur.close(); conn.close()


def _due_loop_once():
    conn, cur = _db()
    try:
        cur.execute(f"""SELECT {_LIST_COLS} FROM quote_requests
                         WHERE status='to_quote' AND due_at <= NOW() AND due_at > NOW() - INTERVAL '7 days'
                         ORDER BY due_at LIMIT 200""")
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    cache = {}
    for r in rows:
        try:
            if not allowed(r["tenant_id"]):
                continue
            s = cache.setdefault(r["tenant_id"], settings(r["tenant_id"]))
            if s.get("alert_due"):
                _alert(r["tenant_id"], r, "due")
            if s.get("alert_manager") and r["due_at"] <= datetime.now(r["due_at"].tzinfo) - timedelta(hours=1):
                _alert(r["tenant_id"], r, "late")
        except Exception as e:
            print(f"⚠️ quote_requests due {r['id']}:", e)


def _loop():
    time.sleep(60)
    while True:
        lock_conn = None
        try:
            ensure_tables()
            lock_conn = get_db_connection()
            lock_conn.autocommit = True
            lc = lock_conn.cursor()
            lc.execute("SELECT pg_try_advisory_lock(80301032)")
            if lc.fetchone()[0]:
                try:
                    _due_loop_once()
                    _expiry_once()
                finally:
                    lc.execute("SELECT pg_advisory_unlock(80301032)")
        except Exception as e:
            print("⚠️ quote_requests loop:", e)
        finally:
            if lock_conn:
                try:
                    lock_conn.close()
                except Exception:
                    pass
        time.sleep(120)


def start():
    if not getattr(threading, "_phixtra_quote_requests_started", False):
        threading._phixtra_quote_requests_started = True  # type: ignore[attr-defined]
        threading.Thread(target=_loop, daemon=True).start()
