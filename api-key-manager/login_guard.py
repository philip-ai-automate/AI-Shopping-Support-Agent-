"""Wrong-guess limit for logins (Nov 2026, Shopify App Store item 14).

After MAX_FAILS wrong tries for the same login name from the same internet
address within WINDOW_MIN minutes, that pair is paused until the window
passes. The account itself is never locked, so a stranger can't lock a real
customer out from somewhere else. Used by the business login, the Admin
login and the Admin two-step code page.
"""
from flask import request
from db import get_db_connection

MAX_FAILS = 5
WINDOW_MIN = 15
MSG_PAUSED = "Too many wrong attempts. Please wait 15 minutes or reset your password."

_ready = False


def _ensure(cur):
    global _ready
    if _ready:
        return
    cur.execute("""CREATE TABLE IF NOT EXISTS login_failures (
                       id BIGSERIAL PRIMARY KEY,
                       scope VARCHAR(20) NOT NULL,
                       ident VARCHAR(255) NOT NULL,
                       ip VARCHAR(64) NOT NULL,
                       at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    cur.execute("CREATE INDEX IF NOT EXISTS login_failures_key ON login_failures (scope, ident, ip, at)")
    _ready = True


def client_ip() -> str:
    try:
        ip = (request.headers.get("CF-Connecting-IP")
              or (request.headers.get("X-Forwarded-For") or "").split(",")[0]
              or request.remote_addr or "")
        return ip.strip()[:64] or "?"
    except Exception:
        return "?"


def _run(fn):
    conn = get_db_connection()
    if not conn:
        return None
    cur = conn.cursor()
    try:
        _ensure(cur)
        out = fn(cur)
        conn.commit()
        return out
    except Exception as e:
        conn.rollback()
        print("login_guard error:", e)
        return None
    finally:
        cur.close(); conn.close()


def paused(scope: str, ident: str) -> bool:
    ident = (ident or "").strip().lower()[:255]
    ip = client_ip()
    def q(cur):
        cur.execute("""SELECT COUNT(*) FROM login_failures
                        WHERE scope=%s AND ident=%s AND ip=%s
                          AND at > NOW() - make_interval(mins => %s)""",
                    (scope, ident, ip, WINDOW_MIN))
        return cur.fetchone()[0] >= MAX_FAILS
    return bool(_run(q))


def failed(scope: str, ident: str) -> None:
    ident = (ident or "").strip().lower()[:255]
    ip = client_ip()
    def q(cur):
        cur.execute("INSERT INTO login_failures (scope, ident, ip) VALUES (%s, %s, %s)", (scope, ident, ip))
        cur.execute("DELETE FROM login_failures WHERE at < NOW() - INTERVAL '2 days'")
    _run(q)


def cleared(scope: str, ident: str) -> None:
    ident = (ident or "").strip().lower()[:255]
    ip = client_ip()
    _run(lambda cur: cur.execute("DELETE FROM login_failures WHERE scope=%s AND ident=%s AND ip=%s",
                                 (scope, ident, ip)))
