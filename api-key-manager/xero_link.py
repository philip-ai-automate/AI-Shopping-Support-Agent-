"""
xero_link.py — Xero for accepted quotations (10 Nov 2026).

AI Sales Agent for B2B (Quotations). The owner connects the business's Xero
once (Integration › Xero). On an Accepted quotation the salesperson presses
"Create Xero invoice": PhiXtra makes a DRAFT sales invoice in Xero with the
quoted lines and prices, for the buyer's Xero contact (found by email, then
name, or created). Xero tells PhiXtra when the invoice changes (webhook), so
the deal shows "Invoice INV-0042 · paid / overdue". Payment chasing stays in
Xero (decision 4). One invoice per quotation.

Decisions (10 Nov 2026): B2B plans only · Draft · a business with a shop
(WooCommerce / Shopify) chooses ONE place for accepted quotes: shop order or
Xero invoice · Xero chases payment · tests on Xero's Demo Company.

Settings in .env: XERO_CLIENT_ID, XERO_CLIENT_SECRET, XERO_WEBHOOK_KEY,
optional XERO_REDIRECT_URI, XERO_SCOPES.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import psycopg2.extras
import requests

import data_crypto
from db import get_db_connection
import quote_requests as qr

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REVOKE_URL = "https://identity.xero.com/connect/revocation"
CONNECTIONS_URL = "https://api.xero.com/connections"
API = "https://api.xero.com/api.xro/2.0"
TIMEOUT = 25
# New Xero apps use granular scopes: invoices + contacts only.
SCOPES = os.getenv("XERO_SCOPES", "openid profile email offline_access accounting.invoices accounting.contacts")
REDIRECT_URI = os.getenv("XERO_REDIRECT_URI", "https://portal.phixtra.com/integrations/xero/callback")

STATUS_WORDS = {"DRAFT": "Draft in Xero", "SUBMITTED": "Awaiting approval", "AUTHORISED": "Awaiting payment",
                "PAID": "Paid", "VOIDED": "Voided", "DELETED": "Deleted", "OVERDUE": "Overdue"}


class XeroError(Exception):
    """code: reconnect · config · temporary · bad (Xero refused the data)."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def client_id() -> str:
    return os.getenv("XERO_CLIENT_ID", "").strip()


def _secret() -> str:
    return os.getenv("XERO_CLIENT_SECRET", "").strip()


def configured() -> bool:
    return bool(client_id() and _secret())


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


_ready = False


def ensure_tables():
    global _ready
    if _ready:
        return
    conn, cur = _db()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS xero_links (
                           tenant_id      INTEGER PRIMARY KEY,
                           xero_tenant_id VARCHAR(64),
                           org_name       VARCHAR(200),
                           orgs           JSONB,
                           connection_id  VARCHAR(64),
                           access_enc     TEXT,
                           refresh_enc    TEXT,
                           expires_at     TIMESTAMPTZ,
                           status         VARCHAR(20) NOT NULL DEFAULT 'active',
                           last_error     TEXT,
                           settings       JSONB NOT NULL DEFAULT '{}'::jsonb,
                           connected_by   VARCHAR(200),
                           connected_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("ALTER TABLE quotations ADD COLUMN IF NOT EXISTS xero_invoice JSONB")
        conn.commit()
        _ready = True
    except Exception as e:
        conn.rollback()
        print("⚠️ xero tables:", e)
    finally:
        cur.close(); conn.close()


# ── sign-in ─────────────────────────────────────────────────────────────────

def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": client_id(), "redirect_uri": REDIRECT_URI,
                                       "scope": SCOPES, "state": state})


def _token(fields: dict) -> dict:
    basic = base64.b64encode(f"{client_id()}:{_secret()}".encode()).decode()
    try:
        r = requests.post(TOKEN_URL, data=fields, headers={"Authorization": f"Basic {basic}"}, timeout=TIMEOUT)
    except requests.RequestException:
        raise XeroError("temporary", "Xero didn't answer. Please try again in a minute.")
    d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in d:
        if d.get("error") in ("invalid_grant",):
            raise XeroError("reconnect", "The Xero sign-in has expired. Please connect Xero again.")
        if d.get("error") in ("invalid_client", "unauthorized_client"):
            raise XeroError("config", "PhiXtra's Xero app settings are wrong. Please tell PhiXtra support.")
        raise XeroError("temporary", "Xero didn't accept the sign-in just now. Please try again.")
    d["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=int(d.get("expires_in") or 1800) - 60)
    return d


def exchange_code(code: str) -> dict:
    return _token({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})


def connections(access: str) -> list:
    r = requests.get(CONNECTIONS_URL, headers={"Authorization": f"Bearer {access}"}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise XeroError("temporary", "Couldn't read your Xero organisations. Please try again.")
    return [c for c in r.json() if c.get("tenantType") == "ORGANISATION"]


def save_link(tenant_id: int, tok: dict, orgs: list, by_label: str):
    ensure_tables()
    first = orgs[0] if orgs else {}
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO xero_links (tenant_id, xero_tenant_id, org_name, orgs, connection_id, access_enc,
                              refresh_enc, expires_at, status, last_error, connected_by, connected_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',NULL,%s,NOW(),NOW())
                       ON CONFLICT (tenant_id) DO UPDATE SET xero_tenant_id=EXCLUDED.xero_tenant_id,
                              org_name=EXCLUDED.org_name, orgs=EXCLUDED.orgs, connection_id=EXCLUDED.connection_id,
                              access_enc=EXCLUDED.access_enc, refresh_enc=EXCLUDED.refresh_enc,
                              expires_at=EXCLUDED.expires_at, status='active', last_error=NULL,
                              connected_by=EXCLUDED.connected_by, connected_at=NOW(), updated_at=NOW()""",
                    (int(tenant_id), first.get("tenantId"), (first.get("tenantName") or "")[:200],
                     psycopg2.extras.Json([{"id": o.get("tenantId"), "name": o.get("tenantName"), "conn": o.get("id")}
                                           for o in orgs]),
                     first.get("id"), data_crypto.enc(tok["access_token"]), data_crypto.enc(tok.get("refresh_token") or ""),
                     tok["expires_at"], by_label[:200]))
        conn.commit()
    finally:
        cur.close(); conn.close()


def link_for(tenant_id: int):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM xero_links WHERE tenant_id=%s", (int(tenant_id),))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def choose_org(tenant_id: int, xero_tenant_id: str) -> bool:
    link = link_for(tenant_id)
    if not link:
        return False
    org = next((o for o in (link.get("orgs") or []) if o.get("id") == xero_tenant_id), None)
    if not org:
        return False
    conn, cur = _db()
    try:
        cur.execute("UPDATE xero_links SET xero_tenant_id=%s, org_name=%s, connection_id=%s, updated_at=NOW() WHERE tenant_id=%s",
                    (org["id"], (org.get("name") or "")[:200], org.get("conn"), int(tenant_id)))
        conn.commit()
        return True
    finally:
        cur.close(); conn.close()


def save_settings(tenant_id: int, s: dict):
    clean = {}
    code = re.sub(r"[^A-Za-z0-9\-_.]", "", str(s.get("account_code") or ""))[:20]
    clean["account_code"] = code
    clean["accepted_to"] = "xero" if s.get("accepted_to") == "xero" else "shop"
    conn, cur = _db()
    try:
        cur.execute("UPDATE xero_links SET settings = settings || %s, updated_at=NOW() WHERE tenant_id=%s",
                    (psycopg2.extras.Json(clean), int(tenant_id)))
        conn.commit()
    finally:
        cur.close(); conn.close()


def disconnect(tenant_id: int):
    link = link_for(tenant_id)
    if not link:
        return
    try:
        tok = _access(link)
        if link.get("connection_id"):
            requests.delete(f"{CONNECTIONS_URL}/{link['connection_id']}", headers={"Authorization": f"Bearer {tok}"},
                            timeout=TIMEOUT)
        rt = data_crypto.dec(link.get("refresh_enc") or "")
        if rt:
            basic = base64.b64encode(f"{client_id()}:{_secret()}".encode()).decode()
            requests.post(REVOKE_URL, data={"token": rt}, headers={"Authorization": f"Basic {basic}"}, timeout=TIMEOUT)
    except Exception as e:
        print("⚠️ xero disconnect at Xero:", e)
    conn, cur = _db()
    try:
        cur.execute("""UPDATE xero_links SET status='disconnected', access_enc=NULL, refresh_enc=NULL, updated_at=NOW()
                        WHERE tenant_id=%s""", (int(tenant_id),))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _mark(tenant_id: int, status: str, err: str = None):
    conn, cur = _db()
    try:
        cur.execute("UPDATE xero_links SET status=%s, last_error=%s, updated_at=NOW() WHERE tenant_id=%s",
                    (status, err, int(tenant_id)))
        conn.commit()
    finally:
        cur.close(); conn.close()


_refresh_lock = threading.Lock()


def _access(link: dict) -> str:
    """A fresh access token (Xero tokens last 30 minutes; refresh tokens rotate)."""
    if link.get("expires_at") and link["expires_at"] > datetime.now(timezone.utc) + timedelta(seconds=30):
        return data_crypto.dec(link["access_enc"])
    with _refresh_lock:
        fresh = link_for(link["tenant_id"])        # another worker may have refreshed already
        if fresh and fresh.get("expires_at") and fresh["expires_at"] > datetime.now(timezone.utc) + timedelta(seconds=30):
            return data_crypto.dec(fresh["access_enc"])
        try:
            tok = _token({"grant_type": "refresh_token", "refresh_token": data_crypto.dec(fresh["refresh_enc"] or "")})
        except XeroError as e:
            if e.code == "reconnect":
                _mark(link["tenant_id"], "reconnect", str(e))
            raise
        conn, cur = _db()
        try:
            cur.execute("""UPDATE xero_links SET access_enc=%s, refresh_enc=%s, expires_at=%s, status='active',
                                  last_error=NULL, updated_at=NOW() WHERE tenant_id=%s""",
                        (data_crypto.enc(tok["access_token"]), data_crypto.enc(tok.get("refresh_token") or ""),
                         tok["expires_at"], link["tenant_id"]))
            conn.commit()
        finally:
            cur.close(); conn.close()
        return tok["access_token"]


def _call(link: dict, method: str, path: str, params: dict = None, body: dict = None, idem: str = None) -> dict:
    headers = {"Authorization": f"Bearer {_access(link)}", "xero-tenant-id": link["xero_tenant_id"],
               "Accept": "application/json"}
    if idem:
        headers["Idempotency-Key"] = idem
    try:
        r = requests.request(method, API + path, params=params, json=body, headers=headers, timeout=TIMEOUT)
    except requests.RequestException:
        raise XeroError("temporary", "Xero didn't answer. Please try again in a minute.")
    if r.status_code == 401:
        _mark(link["tenant_id"], "reconnect", "Xero access was removed")
        raise XeroError("reconnect", "PhiXtra no longer has access to your Xero. Please connect Xero again.")
    if r.status_code == 429:
        raise XeroError("temporary", "Xero is busy (too many requests). Please try again in a minute.")
    d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 400:
        msgs = []
        for el in d.get("Elements") or []:
            msgs += [v.get("Message") for v in el.get("ValidationErrors") or [] if v.get("Message")]
        raise XeroError("bad", "Xero didn't accept it: " + ("; ".join(msgs[:3]) or d.get("Message") or "invalid data"))
    if r.status_code >= 300:
        raise XeroError("temporary", f"Xero answered with an error ({r.status_code}). Please try again.")
    return d


# ── contacts and invoices ───────────────────────────────────────────────────

def _q(v: str) -> str:
    return (v or "").replace("\\", "").replace('"', "")


def find_or_create_contact(link: dict, req: dict) -> tuple:
    """(ContactID, 'existing' | 'created')."""
    email = (req.get("buyer_email") or "").strip().lower()
    name = (req.get("buyer_company") or req.get("buyer_name") or email or "Buyer").strip()[:250]
    if email:
        d = _call(link, "GET", "/Contacts", params={"where": f'EmailAddress!=null&&EmailAddress.ToLower()=="{_q(email)}"'})
        if d.get("Contacts"):
            return d["Contacts"][0]["ContactID"], "existing"
    d = _call(link, "GET", "/Contacts", params={"where": f'Name.ToLower()=="{_q(name.lower())}"'})
    if d.get("Contacts"):
        return d["Contacts"][0]["ContactID"], "existing"
    person = (req.get("buyer_name") or "").split()
    c = {"Name": name, "EmailAddress": email or None,
         "FirstName": person[0] if person else None, "LastName": " ".join(person[1:]) or None}
    if req.get("buyer_phone"):
        c["Phones"] = [{"PhoneType": "DEFAULT", "PhoneNumber": req["buyer_phone"][:50]}]
    d = _call(link, "POST", "/Contacts", body={"Contacts": [{k: v for k, v in c.items() if v}]})
    return d["Contacts"][0]["ContactID"], "created"


def _parse_date(s):
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(s))
    if m:
        return m.group(1)
    m = re.search(r"/Date\((\d+)", str(s))
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc).date().isoformat()
    return None


def _inv_view(inv: dict, link: dict, prev: dict = None) -> dict:
    o = dict(prev or {})
    o.update({"invoice_id": inv.get("InvoiceID"), "number": inv.get("InvoiceNumber") or o.get("number") or "",
              "status": inv.get("Status") or o.get("status"), "total": inv.get("Total"),
              "amount_due": inv.get("AmountDue"), "currency": inv.get("CurrencyCode"),
              "due": _parse_date(inv.get("DueDateString") or inv.get("DueDate")),
              "url": f"https://go.xero.com/AccountsReceivable/View.aspx?InvoiceID={inv.get('InvoiceID')}",
              "org": link.get("org_name") or "",
              "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return o


def state_of(o: dict) -> dict:
    """Words for an invoice: overdue = awaiting payment past its due date."""
    o = dict(o or {})
    st = o.get("status") or "DRAFT"
    if st == "AUTHORISED" and o.get("due") and o["due"] < date.today().isoformat() and float(o.get("amount_due") or 0) > 0:
        st = "OVERDUE"
    o["key"] = st
    o["word"] = STATUS_WORDS.get(st, st.capitalize())
    o["paid"] = st == "PAID"
    return o


# ── which place for an accepted quotation ───────────────────────────────────

def _has_shop(tenant_id: int) -> bool:
    try:
        import woo_orders as _wo
        if _wo.has_shop(tenant_id) or _wo._shopify_on(tenant_id):
            return True
    except Exception:
        pass
    return False


def chosen(tenant_id: int) -> str:
    """'xero' when accepted quotes go to Xero; 'shop' when a shop order is made
    instead; '' when Xero isn't connected."""
    link = link_for(tenant_id)
    if not link or link["status"] == "disconnected" or not link.get("xero_tenant_id"):
        return ""
    if not _has_shop(tenant_id):
        return "xero"
    return "xero" if (link.get("settings") or {}).get("accepted_to") == "xero" else "shop"


def _latest_sent(cur, tenant_id: int, request_id: int):
    cur.execute("""SELECT id, number, version, data, currency, total, sent_at, xero_invoice FROM quotations
                    WHERE tenant_id=%s AND request_id=%s AND status='sent'
                    ORDER BY version DESC, id DESC LIMIT 1""", (tenant_id, request_id))
    return cur.fetchone()


def for_request(tenant_id: int, request_id: int, refresh: bool = True) -> dict:
    """{state: none | ready | reconnect | made, invoice?}."""
    ensure_tables()
    if not qr.allowed(tenant_id):
        return {"state": "none"}
    req = qr.get(tenant_id, request_id)
    if not req or req["status"] != "won":
        return {"state": "none"}
    conn, cur = _db()
    try:
        q = _latest_sent(cur, tenant_id, request_id)
    finally:
        cur.close(); conn.close()
    if not q:
        return {"state": "none"}
    link = link_for(tenant_id)
    if q.get("xero_invoice"):
        o = dict(q["xero_invoice"])
        # Fallback when a webhook is missed: re-read at most every 30 minutes.
        if refresh and link and link["status"] == "active" and o.get("status") not in ("PAID", "VOIDED", "DELETED"):
            try:
                last = datetime.fromisoformat(o.get("checked_at")) if o.get("checked_at") else None
                if not last or datetime.now(timezone.utc) - last > timedelta(minutes=30):
                    o = refresh_invoice(tenant_id, q["id"]) or o
            except Exception as e:
                print("⚠️ xero refresh:", e)
        return {"state": "made", "invoice": state_of(o), "qid": q["id"]}
    if chosen(tenant_id) != "xero":
        return {"state": "none"}
    if link["status"] == "reconnect":
        return {"state": "reconnect", "qid": q["id"]}
    return {"state": "ready", "qid": q["id"]}


def create_invoice(tenant_id: int, request_id: int, actor: dict, po: str = "") -> tuple:
    st = for_request(tenant_id, request_id, refresh=False)
    if st["state"] == "made":
        return True, f"Xero invoice {st['invoice'].get('number') or ''} already exists for this quotation."
    if st["state"] == "reconnect":
        return False, "PhiXtra no longer has access to your Xero. Connect Xero again under Integration."
    if st["state"] != "ready":
        return False, "This quotation can't be turned into a Xero invoice."
    link = link_for(tenant_id)
    req = qr.get(tenant_id, request_id)
    conn, cur = _db()
    try:
        q = _latest_sent(cur, tenant_id, request_id)
    finally:
        cur.close(); conn.close()
    d = q.get("data") or {}
    acct = (link.get("settings") or {}).get("account_code") or None
    lines = []
    for ln in d.get("lines") or []:
        if ln.get("price") is None or not ln.get("qty"):
            continue
        li = {"Description": (ln.get("name") or "Item")[:4000] + (f" ({ln['code']})" if ln.get("code") else ""),
              "Quantity": float(ln["qty"]), "UnitAmount": round(float(ln["price"]), 4)}
        if float(ln.get("disc") or 0):
            li["DiscountRate"] = round(float(ln["disc"]), 2)
        if acct:
            li["AccountCode"] = acct
        lines.append(li)
    for c in d.get("charges") or []:
        amt = float(c.get("price") or 0)
        if amt:
            li = {"Description": (c.get("name") or "Charge")[:4000], "Quantity": float(c.get("qty") or 1), "UnitAmount": amt}
            if acct:
                li["AccountCode"] = acct
            lines.append(li)
    if not lines:
        return False, "The quotation has no priced lines to put on an invoice."
    num = q["number"] + (f" v{q['version']}" if (q.get("version") or 1) > 1 else "")
    po = (po or (req.get("ai_answer") or {}).get("po") or "").strip()
    try:
        contact_id, contact_how = find_or_create_contact(link, req)
        body = {"Invoices": [{"Type": "ACCREC", "Contact": {"ContactID": contact_id}, "Status": "DRAFT",
                              "LineAmountTypes": "Exclusive", "Date": date.today().isoformat(),
                              "Reference": (f"{num} · PO {po}" if po else num)[:255],
                              "CurrencyCode": (q.get("currency") or req.get("quote_currency") or None),
                              "LineItems": lines}]}
        if not body["Invoices"][0]["CurrencyCode"]:
            body["Invoices"][0].pop("CurrencyCode")
        res = _call(link, "POST", "/Invoices", body=body, idem=f"phx-{tenant_id}-{q['id']}")
    except XeroError as e:
        return False, str(e) + " Nothing was created."
    inv = (res.get("Invoices") or [{}])[0]
    o = _inv_view(inv, link, {"contact": contact_how, "quote_total": float(q.get("total") or 0),
                              "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "by": actor.get("label") or ""})
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET xero_invoice=%s WHERE id=%s AND tenant_id=%s",
                    (psycopg2.extras.Json(o), q["id"], tenant_id))
        what = f"Xero draft invoice {o.get('number') or ''} created from {num}".replace("  ", " ")
        if contact_how == "created":
            what += "; a new Xero contact was made for the buyer"
        qr._event(cur, request_id, what, actor.get("label") or "")
        conn.commit()
    finally:
        cur.close(); conn.close()
    return True, f"Draft invoice {o.get('number') or ''} created in Xero. Check it there and approve it to send.".replace("  ", " ")


def refresh_invoice(tenant_id: int, quotation_id: int):
    """Re-read one invoice from Xero; writes a history line when its state changes."""
    link = link_for(tenant_id)
    if not link or link["status"] != "active":
        return None
    conn, cur = _db()
    try:
        cur.execute("SELECT id, request_id, xero_invoice FROM quotations WHERE id=%s AND tenant_id=%s", (quotation_id, tenant_id))
        q = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not q or not q.get("xero_invoice") or not q["xero_invoice"].get("invoice_id"):
        return None
    d = _call(link, "GET", f"/Invoices/{q['xero_invoice']['invoice_id']}")
    inv = (d.get("Invoices") or [{}])[0]
    old = dict(q["xero_invoice"])
    new = _inv_view(inv, link, old)
    before, after = state_of(old)["key"], state_of(new)["key"]
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET xero_invoice=%s WHERE id=%s", (psycopg2.extras.Json(new), quotation_id))
        if before != after:
            if after == "PAID" and not new.get("paid_at"):
                new["paid_at"] = new["checked_at"]
                cur.execute("UPDATE quotations SET xero_invoice=%s WHERE id=%s", (psycopg2.extras.Json(new), quotation_id))
            qr._event(cur, q["request_id"], f"Xero invoice {new.get('number') or ''}: {STATUS_WORDS.get(after, after)}", "Xero")
        conn.commit()
    finally:
        cur.close(); conn.close()
    return new


# ── webhooks ────────────────────────────────────────────────────────────────

def webhook_ok(body: bytes, signature: str) -> bool:
    key = os.getenv("XERO_WEBHOOK_KEY", "").strip()
    if not key or not signature:
        return False
    want = base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, signature.strip())


def handle_events(payload: dict):
    """Invoice events → re-read the invoice (only ones PhiXtra made)."""
    ids = {}
    for ev in payload.get("events") or []:
        if ev.get("eventCategory") == "INVOICE" and ev.get("resourceId") and ev.get("tenantId"):
            ids.setdefault(ev["tenantId"], set()).add(ev["resourceId"])
    if not ids:
        return
    ensure_tables()
    conn, cur = _db()
    try:
        for xt, inv_ids in ids.items():
            cur.execute("""SELECT q.id, q.tenant_id FROM quotations q JOIN xero_links x ON x.tenant_id=q.tenant_id
                            WHERE x.xero_tenant_id=%s AND q.xero_invoice->>'invoice_id' = ANY(%s)""",
                        (xt, list(inv_ids)))
            rows = cur.fetchall()
            conn.commit()
            for r in rows:
                try:
                    refresh_invoice(r["tenant_id"], r["id"])
                except Exception as e:
                    print("⚠️ xero webhook refresh:", e)
    finally:
        cur.close(); conn.close()


def invoices_for_requests(tenant_id: int, request_ids: list) -> dict:
    """{request_id: invoice state} for the Sales Pipeline Quotation column."""
    if not request_ids:
        return {}
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT DISTINCT ON (request_id) request_id, xero_invoice FROM quotations
                        WHERE tenant_id=%s AND request_id = ANY(%s) AND status='sent'
                        ORDER BY request_id, version DESC, id DESC""", (tenant_id, list(request_ids)))
        return {r["request_id"]: state_of(r["xero_invoice"]) for r in cur.fetchall() if r["xero_invoice"]}
    finally:
        cur.close(); conn.close()
