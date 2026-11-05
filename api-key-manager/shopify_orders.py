"""
shopify_orders.py — PhiXtra for Shopify, Part 6: a won quotation becomes a
Shopify draft order (5 Nov 2026; plan https://claude.ai/artifact/AKemVHnEmQPhCPutkmrJcy,
all six recommendations approved).

- Shopify businesses only: the order goes to Shopify, never also to
  Products & Orders.
- Recognised trade buyer -> on their Shopify company, site and contact.
  Anyone else -> to their email (or phone) as a normal Shopify customer.
- Quoted prices are fixed on each line (priceOverride); Shopify works out tax
  and PhiXtra warns when it differs from the quotation.
- The invoice is a separate button. Stock isn't reserved. PhiXtra follows the
  draft until it is completed (draft order webhooks; no order access).
"""
import json
import re
from datetime import datetime, timezone

import psycopg2.extras

import quote_quotes as qq
import quote_requests as qr
from db import get_db_connection

DELIVERY_WORDS = re.compile(r"deliver|shipping|freight|carriage|postage|courier|transport", re.I)

_CALC = """mutation Calc($input: DraftOrderInput!) { draftOrderCalculate(input: $input) {
  calculatedDraftOrder { presentmentCurrencyCode
    subtotalPriceSet { presentmentMoney { amount currencyCode } }
    totalShippingPriceSet { presentmentMoney { amount } }
    totalTaxSet { presentmentMoney { amount } }
    totalPriceSet { presentmentMoney { amount } }
    warnings { message } }
  userErrors { field message } } }"""

_CREATE = """mutation Create($input: DraftOrderInput!) { draftOrderCreate(input: $input) {
  draftOrder { id legacyResourceId name status invoiceUrl presentmentCurrencyCode
    totalPriceSet { presentmentMoney { amount currencyCode } } }
  userErrors { field message } } }"""

_SEND = """mutation Send($id: ID!) { draftOrderInvoiceSend(id: $id) {
  draftOrder { id status invoiceSentAt } userErrors { field message } } }"""

_TERMS = """query Terms($id: ID!) { companyLocation(id: $id) { id
  buyerExperienceConfiguration { paymentTermsTemplate { id name paymentTermsType dueInDays } } } }"""

_VARIANTS = """query Vars($ids: [ID!]!) { nodes(ids: $ids) { ... on Product {
  id title variants(first: 25) { nodes { id title sku } } } } }"""

STATUS_WORDS = {"open": "Open", "invoice_sent": "Invoice sent", "completed": "Completed", "deleted": "Deleted in Shopify"}

_cols_ok = False


def ensure_columns():
    global _cols_ok
    if _cols_ok:
        return
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""SELECT 1 FROM information_schema.columns
                        WHERE table_name='quotations' AND column_name='shopify_draft_id'""")
        if not cur.fetchone():
            cur.execute("SET lock_timeout = '5s'")
            cur.execute("ALTER TABLE quotations ADD COLUMN IF NOT EXISTS shopify_draft_id TEXT")
            cur.execute("ALTER TABLE quotations ADD COLUMN IF NOT EXISTS shopify_draft JSONB")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_quotations_shopify_draft ON quotations (shopify_draft_id)")
            conn.commit()
        _cols_ok = True
    finally:
        cur.close(); conn.close()


def shop_for(tenant_id: int):
    """The business's linked Shopify store row, or None."""
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT * FROM shopify_shops WHERE tenant_id=%s AND uninstalled_at IS NULL
                        AND access_token IS NOT NULL ORDER BY updated_at DESC NULLS LAST LIMIT 1""", (int(tenant_id),))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def is_on(tenant_id: int) -> bool:
    return shop_for(tenant_id) is not None


def _store(shop: str) -> str:
    return shop.replace(".myshopify.com", "")


def admin_url(shop: str, draft_id: str) -> str:
    return f"https://admin.shopify.com/store/{_store(shop)}/draft_orders/{draft_id}"


def order_url(shop: str, order_id: str) -> str:
    return f"https://admin.shopify.com/store/{_store(shop)}/orders/{order_id}"


def _money(v) -> str:
    return "%.2f" % round(float(v or 0), 2)


def _link(tenant_id: int, lead_id):
    """The Shopify company / site / contact the lead belongs to, or None."""
    if not lead_id:
        return None
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT k.*, co.name AS company FROM shopify_b2b_lead_links k
                         LEFT JOIN shopify_b2b_companies co ON co.shop=k.shop AND co.company_id=k.company_id
                        WHERE k.lead_id=%s AND k.tenant_id=%s""", (int(lead_id), int(tenant_id)))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def _no_access(e) -> bool:
    t = str(e).lower()
    return "access denied" in t or "write_draft_orders" in t or "access_denied" in t


NEED_PERMISSION = ("Shopify hasn't given PhiXtra permission to create draft orders yet. "
                   "Open PhiXtra in your Shopify admin (Apps › PhiXtra) and approve the new permission, then try again.")


def latest_sent(tenant_id: int, request_id: int):
    ensure_columns()
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT * FROM quotations WHERE tenant_id=%s AND request_id=%s AND status='sent'
                        ORDER BY version DESC LIMIT 1""", (tenant_id, request_id))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


# ── building the draft order ───────────────────────────────────────────────

def _site_variants(tenant_id, site_loc, pids):
    import shopify_prices as sp
    return sp.site_prices(tenant_id, site_loc, pids)


def _plain_variants(shop, pids):
    """{product_id: [variants]} read live (buyers who aren't a Shopify company)."""
    import shopify_app as sa
    import shopify_prices as sp
    ids = [p for p in dict.fromkeys(pids) if sp.is_shopify_product(p)]
    out = {}
    for i in range(0, len(ids), 10):
        chunk = ids[i:i + 10]
        data = sa.graphql(shop, _VARIANTS, {"ids": [f"gid://shopify/Product/{p.split('-', 1)[1]}" for p in chunk]})
        for pid, node in zip(chunk, data.get("nodes") or []):
            if node:
                out[pid] = [{"id": str(v["id"]).rsplit("/", 1)[-1], "title": v.get("title") or "", "sku": v.get("sku") or ""}
                            for v in (node.get("variants") or {}).get("nodes") or []]
    return out


def _terms(shop, location_id):
    """The site's payment terms as a PaymentTermsInput, plus its name."""
    import shopify_app as sa
    d = sa.graphql(shop, _TERMS, {"id": f"gid://shopify/CompanyLocation/{location_id}"})
    t = (((d.get("companyLocation") or {}).get("buyerExperienceConfiguration") or {}).get("paymentTermsTemplate"))
    if not t:
        return None, None
    kind = t.get("paymentTermsType")
    if kind == "NET":
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"paymentTermsTemplateId": t["id"], "paymentSchedules": [{"issuedAt": now}]}, t.get("name")
    if kind in ("RECEIPT", "FULFILLMENT"):
        return {"paymentTermsTemplateId": t["id"]}, t.get("name")
    return None, t.get("name")       # FIXED needs a due date: written in the note instead


def build(tenant_id: int, request_id: int, po_number: str = ""):
    """Everything needed to create the draft order.
    Returns {"input", "blocks", "warns", "buyer", "quote", "doc", "shop"}."""
    shop_row = shop_for(tenant_id)
    if not shop_row:
        return {"blocks": ["This business isn't linked to a Shopify store."], "warns": []}
    shop = shop_row["shop"]
    req = qr.get(tenant_id, request_id)
    if not req or req["status"] != "won":
        return {"blocks": ["Mark the request Won first."], "warns": []}
    q = latest_sent(tenant_id, request_id)
    if not q:
        return {"blocks": ["There's no quotation made in PhiXtra on this request."], "warns": []}
    d = q["data"]
    doc = qq.to_doc(tenant_id, q, qq.qd.get(tenant_id), req)
    cur_code = (q.get("currency") or doc.get("currency") or "").upper() or None
    blocks, warns, notes = [], [], [f"From PhiXtra quotation {qq.full_number(q)} (request R-{req['ref_no']})"]
    inp = {"tags": ["PhiXtra", qq.full_number(q).replace(" ", "-")]}
    if cur_code:
        inp["presentmentCurrencyCode"] = cur_code

    # The buyer.
    link = _link(tenant_id, req.get("lead_id"))
    if link and link["shop"] == shop:
        site_id = (d.get("shopify_site") or {}).get("location_id") or link.get("location_id")
        if not site_id:
            blocks.append(f"Choose which {link['company'] or 'company'} site this order is for on the request page first.")
        elif not link.get("contact_id"):
            blocks.append("PhiXtra doesn't know which Shopify contact this buyer is. Check the buyer in Shopify › Companies.")
        else:
            inp["purchasingEntity"] = {"purchasingCompany": {
                "companyId": f"gid://shopify/Company/{link['company_id']}",
                "companyLocationId": f"gid://shopify/CompanyLocation/{site_id}",
                "companyContactId": f"gid://shopify/CompanyContact/{link['contact_id']}"}}
        site_name = (d.get("shopify_site") or {}).get("name") or ""
        buyer = {"kind": "company", "company": link["company"], "site": site_name, "location_id": site_id}
    else:
        email = (req.get("buyer_email") or "").strip()
        phone = re.sub(r"[^\d+]", "", req.get("buyer_phone") or "")
        if email:
            inp["email"] = email
        if phone:
            inp["phone"] = phone if phone.startswith("+") else "+" + phone
        if not email and not phone:
            blocks.append("Add the buyer's email address on the request first, so Shopify knows who the order is for.")
        if not email:
            warns.append("No email for this buyer: Shopify can't send them the invoice. Add one on the request, "
                         "or share the invoice link yourself.")
        buyer = {"kind": "customer", "email": email, "phone": phone, "site": "", "location_id": None}
    buyer["name"] = " · ".join(x for x in (req.get("buyer_company"), req.get("buyer_name")) if x)

    # Live variants (and, for trade buyers, the site's quantity rules).
    pids = [ln.get("product_id") for ln in d["lines"]]
    site_info, plain = {}, {}
    try:
        if buyer["kind"] == "company" and buyer.get("location_id"):
            site_info = _site_variants(tenant_id, buyer["location_id"], pids)
        else:
            plain = _plain_variants(shop, pids)
    except Exception as e:
        if _no_access(e):
            return {"blocks": [NEED_PERMISSION], "warns": []}
        blocks.append("Shopify couldn't be reached just now. Please try again in a minute.")
        print(f"⚠️ shopify order variants {shop}: {e}")

    import shopify_prices as sp
    lines, custom_names, rules = [], [], []
    for src, ln in zip(d["lines"], doc["lines"]):
        qty = float(ln["qty"] or 0)
        unit = round(float(ln["price"] or 0) * (1 - float(ln.get("disc") or 0) / 100), 2)
        pid = src.get("product_id")
        variant = None
        if sp.is_shopify_product(pid):
            if buyer["kind"] == "company":
                info = site_info.get(pid)
                vs = sp.real_variants(info) if info else []
                variant = next((v for v in vs if v["id"] == src.get("variant_id")), None)
                if variant:
                    probs = sp.rule_problems(variant, qty)
                    if probs:
                        rules.append(f"{ln['name']}: " + ", ".join(probs))
                elif site_info:
                    blocks.append(f"{ln['name']}: this variant isn't in Shopify any more. Make a new version of the quotation.")
            else:
                vs = plain.get(pid) or []
                variant = (next((v for v in vs if v["id"] == src.get("variant_id")), None)
                           or (vs[0] if len(vs) == 1 else None)
                           or next((v for v in vs if v.get("sku") and v["sku"] == (src.get("code") or "")), None))
                if not variant:
                    custom_names.append(ln["name"])
        if variant and qty == int(qty) and qty > 0:
            lines.append({"variantId": f"gid://shopify/ProductVariant/{variant['id']}", "quantity": int(qty),
                          "priceOverride": {"amount": _money(unit), "currencyCode": cur_code}})
        else:
            u = (ln.get("unit") or "").strip()
            if qty == int(qty) and qty > 0:
                title = ln["name"] + (f" ({u})" if u and u.lower() not in ln["name"].lower() else "")
                item = {"title": title[:255], "quantity": int(qty),
                        "originalUnitPriceWithCurrency": {"amount": _money(unit), "currencyCode": cur_code}}
            else:       # Shopify takes whole quantities: 2.5 hours goes as one line "…, 2.5 hours"
                item = {"title": f"{ln['name']}, {qty:g} {u}".strip()[:255], "quantity": 1,
                        "originalUnitPriceWithCurrency": {"amount": _money(ln["amount"]), "currencyCode": cur_code}}
            if ln.get("code"):
                item["sku"] = ln["code"][:255]
            lines.append(item)
    if rules:
        blocks.append("Quantities Shopify would refuse for this site: " + "; ".join(rules)
                      + ". Make a new version of the quotation with allowed quantities.")
    if custom_names:
        warns.append("Goes in as a typed line, not linked to the Shopify product (stock not counted): "
                     + ", ".join(custom_names) + ". Choose the variant on the quotation to link it.")

    # Charges: delivery → Shopify's one shipping line, the rest as custom lines.
    ship_total, ship_names = 0.0, []
    for c in doc["charges"]:
        if DELIVERY_WORDS.search(c.get("name") or ""):
            ship_total += float(c["amount"] or 0); ship_names.append(c["name"])
        else:
            cq = float(c.get("qty") or 1)
            whole = cq == int(cq) and cq > 0
            lines.append({"title": (c["name"] or "Charge")[:255], "quantity": int(cq) if whole else 1,
                          "originalUnitPriceWithCurrency": {"amount": _money(c["price"] if whole else c["amount"]),
                                                            "currencyCode": cur_code}})
    if ship_names:
        inp["shippingLine"] = {"title": (" + ".join(ship_names))[:255],
                               "priceWithCurrency": {"amount": _money(ship_total), "currencyCode": cur_code}}
    inp["lineItems"] = lines

    # Payment terms: the site's own; otherwise the quotation's, in the note.
    terms_name = None
    if buyer["kind"] == "company" and buyer.get("location_id") and not blocks:
        try:
            terms, terms_name = _terms(shop, buyer["location_id"])
            if terms:
                inp["paymentTerms"] = terms
            elif terms_name:
                notes.append(f"Payment terms: {terms_name}")
        except Exception as e:
            if _no_access(e):
                return {"blocks": [NEED_PERMISSION], "warns": []}
            print(f"⚠️ shopify order terms {shop}: {e}")
    if not terms_name and d.get("payment_terms"):
        notes.append(f"Payment terms on the quotation: {d['payment_terms']}")
    if doc.get("deposit"):
        notes.append(f"Deposit asked: {qr.money(doc['deposit'], doc['currency'])}")
    if doc["deliver"].get("place") or doc["deliver"].get("terms"):
        notes.append("Delivery: " + " · ".join(x for x in (doc["deliver"]["place"], doc["deliver"]["terms"]) if x))
    po = (po_number or "").strip()[:100]
    if po:
        inp["poNumber"] = po
    inp["note"] = "\n".join(notes)[:5000]

    if doc.get("valid_until") and doc["valid_until"] < datetime.now().date():
        warns.append(f"The quotation's valid-until date ({doc['valid_until'].strftime('%d %b %Y')}) has passed.")
    return {"input": inp, "blocks": blocks, "warns": warns, "buyer": buyer, "quote": q, "doc": doc,
            "shop": shop, "terms_name": terms_name}


def _errs(payload) -> list:
    return [e.get("message") for e in (payload or {}).get("userErrors") or [] if e.get("message")]


def preview(tenant_id: int, request_id: int) -> dict:
    """Shopify's totals for the order next to the quotation's (nothing saved)."""
    b = build(tenant_id, request_id)
    out = {"blocks": list(b.get("blocks") or []), "warns": list(b.get("warns") or []), "buyer": b.get("buyer"),
           "terms_name": b.get("terms_name"), "rows": []}
    if out["blocks"]:
        return out
    import shopify_app as sa
    try:
        r = sa.graphql(b["shop"], _CALC, {"input": b["input"]})["draftOrderCalculate"]
    except Exception as e:
        out["blocks"].append(NEED_PERMISSION if _no_access(e) else "Shopify couldn't work out the totals just now. Please try again in a minute.")
        print(f"⚠️ shopify order calc {b['shop']}: {e}")
        return out
    errs = _errs(r)
    if errs:
        out["blocks"].append("Shopify said: " + "; ".join(errs))
        return out
    c = r.get("calculatedDraftOrder") or {}
    doc = b["doc"]
    cur = c.get("presentmentCurrencyCode") or doc["currency"]

    def amt(k):
        return float(((c.get(k) or {}).get("presentmentMoney") or {}).get("amount") or 0)
    ship_q = float((b["input"].get("shippingLine") or {}).get("priceWithCurrency", {}).get("amount") or 0)
    items_q = round(float(doc["total"]) - float(doc.get("tax_amount") or 0) - ship_q, 2)
    rows = [("Items and charges", items_q, amt("subtotalPriceSet")),
            ("Delivery", ship_q, amt("totalShippingPriceSet")),
            ("Tax", float(doc.get("tax_amount") or 0), amt("totalTaxSet")),
            ("Total", float(doc["total"]), amt("totalPriceSet"))]
    out["rows"] = [{"label": l, "quote": qr.money(a, doc["currency"]), "shopify": qr.money(s, cur),
                    "differs": abs(a - s) > 0.009} for l, a, s in rows]
    if cur and doc["currency"] and cur.upper() != doc["currency"].upper():
        out["warns"].append(f"Shopify will charge in {cur}, the quotation is in {doc['currency']}.")
    if out["rows"][2]["differs"]:
        out["warns"].append("Shopify works out tax from the delivery address and tax settings, so it differs from the quotation.")
    elif out["rows"][3]["differs"]:
        out["warns"].append("Shopify's total differs from the quotation.")
    for w in c.get("warnings") or []:
        if w.get("message"):
            out["warns"].append("Shopify: " + w["message"])
    return out


def create(tenant_id: int, request_id: int, actor: dict, po_number: str = "") -> tuple:
    """(ok, message). One draft order per quotation: a second click does nothing new."""
    ensure_columns()
    q0 = latest_sent(tenant_id, request_id)
    if q0 and q0.get("shopify_draft_id"):
        return True, ""
    b = build(tenant_id, request_id, po_number)
    if b.get("blocks"):
        return False, " ".join(b["blocks"])
    q = b["quote"]
    import shopify_app as sa
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT shopify_draft_id FROM quotations WHERE id=%s FOR UPDATE", (q["id"],))
        row = cur.fetchone()
        if row and row["shopify_draft_id"]:
            conn.commit()
            return True, ""
        try:
            r = sa.graphql(b["shop"], _CREATE, {"input": b["input"]})["draftOrderCreate"]
        except Exception as e:
            conn.rollback()
            print(f"⚠️ shopify order create {b['shop']}: {e}")
            return False, NEED_PERMISSION if _no_access(e) else "Shopify couldn't be reached just now. Please try again in a minute."
        errs = _errs(r)
        terms_note = ""
        if errs and b["input"].get("paymentTerms") and any("payment terms" in e.lower() for e in errs):
            # The store hasn't given PhiXtra the payment terms permission yet:
            # make the draft without them and say so (the site's terms go in the note).
            inp = dict(b["input"]); inp.pop("paymentTerms", None)
            inp["note"] = (inp.get("note", "") + f"\nPayment terms: {b.get('terms_name') or 'see the site'} (set them on this draft in Shopify)")[:5000]
            try:
                r = sa.graphql(b["shop"], _CREATE, {"input": inp})["draftOrderCreate"]
            except Exception as e:
                conn.rollback()
                print(f"⚠️ shopify order create (no terms) {b['shop']}: {e}")
                return False, "Shopify couldn't be reached just now. Please try again in a minute."
            errs = _errs(r)
            terms_note = (f" Shopify didn't let PhiXtra set the payment terms ({b.get('terms_name')}): set them on the draft "
                          "in Shopify, or approve PhiXtra's payment terms permission (Apps › PhiXtra) for next time.")
        dr = r.get("draftOrder")
        if errs or not dr:
            conn.rollback()
            return False, "Shopify said: " + ("; ".join(errs) or "the draft order wasn't created.")
        total = ((dr.get("totalPriceSet") or {}).get("presentmentMoney") or {})
        draft = {"id": str(dr["legacyResourceId"]), "gid": dr["id"], "name": dr["name"],
                 "status": (dr.get("status") or "OPEN").lower(), "invoice_url": dr.get("invoiceUrl"),
                 "total": total.get("amount"), "currency": total.get("currencyCode"),
                 "po": b["input"].get("poNumber") or "", "shop": b["shop"], "terms_set": not terms_note,
                 "buyer": b["buyer"], "created_by": actor.get("label"),
                 "created_at": datetime.now(timezone.utc).isoformat()}
        cur.execute("UPDATE quotations SET shopify_draft_id=%s, shopify_draft=%s, updated_at=NOW() WHERE id=%s",
                    (draft["id"], psycopg2.extras.Json(draft), q["id"]))
        who = (f"{b['buyer']['company']} ({b['buyer']['site']})" if b["buyer"]["kind"] == "company"
               else (b["buyer"].get("email") or b["buyer"].get("phone")))
        qr._event(cur, request_id, f"Shopify draft order {dr['name']} created from {qq.full_number(q)} for {who}",
                  actor["label"])
        conn.commit()
        return True, f"Shopify draft order {dr['name']} created from {qq.full_number(q)}." + terms_note
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


def send_invoice(tenant_id: int, request_id: int, actor: dict) -> tuple:
    q = latest_sent(tenant_id, request_id)
    dr = (q or {}).get("shopify_draft") or {}
    if not dr.get("gid"):
        return False, "Create the Shopify draft order first."
    if dr.get("status") in ("completed", "deleted"):
        return False, "This draft order is already " + STATUS_WORDS[dr["status"]].lower() + "."
    import shopify_app as sa
    try:
        r = sa.graphql(dr["shop"], _SEND, {"id": dr["gid"]})["draftOrderInvoiceSend"]
    except Exception as e:
        print(f"⚠️ shopify invoice send {dr.get('shop')}: {e}")
        return False, NEED_PERMISSION if _no_access(e) else "Shopify couldn't be reached just now. Please try again in a minute."
    errs = _errs(r)
    if errs:
        return False, "Shopify said: " + "; ".join(errs)
    dr["status"] = ((r.get("draftOrder") or {}).get("status") or "INVOICE_SENT").lower()
    dr["invoice_sent_at"] = (r.get("draftOrder") or {}).get("invoiceSentAt")
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE quotations SET shopify_draft=%s, updated_at=NOW() WHERE id=%s",
                    (psycopg2.extras.Json(dr), q["id"]))
        qr._event(cur, request_id, f"Shopify invoice for {dr['name']} sent to the buyer", actor["label"])
        conn.commit()
    finally:
        cur.close(); conn.close()
    return True, f"Shopify sent the invoice for {dr['name']} to the buyer."


def webhook(shop: str, topic: str, payload: dict):
    """draft_orders/update | draft_orders/delete: keep the request page current."""
    ensure_columns()
    did = str(payload.get("id") or "")
    if not did:
        return
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT id, request_id, shopify_draft FROM quotations WHERE shopify_draft_id=%s
                        AND shopify_draft->>'shop'=%s FOR UPDATE""", (did, shop))
        row = cur.fetchone()
        if not row:
            conn.commit()
            return
        dr = dict(row["shopify_draft"] or {})
        old = dr.get("status")
        if topic == "draft_orders/delete":
            dr["status"] = "deleted"
            cur.execute("UPDATE quotations SET shopify_draft_id=NULL, shopify_draft=%s, updated_at=NOW() WHERE id=%s",
                        (psycopg2.extras.Json(dr), row["id"]))
            qr._event(cur, row["request_id"], f"Shopify draft order {dr.get('name')} was deleted in Shopify", "Shopify")
        else:
            st = (payload.get("status") or old or "open").lower()
            dr["status"] = st
            if payload.get("total_price") is not None:
                dr["total"] = payload.get("total_price")
            if payload.get("currency"):
                dr["currency"] = payload["currency"]
            if payload.get("order_id"):
                dr["order_id"] = str(payload["order_id"])
            if payload.get("invoice_sent_at"):
                dr["invoice_sent_at"] = payload["invoice_sent_at"]
            cur.execute("UPDATE quotations SET shopify_draft=%s, updated_at=NOW() WHERE id=%s",
                        (psycopg2.extras.Json(dr), row["id"]))
            if st != old:
                what = {"invoice_sent": f"Shopify invoice for {dr.get('name')} sent",
                        "completed": f"Shopify draft order {dr.get('name')} completed: the order is placed"}.get(st)
                if what:
                    qr._event(cur, row["request_id"], what, "Shopify")
        conn.commit()
    finally:
        cur.close(); conn.close()


def state(q) -> dict:
    """The request page's Shopify order box, or {} when none was made."""
    dr = (q or {}).get("shopify_draft") or {}
    if not dr:
        return {}
    out = dict(dr)
    out["status_words"] = STATUS_WORDS.get(dr.get("status"), (dr.get("status") or "").replace("_", " ").capitalize())
    if dr.get("status") != "deleted":
        out["admin_url"] = admin_url(dr["shop"], dr["id"])
    if dr.get("order_id"):
        out["order_url"] = order_url(dr["shop"], dr["order_id"])
    return out
