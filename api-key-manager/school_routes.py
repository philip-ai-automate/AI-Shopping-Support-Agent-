"""
school_routes.py — PhiXtra School (school.phixtra.com)
Blueprint prefix: / (host-matched by LiteSpeed to school.phixtra.com)
"""
import calendar
import csv
import io
import os
import threading
import datetime
from functools import wraps

import psycopg2.extras
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, jsonify, make_response,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from db import get_db_connection
from school_wa import (
    send_attendance_alert, send_fee_reminder,
    send_broadcast, send_wa_text, get_school_template, check_template_status,
)
from school_rag import sync_qa_chunk, process_document, delete_document
import school_payments
import school_billing

_KB_UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads", "school_documents")
_KB_ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}

school_bp = Blueprint("school", __name__, template_folder="templates")

# ── Auth helpers ───────────────────────────────────────────────────────────────

def _logged_in() -> bool:
    return bool(session.get("school_logged_in"))

def _school_id() -> int | None:
    sid = session.get("school_id")
    return int(sid) if sid else None

def _staff_id() -> int | None:
    sid = session.get("school_staff_id")
    return int(sid) if sid else None

def _school_role() -> str:
    return session.get("school_role", "teacher")

def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _logged_in():
            return redirect(url_for("school.login"))
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _logged_in():
            return redirect(url_for("school.login"))
        if _school_role() not in ("admin", "bursar"):
            flash("You do not have permission to access that page.", "warning")
            return redirect(url_for("school.dashboard"))
        return f(*args, **kwargs)
    return decorated

# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_school(school_id: int) -> dict | None:
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM school_profiles WHERE id=%s", (school_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row

def _get_staff(staff_id: int) -> dict | None:
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM school_staff WHERE id=%s", (staff_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row

def _get_classes(school_id: int) -> list[str]:
    """Return distinct class names in this school, ordered naturally."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "SELECT DISTINCT class_name FROM school_students "
        "WHERE school_id=%s AND is_active=TRUE ORDER BY class_name",
        (school_id,)
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [r[0] for r in rows]

def _get_school_plan(school_id: int) -> dict:
    """Fetch this school's plan row (falls back to Free-shaped dict on any error)."""
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT p.* FROM school_plans p
            JOIN school_profiles s ON s.plan_id = p.id
            WHERE s.id = %s
        """, (school_id,))
        plan = cur.fetchone()
        if plan:
            return dict(plan)
    except Exception:
        pass
    finally:
        cur.close(); conn.close()
    return {"slug": "free", "name": "Free", "student_max": 50, "staff_limit": 2,
            "feat_document_rag": False, "feat_broadcasts": False,
            "feat_custom_templates": False, "feat_priority_support": False}

def _get_school_student_count(school_id: int) -> int:
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM school_students WHERE school_id=%s AND is_active=TRUE", (school_id,))
    n = cur.fetchone()[0]
    cur.close(); conn.close()
    return n

def _check_student_cap(school_id: int, adding: int = 1) -> str | None:
    """Returns an upgrade-prompt message if adding this many students would
    exceed the school's plan student_max, else None."""
    plan = _get_school_plan(school_id)
    cap = plan.get("student_max", -1)
    if cap is None or cap == -1:
        return None
    current = _get_school_student_count(school_id)
    if current + adding > cap:
        return (f"Your {plan.get('name', 'current')} plan is capped at {cap} students "
                f"(you currently have {current}). Upgrade your plan to add more.")
    return None

def _check_staff_cap(school_id: int) -> str | None:
    """Returns an upgrade-prompt message if adding one more staff login would
    exceed the school's plan staff_limit, else None."""
    plan = _get_school_plan(school_id)
    cap = plan.get("staff_limit", -1)
    if cap is None or cap == -1:
        return None
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM school_staff WHERE school_id=%s AND is_active=TRUE", (school_id,))
    current = cur.fetchone()[0]
    cur.close(); conn.close()
    if current >= cap:
        return (f"Your {plan.get('name', 'current')} plan is capped at {cap} staff logins "
                f"(you currently have {current}). Upgrade your plan to add more.")
    return None

_SCHOOL_FEATURE_LABELS = {
    "feat_document_rag":     "Document Upload (Knowledge Base)",
    "feat_custom_templates": "Custom WhatsApp Templates",
    "feat_broadcasts":       "Broadcast Messaging",
}

def _require_school_plan_feature(school_id: int, plan_flag: str, min_plan_name: str):
    """
    Gate a route by school plan feature flag.
    Returns None if allowed, or a rendered upgrade-required page if blocked.
    """
    plan = _get_school_plan(school_id)
    if plan.get(plan_flag):
        return None
    return render_template(
        "school/upgrade_required.html",
        feature_label=_SCHOOL_FEATURE_LABELS.get(plan_flag, plan_flag.replace("feat_", "").replace("_", " ").title()),
        min_plan_name=min_plan_name,
        current_plan=plan.get("name", "Free"),
    )


def _teacher_class() -> str | None:
    """Assigned class for the logged-in teacher, or None for admin/bursar
    (no restriction) and for teachers with no class assigned yet — callers
    that need to tell "unrestricted" apart from "unassigned" should check
    _school_role() == 'teacher' separately."""
    if _school_role() != "teacher":
        return None
    staff_id = _staff_id()
    if not staff_id:
        return None
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT class_assigned FROM school_staff WHERE id=%s", (staff_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row and row[0] else None

# ── Landing / Auth ─────────────────────────────────────────────────────────────

@school_bp.route("/")
def index():
    if _logged_in():
        return redirect(url_for("school.dashboard"))
    return redirect(url_for("school.login"))


@school_bp.route("/login", methods=["GET", "POST"])
def login():
    if _logged_in():
        return redirect(url_for("school.dashboard"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT s.*, p.school_name FROM school_staff s
            JOIN school_profiles p ON p.id = s.school_id
            WHERE s.email=%s AND s.is_active=TRUE
        """, (email,))
        staff = cur.fetchone()
        if staff and check_password_hash(staff["password_hash"], password):
            cur.execute(
                "UPDATE school_staff SET last_login=NOW() WHERE id=%s", (staff["id"],)
            )
            conn.commit()
            cur.close(); conn.close()
            session["school_logged_in"]  = True
            session["school_id"]         = int(staff["school_id"])
            session["school_staff_id"]   = int(staff["id"])
            session["school_role"]       = staff["role"]
            session["school_name"]       = staff["school_name"]
            # Redirect to onboarding if not complete
            if int(staff.get("onboarding_step", 0) or 0) < 4:
                conn2 = get_db_connection()
                cur2  = conn2.cursor()
                cur2.execute("SELECT onboarding_step FROM school_profiles WHERE id=%s", (int(staff["school_id"]),))
                ob = cur2.fetchone()
                cur2.close(); conn2.close()
                step = int((ob or [0])[0] or 0)
                if step < 4:
                    return redirect(url_for("school.onboarding", step=max(1, step + 1)))
            return redirect(url_for("school.dashboard"))
        cur.close(); conn.close()
        flash("Invalid email or password.", "danger")
    return render_template("school/login.html")


@school_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("school.login"))


# ── Public fee payment (parent-facing, no login) ────────────────────────────

@school_bp.route("/pay/<token>")
def pay_page(token):
    payment = school_payments.get_payment_by_token(token)
    balance = (float(payment["total_amount"]) - float(payment["amount_paid"])) if payment else 0
    return render_template("school/pay.html",
        payment=payment, balance=max(balance, 0), token=token)


@school_bp.route("/pay/<token>/checkout", methods=["POST"])
def pay_checkout(token):
    checkout_url, error = school_payments.init_checkout(token)
    if error:
        flash(error, "danger")
        return redirect(url_for("school.pay_page", token=token))
    return redirect(checkout_url)


@school_bp.route("/pay/<token>/callback")
def pay_callback(token):
    payment = school_payments.get_payment_by_token(token)
    if not payment:
        return render_template("school/pay.html", payment=None, balance=0, token=token), 404

    if request.args.get("transaction_id"):
        gateway, ref = "flutterwave", request.args.get("transaction_id")
    else:
        gateway, ref = "paystack", (request.args.get("reference") or request.args.get("trxref"))

    ok = bool(ref) and school_payments.verify_and_record_payment(gateway, payment["school_id"], ref)
    return render_template("school/pay_result.html", payment=payment, success=ok, token=token)


@school_bp.route("/webhooks/school-paystack/<int:school_id>", methods=["POST"])
def webhook_school_paystack(school_id):
    gw = school_payments.get_gateway(school_id, "paystack")
    if not gw or not gw.get("secret_key_enc"):
        return "", 404
    secret = school_payments._decrypt_key(gw["secret_key_enc"])
    if not school_payments.verify_paystack_signature(
        secret, request.get_data(), request.headers.get("x-paystack-signature")
    ):
        return "", 403

    event = request.get_json(silent=True) or {}
    if event.get("event") == "charge.success":
        ref = (event.get("data") or {}).get("reference")
        if ref:
            school_payments.verify_and_record_payment("paystack", school_id, ref)
    return "", 200


@school_bp.route("/webhooks/school-flutterwave/<int:school_id>", methods=["POST"])
def webhook_school_flutterwave(school_id):
    gw = school_payments.get_gateway(school_id, "flutterwave")
    if not gw or not gw.get("secret_key_enc"):
        return "", 404
    secret = school_payments._decrypt_key(gw["secret_key_enc"])
    if not school_payments.verify_flutterwave_signature(secret, request.headers.get("verif-hash")):
        return "", 403

    event = request.get_json(silent=True) or {}
    if event.get("event") == "charge.completed":
        tx_id = (event.get("data") or {}).get("id")
        if tx_id:
            school_payments.verify_and_record_payment("flutterwave", school_id, str(tx_id))
    return "", 200


# ── Billing / Plan Subscription (school pays PhiXtra) ──────────────────────────
# Distinct from the /pay/* and /webhooks/school-* routes above, which are the
# PARENT-facing fee collection flow through the school's OWN gateway keys.

@school_bp.route("/billing/plans")
@require_login
def billing_plans():
    sid     = _school_id()
    school  = _get_school(sid)
    current = _get_school_plan(sid)

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM school_plans WHERE is_active=TRUE ORDER BY sort_order")
    all_plans = cur.fetchall() or []
    cur.execute("SELECT COUNT(*) AS n FROM school_staff WHERE school_id=%s AND is_active=TRUE", (sid,))
    staff_count = cur.fetchone()["n"]
    cur.execute("""
        SELECT COUNT(*) AS n FROM school_chat_history
        WHERE school_id=%s AND role='assistant' AND created_at >= %s
    """, (sid, school.get("plan_period_start")))
    msgs_used = cur.fetchone()["n"]
    cur.close(); conn.close()

    return render_template(
        "school/billing_plans.html",
        school=school, current=current, all_plans=all_plans,
        student_count=_get_school_student_count(sid),
        staff_count=staff_count, msgs_used=msgs_used,
    )


@school_bp.route("/billing/plan-upgrade", methods=["POST"])
@require_admin
def billing_plan_upgrade():
    sid       = _school_id()
    plan_slug = (request.form.get("plan_slug") or "").strip()
    cycle     = request.form.get("cycle", "termly")
    if cycle not in ("termly", "annual"):
        cycle = "termly"

    school = _get_school(sid)
    staff  = _get_staff(_staff_id())
    admin_email = (staff or {}).get("email") or ""

    checkout_url, error = school_billing.init_plan_checkout(
        school_id=sid, school_name=school.get("school_name", "your school"),
        admin_email=admin_email, plan_slug=plan_slug, cycle=cycle,
    )
    if error:
        flash(error, "danger")
        return redirect(url_for("school.billing_plans"))
    return redirect(checkout_url)


@school_bp.route("/billing/plan-upgrade/callback")
@require_login
def billing_plan_upgrade_callback():
    """Flutterwave redirect after checkout — verify and activate the plan."""
    status         = request.args.get("status", "")
    tx_ref         = request.args.get("tx_ref", "")
    transaction_id = request.args.get("transaction_id", "")

    if status != "successful" or not transaction_id:
        flash("Payment was not completed. Please try again.", "warning")
        return redirect(url_for("school.billing_plans"))

    txn = school_billing.verify_transaction(transaction_id)
    if not txn:
        flash("Payment verification failed. Contact support.", "danger")
        return redirect(url_for("school.billing_plans"))

    meta = txn.get("meta") or {}
    school_id  = int(meta.get("school_id") or 0)
    plan_id    = int(meta.get("plan_id") or 0)
    plan_slug  = meta.get("plan_slug", "")
    cycle      = meta.get("cycle", "termly")
    amount_ngn = float(meta.get("amount_ngn") or txn.get("amount") or 0)

    if not school_id or not plan_id or school_id != _school_id():
        flash("Payment verified but plan data did not match. Contact support.", "danger")
        return redirect(url_for("school.billing_plans"))

    # Revisiting this URL (browser history, refresh, bookmark) must not
    # re-extend the plan period on an already-processed payment — Flutterwave
    # verifies old transactions successfully indefinitely, so without this
    # guard the same one-time payment could be "replayed" for free renewals.
    if tx_ref and school_billing.tx_ref_already_processed(tx_ref):
        flash(f"You're on the {plan_slug.title()} plan.", "success")
        return redirect(url_for("school.billing_plans"))

    school_billing.activate_plan_subscription(
        school_id=school_id, plan_id=plan_id, cycle=cycle,
        tx_ref=tx_ref, amount=amount_ngn,
        provider_customer_id=(txn.get("customer") or {}).get("email"),
    )
    flash(f"🎉 You're now on the {plan_slug.title()} plan! Subscription activated.", "success")
    return redirect(url_for("school.billing_plans"))


@school_bp.route("/webhooks/school-plan-flutterwave", methods=["POST"])
def webhook_school_plan_flutterwave():
    """PhiXtra's own FW webhook for school subscription payments (not the
    school's own gateway — see webhook_school_flutterwave above for that)."""
    import hmac as _hmac

    fw_hash  = request.headers.get("verif-hash", "")
    expected = os.getenv("FW_WEBHOOK_HASH", "")
    if expected and not _hmac.compare_digest(fw_hash, expected):
        return "unauthorized", 401

    payload  = request.get_json(silent=True) or {}
    event    = payload.get("event", "")
    txn_data = payload.get("data", {})
    tx_ref   = txn_data.get("tx_ref", "")

    if event == "charge.completed" and txn_data.get("status") == "successful":
        if tx_ref and not school_billing.tx_ref_already_processed(tx_ref):
            meta = txn_data.get("meta") or {}
            school_id = int(meta.get("school_id") or 0)
            plan_id   = int(meta.get("plan_id") or 0)
            if school_id and plan_id:
                amount = float(txn_data.get("charged_amount") or txn_data.get("amount") or 0)
                school_billing.activate_plan_subscription(
                    school_id=school_id, plan_id=plan_id,
                    cycle=meta.get("cycle", "termly"),
                    tx_ref=tx_ref, amount=amount,
                    provider_customer_id=(txn_data.get("customer") or {}).get("email"),
                )
    return "ok", 200


@school_bp.route("/register", methods=["GET", "POST"])
def register():
    if _logged_in():
        return redirect(url_for("school.dashboard"))
    if request.method == "GET":
        ref = (request.args.get("ref") or "").strip().lower()[:30]
        return render_template("school/register.html", ref_code=ref)
    if request.method == "POST":
        school_name    = request.form.get("school_name", "").strip()
        school_type    = request.form.get("school_type", "secondary")
        state          = request.form.get("state", "").strip()
        principal_name = request.form.get("principal_name", "").strip()
        admin_name     = request.form.get("admin_name", "").strip()
        email          = request.form.get("email", "").strip().lower()
        password       = request.form.get("password", "")
        ref_code       = (request.form.get("ref_code") or "").strip().lower()[:30]
        if not all([school_name, email, password, admin_name]):
            flash("Please fill all required fields.", "danger")
            return render_template("school/register.html", ref_code=ref_code)
        conn = get_db_connection()
        cur  = conn.cursor()
        try:
            cur.execute(
                "SELECT 1 FROM school_staff WHERE email=%s", (email,)
            )
            if cur.fetchone():
                flash("An account with that email already exists.", "danger")
                return render_template("school/register.html", ref_code=ref_code)
            cur.execute("""
                INSERT INTO school_profiles
                  (school_name, school_type, state, principal_name, contact_email, ref_code)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
            """, (school_name, school_type, state, principal_name, email, ref_code or None))
            school_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO school_staff
                  (school_id, full_name, email, password_hash, role)
                VALUES (%s,%s,%s,%s,'admin')
            """, (school_id, admin_name, email, generate_password_hash(password)))
            conn.commit()
            flash("School registered! Please log in.", "success")
            return redirect(url_for("school.login"))
        except Exception as e:
            conn.rollback()
            flash(f"Registration failed: {e}", "danger")
        finally:
            cur.close(); conn.close()
        return render_template("school/register.html", ref_code=ref_code)
    return render_template("school/register.html")

# ── Dashboard ──────────────────────────────────────────────────────────────────

@school_bp.route("/dashboard")
@require_login
def dashboard():
    sid    = _school_id()
    school = _get_school(sid)
    tclass = _teacher_class()

    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return render_template("school/dashboard.html",
            school=school, today=datetime.date.today().isoformat(),
            total_students=0, total_parents=0, parents_linked=0,
            att={}, outstanding_fees=0, att_by_class=[], overdue_fees=[],
            no_parent_count=0, recent_broadcasts=[],
        )

    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    today  = datetime.date.today().isoformat()
    class_where = " AND s.class_name=%s" if tclass else ""
    class_param = [tclass] if tclass else []

    # ── Stat cards ──────────────────────────────────────────────────────────────
    cur.execute(f"""
        SELECT COUNT(*) AS n FROM school_students s
        WHERE s.school_id=%s AND s.is_active=TRUE {class_where}
    """, [sid, *class_param])
    total_students = (cur.fetchone() or {}).get("n", 0)

    cur.execute(f"""
        SELECT
          COUNT(DISTINCT p.id)                                                  AS total,
          COUNT(DISTINCT p.id) FILTER (WHERE EXISTS (
            SELECT 1 FROM school_student_parents sp WHERE sp.parent_id = p.id
          ))                                                                    AS linked
        FROM school_parents p
        JOIN school_student_parents ssp ON ssp.parent_id = p.id
        JOIN school_students s ON s.id = ssp.student_id
        WHERE p.school_id=%s {class_where}
    """ if tclass else """
        SELECT
          COUNT(*)                                                   AS total,
          COUNT(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM school_student_parents sp WHERE sp.parent_id = p.id
          ))                                                         AS linked
        FROM school_parents p WHERE p.school_id=%s
    """, [sid, *class_param])
    pr = cur.fetchone() or {}
    total_parents  = pr.get("total", 0)
    parents_linked = pr.get("linked", 0)

    cur.execute(f"""
        SELECT
          COUNT(*) FILTER (WHERE a.status='present') AS present,
          COUNT(*) FILTER (WHERE a.status='absent')  AS absent,
          COUNT(*) FILTER (WHERE a.status='late')    AS late,
          COUNT(*)                                  AS total
        FROM school_attendance a
        JOIN school_students s ON s.id = a.student_id
        WHERE a.school_id=%s AND a.attendance_date=%s {class_where}
    """, [sid, today, *class_param])
    att = cur.fetchone() or {}

    cur.execute(f"""
        SELECT COUNT(*) AS n FROM school_fee_payments fp
        JOIN school_fee_schedules fs ON fs.id = fp.schedule_id
        JOIN school_students s ON s.id = fp.student_id
        WHERE fs.school_id=%s AND fp.status IN ('unpaid','partial') {class_where}
    """, [sid, *class_param])
    outstanding_fees = (cur.fetchone() or {}).get("n", 0)

    # ── Today's attendance by class ─────────────────────────────────────────────
    cur.execute(f"""
        SELECT
          s.class_name,
          s.arm,
          COUNT(s.id)                                            AS total,
          COUNT(a.id) FILTER (WHERE a.status='present')         AS present,
          COUNT(a.id) FILTER (WHERE a.status='absent')          AS absent,
          COUNT(a.id) FILTER (WHERE a.status='late')            AS late,
          COUNT(a.id)                                            AS marked
        FROM school_students s
        LEFT JOIN school_attendance a
               ON a.student_id = s.id AND a.attendance_date = %s
        WHERE s.school_id = %s AND s.is_active = TRUE {class_where}
        GROUP BY s.class_name, s.arm
        ORDER BY s.class_name, s.arm
    """, [today, sid, *class_param])
    att_by_class = cur.fetchall()

    # ── Overdue fee schedules ───────────────────────────────────────────────────
    fee_class_where = " AND (fs.class_name=%s OR fs.class_name IS NULL)" if tclass else ""
    cur.execute(f"""
        SELECT fs.name, fs.due_date,
               COUNT(fp.id) FILTER (WHERE fp.status IN ('unpaid','partial')) AS unpaid
        FROM school_fee_schedules fs
        LEFT JOIN school_fee_payments fp ON fp.schedule_id = fs.id
        WHERE fs.school_id=%s AND fs.due_date < %s {fee_class_where}
        GROUP BY fs.id
        HAVING COUNT(fp.id) FILTER (WHERE fp.status IN ('unpaid','partial')) > 0
        ORDER BY fs.due_date
        LIMIT 5
    """, [sid, today, *class_param])
    overdue_fees = cur.fetchall()

    # ── Students with no parent ─────────────────────────────────────────────────
    cur.execute(f"""
        SELECT COUNT(*) AS n FROM school_students s
        WHERE s.school_id=%s AND s.is_active=TRUE {class_where}
          AND NOT EXISTS (SELECT 1 FROM school_student_parents sp WHERE sp.student_id=s.id)
    """, [sid, *class_param])
    no_parent_count = (cur.fetchone() or {}).get("n", 0)

    # ── Recent broadcasts ───────────────────────────────────────────────────────
    bc_class_where = " AND (target_class=%s OR target_class IS NULL)" if tclass else ""
    cur.execute(f"""
        SELECT title, target_class, sent_count, delivered_count, status, sent_at
        FROM school_broadcasts
        WHERE school_id=%s{bc_class_where} ORDER BY created_at DESC LIMIT 5
    """, [sid, *class_param])
    recent_broadcasts = cur.fetchall()

    cur.close(); conn.close()
    return render_template("school/dashboard.html",
        school=school, today=today,
        total_students=total_students,
        total_parents=total_parents,
        parents_linked=parents_linked,
        att=att,
        outstanding_fees=outstanding_fees,
        att_by_class=att_by_class,
        overdue_fees=overdue_fees,
        no_parent_count=no_parent_count,
        recent_broadcasts=recent_broadcasts,
    )

# ── Students ───────────────────────────────────────────────────────────────────

@school_bp.route("/students")
@require_login
def students():
    sid    = _school_id()
    school = _get_school(sid)
    tclass = _teacher_class()
    q      = request.args.get("q", "").strip()
    cls    = tclass if tclass else request.args.get("class", "").strip()

    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return render_template("school/students.html",
            school=school, students=[], grouped={}, classes=[],
            q=q, selected_class="", stats={}, staff_class=None,
        )

    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Summary stats
    stats_where  = ["s.school_id=%s", "s.is_active=TRUE"]
    stats_params = [sid]
    if tclass:
        stats_where.append("s.class_name=%s")
        stats_params.append(tclass)
    cur.execute(f"""
        SELECT
          COUNT(*)                                                          AS total,
          COUNT(DISTINCT class_name)                                        AS class_count,
          SUM(CASE WHEN NOT EXISTS (
            SELECT 1 FROM school_student_parents sp WHERE sp.student_id = s.id
          ) THEN 1 ELSE 0 END)                                             AS no_parent_count
        FROM school_students s
        WHERE {' AND '.join(stats_where)}
    """, stats_params)
    stats = cur.fetchone() or {}

    where  = ["s.school_id=%s", "s.is_active=TRUE"]
    params = [sid]
    if q:
        where.append("(s.full_name ILIKE %s OR s.student_number ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if cls:
        where.append("s.class_name=%s")
        params.append(cls)
    cur.execute(f"""
        SELECT s.*,
               COUNT(DISTINCT ssp.parent_id) AS parent_count
        FROM school_students s
        LEFT JOIN school_student_parents ssp ON ssp.student_id = s.id
        WHERE {' AND '.join(where)}
        GROUP BY s.id
        ORDER BY s.class_name, s.arm, s.full_name
    """, params)
    student_list = cur.fetchall()

    # Group by class+arm for the grouped view
    from collections import OrderedDict
    grouped = OrderedDict()
    for s in student_list:
        key = f"{s['class_name']} {s['arm']}"
        grouped.setdefault(key, []).append(s)

    classes = _get_classes(sid)
    cur.close(); conn.close()
    return render_template("school/students.html",
        school=school, students=student_list, grouped=grouped,
        classes=[] if tclass else classes, q=q, selected_class=cls, stats=stats,
        staff_class=tclass,
    )


@school_bp.route("/students/add", methods=["POST"])
@require_login
def students_add():
    sid = _school_id()
    first_name     = request.form.get("first_name", "").strip()
    middle_name    = request.form.get("middle_name", "").strip()
    last_name      = request.form.get("last_name", "").strip()
    full_name      = " ".join(p for p in (first_name, middle_name, last_name) if p)
    student_number = request.form.get("student_number", "").strip()
    gender         = request.form.get("gender", "")
    class_name     = request.form.get("class_name", "").strip()
    arm            = request.form.get("arm", "A").strip().upper()
    if not first_name or not last_name or not class_name:
        flash("First name, last name and class are required.", "danger")
        return redirect(url_for("school.students"))
    tclass = _teacher_class()
    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.students"))
    if tclass and class_name != tclass:
        flash("You can only add students to your own class.", "warning")
        return redirect(url_for("school.students"))
    cap_err = _check_student_cap(sid, 1)
    if cap_err:
        flash(cap_err, "warning")
        return redirect(url_for("school.billing_plans"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO school_students
          (school_id, full_name, first_name, middle_name, last_name, student_number, gender, class_name, arm)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (sid, full_name, first_name, middle_name or None, last_name, student_number or None, gender or None, class_name, arm))
    conn.commit()
    cur.close(); conn.close()
    flash(f"{full_name} added.", "success")
    return redirect(url_for("school.students"))


@school_bp.route("/students/import", methods=["POST"])
@require_login
def students_import():
    sid  = _school_id()
    tclass = _teacher_class()
    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.students"))
    file = request.files.get("csv_file")
    if not file or not file.filename.endswith(".csv"):
        flash("Please upload a CSV file.", "danger")
        return redirect(url_for("school.students"))
    text  = file.stream.read().decode("utf-8-sig")
    rows  = list(csv.DictReader(io.StringIO(text)))

    # Pass 1: validate/normalise every row before touching the DB, so the
    # cap check below counts only rows that will actually be inserted (not
    # rows a teacher's class restriction will skip, or rows missing required
    # fields) — otherwise a mixed-class CSV can be falsely rejected as
    # over-cap even though the valid subset would fit.
    valid_rows = []
    errors     = []
    for i, row in enumerate(rows, start=2):
        first = (row.get("first_name") or "").strip()
        middle = (row.get("middle_name") or "").strip()
        last  = (row.get("last_name") or "").strip()
        if not first and not last:
            # Legacy CSV format: single full_name/name column — split it.
            legacy = (row.get("full_name") or row.get("name") or "").strip()
            parts  = legacy.split()
            first  = parts[0] if parts else ""
            last   = parts[-1] if len(parts) > 1 else ""
            middle = " ".join(parts[1:-1]) if len(parts) > 2 else ""
        name = " ".join(p for p in (first, middle, last) if p)
        cls  = (row.get("class_name") or row.get("class") or "").strip()
        if not first or not last or not cls:
            errors.append(f"Row {i}: missing first name, last name, or class")
            continue
        if tclass and cls != tclass:
            errors.append(f"Row {i}: you can only import students into your own class ({tclass})")
            continue
        valid_rows.append((i, name, first, middle, last, cls, row))

    cap_err = _check_student_cap(sid, len(valid_rows))
    if cap_err:
        flash(cap_err, "warning")
        return redirect(url_for("school.billing_plans"))

    added = 0
    conn  = get_db_connection()
    cur   = conn.cursor()
    for i, name, first, middle, last, cls, row in valid_rows:
        try:
            cur.execute("""
                INSERT INTO school_students
                  (school_id, full_name, first_name, middle_name, last_name, student_number, gender, class_name, arm)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (
                sid, name, first, middle or None, last,
                (row.get("student_number") or "").strip() or None,
                (row.get("gender") or "").strip() or None,
                cls,
                (row.get("arm") or "A").strip().upper(),
            ))
            added += 1
        except Exception as e:
            errors.append(f"Row {i}: {e}")
    conn.commit()
    cur.close(); conn.close()
    flash(f"{added} students imported." + (f" {len(errors)} errors." if errors else ""), "success" if not errors else "warning")
    return redirect(url_for("school.students"))


@school_bp.route("/students/<int:student_id>/edit", methods=["POST"])
@require_admin
def students_edit(student_id):
    sid            = _school_id()
    first_name     = request.form.get("first_name", "").strip()
    middle_name    = request.form.get("middle_name", "").strip()
    last_name      = request.form.get("last_name", "").strip()
    full_name      = " ".join(p for p in (first_name, middle_name, last_name) if p)
    student_number = request.form.get("student_number", "").strip()
    class_name     = request.form.get("class_name", "").strip()
    arm            = request.form.get("arm", "A").strip()
    gender         = request.form.get("gender", "").strip()
    if not first_name or not last_name or not class_name:
        flash("First name, last name and class are required.", "danger")
        return redirect(url_for("school.students"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        """UPDATE school_students
           SET full_name=%s, first_name=%s, middle_name=%s, last_name=%s,
               student_number=%s, class_name=%s, arm=%s, gender=%s
           WHERE id=%s AND school_id=%s AND is_active=TRUE""",
        (full_name, first_name, middle_name or None, last_name,
         student_number or None, class_name, arm, gender or None, student_id, sid)
    )
    conn.commit()
    cur.close(); conn.close()
    flash("Student updated.", "success")
    return redirect(url_for("school.students"))


@school_bp.route("/students/<int:student_id>/delete", methods=["POST"])
@require_admin
def students_delete(student_id):
    sid = _school_id()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE school_students SET is_active=FALSE WHERE id=%s AND school_id=%s",
        (student_id, sid)
    )
    conn.commit()
    cur.close(); conn.close()
    flash("Student removed.", "success")
    return redirect(url_for("school.students"))


@school_bp.route("/students/<int:student_id>/promote", methods=["POST"])
@require_admin
def students_promote(student_id):
    sid       = _school_id()
    new_class = request.form.get("class_name", "").strip()
    new_arm   = request.form.get("arm", "A").strip()
    if not new_class:
        flash("Please select a destination class.", "danger")
        return redirect(url_for("school.students"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE school_students SET class_name=%s, arm=%s WHERE id=%s AND school_id=%s AND is_active=TRUE",
        (new_class, new_arm, student_id, sid)
    )
    conn.commit()
    cur.close(); conn.close()
    flash("Student promoted.", "success")
    return redirect(url_for("school.students"))


@school_bp.route("/students/promote-bulk", methods=["POST"])
@require_admin
def students_promote_bulk():
    sid        = _school_id()
    new_class  = request.form.get("class_name", "").strip()
    new_arm    = request.form.get("arm", "").strip()
    student_ids = request.form.getlist("student_ids")
    if not new_class or not student_ids:
        flash("Select students and a destination class.", "danger")
        return redirect(url_for("school.students"))
    ids = [int(i) for i in student_ids if i.isdigit()]
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        f"UPDATE school_students SET class_name=%s{', arm=%s' if new_arm else ''} "
        f"WHERE id = ANY(%s) AND school_id=%s AND is_active=TRUE",
        (new_class, new_arm, ids, sid) if new_arm else (new_class, ids, sid)
    )
    conn.commit()
    cur.close(); conn.close()
    flash(f"{len(ids)} student{'s' if len(ids) != 1 else ''} promoted to {new_class}.", "success")
    return redirect(url_for("school.students"))


@school_bp.route("/students/<int:student_id>/summary")
@require_login
def students_summary(student_id):
    sid = _school_id()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT s.id, s.full_name, s.first_name, s.middle_name, s.last_name,
               s.student_number, s.class_name, s.arm, s.gender,
               COUNT(DISTINCT ssp.parent_id) AS parent_count
        FROM school_students s
        LEFT JOIN school_student_parents ssp ON ssp.student_id = s.id
        WHERE s.id = %s AND s.school_id = %s AND s.is_active = TRUE
        GROUP BY s.id
    """, (student_id, sid))
    student = cur.fetchone()
    if not student:
        cur.close(); conn.close()
        return jsonify({"error": "Not found"}), 404
    tclass = _teacher_class()
    if _school_role() == "teacher" and student["class_name"] != tclass:
        cur.close(); conn.close()
        return jsonify({"error": "You can only view students in your own class"}), 403
    cur.execute("""
        SELECT p.full_name, p.whatsapp_number, p.relationship, p.is_opted_in
        FROM school_parents p
        JOIN school_student_parents ssp ON ssp.parent_id = p.id
        WHERE ssp.student_id = %s
        ORDER BY p.full_name
    """, (student_id,))
    parents = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({
        "id": student["id"],
        "full_name": student["full_name"],
        "first_name": student["first_name"] or "",
        "middle_name": student["middle_name"] or "",
        "last_name": student["last_name"] or "",
        "student_number": student["student_number"] or "",
        "class_name": student["class_name"],
        "arm": student["arm"] or "A",
        "gender": student["gender"] or "",
        "parent_count": int(student["parent_count"]),
        "parents": [dict(p) for p in parents],
    })


@school_bp.route("/students/<int:student_id>/attendance")
@require_login
def student_attendance(student_id):
    sid = _school_id()
    school = _get_school(sid)

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, full_name, student_number, class_name, arm
        FROM school_students
        WHERE id=%s AND school_id=%s AND is_active=TRUE
    """, (student_id, sid))
    student = cur.fetchone()
    if not student:
        cur.close(); conn.close()
        flash("Student not found.", "warning")
        return redirect(url_for("school.students"))

    # Teachers may only view attendance for students in their own class
    tclass = _teacher_class()
    if _school_role() == "teacher" and tclass != student["class_name"]:
        cur.close(); conn.close()
        flash("You can only view attendance for your own class.", "warning")
        return redirect(url_for("school.students"))

    today = datetime.date.today()
    view  = request.args.get("view", "month")
    if view not in ("month", "week"):
        view = "month"

    if view == "week":
        week_param = request.args.get("week")
        try:
            week_anchor = datetime.date.fromisoformat(week_param) if week_param else today
        except ValueError:
            week_anchor = today
        period_start = week_anchor - datetime.timedelta(days=week_anchor.weekday())  # Monday
        period_end   = period_start + datetime.timedelta(days=6)
        this_period_start = today - datetime.timedelta(days=today.weekday())
    else:
        month_str = request.args.get("month", today.strftime("%Y-%m"))
        try:
            year, month = (int(x) for x in month_str.split("-"))
            period_start = datetime.date(year, month, 1)
        except (ValueError, TypeError):
            period_start = today.replace(day=1)
        last_day   = calendar.monthrange(period_start.year, period_start.month)[1]
        period_end = period_start.replace(day=last_day)
        this_period_start = today.replace(day=1)

    cur.execute("""
        SELECT attendance_date, status FROM school_attendance
        WHERE school_id=%s AND student_id=%s AND attendance_date BETWEEN %s AND %s
    """, (sid, student_id, period_start, period_end))
    records = {r["attendance_date"].isoformat(): r["status"] for r in cur.fetchall()}
    cur.close(); conn.close()

    weeks = None
    days  = None
    if view == "week":
        days = []
        for i in range(7):
            d = period_start + datetime.timedelta(days=i)
            days.append({
                "date": d,
                "dow": d.strftime("%A"),
                "status": records.get(d.isoformat()),
                "is_today": d == today,
                "is_future": d > today,
            })
        period_label = f"{period_start.strftime('%b %d')} – {period_end.strftime('%b %d, %Y')}"
        prev_period = (period_start - datetime.timedelta(days=7)).isoformat()
        next_period = (period_start + datetime.timedelta(days=7)).isoformat()
        can_go_next = (period_start + datetime.timedelta(days=7)) <= this_period_start
    else:
        cal = calendar.Calendar(firstweekday=0)  # Monday-first
        weeks = []
        for week in cal.monthdatescalendar(period_start.year, period_start.month):
            wdays = []
            for d in week:
                in_month = d.month == period_start.month
                wdays.append({
                    "date": d,
                    "day": d.day,
                    "in_month": in_month,
                    "status": records.get(d.isoformat()) if in_month else None,
                    "is_today": d == today,
                    "is_future": d > today,
                })
            weeks.append(wdays)
        period_label = period_start.strftime("%B %Y")
        prev_period_date = (period_start - datetime.timedelta(days=1)).replace(day=1)
        next_period_date = (period_end + datetime.timedelta(days=1))
        prev_period = prev_period_date.strftime("%Y-%m")
        next_period = next_period_date.strftime("%Y-%m")
        can_go_next = next_period_date <= this_period_start

    present_n = sum(1 for v in records.values() if v == "present")
    absent_n  = sum(1 for v in records.values() if v == "absent")
    late_n    = sum(1 for v in records.values() if v == "late")
    tracked_n = len(records)
    att_pct   = round((present_n + late_n) / tracked_n * 100) if tracked_n else 0

    notable_days = sorted(
        [{"date": d, "status": s} for d, s in records.items() if s in ("absent", "late")],
        key=lambda r: r["date"], reverse=True,
    )

    return render_template("school/student_attendance.html",
        school=school, student=student, view=view,
        weeks=weeks, days=days, period_label=period_label,
        prev_period=prev_period, next_period=next_period,
        can_go_next=can_go_next,
        is_current_period=(period_start == this_period_start),
        present_n=present_n, absent_n=absent_n, late_n=late_n,
        tracked_n=tracked_n, att_pct=att_pct,
        notable_days=notable_days,
    )


@school_bp.route("/students/csv-template")
@require_login
def students_csv_template():
    """Download a blank CSV template for student import."""
    from flask import Response
    rows = [
        "first_name,middle_name,last_name,student_number,class_name,arm,gender",
        "Temi,Ayodele,Adeyemi,SCH2025001,JSS1,A,Female",
        "Emeka,Chukwudi,Okafor,SCH2025002,JSS1,A,Male",
        "Fatima,,Musa,SCH2025003,JSS2,B,Female",
    ]
    csv_text = "\n".join(rows) + "\n"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=students_template.csv"},
    )

# ── Parents ────────────────────────────────────────────────────────────────────

@school_bp.route("/parents")
@require_login
def parents():
    sid    = _school_id()
    school = _get_school(sid)
    tclass = _teacher_class()
    q      = request.args.get("q", "").strip()

    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return render_template("school/parents.html",
            school=school, parents=[], stats={}, student_options=[], q=q, staff_class=None,
        )

    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    class_exists = (
        " AND EXISTS (SELECT 1 FROM school_student_parents sp2 "
        "JOIN school_students s2 ON s2.id = sp2.student_id "
        "WHERE sp2.parent_id = p.id AND s2.class_name=%s)"
    ) if tclass else ""
    class_param = [tclass] if tclass else []

    # Summary stats
    cur.execute(f"""
        SELECT
          COUNT(*)                                                              AS total,
          COUNT(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM school_student_parents sp WHERE sp.parent_id = p.id
          ))                                                                    AS linked_count,
          COUNT(*) FILTER (WHERE NOT is_opted_in)                              AS opted_out
        FROM school_parents p
        WHERE p.school_id=%s{class_exists}
    """, [sid, *class_param])
    stats = cur.fetchone() or {}

    where  = ["p.school_id=%s"]
    params = [sid]
    if q:
        where.append("(p.full_name ILIKE %s OR p.whatsapp_number ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if tclass:
        where.append(
            "EXISTS (SELECT 1 FROM school_student_parents sp2 "
            "JOIN school_students s2 ON s2.id = sp2.student_id "
            "WHERE sp2.parent_id = p.id AND s2.class_name=%s)"
        )
        params.append(tclass)
    cur.execute(f"""
        SELECT p.*,
               COALESCE(
                 JSON_AGG(
                   JSON_BUILD_OBJECT(
                     'id', s.id, 'name', s.full_name,
                     'class', s.class_name, 'arm', s.arm
                   ) ORDER BY s.class_name, s.full_name
                 ) FILTER (WHERE s.id IS NOT NULL),
                 '[]'::json
               ) AS child_list,
               COUNT(ssp.student_id) AS child_count
        FROM school_parents p
        LEFT JOIN school_student_parents ssp ON ssp.parent_id = p.id
        LEFT JOIN school_students s ON s.id = ssp.student_id AND s.is_active=TRUE
        WHERE {' AND '.join(where)}
        GROUP BY p.id
        ORDER BY p.full_name
    """, params)
    parent_list = cur.fetchall()

    # Students for linking dropdown — teachers only see/link their own class
    opt_where  = ["school_id=%s", "is_active=TRUE"]
    opt_params = [sid]
    if tclass:
        opt_where.append("class_name=%s")
        opt_params.append(tclass)
    cur.execute(
        f"SELECT id, full_name, class_name, arm FROM school_students "
        f"WHERE {' AND '.join(opt_where)} ORDER BY class_name, full_name",
        opt_params
    )
    student_options = cur.fetchall()
    cur.close(); conn.close()
    return render_template("school/parents.html",
        school=school, parents=parent_list, stats=stats,
        student_options=student_options, q=q, staff_class=tclass,
    )


@school_bp.route("/parents/add", methods=["POST"])
@require_login
def parents_add():
    sid          = _school_id()
    tclass       = _teacher_class()
    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.parents"))
    full_name    = request.form.get("full_name", "").strip()
    wa_number    = request.form.get("whatsapp_number", "").strip()
    relationship = request.form.get("relationship", "Parent")
    student_ids  = request.form.getlist("student_ids")
    if not full_name or not wa_number:
        flash("Name and WhatsApp number are required.", "danger")
        return redirect(url_for("school.parents"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO school_parents (school_id, full_name, whatsapp_number, relationship)
        VALUES (%s,%s,%s,%s) RETURNING id
    """, (sid, full_name, wa_number, relationship))
    parent_id = cur.fetchone()[0]
    for sid_link in student_ids:
        try:
            if tclass:
                cur.execute("""
                    INSERT INTO school_student_parents (student_id, parent_id)
                    SELECT %s, %s WHERE EXISTS (
                        SELECT 1 FROM school_students WHERE id=%s AND school_id=%s AND class_name=%s
                    ) ON CONFLICT DO NOTHING
                """, (int(sid_link), parent_id, int(sid_link), sid, tclass))
            else:
                cur.execute("""
                    INSERT INTO school_student_parents (student_id, parent_id)
                    VALUES (%s,%s) ON CONFLICT DO NOTHING
                """, (int(sid_link), parent_id))
        except Exception:
            pass
    conn.commit()
    cur.close(); conn.close()
    flash(f"{full_name} added.", "success")
    return redirect(url_for("school.parents"))


@school_bp.route("/parents/import", methods=["POST"])
@require_login
def parents_import():
    """CSV columns: full_name, whatsapp_number, relationship, student_number"""
    sid    = _school_id()
    tclass = _teacher_class()
    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.parents"))
    file = request.files.get("csv_file")
    if not file or not file.filename.endswith(".csv"):
        flash("Please upload a CSV file.", "danger")
        return redirect(url_for("school.parents"))
    text   = file.stream.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    added  = 0
    errors = []
    conn   = get_db_connection()
    cur    = conn.cursor()
    for i, row in enumerate(reader, start=2):
        name   = (row.get("full_name") or "").strip()
        number = (row.get("whatsapp_number") or "").strip()
        if not name or not number:
            errors.append(f"Row {i}: missing name or number")
            continue
        try:
            cur.execute("""
                INSERT INTO school_parents (school_id, full_name, whatsapp_number, relationship)
                VALUES (%s,%s,%s,%s) RETURNING id
            """, (sid, name, number, (row.get("relationship") or "Parent").strip()))
            parent_id = cur.fetchone()[0]
            student_num = (row.get("student_number") or "").strip()
            if student_num:
                if tclass:
                    cur.execute(
                        "SELECT id FROM school_students WHERE school_id=%s AND student_number=%s AND class_name=%s",
                        (sid, student_num, tclass)
                    )
                else:
                    cur.execute(
                        "SELECT id FROM school_students WHERE school_id=%s AND student_number=%s",
                        (sid, student_num)
                    )
                sr = cur.fetchone()
                if sr:
                    cur.execute("""
                        INSERT INTO school_student_parents (student_id, parent_id)
                        VALUES (%s,%s) ON CONFLICT DO NOTHING
                    """, (sr[0], parent_id))
                elif tclass:
                    errors.append(f"Row {i}: student {student_num} not found in your class")
            added += 1
        except Exception as e:
            errors.append(f"Row {i}: {e}")
    conn.commit()
    cur.close(); conn.close()
    flash(f"{added} parents imported." + (f" {len(errors)} errors." if errors else ""), "success" if not errors else "warning")
    return redirect(url_for("school.parents"))


@school_bp.route("/parents/<int:parent_id>/delete", methods=["POST"])
@require_admin
def parents_delete(parent_id):
    sid = _school_id()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM school_parents WHERE id=%s AND school_id=%s", (parent_id, sid))
    conn.commit()
    cur.close(); conn.close()
    flash("Parent removed.", "success")
    return redirect(url_for("school.parents"))


@school_bp.route("/parents/<int:parent_id>/link", methods=["POST"])
@require_login
def parents_link(parent_id):
    """Link one or more students to a parent."""
    sid        = _school_id()
    tclass      = _teacher_class()
    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.parents"))
    student_ids = request.form.getlist("student_ids")
    conn = get_db_connection()
    cur  = conn.cursor()
    linked = 0
    for s_id in student_ids:
        try:
            if tclass:
                cur.execute(
                    "INSERT INTO school_student_parents (student_id, parent_id) "
                    "SELECT %s, %s WHERE EXISTS ("
                    "  SELECT 1 FROM school_students WHERE id=%s AND school_id=%s AND class_name=%s"
                    ") AND EXISTS ("
                    "  SELECT 1 FROM school_parents WHERE id=%s AND school_id=%s"
                    ") ON CONFLICT DO NOTHING",
                    (int(s_id), parent_id, int(s_id), sid, tclass, parent_id, sid)
                )
            else:
                cur.execute("""
                    INSERT INTO school_student_parents (student_id, parent_id)
                    SELECT %s, %s WHERE EXISTS (
                        SELECT 1 FROM school_students WHERE id=%s AND school_id=%s
                    ) AND EXISTS (
                        SELECT 1 FROM school_parents WHERE id=%s AND school_id=%s
                    )
                    ON CONFLICT DO NOTHING
                """, (int(s_id), parent_id, int(s_id), sid, parent_id, sid))
            linked += cur.rowcount
        except Exception:
            pass
    conn.commit()
    cur.close(); conn.close()
    if linked:
        flash(f"{linked} student(s) linked.", "success")
    elif tclass and student_ids:
        flash("No students linked — you can only link students in your own class.", "warning")
    else:
        flash("No students linked.", "warning")
    return redirect(url_for("school.parents"))


@school_bp.route("/parents/<int:parent_id>/unlink/<int:student_id>", methods=["POST"])
@require_login
def parents_unlink(parent_id, student_id):
    """Remove a student-parent link."""
    sid    = _school_id()
    tclass = _teacher_class()
    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.parents"))
    conn = get_db_connection()
    cur  = conn.cursor()
    if tclass:
        cur.execute(
            "DELETE FROM school_student_parents WHERE parent_id=%s AND student_id=%s "
            "AND EXISTS (SELECT 1 FROM school_students WHERE id=%s AND school_id=%s AND class_name=%s)",
            (parent_id, student_id, student_id, sid, tclass)
        )
    else:
        cur.execute("DELETE FROM school_student_parents WHERE parent_id=%s AND student_id=%s",
                    (parent_id, student_id))
    unlinked = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    if unlinked:
        flash("Child unlinked.", "success")
    elif tclass:
        flash("You can only unlink students in your own class.", "warning")
    else:
        flash("Nothing to unlink.", "warning")
    return redirect(url_for("school.parents"))


@school_bp.route("/parents/<int:parent_id>/summary")
@require_login
def parents_summary(parent_id):
    sid = _school_id()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT p.id, p.full_name, p.whatsapp_number, p.relationship, p.is_opted_in,
               COUNT(ssp.student_id) AS child_count
        FROM school_parents p
        LEFT JOIN school_student_parents ssp ON ssp.parent_id = p.id
        WHERE p.id = %s AND p.school_id = %s
        GROUP BY p.id
    """, (parent_id, sid))
    parent = cur.fetchone()
    if not parent:
        cur.close(); conn.close()
        return jsonify({"error": "Not found"}), 404
    tclass = _teacher_class()
    if _school_role() == "teacher":
        cur.execute("""
            SELECT 1 FROM school_student_parents ssp
            JOIN school_students s ON s.id = ssp.student_id
            WHERE ssp.parent_id=%s AND s.class_name=%s
        """, (parent_id, tclass))
        if not cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"error": "You can only view parents linked to your own class"}), 403
    cur.execute("""
        SELECT s.id, s.full_name, s.class_name, s.arm, s.student_number, s.gender
        FROM school_students s
        JOIN school_student_parents ssp ON ssp.student_id = s.id
        WHERE ssp.parent_id = %s AND s.is_active = TRUE
        ORDER BY s.class_name, s.full_name
    """, (parent_id,))
    children = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({
        "id": parent["id"],
        "full_name": parent["full_name"],
        "whatsapp_number": parent["whatsapp_number"],
        "relationship": parent["relationship"] or "Parent",
        "is_opted_in": parent["is_opted_in"],
        "child_count": int(parent["child_count"]),
        "children": [dict(c) for c in children],
    })


@school_bp.route("/parents/<int:parent_id>/edit", methods=["POST"])
@require_admin
def parents_edit(parent_id):
    sid          = _school_id()
    full_name    = request.form.get("full_name", "").strip()
    wa_number    = request.form.get("whatsapp_number", "").strip()
    relationship = request.form.get("relationship", "Parent")
    if not full_name or not wa_number:
        flash("Name and WhatsApp number are required.", "danger")
        return redirect(url_for("school.parents"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE school_parents SET full_name=%s, whatsapp_number=%s, relationship=%s
        WHERE id=%s AND school_id=%s
    """, (full_name, wa_number, relationship, parent_id, sid))
    conn.commit()
    cur.close(); conn.close()
    flash(f"{full_name} updated.", "success")
    return redirect(url_for("school.parents"))


@school_bp.route("/parents/csv-template")
@require_login
def parents_csv_template():
    from flask import Response
    rows = [
        "full_name,whatsapp_number,relationship,student_number",
        "Mrs Adeyemi,2348012345678,Mother,SCH2025001",
        "Mr Okafor,2348087654321,Father,SCH2025002",
        "Mrs Musa,2348033334444,Parent,SCH2025003",
    ]
    return Response(
        "\n".join(rows),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=parents_template.csv"},
    )


# ── Attendance ─────────────────────────────────────────────────────────────────

@school_bp.route("/attendance")
@require_login
def attendance():
    sid    = _school_id()
    school = _get_school(sid)
    classes = _get_classes(sid)
    staff_class = _teacher_class()

    if _school_role() == "teacher" and not staff_class:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return render_template("school/attendance.html",
            school=school, classes=[], selected_class="", selected_arm="",
            arms=[], date_str=datetime.date.today().isoformat(),
            date_label="", students=[], existing={}, existing_meta={},
            staff_class=None, is_today=True,
        )

    # Teachers are hard-locked to their own class — a manually-crafted
    # ?class=OtherClass is ignored, not just unselected by default.
    requested_class = request.args.get("class", "")
    if staff_class:
        selected_class = staff_class
    else:
        selected_class = requested_class or (classes[0] if classes else "")
    selected_arm   = request.args.get("arm", "")
    date_str       = request.args.get("date", datetime.date.today().isoformat())

    arms = []
    students_in_class = []
    existing = {}
    existing_meta = {}

    if selected_class:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT DISTINCT arm FROM school_students
            WHERE school_id=%s AND class_name=%s AND is_active=TRUE ORDER BY arm
        """, (sid, selected_class))
        arms = [r["arm"] for r in cur.fetchall()]

        if not selected_arm and arms:
            selected_arm = arms[0]

        q = """
            SELECT s.id, s.full_name, s.arm, s.student_number
            FROM school_students s
            WHERE s.school_id=%s AND s.class_name=%s AND s.is_active=TRUE
        """
        params = [sid, selected_class]
        if selected_arm and len(arms) > 1:
            q += " AND s.arm=%s"
            params.append(selected_arm)
        q += " ORDER BY s.full_name"
        cur.execute(q, params)
        students_in_class = cur.fetchall()

        if students_in_class:
            ids = [s["id"] for s in students_in_class]
            cur.execute("""
                SELECT a.student_id, a.status, a.marked_at, st.full_name AS marked_by_name
                FROM school_attendance a
                LEFT JOIN school_staff st ON st.id = a.marked_by
                WHERE a.school_id=%s AND a.attendance_date=%s AND a.student_id = ANY(%s)
            """, (sid, date_str, ids))
            rows = cur.fetchall()
            existing = {r["student_id"]: r["status"] for r in rows}
            existing_meta = {
                r["student_id"]: {
                    "marked_at": r["marked_at"].strftime("%H:%M") if r["marked_at"] else None,
                    "marked_by": r["marked_by_name"],
                } for r in rows
            }

        cur.close(); conn.close()

    try:
        date_label = datetime.date.fromisoformat(date_str).strftime("%A, %B %d, %Y")
    except ValueError:
        date_label = date_str

    return render_template("school/attendance.html",
        school=school, classes=classes,
        selected_class=selected_class,
        selected_arm=selected_arm,
        arms=arms,
        date_str=date_str,
        date_label=date_label,
        students=students_in_class,
        existing=existing,
        existing_meta=existing_meta,
        staff_class=staff_class,
        is_today=(date_str == datetime.date.today().isoformat()),
    )


@school_bp.route("/attendance/save", methods=["POST"])
@require_login
def attendance_save():
    sid          = _school_id()
    school       = _get_school(sid)
    staff_id     = _staff_id()
    tclass       = _teacher_class()
    date_str     = request.form.get("date", datetime.date.today().isoformat())
    class_name   = request.form.get("class_name", "")
    student_ids  = request.form.getlist("student_ids[]")
    absent_ids   = set(request.form.getlist("absent[]"))
    late_ids     = set(request.form.getlist("late[]"))

    if not student_ids:
        flash("No students found.", "warning")
        return redirect(url_for("school.attendance"))
    if _school_role() == "teacher" and class_name != tclass:
        flash("You can only mark attendance for your own class.", "warning")
        return redirect(url_for("school.attendance"))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    notified = 0

    int_ids = [int(x) for x in student_ids]
    if tclass:
        # Defense in depth: even if class_name matched above, only ever write
        # students who are actually in the teacher's class.
        cur.execute(
            "SELECT id FROM school_students WHERE id = ANY(%s) AND school_id=%s AND class_name=%s",
            (int_ids, sid, tclass)
        )
        allowed_ids = {r["id"] for r in cur.fetchall()}
        student_ids = [s for s in student_ids if int(s) in allowed_ids]
        int_ids = [i for i in int_ids if i in allowed_ids]
        if not student_ids:
            cur.close(); conn.close()
            flash("You can only mark attendance for your own class.", "warning")
            return redirect(url_for("school.attendance"))

    cur.execute("""
        SELECT student_id, status, wa_notified FROM school_attendance
        WHERE school_id=%s AND attendance_date=%s AND student_id = ANY(%s)
    """, (sid, date_str, int_ids))
    prior = {r["student_id"]: r for r in cur.fetchall()}

    for sid_str in student_ids:
        s_id   = int(sid_str)
        status = "absent" if sid_str in absent_ids else ("late" if sid_str in late_ids else "present")
        cur.execute("""
            INSERT INTO school_attendance (school_id, student_id, attendance_date, status, marked_at, marked_by)
            VALUES (%s,%s,%s,%s,NOW(),%s)
            ON CONFLICT (student_id, attendance_date)
            DO UPDATE SET status=EXCLUDED.status, marked_at=NOW(), marked_by=EXCLUDED.marked_by
        """, (sid, s_id, date_str, status, staff_id))

        # WhatsApp alert for absences — only on first mark-absent or if a prior attempt
        # didn't actually notify, so re-saving the same day doesn't re-spam parents.
        prior_row = prior.get(s_id)
        already_notified = bool(prior_row and prior_row["status"] == "absent" and prior_row["wa_notified"])
        if status == "absent" and not already_notified and school.get("wa_phone_number_id"):
            cur.execute("""
                SELECT ss.full_name, ss.class_name,
                       sp.whatsapp_number
                FROM school_students ss
                JOIN school_student_parents ssp ON ssp.student_id = ss.id
                JOIN school_parents sp ON sp.id = ssp.parent_id
                WHERE ss.id=%s AND sp.is_opted_in=TRUE
            """, (s_id,))
            parents = cur.fetchall()
            for p in parents:
                ok = send_attendance_alert(
                    school_id=sid,
                    parent_wa=p["whatsapp_number"],
                    student_name=p["full_name"],
                    class_name=p["class_name"],
                    date_str=date_str,
                    school_name=school["school_name"],
                )
                if ok:
                    notified += 1
                    cur.execute("""
                        UPDATE school_attendance SET wa_notified=TRUE
                        WHERE school_id=%s AND student_id=%s AND attendance_date=%s
                    """, (sid, s_id, date_str))

    conn.commit()
    cur.close(); conn.close()

    arm = request.form.get("arm", "")
    msg = f"Attendance saved for {class_name}, {date_str}."
    if notified:
        msg += f" {notified} parent(s) notified via WhatsApp."
    flash(msg, "success")
    redir_args = {"class": class_name, "date": date_str}
    if arm:
        redir_args["arm"] = arm
    return redirect(url_for("school.attendance", **redir_args))


@school_bp.route("/attendance/history")
@require_login
def attendance_history():
    sid     = _school_id()
    school  = _get_school(sid)
    tclass  = _teacher_class()
    # Teachers are hard-locked to their own class — a manually-crafted
    # ?class=OtherClass is ignored.
    cls     = tclass if tclass else request.args.get("class", "")
    status  = request.args.get("status", "")
    from_d  = request.args.get("from", (datetime.date.today() - datetime.timedelta(days=14)).isoformat())
    to_d    = request.args.get("to",   datetime.date.today().isoformat())
    classes = _get_classes(sid)

    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return render_template("school/attendance_history.html",
            school=school, grouped={}, classes=[], selected_class="",
            selected_status=status, from_d=from_d, to_d=to_d,
            stats={}, att_pct=0, absentees=[], total_shown=0,
        )

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    base_where  = ["a.school_id=%s", "a.attendance_date BETWEEN %s AND %s"]
    base_params = [sid, from_d, to_d]
    if cls:
        base_where.append("s.class_name=%s")
        base_params.append(cls)

    # ── Summary stats ──
    cur.execute(f"""
        SELECT
          COUNT(DISTINCT a.attendance_date)                           AS days_tracked,
          COUNT(*)                                                    AS total_records,
          SUM(CASE WHEN a.status='absent'  THEN 1 ELSE 0 END)        AS total_absent,
          SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END)        AS total_present,
          SUM(CASE WHEN a.status='late'    THEN 1 ELSE 0 END)        AS total_late,
          SUM(CASE WHEN a.wa_notified      THEN 1 ELSE 0 END)        AS wa_sent
        FROM school_attendance a
        JOIN school_students s ON s.id = a.student_id
        WHERE {' AND '.join(base_where)}
    """, base_params)
    stats = cur.fetchone() or {}

    # ── Records (with optional status filter) ──
    rec_where  = base_where[:]
    rec_params = base_params[:]
    if status:
        rec_where.append("a.status=%s")
        rec_params.append(status)

    cur.execute(f"""
        SELECT a.attendance_date, a.student_id, a.status, a.wa_notified, a.marked_at,
               s.full_name, s.class_name, s.arm, st.full_name AS marked_by_name
        FROM school_attendance a
        JOIN school_students s ON s.id = a.student_id
        LEFT JOIN school_staff st ON st.id = a.marked_by
        WHERE {' AND '.join(rec_where)}
        ORDER BY a.attendance_date DESC, s.class_name, s.arm, s.full_name
        LIMIT 500
    """, rec_params)
    raw = cur.fetchall()

    # Group by date (preserving DESC order)
    from collections import OrderedDict
    grouped = OrderedDict()
    for r in raw:
        d = r["attendance_date"].isoformat() if hasattr(r["attendance_date"], "isoformat") else str(r["attendance_date"])
        grouped.setdefault(d, []).append(r)

    # ── Frequent absentees ──
    cur.execute(f"""
        SELECT s.id AS student_id, s.full_name, s.class_name, s.arm,
               COUNT(*) AS absent_count
        FROM school_attendance a
        JOIN school_students s ON s.id = a.student_id
        WHERE {' AND '.join(base_where)} AND a.status='absent'
        GROUP BY s.id, s.full_name, s.class_name, s.arm
        HAVING COUNT(*) >= 2
        ORDER BY absent_count DESC
        LIMIT 10
    """, base_params)
    absentees = cur.fetchall()

    cur.close(); conn.close()

    att_pct = 0
    if stats.get("total_records"):
        att_pct = round(100 * (stats["total_present"] or 0) / stats["total_records"])

    return render_template("school/attendance_history.html",
        school=school,
        grouped=grouped,
        classes=[tclass] if tclass else classes, selected_class=cls,
        selected_status=status,
        from_d=from_d, to_d=to_d,
        stats=stats, att_pct=att_pct,
        absentees=absentees,
        total_shown=len(raw),
    )


@school_bp.route("/attendance/notify")
@require_login
def attendance_notify():
    sid    = _school_id()
    school = _get_school(sid)
    classes = _get_classes(sid)

    # Teachers are hard-locked to their own class (same rule as Mark Attendance)
    staff_class = _teacher_class()

    if _school_role() == "teacher" and not staff_class:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return render_template("school/attendance_notify.html",
            school=school, classes=[], selected_class="", staff_class=None,
            date_str=datetime.date.today().isoformat(), date_label="",
            absentees=[], template=None,
        )

    date_str = request.args.get("date", datetime.date.today().isoformat())
    cls      = staff_class if staff_class else request.args.get("class", "")

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    where  = ["a.school_id=%s", "a.attendance_date=%s", "a.status='absent'"]
    params = [sid, date_str]
    if cls:
        where.append("s.class_name=%s")
        params.append(cls)

    cur.execute(f"""
        SELECT a.student_id, a.wa_notified, a.marked_at,
               s.full_name, s.class_name, s.arm,
               COALESCE(
                 json_agg(json_build_object(
                   'id', p.id, 'full_name', p.full_name,
                   'whatsapp_number', p.whatsapp_number,
                   'is_opted_in', p.is_opted_in
                 )) FILTER (WHERE p.id IS NOT NULL), '[]'
               ) AS parents
        FROM school_attendance a
        JOIN school_students s ON s.id = a.student_id
        LEFT JOIN school_student_parents ssp ON ssp.student_id = s.id
        LEFT JOIN school_parents p ON p.id = ssp.parent_id
        WHERE {' AND '.join(where)}
        GROUP BY a.student_id, a.wa_notified, a.marked_at, s.full_name, s.class_name, s.arm
        ORDER BY s.class_name, s.arm, s.full_name
    """, params)
    absentees = cur.fetchall()
    cur.close(); conn.close()

    template = get_school_template(sid, "absence_alert")

    try:
        date_label = datetime.date.fromisoformat(date_str).strftime("%A, %B %d, %Y")
    except ValueError:
        date_label = date_str

    return render_template("school/attendance_notify.html",
        school=school, classes=classes,
        selected_class=cls, staff_class=staff_class,
        date_str=date_str, date_label=date_label,
        absentees=absentees, template=template,
    )


@school_bp.route("/attendance/notify/send", methods=["POST"])
@require_login
def attendance_notify_send():
    sid       = _school_id()
    school    = _get_school(sid)
    date_str  = request.form.get("date", datetime.date.today().isoformat())
    student_id = request.form.get("student_id", type=int)
    redir_args = {"date": date_str}
    if request.form.get("class"):
        redir_args["class"] = request.form.get("class")

    if _school_role() == "teacher" and not _teacher_class():
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.attendance_notify", **redir_args))

    if not get_school_template(sid, "absence_alert"):
        flash("Set up a Meta-approved absence alert template in Settings → WhatsApp Templates before sending.", "warning")
        return redirect(url_for("school.attendance_notify", **redir_args))

    if not student_id:
        flash("No student specified.", "warning")
        return redirect(url_for("school.attendance_notify", **redir_args))

    sent_n = _send_absence_notifications(sid, school, date_str, student_ids=[student_id])
    if sent_n:
        flash(f"Notified {sent_n} parent(s).", "success")
    else:
        flash("No opted-in parent could be notified for this student.", "warning")
    return redirect(url_for("school.attendance_notify", **redir_args))


@school_bp.route("/attendance/notify/send-all", methods=["POST"])
@require_login
def attendance_notify_send_all():
    sid      = _school_id()
    school   = _get_school(sid)
    tclass   = _teacher_class()
    date_str = request.form.get("date", datetime.date.today().isoformat())
    cls      = tclass if tclass else request.form.get("class", "")
    redir_args = {"date": date_str}
    if cls:
        redir_args["class"] = cls

    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.attendance_notify", **redir_args))

    if not get_school_template(sid, "absence_alert"):
        flash("Set up a Meta-approved absence alert template in Settings → WhatsApp Templates before sending.", "warning")
        return redirect(url_for("school.attendance_notify", **redir_args))

    sent_n = _send_absence_notifications(sid, school, date_str, class_name=cls)
    if sent_n:
        flash(f"Notified {sent_n} parent(s).", "success")
    else:
        flash("Nothing to send — all opted-in parents are already notified.", "warning")
    return redirect(url_for("school.attendance_notify", **redir_args))


def _send_absence_notifications(sid, school, date_str, student_ids=None, class_name=""):
    """Send (or re-send) absence alerts for the given date. Scoped to either an
    explicit list of student_ids or all pending (not-yet-notified) absentees in
    class_name (empty = all classes). Derives the caller's class restriction
    itself (rather than trusting an argument) so a future caller can't forget
    to scope it: teachers are always restricted to their own class, and an
    unassigned teacher always gets zero results."""
    is_teacher = _school_role() == "teacher"
    tclass     = _teacher_class()
    if is_teacher and not tclass:
        return 0

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    where  = ["a.school_id=%s", "a.attendance_date=%s", "a.status='absent'"]
    params = [sid, date_str]
    if student_ids:
        where.append("a.student_id = ANY(%s)")
        params.append(student_ids)
    else:
        where.append("a.wa_notified = FALSE")
        if class_name:
            where.append("s.class_name=%s")
            params.append(class_name)
    if tclass:
        where.append("s.class_name=%s")
        params.append(tclass)

    cur.execute(f"""
        SELECT a.student_id, s.full_name, s.class_name, sp.whatsapp_number
        FROM school_attendance a
        JOIN school_students s ON s.id = a.student_id
        JOIN school_student_parents ssp ON ssp.student_id = s.id
        JOIN school_parents sp ON sp.id = ssp.parent_id
        WHERE {' AND '.join(where)} AND sp.is_opted_in=TRUE
    """, params)
    targets = cur.fetchall()

    sent_n = 0
    notified_student_ids = set()
    for t in targets:
        ok = send_attendance_alert(
            school_id=sid,
            parent_wa=t["whatsapp_number"],
            student_name=t["full_name"],
            class_name=t["class_name"],
            date_str=date_str,
            school_name=school["school_name"],
        )
        if ok:
            sent_n += 1
            notified_student_ids.add(t["student_id"])

    if notified_student_ids:
        cur.execute("""
            UPDATE school_attendance SET wa_notified=TRUE
            WHERE school_id=%s AND attendance_date=%s AND student_id = ANY(%s)
        """, (sid, date_str, list(notified_student_ids)))
        conn.commit()

    cur.close(); conn.close()
    return sent_n

# ── Fees ───────────────────────────────────────────────────────────────────────

@school_bp.route("/fees")
@require_login
def fees():
    sid    = _school_id()
    school = _get_school(sid)
    tclass = _teacher_class()

    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return render_template("school/fees.html",
            school=school, schedules=[], classes=[], summary={},
            today=datetime.date.today(), staff_class=None,
            has_payment_gateway=school_payments.get_active_gateway(sid) is not None,
        )

    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    fee_class_where = " AND (fs.class_name=%s OR fs.class_name IS NULL)" if tclass else ""
    class_param = [tclass] if tclass else []
    # On a school-wide schedule (fs.class_name IS NULL), payment rows span every
    # class — a teacher must still only see/count their own class's students.
    # Filtered as a subquery (not a WHERE clause on the outer LEFT JOIN) so a
    # schedule with zero matching payment rows still appears with 0/0 stats
    # instead of vanishing from the list entirely.
    cur.execute(f"""
        SELECT fs.*,
               COUNT(fp.id)                                              AS total_students,
               SUM(CASE WHEN fp.status='paid'    THEN 1 ELSE 0 END)     AS paid_count,
               SUM(CASE WHEN fp.status='partial' THEN 1 ELSE 0 END)     AS partial_count,
               SUM(CASE WHEN fp.status='unpaid'  THEN 1 ELSE 0 END)     AS unpaid_count,
               COALESCE(SUM(fp.amount_paid), 0)                         AS total_collected,
               COALESCE(SUM(fs.amount), 0)                              AS total_expected
        FROM school_fee_schedules fs
        LEFT JOIN (
          SELECT fp.* FROM school_fee_payments fp
          JOIN school_students ps ON ps.id = fp.student_id
          WHERE %s::text IS NULL OR ps.class_name = %s
        ) fp ON fp.schedule_id = fs.id
        WHERE fs.school_id=%s AND fs.is_active=TRUE{fee_class_where}
        GROUP BY fs.id
        ORDER BY fs.due_date DESC NULLS LAST, fs.created_at DESC
    """, [tclass, tclass, sid, *class_param])
    schedules = cur.fetchall()

    # Overall summary
    cur.execute(f"""
        SELECT
          COUNT(DISTINCT fs.id)                                          AS schedule_count,
          COALESCE(SUM(fs.amount * COALESCE(fp_cnt.n, 0)), 0)           AS grand_expected,
          COALESCE(SUM(fp_sum.collected), 0)                            AS grand_collected
        FROM school_fee_schedules fs
        LEFT JOIN (
          SELECT fp.schedule_id, COUNT(*) AS n
          FROM school_fee_payments fp
          JOIN school_students ps ON ps.id = fp.student_id
          WHERE %s::text IS NULL OR ps.class_name = %s
          GROUP BY fp.schedule_id
        ) fp_cnt ON fp_cnt.schedule_id = fs.id
        LEFT JOIN (
          SELECT fp.schedule_id, SUM(fp.amount_paid) AS collected
          FROM school_fee_payments fp
          JOIN school_students ps ON ps.id = fp.student_id
          WHERE %s::text IS NULL OR ps.class_name = %s
          GROUP BY fp.schedule_id
        ) fp_sum ON fp_sum.schedule_id = fs.id
        WHERE fs.school_id=%s AND fs.is_active=TRUE{fee_class_where}
    """, [tclass, tclass, tclass, tclass, sid, *class_param])
    summary = cur.fetchone() or {}

    classes = _get_classes(sid)
    today   = datetime.date.today()
    cur.close(); conn.close()
    return render_template("school/fees.html",
        school=school, schedules=schedules, classes=classes,
        summary=summary, today=today, staff_class=tclass,
        has_payment_gateway=school_payments.get_active_gateway(sid) is not None,
    )


@school_bp.route("/fees/add", methods=["POST"])
@require_admin
def fees_add():
    sid        = _school_id()
    name       = request.form.get("name", "").strip()
    class_name = request.form.get("class_name", "").strip() or None
    amount     = request.form.get("amount", "0").replace(",", "")
    due_date   = request.form.get("due_date", "") or None
    term       = request.form.get("term", "").strip() or None
    session_yr = request.form.get("session", "").strip() or None
    if not name or not amount:
        flash("Name and amount are required.", "danger")
        return redirect(url_for("school.fees"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO school_fee_schedules
          (school_id, name, class_name, amount, due_date, term, session)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (sid, name, class_name, float(amount), due_date, term, session_yr))
    schedule_id = cur.fetchone()[0]

    # Auto-create unpaid records for matching students
    where_cls = "AND class_name=%s" if class_name else ""
    params    = [sid] + ([class_name] if class_name else [])
    cur.execute(f"""
        SELECT id FROM school_students
        WHERE school_id=%s AND is_active=TRUE {where_cls}
    """, params)
    student_ids = [r[0] for r in cur.fetchall()]
    for s_id in student_ids:
        cur.execute("""
            INSERT INTO school_fee_payments (schedule_id, student_id, amount_paid, status)
            VALUES (%s,%s,0,'unpaid') ON CONFLICT DO NOTHING
        """, (schedule_id, s_id))

    conn.commit()
    cur.close(); conn.close()
    flash(f"Fee schedule '{name}' created for {len(student_ids)} students.", "success")
    return redirect(url_for("school.fees"))


@school_bp.route("/fees/<int:schedule_id>")
@require_login
def fee_payments(schedule_id):
    sid    = _school_id()
    school = _get_school(sid)
    tclass = _teacher_class()
    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.fees"))
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM school_fee_schedules WHERE id=%s AND school_id=%s",
        (schedule_id, sid)
    )
    schedule = cur.fetchone()
    if not schedule:
        flash("Fee schedule not found.", "danger")
        cur.close(); conn.close()
        return redirect(url_for("school.fees"))
    if tclass and schedule["class_name"] and schedule["class_name"] != tclass:
        flash("This fee schedule is not for your class.", "warning")
        cur.close(); conn.close()
        return redirect(url_for("school.fees"))

    status_filter = request.args.get("status", "")
    where = ["fp.schedule_id=%s"]
    params = [schedule_id]
    if status_filter:
        where.append("fp.status=%s")
        params.append(status_filter)
    # A school-wide schedule (class_name IS NULL) still shows every student to
    # admin/bursar; teachers only ever see their own class's slice of it.
    if tclass:
        where.append("s.class_name=%s")
        params.append(tclass)

    # Payment summary stats (matches the same scope as the row list)
    cur.execute(f"""
        SELECT
          COUNT(*)                                                    AS total,
          SUM(CASE WHEN fp.status='paid'    THEN 1 ELSE 0 END)        AS paid_count,
          SUM(CASE WHEN fp.status='partial' THEN 1 ELSE 0 END)        AS partial_count,
          SUM(CASE WHEN fp.status='unpaid'  THEN 1 ELSE 0 END)        AS unpaid_count,
          COALESCE(SUM(fp.amount_paid), 0)                            AS total_collected
        FROM school_fee_payments fp
        JOIN school_students s ON s.id = fp.student_id
        WHERE {' AND '.join(["fp.schedule_id=%s"] + (["s.class_name=%s"] if tclass else []))}
    """, [schedule_id, *([tclass] if tclass else [])])
    pay_stats = cur.fetchone() or {}

    cur.execute(f"""
        SELECT fp.*, s.full_name, s.class_name, s.arm, s.student_number,
               st.full_name AS reminded_by_name, txn.gateway AS paid_via_gateway
        FROM school_fee_payments fp
        JOIN school_students s ON s.id = fp.student_id
        LEFT JOIN school_staff st ON st.id = fp.last_reminded_by
        LEFT JOIN LATERAL (
            SELECT gateway FROM school_fee_gateway_txns
            WHERE payment_id = fp.id ORDER BY created_at DESC LIMIT 1
        ) txn ON TRUE
        WHERE {' AND '.join(where)}
        ORDER BY s.class_name, s.arm, s.full_name
    """, params)
    payments = cur.fetchall()
    cur.close(); conn.close()

    total_expected = float(schedule["amount"]) * int(pay_stats.get("total") or 0)
    collect_pct = round(100 * float(pay_stats.get("total_collected") or 0) / total_expected) \
                  if total_expected else 0

    has_payment_gateway = school_payments.get_active_gateway(sid) is not None
    if has_payment_gateway:
        for p in payments:
            if p["status"] in ("unpaid", "partial") and not p["payment_token"]:
                p["payment_token"] = school_payments.get_or_create_payment_token(
                    schedule_id, p["student_id"])

    return render_template("school/fee_payments.html",
        school=school, schedule=schedule,
        payments=payments, status_filter=status_filter,
        pay_stats=pay_stats, collect_pct=collect_pct,
        total_expected=total_expected,
        today=datetime.date.today().isoformat(),
        fee_template=get_school_template(sid, "fee_reminder"),
        staff_class=tclass,
        is_teacher=(_school_role() == "teacher"),
        has_payment_gateway=has_payment_gateway,
        school_pay_base_url=school_payments.SCHOOL_PAY_BASE_URL,
    )


@school_bp.route("/fees/<int:schedule_id>/payment", methods=["POST"])
@require_admin
def fee_record_payment(schedule_id):
    sid        = _school_id()
    student_id = int(request.form.get("student_id", 0))
    amount     = float(request.form.get("amount_paid", "0").replace(",", ""))
    ref        = request.form.get("payment_ref", "").strip()
    notes      = request.form.get("notes", "").strip()
    pay_date   = request.form.get("payment_date") or datetime.date.today().isoformat()

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "SELECT amount FROM school_fee_schedules WHERE id=%s AND school_id=%s",
        (schedule_id, sid)
    )
    row = cur.fetchone()
    if not row:
        flash("Not found.", "danger")
        cur.close(); conn.close()
        return redirect(url_for("school.fees"))

    total   = float(row[0])
    status  = "paid" if amount >= total else ("partial" if amount > 0 else "unpaid")
    cur.execute("""
        INSERT INTO school_fee_payments
          (schedule_id, student_id, amount_paid, payment_date, payment_ref, status, notes, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (schedule_id, student_id) DO UPDATE SET
          amount_paid=EXCLUDED.amount_paid,
          payment_date=EXCLUDED.payment_date,
          payment_ref=EXCLUDED.payment_ref,
          status=EXCLUDED.status,
          notes=EXCLUDED.notes,
          updated_at=NOW()
    """, (schedule_id, student_id, amount, pay_date, ref or None, status, notes or None))
    conn.commit()
    cur.close(); conn.close()
    flash("Payment recorded.", "success")
    return redirect(url_for("school.fee_payments", schedule_id=schedule_id))


@school_bp.route("/fees/<int:schedule_id>/import", methods=["POST"])
@require_admin
def fee_import_payments(schedule_id):
    """CSV: student_number, amount_paid, payment_date, payment_ref"""
    sid  = _school_id()
    file = request.files.get("csv_file")
    if not file or not file.filename.endswith(".csv"):
        flash("Please upload a CSV file.", "danger")
        return redirect(url_for("school.fee_payments", schedule_id=schedule_id))

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "SELECT amount FROM school_fee_schedules WHERE id=%s AND school_id=%s",
        (schedule_id, sid)
    )
    row = cur.fetchone()
    if not row:
        flash("Schedule not found.", "danger")
        cur.close(); conn.close()
        return redirect(url_for("school.fees"))
    total  = float(row[0])

    text   = file.stream.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    done   = 0
    errors = []
    for i, row in enumerate(reader, start=2):
        snum   = (row.get("student_number") or "").strip()
        amount = float((row.get("amount_paid") or "0").replace(",", ""))
        if not snum:
            errors.append(f"Row {i}: missing student_number")
            continue
        cur.execute(
            "SELECT id FROM school_students WHERE school_id=%s AND student_number=%s",
            (sid, snum)
        )
        sr = cur.fetchone()
        if not sr:
            errors.append(f"Row {i}: student '{snum}' not found")
            continue
        status = "paid" if amount >= total else ("partial" if amount > 0 else "unpaid")
        cur.execute("""
            INSERT INTO school_fee_payments
              (schedule_id, student_id, amount_paid, payment_date, payment_ref, status, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (schedule_id, student_id) DO UPDATE SET
              amount_paid=EXCLUDED.amount_paid,
              payment_date=EXCLUDED.payment_date,
              payment_ref=EXCLUDED.payment_ref,
              status=EXCLUDED.status,
              updated_at=NOW()
        """, (
            schedule_id, sr[0], amount,
            (row.get("payment_date") or datetime.date.today().isoformat()),
            (row.get("payment_ref") or "").strip() or None,
            status,
        ))
        done += 1

    conn.commit()
    cur.close(); conn.close()
    flash(f"{done} payments imported." + (f" {len(errors)} errors." if errors else ""), "success" if not errors else "warning")
    return redirect(url_for("school.fee_payments", schedule_id=schedule_id))


@school_bp.route("/fees/<int:schedule_id>/remind", methods=["POST"])
@require_login
def fee_remind(schedule_id):
    sid    = _school_id()
    school = _get_school(sid)
    if _school_role() == "teacher" and not _teacher_class():
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.fee_payments", schedule_id=schedule_id))
    if not school.get("wa_phone_number_id"):
        flash("WhatsApp is not connected. Go to Settings → WhatsApp Setup.", "warning")
        return redirect(url_for("school.fee_payments", schedule_id=schedule_id))
    if not get_school_template(sid, "fee_reminder"):
        flash("Set up a Meta-approved fee reminder template in Settings → WhatsApp Templates before sending.", "warning")
        return redirect(url_for("school.fee_payments", schedule_id=schedule_id))

    sent = _send_fee_reminders(sid, school, schedule_id)
    if sent:
        flash(f"Reminder sent to {sent} parent(s).", "success")
    else:
        flash("No opted-in parent could be notified.", "warning")
    return redirect(url_for("school.fee_payments", schedule_id=schedule_id))


@school_bp.route("/fees/<int:schedule_id>/remind/<int:student_id>", methods=["POST"])
@require_login
def fee_remind_one(schedule_id, student_id):
    sid    = _school_id()
    school = _get_school(sid)
    if _school_role() == "teacher" and not _teacher_class():
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.fee_payments", schedule_id=schedule_id))
    if not get_school_template(sid, "fee_reminder"):
        flash("Set up a Meta-approved fee reminder template in Settings → WhatsApp Templates before sending.", "warning")
        return redirect(url_for("school.fee_payments", schedule_id=schedule_id))

    sent = _send_fee_reminders(sid, school, schedule_id, student_ids=[student_id])
    if sent:
        flash(f"Notified {sent} parent(s).", "success")
    else:
        flash("No opted-in parent could be notified for this student.", "warning")
    return redirect(url_for("school.fee_payments", schedule_id=schedule_id))


def _send_fee_reminders(sid, school, schedule_id, student_ids=None):
    """Send (or re-send) fee reminders for a schedule. Scoped to either an
    explicit list of student_ids or all unpaid/partial students on the
    schedule. Derives the caller's class restriction itself (rather than
    trusting an argument) so a future caller can't forget to scope it:
    teachers are always restricted to their own class, and an unassigned
    teacher always gets zero results. Returns the number of parents notified."""
    staff_id   = _staff_id()
    is_teacher = _school_role() == "teacher"
    tclass     = _teacher_class()
    if is_teacher and not tclass:
        return 0

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM school_fee_schedules WHERE id=%s AND school_id=%s",
        (schedule_id, sid)
    )
    schedule = cur.fetchone()
    if not schedule:
        cur.close(); conn.close()
        return 0
    if tclass and schedule["class_name"] and schedule["class_name"] != tclass:
        cur.close(); conn.close()
        return 0

    # Always restrict to unpaid/partial, even for an explicit student_ids
    # list — a stale page or direct POST shouldn't be able to re-notify an
    # already-fully-paid parent.
    where  = ["fp.schedule_id=%s", "fp.status IN ('unpaid','partial')"]
    params = [schedule_id]
    if student_ids:
        where.append("fp.student_id = ANY(%s)")
        params.append(student_ids)
    if tclass:
        where.append("s.class_name=%s")
        params.append(tclass)

    cur.execute(f"""
        SELECT fp.student_id, fp.amount_paid, s.full_name, p.whatsapp_number
        FROM school_fee_payments fp
        JOIN school_students s ON s.id = fp.student_id
        JOIN school_student_parents ssp ON ssp.student_id = s.id
        JOIN school_parents p ON p.id = ssp.parent_id
        WHERE {' AND '.join(where)} AND p.is_opted_in=TRUE
    """, params)
    rows = cur.fetchall()

    sent = 0
    notified_student_ids = set()
    total = float(schedule["amount"])
    due   = schedule["due_date"].strftime("%d %b %Y") if schedule.get("due_date") else "N/A"
    has_gateway = school_payments.get_active_gateway(sid) is not None
    for r in rows:
        balance = total - float(r["amount_paid"])
        payment_token = (
            school_payments.get_or_create_payment_token(schedule_id, r["student_id"])
            if has_gateway else None
        )
        ok = send_fee_reminder(
            school_id=sid,
            parent_wa=r["whatsapp_number"],
            student_name=r["full_name"],
            fee_name=schedule["name"],
            amount=total,
            balance=balance,
            due_date=due,
            school_name=school["school_name"],
            payment_token=payment_token,
        )
        if ok:
            sent += 1
            notified_student_ids.add(r["student_id"])

    if notified_student_ids:
        cur.execute("""
            UPDATE school_fee_payments
            SET last_reminded_at=NOW(), last_reminded_by=%s, reminder_count=reminder_count+1
            WHERE schedule_id=%s AND student_id = ANY(%s)
        """, (staff_id, schedule_id, list(notified_student_ids)))
        conn.commit()

    cur.close(); conn.close()
    return sent

# ── Broadcast ──────────────────────────────────────────────────────────────────

@school_bp.route("/broadcast")
@require_login
def broadcast():
    sid = _school_id()
    gate = _require_school_plan_feature(sid, "feat_broadcasts", "Starter")
    if gate: return gate
    school  = _get_school(sid)
    tclass  = _teacher_class()
    classes = _get_classes(sid)

    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return render_template("school/broadcast.html",
            school=school, classes=[], history=[],
            class_counts={}, total_parents=0, staff_class=None,
        )

    conn    = get_db_connection()
    cur     = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    hist_class_where = " AND (target_class=%s OR target_class IS NULL)" if tclass else ""
    class_param = [tclass] if tclass else []
    cur.execute(f"""
        SELECT * FROM school_broadcasts WHERE school_id=%s{hist_class_where}
        ORDER BY created_at DESC LIMIT 50
    """, [sid, *class_param])
    history = cur.fetchall()

    # Parent reach counts per class (for live "X parents" preview in compose)
    cc_class_where = " AND s.class_name=%s" if tclass else ""
    cur.execute(f"""
        SELECT s.class_name, COUNT(DISTINCT p.id) AS cnt
        FROM school_parents p
        JOIN school_student_parents ssp ON ssp.parent_id = p.id
        JOIN school_students s ON s.id = ssp.student_id
        WHERE s.school_id=%s AND p.is_opted_in=TRUE AND s.is_active=TRUE{cc_class_where}
        GROUP BY s.class_name
    """, [sid, *class_param])
    class_counts = {r["class_name"]: int(r["cnt"]) for r in cur.fetchall()}

    if tclass:
        total_parents = class_counts.get(tclass, 0)
    else:
        cur.execute("""
            SELECT COUNT(DISTINCT id) AS cnt FROM school_parents
            WHERE school_id=%s AND is_opted_in=TRUE
        """, (sid,))
        total_parents = int((cur.fetchone() or {}).get("cnt") or 0)

    cur.close(); conn.close()
    return render_template("school/broadcast.html",
        school=school, classes=[tclass] if tclass else classes, history=history,
        class_counts=class_counts, total_parents=total_parents, staff_class=tclass,
    )


@school_bp.route("/broadcast/send", methods=["POST"])
@require_login
def broadcast_send():
    sid  = _school_id()
    gate = _require_school_plan_feature(sid, "feat_broadcasts", "Starter")
    if gate: return gate
    school       = _get_school(sid)
    tclass       = _teacher_class()
    title        = request.form.get("title", "").strip()
    message      = request.form.get("message", "").strip()
    target_class = request.form.get("target_class", "").strip() or None

    if not title or not message:
        flash("Title and message are required.", "danger")
        return redirect(url_for("school.broadcast"))
    if not school.get("wa_phone_number_id"):
        flash("WhatsApp is not connected. Go to Settings → WhatsApp Setup.", "warning")
        return redirect(url_for("school.broadcast"))
    if _school_role() == "teacher" and not tclass:
        flash("No class assigned to your account yet — contact your school admin.", "warning")
        return redirect(url_for("school.broadcast"))
    if tclass:
        # Teachers can only broadcast to their own class — no "all classes" option.
        target_class = tclass

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Insert broadcast record first
    cur.execute("""
        INSERT INTO school_broadcasts (school_id, title, message, target_class, status, created_by)
        VALUES (%s,%s,%s,%s,'sending',%s) RETURNING id
    """, (sid, title, message, target_class, _staff_id()))
    broadcast_id = cur.fetchone()["id"]
    conn.commit()

    # Fetch target parents
    if target_class:
        cur.execute("""
            SELECT DISTINCT p.whatsapp_number FROM school_parents p
            JOIN school_student_parents ssp ON ssp.parent_id = p.id
            JOIN school_students s ON s.id = ssp.student_id
            WHERE s.school_id=%s AND s.class_name=%s AND p.is_opted_in=TRUE AND s.is_active=TRUE
        """, (sid, target_class))
    else:
        cur.execute("""
            SELECT DISTINCT p.whatsapp_number FROM school_parents p
            WHERE p.school_id=%s AND p.is_opted_in=TRUE
        """, (sid,))
    numbers = [r["whatsapp_number"] for r in cur.fetchall()]

    sent = 0
    for number in numbers:
        ok = send_broadcast(
            school_id=sid,
            parent_wa=number,
            message=message,
            school_name=school["school_name"],
        )
        if ok:
            sent += 1

    cur.execute("""
        UPDATE school_broadcasts
        SET status='sent', sent_count=%s, delivered_count=%s, sent_at=NOW()
        WHERE id=%s
    """, (len(numbers), sent, broadcast_id))
    conn.commit()
    cur.close(); conn.close()

    flash(f"Broadcast sent to {sent} of {len(numbers)} parents.", "success")
    return redirect(url_for("school.broadcast"))

# ── School Knowledge Base ──────────────────────────────────────────────────────

@school_bp.route("/knowledge")
@require_admin
def knowledge():
    sid    = _school_id()
    school = _get_school(sid)
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM school_knowledge WHERE school_id=%s ORDER BY category, id
    """, (sid,))
    entries = cur.fetchall()
    cur.close(); conn.close()
    return render_template("school/knowledge.html",
        school=school, entries=entries,
    )


@school_bp.route("/knowledge/add", methods=["POST"])
@require_admin
def knowledge_add():
    sid      = _school_id()
    category = request.form.get("category", "general").strip()
    question = request.form.get("question", "").strip()
    answer   = request.form.get("answer", "").strip()
    if not question or not answer:
        flash("Question and answer are required.", "danger")
        return redirect(url_for("school.knowledge"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO school_knowledge (school_id, category, question, answer)
        VALUES (%s,%s,%s,%s) RETURNING id
    """, (sid, category, question, answer))
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close(); conn.close()
    sync_qa_chunk(new_id)
    flash("Knowledge entry added.", "success")
    return redirect(url_for("school.knowledge"))


@school_bp.route("/knowledge/<int:entry_id>/edit", methods=["POST"])
@require_admin
def knowledge_edit(entry_id):
    sid      = _school_id()
    category = request.form.get("category", "general").strip()
    question = request.form.get("question", "").strip()
    answer   = request.form.get("answer", "").strip()
    if not question or not answer:
        flash("Question and answer are required.", "danger")
        return redirect(url_for("school.knowledge"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE school_knowledge SET category=%s, question=%s, answer=%s
        WHERE id=%s AND school_id=%s
    """, (category, question, answer, entry_id, sid))
    conn.commit()
    cur.close(); conn.close()
    sync_qa_chunk(entry_id)
    flash("Entry updated.", "success")
    return redirect(url_for("school.knowledge"))


@school_bp.route("/knowledge/<int:entry_id>/delete", methods=["POST"])
@require_admin
def knowledge_delete(entry_id):
    sid = _school_id()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "DELETE FROM school_knowledge WHERE id=%s AND school_id=%s",
        (entry_id, sid)
    )
    cur.execute(
        "DELETE FROM school_kb_chunks WHERE source_type='qa' AND source_id=%s",
        (entry_id,)
    )
    conn.commit()
    cur.close(); conn.close()
    flash("Entry deleted.", "success")
    return redirect(url_for("school.knowledge"))


@school_bp.route("/knowledge/documents")
@require_admin
def knowledge_documents():
    sid    = _school_id()
    school = _get_school(sid)
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM school_kb_documents WHERE school_id=%s ORDER BY created_at DESC
    """, (sid,))
    documents = cur.fetchall()
    cur.close(); conn.close()
    return render_template("school/knowledge_documents.html",
        school=school, documents=documents,
    )


@school_bp.route("/knowledge/documents/upload", methods=["POST"])
@require_admin
def knowledge_documents_upload():
    sid  = _school_id()
    gate = _require_school_plan_feature(sid, "feat_document_rag", "Growing")
    if gate:
        return gate
    file = request.files.get("doc_file")
    if not file or not file.filename:
        flash("Please choose a file to upload.", "danger")
        return redirect(url_for("school.knowledge_documents"))

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in _KB_ALLOWED_EXT:
        flash("Only PDF, DOCX, TXT, or MD files are supported.", "danger")
        return redirect(url_for("school.knowledge_documents"))

    school_folder = os.path.join(_KB_UPLOAD_FOLDER, str(sid))
    os.makedirs(school_folder, exist_ok=True)
    filename = secure_filename(file.filename)
    file_path = os.path.join(school_folder, filename)
    # Avoid clobbering an existing file with the same name
    base, suffix = os.path.splitext(file_path)
    n = 1
    while os.path.exists(file_path):
        file_path = f"{base}_{n}{suffix}"
        n += 1
    file.save(file_path)

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO school_kb_documents (school_id, filename, file_path, uploaded_by)
        VALUES (%s,%s,%s,%s) RETURNING id
    """, (sid, filename, file_path, _staff_id()))
    document_id = cur.fetchone()[0]
    conn.commit()
    cur.close(); conn.close()

    threading.Thread(target=process_document, args=(document_id,), daemon=True).start()

    flash(f"{filename} uploaded — processing in the background.", "success")
    return redirect(url_for("school.knowledge_documents"))


@school_bp.route("/knowledge/documents/<int:document_id>/delete", methods=["POST"])
@require_admin
def knowledge_documents_delete(document_id):
    sid = _school_id()
    if delete_document(document_id, sid):
        flash("Document deleted.", "success")
    else:
        flash("Document not found.", "danger")
    return redirect(url_for("school.knowledge_documents"))

# ── Integrations ───────────────────────────────────────────────────────────────

@school_bp.route("/integrations")
@require_admin
def integrations():
    sid    = _school_id()
    school = _get_school(sid)

    paystack    = school_payments.mask_secret(school_payments.get_gateway(sid, "paystack"))
    flutterwave = school_payments.mask_secret(school_payments.get_gateway(sid, "flutterwave"))
    paystack["health"]    = school_payments.webhook_health(paystack.get("last_webhook_at")) if paystack else "red"
    flutterwave["health"] = school_payments.webhook_health(flutterwave.get("last_webhook_at")) if flutterwave else "red"

    return render_template("school/integrations.html",
        school=school, paystack=paystack, flutterwave=flutterwave,
        default_gateway=school.get("default_payment_gateway"),
        both_connected=bool(paystack) and bool(flutterwave),
        school_base_url=school_payments.SCHOOL_BASE_URL,
    )


# ── Settings ───────────────────────────────────────────────────────────────────

@school_bp.route("/admin/wa-connect")
@require_login
def wa_connect():
    sid    = _school_id()
    school = _get_school(sid)
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM school_staff WHERE id=%s", (_staff_id(),))
    me = cur.fetchone()
    cur.close(); conn.close()
    is_admin       = (_school_role() == "admin")
    meta_app_id    = os.getenv("META_APP_ID", "")
    meta_config_id = os.getenv("META_CONFIG_ID", "")
    embedded_enabled = bool(meta_app_id and meta_config_id)
    return render_template("school/wa_connect.html",
        school=school, me=me, is_admin=is_admin,
        embedded_enabled=embedded_enabled,
        meta_app_id=meta_app_id,
        meta_config_id=meta_config_id,
    )


@school_bp.route("/admin/payments/paystack", methods=["POST"])
@require_admin
def payments_paystack():
    sid = _school_id()
    public_key = (request.form.get("public_key") or "").strip()
    secret_key = (request.form.get("secret_key") or "").strip()
    if not public_key or not secret_key:
        flash("Both Public Key and Secret Key are required.", "danger")
        return redirect(url_for("school.integrations"))

    secret_enc = school_payments._encrypt_key(secret_key)
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO school_payment_gateways (school_id, gateway, public_key, secret_key_enc)
        VALUES (%s, 'paystack', %s, %s)
        ON CONFLICT (school_id, gateway) DO UPDATE SET
          public_key=EXCLUDED.public_key, secret_key_enc=EXCLUDED.secret_key_enc,
          is_active=TRUE, updated_at=NOW()
    """, (sid, public_key, secret_enc))
    conn.commit(); cur.close(); conn.close()
    flash("Paystack keys saved and encrypted.", "success")
    return redirect(url_for("school.integrations"))


@school_bp.route("/admin/payments/paystack/remove", methods=["POST"])
@require_admin
def payments_paystack_remove():
    sid = _school_id()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM school_payment_gateways WHERE school_id=%s AND gateway='paystack'", (sid,))
    conn.commit(); cur.close(); conn.close()
    flash("Paystack disconnected.", "success")
    return redirect(url_for("school.integrations"))


@school_bp.route("/admin/payments/flutterwave", methods=["POST"])
@require_admin
def payments_flutterwave():
    sid = _school_id()
    public_key = (request.form.get("public_key") or "").strip()
    secret_key = (request.form.get("secret_key") or "").strip()
    if not public_key or not secret_key:
        flash("Both Public Key and Secret Key are required.", "danger")
        return redirect(url_for("school.integrations"))

    secret_enc = school_payments._encrypt_key(secret_key)
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO school_payment_gateways (school_id, gateway, public_key, secret_key_enc)
        VALUES (%s, 'flutterwave', %s, %s)
        ON CONFLICT (school_id, gateway) DO UPDATE SET
          public_key=EXCLUDED.public_key, secret_key_enc=EXCLUDED.secret_key_enc,
          is_active=TRUE, updated_at=NOW()
    """, (sid, public_key, secret_enc))
    conn.commit(); cur.close(); conn.close()
    flash("Flutterwave keys saved and encrypted.", "success")
    return redirect(url_for("school.integrations"))


@school_bp.route("/admin/payments/flutterwave/remove", methods=["POST"])
@require_admin
def payments_flutterwave_remove():
    sid = _school_id()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM school_payment_gateways WHERE school_id=%s AND gateway='flutterwave'", (sid,))
    conn.commit(); cur.close(); conn.close()
    flash("Flutterwave disconnected.", "success")
    return redirect(url_for("school.integrations"))


@school_bp.route("/admin/payments/default", methods=["POST"])
@require_admin
def payments_default():
    sid = _school_id()
    gateway = request.form.get("gateway")
    if gateway not in ("paystack", "flutterwave"):
        flash("Invalid gateway selection.", "danger")
        return redirect(url_for("school.integrations"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE school_profiles SET default_payment_gateway=%s WHERE id=%s", (gateway, sid))
    conn.commit(); cur.close(); conn.close()
    flash(f"{gateway.title()} set as default for parent payments.", "success")
    return redirect(url_for("school.integrations"))


@school_bp.route("/settings")
@require_login
def settings():
    sid    = _school_id()
    school = _get_school(sid)
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM school_staff WHERE id=%s", (_staff_id(),)
    )
    me = cur.fetchone()
    cur.close(); conn.close()
    is_admin = (_school_role() == "admin")
    return render_template("school/settings.html",
        school=school, me=me, is_admin=is_admin,
    )


_TEMPLATE_TYPES = [
    {
        "type": "absence_alert",
        "label": "Absence Alert",
        "icon": "✅",
        "description": "Sent when a student is marked absent.",
        "variables": "{{1}} student name · {{2}} class · {{3}} date · {{4}} school name",
        "sample": "Dear Parent/Guardian, this is to inform you that {{1}} ({{2}}) was "
                  "marked absent today, {{3}}. If this is a mistake or you have already "
                  "informed the school, please disregard this message. Sent by {{4}}.",
    },
    {
        "type": "fee_reminder",
        "label": "Fee Reminder",
        "icon": "💰",
        "description": "Sent to parents with an outstanding fee balance.",
        "variables": "{{1}} school name · {{2}} student name · {{3}} balance · {{4}} fee name · {{5}} due date",
        "sample": "Dear Parent/Guardian, this is {{1}} reminding you that {{2}} has an "
                  "outstanding balance of ₦{{3}} for {{4}}, due on {{5}}. Please settle "
                  "this at your earliest convenience.",
    },
]


@school_bp.route("/settings/templates")
@require_admin
def settings_templates():
    sid = _school_id()
    school = _get_school(sid)
    configured = {}
    for t in _TEMPLATE_TYPES:
        configured[t["type"]] = get_school_template(sid, t["type"])
    return render_template("school/settings_wa_templates.html",
        school=school, template_types=_TEMPLATE_TYPES, configured=configured,
    )


@school_bp.route("/settings/templates/save", methods=["POST"])
@require_admin
def settings_templates_save():
    sid  = _school_id()
    ttype = request.form.get("template_type", "")
    name  = request.form.get("template_name", "").strip()
    lang  = request.form.get("language_code", "en_US").strip() or "en_US"
    valid_types = {t["type"] for t in _TEMPLATE_TYPES}
    if ttype not in valid_types:
        flash("Unknown template type.", "danger")
        return redirect(url_for("school.settings_templates"))

    conn = get_db_connection()
    cur  = conn.cursor()
    if name:
        cur.execute("""
            INSERT INTO school_wa_templates (school_id, template_type, template_name, language_code)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (school_id, template_type)
            DO UPDATE SET template_name=EXCLUDED.template_name, language_code=EXCLUDED.language_code
        """, (sid, ttype, name, lang))
    else:
        cur.execute(
            "DELETE FROM school_wa_templates WHERE school_id=%s AND template_type=%s",
            (sid, ttype)
        )
    conn.commit()
    cur.close(); conn.close()

    flash("Template saved." if name else "Template cleared.", "success")
    return redirect(url_for("school.settings_templates"))


@school_bp.route("/settings/templates/check-status", methods=["POST"])
@require_admin
def settings_templates_check_status():
    sid = _school_id()
    ttype = request.form.get("template_type", "")
    template = get_school_template(sid, ttype)
    if not template:
        flash("No template configured for this type.", "warning")
        return redirect(url_for("school.settings_templates"))

    status = check_template_status(sid, template["template_name"])
    label = next((t["label"] for t in _TEMPLATE_TYPES if t["type"] == ttype), ttype)
    status_msg = {
        "APPROVED": f"✅ {label} template is APPROVED — ready to send.",
        "PENDING":  f"⏳ {label} template is still PENDING Meta review.",
        "REJECTED": f"❌ {label} template was REJECTED by Meta — check Meta Business Manager for the reason.",
        "NOT_FOUND": f"⚠️ {label} template name not found on your WhatsApp Business Account.",
        "ERROR": f"⚠️ Could not check status for {label} — check your WhatsApp connection.",
    }.get(status, f"Status: {status}")
    flash(status_msg, "success" if status == "APPROVED" else "warning")
    return redirect(url_for("school.settings_templates"))


@school_bp.route("/settings/save", methods=["POST"])
@require_admin
def settings_save():
    sid             = _school_id()
    school_name     = request.form.get("school_name", "").strip()
    school_type     = request.form.get("school_type", "secondary")
    state           = request.form.get("state", "").strip()
    lga             = request.form.get("lga", "").strip()
    address         = request.form.get("address", "").strip()
    principal_name  = request.form.get("principal_name", "").strip()
    current_session = request.form.get("current_session", "").strip()
    current_term    = request.form.get("current_term", "First")
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE school_profiles SET
          school_name=%s, school_type=%s, state=%s, lga=%s,
          address=%s, principal_name=%s,
          current_session=%s, current_term=%s
        WHERE id=%s
    """, (school_name, school_type, state, lga, address, principal_name,
          current_session, current_term, sid))
    conn.commit()
    cur.close(); conn.close()
    session["school_name"] = school_name
    flash("Settings saved.", "success")
    return redirect(url_for("school.settings"))


_STAFF_HISTORY_TABLES = [
    ("school_attendance", "marked_by"),
    ("school_fee_payments", "last_reminded_by"),
    ("school_broadcasts", "created_by"),
    ("school_kb_documents", "uploaded_by"),
]


def _staff_has_history(staff_id: int) -> bool:
    """True if this staff member has any activity recorded against them —
    used to gate hard delete (deleting would otherwise hit a FK violation,
    or silently lose audit history)."""
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        for table, col in _STAFF_HISTORY_TABLES:
            cur.execute(f"SELECT 1 FROM {table} WHERE {col}=%s LIMIT 1", (staff_id,))
            if cur.fetchone():
                return True
        return False
    finally:
        cur.close(); conn.close()


@school_bp.route("/staff")
@require_admin
def staff():
    sid    = _school_id()
    school = _get_school(sid)
    classes = _get_classes(sid)
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM school_staff WHERE school_id=%s ORDER BY is_active DESC, role, full_name",
        (sid,)
    )
    staff_list = cur.fetchall()
    cur.execute("SELECT * FROM school_staff WHERE id=%s", (_staff_id(),))
    me = cur.fetchone()
    cur.close(); conn.close()

    for s in staff_list:
        s["has_history"] = _staff_has_history(s["id"])

    return render_template("school/staff.html",
        school=school, staff_list=staff_list, me=me, classes=classes,
    )


@school_bp.route("/staff/add", methods=["POST"])
@require_admin
def staff_add():
    sid            = _school_id()
    full_name      = request.form.get("full_name", "").strip()
    email          = request.form.get("email", "").strip().lower()
    password       = request.form.get("password", "")
    role           = request.form.get("role", "teacher")
    class_assigned = request.form.get("class_assigned", "").strip() or None
    if not full_name or not email or not password:
        flash("Name, email and password are required.", "danger")
        return redirect(url_for("school.staff"))
    cap_err = _check_staff_cap(sid)
    if cap_err:
        flash(cap_err, "warning")
        return redirect(url_for("school.billing_plans"))
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO school_staff (school_id, full_name, email, password_hash, role, class_assigned)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (sid, full_name, email, generate_password_hash(password), role, class_assigned))
        conn.commit()
        flash(f"{full_name} added as {role}.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not add staff: {e}", "danger")
    finally:
        cur.close(); conn.close()
    return redirect(url_for("school.staff"))


@school_bp.route("/staff/<int:staff_id>/edit", methods=["POST"])
@require_admin
def staff_edit(staff_id):
    sid            = _school_id()
    full_name      = request.form.get("full_name", "").strip()
    email          = request.form.get("email", "").strip().lower()
    password       = request.form.get("password", "")
    role           = request.form.get("role", "teacher")
    class_assigned = request.form.get("class_assigned", "").strip() or None
    if not full_name or not email:
        flash("Name and email are required.", "danger")
        return redirect(url_for("school.staff"))
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        if password:
            cur.execute("""
                UPDATE school_staff
                SET full_name=%s, email=%s, role=%s, class_assigned=%s, password_hash=%s
                WHERE id=%s AND school_id=%s
            """, (full_name, email, role, class_assigned, generate_password_hash(password), staff_id, sid))
        else:
            cur.execute("""
                UPDATE school_staff
                SET full_name=%s, email=%s, role=%s, class_assigned=%s
                WHERE id=%s AND school_id=%s
            """, (full_name, email, role, class_assigned, staff_id, sid))
        conn.commit()
        flash(f"{full_name} updated.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not update staff: {e}", "danger")
    finally:
        cur.close(); conn.close()
    return redirect(url_for("school.staff"))


@school_bp.route("/staff/<int:staff_id>/reactivate", methods=["POST"])
@require_admin
def staff_reactivate(staff_id):
    sid = _school_id()
    cap_err = _check_staff_cap(sid)
    if cap_err:
        flash(cap_err, "warning")
        return redirect(url_for("school.billing_plans"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE school_staff SET is_active=TRUE WHERE id=%s AND school_id=%s",
        (staff_id, sid)
    )
    conn.commit()
    cur.close(); conn.close()
    flash("Staff member reactivated.", "success")
    return redirect(url_for("school.staff"))


@school_bp.route("/staff/<int:staff_id>/delete", methods=["POST"])
@require_admin
def staff_delete(staff_id):
    sid = _school_id()
    if staff_id == _staff_id():
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("school.staff"))
    if _staff_has_history(staff_id):
        flash("This staff member has activity history (attendance/reminders/broadcasts/uploads) — deactivate instead of deleting.", "warning")
        return redirect(url_for("school.staff"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "DELETE FROM school_staff WHERE id=%s AND school_id=%s",
        (staff_id, sid)
    )
    conn.commit()
    cur.close(); conn.close()
    flash("Staff member deleted.", "success")
    return redirect(url_for("school.staff"))


@school_bp.route("/settings/wa", methods=["POST"])
@require_admin
def settings_wa():
    sid              = _school_id()
    phone_number_id  = request.form.get("wa_phone_number_id", "").strip()
    access_token     = request.form.get("wa_access_token", "").strip()
    waba_id          = request.form.get("wa_waba_id", "").strip()
    display_phone    = request.form.get("wa_display_phone", "").strip()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE school_profiles SET
          wa_phone_number_id=%s, wa_access_token=%s, wa_waba_id=%s,
          wa_display_phone=%s
        WHERE id=%s
    """, (phone_number_id or None, access_token or None, waba_id or None,
          display_phone or None, sid))
    conn.commit()
    cur.close(); conn.close()
    flash("WhatsApp credentials saved.", "success")
    return redirect(url_for("school.settings") + "#wa-setup")


@school_bp.route("/settings/wa/test", methods=["POST"])
@require_admin
def settings_wa_test():
    sid         = _school_id()
    school      = _get_school(sid)
    test_number = request.form.get("test_number", "").strip()
    if not test_number:
        flash("Enter a phone number to test.", "warning")
        return redirect(url_for("school.settings") + "#wa-setup")
    if not school.get("wa_phone_number_id"):
        flash("WhatsApp credentials not saved yet.", "warning")
        return redirect(url_for("school.settings") + "#wa-setup")
    ok = send_wa_text(
        sid, test_number,
        f"✅ *{school['school_name']}*\n\nYour WhatsApp Business number is connected to PhiXtra School. "
        f"Parents can now message this number to get instant answers."
    )
    flash(f"Test message {'sent' if ok else 'failed — check credentials'}.", "success" if ok else "danger")
    return redirect(url_for("school.settings") + "#wa-setup")


@school_bp.route("/staff/<int:staff_id>/deactivate", methods=["POST"])
@require_admin
def staff_deactivate(staff_id):
    sid = _school_id()
    if staff_id == _staff_id():
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("school.staff"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE school_staff SET is_active=FALSE WHERE id=%s AND school_id=%s",
        (staff_id, sid)
    )
    conn.commit()
    cur.close(); conn.close()
    flash("Staff member deactivated.", "success")
    return redirect(url_for("school.staff"))


@school_bp.route("/settings/change-password", methods=["POST"])
@require_login
def change_password():
    staff_id    = _staff_id()
    current_pw  = request.form.get("current_password", "")
    new_pw      = request.form.get("new_password", "")
    confirm_pw  = request.form.get("confirm_password", "")
    if new_pw != confirm_pw:
        flash("New passwords do not match.", "danger")
        return redirect(url_for("school.settings") + "#my-account")
    if len(new_pw) < 6:
        flash("Password must be at least 6 characters.", "danger")
        return redirect(url_for("school.settings") + "#my-account")
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT password_hash FROM school_staff WHERE id=%s", (staff_id,))
    row = cur.fetchone()
    if not row or not check_password_hash(row["password_hash"], current_pw):
        flash("Current password is incorrect.", "danger")
        cur.close(); conn.close()
        return redirect(url_for("school.settings") + "#my-account")
    cur.execute(
        "UPDATE school_staff SET password_hash=%s WHERE id=%s",
        (generate_password_hash(new_pw), staff_id)
    )
    conn.commit()
    cur.close(); conn.close()
    flash("Password updated.", "success")
    return redirect(url_for("school.settings") + "#my-account")


# ── Onboarding Wizard ──────────────────────────────────────────────────────────

@school_bp.route("/onboarding")
@require_login
def onboarding_index():
    sid  = _school_id()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT onboarding_step FROM school_profiles WHERE id=%s", (sid,))
    row  = cur.fetchone()
    cur.close(); conn.close()
    step = int((row or [0])[0] or 0)
    if step >= 4:
        return redirect(url_for("school.dashboard"))
    return redirect(url_for("school.onboarding", step=max(1, step + 1)))


@school_bp.route("/onboarding/<int:step>", methods=["GET", "POST"])
@require_login
def onboarding(step):
    sid    = _school_id()
    school = _get_school(sid)
    meta_app_id    = os.getenv("META_APP_ID", "")
    meta_config_id = os.getenv("META_CONFIG_ID", "")
    embedded_enabled = bool(meta_app_id and meta_config_id)

    if step == 1:
        if request.method == "POST":
            conn = get_db_connection()
            cur  = conn.cursor()
            cur.execute("""
                UPDATE school_profiles SET
                  school_name=%s, school_type=%s, state=%s, lga=%s,
                  address=%s, principal_name=%s,
                  current_session=%s, current_term=%s,
                  onboarding_step=GREATEST(onboarding_step, 1)
                WHERE id=%s
            """, (
                request.form.get("school_name","").strip(),
                request.form.get("school_type","secondary"),
                request.form.get("state","").strip(),
                request.form.get("lga","").strip(),
                request.form.get("address","").strip(),
                request.form.get("principal_name","").strip(),
                request.form.get("current_session","2025/2026").strip(),
                request.form.get("current_term","First"),
                sid,
            ))
            conn.commit(); cur.close(); conn.close()
            session["school_name"] = request.form.get("school_name","").strip()
            return redirect(url_for("school.onboarding", step=2))
        return render_template("school/onboarding_1.html", school=school, step=1)

    if step == 2:
        return render_template("school/onboarding_2.html",
            school=school, step=2,
            embedded_enabled=embedded_enabled,
            meta_app_id=meta_app_id,
            meta_config_id=meta_config_id,
        )

    if step == 3:
        if request.method == "POST":
            test_number = request.form.get("test_number","").strip()
            if test_number and school.get("wa_phone_number_id"):
                from school_wa import send_wa_text
                send_wa_text(
                    sid, test_number,
                    f"✅ Hello from {school['school_name']}! Your WhatsApp is connected to PhiXtra School. Parents can now message this number to get instant AI-powered answers."
                )
                flash(f"Test message sent to {test_number}.", "success")
            conn = get_db_connection()
            cur  = conn.cursor()
            cur.execute("UPDATE school_profiles SET onboarding_step=GREATEST(onboarding_step,3) WHERE id=%s", (sid,))
            conn.commit(); cur.close(); conn.close()
            return redirect(url_for("school.onboarding", step=4))
        return render_template("school/onboarding_3.html", school=school, step=3)

    if step == 4:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("UPDATE school_profiles SET onboarding_step=4 WHERE id=%s", (sid,))
        conn.commit(); cur.close(); conn.close()
        return render_template("school/onboarding_4.html", school=school, step=4)

    return redirect(url_for("school.dashboard"))


@school_bp.route("/onboarding/skip")
@require_login
def onboarding_skip():
    sid = _school_id()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE school_profiles SET onboarding_step=4 WHERE id=%s", (sid,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("school.dashboard"))


# ── Embedded Signup callback (school version) ──────────────────────────────────

@school_bp.route("/onboarding/wa-embedded-callback", methods=["POST"])
@require_login
def wa_embedded_callback():
    """Exchange Meta auth code for long-lived token, save to school_profiles."""
    import requests as _req
    sid  = _school_id()
    data = request.get_json(silent=True) or {}
    code            = (data.get("code")            or "").strip()
    phone_number_id = (data.get("phone_number_id") or "").strip()
    waba_id         = (data.get("waba_id")         or "").strip()

    if not code:
        return jsonify({"error": "No auth code received. Please try again."}), 400

    app_id     = os.getenv("META_APP_ID", "")
    app_secret = os.getenv("META_APP_SECRET", "")
    graph      = "https://graph.facebook.com/v19.0"

    # Exchange code → short-lived token
    r1 = _req.get(f"{graph}/oauth/access_token", params={
        "client_id": app_id, "client_secret": app_secret, "code": code,
    }, timeout=15)
    if r1.status_code != 200:
        return jsonify({"error": "Failed to exchange auth code. Please try again."}), 400
    short_token = r1.json().get("access_token")

    # Exchange short → long-lived (60 days)
    r2 = _req.get(f"{graph}/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": app_id, "client_secret": app_secret,
        "fb_exchange_token": short_token,
    }, timeout=15)
    token = r2.json().get("access_token", short_token) if r2.status_code == 200 else short_token

    # Discover phone numbers under this WABA
    phones = []
    wabas  = [{"id": waba_id}] if waba_id else []
    if not wabas:
        r3 = _req.get(f"{graph}/me/whatsapp_business_accounts",
                      params={"access_token": token, "fields": "id,name"}, timeout=15)
        wabas = r3.json().get("data", []) if r3.status_code == 200 else []
    for w in wabas:
        r4 = _req.get(f"{graph}/{w['id']}/phone_numbers",
                      params={"access_token": token,
                              "fields": "id,display_phone_number,verified_name,status"}, timeout=15)
        if r4.status_code == 200:
            for pn in r4.json().get("data", []):
                phones.append({
                    "waba_id":              w["id"],
                    "phone_number_id":      pn["id"],
                    "display_phone_number": pn.get("display_phone_number", ""),
                    "verified_name":        pn.get("verified_name", ""),
                })

    if not phones:
        return jsonify({"error": "No WhatsApp phone numbers found on this account."}), 400

    # Store pending data in session
    session["school_wa_pending_token"]  = token
    session["school_wa_pending_phones"] = phones

    if len(phones) == 1:
        pn = phones[0]
        _save_school_wa(sid, pn["phone_number_id"], pn["waba_id"], token,
                        pn["display_phone_number"], pn["verified_name"])
        return jsonify({
            "status": "connected",
            "display_phone": pn["display_phone_number"],
            "verified_name": pn["verified_name"],
        })

    return jsonify({"status": "select_phone", "phone_options": phones})


@school_bp.route("/onboarding/wa-manual-save", methods=["POST"])
@require_login
def wa_manual_save():
    sid             = _school_id()
    phone_number_id = request.form.get("phone_number_id","").strip()
    waba_id         = request.form.get("waba_id","").strip()
    access_token    = request.form.get("access_token","").strip()
    display_phone   = request.form.get("display_phone","").strip()

    if not all([phone_number_id, waba_id, access_token]):
        flash("Phone Number ID, WABA ID, and Access Token are required.", "danger")
        return redirect(url_for("school.onboarding", step=2))

    _save_school_wa(sid, phone_number_id, waba_id, access_token, display_phone)
    flash("WhatsApp credentials saved.", "success")
    return redirect(url_for("school.onboarding", step=3))


@school_bp.route("/onboarding/wa-embedded-complete", methods=["POST"])
@require_login
def wa_embedded_complete():
    sid             = _school_id()
    phone_number_id = request.form.get("phone_number_id","").strip()
    waba_id         = request.form.get("waba_id","").strip()
    display_phone   = request.form.get("display_phone","").strip()
    verified_name   = request.form.get("verified_name","").strip()
    token           = session.pop("school_wa_pending_token", None)
    session.pop("school_wa_pending_phones", None)

    if not token:
        flash("Session expired. Please try connecting again.", "danger")
        return redirect(url_for("school.onboarding", step=2))

    _save_school_wa(sid, phone_number_id, waba_id, token, display_phone, verified_name)
    flash("WhatsApp connected successfully!", "success")
    return redirect(url_for("school.onboarding", step=3))


def _save_school_wa(school_id, phone_number_id, waba_id, token,
                    display_phone="", verified_name=""):
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE school_profiles SET
          wa_phone_number_id=%s, wa_waba_id=%s, wa_access_token=%s,
          wa_display_phone=%s, wa_verified_name=%s,
          onboarding_step=GREATEST(onboarding_step, 2)
        WHERE id=%s
    """, (phone_number_id, waba_id, token, display_phone, verified_name, school_id))
    conn.commit(); cur.close(); conn.close()
