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
    shows it too), WhatsApp call made right on the account page in the
    browser (register_calls below; lands in wa_calls and shows here), email (sent from the platform address, reply-to the manager),
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
CHECKIN_TEMPLATE = "phixtra_setup_checkin"
# Body (submitted 6 Nov 2026, replaces phixtra_account_checkin which named the
# sender with a variable and would have said "Admin"):
# "Hi {{1}}, this is Kelvin from PhiXtra. I noticed you started setting up your PhiXtra account but didn't
#  finish. Did anything get in the way, or was something unclear? Just reply and let me know, and I'll be
#  happy to help you complete it."
# The name customers see from the Account Manager (user: "call it Kelvin").
AM_NAME = "Kelvin"


# New check-in (6 Nov 2026, user-approved wording): header, 2 parameters
# (first name, set-up step they stopped at), footer and 3 quick-reply buttons.
# Button taps are handled by whatsapp-gateway/am_flow.py. Used as soon as Meta
# approves it; until then the old phixtra_setup_checkin is used.
HELP_TEMPLATE = "phixtra_setup_help"
HELP_HEADER = "Let's get your PhiXtra account live"
HELP_BODY = ("Hi {{1}},\n\n"
             "I'm Kelvin, your Account Manager at PhiXtra.\n\n"
             "Thank you for signing up. I'd really like to see you up and running, with every customer enquiry "
             "answered and followed up until it becomes a sale.\n\n"
             "I can see your set-up stopped at *{{2}}*. Did something get in the way, or was a step unclear?\n\n"
             "Tap an option below or reply here, and I'll personally help you finish it.")
HELP_FOOTER = "PhiXtra · Enquiry-to-Revenue"
HELP_BUTTONS = [("am_stuck", "I got stuck"), ("am_question", "I have a question"), ("am_later", "Not right now")]
# Set-up step titles (accounts_core.setup_steps_many) as the customer reads them.
STEP_PHRASE = {"Connect WhatsApp": "Connect WhatsApp", "Products": "Add products", "AI trial": "Start AI trial",
               "Connect website": "Connect website", "Sync website": "Sync website"}


def step_phrase(stuck) -> str:
    return STEP_PHRASE.get(stuck or "", stuck or "Getting started")


def help_template_definition() -> dict:
    """What is submitted to Meta for HELP_TEMPLATE."""
    return {
        "name": HELP_TEMPLATE, "language": "en", "category": "UTILITY",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": HELP_HEADER},
            {"type": "BODY", "text": HELP_BODY, "example": {"body_text": [["Laura", "Connect WhatsApp"]]}},
            {"type": "FOOTER", "text": HELP_FOOTER},
            {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": t} for _, t in HELP_BUTTONS]},
        ],
    }


# All Account Manager check-in templates (2026-11-06). The picker on the
# account page / Inbox / bulk bar offers the APPROVED ones, Utility first.
# Only these: other templates on PhiXtra's number (login codes, staff
# alerts, sales campaigns) must never be sent from Accounts.
STATUS_TEMPLATE = "phixtra_setup_status"
AM_TEMPLATES = [
    {"name": STATUS_TEMPLATE, "label": "Account set-up status", "header": "Your PhiXtra account set-up",
     "body": ("Hi {{1}},\n\nI'm Kelvin, your Account Manager at PhiXtra.\n\n"
              "Your account set-up isn't complete yet. It stopped at this step: *{{2}}*\n\n"
              "Did something get in the way, or was a step unclear? Tap an option below or reply here, and I'll help you complete it."),
     "footer": HELP_FOOTER, "buttons": True},
    {"name": HELP_TEMPLATE, "label": "Let's get your account live", "header": HELP_HEADER, "body": HELP_BODY,
     "footer": HELP_FOOTER, "buttons": True},
    {"name": CHECKIN_TEMPLATE, "label": "Plain check-in (no buttons)", "header": None,
     "body": ("Hi {{1}}, this is Kelvin from PhiXtra. I noticed you started setting up your PhiXtra account but didn't "
              "finish. Did anything get in the way, or was something unclear? Just reply and let me know, and I'll be "
              "happy to help you complete it."),
     "footer": None, "buttons": False},
]
AM_TPL = {t["name"]: t for t in AM_TEMPLATES}


def _fill(t, first_name, stuck):
    return t["body"].replace("{{1}}", first_name or "there").replace("{{2}}", step_phrase(stuck))


def _tpl(name=None):
    return AM_TPL.get(name or "") or AM_TPL.get(active_template() or "") or AM_TEMPLATES[-1]


def checkin_text(first_name: str, stuck=None, name=None) -> str:
    """The check-in message as sent (and logged), filled in."""
    t = _tpl(name)
    out = _fill(t, first_name, stuck)
    if t["header"]:
        out = f"*{t['header']}*\n\n" + out
    if t["footer"]:
        out += f"\n\n{t['footer']}"
    if t["buttons"]:
        out += "\n" + "  ".join(f"[{x}]" for _, x in HELP_BUTTONS)
    return out


def checkin_look(first_name: str, stuck=None, name=None) -> dict:
    """Preview pieces: header / text / footer / buttons."""
    t = _tpl(name)
    return {"header": t["header"], "footer": t["footer"], "text": _fill(t, first_name, stuck),
            "buttons": [x for _, x in HELP_BUTTONS] if t["buttons"] else []}


def templates_for(first_name: str, stuck=None) -> list:
    """The picker: every Account Manager template with its live Meta status
    and category, approved Utility first, then approved Marketing, then waiting."""
    st = template_statuses()
    out = []
    for t in AM_TEMPLATES:
        s = st.get(t["name"])
        if not s or s["status"] in ("DELETED", "DISABLED"):
            continue
        look = checkin_look(first_name, stuck, t["name"])
        out.append({"name": t["name"], "label": t["label"], "status": s["status"], "category": s["category"],
                    "approved": s["status"] == "APPROVED", "look": look,
                    "first_line": " ".join(look["text"].split())[:110]})
    rank = lambda x: (0 if x["approved"] else 1, 0 if x["category"] == "UTILITY" else 1)
    return sorted(out, key=rank)


def call_ask_text(first_name: str) -> str:
    """WhatsApp's "Can we call you?" request text (also what the preview shows)."""
    return ((f"Hi {first_name}, " if first_name else "Hi, ") + f"this is {AM_NAME} from PhiXtra. "
            "Is it OK if I call you on WhatsApp about your PhiXtra account?")

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


# ── Account Manager settings (2026-11-06: "must not be hard coded") ─────────
# Stored in am_settings (key/value), edited at Admin › Accounts › Settings.
SETTING_DEFAULTS = {
    "alerts_on": "1",            # email the Account Manager when a customer replies
    "alert_every_min": "2",      # how often to check for new replies
    "alert_window_hours": "2",   # only replies newer than this are alerted
    "alert_fallback": "",        # comma list of admin logins for unassigned accounts; empty = every admin login that is an email
    "later_days": "14",          # "Not right now" button → follow up after this many days
    "am_number": "",             # phone_number_id Account Managers send / call from; empty = PhiXtra's first number
}


def _has_table(cur, name) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (name,))
    return bool(cur.fetchone()["ok"])


def get_settings() -> dict:
    conn, cur = _db()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS am_settings (key VARCHAR(40) PRIMARY KEY, value TEXT NOT NULL,
                         updated_by VARCHAR(190), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.commit()
        cur.execute("SELECT key, value FROM am_settings")
        out = dict(SETTING_DEFAULTS)
        out.update({r["key"]: r["value"] for r in cur.fetchall() or []})
        return out
    finally:
        cur.close(); conn.close()


def save_settings(vals: dict, by: str):
    get_settings()
    conn, cur = _db()
    try:
        for k, v in vals.items():
            if k in SETTING_DEFAULTS or k == "alert_last_run":
                cur.execute("""INSERT INTO am_settings (key, value, updated_by) VALUES (%s,%s,%s)
                               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by,
                                                               updated_at=NOW()""", (k, str(v), by))
        conn.commit()
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


def am_number():
    """The PhiXtra WhatsApp number Account Managers message and call from
    (Admin › Accounts › Settings), else PhiXtra's first number."""
    nums = platform_numbers()
    if not nums:
        return None
    try:
        want = get_settings().get("am_number") or ""
    except Exception:
        want = ""
    return next((n for n in nums if n["phone_number_id"] == want), nums[0])


def am_numbers() -> list:
    n = am_number()
    return [n] if n else []


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


# ── Dashboard (2026-11-06) ────────────────────────────────────────────────

SOURCE_LABEL = {"google": "Google", "other": "Other", "email_advert": "Email advert", "whatsapp_advert": "WhatsApp advert",
                "referral": "Referral", "facebook": "Facebook", "sales_ambassador": "Sales ambassador",
                "linkedin": "LinkedIn", "instagram": "Instagram", "youtube": "YouTube", "tiktok": "TikTok"}
STEP_SHORT = {"Products": "Add products", "AI trial": "Start AI trial"}


def _d(x):
    return x.date() if isinstance(x, datetime) else x


def is_paid_trial_ending(x, days=7) -> bool:
    """A paid-plan trial that ends within `days` and isn't set up yet."""
    end = _d(x.get("trial_ends_at"))
    return bool(end and x.get("plan_slug") not in A.FREE_PLAN_SLUGS and x["status"] not in ("set_up", "lost")
                and date.today() <= end <= date.today() + timedelta(days=days))


def _step_done(x, *titles) -> bool:
    steps = (x.get("setup") or {}).get("steps") or []
    has = [s for s in steps if s["title"] in titles]
    return all(s["done"] for s in has) if has else True


def priority(x, reply_ids=()):
    """(score, [(tag, kind)]) — how urgently an Account Manager should
    contact this account. Shared by the dashboard and the list."""
    today = date.today()
    now = datetime.now(timezone.utc)
    sc, tags = 0, []
    end = _d(x.get("trial_ends_at"))
    if x["tenant_id"] in reply_ids:
        sc += 100; tags.append(("Replied: waiting for you", "good"))
    if is_paid_trial_ending(x):
        left = (end - today).days
        sc += 80 - left * 5
        tags.append((f"{x['plan_name'].split(' ·')[0]} trial ends " + ("today" if left == 0 else "tomorrow" if left == 1 else f"{end.day} {end:%b}"), "paid"))
    if x["follow_up_overdue"]:
        sc += 50; tags.append((f"Follow-up overdue ({x['next_follow_up'].day} {x['next_follow_up']:%b})", "warn"))
    elif x["follow_up_due"]:
        sc += 45; tags.append(("Follow-up due today", "warn"))
    if x["last_login_at"] and (now - x["last_login_at"]).days < 7:
        sc += 20; tags.append((f"Logged in {x['last_login_at'].day} {x['last_login_at']:%b}", "good"))
    if x["status"] == "not_contacted":
        d = x["days_since_signup"] or 0
        sc += 10 + min(d, 30) / 3
        if d < 7:
            sc += 15
    if x["setup"].get("stuck"):
        tags.append(("Stuck: " + STEP_SHORT.get(x["setup"]["stuck"], x["setup"]["stuck"]), "warn"))
    if x["status"] == "not_contacted":
        d = x["days_since_signup"] or 0
        tags.append((f"Never contacted · {d} day{'s' if d != 1 else ''}", "plain"))
    if len(x["phone_digits"] or "") < 8:
        sc -= 15
    return sc, tags[:3]


def dashboard(me: str, who: str = "all", period: int = 30) -> dict:
    """Everything the Account Manager dashboard shows, from real data."""
    today = date.today()
    now = datetime.now(timezone.utc)
    R = rows()
    if who == "me":
        R = [x for x in R if x["assigned_admin"] == me]
    elif who == "none":
        R = [x for x in R if not x["assigned_admin"]]
    elif who != "all":
        R = [x for x in R if x["assigned_admin"] == who]
    total = len(R)
    open_ = [x for x in R if x["status"] not in ("set_up", "lost")]
    pct = lambda n, d: round(n * 100 / d) if d else 0
    ids = {x["tenant_id"] for x in R}

    convs = [c for c in conversations() if c["tenant_id"] in ids]
    replies = [c for c in convs if c["needs_reply"] and c["status"] != "lost"]

    due = [x for x in open_ if x["follow_up_due"]]
    overdue = [x for x in due if x["follow_up_overdue"]]
    never = [x for x in R if x["status"] == "not_contacted"]
    never_week = [x for x in never if x["days_since_signup"] is not None and x["days_since_signup"] < 7]
    trials = [x for x in R if is_paid_trial_ending(x)]
    complete = [x for x in R if x["setup"]["complete"]]
    contacted = [x for x in R if x["status"] != "not_contacted"]
    active7 = [x for x in R if x["last_login_at"] and (now - x["last_login_at"]).days < 7]
    nologin = [x for x in R if not x["last_login_at"]]
    new_p = [x for x in R if x["days_since_signup"] is not None and x["days_since_signup"] < period]

    connected = [x for x in R if _step_done(x, "Connect WhatsApp", "Connect website")]
    content = [x for x in connected if _step_done(x, "Products", "Sync website")]
    funnel = [("Signed up", total), ("Connected WhatsApp or website", len(connected)),
              ("Added products / read website", len(content)), ("Finished set-up", len(complete))]
    funnel = [{"label": l, "n": n, "pct": pct(n, total)} for l, n in funnel]

    stuck = {}
    for x in open_:
        s = x["setup"].get("stuck")
        if s:
            s = STEP_SHORT.get(s, s)
            stuck[s] = stuck.get(s, 0) + 1
    stuck = sorted(stuck.items(), key=lambda kv: -kv[1])

    weeks = []
    for w in range(7, -1, -1):
        start = today - timedelta(days=w * 7 + 6)
        end = today - timedelta(days=w * 7)
        inw = [x for x in R if x["signed_up"] and start <= _d(x["signed_up"]) <= end]
        weeks.append({"label": f"{start.day} {start:%b}", "n": len(inw), "done": sum(1 for x in inw if x["setup"]["complete"])})
    wmax = max([w["n"] for w in weeks] + [1])

    reply_ids = {c["tenant_id"] for c in replies}
    ranked = []
    for x in open_:
        sc, tags = priority(x, reply_ids)
        ranked.append(dict(x, score=sc, tags=tags))
    ranked.sort(key=lambda x: -x["score"])

    team = {}
    for x in open_:
        k = x["assigned_admin"] or ""
        t = team.setdefault(k, {"username": k, "name": manager_name(k) if k else "Unassigned", "n": 0, "contacted": 0, "due": 0})
        t["n"] += 1
        t["contacted"] += x["status"] != "not_contacted"
        t["due"] += bool(x["follow_up_due"])
    team = sorted(team.values(), key=lambda t: (t["username"] == "", -t["n"]))

    reasons = {}
    for x in R:
        if x["reason"]:
            k = x["reason_note"] if (x["reason"] == "setup_unclear" and (x["reason_note"] or "").startswith("Stuck at:")) else x["reason_label"]
            reasons[k] = reasons.get(k, 0) + 1
    sources = {}
    for x in R:
        k = SOURCE_LABEL.get(x["hear_about_us"] or "", (x["hear_about_us"] or "Not given").replace("_", " ").capitalize())
        sources[k] = sources.get(k, 0) + 1
    sources = sorted(sources.items(), key=lambda kv: -kv[1])

    return {
        "total": total, "replies": len(replies), "due": len(due), "overdue": len(overdue),
        "never": len(never), "never_week": len(never_week), "trials": len(trials),
        "trials_not_setup": sum(1 for x in trials if not x["setup"]["complete"]),
        "kpis": [
            {"label": "New sign-ups", "value": len(new_p), "sub": f"last {period} days"},
            {"label": "Finished set-up", "value": f"{len(complete)} of {total}", "sub": f"{pct(len(complete), total)}% of all sign-ups"},
            {"label": "Contacted by us", "value": f"{len(contacted)} of {total}", "sub": f"{pct(len(contacted), total)}% · {len(never)} still waiting"},
            {"label": "Active this week", "value": len(active7), "sub": "logged in, last 7 days"},
            {"label": "Never logged in", "value": len(nologin), "sub": f"{pct(len(nologin), total)}% after sign-up"},
        ],
        "funnel": funnel, "stuck": stuck, "stuck_max": max([n for _, n in stuck] + [1]),
        "weeks": weeks, "wmax": wmax, "w_signups": sum(w["n"] for w in weeks), "w_done": sum(w["done"] for w in weeks),
        "first": ranked[:6], "team": team,
        "reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:6],
        "sources": sources, "src_max": max([n for _, n in sources] + [1]),
        "nophone": sum(1 for x in open_ if len(x["phone_digits"] or "") < 8),
        "nologin_open": sum(1 for x in open_ if not x["last_login_at"]),
    }


# ── All accounts list (2026-11-06): views + dropdown filters with live counts ─

LIST_VIEWS = [("priority", "Priority"), ("due", "Follow-ups due"), ("new", "Never contacted"), ("trials", "Paid trials"),
              ("stuck_wa", "Stuck at Connect WhatsApp"), ("week", "New this week"), ("none", "Unassigned"),
              ("set_up", "Set up"), ("lost", "Lost"), ("all", "All")]
HIDDEN_VIEWS = {"nophone": "No phone number", "nologin": "Never logged in"}
OLD_TABS = {"todo": "priority"}
ACT_OPTS = [("any", "Any time"), ("new7", "Signed up last 7 days"), ("new30", "Signed up last 30 days"),
            ("login7", "Logged in last 7 days"), ("never", "Never logged in")]
FU_OPTS = [("any", "Any"), ("overdue", "Overdue"), ("today", "Due today"), ("week", "Next 7 days"), ("none", "None set")]
SORTS = [("priority", "Priority"), ("newest", "Newest sign-up"), ("waiting", "Oldest waiting"),
         ("followup", "Follow-up date"), ("login", "Last log-in")]


def _in_view(x, v):
    open_ = x["status"] not in ("set_up", "lost")
    if v == "priority": return open_
    if v == "due": return open_ and x["follow_up_due"]
    if v == "new": return x["status"] == "not_contacted"
    if v == "trials": return is_paid_trial_ending(x)
    if v == "stuck_wa": return open_ and x["setup"].get("stuck") == "Connect WhatsApp"
    if v == "week": return x["days_since_signup"] is not None and x["days_since_signup"] < 7
    if v == "none": return open_ and not x["assigned_admin"]
    if v == "set_up": return x["status"] == "set_up"
    if v == "lost": return x["status"] == "lost"
    if v == "nophone": return open_ and len(x["phone_digits"] or "") < 8
    if v == "nologin": return open_ and not x["last_login_at"]
    return True


def _facets(x, me):
    """The value(s) of each filter for one account."""
    now = datetime.now(timezone.utc)
    today = date.today()
    d = x["days_since_signup"]
    act = {"any"}
    if d is not None and d < 7: act.add("new7")
    if d is not None and d < 30: act.add("new30")
    if x["last_login_at"] and (now - x["last_login_at"]).days < 7: act.add("login7")
    if not x["last_login_at"]: act.add("never")
    fu = {"any"}
    n = x["next_follow_up"]
    if not n: fu.add("none")
    elif n < today: fu.add("overdue")
    elif n == today: fu.add("today")
    elif n <= today + timedelta(days=7): fu.add("week")
    mgr = {"all", x["assigned_admin"] or "none"}
    if x["assigned_admin"] == me: mgr.add("me")
    stuck = x["setup"].get("stuck")
    return {
        "mgr": mgr, "act": act, "fu": fu,
        "st": {x["status"]},
        "stop": {STEP_SHORT.get(stuck, stuck)} if stuck and not x["setup"]["complete"] else {"All steps done"},
        "plan": {x["plan_name"]},
        "ctry": {x["country"] or "Not given"},
        "src": {SOURCE_LABEL.get(x["hear_about_us"] or "", (x["hear_about_us"] or "Not given").replace("_", " ").capitalize())},
    }


def next_step(x, reply_ids):
    """(text, kind) shown in the Next step column."""
    today = date.today()
    if x["status"] in ("set_up", "lost"):
        return ("—", "plain")
    if x["tenant_id"] in reply_ids:
        return ("Replied: answer now", "due")
    if is_paid_trial_ending(x):
        end = _d(x["trial_ends_at"]); left = (end - today).days
        return (("Trial ends today" if left == 0 else "Trial ends tomorrow" if left == 1 else f"Trial ends {end:%a} {end.day} {end:%b}") + ": call", "due" if left <= 1 else "todo")
    n = x["next_follow_up"]
    if n and n < today: return (f"Follow-up overdue ({n.day} {n:%b})", "due")
    if n and n == today: return ("Follow up today", "due")
    if n: return (f"Follow up {n:%a} {n.day} {n:%b}", "plain")
    if x["status"] == "not_contacted":
        return ("Send check-in" if len(x["phone_digits"] or "") >= 8 else "Send email", "todo")
    return ("Set a follow-up date", "todo")


def list_view(me: str, args) -> dict:
    """The All accounts list: view, filters (each with counts that respect
    the other filters), search, sort."""
    view = OLD_TABS.get(args.get("tab") or args.get("view") or "", args.get("tab") or args.get("view") or "priority")
    if view not in dict(LIST_VIEWS) and view not in HIDDEN_VIEWS:
        view = "priority"
    sel = {
        "mgr": args.get("mgr") or args.get("who") or "all",
        "act": args.get("act") or "any",
        "fu": args.get("fu") or "any",
        "st": [v for v in args.getlist("st") if v],
        "stop": [v for v in args.getlist("stop") if v],
        "plan": [v for v in args.getlist("plan") if v],
        "ctry": [v for v in args.getlist("ctry") if v],
        "src": [v for v in args.getlist("src") if v],
    }
    q = (args.get("q") or "").strip()
    sort = args.get("sort") if args.get("sort") in dict(SORTS) else "priority"

    R = rows()
    reply_ids = {c["tenant_id"] for c in conversations() if c["needs_reply"]}
    qd = re.sub(r"\D", "", q)
    def match_q(x):
        if not q: return True
        hay = " ".join(str(x.get(k) or "") for k in ("business", "contact", "email", "company_name")).lower()
        return q.lower() in hay or (len(qd) >= 4 and qd in (x["phone_digits"] or ""))
    base = [x for x in R if _in_view(x, view) and match_q(x)]
    F = {x["tenant_id"]: _facets(x, me) for x in R}
    single = ("mgr", "act", "fu")

    def passes(x, skip=None):
        f = F[x["tenant_id"]]
        for k, v in sel.items():
            if k == skip: continue
            if k in single:
                if v not in f[k]: return False
            elif v and not (f[k] & set(v)): return False
        return True

    shown = [x for x in base if passes(x)]

    def counts(key):
        c = {}
        for x in base:
            if passes(x, skip=key):
                for v in F[x["tenant_id"]][key]:
                    c[v] = c.get(v, 0) + 1
        return c

    managers = sorted({x["assigned_admin"] for x in R if x["assigned_admin"]})
    status_order = [k for k, _ in STATUSES]
    filters = []
    def add(key, label, kind, opts, search=False, title=None):
        c = counts(key)
        o = [{"value": v, "label": l, "n": c.get(v, 0)} for v, l in opts]
        if kind == "radio":
            cur = sel[key]
            value = next((x["label"] for x in o if x["value"] == cur), cur)
            active = cur not in ("all", "any")
            for x in o: x["on"] = x["value"] == cur
        else:
            cur = sel[key]
            value = ", ".join(next((x["label"] for x in o if x["value"] == v), v) for v in cur) if cur else "Any"
            if len(cur) > 1: value = f"{len(cur)} selected"
            active = bool(cur)
            for x in o: x["on"] = x["value"] in cur
        filters.append({"key": key, "label": label, "title": title or label, "kind": kind, "options": o,
                        "value": value, "active": active, "search": search})

    add("mgr", "Manager", "radio", [("all", "Everyone"), ("me", "Me")] + [(u, manager_name(u)) for u in managers] + [("none", "Unassigned")],
        title="Account Manager")
    add("st", "Status", "checkbox", [(k, STATUS_LABEL[k]) for k in status_order])
    stops = counts("stop")
    add("stop", "Stopped at", "checkbox", sorted(((k, k) for k in stops), key=lambda kv: -stops[kv[0]]), title="Set-up step they stopped at")
    plans = counts("plan")
    add("plan", "Plan", "checkbox", sorted(((k, k) for k in plans), key=lambda kv: -plans[kv[0]]))
    ctry = counts("ctry")
    add("ctry", "Country", "checkbox", sorted(((k, k) for k in ctry), key=lambda kv: (-ctry[kv[0]], kv[0])), search=True)
    srcs = counts("src")
    add("src", "Came from", "checkbox", sorted(((k, k) for k in srcs), key=lambda kv: (-srcs[kv[0]], kv[0])), search=True,
        title="How they found us")
    add("act", "Activity", "radio", ACT_OPTS, title="Log-ins and sign-up date")
    add("fu", "Follow-up", "radio", FU_OPTS, title="Follow-up date")
    # keep selected values visible even when they now count 0
    for f in filters:
        have = {o["value"] for o in f["options"]}
        for v in (sel[f["key"]] if f["kind"] == "checkbox" else []):
            if v not in have:
                f["options"].append({"value": v, "label": v, "n": 0, "on": True})

    for x in shown:
        x["score"], x["tags"] = priority(x, reply_ids)
        x["next"] = next_step(x, reply_ids)
        x["replied"] = x["tenant_id"] in reply_ids
    far = date(9999, 1, 1)
    old = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if sort == "priority": shown.sort(key=lambda x: -x["score"])
    elif sort == "newest": shown.sort(key=lambda x: x["signed_up"] or old, reverse=True)
    elif sort == "waiting": shown.sort(key=lambda x: ((x["last_contact"] or {}).get("created_at") or old, -(x["days_since_signup"] or 0)))
    elif sort == "followup": shown.sort(key=lambda x: x["next_follow_up"] or far)
    elif sort == "login": shown.sort(key=lambda x: x["last_login_at"] or old, reverse=True)

    views = [{"key": k, "label": l, "n": sum(1 for x in R if _in_view(x, k) and match_q(x) and passes(x)), "on": k == view}
             for k, l in LIST_VIEWS]
    return {"view": view, "view_label": dict(LIST_VIEWS).get(view) or HIDDEN_VIEWS.get(view), "hidden_view": view in HIDDEN_VIEWS,
            "views": views, "filters": filters, "sel": sel, "q": q, "sort": sort, "rows": shown, "total": len(R)}


# ── One account page (2026-11-06) ─────────────────────────────────────────

def next_best(a: dict, window: bool, replied: bool, tpl_ready: bool) -> dict:
    """One suggestion for the Account Manager: {title, why, primary, label}.
    primary = the action panel to open: wa | call | email | logcall | followup | none."""
    today = date.today()
    first = (a.get("first_name") or "").strip() or a.get("contact") or "them"
    phone = len(a.get("phone_digits") or "") >= 8
    stuck = a["setup"].get("stuck")
    stuck_txt = f"they stopped at {STEP_SHORT.get(stuck, stuck)}" if stuck else "they haven't finished set-up"
    seen = ""
    if a.get("last_login_at"):
        days = (datetime.now(timezone.utc) - a["last_login_at"]).days
        seen = "logged in today" if days == 0 else "logged in yesterday" if days == 1 else f"last logged in {days} days ago"
    else:
        seen = "never logged in after signing up"
    reach = ("call", "WhatsApp call") if phone else ("email", "Write an email")
    if a["status"] == "set_up" or a["setup"]["complete"]:
        return {"title": f"{a.get('business') or 'This account'} is set up", "why": "Check in after their first week to make sure the AI is answering customers well.",
                "primary": "note", "label": "Add a note", "tone": "good"}
    if a["status"] == "lost":
        return {"title": "Marked as lost", "why": (a.get("reason_label") or "No reason recorded") + (f": {a['reason_note']}" if a.get("reason_note") else "") + ".",
                "primary": "none", "label": "", "tone": "plain"}
    if replied and window:
        return {"title": f"Reply to {first} on WhatsApp", "why": "They wrote to us and are waiting for an answer. You can write freely for 24 hours after their message.",
                "primary": "wa", "label": "Reply on WhatsApp", "tone": "urgent"}
    if is_paid_trial_ending(a):
        end = _d(a["trial_ends_at"]); left = (end - today).days
        when = "today" if left == 0 else "tomorrow" if left == 1 else f"on {end:%A}"
        return {"title": f"{'WhatsApp call' if phone else 'Email'} {first} before the trial ends {when}",
                "why": f"Paid trial ({a['plan_name'].split(' ·')[0]}), {seen}, but {stuck_txt}. Helping them finish on a WhatsApp call is the quickest way to get them live.",
                "primary": reach[0], "label": reach[1], "tone": "urgent"}
    n = a.get("next_follow_up")
    if n and n <= today:
        return {"title": f"Follow up with {first} {'today' if n == today else f'(due {n.day} {n:%b})'}",
                "why": f"You planned to follow up. {stuck_txt[0].upper() + stuck_txt[1:]} and {seen}.",
                "primary": reach[0], "label": reach[1], "tone": "urgent" if n < today else "warn"}
    if a["status"] == "not_contacted":
        if phone and tpl_ready:
            return {"title": f"Send {first} the check-in message", "why": f"Nobody has contacted them yet. {stuck_txt[0].upper() + stuck_txt[1:]} and {seen}. The check-in asks what got in the way, with one-tap answers.",
                    "primary": "wa", "label": "Preview check-in message", "tone": "warn"}
        if phone:
            return {"title": f"WhatsApp call {first}", "why": f"Nobody has contacted them yet. {stuck_txt[0].upper() + stuck_txt[1:]} and {seen}.",
                    "primary": "call", "label": "WhatsApp call", "tone": "warn"}
        return {"title": f"Email {first}", "why": f"No phone number, so email is the only way. {stuck_txt[0].upper() + stuck_txt[1:]} and {seen}.",
                "primary": "email", "label": "Write an email", "tone": "warn"}
    if not n:
        return {"title": "Set a follow-up date", "why": "You've been in touch, but there's no follow-up date, so this account can be forgotten.",
                "primary": "followup", "label": "Set a follow-up date", "tone": "warn"}
    return {"title": f"Next follow-up {n:%a} {n.day} {n:%b}", "why": f"{stuck_txt[0].upper() + stuck_txt[1:]} and {seen}. Nothing else is due before then.",
            "primary": "note", "label": "Add a note", "tone": "plain"}


def journey(a: dict) -> list:
    """Account events for the timeline (signed up, trial, last log-in)."""
    ev = []
    if a.get("signed_up"):
        src = SOURCE_LABEL.get(a.get("hear_about_us") or "", (a.get("hear_about_us") or "").replace("_", " ").capitalize())
        ev.append({"at": a["signed_up"], "src": "event", "kind": "signup", "title": "Signed up",
                   "sub": f"{a['setup'].get('type') or ''}{' · found us on ' + src if src and src != 'Other' else ''}".strip(" ·")})
    if a.get("trial_granted_at"):
        end = _d(a.get("trial_ends_at"))
        ev.append({"at": a["trial_granted_at"], "src": "event", "kind": "trial", "title": f"{a['plan_name'].split(' ·')[0]} trial started",
                   "sub": f"Ends {end:%a} {end.day} {end:%b}" if end else ""})
    if a.get("last_login_at"):
        ev.append({"at": a["last_login_at"], "src": "event", "kind": "login", "title": "Last logged in to PhiXtra", "sub": ""})
    return ev


# ── Email signature per Account Manager (2026-11-06) ──────────────────────
# Image (PNG, JPEG, GIF, WebP, BMP, TIFF) shown inside every email they send
# from Admin, embedded in the email (cid) so it shows without "load images".

SIG_CID = "am-signature"
SIG_MAX_BYTES = 5 * 1024 * 1024
SIG_MAX_W = 600          # stored size
SIG_SHOW_W = 400         # shown size in the email (sharper on high-res screens)


def _sig_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS am_signatures (
                     admin_username VARCHAR(190) PRIMARY KEY, image BYTEA NOT NULL, mime VARCHAR(40) NOT NULL,
                     width INTEGER NOT NULL, height INTEGER NOT NULL, link TEXT, filename TEXT,
                     updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")


def get_signature(username: str):
    conn, cur = _db()
    try:
        _sig_table(cur); conn.commit()
        cur.execute("SELECT * FROM am_signatures WHERE admin_username=%s", (username,))
        r = cur.fetchone()
        if r:
            r = dict(r); r["image"] = bytes(r["image"])
            r["show_w"] = min(r["width"], SIG_SHOW_W)
            r["show_h"] = round(r["height"] * r["show_w"] / r["width"]) if r["width"] else r["height"]
        return r
    finally:
        cur.close(); conn.close()


def save_signature(username: str, raw: bytes, filename: str, link: str = ""):
    """(ok, message). Reads any common image format, resizes to email width,
    keeps PNG / JPEG / GIF as they are and turns the rest into PNG."""
    import io
    from PIL import Image, ImageOps
    if not raw:
        return False, "Choose an image file."
    if len(raw) > SIG_MAX_BYTES:
        return False, "That image is over 5 MB. Use a smaller one."
    low = (filename or "").lower()
    if low.endswith(".svg") or raw[:200].lstrip().startswith((b"<svg", b"<?xml")):
        return False, "SVG images don't show in most email apps. Save it as PNG or JPEG and upload that."
    if low.endswith((".heic", ".heif")):
        return False, "iPhone HEIC photos can't be used. Save it as JPEG or PNG and upload that."
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return False, "That file isn't an image we can read. Use PNG, JPEG, GIF, WebP, BMP or TIFF."
    fmt = (im.format or "").upper()
    animated = fmt == "GIF" and getattr(im, "is_animated", False)
    if animated and im.width <= SIG_MAX_W:
        data, mime, w, h = raw, "image/gif", im.width, im.height
    else:
        im = ImageOps.exif_transpose(im)
        if im.width > SIG_MAX_W:
            im = im.resize((SIG_MAX_W, round(im.height * SIG_MAX_W / im.width)), Image.LANCZOS)
        out = io.BytesIO()
        if fmt in ("JPEG", "JPG") and im.mode in ("RGB", "L"):
            im.save(out, "JPEG", quality=90, optimize=True); mime = "image/jpeg"
        else:
            if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                im = im.convert("RGBA")
            im.save(out, "PNG", optimize=True); mime = "image/png"
        data, w, h = out.getvalue(), im.width, im.height
    link = (link or "").strip()[:500]
    if link and not link.lower().startswith(("http://", "https://", "mailto:")):
        link = "https://" + link
    conn, cur = _db()
    try:
        _sig_table(cur)
        cur.execute("""INSERT INTO am_signatures (admin_username, image, mime, width, height, link, filename, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT (admin_username) DO UPDATE SET image=EXCLUDED.image, mime=EXCLUDED.mime,
                           width=EXCLUDED.width, height=EXCLUDED.height, link=EXCLUDED.link,
                           filename=EXCLUDED.filename, updated_at=NOW()""",
                    (username, psycopg2.Binary(data), mime, w, h, link or None, (filename or "")[:200]))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return True, "Signature saved. It's added under the emails you send from Accounts."


def signature_html(sig) -> str:
    if not sig:
        return ""
    img = (f'<img src="cid:{SIG_CID}" width="{sig["show_w"]}" height="{sig["show_h"]}" alt="Email signature" '
           f'style="display:block;border:0;max-width:100%;height:auto">')
    if sig.get("link"):
        import html as _h
        img = f'<a href="{_h.escape(sig["link"])}">{img}</a>'
    return f'<div style="margin-top:18px">{img}</div>'


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
            cur.execute("""SELECT m.created_at, m.direction, m.content, m.message_type, m.sent_by_label,
                                  s.status AS wa_status, s.status_at, s.error AS wa_error, s.delivered_at, s.read_at
                             FROM wa_message_log m LEFT JOIN wa_msg_status s ON s.meta_message_id = m.meta_message_id
                            WHERE m.tenant_id=%s AND regexp_replace(m.customer_phone,'\\D','','g')=%s
                              AND NOT COALESCE(m.is_historical, FALSE)
                            ORDER BY m.created_at DESC LIMIT 200""", (pid, d))
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
        n = am_number()   # the 24-hour window is per WhatsApp number
        cur.execute("""SELECT 1 FROM wa_message_log WHERE tenant_id=%s AND direction='inbound'
                         AND regexp_replace(customer_phone,'\\D','','g')=%s AND created_at > NOW() - INTERVAL '24 hours'
                         AND (%s IS NULL OR phone_number_id=%s)
                        LIMIT 1""", (pid, d, n and n["phone_number_id"], n and n["phone_number_id"]))
        return cur.fetchone() is not None
    finally:
        cur.close(); conn.close()


_tpl_cache = {"at": None, "st": {}}


def template_statuses() -> dict:
    """{name: {"status", "category"}} for the Account Manager templates, live
    from Meta (cached 10 min)."""
    now = datetime.now(timezone.utc)
    if _tpl_cache["at"] and now - _tpl_cache["at"] < timedelta(minutes=10):
        return _tpl_cache["st"]
    st = {}
    nums = platform_numbers()
    if nums:
        try:
            r = requests.get(f"{GRAPH}/{nums[0]['waba_id']}/message_templates",
                             params={"fields": "name,status,category", "limit": 200},
                             headers={"Authorization": f"Bearer {nums[0]['access_token']}"}, timeout=8).json()
            for t in r.get("data", []):
                if t.get("name") in AM_TPL:
                    st[t["name"]] = {"status": t.get("status"), "category": t.get("category")}
        except Exception as e:
            print("⚠️ admin_accounts template check:", e)
            return _tpl_cache["st"]
    _tpl_cache.update(at=now, st=st)
    return st


def approved_templates() -> list:
    return [t["name"] for t in templates_for("", None) if t["approved"]]


def active_template():
    """The default check-in: the first approved one (Utility first), or None."""
    a = approved_templates()
    return a[0] if a else None


def checkin_template_ready() -> bool:
    """Is a WhatsApp check-in template approved on PhiXtra's number?"""
    return active_template() is not None


def send_whatsapp(tenant_id: int, phone: str, text: str, by: str, use_template=False, first_name="", manager="",
                  stuck=None, template_name=None):
    """Send from PhiXtra's first WhatsApp number; log it where the Inbox reads it too."""
    num = am_number()
    if not num:
        return False, "PhiXtra's WhatsApp number isn't connected."
    to = digits(phone)
    if len(to) < 8:
        return False, "This customer has no valid phone number."
    if use_template:
        name = template_name or active_template()
        if name not in approved_templates():
            return False, "That WhatsApp template isn't approved by Meta."
        t = AM_TPL[name]
        params = [{"type": "text", "text": first_name or "there"}]
        if "{{2}}" in t["body"]:
            params.append({"type": "text", "text": step_phrase(stuck)})
        comps = [{"type": "body", "parameters": params}]
        if t["buttons"]:
            comps += [{"type": "button", "sub_type": "quick_reply", "index": str(i),
                       "parameters": [{"type": "payload", "payload": key}]} for i, (key, _) in enumerate(HELP_BUTTONS)]
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                   "template": {"name": name, "language": {"code": "en"}, "components": comps}}
        logged = checkin_text(first_name, stuck, name)
    else:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
                   "type": "text", "text": {"body": text}}
        logged = text
    mid = None
    try:
        r = requests.post(f"{GRAPH}/{num['phone_number_id']}/messages", json=payload,
                          headers={"Authorization": f"Bearer {num['access_token']}"}, timeout=15)
        ok = r.status_code == 200
        err = "" if ok else ((r.json().get("error") or {}).get("message") or f"HTTP {r.status_code}")
        if ok:
            mid = ((r.json().get("messages") or [{}])[0]).get("id")
    except Exception as e:
        ok, err = False, str(e)
    pid = A.platform_tenant_id()
    conn, cur = _db()
    try:
        if ok and pid:
            cur.execute("""INSERT INTO wa_message_log (tenant_id, phone_number_id, customer_phone, direction, content,
                                                       message_type, sent_by_label, meta_message_id)
                           VALUES (%s,%s,%s,'outbound',%s,'agent_reply',%s,%s)""",
                        (pid, num["phone_number_id"], to, logged, AM_NAME + " (Account Manager)", mid))
            set_quiet(cur, to, by, True)
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
            "manager": AM_NAME}
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

    @bp.route("/accounts/settings/signature", methods=["POST"])
    def am_signature_save():
        r = _gate("view")   # everyone manages their own signature
        if r: return r
        me = admin_user()
        if request.form.get("remove") == "1":
            conn, cur = _db()
            try:
                _sig_table(cur)
                cur.execute("DELETE FROM am_signatures WHERE admin_username=%s", (me,)); conn.commit()
            finally:
                cur.close(); conn.close()
            flash("Signature removed.", "success")
            return redirect(url_for("portal_admin.am_settings") + "#signature")
        f = request.files.get("image")
        if f and f.filename:
            ok, msg = save_signature(me, f.read(SIG_MAX_BYTES + 1), f.filename, request.form.get("link") or "")
        else:
            sig = get_signature(me)
            if not sig:
                ok, msg = False, "Choose an image file."
            else:   # only the link changed
                conn, cur = _db()
                try:
                    link = (request.form.get("link") or "").strip()[:500]
                    if link and not link.lower().startswith(("http://", "https://", "mailto:")):
                        link = "https://" + link
                    cur.execute("UPDATE am_signatures SET link=%s, updated_at=NOW() WHERE admin_username=%s", (link or None, me))
                    conn.commit()
                finally:
                    cur.close(); conn.close()
                ok, msg = True, "Signature link saved."
        insert_audit_log(admin_username=me, action="am_signature_saved" if ok else "am_signature_refused", details={"msg": msg})
        flash(msg, "success" if ok else "warning")
        return redirect(url_for("portal_admin.am_settings") + "#signature")

    @bp.route("/accounts/signature.img")
    def am_signature_img():
        r = _gate("view")
        if r: return r
        from flask import Response
        sig = get_signature(admin_user())
        if not sig:
            return ("", 404)
        return Response(sig["image"], mimetype=sig["mime"], headers={"Cache-Control": "private, no-store"})

    @bp.route("/accounts/settings/calls", methods=["POST"])
    def am_settings_calls():
        """Account Managers' WhatsApp calls on/off for the Account Manager
        number — here in Admin, because Account Managers have no PhiXtra
        portal login. Outgoing only: customers can't ring this number."""
        r = _gate("modify")
        if r: return r
        import calls_routes as C
        me = admin_user()
        num = am_number()
        if not num:
            flash("PhiXtra has no WhatsApp number connected.", "warning")
            return redirect(url_for("portal_admin.am_settings"))
        want = request.form.get("outgoing") == "1"
        pnid = num["phone_number_id"]
        if want and not C._outgoing_allowed_country(num["display_phone_number"]):
            flash("Meta doesn't allow calling customers from this number's country.", "warning")
            return redirect(url_for("portal_admin.am_settings"))
        conn, cur = _db()
        try:
            cur.execute("SELECT * FROM wa_call_settings WHERE phone_number_id=%s", (pnid,))
            st = cur.fetchone() or {}
            incoming = bool(st.get("incoming_on"))
            hours = st.get("hours") or {"days": [1, 2, 3, 4, 5], "from": "0900", "to": "1700", "tz": "Europe/London"}
            calling = {"status": "ENABLED" if incoming or want else "DISABLED",
                       "call_icon_visibility": "DEFAULT" if incoming else "DISABLE_ALL",
                       "callback_permission_status": "ENABLED" if want else "DISABLED"}
            if st.get("hours_on"):
                calling["call_hours"] = {"status": "ENABLED", "timezone_id": hours.get("tz") or "Europe/London",
                                         "weekly_operating_hours": [{"day_of_week": C.META_DAYS[d], "open_time": hours.get("from", "0900"),
                                                                     "close_time": "2359" if hours.get("to") == "2400" else hours.get("to", "1700")}
                                                                    for d in hours.get("days") or [1, 2, 3, 4, 5]]}
            else:   # same as the portal's Call Settings page
                calling["call_hours"] = {"status": "DISABLED", "timezone_id": hours.get("tz") or "Europe/London",
                                         "weekly_operating_hours": [{"day_of_week": "MONDAY", "open_time": "0000", "close_time": "2359"}]}
            ok, data = C._graph(pnid, num["access_token"], {"calling": calling}, path="settings")
            meta_status = "saved" if ok else C._meta_error(data)
            outgoing = want if ok else bool(st.get("outgoing_on"))
            cur.execute("""INSERT INTO wa_call_settings (phone_number_id, tenant_id, incoming_on, outgoing_on, meta_status, updated_at, updated_by)
                           VALUES (%s,%s,%s,%s,%s,NOW(),%s)
                           ON CONFLICT (phone_number_id) DO UPDATE SET outgoing_on=EXCLUDED.outgoing_on,
                               meta_status=EXCLUDED.meta_status, updated_at=NOW(), updated_by=EXCLUDED.updated_by""",
                        (pnid, A.platform_tenant_id(), incoming, outgoing, meta_status, f"Admin: {me}"))
            conn.commit()
        finally:
            cur.close(); conn.close()
        insert_audit_log(admin_username=me, action="am_calls_" + ("on" if want else "off"),
                         details={"number": num["display_phone_number"], "meta": meta_status})
        if ok:
            flash(f"WhatsApp calls are {'on' if want else 'off'} for {num['display_phone_number']}." +
                  (" Account Managers can now call customers who accept WhatsApp's \"Can we call you?\" request." if want else ""),
                  "success")
        else:
            flash("Meta refused the calling settings: " + meta_status, "danger")
        return redirect(url_for("portal_admin.am_settings"))

    @bp.route("/accounts/settings", methods=["GET", "POST"])
    def am_settings():
        r = _gate("modify" if request.method == "POST" else "view")
        if r: return r
        me = admin_user()
        st = get_settings()
        if request.method == "POST":
            def num(name, lo, hi):
                try:
                    return max(lo, min(hi, int(request.form.get(name) or st[name])))
                except ValueError:
                    return int(st[name])
            picked = [u for u in request.form.getlist("alert_fallback") if u]
            new = {"alerts_on": "1" if request.form.get("alerts_on") else "0",
                   "alert_every_min": num("alert_every_min", 1, 60),
                   "alert_window_hours": num("alert_window_hours", 1, 48),
                   "alert_fallback": ",".join(picked),
                   "later_days": num("later_days", 1, 90)}
            _n = request.form.get("am_number") or ""
            if _n and _n in {x["phone_number_id"] for x in platform_numbers()}:
                new["am_number"] = _n
            save_settings(new, me)
            insert_audit_log(admin_username=me, action="am_settings_saved", details=new)
            flash("Settings saved.", "success")
            return redirect(url_for("portal_admin.am_settings"))
        fallback = [u for u in (st["alert_fallback"] or "").split(",") if u]
        conn, cur = _db()
        try:
            cur.execute("""SELECT updated_by, updated_at FROM am_settings WHERE key <> 'alert_last_run'
                            ORDER BY updated_at DESC LIMIT 1""")
            saved = cur.fetchone()
            sent7 = 0
            if _has_table(cur, "am_reply_alert"):
                cur.execute("SELECT COUNT(*) AS n FROM am_reply_alert WHERE sent_at > NOW() - INTERVAL '7 days'")
                sent7 = cur.fetchone()["n"]
        finally:
            cur.close(); conn.close()
        last_run = None
        try:
            last_run = datetime.fromisoformat(st["alert_last_run"]) if st.get("alert_last_run") else None
        except ValueError:
            pass
        admins = [dict(u, name=manager_name(u["username"])) for u in admin_users() if "@" in u["username"]]
        numbers = []
        conn, cur = _db()
        try:
            for n in platform_numbers():
                cur.execute("""SELECT COALESCE(tm.name, tm.email) AS who FROM team_member_numbers tn
                                 JOIN team_members tm ON tm.id = tn.team_member_id
                                WHERE tn.phone_number_id=%s AND tm.is_active ORDER BY 1""", (n["phone_number_id"],))
                users = [r["who"] for r in cur.fetchall() if r["who"] and "screenshot" not in r["who"].lower()]
                cur.execute("SELECT COALESCE(outgoing_on, FALSE) AS o FROM wa_call_settings WHERE phone_number_id=%s", (n["phone_number_id"],))
                cs = cur.fetchone()
                cur.execute("SELECT COALESCE(label,'') AS l FROM wa_tenants WHERE id=%s", (n["id"],))
                numbers.append({"id": n["phone_number_id"], "display": n["display_phone_number"], "label": cur.fetchone()["l"],
                                "users": users, "calls": bool(cs and cs["o"])})
        finally:
            cur.close(); conn.close()
        cur_num = am_number()
        am_calls = next((n for n in numbers if cur_num and n["id"] == cur_num["phone_number_id"]), None)
        return render_template("portal/admin_accounts_settings.html", st=st, fallback=fallback, admins=admins,
                               saved=saved, last_run=last_run, sent7=sent7, now=datetime.now(timezone.utc),
                               numbers=numbers, cur_num=(cur_num or {}).get("phone_number_id"), am_calls=am_calls,
                               my_sig=get_signature(me),
                               can_modify=(require_admin("accounts", "modify") is None))

    @bp.route("/accounts/dashboard")
    def am_dashboard():
        r = _gate()
        if r: return r
        me = admin_user()
        who = request.args.get("who") or "all"
        try:
            period = int(request.args.get("period") or 30)
        except ValueError:
            period = 30
        period = period if period in (7, 30, 90) else 30
        d = dashboard(me, who, period)
        managers = sorted({x["assigned_admin"] for x in rows() if x["assigned_admin"]})
        return render_template("portal/admin_accounts_dashboard.html", d=d, who=who, period=period, me=me,
                               me_name=manager_name(me), managers=[(u, manager_name(u)) for u in managers],
                               today=date.today(), can_modify=(require_admin("accounts", "modify") is None))

    @bp.route("/accounts")
    def am_accounts():
        r = _gate()
        if r: return r
        me = admin_user()
        L = list_view(me, request.args)
        try:
            limit = max(25, min(1000, int(request.args.get("limit") or 50)))
        except ValueError:
            limit = 50
        # every current filter as (name, value) pairs, for links that change one thing
        keep = [(k, v) for k, vs in request.args.lists() for v in vs if k not in ("limit", "tab", "who") and v]
        if "view" not in request.args and L["view"] != "priority":
            keep.append(("view", L["view"]))
        if request.args.get("who") and not request.args.get("mgr"):
            keep.append(("mgr", request.args.get("who")))
        tpl_ready = checkin_template_ready()
        return render_template("portal/admin_accounts.html", L=L, rows=L["rows"][:limit], more=len(L["rows"]) > limit,
                               limit=limit, keep=keep, me=me, today=date.today(), sorts=SORTS,
                               admins=[u for u in admin_users() if "@" in u["username"]], manager_name=manager_name,
                               checkin_ready=tpl_ready, bulk_templates=[t for t in templates_for("", None) if t["approved"]],
                               can_modify=(require_admin("accounts", "modify") is None))

    @bp.route("/accounts/bulk", methods=["POST"])
    def am_bulk():
        r = _gate("modify")
        if r: return r
        me = admin_user()
        f = request.form
        back = redirect(f.get("back") if (f.get("back") or "").startswith("/admin/accounts") else url_for("portal_admin.am_accounts"))
        ids = []
        for v in f.getlist("ids"):
            try: ids.append(int(v))
            except ValueError: pass
        ids = ids[:200]
        if not ids:
            flash("Tick at least one account first.", "warning")
            return back
        action = f.get("action")
        found = {x["tenant_id"]: x for x in rows() if x["tenant_id"] in set(ids)}
        if action == "assign":
            who = (f.get("assigned_admin") or "").strip() or None
            if who and who not in {u["username"] for u in admin_users()}:
                flash("Pick an Account Manager.", "warning"); return back
            conn, cur = _db()
            try:
                for tid in found:
                    _upsert_state(cur, tid, me, assigned_admin=who)
                    log(cur, tid, "status", me, body=f"Assigned to {manager_name(who) if who else 'nobody'}")
                conn.commit()
            finally:
                cur.close(); conn.close()
            insert_audit_log(admin_username=me, action="am_bulk_assign", details={"ids": list(found), "to": who})
            flash(f"{len(found)} account{'s' if len(found) != 1 else ''} assigned to {manager_name(who) if who else 'nobody'}.", "success")
        elif action == "followup":
            fu = _date(f.get("follow_up"))
            if not fu:
                flash("Pick a follow-up date.", "warning"); return back
            conn, cur = _db()
            try:
                for tid in found:
                    _upsert_state(cur, tid, me, next_follow_up=fu)
                    log(cur, tid, "status", me, body=f"Next follow-up {fu:%-d %b %Y}")
                conn.commit()
            finally:
                cur.close(); conn.close()
            insert_audit_log(admin_username=me, action="am_bulk_followup", details={"ids": list(found), "date": fu.isoformat()})
            flash(f"Follow-up set for {fu:%a %-d %b} on {len(found)} account{'s' if len(found) != 1 else ''}.", "success")
        elif action == "checkin":
            if not checkin_template_ready():
                flash("The WhatsApp check-in message isn't approved by Meta yet.", "warning"); return back
            sent, skipped, failed = 0, [], []
            for tid, a in list(found.items())[:50]:
                if len(a["phone_digits"] or "") < 8:
                    skipped.append(a["business"]); continue
                ok, err = send_whatsapp(tid, a["phone_number"], "", me, use_template=True,
                                        first_name=(a["first_name"] or "").strip(), manager=AM_NAME,
                                        stuck=(a.get("setup") or {}).get("stuck"),
                                        template_name=f.get("template") or None)
                if ok: sent += 1
                else: failed.append(f"{a['business']} ({err})")
            insert_audit_log(admin_username=me, action="am_bulk_checkin",
                             details={"ids": list(found), "sent": sent, "skipped": skipped, "failed": failed})
            msg = f"Check-in sent to {sent} account{'s' if sent != 1 else ''}."
            if skipped: msg += f" Skipped {len(skipped)} with no phone number."
            if failed: msg += " Didn't send: " + "; ".join(failed[:5])
            flash(msg, "success" if sent and not failed else "warning")
        else:
            flash("Unknown action.", "warning")
        return back

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
        _ready = (not window and checkin_template_ready())
        _replied = any(c["tenant_id"] == tenant_id and c["needs_reply"] for c in conversations())
        nb = next_best(a, window, _replied, _ready or window)
        _items = timeline(tenant_id, a["phone_number"]) + journey(a)
        _aware = lambda t: (t if getattr(t, "tzinfo", None) else (t.replace(tzinfo=timezone.utc) if isinstance(t, datetime)
                            else datetime(t.year, t.month, t.day, tzinfo=timezone.utc)))
        _items.sort(key=lambda i: _aware(i["at"]), reverse=True)
        return render_template(
            "portal/admin_account.html", a=a, items=_items, me=me, nb=nb, replied=_replied, my_sig=get_signature(me),
            statuses=STATUSES, reasons=REASONS, outcomes=CALL_OUTCOMES, admins=admin_users(),
            email_templates=EMAIL_TEMPLATES, etpl=etpl, email_subject=fill(etpl[1], a, me),
            email_body=fill(etpl[2], a, me),
            reply_to=(me if "@" in me else "support@phixtra.com"),
            wa_window=window, checkin_ready=(not window and checkin_template_ready()),
            wa_numbers=am_numbers(),
            checkin_preview=checkin_look((a["first_name"] or "").strip(), (a.get("setup") or {}).get("stuck")),
            wa_templates=templates_for((a["first_name"] or "").strip(), (a.get("setup") or {}).get("stuck")),
            call_ask_preview=call_ask_text((a["first_name"] or "").strip()),
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
        if use_tpl and (request.form.get("template") or active_template()) not in approved_templates():
            flash("That WhatsApp template isn't approved by Meta yet.", "warning")
            return _back(tenant_id)
        ok, err = send_whatsapp(tenant_id, a["phone_number"], text, me, use_template=use_tpl,
                                first_name=(a["first_name"] or "").strip(), manager=manager_name(me),
                                stuck=(a.get("setup") or {}).get("stuck"),
                                template_name=request.form.get("template") or None)
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
        sig = get_signature(me) if request.form.get("with_sig") == "1" else None
        if sig:
            html_body += signature_html(sig)
        ok = send_email(a["email"], subject, html_body, body, reply_to=reply_to if "@" in reply_to else None,
                        inline_images=[(SIG_CID, sig["image"], sig["mime"])] if sig else None)
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


# ── WhatsApp calls from the admin (2026-11-06, user: calling must happen on
# the account page, not in PhiXtra's Inbox). Same Meta flow as the portal's
# wa-calls.js / calls_routes (permission → connect with the browser's SDP
# offer → poll for the answer SDP the webhook stores → terminate), made from
# PhiXtra's own number and recorded as the Account Manager. ────────────────

def _call_number(cur):
    """PhiXtra's number Account Managers call from (Admin › Accounts › Settings)."""
    pid = A.platform_tenant_id()
    n = am_number()   # Admin › Accounts › Settings
    cur.execute("""SELECT wt.phone_number_id, wt.display_phone_number, COALESCE(s.outgoing_on, FALSE) AS outgoing_on
                     FROM wa_tenants wt LEFT JOIN wa_call_settings s ON s.phone_number_id = wt.phone_number_id
                    WHERE wt.tenant_id=%s AND wt.active AND wt.access_token LIKE 'EAA%%' AND (%s IS NULL OR wt.phone_number_id=%s)
                    ORDER BY wt.id LIMIT 1""", (pid, n and n["phone_number_id"], n and n["phone_number_id"]))
    return cur.fetchone()


def register_calls(bp, require_admin, admin_user):
    from flask import jsonify
    import calls_routes as C

    def me():
        u = admin_user()
        return "admin:" + u, AM_NAME + " (Account Manager)"

    def _phone(tenant_id):
        found = rows(only_tenant=tenant_id)
        return (found[0] if found else None)

    @bp.route("/accounts/<int:tenant_id>/call/check")
    def am_call_check(tenant_id):
        if require_admin("accounts", "modify"):
            return jsonify({"error": "login"}), 401
        a = _phone(tenant_id)
        if not a or len(a["phone_digits"]) < 8:
            return jsonify({"show": False, "why": "This customer has no phone number."})
        pid = A.platform_tenant_id()
        conn, cur = _db()
        try:
            num = _call_number(cur)
            if not num:
                return jsonify({"show": False, "why": "PhiXtra's WhatsApp number isn't connected."})
            tok, why = C._outgoing_ready(cur, pid, num["phone_number_id"])
            if why and "switched off" in why:
                why = "WhatsApp calls are switched off for this number. Turn them on in Admin › Accounts › Settings."
            if not tok:
                return jsonify({"show": False, "why": why, "number": num["display_phone_number"]})
            status, acts = "no_permission", {}
            try:
                r = requests.get(f"{C.GRAPH}/{num['phone_number_id']}/call_permissions",
                                 params={"user_wa_id": a["phone_digits"]},
                                 headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=10).json()
                status = ((r.get("permission") or {}).get("status")) or status
                acts = {x.get("action_name"): x.get("can_perform_action") for x in (r.get("actions") or [])}
            except Exception:
                pass
        finally:
            cur.close(); conn.close()
        return jsonify({"show": True, "number": num["display_phone_number"], "permission": status,
                        "can_call": bool(acts.get("start_call")) or status in ("temporary", "permanent"),
                        "can_ask": acts.get("send_call_permission_request", True)})

    @bp.route("/accounts/<int:tenant_id>/call/permission", methods=["POST"])
    def am_call_permission(tenant_id):
        if require_admin("accounts", "modify"):
            return jsonify({"error": "login"}), 401
        a = _phone(tenant_id)
        if not a:
            return jsonify({"error": "Account not found."}), 404
        pid = A.platform_tenant_id()
        key, label = me()
        conn, cur = _db()
        try:
            num = _call_number(cur)
            tok, why = C._outgoing_ready(cur, pid, num["phone_number_id"]) if num else (None, "No WhatsApp number.")
            if not tok:
                return jsonify({"error": why}), 400
            text = call_ask_text((a["first_name"] or "").strip())
            ok, data = C._graph(num["phone_number_id"], tok["access_token"], {
                "messaging_product": "whatsapp", "recipient_type": "individual", "to": a["phone_digits"],
                "type": "interactive",
                "interactive": {"type": "call_permission_request", "action": {"name": "call_permission_request"},
                                "body": {"text": text}}}, path="messages")
            if ok:
                cur.execute("""INSERT INTO wa_call_permissions (phone_number_id, customer_phone, tenant_id, status, requested_by)
                               VALUES (%s,%s,%s,'requested',%s)
                               ON CONFLICT (phone_number_id, customer_phone) DO UPDATE SET status='requested',
                                   requested_by=EXCLUDED.requested_by, updated_at=NOW()""",
                            (num["phone_number_id"], a["phone_digits"], pid, label))
                cur.execute("""INSERT INTO wa_message_log (tenant_id, phone_number_id, customer_phone, direction, content,
                                                           message_type, sent_by_label)
                               VALUES (%s,%s,%s,'outbound',%s,'agent_reply',%s)""",
                            (pid, num["phone_number_id"], a["phone_digits"], "[Can we call you?] " + text, label))
                set_quiet(cur, a["phone_digits"], admin_user(), True)
            log(cur, tenant_id, "whatsapp", admin_user(), channel="whatsapp",
                body="Asked on WhatsApp: can we call you?", ok=ok, outcome=None if ok else "failed")
            conn.commit()
        finally:
            cur.close(); conn.close()
        if not ok:
            return jsonify({"error": C._meta_error(data)}), 502
        return jsonify({"ok": True})

    @bp.route("/accounts/<int:tenant_id>/call/start", methods=["POST"])
    def am_call_start(tenant_id):
        if require_admin("accounts", "modify"):
            return jsonify({"error": "login"}), 401
        a = _phone(tenant_id)
        if not a:
            return jsonify({"error": "Account not found."}), 404
        sdp = (request.get_json(silent=True) or {}).get("sdp") or ""
        pid = A.platform_tenant_id()
        key, label = me()
        conn, cur = _db()
        try:
            num = _call_number(cur)
            tok, why = C._outgoing_ready(cur, pid, num["phone_number_id"]) if num else (None, "No WhatsApp number.")
            if not tok:
                return jsonify({"error": why}), 400
            ok, data = C._graph(num["phone_number_id"], tok["access_token"],
                                {"messaging_product": "whatsapp", "to": a["phone_digits"], "action": "connect",
                                 "session": {"sdp_type": "offer", "sdp": sdp}})
            if not ok:
                code = ((data or {}).get("error") or {}).get("code")
                msg = ("They haven't agreed to WhatsApp calls yet. Send \"Can we call you?\" first."
                       if code == 138006 else C._meta_error(data))
                return jsonify({"error": msg}), 400
            wa_id = ((data.get("calls") or [{}])[0]).get("id")
            name = a["contact"] or a["business"]
            cur.execute("""INSERT INTO wa_calls (tenant_id, phone_number_id, wa_call_id, direction, customer_phone,
                                                  customer_name, status, offer_sdp, started_by_key, started_by_name)
                           VALUES (%s,%s,%s,'out',%s,%s,'calling',%s,%s,%s)
                           ON CONFLICT (wa_call_id) DO UPDATE SET started_by_key=EXCLUDED.started_by_key,
                               started_by_name=EXCLUDED.started_by_name, offer_sdp=EXCLUDED.offer_sdp
                           RETURNING id""",
                        (pid, num["phone_number_id"], wa_id, a["phone_digits"], name, sdp, key, label))
            new_id = cur.fetchone()["id"]
            _bump_contacted(cur, tenant_id, admin_user())
            set_quiet(cur, a["phone_digits"], admin_user(), True)
            conn.commit()
        finally:
            cur.close(); conn.close()
        return jsonify({"ok": True, "id": new_id})

    @bp.route("/accounts/call/<int:call_id>/status")
    def am_call_status(call_id):
        if require_admin("accounts", "view"):
            return jsonify({"error": "login"}), 401
        key, _ = me()
        conn, cur = _db()
        try:
            cur.execute("""SELECT status, answer_sdp, answered_at, ended_at, duration FROM wa_calls
                            WHERE id=%s AND tenant_id=%s AND started_by_key=%s""",
                        (call_id, A.platform_tenant_id(), key))
            r = cur.fetchone()
        finally:
            cur.close(); conn.close()
        if not r:
            return jsonify({"error": "Call not found."}), 404
        return jsonify({"status": r["status"], "answer_sdp": r["answer_sdp"],
                        "answered_at": r["answered_at"].isoformat() if r["answered_at"] else None,
                        "ended": bool(r["ended_at"]) or r["status"] in ("completed", "missed", "failed", "rejected")})

    @bp.route("/accounts/call/<int:call_id>/end", methods=["POST"])
    def am_call_end(call_id):
        if require_admin("accounts", "modify"):
            return jsonify({"error": "login"}), 401
        key, _ = me()
        pid = A.platform_tenant_id()
        conn, cur = _db()
        try:
            cur.execute("SELECT * FROM wa_calls WHERE id=%s AND tenant_id=%s AND started_by_key=%s", (call_id, pid, key))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Call not found."}), 404
            tok = C._token(cur, row["phone_number_id"], pid)
            if row["wa_call_id"] and tok:
                C._graph(row["phone_number_id"], tok["access_token"],
                         {"messaging_product": "whatsapp", "call_id": row["wa_call_id"], "action": "terminate"})
            cur.execute("""UPDATE wa_calls SET status = CASE WHEN answered_at IS NOT NULL THEN 'completed' ELSE 'missed' END,
                                  ended_at = COALESCE(ended_at, NOW()),
                                  duration = COALESCE(duration, CASE WHEN answered_at IS NOT NULL
                                             THEN EXTRACT(EPOCH FROM NOW() - answered_at)::int END)
                            WHERE id=%s AND status IN ('answering','answered','calling')""", (call_id,))
            conn.commit()
        finally:
            cur.close(); conn.close()
        return jsonify({"ok": True})


# ── Admin › Inbox (2026-11-06, user: "build an inbox in admin for the account
# manager"). Every WhatsApp conversation between PhiXtra's numbers and a
# signed-up customer (an Accounts business), newest first; reply with preview,
# call, read ticks (wa_msg_status, filled by the gateway), and PhiXtra's AI
# kept quiet for chats the Account Manager handles (am_wa_quiet, checked by
# the gateway). ────────────────────────────────────────────────────────────

def set_quiet(cur, phone, by, on=True):
    pid = A.platform_tenant_id()
    d = digits(phone)
    if not pid or len(d) < 8:
        return
    if on:
        cur.execute("""INSERT INTO am_wa_quiet (tenant_id, phone, set_by) VALUES (%s,%s,%s)
                       ON CONFLICT (tenant_id, phone) DO NOTHING""", (pid, d, by))
    else:
        cur.execute("DELETE FROM am_wa_quiet WHERE tenant_id=%s AND phone=%s", (pid, d))


def is_quiet(phone) -> bool:
    conn, cur = _db()
    try:
        cur.execute("SELECT 1 FROM am_wa_quiet WHERE tenant_id=%s AND phone=%s", (A.platform_tenant_id(), digits(phone)))
        return cur.fetchone() is not None
    finally:
        cur.close(); conn.close()


def conversations():
    """[{account row + last message, last_in, last_out, needs_reply}] newest first."""
    accts = {r["phone_digits"]: r for r in rows() if len(r["phone_digits"] or "") >= 8}
    if not accts:
        return []
    pid = A.platform_tenant_id()
    _amn = (am_number() or {}).get("phone_number_id")   # only chats on the Account Manager number
    conn, cur = _db()
    try:
        cur.execute("""
            WITH m AS (SELECT regexp_replace(customer_phone,'\\D','','g') AS d, direction, content, message_type, created_at
                         FROM wa_message_log WHERE tenant_id=%s AND NOT COALESCE(is_historical, FALSE)
                          AND regexp_replace(customer_phone,'\\D','','g') = ANY(%s)
                          AND (%s IS NULL OR phone_number_id = %s))
            SELECT d, MAX(created_at) AS last_at,
                   MAX(created_at) FILTER (WHERE direction='inbound') AS last_in,
                   MAX(created_at) FILTER (WHERE direction<>'inbound' AND COALESCE(message_type,'')<>'am_auto') AS last_out,
                   (ARRAY_AGG(content ORDER BY created_at DESC))[1] AS last_text,
                   (ARRAY_AGG(direction ORDER BY created_at DESC))[1] AS last_dir
              FROM m GROUP BY d ORDER BY MAX(created_at) DESC""", (pid, list(accts), _amn, _amn))
        out = []
        for r in cur.fetchall() or []:
            a = dict(accts[r["d"]])
            a.update(r)
            a["needs_reply"] = bool(r["last_in"] and (not r["last_out"] or r["last_in"] > r["last_out"]))
            out.append(a)
        return out
    finally:
        cur.close(); conn.close()


def thread(phone):
    pid = A.platform_tenant_id()
    d = digits(phone)
    _amn = (am_number() or {}).get("phone_number_id")   # the chat on the Account Manager number
    conn, cur = _db()
    try:
        cur.execute("""SELECT m.created_at, m.direction, m.content, m.message_type, m.sent_by_label, s.delivered_at, s.read_at, s.error AS wa_error, s.status AS wa_status,
                              s.status_at
                         FROM wa_message_log m LEFT JOIN wa_msg_status s ON s.meta_message_id = m.meta_message_id
                        WHERE m.tenant_id=%s AND regexp_replace(m.customer_phone,'\\D','','g')=%s
                          AND NOT COALESCE(m.is_historical, FALSE) AND m.message_type <> 'call'
                          AND (%s IS NULL OR m.phone_number_id = %s)
                        ORDER BY m.created_at ASC LIMIT 500""", (pid, d, _amn, _amn))
        items = [{"src": "wa", **r} for r in cur.fetchall() or []]
        cur.execute("""SELECT created_at, direction, status, duration, answered_by_name, started_by_name
                         FROM wa_calls WHERE tenant_id=%s AND regexp_replace(customer_phone,'\\D','','g')=%s
                          AND (%s IS NULL OR phone_number_id = %s)""", (pid, d, _amn, _amn))
        items += [{"src": "call", **r} for r in cur.fetchall() or []]
    finally:
        cur.close(); conn.close()
    items.sort(key=lambda x: x["created_at"])
    return items


def needs_reply_count(username: str) -> int:
    try:
        return sum(1 for c in conversations() if c["needs_reply"] and c["status"] != "lost"
                   and (c["assigned_admin"] in (None, username)))
    except Exception:
        return 0


def register_inbox(bp, require_admin, admin_user):
    from flask import jsonify

    @bp.app_context_processor
    def _inbox_badge():
        def am_inbox_count():
            try:
                if not session.get("portal_admin_logged_in"):
                    return 0
                return needs_reply_count(admin_user())
            except Exception:
                return 0
        return {"am_inbox_count": am_inbox_count}

    @bp.route("/inbox")
    @bp.route("/inbox/<int:tenant_id>")
    def am_inbox(tenant_id=None):
        r = require_admin("accounts", "view")
        if r: return r
        me = admin_user()
        show = request.args.get("show") or "all"
        q = (request.args.get("q") or "").strip().lower()
        convs = conversations()
        def keep(c):
            if show == "reply" and not c["needs_reply"]: return False
            if show == "mine" and c["assigned_admin"] != me: return False
            if q and q not in " ".join(str(c.get(k) or "") for k in ("business", "contact", "email", "phone_number")).lower():
                return False
            return True
        shown = [c for c in convs if keep(c)]
        a = None
        if tenant_id is not None:
            found = rows(only_tenant=tenant_id)
            a = found[0] if found else None
        elif shown:
            return redirect(url_for("portal_admin.am_inbox", tenant_id=shown[0]["tenant_id"], show=show, q=q or None))
        ctx = {}
        if a:
            window = wa_window_open(a["phone_number"])
            ctx = dict(items=thread(a["phone_number"]), wa_window=window,
                       checkin_ready=(not window and checkin_template_ready()),
                       checkin_preview=checkin_look((a["first_name"] or "").strip(), (a.get("setup") or {}).get("stuck")),
                       wa_templates=templates_for((a["first_name"] or "").strip(), (a.get("setup") or {}).get("stuck")),
                       call_ask_preview=call_ask_text((a["first_name"] or "").strip()),
                       quiet=is_quiet(a["phone_number"]), wa_numbers=am_numbers())
        latest = max([c["last_at"] for c in convs], default=None)
        return render_template("portal/admin_inbox.html", convs=shown, a=a, show=show, q=q, me=me,
                               counts={"all": len(convs), "reply": sum(1 for c in convs if c["needs_reply"]),
                                       "mine": sum(1 for c in convs if c["assigned_admin"] == me)},
                               latest=latest.isoformat() if latest else "", manager_name=manager_name,
                               can_modify=(require_admin("accounts", "modify") is None), **ctx)

    @bp.route("/inbox/poll")
    def am_inbox_poll():
        if require_admin("accounts", "view"):
            return jsonify({}), 401
        pid = A.platform_tenant_id()
        conn, cur = _db()
        try:
            cur.execute("""SELECT MAX(m.created_at) AS t, MAX(s.status_at) AS s FROM wa_message_log m
                             LEFT JOIN wa_msg_status s ON s.meta_message_id = m.meta_message_id
                            WHERE m.tenant_id=%s AND m.created_at > NOW() - INTERVAL '30 days'""", (pid,))
            r = cur.fetchone()
        finally:
            cur.close(); conn.close()
        return jsonify({"latest": r["t"].isoformat() if r["t"] else "", "status": r["s"].isoformat() if r["s"] else ""})

    @bp.route("/inbox/<int:tenant_id>/send", methods=["POST"])
    def am_inbox_send(tenant_id):
        r = require_admin("accounts", "modify")
        if r: return r
        me = admin_user()
        found = rows(only_tenant=tenant_id)
        if not found:
            return redirect(url_for("portal_admin.am_inbox"))
        a = found[0]
        use_tpl = request.form.get("mode") == "template"
        text = (request.form.get("text") or "").strip()[:4000]
        back = redirect(url_for("portal_admin.am_inbox", tenant_id=tenant_id))
        if not use_tpl and not text:
            return back
        if not use_tpl and not wa_window_open(a["phone_number"]):
            flash("More than 24 hours since their last message: send the check-in message instead.", "warning")
            return back
        if use_tpl and (request.form.get("template") or active_template()) not in approved_templates():
            flash("That WhatsApp template isn't approved by Meta yet.", "warning")
            return back
        ok, err = send_whatsapp(tenant_id, a["phone_number"], text, me, use_template=use_tpl,
                                first_name=(a["first_name"] or "").strip(), manager=AM_NAME,
                                stuck=(a.get("setup") or {}).get("stuck"),
                                template_name=request.form.get("template") or None)
        insert_audit_log(admin_username=me, action="am_whatsapp_sent" if ok else "am_whatsapp_failed",
                         tenant_id=tenant_id, details={"template": use_tpl, "error": err or None, "from": "inbox"})
        if not ok:
            flash(f"WhatsApp didn't send: {err}", "danger")
        return back

    @bp.route("/inbox/<int:tenant_id>/ai", methods=["POST"])
    def am_inbox_ai(tenant_id):
        r = require_admin("accounts", "modify")
        if r: return r
        me = admin_user()
        found = rows(only_tenant=tenant_id)
        if found:
            on = request.form.get("ai") == "on"
            conn, cur = _db()
            try:
                set_quiet(cur, found[0]["phone_number"], me, not on)
                log(cur, tenant_id, "status", me,
                    body="PhiXtra's AI may answer this chat again" if on else "PhiXtra's AI stopped for this chat")
                conn.commit()
            finally:
                cur.close(); conn.close()
        return redirect(url_for("portal_admin.am_inbox", tenant_id=tenant_id))
