"""Fixed messages in the customer's language (2026-10-29).

A few replies are sent without the AI: the polite answer when the AI comes
back blank, the website chat reply while the AI is off, the photo hand-over.
They used to be English only, so a French buyer got English. This picks the
customer's language from their own message and returns the message in it.

Covers the languages the B2B page promises: English, French, Spanish,
Portuguese, Arabic. Anything else (and Nigerian Pidgin) gets English.
Kept in step with whatsapp-gateway/reply_language.py.
"""

import re

_WORDS = {
    "fr": {"bonjour", "bonsoir", "merci", "je", "vous", "nous", "est", "pour", "avec", "pas", "les", "des",
           "une", "le", "la", "et", "votre", "quel", "quelle", "combien", "prix", "il", "me", "faut", "oui",
           "non", "suis", "avez", "pouvez", "s'il", "plaît", "livraison", "commande"},
    "es": {"hola", "gracias", "por", "favor", "usted", "nosotros", "es", "para", "con", "los", "las", "una",
           "el", "y", "su", "cuánto", "cuanto", "precio", "necesito", "quiero", "puede", "pueden", "sí",
           "envío", "pedido", "buenos", "días", "tardes", "qué", "cómo"},
    "pt": {"olá", "ola", "obrigado", "obrigada", "você", "voce", "vocês", "nós", "é", "para", "com", "os",
           "as", "uma", "um", "o", "e", "seu", "sua", "quanto", "preço", "preco", "preciso", "quero", "pode",
           "podem", "sim", "não", "nao", "entrega", "pedido", "bom", "dia", "qual", "prazo"},
}

_TEXT = {
    "blank_answer": {
        "en": "Sorry, I couldn't answer that just now. A member of our team will get back to you soon.",
        "fr": "Désolé, je ne peux pas répondre pour le moment. Un membre de notre équipe vous répondra très bientôt.",
        "es": "Lo siento, no puedo responder en este momento. Un miembro de nuestro equipo le responderá muy pronto.",
        "pt": "Desculpe, não consigo responder agora. Um membro da nossa equipa vai responder-lhe muito em breve.",
        "ar": "عذرًا، لا أستطيع الإجابة الآن. سيتواصل معك أحد أعضاء فريقنا قريبًا.",
    },
    "team_will_reply": {
        "en": "Thanks for your message. Our team will get back to you soon.",
        "fr": "Merci pour votre message. Notre équipe vous répondra très bientôt.",
        "es": "Gracias por su mensaje. Nuestro equipo le responderá muy pronto.",
        "pt": "Obrigado pela sua mensagem. A nossa equipa vai responder-lhe muito em breve.",
        "ar": "شكرًا على رسالتك. سيرد عليك فريقنا قريبًا.",
    },
    "leave_contact": {
        "en": "Please leave your email address or phone number so we can reach you.",
        "fr": "Merci de nous laisser votre adresse e-mail ou votre numéro de téléphone pour que nous puissions vous joindre.",
        "es": "Por favor, déjenos su correo electrónico o número de teléfono para que podamos contactarle.",
        "pt": "Por favor, deixe o seu e-mail ou número de telefone para que possamos contactá-lo.",
        "ar": "يرجى ترك بريدك الإلكتروني أو رقم هاتفك حتى نتمكن من التواصل معك.",
    },
    "photo_check": {
        "en": "Thanks for the photo! I'm not 100% sure which item that is from our catalogue, so I'm getting a team member to help confirm. They'll be with you soon.",
        "fr": "Merci pour la photo ! Je ne suis pas sûr à 100 % de l'article de notre catalogue, alors je demande à un membre de l'équipe de confirmer. Il vous répondra très bientôt.",
        "es": "¡Gracias por la foto! No estoy 100 % seguro de qué artículo de nuestro catálogo es, así que un miembro del equipo lo confirmará. Le responderá muy pronto.",
        "pt": "Obrigado pela foto! Não tenho a certeza de qual artigo do nosso catálogo é, por isso vou pedir a um membro da equipa para confirmar. Vai responder-lhe muito em breve.",
        "ar": "شكرًا على الصورة! لست متأكدًا تمامًا من المنتج في كتالوجنا، لذلك سيؤكد ذلك أحد أعضاء الفريق. سيرد عليك قريبًا.",
    },
}


def detect(text):
    """'en' | 'fr' | 'es' | 'pt' | 'ar' from a customer's message (English if unsure)."""
    t = (text or "").strip().lower()
    if not t:
        return "en"
    letters = [c for c in t if c.isalpha()]
    if letters and sum(1 for c in letters if "؀" <= c <= "ۿ") / len(letters) > 0.3:
        return "ar"
    words = re.findall(r"[a-zà-ÿ']+", t)
    if not words:
        return "en"
    scores = {lang: sum(1 for w in words if w in vocab) for lang, vocab in _WORDS.items()}
    # "e", "o", "a" alone are weak signs; accents and longer words decide.
    if re.search(r"[ãõ]|ção|ções", t):
        scores["pt"] += 2
    if re.search(r"[ñ¿¡]", t):
        scores["es"] += 2
    if re.search(r"[èêëàâçùûœ]", t):
        scores["fr"] += 1
    best = max(scores, key=scores.get)
    if scores[best] >= 2 and scores[best] >= 0.2 * len(words):
        return best
    return "en"


def text(key, customer_text=""):
    """The fixed message `key` in the language of `customer_text`."""
    msgs = _TEXT[key]
    return msgs.get(detect(customer_text), msgs["en"])
