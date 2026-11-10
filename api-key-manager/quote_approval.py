"""
quote_approval.py — a manager's OK before a quotation is sent (10 Nov 2026).

AI Sales Agent for B2B plan (Quotations). Off until the business ticks a
rule in Quotation settings › Approvals:

  appr_amount_on / appr_amount   total above an amount
  appr_margin                    a line below the minimum margin (Prices & margins)
  appr_disc_on / appr_disc       a line more than N% below its list or volume price
  appr_new_buyer                 a buyer with no won quotation yet

People who can approve (owner, or a role with "quotes.approve") never need
approval for their own quotations. The approval lives on the quotation
(data["approval"]) with a fingerprint of its lines and charges: any change
to a price, quantity, line or discount after approval removes it.
"""
import hashlib
import json
from datetime import datetime, timezone

import psycopg2.extras

from db import get_db_connection
import quote_requests as qr

KEYS = ("appr_amount_on", "appr_amount", "appr_margin", "appr_disc_on", "appr_disc", "appr_new_buyer")


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def rules(tenant_id: int) -> dict:
    s = qr.settings(tenant_id)
    return {k: s.get(k) for k in KEYS}


def any_on(r: dict) -> bool:
    return bool(r.get("appr_amount_on") or r.get("appr_margin") or r.get("appr_disc_on") or r.get("appr_new_buyer"))


def fingerprint(d: dict) -> str:
    """What an approval covers: products, quantities, prices, discounts, charges."""
    lines = [(ln.get("product_id") or "", ln.get("name") or "", float(ln.get("qty") or 0),
              None if ln.get("price") is None else round(float(ln["price"]), 4), float(ln.get("disc") or 0))
             for ln in d.get("lines") or []]
    charges = [(c.get("name") or "", float(c.get("qty") or 0), round(float(c.get("price") or 0), 4))
               for c in d.get("charges") or []]
    return hashlib.sha256(json.dumps([lines, charges], sort_keys=True).encode()).hexdigest()[:24]


def _new_buyer(cur, tenant_id: int, req: dict) -> bool:
    conds, args = [], []
    if req.get("lead_id"):
        conds.append("lead_id = %s"); args.append(req["lead_id"])
    if req.get("buyer_email"):
        conds.append("lower(buyer_email) = lower(%s)"); args.append(req["buyer_email"])
    if not conds:
        return True
    cur.execute(f"""SELECT 1 FROM quote_requests WHERE tenant_id=%s AND id <> %s AND status='won'
                     AND ({' OR '.join(conds)}) LIMIT 1""", [tenant_id, req["id"]] + args)
    return cur.fetchone() is None


def reasons(tenant_id: int, q: dict, req: dict) -> list:
    """Why this quotation needs approval (empty = it doesn't)."""
    r = rules(tenant_id)
    if not any_on(r):
        return []
    import quote_quotes as qq
    import quote_design as qd
    d = q.get("data") or {}
    out = []
    if r.get("appr_amount_on") and r.get("appr_amount") is not None:
        total = qq.to_doc(tenant_id, q, qd.get(tenant_id), req)["total"]
        if float(total or 0) > float(r["appr_amount"]):
            out.append(f"total over {qr.money(r['appr_amount'], q.get('currency'))}")
    if r.get("appr_margin"):
        try:
            if qq.low_margin(tenant_id, d):
                out.append("a price below your minimum margin")
        except Exception as e:
            print("⚠️ approval margin:", e)
    if r.get("appr_disc_on") and r.get("appr_disc") is not None:
        import quote_prices as qp
        book = qp.all_for(tenant_id)
        lim = float(r["appr_disc"])
        for ln in d.get("lines") or []:
            if ln.get("price") is None:
                continue
            base = ln.get("list_price")
            vp, _ = qp.step_for((book.get(ln.get("product_id")) or {}).get("breaks"), ln.get("qty"))
            if vp is not None:
                base = vp
            if not base:
                continue
            net = float(ln["price"]) * (1 - float(ln.get("disc") or 0) / 100)
            if net < float(base) * (1 - lim / 100) - 0.0049:
                out.append(f"a discount of more than {'%g' % lim}%")
                break
    if r.get("appr_new_buyer"):
        conn, cur = _db()
        try:
            if _new_buyer(cur, tenant_id, req):
                out.append("first quotation to a new buyer")
        finally:
            cur.close(); conn.close()
    return out


def state(q: dict) -> dict:
    """The approval as it stands now: {} or {status: asked|approved|sent_back, ...}.
    An approval whose lines changed since is reported as {} (it no longer counts)."""
    a = (q.get("data") or {}).get("approval") or {}
    if a.get("status") == "approved" and a.get("fp") != fingerprint(q.get("data") or {}):
        return {"status": "changed", **{k: a.get(k) for k in ("by_label", "at")}}
    return a


def blocks_send(tenant_id: int, q: dict, req: dict, can_approve: bool) -> list:
    """Reasons that still stop sending: rules apply, the sender can't approve,
    and the quotation isn't approved as it is now."""
    if can_approve:
        return []
    why = reasons(tenant_id, q, req)
    if not why:
        return []
    return [] if state(q).get("status") == "approved" else why


def _save(tenant_id: int, qid: int, approval):
    conn, cur = _db()
    try:
        if approval is None:
            cur.execute("""UPDATE quotations SET data = data - 'approval', updated_at=NOW()
                            WHERE id=%s AND tenant_id=%s AND status='draft'""", (qid, tenant_id))
        else:
            cur.execute("""UPDATE quotations SET data = jsonb_set(data, '{approval}', %s::jsonb), updated_at=NOW()
                            WHERE id=%s AND tenant_id=%s AND status='draft'""",
                        (json.dumps(approval), qid, tenant_id))
        ok = cur.rowcount == 1
        conn.commit()
        return ok
    finally:
        cur.close(); conn.close()


def _event(req_id, what, by):
    conn, cur = _db()
    try:
        qr._event(cur, req_id, what, by)
        conn.commit()
    finally:
        cur.close(); conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── who approves ────────────────────────────────────────────────────────────

def _perms(cur, tenant_id, role_id) -> dict:
    if not role_id:
        return {}
    cur.execute("SELECT permissions FROM tenant_roles WHERE id=%s AND tenant_id=%s", (role_id, tenant_id))
    r = cur.fetchone()
    p = (r or {}).get("permissions") or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    return p


def approvers(tenant_id: int, asker_member_id=None) -> list:
    """[{key, label, email}] who should get the request: the asker's line
    manager when they can approve, else everyone with "quotes.approve",
    else the account owner."""
    conn, cur = _db()
    try:
        if asker_member_id:
            cur.execute("""SELECT m.id, m.name, m.email, m.role_id FROM team_members t
                             JOIN team_members m ON m.id = t.line_manager_id AND m.tenant_id = t.tenant_id AND m.is_active
                            WHERE t.id=%s AND t.tenant_id=%s""", (asker_member_id, tenant_id))
            lm = cur.fetchone()
            if lm and _perms(cur, tenant_id, lm["role_id"]).get("quotes.approve"):
                return [{"key": f"team:{lm['id']}", "label": lm["name"], "email": lm["email"]}]
        cur.execute("SELECT id, name, email, role_id FROM team_members WHERE tenant_id=%s AND is_active", (tenant_id,))
        out = [{"key": f"team:{m['id']}", "label": m["name"], "email": m["email"]}
               for m in cur.fetchall() if m["id"] != asker_member_id and _perms(cur, tenant_id, m["role_id"]).get("quotes.approve")]
        if out:
            return out
        cur.execute("""SELECT id, first_name, last_name, COALESCE(handoff_notify_email, email) AS email
                         FROM customers WHERE tenant_id=%s ORDER BY id LIMIT 1""", (tenant_id,))
        o = cur.fetchone()
        if not o:
            return []
        name = " ".join(x for x in (o["first_name"], o["last_name"]) if x) or "Account owner"
        return [{"key": f"owner:{o['id']}", "label": name, "email": o["email"]}]
    finally:
        cur.close(); conn.close()


def is_approver_key(tenant_id: int, key: str, approval: dict) -> bool:
    return key in [a.get("key") for a in approval.get("approvers") or []]


# ── actions ─────────────────────────────────────────────────────────────────

def ask(tenant_id: int, q: dict, req: dict, actor: dict, member_id=None, portal_url: str = "") -> tuple:
    import quote_quotes as qq
    if q["status"] != "draft":
        return False, "Only a draft can be sent for approval."
    why = reasons(tenant_id, q, req)
    if not why:
        return False, "This quotation doesn't need approval. You can send it."
    if state(q).get("status") == "asked":
        return False, "Approval has already been asked."
    who = approvers(tenant_id, member_id)
    if not who:
        return False, "Nobody can approve quotations yet. Ask the account owner."
    a = {"status": "asked", "reasons": why, "asked_by": actor.get("label") or "", "asked_key": actor.get("key") or "",
         "asked_at": _now(), "approvers": [{"key": w["key"], "label": w["label"]} for w in who],
         "fp": fingerprint(q.get("data") or {})}
    if not _save(tenant_id, q["id"], a):
        return False, "This quotation can't be changed now."
    _event(req["id"], f"Approval asked ({'; '.join(why)})", actor.get("label") or "")
    _alert(tenant_id, q, req, who, actor, why, portal_url)
    names = ", ".join(w["label"] for w in who)
    return True, f"Asked {names} for approval."


def withdraw(tenant_id: int, q: dict, req: dict, actor: dict) -> tuple:
    if state(q).get("status") != "asked":
        return False, "There is no request to withdraw."
    _save(tenant_id, q["id"], None)
    _event(req["id"], "Approval request withdrawn", actor.get("label") or "")
    return True, "Request withdrawn. You can edit the quotation again."


def decide(tenant_id: int, q: dict, req: dict, actor: dict, approve: bool, note: str = "") -> tuple:
    a = state(q)
    if a.get("status") != "asked":
        return False, "There is nothing waiting for approval."
    note = (note or "").strip()[:500]
    if not approve and not note:
        return False, "Write a short note so they know what to change."
    if approve and a.get("fp") != fingerprint(q.get("data") or {}):
        return False, "The quotation changed after approval was asked. It needs asking again."
    a = dict(a)
    a.update({"status": "approved" if approve else "sent_back", "by_label": actor.get("label") or "",
              "by_key": actor.get("key") or "", "at": _now(), "note": note,
              "fp": fingerprint(q.get("data") or {})})
    _save(tenant_id, q["id"], a)
    _event(req["id"], ("Approved by " if approve else "Sent back by ") + (actor.get("label") or "")
           + ("" if approve else f": {note}"), actor.get("label") or "")
    _tell_asker(tenant_id, q, req, a)
    return True, "Approved. They can send it now." if approve else "Sent back with your note."


def waiting_count(tenant_id: int, key: str) -> int:
    """Quotations waiting for this person's approval."""
    conn, cur = _db()
    try:
        cur.execute("""SELECT COUNT(*) AS n FROM quotations WHERE tenant_id=%s AND status='draft'
                         AND data->'approval'->>'status' = 'asked'
                         AND data->'approval'->'approvers' @> %s::jsonb""",
                    (tenant_id, json.dumps([{"key": key}])))
        return int(cur.fetchone()["n"] or 0)
    finally:
        cur.close(); conn.close()


def waiting_list(tenant_id: int, key: str) -> list:
    """[{qid, number, buyer, asked_by, reasons}] waiting for this person."""
    conn, cur = _db()
    try:
        cur.execute("""SELECT q.id, q.number, q.version, q.data->'approval' AS a,
                              COALESCE(r.buyer_company, r.buyer_name, '') AS buyer
                         FROM quotations q JOIN quote_requests r ON r.id = q.request_id AND r.tenant_id = q.tenant_id
                        WHERE q.tenant_id=%s AND q.status='draft' AND q.data->'approval'->>'status' = 'asked'
                          AND q.data->'approval'->'approvers' @> %s::jsonb
                        ORDER BY q.updated_at""", (tenant_id, json.dumps([{"key": key}])))
        return [{"qid": r["id"], "number": r["number"] + (f" v{r['version']}" if (r["version"] or 1) > 1 else ""),
                 "buyer": r["buyer"], "asked_by": (r["a"] or {}).get("asked_by") or "",
                 "reasons": (r["a"] or {}).get("reasons") or []} for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()


def waiting_requests(tenant_id: int, key: str) -> set:
    conn, cur = _db()
    try:
        cur.execute("""SELECT request_id FROM quotations WHERE tenant_id=%s AND status='draft'
                         AND data->'approval'->>'status' = 'asked'
                         AND data->'approval'->'approvers' @> %s::jsonb""",
                    (tenant_id, json.dumps([{"key": key}])))
        return {r["request_id"] for r in cur.fetchall()}
    finally:
        cur.close(); conn.close()


# ── emails ──────────────────────────────────────────────────────────────────

def _mail(to, subject, lines, link, button):
    import html as _h
    from portal_utils import send_email
    body = "".join(f"<p>{_h.escape(l)}</p>" for l in lines)
    if link:
        body += (f'<p><a href="{_h.escape(link)}" style="background:#0B1D40;color:#fff;padding:10px 16px;'
                 f'border-radius:10px;text-decoration:none;display:inline-block">{_h.escape(button)}</a></p>')
    html = f'<div style="font-family:Arial,sans-serif;max-width:560px;color:#101828">{body}</div>'
    try:
        return send_email(to, subject, html, text_body="\n\n".join(lines + ([link] if link else [])))
    except Exception as e:
        print("⚠️ approval email:", e)
        return False


def _alert(tenant_id, q, req, who, actor, why, portal_url):
    import quote_quotes as qq
    import quote_design as qd
    total = qr.money(qq.to_doc(tenant_id, q, qd.get(tenant_id), req)["total"], q.get("currency"))
    num = qq.full_number(q)
    buyer = req.get("buyer_company") or req.get("buyer_name") or "a buyer"
    link = f"{portal_url}/quotations/q/{q['id']}" if portal_url else ""
    for w in who:
        if not w.get("email"):
            continue
        first = (w["label"] or "").split(" ")[0]
        _mail(w["email"], f"{num} needs your approval",
              [f"Hi {first}," if first else "Hello,",
               f"{actor.get('label') or 'A salesperson'} asks you to approve quotation {num} for {buyer} ({total}).",
               "Reason: " + "; ".join(why) + "."], link, "Open the quotation")


def _tell_asker(tenant_id, q, req, a):
    key = a.get("asked_key") or ""
    conn, cur = _db()
    try:
        email = None
        if key.startswith("team:"):
            cur.execute("SELECT email FROM team_members WHERE id=%s AND tenant_id=%s", (int(key[5:]), tenant_id))
            email = (cur.fetchone() or {}).get("email")
        elif key.startswith("owner:"):
            cur.execute("SELECT email FROM customers WHERE id=%s AND tenant_id=%s", (int(key[6:]), tenant_id))
            email = (cur.fetchone() or {}).get("email")
    finally:
        cur.close(); conn.close()
    if not email:
        return
    import quote_quotes as qq
    import auto_followups as _af
    num = qq.full_number(q)
    if a["status"] == "approved":
        _mail(email, f"{num} is approved", [f"{a['by_label']} approved quotation {num}. You can send it now."],
              f"{_af.PORTAL}/quotations/q/{q['id']}", "Open the quotation")
    else:
        _mail(email, f"{num} was sent back", [f"{a['by_label']} sent quotation {num} back:", f"“{a.get('note')}”",
                                               "Make the change, then ask again."],
              f"{_af.PORTAL}/quotations/q/{q['id']}", "Open the quotation")
