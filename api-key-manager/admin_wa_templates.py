"""Admin › WhatsApp Templates (13 Nov 2026).

View-only list of every WhatsApp message template Meta holds, for PhiXtra's
own numbers and for every business with a real WhatsApp connection, with its
approval status. Loaded live from Meta (cached 10 min per WhatsApp account;
"Refresh from Meta" skips the cache). Nothing is created, edited or deleted.
"""
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg2.extras
import requests
from markupsafe import Markup, escape

from db import get_db_connection
import accounts_core as A

GRAPH = "https://graph.facebook.com/v19.0"
FIELDS = "id,name,status,category,language,components,rejected_reason"
CACHE_MIN = 10

# Meta statuses → the four groups the page filters and counts by.
GROUP = {"APPROVED": "Approved", "PENDING": "Pending", "IN_APPEAL": "Pending",
         "REJECTED": "Rejected", "PAUSED": "Paused", "DISABLED": "Paused",
         "LIMIT_EXCEEDED": "Paused", "PENDING_DELETION": "Paused"}
GROUPS = ["Approved", "Pending", "Rejected", "Paused"]
CATEGORIES = ["Marketing", "Utility", "Authentication"]

STATUS_LABEL = {"APPROVED": "Approved", "PENDING": "Waiting for Meta", "REJECTED": "Rejected",
                "PAUSED": "Paused by Meta", "DISABLED": "Disabled by Meta", "IN_APPEAL": "Appeal in progress",
                "PENDING_DELETION": "Being deleted", "LIMIT_EXCEEDED": "Limit reached"}
REJECT_LABEL = {"INVALID_FORMAT": "The layout or blanks break Meta's rules",
                "PROMOTIONAL": "Meta sees it as marketing",
                "TAG_CONTENT_MISMATCH": "The category doesn't match the wording",
                "INCORRECT_CATEGORY": "The category doesn't match the wording",
                "ABUSIVE_CONTENT": "Meta judged the wording unsuitable",
                "SCAM": "Meta judged it could mislead people"}

CANT_READ = "Meta won't let PhiXtra read this WhatsApp account. The business may need to reconnect WhatsApp."

_cache = {}            # waba_id -> {"at": datetime, "data": [...] or None, "error": str}
_lock = threading.Lock()


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def accounts() -> list:
    """One entry per WhatsApp account (WABA) per business, real Meta tokens
    only (demo/test rows are skipped). PhiXtra's own business comes first."""
    pid = A.platform_tenant_id()
    conn, cur = _db()
    try:
        cur.execute("""
            SELECT w.tenant_id, t.name AS business, w.waba_id, w.display_phone_number, w.access_token
              FROM wa_tenants w JOIN tenants t ON t.id = w.tenant_id
             WHERE w.active AND w.access_token LIKE 'EAA%%' AND COALESCE(w.waba_id,'') <> ''
             ORDER BY w.tenant_id, w.id""")
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    out, seen = [], {}
    for r in rows:
        key = (r["tenant_id"], r["waba_id"])
        phone = (r["display_phone_number"] or "").strip()
        if phone and phone[0].isdigit():
            phone = "+" + phone
        if key in seen:
            if phone and phone not in seen[key]["phones"]:
                seen[key]["phones"].append(phone)
            continue
        acc = {"tenant_id": r["tenant_id"], "business": r["business"] or f"Business #{r['tenant_id']}",
               "waba_id": r["waba_id"], "token": r["access_token"], "phones": [phone] if phone else [],
               "is_platform": pid is not None and r["tenant_id"] == pid}
        if acc["is_platform"]:
            acc["business"] = "PhiXtra (your numbers)"
        seen[key] = acc
        out.append(acc)
    out.sort(key=lambda a: (not a["is_platform"], a["business"].lower()))
    return out


def _fetch(acc: dict, refresh: bool) -> dict:
    now = datetime.now(timezone.utc)
    with _lock:
        hit = _cache.get(acc["waba_id"])
    if hit and not refresh and now - hit["at"] < timedelta(minutes=CACHE_MIN):
        return hit
    data, err, url = [], "", f"{GRAPH}/{acc['waba_id']}/message_templates"
    params = {"fields": FIELDS, "limit": 200}
    try:
        while url:
            r = requests.get(url, params=params, timeout=12,
                             headers={"Authorization": f"Bearer {acc['token']}"}).json()
            if "error" in r:
                err = r["error"].get("message") or "Meta returned an error."
                data = None
                break
            data.extend(r.get("data", []))
            url = (r.get("paging") or {}).get("next")
            params = None   # the next link already carries them
    except Exception as e:
        print("⚠️ admin_wa_templates fetch:", acc["waba_id"], e)
        data, err = None, "Could not reach Meta. Try Refresh in a minute."
    res = {"at": now, "data": data, "error": err}
    with _lock:
        _cache[acc["waba_id"]] = res
    return res


def _fmt(text: str, examples: list) -> Markup:
    """Escape, show {{n}} blanks as chips (with the example as a tooltip),
    and WhatsApp *bold*."""
    s = str(escape(text or ""))
    def blank(m):
        i = int(m.group(1))
        ex = examples[i - 1] if examples and 0 < i <= len(examples) else ""
        return f'<span class="wt-var" title="Example: {escape(ex)}">{{{{{i}}}}}</span>'
    s = re.sub(r"\{\{(\d+)\}\}", blank, s)
    s = re.sub(r"\*([^*\n]+)\*", r"<strong>\1</strong>", s)
    return Markup(s)


def _preview(components: list) -> dict:
    p = {"header": None, "media": None, "body": None, "footer": None, "buttons": [], "examples": []}
    for c in components or []:
        t = c.get("type")
        if t == "HEADER":
            if c.get("format") == "TEXT":
                ex = ((c.get("example") or {}).get("header_text") or [])
                p["header"] = _fmt(c.get("text"), ex)
            else:
                p["media"] = (c.get("format") or "media").title()
        elif t == "BODY":
            ex = ((c.get("example") or {}).get("body_text") or [[]])[0]
            p["examples"] = ex
            p["body"] = _fmt(c.get("text"), ex)
        elif t == "FOOTER":
            p["footer"] = c.get("text")
        elif t == "BUTTONS":
            p["buttons"] = [b.get("text") or b.get("type", "").title() for b in c.get("buttons", [])]
    return p


def _creators(tenant_id: int, waba_id: str) -> dict:
    conn, cur = _db()
    try:
        cur.execute("""SELECT meta_template_id, submitted_by_label, created_at FROM wa_template_submissions
                        WHERE tenant_id=%s AND waba_id=%s AND deleted_at IS NULL""", (tenant_id, waba_id))
        return {r["meta_template_id"]: r for r in cur.fetchall() or []}
    except Exception as e:
        print("⚠️ admin_wa_templates creators:", e)
        return {}
    finally:
        cur.close(); conn.close()


def _counts(templates: list) -> dict:
    c = {g: 0 for g in GROUPS}
    for t in templates:
        c[t["group"]] = c.get(t["group"], 0) + 1
    return c


def templates_for(acc: dict, refresh: bool = False) -> dict:
    """Everything the page needs for one WhatsApp account."""
    res = _fetch(acc, refresh)
    out = {"acc": acc, "loaded_at": res["at"], "error": "", "meta_error": "", "templates": []}
    if res["data"] is None:
        out["error"] = CANT_READ if "Unsupported get request" in res["error"] or "permission" in res["error"].lower() \
            else "Couldn't load templates from Meta."
        out["meta_error"] = res["error"]
        return out
    who = _creators(acc["tenant_id"], acc["waba_id"])
    for t in res["data"]:
        st = t.get("status") or ""
        reason = (t.get("rejected_reason") or "").upper()
        w = who.get(t.get("id"))
        out["templates"].append({
            "id": t.get("id"), "name": t.get("name") or "", "language": t.get("language") or "",
            "category": (t.get("category") or "").title(),
            "status": st, "status_label": STATUS_LABEL.get(st, st.title()), "group": GROUP.get(st, "Paused"),
            "reason": (REJECT_LABEL.get(reason) or reason.replace("_", " ").capitalize()) if reason and reason != "NONE" else "",
            "preview": _preview(t.get("components")),
            "by": (w["submitted_by_label"] + (" · " + w["created_at"].strftime("%-d %b %Y") if w.get("created_at") else ""))
                  if w else "Not recorded (made outside the portal)",
        })
    out["templates"].sort(key=lambda t: (GROUPS.index(t["group"]), t["name"]))
    out["counts"] = _counts(out["templates"])
    return out


def summary(accs: list, refresh: bool = False) -> list:
    """All businesses view: one row per WhatsApp account with status counts.
    Meta is asked for every account at once (in parallel)."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda a: templates_for(a, refresh), accs))
    return results


# ── Delete (13 Nov 2026) ─────────────────────────────────────────────────────
# Only on PhiXtra's own WhatsApp account. Templates the system sends itself
# are blocked: deleting one breaks that feature at once, and Meta won't let
# the same name be used again for 30 days.

def protected_names() -> dict:
    """{template name: what PhiXtra uses it for}."""
    import os
    out = {os.getenv("WA_OTP_TEMPLATE", "phixtra_login_code"): "WhatsApp login codes"}
    try:
        import admin_accounts
        for t in admin_accounts.AM_TEMPLATES:
            out[t["name"]] = "Account Manager messages"
    except Exception as e:
        print("⚠️ admin_wa_templates AM names:", e)
    pid = A.platform_tenant_id()
    if pid is not None:
        labels = {"staff_chat_alert": "staff alerts for waiting chats",
                  "staff_chat_reminder": "staff reminders for waiting chats",
                  "daily_report": "the daily report", "handoff_alert": "hand-over alerts"}
        conn, cur = _db()
        try:
            cur.execute("SELECT template_type, template_name FROM wa_templates WHERE tenant_id=%s AND active", (pid,))
            for r in cur.fetchall() or []:
                out.setdefault(r["template_name"], labels.get(r["template_type"], r["template_type"].replace("_", " ")))
        finally:
            cur.close(); conn.close()
    return out


def delete_template(acc: dict, name: str, tpl_id: str) -> tuple:
    """Delete one template (that exact language version) from Meta.
    Returns (ok, message in plain words)."""
    if not acc.get("is_platform"):
        return False, "Templates can only be deleted on PhiXtra's own WhatsApp account."
    if name in protected_names():
        return False, f"{name} is used by PhiXtra ({protected_names()[name]}), so it can't be deleted here."
    current = _fetch(acc, refresh=True)
    if current["data"] is None:
        return False, "Couldn't reach Meta to check the template. Try again in a minute."
    if not any(t.get("id") == tpl_id and t.get("name") == name for t in current["data"]):
        return False, f"{name} isn't on Meta any more. The list has been refreshed."
    try:
        r = requests.delete(f"{GRAPH}/{acc['waba_id']}/message_templates",
                            params={"name": name, "hsm_id": tpl_id}, timeout=12,
                            headers={"Authorization": f"Bearer {acc['token']}"}).json()
    except Exception as e:
        print("⚠️ admin_wa_templates delete:", e)
        return False, "Couldn't reach Meta. Nothing was deleted. Try again in a minute."
    with _lock:
        _cache.pop(acc["waba_id"], None)
    if r.get("success"):
        return True, f"{name} was deleted from Meta."
    e = r.get("error") or {}
    return False, "Meta didn't delete it: " + (e.get("error_user_msg") or e.get("message") or "no reason given.")
