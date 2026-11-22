"""What the AI Agent is told from "Tell your AI Agent about your business" (22 Nov 2026).

Shared by the portal (the page) and the AI service, which adds this text to
every answer's instruction. ai-backend/about_business_prompt.py is a symlink
to this file, like ai_default_instruction.py.

The page saves one JSON record per business (table ai_business_profile).
Nothing here touches the database; build_block() only turns that record into
plain instructions, so the same record always gives the same text.
"""

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# What the AI Agent should do, per kind of business. The page shows these
# words as tick boxes; the record keeps the keys.
TASKS = {
    # 22 Nov 2026 rule 6: no "Collect quotation requests" tick; the price question decides.
    "b2b":      [("products", "Answer questions about products"),
                 ("terms", "Explain trade terms (minimum order, lead times)")],
    "services": [("prices", "Answer questions about services and prices"),
                 ("bookings", "Take booking requests for my team"),
                 ("cancellations", "Explain cancellations")],
    "products": [("prices", "Answer questions about products and prices"),
                 ("orders", "Help customers place an order"),
                 ("delivery", "Explain delivery and returns")],
    "both":     [("prices", "Answer questions about products, services and prices"),
                 ("orders", "Help customers place an order"),
                 ("bookings", "Take booking requests for my team"),
                 ("delivery", "Explain delivery and returns")],
}

DEFAULT_TASKS = {"b2b": ["products", "terms"], "services": ["prices", "bookings"],
                 "products": ["prices", "orders", "delivery"], "both": ["prices", "orders", "bookings", "delivery"]}

PAYMENT_OPTIONS = {
    "services": [("card", "Card"), ("cash", "Cash"), ("bank", "Bank transfer"), ("instal", "Payment plans")],
    "products": [("card", "Card"), ("wallet", "Apple Pay / Google Pay"), ("paypal", "PayPal"),
                 ("instal", "Pay in instalments (for example Klarna)"), ("bank", "Bank transfer"),
                 ("cash", "Cash on delivery")],
    "both":     [("card", "Card"), ("wallet", "Apple Pay / Google Pay"), ("paypal", "PayPal"), ("bank", "Bank transfer"),
                 ("instal", "Payment plans or instalments"), ("cash", "Cash")],
}

# What the AI Agent does when a box is NOT ticked (shown under the boxes).
IF_NOT = {"prices": "Your AI Agent passes price questions to your team.",
          "products": "Your AI Agent passes product questions to your team.",
          "orders": "Your AI Agent won't take orders or ask customers to reply ORDER; it passes orders to your team.",
          "bookings": "Your AI Agent explains how to book instead of taking booking requests.",
          "cancellations": "Your AI Agent passes cancellation questions to your team.",
          "delivery": "Your AI Agent passes delivery and returns questions to your team.",
          "terms": "Your AI Agent passes trade-term questions to your team."}


def pay_options(sells: str) -> list:
    return PAYMENT_OPTIONS.get(sells if sells in PAYMENT_OPTIONS else "products")


def task_group(model: str, sells: str) -> str:
    """Which list of tasks a business sees: B2B, services or products."""
    if model == "b2b":
        return "b2b"
    return sells if sells in ("services", "both") else "products"


def _s(v, limit=2000) -> str:
    return " ".join(str(v or "").split())[:limit]


def build_block(p: dict) -> str:
    """The text added to the AI Agent's instruction. Empty when nothing is saved."""
    if not isinstance(p, dict) or not p:
        return ""
    # Tolerate old or odd saved data: wrong shapes are treated as empty.
    p = dict(p)
    if not isinstance(p.get("do"), list):
        p["do"] = []
    if not isinstance(p.get("hours"), list):
        p["hours"] = []
    if not isinstance(p.get("pay"), dict):
        p["pay"] = {}
    if not isinstance(p.get("faqs"), list):
        p["faqs"] = []
    if p.get("sells") not in ("products", "services", "both"):
        p["sells"] = "products"
    model = p.get("model") or "b2c"
    sells = p.get("sells") or "products"
    group = task_group(model, sells)
    do = set(p.get("do") or [])
    facts = []

    name = _s(p.get("business_name"), 200)
    what = _s(p.get("what"), 1500)
    if name or what:
        facts.append(f"- The business: {name or 'this business'}." + (f" {what}" if what else ""))
    if sells == "both" and model != "b2b":
        facts.append("- It sells products and services.")

    hours = p.get("hours") or []
    hrs = [f"{DAYS[i]} {_s(h, 80)}" for i, h in enumerate(hours[:7]) if _s(h)]
    if hrs:
        label = "Opening hours" if sells == "services" else "When the team replies"
        facts.append(f"- {label}: " + "; ".join(hrs) + ".")
    if _s(p.get("addr")):
        facts.append(f"- Address: {_s(p.get('addr'), 300)}.")

    if model == "b2b" and "terms" in do:
        if p.get("moq_none"):
            facts.append("- Minimum order: there is no minimum order.")
        elif _s(p.get("moq")):
            facts.append(f"- Minimum order: {_s(p.get('moq'), 200)}.")
        if _s(p.get("lead")):
            facts.append(f"- Lead time: {_s(p.get('lead'), 300)}.")
        if _s(p.get("del")):
            facts.append(f"- Delivers to: {_s(p.get('del'), 300)}.")
    if model == "b2b":
        if _s(p.get("payment_terms")):
            facts.append(f"- Payment terms: {_s(p.get('payment_terms'), 300)}.")
    else:
        # Only what the page shows for these answers (22 Nov 2026 if/then rules).
        if group in ("services", "both") and _s(p.get("book")):
            facts.append(f"- How customers book: {_s(p.get('book'), 800)}")
        if _s(p.get("canc")) and (group == "both" or (group == "services" and "cancellations" in do)):
            facts.append(f"- Cancellation rule: {_s(p.get('canc'), 300)}.")
        if group in ("products", "both") and "delivery" in do:
            if _s(p.get("dl")):
                facts.append(f"- Delivery: {_s(p.get('dl'), 500)}.")
            if _s(p.get("rt")):
                facts.append(f"- Returns: {_s(p.get('rt'), 500)}.")
        pay = p.get("pay") or {}
        labels = dict(pay_options(sells))
        ways = [labels[k] for k in labels if pay.get(k)]
        if ways or _s(p.get("pay_other")):
            facts.append("- Ways to pay: " + (", ".join(ways) + "." if ways else "")
                         + (f" {_s(p.get('pay_other'), 300)}" if _s(p.get("pay_other")) else ""))

    faqs = [(_s(f.get("q"), 300), _s(f.get("a"), 1200)) for f in (p.get("faqs") or []) if isinstance(f, dict)]
    faqs = [(q, a) for q, a in faqs if q and a]
    if faqs:
        facts.append("- Common questions, with the owner's exact answers:")
        facts += [f"  Q: {q}\n  A: {a}" for q, a in faqs[:40]]
    if _s(p.get("else_text")):
        facts.append(f"- Other notes from the owner: {_s(p.get('else_text'), 4000)}")

    rules = []
    if group == "b2b":
        if p.get("bp") == "quote":
            rules.append("When a buyer asks for a price, do not give a price. Collect a quotation request: the company "
                         "name, an email address, the products and the quantities. Then say the team will send a quotation.")
        elif p.get("bp") == "list":
            rules.append("When a buyer asks for a price, give the price exactly as it appears in the product data.")
        rules.append("Never ask the buyer to reply ORDER.")
        if "terms" not in do:
            rules.append("If a buyer asks about trade terms, offer to pass the question to the team.")
    elif group == "services":
        if "bookings" in do:
            rules.append("Take booking requests: ask for the customer's name, phone number and the service they want, "
                         "then say the team will confirm a time. Never ask them to reply ORDER.")
        else:
            rules.append("Do not take bookings yourself; explain how to book. Never ask the customer to reply ORDER.")
        if "cancellations" not in do:
            rules.append("If a customer asks about cancelling, offer to pass the question to the team.")
        if "prices" not in do:
            rules.append("If a customer asks about prices, offer to pass the question to the team.")
    else:
        if group == "both" and "bookings" in do:
            rules.append("For services, take booking requests: ask for the customer's name, phone number and the service "
                         "they want, then say the team will confirm a time.")
        if "orders" not in do:
            rules.append("Do not take orders and never ask the customer to reply ORDER; offer to pass the order to the team.")
        if "delivery" not in do:
            rules.append("If a customer asks about delivery or returns, offer to pass the question to the team.")
        if "prices" not in do:
            rules.append("If a customer asks about prices, offer to pass the question to the team.")

    if not facts and not rules:
        return ""
    out = "\n\nABOUT THIS BUSINESS (from the owner. Use it to answer; it is more up to date than anything else except the product data):\n"
    out += "\n".join(facts) if facts else "- (nothing added yet)"
    if p.get("has_site"):
        # Fix 2 (22 Nov 2026): the owner's own words win over the website.
        rules.append("The facts above, and the business's own product and service list, are the most up to date. "
                     "If a website page in the information you are given says something different, use these facts. "
                     "Never give a customer both answers.")
    if rules:
        out += "\n\nHOW TO HELP THIS BUSINESS'S CUSTOMERS:\n" + "\n".join("- " + r for r in rules)
    out += ("\nIf the answer to a question is not here, in the product data or in the knowledge base, say you will check "
            "with the team. Never guess.")
    return out
