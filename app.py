import math
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError
from werkzeug.security import check_password_hash, generate_password_hash

from db import count, db, find, find_one, insert, update  # loads .env too

import ai  # noqa: E402  (needs the .env values loaded first)
import academics  # noqa: E402
import ops  # noqa: E402
import reports  # noqa: E402
from auth import HOME, ROLE_LABELS, ROLES, current_user, role_required, roles_required  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # note uploads
app.register_blueprint(ops.bp)
app.register_blueprint(academics.bp)

TICKET_STEPS = ops.STEPS


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def subjects_by_id():
    return {s["id"]: s for s in find("subjects")}


def users_by_id():
    return {u["id"]: u for u in find("users")}


@app.before_request
def csrf_protect():
    # /api/ endpoints are machine-to-machine webhooks authenticated by a shared secret instead
    if request.method == "POST" and not request.path.startswith("/api/"):
        token = session.get("csrf_token")
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not token or token != sent:
            abort(400)


@app.context_processor
def inject_globals():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return {"csrf_token": session["csrf_token"], "ROLE_LABELS": ROLE_LABELS, "TICKET_STEPS": TICKET_STEPS}


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        role = request.form.get("role")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = find_one("users", {"username": username, "role": role})
        if user and check_password_hash(user["password_hash"], password):
            csrf = session.get("csrf_token")
            session.clear()
            session["user_id"] = user["id"]
            session["csrf_token"] = csrf
            return redirect(url_for(HOME[role]))
        flash("Invalid ID, password, or role.", "danger")
    user = current_user()
    if user:
        return redirect(url_for(HOME[user["role"]]))
    return render_template("login.html", roles=ROLES)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403, msg="You don't have access to this page."), 403


# ---------- shared student data ----------
def classes_needed(present, total, target=0.75):
    """Consecutive classes a student must attend to reach the target %."""
    if total == 0 or present / total >= target:
        return 0
    return math.ceil((target * total - present) / (1 - target))


def student_summary(student_id, dept=None):
    """Attendance, marks and fees for the subjects this student is actually enrolled in."""
    subjects = sorted(find("subjects", {"id": {"$in": academics.enrolled_subject_ids(student_id)}}),
                      key=lambda s: s["code"])
    tally = {s["id"]: [0, 0] for s in subjects}
    for a in find("attendance", {"student_id": student_id}):
        if a["subject_id"] in tally:
            tally[a["subject_id"]][0] += a["present"]
            tally[a["subject_id"]][1] += 1
    att = []
    for s in subjects:
        present, total = tally[s["id"]]
        att.append({"code": s["code"], "name": s["name"], "present": present, "total": total,
                    "pct": round(100 * present / total, 1) if total else 0, "need": classes_needed(present, total)})
    tp, tt = sum(a["present"] for a in att), sum(a["total"] for a in att)
    by_id = {s["id"]: s for s in subjects}
    marks = sorted(({"code": by_id[m["subject_id"]]["code"], "name": by_id[m["subject_id"]]["name"],
                     "exam": m["exam"], "score": m["score"], "max_score": m["max_score"]}
                    for m in find("marks", {"student_id": student_id}) if m["subject_id"] in by_id),
                   key=lambda m: (m["code"], m["exam"]))
    fees = find("fees", {"student_id": student_id}, sort=[("due_date", 1)])
    return {"att": att, "overall": round(100 * tp / tt, 1) if tt else 0, "marks": marks, "fees": fees,
            "fee_pending": sum(f["amount"] for f in fees if not f["paid"])}


def pending_assignments(student_id):
    subs = {s["id"]: s for s in find("subjects", {"id": {"$in": academics.enrolled_subject_ids(student_id)}})}
    return [dict(a, code=subs[a["subject_id"]]["code"])
            for a in find("assignments", {"due": {"$gte": date.today().isoformat()}, "subject_id": {"$in": list(subs)}},
                          sort=[("due", 1)])]


def student_notes(student_id):
    subs = {s["id"]: s for s in find("subjects", {"id": {"$in": academics.enrolled_subject_ids(student_id)}})}
    return [dict(n, code=subs[n["subject_id"]]["code"])
            for n in find("notes", {"subject_id": {"$in": list(subs)}}, sort=[("id", DESCENDING)])]


def notices_for(role):
    return find("notices", {"target": {"$in": ["all", role]}}, sort=[("id", DESCENDING)], limit=8)


def student_context(user):
    """Plain-text snapshot of the student's academics for the AI assistant."""
    s = student_summary(user["id"], user["dept"])
    lines = [f"Name: {user['name']} | Roll: {user['roll']} | {user['dept']} Year {user['year']} | "
             f"CGPA: {user['cgpa']} | XP: {user['xp']} | Today: {date.today().isoformat()}",
             "Courses (subject, type, faculty, section): " + "; ".join(
                 f"{c['code']} {c['subject']} ({c['kind']}, {c['faculty']}, sec {c['section']})"
                 for c in academics.student_courses(user)["taking"]),
             f"Overall attendance: {s['overall']}%"]
    lines += [f"- {a['code']} {a['name']}: {a['present']}/{a['total']} ({a['pct']}%)"
              + (f", needs next {a['need']} classes for 75%" if a["need"] else "") for a in s["att"]]
    lines.append("Marks:")
    lines += [f"- {m['code']} {m['exam']}: {m['score']:g}/{m['max_score']:g}" for m in s["marks"]]
    lines.append("Fees:")
    lines += [f"- {f['term']}: Rs.{f['amount']} {'paid' if f['paid'] else 'UNPAID, due ' + f['due_date']}"
              for f in s["fees"]]
    lines.append("Pending assignments:")
    lines += [f"- {a['code']} '{a['title']}' due {a['due']}" for a in pending_assignments(user["id"])]
    lines.append("Shared notes (use read_note with the id for details):")
    lines += [f"- id {n['id']}: {n['code']} {n['title']}" for n in student_notes(user["id"])]
    lines.append("Their maintenance reports:")
    lines += [f"- {t['ref']} {t['title']} ({t['category']}, {t.get('room_code') or 'no location'}) - {t['status']}, "
              f"{t['priority']}" for t in find("tickets", {"reporters": user["id"], "status": {"$ne": "Duplicate"}},
                                               sort=[("id", DESCENDING)], limit=10)]
    return "\n".join(lines)


# ---------- student ----------
@app.route("/student")
@role_required("student")
def student_dashboard():
    sid = g.user["id"]
    data = student_summary(sid, g.user["dept"])
    rank = count("users", {"role": "student", "dept": g.user["dept"], "xp": {"$gt": g.user["xp"]}}) + 1
    leaderboard = find("users", {"role": "student", "dept": g.user["dept"]}, sort=[("xp", DESCENDING)], limit=5)
    done = {q["note_id"] for q in find("quizzes", {"student_id": sid, "status": "Submitted"})}
    notes = [dict(n, has_content=bool(n.get("content")), quiz_done=n["id"] in done) for n in student_notes(g.user["id"])]
    leaves = find("leaves", {"student_id": sid}, sort=[("id", DESCENDING)])
    applied = {a["job_id"]: a["status"] for a in find("applications", {"student_id": sid})}
    jobs = [dict(j, applied=applied.get(j["id"])) for j in find("jobs", sort=[("id", DESCENDING)])]
    semesters = find("sgpa", {"student_id": sid}, sort=[("sem", 1)])
    chat = find("chat_messages", {"student_id": sid}, sort=[("id", DESCENDING)], limit=30)
    return render_template("student.html", u=g.user, rank=rank, leaderboard=leaderboard,
                           assignments=pending_assignments(g.user["id"]), notes=notes, leaves=leaves, jobs=jobs,
                           semesters=semesters, chat=list(reversed(chat)), rooms=find("rooms", sort=[("code", 1)]),
                           categories=ops.CATEGORIES, courses=academics.student_courses(g.user),
                           notices=notices_for("student"), **data)


@app.post("/student/ai")
@role_required("student")
def student_ai():
    message = (request.get_json(silent=True) or {}).get("message", "").strip()[:2000]
    if not message:
        return jsonify(error="Type a message first."), 400
    sid = g.user["id"]
    rows = find("chat_messages", {"student_id": sid}, sort=[("id", DESCENDING)], limit=12)
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    while history and history[0]["role"] != "user":  # the conversation must start with the student's turn
        history.pop(0)
    history.append({"role": "user", "content": message})

    def handle_tool(name, args):
        if name == "raise_ticket":
            t, outcome = ops.intake(f"{args['description']} (Location: {args['location']})", g.user, "AI chat")
            info = {"ticket": t["ref"], "status": t["status"], "priority": t["priority"],
                    "outcome": {"created": "new work order created and assigned",
                                "duplicate": f"merged into an existing report ({t['report_count']} reports so far)",
                                "needs_info": "location not recognised"}.get(outcome, outcome)}
            if outcome == "needs_info":
                info["ask_student"] = t.get("clarification")
            return info
        if name == "read_note":
            note = find_one("notes", {"id": int(args["note_id"])})
            if not note:
                raise ValueError("No note with that id.")
            return {"title": note["title"], "content": note.get("content") or "(This note has no text content.)"}
        raise ValueError(f"Unknown tool {name}")

    try:
        reply, tickets = ai.chat(history, student_context(g.user), handle_tool)
    except ai.AIUnavailable as e:
        return jsonify(error=str(e)), 503
    insert("chat_messages", {"student_id": sid, "role": "user", "content": message, "at": now()})
    insert("chat_messages", {"student_id": sid, "role": "assistant", "content": reply, "at": now()})
    return jsonify(reply=reply, tickets=tickets)


@app.post("/student/ai/clear")
@role_required("student")
def student_ai_clear():
    db.chat_messages.delete_many({"student_id": g.user["id"]})
    return jsonify(ok=True)


@app.post("/student/summary/<kind>/<int:item_id>")
@role_required("student")
def student_summary_ai(kind, item_id):
    coll = {"note": "notes", "assignment": "assignments"}.get(kind) or abort(404)
    item = find_one(coll, {"id": item_id}) or abort(404)
    if item.get("summary"):
        return jsonify(summary=item["summary"])
    if not item.get("content"):
        return jsonify(error="Faculty hasn't uploaded any text for this yet."), 400
    try:
        summary = ai.summarize(item["title"], item["content"])
    except ai.AIUnavailable as e:
        return jsonify(error=str(e)), 503
    update(coll, {"id": item_id}, {"summary": summary})  # cached for every student
    return jsonify(summary=summary)


@app.route("/student/quiz/<int:note_id>", methods=["GET", "POST"])
@role_required("student")
def student_quiz(note_id):
    sid = g.user["id"]
    note = find_one("notes", {"id": note_id}) or abort(404)
    note["code"] = find_one("subjects", {"id": note["subject_id"]})["code"]
    quiz = db.quizzes.find_one({"note_id": note_id, "student_id": sid}, {"_id": 0}, sort=[("id", DESCENDING)])
    if quiz and quiz["status"] == "Submitted":
        flash("You've already taken this quiz. Your score was sent to your faculty.", "info")
        return redirect(url_for("student_dashboard") + "#notes")

    if request.method == "POST":
        if not quiz:
            abort(400)
        questions = quiz["questions"]
        score = sum(1 for i, q in enumerate(questions) if request.form.get(f"q{i}") == str(q["answer_index"]))
        update("quizzes", {"id": quiz["id"]}, {"score": score, "status": "Submitted", "taken_at": now()})
        db.users.update_one({"id": sid}, {"$inc": {"xp": 20}})
        flash("Quiz submitted! Your score has been sent to your faculty. +20 XP", "success")
        return redirect(url_for("student_dashboard") + "#notes")

    if not quiz:
        if not note.get("content"):
            flash("This note has no text yet, so a quiz can't be made.", "warning")
            return redirect(url_for("student_dashboard") + "#notes")
        try:
            questions = ai.make_quiz(note["title"], note["content"])
        except ai.AIUnavailable as e:
            flash(str(e), "danger")
            return redirect(url_for("student_dashboard") + "#notes")
        insert("quizzes", {"note_id": note_id, "student_id": sid, "questions": questions, "score": None,
                           "total": len(questions), "status": "Pending", "created": now(), "taken_at": None})
    else:
        questions = quiz["questions"]
    # Answers stay on the server; the page only gets questions and options.
    return render_template("quiz.html", u=g.user, note=note,
                           questions=[{"question": q["question"], "options": q["options"]} for q in questions])


@app.post("/student/profile")
@role_required("student")
def student_profile():
    phone = re.sub(r"\D", "", request.form.get("parent_phone", ""))
    if not 10 <= len(phone) <= 15:
        flash("Enter the parent's WhatsApp number with country code, e.g. 919876543210.", "danger")
    else:
        update("users", {"id": g.user["id"]}, {"parent_phone": phone})
        flash("Parent's WhatsApp number saved. Weekly reports will go there.", "success")
    return redirect(url_for("student_dashboard") + "#profile")


@app.post("/student/leave")
@role_required("student")
def student_leave():
    f = request.form
    insert("leaves", {"student_id": g.user["id"], "from_date": f["from_date"], "to_date": f["to_date"],
                      "reason": f["reason"], "status": "Pending"})
    flash("Leave request submitted.", "success")
    return redirect(url_for("student_dashboard") + "#leave")


@app.post("/student/pay/<int:fee_id>")
@role_required("student")
def student_pay(fee_id):
    update("fees", {"id": fee_id, "student_id": g.user["id"]}, {"paid": 1})
    flash("Payment recorded (demo mode — no real money moved).", "success")
    return redirect(url_for("student_dashboard") + "#fees")


@app.post("/student/apply/<int:job_id>")
@role_required("student")
def student_apply(job_id):
    job = find_one("jobs", {"id": job_id})
    if job and g.user["cgpa"] >= job["min_cgpa"]:
        try:
            insert("applications", {"job_id": job_id, "student_id": g.user["id"], "status": "Applied"})
        except DuplicateKeyError:
            pass
        flash(f"Applied to {job['company']}.", "success")
    else:
        flash("You are not eligible for this drive.", "warning")
    return redirect(url_for("student_dashboard") + "#career")


@app.post("/student/grievance")
@role_required("student")
def student_grievance():
    token = "GRV-" + secrets.token_hex(3).upper()
    insert("grievances", {"token": token, "category": request.form["category"],
                          "message": request.form["message"], "status": "Open"})
    flash(f"Grievance submitted anonymously. Your tracking token: {token}", "info")
    return redirect(url_for("student_dashboard") + "#grievance")


@app.post("/student/track")
@role_required("student")
def student_track():
    gr = find_one("grievances", {"token": request.form["token"].strip().upper()})
    flash(f"{gr['token']}: {gr['status']}" if gr else "No grievance found for that token.", "info")
    return redirect(url_for("student_dashboard") + "#grievance")


# ---------- faculty ----------
@app.route("/faculty")
@role_required("faculty")
def faculty_dashboard():
    my_sections = academics.offerings_view({"faculty_id": g.user["id"]})
    subjects = find("subjects", {"id": {"$in": [o["subject_id"] for o in my_sections]}}, sort=[("code", 1)])
    my_sub = {s["id"]: s for s in subjects}
    my_students = {e["student_id"] for e in find("enrollments", {"offering_id": {"$in": [o["id"] for o in my_sections]}})}
    students = find("users", {"id": {"$in": list(my_students)}}, sort=[("roll", 1)])
    weak = []
    for st in students:
        s = student_summary(st["id"], g.user["dept"])
        avg = [m["score"] / m["max_score"] * 100 for m in s["marks"]]
        avg = round(sum(avg) / len(avg), 1) if avg else 0
        if s["overall"] < 75 or avg < 50:
            weak.append({"name": st["name"], "roll": st["roll"], "att": s["overall"], "avg": avg})
    users = users_by_id()
    leaves = [dict(l, name=users[l["student_id"]]["name"], roll=users[l["student_id"]]["roll"])
              for l in find("leaves", {"status": "Pending", "student_id": {"$in": [s["id"] for s in students]}},
                            sort=[("id", 1)])]
    my_notes = [dict(n, code=my_sub[n["subject_id"]]["code"])
                for n in find("notes", {"subject_id": {"$in": list(my_sub)}}, sort=[("id", DESCENDING)])]
    notes_by_id = {n["id"]: n for n in my_notes}
    quiz_results = [dict(q, name=users[q["student_id"]]["name"], roll=users[q["student_id"]]["roll"],
                         title=notes_by_id[q["note_id"]]["title"], code=notes_by_id[q["note_id"]]["code"])
                    for q in find("quizzes", {"status": "Submitted", "note_id": {"$in": list(notes_by_id)}},
                                  sort=[("id", DESCENDING)])]
    rooms = find("rooms", sort=[("code", 1)])
    rb = {r["id"]: r for r in rooms}
    my_bookings = find("bookings", {"owner_id": g.user["id"], "status": "Active",
                                    "date": {"$gte": date.today().isoformat(),
                                             "$lte": (date.today() + timedelta(days=7)).isoformat()}},
                       sort=[("date", 1), ("start", 1)])
    for b in my_bookings:
        b["room_code"] = rb[b["room_id"]]["code"]
        b["from_code"] = rb[b["relocated_from"]]["code"] if b.get("relocated_from") else None
    my_reports = ops.enrich(find("tickets", {"reporters": g.user["id"], "status": {"$ne": "Duplicate"}},
                                 sort=[("id", DESCENDING)], limit=8))
    my_requests = find("approvals", {"requested_by": g.user["id"]}, sort=[("id", DESCENDING)], limit=5)
    return render_template("faculty.html", u=g.user, subjects=subjects, students=students, weak=weak, my_sections=my_sections, reqs=academics.faculty_requests(g.user["id"]),
                           leaves=leaves, quiz_results=quiz_results, my_notes=my_notes, rooms=rooms,
                           bookable=[r for r in rooms if r["type"] in ("classroom", "lab", "seminar", "exam_hall")],
                           my_bookings=my_bookings, my_reports=my_reports, my_requests=my_requests,
                           categories=ops.CATEGORIES, today=date.today().isoformat(), notices=notices_for("faculty"))


def faculty_owns(subject_id):
    """A faculty member may only manage subjects they teach a section of."""
    return find_one("offerings", {"subject_id": subject_id, "faculty_id": g.user["id"]})


def uploaded_text():
    """Text from an uploaded .pdf/.txt file, or from the pasted text box."""
    file = request.files.get("file")
    if file and file.filename:
        name = file.filename.lower()
        if name.endswith(".pdf"):
            from pypdf import PdfReader
            return "\n".join(page.extract_text() or "" for page in PdfReader(file.stream).pages).strip()
        if name.endswith(".txt"):
            return file.read().decode("utf-8", errors="ignore").strip()
        flash("Only .pdf or .txt files are supported; used the pasted text instead.", "warning")
    return request.form.get("content", "").strip()


@app.post("/faculty/attendance")
@role_required("faculty")
def faculty_attendance():
    subject_id = int(request.form["subject_id"])
    if not faculty_owns(subject_id):
        abort(403)
    day = request.form["date"]
    present_ids = set(request.form.getlist("present"))
    # only the students in this faculty's own section(s) of the subject
    sections = [o["id"] for o in find("offerings", {"subject_id": subject_id, "faculty_id": g.user["id"]})]
    students = find("users", {"id": {"$in": [e["student_id"] for e in find("enrollments", {"offering_id": {"$in": sections}})]}})
    present_ids &= {str(st["id"]) for st in students}
    db.attendance.insert_many([{"student_id": st["id"], "subject_id": subject_id, "date": day,
                                "present": 1 if str(st["id"]) in present_ids else 0} for st in students])
    db.users.update_many({"id": {"$in": [int(i) for i in present_ids]}}, {"$inc": {"xp": 5}})
    flash(f"Attendance saved: {len(present_ids)} present.", "success")
    return redirect(url_for("faculty_dashboard"))


@app.post("/faculty/marks")
@role_required("faculty")
def faculty_marks():
    f = request.form
    if not faculty_owns(int(f["subject_id"])):
        abort(403)
    db.marks.insert_one({"student_id": int(f["student_id"]), "subject_id": int(f["subject_id"]), "exam": f["exam"],
                         "score": float(f["score"]), "max_score": float(f["max_score"])})
    flash("Marks uploaded.", "success")
    return redirect(url_for("faculty_dashboard"))


@app.post("/faculty/note")
@role_required("faculty")
def faculty_note():
    f = request.form
    if not faculty_owns(int(f["subject_id"])):
        abort(403)
    content = uploaded_text()
    if not content:
        flash("Upload a PDF/TXT file or paste the note text, so the AI can summarize it and make quizzes.", "danger")
        return redirect(url_for("faculty_dashboard") + "#upload")
    insert("notes", {"subject_id": int(f["subject_id"]), "title": f["title"], "content": content, "summary": None})
    flash("Note shared. Students can now get an AI summary and take an AI quiz on it.", "success")
    return redirect(url_for("faculty_dashboard") + "#upload")


@app.post("/faculty/assignment")
@role_required("faculty")
def faculty_assignment():
    f = request.form
    if not faculty_owns(int(f["subject_id"])):
        abort(403)
    insert("assignments", {"subject_id": int(f["subject_id"]), "title": f["title"], "due": f["due"],
                           "content": uploaded_text(), "summary": None})
    flash("Assignment posted.", "success")
    return redirect(url_for("faculty_dashboard") + "#upload")


@app.post("/faculty/leave/<int:leave_id>/<action>")
@role_required("faculty")
def faculty_leave(leave_id, action):
    status = {"approve": "Approved", "reject": "Rejected"}.get(action)
    if status:
        update("leaves", {"id": leave_id}, {"status": status})
    return redirect(url_for("faculty_dashboard"))


# ---------- admin ----------
@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    counts = {r: count("users", {"role": r}) for r in ROLES}
    users = find("users")
    dept_of = {u["id"]: u["dept"] for u in users}
    present = total = 0
    dept_tally = {}
    for a in find("attendance"):
        present += a["present"]
        total += 1
        t = dept_tally.setdefault(dept_of.get(a["student_id"]), [0, 0])
        t[0] += a["present"]
        t[1] += 1
    fees = find("fees")
    stats = {
        "attendance": round(100 * present / total, 1) if total else 0,
        "fees_pending": sum(f["amount"] for f in fees if not f["paid"]),
        "fees_collected": sum(f["amount"] for f in fees if f["paid"]),
        "tickets_open": count("tickets", {"status": {"$in": ops.OPEN_STATES}}),
    }
    dept_att = [{"dept": d, "pct": round(100 * p / t, 1)} for d, (p, t) in sorted(dept_tally.items()) if d]
    by_id = {u["id"]: u for u in users}
    report_log = [dict(r, name=by_id[r["student_id"]]["name"], roll=by_id[r["student_id"]]["roll"])
                  for r in find("report_log", sort=[("id", DESCENDING)], limit=15) if r["student_id"] in by_id]
    last_sent = find_one("settings", {"key": "last_report_run"})
    return render_template("admin.html", u=g.user, counts=counts, stats=stats,
                           users=sorted(users, key=lambda x: (x["role"], x["name"])),
                           grievances=find("grievances", sort=[("id", DESCENDING)]), dept_att=dept_att, roles=ROLES,
                           queue=ops.queue()[:8], ops_stats=ops.analytics(), report_log=report_log,
                           latest_id=(db.tickets.find_one({}, {"id": 1}, sort=[("id", DESCENDING)]) or {"id": 0})["id"],
                           last_sent=last_sent["value"] if last_sent else "never",
                           whatsapp_ready=reports.whatsapp_configured(), notices=notices_for("admin"))


@app.get("/admin/tickets.json")
@role_required("admin")
def admin_tickets_json():
    latest = db.tickets.find_one({}, {"id": 1}, sort=[("id", DESCENDING)])
    return jsonify(open=count("tickets", {"status": {"$in": ops.OPEN_STATES}}), latest=latest["id"] if latest else 0)


@app.post("/admin/reports/send")
@role_required("admin")
def admin_send_reports():
    sent, total = send_weekly_reports()
    flash(f"Weekly reports: {sent}/{total} delivered on WhatsApp. PDFs are saved in the reports folder.",
          "success" if sent == total else "warning")
    return redirect(url_for("admin_dashboard") + "#reports")


@app.get("/admin/report/<roll>")
@role_required("admin")
def admin_report_pdf(roll):
    student = find_one("users", {"roll": roll, "role": "student"}) or abort(404)
    return send_file(build_report(student), mimetype="application/pdf")


@app.post("/admin/notice")
@role_required("admin")
def admin_notice():
    f = request.form
    insert("notices", {"title": f["title"], "body": f["body"], "target": f["target"],
                       "created": date.today().isoformat()})
    flash("Notice published.", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/user")
@role_required("admin")
def admin_add_user():
    f = request.form
    if f["role"] not in ROLES:
        abort(400)
    is_student = f["role"] == "student"
    extra = {"skills": [s.strip() for s in f.get("skills", "general").split(",") if s.strip()],
             "shift": [8, 16], "available": True} if f["role"] == "technician" else {}
    try:
        insert("users", {**extra, "username": f["username"], "password_hash": generate_password_hash(f["password"]),
                         "role": f["role"], "name": f["name"], "dept": f.get("dept") or None,
                         "email": f"{f['username'].lower()}@campus.edu",
                         "roll": f["username"] if is_student else None, "year": 1 if is_student else None,
                         "cgpa": 0, "xp": 0, "blood_group": None,
                         "parent_phone": re.sub(r"\D", "", f.get("parent_phone", "")) or None})
        flash(f"User {f['username']} created.", "success")
    except DuplicateKeyError:
        flash("That login ID already exists.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/grievance/<int:gid>")
@role_required("admin")
def admin_grievance(gid):
    update("grievances", {"id": gid}, {"status": request.form["status"]})
    return redirect(url_for("admin_dashboard"))


# ---------- weekly WhatsApp reports ----------
def build_report(student):
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    extras = {
        "assignments": pending_assignments(student["id"]),
        "quizzes": count("quizzes", {"student_id": student["id"], "status": "Submitted",
                                     "taken_at": {"$gte": week_ago}}),
        "open_tickets": count("tickets", {"reporters": student["id"], "status": {"$in": ops.OPEN_STATES}}),
    }
    return reports.build_pdf(student, student_summary(student["id"], student["dept"]), extras)


def send_weekly_reports():
    """Builds every student's PDF and sends it to their parent's WhatsApp. Returns (sent, total)."""
    students = find("users", {"role": "student"})
    sent = 0
    for st in students:
        path = build_report(st)
        if st.get("parent_phone"):
            ok, detail = reports.send_whatsapp_pdf(
                st["parent_phone"], path, f"Weekly performance report of {st['name']} ({st['roll']}) - CampusVerse")
        else:
            ok, detail = False, "No parent number on file"
        sent += ok
        insert("report_log", {"student_id": st["id"], "sent_at": now(), "status": "Sent" if ok else "Not sent",
                              "detail": detail})
    db.settings.update_one({"key": "last_report_run"}, {"$set": {"value": now()}}, upsert=True)
    return sent, len(students)


def background_scheduler():
    """Every minute: SLA check and escalations. Weekly: parent reports once 7 days have passed."""
    while True:
        try:
            ops.sla_check()
            row = find_one("settings", {"key": "last_report_run"})
            last = datetime.strptime(row["value"], "%Y-%m-%d %H:%M") if row else None
            if last is None or (datetime.now() - last).days >= 7:
                send_weekly_reports()
        except Exception as e:  # keep the scheduler alive; the next check retries
            app.logger.error("Scheduled job failed: %s", e)
        time.sleep(60)


@app.post("/jobs")
@role_required("recruiter")
def post_job():
    f = request.form
    insert("jobs", {"posted_by": g.user["id"], "title": f["title"], "company": f["company"],
                    "description": f["description"], "min_cgpa": float(f["min_cgpa"] or 0), "kind": "Drive"})
    flash("Placement drive posted for students.", "success")
    return redirect(url_for("recruiter_dashboard"))


# ---------- recruiter ----------
@app.route("/recruiter")
@role_required("recruiter")
def recruiter_dashboard():
    min_cgpa = float(request.args.get("min_cgpa", 0) or 0)
    dept = request.args.get("dept", "")
    flt = {"role": "student", "cgpa": {"$gte": min_cgpa}}
    if dept:
        flt["dept"] = dept
    candidates = find("users", flt, sort=[("cgpa", DESCENDING)])
    jobs = find("jobs", {"posted_by": g.user["id"]}, sort=[("id", DESCENDING)])
    jobs_by_id = {j["id"]: j for j in jobs}
    users = users_by_id()
    applicants = sorted((dict(a, title=jobs_by_id[a["job_id"]]["title"], name=users[a["student_id"]]["name"],
                              roll=users[a["student_id"]]["roll"], cgpa=users[a["student_id"]]["cgpa"])
                         for a in find("applications", {"job_id": {"$in": list(jobs_by_id)}})),
                        key=lambda a: (a["job_id"], -a["cgpa"]))
    depts = sorted(db.users.distinct("dept", {"role": "student"}))
    return render_template("recruiter.html", u=g.user, candidates=candidates, jobs=jobs, applicants=applicants,
                           depts=depts, min_cgpa=min_cgpa, dept=dept, notices=notices_for("recruiter"))


@app.post("/recruiter/application/<int:aid>")
@role_required("recruiter")
def recruiter_application(aid):
    application = find_one("applications", {"id": aid}) or abort(404)
    if not find_one("jobs", {"id": application["job_id"], "posted_by": g.user["id"]}):
        abort(403)
    update("applications", {"id": aid}, {"status": request.form["status"]})
    return redirect(url_for("recruiter_dashboard"))


if __name__ == "__main__":
    from db import OFFLINE
    if OFFLINE:
        # In-memory database lives inside this process, so fill it here (only in the serving process)
        if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            import seed
            seed.main()
    elif count("users") == 0:
        # Never seed Atlas automatically: the debug reloader could run it twice at once. Seed explicitly instead.
        print("The database is empty. Run `python seed.py` once, then start the app again.")
    # Flask's debug reloader runs this file twice; start the scheduler only in the serving process.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Thread(target=background_scheduler, daemon=True).start()
    app.run(debug=True)
