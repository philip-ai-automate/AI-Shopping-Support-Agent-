"""Automatic follow-ups (2026-10-29, approved mock-up
https://claude.ai/artifact/1XzJNWytsnK4BMzMrsbXYg).

A follow-up (an email sequence underneath) can now mix Email and WhatsApp
messages. Each business decides, in CRM › Pipeline Settings, WHEN follow-ups
start (its own rules: which stage / signal, how many days, which follow-up,
who is told) and HOW messages go out (hours, days, time zone, most WhatsApp
messages per deal, automatic or approve-first, which number sends). Nothing
here is fixed per business: PhiXtra only seeds suggested rules, switched off
until the business picks a follow-up for them.

WhatsApp and Meta's 24-hour rule: if the customer messaged the business in
the last 24 hours, the step's words go as a normal message (free); otherwise
the approved template is sent (Meta charges the business).

A person stops getting follow-ups when they reply (email or WhatsApp), ask
to stop, their deal closes, or — for follow-ups started by a rule or from a
deal — someone on the team contacts them.

Tables: followup_settings (one row per business), followup_rules,
followup_rule_runs (one start per deal per rule per stage entry),
followup_wa_sends (every WhatsApp follow-up sent). New columns on
email_sequence_steps (channel, wa) and email_sequence_people (phone, origin,
rule_id); email_sequence_people.email may now be empty (WhatsApp-only).
"""
import datetime as _dt
import json as _json
import os
import re
import time as _time

import psycopg2.extras
import requests as _req

from db import get_db_connection

FU_LABEL = "Automatic follow-up"
GRAPH = os.getenv("META_GRAPH_URL", "https://graph.facebook.com/v19.0")

# What a rule can react to. The business picks the stage / days / follow-up.
TRIGGERS = [
    ("stage_no_reply", "When a deal moves into a stage and there's no reply"),
    ("booking_click", "When the customer clicks my booking link but doesn't book"),
    ("wa_price_quiet", "When the customer asks for a price on WhatsApp and goes quiet"),
]
TRIGGER_KEYS = {k for k, _ in TRIGGERS}

# What fills each blank ({{1}}, {{2}} …) of a WhatsApp template.
BLANK_FIELDS = [
    ("first_name", "Their first name"),
    ("contact_name", "Their full name"),
    ("company", "Their company or deal name"),
    ("business", "Your business name"),
    ("owner", "Deal owner's first name"),
    ("text", "Words I type"),
]
BLANK_KEYS = {k for k, _ in BLANK_FIELDS}

# What a rule set to "remind" asks the person to do (nothing goes to the
# customer; on the day the deal's Next follow-up is set for them).
REMIND_ACTIONS = [("call", "call them"), ("whatsapp", "send them a WhatsApp"), ("email", "email them"),
                  ("linkedin", "contact them on LinkedIn"), ("meeting", "hold a meeting")]
REMIND_ACTION_KEYS = {k for k, _ in REMIND_ACTIONS}

DAY_NAMES = [(1, "Mon"), (2, "Tue"), (3, "Wed"), (4, "Thu"), (5, "Fri"), (6, "Sat"), (0, "Sun")]

DEFAULT_SETTINGS = {
    "wa_number_id": None,        # wa_tenants.id; None = the number they last chatted with, else the first one
    "reassign_to": "owner",      # 'owner' or 'team:<id>' — who gets a removed team member's deals
    "no_wa_mode": "email_ask",   # 'email_ask' (email only + tell owner) or 'skip'
    "hours_from": 9, "hours_to": 18,
    "days": [1, 2, 3, 4, 5],
    "tz_mode": "customer",       # 'customer' (from their number) or 'business'
    "business_tz": "Africa/Lagos",
    "max_wa": 3,
    "send_mode": "auto",         # 'auto' or 'approve'
}

_tables_ready = False


def _conn():
    return get_db_connection()


def ensure_tables():
    """DDL only when really missing (no ALTER on every start — see
    feedback: no DDL on hot tables), each with a short lock timeout."""
    global _tables_ready
    if _tables_ready:
        return
    conn = _conn()
    try:
        cur = conn.cursor()

        def _has_table(t):
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (t,))
            return cur.fetchone()[0]

        def _has_col(t, c):
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (t, c))
            return bool(cur.fetchone())

        if not _has_table("followup_settings"):
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS followup_settings (
                               tenant_id   INTEGER PRIMARY KEY,
                               settings    JSONB NOT NULL DEFAULT '{}'::jsonb,
                               seeded      BOOLEAN NOT NULL DEFAULT FALSE,
                               updated_by  VARCHAR(200),
                               updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        if not _has_table("followup_rules"):
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS followup_rules (
                               id           BIGSERIAL PRIMARY KEY,
                               tenant_id    INTEGER NOT NULL,
                               enabled      BOOLEAN NOT NULL DEFAULT FALSE,
                               position     INTEGER NOT NULL DEFAULT 0,
                               trigger      VARCHAR(30) NOT NULL,
                               stage        VARCHAR(40),
                               days         INTEGER NOT NULL DEFAULT 3,
                               sequence_id  BIGINT,
                               notify       VARCHAR(40) NOT NULL DEFAULT 'owner',
                               created_by   VARCHAR(200),
                               created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                               updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS followup_rules_t ON followup_rules (tenant_id, position)")
        for col, ddl in (("do_what", "ALTER TABLE followup_rules ADD COLUMN IF NOT EXISTS do_what VARCHAR(10) NOT NULL DEFAULT 'followup'"),
                         ("remind_action", "ALTER TABLE followup_rules ADD COLUMN IF NOT EXISTS remind_action VARCHAR(12)"),
                         ("remind_note", "ALTER TABLE followup_rules ADD COLUMN IF NOT EXISTS remind_note TEXT")):
            if _has_table("followup_rules") and not _has_col("followup_rules", col):
                cur.execute("SET LOCAL lock_timeout = '5s'")
                cur.execute(ddl)
        if not _has_table("followup_rule_runs"):
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS followup_rule_runs (
                               id          BIGSERIAL PRIMARY KEY,
                               tenant_id   INTEGER NOT NULL,
                               rule_id     BIGINT NOT NULL,
                               lead_id     BIGINT,
                               contact_id  BIGINT,
                               phone       VARCHAR(32),
                               mark        VARCHAR(80) NOT NULL,   -- what started it (e.g. stage entry time)
                               person_id   BIGINT,
                               created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS followup_rule_runs_once
                           ON followup_rule_runs (rule_id, COALESCE(lead_id, 0), COALESCE(phone, ''), mark)""")
        if not _has_table("followup_wa_sends"):
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS followup_wa_sends (
                               id          BIGSERIAL PRIMARY KEY,
                               tenant_id   INTEGER NOT NULL,
                               person_id   BIGINT,
                               sequence_id BIGINT,
                               position    INTEGER,
                               lead_id     BIGINT,
                               contact_id  BIGINT,
                               phone       VARCHAR(32) NOT NULL,
                               mode        VARCHAR(10) NOT NULL,     -- 'template' (paid) or 'text' (free, inside 24h)
                               meta_id     VARCHAR(120),
                               sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS followup_wa_sends_lead ON followup_wa_sends (tenant_id, lead_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS followup_wa_sends_phone ON followup_wa_sends (tenant_id, phone)")
        for col, ddl in (("channel", "ALTER TABLE email_sequence_steps ADD COLUMN IF NOT EXISTS channel VARCHAR(10) NOT NULL DEFAULT 'email'"),
                         ("wa", "ALTER TABLE email_sequence_steps ADD COLUMN IF NOT EXISTS wa JSONB")):
            if not _has_col("email_sequence_steps", col):
                cur.execute("SET LOCAL lock_timeout = '5s'")
                cur.execute(ddl)
        for col, ddl in (("phone", "ALTER TABLE email_sequence_people ADD COLUMN IF NOT EXISTS phone VARCHAR(32)"),
                         ("origin", "ALTER TABLE email_sequence_people ADD COLUMN IF NOT EXISTS origin VARCHAR(10) NOT NULL DEFAULT 'list'"),
                         ("rule_id", "ALTER TABLE email_sequence_people ADD COLUMN IF NOT EXISTS rule_id BIGINT")):
            if not _has_col("email_sequence_people", col):
                cur.execute("SET LOCAL lock_timeout = '5s'")
                cur.execute(ddl)
        cur.execute("""SELECT is_nullable FROM information_schema.columns
                       WHERE table_name='email_sequence_people' AND column_name='email'""")
        r = cur.fetchone()
        if r and r[0] == "NO":
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("ALTER TABLE email_sequence_people ALTER COLUMN email DROP NOT NULL")
        cur.execute("SELECT to_regclass('esp_seq_lead_live_idx') IS NOT NULL")
        if not cur.fetchone()[0]:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("""CREATE INDEX IF NOT EXISTS esp_seq_lead_live_idx ON email_sequence_people (tenant_id, lead_id)
                           WHERE status IN ('active','ready','sending')""")
        conn.commit()
        _tables_ready = True
    except Exception as e:
        conn.rollback()
        print("⚠️ auto_followups.ensure_tables error:", e)
    finally:
        conn.close()


# ── Settings + rules ─────────────────────────────────────────────────────────

def _clean_settings(raw: dict) -> dict:
    s = dict(DEFAULT_SETTINGS)
    raw = raw or {}
    for k in s:
        if k in raw and raw[k] is not None:
            s[k] = raw[k]
    try:
        s["hours_from"] = max(0, min(23, int(s["hours_from"])))
        s["hours_to"] = max(s["hours_from"] + 1, min(24, int(s["hours_to"])))
    except (TypeError, ValueError):
        s["hours_from"], s["hours_to"] = 9, 18
    days = [int(d) for d in (s.get("days") or []) if str(d).isdigit() and 0 <= int(d) <= 6]
    s["days"] = sorted(set(days)) or [1, 2, 3, 4, 5]
    try:
        s["max_wa"] = max(1, min(10, int(s["max_wa"])))
    except (TypeError, ValueError):
        s["max_wa"] = 3
    s["no_wa_mode"] = s["no_wa_mode"] if s["no_wa_mode"] in ("email_ask", "skip") else "email_ask"
    s["tz_mode"] = s["tz_mode"] if s["tz_mode"] in ("customer", "business") else "customer"
    s["send_mode"] = s["send_mode"] if s["send_mode"] in ("auto", "approve") else "auto"
    rt = str(s.get("reassign_to") or "owner")
    s["reassign_to"] = rt if (rt == "owner" or re.fullmatch(r"team:\d+", rt)) else "owner"
    try:
        s["wa_number_id"] = int(s["wa_number_id"]) if s.get("wa_number_id") else None
    except (TypeError, ValueError):
        s["wa_number_id"] = None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(s["business_tz"])
    except Exception:
        s["business_tz"] = "Africa/Lagos"
    return s


def get_settings(tenant_id: int, cur=None) -> dict:
    ensure_tables()
    own = cur is None
    conn = _conn() if own else None
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if own else cur
    try:
        c.execute("SELECT settings FROM followup_settings WHERE tenant_id=%s", (tenant_id,))
        r = c.fetchone()
        return _clean_settings((r or {}).get("settings") if r else {})
    finally:
        if own:
            c.close(); conn.close()


def save_settings(tenant_id: int, data: dict, actor_label: str) -> dict:
    ensure_tables()
    s = _clean_settings(data)
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO followup_settings (tenant_id, settings, updated_by, updated_at)
                       VALUES (%s,%s,%s,NOW())
                       ON CONFLICT (tenant_id) DO UPDATE SET settings=EXCLUDED.settings,
                           updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                    (tenant_id, _json.dumps(s), actor_label))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return s


# Suggested starting rules. Switched OFF and with no follow-up chosen, so
# nothing is ever sent until the business picks a follow-up and turns one on.
_SUGGESTED = [
    ("stage_no_reply", "proposal_sent", 3, "owner"),
    ("booking_click", None, 1, "owner"),
    ("wa_price_quiet", None, 1, "none"),
    ("stage_no_reply", "negotiating", 5, "owner"),
]


def seed_rules(tenant_id: int) -> None:
    ensure_tables()
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO followup_settings (tenant_id, settings, seeded) VALUES (%s,'{}'::jsonb,TRUE)
                       ON CONFLICT (tenant_id) DO UPDATE SET seeded=TRUE
                       WHERE followup_settings.seeded IS NOT TRUE RETURNING tenant_id""", (tenant_id,))
        if cur.fetchone():
            for pos, (trig, stage, days, notify) in enumerate(_SUGGESTED, start=1):
                cur.execute("""INSERT INTO followup_rules (tenant_id, enabled, position, trigger, stage, days, notify, created_by)
                               VALUES (%s,FALSE,%s,%s,%s,%s,%s,'Suggested by PhiXtra')""",
                            (tenant_id, pos, trig, stage, days, notify))
        conn.commit()
    finally:
        cur.close(); conn.close()


def list_rules(tenant_id: int) -> list:
    ensure_tables()
    conn = _conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT r.*, s.name AS sequence_name, s.status AS sequence_status
                       FROM followup_rules r LEFT JOIN email_sequences s ON s.id = r.sequence_id AND s.tenant_id = r.tenant_id
                       WHERE r.tenant_id=%s ORDER BY r.position, r.id""", (tenant_id,))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def save_rule(tenant_id: int, rule_id, data: dict, actor_label: str):
    """Create (rule_id None) or update one rule. Returns (ok, message)."""
    ensure_tables()
    trig = data.get("trigger")
    if trig not in TRIGGER_KEYS:
        return False, "Choose what starts the follow-up."
    stage = (data.get("stage") or "").strip() or None
    if trig == "stage_no_reply" and not stage:
        return False, "Choose the stage."
    try:
        days = max(0, min(90, int(data.get("days") or 0)))
    except (TypeError, ValueError):
        return False, "Write the number of days."
    seq_id = data.get("sequence_id")
    seq_id = int(seq_id) if str(seq_id or "").isdigit() else None
    do_what = "remind" if data.get("do_what") == "remind" else "followup"
    notify = str((data.get("remind_who") if do_what == "remind" else data.get("notify")) or "owner")
    if not (notify in ("owner", "none", "account_owner") or re.fullmatch(r"team:\d+", notify)):
        notify = "owner"
    if do_what == "remind" and notify == "none":
        notify = "owner"
    remind_action = data.get("remind_action") if data.get("remind_action") in REMIND_ACTION_KEYS else "call"
    remind_note = " ".join(str(data.get("remind_note") or "").split())[:300] or None
    enabled = bool(data.get("enabled"))
    if enabled and do_what == "followup" and not seq_id:
        return False, "Choose which follow-up to start before switching the rule on."
    conn = _conn(); cur = conn.cursor()
    try:
        if seq_id:
            cur.execute("SELECT 1 FROM email_sequences WHERE id=%s AND tenant_id=%s", (seq_id, tenant_id))
            if not cur.fetchone():
                return False, "That follow-up was not found."
        if rule_id:
            cur.execute("""UPDATE followup_rules SET trigger=%s, stage=%s, days=%s, sequence_id=%s, notify=%s,
                               enabled=%s, do_what=%s, remind_action=%s, remind_note=%s, updated_at=NOW()
                           WHERE id=%s AND tenant_id=%s""",
                        (trig, stage if trig == "stage_no_reply" else None, days, seq_id, notify, enabled,
                         do_what, remind_action, remind_note, int(rule_id), tenant_id))
            if not cur.rowcount:
                return False, "Rule not found."
        else:
            cur.execute("SELECT COALESCE(max(position),0)+1 FROM followup_rules WHERE tenant_id=%s", (tenant_id,))
            pos = cur.fetchone()[0]
            cur.execute("""INSERT INTO followup_rules (tenant_id, enabled, position, trigger, stage, days, sequence_id, notify,
                               created_by, do_what, remind_action, remind_note)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (tenant_id, enabled, pos, trig, stage if trig == "stage_no_reply" else None, days,
                         seq_id, notify, actor_label, do_what, remind_action, remind_note))
        conn.commit()
        return True, "Rule saved."
    finally:
        cur.close(); conn.close()


def delete_rule(tenant_id: int, rule_id: int) -> bool:
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM followup_rules WHERE id=%s AND tenant_id=%s", (rule_id, tenant_id))
        ok = bool(cur.rowcount)
        conn.commit()
        return ok
    finally:
        cur.close(); conn.close()


def set_rule_enabled(tenant_id: int, rule_id: int, on: bool):
    conn = _conn(); cur = conn.cursor()
    try:
        if on:
            cur.execute("SELECT sequence_id, do_what FROM followup_rules WHERE id=%s AND tenant_id=%s", (rule_id, tenant_id))
            r = cur.fetchone()
            if not r:
                return False, "Rule not found."
            if r[1] != "remind" and not r[0]:
                return False, "Choose which follow-up to start before switching the rule on."
        cur.execute("UPDATE followup_rules SET enabled=%s, updated_at=NOW() WHERE id=%s AND tenant_id=%s",
                    (bool(on), rule_id, tenant_id))
        conn.commit()
        return True, ("Rule switched on." if on else "Rule switched off.")
    finally:
        cur.close(); conn.close()


# ── People, numbers, time ────────────────────────────────────────────────────

def digits(p) -> str:
    return re.sub(r"\D", "", str(p or ""))


# When a number could be in several zones with the same clock (+44 → London,
# Guernsey, Jersey, Isle of Man), name the main one.
_MAIN_ZONES = ("Europe/London", "Africa/Lagos", "America/New_York", "Europe/Paris", "Africa/Johannesburg")


def customer_tz(phone: str, fallback: str) -> str:
    """Time zone from the customer's WhatsApp number (country code); the
    business's own when it can't be told."""
    d = digits(phone)
    if d:
        try:
            import phonenumbers as _pn
            from phonenumbers import timezone as _pntz
            zones = [z for z in _pntz.time_zones_for_number(_pn.parse("+" + d)) if z and z != "Etc/Unknown"]
            for main in _MAIN_ZONES:
                if main in zones:
                    return main
            if zones:
                return zones[0]
        except Exception:
            pass
    return fallback


def sending_time(settings: dict, phone: str, now=None):
    """(ok_now, next_open_utc) for the business's hours/days in the chosen
    time zone (the customer's, from their number, or the business's)."""
    from zoneinfo import ZoneInfo
    now = now or _dt.datetime.now(_dt.timezone.utc)
    tzname = customer_tz(phone, settings["business_tz"]) if settings["tz_mode"] == "customer" else settings["business_tz"]
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    fh, th, days = settings["hours_from"], settings["hours_to"], settings["days"]

    def _ok(t):
        return (t.isoweekday() % 7) in days and fh <= t.hour + t.minute / 60 < th

    if _ok(local):
        return True, now
    d = local
    for _ in range(9):
        start = d.replace(hour=fh, minute=0, second=0, microsecond=0)
        if start > local and (start.isoweekday() % 7) in days:
            return False, start.astimezone(_dt.timezone.utc)
        d = (d + _dt.timedelta(days=1)).replace(hour=0, minute=0)
    return False, now + _dt.timedelta(hours=1)


def person_whatsapp(cur, tenant_id: int, lead_id=None, contact_id=None) -> str:
    """The WhatsApp number to use: the deal's, else its contact's. Never the
    Phone number (WhatsApp-only rule)."""
    if lead_id:
        cur.execute("SELECT whatsapp_number, wa_contact_id FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s",
                    (lead_id, tenant_id))
        r = cur.fetchone()
        if r:
            if digits(r["whatsapp_number"]) and len(digits(r["whatsapp_number"])) >= 8:
                return digits(r["whatsapp_number"])
            contact_id = contact_id or r["wa_contact_id"]
    if contact_id:
        cur.execute("SELECT whatsapp_number FROM wa_contacts WHERE id=%s AND tenant_id=%s", (contact_id, tenant_id))
        r = cur.fetchone()
        if r and len(digits(r["whatsapp_number"])) >= 8:
            return digits(r["whatsapp_number"])
    return ""


def opted_out(cur, tenant_id: int, phone: str) -> bool:
    cur.execute("SELECT 1 FROM wa_contacts WHERE tenant_id=%s AND whatsapp_number=%s AND opted_out LIMIT 1",
                (tenant_id, phone))
    return bool(cur.fetchone())


def window_open(cur, tenant_id: int, phone: str) -> bool:
    """Customer messaged us in the last 24 hours (5 minutes kept spare)."""
    cur.execute("""SELECT 1 FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s AND direction='inbound'
                   AND created_at > NOW() - INTERVAL '23 hours 55 minutes' LIMIT 1""", (tenant_id, phone))
    return bool(cur.fetchone())


def wa_sends_for(cur, tenant_id: int, lead_id, phone) -> int:
    cur.execute("""SELECT count(*) AS n FROM followup_wa_sends WHERE tenant_id=%s
                   AND ((%s::bigint IS NOT NULL AND lead_id=%s) OR phone=%s)""",
                (tenant_id, lead_id, lead_id, phone or "-"))
    return int(cur.fetchone()["n"])


def sending_number(cur, tenant_id: int, settings: dict, phone: str):
    """The connected number (with token) follow-ups go from: the one the
    business chose; else the one this customer last chatted with; else the
    business's first active number."""
    base = """SELECT id, phone_number_id, waba_id, access_token, display_phone_number
              FROM wa_tenants WHERE tenant_id=%s AND active AND access_token IS NOT NULL"""
    if settings.get("wa_number_id"):
        cur.execute(base + " AND id=%s", (tenant_id, settings["wa_number_id"]))
        r = cur.fetchone()
        if r:
            return r
    if phone:
        cur.execute("""SELECT phone_number_id FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s
                       ORDER BY created_at DESC LIMIT 1""", (tenant_id, phone))
        last = cur.fetchone()
        if last:
            cur.execute(base + " AND phone_number_id=%s", (tenant_id, last["phone_number_id"]))
            r = cur.fetchone()
            if r:
                return r
    cur.execute(base + " ORDER BY id LIMIT 1", (tenant_id,))
    return cur.fetchone()


_quality_cache = {}


def number_quality_ok(num) -> bool:
    """False when Meta has lowered this number's quality (YELLOW / RED).
    Checked at most once an hour per number; a failed check never blocks."""
    key = num["phone_number_id"]
    hit = _quality_cache.get(key)
    if hit and _time.time() - hit[0] < 3600:
        return hit[1]
    ok = True
    try:
        r = _req.get(f"{GRAPH}/{key}", params={"fields": "quality_rating", "access_token": num["access_token"]},
                     timeout=8)
        q = (r.json() or {}).get("quality_rating") if r.status_code == 200 else None
        ok = q not in ("YELLOW", "RED")
    except Exception:
        ok = True
    _quality_cache[key] = (_time.time(), ok)
    return ok


# ── Filling a WhatsApp step ──────────────────────────────────────────────────

def person_context(cur, tenant_id: int, lead_id=None, contact_id=None) -> dict:
    ctx = {"first_name": "", "contact_name": "", "company": "", "business": "", "owner": ""}
    cur.execute("SELECT name FROM tenants WHERE id=%s", (tenant_id,))
    r = cur.fetchone()
    ctx["business"] = (r or {}).get("name") or ""
    if lead_id:
        cur.execute("""SELECT customer_name, contact_person, assigned_to, wa_contact_id
                       FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s""", (lead_id, tenant_id))
        l = cur.fetchone()
        if l:
            ctx["company"] = l["customer_name"] or ""
            ctx["contact_name"] = l["contact_person"] or ""
            ctx["owner"] = (l["assigned_to"] or "").split(" ")[0]
            contact_id = contact_id or l["wa_contact_id"]
    if contact_id and not ctx["contact_name"]:
        cur.execute("SELECT display_name, contact_person FROM wa_contacts WHERE id=%s AND tenant_id=%s",
                    (contact_id, tenant_id))
        c = cur.fetchone()
        if c:
            ctx["contact_name"] = c["contact_person"] or ""
            ctx["company"] = ctx["company"] or c["display_name"] or ""
    ctx["first_name"] = (ctx["contact_name"] or "").split(" ")[0]
    return ctx


def fill_values(wa: dict, ctx: dict) -> list:
    """One value per blank. Empty person fields fall back to a polite word,
    because WhatsApp refuses empty blanks."""
    out = []
    fall = {"first_name": "there", "contact_name": "there", "company": "your business",
            "business": "us", "owner": "our team"}
    for b in (wa.get("blanks") or []):
        kind = (b or {}).get("kind")
        if kind == "text":
            v = (b.get("text") or "").strip()
        else:
            v = (ctx.get(kind) or "").strip() or fall.get(kind, "")
        out.append(re.sub(r"\s+", " ", v)[:1000] or "-")
    return out


def filled_body(wa: dict, values: list) -> str:
    body = wa.get("body") or ""
    return re.sub(r"\{\{\s*(\d+)\s*\}\}",
                  lambda m: values[int(m.group(1)) - 1] if int(m.group(1)) - 1 < len(values) else "", body)


def _log_send(cur, tenant_id, num, phone, text, meta_id, p, step, mode):
    cur.execute("""INSERT INTO wa_message_log (tenant_id, phone_number_id, customer_phone, direction, content,
                       message_type, meta_message_id, sent_by_label)
                   VALUES (%s,%s,%s,'outbound',%s,'followup',%s,%s)""",
                (tenant_id, num["phone_number_id"], phone, text, meta_id, FU_LABEL))
    cur.execute("""INSERT INTO followup_wa_sends (tenant_id, person_id, sequence_id, position, lead_id, contact_id,
                       phone, mode, meta_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, p["id"], p["sequence_id"], step["position"], p.get("lead_id"), p.get("contact_id"),
                 phone, mode, meta_id))


def send_wa_step(cur, p: dict, step: dict) -> tuple:
    """Send one WhatsApp follow-up. Returns (result, message, retry_at):
    result 'sent' | 'skip' (move on to the next message) | 'wait' (try again
    at retry_at) | 'stop' (stop this person, message = reason)."""
    tenant_id = int(p["tenant_id"])
    s = get_settings(tenant_id, cur)
    wa = step.get("wa") or {}
    if isinstance(wa, str):
        try:
            wa = _json.loads(wa)
        except Exception:
            wa = {}
    if not wa.get("template"):
        return "skip", f"Message {step['position']} has no WhatsApp message chosen.", None
    phone = digits(p.get("phone")) or person_whatsapp(cur, tenant_id, p.get("lead_id"), p.get("contact_id"))
    if not phone:
        return "skip", "No WhatsApp number on this deal.", None
    if opted_out(cur, tenant_id, phone):
        return "stop", "Asked to stop on WhatsApp", None
    if wa_sends_for(cur, tenant_id, p.get("lead_id"), phone) >= s["max_wa"]:
        return "skip", f"Already had {s['max_wa']} WhatsApp follow-ups.", None
    if p.get("origin") in ("rule", "deal"):
        ok_now, nxt = sending_time(s, phone)
        if not ok_now:
            return "wait", "Waiting for the customer's working hours.", nxt
    num = sending_number(cur, tenant_id, s, phone)
    if not num:
        return "wait", "No connected WhatsApp number.", _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=6)
    if not number_quality_ok(num):
        return "wait", "Paused: Meta lowered this number's quality.", _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=6)
    ctx = person_context(cur, tenant_id, p.get("lead_id"), p.get("contact_id"))
    values = fill_values(wa, ctx)
    text = filled_body(wa, values)
    headers = {"Authorization": f"Bearer {num['access_token']}"}
    if window_open(cur, tenant_id, phone) and text.strip():
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": phone,
                   "type": "text", "text": {"body": text}}
        mode = "text"
    else:
        tpl = {"name": wa["template"], "language": {"code": wa.get("language") or "en"}}
        if values:
            tpl["components"] = [{"type": "body", "parameters": [{"type": "text", "text": v} for v in values]}]
        payload = {"messaging_product": "whatsapp", "to": phone, "type": "template", "template": tpl}
        mode = "template"
    try:
        r = _req.post(f"{GRAPH}/{num['phone_number_id']}/messages", headers=headers, json=payload, timeout=15)
    except Exception as e:
        print("⚠️ follow-up WhatsApp send error:", e)
        return "wait", "Could not reach WhatsApp.", _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=30)
    if r.status_code != 200:
        print(f"⚠️ [FOLLOW-UP WA] tenant={tenant_id} to={phone} {r.status_code} {r.text[:300]}")
        try:
            err = r.json().get("error", {})
            why = (err.get("error_data") or {}).get("details") or err.get("message") or ""
        except Exception:
            why = ""
        return "stop", ("WhatsApp did not accept the message" + (f": {why}" if why else ""))[:200], None
    try:
        meta_id = (r.json().get("messages") or [{}])[0].get("id")
    except Exception:
        meta_id = None
    shown = text if mode == "text" else text + ("\n\n" + "  ".join(f"[ {b} ]" for b in wa.get("buttons") or []) if wa.get("buttons") else "")
    _log_send(cur, tenant_id, num, phone, shown, meta_id, p, step, mode)
    return "sent", f"WhatsApp message {step['position']} sent.", None


# ── Stopping ─────────────────────────────────────────────────────────────────

_TEAM_CONTACT_ACTIONS = ("call", "wa_sent", "messenger_sent", "webchat_sent", "reply_logged", "linkedin",
                         "email_sent", "demo_held", "meeting")


def extra_stop_reason(cur, p: dict):
    """Reasons beyond the email ones: replied or asked to stop on WhatsApp;
    for follow-ups started by a rule or from a deal, the team contacted them."""
    tenant_id = int(p["tenant_id"])
    phone = digits(p.get("phone"))
    if not phone and (p.get("lead_id") or p.get("contact_id")):
        phone = person_whatsapp(cur, tenant_id, p.get("lead_id"), p.get("contact_id"))
    if phone:
        cur.execute("""SELECT 1 FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s AND direction='inbound'
                       AND created_at >= %s LIMIT 1""", (tenant_id, phone, p["watch_from"]))
        if cur.fetchone():
            return "Replied on WhatsApp"
        if p.get("origin") in ("rule", "deal") and opted_out(cur, tenant_id, phone):
            return "Asked to stop on WhatsApp"
    if p.get("origin") in ("rule", "deal"):
        if p.get("lead_id"):
            cur.execute("""SELECT 1 FROM staff_activity WHERE tenant_id=%s AND lead_id=%s AND NOT COALESCE(auto, FALSE)
                           AND action = ANY(%s) AND created_at >= %s LIMIT 1""",
                        (tenant_id, p["lead_id"], list(_TEAM_CONTACT_ACTIONS), p["watch_from"]))
            if cur.fetchone():
                return "Your team contacted them"
        if phone:
            cur.execute("""SELECT 1 FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s AND direction='outbound'
                           AND message_type='agent_reply' AND COALESCE(sent_by_label,'') <> %s
                           AND created_at >= %s LIMIT 1""", (tenant_id, phone, FU_LABEL, p["watch_from"]))
            if cur.fetchone():
                return "Your team contacted them"
    return None


# ── Telling people ───────────────────────────────────────────────────────────

def _ensure_notices(cur):
    cur.execute("SELECT to_regclass('followup_notices') IS NOT NULL AS ok")
    r = cur.fetchone()
    if not (r["ok"] if isinstance(r, dict) else r[0]):
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS followup_notices (
                           id         BIGSERIAL PRIMARY KEY,
                           tenant_id  INTEGER NOT NULL,
                           ref        VARCHAR(120) NOT NULL,
                           created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS followup_notices_once ON followup_notices (tenant_id, ref)")


def _recipient_emails(cur, tenant_id: int, who: str, lead_id=None) -> list:
    """Email addresses for 'owner' (the deal owner, else the account owner),
    'account_owner', 'team:<id>' or 'none'."""
    if who == "none":
        return []
    key = who
    if who == "owner" and lead_id:
        cur.execute("SELECT assigned_key FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s", (lead_id, tenant_id))
        r = cur.fetchone()
        key = (r or {}).get("assigned_key") or "account_owner"
    if key.startswith("team:") and key[5:].isdigit():
        cur.execute("SELECT email FROM team_members WHERE id=%s AND tenant_id=%s AND is_active", (int(key[5:]), tenant_id))
        r = cur.fetchone()
        if r and r["email"]:
            return [r["email"]]
        key = "account_owner"
    if key.startswith("owner:") and key[6:].isdigit():
        cur.execute("SELECT email FROM customers WHERE id=%s AND tenant_id=%s", (int(key[6:]), tenant_id))
        r = cur.fetchone()
        if r and r["email"]:
            return [r["email"]]
    cur.execute("SELECT email FROM customers WHERE tenant_id=%s AND email IS NOT NULL ORDER BY id LIMIT 1", (tenant_id,))
    r = cur.fetchone()
    return [r["email"]] if r else []


def notify(cur, tenant_id: int, who: str, ref: str, subject: str, lines: list, lead_id=None, link: str = "") -> bool:
    """One email per (business, ref) — never repeated. Returns True if sent."""
    if who == "none":
        return False
    _ensure_notices(cur)
    cur.execute("""INSERT INTO followup_notices (tenant_id, ref) VALUES (%s,%s)
                   ON CONFLICT (tenant_id, ref) DO NOTHING RETURNING id""", (tenant_id, ref[:120]))
    if not cur.fetchone():
        return False
    to = _recipient_emails(cur, tenant_id, who, lead_id)
    cur.connection.commit()
    if not to:
        return False
    import html as _html
    from portal_utils import send_email
    body = "".join(f"<p>{_html.escape(l)}</p>" for l in lines)
    if link:
        body += (f'<p><a href="{_html.escape(link)}" style="background:#0B1D40;color:#fff;padding:10px 16px;'
                 'border-radius:10px;text-decoration:none;display:inline-block">Open the deal</a></p>')
    html = f'<div style="font-family:Arial,sans-serif;max-width:560px"><h2 style="color:#0B1D40">{_html.escape(subject)}</h2>{body}</div>'
    ok = False
    for e in to:
        try:
            ok = send_email(e, subject, html, text_body="\n".join(lines + ([link] if link else []))) or ok
        except Exception as ex:
            print("⚠️ follow-up notice email error:", ex)
    return ok


PORTAL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")


def deal_link(lead_id) -> str:
    return f"{PORTAL}/sales-pipeline/{lead_id}" if lead_id else f"{PORTAL}/sales-pipeline"


# ── A team member leaves ─────────────────────────────────────────────────────

def reassign_member_deals(tenant_id: int, member_id: int) -> tuple:
    """Open deals of a removed / deactivated team member go to the person
    the business chose in its follow-up settings. Returns (count, name)."""
    s = get_settings(tenant_id)
    conn = _conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        target = s["reassign_to"]
        key = name = None
        if target.startswith("team:"):
            cur.execute("SELECT id, name FROM team_members WHERE id=%s AND tenant_id=%s AND is_active",
                        (int(target[5:]), tenant_id))
            r = cur.fetchone()
            if r and r["id"] != member_id:
                key, name = f"team:{r['id']}", r["name"]
        if not key:
            cur.execute("SELECT id, first_name, last_name FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1", (tenant_id,))
            r = cur.fetchone()
            if not r:
                return 0, ""
            key = f"owner:{r['id']}"
            name = " ".join(x for x in (r["first_name"], r["last_name"]) if x) or "Account owner"
        cur.execute("""UPDATE merchant_pipeline_leads SET assigned_key=%s, assigned_to=%s, updated_at=NOW()
                       WHERE tenant_id=%s AND assigned_key=%s AND dropped_at IS NULL
                         AND stage <> 'won' AND (outcome IS NULL OR outcome NOT IN ('won','lost','dropped','not_a_fit'))""",
                    (key, name, tenant_id, f"team:{member_id}"))
        n = cur.rowcount
        conn.commit()
        return n, name
    finally:
        cur.close(); conn.close()


# ── Rules: finding deals to follow up ────────────────────────────────────────

LOOKBACK_DAYS = 30   # switching a rule on picks up deals from the last 30 days, never older
BOOKING_URL_RE = r"(outlook\.office(365)?\.com/(owa/calendar|book)|calendly\.com|cal\.com/|bookings|/book(ing)?s?/|zcal\.co|tidycal\.com|hubspot\.com/meetings)"
PRICE_RE = (r"(price|prices|pricing|how much|cost|quote|quotation|rate card|per (carton|unit|pack|piece|kg|box)"
            r"|prix|combien|tarif|devis|precio|cuánto|cuanto|cotización|preço|quanto|orçamento|سعر|كم|بكم)")
TRIGGER_WORDS = {"stage_no_reply": "no reply after moving into {stage}",
                 "booking_click": "clicked your booking link but didn't book",
                 "wa_price_quiet": "asked for a price on WhatsApp, then went quiet"}


def _open_deal_sql(alias="l"):
    return (f"{alias}.dropped_at IS NULL AND {alias}.stage <> 'won' AND "
            f"({alias}.outcome IS NULL OR {alias}.outcome NOT IN ('won','lost','dropped','not_a_fit'))")


def _already_following(cur, tenant_id, lead_id, phone) -> bool:
    cur.execute("""SELECT 1 FROM email_sequence_people WHERE tenant_id=%s AND status IN ('active','ready','sending')
                   AND ((%s::bigint IS NOT NULL AND lead_id=%s) OR (%s <> '' AND phone=%s)) LIMIT 1""",
                (tenant_id, lead_id, lead_id, phone or "", phone or ""))
    return bool(cur.fetchone())


def start_followup(cur, tenant_id: int, seq_id: int, lead_id=None, contact_id=None, phone="", origin="deal",
                   rule_id=None, actor="") -> tuple:
    """Put one deal / contact into a follow-up now. Returns (person_id, problem)."""
    if lead_id:
        cur.execute("SELECT email, wa_contact_id FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s",
                    (lead_id, tenant_id))
        l = cur.fetchone()
        if not l:
            return None, "Deal not found."
        email = (l["email"] or "").strip() or None
        contact_id = contact_id or l["wa_contact_id"]
    else:
        email = None
        if contact_id:
            cur.execute("SELECT email FROM wa_contacts WHERE id=%s AND tenant_id=%s", (contact_id, tenant_id))
            c = cur.fetchone()
            email = ((c or {}).get("email") or "").strip() or None
    phone = digits(phone) or person_whatsapp(cur, tenant_id, lead_id, contact_id)
    if not email and not phone:
        return None, "This deal has no email address and no WhatsApp number."
    if _already_following(cur, tenant_id, lead_id, phone):
        return None, "This deal is already in an automatic follow-up."
    cur.execute("SELECT wait_days FROM email_sequence_steps WHERE sequence_id=%s ORDER BY position LIMIT 1", (seq_id,))
    first = cur.fetchone()
    if not first:
        return None, "That follow-up has no messages yet."
    wait = int(first["wait_days"] or 0)
    if email:
        cur.execute("""INSERT INTO email_sequence_people (tenant_id, sequence_id, email, phone, lead_id, contact_id,
                           next_due_at, added_by, origin, rule_id)
                       VALUES (%s,%s,%s,%s,%s,%s, NOW() + make_interval(days => %s), %s, %s, %s)
                       ON CONFLICT (sequence_id, lower(email)) DO UPDATE SET
                           status='active', steps_sent=0, stop_reason=NULL, next_due_at=EXCLUDED.next_due_at,
                           watch_from=NOW(), added_at=NOW(), updated_at=NOW(), phone=EXCLUDED.phone,
                           lead_id=EXCLUDED.lead_id, contact_id=EXCLUDED.contact_id, added_by=EXCLUDED.added_by,
                           origin=EXCLUDED.origin, rule_id=EXCLUDED.rule_id
                       WHERE email_sequence_people.status IN ('finished','stopped')
                       RETURNING id""",
                    (tenant_id, seq_id, email, phone or None, lead_id, contact_id, wait, actor[:160], origin, rule_id))
    else:
        cur.execute("""INSERT INTO email_sequence_people (tenant_id, sequence_id, email, phone, lead_id, contact_id,
                           next_due_at, added_by, origin, rule_id)
                       VALUES (%s,%s,NULL,%s,%s,%s, NOW() + make_interval(days => %s), %s, %s, %s) RETURNING id""",
                    (tenant_id, seq_id, phone, lead_id, contact_id, wait, actor[:160], origin, rule_id))
    r = cur.fetchone()
    if not r:
        return None, "This deal is already in that follow-up."
    return r["id"], ""


def _team_contacted(cur, tenant_id, lead_id, phone, since) -> bool:
    """Someone on the team got in touch since `since`: a contact logged on
    the deal (call, WhatsApp, email, LinkedIn, meeting…) or a WhatsApp reply
    typed by a person. Automatic messages don't count."""
    if lead_id:
        cur.execute("""SELECT 1 FROM staff_activity WHERE tenant_id=%s AND lead_id=%s AND NOT COALESCE(auto, FALSE)
                       AND action = ANY(%s) AND created_at >= %s LIMIT 1""",
                    (tenant_id, lead_id, list(_TEAM_CONTACT_ACTIONS) + ["wa_logged", "email_logged", "meeting_logged"], since))
        if cur.fetchone():
            return True
    if phone:
        cur.execute("""SELECT 1 FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s AND direction='outbound'
                       AND message_type='agent_reply' AND COALESCE(sent_by_label,'') <> %s
                       AND created_at >= %s LIMIT 1""", (tenant_id, phone, FU_LABEL, since))
        if cur.fetchone():
            return True
    return False


def _candidates(cur, rule) -> list:
    """[(lead_id, contact_id, phone, mark, words)] the rule matches now."""
    t, days, tid = rule["trigger"], int(rule["days"] or 0), rule["tenant_id"]
    out = []
    if t == "stage_no_reply" and rule["stage"]:
        cur.execute(f"""
            SELECT l.id, l.wa_contact_id, l.whatsapp_number,
                   COALESCE((SELECT max(h.created_at) FROM merchant_pipeline_stage_history h
                             WHERE h.lead_id = l.id AND h.to_stage = l.stage), l.updated_at) AS entered
            FROM merchant_pipeline_leads l
            WHERE l.tenant_id=%s AND l.stage=%s AND {_open_deal_sql()}""", (tid, rule["stage"]))
        now = _dt.datetime.now(_dt.timezone.utc)
        for l in cur.fetchall():
            ent = l["entered"]
            if not ent or ent > now - _dt.timedelta(days=days) or ent < now - _dt.timedelta(days=days + LOOKBACK_DAYS):
                continue
            phone = digits(l["whatsapp_number"]) or person_whatsapp(cur, tid, l["id"], l["wa_contact_id"])
            # Any reply from them since they entered the stage?
            cur.execute("""SELECT 1 FROM email_events WHERE tenant_id=%s AND lead_id=%s AND kind='replied'
                           AND created_at >= %s LIMIT 1""", (tid, l["id"], ent))
            if cur.fetchone():
                continue
            if phone:
                cur.execute("""SELECT 1 FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s
                               AND direction='inbound' AND created_at >= %s LIMIT 1""", (tid, phone, ent))
                if cur.fetchone():
                    continue
            # …or did someone on the team get in touch? Then it isn't quiet.
            if _team_contacted(cur, tid, l["id"], phone, ent):
                continue
            out.append((l["id"], l["wa_contact_id"], phone, f"stage:{rule['stage']}:{ent.isoformat()}",
                        TRIGGER_WORDS[t].format(stage=rule.get("stage_label") or rule["stage"])))
    elif t == "booking_click":
        cur.execute(f"""
            SELECT DISTINCT ON (e.lead_id) e.id, e.lead_id, e.created_at, l.wa_contact_id, l.whatsapp_number
            FROM email_events e JOIN merchant_pipeline_leads l ON l.id = e.lead_id AND l.tenant_id = e.tenant_id
            WHERE e.tenant_id=%s AND e.kind='clicked' AND e.detail ~* %s
              AND e.created_at <= NOW() - make_interval(days => %s)
              AND e.created_at >= NOW() - make_interval(days => %s) AND {_open_deal_sql()}
            ORDER BY e.lead_id, e.created_at DESC""", (tid, BOOKING_URL_RE, days, days + LOOKBACK_DAYS))
        for e in cur.fetchall():
            cur.execute("""SELECT 1 FROM booking_appointments WHERE tenant_id=%s AND lead_id=%s
                           AND COALESCE(booked_at, first_seen_at) >= %s LIMIT 1""", (tid, e["lead_id"], e["created_at"]))
            if cur.fetchone():
                continue
            phone = digits(e["whatsapp_number"]) or person_whatsapp(cur, tid, e["lead_id"], e["wa_contact_id"])
            if _team_contacted(cur, tid, e["lead_id"], phone, e["created_at"]):
                continue
            out.append((e["lead_id"], e["wa_contact_id"], phone, f"click:{e['id']}", TRIGGER_WORDS[t]))
    elif t == "wa_price_quiet":
        cur.execute("""
            SELECT DISTINCT ON (m.customer_phone) m.id, m.customer_phone, m.created_at
            FROM wa_message_log m
            WHERE m.tenant_id=%s AND m.direction='inbound' AND m.content ~* %s
              AND m.created_at <= NOW() - make_interval(days => %s)
              AND m.created_at >= NOW() - make_interval(days => %s)
            ORDER BY m.customer_phone, m.created_at DESC""", (tid, PRICE_RE, days, days + LOOKBACK_DAYS))
        for m in cur.fetchall():
            phone = digits(m["customer_phone"])
            cur.execute("""SELECT 1 FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s
                           AND direction='inbound' AND created_at > %s LIMIT 1""", (tid, m["customer_phone"], m["created_at"]))
            if cur.fetchone():
                continue
            cur.execute(f"""SELECT l.id, l.wa_contact_id FROM merchant_pipeline_leads l
                           WHERE l.tenant_id=%s AND regexp_replace(COALESCE(l.whatsapp_number,''),'\\D','','g')=%s
                             AND {_open_deal_sql()} ORDER BY l.id DESC LIMIT 1""", (tid, phone))
            l = cur.fetchone()
            cid = None
            if not l:
                cur.execute("SELECT id FROM wa_contacts WHERE tenant_id=%s AND whatsapp_number=%s LIMIT 1", (tid, phone))
                c = cur.fetchone()
                cid = c["id"] if c else None
            if _team_contacted(cur, tid, (l or {}).get("id"), phone, m["created_at"]):
                continue
            out.append(((l or {}).get("id"), (l or {}).get("wa_contact_id") or cid, phone, f"price:{m['id']}",
                        TRIGGER_WORDS[t]))
    return out


def run_rules(stage_labels_for=None) -> int:
    """Every few minutes: start follow-ups for deals that match a business's
    switched-on rules. Each deal starts once per rule per stage entry /
    click / price question. Returns how many were started."""
    ensure_tables()
    conn = _conn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    started = 0
    try:
        cur.execute("""SELECT r.* FROM followup_rules r
                       LEFT JOIN email_sequences s ON s.id = r.sequence_id AND s.tenant_id = r.tenant_id
                       JOIN tenants tn ON tn.id = r.tenant_id AND NOT COALESCE(tn.is_training, FALSE)
                       WHERE r.enabled AND (r.do_what = 'remind' OR s.id IS NOT NULL)
                       ORDER BY r.tenant_id, r.position""")
        rules = cur.fetchall()
        conn.commit()
        for rule in rules:
            if stage_labels_for and rule["stage"]:
                try:
                    rule["stage_label"] = dict(stage_labels_for(rule["tenant_id"])).get(rule["stage"], rule["stage"])
                except Exception:
                    pass
            try:
                cands = _candidates(cur, rule)
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"⚠️ follow-up rule {rule['id']} check error:", e)
                continue
            for lead_id, contact_id, phone, mark, words in cands:
                try:
                    cur.execute("""INSERT INTO followup_rule_runs (tenant_id, rule_id, lead_id, contact_id, phone, mark)
                                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
                                (rule["tenant_id"], rule["id"], lead_id, contact_id, phone or "", mark[:80]))
                    run = cur.fetchone()
                    if not run:
                        conn.rollback()
                        continue
                    if rule.get("do_what") == "remind":
                        if not lead_id:
                            conn.commit()   # reminders need a deal; remembered so it isn't tried again
                            continue
                        if _remind_from_rule(cur, rule, lead_id, words):
                            started += 1
                        conn.commit()   # remembered either way, so it isn't tried again for this stage entry
                        continue
                    pid, problem = start_followup(cur, rule["tenant_id"], rule["sequence_id"], lead_id=lead_id,
                                                  contact_id=contact_id, phone=phone, origin="rule",
                                                  rule_id=rule["id"], actor="Rule: " + words)
                    cur.execute("UPDATE followup_rule_runs SET person_id=%s WHERE id=%s", (pid, run["id"]))
                    conn.commit()
                    if pid:
                        started += 1
                        cur.execute("SELECT name FROM email_sequences WHERE id=%s", (rule["sequence_id"],))
                        sname = (cur.fetchone() or {}).get("name") or "a follow-up"
                        who = ""
                        if lead_id:
                            cur.execute("SELECT customer_name FROM merchant_pipeline_leads WHERE id=%s", (lead_id,))
                            who = (cur.fetchone() or {}).get("customer_name") or ""
                        notify(cur, rule["tenant_id"], rule["notify"], f"start:{pid}",
                               f"Automatic follow-up started: {who or '+' + (phone or '')}",
                               [f"{who or 'This customer'} {words}.",
                                f"PhiXtra has started \"{sname}\". It stops by itself as soon as they reply."],
                               lead_id=lead_id, link=deal_link(lead_id))
                        conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"⚠️ follow-up rule {rule['id']} start error:", e)
    finally:
        cur.close(); conn.close()
    return started



def _remind_from_rule(cur, rule, lead_id: int, words: str) -> bool:
    """A rule set to "remind": the deal's Next follow-up is set for today
    for the chosen person, and they get an email. Nothing goes to the customer.
    Never replaces a follow-up date a person set by hand: then nothing is
    done and False is returned."""
    import lead_followups as _lf
    tenant_id = rule["tenant_id"]
    _lf.ensure_tables()
    cur.execute("""SELECT 1 FROM lead_followups WHERE tenant_id=%s AND lead_id=%s AND done_at IS NULL
                   AND cancelled_at IS NULL AND NOT auto AND COALESCE(set_by_key,'') NOT LIKE 'system:%%' LIMIT 1""",
                (tenant_id, lead_id))
    if cur.fetchone():
        return False
    who = rule.get("notify") or "owner"
    action = rule.get("remind_action") if rule.get("remind_action") in REMIND_ACTION_KEYS else "call"
    cur.execute("SELECT customer_name FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s", (lead_id, tenant_id))
    name = (cur.fetchone() or {}).get("customer_name") or "this deal"
    for_whom = ""
    if who.startswith("team:"):
        cur.execute("SELECT name FROM team_members WHERE id=%s AND tenant_id=%s", (int(who[5:]), tenant_id))
        for_whom = (cur.fetchone() or {}).get("name") or ""
    elif who == "account_owner":
        cur.execute("SELECT first_name, last_name FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1", (tenant_id,))
        r = cur.fetchone() or {}
        for_whom = " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x)
    note = rule.get("remind_note") or f"They {words}."
    full = (f"For {for_whom}: " if for_whom else "") + note
    _lf.set_next(tenant_id, lead_id, _lf._today(tenant_id), {"key": "system:followup", "label": FU_LABEL},
                 "followup", note=full[:500], channel=action)
    act = dict(REMIND_ACTIONS).get(action, "follow up")
    notify(cur, tenant_id, who, f"rrem:{rule['id']}:{lead_id}:{_lf._today(tenant_id)}",
           f"Reminder: {act} · {name}",
           [f"{name} {words}.", f"Please {act} today." + (f" {rule.get('remind_note')}" if rule.get("remind_note") else ""),
            "It is the deal's Next follow-up for today. Log it on the deal when it's done."],
           lead_id=lead_id, link=deal_link(lead_id))
    return True
