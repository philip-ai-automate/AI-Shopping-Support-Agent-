"""
chat_visitor.py — who a website visitor is, when the website knows (31 Oct 2026).

The WordPress plugin (3.9.16) sends the logged-in customer's name, email,
phone and company with each chat message. They are kept per chat, so the
AI doesn't ask for them again, and AI Lead Qualification (portal,
lead_qualify.qualify_chat) puts them on the lead it makes from the chat.
Only what the website sends is kept; nothing is looked up elsewhere.
"""
import re

from db import get_db_connection

_ok = False
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _ensure(cur):
    global _ok
    if _ok:
        return
    cur.execute("""CREATE TABLE IF NOT EXISTS chat_visitors (
                       tenant_id  INTEGER NOT NULL,
                       session_id VARCHAR(64) NOT NULL,
                       name       VARCHAR(200),
                       email      VARCHAR(255),
                       phone      VARCHAR(40),
                       company    VARCHAR(200),
                       updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                       PRIMARY KEY (tenant_id, session_id))""")
    _ok = True


def clean(v) -> dict:
    """The visitor details as sent, tidied; {} when there is nothing usable."""
    if not isinstance(v, dict):
        return {}
    def s(k, n):
        return re.sub(r"\s+", " ", str(v.get(k) or "")).strip()[:n]
    out = {"name": s("name", 200), "email": s("email", 255).lower(), "phone": s("phone", 40), "company": s("company", 200)}
    if out["email"] and not _EMAIL.match(out["email"]):
        out["email"] = ""
    if len(re.sub(r"\D", "", out["phone"])) < 7:
        out["phone"] = ""
    return out if any(out.values()) else {}


def save(tenant_id: int, session_id: str, v: dict) -> dict:
    """Keep the details for this chat. Returns them cleaned ({} = none). Never raises."""
    v = clean(v)
    if not v or not session_id:
        return v
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            _ensure(cur)
            cur.execute("""INSERT INTO chat_visitors (tenant_id, session_id, name, email, phone, company)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (tenant_id, session_id) DO UPDATE SET
                               name=COALESCE(NULLIF(EXCLUDED.name,''), chat_visitors.name),
                               email=COALESCE(NULLIF(EXCLUDED.email,''), chat_visitors.email),
                               phone=COALESCE(NULLIF(EXCLUDED.phone,''), chat_visitors.phone),
                               company=COALESCE(NULLIF(EXCLUDED.company,''), chat_visitors.company),
                               updated_at=NOW()""",
                        (int(tenant_id), str(session_id)[:64], v["name"] or None, v["email"] or None,
                         v["phone"] or None, v["company"] or None))
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        print("⚠️ chat_visitor.save:", e)
    return v


def prompt(v: dict) -> str:
    """The line the AI gets: who it is talking to, and not to ask again."""
    if not v:
        return ""
    bits = [f"{k}: {v[k]}" for k in ("name", "company", "email", "phone") if v.get(k)]
    have = [w for k, w in (("name", "name"), ("company", "company"), ("email", "email address"),
                           ("phone", "phone number")) if v.get(k)]
    return ("\n\nTHE CUSTOMER IS LOGGED IN to the website, so you already know: " + "; ".join(bits) + ". "
            "Greet them by name where natural. Never ask for their " + ", ".join(have) + ": you have them. "
            "If they give different details, use what they give.")
