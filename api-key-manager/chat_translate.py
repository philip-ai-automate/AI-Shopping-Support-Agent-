"""Chat Translation for the AI Sales Agent for B2B (2026-10-29).

The team reads every Inbox chat in its own language (English by default)
and writes in it; PhiXtra translates what goes to the buyer.

  - Messages are translated once and saved (message_translations), so a
    chat opens instantly the next time. Messages already in the team's
    language are spotted and cost nothing.
  - What the team wrote before it was translated is kept
    (sent_translations), so the chat shows "Your team wrote: …".

Only for businesses whose plan has the "inbox.translate" tick (the B2B
plan) and PhiXtra's own account — see allowed(). Settings live in
chat_translation_settings (one row per business).
"""
import hashlib
import json
import os
import re

from db import get_db_connection

FEATURE_KEY = "inbox.translate"
MAX_TEXTS = 40
MAX_CHARS = 2000

LANG_NAMES = {
    "en": "English", "fr": "French", "ar": "Arabic", "pt": "Portuguese", "es": "Spanish",
    "de": "German", "it": "Italian", "nl": "Dutch", "pl": "Polish", "ro": "Romanian", "tr": "Turkish",
    "ru": "Russian", "uk": "Ukrainian", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
    "hi": "Hindi", "ur": "Urdu", "bn": "Bengali", "fa": "Persian", "he": "Hebrew", "el": "Greek",
    "sw": "Swahili", "ha": "Hausa", "yo": "Yoruba", "ig": "Igbo", "am": "Amharic", "so": "Somali",
    "id": "Indonesian", "ms": "Malay", "vi": "Vietnamese", "th": "Thai", "tl": "Filipino",
}
# Languages offered in Settings and in the reply box, most used first.
PICK_LANGS = ["en", "fr", "ar", "pt", "es", "de", "it", "nl", "pl", "tr", "ru", "zh", "hi", "sw", "ha"]

_EN_WORDS = {"the", "and", "you", "your", "is", "are", "to", "for", "of", "in", "it", "we", "i", "can",
             "please", "thanks", "thank", "hello", "hi", "what", "how", "do", "have", "with", "on", "this",
             "that", "my", "me", "will", "be", "a", "an", "price", "order", "send", "need", "want", "yes", "no"}


def lang_name(code):
    return LANG_NAMES.get((code or "").lower(), (code or "").upper() or "Unknown")


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def _hash(text):
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def _looks_english(text):
    """A cheap check so plain English messages never reach the AI."""
    t = _norm(text).lower()
    if not t or re.search(r"[^\x00-\x7f’‘“”–—£€₦…]", t):
        return False
    words = re.findall(r"[a-z']+", t)
    if not words:
        return True          # numbers, emoji-free symbols: nothing to translate
    hits = sum(1 for w in words if w in _EN_WORDS)
    return hits >= 2 or (len(words) <= 3 and hits >= 1)


# ── Who has it, and their settings ──────────────────────────────────────────

def allowed(tenant_id) -> bool:
    try:
        import accounts_core
        if accounts_core.is_platform_tenant(tenant_id):
            return True
    except Exception:
        pass
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""SELECT 1 FROM tenants t JOIN plan_feature_grants g ON g.plan_id = t.plan_id
                        WHERE t.id = %s AND g.feature_key = %s LIMIT 1""", (int(tenant_id), FEATURE_KEY))
        return cur.fetchone() is not None
    finally:
        cur.close(); conn.close()


def settings(tenant_id) -> dict:
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT enabled, team_lang, reply_mode FROM chat_translation_settings WHERE tenant_id=%s",
                    (int(tenant_id),))
        r = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not r:
        return {"enabled": True, "team_lang": "en", "reply_mode": "translate"}
    return {"enabled": bool(r[0]), "team_lang": r[1] or "en", "reply_mode": r[2] or "translate"}


def save_settings(tenant_id, enabled, team_lang, reply_mode, by_label=""):
    team_lang = team_lang if team_lang in LANG_NAMES else "en"
    reply_mode = reply_mode if reply_mode in ("translate", "as_written") else "translate"
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO chat_translation_settings (tenant_id, enabled, team_lang, reply_mode, updated_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET enabled = EXCLUDED.enabled, team_lang = EXCLUDED.team_lang,
                   reply_mode = EXCLUDED.reply_mode, updated_by = EXCLUDED.updated_by, updated_at = NOW()""",
                    (int(tenant_id), bool(enabled), team_lang, reply_mode, (by_label or "")[:120]))
        conn.commit()
    finally:
        cur.close(); conn.close()


def active_for(tenant_id):
    """Settings when the business has Chat Translation and it is on, else None."""
    if not tenant_id or not allowed(tenant_id):
        return None
    s = settings(tenant_id)
    return s if s["enabled"] else None


# ── The AI call ─────────────────────────────────────────────────────────────

def _ask(prompt):
    import ai_designer as D
    client = D._client("chat_translate", "platform")
    resp = client.chat.completions.create(
        model=os.getenv("CHAT_TRANSLATE_MODEL", D.TEXT_MODEL),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"})
    return json.loads(resp.choices[0].message.content or "{}")


def _ai_translate_many(texts, target):
    """[(lang, translation or None)] for each text. None = already in target."""
    items = [{"i": i, "text": t} for i, t in enumerate(texts)]
    prompt = (
        "You translate business chat messages between a company's sales team and its buyers.\n"
        f"Target language: {lang_name(target)} ({target}).\n"
        "For each message: detect its language as an ISO 639-1 code. If it is already in the target "
        "language, set translation to null. Otherwise translate it naturally and faithfully into the "
        "target language. Keep numbers, prices, currency signs, product codes, names, links and emoji "
        "exactly as written. Do not add or leave out anything.\n"
        'Reply as JSON: {"results": [{"i": 0, "lang": "fr", "translation": "..."}]}\n'
        "Messages:\n" + json.dumps(items, ensure_ascii=False))
    out = _ask(prompt).get("results") or []
    by_i = {int(r.get("i", -1)): r for r in out if isinstance(r, dict)}
    res = []
    for i in range(len(texts)):
        r = by_i.get(i) or {}
        lang = str(r.get("lang") or "").lower()[:10] or None
        tr = r.get("translation")
        res.append((lang, (str(tr).strip() or None) if tr else None))
    return res


# ── Reading: translate messages into the team's language ────────────────────

def translate_for_team(tenant_id, texts, team_lang):
    """[{lang, lang_name, text, wrote}] in the same order as texts.
    text = the team-language version (None when already in it);
    wrote = what the team typed, for messages the team sent translated."""
    texts = [(t or "")[:MAX_CHARS] for t in (texts or [])][:MAX_TEXTS]
    hashes = [_hash(t) for t in texts]
    out = [None] * len(texts)
    conn = get_db_connection(); cur = conn.cursor()
    try:
        wrote = {}
        if hashes:
            cur.execute("""SELECT sent_hash, original, original_lang, sent_lang FROM sent_translations
                            WHERE tenant_id=%s AND sent_hash = ANY(%s)""", (int(tenant_id), hashes))
            wrote = {r[0]: r for r in cur.fetchall()}
            cur.execute("""SELECT src_hash, src_lang, translated FROM message_translations
                            WHERE tenant_id=%s AND target_lang=%s AND src_hash = ANY(%s)""",
                        (int(tenant_id), team_lang, hashes))
            cached = {r[0]: r for r in cur.fetchall()}
        else:
            cached = {}
        todo = []
        for i, (t, h) in enumerate(zip(texts, hashes)):
            if h in wrote:
                w = wrote[h]
                out[i] = {"lang": w[3], "lang_name": lang_name(w[3]), "text": None, "wrote": w[1]}
            elif h in cached:
                c = cached[h]
                out[i] = {"lang": c[1], "lang_name": lang_name(c[1]), "text": c[2], "wrote": None}
            elif not _norm(t) or (team_lang == "en" and _looks_english(t)):
                out[i] = {"lang": team_lang, "lang_name": lang_name(team_lang), "text": None, "wrote": None}
            else:
                todo.append(i)
        if todo:
            got = _ai_translate_many([texts[i] for i in todo], team_lang)
            for i, (lang, tr) in zip(todo, got):
                lang = lang or "und"
                text = tr if (tr and lang != team_lang and _norm(tr) != _norm(texts[i])) else None
                out[i] = {"lang": lang, "lang_name": lang_name(lang), "text": text, "wrote": None}
                cur.execute("""INSERT INTO message_translations (tenant_id, src_hash, target_lang, src_lang, translated)
                               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tenant_id, src_hash, target_lang) DO NOTHING""",
                            (int(tenant_id), hashes[i], team_lang, lang, text))
            conn.commit()
    finally:
        cur.close(); conn.close()
    return out


# ── Replying: the team's words in the buyer's language ──────────────────────

def translate_out(text, to_lang):
    text = (text or "")[:MAX_CHARS]
    if not _norm(text):
        return ""
    prompt = (
        "Translate this message from a company's sales team to its buyer into "
        f"{lang_name(to_lang)} ({to_lang}). Keep the tone friendly and professional. Keep numbers, prices, "
        "currency signs, product codes, names, links and emoji exactly as written. Do not add anything.\n"
        'Reply as JSON: {"translation": "..."}\n\nMessage:\n' + text)
    return str(_ask(prompt).get("translation") or "").strip()


def remember_sent(tenant_id, sent_text, original, original_lang, sent_lang):
    if not _norm(sent_text) or not _norm(original):
        return
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO sent_translations (tenant_id, sent_hash, original, original_lang, sent_lang)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (tenant_id, sent_hash) DO UPDATE SET original = EXCLUDED.original""",
                    (int(tenant_id), _hash(sent_text), original[:MAX_CHARS], (original_lang or "")[:10],
                     (sent_lang or "")[:10]))
        conn.commit()
    finally:
        cur.close(); conn.close()
