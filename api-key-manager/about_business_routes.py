"""AI Agent › About your business (22 Nov 2026). Logic: about_business.py.

Replaces the Store Information pages and the WhatsApp / Website AI Instruction
pages (their old addresses open this page). The website connect / sync pages
stay where they are and are reached from the page's website section.
"""
import os
import re
import uuid

import psycopg2.extras
import requests
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import about_business as AB
from about_business_prompt import DAYS, IF_NOT, TASKS, pay_options, task_group
from feature_access import team_feature

about_bp = Blueprint("about", __name__)

_AI_URL = os.getenv("AI_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
VIEW_KEYS = ("store.info", "ai.instructions_view")
EDIT_KEYS = ("store.info_edit", "ai.instructions_edit")


def _can(keys) -> bool:
    from portal_routes import _team_member_has_permission
    return any(_team_member_has_permission(k) for k in keys)


def _guard(edit: bool):
    """(customer, tenant_id, None) or (None, None, response)."""
    from portal_routes import _customer_id, _get_customer, _require_login, _require_plan_sub_feature
    r = _require_login()
    if r:
        return None, None, r
    customer = _get_customer(_customer_id())
    if not customer:
        return None, None, redirect(url_for("portal.login"))
    gate = _require_plan_sub_feature(customer, "store.info", "About your business")
    if gate:
        return None, None, gate
    if not _can(EDIT_KEYS if edit else VIEW_KEYS):
        flash("You don't have permission to change this page. Ask the account owner." if edit
              else "You don't have permission to open this page. Ask the account owner.", "warning")
        return None, None, redirect(url_for("portal.dashboard"))
    return customer, int(customer["tenant_id"]), None


def _db():
    from db import get_db_connection
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _plan_has(tenant_id: int, key: str) -> bool:
    from portal_routes import _get_tenant_plan, _plan_grants_feature
    try:
        return bool(_plan_grants_feature(_get_tenant_plan(tenant_id), key))
    except Exception:
        return False


def _payment_terms(tenant_id: int) -> str:
    try:
        import quote_design
        return (quote_design.get(tenant_id).get("payment_terms") or "").strip()
    except Exception:
        return ""


def _has_active_agent(cur, tenant_id: int) -> bool:
    cur.execute("SELECT 1 FROM tenant_agents WHERE tenant_id=%s AND is_active LIMIT 1", (tenant_id,))
    return cur.fetchone() is not None


def _ai_off_by_plan(info: dict) -> bool:
    from portal_routes import AI_OFF_FREE_PLAN_SLUGS
    return info["plan_slug"] in AI_OFF_FREE_PLAN_SLUGS or info.get("ai_enabled") is False


@about_bp.route("/ai-agent/about-your-business", methods=["GET", "POST"])
@team_feature(*VIEW_KEYS)
def page():
    customer, tenant_id, err = _guard(edit=request.method == "POST")
    if err:
        return err
    AB.ensure_tables()
    conn, cur = _db()
    try:
        info = AB.business_info(cur, tenant_id)
        prod = AB.product_info(cur, tenant_id)
        web = AB.website_info(cur, tenant_id)
        p, first = AB.load(cur, tenant_id, info, prod)
        can_web = _plan_has(tenant_id, "store.website_connect")
        owns = AB.own_instructions(info, _has_active_agent(cur, tenant_id))

        if request.method == "POST":
            from portal_routes import _store_info_actor, _kick_reembed, sync_all_to_meta, _WIZARD_MARKER, insert_audit_log
            actor = _store_info_actor(customer)
            newp = AB.from_form(request.form, info, p)
            if info["model"] == "b2b":
                newp["payment_terms"] = _payment_terms(tenant_id)
            AB.save(conn, tenant_id, newp, actor["audit_username"], first, keep_old=bool(first and p.get("_old_too_long")))
            prompt_changed = None
            for o in owns:
                if o["field"] not in request.form:
                    continue
                text = (request.form.get(o["field"]) or "").strip()[:20000]
                if o["column"] == "system_prompt" and not text:
                    from ai_default_instruction import default_instruction
                    text = default_instruction(info["name"], None)
                if text == o["text"]:
                    continue
                c2 = conn.cursor()
                # An empty website instruction means the standard one is used.
                c2.execute(f"UPDATE tenants SET {o['column']}=%s WHERE id=%s", (text or None, tenant_id))
                conn.commit(); c2.close()
                if o["column"] == "system_prompt":
                    prompt_changed = text
            insert_audit_log(admin_username=actor["audit_username"], action="about_business_saved",
                             tenant_id=tenant_id, details={"first_time": first, "own_instruction_changed": bool(prompt_changed)})
            _kick_reembed()
            try:
                if prompt_changed:
                    sync_all_to_meta(tenant_id, system_prompt=prompt_changed, wizard_marker=_WIZARD_MARKER)
                else:
                    sync_all_to_meta(tenant_id, wizard_marker=_WIZARD_MARKER)
            except Exception as e:
                print("⚠️ about_business meta sync:", e)
            secs = AB.sections(info, newp, prod, web, can_web)
            if request.form.get("switch_offer") == "1":
                return redirect(url_for("about.page") + "#sec-about")
            if request.form.get("turn_on") == "1":
                return _turn_on(conn, cur, tenant_id, info, secs, actor)
            missing = [x for x in secs if x["status"] == "need"]
            if missing:
                flash("Saved. Still needed before your AI Agent can answer: " + ", ".join(x["title"] for x in missing) + ".", "warning")
            else:
                flash("Saved. Your AI Agent uses this from the next message.", "success")
            return redirect(url_for("about.page"))

        secs = AB.sections(info, p, prod, web, can_web)
        group = task_group(info["model"], p.get("sells") or "products")
        return render_template(
            "portal/about_business.html", customer=customer, info=info, p=p, first=first, secs=secs,
            sec={x["key"]: x for x in secs}, prod=prod, web=web, can_web=can_web, owns=owns,
            tasks=TASKS[group], if_not=IF_NOT, pay_opts=pay_options(p.get("sells") or "products"),
            days=DAYS, fixes=AB.corrections(cur, tenant_id), docs=AB.documents(cur, tenant_id),
            payment_terms=_payment_terms(tenant_id) if info["model"] == "b2b" else "",
            complete=AB.complete(secs), ready=bool(info["ai_agent_ready"] or info["is_demo"]),
            ai_off=_ai_off_by_plan(info), can_edit=_can(EDIT_KEYS),
        )
    finally:
        cur.close(); conn.close()


def _turn_on(conn, cur, tenant_id, info, secs, actor):
    from portal_routes import insert_audit_log
    if not AB.complete(secs):
        missing = [x["title"] for x in secs if x["status"] == "need"]
        flash("Saved. Before your AI Agent can answer, fill in: " + ", ".join(missing) + ".", "warning")
        return redirect(url_for("about.page"))
    cur.execute("UPDATE tenants SET ai_agent_ready=TRUE WHERE id=%s", (tenant_id,))
    conn.commit()
    insert_audit_log(admin_username=actor["audit_username"], action="ai_agent_turned_on", tenant_id=tenant_id, details={})
    if _ai_off_by_plan(info):
        flash("Saved. Your AI Agent is ready. It starts answering customers when your plan includes the AI Agent "
              "(start your free trial or choose a plan).", "success")
    else:
        flash("Your AI Agent is on. It answers new " + ("WhatsApp messages" if info["kind"] == "wa" else "chats")
              + " from now on. Your team can still take over any chat from the Inbox.", "success")
    return redirect(url_for("about.page"))


@about_bp.route("/ai-agent/about-your-business/test", methods=["POST"])
@team_feature(*VIEW_KEYS)
def test_chat():
    """Test chat: the business's own AI Agent answers, with what is saved now.
    Session ids start with playground- so they never show in the Inbox."""
    customer, tenant_id, err = _guard(edit=False)
    if err:
        return jsonify({"ok": False, "error": "Please reload the page and log in again."}), 401
    data = request.get_json(silent=True) or {}
    msg = (data.get("message") or "").strip()[:1000]
    sid = re.sub(r"[^a-z0-9]", "", (data.get("sid") or "").lower())[:24]
    if not msg or not sid:
        return jsonify({"ok": False, "error": "Type a message first."}), 400
    conn, cur = _db()
    try:
        info = AB.business_info(cur, tenant_id)
        if _ai_off_by_plan(info):
            return jsonify({"ok": False, "error": "Your plan doesn't include the AI Agent yet. Start your free trial or choose a plan to try it."})
        key = None
        if info["kind"] == "wa":
            cur.execute("SELECT phixtra_api_key FROM wa_tenants WHERE tenant_id=%s AND active AND phixtra_api_key IS NOT NULL LIMIT 1", (tenant_id,))
            r = cur.fetchone()
            key = r["phixtra_api_key"] if r else None
        if not key:
            cur.execute("""SELECT api_key_plain FROM api_keys WHERE tenant_id=%s AND is_active
                             AND COALESCE(api_key_plain,'') <> '' ORDER BY id DESC LIMIT 1""", (tenant_id,))
            r = cur.fetchone()
            key = r["api_key_plain"] if r else None
    finally:
        cur.close(); conn.close()
    if not key:
        return jsonify({"ok": False, "error": ("Connect your WhatsApp number first, then try the test chat."
                                               if info["kind"] == "wa" else "Connect your website first, then try the test chat.")})
    payload = {"api_key": key, "message": msg, "session_id": f"playground-ab{tenant_id}-{sid}"}
    if info["kind"] == "wa":
        payload["channel"] = "whatsapp"
    try:
        r = requests.post(f"{_AI_URL}/chat", json=payload, timeout=90)
        out = r.json() if r.ok else {}
    except Exception as e:
        print("⚠️ about test chat:", e)
        out = {}
    reply = (out.get("reply") or "").strip()
    if out.get("quota_exceeded"):
        return jsonify({"ok": False, "error": "Your AI Agent has used this month's messages, so it can't answer the test."})
    if not reply:
        return jsonify({"ok": False, "error": "Your AI Agent didn't answer. Please try again in a moment."})
    src = out.get("sources") if isinstance(out.get("sources"), dict) else {}
    return jsonify({"ok": True, "reply": reply, "handed_over": bool(out.get("handoff_triggered")),
                    "sources": src.get("items") or [], "not_found": bool(src.get("not_found"))})


@about_bp.route("/ai-agent/about-your-business/document", methods=["POST"])
@team_feature(*VIEW_KEYS)
def document():
    customer, tenant_id, err = _guard(edit=True)
    if err:
        return err
    from portal_routes import _extract_file_text, _store_info_actor, _kick_reembed, sync_all_to_meta, _WIZARD_MARKER, insert_audit_log
    actor = _store_info_actor(customer)
    action = request.form.get("action")
    conn, cur = _db()
    try:
        if action == "upload":
            f = request.files.get("doc_file")
            title = (request.form.get("doc_title") or "").strip()[:200]
            name = (f.filename if f else "") or ""
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            if not f or ext not in {".pdf", ".docx", ".txt", ".csv", ".json", ".xml"}:
                flash("Please choose a PDF, Word (.docx), text, CSV, JSON or XML file.", "warning")
                return redirect(url_for("about.page") + "#sec-docs")
            raw = f.read()
            import io

            class _F(io.BytesIO):
                pass
            shim = _F(raw); shim.filename = name
            try:
                text = _extract_file_text(shim)
            except Exception as e:
                print("⚠️ about document read:", e)
                text = ""
            if not text:
                flash("We couldn't read any text in that file. If it's a scanned picture, save it as text or Word first.", "warning")
                return redirect(url_for("about.page") + "#sec-docs")
            doc_id = f"store_info-{tenant_id}-upload-{uuid.uuid4().hex[:8]}"
            cur.execute("""INSERT INTO documents (id, tenant_id, type, title, content, file_bytes, file_name, updated_by, updated_at)
                           VALUES (%s, %s, 'store_info', %s, %s, %s, %s, %s, NOW())""",
                        (doc_id, tenant_id, title or name, text, psycopg2.Binary(raw), name, actor["audit_username"]))
            conn.commit()
            insert_audit_log(admin_username=actor["audit_username"], action="store_info_document_uploaded",
                             tenant_id=tenant_id, details={"doc_id": doc_id, "file_name": name})
            flash(f"'{title or name}' added. Your AI Agent can use it within a few minutes.", "success")
        elif action == "delete":
            doc_id = request.form.get("doc_id") or ""
            if doc_id.startswith(f"store_info-{tenant_id}-upload-"):
                cur.execute("DELETE FROM documents WHERE id=%s AND tenant_id=%s", (doc_id, tenant_id))
                conn.commit()
                insert_audit_log(admin_username=actor["audit_username"], action="store_info_document_deleted",
                                 tenant_id=tenant_id, details={"doc_id": doc_id})
                flash("Document removed.", "success")
    finally:
        cur.close(); conn.close()
    _kick_reembed()
    try:
        sync_all_to_meta(tenant_id, wizard_marker=_WIZARD_MARKER)
    except Exception as e:
        print("⚠️ about document meta sync:", e)
    return redirect(url_for("about.page") + "#sec-docs")


@about_bp.route("/ai-agent/about-your-business/correction", methods=["POST"])
@team_feature(*VIEW_KEYS)
def correction():
    customer, tenant_id, err = _guard(edit=True)
    if err:
        return err
    from portal_routes import _store_info_actor, _kick_reembed, sync_all_to_meta, _WIZARD_MARKER, insert_audit_log
    actor = _store_info_actor(customer)
    doc_id = request.form.get("doc_id") or ""
    if not doc_id.startswith(f"store_info-{tenant_id}-fix-"):
        return redirect(url_for("about.page") + "#sec-faq")
    conn, cur = _db()
    try:
        if request.form.get("action") == "delete":
            cur.execute("DELETE FROM documents WHERE id=%s AND tenant_id=%s", (doc_id, tenant_id))
            conn.commit()
            insert_audit_log(admin_username=actor["audit_username"], action="store_info_deleted",
                             tenant_id=tenant_id, details={"entry_id": doc_id})
            flash("Correction deleted.", "success")
        else:
            q = (request.form.get("question") or "").strip()[:250]
            a = (request.form.get("answer") or "").strip()[:4000]
            if not a:
                flash("Type the answer before saving the correction.", "warning")
                return redirect(url_for("about.page") + "#sec-faq")
            cur.execute("""UPDATE documents SET title=%s, content=%s, embedding=NULL, updated_by=%s, updated_at=NOW()
                            WHERE id=%s AND tenant_id=%s""", (q or "Customer question", a, actor["audit_username"], doc_id, tenant_id))
            conn.commit()
            insert_audit_log(admin_username=actor["audit_username"], action="store_info_updated",
                             tenant_id=tenant_id, details={"entry_id": doc_id})
            flash("Correction saved.", "success")
    finally:
        cur.close(); conn.close()
    _kick_reembed()
    try:
        sync_all_to_meta(tenant_id, wizard_marker=_WIZARD_MARKER)
    except Exception as e:
        print("⚠️ about correction meta sync:", e)
    return redirect(url_for("about.page") + "#sec-faq")
