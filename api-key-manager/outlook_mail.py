"""
outlook_mail.py — Microsoft 365 (Outlook) mailbox access for reply tracking
(2026-10-09). No Flask here: sign-in URLs, token exchange/refresh and the
two Microsoft Graph reads the reply checker needs.

How it's used (see mailbox_routes.py): each staff member connects their OWN
mailbox once (delegated sign-in, read-only Mail.Read). Every 2 minutes the
portal lists mail received since the last check, and only messages FROM
someone the business emailed are kept — everything else is skipped and
never stored. PhiXtra never sends, moves or deletes mail.

The app is registered in PhiXtra's Microsoft Entra tenant as "Multiple
organizations" (business accounts only), so sign-in goes through the
/organizations endpoint. Settings in .env:
    MS_CLIENT_ID, MS_CLIENT_SECRET, MS_CLIENT_SECRET_EXPIRES (YYYY-MM-DD)
"""
import os
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

TIMEOUT = 20
AUTHORITY = "https://login.microsoftonline.com/organizations"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "openid email profile offline_access User.Read Mail.Read"
REDIRECT_URI = os.getenv("MS_REDIRECT_URI", "https://portal.phixtra.com/integrations/microsoft/callback")


class GraphError(Exception):
    """code: 'reconnect' (sign-in no longer valid — the person must connect
    again), 'consent' (their organisation's IT must approve PhiXtra first),
    'secret' (PhiXtra's own app key expired/wrong), or 'temporary'."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def client_id() -> str:
    return os.getenv("MS_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("MS_CLIENT_SECRET", "").strip()


def configured() -> bool:
    return bool(client_id() and _client_secret())


def secret_expires() -> date | None:
    raw = os.getenv("MS_CLIENT_SECRET_EXPIRES", "").strip()
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def secret_expired() -> bool:
    exp = secret_expires()
    return bool(exp and date.today() >= exp)


def auth_url(state: str) -> str:
    return f"{AUTHORITY}/oauth2/v2.0/authorize?" + urlencode({
        "client_id": client_id(), "response_type": "code", "redirect_uri": REDIRECT_URI,
        "response_mode": "query", "scope": SCOPES, "state": state, "prompt": "select_account",
    })


def admin_consent_url() -> str:
    """Link a business's IT admin opens once to approve PhiXtra for their
    whole organisation (needed until PhiXtra's publisher is verified)."""
    return f"{AUTHORITY}/v2.0/adminconsent?" + urlencode({
        "client_id": client_id(), "redirect_uri": REDIRECT_URI, "state": "adminconsent",
        "scope": "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read offline_access",
    })


def _token_error(data: dict) -> GraphError:
    err = (data.get("error") or "")
    desc = (data.get("error_description") or "")
    codes = data.get("error_codes") or []
    if 7000215 in codes or 7000222 in codes or "AADSTS7000215" in desc or "AADSTS7000222" in desc:
        return GraphError("secret", "PhiXtra's Microsoft connection key has expired or is wrong.")
    if err == "consent_required" or 65001 in codes or "AADSTS65001" in desc or "AADSTS90094" in desc:
        return GraphError("consent", "Your organisation's IT admin needs to approve PhiXtra first.")
    if err in ("invalid_grant", "interaction_required"):
        return GraphError("reconnect", "Microsoft no longer accepts this sign-in. Connect the mailbox again.")
    return GraphError("temporary", f"Microsoft said: {desc.splitlines()[0][:200] if desc else err or 'unknown error'}")


def _token_request(fields: dict) -> dict:
    fields = dict(fields, client_id=client_id(), client_secret=_client_secret(), scope=SCOPES)
    try:
        r = requests.post(f"{AUTHORITY}/oauth2/v2.0/token", data=fields, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise GraphError("temporary", f"Could not reach Microsoft: {e}")
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in data:
        raise _token_error(data)
    data["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in") or 3600) - 120)
    return data


def exchange_code(code: str) -> dict:
    return _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})


def refresh(refresh_token: str) -> dict:
    """Microsoft sends a new refresh token each time — the caller must save it."""
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})


def _get(token: str, url: str, params: dict = None) -> dict:
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.body-content-type="text"'})
    except requests.RequestException as e:
        raise GraphError("temporary", f"Could not reach Microsoft: {e}")
    if r.status_code == 401:
        raise GraphError("reconnect", "Microsoft no longer accepts this sign-in. Connect the mailbox again.")
    if r.status_code == 403:
        raise GraphError("reconnect", "PhiXtra isn't allowed to read this mailbox. Connect it again and accept the permission.")
    if r.status_code != 200:
        raise GraphError("temporary", f"Microsoft returned {r.status_code}: {r.text[:200]}")
    return r.json()


def me(token: str) -> dict:
    d = _get(token, f"{GRAPH}/me", {"$select": "id,displayName,mail,userPrincipalName"})
    return {"id": d.get("id"), "name": d.get("displayName") or "",
            "email": (d.get("mail") or d.get("userPrincipalName") or "").lower()}


def messages_since(token: str, since: datetime, limit: int = 500) -> list:
    """Mail received at or after `since`, oldest first, in every folder
    (so replies that a mail rule moved still count). Only the fields the
    reply checker needs."""
    out = []
    url = f"{GRAPH}/me/messages"
    params = {
        "$filter": f"receivedDateTime ge {since.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "$orderby": "receivedDateTime asc",
        "$select": "id,from,subject,bodyPreview,receivedDateTime,webLink,isDraft",
        "$top": "50",
    }
    while url and len(out) < limit:
        d = _get(token, url, params)
        out.extend(m for m in d.get("value", []) if not m.get("isDraft"))
        url, params = d.get("@odata.nextLink"), None
    return out[:limit]


_AUTO_SUBJECT = re.compile(r"^\s*(automatic reply|auto(matic)?[- ]?reply|autoreply|out of (the )?office|"
                           r"undeliverable|delivery status notification|mail delivery (failed|subsystem)|"
                           r"returned mail|delivery has failed|r[ée]ponse automatique|auto:)", re.I)


def is_automatic(token: str, message: dict) -> bool:
    """Out-of-office notes, bounces and other machine replies aren't real
    replies. Subject first (cheap); then the message headers."""
    if _AUTO_SUBJECT.search(message.get("subject") or ""):
        return True
    sender = (((message.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
    if sender.startswith(("mailer-daemon@", "postmaster@", "no-reply@", "noreply@")):
        return True
    try:
        d = _get(token, f"{GRAPH}/me/messages/{message['id']}", {"$select": "internetMessageHeaders"})
    except GraphError:
        return False
    for h in d.get("internetMessageHeaders") or []:
        name, value = (h.get("name") or "").lower(), (h.get("value") or "").lower()
        if name == "auto-submitted" and value and value != "no":
            return True
        if name in ("x-autoreply", "x-autorespond"):
            return True
        if name == "precedence" and value in ("auto_reply", "bulk", "junk"):
            return True
    return False


def reply_lines(preview: str, limit: int = 400) -> str:
    """The first few lines of what they wrote — stops at the quoted email
    underneath ("On … wrote:", "From:", "-----Original Message-----")."""
    text = (preview or "").replace("\r", "")
    cut = re.search(r"(\n\s*>|\bOn .{5,80}wrote:|\bFrom: |-{3,}\s*Original Message|_{8,}|\bSent from my )", text)
    if cut:
        text = text[:cut.start()]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")
