"""
pricing_lines.py — the comparison table on phixtra.com/pricing, built from
the plans (25 Nov 2026, user: "how will the subscription plans on the
website update?").

Before: prices and every ✓ / — on the pricing page were typed into the
website's code a second time (page-phx-pricing.php + inc/phx-pricing-compare.php,
"Change both together"). Now:

  * Each LINE of the comparison lives here (Admin › Plans › Pricing page):
    the wording customers see, its link, its heading, and WHAT DECIDES IT —
      ticks   : ✓ when the plan has all of these Plan editor ticks
      limit   : a plan limit (team members, products …), "Unlimited" for -1
      support : the plan's own support line (Plan editor › Shown on the plan card)
    Admins never tick plans here, so the website can't disagree with what
    businesses really get.
  * The website reads /public/pricing.json (prices + both tables). It caches
    it and keeps the last good copy, so the page never breaks.
  * A line whose worked-out values differ from what the website shows today
    keeps the website's value until an admin either fixes the plan / line or
    presses "Use the plan values" — the website never changes by surprise.

The B2B table has one column per B2B plan. The Retail table has Free / Starter
/ Growth / Scale columns and a Website / WhatsApp / Website + WhatsApp switch,
each switch position reading a different plan.
"""
import json
import time

from db import get_db_connection

Y, N = "✓", "—"

TABLES = {
    "b2b": {
        "title": "B2B",
        "channels": ["all"],
        "columns": [("B2B Starter", {"all": "b2b_starter"}),
                    ("B2B Growth", {"all": "b2b"}),
                    ("B2B Scale", {"all": "b2b_scale"})],
    },
    "retail": {
        "title": "Retail",
        "channels": ["web", "wa", "both"],
        "columns": [("Free", {"web": "connect", "wa": "connect", "both": "connect"}),
                    ("Starter", {"web": "web_startup", "wa": "starter", "both": "startup_dual"}),
                    ("Growth", {"web": "web_business", "wa": "growth", "both": "business_dual"}),
                    ("Scale", {"web": "web_enterprise", "wa": "pro", "both": "enterprise_dual"})],
    },
}
CHANNEL_NAMES = {"all": "", "web": "Website", "wa": "WhatsApp", "both": "Website + WhatsApp"}

LIMIT_FIELDS = {
    "ai_messages_limit": ("AI messages / month", "num"),
    "staff_limit": ("Team members (staff seats)", "num"),
    "broadcasts_limit": ("WhatsApp broadcasts / month", "num"),
    "products_limit": ("Products", "num"),
    "photos_per_product": ("Photos per product", "num"),
    "ai_designs_limit": ("AI designs / month", "month"),
    "data_sources_limit": ("Excel product files", "num"),
}


def _ch(web, wa, both=None):
    return {"web": web, "wa": wa, "both": wa if both is None else both}


# Seed = the website as it is on 25 Nov 2026 (inc/phx-pricing-compare.php),
# each line given what decides it. site = what the website shows today.
_SEED = {
    "b2b": [
        ("Win more enquiries", [
            ("Never Miss an Enquiry", "enquiry-management-software", "ticks", ["inbox.page"], None, None, [Y, Y, Y]),
            ("Turn Website Visitors into Buyers", "ai-website-sales-agent", "ticks", ["store.website_connect"], None, None, [Y, Y, Y]),
            ("Sell in Their Language", "multilingual-sales-chat", "ticks", ["inbox.translate"], None, None, [N, Y, Y]),
            ("Win Back Abandoned Carts", "abandoned-cart-recovery", "ticks", ["woo.cart_recovery"], None, None, [N, Y, Y]),
            ("AI agent replies", "", "limit", [], "ai_messages_limit", None, ["Unlimited (fair use)"] * 3),
        ]),
        ("Close more deals", [
            ("Know Your Serious Buyers", "lead-qualification-software", "ticks", ["crm.ai_qualification"], None, None, [Y, Y, Y]),
            ("Quotes in Minutes", "quotation-software", "ticks", ["quotes.page"], None, None, [Y, Y, Y]),
            ("Follow Up Until They Buy", "automated-sales-follow-up", "ticks", ["leads.page"], None, None, [Y, Y, Y]),
            ("WhatsApp calls", "", "ticks", ["voice.calls"], None, None, [Y, Y, Y]),
        ]),
        ("Run your sales", [
            ("Every Deal in One Place", "free-crm", "ticks", ["crm.pipeline_board_view"], None, None, [Y, Y, Y]),
            ("See Every Sale Coming", "sales-dashboard", "ticks", ["reports.pipeline_overview"], None, None, [Y, Y, Y]),
            ("Companies, demos and Microsoft Bookings", "", "ticks", ["crm.companies_view", "crm.demos_view", "crm.bookings_connect"], None, None, [Y, Y, Y]),
            ("Custom report builder", "", "ticks", ["reports.custom"], None, None, [Y, Y, Y]),
        ]),
        ("Team and inbox", [
            ("Team members", "", "limit", [], "staff_limit", None, ["3", "10", "25"]),
            ("WhatsApp numbers", "", "limit", [], "staff_limit", "wa.connect_view", ["3", "10", "25"]),
            ("Shared team inbox", "", "ticks", ["inbox.claim_release"], None, None, [Y, Y, Y]),
            ("Import your WhatsApp chat history", "", "ticks", ["wa.history_import_view"], None, None, [Y, Y, Y]),
        ]),
        ("Marketing", [
            ("WhatsApp broadcasts", "", "limit", [], "broadcasts_limit", "legacy:feat_broadcasts", ["Unlimited"] * 3),
            ("WhatsApp message templates", "", "ticks", ["campaigns_wa.templates_view"], None, None, [Y, Y, Y]),
            ("Email campaigns and sequences", "", "ticks", ["legacy:feat_email_campaigns"], None, None, [Y, Y, Y]),
            ("Social media posts with AI designs", "", "limit", [], "ai_designs_limit", "social.posts_view", ["300 a month"] * 3),
        ]),
        ("Products and orders", [
            ("Products", "", "limit", [], "products_limit", None, ["Unlimited"] * 3),
            ("Photos per product", "", "limit", [], "photos_per_product", None, ["Unlimited"] * 3),
            ("Customers can shop by sending a photo", "", "ticks", ["legacy:feat_visual_match"], None, None, [Y, Y, Y]),
            ("Orders and discount settings", "", "ticks", ["ecom.orders_view", "ecom.discount_settings_view"], None, None, [Y, Y, Y]),
            ("Custom AI agent instructions", "", "ticks", ["ai.instructions_view"], None, None, [Y, Y, Y]),
        ]),
        ("Support", [
            ("Support", "", "support", [], None, None, ["Email", "Priority", "Priority + onboarding call"]),
        ]),
    ],
    "retail": [
        ("Win more sales", [
            ("AI agent replies a month", "", "limit", [], "ai_messages_limit", None,
             [N, _ch("1,000", "1,000", "1,500"), _ch("4,000", "4,000", "6,000"), _ch("12,000", "12,000", "20,000")]),
            ("Turn Website Visitors into Buyers", "ai-website-sales-agent", "ticks", ["store.website_connect"], None, None,
             [N, _ch(Y, N, Y), _ch(Y, N, Y), _ch(Y, N, Y)]),
            ("AI agent on your WhatsApp number", "", "ticks", ["wa.connect_view"], None, None,
             [N, _ch(N, Y), _ch(N, Y), _ch(N, Y)]),
            ("Win Back Abandoned Carts", "abandoned-cart-recovery", "ticks", ["woo.cart_recovery"], None, None,
             [N, N, _ch(Y, N, Y), _ch(Y, N, Y)]),
            ("Customers can shop by sending a photo", "", "ticks", ["legacy:feat_visual_match"], None, None, [N, N, N, Y]),
            ("WhatsApp calls", "", "ticks", ["voice.calls"], None, None, [N, N, N, _ch(N, Y)]),
        ]),
        ("Close and run your sales", [
            ("Follow Up Until They Buy", "automated-sales-follow-up", "ticks", ["leads.page"], None, None, [Y, Y, Y, Y]),
            ("Every Deal in One Place", "free-crm", "ticks", ["crm.pipeline_board_view"], None, None, [Y, Y, Y, Y]),
            ("Companies", "", "ticks", ["crm.companies_view"], None, None, [N, N, Y, Y]),
            ("Custom report builder", "", "ticks", ["reports.custom"], None, None, [N, _ch(Y, N, Y), Y, Y]),
        ]),
        ("Team and inbox", [
            ("Team members", "", "limit", [], "staff_limit", None, ["1", "1", "3", "10"]),
            ("WhatsApp numbers", "", "limit", [], "staff_limit", "wa.connect_view", ["1", _ch(N, "1"), _ch(N, "3"), _ch(N, "10")]),
            ("Shared team inbox", "", "ticks", ["inbox.claim_release"], None, None, [Y, Y, Y, Y]),
            ("Import your WhatsApp chat history", "", "ticks", ["wa.history_import_view"], None, None,
             [N, _ch(N, N, Y), _ch(N, Y), _ch(N, Y)]),
        ]),
        ("Marketing", [
            ("WhatsApp broadcasts a month", "", "limit", [], "broadcasts_limit", "legacy:feat_broadcasts",
             ["100", _ch(N, "500"), _ch(N, "5,000"), _ch(N, "Unlimited")]),
            ("WhatsApp message templates", "", "ticks", ["campaigns_wa.templates_view"], None, None,
             [Y, _ch(N, Y), _ch(N, Y), _ch(N, Y)]),
            ("Email campaigns and sequences", "", "ticks", ["legacy:feat_email_campaigns"], None, None, [N, N, Y, Y]),
            ("Social media posts with AI designs", "", "limit", [], "ai_designs_limit", "social.posts_view",
             [N, "30 a month", "100 a month", "300 a month"]),
        ]),
        ("Products and orders", [
            ("Products", "", "limit", [], "products_limit", None, ["50", "500", "2,000", "Unlimited"]),
            ("Photos per product", "", "limit", [], "photos_per_product", None, ["8", "8", "8", "Unlimited"]),
            ("Store sync (WooCommerce)", "", "ticks", ["ecom.woo_sync_view"], None, None, [N, _ch(Y, N, Y), _ch(Y, N, Y), _ch(Y, N, Y)]),
            ("Orders and discount settings", "", "ticks", ["ecom.orders_view", "ecom.discount_settings_view"], None, None,
             [N, _ch(N, N, Y), Y, Y]),
            ("Custom AI agent instructions", "", "ticks", ["ai.instructions_view"], None, None, [N, N, Y, Y]),
        ]),
        ("Support", [
            ("Support", "", "support", [], None, None, ["Help Center", "Email", "Priority email", "Priority + onboarding call"]),
        ]),
    ],
}
_SEED_NOTES = {
    "Custom AI agent instructions": "The Custom AI Instructions pages were removed on 22 Nov (now About your business › Anything else). Reword this line or hide it.",
}


def ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pricing_lines (
            id           SERIAL PRIMARY KEY,
            tbl          TEXT NOT NULL,            -- b2b | retail
            heading      TEXT NOT NULL,
            text         TEXT NOT NULL,
            link         TEXT NOT NULL DEFAULT '',
            kind         TEXT NOT NULL,            -- ticks | limit | support
            ticks        TEXT[] NOT NULL DEFAULT '{}',
            limit_field  TEXT,
            requires_key TEXT,
            site_vals    JSONB,                    -- what the website shows until accepted
            review_note  TEXT,
            hidden       BOOLEAN NOT NULL DEFAULT FALSE,
            sort         INTEGER NOT NULL DEFAULT 0,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by   TEXT
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pricing_feature_dismissed (
            feature_key TEXT PRIMARY KEY,
            at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            by_who      TEXT
        )""")
    cur.execute("SELECT COUNT(*) FROM pricing_lines")
    if cur.fetchone()[0] == 0:
        for tbl, heads in _SEED.items():
            sort = 0
            for heading, lines in heads:
                for text, link, kind, ticks, limit_field, req, site in lines:
                    sort += 10
                    cur.execute("""
                        INSERT INTO pricing_lines (tbl, heading, text, link, kind, ticks, limit_field,
                               requires_key, site_vals, review_note, sort, updated_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'seed')""",
                        (tbl, heading, text, link, kind, ticks, limit_field, req,
                         json.dumps(site), _SEED_NOTES.get(text), sort))


# ── Working out values ─────────────────────────────────────────────────────

def _plans(cur):
    cur.execute("""SELECT id, slug, name, price_ngn, price_usd, price_dollars, annual_discount_pct,
                          ai_messages_limit, staff_limit, broadcasts_limit, products_limit,
                          photos_per_product, ai_designs_limit, data_sources_limit, support_line,
                          feat_advanced_ai, feat_broadcasts, feat_email_campaigns, feat_visual_match,
                          feat_fw_checkout, is_recommended
                   FROM plans""")
    cols = [d[0] for d in cur.description]
    plans = {r[1]: dict(zip(cols, r)) for r in cur.fetchall()}
    cur.execute("SELECT p.slug, g.feature_key FROM plan_feature_grants g JOIN plans p ON p.id = g.plan_id")
    for slug, key in cur.fetchall():
        plans[slug].setdefault("_keys", set()).add(key)
    for p in plans.values():
        keys = p.setdefault("_keys", set())
        for col in ("feat_advanced_ai", "feat_broadcasts", "feat_email_campaigns", "feat_visual_match", "feat_fw_checkout"):
            if p.get(col):
                keys.add("legacy:" + col)
    return plans


def _fmt_num(v):
    if v is None:
        return N
    try:
        v = int(v)
    except (TypeError, ValueError):
        return str(v)
    if v < 0:
        return "Unlimited"
    if v == 0:
        return N
    return f"{v:,}"


def _value(line, plan):
    if not plan:
        return N
    keys = plan["_keys"]
    if line["requires_key"] and line["requires_key"] not in keys:
        return N
    if line["kind"] == "ticks":
        return Y if line["ticks"] and all(k in keys for k in line["ticks"]) else N
    if line["kind"] == "limit":
        v = _fmt_num(plan.get(line["limit_field"]))
        if v not in (N, "Unlimited") and LIMIT_FIELDS.get(line["limit_field"], ("", ""))[1] == "month":
            v += " a month"
        return v
    if line["kind"] == "support":
        return (plan.get("support_line") or N).replace(" support", "").strip() or N
    return N


def _norm_site(tbl, site):
    """site_vals → {channel: [v per column]}."""
    chans = TABLES[tbl]["channels"]
    out = {c: [] for c in chans}
    for v in site or []:
        for c in chans:
            out[c].append(v.get(c) if isinstance(v, dict) else v)
    return out


def _lines(cur, tbl=None):
    ensure_tables(cur)
    q = """SELECT id, tbl, heading, text, link, kind, ticks, limit_field, requires_key,
                  site_vals, review_note, hidden, sort, updated_at, updated_by
           FROM pricing_lines {} ORDER BY tbl, sort, id"""
    if tbl:
        cur.execute(q.format("WHERE tbl=%s"), (tbl,))
    else:
        cur.execute(q.format(""))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def table_view(cur, tbl, pending=frozenset(), removed=frozenset()):
    """Everything the Admin tab and the feed need for one table."""
    plans = _plans(cur)
    T = TABLES[tbl]
    rows = []
    for ln in _lines(cur, tbl):
        computed = {c: [_value(ln, plans.get(slugs[c])) for _n, slugs in T["columns"]] for c in T["channels"]}
        site = _norm_site(tbl, ln["site_vals"]) if ln["site_vals"] is not None else None
        differs = site is not None and site != computed
        issues = []
        if ln["review_note"]:
            issues.append(("r", ln["review_note"]))
        gone = [k for k in (ln["ticks"] or []) if k in removed]
        if gone:
            issues.append(("r", "Its tick left the portal: " + ", ".join(gone) + ". Pick other ticks or hide the line."))
        waiting = [k for k in (ln["ticks"] or []) if k in pending]
        if waiting:
            issues.append(("a", "Its tick is new and has no plans yet. Choose plans for it first (Plans page)."))
        if differs:
            issues.append(("a", "The plans say something different from the website today (see the two rows). "
                                "Fix the plan or this line, or press “Use the plan values”."))
        shown = site if (site is not None and (differs or waiting)) else computed
        rows.append(dict(ln, computed=computed, site=site, shown=shown, differs=differs, issues=issues))
    cols = [n for n, _s in T["columns"]]
    prices = []
    for _n, slugs in T["columns"]:
        entry = {c: _price(plans.get(slugs[c])) for c in T["channels"]}
        entry["_name"] = _n
        prices.append(entry)
    return {"tbl": tbl, "title": T["title"], "channels": T["channels"], "columns": cols,
            "rows": rows, "prices": prices}


def _price(p):
    if not p:
        return None
    return {"slug": p["slug"], "name": p["name"], "gbp": float(p["price_usd"] or 0),
            "usd": float(p["price_dollars"] or 0), "ngn": int(p["price_ngn"] or 0),
            "annual_discount_pct": int(p["annual_discount_pct"] or 0),
            "team_members": p["staff_limit"]}


def _catalog_sets(cur):
    try:
        import plan_catalog
        plan_catalog.ensure_tables(cur)
        cur.execute("SELECT feature_key, status FROM plan_catalog_state")
        rows = cur.fetchall()
        return (frozenset(k for k, s in rows if s == "pending"),
                frozenset(k for k, s in rows if s in ("removed", "cleaned")))
    except Exception:
        return frozenset(), frozenset()


# ── Public feed for phixtra.com/pricing ────────────────────────────────────

_FEED = {"at": 0, "body": None}


def feed():
    """Prices + both tables as the website should show them. Cached 60 s."""
    if _FEED["body"] and time.time() - _FEED["at"] < 60:
        return _FEED["body"]
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        pending, removed = _catalog_sets(cur)
        out = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tables": {}}
        for tbl in TABLES:
            v = table_view(cur, tbl, pending, removed)
            rows, last = [], None
            for r in v["rows"]:
                if r["hidden"]:
                    continue
                if r["heading"] != last:
                    rows.append({"heading": r["heading"]})
                    last = r["heading"]
                rows.append({"text": r["text"], "link": r["link"] or "", "values": r["shown"]})
            out["tables"][tbl] = {"columns": v["columns"], "channels": v["channels"], "prices": v["prices"], "rows": rows}
        conn.commit()
    finally:
        cur.close(); conn.close()
    body = json.dumps(out, ensure_ascii=False)
    _FEED.update(at=time.time(), body=body)
    return body


def clear_cache():
    _FEED["at"] = 0


# ── For Plan check / Admin ─────────────────────────────────────────────────

def issues(cur):
    pending, removed = _catalog_sets(cur)
    out = []
    for tbl in TABLES:
        for r in table_view(cur, tbl, pending, removed)["rows"]:
            if r["hidden"]:
                continue
            for _lvl, msg in r["issues"]:
                out.append({"table": TABLES[tbl]["title"], "line": r["text"], "issue": msg})
    return out


def unlisted_features(cur, catalog_entries):
    """Features decided in the last 30 days that no pricing line mentions and
    nobody chose to keep off the pricing page — the "show it?" prompt."""
    ensure_tables(cur)
    cur.execute("""SELECT feature_key FROM plan_catalog_state
                   WHERE decided_at > NOW() - INTERVAL '30 days' AND status='active'
                     AND COALESCE(decided_by,'') <> 'switch-over'""")
    recent = [r[0] for r in cur.fetchall()]
    if not recent:
        return []
    cur.execute("SELECT DISTINCT unnest(ticks) FROM pricing_lines")
    used = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT feature_key FROM pricing_feature_dismissed")
    gone = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT feature_key, COUNT(*) FROM plan_feature_grants WHERE feature_key = ANY(%s) GROUP BY 1", (recent,))
    n = dict(cur.fetchall())
    by_key = {e["key"]: e for e in catalog_entries}
    return [{"key": k, "label": f"{by_key[k]['page'] or ''} — {by_key[k]['action']}".strip(" —"),
             "plans": n.get(k, 0)} for k in recent if k not in used and k not in gone and k in by_key]
