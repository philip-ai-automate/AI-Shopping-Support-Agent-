"""
social_workflow.py — Social Posts approval + calendar helpers (2026-09-26).

Approval is a per-business switch (Social Posts › Approval). When it's on,
anyone who can't approve — a team member without the "Approve or send back
Social Posts" tick — can't send a post to Buffer. Their "Post now" /
"Schedule" becomes "Submit for approval": the post stays a draft with
approval='waiting', keeping the time they picked. The account owner and
team members with the tick are approvers; their own posts go straight out.

Every way a post reaches Buffer (Content › Edit, AI Post Designer, Upload
Design) calls hold_for_approval() before sending, so there's one gate.

tenant_social_posts.approval:
  NULL       not in the approval flow (approval off, or sent by an approver)
  'waiting'  submitted, waiting for an approver
  'changes'  sent back with a note (approval_note); the writer edits and resubmits
  'approved' an approver approved it and it went to Buffer
The approval queue is always status='draft' AND approval='waiting'.

Each post also has a person responsible (owner_key / owner_label, default
whoever made it) and an optional due date: the day the content should be
ready. A post is overdue when its due date has passed and it's still a
draft that nobody has submitted (see is_overdue).
"""
import threading
from datetime import datetime, timezone

import psycopg2.extras
from flask import session

from db import get_db_connection, insert_audit_log

APPROVE_KEY = "social.posts_approve"


# ── Setting ────────────────────────────────────────────────────────────────

def approval_required(tenant_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT approval_required FROM social_settings WHERE tenant_id=%s", (tenant_id,))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    return bool(row and row[0])


def set_approval_required(tenant_id: int, on: bool, actor: str):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO social_settings (tenant_id, approval_required, updated_by, updated_at)
                       VALUES (%s,%s,%s,NOW())
                       ON CONFLICT (tenant_id) DO UPDATE SET approval_required=EXCLUDED.approval_required,
                           updated_by=EXCLUDED.updated_by, updated_at=NOW()""", (tenant_id, on, actor))
        conn.commit()
    finally:
        cur.close(); conn.close()
    insert_audit_log(action="social_approval_" + ("on" if on else "off"), tenant_id=tenant_id, details={"by": actor})


def can_approve() -> bool:
    """The logged-in person: the owner always; a team member only with the tick."""
    if not session.get("team_member_id"):
        return True
    return bool((session.get("team_member_permissions") or {}).get(APPROVE_KEY))


def approval_needed(tenant_id: int) -> bool:
    """True if the logged-in person's posts must be approved before sending."""
    return not can_approve() and approval_required(tenant_id)


# ── History ────────────────────────────────────────────────────────────────

def log_event(tenant_id: int, post_id: int, action: str, actor: str, note: str = None):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO social_post_events (tenant_id, post_id, action, actor, note)
                       VALUES (%s,%s,%s,%s,%s)""", (tenant_id, post_id, action, actor, note))
        conn.commit()
    finally:
        cur.close(); conn.close()


def events(tenant_id: int, post_id: int) -> list:
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT * FROM social_post_events WHERE tenant_id=%s AND post_id=%s
                       ORDER BY created_at, id""", (tenant_id, post_id))
        return cur.fetchall() or []
    finally:
        cur.close(); conn.close()


EVENT_TEXT = {
    "assigned": "Person responsible changed",
    "due_set": "Due date changed",
    "submitted": "Submitted for approval",
    "resubmitted": "Submitted again after changes",
    "withdrawn": "Taken back out of approval (saved as a draft)",
    "approved": "Approved",
    "changes": "Sent back for changes",
    "approval_on": "Approval switched on",
}


# ── Status shown to people ─────────────────────────────────────────────────

# key -> (label, css class). Classes match social_posts.html's pills.
DISPLAY = {
    "draft":   ("Draft", "p-n"),
    "waiting": ("Awaiting approval", "p-w"),
    "changes": ("Changes requested", "p-r"),
    "sched":   ("Scheduled", "p-b"),
    "publishing": ("Publishing", "p-b"),
    "sent":    ("Posted", "p-g"),
    "partial": ("Partly posted", "p-w"),
    "failed":  ("Failed", "p-r"),
}


def display_key(post) -> str:
    st = post["status"]
    if st == "draft":
        return {"waiting": "waiting", "changes": "changes"}.get(post.get("approval"), "draft")
    return {"scheduled": "sched"}.get(st, st if st in DISPLAY else "draft")


def post_date(post):
    """The day a post belongs on in the calendar, or None for an undated draft."""
    if post["status"] in ("sent", "partial") and post.get("sent_at"):
        return post["sent_at"]
    if post.get("scheduled_for"):
        return post["scheduled_for"]
    if post["status"] != "draft":
        return post.get("sent_at") or post["created_at"]
    return None


def title_of(post, n: int = 70) -> str:
    text = (post.get("caption") or "").strip().splitlines()
    first = next((ln.strip() for ln in text if ln.strip()), "") or "Untitled post"
    return first if len(first) <= n else first[: n - 1].rstrip() + "…"


def waiting_count(tenant_id: int) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT COUNT(*) FROM tenant_social_posts
                       WHERE tenant_id=%s AND status='draft' AND approval='waiting'""", (tenant_id,))
        return cur.fetchone()[0]
    finally:
        cur.close(); conn.close()


# ── The gate ───────────────────────────────────────────────────────────────

def hold_for_approval(customer, post, action: str, when, actor: dict) -> str:
    """Called instead of sending to Buffer when approval_needed(). Keeps the
    post as a draft, waiting, with the time the writer picked. Returns the
    message to show them."""
    tenant_id = int(customer["tenant_id"])
    again = post.get("approval") == "changes"
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE tenant_social_posts SET approval='waiting', requested_action=%s, scheduled_for=%s,
                              submitted_by=%s, submitted_by_key=%s, submitted_at=NOW(), updated_at=NOW()
                       WHERE id=%s AND tenant_id=%s""",
                    (action, when if action == "schedule" else None, actor["label"], actor["key"],
                     post["id"], tenant_id))
        conn.commit()
    finally:
        cur.close(); conn.close()
    log_event(tenant_id, post["id"], "resubmitted" if again else "submitted", actor["label"])
    insert_audit_log(action="social_post_submitted", tenant_id=tenant_id,
                     details={"post_id": post["id"], "by": actor["label"]})
    notify_approvers(customer, post, actor["label"])
    return ("Sent for approval. It goes out once it's approved"
            + (" at the time you picked." if action == "schedule" else ".")
            + " You'll get an email when it's approved or sent back.")


def withdraw_if_waiting(tenant_id: int, post, actor_label: str):
    """Saving a waiting post as a plain draft takes it out of the queue."""
    if post.get("approval") != "waiting":
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE tenant_social_posts SET approval=NULL, updated_at=NOW() WHERE id=%s AND tenant_id=%s",
                    (post["id"], tenant_id))
        conn.commit()
    finally:
        cur.close(); conn.close()
    log_event(tenant_id, post["id"], "withdrawn", actor_label)


# ── Emails ─────────────────────────────────────────────────────────────────

def _approver_emails(customer) -> list:
    """The owner plus every active team member whose role has the approve tick."""
    emails = []
    if customer.get("email"):
        emails.append(customer["email"].strip())
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT tm.email FROM team_members tm JOIN tenant_roles r ON r.id = tm.role_id
                       WHERE tm.tenant_id=%s AND tm.is_active AND tm.password_hash IS NOT NULL
                         AND COALESCE((r.permissions->>%s)::boolean, FALSE)""",
                    (int(customer["tenant_id"]), APPROVE_KEY))
        emails += [r[0].strip() for r in cur.fetchall() if r[0]]
    finally:
        cur.close(); conn.close()
    seen, out = set(), []
    for e in emails:
        if e.lower() not in seen:
            seen.add(e.lower()); out.append(e)
    return out


def _email_for_key(customer, key: str):
    """Email of whoever submitted: 'owner:<id>' or 'team:<id>'."""
    if not key:
        return None
    kind, _, ident = key.partition(":")
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if kind == "team":
            cur.execute("SELECT email FROM team_members WHERE id=%s AND tenant_id=%s AND is_active",
                        (int(ident), int(customer["tenant_id"])))
        else:
            cur.execute("SELECT email FROM customers WHERE id=%s AND tenant_id=%s",
                        (int(ident), int(customer["tenant_id"])))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    return row[0] if row and row[0] else None


def _send_later(to_list, subject, html, text):
    from portal_utils import send_email
    def run():
        for to in to_list:
            send_email(to, subject, html, text)
    threading.Thread(target=run, daemon=True).start()


def _base_url() -> str:
    import os
    return os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")


def _esc(s) -> str:
    import html
    return html.escape(str(s or ""))


def notify_approvers(customer, post, by_label: str):
    to = _approver_emails(customer)
    if not to:
        return
    title = title_of(post, 90)
    link = f"{_base_url()}/social-posts/approval"
    subject = f"Social post waiting for your approval: {title}"
    text = (f"{by_label} submitted a social post for approval.\n\n\"{title}\"\n\n"
            f"Approve it or send it back with comments here: {link}\n")
    html = (f"<p>{_esc(by_label)} submitted a social post for approval.</p>"
            f"<p style=\"font-size:15px\"><b>{_esc(title)}</b></p>"
            f"<p><a href=\"{link}\" style=\"background:#030C18;color:#fff;padding:10px 16px;border-radius:8px;"
            f"text-decoration:none;display:inline-block\">Review it</a></p>"
            f"<p style=\"color:#667085;font-size:12px\">Sent by PhiXtra because this business needs posts approved before they go out.</p>")
    _send_later(to, subject, html, text)


def notify_submitter(customer, post, decision: str, by_label: str, note: str = None, when_text: str = None):
    to = _email_for_key(customer, post.get("submitted_by_key"))
    if not to:
        return
    title = title_of(post, 90)
    link = f"{_base_url()}/social-posts/{post['id']}/view"
    if decision == "approved":
        subject = f"Approved: {title}"
        line = f"{by_label} approved your social post." + (f" {when_text}" if when_text else "")
    else:
        subject = f"Changes requested: {title}"
        line = f"{by_label} sent your social post back with comments."
    text = f"{line}\n\n\"{title}\"\n" + (f"\nComments: {note}\n" if note else "") + f"\nOpen it: {link}\n"
    html = (f"<p>{_esc(line)}</p><p style=\"font-size:15px\"><b>{_esc(title)}</b></p>"
            + (f"<p style=\"background:#FEF3F2;border:1px solid #FECDCA;border-radius:8px;padding:10px 12px\">"
               f"<b>Comments:</b><br>{_esc(note)}</p>" if note else "")
            + f"<p><a href=\"{link}\">Open the post</a></p>")
    _send_later([to], subject, html, text)


# ── Person responsible + due date ──────────────────────────────────────────

def people(customer) -> list:
    """Who a post can be given to: the account owner, then every active team
    member (invite accepted) whose role can see Social Posts.
    [{"key", "label", "email"}]"""
    out = [{"key": f"owner:{customer['id']}",
            "label": ((customer.get("first_name") or "").strip() or "Owner") + " (owner)",
            "email": customer.get("email")}]
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT tm.id, COALESCE(NULLIF(TRIM(tm.name),''), tm.email), tm.email
                       FROM team_members tm JOIN tenant_roles r ON r.id = tm.role_id
                       WHERE tm.tenant_id=%s AND tm.is_active AND tm.password_hash IS NOT NULL
                         AND COALESCE((r.permissions->>'social.posts_view')::boolean, FALSE)
                       ORDER BY 2""", (int(customer["tenant_id"]),))
        out += [{"key": f"team:{i}", "label": name, "email": email} for i, name, email in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    return out


def default_owner(customer, actor: dict) -> dict:
    """Whoever is making the post, in the same shape as people()."""
    for p in people(customer):
        if p["key"] == actor["key"]:
            return p
    return {"key": actor["key"], "label": actor["label"], "email": None}


def parse_due(raw: str):
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def assignment_from_form(customer, form, actor: dict, keep: dict = None):
    """(owner dict, due date or None, error) from a form's owner_key / due_date.
    An empty owner_key means "unchanged": `keep` (the post's current person)
    when editing, else whoever is making the post."""
    key = (form.get("owner_key") or "").strip()
    owner = keep if keep and keep.get("key") else default_owner(customer, actor)
    if key:
        match = next((p for p in people(customer) if p["key"] == key), None)
        if not match:
            return None, None, "That person can't be given this post. Pick someone from the list."
        owner = match
    raw = (form.get("due_date") or "").strip()
    due = parse_due(raw)
    if raw and not due:
        return None, None, "The due date isn't a real date."
    return owner, due, None


def tz_for(customer):
    """The business's own time zone (UTC if it hasn't set one)."""
    name = (customer or {}).get("timezone")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def today_for(customer):
    """Today in the business's own time zone."""
    return datetime.now(tz_for(customer)).date()


def is_overdue(post, today) -> bool:
    return bool(post.get("due_date") and post["due_date"] < today
                and post["status"] == "draft" and post.get("approval") != "waiting")


def owner_name(post) -> str:
    return post.get("owner_label") or post.get("created_by") or ""


def set_assignment(customer, post, owner: dict, due, actor_label: str) -> bool:
    """Saves a new person / due date on an existing post, with history and an
    email to a newly assigned person. Returns True if anything changed."""
    tenant_id = int(customer["tenant_id"])
    changed_owner = owner["key"] != (post.get("owner_key") or "")
    changed_due = due != post.get("due_date")
    if not (changed_owner or changed_due):
        return False
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE tenant_social_posts SET owner_key=%s, owner_label=%s, due_date=%s, updated_at=NOW()
                       WHERE id=%s AND tenant_id=%s""", (owner["key"], owner["label"], due, post["id"], tenant_id))
        conn.commit()
    finally:
        cur.close(); conn.close()
    if changed_owner:
        log_event(tenant_id, post["id"], "assigned", actor_label, f"Now {owner['label']}")
    if changed_due:
        log_event(tenant_id, post["id"], "due_set", actor_label,
                  f"Due {due.strftime('%a %d %b %Y')}" if due else "No due date")
    if changed_owner:
        notify_assignee(customer, dict(post, owner_key=owner["key"], due_date=due), owner, actor_label)
    return True


def notify_assignee(customer, post, owner: dict, by_label: str):
    """Emails someone who was given a post by somebody else."""
    to = owner.get("email")
    if not to or by_label == owner.get("label"):
        return
    title = title_of(post, 90)
    link = f"{_base_url()}/social-posts/{post['id']}/view"
    due = post.get("due_date")
    due_text = f" It's due {due.strftime('%A %d %B')}." if due else ""
    subject = f"Social post for you: {title}"
    text = f"{by_label} made you responsible for a social post.{due_text}\n\n\"{title}\"\n\nOpen it: {link}\n"
    html = (f"<p>{_esc(by_label)} made you responsible for a social post.{_esc(due_text)}</p>"
            f"<p style=\"font-size:15px\"><b>{_esc(title)}</b></p><p><a href=\"{link}\">Open the post</a></p>")
    _send_later([to], subject, html, text)
