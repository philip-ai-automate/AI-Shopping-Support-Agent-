"""Password rules for every PhiXtra login (Nov 2026, Shopify App Store item 14).

- At least 8 characters with a special character (unchanged, PASSWORD_RULE_TEXT).
- Not a common or leaked password: the NCSC 100k list (common_passwords.txt,
  kept on our own server, nothing is sent anywhere). A dictionary word with
  numbers/symbols stuck on the ends ("Summer2026!", "P@ssw0rd1") counts too.
- Not the person's own email name or business name.
- No forced regular changes (NCSC guidance + Cyber Essentials: 8+ characters
  with a common-password block). Change only when a password may have leaked.
"""
import os
import re

PASSWORD_RULE_TEXT = "at least 8 characters, including a special character such as ! @ # $ %"
MSG_RULE = "Your password must be " + PASSWORD_RULE_TEXT + "."
MSG_COMMON = "This password is too common and easy to guess. Please choose another."
MSG_PERSONAL = "Please don't use your email or business name in your password."

_LIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "common_passwords.txt")
_common = None
_LEET = str.maketrans({"@": "a", "4": "a", "0": "o", "1": "i", "!": "i", "3": "e", "$": "s", "5": "s", "7": "t"})


def _common_set():
    global _common
    if _common is None:
        try:
            with open(_LIST_PATH, encoding="utf-8", errors="ignore") as f:
                _common = frozenset(l.strip() for l in f if l.strip())
        except OSError:
            _common = frozenset()
    return _common


def _cores(pw: str):
    """The password, lower case, plus its letters-only middle (ends of digits
    and symbols removed), each also with look-alike characters turned back
    into letters."""
    p = pw.strip().lower()
    out = {p, p.translate(_LEET)}
    for v in list(out):
        core = re.sub(r"^[^a-z]+|[^a-z]+$", "", v)
        if len(core) >= 4:
            out.add(core)
    # 'p@ssw0rd!' → strip the symbol ends first, then undo look-alikes
    core = re.sub(r"^[^a-z@$0-9]+|[^a-z@$0-9]+$", "", p)
    core = re.sub(r"[0-9]+$", "", core).translate(_LEET)
    core = re.sub(r"^[^a-z]+|[^a-z]+$", "", core)
    if len(core) >= 4:
        out.add(core)
    return out


def is_common(pw: str) -> bool:
    s = _common_set()
    return any(c in s for c in _cores(pw or ""))


def _personal_words(email=None, names=()):
    words = set()
    if email and "@" in email:
        local = email.split("@", 1)[0].lower()
        for w in re.split(r"[^a-z0-9]+", local) + [re.sub(r"[^a-z0-9]", "", local)]:
            if len(w) >= 4:
                words.add(w)
    for n in names or ():
        n = (n or "").lower()
        whole = re.sub(r"[^a-z0-9]", "", n)
        if len(whole) >= 4:
            words.add(whole)
        for w in re.split(r"[^a-z0-9]+", n):
            if len(w) >= 5 and w not in {"limited", "company", "group", "services", "global", "international", "trading", "store", "shop"}:
                words.add(w)
    return words


def password_problem(pw: str, email=None, names=()):
    """None when the password is allowed, else the message to show."""
    pw = pw or ""
    if len(pw) < 8 or not re.search(r"[^A-Za-z0-9\s]", pw):
        return MSG_RULE
    if is_common(pw):
        return MSG_COMMON
    flat = re.sub(r"[^a-z0-9]", "", pw.lower())
    if any(w in flat for w in _personal_words(email, names)):
        return MSG_PERSONAL
    return None
