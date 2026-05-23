import os
import secrets
import bcrypt
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def make_token(length: int = 48) -> str:
    return secrets.token_urlsafe(length)


def utc_now_naive():
    # store in MySQL DATETIME (naive), but treat as UTC
    return datetime.utcnow()


def send_email(to_email: str, subject: str, html_body: str, text_body: str | None = None) -> bool:
    """Send an email. Returns True on success, False on failure.
    Failures are printed to server logs but never raise exceptions so calling
    code decides how to surface the error to the customer."""
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_email = os.getenv("SMTP_FROM", user or "no-reply@phixtra.com").strip()
    use_tls = os.getenv("SMTP_USE_TLS", "1").strip() == "1"
    use_ssl = os.getenv("SMTP_USE_SSL", "0").strip() == "1"  # port 465 SSL connections

    if not host or not from_email:
        print("⚠️ SMTP not configured; skipping email:", subject)
        return False

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text_body or "Please view this email in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    try:
        if use_ssl:
            # Port 465: wrap connection in SSL immediately (no STARTTLS)
            import ssl as _ssl
            ctx = _ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as server:
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                if use_tls:
                    server.starttls()
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        return True
    except Exception as e:
        print("⚠️ send_email failed:", e)
        return False


def next_invoice_number() -> str:
    # Example: PHX-20260214-8F3A2C
    stamp = datetime.utcnow().strftime("%Y%m%d")
    rand = secrets.token_hex(3).upper()
    return f"PHX-{stamp}-{rand}"


TOKENS_PER_CREDIT = 5000


def credits_to_tokens(credits: int) -> int:
    return int(credits) * TOKENS_PER_CREDIT


def tokens_to_credits(tokens: int) -> float:
    return float(tokens) / float(TOKENS_PER_CREDIT)


def calc_vat(amount_pence: int, vat_rate: float) -> int:
    # vat_rate e.g. 20.00
    return int(round(amount_pence * (float(vat_rate) / 100.0)))


def money_fmt(pence: int, currency: str = "gbp") -> str:
    # simple format
    return f"£{pence/100:.2f}" if currency.lower() == "gbp" else f"{pence/100:.2f} {currency.upper()}"
