"""More report subjects (Reports & Dashboards, phase R5 — 19 Nov 2026).

Registered into report_builder.SUBJECTS when report_builder is imported.
Each subject reads real stored fields only. "gate" names a condition the
routes check per business (report_builder_routes._allowed):
  inbox   — owner, or staff whose role has "See all chats"
  manager — owner, or staff whose role has "See all leads"
  orders  — the business has Products & Orders orders, or a connected shop
  shop    — a connected Shopify / WooCommerce shop, or chat discount codes given
  carts   — WooCommerce cart recovery events recorded

Scope: "leads" runs through the viewer's own-records rule (portal_routes._ls_exec);
"own" keeps staff without "See all leads" to rows whose `own` column is them;
"none" is business-wide (only reached through a gate when it matters).
Message words are never read (encrypted at rest).
"""
import report_builder as RB
from report_builder import _col

RB.LABEL_MAPS.update({
    "lm_chan": {"whatsapp": "WhatsApp", "website": "Website chat", "facebook": "Facebook Messenger"},
    "lm_calltype": {"logged": "Logged call", "whatsapp": "WhatsApp call"},
    "lm_callres": {"answered": "Answered", "no_answer": "No answer", "busy": "Busy", "wrong_number": "Wrong number",
                   "not_needed": "Not needed", "completed": "Completed", "missed": "Missed",
                   "out_of_hours": "Out of hours", "rejected": "Rejected", "failed": "Failed"},
    "lm_dir": {"inbound": "Incoming", "outbound": "Outgoing"},
    "lm_campch": {"whatsapp": "WhatsApp", "email": "Email"},
    "lm_campst": {"sent": "Sent", "delivered": "Delivered", "read": "Read", "replied": "Replied",
                  "interested": "Interested", "not_interested": "Not interested", "opportunity": "Opportunity",
                  "converted": "Converted", "failed": "Failed", "bounced": "Bounced", "suppressed": "Not sent (opted out)"},
    "lm_seqst": {"active": "Active", "sending": "Sending", "finished": "Finished", "stopped": "Stopped"},
    "lm_fust": {"done": "Done", "cancelled": "Cancelled", "overdue": "Overdue", "today": "Due today", "upcoming": "Upcoming"},
    "lm_ordst": {"INTENT_CAPTURED": "Started", "PAYMENT_PENDING": "Awaiting payment", "RECEIPT_RECEIVED": "Receipt received",
                 "PAYMENT_VERIFIED": "Paid", "PROCESSING": "Processing", "DISPATCHED": "Dispatched",
                 "DELIVERED": "Delivered", "CANCELLED": "Cancelled"},
    "lm_cart": {"add_to_cart": "Added to cart", "exit_intent": "Tried to leave", "page_idle": "Stopped on the page",
                "recovery_popup_shown": "Reminder shown", "recovery_popup_clicked": "Reminder clicked",
                "recovery_popup_closed": "Reminder closed"},
    "lm_dckind": {"percentage": "Percent off", "fixed_amount": "Amount off", "free_shipping": "Free delivery"},
    "lm_plan": {"paid": "Paid plan", "trial": "Free trial"},
})


def _subject(key, **kw):
    groups = kw.pop("groups")
    cols = {c["key"]: c for _, cs in groups for c in cs}
    for k in kw.pop("group_too", ()):
        cols[k]["group"] = True
    for k in kw.pop("no_group", ()):
        cols[k]["group"] = False
    RB.SUBJECTS[key] = dict({"b2b": False, "fixed": None, "extras": [], "scope": "none", "cur": None},
                            groups=groups, columns=cols, **kw)
    RB.SUBJECT_ORDER.append(key)


# ── Contacts & companies ─────────────────────────────────────────────────────
def _contacts_from(t, joins):
    sql = ["wa_contacts c", "LEFT JOIN crm_companies co ON co.id = c.company_id"]
    if "cd" in joins:
        sql.append(f"""LEFT JOIN (SELECT wa_contact_id,
                        COUNT(*) FILTER (WHERE COALESCE(outcome, '') NOT IN ('won','lost','dropped','not_a_fit')) AS open_n,
                        COUNT(*) FILTER (WHERE outcome = 'won') AS won_n,
                        SUM(fx_to_selling(deal_value, currency, tenant_id)) FILTER (WHERE outcome = 'won') AS won_v
                   FROM merchant_pipeline_leads WHERE tenant_id = {int(t)} AND wa_contact_id IS NOT NULL
                  GROUP BY wa_contact_id) cd ON cd.wa_contact_id = c.id""")
    return "\n".join(sql)


_subject(
    "contacts", label="Contacts & companies", noun="contacts", id="c.id", tenant="c.tenant_id",
    from_=None, scope="leads", member="caddedby",
    groups=[
        ("Contact", [
            _col("cname", "Name", "c.display_name"),
            _col("cperson", "Contact person", "c.contact_person"),
            _col("cphone", "Phone", "c.phone"),
            _col("cwa", "WhatsApp number", "c.whatsapp_number"),
            _col("cemail", "Email", "c.email"),
            _col("cstatus", "Contact status", "c.status", "contact_status"),
            _col("csource", "How they were added", "c.source", "title"),
            _col("ctags", "Tags", "array_to_string(c.tags, ', ')"),
            _col("ccity", "City", "c.addr_city"),
            _col("ccountry", "Country", "c.addr_country", "country"),
            _col("ccategory", "Business category", "c.business_category"),
            _col("cemployees", "Number of employees", "c.employees"),
            _col("cadded", "Added on", "c.created_at::date", "date"),
            _col("caddedby", "Added by", "c.added_by_key", "assigned"),
            _col("cwaout", "WhatsApp opted out", "COALESCE(c.opted_out, FALSE)", "yesno"),
            _col("cemailout", "Email opted out", "COALESCE(c.email_opted_out, FALSE)", "yesno"),
        ]),
        ("Company", [
            _col("coname", "Company", "co.name"),
            _col("cowebsite", "Company website", "co.website"),
            _col("cocountry", "Company country", "co.addr_country", "country"),
        ]),
        ("Deals", [
            _col("copen", "Open deals", "COALESCE(cd.open_n, 0)", "int", ["cd"]),
            _col("cwon", "Won deals", "COALESCE(cd.won_n, 0)", "int", ["cd"]),
            _col("cwonv", "Won value", "COALESCE(cd.won_v, 0)", "money", ["cd"]),
        ]),
    ],
    group_too=("coname", "ccategory", "cemployees", "ccity"),
    no_group=("cname", "cperson", "cphone", "cwa", "cemail", "ctags", "cowebsite"),
    measures=[("count", "Number of contacts", None, "int"),
              ("sum_wonv", "Sum of Won value", "SUM(COALESCE(cd.won_v, 0))", "money")],
    dates=[("cadded", "Added on", "c.created_at::date")], date_default="cadded",
    default_cols=["cname", "coname", "cphone", "cemail", "cstatus", "ccountry", "cadded"],
    default_sort=("cadded", "desc"), hint="Every contact and the company they belong to.",
)
RB.SUBJECTS["contacts"]["from"] = _contacts_from
# Won value needs its join even as a measure.
RB.SUBJECTS["contacts"]["measure_joins"] = {"sum_wonv": ["cd"]}


# ── Conversations (WhatsApp, website chat, Facebook) ─────────────────────────
def _conv_from(t, joins):
    t = int(t)
    return f"""(
  SELECT 'whatsapp'::text AS ch, w.tenant_id, w.customer_phone::text AS who,
         MIN(w.created_at) AS first_at, MAX(w.created_at) AS last_at,
         COUNT(*) FILTER (WHERE w.direction = 'inbound') AS n_in,
         COUNT(*) FILTER (WHERE w.direction = 'outbound') AS n_out,
         COUNT(*) FILTER (WHERE w.direction = 'outbound' AND COALESCE(w.sent_by_label, '') <> '') AS n_staff,
         (array_agg(w.direction ORDER BY w.created_at DESC))[1] = 'inbound' AS waiting,
         MIN(w.created_at) FILTER (WHERE w.direction = 'outbound') AS first_out,
         EXISTS (SELECT 1 FROM wa_handoff_state h WHERE h.tenant_id = {t} AND h.customer_phone = w.customer_phone) AS handed
    FROM wa_message_log w WHERE w.tenant_id = {t} AND NOT COALESCE(w.is_historical, FALSE)
   GROUP BY w.tenant_id, w.customer_phone
  UNION ALL
  SELECT 'website', m.tenant_id, m.session_id::text,
         MIN(m.created_at), MAX(m.created_at),
         COUNT(*) FILTER (WHERE m.role = 'user'), COUNT(*) FILTER (WHERE m.role = 'assistant')
           + COALESCE((SELECT COUNT(*) FROM web_chat_replies r WHERE r.tenant_id = {t} AND r.session_id = m.session_id), 0),
         COALESCE((SELECT COUNT(*) FROM web_chat_replies r WHERE r.tenant_id = {t} AND r.session_id = m.session_id), 0),
         (array_agg(m.role ORDER BY m.created_at DESC))[1] = 'user',
         MIN(m.created_at) FILTER (WHERE m.role = 'assistant'),
         EXISTS (SELECT 1 FROM handoff_requests h WHERE h.tenant_id = {t} AND h.session_id = m.session_id)
    FROM chat_messages m WHERE m.tenant_id = {t}
   GROUP BY m.tenant_id, m.session_id
  UNION ALL
  SELECT 'facebook', f.tenant_id, f.psid,
         MIN(f.created_at), MAX(f.created_at),
         COUNT(*) FILTER (WHERE f.direction = 'inbound'), COUNT(*) FILTER (WHERE f.direction = 'outbound'),
         COUNT(*) FILTER (WHERE f.direction = 'outbound' AND COALESCE(f.sent_by_label, '') <> ''),
         (array_agg(f.direction ORDER BY f.created_at DESC))[1] = 'inbound',
         MIN(f.created_at) FILTER (WHERE f.direction = 'outbound'), FALSE
    FROM fb_message_log f WHERE f.tenant_id = {t}
   GROUP BY f.tenant_id, f.psid
) cv"""


_subject(
    "conversations", label="Conversations", noun="conversations", id="cv.who", tenant="cv.tenant_id",
    gate="inbox",
    groups=[("Conversation", [
        _col("vch", "Channel", "cv.ch", "lm_chan"),
        _col("vwho", "Customer (number or chat)", "cv.who"),
        _col("vstart", "Started on", "cv.first_at::date", "date"),
        _col("vlast", "Last message on", "cv.last_at::date", "date"),
        _col("vin", "Messages from the customer", "cv.n_in", "int"),
        _col("vout", "Replies sent", "cv.n_out", "int"),
        _col("vstaff", "Replies by staff", "cv.n_staff", "int"),
        _col("vai", "Answered by AI Agent only", "(cv.n_staff = 0 AND NOT cv.handed)", "yesno"),
        _col("vhanded", "Handed to staff", "cv.handed", "yesno"),
        _col("vwait", "Waiting for our reply", "cv.waiting", "yesno"),
        _col("vfirst", "First reply (minutes)",
             "CASE WHEN cv.first_out IS NOT NULL THEN GREATEST(0, EXTRACT(EPOCH FROM cv.first_out - cv.first_at) / 60) END", "int"),
    ])],
    measures=[("count", "Number of conversations", None, "int"),
              ("avg_first", "Average first reply (minutes)",
               "AVG(GREATEST(0, EXTRACT(EPOCH FROM cv.first_out - cv.first_at) / 60))", "int"),
              ("sum_in", "Messages from customers", "SUM(cv.n_in)", "int")],
    extras=[("Handed to staff", "COUNT(*) FILTER (WHERE cv.handed)", "int"),
            ("Waiting for our reply", "COUNT(*) FILTER (WHERE cv.waiting)", "int")],
    dates=[("vstart", "Started on", "cv.first_at::date"), ("vlast", "Last message on", "cv.last_at::date")],
    date_default="vstart", default_cols=["vch", "vwho", "vstart", "vlast", "vin", "vout", "vhanded", "vwait"],
    default_sort=("vlast", "desc"), hint="WhatsApp, website chat and Facebook conversations — who answered, who is waiting.",
    no_group=("vwho",),
)
RB.SUBJECTS["conversations"]["from"] = _conv_from


# ── Calls (logged calls + WhatsApp calls) ────────────────────────────────────
def _calls_from(t, joins):
    t = int(t)
    return f"""(
  SELECT 'logged'::text AS kind, lc.tenant_id, lc.id::text AS cid, lc.created_at, lc.outcome::text AS res,
         'outbound'::text AS dir, lc.actor_key::text AS who_key, COALESCE(lc.minutes, 0) * 60 AS secs,
         l.customer_name::text AS customer
    FROM lead_calls lc LEFT JOIN merchant_pipeline_leads l ON l.id = lc.lead_id
   WHERE lc.tenant_id = {t}
  UNION ALL
  SELECT 'whatsapp', wc.tenant_id, 'w' || wc.id, wc.created_at, wc.status::text, wc.direction::text,
         COALESCE(wc.answered_by_key, wc.started_by_key, wc.callback_by_key)::text, COALESCE(wc.duration, 0),
         COALESCE(wc.customer_name, wc.customer_phone)::text
    FROM wa_calls wc WHERE wc.tenant_id = {t}
) k"""


_subject(
    "calls", label="Calls", noun="calls", id="k.cid", tenant="k.tenant_id", scope="own", own="k.who_key",
    member="kwho",
    groups=[("Call", [
        _col("ktype", "Type", "k.kind", "lm_calltype"),
        _col("kdir", "Direction", "k.dir", "lm_dir"),
        _col("kres", "Result", "k.res", "lm_callres"),
        _col("kwho", "Team member", "k.who_key", "assigned"),
        _col("kcust", "Customer", "k.customer"),
        _col("kdate", "Date", "k.created_at::date", "date"),
        _col("kmin", "Minutes", "ROUND(k.secs / 60.0)", "int"),
    ])],
    measures=[("count", "Number of calls", None, "int"),
              ("sum_min", "Total minutes", "SUM(k.secs) / 60.0", "int"),
              ("avg_min", "Average minutes", "AVG(k.secs) / 60.0", "int")],
    extras=[("Answered", "COUNT(*) FILTER (WHERE k.res IN ('answered','completed'))", "int"),
            ("Missed", "COUNT(*) FILTER (WHERE k.res IN ('missed','no_answer','out_of_hours','busy'))", "int")],
    dates=[("kdate", "Date", "k.created_at::date")], date_default="kdate",
    default_cols=["kdate", "ktype", "kdir", "kres", "kwho", "kcust", "kmin"], default_sort=("kdate", "desc"),
    hint="Calls logged on deals and WhatsApp calls — answered, missed, minutes.", no_group=("kcust",),
)
RB.SUBJECTS["calls"]["from"] = _calls_from


# ── Campaign messages (WhatsApp + email) ─────────────────────────────────────
def _camp_from(t, joins):
    t = int(t)
    return f"""(
  SELECT 'whatsapp'::text AS ch, r.tenant_id, 'w' || r.id AS rid, wc.name::text AS camp, r.phone::text AS who,
         r.status::text AS st, r.sent_at, NULL::timestamptz AS opened_at, NULL::timestamptz AS clicked_at,
         r.replied_at, (r.pipeline_lead_id IS NOT NULL) AS lead_made
    FROM wa_campaign_recipients r JOIN wa_campaigns wc ON wc.id = r.campaign_id
   WHERE r.tenant_id = {t}
  UNION ALL
  SELECT 'email', r.tenant_id, 'e' || r.id, ec.name::text, r.email::text, r.status::text, r.sent_at, r.opened_at,
         r.clicked_at, r.replied_at, FALSE
    FROM email_campaign_recipients r JOIN email_campaigns ec ON ec.id = r.campaign_id
   WHERE r.tenant_id = {t}
) cm"""


_subject(
    "campaigns", label="Campaign messages", noun="messages", id="cm.rid", tenant="cm.tenant_id", gate="manager",
    groups=[("Campaign message", [
        _col("mch", "Channel", "cm.ch", "lm_campch"),
        _col("mcamp", "Campaign", "cm.camp"),
        _col("mwho", "Sent to", "cm.who"),
        _col("mst", "Status", "cm.st", "lm_campst"),
        _col("msent", "Sent on", "cm.sent_at::date", "date"),
        _col("mopened", "Opened (email)", "(cm.opened_at IS NOT NULL)", "yesno"),
        _col("mopenedon", "Opened on", "cm.opened_at::date", "date"),
        _col("mclicked", "Clicked (email)", "(cm.clicked_at IS NOT NULL)", "yesno"),
        _col("mreplied", "Replied", "(cm.replied_at IS NOT NULL)", "yesno"),
        _col("mrepliedon", "Replied on", "cm.replied_at::date", "date"),
        _col("mlead", "Became a lead", "cm.lead_made", "yesno"),
    ])],
    group_too=("mcamp",), no_group=("mwho",),
    measures=[("count", "Number of messages", None, "int")],
    extras=[("Opened", "COUNT(*) FILTER (WHERE cm.opened_at IS NOT NULL)", "int"),
            ("Replied", "COUNT(*) FILTER (WHERE cm.replied_at IS NOT NULL)", "int"),
            ("Reply rate", "100.0 * COUNT(*) FILTER (WHERE cm.replied_at IS NOT NULL) / NULLIF(COUNT(*), 0)", "pct")],
    dates=[("msent", "Sent on", "cm.sent_at::date")], date_default="msent",
    default_cols=["mcamp", "mch", "mwho", "mst", "msent", "mreplied"], default_sort=("msent", "desc"),
    hint="Every WhatsApp and email campaign message — delivered, opened, replied.",
)
RB.SUBJECTS["campaigns"]["from"] = _camp_from


# ── Email sequences ──────────────────────────────────────────────────────────
_subject(
    "sequences", label="Email sequences", noun="people", id="sp.id", tenant="sp.tenant_id", gate="manager",
    groups=[("Person in a sequence", [
        _col("sseq", "Sequence", "sq.name"),
        _col("semail", "Email", "sp.email"),
        _col("sst", "Status", "sp.status", "lm_seqst"),
        _col("ssteps", "Emails sent", "COALESCE(sp.steps_sent, 0)", "int"),
        _col("sadded", "Added on", "sp.added_at::date", "date"),
        _col("saddedby", "Added by", "sp.added_by", "person"),
        _col("slast", "Last email on", "sp.last_sent_at::date", "date"),
        _col("snext", "Next email due", "sp.next_due_at::date", "date"),
        _col("sstop", "Stopped because", "sp.stop_reason"),
    ])],
    group_too=("sseq", "sstop"), no_group=("semail",),
    measures=[("count", "Number of people", None, "int"),
              ("avg_steps", "Average emails sent", "AVG(COALESCE(sp.steps_sent, 0))", "int")],
    extras=[("Finished", "COUNT(*) FILTER (WHERE sp.status = 'finished')", "int"),
            ("Stopped", "COUNT(*) FILTER (WHERE sp.status = 'stopped')", "int")],
    dates=[("sadded", "Added on", "sp.added_at::date"), ("slast", "Last email on", "sp.last_sent_at::date")],
    date_default="sadded", default_cols=["sseq", "semail", "sst", "ssteps", "sadded", "slast"],
    default_sort=("sadded", "desc"), hint="Who is in which email sequence, how far they got and why they stopped.",
)
RB.SUBJECTS["sequences"]["from"] = lambda t, joins: "email_sequence_people sp JOIN email_sequences sq ON sq.id = sp.sequence_id"


# ── Follow-ups & tasks ───────────────────────────────────────────────────────
_FU = ("CASE WHEN f.done_at IS NOT NULL THEN 'done' WHEN f.cancelled_at IS NOT NULL THEN 'cancelled' "
       "WHEN f.due_date < CURRENT_DATE THEN 'overdue' WHEN f.due_date = CURRENT_DATE THEN 'today' ELSE 'upcoming' END")
_subject(
    "followups", label="Follow-ups & tasks", noun="follow-ups", id="f.id", tenant="f.tenant_id", scope="leads",
    member="fassigned", cur="l.currency",
    groups=[("Follow-up", [
        _col("fstatus", "Status", _FU, "lm_fust"),
        _col("fdue", "Due on", "f.due_date", "date"),
        _col("fchannel", "How", "f.channel", "title"),
        _col("fauto", "Set automatically", "COALESCE(f.auto, FALSE)", "yesno"),
        _col("fsetby", "Set by", "f.set_by_name", "person"),
        _col("fdone", "Done on", "f.done_at::date", "date"),
        _col("fdoneby", "Done by", "f.done_by_name", "person"),
        _col("fdonehow", "What was done", "f.done_action", "title"),
        _col("fmoved", "Times moved", "COALESCE(f.reschedule_count, 0)", "int"),
        _col("flate", "Days late", "CASE WHEN f.done_at IS NULL AND f.cancelled_at IS NULL AND f.due_date < CURRENT_DATE "
                                    "THEN CURRENT_DATE - f.due_date END", "int"),
        _col("fset", "Set on", "f.created_at::date", "date"),
    ]), ("Deal", [
        _col("fcust", "Customer", "l.customer_name"),
        _col("fassigned", "Assigned to", "l.assigned_key", "assigned"),
        _col("fstage", "Stage", "l.stage", "stage"),
    ])],
    no_group=("fcust",),
    measures=[("count", "Number of follow-ups", None, "int"),
              ("avg_moved", "Average times moved", "AVG(COALESCE(f.reschedule_count, 0))", "int")],
    extras=[("Done", "COUNT(*) FILTER (WHERE f.done_at IS NOT NULL)", "int"),
            ("Overdue", "COUNT(*) FILTER (WHERE f.done_at IS NULL AND f.cancelled_at IS NULL AND f.due_date < CURRENT_DATE)", "int")],
    dates=[("fdue", "Due on", "f.due_date"), ("fdone", "Done on", "f.done_at::date"), ("fset", "Set on", "f.created_at::date")],
    date_default="fdue", default_cols=["fdue", "fstatus", "fcust", "fassigned", "fchannel", "fdone"],
    default_sort=("fdue", "asc"), hint="Follow-ups on deals — done, overdue, who did them.",
)
RB.SUBJECTS["followups"]["from"] = lambda t, joins: "lead_followups f JOIN merchant_pipeline_leads l ON l.id = f.lead_id"


# ── Team activity ────────────────────────────────────────────────────────────
_subject(
    "activity", label="Team activity", noun="actions", id="a.id", tenant="a.tenant_id", scope="own", own="a.actor_key",
    member="awho",
    groups=[("Action", [
        _col("awho", "Team member", "a.actor_key", "assigned"),
        _col("aact", "Action", "a.action", "action"),
        _col("adate", "Date", "a.created_at::date", "date"),
        _col("aqty", "How many", "COALESCE(a.qty, 1)", "int"),
        _col("aauto", "Done automatically", "COALESCE(a.auto, FALSE)", "yesno"),
        _col("areply", "Message to a customer", "(a.action IN ('wa_sent','messenger_sent','webchat_sent','email_sent'))", "yesno"),
        _col("acust", "Customer", "l.customer_name"),
    ])],
    group_too=("aact",), no_group=("acust",),
    measures=[("count", "Number of actions", None, "int"), ("sum_qty", "Total (how many)", "SUM(COALESCE(a.qty, 1))", "int")],
    dates=[("adate", "Date", "a.created_at::date")], date_default="adate",
    default_cols=["adate", "awho", "aact", "acust", "aqty"], default_sort=("adate", "desc"),
    hint="What each team member did — calls, messages, emails, stage moves.",
)
RB.SUBJECTS["activity"]["from"] = lambda t, joins: "staff_activity a LEFT JOIN merchant_pipeline_leads l ON l.id = a.lead_id"


# ── AI Agent usage ───────────────────────────────────────────────────────────
_subject(
    "ai", label="AI Agent usage", noun="AI Agent replies", id="u.id", tenant="u.tenant_id", gate="manager",
    groups=[("AI Agent reply", [
        _col("udate", "Date", "u.created_at::date", "date"),
        _col("usite", "Website", "u.website"),
        _col("uplan", "Plan", "u.key_type", "lm_plan"),
        _col("utok", "Words used (tokens)", "COALESCE(u.used_tokens, 0)", "int"),
        _col("uconv", "Conversation", "u.session_id"),
    ])],
    group_too=("usite",), no_group=("uconv",),
    measures=[("count", "Number of AI Agent replies", None, "int"),
              ("sum_tok", "Tokens used", "SUM(COALESCE(u.used_tokens, 0))", "int")],
    extras=[("Conversations", "COUNT(DISTINCT u.session_id)", "int")],
    dates=[("udate", "Date", "u.created_at::date")], date_default="udate",
    default_cols=["udate", "usite", "uplan", "utok"], default_sort=("udate", "desc"),
    hint="How much the AI Agent answered, by day and website.",
)
RB.SUBJECTS["ai"]["from"] = lambda t, joins: "usage_events u"


# ── Orders (Products & Orders) and products sold ─────────────────────────────
_OTOT = "fx_to_selling(o.total_amount, o.currency, o.tenant_id)"
_subject(
    "orders", label="Orders", noun="orders", id="o.id", tenant="o.tenant_id", gate="orders", cur="o.currency",
    groups=[("Order", [
        _col("oref", "Order number", "o.reference"),
        _col("ocust", "Customer", "o.customer_name"),
        _col("ophone", "Phone", "o.customer_phone"),
        _col("ost", "Status", "o.status", "lm_ordst"),
        _col("opay", "Payment method", "o.payment_method", "title"),
        _col("otot", "Order total", _OTOT, "money"),
        _col("opaid", "Amount paid", "fx_to_selling(o.amount_paid, o.currency, o.tenant_id)", "money"),
        _col("ofee", "Delivery fee", "fx_to_selling(o.delivery_fee, o.currency, o.tenant_id)", "money"),
        _col("ocur", "Currency", "o.currency"),
        _col("odate", "Ordered on", "o.created_at::date", "date"),
        _col("opaidon", "Paid on", "o.paid_at::date", "date"),
        _col("odisp", "Dispatched on", "o.dispatched_at::date", "date"),
        _col("odeliv", "Delivered on", "o.delivered_at::date", "date"),
        _col("ocourier", "Courier", "o.courier"),
        _col("oitems", "Number of items", "(SELECT COALESCE(SUM(i.quantity), 0) FROM order_items i WHERE i.order_id = o.id)", "int"),
    ])],
    group_too=("ocourier", "ocur"), no_group=("oref", "ocust", "ophone"),
    measures=[("count", "Number of orders", None, "int"), ("sum_tot", "Sum of Order total", f"SUM({_OTOT})", "money"),
              ("avg_tot", "Average order", f"AVG({_OTOT})", "money")],
    extras=[("Paid", "COUNT(*) FILTER (WHERE o.paid_at IS NOT NULL)", "int"),
            ("Paid value", f"COALESCE(SUM({_OTOT}) FILTER (WHERE o.paid_at IS NOT NULL), 0)", "money")],
    dates=[("odate", "Ordered on", "o.created_at::date"), ("opaidon", "Paid on", "o.paid_at::date")],
    date_default="odate", default_cols=["oref", "ocust", "ost", "otot", "odate", "opaidon"], default_sort=("odate", "desc"),
    hint="Orders in Products & Orders — status, totals, paid and delivered.",
)
RB.SUBJECTS["orders"]["from"] = lambda t, joins: "orders o"

_subject(
    "products", label="Products sold", noun="order lines", id="i.id", tenant="o.tenant_id", gate="orders", cur="o.currency",
    groups=[("Product", [
        _col("pname", "Product", "i.product_name"),
        _col("pqty", "Quantity", "COALESCE(i.quantity, 0)", "int"),
        _col("pprice", "Unit price", "fx_to_selling(i.unit_price, o.currency, o.tenant_id)", "money"),
        _col("psub", "Line total", "fx_to_selling(i.subtotal, o.currency, o.tenant_id)", "money"),
    ]), ("Order", [
        _col("pref", "Order number", "o.reference"),
        _col("pst", "Order status", "o.status", "lm_ordst"),
        _col("pdate", "Ordered on", "o.created_at::date", "date"),
    ])],
    group_too=("pname",), no_group=("pref",),
    measures=[("count", "Number of order lines", None, "int"), ("sum_qty", "Quantity sold", "SUM(COALESCE(i.quantity, 0))", "int"),
              ("sum_sub", "Sales value", "SUM(fx_to_selling(i.subtotal, o.currency, o.tenant_id))", "money")],
    dates=[("pdate", "Ordered on", "o.created_at::date")], date_default="pdate",
    default_cols=["pname", "pqty", "psub", "pref", "pdate"], default_sort=("pdate", "desc"),
    fixed="o.status <> 'CANCELLED'", hint="Which products sell — quantity and value (cancelled orders left out).",
)
RB.SUBJECTS["products"]["from"] = lambda t, joins: "order_items i JOIN orders o ON o.id = i.order_id"


# ── Discount codes given in chats ────────────────────────────────────────────
_subject(
    "discounts", label="Discount codes", noun="codes", id="d.id", tenant="d.tenant_id", gate="shop",
    groups=[("Discount code", [
        _col("dcode", "Code", "d.code"),
        _col("dkind", "Kind", "d.kind", "lm_dckind"),
        _col("dval", "Value", "d.value", "int"),
        _col("dby", "Given by", "COALESCE(d.given_by_name, d.given_by)", "person"),
        _col("ddate", "Given on", "d.created_at::date", "date"),
        _col("dends", "Ends on", "d.ends_at::date", "date"),
        _col("dused", "Used in an order", "(d.used_at IS NOT NULL)", "yesno"),
        _col("dusedon", "Used on", "d.used_at::date", "date"),
        _col("dplat", "Shop", "d.platform", "title"),
        _col("dst", "Status", "d.status", "title"),
        _col("demail", "Buyer email", "d.buyer_email"),
    ])],
    no_group=("dcode", "demail"),
    measures=[("count", "Number of codes", None, "int")],
    extras=[("Used", "COUNT(*) FILTER (WHERE d.used_at IS NOT NULL)", "int"),
            ("Use rate", "100.0 * COUNT(*) FILTER (WHERE d.used_at IS NOT NULL) / NULLIF(COUNT(*), 0)", "pct")],
    dates=[("ddate", "Given on", "d.created_at::date"), ("dusedon", "Used on", "d.used_at::date")], date_default="ddate",
    default_cols=["dcode", "dkind", "dval", "dby", "ddate", "dused"], default_sort=("ddate", "desc"),
    hint="Discount codes given in chats and whether buyers used them.",
)
RB.SUBJECTS["discounts"]["from"] = lambda t, joins: "chat_discount_codes d"


# ── Cart recovery (WooCommerce) ──────────────────────────────────────────────
_subject(
    "carts", label="Cart recovery", noun="cart events", id="e.id", tenant="e.tenant_id", gate="carts",
    groups=[("Cart event", [
        _col("eev", "What happened", "e.event_type", "lm_cart"),
        _col("eval", "Cart value", "e.cart_value", "money"),
        _col("eemail", "Buyer email", "e.customer_email"),
        _col("epage", "Page", "e.page_url"),
        _col("edate", "Date", "e.created_at::date", "date"),
        _col("esess", "Visit", "e.session_id"),
    ])],
    no_group=("eemail", "esess"), group_too=("epage",),
    measures=[("count", "Number of events", None, "int"), ("sum_val", "Sum of Cart value", "SUM(COALESCE(e.cart_value, 0))", "money")],
    extras=[("Visits", "COUNT(DISTINCT e.session_id)", "int")],
    dates=[("edate", "Date", "e.created_at::date")], date_default="edate",
    default_cols=["edate", "eev", "eval", "eemail", "epage"], default_sort=("edate", "desc"),
    hint="WooCommerce carts left behind and the reminders shown.",
)
RB.SUBJECTS["carts"]["from"] = lambda t, joins: "cart_events e"

for _k in list(RB.SUBJECTS):
    RB.SUBJECTS[_k].pop("from_", None)

# Tables each subject reads; a subject is only offered when they all exist.
NEEDS = {
    "quotes": ["quote_requests"], "contacts": ["wa_contacts", "crm_companies"],
    "conversations": ["wa_message_log", "chat_messages", "fb_message_log", "wa_handoff_state", "handoff_requests",
                      "web_chat_replies"],
    "calls": ["lead_calls", "wa_calls"], "campaigns": ["wa_campaign_recipients", "wa_campaigns",
                                                       "email_campaign_recipients", "email_campaigns"],
    "sequences": ["email_sequence_people", "email_sequences"], "followups": ["lead_followups"],
    "activity": ["staff_activity"], "ai": ["usage_events"], "orders": ["orders", "order_items"],
    "products": ["orders", "order_items"], "discounts": ["chat_discount_codes"], "carts": ["cart_events"],
}


# ── Ready-made reports (R5) ──────────────────────────────────────────────────
# Opened in the builder unsaved; "Save report" keeps a copy of your own.
# set: b2b (quotes plan), b2c (shop / orders), both. A report only shows when
# the business may use its subject. Left out until the data exists: chat-to-order funnel and who made the sale
# (shop orders tied to chats), products asked about but not bought, questions
# the AI Agent couldn't answer.
RB.READY = [
    {"key": "quotes_wonlost", "set": "b2b", "label": "Quotes won and lost", "desc": "Every quote by its outcome",
     "cfg": {"subject": "quotes", "g1": "qstatus", "view": "summary", "chart": "pie", "period": "this_quarter"}},
    {"key": "quotes_by_member", "set": "b2b", "label": "Quotes won by team member", "desc": "Won value per person",
     "cfg": {"subject": "quotes", "f": "qstatus|is|won", "g1": "qassigned", "m": "sum_qvalue", "view": "summary",
             "chart": "hbar", "period": "this_quarter", "datef": "qclosed"}},
    {"key": "quotes_waiting", "set": "b2b", "label": "Quotes waiting on the buyer", "desc": "Sent, no answer yet — oldest first",
     "cfg": {"subject": "quotes", "f": "qstatus|is|quote_sent", "col": ["ref", "bcompany", "qassigned", "qvalue", "firstsent", "waiting", "opens"],
             "cols_set": "1", "sort": "waiting", "dir": "desc", "period": "all", "chart": "table"}},
    {"key": "quote_revenue", "set": "b2b", "label": "Revenue from won quotes",
     "desc": "Invoiced, paid, overdue — read from Xero, QuickBooks or Sage",
     "cfg": {"subject": "quotes", "f": "qstatus|is|won", "g1": "ivst", "m": "sum_ivamt", "view": "summary",
             "chart": "donut", "period": "this_quarter", "datef": "qclosed"}},
    {"key": "overdue_invoices", "set": "b2b", "label": "Overdue invoices from won quotes",
     "desc": "Read from Xero, QuickBooks or Sage — most overdue first",
     "cfg": {"subject": "quotes", "f": ["qstatus|is|won", "ivst|is|overdue"],
             "col": ["ref", "bcompany", "qassigned", "ivsys", "ivnum", "ivamt", "ivdue", "ivlate"], "cols_set": "1",
             "sort": "ivlate", "dir": "desc", "period": "all", "chart": "table"}},
    {"key": "quotes_pivot", "set": "b2b", "label": "Quotes by team member and month", "desc": "Quote value per person, each month in a column",
     "cfg": {"subject": "quotes", "g1": "qassigned", "g2": "requested@month", "m": "sum_qvalue", "view": "summary",
             "chart": "pivot", "period": "this_year"}},
    {"key": "deal_funnel", "set": "both", "label": "Deals by stage", "desc": "Deals at each stage of the Sales Pipeline",
     "cfg": {"subject": "deals", "g1": "stage", "view": "summary", "chart": "funnel", "period": "all"}},
    # Leads & CRM tab (21 Nov 2026, approved simple-reports mock-up Nbf3F8K2ShDdocrnpbrTGr) — every plan
    {"key": "new_leads", "set": "both", "label": "New leads", "desc": "Week by week, by where they came from",
     "cfg": {"subject": "leads", "g1": "created@week", "g2": "source", "view": "summary", "chart": "stacked",
             "period": "90", "datef": "created"}},
    {"key": "lead_tiers", "set": "both", "label": "Hot, warm and cold leads", "desc": "Open leads by buying intent",
     "cfg": {"subject": "leads", "f": "outcome|is|open_lead", "g1": "tier", "view": "summary", "chart": "pie",
             "period": "all"}},
    {"key": "deals_quiet", "set": "both", "label": "Deals gone quiet", "desc": "Open deals with no activity for 14 days or more",
     "cfg": {"subject": "deals", "f": ["outcome|is|open_deal", "quietdays|gte|14"],
             "col": ["customer", "company", "stage", "assigned", "value", "lastact", "quietdays"], "cols_set": "1",
             "sort": "quietdays", "dir": "desc", "period": "all", "chart": "table"}},
    {"key": "stage_days", "set": "both", "label": "Days in each stage", "desc": "Average days open deals have sat in their stage",
     "cfg": {"subject": "deals", "f": "outcome|is|open_deal", "g1": "stage", "m": "avg_daysstage", "view": "summary",
             "chart": "hbar", "period": "all"}},
    {"key": "open_deals_member", "set": "both", "label": "Open deals by team member", "desc": "Open deals and their value per person",
     "cfg": {"subject": "deals", "f": "outcome|is|open_deal", "g1": "assigned", "m": "sum_value", "view": "summary",
             "chart": "hbar", "period": "all"}},
    {"key": "deals_wonlost", "set": "both", "label": "Deals won and lost", "desc": "Won, lost and dropped deals",
     "cfg": {"subject": "deals", "f": "outcome|not|open_deal", "g1": "outcome", "view": "summary", "chart": "pie",
             "period": "this_quarter", "datef": "closed"}},
    {"key": "new_contacts", "set": "both", "label": "New contacts", "desc": "Month by month, and how they were added",
     "cfg": {"subject": "contacts", "g1": "cadded@month", "g2": "csource", "view": "summary", "chart": "stacked",
             "period": "this_year"}},
    {"key": "where_enquiries", "set": "both", "label": "Where enquiries come from", "desc": "Enquiries by where they came from",
     "cfg": {"subject": "leads", "g1": "source", "view": "summary", "chart": "pie", "period": "this_quarter"}},
    {"key": "not_a_fit", "set": "b2b", "label": "Not a fit", "desc": "Enquiries marked Not a fit, by reason",
     "cfg": {"subject": "leads", "f": "outcome|is|not_a_fit", "g1": "reason", "view": "summary", "chart": "hbar",
             "period": "this_quarter"}},
    {"key": "missed_replies", "set": "both", "label": "Missed and slow replies", "desc": "Chats waiting for a reply, by channel",
     "cfg": {"subject": "conversations", "f": "vwait|is|true", "g1": "vch", "m": "count", "view": "summary",
             "chart": "bar", "period": "30", "datef": "vlast"}},
    {"key": "handovers", "set": "both", "label": "AI Agent hand-overs", "desc": "Chats handed to staff, by channel",
     "cfg": {"subject": "conversations", "f": "vhanded|is|true", "g1": "vch", "view": "summary", "chart": "pie", "period": "30"}},
    {"key": "new_wa_customers", "set": "both", "label": "New WhatsApp customers",
     "desc": "People messaging you on WhatsApp for the first time, per week",
     "cfg": {"subject": "conversations", "f": "vch|is|whatsapp", "g1": "vstart@week", "view": "summary",
             "chart": "bar", "period": "90", "datef": "vstart"}},
    {"key": "first_reply", "set": "both", "label": "First reply time", "desc": "Average minutes to the first reply, by channel",
     "cfg": {"subject": "conversations", "g1": "vch", "m": "avg_first", "view": "summary", "chart": "bar", "period": "30"}},
    {"key": "calls", "set": "both", "label": "Calls", "desc": "Calls by result: answered, no answer, busy",
     "cfg": {"subject": "calls", "g1": "kres", "view": "summary", "chart": "pie", "period": "30"}},
    {"key": "campaigns", "set": "both", "label": "Email & WhatsApp campaigns", "desc": "Sent, opened and replied, per campaign",
     "cfg": {"subject": "campaigns", "g1": "mcamp", "view": "summary", "chart": "table", "period": "90"}},
    {"key": "sequences", "set": "both", "label": "Email sequences", "desc": "Who finished, who stopped",
     "cfg": {"subject": "sequences", "g1": "sseq", "g2": "sst", "view": "summary", "chart": "stacked", "period": "90"}},
    {"key": "followups", "set": "both", "label": "Follow-ups by status", "desc": "Done, overdue and coming up",
     "cfg": {"subject": "followups", "g1": "fstatus", "view": "summary", "chart": "pie", "period": "30"}},
    {"key": "team_activity", "set": "both", "label": "Team activity", "desc": "What each person did",
     "cfg": {"subject": "activity", "g1": "awho", "m": "sum_qty", "view": "summary", "chart": "hbar", "period": "30"}},
    {"key": "ai_usage", "set": "both", "label": "AI Agent usage", "desc": "AI Agent replies per week",
     "cfg": {"subject": "ai", "g1": "udate@week", "view": "summary", "chart": "bar", "period": "90"}},
    {"key": "orders_week", "set": "b2c", "label": "Orders by week", "desc": "Number and value of orders",
     "cfg": {"subject": "orders", "g1": "odate@week", "m": "sum_tot", "view": "summary", "chart": "combo", "period": "90"}},
    {"key": "orders_status", "set": "b2c", "label": "Orders by status", "desc": "Awaiting payment, paid, dispatched, delivered",
     "cfg": {"subject": "orders", "g1": "ost", "view": "summary", "chart": "pie", "period": "90"}},
    {"key": "top_products", "set": "b2c", "label": "Top products sold", "desc": "Quantity sold per product",
     "cfg": {"subject": "products", "g1": "pname", "m": "sum_qty", "view": "summary", "chart": "hbar", "period": "90"}},
    {"key": "discount_codes", "set": "b2c", "label": "Discount codes from chats", "desc": "Given vs used",
     "cfg": {"subject": "discounts", "g1": "dused", "view": "summary", "chart": "donut", "period": "90"}},
    {"key": "cart_recovery", "set": "b2c", "label": "Cart recovery", "desc": "Carts left, reminders shown and clicked",
     "cfg": {"subject": "carts", "g1": "eev", "view": "summary", "chart": "bar", "period": "30"}},
]
READY_BY_KEY = {r["key"]: r for r in RB.READY}


def ready_for(allowed: set, b2b: bool, b2c: bool) -> list:
    """The ready-made reports this business sees: its plan's set plus the shared ones."""
    sets = {"both"} | ({"b2b"} if b2b else set()) | ({"b2c"} if b2c else set())
    return [r for r in RB.READY if r["set"] in sets and r["cfg"]["subject"] in allowed]
