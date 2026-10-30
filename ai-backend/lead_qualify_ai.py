"""
lead_qualify_ai.py — AI Lead Qualification, Phase 1 (30 Oct 2026, approved
plan https://claude.ai/artifact/1yo1cNY9ac33GpMRnXvnCp).

On WhatsApp and website chat the AI Sales Agent finds out, naturally while
it helps, the buying details the business asks for (CRM › Pipeline
Settings › AI Lead Qualification) that are still missing on the buyer's
lead. The portal (lead_qualify.py) reads the chat afterwards and saves the
answers on the lead.

Only for businesses whose plan has the "crm.ai_qualification" tick and
PhiXtra's own account, with AI Lead Qualification switched on.
"""
import os
import re
import time

from db import get_db_connection

FEATURE_KEY = "crm.ai_qualification"
ALL_FIELDS = ["company", "business_type", "requirement", "quantity", "delivery_country", "timeline", "authority"]
QUESTIONS = {
    "company": "the name of their company",
    "business_type": "whether they are a wholesaler, distributor, retailer, reseller or the end user",
    "requirement": "exactly which products or services they need (model, specification, size)",
    "quantity": "how many / how much they need",
    "delivery_country": "where the order must be delivered (country and city)",
    "timeline": "when they intend to buy or need delivery",
    "authority": "whether they make the buying decision or buy for someone else (purchasing)",
}
# What on the lead answers each question.
LEAD_COLS = {"company": "customer_name", "business_type": "business_type", "requirement": "product_interest",
             "quantity": "quantity", "delivery_country": "delivery_country", "timeline": "purchase_timeline",
             "authority": "decision_maker_engaged"}

_cache = {}   # tenant_id -> (expires, settings or None)


def _settings(tenant_id: int):
    hit = _cache.get(tenant_id)
    if hit and hit[0] > time.time():
        return hit[1]
    s = None
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ok = str(tenant_id) == (os.getenv("PLATFORM_TENANT_ID") or "").strip()
        if not ok:
            cur.execute("""SELECT 1 FROM tenants t JOIN plan_feature_grants g ON g.plan_id = t.plan_id
                            WHERE t.id=%s AND g.feature_key=%s LIMIT 1""", (tenant_id, FEATURE_KEY))
            ok = cur.fetchone() is not None
        if ok:
            s = {"enabled": True, "ask": list(ALL_FIELDS), "questions": []}
            try:
                cur.execute("SELECT settings FROM lead_qual_settings WHERE tenant_id=%s", (tenant_id,))
                r = cur.fetchone()
                raw = (r[0] if r else None) or {}
                if "enabled" in raw:
                    s["enabled"] = bool(raw["enabled"])
                if isinstance(raw.get("ask"), list):
                    s["ask"] = [f for f in ALL_FIELDS if f in raw["ask"]]
                # The business's own qualifying questions (2026-10-30).
                s["questions"] = [q for q in (raw.get("questions") or [])
                                  if isinstance(q, dict) and q.get("id") and q.get("ask") and q.get("on", True)][:8]
            except Exception:
                conn.rollback()
            if not s["enabled"]:
                s = None
        cur.close()
    finally:
        conn.close()
    _cache[tenant_id] = (time.time() + 120, s)
    return s


def _known(tenant_id: int, session_id: str) -> dict:
    """The details already on this chat's lead (none yet = {})."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute("""SELECT l.customer_name, l.business_type, l.product_interest, l.quantity,
                                  l.delivery_country, l.purchase_timeline, l.decision_maker_engaged, l.qual_answers
                             FROM lead_qual_sources q JOIN merchant_pipeline_leads l ON l.id = q.lead_id
                            WHERE q.tenant_id=%s AND q.source_key=%s""", (tenant_id, "chat:" + session_id))
            r = cur.fetchone()
            if not r:
                # Not read yet: a WhatsApp buyer already on the list, found by number.
                m = re.search(r"(\d{7,15})$", session_id) if session_id.startswith("wa-") else None
                if m:
                    cur.execute("""SELECT customer_name, business_type, product_interest, quantity,
                                          delivery_country, purchase_timeline, decision_maker_engaged, qual_answers
                                     FROM merchant_pipeline_leads
                                    WHERE tenant_id=%s AND dropped_at IS NULL
                                      AND (right(regexp_replace(COALESCE(whatsapp_number,''),'\\D','','g'),9)=%s
                                           OR right(regexp_replace(COALESCE(phone,''),'\\D','','g'),9)=%s)
                                    ORDER BY updated_at DESC NULLS LAST LIMIT 1""",
                                (tenant_id, m.group(1)[-9:], m.group(1)[-9:]))
                    r = cur.fetchone()
        except Exception:
            conn.rollback()
            r = None
        cur.close()
    finally:
        conn.close()
    if not r:
        return {}
    cols = ["customer_name", "business_type", "product_interest", "quantity", "delivery_country",
            "purchase_timeline", "decision_maker_engaged", "qual_answers"]
    return dict(zip(cols, r))


def addon(tenant_id: int, session_id: str) -> str:
    """Extra instruction for this chat, or ''. Never raises."""
    try:
        s = _settings(int(tenant_id))
        if not s or not (s["ask"] or s.get("questions")):
            return ""
        known = _known(int(tenant_id), session_id or "") if session_id else {}
        missing = []
        for f in s["ask"]:
            v = known.get(LEAD_COLS[f])
            if f == "company" and v and (str(v).startswith("+") or "@" in str(v)):
                v = None        # the lead is only named after their number / email
            if not v:
                missing.append(QUESTIONS[f])
        answered = known.get("qual_answers") or {}
        for q in s.get("questions") or []:
            if not (answered.get(q["id"]) or {}).get("answer"):
                missing.append(str(q["ask"])[:200])
        if not missing:
            return ""
        return (
            "\n\nQUALIFYING THE BUYER: when the customer shows interest in buying (asks about products, prices, "
            "stock, quantities or delivery), also find out — naturally, while you help them — the buying details "
            "below that they have not told you yet in this conversation. Rules: first answer what they asked; then "
            "ask at most ONE short, friendly question per reply; never ask for something they already said; never "
            "ask these questions when they are only asking a general question, are not a buyer, or are upset; "
            "never make them feel interrogated, and don't ask at all once they have given everything; ask in the "
            "customer's language. A buyer who sends a full request (product, quantity, where and when) is not asked "
            "anything more except what is truly missing.\n"
            "Still to find out: " + "; ".join(missing) + "."
        )
    except Exception as e:
        print("⚠️ lead_qualify_ai.addon:", e)
        return ""
