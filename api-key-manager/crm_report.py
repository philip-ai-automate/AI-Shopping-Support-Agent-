"""CRM Report — the all-in-one report under Reports › Custom Report
(design approved 2026-10-15: https://claude.ai/artifact/6jBWbU2JqEsC4DTKSZ3ppy).

One page: filters (time period, assigned to, where the lead came from,
sections to show), a "your own table" builder with 122 columns in 12
tick-box dropdowns, and 8 ready-made sections. Every business's data, read
from the real stored fields only. Ambassadors, SMS and estate inspections are
deliberately left out (user 2026-10-14).

Nothing here writes. Every query goes through ctx["ls"] (portal_routes._ls_exec),
so staff without "See all leads / contacts" only ever count their own
records. Per-lead extras (stage history, chats, campaigns, notes, activity)
are pre-grouped derived tables joined by hash, never per-row lookups, so a
20,000-row export stays quick. Only the joins the chosen columns need are added.
"""
import datetime as _dt
import io

# ── Where the lead came from ─────────────────────────────────────────────────
# 'import' and 'whatsapp'/'manual'/'instagram' are written on the lead; a
# campaign reply is a lead that a WhatsApp campaign reply was turned into
# (wa_campaign_recipients.pipeline_lead_id). Import wins — the lead already
# existed before any campaign touched it.
SOURCE_EXPR = ("CASE WHEN l.source = 'import' OR l.import_id IS NOT NULL THEN 'import' "
               "WHEN cl.pipeline_lead_id IS NOT NULL THEN 'campaign' "
               "WHEN COALESCE(l.source, '') = '' THEN 'none' ELSE l.source END")
SOURCE_LABELS = {"whatsapp": "WhatsApp", "campaign": "Campaign reply", "manual": "Added by hand",
                 "import": "Imported list", "instagram": "Instagram", "none": "Not recorded"}
SOURCE_ORDER = ["whatsapp", "campaign", "manual", "import", "instagram", "none"]

OUTCOME_EXPR = ("CASE WHEN l.outcome IN ('won','lost','dropped','not_a_fit') THEN l.outcome "
                "WHEN l.is_opportunity THEN 'open_deal' ELSE 'open_lead' END")
OUTCOME_LABELS = {"open_lead": "Open lead", "open_deal": "Open deal", "won": "Won", "lost": "Lost",
                  "dropped": "Dropped", "not_a_fit": "Not a Fit"}

STAGES = ["new_lead", "contacted", "qualified", "proposal_sent", "negotiating", "won"]
STAGE_RANK = ("CASE l.stage WHEN 'new_lead' THEN 0 WHEN 'contacted' THEN 1 WHEN 'qualified' THEN 2 "
              "WHEN 'proposal_sent' THEN 3 WHEN 'negotiating' THEN 4 WHEN 'won' THEN 5 ELSE 0 END")

MESSAGE_ACTIONS = ("wa_sent", "messenger_sent", "webchat_sent")
MOVE_ACTIONS = ("stage_moved", "opportunity", "proposal", "won", "lost", "not_a_fit", "reopened")
ACTION_LABELS = {
    "call": "Call logged", "wa_sent": "WhatsApp sent", "messenger_sent": "Messenger sent",
    "webchat_sent": "Web chat reply", "email_sent": "Email sent", "seq_added": "Added to email sequence",
    "note_added": "Note added", "lead_created": "Lead created", "lead_edited": "Lead edited",
    "opportunity": "Became a deal", "proposal": "Proposal sent", "won": "Won", "lost": "Lost / Dropped",
    "not_a_fit": "Not a Fit", "reopened": "Reopened", "stage_moved": "Stage moved",
    "chat_claimed": "Chat taken", "reply_logged": "Reply logged", "contact_created": "Contact added",
    "contact_edited": "Contact edited", "company_created": "Company added", "leads_assigned": "Assigned",
    "linkedin": "LinkedIn step",
}
CHANNEL_LABELS = {"email": "Email", "phone_call": "Phone call", "whatsapp": "WhatsApp"}
CAMPAIGN_STATUS_LABELS = {
    "sent": "Sent", "delivered": "Delivered", "read": "Read", "replied": "Replied",
    "interested": "Interested", "not_interested": "Not interested",
    "opportunity": "Opportunity", "converted": "Converted", "failed": "Failed",
}

SECTIONS = [
    ("summary", "Summary"), ("funnel", "From lead to won"), ("closed", "Lost & Dropped deals"),
    ("team", "Team performance"), ("sources", "Where leads came from"),
    ("attention", "Deals needing attention"), ("contacts", "Contacts & Companies"),
    ("campaigns", "Campaigns that brought business"),
]
SECTION_KEYS = [k for k, _ in SECTIONS]

PERIODS = [("30", "Last 30 days"), ("7", "Last 7 days"), ("90", "Last 90 days"),
           ("month", "This month"), ("custom", "Custom dates…")]

GROUP_BY = [("stage", "Stage"), ("outcome", "Outcome"), ("assigned", "Assigned to"), ("source", "Source"),
            ("reason", "Reason closed"), ("company", "Company"), ("campaign", "Campaign")]

SORTS = [("closed", "Closed date (newest first)"), ("value", "Deal value (highest first)"),
         ("name", "Customer name (A–Z)"), ("created", "Created date (newest first)"),
         ("updated", "Last updated (newest first)")]
SORT_SQL = {
    "closed":  "(CASE WHEN l.outcome = 'won' THEN l.won_date ELSE l.dropped_at::date END) DESC NULLS LAST, l.created_at DESC",
    "value":   "fx_to_selling(l.deal_value, l.currency, l.tenant_id) DESC NULLS LAST, l.created_at DESC",
    "name":    "lower(l.customer_name) ASC NULLS LAST, l.id",
    "created": "l.created_at DESC",
    "updated": "l.updated_at DESC NULLS LAST, l.created_at DESC",
}

DEFAULT_COLS = ["customer", "stage", "outcome", "reason", "assigned", "value", "source", "closed"]
STALL_DAYS = 14
EXPORT_MAX_ROWS = 20000


# ── Extra joins, added only when a chosen column needs them ─────────────────
# {t} is the business's id (an int, written inline so placeholders stay simple).
_D = r"regexp_replace({col}, '\D', '', 'g')"
JOINS = {
    "hist": """LEFT JOIN (SELECT h.lead_id, COUNT(*) AS n, MAX(h.created_at) AS last_at,
                     (array_agg(h.changed_by ORDER BY h.created_at DESC))[1] AS last_by
              FROM merchant_pipeline_stage_history h
              JOIN merchant_pipeline_leads hl ON hl.id = h.lead_id AND hl.tenant_id = {t}
              GROUP BY h.lead_id) hist ON hist.lead_id = l.id""",
    "stg": """LEFT JOIN (SELECT h.lead_id, h.to_stage, MAX(h.created_at) AS at
              FROM merchant_pipeline_stage_history h
              JOIN merchant_pipeline_leads hl ON hl.id = h.lead_id AND hl.tenant_id = {t}
              GROUP BY h.lead_id, h.to_stage) stg ON stg.lead_id = l.id AND stg.to_stage = l.stage""",
    "tg": """LEFT JOIN (SELECT x.lead_id, string_agg(DISTINCT x.name, ', ') AS names FROM (
                  SELECT lll.lead_id, ll.name FROM lead_label_leads lll
                  JOIN lead_labels ll ON ll.id = lll.label_id WHERE ll.tenant_id = {t} AND label_visible(ll.id)
                  UNION
                  SELECT ml.id, ll.name FROM lead_label_contacts llc
                  JOIN lead_labels ll ON ll.id = llc.label_id
                  JOIN merchant_pipeline_leads ml ON ml.wa_contact_id = llc.contact_id AND ml.tenant_id = {t}
                  WHERE ll.tenant_id = {t} AND label_visible(ll.id)) x GROUP BY x.lead_id) tg ON tg.lead_id = l.id""",
    "cn": """LEFT JOIN (SELECT n.contact_id, COUNT(*) AS n, MAX(n.created_at) AS last_at,
                     (array_agg(n.body ORDER BY n.created_at DESC))[1] AS last_body
              FROM wa_contact_notes n WHERE n.tenant_id = {t} GROUP BY n.contact_id) cn ON cn.contact_id = c.id""",
    "cod": """LEFT JOIN (SELECT wa_contact_id, COUNT(*) AS n FROM merchant_pipeline_leads
              WHERE tenant_id = {t} AND outcome IS NULL AND is_opportunity AND wa_contact_id IS NOT NULL
              GROUP BY wa_contact_id) cod ON cod.wa_contact_id = c.id""",
    "sup": """LEFT JOIN (SELECT lower(email) AS e, MIN(created_at) AS at FROM email_suppressions
              WHERE tenant_id = {t} GROUP BY lower(email)) sup ON sup.e = lower(l.email)""",
    "cop": """LEFT JOIN (SELECT company_id, COUNT(*) AS n FROM wa_contacts
              WHERE tenant_id = {t} AND company_id IS NOT NULL GROUP BY company_id) cop ON cop.company_id = co.id""",
    "coa": """LEFT JOIN (SELECT company_id,
                     COUNT(*) FILTER (WHERE outcome IS NULL AND is_opportunity) AS open_n,
                     SUM(fx_to_selling(deal_value, currency, tenant_id)) FILTER (WHERE outcome IS NULL AND is_opportunity) AS open_v,
                     SUM(fx_to_selling(deal_value, currency, tenant_id)) FILTER (WHERE outcome = 'won') AS won_v
              FROM merchant_pipeline_leads WHERE tenant_id = {t} AND company_id IS NOT NULL
              GROUP BY company_id) coa ON coa.company_id = co.id""",
    "con": """LEFT JOIN (SELECT n.company_id, COUNT(*) AS n, (array_agg(n.body ORDER BY n.created_at DESC))[1] AS last_body
              FROM crm_company_notes n WHERE n.tenant_id = {t} GROUP BY n.company_id) con ON con.company_id = co.id""",
    "act": """LEFT JOIN (SELECT a.lead_id,
                     SUM(a.qty) FILTER (WHERE a.action = 'email_sent') AS emails,
                     SUM(a.qty) FILTER (WHERE a.action IN ('wa_sent','messenger_sent','webchat_sent')) AS msgs,
                     SUM(a.qty) FILTER (WHERE a.action = 'call') AS calls,
                     SUM(a.qty) FILTER (WHERE a.action = 'note_added') AS notes,
                     COUNT(*) FILTER (WHERE a.action IN ('lead_edited','contact_edited')) AS edits,
                     MAX(a.created_at) AS last_at,
                     (array_agg(a.action ORDER BY a.created_at DESC))[1] AS last_action,
                     (array_agg(a.actor_key ORDER BY a.created_at DESC))[1] AS last_key,
                     (array_agg(a.actor_name ORDER BY a.created_at DESC))[1] AS last_name
              FROM staff_activity a WHERE a.tenant_id = {t} AND a.lead_id IS NOT NULL
              GROUP BY a.lead_id) act ON act.lead_id = l.id""",
    "wm": """LEFT JOIN (SELECT customer_phone,
                     COUNT(*) FILTER (WHERE direction = 'inbound') AS n_in,
                     COUNT(*) FILTER (WHERE direction = 'outbound') AS n_out,
                     MIN(created_at) AS first_at, MAX(created_at) AS last_at
              FROM wa_message_log WHERE tenant_id = {t} GROUP BY customer_phone) wm
             ON wm.customer_phone = ph.d AND ph.d <> ''""",
    "wml": """LEFT JOIN (SELECT DISTINCT ON (customer_phone) customer_phone, direction, content, message_type
              FROM wa_message_log WHERE tenant_id = {t}
              ORDER BY customer_phone, created_at DESC, id DESC) wml ON wml.customer_phone = ph.d AND ph.d <> ''""",
    "wmo": """LEFT JOIN (SELECT DISTINCT ON (customer_phone) customer_phone, sent_by_label
              FROM wa_message_log WHERE tenant_id = {t} AND direction = 'outbound'
              ORDER BY customer_phone, created_at DESC, id DESC) wmo ON wmo.customer_phone = ph.d AND ph.d <> ''""",
    "wca": ("LEFT JOIN (SELECT DISTINCT ON (" + _D.format(col="customer_phone") + ") "
            + _D.format(col="customer_phone") + """ AS d, assigned_to_key, assigned_to_label
              FROM wa_conversation_assignments WHERE tenant_id = {t}
              ORDER BY 1, assigned_at DESC) wca ON wca.d = ph.d AND ph.d <> ''"""),
    "wcr": ("""LEFT JOIN (SELECT DISTINCT ON (r.d) r.d, r.status, r.sent_at, r.replied_at, r.reply_text,
                     r.error_msg, wc.name AS campaign_name, wc.template_name
              FROM (SELECT """ + _D.format(col="phone") + """ AS d, id, campaign_id, status, sent_at, updated_at,
                           replied_at, reply_text, error_msg
                    FROM wa_campaign_recipients WHERE tenant_id = {t}) r
              LEFT JOIN wa_campaigns wc ON wc.id = r.campaign_id
              ORDER BY r.d, COALESCE(r.sent_at, r.updated_at) DESC NULLS LAST, r.id DESC) wcr
             ON wcr.d = ph.d AND ph.d <> ''"""),
    "wcn": ("LEFT JOIN (SELECT " + _D.format(col="phone") + """ AS d, COUNT(DISTINCT campaign_id) AS n
              FROM wa_campaign_recipients WHERE tenant_id = {t} GROUP BY 1) wcn ON wcn.d = ph.d AND ph.d <> ''"""),
    "ecr": """LEFT JOIN (SELECT DISTINCT ON (r.lead_id) r.lead_id, r.status, r.sent_at, r.opened_at, r.open_count,
                     r.clicked_at, r.click_count, r.replied_at, ec.name, ec.subject
              FROM email_campaign_recipients r
              JOIN email_campaigns ec ON ec.id = r.campaign_id AND ec.sequence_id IS NULL
              WHERE r.tenant_id = {t} AND r.lead_id IS NOT NULL
              ORDER BY r.lead_id, r.sent_at DESC NULLS LAST, r.id DESC) ecr ON ecr.lead_id = l.id""",
    "ecn": """LEFT JOIN (SELECT r.lead_id, COUNT(DISTINCT r.campaign_id) AS n
              FROM email_campaign_recipients r
              JOIN email_campaigns ec ON ec.id = r.campaign_id AND ec.sequence_id IS NULL
              WHERE r.tenant_id = {t} AND r.lead_id IS NOT NULL GROUP BY r.lead_id) ecn ON ecn.lead_id = l.id""",
    "seq": """LEFT JOIN (SELECT DISTINCT ON (p.lead_id) p.lead_id, p.status, p.steps_sent, p.last_sent_at,
                     p.next_due_at, p.stop_reason, p.added_at, p.added_by, s.name,
                     (SELECT COUNT(*) FROM email_sequence_steps st WHERE st.sequence_id = s.id) AS n_steps
              FROM email_sequence_people p JOIN email_sequences s ON s.id = p.sequence_id
              WHERE p.tenant_id = {t} AND p.lead_id IS NOT NULL
              ORDER BY p.lead_id, p.added_at DESC) seq ON seq.lead_id = l.id""",
    "imp": "LEFT JOIN crm_imports imp ON imp.id = l.import_id AND imp.tenant_id = {t}",
}
JOIN_ORDER = list(JOINS)


def _c(key, label, expr, fmt="text", joins=()):
    return {"key": key, "label": label, "expr": expr, "fmt": fmt, "joins": tuple(joins)}


_MSG = "('wa_sent','messenger_sent','webchat_sent')"
COL_GROUPS = [
    ("Deal", [
        _c("customer", "Customer name", "l.customer_name", "customer"),
        _c("contact", "Contact person", "l.contact_person"),
        _c("phone", "Phone number", "l.phone"),
        _c("wa", "WhatsApp number", "l.whatsapp_number"),
        _c("email", "Email", "l.email"),
        _c("otheremails", "Other emails", "l.other_emails"),
        _c("website", "Website", "l.website"),
        _c("value", "Deal value", "l.deal_value", "money_own"),
        _c("currency", "Currency", "l.currency"),
        # A lead that isn't a deal yet has no Pipeline step — shown as "—".
        _c("stage", "Stage", "CASE WHEN l.is_opportunity OR l.outcome IN ('won','lost','dropped') THEN l.stage END", "stage"),
        _c("outcome", "Outcome", OUTCOME_EXPR, "outcome"),
        _c("reason", "Reason closed", "l.dropped_reason"),
        _c("closed", "Closed date", "CASE WHEN l.outcome = 'won' THEN l.won_date ELSE l.dropped_at::date END", "date"),
        _c("won", "Won date", "l.won_date", "date"),
        _c("isdeal", "Lead or deal", "CASE WHEN l.is_opportunity THEN 'Deal' ELSE 'Lead' END"),
        _c("becamedeal", "Deal date", "l.opportunity_at", "date"),
        _c("source", "Source", SOURCE_EXPR, "source"),
        _c("assigned", "Assigned to", "l.assigned_key", "assigned"),
        _c("created", "Created date", "l.created_at", "date"),
        _c("updated", "Last updated", "l.updated_at", "date"),
        _c("product", "Product interest", "l.product_interest"),
        _c("budget", "Budget confirmed", "l.budget_confirmed", "yesno"),
        _c("decider", "Decision maker engaged", "l.decision_maker_engaged", "yesno"),
        _c("deadline", "Deadline", "l.deadline_date", "date"),
        _c("category", "Business category", "l.business_category"),
        _c("employees", "Number of employees", "l.employees"),
        _c("dealnotes", "Deal notes", "l.notes", "long"),
        _c("emailcheck", "Email checked", "l.email_verify_status", "title"),
        _c("emailcheckdate", "Email checked on", "l.email_verified_at", "date"),
    ]),
    ("Deal steps", [
        _c("channel", "First contacted by", "l.contact_channel", "channel"),
        _c("contactdate", "Contacted on", "l.contact_date", "date"),
        _c("contactnotes", "Contact notes", "l.contact_notes", "long"),
        _c("qualdate", "Qualified on", "l.qualified_date", "date"),
        _c("qualnotes", "Qualification notes", "l.qualified_notes", "long"),
        _c("propdate", "Proposal sent on", "l.proposal_date", "date"),
        _c("propnotes", "Proposal notes", "l.proposal_notes", "long"),
        _c("negnotes", "Negotiation notes", "l.negotiation_notes", "long"),
        _c("moves", "Stage moves", "COALESCE(hist.n, 0)", "int", ["hist"]),
        _c("lastmove", "Last stage move", "hist.last_at", "date", ["hist"]),
        _c("lastmoveby", "Last moved by", "hist.last_by", "person", ["hist"]),
        _c("days", "Days in this stage",
           "CASE WHEN l.outcome IS NULL THEN GREATEST(0, CURRENT_DATE - COALESCE(stg.at, l.opportunity_at, l.created_at)::date) END",
           "int", ["stg"]),
        _c("daysopen", "Days open",
           "GREATEST(0, COALESCE(CASE WHEN l.outcome = 'won' THEN l.won_date ELSE l.dropped_at::date END, CURRENT_DATE) - l.created_at::date)",
           "int"),
        _c("daystoclose", "Days to close",
           "CASE WHEN l.outcome = 'won' AND l.won_date IS NOT NULL THEN GREATEST(0, l.won_date - COALESCE(l.opportunity_at, l.created_at)::date) END",
           "int"),
    ]),
    ("Contact", [
        _c("cname", "Contact name", "c.display_name"),
        _c("status", "Contact status", "c.status", "title"),
        _c("tags", "Tags", "tg.names", "text", ["tg"]),
        _c("csource", "How contact was added", "c.source", "title"),
        _c("caddedon", "Contact added on", "c.created_at", "date"),
        _c("caddedby", "Contact added by", "c.added_by_key", "actor"),
        _c("personal", "Personal note for messages", "c.personalization_note", "long"),
        _c("notecount", "Number of notes", "CASE WHEN c.id IS NOT NULL THEN COALESCE(cn.n, 0) END", "int", ["cn"]),
        _c("lastnote", "Latest note", "cn.last_body", "long", ["cn"]),
        _c("lastnotedate", "Latest note date", "cn.last_at", "date", ["cn"]),
        _c("opendeals", "Open deals for this contact", "CASE WHEN c.id IS NOT NULL THEN COALESCE(cod.n, 0) END", "int", ["cod"]),
    ]),
    ("Address", [
        _c("street", "Street", "COALESCE(NULLIF(l.addr_street, ''), c.addr_street)"),
        _c("city", "City", "COALESCE(NULLIF(l.addr_city, ''), c.addr_city)"),
        _c("state", "State / county", "COALESCE(NULLIF(l.addr_state, ''), c.addr_state)"),
        _c("country", "Country", "COALESCE(NULLIF(l.addr_country, ''), c.addr_country)", "country"),
        _c("postcode", "Postcode", "COALESCE(NULLIF(l.addr_postcode, ''), c.addr_postcode)"),
    ]),
    ("Can we message them?", [
        _c("waok", "WhatsApp allowed", "CASE WHEN c.id IS NULL THEN NULL WHEN c.opted_out THEN FALSE ELSE TRUE END", "yesno"),
        _c("waout", "WhatsApp opted out on", "CASE WHEN c.opted_out THEN c.opted_out_at END", "date"),
        _c("emailok", "Email allowed",
           "CASE WHEN COALESCE(l.email, '') = '' THEN NULL WHEN c.email_opted_out OR sup.e IS NOT NULL THEN FALSE ELSE TRUE END",
           "yesno", ["sup"]),
        _c("emailout", "Email opted out on", "COALESCE(CASE WHEN c.email_opted_out THEN c.email_opted_out_at END, sup.at)", "date", ["sup"]),
    ]),
    ("Company", [
        _c("company", "Company", "co.name"),
        _c("cowebsite", "Company website", "co.website"),
        _c("people", "People at company", "CASE WHEN co.id IS NOT NULL THEN COALESCE(cop.n, 0) END", "int", ["cop"]),
        _c("codeals", "Company open deals", "CASE WHEN co.id IS NOT NULL THEN COALESCE(coa.open_n, 0) END", "int", ["coa"]),
        _c("covalue", "Company open deal value", "coa.open_v", "money", ["coa"]),
        _c("cowon", "Company won value", "coa.won_v", "money", ["coa"]),
        _c("cocity", "Company city", "co.addr_city"),
        _c("cocountry", "Company country", "co.addr_country", "country"),
        _c("coadded", "Company added on", "co.created_at", "date"),
        _c("coaddedby", "Company added by", "co.added_by_key", "actor"),
        _c("conotes", "Company notes", "CASE WHEN co.id IS NOT NULL THEN COALESCE(con.n, 0) END", "int", ["con"]),
        _c("colastnote", "Latest company note", "con.last_body", "long", ["con"]),
    ]),
    ("Team activity", [
        _c("emails", "Emails sent", "COALESCE(act.emails, 0)", "int", ["act"]),
        _c("wasent", "WhatsApp sent", "COALESCE(act.msgs, 0)", "int", ["act"]),
        _c("calls", "Calls logged", "COALESCE(act.calls, 0)", "int", ["act"]),
        _c("notes", "Notes added", "COALESCE(act.notes, 0)", "int", ["act"]),
        _c("edits", "Times edited", "COALESCE(act.edits, 0)", "int", ["act"]),
        _c("lastact", "Last activity", "act.last_at", "date", ["act"]),
        _c("lastacttype", "Last activity type", "act.last_action", "action", ["act"]),
        _c("lastactby", "Last activity by", "act.last_key || '|' || COALESCE(act.last_name, '')", "actor_named", ["act"]),
    ]),
    ("WhatsApp chat", [
        _c("chatowner", "Chat assigned to (Inbox)", "wca.assigned_to_key || '|' || COALESCE(wca.assigned_to_label, '')",
           "actor_named", ["wca"]),
        _c("msgin", "Messages received", "COALESCE(wm.n_in, 0)", "int", ["wm"]),
        _c("msgout", "Messages sent", "COALESCE(wm.n_out, 0)", "int", ["wm"]),
        _c("firstmsg", "First message", "wm.first_at", "date", ["wm"]),
        _c("lastmsg", "Last message", "wm.last_at", "date", ["wm"]),
        _c("lastfrom", "Last message from",
           "CASE wml.direction WHEN 'inbound' THEN 'Customer' WHEN 'outbound' THEN 'Us' END", "text", ["wml"]),
        _c("lastmsgtext", "Last message text",
           "CASE WHEN COALESCE(wml.content, '') <> '' THEN left(wml.content, 300) WHEN wml.message_type IS NOT NULL THEN '[' || wml.message_type || ']' END",
           "long", ["wml"]),
        _c("lastsentby", "Last reply sent by",
           "CASE WHEN wmo.customer_phone IS NULL THEN NULL ELSE COALESCE(NULLIF(wmo.sent_by_label, ''), 'AI / automatic') END",
           "person", ["wmo"]),
        _c("waitingreply", "Waiting for our reply",
           "CASE wml.direction WHEN 'inbound' THEN TRUE WHEN 'outbound' THEN FALSE END", "yesno", ["wml"]),
    ]),
    ("WhatsApp campaign", [
        _c("campaign", "Campaign", "wcr.campaign_name", "text", ["wcr"]),
        _c("camptemplate", "Template used", "wcr.template_name", "text", ["wcr"]),
        _c("campstatus", "Delivery status", "wcr.status", "campaign_status", ["wcr"]),
        _c("sentat", "Sent on", "wcr.sent_at", "date", ["wcr"]),
        _c("replied", "Replied", "CASE WHEN wcr.d IS NULL THEN NULL ELSE wcr.replied_at IS NOT NULL END", "yesno", ["wcr"]),
        _c("replydate", "Reply date", "wcr.replied_at", "date", ["wcr"]),
        _c("replytext", "Reply text", "wcr.reply_text", "long", ["wcr"]),
        _c("failreason", "Failed reason", "CASE WHEN wcr.status = 'failed' THEN wcr.error_msg END", "long", ["wcr"]),
        _c("campcount", "Campaigns received", "COALESCE(wcn.n, 0)", "int", ["wcn"]),
    ]),
    ("Email campaign", [
        _c("ecamp", "Email campaign", "ecr.name", "text", ["ecr"]),
        _c("esubject", "Subject", "ecr.subject", "text", ["ecr"]),
        _c("estatus", "Email status", "ecr.status", "title", ["ecr"]),
        _c("esent", "Email sent on", "ecr.sent_at", "date", ["ecr"]),
        _c("eopened", "First opened on", "ecr.opened_at", "date", ["ecr"]),
        _c("eopens", "Times opened", "ecr.open_count", "int", ["ecr"]),
        _c("eclicked", "First clicked on", "ecr.clicked_at", "date", ["ecr"]),
        _c("eclicks", "Times clicked", "ecr.click_count", "int", ["ecr"]),
        _c("ereplied", "Email replied on", "ecr.replied_at", "date", ["ecr"]),
        _c("ecount", "Email campaigns received", "COALESCE(ecn.n, 0)", "int", ["ecn"]),
    ]),
    ("Email sequence", [
        _c("seq", "Sequence", "seq.name", "text", ["seq"]),
        _c("seqstatus", "Sequence status", "seq.status", "title", ["seq"]),
        _c("seqsteps", "Sequence emails sent",
           "CASE WHEN seq.lead_id IS NOT NULL THEN seq.steps_sent || ' of ' || seq.n_steps END", "text", ["seq"]),
        _c("seqlast", "Last sequence email", "seq.last_sent_at", "date", ["seq"]),
        _c("seqnext", "Next email due", "CASE WHEN seq.status = 'active' THEN seq.next_due_at END", "date", ["seq"]),
        _c("seqstop", "Stopped because", "seq.stop_reason", "long", ["seq"]),
        _c("seqadded", "Added to sequence on", "seq.added_at", "date", ["seq"]),
        _c("seqby", "Added to sequence by", "seq.added_by", "person", ["seq"]),
    ]),
    ("Import", [
        _c("impfile", "Imported from file", "imp.file_name", "text", ["imp"]),
        _c("impdate", "Imported on", "imp.created_at", "date", ["imp"]),
        _c("impby", "Imported by", "imp.created_by", "person", ["imp"]),
    ]),
]
COLUMNS = {c["key"]: c for _, cols in COL_GROUPS for c in cols}
COLUMN_ORDER = [c["key"] for _, cols in COL_GROUPS for c in cols]
TOTAL_COLUMNS = len(COLUMN_ORDER)

GROUP_EXPR = {
    "stage": ("CASE WHEN l.is_opportunity OR l.outcome IN ('won','lost','dropped') THEN l.stage ELSE '' END", ()), "outcome": (OUTCOME_EXPR, ()), "assigned": ("COALESCE(l.assigned_key, '')", ()),
    "source": (SOURCE_EXPR, ()), "reason": ("COALESCE(l.dropped_reason, '')", ()),
    "company": ("COALESCE(co.name, '')", ()), "campaign": ("COALESCE(wcr.campaign_name, '')", ("wcr",)),
}


# ── Reading the request ──────────────────────────────────────────────────────

def _iso(s):
    try:
        return _dt.date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def read_options(args, today=None) -> dict:
    """Everything the page, the downloads and a saved report share, read from
    the query string (or a saved report's config) — invalid values dropped."""
    today = today or _dt.date.today()
    period = args.get("period") or "30"
    d_from, d_to = _iso(args.get("date_from")), _iso(args.get("date_to"))
    if period == "custom" and d_from and d_to and d_from <= d_to:
        label = d_from.strftime("%d %b %Y") + " – " + d_to.strftime("%d %b %Y")
    elif period == "month":
        d_from, d_to = today.replace(day=1), today
        label = "This month (" + today.strftime("%B %Y") + ")"
    else:
        period = period if period in ("7", "30", "90") else "30"
        d_to = today
        d_from = today - _dt.timedelta(days=int(period) - 1)      # exactly N days, today included
        label = "Last " + period + " days"

    cols = [c for c in args.getlist("col") if c in COLUMNS]
    seen = set()
    cols = [c for c in cols if not (c in seen or seen.add(c))]
    if not cols and args.get("cols_set") != "1":
        cols = list(DEFAULT_COLS)
    secs = [s for s in args.getlist("sec") if s in SECTION_KEYS]
    if not secs and args.get("secs_set") != "1":
        secs = list(SECTION_KEYS)
    group_by = args.get("group_by") or ""
    sort = args.get("sort") or "closed"
    try:
        page = max(1, int(args.get("page") or 1))
    except ValueError:
        page = 1
    return {
        "period": period, "date_from": d_from, "date_to": d_to, "period_label": label,
        "asg": [a for a in args.getlist("asg") if a == "__none__" or a.startswith(("team:", "owner:"))][:200],
        "src": [s for s in args.getlist("src") if s in SOURCE_LABELS],
        "sec": secs, "col": cols,
        "group_by": group_by if group_by in dict(GROUP_BY) else "",
        "sort": sort if sort in SORT_SQL else "closed",
        "page": page,
    }


def config_from(opts: dict) -> dict:
    """What "Save this report" stores (and the query string that reopens it)."""
    cfg = {"period": opts["period"], "col": opts["col"], "cols_set": "1", "sec": opts["sec"], "secs_set": "1",
           "asg": opts["asg"], "src": opts["src"], "group_by": opts["group_by"], "sort": opts["sort"]}
    if opts["period"] == "custom":
        cfg["date_from"], cfg["date_to"] = opts["date_from"].isoformat(), opts["date_to"].isoformat()
    return {k: v for k, v in cfg.items() if v not in ("", [], None)}


def normalized(cfg: dict) -> dict:
    out = {}
    for k in ("period", "date_from", "date_to", "group_by", "sort"):
        out[k] = cfg.get(k) or ""
    for k in ("col", "sec", "asg", "src"):
        v = cfg.get(k) or []
        out[k] = sorted(v if isinstance(v, list) else [v])
    return out


# ── SQL pieces ───────────────────────────────────────────────────────────────

def base_from(tenant_id: int, joins=()) -> str:
    t = int(tenant_id)
    sql = [
        "merchant_pipeline_leads l",
        "LEFT JOIN wa_contacts c ON c.id = l.wa_contact_id",
        "LEFT JOIN crm_companies co ON co.id = COALESCE(l.company_id, c.company_id)",
        f"LEFT JOIN (SELECT DISTINCT pipeline_lead_id FROM wa_campaign_recipients WHERE tenant_id = {t} "
        "AND pipeline_lead_id IS NOT NULL) cl ON cl.pipeline_lead_id = l.id",
        "CROSS JOIN LATERAL (SELECT " + _D.format(
            col="COALESCE(NULLIF(l.whatsapp_number, ''), NULLIF(c.whatsapp_number, ''), "
                "NULLIF(l.phone, ''), NULLIF(c.phone, ''), '')") + " AS d) ph",
    ]
    for j in JOIN_ORDER:
        if j in joins:
            sql.append(JOINS[j].format(t=t))
    return "\n".join(sql)


def base_where(tenant_id: int, opts: dict):
    """Business + Assigned to + Where the lead came from (no time period)."""
    clauses, params = ["l.tenant_id = %s"], [int(tenant_id)]
    asg = opts.get("asg") or []
    if asg:
        keys = [a for a in asg if a != "__none__"]
        parts = []
        if keys:
            parts.append("l.assigned_key = ANY(%s)")
            params.append(keys)
        if "__none__" in asg:
            parts.append("COALESCE(l.assigned_key, '') = ''")
        clauses.append("(" + " OR ".join(parts) + ")")
    src = opts.get("src") or []
    if src:
        clauses.append(f"({SOURCE_EXPR}) = ANY(%s)")
        params.append(list(src))
    return clauses, params


def period_clause(opts: dict):
    """A lead or deal belongs to the period when it was added, became a deal,
    was won or closed, or moved a step in it."""
    a, b = opts["date_from"], opts["date_to"]
    return ("(l.created_at::date BETWEEN %s AND %s OR l.opportunity_at::date BETWEEN %s AND %s "
            "OR l.won_date BETWEEN %s AND %s OR l.dropped_at::date BETWEEN %s AND %s "
            "OR l.id IN (SELECT lead_id FROM merchant_pipeline_stage_history WHERE created_at::date BETWEEN %s AND %s))",
            [a, b] * 5)


def table_query(tenant_id: int, opts: dict, cols: list, page=None, per_page=50, limit=None,
                count_only=False):
    specs = [COLUMNS[k] for k in cols]
    joins = {j for s in specs for j in s["joins"]}
    clauses, params = base_where(tenant_id, opts)
    pc, pp = period_clause(opts)
    clauses.append(pc)
    params += pp
    where = " AND ".join(clauses)
    if count_only:
        return f"SELECT COUNT(*) AS n FROM {base_from(tenant_id)} WHERE {where}", params
    select = ", ".join(["l.id AS _id", "l.currency AS _cur"] + [f"{s['expr']} AS c_{s['key']}" for s in specs])
    sql = f"SELECT {select} FROM {base_from(tenant_id, joins)} WHERE {where} ORDER BY {SORT_SQL[opts['sort']]}"
    if limit:
        sql += " LIMIT %s"
        params = params + [limit]
    else:
        sql += " LIMIT %s OFFSET %s"
        params = params + [per_page, (max(1, page or 1) - 1) * per_page]
    return sql, params


def group_query(tenant_id: int, opts: dict):
    expr, joins = GROUP_EXPR[opts["group_by"]]
    clauses, params = base_where(tenant_id, opts)
    pc, pp = period_clause(opts)
    clauses.append(pc)
    params += pp
    return (f"SELECT {expr} AS grp, COUNT(*) AS n, "
            f"COALESCE(SUM(fx_to_selling(l.deal_value, l.currency, l.tenant_id)), 0) AS v "
            f"FROM {base_from(tenant_id, joins)} WHERE {' AND '.join(clauses)} "
            f"GROUP BY 1 ORDER BY n DESC LIMIT 300", params)


# ── Formatting one cell ──────────────────────────────────────────────────────

def fmt_cell(val, fmt: str, ctx: dict, row: dict = None) -> str:
    if val is None or val == "":
        return "Unassigned" if fmt == "assigned" else "—"
    if fmt == "date":
        return val.strftime("%d %b %Y") if hasattr(val, "strftime") else str(val)
    if fmt == "money_own":
        return ctx["money"](val, (row or {}).get("_cur"))
    if fmt == "money":
        return ctx["money_total"](val)
    if fmt == "int":
        return f"{int(val):,}"
    if fmt == "yesno":
        return "Yes" if val else "No"
    if fmt == "stage":
        return ctx["stage_labels"].get(val, str(val).replace("_", " ").title())
    if fmt == "outcome":
        return OUTCOME_LABELS.get(val, val)
    if fmt == "source":
        return SOURCE_LABELS.get(val, str(val).title())
    if fmt == "channel":
        return CHANNEL_LABELS.get(val, str(val).replace("_", " ").title())
    if fmt == "campaign_status":
        return CAMPAIGN_STATUS_LABELS.get(val, str(val).title())
    if fmt == "action":
        return ACTION_LABELS.get(val, str(val).replace("_", " ").capitalize())
    if fmt == "title":
        return str(val).replace("_", " ").title()
    if fmt == "country":
        return ctx["country_name"](val)
    if fmt in ("assigned", "actor"):
        return ctx["person"](val, None)
    if fmt == "actor_named":
        key, _, name = str(val).partition("|")
        return ctx["person"](key, name or None)
    if fmt == "person":
        return ctx["person"](None, str(val))
    if fmt == "long":
        s = " ".join(str(val).split())
        return s if len(s) <= 300 else s[:297] + "…"
    return str(val)


def run_table(cur, ctx, opts, export=False, per_page=50):
    tenant_id = ctx["tenant_id"]
    sql, params = table_query(tenant_id, opts, opts["col"], count_only=True)
    ctx["ls"](cur, sql, params)
    total = int(cur.fetchone()["n"] or 0)
    rows = []
    if opts["col"]:
        sql, params = table_query(tenant_id, opts, opts["col"], page=opts["page"], per_page=per_page,
                                  limit=EXPORT_MAX_ROWS if export else None)
        ctx["ls"](cur, sql, params)
        for r in cur.fetchall():
            rows.append({"id": r["_id"], "cells": [fmt_cell(r["c_" + k], COLUMNS[k]["fmt"], ctx, r)
                                                    for k in opts["col"]]})
    return rows, total


def run_groups(cur, ctx, opts):
    sql, params = group_query(ctx["tenant_id"], opts)
    ctx["ls"](cur, sql, params)
    raw = cur.fetchall()
    gb = opts["group_by"]
    out = []
    for r in raw:
        g = r["grp"]
        if gb == "stage":
            label = ctx["stage_labels"].get(g, g) if g else "Not a deal yet"
        elif gb == "outcome":
            label = OUTCOME_LABELS.get(g, g)
        elif gb == "assigned":
            label = ctx["person"](g, None) if g else "Unassigned"
        elif gb == "source":
            label = SOURCE_LABELS.get(g, str(g).title())
        elif gb == "reason":
            label = g or "No reason (not closed, or Won)"
        elif gb == "company":
            label = g or "No company"
        else:
            label = g or "No campaign"
        out.append({"label": label, "count": int(r["n"]), "value": float(r["v"] or 0)})
    if gb == "stage":
        rank = {ctx["stage_labels"].get(s, s): i for i, s in enumerate(STAGES)}
        rank["Not a deal yet"] = -1
        out.sort(key=lambda x: rank.get(x["label"], 99))
    return out


# ── The 8 ready-made sections ────────────────────────────────────────────────

def _bars(rows, key="n"):
    top = max([r[key] for r in rows] + [1])
    for r in rows:
        r["pct"] = max(2, round(r[key] / top * 100)) if r[key] else 0
    return rows


def sec_summary(cur, ctx, opts):
    t = ctx["tenant_id"]
    a, b = opts["date_from"], opts["date_to"]
    span = (b - a).days + 1
    pa, pb = a - _dt.timedelta(days=span), a - _dt.timedelta(days=1)
    clauses, params = base_where(t, opts)
    ctx["ls"](cur, f"""
        SELECT COUNT(*) FILTER (WHERE l.created_at::date BETWEEN %s AND %s) AS new_leads,
               COUNT(*) FILTER (WHERE l.created_at::date BETWEEN %s AND %s) AS prev_leads,
               COUNT(*) FILTER (WHERE l.opportunity_at::date BETWEEN %s AND %s) AS deals,
               COUNT(*) FILTER (WHERE l.outcome = 'won' AND l.won_date BETWEEN %s AND %s) AS won,
               COALESCE(SUM(fx_to_selling(l.deal_value, l.currency, l.tenant_id))
                        FILTER (WHERE l.outcome = 'won' AND l.won_date BETWEEN %s AND %s), 0) AS won_v,
               COUNT(*) FILTER (WHERE l.outcome = 'lost' AND l.dropped_at::date BETWEEN %s AND %s) AS lost,
               COUNT(*) FILTER (WHERE l.outcome = 'dropped' AND l.dropped_at::date BETWEEN %s AND %s) AS dropped,
               COUNT(*) FILTER (WHERE l.outcome IS NULL AND l.is_opportunity) AS open_n,
               COALESCE(SUM(fx_to_selling(l.deal_value, l.currency, l.tenant_id))
                        FILTER (WHERE l.outcome IS NULL AND l.is_opportunity), 0) AS open_v,
               AVG(GREATEST(0, l.won_date - COALESCE(l.opportunity_at, l.created_at)::date))
                   FILTER (WHERE l.outcome = 'won' AND l.won_date BETWEEN %s AND %s) AS avg_days
        FROM {base_from(t)} WHERE {' AND '.join(clauses)}""",
                 [a, b, pa, pb, a, b, a, b, a, b, a, b, a, b, a, b] + params)
    r = cur.fetchone()
    new, prev = int(r["new_leads"]), int(r["prev_leads"])
    if prev:
        ch = round((new - prev) / prev * 100)
        trend = f"{'+' if ch >= 0 else ''}{ch}% on the {span} days before"
    else:
        trend = f"None in the {span} days before" if not new else f"Up from none in the {span} days before"
    won, lost = int(r["won"]), int(r["lost"])
    rate = round(won / (won + lost) * 100) if (won + lost) else None
    deals = int(r["deals"])
    return {"tiles": [
        {"label": "New leads", "value": f"{new:,}", "note": trend},
        {"label": "Became deals", "value": f"{deals:,}",
         "note": (f"{round(deals / new * 100)}% of new leads" if new else "Moved onto the Pipeline Board")},
        {"label": "Won", "value": f"{won:,}", "note": ctx["money_total"](r["won_v"]) + " won"},
        {"label": "Lost", "value": f"{lost:,}", "note": "Customer said no"},
        {"label": "Dropped", "value": f"{int(r['dropped']):,}", "note": "We stopped pursuing"},
        {"label": "Win rate", "value": (f"{rate}%" if rate is not None else "—"), "note": "Won ÷ (Won + Lost)"},
        {"label": "Open deal value", "value": ctx["money_total"](r["open_v"]),
         "note": f"Right now, {int(r['open_n']):,} open deal{'s' if int(r['open_n']) != 1 else ''}"},
        {"label": "Average days to close",
         "value": (str(round(float(r["avg_days"]))) if r["avg_days"] is not None else "—"), "note": "From deal to won"},
    ]}


def sec_funnel(cur, ctx, opts):
    """Leads added in the period, and how far each one has got."""
    t = ctx["tenant_id"]
    clauses, params = base_where(t, opts)
    clauses.append("l.created_at::date BETWEEN %s AND %s")
    params += [opts["date_from"], opts["date_to"]]
    deal = "(l.is_opportunity OR l.outcome IN ('won','lost','dropped'))"
    reach = ", ".join(
        f"COUNT(*) FILTER (WHERE {deal} AND (l.outcome = 'won' OR {STAGE_RANK} >= {i})) AS s{i}"
        for i in range(len(STAGES)))
    ctx["ls"](cur, f"SELECT COUNT(*) AS n, {reach} FROM {base_from(t)} WHERE {' AND '.join(clauses)}", params)
    r = cur.fetchone()
    rows = [{"label": "New leads", "n": int(r["n"]), "conv": ""}]
    for i, s in enumerate(STAGES):
        rows.append({"label": ctx["stage_labels"].get(s, s), "n": int(r[f"s{i}"]), "conv": ""})
    for prev, row in zip(rows, rows[1:]):
        row["conv"] = (f"{round(row['n'] / prev['n'] * 100)}% carried on" if prev["n"] else "")
    return {"rows": _bars(rows)}


def sec_closed(cur, ctx, opts):
    t = ctx["tenant_id"]
    clauses, params = base_where(t, opts)
    clauses.append("l.outcome IN ('lost','dropped') AND l.dropped_at::date BETWEEN %s AND %s")
    params += [opts["date_from"], opts["date_to"]]
    where = " AND ".join(clauses)
    ctx["ls"](cur, f"""SELECT COALESCE(NULLIF(l.dropped_reason, ''), 'No reason given') AS label, COUNT(*) AS n
                       FROM {base_from(t)} WHERE {where} GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", params)
    reasons = [{"label": r["label"], "n": int(r["n"])} for r in cur.fetchall()]
    ctx["ls"](cur, f"SELECT l.stage, COUNT(*) AS n FROM {base_from(t)} WHERE {where} GROUP BY 1", params)
    by_stage = {r["stage"]: int(r["n"]) for r in cur.fetchall()}
    reached = [{"label": ctx["stage_labels"].get(s, s), "n": by_stage.pop(s)} for s in STAGES if s in by_stage]
    reached += [{"label": str(s).replace("_", " ").title(), "n": n} for s, n in by_stage.items()]
    ctx["ls"](cur, f"""
        SELECT l.id, l.customer_name, l.outcome, l.stage, l.dropped_reason, l.dropped_at,
               (SELECT h.changed_by FROM merchant_pipeline_stage_history h
                 WHERE h.lead_id = l.id AND h.to_stage IN ('lost','dropped')
                 ORDER BY h.created_at DESC LIMIT 1) AS closed_by
        FROM {base_from(t)} WHERE {where} ORDER BY l.dropped_at DESC LIMIT 50""", params)
    rows = [{
        "id": r["id"], "name": r["customer_name"] or "—", "outcome": r["outcome"],
        "outcome_label": OUTCOME_LABELS.get(r["outcome"], r["outcome"]),
        "stage": ctx["stage_labels"].get(r["stage"], r["stage"]), "reason": r["dropped_reason"] or "—",
        "by": ctx["person"](None, r["closed_by"]) if r["closed_by"] else "—",
        "date": r["dropped_at"].strftime("%d %b %Y") if r["dropped_at"] else "—",
    } for r in cur.fetchall()]
    total = sum(x["n"] for x in reasons)
    return {"reasons": _bars(reasons), "reached": _bars(reached), "rows": rows, "total": total}


def sec_team(cur, ctx, opts):
    """One row per person: leads and deals assigned to them, and the work
    they logged in the portal in this period. ctx["team_scope"] is None (see
    everyone) or the set of person keys this viewer may see."""
    t = ctx["tenant_id"]
    a, b = opts["date_from"], opts["date_to"]
    clauses, params = base_where(t, opts)
    ctx["ls"](cur, f"""
        SELECT COALESCE(l.assigned_key, '') AS k,
               COUNT(*) FILTER (WHERE l.outcome IS NULL) AS leads,
               COUNT(*) FILTER (WHERE l.opportunity_at::date BETWEEN %s AND %s) AS deals,
               COUNT(*) FILTER (WHERE l.outcome = 'won' AND l.won_date BETWEEN %s AND %s) AS won,
               COUNT(*) FILTER (WHERE l.outcome = 'lost' AND l.dropped_at::date BETWEEN %s AND %s) AS lost,
               COALESCE(SUM(fx_to_selling(l.deal_value, l.currency, l.tenant_id))
                        FILTER (WHERE l.outcome IS NULL AND l.is_opportunity), 0) AS open_v
        FROM {base_from(t)} WHERE {' AND '.join(clauses)} GROUP BY 1""", [a, b, a, b, a, b] + params)
    by_lead = {r["k"]: r for r in cur.fetchall()}
    tz = ctx.get("tz") or "UTC"
    cur.execute("""
        SELECT actor_key AS k, MAX(actor_name) AS name,
               COALESCE(SUM(qty) FILTER (WHERE action = ANY(%s)), 0) AS moves,
               COALESCE(SUM(qty) FILTER (WHERE action = 'email_sent'), 0) AS emails,
               COALESCE(SUM(qty) FILTER (WHERE action = ANY(%s)), 0) AS msgs,
               COALESCE(SUM(qty) FILTER (WHERE action = 'call'), 0) AS calls,
               COALESCE(SUM(qty) FILTER (WHERE action = 'note_added'), 0) AS notes
        FROM staff_activity
        WHERE tenant_id = %s AND (created_at AT TIME ZONE %s)::date BETWEEN %s AND %s
          AND actor_key NOT LIKE 'system%%'
        GROUP BY actor_key""", (list(MOVE_ACTIONS), list(MESSAGE_ACTIONS), t, tz, a, b))
    by_act = {r["k"]: r for r in cur.fetchall()}
    # Demos (2026-10-22): Microsoft Bookings demos held in this period, by the lead's salesperson
    by_demo = {}
    cur.execute("SELECT to_regclass('booking_appointments') IS NOT NULL AS ok")
    if cur.fetchone()["ok"]:
        cur.execute("""
            SELECT COALESCE(l.assigned_key, '') AS k, COUNT(*) AS demos,
                   COUNT(*) FILTER (WHERE a.status = 'done') AS attended,
                   COUNT(*) FILTER (WHERE a.status = 'no_show') AS no_show
            FROM booking_appointments a JOIN merchant_pipeline_leads l ON l.id = a.lead_id
            WHERE a.tenant_id = %s AND a.status <> 'cancelled'
              AND (a.start_at AT TIME ZONE %s)::date BETWEEN %s AND %s
            GROUP BY 1""", (t, tz, a, b))
        by_demo = {r["k"]: r for r in cur.fetchall()}

    scope = ctx.get("team_scope")
    asg = set(opts.get("asg") or [])
    keys = [p["key"] for p in ctx["people"]]
    keys += [k for k in list(by_lead) + list(by_act) + list(by_demo) if k and k not in keys]
    rows = []
    for k in keys + [""]:
        if scope is not None and k not in scope:
            continue
        if asg and (k or "__none__") not in asg:
            continue
        L, A, D = by_lead.get(k) or {}, by_act.get(k) or {}, by_demo.get(k) or {}
        if not L and not A and not D and (k == "" or k not in ctx["active_keys"]):
            continue
        won, lost = int(L.get("won") or 0), int(L.get("lost") or 0)
        no_act = k == ""
        rows.append({
            "name": "Unassigned" if k == "" else ctx["person"](k, A.get("name")),
            "leads": int(L.get("leads") or 0), "deals": int(L.get("deals") or 0),
            "moves": "—" if no_act else int(A.get("moves") or 0),
            "emails": "—" if no_act else int(A.get("emails") or 0),
            "wa": "—" if no_act else int(A.get("msgs") or 0),
            "calls": "—" if no_act else int(A.get("calls") or 0),
            "notes": "—" if no_act else int(A.get("notes") or 0),
            "demos": int(D.get("demos") or 0), "demos_attended": int(D.get("attended") or 0),
            "demos_no_show": int(D.get("no_show") or 0),
            "won": won, "rate": f"{round(won / (won + lost) * 100)}%" if (won + lost) else "—",
            "value": ctx["money_total"](L.get("open_v") or 0),
        })
    return {"rows": rows}


def sec_sources(cur, ctx, opts):
    t = ctx["tenant_id"]
    clauses, params = base_where(t, opts)
    clauses.append("l.created_at::date BETWEEN %s AND %s")
    params += [opts["date_from"], opts["date_to"]]
    ctx["ls"](cur, f"""
        SELECT {SOURCE_EXPR} AS s, COUNT(*) AS leads,
               COUNT(*) FILTER (WHERE l.is_opportunity OR l.outcome IN ('won','lost','dropped')) AS deals,
               COUNT(*) FILTER (WHERE l.outcome = 'won') AS won,
               COALESCE(SUM(fx_to_selling(l.deal_value, l.currency, l.tenant_id)) FILTER (WHERE l.outcome = 'won'), 0) AS won_v
        FROM {base_from(t)} WHERE {' AND '.join(clauses)} GROUP BY 1""", params)
    got = {r["s"]: r for r in cur.fetchall()}
    order = [s for s in SOURCE_ORDER if s in got] + [s for s in got if s not in SOURCE_ORDER]
    rows = []
    for s in order:
        r = got[s]
        leads, won = int(r["leads"]), int(r["won"])
        rows.append({"name": SOURCE_LABELS.get(s, str(s).title()), "leads": leads, "deals": int(r["deals"]),
                     "won": won, "conv": f"{won / leads * 100:.1f}%" if leads else "—",
                     "value": ctx["money_total"](r["won_v"]) if r["won_v"] else "—"})
    return {"rows": rows}


def sec_attention(cur, ctx, opts):
    """Open deals with no step moved for STALL_DAYS days or more (right now,
    not limited to the period)."""
    t = ctx["tenant_id"]
    clauses, params = base_where(t, opts)
    clauses.append("l.outcome IS NULL AND l.is_opportunity")
    since = "COALESCE(hist.last_at, l.opportunity_at, l.created_at)"
    clauses.append(f"CURRENT_DATE - {since}::date >= {STALL_DAYS}")
    where = " AND ".join(clauses)
    frm = base_from(t, ("hist", "act"))
    ctx["ls"](cur, f"SELECT COUNT(*) AS n FROM {frm} WHERE {where}", params)
    total = int(cur.fetchone()["n"] or 0)
    ctx["ls"](cur, f"""
        SELECT l.id, l.customer_name, l.stage, l.assigned_key, l.deal_value, l.currency,
               CURRENT_DATE - {since}::date AS days, act.last_at
        FROM {frm} WHERE {where} ORDER BY days DESC, l.id LIMIT 50""", params)
    rows = [{
        "id": r["id"], "name": r["customer_name"] or "—",
        "stage": ctx["stage_labels"].get(r["stage"], r["stage"]),
        "who": ctx["person"](r["assigned_key"], None) if r["assigned_key"] else "Unassigned",
        "days": int(r["days"]),
        "last": r["last_at"].strftime("%d %b %Y") if r["last_at"] else "No activity logged",
        "value": ctx["money"](r["deal_value"], r["currency"]) if r["deal_value"] else "—",
    } for r in cur.fetchall()]
    return {"rows": rows, "total": total}


def sec_contacts(cur, ctx, opts):
    t = ctx["tenant_id"]
    a, b = opts["date_from"], opts["date_to"]
    ctx["ls"](cur, "SELECT COUNT(*) AS n FROM wa_contacts c WHERE c.tenant_id = %s AND c.created_at::date BETWEEN %s AND %s",
              (t, a, b))
    new_contacts = int(cur.fetchone()["n"])
    ctx["ls"](cur, "SELECT COUNT(*) AS n FROM crm_companies co WHERE co.tenant_id = %s AND co.created_at::date BETWEEN %s AND %s",
              (t, a, b))
    new_companies = int(cur.fetchone()["n"])
    clauses, params = base_where(t, opts)
    ctx["ls"](cur, f"""SELECT COUNT(DISTINCT l.wa_contact_id) AS n FROM {base_from(t)}
                       WHERE {' AND '.join(clauses)} AND l.outcome = 'won' AND l.won_date BETWEEN %s AND %s""",
              params + [a, b])
    customers = int(cur.fetchone()["n"])
    ctx["ls"](cur, "SELECT COALESCE(NULLIF(c.status, ''), 'none') AS s, COUNT(*) AS n FROM wa_contacts c "
                   "WHERE c.tenant_id = %s GROUP BY 1 ORDER BY 2 DESC", (t,))
    statuses = [{"label": "No status" if r["s"] == "none" else str(r["s"]).replace("_", " ").title(),
                 "n": int(r["n"])} for r in cur.fetchall()]
    ctx["ls"](cur, f"""
        SELECT co.id, co.name, COUNT(*) AS deals,
               SUM(fx_to_selling(l.deal_value, l.currency, l.tenant_id)) AS v,
               (SELECT COUNT(*) FROM wa_contacts c2 WHERE c2.company_id = co.id) AS people
        FROM {base_from(t)} WHERE {' AND '.join(clauses)} AND l.outcome IS NULL AND l.is_opportunity
          AND co.id IS NOT NULL
        GROUP BY co.id, co.name ORDER BY v DESC NULLS LAST, deals DESC LIMIT 10""", params)
    companies = [{"id": r["id"], "name": r["name"] or "—", "people": int(r["people"] or 0), "deals": int(r["deals"]),
                  "value": ctx["money_total"](r["v"] or 0)} for r in cur.fetchall()]
    return {"new_contacts": new_contacts, "new_companies": new_companies, "customers": customers,
            "statuses": _bars(statuses), "companies": companies}


def sec_campaigns(cur, ctx, opts):
    """WhatsApp campaigns sent in the period, and the deals that came from
    their replies (a reply turned into a lead: pipeline_lead_id)."""
    t = ctx["tenant_id"]
    a, b = opts["date_from"], opts["date_to"]
    cur.execute("""
        SELECT wc.id, wc.name,
               COUNT(*) FILTER (WHERE r.status <> 'failed') AS sent,
               COUNT(*) FILTER (WHERE r.replied_at IS NOT NULL) AS replied
        FROM wa_campaign_recipients r
        JOIN wa_campaigns wc ON wc.id = r.campaign_id
        WHERE r.tenant_id = %s AND r.sent_at::date BETWEEN %s AND %s
        GROUP BY wc.id, wc.name ORDER BY MAX(r.sent_at) DESC""", (t, a, b))
    camps = cur.fetchall()
    # Each deal counted once per campaign, even if it got two messages from it.
    ctx["ls"](cur, """
        SELECT d.campaign_id, COUNT(*) AS deals,
               COALESCE(SUM(d.v) FILTER (WHERE d.outcome = 'won'), 0) AS won_v
        FROM (SELECT DISTINCT r.campaign_id, l.id, l.outcome,
                     fx_to_selling(l.deal_value, l.currency, l.tenant_id) AS v
              FROM wa_campaign_recipients r
              JOIN merchant_pipeline_leads l ON l.id = r.pipeline_lead_id
              WHERE r.tenant_id = %s AND r.sent_at::date BETWEEN %s AND %s
                AND (l.is_opportunity OR l.outcome IN ('won','lost','dropped'))) d
        GROUP BY d.campaign_id""", (t, a, b))
    deals = {r["campaign_id"]: r for r in cur.fetchall()}
    rows = []
    for r in camps:
        d = deals.get(r["id"]) or {}
        sent, replied = int(r["sent"]), int(r["replied"])
        rows.append({"name": r["name"] or "—", "sent": sent, "replied": replied,
                     "rate": f"{replied / sent * 100:.1f}%" if sent else "—",
                     "deals": int(d.get("deals") or 0),
                     "value": ctx["money_total"](d["won_v"]) if d.get("won_v") else "—"})
    return {"rows": rows}


SECTION_FUNCS = {"summary": sec_summary, "funnel": sec_funnel, "closed": sec_closed, "team": sec_team,
                 "sources": sec_sources, "attention": sec_attention, "contacts": sec_contacts,
                 "campaigns": sec_campaigns}


def run_sections(cur, ctx, opts) -> dict:
    out = {}
    for key in opts["sec"]:
        if key == "team" and not ctx.get("can_team"):
            continue
        try:
            out[key] = SECTION_FUNCS[key](cur, ctx, opts)
        except Exception as e:
            cur.connection.rollback()
            print(f"⚠️ crm_report section {key} error:", e)
            out[key] = {"error": True}
    return out


# ── Downloads ────────────────────────────────────────────────────────────────

def section_tables(sections: dict, stage_note: str = "") -> list:
    """Each section as (title, headers, rows) — for Excel sheets and the PDF."""
    out = []
    s = sections.get("summary")
    if s and not s.get("error"):
        out.append(("Summary", ["Measure", "Number", "Note"], [[x["label"], x["value"], x["note"]] for x in s["tiles"]]))
    s = sections.get("funnel")
    if s and not s.get("error"):
        out.append(("From lead to won", ["Step", "How many", "Carried on"],
                    [[x["label"], f"{x['n']:,}", x["conv"]] for x in s["rows"]]))
    s = sections.get("closed")
    if s and not s.get("error"):
        out.append(("Lost & Dropped deals", ["Customer", "Outcome", "Step reached", "Reason", "Closed by", "Closed on"],
                    [[x["name"], x["outcome_label"], x["stage"], x["reason"], x["by"], x["date"]] for x in s["rows"]]))
        out.append(("Reasons given", ["Reason", "Deals"], [[x["label"], x["n"]] for x in s["reasons"]]))
    s = sections.get("team")
    if s and not s.get("error"):
        out.append(("Team performance", ["Person", "Open leads", "Became deals", "Steps moved", "Emails", "WhatsApp",
                                         "Calls", "Notes", "Demos", "Attended", "No-show", "Won", "Win rate", "Open value"],
                    [[x["name"], x["leads"], x["deals"], x["moves"], x["emails"], x["wa"], x["calls"], x["notes"],
                      x.get("demos", 0), x.get("demos_attended", 0), x.get("demos_no_show", 0),
                      x["won"], x["rate"], x["value"]] for x in s["rows"]]))
    s = sections.get("sources")
    if s and not s.get("error"):
        out.append(("Where leads came from", ["Source", "Leads", "Became deals", "Won", "Lead → won", "Won value"],
                    [[x["name"], x["leads"], x["deals"], x["won"], x["conv"], x["value"]] for x in s["rows"]]))
    s = sections.get("attention")
    if s and not s.get("error"):
        out.append(("Deals needing attention", ["Customer", "Step", "Assigned to", "Days without moving",
                                                "Last activity", "Deal value"],
                    [[x["name"], x["stage"], x["who"], x["days"], x["last"], x["value"]] for x in s["rows"]]))
    s = sections.get("contacts")
    if s and not s.get("error"):
        out.append(("Contacts & Companies", ["Measure", "Number"],
                    [["New contacts", s["new_contacts"]], ["New companies", s["new_companies"]],
                     ["Became customers", s["customers"]]] + [["Contacts: " + x["label"], x["n"]] for x in s["statuses"]]))
        out.append(("Top companies", ["Company", "People", "Open deals", "Open value"],
                    [[x["name"], x["people"], x["deals"], x["value"]] for x in s["companies"]]))
    s = sections.get("campaigns")
    if s and not s.get("error"):
        out.append(("Campaigns", ["Campaign", "Sent", "Replied", "Reply rate", "Became deals", "Won value"],
                    [[x["name"], x["sent"], x["replied"], x["rate"], x["deals"], x["value"]] for x in s["rows"]]))
    return out


def export_xlsx(title: str, subtitle: str, table_headers: list, table_rows: list, sections: dict) -> bytes:
    """One sheet for "Your table", one per ready-made section. Write-only
    workbook: 20,000 rows × 122 columns in seconds, not half a minute."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook(write_only=True)
    navy = PatternFill("solid", fgColor="0B1D40")
    used = set()

    def sheet(name, headers, rows):
        n = name[:31].replace("/", "-")
        while n in used:
            n = n[:28] + " 2"
        used.add(n)
        ws = wb.create_sheet(n)
        for i, h in enumerate(headers, 1):
            longest = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:300]])
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, longest + 2))
        ws.freeze_panes = "A5"
        t = WriteOnlyCell(ws, value=name)
        t.font = Font(bold=True, size=14, color="0B1D40")
        s = WriteOnlyCell(ws, value=subtitle)
        s.font = Font(size=10, color="4E5F7A")
        ws.append([t])
        ws.append([s])
        ws.append([])
        head = []
        for h in headers:
            c = WriteOnlyCell(ws, value=h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = navy
            head.append(c)
        ws.append(head)
        for row in rows:
            ws.append(list(row))

    sheet("Your table", table_headers or ["(no columns ticked)"], table_rows)
    for name, headers, rows in section_tables(sections):
        sheet(name, headers, rows)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


PDF_MAX_COLS = 10


def export_pdf(title: str, subtitle: str, table_headers: list, table_rows: list, sections: dict) -> bytes:
    """A4 landscape: every shown section, then "Your table" (first
    PDF_MAX_COLS columns — Excel and CSV carry them all)."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font, bold = "Helvetica", "Helvetica-Bold"
    try:
        if "DejaVuSans" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
        font, bold = "DejaVuSans", "DejaVuSans-Bold"      # has the ₦ sign
    except Exception as e:
        print("⚠️ crm_report pdf font:", e)

    navy = colors.HexColor("#0B1D40")
    h1 = ParagraphStyle("h1", fontName=bold, fontSize=18, textColor=navy, leading=22)
    h2 = ParagraphStyle("h2", fontName=bold, fontSize=12, textColor=navy, leading=16, spaceBefore=10, spaceAfter=4)
    small = ParagraphStyle("s", fontName=font, fontSize=9, textColor=colors.HexColor("#4E5F7A"), leading=12)
    cell = ParagraphStyle("c", fontName=font, fontSize=7.5, leading=9.5)
    head = ParagraphStyle("hd", fontName=bold, fontSize=7.5, leading=9.5, textColor=colors.white)

    def esc(v):
        return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    page_w = landscape(A4)[0] - 24 * mm

    def table(headers, rows):
        data = [[Paragraph(esc(h), head) for h in headers]]
        data += [[Paragraph(esc(v), cell) for v in r] for r in rows] or \
                [[Paragraph("Nothing in this period.", cell)] + [""] * (len(headers) - 1)]
        tb = Table(data, colWidths=[page_w / len(headers)] * len(headers), repeatRows=1)
        tb.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), navy),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F6FB")]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#DCE3EF")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return tb

    story = [Paragraph(esc(title), h1), Paragraph(esc(subtitle), small), Spacer(1, 6)]
    for name, headers, rows in section_tables(sections):
        story += [Paragraph(esc(name), h2), table(headers, rows)]
    if table_headers:
        story.append(Paragraph("Your table", h2))
        heads, rows = table_headers, table_rows
        if len(heads) > PDF_MAX_COLS:
            story.append(Paragraph(f"Showing the first {PDF_MAX_COLS} of {len(heads)} columns — "
                                   "download Excel or CSV for all of them.", small))
            heads, rows = heads[:PDF_MAX_COLS], [r[:PDF_MAX_COLS] for r in rows]
        if len(rows) > 2000:
            story.append(Paragraph(f"Showing the first 2,000 of {len(rows):,} rows — "
                                   "download Excel or CSV for all of them.", small))
            rows = rows[:2000]
        story.append(table(heads, rows))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=title)
    doc.build(story)
    return buf.getvalue()
