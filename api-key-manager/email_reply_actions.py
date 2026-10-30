"""
email_reply_actions.py — what happens when someone replies to an email
campaign (user, 30 Oct 2026, after a reply "Let's talk on WhatsApp +234…"
sat unnoticed on an unassigned lead).

Called by mailbox_routes once a reply found in a connected Outlook is saved:
  1. Nobody owns the lead → it goes to the person whose mailbox got the reply.
  2. A phone number written in the reply is saved on the lead (and its
     contact): WhatsApp number when they mention WhatsApp (or don't say),
     Phone number when they ask to be called. A field that already holds a
     different number is never overwritten — the new one is added to Notes
     as "Other numbers", which search also finds.
  3. Their follow-up is set for today, with the reply as the note, so it is
     on the person's Follow-ups list now.
  4. The lead's owner gets an email straight away.
"""
import re

import phonenumbers
import psycopg2.extras

from db import get_db_connection

PORTAL = "https://portal.phixtra.com"
_CALL_WORDS = re.compile(r"\b(call|ring|phone me|phone call)\b", re.I)
_WA_WORDS = re.compile(r"whats\s*app|\bwa\b", re.I)


def numbers_in_text(text: str, country: str = None) -> list:
    """Phone numbers written in the text, as +<country><number>. A number
    without +country is read as the lead's own country's; with no country
    known, only numbers written with + are taken (never guessed)."""
    region = (country or "").upper() or None
    out = []
    try:
        for m in phonenumbers.PhoneNumberMatcher(text or "", region or "ZZ"):
            if phonenumbers.is_valid_number(m.number):
                e164 = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
                if e164 not in out:
                    out.append(e164)
    except Exception as e:
        print("⚠️ numbers_in_text:", e)
    return out


def _digits(s) -> str:
    return re.sub(r"\D", "", s or "")


def _same(a, b) -> bool:
    da, db = _digits(a), _digits(b)
    return bool(da and db) and (da[-9:] == db[-9:])


def on_reply(tenant_id: int, lead_id: int, reply_id: int, owner_key: str, owner_label: str,
             reply_text: str, subject: str = "") -> dict:
    """Run after the reply row is committed. Never raises."""
    done = {}
    if not lead_id:
        return done
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute("""SELECT id, customer_name, contact_person, phone, whatsapp_number, notes,
                                  addr_country, assigned_key, assigned_to, wa_contact_id
                             FROM merchant_pipeline_leads WHERE id=%s AND tenant_id=%s""", (lead_id, tenant_id))
            lead = cur.fetchone()
            if not lead:
                return done
            # 1. Owner
            if not lead["assigned_key"] and owner_key:
                cur.execute("""UPDATE merchant_pipeline_leads SET assigned_key=%s, assigned_to=%s, updated_at=NOW()
                                WHERE id=%s AND assigned_key IS NULL""", (owner_key, owner_label, lead_id))
                if cur.rowcount:
                    lead["assigned_key"], lead["assigned_to"] = owner_key, owner_label
                    done["assigned"] = owner_label
            # 2. Numbers in the reply
            nums = numbers_in_text(reply_text, lead["addr_country"])
            field = "phone" if (_CALL_WORDS.search(reply_text or "") and not _WA_WORDS.search(reply_text or "")) \
                else "whatsapp_number"
            extra = []
            for n in nums:
                if any(_same(n, lead[f]) for f in ("phone", "whatsapp_number")):
                    if field == "whatsapp_number" and not lead["whatsapp_number"] and _same(n, lead["phone"]):
                        pass   # their phone number is also their WhatsApp: fill it below
                    else:
                        continue
                if not lead[field]:
                    cur.execute(f"UPDATE merchant_pipeline_leads SET {field}=%s, updated_at=NOW() WHERE id=%s",
                                (n, lead_id))
                    lead[field] = n
                    done[field] = n
                    if lead["wa_contact_id"]:
                        col = "whatsapp_number" if field == "whatsapp_number" else "phone"
                        try:
                            cur.execute("SAVEPOINT rc")
                            cur.execute(f"""UPDATE wa_contacts SET {col}=%s, updated_at=NOW()
                                             WHERE id=%s AND tenant_id=%s AND COALESCE({col}, '')=''""",
                                        (_digits(n), lead["wa_contact_id"], tenant_id))
                            cur.execute("RELEASE SAVEPOINT rc")
                        except Exception as e:   # number already on another contact
                            cur.execute("ROLLBACK TO SAVEPOINT rc")
                            print("⚠️ on_reply contact number:", e)
                elif not re.search(re.escape(_digits(n)[-9:]), _digits(lead["notes"])):
                    extra.append(n)
            if extra:
                line = "Other numbers (from their email reply): " + ", ".join(extra)
                cur.execute("""UPDATE merchant_pipeline_leads
                                  SET notes = CASE WHEN COALESCE(notes,'')='' THEN %s ELSE notes || E'\\n' || %s END,
                                      updated_at=NOW() WHERE id=%s""", (line, line, lead_id))
                done["notes"] = extra
            conn.commit()

            # 3. Follow-up today
            text = re.sub(r"\s+", " ", reply_text or "").strip()
            short = (text[:180] + "…") if len(text) > 180 else text
            channel = "whatsapp" if (lead["whatsapp_number"] and _WA_WORDS.search(text)) else \
                      ("call" if field == "phone" and done.get("phone") else "email")
            try:
                import lead_followups as _fu
                _fu.set_next(tenant_id, lead_id, _fu._today(tenant_id),
                             {"key": owner_key or lead["assigned_key"] or "", "label": "Email reply"},
                             source="reply_logged", note=f"Replied to the email: “{short}”", channel=channel)
                done["followup"] = "today"
            except Exception as e:
                print("⚠️ on_reply follow-up:", e)

            # 4. Tell the lead's owner now
            try:
                import auto_followups as _af
                who = lead["contact_person"] or lead["customer_name"] or "A lead"
                biz = lead["customer_name"] or ""
                lines = [f"{who}{' (' + biz + ')' if biz and biz != who else ''} replied to your email"
                         + (f" “{subject}”" if subject else "") + ":",
                         f"“{short}”"]
                saved = done.get("whatsapp_number") or done.get("phone")
                if saved:
                    lines.append(f"The number {saved} is now saved as their "
                                 f"{'WhatsApp number' if done.get('whatsapp_number') else 'phone number'}.")
                if done.get("assigned"):
                    lines.append(f"Nobody owned this lead, so it is now assigned to {done['assigned']}.")
                lines.append("It is on your Follow-ups for today.")
                done["alert"] = _af.notify(cur, tenant_id, "owner", f"email_reply:{reply_id}",
                                           f"{biz or who} replied to your email", lines, lead_id=lead_id,
                                           link=f"{PORTAL}/leads/{lead_id}")
            except Exception as e:
                print("⚠️ on_reply alert:", e)
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ email_reply_actions.on_reply:", e)
    # 5. AI Lead Qualification: what the reply means (interested, price, not now,
    #    not interested) — businesses that have it only (30 Oct 2026).
    try:
        import lead_qualify
        done["ai"] = lead_qualify.on_reply(tenant_id, lead_id, reply_id, reply_text, (owner_key, owner_label))
    except Exception as e:
        print("⚠️ email_reply_actions AI qualify:", e)
    return done
