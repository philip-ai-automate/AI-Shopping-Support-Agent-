from flask import Flask, render_template, request, redirect, url_for, session, flash
import bcrypt
import psycopg2
import psycopg2.extras
import psycopg2.errors
import secrets
import string
from datetime import datetime, timedelta

from db import get_db_connection, insert_audit_log

app = Flask(__name__, template_folder="templates")
app.secret_key = "profitbuyz-very-secret-key-2026"

def _get_trial_default_days() -> int:
    """Read trial_default_days from system_settings. Returns 14 if not found."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT setting_value FROM system_settings WHERE setting_key='trial_default_days'")
        row = cur.fetchone()
        cur.close(); conn.close()
        if row:
            return max(1, int(row["setting_value"]))
    except Exception:
        pass
    return 14

def generate_api_key_and_hash(length: int = 28):
    alphabet = string.ascii_letters + string.digits
    plain_key = ''.join(secrets.choice(alphabet) for _ in range(length))
    hashed_key = bcrypt.hashpw(plain_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    return plain_key, hashed_key

def is_logged_in():
    return session.get("logged_in") is True

def _safe_int(v: str, default=None):
    try:
        return int(v)
    except Exception:
        return default

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        conn = get_db_connection()
        if not conn:
            return render_template("login.html", error="Database unavailable")

        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM admin_users WHERE username=%s", (username,))
        admin = cursor.fetchone()
        cursor.close()
        conn.close()

        # Plain password check (as requested)
        if admin and password == admin["password"]:
            session["logged_in"] = True
            session["admin_username"] = admin["username"]
            return redirect(url_for("index"))
        else:
            error = "Invalid login"

    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/", methods=["GET", "POST"])
def index():
    """Create API key (trial/paid) - now blocks duplicates and points staff to Edit."""
    if not is_logged_in():
        return redirect(url_for("login"))

    plain_key = None
    created_at = None
    error = None

    if request.method == "POST":
        website = request.form.get("website", "").strip()
        tenant_id_str = request.form.get("tenant_id", "").strip()
        key_type = request.form.get("key_type", "paid").strip()
        token_limit = request.form.get("token_limit", "").strip()
        activate_now = request.form.get("activate_now") == "on"

        if not website or not tenant_id_str:
            error = "Website and Tenant ID are required"
            return render_template("index.html", plain_key=None, created_at=None, error=error)

        tenant_id = _safe_int(tenant_id_str)
        if tenant_id is None:
            error = "Tenant ID must be a number"
            return render_template("index.html", plain_key=None, created_at=None, error=error)

        if key_type not in ("paid", "trial"):
            error = "Invalid key type"
            return render_template("index.html", plain_key=None, created_at=None, error=error)

        token_limit_int = None
        if key_type == "trial":
            # token_limit is required for trial
            if not token_limit:
                error = "Token limit is required for trial keys"
                return render_template("index.html", plain_key=None, created_at=None, error=error)
            try:
                token_limit_int = int(token_limit)
                if token_limit_int <= 0:
                    raise ValueError("token_limit must be > 0")
            except Exception:
                error = "Token limit must be a number greater than 0"
                return render_template("index.html", plain_key=None, created_at=None, error=error)

        conn = get_db_connection()
        if not conn:
            return render_template("index.html", plain_key=None, created_at=None, error="Database unavailable")

        admin_username = session.get("admin_username", "unknown")

        try:
            # 1) Verify tenant exists (so staff can't mistype tenant_id)
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT id, name, domain FROM tenants WHERE id=%s", (tenant_id,))
            tenant = cur.fetchone()
            cur.close()
            if not tenant:
                conn.close()
                error = f"Tenant ID {tenant_id} not found. Create/select a valid tenant."
                return render_template("index.html", plain_key=None, created_at=None, error=error)

            # 2) BLOCK duplicates: if same tenant+website+key_type exists, don't create a new one.
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT id, is_active, key_type, website, tenant_id, tokens_used, token_limit, trial_expires_at
                FROM api_keys
                WHERE tenant_id=%s AND website=%s AND key_type=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                (tenant_id, website, key_type),
            )
            existing_same_type = cur.fetchone()
            cur.close()

            if existing_same_type:
                conn.close()
                flash(
                    f"A {key_type.upper()} key already exists for {website} (Tenant {tenant_id}). Use Edit instead of creating a duplicate.",
                    "warning",
                )
                return redirect(url_for("edit_key", key_id=existing_same_type["id"]))

            # 3) Enforce the business rule: at most 1 trial and 1 paid per website (per tenant).
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT key_type, id
                FROM api_keys
                WHERE tenant_id=%s AND website=%s
                """,
                (tenant_id, website),
            )
            rows = cur.fetchall()
            cur.close()

            existing_types = {r.get("key_type") for r in rows}
            if "trial" in existing_types and "paid" in existing_types:
                conn.close()
                flash(
                    f"This website already has BOTH a TRIAL and a PAID key. No more keys can be created for {website}. Use Edit on the existing keys.",
                    "warning",
                )
                return redirect(url_for("keys"))

            # Create new key
            plain_key, hashed_key = generate_api_key_and_hash()
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            last4 = plain_key[-4:]

            trial_activated_at = None
            trial_expires_at = None
            if key_type == "trial" and activate_now:
                trial_activated_at = datetime.utcnow()
                trial_expires_at = trial_activated_at + timedelta(days=_get_trial_default_days())

            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO api_keys
                      (tenant_id, api_key_hash, is_active, website, key_type, trial_activated_at, trial_expires_at, token_limit, tokens_used)
                    VALUES
                      (%s, %s, TRUE, %s, %s, %s, %s, %s, 0)
                    RETURNING id
                    """,
                    (tenant_id, hashed_key, website, key_type, trial_activated_at, trial_expires_at, token_limit_int),
                )
                api_key_id = cur.fetchone()[0]
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                # In case someone clicks twice / race condition. DB unique constraint blocks duplicates.
                conn.rollback()
                cur.close()
                conn.close()
                flash(
                    f"Duplicate blocked ✅ A {key_type.upper()} key for {website} already exists. Use Edit.",
                    "warning",
                )
                return redirect(url_for("keys"))
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

            conn.close()

            # Audit log (creation)
            insert_audit_log(
                admin_username=admin_username,
                action="create_key",
                tenant_id=tenant_id,
                website=website,
                key_type=key_type,
                api_key_id=api_key_id,
                api_key_last4=last4,
                api_key_plain=plain_key,
                details={"activate_now": activate_now, "token_limit": token_limit_int},
            )

            flash(f"{key_type.upper()} key created for {website}. Copy it now — it won't be shown again.", "success")

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            error = f"Error creating key: {e}"

    return render_template("index.html", plain_key=plain_key, created_at=created_at, error=error)

@app.route("/keys")
def keys():
    if not is_logged_in():
        return redirect(url_for("login"))

    conn = get_db_connection()
    if not conn:
        return render_template("keys.html", keys=[], error="Database unavailable")

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT
          id, tenant_id, website, key_type, is_active,
          trial_activated_at, trial_expires_at,
          token_limit, tokens_used,
          created_at
        FROM api_keys
        ORDER BY id DESC
        LIMIT 300
        """
    )
    keys_list = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("keys.html", keys=keys_list, error=None)

@app.route("/keys/<int:key_id>/edit", methods=["GET", "POST"])
def edit_key(key_id: int):
    """Edit an existing key instead of creating duplicates."""
    if not is_logged_in():
        return redirect(url_for("login"))

    conn = get_db_connection()
    if not conn:
        return render_template("key_edit.html", key=None, error="Database unavailable")

    admin_username = session.get("admin_username", "unknown")
    error = None

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT id, tenant_id, website, key_type, is_active,
               trial_activated_at, trial_expires_at,
               token_limit, tokens_used, created_at
        FROM api_keys
        WHERE id=%s
        """,
        (key_id,),
    )
    key_row = cur.fetchone()
    cur.close()

    if not key_row:
        conn.close()
        return render_template("key_edit.html", key=None, error="Key not found")

    if request.method == "POST":
        # Allowed edits: is_active, token_limit (trial), activate_now (trial), deactivate_now
        new_is_active = True if request.form.get("is_active") == "on" else False
        new_token_limit = request.form.get("token_limit", "").strip()
        reset_tokens = request.form.get("reset_tokens") == "on"
        activate_now = request.form.get("activate_now") == "on"

        token_limit_int = key_row.get("token_limit")
        if key_row.get("key_type") == "trial":
            if new_token_limit:
                try:
                    token_limit_int = int(new_token_limit)
                    if token_limit_int <= 0:
                        raise ValueError()
                except Exception:
                    error = "Token limit must be a number greater than 0"
            else:
                token_limit_int = key_row.get("token_limit")

        if not error:
            trial_activated_at = key_row.get("trial_activated_at")
            trial_expires_at = key_row.get("trial_expires_at")
            if key_row.get("key_type") == "trial" and activate_now:
                # Activate only if not activated before
                if not trial_activated_at:
                    trial_activated_at = datetime.utcnow()
                    trial_expires_at = trial_activated_at + timedelta(days=_get_trial_default_days())

            tokens_used = 0 if reset_tokens else key_row.get("tokens_used", 0)

            cur = conn.cursor()
            cur.execute(
                """
                UPDATE api_keys
                SET is_active=%s,
                    token_limit=%s,
                    tokens_used=%s,
                    trial_activated_at=%s,
                    trial_expires_at=%s
                WHERE id=%s
                """,
                (new_is_active, token_limit_int, tokens_used, trial_activated_at, trial_expires_at, key_id),
            )
            conn.commit()
            cur.close()
            conn.close()

            insert_audit_log(
                admin_username=admin_username,
                action="edit_key",
                tenant_id=key_row.get("tenant_id"),
                website=key_row.get("website"),
                key_type=key_row.get("key_type"),
                api_key_id=key_id,
                api_key_last4=None,
                api_key_plain=None,
                details={
                    "is_active": new_is_active,
                    "token_limit": token_limit_int,
                    "reset_tokens": reset_tokens,
                    "activate_now": activate_now,
                },
            )

            flash("Key updated successfully ✅", "success")
            return redirect(url_for("keys"))

    conn.close()
    return render_template("key_edit.html", key=key_row, error=error)

@app.route("/tenants", methods=["GET", "POST"])
def tenants():
    """Tenant management (blocks duplicate domain, provides edit)."""
    if not is_logged_in():
        return redirect(url_for("login"))

    conn = get_db_connection()
    if not conn:
        return render_template("tenants.html", tenants=[], error="Database unavailable")

    admin_username = session.get("admin_username", "unknown")
    error = None

    if request.method == "POST":
        action = request.form.get("action")
        name = request.form.get("name", "").strip()
        domain = request.form.get("domain", "").strip()
        tenant_id = request.form.get("tenant_id", "").strip()

        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            if action == "add":
                if not name or not domain:
                    error = "Tenant name and domain are required"
                else:
                    # Friendly pre-check (DB unique constraint also blocks)
                    cur.execute("SELECT id FROM tenants WHERE domain=%s LIMIT 1", (domain,))
                    existing = cur.fetchone()
                    if existing:
                        error = f"Duplicate blocked ✅ Tenant domain already exists. Use Edit."
                    else:
                        cur2 = conn.cursor()
                        cur2.execute("INSERT INTO tenants (name, domain, status) VALUES (%s, %s, 'active')", (name, domain))
                        conn.commit()
                        cur2.close()
                        insert_audit_log(admin_username=admin_username, action="tenant_add", details={"name": name, "domain": domain})

            elif action == "update":
                tid = _safe_int(tenant_id)
                if not tid:
                    error = "Invalid tenant id"
                elif not name or not domain:
                    error = "Tenant name and domain are required"
                else:
                    # Domain uniqueness check (exclude self)
                    cur.execute("SELECT id FROM tenants WHERE domain=%s AND id<>%s LIMIT 1", (domain, tid))
                    dup = cur.fetchone()
                    if dup:
                        error = "Duplicate blocked ✅ Another tenant already uses this domain."
                    else:
                        cur2 = conn.cursor()
                        cur2.execute("UPDATE tenants SET name=%s, domain=%s WHERE id=%s", (name, domain, tid))
                        conn.commit()
                        cur2.close()
                        insert_audit_log(admin_username=admin_username, action="tenant_update", tenant_id=tid, details={"name": name, "domain": domain})

            cur.close()
        except psycopg2.errors.UniqueViolation:
            error = "Duplicate blocked ✅ This domain already exists."
        except Exception as e:
            error = str(e)

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, domain, status, created_at FROM tenants ORDER BY id DESC LIMIT 300")
    tenants_list = cur.fetchall()
    cur.close()
    conn.close()

    return render_template("tenants.html", tenants=tenants_list, error=error)

@app.route("/admins", methods=["GET", "POST"])
def admins():
    if not is_logged_in():
        return redirect(url_for("login"))

    conn = get_db_connection()
    if not conn:
        return render_template("admins.html", admins=[], error="Database unavailable")

    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    error = None
    try:
        if request.method == "POST":
            action = request.form.get("action")
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()
            admin_id = request.form.get("admin_id", "").strip()

            if action == "add" and username and password:
                cursor.execute("INSERT INTO admin_users (username, password) VALUES (%s, %s)", (username, password))
                conn.commit()
                insert_audit_log(admin_username=session.get("admin_username"), action="admin_add", details={"username": username})

            elif action == "update" and admin_id and password:
                cursor.execute("UPDATE admin_users SET password=%s WHERE id=%s", (password, admin_id))
                conn.commit()
                insert_audit_log(admin_username=session.get("admin_username"), action="admin_update", details={"admin_id": admin_id})

            elif action == "delete" and admin_id:
                cursor.execute("DELETE FROM admin_users WHERE id=%s", (admin_id,))
                conn.commit()
                insert_audit_log(admin_username=session.get("admin_username"), action="admin_delete", details={"admin_id": admin_id})

        cursor.execute("SELECT id, username, password, created_at FROM admin_users ORDER BY id DESC")
        admins_list = cursor.fetchall()
    except Exception as e:
        admins_list = []
        error = str(e)

    cursor.close()
    conn.close()
    return render_template("admins.html", admins=admins_list, error=error)

@app.route("/audit")
def audit():
    if not is_logged_in():
        return redirect(url_for("login"))

    conn = get_db_connection()
    if not conn:
        return render_template("audit.html", logs=[], error="Database unavailable")

    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute(
        """
        SELECT id, admin_username, action, tenant_id, website, key_type,
               api_key_id, api_key_last4, created_at, details
        FROM audit_logs
        ORDER BY created_at DESC
        LIMIT 500
        """
    )
    logs = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("audit.html", logs=logs, error=None)
