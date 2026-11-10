"""Online quotations (Quote engine Step 5, 2026-11-10; plan approved "yes to all").

Every quotation sent from PhiXtra gets a private link the buyer can open
without logging in: portal.phixtra.com/q/<token>. The page shows the
quotation (PDF) and lets the buyer Accept, Ask a question or Decline.

  * The token is ~40 random characters, stored ENCRYPTED (data_crypto) so
    follow-ups can reuse it ({{Quote Link}}); lookups use its SHA-256.
  * A link only works for the version it was made for, while the quote is
    out and not expired, and while the business keeps online links on.
    Otherwise the page says why, with no prices.
  * Opens are counted only when a person views the page (a script runs
    after a moment), never for scanners or link previews, and never for the
    business's own logged-in staff. Accept / Question / Decline need a
    button press (POST).
  * Every response: no-store (Cloudflare and browsers keep nothing),
    noindex, no referrer.

Works for every business with Quotations, whatever their website platform.
Shopify: Accept marks the quote Won only; the salesperson still creates the
Shopify draft order, as today.
"""
import hashlib
import os
import re
import secrets
import threading
import time
from datetime import datetime

import psycopg2
import psycopg2.extras
from flask import Blueprint, Response, abort, jsonify, render_template, request, session

import data_crypto
import quote_requests as qr

quote_online_bp = Blueprint("quote_online", __name__)

PORTAL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")
ONLINE_ACTOR = {"key": "buyer_online", "label": "Buyer (online)"}
_tables_ok = False


def _db():
    return qr._db()


def ensure_tables():
    global _tables_ok
    if _tables_ok:
        return
    conn, cur = _db()
    try:
        cur.execute("SELECT to_regclass('quote_links') IS NOT NULL AND to_regclass('quote_link_actions') IS NOT NULL AS ok")
        if cur.fetchone()["ok"]:
            _tables_ok = True
            return
        cur.execute("SET lock_timeout = '5s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS quote_links (
                           id              BIGSERIAL PRIMARY KEY,
                           tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                           request_id      BIGINT NOT NULL REFERENCES quote_requests(id) ON DELETE CASCADE,
                           quotation_id    BIGINT,            -- PhiXtra quotation version; NULL = own software
                           sent_at         TIMESTAMPTZ,       -- own software: the Quote sent it belongs to
                           token_hash      CHAR(64) NOT NULL UNIQUE,
                           token_enc       TEXT NOT NULL,
                           lang            VARCHAR(2) NOT NULL DEFAULT 'en',
                           open_count      INTEGER NOT NULL DEFAULT 0,
                           first_opened_at TIMESTAMPTZ,
                           last_opened_at  TIMESTAMPTZ,
                           created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_quote_links_req ON quote_links (request_id, id DESC)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_quote_links_qid ON quote_links (quotation_id) WHERE quotation_id IS NOT NULL")
        cur.execute("""CREATE TABLE IF NOT EXISTS quote_link_actions (
                           id         BIGSERIAL PRIMARY KEY,
                           link_id    BIGINT NOT NULL REFERENCES quote_links(id) ON DELETE CASCADE,
                           action     VARCHAR(10) NOT NULL,     -- accept | decline | question
                           name       VARCHAR(120),
                           po         VARCHAR(60),
                           text       TEXT,
                           ip_hash    CHAR(16),
                           created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_quote_link_actions ON quote_link_actions (link_id, created_at)")
        conn.commit()
        _tables_ok = True
    except Exception as e:
        conn.rollback()
        print("⚠️ quote_links tables:", e)
    finally:
        cur.close(); conn.close()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def url_for_token(token: str) -> str:
    return f"{PORTAL}/q/{token}"


def _new_link(cur, tenant_id, request_id, quotation_id=None, sent_at=None, lang="en") -> str:
    token = secrets.token_urlsafe(30)          # 40 characters
    cur.execute("""INSERT INTO quote_links (tenant_id, request_id, quotation_id, sent_at, token_hash, token_enc, lang)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, request_id, quotation_id, sent_at, _hash(token), data_crypto.enc(token),
                 lang if lang in LANGS else "en"))
    return token


def link_for_quotation(tenant_id: int, q: dict, lang: str = "en"):
    """The link for one PhiXtra quotation version (made once, reused), or
    None when the business switched online links off."""
    if not qr.settings(tenant_id).get("online_link"):
        return None
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT token_enc FROM quote_links WHERE quotation_id=%s AND tenant_id=%s", (q["id"], tenant_id))
        r = cur.fetchone()
        if r:
            return url_for_token(data_crypto.dec(r["token_enc"]))
        token = _new_link(cur, tenant_id, q["request_id"], quotation_id=q["id"], lang=lang)
        conn.commit()
        return url_for_token(token)
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.execute("SELECT token_enc FROM quote_links WHERE quotation_id=%s AND tenant_id=%s", (q["id"], tenant_id))
        r = cur.fetchone()
        return url_for_token(data_crypto.dec(r["token_enc"])) if r else None
    finally:
        cur.close(); conn.close()


def set_lang(tenant_id: int, quotation_id: int, lang: str):
    """The buyer page speaks the language the cover message was sent in."""
    if lang not in LANGS:
        return
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("UPDATE quote_links SET lang=%s WHERE quotation_id=%s AND tenant_id=%s", (lang, quotation_id, tenant_id))
        conn.commit()
    finally:
        cur.close(); conn.close()


def link_for_own_send(tenant_id: int, request_id: int, sent_at, lang: str = "en"):
    """Own quoting software: a link for the PDF uploaded at Quote sent
    (decision 4: only when there is a PDF). Returns the URL or None."""
    if not qr.settings(tenant_id).get("online_link"):
        return None
    ensure_tables()
    conn, cur = _db()
    try:
        token = _new_link(cur, tenant_id, request_id, sent_at=sent_at, lang=lang)
        conn.commit()
        return url_for_token(token)
    finally:
        cur.close(); conn.close()


def latest_for_request(tenant_id: int, request_id: int):
    """{url, open_count, first_opened_at, last_opened_at, live} for the
    request's newest link, or None."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT * FROM quote_links WHERE tenant_id=%s AND request_id=%s ORDER BY id DESC LIMIT 5""",
                    (tenant_id, request_id))
        rows = cur.fetchall()
        for l in rows:
            st = _state(cur, l)
            if st["kind"] == "draft":
                continue
            return {"url": url_for_token(data_crypto.dec(l["token_enc"])), "open_count": l["open_count"],
                    "first_opened_at": l["first_opened_at"], "last_opened_at": l["last_opened_at"],
                    "live": st["kind"] == "live"}
        return None
    finally:
        cur.close(); conn.close()


def opens_for(tenant_id: int, request_ids: list) -> dict:
    """{request_id: {open_count, last_opened_at}} summed over the request's links."""
    if not request_ids:
        return {}
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT request_id, SUM(open_count) AS n, max(last_opened_at) AS last FROM quote_links
                        WHERE tenant_id=%s AND request_id = ANY(%s) GROUP BY request_id""",
                    (tenant_id, list(request_ids)))
        return {r["request_id"]: {"open_count": int(r["n"] or 0), "last_opened_at": r["last"]} for r in cur.fetchall()}
    finally:
        cur.close(); conn.close()


def live_url_for_lead(cur, tenant_id: int, lead_id) -> str:
    """{{Quote Link}} in follow-ups: the live link of the deal's quote out now ('' if none).
    Uses its own connection, so it works whatever cursor the caller has."""
    if not lead_id:
        return ""
    ensure_tables()
    conn, c2 = _db()
    try:
        c2.execute("""SELECT l.* FROM quote_links l JOIN quote_requests q ON q.id=l.request_id
                       WHERE l.tenant_id=%s AND q.lead_id=%s AND q.status='quote_sent'
                       ORDER BY l.id DESC LIMIT 5""", (tenant_id, lead_id))
        for l in c2.fetchall():
            if _state(c2, l)["kind"] == "live":
                return url_for_token(data_crypto.dec(l["token_enc"]))
        return ""
    except Exception as e:
        print("⚠️ quote link for lead:", e)
        return ""
    finally:
        c2.close(); conn.close()


# ── what a link shows ───────────────────────────────────────────────────────

def _state(cur, link) -> dict:
    """kind: live | expired | replaced | updating | won | lost | off | draft | gone."""
    tid = link["tenant_id"]
    if not qr.allowed(tid) or not qr.settings(tid).get("online_link"):
        return {"kind": "off"}
    cur.execute(f"SELECT {qr._LIST_COLS} FROM quote_requests WHERE id=%s AND tenant_id=%s",
                (link["request_id"], tid))
    req = cur.fetchone()
    if not req:
        return {"kind": "gone"}
    q = None
    if link["quotation_id"]:
        cur.execute("""SELECT id, status, number, version, total, currency, valid_until, sent_at
                         FROM quotations WHERE id=%s AND tenant_id=%s""", (link["quotation_id"], tid))
        q = cur.fetchone()
        if not q:
            return {"kind": "gone"}
        if q["status"] in ("draft", "sending"):
            return {"kind": "draft"}
        if q["status"] != "sent":
            return {"kind": "replaced", "req": req}
    elif not req.get("quote_sent_at") or link.get("sent_at") is None or \
            abs((req["quote_sent_at"] - link["sent_at"]).total_seconds()) > 1:
        return {"kind": "replaced", "req": req}
    if req["status"] == "won":
        return {"kind": "won", "req": req}
    if req["status"] == "lost":
        return {"kind": "lost", "req": req}
    if req["status"] == "to_quote":
        return {"kind": "updating", "req": req}
    from team_performance import tz_for
    today = datetime.now(tz_for(tid)).date()
    if req.get("quote_valid_until") and req["quote_valid_until"] < today:
        return {"kind": "expired", "req": req}
    return {"kind": "live", "req": req, "q": q}


def _find(token: str):
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{30,60}", token):
        return None
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM quote_links WHERE token_hash=%s", (_hash(token),))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


# ── guessing protection (per worker; enough to make guessing pointless) ────

_misses, _mlock = {}, threading.Lock()


def _ip() -> str:
    return (request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0]
            or request.remote_addr or "").strip()


def _ip_hash() -> str:
    return hashlib.sha256(("quote-link|" + _ip()).encode()).hexdigest()[:16]


def _blocked() -> bool:
    now = time.time()
    with _mlock:
        hits = [t for t in _misses.get(_ip(), []) if now - t < 600]
        _misses[_ip()] = hits
        return len(hits) >= 20


def _miss():
    now = time.time()
    with _mlock:
        if len(_misses) > 5000:        # never grows without end
            for k in [k for k, v in _misses.items() if not v or now - v[-1] > 600]:
                _misses.pop(k, None)
            if len(_misses) > 5000:
                _misses.clear()
        _misses.setdefault(_ip(), []).append(now)


def _private(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def _staff_of(tenant_id) -> bool:
    """The business's own staff looking at the link (logged in to the portal)."""
    try:
        if not session.get("portal_logged_in") or not session.get("customer_id"):
            return False
        conn, cur = _db()
        try:
            cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (session["customer_id"],))
            r = cur.fetchone()
            return bool(r) and int(r["tenant_id"]) == int(tenant_id)
        finally:
            cur.close(); conn.close()
    except Exception:
        return False


_BOT_UA = re.compile(r"(bot|crawl|spider|preview|scan|facebookexternalhit|whatsapp|slack|telegram|skype|"
                     r"outlook|office|microsoft|proofpoint|mimecast|barracuda|headless|python|curl|wget)", re.I)


# ── texts (the buyer's language: en fr es pt de ar) ─────────────────────────

LANGS = ("en", "fr", "es", "pt", "de", "ar")
T = {
    "en": {"quotation": "Quotation", "for": "for", "total": "Total", "valid": "Valid until", "view": "View the quotation (PDF)",
           "accept": "Accept quotation", "question": "Ask a question", "decline": "Decline",
           "name": "Your name", "po": "Your order number (optional)", "behalf": "I accept this quotation on behalf of {company}",
           "send_accept": "Accept", "your_q": "Your question", "send_q": "Send question", "reason": "Reason (optional)",
           "send_decline": "Decline the quotation", "cancel": "Cancel",
           "thanks_accept": "Thank you, the quotation is accepted. {biz} will be in touch about your order.",
           "thanks_q": "Thank you, your question has been sent to {biz}. They'll reply soon.",
           "thanks_decline": "Thank you for letting us know. {biz} has been told.",
           "expired": "This quotation expired on {date}. Please contact {biz} for an updated quotation.",
           "replaced": "This quotation has been replaced by a newer one. Please use the latest quotation from {biz}.",
           "updating": "{biz} is updating this quotation. A new one will follow.",
           "won": "This quotation has been accepted. Thank you.", "lost": "This quotation is closed.",
           "off": "This link no longer works. Please contact the business that sent it.",
           "gone": "This link doesn't work. Please check it, or contact the business that sent it.",
           "need_name": "Please type your name.", "need_tick": "Please tick the box to confirm.",
           "need_text": "Please type your question.", "busy": "Too many messages. Please try again later.",
           "private": "This page is private to you. Please don't share the link.", "by": "Sent by"},
    "fr": {"quotation": "Devis", "for": "pour", "total": "Total", "valid": "Valable jusqu'au", "view": "Voir le devis (PDF)",
           "accept": "Accepter le devis", "question": "Poser une question", "decline": "Refuser",
           "name": "Votre nom", "po": "Votre numéro de commande (facultatif)", "behalf": "J'accepte ce devis au nom de {company}",
           "send_accept": "Accepter", "your_q": "Votre question", "send_q": "Envoyer la question", "reason": "Motif (facultatif)",
           "send_decline": "Refuser le devis", "cancel": "Annuler",
           "thanks_accept": "Merci, le devis est accepté. {biz} vous contactera au sujet de votre commande.",
           "thanks_q": "Merci, votre question a été envoyée à {biz}. Vous recevrez une réponse rapidement.",
           "thanks_decline": "Merci de nous avoir informés. {biz} a été prévenu.",
           "expired": "Ce devis a expiré le {date}. Veuillez contacter {biz} pour un devis à jour.",
           "replaced": "Ce devis a été remplacé par un plus récent. Veuillez utiliser le dernier devis de {biz}.",
           "updating": "{biz} met ce devis à jour. Un nouveau devis suivra.",
           "won": "Ce devis a été accepté. Merci.", "lost": "Ce devis est clôturé.",
           "off": "Ce lien ne fonctionne plus. Veuillez contacter l'entreprise qui l'a envoyé.",
           "gone": "Ce lien ne fonctionne pas. Vérifiez-le ou contactez l'entreprise qui l'a envoyé.",
           "need_name": "Veuillez saisir votre nom.", "need_tick": "Veuillez cocher la case pour confirmer.",
           "need_text": "Veuillez saisir votre question.", "busy": "Trop de messages. Veuillez réessayer plus tard.",
           "private": "Cette page vous est réservée. Merci de ne pas partager le lien.", "by": "Envoyé par"},
    "es": {"quotation": "Presupuesto", "for": "para", "total": "Total", "valid": "Válido hasta", "view": "Ver el presupuesto (PDF)",
           "accept": "Aceptar el presupuesto", "question": "Hacer una pregunta", "decline": "Rechazar",
           "name": "Su nombre", "po": "Su número de pedido (opcional)", "behalf": "Acepto este presupuesto en nombre de {company}",
           "send_accept": "Aceptar", "your_q": "Su pregunta", "send_q": "Enviar la pregunta", "reason": "Motivo (opcional)",
           "send_decline": "Rechazar el presupuesto", "cancel": "Cancelar",
           "thanks_accept": "Gracias, el presupuesto está aceptado. {biz} se pondrá en contacto sobre su pedido.",
           "thanks_q": "Gracias, su pregunta se ha enviado a {biz}. Le responderán pronto.",
           "thanks_decline": "Gracias por avisarnos. Se ha informado a {biz}.",
           "expired": "Este presupuesto caducó el {date}. Póngase en contacto con {biz} para uno actualizado.",
           "replaced": "Este presupuesto ha sido sustituido por uno más reciente. Utilice el último presupuesto de {biz}.",
           "updating": "{biz} está actualizando este presupuesto. Recibirá uno nuevo.",
           "won": "Este presupuesto ha sido aceptado. Gracias.", "lost": "Este presupuesto está cerrado.",
           "off": "Este enlace ya no funciona. Póngase en contacto con la empresa que lo envió.",
           "gone": "Este enlace no funciona. Compruébelo o póngase en contacto con la empresa que lo envió.",
           "need_name": "Escriba su nombre.", "need_tick": "Marque la casilla para confirmar.",
           "need_text": "Escriba su pregunta.", "busy": "Demasiados mensajes. Inténtelo más tarde.",
           "private": "Esta página es solo para usted. No comparta el enlace.", "by": "Enviado por"},
    "pt": {"quotation": "Orçamento", "for": "para", "total": "Total", "valid": "Válido até", "view": "Ver o orçamento (PDF)",
           "accept": "Aceitar o orçamento", "question": "Fazer uma pergunta", "decline": "Recusar",
           "name": "O seu nome", "po": "O número da sua encomenda (opcional)", "behalf": "Aceito este orçamento em nome de {company}",
           "send_accept": "Aceitar", "your_q": "A sua pergunta", "send_q": "Enviar a pergunta", "reason": "Motivo (opcional)",
           "send_decline": "Recusar o orçamento", "cancel": "Cancelar",
           "thanks_accept": "Obrigado, o orçamento foi aceite. A {biz} entrará em contacto sobre a sua encomenda.",
           "thanks_q": "Obrigado, a sua pergunta foi enviada à {biz}. Responderão em breve.",
           "thanks_decline": "Obrigado por nos informar. A {biz} foi avisada.",
           "expired": "Este orçamento expirou a {date}. Contacte a {biz} para um orçamento atualizado.",
           "replaced": "Este orçamento foi substituído por um mais recente. Utilize o último orçamento da {biz}.",
           "updating": "A {biz} está a atualizar este orçamento. Receberá um novo.",
           "won": "Este orçamento foi aceite. Obrigado.", "lost": "Este orçamento está fechado.",
           "off": "Este link já não funciona. Contacte a empresa que o enviou.",
           "gone": "Este link não funciona. Verifique-o ou contacte a empresa que o enviou.",
           "need_name": "Escreva o seu nome.", "need_tick": "Assinale a caixa para confirmar.",
           "need_text": "Escreva a sua pergunta.", "busy": "Demasiadas mensagens. Tente mais tarde.",
           "private": "Esta página é só para si. Não partilhe o link.", "by": "Enviado por"},
    "de": {"quotation": "Angebot", "for": "für", "total": "Gesamt", "valid": "Gültig bis", "view": "Angebot ansehen (PDF)",
           "accept": "Angebot annehmen", "question": "Frage stellen", "decline": "Ablehnen",
           "name": "Ihr Name", "po": "Ihre Bestellnummer (optional)", "behalf": "Ich nehme dieses Angebot im Namen von {company} an",
           "send_accept": "Annehmen", "your_q": "Ihre Frage", "send_q": "Frage senden", "reason": "Grund (optional)",
           "send_decline": "Angebot ablehnen", "cancel": "Abbrechen",
           "thanks_accept": "Vielen Dank, das Angebot ist angenommen. {biz} meldet sich zu Ihrer Bestellung.",
           "thanks_q": "Vielen Dank, Ihre Frage wurde an {biz} gesendet. Sie erhalten bald eine Antwort.",
           "thanks_decline": "Danke für Ihre Rückmeldung. {biz} wurde informiert.",
           "expired": "Dieses Angebot ist am {date} abgelaufen. Bitte wenden Sie sich für ein neues Angebot an {biz}.",
           "replaced": "Dieses Angebot wurde durch ein neueres ersetzt. Bitte verwenden Sie das neueste Angebot von {biz}.",
           "updating": "{biz} überarbeitet dieses Angebot. Ein neues folgt.",
           "won": "Dieses Angebot wurde angenommen. Vielen Dank.", "lost": "Dieses Angebot ist abgeschlossen.",
           "off": "Dieser Link funktioniert nicht mehr. Bitte wenden Sie sich an das Unternehmen, das ihn gesendet hat.",
           "gone": "Dieser Link funktioniert nicht. Bitte prüfen Sie ihn oder wenden Sie sich an den Absender.",
           "need_name": "Bitte geben Sie Ihren Namen ein.", "need_tick": "Bitte bestätigen Sie mit dem Häkchen.",
           "need_text": "Bitte geben Sie Ihre Frage ein.", "busy": "Zu viele Nachrichten. Bitte später erneut versuchen.",
           "private": "Diese Seite ist nur für Sie. Bitte teilen Sie den Link nicht.", "by": "Gesendet von"},
    "ar": {"quotation": "عرض السعر", "for": "لـ", "total": "الإجمالي", "valid": "ساري حتى", "view": "عرض السعر (PDF)",
           "accept": "قبول العرض", "question": "طرح سؤال", "decline": "رفض",
           "name": "اسمكم", "po": "رقم طلبكم (اختياري)", "behalf": "أوافق على هذا العرض نيابةً عن {company}",
           "send_accept": "قبول", "your_q": "سؤالكم", "send_q": "إرسال السؤال", "reason": "السبب (اختياري)",
           "send_decline": "رفض العرض", "cancel": "إلغاء",
           "thanks_accept": "شكرًا لكم، تم قبول العرض. سيتواصل معكم {biz} بخصوص طلبكم.",
           "thanks_q": "شكرًا لكم، تم إرسال سؤالكم إلى {biz}. سيتم الرد قريبًا.",
           "thanks_decline": "شكرًا لإبلاغنا. تم إخطار {biz}.",
           "expired": "انتهت صلاحية هذا العرض في {date}. يرجى التواصل مع {biz} للحصول على عرض محدّث.",
           "replaced": "تم استبدال هذا العرض بعرض أحدث. يرجى استخدام أحدث عرض من {biz}.",
           "updating": "يقوم {biz} بتحديث هذا العرض. سيصلكم عرض جديد.",
           "won": "تم قبول هذا العرض. شكرًا لكم.", "lost": "هذا العرض مغلق.",
           "off": "هذا الرابط لم يعد يعمل. يرجى التواصل مع الجهة التي أرسلته.",
           "gone": "هذا الرابط لا يعمل. يرجى التحقق منه أو التواصل مع الجهة التي أرسلته.",
           "need_name": "يرجى كتابة اسمكم.", "need_tick": "يرجى وضع علامة في المربع للتأكيد.",
           "need_text": "يرجى كتابة سؤالكم.", "busy": "رسائل كثيرة جدًا. يرجى المحاولة لاحقًا.",
           "private": "هذه الصفحة خاصة بكم. يرجى عدم مشاركة الرابط.", "by": "أرسله"},
}

# The cover message's "accept" paragraph when the link goes with it
# (decision: link on its own line so every mail app makes it clickable).
COVER_LINE = {
    "en": ("You can view and accept it online here:\n{link}\n\nOr simply reply with your order number. Any questions, I'm happy to help.",
           "You can view it online here:\n{link}\n\nTo accept, just reply with your order number. Any questions, I'm happy to help."),
    "fr": ("Vous pouvez consulter et accepter ce devis en ligne ici :\n{link}\n\nVous pouvez aussi simplement répondre avec votre numéro de commande. Je reste à votre disposition pour toute question.",
           "Vous pouvez consulter ce devis en ligne ici :\n{link}\n\nPour l'accepter, répondez simplement avec votre numéro de commande. Je reste à votre disposition pour toute question."),
    "es": ("Puede consultarlo y aceptarlo en línea aquí:\n{link}\n\nO simplemente responda con su número de pedido. Quedo a su disposición para cualquier pregunta.",
           "Puede consultarlo en línea aquí:\n{link}\n\nPara aceptarlo, responda con su número de pedido. Quedo a su disposición para cualquier pregunta."),
    "pt": ("Pode consultá-lo e aceitá-lo online aqui:\n{link}\n\nOu basta responder com o número da sua encomenda. Estou à disposição para qualquer dúvida.",
           "Pode consultá-lo online aqui:\n{link}\n\nPara aceitar, basta responder com o número da sua encomenda. Estou à disposição para qualquer dúvida."),
    "de": ("Sie können das Angebot hier online ansehen und annehmen:\n{link}\n\nOder antworten Sie einfach mit Ihrer Bestellnummer. Bei Fragen helfe ich Ihnen gern.",
           "Sie können das Angebot hier online ansehen:\n{link}\n\nZur Bestellung antworten Sie einfach mit Ihrer Bestellnummer. Bei Fragen helfe ich Ihnen gern."),
    "ar": ("يمكنكم الاطلاع على العرض وقبوله عبر الإنترنت من هنا:\n{link}\n\nأو يكفي الرد برقم طلبكم. يسعدني الإجابة عن أي سؤال.",
           "يمكنكم الاطلاع على العرض عبر الإنترنت من هنا:\n{link}\n\nللموافقة، يكفي الرد برقم طلبكم. يسعدني الإجابة عن أي سؤال."),
}


def cover_paragraph(tenant_id: int, lang: str, link: str) -> str:
    lang = lang if lang in COVER_LINE else "en"
    accept_on = qr.settings(tenant_id).get("online_accept")
    return COVER_LINE[lang][0 if accept_on else 1].format(link=link)


# ── the buyer's page ────────────────────────────────────────────────────────

def _biz(tenant_id: int) -> dict:
    try:
        import quote_design as qd
        p = qd.profile(tenant_id)
        d = qd.get(tenant_id)
        return {"name": p.get("name") or "", "logo": bool(qd.logo_bytes(tenant_id)),
                "colour": d.get("colour") or "#0B1D40"}
    except Exception:
        return {"name": "", "logo": False, "colour": "#0B1D40"}


STAFF_NOTE = ("You're logged in to PhiXtra for this business, so the buyer's buttons don't work for you. "
              "This is what your buyer sees.")


def _page(link, st, lang, msg=None, error=None, open_form=None):
    tid = link["tenant_id"] if link else None
    biz = _biz(tid) if tid else {"name": "", "logo": False, "colour": "#0B1D40"}
    t = {k: v for k, v in T.get(lang, T["en"]).items()}
    req = (st or {}).get("req") or {}
    words = None
    kind = (st or {}).get("kind", "gone")
    if kind not in ("live",):
        if kind == "expired":
            words = t["expired"].format(date=req["quote_valid_until"].strftime("%d %b %Y"), biz=biz["name"] or "the business")
        elif kind in t:
            words = t[kind].format(biz=biz["name"] or "the business")
        else:
            words = t["gone"]
    resp = Response(render_template(
        "public/quote_online.html", t=t, lang=lang, biz=biz, kind=kind, words=words, req=req, q=(st or {}).get("q"),
        token=request.view_args.get("token"), msg=msg, error=error, open_form=open_form,
        accept_on=bool(tid and qr.settings(tid).get("online_accept")),
        staff=bool(tid and _staff_of(tid)), staff_note=STAFF_NOTE,
        number=(req.get("quote_number") or (f"R-{req['ref_no']}" if req.get("ref_no") else "")),
        total=(qr.money(req.get("quote_value"), req.get("quote_currency")) if req.get("quote_value") is not None else ""),
        company=req.get("buyer_company") or req.get("buyer_name") or ""))
    return _private(resp)


def _load(token):
    if _blocked():
        return None, None, _private(Response("Too many attempts. Please try again later.", status=429))
    link = _find(token)
    if not link:
        _miss()
        return None, None, None
    conn, cur = _db()
    try:
        st = _state(cur, link)
    finally:
        cur.close(); conn.close()
    return link, st, None


@quote_online_bp.route("/q/<token>", methods=["GET"])
def page(token):
    link, st, err = _load(token)
    if err:
        return err
    if not link:
        r = _page(None, {"kind": "gone"}, "en")
        r.status_code = 404
        return r
    if st["kind"] == "draft":
        r = _page(None, {"kind": "gone"}, link["lang"])
        r.status_code = 404
        return r
    return _page(link, st, link["lang"])


@quote_online_bp.route("/q/<token>/pdf", methods=["GET"])
def pdf(token):
    link, st, err = _load(token)
    if err:
        return err
    if not link or st["kind"] != "live":
        abort(404)
    conn, cur = _db()
    try:
        if link["quotation_id"]:
            cur.execute("SELECT pdf FROM quotations WHERE id=%s AND tenant_id=%s", (link["quotation_id"], link["tenant_id"]))
        else:
            cur.execute("SELECT quote_pdf AS pdf FROM quote_requests WHERE id=%s AND tenant_id=%s",
                        (link["request_id"], link["tenant_id"]))
        r = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not r or not r["pdf"]:
        abort(404)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (st["req"].get("quote_number") or "quotation")) + ".pdf"
    resp = Response(bytes(r["pdf"]), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'inline; filename="{name}"'
    return _private(resp)


@quote_online_bp.route("/q/<token>/logo", methods=["GET"])
def logo(token):
    link, st, err = _load(token)
    if err:
        return err
    if not link or st["kind"] in ("draft", "gone"):
        abort(404)
    import quote_design as qd
    b = qd.logo_bytes(link["tenant_id"])
    if not b:
        abort(404)
    mime = "image/png" if b[:4] == b"\x89PNG" else ("image/svg+xml" if b.lstrip()[:1] == b"<" else "image/jpeg")
    if mime == "image/svg+xml":
        abort(404)   # never serve script-capable SVG from a public page
    return _private(Response(b, mimetype=mime))


@quote_online_bp.route("/q/<token>/seen", methods=["POST"])
def seen(token):
    """Sent by the page's script after the buyer has had it open for a
    moment. Scanners and link previews don't run it; staff don't count."""
    link, st, err = _load(token)
    if err or not link or st["kind"] in ("draft", "gone"):
        return _private(jsonify(ok=False)), 404
    if _BOT_UA.search(request.headers.get("User-Agent", "")) or _staff_of(link["tenant_id"]):
        return _private(jsonify(ok=True, counted=False))
    conn, cur = _db()
    try:
        cur.execute("""UPDATE quote_links SET open_count=open_count+1, last_opened_at=NOW(),
                              first_opened_at=COALESCE(first_opened_at, NOW())
                        WHERE id=%s RETURNING open_count""", (link["id"],))
        n = cur.fetchone()["open_count"]
        req = st.get("req") or {}
        if n == 1 and req:
            num = req.get("quote_number") or f"R-{req['ref_no']}"
            qr._event(cur, req["id"], f"The buyer opened quotation {num} online for the first time.", ONLINE_ACTOR["label"])
        conn.commit()
    finally:
        cur.close(); conn.close()
    if n == 1 and st.get("req") and qr.settings(link["tenant_id"]).get("online_open_alert"):
        _tell(link["tenant_id"], st["req"], "opened")
    return _private(jsonify(ok=True, counted=True))


def _tell(tenant_id, req, what, name="", po="", text=""):
    """Email the salesperson (or the owner when nobody is assigned)."""
    import auto_followups as _af
    buyer = req.get("buyer_company") or req.get("buyer_name") or "The buyer"
    num = req.get("quote_number") or f"R-{req['ref_no']}"
    val = qr.money(req.get("quote_value"), req.get("quote_currency"))
    who = f" ({name})" if name else ""
    subject, lines = {
        "opened": (f"{buyer} opened quotation {num}",
                   [f"{buyer} has just opened quotation {num} ({val}) online for the first time.",
                    "It's a good moment to give them a call."]),
        "accept": (f"Won: {buyer} accepted {num} online",
                   [f"{buyer}{who} accepted quotation {num} ({val}) online." + (f" Their order number: {po}." if po else ""),
                    "The deal is marked Won. If this is wrong, open the request and press Undo."]),
        "decline": (f"Lost: {buyer} declined {num} online",
                    [f"{buyer}{who} declined quotation {num} ({val}) online: {text or 'no reason given'}.",
                     "The deal is marked Lost and its follow-ups have stopped. If this is wrong, open the request and press Undo."]),
        "question": (f"{buyer} asked about {num}",
                     [f"{buyer}{who} asked about quotation {num} online:", f"“{text}”", "Please reply to them."]),
    }[what]
    key = hashlib.sha1(f"{what}|{name}|{po}|{text}".encode()).hexdigest()[:12]
    conn, cur = _db()
    try:
        _af.notify(cur, tenant_id, req.get("assigned_key") or "owner", f"qonline:{req['id']}:{what}:{key}", subject, lines,
                   lead_id=req.get("lead_id"), link=f"{_af.PORTAL}/quotations/requests/{req['id']}")
        conn.commit()
    except Exception as e:
        print(f"⚠️ quote online alert {req.get('id')}:", e)
    finally:
        cur.close(); conn.close()


def _log_action(link_id, action, name=None, po=None, text=None):
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO quote_link_actions (link_id, action, name, po, text, ip_hash)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (link_id, action, name, po, data_crypto.enc(text) if text else None, _ip_hash()))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _too_many(link_id) -> bool:
    conn, cur = _db()
    try:
        cur.execute("""SELECT count(*) AS n FROM quote_link_actions WHERE link_id=%s AND created_at > NOW() - INTERVAL '1 day'""",
                    (link_id,))
        return cur.fetchone()["n"] >= 10
    finally:
        cur.close(); conn.close()


def _clean(v, n):
    return " ".join(str(v or "").split())[:n]


def _snapshot(tenant_id, req, answer, detail, po=None) -> dict:
    """What Undo needs (same shape the AI Agent stores, via 'online')."""
    conn, cur = _db()
    try:
        lead = qr._lead(cur, tenant_id, req["lead_id"]) if req.get("lead_id") else None
    finally:
        cur.close(); conn.close()
    return {"status": req["status"], "lead_stage": (lead or {}).get("stage"), "lead_outcome": (lead or {}).get("outcome"),
            "at": datetime.utcnow().isoformat(timespec="seconds"), "answer": answer, "detail": detail,
            "via": "online", "po": po}


def _save_snapshot(request_id, before):
    conn, cur = _db()
    try:
        cur.execute("UPDATE quote_requests SET ai_answer=%s WHERE id=%s", (psycopg2.extras.Json(before), request_id))
        conn.commit()
    finally:
        cur.close(); conn.close()


@quote_online_bp.route("/q/<token>/<action>", methods=["POST"])
def act(token, action):
    if action not in ("accept", "question", "decline"):
        abort(404)
    link, st, err = _load(token)
    if err:
        return err
    if not link or st["kind"] in ("draft", "gone"):
        abort(404)
    lang = link["lang"]
    t = T.get(lang, T["en"])
    if st["kind"] != "live":
        return _page(link, st, lang)
    tid, req = link["tenant_id"], st["req"]
    biz = _biz(tid)["name"] or "the business"
    if _staff_of(tid):
        # The business's own staff previewing the link never answer for the buyer.
        return _page(link, st, lang, error=STAFF_NOTE)
    if _too_many(link["id"]):
        return _page(link, st, lang, error=t["busy"])
    f = request.form
    if action == "question":
        text = (f.get("text") or "").strip()[:2000]
        if not text:
            return _page(link, st, lang, error=t["need_text"], open_form="question")
        _log_action(link["id"], "question", text=text)
        conn, cur = _db()
        try:
            qr._event(cur, req["id"], f"The buyer asked about the quotation (online): {text[:500]}", ONLINE_ACTOR["label"])
            conn.commit()
        finally:
            cur.close(); conn.close()
        _tell(tid, req, "question", text=text)
        return _page(link, st, lang, msg=t["thanks_q"].format(biz=biz))
    if not qr.settings(tid).get("online_accept"):
        abort(404)
    name = _clean(f.get("name"), 120)
    if action == "accept":
        po = _clean(f.get("po"), 60)
        if len(name) < 2:
            return _page(link, st, lang, error=t["need_name"], open_form="accept")
        if f.get("behalf") != "1":
            return _page(link, st, lang, error=t["need_tick"], open_form="accept")
        before = _snapshot(tid, req, "accept", f"accepted online by {name}", po or None)
        note = f"Accepted online by {name}" + (f", order number {po}" if po else "")
        # Undo record first: if Won is then saved, Undo is always there.
        old_answer = req.get("ai_answer")
        _save_snapshot(req["id"], before)
        ok, _msg, won_lead = qr.mark_won(tid, req["id"], None, ONLINE_ACTOR, note[:300])
        if not ok:
            _save_snapshot(req["id"], old_answer)
            conn, cur = _db()
            try:
                st = _state(cur, link)
            finally:
                cur.close(); conn.close()
            return _page(link, st, lang)
        _log_action(link["id"], "accept", name=name, po=po or None)
        if won_lead:
            try:
                from portal_routes import _on_lead_won, _stamp_campaign_converted
                _stamp_campaign_converted(won_lead)
                _on_lead_won(tid, won_lead)
            except Exception as e:
                print("⚠️ quote online won hooks:", e)
        _tell(tid, req, "accept", name=name, po=po)
        return _page(link, {"kind": "won", "req": req}, lang, msg=t["thanks_accept"].format(biz=biz))
    # decline
    reason = (f.get("reason") or "").strip()[:300]
    before = _snapshot(tid, req, "decline", reason or "declined online", None)
    old_answer = req.get("ai_answer")
    _save_snapshot(req["id"], before)
    ok, _msg = qr.close(tid, req["id"], ("Declined online: " + reason) if reason else "Declined online by the buyer",
                        ONLINE_ACTOR)
    if not ok:
        _save_snapshot(req["id"], old_answer)
        return _page(link, st, lang)
    _log_action(link["id"], "decline", name=name or None, text=reason or None)
    _tell(tid, req, "decline", name=name, text=reason)
    return _page(link, {"kind": "lost", "req": req}, lang, msg=t["thanks_decline"].format(biz=biz))
