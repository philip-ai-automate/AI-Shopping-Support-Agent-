"""Discounts in Chat (11 Nov 2026, Phase 1) — the chat side.

The rules, the limits and the Shopify code live in the portal
(api-key-manager/chat_discounts.py); this file only:
  brief()  → the DISCOUNTS line for this chat's AI instruction
  apply()  → the AI wrote <<<PHIXTRA_DISCOUNT>>>: make the real code and put it
             in the reply, or (no code possible) send a polite fixed reply.
"""

import os
import re

import requests

from db import get_db_connection

TAG = "<<<PHIXTRA_DISCOUNT>>>"
_TAG_RE = re.compile(r"<<<PHIXTRA_DISCOUNT[^>]*>>>")


def _portal(path: str, body: dict, timeout: int = 8) -> dict:
    r = requests.post(os.getenv("PORTAL_INTERNAL_URL", "http://127.0.0.1:5055") + "/shopify/internal/discount/" + path,
                      json=body, headers={"Authorization": "Bearer " + os.getenv("PHIXTRA_INTERNAL_TOKEN", "")},
                      timeout=timeout)
    return r.json() if r.ok else {}


def _switched_on(tenant_id: int) -> bool:
    """Cheap check first, so businesses without it never wait on the portal."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT enabled FROM chat_discount_settings WHERE tenant_id=%s", (int(tenant_id),))
        r = cur.fetchone()
        cur.close()
        return bool(r and r[0])
    except Exception:
        return False
    finally:
        conn.close()


def _email(visitor) -> str:
    if isinstance(visitor, dict):
        return str(visitor.get("email") or "")[:200]
    return ""


def brief(tenant_id: int, session_id: str, visitor=None) -> str:
    if not _switched_on(tenant_id):
        return ""
    try:
        return _portal("brief", {"tenant_id": int(tenant_id), "session_id": session_id,
                                 "email": _email(visitor)}).get("line") or ""
    except Exception as e:
        print("⚠️ chat_discount.brief:", e)
        return ""


_LABELS = {
    "en": {"off": "off", "once": "works once", "ends": "ends", "min": "on orders of {m} or more", "copy": "Copy code", "copied": "Copied"},
    "fr": {"off": "de remise", "once": "valable une fois", "ends": "fin", "min": "dès {m} d'achat", "copy": "Copier le code", "copied": "Copié"},
    "es": {"off": "de descuento", "once": "válido una vez", "ends": "termina", "min": "en pedidos desde {m}", "copy": "Copiar código", "copied": "Copiado"},
    "pt": {"off": "de desconto", "once": "válido uma vez", "ends": "termina", "min": "em pedidos a partir de {m}", "copy": "Copiar código", "copied": "Copiado"},
    "ar": {"off": "خصم", "once": "صالح مرة واحدة", "ends": "ينتهي", "min": "للطلبات من {m}", "copy": "نسخ الرمز", "copied": "تم النسخ"},
}


def apply(tenant_id: int, session_id: str, visitor, answer: str, user_message: str, allowed: bool):
    """(new answer, code box data or None)."""
    import reply_language
    lang = reply_language.detect(user_message)
    if not allowed:
        # The AI wasn't told it could give a code: never show a tag.
        return _TAG_RE.sub("", answer).strip(), None
    try:
        res = _portal("make", {"tenant_id": int(tenant_id), "session_id": session_id,
                               "email": _email(visitor)}, timeout=20)
    except Exception as e:
        print("⚠️ chat_discount.make:", e)
        res = {}
    if not res.get("ok"):
        print(f"   discount not made tenant={tenant_id} session={session_id}: {res.get('why') or 'no answer'}")
        return reply_language.text("discount_unavailable", user_message), None
    code = res["code"]
    # The code on its own line where the AI put the tag; a second tag is dropped.
    first = [True]

    def _sub(_m):
        if first[0]:
            first[0] = False
            return "\n" + code + "\n"
        return ""
    answer = re.sub(r"[ \t]*\n?" + _TAG_RE.pattern + r"[ \t]*\n?", _sub, answer).strip()
    lab = _LABELS.get(lang) or _LABELS["en"]
    bits = [f"{res['value_text']} {lab['off']}" if lang != "ar" else f"{lab['off']} {res['value_text']}", lab["once"]]
    if res.get("min_order_text"):
        bits.append(lab["min"].format(m=res["min_order_text"]))
    bits.append(f"{lab['ends']} {res['ends_text']}")
    return answer, {"code": code, "summary": " · ".join(bits), "ends_at": res.get("ends_at"),
                    "copy": lab["copy"], "copied": lab["copied"]}
