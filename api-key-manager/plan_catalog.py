"""
plan_catalog.py — the Plan editor's list of ticks, built from the live portal.

Why (25 Nov 2026, user): Plan management never followed portal changes. Its
ticks were grouped and named by a hand-kept dict (PLAN_FEATURE_CATALOG), so a
removed or renamed page (Store Information → About your business) kept its old
group and name, and a new feature was switched on for every plan by itself.

How it works now:
  * Each tick names the PAGE it lives on (KEY_HOME below: key → the page's
    endpoint). The GROUP and PAGE NAME are then read from the side menu
    (templates/portal/base.html) when the portal starts, so renaming or moving
    a menu item moves its ticks in Plan management by itself.
  * PLAN_FEATURE_CATALOG only supplies the ACTION wording ("View",
    "Create a Company"). Page names inside old labels are not shown.
  * Every key is remembered in plan_catalog_state:
      - a key seen for the first time is PENDING: on no plan and hidden from
        every business (no menu item, no padlock, page closed) until an admin
        chooses plans for it;
      - a changed group or page name is recorded, so the editor labels it
        RENAMED / MOVED until that plan is next saved;
      - a key that leaves the portal is REMOVED (plans keep it until an
        admin presses Clean up).
  * check() lists everything out of step. deploy_portal.sh stops a reload when
    a menu page has no tick, or a tick has no page entry in KEY_HOME.

ADDING A FEATURE (the only steps):
  1. @team_feature("x.key", ...) on the route, and the key with its action
     wording in PLAN_FEATURE_CATALOG (portal_routes.py) — as before.
  2. One line in KEY_HOME: "x.key": "<endpoint of the menu page it lives on>".
  It then shows in Admin › Plans as NEW, hidden from businesses, until plans
  are chosen.
"""
import glob
import html
import os
import re
import threading
import time
from html.parser import HTMLParser

_HERE = os.path.dirname(os.path.abspath(__file__))
MENU_TEMPLATE = os.path.join(_HERE, "templates", "portal", "base.html")

# ── Where each tick lives: key → endpoint of its menu page ──────────────────
# None = a plan switch with no page of its own; it is listed under NO_PAGE_GROUP.
KEY_HOME = {
    # Dashboard / Inbox
    "dashboard.page": "portal.dashboard",
    "dashboard.handoff_handled": "portal.dashboard",
    "inbox.page": "portal.my_inbox",
    "inbox.reply": "portal.my_inbox",
    "inbox.claim_release": "portal.my_inbox",
    "inbox.resolve": "portal.my_inbox",
    "inbox.takeover": "portal.my_inbox",
    "inbox.manage_contact": "portal.my_inbox",
    "inbox.send_new": "portal.my_inbox",
    "inbox.see_all": "portal.my_inbox",
    "inbox.delete_chat": "portal.my_inbox",
    "inbox.translate": "portal.my_inbox",
    "wa.handoff_reports_view": "portal.whatsapp_reports",
    # Outreach › WhatsApp
    "legacy:feat_broadcasts": "portal.whatsapp_campaigns",
    "campaigns_wa.all_view": "portal.whatsapp_campaigns",
    "campaigns_wa.all_create": "portal.whatsapp_campaigns",
    "campaigns_wa.all_send": "portal.whatsapp_campaigns",
    "campaigns_wa.all_delete": "portal.whatsapp_campaigns",
    "campaigns_wa.segments_view": "portal.whatsapp_campaign_segments_page",
    "campaigns_wa.segments_create": "portal.whatsapp_campaign_segments_page",
    "campaigns_wa.segments_edit": "portal.whatsapp_campaign_segments_page",
    "campaigns_wa.segments_delete": "portal.whatsapp_campaign_segments_page",
    "campaigns_wa.templates_view": "portal.whatsapp_message_templates",
    "campaigns_wa.templates_create": "portal.whatsapp_message_templates",
    "campaigns_wa.templates_delete": "portal.whatsapp_message_templates",
    "campaigns_wa.reports_view": "portal.whatsapp_campaigns_reports",
    "wa.report": None,
    "campaigns_wa.needs_review_view": "portal.whatsapp_campaign_reviews",
    "campaigns_wa.needs_review_manage": "portal.whatsapp_campaign_reviews",
    "voice.calls": "calls.calls_page",
    "calls.answer": "calls.calls_page",
    "calls.make": "calls.calls_page",
    "calls.report": "calls.calls_page",
    "calls.settings": "calls.calls_settings_index",
    # Outreach › Email
    "legacy:feat_email_campaigns": "portal.email_campaigns",
    "campaigns_email.all_view": "portal.email_campaigns",
    "campaigns_email.all_create": "portal.email_new",
    "campaigns_email.all_edit": "portal.email_campaigns",
    "campaigns_email.all_send": "portal.email_campaigns",
    "campaigns_email.all_delete": "portal.email_campaigns",
    "campaigns_email.reports_view": "portal.email_campaigns",
    "campaigns_email.tracking": "portal.email_campaigns",
    "campaigns_email.ai_write": "portal.email_new",
    "campaigns_email.saved_see_all": "portal.email_saved_emails",
    "campaigns_email.segments_view": "portal.email_campaign_segments_page",
    "campaigns_email.segments_create": "portal.email_campaign_segments_page",
    "campaigns_email.segments_edit": "portal.email_campaign_segments_page",
    "campaigns_email.segments_delete": "portal.email_campaign_segments_page",
    "campaigns_email.sequences_view": "portal.email_sequences",
    "campaigns_email.sequences_create": "portal.email_sequences",
    "campaigns_email.sequences_edit": "portal.email_sequences",
    "campaigns_email.sequences_send": "portal.email_sequences",
    "campaigns_email.sequences_delete": "portal.email_sequences",
    "campaigns_email.mailbox_connect": "portal.channels_page",
    # Outreach › Social Media
    "social.posts_view": "socialcal.overview",
    "social.posts_create": "social.new_post",
    "social.posts_edit": "socialcal.overview",
    "social.posts_delete": "socialcal.overview",
    "social.posts_approve": "socialcal.approval",
    "social.analytics_view": "socialcal.analytics",
    "social.ai_plan": "socialcal.calendar",
    "social.ai_designs": "upload.start",
    "social.brand_kit_view": "social.brand_kit",
    "social.brand_kit_edit": "social.brand_kit",
    "social.usage_view": "social.usage",
    "channels.connect_buffer_view": "portal.channels_page",
    "channels.connect_buffer_manage": "portal.channels_page",
    "channels.connect_buffer_remove": "portal.channels_page",
    # CRM
    "crm.contacts_view": "portal.whatsapp_contacts",
    "crm.contacts_create": "portal.whatsapp_contacts",
    "crm.contacts_edit": "portal.whatsapp_contacts",
    "crm.contacts_delete": "portal.whatsapp_contacts",
    "crm.contacts_export": "portal.whatsapp_contacts",
    "crm.contacts_see_all": "portal.whatsapp_contacts",
    "crm.import": "portal.whatsapp_contacts",
    "crm.merge_review_view": "portal.whatsapp_contacts",
    "crm.merge_review_confirm": "portal.whatsapp_contacts",
    "crm.merge_review_reject": "portal.whatsapp_contacts",
    "crm.companies_view": "portal.crm_companies",
    "crm.companies_create": "portal.crm_companies",
    "crm.companies_edit": "portal.crm_companies",
    "crm.companies_delete": "portal.crm_companies",
    "reports.pipeline_overview": "portal.report_pipeline_overview",
    "crm.pipeline_board_view": "portal.sales_pipeline",
    "crm.pipeline_board_edit": "portal.sales_pipeline",
    "crm.pipeline_board_export": "portal.sales_pipeline",
    "crm.bookings_connect": "bookings.demos",
    "crm.demos_view": "bookings.demos",
    "crm.demos_all": "bookings.demos",
    "crm.targets_own": "portal.daily_targets",
    "crm.targets_team": "portal.daily_targets",
    "crm.targets_set": "portal.daily_targets",
    "crm.segments_view": "portal.whatsapp_segments",
    "crm.segments_create": "portal.whatsapp_segments",
    "crm.segments_edit": "portal.whatsapp_segments",
    "crm.segments_delete": "portal.whatsapp_segments",
    "crm.tags_view": "portal.lead_labels_page",
    "crm.tags_assign": "portal.lead_labels_page",
    "crm.tags_create": "portal.lead_labels_page",
    "crm.tags_edit": "portal.lead_labels_page",
    "crm.tags_delete": "portal.lead_labels_page",
    "crm.pipeline_settings_view": "portal.sales_pipeline_settings",
    "crm.pipeline_settings_edit": "portal.sales_pipeline_settings",
    # Leads
    "leads.page": "portal.leads_overview",
    "leads.create": "portal.leads_page",
    "leads.qualify": "portal.leads_page",
    "leads.assign": "portal.leads_page",
    "leads.see_all": "portal.leads_page",
    "crm.columns_own": "portal.leads_page",
    "crm.columns_create": "portal.leads_page",
    "crm.columns_edit": "portal.leads_page",
    "crm.columns_delete": "portal.leads_page",
    "crm.ai_qualification": "portal.lead_qual_dashboard",
    "leads.team_performance": "portal.team_performance",
    # Quotations
    "quotes.page": "portal.quotes_requests",
    "quotes.approve": "portal.quotes_requests",
    # Products & Orders
    "ecom.products_view": "portal.products",
    "ecom.products_create": "portal.products",
    "ecom.products_edit": "portal.products",
    "ecom.products_delete": "portal.products",
    "woo.product_recommendation": "portal.products",
    "woo.cross_selling": "portal.products",
    "legacy:feat_visual_match": "portal.products",
    "ecom.orders_view": "portal.orders",
    "ecom.orders_manage": "portal.orders",
    "ecom.orders_cancel": "portal.orders",
    "legacy:feat_fw_checkout": "portal.orders",
    "ecom.customers_view": "portal.customers",
    "ecom.woo_sync_view": "portal.woo_sync",
    "ecom.woo_sync_delete": "portal.woo_sync",
    "ecom.shopify_store_view": "portal.shopify_store",
    "ecom.shopify_store_sync": "portal.shopify_store",
    "ecom.data_sources_view": "portal.data_sources",
    "ecom.data_sources_create": "portal.data_sources",
    "ecom.data_sources_edit": "portal.data_sources",
    "ecom.data_sources_delete": "portal.data_sources",
    "ecom.discount_settings_view": "portal.wa_discount_settings",
    "ecom.discount_settings_edit": "portal.wa_discount_settings",
    "woo.message_templates_view": "portal.whatsapp_templates",
    "woo.message_templates_edit": "portal.whatsapp_templates",
    "woo.cart_recovery": "portal.cart_recovery_dashboard",
    "woo.cart_recovery_view": "portal.cart_recovery_dashboard",
    "woo.cart_recovery_edit": "portal.cart_recovery_dashboard",
    "woo.cart_recovery_templates_view": "portal.cart_recovery_email_template",
    "woo.cart_recovery_templates_edit": "portal.cart_recovery_email_template",
    # PhiXtra AI Agent
    "store.info": "about.page",
    "store.info_create": "about.page",
    "store.info_edit": "about.page",
    "store.info_delete": "about.page",
    "store.info_documents_upload": "about.page",
    "store.info_documents_delete": "about.page",
    "ai.instructions_view": "about.page",
    "ai.instructions_edit": "about.page",
    "store.website_connect": "portal.store_info_website_connect",
    "store.website_schedule": "portal.store_info_website_connect",
    "ai.api_keys_view": "portal.api_keys",
    "ai.api_keys_revoke": "portal.api_keys",
    "legacy:feat_advanced_ai": "portal.ai_agents",
    "ai.agent_profiles_view": "portal.ai_agents",
    "ai.agent_profiles_create": "portal.ai_agents",
    "ai.agent_profiles_edit": "portal.ai_agents",
    "ai.agent_profiles_delete": "portal.ai_agents",
    "ai.handoff_rules_view": "portal.handoff_rules",
    "ai.handoff_rules_create": "portal.handoff_rules",
    "ai.handoff_rules_edit": "portal.handoff_rules",
    "ai.handoff_rules_delete": "portal.handoff_rules",
    "ai.discounts_view": "portal.ai_discounts",
    "ai.discounts_edit": "portal.ai_discounts",
    "ai.discounts_send": "portal.ai_discounts",
    "woo.verified_specs_view": "portal.verified_specs_settings",
    "woo.verified_specs_create": "portal.verified_specs_settings",
    "woo.verified_specs_delete": "portal.verified_specs_settings",
    "woo.chat_archive_view": "portal.chat_archive",
    "woo.chat_archive_30days": "portal.chat_archive",
    "woo.chat_archive_unlimited": "portal.chat_archive",
    # Reports
    "reports.custom": "reports.wizard",
    "analytics.page": "portal.analytics",
    "reports.billing": "portal.report_billing",
    "reports.leads_sources": "portal.report_leads_sources",
    "reports.usage": "portal.report_usage",
    "reports.cart": "portal.report_cart",
    # Settings
    "settings.chatbox": "portal.settings_chatbox",
    "channels.page": "portal.channels_page",
    "channels.connect_messenger": "portal.channels_page",
    "wa.connect_view": "portal.whatsapp_connect",
    "wa.connect_manage": "portal.whatsapp_connect",
    "wa.connect_delete": "portal.whatsapp_connect",
    "wa.history_import_view": "portal.whatsapp_connect",
    "wa.history_import_create": "portal.whatsapp_connect",
    "wa.history_import_delete": "portal.whatsapp_connect",
    "team.manage": "portal.team_page",
    "team.members_create": "portal.team_create",
    "team.members_edit": "portal.team_page",
    "team.members_assign_role": "portal.team_page",
    "team.members_deactivate": "portal.team_page",
    "team.members_remove": "portal.team_page",
    "team.members_reset_password": "portal.team_page",
    "team.members_agent_access": "portal.team_page",
    "team.members_messenger_access": "portal.team_page",
    "team.members_webchat_access": "portal.team_page",
    "team.members_alerts": "portal.team_page",
    "team.roles_create": "portal.team_roles_page",
    "team.roles_edit": "portal.team_roles_page",
    "team.roles_delete": "portal.team_roles_page",
    "team.departments_create": "portal.team_departments_page",
    "team.departments_edit": "portal.team_departments_page",
    "team.departments_delete": "portal.team_departments_page",
    "team.positions_create": "portal.team_positions_page",
    "team.positions_edit": "portal.team_positions_page",
    "team.positions_delete": "portal.team_positions_page",
    "team.record_access_view": "portal.record_access",
    "billing.subscription_view": "portal.billing_plans",
    "billing.subscription_manage": "portal.billing_plans",
    "billing.credits_view": "portal.billing",
    "billing.credits_manage": "portal.billing",
    "billing.invoices_view": "portal.invoices",
    "billing.payment_gateways_view": "portal.payment_settings",
    "billing.payment_gateways_manage": "portal.payment_settings",
    "billing.payment_gateways_remove": "portal.payment_settings",
    "billing.payment_gateways_reveal_secret": "portal.payment_settings",
    "settings.account": "portal.settings",
    "settings.profile_edit": "portal.settings",
    "settings.business_edit": "portal.settings",
    "settings.password": "portal.settings",
    "settings.cancel_plan": "portal.settings",
    # Help (no page checks these keys any more)
    "help.tutorials": None,
    "help.videos": None,
}

NO_PAGE_GROUP = "Not on a menu page"

# Menu pages that are on every plan, or only PhiXtra staff see. Any OTHER
# menu page without a tick is an error in check() and stops a portal update.
PHIXTRA_ONLY_ENDPOINTS = {"leadfinder.page", "portal.sms_campaigns", "outreach.page",
                          "accounts.accounts_list"}
EVERY_PLAN_ENDPOINTS = {"reports.dashboards", "reports.library", "reports.custom_mine",
                        "reports.custom_shared"}

# Brand-new keys added by this change (25 Nov 2026). On their first start they
# are given to the plans that could already open those pages, so no business
# loses anything. Later new keys are PENDING instead (hidden until chosen).
SWITCHOVER_SEED = {
    "quotes.page": ("crm.ai_qualification",),
    "settings.chatbox": ("store.info", "store.website_connect"),
}

# Action wording where the old label named a page that no longer exists.
ACTION_OVERRIDES = {
    "store.info": "View",
    "store.info_create": "Add an answer or correction",
    "store.info_edit": "Edit",
    "store.info_delete": "Delete an answer or correction",
    "store.info_documents_upload": "Upload a document",
    "store.info_documents_delete": "Delete a document",
    "store.website_connect": "Connect / Sync / Delete Website",
    "store.website_schedule": "Automatic re-reading (daily / weekly / monthly)",
    "ai.instructions_view": "View (old AI Instruction page link)",
    "ai.instructions_edit": "Edit (old AI Instruction page link)",
    "woo.message_templates_view": "View",
    "woo.message_templates_edit": "Edit",
    "channels.page": "View",
    "billing.credits_view": "View",
    "legacy:feat_advanced_ai": "Custom AI Personality (plan unlock)",
    "voice.calls": "View calls and call back missed ones",
    "calls.settings": "Manage call settings for each WhatsApp number",
}


# ── Menu ────────────────────────────────────────────────────────────────────

_JINJA_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)
_URL_FOR = re.compile(r"url_for\(\s*['\"]([\w.]+)['\"]")


def _clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"[^\w&/+'’().,:\- ]", " ", text)   # drop emoji / lock icons
    return re.sub(r"\s+", " ", text).strip(" -")


def _expr_text(m):
    """{{ 'My Settings' if x else 'Account Settings' }} → the last literal
    (what the account owner sees); url_for(...) is kept for the parser."""
    inner = m.group(1)
    if "url_for" in inner:
        return m.group(0)
    lits = re.findall(r"""'([^']*)'|"([^"]*)\"""", inner)
    # Only "{{ 'A' if x else 'B' }}" wording — not counters or badges.
    if lits and re.match(r"\s*['\"]", inner) and " if " in inner:
        a, b = lits[-1]
        return a or b
    return ""


class _MenuParser(HTMLParser):
    """Walks the side menu: groups (.sb-group → .sb-gtitle), sub-menus
    (.sb-subgroup → .sb-subtoggle) and links (<a href="{{ url_for('x') }}">)."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.groups = []
        self.items = []
        self._grab = None
        self._buf = []
        self._link_ep = None
        self._span_depth = 0
        self._lock_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div":
            kind = None
            if re.search(r"(^|\s)sb-group(\s|$)", cls):
                kind = "group"
            elif "sb-subgroup" in cls.split():
                kind = "sub"
            self.stack.append(kind)
            if kind:
                self.groups.append(None)
            return
        if tag == "span" and "sb-lock" in cls:
            self._lock_depth += 1
            return
        if tag == "span" and "sb-gtitle" in cls and self.groups:
            self._grab, self._buf, self._span_depth = "gtitle", [], 1
            return
        if tag == "button" and "sb-subtoggle" in cls and self.groups:
            self._grab, self._buf = "subtitle", []
            return
        if tag == "span" and self._grab == "gtitle":
            self._span_depth += 1
        if tag == "a":
            m = _URL_FOR.search(a.get("href") or "")
            if m:
                self._grab, self._buf, self._link_ep = "link", [], m.group(1)

    def handle_endtag(self, tag):
        if tag == "div" and self.stack:
            if self.stack.pop():
                self.groups.pop()
            return
        if tag == "span" and self._lock_depth:
            self._lock_depth -= 1
            return
        if tag == "span" and self._grab == "gtitle":
            self._span_depth -= 1
            if self._span_depth <= 0:
                self.groups[-1] = _clean("".join(self._buf))
                self._grab = None
            return
        if tag == "button" and self._grab == "subtitle":
            self.groups[-1] = _clean("".join(self._buf))
            self._grab = None
            return
        if tag == "a" and self._grab == "link":
            label = _clean("".join(self._buf))
            path = [g for g in self.groups if g]
            self.items.append({"endpoint": self._link_ep, "label": label,
                               "group": " › ".join(path) if path else label,
                               "order": len(self.items)})
            self._grab = None

    def handle_data(self, data):
        if self._grab and not self._lock_depth:
            self._buf.append(data)

    def handle_entityref(self, name):
        self.handle_data(html.unescape("&%s;" % name))

    def handle_charref(self, name):
        self.handle_data(html.unescape("&#%s;" % name))


def parse_menu(path=None):
    """[{endpoint, label, group, order}] in menu order. The first link to an
    endpoint wins; admin and log-out links are left out."""
    src = open(path or MENU_TEMPLATE, encoding="utf-8").read()
    start = src.find("sb-group")
    end = src.find("</nav>", start)
    src = src[max(0, start - 2000): end if end > 0 else len(src)]
    src = re.sub(r"\{%.*?%\}|\{#.*?#\}", "", src, flags=re.S)
    src = _JINJA_EXPR.sub(_expr_text, src)
    p = _MenuParser()
    p.feed(src)
    seen, out = set(), []
    for it in p.items:
        ep = it["endpoint"]
        if ep in seen or not it["label"] or ep == "portal.logout" or ep.startswith("portal_admin."):
            continue
        seen.add(ep)
        it["order"] = len(out)
        out.append(it)
    return out


# ── Building the list ──────────────────────────────────────────────────────

# Sub-features that sit on another page: their "X — view" keeps the X.
_KEEP_FULL_LABEL = {"crm.merge_review_view", "campaigns_email.reports_view", "wa.history_import_view"}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _action_from_label(key, label, page=None):
    """The tick's wording without the page name (the page name comes from the
    menu). Only a leading page name is dropped: "Companies — view" on the
    Companies page → "View"; "Chat Translation — read and reply…" keeps its
    feature name."""
    if key in ACTION_OVERRIDES:
        return ACTION_OVERRIDES[key]
    if " — " in label and page and key not in _KEEP_FULL_LABEL:
        pre, rest = label.split(" — ", 1)
        if _norm(pre) == _norm(page) or rest.strip().lower() == "view":
            rest = rest.strip()
            return rest[:1].upper() + rest[1:]
    return label


def _route_keys(app):
    from feature_access import route_access, TEAM
    out = {}
    for ep, fn in app.view_functions.items():
        kind, keys = route_access(fn)
        if kind == TEAM and keys:
            out[ep] = tuple(keys)
    return out


def _code_mentions(keys, code_paths):
    """Keys the code still uses, outside the catalog lists themselves. A legacy
    key counts when its plans column (feat_x) is used."""
    wanted = {}
    for k in keys:
        wanted[k] = k
        if k.startswith("legacy:"):
            wanted[k.split(":", 1)[1]] = k
    pat = re.compile(r"""["']([a-z_]+(?:\.[a-z_0-9]+)?|legacy:feat_[a-z_]+)["']""")
    found = set()
    for pattern in code_paths:
        for path in glob.glob(pattern, recursive=True):
            if ".bak" in path or path.endswith(("portal_migrations.py", "plan_catalog.py")):
                continue
            try:
                src = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if path.endswith("portal_routes.py"):
                for name in ("PLAN_FEATURE_CATALOG = {", "ROLE_FORM_GRID = {", "PLAN_ONLY_FEATURE_KEYS = {"):
                    i = src.find(name)
                    if i >= 0:
                        j = src.find("\n}\n", i)
                        src = src[:i] + src[j:]
            for tok in pat.findall(src):
                if tok in wanted:
                    found.add(wanted[tok])
    return found


_CODE_CACHE = {"at": 0, "keys": None, "found": set()}


def build(app, catalog, code_paths=None):
    """The editor list as the portal is RIGHT NOW (no database).

    entries (menu order): key, action, page, group, endpoint, has_page,
    in_menu, in_code, also (other menu pages the key opens), old_module, old_label
    """
    menu = parse_menu()
    by_ep = {m["endpoint"]: m for m in menu}
    route_keys = _route_keys(app)
    key_pages = {}
    for m in menu:
        for k in route_keys.get(m["endpoint"], ()):
            key_pages.setdefault(k, []).append(m["label"])
    flat = [(mod, k, label) for mod, feats in catalog.items() for k, label in feats]
    keys = {k for _, k, _ in flat}
    if code_paths:
        if _CODE_CACHE["keys"] != keys or time.time() - _CODE_CACHE["at"] > 600:
            _CODE_CACHE.update(at=time.time(), keys=keys, found=_code_mentions(keys, code_paths))
        in_code = _CODE_CACHE["found"]
    else:
        in_code = keys
    entries = []
    for i, (mod, k, label) in enumerate(flat):
        declared = k in KEY_HOME
        ep = KEY_HOME.get(k)
        home = by_ep.get(ep) if ep else None
        page_name = home["label"] if home else None
        entries.append({
            "key": k,
            "action": _action_from_label(k, label, page_name),
            "page": page_name,
            "group": home["group"] if home else NO_PAGE_GROUP,
            "endpoint": ep,
            "declared": declared,
            "has_page": bool(ep),
            "in_menu": bool(home),
            "in_code": k in in_code,
            "also": [p for p in key_pages.get(k, []) if p != page_name],
            "old_module": mod,
            "old_label": label,
            "order": (home["order"] if home else 100000, i),
        })
    entries.sort(key=lambda e: e["order"])
    return entries, menu


# ── Database state ─────────────────────────────────────────────────────────

def ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS plan_catalog_state (
            feature_key  TEXT PRIMARY KEY,
            grp          TEXT,
            page         TEXT,
            action       TEXT,
            status       TEXT NOT NULL DEFAULT 'active',   -- active | pending | removed
            first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            prev_grp     TEXT,
            prev_page    TEXT,
            changed_at   TIMESTAMPTZ,
            is_new       BOOLEAN NOT NULL DEFAULT FALSE,
            removed_at   TIMESTAMPTZ,
            removed_why  TEXT,
            decided_at   TIMESTAMPTZ,
            decided_by   TEXT
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS plan_editor_saves (
            plan_id  INTEGER PRIMARY KEY,
            saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")


def _readonly():
    return os.getenv("PLAN_CATALOG_READONLY") == "1"


def sync_state(app, catalog, get_db_connection, code_paths=None):
    """Called once at portal start. Records new / renamed / moved / removed
    keys. Returns a short summary for the log."""
    entries, _menu = build(app, catalog, code_paths)
    if _readonly():
        return {"entries": len(entries)}
    conn = get_db_connection()
    if not conn:
        return {}
    summary = {"new": [], "changed": [], "removed": [], "back": []}
    try:
        cur = conn.cursor()
        # Two portal workers start together: one does the work, the other waits.
        cur.execute("SELECT pg_advisory_xact_lock(7311025)")
        ensure_tables(cur)
        cur.execute("SELECT feature_key, grp, page, status FROM plan_catalog_state")
        state = {r[0]: {"grp": r[1], "page": r[2], "status": r[3]} for r in cur.fetchall()}
        switchover = not state
        live = {}
        for e in entries:
            gone = not e["in_code"]
            live[e["key"]] = e
            old = state.get(e["key"])
            if old is None:
                if e["key"] in SWITCHOVER_SEED:
                    # Give it to every plan that could already open the page.
                    needs = SWITCHOVER_SEED[e["key"]]
                    cur.execute("""
                        INSERT INTO plan_feature_grants (plan_id, feature_key)
                        SELECT g.plan_id, %s FROM plan_feature_grants g
                        WHERE g.feature_key = ANY(%s)
                        GROUP BY g.plan_id HAVING COUNT(DISTINCT g.feature_key) = %s
                        ON CONFLICT DO NOTHING""", (e["key"], list(needs), len(needs)))
                    cur.execute("""
                        INSERT INTO plan_catalog_state (feature_key, grp, page, action, status, is_new,
                               changed_at, decided_at, decided_by)
                        VALUES (%s,%s,%s,%s,'active',TRUE,NOW(),NOW(),'switch-over')""",
                        (e["key"], e["group"], e["page"], e["action"]))
                    summary["new"].append(e["key"])
                    continue
                if switchover:
                    # First run: what the old editor showed becomes the "was".
                    # Renamed = the old editor showed a page name this tick no longer carries.
                    pre = e["old_label"].split(" — ", 1)[0] if " — " in e["old_label"] else None
                    renamed = bool(e["page"]) and e["action"] != e["old_label"] and \
                        (pre is None or _norm(pre) != _norm(e["page"]))
                    old_page = e["old_label"] if renamed else None
                    changed = (e["group"] != e["old_module"]) or renamed
                    cur.execute("""
                        INSERT INTO plan_catalog_state (feature_key, grp, page, action, status,
                               prev_grp, prev_page)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (e["key"], e["group"], e["page"], e["action"],
                         "removed" if gone else "active",
                         e["old_module"], old_page))
                    if changed or gone:
                        cur.execute("UPDATE plan_catalog_state SET changed_at=NOW() WHERE feature_key=%s", (e["key"],))
                    if gone:
                        cur.execute("UPDATE plan_catalog_state SET removed_at=NOW(), removed_why=%s WHERE feature_key=%s",
                                    ("No page or code uses this tick any more", e["key"]))
                    continue
                cur.execute("""
                    INSERT INTO plan_catalog_state (feature_key, grp, page, action, status, is_new,
                           changed_at)
                    VALUES (%s,%s,%s,%s,'pending',TRUE,NOW())""",
                    (e["key"], e["group"], e["page"], e["action"]))
                summary["new"].append(e["key"])
                continue
            if gone and old["status"] != "removed":
                cur.execute("""UPDATE plan_catalog_state SET status='removed', removed_at=NOW(),
                               removed_why=%s, changed_at=NOW() WHERE feature_key=%s""",
                            ("No page or code uses this tick any more", e["key"]))
                summary["removed"].append(e["key"])
                continue
            if not gone and old["status"] == "removed":
                cur.execute("""UPDATE plan_catalog_state SET status='active', removed_at=NULL,
                               removed_why=NULL WHERE feature_key=%s""", (e["key"],))
                summary["back"].append(e["key"])
            if (old["grp"], old["page"]) != (e["group"], e["page"]):
                cur.execute("""UPDATE plan_catalog_state SET prev_grp=%s, prev_page=%s, grp=%s, page=%s,
                               changed_at=NOW() WHERE feature_key=%s""",
                            (old["grp"], old["page"], e["group"], e["page"], e["key"]))
                summary["changed"].append(e["key"])
            cur.execute("UPDATE plan_catalog_state SET action=%s, last_seen=NOW() WHERE feature_key=%s",
                        (e["action"], e["key"]))
        for k, old in state.items():
            if k not in live and old["status"] != "removed":
                cur.execute("""UPDATE plan_catalog_state SET status='removed', removed_at=NOW(),
                               removed_why=%s, changed_at=NOW() WHERE feature_key=%s""",
                            ("Taken out of the portal", k))
                summary["removed"].append(k)
        conn.commit()
        cur.close()
    finally:
        conn.close()
    _PENDING["at"] = 0
    return summary


# ── Pending keys: hidden from businesses until plans are chosen ─────────────

_PENDING = {"at": 0, "keys": frozenset()}
_PENDING_LOCK = threading.Lock()


def pending_keys():
    """Keys nobody has chosen plans for yet (refreshed every 30 s)."""
    if time.time() - _PENDING["at"] < 30:
        return _PENDING["keys"]
    with _PENDING_LOCK:
        if time.time() - _PENDING["at"] < 30:
            return _PENDING["keys"]
        keys = frozenset()
        try:
            from db import get_db_connection
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('plan_catalog_state')")
            if cur.fetchone()[0]:
                cur.execute("SELECT feature_key FROM plan_catalog_state WHERE status='pending'")
                keys = frozenset(r[0] for r in cur.fetchall())
            cur.close(); conn.close()
        except Exception as e:
            print("⚠️ plan_catalog.pending_keys:", e)
            keys = _PENDING["keys"]
        _PENDING.update(at=time.time(), keys=keys)
        return keys


def endpoint_hidden(app, endpoint):
    """True when every tick on this page is still pending — the page is not
    shown to any business yet (PhiXtra staff opening an account still see it)."""
    pend = pending_keys()
    if not pend or not endpoint:
        return False
    from feature_access import route_access, TEAM
    kind, keys = route_access(app.view_functions.get(endpoint))
    return kind == TEAM and bool(keys) and all(k in pend for k in keys)


def mark_decided(cur, keys, who):
    if keys:
        cur.execute("""UPDATE plan_catalog_state SET status='active', decided_at=NOW(), decided_by=%s
                       WHERE feature_key = ANY(%s) AND status='pending'""", (who, list(keys)))
        _PENDING["at"] = 0


# ── What the Admin screens show ────────────────────────────────────────────

GROUP_COLOURS = ['#F04438', '#F79009', '#EAAA08', '#12B76A', '#15B79E', '#0BA5EC',
                 '#2E90FA', '#6172F3', '#7A5AF8', '#7C3AED', '#9E77ED', '#EE46BC',
                 '#F63D68', '#DC6803', '#667085']


def editor_groups(app, catalog, cur, plan_id=None, granted=None, code_paths=None):
    """Groups for the Plan editor's Features card, in menu order.

    Each group: name, colour, pages:[{name, also, items:[...]}], counts.
    Each item: key, action, checked, pill (new|renamed|moved|None), note, pending.
    removed: [{key, action, page, why, when}] — shown crossed out, not ticked.
    """
    entries, _menu = build(app, catalog, code_paths)
    ensure_tables(cur)
    cur.execute("""SELECT feature_key, status, prev_grp, prev_page, changed_at, is_new,
                          removed_at, removed_why FROM plan_catalog_state""")
    st = {r[0]: r for r in cur.fetchall()}
    saved_at = None
    if plan_id:
        cur.execute("SELECT saved_at FROM plan_editor_saves WHERE plan_id=%s", (plan_id,))
        row = cur.fetchone()
        saved_at = row[0] if row else None
    granted = granted or set()
    groups, by_name, removed = [], {}, []
    counts = {"new": 0, "renamed": 0, "moved": 0, "removed": 0, "pending": 0}
    for e in entries:
        s = st.get(e["key"])
        status = s[1] if s else "pending"
        if status == "removed":
            removed.append({"key": e["key"], "action": e["action"], "page": e["page"] or e["old_module"],
                            "why": (s[7] if s else ""), "when": (s[6] if s else None),
                            "checked": e["key"] in granted})
            counts["removed"] += 1
            continue
        changed_at = s[4] if s else None
        fresh = changed_at is not None and (saved_at is None or changed_at > saved_at)
        pill, note = None, ""
        if s and (s[5] or status == "pending") and fresh:
            pill = "new"
            note = "Hidden from every business until you choose plans" if status == "pending" else ""
        elif fresh and s:
            prev_grp, prev_page = s[2], s[3]
            if prev_grp and prev_grp != e["group"]:
                pill, note = "moved", f"Moved from “{prev_grp}”"
            if prev_page and e["page"] and prev_page != e["page"]:
                if pill is None:
                    pill = "renamed"
                note = (note + " · " if note else "") + f"Was “{prev_page}”"
        if pill:
            counts[pill] += 1
        if status == "pending":
            counts["pending"] += 1
        g = by_name.get(e["group"])
        if g is None:
            g = {"name": e["group"], "colour": GROUP_COLOURS[len(groups) % len(GROUP_COLOURS)],
                 "pages": [], "_pages": {}, "total": 0, "new": 0}
            by_name[e["group"]] = g
            groups.append(g)
        pname = e["page"] or "Plan switches with no page"
        pg = g["_pages"].get(pname)
        if pg is None:
            pg = {"name": pname, "also": [], "items": []}
            g["_pages"][pname] = pg
            g["pages"].append(pg)
        for a in e["also"]:
            if a not in pg["also"]:
                pg["also"].append(a)
        pg["items"].append({"key": e["key"], "action": e["action"], "checked": e["key"] in granted,
                            "pill": pill, "note": note, "pending": status == "pending"})
        g["total"] += 1
        if pill == "new":
            g["new"] += 1
    for g in groups:
        g.pop("_pages", None)
        # A whole page that moved together says so once, on the page name.
        for pg in g["pages"]:
            notes = {(it["pill"], it["note"]) for it in pg["items"]}
            pg["pill"], pg["note"] = (None, "")
            if len(notes) == 1:
                pill, note = next(iter(notes))
                if pill in ("moved", "renamed") and len(pg["items"]) > 1:
                    pg["pill"], pg["note"] = pill, note
                    for it in pg["items"]:
                        it["quiet"] = True
    return groups, removed, counts


def record_save(cur, plan_id):
    ensure_tables(cur)
    cur.execute("""INSERT INTO plan_editor_saves (plan_id, saved_at) VALUES (%s, NOW())
                   ON CONFLICT (plan_id) DO UPDATE SET saved_at = NOW()""", (plan_id,))


def pending_rows(app, catalog, cur, code_paths=None):
    """Pending keys for the Plans page box: [{key, action, page, group}]."""
    entries, _ = build(app, catalog, code_paths)
    ensure_tables(cur)
    cur.execute("SELECT feature_key FROM plan_catalog_state WHERE status='pending'")
    pend = {r[0] for r in cur.fetchall()}
    return [{"key": e["key"], "action": e["action"], "page": e["page"] or "No menu page",
             "group": e["group"]} for e in entries if e["key"] in pend]


def removed_rows(cur):
    ensure_tables(cur)
    cur.execute("""SELECT s.feature_key, s.page, s.action, s.removed_at, s.removed_why,
                          (SELECT COUNT(*) FROM plan_feature_grants g WHERE g.feature_key = s.feature_key)
                   FROM plan_catalog_state s WHERE s.status='removed' ORDER BY s.removed_at""")
    return [{"key": r[0], "page": r[1], "action": r[2], "when": r[3], "why": r[4], "plans": r[5]}
            for r in cur.fetchall()]


# ── The check ──────────────────────────────────────────────────────────────

def check(app, catalog, code_paths=None):
    """Everything out of step. 'blocking' items stop a portal update."""
    entries, menu = build(app, catalog, code_paths)
    route_keys = _route_keys(app)
    out = {"blocking": [], "warnings": [], "info": []}
    for m in menu:
        ep = m["endpoint"]
        if ep in PHIXTRA_ONLY_ENDPOINTS or ep in EVERY_PLAN_ENDPOINTS:
            continue
        if not route_keys.get(ep):
            out["blocking"].append({"area": "Menu page with no tick",
                                    "item": f"{m['group']} › {m['label']}" if m["group"] != m["label"] else m["label"],
                                    "fix": f"Give the page ({ep}) a tick: @team_feature + PLAN_FEATURE_CATALOG + KEY_HOME"})
    for e in entries:
        if not e["declared"]:
            out["blocking"].append({"area": "Tick with no page entry",
                                    "item": f"{e['key']} ({e['old_label']})",
                                    "fix": "Add it to KEY_HOME in plan_catalog.py with the menu page it lives on"})
        elif e["has_page"] and not e["in_menu"] and e["in_code"]:
            out["warnings"].append({"area": "Tick whose page is not in the menu",
                                    "item": f"{e['action']} ({e['key']})",
                                    "fix": f"Page {e['endpoint']} is not in the side menu. Put it back in the menu, or move the tick to the page that replaced it"})
        if not e["in_code"]:
            out["warnings"].append({"area": "Tick nothing uses any more",
                                    "item": f"{e['action']} ({e['key']})",
                                    "fix": "Shown under Removed from the portal. Clean it up on the Plans page"})
    for ep in set(KEY_HOME.values()) - {None}:
        if ep not in app.view_functions:
            out["blocking"].append({"area": "Tick points at a page that does not exist",
                                    "item": ep, "fix": "Fix the endpoint in KEY_HOME"})
    return out
