

# ===== Phixtra Safe Recommendation Guard =====
def _validate_products(products):
    """Allow only real catalog products with URL, product_id, and a non-zero price."""
    safe = []
    for p in products:
        url = p.get("url")
        product_id = p.get("product_id") or p.get("id")
        name = p.get("name")
        price = p.get("price") or ""
        if url and product_id and name and price:
            safe.append(p)
    return safe
# ===== End Guard =====

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uuid
import os
import json as _json
import re as _re
import psycopg2.extras

from auth import verify_api_key
from search import search_documents, search_documents_with_meta, search_related_products, upsert_verified_spec
from llm import ask_llm
from db import get_db_connection, insert_audit_log
from memory_store import (
    init_memory_tables,
    ensure_session,
    get_session_status,
    add_message,
    maybe_summarize_session,
    get_history_with_summary,
    get_semantic_history,
)
from billing import ensure_billing_tables, deduct_tokens, maybe_send_low_balance_alert
from cart_db import (
    log_cart_event,
    get_session_events,
    upsert_abandonment_queue,
    get_queue_row_by_session,
    mark_queue_status,
    log_recovery_action,
    expire_stale_queue_entries,
)
from cart_scorer import compute_intent_score, RECOVERY_THRESHOLD
from cart_recovery import start_recovery_sequence
from web_spec_lookup import is_spec_question, lookup_spec_verified


app = FastAPI(title="ProfitBuyz AI Support API")

from chatwoot_webhook import router as _chatwoot_router
app.include_router(_chatwoot_router)

from estate.main import estate_router as _estate_router
app.include_router(_estate_router)

app.add_middleware(
    CORSMiddleware,
        # Multi-tenant widgets load from customer domains. Since we do not use cookies
    # (auth is via API key in request body) and allow_credentials=False, it is safe
    # to allow cross-origin requests from any origin.
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_memory_tables()
ensure_billing_tables()
# Expire any stale recovery queue entries from previous server runs
try:
    expire_stale_queue_entries()
except Exception as _e:
    print("⚠️ expire_stale_queue_entries on startup failed:", _e)


class ChatRequest(BaseModel):
    api_key: str
    message: str
    session_id: str | None = None
    system_addon: str | None = None           # injected by WA gateway for pending-order context
    override_system_prompt: str | None = None  # per-WA-number agent prompt (overrides tenant default)


def record_usage_event(
    tenant_id: int,
    api_key_id: int,
    website: str | None,
    key_type: str | None,
    session_id: str | None,
    used_tokens: int,
):
    """Writes a lightweight usage row for portal analytics. Never throws."""
    if used_tokens <= 0:
        return

    conn = get_db_connection()
    if not conn:
        return

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO usage_events (tenant_id, api_key_id, website, key_type, session_id, used_tokens)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (tenant_id, api_key_id, website, key_type, session_id, used_tokens),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ record_usage_event failed:", e)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def record_token_usage(
    api_key_id: int,
    tenant_id: int,
    website: str | None,
    key_type: str | None,
    used_now: int,
    token_limit: int | None,
    session_id: str | None,
):
    """
    Updates api_keys.tokens_used, writes audit log, writes usage_events,
    enforces trial token limits, and enforces PAID credit balance.

    IMPORTANT: This function never raises to avoid breaking /chat.
    """
    if used_now <= 0:
        return

    conn = get_db_connection()
    if not conn:
        return

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # 1) Always add to cumulative tokens_used
        cur.execute(
            "UPDATE api_keys SET tokens_used = tokens_used + %s WHERE id=%s",
            (used_now, api_key_id),
        )
        conn.commit()

        # 2) Read back key row
        cur.execute(
            "SELECT tokens_used, is_active, token_limit, key_type FROM api_keys WHERE id=%s",
            (api_key_id,),
        )
        row = cur.fetchone() or {}

        tokens_used_total = int(row.get("tokens_used") or 0)
        token_limit_db = row.get("token_limit")
        key_type_db = row.get("key_type") or key_type

        # 3) Credit enforcement for PAID keys (tenant-level token balance)
        if key_type_db == "paid":
            ok, new_balance_tokens = deduct_tokens(int(tenant_id), int(used_now))

            if not ok:
                # Balance hit 0 → deactivate this paid key
                cur.execute("UPDATE api_keys SET is_active=FALSE WHERE id=%s", (api_key_id,))
                conn.commit()

                insert_audit_log(
                    action="paid_credits_exhausted_deactivated",
                    tenant_id=tenant_id,
                    website=website,
                    key_type="paid",
                    api_key_id=api_key_id,
                    details={"used_now": used_now, "new_balance_tokens": int(new_balance_tokens)},
                )

            # Low-balance email alerts (best effort)
            try:
                maybe_send_low_balance_alert(int(tenant_id), int(new_balance_tokens))
            except Exception:
                pass

        # 4) Deactivate trial if it crosses token_limit
        if key_type_db == "trial" and token_limit_db is not None and tokens_used_total >= int(token_limit_db):
            cur.execute("UPDATE api_keys SET is_active=FALSE WHERE id=%s", (api_key_id,))
            conn.commit()
            insert_audit_log(
                action="trial_token_limit_deactivated",
                tenant_id=tenant_id,
                website=website,
                key_type="trial",
                api_key_id=api_key_id,
                details={"token_limit": int(token_limit_db), "tokens_used": tokens_used_total},
            )

        # 5) Usage row for portal
        record_usage_event(
            tenant_id=int(tenant_id),
            api_key_id=int(api_key_id),
            website=website,
            key_type=key_type_db,
            session_id=session_id,
            used_tokens=int(used_now),
        )

        # 6) Audit
        insert_audit_log(
            action="token_usage",
            tenant_id=tenant_id,
            website=website,
            key_type=key_type_db,
            api_key_id=api_key_id,
            details={
                "used_now": used_now,
                "tokens_used_total": tokens_used_total,
                "token_limit": token_limit_db,
                "session_id": session_id,
            },
        )

    except Exception as e:
        print("⚠️ record_token_usage failed:", e)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ── Product recommendation tag pattern ───────────────────────────────────────
# The AI appends this tag when it wants to recommend products.
# We parse and strip it from the visible reply.
_PRODUCT_TAG_PATTERN = r'<<<PHIXTRA_PRODUCTS:(\[.*?\])>>>'

# ── System prompt instruction appended when feature is enabled ────────────────
_PRODUCT_REC_INSTRUCTION = (
    "\n\n[PRODUCT DISPLAY — CRITICAL RULE]\n"
    "The system automatically sends the customer rich product cards with images, full specs, "
    "prices, and an order button. YOU MUST NEVER write product names, prices, model numbers, "
    "or URLs in your text reply. If you list products in text, the customer sees them TWICE "
    "(your text AND the automatic cards) which looks broken and unprofessional.\n\n"
    "When a customer asks about a product or wants to buy something:\n"
    "1. Write ONE short sentence introducing the results (e.g. 'Here are some great iPhone 13 options for you!')\n"
    "2. Append this exact tag at the very END of your message with up to 6 product names:\n"
    '<<<PHIXTRA_PRODUCTS:["Exact Title One","Exact Title Two","Exact Title Three"]>>>\n\n'
    "CORRECT example:\n"
    "Customer: 'I want iPhone 13'\n"
    "Your reply: 'Great choice — here are the iPhone 13 options we have in stock for you! "
    '<<<PHIXTRA_PRODUCTS:["Apple iPhone 13 128gb [UK Used] Midnight","Apple iPhone 13 - [UK Used] - starlight / very-good / 128GB"]>>>\'\n\n'
    "WRONG — NEVER do this:\n"
    "Customer: 'I want iPhone 13'\n"
    "Your reply: '1. Apple iPhone 13 Midnight — ₦740,000\\n2. Apple iPhone 13 512GB — ₦777,000\\nWhich would you like?'\n\n"
    "Additional rules:\n"
    "- Copy product names EXACTLY as they appear in the Title: field of the store data.\n"
    "- Maximum 6 products in the tag. Always include as many relevant matches as you find, up to 6.\n"
    "- The tag must always be at the very END — never in the middle.\n"
    "- The tag is stripped automatically before the customer sees your message.\n"
    "- If you are NOT recommending a specific product, omit the tag entirely."
)


def _check_quota(tenant_id: int) -> dict:
    """
    Returns quota state for a tenant.
    dict keys: allowed, messages_used, messages_limit, is_overage, plan_slug, overage_rate_ngn
    """
    from datetime import date as _date
    conn = get_db_connection()
    if not conn:
        return {"allowed": True, "messages_used": 0, "messages_limit": -1,
                "is_overage": False, "plan_slug": "free", "overage_rate_ngn": 10.0}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT t.plan_period_start, t.quota_notified_at,
                   COALESCE(p.slug, 'free')              AS plan_slug,
                   COALESCE(p.ai_messages_limit, 100)    AS ai_messages_limit,
                   COALESCE(p.overage_per_msg_ngn, 10)   AS overage_rate_ngn
            FROM tenants t
            LEFT JOIN plans p ON p.id = t.plan_id
            WHERE t.id = %s
        """, (tenant_id,))
        row = cur.fetchone()
        if not row:
            return {"allowed": True, "messages_used": 0, "messages_limit": 100,
                    "is_overage": False, "plan_slug": "free", "overage_rate_ngn": 10.0}

        limit       = int(row["ai_messages_limit"])
        period_start = row["plan_period_start"] or _date.today().replace(day=1)

        cur.execute("""
            SELECT COUNT(*) AS used FROM usage_events
            WHERE tenant_id=%s AND created_at >= %s
        """, (tenant_id, period_start))
        used = int((cur.fetchone() or {}).get("used") or 0)

        allowed   = limit == -1 or used < limit
        is_over   = limit != -1 and used >= limit

        return {
            "allowed":          allowed,
            "messages_used":    used,
            "messages_limit":   limit,
            "is_overage":       is_over,
            "plan_slug":        row["plan_slug"],
            "overage_rate_ngn": float(row["overage_rate_ngn"]),
        }
    except Exception as e:
        print("⚠️ _check_quota error:", e)
        return {"allowed": True, "messages_used": 0, "messages_limit": -1,
                "is_overage": False, "plan_slug": "free", "overage_rate_ngn": 10.0}
    finally:
        cur.close()
        conn.close()


def _log_overage_event(tenant_id: int, quota: dict) -> None:
    """Record one overage event for billing. Never throws."""
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO quota_overage_log (tenant_id, plan_slug, msgs_used, msgs_limit, rate_ngn)
            VALUES (%s, %s, %s, %s, %s)
        """, (tenant_id, quota.get("plan_slug"), quota.get("messages_used"),
              quota.get("messages_limit"), quota.get("overage_rate_ngn")))
        conn.commit()
    except Exception as e:
        print("⚠️ _log_overage_event error:", e)
    finally:
        cur.close()
        conn.close()


@app.post("/chat")
def chat(req: ChatRequest):
    tenant, error = verify_api_key(req.api_key)
    if error:
        raise HTTPException(status_code=401, detail=error)

    tenant_id = tenant["tenant_id"]

    # ── Quota check ───────────────────────────────────────────────────────────
    _quota = _check_quota(int(tenant_id))
    if not _quota["allowed"]:
        print(f"🚫 [QUOTA] tenant={tenant_id} BLOCKED — {_quota['messages_used']}/{_quota['messages_limit']} msgs used")
        _log_overage_event(int(tenant_id), _quota)
        return {
            "reply": "",
            "session_id": req.session_id or "",
            "product_recommendations": [],
            "quota_exceeded": True,
            "plan_slug": _quota["plan_slug"],
            "messages_used": _quota["messages_used"],
            "messages_limit": _quota["messages_limit"],
        }
    # ─────────────────────────────────────────────────────────────────────────
    # Per-number agent prompt overrides the tenant default (set by WA gateway)
    if req.override_system_prompt:
        system_prompt = req.override_system_prompt
    else:
        system_prompt = tenant["system_prompt"] or ""
    semantic_config = tenant.get("azure_semantic_config") or ""

    # Pending-order context from WA gateway — appended to whichever prompt is active
    if req.system_addon:
        system_prompt = system_prompt + "\n\n" + req.system_addon

    session_id = req.session_id or uuid.uuid4().hex

    print(f"✅ /chat tenant_id={tenant_id} session_id={session_id}")

    # ── Product Recommendation Feature Check ─────────────────────────────────
    # Read the features JSON from the tenant row (added by portal_migrations.py).
    # auth.py's SQL already selects all tenant columns so this arrives automatically.
    _raw_features = tenant.get("features")
    _features = {}
    if isinstance(_raw_features, str):
        try:
            _features = _json.loads(_raw_features)
        except Exception:
            _features = {}
    elif isinstance(_raw_features, dict):
        _features = _raw_features

    rec_enabled = bool(_features.get("product_recommendation", True))
    related_enabled = bool(_features.get("related_products", False))

    # Product recommendation instruction goes FIRST — before handoff rules —
    # so it carries maximum weight with the model.
    if rec_enabled:
        system_prompt = system_prompt + _PRODUCT_REC_INSTRUCTION

    # ── Handoff rules injection ───────────────────────────────────────────────
    # Read the tenant's active handoff rules from the DB and append them to the
    # system prompt. This means toggling a rule in the portal takes effect on
    # the very next chat — no server restart or prompt editing required.
    try:
        from handoff import build_handoff_instruction
        _handoff_block = build_handoff_instruction(int(tenant_id))
        if _handoff_block:
            system_prompt = system_prompt + _handoff_block
    except Exception as _hb_err:
        print("⚠️ build_handoff_instruction error:", _hb_err)
    # ─────────────────────────────────────────────────────────────────────────

    print(f"   product_recommendation={'ON' if rec_enabled else 'OFF'}  related_products={'ON' if related_enabled else 'OFF'}")
    # ─────────────────────────────────────────────────────────────────────────

    # ── New-visitor / dormant-user business introduction ──────────────────────
    # Check BEFORE ensure_session updates last_seen.
    # If this is the user's first-ever message, or they haven't chatted in 48h,
    # instruct the AI to open with a brief business introduction so the user
    # knows what the store sells without having to guess.
    try:
        _sess_status = get_session_status(session_id)
        if _sess_status["is_new"] or _sess_status["dormant"]:
            _intro_reason = "new customer" if _sess_status["is_new"] else "returning customer (inactive 48+ hours)"
            system_prompt += (
                f"\n\nFIRST-CONTACT GREETING ({_intro_reason}): Before answering the customer's message, "
                "open with a short, warm welcome and introduce what this business sells or offers "
                "(2-3 sentences drawn from the context above — business name, main products/services, "
                "and an invitation to ask questions). "
                "Do NOT repeat this introduction in future replies — only on this first message."
            )
            print(f"   [INTRO] Injecting business intro for session={session_id} ({_intro_reason})")
    except Exception as _intro_err:
        print(f"⚠️ session status check error: {_intro_err}")
    # ─────────────────────────────────────────────────────────────────────────

    ensure_session(session_id, tenant_id)

    # Summarise older history after ~3 turns to reduce token usage.
    try:
        maybe_summarize_session(session_id, tenant_id, keep_last_n=int(os.getenv("HISTORY_KEEP_LAST_N", "4")))
    except Exception:
        pass

    # Generate embedding once — reused for semantic history + product search
    msg_embedding = None
    try:
        from search import _embed_query as _emb
        msg_embedding = _emb(req.message)
    except Exception as _emb_err:
        print(f"⚠️ embedding for memory failed (semantic history disabled this turn): {_emb_err}")

    if msg_embedding:
        history = get_semantic_history(
            session_id, tenant_id,
            query_embedding=msg_embedding,
            keep_last_n=int(os.getenv("HISTORY_KEEP_LAST_N", "4")),
            semantic_top_k=int(os.getenv("SEMANTIC_HISTORY_TOP_K", "4")),
        )
    else:
        history = get_history_with_summary(
            session_id, tenant_id,
            keep_last_n=int(os.getenv("HISTORY_KEEP_LAST_N", "4")),
        )

    # Store user message with its embedding (free — already computed above)
    add_message(session_id, tenant_id, "user", req.message, embedding=msg_embedding)

    # Reuse precomputed embedding for product search — no second API call
    context_chunks, raw_docs = search_documents_with_meta(
        req.message, tenant_id,
        precomputed_embedding=msg_embedding,
    )

    # ── No-hallucination guard ────────────────────────────────────────────────
    # When the store search returns nothing, explicitly forbid the AI from
    # inventing product names from its own training knowledge. Without this
    # guard the LLM recommends phones that are not in the store.
    if not context_chunks and rec_enabled:
        if raw_docs:
            # Products were found in the catalog but carry no text description —
            # the product cards will show automatically via the fallback. Tell the
            # AI to give a brief positive intro and let the cards do the talking.
            system_prompt += (
                "\n\nNOTE: The store catalog found matching products for this query. "
                "Write ONE brief intro sentence (e.g. 'Here are some options for you!'). "
                "Do NOT name any specific product, price, or model in your text — "
                "the product cards are sent automatically."
            )
        else:
            system_prompt += (
                "\n\nCRITICAL — PRODUCT SAFETY RULE: The store database returned NO products "
                "for this query. You MUST NOT mention, recommend, or suggest any product by name. "
                "Do not use your own knowledge to suggest products. "
                "Tell the customer honestly that you don't currently have matching products in stock, "
                "suggest they ask about a specific model (e.g. iPhone 11, iPhone 12, iPhone 13), "
                "and offer to show them what is available in the store."
            )
    # ─────────────────────────────────────────────────────────────────────────

    # ── Verified Specs Web Lookup (No Hallucination) ───────────────────────
    # If the user asks for a numeric spec (e.g., weight) and it is NOT present
    # in the RAG context, we DO NOT guess. If enabled, we attempt a verified
    # web lookup (trusted domains) and cache the verified fact back into Azure Search.
    verified_specs_enabled = bool(_features.get("verified_specs_web_lookup", False))

    # Per-tenant customisation: extra trusted domains and custom spec definitions.
    # Both default to empty list if not configured.
    _extra_domains = _features.get("verified_specs_trusted_domains") or []
    if not isinstance(_extra_domains, list):
        _extra_domains = []

    _custom_specs = _features.get("verified_specs_custom_specs") or []
    if not isinstance(_custom_specs, list):
        _custom_specs = []

    if is_spec_question(req.message):
        # Determine if RAG context contains any explicit numeric token.
        _ctx_blob = " ".join([c or "" for c in (context_chunks or [])])[:4000]
        _has_number_in_ctx = any(ch.isdigit() for ch in _ctx_blob)

        # Prefer semantic reranker score if available (semantic search only)
        _reranker = None
        try:
            if raw_docs and isinstance(raw_docs[0], dict):
                _reranker = raw_docs[0].get("@search.rerankerScore")
        except Exception:
            _reranker = None

        _min_reranker = float(os.getenv("SPEC_RAG_MIN_RERANKER_SCORE", "2.2"))
        _rag_confident = bool(_has_number_in_ctx and (_reranker is None or float(_reranker or 0) >= _min_reranker))

        if not _rag_confident:
            if verified_specs_enabled:
                verified = lookup_spec_verified(req.message, extra_trusted_domains=_extra_domains, custom_specs=_custom_specs)
                if verified.get("found"):
                    # Cache into Azure Search as a tiny verified-spec doc (mergeOrUpload)
                    try:
                        model_hint = req.message
                        upsert_verified_spec(
                            tenant_id=tenant_id,
                            model_hint=model_hint,
                            spec_key=verified.get("spec_key") or "spec",
                            spec_value=verified.get("spec_value") or "",
                            qualifier=verified.get("qualifier") or "",
                            sources=verified.get("sources") or [],
                        )
                    except Exception:
                        pass

                    # Deterministic, non-LLM answer to avoid hallucination
                    srcs = verified.get("sources") or []
                    src_lines = []
                    for s in srcs[:2]:
                        u = (s.get("url") or "").strip()
                        t = (s.get("title") or "").strip()
                        if u:
                            src_lines.append(f"- {t or u}: {u}")
                    cite = "\n".join(src_lines) if src_lines else ""
                    answer = f"{verified.get('spec_key','Spec').title()}: {verified.get('spec_value','')}\n{verified.get('qualifier','')}".strip()
                    if cite:
                        answer = answer + "\n\nSource(s):\n" + cite
                    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                    add_message(session_id, tenant_id, "assistant", answer)
                    return {"reply": answer, "session_id": session_id, "product_recommendations": []}

                # If web lookup fails, refuse safely (no guessing)
                answer = (
                    "I can’t verify that specification from the store data I have, and I don’t want to guess. "
                    "If you share the exact configuration/model code (or a product link), I can confirm it from an official source."
                )
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                add_message(session_id, tenant_id, "assistant", answer)
                return {"reply": answer, "session_id": session_id, "product_recommendations": []}
            else:
                # Feature not enabled: refuse rather than hallucinate
                answer = (
                    "I don’t have a verified answer for that specification in the store data, and I don’t want to guess. "
                    "Please share the exact model/config (or a product link) and I can confirm it."
                )
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                add_message(session_id, tenant_id, "assistant", answer)
                return {"reply": answer, "session_id": session_id, "product_recommendations": []}
    # ───────────────────────────────────────────────────────────────────────


    answer, needs_handoff, usage = ask_llm(
        system_prompt, req.message, context_chunks, history=history, structured_handoff=True
    )

    # ── Human handoff detection (best-effort, never crashes /chat) ───────────
    handoff_triggered = False
    try:
        from handoff import detect_and_process_handoff
        answer, handoff_triggered = detect_and_process_handoff(
            answer=answer,
            needs_handoff=bool(needs_handoff),
            user_message=req.message,
            tenant_id=int(tenant_id),
            session_id=session_id,
            store_domain=tenant.get("website") or "",
        )
    except Exception as _hf_err:
        print("⚠️ handoff detection error:", _hf_err)
    # ─────────────────────────────────────────────────────────────────────────

    add_message(session_id, tenant_id, "assistant", answer)

    # ── Parse and strip product recommendation tag ───────────────────────────
    product_recommendations = []
    doc_by_title = {}   # built inside the rec block; initialised here so Feature 5 can reference it safely
    if rec_enabled:
        _match = _re.search(_PRODUCT_TAG_PATTERN, answer, _re.DOTALL)
        if _match:
            try:
                recommended_names = _json.loads(_match.group(1))
                # Remove the hidden tag from the reply the customer sees
                answer = _re.sub(_PRODUCT_TAG_PATTERN, "", answer, flags=_re.DOTALL).strip()
                print(f"   extracted {len(recommended_names)} product recommendation(s): {recommended_names}")

                # Match recommended names against raw search results to build rich product cards.
                # We use case-insensitive substring matching — good enough since the AI was
                # given the exact titles from the same search results.
                def _extract_product_id(doc_id: str) -> str:
                    """Extract WooCommerce product ID from Azure doc key e.g. 'product-123' → '123'"""
                    try:
                        # Format is product-{wp_id} after make_safe_doc_key conversion
                        parts = (doc_id or "").split("-")
                        if len(parts) >= 2 and parts[0] == "product":
                            return parts[1]
                    except Exception:
                        pass
                    return ""

                def _format_price(price_min, price_max, currency: str = "") -> str:
                    from search import _fmt_currency_val
                    try:
                        mn = float(price_min) if price_min is not None else None
                        mx = float(price_max) if price_max is not None else None
                        if mn is not None and mn <= 0:
                            mn = None
                        if mx is not None and mx <= 0:
                            mx = None
                        ref = mn if mn is not None else mx
                        if ref is not None:
                            if mn is not None and mx is not None and abs(mx - mn) > 0.01:
                                return f"{_fmt_currency_val(mn, currency)} – {_fmt_currency_val(mx, currency)}"
                            elif mn is not None:
                                return _fmt_currency_val(mn, currency)
                            elif mx is not None:
                                return _fmt_currency_val(mx, currency)
                    except Exception:
                        pass
                    return ""

                # Build a lookup dict from raw_docs: normalised title → doc
                doc_by_title = {}
                for doc in raw_docs:
                    t = (doc.get("title") or "").strip().lower()
                    if t:
                        doc_by_title[t] = doc

                for name in recommended_names:
                    name_lower = (name or "").strip().lower()
                    if not name_lower:
                        continue

                    # Try exact match first, then punctuation-stripped substring match
                    # e.g. "Apple iPhone 11 [UK Used]" matches "Apple iPhone 11 - [UK Used]"
                    import re as _re_match2
                    def _strip_punct2(s):
                        s = _re_match2.sub(r'[^a-z0-9 ]', '', s).strip(); return _re_match2.sub(r' +', ' ', s)
                    matched_doc = doc_by_title.get(name_lower)
                    if matched_doc is None:
                        name_stripped2 = _strip_punct2(name_lower)
                        for title_key, doc in doc_by_title.items():
                            title_stripped2 = _strip_punct2(title_key)
                            if name_stripped2 in title_stripped2 or title_stripped2 in name_stripped2:
                                matched_doc = doc
                                break

                    if matched_doc:
                        product_id = _extract_product_id(matched_doc.get("id", ""))
                        url = matched_doc.get("url") or ""
                        # Build Add to Cart URL: WooCommerce accepts ?add-to-cart={id}
                        # For variable products the customer lands on product page to pick variant
                        cart_url = f"{url}?add-to-cart={product_id}" if product_id and url else url

                        product_recommendations.append({
                            "name": matched_doc.get("title") or name,
                            "price": _format_price(matched_doc.get("price_min"), matched_doc.get("price_max"), matched_doc.get("currency") or ""),
                            "url": url,
                            "cart_url": cart_url,
                            "in_stock": bool(matched_doc.get("in_stock", True)),
                            "sku": matched_doc.get("sku") or "",
                            "brand": matched_doc.get("brand") or "",
                            "product_id": product_id,
                            "id": matched_doc.get("id") or "",
                            "image_url": matched_doc.get("image_url") or "",
                            "description": (matched_doc.get("content") or "")[:600],
                        })
                    else:
                        # Name not in current search results — do a targeted search
                        # specifically for this product name to get its URL.
                        # This handles cases where the AI recommends products from
                        # conversation history that aren't in the current query's results.
                        try:
                            _, fallback_docs = search_documents_with_meta(
                                name, tenant_id
                            )
                            # Strip all punctuation before comparing so that
                            # "Apple iPhone 11 [UK Used]" matches
                            # "Apple iPhone 11 - [UK Used]" (dash difference)
                            import re as _re_match
                            def _strip_punct(s):
                                s = _re_match.sub(r'[^a-z0-9 ]', '', s).strip(); return _re_match.sub(r' +', ' ', s)
                            name_stripped = _strip_punct(name_lower)
                            for fb_doc in fallback_docs:
                                fb_title = (fb_doc.get("title") or "").strip().lower()
                                fb_stripped = _strip_punct(fb_title)
                                if name_stripped in fb_stripped or fb_stripped in name_stripped:
                                    matched_doc = fb_doc
                                    break
                        except Exception:
                            matched_doc = None

                        if matched_doc:
                            # Add fallback doc to doc_by_title so cross-selling can use it
                            fb_title_key = (matched_doc.get("title") or "").strip().lower()
                            if fb_title_key:
                                doc_by_title[fb_title_key] = matched_doc
                            product_id = _extract_product_id(matched_doc.get("id", ""))
                            url = matched_doc.get("url") or ""
                            cart_url = f"{url}?add-to-cart={product_id}" if product_id and url else url
                            product_recommendations.append({
                                "name": matched_doc.get("title") or name,
                                "price": _format_price(matched_doc.get("price_min"), matched_doc.get("price_max"), matched_doc.get("currency") or ""),
                                "url": url,
                                "cart_url": cart_url,
                                "in_stock": bool(matched_doc.get("in_stock", True)),
                                "sku": matched_doc.get("sku") or "",
                                "brand": matched_doc.get("brand") or "",
                                "product_id": product_id,
                                "id": matched_doc.get("id") or "",
                                "image_url": matched_doc.get("image_url") or "",
                                "description": (matched_doc.get("content") or "")[:600],
                            })
                        else:
                            # Still not found — show card with name only, no buttons
                            product_recommendations.append({
                                "name": name,
                                "price": "",
                                "url": "",
                                "cart_url": "",
                                "in_stock": True,
                                "sku": "",
                                "brand": "",
                                "image_url": "",
                            })

            except Exception as parse_err:
                print(f"   ⚠️ product tag parse failed: {parse_err}")

        # ── Feature 5: Automated Cross-selling (Related Product Cards) ───────
        # After the main recommendations are built, run a second Azure Search
        # query using the first recommended product's category + price range.
        # Up to 2 related products are appended with 'related': True so the
        # widget can label them separately as "You may also like:".
        if related_enabled and product_recommendations:
            try:
                first = product_recommendations[0]
                first_doc = doc_by_title.get((first["name"] or "").lower())
                if first_doc:
                    # DEBUG — log every field in first_doc so we can see exactly what
                    # the Azure Search index actually stores for a real product.
                    print(f"   [CROSS-SELL DEBUG] first_doc fields: { {k: v for k, v in first_doc.items() if k != 'content'} }")
                    cat       = first_doc.get("categories_text") or ""
                    prod_type = first_doc.get("type") or ""
                    already_recommended = {p["name"].lower() for p in product_recommendations}

                    # Derive budget cap from the most expensive already-recommended
                    # product (all of which passed the customer's price filter).
                    # This prevents related products from exceeding the customer's budget.
                    _prices = []
                    for _p in product_recommendations:
                        _raw = (_p.get("price") or "").replace("£", "").replace("₦", "").replace(",", "")
                        try:
                            _prices.append(float(_raw.split("–")[-1].strip()))
                        except (ValueError, IndexError):
                            pass
                    _max_price_gbp = max(_prices) if _prices else None

                    related = search_related_products(
                        tenant_id=tenant_id,
                        exclude_names=already_recommended,
                        prod_type=prod_type,
                        category=cat,
                        top=2,
                        search_hint=first["name"],
                        max_price_gbp=_max_price_gbp,
                    )
                    for r in related:
                        product_recommendations.append({**r, "related": True})

                    print(f"   related_products: appended {len(related)} related card(s)")
            except Exception as rel_err:
                print(f"   ⚠️ related_products failed: {rel_err}")
        # ─────────────────────────────────────────────────────────────────────

        # ── Fallback: AI skipped the tag — build list from raw search results ─
        # This fires when the search found real products but the AI forgot to
        # include <<<PHIXTRA_PRODUCTS:...>>> in its reply. Without this, the
        # customer sees a plain-text product list instead of the View Options list.
        # Guard: only fire if the AI reply actually sounds like a product recommendation.
        # Without this, greetings like "hello" trigger the fallback because the vector
        # search always returns something regardless of query relevance.
        # Only fire the fallback when the AI reply is unambiguously a product
        # recommendation — not on generic words like "available" or "option"
        # that appear in any helpful answer (e.g. "I'm available to help").
        _PRODUCT_SIGNALS = (
            "here are some", "here are a few", "here are the",
            "here is a", "here is an",
            "i found some", "i found a few", "i found these",
            "take a look at these", "take a look at this",
            "check out these", "check out this",
            "currently in stock", "these are available",
            "options for you", "choices for you",
            "products for you", "items for you",
            "we currently have", "we have the following",
            "following products", "following items", "following options",
        )
        _reply_suggests_products = any(p in answer.lower() for p in _PRODUCT_SIGNALS)
        if not product_recommendations and raw_docs and _reply_suggests_products:
            def _pid_fb(doc_id):
                p = (doc_id or "").split("-")
                return p[1] if len(p) >= 2 and p[0] == "product" else ""

            def _price_fb(pmin, pmax, currency: str = ""):
                from search import _fmt_currency_val
                try:
                    mn = float(pmin) if pmin is not None else None
                    mx = float(pmax) if pmax is not None else None
                    if mn is not None and mn <= 0: mn = None
                    if mx is not None and mx <= 0: mx = None
                    ref = mn if mn is not None else mx
                    if ref is not None:
                        if mn and mx and abs(mx - mn) > 0.01: return f"{_fmt_currency_val(mn, currency)} – {_fmt_currency_val(mx, currency)}"
                        elif mn: return _fmt_currency_val(mn, currency)
                        elif mx: return _fmt_currency_val(mx, currency)
                except Exception: pass
                return ""

            for _doc in raw_docs[:6]:
                _pid  = _pid_fb(_doc.get("id", ""))
                _url  = _doc.get("url") or ""
                _cart = f"{_url}?add-to-cart={_pid}" if _pid and _url else _url
                product_recommendations.append({
                    "name":        _doc.get("title") or "",
                    "price":       _price_fb(_doc.get("price_min"), _doc.get("price_max"), _doc.get("currency") or ""),
                    "url":         _url,
                    "cart_url":    _cart,
                    "in_stock":    bool(_doc.get("in_stock", True)),
                    "sku":         _doc.get("sku") or "",
                    "brand":       _doc.get("brand") or "",
                    "product_id":  _pid,
                    "id":          _doc.get("id") or "",
                    "image_url":   _doc.get("image_url") or "",
                    "description": (_doc.get("content") or "")[:600],
                })

            # Only strip the AI's text list if the fallback actually produces
            # valid product cards (url + product_id + price). When raw_docs are
            # all pages (no price/product_id), validation produces 0 items and
            # we must NOT strip — the AI's bullet points are the real answer.
            valid_fallback = _validate_products(product_recommendations)
            if valid_fallback:
                product_recommendations = valid_fallback
                print(f"   ⚡ tag-fallback: {len(product_recommendations)} product(s) from raw_docs")
                # Strip any numbered or bulleted product list the AI wrote so
                # the customer only sees the intro sentence + the View Options list
                answer = _re.sub(r'\n\d+\.\s+\*.*', '', answer, flags=_re.DOTALL).strip()
                answer = _re.sub(r'\n[-•]\s+\*.*', '', answer, flags=_re.DOTALL).strip()
            else:
                product_recommendations = []
        # ─────────────────────────────────────────────────────────────────────

    # Token accounting
    used_now = int((usage or {}).get("total_tokens", 0) or 0)
    record_token_usage(
        api_key_id=int(tenant["api_key_id"]),
        tenant_id=int(tenant_id),
        website=tenant.get("website"),
        key_type=tenant.get("key_type"),
        used_now=used_now,
        token_limit=tenant.get("token_limit"),
        session_id=session_id,
    )

    # Build response — only include product_recommendations if there are any
    response = {
        "reply": answer,
        "session_id": session_id,
        "tokens_used_this_request": used_now,
        "key_type": tenant.get("key_type"),
        "website": tenant.get("website"),
        "handoff_triggered": handoff_triggered,
    }
    product_recommendations = _validate_products(product_recommendations)
    if product_recommendations:
        response["product_recommendations"] = product_recommendations

    return response


# ══════════════════════════════════════════════════════════════════════════════
# INTELLIGENT CART REVENUE RECOVERY  —  NEW ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

import os as _os


# ══════════════════════════════════════════════════════════════════════════════
# NOTIFY WHEN IN STOCK — NEW ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

class NotifyStockRequest(BaseModel):
    api_key:      str
    product_id:   str          # WooCommerce product ID
    product_name: str          # Product display name (for email)
    product_url:  str          # Product page URL (for email)
    email:        str          # Shopper's email address


class StockBackInRequest(BaseModel):
    api_key:      str
    product_id:   str          # WooCommerce product ID now back in stock


@app.post("/notify-when-in-stock")
def notify_when_in_stock(req: NotifyStockRequest):
    """
    Saves a shopper's email so they are notified when a product comes back in stock.
    Called by the WordPress plugin REST proxy when the widget button is clicked.
    """
    tenant, error = verify_api_key(req.api_key)
    if error:
        raise HTTPException(status_code=401, detail=error)

    tenant_id = int(tenant["tenant_id"])

    # Basic email validation
    import re as _re_email
    if not req.email or not _re_email.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", req.email.strip()):
        raise HTTPException(status_code=400, detail="Invalid email address")

    if not req.product_id:
        raise HTTPException(status_code=400, detail="product_id is required")

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database unavailable")

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO stock_notifications
                (tenant_id, product_id, product_name, product_url, email, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (tenant_id, product_id, email) DO UPDATE SET
                product_name = EXCLUDED.product_name,
                product_url  = EXCLUDED.product_url,
                status       = CASE WHEN stock_notifications.status = 'notified' THEN 'pending' ELSE stock_notifications.status END,
                notified_at  = CASE WHEN stock_notifications.status = 'notified' THEN NULL ELSE stock_notifications.notified_at END
            """,
            (
                tenant_id,
                req.product_id.strip(),
                (req.product_name or "").strip()[:512],
                (req.product_url or "").strip()[:1024],
                req.email.strip().lower(),
            ),
        )
        conn.commit()
        print(
            f"✅ /notify-when-in-stock tenant_id={tenant_id} "
            f"product_id={req.product_id} email={req.email.strip().lower()}"
        )
    except Exception as e:
        print("⚠️ notify_when_in_stock DB error:", e)
        raise HTTPException(status_code=500, detail="Could not save notification request")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    insert_audit_log(
        action="notify_when_in_stock_subscribed",
        tenant_id=tenant_id,
        website=tenant.get("website"),
        details={"product_id": req.product_id, "product_name": req.product_name},
    )

    return {"status": "ok", "message": "You'll be notified when this product is back in stock."}


@app.post("/stock-back-in")
def stock_back_in(req: StockBackInRequest):
    """
    Called by the WordPress plugin when WooCommerce marks a product as back in stock.
    Sends notification emails to all pending subscribers and marks them as notified.
    """
    tenant, error = verify_api_key(req.api_key)
    if error:
        raise HTTPException(status_code=401, detail=error)

    tenant_id = int(tenant["tenant_id"])

    if not req.product_id:
        raise HTTPException(status_code=400, detail="product_id is required")

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database unavailable")

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    notified_count = 0
    failed_count   = 0

    try:
        cur.execute(
            """
            SELECT id, email, product_name, product_url
            FROM   stock_notifications
            WHERE  tenant_id  = %s
            AND    product_id = %s
            AND    status     = 'pending'
            """,
            (tenant_id, req.product_id.strip()),
        )
        rows = cur.fetchall()

        store_name = tenant.get("website") or "our store"
        store_url  = tenant.get("website") or ""

        for row in rows:
            email        = row["email"]
            product_name = row["product_name"] or "A product you were watching"
            product_url  = row["product_url"]  or store_url
            row_id       = int(row["id"])

            sent = _send_back_in_stock_email(
                to_email=email,
                product_name=product_name,
                product_url=product_url,
                store_name=store_name,
            )

            if sent:
                cur.execute(
                    "UPDATE stock_notifications SET status='notified', notified_at=NOW() WHERE id=%s",
                    (row_id,),
                )
                notified_count += 1
            else:
                cur.execute(
                    "UPDATE stock_notifications SET status='failed' WHERE id=%s",
                    (row_id,),
                )
                failed_count += 1

        conn.commit()
        print(
            f"✅ /stock-back-in tenant_id={tenant_id} product_id={req.product_id} "
            f"notified={notified_count} failed={failed_count}"
        )

    except Exception as e:
        print("⚠️ stock_back_in error:", e)
        raise HTTPException(status_code=500, detail="Error processing notifications")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    insert_audit_log(
        action="stock_back_in_notifications_sent",
        tenant_id=tenant_id,
        website=tenant.get("website"),
        details={
            "product_id":     req.product_id,
            "notified_count": notified_count,
            "failed_count":   failed_count,
        },
    )

    return {
        "status":         "ok",
        "product_id":     req.product_id,
        "notified_count": notified_count,
        "failed_count":   failed_count,
    }


def _send_back_in_stock_email(
    to_email: str,
    product_name: str,
    product_url: str,
    store_name: str,
) -> bool:
    """
    Sends a 'back in stock' notification email.
    Uses SendGrid if SENDGRID_API_KEY is set, otherwise falls back to smtplib.
    Returns True on success, False on failure.
    """
    subject   = f"✅ {product_name} is back in stock!"
    from_email = _os.getenv("NOTIFICATION_FROM_EMAIL", "noreply@phixtra.com")
    from_name  = _os.getenv("NOTIFICATION_FROM_NAME",  store_name)

    html_body = f"""
    <div style="font-family:ui-sans-serif,system-ui,Arial,sans-serif;max-width:600px;margin:0 auto;padding:32px 24px;background:#fff;">
      <h2 style="color:#1a1a2e;margin-bottom:8px;">Great news! 🎉</h2>
      <p style="color:#374151;font-size:15px;line-height:1.6;">
        <strong>{_html_escape(product_name)}</strong> is back in stock at {_html_escape(store_name)}.
      </p>
      <p style="color:#6b7280;font-size:14px;">
        You asked us to let you know — so here we are! Grab it before it sells out again.
      </p>
      <a href="{product_url}"
         style="display:inline-block;margin-top:16px;padding:13px 28px;background:#1a73e8;
                color:#fff;border-radius:8px;font-weight:700;font-size:15px;
                text-decoration:none;">
        Shop Now →
      </a>
      <p style="color:#9ca3af;font-size:12px;margin-top:32px;border-top:1px solid #f3f4f6;padding-top:16px;">
        You received this because you clicked "Notify When In Stock" in our chat assistant.
      </p>
    </div>
    """

    # ── Try SendGrid ─────────────────────────────────────────────────────────
    sendgrid_key = _os.getenv("SENDGRID_API_KEY", "")
    if sendgrid_key:
        try:
            import urllib.request, urllib.error, json as _j
            payload = {
                "personalizations": [{"to": [{"email": to_email}]}],
                "from":    {"email": from_email, "name": from_name},
                "subject": subject,
                "content": [{"type": "text/html", "value": html_body}],
            }
            req_obj = urllib.request.Request(
                "https://api.sendgrid.com/v3/mail/send",
                data=_j.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {sendgrid_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(req_obj, timeout=10)
            return True
        except Exception as e:
            print(f"⚠️ SendGrid email failed for {to_email}: {e}")

    # ── Fallback: smtplib ────────────────────────────────────────────────────
    smtp_host = _os.getenv("SMTP_HOST", "")
    if smtp_host:
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"{from_name} <{from_email}>"
            msg["To"]      = to_email
            msg.attach(MIMEText(html_body, "html"))

            smtp_port = int(_os.getenv("SMTP_PORT", "587"))
            smtp_user = _os.getenv("SMTP_USER", from_email)
            smtp_pass = _os.getenv("SMTP_PASSWORD", "")

            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.starttls()
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.sendmail(from_email, to_email, msg.as_string())
            return True
        except Exception as e:
            print(f"⚠️ SMTP email failed for {to_email}: {e}")

    # No sending infrastructure configured — log and return False
    print(f"⚠️ No email provider configured. Would have sent to {to_email}: {subject}")
    return False


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ══════════════════════════════════════════════════════════════════════════════
# END NOTIFY WHEN IN STOCK
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# HANDOFF CONTACT CAPTURE
# Receives the visitor's name / mobile / email from the in-widget contact form
# that appears automatically when a handoff is triggered.
# ══════════════════════════════════════════════════════════════════════════════

class HandoffContactRequest(BaseModel):
    api_key:       str
    session_id:    str
    visitor_name:  str | None = None
    visitor_phone: str | None = None
    visitor_email: str | None = None


@app.post("/handoff-contact")
def handoff_contact(req: HandoffContactRequest):
    """
    Called by the widget after the visitor fills in the contact-capture form.
    Saves their name, phone and email against the handoff_requests row for this
    session and sends a follow-up email to the store owner.
    """
    tenant, error = verify_api_key(req.api_key)
    if error:
        raise HTTPException(status_code=401, detail=error)

    tenant_id = int(tenant["tenant_id"])

    import re as _re_hc
    raw_email = (req.visitor_email or "").strip()
    if raw_email and not _re_hc.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", raw_email):
        raise HTTPException(status_code=400, detail="Invalid email address")

    try:
        from handoff import update_handoff_contact
        update_handoff_contact(
            tenant_id=tenant_id,
            session_id=req.session_id,
            visitor_name=(req.visitor_name  or "").strip(),
            visitor_phone=(req.visitor_phone or "").strip(),
            visitor_email=raw_email,
            store_domain=tenant.get("website") or "",
        )
    except Exception as _hc_err:
        print("⚠️ handoff_contact error:", _hc_err)

    return {"status": "ok"}

# ══════════════════════════════════════════════════════════════════════════════
# END HANDOFF CONTACT CAPTURE
# ══════════════════════════════════════════════════════════════════════════════


class CartEventRequest(BaseModel):
    api_key:        str
    session_id:     str
    event_type:     str                  # add_to_cart | checkout_abandoned | exit_intent | ...
    cart_value:     float | None = None  # total cart value in store currency
    cart_items:     list  | None = None  # [{name, sku, qty, price}, ...]
    customer_email: str   | None = None  # if known (logged-in user)
    page_url:       str   | None = None  # URL where event occurred


class CartRecoveryReplyRequest(BaseModel):
    api_key:    str
    session_id: str
    message:    str          # customer's typed message in the recovery popup


class CheckRecoveryRequest(BaseModel):
    api_key:    str
    session_id: str




# ── Endpoint 1: POST /cart-event ──────────────────────────────────────────────

@app.post("/cart-event")
def cart_event(req: CartEventRequest):
    """
    Receives cart lifecycle events from the WordPress plugin.
    Scores abandonment intent and starts a recovery sequence when the threshold
    is crossed for the first time.

    Auth: same verify_api_key() as /chat.
    Feature flag: 'cart_recovery' must be enabled in tenant features JSON.
    """
    tenant, error = verify_api_key(req.api_key)
    if error:
        raise HTTPException(status_code=401, detail=error)

    tenant_id = int(tenant["tenant_id"])

    # ── Feature flag check ────────────────────────────────────────────────────
    _raw_features = tenant.get("features")
    _features: dict = {}
    if isinstance(_raw_features, str):
        try:
            _features = _json.loads(_raw_features)
        except Exception:
            _features = {}
    elif isinstance(_raw_features, dict):
        _features = _raw_features

    if not bool(_features.get("cart_recovery", False)):
        # Feature not enabled for this tenant — silently accept but do nothing
        return {"status": "ok", "cart_recovery": "disabled"}

    print(
        f"✅ /cart-event tenant_id={tenant_id} session={req.session_id} "
        f"event={req.event_type} cart_value={req.cart_value}"
    )

    # ── Expire any stale entries (lightweight, idempotent) ────────────────────
    try:
        expire_stale_queue_entries()
    except Exception:
        pass

    # ── 1. Log the raw event ──────────────────────────────────────────────────
    log_cart_event(
        tenant_id=tenant_id,
        session_id=req.session_id,
        event_type=req.event_type,
        cart_value=req.cart_value,
        cart_items=req.cart_items,
        page_url=req.page_url,
        customer_email=req.customer_email,
    )

    # ── 2. Handle checkout_completed → mark recovered immediately ─────────────
    if req.event_type == "checkout_completed":
        existing = get_queue_row_by_session(tenant_id, req.session_id)
        if existing and existing.get("status") in ("pending", "in_progress"):
            mark_queue_status(int(existing["id"]), "recovered")
            log_recovery_action(
                queue_id=int(existing["id"]),
                action_type="recovered",
                channel="woocommerce",
                message_preview="Checkout completed — cart recovered",
            )
            print(f"   ✅ Cart recovered for session={req.session_id}")
        return {"status": "ok", "event": "checkout_completed", "cart_recovery": "recovered"}

    # ── 3. Fetch all events for this session and compute cumulative score ──────
    all_events = get_session_events(tenant_id, req.session_id)
    score, priority = compute_intent_score(all_events, cart_value=req.cart_value)

    print(f"   Intent score={score} priority={priority}")

    # ── 4. Upsert the abandonment queue ──────────────────────────────────────
    queue_id = upsert_abandonment_queue(
        tenant_id=tenant_id,
        session_id=req.session_id,
        intent_score=score,
        priority=priority,
        cart_value=req.cart_value,
        cart_items=req.cart_items,
        customer_email=req.customer_email,
    )

    # ── 5. Trigger recovery if threshold crossed and not already started ───────
    triggered = False
    if queue_id and score >= RECOVERY_THRESHOLD:
        existing = get_queue_row_by_session(tenant_id, req.session_id)
        # Only start a new sequence if the entry is still in the initial 'pending' state
        # (upsert_abandonment_queue keeps it pending until we move it forward)
        if existing and existing.get("status") == "pending":
            # Read recovery settings from env (store admin configures these server-side)
            # In future they can come from the tenant features JSON
            store_name    = _os.getenv("STORE_NAME",    tenant.get("website") or "our store")
            store_url     = _os.getenv("STORE_URL",     tenant.get("website") or "")
            incentive_pct = int(_features.get("cart_recovery_incentive_pct", 0))

            # Read custom email templates saved by the store owner via the portal.
            # Empty strings tell start_recovery_sequence to use AI generation instead.
            custom_t2_subject = str(_features.get("cart_recovery_t2_subject", "") or "")
            custom_t2_html    = str(_features.get("cart_recovery_t2_html",    "") or "")
            custom_t3_subject = str(_features.get("cart_recovery_t3_subject", "") or "")
            custom_t3_html    = str(_features.get("cart_recovery_t3_html",    "") or "")

            start_recovery_sequence(
                queue_id=queue_id,
                to_email=req.customer_email,
                store_name=store_name,
                store_url=store_url,
                cart_items=req.cart_items,
                cart_value=req.cart_value,
                incentive_pct=incentive_pct,
                custom_t2_subject=custom_t2_subject,
                custom_t2_html=custom_t2_html,
                custom_t3_subject=custom_t3_subject,
                custom_t3_html=custom_t3_html,
                # WhatsApp recovery: fires alongside email if session is WA-based
                wa_api_key=req.api_key,
                wa_session_id=req.session_id or "",
            )
            triggered = True

    return {
        "status":        "ok",
        "score":         score,
        "priority":      priority,
        "queue_id":      queue_id,
        "recovery_triggered": triggered,
    }


# ── Endpoint 2: GET /check-recovery ──────────────────────────────────────────

@app.post("/check-recovery")
def check_recovery(req: CheckRecoveryRequest):
    """
    Called by the WordPress plugin JS on each page load to determine whether
    a cart recovery popup should be shown for this visitor's session.

    Returns show_popup=True only when:
      - The feature is enabled for this tenant
      - There is an 'in_progress' queue entry for this session
      - The entry has not expired

    The widget JS shows the popup and then calls /cart-event with
    event_type='recovery_popup_shown' to acknowledge it.
    """
    tenant, error = verify_api_key(req.api_key)
    if error:
        raise HTTPException(status_code=401, detail=error)

    tenant_id = int(tenant["tenant_id"])

    # Feature flag
    _raw_features = tenant.get("features")
    _features: dict = {}
    if isinstance(_raw_features, str):
        try:
            _features = _json.loads(_raw_features)
        except Exception:
            _features = {}
    elif isinstance(_raw_features, dict):
        _features = _raw_features

    if not bool(_features.get("cart_recovery", False)):
        return {"show_popup": False}

    # Check queue
    row = get_queue_row_by_session(tenant_id, req.session_id)
    if not row or row.get("status") != "in_progress":
        return {"show_popup": False}

    # Build recovery message (can be customised via tenant features JSON)
    recovery_message = str(
        _features.get(
            "cart_recovery_popup_message",
            "👋 Still thinking it over? Your cart is saved and ready for you!"
        )
    )
    incentive_pct = int(_features.get("cart_recovery_incentive_pct", 0))
    incentive_msg = ""
    if incentive_pct > 0:
        incentive_msg = (
            f" Use code COMEBACK{incentive_pct} for {incentive_pct}% off!"
        )

    return {
        "show_popup":       True,
        "recovery_message": recovery_message + incentive_msg,
        "incentive_pct":    incentive_pct,
        "queue_id":         int(row["id"]),
    }


# ── Endpoint 3: POST /cart-recovery-reply ────────────────────────────────────

@app.post("/cart-recovery-reply")
def cart_recovery_reply(req: CartRecoveryReplyRequest):
    """
    Handles the customer's typed reply inside the recovery popup chat.
    Uses the same GPT-4o mini / context pattern as /chat but with a
    recovery-focused system prompt. Also marks the cart as recovered
    if the customer signals they are completing checkout.
    """
    tenant, error = verify_api_key(req.api_key)
    if error:
        raise HTTPException(status_code=401, detail=error)

    tenant_id   = int(tenant["tenant_id"])
    session_id  = req.session_id

    # Feature flag
    _raw_features = tenant.get("features")
    _features: dict = {}
    if isinstance(_raw_features, str):
        try:
            _features = _json.loads(_raw_features)
        except Exception:
            _features = {}
    elif isinstance(_raw_features, dict):
        _features = _raw_features

    if not bool(_features.get("cart_recovery", False)):
        raise HTTPException(status_code=403, detail="Cart recovery not enabled for this tenant")

    print(f"✅ /cart-recovery-reply tenant_id={tenant_id} session={session_id}")

    # Fetch queue row for context
    queue_row = get_queue_row_by_session(tenant_id, session_id)

    # Build a recovery-focused system prompt
    store_name = _os.getenv("STORE_NAME", tenant.get("website") or "our store")
    store_url  = _os.getenv("STORE_URL",  tenant.get("website") or "")

    cart_items_desc = ""
    if queue_row and queue_row.get("cart_items"):
        try:
            import json as __json
            items = __json.loads(queue_row["cart_items"]) if isinstance(queue_row["cart_items"], str) else queue_row["cart_items"]
            names = [i.get("name") or i.get("title") or "" for i in (items or []) if i.get("name") or i.get("title")]
            cart_items_desc = ", ".join(names[:5])
        except Exception:
            cart_items_desc = ""

    recovery_system_prompt = (
        f"You are a helpful cart recovery assistant for {store_name}. "
        f"A customer left their cart without completing their purchase. "
        f"Their cart contains: {cart_items_desc or 'some items'}. "
        f"Your goal is to warmly help them complete their purchase. "
        f"Answer questions about products, shipping, returns, and anything "
        f"that helps them feel confident buying. "
        f"Keep replies concise and friendly. "
        f"The cart URL is: {store_url.rstrip('/')}/cart"
    )

    context_chunks, _ = search_documents_with_meta(req.message, tenant_id)

    answer, _needs_handoff, usage = ask_llm(
        system_prompt=recovery_system_prompt,
        user_message=req.message,
        context_chunks=context_chunks,
        history=[],
    )

    # Token accounting (best effort — same pattern as /chat)
    used_now = int((usage or {}).get("total_tokens", 0) or 0)
    try:
        record_token_usage(
            api_key_id=int(tenant["api_key_id"]),
            tenant_id=tenant_id,
            website=tenant.get("website"),
            key_type=tenant.get("key_type"),
            used_now=used_now,
            token_limit=tenant.get("token_limit"),
            session_id=session_id,
        )
    except Exception:
        pass

    # Check if the customer's message signals they are completing checkout
    # If so, mark the queue entry as recovered
    if queue_row and queue_row.get("status") in ("pending", "in_progress"):
        completion_signals = ("checkout", "buy", "purchase", "order", "complete", "going to buy")
        if any(sig in req.message.lower() for sig in completion_signals):
            mark_queue_status(int(queue_row["id"]), "recovered")
            log_recovery_action(
                queue_id=int(queue_row["id"]),
                action_type="recovered_via_chat",
                channel="widget",
                message_preview=req.message[:100],
            )
            print(f"   ✅ Cart recovery chat signal — marked recovered session={session_id}")

    return {
        "reply":      answer,
        "session_id": session_id,
    }



