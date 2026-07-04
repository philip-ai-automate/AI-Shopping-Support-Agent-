"""
ambassador_routes.py — Ambassador portal (/ambassador/*)
Separate from the customer portal. Ambassadors register, admin approves,
then they can log in to see referrals, earnings, QR code and tier progress.
"""
import os
import io
import csv
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
                   url_for, session, flash, g, Response)

UPLOAD_FOLDER      = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'ambassador_ids')
QUAL_UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'ambassador_quals')
ALLOWED_DOC_EXTS   = {'jpg', 'jpeg', 'png', 'pdf'}

QUAL_ORDER = [
    'OND', 'HND', 'BSc/BA/BEng', 'MSc/MA/MEng', 'PhD',
    'Professional Certification', 'Other',
]

def _allowed_doc(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_DOC_EXTS

from db import get_db_connection, insert_audit_log
from lead_pipeline import (STAGE_ORDER, STAGE_LABELS, STAGE_DESCRIPTIONS,
                            next_stage, record_stage_change, get_stage_history)
from portal_utils import send_email

ambassador_bp = Blueprint("ambassador", __name__)

BRAND         = "#030C18"
BASE_URL      = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com")
COMMISSION_PC          = 0.20   # 20% ambassador commission on subscription payments
SALES_MANAGER_OVERRIDE = 0.05   # 5% override to the sales manager who recruited the ambassador
                                 # (recurring subscription commission only, not the upsell bonus)

TURNSTILE_SECRET       = os.getenv("TURNSTILE_SECRET_KEY", "")
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_MINS  = 15

UPSELL_BONUSES = {
    ("starter", "growth"): 5000,
    ("growth",  "pro"):   10000,
    ("starter", "pro"):   15000,
    ("free",    "pro"):   20000,
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


@ambassador_bp.context_processor
def inject_amb_role():
    """Make `_amb_role` available in every ambassador template (for nav gating)."""
    if not _amb_logged_in():
        return {"_amb_role": None}
    if not hasattr(g, "_cached_amb_role"):
        try:
            conn = get_db_connection()
            cur  = conn.cursor()
            cur.execute("SELECT role FROM ambassadors WHERE id=%s", (_amb_id(),))
            row = cur.fetchone()
            cur.close(); conn.close()
            g._cached_amb_role = row[0] if row else None
        except Exception:
            g._cached_amb_role = None
    return {"_amb_role": g._cached_amb_role}

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
      <p>Here's how your commissions work:</p>
      <ul style="line-height:1.9;padding-left:20px">
        <li><strong>30% commission</strong> on every subscription payment from clients who sign up
            directly through your referral link</li>
        <li><strong>20% commission</strong> on clients you refer as a lead via the Ambassador Hub
            and our team closes</li>
      </ul>
      <p>You also earn one-time upsell bonuses when referred clients upgrade:
         <strong>₦5,000</strong> (Starter→Growth),
         <strong>₦10,000</strong> (Growth→Pro),
         <strong>₦15,000</strong> (Starter→Pro),
         or <strong>₦20,000</strong> (Free→Pro).</p>

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
               text_body=(
                   f"Approved! Log in: {link}\n\n"
                   f"Your referral link: {ref_link}\n\n"
                   f"Commission rates:\n"
                   f"- 30% on clients who sign up via your referral link\n"
                   f"- 20% on leads you submit that our team closes\n\n"
                   f"Upsell bonuses: ₦5k (Starter→Growth), ₦10k (Growth→Pro), "
                   f"₦15k (Starter→Pro), ₦20k (Free→Pro)\n\n"
                   f"Join our Ambassador WhatsApp support group: {WA_GROUP_LINK}"
               ))


# ── Auth routes ────────────────────────────────────────────────────────────

@ambassador_bp.route("/ambassador/register", methods=["GET", "POST"])
def register():
    if _amb_logged_in():
        return redirect(url_for("ambassador.dashboard"))

    if request.method == "GET":
        recruiter_code = (request.args.get("recruiter") or "").strip()
        recruiter_name = None
        if recruiter_code:
            conn = get_db_connection()
            cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT first_name, last_name FROM ambassadors
                WHERE ref_code=%s AND role='sales_manager' AND status='active'
            """, (recruiter_code,))
            rec = cur.fetchone()
            cur.close(); conn.close()
            if rec:
                recruiter_name = f"{rec['first_name']} {rec['last_name']}"
            else:
                recruiter_code = None
        return render_template("ambassador/register.html",
                               recruiter_code=recruiter_code, recruiter_name=recruiter_name)

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
    if request.form.get("contract_agreed") != "1":
        return _bail("You must read and accept the Ambassador Partnership Agreement to continue.")

    # ── Minimum education: OND ───────────────────────────────────────────────
    if qualification not in QUAL_ORDER:
        return _bail("Please select a valid educational qualification.")

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

    MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

    # ── ID document upload ───────────────────────────────────────────────────
    file = request.files.get("id_document")
    if not file or not file.filename:
        return _bail("Please upload an identity document.")
    if not _allowed_doc(file.filename):
        return _bail("Identity document must be a JPG, PNG, or PDF file.")
    file.seek(0, 2)
    if file.tell() > MAX_UPLOAD_BYTES:
        return _bail("Identity document must be 5 MB or smaller.")
    file.seek(0)

    ext      = file.filename.rsplit('.', 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    file.save(os.path.join(UPLOAD_FOLDER, filename))
    id_doc_path = f"uploads/ambassador_ids/{filename}"

    # ── Qualification proof upload ───────────────────────────────────────────
    qual_file = request.files.get("qual_document")
    if not qual_file or not qual_file.filename:
        return _bail("Please upload proof of your educational qualification.")
    if not _allowed_doc(qual_file.filename):
        return _bail("Qualification document must be a JPG, PNG, or PDF file.")
    qual_file.seek(0, 2)
    if qual_file.tell() > MAX_UPLOAD_BYTES:
        return _bail("Qualification document must be 5 MB or smaller.")
    qual_file.seek(0)

    qual_ext      = qual_file.filename.rsplit('.', 1)[1].lower()
    qual_filename = f"{uuid.uuid4().hex}.{qual_ext}"
    os.makedirs(QUAL_UPLOAD_FOLDER, exist_ok=True)
    qual_file.save(os.path.join(QUAL_UPLOAD_FOLDER, qual_filename))
    qual_doc_path = f"uploads/ambassador_quals/{qual_filename}"

    ref_code = _generate_ref_code(first, last)
    pw_hash  = _hash_pw(pw)

    # ── Resolve recruiter (if any) ───────────────────────────────────────────
    recruiter_code = (request.form.get("recruiter") or "").strip()
    recruited_by_id = None
    if recruiter_code:
        conn0 = get_db_connection()
        cur0  = conn0.cursor()
        cur0.execute("""
            SELECT id FROM ambassadors
            WHERE ref_code=%s AND role='sales_manager' AND status='active'
        """, (recruiter_code,))
        rec_row = cur0.fetchone()
        cur0.close(); conn0.close()
        if rec_row:
            recruited_by_id = rec_row[0]

    # ── Insert ───────────────────────────────────────────────────────────────
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO ambassadors
               (first_name, last_name, email, phone, whatsapp_number, password_hash, ref_code,
                status, date_of_birth, gender, nationality, address, location,
                highest_qualification, id_document_path, id_document_type,
                bank_name, account_number, account_name, sort_code, swift_code,
                qual_document_path, recruited_by_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (first, last, email, phone, whatsapp, pw_hash, ref_code,
              dob, gender, nationality, address, location,
              qualification, id_doc_path, id_doc_type,
              bank_name, account_num, account_name, sort_code, swift_code,
              qual_doc_path, recruited_by_id))
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


# ── My Team (Sales Manager only) ────────────────────────────────────────────

def _require_sales_manager():
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    if not amb or amb.get("role") != "sales_manager":
        flash("This page is only available to Sales Managers.", "warning")
        return redirect(url_for("ambassador.dashboard"))
    return None

@ambassador_bp.route("/ambassador/team")
def team():
    r = _require_sales_manager()
    if r: return r
    amb = _get_ambassador(_amb_id())

    date_from = (request.args.get("from") or "").strip() or None
    date_to   = (request.args.get("to") or "").strip() or None

    # Date filter applied to ambassador_commissions.created_at (as a date, inclusive both ends)
    range_clause = ""
    range_params = []
    if date_from:
        range_clause += " AND ac.created_at::date >= %s"
        range_params.append(date_from)
    if date_to:
        range_clause += " AND ac.created_at::date <= %s"
        range_params.append(date_to)

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
        SELECT a.id, a.first_name, a.last_name, a.email, a.ref_code, a.status, a.created_at,
               (SELECT COUNT(*) FROM tenants t WHERE t.ref_code=a.ref_code AND t.status='active') AS active_clients,
               (SELECT COALESCE(SUM(ac.commission_amount),0) FROM ambassador_commissions ac
                WHERE ac.ambassador_id=a.id {range_clause}) AS recruit_earned,
               (SELECT COALESCE(SUM(ac.commission_amount),0) FROM ambassador_commissions ac
                JOIN tenants t ON t.id = ac.tenant_id
                WHERE ac.ambassador_id=%s AND ac.commission_type='override' AND t.ref_code=a.ref_code {range_clause}) AS override_earned
        FROM ambassadors a
        WHERE a.recruited_by_id = %s
        ORDER BY CASE a.status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, a.created_at DESC
    """, [*range_params, amb["id"], *range_params, amb["id"]])
    recruits = cur.fetchall() or []

    cur.execute(f"""
        SELECT date_trunc('month', ac.created_at) AS month, COALESCE(SUM(ac.commission_amount),0) AS total
        FROM ambassador_commissions ac
        WHERE ac.ambassador_id=%s AND ac.commission_type='override' {range_clause}
        GROUP BY month
        ORDER BY month DESC
        {"" if (date_from or date_to) else "LIMIT 12"}
    """, [amb["id"], *range_params])
    monthly_override = cur.fetchall() or []

    cur.execute(f"""
        SELECT t.ref_code AS recruit_ref_code, date_trunc('month', ac.created_at) AS month,
               SUM(ac.commission_amount) AS total
        FROM ambassador_commissions ac
        JOIN tenants t ON t.id = ac.tenant_id
        WHERE ac.ambassador_id=%s AND ac.commission_type='override' {range_clause}
        GROUP BY t.ref_code, month
        ORDER BY month DESC
    """, [amb["id"], *range_params])
    per_recruit_rows = cur.fetchall() or []
    cur.close(); conn.close()

    per_recruit_monthly = {}
    for row in per_recruit_rows:
        per_recruit_monthly.setdefault(row["recruit_ref_code"], []).append(
            {"month": row["month"], "total": float(row["total"] or 0)}
        )

    recruiter_link = f"{BASE_URL}/ambassador/register?recruiter={amb['ref_code']}"
    return render_template("ambassador/team.html", amb=amb, recruits=recruits,
                           monthly_override=monthly_override, per_recruit_monthly=per_recruit_monthly,
                           recruiter_link=recruiter_link, date_from=date_from, date_to=date_to)


@ambassador_bp.route("/ambassador/team/export")
def team_export():
    r = _require_sales_manager()
    if r: return r
    amb = _get_ambassador(_amb_id())

    date_from = (request.args.get("from") or "").strip() or None
    date_to   = (request.args.get("to") or "").strip() or None

    range_clause = ""
    range_params = []
    if date_from:
        range_clause += " AND ac.created_at::date >= %s"
        range_params.append(date_from)
    if date_to:
        range_clause += " AND ac.created_at::date <= %s"
        range_params.append(date_to)

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
        SELECT rec.first_name, rec.last_name, rec.ref_code, rec.status,
               date_trunc('month', ac.created_at) AS month, SUM(ac.commission_amount) AS total
        FROM ambassador_commissions ac
        JOIN tenants t ON t.id = ac.tenant_id
        JOIN ambassadors rec ON rec.ref_code = t.ref_code
        WHERE ac.ambassador_id=%s AND ac.commission_type='override' AND rec.recruited_by_id=%s {range_clause}
        GROUP BY rec.id, rec.first_name, rec.last_name, rec.ref_code, rec.status, month
        ORDER BY month DESC, rec.first_name, rec.last_name
    """, [amb["id"], amb["id"], *range_params])
    rows = cur.fetchall() or []
    cur.close(); conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Month", "Recruit Name", "Ref Code", "Recruit Status", "Your Override Earned (NGN)"])
    for row in rows:
        writer.writerow([
            row["month"].strftime("%B %Y"),
            f"{row['first_name']} {row['last_name']}",
            row["ref_code"],
            row["status"],
            f"{float(row['total'] or 0):.2f}",
        ])

    range_label = ""
    if date_from or date_to:
        range_label = f"_{date_from or 'start'}_to_{date_to or 'end'}"
    filename = f"team-earnings-{amb['ref_code']}{range_label}.csv"

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _recruit_owned_by_me(recruit_id: int):
    """Return the recruit row if it belongs to the logged-in sales manager, else None."""
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM ambassadors WHERE id=%s AND recruited_by_id=%s", (recruit_id, _amb_id()))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row

@ambassador_bp.route("/ambassador/team/<int:recruit_id>/approve", methods=["POST"])
def team_approve(recruit_id: int):
    r = _require_sales_manager()
    if r: return r
    recruit = _recruit_owned_by_me(recruit_id)
    if not recruit:
        flash("Ambassador not found in your team.", "danger")
        return redirect(url_for("ambassador.team"))

    manager = _get_ambassador(_amb_id())
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE ambassadors
           SET status='active', approved_at=NOW(), approved_by=%s, partnership_start=%s
         WHERE id=%s
    """, (f"{manager['first_name']} {manager['last_name']} (Sales Manager)", date.today(), recruit_id))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(
        admin_username=f"{manager['first_name']} {manager['last_name']} (Sales Manager)",
        action="ambassador_approve",
        details={"ambassador_id": recruit_id, "ambassador_name": f"{recruit['first_name']} {recruit['last_name']}",
                 "ref_code": recruit['ref_code'], "old_status": recruit['status'], "new_status": "active"},
    )
    try:
        from ambassador_demo import create_ambassador_demo
        create_ambassador_demo(recruit['id'], recruit['first_name'], recruit['ref_code'])
    except Exception as _e:
        print("⚠️ team_approve demo tenant creation failed:", _e)
    try:
        _send_approved_email(recruit['first_name'], recruit['email'], recruit['ref_code'])
    except Exception as _e:
        print("⚠️ team_approve approval email failed:", _e)
    flash(f"{recruit['first_name']} {recruit['last_name']} approved.", "success")
    return redirect(url_for("ambassador.team"))

@ambassador_bp.route("/ambassador/team/<int:recruit_id>/reject", methods=["POST"])
def team_reject(recruit_id: int):
    r = _require_sales_manager()
    if r: return r
    recruit = _recruit_owned_by_me(recruit_id)
    if not recruit:
        flash("Ambassador not found in your team.", "danger")
        return redirect(url_for("ambassador.team"))
    if recruit["status"] != "pending":
        flash("Only pending applications can be rejected.", "warning")
        return redirect(url_for("ambassador.team"))

    reason  = (request.form.get("reason") or "").strip() or None
    manager = _get_ambassador(_amb_id())
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE ambassadors SET status='rejected', rejected_at=NOW(), rejected_reason=%s WHERE id=%s
    """, (reason, recruit_id))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(
        admin_username=f"{manager['first_name']} {manager['last_name']} (Sales Manager)",
        action="ambassador_reject",
        details={"ambassador_id": recruit_id, "ambassador_name": f"{recruit['first_name']} {recruit['last_name']}",
                 "ref_code": recruit['ref_code'], "old_status": recruit['status'], "new_status": "rejected",
                 "reason": reason},
    )
    flash(f"{recruit['first_name']} {recruit['last_name']}'s application was rejected.", "warning")
    return redirect(url_for("ambassador.team"))

@ambassador_bp.route("/ambassador/team/<int:recruit_id>/suspend", methods=["POST"])
def team_suspend(recruit_id: int):
    r = _require_sales_manager()
    if r: return r
    recruit = _recruit_owned_by_me(recruit_id)
    if not recruit:
        flash("Ambassador not found in your team.", "danger")
        return redirect(url_for("ambassador.team"))

    manager = _get_ambassador(_amb_id())
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE ambassadors SET status='suspended' WHERE id=%s", (recruit_id,))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(
        admin_username=f"{manager['first_name']} {manager['last_name']} (Sales Manager)",
        action="ambassador_suspend",
        details={"ambassador_id": recruit_id, "ambassador_name": f"{recruit['first_name']} {recruit['last_name']}",
                 "ref_code": recruit['ref_code'], "old_status": recruit['status'], "new_status": "suspended"},
    )
    flash(f"{recruit['first_name']} {recruit['last_name']} suspended.", "warning")
    return redirect(url_for("ambassador.team"))

@ambassador_bp.route("/ambassador/team/<int:recruit_id>/reactivate", methods=["POST"])
def team_reactivate(recruit_id: int):
    r = _require_sales_manager()
    if r: return r
    recruit = _recruit_owned_by_me(recruit_id)
    if not recruit:
        flash("Ambassador not found in your team.", "danger")
        return redirect(url_for("ambassador.team"))

    manager = _get_ambassador(_amb_id())
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE ambassadors SET status='active' WHERE id=%s", (recruit_id,))
    conn.commit()
    cur.close(); conn.close()
    insert_audit_log(
        admin_username=f"{manager['first_name']} {manager['last_name']} (Sales Manager)",
        action="ambassador_reactivate",
        details={"ambassador_id": recruit_id, "ambassador_name": f"{recruit['first_name']} {recruit['last_name']}",
                 "ref_code": recruit['ref_code'], "old_status": recruit['status'], "new_status": "active"},
    )
    flash(f"{recruit['first_name']} {recruit['last_name']} reactivated.", "success")
    return redirect(url_for("ambassador.team"))


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
    ("common-questions",    "Common Questions",        "💡", "Answers to the questions prospects ask most — ready to copy and send, or share as a PDF"),
    ("demo-to-business",    "Demo to Business",        "🎬", "Show a live PhiXtra AI demo using the profitbuyz.com WhatsApp account — iPhone questions only for now"),
    ("client-requirements", "Client Requirements",     "📋", "Four things a shop must have ready before they can go live on PhiXtra"),
    ("after-signup",        "After They Sign Up",      "🤝", "Five things to do after a shop signs up to make sure they go live fast and you start earning"),
    ("how-you-earn",        "How You Earn",            "💰", "Your 30% monthly cut, how your level goes up, and the bonuses you earn when shops upgrade"),
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


@ambassador_bp.route("/ambassador/demo-qr")
def demo_qr_page():
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    return render_template("ambassador/demo_qr.html", amb=amb, demo_wa_number=DEMO_WA_NUMBER)


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

@ambassador_bp.route("/ambassador/faq.pdf")
def faq_pdf():
    r = _require_amb()
    if r: return r

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor, white
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, HRFlowable)
    from reportlab.lib.units import cm
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
    from reportlab.pdfgen import canvas as pdfcanvas
    import io as _io

    INK   = HexColor("#030C18")
    TEAL  = HexColor("#0d9488")
    LGREY = HexColor("#f8f9fb")
    MGREY = HexColor("#e8eaf0")
    DKGREY= HexColor("#374151")

    buf = _io.BytesIO()

    def _draw_header(c, doc):
        W, H = A4
        c.setFillColor(INK)
        c.rect(0, H - 72, W, 72, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(2*cm, H - 30, "PhiXtra AI")
        c.setFont("Helvetica", 10)
        c.drawString(2*cm, H - 46, "Common Questions & Answers for Prospects")
        c.setFillColor(TEAL)
        c.circle(2*cm - 6, H - 18, 5, fill=1, stroke=0)
        c.setFillColor(DKGREY)
        c.setFont("Helvetica", 8)
        c.drawRightString(W - 2*cm, H - 56, "share this with anyone considering PhiXtra")
        # footer
        c.setFillColor(MGREY)
        c.rect(0, 0, W, 28, fill=1, stroke=0)
        c.setFillColor(DKGREY)
        c.setFont("Helvetica", 8)
        c.drawString(2*cm, 10, "Questions? Contact support@phixtra.com  ·  phixtra.com")
        c.drawRightString(W - 2*cm, 10, f"Page {doc.page}")

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=2.8*cm, bottomMargin=1.4*cm,
        leftMargin=2*cm, rightMargin=2*cm,
    )

    Q  = ParagraphStyle("Q",  fontName="Helvetica-Bold", fontSize=12, textColor=INK,
                         spaceAfter=5, spaceBefore=14, leading=16)
    A  = ParagraphStyle("A",  fontName="Helvetica",      fontSize=10, textColor=DKGREY,
                         spaceAfter=4, leading=15, alignment=TA_JUSTIFY)
    BL = ParagraphStyle("BL", fontName="Helvetica",      fontSize=10, textColor=DKGREY,
                         spaceAfter=3, leading=14, leftIndent=14, bulletIndent=4)
    IN = ParagraphStyle("IN", fontName="Helvetica-Oblique", fontSize=9.5,
                         textColor=HexColor("#1d4ed8"), spaceAfter=4, leading=13,
                         leftIndent=12, borderPad=6, backColor=HexColor("#eff6ff"),
                         borderColor=HexColor("#bfdbfe"), borderWidth=1, borderRadius=4)
    SB = ParagraphStyle("SB", fontName="Helvetica-Bold", fontSize=10, textColor=DKGREY,
                         spaceAfter=3, spaceBefore=6, leading=14)

    QA = [
        ("🤖  How does PhiXtra actually work?",
         [("p", "Imagine hiring a smart, tireless customer service rep who knows every single product you sell, never takes a day off, never sleeps, and can handle hundreds of customers at once — all on your existing WhatsApp number. That is PhiXtra."),
          ("sb", "Step 1 — You connect your WhatsApp"),
          ("p", "Your business WhatsApp number is linked to PhiXtra using the official Meta Business API. Nothing changes for your customers — they still message the same number they always have."),
          ("sb", "Step 2 — You upload your products"),
          ("p", "You add your products, prices, and any information you want the AI to know. Think of it as briefing your new staff member. The more detail you give, the better it performs."),
          ("sb", "Step 3 — The AI goes live"),
          ("p", "From that moment, every customer message is read and answered by the AI within seconds. A customer asks \"what is the price of Samsung A55?\" — the AI checks your product list and replies immediately with the price, specs, and availability. Even at midnight. Even on Sundays."),
          ("sb", "Step 4 — You stay in control"),
          ("p", "Nothing is hidden from you. You can log in at any time, read every conversation, update your products, or take over a conversation yourself if a customer needs personal attention. The AI works for you — you are always the boss."),
         ]),
        ("🔒  How does PhiXtra handle my customer data?",
         [("p", "This is one of the most important questions to ask any software you trust with your business — and we want to answer it fully."),
          ("sb", "Your data is yours. Full stop."),
          ("p", "When a customer messages your WhatsApp, that conversation is stored on your PhiXtra account. No other business can see it. No other business has access to it. Your customer list, your chat history, your product prices — all of it belongs only to you."),
          ("sb", "What PhiXtra does with your data:"),
          ("bl", "✓  Reads your product information to answer customer questions"),
          ("bl", "✓  Stores your conversations so you can review them"),
          ("bl", "✓  Uses your chat history to improve responses within your account only"),
          ("sb", "What PhiXtra does NOT do:"),
          ("bl", "✗  Sell your data to anyone"),
          ("bl", "✗  Share your customer information with other businesses"),
          ("bl", "✗  Use your data to train public AI models"),
          ("bl", "✗  Access your account without your permission"),
          ("sb", "You are always in control:"),
          ("p", "You can export your data, delete conversations, or close your account at any time. When you delete your account, your data is permanently removed from our servers. All data is stored on secure, encrypted servers using the official Meta Business API infrastructure."),
         ]),
        ("📱  Is it safe to connect my WhatsApp number?",
         [("p", "Completely. PhiXtra uses the official Meta (WhatsApp) Business API — the same approved system that banks, fintech companies, and large corporations use to communicate with customers on WhatsApp."),
          ("p", "This is NOT a hack, a third-party workaround, or anything that violates WhatsApp's rules. Your WhatsApp number remains 100% yours. If you ever decide to stop using PhiXtra, you simply disconnect the number and it returns to working as a normal WhatsApp line immediately — nothing is lost."),
         ]),
        ("❓  What if the AI gives a wrong answer to a customer?",
         [("p", "The AI can only say what it knows — and it knows what you tell it. If a customer asks about a product that is not in your product list, the AI will say it does not have that information and invite the customer to ask again or call directly."),
          ("p", "If the AI ever gives outdated or incorrect information, it is almost always because the product list has not been updated. Fix the price or product detail in your PhiXtra portal, and from that second, the AI uses the new information."),
          ("p", "You can read every conversation in your portal inbox, so nothing is hidden from you. If a conversation needs a human touch, you can jump in directly and the AI steps aside. Most business owners find that within the first two weeks, they rarely need to correct anything."),
         ]),
        ("👁️  Will my customers know they are talking to AI?",
         [("p", "That is entirely your choice as the business owner."),
          ("p", "Some businesses are fully transparent — they set the AI up to introduce itself as a virtual assistant. Others give the AI a human name and customers never ask. What matters most to customers is getting a fast, accurate answer — and PhiXtra delivers that either way."),
          ("tip", "Customers are far more frustrated by slow replies and unanswered messages than they are by talking to a well-trained AI. PhiXtra solves the problem that actually loses you sales."),
         ]),
        ("🕐  What happens when my shop is closed?",
         [("p", "The AI never closes. Whether it is 2am on a Saturday, Christmas Day, or a public holiday — every customer message is answered within seconds. No customer waits. No sale is lost because nobody was there to reply."),
          ("p", "Most impulse purchases happen in the evening when people are relaxed and browsing on their phones. These are exactly the hours that traditional shops miss. PhiXtra captures every one of those customers."),
          ("tip", "Business owners who switch to PhiXtra often say: \"I woke up to three orders I didn't even know about.\""),
         ]),
        ("💳  Is there a free trial? What does it cost?",
         [("p", "Yes — you can start for free with no credit card required. You get full access to try the platform and see how it works with your business before spending a single naira."),
          ("p", "After your free trial, choose the plan that fits your business:"),
          ("tbl", [
              ["Plan", "Price/Month", "Best For"],
              ["Starter", "₦25,000", "Small shops, getting started"],
              ["Growth",  "₦75,000", "Busier shops, higher volume"],
              ["Pro",     "₦200,000","High-volume businesses, advanced features"],
          ]),
          ("p", "All plans include unlimited AI replies to customer messages. There is no contract and no cancellation fee. Most business owners recover the monthly cost within the first week from sales that would otherwise have been missed."),
         ]),
        ("🔌  How long does setup take?",
         [("p", "For phone and computer shops: about 5 minutes. You create your account, upload your product list, and connect your WhatsApp number. The AI is live from that moment."),
          ("p", "For other types of businesses — fashion, food, logistics, services — the PhiXtra team will personally configure your account within 48 hours at no extra charge."),
          ("p", "Your PhiXtra Ambassador will guide you through every step and can stay on a call with you while you set up."),
         ]),
    ]

    story = [Spacer(1, 0.3*cm)]

    for question, body in QA:
        story.append(Paragraph(question, Q))
        story.append(HRFlowable(width="100%", thickness=1, color=MGREY, spaceAfter=6))
        for kind, content in body:
            if kind == "p":
                story.append(Paragraph(content, A))
            elif kind == "sb":
                story.append(Paragraph(content, SB))
            elif kind == "bl":
                story.append(Paragraph(content, BL))
            elif kind == "tip":
                story.append(Paragraph(content, IN))
            elif kind == "tbl":
                col_w = [(A4[0] - 4*cm) / 3] * 3
                tbl = Table(content, colWidths=col_w)
                tbl.setStyle(TableStyle([
                    ("BACKGROUND", (0,0), (-1,0), INK),
                    ("TEXTCOLOR",  (0,0), (-1,0), white),
                    ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
                    ("FONTSIZE",   (0,0), (-1,-1), 9.5),
                    ("ROWBACKGROUNDS", (0,1), (-1,-1), [white, LGREY]),
                    ("GRID", (0,0), (-1,-1), 0.5, MGREY),
                    ("TOPPADDING",    (0,0), (-1,-1), 7),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 7),
                    ("LEFTPADDING",   (0,0), (-1,-1), 10),
                ]))
                story.append(Spacer(1, 4))
                story.append(tbl)
                story.append(Spacer(1, 6))
        story.append(Spacer(1, 8))

    story.append(HRFlowable(width="100%", thickness=1, color=MGREY, spaceBefore=10, spaceAfter=8))
    story.append(Paragraph(
        "Still have questions? Ask your PhiXtra Ambassador — they are trained to help you, "
        "or they will get the PhiXtra team to answer within a few hours.",
        ParagraphStyle("foot", fontName="Helvetica-Oblique", fontSize=9.5,
                       textColor=HexColor("#6b7280"), alignment=TA_CENTER, leading=14)
    ))

    doc.build(story, onFirstPage=_draw_header, onLaterPages=_draw_header)
    buf.seek(0)
    from flask import Response as _Resp
    return _Resp(
        buf.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=PhiXtra-AI-FAQ.pdf"}
    )


@ambassador_bp.route("/ambassador/leads", methods=["GET", "POST"])
def leads():
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())

    if request.method == "POST":
        business_name = (request.form.get("business_name") or "").strip()
        industry      = (request.form.get("industry")      or "").strip() or None
        contact_name  = (request.form.get("contact_name")  or "").strip() or None
        phone         = (request.form.get("phone")         or "").strip() or None
        email         = (request.form.get("email")         or "").strip() or None
        notes         = (request.form.get("notes")         or "").strip() or None

        if not business_name:
            flash("Business name is required.", "danger")
            return redirect(url_for("ambassador.leads"))

        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO ambassador_leads
              (ambassador_id, business_name, industry, contact_name, phone, email, notes, stage)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'lead')
            RETURNING id
        """, (amb["id"], business_name, industry, contact_name, phone, email, notes))
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.close(); conn.close()
        record_stage_change(new_id, None, "lead", f"{amb['first_name']} {amb['last_name']}")
        flash("Lead added to your pipeline.", "success")
        return redirect(url_for("ambassador.leads"))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM ambassador_leads
        WHERE ambassador_id=%s AND dropped_at IS NULL
        ORDER BY CASE stage
            WHEN 'lead' THEN 0 WHEN 'contacted' THEN 1 WHEN 'demo_done' THEN 2
            WHEN 'requirements_confirmed' THEN 3 WHEN 'onboarding' THEN 4
            WHEN 'active_client' THEN 5 WHEN 'support' THEN 6 ELSE 7 END,
            created_at DESC
    """, (amb["id"],))
    pipeline_leads = [dict(l) for l in cur.fetchall()]

    cur.execute("""
        SELECT * FROM ambassador_leads WHERE ambassador_id=%s AND dropped_at IS NOT NULL
        ORDER BY dropped_at DESC
    """, (amb["id"],))
    dropped_leads = [dict(l) for l in cur.fetchall()]

    # Tenants referred by this ambassador, available to link at the Active Client stage
    cur.execute("""
        SELECT t.id, t.name FROM tenants t
        WHERE t.ref_code=%s AND t.id NOT IN (
            SELECT tenant_id FROM ambassador_leads WHERE tenant_id IS NOT NULL
        )
        ORDER BY t.created_at DESC
    """, (amb["ref_code"],))
    linkable_tenants = [dict(t) for t in cur.fetchall()]

    # Self-closed clients (referral link signups, tracked separately from the manual pipeline)
    cur.execute("""
        SELECT t.name, t.status, p.name AS plan_name, p.price_ngn,
               t.created_at,
               COALESCE(SUM(ac.commission_amount), 0) AS earned
        FROM tenants t
        LEFT JOIN plans p ON p.id = t.plan_id
        LEFT JOIN ambassador_commissions ac ON ac.tenant_id = t.id AND ac.ambassador_id=%s
        WHERE t.ref_code = %s
        GROUP BY t.id, t.name, t.status, p.name, p.price_ngn, t.created_at
        ORDER BY t.created_at DESC
    """, (amb["id"], amb["ref_code"]))
    self_closed = [dict(r) for r in cur.fetchall()]

    cur.close(); conn.close()

    stage_counts = {s: 0 for s in STAGE_ORDER}
    for l in pipeline_leads:
        stage_counts[l["stage"]] = stage_counts.get(l["stage"], 0) + 1

    return render_template("ambassador/leads.html",
        amb=amb, pipeline_leads=pipeline_leads, dropped_leads=dropped_leads,
        linkable_tenants=linkable_tenants, self_closed=self_closed, stage_counts=stage_counts,
        stage_order=STAGE_ORDER, stage_labels=STAGE_LABELS, stage_descriptions=STAGE_DESCRIPTIONS,
        next_stage=next_stage)


@ambassador_bp.route("/ambassador/leads/<int:lead_id>/advance", methods=["POST"])
def lead_advance(lead_id: int):
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM ambassador_leads WHERE id=%s AND ambassador_id=%s", (lead_id, amb["id"]))
    lead = cur.fetchone()
    if not lead:
        cur.close(); conn.close()
        flash("Lead not found.", "danger")
        return redirect(url_for("ambassador.leads"))

    target = next_stage(lead["stage"])
    if not target:
        cur.close(); conn.close()
        flash("This lead is already at the final stage.", "warning")
        return redirect(url_for("ambassador.leads"))

    f = request.form
    updates = {"stage": target}

    if target == "contacted":
        contact_channel  = (f.get("contact_channel") or "").strip()
        contact_date     = (f.get("contact_date") or "").strip()
        contact_response = (f.get("contact_response") or "").strip()
        if not contact_channel or not contact_date:
            cur.close(); conn.close()
            flash("Contact channel and date are required.", "danger")
            return redirect(url_for("ambassador.leads"))
        updates.update(contact_channel=contact_channel, contact_date=contact_date,
                       contact_response=contact_response or None)

    elif target == "demo_done":
        demo_date     = (f.get("demo_date") or "").strip()
        demo_reaction = (f.get("demo_reaction") or "").strip()
        if not demo_date:
            cur.close(); conn.close()
            flash("Demo date is required.", "danger")
            return redirect(url_for("ambassador.leads"))
        updates.update(demo_date=demo_date, demo_reaction=demo_reaction or None)

    elif target == "requirements_confirmed":
        req_phone    = f.get("req_phone") == "1"
        req_meta     = f.get("req_meta_account") == "1"
        req_whatsapp = f.get("req_whatsapp_connected") == "1"
        req_products = f.get("req_product_list") == "1"
        if not (req_phone and req_meta and req_whatsapp and req_products):
            cur.close(); conn.close()
            flash("All 4 requirements must be confirmed before advancing.", "danger")
            return redirect(url_for("ambassador.leads"))
        updates.update(req_phone=True, req_meta_account=True,
                       req_whatsapp_connected=True, req_product_list=True)

    elif target == "onboarding":
        onboarding_date  = (f.get("onboarding_date") or "").strip()
        onboarding_notes = (f.get("onboarding_notes") or "").strip()
        if not onboarding_date:
            cur.close(); conn.close()
            flash("Onboarding date is required.", "danger")
            return redirect(url_for("ambassador.leads"))
        updates.update(onboarding_date=onboarding_date, onboarding_notes=onboarding_notes or None)

    elif target == "active_client":
        tenant_id_raw = (f.get("tenant_id") or "").strip()
        tenant_id = int(tenant_id_raw) if tenant_id_raw.isdigit() else None
        if tenant_id:
            cur.execute("SELECT id FROM tenants WHERE id=%s AND ref_code=%s", (tenant_id, amb["ref_code"]))
            if not cur.fetchone():
                cur.close(); conn.close()
                flash("Selected client doesn't match your referral code.", "danger")
                return redirect(url_for("ambassador.leads"))
        updates["tenant_id"] = tenant_id

    set_clause = ", ".join(f"{k}=%s" for k in updates)
    cur2 = conn.cursor()
    cur2.execute(f"UPDATE ambassador_leads SET {set_clause} WHERE id=%s",
                list(updates.values()) + [lead_id])
    conn.commit()
    cur2.close(); cur.close(); conn.close()

    record_stage_change(lead_id, lead["stage"], target, f"{amb['first_name']} {amb['last_name']}")
    flash(f"{lead['business_name']} moved to {STAGE_LABELS[target]}.", "success")
    return redirect(url_for("ambassador.leads"))


@ambassador_bp.route("/ambassador/leads/<int:lead_id>/drop", methods=["POST"])
def lead_drop(lead_id: int):
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    reason = (request.form.get("reason") or "").strip() or None

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM ambassador_leads WHERE id=%s AND ambassador_id=%s", (lead_id, amb["id"]))
    lead = cur.fetchone()
    if not lead:
        cur.close(); conn.close()
        flash("Lead not found.", "danger")
        return redirect(url_for("ambassador.leads"))

    cur2 = conn.cursor()
    cur2.execute("UPDATE ambassador_leads SET dropped_at=NOW(), dropped_reason=%s WHERE id=%s",
                (reason, lead_id))
    conn.commit()
    cur2.close(); cur.close(); conn.close()
    record_stage_change(lead_id, lead["stage"], "dropped", f"{amb['first_name']} {amb['last_name']}", reason)
    flash(f"{lead['business_name']} marked as dropped.", "warning")
    return redirect(url_for("ambassador.leads"))


@ambassador_bp.route("/ambassador/leads/<int:lead_id>/tickets", methods=["POST"])
def lead_add_ticket(lead_id: int):
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    subject = (request.form.get("subject") or "").strip()
    notes   = (request.form.get("notes") or "").strip() or None
    if not subject:
        flash("Ticket subject is required.", "danger")
        return redirect(url_for("ambassador.leads"))

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT 1 FROM ambassador_leads WHERE id=%s AND ambassador_id=%s", (lead_id, amb["id"]))
    if not cur.fetchone():
        cur.close(); conn.close()
        flash("Lead not found.", "danger")
        return redirect(url_for("ambassador.leads"))
    cur.execute("""
        INSERT INTO lead_support_tickets (lead_id, subject, notes, created_by)
        VALUES (%s, %s, %s, %s)
    """, (lead_id, subject, notes, f"{amb['first_name']} {amb['last_name']}"))
    conn.commit()
    cur.close(); conn.close()
    flash("Ticket logged.", "success")
    return redirect(url_for("ambassador.leads"))


@ambassador_bp.route("/ambassador/leads/<int:lead_id>/review", methods=["POST"])
def lead_mark_reviewed(lead_id: int):
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE ambassador_leads SET last_reviewed_at=NOW() WHERE id=%s AND ambassador_id=%s",
                (lead_id, amb["id"]))
    conn.commit()
    cur.close(); conn.close()
    flash("Marked as reviewed.", "success")
    return redirect(url_for("ambassador.leads"))


@ambassador_bp.route("/ambassador/leads/<int:lead_id>/history")
def lead_history(lead_id: int):
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT 1 FROM ambassador_leads WHERE id=%s AND ambassador_id=%s", (lead_id, amb["id"]))
    owned = cur.fetchone()
    cur.close(); conn.close()
    if not owned:
        from flask import jsonify
        return jsonify({"error": "Not found"}), 404
    history = get_stage_history(lead_id)
    from flask import jsonify
    return jsonify([
        {"from_stage": h["from_stage"], "to_stage": h["to_stage"], "changed_by": h["changed_by"],
         "notes": h["notes"], "created_at": h["created_at"].isoformat() if h["created_at"] else ""}
        for h in history
    ])


@ambassador_bp.route("/ambassador/contract")
def contract():
    r = _require_amb()
    if r: return r
    amb = _get_ambassador(_amb_id())
    return render_template("ambassador/contract.html", amb=amb)


def record_ambassador_commission(tenant_id: int, plan_id: int, prev_plan_id: int,
                                  amount, currency: str) -> None:
    """
    Called after a successful subscription payment.
    - Records 20% commission if ambassador is eligible.
    - Records upsell bonus for Starter→Growth (₦5k), Growth→Pro (₦10k), Starter→Pro (₦15k), Free→Pro (₦20k).
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

        # 5% override to the sales manager who recruited this ambassador (if any, and still active)
        if amb.get("recruited_by_id"):
            cur3 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur3.execute("""
                SELECT id, first_name, last_name FROM ambassadors
                WHERE id=%s AND role='sales_manager' AND status='active'
            """, (amb["recruited_by_id"],))
            manager = cur3.fetchone()
            cur3.close()
            if manager:
                override = round(float(amount) * SALES_MANAGER_OVERRIDE, 2)
                cur2.execute("""
                    INSERT INTO ambassador_commissions
                      (ambassador_id, tenant_id, commission_type, currency, source_amount, commission_amount, description)
                    VALUES (%s,%s,'override',%s,%s,%s,%s)
                """, (manager["id"], tenant_id, currency, float(amount), override,
                      f"5% override — {amb['first_name']} {amb['last_name']}'s referral"))

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
