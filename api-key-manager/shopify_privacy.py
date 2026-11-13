"""
shopify_privacy.py — what Shopify's privacy webhooks remove (12 Nov 2026).

shop/redact (48 h after uninstall) — on top of products (shopify_sync.forget_shop)
and B2B companies/contacts (shopify_b2b.forget):
  - discount codes made in that store (chat_discount_codes, platform shopify)
  - the buyer's details on quotations sent to that store as draft orders
    (the quotation itself stays in the business's PhiXtra account)
  - saved webhook payloads for that store
  Chats stay with the business's PhiXtra account (deleted by hand, see below).

customers/redact — that one buyer at that store:
  - B2B contact (shopify_b2b.forget_customer)
  - email on discount codes, buyer email/phone on draft-order records
  - name/email/phone/company on chat visitor details + hand-over requests
  - saved webhook payloads for that customer
  Chats themselves are deleted BY HAND (user decision 12 Nov 2026): support@
  gets an email listing the chats to delete.

customers/data_request — support@ gets an email with what PhiXtra holds and
the date it must be answered by (Shopify gives 30 days).
"""
import json
from datetime import datetime, timedelta, timezone

import psycopg2.extras

from db import get_db_connection

ALERT_TO = "support@phixtra.com"
# Payloads we keep in shopify_webhook_log (others are saved as {}).
KEEP_PAYLOAD = ("customers/data_request", "customers/redact", "shop/redact")


def _tenant(cur, shop):
    cur.execute("SELECT tenant_id FROM shopify_shops WHERE shop=%s", (shop,))
    r = cur.fetchone()
    return (r[0] if not isinstance(r, dict) else r["tenant_id"]) if r else None


def _norm_phone(p):
    d = "".join(ch for ch in str(p or "") if ch.isdigit())
    return d[-9:] if len(d) >= 7 else ""


def _who(payload):
    """customer id, emails, phones from a customers/* payload."""
    c = payload.get("customer") or {}
    cid = str(c.get("id") or payload.get("id") or "")
    emails = {str(e).strip().lower() for e in (c.get("email"), payload.get("email")) if e and "@" in str(e)}
    phones = {_norm_phone(p) for p in (c.get("phone"), payload.get("phone"))} - {""}
    return cid, emails, phones


def _matching_sessions(cur, tenant_id, emails, phones):
    """Chat sessions of this business where the buyer gave that email/phone
    (details are encrypted, so each one is opened)."""
    import data_crypto
    found = set()
    if not tenant_id or not (emails or phones):
        return found
    cur.execute("SELECT session_id, email, phone FROM chat_visitors WHERE tenant_id=%s", (tenant_id,))
    for sid, e, p in cur.fetchall():
        if (data_crypto.dec(e) or "").strip().lower() in emails or (_norm_phone(data_crypto.dec(p)) in phones):
            found.add(sid)
    cur.execute("""SELECT session_id, visitor_email FROM handoff_requests
                    WHERE tenant_id=%s AND COALESCE(visitor_email,'') <> ''""", (tenant_id,))
    for sid, e in cur.fetchall():
        if sid and (data_crypto.dec(e) or "").strip().lower() in emails:
            found.add(sid)
    cur.execute("""SELECT session_id FROM chat_discount_codes
                    WHERE tenant_id=%s AND lower(COALESCE(buyer_email,'')) = ANY(%s)""",
                (tenant_id, list(emails)))
    found |= {r[0] for r in cur.fetchall() if r[0]}
    # Website chats only ('wa-meta-…' is the AI memory of a WhatsApp chat).
    return {x for x in found if not x.startswith("wa-meta-")}


def _draft_rows(cur, tenant_id, shop):
    cur.execute("""SELECT id, shopify_draft FROM quotations
                    WHERE tenant_id=%s AND shopify_draft IS NOT NULL AND shopify_draft->>'shop'=%s""",
                (tenant_id, shop))
    return cur.fetchall()


def _strip_buyer(d):
    d = dict(d or {})
    b = dict(d.get("buyer") or {})
    for k in ("email", "phone", "name", "contact"):
        b.pop(k, None)
    d["buyer"] = b
    d.pop("invoice_url", None)
    d["redacted_at"] = datetime.now(timezone.utc).isoformat()
    return d


def forget_shop(shop: str) -> dict:
    """shop/redact extras. Call BEFORE the shopify_shops row is deleted."""
    conn = get_db_connection(); cur = conn.cursor()
    out = {}
    try:
        tid = _tenant(cur, shop)
        cur.execute("DELETE FROM chat_discount_codes WHERE shop=%s AND COALESCE(platform,'shopify')='shopify'", (shop,))
        out["discount_codes"] = cur.rowcount
        n = 0
        if tid:
            for qid, d in _draft_rows(cur, tid, shop):
                cur.execute("UPDATE quotations SET shopify_draft=%s WHERE id=%s",
                            (psycopg2.extras.Json(_strip_buyer(d)), qid))
                n += 1
        out["draft_records"] = n
        cur.execute("UPDATE shopify_webhook_log SET payload='{}' WHERE shop=%s AND topic <> 'shop/redact'", (shop,))
        out["webhook_payloads"] = cur.rowcount
        conn.commit()
    finally:
        cur.close(); conn.close()
    _record(shop, tid, "shop/redact", None,
            f"{out.get('discount_codes', 0)} discount codes deleted, buyer removed from {out.get('draft_records', 0)} "
            f"draft-order records (products and B2B data deleted too)", status="done")
    return out


def forget_customer(shop: str, payload: dict) -> dict:
    """customers/redact extras (the B2B contact is removed by shopify_b2b)."""
    import data_crypto
    cid, emails, phones = _who(payload)
    # B2B contact names (read before shopify_b2b removes the contact): trade
    # buyers' draft-order records carry "Company · Contact name", not an email.
    names = set()
    try:
        import shopify_b2b
        names = {(c.get("name") or "").strip().lower()
                 for c in (shopify_b2b.customer_data(shop, cid) if cid else {}).get("contacts") or []} - {""}
    except Exception as e:
        print(f"⚠️ shopify privacy b2b names {shop}: {e}")
    conn = get_db_connection(); cur = conn.cursor()
    out = {"customer_id": cid}
    try:
        tid = _tenant(cur, shop)
        sessions = _matching_sessions(cur, tid, emails, phones)
        out["chats"] = sorted(sessions)
        if emails:
            cur.execute("""UPDATE chat_discount_codes SET buyer_email=NULL
                            WHERE shop=%s AND lower(COALESCE(buyer_email,'')) = ANY(%s)""", (shop, list(emails)))
            out["discount_codes"] = cur.rowcount
        n = 0
        if tid:
            for qid, d in _draft_rows(cur, tid, shop):
                b = (d or {}).get("buyer") or {}
                bname = (b.get("name") or "").strip().lower()
                if ((b.get("email") or "").strip().lower() in emails
                        or (_norm_phone(b.get("phone")) and _norm_phone(b.get("phone")) in phones)
                        or (bname and any(bname == n or bname.endswith(" · " + n) for n in names))):
                    cur.execute("UPDATE quotations SET shopify_draft=%s WHERE id=%s",
                                (psycopg2.extras.Json(_strip_buyer(d)), qid))
                    n += 1
        out["draft_records"] = n
        if tid and sessions:
            cur.execute("""UPDATE chat_visitors SET name=NULL, email=NULL, phone=NULL, company=NULL, updated_at=NOW()
                            WHERE tenant_id=%s AND session_id = ANY(%s)""", (tid, list(sessions)))
            out["chat_details"] = cur.rowcount
            cur.execute("""UPDATE handoff_requests SET visitor_name=NULL, visitor_email=NULL
                            WHERE tenant_id=%s AND session_id = ANY(%s)""", (tid, list(sessions)))
            out["handoff_details"] = cur.rowcount
        # Saved payloads for this customer: keep only the id.
        if cid:
            cur.execute("""UPDATE shopify_webhook_log SET payload=%s
                            WHERE shop=%s AND topic LIKE 'customers/%%'
                              AND (payload->'customer'->>'id'=%s OR payload->>'id'=%s)""",
                        (json.dumps({"customer_id": cid, "redacted": True}), shop, cid, cid))
        conn.commit()
    finally:
        cur.close(); conn.close()
    chats = out.get("chats") or []
    _record(shop, tid, "customers/redact", cid,
            f"Details removed: {out.get('chat_details', 0)} chats, {out.get('handoff_details', 0)} hand-over requests, "
            f"{out.get('discount_codes', 0)} discount codes, {out.get('draft_records', 0)} draft-order records. "
            + (f"{len(chats)} chats left to delete." if chats else "No chats to delete."),
            chats, status="todo" if chats else "done")
    _alert_redact(shop, tid, out)
    return out


def data_request(shop: str, payload: dict) -> bool:
    """customers/data_request: tell support@ what we hold and the deadline."""
    cid, emails, phones = _who(payload)
    req_id = (payload.get("data_request") or {}).get("id") or ""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        tid = _tenant(cur, shop)
        sessions = _matching_sessions(cur, tid, emails, phones)
        codes = 0
        if emails:
            cur.execute("""SELECT COUNT(*) FROM chat_discount_codes
                            WHERE shop=%s AND lower(COALESCE(buyer_email,'')) = ANY(%s)""", (shop, list(emails)))
            codes = cur.fetchone()[0]
        drafts = sum(1 for _, d in (_draft_rows(cur, tid, shop) if tid else [])
                     if (((d or {}).get("buyer") or {}).get("email") or "").strip().lower() in emails)
        conn.commit()
    finally:
        cur.close(); conn.close()
    b2b = {}
    try:
        import shopify_b2b
        b2b = shopify_b2b.customer_data(shop, cid) if cid else {}
    except Exception as e:
        b2b = {"error": str(e)}
    due = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%d %b %Y")
    lines = [
        f"Shopify store: {shop}",
        f"PhiXtra business (tenant): {tid or 'not linked'}",
        f"Shopify customer ID: {cid or '-'}",
        f"Shopify request ID: {req_id or '-'}",
        f"Email(s) in the request: {', '.join(sorted(emails)) or '-'}",
        "",
        "What PhiXtra holds:",
        f"- B2B contact records: {len((b2b or {}).get('contacts') or [])}",
        f"- Website chats with this email/phone: {len(sessions)}" + (f" ({', '.join(sorted(sessions))})" if sessions else ""),
        f"- Discount codes given in chat: {codes}",
        f"- Quotations sent to this buyer as Shopify draft orders: {drafts}",
        "",
        f"Answer the store owner by {due} (Shopify gives 30 days).",
    ]
    _record(shop, tid, "customers/data_request", cid,
            f"{len((b2b or {}).get('contacts') or [])} B2B contact records · {len(sessions)} chats · "
            f"{codes} discount codes · {drafts} draft-order quotes", sorted(sessions))
    lines += ["", f"Mark it answered in Admin › Privacy Requests: {ADMIN_URL}"]
    return _mail(f"Shopify data request — {shop} — answer by {due}", lines)


def _alert_redact(shop, tid, out):
    chats = out.get("chats") or []
    lines = [
        f"Shopify asked PhiXtra to delete a buyer's data. Store: {shop}, business (tenant): {tid or 'not linked'}, "
        f"Shopify customer ID: {out.get('customer_id') or '-'}.",
        "",
        "Done automatically:",
        f"- Contact details removed from {out.get('chat_details', 0)} chat(s) and {out.get('handoff_details', 0)} hand-over request(s)",
        f"- Email removed from {out.get('discount_codes', 0)} discount code(s)",
        f"- Buyer details removed from {out.get('draft_records', 0)} draft-order record(s)",
        "- B2B contact record removed (if there was one)",
        "",
    ]
    if chats:
        lines += [f"TO DO: delete {len(chats)} chat(s) in Admin › Privacy Requests: {ADMIN_URL}"]
    else:
        lines += ["No chats found for this buyer — nothing to delete by hand."]
    _mail(f"Shopify delete request — {shop}" + (f" — {len(chats)} chat(s) to delete" if chats else ""), lines)


def _mail(subject, lines) -> bool:
    try:
        from portal_utils import send_email
        import html
        body = "<br>".join(html.escape(l) for l in lines)
        return send_email(ALERT_TO, subject, f"<div style='font-family:Arial,sans-serif;font-size:14px'>{body}</div>",
                          "\n".join(lines))
    except Exception as e:
        print(f"⚠️ shopify privacy email failed: {e}")
        return False


# ── Admin › Privacy Requests (12 Nov 2026) ──────────────────────────────
ADMIN_URL = "https://portal.phixtra.com/admin/privacy"
_TOPIC_WORDS = {"customers/redact": "Delete a buyer's data", "customers/data_request": "What do you hold on a buyer?",
                "shop/redact": "Store uninstalled, delete its data"}
_ok = False


def ensure_table(cur=None):
    global _ok
    if _ok:
        return
    own = cur is None
    if own:
        conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS shopify_privacy_requests (
                           id          SERIAL PRIMARY KEY,
                           shop        TEXT NOT NULL,
                           tenant_id   INTEGER,
                           topic       TEXT NOT NULL,
                           customer_id TEXT,
                           summary     TEXT NOT NULL DEFAULT '',
                           chats       TEXT[] NOT NULL DEFAULT '{}',
                           received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                           due_at      TIMESTAMPTZ,
                           status      TEXT NOT NULL DEFAULT 'todo',
                           done_at     TIMESTAMPTZ,
                           done_by     TEXT)""")
        cur.execute("CREATE INDEX IF NOT EXISTS shopify_privacy_requests_recv ON shopify_privacy_requests(received_at DESC)")
        if own:
            conn.commit()
        _ok = True
    finally:
        if own:
            cur.close(); conn.close()


def _record(shop, tid, topic, customer_id, summary, chats=(), status="todo"):
    """Never holds emails/phones: store, business, Shopify customer id, counts, chat ids."""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        ensure_table(cur)
        due = None if topic == "shop/redact" else datetime.now(timezone.utc) + timedelta(days=30)
        cur.execute("""INSERT INTO shopify_privacy_requests (shop, tenant_id, topic, customer_id, summary, chats, due_at, status, done_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s='done' THEN NOW() END) RETURNING id""",
                    (shop, tid, topic, customer_id or None, summary[:1000], list(chats), due, status, status))
        rid = cur.fetchone()[0]
        conn.commit()
        return rid
    except Exception as e:
        print(f"⚠️ shopify privacy record {shop}: {e}")
        return None
    finally:
        cur.close(); conn.close()


def list_requests(limit=200):
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        ensure_table(cur)
        cur.execute("""SELECT * FROM shopify_privacy_requests ORDER BY (status='todo') DESC, received_at DESC LIMIT %s""", (limit,))
        rows = cur.fetchall()
        conn.commit()
    finally:
        cur.close(); conn.close()
    now = datetime.now(timezone.utc)
    for r in rows:
        r["words"] = _TOPIC_WORDS.get(r["topic"], r["topic"])
        r["late"] = bool(r["status"] == "todo" and r["due_at"] and r["due_at"] < now)
        r["days_left"] = (r["due_at"] - now).days if r["due_at"] else None
    return rows


def delete_chats(req_id: int, by: str) -> dict:
    """Admin: delete the chats left by a customers/redact request."""
    import chat_delete
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        ensure_table(cur)
        cur.execute("SELECT * FROM shopify_privacy_requests WHERE id=%s", (int(req_id),))
        r = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not r or r["topic"] != "customers/redact" or r["status"] != "todo":
        return {"ok": False, "error": "Nothing to delete for this request."}
    gone, failed = 0, []
    for sid in r["chats"] or []:
        if not r["tenant_id"]:
            break
        try:
            res = chat_delete.delete(r["tenant_id"], "web:" + sid, "PhiXtra (Shopify privacy request)", full=True)
            if res.get("ok") or res.get("error") == "Chat not found.":
                gone += 1
            else:
                failed.append(sid)
        except Exception as e:
            print(f"⚠️ privacy delete chat {sid}: {e}")
            failed.append(sid)
    if not failed:
        _mark(req_id, by)
    return {"ok": not failed, "deleted": gone, "failed": failed}


def mark_answered(req_id: int, by: str) -> bool:
    return _mark(req_id, by, topic="customers/data_request")


def _mark(req_id, by, topic=None):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE shopify_privacy_requests SET status='done', done_at=NOW(), done_by=%s
                        WHERE id=%s AND status='todo'""" + (" AND topic=%s" if topic else ""),
                    (by[:200], int(req_id)) + ((topic,) if topic else ()))
        ok = cur.rowcount > 0
        conn.commit()
        return ok
    finally:
        cur.close(); conn.close()


def todo_count() -> int:
    conn = get_db_connection(); cur = conn.cursor()
    try:
        ensure_table(cur)
        cur.execute("SELECT COUNT(*) FROM shopify_privacy_requests WHERE status='todo'")
        n = cur.fetchone()[0]
        conn.commit()
        return n
    finally:
        cur.close(); conn.close()
