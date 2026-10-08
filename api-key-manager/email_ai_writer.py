"""
email_ai_writer.py — "✨ Write with AI" on the New Email page (2026-10-08).
Every business. No Flask here; the routes are in portal_routes.py.

Staff type a brief and pick the type of email and the tone; the AI fills the
editor (subject, preview text, message, button words). Staff check it and
press Save email themselves — the AI never sends anything.

Counting (approved 2026-10-08): uses the business's AI designs balance, the
same one as the AI Post Designer (ai_designer.allowance / _record):
  * a new draft = 1 AI design, only if it works;
  * up to 5 rewrites per draft are free ("Try again", "Shorter", ...);
  * nothing left → the Post Designer's "buy extra / upgrade" message.

What the AI reads: the brief, the business name, the business's own website
pages / product information already in PhiXtra (documents table, best
matches for the brief), and up to two of the viewer's saved emails as a style
guide. Any sentence with a number that isn't in that information is dropped,
so prices, discounts and dates can't be invented.
"""
import html as _html
import json
import re

import psycopg2.extras

import ai_designer as D
from db import get_db_connection

KINDS = {
    "first_contact": "A first email to a business that doesn't know us yet (cold outreach). Earn a reply; no hard sell.",
    "follow_up": "A follow-up to someone who got an earlier email and hasn't replied. Short, polite, a new angle.",
    "offer": "An email about an offer or a product. Clear benefit, one call to action.",
    "newsletter": "A newsletter-style update for existing contacts and customers.",
    "event": "An invitation to an event, webinar or demo.",
}
TONES = {
    "friendly": "Warm, friendly and plain. Like a helpful person, not a brochure.",
    "professional": "Professional and clear, still human. No jargon.",
    "short": "Very short: under 90 words in the message.",
}
REWRITES = {
    "again": "Write a fresh version with a different angle.",
    "shorter": "Make it noticeably shorter. Keep the main point and the call to action.",
    "formal": "Make it more formal and professional.",
    "friendlier": "Make it warmer and friendlier.",
}
FREE_REWRITES = D.FREE_REWRITES

_NUM = re.compile(r"\d[\d,.]*")


class WriterError(Exception):
    """Plain-English reason the AI couldn't write the email."""


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _text(html_body: str) -> str:
    t = re.sub(r"<[^>]+>", " ", html_body or "")
    return re.sub(r"\s+", " ", _html.unescape(t)).strip()


def business_info(tenant_id: int, brief: str, limit: int = 4, per: int = 1800) -> str:
    """The business's own information that best matches the brief."""
    conn, cur = _db()
    try:
        cur.execute("""SELECT title, content FROM documents
                       WHERE tenant_id=%s AND content IS NOT NULL AND content <> ''
                       ORDER BY ts_rank(search_vector, plainto_tsquery('english', %s)) DESC,
                                (title ILIKE '%%about%%') DESC, id
                       LIMIT %s""", (tenant_id, brief or "about us", limit))
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    parts = []
    for r in rows:
        body = re.sub(r"\s+", " ", r["content"])[:per]
        parts.append(f"[{r['title'] or 'Page'}]\n{body}")
    return "\n\n".join(parts)


def _style_examples(tenant_id: int, visible_sql: str, visible_params: list, n: int = 2) -> list:
    conn, cur = _db()
    try:
        cur.execute("SELECT t.name, t.fields FROM email_templates t WHERE t.tenant_id=%s" + visible_sql
                    + " ORDER BY t.updated_at DESC NULLS LAST LIMIT %s", [tenant_id] + visible_params + [n])
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()
    return [{"subject": (r["fields"] or {}).get("subject") or "", "text": _text((r["fields"] or {}).get("body_html"))[:900]}
            for r in rows]


def usual_button_link(tenant_id: int, visible_sql: str, visible_params: list) -> str:
    """The button link the business uses most in its saved emails, if any."""
    conn, cur = _db()
    try:
        cur.execute("""SELECT t.fields->>'cta_url' AS u, count(*) AS n FROM email_templates t
                       WHERE t.tenant_id=%s AND COALESCE(t.fields->>'cta_url', '') <> ''""" + visible_sql
                    + " GROUP BY 1 ORDER BY n DESC LIMIT 1", [tenant_id] + visible_params)
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    return row["u"] if row else ""


def _prompt(business: str, d: dict, info: str, examples: list, extra: str = "") -> str:
    lines = [
        f"Business sending the email: {business}.",
        f"Type of email: {KINDS.get(d['kind'], KINDS['first_contact'])}",
        f"Tone: {TONES.get(d['tone'], TONES['friendly'])}",
        f"What the email is for (from the team): {d['brief']}",
    ]
    if info:
        lines.append("Facts about the business (use only these; quote nothing that isn't here):\n" + info)
    if examples:
        lines.append("Emails this team already uses (match their style, don't copy them):\n" + "\n---\n".join(
            f"Subject: {e['subject']}\n{e['text']}" for e in examples))
    if extra:
        lines.append(extra)
    return (
        "You write marketing and sales emails for a business. Reply with JSON only.\n\n"
        + "\n\n".join(lines) + "\n\n"
        "Return an object with:\n"
        "- \"subject\": under 60 characters, no clickbait, no ALL CAPS, at most one emoji (prefer none).\n"
        "- \"preheader\": one short line shown next to the subject in inboxes.\n"
        "- \"paragraphs\": the message as a list of short paragraphs (plain text, 1-3 sentences each). "
        "Start with \"Hi {{First Name}},\". You may use {{Business Name}} for the reader's business. "
        "A line starting with \"> \" is shown as a quote (use for things customers ask). No sign-off — it's added separately.\n"
        "- \"button_text\": 2-5 words for the one call-to-action button.\n"
        "Rules: plain everyday words; no em dashes; never invent prices, discounts, percentages, dates, stock, "
        "phone numbers or results — only numbers that appear in the facts or the team's brief; don't promise "
        "things the facts don't support; keep it readable on a phone."
    )


def _to_html(paragraphs: list) -> str:
    out = []
    for p in paragraphs or []:
        p = str(p or "").strip()
        if not p:
            continue
        if p.startswith(">"):
            out.append(f"<blockquote>{_html.escape(p.lstrip('> ').strip())}</blockquote>")
        else:
            out.append(f"<p>{_html.escape(p)}</p>")
    return "".join(out)


def _drop_invented_numbers(paragraphs: list, allowed_source: str) -> list:
    """Remove any sentence with a number the business never gave us."""
    allowed = {n.replace(",", "").rstrip(".") for n in _NUM.findall(allowed_source or "")}
    keep = []
    for p in paragraphs or []:
        sentences = re.split(r"(?<=[.!?])\s+", str(p or ""))
        ok = [s for s in sentences
              if all(n.replace(",", "").rstrip(".") in allowed for n in _NUM.findall(s))]
        if ok:
            keep.append(" ".join(ok))
    return keep


def _ask(prompt: str) -> dict:
    client = D._client("email", "platform")
    resp = client.chat.completions.create(model=D.TEXT_MODEL, messages=[{"role": "user", "content": prompt}],
                                          response_format={"type": "json_object"})
    return json.loads(resp.choices[0].message.content or "{}")


def _result(words: dict, allowed_source: str, button_link: str) -> dict:
    paras = _drop_invented_numbers(words.get("paragraphs") or [], allowed_source)
    subject = str(words.get("subject") or "").strip()
    if _drop_invented_numbers([subject], allowed_source) != [subject]:
        subject = ""  # an invented number in the subject: treat as a failed try (not counted)
    if not paras or not subject:
        raise WriterError("The AI couldn't write a usable email this time. This wasn't counted. Try again.")
    return {"subject": subject[:150], "preheader": str(words.get("preheader") or "").strip()[:200],
            "body_html": _to_html(paras), "cta_text": str(words.get("button_text") or "").strip()[:60],
            "cta_url": button_link}


def write(tenant_id: int, actor_key: str, actor_label: str, business: str, brief: str, kind: str, tone: str,
          visible_sql: str, visible_params: list) -> dict:
    """A new draft. Uses 1 AI design if it works. Returns {"draft_id", "email", "rewrites_left"}."""
    brief = (brief or "").strip()[:1200]
    if len(brief) < 8:
        raise WriterError("Tell the AI a little more about what the email is for.")
    owner = D.tenant_owner(tenant_id)
    allow = D.allowance(owner, tenant_id)
    if not allow["source"]:
        raise WriterError(D.no_designs_message(allow))
    d = {"brief": brief, "kind": kind if kind in KINDS else "first_contact", "tone": tone if tone in TONES else "friendly"}
    info = business_info(tenant_id, brief)
    link = usual_button_link(tenant_id, visible_sql, visible_params)
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO email_ai_drafts (tenant_id, created_by_key, brief, kind, tone)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""", (tenant_id, actor_key, d["brief"], d["kind"], d["tone"]))
        draft_id = cur.fetchone()["id"]; conn.commit()
    finally:
        cur.close(); conn.close()
    try:
        words = _ask(_prompt(business, d, info, _style_examples(tenant_id, visible_sql, visible_params)))
        email = _result(words, brief + " " + info, link)
    except WriterError as e:
        D._record(owner, tenant_id, draft_id, "email_draft", allow["source"], False, True, str(e), actor_label)
        raise
    except Exception as e:
        D._record(owner, tenant_id, draft_id, "email_draft", allow["source"], False, True, f"{type(e).__name__}: {e}", actor_label)
        busy = "busy" in str(D._ai_error(owner, allow["source"], e))
        raise WriterError("The AI is busy right now. Wait a minute and try again. This wasn't counted." if busy
                          else "The AI couldn't write the email this time. This wasn't counted. Try again.")
    _save(draft_id, email, 0)
    D._record(owner, tenant_id, draft_id, "email_draft", allow["source"], True, False, d["brief"][:200], actor_label)
    return {"draft_id": draft_id, "email": email, "rewrites_left": FREE_REWRITES}


def rewrite(tenant_id: int, actor_key: str, actor_label: str, business: str, draft_id: int, how: str,
            current: dict, visible_sql: str, visible_params: list) -> dict:
    """Free, up to 5 per draft. `current` = what's in the editor now (staff may have changed it)."""
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM email_ai_drafts WHERE id=%s AND tenant_id=%s AND created_by_key=%s",
                    (draft_id, tenant_id, actor_key))
        d = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not d:
        raise WriterError("Start with “✨ Write it” first.")
    if d["rewrites_used"] >= FREE_REWRITES:
        raise WriterError("You've used the 5 free rewrites for this email. Change the words yourself, or press “✨ Write it” for a new one.")
    owner = D.tenant_owner(tenant_id)
    info = business_info(tenant_id, d["brief"])
    extra = (f"Current email (the team may have edited it):\nSubject: {current.get('subject') or ''}\n"
             f"{_text(current.get('body_html'))[:2500]}\nButton: {current.get('cta_text') or ''}\n\n"
             f"Rewrite request: {REWRITES.get(how, REWRITES['again'])}")
    try:
        words = _ask(_prompt(business, d, info, [], extra))
        email = _result(words, d["brief"] + " " + info + " " + _text(current.get("body_html")),
                        current.get("cta_url") or "")
    except WriterError as e:
        D._record(owner, tenant_id, draft_id, "email_rewrite", "platform", False, True, str(e), actor_label)
        raise
    except Exception as e:
        D._record(owner, tenant_id, draft_id, "email_rewrite", "platform", False, True, f"{type(e).__name__}: {e}", actor_label)
        raise WriterError("The AI couldn't rewrite it this time. This wasn't counted. Try again.")
    used = d["rewrites_used"] + 1
    _save(draft_id, email, used)
    D._record(owner, tenant_id, draft_id, "email_rewrite", "platform", False, False, how, actor_label)
    return {"draft_id": draft_id, "email": email, "rewrites_left": FREE_REWRITES - used}


def _save(draft_id: int, email: dict, rewrites_used: int) -> None:
    conn, cur = _db()
    try:
        cur.execute("UPDATE email_ai_drafts SET result=%s, rewrites_used=%s, updated_at=NOW() WHERE id=%s",
                    (psycopg2.extras.Json(email), rewrites_used, draft_id))
        conn.commit()
    finally:
        cur.close(); conn.close()
