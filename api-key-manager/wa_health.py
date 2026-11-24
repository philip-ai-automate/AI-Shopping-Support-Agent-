"""
wa_health.py: WhatsApp account health (24 Nov 2026).
Approved mock-up: https://claude.ai/artifact/ExrN88mpMbfXub6uaghyL5

Every connected WhatsApp number is checked with Meta:
  - at once, when Meta sends a warning (stored by the WhatsApp gateway in
    wa_meta_events, see whatsapp-gateway/meta_health_events.py),
  - every day at 07:00 UK time,
  - every hour for businesses the owner ticked "High-risk business".
The check only READS Meta (number status, quality rating, health, review and
whether PhiXtra still has permission). Nothing in the business's account is changed.

States: red (banned, locked, disconnected, disconnected from PhiXtra in Meta, quality Red),
yellow (quality warning or limited), green (working).
  Red    → timeline entry + support request opens + staff alerted (Support alerts)
  Yellow → timeline entry + Account Manager alerted, no support request
  Green  → timeline entry only (after a problem)
The business sees it in the portal: popup at log-in, coloured bar, Dashboard
card and Settings › WhatsApp Connect. Staff see it in Admin › Accounts.

  python wa_health.py --check [tenant_id]   check now (all, or one business)
  python wa_health.py --dry-run --check     check and print, no alerts or requests
"""
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import psycopg2.extras
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_db_connection, insert_audit_log   # noqa: E402

try:
    from zoneinfo import ZoneInfo
    _UK = ZoneInfo("Europe/London")
except Exception:          # pragma: no cover
    _UK = timezone.utc

GRAPH = "https://graph.facebook.com/v21.0"
DAILY_HOUR_UK = 7
WATCH_EVERY_MIN = 60
DRY = os.getenv("WA_HEALTH_DRY") == "1"
# Meta health errors that are not a problem for the business:
# calling over SIP not set up, business not verified (normal for small businesses).
IGNORE_CODES = {138024, 138025, 141010}

# kind → (state, staff label, business headline, business sentence)
KINDS = {
    "banned": ("red", "Banned by Meta", "Meta has banned your WhatsApp Business account",
               "Customers who message you on WhatsApp get no reply, so their enquiries are being missed. "
               "Only Meta can lift the ban."),
    "locked": ("red", "Locked by Meta", "Meta has locked your WhatsApp Business account",
               "Customers who message you on WhatsApp get no reply, so their enquiries are being missed. "
               "Only Meta can unlock it."),
    "blocked": ("red", "Blocked by Meta", "Meta has stopped your WhatsApp messages",
                "Customers who message you on WhatsApp get no reply, so their enquiries are being missed. "
                "Meta's reason is shown in Meta Business Suite › Account Quality."),
    "disconnected": ("red", "Number disconnected", "Your WhatsApp number is disconnected",
                     "Customers who message you on WhatsApp get no reply. Connect your number again to bring them back."),
    "lost_access": ("red", "Disconnected from your PhiXtra AI Agent",
                    "Your WhatsApp enquiries aren't reaching your PhiXtra AI Agent",
                    "Your WhatsApp number was disconnected from PhiXtra in Meta, so new customer enquiries don't "
                    "arrive in your Inbox and your AI Agent can't reply to them. Connect WhatsApp again to "
                    "start receiving them."),
    "quality_red": ("red", "Quality Red", "Meta rates your customer messages Red",
                    "Many customers have blocked or reported your messages. Meta can now limit how many customers "
                    "you can message, or lock the account."),
    "quality_yellow": ("yellow", "Quality warning (Yellow)", "Meta has warned you about message quality",
                       "WhatsApp still works and customers can reach you, but recently more people have blocked or "
                       "reported your messages. If it drops to Red, Meta can limit how many customers you can "
                       "message, or lock the account."),
    "limited": ("yellow", "Limited by Meta", "Meta has limited your WhatsApp messages",
                "WhatsApp still works, but Meta has limited what your number can send."),
    "ok": ("green", "Working", "Your WhatsApp is working", "Customers can reach you on WhatsApp."),
}
# kind → how staff read it in a sentence ("WhatsApp banned by Meta")
PHRASE = {"banned": "banned by Meta", "locked": "locked by Meta", "blocked": "blocked by Meta", "disconnected": "number disconnected",
          "lost_access": "disconnected from PhiXtra", "quality_red": "quality Red", "quality_yellow": "quality warning",
          "limited": "limited by Meta", "ok": "working"}
RANK = {"red": 3, "yellow": 2, "green": 1, "unknown": 0}
FIELD_LABEL = {"account_update": "Account update", "account_alerts": "Account alert",
               "account_review_update": "Account review", "phone_number_quality_update": "Quality update",
               "phone_number_name_update": "Display name", "business_capability_update": "Messaging limit",
               "security": "Security"}

_ready = False


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_tables():
    global _ready
    if _ready:
        return
    conn, cur = _db()
    try:
        cur.execute("SET lock_timeout = '3s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS wa_health (
            phone_number_id VARCHAR(40) PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            display_number VARCHAR(40),
            state VARCHAR(10) NOT NULL DEFAULT 'unknown',
            kind VARCHAR(20) NOT NULL DEFAULT 'ok',
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            meta_words TEXT,
            since TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            cleared_at TIMESTAMPTZ,
            checked_at TIMESTAMPTZ,
            check_error TEXT)""")
        cur.execute("CREATE INDEX IF NOT EXISTS wa_health_tenant ON wa_health (tenant_id)")
        cur.execute("""CREATE TABLE IF NOT EXISTS wa_health_watch (
            tenant_id INTEGER PRIMARY KEY, set_by VARCHAR(200), set_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS wa_health_meta (
            key VARCHAR(40) PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS wa_policy_ack (
            id BIGSERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL, customer_id INTEGER, team_member_id INTEGER,
            acked_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS am_ready_texts (
            key VARCHAR(40) PRIMARY KEY, label VARCHAR(120) NOT NULL, body TEXT NOT NULL,
            updated_by VARCHAR(200), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.commit()
        _ready = True
    finally:
        cur.close(); conn.close()


def uk_day_time(dt) -> str:
    if not dt:
        return ""
    return dt.astimezone(_UK).strftime("%-d %b, %H:%M")


def digits(s) -> str:
    return re.sub(r"\D", "", s or "")


# ── Checking Meta ─────────────────────────────────────────────────────────

def numbers(tenant_id=None) -> list:
    """Connected WhatsApp numbers with a real Meta token."""
    conn, cur = _db()
    try:
        cur.execute(f"""SELECT w.tenant_id, w.phone_number_id, w.waba_id, w.display_phone_number, w.access_token
                          FROM wa_tenants w JOIN tenants t ON t.id = w.tenant_id
                         WHERE w.active AND w.access_token LIKE 'EAA%%' AND COALESCE(w.phone_number_id,'') <> ''
                           {'AND w.tenant_id = %s' if tenant_id else ''}
                         ORDER BY w.tenant_id""", ((tenant_id,) if tenant_id else ()))
        return cur.fetchall() or []
    finally:
        cur.close(); conn.close()


def _get(path, token, fields):
    try:
        return requests.get(f"{GRAPH}/{path}", params={"fields": fields},
                            headers={"Authorization": f"Bearer {token}"}, timeout=20).json()
    except Exception as e:
        return {"error": {"code": -1, "message": str(e)[:200]}}


def _token_has_whatsapp(token) -> bool | None:
    """True/False from Meta's token check; None if it can't be checked."""
    app = (os.getenv("META_APP_ID") or "") + "|" + (os.getenv("META_APP_SECRET") or os.getenv("FB_APP_SECRET") or "")
    if app.startswith("|") or app.endswith("|"):
        return None
    try:
        d = requests.get(f"{GRAPH}/debug_token", params={"input_token": token, "access_token": app},
                         timeout=20).json().get("data") or {}
    except Exception:
        return None
    if not d:
        return None
    if not d.get("is_valid"):
        return False
    return "whatsapp_business_messaging" in (d.get("scopes") or [])


def check(n: dict) -> dict:
    """{kind, details, meta_words} or {error} when Meta couldn't be asked."""
    tok = n["access_token"]
    ph = _get(n["phone_number_id"], tok, "display_phone_number,verified_name,quality_rating,status,name_status,"
                                         "messaging_limit_tier,health_status")
    if "error" in ph:
        code = (ph["error"] or {}).get("code")
        if code in (10, 100, 190, 200):
            has = _token_has_whatsapp(tok)
            if has is False:
                return {"kind": "lost_access", "details": {"access": False}, "meta_words": ""}   # Meta's error here is technical
        return {"error": f"{code}: {(ph['error'].get('message') or '')[:200]}"}
    wb = _get(n["waba_id"], tok, "account_review_status,business_verification_status,health_status") if n["waba_id"] else {}
    if "error" in wb:
        wb = {}
    status = (ph.get("status") or "").upper()
    quality = (ph.get("quality_rating") or "").upper()
    hs = ph.get("health_status") or {}
    overall = (hs.get("can_send_message") or "").upper()
    errs = []
    for ent in (hs.get("entities") or []) + ((wb.get("health_status") or {}).get("entities") or []):
        for x in ent.get("errors") or []:
            errs.append({"entity": ent.get("entity_type"), "can_send": ent.get("can_send_message"),
                         "code": x.get("error_code"), "words": x.get("error_description") or ""})
    real = [e for e in errs if e["code"] not in IGNORE_CODES and e["can_send"] in ("BLOCKED", "LIMITED")]
    codes = {e["code"] for e in errs}
    if status == "BANNED" or 141014 in codes:
        kind = "banned"
    elif status == "RESTRICTED":
        kind = "locked"
    elif overall == "BLOCKED":
        kind = "blocked"        # Meta stopped sending for another reason (e.g. payment); Meta's words say why
    elif status in ("DISCONNECTED", "DELETED"):
        kind = "disconnected"
    elif quality == "RED":
        kind = "quality_red"
    elif quality == "YELLOW" or status == "FLAGGED":
        kind = "quality_yellow"
    elif overall == "LIMITED" and real:
        kind = "limited"
    else:
        kind = "ok"
    details = {"status": status, "quality": quality or "UNKNOWN", "can_send": overall,
               "name": ph.get("verified_name") or "", "name_status": ph.get("name_status") or "",
               "limit": ph.get("messaging_limit_tier") or "", "review": wb.get("account_review_status") or "",
               "verified": wb.get("business_verification_status") or "", "access": True,
               "errors": [e for e in errs if e["code"] not in IGNORE_CODES][:6]}
    words = (real[0]["words"] if real else "")[:300]
    return {"kind": kind, "details": details, "meta_words": words}


# ── Saving + what happens on a change ─────────────────────────────────────

def _log(cur, tenant_id, subject, body):
    import data_crypto
    cur.execute("""INSERT INTO am_activity (tenant_id, kind, subject, body, by_admin, ok)
                   VALUES (%s,'problem',%s,%s,NULL,TRUE)""", (tenant_id, subject, data_crypto.enc(body)))


def sentence(row: dict) -> str:
    """One plain sentence for staff (used by support requests and Ask PhiXtra)."""
    st, label, head, text = KINDS.get(row["kind"], KINDS["ok"])
    s = (f"WhatsApp {label.lower() if row['kind'] != 'lost_access' else 'disconnected from PhiXtra'}: "
         f"{row.get('display_number') or 'their number'}, found by PhiXtra {uk_day_time(row['since'])} UK time.")
    if row["kind"] in ("lost_access", "disconnected"):
        s += " New customer enquiries on WhatsApp don't arrive in their PhiXtra Inbox, so nobody replies to them."
    else:
        s += {"red": " Customers can't reach them on WhatsApp.", "yellow": " WhatsApp still works, but Meta has warned them."}.get(st, "")
    if row.get("meta_words"):
        s += f" Meta's words: \"{row['meta_words']}\""
    return s


def check_and_save(n: dict, why: str = "daily check") -> dict | None:
    ensure_tables()
    res = check(n)
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM wa_health WHERE phone_number_id=%s", (n["phone_number_id"],))
        prev = cur.fetchone()
        if "error" in res:
            cur.execute("""INSERT INTO wa_health (phone_number_id, tenant_id, display_number, checked_at, check_error)
                           VALUES (%s,%s,%s,NOW(),%s) ON CONFLICT (phone_number_id)
                           DO UPDATE SET checked_at=NOW(), check_error=EXCLUDED.check_error""",
                        (n["phone_number_id"], n["tenant_id"], n["display_phone_number"], res["error"]))
            conn.commit()
            print(f"⚠️ wa_health {n['tenant_id']} {n['phone_number_id']}: could not ask Meta: {res['error']}")
            return None
        kind = res["kind"]
        state = KINDS[kind][0]
        changed = not prev or prev["kind"] != kind or prev["state"] != state
        now = datetime.now(timezone.utc)
        if changed:
            cleared = now if (state == "green" and prev and prev["state"] in ("red", "yellow")) else None
            cur.execute("""INSERT INTO wa_health (phone_number_id, tenant_id, display_number, state, kind, details,
                                                  meta_words, since, cleared_at, checked_at, check_error)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),%s,NOW(),NULL)
                           ON CONFLICT (phone_number_id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id,
                             display_number=EXCLUDED.display_number, state=EXCLUDED.state, kind=EXCLUDED.kind,
                             details=EXCLUDED.details, meta_words=EXCLUDED.meta_words, since=NOW(),
                             cleared_at=EXCLUDED.cleared_at, checked_at=NOW(), check_error=NULL""",
                        (n["phone_number_id"], n["tenant_id"], n["display_phone_number"], state, kind,
                         json.dumps(res["details"]), res["meta_words"] or None, cleared))
        else:
            cur.execute("""UPDATE wa_health SET details=%s, meta_words=%s, checked_at=NOW(), check_error=NULL,
                             display_number=%s WHERE phone_number_id=%s""",
                        (json.dumps(res["details"]), res["meta_words"] or None, n["display_phone_number"],
                         n["phone_number_id"]))
        cur.execute("SELECT * FROM wa_health WHERE phone_number_id=%s", (n["phone_number_id"],))
        row = cur.fetchone()
        if changed and (prev or state != "green"):
            if state in ("red", "yellow"):
                _log(cur, n["tenant_id"], f"WhatsApp problem: {KINDS[kind][1]}",
                     f"Found by the {why}. " + sentence(row))
            elif prev and prev["state"] in ("red", "yellow"):
                _log(cur, n["tenant_id"], "WhatsApp working again",
                     f"Found by the {why}: WhatsApp {n['display_phone_number'] or ''} is working again "
                     f"(was: {KINDS.get(prev['kind'], KINDS['ok'])[1]}).")
        conn.commit()
    finally:
        cur.close(); conn.close()
    if changed:
        old_state = prev["state"] if prev else "unknown"
        print(f"🩺 wa_health {n['tenant_id']} {n['display_phone_number']}: {old_state} → {state} ({kind})")
        try:
            _on_change(row, old_state)
        except Exception as e:
            print("⚠️ wa_health alert:", e)
    return row


def _on_change(row: dict, old_state: str):
    """Red: support request + staff alerts. Yellow: Account Manager alerted. Green: nothing."""
    new = row["state"]
    if new == "red" and old_state != "red":
        if DRY:
            print(f"   DRY: would open a support request + alert staff for tenant {row['tenant_id']}")
            return
        import support_requests as SR
        req, created = SR.create_auto(row["tenant_id"], sentence(row))
        if created:
            try:
                SR.send_alerts(req, "first")
            except Exception as e:
                print("⚠️ wa_health alerts:", e)
        _email(row, req)
    elif new == "yellow" and old_state in ("green", "unknown"):
        if DRY:
            print(f"   DRY: would alert the Account Manager for tenant {row['tenant_id']}")
            return
        import support_requests as SR
        who = SR.recipients({"tenant_id": row["tenant_id"]})
        SR._push([p["username"] for p in who], f"{SR._business(row['tenant_id'])}: WhatsApp warning",
                 KINDS[row["kind"]][1] + " · " + KINDS[row["kind"]][3][:120],
                 f"{SR.PORTAL}/admin/accounts/{row['tenant_id']}", f"wa-health-{row['tenant_id']}")
        _email(row, None)


def _email(row: dict, req):
    """Email support@ and the Account Manager."""
    from portal_utils import send_email as _send
    import admin_accounts as M
    import support_requests as SR
    e = html.escape
    business = SR._business(row["tenant_id"])
    conn, cur = _db()
    try:
        cur.execute("SELECT assigned_admin FROM am_account_state WHERE tenant_id=%s", (row["tenant_id"],))
        am = (cur.fetchone() or {}).get("assigned_admin")
    finally:
        cur.close(); conn.close()
    state, label, head, text = KINDS[row["kind"]]
    colour = "#B42318" if state == "red" else "#B54708"
    url = f"{SR.PORTAL}/admin/accounts/{row['tenant_id']}" + ("#support" if req else "")
    what = ("A support request has been opened." if req else
            "No support request is opened for a warning. Contact them before it gets worse.")
    body = f"""<div style="font-family:Arial,sans-serif;font-size:14px;color:#030C18;max-width:640px">
<div style="background:{colour};color:#fff;padding:14px 18px;border-radius:10px 10px 0 0;font-size:17px;font-weight:bold">WhatsApp {'problem' if state == 'red' else 'warning'}: {e(business)} · {e(label)}</div>
<div style="border:1px solid #E4E7EC;border-top:0;border-radius:0 0 10px 10px;padding:16px 18px">
<p style="margin:0 0 10px">{e(sentence(row))}</p>
<p style="margin:0 0 10px">{e(what)} The business sees it in their portal (pop-up at log-in, coloured bar, Dashboard).</p>
<p style="margin:16px 0 6px"><a href="{url}" style="display:inline-block;background:#030C18;color:#fff;padding:11px 18px;border-radius:8px;text-decoration:none;font-weight:bold">Open their account in Admin</a></p>
<p style="font-size:12.5px;color:#5D6673">Account Manager: {e(M.manager_name(am) if am else 'Nobody yet')}. Found by PhiXtra's WhatsApp health check.</p>
</div></div>"""
    subject = f"WhatsApp {'problem' if state == 'red' else 'warning'}: {business} · {label}"
    _send(SR.SUPPORT_EMAIL, subject, body)
    if am and "@" in am and am.lower() != SR.SUPPORT_EMAIL.lower():
        _send(am, subject, body)


# ── Meta's warnings (webhooks) + the schedule ─────────────────────────────

def process_events() -> int:
    """Meta warnings stored by the WhatsApp gateway: check those numbers now."""
    conn, cur = _db()
    try:
        cur.execute("SELECT to_regclass('wa_meta_events') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            return 0
        cur.execute("""SELECT * FROM wa_meta_events WHERE processed_at IS NULL ORDER BY id LIMIT 50""")
        evs = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    done = 0
    for ev in evs:
        try:
            _process_one(ev)
        except Exception as e:
            print(f"⚠️ wa_health event {ev['id']}:", e)
        finally:
            conn, cur = _db()
            try:
                cur.execute("UPDATE wa_meta_events SET processed_at=NOW() WHERE id=%s", (ev["id"],))
                conn.commit()
            finally:
                cur.close(); conn.close()
        done += 1
    return done


def _process_one(ev):
    """One stored Meta warning: note it on the timeline (signed only) and check those numbers now."""
    targets = [n for n in numbers()
               if (ev["phone_number_id"] and n["phone_number_id"] == ev["phone_number_id"])
               or (ev["waba_id"] and n["waba_id"] == ev["waba_id"])
               or (ev["display_phone"] and digits(n["display_phone_number"]) == digits(ev["display_phone"]))]
    for n in targets:
        if ev["sig_ok"]:
            v = ev["value"] or {}
            what = (v.get("event") or v.get("decision") or v.get("alert_type") or v.get("current_limit") or "")
            conn, cur = _db()
            try:
                _log(cur, n["tenant_id"], f"From Meta: {FIELD_LABEL.get(ev['field'], ev['field'])}"
                                          + (f" ({str(what)[:60]})" if what else ""),
                     (v.get("alert_description") or v.get("reason") or json.dumps(v)[:400]))
                conn.commit()
            finally:
                cur.close(); conn.close()
        else:
            # unsigned: at most one extra check per number every 10 minutes
            conn, cur = _db()
            try:
                cur.execute("""SELECT 1 FROM wa_health WHERE phone_number_id=%s
                                AND checked_at > NOW() - INTERVAL '10 minutes'""", (n["phone_number_id"],))
                recent = cur.fetchone()
            finally:
                cur.close(); conn.close()
            if recent:
                continue
        check_and_save(n, "warning from Meta")


def _meta_get(key):
    conn, cur = _db()
    try:
        cur.execute("SELECT value FROM wa_health_meta WHERE key=%s", (key,))
        r = cur.fetchone()
        return r["value"] if r else None
    finally:
        cur.close(); conn.close()


def _meta_set(key, value):
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO wa_health_meta (key, value) VALUES (%s,%s)
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""", (key, value))
        conn.commit()
    finally:
        cur.close(); conn.close()


def check_all(tenant_id=None, why="daily check") -> int:
    n_done = 0
    for n in numbers(tenant_id):
        try:
            check_and_save(n, why)
            n_done += 1
        except Exception as e:
            print(f"⚠️ wa_health check {n['tenant_id']}:", e)
    return n_done


def tick():
    """Every minute (am_reply_alerts.py): Meta warnings, the 07:00 daily check,
    hourly checks for high-risk businesses."""
    ensure_tables()
    process_events()
    now_uk = datetime.now(_UK)
    today = now_uk.strftime("%Y-%m-%d")
    if now_uk.hour >= DAILY_HOUR_UK and _meta_get("daily_run") != today:
        _meta_set("daily_run", today)
        print(f"🩺 wa_health daily check: {check_all()} number(s)")
    conn, cur = _db()
    try:
        cur.execute("""SELECT w.tenant_id FROM wa_health_watch w
                        WHERE NOT EXISTS (SELECT 1 FROM wa_health h WHERE h.tenant_id = w.tenant_id
                                            AND h.checked_at > NOW() - (%s || ' minutes')::interval)""",
                    (str(WATCH_EVERY_MIN),))
        due = [r["tenant_id"] for r in cur.fetchall() or []]
    finally:
        cur.close(); conn.close()
    for tid in due:
        check_all(tid, "hourly check (high-risk business)")


# ── What the screens show ─────────────────────────────────────────────────

def for_tenant(tenant_id: int) -> list:
    try:
        ensure_tables()
        conn, cur = _db()
        try:
            cur.execute("""SELECT h.* FROM wa_health h WHERE h.tenant_id=%s
                             AND EXISTS (SELECT 1 FROM wa_tenants w WHERE w.phone_number_id = h.phone_number_id
                                           AND w.tenant_id = h.tenant_id AND w.active)
                           ORDER BY h.phone_number_id""", (tenant_id,))
            rows = cur.fetchall() or []
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ wa_health.for_tenant:", e)
        return []
    for r in rows:
        st, r["label"], r["head"], r["text"] = KINDS.get(r["kind"], KINDS["ok"])
        r["phrase"] = PHRASE.get(r["kind"], "")
    return rows


def worst(tenant_id: int):
    rows = [r for r in for_tenant(tenant_id) if r["state"] != "unknown"]
    return max(rows, key=lambda r: (RANK.get(r["state"], 0), r["since"])) if rows else None


def problem_rows(tenant_ids=None) -> dict:
    """{tenant_id: worst red/yellow row} for lists and counts."""
    try:
        ensure_tables()
        conn, cur = _db()
        try:
            cur.execute("""SELECT h.* FROM wa_health h WHERE h.state IN ('red','yellow')
                             AND EXISTS (SELECT 1 FROM wa_tenants w WHERE w.phone_number_id = h.phone_number_id
                                           AND w.tenant_id = h.tenant_id AND w.active)""")
            rows = cur.fetchall() or []
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ wa_health.problem_rows:", e)
        return {}
    out = {}
    for r in rows:
        st, r["label"], r["head"], r["text"] = KINDS.get(r["kind"], KINDS["ok"])
        r["phrase"] = PHRASE.get(r["kind"], "")
        cur_ = out.get(r["tenant_id"])
        if not cur_ or RANK[r["state"]] > RANK[cur_["state"]]:
            out[r["tenant_id"]] = r
    return out


def business_view(tenant_id: int) -> dict | None:
    """For the business's portal: {state, kind, head, text, since, number, green_until}."""
    w = worst(tenant_id)
    if not w:
        return None
    v = {"state": w["state"], "kind": w["kind"], "head": w["head"], "text": w["text"], "label": w["label"],
         "since": uk_day_time(w["since"]), "number": w["display_number"] or "",
         "quality": (w["details"] or {}).get("quality") or "", "checked": uk_day_time(w["checked_at"]),
         "key": f"{w['phone_number_id']}:{w['kind']}:{int(w['since'].timestamp())}", "green_recent": False}
    if w["state"] == "green" and w["cleared_at"] and datetime.now(timezone.utc) - w["cleared_at"] < timedelta(days=1):
        v["green_recent"] = True
        v["key"] = f"{w['phone_number_id']}:green:{int(w['cleared_at'].timestamp())}"
    return v


def is_watched(tenant_id: int) -> dict | None:
    try:
        ensure_tables()
        conn, cur = _db()
        try:
            cur.execute("SELECT * FROM wa_health_watch WHERE tenant_id=%s", (tenant_id,))
            return cur.fetchone()
        finally:
            cur.close(); conn.close()
    except Exception:
        return None


def set_watch(tenant_id: int, on: bool, by: str):
    ensure_tables()
    conn, cur = _db()
    try:
        if on:
            cur.execute("""INSERT INTO wa_health_watch (tenant_id, set_by) VALUES (%s,%s)
                           ON CONFLICT (tenant_id) DO UPDATE SET set_by=EXCLUDED.set_by, set_at=NOW()""",
                        (tenant_id, by))
        else:
            cur.execute("DELETE FROM wa_health_watch WHERE tenant_id=%s", (tenant_id,))
        import admin_accounts as M
        M.log(cur, tenant_id, "note", by, subject=None,
              body=("Marked High-risk business: WhatsApp health checked every hour."
                    if on else "High-risk business mark removed: WhatsApp health checked daily."))
        conn.commit()
    finally:
        cur.close(); conn.close()
    insert_audit_log(admin_username=by, action="wa_health_watch_" + ("on" if on else "off"), tenant_id=tenant_id)


def wa_unreachable(tenant_id: int, phone) -> bool:
    """The contact phone IS a WhatsApp number with a red problem: a WhatsApp reply won't reach them."""
    d = digits(phone)
    if len(d) < 8:
        return False
    return any(r["state"] == "red" and digits(r["display_number"])[-9:] == d[-9:] for r in for_tenant(tenant_id))


def ready_texts(keys=None) -> list:
    """[(key, label, body)] written in Admin › Accounts › Settings (am_ready_texts)."""
    try:
        ensure_tables()
        conn, cur = _db()
        try:
            cur.execute("SELECT key, label, body FROM am_ready_texts ORDER BY key")
            rows = cur.fetchall() or []
        finally:
            cur.close(); conn.close()
    except Exception:
        return []
    return [(r["key"], r["label"], r["body"]) for r in rows if not keys or r["key"] in keys]


def policy_ack(tenant_id, customer_id, team_member_id, by_staff=False):
    ensure_tables()
    if by_staff:            # PhiXtra staff viewing the business's portal: not the business's own consent
        return
    conn, cur = _db()
    try:
        cur.execute("SELECT 1 FROM wa_policy_ack WHERE tenant_id=%s LIMIT 1", (tenant_id,))
        first = cur.fetchone() is None
        cur.execute("INSERT INTO wa_policy_ack (tenant_id, customer_id, team_member_id) VALUES (%s,%s,%s)",
                    (tenant_id, customer_id, team_member_id))
        if first:
            import admin_accounts as M
            M.log(cur, tenant_id, "note", None, body="Ticked \"My business follows Meta's rules\" when connecting WhatsApp.")
        conn.commit()
    finally:
        cur.close(); conn.close()


# ── Business portal: bar, popup, Dashboard card, WhatsApp Connect box ────

def register_portal(app):
    from flask import g, jsonify, request, session

    def _tenant():
        if not session.get("portal_logged_in"):
            return None
        if hasattr(g, "_wa_health_tid"):
            return g._wa_health_tid
        tid = None
        cid = session.get("impersonate_customer_id") or session.get("customer_id")
        if cid:
            try:
                conn, cur = _db()
                try:
                    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (int(cid),))
                    tid = (cur.fetchone() or {}).get("tenant_id")
                finally:
                    cur.close(); conn.close()
            except Exception:
                tid = None
        g._wa_health_tid = tid
        return tid

    @app.context_processor
    def _wa_health_ctx():
        def wa_health_ui():
            if hasattr(g, "_wa_health_ui"):
                return g._wa_health_ui
            tid = _tenant()
            g._wa_health_ui = business_view(tid) if tid else None
            return g._wa_health_ui

        def wa_health_popup():
            """True once per log-in (and again for a new problem) while WhatsApp is Red or Yellow."""
            v = wa_health_ui()
            if not v or v["state"] not in ("red", "yellow") or request.method != "GET":
                return False
            if session.get("wa_health_popup") == v["key"]:
                return False
            session["wa_health_popup"] = v["key"]
            return True
        return {"wa_health_ui": wa_health_ui, "wa_health_popup": wa_health_popup}

    @app.route("/whatsapp/meta-rules-ack", methods=["POST"])
    def wa_meta_rules_ack():
        tid = _tenant()
        if not tid:
            return jsonify(ok=False), 401
        policy_ack(tid, session.get("customer_id"), session.get("team_member_id"),
                   by_staff=bool(session.get("impersonate_customer_id")))
        return jsonify(ok=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", nargs="?", const="all")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.dry_run:
        DRY = True
    if a.check:
        print(check_all(None if a.check == "all" else int(a.check), "check"), "number(s) checked")
