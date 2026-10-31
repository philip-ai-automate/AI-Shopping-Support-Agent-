"""
quote_public.py — "Add to quote" and the quote list on the business's own
website (Intelligent RFQ Step 1, Phase 4, 31 Oct 2026; design v5
https://claude.ai/artifact/1cF1TzQiVMfM3wNCv43mZJ section 2).

Two ways in, one result (a request in Quotations › Requests):
  - WordPress / WooCommerce: the PhiXtra plugin's own server calls these
    with the business's API key (never shown in the browser).
  - Any other website: the PhiXtra paste-in chat button calls them from the
    visitor's browser with the public website key, accepted only from the
    business's own website address (same rule as the chat box).

Off unless the business switches it on (Quotations › Quotation settings),
has the B2B plan (or is PhiXtra) and has a product list. No buyer accounts:
the buyer gives a name and email (or number) and gets a confirmation email.
"""
import hashlib
import html as _html
import re
import time
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import urlparse

import psycopg2.extras
from flask import Blueprint, jsonify, request

from db import get_db_connection
from feature_access import public_route
import quote_requests as qr

quote_public_bp = Blueprint("quote_public", __name__)

MAX_LINES = 100
_RATE = defaultdict(deque)            # (ip, tenant) -> recent submit times (per worker)
_RATE_MAX, _RATE_WINDOW = 10, 3600    # 10 quote lists sent per visitor per hour (typos don't count)
_CHAT_LINK_HOST = "chat.phixtra.com"
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,24}$")


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _host(value: str) -> str:
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "://" not in v:
        v = "https://" + v
    h = (urlparse(v).hostname or "").strip(".")
    return h[4:] if h.startswith("www.") else h


# ── who is asking ───────────────────────────────────────────────────────────

def _by_site(site_key: str):
    """(tenant row, None) for a public website key used on the business's own
    website, else (None, (status, message))."""
    key = (site_key or "").strip()
    if not key or len(key) > 64:
        return None, (400, "Missing website key")
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    conn, cur = _db()
    try:
        cur.execute("""SELECT t.id AS tenant_id, t.name, t.domain, t.status FROM site_widget_keys k
                         JOIN tenants t ON t.id = k.tenant_id WHERE k.site_key=%s""", (key,))
        row = cur.fetchone()
        if not row or row["status"] not in ("active", "pending"):
            return None, (403, "Unknown website key")
        ok = row["domain"] and _host(origin) == _host(row["domain"])
        if not ok and _host(origin) == _CHAT_LINK_HOST:
            cur.execute("SELECT 1 FROM chat_links WHERE tenant_id=%s AND enabled LIMIT 1", (row["tenant_id"],))
            ok = cur.fetchone() is not None
        if not ok:
            return None, (403, "This only works on the business's own website")
        return dict(row), None
    finally:
        cur.close(); conn.close()


def _by_api_key(api_key: str):
    """(tenant row, None) for the business's secret API key (WordPress plugin,
    server to server), else (None, (status, message)). No side effects (the
    chat's trial clock is not started by a quote list)."""
    import bcrypt
    key = (api_key or "").strip()
    if not key or len(key) > 200:
        return None, (400, "Missing API key")
    conn, cur = _db()
    try:
        cur.execute("""SELECT ak.api_key_hash, t.id AS tenant_id, t.name, t.domain, t.status
                         FROM api_keys ak JOIN tenants t ON t.id = ak.tenant_id
                        WHERE ak.is_active AND ak.api_key_lookup=%s""", (hashlib.sha256(key.encode()).hexdigest(),))
        for row in cur.fetchall():
            try:
                if bcrypt.checkpw(key.encode(), row["api_key_hash"].encode()) and row["status"] in ("active", "pending"):
                    return {k: row[k] for k in ("tenant_id", "name", "domain", "status")}, None
            except Exception:
                continue
        return None, (403, "Invalid API key")
    finally:
        cur.close(); conn.close()


def _who():
    data = request.get_json(silent=True) or {}
    if data.get("api_key") or request.headers.get("X-Phixtra-Key"):
        return _by_api_key(data.get("api_key") or request.headers.get("X-Phixtra-Key")), data, False
    site = data.get("site") or request.args.get("site")
    return _by_site(site), data, True


def _cors(resp, browser: bool):
    if browser:
        origin = request.headers.get("Origin")
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
    return resp


def _err(status, msg, browser):
    return _cors(jsonify({"ok": False, "enabled": False, "error": msg}), browser), status


def _preflight():
    resp = jsonify({})
    origin = request.headers.get("Origin")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Max-Age"] = "600"
    return resp, 204


# ── what the website may show ───────────────────────────────────────────────

def config_for(tenant_id: int) -> dict:
    """Whether the website quote list is on, and what it shows."""
    if not qr.allowed(tenant_id):
        return {"enabled": False}
    s = qr.settings(tenant_id)
    if not s.get("website_on"):
        return {"enabled": False}
    conn, cur = _db()
    try:
        cur.execute("SELECT 1 FROM products WHERE tenant_id=%s AND is_active LIMIT 1", (tenant_id,))
        has_products = cur.fetchone() is not None
        cur.execute("SELECT name FROM tenants WHERE id=%s", (tenant_id,))
        name = (cur.fetchone() or {}).get("name") or ""
    finally:
        cur.close(); conn.close()
    if not has_products:
        return {"enabled": False}
    try:
        import selling_currency
        code = selling_currency.tenant_currency(tenant_id)
        prefix = selling_currency.currency_label(code)["prefix"]
    except Exception:
        code, prefix = "", ""
    return {"enabled": True, "prices": s["website_prices"], "currency": code, "prefix": prefix,
            "when": WHEN.get(s["target"], WHEN["1h"]), "target": s["target"], "business": name}


WHEN = {"30m": "within about 30 minutes in working hours", "1h": "within about an hour in working hours",
        "2h": "within about 2 hours in working hours", "4h": "within about 4 hours in working hours",
        "same_day": "by the end of the working day", "next_day": "by the end of the next working day"}


@quote_public_bp.route("/public/quote/config", methods=["GET", "POST", "OPTIONS"])
@public_route
def quote_config():
    if request.method == "OPTIONS":
        return _preflight()
    (row, err), _data, browser = _who()
    if err:
        return _err(err[0], err[1], browser)
    resp = _cors(jsonify(config_for(int(row["tenant_id"]))), browser)
    if browser:
        resp.headers["Cache-Control"] = "public, max-age=300"   # every page view of the website asks
    return resp


@quote_public_bp.route("/public/quote/products", methods=["GET", "OPTIONS"])
@public_route
def quote_products():
    """Product search for the paste-in quote list (WordPress uses its own shop)."""
    if request.method == "OPTIONS":
        return _preflight()
    row, err = _by_site(request.args.get("site"))
    if err:
        return _err(err[0], err[1], True)
    cfg = config_for(int(row["tenant_id"]))
    if not cfg["enabled"]:
        return _err(403, "Quote requests are not switched on", True)
    q = re.sub(r"\s+", " ", request.args.get("q") or "").strip()[:80]
    conn, cur = _db()
    try:
        if q:
            cur.execute("""SELECT id, name, price, price_on_request FROM products
                            WHERE tenant_id=%s AND is_active AND name ILIKE %s
                            ORDER BY (name ILIKE %s) DESC, lower(name) LIMIT 20""",
                        (row["tenant_id"], "%" + q.replace("%", "").replace("_", "\\_") + "%", q.replace("%", "") + "%"))
        else:
            cur.execute("""SELECT id, name, price, price_on_request FROM products WHERE tenant_id=%s AND is_active
                            ORDER BY updated_at DESC NULLS LAST LIMIT 20""", (row["tenant_id"],))
        items = [{"id": p["id"], "name": p["name"],
                  # "An estimate only" still needs the prices to add up; the box doesn't show them per line.
                  "price": (float(p["price"]) if cfg["prices"] in ("list_estimate", "estimate") and p["price"] is not None
                            and not p["price_on_request"] else None)} for p in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    return _cors(jsonify({"ok": True, "items": items}), True)


# ── the buyer sends their list ──────────────────────────────────────────────

def _clean(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n] or None


def _rate_prune():
    """Forget visitors not seen for an hour (the table would otherwise only grow)."""
    if len(_RATE) > 5000:
        now = time.time()
        for k in [k for k, q in list(_RATE.items()) if not q or now - q[-1] > _RATE_WINDOW]:
            _RATE.pop(k, None)


def _rate_q(tenant_id):
    _rate_prune()
    # The WordPress plugin calls from the shop's server and passes the buyer's
    # own address (only trusted with the secret API key).
    data = request.get_json(silent=True) or {}
    buyer = request.headers.get("X-Buyer-IP") if data.get("api_key") else None
    ip = (buyer or request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    q, now = _RATE[(ip, tenant_id)], time.time()
    while q and now - q[0] > _RATE_WINDOW:
        q.popleft()
    return q


def _rate_ok(tenant_id) -> bool:
    return len(_rate_q(tenant_id)) < _RATE_MAX


def _match_lines(tenant_id: int, raw: list) -> list:
    """The buyer's list → request lines, matched to the business's product
    list by our product id, then exact name, then closest name."""
    products = qr.products_for(tenant_id)
    out = []
    for ln in (raw or [])[:MAX_LINES]:
        if not isinstance(ln, dict):
            continue
        name = _clean(ln.get("name"), 300)
        qty = qr._num(ln.get("qty"), 0.0001, 1e9)
        if not name or qty is None:
            continue
        note = _clean(ln.get("note"), 300) or ""
        p = products.get(str(ln.get("product_id") or "")[:36])
        if not p and " \u2013 " in name:
            # WooCommerce options ("Kraft box – Large, Brown"): match the product,
            # and the options always go in the note so the salesperson sees them.
            base, opts = name.split(" \u2013 ", 1)
            p = qr.match_product(base, products)
            if p:
                note = "; ".join(x for x in (opts, note) if x)[:300]
        if not p:
            p = qr.match_product(name, products)
        out.append({"product_id": p["id"] if p else None, "name": p["name"] if p else name, "qty": qty,
                    "unit": _clean(ln.get("unit"), 40) or "",
                    "price": (float(p["price"]) if p and p.get("price") is not None and not p.get("price_on_request") else None),
                    "note": note})
    return out


@quote_public_bp.route("/public/quote/submit", methods=["POST", "OPTIONS"])
@public_route
def quote_submit():
    if request.method == "OPTIONS":
        return _preflight()
    if (request.content_length or 0) > 200_000:
        return _err(413, "That list is too long.", True)
    (row, err), data, browser = _who()
    if err:
        return _err(err[0], err[1], browser)
    tenant_id = int(row["tenant_id"])
    cfg = config_for(tenant_id)
    if not cfg["enabled"]:
        return _err(403, "Quote requests are not switched on for this website", browser)
    if _clean(data.get("hp"), 50):
        # The hidden "website" box only robots fill in: say thanks, keep nothing.
        return _cors(jsonify({"ok": True, "ref": None, "message": _thanks(data.get("lang"), None, cfg)}), browser)
    if not _rate_ok(tenant_id):
        return _err(429, "Too many quote requests. Please try again later.", browser)
    lines = _match_lines(tenant_id, data.get("lines") if isinstance(data.get("lines"), list) else [])
    email = (_clean(data.get("email"), 255) or "").lower() or None
    phone = _clean(data.get("phone"), 40)
    d = {"buyer_company": _clean(data.get("company"), 300), "buyer_name": _clean(data.get("name"), 200),
         "buyer_email": email, "buyer_phone": phone, "deliver_to": _clean(data.get("deliver_to"), 300),
         "notes": (str(data.get("notes") or "").strip()[:2000] or None), "lines": lines}
    if not lines:
        return _err(400, "Add at least one product with a quantity.", browser)
    if not (d["buyer_company"] or d["buyer_name"]):
        return _err(400, "Please add your name or company.", browser)
    if email and not _EMAIL.match(email):
        return _err(400, "That email address doesn't look right.", browser)
    if not (email or (phone and len(re.sub(r"\D", "", phone)) >= 7)):
        return _err(400, "Please add your email (or WhatsApp number) so we can send your quotation.", browser)
    nb = _clean(data.get("needed_by"), 10)
    d["needed_by"] = None
    if nb:
        try:
            d["needed_by"] = datetime.strptime(nb, "%Y-%m-%d").date()
        except ValueError:
            pass
    page = _clean(data.get("page"), 300)
    country = (_clean(data.get("country"), 2) or "").upper()
    d["buyer_country"] = country if re.fullmatch(r"[A-Z]{2}", country) else None
    words = "Sent from the quote list on " + (_host(page) or row.get("domain") or "the website")
    d["buyer_words"] = words + ("\n" + d["notes"] if d["notes"] else "")
    try:
        res = _save(tenant_id, d)
        _rate_q(tenant_id).append(time.time())
    except Exception as e:
        print(f"⚠️ quote_public submit tenant={tenant_id}:", e)
        return _err(500, "Sorry, something went wrong. Please try again.", browser)
    if email and res.get("action") != "same":
        _confirm(tenant_id, res["request"], cfg, data.get("lang"))
    return _cors(jsonify({"ok": True, "ref": f"R-{res['request']['ref_no']}",
                          "message": _thanks(data.get("lang"), f"R-{res['request']['ref_no']}", cfg, bool(email))}),
                 browser)


def _save(tenant_id: int, d: dict) -> dict:
    """New request, or the buyer's waiting one with the new lines added."""
    import lead_qualify as _lq
    from team_performance import tz_for
    conn, cur = _db()
    try:
        phones = _lq._norm_phones([d["buyer_phone"]] if d.get("buyer_phone") else [], None)
        m = _lq.find_buyer(cur, tenant_id, [d["buyer_email"]] if d.get("buyer_email") else [], phones or
                           ([d["buyer_phone"]] if d.get("buyer_phone") else []), d.get("buyer_company"))
        lead = m.get("lead") if m.get("lead") and m["lead"].get("dropped_at") is None and not m["lead"].get("outcome") else None
        if lead:
            cur.execute(f"""SELECT {qr._LIST_COLS} FROM quote_requests WHERE tenant_id=%s AND lead_id=%s
                              AND status IN ('to_quote','quote_sent') ORDER BY id DESC LIMIT 1""", (tenant_id, lead["id"]))
            act = cur.fetchone()
            if act and act["status"] == "to_quote":
                key = lambda ln: (ln.get("product_id") or ln["name"].lower(), ln.get("qty"))
                have = {key(ln) for ln in act["lines"]}
                add = [ln for ln in d["lines"] if key(ln) not in have]
                if not add:     # the same list sent twice (double click, back button)
                    conn.commit()
                    return {"request": act, "action": "same"}
                byname = {(ln.get("product_id") or ln["name"].lower()): i for i, ln in enumerate(act["lines"])}
                merged = list(act["lines"])
                for ln in add:      # a product already asked for: the new quantity wins
                    i = byname.get(ln.get("product_id") or ln["name"].lower())
                    if i is None:
                        merged.append(ln)
                    else:
                        merged[i] = ln
                merged = merged[:qr.MAX_LINES]
                note = ("Website note: " + d["notes"]) if d.get("notes") else None
                cur.execute("""UPDATE quote_requests SET lines=%s, estimate=%s, updated_at=NOW(),
                                      deliver_to=COALESCE(deliver_to, %s), needed_by=COALESCE(needed_by, %s),
                                      notes=CASE WHEN %s::text IS NULL THEN notes WHEN COALESCE(notes,'')='' THEN %s
                                                 ELSE notes || E'\n' || %s END
                                WHERE id=%s""", (psycopg2.extras.Json(merged), qr.estimate(merged),
                                                 d.get("deliver_to"), d.get("needed_by"), note, note, note, act["id"]))
                qr._event(cur, act["id"], "The buyer sent another quote list from the website: "
                          + ", ".join(ln["name"] for ln in add)[:500], qr.AI_ACTOR["label"])
                conn.commit()
                return {"request": qr.get(tenant_id, act["id"]), "action": "added"}
            parallel = bool(act)        # a quote is already with them: this is a new order
        else:
            parallel = False
        # Who gets it: the lead's owner, else the AI Lead Qualification rule.
        owner = (lead["assigned_key"], lead["assigned_to"]) if lead and lead.get("assigned_key") else None
        if not owner:
            try:
                owner = _lq._pick_owner(cur, tenant_id, _lq.settings(tenant_id))
            except Exception:
                owner = None
        conn.commit()
    finally:
        cur.close(); conn.close()
    try:
        import selling_currency
        code = selling_currency.tenant_currency(tenant_id)
    except Exception:
        code = None
    d.update({"lead_id": lead["id"] if lead else None, "channel": "website", "currency": code,
              "assigned_key": owner[0] if owner else None, "assigned_to": owner[1] if owner else None,
              "source_key": "web:" + (d.get("buyer_email") or re.sub(r"\D", "", d.get("buyer_phone") or "")),
              "parallel": parallel})
    r = qr.create(tenant_id, d, qr.AI_ACTOR, qr.settings(tenant_id), tz_for(tenant_id))
    return {"request": r, "action": "same" if r.get("_existing") else "created"}


# ── telling the buyer ───────────────────────────────────────────────────────

_T = {
    "en": {"thanks": "Thank you! Your quote request {ref} has been sent to {biz}.",
           "when": "You'll get your quotation by email {when}.", "when_phone": "We'll be in touch {when}.",
           "subject": "Your quote request {ref} — {biz}", "hi": "Hello {name},",
           "got": "We've received your quote request {ref}:", "reply": "To add anything, just reply to this email."},
    "fr": {"thanks": "Merci ! Votre demande de devis {ref} a été envoyée à {biz}.",
           "when": "Vous recevrez votre devis par e-mail {when}.", "when_phone": "Nous vous contacterons {when}.",
           "subject": "Votre demande de devis {ref} — {biz}", "hi": "Bonjour {name},",
           "got": "Nous avons bien reçu votre demande de devis {ref} :", "reply": "Pour ajouter quelque chose, répondez simplement à cet e-mail."},
    "es": {"thanks": "¡Gracias! Su solicitud de presupuesto {ref} se ha enviado a {biz}.",
           "when": "Recibirá su presupuesto por correo electrónico {when}.", "when_phone": "Nos pondremos en contacto {when}.",
           "subject": "Su solicitud de presupuesto {ref} — {biz}", "hi": "Hola {name}:",
           "got": "Hemos recibido su solicitud de presupuesto {ref}:", "reply": "Para añadir algo, responda a este correo."},
    "pt": {"thanks": "Obrigado! O seu pedido de orçamento {ref} foi enviado para {biz}.",
           "when": "Receberá o seu orçamento por e-mail {when}.", "when_phone": "Entraremos em contacto {when}.",
           "subject": "O seu pedido de orçamento {ref} — {biz}", "hi": "Olá {name},",
           "got": "Recebemos o seu pedido de orçamento {ref}:", "reply": "Para acrescentar algo, basta responder a este e-mail."},
    "de": {"thanks": "Vielen Dank! Ihre Angebotsanfrage {ref} wurde an {biz} gesendet.",
           "when": "Sie erhalten Ihr Angebot per E-Mail {when}.", "when_phone": "Wir melden uns {when}.",
           "subject": "Ihre Angebotsanfrage {ref} — {biz}", "hi": "Guten Tag {name},",
           "got": "Wir haben Ihre Angebotsanfrage {ref} erhalten:", "reply": "Um etwas zu ergänzen, antworten Sie einfach auf diese E-Mail."},
    "ar": {"thanks": "شكرًا لك! تم إرسال طلب عرض السعر {ref} إلى {biz}.",
           "when": "ستصلك عرض السعر عبر البريد الإلكتروني {when}.", "when_phone": "سنتواصل معك {when}.",
           "subject": "طلب عرض السعر {ref} — {biz}", "hi": "مرحبًا {name}،",
           "got": "لقد استلمنا طلب عرض السعر {ref}:", "reply": "لإضافة أي شيء، ما عليك سوى الرد على هذه الرسالة."},
}
# The promised time in each language.
_WHEN_T = {
    "fr": {"30m": "dans environ 30 minutes (heures ouvrables)", "1h": "dans environ une heure (heures ouvrables)",
           "2h": "dans environ 2 heures (heures ouvrables)", "4h": "dans environ 4 heures (heures ouvrables)",
           "same_day": "avant la fin de la journée de travail", "next_day": "avant la fin du prochain jour ouvrable"},
    "es": {"30m": "en unos 30 minutos (horario laboral)", "1h": "en aproximadamente una hora (horario laboral)",
           "2h": "en unas 2 horas (horario laboral)", "4h": "en unas 4 horas (horario laboral)",
           "same_day": "antes del final de la jornada laboral", "next_day": "antes del final del siguiente día laborable"},
    "pt": {"30m": "em cerca de 30 minutos (horário de trabalho)", "1h": "em cerca de uma hora (horário de trabalho)",
           "2h": "em cerca de 2 horas (horário de trabalho)", "4h": "em cerca de 4 horas (horário de trabalho)",
           "same_day": "até ao fim do dia útil", "next_day": "até ao fim do próximo dia útil"},
    "de": {"30m": "in etwa 30 Minuten (Geschäftszeiten)", "1h": "in etwa einer Stunde (Geschäftszeiten)",
           "2h": "in etwa 2 Stunden (Geschäftszeiten)", "4h": "in etwa 4 Stunden (Geschäftszeiten)",
           "same_day": "bis zum Ende des Arbeitstages", "next_day": "bis zum Ende des nächsten Arbeitstages"},
    "ar": {"30m": "خلال 30 دقيقة تقريبًا (في ساعات العمل)", "1h": "خلال ساعة تقريبًا (في ساعات العمل)",
           "2h": "خلال ساعتين تقريبًا (في ساعات العمل)", "4h": "خلال 4 ساعات تقريبًا (في ساعات العمل)",
           "same_day": "قبل نهاية يوم العمل", "next_day": "قبل نهاية يوم العمل التالي"},
}


def _lang(v) -> str:
    v = str(v or "en").lower()[:2]
    return v if v in _T else "en"


def _when(lang: str, cfg: dict) -> str:
    target = cfg.get("target") if cfg.get("target") in WHEN else "1h"
    return _WHEN_T.get(lang, {}).get(target) or WHEN[target]


def _thanks(lang, ref, cfg, by_email=True) -> str:
    L = _lang(lang)
    t = _T[L]
    first = t["thanks"].format(ref=ref or "", biz=cfg.get("business") or "us").replace("  ", " ")
    return first + " " + (t["when"] if by_email else t["when_phone"]).format(when=_when(L, cfg))


def _confirm(tenant_id: int, r: dict, cfg: dict, lang):
    """A short email to the buyer: received, and when the quotation comes.
    Replies go to the salesperson (or the business)."""
    from portal_utils import send_email
    L = _lang(lang)
    t = _T[L]
    ref = f"R-{r['ref_no']}"
    biz = cfg.get("business") or ""
    reply_to = None
    conn, cur = _db()
    try:
        key = r.get("assigned_key") or ""
        if key.startswith("team:") and key[5:].isdigit():
            cur.execute("SELECT email FROM team_members WHERE id=%s AND tenant_id=%s", (int(key[5:]), tenant_id))
            reply_to = (cur.fetchone() or {}).get("email")
        if not reply_to:
            cur.execute("SELECT email FROM customers WHERE tenant_id=%s AND email IS NOT NULL ORDER BY id LIMIT 1", (tenant_id,))
            reply_to = (cur.fetchone() or {}).get("email")
    finally:
        cur.close(); conn.close()
    items = "".join(f"<li>{_html.escape(('%g ' % ln['qty']) if ln.get('qty') is not None else '')}"
                    f"{_html.escape(ln.get('unit') + ' ' if ln.get('unit') else '')}{_html.escape(ln['name'])}</li>"
                    for ln in r["lines"])
    rtl = ' dir="rtl"' if L == "ar" else ""
    body = (f'<div{rtl} style="font-family:Arial,sans-serif;max-width:560px;color:#1B2433">'
            f"<p>{_html.escape(t['hi'].format(name=r.get('buyer_name') or r.get('buyer_company') or ''))}</p>"
            f"<p>{_html.escape(t['got'].format(ref=ref))}</p><ul>{items}</ul>"
            f"<p>{_html.escape(t['when'].format(when=_when(L, cfg)))}</p>"
            f"<p>{_html.escape(t['reply'])}</p><p>{_html.escape(biz)}</p></div>")
    text = "\n".join([t["hi"].format(name=r.get("buyer_name") or ""), t["got"].format(ref=ref)]
                     + [f"- {('%g ' % ln['qty']) if ln.get('qty') is not None else ''}{ln['name']}" for ln in r["lines"]]
                     + [t["when"].format(when=_when(L, cfg)), t["reply"], biz])
    try:
        ok = send_email(r["buyer_email"], t["subject"].format(ref=ref, biz=biz), body, text_body=text, reply_to=reply_to)
        conn, cur = _db()
        try:
            qr._event(cur, r["id"], ("Confirmation emailed to the buyer" if ok else "The confirmation email to the buyer "
                                     "could not be sent") + f" ({r['buyer_email']})", "PhiXtra")
            conn.commit()
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print(f"⚠️ quote_public confirm {r.get('id')}:", e)
