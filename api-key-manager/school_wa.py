"""
school_wa.py — WhatsApp sending helpers for PhiXtra School.
All functions use the school's own Meta credentials stored in school_profiles.
"""
import requests as _req
from db import get_db_connection

_GRAPH = "https://graph.facebook.com/v19.0"


def _get_school_wa_creds(school_id: int) -> dict | None:
    """Return {phone_number_id, access_token} or None if not configured."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            "SELECT wa_phone_number_id, wa_access_token FROM school_profiles WHERE id=%s",
            (school_id,)
        )
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] and row[1]:
            return {"phone_number_id": row[0], "access_token": row[1]}
        return None
    except Exception as e:
        print(f"⚠️ _get_school_wa_creds error: {e}")
        return None


def send_wa_text(school_id: int, to: str, text: str) -> bool:
    """Send a plain text WhatsApp message from the school's number."""
    creds = _get_school_wa_creds(school_id)
    if not creds:
        print(f"⚠️ school {school_id} has no WhatsApp credentials")
        return False
    # Normalise to E.164 (strip leading 0, prepend 234 for Nigeria)
    number = _normalise_number(to)
    try:
        r = _req.post(
            f"{_GRAPH}/{creds['phone_number_id']}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": number,
                "type": "text",
                "text": {"body": text, "preview_url": False},
            },
            headers={"Authorization": f"Bearer {creds['access_token']}"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"⚠️ WA send failed {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"⚠️ send_wa_text error: {e}")
        return False


def send_attendance_alert(school_id: int, parent_wa: str,
                          student_name: str, class_name: str,
                          date_str: str, school_name: str) -> bool:
    msg = (
        f"*{school_name}*\n\n"
        f"Dear Parent/Guardian,\n\n"
        f"This is to inform you that *{student_name}* ({class_name}) "
        f"was marked *absent* today, {date_str}.\n\n"
        f"If this is a mistake or you have already informed the school, "
        f"please disregard this message.\n\n"
        f"_Automated alert from {school_name}_"
    )
    return send_wa_text(school_id, parent_wa, msg)


def send_fee_reminder(school_id: int, parent_wa: str,
                      student_name: str, fee_name: str,
                      amount: float, balance: float,
                      due_date: str, school_name: str) -> bool:
    paid = amount - balance
    msg = (
        f"*{school_name} — Fee Reminder*\n\n"
        f"Dear Parent/Guardian,\n\n"
        f"This is a reminder that *{student_name}* has an outstanding fee balance.\n\n"
        f"*Fee:* {fee_name}\n"
        f"*Total Amount:* ₦{amount:,.2f}\n"
        f"*Amount Paid:* ₦{paid:,.2f}\n"
        f"*Balance:* ₦{balance:,.2f}\n"
        f"*Due Date:* {due_date}\n\n"
        f"Please visit the school bursar to settle this balance.\n\n"
        f"_Automated reminder from {school_name}_"
    )
    return send_wa_text(school_id, parent_wa, msg)


def send_broadcast(school_id: int, parent_wa: str,
                   message: str, school_name: str) -> bool:
    full_msg = f"*{school_name}*\n\n{message}"
    return send_wa_text(school_id, parent_wa, full_msg)


def send_result_summary(school_id: int, parent_wa: str,
                        student_name: str, class_name: str,
                        term: str, session: str,
                        subjects: list, average: float,
                        position: int, class_size: int,
                        school_name: str) -> bool:
    subject_lines = "\n".join(
        f"  • {s['subject']}: {s['score']} ({s.get('grade','')})"
        for s in subjects
    )
    msg = (
        f"*{school_name} — Term Result*\n\n"
        f"*Student:* {student_name}\n"
        f"*Class:* {class_name}\n"
        f"*Term:* {term} Term, {session}\n\n"
        f"*Subjects:*\n{subject_lines}\n\n"
        f"*Average:* {average:.1f}%\n"
        f"*Position:* {position} out of {class_size}\n\n"
        f"_Result delivered by {school_name} via PhiXtra_"
    )
    return send_wa_text(school_id, parent_wa, msg)


def _normalise_number(number: str) -> str:
    """Convert Nigerian number to E.164 (2348012345678)."""
    n = number.strip().replace(" ", "").replace("-", "").replace("+", "")
    if n.startswith("0") and len(n) == 11:
        n = "234" + n[1:]
    if not n.startswith("234"):
        n = "234" + n
    return n
