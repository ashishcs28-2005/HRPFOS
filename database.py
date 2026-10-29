from datetime import datetime, timedelta
from pathlib import Path
import random
import sqlite3


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "hospital.db")


def get_db():
    from flask import g

    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                age INTEGER NOT NULL,
                department TEXT NOT NULL,
                priority TEXT NOT NULL,
                arrival_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Waiting'
            );
            CREATE TABLE IF NOT EXISTS beds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bed_number TEXT NOT NULL,
                ward TEXT NOT NULL,
                bed_type TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS staff (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                department TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ambulances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ambulance_number TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_type TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ambulance_trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ambulance_id INTEGER NOT NULL,
                patient_id INTEGER,
                patient_name TEXT,
                pickup_location TEXT,
                destination TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_minutes INTEGER
            );
            CREATE TABLE IF NOT EXISTS fuel_refills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ambulance_id INTEGER NOT NULL,
                previous_level INTEGER NOT NULL,
                new_level INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        migrations = {
            "beds": {
                "assigned_patient": "TEXT",
                "admission_time": "TEXT",
                "expected_discharge": "TEXT",
            },
            "patients": {
                "problem": "TEXT DEFAULT 'General check-up'",
                "operation_time": "TEXT",
                "status_updated_at": "TEXT",
                "assigned_bed_id": "INTEGER",
                "assigned_staff_id": "INTEGER",
                "archived_at": "TEXT",
                "phone": "TEXT",
                "address": "TEXT",
                "emergency_contact": "TEXT",
                "blood_group": "TEXT",
            },
            "staff": {"shift": "TEXT DEFAULT 'Day'"},
            "ambulances": {
                "assigned_patient_id": "INTEGER",
                "driver_name": "TEXT DEFAULT 'Unassigned'",
                "assigned_patient": "TEXT DEFAULT 'None'",
                "fuel_level": "INTEGER DEFAULT 100",
                "pickup_location": "TEXT DEFAULT 'Not assigned'",
                "destination": "TEXT DEFAULT 'Hospital'",
                "dispatch_time": "TEXT",
                "eta": "TEXT",
                "emergency_level": "TEXT DEFAULT 'Normal'",
                "current_location": "TEXT DEFAULT 'Base station'",
                "driver_phone": "TEXT",
                "maintenance_due": "TEXT",
                "maintenance_notes": "TEXT",
                "fuel_refilled_at": "TEXT",
                "fuel_refill_amount": "INTEGER",
                "trip_started_at": "TEXT",
                "trip_completed_at": "TEXT",
                "trip_duration_minutes": "INTEGER",
            },
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, definition in columns.items():
                if column not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.commit()


def seed_database(force=False):
    init_db()
    now = datetime.now().replace(second=0, microsecond=0)
    departments = ["Emergency", "Cardiology", "Orthopedics", "Pediatrics", "General Medicine"]
    first_names = ["Aarav", "Ananya", "Rohan", "Ishita", "Kabir", "Meera", "Arjun", "Diya"]
    last_names = ["Sharma", "Singh", "Verma", "Gupta", "Khan", "Patel"]
    priorities = ["High", "Medium", "Low"]

    with sqlite3.connect(DB_PATH) as db:
        if force:
            db.execute("DELETE FROM ambulance_trips")
            db.execute("DELETE FROM fuel_refills")
            db.executescript("DELETE FROM patients; DELETE FROM beds; DELETE FROM staff; DELETE FROM ambulances;")
        if db.execute("SELECT COUNT(*) FROM patients").fetchone()[0] > 0:
            problems = ["Fever", "Chest pain", "Fracture", "Abdominal pain", "Breathing difficulty", "Routine check-up"]
            patient_rows = db.execute("SELECT id, status, problem, arrival_time, operation_time, status_updated_at FROM patients").fetchall()
            all_default_problems = bool(patient_rows) and all(row[2] == "General check-up" for row in patient_rows)
            for row in patient_rows:
                problem = random.choice(problems) if all_default_problems else (row[2] or "General check-up")
                operation_time = row[4] if row[1] in ("In Operation", "Completed") else None
                if row[1] not in ("In Operation", "Completed") and row[4]:
                    db.execute("UPDATE patients SET operation_time = NULL WHERE id = ?", (row[0],))
                if not operation_time and row[1] in ("In Operation", "Completed"):
                    try:
                        operation_time = (
                            datetime.fromisoformat(row[3]) + timedelta(minutes=random.randint(30, 180))
                        ).isoformat(timespec="minutes")
                    except ValueError:
                        operation_time = datetime.now().isoformat(timespec="minutes")
                if all_default_problems or not row[2] or operation_time:
                    status_updated_at = row[5] or row[3]
                    db.execute("UPDATE patients SET problem = ?, operation_time = ?, status_updated_at = COALESCE(status_updated_at, ?) WHERE id = ?",
                               (problem, operation_time, status_updated_at, row[0]))
                elif not row[5]:
                    db.execute("UPDATE patients SET operation_time = NULL, status_updated_at = ? WHERE id = ?", (row[3], row[0]))
            ambulance_drivers = ["Rajesh Kumar", "Vikram Singh", "Suresh Yadav", "Amit Verma", "Deepak Sharma", "Mohan Patel"]
            ambulance_rows = db.execute("SELECT id FROM ambulances ORDER BY ambulance_number").fetchall()
            for index, ambulance in enumerate(ambulance_rows):
                db.execute("UPDATE ambulances SET driver_name = ? WHERE id = ? AND (driver_name IS NULL OR driver_name = 'Unassigned')",
                           (ambulance_drivers[index % len(ambulance_drivers)], ambulance[0]))
            db.execute("UPDATE ambulances SET current_location = CASE WHEN status IN ('Arrived', 'At Hospital', 'Completed') THEN COALESCE(destination, 'Hospital') WHEN status IN ('Dispatched', 'On Route') THEN COALESCE(pickup_location, 'Not assigned') ELSE 'Base station' END WHERE current_location IS NULL OR current_location IN ('', 'Base station')")
            db.commit()
            return
        for i in range(24):
            arrival = now - timedelta(minutes=random.randint(5, 240))
            db.execute(
                "INSERT INTO patients (name, age, department, priority, arrival_time, status, problem, operation_time, status_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{random.choice(first_names)} {random.choice(last_names)}", random.randint(2, 85),
                 random.choice(departments), random.choices(priorities, weights=[2, 5, 2])[0],
                 arrival.isoformat(timespec="minutes"), random.choice(["Waiting", "Waiting", "In Consultation", "Completed"]),
                 random.choice(["Fever", "Chest pain", "Fracture", "Abdominal pain", "Breathing difficulty", "Routine check-up"]),
                 (arrival + timedelta(minutes=random.randint(30, 180))).isoformat(timespec="minutes"),
                 arrival.isoformat(timespec="minutes")),
            )
        wards = [("Emergency", "Standard"), ("ICU", "ICU"), ("General", "Standard"), ("Pediatrics", "Pediatric")]
        for i in range(20):
            ward, bed_type = random.choice(wards)
            db.execute("INSERT INTO beds (bed_number, ward, bed_type, status) VALUES (?, ?, ?, ?)",
                       (f"B-{i + 1:02d}", ward, bed_type, random.choices(["Available", "Occupied"], weights=[3, 2])[0]))
        staff_names = ["Dr. Neha Kapoor", "Dr. Rahul Mehta", "Nurse Priya", "Nurse Amit", "Dr. Sana Ali", "Nurse Kavita"]
        for name in staff_names:
            role = "Doctor" if name.startswith("Dr.") else "Nurse"
            db.execute("INSERT INTO staff (name, role, department, status) VALUES (?, ?, ?, ?)",
                       (name, role, random.choice(departments), random.choice(["Available", "Busy"])))
        ambulance_drivers = ["Rajesh Kumar", "Vikram Singh", "Suresh Yadav", "Amit Verma", "Deepak Sharma", "Mohan Patel"]
        for i in range(6):
            db.execute("INSERT INTO ambulances (ambulance_number, latitude, longitude, status, priority) VALUES (?, ?, ?, ?, ?)",
                       (f"AMB-{i + 1:02d}", 28.45 + random.uniform(-0.08, 0.08), 77.50 + random.uniform(-0.08, 0.08),
                        random.choice(["Available", "On Route", "At Hospital"]), random.randint(1, 5)))
            db.execute("UPDATE ambulances SET driver_name = ? WHERE ambulance_number = ?",
                       (ambulance_drivers[i], f"AMB-{i + 1:02d}"))
        db.commit()
