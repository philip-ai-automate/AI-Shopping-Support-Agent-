"""
support_requests.py — Ask PhiXtra → PhiXtra team hand-over (23 Nov 2026).

Approved mock-up: https://claude.ai/artifact/Tr234G8h9Cun71J692Qt34

When a business asks for a person in Ask PhiXtra (or Ask PhiXtra can't help),
a support request is opened for that business. It is worked on the business's
EXISTING Accounts page (/admin/accounts/<tenant_id>#support): replies go through
the page's own WhatsApp / call / email tabs, so they land on the same Timeline
and in Admin Inbox. Admin › Accounts › Support requests is only the list.

Everything is saved on the account's one record (am_activity):
  kind 'support'  — asked for a person / request closed
  kind 'problem'  — an account problem PhiXtra found (e.g. WhatsApp locked by Meta)

Alerts to PhiXtra staff (Admin › Settings › Support alerts, owner-only):
  phone notification (web push via the WhatsApp gateway, which holds the key),
  WhatsApp (template phixtra_support_alert from PhiXtra's number), or both.
  The business's Account Manager plus everyone switched on is alerted.
  Reminders and the delayed WhatsApp are sent by tick(), run every minute by
  am_reply_alerts.py (phixtra-am-reply-alerts.timer).
"""
import html
import os
import re
from datetime import datetime, timedelta, timezone

import psycopg2.extras
import requests

from db import get_db_connection, insert_audit_log
import data_crypto

try:
    from zoneinfo import ZoneInfo
    _UK = ZoneInfo("Europe/London")
except Exception:          # pragma: no cover
    _UK = timezone.utc

SUPPORT_EMAIL = os.getenv("ASK_PHIXTRA_SUPPORT_EMAIL", "support@phixtra.com")
PORTAL = (os.getenv("PORTAL_BASE_URL") or "https://portal.phixtra.com").rstrip("/")
GATEWAY = os.getenv("WA_GATEWAY_INTERNAL_URL", "http://127.0.0.1:8001")
ALERT_TEMPLATE = "phixtra_support_alert"

OUTCOMES = [("solved", "Solved"), ("meta", "Waiting on Meta"), ("customer", "Waiting on the business"),
            ("notours", "Not a PhiXtra issue")]
OUTCOME_LABEL = dict(OUTCOMES)

# Alert settings live in am_settings (admin_accounts.SETTING_DEFAULTS)
METHODS = [("phone", "Phone notification"), ("wa", "WhatsApp"), ("both", "Both")]
VIA_LABEL = {"whatsapp": "WhatsApp", "email": "email", "call": "a phone call"}   # how the business wants a reply

_ready = False


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def uk_time(dt) -> str:
    if not dt:
        return ""
    if not getattr(dt, "tzinfo", None):
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_UK).strftime("%H:%M")


def uk_day_time(dt) -> str:
    if not dt:
        return ""
    if not getattr(dt, "tzinfo", None):
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt.astimezone(_UK)
    return f"{d.day} {d:%b} {d:%H:%M}"


def ensure_tables():
    global _ready
    if _ready:
        return
    conn, cur = _db()
    try:
        cur.execute("SET lock_timeout = '3s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS support_requests (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            customer_id INTEGER, team_member_id INTEGER,
            person_name TEXT,
            via VARCHAR(10) NOT NULL DEFAULT 'email',
            contact TEXT,
            note TEXT,
            page TEXT,
            question_ids BIGINT[] NOT NULL DEFAULT '{}',
            problem TEXT,
            status VARCHAR(12) NOT NULL DEFAULT 'open',
            outcome VARCHAR(20),
            handled_by VARCHAR(200), handled_at TIMESTAMPTZ,
            first_opened_at TIMESTAMPTZ, first_opened_by VARCHAR(200),
            alerted_at TIMESTAMPTZ, wa_alert_at TIMESTAMPTZ, reminded_at TIMESTAMPTZ, reminders INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("CREATE INDEX IF NOT EXISTS support_requests_open ON support_requests (status, tenant_id)")
        cur.execute("""CREATE TABLE IF NOT EXISTS admin_push_devices (
            id BIGSERIAL PRIMARY KEY,
            admin_username VARCHAR(200) NOT NULL,
            endpoint TEXT NOT NULL UNIQUE, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
            device_label VARCHAR(80),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_sent_at TIMESTAMPTZ,
            fail_count INTEGER NOT NULL DEFAULT 0, last_error TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS admin_alert_prefs (
            admin_username VARCHAR(200) PRIMARY KEY,
            alerts_on BOOLEAN NOT NULL DEFAULT TRUE,
            wa_number VARCHAR(40),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.commit()
        _ready = True
    finally:
        cur.close(); conn.close()


# ── Account problems PhiXtra can see (no buyer content is read) ────────────

def _problem_title(err: str) -> str:
    e = (err or "").lower()
    if "locked" in e:
        return "Meta has locked the WhatsApp Business Account"
    if "restrict" in e or "banned" in e or "disabled" in e or "suspend" in e:
        return "Meta has restricted the WhatsApp Business Account"
    if "payment" in e:
        return "WhatsApp messages are failing: a Meta payment problem"
    return "WhatsApp messages are not being sent"


def account_problems(tenant_id: int) -> list:
    """[{kind, title, error, since, last_ok, number, week}] — WhatsApp problems.
    24 Nov 2026: first Meta's real state from the WhatsApp health check
    (kind 'health', with state red/yellow and the health kind), then WhatsApp
    sends that started failing and haven't worked since."""
    out = []
    try:
        import wa_health as WH
        for r in WH.for_tenant(tenant_id):
            if r["state"] in ("red", "yellow"):
                out.append({"kind": "health", "state": r["state"], "health": r["kind"], "title": r["head"],
                            "label": r["label"], "error": r.get("meta_words") or "", "since": r["since"],
                            "last_ok": None, "number": r["display_number"] or "", "week": 0,
                            "sentence": WH.sentence(r)})
        out.sort(key=lambda p: p["state"] != "red")
    except Exception as e:
        print("⚠️ support_requests health problems:", e)
    if any(p.get("state") == "red" for p in out):
        return out          # Meta's own answer already explains failed sends
    conn, cur = _db()
    try:
        cur.execute("""
            SELECT s.status, s.error, COALESCE(s.status_at, m.created_at) AS at, m.phone_number_id
              FROM wa_message_log m JOIN wa_msg_status s ON s.meta_message_id = m.meta_message_id
             WHERE m.tenant_id = %s AND m.direction = 'outbound' AND m.created_at > NOW() - INTERVAL '4 days'
             ORDER BY COALESCE(s.status_at, m.created_at) DESC LIMIT 400""", (tenant_id,))
        rows = cur.fetchall() or []
        fails = [r for r in rows if r["status"] == "failed" and r["error"]]
        oks = [r for r in rows if r["status"] in ("sent", "delivered", "read")]
        if fails:
            last_ok = max((r["at"] for r in oks), default=None)
            first_fail_after = [r for r in fails if not last_ok or r["at"] > last_ok]
            # only a problem when nothing has worked since the failures began, and the
            # error is about the account (not one buyer's number)
            if first_fail_after:
                err = first_fail_after[0]["error"]
                if not re.search(r"re-engagement|24 hours|user's number|not a valid whatsapp|recipient", err or "", re.I):
                    since = min(r["at"] for r in first_fail_after)
                    cur.execute("""SELECT display_phone_number FROM wa_tenants WHERE phone_number_id=%s LIMIT 1""",
                                (first_fail_after[0]["phone_number_id"],))
                    num = (cur.fetchone() or {}).get("display_phone_number") or ""
                    cur.execute("""SELECT COUNT(*) AS n FROM wa_message_log WHERE tenant_id=%s
                                    AND created_at > NOW() - INTERVAL '7 days'""", (tenant_id,))
                    week = int(cur.fetchone()["n"])
                    out.append({"kind": "wa", "title": _problem_title(err), "error": err, "since": since,
                                "last_ok": last_ok, "number": num, "week": week})
    except Exception as e:
        conn.rollback()
        print("⚠️ support_requests.account_problems:", e)
    finally:
        cur.close(); conn.close()
    return out


def problem_sentence(p: dict) -> str:
    if p.get("kind") == "health":
        return p["sentence"]
    since = uk_day_time(p["since"])
    s = f"{p['title']}. WhatsApp messages{(' from ' + p['number']) if p.get('number') else ''} have not been sent since {since}"
    if p.get("last_ok"):
        s += f" (last one sent {uk_day_time(p['last_ok'])})"
    return s + "."


# ── Requests ──────────────────────────────────────────────────────────────

def open_request(tenant_id: int):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM support_requests WHERE tenant_id=%s AND status='open' ORDER BY id DESC LIMIT 1",
                    (tenant_id,))
        r = cur.fetchone()
        return _dec(r) if r else None
    finally:
        cur.close(); conn.close()


def get(rid: int):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM support_requests WHERE id=%s", (rid,))
        r = cur.fetchone()
        return _dec(r) if r else None
    finally:
        cur.close(); conn.close()


def _dec(r):
    r = dict(r)
    r["note"] = data_crypto.dec(r.get("note")) if r.get("note") else r.get("note")
    return r


def open_tenant_ids() -> set:
    try:
        ensure_tables()
        conn, cur = _db()
        try:
            cur.execute("SELECT DISTINCT tenant_id FROM support_requests WHERE status='open'")
            return {r["tenant_id"] for r in cur.fetchall() or []}
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ support_requests.open_tenant_ids:", e)
        return set()


def recent_question_ids(tenant_id: int, customer_id, team_member_id, hours: int = 6) -> list:
    conn, cur = _db()
    try:
        cur.execute("""SELECT id FROM ask_phixtra_questions WHERE tenant_id=%s AND customer_id IS NOT DISTINCT FROM %s
                         AND team_member_id IS NOT DISTINCT FROM %s AND created_at > NOW() - (%s || ' hours')::interval
                       ORDER BY id""", (tenant_id, customer_id, team_member_id, str(hours)))
        return [r["id"] for r in cur.fetchall() or []]
    finally:
        cur.close(); conn.close()


def conversation(req: dict) -> list:
    """[{k: 'c'|'b', at, text}] — what the person asked and what Ask PhiXtra answered."""
    ids = list(req.get("question_ids") or [])
    if not ids:
        return []
    conn, cur = _db()
    try:
        cur.execute("""SELECT id, question, answer, answered, created_at FROM ask_phixtra_questions
                        WHERE id = ANY(%s) ORDER BY id""", (ids,))
        out = []
        for q in cur.fetchall() or []:
            out.append({"k": "c", "at": q["created_at"], "text": q["question"]})
            if (q["answer"] or "").strip():
                out.append({"k": "b", "at": q["created_at"], "text": q["answer"].replace("**", "")})
        return out
    finally:
        cur.close(); conn.close()


def _first_name(person: str) -> str:
    return (person or "").strip().split(" ")[0] or "They"


def create(tenant_id: int, customer_id, team_member_id, person_name: str, via: str, contact: str,
           note: str, page: str, question_ids: list) -> tuple:
    """(request, created) — one open request per business; a second ask adds to it."""
    ensure_tables()
    existing = open_request(tenant_id)
    probs = account_problems(tenant_id)
    conn, cur = _db()
    try:
        if existing:
            ids = sorted(set(list(existing["question_ids"] or []) + list(question_ids or [])))
            cur.execute("UPDATE support_requests SET question_ids=%s WHERE id=%s", (ids, existing["id"]))
            if note:
                _log(cur, tenant_id, "support", None, subject=f"{_first_name(person_name)} added to the support request",
                     body=note)
            conn.commit()
            existing["question_ids"] = ids
            return existing, False
        problem = problem_sentence(probs[0]) if probs else None
        cur.execute("""INSERT INTO support_requests (tenant_id, customer_id, team_member_id, person_name, via, contact,
                         note, page, question_ids, problem) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (tenant_id, customer_id, team_member_id, person_name, via, contact,
                     data_crypto.enc(note) if note else None, page, list(question_ids or []), problem))
        req = _dec(cur.fetchone())
        if probs:
            # one 'problem' entry per day on the Timeline
            cur.execute("""SELECT 1 FROM am_activity WHERE tenant_id=%s AND kind='problem'
                            AND created_at > NOW() - INTERVAL '1 day' LIMIT 1""", (tenant_id,))
            if not cur.fetchone():
                p = probs[0]
                _log(cur, tenant_id, "problem", None, subject="WhatsApp problem: " + p["title"],
                     body=problem_sentence(p) + f"\nMeta's message: {p['error']}", at=p["since"])
        n = len(question_ids or [])
        _log(cur, tenant_id, "support", None,
             subject="Asked for a person in Ask PhiXtra",
             body=(f"{person_name or 'Someone'} · {n} question{'s' if n != 1 else ''} · prefers "
                   f"{VIA_LABEL.get(via, 'email')} ({contact or 'no contact given'})"
                   + (f"\nNote: {note}" if note else "")))
        if question_ids:
            cur.execute("UPDATE ask_phixtra_questions SET emailed=TRUE WHERE id = ANY(%s)", (list(question_ids),))
        conn.commit()
    finally:
        cur.close(); conn.close()
    insert_audit_log(action="support_request_opened", tenant_id=tenant_id, details={"id": req["id"], "via": via})
    return req, True


def create_auto(tenant_id: int, problem: str) -> tuple:
    """(request, created): opened by the WhatsApp health check (24 Nov 2026),
    nobody asked. Replies go to the owner's email (WhatsApp may be the problem)."""
    ensure_tables()
    existing = open_request(tenant_id)
    if existing:
        return existing, False
    conn, cur = _db()
    try:
        cur.execute("""SELECT email FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1""", (tenant_id,))
        email = (cur.fetchone() or {}).get("email") or ""
        cur.execute("""INSERT INTO support_requests (tenant_id, person_name, via, contact, problem)
                       VALUES (%s,'','email',%s,%s) RETURNING *""", (tenant_id, email, problem))
        req = _dec(cur.fetchone())
        _log(cur, tenant_id, "support", None, subject="Support request opened by the WhatsApp health check",
             body=problem)
        conn.commit()
    finally:
        cur.close(); conn.close()
    insert_audit_log(action="support_request_opened", tenant_id=tenant_id, details={"id": req["id"], "via": "auto"})
    return req, True


def _log(cur, tenant_id, kind, by, subject=None, body=None, outcome=None, follow_up_on=None, at=None):
    if at:
        cur.execute("""INSERT INTO am_activity (tenant_id, kind, subject, body, outcome, follow_up_on, by_admin, ok, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s)""",
                    (tenant_id, kind, subject, data_crypto.enc(body), outcome, follow_up_on, by, at))
    else:
        cur.execute("""INSERT INTO am_activity (tenant_id, kind, subject, body, outcome, follow_up_on, by_admin, ok)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)""",
                    (tenant_id, kind, subject, data_crypto.enc(body), outcome, follow_up_on, by))


def mark_opened(req: dict, username: str):
    if not req or req.get("first_opened_at"):
        return
    conn, cur = _db()
    try:
        cur.execute("""UPDATE support_requests SET first_opened_at=NOW(), first_opened_by=%s
                        WHERE id=%s AND first_opened_at IS NULL""", (username, req["id"]))
        conn.commit()
    finally:
        cur.close(); conn.close()


def close(rid: int, outcome: str, by: str, follow_up_days: int = 0) -> bool:
    import admin_accounts as M
    req = get(rid)
    if not req or req["status"] != "open" or outcome not in OUTCOME_LABEL:
        return False
    fu = (datetime.now(_UK).date() + timedelta(days=follow_up_days)) if follow_up_days else None
    conn, cur = _db()
    try:
        cur.execute("""UPDATE support_requests SET status='handled', outcome=%s, handled_by=%s, handled_at=NOW()
                        WHERE id=%s AND status='open'""", (outcome, by, rid))
        if not cur.rowcount:
            conn.rollback(); return False
        _log(cur, req["tenant_id"], "support", by, subject="Support request closed: " + OUTCOME_LABEL[outcome],
             outcome=outcome, follow_up_on=fu)
        if fu:
            M._upsert_state(cur, req["tenant_id"], by, next_follow_up=fu)
        conn.commit()
    finally:
        cur.close(); conn.close()
    insert_audit_log(admin_username=by, action="support_request_closed", tenant_id=req["tenant_id"],
                     details={"id": rid, "outcome": outcome, "follow_up_days": follow_up_days})
    return True


def reopen(rid: int, by: str) -> bool:
    req = get(rid)
    if not req or req["status"] == "open" or open_request(req["tenant_id"]):
        return False
    conn, cur = _db()
    try:
        cur.execute("""UPDATE support_requests SET status='open', outcome=NULL, handled_by=NULL, handled_at=NULL
                        WHERE id=%s""", (rid,))
        _log(cur, req["tenant_id"], "support", by, subject="Support request reopened")
        conn.commit()
    finally:
        cur.close(); conn.close()
    return True


def last_for_tenant(tenant_id: int):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM support_requests WHERE tenant_id=%s ORDER BY id DESC LIMIT 1", (tenant_id,))
        r = cur.fetchone()
        return _dec(r) if r else None
    finally:
        cur.close(); conn.close()


def list_requests(status: str = "open") -> list:
    """For Admin › Accounts › Support requests."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute(f"""
            SELECT r.*, t.name AS business, s.assigned_admin,
                   (SELECT q.question FROM ask_phixtra_questions q WHERE q.id = ANY(r.question_ids) ORDER BY q.id DESC LIMIT 1) AS last_q,
                   (SELECT MAX(a.created_at) FROM am_activity a WHERE a.tenant_id = r.tenant_id
                     AND a.kind IN ('call','whatsapp','email')) AS last_contact
              FROM support_requests r
              JOIN tenants t ON t.id = r.tenant_id
              LEFT JOIN am_account_state s ON s.tenant_id = r.tenant_id
             WHERE r.status = %s
             ORDER BY {'r.created_at ASC' if status == 'open' else 'r.handled_at DESC NULLS LAST'} LIMIT 300""", (status,))
        return [_dec(r) for r in cur.fetchall() or []]
    finally:
        cur.close(); conn.close()


def waiting_text(created_at) -> str:
    mins = int((datetime.now(timezone.utc) - created_at).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 0)} min"
    if mins < 48 * 60:
        h, m = divmod(mins, 60)
        return f"{h} h {m} min" if m and h < 10 else f"{h} h"
    return f"{mins // 1440} days"


# ── Alert settings + people ───────────────────────────────────────────────

def alert_settings() -> dict:
    import admin_accounts as M
    st = M.get_settings()
    method = st.get("support_method") or "both"
    try:
        delay = int(st.get("support_wa_delay") or 15)
    except ValueError:
        delay = 15
    try:
        remind = int(st.get("support_remind") or 60)
    except ValueError:
        remind = 60
    return {"method": method if method in dict(METHODS) else "both", "delay": delay, "remind": remind}


def people() -> list:
    """Every active Admin login that can see Accounts (or is the owner),
    with alerts on/off, WhatsApp number and phone-notification devices."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT u.username, u.role, u.permissions, COALESCE(p.alerts_on, TRUE) AS alerts_on, p.wa_number
                         FROM admin_users u LEFT JOIN admin_alert_prefs p ON p.admin_username = u.username
                        WHERE u.active AND u.username <> 'tutorial-bot' ORDER BY u.id""")
        users = cur.fetchall() or []
        cur.execute("""SELECT admin_username, device_label, created_at FROM admin_push_devices ORDER BY created_at""")
        devs = {}
        for d in cur.fetchall() or []:
            devs.setdefault(d["admin_username"], []).append(d)
    finally:
        cur.close(); conn.close()
    import admin_accounts as M
    out = []
    seen_owner_email = set()
    for u in users:
        perms = u["permissions"] or {}
        if isinstance(perms, str):
            import json
            try:
                perms = json.loads(perms)
            except Exception:
                perms = {}
        is_owner = (u["role"] or "owner") == "owner"
        if not is_owner and not (perms.get("accounts") or {}).get("view"):
            continue
        out.append({"username": u["username"], "name": M.manager_name(u["username"]), "is_owner": is_owner,
                    "alerts_on": bool(u["alerts_on"]), "wa_number": u["wa_number"] or "",
                    "devices": devs.get(u["username"], [])})
    return out


def save_people(prefs: dict, by: str):
    """prefs = {username: {"alerts_on": bool, "wa_number": str}}"""
    ensure_tables()
    conn, cur = _db()
    try:
        for u, p in prefs.items():
            num = re.sub(r"[^\d+]", "", p.get("wa_number") or "")[:20] or None
            cur.execute("""INSERT INTO admin_alert_prefs (admin_username, alerts_on, wa_number, updated_at)
                           VALUES (%s,%s,%s,NOW()) ON CONFLICT (admin_username) DO UPDATE
                           SET alerts_on=EXCLUDED.alerts_on, wa_number=EXCLUDED.wa_number, updated_at=NOW()""",
                        (u, bool(p.get("alerts_on")), num))
        conn.commit()
    finally:
        cur.close(); conn.close()


def recipients(req: dict) -> list:
    """The business's Account Manager plus everyone switched on."""
    conn, cur = _db()
    try:
        cur.execute("SELECT assigned_admin FROM am_account_state WHERE tenant_id=%s", (req["tenant_id"],))
        am = (cur.fetchone() or {}).get("assigned_admin")
    finally:
        cur.close(); conn.close()
    ppl = people()
    out = [p for p in ppl if p["alerts_on"] or p["username"] == am]
    out.sort(key=lambda p: p["username"] != am)
    return out


# ── Sending alerts ────────────────────────────────────────────────────────

def _push(usernames: list, title: str, body: str, url: str, tag: str) -> int:
    if not usernames:
        return 0
    try:
        r = requests.post(GATEWAY + "/internal/admin-push",
                          json={"usernames": usernames, "title": title, "body": body, "url": url, "tag": tag},
                          headers={"Authorization": "Bearer " + os.getenv("PHIXTRA_INTERNAL_TOKEN", "")}, timeout=25)
        return int((r.json() or {}).get("sent") or 0) if r.ok else 0
    except Exception as e:
        print("⚠️ support push:", e)
        return 0


def alert_template_ready() -> bool:
    import admin_accounts as M
    nums = M.platform_numbers()
    if not nums:
        return False
    try:
        r = requests.get(f"{M.GRAPH}/{nums[0]['waba_id']}/message_templates",
                         params={"name": ALERT_TEMPLATE, "fields": "name,status"},
                         headers={"Authorization": f"Bearer {nums[0]['access_token']}"}, timeout=8).json()
        return any(t.get("name") == ALERT_TEMPLATE and t.get("status") == "APPROVED" for t in r.get("data", []))
    except Exception:
        return False


def alert_template_status() -> str:
    import admin_accounts as M
    nums = M.platform_numbers()
    if not nums:
        return "NO_NUMBER"
    try:
        r = requests.get(f"{M.GRAPH}/{nums[0]['waba_id']}/message_templates",
                         params={"name": ALERT_TEMPLATE, "fields": "name,status"},
                         headers={"Authorization": f"Bearer {nums[0]['access_token']}"}, timeout=8).json()
        for t in r.get("data", []):
            if t.get("name") == ALERT_TEMPLATE:
                return t.get("status") or "UNKNOWN"
        return "NOT_SUBMITTED"
    except Exception:
        return "UNKNOWN"


def _wa_alert(numbers: list, business: str, since: str, tenant_id: int) -> int:
    import admin_accounts as M
    num = M.am_number()
    if not num or not numbers:
        return 0
    sent = 0
    for to in numbers:
        to = re.sub(r"\D", "", to or "")
        if len(to) < 8:
            continue
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                   "template": {"name": ALERT_TEMPLATE, "language": {"code": "en"}, "components": [
                       {"type": "body", "parameters": [{"type": "text", "text": business[:60]},
                                                        {"type": "text", "text": since}]},
                       {"type": "button", "sub_type": "url", "index": "0",
                        "parameters": [{"type": "text", "text": str(tenant_id)}]}]}}
        try:
            r = requests.post(f"{M.GRAPH}/{num['phone_number_id']}/messages", json=payload,
                              headers={"Authorization": f"Bearer {num['access_token']}"}, timeout=15)
            if r.status_code == 200:
                sent += 1
            else:
                print("⚠️ support WhatsApp alert:", r.text[:300])
        except Exception as e:
            print("⚠️ support WhatsApp alert:", e)
    return sent


def _business(tenant_id: int) -> str:
    conn, cur = _db()
    try:
        cur.execute("""SELECT t.name, c.first_name, c.last_name, c.email FROM tenants t
                        LEFT JOIN LATERAL (SELECT * FROM customers WHERE tenant_id=t.id ORDER BY id LIMIT 1) c ON TRUE
                        WHERE t.id=%s""", (tenant_id,))
        r = cur.fetchone() or {}
        return (r.get("name") or " ".join(x for x in [r.get("first_name"), r.get("last_name")] if x)
                or r.get("email") or "A business")
    finally:
        cur.close(); conn.close()


def send_alerts(req: dict, stage: str = "first") -> dict:
    """stage: first | wa | remind. Returns {push, wa}."""
    st = alert_settings()
    who = recipients(req)
    business = _business(req["tenant_id"])
    since = uk_time(req["created_at"])
    first = _first_name(req.get("person_name"))
    url = f"{PORTAL}/admin/accounts/{req['tenant_id']}#support"
    res = {"push": 0, "wa": 0}
    use_push = st["method"] in ("phone", "both")
    use_wa = st["method"] in ("wa", "both")
    if stage == "first":
        wa_now = use_wa and (st["method"] == "wa" or st["delay"] == 0)
    elif stage == "wa":
        wa_now, use_push = use_wa, False
    else:                                  # reminder: same way as the first alert
        wa_now = use_wa
    if use_push:
        title = (f"Reminder: {business} still needs a person" if stage == "remind" else f"{business} needs a person")
        body = (req.get("problem") or "").split(".")[0] or f"{first} asked for a person in Ask PhiXtra"
        text = (f"{first}: asked for a person · {body}" if (req.get("person_name") or "").strip()
                else f"WhatsApp problem found · {body}")
        res["push"] = _push([p["username"] for p in who], title, text[:170], url, f"support-{req['id']}")
    if wa_now and alert_template_ready():
        res["wa"] = _wa_alert([p["wa_number"] for p in who if p["wa_number"]], business, since, req["tenant_id"])
    conn, cur = _db()
    try:
        if stage == "first":
            cur.execute("UPDATE support_requests SET alerted_at=NOW() WHERE id=%s", (req["id"],))
        if wa_now:
            cur.execute("UPDATE support_requests SET wa_alert_at=NOW() WHERE id=%s", (req["id"],))
        if stage == "remind":
            cur.execute("UPDATE support_requests SET reminded_at=NOW(), reminders=reminders+1 WHERE id=%s", (req["id"],))
        conn.commit()
    finally:
        cur.close(); conn.close()
    print(f"🔔 support request {req['id']} {stage}: push {res['push']}, WhatsApp {res['wa']} → {[p['username'] for p in who]}")
    return res


def tick():
    """Every minute: the delayed WhatsApp (Both) and reminders."""
    ensure_tables()
    st = alert_settings()
    now = datetime.now(timezone.utc)
    for req in list_requests("open"):
        try:
            if (st["method"] == "both" and st["delay"] > 0 and not req["wa_alert_at"] and not req["first_opened_at"]
                    and now - req["created_at"] >= timedelta(minutes=st["delay"])):
                send_alerts(req, "wa")
            last = req["reminded_at"] or req["alerted_at"] or req["created_at"]
            if st["remind"] > 0 and now - last >= timedelta(minutes=st["remind"]) and req["reminders"] < 12:
                send_alerts(req, "remind")
        except Exception as e:
            print(f"⚠️ support tick request {req.get('id')}:", e)


# ── The email to support@ and the Account Manager ─────────────────────────

def send_email(req: dict) -> bool:
    from portal_utils import send_email as _send
    import admin_accounts as M
    e = html.escape
    business = _business(req["tenant_id"])
    conn, cur = _db()
    try:
        cur.execute("""SELECT s.assigned_admin, p.name AS plan, c.first_name, c.last_name, c.email, c.phone_number
                         FROM tenants t LEFT JOIN plans p ON p.id=t.plan_id
                         LEFT JOIN am_account_state s ON s.tenant_id=t.id
                         LEFT JOIN LATERAL (SELECT * FROM customers WHERE tenant_id=t.id ORDER BY id LIMIT 1) c ON TRUE
                        WHERE t.id=%s""", (req["tenant_id"],))
        a = cur.fetchone() or {}
    finally:
        cur.close(); conn.close()
    am = a.get("assigned_admin")
    first = _first_name(req.get("person_name"))
    conv = conversation(req)
    rows = []
    for m in conv:
        who = (f"<b>{e(first)}</b> (business)" if m["k"] == "c" else '<b style="color:#5B21B6">Ask PhiXtra</b> (automatic answer)')
        bg = "#FFFFFF" if m["k"] == "c" else "#F5F3FF"
        rows.append(f'<div style="margin:0 0 8px;padding:8px 12px;border:1px solid #E4E7EC;border-radius:10px;background:{bg}">'
                    f'<div style="font-size:12px;color:#5D6673">{who} · {uk_time(m["at"])}</div>'
                    f'<div style="white-space:pre-wrap">{e(m["text"])}</div></div>')
    url = f"{PORTAL}/admin/accounts/{req['tenant_id']}#support"
    contact_line = (f"WhatsApp {e(req['contact'])} (their choice)" if req["via"] == "whatsapp"
                    else f"Call them on {e(req['contact'] or '')} (their choice)" if req["via"] == "call"
                    else f"Email {e(req['contact'] or '')} (their choice)")
    body = f"""<div style="font-family:Arial,sans-serif;font-size:14px;color:#030C18;max-width:640px">
<div style="background:#030C18;color:#fff;padding:14px 18px;border-radius:10px 10px 0 0;font-size:17px;font-weight:bold">{e(first)} asked for a person · {uk_day_time(req['created_at'])} UK</div>
<div style="border:1px solid #E4E7EC;border-top:0;border-radius:0 0 10px 10px;padding:16px 18px">
<table style="width:100%;font-size:14px;border-collapse:collapse">
<tr><td style="color:#5D6673;padding:3px 0;width:150px">Business</td><td><b>{e(business)}</b></td></tr>
<tr><td style="color:#5D6673;padding:3px 0">Person</td><td>{e(req.get('person_name') or '')}</td></tr>
<tr><td style="color:#5D6673;padding:3px 0">Contact them on</td><td>{contact_line}</td></tr>
<tr><td style="color:#5D6673;padding:3px 0">Other contact</td><td>{e(a.get('email') or '')} {e(a.get('phone_number') or '')}</td></tr>
<tr><td style="color:#5D6673;padding:3px 0">Plan</td><td>{e(a.get('plan') or 'Free')}</td></tr>
<tr><td style="color:#5D6673;padding:3px 0">Account Manager</td><td>{e(M.manager_name(am) if am else 'Nobody yet')}</td></tr>
</table>
{f'<div style="margin:14px 0;padding:10px 12px;background:#FEF3F2;border:1px solid #FECDCA;border-radius:8px"><b style="color:#B42318">Problem found on the account</b><br>{e(req["problem"])}</div>' if req.get('problem') else ''}
{f'<p><b>Their note:</b> {e(req["note"])}</p>' if req.get('note') else ''}
<p style="font-weight:bold;margin:14px 0 8px">The whole conversation</p>
{''.join(rows) or '<p>(no questions)</p>'}
<p style="margin:16px 0 6px"><a href="{url}" style="display:inline-block;background:#030C18;color:#fff;padding:11px 18px;border-radius:8px;text-decoration:none;font-weight:bold">Open their account in Admin</a></p>
<p style="font-size:12.5px;color:#5D6673">Reply from their account page in Admin, so it goes on the Timeline and the team can see it's handled. Replying to this email also reaches {e(first)}, but won't mark it handled.</p>
</div></div>"""
    subject = f"Needs a person: {business}" + (f" · {req['problem'].split('.')[0]}" if req.get("problem") else "")
    reply_to = req["contact"] if req["via"] == "email" and req.get("contact") else a.get("email")
    ok = _send(SUPPORT_EMAIL, subject, body, reply_to=reply_to)
    if am and "@" in am and am.lower() != SUPPORT_EMAIL.lower():
        _send(am, subject, body, reply_to=reply_to)
    return ok
