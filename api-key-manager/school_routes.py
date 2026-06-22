"""
school_routes.py — PhiXtra School (school.phixtra.com)
Blueprint prefix: / (host-matched by LiteSpeed to school.phixtra.com)
"""
import csv
import io
import os
import datetime
from functools import wraps

import psycopg2.extras
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, jsonify, make_response,
)
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_db_connection
from school_wa import (
    send_attendance_alert, send_fee_reminder,
    send_broadcast, send_wa_text,
)

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


@school_bp.route("/register", methods=["GET", "POST"])
def register():
    if _logged_in():
        return redirect(url_for("school.dashboard"))
    if request.method == "POST":
        school_name    = request.form.get("school_name", "").strip()
        school_type    = request.form.get("school_type", "secondary")
        state          = request.form.get("state", "").strip()
        principal_name = request.form.get("principal_name", "").strip()
        admin_name     = request.form.get("admin_name", "").strip()
        email          = request.form.get("email", "").strip().lower()
        password       = request.form.get("password", "")
        if not all([school_name, email, password, admin_name]):
            flash("Please fill all required fields.", "danger")
            return render_template("school/register.html")
        conn = get_db_connection()
        cur  = conn.cursor()
        try:
            cur.execute(
                "SELECT 1 FROM school_staff WHERE email=%s", (email,)
            )
            if cur.fetchone():
                flash("An account with that email already exists.", "danger")
                return render_template("school/register.html")
            cur.execute("""
                INSERT INTO school_profiles
                  (school_name, school_type, state, principal_name, contact_email)
                VALUES (%s,%s,%s,%s,%s) RETURNING id
            """, (school_name, school_type, state, principal_name, email))
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
    return render_template("school/register.html")

# ── Dashboard ──────────────────────────────────────────────────────────────────

@school_bp.route("/dashboard")
@require_login
def dashboard():
    sid    = _school_id()
    school = _get_school(sid)
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    today  = datetime.date.today().isoformat()

    # ── Stat cards ──────────────────────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) AS n FROM school_students WHERE school_id=%s AND is_active=TRUE", (sid,))
    total_students = (cur.fetchone() or {}).get("n", 0)

    cur.execute("""
        SELECT
          COUNT(*)                                                   AS total,
          COUNT(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM school_student_parents sp WHERE sp.parent_id = p.id
          ))                                                         AS linked
        FROM school_parents p WHERE p.school_id=%s
    """, (sid,))
    pr = cur.fetchone() or {}
    total_parents  = pr.get("total", 0)
    parents_linked = pr.get("linked", 0)

    cur.execute("""
        SELECT
          COUNT(*) FILTER (WHERE status='present') AS present,
          COUNT(*) FILTER (WHERE status='absent')  AS absent,
          COUNT(*) FILTER (WHERE status='late')    AS late,
          COUNT(*)                                  AS total
        FROM school_attendance
        WHERE school_id=%s AND attendance_date=%s
    """, (sid, today))
    att = cur.fetchone() or {}

    cur.execute("""
        SELECT COUNT(*) AS n FROM school_fee_payments fp
        JOIN school_fee_schedules fs ON fs.id = fp.schedule_id
        WHERE fs.school_id=%s AND fp.status IN ('unpaid','partial')
    """, (sid,))
    outstanding_fees = (cur.fetchone() or {}).get("n", 0)

    # ── Today's attendance by class ─────────────────────────────────────────────
    cur.execute("""
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
        WHERE s.school_id = %s AND s.is_active = TRUE
        GROUP BY s.class_name, s.arm
        ORDER BY s.class_name, s.arm
    """, (today, sid))
    att_by_class = cur.fetchall()

    # ── Overdue fee schedules ───────────────────────────────────────────────────
    cur.execute("""
        SELECT fs.name, fs.due_date,
               COUNT(fp.id) FILTER (WHERE fp.status IN ('unpaid','partial')) AS unpaid
        FROM school_fee_schedules fs
        LEFT JOIN school_fee_payments fp ON fp.schedule_id = fs.id
        WHERE fs.school_id=%s AND fs.due_date < %s
        GROUP BY fs.id
        HAVING COUNT(fp.id) FILTER (WHERE fp.status IN ('unpaid','partial')) > 0
        ORDER BY fs.due_date
        LIMIT 5
    """, (sid, today))
    overdue_fees = cur.fetchall()

    # ── Students with no parent ─────────────────────────────────────────────────
    cur.execute("""
        SELECT COUNT(*) AS n FROM school_students s
        WHERE s.school_id=%s AND s.is_active=TRUE
          AND NOT EXISTS (SELECT 1 FROM school_student_parents sp WHERE sp.student_id=s.id)
    """, (sid,))
    no_parent_count = (cur.fetchone() or {}).get("n", 0)

    # ── Recent broadcasts ───────────────────────────────────────────────────────
    cur.execute("""
        SELECT title, target_class, sent_count, delivered_count, status, sent_at
        FROM school_broadcasts
        WHERE school_id=%s ORDER BY created_at DESC LIMIT 5
    """, (sid,))
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
    q      = request.args.get("q", "").strip()
    cls    = request.args.get("class", "").strip()
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Summary stats
    cur.execute("""
        SELECT
          COUNT(*)                                                          AS total,
          COUNT(DISTINCT class_name)                                        AS class_count,
          SUM(CASE WHEN NOT EXISTS (
            SELECT 1 FROM school_student_parents sp WHERE sp.student_id = s.id
          ) THEN 1 ELSE 0 END)                                             AS no_parent_count
        FROM school_students s
        WHERE s.school_id=%s AND s.is_active=TRUE
    """, (sid,))
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
        classes=classes, q=q, selected_class=cls, stats=stats,
    )


@school_bp.route("/students/add", methods=["POST"])
@require_login
def students_add():
    sid = _school_id()
    full_name      = request.form.get("full_name", "").strip()
    student_number = request.form.get("student_number", "").strip()
    gender         = request.form.get("gender", "")
    class_name     = request.form.get("class_name", "").strip()
    arm            = request.form.get("arm", "A").strip().upper()
    if not full_name or not class_name:
        flash("Name and class are required.", "danger")
        return redirect(url_for("school.students"))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO school_students (school_id, full_name, student_number, gender, class_name, arm)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (sid, full_name, student_number or None, gender or None, class_name, arm))
    conn.commit()
    cur.close(); conn.close()
    flash(f"{full_name} added.", "success")
    return redirect(url_for("school.students"))


@school_bp.route("/students/import", methods=["POST"])
@require_login
def students_import():
    sid  = _school_id()
    file = request.files.get("csv_file")
    if not file or not file.filename.endswith(".csv"):
        flash("Please upload a CSV file.", "danger")
        return redirect(url_for("school.students"))
    text    = file.stream.read().decode("utf-8-sig")
    reader  = csv.DictReader(io.StringIO(text))
    added   = 0
    errors  = []
    conn    = get_db_connection()
    cur     = conn.cursor()
    for i, row in enumerate(reader, start=2):
        name  = (row.get("full_name") or row.get("name") or "").strip()
        cls   = (row.get("class_name") or row.get("class") or "").strip()
        if not name or not cls:
            errors.append(f"Row {i}: missing name or class")
            continue
        try:
            cur.execute("""
                INSERT INTO school_students
                  (school_id, full_name, student_number, gender, class_name, arm)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (
                sid, name,
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


@school_bp.route("/students/csv-template")
@require_login
def students_csv_template():
    """Download a blank CSV template for student import."""
    from flask import Response
    rows = [
        "full_name,student_number,class_name,arm,gender",
        "Temi Adeyemi,SCH2025001,JSS1,A,Female",
        "Emeka Okafor,SCH2025002,JSS1,A,Male",
        "Fatima Musa,SCH2025003,JSS2,B,Female",
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
    q      = request.args.get("q", "").strip()
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Summary stats
    cur.execute("""
        SELECT
          COUNT(*)                                                              AS total,
          COUNT(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM school_student_parents sp WHERE sp.parent_id = p.id
          ))                                                                    AS linked_count,
          COUNT(*) FILTER (WHERE NOT is_opted_in)                              AS opted_out
        FROM school_parents p
        WHERE p.school_id=%s
    """, (sid,))
    stats = cur.fetchone() or {}

    where  = ["p.school_id=%s"]
    params = [sid]
    if q:
        where.append("(p.full_name ILIKE %s OR p.whatsapp_number ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
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

    # Students for linking dropdown
    cur.execute(
        "SELECT id, full_name, class_name, arm FROM school_students "
        "WHERE school_id=%s AND is_active=TRUE ORDER BY class_name, full_name",
        (sid,)
    )
    student_options = cur.fetchall()
    cur.close(); conn.close()
    return render_template("school/parents.html",
        school=school, parents=parent_list, stats=stats,
        student_options=student_options, q=q,
    )


@school_bp.route("/parents/add", methods=["POST"])
@require_login
def parents_add():
    sid          = _school_id()
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
    sid  = _school_id()
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
    student_ids = request.form.getlist("student_ids")
    conn = get_db_connection()
    cur  = conn.cursor()
    for s_id in student_ids:
        try:
            cur.execute("""
                INSERT INTO school_student_parents (student_id, parent_id)
                SELECT %s, %s WHERE EXISTS (
                    SELECT 1 FROM school_students WHERE id=%s AND school_id=%s
                ) AND EXISTS (
                    SELECT 1 FROM school_parents WHERE id=%s AND school_id=%s
                )
                ON CONFLICT DO NOTHING
            """, (int(s_id), parent_id, int(s_id), sid, parent_id, sid))
        except Exception:
            pass
    conn.commit()
    cur.close(); conn.close()
    flash("Student(s) linked.", "success")
    return redirect(url_for("school.parents"))


@school_bp.route("/parents/<int:parent_id>/unlink/<int:student_id>", methods=["POST"])
@require_login
def parents_unlink(parent_id, student_id):
    """Remove a student-parent link."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM school_student_parents WHERE parent_id=%s AND student_id=%s",
                (parent_id, student_id))
    conn.commit()
    cur.close(); conn.close()
    flash("Child unlinked.", "success")
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

    # If teacher, auto-load their assigned class
    staff_class = None
    staff_id = _staff_id()
    if staff_id and _school_role() == "teacher":
        conn0 = get_db_connection()
        cur0  = conn0.cursor()
        cur0.execute("SELECT class_assigned FROM school_staff WHERE id=%s", (staff_id,))
        row0 = cur0.fetchone()
        cur0.close(); conn0.close()
        if row0:
            staff_class = row0[0]

    selected_class = request.args.get("class", staff_class or (classes[0] if classes else ""))
    selected_arm   = request.args.get("arm", "")
    date_str       = request.args.get("date", datetime.date.today().isoformat())

    arms = []
    students_in_class = []
    existing = {}

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
                SELECT student_id, status FROM school_attendance
                WHERE school_id=%s AND attendance_date=%s AND student_id = ANY(%s)
            """, (sid, date_str, ids))
            existing = {r["student_id"]: r["status"] for r in cur.fetchall()}

        cur.close(); conn.close()

    return render_template("school/attendance.html",
        school=school, classes=classes,
        selected_class=selected_class,
        selected_arm=selected_arm,
        arms=arms,
        date_str=date_str,
        students=students_in_class,
        existing=existing,
        staff_class=staff_class,
    )


@school_bp.route("/attendance/save", methods=["POST"])
@require_login
def attendance_save():
    sid          = _school_id()
    school       = _get_school(sid)
    date_str     = request.form.get("date", datetime.date.today().isoformat())
    class_name   = request.form.get("class_name", "")
    student_ids  = request.form.getlist("student_ids[]")
    absent_ids   = set(request.form.getlist("absent[]"))
    late_ids     = set(request.form.getlist("late[]"))

    if not student_ids:
        flash("No students found.", "warning")
        return redirect(url_for("school.attendance"))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    notified = 0

    for sid_str in student_ids:
        s_id   = int(sid_str)
        status = "absent" if sid_str in absent_ids else ("late" if sid_str in late_ids else "present")
        cur.execute("""
            INSERT INTO school_attendance (school_id, student_id, attendance_date, status)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (student_id, attendance_date)
            DO UPDATE SET status=EXCLUDED.status
        """, (sid, s_id, date_str, status))

        # WhatsApp alert for absences
        if status == "absent" and school.get("wa_phone_number_id"):
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
    cls     = request.args.get("class", "")
    status  = request.args.get("status", "")
    from_d  = request.args.get("from", (datetime.date.today() - datetime.timedelta(days=14)).isoformat())
    to_d    = request.args.get("to",   datetime.date.today().isoformat())
    classes = _get_classes(sid)

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
        SELECT a.attendance_date, a.student_id, a.status, a.wa_notified,
               s.full_name, s.class_name, s.arm
        FROM school_attendance a
        JOIN school_students s ON s.id = a.student_id
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
        SELECT s.full_name, s.class_name, s.arm,
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
        classes=classes, selected_class=cls,
        selected_status=status,
        from_d=from_d, to_d=to_d,
        stats=stats, att_pct=att_pct,
        absentees=absentees,
        total_shown=len(raw),
    )

# ── Fees ───────────────────────────────────────────────────────────────────────

@school_bp.route("/fees")
@require_login
def fees():
    sid    = _school_id()
    school = _get_school(sid)
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT fs.*,
               COUNT(fp.id)                                              AS total_students,
               SUM(CASE WHEN fp.status='paid'    THEN 1 ELSE 0 END)     AS paid_count,
               SUM(CASE WHEN fp.status='partial' THEN 1 ELSE 0 END)     AS partial_count,
               SUM(CASE WHEN fp.status='unpaid'  THEN 1 ELSE 0 END)     AS unpaid_count,
               COALESCE(SUM(fp.amount_paid), 0)                         AS total_collected,
               COALESCE(SUM(fs.amount), 0)                              AS total_expected
        FROM school_fee_schedules fs
        LEFT JOIN school_fee_payments fp ON fp.schedule_id = fs.id
        WHERE fs.school_id=%s AND fs.is_active=TRUE
        GROUP BY fs.id
        ORDER BY fs.due_date DESC NULLS LAST, fs.created_at DESC
    """, (sid,))
    schedules = cur.fetchall()

    # Overall summary
    cur.execute("""
        SELECT
          COUNT(DISTINCT fs.id)                                          AS schedule_count,
          COALESCE(SUM(fs.amount * COALESCE(fp_cnt.n, 0)), 0)           AS grand_expected,
          COALESCE(SUM(fp_sum.collected), 0)                            AS grand_collected
        FROM school_fee_schedules fs
        LEFT JOIN (
          SELECT schedule_id, COUNT(*) AS n FROM school_fee_payments GROUP BY schedule_id
        ) fp_cnt ON fp_cnt.schedule_id = fs.id
        LEFT JOIN (
          SELECT schedule_id, SUM(amount_paid) AS collected FROM school_fee_payments GROUP BY schedule_id
        ) fp_sum ON fp_sum.schedule_id = fs.id
        WHERE fs.school_id=%s AND fs.is_active=TRUE
    """, (sid,))
    summary = cur.fetchone() or {}

    classes = _get_classes(sid)
    today   = datetime.date.today()
    cur.close(); conn.close()
    return render_template("school/fees.html",
        school=school, schedules=schedules, classes=classes,
        summary=summary, today=today,
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
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM school_fee_schedules WHERE id=%s AND school_id=%s",
        (schedule_id, sid)
    )
    schedule = cur.fetchone()
    if not schedule:
        flash("Fee schedule not found.", "danger")
        return redirect(url_for("school.fees"))

    status_filter = request.args.get("status", "")
    where = ["fp.schedule_id=%s"]
    params = [schedule_id]
    if status_filter:
        where.append("fp.status=%s")
        params.append(status_filter)

    # Payment summary stats (always across full schedule, not filtered)
    cur.execute("""
        SELECT
          COUNT(*)                                                    AS total,
          SUM(CASE WHEN status='paid'    THEN 1 ELSE 0 END)          AS paid_count,
          SUM(CASE WHEN status='partial' THEN 1 ELSE 0 END)          AS partial_count,
          SUM(CASE WHEN status='unpaid'  THEN 1 ELSE 0 END)          AS unpaid_count,
          COALESCE(SUM(amount_paid), 0)                              AS total_collected
        FROM school_fee_payments WHERE schedule_id=%s
    """, (schedule_id,))
    pay_stats = cur.fetchone() or {}

    cur.execute(f"""
        SELECT fp.*, s.full_name, s.class_name, s.arm, s.student_number
        FROM school_fee_payments fp
        JOIN school_students s ON s.id = fp.student_id
        WHERE {' AND '.join(where)}
        ORDER BY s.class_name, s.arm, s.full_name
    """, params)
    payments = cur.fetchall()
    cur.close(); conn.close()

    total_expected = float(schedule["amount"]) * int(pay_stats.get("total") or 0)
    collect_pct = round(100 * float(pay_stats.get("total_collected") or 0) / total_expected) \
                  if total_expected else 0

    return render_template("school/fee_payments.html",
        school=school, schedule=schedule,
        payments=payments, status_filter=status_filter,
        pay_stats=pay_stats, collect_pct=collect_pct,
        total_expected=total_expected,
        today=datetime.date.today().isoformat(),
    )


@school_bp.route("/fees/<int:schedule_id>/payment", methods=["POST"])
@require_login
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
@require_login
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
    if not school.get("wa_phone_number_id"):
        flash("WhatsApp is not connected. Go to Settings → WhatsApp Setup.", "warning")
        return redirect(url_for("school.fee_payments", schedule_id=schedule_id))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM school_fee_schedules WHERE id=%s AND school_id=%s",
        (schedule_id, sid)
    )
    schedule = cur.fetchone()
    if not schedule:
        flash("Not found.", "danger")
        cur.close(); conn.close()
        return redirect(url_for("school.fees"))

    cur.execute("""
        SELECT fp.amount_paid, s.full_name,
               p.whatsapp_number
        FROM school_fee_payments fp
        JOIN school_students s ON s.id = fp.student_id
        JOIN school_student_parents ssp ON ssp.student_id = s.id
        JOIN school_parents p ON p.id = ssp.parent_id
        WHERE fp.schedule_id=%s AND fp.status IN ('unpaid','partial') AND p.is_opted_in=TRUE
    """, (schedule_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()

    sent = 0
    total = float(schedule["amount"])
    due   = schedule["due_date"].strftime("%d %b %Y") if schedule.get("due_date") else "N/A"
    for r in rows:
        balance = total - float(r["amount_paid"])
        ok = send_fee_reminder(
            school_id=sid,
            parent_wa=r["whatsapp_number"],
            student_name=r["full_name"],
            fee_name=schedule["name"],
            amount=total,
            balance=balance,
            due_date=due,
            school_name=school["school_name"],
        )
        if ok:
            sent += 1

    flash(f"Reminder sent to {sent} parent(s).", "success")
    return redirect(url_for("school.fee_payments", schedule_id=schedule_id))

# ── Broadcast ──────────────────────────────────────────────────────────────────

@school_bp.route("/broadcast")
@require_login
def broadcast():
    sid     = _school_id()
    school  = _get_school(sid)
    classes = _get_classes(sid)
    conn    = get_db_connection()
    cur     = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT * FROM school_broadcasts WHERE school_id=%s
        ORDER BY created_at DESC LIMIT 50
    """, (sid,))
    history = cur.fetchall()

    # Parent reach counts per class (for live "X parents" preview in compose)
    cur.execute("""
        SELECT s.class_name, COUNT(DISTINCT p.id) AS cnt
        FROM school_parents p
        JOIN school_student_parents ssp ON ssp.parent_id = p.id
        JOIN school_students s ON s.id = ssp.student_id
        WHERE s.school_id=%s AND p.is_opted_in=TRUE AND s.is_active=TRUE
        GROUP BY s.class_name
    """, (sid,))
    class_counts = {r["class_name"]: int(r["cnt"]) for r in cur.fetchall()}

    cur.execute("""
        SELECT COUNT(DISTINCT id) AS cnt FROM school_parents
        WHERE school_id=%s AND is_opted_in=TRUE
    """, (sid,))
    total_parents = int((cur.fetchone() or {}).get("cnt") or 0)

    cur.close(); conn.close()
    return render_template("school/broadcast.html",
        school=school, classes=classes, history=history,
        class_counts=class_counts, total_parents=total_parents,
    )


@school_bp.route("/broadcast/send", methods=["POST"])
@require_login
def broadcast_send():
    sid          = _school_id()
    school       = _get_school(sid)
    title        = request.form.get("title", "").strip()
    message      = request.form.get("message", "").strip()
    target_class = request.form.get("target_class", "").strip() or None

    if not title or not message:
        flash("Title and message are required.", "danger")
        return redirect(url_for("school.broadcast"))
    if not school.get("wa_phone_number_id"):
        flash("WhatsApp is not connected. Go to Settings → WhatsApp Setup.", "warning")
        return redirect(url_for("school.broadcast"))

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
@require_login
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
@require_login
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
        VALUES (%s,%s,%s,%s)
    """, (sid, category, question, answer))
    conn.commit()
    cur.close(); conn.close()
    flash("Knowledge entry added.", "success")
    return redirect(url_for("school.knowledge"))


@school_bp.route("/knowledge/<int:entry_id>/edit", methods=["POST"])
@require_login
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
    flash("Entry updated.", "success")
    return redirect(url_for("school.knowledge"))


@school_bp.route("/knowledge/<int:entry_id>/delete", methods=["POST"])
@require_login
def knowledge_delete(entry_id):
    sid = _school_id()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "DELETE FROM school_knowledge WHERE id=%s AND school_id=%s",
        (entry_id, sid)
    )
    conn.commit()
    cur.close(); conn.close()
    flash("Entry deleted.", "success")
    return redirect(url_for("school.knowledge"))

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


@school_bp.route("/settings")
@require_login
def settings():
    sid    = _school_id()
    school = _get_school(sid)
    conn   = get_db_connection()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM school_staff WHERE school_id=%s AND is_active=TRUE ORDER BY role, full_name",
        (sid,)
    )
    staff_list = cur.fetchall()
    cur.execute(
        "SELECT * FROM school_staff WHERE id=%s", (_staff_id(),)
    )
    me = cur.fetchone()
    cur.close(); conn.close()
    is_admin = (_school_role() == "admin")
    return render_template("school/settings.html",
        school=school, staff_list=staff_list, me=me, is_admin=is_admin,
    )


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


@school_bp.route("/settings/staff/add", methods=["POST"])
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
        return redirect(url_for("school.settings"))
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
    return redirect(url_for("school.settings"))


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


@school_bp.route("/settings/staff/<int:staff_id>/remove", methods=["POST"])
@require_admin
def staff_remove(staff_id):
    sid = _school_id()
    if staff_id == _staff_id():
        flash("You cannot remove your own account.", "danger")
        return redirect(url_for("school.settings") + "#staff")
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE school_staff SET is_active=FALSE WHERE id=%s AND school_id=%s",
        (staff_id, sid)
    )
    conn.commit()
    cur.close(); conn.close()
    flash("Staff member deactivated.", "success")
    return redirect(url_for("school.settings") + "#staff")


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
