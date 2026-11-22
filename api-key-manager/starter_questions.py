"""Starter questions (22 Nov 2026, approved mock-up
https://claude.ai/artifact/N6a6wxTXBo5SkQQTdE6D1p).

Questions customers can tap instead of typing:
  * website chat box: the chat box's quick questions (chatbox_settings.quick_qs,
    the same list as Settings › Chat Box). A business without Chat Box settings
    keeps them in its About your business answers (starter_qs), so saving them
    never changes how the chat box looks.
  * WhatsApp: the same questions are set on each active WhatsApp number as
    Meta "ice breakers" (shown the first time a customer opens a chat).
  * WhatsApp buttons: up to 3 short buttons under the AI Agent's first reply
    to a new customer (whatsapp-gateway reads wa_btn_on / wa_btns).
"""
import json
import re

import requests

Q_MAX, Q_LEN = 4, 40        # the chat box's limit (Meta allows 80)
B_MAX, B_LEN = 3, 20        # Meta: 3 reply buttons, 20 characters each
GRAPH = "https://graph.facebook.com/v22.0"
_LINK = re.compile(r"(https?://|www\.)", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")

SUGGEST = {
    "products": (["How much is delivery?", "Can I return something I don't like?", "Do you have this in stock?",
                  "How can I pay?"], ["Delivery", "Returns", "Talk to the team"]),
    "services": (["How do I book an appointment?", "How much does it cost?", "What are your opening hours?",
                  "Where are you?"], ["Book", "Prices", "Talk to the team"]),
    "both":     (["How much does it cost?", "How much is delivery?", "How do I book?",
                  "What are your opening hours?"], ["Prices", "Book", "Talk to the team"]),
    "b2b":      (["What is your minimum order?", "Can I get a price list?", "How long does delivery take?",
                  "Can I get a quote?"], ["Prices", "Get a quote", "Talk to the team"]),
}
# A suggested question about something the business didn't tick is left out.
_NEEDS = {"How much is delivery?": "delivery", "Can I return something I don't like?": "delivery",
          "How do I book an appointment?": "bookings", "How do I book?": "bookings",
          "How long does delivery take?": "terms", "What is your minimum order?": "terms"}
_BTN_NEEDS = {"Delivery": "delivery", "Returns": "delivery", "Book": "bookings"}


def clean_q(text: str, n: int) -> str:
    return _EMOJI.sub("", " ".join((text or "").split()))[:n].strip()


def suggest(model: str, sells: str, do: list) -> tuple[list, list]:
    group = "b2b" if model == "b2b" else (sells if sells in ("products", "services", "both") else "products")
    qs, btns = SUGGEST[group]
    ticked = set(do or [])
    qs = [q for q in qs if _NEEDS.get(q) is None or _NEEDS[q] in ticked]
    btns = [b for b in btns if _BTN_NEEDS.get(b) is None or _BTN_NEEDS[b] in ticked]
    return qs[:Q_MAX], btns[:B_MAX]


def _chatbox_row(cur, tenant_id: int):
    """(has_row, quick_qs) from Settings › Chat Box."""
    try:
        cur.execute("SELECT quick_qs FROM chatbox_settings WHERE tenant_id=%s", (tenant_id,))
    except Exception:
        cur.connection.rollback()
        return False, []
    r = cur.fetchone()
    if not r:
        return False, []
    v = r["quick_qs"] if isinstance(r, dict) else r[0]
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = []
    return True, [q for q in (v or []) if isinstance(q, str) and q.strip()][:Q_MAX]


def current(cur, tenant_id: int, p: dict, info: dict) -> dict:
    """What the Starter questions section shows."""
    has_row, box_qs = _chatbox_row(cur, tenant_id)
    saved = bool(p.get("starter_saved"))
    qs = box_qs if has_row else [q for q in (p.get("starter_qs") or []) if isinstance(q, str)][:Q_MAX]
    sug_qs, sug_btns = suggest(info["model"], p.get("sells") or "products", p.get("do") or [])
    suggested = not saved and not qs
    btns = [b for b in (p.get("wa_btns") or []) if isinstance(b, str)][:B_MAX]
    return {"qs": sug_qs if suggested else qs, "suggested": suggested,
            "btn_on": bool(p.get("wa_btn_on")) if saved else True,
            "btns": btns if saved else sug_btns, "sug_qs": sug_qs, "sug_btns": sug_btns,
            # WhatsApp-only sign-ups that later connected their website usually have the chat box too.
            "has_box": info["kind"] != "wa" or bool(info.get("site_read")), "has_wa": bool(info.get("wa_on"))}


def from_form(form, p: dict) -> list:
    """Reads the section into p (only when it was on the page). Returns error messages."""
    if form.get("sq_shown") != "1":
        return []
    errs = []
    qs = [clean_q(q, 200) for q in form.getlist("sq")]
    qs = [q for q in qs if q][:Q_MAX]
    for i, q in enumerate(qs, 1):
        if len(q) > Q_LEN:
            errs.append(f"Starter question {i} can be up to {Q_LEN} characters.")
    btns = [clean_q(b, 100) for b in form.getlist("sq_btn")]
    btns = [b for b in btns if b][:B_MAX]
    for i, b in enumerate(btns, 1):
        if len(b) > B_LEN:
            errs.append(f"Button {i} can be up to {B_LEN} characters.")
    if len(set(x.lower() for x in btns)) != len(btns):
        errs.append("Each button needs different words.")
    if any(_LINK.search(x) for x in qs + btns):
        errs.append("Please take web links out of the starter questions and buttons.")
    if errs:
        return errs
    p["starter_qs"] = qs
    p["wa_btn_on"] = form.get("sq_btn_on") == "1"
    p["wa_btns"] = btns
    p["starter_saved"] = True
    return []


def save(conn, tenant_id: int, p: dict) -> bool:
    """Puts the questions where the chat box reads them. True when they changed."""
    cur = conn.cursor()
    try:
        has_row, box_qs = _chatbox_row(cur, tenant_id)
        qs = p.get("starter_qs") or []
        if has_row:
            changed = box_qs != qs
            if changed:
                cur.execute("UPDATE chatbox_settings SET quick_qs=%s, updated_at=NOW() WHERE tenant_id=%s",
                            (json.dumps(qs) if qs else None, tenant_id))
        else:
            changed = True   # kept with the answers (saved by about_business.save)
        if changed:
            try:   # translations of the old questions for visitors in other languages
                cur.execute("DELETE FROM chatbox_welcome_tr WHERE tenant_id=%s", (tenant_id,))
            except Exception:
                conn.rollback()
        conn.commit()
        return changed
    finally:
        cur.close()


def push_whatsapp(cur, tenant_id: int, qs: list) -> list:
    """Sets the questions as ice breakers on each active WhatsApp number.
    Returns [(display number, error or None)]."""
    cur.execute("""SELECT phone_number_id, access_token, display_phone_number FROM wa_tenants
                    WHERE tenant_id=%s AND active AND access_token IS NOT NULL""", (tenant_id,))
    out = []
    for r in cur.fetchall():
        pnid = r["phone_number_id"] if isinstance(r, dict) else r[0]
        tok = r["access_token"] if isinstance(r, dict) else r[1]
        num = (r["display_phone_number"] if isinstance(r, dict) else r[2]) or pnid
        try:
            res = requests.post(f"{GRAPH}/{pnid}/conversational_automation",
                                headers={"Authorization": f"Bearer {tok}"},
                                json={"prompts": [q[:80] for q in qs]}, timeout=15)
            data = res.json() if res.content else {}
            if res.ok and data.get("success") is not False:
                out.append((num, None))
            else:
                out.append((num, ((data.get("error") or {}).get("message") or f"HTTP {res.status_code}")[:200]))
        except Exception as e:
            out.append((num, f"{type(e).__name__}: {e}"[:200]))
    return out
