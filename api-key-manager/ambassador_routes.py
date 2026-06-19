"""
ambassador_routes.py — Ambassador portal (/ambassador/*)
Separate from the customer portal. Ambassadors register, admin approves,
then they can log in to see referrals, earnings, QR code and tier progress.
"""
import os
import io
import json as _json
import uuid
import secrets
import string
import urllib.request
import urllib.parse
import bcrypt
import psycopg2
import psycopg2.extras
from datetime import date, timedelta, datetime
from werkzeug.utils import secure_filename
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash)

UPLOAD_FOLDER    = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'ambassador_ids')
ALLOWED_DOC_EXTS = {'jpg', 'jpeg', 'png', 'pdf'}

def _allowed_doc(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_DOC_EXTS

from db import get_db_connection
from portal_utils import send_email

ambassador_bp = Blueprint("ambassador", __name__)

BRAND         = "#030C18"
BASE_URL      = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")
COMMISSION_PC = 0.20   # 20%

TURNSTILE_SECRET       = os.getenv("TURNSTILE_SECRET_KEY", "")
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_MINS  = 15

UPSELL_BONUSES = {
    ("starter", "growth"): 5000,
    ("growth",  "pro"):   10000,
    ("starter", "pro"):   15000,
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _amb_logged_in() -> bool:
    return session.get("ambassador_logged_in") is True

def _require_amb():
    if not _amb_logged_in():
        return redirect(url_for("ambassador.login"))
    return None

def _amb_id() -> int:
    return int(session["ambassador_id"])

def _hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def _check_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False

def _generate_ref_code(first: str, last: str) -> str:
    base = (first[:3] + last[:3]).lower()
    base = ''.join(c for c in base if c.isalnum())
    return base + secrets.token_hex(2)

def _verify_turnstile(token: str, remote_ip: str = "") -> bool:
    """Verify a Cloudflare Turnstile token. Returns True if valid (or if Turnstile is not configured)."""
    if not TURNSTILE_SECRET:
        return True          # not configured — skip check
    if not token:
        return False
    try:
        data = urllib.parse.urlencode({
            "secret":   TURNSTILE_SECRET,
            "response": token,
            "remoteip": remote_ip,
        }).encode()
        req = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return bool(_json.loads(resp.read()).get("success"))
    except Exception:
        return True          # fail open on transient network error


def _is_rate_limited(ip: str) -> bool:
    """True if this IP has exceeded the failed login attempt ceiling."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cutoff = datetime.utcnow() - timedelta(minutes=RATE_LIMIT_WINDOW_MINS)
    cur.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ip_address=%s AND attempted_at > %s",
        (ip, cutoff)
    )
    count = int((cur.fetchone() or [0])[0])
    # Opportunistic cleanup of records older than 24 h (keeps table small)
    cur.execute("DELETE FROM login_attempts WHERE attempted_at < NOW() - INTERVAL '1 day'")
    conn.commit()
    cur.close(); conn.close()
    return count >= RATE_LIMIT_MAX_ATTEMPTS


def _log_login_attempt(ip: str, email: str):
    """Record a failed login attempt for rate-limit tracking."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO login_attempts (ip_address, email) VALUES (%s, %s)",
        (ip, email)
    )
    conn.commit()
    cur.close(); conn.close()


def _get_ambassador(amb_id: int) -> dict | None:
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM ambassadors WHERE id=%s", (amb_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return dict(row) if row else None

def _active_client_count(ref_code: str) -> int:
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM tenants WHERE ref_code=%s AND status='active'",
        (ref_code,)
    )
    count = int((cur.fetchone() or [0])[0])
    cur.close(); conn.close()
    return count

def _tier_info(active_clients: int, partnership_start) -> dict:
    """Return tier name, commission months (None=lifetime), next tier info."""
    if active_clients >= 20:
        return {
            "name": "Master Ambassador", "months": None, "min": 20, "max": None,
            "next_name": None, "next_min": None, "progress_pct": 100,
        }
    elif active_clients >= 10:
        return {
            "name": "Elite Ambassador", "months": 24, "min": 10, "max": 19,
            "next_name": "Master Ambassador", "next_min": 20,
            "progress_pct": int(((active_clients - 10) / 10) * 100),
        }
    else:
        return {
            "name": "Standard Ambassador", "months": 12, "min": 1, "max": 9,
            "next_name": "Elite Ambassador", "next_min": 10,
            "progress_pct": int((active_clients / 10) * 100),
        }

def _commission_eligible(amb: dict) -> bool:
    if amb.get("status") != "active":
        return False
    ps = amb.get("partnership_start")
    if not ps:
        return False
    active = _active_client_count(amb["ref_code"])
    tier   = _tier_info(active, ps)
    if tier["months"] is None:
        return True
    expiry = ps + timedelta(days=30 * tier["months"])
    return date.today() <= expiry

def _send_pending_email(amb_name: str, amb_email: str):
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px">
      <h2 style="color:{BRAND}">Application Received</h2>
      <p>Hi {amb_name},</p>
      <p>Thank you for applying to the PhiXtra Ambassador programme. Your application is under
         review and you will hear from us within 24 hours.</p>
      <p style="color:#888;font-size:12px">Questions? Contact support@phixtra.com</p>
    </div>"""
    send_email(amb_email, "Your PhiXtra Ambassador Application", html,
               text_body="Your application is under review. We'll be in touch within 24 hours.")

def _send_admin_ambassador_signup_email(first: str, last: str, email: str, phone: str,
                                         whatsapp: str = "", nationality: str = "",
                                         location: str = "", qualification: str = "",
                                         id_doc_type: str = ""):
    admin_link = f"{BASE_URL}/admin/ambassadors"
    def row(label, val):
        return (f'<tr><td style="padding:6px 10px;font-weight:700;background:#f3f4f6;'
                f'border:1px solid #e5e7eb;width:160px">{label}</td>'
                f'<td style="padding:6px 10px;border:1px solid #e5e7eb">{val or "—"}</td></tr>')
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px">
      <h2 style="color:{BRAND}">&#128226; New Ambassador Application</h2>
      <table style="border-collapse:collapse;width:100%;margin-bottom:16px">
        {row("Name", f"{first} {last}")}
        {row("Email", email)}
        {row("Phone", phone)}
        {row("WhatsApp", whatsapp)}
        {row("Nationality", nationality)}
        {row("Location", location)}
        {row("Qualification", qualification)}
        {row("ID Document", id_doc_type)}
      </table>
      <p style="margin-top:12px">
        <a href="{admin_link}" style="background:{BRAND};color:#fff;padding:10px 18px;
           border-radius:12px;text-decoration:none;display:inline-block">
          Review Application
        </a>
      </p>
    </div>"""
    send_email(
        "support@phixtra.com",
        f"New ambassador application: {first} {last}",
        html,
        text_body=f"New ambassador application from {first} {last} <{email}>.\nReview: {admin_link}",
    )

WA_GROUP_LINK = "https://chat.whatsapp.com/KKRG7lVuxq6LVGMdLHYaqG"


def _send_whatsapp_welcome(first_name: str, whatsapp_number: str, ref_code: str) -> bool:
    """Send a WhatsApp welcome message to a newly approved ambassador."""
    phone_number_id = os.getenv("WA_OTP_PHONE_NUMBER_ID", "")
    access_token    = os.getenv("WA_OTP_ACCESS_TOKEN", "")
    if not phone_number_id or not access_token:
        print("⚠️ WA_OTP_PHONE_NUMBER_ID or WA_OTP_ACCESS_TOKEN not set — skipping WhatsApp welcome")
        return False

    # Normalise: strip +, spaces, hyphens — Meta API expects digits only with country code
    to = "".join(c for c in (whatsapp_number or "") if c.isdigit())
    if not to:
        return False

    gs_link  = f"{BASE_URL}/ambassador/getting-started"
    ref_link = f"{BASE_URL}/register?ref={ref_code}"
    login_link = f"{BASE_URL}/ambassador/login"

    message = (
        f"Hello {first_name}! 🎉\n\n"
        f"Your PhiXtra Ambassador application has been *approved* — welcome to the team!\n\n"
        f"Here are your two most important first steps:\n\n"
        f"*1️⃣ Join the Ambassador WhatsApp Support Group*\n"
        f"This is where our team supports you in the field — quick answers, tips, and updates:\n"
        f"{WA_GROUP_LINK}\n\n"
        f"*2️⃣ Complete your Getting Started guide*\n"
        f"Log in to your Ambassador Hub and go through the Getting Started section. "
        f"It covers who to approach, how to pitch, how to demo PhiXtra, and how you earn:\n"
        f"{login_link}\n\n"
        f"Your personal referral link is ready inside the Hub. "
        f"Every client who signs up through it is tracked to your account.\n\n"
        f"Welcome aboard! 💪\n"
        f"— PhiXtra AI Team"
    )

    try:
        import urllib.request as _urlreq
        import json as _json
        data = _json.dumps({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": message},
        }).encode()
        req = _urlreq.Request(
            f"https://graph.facebook.com/v19.0/{phone_number_id}/messages",
            data=data,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
            if not ok:
                print(f"⚠️ WhatsApp welcome send failed: HTTP {resp.status}")
            return ok
    except Exception as e:
        print(f"⚠️ _send_whatsapp_welcome error: {e}")
        return False


def _send_approved_email(amb_name: str, amb_email: str, ref_code: str):
    link     = f"{BASE_URL}/ambassador/login"
    ref_link = f"{BASE_URL}/register?ref={ref_code}"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px">
      <h2 style="color:{BRAND}">Welcome to the PhiXtra Ambassador Programme!</h2>
      <p>Hi {amb_name},</p>
      <p>Congratulations — your application has been approved. You can now log in to your
         Ambassador Hub to access your QR code, track referrals, and monitor your earnings.</p>
      <p><a href="{link}" style="background:{BRAND};color:#fff;padding:10px 18px;
         border-radius:12px;text-decoration:none;display:inline-block">Log In to Ambassador Hub</a></p>
      <p><strong>Your referral link:</strong><br>
         <a href="{ref_link}" style="color:{BRAND}">{ref_link}</a></p>
      <p>You earn <strong>20% commission</strong> on every subscription payment from clients
         you refer. You also earn one-time upsell bonuses: <strong>₦5,000</strong> (Starter→Growth),
         <strong>₦10,000</strong> (Growth→Pro), or <strong>₦15,000</strong> (Starter→Pro).</p>

      <div style="background:#dcfce7;border:1px solid #bbf7d0;border-radius:12px;padding:16px 20px;margin:20px 0">
        <p style="margin:0 0 6px;font-size:15px;font-weight:700;color:#15803d">💬 Join the Ambassador WhatsApp Support Group</p>
        <p style="margin:0 0 14px;font-size:13px;color:#166534;line-height:1.5">
          When you're out in the field pitching or onboarding a client, our team is ready to help fast.
          Join the group to get quick answers, tips, and updates directly from PhiXtra.
        </p>
        <a href="{WA_GROUP_LINK}" style="background:#16a34a;color:#fff;padding:10px 20px;
           border-radius:10px;text-decoration:none;display:inline-block;font-weight:700;font-size:13px">
          Join WhatsApp Group →
        </a>
      </div>

      <p style="color:#888;font-size:12px">Questions? Contact support@phixtra.com</p>
    </div>"""
    send_email(amb_email, "You're approved — Welcome to PhiXtra Ambassadors!", html,
               text_body=f"Approved! Log in: {link}\n\nYour referral link: {ref_link}\n\nJoin our Ambassador WhatsApp support group: {WA_GROUP_LINK}")


# ── Auth routes ────────────────────────────────────────────────────────────

@ambassador_bp.route("/ambassador/register", methods=["GET", "POST"])
def register():
    if _amb_logged_in():
        return redirect(url_for("ambassador.dashboard"))

    if request.method == "GET":
        return render_template("ambassador/register.html")

    # ── Parse all fields ────────────────────────────────────────────────────
    first         = (request.form.get("first_name")            or "").strip()
    last          = (request.form.get("last_name")             or "").strip()
    email         = (request.form.get("email")                 or "").strip().lower()
    phone         = (request.form.get("phone")                 or "").strip()
    whatsapp      = (request.form.get("whatsapp_number")       or "").strip()
    pw            = (request.form.get("password")              or "").strip()
    pw2           = (request.form.get("confirm_password")      or "").strip()
    dob           = (request.form.get("date_of_birth")         or "").strip() or None
    gender        = (request.form.get("gender")                or "").strip()
    nationality   = (request.form.get("nationality")           or "").strip()
    address       = (request.form.get("address")               or "").strip()
    location      = (request.form.get("location")              or "").strip()
    qualification = (request.form.get("highest_qualification") or "").strip()
    id_doc_type   = (request.form.get("id_document_type")      or "").strip()
    bank_name     = (request.form.get("bank_name")             or "").strip()
    account_num   = (request.form.get("account_number")        or "").strip()
    account_name  = (request.form.get("account_name")          or "").strip()
    sort_code     = (request.form.get("sort_code")             or "").strip() or None
    swift_code    = (request.form.get("swift_code")            or "").strip() or None

    def _bail(msg, cat="danger"):
        flash(msg, cat)
        return render_template("ambassador/register.html")

    # ── Required-field check ─────────────────────────────────────────────────
    required = [first, last, email, phone, whatsapp, pw, gender, nationality,
                address, location, qualification, id_doc_type, bank_name, account_num, account_name]
    if not all(required):
        return _bail("All required fields must be completed.")
    if len(pw) < 8:
        return _bail("Password must be at least 8 characters.")
    if pw != pw2:
        return _bail("Passwords do not match.")

    # ── Cloudflare Turnstile bot check ───────────────────────────────────────
    ts_token = request.form.get("cf-turnstile-response", "")
    if not _verify_turnstile(ts_token, request.remote_addr or ""):
        return _bail("Security check failed. Please complete the verification and try again.")

    # ── Duplicate checks BEFORE touching the filesystem ─────────────────────
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT 1 FROM ambassadors WHERE email=%s LIMIT 1", (email,))
    if cur.fetchone():
        cur.close(); conn.close()
        return _bail("An account with that email address already exists. Please log in.", "warning")
    cur.execute("SELECT 1 FROM ambassadors WHERE phone=%s LIMIT 1", (phone,))
    if cur.fetchone():
        cur.close(); conn.close()
        return _bail("An account with that phone number is already registered.", "warning")
    cur.execute("SELECT 1 FROM ambassadors WHERE whatsapp_number=%s LIMIT 1", (whatsapp,))
    if cur.fetchone():
        cur.close(); conn.close()
        return _bail("An account with that WhatsApp number is already registered.", "warning")
    cur.close(); conn.close()

    # ── ID document upload ───────────────────────────────────────────────────
    file = request.files.get("id_document")
    if not file or not file.filename:
        return _bail("Please upload an identity document.")
    if not _allowed_doc(file.filename):
        return _bail("Identity document must be a JPG, PNG, or PDF file.")

    ext      = file.filename.rsplit('.', 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    file.save(os.path.join(UPLOAD_FOLDER, filename))
    id_doc_path = f"uploads/ambassador_ids/{filename}"

    ref_code = _generate_ref_code(first, last)
    pw_hash  = _hash_pw(pw)

    # ── Insert ───────────────────────────────────────────────────────────────
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO ambassadors
               (first_name, last_name, email, phone, whatsapp_number, password_hash, ref_code,
                status, date_of_birth, gender, nationality, address, location,
                highest_qualification, id_document_path, id_document_type,
                bank_name, account_number, account_name, sort_code, swift_code)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (first, last, email, phone, whatsapp, pw_hash, ref_code,
              dob, gender, nationality, address, location,
              qualification, id_doc_path, id_doc_type,
              bank_name, account_num, account_name, sort_code, swift_code))
        conn.commit()
    except psycopg2.errors.UniqueViolation as e:
        conn.rollback()
        cur.close(); conn.close()
        cname = (e.diag.constraint_name or "").lower()
        if "phone" in cname:
            return _bail("An account with that phone number is already registered.", "warning")
        if "whatsapp" in cname:
            return _bail("An account with that WhatsApp number is already registered.", "warning")
        return _bail("An account with that email already exists. Please log in.", "warning")
    finally:
        cur.close(); conn.close()

    _send_pending_email(first, email)
    _send_admin_ambassador_signup_email(first, last, email, phone, whatsapp,
                                        nationality, location, qualification, id_doc_type)
    return render_template("ambassador/register.html", submitted=True)


@ambassador_bp.route("/ambassador/login", methods=["GET", "POST"])
def login():
    if _amb_logged_in():
        return redirect(url_for("ambassador.dashboard"))

    if request.method == "GET":
        return render_template("ambassador/login.html")

    ip    = request.remote_addr or "unknown"
    email = (request.form.get("email")    or "").strip().lower()
    pw    = (request.form.get("password") or "").strip()

    # ── Rate limit: block after 5 failed attempts in 15 minutes ─────────────
    if _is_rate_limited(ip):
        flash(
            f"Too many failed login attempts from your location. "
            f"Please wait {RATE_LIMIT_WINDOW_MINS} minutes and try again.",
            "danger"
        )
        return render_template("ambassador/login.html")

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM ambassadors WHERE email=%s", (email,))
    amb = cur.fetchone()
    cur.close(); conn.close()

    if not amb or not _check_pw(pw, amb["password_hash"]):
        _log_login_attempt(ip, email)   # count only genuine auth failures
        flash("Invalid email or password.", "danger")
        return render_template("ambassador/login.html")

    if amb["status"] == "pending":
        flash("Your application is still under review. We'll notify you once approved.", "warning")
        return render_template("ambassador/login.html")

    if amb["status"] == "suspended":
        flash("Your account has been suspended. Please contact support@phixtra.com.", "danger")
        return render_template("ambassador/login.html")

    session["ambassador_logged_in"] = True
    session["ambassador_id"]        = int(amb["id"])
    return redirect(url_for("ambassador.dashboard"))


@ambassador_bp.route("/ambassador/logout")
def logout():
    session.pop("ambassador_logged_in", None)
    session.pop("ambassador_id", None)
    return redirect(url_for("ambassador.login"))


# ── Dashboard ──────────────────────────────────────────────────────────────

@ambassador_bp.route("/ambassador/dashboard")
def dashboard():
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    active_clients = _active_client_count(amb["ref_code"])
    tier           = _tier_info(active_clients, amb.get("partnership_start"))

    # Total earnings
    cur.execute(
        "SELECT COALESCE(SUM(commission_amount),0) AS total FROM ambassador_commissions WHERE ambassador_id=%s",
        (amb["id"],)
    )
    total_earnings = float((cur.fetchone() or {}).get("total") or 0)

    # This month
    cur.execute(
        """SELECT COALESCE(SUM(commission_amount),0) AS total
           FROM ambassador_commissions
           WHERE ambassador_id=%s AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())""",
        (amb["id"],)
    )
    month_earnings = float((cur.fetchone() or {}).get("total") or 0)

    # Recent 5 referrals
    cur.execute("""
        SELECT t.name, t.status, p.name AS plan_name, p.price_ngn,
               t.created_at,
               EXISTS(SELECT 1 FROM ambassador_commissions ac
                      WHERE ac.ambassador_id=%s AND ac.tenant_id=t.id
                        AND ac.commission_type='upsell_bonus') AS has_bonus
        FROM tenants t
        LEFT JOIN plans p ON p.id = t.plan_id
        WHERE t.ref_code = %s
        ORDER BY t.created_at DESC
        LIMIT 5
    """, (amb["id"], amb["ref_code"]))
    recent_referrals = cur.fetchall() or []

    cur.close(); conn.close()

    return render_template("ambassador/dashboard.html",
        amb=amb, tier=tier, active_clients=active_clients,
        total_earnings=total_earnings, month_earnings=month_earnings,
        recent_referrals=recent_referrals, base_url=BASE_URL)


# ── Referrals ──────────────────────────────────────────────────────────────

@ambassador_bp.route("/ambassador/referrals")
def referrals():
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT t.id, t.name, t.domain, t.status, t.created_at,
               p.name AS plan_name, p.price_ngn,
               EXISTS(SELECT 1 FROM ambassador_commissions ac
                      WHERE ac.ambassador_id=%s AND ac.tenant_id=t.id
                        AND ac.commission_type='upsell_bonus') AS has_bonus,
               COALESCE((SELECT SUM(commission_amount) FROM ambassador_commissions ac
                         WHERE ac.ambassador_id=%s AND ac.tenant_id=t.id), 0) AS earned
        FROM tenants t
        LEFT JOIN plans p ON p.id = t.plan_id
        WHERE t.ref_code = %s
        ORDER BY t.created_at DESC
    """, (amb["id"], amb["id"], amb["ref_code"]))
    all_referrals = cur.fetchall() or []
    cur.close(); conn.close()

    return render_template("ambassador/referrals.html", amb=amb, referrals=all_referrals)


# ── Earnings ───────────────────────────────────────────────────────────────

@ambassador_bp.route("/ambassador/earnings")
def earnings():
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT COALESCE(SUM(commission_amount),0) AS total FROM ambassador_commissions WHERE ambassador_id=%s",
        (amb["id"],)
    )
    total = float((cur.fetchone() or {}).get("total") or 0)

    cur.execute("""
        SELECT ac.*, t.name AS tenant_name
        FROM ambassador_commissions ac
        JOIN tenants t ON t.id = ac.tenant_id
        WHERE ac.ambassador_id=%s
        ORDER BY ac.created_at DESC
    """, (amb["id"],))
    entries = cur.fetchall() or []
    cur.close(); conn.close()

    return render_template("ambassador/earnings.html", amb=amb, entries=entries, total=total)


# ── QR Code ────────────────────────────────────────────────────────────────

@ambassador_bp.route("/ambassador/qr")
def qr_page():
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    return render_template("ambassador/qr.html", amb=amb, base_url=BASE_URL)


GS_TOPICS = [
    ("understand-phixtra",  "Understand PhiXtra",      "🗺️", "Learn what PhiXtra is, what it does, and explore a live demo portal account before you pitch"),
    ("your-link",           "Your Link & QR Code",    "🔗", "Your personal sign-up link and QR code — every shop that uses it is tracked to your account"),
    ("who-to-approach",     "Who to Approach",         "👥", "Start with phone and computer shops — find out how to spot a good one and where to find them"),
    ("pitch-messages",      "Pitch Messages",          "💬", "Ready-to-send WhatsApp messages and a step-by-step script for when you walk into a shop"),
    ("demo-to-business",    "Demo to Business",        "🎬", "Show a live PhiXtra AI demo using the profitbuyz.com WhatsApp account — iPhone questions only for now"),
    ("client-requirements", "Client Requirements",     "📋", "Four things a shop must have ready before they can go live on PhiXtra"),
    ("after-signup",        "After They Sign Up",      "🤝", "Five things to do after a shop signs up to make sure they go live fast and you start earning"),
    ("how-you-earn",        "How You Earn",            "💰", "Your 20% monthly cut, how your level goes up, and the bonuses you earn when shops upgrade"),
]

DEMO_WA_NUMBER = "447778391737"  # profitbuyz.com WhatsApp demo account

@ambassador_bp.route("/ambassador/getting-started")
def getting_started():
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    return render_template("ambassador/getting_started.html", amb=amb, topics=GS_TOPICS)

@ambassador_bp.route("/ambassador/getting-started/<topic>")
def getting_started_topic(topic):
    r = _require_amb()
    if r: return r
    valid = [t[0] for t in GS_TOPICS]
    if topic not in valid:
        return redirect(url_for("ambassador.getting_started"))
    amb    = _get_ambassador(_amb_id())
    idx    = valid.index(topic)
    return render_template(
        f"ambassador/gs_{topic.replace('-', '_')}.html",
        amb=amb, base_url=BASE_URL,
        current=GS_TOPICS[idx],
        prev_topic=GS_TOPICS[idx - 1] if idx > 0 else None,
        next_topic=GS_TOPICS[idx + 1] if idx < len(GS_TOPICS) - 1 else None,
        topic_index=idx + 1,
        topic_count=len(GS_TOPICS),
        demo_wa_number=DEMO_WA_NUMBER,
    )


@ambassador_bp.route("/ambassador/checklist")
def checklist():
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    return render_template("ambassador/checklist.html", amb=amb)


@ambassador_bp.route("/ambassador/demo-qr.png")
def demo_qr_image():
    r = _require_amb()
    if r: return r
    import qrcode
    from flask import send_file
    wa_url = f"https://wa.me/{DEMO_WA_NUMBER}"
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=10, border=4)
    qr.add_data(wa_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    as_dl = request.args.get("download") == "1"
    return send_file(buf, mimetype="image/png", as_attachment=as_dl,
                     download_name="phixtra-demo-whatsapp-qr.png")


@ambassador_bp.route("/ambassador/demo-access")
def demo_portal_access():
    """One-click auto-login into the ambassador's personal demo portal."""
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    if not amb:
        flash("Ambassador account not found.", "danger")
        return redirect(url_for("ambassador.dashboard"))
    try:
        from ambassador_demo import create_ambassador_demo
        result = create_ambassador_demo(amb["id"], amb["first_name"], amb["ref_code"])
    except Exception as e:
        print(f"⚠️ demo_portal_access error: {e}")
        flash("Could not open your demo portal. Please try again.", "danger")
        return redirect(url_for("ambassador.getting_started_topic", topic="understand-phixtra"))
    token = result["token"]
    return redirect(f"{BASE_URL}/demo-access/{token}")


@ambassador_bp.route("/ambassador/demo-reset", methods=["POST"])
def demo_portal_reset():
    """Wipe and re-seed the ambassador's demo tenant back to its default state."""
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    if not amb:
        flash("Ambassador account not found.", "danger")
        return redirect(url_for("ambassador.dashboard"))
    try:
        from ambassador_demo import reset_ambassador_demo
        reset_ambassador_demo(amb["id"], amb["first_name"], amb["ref_code"])
        flash("Your demo portal has been reset to default data.", "success")
    except Exception as e:
        print(f"⚠️ demo_portal_reset error: {e}")
        flash("Reset failed — please try again.", "danger")
    return redirect(url_for("ambassador.getting_started_topic", topic="understand-phixtra"))


@ambassador_bp.route("/ambassador/qr.png")
def qr_image():
    r = _require_amb()
    if r: return r
    amb     = _get_ambassador(_amb_id())
    reg_url = f"{BASE_URL}/register?ref={amb['ref_code']}"

    import qrcode
    from flask import send_file
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=10, border=4)
    qr.add_data(reg_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    as_dl    = request.args.get("download") == "1"
    safe     = (amb["first_name"] + "-" + amb["last_name"]).lower().replace(" ", "-")
    return send_file(buf, mimetype="image/png", as_attachment=as_dl,
                     download_name=f"phixtra-ambassador-qr-{safe}.png")


# ── Commission helper (called from portal_routes._activate_plan_subscription) ──

def record_ambassador_commission(tenant_id: int, plan_id: int, prev_plan_id: int,
                                  amount, currency: str) -> None:
    """
    Called after a successful subscription payment.
    - Records 20% commission if ambassador is eligible.
    - Records upsell bonus for Starter→Growth (₦5k), Growth→Pro (₦10k), Starter→Pro (₦15k).
    """
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Resolve plan slugs for both current and previous plan
    cur.execute("SELECT id, slug FROM plans WHERE id = ANY(%s)", ([plan_id, prev_plan_id],))
    slug_map = {r["id"]: r["slug"] for r in cur.fetchall()}
    new_slug  = slug_map.get(plan_id, "")
    prev_slug = slug_map.get(prev_plan_id, "")

    # Find the tenant's ref_code
    cur.execute("SELECT ref_code FROM tenants WHERE id=%s", (tenant_id,))
    row = cur.fetchone()
    if not row or not row.get("ref_code"):
        cur.close(); conn.close()
        return

    ref_code = row["ref_code"]

    # Find the active ambassador for this ref_code
    cur.execute("SELECT * FROM ambassadors WHERE ref_code=%s AND status='active'", (ref_code,))
    amb = cur.fetchone()
    if not amb:
        cur.close(); conn.close()
        return

    amb = dict(amb)
    cur2 = conn.cursor()

    # 20% subscription commission (if eligible, from first payment)
    if _commission_eligible(amb) and float(amount or 0) > 0:
        commission = round(float(amount) * COMMISSION_PC, 2)
        cur2.execute("""
            INSERT INTO ambassador_commissions
              (ambassador_id, tenant_id, commission_type, currency, source_amount, commission_amount, description)
            VALUES (%s,%s,'subscription',%s,%s,%s,%s)
        """, (amb["id"], tenant_id, currency, float(amount), commission,
              f"20% of {currency} {float(amount):.2f} subscription payment"))

    # One-time upsell bonus (per upgrade path, once per tenant)
    bonus = UPSELL_BONUSES.get((prev_slug, new_slug))
    if bonus:
        cur.execute("""
            SELECT 1 FROM ambassador_commissions
            WHERE ambassador_id=%s AND tenant_id=%s AND commission_type='upsell_bonus'
              AND description ILIKE %s
            LIMIT 1
        """, (amb["id"], tenant_id, f"%{prev_slug}%{new_slug}%"))
        if not cur.fetchone():
            label = f"₦{bonus:,} bonus — client upgraded from {prev_slug.title()} to {new_slug.title()}"
            cur2.execute("""
                INSERT INTO ambassador_commissions
                  (ambassador_id, tenant_id, commission_type, currency,
                   source_amount, commission_amount, description)
                VALUES (%s,%s,'upsell_bonus','NGN',0,%s,%s)
            """, (amb["id"], tenant_id, bonus, label))

    conn.commit()
    cur2.close(); cur.close(); conn.close()
