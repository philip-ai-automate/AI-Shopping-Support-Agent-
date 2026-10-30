"""
lead_qualify.py — AI Lead Qualification, Phase 1 (30 Oct 2026, approved
plan https://claude.ai/artifact/1yo1cNY9ac33GpMRnXvnCp, user: "go ahead
with Phase 1").

The AI decides who is a real buyer, on every channel:
  - New enquiry emails in a connected Outlook mailbox (mailbox_routes calls
    on_inbox_email for mail that isn't a campaign reply). Invoices,
    newsletters, colleagues etc. are skipped and never stored.
  - Replies to the business's emails (email_reply_actions.on_reply calls
    on_reply): interested / price / not now / not interested.
  - WhatsApp and website chats: a background check reads each conversation
    once the buyer pauses and saves what they said on the lead. The AI
    Sales Agent itself asks the missing questions (ai-backend lead_qualify_ai).

Seven things are collected (company, business type, requirement, quantity,
location, timeline, buying authority) and the lead gets a score out of 100
from six criteria, each worth the business's own points. Hot / Warm / Cold
bands are the business's own too (CRM › Pipeline Settings). Every AI
decision is logged with its reason and can be undone (lead_ai_decisions);
when the AI isn't sure, the lead goes in Check these (lead_check_items).
One buyer, one record: email, any way of writing the number, company
domain / name, open deal vs new requirement.

Only for businesses whose plan has the "crm.ai_qualification" tick (the
B2B plan) and PhiXtra's own account — same rule as Chat Translation.
"""
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2.extras

from db import get_db_connection

FEATURE_KEY = "crm.ai_qualification"
AI_NAME = "AI"                      # "Qualified by AI" (approved wording)
PORTAL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")

# The seven things the AI finds out (key, label shown in settings).
ASK_FIELDS = [("company", "company"), ("business_type", "business type"), ("requirement", "product"),
              ("quantity", "quantity"), ("delivery_country", "delivery country"), ("timeline", "timeline"),
              ("authority", "who decides")]
# Score criteria (key, label, default points).
CRITERIA = [("verified", "Verified business", 15), ("requirement", "Clear product requirement", 20),
            ("commercial", "Commercial quantity", 20), ("timeline", "Confirmed purchasing timeline", 20),
            ("authority", "Decision maker or purchasing authority", 15), ("location", "Delivery location confirmed", 10)]
# Which question each score criterion comes from: a question that isn't
# asked (switched off in settings) isn't scored either (2026-10-30 redesign).
CRITERION_ASK = {"verified": "company", "requirement": "requirement", "commercial": "quantity",
                 "timeline": "timeline", "authority": "authority", "location": "delivery_country"}
BUSINESS_TYPES = ["Wholesaler", "Distributor", "Retailer", "Reseller", "Manufacturer", "End user",
                  "Service business", "Other"]
INTENT_WORDS = {"full_rfq": "Full RFQ", "wants_talk": "Wants to talk", "price_request": "Price request",
                "question": "Enquiry", "not_now": "Not now", "not_interested": "Not interested",
                "auto_reply": "Auto-reply", "not_enquiry": "Not an enquiry"}
STRONG_INTENTS = ("full_rfq", "wants_talk", "price_request")
CHANNEL_WORDS = {"email": "email enquiry", "reply": "email reply", "whatsapp": "WhatsApp", "web": "website chat"}

DEFAULTS = {
    "enabled": True,
    "ask": [k for k, _ in ASK_FIELDS],
    "min_qty": 50, "min_value": 1000,
    "deliver_to": "",                 # blank = anywhere
    "hot_from": 75, "warm_from": 45,
    "points": {k: p for k, _, p in CRITERIA},
    "on_hot_qualify": True, "on_hot_alert": True,
    "read_inbox": True,
    "owner_rule": "inbox",            # inbox | turns | none
    # The business's own qualifying questions (2026-10-30): [{"id", "label",
    # "ask", "good_if", "points"}]. The AI asks them like the standard ones,
    # judges each answer against "good_if" and they count in the score.
    "questions": [],
    # Team members left out of "share in turn" (ids), e.g. a training account.
    "rotation_skip": [],
}
MAX_QUESTIONS = 8
_QID = re.compile(r"^q[a-z0-9]{4,12}$")


def new_question_id() -> str:
    import secrets
    return "q" + secrets.token_hex(4)

# Personal / free email domains: never "the same company".
_FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "hotmail.com", "hotmail.co.uk",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "gmx.de", "web.de", "mail.com", "yandex.com", "yandex.ru", "zoho.com", "qq.com", "163.com",
    "126.com", "rocketmail.com", "btinternet.com", "sky.com", "orange.fr", "free.fr", "laposte.net",
}
# Senders that are never a buyer writing in (platform notices, newsletters).
_SKIP_SENDER = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|newsletter|news|mailer|"
    r"mailer-daemon|postmaster|bounce[s]?|marketing|updates?|digest|billing|invoices?|receipts?|"
    r"accounts?-?payable|security|support-noreply)[@.+-]", re.I)
_SKIP_DOMAINS = re.compile(
    r"(^|\.)(linkedin\.com|facebookmail\.com|facebook\.com|google\.com|microsoft\.com|office\.com|"
    r"office365\.com|apple\.com|amazon\.[a-z.]+|paypal\.[a-z.]+|stripe\.com|zoho\.[a-z.]+|zeptomail\.[a-z.]+|"
    r"mailchimp\.com|sendgrid\.net|hubspot\.com|hubspotemail\.net|intuit\.com|xero\.com|quickbooks\.com|"
    r"slack\.com|zoom\.us|calendly\.com|canva\.com|github\.com|godaddy\.com|wordpress\.com|"
    r"cyberpanel\.net|meta\.com|whatsapp\.com|twitter\.com|x\.com|tiktok\.com|youtube\.com|"
    r"medium\.com|substack\.com|eventbrite\.[a-z.]+|prospeo\.io|openai\.com|anthropic\.com)$", re.I)


# ── who has it, and their settings ──────────────────────────────────────────

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
        cur.execute("SET lock_timeout = '5s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS lead_qual_settings (
                           tenant_id  INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
                           settings   JSONB NOT NULL DEFAULT '{}'::jsonb,
                           since      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           rr_last    VARCHAR(64),
                           updated_by VARCHAR(200),
                           updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS lead_ai_decisions (
                           id           BIGSERIAL PRIMARY KEY,
                           tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                           lead_id      INTEGER NOT NULL,
                           channel      VARCHAR(12),
                           intent       VARCHAR(20),
                           summary      TEXT,
                           reason       TEXT,
                           score        SMALLINT,
                           tier         VARCHAR(8),
                           before       JSONB,
                           created_lead BOOLEAN NOT NULL DEFAULT FALSE,
                           qualified    BOOLEAN NOT NULL DEFAULT FALSE,
                           created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           undone_at    TIMESTAMPTZ,
                           undone_by    VARCHAR(200))""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lead_ai_decisions_lead ON lead_ai_decisions (lead_id, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lead_ai_decisions_tenant ON lead_ai_decisions (tenant_id, created_at DESC)")
        cur.execute("""CREATE TABLE IF NOT EXISTS lead_check_items (
                           id            BIGSERIAL PRIMARY KEY,
                           tenant_id     INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                           lead_id       INTEGER NOT NULL,
                           other_lead_id INTEGER,
                           kind          VARCHAR(12) NOT NULL,
                           reason        TEXT,
                           status        VARCHAR(10) NOT NULL DEFAULT 'open',
                           created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           resolved_by   VARCHAR(200),
                           resolved_at   TIMESTAMPTZ)""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lead_check_items_open ON lead_check_items (tenant_id) WHERE status='open'")
        # What has been read already: an email ("mail:<mailbox>:<message id>")
        # or a chat ("chat:<session id>", last message read + its lead).
        cur.execute("""CREATE TABLE IF NOT EXISTS lead_qual_sources (
                           tenant_id  INTEGER NOT NULL,
                           source_key TEXT NOT NULL,
                           lead_id    INTEGER,
                           last_seen  BIGINT,
                           verdict    VARCHAR(20),
                           updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           PRIMARY KEY (tenant_id, source_key))""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lead_qual_sources_lead ON lead_qual_sources (lead_id)")
        conn.commit()
        _tables_ok = True
    except Exception as e:
        conn.rollback()
        print("⚠️ lead_qualify tables:", e)
    finally:
        cur.close(); conn.close()


def allowed(tenant_id) -> bool:
    if not tenant_id:
        return False
    try:
        import accounts_core
        if accounts_core.is_platform_tenant(tenant_id):
            return True
    except Exception:
        pass
    conn, cur = _db()
    try:
        cur.execute("""SELECT 1 FROM tenants t JOIN plan_feature_grants g ON g.plan_id = t.plan_id
                        WHERE t.id = %s AND g.feature_key = %s LIMIT 1""", (int(tenant_id), FEATURE_KEY))
        return cur.fetchone() is not None
    finally:
        cur.close(); conn.close()


def _clean_settings(raw: dict) -> dict:
    s = json.loads(json.dumps(DEFAULTS))
    raw = raw or {}
    for k in ("enabled", "on_hot_qualify", "on_hot_alert", "read_inbox"):
        if k in raw:
            s[k] = bool(raw[k])
    if isinstance(raw.get("ask"), list):
        s["ask"] = [k for k, _ in ASK_FIELDS if k in raw["ask"]]
    for k, lo, hi in (("min_qty", 0, 10_000_000), ("min_value", 0, 1_000_000_000),
                      ("hot_from", 1, 100), ("warm_from", 0, 99)):
        try:
            if raw.get(k) not in (None, ""):
                s[k] = max(lo, min(hi, int(float(raw[k]))))
        except (TypeError, ValueError):
            pass
    if s["warm_from"] >= s["hot_from"]:
        s["warm_from"] = max(0, s["hot_from"] - 1)
    if isinstance(raw.get("points"), dict):
        for k, _, _ in CRITERIA:
            try:
                if raw["points"].get(k) not in (None, ""):
                    s["points"][k] = max(0, min(100, int(float(raw["points"][k]))))
            except (TypeError, ValueError):
                pass
    if isinstance(raw.get("deliver_to"), str):
        s["deliver_to"] = re.sub(r"\s+", " ", raw["deliver_to"]).strip()[:4000]
    if raw.get("owner_rule") in ("inbox", "turns", "none"):
        s["owner_rule"] = raw["owner_rule"]
    if isinstance(raw.get("rotation_skip"), list):
        s["rotation_skip"] = sorted({int(x) for x in raw["rotation_skip"] if str(x).isdigit()})[:500]
    if isinstance(raw.get("questions"), list):
        qs, seen = [], set()
        for q in raw["questions"]:
            if not isinstance(q, dict):
                continue
            label = re.sub(r"\s+", " ", str(q.get("label") or "")).strip()[:40]
            ask = re.sub(r"\s+", " ", str(q.get("ask") or "")).strip()[:200]
            if not label and not ask:
                continue
            qid = str(q.get("id") or "")
            if not _QID.match(qid) or qid in seen:
                qid = new_question_id()
            seen.add(qid)
            try:
                pts = max(0, min(100, int(float(q.get("points") or 0))))
            except (TypeError, ValueError):
                pts = 0
            on = q.get("on", True)
            qs.append({"id": qid, "label": label or ask[:40], "ask": ask or label,
                       "good_if": re.sub(r"\s+", " ", str(q.get("good_if") or "")).strip()[:200], "points": pts,
                       "on": on is True or on in ("1", "true", "on")})
            if len(qs) >= MAX_QUESTIONS:
                break
        s["questions"] = qs
    return s


def settings(tenant_id) -> dict:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT settings, since FROM lead_qual_settings WHERE tenant_id=%s", (int(tenant_id),))
        r = cur.fetchone()
        if not r:
            # First use: from now on (old mail is never turned into leads).
            cur.execute("""INSERT INTO lead_qual_settings (tenant_id) VALUES (%s) ON CONFLICT DO NOTHING""",
                        (int(tenant_id),))
            conn.commit()
            cur.execute("SELECT settings, since FROM lead_qual_settings WHERE tenant_id=%s", (int(tenant_id),))
            r = cur.fetchone()
    finally:
        cur.close(); conn.close()
    s = _clean_settings((r or {}).get("settings") or {})
    s["since"] = (r or {}).get("since")
    return s


def save_settings(tenant_id, raw: dict, by_label: str = "") -> dict:
    s = _clean_settings(raw)
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO lead_qual_settings (tenant_id, settings, updated_by, updated_at)
                       VALUES (%s, %s, %s, NOW())
                       ON CONFLICT (tenant_id) DO UPDATE SET settings=EXCLUDED.settings,
                              updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                    (int(tenant_id), psycopg2.extras.Json(s), (by_label or "")[:200]))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return s


def active(tenant_id):
    """Settings when the business has AI Lead Qualification and it is on, else None."""
    try:
        if not allowed(tenant_id):
            return None
        s = settings(tenant_id)
        return s if s["enabled"] else None
    except Exception as e:
        print("⚠️ lead_qualify.active:", e)
        return None


# ── the score ───────────────────────────────────────────────────────────────

def _filled(v) -> bool:
    return v is not None and str(v).strip() != ""


def checks_for(lead: dict) -> dict:
    """Each criterion met or not. The AI's judgement (qual_checks) counts
    first; a criterion it never judged follows the lead's own details."""
    ai = lead.get("qual_checks") or {}
    if isinstance(ai, str):
        try:
            ai = json.loads(ai)
        except ValueError:
            ai = {}
    fallback = {
        "verified": _filled(lead.get("website")),
        "requirement": _filled(lead.get("product_interest")),
        "commercial": _filled(lead.get("quantity")),
        "timeline": _filled(lead.get("purchase_timeline")) or lead.get("deadline_date") is not None,
        "authority": bool(lead.get("decision_maker_engaged")),
        "location": _filled(lead.get("delivery_country")),
    }
    return {k: (bool(ai[k]) if isinstance(ai.get(k), bool) else fallback[k]) for k, _, _ in CRITERIA}


def tier_for(score: int, s: dict) -> str:
    return "hot" if score >= s["hot_from"] else ("warm" if score >= s["warm_from"] else "cold")


def answers_for(lead: dict) -> dict:
    a = lead.get("qual_answers") or {}
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            a = {}
    return a if isinstance(a, dict) else {}


def scored_points(s: dict) -> dict:
    """Points of each standard criterion that counts (its question is asked)."""
    return {k: (int(s["points"].get(k, 0)) if CRITERION_ASK[k] in s["ask"] else 0) for k, _, _ in CRITERIA}


def own_questions(s: dict) -> list:
    """The business's own questions that are switched on."""
    return [q for q in s.get("questions") or [] if q.get("on", True)]


def total_points(s: dict) -> int:
    return sum(scored_points(s).values()) + sum(int(q["points"]) for q in own_questions(s))


def score(lead: dict, s: dict) -> tuple:
    """(score, tier, breakdown) — breakdown lines for the lead page. The
    score is always out of 100: points got ÷ all points the business set."""
    met = checks_for(lead)
    ans = answers_for(lead)
    lines, got_all = [], 0
    sp = scored_points(s)
    for k, label, _ in CRITERIA:
        pts = sp[k]
        if not pts:
            continue
        got = pts if met[k] else 0
        got_all += got
        lines.append({"label": label, "points": got, "of": pts, "active": met[k]})
    for q in own_questions(s):
        ok = (ans.get(q["id"]) or {}).get("met") is True
        got = q["points"] if ok else 0
        got_all += got
        lines.append({"label": q["label"], "points": got, "of": q["points"], "active": ok,
                      "answer": (ans.get(q["id"]) or {}).get("answer")})
    tp = total_points(s)
    total = min(100, round(got_all * 100 / tp)) if tp else 0
    return total, tier_for(total, s), lines


def score_sql(s: dict, a: str = "mpl") -> str:
    """The same score in SQL (points are whole numbers from settings)."""
    def chk(key, fallback):
        return (f"COALESCE(CASE WHEN jsonb_typeof({a}.qual_checks->'{key}')='boolean' "
                f"THEN ({a}.qual_checks->>'{key}')::boolean END, {fallback})")
    p = scored_points(s)
    tp = total_points(s)
    if not tp:
        return "0"
    parts = [
        (p["verified"], chk("verified", f"COALESCE({a}.website,'')<>''")),
        (p["requirement"], chk("requirement", f"COALESCE({a}.product_interest,'')<>''")),
        (p["commercial"], chk("commercial", f"COALESCE({a}.quantity,'')<>''")),
        (p["timeline"], chk("timeline", f"(COALESCE({a}.purchase_timeline,'')<>'' OR {a}.deadline_date IS NOT NULL)")),
        (p["authority"], chk("authority", f"COALESCE({a}.decision_maker_engaged, FALSE)")),
        (p["location"], chk("location", f"COALESCE({a}.delivery_country,'')<>''")),
    ]
    for q in own_questions(s):
        if _QID.match(q["id"]):
            parts.append((int(q["points"]), f"(({a}.qual_answers->'{q['id']}'->>'met') = 'true')"))
    got = " + ".join(f"(CASE WHEN {c} THEN {n} ELSE 0 END)" for n, c in parts if n)
    return f"LEAST(100, ROUND(({got or '0'}) * 100.0 / {tp}))"


# ── the AI read ─────────────────────────────────────────────────────────────

def _ask(prompt: str) -> dict:
    import ai_designer as D
    client = D._client("lead_qualify", "platform")
    resp = client.chat.completions.create(
        model=os.getenv("LEAD_QUALIFY_MODEL", D.TEXT_MODEL),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"})
    return json.loads(resp.choices[0].message.content or "{}")


def _business_context(tenant_id: int) -> dict:
    conn, cur = _db()
    try:
        cur.execute("""SELECT t.name, cp.sells, cp.trading_name, cp.reg_country
                         FROM tenants t LEFT JOIN company_profiles cp ON cp.tenant_id = t.id WHERE t.id=%s""",
                    (tenant_id,))
        t = cur.fetchone() or {}
        cur.execute("""SELECT name FROM products WHERE tenant_id=%s AND COALESCE(is_active, TRUE)
                        ORDER BY updated_at DESC NULLS LAST LIMIT 200""", (tenant_id,))
        products = [r["name"] for r in cur.fetchall() if r["name"]]
    finally:
        cur.close(); conn.close()
    try:
        import selling_currency
        currency = selling_currency.tenant_currency(tenant_id)
    except Exception:
        currency = ""
    return {"name": t.get("trading_name") or t.get("name") or "the business", "sells": t.get("sells") or "",
            "country": t.get("reg_country") or "", "products": products, "currency": currency}


def read(tenant_id: int, kind: str, text: str, s: dict, known: dict = None, today: date = None) -> dict:
    """The AI's reading of one email / reply / chat. kind: email | reply | chat."""
    biz = _business_context(tenant_id)
    today = today or date.today()
    what = {
        "email": "an email that arrived in the business's sales inbox",
        "reply": "a reply to an email the business sent to this lead",
        "chat": "a chat between a buyer and the business's AI sales agent (BUYER / AGENT lines)",
    }[kind]
    known_txt = ""
    if known:
        bits = {k: v for k, v in known.items() if _filled(v)}
        if bits:
            known_txt = "What the business already has on this buyer: " + json.dumps(bits, default=str) + "\n"
    own_q = own_questions(s)
    own_txt = ""
    if own_q:
        own_txt = ("\nThe business's own qualifying questions (answer each from the text; null = not answered):\n" +
                   "\n".join(f'- id "{q["id"]}": {q["ask"]}' + (f' — counts as a good answer if: {q["good_if"]}' if q["good_if"] else "")
                              for q in own_q) + "\n")
    rules = [f"A commercial quantity is at least {s['min_qty']} units or {s['min_value']} {biz['currency']} in value."]
    if s.get("deliver_to"):
        rules.append(f"The business delivers to: {s['deliver_to']}.")
    prompt = f"""You qualify sales leads for {biz['name']}, a business that sells to other businesses.
What they sell: {biz['sells'] or 'see the product list'}.
Products / services{' (sample)' if len(biz['products']) >= 200 else ''}: {' | '.join(biz['products'][:200]) or 'not listed'}.
{' '.join(rules)}
Today is {today.isoformat()}.{own_txt}

Below is {what}.
{known_txt}
Decide three things: is this a genuine potential customer (a real business or buyer, not a student,
competitor, job seeker, supplier, spam, newsletter, invoice, colleague or platform notice)? Is there a real
requirement the business can fulfil? Are they likely to buy (timeline, decision maker, asking for price/call)?

Use ONLY what is written. Never invent a value: unknown = null. Keep every value short, in English
(translate if the buyer wrote in another language), but keep company and person names as written.

Reply with JSON only:
{{
 "is_enquiry": true/false,       // a potential customer asking to buy / about products or services
 "intent": "full_rfq" | "wants_talk" | "price_request" | "question" | "not_now" | "not_interested" | "auto_reply" | "not_enquiry",
   // full_rfq = gives the product, quantity and where/when in one go; wants_talk = asks for a call/meeting/WhatsApp;
   // not_now = interested later; not_interested = says no / already has a supplier
 "company": string|null, "contact_person": string|null, "job_title": string|null,
 "email": string|null, "phones": [strings written in the text, with + and country code if given],
 "website": string|null,
 "business_type": one of {json.dumps(BUSINESS_TYPES)} or null,
 "requirement": string|null,     // what products/services, with specs
 "quantity": string|null,        // e.g. "200 units", "5,000 boxes a month"
 "buyer_country": ISO 3166 two-letter code of where the buyer is | null,
 "buyer_city": string|null,
 "delivery_country": country name where delivery must happen | null,
 "timeline": string|null,        // when they intend to buy, e.g. "within 3 weeks"
 "deadline_date": "YYYY-MM-DD"|null,   // only if a date can be worked out from the timeline
 "authority": "decision_maker" | "purchasing" | "unknown",
 "checks": {{
   "verified": true/false,       // clearly a genuine business (company named, business email/website, real trade)
   "requirement": true/false,    // a clear product/service need the business can supply
   "commercial": true/false/null,// quantity meets the commercial quantity above (null = no quantity given)
   "timeline": true/false,       // a purchasing timeline is stated
   "authority": true/false,      // decision maker or purchasing person
   "location": true/false        // delivery location given{' and within where the business delivers' if s.get('deliver_to') else ''}
 }},
 "follow_up_date": "YYYY-MM-DD"|null,  // for not_now: when they said to come back
 "not_fit_reason": string|null,        // for not_interested: their reason in a few words
 "new_requirement": true/false,        // known buyer asking for something different from what the business already has on them
 "answers": {{ "<question id>": {{"answer": "their answer in a few words" | null, "met": true/false/null}} }},  // the business's own questions only
 "items": [ {{"product": string, "qty": number|null, "unit": string|null, "note": string|null}} ],
   // ONLY when they ask for prices / a quotation for specific products: one entry per product they named.
   // product = the exact name from the business's product list when it is clearly that product, else what they asked for
   //   TRANSLATED INTO ENGLISH (e.g. "kompostierbare Strohhalme" → "compostable straws").
   // qty = the number they gave for it; unit = the unit in English ("cases", "boxes", "packs", "kg"); note = size/colour/spec they added, in English.
   // In a chat, list everything they asked a price for in the WHOLE conversation (latest quantities win). Otherwise [].
 "deliver_to": string|null,      // the delivery place they gave (city, country or address) for this order
 "needed_by": "YYYY-MM-DD"|null, // the date they need it by, only if it can be worked out
 "reason": "one short plain sentence: why you decided this (what they want, how much, where, when, what is missing)",
 "confidence": number 0-1
}}

TEXT:
\"\"\"{(text or '')[:6000]}\"\"\""""
    out = _ask(prompt)
    if not isinstance(out, dict):
        return {}
    # The AI's answer is checked before anything is saved: text fields are
    # text (a number like 200 becomes "200"), phones a list, confidence a number.
    for k in ("company", "contact_person", "job_title", "email", "website", "requirement", "quantity",
              "buyer_country", "buyer_city", "delivery_country", "timeline", "deadline_date",
              "follow_up_date", "not_fit_reason", "reason", "authority", "business_type"):
        v = out.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = str(v)
        elif not isinstance(v, str):
            out[k] = None
        else:
            out[k] = v.strip() or None
    ph = out.get("phones")
    if isinstance(ph, str):
        ph = [ph]
    out["phones"] = [str(p) for p in (ph if isinstance(ph, list) else []) if isinstance(p, (str, int))][:5]
    try:
        out["confidence"] = float(out.get("confidence"))
    except (TypeError, ValueError):
        out["confidence"] = None
    ids = {q["id"] for q in own_q}
    ans = {}
    for qid, v in (out.get("answers") if isinstance(out.get("answers"), dict) else {}).items():
        if qid in ids and isinstance(v, dict):
            a = v.get("answer")
            a = str(a).strip()[:300] if isinstance(a, (str, int, float)) and not isinstance(a, bool) and str(a).strip() else None
            m = v.get("met") if isinstance(v.get("met"), bool) else None
            if a or m is not None:
                ans[qid] = {"answer": a, "met": m}
    out["answers"] = ans
    items = []
    for it in (out.get("items") if isinstance(out.get("items"), list) else [])[:200]:
        if not isinstance(it, dict):
            continue
        name = it.get("product")
        if not isinstance(name, str) or not name.strip():
            continue
        q = it.get("qty")
        try:
            q = float(str(q).replace(",", "")) if q not in (None, "") and not isinstance(q, bool) else None
            q = q if q is not None and 0 <= q <= 1e9 else None
        except (TypeError, ValueError):
            q = None
        items.append({"product": re.sub(r"\s+", " ", name).strip()[:300], "qty": q,
                      "unit": str(it.get("unit")).strip()[:40] if isinstance(it.get("unit"), str) else None,
                      "note": str(it.get("note")).strip()[:300] if isinstance(it.get("note"), str) else None})
    out["items"] = items
    for k in ("deliver_to", "needed_by"):
        v = out.get(k)
        out[k] = v.strip()[:300] if isinstance(v, str) and v.strip() else None
    out["is_enquiry"] = out.get("is_enquiry") is True
    out["new_requirement"] = out.get("new_requirement") is True
    out["intent"] = out.get("intent") if out.get("intent") in INTENT_WORDS else (
        "question" if out.get("is_enquiry") else "not_enquiry")
    if out.get("business_type") not in BUSINESS_TYPES:
        out["business_type"] = None
    out["checks"] = {k: v for k, v in (out.get("checks") or {}).items()
                     if k in dict((c, 1) for c, _, _ in CRITERIA) and isinstance(v, bool)}
    return out


# ── one buyer, one record ───────────────────────────────────────────────────

def _digits(v) -> str:
    return re.sub(r"\D", "", v or "")


def _norm_phones(phones, region: str = None) -> list:
    """Numbers as +<country><number>. Written without + they are read as the
    buyer's own country's; with no country known they are never guessed."""
    import phonenumbers
    out = []
    for p in phones or []:
        p = str(p).strip()
        try:
            n = phonenumbers.parse(p, (region or "").upper() or None) if not p.startswith("+") else phonenumbers.parse(p, None)
        except phonenumbers.NumberParseException:
            if _digits(p) and not p.startswith("+") and len(_digits(p)) >= 11:
                try:
                    n = phonenumbers.parse("+" + _digits(p), None)    # e.g. 2348031234567
                except phonenumbers.NumberParseException:
                    continue
            else:
                continue
        if phonenumbers.is_valid_number(n):
            e = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
            if e not in out:
                out.append(e)
    return out


def _domain(email: str) -> str:
    e = (email or "").strip().lower()
    return e.split("@", 1)[1] if "@" in e else ""


def _site_domain(url: str) -> str:
    u = re.sub(r"^https?://", "", (url or "").strip().lower())
    return re.sub(r"^www\.", "", u.split("/")[0])


_LEAD_COLS = """id, tenant_id, customer_name, contact_person, phone, whatsapp_number, email, other_emails, website,
                notes, product_interest, business_type, quantity, delivery_country, purchase_timeline, deadline_date,
                decision_maker_engaged, addr_country, addr_city, qual_checks, qual_answers, qual_by, qual_reason, qual_at,
                is_opportunity, stage, outcome, dropped_at, dropped_reason, assigned_key, assigned_to,
                wa_contact_id, company_id, source, created_at"""


def find_buyer(cur, tenant_id: int, emails=(), phones=(), company: str = None, website: str = None) -> dict:
    """{"lead": the buyer's latest lead or None, "how": what matched,
        "company_lead": a lead at the same company (new person) or None,
        "unsure": a possible match the AI must not merge by guessing}."""
    emails = [e.strip().lower() for e in emails if e and "@" in e]
    keys = [d[-9:] for d in (_digits(p) for p in phones) if len(d) >= 7]
    order = "ORDER BY (dropped_at IS NULL) DESC, updated_at DESC NULLS LAST, id DESC LIMIT 1"
    if emails:
        cur.execute(f"""SELECT {_LEAD_COLS} FROM merchant_pipeline_leads
                        WHERE tenant_id=%s AND (lower(email) = ANY(%s)
                              OR string_to_array(lower(COALESCE(other_emails,'')), ', ') && %s::text[]) {order}""",
                    (tenant_id, emails, emails))
        r = cur.fetchone()
        if r:
            return {"lead": r, "how": "email"}
    for k in keys:
        cur.execute(f"""SELECT {_LEAD_COLS} FROM merchant_pipeline_leads
                        WHERE tenant_id=%s AND (right(regexp_replace(COALESCE(whatsapp_number,''),'\\D','','g'),9)=%s
                              OR right(regexp_replace(COALESCE(phone,''),'\\D','','g'),9)=%s) {order}""",
                    (tenant_id, k, k))
        r = cur.fetchone()
        if r:
            return {"lead": r, "how": "number"}
    # Same company: the email's company domain or the website.
    doms = {d for d in (_domain(e) for e in emails) if d and d not in _FREE_DOMAINS}
    if website and _site_domain(website):
        doms.add(_site_domain(website))
    for d in doms:
        cur.execute(f"""SELECT {_LEAD_COLS} FROM merchant_pipeline_leads
                        WHERE tenant_id=%s AND (lower(email) LIKE %s OR lower(COALESCE(other_emails,'')) LIKE %s
                              OR regexp_replace(lower(COALESCE(website,'')), '^https?://(www\\.)?', '') LIKE %s) {order}""",
                    (tenant_id, f"%@{d}", f"%@{d}%", f"{d}%"))
        r = cur.fetchone()
        if r:
            return {"lead": None, "how": "company", "company_lead": r}
    # Same company name but nothing else in common: never merged by guessing.
    if company and len(company.strip()) >= 3:
        cur.execute(f"""SELECT {_LEAD_COLS} FROM merchant_pipeline_leads
                        WHERE tenant_id=%s AND lower(customer_name)=lower(%s) {order}""", (tenant_id, company.strip()))
        r = cur.fetchone()
        if r:
            return {"lead": None, "how": "unsure", "unsure": r}
    return {"lead": None, "how": None}


# ── what the AI does with its reading ───────────────────────────────────────

def _is_open(lead) -> bool:
    return lead and lead.get("dropped_at") is None and not lead.get("outcome")


def _stage_history(cur, lead_id, from_stage, to_stage, notes):
    cur.execute("""INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                   VALUES (%s,%s,%s,%s,%s)""", (lead_id, from_stage, to_stage, AI_NAME, (notes or "")[:2000]))


def _pick_owner(cur, tenant_id: int, s: dict, inbox_owner: tuple = None):
    """(key, label) for a lead nobody owns, by the business's rule."""
    if s["owner_rule"] == "none":
        return None
    if s["owner_rule"] == "inbox" and inbox_owner and inbox_owner[0]:
        return inbox_owner
    if s["owner_rule"] in ("inbox", "turns"):
        cur.execute("""SELECT tm.id, tm.name FROM team_members tm
                         LEFT JOIN tenant_roles r ON r.id = tm.role_id AND r.tenant_id = tm.tenant_id
                        WHERE tm.tenant_id=%s AND tm.is_active
                          AND (r.permissions->>'leads.page') IN ('true', '1')
                        ORDER BY tm.id""", (tenant_id,))
        skip = set(s.get("rotation_skip") or [])
        team = [(f"team:{r['id']}", r["name"]) for r in cur.fetchall() if r["id"] not in skip]
        if not team:
            return None
        cur.execute("SELECT rr_last FROM lead_qual_settings WHERE tenant_id=%s FOR UPDATE", (tenant_id,))
        last = (cur.fetchone() or {}).get("rr_last")
        keys = [k for k, _ in team]
        nxt = team[(keys.index(last) + 1) % len(team)] if last in keys else team[0]
        cur.execute("UPDATE lead_qual_settings SET rr_last=%s WHERE tenant_id=%s", (nxt[0], tenant_id))
        return nxt
    return None


def _create_lead(cur, tenant_id: int, r: dict, channel: str, ctx: dict, like: dict = None) -> int:
    """A new lead from what the buyer wrote (like = a lead of the same buyer
    or company, whose contact / company it joins)."""
    email = (ctx.get("email") or r.get("email") or "").strip().lower() or None
    wa = _digits(ctx.get("wa_number")) or None
    phones = [p for p in (r.get("phones") or []) if len(_digits(p)) >= 7]
    phone = None
    if phones and not (wa and _digits(phones[0])[-9:] == wa[-9:]):
        phone = phones[0]
    company = (r.get("company") or "").strip() or None
    person = (r.get("contact_person") or ctx.get("name") or "").strip() or None
    if like and not company:
        company = like.get("customer_name")
    name = company or person or email or (f"+{wa}" if wa else "New enquiry")
    source = {"email": "email", "reply": "email", "whatsapp": "whatsapp", "web": "website"}[channel]
    cur.execute("""INSERT INTO merchant_pipeline_leads
                     (tenant_id, customer_name, contact_person, email, phone, whatsapp_number, website,
                      addr_country, addr_city, stage, contact_channel, source, wa_contact_id, company_id, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'new_lead',%s,%s,%s,%s,%s) RETURNING id""",
                (tenant_id, name[:300], (person or "")[:200] or None, email, phone, wa,
                 (r.get("website") or "")[:300] or None,
                 (r.get("buyer_country") or "")[:2].upper() or None, (r.get("buyer_city") or "")[:100] or None,
                 "email" if source == "email" else ("whatsapp" if source == "whatsapp" else None), source,
                 None, (like or {}).get("company_id"),
                 (f"Job title: {r['job_title']}" if r.get("job_title") else None)))
    lead_id = cur.fetchone()["id"]
    _stage_history(cur, lead_id, None, "new_lead", f"Created by AI from a {CHANNEL_WORDS[channel]}")
    return lead_id


def _fill(cur, lead: dict, r: dict, staff_owned: bool) -> dict:
    """Save what the AI found on the lead. Never overwrites what a person
    typed; the AI's own earlier answers are updated as the buyer adds detail.
    Returns the previous values of everything changed (for Undo)."""
    before, sets = {}, {}

    def put(col, val, replace_own=False):
        if val is None or (isinstance(val, str) and not val.strip()):
            return
        cur_v = lead.get(col)
        if _filled(cur_v) and not (replace_own and not staff_owned):
            return
        if cur_v == val:
            return
        before[col] = cur_v
        sets[col] = val
        lead[col] = val

    if (lead.get("customer_name") or "").startswith("+") and r.get("company"):
        put_name = r["company"].strip()[:300]
        before["customer_name"], sets["customer_name"], lead["customer_name"] = lead["customer_name"], put_name, put_name
    put("contact_person", (r.get("contact_person") or "").strip()[:200] or None)
    put("website", (r.get("website") or "").strip()[:300] or None)
    put("product_interest", (r.get("requirement") or "").strip()[:500] or None, replace_own=True)
    put("business_type", r.get("business_type"), replace_own=True)
    put("quantity", (r.get("quantity") or "").strip()[:200] or None, replace_own=True)
    put("delivery_country", (r.get("delivery_country") or "").strip()[:80] or None, replace_own=True)
    put("purchase_timeline", (r.get("timeline") or "").strip()[:200] or None, replace_own=True)
    if r.get("deadline_date"):
        try:
            put("deadline_date", date.fromisoformat(str(r["deadline_date"])[:10]))
        except ValueError:
            pass
    put("addr_country", (r.get("buyer_country") or "").strip()[:2].upper() or None)
    put("addr_city", (r.get("buyer_city") or "").strip()[:100] or None)
    if r.get("authority") in ("decision_maker", "purchasing") and not lead.get("decision_maker_engaged"):
        before["decision_maker_engaged"] = lead.get("decision_maker_engaged")
        sets["decision_maker_engaged"] = True
        lead["decision_maker_engaged"] = True
    # Numbers: the buyer's email address / numbers they wrote.
    email = (r.get("email") or "").strip().lower()
    if email and "@" in email and email != (lead.get("email") or "").lower():
        if not lead.get("email"):
            put("email", email)
        elif email not in (lead.get("other_emails") or "").lower():
            before["other_emails"] = lead.get("other_emails")
            sets["other_emails"] = ", ".join(x for x in (lead.get("other_emails"), email) if x)
            lead["other_emails"] = sets["other_emails"]
    for p in r.get("phones") or []:
        d = _digits(p)
        if len(d) < 7 or any(_digits(lead.get(f))[-9:] == d[-9:] for f in ("phone", "whatsapp_number") if lead.get(f)):
            continue
        if not lead.get("phone"):
            put("phone", p)
        elif d[-9:] not in _digits(lead.get("notes")):
            line = f"Other numbers (from the buyer): {p}"
            before.setdefault("notes", lead.get("notes"))
            sets["notes"] = (lead.get("notes") + "\n" + line) if lead.get("notes") else line
            lead["notes"] = sets["notes"]
    # The business's own questions: a person's answer is never replaced.
    old_ans = answers_for(lead)
    new_ans = json.loads(json.dumps(old_ans))
    for qid, v in (r.get("answers") or {}).items():
        cur_a = new_ans.get(qid) or {}
        if cur_a.get("by") == "staff":
            continue
        a = {"answer": v.get("answer") or cur_a.get("answer"), "by": "ai",
             "met": (v["met"] or bool(cur_a.get("met"))) if isinstance(v.get("met"), bool) else cur_a.get("met")}
        new_ans[qid] = a
    if new_ans != old_ans:
        before["qual_answers"] = old_ans or None
        sets["qual_answers"] = psycopg2.extras.Json(new_ans)
        lead["qual_answers"] = new_ans
    # The AI's judgement of each criterion.
    checks = dict(lead.get("qual_checks") or {})
    new_checks = dict(checks)
    for k, v in (r.get("checks") or {}).items():
        if isinstance(v, bool):
            # Once met, a criterion stays met when a later message doesn't repeat it.
            new_checks[k] = v or bool(checks.get(k))
    if new_checks != checks:
        before["qual_checks"] = checks or None
        sets["qual_checks"] = psycopg2.extras.Json(new_checks)
        lead["qual_checks"] = new_checks
    if sets:
        cols = ", ".join(f"{c}=%s" for c in sets)
        cur.execute(f"UPDATE merchant_pipeline_leads SET {cols}, updated_at=NOW() WHERE id=%s",
                    list(sets.values()) + [lead["id"]])
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in before.items()}


def _qualify(cur, lead: dict, why: str) -> bool:
    if lead.get("is_opportunity") or not _is_open(lead):
        return False
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
                        (lead["tenant_id"], name[:200], "ai"))
            company_id = cur.fetchone()["id"]
    cur.execute("""UPDATE merchant_pipeline_leads SET is_opportunity=TRUE, opportunity_at=NOW(), updated_at=NOW(),
                          company_id=COALESCE(company_id, %s) WHERE id=%s AND NOT is_opportunity""",
                (company_id, lead["id"]))
    if not cur.rowcount:
        return False
    _stage_history(cur, lead["id"], lead["stage"], "opportunity", f"Qualified by AI — became an Opportunity. {why}")
    lead["is_opportunity"] = True
    return True


def _not_a_fit(cur, lead: dict, reason: str) -> bool:
    if not _is_open(lead):
        return False
    cur.execute("""UPDATE merchant_pipeline_leads SET dropped_at=NOW(), dropped_reason=%s, outcome='not_a_fit',
                          updated_at=NOW() WHERE id=%s AND outcome IS NULL""", (reason[:300], lead["id"]))
    if not cur.rowcount:
        return False
    _stage_history(cur, lead["id"], lead["stage"], "not_a_fit", reason)
    try:
        import lead_followups
        lead_followups.on_close(cur, lead["id"])
    except Exception as e:
        print("⚠️ lead_qualify follow-up close:", e)
    lead["outcome"] = "not_a_fit"
    return True


def _log_decision(cur, tenant_id, lead_id, channel, r, summary, total, tier, before, created, qualified):
    cur.execute("""INSERT INTO lead_ai_decisions (tenant_id, lead_id, channel, intent, summary, reason, score, tier,
                                                  before, created_lead, qualified)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (tenant_id, lead_id, channel, r.get("intent"), summary[:500], (r.get("reason") or "")[:1000],
                 total, tier, psycopg2.extras.Json(before), created, qualified))
    return cur.fetchone()["id"]


def act(tenant_id: int, channel: str, r: dict, ctx: dict, s: dict) -> dict:
    """Do what the reading says. ctx: lead_id (known), email, wa_number,
    name, inbox_owner (key, label), source_key, text. Returns what was done."""
    done = {"intent": r.get("intent")}
    region = r.get("buyer_country")
    if not region and ctx.get("wa_number"):
        try:
            import phonenumbers
            region = phonenumbers.region_code_for_number(phonenumbers.parse("+" + _digits(ctx["wa_number"]), None))
        except Exception:
            region = None
    r["phones"] = _norm_phones(r.get("phones"), region)
    conn, cur = _db()
    try:
        lead, created, check = None, False, None
        if ctx.get("lead_id"):
            cur.execute(f"SELECT {_LEAD_COLS} FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s",
                        (ctx["lead_id"], tenant_id))
            lead = cur.fetchone()
        if not lead:
            if not r.get("is_enquiry") or r.get("intent") in ("not_enquiry", "auto_reply"):
                return {**done, "skipped": True}
            if channel == "web" and not (r.get("email") or r.get("phones")):
                # A website visitor who left no email or number: no lead yet
                # (nobody could contact them). Read again when they write more.
                return {**done, "skipped": True, "no_contact": True}
            emails = [e for e in (ctx.get("email"), r.get("email")) if e]
            phones = [p for p in [ctx.get("wa_number")] + list(r.get("phones") or []) if p]
            m = find_buyer(cur, tenant_id, emails, phones, r.get("company"), r.get("website"))
            if m.get("lead") and _is_open(m["lead"]) and not (r.get("new_requirement") and m["lead"].get("is_opportunity")):
                lead = m["lead"]
                done["matched"] = m["how"]
            else:
                like = m.get("lead") or m.get("company_lead")
                lead_id = _create_lead(cur, tenant_id, r, channel, ctx, like=like)
                created = True
                cur.execute(f"SELECT {_LEAD_COLS} FROM merchant_pipeline_leads WHERE id=%s", (lead_id,))
                lead = cur.fetchone()
                if m.get("lead"):
                    done["repeat_of"] = m["lead"]["id"]
                if m.get("unsure"):
                    check = (m["unsure"]["id"], f"Same company name as {m['unsure']['customer_name']}, "
                                                "but a different email and number. The same buyer?")
        lead = dict(lead)
        wa = _digits(ctx.get("wa_number"))
        if wa and not lead.get("whatsapp_number"):
            # They wrote on WhatsApp from this number: it is their WhatsApp number.
            cur.execute("""UPDATE merchant_pipeline_leads SET whatsapp_number=%s WHERE id=%s
                           AND COALESCE(whatsapp_number,'')=''""", (wa, lead["id"]))
            lead["whatsapp_number"] = wa
        staff_owned = lead.get("qual_by") == "staff"
        before = {"_open": _is_open(lead), "is_opportunity": lead.get("is_opportunity"), "stage": lead.get("stage"),
                  "assigned_key": lead.get("assigned_key"), "assigned_to": lead.get("assigned_to"),
                  "outcome": lead.get("outcome"), "dropped_reason": lead.get("dropped_reason"),
                  "qual_by": lead.get("qual_by"), "qual_reason": lead.get("qual_reason")}
        before["fields"] = _fill(cur, lead, r, staff_owned)
        total, tier, _ = score(lead, s)
        intent = r.get("intent")
        actions = []

        # Owner for a lead nobody owns.
        if not lead.get("assigned_key") and (created or intent in STRONG_INTENTS or tier == "hot"):
            who = _pick_owner(cur, tenant_id, s, ctx.get("inbox_owner"))
            if who:
                cur.execute("""UPDATE merchant_pipeline_leads SET assigned_key=%s, assigned_to=%s
                               WHERE id=%s AND assigned_key IS NULL""", (who[0], who[1], lead["id"]))
                if cur.rowcount:
                    lead["assigned_key"], lead["assigned_to"] = who
                    actions.append(f"assigned to {who[1]}")

        qualified = False
        reason = (r.get("reason") or "").strip()
        if intent == "not_interested" and channel in ("chat", "whatsapp", "web"):
            pass    # a chat is too casual ("just looking") to close a lead by itself; it stays as it is
        elif intent == "not_interested":
            why = "Not interested" + (f": {r['not_fit_reason']}" if r.get("not_fit_reason") else "")
            if _not_a_fit(cur, lead, why):
                actions.append("closed as Not a Fit, follow-ups stopped")
        elif _is_open(lead) and s["on_hot_qualify"] and (tier == "hot" or intent in STRONG_INTENTS):
            qualified = _qualify(cur, lead, reason)
            if qualified:
                actions.append("Qualified — moved to the Sales Pipeline")

        cur.execute("""UPDATE merchant_pipeline_leads SET qual_reason=%s, qual_at=NOW(),
                              qual_by=CASE WHEN %s THEN 'ai' ELSE qual_by END WHERE id=%s""",
                    (reason[:1000] or None, qualified or not staff_owned, lead["id"]))
        summary_bits = [INTENT_WORDS.get(intent, "Enquiry"), f"{tier.capitalize()} · {total}"] + actions
        summary = " · ".join(summary_bits)
        dec_id = _log_decision(cur, tenant_id, lead["id"], channel, r, summary, total, tier, before, created, qualified)
        if check:
            cur.execute("""INSERT INTO lead_check_items (tenant_id, lead_id, other_lead_id, kind, reason)
                           VALUES (%s,%s,%s,'duplicate',%s)""", (tenant_id, lead["id"], check[0], check[1]))
        elif r.get("confidence") is not None and r["confidence"] < 0.6 and r.get("is_enquiry"):
            cur.execute("""INSERT INTO lead_check_items (tenant_id, lead_id, kind, reason)
                           SELECT %s,%s,'unsure',%s WHERE NOT EXISTS (SELECT 1 FROM lead_check_items
                                  WHERE lead_id=%s AND kind='unsure' AND status='open')""",
                        (tenant_id, lead["id"], f"The AI wasn't sure about this {CHANNEL_WORDS[channel]}: {reason}"[:1000],
                         lead["id"]))
        conn.commit()
        done.update({"lead_id": lead["id"], "created": created, "score": total, "tier": tier,
                     "qualified": qualified, "decision_id": dec_id, "actions": actions})
    except Exception as e:
        conn.rollback()
        print(f"⚠️ lead_qualify.act tenant={tenant_id} channel={channel}:", e)
        return {**done, "error": str(e)}
    finally:
        cur.close(); conn.close()

    # Contact link, follow-up, alert — after the lead is saved.
    lead_id = done["lead_id"]
    if created:
        try:
            from portal_routes import _link_lead_to_contact_now
            _link_lead_to_contact_now(tenant_id, lead_id)
        except Exception as e:
            print("⚠️ lead_qualify contact link:", e)
    _after(tenant_id, lead_id, channel, r, done, s)
    return done


def _after(tenant_id, lead_id, channel, r, done, s):
    intent = r.get("intent")
    conn, cur = _db()
    try:
        cur.execute("SELECT assigned_key, customer_name, contact_person FROM merchant_pipeline_leads WHERE id=%s",
                    (lead_id,))
        lead = cur.fetchone() or {}
    finally:
        cur.close(); conn.close()
    try:
        import lead_followups as _fu
        today = _fu._today(tenant_id)
        actor = {"key": lead.get("assigned_key") or "", "label": AI_NAME}
        ch = "whatsapp" if channel == "whatsapp" else ("email" if channel in ("email", "reply") else None)
        if intent == "not_now":
            due = None
            try:
                due = date.fromisoformat(str(r.get("follow_up_date"))[:10]) if r.get("follow_up_date") else None
            except ValueError:
                due = None
            if not due or due <= today:
                due = today + timedelta(days=60)
            _fu.set_next(tenant_id, lead_id, due, actor, source="reply_logged",
                         note=f"Not now — AI: {r.get('reason') or ''}"[:300], channel=ch)
            done["followup"] = due.isoformat()
        elif done.get("qualified") or intent in STRONG_INTENTS:
            _fu.set_next(tenant_id, lead_id, today, actor, source="reply_logged",
                         note=f"AI: {r.get('reason') or ''}"[:300], channel=ch)
            done["followup"] = today.isoformat()
        elif done.get("created") and done.get("tier") == "warm":
            due = _fu.add_working_days(today, 2)
            _fu.set_next(tenant_id, lead_id, due, actor, source="reply_logged",
                         note=f"AI: {r.get('reason') or ''}"[:300], channel=ch)
            done["followup"] = due.isoformat()
    except Exception as e:
        print("⚠️ lead_qualify follow-up:", e)
    # Tell the lead's owner when a buyer is qualified (once per lead).
    if done.get("qualified") and s.get("on_hot_alert"):
        try:
            import auto_followups as _af
            conn, cur = _db()
            try:
                who = lead.get("contact_person") or lead.get("customer_name") or "A buyer"
                biz = lead.get("customer_name") or ""
                lines = [f"The AI qualified {who}{' (' + biz + ')' if biz and biz != who else ''} "
                         f"from a {CHANNEL_WORDS.get(channel, channel)}. Score {done['score']}.",
                         f"Why: {r.get('reason') or '—'}",
                         "It is on the Sales Pipeline and on your Follow-ups for today.",
                         "If the AI got it wrong, open the deal and press Undo."]
                done["alert"] = _af.notify(cur, tenant_id, "owner", f"ai_qualified:{lead_id}",
                                           f"Qualified by AI: {biz or who}", lines, lead_id=lead_id,
                                           link=f"{PORTAL}/sales-pipeline/{lead_id}")
            finally:
                cur.close(); conn.close()
        except Exception as e:
            print("⚠️ lead_qualify alert:", e)


# ── Undo, Check these ───────────────────────────────────────────────────────

def lead_decisions(tenant_id: int, lead_id: int, limit: int = 10) -> list:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT id, channel, intent, summary, reason, score, tier, created_lead, qualified,
                              created_at, undone_at, undone_by
                         FROM lead_ai_decisions WHERE tenant_id=%s AND lead_id=%s
                        ORDER BY created_at DESC LIMIT %s""", (tenant_id, lead_id, limit))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def undo_target(decisions: list, lead: dict):
    """The decision the lead page's Undo button undoes: the AI qualifying
    this deal while it is still on the Sales Pipeline, else the AI closing
    it, else the AI's latest decision."""
    live = [d for d in decisions if not d.get("undone_at")]
    if lead.get("is_opportunity"):
        q = next((d for d in live if d.get("qualified")), None)
        if q:
            return q
    if lead.get("outcome") == "not_a_fit":
        q = next((d for d in live if d.get("intent") == "not_interested"), None)
        if q:
            return q
    return live[0] if live else None


def undo(tenant_id: int, decision_id: int, by_label: str, lead_id: int = None) -> tuple:
    """Undo one AI decision. Qualified → back to All Leads (what the buyer
    told us stays); closed → reopened; a lead the AI made → closed as not a
    real enquiry; otherwise the details it saved go back as they were."""
    conn, cur = _db()
    try:
        cur.execute("""SELECT * FROM lead_ai_decisions WHERE id=%s AND tenant_id=%s AND undone_at IS NULL FOR UPDATE""",
                    (decision_id, tenant_id))
        d = cur.fetchone()
        if not d or (lead_id and d["lead_id"] != lead_id):
            return False, "Already undone.", None
        b = d["before"] or {}
        cur.execute(f"SELECT {_LEAD_COLS} FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s", (d["lead_id"], tenant_id))
        lead = cur.fetchone()
        if not lead:
            return False, "Lead not found.", None
        note = f"AI decision undone by {by_label}"
        sets, hist, msg = {}, None, "Undone."
        if d["qualified"] and lead["is_opportunity"]:
            sets.update({"is_opportunity": False, "opportunity_at": None, "qual_by": None})
            hist = ("opportunity", lead["stage"], f"{note} — back to All Leads")
            msg = "Undone — the lead is back in All Leads, waiting for a decision."
        elif d["intent"] == "not_interested" and lead["outcome"] == "not_a_fit" and b.get("_open"):
            sets.update({"dropped_at": None, "dropped_reason": None, "outcome": None})
            hist = ("not_a_fit", lead["stage"], f"{note} — reopened")
            msg = "Undone — the lead is open again."
        elif d["created_lead"] and _is_open(lead):
            sets.update({"dropped_at": datetime.now(timezone.utc), "outcome": "not_a_fit", "dropped_reason": "Invalid lead"})
            hist = (lead["stage"], "not_a_fit", f"{note} — not a real enquiry")
            msg = "Undone — the lead is closed as not a real enquiry."
        else:
            for col, val in (b.get("fields") or {}).items():
                if col in ("qual_checks", "qual_answers"):
                    sets[col] = psycopg2.extras.Json(val) if val else None
                elif col == "deadline_date":
                    sets[col] = date.fromisoformat(val) if val else None
                else:
                    sets[col] = val
            sets["qual_reason"] = b.get("qual_reason")
            msg = "Undone — the details the AI saved are back as they were."
        cols = ", ".join(f"{c}=%s" for c in sets)
        cur.execute(f"UPDATE merchant_pipeline_leads SET {cols}, updated_at=NOW() WHERE id=%s",
                    list(sets.values()) + [lead["id"]])
        if hist:
            cur.execute("""INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                           VALUES (%s,%s,%s,%s,%s)""", (lead["id"], hist[0], hist[1], by_label[:100], hist[2]))
        if sets.get("outcome") == "not_a_fit":
            import lead_followups
            lead_followups.on_close(cur, lead["id"])
        cur.execute("UPDATE lead_ai_decisions SET undone_at=NOW(), undone_by=%s WHERE id=%s", (by_label[:200], decision_id))
        conn.commit()
        return True, msg, lead["id"]
    except Exception as e:
        conn.rollback()
        print("⚠️ lead_qualify.undo:", e)
        return False, "Couldn't undo this. Please try again.", None
    finally:
        cur.close(); conn.close()


def check_items(tenant_id: int, scope_sql: str = "", scope_params=()) -> list:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute(f"""SELECT ci.*, l.customer_name, l.contact_person, l.email, l.phone, l.whatsapp_number,
                               l.product_interest, l.quantity, l.is_opportunity, l.qual_reason, l.source,
                               o.customer_name AS o_name, o.contact_person AS o_person, o.email AS o_email,
                               o.phone AS o_phone, o.whatsapp_number AS o_wa, o.product_interest AS o_product,
                               o.is_opportunity AS o_is_opp
                          FROM lead_check_items ci
                          JOIN merchant_pipeline_leads l ON l.id = ci.lead_id
                          LEFT JOIN merchant_pipeline_leads o ON o.id = ci.other_lead_id
                         WHERE ci.tenant_id=%s AND ci.status='open' {scope_sql}
                         ORDER BY ci.created_at DESC LIMIT 200""", (tenant_id, *scope_params))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def open_check_count(tenant_id: int) -> int:
    try:
        ensure_tables()
        conn, cur = _db()
        try:
            cur.execute("SELECT count(*) AS n FROM lead_check_items WHERE tenant_id=%s AND status='open'", (tenant_id,))
            return int(cur.fetchone()["n"])
        finally:
            cur.close(); conn.close()
    except Exception:
        return 0


def resolve_check(tenant_id: int, item_id: int, action: str, by_label: str) -> tuple:
    """action: merge | separate | done."""
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM lead_check_items WHERE id=%s AND tenant_id=%s AND status='open' FOR UPDATE",
                    (item_id, tenant_id))
        it = cur.fetchone()
        if not it:
            return False, "Already sorted."
        msg = "Done."
        if action == "merge" and it["other_lead_id"]:
            cur.execute(f"SELECT {_LEAD_COLS} FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s",
                        (it["lead_id"], tenant_id))
            new = cur.fetchone()
            cur.execute(f"SELECT {_LEAD_COLS} FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s",
                        (it["other_lead_id"], tenant_id))
            old = cur.fetchone()
            if not new or not old:
                return False, "One of the two leads no longer exists."
            sets = {}
            for col in ("contact_person", "email", "phone", "whatsapp_number", "website", "product_interest",
                        "business_type", "quantity", "delivery_country", "purchase_timeline", "deadline_date",
                        "addr_country", "addr_city"):
                if not _filled(old.get(col)) and _filled(new.get(col)):
                    sets[col] = new[col]
            extra = []
            if _filled(new.get("email")) and new["email"] != old.get("email") and "email" not in sets \
                    and new["email"] not in (old.get("other_emails") or ""):
                sets["other_emails"] = ", ".join(x for x in (old.get("other_emails"), new["email"]) if x)
            for f in ("phone", "whatsapp_number"):
                if _filled(new.get(f)) and f not in sets and _digits(new[f])[-9:] not in \
                        (_digits(old.get("phone")) + _digits(old.get("whatsapp_number")) + _digits(old.get("notes"))):
                    extra.append(new[f])
            if new.get("decision_maker_engaged") and not old.get("decision_maker_engaged"):
                sets["decision_maker_engaged"] = True
            oc, nc = dict(old.get("qual_checks") or {}), dict(new.get("qual_checks") or {})
            if nc:
                sets["qual_checks"] = psycopg2.extras.Json({k: bool(oc.get(k)) or bool(nc.get(k)) for k in set(oc) | set(nc)})
            oa, na = answers_for(old), answers_for(new)
            if any(k not in oa for k in na):
                sets["qual_answers"] = psycopg2.extras.Json({**na, **oa})
            if extra:
                line = "Other numbers (merged): " + ", ".join(extra)
                sets["notes"] = (old["notes"] + "\n" + line) if old.get("notes") else line
            if sets:
                cols = ", ".join(f"{c}=%s" for c in sets)
                cur.execute(f"UPDATE merchant_pipeline_leads SET {cols}, updated_at=NOW() WHERE id=%s",
                            list(sets.values()) + [old["id"]])
            # Their messages and the AI's notes go with them.
            cur.execute("UPDATE email_replies SET lead_id=%s WHERE lead_id=%s AND tenant_id=%s", (old["id"], new["id"], tenant_id))
            cur.execute("UPDATE email_events SET lead_id=%s WHERE lead_id=%s AND tenant_id=%s", (old["id"], new["id"], tenant_id))
            cur.execute("UPDATE lead_qual_sources SET lead_id=%s WHERE lead_id=%s AND tenant_id=%s", (old["id"], new["id"], tenant_id))
            cur.execute("UPDATE lead_ai_decisions SET lead_id=%s WHERE lead_id=%s AND tenant_id=%s", (old["id"], new["id"], tenant_id))
            cur.execute("""UPDATE merchant_pipeline_leads SET dropped_at=NOW(), outcome='not_a_fit', updated_at=NOW(),
                                  dropped_reason='Duplicate' WHERE id=%s AND outcome IS NULL""", (new["id"],))
            cur.execute("""INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                           VALUES (%s,%s,'not_a_fit',%s,%s)""",
                        (new["id"], new["stage"], by_label[:100], f"Merged into {old['customer_name']} (same buyer)"))
            cur.execute("""INSERT INTO merchant_pipeline_stage_history (lead_id, from_stage, to_stage, changed_by, notes)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (old["id"], old["stage"], old["stage"], by_label[:100],
                         f"Merged in the new enquiry from {new['contact_person'] or new['customer_name']}"))
            import lead_followups
            lead_followups.on_close(cur, new["id"])
            msg = f"Merged into {old['customer_name']}."
        elif action == "separate":
            msg = "Kept as two separate buyers."
        cur.execute("""UPDATE lead_check_items SET status=%s, resolved_by=%s, resolved_at=NOW() WHERE id=%s""",
                    ({"merge": "merged", "separate": "kept"}.get(action, "done"), by_label[:200], item_id))
        conn.commit()
        return True, msg
    except Exception as e:
        conn.rollback()
        print("⚠️ lead_qualify.resolve_check:", e)
        return False, "Couldn't do that. Please try again."
    finally:
        cur.close(); conn.close()


# ── channel 1: email ────────────────────────────────────────────────────────

def _seen(cur, tenant_id, key) -> bool:
    cur.execute("SELECT 1 FROM lead_qual_sources WHERE tenant_id=%s AND source_key=%s", (tenant_id, key))
    return cur.fetchone() is not None


def _mark(tenant_id, key, verdict, lead_id=None, last_seen=None):
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO lead_qual_sources (tenant_id, source_key, lead_id, last_seen, verdict, updated_at)
                       VALUES (%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT (tenant_id, source_key) DO UPDATE SET lead_id=COALESCE(EXCLUDED.lead_id, lead_qual_sources.lead_id),
                              last_seen=EXCLUDED.last_seen, verdict=EXCLUDED.verdict, updated_at=NOW()""",
                    (tenant_id, key[:500], lead_id, last_seen, verdict[:20]))
        conn.commit()
    finally:
        cur.close(); conn.close()


def on_inbox_email(mb: dict, msg: dict, sender: str, received: datetime, token: str) -> dict:
    """A message in a connected mailbox that isn't a reply to a campaign.
    Never raises. Only a buyer's enquiry is kept (on the lead, like a reply)."""
    tenant_id = mb["tenant_id"]
    try:
        s = active(tenant_id)
        if not s or not s["read_inbox"]:
            return {}
        if s.get("since") and received < s["since"]:
            return {}
        own_dom = _domain(mb.get("email"))
        dom = _domain(sender)
        if not dom or (own_dom and dom == own_dom) or _SKIP_SENDER.search(sender) or _SKIP_DOMAINS.search(dom):
            return {}
        key = f"mail:{mb['id']}:{msg['id']}"
        conn, cur = _db()
        try:
            if _seen(cur, tenant_id, key):
                return {}
            # Colleagues on the team are never buyers.
            cur.execute("""SELECT 1 FROM team_members WHERE tenant_id=%s AND lower(email)=%s
                           UNION ALL SELECT 1 FROM customers WHERE tenant_id=%s AND lower(email)=%s LIMIT 1""",
                        (tenant_id, sender, tenant_id, sender))
            if cur.fetchone():
                return {}
        finally:
            cur.close(); conn.close()
        import outlook_mail as M
        if M.is_automatic(token, msg) or _is_newsletter(token, msg):
            _mark(tenant_id, key, "skipped")
            return {}
        body = _message_text(token, msg)
        name = (((msg.get("from") or {}).get("emailAddress") or {}).get("name") or "").strip()
        text = f"From: {name} <{sender}>\nSubject: {msg.get('subject') or ''}\n\n{body}"
        r = read(tenant_id, "email", text, s)
        if not r.get("is_enquiry") or r.get("intent") in ("not_enquiry", "auto_reply"):
            _mark(tenant_id, key, "not_enquiry")    # nothing about the email is kept
            return {"skipped": True}
        ctx = {"email": sender, "name": name if name and "@" not in name else None,
               "inbox_owner": (mb["owner_key"], mb.get("owner_label") or mb.get("email"))}
        done = act(tenant_id, "email", r, ctx, s)
        if done.get("lead_id"):
            _record_email_on_lead(mb, msg, sender, received, done["lead_id"], body)
            _quote_hook(tenant_id, "email", r, done, key, body)
        _mark(tenant_id, key, r.get("intent") or "enquiry", done.get("lead_id"))
        print(f"   [AI-QUALIFY] email tenant={tenant_id} from={sender} → {done}")
        return done
    except Exception as e:
        print(f"⚠️ lead_qualify.on_inbox_email tenant={tenant_id}:", e)
        return {}


def _message_text(token, msg) -> str:
    import outlook_mail as M
    try:
        d = M._get(token, f"{M.GRAPH}/me/messages/{msg['id']}", {"$select": "uniqueBody,body"})
        txt = ((d.get("uniqueBody") or {}).get("content") or (d.get("body") or {}).get("content") or "").strip()
        if txt:
            return re.sub(r"\n{3,}", "\n\n", txt)[:8000]
    except Exception as e:
        print("⚠️ lead_qualify message text:", e)
    return msg.get("bodyPreview") or ""


def _is_newsletter(token, msg) -> bool:
    import outlook_mail as M
    try:
        d = M._get(token, f"{M.GRAPH}/me/messages/{msg['id']}", {"$select": "internetMessageHeaders"})
    except Exception:
        return False
    for h in d.get("internetMessageHeaders") or []:
        if (h.get("name") or "").lower() in ("list-unsubscribe", "list-id"):
            return True
    return False


def _record_email_on_lead(mb, msg, sender, received, lead_id, body):
    """The enquiry goes on the lead's Emails tab (like a reply)."""
    import outlook_mail as M
    conn, cur = _db()
    try:
        cur.execute("SELECT wa_contact_id FROM merchant_pipeline_leads WHERE id=%s", (lead_id,))
        contact_id = (cur.fetchone() or {}).get("wa_contact_id")
        cur.execute("""INSERT INTO email_replies (tenant_id, mailbox_id, message_id, lead_id, contact_id, from_email,
                                                  subject, snippet, web_link, received_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (mailbox_id, message_id) DO NOTHING RETURNING id""",
                    (mb["tenant_id"], mb["id"], msg["id"], lead_id, contact_id, sender,
                     (msg.get("subject") or "")[:500], M.reply_lines(body, 600), msg.get("webLink"), received))
        row = cur.fetchone()
        if row:
            cur.execute("""INSERT INTO email_events (tenant_id, lead_id, contact_id, email, kind, detail, actor, created_at)
                           VALUES (%s,%s,%s,%s,'enquiry',%s,%s,%s) RETURNING id""",
                        (mb["tenant_id"], lead_id, contact_id, sender, "New enquiry email",
                         f"Outlook ({mb.get('owner_label') or mb.get('email')})", received))
            cur.execute("UPDATE email_replies SET event_id=%s WHERE id=%s", (cur.fetchone()["id"], row["id"]))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️ lead_qualify record email:", e)
    finally:
        cur.close(); conn.close()


# ── channel 2: replies to the business's emails ─────────────────────────────

def on_reply(tenant_id: int, lead_id: int, reply_id: int, text: str, owner: tuple = None) -> dict:
    """After email_reply_actions.on_reply: what the reply means."""
    try:
        s = active(tenant_id)
        if not s or not lead_id or not (text or "").strip():
            return {}
        conn, cur = _db()
        try:
            cur.execute(f"SELECT {_LEAD_COLS} FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s", (lead_id, tenant_id))
            lead = cur.fetchone()
        finally:
            cur.close(); conn.close()
        if not lead:
            return {}
        known = {k: lead.get(k) for k in ("customer_name", "contact_person", "product_interest", "quantity",
                                           "delivery_country", "purchase_timeline", "business_type")}
        if answers_for(lead):
            known["answers to the business's own questions"] = {k: v.get("answer") for k, v in answers_for(lead).items()}
        r = read(tenant_id, "reply", text, s, known=known)
        if r.get("intent") in ("auto_reply", "not_enquiry") and not r.get("is_enquiry"):
            return {"skipped": True}
        r["is_enquiry"] = True
        done = act(tenant_id, "reply", r, {"lead_id": lead_id, "inbox_owner": owner}, s)
        _quote_hook(tenant_id, "reply", r, done, f"reply:{reply_id}", text)
        print(f"   [AI-QUALIFY] reply tenant={tenant_id} lead={lead_id} → {done}")
        return done
    except Exception as e:
        print(f"⚠️ lead_qualify.on_reply tenant={tenant_id}:", e)
        return {}


def _quote_hook(tenant_id, channel, r, done, source_key, buyer_words):
    """A price request / full RFQ naming products becomes a quote request
    in Quotations › Requests (Intelligent RFQ Phase 2). Never raises."""
    try:
        if not done.get("lead_id") or done.get("error"):
            return
        import quote_requests
        res = quote_requests.from_ai(tenant_id, done["lead_id"], channel, r, source_key, buyer_words)
        if res:
            done["quote_request"] = res
    except Exception as e:
        print(f"⚠️ lead_qualify quote hook tenant={tenant_id}:", e)


# ── channel 3: WhatsApp and website chats ───────────────────────────────────

CHAT_PAUSE = timedelta(seconds=45)     # read a chat once the buyer has paused
CHAT_LOOK_BACK = timedelta(days=2)
_SKIP_SESSIONS = ("test-", "demo-", "cw-")


def _chat_channel(session_id: str) -> tuple:
    """('whatsapp', number) or ('web', None)."""
    m = re.match(r"^wa-(?:meta-)?(?:\d+-)?(\d{7,15})$", session_id or "")
    if session_id.startswith("wa-") and m:
        return "whatsapp", m.group(1)
    if session_id.startswith("wa-"):
        return "whatsapp", (re.findall(r"(\d{7,15})$", session_id) or [None])[0]
    return "web", None


def _due_chats(tenant_ids: list) -> list:
    conn, cur = _db()
    try:
        cur.execute("""SELECT m.tenant_id, m.session_id, max(m.id) AS last_id
                         FROM chat_messages m
                        WHERE m.tenant_id = ANY(%s) AND m.role='user' AND m.created_at > NOW() - %s
                          AND NOT (m.session_id LIKE 'test-%%' OR m.session_id LIKE 'demo-%%' OR m.session_id LIKE 'cw-%%')
                        GROUP BY m.tenant_id, m.session_id
                       HAVING max(m.created_at) < NOW() - %s""", (tenant_ids, CHAT_LOOK_BACK, CHAT_PAUSE))
        rows = cur.fetchall()
        out = []
        for r in rows:
            cur.execute("SELECT last_seen, lead_id FROM lead_qual_sources WHERE tenant_id=%s AND source_key=%s",
                        (r["tenant_id"], "chat:" + r["session_id"]))
            seen = cur.fetchone()
            if seen and (seen["last_seen"] or 0) >= r["last_id"]:
                continue
            r["lead_id"] = (seen or {}).get("lead_id")
            out.append(r)
        conn.commit()
        return out[:20]
    finally:
        cur.close(); conn.close()


def qualify_chat(tenant_id: int, session_id: str, last_id: int, lead_id: int = None, s: dict = None) -> dict:
    s = s or active(tenant_id)
    key = "chat:" + session_id
    if not s:
        return {}
    channel, number = _chat_channel(session_id)
    conn, cur = _db()
    try:
        cur.execute("""SELECT role, content FROM (SELECT id, role, content FROM chat_messages
                         WHERE tenant_id=%s AND session_id=%s AND id <= %s ORDER BY id DESC LIMIT 40) x ORDER BY id""",
                    (tenant_id, session_id, last_id))
        msgs = cur.fetchall()
        known = None
        if not lead_id and number:
            # First read of this WhatsApp chat: a buyer already on the list, found by number.
            m = find_buyer(cur, tenant_id, (), [number])
            if m.get("lead") and _is_open(m["lead"]):
                lead_id = m["lead"]["id"]
        if lead_id:
            cur.execute(f"SELECT {_LEAD_COLS} FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s", (lead_id, tenant_id))
            lead = cur.fetchone()
            if lead and _is_open(lead):
                known = {k: lead.get(k) for k in ("customer_name", "contact_person", "product_interest", "quantity",
                                                   "delivery_country", "purchase_timeline", "business_type")}
                if answers_for(lead):
                    known["answers to the business's own questions"] = {k: v.get("answer") for k, v in answers_for(lead).items()}
            else:
                lead_id = None
    finally:
        cur.close(); conn.close()
    lines = []
    for m in msgs:
        txt = re.sub(r"\s+", " ", m["content"] or "").strip()
        if txt:
            lines.append(("BUYER: " if m["role"] == "user" else "AGENT: ") + txt[:800])
    if not lines:
        _mark(tenant_id, key, "empty", lead_id, last_id)
        return {}
    r = read(tenant_id, "chat", "\n".join(lines), s, known=known)
    if not lead_id and (not r.get("is_enquiry") or r.get("intent") in ("not_enquiry", "auto_reply")):
        _mark(tenant_id, key, "not_enquiry", None, last_id)
        return {"skipped": True}
    ctx = {"lead_id": lead_id, "wa_number": number}
    if lead_id:
        r["is_enquiry"] = True
    done = act(tenant_id, channel, r, ctx, s)
    _quote_hook(tenant_id, channel, r, done, key,
                "\n".join(l[7:] for l in lines if l.startswith("BUYER: ")))
    _mark(tenant_id, key, "no_contact" if done.get("no_contact") else (r.get("intent") or "enquiry"),
          done.get("lead_id"), last_id)
    print(f"   [AI-QUALIFY] chat tenant={tenant_id} session={session_id} → {done}")
    return done


def _tenants_on() -> list:
    conn, cur = _db()
    try:
        cur.execute("""SELECT DISTINCT t.id FROM tenants t JOIN plan_feature_grants g ON g.plan_id=t.plan_id
                        WHERE g.feature_key=%s""", (FEATURE_KEY,))
        ids = [r["id"] for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    try:
        import accounts_core
        pid = accounts_core.platform_tenant_id()
        if pid and pid not in ids:
            ids.append(pid)
    except Exception:
        pass
    return [t for t in ids if active(t)]


def _loop():
    time.sleep(40)
    while True:
        lock_conn = None
        try:
            ensure_tables()
            lock_conn = get_db_connection()
            lock_conn.autocommit = True
            lc = lock_conn.cursor()
            lc.execute("SELECT pg_try_advisory_lock(80301030)")
            if lc.fetchone()[0]:     # only one portal worker reads chats at a time
                try:
                    tenants = _tenants_on()
                    if tenants:
                        cache = {}
                        for c in _due_chats(tenants):
                            s = cache.setdefault(c["tenant_id"], active(c["tenant_id"]))
                            try:
                                qualify_chat(c["tenant_id"], c["session_id"], c["last_id"], c.get("lead_id"), s)
                            except Exception as e:
                                print(f"⚠️ lead_qualify chat {c['session_id']}:", e)
                                _mark(c["tenant_id"], "chat:" + c["session_id"], "error", c.get("lead_id"), c["last_id"])
                finally:
                    lc.execute("SELECT pg_advisory_unlock(80301030)")
        except Exception as e:
            print("⚠️ lead_qualify loop:", e)
        finally:
            if lock_conn:
                try:
                    lock_conn.close()
                except Exception:
                    pass
        time.sleep(60)


def start():
    if not getattr(threading, "_phixtra_lead_qualify_started", False):
        threading._phixtra_lead_qualify_started = True  # type: ignore[attr-defined]
        threading.Thread(target=_loop, daemon=True).start()
