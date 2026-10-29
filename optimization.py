from datetime import datetime


def calculate_metrics(db):
    total_patients = db.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    waiting = db.execute("SELECT COUNT(*) FROM patients WHERE status = 'Waiting'").fetchone()[0]
    available_beds = db.execute("SELECT COUNT(*) FROM beds WHERE status = 'Available'").fetchone()[0]
    total_beds = db.execute("SELECT COUNT(*) FROM beds").fetchone()[0]
    available_staff = db.execute("SELECT COUNT(*) FROM staff WHERE status = 'Available'").fetchone()[0]
    total_staff = db.execute("SELECT COUNT(*) FROM staff").fetchone()[0]
    ambulance_count = db.execute("SELECT COUNT(*) FROM ambulances WHERE status = 'Available'").fetchone()[0]
    waiting_rows = db.execute("SELECT arrival_time FROM patients WHERE status = 'Waiting'").fetchall()
    wait_minutes = []
    for row in waiting_rows:
        try:
            wait_minutes.append(max(0, int((datetime.now() - datetime.fromisoformat(row[0])).total_seconds() / 60)))
        except ValueError:
            pass
    return {
        "total_patients": total_patients,
        "waiting_patients": waiting,
        "available_beds": available_beds,
        "total_beds": total_beds,
        "bed_occupancy": round((1 - available_beds / total_beds) * 100) if total_beds else 0,
        "available_staff": available_staff,
        "total_staff": total_staff,
        "staff_utilization": round((1 - available_staff / total_staff) * 100) if total_staff else 0,
        "available_ambulances": ambulance_count,
        "average_wait": round(sum(wait_minutes) / len(wait_minutes)) if wait_minutes else 0,
    }


def build_recommendations(db, metrics):
    recommendations = []
    if metrics["waiting_patients"] > metrics["available_staff"] * 2:
        recommendations.append(("High priority", "Assign additional staff to reduce the current patient queue."))
    if metrics["bed_occupancy"] >= 80:
        recommendations.append(("Capacity alert", "Bed occupancy is high. Review discharge-ready patients and open reserve capacity."))
    if metrics["available_ambulances"] == 0:
        recommendations.append(("Emergency alert", "No ambulance is currently available for a new emergency request."))
    if not recommendations:
        recommendations.append(("Balanced flow", "Current resources can handle the recorded patient demand."))
    department = db.execute("SELECT department, COUNT(*) AS total FROM patients WHERE status = 'Waiting' GROUP BY department ORDER BY total DESC LIMIT 1").fetchone()
    if department:
        recommendations.append(("Queue focus", f"{department['department']} has the longest waiting queue ({department['total']} patients)."))
    return recommendations


def build_allocation_plan(db):
    patients = db.execute(
        "SELECT * FROM patients WHERE status = 'Waiting' "
        "ORDER BY CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, arrival_time"
    ).fetchall()
    beds = db.execute("SELECT * FROM beds WHERE status = 'Available' ORDER BY ward, bed_number").fetchall()
    staff = db.execute("SELECT * FROM staff WHERE status = 'Available' ORDER BY department, role").fetchall()
    ambulances = db.execute("SELECT * FROM ambulances WHERE status = 'Available' ORDER BY priority DESC").fetchall()

    bed_by_ward = {}
    for bed in beds:
        bed_by_ward.setdefault(bed["ward"], []).append(bed)
    staff_by_department = {}
    for member in staff:
        staff_by_department.setdefault(member["department"], []).append(member)
    remaining_staff = list(staff)
    all_staff = list(staff)

    patient_plan = []
    for patient_index, patient in enumerate(patients):
        suggested_ward = "Emergency" if patient["department"] == "Emergency" else patient["department"]
        matching_beds = bed_by_ward.get(suggested_ward, []) or bed_by_ward.get("General", [])
        bed = matching_beds[0] if matching_beds else None
        assigned_staff = staff_by_department.get(patient["department"], [])
        staff_scope = "Same department"
        if not assigned_staff:
            assigned_staff = remaining_staff
            staff_scope = "Cross-department support"
        selected_staff = assigned_staff[0] if assigned_staff else None
        if not selected_staff and all_staff:
            selected_staff = all_staff[patient_index % len(all_staff)]
            staff_scope = "Shared capacity review"
        patient_plan.append({
            "patient": patient,
            "bed": bed,
            "staff": selected_staff,
            "staff_scope": staff_scope if selected_staff else "No staff available",
            "reason": "High priority patient" if patient["priority"] == "High" else "Waiting queue order",
        })
        if bed:
            matching_beds.pop(0)
        if selected_staff:
            if selected_staff in remaining_staff:
                remaining_staff.remove(selected_staff)
            for department_staff in staff_by_department.values():
                if selected_staff in department_staff:
                    department_staff.remove(selected_staff)
                    break

    department_load = db.execute(
        "SELECT department, COUNT(*) AS waiting FROM patients WHERE status = 'Waiting' "
        "GROUP BY department ORDER BY waiting DESC"
    ).fetchall()
    staff_plan = []
    available_staff = list(staff)
    for row in department_load:
        department_staff = [member for member in available_staff if member["department"] == row["department"]]
        staff_plan.append({"department": row["department"], "waiting": row["waiting"], "available": len(department_staff)})

    return {"patients": patient_plan, "staff": staff_plan, "ambulances": ambulances}
