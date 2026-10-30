"""
demo_codes.py — one private "Try it yourself" space per visitor (30 Oct 2026).

Shared by the portal (Seller side, demo_visitor.py) and chat.phixtra.com
(Buyer side, ai-backend main.py loads this file by path) — plain psycopg2,
no Flask/FastAPI imports. Every function takes an open connection.

A visitor gets a code (5 digits, shown in the WhatsApp message) plus a
secret (keeps others from opening their chat). The token travels in the
links: ?v=<code>.<secret>.

What belongs to a visitor:
  • web chats whose session_id starts with  dv-<code>-<secret>-
    (their own buyer chat  dv-…-b, and their copies of the example chats
    'demo-…' → dv-…-<rest>);
  • WhatsApp chats whose inbound messages contain "demo <code>" (the
    ready-typed first message), sent after the code was made.
Everything else in the demo business (the original example chats, stray
WhatsApp messages) is hidden from every visitor.

wipe_idle() deletes a visitor's chats after IDLE_MINUTES with no activity
on either side; wipe_all() is the nightly safety net.
"""
import re
import secrets

IDLE_MINUTES = 60

_DDL = """
CREATE TABLE IF NOT EXISTS demo_visitor_codes (
    code         VARCHAR(8) PRIMARY KEY,
    secret       VARCHAR(16) NOT NULL,
    tenant_id    INTEGER NOT NULL,
    slug         VARCHAR(60) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    wiped_at     TIMESTAMPTZ
)"""
_TOKEN_RX = re.compile(r"^(\d{5})\.([a-z0-9]{10})$")


def ensure_table(conn) -> None:
    cur = conn.cursor()
    cur.execute(_DDL)
    conn.commit()


def prefix(code: str, secret: str) -> str:
    return f"dv-{code}-{secret}-"


def buyer_session(code: str, secret: str) -> str:
    return prefix(code, secret) + "b"


def token(v: dict) -> str:
    return f"{v['code']}.{v['secret']}"


def get(conn, tok: str, tenant_id: int = None):
    """The live (not wiped) visitor for a ?v= token, else None."""
    m = _TOKEN_RX.match((tok or "").strip().lower())
    if not m:
        return None
    cur = conn.cursor()
    cur.execute("""SELECT code, secret, tenant_id, slug FROM demo_visitor_codes
                    WHERE code=%s AND secret=%s AND wiped_at IS NULL""", m.groups())
    r = cur.fetchone()
    if not r or (tenant_id is not None and r[2] != tenant_id):
        return None
    return {"code": r[0], "secret": r[1], "tenant_id": r[2], "slug": r[3]}


def create(conn, tenant_id: int, slug: str) -> dict:
    """New visitor + their own copy of the example chats."""
    ensure_table(conn)
    cur = conn.cursor()
    for _ in range(50):
        code = f"{secrets.randbelow(90000) + 10000}"
        # A code is reused only once its old visitor was wiped a day ago, so
        # an old WhatsApp "demo 12345" never lands with a new visitor.
        cur.execute("""DELETE FROM demo_visitor_codes
                        WHERE code=%s AND wiped_at < NOW() - INTERVAL '1 day'""", (code,))
        cur.execute("SELECT 1 FROM demo_visitor_codes WHERE code=%s", (code,))
        if not cur.fetchone():
            break
    secret = "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(10))
    cur.execute("""INSERT INTO demo_visitor_codes (code, secret, tenant_id, slug)
                   VALUES (%s, %s, %s, %s)""", (code, secret, tenant_id, slug))
    p = prefix(code, secret)
    # Example chats 'demo-fr-abidjan' → 'dv-<code>-<secret>-fr-abidjan'.
    cur.execute("""INSERT INTO chat_sessions (session_id, tenant_id, created_at, last_seen)
                   SELECT %s || SUBSTRING(session_id FROM 6), tenant_id, created_at, last_seen
                     FROM chat_sessions WHERE tenant_id=%s AND session_id LIKE 'demo-%%'""",
                (p, tenant_id))
    cur.execute("""INSERT INTO chat_messages (session_id, tenant_id, role, content, created_at, embedding)
                   SELECT %s || SUBSTRING(session_id FROM 6), tenant_id, role, content, created_at, embedding
                     FROM chat_messages WHERE tenant_id=%s AND session_id LIKE 'demo-%%'""",
                (p, tenant_id))
    cur.execute("""INSERT INTO handoff_requests (tenant_id, session_id, whatsapp_number, visitor_message,
                                                 status, created_at, visitor_name, visitor_email)
                   SELECT tenant_id, %s || SUBSTRING(session_id FROM 6), whatsapp_number, visitor_message,
                          'pending', created_at, visitor_name, visitor_email
                     FROM handoff_requests WHERE tenant_id=%s AND session_id LIKE 'demo-%%'""",
                (p, tenant_id))
    conn.commit()
    return {"code": code, "secret": secret, "tenant_id": tenant_id, "slug": slug}


def touch(conn, code: str) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE demo_visitor_codes SET last_seen_at=NOW() WHERE code=%s AND wiped_at IS NULL", (code,))
    conn.commit()


def wa_phones(conn, tenant_id: int, code: str) -> set:
    """WhatsApp numbers that sent "demo <code>" since the code was made."""
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT m.customer_phone
                     FROM wa_message_log m JOIN demo_visitor_codes v ON v.code=%s
                    WHERE m.tenant_id=%s AND m.direction='inbound'
                      AND m.created_at >= v.created_at
                      AND m.content ~* %s""",
                (code, tenant_id, r"\mdemo\s*" + code + r"\M"))
    return {r[0] for r in cur.fetchall()}


def key_visible(key: str, v: dict, phones: set) -> bool:
    """May this visitor see / answer this Inbox chat key?"""
    key = key or ""
    if key.startswith("web:"):
        return key[4:].startswith(prefix(v["code"], v["secret"]))
    if key.startswith("fb:"):
        return False
    return key in phones


# ── Wiping ───────────────────────────────────────────────────────────────
_WEB_TABLES = ("chat_messages", "chat_sessions", "handoff_requests", "web_chat_alert_queue",
               "web_chat_presence", "web_chat_replies")


def _wipe_one(cur, conn, code: str, secret: str, tenant_id: int) -> int:
    n = 0
    like = prefix(code, secret).replace("_", r"\_") + "%"
    for t in _WEB_TABLES:
        cur.execute(f"DELETE FROM {t} WHERE tenant_id=%s AND session_id LIKE %s", (tenant_id, like))
        n += cur.rowcount
    cur.execute("DELETE FROM wa_conversation_assignments WHERE tenant_id=%s AND customer_phone LIKE %s",
                (tenant_id, "web:" + like))
    n += cur.rowcount
    # Their WhatsApp chats — unless another live visitor uses the same phone.
    phones = wa_phones(conn, tenant_id, code)
    cur.execute("""SELECT code FROM demo_visitor_codes
                    WHERE tenant_id=%s AND wiped_at IS NULL AND code<>%s""", (tenant_id, code))
    for (other,) in cur.fetchall():
        phones -= wa_phones(conn, tenant_id, other)
    for ph in phones:
        n += _wipe_wa_phone(cur, tenant_id, ph)
    cur.execute("UPDATE demo_visitor_codes SET wiped_at=NOW() WHERE code=%s", (code,))
    return n


def _wipe_wa_phone(cur, tenant_id: int, ph: str) -> int:
    n = 0
    for sql in ("DELETE FROM wa_message_log WHERE tenant_id=%s AND customer_phone=%s",
                "DELETE FROM wa_handoff_state WHERE tenant_id=%s AND customer_phone=%s",
                "DELETE FROM wa_conversation_assignments WHERE tenant_id=%s AND customer_phone=%s",
                "DELETE FROM wa_contacts WHERE tenant_id=%s AND (whatsapp_number=%s OR phone=%s)"):
        cur.execute(sql, (tenant_id, ph, ph) if sql.count("%s") == 3 else (tenant_id, ph))
        n += cur.rowcount
    # The AI's own memory of that WhatsApp chat.
    like = "wa-meta-%-" + ph
    for t in ("chat_messages", "chat_sessions", "handoff_requests"):
        cur.execute(f"DELETE FROM {t} WHERE tenant_id=%s AND session_id LIKE %s", (tenant_id, like))
        n += cur.rowcount
    return n


def wipe_idle(conn) -> dict:
    """Wipe every visitor with no activity for IDLE_MINUTES on either side."""
    ensure_table(conn)
    cur = conn.cursor()
    cur.execute("""
        SELECT v.code, v.secret, v.tenant_id
          FROM demo_visitor_codes v
         WHERE v.wiped_at IS NULL
           AND GREATEST(v.last_seen_at,
                 COALESCE((SELECT MAX(created_at) FROM chat_messages m
                            WHERE m.tenant_id=v.tenant_id
                              AND m.session_id LIKE 'dv-' || v.code || '-' || v.secret || '-%%'), v.last_seen_at),
                 COALESCE((SELECT MAX(last_seen_at) FROM web_chat_presence p
                            WHERE p.tenant_id=v.tenant_id
                              AND p.session_id LIKE 'dv-' || v.code || '-' || v.secret || '-%%'), v.last_seen_at),
                 COALESCE((SELECT MAX(created_at) FROM wa_message_log w
                            WHERE w.tenant_id=v.tenant_id AND w.created_at >= v.created_at
                              AND w.content ~* ('\\mdemo\\s*' || v.code || '\\M')), v.last_seen_at)
               ) < NOW() - make_interval(mins => %s)
    """, (IDLE_MINUTES,))
    rows = cur.fetchall()
    done = {}
    for code, secret, tenant_id in rows:
        done[code] = _wipe_one(cur, conn, code, secret, tenant_id)
    conn.commit()
    return done


def wipe_all(conn) -> dict:
    """Nightly: every visitor, plus any WhatsApp chat in a demo business that
    never carried a demo code (strangers messaging the demo number)."""
    ensure_table(conn)
    cur = conn.cursor()
    cur.execute("SELECT code, secret, tenant_id FROM demo_visitor_codes WHERE wiped_at IS NULL")
    done = {}
    for code, secret, tenant_id in cur.fetchall():
        done[code] = _wipe_one(cur, conn, code, secret, tenant_id)
    cur.execute("SELECT DISTINCT tenant_id FROM chat_links WHERE is_demo")
    for (t,) in cur.fetchall():
        cur.execute("SELECT DISTINCT customer_phone FROM wa_message_log WHERE tenant_id=%s", (t,))
        for (ph,) in cur.fetchall():
            done.setdefault("wa", 0)
            done["wa"] += _wipe_wa_phone(cur, t, ph)
    conn.commit()
    return done
