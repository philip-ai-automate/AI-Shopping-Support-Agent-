"""Admin two-step login (Nov 2026, Shopify App Store item 14).

Password, then a 6-digit code from an authenticator app (TOTP, RFC 6238 —
Google/Microsoft Authenticator, 1Password). Backup: a 6-digit code emailed to
the admin's backup address (10 minutes), or one of 10 one-time recovery codes.
"Trust this computer" skips the code for 30 days on that browser.

On/off switch: portal_settings 'admin_2fa_enabled' = 'on'. Anything else (or
the row missing) = Admin goes back to password only, nothing else changes.
"""
import base64
import hashlib
import hmac
import io
import json
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone

from db import get_db_connection

ISSUER = "PhiXtra Admin"
TRUST_COOKIE = "phx_admin_trust"
TRUST_DAYS = 30
EMAIL_CODE_MIN = 10
PENDING_MIN = 10

_ready = False


def _conn():
    conn = get_db_connection()
    cur = conn.cursor()
    ensure(cur)
    return conn, cur


def ensure(cur):
    global _ready
    if _ready:
        return
    for col, typ in (("email", "VARCHAR(255)"), ("totp_secret", "VARCHAR(64)"),
                     ("totp_enabled_at", "TIMESTAMPTZ"), ("totp_last_step", "BIGINT"),
                     ("recovery_codes", "JSONB")):
        cur.execute(f"ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS {col} {typ}")
    cur.execute("""CREATE TABLE IF NOT EXISTS admin_trusted_devices (
                       id BIGSERIAL PRIMARY KEY,
                       admin_id INTEGER NOT NULL,
                       token_hash VARCHAR(64) NOT NULL UNIQUE,
                       created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                       expires_at TIMESTAMPTZ NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS admin_email_codes (
                       admin_id INTEGER PRIMARY KEY,
                       code_hash VARCHAR(64) NOT NULL,
                       sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                       expires_at TIMESTAMPTZ NOT NULL,
                       tries INTEGER NOT NULL DEFAULT 0)""")
    _ready = True


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()


# ── switch ────────────────────────────────────────────────────────────────
def enabled() -> bool:
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT setting_value FROM portal_settings WHERE setting_key='admin_2fa_enabled' LIMIT 1")
        row = cur.fetchone()
        cur.close(); conn.close()
        return bool(row) and (row[0] or "").strip().lower() == "on"
    except Exception:
        return False


# ── TOTP ──────────────────────────────────────────────────────────────────
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    h = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    n = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{n:06d}"


def match_step(secret: str, code: str, now=None):
    """The time step the code belongs to (now ± 30 s), else None."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6 or not secret:
        return None
    step = int((now or time.time()) // 30)
    for s in (step, step - 1, step + 1):
        if hmac.compare_digest(_code_at(secret, s), code):
            return s
    return None


def otpauth_uri(secret: str, username: str) -> str:
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(ISSUER)}:{quote(username)}?secret={secret}"
            f"&issuer={quote(ISSUER)}&digits=6&period=30")


def qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


# ── per-admin state ───────────────────────────────────────────────────────
def get(admin_id: int) -> dict:
    conn, cur = _conn()
    cur.execute("""SELECT id, username, email, totp_secret, totp_enabled_at, totp_last_step,
                          recovery_codes FROM admin_users WHERE id=%s""", (admin_id,))
    r = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    if not r:
        return {}
    keys = ("id", "username", "email", "totp_secret", "totp_enabled_at", "totp_last_step", "recovery_codes")
    d = dict(zip(keys, r))
    rc = d.get("recovery_codes")
    d["recovery_codes"] = rc if isinstance(rc, list) else (json.loads(rc) if rc else [])
    return d


def is_set_up(a: dict) -> bool:
    return bool(a.get("totp_secret") and a.get("totp_enabled_at"))


def check_app_code(admin_id: int, code: str) -> bool:
    """App code, each code usable once (no replay)."""
    a = get(admin_id)
    s = match_step(a.get("totp_secret"), code)
    if s is None or (a.get("totp_last_step") and s <= int(a["totp_last_step"])):
        return False
    conn, cur = _conn()
    cur.execute("UPDATE admin_users SET totp_last_step=%s WHERE id=%s", (s, admin_id))
    conn.commit(); cur.close(); conn.close()
    return True


def new_recovery_codes():
    plain = ["-".join([secrets.token_hex(2), secrets.token_hex(2)]).upper() for _ in range(10)]
    return plain, [_sha(p) for p in plain]


def save_setup(admin_id: int, secret: str, email: str, step: int):
    plain, hashed = new_recovery_codes()
    conn, cur = _conn()
    cur.execute("""UPDATE admin_users SET totp_secret=%s, totp_enabled_at=NOW(), totp_last_step=%s,
                          email=%s, recovery_codes=%s WHERE id=%s""",
                (secret, step, email, json.dumps(hashed), admin_id))
    conn.commit(); cur.close(); conn.close()
    return plain


def use_recovery_code(admin_id: int, code: str) -> bool:
    c = (code or "").strip().upper().replace(" ", "")
    if len(c) == 8 and "-" not in c:
        c = c[:4] + "-" + c[4:]
    h = _sha(c)
    a = get(admin_id)
    codes = a.get("recovery_codes") or []
    if h not in codes:
        return False
    codes.remove(h)
    conn, cur = _conn()
    cur.execute("UPDATE admin_users SET recovery_codes=%s WHERE id=%s", (json.dumps(codes), admin_id))
    conn.commit(); cur.close(); conn.close()
    return True


def reset(admin_id: int):
    """Owner's 'Reset two-step': set up again at next login; trusted computers forgotten."""
    conn, cur = _conn()
    cur.execute("""UPDATE admin_users SET totp_secret=NULL, totp_enabled_at=NULL, totp_last_step=NULL,
                          recovery_codes=NULL WHERE id=%s""", (admin_id,))
    cur.execute("DELETE FROM admin_trusted_devices WHERE admin_id=%s", (admin_id,))
    cur.execute("DELETE FROM admin_email_codes WHERE admin_id=%s", (admin_id,))
    conn.commit(); cur.close(); conn.close()


def set_email(admin_id: int, email: str):
    conn, cur = _conn()
    cur.execute("UPDATE admin_users SET email=%s WHERE id=%s", ((email or "").strip().lower() or None, admin_id))
    conn.commit(); cur.close(); conn.close()


def status_map() -> dict:
    """{admin_id: {'on': bool, 'since': dt, 'email': str, 'codes_left': n}} for Admin › Team."""
    conn, cur = _conn()
    cur.execute("SELECT id, email, totp_secret IS NOT NULL AND totp_enabled_at IS NOT NULL, totp_enabled_at, recovery_codes FROM admin_users")
    out = {}
    for i, em, on, since, rc in cur.fetchall():
        rc = rc if isinstance(rc, list) else (json.loads(rc) if rc else [])
        out[i] = {"on": bool(on), "since": since, "email": em, "codes_left": len(rc)}
    conn.commit(); cur.close(); conn.close()
    return out


# ── email backup code ─────────────────────────────────────────────────────
def send_email_code(admin_id: int):
    """(ok, message). One code at a time, at most one email a minute."""
    a = get(admin_id)
    if not a.get("email"):
        return False, "No backup email is saved for this login. Ask an owner to add one on Admin › Team."
    conn, cur = _conn()
    cur.execute("SELECT sent_at FROM admin_email_codes WHERE admin_id=%s AND sent_at > NOW() - INTERVAL '60 seconds'", (admin_id,))
    if cur.fetchone():
        conn.commit(); cur.close(); conn.close()
        return False, "A code was just sent. Please wait a minute before asking for another."
    code = f"{secrets.randbelow(1000000):06d}"
    cur.execute("""INSERT INTO admin_email_codes (admin_id, code_hash, sent_at, expires_at, tries)
                   VALUES (%s, %s, NOW(), NOW() + make_interval(mins => %s), 0)
                   ON CONFLICT (admin_id) DO UPDATE SET code_hash=EXCLUDED.code_hash, sent_at=NOW(),
                          expires_at=EXCLUDED.expires_at, tries=0""",
                (admin_id, _sha(code), EMAIL_CODE_MIN))
    conn.commit(); cur.close(); conn.close()
    from portal_utils import send_email
    html = (f"<p>Your PhiXtra Admin login code is:</p>"
            f"<p style='font-size:28px;font-weight:800;letter-spacing:4px'>{code}</p>"
            f"<p>It works for {EMAIL_CODE_MIN} minutes. If you didn't try to log in to PhiXtra Admin, "
            f"change your Admin password straight away.</p>")
    ok = send_email(a["email"], "Your PhiXtra Admin login code", html,
                    text_body=f"Your PhiXtra Admin login code is {code}. It works for {EMAIL_CODE_MIN} minutes.")
    if not ok:
        return False, "The email could not be sent. Please use your authenticator app or a recovery code."
    em = a["email"]
    shown = em[0] + "•••" + em[em.index("@") - 1:] if "@" in em and em.index("@") > 1 else em
    return True, f"We emailed a 6-digit code to {shown}. It works for {EMAIL_CODE_MIN} minutes."


def check_email_code(admin_id: int, code: str) -> bool:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    conn, cur = _conn()
    cur.execute("SELECT code_hash, tries FROM admin_email_codes WHERE admin_id=%s AND expires_at > NOW()", (admin_id,))
    r = cur.fetchone()
    ok = False
    if r and r[1] < 5:
        if len(code) == 6 and hmac.compare_digest(r[0], _sha(code)):
            ok = True
            cur.execute("DELETE FROM admin_email_codes WHERE admin_id=%s", (admin_id,))
        else:
            cur.execute("UPDATE admin_email_codes SET tries=tries+1 WHERE admin_id=%s", (admin_id,))
    conn.commit(); cur.close(); conn.close()
    return ok


# ── trusted computers ─────────────────────────────────────────────────────
def trust_this_computer(admin_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn, cur = _conn()
    cur.execute("""INSERT INTO admin_trusted_devices (admin_id, token_hash, expires_at)
                   VALUES (%s, %s, NOW() + make_interval(days => %s))""", (admin_id, _sha(token), TRUST_DAYS))
    cur.execute("DELETE FROM admin_trusted_devices WHERE expires_at < NOW()")
    conn.commit(); cur.close(); conn.close()
    return token


def is_trusted(admin_id: int, token: str) -> bool:
    if not token:
        return False
    conn, cur = _conn()
    cur.execute("SELECT 1 FROM admin_trusted_devices WHERE admin_id=%s AND token_hash=%s AND expires_at > NOW()",
                (admin_id, _sha(token)))
    ok = cur.fetchone() is not None
    conn.commit(); cur.close(); conn.close()
    return ok


def pending_fresh(p: dict) -> bool:
    try:
        return bool(p) and (time.time() - float(p.get("at", 0))) < PENDING_MIN * 60
    except Exception:
        return False
