"""
buffer_accounts.py — where Buffer connections are stored, for both sides:

  owner_key 'tenant:<id>'  a business's own Buffer (Integration › Buffer)
  owner_key 'admin'        PhiXtra's own Buffer (admin › PhiXtra's Buffer)

The API key is Fernet-encrypted with the same helper the payment gateways
use and is never shown back in full — only key_last4. Every Buffer call
made on behalf of an owner goes through call(), so a key that stops working
(deleted in Buffer, account closed) flips the connection to
'needs_attention' in one place and, for a business, emails the account
owner once.
"""
import psycopg2.extras

from db import get_db_connection
from buffer_client import (BufferAPIError, buffer_get_organizations, buffer_list_channels)

ADMIN_OWNER = "admin"

# Buffer service name -> label + colour used for the little network dot.
SERVICE_INFO = {
    "facebook":       ("Facebook",        "#0866FF"),
    "instagram":      ("Instagram",       "#D62976"),
    "linkedin":       ("LinkedIn",        "#0A66C2"),
    "twitter":        ("X",               "#111111"),
    "tiktok":         ("TikTok",          "#111111"),
    "threads":        ("Threads",         "#111111"),
    "youtube":        ("YouTube",         "#FF0000"),
    "pinterest":      ("Pinterest",       "#E60023"),
    "googleBusiness": ("Google Business", "#1A73E8"),
    "mastodon":       ("Mastodon",        "#6364FF"),
    "bluesky":        ("Bluesky",         "#1185FE"),
}


def service_label(service: str) -> str:
    return SERVICE_INFO[service][0] if service in SERVICE_INFO else (service or "Other").title()


def service_colour(service: str) -> str:
    return SERVICE_INFO.get(service or "", ("", "#667085"))[1]


def tenant_owner(tenant_id: int) -> str:
    return f"tenant:{int(tenant_id)}"


def _crypto():
    # Imported lazily: portal_routes imports half the app.
    from portal_routes import _encrypt_key, _decrypt_key
    return _encrypt_key, _decrypt_key


def get_account(owner_key: str):
    """The connection row without the encrypted key, or None."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""SELECT owner_key, tenant_id, key_last4, organization_id, organization_name,
                              status, last_error, last_checked_at, connected_by, connected_at
                       FROM buffer_accounts WHERE owner_key=%s""", (owner_key,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def is_connected(owner_key: str) -> bool:
    try:
        return get_account(owner_key) is not None
    except Exception:
        return False


def get_api_key(owner_key: str) -> str:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT api_key_enc FROM buffer_accounts WHERE owner_key=%s", (owner_key,))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not row:
        return ""
    _, decrypt = _crypto()
    return decrypt(row[0])


def list_channels(owner_key: str, enabled_only: bool = False) -> list:
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        sql = "SELECT * FROM buffer_channels WHERE owner_key=%s"
        if enabled_only:
            sql += " AND enabled AND NOT is_disconnected"
        cur.execute(sql + " ORDER BY service, COALESCE(display_name, name)", (owner_key,))
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()
    for r in rows:
        r["label"] = service_label(r["service"])
        r["colour"] = service_colour(r["service"])
        r["title"] = r.get("display_name") or r.get("name") or r["channel_id"]
    return rows


def check_key(api_key: str) -> list:
    """Proves a key works. Returns its organizations; raises BufferAPIError."""
    orgs = buffer_get_organizations(api_key)
    if not orgs:
        raise BufferAPIError("Buffer accepted the key but this account has no organisation yet. "
                             "Finish setting up your Buffer account, then try again.")
    return orgs


def save_account(owner_key: str, tenant_id, api_key: str, org: dict, connected_by: str):
    """Stores (or replaces) the key and organisation, then loads its channels.
    Replacing the key with one from a different Buffer organisation drops the
    old organisation's channels."""
    encrypt, _ = _crypto()
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT organization_id FROM buffer_accounts WHERE owner_key=%s", (owner_key,))
        prev = cur.fetchone()
        cur.execute("""
            INSERT INTO buffer_accounts (owner_key, tenant_id, api_key_enc, key_last4, organization_id,
                                         organization_name, status, last_error, last_checked_at,
                                         alert_sent_at, connected_by, connected_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,'ok',NULL,NOW(),NULL,%s,NOW(),NOW())
            ON CONFLICT (owner_key) DO UPDATE SET
                api_key_enc=EXCLUDED.api_key_enc, key_last4=EXCLUDED.key_last4,
                organization_id=EXCLUDED.organization_id, organization_name=EXCLUDED.organization_name,
                status='ok', last_error=NULL, last_checked_at=NOW(), alert_sent_at=NULL,
                connected_by=EXCLUDED.connected_by, updated_at=NOW()
        """, (owner_key, tenant_id, encrypt(api_key), api_key[-4:], org["id"], org.get("name"), connected_by))
        if prev and prev[0] != org["id"]:
            cur.execute("DELETE FROM buffer_channels WHERE owner_key=%s", (owner_key,))
        conn.commit()
    finally:
        cur.close(); conn.close()
    refresh_channels(owner_key)


def switch_organization(owner_key: str, org_id: str):
    """Only to an organisation the saved key can see."""
    orgs = call(owner_key, buffer_get_organizations)
    org = next((o for o in orgs if o["id"] == org_id), None)
    if not org:
        raise BufferAPIError("That Buffer organisation isn't available to this key.")
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE buffer_accounts SET organization_id=%s, organization_name=%s, updated_at=NOW()
                       WHERE owner_key=%s""", (org["id"], org.get("name"), owner_key))
        cur.execute("DELETE FROM buffer_channels WHERE owner_key=%s", (owner_key,))
        conn.commit()
    finally:
        cur.close(); conn.close()
    refresh_channels(owner_key)


def list_organizations(owner_key: str) -> list:
    return call(owner_key, buffer_get_organizations)


def refresh_channels(owner_key: str) -> list:
    """Re-reads the social accounts from Buffer. Keeps each existing
    "Use in PhiXtra" choice; new accounts come in switched on; accounts
    removed from Buffer are dropped."""
    acct = get_account(owner_key)
    if not acct:
        return []
    _stamp_channels_checked(owner_key)
    live = call(owner_key, buffer_list_channels, acct["organization_id"])
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        ids = []
        for ch in live:
            ids.append(ch["id"])
            cur.execute("""
                INSERT INTO buffer_channels (owner_key, channel_id, service, name, display_name, avatar,
                                             is_disconnected, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (owner_key, channel_id) DO UPDATE SET
                    service=EXCLUDED.service, name=EXCLUDED.name, display_name=EXCLUDED.display_name,
                    avatar=EXCLUDED.avatar, is_disconnected=EXCLUDED.is_disconnected, updated_at=NOW()
            """, (owner_key, ch["id"], ch.get("service"), ch.get("name"), ch.get("displayName"),
                  ch.get("avatar"), bool(ch.get("isDisconnected") or ch.get("isLocked"))))
        if ids:
            cur.execute("DELETE FROM buffer_channels WHERE owner_key=%s AND NOT (channel_id = ANY(%s))",
                        (owner_key, ids))
        else:
            cur.execute("DELETE FROM buffer_channels WHERE owner_key=%s", (owner_key,))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return list_channels(owner_key)


CHANNELS_MAX_AGE_MINUTES = 10


def refresh_channels_if_stale(owner_key: str):
    """Re-reads the channel list from Buffer if it's over 10 minutes old
    (read-only Buffer call). Called by every page that shows the accounts,
    so one added in Buffer later appears on its own. Never breaks the page:
    a Buffer error is logged and the saved list is used; the attempt still
    counts, so a broken key isn't retried on every page load."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT 1 FROM buffer_accounts WHERE owner_key=%s
                       AND (channels_checked_at IS NULL
                            OR channels_checked_at < NOW() - make_interval(mins => %s))""",
                    (owner_key, CHANNELS_MAX_AGE_MINUTES))
        stale = cur.fetchone() is not None
    finally:
        cur.close(); conn.close()
    if not stale:
        return
    try:
        refresh_channels(owner_key)
    except Exception as e:
        print(f"⚠️ Buffer channel auto-refresh for {owner_key}:", e)


def _stamp_channels_checked(owner_key: str):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE buffer_accounts SET channels_checked_at=NOW() WHERE owner_key=%s", (owner_key,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def set_enabled_channels(owner_key: str, enabled_ids):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE buffer_channels SET enabled = (channel_id = ANY(%s)), updated_at=NOW() WHERE owner_key=%s",
                    (list(enabled_ids), owner_key))
        conn.commit()
    finally:
        cur.close(); conn.close()


def disconnect(owner_key: str):
    """Deletes the stored key and channel list. Posts already created are
    kept as history."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM buffer_accounts WHERE owner_key=%s", (owner_key,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def call(owner_key: str, fn, *args, **kwargs):
    """Runs one buffer_client function with this owner's saved key, keeping
    the connection's health up to date. Re-raises BufferAPIError."""
    api_key = get_api_key(owner_key)
    try:
        result = fn(api_key, *args, **kwargs)
    except BufferAPIError as e:
        if e.is_auth_error:
            _mark_needs_attention(owner_key, str(e))
        raise
    _mark_ok(owner_key)
    return result


def _mark_ok(owner_key: str):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE buffer_accounts SET status='ok', last_error=NULL, last_checked_at=NOW(),
                              alert_sent_at=NULL
                       WHERE owner_key=%s""", (owner_key,))
        conn.commit()
    finally:
        cur.close(); conn.close()


def _mark_needs_attention(owner_key: str, message: str):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""UPDATE buffer_accounts SET status='needs_attention', last_error=%s, last_checked_at=NOW()
                       WHERE owner_key=%s RETURNING tenant_id, alert_sent_at""", (message, owner_key))
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()
    if row and row["tenant_id"] and not row["alert_sent_at"]:
        _email_owner_key_problem(owner_key, row["tenant_id"])


def _email_owner_key_problem(owner_key: str, tenant_id: int):
    """One email per breakage (reset when the connection works again)."""
    try:
        from portal_utils import send_email
        from portal_routes import BRAND, _PORTAL_BASE_URL
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT email, first_name FROM customers WHERE tenant_id=%s
                       ORDER BY id LIMIT 1""", (tenant_id,))
        owner = cur.fetchone()
        cur.close(); conn.close()
        if not owner or not owner.get("email"):
            return
        link = f"{_PORTAL_BASE_URL}/integrations/buffer"
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px">
          <h2 style="color:{BRAND}">Your Buffer connection needs attention</h2>
          <p>Hi {owner.get('first_name') or 'there'},</p>
          <p>Buffer stopped accepting the key saved in PhiXtra. This usually means the key was deleted
             in Buffer or the Buffer account was closed. Scheduled social posts may not go out until it's fixed.</p>
          <p>To fix it, create a new key in Buffer (Settings › API) and paste it into PhiXtra.</p>
          <p><a href="{link}" style="background:{BRAND};color:#fff;padding:10px 18px;border-radius:12px;text-decoration:none;display:inline-block">Open Buffer in PhiXtra</a></p>
        </div>"""
        if send_email(owner["email"], "Your Buffer connection needs attention", html,
                      text_body=f"Buffer stopped accepting your saved key. Fix it here: {link}"):
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE buffer_accounts SET alert_sent_at=NOW() WHERE owner_key=%s", (owner_key,))
            conn.commit()
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ Buffer needs-attention email failed:", e)
