"""
incidents.py — Admin › Incidents: the written record the Incident Response
Policy asks for (8 Nov 2026, Shopify 7A item 16; policy approved by the CEO,
https://claude.ai/artifact/8cxP7CXoE6FbMRCjQJrpw5).

Owner only. One record per incident (or practice run):
  details      title, found at, reported by, grade, systems, what happened
  deadlines    Shopify told (24 h, when Shopify data is involved), businesses
               told (target 24 h, when businesses are affected), ICO told or
               "not needed" + reason (72 h, High / Medium)
  businesses   affected tenants + what data
  action log   entries can only be ADDED — never changed or deleted (trigger)
  status       open → contained → closed; review (cause, worked, didn't,
               changes) asked at close, reminder 7 days after close if empty

Text that may hold personal data is encrypted with data_crypto (item 9).
Records are deleted 3 years after closing (nightly); the database refuses
earlier deletes. Reminder emails to philip@phixtra.com:
  `python3 incidents.py reminders` (every 15 min) · `python3 incidents.py nightly`
"""
import time
from datetime import datetime, timedelta, timezone

from db import get_db_connection
import data_crypto as dc

KEEP_YEARS = 3
REMIND_TO = "philip@phixtra.com"
SHOPIFY_HOURS, BUSINESS_HOURS, ICO_HOURS = 24, 24, 72
SHOPIFY_WARN_H, ICO_WARN_H = 6, 12
REVIEW_DAYS = 7

GRADES = {"high": "High", "medium": "Medium", "low": "Low"}
GRADE_HELP = {"high": "personal data seen or taken, or likely to have been",
              "medium": "a weakness or leaked key, no sign anyone used it",
              "low": "an outage, no data at risk"}
STATUSES = {"open": "Open", "contained": "Contained", "closed": "Closed"}
SYSTEMS = {"portal": "Portal / Admin", "ai": "AI agent", "database": "Database", "backups": "Backups",
           "shopify": "Shopify app", "whatsapp": "WhatsApp", "email": "Email sending", "website": "Website",
           "cloudflare": "Cloudflare", "m365": "Microsoft 365", "other": "Other"}
ENC_COLS = ("title", "reported_by", "what", "data_affected", "ico_reason", "cause", "worked", "didnt", "changes")

_ready = False


# ── Tables ───────────────────────────────────────────────────────────────
def ensure(cur):
    global _ready
    if _ready:
        return
    cur.execute("""CREATE TABLE IF NOT EXISTS incidents (
                     id               SERIAL PRIMARY KEY,
                     created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                     created_by       TEXT,
                     found_at         TIMESTAMPTZ NOT NULL,
                     title            TEXT NOT NULL,          -- encrypted
                     reported_by      TEXT,                   -- encrypted
                     grade            VARCHAR(10) NOT NULL CHECK (grade IN ('high','medium','low')),
                     systems          TEXT[] NOT NULL DEFAULT '{}',
                     what             TEXT,                   -- encrypted
                     practice         BOOLEAN NOT NULL DEFAULT FALSE,
                     status           VARCHAR(10) NOT NULL DEFAULT 'open'
                                      CHECK (status IN ('open','contained','closed')),
                     shopify_involved BOOLEAN NOT NULL DEFAULT FALSE,
                     shopify_told_at  TIMESTAMPTZ,
                     business_told_at TIMESTAMPTZ,
                     ico_told_at      TIMESTAMPTZ,
                     ico_not_needed   BOOLEAN NOT NULL DEFAULT FALSE,
                     ico_reason       TEXT,                   -- encrypted
                     data_affected    TEXT,                   -- encrypted
                     cause            TEXT, worked TEXT, didnt TEXT, changes TEXT,   -- encrypted (review)
                     review_done_at   TIMESTAMPTZ,
                     closed_at        TIMESTAMPTZ,
                     closed_by        TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS incident_tenants (
                     incident_id INTEGER NOT NULL REFERENCES incidents(id),
                     tenant_id   INTEGER NOT NULL,
                     PRIMARY KEY (incident_id, tenant_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS incident_entries (
                     id          BIGSERIAL PRIMARY KEY,
                     incident_id INTEGER NOT NULL REFERENCES incidents(id),
                     at          TIMESTAMPTZ NOT NULL,           -- when the action happened
                     text        TEXT NOT NULL,                  -- encrypted
                     added_by    TEXT,
                     added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                     auto        BOOLEAN NOT NULL DEFAULT FALSE)""")
    cur.execute("CREATE INDEX IF NOT EXISTS incident_entries_inc ON incident_entries (incident_id, at)")
    cur.execute("""CREATE TABLE IF NOT EXISTS incident_reminders (
                     incident_id INTEGER NOT NULL, kind VARCHAR(20) NOT NULL,
                     sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (incident_id, kind))""")
    # The database itself refuses: changing / deleting action-log entries, and
    # deleting any incident (or its entries / businesses) before 3 years after close.
    cur.execute(f"""CREATE OR REPLACE FUNCTION incident_guard() RETURNS trigger AS $$
                    DECLARE inc_id INTEGER; closed TIMESTAMPTZ;
                    BEGIN
                      IF TG_OP = 'TRUNCATE' THEN
                        RAISE EXCEPTION 'incident records cannot be emptied';
                      END IF;
                      IF TG_TABLE_NAME = 'incident_entries' AND TG_OP = 'UPDATE' THEN
                        RAISE EXCEPTION 'incident action log entries cannot be changed';
                      END IF;
                      IF TG_OP = 'DELETE' THEN
                        IF TG_TABLE_NAME = 'incidents' THEN
                          inc_id := OLD.id;
                        ELSE
                          inc_id := OLD.incident_id;
                        END IF;
                        SELECT closed_at INTO closed FROM incidents WHERE id = inc_id;
                        IF TG_TABLE_NAME = 'incident_tenants' AND closed IS NULL THEN
                          RETURN OLD;   -- an open incident may take a business off its list
                        END IF;
                        IF closed IS NULL OR closed > NOW() - INTERVAL '{KEEP_YEARS} years' THEN
                          RAISE EXCEPTION 'incident records are kept {KEEP_YEARS} years after closing';
                        END IF;
                      END IF;
                      RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
                    END $$ LANGUAGE plpgsql""")
    for tbl in ("incidents", "incident_entries", "incident_tenants"):
        cur.execute("SELECT 1 FROM pg_trigger WHERE tgname=%s", (f"{tbl}_guard_rows",))
        if not cur.fetchone():
            ops = "UPDATE OR DELETE" if tbl == "incident_entries" else "DELETE"
            cur.execute(f"""CREATE TRIGGER {tbl}_guard_rows BEFORE {ops} ON {tbl}
                            FOR EACH ROW EXECUTE FUNCTION incident_guard()""")
            cur.execute(f"""CREATE TRIGGER {tbl}_guard_truncate BEFORE TRUNCATE ON {tbl}
                            FOR EACH STATEMENT EXECUTE FUNCTION incident_guard()""")
    _ready = True


def _conn():
    conn = get_db_connection()
    if not _ready:
        ensure(conn.cursor())
        conn.commit()
    return conn


def ref(i) -> str:
    return f"INC-{int(i):03d}"


# ── Deadlines ────────────────────────────────────────────────────────────
def deadlines(inc, n_tenants, now=None):
    """List of {key, label, due, done, state, left} for the deadlines that apply."""
    now = now or datetime.now(timezone.utc)
    out = []
    f = inc["found_at"]

    def one(key, label, hours, done, warn_h):
        due = f + timedelta(hours=hours)
        left = (due - now).total_seconds() / 3600
        state = "done" if done else ("late" if left < 0 else ("soon" if left <= warn_h else "ok"))
        out.append({"key": key, "label": label, "due": due, "done": done, "state": state, "left": left})

    if inc["shopify_involved"]:
        one("shopify", "Shopify told", SHOPIFY_HOURS, inc["shopify_told_at"], SHOPIFY_WARN_H)
    if n_tenants:
        one("business", "Businesses told", BUSINESS_HOURS, inc["business_told_at"], 6)
    if inc["grade"] in ("high", "medium"):
        one("ico", "ICO told", ICO_HOURS, inc["ico_told_at"] or (inc["ico_not_needed"] and "not needed"), ICO_WARN_H)
    return out


def left_text(hours) -> str:
    h = abs(hours)
    s = f"{int(h)}h" if h >= 1 else f"{int(h * 60)}m"
    return f"{s} left" if hours >= 0 else f"{s} overdue"


# ── Reads ────────────────────────────────────────────────────────────────
_COLS = ("id", "created_at", "created_by", "found_at", "title", "reported_by", "grade", "systems", "what",
         "practice", "status", "shopify_involved", "shopify_told_at", "business_told_at", "ico_told_at",
         "ico_not_needed", "ico_reason", "data_affected", "cause", "worked", "didnt", "changes",
         "review_done_at", "closed_at", "closed_by")


def _row(r):
    d = dict(zip(_COLS, r))
    for c in ENC_COLS:
        d[c] = dc.dec(d[c]) or ""
    d["ref"] = ref(d["id"])
    return d


def list_all():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"""SELECT {', '.join('i.' + c for c in _COLS)},
                               (SELECT COUNT(*) FROM incident_tenants t WHERE t.incident_id = i.id)
                          FROM incidents i
                         ORDER BY (i.status = 'closed'), i.found_at DESC""")
        out = []
        for r in cur.fetchall():
            d = _row(r[:-1])
            d["n_tenants"] = r[-1]
            d["deadlines"] = deadlines(d, d["n_tenants"]) if d["status"] != "closed" else []
            out.append(d)
        return out
    finally:
        conn.close()


def get(inc_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(_COLS)} FROM incidents WHERE id=%s", (inc_id,))
        r = cur.fetchone()
        if not r:
            return None
        d = _row(r)
        cur.execute("""SELECT t.id, t.name, COALESCE(t.account_kind, 'real') FROM incident_tenants it
                       JOIN tenants t ON t.id = it.tenant_id WHERE it.incident_id=%s ORDER BY t.name""", (inc_id,))
        d["tenants"] = [{"id": a, "name": b, "kind": c} for a, b, c in cur.fetchall()]
        cur.execute("""SELECT at, text, added_by, added_at, auto FROM incident_entries
                       WHERE incident_id=%s ORDER BY at, id""", (inc_id,))
        d["entries"] = [{"at": a, "text": dc.dec(b) or "", "by": c, "added_at": e, "auto": f}
                        for a, b, c, e, f in cur.fetchall()]
        d["deadlines"] = deadlines(d, len(d["tenants"]))
        return d
    finally:
        conn.close()


def tenant_choices():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM tenants ORDER BY name")
        return [{"id": a, "name": b or f"Business {a}"} for a, b in cur.fetchall()]
    finally:
        conn.close()


# ── Writes ───────────────────────────────────────────────────────────────
def _entry(cur, inc_id, text, by, at=None, auto=False):
    cur.execute("""INSERT INTO incident_entries (incident_id, at, text, added_by, auto)
                   VALUES (%s, COALESCE(%s, NOW()), %s, %s, %s)""", (inc_id, at, dc.enc(text), by, auto))


def create(f, by):
    systems = [s for s in f.get("systems", []) if s in SYSTEMS]
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO incidents (created_by, found_at, title, reported_by, grade, systems, what,
                                              practice, shopify_involved)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (by, f["found_at"], dc.enc(f["title"]), dc.enc(f.get("reported_by")), f["grade"], systems,
                     dc.enc(f.get("what")), bool(f.get("practice")), "shopify" in systems))
        inc_id = cur.fetchone()[0]
        _entry(cur, inc_id, f"Incident opened, graded {GRADES[f['grade']]}"
                            + (" (practice run)" if f.get("practice") else ""), by, auto=True)
        conn.commit()
        return inc_id
    finally:
        conn.close()


def add_entry(inc_id, text, at, by):
    conn = _conn()
    try:
        cur = conn.cursor()
        _entry(cur, inc_id, text, by, at=at)
        conn.commit()
    finally:
        conn.close()


def update(inc_id, what, f, by):
    """One change from the incident page. Every change also adds an automatic
    line to the action log so the record shows who did what and when."""
    inc = get(inc_id)
    if not inc:
        return "Incident not found."
    if inc["status"] == "closed" and what not in ("review", "reopen"):
        return "This incident is closed. Reopen it first."
    conn = _conn()
    try:
        cur = conn.cursor()
        note = None
        if what == "details":
            systems = [s for s in f.get("systems", []) if s in SYSTEMS]
            grade = f.get("grade") if f.get("grade") in GRADES else inc["grade"]
            cur.execute("""UPDATE incidents SET title=%s, reported_by=%s, what=%s, grade=%s, systems=%s,
                                  shopify_involved=%s, data_affected=%s WHERE id=%s""",
                        (dc.enc(f.get("title") or inc["title"]), dc.enc(f.get("reported_by")), dc.enc(f.get("what")),
                         grade, systems, "shopify" in systems, dc.enc(f.get("data_affected")), inc_id))
            note = "Details updated" + (f"; grade {GRADES[inc['grade']]} → {GRADES[grade]}"
                                        if grade != inc["grade"] else "")
        elif what == "told":
            key, at = f.get("key"), f.get("at")
            col = {"shopify": "shopify_told_at", "business": "business_told_at", "ico": "ico_told_at"}.get(key)
            if not col or not at:
                return "Choose when they were told."
            cur.execute(f"UPDATE incidents SET {col}=%s WHERE id=%s", (at, inc_id))
            if key == "ico":
                cur.execute("UPDATE incidents SET ico_not_needed=FALSE, ico_reason=NULL WHERE id=%s", (inc_id,))
            note = {"shopify": "Shopify told", "business": "Affected businesses told", "ico": "ICO told"}[key] \
                + f" at {at.astimezone(_uk()).strftime('%-d %b %H:%M')} UK"
        elif what == "ico_not_needed":
            reason = (f.get("reason") or "").strip()
            if not reason:
                return "Give the reason the ICO does not need to be told."
            cur.execute("UPDATE incidents SET ico_not_needed=TRUE, ico_told_at=NULL, ico_reason=%s WHERE id=%s",
                        (dc.enc(reason), inc_id))
            note = f"ICO not told — reason: {reason}"
        elif what == "add_tenant":
            tid = f.get("tenant_id")
            cur.execute("SELECT name FROM tenants WHERE id=%s", (tid,))
            t = cur.fetchone()
            if not t:
                return "Business not found — pick it from the list."
            cur.execute("INSERT INTO incident_tenants VALUES (%s,%s) ON CONFLICT DO NOTHING", (inc_id, tid))
            note = f"Affected business added: {t[0]} (#{tid})" if cur.rowcount else None
        elif what == "remove_tenant":
            tid = f.get("tenant_id")
            cur.execute("SELECT name FROM tenants WHERE id=%s", (tid,))
            t = cur.fetchone()
            cur.execute("DELETE FROM incident_tenants WHERE incident_id=%s AND tenant_id=%s", (inc_id, tid))
            note = f"Affected business removed: {t[0] if t else ''} (#{tid})" if cur.rowcount else None
        elif what == "status":
            st = f.get("status")
            if st == "contained" and inc["status"] == "open":
                cur.execute("UPDATE incidents SET status='contained' WHERE id=%s", (inc_id,))
                note = "Marked contained — the problem is stopped"
            elif st == "open" and inc["status"] == "contained":
                cur.execute("UPDATE incidents SET status='open' WHERE id=%s", (inc_id,))
                note = "Back to open"
            elif st == "closed":
                missing = [d["label"] for d in deadlines(inc, len(inc["tenants"])) if not d["done"]]
                if missing and not inc["practice"]:
                    return "Before closing, record: " + ", ".join(missing) + "."
                if not (f.get("cause") or "").strip():
                    return "Before closing, write down what caused it."
                _save_review(cur, inc_id, f)
                cur.execute("UPDATE incidents SET status='closed', closed_at=NOW(), closed_by=%s WHERE id=%s",
                            (by, inc_id))
                note = "Incident closed"
            else:
                return None
        elif what == "review":
            _save_review(cur, inc_id, f)
            note = "Review updated"
        elif what == "reopen":
            cur.execute("UPDATE incidents SET status='open', closed_at=NULL, closed_by=NULL WHERE id=%s", (inc_id,))
            cur.execute("DELETE FROM incident_reminders WHERE incident_id=%s AND kind='review'", (inc_id,))
            note = "Incident reopened"
        else:
            return None
        if note:
            _entry(cur, inc_id, note, by, auto=True)
        conn.commit()
        return None
    finally:
        conn.close()


def _save_review(cur, inc_id, f):
    vals = {k: (f.get(k) or "").strip() for k in ("cause", "worked", "didnt", "changes")}
    done = all(vals.values())
    cur.execute("""UPDATE incidents SET cause=%s, worked=%s, didnt=%s, changes=%s,
                          review_done_at = CASE WHEN %s THEN COALESCE(review_done_at, NOW()) ELSE NULL END
                   WHERE id=%s""",
                (dc.enc(vals["cause"]), dc.enc(vals["worked"]), dc.enc(vals["didnt"]), dc.enc(vals["changes"]),
                 done, inc_id))


def _uk():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Europe/London")


def parse_uk(s):
    """'2026-11-08T14:05' typed as UK time → aware datetime (None if bad)."""
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=_uk())
    except ValueError:
        return None


# ── Jobs ─────────────────────────────────────────────────────────────────
def reminders():
    """Email once per incident per kind: Shopify 6 h before, ICO 12 h before
    (and again if overdue), review 7 days after closing when not finished."""
    now = datetime.now(timezone.utc)
    msgs = []
    for inc in list_all():
        tag = "[PRACTICE] " if inc["practice"] else ""
        if inc["status"] != "closed":
            for d in inc["deadlines"]:
                if d["done"] or d["key"] == "business":
                    continue
                kinds = []
                if d["state"] in ("soon", "late"):
                    kinds.append(f"{d['key']}_soon")
                if d["state"] == "late":
                    kinds.append(f"{d['key']}_late")
                for k in kinds:
                    if _mark(inc["id"], k):
                        who = "Shopify" if d["key"] == "shopify" else "the ICO"
                        msgs.append(f"{tag}{inc['ref']} {inc['title']}: {who} must be told by "
                                    f"{d['due'].astimezone(_uk()).strftime('%a %-d %b %H:%M')} UK "
                                    f"({left_text(d['left'])}).")
        elif not inc["review_done_at"] and inc["closed_at"] and now - inc["closed_at"] >= timedelta(days=REVIEW_DAYS):
            if _mark(inc["id"], "review"):
                msgs.append(f"{tag}{inc['ref']} {inc['title']}: the review after closing is not finished "
                            f"(cause, what worked, what didn't, changes).")
    if msgs:
        body = "\n".join("- " + m for m in msgs)
        body += "\n\nOpen Admin › Incidents: https://portal.phixtra.com/admin/incidents\n"
        _email(f"PhiXtra incident reminder ({len(msgs)})", body)
        print(time.strftime("%F %T"), "incident reminders sent:", msgs)


def _mark(inc_id, kind) -> bool:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO incident_reminders VALUES (%s,%s) ON CONFLICT DO NOTHING", (inc_id, kind))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def nightly():
    """Delete incidents closed more than 3 years ago (the policy's keep period)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM incidents WHERE closed_at < NOW() - INTERVAL '{KEEP_YEARS} years'")
        ids = [r[0] for r in cur.fetchall()]
        for i in ids:
            cur.execute("DELETE FROM incident_entries WHERE incident_id=%s", (i,))
            cur.execute("DELETE FROM incident_tenants WHERE incident_id=%s", (i,))
            cur.execute("DELETE FROM incident_reminders WHERE incident_id=%s", (i,))
            cur.execute("DELETE FROM incidents WHERE id=%s", (i,))
        conn.commit()
        if ids:
            print(time.strftime("%F %T"), f"incidents: deleted {len(ids)} closed more than {KEEP_YEARS} years ago")
    finally:
        conn.close()


def _email(subject, body):
    import access_log
    _to = access_log.ALERT_TO
    try:
        access_log.ALERT_TO = REMIND_TO
        access_log._email(subject, body)
    finally:
        access_log.ALERT_TO = _to


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    {"nightly": nightly, "reminders": reminders}.get((sys.argv[1:] or ["reminders"])[0], reminders)()
