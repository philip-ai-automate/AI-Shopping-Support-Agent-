"""
social_ai_planner.py — Social Posts › "Plan my month with AI" (2026-09-26).

The AI suggests a month of posts (day, topic, theme, a caption per network,
call to action, and a short brief for the picture). Nothing reaches the
calendar until someone picks which to add; accepted ones become ordinary
drafts with a planned day, person responsible and due date.

Cost: text only, so it does NOT use the AI design allowance (user decision
2026-09-26). It runs on the business's own AI key when one is connected,
otherwise on PhiXtra's key, capped at MAX_PLANS_PER_MONTH per business so a
stuck button can't run up a bill.

A planned draft has no picture yet. "Make the picture" on its page opens the
AI Post Designer or Upload Design with the topic filled in, and their last
step fills THAT draft instead of making a second post (fill_target()).
"""
import json
from datetime import datetime, timezone, timedelta, date

import psycopg2.extras
from flask import session

from db import get_db_connection, insert_audit_log
import ai_designer as D
import buffer_accounts as ba
import social_workflow as W

MAX_PLANS_PER_MONTH = 10
MAX_POSTS = 31
COUNTS = (4, 8, 12, 16, 20)
THEMES = [
    ("tips", "Tips and how-tos"),
    ("product", "Products and services"),
    ("story", "Customer stories"),
    ("offer", "Offers"),
    ("behind", "Behind the scenes"),
    ("news", "News and updates"),
    ("question", "Questions to get people talking"),
]
THEME_NAMES = dict(THEMES)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FILL_KEY = "social_fill_post"


class PlanError(Exception):
    """Plain-English reason a plan couldn't be made."""


# ── Settings from the form ─────────────────────────────────────────────────

def month_choices(today: date) -> list:
    """This month (if there are days left) and the next two."""
    out, first = [], today.replace(day=1)
    for _ in range(3):
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        if nxt - timedelta(days=1) > today:
            out.append(first)
        first = nxt
    return out


def posting_days(first: date, today: date, weekdays: list) -> list:
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    d, out = max(first, today + timedelta(days=1)), []
    while d < nxt:
        if d.weekday() in weekdays:
            out.append(d)
        d += timedelta(days=1)
    return out


def spread(days: list, n: int) -> list:
    """n days spread evenly across the list, in order."""
    if n >= len(days):
        return list(days)
    return [days[int(i * len(days) / n)] for i in range(n)]


# ── Making the plan ────────────────────────────────────────────────────────

def plans_this_month(tenant_id: int) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT COUNT(*) FROM social_ai_plans WHERE tenant_id=%s AND source='platform'
                       AND created_at >= date_trunc('month', NOW())""", (tenant_id,))
        return cur.fetchone()[0]
    finally:
        cur.close(); conn.close()


def ai_source(customer) -> str:
    """'own_key' when the business has a working AI key of its own, else 'platform'."""
    tid = int(customer["tenant_id"])
    allow = D.allowance(D.tenant_owner(tid), tid)
    return "own_key" if allow.get("own_key") else "platform"


_SKIP_PAGES = ("about", "contact", "privacy", "terms", "cookie", "cart", "checkout", "account", "login",
               "register", "refund", "return", "shipping policy", "faq", "sitemap", "blog", "resources")


def _plain(html_text: str, n: int) -> str:
    import re
    t = re.sub(r"<[^>]+>", " ", html_text or "")
    return re.sub(r"\s+", " ", t).strip()[:n]


def _business_context(customer) -> dict:
    """What the AI is told about the business, from what it has already
    given PhiXtra: Brand Kit, Store Information (or a synced "About" web
    page), its products or web pages, and its recent posts."""
    tid = int(customer["tenant_id"])
    kit = D.get_brand_kit(D.tenant_owner(tid), "")
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT name FROM tenants WHERE id=%s", (tid,))
        tname = (cur.fetchone() or {}).get("name")
        cur.execute("SELECT content FROM documents WHERE id=%s", (f"store_info-{tid}-about_us",))
        about = _plain((cur.fetchone() or {}).get("content"), 2500)
        if not about:
            cur.execute("""SELECT content FROM documents WHERE tenant_id=%s AND type='page' AND title ILIKE 'about%%'
                           ORDER BY length(content) DESC LIMIT 1""", (str(tid),))
            about = _plain((cur.fetchone() or {}).get("content"), 2500)
        cur.execute("""SELECT title FROM documents WHERE tenant_id=%s AND type='product' AND title IS NOT NULL
                       ORDER BY updated_at DESC NULLS LAST LIMIT 20""", (str(tid),))
        offers = [r["title"] for r in cur.fetchall() or []]
        if not offers:
            cur.execute("""SELECT title FROM documents WHERE tenant_id=%s AND type='page' AND title IS NOT NULL
                           ORDER BY updated_at DESC NULLS LAST LIMIT 40""", (str(tid),))
            offers = [r["title"] for r in cur.fetchall() or []
                      if not any(w in r["title"].lower() for w in _SKIP_PAGES)][:20]
        cur.execute("""SELECT caption FROM tenant_social_posts WHERE tenant_id=%s AND caption IS NOT NULL
                       ORDER BY created_at DESC LIMIT 15""", (tid,))
        recent = [W.title_of(r, 90) for r in cur.fetchall() or []]
    finally:
        cur.close(); conn.close()
    name = (kit.get("display_name") or customer.get("company_name") or customer.get("business_name")
            or tname or "").strip()
    return {"name": name or "the business",
            "voice": D.VOICES.get(kit.get("voice"), "Warm and friendly"),
            "about": about, "offers": [o[:100] for o in offers],
            "recent": [t for t in recent if t and t != "Untitled post"]}


def knows_little(customer) -> bool:
    """True when the AI would have nothing about the business to go on."""
    c = _business_context(customer)
    return not c["about"] and not c["offers"]


def _prompt(ctx: dict, days: list, services: list, themes: list, focus: str) -> str:
    rules = "\n".join("- " + D.SERVICE_RULES.get(s, f"{s}: short and friendly.") for s in services)
    lines = [f"Business: {ctx['name']}.", f"Voice: {ctx['voice']}."]
    if ctx["about"]:
        lines.append("About the business (their own words):\n" + ctx["about"])
    if ctx.get("offers"):
        lines.append("What they sell or offer:\n" + "\n".join("- " + o for o in ctx["offers"]))
    if focus:
        lines.append(f"What they want to focus on this month: {focus}")
    if ctx["recent"]:
        lines.append("Recent posts (don't repeat these topics):\n" + "\n".join("- " + t for t in ctx["recent"]))
    theme_list = ", ".join(f"\"{k}\" ({THEME_NAMES[k]})" for k in themes)
    return (
        "You plan a month of social media posts for a small business. Reply with JSON only.\n\n"
        + "\n\n".join(lines) + "\n\n"
        f"Plan exactly {len(days)} posts, one for each of these dates in this order: "
        + ", ".join(d.strftime("%a %d %b") for d in days) + ".\n"
        f"Mix these themes across the month: {theme_list}. Vary the topics; no two posts about the same thing.\n\n"
        "Return JSON shaped exactly like this, one object per date, in date order:\n"
        + json.dumps({"posts": [{"topic": "...", "theme": themes[0] if themes else "tips",
                                 "captions": {s: "..." for s in services}, "cta": "...", "brief": "..."}]}) + "\n"
        "- \"topic\": the post's subject in under 10 words, specific to this business.\n"
        "- \"theme\": one of the theme keys above.\n"
        f"- \"captions\": ONLY these keys: {', '.join(services)}. Nothing else goes in captions.\n"
        "- \"cta\": the call to action in under 8 words (e.g. \"Send us a WhatsApp message\").\n"
        "- \"brief\": one or two sentences telling a designer what the picture should show.\n"
        "Caption rules:\n" + rules + "\n"
        "Never invent prices, discounts, dates, stock counts, phone numbers or customer names. "
        "Only use facts given above. If a post needs a detail you don't have, write [add detail] for the business to fill in."
    )


def _ask(client, prompt: str) -> list:
    resp = client.chat.completions.create(model=D.TEXT_MODEL, messages=[{"role": "user", "content": prompt}],
                                          response_format={"type": "json_object"})
    try:
        data = json.loads(resp.choices[0].message.content or "{}")
    except ValueError:
        raise PlanError("The AI's answer didn't come through properly. Try again.")
    raw = data.get("posts") if isinstance(data, dict) else None
    return [p if isinstance(p, dict) else {} for p in raw] if isinstance(raw, list) else []


def _item(p: dict, d: date, services: list, themes: list):
    topic = str(p.get("topic") or "").strip()[:120]
    if not topic:
        return None
    caps = dict(p.get("captions")) if isinstance(p.get("captions"), dict) else {}
    for k in ("cta", "brief"):                  # the AI sometimes nests these in captions
        if not p.get(k) and caps.get(k):
            p[k] = caps.pop(k)
    return {"date": d.isoformat(), "topic": topic,
            "theme": p.get("theme") if p.get("theme") in themes else themes[d.day % len(themes)],
            "captions": {s: str(caps.get(s) or "").strip()[:2200] or topic for s in services},
            "cta": str(p.get("cta") or "").strip()[:120],
            "brief": str(p.get("brief") or "").strip()[:600],
            "post_id": None}


def make_plan(customer, actor: str, *, month: date, count: int, channels: list, weekdays: list,
              time_text: str, themes: list, focus: str, owner: dict, due_days: int) -> int:
    """Asks the AI, saves the suggestions, returns the plan id. Raises PlanError."""
    tid = int(customer["tenant_id"])
    today = W.today_for(customer)
    days = spread(posting_days(month, today, weekdays), count)
    if not days:
        raise PlanError("There are no posting days left in that month on the days you picked. "
                        "Pick more days of the week, or a later month.")
    services = sorted({c["service"] for c in channels})
    source = ai_source(customer)
    if source == "platform" and plans_this_month(tid) >= MAX_PLANS_PER_MONTH:
        raise PlanError(f"This business has made {MAX_PLANS_PER_MONTH} AI month plans this month, the most allowed. "
                        "Open one of the plans already made, or try again next month.")
    allowed_themes = themes or [k for k, _ in THEMES]
    ctx = _business_context(customer)
    owner_key = D.tenant_owner(tid)
    try:
        client = D._client(owner_key, "own_key" if source == "own_key" else "allowance")
    except D.DesignError as e:
        raise PlanError(str(e).replace(" This wasn't counted.", ""))
    # The AI sometimes stops short of the number asked for, so ask again for
    # the days still missing (twice at most), telling it what's already planned.
    by_day, first_error = {}, None
    for _ in range(3):
        missing = [d for d in days if d not in by_day]
        if not missing:
            break
        extra = dict(ctx, recent=ctx["recent"] + [it["topic"] for it in by_day.values()])
        try:
            got = _ask(client, _prompt(extra, missing, services, allowed_themes, focus))
        except PlanError as e:
            first_error = first_error or e
            break
        except Exception as e:
            first_error = first_error or PlanError(str(D._ai_error(owner_key, source, e))
                                                   .replace(" This wasn't counted.", "").replace("the designs", "the plan"))
            break
        for d, p in zip(missing, got):
            item = _item(p, d, services, allowed_themes)
            if item:
                by_day[d] = item
        if not got:
            break
    if not by_day:
        raise first_error or PlanError("The AI didn't send back a plan. Try again.")
    items = [dict(by_day[d], i=n) for n, d in enumerate(d for d in days if d in by_day)]
    if not items:
        raise PlanError("The AI didn't send back a usable plan. Try again.")
    settings = {"month": month.isoformat(), "channel_ids": [c["channel_id"] for c in channels],
                "time": time_text, "owner_key": owner["key"], "owner_label": owner["label"],
                "due_days": due_days, "themes": allowed_themes, "focus": focus, "weekdays": weekdays}
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO social_ai_plans (tenant_id, month, settings, items, source, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (tid, month, json.dumps(settings), json.dumps(items), source, actor))
        plan_id = cur.fetchone()[0]
        conn.commit()
    finally:
        cur.close(); conn.close()
    insert_audit_log(action="social_ai_month_plan", tenant_id=tid,
                     details={"plan_id": plan_id, "posts": len(items), "source": source, "by": actor})
    return plan_id


def get_plan(tenant_id: int, plan_id: int):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM social_ai_plans WHERE id=%s AND tenant_id=%s", (plan_id, tenant_id))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def recent_plans(tenant_id: int, limit: int = 5) -> list:
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT id, month, created_by, created_at, jsonb_array_length(items) AS n,
                              (SELECT COUNT(*) FROM jsonb_array_elements(items) e WHERE e->>'post_id' IS NOT NULL) AS added
                       FROM social_ai_plans WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s""", (tenant_id, limit))
        return cur.fetchall() or []
    finally:
        cur.close(); conn.close()


# ── Adding the chosen suggestions as drafts ────────────────────────────────

def add_drafts(customer, plan: dict, picks: list, actor: str) -> int:
    """picks: [{"i", "date": date, "owner": person dict}]. Creates one draft
    per pick not already added. Returns how many were added."""
    tid = int(customer["tenant_id"])
    s = plan["settings"]
    items = plan["items"]
    tz = W.tz_for(customer)
    hh, mm = (int(x) for x in (s.get("time") or "10:00").split(":"))
    today = W.today_for(customer)
    usable = {c["channel_id"] for c in ba.list_channels(ba.tenant_owner(tid), enabled_only=True)}
    channel_ids = [c for c in s.get("channel_ids") or [] if c in usable]
    added, given = 0, {}
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        for pk in picks:
            it = next((x for x in items if x["i"] == pk["i"]), None)
            if not it or it.get("post_id"):
                continue
            when = datetime(pk["date"].year, pk["date"].month, pk["date"].day, hh, mm, tzinfo=tz).astimezone(timezone.utc)
            due = None
            if s.get("due_days") is not None and s.get("due_days") != "":
                due = max(pk["date"] - timedelta(days=int(s["due_days"])), today)
            caps = it["captions"]
            main = next((t for t in caps.values() if t), it["topic"])
            brief = it["brief"] + (f"\nCall to action: {it['cta']}" if it.get("cta") else "")
            person = pk["owner"]
            cur.execute("""INSERT INTO tenant_social_posts (tenant_id, caption, captions, public_token, channel_ids, status,
                                  scheduled_for, created_by, owner_key, owner_label, due_date, plan_topic, plan_brief, ai_plan_id)
                           VALUES (%s,%s,%s,md5(random()::text || clock_timestamp()::text),%s,'draft',%s,%s,%s,%s,%s,%s,%s,%s)
                           RETURNING id""",
                        (tid, main, json.dumps(caps), channel_ids, when, actor, person["key"], person["label"],
                         due, it["topic"], brief.strip(), plan["id"]))
            it["post_id"] = cur.fetchone()["id"]
            added += 1
            if person.get("email") and person["label"] != actor:
                given.setdefault(person["key"], (person, []))[1].append((it, due))
        cur.execute("UPDATE social_ai_plans SET items=%s WHERE id=%s AND tenant_id=%s",
                    (json.dumps(items), plan["id"], tid))
        conn.commit()
    finally:
        cur.close(); conn.close()
    for it in items:
        if it.get("post_id") and any(pk["i"] == it["i"] for pk in picks):
            W.log_event(tid, it["post_id"], "planned", actor)
    for person, list_ in given.values():
        _email_assignee(person, list_, actor)
    if added:
        insert_audit_log(action="social_ai_plan_added", tenant_id=tid,
                         details={"plan_id": plan["id"], "added": added, "by": actor})
    return added


def _email_assignee(person: dict, items: list, by_label: str):
    """One email per person listing every planned post they were given."""
    link = f"{W._base_url()}/social-posts/calendar"
    rows = "".join(f"<li><b>{W._esc(it['topic'])}</b>" + (f" · due {due.strftime('%a %d %b')}" if due else "") + "</li>"
                   for it, due in items)
    text_rows = "\n".join(f"- {it['topic']}" + (f" (due {due.strftime('%a %d %b')})" if due else "") for it, due in items)
    n = len(items)
    subject = f"{n} social post{'s' if n != 1 else ''} planned for you"
    W._send_later([person["email"]], subject,
                  f"<p>{W._esc(by_label)} gave you {n} planned social post{'s' if n != 1 else ''}:</p><ul>{rows}</ul>"
                  f"<p><a href=\"{link}\">Open the calendar</a></p>",
                  f"{by_label} gave you {n} planned social post{'s' if n != 1 else ''}:\n{text_rows}\n\nOpen the calendar: {link}\n")


# ── "Make the picture" for a planned draft ─────────────────────────────────

def start_fill(post: dict):
    """Remembers which draft the next AI design / upload should fill (2 hours)."""
    session[FILL_KEY] = f"{post['id']}|{int(datetime.now(timezone.utc).timestamp())}"
    d = W.post_date(post)
    if d:
        session["social_plan_date"] = f"{d.date().isoformat()}|{int(datetime.now(timezone.utc).timestamp())}"


def fill_target(tenant_id: int):
    """The draft being given a picture, if it's still a draft that isn't
    waiting for approval; else None."""
    pid, _, at = (session.get(FILL_KEY) or "").partition("|")
    if not (pid.isdigit() and at.isdigit()) or datetime.now(timezone.utc).timestamp() - int(at) > 7200:
        return None
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT * FROM tenant_social_posts WHERE id=%s AND tenant_id=%s AND status='draft'
                         AND approval IS DISTINCT FROM 'waiting'""", (int(pid), tenant_id))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def fill_idea(post: dict) -> str:
    """What to pre-fill in the AI Post Designer's "Describe the post"."""
    topic = post.get("plan_topic") or W.title_of(post, 120)
    brief = (post.get("plan_brief") or "").strip()
    return (topic + (". " + brief if brief else ""))[:600]
