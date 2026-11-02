"""Course registration: which faculty teaches which subject, and which section each student joins.

  Department Manager (HOD) ──assigns──▶ Offering (subject + faculty + section + seats)
  Student ──registers──▶ Enrollment (one offering per subject)

Rules are deterministic:
  * A faculty member can only teach subjects in their own department that are in their expertise,
    and only up to MAX_CREDITS of teaching load.
  * Core subjects: every student is enrolled automatically, balanced across sections.
  * Electives: each student picks the department's required number and chooses the faculty (section) they want.
  * Choosing is a REQUEST: Student → request (subject + faculty section) → that faculty accepts or rejects.
    Acceptance re-checks seats and the elective limit, takes the seat atomically, then enrolls (or switches section).
    Students can also request to move a core subject to another faculty's section.
  * Students can change, cancel or drop only while registration is open.
"""
import math

from flask import Blueprint, flash, g, redirect, request, url_for
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from auth import roles_required
from db import count, db, find, find_one, insert, update

bp = Blueprint("academics", __name__)

MAX_CREDITS = 18
ELECTIVES_REQUIRED = {"CSE": 2, "ECE": 2, "MECH": 1}
SECTION_NAMES = "ABCD"


# ---------------------------------------------------------------- queries
def registration_open(dept):
    s = find_one("settings", {"key": f"registration_open:{dept}"})
    return bool(s and s["value"])


def faculty_load(faculty_id):
    subs = {s["id"]: s for s in find("subjects")}
    return sum(subs[o["subject_id"]]["credits"] for o in find("offerings", {"faculty_id": faculty_id})
               if o["subject_id"] in subs)


def enrolled_subject_ids(student_id):
    return [e["subject_id"] for e in find("enrollments", {"student_id": student_id})]


def offerings_view(flt=None):
    """Offerings with subject, faculty name and seats left."""
    subs = {s["id"]: s for s in find("subjects")}
    fac = {u["id"]: u for u in find("users", {"role": "faculty"})}
    out = []
    for o in find("offerings", flt or {}, sort=[("subject_id", 1), ("section", 1)]):
        s = subs[o["subject_id"]]
        out.append(dict(o, code=s["code"], subject=s["name"], kind=s["kind"], credits=s["credits"],
                        faculty=fac.get(o["faculty_id"], {}).get("name", "?"),
                        seats_left=o["capacity"] - o["enrolled"]))
    return out


def student_courses(student):
    """What the student is taking (with faculty) and the electives they can still choose from."""
    mine = {e["subject_id"]: e for e in find("enrollments", {"student_id": student["id"]})}
    offers = offerings_view({"subject_id": {"$in": [s["id"] for s in find("subjects", {"dept": student["dept"]})]}})
    by_offer = {o["id"]: o for o in offers}
    taking = [dict(by_offer[e["offering_id"]], enrollment_id=e["id"]) for e in mine.values() if e["offering_id"] in by_offer]
    electives = {}
    for o in offers:
        if o["kind"] == "elective":
            electives.setdefault(o["subject_id"], {"code": o["code"], "subject": o["subject"], "credits": o["credits"],
                                                   "subject_id": o["subject_id"], "sections": [],
                                                   "chosen": mine.get(o["subject_id"], {}).get("offering_id")})
            electives[o["subject_id"]]["sections"].append(o)
    chosen = sum(1 for e in electives.values() if e["chosen"])
    # Every subject with its faculty sections, plus this student's requests, for the "choose your faculty" page
    reqs = find("enrollment_requests", {"student_id": student["id"]}, sort=[("id", -1)])
    pending = {r["subject_id"]: r for r in reqs if r["status"] == "Pending"}
    last = {}
    for r in reqs:
        last.setdefault(r["subject_id"], r)
    catalog = {}
    for o in offers:
        c = catalog.setdefault(o["subject_id"], {"code": o["code"], "subject": o["subject"], "kind": o["kind"],
                                                 "credits": o["credits"], "subject_id": o["subject_id"], "sections": [],
                                                 "chosen": mine.get(o["subject_id"], {}).get("offering_id"),
                                                 "pending": pending.get(o["subject_id"]),
                                                 "last": last.get(o["subject_id"])})
        c["sections"].append(o)
    new_pending = sum(1 for r in pending.values() if r["subject_id"] not in mine and r["kind"] == "elective")
    return {"taking": sorted(taking, key=lambda o: (o["kind"] != "core", o["code"])),
            "electives": sorted(electives.values(), key=lambda e: e["code"]),
            "catalog": sorted(catalog.values(), key=lambda c: (c["kind"] != "core", c["code"])),
            "requests": reqs[:12], "pending_count": len(pending), "new_pending": new_pending,
            "required": ELECTIVES_REQUIRED.get(student["dept"], 0), "chosen": chosen,
            "open": registration_open(student["dept"]),
            "credits": sum(o["credits"] for o in taking)}


# ---------------------------------------------------------------- student → faculty requests
def request_section(student, offering_id, note=""):
    """Student asks a specific faculty member to take them into their section of a subject.
    Nothing changes until that faculty member accepts."""
    o = find_one("offerings", {"id": offering_id})
    if not o:
        return False, "Section not found."
    subject = find_one("subjects", {"id": o["subject_id"]})
    if subject["dept"] != student["dept"]:
        return False, "You can only request your own department's subjects."
    if not registration_open(student["dept"]):
        return False, "Course registration is closed."
    current = find_one("enrollments", {"student_id": student["id"], "subject_id": subject["id"]})
    if current and current["offering_id"] == offering_id:
        return False, "You're already in this faculty's section."
    if subject["kind"] == "core" and not current:
        return False, "Core subjects are assigned automatically; you can request a different faculty once placed."
    if o["capacity"] - o["enrolled"] <= 0:
        return False, f"{subject['code']} section {o['section']} is full."
    if not current:  # a new elective: respect the limit, counting requests still waiting
        taken = count("enrollments", {"student_id": student["id"], "kind": "elective"})
        waiting = count("enrollment_requests", {"student_id": student["id"], "status": "Pending", "kind": "elective",
                                                "is_switch": False, "subject_id": {"$ne": subject["id"]}})
        if taken + waiting >= ELECTIVES_REQUIRED.get(student["dept"], 0):
            return False, f"You can take only {ELECTIVES_REQUIRED[student['dept']]} elective(s) (counting pending requests)."
    # One open request per subject: a new choice replaces the old one
    db.enrollment_requests.update_many({"student_id": student["id"], "subject_id": subject["id"], "status": "Pending"},
                                       {"$set": {"status": "Cancelled", "decided_at": _now()}})
    faculty = find_one("users", {"id": o["faculty_id"]})
    rid = insert("enrollment_requests", {
        "student_id": student["id"], "student_name": student["name"], "roll": student["roll"],
        "subject_id": subject["id"], "code": subject["code"], "subject": subject["name"], "kind": subject["kind"],
        "offering_id": offering_id, "section": o["section"], "faculty_id": o["faculty_id"],
        "faculty_name": faculty["name"], "is_switch": bool(current),
        "from_offering_id": current["offering_id"] if current else None, "note": note.strip()[:300],
        "status": "Pending", "reason": None, "created": _now(), "decided_at": None})
    _notify([o["faculty_id"]], f"Enrollment request: {student['name']} ({student['roll']}) wants your "
                               f"{subject['code']} section {o['section']}" + (" (switching from another section)" if current else "") + ".")
    return True, f"Request sent to {faculty['name']} for {subject['code']} section {o['section']}. You'll be notified."


def decide_request(faculty, request_id, accept, reason=""):
    """Only the faculty member who teaches the requested section can accept or reject it.
    Acceptance re-checks the rules (seat, elective limit) before enrolling."""
    r = find_one("enrollment_requests", {"id": request_id})
    if not r:
        return False, "Request not found."
    if r["faculty_id"] != faculty["id"]:
        return False, "Only the faculty member who teaches that section can decide."
    if r["status"] != "Pending":
        return False, f"This request is already {r['status'].lower()}."
    if not db.enrollment_requests.update_one({"id": request_id, "status": "Pending"},
                                             {"$set": {"status": "Deciding"}}).modified_count:
        return False, "This request was already decided."

    def close(status, why):
        update("enrollment_requests", {"id": request_id}, {"status": status, "reason": why, "decided_at": _now()})
        word = "accepted ✅" if status == "Accepted" else "declined"
        _notify([r["student_id"]], f"{faculty['name']} {word} your request for {r['code']} section {r['section']}"
                                   + (f": {why}" if why else "."))
        return status == "Accepted", f"{status}: {r['roll']} for {r['code']} section {r['section']}" + (f" ({why})" if why else "")

    if not accept:
        return close("Rejected", reason.strip() or "Not accepted by faculty")
    student = find_one("users", {"id": r["student_id"]})
    current = find_one("enrollments", {"student_id": student["id"], "subject_id": r["subject_id"]})
    if not current and r["kind"] == "elective" and \
            count("enrollments", {"student_id": student["id"], "kind": "elective"}) >= ELECTIVES_REQUIRED.get(student["dept"], 0):
        return close("Rejected", "Student already has the maximum number of electives")
    if not take_seat(r["offering_id"]):
        return close("Rejected", "Section is full")
    if current:
        db.offerings.update_one({"id": current["offering_id"]}, {"$inc": {"enrolled": -1}})
        update("enrollments", {"id": current["id"]}, {"offering_id": r["offering_id"]})
    else:
        try:
            insert("enrollments", {"student_id": student["id"], "subject_id": r["subject_id"],
                                   "offering_id": r["offering_id"], "kind": r["kind"]})
        except DuplicateKeyError:
            db.offerings.update_one({"id": r["offering_id"]}, {"$inc": {"enrolled": -1}})
            return close("Rejected", "Student is already enrolled in this subject")
    return close("Accepted", reason.strip())


def cancel_request(student, request_id):
    res = db.enrollment_requests.update_one({"id": request_id, "student_id": student["id"], "status": "Pending"},
                                            {"$set": {"status": "Cancelled", "decided_at": _now()}})
    return bool(res.modified_count), "Request cancelled." if res.modified_count else "Nothing to cancel."


def faculty_requests(faculty_id):
    pending = find("enrollment_requests", {"faculty_id": faculty_id, "status": "Pending"}, sort=[("id", 1)])
    users = {u["id"]: u for u in find("users", {"id": {"$in": [r["student_id"] for r in pending]}})}
    seats = {o["id"]: o["capacity"] - o["enrolled"] for o in find("offerings", {"faculty_id": faculty_id})}
    for r in pending:
        u = users.get(r["student_id"], {})
        r["cgpa"], r["seats_left"] = u.get("cgpa"), seats.get(r["offering_id"], 0)
        r["from_faculty"] = None
        if r.get("from_offering_id"):
            old = find_one("offerings", {"id": r["from_offering_id"]})
            r["from_faculty"] = (find_one("users", {"id": old["faculty_id"]}) or {}).get("name") if old else None
    history = find("enrollment_requests", {"faculty_id": faculty_id, "status": {"$in": ["Accepted", "Rejected"]}},
                   sort=[("id", -1)], limit=10)
    return {"pending": pending, "history": history}


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _notify(user_ids, message):
    import ops
    ops.notify(message, user_ids=user_ids)


# ---------------------------------------------------------------- teaching assignment (HOD)
def can_teach(faculty, subject):
    if faculty["role"] != "faculty" or faculty.get("dept") != subject["dept"]:
        return False, f"{faculty['name']} is not in the {subject['dept']} department."
    if subject["code"] not in faculty.get("expertise", []):
        return False, f"{faculty['name']} is not qualified for {subject['code']} (not in their expertise)."
    load = faculty_load(faculty["id"])
    if load + subject["credits"] > MAX_CREDITS:
        return False, f"{faculty['name']} already teaches {load} credits; adding {subject['credits']} exceeds {MAX_CREDITS}."
    return True, ""


def add_section(subject_id, faculty_id, capacity):
    subject = find_one("subjects", {"id": subject_id})
    faculty = find_one("users", {"id": faculty_id})
    if not subject or not faculty:
        return None, "Unknown subject or faculty."
    if find_one("offerings", {"subject_id": subject_id, "faculty_id": faculty_id}):
        return None, f"{faculty['name']} already has a section of {subject['code']}."
    ok, why = can_teach(faculty, subject)
    if not ok:
        return None, why
    used = {o["section"] for o in find("offerings", {"subject_id": subject_id})}
    section = next(s for s in SECTION_NAMES if s not in used)
    oid = insert("offerings", {"subject_id": subject_id, "faculty_id": faculty_id, "section": section,
                               "capacity": max(1, capacity), "enrolled": 0})
    if subject["kind"] == "core":
        rebalance_core(subject)
    return oid, f"{faculty['name']} now teaches {subject['code']} section {section}."


def remove_section(offering_id):
    """Students in the removed section move to another section of the same subject if seats allow."""
    o = find_one("offerings", {"id": offering_id})
    if not o:
        return False, "Section not found."
    others = [x for x in find("offerings", {"subject_id": o["subject_id"], "id": {"$ne": offering_id}})]
    free = sum(x["capacity"] - x["enrolled"] for x in others)
    if o["enrolled"] > free:
        return False, f"Can't remove: {o['enrolled']} students enrolled and only {free} free seats in other sections."
    for e in find("enrollments", {"offering_id": offering_id}):
        target = min((x for x in others if x["capacity"] > x["enrolled"]), key=lambda x: x["enrolled"])
        db.enrollments.update_one({"id": e["id"]}, {"$set": {"offering_id": target["id"]}})
        db.offerings.update_one({"id": target["id"]}, {"$inc": {"enrolled": 1}})
        target["enrolled"] += 1
    db.offerings.delete_one({"id": offering_id})
    return True, f"Section removed; {o['enrolled']} student(s) moved."


def auto_assign_teaching(dept):
    """Gives every unstaffed subject a teacher: qualified faculty with the lowest load first.
    Core subjects get one section for the whole class; electives get up to two sections so students can choose."""
    students = count("users", {"role": "student", "dept": dept})
    faculty = find("users", {"role": "faculty", "dept": dept})
    made = []
    for s in sorted(find("subjects", {"dept": dept}), key=lambda s: (s["kind"] != "core", s["code"])):
        have = count("offerings", {"subject_id": s["id"]})
        want = 1 if s["kind"] == "core" else 2
        cap = students if s["kind"] == "core" else math.ceil(students * 0.75)
        for f in sorted(faculty, key=lambda f: (faculty_load(f["id"]), f["id"])):
            if have >= want:
                break
            if find_one("offerings", {"subject_id": s["id"], "faculty_id": f["id"]}) or not can_teach(f, s)[0]:
                continue
            add_section(s["id"], f["id"], cap)
            made.append(f"{s['code']} → {f['name']}")
            have += 1
    return made


# ---------------------------------------------------------------- student registration
def take_seat(offering_id):
    """Atomically takes one seat if any are free (compare-and-set on the current count)."""
    for _ in range(5):
        o = find_one("offerings", {"id": offering_id})
        if not o or o["enrolled"] >= o["capacity"]:
            return None
        got = db.offerings.find_one_and_update({"id": offering_id, "enrolled": o["enrolled"], "capacity": o["capacity"]},
                                               {"$inc": {"enrolled": 1}}, return_document=ReturnDocument.AFTER)
        if got:
            return got
    return None


def enroll(student, offering_id):
    o = find_one("offerings", {"id": offering_id})
    if not o:
        return False, "Section not found."
    subject = find_one("subjects", {"id": o["subject_id"]})
    if subject["dept"] != student["dept"]:
        return False, "You can only register for your department's subjects."
    if subject["kind"] == "core":
        return False, "Core subjects are assigned automatically."
    if not registration_open(student["dept"]):
        return False, "Course registration is closed."
    current = find_one("enrollments", {"student_id": student["id"], "subject_id": subject["id"]})
    if current and current["offering_id"] == offering_id:
        return True, "You're already in this section."
    if not current:
        chosen = count("enrollments", {"student_id": student["id"], "kind": "elective"})
        if chosen >= ELECTIVES_REQUIRED.get(student["dept"], 0):
            return False, f"You can choose only {ELECTIVES_REQUIRED[student['dept']]} elective(s). Drop one first."
    if not take_seat(offering_id):
        return False, f"Section {o['section']} of {subject['code']} is full. Pick another faculty's section."
    if current:  # switching faculty: free the old seat
        db.offerings.update_one({"id": current["offering_id"]}, {"$inc": {"enrolled": -1}})
        update("enrollments", {"id": current["id"]}, {"offering_id": offering_id})
        return True, f"Switched {subject['code']} to section {o['section']}."
    try:
        insert("enrollments", {"student_id": student["id"], "subject_id": subject["id"], "offering_id": offering_id,
                               "kind": "elective"})
    except DuplicateKeyError:  # double click: give the seat back
        db.offerings.update_one({"id": offering_id}, {"$inc": {"enrolled": -1}})
        return True, "Already registered."
    return True, f"Registered for {subject['code']} {subject['name']} (section {o['section']})."


def drop(student, subject_id):
    if not registration_open(student["dept"]):
        return False, "Course registration is closed."
    e = find_one("enrollments", {"student_id": student["id"], "subject_id": subject_id, "kind": "elective"})
    if not e:
        return False, "You're not registered for that elective."
    if db.enrollments.delete_one({"id": e["id"]}).deleted_count:
        db.offerings.update_one({"id": e["offering_id"]}, {"$inc": {"enrolled": -1}})
    return True, "Elective dropped."


def enroll_core(student):
    """Places the student in every core subject of their department, in the least-full section."""
    for s in find("subjects", {"dept": student["dept"], "kind": "core"}):
        if find_one("enrollments", {"student_id": student["id"], "subject_id": s["id"]}):
            continue
        for o in sorted(find("offerings", {"subject_id": s["id"]}), key=lambda o: o["enrolled"] / o["capacity"]):
            if take_seat(o["id"]):
                insert("enrollments", {"student_id": student["id"], "subject_id": s["id"], "offering_id": o["id"],
                                       "kind": "core"})
                break


def rebalance_core(subject):
    for st in find("users", {"role": "student", "dept": subject["dept"]}):
        enroll_core(st)


# ---------------------------------------------------------------- routes
@bp.post("/courses/request/<int:offering_id>")
@roles_required("student")
def request_route(offering_id):
    ok, msg = request_section(g.user, offering_id, request.form.get("note", ""))
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("student_dashboard") + "#choose")


@bp.post("/courses/request/<int:request_id>/cancel")
@roles_required("student")
def cancel_route(request_id):
    ok, msg = cancel_request(g.user, request_id)
    flash(msg, "info" if ok else "warning")
    return redirect(url_for("student_dashboard") + "#choose")


@bp.post("/faculty/requests/<int:request_id>")
@roles_required("faculty")
def decide_route(request_id):
    ok, msg = decide_request(g.user, request_id, request.form.get("decision") == "accept", request.form.get("reason", ""))
    flash(msg, "success" if ok else "warning")
    return redirect(url_for("faculty_dashboard") + "#requests")


@bp.post("/faculty/requests/accept-all")
@roles_required("faculty")
def accept_all_route():
    done = [decide_request(g.user, r["id"], True)[0] for r in
            find("enrollment_requests", {"faculty_id": g.user["id"], "status": "Pending"}, sort=[("id", 1)])]
    flash(f"Accepted {sum(done)} of {len(done)} request(s) (first come, first served while seats last).", "success")
    return redirect(url_for("faculty_dashboard") + "#requests")


@bp.post("/courses/drop/<int:subject_id>")
@roles_required("student")
def drop_route(subject_id):
    ok, msg = drop(g.user, subject_id)
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("student_dashboard") + "#courses")


@bp.post("/teaching/add")
@roles_required("dept_manager")
def add_section_route():
    subject = find_one("subjects", {"id": int(request.form["subject_id"])})
    if not subject or subject["dept"] != g.user["dept"]:
        flash("You can only staff your own department's subjects.", "danger")
    else:
        oid, msg = add_section(subject["id"], int(request.form["faculty_id"]), int(request.form.get("capacity") or 30))
        flash(msg, "success" if oid else "danger")
    return redirect(url_for("ops.manager") + "#teaching")


@bp.post("/teaching/remove/<int:offering_id>")
@roles_required("dept_manager")
def remove_section_route(offering_id):
    o = find_one("offerings", {"id": offering_id})
    if not o or find_one("subjects", {"id": o["subject_id"]})["dept"] != g.user["dept"]:
        flash("You can only change your own department's sections.", "danger")
    else:
        ok, msg = remove_section(offering_id)
        flash(msg, "success" if ok else "danger")
    return redirect(url_for("ops.manager") + "#teaching")


@bp.post("/teaching/auto")
@roles_required("dept_manager")
def auto_route():
    made = auto_assign_teaching(g.user["dept"])
    flash("Auto-assigned: " + ", ".join(made) if made else "Every subject already has its sections.", "success")
    return redirect(url_for("ops.manager") + "#teaching")


@bp.post("/teaching/registration")
@roles_required("dept_manager")
def registration_route():
    is_open = request.form.get("open") == "1"
    db.settings.update_one({"key": f"registration_open:{g.user['dept']}"}, {"$set": {"value": is_open}}, upsert=True)
    flash(f"Course registration {'opened' if is_open else 'closed'} for {g.user['dept']}.", "info")
    return redirect(url_for("ops.manager") + "#teaching")


def teaching_board(dept):
    """Data for the HOD's teaching-assignment screen."""
    subjects = find("subjects", {"dept": dept}, sort=[("kind", 1), ("code", 1)])
    offers = offerings_view({"subject_id": {"$in": [s["id"] for s in subjects]}})
    faculty = find("users", {"role": "faculty", "dept": dept}, sort=[("username", 1)])
    for f in faculty:
        f["load"] = faculty_load(f["id"])
    students = find("users", {"role": "student", "dept": dept})
    pending = [s for s in students if count("enrollments", {"student_id": s["id"], "kind": "elective"})
               < ELECTIVES_REQUIRED.get(dept, 0)]
    for s in subjects:
        s["sections"] = [o for o in offers if o["subject_id"] == s["id"]]
        s["qualified"] = [f for f in faculty if s["code"] in f.get("expertise", [])]
    return {"subjects": subjects, "faculty": faculty, "max_credits": MAX_CREDITS,
            "reg_open": registration_open(dept), "pending_students": pending,
            "electives_required": ELECTIVES_REQUIRED.get(dept, 0)}
