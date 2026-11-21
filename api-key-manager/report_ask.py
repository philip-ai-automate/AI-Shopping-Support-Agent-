"""Ask for a report (21 Nov 2026, approved mock-up Nbf3F8K2ShDdocrnpbrTGr screen 3).

The AI Agent only PICKS one of the business's ready-made reports and fills in the same four
choices a person could set by hand (time, team member, split by, chart or list). It never makes
new reports, columns or numbers — every number still comes from the report itself.

Answers: {"ready": key, "choices": {...}}            found it
         {"closest": [key, …]}                         nothing fits exactly
         {"not_recorded": True, "reason": "…"}         PhiXtra doesn't hold that (stock, profit, payroll…)
Without the AI (no key, too many questions, an error) a word match does the same job, more roughly.
"""
import json
import re
import time

import report_builder as RB
import report_plain as RP

AI_PER_10_MIN = 20
_recent = {}                 # viewer key → [times]; per worker, good enough for a soft limit

NOT_HELD = [
    (("stock", "inventory", "warehouse"), "PhiXtra doesn't hold your stock levels. Your online shop or stock system has that report."),
    (("profit", "margin", "cost of", "expenses", "tax", "vat", "payroll", "salary", "salaries", "accounting", "balance sheet"),
     "PhiXtra is a sales system, so it doesn't hold accounts like profit, costs, tax or payroll. Your accounting software has those reports."),
    (("website visitors", "page views", "traffic", "bounce rate", "google analytics"),
     "PhiXtra doesn't record website visits. Google Analytics (or your website's own reports) has those."),
]
STOP = set("a an the of for by to in on at and or is are was were do does did we our us me my i you your "
           "how many much what which who whom where when show give get see report reports list please last this "
           "month week year quarter today yesterday per each all".split())
PERIOD_WORDS = [
    (r"\blast\s+(\d{1,3})\s+days?\b", "lastNd"), (r"\blast\s+(\d{1,3})\s+weeks?\b", "lastNw"),
    (r"\blast\s+(\d{1,3})\s+months?\b", "lastNm"),
    (r"\btoday\b", "today"), (r"\byesterday\b", "yesterday"), (r"\bthis\s+week\b", "this_week"),
    (r"\blast\s+week\b", "last_week"), (r"\bthis\s+month\b", "this_month"), (r"\blast\s+month\b", "last_month"),
    (r"\bthis\s+quarter\b", "this_quarter"), (r"\blast\s+quarter\b", "last_quarter"),
    (r"\bthis\s+year\b", "this_year"), (r"\blast\s+year\b", "last_year"), (r"\b(all\s+time|ever)\b", "all"),
]
SPLIT_WORDS = [  # words in a question → a split label (matched against the subject's plain split choices)
    (("salesperson", "sales person", "team member", "per person", "by person", "who ", "staff", "rep "), "team member"),
    (("by month", "per month", "monthly", "month by month"), "month"), (("by week", "per week", "weekly"), "week"),
    (("by stage", "per stage", "each stage"), "stage"), (("country", "countries"), "country"),
    (("where", "source", "channel", "came from"), "where"), (("company", "companies", "buyer"), "company"),
    (("status",), "status"), (("product",), "product"), (("campaign",), "campaign"),
]


def _words(s):
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 1]


def _stem(w):
    for suf in ("ies", "es", "s", "ing", "ed"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _period(q):
    ql = q.lower()
    for pat, key in PERIOD_WORDS:
        m = re.search(pat, ql)
        if m:
            if key.startswith("lastN"):
                n = int(m.group(1))
                return f"last{n}{key[-1]}" if RB.period_ok(f"last{n}{key[-1]}") else ""
            return key
    return ""


def _member(q, people):
    ql = " " + q.lower() + " "
    for p in people:
        name = (p.get("display") or "").strip().lower()
        if not name:
            continue
        first = name.split()[0]
        if f" {name} " in ql or (len(first) > 2 and re.search(rf"\b{re.escape(first)}\b", ql)):
            return p["key"]
    return ""


def _split_for(q, subject):
    ql = q.lower()
    opts = RP.SPLITS.get(subject, [])
    for words, want in SPLIT_WORDS:
        if any(w in ql for w in words):
            for k, label, _h in opts:
                if want in label.lower():
                    return k
    return None


def _not_held(q):
    ql = q.lower()
    for words, msg in NOT_HELD:
        if any(re.search(rf"\b{re.escape(w)}\b", ql) for w in words):
            return msg
    return ""


def _scores(q, offered):
    qw = {_stem(w) for w in _words(q)}
    out = []
    for r in offered:
        text = " ".join([RP.QUESTIONS.get(r["key"], ""), r["label"], r["desc"], RB.SUBJECTS[r["cfg"]["subject"]]["label"]])
        rw = {_stem(w) for w in _words(text)}
        hit = len(qw & rw)
        if hit:
            out.append((hit / max(1, len(qw)), hit, r["key"]))
    out.sort(reverse=True)
    return out


def _by_words(q, offered, people):
    held = _not_held(q)
    sc = _scores(q, offered)
    if held and (not sc or sc[0][0] < 0.6):
        return {"not_recorded": True, "reason": held}
    if sc and sc[0][0] >= 0.5 and sc[0][1] >= 2 and (len(sc) == 1 or sc[0][1] > sc[1][1]):
        return {"ready": sc[0][2], "why": "words"}
    return {"closest": [k for _, _, k in sc[:3]]}


def _ai_allowed(key):
    now = time.time()
    xs = [t for t in _recent.get(key, []) if now - t < 600]
    if len(xs) >= AI_PER_10_MIN:
        _recent[key] = xs
        return False
    xs.append(now)
    _recent[key] = xs
    return True


def _by_ai(q, offered):
    import ask_phixtra
    lines = [f'- {r["key"]}: {RP.QUESTIONS.get(r["key"], r["label"])} ({r["desc"]}; about {RB.SUBJECTS[r["cfg"]["subject"]]["noun"]})'
             for r in offered]
    splits = {r["cfg"]["subject"]: [k for k, _l, _h in RP.SPLITS.get(r["cfg"]["subject"], [])] for r in offered}
    prompt = f"""You help a small business find the right ready-made report in PhiXtra, a sales system
(enquiries, leads, deals, quotes, chats, calls, campaigns, follow-ups, online-shop orders).
You may ONLY pick from this list. Never invent a report.

READY-MADE REPORTS (key: question)
{chr(10).join(lines)}

SPLIT CHOICES per subject (use one only if the person asks to split / group / "by" something): {json.dumps(splits)}
TIME CHOICES: today, yesterday, this_week, last_week, this_month, last_month, this_quarter, last_quarter,
this_year, last_year, all, or lastNd / lastNw / lastNm (e.g. last45d, last6w, last3m). Use "" when no time is asked.

QUESTION: {q}

Reply in JSON only:
{{"match": "<key or empty>", "time": "<time choice or empty>", "split": "<split choice or empty>",
  "show": "chart" or "list" or "", "closest": ["<up to 3 keys>"],
  "not_recorded": true/false, "reason": "<only when not_recorded: one short plain sentence saying what PhiXtra doesn't hold and where they'd find it>"}}
"match" only when one report clearly answers the question. "not_recorded" only for things a sales system
doesn't hold (stock levels, profit, costs, tax, payroll, website visits)."""
    return ask_phixtra._llm(prompt)


def answer(q, offered, people, session_key="") -> dict:
    keys = {r["key"] for r in offered}
    by_key = {r["key"]: r for r in offered}
    res = None
    if _ai_allowed(session_key or "-"):
        try:
            a = _by_ai(q, offered)
            if a.get("not_recorded"):
                res = {"not_recorded": True, "reason": str(a.get("reason") or "")[:300] or _not_held(q) or
                       "PhiXtra doesn't record that, so there is no report for it."}
            elif a.get("match") in keys:
                res = {"ready": a["match"], "time": str(a.get("time") or ""), "split": str(a.get("split") or ""),
                       "show": a.get("show") if a.get("show") in ("chart", "list") else "", "why": "ai"}
            else:
                res = {"closest": [k for k in (a.get("closest") or []) if k in keys][:3]}
        except Exception as e:
            print("⚠️ ask for a report (AI):", str(e)[:200])
    if res is None or (not res.get("ready") and not res.get("not_recorded") and not res.get("closest")):
        res = _by_words(q, offered, people)
    if res.get("ready"):
        subj = by_key[res["ready"]]["cfg"]["subject"]
        t = res.get("time") or _period(q)
        sp = res.get("split") or _split_for(q, subj) or ""
        valid_splits = {k for k, _l, _h in RP.SPLITS.get(subj, [])}
        ch = {"period": t if t and RB.period_ok(t) and t != "custom" else "",
              "member": _member(q, people) if RB.DASH_MEMBER_COL.get(subj) or RB.SUBJECTS[subj].get("member") else "",
              "show": res.get("show") or ""}
        if sp in valid_splits:
            ch["split"] = sp
        res["choices"] = {k: v for k, v in ch.items() if v}
        res["chips"] = _chips(res["choices"], subj, people)
    return res


def _chips(ch, subj, people):
    out = []
    if ch.get("period"):
        from werkzeug.datastructures import MultiDict
        cfg = RB.read_config(MultiDict({"subject": subj, "period": ch["period"]}), {subj})
        out.append("Time: " + RP.period_words(cfg))
    if ch.get("member"):
        name = next((p["display"] for p in people if p["key"] == ch["member"]), "")
        if name:
            out.append("Team member: " + name)
    if ch.get("split"):
        label = next((l for k, l, _h in RP.SPLITS.get(subj, []) if k == ch["split"]), "")
        if label:
            out.append("Split by: " + label.lower())
    if ch.get("show"):
        out.append("Show as: " + ch["show"])
    return out


def examples(offered):
    keys = {r["key"] for r in offered}
    out = []
    if "quotes_by_member" in keys:
        out.append("won quotes by salesperson last month")
    if "new_leads" in keys:
        out.append("new leads this month")
    if "deals_quiet" in keys:
        out.append("which deals have gone quiet")
    if "orders_week" in keys:
        out.append("orders per week this quarter")
    if "missed_replies" in keys and len(out) < 3:
        out.append("chats waiting for a reply")
    return out[:3]
