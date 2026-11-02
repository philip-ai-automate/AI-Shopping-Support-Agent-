"""
auto_reply_guard.py — stop the AI answering automatic replies (31 Oct 2026).

Problem (seen 28 Oct): a customer's WhatsApp Business "away" message answered
our message, and our AI answered the away message. Two automatic systems can
keep answering each other.

The inbound message is already in wa_message_log (and the Inbox) when this
runs. check() returns:
  "auto_reply" — looks automatic: the AI stays quiet for this one message
  "loop"       — too many back-and-forth messages in a few minutes: the chat
                 is handed to the team (AI paused for this chat)
  None         — a normal message
Never raises: on any error it returns None (the AI answers as before).
"""
import re
from datetime import datetime, timedelta, timezone

from wa_db import get_db_connection

# Counted as "right after our message".
QUICK = timedelta(seconds=120)
# Loop: the same text arriving LOOP_REPEATS times inside LOOP_WINDOW (two
# automatic systems answering each other). Fast-chatting people don't repeat.
LOOP_REPEATS = 3
LOOP_MIN_LEN = 15
LOOP_WINDOW = timedelta(minutes=10)
# Repeated identical text from the same person (only longer texts count).
REPEAT_MIN_LEN = 40
REPEAT_WINDOW = timedelta(days=7)

_AWAY = re.compile(
    r"thanks? (you )?for (contacting|messaging|reaching out|your message|getting in touch)"
    r"|we(?:'re| are)? (?:currently )?(?:away|closed|unavailable|not available|out of (?:the )?office)"
    r"|i(?:'m| am) (?:currently )?(?:away|unavailable|not available|out of (?:the )?office)"
    r"|(?:will|'ll) (?:get back|respond|reply) to you (?:shortly|soon|as soon as|within)"
    r"|out of (?:the )?office"
    r"|auto(?:matic|mated)?[- ]?(?:reply|response|message)"
    r"|this is an automated"
    r"|outside (?:our )?(?:business|working|opening|office) hours"
    r"|our (?:business|working|opening|office) hours are"
    r"|merci (?:pour|de) votre message|nous (?:sommes|reviendrons)"
    r"|gracias por (?:su|tu) mensaje|obrigad[oa] pela (?:sua )?mensagem",
    re.I)


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def looks_away(text: str) -> bool:
    return bool(_AWAY.search(text or ""))


def decide(text: str, now: datetime, last_out_at, same_text_before: int, same_text_recent: int):
    """Pure rule (tested on its own). Times are aware datetimes.
    same_text_before = earlier identical messages in 7 days; same_text_recent
    = earlier identical messages in the last 10 minutes."""
    if len(_norm(text)) >= LOOP_MIN_LEN and same_text_recent + 1 >= LOOP_REPEATS:
        return "loop"
    quick = last_out_at is not None and (now - last_out_at) <= QUICK
    if quick and looks_away(text):
        return "auto_reply"
    if quick and len(_norm(text)) >= REPEAT_MIN_LEN and same_text_before >= 1:
        return "auto_reply"
    return None


def check(tenant_id: int, customer_phone: str, text: str):
    if not text or not customer_phone:
        return None
    conn = None
    try:
        now = datetime.now(timezone.utc)
        conn = get_db_connection()
        if not conn:
            return None
        cur = conn.cursor()
        cur.execute("""
            SELECT MAX(created_at) FROM wa_message_log
             WHERE tenant_id = %s AND customer_phone = %s AND direction = 'outbound'
               AND NOT is_historical AND created_at >= %s""",
            (tenant_id, customer_phone, now - REPEAT_WINDOW))
        last_out = cur.fetchone()[0]
        same = same_recent = 0
        norm = _norm(text)
        if len(norm) >= LOOP_MIN_LEN:
            # Earlier inbound messages with the same text (the current one is
            # already logged, so it counts itself once).
            cur.execute("""
                SELECT COUNT(*), COUNT(*) FILTER (WHERE created_at >= %s) FROM wa_message_log
                 WHERE tenant_id = %s AND customer_phone = %s AND direction = 'inbound'
                   AND NOT is_historical AND created_at >= %s
                   AND lower(regexp_replace(btrim(content), '\\s+', ' ', 'g')) = %s""",
                (now - LOOP_WINDOW, tenant_id, customer_phone, now - REPEAT_WINDOW, norm))
            a, b = cur.fetchone()
            same, same_recent = max(int(a or 0) - 1, 0), max(int(b or 0) - 1, 0)
        cur.close()
        return decide(text, now, last_out, same, same_recent)
    except Exception as e:
        print("⚠️ auto_reply_guard:", e)
        return None
    finally:
        try:
            conn and conn.close()
        except Exception:
            pass
