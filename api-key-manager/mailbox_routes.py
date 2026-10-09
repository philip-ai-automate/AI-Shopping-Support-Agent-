"""
mailbox_routes.py — Integration › Microsoft 365 (Outlook): each staff member
connects their OWN mailbox so replies to the business's campaign and
sequence emails land on the lead by themselves (2026-10-09, approved:
placement Integration, each staff their own mailbox, show the reply lines).

  GET  /integrations/microsoft                    the page (own mailbox; owner/IT
                                                  also see every team mailbox)
  GET  /integrations/microsoft/connect            off to Microsoft sign-in
  GET  /integrations/microsoft/callback           back from Microsoft (also where
                                                  an IT admin lands after approving)
  POST /integrations/microsoft/check-now          check my mailbox now
  POST /integrations/microsoft/disconnect         disconnect my mailbox
  POST /integrations/microsoft/<id>/disconnect    owner/IT: disconnect anyone's

A background loop (every minute, rows claimed so the two portal workers
never double up) checks each connected mailbox every 2 minutes. A message
counts as a reply only if it's FROM someone this business emailed in the
60 days before it arrived; out-of-office notes and bounces are ignored.
A reply does exactly what the lead's "They replied" button does: history
entry, New → Contacted, running sequences stop, campaign report Replied.
Nothing else in the mailbox is stored. Microsoft access: outlook_mail.py.
"""
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2.extras
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

import email_outreach
import outlook_mail as M
from db import get_db_connection
from feature_access import public_route, team_feature
from portal_routes import (
    _current_actor, _customer_id, _decrypt_key, _email_lead_contacted, _encrypt_key, _get_customer,
    _get_tenant_plan, _inject_granted_features, _plan_grants_feature, _require_login,
    _require_plan_sub_feature, _require_team_permission, _segments_see_all, _seq_stop_matching,
)

KEY = "campaigns_email.mailbox_connect"
CHECK_EVERY = timedelta(minutes=2)
LOOK_BACK_FIRST = timedelta(days=14)    # first check after connecting
MATCH_WINDOW = timedelta(days=60)       # reply must come within 60 days of our email

mailbox_bp = Blueprint("mailbox", __name__)
mailbox_bp.context_processor(_inject_granted_features)


# ── who / what ─────────────────────────────────────────────────────────────

def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _reply_to_address(customer: dict) -> str:
    """Where this person's campaign emails ask people to reply (the default
    Reply-To is the logged-in staff member's own address)."""
    tm_id = session.get("team_member_id")
    if not tm_id:
        return (customer.get("email") or "").lower()
    conn, cur = _db()
    try:
        cur.execute("SELECT email FROM team_members WHERE id=%s", (int(tm_id),))
        row = cur.fetchone()
        return ((row or {}).get("email") or "").lower()
    finally:
        cur.close(); conn.close()


def _my_mailbox(tenant_id: int, owner_key: str):
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM mailbox_connections WHERE tenant_id=%s AND owner_key=%s AND provider='microsoft'",
                    (tenant_id, owner_key))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def team_mailboxes(tenant_id: int) -> list:
    conn, cur = _db()
    try:
        cur.execute("""SELECT * FROM mailbox_connections WHERE tenant_id=%s AND provider='microsoft'
                       AND status <> 'disconnected' ORDER BY owner_label, id""", (tenant_id,))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def card_summary(customer: dict) -> dict:
    """What the Integration page's Microsoft card shows."""
    tenant_id = int(customer["tenant_id"])
    mine = _my_mailbox(tenant_id, _current_actor(customer)["key"])
    team = team_mailboxes(tenant_id) if _segments_see_all() else []
    return {"mine": mine if mine and mine["status"] != "disconnected" else None,
            "team_count": sum(1 for m in team if m["status"] == "active"),
            "attention": sum(1 for m in team if m["status"] != "active"),
            "is_manager": _segments_see_all(), "configured": M.configured(), "expired": M.secret_expired()}


def _plan_gate(customer):
    """None if the business's plan includes Outlook reply tracking, else a redirect."""
    return _require_plan_sub_feature(customer, KEY, "Outlook reply tracking")


# ── pages ──────────────────────────────────────────────────────────────────

@mailbox_bp.route("/integrations/microsoft", methods=["GET"])
@team_feature(KEY)
def page():
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _plan_gate(customer) or _require_team_permission(KEY)
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    me_key = _current_actor(customer)["key"]
    mine = _my_mailbox(tenant_id, me_key)
    if mine and mine["status"] == "disconnected":
        mine = None
    reply_to = _reply_to_address(customer)
    is_manager = _segments_see_all()
    return render_template(
        "portal/mailbox_microsoft.html", customer=customer, mine=mine, reply_to=reply_to,
        mismatch=bool(mine and reply_to and mine["email"] and mine["email"].lower() != reply_to),
        is_manager=is_manager, team=team_mailboxes(tenant_id) if is_manager else [],
        configured=M.configured(), expired=M.secret_expired(), consent_url=M.admin_consent_url(),
        me_key=me_key,
    )


@mailbox_bp.route("/integrations/microsoft/connect", methods=["GET"])
@team_feature(KEY)
def connect():
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _plan_gate(customer) or _require_team_permission(KEY)
    if r:
        return r
    if not M.configured() or M.secret_expired():
        flash("The Microsoft connection isn't available right now. Please try again later.", "warning")
        return redirect(url_for("mailbox.page"))
    state = secrets.token_urlsafe(24)
    session["ms_oauth_state"] = state
    return redirect(M.auth_url(state))


@mailbox_bp.route("/integrations/microsoft/callback", methods=["GET"])
@public_route   # an IT admin approving PhiXtra for their organisation lands here without a portal login
def callback():
    if request.args.get("state") == "adminconsent":
        ok = request.args.get("admin_consent", "").lower() == "true"
        return render_template("portal/mailbox_consent_done.html", ok=ok,
                               error=request.args.get("error_description", "")[:300])
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _plan_gate(customer) or _require_team_permission(KEY)
    if r:
        return r
    expected = session.pop("ms_oauth_state", None)
    if not expected or request.args.get("state") != expected:
        flash("That sign-in link had expired. Please press Connect again.", "warning")
        return redirect(url_for("mailbox.page"))
    if request.args.get("error"):
        desc = request.args.get("error_description", "")
        # "Needs admin approval" can also come back as access_denied, so check it
        # first; a person simply declining (AADSTS65004) is a plain cancel.
        if (request.args.get("error") == "consent_required" or "AADSTS65001" in desc or "AADSTS90094" in desc
                or "AADSTS90008" in desc or "admin" in desc.lower()):
            session["ms_needs_consent"] = True
            flash("Your organisation's IT admin needs to approve PhiXtra first. Send them the link below.", "warning")
        elif request.args.get("error") == "access_denied":
            flash("Microsoft sign-in was cancelled. Nothing was connected.", "warning")
        else:
            flash("Microsoft didn't connect the mailbox: " + (desc.splitlines()[0][:200] if desc else request.args.get("error")), "danger")
        return redirect(url_for("mailbox.page"))
    try:
        tok = M.exchange_code(request.args.get("code", ""))
        who = M.me(tok["access_token"])
    except M.GraphError as e:
        if e.code == "consent":
            session["ms_needs_consent"] = True
        flash(str(e), "danger")
        return redirect(url_for("mailbox.page"))
    actor = _current_actor(customer)
    tenant_id = int(customer["tenant_id"])
    conn, cur = _db()
    try:
        cur.execute("""
            INSERT INTO mailbox_connections (tenant_id, owner_key, owner_label, provider, email, display_name,
                   provider_user_id, refresh_token_enc, access_token_enc, access_expires_at, watermark, status,
                   last_error, next_check_at, connected_at, updated_at)
            VALUES (%s,%s,%s,'microsoft',%s,%s,%s,%s,%s,%s,NULL,'active',NULL,NOW(),NOW(),NOW())
            ON CONFLICT (tenant_id, owner_key, provider) DO UPDATE SET
                owner_label=EXCLUDED.owner_label, email=EXCLUDED.email, display_name=EXCLUDED.display_name,
                provider_user_id=EXCLUDED.provider_user_id, refresh_token_enc=EXCLUDED.refresh_token_enc,
                access_token_enc=EXCLUDED.access_token_enc, access_expires_at=EXCLUDED.access_expires_at,
                status='active', last_error=NULL, next_check_at=NOW(), updated_at=NOW(),
                -- a different mailbox starts fresh; the same one carries on where it was
                watermark = CASE WHEN mailbox_connections.provider_user_id = EXCLUDED.provider_user_id
                                 THEN mailbox_connections.watermark END,
                connected_at = CASE WHEN mailbox_connections.provider_user_id = EXCLUDED.provider_user_id
                                    THEN mailbox_connections.connected_at ELSE NOW() END
            RETURNING id""",
            (tenant_id, actor["key"], actor["label"], who["email"], who["name"], who["id"],
             _encrypt_key(tok.get("refresh_token") or ""), _encrypt_key(tok["access_token"]), tok["expires_at"]))
        mb_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        cur.close(); conn.close()
    session.pop("ms_needs_consent", None)
    threading.Thread(target=check_mailbox, args=(mb_id,), daemon=True).start()
    flash(f"Connected {who['email']}. Replies from people you've emailed will now show on their lead "
          f"(checked every 2 minutes; replies from the last 14 days are being picked up now).", "success")
    return redirect(url_for("mailbox.page"))


@mailbox_bp.route("/integrations/microsoft/check-now", methods=["POST"])
@team_feature(KEY)
def check_now():
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _plan_gate(customer) or _require_team_permission(KEY)
    if r:
        return r
    mine = _my_mailbox(int(customer["tenant_id"]), _current_actor(customer)["key"])
    if not mine or mine["status"] == "disconnected":
        flash("Connect your mailbox first.", "warning")
        return redirect(url_for("mailbox.page"))
    res = check_mailbox(mine["id"], force=True)
    if res.get("ok"):
        n = res.get("found", 0)
        flash(f"Checked just now: {n} new repl{'y' if n == 1 else 'ies'} added to leads." if n else
              "Checked just now: no new replies from people you've emailed.", "success")
    else:
        flash(res.get("error") or "Couldn't check the mailbox. Please try again.", "danger")
    return redirect(url_for("mailbox.page"))


def _disconnect(tenant_id: int, mb_id: int):
    conn, cur = _db()
    try:
        cur.execute("""UPDATE mailbox_connections SET status='disconnected', refresh_token_enc=NULL,
                           access_token_enc=NULL, access_expires_at=NULL, updated_at=NOW()
                       WHERE id=%s AND tenant_id=%s""", (mb_id, tenant_id))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()


@mailbox_bp.route("/integrations/microsoft/disconnect", methods=["POST"])
@team_feature(KEY)
def disconnect_mine():
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _require_team_permission(KEY)   # no plan check: anyone may always disconnect
    if r:
        return r
    mine = _my_mailbox(int(customer["tenant_id"]), _current_actor(customer)["key"])
    if mine:
        _disconnect(int(customer["tenant_id"]), mine["id"])
    flash("Your mailbox is disconnected. PhiXtra no longer reads it. Replies already on leads stay there.", "success")
    return redirect(url_for("mailbox.page"))


@mailbox_bp.route("/integrations/microsoft/<int:mb_id>/disconnect", methods=["POST"])
@team_feature(KEY)
def disconnect_other(mb_id: int):
    r = _require_login()
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _require_team_permission(KEY)
    if r:
        return r
    if not _segments_see_all():
        flash("Only the owner or someone who manages the team can disconnect another person's mailbox.", "danger")
        return redirect(url_for("mailbox.page"))
    if _disconnect(int(customer["tenant_id"]), mb_id):
        flash("Mailbox disconnected.", "success")
    return redirect(url_for("mailbox.page"))


# ── the reply checker ──────────────────────────────────────────────────────

def _still_allowed(cur, mb) -> str | None:
    """Reason to skip this mailbox right now, or None. Losing the plan or the
    Roles tick pauses checking (nothing is deleted); a removed team member's
    mailbox is disconnected."""
    if not _plan_grants_feature(_get_tenant_plan(mb["tenant_id"]), KEY):
        return "Paused: the business's plan doesn't include Outlook reply tracking."
    if mb["owner_key"].startswith("team:"):
        cur.execute("""SELECT tm.is_active, r.permissions FROM team_members tm
                       LEFT JOIN tenant_roles r ON r.id = tm.role_id AND r.tenant_id = tm.tenant_id
                       WHERE tm.id=%s AND tm.tenant_id=%s""", (int(mb["owner_key"].split(":")[1]), mb["tenant_id"]))
        tm = cur.fetchone()
        if not tm or not tm["is_active"]:
            return "disconnect"
        if not (tm["permissions"] or {}).get(KEY):
            return "Paused: this person's role no longer has the Outlook mailbox tick."
    return None


def _access_token(cur, mb) -> str:
    now = datetime.now(timezone.utc)
    if mb["access_token_enc"] and mb["access_expires_at"] and mb["access_expires_at"] > now:
        tok = _decrypt_key(mb["access_token_enc"])
        if tok:
            return tok
    rt = _decrypt_key(mb["refresh_token_enc"] or "")
    if not rt:
        raise M.GraphError("reconnect", "Microsoft no longer accepts this sign-in. Connect the mailbox again.")
    t = M.refresh(rt)
    cur.execute("""UPDATE mailbox_connections SET access_token_enc=%s, access_expires_at=%s,
                       refresh_token_enc=COALESCE(%s, refresh_token_enc), updated_at=NOW() WHERE id=%s""",
                (_encrypt_key(t["access_token"]), t["expires_at"],
                 _encrypt_key(t["refresh_token"]) if t.get("refresh_token") else None, mb["id"]))
    cur.connection.commit()
    return t["access_token"]


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _record_reply(cur, mb, msg, sender: str, received: datetime, token: str) -> bool:
    """True if this message was a new reply and is now on the lead."""
    cur.execute("""SELECT r.*, c.name AS campaign_name FROM email_campaign_recipients r
                   JOIN email_campaigns c ON c.id = r.campaign_id
                   WHERE r.tenant_id=%s AND lower(r.email)=%s AND r.status='sent'
                     AND r.sent_at <= %s AND r.sent_at >= %s
                   ORDER BY r.sent_at DESC LIMIT 1""",
                (mb["tenant_id"], sender, received, received - MATCH_WINDOW))
    rec = cur.fetchone()
    if not rec:
        return False   # not someone this business emailed: skipped, never stored
    cur.execute("SELECT 1 FROM email_replies WHERE mailbox_id=%s AND message_id=%s", (mb["id"], msg["id"]))
    if cur.fetchone() or M.is_automatic(token, msg):
        return False
    lead_id, contact_id = rec["lead_id"], rec["contact_id"]
    if not lead_id and not contact_id:
        ctx = email_outreach.recipient_context(cur, mb["tenant_id"], sender)
        lead_id, contact_id = ctx.get("lead_id"), ctx.get("contact_id")
    cur.execute("""INSERT INTO email_replies (tenant_id, mailbox_id, message_id, campaign_id, recipient_id, lead_id,
                       contact_id, from_email, subject, snippet, web_link, received_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (mailbox_id, message_id) DO NOTHING RETURNING id""",
                (mb["tenant_id"], mb["id"], msg["id"], rec["campaign_id"], rec["id"], lead_id, contact_id, sender,
                 (msg.get("subject") or "")[:500], M.reply_lines(msg.get("bodyPreview")), msg.get("webLink"), received))
    row = cur.fetchone()
    if not row:
        return False   # the other portal worker got there first
    cur.execute("""INSERT INTO email_events (tenant_id, campaign_id, recipient_id, lead_id, contact_id, email, kind, detail, actor, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,'replied',%s,%s,%s) RETURNING id""",
                (mb["tenant_id"], rec["campaign_id"], rec["id"], lead_id, contact_id, sender, rec["campaign_name"],
                 f"Outlook ({mb['owner_label'] or mb['email']})", received))
    event_id = cur.fetchone()["id"]
    cur.execute("UPDATE email_replies SET event_id=%s WHERE id=%s", (event_id, row["id"]))
    cur.execute("UPDATE email_campaign_recipients SET replied_at=COALESCE(replied_at, %s) WHERE id=%s", (received, rec["id"]))
    _email_lead_contacted(cur, {"lead_id": lead_id, "tenant_id": mb["tenant_id"], "campaign_name": rec["campaign_name"]},
                          "Replied to the email (found in Outlook)")
    _seq_stop_matching(cur, mb["tenant_id"], "Replied", email=sender, lead_id=lead_id)
    if lead_id:
        cur.execute("UPDATE merchant_pipeline_leads SET updated_at=NOW() WHERE id=%s", (lead_id,))
    return True


def check_mailbox(mb_id: int, force: bool = False) -> dict:
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM mailbox_connections WHERE id=%s", (mb_id,))
        mb = cur.fetchone()
        if not mb or mb["status"] != "active":
            return {"ok": False, "error": "This mailbox isn't connected."}
        skip = _still_allowed(cur, mb)
        if skip == "disconnect":
            cur.execute("""UPDATE mailbox_connections SET status='disconnected', refresh_token_enc=NULL,
                               access_token_enc=NULL, last_error='The team member was removed.', updated_at=NOW()
                           WHERE id=%s""", (mb_id,))
            conn.commit()
            return {"ok": False, "error": "The team member was removed."}
        if skip:
            cur.execute("UPDATE mailbox_connections SET last_error=%s, last_checked_at=NOW() WHERE id=%s", (skip, mb_id))
            conn.commit()
            return {"ok": False, "error": skip}
        if not M.configured() or M.secret_expired():
            msg = "Paused: PhiXtra's Microsoft connection key needs renewing (PhiXtra is on it)."
            cur.execute("UPDATE mailbox_connections SET last_error=%s, last_checked_at=NOW() WHERE id=%s", (msg, mb_id))
            conn.commit()
            return {"ok": False, "error": msg}
        token = _access_token(cur, mb)
        since = mb["watermark"] or (mb["connected_at"] - LOOK_BACK_FIRST)
        messages = M.messages_since(token, since)
        found, newest = 0, mb["watermark"]
        own = (mb["email"] or "").lower()
        for msg in messages:
            received = _parse_dt(msg["receivedDateTime"])
            newest = max(newest, received) if newest else received
            sender = (((msg.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
            if not sender or sender == own:
                continue
            try:
                if _record_reply(cur, mb, msg, sender, received, token):
                    found += 1
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"⚠️ mailbox {mb_id} reply {msg.get('id', '')[:20]} error:", e)
        cur.execute("""UPDATE mailbox_connections SET watermark=COALESCE(%s, watermark), last_checked_at=NOW(),
                           last_error=NULL, replies_found=replies_found+%s, updated_at=NOW() WHERE id=%s""",
                    (newest, found, mb_id))
        conn.commit()
        return {"ok": True, "found": found}
    except M.GraphError as e:
        conn.rollback()
        if e.code in ("reconnect", "consent"):
            cur.execute("""UPDATE mailbox_connections SET status='needs_reconnect', last_error=%s,
                               last_checked_at=NOW(), updated_at=NOW() WHERE id=%s""", (str(e), mb_id))
        else:
            cur.execute("UPDATE mailbox_connections SET last_error=%s, last_checked_at=NOW() WHERE id=%s", (str(e), mb_id))
        conn.commit()
        return {"ok": False, "error": str(e)}
    except Exception as e:
        conn.rollback()
        print(f"⚠️ mailbox {mb_id} check error:", e)
        return {"ok": False, "error": "Couldn't check the mailbox. Please try again."}
    finally:
        cur.close(); conn.close()


def _due_mailboxes() -> list:
    """Claim mailboxes due a check; SKIP LOCKED + the moved next_check_at
    mean the two portal workers never check the same one. Practice
    (training) businesses are skipped, like the sequence sender; "Check
    for replies now" still works there."""
    conn, cur = _db()
    try:
        cur.execute("""UPDATE mailbox_connections SET next_check_at = NOW() + %s
                       WHERE id IN (SELECT m.id FROM mailbox_connections m
                                    JOIN tenants tn ON tn.id = m.tenant_id AND NOT COALESCE(tn.is_training, FALSE)
                                    WHERE m.status='active' AND m.next_check_at <= NOW()
                                    ORDER BY m.next_check_at LIMIT 50 FOR UPDATE OF m SKIP LOCKED)
                       RETURNING id""", (CHECK_EVERY,))
        ids = [r["id"] for r in cur.fetchall()]
        conn.commit()
        return ids
    finally:
        cur.close(); conn.close()


def _secret_reminder():
    """Email PhiXtra's admins 30 days, 7 days and on the day PhiXtra's
    Microsoft app key expires (Microsoft allows 24 months at most)."""
    exp = M.secret_expires()
    if not exp:
        return
    days = (exp - date.today()).days
    due = [t for t in (30, 7, 0) if days <= t]
    if not due:
        return
    import os
    from portal_utils import send_email
    conn, cur = _db()
    try:
        new = []
        for t in due:
            cur.execute("INSERT INTO system_notices_sent (notice_key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING notice_key",
                        (f"ms_secret_{exp.isoformat()}_{t}",))
            if cur.fetchone():
                new.append(t)
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not new:
        return
    when = "has expired" if days <= 0 else f"expires in {days} day{'s' if days != 1 else ''} ({exp.strftime('%d %b %Y')})"
    html = (f"<p>PhiXtra's Microsoft connection key (the client secret on the <b>PhiXtra</b> app in Microsoft Entra) {when}.</p>"
            "<p>While it's expired, Outlook reply tracking pauses for every business. Nothing is lost.</p>"
            "<p><b>To renew (2 minutes):</b> entra.microsoft.com › Entra ID › App registrations › PhiXtra › "
            "Certificates &amp; secrets › New client secret (24 months). Put the new Value and expiry date in the "
            "portal settings (MS_CLIENT_SECRET, MS_CLIENT_SECRET_EXPIRES), restart the portal, then delete the old secret. "
            "Staff don't need to reconnect.</p>")
    for addr in [a.strip() for a in os.getenv("ADMIN_EMAILS", "").split(",") if a.strip()]:
        send_email(addr, f"Action needed: Microsoft connection key {when}", html)


def _loop():
    time.sleep(30)   # let the portal finish its start-up (tables) first
    last_reminder = 0.0
    while True:
        try:
            for mb_id in _due_mailboxes():
                check_mailbox(mb_id)
            if time.time() - last_reminder > 3600:
                last_reminder = time.time()
                _secret_reminder()
        except Exception as e:
            print("⚠️ mailbox checker error:", e)
        time.sleep(60)


if not getattr(threading, "_phixtra_mailbox_checker_started", False):
    threading._phixtra_mailbox_checker_started = True  # type: ignore[attr-defined]
    threading.Thread(target=_loop, daemon=True).start()
