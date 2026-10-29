from datetime import date, datetime, timedelta
import csv
import io
import os
import secrets
from math import hypot
from pathlib import Path
import sqlite3
from time import monotonic

from flask import Flask, Response, flash, g, redirect, render_template, request, session, stream_with_context, url_for

from database import DB_PATH, get_db, init_db, seed_database
from optimization import build_allocation_plan, build_recommendations, calculate_metrics
from threading import Condition


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("HRPFOS_SECRET_KEY") or secrets.token_hex(32)
app.config["DATABASE"] = DB_PATH
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["TEMPLATES_AUTO_RELOAD"] = True
DEMO_USERS = {
    "ashish": {"password": os.environ.get("HRPFOS_ASHISH_PASSWORD"), "role": "Administrator"},
    "ankita": {"password": os.environ.get("HRPFOS_ANKITA_PASSWORD"), "role": "Staff"},
    "tanmay": {"password": os.environ.get("HRPFOS_TANMAY_PASSWORD"), "role": "Staff"},
    "aman": {"password": os.environ.get("HRPFOS_AMAN_PASSWORD"), "role": "Staff"},
}
dashboard_event_condition = Condition()
dashboard_event_version = 0
AMBULANCE_TRANSITIONS = {
    "Available": ("Dispatched", "Maintenance"),
    "Dispatched": ("On Route",),
    "On Route": ("Arrived",),
    "Arrived": ("At Hospital", "Completed"),
    "At Hospital": ("Completed",),
    "Completed": ("Available", "Maintenance"),
    "Maintenance": ("Available",),
}
dashboard_reload_paths = tuple(
    Path(__file__).resolve().parent / path
    for path in (
        "app.py",
        "database.py",
        "optimization.py",
        "templates/base.html",
        "templates/dashboard.html",
        "static/css/style.css",
    )
)


def dashboard_asset_signature():
    signature = []
    for path in dashboard_reload_paths:
        try:
            signature.append((path, path.stat().st_mtime_ns))
        except OSError:
            signature.append((path, None))
    return tuple(signature)


def notify_dashboard_update():
    global dashboard_event_version
    with dashboard_event_condition:
        dashboard_event_version += 1
        dashboard_event_condition.notify_all()


@app.before_request
def require_login():
    if session.get("user"):
        session.permanent = True
    if request.endpoint not in {"login", "static", "dashboard", "dashboard_events"} and not session.get("user"):
        return redirect(url_for("login", next=request.path))


@app.route("/dashboard/events")
def dashboard_events():
    @stream_with_context
    def event_stream():
        global dashboard_event_version
        with dashboard_event_condition:
            last_version = dashboard_event_version
        last_assets = dashboard_asset_signature()
        last_heartbeat = monotonic()
        yield f"event: connected\ndata: {last_version}\n\n"
        while True:
            with dashboard_event_condition:
                dashboard_event_condition.wait(timeout=1)
                current_version = dashboard_event_version
            current_assets = dashboard_asset_signature()
            if current_version != last_version or current_assets != last_assets:
                last_version = current_version
                last_assets = current_assets
                last_heartbeat = monotonic()
                yield f"event: update\ndata: {current_version}\n\n"
            elif monotonic() - last_heartbeat >= 15:
                last_heartbeat = monotonic()
                yield ": keep-alive\n\n"

    response = Response(event_stream(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        user = DEMO_USERS.get(username)
        if user and user["password"] and secrets.compare_digest(user["password"], request.form.get("password", "")):
            session.permanent = True
            session["user"] = username
            session["role"] = user["role"]
            return redirect(request.args.get("next") or request.form.get("next") or url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.teardown_appcontext
def close_database(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def log_action(db, resource_type, action, details):
    db.execute(
        "INSERT INTO audit_logs (resource_type, action, details, created_at) VALUES (?, ?, ?, ?)",
        (resource_type, action, details, datetime.now().isoformat(timespec="minutes")),
    )


@app.route("/")
def dashboard():
    db = get_db()
    metrics = calculate_metrics(db)
    recommendations = build_recommendations(db, metrics)
    recent_patients = db.execute(
        "SELECT * FROM patients ORDER BY arrival_time DESC LIMIT 8"
    ).fetchall()
    bed_assignments = db.execute(
        "SELECT b.id AS bed_id, b.bed_number, b.ward, b.expected_discharge, "
        "p.id AS patient_id, p.name AS patient_name, p.department "
        "FROM beds b JOIN patients p ON p.assigned_bed_id = b.id "
        "ORDER BY b.ward, b.bed_number"
    ).fetchall()
    staff_assignments = db.execute(
        "SELECT p.id AS patient_id, p.name AS patient_name, p.department, "
        "s.name AS staff_name, s.role, s.shift "
        "FROM patients p JOIN staff s ON s.id = p.assigned_staff_id "
        "ORDER BY p.department, p.name"
    ).fetchall()
    ambulances = db.execute("SELECT * FROM ambulances ORDER BY priority DESC").fetchall()
    return render_template(
        "dashboard.html",
        metrics=metrics,
        recommendations=recommendations,
        recent_patients=recent_patients,
        bed_assignments=bed_assignments,
        staff_assignments=staff_assignments,
        ambulances=ambulances,
    )


@app.route("/patients")
def patients():
    db = get_db()
    search = request.args.get("q", "").strip()
    department = request.args.get("department", "").strip()
    priority = request.args.get("priority", "").strip()
    status = request.args.get("status", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 10
    rows = db.execute(
        "SELECT p.*, b.bed_number AS assigned_bed_number, s.name AS assigned_staff_name "
        "FROM patients p LEFT JOIN beds b ON b.id = p.assigned_bed_id LEFT JOIN staff s ON s.id = p.assigned_staff_id WHERE "
        "(? = '' OR p.name LIKE '%' || ? || '%' OR p.problem LIKE '%' || ? || '%' OR p.department LIKE '%' || ? || '%') "
        "AND (? = '' OR p.department = ?) AND (? = '' OR p.priority = ?) AND (? = '' OR p.status = ?) "
        "ORDER BY p.arrival_time DESC",
        (search, search, search, search, department, department, priority, priority, status, status),
    ).fetchall()
    total_filtered = len(rows)
    total_pages = max(1, (total_filtered + per_page - 1) // per_page)
    page = min(page, total_pages)
    rows = rows[(page - 1) * per_page: page * per_page]
    patient_rows = []
    for row in rows:
        patient_row = dict(row)
        patient_row["waiting_text"] = "—"
        if row["status"] in ("Waiting", "In Consultation"):
            try:
                patient_row["waiting_text"] = f"{max(0, int((datetime.now() - datetime.fromisoformat(row['arrival_time'])).total_seconds() / 60))} min"
            except ValueError:
                patient_row["waiting_text"] = "0 min"
        patient_rows.append(patient_row)
    departments = db.execute("SELECT DISTINCT department FROM patients ORDER BY department").fetchall()
    status_counts = db.execute(
        "SELECT status, COUNT(*) AS total FROM patients GROUP BY status"
    ).fetchall()
    counts = {row["status"]: row["total"] for row in status_counts}
    return render_template("patients.html", patients=patient_rows, status_counts=counts,
                           departments=departments, filters={"q": search, "department": department,
                           "priority": priority, "status": status}, page=page, total_pages=total_pages)


@app.route("/patients/add", methods=("GET", "POST"))
def add_patient():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        department = request.form.get("department", "").strip()
        priority = request.form.get("priority", "Medium")
        problem = request.form.get("problem", "General check-up").strip() or "General check-up"
        operation_time = request.form.get("operation_time") or None
        age = request.form.get("age", type=int)
        phone = request.form.get("phone", "").strip() or None
        address = request.form.get("address", "").strip() or None
        emergency_contact = request.form.get("emergency_contact", "").strip() or None
        blood_group = request.form.get("blood_group", "").strip() or None
        if not name or not department or age is None or not 0 < age < 130:
            flash("Please enter a valid name, age, and department.", "error")
            return render_template("patient_form.html")
        db = get_db()
        db.execute(
            "INSERT INTO patients (name, age, department, priority, arrival_time, status, problem, operation_time, status_updated_at, phone, address, emergency_contact, blood_group) "
            "VALUES (?, ?, ?, ?, ?, 'Waiting', ?, ?, ?, ?, ?, ?, ?)",
            (name, age, department, priority, datetime.now().isoformat(timespec="minutes"), problem, operation_time,
             datetime.now().isoformat(timespec="minutes"), phone, address, emergency_contact, blood_group),
        )
        db.commit()
        notify_dashboard_update()
        flash("Patient added to the flow queue.", "success")
        return redirect(url_for("patients"))
    return render_template("patient_form.html")


def assignment_beds(db, patient_id):
    return db.execute(
        "SELECT * FROM beds WHERE status = 'Available' OR id = (SELECT assigned_bed_id FROM patients WHERE id = ?) ORDER BY ward, bed_number",
        (patient_id,),
    ).fetchall()


def assignment_staff(db, patient_id):
    return db.execute(
        "SELECT * FROM staff WHERE status = 'Available' OR id = (SELECT assigned_staff_id FROM patients WHERE id = ?) ORDER BY department, role, name",
        (patient_id,),
    ).fetchall()


@app.route("/patients/<int:patient_id>/edit", methods=("GET", "POST"))
def edit_patient(patient_id):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if patient is None:
        flash("Patient record not found.", "error")
        return redirect(url_for("patients"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        department = request.form.get("department", "").strip()
        priority = request.form.get("priority", "Medium")
        problem = request.form.get("problem", "General check-up").strip() or "General check-up"
        operation_time = request.form.get("operation_time") or None
        phone = request.form.get("phone", "").strip() or None
        address = request.form.get("address", "").strip() or None
        emergency_contact = request.form.get("emergency_contact", "").strip() or None
        blood_group = request.form.get("blood_group", "").strip() or None
        bed_id = request.form.get("assigned_bed_id", type=int)
        staff_id = request.form.get("assigned_staff_id", type=int)
        age = request.form.get("age", type=int)
        if not name or not department or age is None or not 0 < age < 130:
            flash("Please enter a valid name, age, and department.", "error")
            return render_template("patient_form.html", patient=patient, edit_mode=True,
                                   beds=assignment_beds(db, patient_id), staff=assignment_staff(db, patient_id))
        old_bed_id = patient["assigned_bed_id"]
        old_staff_id = patient["assigned_staff_id"]
        if bed_id:
            bed = db.execute("SELECT id FROM beds WHERE id = ? AND (status = 'Available' OR id = ?)",
                             (bed_id, old_bed_id or 0)).fetchone()
            if bed is None:
                flash("Selected bed is not available.", "error")
                return render_template("patient_form.html", patient=patient, edit_mode=True,
                                       beds=assignment_beds(db, patient_id), staff=assignment_staff(db, patient_id))
        if staff_id:
            staff_member = db.execute("SELECT id FROM staff WHERE id = ? AND (status = 'Available' OR id = ?)",
                                      (staff_id, old_staff_id or 0)).fetchone()
            if staff_member is None:
                flash("Selected staff member is not available.", "error")
                return render_template("patient_form.html", patient=patient, edit_mode=True,
                                       beds=assignment_beds(db, patient_id), staff=assignment_staff(db, patient_id))
        if old_bed_id:
            db.execute("UPDATE beds SET status = 'Available', assigned_patient = NULL, admission_time = NULL, expected_discharge = NULL WHERE id = ?",
                       (old_bed_id,))
        if old_staff_id:
            db.execute("UPDATE staff SET status = 'Available' WHERE id = ?", (old_staff_id,))
        if bed_id:
            db.execute("UPDATE beds SET status = 'Occupied', assigned_patient = ?, admission_time = ?, expected_discharge = NULL WHERE id = ?",
                       (name, datetime.now().isoformat(timespec="minutes"), bed_id))
        if staff_id:
            db.execute("UPDATE staff SET status = 'Busy' WHERE id = ?", (staff_id,))
        db.execute(
            "UPDATE patients SET name = ?, age = ?, department = ?, priority = ?, problem = ?, operation_time = ?, phone = ?, address = ?, emergency_contact = ?, blood_group = ?, assigned_bed_id = ?, assigned_staff_id = ? WHERE id = ?",
            (name, age, department, priority, problem, operation_time, phone, address, emergency_contact, blood_group, bed_id, staff_id, patient_id),
        )
        log_action(db, "Patient", "Updated", f"Patient {patient_id} details updated")
        log_action(db, "Patient", "Assignment updated", f"Patient {patient_id} assigned bed {bed_id or 'none'} and staff {staff_id or 'none'}")
        db.commit()
        notify_dashboard_update()
        flash("Patient details updated.", "success")
        return redirect(url_for("patients"))
    return render_template("patient_form.html", patient=patient, edit_mode=True,
                           beds=assignment_beds(db, patient_id), staff=assignment_staff(db, patient_id))


@app.route("/patients/<int:patient_id>")
def patient_detail(patient_id):
    db = get_db()
    patient = db.execute(
        "SELECT p.*, b.bed_number AS assigned_bed_number, b.ward AS assigned_ward, "
        "s.name AS assigned_staff_name, s.role AS assigned_staff_role "
        "FROM patients p LEFT JOIN beds b ON b.id = p.assigned_bed_id "
        "LEFT JOIN staff s ON s.id = p.assigned_staff_id WHERE p.id = ?",
        (patient_id,),
    ).fetchone()
    if patient is None:
        flash("Patient record not found.", "error")
        return redirect(url_for("patients"))
    history = db.execute(
        "SELECT * FROM audit_logs WHERE resource_type = 'Patient' AND details LIKE ? ORDER BY id DESC LIMIT 30",
        (f"%Patient {patient_id}%",),
    ).fetchall()
    return render_template("patient_detail.html", patient=patient, history=history)


@app.route("/resources")
def resources():
    db = get_db()
    bed_filter = request.args.get("bed_status", "")
    staff_filter = request.args.get("staff_status", "")
    beds = db.execute("SELECT * FROM beds WHERE (? = '' OR status = ?) ORDER BY ward, bed_number",
                      (bed_filter, bed_filter)).fetchall()
    staff = db.execute("SELECT * FROM staff WHERE (? = '' OR status = ?) ORDER BY department, name",
                       (staff_filter, staff_filter)).fetchall()
    assignable_patients = db.execute(
        "SELECT id, name, department, status, assigned_bed_id FROM patients "
        "WHERE status NOT IN ('Completed', 'Discharged') "
        "ORDER BY CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, name"
    ).fetchall()
    bed_total = db.execute("SELECT COUNT(*) FROM beds").fetchone()[0]
    bed_available = db.execute("SELECT COUNT(*) FROM beds WHERE status = 'Available'").fetchone()[0]
    staff_total = db.execute("SELECT COUNT(*) FROM staff").fetchone()[0]
    staff_available = db.execute("SELECT COUNT(*) FROM staff WHERE status = 'Available'").fetchone()[0]
    workload = db.execute("SELECT department, COUNT(*) AS total FROM patients WHERE status IN ('Waiting', 'In Consultation') GROUP BY department ORDER BY total DESC").fetchall()
    resource_summary = db.execute(
        "WITH departments AS ("
        "SELECT department FROM patients UNION SELECT department FROM staff UNION SELECT ward AS department FROM beds) "
        "SELECT d.department, "
        "(SELECT COUNT(*) FROM patients p WHERE p.department = d.department AND p.status NOT IN ('Completed', 'Discharged')) AS active_patients, "
        "(SELECT COUNT(*) FROM staff s WHERE s.department = d.department) AS total_staff, "
        "(SELECT COUNT(*) FROM staff s WHERE s.department = d.department AND s.status = 'Available') AS available_staff, "
        "(SELECT COUNT(*) FROM beds b WHERE b.ward = d.department) AS total_beds, "
        "(SELECT COUNT(*) FROM beds b WHERE b.ward = d.department AND b.status = 'Available') AS available_beds "
        "FROM departments d ORDER BY d.department"
    ).fetchall()
    shortage_alerts = []
    for row in resource_summary:
        if row["active_patients"] and row["available_staff"] == 0:
            shortage_alerts.append(f"{row['department']}: no available staff for {row['active_patients']} active patients")
        if row["active_patients"] and row["available_beds"] == 0:
            shortage_alerts.append(f"{row['department']}: no available beds for {row['active_patients']} active patients")
    logs = db.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 10").fetchall()
    return render_template("resources.html", beds=beds, staff=staff, workload=workload, logs=logs,
                           bed_total=bed_total, bed_available=bed_available,
                           staff_total=staff_total, staff_available=staff_available,
                           bed_filter=bed_filter, staff_filter=staff_filter,
                           resource_summary=resource_summary, shortage_alerts=shortage_alerts,
                           assignable_patients=assignable_patients)


@app.route("/beds/add", methods=("POST",))
def add_bed():
    bed_number = request.form.get("bed_number", "").strip()
    ward = request.form.get("ward", "").strip()
    bed_type = request.form.get("bed_type", "Standard")
    if not bed_number or not ward:
        flash("Bed number and ward are required.", "error")
        return redirect(url_for("resources"))
    db = get_db()
    duplicate = db.execute("SELECT 1 FROM beds WHERE bed_number = ?", (bed_number,)).fetchone()
    if duplicate:
        flash("That bed number already exists.", "error")
        return redirect(url_for("resources"))
    db.execute("INSERT INTO beds (bed_number, ward, bed_type, status) VALUES (?, ?, ?, 'Available')",
               (bed_number, ward, bed_type))
    log_action(db, "Bed", "Added", f"{bed_number} added to {ward} ward")
    db.commit()
    notify_dashboard_update()
    flash(f"Bed {bed_number} added successfully.", "success")
    return redirect(url_for("resources"))


@app.route("/staff/add", methods=("POST",))
def add_staff():
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "Nurse")
    department = request.form.get("department", "").strip()
    if not name or not department:
        flash("Staff name and department are required.", "error")
        return redirect(url_for("resources"))
    db = get_db()
    shift = request.form.get("shift", "Day")
    db.execute("INSERT INTO staff (name, role, department, shift, status) VALUES (?, ?, ?, ?, 'Available')",
               (name, role, department, shift))
    log_action(db, "Staff", "Added", f"{name} added to {department} ({shift} shift)")
    db.commit()
    notify_dashboard_update()
    flash(f"{name} added to staff availability.", "success")
    return redirect(url_for("resources"))


def filtered_ambulance_trips(db):
    filters = {key: request.args.get(key, "").strip() for key in
               ("trip_q", "trip_status", "trip_from", "trip_to")}
    if filters["trip_status"] not in {"", "active", "completed"}:
        raise ValueError("Choose a valid trip status.")
    for key in ("trip_from", "trip_to"):
        if filters[key]:
            try:
                date.fromisoformat(filters[key])
            except ValueError:
                raise ValueError("Enter valid trip dates.") from None
    if filters["trip_from"] and filters["trip_to"] and filters["trip_from"] > filters["trip_to"]:
        raise ValueError("Trip start date must be on or before the end date.")
    clauses, params = [], []
    if filters["trip_q"]:
        clauses.append("(a.ambulance_number LIKE ? OR t.patient_name LIKE ?)")
        params.extend([f"%{filters['trip_q']}%"] * 2)
    if filters["trip_status"]:
        clauses.append("t.completed_at IS " + ("NOT NULL" if filters["trip_status"] == "completed" else "NULL"))
    if filters["trip_from"]:
        clauses.append("substr(t.started_at, 1, 10) >= ?")
        params.append(filters["trip_from"])
    if filters["trip_to"]:
        clauses.append("substr(t.started_at, 1, 10) <= ?")
        params.append(filters["trip_to"])
    sql = "SELECT t.*, a.ambulance_number FROM ambulance_trips t JOIN ambulances a ON a.id=t.ambulance_id"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return db.execute(sql + " ORDER BY t.id DESC", params).fetchall(), filters


@app.route("/ambulances/trips/export")
def export_ambulance_trips():
    try:
        trips, _ = filtered_ambulance_trips(get_db())
    except ValueError as exc:
        return str(exc), 400
    columns = ("id", "ambulance_number", "patient_id", "patient_name", "pickup_location",
               "destination", "started_at", "completed_at", "duration_minutes")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for trip in trips:
        writer.writerow([("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value)
                         for value in (trip[key] for key in columns)])
    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=ambulance_trips.csv"
    return response


@app.route("/ambulances")
def ambulances():
    db = get_db()
    status_filter = request.args.get("status", "").strip()
    rows = db.execute(
        "SELECT * FROM ambulances WHERE (? = '' OR status = ?) ORDER BY priority DESC, ambulance_number",
        (status_filter, status_filter),
    ).fetchall()
    status_counts = db.execute("SELECT status, COUNT(*) AS total FROM ambulances GROUP BY status").fetchall()
    counts = {row["status"]: row["total"] for row in status_counts}
    low_fuel = db.execute("SELECT * FROM ambulances WHERE fuel_level <= 25 ORDER BY fuel_level, priority DESC").fetchall()
    dispatch_history = db.execute(
        "SELECT * FROM audit_logs WHERE resource_type = 'Ambulance' ORDER BY id DESC LIMIT 12"
    ).fetchall()
    completed_trips = db.execute(
        "SELECT * FROM audit_logs WHERE resource_type = 'Ambulance' AND details LIKE '%marked Completed%' ORDER BY id DESC LIMIT 20"
    ).fetchall()
    available_for_dispatch = db.execute("SELECT * FROM ambulances WHERE status = 'Available' ORDER BY priority DESC").fetchall()
    nearest_ambulance = None
    if available_for_dispatch:
        nearest_ambulance = min(available_for_dispatch, key=lambda row: hypot(row["latitude"] - 28.54, row["longitude"] - 77.42))
    maintenance_due = db.execute("SELECT * FROM ambulances WHERE maintenance_due IS NOT NULL AND maintenance_due != '' ORDER BY maintenance_due").fetchall()
    today = date.today().isoformat()
    overdue_ids = {row["id"] for row in maintenance_due if row["maintenance_due"] < today}
    try:
        trips, trip_filters = filtered_ambulance_trips(db)
        trip_error = None
    except ValueError as exc:
        trips = []
        trip_filters = {key: request.args.get(key, "") for key in ("trip_q", "trip_status", "trip_from", "trip_to")}
        trip_error = str(exc)
    latitudes = [row["latitude"] for row in rows] or [28.5]
    longitudes = [row["longitude"] for row in rows] or [77.5]
    map_points = [{"vehicle": row,
                   "x": 8 + 84 * (row["longitude"] - min(longitudes)) / max(max(longitudes) - min(longitudes), 0.01),
                   "y": 8 + 84 * (max(latitudes) - row["latitude"]) / max(max(latitudes) - min(latitudes), 0.01)}
                  for row in rows]
    return render_template("ambulances.html", ambulances=rows, status_counts=counts,
                           low_fuel=low_fuel, dispatch_history=dispatch_history,
                           completed_trips=completed_trips, maintenance_due=maintenance_due,
                           nearest_ambulance=nearest_ambulance, status_filter=status_filter,
                           transitions=AMBULANCE_TRANSITIONS,
                           overdue_ids=overdue_ids, map_points=map_points,
                           trip_filters=trip_filters, trip_error=trip_error,
                           trip_total=db.execute("SELECT COUNT(*) FROM ambulance_trips").fetchone()[0],
                           patients=db.execute("SELECT id, name, status FROM patients ORDER BY name").fetchall(),
                           trips=trips,
                           refills=db.execute("SELECT f.*, a.ambulance_number FROM fuel_refills f JOIN ambulances a ON a.id=f.ambulance_id ORDER BY f.id DESC LIMIT 20").fetchall())


@app.route("/analytics")
def analytics():
    db = get_db()
    department_rows = db.execute(
        "SELECT department, COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'Waiting' THEN 1 ELSE 0 END) AS waiting, "
        "SUM(CASE WHEN priority = 'High' THEN 1 ELSE 0 END) AS high_priority "
        "FROM patients GROUP BY department ORDER BY waiting DESC, total DESC"
    ).fetchall()
    status_rows = db.execute(
        "SELECT status, COUNT(*) AS total FROM patients GROUP BY status ORDER BY total DESC"
    ).fetchall()
    ward_rows = db.execute(
        "SELECT ward, COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'Occupied' THEN 1 ELSE 0 END) AS occupied "
        "FROM beds GROUP BY ward ORDER BY ward"
    ).fetchall()
    max_waiting = max((row["waiting"] for row in department_rows), default=1)
    max_total = max((row["total"] for row in status_rows), default=1)
    return render_template("analytics.html", departments=department_rows, statuses=status_rows,
                           wards=ward_rows, max_waiting=max_waiting, max_status_total=max_total)


@app.route("/ambulances/<int:ambulance_id>/simulate", methods=("POST",))
def simulate_ambulance_location(ambulance_id):
    db = get_db()
    ambulance = db.execute("SELECT * FROM ambulances WHERE id = ?", (ambulance_id,)).fetchone()
    if ambulance is None:
        flash("Ambulance record not found.", "error")
        return redirect(url_for("ambulances"))
    if ambulance["status"] not in {"Dispatched", "On Route"}:
        flash("Set ambulance status to Dispatched or On Route before simulating movement.", "error")
        return redirect(url_for("ambulances"))
    step = 0.002 + ((ambulance_id % 3) * 0.001)
    latitude = max(-90, min(90, ambulance["latitude"] + step))
    longitude = max(-180, min(180, ambulance["longitude"] + step))
    db.execute("UPDATE ambulances SET status = 'On Route', latitude = ?, longitude = ?, current_location = ? WHERE id = ?",
               (latitude, longitude, f"Simulated route point ({latitude:.4f}, {longitude:.4f})", ambulance_id))
    log_action(db, "Ambulance", "GPS simulated", f"Ambulance {ambulance_id} moved to {latitude:.4f}, {longitude:.4f}")
    db.commit()
    notify_dashboard_update()
    flash("Simulated GPS position updated.", "success")
    return redirect(url_for("ambulances"))


@app.route("/reports")
def reports():
    db = get_db()
    admission_waiting = db.execute(
        "SELECT COUNT(*) FROM patients WHERE status IN ('Waiting', 'In Consultation')"
    ).fetchone()[0]
    admitted = db.execute("SELECT COUNT(*) FROM patients WHERE status = 'Admitted'").fetchone()[0]
    staff_by_department = db.execute(
        "SELECT department, COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'Available' THEN 1 ELSE 0 END) AS available, "
        "SUM(CASE WHEN status = 'Busy' THEN 1 ELSE 0 END) AS busy "
        "FROM staff GROUP BY department ORDER BY department"
    ).fetchall()
    daily_patients = db.execute(
        "SELECT substr(arrival_time, 1, 10) AS day, COUNT(*) AS total, "
        "GROUP_CONCAT(DISTINCT problem) AS problems "
        "FROM patients GROUP BY day ORDER BY day DESC"
    ).fetchall()
    operations = db.execute(
        "SELECT name, department, problem, operation_time, status, status_updated_at "
        "FROM patients ORDER BY status_updated_at DESC, arrival_time DESC LIMIT 50"
    ).fetchall()
    return render_template("reports.html", admission_waiting=admission_waiting, admitted=admitted,
                           staff_by_department=staff_by_department, daily_patients=daily_patients,
                           operations=operations)


@app.route("/allocation")
def allocation():
    return render_template("allocation.html", plan=build_allocation_plan(get_db()))


@app.route("/beds/<int:bed_id>/status", methods=("POST",))
def update_bed_status(bed_id):
    status = request.form.get("status", "Available")
    if status not in {"Available", "Occupied"}:
        flash("Invalid bed status.", "error")
    else:
        db = get_db()
        bed = db.execute("SELECT id FROM beds WHERE id = ?", (bed_id,)).fetchone()
        linked_patient = db.execute("SELECT id FROM patients WHERE assigned_bed_id = ? LIMIT 1", (bed_id,)).fetchone()
        if bed is None:
            flash("Bed record not found.", "error")
        elif status == "Available" and linked_patient:
            flash("Clear the bed assignment before marking this bed available.", "error")
        else:
            if status == "Available":
                db.execute("UPDATE beds SET status = ?, assigned_patient = NULL, admission_time = NULL, expected_discharge = NULL WHERE id = ?",
                           (status, bed_id))
            else:
                db.execute("UPDATE beds SET status = ? WHERE id = ?", (status, bed_id))
            log_action(db, "Bed", "Status updated", f"Bed {bed_id} marked {status}")
            db.commit()
            notify_dashboard_update()
            flash("Bed status updated.", "success")
    return redirect(url_for("resources"))


@app.route("/staff/<int:staff_id>/status", methods=("POST",))
def update_staff_status(staff_id):
    status = request.form.get("status", "Available")
    if status not in {"Available", "Busy"}:
        flash("Invalid staff status.", "error")
    else:
        db = get_db()
        staff_member = db.execute("SELECT id, name FROM staff WHERE id = ?", (staff_id,)).fetchone()
        if staff_member is None:
            flash("Staff member not found.", "error")
        else:
            if status == "Available":
                db.execute("UPDATE patients SET assigned_staff_id = NULL WHERE assigned_staff_id = ?", (staff_id,))
            db.execute("UPDATE staff SET status = ? WHERE id = ?", (status, staff_id))
            detail = f"Staff {staff_member['name']} marked {status}"
            if status == "Available":
                detail += "; patient assignments released"
            log_action(db, "Staff", "Status updated", detail)
            db.commit()
            notify_dashboard_update()
            flash("Staff status updated.", "success")
    return redirect(url_for("resources"))


@app.route("/beds/<int:bed_id>/delete", methods=("POST",))
def delete_bed(bed_id):
    db = get_db()
    bed = db.execute("SELECT bed_number FROM beds WHERE id = ?", (bed_id,)).fetchone()
    if bed is None:
        flash("Bed record not found.", "error")
        return redirect(url_for("resources"))
    db.execute("UPDATE patients SET assigned_bed_id = NULL WHERE assigned_bed_id = ?", (bed_id,))
    db.execute("DELETE FROM beds WHERE id = ?", (bed_id,))
    log_action(db, "Bed", "Deleted", f"Bed {bed['bed_number']} deleted; linked patients were unassigned")
    db.commit()
    notify_dashboard_update()
    flash("Bed deleted.", "success")
    return redirect(url_for("resources"))


@app.route("/staff/<int:staff_id>/delete", methods=("POST",))
def delete_staff(staff_id):
    db = get_db()
    staff_member = db.execute("SELECT name FROM staff WHERE id = ?", (staff_id,)).fetchone()
    if staff_member is None:
        flash("Staff member not found.", "error")
        return redirect(url_for("resources"))
    db.execute("UPDATE patients SET assigned_staff_id = NULL WHERE assigned_staff_id = ?", (staff_id,))
    db.execute("DELETE FROM staff WHERE id = ?", (staff_id,))
    log_action(db, "Staff", "Deleted", f"Staff {staff_member['name']} deleted; linked patient assignments were cleared")
    db.commit()
    notify_dashboard_update()
    flash("Staff member deleted.", "success")
    return redirect(url_for("resources"))


@app.route("/beds/<int:bed_id>/assignment", methods=("POST",))
def update_bed_assignment(bed_id):
    db = get_db()
    patient_value = request.form.get("assigned_patient_id", "").strip()
    try:
        patient_id = int(patient_value) if patient_value else None
    except ValueError:
        flash("Choose a valid patient.", "error")
        return redirect(url_for("resources"))
    expected = request.form.get("expected_discharge", "").strip() or None
    bed = db.execute("SELECT * FROM beds WHERE id = ?", (bed_id,)).fetchone()
    if bed is None:
        flash("Bed record not found.", "error")
        return redirect(url_for("resources"))

    current_patient = db.execute("SELECT id FROM patients WHERE assigned_bed_id = ? LIMIT 1", (bed_id,)).fetchone()
    if patient_id is None:
        db.execute("UPDATE patients SET assigned_bed_id = NULL WHERE assigned_bed_id = ?", (bed_id,))
        db.execute("UPDATE beds SET assigned_patient = NULL, expected_discharge = NULL, admission_time = NULL, status = 'Available' WHERE id = ?",
                   (bed_id,))
        detail = f"Bed {bed['bed_number']} assignment cleared"
    else:
        patient = db.execute(
            "SELECT id, name, status, assigned_bed_id FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if patient is None or patient["status"] in {"Completed", "Discharged"}:
            flash("Choose an active patient to assign to this bed.", "error")
            return redirect(url_for("resources"))
        if current_patient and current_patient["id"] != patient_id:
            flash("This bed is already assigned. Clear its current assignment first.", "error")
            return redirect(url_for("resources"))

        previous_bed_id = patient["assigned_bed_id"]
        if previous_bed_id and previous_bed_id != bed_id:
            db.execute(
                "UPDATE beds SET assigned_patient = NULL, expected_discharge = NULL, admission_time = NULL, status = 'Available' WHERE id = ?",
                (previous_bed_id,),
            )
        admission_time = bed["admission_time"] if current_patient and current_patient["id"] == patient_id else datetime.now().isoformat(timespec="minutes")
        db.execute("UPDATE patients SET assigned_bed_id = ? WHERE id = ?", (bed_id, patient_id))
        db.execute("UPDATE beds SET assigned_patient = ?, expected_discharge = ?, admission_time = ?, status = 'Occupied' WHERE id = ?",
                   (patient["name"], expected, admission_time, bed_id))
        detail = f"Bed {bed['bed_number']} assigned to patient {patient['name']} (ID {patient_id})"

    log_action(db, "Bed", "Assignment updated", detail)
    db.commit()
    notify_dashboard_update()
    flash("Bed assignment updated.", "success")
    return redirect(url_for("resources"))


@app.route("/ambulances/add", methods=("POST",))
def add_ambulance():
    db = get_db()
    number = request.form.get("ambulance_number", "").strip().upper()
    latitude = request.form.get("latitude", type=float)
    longitude = request.form.get("longitude", type=float)
    if not number or latitude is None or longitude is None or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        flash("Enter a vehicle number and valid coordinates.", "error")
    elif db.execute("SELECT id FROM ambulances WHERE UPPER(ambulance_number) = ?", (number,)).fetchone():
        flash("This vehicle number already exists.", "error")
    else:
        db.execute("INSERT INTO ambulances (ambulance_number, latitude, longitude, status, priority, driver_name) VALUES (?, ?, ?, 'Available', 1, ?)",
                   (number, latitude, longitude, request.form.get("driver_name", "").strip() or "Unassigned"))
        log_action(db, "Ambulance", "Added", f"Vehicle {number} added")
        db.commit()
        notify_dashboard_update()
        flash("Ambulance added.", "success")
    return redirect(url_for("ambulances"))


@app.route("/ambulances/<int:ambulance_id>/refill", methods=("POST",))
def refill_ambulance(ambulance_id):
    db = get_db()
    vehicle = db.execute("SELECT * FROM ambulances WHERE id = ?", (ambulance_id,)).fetchone()
    level = request.form.get("new_level", type=int)
    if vehicle is None:
        flash("Ambulance record not found.", "error")
    elif level is None or not vehicle["fuel_level"] < level <= 100:
        flash("Refill level must exceed current fuel and be at most 100%.", "error")
    else:
        now = datetime.now().isoformat(timespec="minutes")
        db.execute("INSERT INTO fuel_refills (ambulance_id, previous_level, new_level, created_at) VALUES (?, ?, ?, ?)",
                   (ambulance_id, vehicle["fuel_level"], level, now))
        db.execute("UPDATE ambulances SET fuel_level=?, fuel_refilled_at=?, fuel_refill_amount=? WHERE id=?",
                   (level, now, level - vehicle["fuel_level"], ambulance_id))
        log_action(db, "Ambulance", "Fuel refill", f"Ambulance {ambulance_id}: {vehicle['fuel_level']}% to {level}%")
        db.commit()
        notify_dashboard_update()
        flash("Fuel refill recorded.", "success")
    return redirect(url_for("ambulances"))


@app.route("/ambulances/<int:ambulance_id>/status", methods=("POST",))
def update_ambulance_details(ambulance_id):
    db = get_db()
    vehicle = db.execute("SELECT * FROM ambulances WHERE id=?", (ambulance_id,)).fetchone()
    if vehicle is None:
        flash("Ambulance record not found.", "error")
        return redirect(url_for("ambulances"))
    try:
        status = request.form.get("status", vehicle["status"])
        if status not in {"Available", "Dispatched", "On Route", "Arrived", "At Hospital", "Completed", "Maintenance"}:
            raise ValueError("Invalid ambulance status.")
        if status != vehicle["status"] and status not in AMBULANCE_TRANSITIONS.get(vehicle["status"], ()):
            raise ValueError(f"Cannot change {vehicle['status']} to {status}. Next: {', '.join(AMBULANCE_TRANSITIONS.get(vehicle['status'], ())) or 'none'}.")
        values = {key: request.form.get(key, vehicle[key]) for key in (
            "driver_name", "driver_phone", "pickup_location", "destination", "current_location",
            "eta", "emergency_level", "maintenance_due", "maintenance_notes")}
        latitude = float(request.form.get("latitude", vehicle["latitude"]))
        longitude = float(request.form.get("longitude", vehicle["longitude"]))
        fuel = int(request.form.get("fuel_level", vehicle["fuel_level"]))
        priority = int(request.form.get("priority", vehicle["priority"]))
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180 or not 0 <= fuel <= 100 or not 1 <= priority <= 5:
            raise ValueError("Check coordinates, fuel (0-100), and priority (1-5).")
        if values["emergency_level"] not in {"Normal", "High", "Critical"}:
            raise ValueError("Invalid emergency level.")
        for key in ("eta", "maintenance_due"):
            if values[key]:
                if key == "maintenance_due":
                    date.fromisoformat(values[key])
                else:
                    datetime.fromisoformat(values[key])
        patient_id = request.form.get("assigned_patient_id", vehicle["assigned_patient_id"])
        patient_id = int(patient_id) if patient_id else None
        patient = db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone() if patient_id else None
        if patient_id and (patient is None or (patient["status"] in {"Completed", "Discharged"} and patient_id != vehicle["assigned_patient_id"])):
            raise ValueError("Choose an active patient.")
        patient_name = patient["name"] if patient else "None"
        now = datetime.now().isoformat(timespec="minutes")
        trip = db.execute("SELECT * FROM ambulance_trips WHERE ambulance_id=? AND completed_at IS NULL ORDER BY id DESC LIMIT 1", (ambulance_id,)).fetchone()
        if trip and status in {"Available", "Maintenance"}:
            raise ValueError("Complete the active trip before making this vehicle available or scheduling maintenance.")
        if trip and patient_id != trip["patient_id"]:
            raise ValueError("Complete the active trip before changing its patient.")
        if status == "Dispatched" and vehicle["status"] == "Available" and not trip:
            if any(due and due < date.today().isoformat() for due in (vehicle["maintenance_due"], values["maintenance_due"])):
                raise ValueError("Maintenance is overdue. Update the service schedule and save before dispatching.")
            if not patient or not values["driver_name"] or values["driver_name"] == "Unassigned" or values["pickup_location"] in {None, "", "Not assigned"} or not values["destination"]:
                raise ValueError("Dispatch needs an active patient, driver, pickup, and destination.")
            if fuel <= 25 or vehicle["status"] == "Maintenance":
                raise ValueError("Refuel above 25% and mark maintenance vehicles Available before dispatch.")
            if db.execute("SELECT id FROM ambulance_trips WHERE patient_id=? AND completed_at IS NULL", (patient_id,)).fetchone():
                raise ValueError("This patient already has an active ambulance trip.")
            db.execute("INSERT INTO ambulance_trips (ambulance_id, patient_id, patient_name, pickup_location, destination, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (ambulance_id, patient_id, patient_name, values["pickup_location"], values["destination"], now))
            db.execute("UPDATE ambulances SET dispatch_time=?, trip_started_at=?, trip_completed_at=NULL, trip_duration_minutes=NULL WHERE id=?", (now, now, ambulance_id))
            values["current_location"] = values["pickup_location"]
            values["eta"] = values["eta"] or (datetime.now() + timedelta(minutes=30)).isoformat(timespec="minutes")
        if trip:
            db.execute("UPDATE ambulance_trips SET pickup_location=?, destination=? WHERE id=?", (values["pickup_location"], values["destination"], trip["id"]))
        if status == "Completed" and trip:
            duration = max(0, int((datetime.fromisoformat(now) - datetime.fromisoformat(trip["started_at"])).total_seconds() / 60))
            db.execute("UPDATE ambulance_trips SET completed_at=?, duration_minutes=? WHERE id=?", (now, duration, trip["id"]))
            db.execute("UPDATE ambulances SET trip_completed_at=?, trip_duration_minutes=? WHERE id=?", (now, duration, ambulance_id))
        if status == "Available":
            patient_id, patient_name = None, "None"
            values["current_location"], values["eta"] = "Base station", None
        elif status in {"Arrived", "At Hospital", "Completed"}:
            values["current_location"] = values["destination"]
        db.execute("UPDATE ambulances SET status=?, assigned_patient_id=?, assigned_patient=?, fuel_level=?, latitude=?, longitude=?, priority=?, " +
                   ", ".join(f"{key}=?" for key in values) + " WHERE id=?",
                   (status, patient_id, patient_name, fuel, latitude, longitude, priority, *values.values(), ambulance_id))
        log_action(db, "Ambulance", "Updated", f"Ambulance {ambulance_id} marked {status} - {patient_name}")
        db.commit()
        notify_dashboard_update()
        flash("Ambulance details updated.", "success")
    except (ValueError, TypeError) as exc:
        db.rollback()
        flash(str(exc) or "Enter valid ambulance details.", "error")
    return redirect(url_for("ambulances"))


@app.route("/export/<resource>")
def export_resource(resource):
    definitions = {
        "beds": ("beds", ["bed_number", "ward", "bed_type", "status", "assigned_patient", "expected_discharge"]),
        "staff": ("staff", ["name", "role", "department", "shift", "status"]),
        "patients": ("patients", ["name", "age", "department", "priority", "arrival_time", "status", "problem", "operation_time", "status_updated_at", "phone", "address", "emergency_contact", "blood_group"]),
        "ambulances": ("ambulances", ["ambulance_number", "driver_name", "driver_phone", "assigned_patient", "status", "fuel_level", "priority", "pickup_location", "current_location", "destination", "dispatch_time", "eta", "emergency_level", "maintenance_due", "maintenance_notes", "latitude", "longitude"]),
    }
    if resource not in definitions:
        return "Unknown resource", 404
    table, columns = definitions[resource]
    db = get_db()
    rows = db.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerows([tuple(row[column] for column in columns) for row in rows])
    response = app.response_class(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename={resource}.csv"
    return response


@app.route("/import/<resource>", methods=("POST",))
def import_resource(resource):
    upload = request.files.get("file")
    if resource not in {"beds", "staff", "patients", "ambulances"} or not upload or not upload.filename:
        flash("Choose a valid CSV file.", "error")
        return redirect(url_for("resources"))
    try:
        records = csv.DictReader(io.StringIO(upload.read().decode("utf-8-sig")))
        db = get_db()
        imported = 0
        for row in records:
            if resource == "beds" and row.get("bed_number") and row.get("ward"):
                db.execute("INSERT OR IGNORE INTO beds (bed_number, ward, bed_type, status) VALUES (?, ?, ?, ?)",
                           (row["bed_number"], row["ward"], row.get("bed_type", "Standard"), row.get("status", "Available")))
            elif resource == "staff" and row.get("name") and row.get("department"):
                db.execute("INSERT INTO staff (name, role, department, shift, status) VALUES (?, ?, ?, ?, ?)",
                           (row["name"], row.get("role", "Nurse"), row["department"], row.get("shift", "Day"), row.get("status", "Available")))
            elif resource == "patients" and row.get("name") and row.get("department"):
                db.execute("INSERT INTO patients (name, age, department, priority, arrival_time, status, problem, operation_time, status_updated_at, phone, address, emergency_contact, blood_group) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (row["name"], int(row.get("age", 0)), row["department"], row.get("priority", "Medium"),
                            row.get("arrival_time", datetime.now().isoformat(timespec="minutes")), row.get("status", "Waiting"),
                            row.get("problem", "General check-up"), row.get("operation_time") or None,
                            row.get("status_updated_at") or datetime.now().isoformat(timespec="minutes"),
                            row.get("phone") or None, row.get("address") or None,
                            row.get("emergency_contact") or None, row.get("blood_group") or None))
            elif resource == "ambulances" and row.get("ambulance_number"):
                db.execute("INSERT INTO ambulances (ambulance_number, latitude, longitude, status, priority, driver_name, driver_phone, assigned_patient, fuel_level, maintenance_due, maintenance_notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (row["ambulance_number"], float(row.get("latitude", 28.5)), float(row.get("longitude", 77.5)),
                            row.get("status", "Available"), int(row.get("priority", 1)), row.get("driver_name", "Unassigned"),
                            row.get("driver_phone") or None, row.get("assigned_patient", "None"), int(row.get("fuel_level", 100)),
                            row.get("maintenance_due") or None, row.get("maintenance_notes") or None))
            else:
                continue
            imported += 1
        log_action(db, resource.title(), "CSV imported", f"{imported} records imported")
        db.commit()
        notify_dashboard_update()
        flash(f"{imported} {resource} records imported.", "success")
    except (ValueError, UnicodeDecodeError, csv.Error):
        flash("CSV format is invalid.", "error")
    return redirect(url_for("resources" if resource != "ambulances" else "ambulances"))


@app.route("/patients/<int:patient_id>/status", methods=("POST",))
def update_patient_status(patient_id):
    status = request.form.get("status", "Waiting")
    allowed = {"Waiting", "In Consultation", "Admitted", "In Operation", "Completed", "Discharged"}
    if status not in allowed:
        flash("Invalid patient status.", "error")
        return redirect(url_for("patients"))
    db = get_db()
    updated_at = datetime.now().isoformat(timespec="minutes")
    operation_time = updated_at if status in {"In Operation", "Completed"} else None
    patient = db.execute("SELECT name, assigned_bed_id, assigned_staff_id FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if patient is None:
        flash("Patient record not found.", "error")
        return redirect(url_for("patients"))
    if status == "Discharged":
        if patient["assigned_bed_id"]:
            db.execute("UPDATE beds SET status = 'Available', assigned_patient = NULL, admission_time = NULL, expected_discharge = NULL WHERE id = ?", (patient["assigned_bed_id"],))
        if patient["assigned_staff_id"]:
            db.execute("UPDATE staff SET status = 'Available' WHERE id = ?", (patient["assigned_staff_id"],))
        db.execute("UPDATE patients SET status = ?, status_updated_at = ?, archived_at = ?, assigned_bed_id = NULL, assigned_staff_id = NULL WHERE id = ?",
                   (status, updated_at, updated_at, patient_id))
        log_action(db, "Patient", "Discharged", f"Patient {patient_id} discharged and resources released")
        db.commit()
        notify_dashboard_update()
        flash("Patient discharged and assigned resources released.", "success")
        return redirect(url_for("patients"))
    if operation_time:
        db.execute("UPDATE patients SET status = ?, status_updated_at = ?, operation_time = COALESCE(operation_time, ?) WHERE id = ?",
                   (status, updated_at, operation_time, patient_id))
    else:
        db.execute("UPDATE patients SET status = ?, status_updated_at = ? WHERE id = ?",
                   (status, updated_at, patient_id))
    log_action(db, "Patient", "Status updated", f"Patient {patient_id} moved to {status}")
    db.commit()
    notify_dashboard_update()
    flash("Patient status updated.", "success")
    return redirect(url_for("patients"))


@app.cli.command("seed")
def seed_command():
    seed_database(force=True)
    print("Synthetic hospital data created.")


def ensure_database():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    init_db()
    seed_database()


if __name__ == "__main__":
    ensure_database()
    app.run(debug=True)
