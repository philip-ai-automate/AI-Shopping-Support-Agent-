"""
upload_design_routes.py — Social Posts › Upload Design pages (2026-09-25).
Rules and file work are in upload_design.py; posting goes through
buffer_routes.create_post_from_upload like every other post.

  GET  /social-posts/upload                         pick accounts + upload
  POST /social-posts/upload                         same, without JavaScript (files in the form)
  POST /social-posts/upload/start                   JS: make an empty draft for the picked accounts
  POST /social-posts/upload/<id>/file               JS: add one file (?replace=<item> / ?alt=<network>)
  GET  /social-posts/upload/<id>                    automatic size check + previews
  POST /social-posts/upload/<id>/remove/<item>      remove a file
  POST /social-posts/upload/<id>/networks           change the accounts
  GET/POST /social-posts/upload/<id>/fix/<network>  crop / borders / other version for one network
  GET/POST /social-posts/upload/<id>/post           carousel order, captions, when
  GET  /social-posts/upload/<id>/media/<name>       the draft's files (logged in)
  GET  /social-posts/size-guide
"""
import json
from datetime import datetime, timezone

from flask import Blueprint, request, render_template, redirect, url_for, flash, jsonify, send_from_directory, abort

from feature_access import team_feature
from portal_routes import (_require_login, _customer_id, _get_customer, _require_plan_sub_feature,
                           _require_team_permission, _current_actor, _inject_granted_features,
                           _inject_connect_flag)
import buffer_accounts as ba
import upload_design as U

upload_bp = Blueprint("upload", __name__)
upload_bp.context_processor(_inject_granted_features)
upload_bp.context_processor(_inject_connect_flag)

PLAN_KEY = "social.posts_view"


def _ctx():
    """(response, customer, channels). Buffer must be connected."""
    customer = _get_customer(_customer_id())
    r = _require_plan_sub_feature(customer, PLAN_KEY, "Social Posts")
    if r:
        return r, None, None
    owner = ba.tenant_owner(customer["tenant_id"])
    if not ba.is_connected(owner):
        flash("Connect Buffer first on Integration › Buffer. Social Posts publishes through it.", "warning")
        return redirect(url_for("buffer.connect")), None, None
    if request.method == "GET":
        ba.refresh_channels_if_stale(owner)
    chans = []
    for c in ba.list_channels(owner, enabled_only=True):
        chans.append({"channel_id": c["channel_id"], "service": c["service"], "label": c["label"],
                      "title": c["title"], "colour": c["colour"],
                      "unsupported": U.UNSUPPORTED.get(c["service"]) or (None if c["service"] in U.SPECS else "Not supported by Upload Design yet.")})
    return None, customer, chans


def _draft_or_404(customer, did):
    d = U.get_draft(int(customer["tenant_id"]), did)
    if not d:
        abort(404)
    if U.refresh_jobs(d):
        U.save_draft(d)
    return d


def _services(d):
    seen, out = set(), []
    for c in d["channels"]:
        if c["service"] not in seen:
            seen.add(c["service"])
            out.append(c["service"])
    return out


def _picked(chans, ids):
    ids = set(ids)
    return [c for c in chans if c["channel_id"] in ids and not c["unsupported"]]


def _common(**kw):
    kw.update(specs=U.SPECS, fmt_bytes=U.fmt_bytes, fmt_dur=U.fmt_dur, fmt_limit=U.fmt_limit)
    return kw


# ── start ───────────────────────────────────────────────────────────────────

@upload_bp.route("/social-posts/upload", methods=["GET", "POST"])
@team_feature("social.posts_create")
def start():
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, customer, chans = _ctx()
    if r:
        return r
    if request.method == "POST":   # no-JavaScript path
        picked = _picked(chans, request.form.getlist("channel_ids"))
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not picked:
            flash("Pick at least one social account.", "warning")
            return redirect(url_for("upload.start"))
        if not files:
            flash("Choose at least one image or video.", "warning")
            return redirect(url_for("upload.start"))
        did = U.create_draft(int(customer["tenant_id"]), picked, _current_actor(customer)["label"])
        d = U.get_draft(int(customer["tenant_id"]), did)
        for f in files[:U.MAX_ITEMS]:
            try:
                d["items"].append(U.store_upload(f))
            except U.UploadError as e:
                flash(str(e), "warning")
        U.save_draft(d)
        return redirect(url_for("upload.check", did=did))
    return render_template("portal/upload_start.html", **_common(customer=customer, channels=chans))


@upload_bp.route("/social-posts/upload/start", methods=["POST"])
@team_feature("social.posts_create")
def start_json():
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return jsonify(ok=False, error="Please log in again."), 401
    r, customer, chans = _ctx()
    if r:
        return jsonify(ok=False, error="Connect Buffer first."), 400
    picked = _picked(chans, request.form.getlist("channel_ids"))
    if not picked:
        return jsonify(ok=False, error="Pick at least one social account."), 400
    did = U.create_draft(int(customer["tenant_id"]), picked, _current_actor(customer)["label"])
    return jsonify(ok=True, id=did, check_url=url_for("upload.check", did=did))


@upload_bp.route("/social-posts/upload/<int:did>/file", methods=["POST"])
@team_feature("social.posts_create")
def add_file(did):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return jsonify(ok=False, error="Please log in again."), 401
    r, customer, _chans = _ctx()
    if r:
        return jsonify(ok=False, error="Connect Buffer first."), 400
    d = _draft_or_404(customer, did)
    if d.get("post_id"):
        return jsonify(ok=False, error="This design was already posted. Start a new upload."), 400
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="No file received. Try again."), 400
    alt, replace = request.args.get("alt"), request.args.get("replace")
    try:
        item = U.store_upload(f)
    except U.UploadError as e:
        return jsonify(ok=False, error=str(e)), 400
    if alt:
        if alt not in _services(d):
            U.remove_files([item.get("file"), item.get("thumb")])
            return jsonify(ok=False, error="That network isn't in this post."), 400
        fx = (d.get("fixes") or {}).get(alt)
        items = (fx.get("alt_items") if fx and fx.get("mode") == "alt" else None) or []
        if items and (item["kind"] == "video" or items[0]["kind"] == "video"):
            U.set_alt(d, alt, [item])
        else:
            keep = list(items)
            d.setdefault("fixes", {})[alt] = {"mode": "alt", "alt_items": keep + [item]}
            if fx and fx.get("mode") != "alt":
                U.remove_files(U._fix_files(fx))
    elif replace:
        idx = next((i for i, it in enumerate(d["items"]) if it["id"] == replace), None)
        if idx is None:
            U.remove_files([item.get("file"), item.get("thumb")])
            return jsonify(ok=False, error="That file is no longer in this post."), 400
        old = d["items"][idx]
        item["id"] = old["id"]
        d["items"][idx] = item
        U.remove_files([old.get("file"), old.get("thumb")])
        for svc in list((d.get("fixes") or {}).keys()):
            if d["fixes"][svc].get("mode") in ("crop", "fit"):
                U.clear_fix(d, svc)
    else:
        kinds = {it["kind"] for it in d["items"]}
        if item["kind"] == "video" and d["items"] or "video" in kinds:
            U.remove_files([item.get("file"), item.get("thumb")])
            return jsonify(ok=False, error="A post can have one video, or up to 10 images, not both. Start a new upload for the video."), 400
        if len(d["items"]) >= U.MAX_ITEMS:
            U.remove_files([item.get("file"), item.get("thumb")])
            return jsonify(ok=False, error=f"Up to {U.MAX_ITEMS} images per post."), 400
        d["items"].append(item)
        for svc in list((d.get("fixes") or {}).keys()):
            if d["fixes"][svc].get("mode") in ("crop", "fit"):
                U.clear_fix(d, svc)
    U.save_draft(d)
    return jsonify(ok=True, item={k: v for k, v in item.items() if not k.startswith("_")})


# ── check ───────────────────────────────────────────────────────────────────

@upload_bp.route("/social-posts/upload/<int:did>", methods=["GET"])
@team_feature("social.posts_create")
def check(did):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, customer, chans = _ctx()
    if r:
        return r
    d = _draft_or_404(customer, did)
    if d.get("post_id"):
        flash("This design was already posted.", "info")
        return redirect(url_for("buffer.posts"))
    results = {svc: U.check(d, svc) for svc in _services(d)}
    previews = []
    for svc in _services(d):
        media = U.media_for(d, svc)
        if media:
            m = media[0]
            previews.append({"service": svc, "item": m, "count": len(media),
                             "frame": U.display_ratio(svc, m) if m.get("w") else 1})
    processing = any(v["level"] == "processing" for v in results.values())
    can_continue = bool(d["items"]) and all(v["level"] in ("ok", "warn") for v in results.values())
    picked_ids = {c["channel_id"] for c in d["channels"]}
    return render_template("portal/upload_check.html", **_common(
        customer=customer, d=d, results=results, previews=previews, processing=processing,
        can_continue=can_continue, channels=chans, picked_ids=picked_ids))


@upload_bp.route("/social-posts/upload/<int:did>/remove/<item_id>", methods=["POST"])
@team_feature("social.posts_create")
def remove_item(did, item_id):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, customer, _c = _ctx()
    if r:
        return r
    d = _draft_or_404(customer, did)
    it = next((x for x in d["items"] if x["id"] == item_id), None)
    if it:
        d["items"].remove(it)
        U.remove_files([it.get("file"), it.get("thumb")])
        for svc in list((d.get("fixes") or {}).keys()):
            if d["fixes"][svc].get("mode") in ("crop", "fit"):
                U.clear_fix(d, svc)
        U.save_draft(d)
    return redirect(url_for("upload.check", did=did))


@upload_bp.route("/social-posts/upload/<int:did>/networks", methods=["POST"])
@team_feature("social.posts_create")
def networks(did):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, customer, chans = _ctx()
    if r:
        return r
    d = _draft_or_404(customer, did)
    picked = _picked(chans, request.form.getlist("channel_ids"))
    if not picked:
        flash("Keep at least one social account.", "warning")
        return redirect(url_for("upload.check", did=did))
    d["channels"] = picked
    for svc in list((d.get("fixes") or {}).keys()):
        if svc not in {c["service"] for c in picked}:
            U.clear_fix(d, svc)
    U.save_draft(d)
    return redirect(url_for("upload.check", did=did))


@upload_bp.route("/social-posts/upload/<int:did>/media/<name>", methods=["GET"])
@team_feature("social.posts_create")
def draft_media(did, name):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    customer = _get_customer(_customer_id())
    d = U.get_draft(int(customer["tenant_id"]), did)
    if not d or name not in U._draft_files(d):
        abort(404)
    return send_from_directory(U.FOLDER, name, conditional=True)


# ── fix one network ─────────────────────────────────────────────────────────

@upload_bp.route("/social-posts/upload/<int:did>/fix/<service>", methods=["GET", "POST"])
@team_feature("social.posts_create")
def fix(did, service):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, customer, _c = _ctx()
    if r:
        return r
    d = _draft_or_404(customer, did)
    if service not in _services(d) or service not in U.SPECS:
        abort(404)
    spec = U.SPECS[service]
    is_video = any(it["kind"] == "video" for it in d["items"])
    ratios = [s[0] for s in (spec["shapes"] if not is_video else [(spec["video"]["best"],)] + [("1:1",), ("4:5",), ("16:9",), ("9:16",)])]
    ratios = list(dict.fromkeys(r_ for r_ in ratios if r_ in U.R))
    if request.method == "POST":
        mode = request.form.get("mode")
        if mode == "original":
            U.clear_fix(d, service)
        elif mode in ("crop", "fit"):
            ratio = request.form.get("ratio") if request.form.get("ratio") in U.R else ratios[0]
            crops = {}
            try:
                raw = json.loads(request.form.get("crops") or "{}")
                for k, v in raw.items():
                    if isinstance(v, list) and len(v) == 4:
                        x, y, w, h = [max(0.0, min(1.0, float(n))) for n in v]
                        if w > 0.02 and h > 0.02 and x + w <= 1.001 and y + h <= 1.001:
                            crops[str(k)] = [x, y, w, h]
            except (ValueError, TypeError):
                crops = {}
            colour = request.form.get("colour") or "#FFFFFF"
            if colour != "blur" and not (len(colour) == 7 and colour.startswith("#")):
                colour = "#FFFFFF"
            try:
                U.make_fix(d, service, mode, ratio, crops, colour)
            except Exception as e:
                print("⚠️ upload fix:", e)
                flash("That change couldn't be made. Try again, or upload a version made for this network.", "warning")
                return redirect(url_for("upload.fix", did=did, service=service))
        elif mode == "alt":
            fx = (d.get("fixes") or {}).get(service)
            if not (fx and fx.get("mode") == "alt" and fx.get("alt_items")):
                flash(f"Upload the {spec['label']} version first.", "warning")
                return redirect(url_for("upload.fix", did=did, service=service))
        U.save_draft(d)
        flash(f"Saved for {spec['label']}.", "success")
        return redirect(url_for("upload.check", did=did))
    import ai_designer as _D
    kit = _D.get_brand_kit(_D.tenant_owner(customer["tenant_id"]))
    return render_template("portal/upload_fix.html", **_common(
        customer=customer, d=d, service=service, spec=spec, ratios=ratios, ratio_values=U.R,
        fx=(d.get("fixes") or {}).get(service), result=U.check(d, service), is_video=is_video,
        kit_colours=[("Brand background", kit["color_bg"]), ("Main", kit["color_main"]),
                     ("Highlight", kit["color_accent"]), ("White", "#FFFFFF"), ("Black", "#000000")]))


# ── captions & schedule ─────────────────────────────────────────────────────

@upload_bp.route("/social-posts/upload/<int:did>/post", methods=["GET", "POST"])
@team_feature("social.posts_create")
def post(did):
    r = _require_login() or _require_team_permission("social.posts_create")
    if r:
        return r
    r, customer, _c = _ctx()
    if r:
        return r
    d = _draft_or_404(customer, did)
    if d.get("post_id"):
        return redirect(url_for("buffer.posts"))
    services = _services(d)
    results = {svc: U.check(d, svc) for svc in services}
    blocked = [U.SPECS.get(s, {}).get("label", s) for s, v in results.items() if v["level"] not in ("ok", "warn")]
    if blocked:
        flash("Fix these first: " + ", ".join(blocked) + ".", "warning")
        return redirect(url_for("upload.check", did=did))
    has_video = any(it["kind"] == "video" for it in d["items"])

    if request.method == "POST":
        order = [x for x in (request.form.get("order") or "").split(",") if x]
        if order and set(order) == {it["id"] for it in d["items"]}:
            d["items"].sort(key=lambda it: order.index(it["id"]))
            U.save_draft(d)   # fix outputs are keyed by item id, so they follow the new order
        per_network = request.form.get("per_network") == "1"
        common = (request.form.get("caption") or "").strip()
        captions = {}
        for svc in services:
            captions[svc] = ((request.form.get(f"caption__{svc}") or "").strip() if per_network else common)
        action = request.form.get("action") or "draft"
        problems = []
        if action != "draft" and not any(captions.values()) and not has_video:
            problems.append("Write a caption.")
        for svc in services:
            problems += U.caption_problems(svc, captions[svc], has_video)
        if problems:
            flash(" ".join(problems), "warning")
            return render_template("portal/upload_post.html", **_common(
                customer=customer, d=d, services=services, has_video=has_video, results=results,
                form=request.form, limits={s: U.caption_limit(s, has_video) for s in services}))
        media = {"_default": [{"kind": it["kind"], "file": it["file"], "thumb": it.get("thumb")} for it in d["items"]]}
        for svc in services:
            lst = U.media_for(d, svc)[: U.SPECS[svc]["max_images"] if not has_video else 1]
            media[svc] = [{"kind": m["kind"], "file": m["file"], "thumb": m.get("thumb")} for m in lst]
        first = d["items"][0]
        cover = first.get("thumb") if first["kind"] == "video" else first["file"]
        from buffer_routes import create_post_from_upload, _parse_when
        when = _parse_when(request.form.get("scheduled_for_utc")) if action == "schedule" else None
        ok, msg, post_id = create_post_from_upload(customer, captions, media, cover, [c["channel_id"] for c in d["channels"]],
                                                   action, when, _current_actor(customer)["label"])
        if not post_id:
            flash(msg, "warning")
            return render_template("portal/upload_post.html", **_common(
                customer=customer, d=d, services=services, has_video=has_video, results=results,
                form=request.form, limits={s: U.caption_limit(s, has_video) for s in services}))
        d["post_id"] = post_id
        U.save_draft(d)
        flash(msg, "success" if ok else "danger")
        return redirect(url_for("buffer.posts"))
    return render_template("portal/upload_post.html", **_common(
        customer=customer, d=d, services=services, has_video=has_video, results=results, form={},
        limits={s: U.caption_limit(s, has_video) for s in services}))


@upload_bp.route("/social-posts/size-guide", methods=["GET"])
@team_feature("social.posts_view")
def size_guide():
    r = _require_login() or _require_team_permission("social.posts_view")
    if r:
        return r
    customer = _get_customer(_customer_id())
    r = _require_plan_sub_feature(customer, PLAN_KEY, "Social Posts")
    if r:
        return r
    return render_template("portal/upload_size_guide.html", **_common(customer=customer, extra=U.GUIDE_EXTRA))
