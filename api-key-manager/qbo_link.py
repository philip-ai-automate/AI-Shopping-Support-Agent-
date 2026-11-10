"""
qbo_link.py — QuickBooks Online for accepted quotations (10 Nov 2026).

Same way of working as Xero (xero_link.py), approved plan
https://claude.ai/artifact/AfixK2xzmJA1XXCrC4suLs:
  * B2B plans only; one accounts package per business (Xero OR QuickBooks).
  * "Create QuickBooks invoice" on an Accepted quotation makes a normal
    invoice that QuickBooks does NOT email (QuickBooks has no drafts); the
    accountant checks it and presses Send in QuickBooks.
  * Each line uses the QuickBooks product/service with the same name or SKU;
    a line with no match uses the one item the business picked (e.g. "Sales")
    with the product name in the description. No new items are made.
  * Customer found by email, then name, or created. One invoice per quotation.
  * Paid / overdue comes back by Intuit's signed webhook, with a re-read
    every 30 minutes as a backup. Payment chasing stays in QuickBooks.
  * Businesses with a shop choose ONE place for accepted quotes.

Settings in .env: QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_WEBHOOK_TOKEN,
QBO_ENV (sandbox | production, default sandbox), optional QBO_REDIRECT_URI.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import psycopg2.extras
import requests

import data_crypto
from db import get_db_connection
import quote_requests as qr

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
SCOPE = "com.intuit.quickbooks.accounting"
MINOR = "75"
TIMEOUT = 25
REDIRECT_URI = os.getenv("QBO_REDIRECT_URI", "https://portal.phixtra.com/integrations/quickbooks/callback")

WORDS = {"NOT_SENT": "Not sent yet", "OPEN": "Awaiting payment", "PAID": "Paid", "OVERDUE": "Overdue",
         "PART_PAID": "Part paid", "VOIDED": "Voided", "DELETED": "Deleted"}


class QboError(Exception):
    """code: reconnect · config · temporary · bad."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _env() -> str:
    return "production" if os.getenv("QBO_ENV", "sandbox").strip().lower() == "production" else "sandbox"


def api_base() -> str:
    return "https://quickbooks.api.intuit.com" if _env() == "production" else "https://sandbox-quickbooks.api.intuit.com"


def app_base() -> str:
    return "https://app.qbo.intuit.com" if _env() == "production" else "https://app.sandbox.qbo.intuit.com"


def client_id() -> str:
    return os.getenv("QBO_CLIENT_ID", "").strip()


def _secret() -> str:
    return os.getenv("QBO_CLIENT_SECRET", "").strip()


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
        cur.execute("""CREATE TABLE IF NOT EXISTS qbo_links (
                           tenant_id     INTEGER PRIMARY KEY,
                           realm_id      VARCHAR(40),
                           company_name  VARCHAR(200),
                           access_enc    TEXT,
                           refresh_enc   TEXT,
                           expires_at    TIMESTAMPTZ,
                           status        VARCHAR(20) NOT NULL DEFAULT 'active',
                           last_error    TEXT,
                           settings      JSONB NOT NULL DEFAULT '{}'::jsonb,
                           connected_by  VARCHAR(200),
                           connected_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("ALTER TABLE quotations ADD COLUMN IF NOT EXISTS qbo_invoice JSONB")
        conn.commit()
        _ready = True
    except Exception as e:
        conn.rollback()
        print("⚠️ qbo tables:", e)
    finally:
        cur.close(); conn.close()


# ── sign-in ─────────────────────────────────────────────────────────────────

def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({"client_id": client_id(), "response_type": "code", "scope": SCOPE,
                                       "redirect_uri": REDIRECT_URI, "state": state})


def _basic() -> str:
    return "Basic " + base64.b64encode(f"{client_id()}:{_secret()}".encode()).decode()


def _token(fields: dict) -> dict:
    try:
        r = requests.post(TOKEN_URL, data=fields, timeout=TIMEOUT,
                          headers={"Authorization": _basic(), "Accept": "application/json"})
    except requests.RequestException:
        raise QboError("temporary", "QuickBooks didn't answer. Please try again in a minute.")
    d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in d:
        if d.get("error") == "invalid_grant":
            raise QboError("reconnect", "The QuickBooks sign-in has expired. Please connect QuickBooks again.")
        if d.get("error") in ("invalid_client", "unauthorized_client"):
            raise QboError("config", "PhiXtra's QuickBooks app settings are wrong. Please tell PhiXtra support.")
        raise QboError("temporary", "QuickBooks didn't accept the sign-in just now. Please try again.")
    d["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=int(d.get("expires_in") or 3600) - 60)
    return d


def exchange_code(code: str) -> dict:
    return _token({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})


def save_link(tenant_id: int, tok: dict, realm_id: str, by_label: str):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO qbo_links (tenant_id, realm_id, access_enc, refresh_enc, expires_at, status, last_error,
                              connected_by, connected_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,'active',NULL,%s,NOW(),NOW())
                       ON CONFLICT (tenant_id) DO UPDATE SET realm_id=EXCLUDED.realm_id, access_enc=EXCLUDED.access_enc,
                              refresh_enc=EXCLUDED.refresh_enc, expires_at=EXCLUDED.expires_at, status='active',
                              last_error=NULL, connected_by=EXCLUDED.connected_by, connected_at=NOW(), updated_at=NOW()""",
                    (int(tenant_id), str(realm_id)[:40], data_crypto.enc(tok["access_token"]),
                     data_crypto.enc(tok.get("refresh_token") or ""), tok["expires_at"], by_label[:200]))
        conn.commit()
    finally:
        cur.close(); conn.close()
    try:
        link = link_for(tenant_id)
        info = _call(link, "GET", f"/companyinfo/{link['realm_id']}")
        name = ((info.get("CompanyInfo") or {}).get("CompanyName") or "")[:200]
        conn, cur = _db()
        try:
            cur.execute("UPDATE qbo_links SET company_name=%s WHERE tenant_id=%s", (name, int(tenant_id)))
            conn.commit()
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ qbo company name:", e)


def link_for(tenant_id: int):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM qbo_links WHERE tenant_id=%s", (int(tenant_id),))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def connected(tenant_id: int) -> bool:
    link = link_for(tenant_id)
    return bool(link and link["status"] != "disconnected" and link.get("realm_id"))


def save_settings(tenant_id: int, s: dict):
    clean = {"accepted_to": "accounts" if s.get("accepted_to") in ("accounts", "qbo") else "shop"}
    if s.get("fallback_item_id"):
        clean["fallback_item_id"] = re.sub(r"\D", "", str(s["fallback_item_id"]))[:20]
        clean["fallback_item_name"] = str(s.get("fallback_item_name") or "")[:100]
    conn, cur = _db()
    try:
        cur.execute("UPDATE qbo_links SET settings = settings || %s, updated_at=NOW() WHERE tenant_id=%s",
                    (psycopg2.extras.Json(clean), int(tenant_id)))
        conn.commit()
    finally:
        cur.close(); conn.close()


def disconnect(tenant_id: int):
    link = link_for(tenant_id)
    if not link:
        return
    try:
        rt = data_crypto.dec(link.get("refresh_enc") or "")
        if rt:
            requests.post(REVOKE_URL, json={"token": rt}, timeout=TIMEOUT,
                          headers={"Authorization": _basic(), "Accept": "application/json"})
    except Exception as e:
        print("⚠️ qbo revoke:", e)
    conn, cur = _db()
    try:
        cur.execute("""UPDATE qbo_links SET status='disconnected', access_enc=NULL, refresh_enc=NULL, updated_at=NOW()
                        WHERE tenant_id=%s""", (int(tenant_id),))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _mark(tenant_id: int, status: str, err: str = None):
    conn, cur = _db()
    try:
        cur.execute("UPDATE qbo_links SET status=%s, last_error=%s, updated_at=NOW() WHERE tenant_id=%s",
                    (status, err, int(tenant_id)))
        conn.commit()
    finally:
        cur.close(); conn.close()


_lock = threading.Lock()


def _access(link: dict) -> str:
    if link.get("expires_at") and link["expires_at"] > datetime.now(timezone.utc) + timedelta(seconds=30):
        return data_crypto.dec(link["access_enc"])
    with _lock:
        fresh = link_for(link["tenant_id"])
        if fresh and fresh.get("expires_at") and fresh["expires_at"] > datetime.now(timezone.utc) + timedelta(seconds=30):
            return data_crypto.dec(fresh["access_enc"])
        try:
            tok = _token({"grant_type": "refresh_token", "refresh_token": data_crypto.dec(fresh["refresh_enc"] or "")})
        except QboError as e:
            if e.code == "reconnect":
                _mark(link["tenant_id"], "reconnect", str(e))
            raise
        conn, cur = _db()
        try:
            cur.execute("""UPDATE qbo_links SET access_enc=%s, refresh_enc=%s, expires_at=%s, status='active',
                                  last_error=NULL, updated_at=NOW() WHERE tenant_id=%s""",
                        (data_crypto.enc(tok["access_token"]), data_crypto.enc(tok.get("refresh_token") or ""),
                         tok["expires_at"], link["tenant_id"]))
            conn.commit()
        finally:
            cur.close(); conn.close()
        return tok["access_token"]


def _call(link: dict, method: str, path: str, params: dict = None, body: dict = None) -> dict:
    url = f"{api_base()}/v3/company/{link['realm_id']}{path}" if not path.startswith("/companyinfo") \
        else f"{api_base()}/v3/company/{link['realm_id']}{path}"
    params = dict(params or {}, minorversion=MINOR)
    try:
        r = requests.request(method, url, params=params, json=body, timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {_access(link)}", "Accept": "application/json",
                                      "Content-Type": "application/json"})
    except requests.RequestException:
        raise QboError("temporary", "QuickBooks didn't answer. Please try again in a minute.")
    if r.status_code == 401:
        _mark(link["tenant_id"], "reconnect", "QuickBooks access was removed")
        raise QboError("reconnect", "PhiXtra no longer has access to your QuickBooks. Please connect QuickBooks again.")
    if r.status_code == 429:
        raise QboError("temporary", "QuickBooks is busy. Please try again in a minute.")
    d = r.json() if "json" in r.headers.get("content-type", "") else {}
    if r.status_code >= 400:
        errs = ((d.get("Fault") or {}).get("Error") or [])
        msg = "; ".join((e.get("Detail") or e.get("Message") or "") for e in errs[:2]) or f"error {r.status_code}"
        raise QboError("bad" if r.status_code == 400 else "temporary", "QuickBooks didn't accept it: " + msg)
    return d


def _query(link: dict, q: str) -> dict:
    return (_call(link, "GET", "/query", params={"query": q}).get("QueryResponse") or {})


def _s(v: str) -> str:
    """A value inside a QuickBooks query string."""
    return (v or "").replace("\\", "").replace("'", "\\'")[:200]


# ── customers, items, invoices ──────────────────────────────────────────────

def find_or_create_customer(link: dict, req: dict) -> tuple:
    email = (req.get("buyer_email") or "").strip().lower()
    name = (req.get("buyer_company") or req.get("buyer_name") or email or "Buyer").strip()[:100]
    if email:
        try:
            got = _query(link, f"select Id from Customer where PrimaryEmailAddr = '{_s(email)}'").get("Customer") or []
            if got:
                return got[0]["Id"], "existing"
        except QboError as e:
            if e.code != "bad":
                raise
    got = _query(link, f"select Id from Customer where DisplayName = '{_s(name)}'").get("Customer") or []
    if got:
        return got[0]["Id"], "existing"
    person = (req.get("buyer_name") or "").split()
    c = {"DisplayName": name, "CompanyName": (req.get("buyer_company") or "")[:100] or None,
         "GivenName": person[0][:25] if person else None, "FamilyName": " ".join(person[1:])[:25] or None,
         "PrimaryEmailAddr": {"Address": email} if email else None,
         "PrimaryPhone": {"FreeFormNumber": req["buyer_phone"][:30]} if req.get("buyer_phone") else None}
    d = _call(link, "POST", "/customer", body={k: v for k, v in c.items() if v})
    return d["Customer"]["Id"], "created"


def items(link: dict) -> list:
    """Products and services the business can pick as the item for unmatched lines."""
    got = _query(link, "select Id, Name, Type from Item where Active = true maxresults 200").get("Item") or []
    return [{"id": i["Id"], "name": i["Name"], "type": i.get("Type")} for i in got if i.get("Type") != "Category"]


def _match_item(link: dict, name: str, sku: str, cache: dict):
    key = (name or "", sku or "")
    if key in cache:
        return cache[key]
    found = None
    if sku:
        got = _query(link, f"select Id, Name from Item where Sku = '{_s(sku)}'").get("Item") or []
        found = got[0] if got else None
    if not found and name:
        got = _query(link, f"select Id, Name from Item where Name = '{_s(name)}'").get("Item") or []
        found = got[0] if got else None
    cache[key] = found
    return found


def _view(inv: dict, prev: dict = None) -> dict:
    o = dict(prev or {})
    o.update({"invoice_id": inv.get("Id"), "number": inv.get("DocNumber") or o.get("number") or "",
              "total": inv.get("TotalAmt"), "balance": inv.get("Balance"), "due": inv.get("DueDate"),
              "email_status": inv.get("EmailStatus"), "currency": (inv.get("CurrencyRef") or {}).get("value"),
              "sync": inv.get("SyncToken"),
              "url": f"{app_base()}/app/invoice?txnId={inv.get('Id')}",
              "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if inv.get("status") == "Deleted":
        o["deleted"] = True
    return o


def state_of(o: dict) -> dict:
    o = dict(o or {})
    total, bal = float(o.get("total") or 0), float(o.get("balance") if o.get("balance") is not None else o.get("total") or 0)
    if o.get("deleted"):
        k = "DELETED"
    elif total > 0 and bal <= 0.004:
        k = "PAID"
    elif o.get("due") and o["due"] < date.today().isoformat() and bal > 0:
        k = "OVERDUE"
    elif 0 < bal < total:
        k = "PART_PAID"
    elif o.get("email_status") in (None, "", "NotSet", "NeedToSend"):
        k = "NOT_SENT"
    else:
        k = "OPEN"
    o["key"], o["word"], o["paid"] = k, WORDS[k], k == "PAID"
    return o


# ── which place for an accepted quotation ───────────────────────────────────

def _has_shop(tenant_id: int) -> bool:
    try:
        import woo_orders as _wo
        return bool(_wo.has_shop(tenant_id) or _wo._shopify_on(tenant_id))
    except Exception:
        return False


def chosen(tenant_id: int) -> str:
    """'qbo' when accepted quotes go to QuickBooks; 'shop'; '' = not connected."""
    link = link_for(tenant_id)
    if not link or link["status"] == "disconnected" or not link.get("realm_id"):
        return ""
    if not _has_shop(tenant_id):
        return "qbo"
    return "qbo" if (link.get("settings") or {}).get("accepted_to") == "accounts" else "shop"


def _latest_sent(cur, tenant_id, request_id):
    cur.execute("""SELECT id, number, version, data, currency, total, sent_at, qbo_invoice FROM quotations
                    WHERE tenant_id=%s AND request_id=%s AND status='sent'
                    ORDER BY version DESC, id DESC LIMIT 1""", (tenant_id, request_id))
    return cur.fetchone()


def for_request(tenant_id: int, request_id: int, refresh: bool = True) -> dict:
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
    if q.get("qbo_invoice"):
        o = dict(q["qbo_invoice"])
        if refresh and link and link["status"] == "active" and state_of(o)["key"] not in ("PAID", "DELETED"):
            try:
                last = datetime.fromisoformat(o["checked_at"]) if o.get("checked_at") else None
                if not last or datetime.now(timezone.utc) - last > timedelta(minutes=30):
                    o = refresh_invoice(tenant_id, q["id"]) or o
            except Exception as e:
                print("⚠️ qbo refresh:", e)
        return {"state": "made", "invoice": state_of(o), "qid": q["id"]}
    if chosen(tenant_id) != "qbo":
        return {"state": "none"}
    if link["status"] == "reconnect":
        return {"state": "reconnect", "qid": q["id"]}
    if not (link.get("settings") or {}).get("fallback_item_id"):
        return {"state": "setup", "qid": q["id"]}
    return {"state": "ready", "qid": q["id"]}


def create_invoice(tenant_id: int, request_id: int, actor: dict, po: str = "") -> tuple:
    st = for_request(tenant_id, request_id, refresh=False)
    if st["state"] == "made":
        return True, f"QuickBooks invoice {st['invoice'].get('number') or ''} already exists for this quotation."
    if st["state"] == "reconnect":
        return False, "PhiXtra no longer has access to your QuickBooks. Connect QuickBooks again under Integration."
    if st["state"] == "setup":
        return False, "Pick the QuickBooks item for unmatched lines first (Integration › QuickBooks)."
    if st["state"] != "ready":
        return False, "This quotation can't be turned into a QuickBooks invoice."
    link = link_for(tenant_id)
    fallback = (link.get("settings") or {}).get("fallback_item_id")
    req = qr.get(tenant_id, request_id)
    conn, cur = _db()
    try:
        q = _latest_sent(cur, tenant_id, request_id)
    finally:
        cur.close(); conn.close()
    d = q.get("data") or {}
    num = q["number"] + (f" v{q['version']}" if (q.get("version") or 1) > 1 else "")
    po = (po or (req.get("ai_answer") or {}).get("po") or "").strip()
    try:
        cust_id, how = find_or_create_customer(link, req)
        cache, lines, unmatched = {}, [], 0
        for ln in d.get("lines") or []:
            if ln.get("price") is None or not ln.get("qty"):
                continue
            qty = float(ln["qty"])
            disc = float(ln.get("disc") or 0)
            unit = round(float(ln["price"]) * (1 - disc / 100), 4)
            it = _match_item(link, ln.get("name") or "", ln.get("code") or "", cache)
            desc = (ln.get("name") or "Item") + (f" ({ln['code']})" if ln.get("code") else "") + \
                   (f" — {('%g' % disc)}% discount" if disc else "")
            if not it:
                unmatched += 1
            lines.append({"DetailType": "SalesItemLineDetail", "Amount": round(qty * unit, 2), "Description": desc[:4000],
                          "SalesItemLineDetail": {"ItemRef": {"value": (it or {}).get("Id") or fallback},
                                                  "Qty": qty, "UnitPrice": unit}})
        for c in d.get("charges") or []:
            amt = float(c.get("price") or 0)
            if not amt:
                continue
            qty = float(c.get("qty") or 1)
            lines.append({"DetailType": "SalesItemLineDetail", "Amount": round(qty * amt, 2),
                          "Description": (c.get("name") or "Charge")[:4000],
                          "SalesItemLineDetail": {"ItemRef": {"value": fallback}, "Qty": qty, "UnitPrice": amt}})
        if not lines:
            return False, "The quotation has no priced lines to put on an invoice."
        memo = f"Quotation {num}" + (f" · PO {po}" if po else "")
        body = {"CustomerRef": {"value": cust_id}, "Line": lines, "EmailStatus": "NotSet",
                "CustomerMemo": {"value": memo[:1000]}, "PrivateNote": f"From PhiXtra quotation {num}"[:4000]}
        cur_code = q.get("currency") or req.get("quote_currency")
        if cur_code:
            body["CurrencyRef"] = {"value": cur_code}
        res = _call(link, "POST", "/invoice", params={"requestid": f"phx-{tenant_id}-{q['id']}"}, body=body)
    except QboError as e:
        return False, str(e) + " Nothing was created."
    inv = res.get("Invoice") or {}
    o = _view(inv, {"customer": how, "quote_total": float(q.get("total") or 0), "unmatched": unmatched,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": actor.get("label") or ""})
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET qbo_invoice=%s WHERE id=%s AND tenant_id=%s",
                    (psycopg2.extras.Json(o), q["id"], tenant_id))
        what = f"QuickBooks invoice {o.get('number') or ''} created from {num} (not emailed)".replace("  ", " ")
        if how == "created":
            what += "; a new QuickBooks customer was made for the buyer"
        qr._event(cur, request_id, what, actor.get("label") or "")
        conn.commit()
    finally:
        cur.close(); conn.close()
    return True, (f"Invoice {o.get('number') or ''} created in QuickBooks, not emailed yet. "
                  "Check it there and press Send.").replace("  ", " ")


def refresh_invoice(tenant_id: int, quotation_id: int):
    link = link_for(tenant_id)
    if not link or link["status"] != "active":
        return None
    conn, cur = _db()
    try:
        cur.execute("SELECT id, request_id, qbo_invoice FROM quotations WHERE id=%s AND tenant_id=%s", (quotation_id, tenant_id))
        q = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not q or not (q.get("qbo_invoice") or {}).get("invoice_id"):
        return None
    try:
        inv = _call(link, "GET", f"/invoice/{q['qbo_invoice']['invoice_id']}").get("Invoice") or {}
    except QboError as e:
        if "Object Not Found" in str(e) or "deleted" in str(e).lower():
            inv = {"Id": q["qbo_invoice"]["invoice_id"], "status": "Deleted"}
        else:
            raise
    old = dict(q["qbo_invoice"])
    new = _view(inv, old)
    before, after = state_of(old)["key"], state_of(new)["key"]
    if after == "PAID" and not new.get("paid_at"):
        new["paid_at"] = new["checked_at"]
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET qbo_invoice=%s WHERE id=%s", (psycopg2.extras.Json(new), quotation_id))
        if before != after:
            qr._event(cur, q["request_id"], f"QuickBooks invoice {new.get('number') or ''}: {WORDS[after]}", "QuickBooks")
        conn.commit()
    finally:
        cur.close(); conn.close()
    return new


# ── webhooks ────────────────────────────────────────────────────────────────

def webhook_ok(body: bytes, signature: str) -> bool:
    key = os.getenv("QBO_WEBHOOK_TOKEN", "").strip()
    if not key or not signature:
        return False
    want = base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, signature.strip())


def _events(payload) -> list:
    """[(realmId, invoice id)] from the classic format or the newer CloudEvents list."""
    out = []
    if isinstance(payload, dict):
        for n in payload.get("eventNotifications") or []:
            for e in ((n.get("dataChangeEvent") or {}).get("entities") or []):
                if e.get("name") == "Invoice" and e.get("id"):
                    out.append((str(n.get("realmId")), str(e["id"])))
    elif isinstance(payload, list):
        for e in payload:
            if isinstance(e, dict) and "invoice" in str(e.get("type", "")).lower():
                out.append((str(e.get("intuitaccountid") or e.get("accountId") or ""),
                            str(e.get("intuitentityid") or (e.get("data") or {}).get("id") or "")))
    return [x for x in out if x[0] and x[1]]


def handle_events(payload):
    evs = _events(payload)
    if not evs:
        return
    ensure_tables()
    conn, cur = _db()
    try:
        rows = []
        for realm, inv_id in evs:
            cur.execute("""SELECT q.id, q.tenant_id FROM quotations q JOIN qbo_links l ON l.tenant_id=q.tenant_id
                            WHERE l.realm_id=%s AND q.qbo_invoice->>'invoice_id' = %s""", (realm, inv_id))
            rows += cur.fetchall()
        conn.commit()
    finally:
        cur.close(); conn.close()
    for r in rows:
        try:
            refresh_invoice(r["tenant_id"], r["id"])
        except Exception as e:
            print("⚠️ qbo webhook refresh:", e)


def invoices_for_requests(tenant_id: int, request_ids: list) -> dict:
    if not request_ids:
        return {}
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT DISTINCT ON (request_id) request_id, qbo_invoice FROM quotations
                        WHERE tenant_id=%s AND request_id = ANY(%s) AND status='sent'
                        ORDER BY request_id, version DESC, id DESC""", (tenant_id, list(request_ids)))
        return {r["request_id"]: state_of(r["qbo_invoice"]) for r in cur.fetchall() if r["qbo_invoice"]}
    finally:
        cur.close(); conn.close()
