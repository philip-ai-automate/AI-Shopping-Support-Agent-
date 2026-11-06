"""admin_accounts.py — Admin › Accounts: the Account Manager workspace
(2026-11-06, user: "yes go ahead in the admin with option A").

Businesses that signed up but never finished setting up, followed up by a
PhiXtra Account Manager from the ADMIN (the PhiXtra portal business is left
for sales and outreach):

  * every sign-up is listed automatically (not only assigned ones), with the
    set-up step it stopped at (accounts_core.setup_steps_many), status,
    last contact and next follow-up;
  * per account: status (Not contacted → … → Set up / Lost), why they left
    (reason picklist), next follow-up date, assigned Account Manager;
  * contact with a record: WhatsApp chat sent from PhiXtra's own WhatsApp
    number (logged in wa_message_log like an Inbox reply, so the PhiXtra Inbox
    shows it too), WhatsApp call (option A: opens that chat in PhiXtra's Inbox,
    where the call button works; the call itself lands in wa_calls and shows
    here), email (sent from the platform address, reply-to the manager),
    call results and notes;
  * one timeline per account: our log + WhatsApp messages + WhatsApp calls.

Tables: am_account_state (one row per business), am_activity (the log).
No automatic messages: every contact is a person's decision.
"""
import re
from datetime import date, datetime, timedelta, timezone

import psycopg2.extras
import requests
from flask import flash, redirect, render_template, request, url_for, session

from db import get_db_connection, insert_audit_log
import accounts_core as A
from portal_address import country_name

GRAPH = "https://graph.facebook.com/v19.0"
CHECKIN_TEMPLATE = "phixtra_account_checkin"
# Body agreed 6 Nov 2026 (asks about challenges first, then offers help):
# "Hi {{1}}, this is {{2}} from PhiXtra. I noticed you started setting up your PhiXtra account but didn't
#  finish. Did anything get in the way, or was something unclear? Just reply and let me know, and I'll be
#  happy to help you complete it."

STATUSES = [
    ("not_contacted", "Not contacted"),
    ("contacted", "Contacted"),
    ("replied", "Replied"),
    ("helping", "Helping to set up"),
    ("set_up", "Set up"),
    ("lost", "Lost"),
]
STATUS_LABEL = dict(STATUSES)
REASONS = [
    ("no_time", "No time yet"),
    ("setup_unclear", "Didn't understand set-up"),
    ("no_wa_business", "No WhatsApp Business number"),
    ("no_website", "No website yet"),
    ("too_expensive", "Too expensive"),
    ("just_testing", "Just testing"),
    ("chose_other", "Chose another tool"),
    ("not_a_fit", "Not a fit"),
    ("no_reply", "No reply"),
    ("other", "Other"),
]
REASON_LABEL = dict(REASONS)
CALL_OUTCOMES = [
    ("answered", "Answered"),
    ("no_answer", "No answer"),
    ("call_back", "Asked to call back later"),
    ("wrong_number", "Wrong number"),
    ("not_interested", "Not interested"),
]
CALL_OUTCOME_LABEL = dict(CALL_OUTCOMES)

EMAIL_TEMPLATES = [
    ("help", "Need help finishing set-up?",
     "Hi {first_name},\n\nI'm {manager}, your account manager at PhiXtra. I saw you started setting up {business} "
     "but stopped at \"{stopped_at}\".\n\nCan I help you finish? It usually takes about 10 minutes, and I'm happy to "
     "do it with you on a quick call or WhatsApp.\n\nJust reply to this email with a good time.\n\nBest regards,\n{manager}\nPhiXtra"),
    ("checkin", "Checking in",
     "Hi {first_name},\n\nI'm {manager} from PhiXtra. I wanted to check in: is there anything that stopped you "
     "finishing your set-up for {business}?\n\nYour answer helps us improve, and I can help with any step.\n\n"
     "Best regards,\n{manager}\nPhiXtra"),
    ("blank", "", "Hi {first_name},\n\n\n\nBest regards,\n{manager}\nPhiXtra"),
]

_tables_ready = False


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def digits(p) -> str:
    return re.sub(r"\D", "", p or "")


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    conn, cur = _db()
    try:
        cur.execute("SELECT to_regclass('am_activity') IS NOT NULL AS ok")
        if cur.fetchone()["ok"]:
            _tables_ready = True
            return
        cur.execute("""
            CREATE TABLE IF NOT EXISTS am_account_state (
                tenant_id           INTEGER PRIMARY KEY,
                assigned_admin      VARCHAR(200),
                status              VARCHAR(20) NOT NULL DEFAULT 'not_contacted',
                reason              VARCHAR(30),
                reason_note         TEXT,
                next_follow_up      DATE,
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by          VARCHAR(200)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS am_activity (
                id              BIGSERIAL PRIMARY KEY,
                tenant_id       INTEGER NOT NULL,
                kind            VARCHAR(20) NOT NULL,
                channel         VARCHAR(20),
                outcome         VARCHAR(30),
                subject         TEXT,
                body            TEXT,
                follow_up_on    DATE,
                by_admin        VARCHAR(200),
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ok              BOOLEAN
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS am_activity_t_idx ON am_activity (tenant_id, created_at DESC)")
        conn.commit()
        _tables_ready = True
    finally:
        cur.close(); conn.close()


def admin_users() -> list:
    conn, cur = _db()
    try:
        cur.execute("SELECT username, role FROM admin_users WHERE active AND username <> 'tutorial-bot' ORDER BY username")
        return cur.fetchall() or []
    finally:
        cur.close(); conn.close()


def manager_name(username: str) -> str:
    """'d.ogbudu@phixtra.com' -> 'D Ogbudu'; 'admin@phixtra.com' -> 'Admin'."""
    if not username:
        return ""
    local = username.split("@")[0]
    parts = [p for p in re.split(r"[._\-]+", local) if p]
    return " ".join(p.capitalize() for p in parts) or username


def platform_numbers() -> list:
    pid = A.platform_tenant_id()
    if pid is None:
        return []
    conn, cur = _db()
    try:
        cur.execute("""SELECT id, phone_number_id, waba_id, display_phone_number, access_token FROM wa_tenants
                        WHERE tenant_id=%s AND active AND access_token LIKE 'EAA%%' ORDER BY id""", (pid,))
        return cur.fetchall() or []
    finally:
        cur.close(); conn.close()


# ── list ──────────────────────────────────────────────────────────────────

def rows(only_tenant=None) -> list:
    """Every signed-up business except PhiXtra's own, training, demo and
    internal test accounts (owner email on phixtra.com / profitbuyz)."""
    ensure_tables()
    pid = A.platform_tenant_id() or 0
    conn, cur = _db()
    try:
        where, params = ["t.id <> %s", "NOT COALESCE(t.is_training, FALSE)", "NOT COALESCE(t.is_demo, FALSE)",
                         "COALESCE(t.status,'') <> 'cancelled'", "c.id IS NOT NULL",
                         # PhiXtra's own demo / test / training accounts (website "Try it"
                         # demo shops, test stores, B2B/B2C demos) are not customers.
                         r"COALESCE(c.email,'') !~* '@(phixtra\.com|profitbuyz\.com|profitbuyz\.uk)$'"], [pid]
        if only_tenant is not None:
            where.append("t.id = %s"); params.append(int(only_tenant))
        cur.execute(f"""
            SELECT t.id AS tenant_id, t.name AS business, t.created_at AS signed_up, t.trial_granted_at, t.trial_ends_at,
                   t.website_platform, t.domain, COALESCE(p.name, 'Free') AS plan_name, p.slug AS plan_slug,
                   t.signup_channel, t.source_type,
                   c.id AS customer_id, c.first_name, c.last_name, c.email, c.phone_number, c.phone_verified,
                   c.email_verified, c.last_login_at, c.company_name, c.business_country, c.hear_about_us,
                   c.billing_address_line1, c.billing_city, c.billing_postcode, c.billing_country,
                   s.assigned_admin, COALESCE(s.status, 'not_contacted') AS status, s.reason, s.reason_note,
                   s.next_follow_up, s.updated_at AS state_updated
              FROM tenants t
              LEFT JOIN plans p ON p.id = t.plan_id
              LEFT JOIN LATERAL (SELECT * FROM customers WHERE tenant_id = t.id ORDER BY id LIMIT 1) c ON TRUE
              LEFT JOIN am_account_state s ON s.tenant_id = t.id
             WHERE {' AND '.join(where)}
             ORDER BY t.created_at DESC""", params)
        out = cur.fetchall() or []
        ids = [r["tenant_id"] for r in out]
        last = {}
        if ids:
            cur.execute("""SELECT DISTINCT ON (tenant_id) tenant_id, kind, channel, created_at, by_admin
                             FROM am_activity WHERE tenant_id = ANY(%s) AND kind IN ('call','whatsapp','email')
                            ORDER BY tenant_id, created_at DESC""", (ids,))
            last = {r["tenant_id"]: r for r in cur.fetchall() or []}
    finally:
        cur.close(); conn.close()
    steps = A.setup_steps_many([r["tenant_id"] for r in out])
    today = date.today()
    for r in out:
        r["setup"] = steps.get(r["tenant_id"]) or {"type": "", "steps": [], "done": 0, "total": 0, "complete": False, "stuck": None}
        r["contact"] = " ".join(x for x in [(r["first_name"] or "").strip(), (r["last_name"] or "").strip()] if x)
        r["country"] = country_name(r["business_country"]) or (r["business_country"] or "")
        r["status_label"] = STATUS_LABEL.get(r["status"], r["status"])
        if r["setup"]["complete"] and r["status"] not in ("lost",):
            r["status"], r["status_label"] = "set_up", "Set up"
        r["reason_label"] = REASON_LABEL.get(r["reason"] or "", "")
        r["last_contact"] = last.get(r["tenant_id"])
        fu = r["next_follow_up"]
        r["follow_up_due"] = bool(fu and fu <= today)
        r["follow_up_overdue"] = bool(fu and fu < today)
        r["manager"] = manager_name(r["assigned_admin"] or "")
        r["phone_digits"] = digits(r["phone_number"])
        parts = [x.strip() for x in [r["billing_address_line1"], r["billing_city"], r["billing_postcode"]] if x and x.strip()]
        if parts and r["billing_country"]:
            parts.append(country_name(r["billing_country"]) or r["billing_country"])
        r["address"] = ", ".join(parts)
        r["days_since_signup"] = (datetime.now(timezone.utc) - r["signed_up"]).days if r["signed_up"] else None
    return out


def due_count(username: str) -> int:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT COUNT(*) AS n FROM am_account_state
                        WHERE next_follow_up <= CURRENT_DATE AND status NOT IN ('set_up','lost')
                          AND (assigned_admin = %s OR assigned_admin IS NULL)""", (username,))
        return int(cur.fetchone()["n"])
    finally:
        cur.close(); conn.close()


# ── state + log ───────────────────────────────────────────────────────────

def _upsert_state(cur, tenant_id, by, **cols):
    cur.execute("INSERT INTO am_account_state (tenant_id, updated_by) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (tenant_id, by))
    if cols:
        sets = ", ".join(f"{k}=%s" for k in cols)
        cur.execute(f"UPDATE am_account_state SET {sets}, updated_at=NOW(), updated_by=%s WHERE tenant_id=%s",
                    (*cols.values(), by, tenant_id))


def log(cur, tenant_id, kind, by, channel=None, outcome=None, subject=None, body=None, follow_up_on=None, ok=True):
    cur.execute("""INSERT INTO am_activity (tenant_id, kind, channel, outcome, subject, body, follow_up_on, by_admin, ok)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, kind, channel, outcome, subject, body, follow_up_on, by, ok))
    if follow_up_on:
        _upsert_state(cur, tenant_id, by, next_follow_up=follow_up_on)


def _bump_contacted(cur, tenant_id, by):
    """First contact moves 'Not contacted' to 'Contacted'."""
    _upsert_state(cur, tenant_id, by)
    cur.execute("""UPDATE am_account_state SET status='contacted' WHERE tenant_id=%s AND status='not_contacted'""",
                (tenant_id,))


def timeline(tenant_id: int, phone: str) -> list:
    """Our log + WhatsApp messages + WhatsApp calls with this customer on
    PhiXtra's own numbers, newest first."""
    pid = A.platform_tenant_id()
    d = digits(phone)
    items = []
    conn, cur = _db()
    try:
        cur.execute("""SELECT * FROM am_activity WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 300""", (tenant_id,))
        for r in cur.fetchall() or []:
            items.append({"at": r["created_at"], "src": "log", **r})
        cur.execute("""SELECT created_at, author_label, note, follow_up_on FROM platform_account_notes
                        WHERE tenant_id=%s AND author_key IS NOT NULL AND NOT EXISTS (
                              SELECT 1 FROM am_activity a WHERE a.tenant_id=%s AND a.kind='note'
                                AND a.body = platform_account_notes.note AND a.created_at = platform_account_notes.created_at)
                        ORDER BY created_at DESC LIMIT 100""", (tenant_id, tenant_id))
        for r in cur.fetchall() or []:
            items.append({"at": r["created_at"], "src": "log", "kind": "note", "body": r["note"],
                          "by_admin": r["author_label"], "follow_up_on": r["follow_up_on"], "ok": True})
        if pid and len(d) >= 8:
            cur.execute("""SELECT created_at, direction, content, message_type, sent_by_label FROM wa_message_log
                            WHERE tenant_id=%s AND regexp_replace(customer_phone,'\\D','','g')=%s
                              AND NOT COALESCE(is_historical, FALSE)
                            ORDER BY created_at DESC LIMIT 200""", (pid, d))
            for r in cur.fetchall() or []:
                if r["message_type"] == "call":
                    continue
                items.append({"at": r["created_at"], "src": "wa", **r})
            cur.execute("""SELECT created_at, direction, status, duration, answered_by_name, started_by_name, missed_reason
                             FROM wa_calls WHERE tenant_id=%s AND regexp_replace(customer_phone,'\\D','','g')=%s
                            ORDER BY created_at DESC LIMIT 100""", (pid, d))
            for r in cur.fetchall() or []:
                items.append({"at": r["created_at"], "src": "wacall", **r})
    finally:
        cur.close(); conn.close()
    items.sort(key=lambda x: x["at"], reverse=True)
    return items


def wa_window_open(phone: str) -> bool:
    """WhatsApp only allows free text within 24 h of the customer's last message."""
    pid = A.platform_tenant_id()
    d = digits(phone)
    if not pid or len(d) < 8:
        return False
    conn, cur = _db()
    try:
        cur.execute("""SELECT 1 FROM wa_message_log WHERE tenant_id=%s AND direction='inbound'
                         AND regexp_replace(customer_phone,'\\D','','g')=%s AND created_at > NOW() - INTERVAL '24 hours'
                        LIMIT 1""", (pid, d))
        return cur.fetchone() is not None
    finally:
        cur.close(); conn.close()


_tpl_cache = {"at": None, "ok": False}


def checkin_template_ready() -> bool:
    """Is the WhatsApp check-in template approved on PhiXtra's number? (cached 10 min)"""
    now = datetime.now(timezone.utc)
    if _tpl_cache["at"] and now - _tpl_cache["at"] < timedelta(minutes=10):
        return _tpl_cache["ok"]
    ok = False
    nums = platform_numbers()
    if nums:
        try:
            r = requests.get(f"{GRAPH}/{nums[0]['waba_id']}/message_templates",
                             params={"name": CHECKIN_TEMPLATE, "fields": "name,status"},
                             headers={"Authorization": f"Bearer {nums[0]['access_token']}"}, timeout=8).json()
            ok = any(t.get("name") == CHECKIN_TEMPLATE and t.get("status") == "APPROVED" for t in r.get("data", []))
        except Exception as e:
            print("⚠️ admin_accounts template check:", e)
    _tpl_cache.update(at=now, ok=ok)
    return ok


def send_whatsapp(tenant_id: int, phone: str, text: str, by: str, use_template=False, first_name="", manager=""):
    """Send from PhiXtra's first WhatsApp number; log it where the Inbox reads it too."""
    nums = platform_numbers()
    if not nums:
        return False, "PhiXtra's WhatsApp number isn't connected."
    num = nums[0]
    to = digits(phone)
    if len(to) < 8:
        return False, "This customer has no valid phone number."
    if use_template:
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                   "template": {"name": CHECKIN_TEMPLATE, "language": {"code": "en"},
                                "components": [{"type": "body", "parameters": [
                                    {"type": "text", "text": first_name or "there"},
                                    {"type": "text", "text": manager or "PhiXtra"}]}]}}
        logged = (f"Hi {first_name or 'there'}, this is {manager or 'PhiXtra'} from PhiXtra. I noticed you started "
                  "setting up your PhiXtra account but didn't finish. Did anything get in the way, or was something "
                  "unclear? Just reply and let me know, and I'll be happy to help you complete it.")
    else:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
                   "type": "text", "text": {"body": text}}
        logged = text
    try:
        r = requests.post(f"{GRAPH}/{num['phone_number_id']}/messages", json=payload,
                          headers={"Authorization": f"Bearer {num['access_token']}"}, timeout=15)
        ok = r.status_code == 200
        err = "" if ok else ((r.json().get("error") or {}).get("message") or f"HTTP {r.status_code}")
    except Exception as e:
        ok, err = False, str(e)
    pid = A.platform_tenant_id()
    conn, cur = _db()
    try:
        if ok and pid:
            cur.execute("""INSERT INTO wa_message_log (tenant_id, phone_number_id, customer_phone, direction, content,
                                                       message_type, sent_by_label)
                           VALUES (%s,%s,%s,'outbound',%s,'agent_reply',%s)""",
                        (pid, num["phone_number_id"], to, logged, manager_name(by) + " (Account Manager)"))
        log(cur, tenant_id, "whatsapp", by, channel="whatsapp", body=logged, ok=ok,
            outcome=None if ok else "failed")
        if ok:
            _bump_contacted(cur, tenant_id, by)
        conn.commit()
    finally:
        cur.close(); conn.close()
    return ok, err


def fill(text: str, a: dict, by: str) -> str:
    vals = {"first_name": (a.get("first_name") or "").strip() or "there",
            "business": a.get("business") or "your business",
            "stopped_at": (a.get("setup") or {}).get("stuck") or "set-up",
            "manager": manager_name(by)}
    for k, v in vals.items():
        text = text.replace("{" + k + "}", v)
    return text


# ── routes (registered on the admin blueprint) ────────────────────────────

def register(bp, require_admin, admin_user):

    def _gate(action="view"):
        return require_admin("accounts", action)

    @bp.app_context_processor
    def _am_badge():
        def am_due_count():
            try:
                if not session.get("portal_admin_logged_in"):
                    return 0
                return due_count(admin_user())
            except Exception:
                return 0
        return {"am_due_count": am_due_count}

    @bp.route("/accounts")
    def am_accounts():
        r = _gate()
        if r: return r
        me = admin_user()
        tab = request.args.get("tab") or "todo"
        who = request.args.get("who") or "all"
        all_rows = rows()
        today = date.today()
        def keep(x):
            if who == "me" and x["assigned_admin"] != me: return False
            if who == "none" and x["assigned_admin"]: return False
            if tab == "todo": return x["status"] not in ("set_up", "lost")
            if tab == "due": return x["follow_up_due"] and x["status"] not in ("set_up", "lost")
            if tab == "set_up": return x["status"] == "set_up"
            if tab == "lost": return x["status"] == "lost"
            return True
        shown = [x for x in all_rows if keep(x)]
        if tab == "due":
            shown.sort(key=lambda x: x["next_follow_up"] or today)
        counts = {
            "todo": sum(1 for x in all_rows if x["status"] not in ("set_up", "lost")),
            "due": sum(1 for x in all_rows if x["follow_up_due"] and x["status"] not in ("set_up", "lost")),
            "set_up": sum(1 for x in all_rows if x["status"] == "set_up"),
            "lost": sum(1 for x in all_rows if x["status"] == "lost"),
            "all": len(all_rows),
        }
        reasons = {}
        for x in all_rows:
            if x["status"] == "lost" and x["reason"]:
                reasons[x["reason_label"]] = reasons.get(x["reason_label"], 0) + 1
        stuck = {}
        for x in all_rows:
            if x["status"] not in ("set_up",) and x["setup"].get("stuck"):
                stuck[x["setup"]["stuck"]] = stuck.get(x["setup"]["stuck"], 0) + 1
        return render_template("portal/admin_accounts.html", rows=shown, tab=tab, who=who, counts=counts,
                               me=me, today=today, reasons=sorted(reasons.items(), key=lambda kv: -kv[1]),
                               stuck=sorted(stuck.items(), key=lambda kv: -kv[1]))

    @bp.route("/accounts/<int:tenant_id>")
    def am_account(tenant_id):
        r = _gate()
        if r: return r
        found = rows(only_tenant=tenant_id)
        if not found:
            flash("Account not found.", "warning")
            return redirect(url_for("portal_admin.am_accounts"))
        a = found[0]
        me = admin_user()
        tpl = request.args.get("tpl") or "help"
        etpl = next((t for t in EMAIL_TEMPLATES if t[0] == tpl), EMAIL_TEMPLATES[0])
        import os as _os
        portal = (_os.getenv("PORTAL_BASE_URL") or "https://portal.phixtra.com").rstrip("/")
        window = wa_window_open(a["phone_number"])
        return render_template(
            "portal/admin_account.html", a=a, items=timeline(tenant_id, a["phone_number"]), me=me,
            statuses=STATUSES, reasons=REASONS, outcomes=CALL_OUTCOMES, admins=admin_users(),
            email_templates=EMAIL_TEMPLATES, etpl=etpl, email_subject=fill(etpl[1], a, me),
            email_body=fill(etpl[2], a, me),
            reply_to=(me if "@" in me else "support@phixtra.com"),
            wa_window=window, checkin_ready=(not window and checkin_template_ready()),
            wa_numbers=platform_numbers(), inbox_url=f"{portal}/inbox?phone={a['phone_digits']}",
            today=date.today(), can_modify=(require_admin("accounts", "modify") is None),
            call_outcome_label=CALL_OUTCOME_LABEL, reason_label=REASON_LABEL, status_label=STATUS_LABEL,
            manager_name=manager_name)

    def _back(tid):
        return redirect(url_for("portal_admin.am_account", tenant_id=tid) + "#log")

    def _date(v):
        try:
            return date.fromisoformat(v) if v else None
        except ValueError:
            return None

    @bp.route("/accounts/<int:tenant_id>/state", methods=["POST"])
    def am_state(tenant_id):
        r = _gate("modify")
        if r: return r
        me = admin_user()
        f = request.form
        status = f.get("status") if f.get("status") in STATUS_LABEL else None
        reason = f.get("reason") if f.get("reason") in REASON_LABEL else None
        assigned = (f.get("assigned_admin") or "").strip() or None
        valid_admins = {u["username"] for u in admin_users()}
        if assigned and assigned not in valid_admins:
            assigned = None
        fu = _date(f.get("next_follow_up"))
        conn, cur = _db()
        try:
            ensure_tables()
            cur.execute("SELECT * FROM am_account_state WHERE tenant_id=%s", (tenant_id,))
            old = cur.fetchone() or {}
            _upsert_state(cur, tenant_id, me, status=status or old.get("status") or "not_contacted",
                          reason=reason, reason_note=(f.get("reason_note") or "").strip()[:500] or None,
                          assigned_admin=assigned, next_follow_up=fu)
            changes = []
            if status and status != (old.get("status") or "not_contacted"):
                changes.append(f"Status: {STATUS_LABEL[status]}")
            if reason != old.get("reason"):
                changes.append(f"Why: {REASON_LABEL.get(reason or '', 'cleared')}")
            if assigned != old.get("assigned_admin"):
                changes.append(f"Assigned to {manager_name(assigned) if assigned else 'nobody'}")
            if fu != old.get("next_follow_up"):
                changes.append(f"Next follow-up {fu:%-d %b %Y}" if fu else "Follow-up cleared")
            if changes:
                log(cur, tenant_id, "status", me, body=" · ".join(changes))
            conn.commit()
        finally:
            cur.close(); conn.close()
        insert_audit_log(admin_username=me, action="am_account_state", tenant_id=tenant_id,
                         details={"status": status, "reason": reason, "assigned": assigned,
                                  "follow_up": fu.isoformat() if fu else None})
        flash("Saved.", "success")
        return _back(tenant_id)

    @bp.route("/accounts/<int:tenant_id>/note", methods=["POST"])
    def am_note(tenant_id):
        r = _gate("modify")
        if r: return r
        me = admin_user()
        text = (request.form.get("note") or "").strip()[:3000]
        fu = _date(request.form.get("follow_up_on"))
        if not text and not fu:
            flash("Write a note or pick a follow-up date.", "warning")
            return _back(tenant_id)
        ensure_tables()
        conn, cur = _db()
        try:
            log(cur, tenant_id, "note", me, body=text or None, follow_up_on=fu)
            conn.commit()
        finally:
            cur.close(); conn.close()
        flash("Note saved.", "success")
        return _back(tenant_id)

    @bp.route("/accounts/<int:tenant_id>/call", methods=["POST"])
    def am_call(tenant_id):
        r = _gate("modify")
        if r: return r
        me = admin_user()
        f = request.form
        outcome = f.get("outcome") if f.get("outcome") in CALL_OUTCOME_LABEL else None
        if not outcome:
            flash("Pick how the call went.", "warning")
            return _back(tenant_id)
        channel = "whatsapp_call" if f.get("channel") == "whatsapp_call" else "phone"
        ensure_tables()
        conn, cur = _db()
        try:
            log(cur, tenant_id, "call", me, channel=channel, outcome=outcome,
                body=(f.get("note") or "").strip()[:3000] or None, follow_up_on=_date(f.get("follow_up_on")))
            _bump_contacted(cur, tenant_id, me)
            if outcome == "answered":
                cur.execute("UPDATE am_account_state SET status='replied' WHERE tenant_id=%s AND status='contacted'",
                            (tenant_id,))
            conn.commit()
        finally:
            cur.close(); conn.close()
        flash("Call result saved.", "success")
        return _back(tenant_id)

    @bp.route("/accounts/<int:tenant_id>/whatsapp", methods=["POST"])
    def am_whatsapp(tenant_id):
        r = _gate("modify")
        if r: return r
        me = admin_user()
        found = rows(only_tenant=tenant_id)
        if not found:
            return redirect(url_for("portal_admin.am_accounts"))
        a = found[0]
        use_tpl = request.form.get("mode") == "template"
        text = (request.form.get("text") or "").strip()[:4000]
        if not use_tpl and not text:
            flash("Write the message first.", "warning")
            return _back(tenant_id)
        if not use_tpl and not wa_window_open(a["phone_number"]):
            flash("WhatsApp only allows a free message within 24 hours of the customer's last message. "
                  "Use the check-in template, email or a WhatsApp call.", "warning")
            return _back(tenant_id)
        if use_tpl and not checkin_template_ready():
            flash("The WhatsApp check-in template isn't approved by Meta yet.", "warning")
            return _back(tenant_id)
        ok, err = send_whatsapp(tenant_id, a["phone_number"], text, me, use_template=use_tpl,
                                first_name=(a["first_name"] or "").strip(), manager=manager_name(me))
        insert_audit_log(admin_username=me, action="am_whatsapp_sent" if ok else "am_whatsapp_failed",
                         tenant_id=tenant_id, details={"template": use_tpl, "error": err or None})
        flash("WhatsApp message sent." if ok else f"WhatsApp didn't send: {err}", "success" if ok else "danger")
        return _back(tenant_id)

    @bp.route("/accounts/<int:tenant_id>/email", methods=["POST"])
    def am_email(tenant_id):
        r = _gate("modify")
        if r: return r
        me = admin_user()
        found = rows(only_tenant=tenant_id)
        if not found:
            return redirect(url_for("portal_admin.am_accounts"))
        a = found[0]
        subject = (request.form.get("subject") or "").strip()[:200]
        body = (request.form.get("body") or "").strip()[:8000]
        reply_to = (request.form.get("reply_to") or "").strip()[:200]
        if not (subject and body and a["email"]):
            flash("Subject and message are needed, and the customer needs an email address.", "warning")
            return _back(tenant_id)
        import html as _h
        from portal_utils import send_email
        html_body = ("<div style=\"font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;font-size:15px;"
                     "line-height:1.6;color:#111827\">" + _h.escape(body).replace("\n", "<br>") + "</div>")
        ok = send_email(a["email"], subject, html_body, body, reply_to=reply_to if "@" in reply_to else None)
        ensure_tables()
        conn, cur = _db()
        try:
            log(cur, tenant_id, "email", me, channel="email", subject=subject, body=body, ok=ok,
                outcome=None if ok else "failed", follow_up_on=_date(request.form.get("follow_up_on")))
            if ok:
                _bump_contacted(cur, tenant_id, me)
            conn.commit()
        finally:
            cur.close(); conn.close()
        insert_audit_log(admin_username=me, action="am_email_sent" if ok else "am_email_failed", tenant_id=tenant_id,
                         details={"to": a["email"], "subject": subject})
        flash(f"Email sent to {a['email']}." if ok else "The email didn't send. Try again.", "success" if ok else "danger")
        return _back(tenant_id)
