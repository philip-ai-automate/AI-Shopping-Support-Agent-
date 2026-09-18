"""
Meta Business AI sync (2026-09-12).

Lets a tenant hand their WhatsApp number's customer replies over to Meta's
own built-in AI agent instead of PhiXtra's, while PhiXtra stays the CRM
underneath. Two directions:

  1. OUTBOUND (this file): whenever a tenant saves Store Information or
     System Instruction in the portal, and tenants.meta_ai_enabled is TRUE,
     mirror that same content to Meta's Business Info / FAQ / Skills APIs
     so Meta's AI answers with the same knowledge and tone.
  2. INBOUND (separate piece, not built here): a connector Meta's AI calls
     into so leads it captures land in the Sales Pipeline.

Deliberately best-effort: a sync failure never blocks or rolls back the
portal save that triggered it — the merchant's own PhiXtra AI keeps working
regardless of whether the Meta side is reachable. Failures are recorded on
tenants.meta_ai_last_sync_error so the portal can show a plain status.

See project_phixtra_meta_business_agent_connector memory for the design
this implements, and https://developers.facebook.com/documentation/meta-business-agent/
for the API this calls.
"""

import re

import psycopg2.extras
import requests

from db import get_db_connection

META_API_BASE = "https://api.facebook.com"
API_VERSION = "2.0.0"
TIMEOUT = 15

# Public HTTPS address for the AI backend's Meta connector endpoint —
# chat.phixtra.com already proxies to the AI backend (127.0.0.1:8000) for an
# unrelated reason, and its "/" context forwards every path through
# unchanged, so this new route rides the same existing public domain rather
# than needing a new one.
PRODUCT_SEARCH_CONNECTOR_BASE_URL = "https://chat.phixtra.com"

# Store Info section key -> Business Info field it maps to. "contact" and
# "custom"/"about_us" don't split cleanly into Meta's structured
# contact_info.{email,hours_of_operation,address} sub-fields (PhiXtra asks
# for all of it in one free-text box) — best-effort placed under
# contact_info.address as a single block rather than guessed apart, and
# flagged here rather than silently pretending it's a clean split.
_BUSINESS_INFO_FIELD_MAP = {
    "payment":  "payment_method",
    "returns":  "return_policy",
    "delivery": "delivery_and_shipping",
}


def _get_wa_credentials(cur, tenant_id: int):
    """Returns (entity_id, access_token) for this tenant's connected WhatsApp
    number, or (None, None) if not connected yet — sync is a no-op then."""
    cur.execute(
        "SELECT phone_number_id, access_token FROM wa_tenants WHERE tenant_id=%s AND active=TRUE LIMIT 1",
        (tenant_id,),
    )
    row = cur.fetchone()
    if not row or not row.get("phone_number_id") or not row.get("access_token"):
        return None, None
    return row["phone_number_id"], row["access_token"]


def _meta_request(method: str, entity_id: str, path: str, access_token: str, json_body=None):
    url = f"{META_API_BASE}/{entity_id}{path}"
    resp = requests.request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-API-Version": API_VERSION,
        },
        json=json_body,
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Meta {method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.text else {}


def _meta_upload_file(entity_id: str, access_token: str, file_name: str, file_bytes: bytes):
    """Files uses multipart/form-data, not JSON — separate from _meta_request."""
    url = f"{META_API_BASE}/{entity_id}/agent_config/files"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {access_token}", "X-API-Version": API_VERSION},
        data={"file_name": file_name},
        files={"file": (file_name, file_bytes)},
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Meta POST /agent_config/files -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.text else {}


def _meta_delete_file(entity_id: str, access_token: str, file_id: str):
    url = f"{META_API_BASE}/{entity_id}/agent_config/files/{file_id}"
    resp = requests.delete(
        url,
        headers={"Authorization": f"Bearer {access_token}", "X-API-Version": API_VERSION},
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400 and resp.status_code != 404:
        raise RuntimeError(f"Meta DELETE /agent_config/files/{file_id} -> {resp.status_code}: {resp.text[:300]}")


def _get_synced_meta_id(cur, tenant_id: int, kind: str, key: str):
    cur.execute(
        "SELECT meta_id FROM meta_ai_synced_items WHERE tenant_id=%s AND kind=%s AND key=%s",
        (tenant_id, kind, key),
    )
    row = cur.fetchone()
    return row["meta_id"] if row else None


def _remember_synced_meta_id(conn, cur, tenant_id: int, kind: str, key: str, meta_id: str):
    cur.execute(
        """
        INSERT INTO meta_ai_synced_items (tenant_id, kind, key, meta_id, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (tenant_id, kind, key) DO UPDATE SET meta_id = EXCLUDED.meta_id, updated_at = NOW()
        """,
        (tenant_id, kind, key, meta_id),
    )
    conn.commit()


def sync_business_info(cur, entity_id: str, access_token: str, tenant_id: int):
    """Reads the tenant's Store Information sections and PUTs them to Meta's
    Business Info API. PUT fully replaces, so this is safe to call on every
    save without tracking a separate id."""
    cur.execute(
        "SELECT id, content FROM documents WHERE tenant_id=%s AND type='store_info'",
        (tenant_id,),
    )
    rows = {r["id"].rsplit("-", 1)[-1]: r["content"] for r in cur.fetchall()}

    body = {}
    for section_key, field_name in _BUSINESS_INFO_FIELD_MAP.items():
        text = rows.get(section_key)
        if text:
            body[field_name] = text[:2000]

    about = rows.get("about_us") or ""
    other = rows.get("custom") or ""
    description = "\n\n".join(t for t in (about, other) if t).strip()
    if description:
        body["business_description"] = description[:2000]

    contact_text = rows.get("contact")
    if contact_text:
        body["contact_info"] = {"address": contact_text[:2000]}

    if not body:
        return False  # nothing filled in yet — nothing to push
    _meta_request("PUT", entity_id, "/agent_config/business_info", access_token, body)
    return True


def sync_faq(cur, conn, entity_id: str, access_token: str, tenant_id: int):
    """PhiXtra's Store Information page collects FAQs as one free-text block,
    not discrete question/answer pairs the way Meta's API wants. Rather than
    guess a parser for arbitrary merchant text, this pushes the whole block
    as a single FAQ entry. If a merchant wants several distinct Q&As
    recognised separately on Meta's side, that needs its own follow-up
    (splitting the text box into repeatable question/answer rows)."""
    cur.execute(
        "SELECT content FROM documents WHERE id=%s",
        (f"store_info-{tenant_id}-faqs",),
    )
    row = cur.fetchone()
    text = (row["content"] if row else "") or ""
    if not text.strip():
        return False

    body = {
        "question": "What are your frequently asked questions?",
        "answer": text[:5000],
    }
    existing_id = _get_synced_meta_id(cur, tenant_id, "faq", "store_info_faq")
    if existing_id:
        _meta_request("PUT", entity_id, f"/agent_config/faq/{existing_id}", access_token, body)
    else:
        result = _meta_request("POST", entity_id, "/agent_config/faq", access_token, body)
        if result.get("id"):
            _remember_synced_meta_id(conn, cur, tenant_id, "faq", "store_info_faq", result["id"])
    return True


# Mirrors the wizard's own client-side reconstruction logic in
# ai_instruction.html (the `saved.indexOf(...)` checks) so the same known
# sentence fragments are recognised server-side. Order matches Step 2 on
# the page. A raw/custom-written prompt (is_custom_prompt) has no such
# structure and is pushed as one whole skill instead — see sync_skills().
_WIZARD_BEHAVIOUR_FRAGMENTS = [
    ("orders",    "collect their full name, delivery address",
     "Apply when a customer is ready to buy.",
     "wizard-orders"),
    ("recommend", "suggest one or two related or complementary",
     "Apply when a customer shows interest in a product.",
     "wizard-recommend"),
    ("promo",     "current promotions, discounts, or special deals",
     "Apply whenever relevant during the conversation.",
     "wizard-promo"),
    ("noprice",   "Never share specific prices",
     "Apply whenever a customer asks about pricing.",
     "wizard-noprice"),
]


def _extract_skill_fragments(system_prompt: str, wizard_marker: str):
    """Returns a list of (key, description, skill_text) tuples to push as
    separate Meta Skills. Falls back to one whole-prompt skill if this
    doesn't look like wizard-generated text (a hand-written custom prompt,
    or an older prompt predating the marker)."""
    # An empty/missing marker is not "found everywhere" — Python's str.split("")
    # raises ValueError rather than treating "" as absent, so it must be
    # checked first or every call missing a real marker crashes here.
    if not wizard_marker or wizard_marker not in (system_prompt or ""):
        text = (system_prompt or "").strip()
        if not text:
            return []
        return [("custom-instructions", "Apply to every conversation.", text[:20000])]

    customisation = system_prompt.split(wizard_marker, 1)[1].strip()
    fragments = []
    for _id, needle, description, key in _WIZARD_BEHAVIOUR_FRAGMENTS:
        # Find the one sentence containing this needle rather than pushing
        # the whole concatenated blob — matches how buildPreview() in
        # ai_instruction.html assembles each behaviour as its own sentence.
        for sentence in re.split(r"(?<=[.!?])\s+", customisation):
            if needle in sentence:
                fragments.append((key, description, sentence.strip()))
                break
    if not fragments:
        # Category sentence only (no behaviour checkboxes matched) — still
        # worth pushing as a single tone/context skill.
        first_sentence = customisation.split(".", 1)[0].strip()
        if first_sentence:
            fragments.append(("wizard-category", "Apply to every conversation.", first_sentence + "."))
    return fragments


# Extensions Meta's Files API accepts as-is (per its own reference). PhiXtra's
# own Store Information upload box accepts a wider list (.txt/.json/.xml too,
# unrestricted deliberately) — anything outside Meta's list still needs to be
# repackaged rather than sent raw.
_META_NATIVE_FILE_EXTENSIONS = {".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".csv", ".xlsx"}


def sync_files(cur, conn, entity_id: str, access_token: str, tenant_id: int) -> bool:
    """Pushes each document a merchant uploaded on Store Information (the
    "Upload a Document" box, distinct from the 7 fixed text sections) to
    Meta's Files knowledge source.

    Sends the ORIGINAL file unchanged whenever its format is one Meta
    already accepts (PDF/DOC/DOCX/PNG/JPG/JPEG/CSV/XLSX) — no rebuilding,
    no lossy conversion. Only a format Meta doesn't support at all
    (TXT/JSON/XML, which PhiXtra's own upload box still accepts) falls back
    to repackaging the extracted text into a .docx, since that's the only
    way those get in front of Meta's AI at all. Anything uploaded before the
    file_bytes column existed (file_bytes IS NULL) also falls back this way,
    since the original was never kept for those.

    The Files API has no PUT (per Meta's own reference) — re-syncing an
    already-uploaded document deletes the old Meta file first, then uploads
    a fresh one, rather than accumulating duplicates on every save."""
    import io
    from docx import Document

    cur.execute(
        "SELECT id, title, content, file_bytes, file_name FROM documents "
        "WHERE tenant_id=%s AND type='store_info' AND id LIKE %s",
        (tenant_id, f"store_info-{tenant_id}-upload-%"),
    )
    uploads = cur.fetchall()
    if not uploads:
        return False

    pushed_any = False
    for doc in uploads:
        doc_id = doc["id"]
        title = doc["title"] or "Document"
        content = doc["content"] or ""
        file_bytes = doc.get("file_bytes")
        file_name = doc.get("file_name") or ""
        ext = ("." + file_name.rsplit(".", 1)[-1].lower()) if "." in file_name else ""

        if file_bytes is not None and ext in _META_NATIVE_FILE_EXTENSIONS:
            upload_name = file_name
            upload_bytes = bytes(file_bytes)
        else:
            if not content.strip():
                continue  # nothing to rebuild from either — genuinely nothing to send
            docx_buf = io.BytesIO()
            d = Document()
            d.add_heading(title, level=1)
            for para in content.split("\n"):
                d.add_paragraph(para)
            d.save(docx_buf)
            safe_name = "".join(c if c.isalnum() or c in " -_." else "_" for c in title)[:80] or "document"
            upload_name = f"{safe_name}.docx"
            upload_bytes = docx_buf.getvalue()

        existing_id = _get_synced_meta_id(cur, tenant_id, "file", doc_id)
        if existing_id:
            try:
                _meta_delete_file(entity_id, access_token, existing_id)
            except Exception as e:
                print(f"⚠️ meta sync_files: could not delete old file {existing_id} (tenant {tenant_id}):", e)

        result = _meta_upload_file(entity_id, access_token, upload_name, upload_bytes)
        if result.get("id"):
            _remember_synced_meta_id(conn, cur, tenant_id, "file", doc_id, result["id"])
        pushed_any = True
    return pushed_any


def sync_skills(cur, conn, entity_id: str, access_token: str, tenant_id: int, system_prompt: str, wizard_marker: str):
    fragments = _extract_skill_fragments(system_prompt, wizard_marker)
    if not fragments:
        return False
    for key, description, skill_text in fragments:
        body = {
            "title": f"phixtra-{key}"[:64],
            "description": description[:1024],
            "skill": skill_text[:20000],
        }
        existing_id = _get_synced_meta_id(cur, tenant_id, "skill", key)
        if existing_id:
            _meta_request("PUT", entity_id, f"/agent_config/skills/{existing_id}", access_token, body)
        else:
            result = _meta_request("POST", entity_id, "/agent_config/skills", access_token, body)
            if result.get("id"):
                _remember_synced_meta_id(conn, cur, tenant_id, "skill", key, result["id"])
    return True


def check_eligibility(tenant_id: int) -> dict:
    """Asks Meta whether this tenant's connected WhatsApp number is allowed
    to use Business Agent at all. Read-only — changes nothing. Returns
    {"eligible": bool, "error": str|None}."""
    conn = get_db_connection()
    if not conn:
        return {"eligible": False, "error": "Could not reach the database."}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        entity_id, access_token = _get_wa_credentials(cur, tenant_id)
        if not entity_id:
            return {"eligible": False, "error": "No connected WhatsApp number yet."}
        result = _meta_request("GET", entity_id, "/agent_eligibility", access_token)
        eligible = bool(result.get("is_eligible"))
        cur.execute(
            "UPDATE tenants SET meta_ai_eligible=%s, meta_ai_eligibility_checked_at=NOW() WHERE id=%s",
            (eligible, tenant_id),
        )
        conn.commit()
        return {"eligible": eligible, "error": None}
    except Exception as e:
        conn.rollback()
        return {"eligible": False, "error": str(e)[:500]}
    finally:
        cur.close()
        conn.close()


def add_test_number_to_allowlist(tenant_id: int, phone_e164: str) -> dict:
    """Adds one phone number to Meta's allowlist for this tenant's entity —
    required before an ALLOWLISTED_ONLY go-live means anything, since Meta
    replies to nobody until at least one number is listed. phone_e164 must
    include the country code with a leading + (e.g. "+447503094646")."""
    conn = get_db_connection()
    if not conn:
        return {"ok": False, "error": "Could not reach the database."}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        entity_id, access_token = _get_wa_credentials(cur, tenant_id)
        if not entity_id:
            return {"ok": False, "error": "No connected WhatsApp number yet."}
        result = _meta_request("POST", entity_id, "/agent_config/allowlist", access_token,
                                {"consumer_phone_number": phone_e164})
        return {"ok": True, "error": None, "id": result.get("id")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}
    finally:
        cur.close()
        conn.close()


def sync_product_search_connector(tenant_id: int) -> dict:
    """Registers (or re-registers) the one Connector + one Tool that lets
    Meta's AI search this tenant's real product catalogue mid-conversation
    — the piece that stops Meta's AI falling back to a human handoff for
    every "do you have X" / "anything under [budget]" question, since
    without it Meta's AI has no way to see live product/price data at all.

    Idempotent: safe to call again later (e.g. if the tenant's phixtra_api_key
    ever changes) — updates the existing Connector/Tool in place via PUT
    rather than creating duplicates, using the ids remembered in
    meta_ai_synced_items from the first run.
    Returns {"ok": bool, "error": str|None}."""
    conn = get_db_connection()
    if not conn:
        return {"ok": False, "error": "Could not reach the database."}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        entity_id, access_token = _get_wa_credentials(cur, tenant_id)
        if not entity_id:
            return {"ok": False, "error": "No connected WhatsApp number yet."}

        cur.execute("SELECT phixtra_api_key FROM wa_tenants WHERE tenant_id=%s AND active=TRUE LIMIT 1", (tenant_id,))
        row = cur.fetchone()
        phixtra_key = row["phixtra_api_key"] if row else None
        if not phixtra_key:
            return {"ok": False, "error": "This business has no PhiXtra key on its WhatsApp connection yet."}

        connector_body = {
            "name": "PhiXtra Product Search",
            "description": (
                "Searches this business's real, live product catalogue managed in PhiXtra — "
                "by product name, category, brand, or a budget/price range (e.g. 'iPhone below "
                "800000 naira'). Use this whenever a customer asks whether a specific product is "
                "available, asks for recommendations, or gives a price range. Returns real "
                "in-stock products with their name, price, photo, and link."
            ),
            "base_url": PRODUCT_SEARCH_CONNECTOR_BASE_URL,
            "connector_protocol": "HTTP",
            "auth_type": "API_KEY",
            "auth_config": {
                "api_key": {
                    "headers": [
                        {"field_name": "X-PhiXtra-Key", "value": phixtra_key}
                    ]
                }
            },
        }

        connector_id = _get_synced_meta_id(cur, tenant_id, "connector", "product_search")
        if connector_id:
            _meta_request("PUT", entity_id, f"/agent_connectors/{connector_id}", access_token, connector_body)
        else:
            result = _meta_request("POST", entity_id, "/agent_connectors", access_token, connector_body)
            connector_id = result.get("id")
            if not connector_id:
                return {"ok": False, "error": "Meta did not return a connector id."}
            _remember_synced_meta_id(conn, cur, tenant_id, "connector", "product_search", connector_id)

        tool_body = {
            "name": "search_products",
            "description": (
                "Search this business's product catalogue. Call this whenever a customer asks "
                "about a specific product, category, brand, or price/budget range."
            ),
            "request_definition": {
                "method": "POST",
                "path": "/meta-connector/search-products",
                "body": {
                    "content_type": "application/json",
                    "params": {
                        "query": {
                            "type": "string",
                            "description": (
                                "The customer's product request in plain words, e.g. "
                                "'iPhone below 800000 naira' or 'wireless earphones'."
                            ),
                            "required": True,
                        }
                    },
                    "required": ["query"],
                },
            },
            "user_auth_required": False,
        }

        tool_id = _get_synced_meta_id(cur, tenant_id, "connector_tool", "product_search")
        if tool_id:
            _meta_request("PUT", entity_id, f"/agent_connectors/{connector_id}/tools/{tool_id}", access_token, tool_body)
        else:
            result = _meta_request("POST", entity_id, f"/agent_connectors/{connector_id}/tools", access_token, tool_body)
            if result.get("id"):
                _remember_synced_meta_id(conn, cur, tenant_id, "connector_tool", "product_search", result["id"])

        return {"ok": True, "error": None}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}
    finally:
        cur.close()
        conn.close()


def go_live_with_meta(tenant_id: int, ai_audience: str = "EVERYONE") -> dict:
    """The actual switch-over: tells Meta to start answering, and — for a
    real EVERYONE rollout only — turns PhiXtra's own AI off for the same
    tenant in the same call, so there's never a moment with both replying
    or neither.

    ai_audience: "EVERYONE" (real customers — needs a payment method on
    Meta's side already) or "ALLOWLISTED_ONLY" (safe to test with no
    payment method — only the specific numbers on Meta's allowlist get a
    reply from Meta; PhiXtra's own AI is deliberately left ON for everyone
    else, since Meta only covers the allowlisted numbers, not this
    business's real customers as a whole — turning PhiXtra's AI off here
    would leave every non-test customer with no reply from either side).
    Returns {"ok": bool, "error": str|None}."""
    conn = get_db_connection()
    if not conn:
        return {"ok": False, "error": "Could not reach the database."}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        entity_id, access_token = _get_wa_credentials(cur, tenant_id)
        if not entity_id:
            return {"ok": False, "error": "No connected WhatsApp number yet."}

        _meta_request("PUT", entity_id, "/agent_config/settings", access_token, {
            "rollout": {"enabled": True},
            "ai_audience": ai_audience,
        })

        # Only flip PhiXtra's own switch off once Meta's own call above has
        # actually succeeded, and only for a real EVERYONE rollout — if Meta
        # rejected the request, or this is only an allowlisted test, PhiXtra's
        # AI must keep answering rather than leaving real customers silent.
        if ai_audience == "EVERYONE":
            cur.execute("UPDATE tenants SET ai_enabled=FALSE WHERE id=%s", (tenant_id,))
            conn.commit()
        return {"ok": True, "error": None}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)[:500]}
    finally:
        cur.close()
        conn.close()


def revert_to_phixtra_ai(tenant_id: int) -> dict:
    """Undoes go_live_with_meta: tells Meta to stop responding, and turns
    PhiXtra's own AI back on for the same tenant. Best-effort on the Meta
    side — PhiXtra's own AI is switched back on regardless of whether the
    Meta call succeeds, since the point of this button is "get me out of a
    bad situation fast," not to leave the business silent while Meta is
    unreachable."""
    conn = get_db_connection()
    if not conn:
        return {"ok": False, "error": "Could not reach the database."}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    meta_error = None
    try:
        entity_id, access_token = _get_wa_credentials(cur, tenant_id)
        if entity_id:
            try:
                _meta_request("PUT", entity_id, "/agent_config/settings", access_token, {
                    "rollout": {"enabled": False},
                })
            except Exception as e:
                meta_error = str(e)[:500]

        cur.execute("UPDATE tenants SET ai_enabled=TRUE WHERE id=%s", (tenant_id,))
        conn.commit()
        return {"ok": True, "error": meta_error}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)[:500]}
    finally:
        cur.close()
        conn.close()


def _record_item_status(conn, cur, tenant_id: int, item: str, state: str, error: str = None):
    """Writes this one item's result into tenants.meta_ai_sync_status without
    touching the other two items' entries — a Business Info failure must
    never overwrite what Skills or FAQ already reported.
    state: "synced" (content was pushed), "skipped" (nothing to push — the
    section is empty, not a failure), or "failed" (Meta rejected the call)."""
    import json
    import datetime
    entry = {
        "state": state,
        "ok": state == "synced",  # kept for older callers reading this field
        "error": error,
        "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    cur.execute(
        "UPDATE tenants SET meta_ai_sync_status = meta_ai_sync_status || %s::jsonb WHERE id=%s",
        (json.dumps({item: entry}), tenant_id),
    )
    conn.commit()


def sync_all_to_meta(tenant_id: int, system_prompt: str = None, wizard_marker: str = ""):
    """Entry point called from the portal's Store Information / System
    Instruction save handlers. Safe no-op if the tenant hasn't opted in
    (meta_ai_enabled) or hasn't connected a WhatsApp number yet.

    Business Info, FAQ, and Skills are pushed independently — one failing
    (e.g. Meta returning a 500 on Business Info) does NOT stop the other two
    from being attempted, and each writes its own pass/fail into
    tenants.meta_ai_sync_status rather than sharing one bundled error field.
    Never raises."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT meta_ai_enabled, system_prompt FROM tenants WHERE id=%s", (tenant_id,))
        trow = cur.fetchone()
        if not trow or not trow.get("meta_ai_enabled"):
            return

        entity_id, access_token = _get_wa_credentials(cur, tenant_id)
        if not entity_id:
            no_wa = "No connected WhatsApp number yet."
            for item in ("business_info", "faq", "skills", "files"):
                _record_item_status(conn, cur, tenant_id, item, state="failed", error=no_wa)
            return

        prompt_to_use = system_prompt if system_prompt is not None else (trow.get("system_prompt") or "")

        try:
            pushed = sync_business_info(cur, entity_id, access_token, tenant_id)
            _record_item_status(conn, cur, tenant_id, "business_info", state="synced" if pushed else "skipped")
        except Exception as e:
            print(f"⚠️ meta sync business_info failed (tenant {tenant_id}):", e)
            conn.rollback()
            _record_item_status(conn, cur, tenant_id, "business_info", state="failed", error=str(e)[:500])

        try:
            pushed = sync_faq(cur, conn, entity_id, access_token, tenant_id)
            _record_item_status(conn, cur, tenant_id, "faq", state="synced" if pushed else "skipped")
        except Exception as e:
            print(f"⚠️ meta sync faq failed (tenant {tenant_id}):", e)
            conn.rollback()
            _record_item_status(conn, cur, tenant_id, "faq", state="failed", error=str(e)[:500])

        try:
            pushed = sync_skills(cur, conn, entity_id, access_token, tenant_id, prompt_to_use, wizard_marker)
            _record_item_status(conn, cur, tenant_id, "skills", state="synced" if pushed else "skipped")
        except Exception as e:
            print(f"⚠️ meta sync skills failed (tenant {tenant_id}):", e)
            conn.rollback()
            _record_item_status(conn, cur, tenant_id, "skills", state="failed", error=str(e)[:500])

        try:
            pushed = sync_files(cur, conn, entity_id, access_token, tenant_id)
            _record_item_status(conn, cur, tenant_id, "files", state="synced" if pushed else "skipped")
        except Exception as e:
            print(f"⚠️ meta sync files failed (tenant {tenant_id}):", e)
            conn.rollback()
            _record_item_status(conn, cur, tenant_id, "files", state="failed", error=str(e)[:500])

        cur.execute("UPDATE tenants SET meta_ai_last_synced_at=NOW() WHERE id=%s", (tenant_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
