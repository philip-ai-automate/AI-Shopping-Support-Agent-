"""Where a test-chat answer came from (22 Nov 2026, Suggestion 3).

Only for the test chat on AI Agent › About your business (session ids
playground-ab…). Real customer chats never run this. After the AI Agent has
answered, one small check asks which of the things it was given (what the
owner wrote in About your business, website pages, the product list,
uploaded documents) hold the facts the answer used. The page then shows a
line under the answer, such as "From: About your business › Delivery".
"""
import json
import os
import re

_SCHEMA = {
    "name": "answer_sources",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "used": {"type": "array", "items": {"type": "integer"}},
            "not_found": {"type": "boolean"},
        },
        "required": ["used", "not_found"],
    },
}

_PAY_LABELS = {"card": "Card", "bank": "Bank transfer", "paypal": "PayPal", "cash": "Cash",
               "wallet": "Apple Pay / Google Pay", "instal": "Instalments"}


def _s(x, n=1200):
    return re.sub(r"\s+", " ", str(x or "")).strip()[:n]


def _section_for_page(title: str, url: str) -> str:
    """Which box on About your business corrects what this website page says."""
    t = f"{title} {url}".lower()
    if re.search(r"deliver|shipping|postage|return|refund", t):
        return "delivery"
    if re.search(r"hour|opening|contact|find us|location|visit", t):
        return "hours"
    if re.search(r"pay", t):
        return "pay"
    return "else"


def _about_items(profile: dict) -> list:
    p = profile or {}
    out = []

    def add(section, label, text):
        if _s(text):
            out.append({"kind": "ab", "section": section, "label": label, "text": _s(text)})

    add("about", "About your business › What your business does", p.get("what"))
    hours = [h for h in (p.get("hours") or []) if isinstance(h, str) and h.strip()]
    add("hours", "About your business › Hours and address", " ".join(hours) + " " + _s(p.get("addr"), 300))
    add("delivery", "About your business › Delivery", p.get("dl"))
    add("delivery", "About your business › Returns", p.get("rt"))
    pay = p.get("pay") if isinstance(p.get("pay"), dict) else {}
    ways = ", ".join(_PAY_LABELS.get(k, k) for k, v in pay.items() if v)
    add("pay", "About your business › How customers can pay", (ways + " " + _s(p.get("pay_other"), 300)).strip())
    add("bookings", "About your business › Bookings", _s(p.get("book")) + " " + _s(p.get("canc"), 300))
    trade = " ".join(x for x in [("No minimum order" if p.get("moq_none") else _s(p.get("moq"), 200)),
                                 _s(p.get("lead"), 300), _s(p.get("del"), 300)] if x)
    add("trade", "About your business › Trade terms", trade)
    for f in (p.get("faqs") or []):
        if isinstance(f, dict) and _s(f.get("q")):
            add("faq", "About your business › Common questions", f"{_s(f.get('q'), 300)} {_s(f.get('a'))}")
    add("else", "About your business › Anything else", p.get("else_text"))
    return out


# Store Information mirrors of the About your business answers (store_info-<t>-<key>).
_MIRROR = {"about_us": ("about", "About your business › What your business does"),
           "delivery": ("delivery", "About your business › Delivery"),
           "returns": ("delivery", "About your business › Returns"),
           "contact": ("hours", "About your business › Hours and address"),
           "payment": ("pay", "About your business › How customers can pay"),
           "faqs": ("faq", "About your business › Common questions"),
           "custom": ("else", "About your business › Anything else")}


def _doc_item(d: dict):
    did, typ = str(d.get("id") or ""), (d.get("type") or "")
    title = _s(d.get("title"), 120) or "Untitled"
    text = _s(d.get("content"))
    if typ in ("page", "post"):
        url = _s(d.get("url"), 500)
        return {"kind": "site", "section": _section_for_page(title, url),
                "label": f"your website › {title} page", "url": url, "text": f"{title}. {text}"}
    if typ == "store_info":
        if "-fix-" in did:
            return {"kind": "ab", "section": "faq", "label": "About your business › Your corrections", "text": f"{title}. {text}"}
        if "-upload-" in did:
            return {"kind": "ab", "section": "docs", "label": f"About your business › Documents › {title}", "text": text}
        key = did.split("-", 2)[-1] if did.count("-") >= 2 else ""
        sec, label = _MIRROR.get(key, ("else", "About your business › Anything else"))
        return {"kind": "ab", "section": sec, "label": label, "text": text}
    if typ == "product":
        ptext = f"{title}. {_s(d.get('price_min'), 20)} {text}"
        if did.startswith("man-"):        # typed into My Products & Services: can be changed there
            return {"kind": "product", "section": "products", "label": f"My Products & Services › {title}",
                    "q": title, "text": ptext}
        if did.startswith("xls-"):
            return {"kind": "info", "section": "products", "label": f"your uploaded list › {title}", "text": ptext}
        if did.startswith("site-"):
            return {"kind": "info", "section": "products", "label": f"your website › {title}", "text": ptext}
        # Shopify / WooCommerce: changed in the store itself, so no link here.
        return {"kind": "info", "section": "products", "label": f"your store › {title}", "text": ptext}
    return None


def find(client, question: str, answer: str, profile: dict, docs: list) -> dict:
    """{"items": [{kind, section, label, url?, q?}], "not_found": bool}. Never raises."""
    try:
        cands, seen = [], set()
        for it in _about_items(profile) + [x for x in (_doc_item(d) for d in (docs or [])) if x]:
            k = (it["label"], it["text"][:200])
            if k in seen:
                continue
            seen.add(k)
            cands.append(it)
        listing = "\n".join(f"[{i}] {c['label']}: {c['text'][:1200]}" for i, c in enumerate(cands)) or "(none)"
        prompt = (
            "A business's AI Agent gave the answer below to a customer. Which numbered sources hold the facts "
            "the answer states? List a source only if the answer states a fact that is in it. When two sources "
            "disagree, list only the one the answer agrees with. If the answer only greets, asks a question back, "
            "or says it doesn't have the information or will check with the team, list none.\n"
            "Set not_found to true only if the answer says it doesn't have the information, can't answer, or "
            "will pass the question to the team.\n\n"
            f"Customer: {_s(question, 1000)}\nAnswer: {_s(answer, 2000)}\n\nSources:\n{listing}"
        )
        r = client.chat.completions.create(
            model=os.getenv("RELEVANCE_CHECK_MODEL", "gpt-5.6-luna"),
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=2000,   # room for the model's thinking (300 ran out, 22 Nov)
            response_format={"type": "json_schema", "json_schema": _SCHEMA},
        )
        parsed = json.loads(r.choices[0].message.content)
        items, labels = [], set()
        for i in parsed.get("used") or []:
            if isinstance(i, int) and 0 <= i < len(cands) and cands[i]["label"] not in labels:
                c = cands[i]
                labels.add(c["label"])
                items.append({k: c[k] for k in ("kind", "section", "label", "url", "q") if k in c})
        return {"items": items[:4], "not_found": bool(parsed.get("not_found")) and not items}
    except Exception as e:
        print(f"⚠️ answer_sources: {type(e).__name__}: {e}")
        return {"items": [], "not_found": False}
