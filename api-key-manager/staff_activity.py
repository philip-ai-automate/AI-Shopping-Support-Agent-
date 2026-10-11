"""Who did what — the record behind Team performance (user 2026-10-11).

Every sales action a person takes in the portal (stage moves, Won/Lost,
notes, WhatsApp/Messenger/web chat replies, adding people to email
sequences, sequence emails going out, creating/editing/assigning leads) is
saved here with the person's key ("team:<id>" or "owner:<customer id>") and
name. staff_presence keeps one row per person per day: first seen, last
seen, minutes active and the longest gap.

Logging must never break the action it records: every write uses its own
short autocommit connection, and every error is printed and swallowed.
An admin impersonating a business is not a staff member — nothing is logged.
"""
import time as _time
import threading as _threading

from db import get_db_connection

DAY_TZ = "Africa/Lagos"          # the business day used for "Today" and presence rows
ACTIVE_GAP_MIN = 15              # a gap longer than this is "away", not active time
_PRESENCE_EVERY_S = 60           # write presence at most once a minute per person per worker

_presence_last = {}              # actor key → last presence write (per worker)
_tenant_of_customer = {}         # customer id → tenant id (per worker)
_owner_name = {}                 # customer id → (name, fetched at)
_lock = _threading.Lock()


def ensure_tables(cur) -> None:
    """New tables only — never ALTERs an existing busy table."""
    cur.execute("SET LOCAL lock_timeout = '3s'")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS staff_activity (
            id          BIGSERIAL PRIMARY KEY,
            tenant_id   INTEGER NOT NULL,
            actor_key   VARCHAR(40) NOT NULL,
            actor_name  VARCHAR(200),
            action      VARCHAR(40) NOT NULL,
            lead_id     INTEGER,
            contact_id  INTEGER,
            company_id  INTEGER,
            qty         INTEGER NOT NULL DEFAULT 1,
            value       NUMERIC(14,2),
            detail      TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS staff_activity_tenant_time ON staff_activity (tenant_id, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS staff_activity_actor_time ON staff_activity (tenant_id, actor_key, created_at)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS staff_presence (
            tenant_id        INTEGER NOT NULL,
            actor_key        VARCHAR(40) NOT NULL,
            day              DATE NOT NULL,
            actor_name       VARCHAR(200),
            first_seen       TIMESTAMPTZ NOT NULL,
            last_seen        TIMESTAMPTZ NOT NULL,
            active_minutes   INTEGER NOT NULL DEFAULT 0,
            longest_gap_min  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, actor_key, day)
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS staff_targets (
            tenant_id   INTEGER NOT NULL,
            actor_key   VARCHAR(40) NOT NULL,
            month       DATE NOT NULL,
            opp_target  INTEGER,
            won_target  NUMERIC(14,2),
            set_by      VARCHAR(200),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, actor_key, month)
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lead_calls (
            id          BIGSERIAL PRIMARY KEY,
            tenant_id   INTEGER NOT NULL,
            lead_id     INTEGER NOT NULL,
            actor_key   VARCHAR(40),
            actor_name  VARCHAR(200),
            outcome     VARCHAR(30) NOT NULL,
            minutes     INTEGER,
            note        TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS lead_calls_lead ON lead_calls (tenant_id, lead_id, created_at)")


def _session_actor():
    """(key, name, customer_id) for whoever is logged in, or None outside a
    request, when not logged in, or when an admin is impersonating."""
    try:
        from flask import has_request_context, session
        if not has_request_context() or not session.get("portal_logged_in"):
            return None
        if session.get("impersonate_customer_id"):
            return None
        cid = session.get("customer_id")
        if not cid:
            return None
        tm = session.get("team_member_id")
        if tm:
            return f"team:{int(tm)}", session.get("team_member_name") or "Team member", int(cid)
        return f"owner:{int(cid)}", _owner_display(int(cid)), int(cid)
    except Exception as e:
        print("⚠️ staff_activity._session_actor error:", e)
        return None


def _owner_display(cid: int) -> str:
    hit = _owner_name.get(cid)
    if hit and _time.time() - hit[1] < 600:
        return hit[0]
    name = "Owner"
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT first_name, last_name FROM customers WHERE id=%s", (cid,))
        r = cur.fetchone()
        if r:
            name = f"{(r[0] or '').strip()} {(r[1] or '').strip()}".strip() or "Owner"
        cur.close()
    finally:
        conn.close()
    _owner_name[cid] = (name, _time.time())
    return name


def _tenant_for(cid: int, cur):
    if cid in _tenant_of_customer:
        return _tenant_of_customer[cid]
    cur.execute("SELECT tenant_id FROM customers WHERE id=%s", (cid,))
    r = cur.fetchone()
    tid = int(r[0]) if r and r[0] else None
    if tid:
        _tenant_of_customer[cid] = tid
    return tid


def in_staff_request() -> bool:
    return _session_actor() is not None


def actor_by_name(tenant_id: int, name: str):
    """(key, name) for a person saved only by display name (e.g. who added
    someone to a sequence), or (None, None). Active staff first, then the owner."""
    name = (name or "").strip()
    if not name or not tenant_id:
        return None, None
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""SELECT id, name FROM team_members WHERE tenant_id=%s AND lower(name)=lower(%s)
                           ORDER BY is_active DESC, last_login_at DESC NULLS LAST LIMIT 1""", (tenant_id, name))
            r = cur.fetchone()
            if r:
                return f"team:{r[0]}", r[1]
            cur.execute("""SELECT id FROM customers WHERE tenant_id=%s AND (
                               lower(trim(coalesce(first_name,'') || ' ' || coalesce(last_name,''))) = lower(%s)
                               OR lower(first_name) = lower(%s)) ORDER BY id LIMIT 1""", (tenant_id, name, name))
            r = cur.fetchone()
            if r:
                return f"owner:{r[0]}", name
        finally:
            conn.close()
    except Exception as e:
        print("⚠️ staff_activity.actor_by_name error:", e)
    return None, None


def current_name(default: str = None) -> str:
    """The real person's name for "changed by" style labels (falls back to
    `default` outside a staff request)."""
    a = _session_actor()
    return a[1] if a else default


def log(action: str, tenant_id: int = None, lead_id: int = None, contact_id: int = None,
        company_id: int = None, qty: int = 1, value=None, detail: str = None,
        actor_key: str = None, actor_name: str = None, phone: str = None) -> None:
    """Save one action. Uses the logged-in person unless actor_key is given
    (background jobs acting for someone, e.g. a sequence email going out)."""
    try:
        if not actor_key:
            a = _session_actor()
            if not a:
                return
            actor_key, actor_name, cid = a
        else:
            cid = None
        conn = get_db_connection()
        if not conn:
            return
        try:
            conn.autocommit = True
            cur = conn.cursor()
            if not tenant_id and lead_id:
                cur.execute("SELECT tenant_id FROM merchant_pipeline_leads WHERE id=%s", (lead_id,))
                r = cur.fetchone()
                tenant_id = r[0] if r else None
            if not tenant_id and cid:
                tenant_id = _tenant_for(cid, cur)
            if not tenant_id:
                return
            if not lead_id and not contact_id and phone:
                digits = "".join(ch for ch in str(phone) if ch.isdigit())
                if digits:
                    cur.execute("SELECT id FROM wa_contacts WHERE tenant_id=%s AND whatsapp_number IN (%s,%s) LIMIT 1",
                                (tenant_id, phone, digits))
                    r = cur.fetchone()
                    contact_id = r[0] if r else None
                    detail = detail or digits
            if not lead_id and contact_id:
                # The lead behind this contact, so "leads worked" counts it.
                cur.execute("""SELECT id FROM merchant_pipeline_leads WHERE tenant_id=%s AND wa_contact_id=%s
                               ORDER BY (outcome IS NULL) DESC, id DESC LIMIT 1""", (tenant_id, contact_id))
                r = cur.fetchone()
                lead_id = r[0] if r else None
            cur.execute("""INSERT INTO staff_activity
                               (tenant_id, actor_key, actor_name, action, lead_id, contact_id, company_id, qty, value, detail)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (int(tenant_id), actor_key, (actor_name or "")[:200] or None, action[:40],
                         lead_id, contact_id, company_id, int(qty or 1), value,
                         (detail or None) and str(detail)[:500]))
            cur.close()
        finally:
            conn.close()
        if actor_key and not actor_key.startswith("system"):
            _presence(tenant_id, actor_key, actor_name, force=True)
    except Exception as e:
        print(f"⚠️ staff_activity.log error ({action}):", e)


STAGE_ACTIONS = {"opportunity": "opportunity", "proposal_sent": "proposal", "won": "won",
                 "lost": "lost", "dropped": "lost", "not_a_fit": "not_a_fit"}


def log_stage_change(lead_id: int, from_stage, to_stage: str, notes: str = None) -> None:
    """Called from merchant_pipeline.record_stage_change for every move."""
    if from_stage is None and to_stage == "new_lead":
        action = "lead_created"
    elif from_stage == "not_a_fit":
        action = "reopened"
    else:
        action = STAGE_ACTIONS.get(to_stage, "stage_moved")
    value = None
    if action == "won":
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT deal_value FROM merchant_pipeline_leads WHERE id=%s", (lead_id,))
            r = cur.fetchone()
            value = r[0] if r else None
            cur.close(); conn.close()
        except Exception as e:
            print("⚠️ staff_activity won value error:", e)
    log(action, lead_id=lead_id, value=value, detail=f"{from_stage or ''}→{to_stage}")


def _presence(tenant_id, actor_key, actor_name, force=False):
    now = _time.time()
    with _lock:
        last = _presence_last.get(actor_key, 0)
        if not force and now - last < _PRESENCE_EVERY_S:
            return
        _presence_last[actor_key] = now
    conn = get_db_connection()
    if not conn:
        return
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"""
            INSERT INTO staff_presence (tenant_id, actor_key, day, actor_name, first_seen, last_seen, active_minutes)
            VALUES (%s, %s, (NOW() AT TIME ZONE '{DAY_TZ}')::date, %s, NOW(), NOW(), 1)
            ON CONFLICT (tenant_id, actor_key, day) DO UPDATE SET
                actor_name = COALESCE(EXCLUDED.actor_name, staff_presence.actor_name),
                active_minutes = staff_presence.active_minutes + CASE
                    WHEN NOW() - staff_presence.last_seen <= INTERVAL '{ACTIVE_GAP_MIN} minutes'
                    THEN CEIL(EXTRACT(EPOCH FROM NOW() - staff_presence.last_seen) / 60)::int
                    ELSE 1 END,
                longest_gap_min = GREATEST(staff_presence.longest_gap_min, CASE
                    WHEN NOW() - staff_presence.last_seen > INTERVAL '{ACTIVE_GAP_MIN} minutes'
                    THEN FLOOR(EXTRACT(EPOCH FROM NOW() - staff_presence.last_seen) / 60)::int
                    ELSE 0 END),
                last_seen = NOW()
        """, (int(tenant_id), actor_key, (actor_name or "")[:200] or None))
        cur.close()
    except Exception as e:
        print("⚠️ staff_activity presence error:", e)
    finally:
        conn.close()


_SKIP_ENDPOINT_BITS = ("static", "service_worker", "manifest", "favicon")


def track_request(request) -> None:
    """before_app_request hook: a real page visit or a real action counts as
    being present. Background polling (XHR/JSON GETs every few seconds, which
    keep running in an idle tab) does not, so idle staff don't look busy."""
    try:
        ep = request.endpoint or ""
        if any(b in ep for b in _SKIP_ENDPOINT_BITS):
            return
        if request.method == "GET" and (
                request.headers.get("X-Requested-With") == "XMLHttpRequest"
                or "application/json" in (request.headers.get("Accept") or "")
                or request.headers.get("Sec-Fetch-Dest") not in (None, "document")):
            return
        a = _session_actor()
        if not a:
            return
        actor_key, actor_name, cid = a
        now = _time.time()
        if now - _presence_last.get(actor_key, 0) < _PRESENCE_EVERY_S:
            return
        conn = get_db_connection()
        if not conn:
            return
        try:
            cur = conn.cursor()
            tid = _tenant_for(cid, cur)
            cur.close()
        finally:
            conn.close()
        if tid:
            _presence(tid, actor_key, actor_name)
    except Exception as e:
        print("⚠️ staff_activity.track_request error:", e)
