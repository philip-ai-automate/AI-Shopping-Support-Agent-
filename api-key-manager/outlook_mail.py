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
# AI outreach drafts (2026-11-04, PhiXtra pilot, outreach_drafts.py): the
# portal may also SAVE drafts in the person's Outlook. It never sends: a
# person opens Drafts and presses Send. Asked for only by businesses in the
# pilot; everyone else keeps read-only access.
DRAFT_SCOPES = "openid email profile offline_access User.Read Mail.ReadWrite"
# Microsoft Bookings (2026-10-18, bookings_routes.py): a separate sign-in that
# can only read a business's Bookings appointments, never its mail.
BOOKINGS_SCOPES = "openid email profile offline_access User.Read Bookings.Read.All"
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


def auth_url(state: str, scopes: str = SCOPES) -> str:
    return f"{AUTHORITY}/oauth2/v2.0/authorize?" + urlencode({
        "client_id": client_id(), "response_type": "code", "redirect_uri": REDIRECT_URI,
        "response_mode": "query", "scope": scopes, "state": state, "prompt": "select_account",
    })


def admin_consent_url(scopes: str = SCOPES) -> str:
    """Link a business's IT admin opens once to approve PhiXtra for their
    whole organisation (needed until PhiXtra's publisher is verified)."""
    graph = " ".join(f"https://graph.microsoft.com/{s}" for s in scopes.split()
                     if s not in ("openid", "email", "profile", "offline_access"))
    return f"{AUTHORITY}/v2.0/adminconsent?" + urlencode({
        "client_id": client_id(), "redirect_uri": REDIRECT_URI, "state": "adminconsent",
        "scope": graph + " offline_access",
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


def _token_request(fields: dict, scopes: str = SCOPES) -> dict:
    fields = dict(fields, client_id=client_id(), client_secret=_client_secret(), scope=scopes)
    try:
        r = requests.post(f"{AUTHORITY}/oauth2/v2.0/token", data=fields, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise GraphError("temporary", f"Could not reach Microsoft: {e}")
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in data:
        raise _token_error(data)
    data["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in") or 3600) - 120)
    return data


def exchange_code(code: str, scopes: str = SCOPES) -> dict:
    return _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}, scopes)


def refresh(refresh_token: str, scopes: str = SCOPES) -> dict:
    """Microsoft sends a new refresh token each time — the caller must save it."""
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token}, scopes)


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
        "$select": "id,from,toRecipients,subject,bodyPreview,receivedDateTime,webLink,isDraft",
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


# ── Drafts (AI outreach pilot, 2026-11-04) — never sends ──────────────────

def _write(token: str, method: str, url: str, body: dict = None) -> dict:
    try:
        r = requests.request(method, url, json=body, timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise GraphError("temporary", f"Could not reach Microsoft: {e}")
    if r.status_code == 401:
        raise GraphError("reconnect", "Microsoft no longer accepts this sign-in. Connect the mailbox again.")
    if r.status_code == 403:
        raise GraphError("drafts", "PhiXtra isn't allowed to save drafts in this mailbox yet. "
                                   "Reconnect it on Settings › Integration › Microsoft 365 and accept the permission.")
    if r.status_code == 404:
        raise GraphError("gone", "That draft is no longer in Outlook (it was sent or deleted).")
    if r.status_code not in (200, 201, 202, 204):
        raise GraphError("temporary", f"Microsoft returned {r.status_code}: {r.text[:200]}")
    return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else {}


def _draft_out(d: dict) -> dict:
    return {"id": d.get("id"), "web_link": d.get("webLink"), "conversation_id": d.get("conversationId"),
            "body_html": ((d.get("body") or {}).get("content") or "")}


def create_draft(token: str, to_email: str, to_name: str, subject: str, html: str, categories: list) -> dict:
    """A new email in the person's Drafts folder."""
    d = _write(token, "POST", f"{GRAPH}/me/messages", {
        "subject": subject, "body": {"contentType": "HTML", "content": html},
        "toRecipients": [{"emailAddress": {"address": to_email, "name": to_name or to_email}}],
        "categories": categories,
    })
    return _draft_out(d)


def create_reply_draft(token: str, sent_message_id: str, to_email: str, to_name: str, categories: list) -> dict:
    """A reply draft to an email the person sent (same thread). Outlook adds
    the quoted earlier email; the caller puts its own words above it.
    Addressed to the buyer explicitly (a reply to your own sent email would
    otherwise go back to yourself)."""
    d = _write(token, "POST", f"{GRAPH}/me/messages/{sent_message_id}/createReply", {})
    _write(token, "PATCH", f"{GRAPH}/me/messages/{d['id']}", {
        "toRecipients": [{"emailAddress": {"address": to_email, "name": to_name or to_email}}],
        "categories": categories,
    })
    return _draft_out(d)


def update_draft(token: str, message_id: str, html: str = None, subject: str = None, categories: list = None) -> dict:
    body = {}
    if html is not None:
        body["body"] = {"contentType": "HTML", "content": html}
    if subject is not None:
        body["subject"] = subject
    if categories is not None:
        body["categories"] = categories
    return _write(token, "PATCH", f"{GRAPH}/me/messages/{message_id}", body) if body else {}


def set_inline_image(token: str, message_id: str, data: bytes, mime: str, content_id: str, name: str) -> None:
    """Put an image inside a draft (shown where the body has <img src="cid:...">),
    replacing an earlier one with the same content id."""
    import base64
    d = _get(token, f"{GRAPH}/me/messages/{message_id}/attachments", {"$select": "id,name,isInline"})
    d = {"value": [dict(a, contentId=(_get(token, f"{GRAPH}/me/messages/{message_id}/attachments/{a['id']}").get("contentId") or "")) for a in d.get("value", []) if a.get("isInline")]}
    for a in d.get("value", []):
        if (a.get("contentId") or "").strip("<>") == content_id:
            _write(token, "DELETE", f"{GRAPH}/me/messages/{message_id}/attachments/{a['id']}")
    if data:
        _write(token, "POST", f"{GRAPH}/me/messages/{message_id}/attachments", {
            "@odata.type": "#microsoft.graph.fileAttachment", "name": name, "contentType": mime,
            "contentBytes": base64.b64encode(data).decode(), "isInline": True, "contentId": content_id,
        })


def remove_inline_image(token: str, message_id: str, content_id: str) -> None:
    set_inline_image(token, message_id, b"", "", content_id, "")


def delete_draft(token: str, message_id: str) -> None:
    """Moves an unsent draft to Deleted Items. Already gone = fine. Checks
    it is still a draft first, so a sent email is never touched."""
    try:
        r = requests.get(f"{GRAPH}/me/messages/{message_id}", params={"$select": "isDraft"},
                         headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise GraphError("temporary", f"Could not reach Microsoft: {e}")
    if r.status_code == 404:
        return
    if r.status_code != 200 or not r.json().get("isDraft"):
        return
    try:
        _write(token, "DELETE", f"{GRAPH}/me/messages/{message_id}")
    except GraphError as e:
        if e.code != "gone":
            raise


# ── Microsoft Bookings (read only) ─────────────────────────────────────────

def booking_businesses(token: str) -> list:
    """The Bookings pages this person manages: [{"id", "name"}]. The id is
    the Bookings page's own address (e.g. TalktoUs1@phixtra.com)."""
    d = _get(token, f"{GRAPH}/solutions/bookingBusinesses")
    return [{"id": b.get("id"), "name": b.get("displayName") or b.get("id")} for b in d.get("value", []) if b.get("id")]


def booking_appointments(token: str, business_id: str, start: datetime, end: datetime) -> list:
    """Every appointment on one Bookings page between start and end. Raises
    GraphError if any page of the list can't be read, so a half-read list is
    never mistaken for cancellations."""
    from urllib.parse import quote
    fmt = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{GRAPH}/solutions/bookingBusinesses/{quote(business_id, safe='@.')}/calendarView"
    params = {"start": fmt(start), "end": fmt(end)}
    out = []
    while url:
        d = _get(token, url, params)
        out.extend(d.get("value", []))
        url, params = d.get("@odata.nextLink"), None
        if len(out) > 5000:
            raise GraphError("temporary", "More than 5,000 appointments — stopped reading.")
    return out
