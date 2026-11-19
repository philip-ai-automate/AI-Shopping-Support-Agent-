"""
ask_phixtra.py — Ask PhiXtra, the help panel inside the customer portal (18 Nov 2026).

A business's staff ask "how do I…" and get short steps plus a "Take me there"
link. Approved mock-up: https://claude.ai/artifact/CQzpZv56Fv8v86CotpCbML

Where the answers come from (no help articles are written):
  1. A map of the portal, rebuilt by itself whenever the portal's pages change:
     every side-menu item (with its menu path and plan lock), the portal search
     catalogue (titles, descriptions, keywords) and the headings, buttons and
     field names on each page's template.
  2. The person asking, at that moment: the page they are on, their plan (what
     it includes), their team role (which pages they may open) and their
     business's set-up status (website, products, WhatsApp, chat box, Shopify,
     Xero, QuickBooks, Sage).
  3. Nothing else. No buyers' chats or customer details are ever read. If the
     map has no answer it says so and offers Email PhiXtra Support; the question
     appears in Admin › Ask PhiXtra (unanswered only).

It never changes anything in the business's account: it explains and links.
"""
import html
import inspect
import json
import os
import re
import threading
import time
from glob import glob

import psycopg2.extras
from flask import Blueprint, current_app, jsonify, render_template, request, session, url_for

from db import get_db_connection
from feature_access import TEAM, TEAM_ANY, any_team_member, route_access

ask_bp = Blueprint("askphx", __name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_TPL = os.path.join(_HERE, "templates")
_BASE = os.path.join(_TPL, "portal", "base.html")
SUPPORT_EMAIL = os.getenv("ASK_PHIXTRA_SUPPORT_EMAIL", "support@phixtra.com")
HOURLY_LIMIT = 30          # questions per person per hour (AI cost guard)
MAX_QUESTION = 500
TIMEOUT = 40

STOP = set("""a an and are as at be but by can do does for from get go has have how i if in into is it its me my
no not of on or our out so that the their them then there these they this to up us was we what when where which
who why will with you your yours i'm im dont don't cant can't please want need see show find make use using""".split())


# ══════════════════════════════════════════════════════════════════════════════
# 1. The portal map
# ══════════════════════════════════════════════════════════════════════════════

_MAP = {"sig": None, "checked": 0.0, "pages": {}}
_MAP_LOCK = threading.Lock()


def _clean(s: str) -> str:
    s = re.sub(r"\{#.*?#\}", " ", s, flags=re.S)
    s = re.sub(r"\{%.*?%\}", " ", s, flags=re.S)
    s = re.sub(r"\{\{(.*?)\}\}", lambda m: " " + " / ".join(re.findall(r"'([^']{2,60})'", m.group(1))) + " "
               if re.search(r"'[^']{2,60}'", m.group(1)) and "url_for" not in m.group(1) else " … ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.replace("🔒", " ")
    s = re.sub(r"(\s*…\s*)+", " … ", s)
    return re.sub(r"\s+", " ", s).strip(" …")


def _sig() -> str:
    files = glob(os.path.join(_TPL, "portal", "*.html")) + [os.path.join(_HERE, "portal_routes.py")]
    try:
        return f"{len(files)}:{max(os.path.getmtime(f) for f in files):.0f}"
    except ValueError:
        return "0"


def _kwargs(argtext: str) -> dict:
    return dict(re.findall(r"(\w+)\s*=\s*['\"]([^'\"]*)['\"]", argtext or ""))


def _parse_menu(text: str) -> list:
    """Side-menu items in order: endpoint, url kwargs, label, menu path, plan lock key."""
    lines = text.splitlines()
    out, group, sub, sub_indent = [], None, None, -1
    stop = next((i for i, ln in enumerate(lines) if "Admin Panel" in ln and "sb-icon" in ln), len(lines))
    start = next((i for i, ln in enumerate(lines) if 'class="sb-icon"' in ln), 0)
    i = max(0, start - 3)
    while i < stop:
        ln = lines[i]
        indent = len(ln) - len(ln.lstrip())
        m = re.search(r'<span class="sb-icon">[^<]*</span>\s*([^<{]+)', ln)
        if m:
            group, sub, sub_indent = _clean(m.group(1)), None, -1
        m = re.match(r"^\s*<span>([^<]+)</span>\s*$", ln)
        if m:
            sub, sub_indent = _clean(m.group(1)), indent
        m = None
        if re.search(r"<a\b", ln):
            m = re.search(r"""<a\s[^>]*?href="\{\{\s*url_for\(\s*['"]([\w.]+)['"]([^)]*)\)\s*\}\}[^"]*\"""",
                          " ".join(lines[i:i + 4]))
        if m and not m.group(1).startswith("portal_admin"):
            if sub and indent <= sub_indent:
                sub, sub_indent = None, -1
            block, j = [], i
            while j < stop:
                block.append(lines[j])
                if "</a>" in lines[j]:
                    break
                j += 1
            raw = "\n".join(block)
            inner = raw.split(">", 1)[1] if ">" in raw else raw
            label = _clean(inner.split("</a>")[0])
            label = re.sub(r"[^\w)]+$", "", re.sub(r"^[^\w(]+", "", label)).strip()
            lock = re.search(r"""['"]([\w.:]+)['"]\s+not in granted_features""", raw)
            path = [p for p in (group, sub) if p]
            if label and (not path or path[-1] != label):
                path.append(label)
            if label:
                out.append({"endpoint": m.group(1), "kwargs": _kwargs(m.group(2)), "label": label,
                            "path": " › ".join(path), "lock": lock.group(1) if lock else None})
            i = j
        i += 1
    return out


_SEARCH_RE = re.compile(
    r"""\{\s*icon:'[^']*',\s*title:'((?:[^'\\]|\\.)*)',\s*desc:'((?:[^'\\]|\\.)*)',\s*"""
    r"""url:'\{\{\s*url_for\(\s*["']([\w.]+)["']([^)]*)\)\s*\}\}[^']*',\s*tags:'((?:[^'\\]|\\.)*)'""")


def _parse_search(text: str) -> list:
    return [{"title": html.unescape(t), "desc": html.unescape(d), "endpoint": ep, "kwargs": _kwargs(a), "tags": tg}
            for t, d, ep, a, tg in _SEARCH_RE.findall(text) if not ep.startswith("portal_admin")]


def _template_of(endpoint: str):
    view = current_app.view_functions.get(endpoint)
    if not view:
        return None
    try:
        src = inspect.getsource(inspect.unwrap(view))
    except (OSError, TypeError):
        return None
    m = re.search(r"""render_template\(\s*["']([\w/.-]+\.html)["']""", src)
    return m.group(1) if m else None


def _page_text(tpl: str) -> str:
    """Headings, buttons, field names and short help lines of one page template."""
    path = os.path.join(_TPL, tpl)
    try:
        s = open(path, encoding="utf-8").read()
    except OSError:
        return ""
    # "{# ask: … #}" notes: how a page works where its controls are drawn by code
    # (chart types, pop-ups). Never shown to customers; read here first (19 Nov 2026).
    notes = [" ".join(n.split()) for n in re.findall(r"\{#\s*ask:(.*?)#\}", s, flags=re.S)]
    s = re.sub(r"\{#.*?#\}", " ", s, flags=re.S)
    s = re.sub(r"<script\b.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style\b.*?</style>", " ", s, flags=re.S | re.I)

    def grab(pat, limit):
        seen, res = set(), []
        for x in re.findall(pat, s, flags=re.S | re.I):
            c = _clean(x)
            if 2 <= len(c) <= 160 and c.lower() not in seen:
                seen.add(c.lower()); res.append(c)
            if len(res) >= limit:
                break
        return res

    parts = ["How it works: " + " ".join(notes)] if notes else []
    for name, pat, lim in (("Headings", r"<h[1-4][^>]*>(.*?)</h[1-4]>", 14),
                           ("Buttons", r"<button[^>]*>(.*?)</button>", 18),
                           ("Links", r"<a[^>]*class=\"[^\"]*btn[^\"]*\"[^>]*>(.*?)</a>", 12),
                           ("Fields", r"<label[^>]*>(.*?)</label>", 18),
                           ("Help text", r"<p[^>]*>(.*?)</p>", 10)):
        got = grab(pat, lim)
        if got:
            parts.append(f"{name}: " + " | ".join(got))
    return "\n".join(parts)[:3200 if notes else 1600]


def _title_of(tpl: str) -> str:
    try:
        s = open(os.path.join(_TPL, tpl), encoding="utf-8").read()
    except OSError:
        return ""
    for pat in (r"<h1[^>]*>(.*?)</h1>", r"\{%\s*block title\s*%\}(.*?)\{%"):
        m = re.search(pat, s, flags=re.S | re.I)
        if m:
            t = re.sub(r"\s*[—|-]\s*PhiXtra.*$", "", _clean(m.group(1))).strip(" …")
            if len(t) >= 3:
                return t[:80]
    return ""


def _add_children(pages: dict):
    """Pages opened from inside a menu page (e.g. Integration › Xero, a quote request's own page)."""
    for parent in list(pages.values()):
        tpl = _template_of(parent["endpoint"])
        if not tpl:
            continue
        try:
            s = open(os.path.join(_TPL, tpl), encoding="utf-8").read()
        except OSError:
            continue
        for ep, args in re.findall(r"""href="\{\{\s*url_for\(\s*['"]([\w.]+)['"]([^)]*)\)""", s):
            if ep.startswith(("portal_admin", "static")) or any(p["endpoint"] == ep for p in pages.values()):
                continue
            rule = next((r for r in current_app.url_map.iter_rules() if r.endpoint == ep), None)
            if not rule or "GET" not in (rule.methods or set()):
                continue
            ctpl = _template_of(ep)
            title = (_title_of(ctpl) if ctpl else "") or ep.split(".")[-1].replace("_", " ").capitalize()
            pages[ep] = {"endpoint": ep, "kwargs": _kwargs(args), "label": title,
                         "path": f"{parent['path']} › {title} (opened from that page)", "lock": parent["lock"],
                         "desc": "", "tags": "", "parent": parent["endpoint"], "parent_kwargs": parent["kwargs"],
                         "parent_label": parent["label"]}


def portal_map() -> dict:
    """{key: page} built from the live templates; rebuilt when any portal template changes."""
    now = time.time()
    if _MAP["pages"] and now - _MAP["checked"] < 60:
        return _MAP["pages"]
    with _MAP_LOCK:
        _MAP["checked"] = now
        sig = _sig()
        if _MAP["pages"] and sig == _MAP["sig"]:
            return _MAP["pages"]
        try:
            text = open(_BASE, encoding="utf-8").read()
        except OSError:
            return _MAP["pages"]
        pages = {}
        for it in _parse_menu(text):
            key = it["endpoint"] + ("?" + "&".join(f"{k}={v}" for k, v in sorted(it["kwargs"].items())) if it["kwargs"] else "")
            if key not in pages:
                pages[key] = dict(it, desc="", tags="")
        for it in _parse_search(text):
            key = it["endpoint"] + ("?" + "&".join(f"{k}={v}" for k, v in sorted(it["kwargs"].items())) if it["kwargs"] else "")
            pg = pages.get(key) or next((p for k, p in pages.items() if p["endpoint"] == it["endpoint"]), None)
            if pg:
                pg["desc"] = (pg["desc"] + " " + it["desc"]).strip()
                pg["tags"] = (pg["tags"] + " " + it["tags"]).strip()
            else:
                pages[key] = {"endpoint": it["endpoint"], "kwargs": it["kwargs"], "label": it["title"],
                              "path": it["title"] + " (find it with Search features)", "lock": None,
                              "desc": it["desc"], "tags": it["tags"]}
        _add_children(pages)
        texts = {}
        for pg in pages.values():
            tpl = _template_of(pg["endpoint"])
            if tpl and tpl not in texts:
                texts[tpl] = _page_text(tpl)
            pg["text"] = texts.get(tpl, "") if tpl else ""
        for n, (k, pg) in enumerate(pages.items(), 1):
            pg["id"] = f"P{n}"
            pg["key"] = k
            pg["words"] = _words(" ".join([pg["label"], pg["path"], pg["desc"], pg["tags"]]))
            pg["twords"] = _words(pg["text"])
        _MAP.update(sig=sig, pages=pages)
        print(f"ℹ️ Ask PhiXtra: portal map built, {len(pages)} pages")
        return pages


# ══════════════════════════════════════════════════════════════════════════════
# 2. The person asking
# ══════════════════════════════════════════════════════════════════════════════

def _words(s: str) -> set:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (s or "").lower()):
        if len(w) < 3 or w in STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _can_open(endpoint: str) -> bool:
    if not session.get("team_member_id"):
        return True
    kind, keys = route_access(current_app.view_functions.get(endpoint))
    if kind == TEAM_ANY:
        return True
    if kind != TEAM:
        return False
    perms = session.get("team_member_permissions") or {}
    return any(perms.get(k) for k in keys)


def _page_for_endpoint(pages: dict, endpoint: str):
    return next((p for p in pages.values() if p["endpoint"] == endpoint), None)


def _relevant(pages: dict, question: str, endpoint: str, n: int = 8) -> list:
    q = _words(question)
    scored = []
    for pg in pages.values():
        s = 3 * len(q & pg["words"]) + len(q & pg["twords"])
        if s:
            scored.append((s, pg))
    scored.sort(key=lambda x: -x[0])
    out = [pg for _, pg in scored[:n]]
    here = _page_for_endpoint(pages, endpoint)
    if here and here not in out:
        out.insert(0, here)
    plans = _page_for_endpoint(pages, "portal.billing_plans")
    if plans and plans not in out and any(p["lock"] for p in out):
        out.append(plans)
    return out


def _best_score(pages: dict, question: str) -> int:
    q = _words(question)
    return max((3 * len(q & pg["words"]) + len(q & pg["twords"]) for pg in pages.values()), default=0)


def _plans_with(cur, key: str) -> list:
    cur.execute("""SELECT p.name FROM plans p JOIN plan_feature_grants g ON g.plan_id = p.id
                    WHERE g.feature_key = %s AND p.is_active = TRUE ORDER BY p.sort_order NULLS LAST, p.id""", (key,))
    return [r["name"] for r in cur.fetchall()][:6]


def _setup_status(tenant_id: int) -> list:
    """Short lines on what this business has connected / switched on. Never raises."""
    import portal_routes as PR
    lines = []
    try:
        ck = PR._setup_checklist(tenant_id)
        if ck:
            for st in ck.get("steps", []):
                lines.append(f"Set-up step '{st.get('title')}': {'done' if st.get('done') else 'not done'}"
                             + (f" ({_clean(str(st.get('text')))[:160]})" if st.get("text") else ""))
            for ex in ck.get("extras", []):
                lines.append(f"{ex.get('title')}: {'done' if ex.get('done') else 'not done'}"
                             + (f" ({_clean(str(ex.get('text')))[:160]})" if ex.get("text") else ""))
    except Exception as e:
        print("⚠️ Ask PhiXtra setup checklist:", e)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT t.domain, t.website_platform, t.chatbox_seen_at, t.signup_channel
                         FROM tenants t WHERE t.id = %s""", (tenant_id,))
        t = cur.fetchone() or {}
        lines.append(f"Website: {t.get('domain') or 'none saved'}"
                     + (f" ({t['website_platform']})" if t.get("website_platform") else ""))
        lines.append("Website chat box: " + (f"seen on the website, last {t['chatbox_seen_at']:%d %b %Y}"
                                              if t.get("chatbox_seen_at") else "never seen on a website yet"))
        cur.execute("SELECT 1 FROM wa_tenants WHERE tenant_id = %s AND active LIMIT 1", (tenant_id,))
        lines.append("WhatsApp number: " + ("connected" if cur.fetchone() else "not connected"))
        cur.execute("SELECT shop FROM shopify_shops WHERE tenant_id = %s AND uninstalled_at IS NULL LIMIT 1", (tenant_id,))
        r = cur.fetchone()
        if r:
            lines.append("Shopify: connected through the PhiXtra app")
    except Exception as e:
        conn.rollback()
        print("⚠️ Ask PhiXtra status:", e)
    finally:
        cur.close(); conn.close()
    for name, mod in (("Xero", "xero_link"), ("QuickBooks", "qbo_link"), ("Sage", "sage_link")):
        try:
            m = __import__(mod)
            link = m.link_for(tenant_id)
            if link and link.get("status") != "disconnected":
                lines.append(f"{name}: connected" + (f" ({link['status']})" if link.get("status") not in (None, "", "ok", "connected") else ""))
        except Exception:
            pass
    return lines


def _person(customer: dict, tenant_id: int, cands: list) -> dict:
    import portal_routes as PR
    granted = set()
    try:
        granted = PR._inject_granted_features().get("granted_features") or set()
    except Exception:
        pass
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    plan_name, plans_for = None, {}
    try:
        cur.execute("SELECT p.name FROM tenants t LEFT JOIN plans p ON p.id = t.plan_id WHERE t.id = %s", (tenant_id,))
        plan_name = (cur.fetchone() or {}).get("name")
        for pg in cands:
            if pg["lock"] and pg["lock"] not in granted and pg["lock"] not in plans_for:
                plans_for[pg["lock"]] = _plans_with(cur, pg["lock"])
    except Exception as e:
        conn.rollback()
        print("⚠️ Ask PhiXtra plan:", e)
    finally:
        cur.close(); conn.close()
    is_team = bool(session.get("team_member_id"))
    return {"name": (session.get("team_member_name") if is_team else (customer.get("first_name") or "")) or "",
            "is_team": is_team, "plan": plan_name or "no plan", "granted": granted, "plans_for": plans_for,
            "setup": _setup_status(tenant_id)}


# ══════════════════════════════════════════════════════════════════════════════
# 3. The answer
# ══════════════════════════════════════════════════════════════════════════════

def _prompt(question: str, here: str, cands: list, who: dict, business: str) -> str:
    rows = []
    for pg in cands:
        plan = "yes"
        if pg["lock"] and pg["lock"] not in who["granted"]:
            plan = "NO, not on their plan" + (f"; included in: {', '.join(who['plans_for'].get(pg['lock']) or [])}"
                                               if who["plans_for"].get(pg["lock"]) else "")
        rows.append(f"[{pg['id']}] {pg['path']}\n"
                    f"On their plan: {plan}. Their role may open it: {'yes' if _can_open(pg['endpoint']) else 'NO'}\n"
                    + (f"What it is: {pg['desc']}\n" if pg["desc"] else "")
                    + (pg["text"] + "\n" if pg["text"] else ""))
    return f"""You are "Ask PhiXtra", the help panel inside the PhiXtra customer portal.
You help the staff of the business "{business}" use the PhiXtra portal. You are NOT the sales AI Agent that talks to buyers.

Rules:
- Answer ONLY from the PORTAL MAP and the ACCOUNT facts below. Never invent a menu, page, button, setting, plan or price.
- Name menus and buttons exactly as written, in **bold**, with the menu path like **Settings › Team**.
- Give short numbered steps (at most 6), plain words, no jargon. One short line before the steps is fine.
- If the page they need says "On their plan: NO", that IS an answer (answered true): say it is not included in their current plan, name the plans that include it (only if listed), say the account owner can change plan in **Settings › Subscription Plans**, and set "link" to the Subscription Plans page if it is in the map.
- Only if a page says "Their role may open it: NO", tell them to ask the account owner and give no steps for it. Otherwise never mention asking the account owner.
- For "why isn't … working" questions, use the SET-UP STATUS facts.
- Never talk about buyers' chats or customer details; you cannot see them.
- If the map does not answer the question, set "answered" to false and say in one sentence that you don't have an answer yet.
- Reply in the language the question is written in.

Return JSON only: {{"answered": true|false, "answer": "text with numbered steps, \\n between lines", "link": "the [P…] id of the ONE page to open, or empty"}}

ACCOUNT
Person: {'a team member' if who['is_team'] else 'the account owner'}{(' named ' + who['name']) if who['name'] else ''}
Plan: {who['plan']}
Page they are on now: {here or 'unknown'}

SET-UP STATUS
{chr(10).join('- ' + s for s in who['setup']) or '- unknown'}

PORTAL MAP (only the pages that may matter)
{chr(10).join(rows) or '(nothing matched)'}

QUESTION
{question}"""


def _llm(prompt: str) -> dict:
    import ai_designer as D
    from openai import OpenAI
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("no AI key")
    client = OpenAI(api_key=key, timeout=TIMEOUT, max_retries=0)
    resp = client.chat.completions.create(
        model=os.getenv("ASK_PHIXTRA_MODEL", D.TEXT_MODEL),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"})
    return json.loads(resp.choices[0].message.content or "{}")


# ══════════════════════════════════════════════════════════════════════════════
# Storage: every question (rate limit); Admin shows only unanswered / not helpful
# ══════════════════════════════════════════════════════════════════════════════

_TABLES_OK = False


def ensure_tables():
    global _TABLES_OK
    if _TABLES_OK:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SET lock_timeout = '3s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS ask_phixtra_questions (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INT, customer_id INT, team_member_id INT,
            page TEXT, endpoint TEXT, question TEXT NOT NULL, answer TEXT,
            answered BOOLEAN, helpful SMALLINT, emailed BOOLEAN NOT NULL DEFAULT FALSE,
            dismissed BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("CREATE INDEX IF NOT EXISTS ask_phixtra_q_who ON ask_phixtra_questions (tenant_id, created_at)")
        conn.commit()
        _TABLES_OK = True
    finally:
        cur.close(); conn.close()


def _who_ids():
    import portal_routes as PR
    return PR._customer_id(), (int(session["team_member_id"]) if session.get("team_member_id") else None)


def _guard():
    import portal_routes as PR
    if PR._require_login():
        return None, None, (jsonify(ok=False, error="Please log in again."), 401)
    cust = PR._get_customer(PR._customer_id())
    if not cust:
        return None, None, (jsonify(ok=False, error="Please log in again."), 401)
    return cust, int(cust["tenant_id"]), None


def _safe_path(p: str) -> str:
    p = (p or "")[:300]
    return p if p.startswith("/") and not p.startswith("//") else ""


@ask_bp.route("/ask-phixtra/ask", methods=["POST"])
@any_team_member
def ask():
    cust, tenant_id, err = _guard()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()[:MAX_QUESTION]
    endpoint = (data.get("endpoint") or "")[:120]
    page = _safe_path(data.get("page"))
    if len(question) < 3:
        return jsonify(ok=False, error="Type your question first.")
    ensure_tables()
    cid, tm = _who_ids()
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""SELECT COUNT(*) FROM ask_phixtra_questions WHERE tenant_id = %s AND customer_id = %s
                    AND team_member_id IS NOT DISTINCT FROM %s AND created_at > NOW() - INTERVAL '1 hour'""",
                (tenant_id, cid, tm))
    used = cur.fetchone()[0]
    cur.close(); conn.close()
    if used >= HOURLY_LIMIT:
        return jsonify(ok=False, error="That's a lot of questions in one hour. Please try again a little later, "
                                        "or use Email PhiXtra Support.")

    pages = portal_map()
    cands = _relevant(pages, question, endpoint)
    if _best_score(pages, question) < 3:
        try:
            kw = _llm("Turn this question about a business software portal into 3 to 8 plain English search words "
                      "(menu and feature words). Return JSON {\"words\": \"...\"}.\nQuestion: " + question)
            extra = str(kw.get("words") or "")[:200]
            if extra:
                cands = _relevant(pages, question + " " + extra, endpoint)
        except Exception as e:
            print("⚠️ Ask PhiXtra keywords:", e)
    here = _page_for_endpoint(pages, endpoint)
    answered, answer, link = False, "", None
    try:
        who = _person(cust, tenant_id, cands)
        out = _llm(_prompt(question, here["path"] if here else page, cands, who,
                           cust.get("company_name") or "this business"))
        answered = bool(out.get("answered")) and bool((out.get("answer") or "").strip())
        answer = (out.get("answer") or "").strip()[:3000]
        pid = (out.get("link") or "").strip().strip("[]")
        pg = next((p for p in cands if p["id"] == pid), None)
        if pg and pg["lock"] and pg["lock"] not in who["granted"] and pg["endpoint"] != "portal.billing_plans":
            inc = who["plans_for"].get(pg["lock"]) or []
            answered = True
            answer = (f"**{pg['path'].split(' (opened')[0]}** is not included in your current plan ({who['plan']})."
                      + (f"\nIt is included in: {', '.join(inc)}." if inc else "")
                      + ("\nAsk the account owner to change plan in **Settings › Subscription Plans**."
                         if who["is_team"] else "\nYou can change plan in **Settings › Subscription Plans**."))
            pg = _page_for_endpoint(pages, "portal.billing_plans")
        elif pg and not _can_open(pg["endpoint"]):
            answered = True
            answer = (f"**{pg['path'].split(' (opened')[0]}** is not part of your team role, so you can't open it."
                      "\nAsk the account owner to do it, or to add it to your role in **Settings › Team Members**.")
            pg = None
        if answered and pg:
            for ep, kw, label in ((pg["endpoint"], pg["kwargs"], pg["label"]),
                                  (pg.get("parent"), pg.get("parent_kwargs") or {}, pg.get("parent_label"))):
                if not ep or not _can_open(ep):
                    continue
                try:
                    link = {"url": url_for(ep, **kw), "label": label}
                    break
                except Exception:
                    continue
    except Exception as e:
        print("⚠️ Ask PhiXtra answer:", e)
        answered, answer = False, ""
    if not answered:
        answer = answer or "I don't have an answer for this yet, so I don't want to guess. The PhiXtra team can check your account."

    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""INSERT INTO ask_phixtra_questions (tenant_id, customer_id, team_member_id, page, endpoint, question, answer, answered)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (tenant_id, cid, tm, page, endpoint, question, answer, answered))
    qid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, id=qid, answered=answered, answer=answer, link=link)


def _own_question(cur, qid: int, tenant_id: int):
    cur.execute("SELECT * FROM ask_phixtra_questions WHERE id = %s AND tenant_id = %s", (qid, tenant_id))
    return cur.fetchone()


@ask_bp.route("/ask-phixtra/feedback", methods=["POST"])
@any_team_member
def feedback():
    cust, tenant_id, err = _guard()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        qid, val = int(data.get("id")), (1 if int(data.get("helpful")) > 0 else -1)
    except (TypeError, ValueError):
        return jsonify(ok=False)
    ensure_tables()
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if _own_question(cur, qid, tenant_id):
        cur.execute("UPDATE ask_phixtra_questions SET helpful = %s WHERE id = %s", (val, qid))
        conn.commit()
    cur.close(); conn.close()
    return jsonify(ok=True)


@ask_bp.route("/ask-phixtra/email-support", methods=["POST"])
@any_team_member
def email_support():
    """Sends the question, the page and who asked to PhiXtra Support; replies go to the person."""
    import portal_routes as PR
    from portal_utils import send_email
    cust, tenant_id, err = _guard()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    ensure_tables()
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    row = None
    try:
        row = _own_question(cur, int(data.get("id")), tenant_id)
    except (TypeError, ValueError):
        pass
    question = (row["question"] if row else (data.get("question") or "")).strip()[:MAX_QUESTION]
    page = row["page"] if row else _safe_path(data.get("page"))
    if not question:
        cur.close(); conn.close()
        return jsonify(ok=False, error="Type your question first.")
    actor = PR._current_actor(cust)
    reply_to = None
    if actor.get("is_team"):
        try:
            cur.execute("SELECT email FROM team_members WHERE id = %s", (actor["team_member_id"],))
            reply_to = (cur.fetchone() or {}).get("email")
        except Exception:
            conn.rollback()
    reply_to = reply_to or cust.get("email")
    business = cust.get("company_name") or f"Business {tenant_id}"
    e = html.escape
    body = (f"<p><b>Ask PhiXtra question</b> from {e(actor.get('label') or '')} at <b>{e(business)}</b> "
            f"(account {tenant_id}).</p>"
            f"<p><b>Question:</b><br>{e(question)}</p>"
            f"<p><b>Page:</b> {e(request.host_url.rstrip('/') + page) if page else 'unknown'}</p>"
            + (f"<p><b>Ask PhiXtra answered:</b><br>{e(row['answer'] or '')}</p>" if row and row.get("answered") else "")
            + f"<p>Reply to this email to answer them ({e(reply_to or 'no email on file')}).</p>")
    ok = send_email(SUPPORT_EMAIL, f"Ask PhiXtra: question from {business}", body, reply_to=reply_to)
    if ok and row:
        cur.execute("UPDATE ask_phixtra_questions SET emailed = TRUE WHERE id = %s", (row["id"],))
        conn.commit()
    cur.close(); conn.close()
    if not ok:
        return jsonify(ok=False, error=f"The email didn't send. Please write to {SUPPORT_EMAIL}.")
    return jsonify(ok=True, message=f"Sent to PhiXtra Support. They will reply to {reply_to or 'your email'}.")


# ══════════════════════════════════════════════════════════════════════════════
# The panel's opening suggestions (by menu group of the current page)
# ══════════════════════════════════════════════════════════════════════════════

SUGGEST = {
    "Dashboard": ["What should I set up first?", "How do I put the chat box on my website?", "How do I connect my WhatsApp number?"],
    "Inbox": ["How do I take over a chat from the AI Agent?", "How do I reply in the buyer's language?", "What does the red number on Inbox mean?"],
    "Outreach": ["How do I connect my WhatsApp number?", "How do I send an email campaign?", "How do I send a WhatsApp broadcast?"],
    "CRM": ["How do I add a contact?", "How do I import my contacts?", "How do I move a deal in the Sales Pipeline?"],
    "Leads": ["How does lead scoring work?", "How do I find new leads?", "How do I move a lead to the Sales Pipeline?"],
    "Quotations": ["How do I send a quotation?", "How do I see quotes waiting for an answer?", "What happens when a buyer accepts?"],
    "Products & Orders": ["How do I add my products?", "How does my AI Agent learn my prices?", "How do I see my orders?"],
    "AI Agent": ["How do I change what my AI Agent says?", "How do I put the chat box on my website?", "How do I set my opening hours?"],
    "Reports": ["How do I build a report?", "How do I see quotes won this month?"],
    "Settings": ["How do I add a team member?", "How do I change my plan?", "How do I connect Xero?"],
}
DEFAULT_SUGGEST = ["What should I set up first?", "How do I add a team member?", "How do I connect my WhatsApp number?"]


@ask_bp.app_context_processor
def _ask_ctx():
    def ask_phixtra_suggestions():
        try:
            pg = _page_for_endpoint(portal_map(), request.endpoint or "")
            group = pg["path"].split(" › ")[0] if pg else None
            return SUGGEST.get(group, DEFAULT_SUGGEST)
        except Exception:
            return DEFAULT_SUGGEST
    return {"ask_phixtra_suggestions": ask_phixtra_suggestions}


# ══════════════════════════════════════════════════════════════════════════════
# Admin › Ask PhiXtra — unanswered and not-helpful questions only
# ══════════════════════════════════════════════════════════════════════════════

@ask_bp.route("/admin/ask-phixtra")
def admin_page():
    from portal_admin_routes import _require_admin
    r = _require_admin("customers", "view")
    if r:
        return r
    ensure_tables()
    show = request.args.get("show", "open")
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
        SELECT LOWER(TRIM(q.question)) AS k, MIN(q.question) AS question, COUNT(*) AS asked,
               MAX(q.created_at) AS last_at, BOOL_OR(q.answered IS NOT TRUE) AS no_answer,
               BOOL_OR(q.helpful = -1) AS not_helpful, BOOL_OR(q.emailed) AS emailed,
               (ARRAY_AGG(q.page ORDER BY q.created_at DESC))[1] AS page,
               (ARRAY_AGG(q.endpoint ORDER BY q.created_at DESC))[1] AS endpoint,
               (ARRAY_AGG(COALESCE(t.name, 'Business ' || q.tenant_id::text)
                          ORDER BY q.created_at DESC))[1] AS business,
               ARRAY_AGG(q.id) AS ids
          FROM ask_phixtra_questions q LEFT JOIN tenants t ON t.id = q.tenant_id
         WHERE (q.answered IS NOT TRUE OR q.helpful = -1)
           AND q.dismissed = {'TRUE' if show == 'dismissed' else 'FALSE'}
         GROUP BY 1 ORDER BY MAX(q.created_at) DESC LIMIT 300""")
    rows = cur.fetchall()
    cur.close(); conn.close()
    pages = portal_map()
    for r_ in rows:
        pg = _page_for_endpoint(pages, r_["endpoint"] or "")
        r_["where"] = pg["path"].split(" › ")[0] if pg else (r_["page"] or "")
    from portal_admin_routes import _disk_warning_ctx
    return render_template("portal/admin_ask_phixtra.html", rows=rows, show=show, map_size=len(pages),
                           **_disk_warning_ctx())


@ask_bp.route("/admin/ask-phixtra/dismiss", methods=["POST"])
def admin_dismiss():
    from portal_admin_routes import _require_admin
    r = _require_admin("customers", "edit")
    if r:
        return r
    ids = [int(x) for x in re.findall(r"\d+", request.form.get("ids", ""))][:500]
    undo = request.form.get("undo") == "1"
    if ids:
        ensure_tables()
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE ask_phixtra_questions SET dismissed = %s WHERE id = ANY(%s)", (not undo, ids))
        conn.commit(); cur.close(); conn.close()
    from flask import redirect
    return redirect(url_for("askphx.admin_page", show=("dismissed" if undo else "open")))
