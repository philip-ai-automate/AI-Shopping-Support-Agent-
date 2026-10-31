"""
quote_quotes.py — PhiXtra quotations, Part B (31 Oct 2026): making a
quotation from a request and sending it. Mock-up
https://claude.ai/artifact/PKhjF22chVUgXF9mFNaomK sections 3 and 5
(user: "start b").

For businesses whose Quotation settings say "In PhiXtra". "Make quotation"
on a request fills a draft from the request and the product list; the
salesperson sets every price; PhiXtra checks it (never sets a price), makes
the PDF in the business's design (quote_design) and sends it by email (from
PhiXtra's mail service in the business's name, replies to the salesperson)
or WhatsApp (a document, inside the buyer's 24-hour window). Sending records
"Quote sent" on the request by itself, so follow-ups and the AI reading the
buyer's answer (Phase 3) carry on as before. The quotation is English; the
covering message is in the buyer's language.
"""
import html as _html
import re
from datetime import date, datetime, timedelta

import psycopg2.extras

from db import get_db_connection
import quote_design as qd
import quote_requests as qr

MAX_LINES = 200
TAX_DEFAULT = {"name": "VAT", "rate": 0, "reason": ""}


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


_tables_ok = False


def ensure_tables():
    global _tables_ok
    if _tables_ok:
        return
    conn, cur = _db()
    try:
        cur.execute("SELECT to_regclass('quotations') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            cur.execute("SET lock_timeout = '5s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS quotations (
                               id           BIGSERIAL PRIMARY KEY,
                               tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                               request_id   BIGINT REFERENCES quote_requests(id) ON DELETE SET NULL,
                               number       VARCHAR(40) NOT NULL,
                               version      INTEGER NOT NULL DEFAULT 1,
                               status       VARCHAR(12) NOT NULL DEFAULT 'draft',
                               data         JSONB NOT NULL DEFAULT '{}'::jsonb,
                               total        NUMERIC(14,2),
                               currency     VARCHAR(3),
                               valid_until  DATE,
                               pdf          BYTEA,
                               sent_at      TIMESTAMPTZ,
                               sent_via     VARCHAR(12),
                               sent_to      VARCHAR(255),
                               sent_by      VARCHAR(200),
                               created_by   VARCHAR(200),
                               created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                               updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                               UNIQUE (tenant_id, number, version))""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_quotations_request ON quotations (request_id, version DESC)")
            conn.commit()
        _tables_ok = True
    except Exception as e:
        conn.rollback()
        print("⚠️ quotations tables:", e)
    finally:
        cur.close(); conn.close()


def is_on(tenant_id: int) -> bool:
    return qr.allowed(tenant_id) and qr.settings(tenant_id).get("maker") == "phixtra"


# ── numbers ─────────────────────────────────────────────────────────────────

def _take_number(cur, tenant_id: int) -> str:
    """The next quotation number, never reused (a deleted draft leaves a gap)."""
    qd.ensure_tables()
    cur.execute("SELECT settings FROM quote_design WHERE tenant_id=%s FOR UPDATE", (tenant_id,))
    r = cur.fetchone()
    s = qd.clean(r["settings"] if r else {})
    n = int(s["next_number"])
    num = qd.number_text(s, n, date.today().year)
    while True:     # someone may have typed a "next number" already used
        cur.execute("SELECT 1 FROM quotations WHERE tenant_id=%s AND number=%s LIMIT 1", (tenant_id, num))
        if not cur.fetchone():
            break
        n += 1
        num = qd.number_text(s, n, date.today().year)
    s["next_number"] = n + 1
    if r:
        cur.execute("UPDATE quote_design SET settings=%s, updated_at=NOW() WHERE tenant_id=%s",
                    (psycopg2.extras.Json(s), tenant_id))
    else:
        cur.execute("INSERT INTO quote_design (tenant_id, settings) VALUES (%s,%s)", (tenant_id, psycopg2.extras.Json(s)))
    return num


# ── the draft ───────────────────────────────────────────────────────────────

def _person(cur, tenant_id: int, key: str, label: str) -> dict:
    """The salesperson as shown on the quotation: name, email, phone."""
    out = {"name": label or "", "email": "", "phone": ""}
    key = key or ""
    if key.startswith("team:") and key[5:].isdigit():
        cur.execute("""SELECT first_name, last_name, name, email, alert_phone FROM team_members
                        WHERE id=%s AND tenant_id=%s""", (int(key[5:]), tenant_id))
        t = cur.fetchone()
        if t:
            out = {"name": (f"{t['first_name'] or ''} {t['last_name'] or ''}").strip() or t["name"],
                   "email": t["email"] or "", "phone": t["alert_phone"] or ""}
    elif key.startswith("owner:") and key[6:].isdigit():
        cur.execute("SELECT first_name, last_name, email FROM customers WHERE id=%s AND tenant_id=%s",
                    (int(key[6:]), tenant_id))
        c = cur.fetchone()
        if c:
            out = {"name": (f"{c['first_name'] or ''} {c['last_name'] or ''}").strip() or label or "",
                   "email": c["email"] or "", "phone": ""}
    return out


def for_request(tenant_id: int, request_id: int) -> list:
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT id, number, version, status, total, currency, valid_until, sent_at, sent_via, created_at
                         FROM quotations WHERE tenant_id=%s AND request_id=%s ORDER BY version DESC, id DESC""",
                    (tenant_id, request_id))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def get(tenant_id: int, qid: int):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT id, tenant_id, request_id, number, version, status, data, total, currency, valid_until,
                              sent_at, sent_via, sent_to, sent_by, created_by, created_at, updated_at,
                              (pdf IS NOT NULL) AS has_pdf
                         FROM quotations WHERE id=%s AND tenant_id=%s""", (qid, tenant_id))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def start(tenant_id: int, req: dict, actor: dict) -> int:
    """The request's draft quotation (made now from the request if there is none)."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT pg_advisory_xact_lock(80301033, %s)", (tenant_id,))
        cur.execute("""SELECT id FROM quotations WHERE tenant_id=%s AND request_id=%s AND status='draft'
                        ORDER BY id DESC LIMIT 1""", (tenant_id, req["id"]))
        r = cur.fetchone()
        if r:
            conn.commit()
            return r["id"]
        cur.execute("""SELECT number, version, data FROM quotations WHERE tenant_id=%s AND request_id=%s
                        ORDER BY version DESC LIMIT 1""", (tenant_id, req["id"]))
        prev = cur.fetchone()
        s = qd.get(tenant_id)
        if prev:
            # A new version of the quotation already sent: same number, its lines.
            number, version, data = prev["number"], prev["version"] + 1, dict(prev["data"])
            data["valid_until"] = (date.today() + timedelta(days=s["valid_days"])).isoformat()
        else:
            number, version = _take_number(cur, tenant_id), 1
            products = qr.products_for(tenant_id, [ln.get("product_id") for ln in req["lines"]])
            lines = []
            for ln in req["lines"]:
                p = products.get(ln.get("product_id"))
                lines.append({"product_id": ln.get("product_id"), "name": ln["name"], "code": "",
                              "qty": ln.get("qty") or 0, "unit": ln.get("unit") or "",
                              "list_price": float(p["price"]) if p and p.get("price") is not None and not p.get("price_on_request") else None,
                              "price": float(p["price"]) if p and p.get("price") is not None and not p.get("price_on_request") else None,
                              "disc": 0, "note": ln.get("note") or ""})
            cur.execute("""SELECT data->'tax' AS tax FROM quotations WHERE tenant_id=%s AND status='sent'
                            ORDER BY sent_at DESC LIMIT 1""", (tenant_id,))
            last = cur.fetchone()
            data = {"lines": lines, "charges": [], "tax": (last or {}).get("tax") or dict(TAX_DEFAULT),
                    "valid_until": (date.today() + timedelta(days=s["valid_days"])).isoformat(),
                    "deliver_place": req.get("deliver_to") or "", "deliver_terms": "",
                    "note": "", "plan": [], "prepared_by": _person(cur, tenant_id, req.get("assigned_key"),
                                                                   req.get("assigned_to") or actor["label"])}
        cur.execute("""INSERT INTO quotations (tenant_id, request_id, number, version, status, data, currency,
                                               valid_until, created_by)
                       VALUES (%s,%s,%s,%s,'draft',%s,%s,%s,%s) RETURNING id""",
                    (tenant_id, req["id"], number, version, psycopg2.extras.Json(data), req.get("currency"),
                     data["valid_until"], actor["label"]))
        qid = cur.fetchone()["id"]
        qr._event(cur, req["id"], f"Quotation {number}{' v%d' % version if version > 1 else ''} started", actor["label"])
        conn.commit()
        return qid
    finally:
        cur.close(); conn.close()


def _num(v, lo=0.0, hi=1e12):
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return round(x, 4) if lo <= x <= hi else None


def clean_form(raw: dict) -> dict:
    """The editor's JSON → saved data. Prices are left empty (None) when
    not typed: the checks then say so; nothing is guessed."""
    out = {"lines": [], "charges": [], "plan": []}
    for ln in (raw.get("lines") or [])[:MAX_LINES]:
        if not isinstance(ln, dict):
            continue
        name = re.sub(r"\s+", " ", str(ln.get("name") or "")).strip()[:300]
        if not name:
            continue
        out["lines"].append({"product_id": (str(ln.get("product_id") or "")[:36] or None), "name": name,
                             "code": re.sub(r"\s+", " ", str(ln.get("code") or "")).strip()[:40],
                             "qty": _num(ln.get("qty"), 0, 1e9) or 0,
                             "unit": re.sub(r"\s+", " ", str(ln.get("unit") or "")).strip()[:40],
                             "list_price": _num(ln.get("list_price")),
                             "price": _num(ln.get("price")) if str(ln.get("price") or "").strip() != "" else None,
                             "disc": _num(ln.get("disc"), 0, 100) or 0,
                             "note": re.sub(r"\s+", " ", str(ln.get("note") or "")).strip()[:200]})
    for c in (raw.get("charges") or [])[:20]:
        if not isinstance(c, dict):
            continue
        name = re.sub(r"\s+", " ", str(c.get("name") or "")).strip()[:120]
        price = _num(c.get("price"))
        if name and price is not None:
            out["charges"].append({"name": name, "qty": _num(c.get("qty"), 0, 1e6) or 1, "price": price})
    for st in (raw.get("plan") or [])[:6]:
        if isinstance(st, dict) and (st.get("date") or st.get("text")):
            out["plan"].append({"date": str(st.get("date") or "").strip()[:40], "text": str(st.get("text") or "").strip()[:200]})
    t = raw.get("tax") if isinstance(raw.get("tax"), dict) else {}
    out["tax"] = {"name": re.sub(r"\s+", " ", str(t.get("name") or "VAT")).strip()[:20] or "VAT",
                  "rate": _num(t.get("rate"), 0, 100) or 0,
                  "reason": re.sub(r"\s+", " ", str(t.get("reason") or "")).strip()[:120]}
    try:
        vu = datetime.strptime(str(raw.get("valid_until") or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        vu = date.today() + timedelta(days=14)
    out["valid_until"] = vu.isoformat()
    for k, n in (("deliver_place", 200), ("deliver_terms", 200)):
        out[k] = re.sub(r"\s+", " ", str(raw.get(k) or "")).strip()[:n]
    out["note"] = str(raw.get("note") or "").strip()[:1500]
    return out


def save(tenant_id: int, qid: int, raw: dict, actor: dict) -> tuple:
    q = get(tenant_id, qid)
    if not q:
        return False, "Quotation not found."
    if q["status"] != "draft":
        return False, "This quotation has been sent and can't be changed. Make a new version instead."
    d = clean_form(raw)
    d["prepared_by"] = q["data"].get("prepared_by") or {}
    doc = to_doc(tenant_id, {**q, "data": d}, qd.get(tenant_id))
    conn, cur = _db()
    try:
        cur.execute("""UPDATE quotations SET data=%s, total=%s, valid_until=%s, updated_at=NOW()
                        WHERE id=%s AND status='draft'""",
                    (psycopg2.extras.Json(d), doc["total"], d["valid_until"], qid))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return True, "Saved."


def delete_draft(tenant_id: int, qid: int, actor: dict) -> tuple:
    conn, cur = _db()
    try:
        cur.execute("DELETE FROM quotations WHERE id=%s AND tenant_id=%s AND status='draft' RETURNING request_id, number",
                    (qid, tenant_id))
        r = cur.fetchone()
        if r and r["request_id"]:
            qr._event(cur, r["request_id"], f"Draft quotation {r['number']} deleted", actor["label"])
        conn.commit()
        return (True, "Draft deleted. Its number isn't used again.") if r else (False, "Only a draft can be deleted.")
    finally:
        cur.close(); conn.close()


# ── the document ────────────────────────────────────────────────────────────

def full_number(q) -> str:
    return q["number"] + (f" v{q['version']}" if q["version"] > 1 else "")


def to_doc(tenant_id: int, q: dict, s: dict, req: dict = None) -> dict:
    """The quotation as the document template wants it (with totals)."""
    d = q["data"]
    if req is None and q.get("request_id"):
        req = qr.get(tenant_id, q["request_id"])
    req = req or {}
    tax = d.get("tax") or TAX_DEFAULT
    rate = float(tax.get("rate") or 0)
    created = q.get("sent_at") or q.get("created_at") or datetime.now()
    doc = {"number": full_number(q), "date": created.date() if hasattr(created, "date") else date.today(),
           "valid_until": datetime.strptime(d["valid_until"], "%Y-%m-%d").date(),
           "ref": f"R-{req['ref_no']}" if req.get("ref_no") else "",
           "buyer": {"company": req.get("buyer_company") or "", "name": req.get("buyer_name") or "", "role": "",
                     "email": req.get("buyer_email") or req.get("buyer_phone") or ""},
           "deliver": {"place": d.get("deliver_place") or "", "terms": d.get("deliver_terms") or ""},
           "prepared_by": d.get("prepared_by") or {},
           "lines": [{"code": ln.get("code") or "", "name": ln["name"], "pack": "", "qty": ln["qty"],
                      "unit": ln.get("unit") or "", "price": ln["price"] or 0, "disc": ln.get("disc") or 0,
                      "note": ln.get("note") or ("Discount" if ln.get("disc") else "")} for ln in d["lines"]],
           "charges": [dict(c) for c in d.get("charges") or []],
           "tax": {"rate": rate, "label": f"{tax.get('name') or 'VAT'} {('%g' % rate)}%", "reason": tax.get("reason") or ""},
           "note": d.get("note") or "", "plan": d.get("plan") or [], "currency": q.get("currency") or "GBP"}
    return qd.totals(doc, s)


def checks(tenant_id: int, q: dict, req: dict) -> list:
    """What to look at before sending: [(level ok|warn|bad, text)]. Rules,
    not the AI's guesses: nothing here ever changes a price."""
    d = q["data"]
    out = []
    asked = {}
    for ln in req.get("lines") or []:
        asked[(ln.get("product_id") or ln["name"].lower())] = (ln["name"], ln.get("qty"))
    have = {(ln.get("product_id") or ln["name"].lower()): ln for ln in d["lines"]}
    missing = [n for k, (n, _) in asked.items() if k not in have]
    changed = [f"{n} ({'%g' % (have[k]['qty'] or 0)} instead of {'%g' % qty})" for k, (n, qty) in asked.items()
               if k in have and qty is not None and float(have[k]["qty"] or 0) != float(qty)]
    if missing:
        out.append(("bad", "Not on the quotation but the buyer asked for: " + ", ".join(missing)))
    elif not changed:
        out.append(("ok", "Every product the buyer asked for is on it, same quantities"))
    if changed:
        out.append(("warn", "Quantity changed from the request: " + "; ".join(changed)))
    unpriced = [ln["name"] for ln in d["lines"] if ln.get("price") in (None, "")]
    out.append(("bad", "No price yet: " + ", ".join(unpriced)) if unpriced else ("ok", "Every line has a price"))
    zero = [ln["name"] for ln in d["lines"] if ln.get("price") == 0]
    if zero:
        out.append(("warn", "Price is 0: " + ", ".join(zero)))
    below = [ln["name"] for ln in d["lines"] if ln.get("list_price") and ln.get("price") is not None
             and ln["price"] * (1 - (ln.get("disc") or 0) / 100) < ln["list_price"] * 0.7]
    if below:
        out.append(("warn", "More than 30% below your list price: " + ", ".join(below)))
    if any((ln.get("qty") or 0) <= 0 for ln in d["lines"]):
        out.append(("bad", "A line has no quantity"))
    if not d["lines"]:
        out.append(("bad", "The quotation has no products"))
    products = qr.products_for(tenant_id, [ln.get("product_id") for ln in d["lines"]])
    short = []
    for ln in d["lines"]:
        p = products.get(ln.get("product_id"))
        if p and not p.get("is_service"):
            stock = int((p.get("stock_quantity") or 0) - (p.get("reserved_quantity") or 0))
            if (ln.get("qty") or 0) > stock:
                short.append(f"{ln['name']} ({stock} in stock)")
    if short:
        out.append(("warn", "Not enough stock for: " + "; ".join(short) + ". Say when the rest comes, in the note"))
    words = (req.get("buyer_words") or "") + "\n" + (req.get("notes") or "")
    if "?" in words and not d.get("note"):
        out.append(("warn", "The buyer asked a question in their request. Answer it in the note"))
    try:
        vu = datetime.strptime(d["valid_until"], "%Y-%m-%d").date()
        if vu < date.today():
            out.append(("bad", "'Valid until' is in the past"))
        if req.get("needed_by") and vu > req["needed_by"]:
            out.append(("warn", f"Valid until is after the date the buyer needs it ({req['needed_by'].strftime('%d %b')})"))
    except ValueError:
        out.append(("bad", "'Valid until' isn't a date"))
    if not qd.ready(tenant_id):
        out.append(("bad", "Finish Quotation design first (business address, payment terms, bank details)"))
    return out


def can_send(chk: list) -> bool:
    return not any(level == "bad" for level, _ in chk)


def render(tenant_id: int, q: dict, screen=True, req=None) -> str:
    s = qd.get(tenant_id)
    return qd.render_html(tenant_id, s, to_doc(tenant_id, q, s, req), screen=screen)


def make_pdf(tenant_id: int, q: dict, req=None) -> bytes:
    s = qd.get(tenant_id)
    doc = to_doc(tenant_id, q, s, req)
    prof = qd.profile(tenant_id)
    return qd.render_pdf(qd.render_html(tenant_id, s, doc, screen=False), s.get("footer") or prof["name"], doc["number"])


def pdf_name(tenant_id: int, q: dict) -> str:
    biz = re.sub(r"[^A-Za-z0-9 &-]+", "", qd.profile(tenant_id)["name"]).strip()[:40]
    return f"{full_number(q)} {biz}.pdf".strip()


def stored_pdf(tenant_id: int, qid: int):
    conn, cur = _db()
    try:
        cur.execute("SELECT pdf FROM quotations WHERE id=%s AND tenant_id=%s", (qid, tenant_id))
        r = cur.fetchone()
        return bytes(r["pdf"]) if r and r["pdf"] is not None else None
    finally:
        cur.close(); conn.close()


# ── the covering message (buyer's language) ─────────────────────────────────

_WORDS = {
    "fr": {"bonjour", "merci", "je", "vous", "nous", "pour", "avec", "les", "des", "une", "votre", "prix", "devis",
           "livraison", "commande", "il", "faut", "combien", "s'il", "plaît"},
    "es": {"hola", "gracias", "por", "favor", "usted", "para", "con", "los", "las", "una", "precio", "necesito",
           "quiero", "presupuesto", "pedido", "envío", "cuánto", "buenos", "días"},
    "pt": {"olá", "obrigado", "obrigada", "você", "para", "com", "os", "uma", "preço", "preciso", "quero",
           "orçamento", "entrega", "pedido", "bom", "dia", "quanto", "prazo"},
    "de": {"guten", "tag", "danke", "bitte", "wir", "sie", "ihr", "ihre", "und", "für", "mit", "die", "der",
           "das", "angebot", "anfrage", "lieferung", "preis", "brauchen", "möglich", "grüßen", "stück", "kartons"},
}


def detect_lang(text: str) -> str:
    t = (text or "").lower()
    if not t.strip():
        return "en"
    letters = [c for c in t if c.isalpha()]
    if letters and sum(1 for c in letters if "؀" <= c <= "ۿ") / len(letters) > 0.3:
        return "ar"
    words = re.findall(r"[a-zà-ÿßäöü']+", t)
    if not words:
        return "en"
    scores = {k: sum(1 for w in words if w in v) for k, v in _WORDS.items()}
    if re.search(r"[äöüß]", t):
        scores["de"] += 2
    if re.search(r"[ãõ]|ção", t):
        scores["pt"] += 2
    if re.search(r"[ñ¿¡]", t):
        scores["es"] += 2
    if re.search(r"[èêëàâçùûœ]", t):
        scores["fr"] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 and scores[best] >= 0.15 * len(words) else "en"


LANGS = [("en", "English"), ("fr", "French"), ("es", "Spanish"), ("pt", "Portuguese"), ("de", "German"), ("ar", "Arabic")]
COVER = {
    "en": ("Quotation {number} · {biz}",
           "Hello {name},\n\nPlease find attached our quotation {number} for your request, valid until {valid}.\n\n"
           "To accept, just reply with your order number. Any questions, I'm happy to help.\n\nKind regards,\n{sender}\n{biz}"),
    "fr": ("Devis {number} · {biz}",
           "Bonjour {name},\n\nVeuillez trouver ci-joint notre devis {number} pour votre demande, valable jusqu'au {valid}.\n\n"
           "Pour l'accepter, répondez simplement avec votre numéro de commande. Je reste à votre disposition pour toute question.\n\n"
           "Cordialement,\n{sender}\n{biz}"),
    "es": ("Presupuesto {number} · {biz}",
           "Hola {name}:\n\nAdjuntamos nuestro presupuesto {number} para su solicitud, válido hasta el {valid}.\n\n"
           "Para aceptarlo, responda con su número de pedido. Quedo a su disposición para cualquier pregunta.\n\n"
           "Saludos cordiales,\n{sender}\n{biz}"),
    "pt": ("Orçamento {number} · {biz}",
           "Olá {name},\n\nSegue em anexo o nosso orçamento {number} para o seu pedido, válido até {valid}.\n\n"
           "Para aceitar, basta responder com o número da sua encomenda. Estou à disposição para qualquer dúvida.\n\n"
           "Com os melhores cumprimentos,\n{sender}\n{biz}"),
    "de": ("Angebot {number} · {biz}",
           "Guten Tag {name},\n\nanbei erhalten Sie unser Angebot {number} zu Ihrer Anfrage, gültig bis {valid}.\n\n"
           "Zur Bestellung antworten Sie einfach mit Ihrer Bestellnummer. Bei Fragen helfe ich Ihnen gern.\n\n"
           "Mit freundlichen Grüßen\n{sender}\n{biz}"),
    "ar": ("عرض السعر {number} · {biz}",
           "مرحبًا {name}،\n\nتجدون مرفقًا عرض السعر {number} لطلبكم، وهو ساري حتى {valid}.\n\n"
           "للموافقة، يكفي الرد برقم طلبكم. يسعدني الإجابة عن أي سؤال.\n\nمع أطيب التحيات،\n{sender}\n{biz}"),
}


def cover(lang: str, q: dict, req: dict, tenant_id: int) -> tuple:
    lang = lang if lang in COVER else "en"
    subj, body = COVER[lang]
    vu = datetime.strptime(q["data"]["valid_until"], "%Y-%m-%d").date()
    vals = {"number": full_number(q), "biz": qd.profile(tenant_id)["name"],
            "name": (req.get("buyer_name") or "").split()[0] if req.get("buyer_name") else "",
            "valid": vu.strftime("%d.%m.%Y") if lang in ("de", "fr", "es", "pt") else vu.strftime("%d %B %Y").lstrip("0"),
            "sender": (q["data"].get("prepared_by") or {}).get("name") or ""}
    text = body.format(**vals).replace(" ,", ",").replace("Hello ,", "Hello,").replace("Bonjour ,", "Bonjour,")
    text = text.replace("Hola :", "Hola:").replace("Olá ,", "Olá,").replace("Guten Tag ,", "Guten Tag,")
    return subj.format(**vals), text


# ── sending ─────────────────────────────────────────────────────────────────

def wa_ready(tenant_id: int, phone: str) -> tuple:
    """(True, number row) when a WhatsApp document can go now, else (False, why)."""
    import auto_followups as af
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 7:
        return False, "No WhatsApp number for this buyer."
    conn, cur = _db()
    try:
        if not af.window_open(cur, tenant_id, digits):
            return False, ("The buyer hasn't written on WhatsApp in the last 24 hours, so WhatsApp only allows an "
                           "approved template. Send it by email instead.")
        num = af.sending_number(cur, tenant_id, af.get_settings(tenant_id, cur), digits)
        if not num:
            return False, "No WhatsApp number is connected."
        return True, num
    finally:
        cur.close(); conn.close()


def send(tenant_id: int, qid: int, via: str, to: str, subject: str, message: str, actor: dict) -> tuple:
    """Make the PDF, send it, mark the quotation sent and the request Quote sent."""
    q = get(tenant_id, qid)
    if not q or q["status"] != "draft":
        return False, "Only a draft quotation can be sent."
    req = qr.get(tenant_id, q["request_id"]) if q["request_id"] else None
    if not req:
        return False, "The request for this quotation is gone."
    chk = checks(tenant_id, q, req)
    if not can_send(chk):
        return False, "Fix the red items before sending: " + "; ".join(t for lvl, t in chk if lvl == "bad")
    message = (message or "").strip()[:4000]
    if not message:
        return False, "Write a short covering message."
    if not _claim(qid):
        return False, "This quotation is being sent already."
    try:
        ok, why = _send(tenant_id, q, req, via, to, subject, message, actor)
    except Exception as e:
        print(f"⚠️ quotation send {qid}:", e)
        ok, why = False, "Something went wrong sending it. Please try again."
    if not ok:
        _release(qid)
    return ok, why


def _claim(qid: int) -> bool:
    """Mark the draft 'sending' so a second click can't send it twice. One
    left 'sending' by a crash is free again after 5 minutes."""
    conn, cur = _db()
    try:
        cur.execute("""UPDATE quotations SET status='sending', updated_at=NOW() WHERE id=%s
                         AND (status='draft' OR (status='sending' AND updated_at < NOW() - INTERVAL '5 minutes'))""",
                    (qid,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        cur.close(); conn.close()


def _release(qid: int):
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET status='draft', updated_at=NOW() WHERE id=%s AND status='sending'", (qid,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _send(tenant_id, q, req, via, to, subject, message, actor) -> tuple:
    qid = q["id"]
    try:
        pdf = make_pdf(tenant_id, q, req)
    except Exception as e:
        print(f"⚠️ quotation PDF {qid}:", e)
        return False, "The PDF couldn't be made just now. Please try again in a minute."
    fname = pdf_name(tenant_id, q)
    doc = to_doc(tenant_id, q, qd.get(tenant_id), req)
    person = q["data"].get("prepared_by") or {}
    biz = qd.profile(tenant_id)["name"]
    if via == "email":
        to = (to or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
            return False, "That email address doesn't look right."
        from portal_utils import send_email_with_attachment
        s = qd.get(tenant_id)
        reply = person.get("email") or s.get("email") or None
        body_html = ('<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#17212E;white-space:pre-line">'
                     + _html.escape(message) + "</div>")
        ok = send_email_with_attachment(to, (subject or "").strip()[:200] or f"Quotation {full_number(q)}", body_html,
                                        pdf, fname, "application/pdf", text_body=message,
                                        from_name=(f"{person.get('name')} at {biz}" if person.get("name") else biz),
                                        reply_to=reply, cc_email=reply)
        if not ok:
            return False, "The email couldn't be sent just now. Please try again."
    elif via == "whatsapp":
        ok_wa, num = wa_ready(tenant_id, to)
        if not ok_wa:
            return False, num
        ok, why = _send_wa_document(tenant_id, num, re.sub(r"\D", "", to), pdf, fname, message, actor)
        if not ok:
            return False, why
        to = re.sub(r"\D", "", to)
    else:
        return False, "Choose email or WhatsApp."
    conn, cur = _db()
    try:
        cur.execute("""UPDATE quotations SET status='sent', pdf=%s, total=%s, sent_at=NOW(), sent_via=%s, sent_to=%s,
                              sent_by=%s, updated_at=NOW() WHERE id=%s AND status='sending'""",
                    (psycopg2.Binary(pdf), doc["total"], via, to[:255], actor["label"][:200], qid))
        if q["version"] > 1:
            cur.execute("""UPDATE quotations SET status='superseded', updated_at=NOW()
                            WHERE tenant_id=%s AND request_id=%s AND status='sent' AND id<>%s""",
                        (tenant_id, q["request_id"], qid))
        conn.commit()
    finally:
        cur.close(); conn.close()
    ok, msg = qr.mark_sent(tenant_id, q["request_id"], {
        "value": doc["total"], "currency": doc["currency"], "valid_until": doc["valid_until"],
        "number": full_number(q), "via": via, "pdf": pdf, "pdf_name": fname}, actor)
    return True, f"Quotation {full_number(q)} sent by {'email' if via == 'email' else 'WhatsApp'}. " + (msg if ok else "")


def _send_wa_document(tenant_id, num, phone, pdf, fname, caption, actor) -> tuple:
    """Upload the PDF to Meta, send it as a document with the message as its
    caption, and show it in the Inbox like any message the team sends."""
    import requests as _req
    import auto_followups as af
    headers = {"Authorization": f"Bearer {num['access_token']}"}
    try:
        up = _req.post(f"{af.GRAPH}/{num['phone_number_id']}/media", headers=headers,
                       data={"messaging_product": "whatsapp", "type": "application/pdf"},
                       files={"file": (fname, pdf, "application/pdf")}, timeout=30)
        media_id = up.json().get("id") if up.status_code == 200 else None
        if not media_id:
            print(f"⚠️ quotation WA upload {up.status_code} {up.text[:300]}")
            return False, "WhatsApp didn't accept the PDF. Send it by email instead."
        r = _req.post(f"{af.GRAPH}/{num['phone_number_id']}/messages", headers=headers, timeout=20,
                      json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": phone,
                            "type": "document", "document": {"id": media_id, "filename": fname,
                                                             "caption": caption[:1024]}})
        if r.status_code != 200:
            print(f"⚠️ quotation WA send {r.status_code} {r.text[:300]}")
            return False, "WhatsApp didn't accept the message. Send it by email instead."
        meta_id = (r.json().get("messages") or [{}])[0].get("id")
    except Exception as e:
        print("⚠️ quotation WA:", e)
        return False, "Couldn't reach WhatsApp just now. Please try again."
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO wa_message_log (tenant_id, phone_number_id, customer_phone, direction, content,
                           message_type, meta_message_id, sent_by_label)
                       VALUES (%s,%s,%s,'outbound',%s,'agent_reply',%s,%s)""",
                    (tenant_id, num["phone_number_id"], phone, f"📄 {fname}\n\n{caption}", meta_id, actor["label"][:200]))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️ quotation WA log:", e)
    finally:
        cur.close(); conn.close()
    return True, ""
