"""STAGING mail trap (8 Nov 2026, item 11 Phase 2).

Every PhiXtra service on the staging server sends email to 127.0.0.1:2525
(SMTP_HOST/SMTP_PORT in each .env). This catches ALL of it:
  • every message is saved (.eml) and listed in index.jsonl → Admin › Staging mail
  • a message is passed on to the real mail server ONLY when every recipient is
    on the allow list AND relay settings are present; otherwise it is held.
So staging can never email a real customer.

Accepts plain SMTP and STARTTLS (self-signed), and any login — senders differ.
Settings: /etc/phixtra-mail-trap.env
  MAIL_TRAP_DIR, MAIL_TRAP_ALLOW (comma list), RELAY_HOST, RELAY_PORT,
  RELAY_USER, RELAY_PASSWORD (relay optional: blank = hold everything)
"""
import email, email.policy, json, os, smtplib, ssl, time, uuid
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

DIR = os.getenv("MAIL_TRAP_DIR", "/srv/phixtra/mail-trap")
ALLOW = {a.strip().lower() for a in os.getenv("MAIL_TRAP_ALLOW", "").split(",") if a.strip()}
RELAY = {k: os.getenv(k, "") for k in ("RELAY_HOST", "RELAY_PORT", "RELAY_USER", "RELAY_PASSWORD")}
CERT, KEY = os.path.join(DIR, "trap.crt"), os.path.join(DIR, "trap.key")


def _relay(mail_from, rcpts, data):
    if not (RELAY["RELAY_HOST"] and RELAY["RELAY_USER"]):
        return "held: no relay settings"
    port = int(RELAY["RELAY_PORT"] or 587)
    s = (smtplib.SMTP_SSL(RELAY["RELAY_HOST"], port, timeout=30) if port == 465
         else smtplib.SMTP(RELAY["RELAY_HOST"], port, timeout=30))
    if port != 465:
        s.starttls(context=ssl.create_default_context())
    s.login(RELAY["RELAY_USER"], RELAY["RELAY_PASSWORD"])
    s.sendmail(mail_from, rcpts, data)
    s.quit()
    return "sent"


class Handler:
    async def handle_DATA(self, server, session, envelope):
        rcpts = [r.lower() for r in envelope.rcpt_tos]
        msg = email.message_from_bytes(envelope.content, policy=email.policy.default)
        mid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with open(os.path.join(DIR, "msgs", mid + ".eml"), "wb") as f:
            f.write(envelope.content)
        blocked = [r for r in rcpts if r not in ALLOW]
        if blocked:
            result = "held: not on allow list (" + ", ".join(blocked) + ")"
        else:
            try:
                result = _relay(envelope.mail_from, rcpts, envelope.content)
            except Exception as e:
                result = f"relay failed: {e}"[:300]
        with open(os.path.join(DIR, "index.jsonl"), "a") as f:
            f.write(json.dumps({"id": mid, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "from": envelope.mail_from, "to": rcpts,
                                "subject": str(msg.get("Subject", ""))[:300], "result": result}) + "\n")
        return "250 OK (staging mail trap)"


def _auth(server, session, envelope, mechanism, auth_data):
    return AuthResult(success=True)


def main():
    os.makedirs(os.path.join(DIR, "msgs"), exist_ok=True)
    tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    tls.load_cert_chain(CERT, KEY)
    c = Controller(Handler(), hostname="127.0.0.1", port=2525, tls_context=tls, require_starttls=False,
                   authenticator=_auth, auth_require_tls=False)
    c.start()
    print(f"mail trap on 127.0.0.1:2525 — allow list {sorted(ALLOW)}; relay {'on' if RELAY['RELAY_HOST'] else 'off'}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
