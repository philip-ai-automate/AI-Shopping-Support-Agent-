"""
lead_qual_dashboard.py — CRM › Lead Qualification (Phase 2, 30 Oct 2026,
user: "yes go ahead with the dashboard"). Approved mock-up: section 2 of
https://claude.ai/artifact/1yo1cNY9ac33GpMRnXvnCp.

How the AI turns enquiries into orders, what needs a person today, and how
far the business can trust the AI's decisions. Every number comes from what
lead_qualify already records — nothing here is typed in.

  Enquiry   = a lead the AI read an enquiry or reply for in the period
              (lead_ai_decisions, not auto-replies / not-an-enquiry). Each
              lead counts once, under the channel of its first enquiry.
  Qualified = that lead became an Opportunity (by the AI or by staff).
  Quotation = it reached Proposal Sent (or later) in the Sales Pipeline.
  Order     = it was won; revenue = its deal value.
The funnel follows the same enquiries through, so the steps always add up.
"""
from datetime import date, datetime, time, timedelta, timezone

import psycopg2.extras

import lead_qualify as _lq
from db import get_db_connection

PERIODS = [("month", "This month"), ("last_month", "Last month"), ("7d", "Last 7 days"),
           ("30d", "Last 30 days"), ("90d", "Last 90 days"), ("year", "This year")]
CHANNELS = [("whatsapp", "WhatsApp"), ("web", "Website chat"), ("email", "Email enquiries"),
            ("reply", "Replies to your emails")]
CHANNEL_SHORT = {"whatsapp": "WhatsApp", "web": "Website chat", "email": "Email", "reply": "Email reply"}
_PROPOSAL_ON = ("proposal_sent", "negotiating", "won")
_SKIP_INTENTS = ("auto_reply", "not_enquiry")
MAX_COUNTRIES = 6
# Not a real enquiry: a lead the AI made that staff undid (closed "Invalid
# lead"), or a duplicate merged in Check these (closed "Duplicate").
_REAL = ("NOT (COALESCE(l.outcome, '') = 'not_a_fit' "
         "AND COALESCE(l.dropped_reason, '') IN ('Invalid lead', 'Duplicate'))")


def period_range(key: str, today: date):
    """(start, end exclusive, label, previous start, previous end, previous label)."""
    if key == "last_month":
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    elif key in ("7d", "30d", "90d"):
        n = int(key[:-1])
        end = today + timedelta(days=1)
        start = end - timedelta(days=n)
    elif key == "year":
        start, end = today.replace(month=1, day=1), today + timedelta(days=1)
    else:
        key = "month"
        start, end = today.replace(day=1), today + timedelta(days=1)
    if key in ("month", "last_month"):
        p_end = start
        p_start = (start - timedelta(days=1)).replace(day=1)
        p_label = p_start.strftime("%B")
    elif key == "year":
        p_start = start.replace(year=start.year - 1)
        p_end = p_start + (end - start)              # same number of days (safe on 29 February)
        p_label = f"the same time in {start.year - 1}"
    else:
        p_end, p_start = start, start - (end - start)
        p_label = f"the {int(key[:-1])} days before"
    last = end - timedelta(days=1)
    label = (f"{start.day} {start:%B %Y}" if start == last else
             f"{start.day}–{last.day} {last:%B %Y}" if (start.month, start.year) == (last.month, last.year) else
             f"{start.day} {start:%b} – {last.day} {last:%b %Y}")
    return start, end, key, label, p_start, p_end, p_label


def _cohort_sql(scope_sql: str) -> str:
    """One row per enquiry lead in [start, end): its first enquiry time and
    channel, whether the AI qualified it, and where it is now."""
    return f"""
        WITH firsts AS (
            SELECT DISTINCT ON (d.lead_id) d.lead_id, d.channel, d.created_at AS enquiry_at
              FROM lead_ai_decisions d
             WHERE d.tenant_id = %(t)s AND d.created_at >= %(s)s AND d.created_at < %(e)s
               AND COALESCE(d.intent, '') NOT IN %(skip)s
             ORDER BY d.lead_id, d.created_at)
        SELECT f.lead_id, f.channel, f.enquiry_at, l.customer_name, l.addr_country, l.addr_city,
               l.assigned_key, l.assigned_to, l.is_opportunity, l.opportunity_at, l.stage, l.outcome,
               l.proposal_date, l.deal_value, l.currency, l.won_date, l.dropped_at,
               fx_to_selling(l.deal_value, l.currency, l.tenant_id) AS value_biz,
               l.product_interest, l.quantity, l.purchase_timeline,
               ({{score}})::int AS score,
               EXISTS (SELECT 1 FROM lead_ai_decisions q WHERE q.lead_id = l.id AND q.qualified
                          AND q.undone_at IS NULL) AS by_ai
          FROM firsts f JOIN merchant_pipeline_leads l ON l.id = f.lead_id AND l.tenant_id = %(t)s
         WHERE {_REAL} {scope_sql}"""


def _won(r) -> bool:
    return r["outcome"] == "won" or r["stage"] == "won"


def _quoted(r) -> bool:
    return r["proposal_date"] is not None or r["stage"] in _PROPOSAL_ON or _won(r)


def _pct(a, b) -> int:
    return round(a * 100 / b) if b else 0


def _change(now, before) -> dict:
    if not before:
        return {"text": "", "dir": ""}
    d = now - before
    if d == 0:
        return {"text": "Same as", "dir": ""}
    return {"text": f"{'▲' if d > 0 else '▼'} {_pct(abs(d), before)}% on", "dir": "up" if d > 0 else "down"}


def _waited(since: datetime, now: datetime) -> tuple:
    """('1 day 4 h', hours)."""
    if not since:
        return "", 0
    secs = max(0, (now - since).total_seconds())
    h = int(secs // 3600)
    if h < 1:
        return f"{max(1, int(secs // 60))} min", 0
    if h < 24:
        return f"{h} h", h
    days, rest = divmod(h, 24)
    return f"{days} day{'s' if days != 1 else ''}" + (f" {rest} h" if rest else ""), h


def _median_hours(vals):
    vals = sorted(v for v in vals if v is not None and v >= 0)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


def duration_words(hours) -> str:
    if hours is None:
        return "—"
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 48:
        return f"{round(hours, 1):g} hours" if hours < 10 else f"{round(hours)} hours"
    return f"{round(hours / 24, 1):g} days"


def build(tenant_id: int, s: dict, today: date, now: datetime, period: str = "month", channel: str = "",
          country: str = "", owner: str = "", scope_key: str = None, country_names: dict = None,
          tz=None) -> dict:
    """Everything the page shows. scope_key = only this person's leads
    (staff whose role can't see all leads)."""
    start, end, period, label, p_start, p_end, p_label = period_range(period, today)
    country_names = country_names or {}
    tz = tz or timezone.utc

    def at(d):                                       # midnight in the business's own time zone
        return datetime.combine(d, time.min).replace(tzinfo=tz)
    scope, params = "", {"t": tenant_id, "skip": _SKIP_INTENTS}
    if scope_key:
        scope += " AND l.assigned_key = %(scope)s"
        params["scope"] = scope_key
    sql = _cohort_sql(scope).replace("{score}", _lq.score_sql(s, "l"))
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(sql, {**params, "s": at(start), "e": at(end)})
        rows_all = cur.fetchall()
        cur.execute(sql, {**params, "s": at(p_start), "e": at(p_end)})
        prev_all = cur.fetchall()
        # Choices for the filters come from this period's enquiries (owners
        # also from the waiting lists below, which aren't tied to the period).
        owner_set = {(r["assigned_key"], r["assigned_to"] or "Someone") for r in rows_all if r["assigned_key"]}
        countries = sorted({r["addr_country"] for r in rows_all if r["addr_country"]},
                           key=lambda c: country_names.get(c, c))

        def keep(r):
            return ((not channel or r["channel"] == channel) and (not country or r["addr_country"] == country)
                    and (not owner or (r["assigned_key"] or "") == owner))
        rows = [r for r in rows_all if keep(r)]
        prev = [r for r in prev_all if keep(r)]
        for r in rows:
            r["day"] = r["enquiry_at"].astimezone(tz).date()

        qualified = [r for r in rows if r["is_opportunity"]]
        quoted = [r for r in qualified if _quoted(r)]
        won = [r for r in rows if _won(r)]
        lost = [r for r in quoted if not _won(r) and r["outcome"]]
        q_waiting = [r for r in quoted if not _won(r) and not r["outcome"]]
        revenue = sum(float(r["value_biz"] or 0) for r in won)
        revenue_ai = sum(float(r["value_biz"] or 0) for r in won if r["by_ai"])
        p_won = [r for r in prev if _won(r)]

        # Needs you now: qualified, still open, no quotation yet — highest
        # score first. Not tied to the period: a lead waiting is waiting.
        cur.execute(f"""
            SELECT l.id, l.customer_name, l.addr_country, l.addr_city, l.assigned_key, l.assigned_to,
                   l.opportunity_at, l.product_interest, l.quantity, l.purchase_timeline,
                   ({_lq.score_sql(s, "l")})::int AS score,
                   (SELECT d.channel FROM lead_ai_decisions d WHERE d.lead_id = l.id
                     ORDER BY d.created_at LIMIT 1) AS channel
              FROM merchant_pipeline_leads l
             WHERE l.tenant_id = %(t)s AND l.is_opportunity AND l.outcome IS NULL AND l.dropped_at IS NULL
               AND l.proposal_date IS NULL AND COALESCE(l.stage, '') NOT IN ('proposal_sent', 'negotiating', 'won')
               AND EXISTS (SELECT 1 FROM lead_ai_decisions d WHERE d.lead_id = l.id) {scope}
             ORDER BY score DESC, l.opportunity_at""", params)
        needs_all = cur.fetchall()
        needs = [r for r in needs_all
                 if (not channel or r["channel"] == channel) and (not country or r["addr_country"] == country)
                 and (not owner or (r["assigned_key"] or "") == owner)]
        for r in needs:
            r["waiting"], r["waited_h"] = _waited(r["opportunity_at"], now)
        needs_over_day = sum(1 for r in needs if r["waited_h"] >= 24)

        # Quotations waiting for the customer: proposal sent, still open.
        cur.execute(f"""
            SELECT l.id, l.customer_name, l.addr_country, l.addr_city, l.assigned_key, l.assigned_to, l.deal_value, l.currency,
                   l.proposal_date, l.stage, fx_to_selling(l.deal_value, l.currency, l.tenant_id) AS value_biz,
                   (SELECT d.channel FROM lead_ai_decisions d WHERE d.lead_id = l.id
                     ORDER BY d.created_at LIMIT 1) AS channel,
                   (SELECT f.due_date FROM lead_followups f WHERE f.lead_id = l.id AND f.done_at IS NULL
                       AND f.cancelled_at IS NULL ORDER BY f.due_date LIMIT 1) AS next_fu,
                   (SELECT f.auto FROM lead_followups f WHERE f.lead_id = l.id AND f.done_at IS NULL
                       AND f.cancelled_at IS NULL ORDER BY f.due_date LIMIT 1) AS next_fu_auto
              FROM merchant_pipeline_leads l
             WHERE l.tenant_id = %(t)s AND l.is_opportunity AND l.outcome IS NULL AND l.dropped_at IS NULL
               AND l.stage IN ('proposal_sent', 'negotiating')
               AND EXISTS (SELECT 1 FROM lead_ai_decisions d WHERE d.lead_id = l.id) {scope}
             ORDER BY value_biz DESC NULLS LAST, l.proposal_date""", params)
        quotes_all = cur.fetchall()
        quotes = [r for r in quotes_all
                  if (not channel or r["channel"] == channel) and (not country or r["addr_country"] == country)
                  and (not owner or (r["assigned_key"] or "") == owner)]
        owner_set |= {(r["assigned_key"], r["assigned_to"] or "Someone") for r in needs_all + quotes_all
                      if r["assigned_key"]}
        owners = sorted(owner_set, key=lambda x: x[1].lower())
        quotes_value = sum(float(r["value_biz"] or 0) for r in quotes)

        # Trust: undone decisions and Check these.
        cur.execute(f"""SELECT COUNT(*) FILTER (WHERE d.undone_at IS NOT NULL) AS undone, COUNT(*) AS total
                          FROM lead_ai_decisions d JOIN merchant_pipeline_leads l ON l.id = d.lead_id
                         WHERE d.tenant_id = %(t)s AND d.created_at >= %(s)s AND d.created_at < %(e)s
                           AND COALESCE(d.intent, '') NOT IN %(skip)s {scope}""", {**params, "s": at(start), "e": at(end)})
        dec = cur.fetchone()
        cur.execute(f"""SELECT COUNT(*) AS n FROM lead_check_items c JOIN merchant_pipeline_leads l ON l.id = c.lead_id
                         WHERE c.tenant_id = %(t)s AND c.status = 'open' {scope}""", params)
        check_open = cur.fetchone()["n"]
    finally:
        cur.close(); conn.close()

    # Hot / warm / cold by week of the enquiry (score as it is now).
    weeks, ws = [], start - timedelta(days=start.weekday())
    while ws < end:
        we = ws + timedelta(days=7)
        a, b = max(ws, start), min(we, end) - timedelta(days=1)
        wk = [r for r in rows if a <= r["day"] <= b]
        tiers = [_lq.tier_for(r["score"] or 0, s) for r in wk]
        weeks.append({"label": (f"{a.day}–{b.day} {b:%b}" if a.month == b.month else f"{a.day} {a:%b}–{b.day} {b:%b}")
                      if a != b else f"{a.day} {a:%b}",
                      "hot": tiers.count("hot"), "warm": tiers.count("warm"), "cold": tiers.count("cold"),
                      "total": len(wk)})
        ws = we
    group = "week"
    if len(weeks) > 14:                              # a long period: group by month instead
        months, k = {}, start.replace(day=1)
        while k < end:                               # every month, empty ones too
            months[k] = {"label": f"{k:%b}", "hot": 0, "warm": 0, "cold": 0, "total": 0}
            k = (k + timedelta(days=32)).replace(day=1)
        for r in rows:
            k = r["day"].replace(day=1)
            m = months.setdefault(k, {"label": f"{k:%b}", "hot": 0, "warm": 0, "cold": 0, "total": 0})
            m[_lq.tier_for(r["score"] or 0, s)] += 1
            m["total"] += 1
        weeks = [months[k] for k in sorted(months)]
        group = "month"
    week_max = max([w["total"] for w in weeks] + [1])
    tier_totals = {t: sum(w[t] for w in weeks) for t in ("hot", "warm", "cold")}

    # By channel and by country.
    def tally(items):
        q = [r for r in items if r["is_opportunity"]]
        w = [r for r in items if _won(r)]
        return {"enquiries": len(items), "qualified": len(q), "orders": len(w),
                "revenue": sum(float(r["value_biz"] or 0) for r in w)}
    by_channel = [{"key": k, "label": l, **tally([r for r in rows if r["channel"] == k])} for k, l in CHANNELS]
    by_channel = [c for c in by_channel if c["enquiries"] or c["key"] != "reply"]
    by_country_all = {}
    for r in rows:
        by_country_all.setdefault(r["addr_country"] or "", []).append(r)
    known = sorted(((c, g) for c, g in by_country_all.items() if c), key=lambda x: -len(x[1]))
    by_country = [{"label": country_names.get(c, c), **tally(g)} for c, g in known[:MAX_COUNTRIES]]
    rest = [r for c, g in known[MAX_COUNTRIES:] for r in g] + by_country_all.get("", [])
    if rest:
        by_country.append({"label": "Other countries" if known[MAX_COUNTRIES:] else "Country not known",
                           **tally(rest)})
    country_max = max([c["enquiries"] for c in by_country] + [1])

    ai_q = [r for r in qualified if r["by_ai"]]
    staff_q = [r for r in qualified if not r["by_ai"]]

    def hours(r):
        if not r["opportunity_at"]:
            return None
        return (r["opportunity_at"] - r["enquiry_at"]).total_seconds() / 3600

    return {
        "period": period, "period_label": label, "prev_label": p_label,
        "start": start, "end": end, "today": today, "group": group,
        "owners": owners, "countries": [(c, country_names.get(c, c)) for c in countries],
        "kpi": {
            "enquiries": len(rows), "enquiries_change": _change(len(rows), len(prev)),
            "qualified": len(qualified), "qualified_ai": len(ai_q), "qualified_staff": len(staff_q),
            "needs": len(needs), "needs_over_day": needs_over_day,
            "orders": len(won), "orders_change": _change(len(won), len(p_won)),
            "revenue": revenue, "revenue_ai": revenue_ai,
        },
        "funnel": {
            "enquiries": len(rows), "qualified": len(qualified), "quoted": len(quoted), "orders": len(won),
            "q_pct": _pct(len(qualified), len(rows)), "quote_pct": _pct(len(quoted), len(qualified)),
            "won_pct": _pct(len(won), len(quoted)), "waiting": len(q_waiting), "lost": len(lost),
        },
        "weeks": weeks, "week_max": week_max, "tier_totals": tier_totals,
        "needs": needs, "quotes": quotes, "quotes_value": quotes_value,
        "by_channel": by_channel, "by_country": by_country, "country_max": country_max,
        "trust": {
            "ai": len(ai_q), "staff": len(staff_q),
            "undone": dec["undone"], "decisions": dec["total"], "undone_pct": _pct(dec["undone"], dec["total"]),
            "check_open": check_open,
            "ai_time": duration_words(_median_hours([hours(r) for r in ai_q])),
            "staff_time": duration_words(_median_hours([hours(r) for r in staff_q])),
        },
    }
