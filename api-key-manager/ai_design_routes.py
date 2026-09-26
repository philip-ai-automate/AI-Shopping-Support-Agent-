"""
ai_design_routes.py — pages for the AI Post Designer (2026-09-25), one set
of screens used twice:

  social_bp      every business, under the Social Posts menu
                 /social-posts/new, /social-posts/design/<id>…, /social-posts/brand-kit,
                 /social-posts/usage, and Integration › Your own AI key (/integrations/ai-key)
  ai_admin_bp    PhiXtra admin, from Social Media Posts
                 /admin/social-media/design/…, /admin/social-media/brand-kit

The engine (AI calls, allowance, Brand Kit, drawing) is ai_designer.py.
Templates take `layout_base` (portal or admin frame) and an endpoint prefix
`ep` so the same HTML serves both sides.
"""
import os
from datetime import datetime, timezone

from flask import (Blueprint, request, render_template, redirect, url_for, flash, session,
                   send_from_directory, abort, Response)

from db import insert_audit_log
from feature_access import team_feature
from portal_routes import (_require_login, _customer_id, _get_customer, _require_plan_sub_feature,
                           _require_team_permission, _team_member_has_permission, _current_actor,
                           _inject_granted_features, _inject_connect_flag)
import ai_designer as D
import ai_design_render as R
import buffer_accounts as ba

social_bp = Blueprint("social", __name__)
social_bp.context_processor(_inject_granted_features)
social_bp.context_processor(_inject_connect_flag)
ai_admin_bp = Blueprint("ai_admin", __name__, url_prefix="/admin/social-media")

PLAN_KEY = "social.posts_view"


# ══════════════════════════════════════════════════════════════════════════
# shared context
# ══════════════════════════════════════════════════════════════════════════

class Ctx:
    """Who is designing and where they post."""
    def __init__(self, owner_key, tenant_id, actor, is_admin, channels, business_name):
        self.owner_key, self.tenant_id, self.actor, self.is_admin = owner_key, tenant_id, actor, is_admin
        self.channels, self.business_name = channels, business_name
        self.ep = "ai_admin." if is_admin else "social."
        self.base = "portal/admin_base.html" if is_admin else "portal/base.html"


def _business_ctx():
    customer = _get_customer(_customer_id())
    tid = int(customer["tenant_id"])
    owner = ba.tenant_owner(tid)
    if request.method == "GET":
        ba.refresh_channels_if_stale(owner)
    chans = [{"channel_id": c["channel_id"], "service": c["service"], "label": c["label"], "title": c["title"],
              "colour": c["colour"]} for c in ba.list_channels(owner, enabled_only=True)]
    return Ctx(D.tenant_owner(tid), tid, _current_actor(customer)["label"], False, chans,
               customer.get("tenant_name") or ""), customer


def _admin_ctx():
    from portal_admin_routes import SM_PLATFORMS, _admin_user
    chans = [{"channel_id": k, "service": v["service"], "label": v["label"], "title": v["label"],
              "colour": ba.service_colour(v["service"])} for k, v in SM_PLATFORMS.items()]
    return Ctx(D.ADMIN_OWNER, None, _admin_user(), True, chans, "PhiXtra")


def _common(ctx, **extra):
    d = dict(layout_base=ctx.base, ep=ctx.ep, is_admin=ctx.is_admin,
             allow=D.allowance(ctx.owner_key, ctx.tenant_id),
             month_reset=_next_month_label(), styles=R.STYLES, style_labels=R.STYLE_LABELS,
             layouts=R.LAYOUTS, layout_labels=R.LAYOUT_LABELS)
    d.update(extra)
    return d


def _next_month_label():
    now = datetime.now(timezone.utc)
    nxt = datetime(now.year + (now.month == 12), 1 if now.month == 12 else now.month + 1, 1)
    return nxt.strftime("%-d %b")


def _session_or_404(ctx, sid):
    s = D.get_session(ctx.owner_key, sid)
    if not s:
        abort(404)
    return s


# ══════════════════════════════════════════════════════════════════════════
# shared handlers (auth already done by the caller)
# ══════════════════════════════════════════════════════════════════════════

def h_new(ctx, can_ai=True):
    if request.method == "POST":
        src = request.form.get("source") or ("idea" if ctx.is_admin else "product")
        product = None
        if src == "product" and not ctx.is_admin:
            product = D.get_product(ctx.tenant_id, request.form.get("product_ref") or "")
        picked = set(request.form.getlist("channel_ids"))
        channels = [c for c in ctx.channels if c["channel_id"] in picked]
        use_ai = request.form.get("action") == "ai"
        if use_ai and not can_ai:
            flash("Your role can't create AI designs. Use a ready-made layout, or ask your account owner.", "warning")
            return redirect(url_for(ctx.ep + "new_post"))
        if not channels:
            flash("Pick at least one social account.", "warning")
            return redirect(url_for(ctx.ep + "new_post", q=request.form.get("q", "")))
        try:
            s = D.create_session(ctx.owner_key, ctx.tenant_id, ctx.actor, source=src, product=product,
                                 idea=request.form.get("idea") or "", offer=request.form.get("offer") or "",
                                 channels=channels, picture_mode=request.form.get("picture") or "product",
                                 upload=request.files.get("image_file"), use_ai=use_ai)
        except D.DesignError as e:
            flash(str(e), "warning")
            return redirect(url_for(ctx.ep + "new_post"))
        return redirect(url_for(ctx.ep + "pick", sid=s["id"]))

    q = request.args.get("q", "")
    products = [] if ctx.is_admin else D.product_choices(ctx.tenant_id, q)
    return render_template("portal/ai_design_new.html", **_common(
        ctx, products=products, q=q, channels=ctx.channels, can_ai=can_ai,
        kit=D.get_brand_kit(ctx.owner_key, ctx.business_name)))


def h_pick(ctx, sid):
    s = _session_or_404(ctx, sid)
    kit = D.get_brand_kit(ctx.owner_key, ctx.business_name)
    return render_template("portal/ai_design_pick.html", **_common(
        ctx, s=s, v=s["variants"][int(s.get("selected") or 0)] if s["variants"] else None,
        swatches=R.palette_swatches(kit), tones=D.REWRITE_TONES, free_rewrites=D.FREE_REWRITES,
        kit=kit, stamp=int(datetime.now().timestamp())))


def h_action(ctx, sid, what):
    s = _session_or_404(ctx, sid)
    f = request.form
    try:
        if what == "select":
            i = int(f.get("i") or 0)
            if 0 <= i < len(s["variants"]):
                s["selected"] = i
                D._save_session(s)
        elif what == "restyle":
            D.restyle(s, f.get("style"), f.get("palette"), f.get("layout"))
        elif what == "vote":
            i = int(f.get("i") or 0)
            if 0 <= i < len(s["variants"]):
                cur = s["variants"][i].get("thumb")
                val = int(f.get("v") or 0)
                if (cur == "up" and val == 1) or (cur == "down" and val == -1):
                    val = 0  # tapping again clears it
                D.vote(ctx.owner_key, s, i, val)
        elif what == "rewrite":
            D.rewrite(ctx.owner_key, ctx.tenant_id, s, ctx.actor, f.get("tone") or "")
            flash("Words rewritten.", "success")
        elif what == "words":
            v = s["variants"][int(s.get("selected") or 0)]
            v["headline"] = (f.get("headline") or "").strip()[:80]
            v["price_label"] = (f.get("price_label") or "").strip()[:40]
            caps = dict(v.get("captions") or {})
            for c in s.get("channels") or []:
                key = f"caption__{c['service']}"
                if key in f:
                    caps[c["service"]] = f.get(key, "").strip()[:2200]
            v["captions"] = caps
            v["show_logo"] = f.get("show_logo") == "1"
            v["show_contact"] = f.get("show_contact") == "1"
            D._save_session(s)
    except D.DesignError as e:
        flash(str(e), "warning")
    target = request.form.get("back") or "pick"
    return redirect(url_for(ctx.ep + ("finish" if target == "finish" else "pick"), sid=sid) + f"#{what}")


def h_again(ctx, sid):
    s = _session_or_404(ctx, sid)
    if request.method == "POST":
        reasons = [r for r in request.form.getlist("reasons") if r in D.FEEDBACK]
        try:
            D.make_set(ctx.owner_key, ctx.tenant_id, s, ctx.actor, feedback=reasons,
                       note=request.form.get("note") or "")
            flash("Here are 4 new designs.", "success")
            return redirect(url_for(ctx.ep + "pick", sid=sid))
        except D.DesignError as e:
            flash(str(e), "warning")
            return redirect(url_for(ctx.ep + "again", sid=sid))
    return render_template("portal/ai_design_again.html", **_common(
        ctx, s=s, reasons=D.FEEDBACK, taste=D.taste(ctx.owner_key)))


def h_img(ctx, sid, idx, size):
    s = _session_or_404(ctx, sid)
    if size not in R.SIZES or not (0 <= idx < len(s["variants"])):
        abort(404)
    png = D.render_variant(ctx.owner_key, s, idx, size)
    return Response(png, mimetype="image/png", headers={"Cache-Control": "private, max-age=60"})


def h_finish(ctx, sid, customer=None):
    s = _session_or_404(ctx, sid)
    if not s["variants"]:
        return redirect(url_for(ctx.ep + "pick", sid=sid))
    v = s["variants"][int(s.get("selected") or 0)]
    kit = D.get_brand_kit(ctx.owner_key, ctx.business_name)
    fallback = D._template_captions(s, kit, s.get("channels") or [])
    if request.method == "POST":
        action = request.form.get("action") or "draft"
        caps = {}
        for c in s.get("channels") or []:
            caps[c["service"]] = (request.form.get(f"caption__{c['service']}") or "").strip()[:2200] \
                or (v.get("captions") or {}).get(c["service"]) or fallback.get(c["service"], "")
        v["captions"] = caps
        D._save_session(s)
        main_caption = next((t for t in caps.values() if t), "")
        if ctx.is_admin:
            return _admin_finish(ctx, s, main_caption)
        from buffer_routes import UPLOAD_FOLDER, create_post_from_design, _parse_when, wants_time
        when = _parse_when(request.form.get("scheduled_for_utc")) if wants_time(action) else None
        square, wide = D.save_final_images(ctx.owner_key, s, UPLOAD_FOLDER)
        ok, msg = create_post_from_design(customer, main_caption, caps, square, wide,
                                          [c["channel_id"] for c in s.get("channels") or []], action, when, ctx.actor)
        if not ok and "saved under Posts" not in msg:
            for n in (square, wide):
                try:
                    os.remove(os.path.join(UPLOAD_FOLDER, n))
                except OSError:
                    pass
            flash(msg, "warning")
            return redirect(url_for(ctx.ep + "finish", sid=sid))
        flash(msg, "success" if ok else "danger")
        return redirect(url_for("buffer.posts"))
    return render_template("portal/ai_design_finish.html", **_common(
        ctx, s=s, v=v, kit=kit, swatches=R.palette_swatches(kit), fallback=fallback,
        stamp=int(datetime.now().timestamp())))


def _admin_finish(ctx, s, caption):
    import uuid
    from db import get_db_connection
    from portal_admin_routes import SM_UPLOAD_FOLDER
    square, _wide = D.save_final_images(ctx.owner_key, s, SM_UPLOAD_FOLDER)
    try:
        os.remove(os.path.join(SM_UPLOAD_FOLDER, _wide))
    except OSError:
        pass
    platforms = [c["channel_id"] for c in s.get("channels") or []]
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""INSERT INTO social_media_posts (caption, image_filename, original_filename, platforms, status,
                                                   created_by, is_urgent, public_token)
                   VALUES (%s,%s,'AI design.png',%s,'ready',%s,FALSE,%s) RETURNING id""",
                (caption, square, platforms, ctx.actor, uuid.uuid4().hex))
    post_id = cur.fetchone()[0]
    conn.commit()
    cur.close(); conn.close()
    s["post_id"] = post_id
    D._save_session(s)
    insert_audit_log(admin_username=ctx.actor, action="social_media_post_ai_design", details={"post_id": post_id})
    flash("Added to Social Media Posts as Ready to Post. Send or schedule it through Buffer from there.", "success")
    return redirect(url_for("portal_admin.social_media_posts"))


def h_brand_kit(ctx):
    if request.method == "POST":
        try:
            if request.form.get("action") == "from_logo":
                main, accent = D.colours_from_logo(ctx.owner_key)
                form = dict(request.form)
                form["color_main"], form["color_accent"] = main, accent
                D.save_brand_kit(ctx.owner_key, form, None, ctx.actor)
                flash("Colours picked from your logo. Adjust them if needed, then save.", "success")
            elif request.form.get("action") == "reset_taste":
                D.reset_taste(ctx.owner_key)
                flash("Cleared what the AI learned from your 👍 / 👎.", "success")
            else:
                D.save_brand_kit(ctx.owner_key, request.form, request.files.get("logo_file"), ctx.actor)
                flash("Brand Kit saved.", "success")
        except D.DesignError as e:
            flash(str(e), "warning")
        return redirect(url_for(ctx.ep + "brand_kit"))
    kit = D.get_brand_kit(ctx.owner_key, ctx.business_name)
    sample = {"style": kit["style"], "layout": "right", "palette": 0, "headline": "Your headline here",
              "price_label": "₦12,500", "show_logo": True, "show_contact": bool(kit.get("contact_line"))}
    return render_template("portal/ai_design_brand_kit.html", **_common(
        ctx, kit=kit, voices=D.VOICES, taste=D.taste(ctx.owner_key), sample=sample,
        stamp=int(datetime.now().timestamp())))


def h_brand_preview(ctx):
    kit = D.get_brand_kit(ctx.owner_key, ctx.business_name)
    style = request.args.get("style") if request.args.get("style") in R.STYLES else kit["style"]
    v = {"style": style, "layout": "right", "palette": 0, "headline": "Your headline here",
         "price_label": "₦12,500", "show_logo": True, "show_contact": bool(kit.get("contact_line"))}
    png = R.render(v, kit, None, D.logo_path(kit), "square")
    return Response(png, mimetype="image/png", headers={"Cache-Control": "no-store"})


def h_logo(ctx):
    kit = D.get_brand_kit(ctx.owner_key)
    if not kit.get("logo_filename"):
        abort(404)
    return send_from_directory(D.LOGO_FOLDER, kit["logo_filename"])


def h_session_image(ctx, sid, which):
    s = _session_or_404(ctx, sid)
    name = s.get("subject_image") if which == "subject" else s.get("ai_image")
    if not name:
        abort(404)
    return send_from_directory(D.IMG_FOLDER, name)


# ══════════════════════════════════════════════════════════════════════════
# Business pages
# ══════════════════════════════════════════════════════════════════════════

def _biz(team_key):
    """Login, team role, plan, and Buffer connected. Returns (response, ctx, customer)."""
    r = _require_login() or _require_team_permission(team_key)
    if r:
        return r, None, None
    customer = _get_customer(_customer_id())
    r = _require_plan_sub_feature(customer, PLAN_KEY, "Social Posts")
    if r:
        return r, None, None
    if not ba.is_connected(ba.tenant_owner(customer["tenant_id"])):
        flash("Connect Buffer first on Integration › Buffer. Social Posts publishes through it.", "warning")
        return redirect(url_for("buffer.connect")), None, None
    ctx, customer = _business_ctx()
    return None, ctx, customer


@social_bp.route("/social-posts/new", methods=["GET", "POST"])
@team_feature("social.posts_create")
def new_post():
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, ctx, _c = _biz("social.posts_create")
    if r:
        return r
    return h_new(ctx, can_ai=_team_member_has_permission("social.ai_designs"))


@social_bp.route("/social-posts/design/<int:sid>", methods=["GET"])
@team_feature("social.posts_create")
def pick(sid):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, ctx, _c = _biz("social.posts_create")
    if r:
        return r
    return h_pick(ctx, sid)


@social_bp.route("/social-posts/design/<int:sid>/<any(select, restyle, vote, rewrite, words):what>", methods=["POST"])
@team_feature("social.posts_create")
def design_action(sid, what):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, ctx, _c = _biz("social.posts_create")
    if r:
        return r
    return h_action(ctx, sid, what)


@social_bp.route("/social-posts/design/<int:sid>/again", methods=["GET", "POST"])
@team_feature("social.ai_designs")
def again(sid):
    r = _require_login() or _require_team_permission("social.ai_designs")
    if r:
        return r
    r, ctx, _c = _biz("social.ai_designs")
    if r:
        return r
    return h_again(ctx, sid)


@social_bp.route("/social-posts/design/<int:sid>/img/<int:idx>/<size>.png", methods=["GET"])
@team_feature("social.posts_create")
def design_img(sid, idx, size):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, ctx, _c = _biz("social.posts_create")
    if r:
        return r
    return h_img(ctx, sid, idx, size)


@social_bp.route("/social-posts/design/<int:sid>/finish", methods=["GET", "POST"])
@team_feature("social.posts_create")
def finish(sid):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, ctx, customer = _biz("social.posts_create")
    if r:
        return r
    return h_finish(ctx, sid, customer)


@social_bp.route("/social-posts/brand-kit", methods=["GET", "POST"])
@team_feature("social.brand_kit_view", "social.brand_kit_edit")
def brand_kit():
    key = "social.brand_kit_edit" if request.method == "POST" else "social.brand_kit_view"
    r = _require_login() or _require_team_permission(key)
    if r:
        return r
    r, ctx, _c = _biz(key)
    if r:
        return r
    return h_brand_kit(ctx)


@social_bp.route("/social-posts/brand-kit/preview.png", methods=["GET"])
@team_feature("social.brand_kit_view")
def brand_preview():
    r = _require_login() or _require_team_permission("social.brand_kit_view")
    if r:
        return r
    r, ctx, _c = _biz("social.brand_kit_view")
    if r:
        return r
    return h_brand_preview(ctx)


@social_bp.route("/social-posts/brand-kit/logo", methods=["GET"])
@team_feature("social.brand_kit_view")
def brand_logo():
    r = _require_login() or _require_team_permission("social.brand_kit_view")
    if r:
        return r
    r, ctx, _c = _biz("social.brand_kit_view")
    if r:
        return r
    return h_logo(ctx)


@social_bp.route("/social-posts/usage", methods=["GET"])
@team_feature("social.usage_view")
def usage():
    r = _require_login() or _require_team_permission("social.usage_view")
    if r:
        return r
    r, ctx, _c = _biz("social.usage_view")
    if r:
        return r
    return render_template("portal/ai_design_usage.html", **_common(ctx, log=D.usage_log(ctx.owner_key)))


# ── Integration › Your own AI key ──

@social_bp.route("/integrations/ai-key", methods=["GET", "POST"])
@team_feature("channels.connect_ai_key_view", "channels.connect_ai_key_manage")
def ai_key():
    key = "channels.connect_ai_key_manage" if request.method == "POST" else "channels.connect_ai_key_view"
    r = _require_login() or _require_team_permission(key)
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _require_plan_sub_feature(customer, "channels.connect_ai_key_view", "Your own AI key")
    if r:
        return r
    tid = int(customer["tenant_id"])
    owner = D.tenant_owner(tid)
    plan = D.plan_design_settings(tid)
    if request.method == "POST":
        if not plan["allow_own_key"]:
            flash("Your plan doesn't allow connecting your own AI key.", "warning")
            return redirect(url_for("social.ai_key"))
        api_key = (request.form.get("api_key") or "").strip()
        if not api_key:
            session["ai_key_error"] = "Paste your OpenAI API key first."
            return redirect(url_for("social.ai_key"))
        try:
            D.check_and_save_ai_key(owner, tid, api_key, _current_actor(customer)["label"])
        except D.DesignError as e:
            session["ai_key_error"] = str(e)
            return redirect(url_for("social.ai_key"))
        insert_audit_log(action="ai_key_connected", tenant_id=tid, details={"by": _current_actor(customer)["label"]})
        flash("Key working. AI designs are now unlimited and no longer use your plan's allowance.", "success")
        return redirect(url_for("social.ai_key"))
    return render_template("portal/ai_key_connect.html", customer=customer, key=D.get_ai_key_row(owner),
                           plan=plan, allow=D.allowance(owner, tid), key_error=session.pop("ai_key_error", None),
                           can_manage=_team_member_has_permission("channels.connect_ai_key_manage"),
                           can_remove=_team_member_has_permission("channels.connect_ai_key_remove"))


@social_bp.route("/integrations/ai-key/disconnect", methods=["POST"])
@team_feature("channels.connect_ai_key_remove")
def ai_key_disconnect():
    r = _require_login() or _require_team_permission("channels.connect_ai_key_remove")
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _require_plan_sub_feature(customer, "channels.connect_ai_key_view", "Your own AI key")
    if r:
        return r
    tid = int(customer["tenant_id"])
    D.remove_ai_key(D.tenant_owner(tid))
    insert_audit_log(action="ai_key_disconnected", tenant_id=tid, details={"by": _current_actor(customer)["label"]})
    flash("Your AI key was removed. AI designs now use your plan's monthly allowance.", "success")
    return redirect(url_for("social.ai_key"))


@social_bp.app_context_processor
def _inject_social_menu():
    """For the Social Posts side-menu group."""
    return {"social_menu_endpoints": ("buffer.posts", "social.new_post", "social.pick", "social.again",
                                      "social.finish", "social.brand_kit", "social.usage", "upload.start",
                                      "upload.check", "upload.fix", "upload.post", "upload.size_guide",
                                      "socialcal.calendar", "socialcal.approval", "socialcal.post_view")}


# ══════════════════════════════════════════════════════════════════════════
# PhiXtra admin
# ══════════════════════════════════════════════════════════════════════════

def _adm(action):
    from portal_admin_routes import _require_admin
    r = _require_admin("social_media", action)
    return r, (None if r else _admin_ctx())


@ai_admin_bp.route("/design/new", methods=["GET", "POST"])
def new_post():
    r, ctx = _adm("create")
    return r or h_new(ctx)


@ai_admin_bp.route("/design/<int:sid>", methods=["GET"])
def pick(sid):
    r, ctx = _adm("create")
    return r or h_pick(ctx, sid)


@ai_admin_bp.route("/design/<int:sid>/<any(select, restyle, vote, rewrite, words):what>", methods=["POST"])
def design_action(sid, what):
    r, ctx = _adm("create")
    return r or h_action(ctx, sid, what)


@ai_admin_bp.route("/design/<int:sid>/again", methods=["GET", "POST"])
def again(sid):
    r, ctx = _adm("create")
    return r or h_again(ctx, sid)


@ai_admin_bp.route("/design/<int:sid>/img/<int:idx>/<size>.png", methods=["GET"])
def design_img(sid, idx, size):
    r, ctx = _adm("create")
    return r or h_img(ctx, sid, idx, size)


@ai_admin_bp.route("/design/<int:sid>/finish", methods=["GET", "POST"])
def finish(sid):
    r, ctx = _adm("create")
    return r or h_finish(ctx, sid)


@ai_admin_bp.route("/brand-kit", methods=["GET", "POST"])
def brand_kit():
    r, ctx = _adm("modify" if request.method == "POST" else "view")
    return r or h_brand_kit(ctx)


@ai_admin_bp.route("/brand-kit/preview.png", methods=["GET"])
def brand_preview():
    r, ctx = _adm("view")
    return r or h_brand_preview(ctx)


@ai_admin_bp.route("/brand-kit/logo", methods=["GET"])
def brand_logo():
    r, ctx = _adm("view")
    return r or h_logo(ctx)


@ai_admin_bp.route("/design/usage", methods=["GET"])
def usage():
    r, ctx = _adm("view")
    return r or render_template("portal/ai_design_usage.html", **_common(ctx, log=D.usage_log(ctx.owner_key)))
