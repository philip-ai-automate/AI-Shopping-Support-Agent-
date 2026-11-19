"""Plain words for reports (19 Nov 2026, approved mock-up EPpwZTeFt5iccD8SH155P1 screens 8–10).

The engine (report_builder.py) speaks in subjects, measures, groupings and
filters. People don't. This layer turns those into:
  * the "Create a report" wizard: What to count · Which … · Split it by · Time · Show it as
  * the sentence on a report, where each underlined part opens its choices:
      Show [the total quote value of] [won quotes], split by [team member], for [this quarter], [compared with the period before].
Nothing here reads data; it only describes settings.
"""
import report_builder as RB

# ── "Which …?" quick picks: (key, button label, words in the sentence, filters) ──
WHICH = {
    "quotes": [("all", "All quotes", "all quotes", []), ("won", "Won", "won quotes", ["qstatus|is|won"]),
               ("lost", "Lost", "lost quotes", ["qstatus|is|lost"]),
               ("waiting", "Waiting for an answer", "quotes waiting for an answer", ["qstatus|is|quote_sent"]),
               ("toquote", "Not quoted yet", "requests not quoted yet", ["qstatus|is|to_quote"])],
    "deals": [("all", "All deals", "all deals", []), ("won", "Won", "won deals", ["outcome|is|won"]),
              ("lost", "Lost", "lost deals", ["outcome|is|lost"]), ("open", "Still open", "open deals", ["outcome|is|open_deal"])],
    "leads": [("all", "All enquiries", "all enquiries", []), ("deal", "Became a deal", "enquiries that became a deal", ["isdeal|is|Deal"]),
              ("notdeal", "Not a deal yet", "enquiries not a deal yet", ["isdeal|is|Lead"]),
              ("notafit", "Not a fit", "enquiries marked Not a fit", ["outcome|is|not_a_fit"])],
    "contacts": [("all", "All contacts", "all contacts", []), ("customer", "Customers", "customers", ["cstatus|is|customer"]),
                 ("lead", "Leads", "contacts who are leads", ["cstatus|is|lead"]), ("prospect", "Prospects", "prospects", ["cstatus|is|prospect"])],
    "conversations": [("all", "All conversations", "all conversations", []),
                      ("waiting", "Waiting for our reply", "conversations waiting for our reply", ["vwait|is|true"]),
                      ("handed", "Handed to staff", "conversations handed to staff", ["vhanded|is|true"]),
                      ("ai", "AI Agent only", "conversations the AI Agent handled alone", ["vai|is|true"])],
    "calls": [("all", "All calls", "all calls", []), ("answered", "Answered", "answered calls", ["kres|is|answered"]),
              ("noanswer", "No answer", "calls with no answer", ["kres|is|no_answer"]),
              ("whatsapp", "WhatsApp calls", "WhatsApp calls", ["ktype|is|whatsapp"])],
    "campaigns": [("all", "All messages", "all campaign messages", []), ("replied", "Replied", "campaign messages that got a reply", ["mreplied|is|true"]),
                  ("email", "Email", "email campaign messages", ["mch|is|email"]), ("wa", "WhatsApp", "WhatsApp campaign messages", ["mch|is|whatsapp"])],
    "sequences": [("all", "Everyone", "everyone in email sequences", []), ("active", "Still active", "people still in a sequence", ["sst|is|active"]),
                  ("finished", "Finished", "people who finished a sequence", ["sst|is|finished"]),
                  ("stopped", "Stopped", "people who were stopped", ["sst|is|stopped"])],
    "followups": [("all", "All follow-ups", "all follow-ups", []), ("overdue", "Overdue", "overdue follow-ups", ["fstatus|is|overdue"]),
                  ("done", "Done", "follow-ups done", ["fstatus|is|done"]), ("upcoming", "Coming up", "follow-ups coming up", ["fstatus|is|upcoming"])],
    "activity": [("all", "Everything", "every team action", []), ("manual", "Done by hand", "actions done by hand", ["aauto|is|false"])],
    "ai": [("all", "All replies", "AI Agent replies", [])],
    "orders": [("all", "All orders", "all orders", []), ("paid", "Paid", "paid orders", ["opaidon|set|"]),
               ("awaiting", "Awaiting payment", "orders awaiting payment", ["ost|is|PAYMENT_PENDING"]),
               ("delivered", "Delivered", "delivered orders", ["ost|is|DELIVERED"])],
    "products": [("all", "All products sold", "products sold", [])],
    "discounts": [("all", "All codes", "discount codes given", []), ("used", "Used", "codes used in an order", ["dused|is|true"]),
                  ("unused", "Not used", "codes not used", ["dused|is|false"])],
    "carts": [("all", "Everything", "cart events", []), ("shown", "Reminder shown", "reminders shown", ["eev|is|recovery_popup_shown"])],
}

# ── "Split it by" choices: (grouping, label, help) ──────────────────────────
SPLITS = {
    "quotes": [("qassigned", "Team member", "Who it's assigned to"), ("requested@month", "Month", "See the trend"),
               ("qstatus", "Status", "Won, lost, waiting"), ("qchannel", "Where it came from", "Email, WhatsApp, website chat…"),
               ("bcompany", "Buyer company", "Your biggest buyers"), ("bcountry", "Buyer country", "Where they are"),
               ("ivst", "Invoice status", "Paid, awaiting payment, overdue")],
    "deals": [("assigned", "Team member", "Who owns the deal"), ("becamedeal@month", "Month", "See the trend"),
              ("stage", "Stage", "Where deals sit"), ("outcome", "Outcome", "Won, lost, open"),
              ("source", "Where the lead came from", "WhatsApp, imported list…"), ("reason", "Reason lost", "Why deals were lost")],
    "leads": [("source", "Where they came from", "WhatsApp, Instagram, imported…"), ("created@month", "Month", "See the trend"),
              ("assigned", "Team member", "Who it's assigned to"), ("channel", "First contacted by", "Email, phone, WhatsApp"),
              ("country", "Country", "Where they are"), ("isdeal", "Lead or deal", "How many became deals")],
    "contacts": [("cstatus", "Contact status", "Lead, prospect, customer"), ("csource", "How they were added", "WhatsApp, by hand, import"),
                 ("ccountry", "Country", "Where they are"), ("coname", "Company", "Contacts per company"),
                 ("cadded@month", "Month added", "See the trend"), ("caddedby", "Added by", "Who added them")],
    "conversations": [("vch", "Channel", "WhatsApp, website chat, Facebook"), ("vstart@week", "Week", "See the trend"),
                      ("vhanded", "Handed to staff", "Yes or no"), ("vwait", "Waiting for our reply", "Yes or no")],
    "calls": [("kwho", "Team member", "Who made or took the call"), ("kres", "Result", "Answered, no answer, busy…"),
              ("ktype", "Type", "Logged or WhatsApp call"), ("kdate@week", "Week", "See the trend")],
    "campaigns": [("mcamp", "Campaign", "Each campaign"), ("mch", "Channel", "Email or WhatsApp"), ("mst", "Status", "Delivered, read, replied…"),
                  ("msent@week", "Week", "See the trend")],
    "sequences": [("sseq", "Sequence", "Each sequence"), ("sst", "Status", "Active, finished, stopped")],
    "followups": [("fstatus", "Status", "Done, overdue, coming up"), ("fassigned", "Team member", "Whose deals"),
                  ("fdue@week", "Week due", "See the workload"), ("fchannel", "How", "Call, email, WhatsApp")],
    "activity": [("awho", "Team member", "Who did it"), ("aact", "Action", "Calls, messages, stage moves…"), ("adate@week", "Week", "See the trend")],
    "ai": [("udate@week", "Week", "See the trend"), ("usite", "Website", "Each website"), ("uplan", "Plan", "Paid or trial")],
    "orders": [("ost", "Status", "Paid, dispatched, delivered…"), ("odate@week", "Week", "See the trend"), ("opay", "Payment method", "How they paid")],
    "products": [("pname", "Product", "Each product"), ("pdate@month", "Month", "See the trend")],
    "discounts": [("dused", "Used or not", "Did buyers use them"), ("dkind", "Kind", "Percent, amount, free delivery"),
                  ("dby", "Given by", "AI Agent or a person"), ("ddate@month", "Month", "See the trend")],
    "carts": [("eev", "What happened", "Added to cart, tried to leave…"), ("edate@week", "Week", "See the trend")],
}

# ── The ready-made reports as plain questions (wizard step 1) ───────────────
QUESTIONS = {
    "quotes_wonlost": "Won, lost or still waiting?", "quotes_by_member": "Who wins the most quotes?",
    "quotes_waiting": "Which quotes are still waiting for an answer?", "quote_revenue": "How much have won quotes been paid?",
    "overdue_invoices": "Which invoices are overdue?", "quotes_pivot": "Quote value per person, month by month",
    "deal_funnel": "How many deals sit at each stage?", "where_enquiries": "Where do our enquiries come from?",
    "not_a_fit": "Why are enquiries not a fit?", "missed_replies": "Which chats are waiting for our reply?",
    "handovers": "How often does the AI Agent hand over to staff?", "first_reply": "How fast do we first reply?",
    "calls": "How many calls get answered?", "campaigns": "Which campaigns got replies?", "sequences": "Who finished our email sequences?",
    "followups": "Are follow-ups done on time?", "team_activity": "What did each team member do?", "ai_usage": "How much did the AI Agent answer?",
    "orders_week": "How many orders do we get each week?", "orders_status": "Where are our orders up to?",
    "top_products": "Which products sell most?", "discount_codes": "Do buyers use the discount codes we give?",
    "cart_recovery": "What happens to carts left behind?",
    "new_wa_customers": "How many new customers message us on WhatsApp?",
}

# Reports home: the ready-made questions in topic tabs (approved mock-up screen 11)
TOPICS = [("sales", "Sales & quotes", ["quotes_by_member", "quotes_wonlost", "quotes_waiting", "quote_revenue",
                                        "overdue_invoices", "quotes_pivot", "deal_funnel"]),
          ("chats", "Enquiries & chats", ["where_enquiries", "missed_replies", "new_wa_customers", "handovers",
                                          "first_reply", "not_a_fit"]),
          ("team", "Team", ["team_activity", "followups", "calls"]),
          ("marketing", "Marketing", ["campaigns", "sequences", "ai_usage"]),
          ("shop", "Online shop", ["orders_week", "orders_status", "top_products", "discount_codes", "cart_recovery"])]
CHART_ICON = {"bar": "▮▮", "hbar": "▬", "stacked": "▮▮", "line": "⟋", "area": "⟋", "combo": "▮⟋", "pie": "◔",
              "donut": "◔", "funnel": "▤", "number": "#", "heatmap": "▦", "pivot": "⊞", "table": "☰", "auto": "▮▮"}
CHART_WORD = {"bar": "Bar", "hbar": "Bar", "stacked": "Stacked bar", "line": "Line", "area": "Area", "combo": "Bar + line",
              "pie": "Pie", "donut": "Donut", "funnel": "Funnel", "number": "One number", "heatmap": "Heatmap",
              "pivot": "Pivot table", "table": "List", "auto": "Chart"}


def plain_sentence(cfg) -> str:
    """The report's sentence as plain text (Reports home list)."""
    subj = RB.SUBJECTS[cfg["subject"]]
    m = RB.measure_of(cfg)
    which, rest = which_of(cfg)
    s = measure_words(cfg["subject"], m) + " " + which[2]
    s = s[0].upper() + s[1:]
    if cfg["g1"]:
        s += ", split by " + split_words(cfg, cfg["g1"]) + (" and " + split_words(cfg, cfg["g2"]) if cfg["g2"] else "")
    s += ", " + period_words(cfg)
    if cfg.get("cmp") and cfg["period"] != "all":
        s += ", " + dict(COMPARE_WORDS)[cfg["cmp"]]
    if rest:
        s += f" ({len(rest)} condition{'s' if len(rest) > 1 else ''})"
    return s

PERIOD_WORDS = {"this_month": "this month", "last_month": "last month", "this_quarter": "this quarter",
                "last_quarter": "last quarter", "this_year": "this year", "last_year": "last year", "7": "the last 7 days",
                "30": "the last 30 days", "90": "the last 90 days", "all": "all time", "custom": "dates you pick"}
COMPARE_WORDS = [("prev", "compared with the period before"), ("year", "compared with the same time last year"),
                 ("", "with no comparison")]


def _noun(subj_key):
    return RB.SUBJECTS[subj_key]["noun"]


def measure_card(subj_key, m):
    """(label, help) for a What-to-count card."""
    key, label, sql, f = m
    if key == "count":
        return f"Number of {_noun(subj_key)}", "How many"
    if label.startswith("Sum of "):
        return "Total " + label[7:].lower(), "Adds them up"
    if label.startswith("Average "):
        return label[0] + label[1:].lower(), "The typical size"
    return label, ""


def measure_words(subj_key, m):
    """Words in the sentence before the 'which' part, e.g. 'the total quote value of'."""
    key, label, sql, f = m
    if key == "count":
        return "the number of"
    if label.startswith("Sum of "):
        return "the total " + label[7:].lower() + " of"
    if label.startswith("Average days"):
        return "the " + label.lower() + " for"
    if label.startswith("Average "):
        return "the average " + label[8:].lower() + " of"
    return "the " + label.lower() + " for"


def which_of(cfg):
    """(which option, the other filters) — the quick pick the report's filters match."""
    opts = WHICH.get(cfg["subject"], [])
    raw = [f"{f['key']}|{f['op']}|{f['val']}" for f in cfg["f"]]
    best = None
    for o in opts:
        if all(x in raw for x in o[3]) and (best is None or len(o[3]) > len(best[3])):
            best = o
    if best is None:
        best = ("all", "All", "all " + _noun(cfg["subject"]), [])
    rest = [f for f in cfg["f"] if f"{f['key']}|{f['op']}|{f['val']}" not in best[3]]
    return best, rest


def split_words(cfg, g):
    if not g:
        return "nothing"
    S = RB.SUBJECTS[cfg["subject"]]
    for k, label, _h in SPLITS.get(cfg["subject"], []):
        if k == g:
            return label.lower()
    col, _, bucket = g.partition("@")
    lab = S["columns"][col]["label"].lower()
    return (bucket + " (" + lab + ")") if bucket else lab


def period_words(cfg):
    if cfg["period"] == "custom" and cfg["date_from"]:
        return cfg["date_from"].strftime("%d %b %Y") + " to " + cfg["date_to"].strftime("%d %b %Y")
    return PERIOD_WORDS.get(cfg["period"], cfg["period_label"].lower())


def split_choices(subj_key):
    """All groupings for 'And also split by' / more choices: [(value, label)]."""
    S = RB.SUBJECTS[subj_key]
    out = []
    for gname, cols in S["groups"]:
        for c in cols:
            if not c["group"]:
                continue
            if c["fmt"] == "date":
                for b, bl in RB.DATE_BUCKETS:
                    out.append((c["key"] + "@" + b, c["label"] + " " + bl))
            else:
                out.append((c["key"], c["label"]))
    return out


def wizard_spec(allowed: set, ctx) -> dict:
    """Everything the wizard needs, per subject this business may use."""
    subjects = []
    for k in RB.SUBJECT_ORDER:
        if k not in allowed:
            continue
        S = RB.SUBJECTS[k]
        splits = [s for s in SPLITS.get(k, []) if RB._group_ok(S, s[0])]
        subjects.append({
            "key": k, "label": S["label"], "noun": S["noun"], "hint": S["hint"],
            "measures": [{"key": m[0], "label": measure_card(k, m)[0], "help": measure_card(k, m)[1],
                          "words": measure_words(k, m), "additive": m[0] == "count" or m[0].startswith("sum")}
                         for m in S["measures"]],
            "which": [{"key": w[0], "label": w[1], "words": w[2], "f": w[3]} for w in WHICH.get(k, [])],
            "splits": [{"key": s[0], "label": s[1], "help": s[2], "date": "@" in s[0]} for s in splits],
            "more_splits": [{"key": v, "label": l} for v, l in split_choices(k)],
            "dates": [{"key": d[0], "label": d[1]} for d in S["dates"]], "date_default": S["date_default"],
            "fields": [{"key": c["key"], "label": c["label"], "group": g, "type": c["filter"],
                        "options": RB.choice_options(k, c["key"], ctx) if c["filter"] == "choice" else []}
                       for g, cols in S["groups"] for c in cols if c["filter"]],
            "questions": [],
        })
    by = {s["key"]: s for s in subjects}
    for r in RB.READY:
        s = by.get(r["cfg"]["subject"])
        if s:
            s["questions"].append({"key": r["key"], "q": QUESTIONS.get(r["key"], r["label"]), "how": r["desc"],
                                   "label": r["label"], "cfg": r["cfg"]})
    return {"subjects": subjects, "periods": [{"key": k, "label": PERIOD_WORDS[k].capitalize()} for k, l in RB.PERIODS if k != "custom"],
            "period_words": PERIOD_WORDS, "charts": [{"key": k, "label": l} for k, l in RB.CHARTS], "ops": RB.FILTER_OPS}
