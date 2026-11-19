"""Inbox customer panel (17 Nov 2026, Inbox redesign): who the staff member is
talking to, built from what PhiXtra already knows: the contact, the lead
(deal stage, value, salesperson, next follow-up), labels, orders, quote
requests, the Shopify trade company, notes. Read-only. Every section is
optional: a section with nothing in it is left out, never guessed."""
import re
import psycopg2.extras

import data_crypto
from db import get_db_connection


def _digits(v):
    return re.sub(r"\D", "", str(v or ""))


def _dec(v):
    try:
        return data_crypto.dec(v) if v else v
    except Exception:
        return v


def _money(v, cur):
    if v is None:
        return None
    try:
        v = float(v)
    except Exception:
        return None
    sym = {"USD": "$", "GBP": "£", "EUR": "€", "NGN": "₦"}.get((cur or "").upper(), "")
    s = f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"
    return f"{sym}{s}" if sym else (f"{s} {cur.upper()}" if cur else s)


def _day(d):
    return d.strftime("%-d %b %Y") if d else None


def build(tenant_id: int, key: str, numbers: list, emails: list, name_hint: str = None,
          can_sales: bool = True, lead_url=None, quote_url=None, shopify_card: dict = None) -> dict:
    """numbers/emails: what the chat itself knows about the person.
    can_sales: the viewer may see deal stage/value/quotes/orders.
    lead_url(lead_id) / quote_url(request_id): URL builders from the route."""
    ds = sorted({d for d in (_digits(n) for n in numbers) if len(d) >= 6})
    es = sorted({e.strip().lower() for e in emails if e and e.strip()})
    out = {"name": name_hint or None, "company": None, "role": None, "tags": [], "tiles": [],
           "contact": [], "sales": [], "recent": [], "note": None, "links": [], "channel": None}
    if key.startswith("web:"):
        out["channel"] = "Website chat"
    elif key.startswith("fb:"):
        out["channel"] = "Messenger"
    else:
        out["channel"] = "WhatsApp"

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # Website visitor details (a Shopify login or a form) — only when the chat itself says so.
        if key.startswith("web:"):
            cur.execute("SELECT name, email, phone, company FROM chat_visitors WHERE tenant_id=%s AND session_id=%s",
                        (tenant_id, key[4:]))
            v = cur.fetchone()
            if v:
                vn, ve, vp, vc = (_dec(v.get(k)) for k in ("name", "email", "phone", "company"))
                out["name"] = out["name"] or vn
                out["company"] = vc or None
                if ve:
                    es = sorted(set(es) | {ve.strip().lower()})
                if _digits(vp):
                    ds = sorted(set(ds) | {_digits(vp)})

        # The saved contact.
        contact = None
        if ds or es:
            cur.execute("""SELECT id, display_name, contact_person, email, whatsapp_number, phone, tags, source,
                                  addr_city, addr_country, created_at
                             FROM wa_contacts
                            WHERE tenant_id=%s
                              AND (regexp_replace(COALESCE(whatsapp_number,''), '\\D', '', 'g') = ANY(%s)
                                   OR regexp_replace(COALESCE(phone,''), '\\D', '', 'g') = ANY(%s)
                                   OR LOWER(COALESCE(email,'')) = ANY(%s))
                            ORDER BY updated_at DESC NULLS LAST LIMIT 1""", (tenant_id, ds or ['~none~'], ds or ['~none~'], es or ['~none~']))
            contact = cur.fetchone()

        # The lead (newest that matches the contact, phone or email).
        lead = None
        if ds or es or contact:
            cur.execute("""SELECT id, customer_name, contact_person, email, phone, whatsapp_number, stage, deal_value,
                                  currency, assigned_to, assigned_key, source, contact_channel, created_at,
                                  addr_city, addr_country, notes, product_interest
                             FROM merchant_pipeline_leads
                            WHERE tenant_id=%s AND dropped_at IS NULL
                              AND (wa_contact_id = %s
                                   OR regexp_replace(COALESCE(whatsapp_number,''), '\\D', '', 'g') = ANY(%s)
                                   OR regexp_replace(COALESCE(phone,''), '\\D', '', 'g') = ANY(%s)
                                   OR LOWER(COALESCE(email,'')) = ANY(%s))
                            ORDER BY updated_at DESC NULLS LAST LIMIT 1""",
                        (tenant_id, contact["id"] if contact else -1, ds or ['~none~'], ds or ['~none~'], es or ['~none~']))
            lead = cur.fetchone()
        # A logged-in Shopify trade buyer is linked to a lead directly.
        if not lead and key.startswith("web:") and _has_col(cur, "handoff_requests", "lead_id"):
            cur.execute("""SELECT l.id, l.customer_name, l.contact_person, l.email, l.phone, l.whatsapp_number, l.stage,
                                  l.deal_value, l.currency, l.assigned_to, l.assigned_key, l.source, l.contact_channel,
                                  l.created_at, l.addr_city, l.addr_country, l.notes, l.product_interest
                             FROM handoff_requests h JOIN merchant_pipeline_leads l ON l.id = h.lead_id
                            WHERE h.tenant_id=%s AND h.session_id=%s ORDER BY h.created_at DESC LIMIT 1""",
                        (tenant_id, key[4:]))
            lead = cur.fetchone()
        if not lead and key.startswith("web:") and _has_col(cur, "quote_requests", "source_key"):
            cur.execute("""SELECT l.id, l.customer_name, l.contact_person, l.email, l.phone, l.whatsapp_number, l.stage,
                                  l.deal_value, l.currency, l.assigned_to, l.assigned_key, l.source, l.contact_channel,
                                  l.created_at, l.addr_city, l.addr_country, l.notes, l.product_interest
                             FROM quote_requests q JOIN merchant_pipeline_leads l ON l.id = q.lead_id
                            WHERE q.tenant_id=%s AND q.source_key=%s ORDER BY q.created_at DESC LIMIT 1""",
                        (tenant_id, "chat:" + key[4:]))
            lead = cur.fetchone()

        # Who they are.
        if contact:
            out["name"] = out["name"] or contact.get("display_name") or contact.get("contact_person")   # the chat's own name first
        if lead:
            person = lead.get("contact_person")
            biz = lead.get("customer_name")
            out["name"] = out["name"] or person or biz
            if biz and biz != out["name"]:
                out["company"] = out["company"] or biz
        if shopify_card and shopify_card.get("company"):
            out["company"] = shopify_card["company"]
            out["tags"].append({"name": "Shopify trade customer", "colour": "#166534"})

        # Labels.
        if lead:
            cur.execute("""SELECT lb.name, lb.colour FROM lead_label_leads ll JOIN lead_labels lb ON lb.id = ll.label_id
                            WHERE ll.lead_id=%s AND lb.tenant_id=%s ORDER BY lb.name""", (lead["id"], tenant_id))
            out["tags"] += [{"name": r["name"], "colour": r.get("colour") or "#3730A3"} for r in cur.fetchall()]
        if contact and contact.get("tags"):
            t = contact["tags"]
            for tg in (t if isinstance(t, list) else [x.strip() for x in str(t).split(",")]):
                if tg and not any(x["name"].lower() == str(tg).lower() for x in out["tags"]):
                    out["tags"].append({"name": str(tg), "colour": "#334155"})

        # Contact details.
        wa = (contact or {}).get("whatsapp_number") or (lead or {}).get("whatsapp_number") or (ds[0] if ds else None)
        email = (contact or {}).get("email") or (lead or {}).get("email") or (es[0] if es else None)
        city = (lead or {}).get("addr_city") or (contact or {}).get("addr_city")
        country = (lead or {}).get("addr_country") or (contact or {}).get("addr_country")
        if wa:
            out["contact"].append(["WhatsApp", "+" + _digits(wa)])
        if email:
            out["contact"].append(["Email", email])
        if city or country:
            out["contact"].append(["Location", ", ".join(x for x in (city, country) if x)])
        since = min([d for d in ((contact or {}).get("created_at"), (lead or {}).get("created_at")) if d], default=None)
        if since:
            src = (lead or {}).get("source") or (lead or {}).get("contact_channel") or (contact or {}).get("source")
            out["contact"].append(["Customer since", _day(since) + (f" · {src}" if src else "")])

        # Sales (only for staff who may see leads).
        if can_sales:
            if lead:
                if lead.get("stage"):
                    out["sales"].append(["Deal stage", str(lead["stage"]).replace("_", " ").capitalize()])
                if lead.get("deal_value"):
                    out["sales"].append(["Deal value", _money(lead["deal_value"], lead.get("currency"))])
                if lead.get("assigned_to"):
                    try:
                        from portal_routes import display_actor
                        who = display_actor(lead["assigned_to"], lead.get("assigned_key"))
                    except Exception:
                        who = lead["assigned_to"]
                    out["sales"].append(["Salesperson", who])
                if lead.get("product_interest"):
                    out["sales"].append(["Interested in", str(lead["product_interest"])[:80]])
                cur.execute("""SELECT due_date FROM lead_followups WHERE tenant_id=%s AND lead_id=%s
                                 AND done_at IS NULL AND cancelled_at IS NULL ORDER BY due_date LIMIT 1""",
                            (tenant_id, lead["id"]))
                fu = cur.fetchone()
                if fu and fu.get("due_date"):
                    out["sales"].append(["Next follow-up", _day(fu["due_date"])])
            if shopify_card and shopify_card.get("location"):
                loc = shopify_card["location"]
                if loc.get("payment_terms"):
                    out["sales"].append(["Payment terms", f"{loc['payment_terms']} (Shopify)"])
                if loc.get("name"):
                    out["sales"].append(["Site", loc["name"]])

            # Quote requests for this lead (or this phone/email).
            cur.execute("""SELECT id, ref_no, status, quote_value, quote_currency, currency, quote_sent_at, created_at
                             FROM quote_requests
                            WHERE tenant_id=%s
                              AND (lead_id = %s
                                   OR regexp_replace(COALESCE(buyer_phone,''), '\\D', '', 'g') = ANY(%s)
                                   OR LOWER(COALESCE(buyer_email,'')) = ANY(%s))
                            ORDER BY created_at DESC LIMIT 5""",
                        (tenant_id, lead["id"] if lead else -1, ds or ['~none~'], es or ['~none~']))
            qrs = cur.fetchall()
            open_q = next((q for q in qrs if q["status"] not in ("won", "lost")), None)
            if open_q:
                val = _money(open_q.get("quote_value"), open_q.get("quote_currency") or open_q.get("currency"))
                out["tiles"].append(["Open quote", val or f"R-{open_q['ref_no']}"])
                out["sales"].append(["Quote", {"text": f"R-{open_q['ref_no']}" + (f" · {val}" if val else "")
                                               + f" · {str(open_q['status']).replace('_', ' ')}",
                                               "url": quote_url(open_q["id"]) if quote_url else None}])
            won = [q for q in qrs if q["status"] == "won"]

            # Orders (PhiXtra orders by phone).
            orders = []
            if ds:
                cur.execute("""SELECT reference, total_amount, currency, status, created_at FROM orders
                                WHERE tenant_id=%s AND regexp_replace(COALESCE(customer_phone,''), '\\D', '', 'g') = ANY(%s)
                                ORDER BY created_at DESC LIMIT 50""", (tenant_id, ds))
                orders = cur.fetchall()
            if orders:
                cur_code = orders[0].get("currency")
                total = sum(float(o["total_amount"] or 0) for o in orders if (o.get("status") or "") not in ("cancelled",))
                out["tiles"].append(["Orders", str(len(orders))])
                out["tiles"].append(["Spent", _money(total, cur_code)])
                out["tiles"].append(["Last order", orders[0]["created_at"].strftime("%-d %b")])
            elif won:
                out["tiles"].append(["Quotes won", str(len(won))])

            # Recent activity.
            ev = []
            for o in orders[:3]:
                ev.append((o["created_at"], f"Order {o['reference']} · {_money(o['total_amount'], o.get('currency'))}"))
            for q in qrs[:3]:
                when = q.get("quote_sent_at") or q.get("created_at")
                verb = {"won": "won", "lost": "lost", "sent": "quote sent"}.get(q["status"], "quote request")
                ev.append((when, f"R-{q['ref_no']} {verb}"))
            if since:
                ev.append((since, "First contact"))
            ev = sorted([e for e in ev if e[0]], key=lambda e: e[0], reverse=True)[:4]
            out["recent"] = [[e[0].strftime("%-d %b"), e[1]] for e in ev]

        # Note for the team: the newest contact note, else the lead's notes.
        if contact:
            cur.execute("SELECT body FROM wa_contact_notes WHERE tenant_id=%s AND contact_id=%s ORDER BY created_at DESC LIMIT 1",
                        (tenant_id, contact["id"]))
            n = cur.fetchone()
            if n and n.get("body"):
                out["note"] = str(n["body"])[:300]
        if not out["note"] and lead and lead.get("notes"):
            out["note"] = str(lead["notes"])[:300]

        if lead and lead_url:
            out["links"].append({"text": "Open the lead", "url": lead_url(lead["id"]), "primary": False})
        if can_sales and open_q and quote_url:
            out["links"].append({"text": "Open the quote request", "url": quote_url(open_q["id"]), "primary": True})
        out["lead_id"] = lead["id"] if lead else None
    finally:
        cur.close(); conn.close()
    out["known"] = bool(out["contact"] or out["sales"] or out["tiles"] or out["tags"] or out["note"])
    return out


_COLS = {}


def _has_col(cur, table, col):
    k = (table, col)
    if k not in _COLS:
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (table, col))
        _COLS[k] = cur.fetchone() is not None
    return _COLS[k]
