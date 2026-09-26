"""
buffer_routes.py — Buffer for businesses (2026-09-25). Bring-your-own-account,
same model as PressOne: a business that already uses Buffer pastes its own
Buffer API key on Integration › Buffer, picks which of its Buffer social
accounts PhiXtra may post to, then writes posts on the Social Posts page.
PhiXtra never posts through anyone else's Buffer. PhiXtra's own Buffer is
set up separately in admin (portal_admin_routes.buffer_channels_settings).

  GET  /integrations/buffer               connect screen / manage screen
  POST /integrations/buffer/key           check a key with Buffer and save it (connect or replace)
  POST /integrations/buffer/organization  switch Buffer organisation (key sees more than one)
  POST /integrations/buffer/channels      save the "Use in PhiXtra" switches
  POST /integrations/buffer/refresh       re-check the key and re-read the social accounts
  POST /integrations/buffer/disconnect

  GET  /social-posts                      Content: posts not yet out (?tab=, ?person=; ?edit=<id> opens the edit form)
  GET  /social-posts/published            Published: posts Buffer sent out (?tab=partial, ?person=, ?page=)
  POST /social-posts/save                 new post: post now / schedule / save as draft
  POST /social-posts/<id>/save            edit a draft, scheduled or failed post in place
  POST /social-posts/<id>/cancel          take a scheduled post out of Buffer, back to draft
  POST /social-posts/<id>/delete
  POST /social-posts/refresh              ask Buffer whether scheduled posts went out
  GET  /social-posts/<id>/image           thumbnail for logged-in users
  GET  /social-posts/public/<token>       the image URL Buffer fetches (no login, unguessable)
"""
import os
import uuid
import json as _json
from datetime import datetime, timezone, timedelta

import psycopg2.extras
from flask import (Blueprint, request, render_template, redirect, url_for, flash,
                   send_from_directory, session, abort)

from db import get_db_connection, insert_audit_log
from feature_access import team_feature, public_route
from portal_routes import (_require_login, _customer_id, _get_customer, _require_plan_sub_feature,
                           _require_team_permission, _team_member_has_permission, _current_actor,
                           _inject_granted_features, _inject_connect_flag)
import buffer_accounts as ba
import social_workflow as W
from buffer_client import BufferAPIError, buffer_create_post, buffer_get_post, buffer_delete_post

buffer_bp = Blueprint("buffer", __name__)
# The side menu's padlocks read these; they're registered on portal_bp only,
# so pages on this blueprint need them too or every menu item shows locked.
buffer_bp.context_processor(_inject_granted_features)
buffer_bp.context_processor(_inject_connect_flag)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads", "tenant_social_posts")
ALLOWED_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_BYTES = 15 * 1024 * 1024

EDITABLE = ("draft", "scheduled", "failed")


def _public_base() -> str:
    return os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")


def _gate(plan_key: str, label: str):
    """After the caller's own login + team-role check: the plan. Returns
    (response, customer)."""
    customer = _get_customer(_customer_id())
    r = _require_plan_sub_feature(customer, plan_key, label)
    if r:
        return r, None
    return None, customer


def _owner(customer) -> str:
    return ba.tenant_owner(customer["tenant_id"])


def _error_text(e: BufferAPIError) -> str:
    if e.is_auth_error:
        return ("Buffer didn't accept this key. Check you copied the whole key from Buffer › Settings › API, "
                "with no spaces, then try again. If you deleted that key in Buffer, create a new one.")
    if e.code == "RATE_LIMIT_EXCEEDED":
        return "Buffer is limiting how often PhiXtra can ask right now. Wait 15 minutes, then try again."
    return f"Buffer said: {e}"


# ══════════════════════════════════════════════════════════════════════════
# Integration › Buffer
# ══════════════════════════════════════════════════════════════════════════

@buffer_bp.route("/integrations/buffer", methods=["GET"])
@team_feature("channels.connect_buffer_view")
def connect():
    r = _require_login() or _require_team_permission("channels.connect_buffer_view")
    if r:
        return r
    r, customer = _gate("channels.connect_buffer_view", "Buffer")
    if r:
        return r
    owner = _owner(customer)
    ba.refresh_channels_if_stale(owner)
    account = ba.get_account(owner)
    channels = ba.list_channels(owner) if account else []
    orgs = session.pop("buffer_orgs", None) if account else None
    return render_template(
        "portal/buffer_connect.html",
        customer=customer,
        account=account,
        channels=channels,
        orgs=orgs,
        just_connected=request.args.get("step") == "choose",
        key_error=session.pop("buffer_key_error", None),
        can_manage=_team_member_has_permission("channels.connect_buffer_manage"),
        can_remove=_team_member_has_permission("channels.connect_buffer_remove"),
        can_post=_team_member_has_permission("social.posts_view"),
    )


@buffer_bp.route("/integrations/buffer/key", methods=["POST"])
@team_feature("channels.connect_buffer_manage")
def save_key():
    r = _require_login() or _require_team_permission("channels.connect_buffer_manage")
    if r:
        return r
    r, customer = _gate("channels.connect_buffer_view", "Buffer")
    if r:
        return r
    owner = _owner(customer)
    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        session["buffer_key_error"] = "Paste your Buffer API key first."
        return redirect(url_for("buffer.connect"))
    try:
        orgs = ba.check_key(api_key)
    except BufferAPIError as e:
        session["buffer_key_error"] = _error_text(e)
        return redirect(url_for("buffer.connect"))

    replacing = ba.get_account(owner) is not None
    try:
        ba.save_account(owner, int(customer["tenant_id"]), api_key, orgs[0], _current_actor(customer)["label"])
    except BufferAPIError as e:
        flash(f"Your key was saved, but PhiXtra couldn't load your social accounts yet. {_error_text(e)}", "warning")
        return redirect(url_for("buffer.connect"))

    insert_audit_log(action="buffer_key_replaced" if replacing else "buffer_connected",
                     tenant_id=int(customer["tenant_id"]),
                     details={"organization": orgs[0].get("name"), "by": _current_actor(customer)["label"]})
    if len(orgs) > 1:
        session["buffer_orgs"] = orgs
    flash(f"Buffer accepted your key. Found organisation “{orgs[0].get('name')}”.", "success")
    return redirect(url_for("buffer.connect", step="choose"))


@buffer_bp.route("/integrations/buffer/organization", methods=["POST"])
@team_feature("channels.connect_buffer_manage")
def switch_organization():
    r = _require_login() or _require_team_permission("channels.connect_buffer_manage")
    if r:
        return r
    r, customer = _gate("channels.connect_buffer_view", "Buffer")
    if r:
        return r
    try:
        ba.switch_organization(_owner(customer), request.form.get("organization_id") or "")
        flash("Switched Buffer organisation. Choose which accounts PhiXtra can post to.", "success")
    except BufferAPIError as e:
        flash(_error_text(e), "danger")
    return redirect(url_for("buffer.connect", step="choose"))


@buffer_bp.route("/integrations/buffer/channels", methods=["POST"])
@team_feature("channels.connect_buffer_manage")
def save_channels():
    r = _require_login() or _require_team_permission("channels.connect_buffer_manage")
    if r:
        return r
    r, customer = _gate("channels.connect_buffer_view", "Buffer")
    if r:
        return r
    owner = _owner(customer)
    known = {c["channel_id"] for c in ba.list_channels(owner)}
    picked = [c for c in request.form.getlist("channel_ids") if c in known]
    ba.set_enabled_channels(owner, picked)
    insert_audit_log(action="buffer_channels_saved", tenant_id=int(customer["tenant_id"]),
                     details={"enabled": len(picked)})
    flash(f"Saved. PhiXtra can post to {len(picked)} social account{'s' if len(picked) != 1 else ''}.", "success")
    return redirect(url_for("buffer.connect"))


@buffer_bp.route("/integrations/buffer/refresh", methods=["POST"])
@team_feature("channels.connect_buffer_manage")
def refresh():
    r = _require_login() or _require_team_permission("channels.connect_buffer_manage")
    if r:
        return r
    r, customer = _gate("channels.connect_buffer_view", "Buffer")
    if r:
        return r
    try:
        chans = ba.refresh_channels(_owner(customer))
        flash(f"Connection working. {len(chans)} social account{'s' if len(chans) != 1 else ''} found in Buffer.", "success")
    except BufferAPIError as e:
        flash(_error_text(e), "danger")
    return redirect(url_for("buffer.connect"))


@buffer_bp.route("/integrations/buffer/disconnect", methods=["POST"])
@team_feature("channels.connect_buffer_remove")
def disconnect():
    r = _require_login() or _require_team_permission("channels.connect_buffer_remove")
    if r:
        return r
    r, customer = _gate("channels.connect_buffer_view", "Buffer")
    if r:
        return r
    ba.disconnect(_owner(customer))
    insert_audit_log(action="buffer_disconnected", tenant_id=int(customer["tenant_id"]),
                     details={"by": _current_actor(customer)["label"]})
    flash("Buffer disconnected and your key deleted from PhiXtra. Posts already scheduled in Buffer stay there.", "success")
    return redirect(url_for("buffer.connect"))


# ══════════════════════════════════════════════════════════════════════════
# Social Posts
# ══════════════════════════════════════════════════════════════════════════

def _get_post(tenant_id: int, post_id: int):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM tenant_social_posts WHERE id=%s AND tenant_id=%s", (post_id, tenant_id))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


WIDE_SERVICES = ("facebook", "linkedin", "twitter")


def _media_assets(post, service):
    """Upload Design posts: the exact images / video chosen for this network
    (fixes already applied), as Buffer assets. None for other posts."""
    media = (post.get("media") or {})
    items = media.get(service) or media.get("_default")
    if not items:
        return None
    base = f"{_public_base()}/social-posts/media/{post['public_token']}"
    out = []
    for m in items:
        if m["kind"] == "video":
            out.append({"video": {"url": f"{base}/{m['file']}", "metadata": {"thumbnailOffset": 1000}}})
        else:
            out.append({"image": {"url": f"{base}/{m['file']}"}})
    return out


def _image_url(post, service: str = None) -> str:
    """Public address Buffer fetches the picture from. AI Post Designer
    posts have a wide version too, used for Facebook / LinkedIn / X."""
    if service in WIDE_SERVICES and post.get("wide_image_filename"):
        ext = post["wide_image_filename"].rsplit(".", 1)[-1]
        return f"{_public_base()}/social-posts/public/{post['public_token']}-wide.{ext}"
    if not post.get("image_filename"):
        return None
    ext = post["image_filename"].rsplit(".", 1)[-1]
    return f"{_public_base()}/social-posts/public/{post['public_token']}.{ext}"


def _parse_when(raw: str):
    """The browser converts the picked local time to UTC ISO before sending
    (hidden field), so the business's own timezone is respected."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _save_image(file):
    """Returns (stored_filename, original_filename, error)."""
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTS:
        return None, None, "Images must be JPG, PNG, WEBP or GIF."
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > MAX_BYTES:
        return None, None, "That image is over 15 MB. Use a smaller one."
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    stored = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(UPLOAD_FOLDER, stored))
    return stored, file.filename, None


def _remove_image(filename):
    if filename:
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, filename))
        except OSError:
            pass


def _delete_from_buffer(owner: str, post) -> list:
    """Takes a post's pending copies out of Buffer. Returns error texts."""
    errors = []
    for ch_id, buf_id in (post.get("buffer_post_ids") or {}).items():
        try:
            ba.call(owner, buffer_delete_post, buf_id)
        except BufferAPIError as e:
            if e.code != "NOT_FOUND":
                errors.append(str(e))
    return errors


def _send_to_buffer(owner: str, post, channels_by_id: dict, when):
    """Creates the post in Buffer on every picked account. Returns
    (status, buffer_post_ids, channel_results, error_text)."""
    # Always UTC: a time read back from the database carries the server's
    # own offset, and Buffer reads this string as UTC ("Z").
    when_iso = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z") if when else None
    captions = post.get("captions") or {}
    ids, results, errors = {}, {}, []
    for ch_id in post["channel_ids"]:
        ch = channels_by_id.get(ch_id)
        service = ch["service"] if ch else None
        text = captions.get(service) or post["caption"]
        try:
            assets = _media_assets(post, service)
            buf_id, _st = ba.call(owner, buffer_create_post, ch_id, text,
                                  None if assets else _image_url(post, service), when_iso,
                                  service=service, assets=assets)
            ids[ch_id] = buf_id
            results[ch_id] = {"status": "scheduled" if when else "publishing"}
        except BufferAPIError as e:
            name = f"{ch['label']} ({ch['title']})" if ch else ch_id
            errors.append(f"{name}: {e}")
            results[ch_id] = {"status": "failed", "error": str(e)}
    if not ids:
        status = "failed"
    elif errors:
        status = "partial"
    else:
        status = "scheduled" if when else "publishing"
    return status, ids, results, " | ".join(errors) or None


def _refresh_statuses(tenant_id: int, limit: int = 10) -> int:
    """Asks Buffer about posts that should have gone out by now. Capped per
    call to stay well inside Buffer's 100-requests-per-15-minutes limit."""
    owner = ba.tenant_owner(tenant_id)
    if not ba.is_connected(owner):
        return 0
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT * FROM tenant_social_posts
                   WHERE tenant_id=%s AND status IN ('publishing','scheduled','partial')
                     AND buffer_post_ids IS NOT NULL
                     AND (scheduled_for IS NULL OR scheduled_for <= NOW())
                   ORDER BY COALESCE(scheduled_for, created_at) LIMIT %s""", (tenant_id, limit))
    posts = cur.fetchall() or []
    cur.close(); conn.close()
    changed = 0
    for p in posts:
        results = dict(p.get("channel_results") or {})
        try:
            for ch_id, buf_id in (p["buffer_post_ids"] or {}).items():
                if (results.get(ch_id) or {}).get("status") in ("sent", "failed"):
                    continue
                info = ba.call(owner, buffer_get_post, buf_id)
                st = info.get("status")
                if st == "sent":
                    results[ch_id] = {"status": "sent", "link": info.get("externalLink")}
                elif st in ("failed", "error"):
                    results[ch_id] = {"status": "failed",
                                      "error": ((info.get("error") or {}).get("message")) or "Buffer couldn't publish it."}
        except BufferAPIError:
            break
        states = [(results.get(c) or {}).get("status") for c in p["channel_ids"]]
        if all(s == "sent" for s in states):
            new = "sent"
        elif all(s in ("sent", "failed") for s in states):
            new = "failed" if not any(s == "sent" for s in states) else "partial"
        else:
            new = p["status"]
        errs = " | ".join((results.get(c) or {}).get("error") for c in p["channel_ids"]
                          if (results.get(c) or {}).get("error")) or None
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""UPDATE tenant_social_posts SET status=%s, channel_results=%s, buffer_error=%s,
                              sent_at = CASE WHEN %s IN ('sent','partial') AND sent_at IS NULL THEN NOW() ELSE sent_at END,
                              updated_at=NOW()
                       WHERE id=%s""", (new, _json.dumps(results), errs, new, p["id"]))
        conn.commit()
        cur.close(); conn.close()
        changed += new != p["status"]
    return changed


def _insert_or_fill(customer, cols: dict, action: str, when, actor: str):
    """Saves a finished design as a post. If "Make the picture" was started
    from a planned draft (social_ai_planner.fill_target), THAT draft is
    updated instead of a second post being made; its planned day and person
    stay unless the form changed them. Returns (post, filled: bool, person)."""
    import social_ai_planner as PL
    tenant_id = int(customer["tenant_id"])
    target = PL.fill_target(tenant_id)
    if target and request.form.get("fill_post_id") != str(target["id"]):
        target = None                           # unticked: make a separate post
    keep = {"key": target.get("owner_key"), "label": W.owner_name(target), "email": None} if target else None
    person, due, err = W.assignment_from_form(customer, request.form, _current_actor(customer), keep=keep)
    if err:
        return None, False, err
    if target and not request.form.get("due_date") and target.get("due_date"):
        due = target["due_date"]
    if target and not when and action == "draft":
        when = target.get("scheduled_for")      # a plain "save draft" keeps the planned day
    cols = dict(cols, scheduled_for=when, owner_key=person["key"], owner_label=person["label"], due_date=due)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if target:
        sets = ", ".join(f"{k}=%s" for k in cols)
        cur.execute(f"UPDATE tenant_social_posts SET {sets}, updated_at=NOW() WHERE id=%s AND tenant_id=%s RETURNING *",
                    list(cols.values()) + [target["id"], tenant_id])
    else:
        cols = dict(cols, tenant_id=tenant_id, public_token=uuid.uuid4().hex, status="draft", created_by=actor)
        cur.execute(f"INSERT INTO tenant_social_posts ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
                    list(cols.values()))
    post = cur.fetchone()
    conn.commit()
    cur.close(); conn.close()
    session.pop(PL.FILL_KEY, None)
    session.pop("social_plan_date", None)
    if target:
        W.log_event(tenant_id, post["id"], "picture_added", actor)
        if person["key"] != (target.get("owner_key") or ""):
            W.log_event(tenant_id, post["id"], "assigned", actor, f"Now {person['label']}")
            W.notify_assignee(customer, post, person, actor)
    else:
        W.notify_assignee(customer, post, person, actor)
    return post, bool(target), person


def create_post_from_design(customer, caption: str, captions: dict, square: str, wide: str,
                            channel_ids: list, action: str, when, actor: str):
    """Used by the AI Post Designer's last step: saves the finished post (its
    pictures are already in UPLOAD_FOLDER) and, unless it's a draft, sends it
    to Buffer exactly like a hand-made post. Returns (ok, message)."""
    tenant_id = int(customer["tenant_id"])
    owner = _owner(customer)
    usable = {c["channel_id"]: c for c in ba.list_channels(owner, enabled_only=True)}
    picked = [c for c in channel_ids if c in usable]
    err = _validate(caption, picked, action, when, usable, True)
    if err:
        return False, err
    post, filled, err = _insert_or_fill(customer, {
        "caption": caption, "captions": _json.dumps(captions) if captions else None, "image_filename": square,
        "wide_image_filename": wide, "original_filename": "AI design", "channel_ids": picked, "media": None},
        action, when, actor)
    if not post:
        return False, err
    when = post["scheduled_for"] if action == "schedule" else None
    if action == "draft":
        insert_audit_log(action="social_post_draft_saved", tenant_id=tenant_id, details={"post_id": post["id"], "by": actor, "ai": True})
        return True, ("Picture added to the planned draft. Find it under Social Posts › Content." if filled
                      else "Draft saved. Find it under Social Posts › Content.")
    if W.approval_needed(tenant_id):
        return True, W.hold_for_approval(customer, post, action, when, _current_actor(customer))
    status, ids, results, errors = _send_to_buffer(owner, post, usable, when)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""UPDATE tenant_social_posts SET status=%s, buffer_post_ids=%s, channel_results=%s,
                          buffer_error=%s, updated_at=NOW() WHERE id=%s""",
                (status, _json.dumps(ids) if ids else None, _json.dumps(results), errors, post["id"]))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="social_post_sent_to_buffer", tenant_id=tenant_id,
                     details={"post_id": post["id"], "status": status, "by": actor, "ai": True})
    if status == "failed":
        return False, f"Buffer didn't accept the post. It's saved under Content so you can try again. {errors}"
    if status == "partial":
        return True, f"Sent to some accounts, but not all. {errors}"
    return True, "Post scheduled. Buffer will publish it at the time you picked." if when else "Sent to Buffer. It's publishing now."


def create_post_from_upload(customer, captions: dict, media: dict, cover: str, channel_ids: list,
                            action: str, when, actor: str):
    """Upload Design's last step: saves the post with the exact media for each
    network and, unless it's a draft, sends it to Buffer. Returns (ok, msg, post_id)."""
    tenant_id = int(customer["tenant_id"])
    owner = _owner(customer)
    usable = {c["channel_id"]: c for c in ba.list_channels(owner, enabled_only=True)}
    picked = [c for c in channel_ids if c in usable]
    if not picked:
        return False, "Pick at least one social account.", None
    if action == "schedule":
        if not when:
            return False, "Pick the date and time to post.", None
        if when < datetime.now(timezone.utc) + timedelta(minutes=2):
            return False, "Pick a time at least 2 minutes from now, or choose Post now.", None
    main = next((t for t in captions.values() if t), "")
    post, filled, err = _insert_or_fill(customer, {
        "caption": main or " ", "captions": _json.dumps(captions), "image_filename": cover,
        "wide_image_filename": None, "original_filename": "upload", "channel_ids": picked,
        "media": _json.dumps(media)}, action, when, actor)
    if not post:
        return False, err, None
    when = post["scheduled_for"] if action == "schedule" else None
    if action == "draft":
        insert_audit_log(action="social_post_draft_saved", tenant_id=tenant_id, details={"post_id": post["id"], "by": actor, "upload": True})
        return True, ("Picture added to the planned draft. Find it under Social Posts › Content." if filled
                      else "Draft saved. Find it under Social Posts › Content."), post["id"]
    if W.approval_needed(tenant_id):
        return True, W.hold_for_approval(customer, post, action, when, _current_actor(customer)), post["id"]
    status, ids, results, errors = _send_to_buffer(owner, post, usable, when)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""UPDATE tenant_social_posts SET status=%s, buffer_post_ids=%s, channel_results=%s,
                          buffer_error=%s, updated_at=NOW() WHERE id=%s""",
                (status, _json.dumps(ids) if ids else None, _json.dumps(results), errors, post["id"]))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="social_post_sent_to_buffer", tenant_id=tenant_id,
                     details={"post_id": post["id"], "status": status, "by": actor, "upload": True})
    if status == "failed":
        return False, f"Buffer didn't accept the post. It's saved under Content so you can try again. {errors}", post["id"]
    if status == "partial":
        return True, f"Sent to some accounts, but not all. {errors}", post["id"]
    return True, ("Post scheduled. Buffer will publish it at the time you picked." if when else "Sent to Buffer. It's publishing now."), post["id"]


OUT = ("sent", "partial")   # went out (all or some accounts) → Published; everything else → Content

CONTENT_TABS = [("all", "All not yet out"), ("overdue", "Overdue"), ("draft", "Drafts"),
                ("waiting", "Awaiting approval"), ("changes", "Changes requested"),
                ("sched", "Scheduled"), ("failed", "Failed")]


def _in_tab(p, tab, today) -> bool:
    if tab == "all":
        return True
    if tab == "overdue":
        return W.is_overdue(p, today)
    k = W.display_key(p)
    return k == tab or (tab == "sched" and k == "publishing")


def _list_context(customer):
    """What both Content and Published need: Buffer account, channels, fresh statuses."""
    tenant_id = int(customer["tenant_id"])
    owner = _owner(customer)
    ba.refresh_channels_if_stale(owner)
    account = ba.get_account(owner)
    if account:
        try:
            _refresh_statuses(tenant_id)
        except Exception as e:
            print("⚠️ social posts status refresh:", e)
    channels = ba.list_channels(owner) if account else []
    return tenant_id, account, channels, {c["channel_id"]: c for c in channels}


def _people_in(rows) -> list:
    """(key, name) of everyone responsible for at least one of these posts, for the person filter."""
    seen = {}
    for r in rows:
        name = W.owner_name(r)
        if name:
            seen.setdefault(r.get("owner_key") or "name:" + name, name)
    return sorted(seen.items(), key=lambda o: o[1].lower())


def _person_match(p, person: str) -> bool:
    return not person or (p.get("owner_key") or "name:" + W.owner_name(p)) == person


@buffer_bp.route("/social-posts", methods=["GET"])
@team_feature("social.posts_view")
def posts():
    """Content: every post that hasn't gone out yet, who's responsible and when it's due."""
    r = _require_login() or _require_team_permission("social.posts_view")
    if r:
        return r
    r, customer = _gate("social.posts_view", "Social Posts")
    if r:
        return r
    tenant_id, account, channels, channels_by_id = _list_context(customer)

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT * FROM tenant_social_posts WHERE tenant_id=%s AND status NOT IN ('sent','partial')
                   ORDER BY scheduled_for IS NULL, scheduled_for, created_at DESC""", (tenant_id,))
    rows = cur.fetchall() or []
    cur.close(); conn.close()

    today = W.today_for(customer)
    person = request.args.get("person", "")
    mine = [p for p in rows if _person_match(p, person)]
    tab = request.args.get("tab", "all")
    if tab not in dict(CONTENT_TABS):
        tab = "all"
    counts = {k: sum(1 for p in mine if _in_tab(p, k, today)) for k, _ in CONTENT_TABS}
    shown = [p for p in mine if _in_tab(p, tab, today)]

    editing = None
    edit_id = request.args.get("edit", "")
    if edit_id.isdigit():
        editing = next((p for p in rows if p["id"] == int(edit_id) and p["status"] in EDITABLE), None)

    return render_template(
        "portal/social_posts.html",
        customer=customer,
        today=today,
        account=account,
        channels=[c for c in channels if c["enabled"] and not c["is_disconnected"]],
        channels_by_id=channels_by_id,
        posts=shown,
        tab=tab, tabs=CONTENT_TABS, counts=counts,
        people=_people_in(rows), person=person,
        approval_on=W.approval_required(tenant_id),
        display=W.DISPLAY, display_key=W.display_key, is_overdue=W.is_overdue, title_of=W.title_of,
        editing=editing,
        editable=EDITABLE,
        can_create=_team_member_has_permission("social.posts_create"),
        can_edit=_team_member_has_permission("social.posts_edit"),
        can_delete=_team_member_has_permission("social.posts_delete"),
    )


PUBLISHED_PER_PAGE = 50


@buffer_bp.route("/social-posts/published", methods=["GET"])
@team_feature("social.posts_view")
def published():
    """Published: posts Buffer has sent out, newest first."""
    r = _require_login() or _require_team_permission("social.posts_view")
    if r:
        return r
    r, customer = _gate("social.posts_view", "Social Posts")
    if r:
        return r
    tenant_id, account, channels, channels_by_id = _list_context(customer)

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT * FROM tenant_social_posts WHERE tenant_id=%s AND status IN ('sent','partial')
                   ORDER BY COALESCE(sent_at, scheduled_for, created_at) DESC, id DESC""", (tenant_id,))
    rows = cur.fetchall() or []
    cur.close(); conn.close()

    person = request.args.get("person", "")
    mine = [p for p in rows if _person_match(p, person)]
    tab = "partial" if request.args.get("tab") == "partial" else "all"
    counts = {"all": len(mine), "partial": sum(1 for p in mine if p["status"] == "partial")}
    shown = [p for p in mine if tab == "all" or p["status"] == "partial"]
    pages = max(1, -(-len(shown) // PUBLISHED_PER_PAGE))
    try:
        page = min(max(1, int(request.args.get("page", 1))), pages)
    except ValueError:
        page = 1

    return render_template(
        "portal/social_published.html",
        customer=customer,
        account=account,
        channels_by_id=channels_by_id,
        posts=shown[(page - 1) * PUBLISHED_PER_PAGE: page * PUBLISHED_PER_PAGE],
        page=page, pages=pages, total=len(shown),
        tab=tab, counts=counts,
        people=_people_in(rows), person=person,
        title_of=W.title_of, owner_name=W.owner_name,
        can_delete=_team_member_has_permission("social.posts_delete"),
    )


def _form_values(owner):
    caption = (request.form.get("caption") or "").strip()
    usable = {c["channel_id"]: c for c in ba.list_channels(owner, enabled_only=True)}
    picked = [c for c in request.form.getlist("channel_ids") if c in usable]
    action = request.form.get("action") or "draft"  # now | schedule | draft
    when = _parse_when(request.form.get("scheduled_for_utc")) if wants_time(action) else None
    return caption, picked, action, when, usable


def wants_time(action: str) -> bool:
    """Schedule uses the picked time; a draft keeps it too as its planned
    date (shown on the calendar), if "Schedule for later" was chosen."""
    return action == "schedule" or (action == "draft" and request.form.get("when") == "schedule")


def _validate(caption, picked, action, when, usable, has_image):
    if not caption:
        return "Write a caption for the post."
    if action in ("now", "schedule") and not picked:
        return "Pick at least one social account to post to."
    if action == "schedule":
        if not when:
            return "Pick the date and time to post."
        if when < datetime.now(timezone.utc) + timedelta(minutes=2):
            return "Pick a time at least 2 minutes from now, or choose Post now."
    if action in ("now", "schedule") and not has_image:
        if any(usable[c]["service"] in ("instagram", "pinterest", "tiktok") for c in picked):
            return "Instagram, Pinterest and TikTok posts need an image. Add one, or untick those accounts."
    return None


@buffer_bp.route("/social-posts/save", methods=["POST"])
@buffer_bp.route("/social-posts/<int:post_id>/save", methods=["POST"])
@team_feature("social.posts_create", "social.posts_edit")
def save_post(post_id: int = None):
    team_key = "social.posts_edit" if post_id else "social.posts_create"
    r = _require_login() or _require_team_permission(team_key)
    if r:
        return r
    r, customer = _gate("social.posts_view", "Social Posts")
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    owner = _owner(customer)
    if not ba.is_connected(owner):
        flash("Connect Buffer first on Integration › Buffer.", "warning")
        return redirect(url_for("buffer.connect"))

    existing = None
    if post_id:
        existing = _get_post(tenant_id, post_id)
        if not existing or existing["status"] not in EDITABLE:
            flash("That post can't be edited any more. It has already gone out.", "warning")
            return redirect(url_for("buffer.posts"))

    caption, picked, action, when, usable = _form_values(owner)
    file = request.files.get("image_file")
    new_upload = bool(file and file.filename)
    remove_image = request.form.get("remove_image") == "1"
    if existing and existing.get("media"):
        # Upload Design posts keep their checked images / video; only the
        # words, accounts and time can change here.
        new_upload, remove_image, file = False, False, None
    has_image = new_upload or (existing and existing.get("image_filename") and not remove_image)
    back = url_for("buffer.posts", edit=post_id) if post_id else url_for("buffer.posts", new=1)

    err = _validate(caption, picked, action, when, usable, has_image)
    if err:
        flash(err, "warning")
        return redirect(back)

    image_filename = existing.get("image_filename") if existing else None
    original_filename = existing.get("original_filename") if existing else None
    old_image = None
    if new_upload:
        stored, orig, img_err = _save_image(file)
        if img_err:
            flash(img_err, "warning")
            return redirect(back)
        old_image, image_filename, original_filename = image_filename, stored, orig
    elif remove_image:
        old_image, image_filename, original_filename = image_filename, None, None

    # Editing a post that's already in Buffer: take the old copies out first,
    # then send the edited version fresh.
    if existing and existing.get("buffer_post_ids"):
        errs = _delete_from_buffer(owner, existing)
        if errs:
            if new_upload:
                _remove_image(image_filename)
            flash("Couldn't update the post in Buffer, so nothing was changed. " + " | ".join(errs), "danger")
            return redirect(back)

    actor = _current_actor(customer)["label"]
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if existing:
        caption_changed = caption != existing["caption"]
        cur.execute("""UPDATE tenant_social_posts SET caption=%s, image_filename=%s, original_filename=%s,
                              channel_ids=%s, status='draft', scheduled_for=%s, buffer_post_ids=NULL,
                              channel_results=NULL, buffer_error=NULL, updated_at=NOW(),
                              captions = CASE WHEN %s THEN NULL ELSE captions END,
                              wide_image_filename = CASE WHEN %s THEN NULL ELSE wide_image_filename END
                       WHERE id=%s RETURNING *""",
                    (caption, image_filename, original_filename, picked, when,
                     caption_changed, bool(new_upload or remove_image), post_id))
    else:
        cur.execute("""INSERT INTO tenant_social_posts (tenant_id, caption, image_filename, original_filename,
                              public_token, channel_ids, status, scheduled_for, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,'draft',%s,%s) RETURNING *""",
                    (tenant_id, caption, image_filename, original_filename, uuid.uuid4().hex, picked, when, actor))
    post = cur.fetchone()
    conn.commit()
    cur.close(); conn.close()
    if old_image:
        _remove_image(old_image)
        if existing and existing.get("wide_image_filename"):
            _remove_image(existing["wide_image_filename"])

    if action == "draft":
        W.withdraw_if_waiting(tenant_id, post, actor)
        insert_audit_log(action="social_post_draft_saved", tenant_id=tenant_id, details={"post_id": post["id"], "by": actor})
        flash("Draft saved.", "success")
        return redirect(url_for("buffer.posts"))

    if W.approval_needed(tenant_id):
        flash(W.hold_for_approval(customer, post, action, when, _current_actor(customer)), "success")
        return redirect(url_for("buffer.posts"))

    status, ids, results, errors = _send_to_buffer(owner, post, usable, when)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""UPDATE tenant_social_posts SET status=%s, buffer_post_ids=%s, channel_results=%s,
                          buffer_error=%s, updated_at=NOW() WHERE id=%s""",
                (status, _json.dumps(ids) if ids else None, _json.dumps(results), errors, post["id"]))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="social_post_sent_to_buffer", tenant_id=tenant_id,
                     details={"post_id": post["id"], "status": status, "by": actor})

    if status == "failed":
        flash(f"Buffer didn't accept the post. {errors}", "danger")
    elif status == "partial":
        flash(f"Sent to some accounts, but not all. {errors}", "warning")
    elif when:
        flash("Post scheduled. Buffer will publish it at the time you picked.", "success")
    else:
        flash("Sent to Buffer. It's publishing now.", "success")
    return redirect(url_for("buffer.posts"))


@buffer_bp.route("/social-posts/<int:post_id>/cancel", methods=["POST"])
@team_feature("social.posts_edit")
def cancel_post(post_id: int):
    r = _require_login() or _require_team_permission("social.posts_edit")
    if r:
        return r
    r, customer = _gate("social.posts_view", "Social Posts")
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    post = _get_post(tenant_id, post_id)
    if not post or post["status"] != "scheduled":
        flash("Only a scheduled post can be cancelled.", "warning")
        return redirect(url_for("buffer.posts"))
    errs = _delete_from_buffer(_owner(customer), post)
    if errs:
        flash("Couldn't cancel it in Buffer. " + " | ".join(errs), "danger")
        return redirect(url_for("buffer.posts"))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""UPDATE tenant_social_posts SET status='draft', buffer_post_ids=NULL, channel_results=NULL,
                          scheduled_for=NULL, updated_at=NOW() WHERE id=%s""", (post_id,))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(action="social_post_cancelled", tenant_id=tenant_id,
                     details={"post_id": post_id, "by": _current_actor(customer)["label"]})
    flash("Cancelled. The post is back in your drafts.", "success")
    return redirect(url_for("buffer.posts"))


@buffer_bp.route("/social-posts/<int:post_id>/delete", methods=["POST"])
@team_feature("social.posts_delete")
def delete_post(post_id: int):
    r = _require_login() or _require_team_permission("social.posts_delete")
    if r:
        return r
    r, customer = _gate("social.posts_view", "Social Posts")
    if r:
        return r
    tenant_id = int(customer["tenant_id"])
    post = _get_post(tenant_id, post_id)
    if not post:
        return redirect(url_for("buffer.posts"))
    if post["status"] == "scheduled":
        errs = _delete_from_buffer(_owner(customer), post)
        if errs:
            flash("Couldn't remove it from Buffer, so it wasn't deleted. " + " | ".join(errs), "danger")
            return redirect(url_for("buffer.posts"))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM tenant_social_posts WHERE id=%s AND tenant_id=%s", (post_id, tenant_id))
    conn.commit()
    cur.close(); conn.close()
    _remove_image(post.get("image_filename"))
    _remove_image(post.get("wide_image_filename"))
    for lst in (post.get("media") or {}).values():
        for m in lst:
            _remove_image(m.get("file"))
            _remove_image(m.get("thumb"))
    insert_audit_log(action="social_post_deleted", tenant_id=tenant_id,
                     details={"post_id": post_id, "status": post["status"], "by": _current_actor(customer)["label"]})
    flash("Post deleted." + (" It stays on the social networks where it was already published."
                             if post["status"] in OUT else ""), "success")
    return redirect(url_for("buffer.published") if post["status"] in OUT else url_for("buffer.posts"))


@buffer_bp.route("/social-posts/refresh", methods=["POST"])
@team_feature("social.posts_view")
def refresh_posts():
    r = _require_login() or _require_team_permission("social.posts_view")
    if r:
        return r
    r, customer = _gate("social.posts_view", "Social Posts")
    if r:
        return r
    n = _refresh_statuses(int(customer["tenant_id"]), limit=20)
    flash(f"Checked with Buffer. {n} post{'s' if n != 1 else ''} updated." if n else "Checked with Buffer. Nothing new yet.", "success")
    return redirect(url_for("buffer.published") if request.form.get("back") == "published" else url_for("buffer.posts"))


@buffer_bp.route("/social-posts/<int:post_id>/image", methods=["GET"])
@team_feature("social.posts_view")
def post_image(post_id: int):
    r = _require_login() or _require_team_permission("social.posts_view")
    if r:
        return r
    r, customer = _gate("social.posts_view", "Social Posts")
    if r:
        return r
    post = _get_post(int(customer["tenant_id"]), post_id)
    if not post or not post.get("image_filename"):
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, post["image_filename"])


@buffer_bp.route("/social-posts/public/<token>.<ext>", methods=["GET"])
@public_route
def public_image(token: str, ext: str):
    """No login by design: Buffer's servers fetch the image from here when
    the post publishes. The token is a random 32-character value per post."""
    wide = token.endswith("-wide")
    token = token[:-5] if wide else token
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT image_filename, wide_image_filename FROM tenant_social_posts WHERE public_token=%s", (token,))
    row = cur.fetchone()
    cur.close(); conn.close()
    name = (row[1] if wide else row[0]) if row else None
    if not name:
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, name)


@buffer_bp.route("/social-posts/media/<token>/<name>", methods=["GET"])
@public_route
def public_media(token: str, name: str):
    """No login by design: Buffer's servers fetch Upload Design images and
    videos from here. Only files that belong to that post are served."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT media FROM tenant_social_posts WHERE public_token=%s", (token,))
    row = cur.fetchone()
    cur.close(); conn.close()
    files = {m["file"] for lst in ((row or {}).get("media") or {}).values() for m in lst}
    if not row or name not in files:
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, name, conditional=True)


@buffer_bp.app_context_processor
def _inject_buffer_menu():
    """Social Posts appears in the side menu once Buffer is connected."""
    try:
        if request.blueprint in (None, "portal_admin") or not session.get("portal_logged_in"):
            return {}
        cid = _customer_id()
        if not cid:
            return {}
        customer = _get_customer(cid)
        return {"buffer_connected": bool(customer) and ba.is_connected(ba.tenant_owner(customer["tenant_id"]))}
    except Exception:
        return {}
