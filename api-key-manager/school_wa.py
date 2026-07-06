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


def check_template_status(school_id: int, template_name: str) -> str:
    """Query Meta for a template's current review status.
    Returns 'APPROVED' | 'PENDING' | 'REJECTED' | 'NOT_FOUND' | 'ERROR'."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            "SELECT wa_waba_id, wa_access_token FROM school_profiles WHERE id=%s",
            (school_id,)
        )
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row or not row[0] or not row[1]:
            return "ERROR"
        waba_id, access_token = row
        r = _req.get(
            f"{_GRAPH}/{waba_id}/message_templates",
            params={"name": template_name},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if r.status_code != 200:
            return "ERROR"
        data = r.json().get("data", [])
        if not data:
            return "NOT_FOUND"
        return data[0].get("status", "ERROR")
    except Exception as e:
        print(f"⚠️ check_template_status error: {e}")
        return "ERROR"


def get_school_template(school_id: int, template_type: str) -> dict | None:
    """Return {template_name, language_code} if the school has configured a
    Meta-approved template for this notification type, else None.
    template_type: 'absence_alert' | 'fee_reminder' | (future) 'result_summary'."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            "SELECT template_name, language_code FROM school_wa_templates "
            "WHERE school_id=%s AND template_type=%s",
            (school_id, template_type)
        )
        row = cur.fetchone()
        cur.close(); conn.close()
        if row:
            return {"template_name": row[0], "language_code": row[1] or "en_US"}
        return None
    except Exception as e:
        print(f"⚠️ get_school_template error: {e}")
        return None


def send_wa_template(school_id: int, to: str, template_name: str,
                     language_code: str, body_params: list,
                     button_param: str | None = None) -> bool:
    """Send a Meta-approved template message (works outside the 24h customer
    service window, unlike send_wa_text). body_params fill {{1}}, {{2}}, ... in
    the template body, in order. button_param fills the dynamic suffix of a
    template's "Website (Dynamic URL)" button, if the approved template has one —
    e.g. a payment link token appended to the button's base URL."""
    creds = _get_school_wa_creds(school_id)
    if not creds:
        print(f"⚠️ school {school_id} has no WhatsApp credentials")
        return False
    number = _normalise_number(to)
    components = []
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        })
    if button_param:
        components.append({
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": str(button_param)}],
        })
    try:
        r = _req.post(
            f"{_GRAPH}/{creds['phone_number_id']}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": number,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language_code},
                    "components": components,
                },
            },
            headers={"Authorization": f"Bearer {creds['access_token']}"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"⚠️ WA template send failed {r.status_code}: {r.text[:300]}")
        return r.status_code == 200
    except Exception as e:
        print(f"⚠️ send_wa_template error: {e}")
        return False


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
    """Notify a parent that their child was marked absent.

    Uses the school's Meta-approved absence template when configured — required
    because this is a business-initiated message and will be silently rejected
    by Meta outside the 24h customer service window if sent as plain text.
    Falls back to plain text only when no template is configured (works only
    if the parent has messaged the school's WhatsApp number in the last 24h).
    """
    template = get_school_template(school_id, "absence_alert")
    if template:
        return send_wa_template(
            school_id, parent_wa,
            template_name=template["template_name"],
            language_code=template["language_code"],
            body_params=[student_name, class_name, date_str, school_name],
        )
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
                      due_date: str, school_name: str,
                      payment_token: str | None = None) -> bool:
    """Notify a parent of an outstanding fee balance.

    Uses the school's Meta-approved fee-reminder template when configured —
    same 24h-window constraint as send_attendance_alert(). Falls back to plain
    text only when no template is configured. When payment_token is given (a
    payment gateway is connected for this school), the template's dynamic pay
    button is filled in, and the plaintext fallback includes the pay link.
    """
    template = get_school_template(school_id, "fee_reminder")
    if template:
        return send_wa_template(
            school_id, parent_wa,
            template_name=template["template_name"],
            language_code=template["language_code"],
            body_params=[school_name, student_name, f"{balance:,.2f}", fee_name, due_date],
            button_param=payment_token,
        )
    paid = amount - balance
    pay_line = ""
    if payment_token:
        from school_payments import SCHOOL_PAY_BASE_URL
        pay_line = f"\nPay online: {SCHOOL_PAY_BASE_URL}/pay/{payment_token}\n"
    msg = (
        f"*{school_name} — Fee Reminder*\n\n"
        f"Dear Parent/Guardian,\n\n"
        f"This is a reminder that *{student_name}* has an outstanding fee balance.\n\n"
        f"*Fee:* {fee_name}\n"
        f"*Total Amount:* ₦{amount:,.2f}\n"
        f"*Amount Paid:* ₦{paid:,.2f}\n"
        f"*Balance:* ₦{balance:,.2f}\n"
        f"*Due Date:* {due_date}\n"
        f"{pay_line}\n"
        f"Please visit the school bursar to settle this balance.\n\n"
        f"_Automated reminder from {school_name}_"
    )
    return send_wa_text(school_id, parent_wa, msg)


def send_fee_payment_confirmation(school_id: int, parent_wa: str,
                                  student_name: str, fee_name: str,
                                  amount_paid: float, balance: float,
                                  school_name: str) -> bool:
    """Notify a parent that a fee payment was received and verified.

    Uses the school's Meta-approved payment-confirmation template when
    configured — same 24h-window constraint as send_fee_reminder(). Falls back
    to plain text only when no template is configured.
    """
    template = get_school_template(school_id, "fee_payment_confirmed")
    if template:
        return send_wa_template(
            school_id, parent_wa,
            template_name=template["template_name"],
            language_code=template["language_code"],
            body_params=[student_name, f"{amount_paid:,.2f}", fee_name, f"{balance:,.2f}", school_name],
        )
    msg = (
        f"*{school_name} — Payment Received*\n\n"
        f"Dear Parent/Guardian,\n\n"
        f"We've received a payment of *₦{amount_paid:,.2f}* towards *{student_name}*'s "
        f"{fee_name}.\n\n"
        f"*Remaining Balance:* ₦{balance:,.2f}\n\n"
        f"Thank you!\n\n"
        f"_Automated confirmation from {school_name}_"
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
    """Convert a local Nigerian number to E.164 (2348012345678). Numbers that
    already carry a country code (any length > 10 not starting with a local
    leading 0) are passed through unchanged — this only rewrites bare local
    Nigerian formats, it does not assume every number is Nigerian."""
    n = number.strip().replace(" ", "").replace("-", "").replace("+", "")
    if n.startswith("0") and len(n) == 11:
        n = "234" + n[1:]
    elif len(n) <= 10:
        n = "234" + n
    return n
