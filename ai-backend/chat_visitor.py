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
    # Shopify Part 4 (3 Nov 2026): signed by the store's chat app embed, with
    # the buyer's selected B2B company location.
    cur.execute("ALTER TABLE chat_visitors ADD COLUMN IF NOT EXISTS shopify_signed BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE chat_visitors ADD COLUMN IF NOT EXISTS shopify_location_id VARCHAR(30)")
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
    if not any(out.values()):
        return {}
    if v.get("shopify_signed") is True:
        out["shopify_signed"] = True
        out["shopify_location_id"] = re.sub(r"\D", "", str(v.get("shopify_location_id") or ""))[:20] or None
    return out


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
            cur.execute("""INSERT INTO chat_visitors (tenant_id, session_id, name, email, phone, company,
                                                      shopify_signed, shopify_location_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (tenant_id, session_id) DO UPDATE SET
                               name=COALESCE(NULLIF(EXCLUDED.name,''), chat_visitors.name),
                               email=COALESCE(NULLIF(EXCLUDED.email,''), chat_visitors.email),
                               phone=COALESCE(NULLIF(EXCLUDED.phone,''), chat_visitors.phone),
                               company=COALESCE(NULLIF(EXCLUDED.company,''), chat_visitors.company),
                               shopify_signed=EXCLUDED.shopify_signed OR chat_visitors.shopify_signed,
                               shopify_location_id=COALESCE(EXCLUDED.shopify_location_id, chat_visitors.shopify_location_id),
                               updated_at=NOW()""",
                        (int(tenant_id), str(session_id)[:64], v["name"] or None, v["email"] or None,
                         v["phone"] or None, v["company"] or None, bool(v.get("shopify_signed")),
                         v.get("shopify_location_id")))
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        print("⚠️ chat_visitor.save:", e)
    return v


def _fingerprint(secret: str, kind: str, value: str) -> str:
    """Same as portal shopify_b2b.fingerprint — keep in step."""
    import hashlib, hmac
    if not value or not secret:
        return ""
    return hmac.new(secret.encode(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


def shopify_trade(tenant_id: int, v: dict) -> dict:
    """Shopify Part 4: a signed, logged-in buyer who is a contact of one of
    the store's B2B companies → {company, location, locations, terms}. {} = none.
    Only for signed buyers (certain); never for typed-in details."""
    if not v or not v.get("shopify_signed") or not v.get("email"):
        return {}
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""SELECT shop, visitor_secret FROM shopify_shops WHERE tenant_id=%s AND uninstalled_at IS NULL
                             AND b2b_status='done' ORDER BY linked_at DESC NULLS LAST LIMIT 1""", (int(tenant_id),))
            s = cur.fetchone()
            if not s or not s[1]:
                return {}
            shop = s[0]
            fp = _fingerprint(s[1], "email", v["email"].strip().lower())
            cur.execute("""SELECT c.contact_id, co.name FROM shopify_b2b_contacts c
                             JOIN shopify_b2b_companies co ON co.shop=c.shop AND co.company_id=c.company_id
                            WHERE c.shop=%s AND c.email_fp=%s LIMIT 1""", (shop, fp))
            hit = cur.fetchone()
            if not hit:
                return {}
            cur.execute("""SELECT l.location_id, l.name, l.payment_terms FROM shopify_b2b_roles r
                             JOIN shopify_b2b_locations l ON l.shop=r.shop AND l.location_id=r.location_id
                            WHERE r.shop=%s AND r.contact_id=%s ORDER BY l.name""", (shop, hit[0]))
            locs = [{"id": x[0], "name": x[1], "terms": x[2]} for x in cur.fetchall()]
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        print("⚠️ chat_visitor.shopify_trade:", e)
        return {}
    chosen = next((l for l in locs if l["id"] == (v.get("shopify_location_id") or "")), None)
    if not chosen and len(locs) == 1:
        chosen = locs[0]
    return {"company": hit[1], "location": chosen, "locations": locs}


def _trade_line(t: dict) -> str:
    if not t:
        return ""
    line = (f"\n\nEXISTING TRADE CUSTOMER: this buyer orders for {t['company']}, an existing business customer "
            "of this store (their store account says so). Greet them as a returning customer of "
            f"{t['company']}. Do NOT ask qualifying questions (company, who decides, budget, timeline): "
            "only gather what this order needs (products, quantities, delivery date). ")
    if t.get("location"):
        line += f"They are ordering for the site \"{t['location']['name']}\". "
        if t["location"].get("terms"):
            line += f"Their account has payment terms: {t['location']['terms']}. Mention only if they ask. "
    elif len(t.get("locations") or []) > 1:
        names = ", ".join(l["name"] for l in t["locations"][:8])
        line += f"They buy for several sites ({names}): if they haven't said, ask once which site this order is for. "
    line += "Prices you have are list prices: their account price is confirmed on the quote."
    return line


def prompt(v: dict, tenant_id: int = None) -> str:
    """The line the AI gets: who it is talking to, and not to ask again."""
    if not v:
        return ""
    bits = [f"{k}: {v[k]}" for k in ("name", "company", "email", "phone") if v.get(k)]
    have = [w for k, w in (("name", "name"), ("company", "company"), ("email", "email address"),
                           ("phone", "phone number")) if v.get(k)]
    return ("\n\nTHE CUSTOMER IS LOGGED IN to the website, so you already know: " + "; ".join(bits) + ". "
            "Greet them by name where natural. Never ask for their " + ", ".join(have) + ": you have them. "
            "If they give different details, use what they give."
            + (_trade_line(shopify_trade(tenant_id, v)) if tenant_id else ""))
