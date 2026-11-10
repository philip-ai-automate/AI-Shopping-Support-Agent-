"""
quote_ai_draft.py — the AI Agent prepares a quotation draft (10 Nov 2026).

When a salesperson presses Make quotation (PhiXtra quotations, setting
"AI Agent prepares the draft" on), the new draft is filled in further:

  Prices      — this buyer's price on their last ACCEPTED quotation for the
                same product, else the product discount set in My Products.
                Worked out here in code; the AI Agent never chooses a price.
  Delivery    — the charge from this buyer's last quotation, when this one
                has none.
  Stock short — a delivery plan: what goes from stock now, the rest with the
                date left for the salesperson. Never a guessed date.
  AI Agent    — one call: up to 3 closest products for lines that are not on
                the business's list (only from candidates given), answers to
                the buyer's questions from what PhiXtra knows (unknown =
                left for the salesperson), and the covering message in the
                buyer's language. The quotation itself stays English.

Every change is recorded in data["ai_draft"]["changes"] with its reason and
what it was before, and the field itself carries an "ai" mark, so the editor
shows it in purple and can undo it. data["ai_draft"]["base"] keeps the draft
as it was, for "Start again without AI Agent". Nothing is ever sent.
"""
import json
import os
import re
from datetime import date

import psycopg2.extras

from db import get_db_connection
import quote_requests as qr
import quote_design as qd

TIMEOUT = 20          # seconds; after that the editor opens as before
MAX_CANDIDATES = 12   # products offered to the AI Agent per not-on-list line


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def enabled(tenant_id: int) -> bool:
    try:
        return bool(qr.settings(tenant_id).get("ai_draft", True))
    except Exception:
        return False


def _plain(v):
    from decimal import Decimal
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


def _money(v) -> float:
    return round(float(v) + 1e-9, 2)


def _dmy(d) -> str:
    return d.strftime("%d %b").lstrip("0") if d else ""


# ── what PhiXtra knows about this buyer ─────────────────────────────────────

def _buyer_history(cur, tenant_id: int, req: dict) -> list:
    """This buyer's ACCEPTED (won) quotations, newest first:
    [{number, accepted, lines, charges}] — only this buyer's own requests."""
    conds, args = [], []
    if req.get("lead_id"):
        conds.append("r.lead_id = %s"); args.append(req["lead_id"])
    if req.get("buyer_email"):
        conds.append("lower(r.buyer_email) = lower(%s)"); args.append(req["buyer_email"])
    ph = re.sub(r"\D", "", req.get("buyer_phone") or "")
    if len(ph) >= 9:
        conds.append("right(regexp_replace(COALESCE(r.buyer_phone,''), '\\D', '', 'g'), 9) = %s"); args.append(ph[-9:])
    if not conds:
        return []
    cur.execute(f"""SELECT r.id, r.closed_at, q.number, q.version, q.data, q.currency
                      FROM quote_requests r
                      JOIN LATERAL (SELECT number, version, data, currency FROM quotations
                                     WHERE tenant_id = r.tenant_id AND request_id = r.id AND status = 'sent'
                                     ORDER BY version DESC LIMIT 1) q ON TRUE
                     WHERE r.tenant_id = %s AND r.id <> %s AND r.status = 'won' AND ({' OR '.join(conds)})
                     ORDER BY r.closed_at DESC NULLS LAST LIMIT 20""", [tenant_id, req["id"]] + args)
    out = []
    for r in cur.fetchall():
        num = r["number"] + (f" v{r['version']}" if (r["version"] or 1) > 1 else "")
        out.append({"number": num, "accepted": r["closed_at"], "currency": r["currency"],
                    "lines": (r["data"] or {}).get("lines") or [], "charges": (r["data"] or {}).get("charges") or []})
    return out


def _last_charges(cur, tenant_id: int, req: dict, currency=None) -> tuple:
    """Charges on this buyer's most recent SENT quotation (won or not)."""
    conds, args = [], []
    if req.get("lead_id"):
        conds.append("r.lead_id = %s"); args.append(req["lead_id"])
    if req.get("buyer_email"):
        conds.append("lower(r.buyer_email) = lower(%s)"); args.append(req["buyer_email"])
    if not conds:
        return None, []
    cur.execute(f"""SELECT q.number, q.version, q.data->'charges' AS charges FROM quotations q
                      JOIN quote_requests r ON r.id = q.request_id AND r.tenant_id = q.tenant_id
                     WHERE q.tenant_id = %s AND q.status = 'sent' AND r.id <> %s AND ({' OR '.join(conds)})
                       AND jsonb_array_length(COALESCE(q.data->'charges', '[]'::jsonb)) > 0
                       AND COALESCE(q.currency, '') = COALESCE(%s, '')
                     ORDER BY q.sent_at DESC LIMIT 1""", [tenant_id, req["id"]] + args + [currency])
    r = cur.fetchone()
    if not r:
        return None, []
    return r["number"] + (f" v{r['version']}" if (r["version"] or 1) > 1 else ""), r["charges"] or []


def _discounts(cur, tenant_id: int, ids: list) -> dict:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    cur.execute("""SELECT id, discount_type, discount_value FROM products
                    WHERE tenant_id=%s AND id = ANY(%s) AND COALESCE(discount_value, 0) > 0""", (tenant_id, ids))
    return {r["id"]: (r["discount_type"] or "percent", float(r["discount_value"])) for r in cur.fetchall()}


def _descriptions(cur, tenant_id: int, ids: list) -> dict:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    cur.execute("""SELECT id, name, left(COALESCE(description, ''), 600) AS d, left(COALESCE(attributes, ''), 300) AS a
                     FROM products WHERE tenant_id=%s AND id = ANY(%s)""", (tenant_id, ids))
    return {r["id"]: (r["name"] + ": " + " ".join(x for x in (r["d"], r["a"]) if x)).strip() for r in cur.fetchall()}


_STOP = {"the", "a", "an", "of", "for", "and", "with", "in", "on", "to", "some", "pcs", "pc", "units", "unit",
         "x", "case", "cases", "box", "boxes", "pack", "packs", "per"}


def _tokens(text: str) -> set:
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w
            for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP}


def _candidates(name: str, products: dict) -> list:
    """Products the AI Agent may choose from for a not-on-list line: the ones
    sharing words with it, then (for other languages) the whole list, capped."""
    want = _tokens(name)
    scored = sorted(products.values(), key=lambda p: (-len(want & _tokens(p["name"])), p["name"].lower()))
    hits = [p for p in scored if want & _tokens(p["name"])]
    return (hits + [p for p in scored if p not in hits])[:MAX_CANDIDATES] if len(products) <= 60 else hits[:MAX_CANDIDATES]


# ── the one AI Agent call ───────────────────────────────────────────────────

def _ask(prompt: str) -> dict:
    import ai_designer as D
    from openai import OpenAI
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("no AI key")
    client = OpenAI(api_key=key, timeout=TIMEOUT, max_retries=0)
    resp = client.chat.completions.create(
        model=os.getenv("QUOTE_DRAFT_MODEL", os.getenv("LEAD_QUALIFY_MODEL", D.TEXT_MODEL)),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"})
    return json.loads(resp.choices[0].message.content or "{}")


def _prompt(biz: dict, req: dict, d: dict, cand: dict, desc: dict, lang: str, total_text: str) -> str:
    lines = []
    for i, ln in enumerate(d["lines"]):
        lines.append({"line": i, "name": ln["name"], "qty": ln.get("qty"), "unit": ln.get("unit") or "",
                      "on_list": bool(ln.get("product_id"))})
    offers = {str(i): [{"id": p["id"], "name": p["name"]} for p in ps] for i, ps in cand.items()}
    return f"""You help a salesperson at {biz['name']} prepare a quotation. You only fill a DRAFT; a person checks and sends it.

BUYER'S WORDS (their request, any language):
\"\"\"{(req.get('buyer_words') or '')[:3000]}\"\"\"
Notes from the team: \"\"\"{(req.get('notes') or '')[:800]}\"\"\"

QUOTATION LINES: {json.dumps(lines, ensure_ascii=False)}
PRODUCT DETAILS PhiXtra knows: {json.dumps(list(desc.values()), ensure_ascii=False)[:4000]}
BUSINESS DETAILS: payment terms: "{biz.get('payment_terms') or ''}"; deposit: {biz.get('deposit_pct') or 0}%.
For lines that are NOT on the business's list, the products you may suggest (ids): {json.dumps(offers, ensure_ascii=False)}

Return JSON:
{{"questions": [{{"q": "the buyer's question, short, in English", "answer": "answer in English, or \\"\\" when PhiXtra's facts above don't answer it"}}],
 "suggest": {{"<line number>": {{"ids": ["up to 3 product ids from the list given for that line, closest first"], "why": "one short English sentence"}}}},
 "cover": {{"subject": "email subject in the buyer's language ({lang})", "body": "short covering message in the buyer's language ({lang})"}}}}

Rules:
- questions: only real questions the buyer asked about this order (fit, size, delivery, payment, stock…). Answer ONLY from the facts above. Never invent delivery dates, lead times, stock, prices or terms: leave "answer" empty instead. Max 5.
- suggest: only for lines not on the list, only ids given for that line. Suggest a product ONLY if it is the same kind of item and could really replace what the buyer asked for (a paper straw for a bamboo straw: yes; a cup for a straw: NO). When nothing is the same kind of item, return "ids": []. Never invent products.
- cover: never mention lines with "on_list": false (they may not be on the final quotation). Greet the buyer by first name if known ({(req.get('buyer_name') or '').split(' ')[0]}), say the quotation is attached, mention briefly what it covers and anything changed (e.g. a substitute product, part delivery). Do NOT state any prices, amounts or totals (they can still change before sending). Do NOT write anything about delivery dates or timing, and do NOT mention questions you could not answer (the salesperson answers those). Do NOT add a link or sign-off name; end with a polite closing line only. Max 90 words.
"""


# ── preparing the draft ─────────────────────────────────────────────────────

def prepare(tenant_id: int, qid: int, req: dict) -> dict:
    """Fill the new draft. Returns {"status": "done"|"failed"|"off", "n": changes}."""
    import quote_quotes as qq
    if not enabled(tenant_id):
        return {"status": "off", "n": 0}
    q = qq.get(tenant_id, qid)
    if not q or q["status"] != "draft" or (q["version"] or 1) > 1 or (q["data"] or {}).get("ai_draft"):
        return {"status": "off", "n": 0}
    d = json.loads(json.dumps(q["data"], default=_plain))
    base = {k: json.loads(json.dumps(d.get(k), default=_plain)) for k in ("lines", "charges", "plan", "note")}
    changes = []
    shopify = bool(d.get("shopify_site"))
    conn, cur = _db()
    try:
        history = _buyer_history(cur, tenant_id, req)
        ids = [ln.get("product_id") for ln in d["lines"]]
        disc = _discounts(cur, tenant_id, ids)
        products = qr.products_for(tenant_id)
        desc = _descriptions(cur, tenant_id, ids)
        last_num, last_charges = _last_charges(cur, tenant_id, req, q.get("currency"))
    finally:
        cur.close(); conn.close()

    # 1. Prices (code only; Shopify trade prices are left as they are).
    if not shopify:
        for ln in d["lines"]:
            pid = ln.get("product_id")
            if not pid:
                continue
            was = ln.get("price")
            new, why = None, None
            for h in history:
                if (h.get("currency") or q.get("currency")) != q.get("currency"):
                    continue
                old = next((x for x in h["lines"] if x.get("product_id") == pid and x.get("price") is not None), None)
                if old:
                    new = _money(float(old["price"]) * (1 - float(old.get("disc") or 0) / 100))
                    why = f"Their price on {h['number']}, accepted {_dmy(h['accepted'])}"
                    break
            if new is None and pid in disc and ln.get("list_price") is not None:
                kind, val = disc[pid]
                lp = float(ln["list_price"])
                new = _money(lp * (1 - val / 100)) if kind == "percent" else _money(max(lp - val, 0))
                why = f"{'%g' % val}% discount set in My Products" if kind == "percent" else f"Discount set in My Products"
            if new is not None and new >= 0 and (was is None or abs(float(was) - new) > 0.004):
                if ln.get("list_price") is not None:
                    why += f" (list {qr.money(ln['list_price'], q.get('currency'))})"
                ln["price"] = new
                ln["ai"] = {"price": {"was": was, "why": why}}
                changes.append({"kind": "price", "target": ln["name"], "value": new, "why": why})

    # 2. Delivery charge from this buyer's last quotation.
    if not d.get("charges") and last_charges:
        d["charges"] = []
        for c in last_charges[:3]:
            if c.get("name") and c.get("price") is not None:
                d["charges"].append({"name": c["name"], "qty": c.get("qty") or 1, "price": float(c["price"]),
                                     "ai": {"why": f"Same charge as {last_num}"}})
                changes.append({"kind": "charge", "target": c["name"], "value": float(c["price"]),
                                "why": f"Same charge as {last_num}"})

    # 3. Stock short → delivery plan (only when the design shows a plan).
    s_design = qd.get(tenant_id)
    if not d.get("plan") and s_design.get("template") == "summary":
        now, later = [], []
        for ln in d["lines"]:
            if not ln.get("product_id"):
                continue          # not on the list: PhiXtra can't know its stock
            p = products.get(ln.get("product_id"))
            stock = qr.stock_of(p)
            qty = float(ln.get("qty") or 0)
            unit = (" " + ln["unit"]) if ln.get("unit") else ""
            if stock is None or qty <= stock:
                now.append(f"{'%g' % qty}{unit} {ln['name']}")
            elif stock > 0:
                now.append(f"{'%g' % stock}{unit} {ln['name']}")
                later.append(f"{'%g' % (qty - stock)}{unit} {ln['name']}")
            else:
                later.append(f"{'%g' % qty}{unit} {ln['name']}")
        if later:
            why = "Not enough stock for everything"
            if now:
                d["plan"].append({"date": "From stock now", "text": " + ".join(now)[:200], "ai": {"why": why}})
            d["plan"].append({"date": "", "text": " + ".join(later)[:200], "ai": {"why": why}})
            changes.append({"kind": "plan", "target": "Delivery plan", "why": why + ": the rest needs your date"})

    # 4. The AI Agent: not-on-list suggestions, answers, covering message.
    status = "done"
    lang = qq.detect_lang(req.get("buyer_words") or req.get("notes") or "")
    cand = {i: _candidates(ln["name"], products) for i, ln in enumerate(d["lines"]) if not ln.get("product_id")}
    cand = {i: c for i, c in cand.items() if c}
    try:
        doc = qq.to_doc(tenant_id, {**q, "data": d}, s_design, req)
        total_text = qr.money(doc["total"], q.get("currency"))
        biz = {"name": qd.profile(tenant_id)["name"], "payment_terms": s_design.get("payment_terms") or "",
               "deposit_pct": s_design.get("deposit_pct") or 0}
        a = _ask(_prompt(biz, req, d, cand, desc, dict(qq.LANGS).get(lang, "English"), total_text))
    except Exception as e:
        print(f"⚠️ quote AI draft {qid}: {e}")
        a, status = {}, ("failed" if not changes else "partial")

    qa = []
    for item in (a.get("questions") or [])[:5]:
        if not isinstance(item, dict):
            continue
        qtext = re.sub(r"\s+", " ", str(item.get("q") or "")).strip()[:200]
        ans = re.sub(r"[ \t]+", " ", str(item.get("answer") or "")).strip()[:500]
        if not qtext:
            continue
        qa.append({"q": qtext, "a": ans, "ai": bool(ans)})
        if ans:
            changes.append({"kind": "answer", "target": qtext, "why": "Answered from what PhiXtra knows"})
    if qa:
        d["qa"] = qa

    sug = a.get("suggest") if isinstance(a.get("suggest"), dict) else {}
    for k, v in sug.items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        allowed = {p["id"]: p for p in cand.get(i, [])}
        ids = [x for x in ((v or {}).get("ids") or []) if x in allowed][:3] if isinstance(v, dict) else []
        if ids:
            d["lines"][i]["ai_suggest"] = [{"product_id": x, "name": allowed[x]["name"],
                                            "price": (float(allowed[x]["price"]) if allowed[x].get("price") is not None else None),
                                            "sku": allowed[x].get("sku") or ""}
                                           for x in ids]
            d["lines"][i]["ai_suggest_why"] = re.sub(r"\s+", " ", str(v.get("why") or "")).strip()[:200]

    cov = a.get("cover") if isinstance(a.get("cover"), dict) else {}
    cover = None
    if cov.get("body"):
        body = str(cov["body"]).replace("\\n", "\n").strip()[:2500]
        # Safety net: a message naming a product that isn't on the list could
        # promise something the quotation won't have → the standard message is used.
        off = [ln["name"].lower() for ln in d["lines"] if not ln.get("product_id") and len(ln["name"]) >= 4]
        if not any(o in body.lower() for o in off):
            cover = {"lang": lang, "subject": re.sub(r"\s+", " ", str(cov.get("subject") or "")).strip()[:200],
                     "body": body}

    d["ai_draft"] = {"at": date.today().isoformat(), "status": status, "n": len(changes), "changes": changes,
                     "cover": cover, "base": base}
    d = json.loads(json.dumps(d, default=_plain))   # Decimal/date from the database → plain JSON
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET data=%s, updated_at=NOW() WHERE id=%s AND tenant_id=%s AND status='draft'",
                    (psycopg2.extras.Json(d), qid, tenant_id))
        if changes or qa or cover:
            qr._event(cur, req["id"], f"Draft prepared by AI Agent ({len(changes)} change{'s' if len(changes) != 1 else ''})",
                      "AI Agent")
        conn.commit()
    finally:
        cur.close(); conn.close()
    try:
        tot = qq.to_doc(tenant_id, {**q, "data": d}, s_design, req)["total"]
        conn, cur = _db()
        cur.execute("UPDATE quotations SET total=%s WHERE id=%s", (tot, qid)); conn.commit()
        cur.close(); conn.close()
    except Exception:
        pass
    return {"status": status, "n": len(changes)}


def start_again(tenant_id: int, qid: int) -> bool:
    """'Start again without AI Agent': the draft as it was before."""
    import quote_quotes as qq
    q = qq.get(tenant_id, qid)
    if not q or q["status"] != "draft":
        return False
    d = dict(q["data"])
    base = (d.get("ai_draft") or {}).get("base")
    if not base:
        return False
    for k, v in base.items():
        d[k] = v
    d.pop("qa", None)
    d["ai_draft"] = {"status": "removed", "n": 0, "changes": [], "cover": None}
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET data=%s, updated_at=NOW() WHERE id=%s AND tenant_id=%s AND status='draft'",
                    (psycopg2.extras.Json(d), qid, tenant_id))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return True
