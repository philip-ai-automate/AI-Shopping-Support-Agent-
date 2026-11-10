"""
sage_link.py — Sage Accounting (online, API v3.1) for accepted quotations (10 Nov 2026).

Approved plan https://claude.ai/artifact/VvXDkSZmCoXASnBdpEpX6v (recommended
decisions): Sage Accounting online only (not Sage 50 desktop) · B2B plans only
· a DRAFT sales invoice that the accountant releases in Sage · the business
picks its sales ledger account and VAT rate once · invoice status checked every
hour and when the deal is opened (Sage webhooks are not relied on) · one
accounts package per business (Xero, QuickBooks or Sage) · businesses with a
shop choose one place · payment chasing stays in Sage · free developer use.

Same shape as xero_link.py / qbo_link.py. Settings in .env: SAGE_CLIENT_ID,
SAGE_CLIENT_SECRET, optional SAGE_REDIRECT_URI.
"""
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

AUTH_URL = "https://www.sageone.com/oauth2/auth/central?filter=apiv3.1"
TOKEN_URL = "https://oauth.accounting.sage.com/token"
REVOKE_URL = "https://oauth.accounting.sage.com/revoke"
API = "https://api.accounting.sage.com/v3.1"
APP_URL = "https://app.accounting.sage.com"
SCOPE = "full_access"
TIMEOUT = 25
REDIRECT_URI = os.getenv("SAGE_REDIRECT_URI", "https://portal.phixtra.com/integrations/sage/callback")
CHECK_EVERY = timedelta(hours=1)

WORDS = {"DRAFT": "Draft in Sage", "UNPAID": "Awaiting payment", "PART_PAID": "Part paid", "PAID": "Paid",
         "OVERDUE": "Overdue", "VOID": "Voided", "DELETED": "Deleted"}


class SageError(Exception):
    """code: reconnect · config · temporary · bad."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def client_id() -> str:
    return os.getenv("SAGE_CLIENT_ID", "").strip()


def _secret() -> str:
    return os.getenv("SAGE_CLIENT_SECRET", "").strip()


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
        cur.execute("""CREATE TABLE IF NOT EXISTS sage_links (
                           tenant_id     INTEGER PRIMARY KEY,
                           business_id   VARCHAR(64),
                           business_name VARCHAR(200),
                           businesses    JSONB,
                           access_enc    TEXT,
                           refresh_enc   TEXT,
                           expires_at    TIMESTAMPTZ,
                           status        VARCHAR(20) NOT NULL DEFAULT 'active',
                           last_error    TEXT,
                           settings      JSONB NOT NULL DEFAULT '{}'::jsonb,
                           connected_by  VARCHAR(200),
                           connected_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("ALTER TABLE quotations ADD COLUMN IF NOT EXISTS sage_invoice JSONB")
        conn.commit()
        _ready = True
    except Exception as e:
        conn.rollback()
        print("⚠️ sage tables:", e)
    finally:
        cur.close(); conn.close()


# ── sign-in ─────────────────────────────────────────────────────────────────

def auth_url(state: str) -> str:
    return AUTH_URL + "&" + urlencode({"response_type": "code", "client_id": client_id(), "redirect_uri": REDIRECT_URI,
                                       "scope": SCOPE, "state": state})


def _token(fields: dict) -> dict:
    fields = dict(fields, client_id=client_id(), client_secret=_secret())
    try:
        r = requests.post(TOKEN_URL, data=fields, timeout=TIMEOUT, headers={"Accept": "application/json"})
    except requests.RequestException:
        raise SageError("temporary", "Sage didn't answer. Please try again in a minute.")
    d = r.json() if "json" in r.headers.get("content-type", "") else {}
    if r.status_code != 200 or "access_token" not in d:
        if d.get("error") == "invalid_grant":
            raise SageError("reconnect", "The Sage sign-in has expired. Please connect Sage again.")
        if d.get("error") in ("invalid_client", "unauthorized_client"):
            raise SageError("config", "PhiXtra's Sage app settings are wrong. Please tell PhiXtra support.")
        raise SageError("temporary", "Sage didn't accept the sign-in just now. Please try again.")
    d["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=max(60, int(d.get("expires_in") or 300) - 30))
    return d


def exchange_code(code: str) -> dict:
    return _token({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})


def save_link(tenant_id: int, tok: dict, by_label: str):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO sage_links (tenant_id, access_enc, refresh_enc, expires_at, status, last_error,
                              connected_by, connected_at, updated_at)
                       VALUES (%s,%s,%s,%s,'active',NULL,%s,NOW(),NOW())
                       ON CONFLICT (tenant_id) DO UPDATE SET access_enc=EXCLUDED.access_enc, refresh_enc=EXCLUDED.refresh_enc,
                              expires_at=EXCLUDED.expires_at, status='active', last_error=NULL,
                              connected_by=EXCLUDED.connected_by, connected_at=NOW(), updated_at=NOW()""",
                    (int(tenant_id), data_crypto.enc(tok["access_token"]), data_crypto.enc(tok.get("refresh_token") or ""),
                     tok["expires_at"], by_label[:200]))
        conn.commit()
    finally:
        cur.close(); conn.close()
    link = link_for(tenant_id)
    bizs = _call(link, "GET", "/businesses", business=False)
    bizs = bizs if isinstance(bizs, list) else (bizs.get("$items") or [])
    first = bizs[0] if bizs else {}
    conn, cur = _db()
    try:
        cur.execute("UPDATE sage_links SET businesses=%s, business_id=%s, business_name=%s WHERE tenant_id=%s",
                    (psycopg2.extras.Json([{"id": b.get("id"), "name": b.get("name") or b.get("displayed_as")} for b in bizs]),
                     first.get("id"), (first.get("name") or first.get("displayed_as") or "")[:200], int(tenant_id)))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return len(bizs)


def link_for(tenant_id: int):
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM sage_links WHERE tenant_id=%s", (int(tenant_id),))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def connected(tenant_id: int) -> bool:
    link = link_for(tenant_id)
    return bool(link and link["status"] != "disconnected" and link.get("business_id"))


def choose_business(tenant_id: int, business_id: str) -> bool:
    link = link_for(tenant_id)
    b = next((b for b in (link or {}).get("businesses") or [] if b.get("id") == business_id), None)
    if not b:
        return False
    conn, cur = _db()
    try:
        cur.execute("UPDATE sage_links SET business_id=%s, business_name=%s, updated_at=NOW() WHERE tenant_id=%s",
                    (b["id"], (b.get("name") or "")[:200], int(tenant_id)))
        conn.commit()
        return True
    finally:
        cur.close(); conn.close()


def save_settings(tenant_id: int, s: dict):
    clean = {"accepted_to": "accounts" if s.get("accepted_to") == "accounts" else "shop"}
    for k in ("ledger_account_id", "tax_rate_id"):
        if s.get(k):
            clean[k] = re.sub(r"[^A-Za-z0-9_\-]", "", str(s[k]))[:64]
    for k in ("ledger_account_name", "tax_rate_name"):
        if s.get(k):
            clean[k] = str(s[k])[:100]
    conn, cur = _db()
    try:
        cur.execute("UPDATE sage_links SET settings = settings || %s, updated_at=NOW() WHERE tenant_id=%s",
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
            requests.post(REVOKE_URL, data={"token": rt, "client_id": client_id(), "client_secret": _secret()}, timeout=TIMEOUT)
    except Exception as e:
        print("⚠️ sage revoke:", e)
    conn, cur = _db()
    try:
        cur.execute("""UPDATE sage_links SET status='disconnected', access_enc=NULL, refresh_enc=NULL, updated_at=NOW()
                        WHERE tenant_id=%s""", (int(tenant_id),))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _mark(tenant_id: int, status: str, err: str = None):
    conn, cur = _db()
    try:
        cur.execute("UPDATE sage_links SET status=%s, last_error=%s, updated_at=NOW() WHERE tenant_id=%s",
                    (status, err, int(tenant_id)))
        conn.commit()
    finally:
        cur.close(); conn.close()


_lock = threading.Lock()


def _access(link: dict) -> str:
    """Sage access tokens last minutes; refresh tokens rotate."""
    if link.get("expires_at") and link["expires_at"] > datetime.now(timezone.utc) + timedelta(seconds=20):
        return data_crypto.dec(link["access_enc"])
    with _lock:
        fresh = link_for(link["tenant_id"])
        if fresh and fresh.get("expires_at") and fresh["expires_at"] > datetime.now(timezone.utc) + timedelta(seconds=20):
            return data_crypto.dec(fresh["access_enc"])
        try:
            tok = _token({"grant_type": "refresh_token", "refresh_token": data_crypto.dec(fresh["refresh_enc"] or "")})
        except SageError as e:
            if e.code == "reconnect":
                _mark(link["tenant_id"], "reconnect", str(e))
            raise
        conn, cur = _db()
        try:
            cur.execute("""UPDATE sage_links SET access_enc=%s, refresh_enc=%s, expires_at=%s, status='active',
                                  last_error=NULL, updated_at=NOW() WHERE tenant_id=%s""",
                        (data_crypto.enc(tok["access_token"]), data_crypto.enc(tok.get("refresh_token") or ""),
                         tok["expires_at"], link["tenant_id"]))
            conn.commit()
        finally:
            cur.close(); conn.close()
        return tok["access_token"]


def _call(link: dict, method: str, path: str, params: dict = None, body: dict = None, business: bool = True):
    headers = {"Authorization": f"Bearer {_access(link)}", "Accept": "application/json"}
    if business and link.get("business_id"):
        headers["X-Business"] = link["business_id"]
    try:
        r = requests.request(method, API + path, params=params, json=body, headers=headers, timeout=TIMEOUT)
    except requests.RequestException:
        raise SageError("temporary", "Sage didn't answer. Please try again in a minute.")
    if r.status_code == 401:
        _mark(link["tenant_id"], "reconnect", "Sage access was removed")
        raise SageError("reconnect", "PhiXtra no longer has access to your Sage. Please connect Sage again.")
    if r.status_code == 429:
        raise SageError("temporary", "Sage is busy. Please try again in a minute.")
    d = r.json() if "json" in r.headers.get("content-type", "") else {}
    if r.status_code == 404:
        raise SageError("gone", "Not found in Sage.")
    if r.status_code >= 400:
        errs = d if isinstance(d, list) else [d]
        msg = "; ".join((e.get("$message") or e.get("message") or "") for e in errs[:2] if isinstance(e, dict)) \
            or f"error {r.status_code}"
        raise SageError("bad" if r.status_code in (400, 422) else "temporary", "Sage didn't accept it: " + msg)
    return d


def _items(d) -> list:
    return d if isinstance(d, list) else ((d or {}).get("$items") or [])


# ── choices for the settings page ───────────────────────────────────────────

def ledger_accounts(link: dict) -> list:
    d = _call(link, "GET", "/ledger_accounts", params={"visible_in": "sales", "items_per_page": 200, "attributes": "nominal_code"})
    return [{"id": a["id"], "name": (f"{a.get('nominal_code')} " if a.get("nominal_code") else "") + (a.get("displayed_as") or "")}
            for a in _items(d)]


def tax_rates(link: dict) -> list:
    d = _call(link, "GET", "/tax_rates", params={"items_per_page": 100})
    return [{"id": t["id"], "name": t.get("displayed_as") or t["id"]} for t in _items(d)]


# ── contacts, products, invoices ────────────────────────────────────────────

def find_or_create_contact(link: dict, req: dict) -> tuple:
    email = (req.get("buyer_email") or "").strip().lower()
    name = (req.get("buyer_company") or req.get("buyer_name") or email or "Buyer").strip()[:100]
    for term in [t for t in (email, name) if t]:
        got = _items(_call(link, "GET", "/contacts", params={"search": term, "contact_type_id": "CUSTOMER",
                                                             "attributes": "email,name"}))
        for c in got:
            if (email and (c.get("email") or "").lower() == email) or (c.get("name") or c.get("displayed_as") or "").lower() == name.lower():
                return c["id"], "existing"
    body = {"contact": {"name": name, "contact_type_ids": ["CUSTOMER"],
                        "main_contact_person": {"name": req.get("buyer_name") or name, "email": email or None,
                                                "telephone": (req.get("buyer_phone") or None)}}}
    body["contact"]["main_contact_person"] = {k: v for k, v in body["contact"]["main_contact_person"].items() if v}
    d = _call(link, "POST", "/contacts", body=body)
    return d["id"], "created"


def _match_product(link: dict, name: str, code: str, cache: dict):
    key = (name or "", code or "")
    if key in cache:
        return cache[key]
    found = None
    for term in [t for t in (code, name) if t]:
        got = _items(_call(link, "GET", "/products", params={"search": term, "attributes": "item_code,description"}))
        found = next((p for p in got if (code and (p.get("item_code") or "") == code)
                      or (p.get("description") or p.get("displayed_as") or "").lower() == (name or "").lower()), None)
        if found:
            break
    cache[key] = found
    return found


def _view(inv: dict, prev: dict = None) -> dict:
    o = dict(prev or {})
    o.update({"invoice_id": inv.get("id"), "number": inv.get("invoice_number") or inv.get("displayed_as") or o.get("number") or "",
              "status": ((inv.get("status") or {}).get("id") if isinstance(inv.get("status"), dict) else inv.get("status")) or o.get("status"),
              "total": inv.get("total_amount"), "outstanding": inv.get("outstanding_amount"),
              "due": (inv.get("due_date") or "")[:10] or None, "currency": ((inv.get("currency") or {}).get("id") if isinstance(inv.get("currency"), dict) else None),
              "url": f"{APP_URL}/invoicing/sales_invoices/{inv.get('id')}" if inv.get("id") else APP_URL,
              "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return o


def state_of(o: dict) -> dict:
    o = dict(o or {})
    st = (o.get("status") or "DRAFT").upper()
    if o.get("deleted"):
        st = "DELETED"
    elif st in ("UNPAID", "PART_PAID") and o.get("due") and o["due"] < date.today().isoformat() and float(o.get("outstanding") or 0) > 0:
        st = "OVERDUE"
    elif st not in WORDS:
        st = "UNPAID"
    o["key"], o["word"], o["paid"] = st, WORDS[st], st == "PAID"
    return o


def _has_shop(tenant_id: int) -> bool:
    try:
        import woo_orders as _wo
        return bool(_wo.has_shop(tenant_id) or _wo._shopify_on(tenant_id))
    except Exception:
        return False


def chosen(tenant_id: int) -> str:
    link = link_for(tenant_id)
    if not link or link["status"] == "disconnected" or not link.get("business_id"):
        return ""
    if not _has_shop(tenant_id):
        return "sage"
    return "sage" if (link.get("settings") or {}).get("accepted_to") == "accounts" else "shop"


def _latest_sent(cur, tenant_id, request_id):
    cur.execute("""SELECT id, number, version, data, currency, total, sent_at, sage_invoice FROM quotations
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
    if q.get("sage_invoice") and q["sage_invoice"].get("invoice_id"):
        o = dict(q["sage_invoice"])
        if refresh and link and link["status"] == "active" and state_of(o)["key"] not in ("PAID", "VOID", "DELETED"):
            try:
                last = datetime.fromisoformat(o["checked_at"]) if o.get("checked_at") else None
                if not last or datetime.now(timezone.utc) - last > timedelta(minutes=10):
                    o = refresh_invoice(tenant_id, q["id"]) or o
            except Exception as e:
                print("⚠️ sage refresh:", e)
        return {"state": "made", "invoice": state_of(o), "qid": q["id"]}
    if chosen(tenant_id) != "sage":
        return {"state": "none"}
    if link["status"] == "reconnect":
        return {"state": "reconnect", "qid": q["id"]}
    st = link.get("settings") or {}
    if not st.get("ledger_account_id") or not st.get("tax_rate_id"):
        return {"state": "setup", "qid": q["id"]}
    return {"state": "ready", "qid": q["id"]}


def _claim(tenant_id: int, qid: int) -> bool:
    """One invoice per quotation: Sage has no idempotency key, so mark the
    quotation 'creating' first; a second press finds it taken."""
    conn, cur = _db()
    try:
        cur.execute("""UPDATE quotations SET sage_invoice='{"creating": true}'::jsonb
                        WHERE id=%s AND tenant_id=%s AND (sage_invoice IS NULL
                              OR (sage_invoice->>'creating' = 'true' AND sage_invoice->>'invoice_id' IS NULL
                                  AND COALESCE(sage_invoice->>'claimed_at','') < %s))""",
                    (qid, tenant_id, (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()))
        ok = cur.rowcount == 1
        if ok:
            cur.execute("UPDATE quotations SET sage_invoice = sage_invoice || %s WHERE id=%s",
                        (psycopg2.extras.Json({"claimed_at": datetime.now(timezone.utc).isoformat()}), qid))
        conn.commit()
        return ok
    finally:
        cur.close(); conn.close()


def _release_claim(qid: int):
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET sage_invoice=NULL WHERE id=%s AND sage_invoice->>'invoice_id' IS NULL", (qid,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def create_invoice(tenant_id: int, request_id: int, actor: dict, po: str = "") -> tuple:
    st = for_request(tenant_id, request_id, refresh=False)
    if st["state"] == "made":
        return True, f"Sage invoice {st['invoice'].get('number') or ''} already exists for this quotation."
    if st["state"] == "reconnect":
        return False, "PhiXtra no longer has access to your Sage. Connect Sage again under Integration."
    if st["state"] == "setup":
        return False, "Pick the sales account and VAT rate first (Integration › Sage)."
    if st["state"] != "ready":
        return False, "This quotation can't be turned into a Sage invoice."
    link = link_for(tenant_id)
    s = link.get("settings") or {}
    req = qr.get(tenant_id, request_id)
    conn, cur = _db()
    try:
        q = _latest_sent(cur, tenant_id, request_id)
    finally:
        cur.close(); conn.close()
    if not _claim(tenant_id, q["id"]):
        return False, "The invoice is already being made. Refresh the page in a moment."
    d = q.get("data") or {}
    num = q["number"] + (f" v{q['version']}" if (q.get("version") or 1) > 1 else "")
    po = (po or (req.get("ai_answer") or {}).get("po") or "").strip()
    try:
        contact_id, how = find_or_create_contact(link, req)
        cache, lines = {}, []
        for ln in d.get("lines") or []:
            if ln.get("price") is None or not ln.get("qty"):
                continue
            disc = float(ln.get("disc") or 0)
            unit = round(float(ln["price"]) * (1 - disc / 100), 4)
            p = _match_product(link, ln.get("name") or "", ln.get("code") or "", cache)
            line = {"description": ((ln.get("name") or "Item") + (f" ({ln['code']})" if ln.get("code") else "")
                                    + (f" — {('%g' % disc)}% discount" if disc else ""))[:500],
                    "quantity": float(ln["qty"]), "unit_price": unit,
                    "ledger_account_id": s["ledger_account_id"], "tax_rate_id": s["tax_rate_id"]}
            if p:
                line["product_id"] = p["id"]
            lines.append(line)
        for c in d.get("charges") or []:
            amt = float(c.get("price") or 0)
            if amt:
                lines.append({"description": (c.get("name") or "Charge")[:500], "quantity": float(c.get("qty") or 1),
                              "unit_price": amt, "ledger_account_id": s["ledger_account_id"], "tax_rate_id": s["tax_rate_id"]})
        if not lines:
            _release_claim(q["id"])
            return False, "The quotation has no priced lines to put on an invoice."
        body = {"sales_invoice": {"contact_id": contact_id, "date": date.today().isoformat(), "status_id": "DRAFT",
                                  "reference": (f"{num} · PO {po}" if po else num)[:25],
                                  "notes": f"From PhiXtra quotation {num}" + (f", buyer PO {po}" if po else ""),
                                  "invoice_lines": lines}}
        inv = _call(link, "POST", "/sales_invoices", body=body)
    except SageError as e:
        _release_claim(q["id"])
        return False, str(e) + " Nothing was created."
    except Exception:
        _release_claim(q["id"])
        raise
    o = _view(inv, {"contact": how, "quote_total": float(q.get("total") or 0),
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": actor.get("label") or ""})
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET sage_invoice=%s WHERE id=%s AND tenant_id=%s", (psycopg2.extras.Json(o), q["id"], tenant_id))
        what = f"Sage draft invoice {o.get('number') or ''} created from {num}".replace("  ", " ")
        if how == "created":
            what += "; a new Sage contact was made for the buyer"
        qr._event(cur, request_id, what, actor.get("label") or "")
        conn.commit()
    finally:
        cur.close(); conn.close()
    return True, f"Draft invoice {o.get('number') or ''} created in Sage. Check it there and release it to send.".replace("  ", " ")


def refresh_invoice(tenant_id: int, quotation_id: int):
    link = link_for(tenant_id)
    if not link or link["status"] != "active":
        return None
    conn, cur = _db()
    try:
        cur.execute("SELECT id, request_id, sage_invoice FROM quotations WHERE id=%s AND tenant_id=%s", (quotation_id, tenant_id))
        q = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not q or not (q.get("sage_invoice") or {}).get("invoice_id"):
        return None
    old = dict(q["sage_invoice"])
    try:
        inv = _call(link, "GET", f"/sales_invoices/{old['invoice_id']}")
        new = _view(inv, old)
    except SageError as e:
        if e.code != "gone":
            raise
        new = dict(old, deleted=True, checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    before, after = state_of(old)["key"], state_of(new)["key"]
    if after == "PAID" and not new.get("paid_at"):
        new["paid_at"] = new["checked_at"]
    conn, cur = _db()
    try:
        cur.execute("UPDATE quotations SET sage_invoice=%s WHERE id=%s", (psycopg2.extras.Json(new), quotation_id))
        if before != after:
            qr._event(cur, q["request_id"], f"Sage invoice {new.get('number') or ''}: {WORDS[after]}", "Sage")
        conn.commit()
    finally:
        cur.close(); conn.close()
    return new


def invoices_for_requests(tenant_id: int, request_ids: list) -> dict:
    if not request_ids:
        return {}
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("""SELECT DISTINCT ON (request_id) request_id, sage_invoice FROM quotations
                        WHERE tenant_id=%s AND request_id = ANY(%s) AND status='sent'
                        ORDER BY request_id, version DESC, id DESC""", (tenant_id, list(request_ids)))
        return {r["request_id"]: state_of(r["sage_invoice"]) for r in cur.fetchall()
                if r["sage_invoice"] and r["sage_invoice"].get("invoice_id")}
    finally:
        cur.close(); conn.close()


# ── hourly check (no webhooks relied on) ────────────────────────────────────

def check_open_invoices() -> int:
    """Re-read every open Sage invoice not checked in the last hour. Only one
    portal worker does it at a time (advisory lock)."""
    ensure_tables()
    conn, cur = _db()
    try:
        cur.execute("SELECT pg_try_advisory_lock(hashtext('phixtra_sage_checker')) AS ok")
        if not cur.fetchone()["ok"]:
            conn.rollback()
            return 0
        cur.execute("""SELECT q.id, q.tenant_id FROM quotations q JOIN sage_links l ON l.tenant_id=q.tenant_id AND l.status='active'
                        WHERE q.sage_invoice->>'invoice_id' IS NOT NULL
                          AND COALESCE(q.sage_invoice->>'status','') NOT IN ('PAID','VOID')
                          AND COALESCE(q.sage_invoice->>'deleted','') <> 'true'
                          AND COALESCE(q.sage_invoice->>'checked_at','') < %s
                        LIMIT 500""", ((datetime.now(timezone.utc) - CHECK_EVERY).isoformat(),))
        rows = cur.fetchall()
        n = 0
        for r in rows:
            try:
                refresh_invoice(r["tenant_id"], r["id"])
                n += 1
            except Exception as e:
                print("⚠️ sage hourly check:", e)
        cur.execute("SELECT pg_advisory_unlock(hashtext('phixtra_sage_checker'))")
        conn.commit()
        return n
    finally:
        cur.close(); conn.close()


def _loop():
    time.sleep(90)
    while True:
        try:
            if configured():
                check_open_invoices()
        except Exception as e:
            print("⚠️ sage checker:", e)
        time.sleep(600)


def start_checker():
    if not getattr(threading, "_phixtra_sage_checker_started", False):
        threading._phixtra_sage_checker_started = True  # type: ignore[attr-defined]
        threading.Thread(target=_loop, daemon=True).start()
