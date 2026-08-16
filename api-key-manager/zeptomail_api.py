"""
Thin wrapper around ZeptoMail's transactional-email HTTP API. Used to send
bulk marketing campaigns (see "EMAIL CAMPAIGNS" section in portal_routes.py),
as opposed to portal_utils.py's send_email(), which is single-recipient/
blocking SMTP reserved for transactional mail (password resets, etc) — kept
on a separate Send Mail Token/Mail Agent so a marketing deliverability
problem never risks that transactional path.

Auth here is a static per-tenant "Send Mail Token" (Authorization:
Zoho-enczapikey <token>), NOT OAuth2 — ZeptoMail's domain/Mail Agent
management endpoints do require OAuth2 with separate scopes
(Zeptomail.Domains.CREATE etc), but that's not used here: domains and Mail
Agents are created manually in the ZeptoMail dashboard by an admin, and the
resulting Send Mail Token is entered per tenant via the admin panel
(portal_admin_routes.py) and stored encrypted in email_domains.

Region matches the existing transactional SMTP host (smtp.zeptomail.eu) —
ZeptoMail accounts are region-locked, so a .com token will not work against
the .eu API host or vice versa.
"""
import requests

TIMEOUT = 20
API_URL = "https://api.zeptomail.eu/v1.1/email"


def send_email(send_mail_token: str, from_email: str, from_name: str,
                to_email: str, to_name: str, subject: str, html_body: str):
    """Sends a single email via ZeptoMail's HTTP API. Returns (ok, error)."""
    payload = {
        "from": {"address": from_email, "name": from_name or ""},
        "to": [{"email_address": {"address": to_email, "name": to_name or ""}}],
        "subject": subject,
        "htmlbody": html_body,
    }
    try:
        r = requests.post(
            API_URL,
            json=payload,
            headers={
                "Authorization": f"Zoho-enczapikey {send_mail_token}",
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"Could not reach ZeptoMail: {e}"

    if r.status_code in (200, 201):
        return True, None

    try:
        data = r.json()
        error = data.get("message") or data.get("error", {}).get("message") or r.text[:200]
    except ValueError:
        error = r.text[:200]
    return False, f"ZeptoMail returned {r.status_code}: {error}"
