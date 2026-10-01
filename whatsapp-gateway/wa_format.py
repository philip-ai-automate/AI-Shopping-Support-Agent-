"""Turn the AI's chat formatting into WhatsApp formatting (2026-10-01).

The AI writes the same text for the website chat and WhatsApp: **bold**,
'- ' bullets and [label](url) links. WhatsApp bold is a single *asterisk*,
so **bold** showed stray asterisks to WhatsApp customers.
"""
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_UNDER_BOLD = re.compile(r"__(.+?)__", re.S)
_HEADING = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_LINK = re.compile(r"\[([^\]]+)\]\(((?:https?://|www\.)[^\s)]+)\)")
_BULLET = re.compile(r"(?m)^([ \t]*)[-*][ \t]+")


def to_whatsapp_text(text: str) -> str:
    if not text:
        return text
    s = _LINK.sub(lambda m: m.group(1) if m.group(2) in m.group(1) else f"{m.group(1)}: {m.group(2)}", text)
    s = _HEADING.sub(r"*\1*", s)
    s = _BULLET.sub(r"\1• ", s)          # before bold, so "* item" bullets aren't read as bold
    s = _BOLD.sub(r"*\1*", s)
    s = _UNDER_BOLD.sub(r"_\1_", s)
    return s
