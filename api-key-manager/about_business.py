"""Tell your AI Agent about your business (22 Nov 2026, mock-up Ezcv1MKrGfRY5Wc3KU7c51).

One page holds everything the AI Agent knows. It changes with the sign-up type
(WhatsApp only, other website, WooCommerce plugin, Shopify app) and with B2B
or B2C. Answers are kept in ai_business_profile (one JSON record per business)
and the AI service adds them to every answer (about_business_prompt.py).

The same answers are also written to the old Store Information records
(documents type 'store_info', fixed ids), because three other parts read those:
the AI's knowledge search, the Meta AI link and the social media post planner.
Only the screens moved; the records stay where they were.

tenants.ai_agent_ready: the AI Agent answers customers only once the business
presses "Turn on AI Agent" (existing businesses were set to ready, decision A1).
"""
import json

import psycopg2.extras

from about_business_prompt import DAYS, DEFAULT_TASKS, PAYMENT_OPTIONS, TASKS, pay_options, task_group

# Old Store Information sections the page writes to (ids store_info-<tenant>-<key>).
_MIRROR_KEYS = ("about_us", "delivery", "returns", "contact", "payment", "faqs", "custom")
_MIRROR_TITLES = {"about_us": "About Us", "delivery": "Delivery Information", "returns": "Returns & Refunds",
                  "contact": "Contact Information", "payment": "Payment Methods", "faqs": "FAQs",
                  "custom": "Other Information"}

LIMITS = {"business_name": 200, "what": 1500, "addr": 300, "moq": 200, "lead": 300, "del": 300, "book": 800,
          "canc": 300, "dl": 500, "rt": 500, "pay_other": 300, "else_text": 20000, "hour": 80, "q": 300, "a": 1200}


def ensure_tables(conn=None):
    """ai_business_profile + tenants.ai_agent_ready. Existing businesses are
    ready (they keep answering); new ones start not ready (column default FALSE)."""
    from db import get_db_connection
    own = conn is None
    conn = conn or get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SET LOCAL lock_timeout = '3s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS ai_business_profile (
                           tenant_id  INTEGER PRIMARY KEY,
                           data       JSONB   NOT NULL DEFAULT '{}'::jsonb,
                           updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           updated_by TEXT)""")
        cur.execute("""SELECT 1 FROM information_schema.columns
                        WHERE table_name='tenants' AND column_name='ai_agent_ready'""")
        if not cur.fetchone():
            cur.execute("ALTER TABLE tenants ADD COLUMN ai_agent_ready BOOLEAN NOT NULL DEFAULT TRUE")
            cur.execute("ALTER TABLE tenants ALTER COLUMN ai_agent_ready SET DEFAULT FALSE")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️ about_business.ensure_tables:", e)
    finally:
        cur.close()
        if own:
            conn.close()


def update_default_wording(conn=None):
    """"AI Agent" instead of "AI shopping assistant" in instructions that still
    use the standard sentences (22 Nov 2026). Only those exact sentences change."""
    from db import get_db_connection
    own = conn is None
    conn = conn or get_db_connection()
    cur = conn.cursor()
    pairs = [("You are a helpful AI shopping assistant for ", "You are the AI Agent for "),
             ("I'm your AI shopping assistant.", "I'm the AI Agent.")]
    try:
        cur.execute("SET LOCAL lock_timeout = '3s'")
        for old, new in pairs:
            for table, col in (("tenants", "system_prompt"), ("tenants", "web_system_prompt"),
                               ("tenant_agents", "system_prompt")):
                cur.execute(f"UPDATE {table} SET {col} = replace({col}, %s, %s) WHERE {col} LIKE %s",
                            (old, new, "%" + old + "%"))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️ about_business.update_default_wording:", e)
    finally:
        cur.close()
        if own:
            conn.close()


# ── Who is this business ─────────────────────────────────────────────────────
def business_info(cur, tenant_id: int) -> dict:
    """Sign-up type, B2B/B2C and the few facts the page needs."""
    cur.execute("""SELECT t.id, t.name, t.domain, t.signup_channel, t.source_type, t.website_platform,
                          t.is_demo, t.ai_enabled, COALESCE(t.ai_agent_ready, TRUE) AS ai_agent_ready,
                          t.system_prompt, t.web_system_prompt, COALESCE(p.slug, 'free') AS plan_slug
                     FROM tenants t LEFT JOIN plans p ON p.id = t.plan_id WHERE t.id = %s""", (tenant_id,))
    t = dict(cur.fetchone())
    cur.execute("SELECT 1 FROM shopify_shops WHERE tenant_id=%s AND uninstalled_at IS NULL LIMIT 1", (tenant_id,))
    shopify = cur.fetchone() is not None
    cur.execute("SELECT COALESCE(is_b2b, FALSE) AS b FROM company_profiles WHERE tenant_id=%s", (tenant_id,))
    r = cur.fetchone()
    b2b = bool(r and r["b"])
    web = t["signup_channel"] in ("website", "both") or t["source_type"] != "whatsapp" or shopify
    if shopify:
        kind = "shopify"
    elif not web:
        kind = "wa"
    elif (t["website_platform"] or "") == "wordpress":
        kind = "woo"
    else:
        kind = "custom"
    cur.execute("SELECT 1 FROM wa_tenants WHERE tenant_id=%s AND active LIMIT 1", (tenant_id,))
    t["wa_on"] = cur.fetchone() is not None
    # Fix 1 (22 Nov 2026): a WhatsApp-only business that has connected its
    # website and had it read at least once gets its products, services and
    # prices from the website, so they are no longer "Needed".
    cur.execute("SELECT 1 FROM website_sources WHERE tenant_id=%s AND last_read_at IS NOT NULL", (tenant_id,))
    t["site_read"] = cur.fetchone() is not None
    t.update(kind=kind, model="b2b" if b2b else "b2c", shopify=shopify)
    return t


def product_info(cur, tenant_id: int) -> dict:
    """How many products/services the AI Agent knows, a few names, and whether they're services."""
    cur.execute("""SELECT title, price_min, currency FROM documents
                    WHERE tenant_id=%s AND type='product' ORDER BY updated_at DESC NULLS LAST LIMIT 5""", (tenant_id,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*) AS n FROM documents WHERE tenant_id=%s AND type IN ('product','product_off')", (tenant_id,))
    n = int(cur.fetchone()["n"])
    try:
        cur.execute("""SELECT COUNT(*) FILTER (WHERE is_service) AS s, COUNT(*) FILTER (WHERE NOT is_service) AS p
                         FROM products WHERE tenant_id=%s""", (tenant_id,))
        c = cur.fetchone()
        services, goods = int(c["s"] or 0), int(c["p"] or 0)
    except Exception:
        cur.connection.rollback()
        services = goods = 0
    return {"count": n, "rows": rows, "services": services, "goods": goods}


def website_info(cur, tenant_id: int) -> dict | None:
    cur.execute("SELECT url, status, pages_read, last_read_at FROM website_sources WHERE tenant_id=%s", (tenant_id,))
    r = cur.fetchone()
    return dict(r) if r else None


# ── Saved answers ────────────────────────────────────────────────────────────
def _blank(info: dict, prod: dict) -> dict:
    model = info["model"]
    if info["kind"] in ("woo", "shopify"):
        sells = "products"
    elif prod["services"] and not prod["goods"]:
        sells = "services"
    else:
        sells = "products"
    group = task_group(model, sells)
    defaults_on = list(DEFAULT_TASKS[group])
    return {"business_name": info["name"] or "", "what": "", "sells": sells, "do": defaults_on, "bp": "",
            "hours": [""] * 7, "addr": "", "moq": "", "moq_none": False, "lead": "", "del": "",
            "book": "", "canc": "", "dl": "", "rt": "", "pay": {}, "pay_other": "", "faqs": [], "else_text": ""}


def _legacy_store_info(cur, tenant_id: int) -> dict:
    """Old Store Information text, to fill the page the first time it opens."""
    cur.execute("""SELECT id, title, content FROM documents WHERE tenant_id=%s AND type='store_info'
                    AND id NOT LIKE %s AND id NOT LIKE %s ORDER BY updated_at""",
                (tenant_id, f"store_info-{tenant_id}-upload-%", f"store_info-{tenant_id}-fix-%"))
    out = {}
    pre = f"store_info-{tenant_id}-"
    for r in cur.fetchall():
        out[r["id"][len(pre):] if r["id"].startswith(pre) else r["id"]] = (r["title"] or "", (r["content"] or "").strip())
    return out


def load(cur, tenant_id: int, info: dict, prod: dict) -> tuple[dict, bool]:
    """(answers, first_time). First time: filled from the old Store Information."""
    cur.execute("SELECT data FROM ai_business_profile WHERE tenant_id=%s", (tenant_id,))
    r = cur.fetchone()
    base = _blank(info, prod)
    if r and isinstance(r["data"], dict) and r["data"]:
        base.update({k: v for k, v in r["data"].items() if k in base and v is not None})
        base = _clean(base)
        _fit_to_business(base, info)
        return base, False
    old = _legacy_store_info(cur, tenant_id)
    take = lambda k: old.pop(k, ("", ""))[1]
    rest = []
    # Old text goes into the box the page will show for this business; anything
    # that doesn't fit, or has no box here, goes into "Anything else" in full.
    about = take("about_us") or take("about")
    base["what"] = about if len(about) <= LIMITS["what"] else ""
    if about and not base["what"]:
        rest.append("About us:\n" + about)
    shows_delivery = info["model"] == "b2c" and base["sells"] in ("products", "both")
    for key, field, title in (("delivery", "dl", "Delivery Information"), ("returns", "rt", "Returns & Refunds")):
        text = take(key)
        if text and shows_delivery and len(text) <= LIMITS[field]:
            base[field] = text
        elif text:
            rest.append(f"{title}:\n{text}")
    pay = take("payment")
    if pay and info["model"] == "b2c" and len(pay) <= LIMITS["pay_other"]:
        base["pay_other"] = pay
    elif pay:
        rest.append("Payment Methods:\n" + pay)
    for key, (title, text) in old.items():
        if text:
            rest.append(f"{title or key}:\n{text}")
    joined = "\n\n".join(rest)
    base["else_text"] = joined[:LIMITS["else_text"]]
    if len(joined) > LIMITS["else_text"]:
        base["_old_too_long"] = True   # then the old records are kept (see save)
    base["_from_store_info"] = bool(any([base["what"], base["dl"], base["rt"], base["pay_other"], base["else_text"]]))
    return base, True


def _clean(p: dict) -> dict:
    """Saved answers in the shapes the page expects (never trust old or odd data)."""
    for k in ("business_name", "what", "bp", "addr", "moq", "lead", "del", "book", "canc", "dl", "rt",
              "pay_other", "else_text"):
        if not isinstance(p.get(k), str):
            p[k] = "" if p.get(k) is None else str(p.get(k))
    if p.get("sells") not in ("products", "services", "both"):
        p["sells"] = "products"
    p["do"] = [x for x in p.get("do") if isinstance(x, str)] if isinstance(p.get("do"), list) else []
    hours = p.get("hours") if isinstance(p.get("hours"), list) else []
    p["hours"] = ([h if isinstance(h, str) else "" for h in hours] + [""] * 7)[:7]
    p["pay"] = {k: True for k, v in p.get("pay").items() if v} if isinstance(p.get("pay"), dict) else {}
    p["faqs"] = [{"q": str(f.get("q") or ""), "a": str(f.get("a") or "")}
                 for f in (p.get("faqs") if isinstance(p.get("faqs"), list) else []) if isinstance(f, dict)]
    p["moq_none"] = bool(p.get("moq_none"))
    return p


def _fit_to_business(p: dict, info: dict) -> None:
    """Woo/Shopify sell products; ticks must belong to this kind of business
    (e.g. an old B2B "quotes" tick, or B2C ticks after becoming B2B)."""
    if info["kind"] in ("woo", "shopify"):
        p["sells"] = "products"
    allowed = [k for k, _ in TASKS[task_group(info["model"], p["sells"])]]
    kept = [k for k in p["do"] if k in allowed]
    p["do"] = kept if (kept or not p["do"]) else list(DEFAULT_TASKS[task_group(info["model"], p["sells"])])


def from_form(form, info: dict, current: dict) -> dict:
    """Answers from the page's form, cleaned and cut to size."""
    g = lambda k: (form.get(k) or "").strip()[:LIMITS.get(k, 500)]
    p = dict(current)
    p.pop("_from_store_info", None)
    p.pop("_old_too_long", None)
    old_group = task_group(info["model"], current.get("sells") or "products")
    p["business_name"] = g("business_name")
    p["what"] = g("what")
    if info["kind"] in ("wa", "custom") and form.get("sells") in ("products", "services", "both"):
        p["sells"] = form.get("sells")
    elif info["kind"] in ("woo", "shopify"):
        p["sells"] = "products"
    group = task_group(info["model"], p["sells"])
    allowed = [k for k, _ in TASKS[group]]
    p["do"] = [k for k in form.getlist("do") if k in allowed]
    if group != old_group:
        # The offer changed (products / services / both): start from that kind's usual ticks.
        p["do"] = list(DEFAULT_TASKS[group])
    p["bp"] = form.get("bp") if form.get("bp") in ("list", "quote") else ""
    if "h0" in form:
        p["hours"] = [(form.get(f"h{i}") or "").strip()[:LIMITS["hour"]] for i in range(7)]
    # Only answers whose boxes were on the page change; hidden sections keep theirs.
    for k in ("addr", "moq", "lead", "del", "book", "canc", "dl", "rt", "pay_other", "else_text"):
        if k in form:
            p[k] = g(k)
    if "moq" in form:
        p["moq_none"] = form.get("moq_none") == "1"
    if form.get("pay_shown") == "1":
        p["pay"] = {k: True for k, _ in pay_options(p["sells"]) if form.get("pay_" + k) == "1"}
    qs, as_ = form.getlist("faq_q"), form.getlist("faq_a")
    p["faqs"] = [{"q": q.strip()[:LIMITS["q"]], "a": a.strip()[:LIMITS["a"]]}
                 for q, a in zip(qs, as_) if q.strip() or a.strip()][:60]
    p["model"] = info["model"]
    return p


# ── What is still needed (the red * fields) ──────────────────────────────────
def sections(info: dict, p: dict, prod: dict, web: dict | None, can_web: bool) -> list[dict]:
    """The page's sections for this business, each with status:
    auto (done for you), need (a * field is empty), added, opt (optional)."""
    kind, model, sells = info["kind"], info["model"], p.get("sells") or "products"
    svc = sells == "services"
    out = []

    do = set(p.get("do") or [])

    def add(key, title, sub, req=None, auto=False, show=True, hidden=False, show_if=None):
        """hidden: on the page but not shown until its box is ticked (show_if)."""
        if not show:
            return
        missing = [m for ok, m in (req or []) if not ok]
        st = "opt" if hidden else ("auto" if auto else ("need" if missing else ("added" if req else "opt")))
        out.append({"key": key, "title": title, "sub": sub, "status": st, "missing": [] if hidden else missing,
                    "hidden": hidden, "show_if": show_if})

    s = lambda k: bool((p.get(k) or "").strip()) if isinstance(p.get(k), str) else bool(p.get(k))
    about_req = [(s("business_name"), "Business name"), (s("what"), "What your business does"),
                 (bool(p.get("do")), "What your AI Agent should do")]
    if model == "b2b":
        about_req.append((p.get("bp") in ("list", "quote"), "What to do when a buyer asks for a price"))
    add("about", "About your business", "What you do and what your AI Agent should do", about_req)

    ptitle = {"services": "Your services and prices", "both": "Your products, services and prices"}.get(sells, "Your products and prices")
    one = {"services": "service", "both": "product or service"}.get(sells, "product")
    if kind == "wa" and info.get("site_read"):
        add("products", ptitle, "From your website or an uploaded list", auto=True)
    elif kind == "wa":
        add("products", ptitle, "The same list as My Products & Services, not a copy",
            [(prod["count"] > 0, "At least one " + one)])
    else:
        sub = {"custom": "From your website or an uploaded list", "woo": "From WooCommerce, through the PhiXtra plugin",
               "shopify": "From your Shopify store, through the PhiXtra app"}[kind]
        add("products", ptitle, sub, auto=prod["count"] > 0)

    if kind == "wa" and not info.get("site_read"):
        add("website", "Your website", "If you have one", show=can_web, auto=bool(web and web.get("last_read_at")))
    else:
        add("website", "Your website", "Read when you connected it", auto=bool(web and web.get("last_read_at")))

    hours_req = [(any((h or "").strip() for h in (p.get("hours") or [])), "Hours (at least one day)")]
    if svc:
        hours_req.append((s("addr"), "Address"))
    add("hours", "Opening hours and where you are" if svc else "Your team's hours and address",
        "So it can say when you're open and how to find you" if svc else "So it can say when your team will reply",
        hours_req)

    # If/then rules (22 Nov 2026): these sections follow the ticks in section 1.
    if model == "b2b":
        add("trade", "Trade terms", "Shown because you ticked \"Explain trade terms\"",
            [(bool(p.get("moq_none")) or s("moq"), "Minimum order"), (s("lead"), "Lead time"), (s("del"), "Where you deliver")],
            hidden="terms" not in do, show_if="terms")
    else:
        if sells in ("products", "both"):
            add("delivery", "Delivery and returns",
                "Shown because you ticked \"Explain delivery and returns\"" if kind == "wa"
                else "Your AI Agent also reads the delivery and returns pages on your website",
                [(s("dl"), "Delivery"), (s("rt"), "Returns")] if kind == "wa" else None,
                hidden="delivery" not in do, show_if="delivery")
        if sells in ("services", "both"):
            breq = []
            if "bookings" in do:
                breq.append((s("book"), "How customers book"))
            if sells == "services" and "cancellations" in do:
                breq.append((s("canc"), "Cancellation rule"))
            add("bookings", "Bookings and cancellations", "How customers book" + (" and your cancellation rule" if sells == "both" or "cancellations" in do else ""),
                breq or None)
    if model == "b2c":
        add("pay", "How customers can pay", "So it can answer questions about paying")
    add("faq", "Common questions", "Questions customers ask, with your answers")
    add("docs", "Documents", "Price lists, brochures, FAQs · PDF, Word, text, CSV")
    add("else", "Anything else your AI Agent should know", "Rules and things you don't do")
    # The page shows the "Done for you" sections first; number them in that order.
    out = [x for x in out if x["status"] == "auto"] + [x for x in out if x["status"] != "auto"]
    n = 0
    for x in out:
        if not x["hidden"]:
            n += 1
        x["n"] = n
    return out


def complete(secs: list[dict]) -> bool:
    return not any(x["status"] == "need" for x in secs)


# ── Saving ───────────────────────────────────────────────────────────────────
def _contact_text(p: dict) -> str:
    lines = []
    hrs = [f"{DAYS[i]}: {h}" for i, h in enumerate(p.get("hours") or []) if (h or "").strip()]
    if hrs:
        lines.append(("Opening hours" if p.get("sells") == "services" else "Team hours") + ":\n" + "\n".join(hrs))
    if (p.get("addr") or "").strip():
        lines.append("Address: " + p["addr"].strip())
    return "\n\n".join(lines)


def mirror_texts(p: dict) -> dict:
    """Text for each old Store Information section (empty = remove it)."""
    b2b = p.get("model") == "b2b"
    do = set(p.get("do") or [])
    sells = p.get("sells") or "products"
    pay = p.get("pay") or {}
    labels = dict(pay_options(sells))
    pay_words = ", ".join(labels[k] for k in labels if pay.get(k))
    if b2b:
        pay_text = ("Payment terms: " + p["payment_terms"]) if (p.get("payment_terms") or "").strip() else ""
        delivery = "\n".join(x for x in [
            ("Minimum order: no minimum" if p.get("moq_none") else ("Minimum order: " + p["moq"] if p.get("moq") else "")),
            ("Lead time: " + p["lead"]) if p.get("lead") else "", ("Delivers to: " + p["del"]) if p.get("del") else ""] if x) \
            if "terms" in do else ""
        returns = ""
    else:
        pay_text = "\n".join(x for x in [("We accept: " + pay_words) if pay_words else "", p.get("pay_other") or ""] if x)
        # Same if/then rules as the page and the AI Agent's instructions.
        ship = sells in ("products", "both") and "delivery" in do
        book_ok = sells in ("services", "both")
        canc_ok = sells == "both" or (sells == "services" and "cancellations" in do)
        delivery = "\n".join(x for x in [(p.get("dl") or "") if ship else "",
                                         ("How to book: " + p["book"]) if (book_ok and p.get("book")) else "",
                                         ("Cancellations: " + p["canc"]) if (canc_ok and p.get("canc")) else ""] if x)
        returns = (p.get("rt") or "") if ship else ""
    faqs = "\n\n".join(f"Q: {f['q']}\nA: {f['a']}" for f in p.get("faqs") or [] if f.get("q") and f.get("a"))
    about = "\n".join(x for x in [p.get("business_name") or "", p.get("what") or ""] if x)
    return {"about_us": about, "delivery": delivery, "returns": returns, "contact": _contact_text(p),
            "payment": pay_text, "faqs": faqs, "custom": p.get("else_text") or ""}


def save(conn, tenant_id: int, p: dict, actor: str, first_time: bool, keep_old: bool = False) -> None:
    cur = conn.cursor()
    cur.execute("""INSERT INTO ai_business_profile (tenant_id, data, updated_at, updated_by)
                   VALUES (%s, %s, NOW(), %s)
                   ON CONFLICT (tenant_id) DO UPDATE SET data=EXCLUDED.data, updated_at=NOW(), updated_by=EXCLUDED.updated_by""",
                (tenant_id, json.dumps(p), actor))
    for key, text in mirror_texts(p).items():
        doc_id = f"store_info-{tenant_id}-{key}"
        if text.strip():
            cur.execute("""INSERT INTO documents (id, tenant_id, type, title, content, updated_by, updated_at)
                           VALUES (%s, %s, 'store_info', %s, %s, %s, NOW())
                           ON CONFLICT (id) DO UPDATE SET content=EXCLUDED.content, title=EXCLUDED.title,
                                 embedding=NULL, updated_by=EXCLUDED.updated_by, updated_at=NOW()
                           WHERE documents.content IS DISTINCT FROM EXCLUDED.content""",
                        (doc_id, tenant_id, _MIRROR_TITLES[key], text, actor))
        elif not keep_old:
            cur.execute("DELETE FROM documents WHERE id=%s AND tenant_id=%s", (doc_id, tenant_id))
    if first_time and not keep_old:
        # The old own entries and older section names were shown in the page
        # (Anything else / What you do); from now on they live there only.
        cur.execute("""DELETE FROM documents WHERE tenant_id=%s AND type='store_info'
                        AND (id LIKE %s OR id = ANY(%s))""",
                    (tenant_id, f"store_info-{tenant_id}-entry-%",
                     [f"store_info-{tenant_id}-{k}" for k in ("about", "sizes")]))
    conn.commit()
    cur.close()


def corrections(cur, tenant_id: int) -> list[dict]:
    """"Fix this answer" corrections (store_info-<tenant>-fix-…)."""
    cur.execute("""SELECT id, title, content, updated_at FROM documents
                    WHERE tenant_id=%s AND type='store_info' AND id LIKE %s ORDER BY updated_at DESC""",
                (tenant_id, f"store_info-{tenant_id}-fix-%"))
    return [dict(r) for r in cur.fetchall()]


def documents(cur, tenant_id: int) -> list[dict]:
    cur.execute("""SELECT id, title, file_name, updated_at FROM documents
                    WHERE tenant_id=%s AND type='store_info' AND id LIKE %s ORDER BY updated_at DESC""",
                (tenant_id, f"store_info-{tenant_id}-upload-%"))
    return [dict(r) for r in cur.fetchall()]


def own_instructions(info: dict, has_active_agent: bool) -> list[dict]:
    """Hand-written instructions (decision D1), shown in Anything else so the
    business can still see and change them. The standard one is never shown.
    WhatsApp uses tenants.system_prompt (unless AI Agent Profiles are used);
    website chat uses tenants.web_system_prompt."""
    from portal_routes import _is_wizard_template_prompt
    custom = lambda t: bool(t) and not _is_wizard_template_prompt(t)
    out = []
    wa = (info.get("system_prompt") or "").strip()
    if not has_active_agent and custom(wa) and (info["kind"] == "wa" or info.get("wa_on")):
        out.append({"field": "own_instruction", "column": "system_prompt", "text": wa,
                    "label": "Your own instruction" if info["kind"] == "wa" else "Your own WhatsApp instruction"})
    web = (info.get("web_system_prompt") or "").strip()
    if info["kind"] != "wa" and custom(web):
        out.append({"field": "own_web_instruction", "column": "web_system_prompt", "text": web,
                    "label": "Your own website chat instruction"})
    return out


def is_ready_for_customers(tenant_id: int) -> bool | None:
    """True once the business turned its AI Agent on (None if unknown)."""
    from db import get_db_connection
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(ai_agent_ready, TRUE) OR COALESCE(is_demo, FALSE) FROM tenants WHERE id=%s", (tenant_id,))
        r = cur.fetchone()
        return bool(r[0]) if r else None
    except Exception:
        conn.rollback()
        return None
    finally:
        cur.close(); conn.close()


def setup_status(cur, tenant_id: int) -> dict:
    """For the Dashboard's Get set up box: is the step done, and what's missing."""
    info = business_info(cur, tenant_id)
    prod = product_info(cur, tenant_id)
    web = website_info(cur, tenant_id)
    p, _ = load(cur, tenant_id, info, prod)
    secs = sections(info, p, prod, web, True)
    ok = complete(secs)
    ready = bool(info["ai_agent_ready"] or info["is_demo"])
    return {"done": ok and ready, "complete": ok, "ready": ready, "kind": info["kind"],
            "needs": [x["title"] for x in secs if x["status"] == "need" and not x["hidden"]],
            "autos": [x["title"] for x in secs if x["status"] == "auto"],
            "minutes": 10 if info["kind"] == "wa" else (5 if info["kind"] == "custom" else 3)}
