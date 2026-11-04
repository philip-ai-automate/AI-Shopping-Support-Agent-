"""
outreach_drafts.py — AI outreach drafts in Outlook (PhiXtra pilot, approved
by the user 4 Nov 2026).

  1. Someone ticks deals on the Sales Pipeline and presses "Write AI drafts".
  2. The AI writes a personal email to each decision maker (their name, job,
     company and website, plus PhiXtra's own information and the approved
     sales-plan templates) and SAVES IT AS A DRAFT in the sending person's
     Outlook (pilot: Joanna's). The portal never sends anything.
  3. Philip or David reads it on CRM › AI Email Drafts, edits if needed and
     presses Approve. The draft then carries the Outlook category
     "PhiXtra: checked, OK to send"; unchecked drafts carry "PhiXtra:
     waiting for check".
  4. The sender presses Send in Outlook. The Outlook checker
     (mailbox_routes) sees it: the deal moves to Contacted, the email is
     logged, it counts towards the Daily Target, and the deal's Next
     follow-up is set to the day the next draft is written.
  5. Follow-ups 2–5 are written on their days as replies in the same thread
     and go through the same check → send.
  6. After email 5 and a final wait with no reply, the drafts stop and the
     deal owner is asked to decide (LinkedIn, close as Unresponsive, keep).
     Nothing closes by itself.

Stops by itself: the buyer replies (drafts pause; Philip, David and Joanna
get an email; the AI never writes the answer), the email bounces, or the
deal is Won / Lost / Not a Fit / dropped. A person can press Stop.

Only businesses in OUTREACH_DRAFT_TENANTS (default "19" = PhiXtra) — the
user said PhiXtra only for now (feedback: PhiXtra-only features stay off
the SaaS until asked).
"""
import html as _html
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2.extras
import requests
from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from db import get_db_connection
from feature_access import any_team_member

MAX_EMAILS = 5
DEFAULT_GAPS = [3, 4, 7, 14]            # days after email 1, 2, 3, 4 → email 2, 3, 4, 5 (day 0, 3, 7, 14, 28)
DEFAULT_FINAL_WAIT = 7                  # days after email 5 before the owner is asked to decide
CAT_WAITING = "PhiXtra: waiting for check"
CAT_OK = "PhiXtra: checked, OK to send"
OPEN = ("writing", "waiting", "approved")
PORTAL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")

outreach_bp = Blueprint("outreach", __name__)


def pilot(tenant_id) -> bool:
    raw = os.getenv("OUTREACH_DRAFT_TENANTS", "19")
    try:
        return int(tenant_id) in {int(x) for x in raw.split(",") if x.strip().isdigit()}
    except (TypeError, ValueError):
        return False


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ── tables ─────────────────────────────────────────────────────────────────

_GUIDE = """Who we write to: owners, MDs and sales or commercial directors of UK companies (11–200 staff) that sell to other businesses.
The offer (never "a demo of our software"): "Give me 20 minutes and I'll show you an AI agent answering trade enquiries about your own products, from your own website."
Rules for every email: one personal line about their business (true, from their website or details), one problem, the offer, one easy question. Short, plain text, no attachments, no hype.

Angle 1 (speed): trade buyers often go with whichever supplier answers first. Opening question: "How long does it usually take your team to send a quote after a new enquiry comes in?" PhiXtra gives the team an AI agent that replies to enquiries in minutes, day or night, gathers what the buyer needs, and prepares the quotation for the team to approve.
Angle 2 (lost follow-up): quotes go quiet and nobody chases them. Opening question: "What happens to a quote after it goes out and the buyer goes quiet?" PhiXtra's AI agent answers each enquiry, prepares the quotation for the team to approve, and follows up politely until the buyer replies, so nothing is forgotten.

Example follow-up ideas: an enquiry arriving at 7pm on a Friday; how many quotes last month were followed up twice; buyers who write in another language; showing it working on their own products before the call. The last email is a polite close: "I'll leave it here so I don't fill up your inbox", reply any time.
Philip (a colleague) may also have connected with them on LinkedIn."""

_SIGNOFF_FIRST = "Kind regards,\nJoanna Mae\nPhiXtra | phixtra.com"
_SIGNOFF_FOLLOWUP = "Joanna"
_OPTOUT = "If you'd rather not hear from me, just reply \"no thanks\" and I won't contact you again."
_BOOKING = "https://outlook.office.com/book/TalktoUs1@phixtra.com/"


def ensure_tables() -> None:
    conn, cur = _db()
    try:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("ALTER TABLE mailbox_connections ADD COLUMN IF NOT EXISTS scopes TEXT")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outreach_settings (
                tenant_id        INTEGER PRIMARY KEY,
                mailbox_id       BIGINT,
                gaps             INTEGER[] NOT NULL DEFAULT '{3,4,7,14}',
                final_wait       INTEGER   NOT NULL DEFAULT 7,
                guide            TEXT,
                signoff_first    TEXT,
                signoff_followup TEXT,
                optout           TEXT,
                booking_link     TEXT,
                alert_keys       TEXT[]    NOT NULL DEFAULT '{}',
                updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outreach_runs (
                id                   BIGSERIAL PRIMARY KEY,
                tenant_id            INTEGER     NOT NULL,
                lead_id              BIGINT      NOT NULL,
                mailbox_id           BIGINT      NOT NULL,
                angle                SMALLINT    NOT NULL DEFAULT 1,
                status               VARCHAR(20) NOT NULL DEFAULT 'running',
                emails_sent          INTEGER     NOT NULL DEFAULT 0,
                last_sent_at         TIMESTAMPTZ,
                last_sent_message_id VARCHAR(255),
                next_draft_on        DATE,
                site_text            TEXT,
                last_error           TEXT,
                stop_reason          TEXT,
                started_by_key       VARCHAR(40),
                started_by_name      VARCHAR(200),
                started_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                stopped_at           TIMESTAMPTZ,
                updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (tenant_id, lead_id)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outreach_drafts (
                id               BIGSERIAL PRIMARY KEY,
                tenant_id        INTEGER     NOT NULL,
                run_id           BIGINT      NOT NULL,
                lead_id          BIGINT      NOT NULL,
                step             INTEGER     NOT NULL,
                status           VARCHAR(20) NOT NULL DEFAULT 'writing',
                subject          TEXT,
                body_text        TEXT,
                quoted_html      TEXT,
                graph_id         VARCHAR(255),
                web_link         TEXT,
                error            TEXT,
                checked_by_name  VARCHAR(200),
                checked_at       TIMESTAMPTZ,
                sent_message_id  VARCHAR(255),
                sent_at          TIMESTAMPTZ,
                sent_unchecked   BOOLEAN     NOT NULL DEFAULT FALSE,
                by_hand          BOOLEAN     NOT NULL DEFAULT FALSE,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outreach_signatures (
                tenant_id   INTEGER     NOT NULL,
                mailbox_id  BIGINT      NOT NULL,
                image       BYTEA       NOT NULL,
                mime        VARCHAR(40) NOT NULL,
                width       INTEGER     NOT NULL,
                height      INTEGER     NOT NULL,
                link        TEXT,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (tenant_id, mailbox_id)
            )""")
        cur.execute("ALTER TABLE outreach_drafts ADD COLUMN IF NOT EXISTS greeting TEXT")
        cur.execute("ALTER TABLE outreach_drafts ADD COLUMN IF NOT EXISTS ai_text TEXT")
        cur.execute("ALTER TABLE outreach_drafts ADD COLUMN IF NOT EXISTS tail TEXT")
        cur.execute("ALTER TABLE outreach_settings ADD COLUMN IF NOT EXISTS booking_on BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE outreach_settings ADD COLUMN IF NOT EXISTS morning_email BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE outreach_settings ADD COLUMN IF NOT EXISTS morning_sent_on DATE")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS outreach_drafts_one_open
                       ON outreach_drafts (run_id, step) WHERE status IN ('writing','waiting','approved','sent')""")
        cur.execute("CREATE INDEX IF NOT EXISTS outreach_drafts_t_idx ON outreach_drafts (tenant_id, status, created_at DESC)")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️ outreach_drafts tables:", e)
    finally:
        cur.close(); conn.close()


def settings(tenant_id: int) -> dict:
    """This business's settings, created with the pilot defaults the first time."""
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM outreach_settings WHERE tenant_id=%s", (tenant_id,))
        s = cur.fetchone()
        if not s:
            # Pilot defaults: send from Joanna's mailbox (the user, 4 Nov); alert
            # the owner + IT admins Philip and David, and the sender.
            cur.execute("""SELECT m.id, m.owner_key FROM mailbox_connections m
                           JOIN team_members tm ON 'team:' || tm.id = m.owner_key
                           WHERE m.tenant_id=%s AND m.status='active' AND lower(tm.email)='joanna.mae@phixtra.com'""",
                        (tenant_id,))
            mb = cur.fetchone()
            cur.execute("""SELECT 'team:' || id AS k FROM team_members WHERE tenant_id=%s AND is_active
                           AND lower(email) IN ('philip@phixtra.com','david.o@phixtra.com','joanna.mae@phixtra.com')
                           ORDER BY id""", (tenant_id,))
            keys = [r["k"] for r in cur.fetchall()]
            cur.execute("""INSERT INTO outreach_settings (tenant_id, mailbox_id, gaps, final_wait, guide, signoff_first,
                               signoff_followup, optout, booking_link, alert_keys)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (tenant_id, mb["id"] if mb else None, DEFAULT_GAPS, DEFAULT_FINAL_WAIT, _GUIDE,
                         _SIGNOFF_FIRST, _SIGNOFF_FOLLOWUP, _OPTOUT, _BOOKING, keys))
            conn.commit()
            cur.execute("SELECT * FROM outreach_settings WHERE tenant_id=%s", (tenant_id,))
            s = cur.fetchone()
        s = dict(s)
        s["gaps"] = list(s["gaps"] or DEFAULT_GAPS)[:MAX_EMAILS - 1]
        while len(s["gaps"]) < MAX_EMAILS - 1:
            s["gaps"].append(DEFAULT_GAPS[len(s["gaps"])])
        return s
    finally:
        cur.close(); conn.close()


def schedule_days(s: dict) -> list:
    """Day number of each email: [0, 3, 7, 14, 28] with the default gaps."""
    out, d = [0], 0
    for g in s["gaps"]:
        d += g
        out.append(d)
    return out


# ── Outlook access (the sender's own connected mailbox) ────────────────────

def _mailbox(cur, mailbox_id):
    cur.execute("SELECT * FROM mailbox_connections WHERE id=%s", (mailbox_id,))
    return cur.fetchone()


def can_draft(mb) -> bool:
    return bool(mb and mb["status"] == "active" and "Mail.ReadWrite" in (mb.get("scopes") or ""))


def _token(cur, mb) -> str:
    from mailbox_routes import _access_token
    return _access_token(cur, mb)


# ── the AI ─────────────────────────────────────────────────────────────────

def _site_text(url: str) -> str:
    """Their own website's words (home page), for the personal line."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    got = _site_fetch(url)
    if not got and "://www." not in url:
        got = _site_fetch(url.replace("://", "://www.", 1))
    return got


def _site_fetch(url: str) -> str:
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (compatible; PhiXtra/1.0)"},
                         allow_redirects=True, stream=True)
        raw = r.raw.read(400_000, decode_content=True)
        if r.status_code != 200 or "html" not in (r.headers.get("content-type") or ""):
            return ""
    except Exception:
        return ""
    try:
        page = raw.decode(r.encoding or "utf-8", errors="replace")
        title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        desc = re.search(r"<meta[^>]+name=[\"']description[\"'][^>]*content=[\"']([^\"']*)", page, re.I) or \
            re.search(r"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*name=[\"']description", page, re.I)
        body = re.sub(r"<(script|style|noscript|svg|nav|footer|form|head)\b.*?</\1>", " ", page, flags=re.I | re.S)
        body = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", _html.unescape(body)).strip()
        t = _html.unescape(re.sub(r"\s+", " ", title.group(1))).strip() if title else ""
        dsc = _html.unescape(desc.group(1)).strip() if desc else ""
        if not (t or dsc or text):
            return ""
        return f"Title: {t}\nDescription: {dsc}\nPage text: {text[:2500]}"[:3200]
    except Exception:
        return ""


def _job_title(notes: str) -> str:
    m = re.search(r"Job title:\s*(.+)", notes or "")
    return m.group(1).strip()[:120] if m else ""


def first_name(lead: dict) -> str:
    n = (lead.get("contact_person") or "").strip()
    n = re.sub(r"^(mr|mrs|ms|miss|dr)\.?\s+", "", n, flags=re.I)
    return n.split()[0].capitalize() if n and n.split()[0].isalpha() else ""


_STEP_BRIEF = {
    1: "Email 1, the first email (they don't know us). Structure, in this order: (1) one true personal line about "
       "their business from their website, as its OWN paragraph (nothing else in it); (2) the opening question of the angle; (3) one or two sentences on what "
       "PhiXtra does for that problem; (4) the offer to show it working on their own products, from their own website, "
       "in 20 minutes; (5) a short closing question such as \"Worth a look?\". 90–130 words. Include a subject line.",
    2: "Email 2, a short reply in the same thread, a few days after email 1. Open with a line like \"Just bringing "
       "this back to the top of your inbox.\" Then ONE new, specific question for their kind of business (e.g. what "
       "happens to an enquiry that arrives at 7pm on a Friday). Then offer the 20 minutes. Do not repeat the personal "
       "line or what PhiXtra does in full. 40–70 words.",
    3: "Email 3, a reply in the same thread. Use a different point from the guide than emails 1 and 2 (for example "
       "buyers writing in another language, or quotes that are never followed up twice). Do not repeat earlier "
       "questions. 40–70 words.",
    4: "Email 4, a reply in the same thread. Offer one concrete thing: before a 20-minute call we set the AI agent up "
       "on their own website, so they watch it answer questions about their own products. One short question. "
       "35–60 words.",
    5: "Email 5, the LAST email: a polite close, nothing else. Say you'll leave it here so you don't fill up their "
       "inbox, and that if faster replies and quotes become a priority they can just reply and you'll set up a short "
       "demo on their own products. NO question, NO new pitch, NO personal line. 25–45 words.",
}


def _prompt(s: dict, lead: dict, site: str, step: int, angle: int, earlier: list, how: str = "") -> str:
    company = lead.get("customer_name") or ""
    lines = [
        "You write short, personal B2B sales emails for PhiXtra. They are saved as drafts and a person checks and "
        "sends each one, so write exactly what should be sent. Reply with JSON only.",
        f"Who it's to: {lead.get('contact_person') or 'unknown name'}, {_job_title(lead.get('notes')) or 'job title unknown'}, "
        f"at {company}. Country: {lead.get('addr_country') or 'unknown'}. Town: {lead.get('addr_city') or 'unknown'}. "
        f"Business category: {lead.get('business_category') or 'unknown'}. Staff: {lead.get('employees') or 'unknown'}. "
        f"Website: {lead.get('website') or 'none'}.",
        "Their website (use this for the personal line; never state anything about them that isn't here or above):\n"
        + (site or "(not available. Base the personal line on their business category, job title and company name "
                   "only, and keep it true. Never mention their website or that anything couldn't be read.)"),
        f"Use angle {angle} from the guide for email 1, and keep follow-ups consistent with it.",
        "Sales guide and approved templates (follow their style and rules; don't copy word for word):\n" + (s.get("guide") or _GUIDE),
    ]
    if earlier:
        lines.append("Emails already sent to them in this thread (don't repeat their points or questions):\n"
                     + "\n---\n".join(earlier))
    lines.append("THIS EMAIL (follow this exactly, it matters most): " + _STEP_BRIEF.get(step, _STEP_BRIEF[2]))
    if how:
        lines.append(how)
    lines.append(
        "Return {\"subject\": \"...\", \"paragraphs\": [\"...\", ...]}.\n"
        "- subject: under 60 characters, plain, no clickbait. If it names the company, use the company name exactly as "
        "written in 'Who it's to' above, never a different name from their website. (For follow-ups it is ignored.)\n"
        "- paragraphs: the message body ONLY, 1–3 sentences each. Do NOT write the greeting (\"Hi …\"), the sign-off, "
        "the booking link or the opt-out line; they are added separately.\n"
        "Never repeat the business category label word for word (it is a rough directory label, e.g. 'Shopping'); "
        "describe what they actually do from their website instead, or keep it general.\n"
        "Rules: British English; plain everyday words; no em dashes; no exclamation marks; no emojis; at most one question "
        "mark per email; never invent numbers, prices, results, customers or facts about them; only numbers that appear "
        "in the guide or their website text.")
    return "\n\n".join(lines)


def _ask(prompt: str) -> dict:
    import ai_designer as D
    client = D._client("email", "platform")
    resp = client.chat.completions.create(model=D.TEXT_MODEL, messages=[{"role": "user", "content": prompt}],
                                          response_format={"type": "json_object"})
    return json.loads(resp.choices[0].message.content or "{}")


SIG_TOKEN = "[[signature]]"
SIG_CID = "phx-signature"


def signature(tenant_id: int, mailbox_id) -> dict | None:
    """The sender's own image signature (user, 4 Nov 2026: one per sender, on every email)."""
    if not mailbox_id:
        return None
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM outreach_signatures WHERE tenant_id=%s AND mailbox_id=%s", (tenant_id, mailbox_id))
        r = cur.fetchone()
        if r:
            r = dict(r); r["image"] = bytes(r["image"])
        return r
    finally:
        cur.close(); conn.close()


def frame(s: dict, lead: dict, step: int, sig: dict | None = None) -> tuple:
    """(greeting, tail): the fixed lines around the AI's words, from Settings.
    With an image signature, the typed name lines give way to the image:
    email 1 keeps the sign-off's first line ("Kind regards,"), follow-ups
    keep their sign-off ("Joanna"), then the image."""
    fn = first_name(lead)
    greeting = f"Hi {fn}," if fn else "Hello,"
    tail = []
    if s.get("booking_on", True) and s.get("booking_link"):
        tail.append(f"Or pick a time that suits you here: {s['booking_link']}")
    signoff = ((s.get("signoff_first") if step == 1 else s.get("signoff_followup")) or "").strip()
    if sig:
        if step == 1 and signoff:
            signoff = signoff.splitlines()[0].strip()
        tail.append((signoff + "\n" if signoff else "") + SIG_TOKEN)
    elif signoff:
        tail.append(signoff)
    if (s.get("optout") or "").strip():
        tail.append(s["optout"].strip())
    return greeting, "\n\n".join(tail)


def join_body(greeting: str, ai_text: str, tail: str) -> str:
    return "\n\n".join(x.strip() for x in (greeting, ai_text, tail) if x and x.strip())


def write_text(s: dict, lead: dict, site: str, step: int, angle: int, earlier: list, how: str = "") -> tuple:
    """(subject, ai_text): the AI's own words only. Raises ValueError if unusable."""
    from email_ai_writer import _drop_invented_numbers
    words = _ask(_prompt(s, lead, site, step, angle, earlier, how))
    allowed = " ".join([s.get("guide") or _GUIDE, site or "", json.dumps({k: str(v) for k, v in lead.items()})])
    paras = _drop_invented_numbers([str(p) for p in (words.get("paragraphs") or [])], allowed)
    subject = re.sub(r"\s+", " ", str(words.get("subject") or "")).strip()[:150]
    if _drop_invented_numbers([subject], allowed) != [subject]:
        subject = ""
    if not paras or (step == 1 and not subject):
        raise ValueError("The AI couldn't write a usable email this time.")
    paras = [p.replace("—", ",").replace("–", "-") for p in paras]
    meta = re.compile(r"(could ?n[o']t (read|access|load|find)|unable to (read|access)|as an ai|i don't have (access|information)|"
                      r"\[|\{|personal line)", re.I)
    if any(meta.search(p) for p in paras + [subject]):
        raise ValueError("The AI wrote a note to itself instead of the email.")
    return subject, "\n\n".join(p.strip() for p in paras if p.strip())


def sig_html(sig: dict | None, src: str = None) -> str:
    if not sig:
        return ""
    w = min(int(sig["width"] or 0) or 400, 600)
    h = int(round((sig["height"] or 0) * w / (sig["width"] or w))) if sig.get("height") else None
    img = (f'<img src="{_html.escape(src or "cid:" + SIG_CID)}" width="{w}"' + (f' height="{h}"' if h else "")
           + ' alt="Email signature" style="display:block;border:0;max-width:100%;height:auto">')
    if sig.get("link"):
        img = f'<a href="{_html.escape(sig["link"])}">{img}</a>'
    return img


def plain(text: str) -> str:
    """The email as plain words (no signature placeholder): for the AI and lists."""
    return re.sub(r"\n?\[\[signature\]\]", "", text or "").strip()


def text_to_html(text: str, sig: dict | None = None, sig_src: str = None) -> str:
    out = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        if block.strip():
            esc = _html.escape(block.strip()).replace("\n", "<br>")
            esc = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc)
            if _html.escape(SIG_TOKEN) in esc:
                esc = esc.replace("<br>" + _html.escape(SIG_TOKEN), "").replace(_html.escape(SIG_TOKEN), "")
                tag = f"<p style=\"margin:0 0 6px\">{esc}</p>" if esc.strip() else ""
                out.append(tag + f'<div style="margin:0 0 12px">{sig_html(sig, sig_src)}</div>')
                continue
            out.append(f"<p style=\"margin:0 0 12px\">{esc}</p>")
    return '<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#1f1f1f">' + "".join(out) + "</div>"


def _with_quote(ours_html: str, quoted_html: str) -> str:
    """Our words above Outlook's quoted earlier email (a reply draft)."""
    if not quoted_html:
        return ours_html
    m = re.search(r"<body[^>]*>", quoted_html, re.I)
    if m:
        return quoted_html[:m.end()] + ours_html + quoted_html[m.end():]
    return ours_html + quoted_html


def _plain(html_or_text: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", html_or_text or "", flags=re.I)
    t = re.sub(r"</p>", "\n\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return _html.unescape(re.sub(r"\n{3,}", "\n\n", t)).strip()


# ── writing a draft (background) ───────────────────────────────────────────

_LEAD_COLS = ("id, tenant_id, customer_name, contact_person, email, website, notes, addr_city, addr_country, "
              "business_category, employees, assigned_key, assigned_to, stage, outcome, dropped_at, is_opportunity")


def _lead(cur, tenant_id, lead_id):
    cur.execute(f"SELECT {_LEAD_COLS} FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s", (lead_id, tenant_id))
    return cur.fetchone()


def _is_open(lead) -> bool:
    return bool(lead and lead["is_opportunity"] and not lead["outcome"] and not lead["dropped_at"])


def write_next(run_id: int, how: str = "") -> dict:
    """Write the run's next email as an Outlook draft. Never raises."""
    conn, cur = _db()
    draft_id = None
    try:
        cur.execute("SELECT * FROM outreach_runs WHERE id=%s", (run_id,))
        run = cur.fetchone()
        if not run or run["status"] != "running" or run["emails_sent"] >= MAX_EMAILS:
            return {"skipped": "not running"}
        step = run["emails_sent"] + 1
        cur.execute("""INSERT INTO outreach_drafts (tenant_id, run_id, lead_id, step, status)
                       VALUES (%s,%s,%s,%s,'writing')
                       ON CONFLICT (run_id, step) WHERE status IN ('writing','waiting','approved','sent') DO NOTHING
                       RETURNING id""", (run["tenant_id"], run_id, run["lead_id"], step))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return {"skipped": "already has a draft"}
        draft_id = row["id"]
        lead = _lead(cur, run["tenant_id"], run["lead_id"])
        if not _is_open(lead):
            raise _Stop("The deal is closed (Won, Lost, Not a Fit or dropped).")
        if not (lead["email"] or "").strip():
            raise _Stop("The deal has no email address.")
        s = settings(run["tenant_id"])
        mb = _mailbox(cur, run["mailbox_id"])
        if not can_draft(mb):
            raise RuntimeError("The sending mailbox isn't connected with permission to save drafts. "
                               "Reconnect it on Settings › Integration › Microsoft 365.")
        site = run["site_text"]
        if site is None:
            site = _site_text(lead["website"])
            cur.execute("UPDATE outreach_runs SET site_text=%s WHERE id=%s", (site, run_id))
            conn.commit()
        cur.execute("""SELECT step, body_text, subject FROM outreach_drafts WHERE run_id=%s AND status='sent'
                       ORDER BY step""", (run_id,))
        sent = cur.fetchall()
        earlier = [f"Email {d['step']}: {plain(d['body_text'])}"[:1500] for d in sent]
        subject, ai_text = write_text(s, lead, site, step, run["angle"], earlier, how)
        sig = signature(run["tenant_id"], run["mailbox_id"])
        greeting, tail = frame(s, lead, step, sig)
        body = join_body(greeting, ai_text, tail)
        token = _token(cur, mb)
        import outlook_mail as M
        to_name = lead["contact_person"] or lead["customer_name"] or ""
        if step > 1 and run["last_sent_message_id"]:
            d = M.create_reply_draft(token, run["last_sent_message_id"], lead["email"].strip(), to_name, [CAT_WAITING])
            quoted = d["body_html"]
            M.update_draft(token, d["id"], html=_with_quote(text_to_html(body, sig), quoted))
            subject = (sent[-1]["subject"] if sent and sent[-1]["subject"] else subject)
            subject = subject if subject.lower().startswith("re:") else f"RE: {subject}"
        else:
            quoted = None
            d = M.create_draft(token, lead["email"].strip(), to_name, subject, text_to_html(body, sig), [CAT_WAITING])
        if sig:
            M.set_inline_image(token, d["id"], sig["image"], sig["mime"], SIG_CID, "signature." + sig["mime"].split("/")[-1])
        cur.execute("""UPDATE outreach_drafts SET status='waiting', subject=%s, body_text=%s, quoted_html=%s,
                           graph_id=%s, web_link=%s, error=NULL, greeting=%s, ai_text=%s, tail=%s, updated_at=NOW()
                       WHERE id=%s""",
                    (subject, body, quoted, d["id"], d["web_link"], greeting, ai_text, tail, draft_id))
        cur.execute("UPDATE outreach_runs SET last_error=NULL, updated_at=NOW() WHERE id=%s", (run_id,))
        conn.commit()
        return {"ok": True, "draft_id": draft_id}
    except _Stop as e:
        conn.rollback()
        if draft_id:
            cur.execute("UPDATE outreach_drafts SET status='cancelled', error=%s, updated_at=NOW() WHERE id=%s", (str(e), draft_id))
        cur.execute("""UPDATE outreach_runs SET status='stopped', stop_reason=%s, stopped_at=NOW(), updated_at=NOW()
                       WHERE id=%s""", (str(e), run_id))
        conn.commit()
        return {"stopped": str(e)}
    except Exception as e:
        conn.rollback()
        msg = str(e) if not isinstance(e, (KeyError, TypeError)) else f"{type(e).__name__}: {e}"
        print(f"⚠️ outreach draft run {run_id}:", msg)
        if draft_id:
            cur.execute("UPDATE outreach_drafts SET status='failed', error=%s, updated_at=NOW() WHERE id=%s", (msg[:500], draft_id))
        # Try again tomorrow (or now, with "Try again" on the page).
        cur.execute("""UPDATE outreach_runs SET last_error=%s, next_draft_on=CURRENT_DATE + 1, updated_at=NOW()
                       WHERE id=%s""", (msg[:500], run_id))
        conn.commit()
        return {"error": msg}
    finally:
        cur.close(); conn.close()


class _Stop(Exception):
    pass


# ── starting / stopping ────────────────────────────────────────────────────

def start(tenant_id: int, lead_ids: list, actor: dict) -> dict:
    """Start AI drafts for these deals. Returns counts and reasons skipped."""
    s = settings(tenant_id)
    conn, cur = _db()
    out = {"started": 0, "already": 0, "closed": 0, "no_email": 0}
    try:
        mb = _mailbox(cur, s["mailbox_id"]) if s["mailbox_id"] else None
        if not can_draft(mb):
            return {"error": "The sending mailbox isn't ready. On CRM › AI Email Drafts, pick who sends, and make sure "
                             "they have reconnected Outlook on Settings › Integration › Microsoft 365 to allow drafts."}
        for lid in lead_ids:
            lead = _lead(cur, tenant_id, lid)
            if not _is_open(lead):
                out["closed"] += 1
                continue
            if not (lead["email"] or "").strip():
                out["no_email"] += 1
                continue
            cur.execute("""INSERT INTO outreach_runs (tenant_id, lead_id, mailbox_id, angle, status, next_draft_on,
                               started_by_key, started_by_name)
                           VALUES (%s,%s,%s,%s,'running',CURRENT_DATE,%s,%s)
                           ON CONFLICT (tenant_id, lead_id) DO UPDATE SET
                               status='running', mailbox_id=EXCLUDED.mailbox_id, emails_sent=0, last_sent_at=NULL,
                               last_sent_message_id=NULL, next_draft_on=CURRENT_DATE, last_error=NULL, stop_reason=NULL,
                               stopped_at=NULL, started_by_key=EXCLUDED.started_by_key,
                               started_by_name=EXCLUDED.started_by_name, started_at=NOW(), updated_at=NOW()
                           WHERE outreach_runs.status IN ('stopped','finished')
                           RETURNING id""",
                        (tenant_id, lid, mb["id"], 1 if lid % 2 else 2, actor["key"], actor["label"][:200]))
            row = cur.fetchone()
            if row:
                out["started"] += 1
                # A restart: the earlier round's emails stay as history ('earlier'); its unsent ones are dropped.
                cur.execute("""UPDATE outreach_drafts SET status = CASE WHEN status='sent' THEN 'earlier' ELSE 'cancelled' END,
                                   updated_at=NOW()
                               WHERE run_id=%s AND status IN ('sent','failed','writing')""", (row["id"],))
            else:
                out["already"] += 1
        conn.commit()
    finally:
        cur.close(); conn.close()
    kick()
    return out


def _cancel_open_drafts(cur, run, reason: str) -> None:
    """Unsent drafts of this run: marked cancelled and removed from Outlook."""
    cur.execute("""SELECT id, graph_id FROM outreach_drafts WHERE run_id=%s AND status IN ('waiting','approved')""", (run["id"],))
    rows = cur.fetchall()
    if not rows:
        return
    token = None
    try:
        mb = _mailbox(cur, run["mailbox_id"])
        token = _token(cur, mb) if mb and mb["status"] == "active" else None
    except Exception as e:
        print("⚠️ outreach cancel token:", e)
    import outlook_mail as M
    for r in rows:
        note = reason
        if token and r["graph_id"]:
            try:
                M.delete_draft(token, r["graph_id"])
            except Exception as e:
                note = f"{reason} (couldn't remove it from Outlook: {e}. Delete it there by hand.)"
        cur.execute("UPDATE outreach_drafts SET status='cancelled', error=%s, updated_at=NOW() WHERE id=%s", (note[:500], r["id"]))


def stop(tenant_id: int, lead_id: int, reason: str, status: str = "stopped") -> bool:
    conn, cur = _db()
    try:
        cur.execute("""SELECT * FROM outreach_runs WHERE tenant_id=%s AND lead_id=%s AND status IN ('running','finished')
                       FOR UPDATE""", (tenant_id, lead_id))
        run = cur.fetchone()
        if not run:
            return False
        _cancel_open_drafts(cur, run, reason)
        cur.execute("""UPDATE outreach_runs SET status=%s, stop_reason=%s, stopped_at=NOW(), updated_at=NOW()
                       WHERE id=%s""", (status, reason[:300], run["id"]))
        conn.commit()
        return True
    finally:
        cur.close(); conn.close()


# ── hooks from the Outlook checker (mailbox_routes) ────────────────────────

def outlook_sent_match(cur, tenant_id: int, sender: str, received, window) -> dict | None:
    """A reply to an email the team sent from Outlook to a deal (pilot only):
    shaped like the campaign recipient row _record_reply expects."""
    if not pilot(tenant_id):
        return None
    cur.execute("""SELECT s.lead_id, s.subject, l.wa_contact_id FROM mailbox_sent_emails s
                   JOIN merchant_pipeline_leads l ON l.id = s.lead_id
                   WHERE s.tenant_id=%s AND lower(s.to_email)=%s AND s.sent_at <= %s AND s.sent_at >= %s
                   ORDER BY s.sent_at DESC LIMIT 1""", (tenant_id, sender, received, received - window))
    r = cur.fetchone()
    if not r:
        return None
    return {"campaign_id": None, "id": None, "lead_id": r["lead_id"], "contact_id": r["wa_contact_id"],
            "campaign_name": r["subject"] or "your email"}


def on_sent(mb: dict, lead_id: int, message_id: str, sent_at: datetime) -> None:
    """The sender sent an email to this deal from Outlook: it is the next
    email of the run (normally the approved draft). Never raises."""
    try:
        if not pilot(mb["tenant_id"]):
            return
        conn, cur = _db()
        try:
            cur.execute("""SELECT * FROM outreach_runs WHERE tenant_id=%s AND lead_id=%s AND mailbox_id=%s
                           AND status='running' FOR UPDATE""", (mb["tenant_id"], lead_id, mb["id"]))
            run = cur.fetchone()
            if not run or run["last_sent_message_id"] == message_id or run["emails_sent"] >= MAX_EMAILS:
                return
            cur.execute("SELECT 1 FROM outreach_drafts WHERE run_id=%s AND sent_message_id=%s", (run["id"], message_id))
            if cur.fetchone():
                return
            step = run["emails_sent"] + 1
            cur.execute("""SELECT id, status FROM outreach_drafts WHERE run_id=%s AND status IN ('waiting','approved','writing')
                           ORDER BY step LIMIT 1""", (run["id"],))
            d = cur.fetchone()
            if d:
                cur.execute("""UPDATE outreach_drafts SET status='sent', sent_message_id=%s, sent_at=%s,
                                   sent_unchecked=%s, updated_at=NOW() WHERE id=%s""",
                            (message_id, sent_at, d["status"] != "approved", d["id"]))
            else:
                cur.execute("SELECT subject, snippet FROM mailbox_sent_emails WHERE mailbox_id=%s AND message_id=%s AND lead_id=%s",
                            (mb["id"], message_id, lead_id))
                m = cur.fetchone() or {}
                cur.execute("""INSERT INTO outreach_drafts (tenant_id, run_id, lead_id, step, status, subject, body_text,
                                   sent_message_id, sent_at, by_hand)
                               VALUES (%s,%s,%s,%s,'sent',%s,%s,%s,%s,TRUE)
                               ON CONFLICT (run_id, step) WHERE status IN ('writing','waiting','approved','sent') DO NOTHING""",
                            (mb["tenant_id"], run["id"], lead_id, step, m.get("subject"), m.get("snippet"), message_id, sent_at))
            s = settings(mb["tenant_id"])
            from team_performance import tz_for
            day = sent_at.astimezone(tz_for(mb["tenant_id"])).date()
            if step < MAX_EMAILS:
                nxt = day + timedelta(days=s["gaps"][step - 1])
                note = f"AI email {step + 1} of {MAX_EMAILS} is written as an Outlook draft for checking on this day."
            else:
                nxt = day + timedelta(days=s["final_wait"])
                note = (f"All {MAX_EMAILS} AI emails sent. If there's still no reply on this day, decide: "
                        "LinkedIn, close as Unresponsive, or keep.")
            cur.execute("""UPDATE outreach_runs SET emails_sent=%s, last_sent_at=%s, last_sent_message_id=%s,
                               next_draft_on=%s, last_error=NULL, updated_at=NOW() WHERE id=%s""",
                        (step, sent_at, message_id, nxt, run["id"]))
            conn.commit()
        finally:
            cur.close(); conn.close()
        import lead_followups as _fu
        _fu.set_next(mb["tenant_id"], lead_id, nxt, {"key": mb["owner_key"], "label": mb["owner_label"] or mb["email"]},
                     source="email_sent", note=note, channel="email")
    except Exception as e:
        print(f"⚠️ outreach on_sent lead {lead_id}:", e)


def _alert(tenant_id: int, lead_id: int, ref: str, subject: str, lines: list, skip_emails=()) -> None:
    """Email each person on the alert list (once per ref)."""
    import auto_followups as _af
    s = settings(tenant_id)
    conn, cur = _db()
    try:
        for key in s["alert_keys"] or []:
            to = _af._recipient_emails(cur, tenant_id, key, lead_id)
            if to and to[0].lower() in skip_emails:
                continue
            _af.notify(cur, tenant_id, key, f"{ref}:{key}", subject, lines, lead_id=lead_id,
                       link=f"{PORTAL}/sales-pipeline/{lead_id}")
    finally:
        cur.close(); conn.close()


def on_reply(tenant_id: int, lead_id: int, reply_id: int, text: str) -> None:
    """They replied: drafts pause (unsent ones are removed from Outlook) and
    Philip, David and Joanna are told. The AI writes nothing. Never raises."""
    try:
        if not pilot(tenant_id) or not lead_id:
            return
        if not stop(tenant_id, lead_id, "They replied", status="replied"):
            return
        import auto_followups as _af
        conn, cur = _db()
        try:
            lead = _lead(cur, tenant_id, lead_id)
            owner = [e.lower() for e in _af._recipient_emails(cur, tenant_id, "owner", lead_id)]
        finally:
            cur.close(); conn.close()
        who = (lead or {}).get("contact_person") or (lead or {}).get("customer_name") or "A buyer"
        biz = (lead or {}).get("customer_name") or ""
        short = re.sub(r"\s+", " ", text or "").strip()
        short = short[:300] + ("…" if len(short) > 300 else "")
        _alert(tenant_id, lead_id, f"outreach_reply:{reply_id}",
               f"{biz or who} replied to Joanna's email",
               [f"{who}{' (' + biz + ')' if biz and biz != who else ''} replied:", f"“{short}”",
                "The AI follow-up drafts for this deal have stopped, and any unsent draft was removed from Outlook. "
                "The AI has not replied. Please answer them yourselves, within the hour if you can."],
               skip_emails=owner)
    except Exception as e:
        print(f"⚠️ outreach on_reply lead {lead_id}:", e)


_BOUNCE = re.compile(r"(undeliverable|delivery (status notification|has failed|failed)|mail delivery (failed|subsystem)|"
                     r"returned mail|couldn't be delivered|could not be delivered|address not found)", re.I)


def on_other_mail(mb: dict, msg: dict, sender: str) -> bool:
    """A bounce for one of our drafts' emails stops that deal. True if handled."""
    try:
        if not pilot(mb["tenant_id"]):
            return False
        subject = msg.get("subject") or ""
        if not (_BOUNCE.search(subject) or sender.startswith(("mailer-daemon@", "postmaster@"))):
            return False
        preview = (msg.get("bodyPreview") or "").lower()
        conn, cur = _db()
        try:
            cur.execute("""SELECT r.lead_id, lower(l.email) AS email, l.customer_name FROM outreach_runs r
                           JOIN merchant_pipeline_leads l ON l.id = r.lead_id
                           WHERE r.tenant_id=%s AND r.mailbox_id=%s AND r.status='running' AND r.emails_sent > 0""",
                        (mb["tenant_id"], mb["id"]))
            hits = [r for r in cur.fetchall() if r["email"] and (r["email"] in preview or r["email"] in subject.lower())]
        finally:
            cur.close(); conn.close()
        for h in hits:
            if stop(mb["tenant_id"], h["lead_id"], "The email bounced (address not reachable)"):
                _alert(mb["tenant_id"], h["lead_id"], f"outreach_bounce:{msg.get('id', '')[:60]}",
                       f"Email to {h['customer_name']} bounced",
                       [f"The email to {h['email']} ({h['customer_name']}) bounced, so its AI drafts have stopped.",
                        "Check the address on the deal, or close it as Not a Fit."])
        return bool(hits)
    except Exception as e:
        print("⚠️ outreach on_other_mail:", e)
        return False


# ── the background loop ────────────────────────────────────────────────────

def tick() -> None:
    tenants = [int(x) for x in os.getenv("OUTREACH_DRAFT_TENANTS", "19").split(",") if x.strip().isdigit()]
    for tid in tenants:
        conn, cur = _db()
        try:
            from lead_followups import _today
            today = _today(tid)
            # Closed deals stop.
            cur.execute("""SELECT r.lead_id FROM outreach_runs r JOIN merchant_pipeline_leads l ON l.id = r.lead_id
                           WHERE r.tenant_id=%s AND r.status IN ('running','finished')
                             AND (l.outcome IS NOT NULL OR l.dropped_at IS NOT NULL OR NOT l.is_opportunity)""", (tid,))
            closed = [r["lead_id"] for r in cur.fetchall()]
            # Five emails sent and the final wait is over: the owner decides.
            cur.execute("""SELECT id, lead_id FROM outreach_runs WHERE tenant_id=%s AND status='running'
                           AND emails_sent >= %s AND next_draft_on <= %s""", (tid, MAX_EMAILS, today))
            done = cur.fetchall()
            # Due for a draft, with no draft open yet. Oldest first, a few at a time.
            cur.execute("""SELECT r.id FROM outreach_runs r WHERE r.tenant_id=%s AND r.status='running'
                           AND r.emails_sent < %s AND r.next_draft_on <= %s
                           AND NOT EXISTS (SELECT 1 FROM outreach_drafts d WHERE d.run_id = r.id
                                           AND d.status IN ('writing','waiting','approved'))
                           ORDER BY r.next_draft_on, r.id LIMIT 15""", (tid, MAX_EMAILS, today))
            due = [r["id"] for r in cur.fetchall()]
            # A 'writing' row left behind by a restart mid-write (over 15 min old) is cleared.
            cur.execute("""UPDATE outreach_drafts SET status='failed', error='Interrupted while writing; written again.'
                           WHERE tenant_id=%s AND status='writing' AND created_at < NOW() - INTERVAL '15 minutes'""", (tid,))
            conn.commit()
        finally:
            cur.close(); conn.close()
        for lid in closed:
            stop(tid, lid, "The deal was closed (Won, Lost, Not a Fit or dropped)")
        for r in done:
            _finish(tid, r["id"], r["lead_id"])
        for rid in due:
            write_next(rid)
        try:
            morning_email(tid)
        except Exception as e:
            print("⚠️ outreach morning email:", e)


def _finish(tenant_id: int, run_id: int, lead_id: int) -> None:
    conn, cur = _db()
    try:
        cur.execute("""UPDATE outreach_runs SET status='finished', stop_reason='5 emails, no reply', stopped_at=NOW(),
                           updated_at=NOW() WHERE id=%s AND status='running' RETURNING mailbox_id""", (run_id,))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return
        lead = _lead(cur, tenant_id, lead_id)
    finally:
        cur.close(); conn.close()
    import lead_followups as _fu
    note = f"{MAX_EMAILS} AI emails, no reply. Decide: LinkedIn, close as Unresponsive, or keep."
    _fu.set_next(tenant_id, lead_id, _fu._today(tenant_id), {"key": "system:outreach", "label": "AI email drafts"},
                 source="outreach_done", note=note, channel="email")
    conn, cur = _db()
    try:
        import auto_followups as _af
        _af.notify(cur, tenant_id, "owner", f"outreach_done:{run_id}",
                   f"No reply from {lead['customer_name']} after {MAX_EMAILS} emails",
                   [f"{lead['contact_person'] or lead['customer_name']} hasn't replied to any of the {MAX_EMAILS} emails.",
                    "The AI drafts have stopped. It's on your Follow-ups for today: decide whether to try LinkedIn, "
                    "close it as Not a Fit (Unresponsive), or keep it."],
                   lead_id=lead_id, link=f"{PORTAL}/sales-pipeline/{lead_id}")
    finally:
        cur.close(); conn.close()


_kick = threading.Event()


def kick() -> None:
    _kick.set()


def _claim_tick() -> bool:
    """Only one portal worker runs a tick at a time (advisory lock)."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT pg_try_advisory_lock(860411)")
        if not c.fetchone()[0]:
            return False
        try:
            tick()
        finally:
            c.execute("SELECT pg_advisory_unlock(860411)")
        return True
    finally:
        conn.close()


def _loop():
    time.sleep(40)
    while True:
        try:
            _claim_tick()
        except Exception as e:
            print("⚠️ outreach drafts loop:", e)
        _kick.wait(120)
        _kick.clear()


def start_loop() -> None:
    ensure_tables()
    if not getattr(threading, "_phixtra_outreach_started", False):
        threading._phixtra_outreach_started = True  # type: ignore[attr-defined]
        threading.Thread(target=_loop, daemon=True).start()


# ── pages ──────────────────────────────────────────────────────────────────

def _ctx():
    """(customer, actor, can_check) for a logged-in person of a pilot business, else None."""
    from portal_routes import _current_actor, _customer_id, _get_customer, _segments_see_all
    cid = _customer_id()
    if not cid:
        return None
    customer = _get_customer(cid)
    if not customer or not pilot(customer["tenant_id"]):
        return None
    return customer, _current_actor(customer), _segments_see_all()


def _inject():
    from portal_routes import _inject_granted_features
    return _inject_granted_features()


outreach_bp.context_processor(_inject)


@outreach_bp.app_context_processor
def _helpers():
    def outreach_on():
        try:
            cid = session.get("impersonate_customer_id") or session.get("customer_id")
            if not cid:
                return False
            from portal_routes import _get_customer
            c = _get_customer(cid)
            return bool(c and pilot(c["tenant_id"]))
        except Exception:
            return False

    def outreach_box(lead_id):
        try:
            x = _ctx()
            if not x:
                return None
            return deal_box(int(x[0]["tenant_id"]), int(lead_id))
        except Exception as e:
            print("⚠️ outreach_box:", e)
            return None
    def outreach_can_check():
        try:
            from portal_routes import _segments_see_all
            return outreach_on() and _segments_see_all()
        except Exception:
            return False
    return {"outreach_on": outreach_on, "outreach_box": outreach_box, "outreach_can_check": outreach_can_check}


def _today_for(tid):
    from lead_followups import _today
    return _today(tid)


def _site_bits(site: str) -> dict:
    """The parts of the stored website text worth showing to the checker."""
    if not site:
        return {}
    d = re.search(r"Description:\s*(.*)", site)
    t = re.search(r"Page text:\s*(.*)", site, re.S)
    return {"description": (d.group(1).strip() if d else "")[:300],
            "text": (re.sub(r"\s+", " ", t.group(1)).strip() if t else "")[:320]}


def _future_dates(s: dict, run: dict, open_step) -> dict:
    """Step → expected draft day: the run's next date, then the gaps. When
    the next email is already drafted, later ones count from today (a guide;
    the real date follows the send)."""
    out = {}
    if not run or run["status"] != "running":
        return out
    first = run["emails_sent"] + 1
    if open_step == first:
        d, start = date.today(), first + 1
    elif run["next_draft_on"]:
        d, start = run["next_draft_on"], first + 1
        out[first] = d
    else:
        return out
    for n in range(start, MAX_EMAILS + 1):
        d = d + timedelta(days=s["gaps"][n - 2])
        out[n] = d
    return out


def deal_box(tenant_id: int, lead_id: int) -> dict | None:
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM outreach_runs WHERE tenant_id=%s AND lead_id=%s", (tenant_id, lead_id))
        run = cur.fetchone()
        if not run:
            return {"run": None}
        cur.execute("""SELECT id, step, status, subject, sent_at, sent_unchecked, by_hand, checked_by_name, web_link, error,
                              ai_text, body_text, created_at
                       FROM outreach_drafts WHERE run_id=%s AND status NOT IN ('cancelled','earlier')
                       ORDER BY step, id""", (run["id"],))
        drafts = cur.fetchall()
    finally:
        cur.close(); conn.close()
    s = settings(tenant_id)
    by_step = {}
    for d in drafts:
        if d["status"] != "failed" or d["step"] not in by_step:
            by_step[d["step"]] = d
    current = next((d for d in drafts if d["status"] in ("waiting", "approved")), None)
    future = _future_dates(s, run, current["step"] if current else None)
    steps = []
    for n in range(1, MAX_EMAILS + 1):
        d = by_step.get(n)
        state = d["status"] if d and d["status"] in ("sent", "waiting", "approved", "writing", "failed") else "future"
        steps.append({"n": n, "d": d, "state": state, "when": future.get(n)})
    preview = None
    if current:
        txt = (current["ai_text"] or current["body_text"] or "").strip()
        preview = (txt[:180] + "…") if len(txt) > 180 else txt
    return {"run": run, "steps": steps, "max": MAX_EMAILS, "current": current, "preview": preview}


# ── what the page lists ────────────────────────────────────────────────────

def _replies_to_answer(cur, tid: int, limit: int = 50) -> list:
    """Deals whose buyer replied in the last 30 days and nobody has emailed since."""
    cur.execute("""SELECT r.lead_id, l.customer_name, l.contact_person, l.email, er.snippet, er.received_at, er.web_link
                   FROM outreach_runs r JOIN merchant_pipeline_leads l ON l.id = r.lead_id
                   JOIN LATERAL (SELECT snippet, received_at, web_link FROM email_replies e
                                 WHERE e.tenant_id = r.tenant_id AND e.lead_id = r.lead_id
                                 ORDER BY received_at DESC LIMIT 1) er ON TRUE
                   WHERE r.tenant_id=%s AND r.status='replied' AND er.received_at > NOW() - INTERVAL '30 days'
                     AND NOT EXISTS (SELECT 1 FROM mailbox_sent_emails m WHERE m.tenant_id=r.tenant_id
                                     AND m.lead_id=r.lead_id AND m.sent_at > er.received_at)
                   ORDER BY er.received_at DESC LIMIT %s""", (tid, limit))
    return cur.fetchall()


def _counts(cur, tid: int) -> dict:
    cur.execute("""SELECT
          count(*) FILTER (WHERE d.status='waiting') AS waiting,
          count(*) FILTER (WHERE d.status='approved') AS approved,
          count(*) FILTER (WHERE d.status='sent' AND d.sent_at > NOW() - INTERVAL '7 days') AS sent_week,
          count(*) FILTER (WHERE d.status='failed' AND r.status='running' AND NOT EXISTS (
                SELECT 1 FROM outreach_drafts x WHERE x.run_id=d.run_id AND x.step=d.step AND x.id > d.id)) AS problems
        FROM outreach_drafts d JOIN outreach_runs r ON r.id = d.run_id WHERE d.tenant_id=%s""", (tid,))
    c = dict(cur.fetchone())
    c["replied"] = len(_replies_to_answer(cur, tid))
    return c


_QUEUE_SQL = """SELECT d.*, r.mailbox_id, r.angle, r.emails_sent, r.last_error, r.site_text, r.next_draft_on, r.status AS run_status,
                       l.customer_name, l.contact_person, l.email, l.website, l.notes, l.business_category, l.employees,
                       l.addr_country, l.addr_city, l.stage
                FROM outreach_drafts d JOIN outreach_runs r ON r.id = d.run_id
                JOIN merchant_pipeline_leads l ON l.id = d.lead_id
                WHERE d.tenant_id=%s AND {cond} ORDER BY {order} LIMIT 200"""
_QUEUES = {
    "waiting": ("d.status='waiting'", "d.created_at, d.id"),
    "approved": ("d.status='approved'", "d.checked_at, d.id"),
    "problems": ("""d.status='failed' AND r.status='running' AND NOT EXISTS (SELECT 1 FROM outreach_drafts x
                    WHERE x.run_id=d.run_id AND x.step=d.step AND x.id > d.id)""", "d.created_at DESC"),
}


def _pane(cur, tid: int, d: dict) -> dict:
    """Everything the right-hand pane shows for one draft."""
    d = dict(d)
    d["job"] = _job_title(d.get("notes"))
    d["site"] = _site_bits(d.get("site_text") or "")
    d["first"] = first_name(d)
    paras = [p for p in re.split(r"\n\s*\n", (d.get("ai_text") or "").strip()) if p.strip()]
    d["paras"] = paras
    d["tail_lines"] = [p for p in re.split(r"\n\s*\n", (d.get("tail") or "").strip()) if p.strip()]
    d["has_sig"] = SIG_TOKEN in (d.get("tail") or "")
    d["tail_blocks"] = [{"text": p.replace("\n" + SIG_TOKEN, "").replace(SIG_TOKEN, "").strip(), "sig": SIG_TOKEN in p}
                        for p in d["tail_lines"]]
    if d["has_sig"]:
        sg = signature(tid, d.get("mailbox_id"))
        d["sig_v"] = int(sg["updated_at"].timestamp()) if sg else 0
    cur.execute("""SELECT step, subject, body_text, sent_at FROM outreach_drafts WHERE run_id=%s AND status='sent'
                   AND step < %s ORDER BY step""", (d["run_id"], d["step"]))
    d["earlier"] = [dict(e, body_text=plain(e["body_text"])) for e in cur.fetchall()]
    return d


def _queue(cur, tid: int, tab: str) -> list:
    cond, order = _QUEUES[tab]
    cur.execute(_QUEUE_SQL.format(cond=cond, order=order), (tid,))
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["job"] = _job_title(r.get("notes"))
    return rows


def _send_view(cur, tid: int, s: dict) -> dict:
    today = _today_for(tid)
    from team_performance import tz_for
    tzname = tz_for(tid).key
    cur.execute("""SELECT d.id, d.step, d.subject, d.web_link, d.checked_by_name, d.checked_at, l.customer_name,
                          l.contact_person, l.email, d.lead_id
                   FROM outreach_drafts d JOIN merchant_pipeline_leads l ON l.id = d.lead_id
                   WHERE d.tenant_id=%s AND d.status='approved' ORDER BY d.checked_at, d.id""", (tid,))
    ready = cur.fetchall()
    cur.execute("""SELECT count(*) AS n FROM outreach_drafts WHERE tenant_id=%s AND status='sent'
                   AND (sent_at AT TIME ZONE %s)::date = %s""", (tid, tzname, today))
    sent_today = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM outreach_drafts WHERE tenant_id=%s AND status='waiting'", (tid,))
    waiting = cur.fetchone()["n"]
    return {"ready": ready, "replies": _replies_to_answer(cur, tid), "sent_today": sent_today, "waiting": waiting}


TAB_NAMES = {"waiting": "Waiting for check", "approved": "Approved, not sent", "send": "Ready to send",
             "sent": "Sent this week", "replied": "Replied, answer now", "problems": "Problems", "deals": "All deals"}


@outreach_bp.route("/crm/ai-drafts", methods=["GET"])
@any_team_member
def page():
    from portal_routes import _require_login
    r = _require_login()
    if r:
        return r
    x = _ctx()
    if not x:
        return redirect(url_for("portal.sales_pipeline"))
    customer, actor, can_check = x
    tid = int(customer["tenant_id"])
    tab = request.args.get("tab") or ("waiting" if can_check else "send")
    if tab not in TAB_NAMES:
        tab = "waiting"
    if not can_check and tab in ("waiting", "approved", "problems"):
        tab = "send"
    s = settings(tid)
    conn, cur = _db()
    try:
        counts = _counts(cur, tid)
        ctx = {"queue": [], "sel": None, "send": None, "sent": [], "deals": [], "replies": []}
        if tab in _QUEUES:
            ctx["queue"] = _queue(cur, tid, tab)
            want = request.args.get("d", "")
            sel = next((q for q in ctx["queue"] if str(q["id"]) == want), None) or (ctx["queue"][0] if ctx["queue"] else None)
            if sel:
                ctx["sel"] = _pane(cur, tid, sel)
            if request.args.get("partial") and ctx["sel"]:
                return render_template("portal/_outreach_draft_pane.html", d=ctx["sel"], tab=tab, can_check=can_check,
                                       max=MAX_EMAILS, cat_ok=CAT_OK)
            if request.args.get("partial"):
                return ""
        elif tab == "send":
            ctx["send"] = _send_view(cur, tid, s)
        elif tab == "replied":
            ctx["replies"] = _replies_to_answer(cur, tid)
        elif tab == "sent":
            cur.execute("""SELECT d.*, l.customer_name, l.contact_person FROM outreach_drafts d
                           JOIN merchant_pipeline_leads l ON l.id = d.lead_id
                           WHERE d.tenant_id=%s AND d.status='sent' AND d.sent_at > NOW() - INTERVAL '7 days'
                           ORDER BY d.sent_at DESC LIMIT 300""", (tid,))
            ctx["sent"] = cur.fetchall()
        elif tab == "deals":
            cur.execute("""SELECT r.*, l.customer_name, l.contact_person, l.email FROM outreach_runs r
                           JOIN merchant_pipeline_leads l ON l.id = r.lead_id
                           WHERE r.tenant_id=%s ORDER BY (r.status='running') DESC, r.updated_at DESC LIMIT 300""", (tid,))
            ctx["deals"] = cur.fetchall()
        cur.execute("""SELECT m.id, m.email, m.owner_label, m.status, m.scopes FROM mailbox_connections m
                       WHERE m.tenant_id=%s AND m.provider='microsoft' AND m.status <> 'disconnected' ORDER BY m.owner_label""", (tid,))
        mailboxes = cur.fetchall()
        cur.execute("""SELECT 'team:' || id AS k, name, email FROM team_members WHERE tenant_id=%s AND is_active
                       AND lower(email) NOT LIKE '%%gmail.com' ORDER BY name""", (tid,))
        people = cur.fetchall()
    finally:
        cur.close(); conn.close()
    sender = next((m for m in mailboxes if m["id"] == s["mailbox_id"]), None)
    sg = signature(tid, s["mailbox_id"]) if s["mailbox_id"] else None
    sig = ({"w": sg["width"], "h": sg["height"], "link": sg["link"], "v": int(sg["updated_at"].timestamp())} if sg else None)
    return render_template("portal/outreach_drafts.html", sig=sig, customer=customer, tab=tab, tab_names=TAB_NAMES,
                           counts=counts, s=s, days=schedule_days(s), max=MAX_EMAILS, mailboxes=mailboxes,
                           sender=sender, sender_ready=can_draft(sender), people=people, can_check=can_check,
                           cat_ok=CAT_OK, cat_waiting=CAT_WAITING, **ctx)


def _guard_json(check=True):
    from portal_routes import _require_login
    if _require_login():
        return None, (jsonify({"error": "Please log in again."}), 401)
    x = _ctx()
    if not x:
        return None, (jsonify({"error": "Not available for this business."}), 403)
    if check and not x[2]:
        return None, (jsonify({"error": "Only Philip, David or another manager can do this."}), 403)
    return x, None


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "fetch" or request.form.get("ajax") == "1"


def preview(tenant_id: int, lead_ids: list) -> dict:
    """What "Write AI drafts" would do, before it does it (the start window)."""
    s = settings(tenant_id)
    conn, cur = _db()
    try:
        mb = _mailbox(cur, s["mailbox_id"]) if s["mailbox_id"] else None
        cur.execute(f"""SELECT l.id, l.customer_name, l.email, l.is_opportunity, l.outcome, l.dropped_at, r.status AS run_status
                        FROM merchant_pipeline_leads l
                        LEFT JOIN outreach_runs r ON r.tenant_id = l.tenant_id AND r.lead_id = l.id
                        WHERE l.tenant_id=%s AND l.id = ANY(%s)""", (tenant_id, lead_ids))
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    out = {"start": 0, "no_email": [], "closed": [], "running": [], "replied": []}
    for r in rows:
        if not _is_open(r):
            out["closed"].append(r["customer_name"])
        elif not (r["email"] or "").strip():
            out["no_email"].append(r["customer_name"])
        elif r["run_status"] == "running":
            out["running"].append(r["customer_name"])
        elif r["run_status"] == "replied":
            out["replied"].append(r["customer_name"])
        else:
            out["start"] += 1
    out["sender"] = ({"name": mb["owner_label"], "email": mb["email"], "ready": can_draft(mb)} if mb else None)
    out["days"] = schedule_days(s)
    return out


@outreach_bp.route("/crm/ai-drafts/preview", methods=["POST"])
@any_team_member
def preview_route():
    x, err = _guard_json()
    if err:
        return err
    ids = [int(v) for v in request.form.getlist("lead_ids") if v.isdigit()][:500]
    return jsonify(preview(int(x[0]["tenant_id"]), ids))


@outreach_bp.route("/crm/ai-drafts/start", methods=["POST"])
@any_team_member
def start_route():
    x, err = _guard_json()
    if err:
        return err
    customer, actor, _ = x
    ids = [int(v) for v in request.form.getlist("lead_ids") if v.isdigit()][:500]
    if not ids:
        return jsonify({"error": "Tick at least one deal."})
    res = start(int(customer["tenant_id"]), ids, actor)
    return jsonify(res)


def _draft_for_action(cur, tid, draft_id):
    cur.execute("""SELECT d.*, r.mailbox_id, r.status AS run_status FROM outreach_drafts d
                   JOIN outreach_runs r ON r.id = d.run_id WHERE d.id=%s AND d.tenant_id=%s FOR UPDATE OF d""",
                (draft_id, tid))
    return cur.fetchone()


def _next_in_queue(tid: int, tab: str, after_id: int):
    conn, cur = _db()
    try:
        q = _queue(cur, tid, tab if tab in _QUEUES else "waiting")
    finally:
        cur.close(); conn.close()
    ids = [r["id"] for r in q if r["id"] != after_id]
    return ids[0] if ids else None


def _done(ok: bool, msg: str, tab: str, draft_id: int, kind: str = "success", nxt=None):
    """JSON for the page's buttons, or flash + redirect without JavaScript."""
    if _wants_json():
        return jsonify({"ok": ok, "message": msg, "next": nxt})
    flash(msg, kind if ok else "danger")
    return redirect(url_for("outreach.page", tab=tab, d=nxt or draft_id))


@outreach_bp.route("/crm/ai-drafts/<int:draft_id>/approve", methods=["POST"])
@any_team_member
def approve(draft_id: int):
    """Save any edits into the Outlook draft and mark it checked."""
    x, err = _guard_json()
    if err:
        return err if _wants_json() else redirect(url_for("outreach.page"))
    customer, actor, _ = x
    tid = int(customer["tenant_id"])
    tab = request.form.get("tab") or "waiting"
    import outlook_mail as M
    conn, cur = _db()
    try:
        d = _draft_for_action(cur, tid, draft_id)
        if not d or d["status"] not in ("waiting", "approved"):
            conn.rollback()
            return _done(False, "That draft has already been sent or stopped.", tab, draft_id,
                         nxt=_next_in_queue(tid, tab, draft_id))
        subject = (request.form.get("subject") or d["subject"] or "").strip()[:150]
        if "ai_text" in request.form and d["ai_text"] is not None:
            ai_text = (request.form.get("ai_text") or "").replace("\r\n", "\n").strip()
            if not ai_text:
                conn.rollback()
                return _done(False, "The email can't be empty.", tab, draft_id)
            body = join_body(d["greeting"] or "", ai_text, d["tail"] or "")
        else:
            ai_text = d["ai_text"]
            body = (request.form.get("body") or d["body_text"] or "").replace("\r\n", "\n").strip()
        mb = _mailbox(cur, d["mailbox_id"])
        token = _token(cur, mb)
        changed_body = body != (d["body_text"] or "").strip()
        changed_subject = d["step"] == 1 and subject and subject != (d["subject"] or "")
        try:
            M.update_draft(token, d["graph_id"],
                           html=_with_quote(text_to_html(body, signature(tid, d["mailbox_id"])), d["quoted_html"]) if changed_body else None,
                           subject=subject if changed_subject else None, categories=[CAT_OK])
        except M.GraphError as e:
            if e.code == "gone":
                cur.execute("UPDATE outreach_drafts SET status='cancelled', error=%s, updated_at=NOW() WHERE id=%s",
                            ("Deleted in Outlook before it was checked.", draft_id))
                cur.execute("""UPDATE outreach_runs SET status='stopped', stop_reason='Draft deleted in Outlook',
                                   stopped_at=NOW(), updated_at=NOW() WHERE id=%s AND status='running'""", (d["run_id"],))
                conn.commit()
                return _done(False, "That draft isn't in Outlook any more (deleted or already sent), so this deal's "
                                    "drafts have stopped.", tab, draft_id, nxt=_next_in_queue(tid, tab, draft_id))
            raise
        cur.execute("""UPDATE outreach_drafts SET status='approved', body_text=%s, ai_text=%s, subject=%s, checked_by_name=%s,
                           checked_at=NOW(), updated_at=NOW() WHERE id=%s""",
                    (body, ai_text, subject if changed_subject else d["subject"], actor["label"][:200], draft_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return _done(False, f"Couldn't update the Outlook draft: {e}", tab, draft_id)
    finally:
        cur.close(); conn.close()
    was = d["status"]
    return _done(True, "Saved." if was == "approved" else "Approved. It's marked as checked in Joanna's Outlook.",
                 tab, draft_id, nxt=_next_in_queue(tid, tab, draft_id) if was == "waiting" else draft_id)


REWRITES = {
    "again": "Rewrite it with a different personal line and wording.",
    "shorter": "Make it noticeably shorter. Keep the question and the offer.",
    "warmer": "Make it warmer and more conversational.",
    "formal": "Make it a little more formal.",
}


@outreach_bp.route("/crm/ai-drafts/<int:draft_id>/rewrite", methods=["POST"])
@any_team_member
def rewrite(draft_id: int):
    x, err = _guard_json()
    if err:
        return err if _wants_json() else redirect(url_for("outreach.page"))
    customer, actor, _ = x
    tid = int(customer["tenant_id"])
    tab = request.form.get("tab") or "waiting"
    how = REWRITES.get(request.form.get("how"), REWRITES["again"])
    import outlook_mail as M
    conn, cur = _db()
    try:
        d = _draft_for_action(cur, tid, draft_id)
        if not d or d["status"] not in ("waiting", "approved"):
            conn.rollback()
            return _done(False, "That draft has already been sent or stopped.", tab, draft_id)
        cur.execute("SELECT * FROM outreach_runs WHERE id=%s", (d["run_id"],))
        run = cur.fetchone()
        lead = _lead(cur, tid, d["lead_id"])
        s = settings(tid)
        cur.execute("SELECT step, body_text FROM outreach_drafts WHERE run_id=%s AND status='sent' ORDER BY step", (run["id"],))
        earlier = [f"Email {e['step']}: {plain(e['body_text'])}"[:1500] for e in cur.fetchall()]
        current = (request.form.get("ai_text") or d["ai_text"] or d["body_text"] or "")[:3000]
        subject, ai_text = write_text(s, lead, run["site_text"] or "", d["step"], run["angle"], earlier,
                                      f"Current draft (the team may have edited it):\n{current}\n\nRewrite request: {how}")
        greeting = d["greeting"] if d["greeting"] is not None else frame(s, lead, d["step"])[0]
        tail = d["tail"] if d["tail"] is not None else frame(s, lead, d["step"])[1]
        body = join_body(greeting, ai_text, tail)
        mb = _mailbox(cur, d["mailbox_id"])
        token = _token(cur, mb)
        M.update_draft(token, d["graph_id"], html=_with_quote(text_to_html(body, signature(tid, d["mailbox_id"])), d["quoted_html"]),
                       subject=subject if d["step"] == 1 else None, categories=[CAT_WAITING])
        cur.execute("""UPDATE outreach_drafts SET status='waiting', body_text=%s, ai_text=%s, greeting=%s, tail=%s, subject=%s,
                           checked_by_name=NULL, checked_at=NULL, updated_at=NOW() WHERE id=%s""",
                    (body, ai_text, greeting, tail, subject if d["step"] == 1 else d["subject"], draft_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return _done(False, f"Couldn't rewrite it: {e}", tab, draft_id)
    finally:
        cur.close(); conn.close()
    return _done(True, "Rewritten, here and in Outlook. Check it again before approving.", "waiting", draft_id, nxt=draft_id)


@outreach_bp.route("/crm/ai-drafts/<int:draft_id>/stop", methods=["POST"])
@any_team_member
def stop_draft(draft_id: int):
    """Don't send: removes the draft from Outlook and stops this deal."""
    x, err = _guard_json()
    if err:
        return err if _wants_json() else redirect(url_for("outreach.page"))
    customer, actor, _ = x
    tid = int(customer["tenant_id"])
    tab = request.form.get("tab") or "waiting"
    conn, cur = _db()
    try:
        cur.execute("SELECT lead_id FROM outreach_drafts WHERE id=%s AND tenant_id=%s", (draft_id, tid))
        d = cur.fetchone()
    finally:
        cur.close(); conn.close()
    nxt = _next_in_queue(tid, tab, draft_id)
    if d and stop(tid, d["lead_id"], f"Stopped by {actor['label']}"):
        return _done(True, "Not sent. The draft was removed from Outlook and this deal's AI drafts have stopped.",
                     tab, draft_id, nxt=nxt)
    return _done(False, "That deal had already stopped.", tab, draft_id, nxt=nxt)


@outreach_bp.route("/crm/ai-drafts/deal/<int:lead_id>", methods=["POST"])
@any_team_member
def deal_action(lead_id: int):
    """Deal page box: stop, start again, or try a failed draft again now."""
    x, err = _guard_json()
    back = request.referrer or url_for("outreach.page")
    if err:
        flash("Only Philip, David or another manager can change the AI drafts.", "warning")
        return redirect(back)
    customer, actor, _ = x
    tid = int(customer["tenant_id"])
    action = request.form.get("action")
    if action == "stop":
        if stop(tid, lead_id, f"Stopped by {actor['label']}"):
            flash("AI drafts stopped for this deal. Any unsent draft was removed from Outlook.", "success")
    elif action == "start":
        res = start(tid, [lead_id], actor)
        if res.get("error"):
            flash(res["error"], "danger")
        elif res.get("started"):
            flash("AI drafts started. The first draft will be ready to check in about a minute.", "success")
        else:
            flash("Couldn't start: the deal is closed, has no email address, already running, or the buyer replied.", "warning")
    elif action == "retry":
        conn, cur = _db()
        try:
            cur.execute("""UPDATE outreach_runs SET next_draft_on=CURRENT_DATE, last_error=NULL, updated_at=NOW()
                           WHERE tenant_id=%s AND lead_id=%s AND status='running'""", (tid, lead_id))
            conn.commit()
        finally:
            cur.close(); conn.close()
        kick()
        flash("Trying again now. Refresh in a minute.", "success")
    return redirect(back)


@outreach_bp.route("/crm/ai-drafts/settings", methods=["POST"])
@any_team_member
def save_settings():
    x, err = _guard_json()
    if err:
        return redirect(url_for("outreach.page"))
    customer, actor, _ = x
    tid = int(customer["tenant_id"])
    tab = request.form.get("tab") or "waiting"
    try:
        gaps = [int(request.form.get(f"gap{i}") or 0) for i in range(1, MAX_EMAILS)]
        final = int(request.form.get("final_wait") or 0)
    except ValueError:
        gaps, final = [], 0
    if len(gaps) != MAX_EMAILS - 1 or not all(1 <= g <= 60 for g in gaps) or not 1 <= final <= 60:
        flash("Days between emails must each be from 1 to 60.", "danger")
        return redirect(url_for("outreach.page", tab=tab, settings=1))
    mb_raw = request.form.get("mailbox_id") or ""
    keys = [k for k in request.form.getlist("alert_keys") if re.fullmatch(r"team:\d+|owner:\d+", k)]
    conn, cur = _db()
    try:
        cur.execute("""UPDATE outreach_settings SET mailbox_id=%s, gaps=%s, final_wait=%s, guide=%s, signoff_first=%s,
                           signoff_followup=%s, optout=%s, booking_link=%s, booking_on=%s, morning_email=%s,
                           alert_keys=%s, updated_at=NOW()
                       WHERE tenant_id=%s""",
                    (int(mb_raw) if mb_raw.isdigit() else None, gaps, final,
                     (request.form.get("guide") or "").strip()[:8000] or _GUIDE,
                     (request.form.get("signoff_first") or "").replace("\r\n", "\n").strip()[:500],
                     (request.form.get("signoff_followup") or "").replace("\r\n", "\n").strip()[:500],
                     (request.form.get("optout") or "").strip()[:500],
                     (request.form.get("booking_link") or "").strip()[:500],
                     bool(request.form.get("booking_on")), bool(request.form.get("morning_email")), keys, tid))
        conn.commit()
    finally:
        cur.close(); conn.close()
    flash("Settings saved. They apply to drafts written from now on.", "success")
    return redirect(url_for("outreach.page", tab=tab))


# ── Sales Pipeline: filter, counts, a chip on each row ─────────────────────

_PIPE_CONDS = {
    "none": "NOT EXISTS (SELECT 1 FROM outreach_runs r0 WHERE r0.tenant_id=mpl.tenant_id AND r0.lead_id=mpl.id)",
    "running": "r.status='running'",
    "waiting": "EXISTS (SELECT 1 FROM outreach_drafts d WHERE d.run_id=r.id AND d.status='waiting')",
    "approved": "EXISTS (SELECT 1 FROM outreach_drafts d WHERE d.run_id=r.id AND d.status='approved')",
    "replied": "r.status='replied'",
    "stopped": "r.status IN ('stopped','finished')",
}
PIPELINE_FILTER_LABELS = {"none": "Not started", "running": "Running", "waiting": "Waiting for check",
                          "approved": "Approved, not sent", "replied": "Replied", "stopped": "Stopped or finished"}


def pipeline_clause(tenant_id: int, value: str):
    """Sales Pipeline filter ?ai_drafts=…: (sql, params) or (None, None)."""
    if not pilot(tenant_id) or value not in _PIPE_CONDS:
        return None, None
    if value == "none":
        return _PIPE_CONDS["none"], []
    return (f"mpl.id IN (SELECT r.lead_id FROM outreach_runs r WHERE r.tenant_id=%s AND {_PIPE_CONDS[value]})", [tenant_id])


def pipeline_counts(tenant_id: int) -> dict:
    """How many open deals each AI drafts filter would show."""
    if not pilot(tenant_id):
        return {}
    conn, cur = _db()
    try:
        out = {}
        base = """FROM merchant_pipeline_leads mpl WHERE mpl.tenant_id=%s AND mpl.is_opportunity
                  AND mpl.outcome IS NULL AND mpl.dropped_at IS NULL"""
        for k in _PIPE_CONDS:
            sql, params = pipeline_clause(tenant_id, k)
            cur.execute(f"SELECT count(*) AS n {base} AND {sql}", [tenant_id] + params)
            out[k] = cur.fetchone()["n"]
        return out
    finally:
        cur.close(); conn.close()


def row_chips(tenant_id: int, lead_ids: list) -> dict:
    """lead id → {"text", "kind", "link"} for the Pipeline's AI drafts column."""
    if not pilot(tenant_id) or not lead_ids:
        return {}
    conn, cur = _db()
    try:
        cur.execute("""SELECT r.lead_id, r.status, r.emails_sent, r.next_draft_on,
                              (SELECT d.id FROM outreach_drafts d WHERE d.run_id=r.id AND d.status='waiting' ORDER BY d.step LIMIT 1) AS w,
                              (SELECT d.id FROM outreach_drafts d WHERE d.run_id=r.id AND d.status='approved' ORDER BY d.step LIMIT 1) AS a
                       FROM outreach_runs r WHERE r.tenant_id=%s AND r.lead_id = ANY(%s)""", (tenant_id, list(lead_ids)))
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    out = {}
    for r in rows:
        n = r["emails_sent"]
        if r["status"] == "replied":
            out[r["lead_id"]] = {"text": "Replied · answer now", "kind": "replied", "link": "/crm/ai-drafts?tab=replied"}
        elif r["status"] in ("stopped", "finished"):
            out[r["lead_id"]] = {"text": f"{'Finished' if r['status'] == 'finished' else 'Stopped'} · {n}/{MAX_EMAILS}",
                                 "kind": "stopped", "link": None}
        elif r["w"]:
            out[r["lead_id"]] = {"text": f"{n + 1}/{MAX_EMAILS} · waiting check", "kind": "waiting",
                                 "link": f"/crm/ai-drafts?tab=waiting&d={r['w']}"}
        elif r["a"]:
            out[r["lead_id"]] = {"text": f"{n + 1}/{MAX_EMAILS} · ready to send", "kind": "approved",
                                 "link": "/crm/ai-drafts?tab=send"}
        elif n >= MAX_EMAILS:
            out[r["lead_id"]] = {"text": f"{n}/{MAX_EMAILS} · waiting for reply", "kind": "running", "link": None}
        else:
            when = r["next_draft_on"].strftime("%a %-d %b") if r["next_draft_on"] else "soon"
            out[r["lead_id"]] = {"text": f"{n}/{MAX_EMAILS} · next {when}", "kind": "running", "link": None}
    return out


# ── the 9am email to the sender (user, 4 Nov 2026) ─────────────────────────

def morning_email(tid: int) -> bool:
    """Mon–Fri from 9am business time, once a day: tell the sender what's
    ready to send and which replies to answer. Nothing to say = no email."""
    from team_performance import tz_for
    now = datetime.now(tz_for(tid))
    if now.weekday() >= 5 or now.hour < 9:
        return False
    s = settings(tid)
    if not s.get("morning_email", True) or not s["mailbox_id"]:
        return False
    conn, cur = _db()
    try:
        cur.execute("""UPDATE outreach_settings SET morning_sent_on=%s WHERE tenant_id=%s
                       AND morning_sent_on IS DISTINCT FROM %s RETURNING tenant_id""", (now.date(), tid, now.date()))
        if not cur.fetchone():
            conn.commit()
            return False
        conn.commit()
        mb = _mailbox(cur, s["mailbox_id"])
        v = _send_view(cur, tid, s)
        import auto_followups as _af
        to = _af._recipient_emails(cur, tid, mb["owner_key"]) if mb else []
    finally:
        cur.close(); conn.close()
    ready, replies, waiting = len(v["ready"]), len(v["replies"]), v["waiting"]
    if not (ready or replies or waiting) or not to:
        return False
    name = ((mb.get("owner_label") or "").split() or ["there"])[0]
    parts = []
    if ready:
        parts.append(f"{ready} checked email{'s' if ready != 1 else ''} ready to send")
    if replies:
        parts.append(f"{replies} repl{'ies' if replies != 1 else 'y'} to answer")
    subject = "Good morning: " + (" and ".join(parts) if parts else f"{waiting} drafts are being checked")
    lines = []
    if replies:
        lines.append(f"<p><b>Answer first ({replies}):</b></p><ul>" + "".join(
            f"<li>{_html.escape(r['customer_name'] or '')}: “{_html.escape((r['snippet'] or '')[:140])}”</li>"
            for r in v["replies"][:10]) + "</ul>")
    if ready:
        lines.append(f"<p><b>Ready to send ({ready}):</b> in Outlook they show “{_html.escape(CAT_OK)}”.</p><ul>" + "".join(
            f"<li>{_html.escape(r['customer_name'] or '')} · email {r['step']}</li>" for r in v["ready"][:15]) + "</ul>")
    if waiting:
        lines.append(f"<p>{waiting} more draft{'s are' if waiting != 1 else ' is'} waiting for Philip or David to check. "
                     "Don't send drafts marked “" + _html.escape(CAT_WAITING) + "”.</p>")
    link = f"{PORTAL}/crm/ai-drafts?tab=send"
    html_body = (f'<div style="font-family:Arial,sans-serif;max-width:560px;color:#111E2D">'
                 f'<h2 style="color:#0B1D40">Good morning, {_html.escape(name)}</h2>' + "".join(lines) +
                 f'<p><a href="{link}" style="background:#0B1D40;color:#fff;padding:10px 16px;border-radius:10px;'
                 f'text-decoration:none;display:inline-block">Open Ready to send</a></p></div>')
    text = f"Good morning, {name}. " + "; ".join(parts) + f". {link}"
    from portal_utils import send_email
    ok = False
    for e in to:
        try:
            ok = send_email(e, subject, html_body, text_body=text) or ok
        except Exception as ex:
            print("⚠️ outreach morning email:", ex)
    return ok




# ── image signature per sender + Preview (user, 4 Nov 2026) ────────────────

SIG_MAX_BYTES = 1_000_000


def _refresh_open_drafts(tenant_id: int, mailbox_id: int) -> tuple:
    """After a signature is added, changed or removed: the sender's unsent
    drafts get the new closing lines and image, in the portal and in Outlook.
    Returns (updated, failed)."""
    import outlook_mail as M
    s = settings(tenant_id)
    sig = signature(tenant_id, mailbox_id)
    conn, cur = _db()
    ok = bad = 0
    try:
        cur.execute("""SELECT d.*, r.mailbox_id FROM outreach_drafts d JOIN outreach_runs r ON r.id = d.run_id
                       WHERE d.tenant_id=%s AND r.mailbox_id=%s AND d.status IN ('waiting','approved')
                         AND d.ai_text IS NOT NULL""", (tenant_id, mailbox_id))
        rows = cur.fetchall()
        if not rows:
            return 0, 0
        mb = _mailbox(cur, mailbox_id)
        token = _token(cur, mb)
        for d in rows:
            try:
                lead = _lead(cur, tenant_id, d["lead_id"])
                greeting, tail = frame(s, lead, d["step"], sig)
                body = join_body(d["greeting"] or greeting, d["ai_text"], tail)
                M.update_draft(token, d["graph_id"], html=_with_quote(text_to_html(body, sig), d["quoted_html"]))
                if sig:
                    M.set_inline_image(token, d["graph_id"], sig["image"], sig["mime"], SIG_CID,
                                       "signature." + sig["mime"].split("/")[-1])
                else:
                    M.remove_inline_image(token, d["graph_id"], SIG_CID)
                cur.execute("UPDATE outreach_drafts SET tail=%s, body_text=%s, updated_at=NOW() WHERE id=%s",
                            (tail, body, d["id"]))
                conn.commit()
                ok += 1
            except Exception as e:
                conn.rollback()
                print(f"⚠️ outreach signature refresh draft {d['id']}:", e)
                bad += 1
    finally:
        cur.close(); conn.close()
    return ok, bad


@outreach_bp.route("/crm/ai-drafts/signature", methods=["POST"])
@any_team_member
def signature_save():
    x, err = _guard_json()
    if err:
        return redirect(url_for("outreach.page"))
    customer, actor, _ = x
    tid = int(customer["tenant_id"])
    tab = request.form.get("tab") or "waiting"
    mb_raw = request.form.get("mailbox_id") or ""
    back = url_for("outreach.page", tab=tab, settings=1)
    conn, cur = _db()
    try:
        cur.execute("SELECT id FROM mailbox_connections WHERE id=%s AND tenant_id=%s", (int(mb_raw) if mb_raw.isdigit() else 0, tid))
        if not cur.fetchone():
            flash("Pick who sends first, and save the settings.", "danger")
            return redirect(back)
        mb_id = int(mb_raw)
        link = (request.form.get("sig_link") or "").strip()[:500]
        if link and not re.match(r"^https?://", link):
            link = "https://" + link
        if request.form.get("action") == "remove":
            cur.execute("DELETE FROM outreach_signatures WHERE tenant_id=%s AND mailbox_id=%s", (tid, mb_id))
            conn.commit()
            msg = "Signature removed."
        else:
            f = request.files.get("sig_file")
            data = f.read(SIG_MAX_BYTES + 1) if f and f.filename else b""
            if not data:
                cur.execute("UPDATE outreach_signatures SET link=%s, updated_at=NOW() WHERE tenant_id=%s AND mailbox_id=%s",
                            (link or None, tid, mb_id))
                conn.commit()
                if not cur.rowcount:
                    flash("Choose a PNG or JPG image to upload.", "danger")
                    return redirect(back)
                msg = "Signature link saved."
            else:
                if len(data) > SIG_MAX_BYTES:
                    flash("That image is over 1 MB. Please use a smaller one.", "danger")
                    return redirect(back)
                import io
                from PIL import Image
                try:
                    im = Image.open(io.BytesIO(data)); im.load()
                except Exception:
                    flash("That file isn't an image we can read. Please upload a PNG or JPG.", "danger")
                    return redirect(back)
                if im.format not in ("PNG", "JPEG"):
                    flash("Please upload a PNG or JPG image.", "danger")
                    return redirect(back)
                fmt = im.format
                w, h = im.size
                if w > 1200:   # keep it light for email; still sharp on high-resolution screens
                    im = im.resize((1200, round(h * 1200 / w)), Image.LANCZOS)
                    buf = io.BytesIO(); im.save(buf, format=fmt); data = buf.getvalue()
                    w, h = im.size
                mime = "image/png" if fmt == "PNG" else "image/jpeg"
                disp_w = min(w, 600)
                cur.execute("""INSERT INTO outreach_signatures (tenant_id, mailbox_id, image, mime, width, height, link)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (tenant_id, mailbox_id) DO UPDATE SET image=EXCLUDED.image, mime=EXCLUDED.mime,
                                   width=EXCLUDED.width, height=EXCLUDED.height, link=EXCLUDED.link, updated_at=NOW()""",
                            (tid, mb_id, psycopg2.Binary(data), mime, disp_w, round(h * disp_w / w), link or None))
                conn.commit()
                msg = "Signature saved. It's added to every email from now on."
    finally:
        cur.close(); conn.close()
    try:
        ok, bad = _refresh_open_drafts(tid, mb_id)
        if ok:
            msg += f" {ok} draft{'s' if ok != 1 else ''} waiting to be sent {'were' if ok != 1 else 'was'} updated too."
        if bad:
            msg += f" {bad} couldn't be updated in Outlook; try again in a minute."
    except Exception as e:
        print("⚠️ outreach signature refresh:", e)
        msg += " The drafts already waiting couldn't be updated in Outlook right now."
    flash(msg, "success")
    return redirect(back)


@outreach_bp.route("/crm/ai-drafts/signature/<int:mailbox_id>.img", methods=["GET"])
@any_team_member
def signature_image(mailbox_id: int):
    from flask import Response, abort
    x = _ctx()
    if not x:
        abort(404)
    sig = signature(int(x[0]["tenant_id"]), mailbox_id)
    if not sig:
        abort(404)
    return Response(sig["image"], mimetype=sig["mime"], headers={"Cache-Control": "private, max-age=60"})


@outreach_bp.route("/crm/ai-drafts/<int:draft_id>/preview", methods=["GET", "POST"])
@any_team_member
def preview_draft(draft_id: int):
    """The email exactly as the buyer will see it (POST carries unsaved edits)."""
    from flask import abort
    x = _ctx()
    if not x:
        abort(404)
    tid = int(x[0]["tenant_id"])
    conn, cur = _db()
    try:
        cur.execute("""SELECT d.*, r.mailbox_id FROM outreach_drafts d JOIN outreach_runs r ON r.id = d.run_id
                       WHERE d.id=%s AND d.tenant_id=%s""", (draft_id, tid))
        d = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not d:
        abort(404)
    sig = signature(tid, d["mailbox_id"])
    body = d["body_text"] or ""
    if request.method == "POST" and d["ai_text"] is not None and request.form.get("ai_text") is not None:
        body = join_body(d["greeting"] or "", request.form.get("ai_text").replace("\r\n", "\n"), d["tail"] or "")
    elif request.method == "POST" and request.form.get("body") is not None:
        body = request.form.get("body").replace("\r\n", "\n")
    src = url_for("outreach.signature_image", mailbox_id=d["mailbox_id"]) + f"?v={int(sig['updated_at'].timestamp())}" if sig else None
    html_body = _with_quote(text_to_html(body, sig, src), d["quoted_html"])
    if src:
        html_body = html_body.replace("cid:" + SIG_CID, src)
    head = '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><base target="_blank">'
    if "<html" not in html_body.lower():
        html_body = f'<!doctype html><html><head>{head}</head><body style="margin:16px">{html_body}</body></html>'
    else:
        html_body = re.sub(r"(?i)<body([^>]*)>", r'<body\1 style="margin:16px">', html_body, count=1)
        html_body = re.sub(r"(?i)<head[^>]*>", lambda m: m.group(0) + head, html_body, count=1)
    from flask import Response
    return Response(html_body, mimetype="text/html", headers={"Content-Security-Policy": "script-src 'none'"})
