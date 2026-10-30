"""
demo_visitor.py — "Try it yourself" Seller side (30 Oct 2026).

portal.phixtra.com/demo/<slug> logs a website visitor straight into the demo
business behind a chat_links row (is_demo = TRUE, enabled = TRUE) — no
password. The session carries demo_visitor=<slug>, and the lock below keeps
that visitor to looking around + answering the demo web chats:

  • GET: every page opens, except money / account / team / connection pages
    and anything that deletes, exports or downloads.
  • POST: blocked, except replying to / claiming / releasing / resolving a
    WEB chat ('web:' key) and the Inbox translate buttons. WhatsApp chats can
    never be answered from a demo link.

Private space per visitor (30 Oct 2026, demo_codes.py): each visitor has a
code, kept in their session and carried in the Buyer / WhatsApp links. Their
Inbox shows only their own chats (web + WhatsApp), and they can answer only
those. Contacts / Segments / Chat Archive are closed in the demo, since they
would list other visitors' names and numbers.

Cron: `demo_visitor.py idle` every 10 min wipes visitors idle for an hour;
`demo_visitor.py` (nightly) wipes everyone.
"""
import base64
import io
import re
import time
from urllib.parse import quote

import psycopg2.extras
from flask import Blueprint, session, redirect, request, flash, jsonify, abort, g

import demo_codes

from db import get_db_connection
from feature_access import public_route

demo_bp = Blueprint("demo_visitor", __name__)

TRIAL_URL = "https://portal.phixtra.com/register?type=b2b"
BUYER_URL = "https://chat.phixtra.com/c/{slug}?v={token}"

# First path part → page closed to demo visitors.
_BLOCKED_GET_PREFIXES = (
    "/billing", "/settings", "/team", "/api-keys", "/invoices", "/invoice",
    "/plans", "/pay", "/credit-packages", "/calls/settings", "/data-sources",
    "/woo-sync", "/store-info/website/connect", "/connect", "/wa-login",
    "/onboard-wa", "/onboarding", "/integrations", "/plugins", "/deletion",
    "/impersonate", "/stop-impersonate", "/admin", "/reset", "/forgot",
    "/verified-specs-settings", "/discount-settings", "/reports/billing",
    "/reports/usage", "/whatsapp/message-templates", "/channels",
    "/modules", "/inactive-accounts", "/accounts", "/lead-finder",
    # Would list other visitors' names / numbers (private space per visitor).
    "/whatsapp/contacts", "/whatsapp/segments", "/chat-archive",
)
_BLOCKED_GET_WORDS = re.compile(
    r"(delete|remove|toggle|export|download|disconnect|revoke|reset|unsubscribe)",
    re.I)
# Team Performance is a read-only report — /team above must not swallow it.
_ALLOWED_GET_EXCEPTIONS = ("/team-performance",)

# Answering is allowed only on the visitor's own chats (checked below).
_ALLOWED_CHAT_POST = re.compile(r"^/inbox/(web:[^/]+|\d{6,15})/(reply|claim|release|resolve)$")
_ALLOWED_POST = (
    re.compile(r"^/inbox/translate(-out)?$"),
)


def _starts(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _get_allowed(path: str) -> bool:
    if any(_starts(path, p) for p in _ALLOWED_GET_EXCEPTIONS):
        return not _BLOCKED_GET_WORDS.search(path)
    if any(_starts(path, p) for p in _BLOCKED_GET_PREFIXES):
        return False
    return not _BLOCKED_GET_WORDS.search(path)


def _post_allowed(path: str) -> bool:
    m = _ALLOWED_CHAT_POST.match(path)
    if m:
        return can_see(m.group(1))
    return any(rx.match(path) for rx in _ALLOWED_POST)


# ── The visitor's own code ───────────────────────────────────────────────
def _visitor():
    """The demo visitor's code dict for this request, or None (not a demo
    login). A code wiped for being idle is replaced by a fresh one."""
    if not session.get("demo_visitor"):
        return None
    if "_dv" in g:
        return g._dv
    conn = get_db_connection()
    try:
        v = demo_codes.get(conn, session.get("demo_token"), session.get("demo_tenant"))
        if not v and session.get("demo_tenant"):
            v = demo_codes.create(conn, int(session["demo_tenant"]), session["demo_visitor"])
            session["demo_token"] = demo_codes.token(v)
        if v and time.time() - session.get("demo_touch", 0) > 60:
            demo_codes.touch(conn, v["code"])
            session["demo_touch"] = time.time()
        if v:
            v["phones"] = demo_codes.wa_phones(conn, v["tenant_id"], v["code"])
    finally:
        conn.close()
    g._dv = v
    return v


def can_see(key: str) -> bool:
    """Inbox rule for demo visitors: their own chats only. True for every
    normal login (called from portal_routes' Inbox visibility checks)."""
    if not session.get("demo_visitor"):
        return True
    v = _visitor()
    return bool(v) and demo_codes.key_visible(key, v, v["phones"])


def _wants_json() -> bool:
    return (request.is_json
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in (request.headers.get("Accept") or ""))


def enforce_demo_lock():
    """App-wide before_request. Does nothing for normal logins."""
    if not session.get("demo_visitor"):
        return None
    path = request.path or "/"
    if not path.startswith("/static/"):
        _visitor()
    if path.startswith("/static/") or path in ("/sw.js", "/logout") or _starts(path, "/demo"):
        return None
    if request.method in ("GET", "HEAD", "OPTIONS"):
        if _get_allowed(path):
            return None
    elif _post_allowed(path):
        return None
    msg = "That part is switched off in the demo. Start your free B2B trial to use it."
    if _wants_json():
        return jsonify({"ok": False, "error": msg}), 403
    flash(msg, "warning")
    back = request.referrer or ""
    if request.method != "GET" and back.startswith(request.host_url):
        return redirect(back)
    return redirect("/inbox")


def _demo_link(slug: str):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT cl.slug, cl.tenant_id,
                   (SELECT c.id FROM customers c
                     WHERE c.tenant_id = cl.tenant_id AND c.is_active
                     ORDER BY c.id LIMIT 1) AS customer_id
              FROM chat_links cl
             WHERE cl.slug = %s AND cl.enabled AND cl.is_demo
        """, (slug,))
        return cur.fetchone()
    finally:
        conn.close()


@demo_bp.route("/demo/<slug>")
@public_route
def demo_login(slug: str):
    if not re.fullmatch(r"[a-z0-9-]{1,40}", slug or ""):
        abort(404)
    link = _demo_link(slug)
    if not link or not link["customer_id"]:
        abort(404)
    tenant_id = int(link["tenant_id"])
    conn = get_db_connection()
    try:
        # Came from the Buyer chat (?v=…) → same visitor, so both sides pair.
        # Else keep this browser's own code, else make a new one.
        v = (demo_codes.get(conn, request.args.get("v"), tenant_id)
             or (demo_codes.get(conn, session.get("demo_token"), tenant_id)
                 if session.get("demo_visitor") == slug else None)
             or demo_codes.create(conn, tenant_id, slug))
        p = demo_codes.prefix(v["code"], v["secret"])
        cur = conn.cursor()
        cur.execute("""SELECT session_id FROM handoff_requests
                        WHERE tenant_id=%s AND session_id LIKE %s
                        ORDER BY (session_id = %s) DESC, created_at DESC LIMIT 1""",
                    (tenant_id, p + "%", p + "b"))
        row = cur.fetchone()
    finally:
        conn.close()
    session.clear()
    session["portal_logged_in"] = True
    session["customer_id"] = int(link["customer_id"])
    session["demo_visitor"] = slug
    session["demo_tenant"] = tenant_id
    session["demo_token"] = demo_codes.token(v)
    # Open on their buyer chat if they started there, else newest of theirs.
    return redirect("/inbox?phone=web:" + row[0] if row else "/inbox")


def _qr(text: str) -> str:
    import qrcode
    buf = io.BytesIO()
    qrcode.make(text, box_size=5, border=2).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _demo_wa_number(tenant_id: int):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT w.display_phone_number, t.name FROM tenants t
                         LEFT JOIN wa_tenants w ON w.tenant_id=t.id AND w.active
                        WHERE t.id=%s ORDER BY w.id LIMIT 1""", (tenant_id,))
        return cur.fetchone() or (None, None)
    finally:
        conn.close()


def demo_bar_context():
    slug = session.get("demo_visitor")
    if not slug or request.path.startswith("/static/"):
        return {"_demo_visitor": None}
    v = _visitor()
    if not v:
        return {"_demo_visitor": None}
    tok = demo_codes.token(v)
    buyer = BUYER_URL.format(slug=slug, token=tok)
    out = {"buyer_url": buyer, "trial_url": TRIAL_URL, "code": v["code"],
           "buyer_qr": _qr(buyer), "wa_url": None, "wa_qr": None, "wa_number": None}
    number, name = _demo_wa_number(v["tenant_id"])
    digits = re.sub(r"\D", "", number or "")
    if digits:
        text = f"Hi {name or 'there'} (demo {v['code']})"
        out["wa_url"] = f"https://wa.me/{digits}?text={quote(text)}"
        out["wa_qr"] = _qr(out["wa_url"])
        out["wa_number"] = number
    return {"_demo_visitor": out}


def wipe_idle_visitors() -> dict:
    conn = get_db_connection()
    try:
        return demo_codes.wipe_idle(conn)
    finally:
        conn.close()


def clear_demo_chats() -> dict:
    """Nightly: every visitor's space + any leftover chat in every demo
    business; keep the 'demo-…' example chats (the originals visitors copy)."""
    conn = get_db_connection()
    try:
        visitors = demo_codes.wipe_all(conn)
    finally:
        conn.close()
    conn = get_db_connection()
    done = {}
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT tenant_id FROM chat_links WHERE is_demo")
        tenants = [r[0] for r in cur.fetchall()]
        keep = "demo-%"
        for t in tenants:
            n = 0
            for table in ("chat_messages", "chat_sessions", "handoff_requests",
                          "web_chat_alert_queue", "web_chat_presence",
                          "web_chat_replies"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id=%s AND session_id NOT LIKE %s",
                            (t, keep))
                n += cur.rowcount
            cur.execute("""DELETE FROM wa_conversation_assignments
                            WHERE tenant_id=%s AND customer_phone LIKE 'web:%%'""", (t,))
            n += cur.rowcount
            # Replies the visitors typed on the seeded example chats, and the
            # seeded chats back to "needs an answer".
            cur.execute("DELETE FROM web_chat_replies WHERE tenant_id=%s", (t,))
            n += cur.rowcount
            cur.execute("""UPDATE handoff_requests SET status='pending', handled_at=NULL
                            WHERE tenant_id=%s AND session_id LIKE %s""", (t, keep))
            done[t] = n
        conn.commit()
    finally:
        conn.close()
    done["visitors"] = visitors
    return done


if __name__ == "__main__":
    import sys
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    if sys.argv[1:] == ["idle"]:
        done = wipe_idle_visitors()
        if done:
            print(now, "wipe_idle_visitors", done)
    else:
        print(now, "clear_demo_chats", clear_demo_chats())
