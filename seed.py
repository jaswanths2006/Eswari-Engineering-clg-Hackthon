"""Fills the MongoDB Atlas database with fictional demo data for CampusVerse + CampusOps.
Run: python seed.py      WARNING: this deletes and recreates all CampusVerse collections.

Demo logins (local testing only — change before deploying):
  Role                      Login ID                      Password
  System Administrator      admin                         admin@123
  Facilities Administrator  facilities                    facilities@123
  Procurement Approver      procurement                   procure@123
  Department Manager        mgr.cse / mgr.ece / mgr.mech  manager@123
  Technician                tech.ramesh, tech.suresh, tech.kavya, tech.imran, tech.lakshmi, tech.prakash   tech@123
  Faculty                   FAC101 FAC102 FAC201 FAC202 FAC301 FAC302   faculty@123
  Student                   22CSE001-010, 22ECE001-010, 22MEC001-010    student@123
  Recruiter                 hr.techcorp                   recruiter@123
"""
import os
import random
from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash

from db import db, ensure_indexes, insert

COLLECTIONS = ["users", "subjects", "attendance", "marks", "sgpa", "fees", "assignments", "notes", "notices",
               "leaves", "grievances", "jobs", "applications", "quizzes", "tickets", "chat_messages",
               "report_log", "settings", "counters", "buildings", "rooms", "equipment", "inventory",
               "maintenance_history", "bookings", "approvals", "purchase_orders", "notifications", "outbox",
               "incident_groups", "processed_events", "offerings", "enrollments", "enrollment_requests"]

DEPTS = {"CSE": "Computer Science & Engineering", "ECE": "Electronics & Communication Engineering",
         "MECH": "Mechanical Engineering"}
# First 4 per department are core (everyone takes them); the rest are electives students choose
SUBJECTS = {
    "CSE": [("CS301", "Database Systems"), ("CS302", "Operating Systems"), ("CS303", "Computer Networks"),
            ("CS304", "Machine Learning"), ("CS305", "Software Engineering"), ("CS306", "Web Technologies"),
            ("CS307", "Compiler Design")],
    "ECE": [("EC301", "Digital Signal Processing"), ("EC302", "VLSI Design"), ("EC303", "Microprocessors"),
            ("EC304", "Communication Systems"), ("EC305", "Control Systems"), ("EC306", "Embedded Systems"),
            ("EC307", "Antennas & Wave Propagation")],
    "MECH": [("ME301", "Thermodynamics"), ("ME302", "Fluid Mechanics"), ("ME303", "Machine Design"),
             ("ME304", "Manufacturing Technology"), ("ME305", "Heat Transfer"), ("ME306", "CAD/CAM")],
}
# Subjects each faculty member is qualified to teach (the HOD can only assign within this list)
EXPERTISE = {"FAC101": ["CS301", "CS302", "CS303", "CS305", "CS306"],
             "FAC102": ["CS303", "CS304", "CS305", "CS306", "CS307"],
             "FAC201": ["EC301", "EC302", "EC303", "EC305", "EC306"],
             "FAC202": ["EC302", "EC304", "EC305", "EC306", "EC307"],
             "FAC301": ["ME301", "ME302", "ME303", "ME305", "ME306"],
             "FAC302": ["ME303", "ME304", "ME305", "ME306"]}
FACULTY = [("FAC101", "Prof. Anitha Rao", "CSE", 4), ("FAC102", "Prof. Kiran Kumar", "CSE", 3),
           ("FAC201", "Prof. Deepa Nair", "ECE", 4), ("FAC202", "Prof. Rajesh Iyer", "ECE", 3),
           ("FAC301", "Prof. Vikram Singh", "MECH", 3), ("FAC302", "Prof. Sneha Patil", "MECH", 3)]
STUDENTS = {
    "CSE": ["Sandeep Paleru", "Priya Sharma", "Rahul Verma", "Ananya Gupta", "Karthik Reddy", "Sneha Iyer",
            "Vikas Yadav", "Divya Menon", "Arun Prakash", "Meera Joshi"],
    "ECE": ["Arjun Reddy", "Pooja Nair", "Rohit Das", "Kavitha Raman", "Nikhil Jain", "Swathi Rao",
            "Manoj Kumar", "Harini Subramanian", "Aditya Shetty", "Lavanya Pillai"],
    "MECH": ["Siddharth Patil", "Ritika Singh", "Gautham Krishna", "Neha Kulkarni", "Varun Malhotra",
             "Shreya Ghosh", "Abhishek Mishra", "Fathima Shaikh", "Pranav Hegde", "Ishita Bose"],
}
ROLL_PREFIX = {"CSE": "22CSE", "ECE": "22ECE", "MECH": "22MEC"}
BUILDINGS = [("A", "Academic Block A", "CSE"), ("B", "Academic Block B", "ECE"), ("C", "Academic Block C", "MECH"),
             ("L", "Library & Admin Block", None), ("H", "Hostel Block H", None)]
ROOMS = [  # code, name, building, type, capacity
    ("A-101", "Classroom A-101", "A", "classroom", 60), ("A-102", "Classroom A-102", "A", "classroom", 60),
    ("A-201", "Computer Lab 1", "A", "lab", 40), ("A-202", "Computer Lab 2", "A", "lab", 40),
    ("A-301", "Exam Hall A", "A", "exam_hall", 120),
    ("B-101", "Classroom B-101", "B", "classroom", 60), ("B-102", "Classroom B-102", "B", "classroom", 60),
    ("B-201", "Electronics Lab", "B", "lab", 40), ("B-301", "Seminar Hall B", "B", "seminar", 150),
    ("C-101", "Classroom C-101", "C", "classroom", 60), ("C-102", "Classroom C-102", "C", "classroom", 60),
    ("C-201", "Mechanical Workshop", "C", "lab", 40), ("C-202", "CAD Lab", "C", "lab", 40),
    ("L-001", "Central Library Reading Hall", "L", "library", 100), ("L-101", "Server Room", "L", "server", 5),
    ("L-201", "Main Auditorium", "L", "seminar", 300),
    ("H-001", "Hostel Mess Hall", "H", "mess", 200), ("H-101", "Hostel Room H-101", "H", "hostel", 3),
    ("H-102", "Hostel Room H-102", "H", "hostel", 3), ("H-214", "Hostel Room H-214", "H", "hostel", 3),
]
EQUIPMENT_BY_TYPE = {
    "classroom": ["Projector", "Split AC", "Ceiling fans (4)", "Wi-Fi access point", "LED lights (8)"],
    "lab": ["Desktop computers (40)", "Split AC (2)", "Wi-Fi access point", "UPS 10kVA", "LED lights (10)"],
    "exam_hall": ["Central AC", "LED lights (20)", "CCTV cameras (4)", "PA system"],
    "seminar": ["Projector", "Central AC", "PA system", "Wi-Fi access point"],
    "library": ["Split AC (3)", "Wi-Fi access point (2)", "LED lights (16)", "CCTV cameras (2)"],
    "server": ["Network switch 24-port (core)", "UPS 20kVA", "Precision AC", "Firewall"],
    "mess": ["Ceiling fans (12)", "Water purifier", "Wi-Fi access point", "Exhaust fans"],
    "hostel": ["Ceiling fan", "LED tube light", "Study lamp", "Attached bathroom tap"],
}
TECHNICIANS = [  # login, name, skills, shift [start, end)
    ("tech.ramesh", "Ramesh Kumar", ["electrical", "hvac"], [8, 16]),
    ("tech.suresh", "Suresh Babu", ["plumbing", "carpentry", "housekeeping"], [8, 16]),
    ("tech.kavya", "Kavya Reddy", ["network", "it"], [8, 16]),
    ("tech.imran", "Imran Khan", ["electrical", "it", "carpentry"], [14, 22]),
    ("tech.lakshmi", "Lakshmi Devi", ["network", "electrical", "security"], [22, 6]),
    ("tech.prakash", "Prakash Rao", ["hvac", "plumbing", "housekeeping"], [14, 22]),
]
INVENTORY = [  # name, stock, reorder level, unit cost (Rs), category
    ("LED tube light 40W", 25, 5, 350, "Electrical"), ("Ceiling fan capacitor", 15, 4, 120, "Electrical"),
    ("MCB 32A", 6, 2, 450, "Electrical"), ("Projector lamp", 1, 1, 8500, "IT Equipment"),
    ("HDMI cable 10m", 8, 2, 900, "IT Equipment"), ("AC gas refill kit", 2, 1, 3500, "HVAC"),
    ("Water tap (faucet)", 10, 3, 650, "Plumbing"), ("PVC pipe 1 inch (3m)", 12, 3, 300, "Plumbing"),
    ("Wi-Fi access point", 3, 1, 9500, "Network"), ("Network switch 24-port", 0, 1, 42000, "Network"),
    ("UPS battery 12V", 4, 2, 2800, "Electrical"), ("Door lock (mortise)", 5, 2, 750, "Carpentry"),
]
SLOTS = [("09:00", "10:00"), ("10:00", "11:00"), ("11:15", "12:15"), ("14:00", "15:00"), ("15:00", "16:00")]


def NOTES(sub_ids):
    return (
        (sub_ids[0], "Unit 1 - Relational Model",
         "The relational model stores data in relations (tables). Each row is a tuple and each column an attribute "
         "with a domain. A super key uniquely identifies a tuple; a candidate key is a minimal super key; the primary "
         "key is the chosen candidate key. A foreign key references the primary key of another relation and enforces "
         "referential integrity. Entity integrity means a primary key can never be NULL. Relational algebra "
         "operations: selection (sigma) filters rows, projection (pi) picks columns, union, set difference, "
         "Cartesian product, and join, which combines related tuples. Natural join matches on common attributes."),
        (sub_ids[0], "Unit 2 - SQL Queries",
         "SQL has DDL (CREATE, ALTER, DROP), DML (SELECT, INSERT, UPDATE, DELETE) and DCL (GRANT, REVOKE). "
         "SELECT columns FROM table WHERE condition filters rows. GROUP BY groups rows for aggregate functions "
         "COUNT, SUM, AVG, MIN, MAX; HAVING filters groups after aggregation, while WHERE filters rows before it. "
         "ORDER BY sorts results. INNER JOIN returns matching rows from both tables; LEFT JOIN keeps all rows of the "
         "left table. A subquery is a query nested inside another. Views are virtual tables defined by a query."),
        (sub_ids[1], "Unit 1 - Processes & Threads",
         "A process is a program in execution with its own address space, stack and PCB (process control block). "
         "Process states: new, ready, running, waiting, terminated. A context switch saves the state of one process "
         "and loads another. Threads are lightweight units inside a process that share code, data and files but have "
         "their own stack and registers. User-level threads are managed by a library; kernel threads by the OS. "
         "Multithreading improves responsiveness and resource sharing. CPU scheduling algorithms include FCFS, SJF, "
         "Priority and Round Robin, which uses a fixed time quantum."),
        (sub_ids[2], "Unit 1 - OSI Model",
         "The OSI model has 7 layers: Physical (bits over the medium), Data Link (frames, MAC addresses, error "
         "detection), Network (packets, IP addressing, routing), Transport (segments, TCP reliable and UDP "
         "connectionless, ports), Session (dialog control), Presentation (encryption, compression, translation) and "
         "Application (HTTP, FTP, SMTP, DNS). The TCP/IP model has 4 layers: Link, Internet, Transport, Application. "
         "Routers work at the network layer, switches at the data link layer, hubs at the physical layer."),
        (sub_ids[3], "Unit 1 - Intro to ML",
         "Machine learning lets computers learn patterns from data. Supervised learning uses labelled data: "
         "regression predicts numbers, classification predicts categories. Unsupervised learning finds structure in "
         "unlabelled data, e.g. clustering with k-means. Reinforcement learning learns by reward. Data is split into "
         "training and test sets. Overfitting means the model memorises training data and performs poorly on new "
         "data; underfitting means it is too simple. Common metrics: accuracy, precision, recall, MSE."),
    )


EXTRA_NOTES = {
    "EC301": ("Unit 1 - Signals and the DFT",
              "A discrete-time signal is a sequence of samples taken at regular intervals. The sampling theorem says "
              "a signal must be sampled at more than twice its highest frequency, called the Nyquist rate, to avoid "
              "aliasing. The Discrete Fourier Transform converts N time samples into N frequency components. The Fast "
              "Fourier Transform computes the DFT in N log N operations instead of N squared. FIR filters have a finite "
              "impulse response and are always stable; IIR filters use feedback and can be unstable."),
    "EC303": ("Unit 2 - 8086 Architecture",
              "The 8086 is a 16-bit microprocessor with a 20-bit address bus, so it can address 1 MB of memory. It is "
              "divided into the Bus Interface Unit, which fetches instructions into a 6-byte queue, and the Execution "
              "Unit, which decodes and executes them. Memory is segmented into code, data, stack and extra segments. "
              "A physical address is formed by shifting the segment register left by 4 bits and adding the offset."),
    "ME301": ("Unit 1 - Laws of Thermodynamics",
              "The zeroth law defines temperature through thermal equilibrium. The first law is conservation of energy: "
              "heat added equals the change in internal energy plus work done. The second law states that heat cannot "
              "flow from a colder body to a hotter body without external work, and that entropy of an isolated system "
              "never decreases. The Carnot cycle is the most efficient cycle between two temperatures."),
    "ME302": ("Unit 1 - Fluid Properties",
              "Density is mass per unit volume. Viscosity is a fluid's resistance to flow; Newtonian fluids have constant "
              "viscosity. Bernoulli's equation states that pressure energy, kinetic energy and potential energy per unit "
              "volume stay constant along a streamline for an ideal fluid. The Reynolds number predicts laminar flow "
              "below about 2000 and turbulent flow above about 4000 in pipes."),
}


def main():
    for name in COLLECTIONS:
        db[name].drop()
    ensure_indexes()
    random.seed(7)
    today = date.today()

    def user(username, pw, role, name, **extra):
        return insert("users", {"username": username, "password_hash": generate_password_hash(pw), "role": role,
                                "name": name, "dept": None, "roll": None, "year": None, "cgpa": 0, "xp": 0,
                                "blood_group": None, "parent_phone": None,
                                "email": f"{username.lower()}@campus.edu", **extra})

    # ---- people
    user("admin", "admin@123", "admin", "Dr. Principal Office (System Admin)")
    user("facilities", "facilities@123", "facilities_admin", "Venkat Rao (Facilities Office)")
    user("procurement", "procure@123", "procurement", "Fatima Begum (Procurement)")
    managers = {d: user(f"mgr.{d.lower()}", "manager@123", "dept_manager", n, dept=d)
                for d, n in (("CSE", "Dr. Meena Krishnan"), ("ECE", "Dr. Arvind Menon"),
                             ("MECH", "Dr. Joseph Thomas"))}
    techs = [user(login, "tech@123", "technician", name, skills=skills, shift=shift, available=True)
             for login, name, skills, shift in TECHNICIANS]
    faculty = {code: user(code, "faculty@123", "faculty", name, dept=dept) for code, name, dept, _ in FACULTY}
    students = {}
    for dept, names in STUDENTS.items():
        for i, name in enumerate(names, 1):
            roll = f"{ROLL_PREFIX[dept]}{i:03d}"
            students[roll] = user(roll, "student@123", "student", name, dept=dept, roll=roll, year=3,
                                  cgpa=round(random.uniform(6.2, 9.4), 1), xp=random.randint(80, 520),
                                  blood_group=random.choice(["A+", "B+", "O+", "AB+", "O-"]),
                                  parent_phone=f"9100000{len(students) + 1:05d}")
    db.users.update_one({"username": "22CSE001"}, {"$set": {"cgpa": 8.6, "xp": 420}})
    hr = user("hr.techcorp", "recruiter@123", "recruiter", "TechCorp HR Team")

    # ---- subjects (20): 4 core per department + electives; faculty assigned by the HOD rules in academics.py
    import academics
    subject_ids, dept_subjects = {}, {}
    for dept, subs in SUBJECTS.items():
        for i, (code, name) in enumerate(subs):
            kind = "core" if i < 4 else "elective"
            sid = insert("subjects", {"code": code, "name": name, "dept": dept, "kind": kind,
                                      "credits": 4 if kind == "core" else 3})
            subject_ids[code] = sid
    for code, _, dept, _ in FACULTY:
        db.users.update_one({"id": faculty[code]}, {"$set": {"expertise": EXPERTISE[code]}})
    for dept in SUBJECTS:
        academics.auto_assign_teaching(dept)
        db.settings.insert_one({"key": f"registration_open:{dept}", "value": True})
    for sub in db.subjects.find({}, {"_id": 0}):  # main teacher = section A (used for the timetable)
        first = db.offerings.find_one({"subject_id": sub["id"]}, {"_id": 0}, sort=[("section", 1)])
        dept_subjects.setdefault(sub["dept"], []).append((sub["id"], sub["code"], sub["name"], first["faculty_id"]))

    # ---- course registration: core automatically, electives chosen by each student (with a faculty section)
    for roll, sid in students.items():
        st = db.users.find_one({"id": sid}, {"_id": 0})
        academics.enroll_core(st)
        electives = [s for s in db.subjects.find({"dept": st["dept"], "kind": "elective"}, {"_id": 0})]
        for s in random.sample(electives, len(electives)):
            if db.enrollments.count_documents({"student_id": sid, "kind": "elective"}) >= academics.ELECTIVES_REQUIRED[st["dept"]]:
                break
            for o in random.sample(list(db.offerings.find({"subject_id": s["id"]}, {"_id": 0})),
                                   db.offerings.count_documents({"subject_id": s["id"]})):
                if academics.enroll(st, o["id"])[0]:
                    break

    # ---- demo requests waiting for faculty: students asking to join / switch to a specific faculty's section
    st1 = db.users.find_one({"username": "22CSE001"}, {"_id": 0})
    first_el = db.enrollments.find_one({"student_id": st1["id"], "kind": "elective"}, {"_id": 0})
    academics.drop(st1, first_el["subject_id"])  # leaves 22CSE001 one elective slot to request in the demo
    for login, code, note in (("22CSE003", "CS305", "I'd like to learn from this faculty's project-based style"),
                              ("22CSE006", "CS306", "My friends are in this section for the web project"),
                              ("22ECE003", "EC305", "Clash with my lab batch in the other section"),
                              ("22MEC002", "ME306", "Interested in this faculty's CAD workshop")):
        st = db.users.find_one({"username": login}, {"_id": 0})
        sub = db.subjects.find_one({"code": code}, {"_id": 0})
        mine = db.enrollments.find_one({"student_id": st["id"], "subject_id": sub["id"]}, {"_id": 0})
        for o in db.offerings.find({"subject_id": sub["id"]}, {"_id": 0}):
            if not mine or o["id"] != mine["offering_id"]:
                ok, _ = academics.request_section(st, o["id"], note)
                if ok:
                    break

    # ---- academics: attendance (20 class days), marks, SGPA, fees — for the subjects each student takes
    rows = []
    for roll, sid in students.items():
        taking = academics.enrolled_subject_ids(sid)
        habit = 0.82 if roll == "22CSE001" else 0.62 if roll == "22ECE001" else random.uniform(0.66, 0.97)
        for i in range(20):
            day = (today - timedelta(days=28 - i)).isoformat()
            for sub_id in taking:
                rows.append({"student_id": sid, "subject_id": sub_id, "date": day,
                             "present": int(random.random() < habit)})
        base = random.randint(45, 88)
        db.marks.insert_many([{"student_id": sid, "subject_id": sub_id, "exam": exam, "max_score": mx,
                               "score": max(0, min(mx, round(mx * (base + random.randint(-12, 12)) / 100)))}
                              for sub_id in taking
                              for exam, mx in (("Mid-1", 30), ("Mid-2", 30), ("Assignment", 10))])
        db.sgpa.insert_many([{"student_id": sid, "sem": s, "sgpa": round(min(10, base / 10 + random.uniform(-0.6, 0.8)), 2)}
                             for s in range(1, 6)])
        for term, amount, paid, days in (("Sem 5 Tuition", 65000, 1, -60), ("Sem 6 Tuition", 65000,
                                          int(random.random() < 0.4), 15), ("Exam Fee", 1500, 0, 5)):
            insert("fees", {"student_id": sid, "term": term, "amount": amount, "paid": paid,
                            "due_date": (today + timedelta(days=days)).isoformat()})
    db.attendance.insert_many(rows)

    # ---- notes and assignments
    cse_ids = [subject_ids[c] for c in ("CS301", "CS302", "CS303", "CS304")]
    for sub, title, content in NOTES(cse_ids):
        insert("notes", {"subject_id": sub, "title": title, "content": content, "summary": None})
    for code, (title, content) in EXTRA_NOTES.items():
        insert("notes", {"subject_id": subject_ids[code], "title": title, "content": content, "summary": None})
    for code, title, d, content in (
        ("CS301", "ER diagram for library system", 3,
         "Draw an ER diagram for a college library with Book, Member, Loan and Author. Show keys and cardinality, then "
         "convert it to tables in 3NF. Submit as a PDF."),
        ("CS302", "Process scheduling report", 6,
         "Compare FCFS, SJF, Round Robin (quantum 4) and Priority scheduling for the given 5 processes with Gantt "
         "charts, average waiting and turnaround time."),
        ("CS304", "Linear regression mini-project", 10,
         "Predict house price from area and rooms. Split 80/20, report MSE and R2, plot predicted vs actual."),
        ("EC303", "8086 assembly programs", 4,
         "Write 8086 programs to add two 16-bit numbers, find the largest in an array and sort 10 numbers. "
         "Include flowcharts and outputs from the emulator."),
        ("EC301", "FIR filter design", 8,
         "Design a low-pass FIR filter using the window method for a cutoff of 1 kHz at 8 kHz sampling. "
         "Plot the magnitude response for Hamming and rectangular windows."),
        ("ME301", "Carnot cycle problem set", 5,
         "Solve the 6 problems on Carnot efficiency, entropy change and heat engines. Show units at every step."),
        ("ME306", "CAD model of a gear assembly", 9,
         "Model a spur gear pair in CAD with 20 and 40 teeth, module 2. Submit drawings with dimensions and a BOM."),
    ):
        insert("assignments", {"subject_id": subject_ids[code], "title": title, "content": content, "summary": None,
                               "due": (today + timedelta(days=d)).isoformat()})

    # ---- campus: buildings, rooms, equipment
    for code, name, dept in BUILDINGS:
        insert("buildings", {"code": code, "name": name, "dept": dept})
    room_ids = {}
    for code, name, b, rtype, cap in ROOMS:
        dept = next(d for c, _, d in BUILDINGS if c == b)
        room_ids[code] = insert("rooms", {"code": code, "name": name, "building": b, "type": rtype, "capacity": cap,
                                          "dept": dept, "status": "available", "maintenance_ticket": None})
        for eq in EQUIPMENT_BY_TYPE[rtype]:
            insert("equipment", {"room_id": room_ids[code], "name": eq, "status": "Working",
                                 "installed": f"{random.randint(2017, 2024)}-0{random.randint(1, 9)}-15",
                                 "last_service": (today - timedelta(days=random.randint(10, 200))).isoformat()})

    # ---- inventory
    for name, stock, reorder, cost, cat in INVENTORY:
        insert("inventory", {"name": name, "stock": stock, "reserved": 0, "on_order": 0, "reorder_level": reorder,
                             "unit_cost": cost, "category": cat, "supplier": "Campus Supplies Pvt Ltd"})

    # ---- maintenance history (last 60 days) incl. recurring problems
    for code, cat, title, days, tech, cost in (
        ("A-201", "HVAC", "AC not cooling", 52, 0, 3500), ("A-201", "HVAC", "AC leaking water", 30, 5, 1200),
        ("A-201", "HVAC", "AC not cooling again", 9, 0, 3500), ("H-214", "Electrical", "Fan regulator faulty", 40, 3, 300),
        ("H-214", "Electrical", "Tube light flickering", 18, 0, 350), ("L-101", "Network", "Switch port failure", 35, 2, 0),
        ("L-101", "Network", "Core switch reboot loop", 12, 2, 0), ("B-201", "Electrical", "UPS battery replaced", 25, 3, 2800),
        ("C-201", "Carpentry", "Workbench repaired", 20, 1, 900), ("H-102", "Plumbing", "Tap replaced", 15, 1, 650),
    ):
        insert("maintenance_history", {"room_id": room_ids[code], "category": cat, "title": title, "ticket_id": None,
                                       "technician_id": techs[tech], "cost": cost,
                                       "date": (today - timedelta(days=days)).isoformat()})

    # ---- bookings: 14-day timetable, exams, events
    lab_of = {"CSE": "A-201", "ECE": "B-201", "MECH": "C-201"}
    class_of = {"CSE": "A-101", "ECE": "B-101", "MECH": "C-101"}
    second_of = {"CSE": "A-102", "ECE": "B-102", "MECH": "C-102"}
    for offset in range(14):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for dept, subs in dept_subjects.items():
            for s, (start, end) in enumerate(SLOTS):
                sub_id, code, name, fac_id = subs[(day.weekday() * 5 + s) % len(subs)]
                room = lab_of[dept] if s >= 3 and s % 2 else class_of[dept]
                insert("bookings", {"room_id": room_ids[room], "date": day.isoformat(), "start": start, "end": end,
                                    "title": f"{code} {name}" + (" (Lab)" if room == lab_of[dept] else ""),
                                    "kind": "class", "owner_id": fac_id, "dept": dept, "attendees": 40,
                                    "status": "Active", "created": "seed"})
                if s in (0, 3):  # second-year classes use the other classroom part of the day
                    insert("bookings", {"room_id": room_ids[second_of[dept]], "date": day.isoformat(), "start": start,
                                        "end": end, "title": f"Year 2: {code[:2]}2{code[3:]} tutorial", "kind": "class",
                                        "owner_id": fac_id, "dept": dept, "attendees": 45, "status": "Active",
                                        "created": "seed"})
    tomorrow = (today + timedelta(days=1)).isoformat()
    for code, room, d, start, end, n in (("CS301", "A-301", 1, "10:00", "13:00", 110),
                                         ("EC303", "B-301", 2, "10:00", "13:00", 100),
                                         ("ME301", "A-301", 3, "14:00", "17:00", 90)):
        sub = next(s for subs in dept_subjects.values() for s in subs if s[1] == code)
        insert("bookings", {"room_id": room_ids[room], "date": (today + timedelta(days=d)).isoformat(), "start": start,
                            "end": end, "title": f"Mid-2 Exam: {code} {sub[2]}", "kind": "exam", "owner_id": sub[3],
                            "dept": next(dp for dp, ss in dept_subjects.items() if sub in ss), "attendees": n,
                            "status": "Active", "created": "seed"})
    insert("bookings", {"room_id": room_ids["L-201"], "date": tomorrow, "start": "15:00", "end": "17:00",
                        "title": "Guest lecture: AI in Industry", "kind": "event", "owner_id": faculty["FAC102"],
                        "dept": "CSE", "attendees": 250, "status": "Active", "created": "seed"})

    # ---- notices, grievances, jobs, leaves
    for title, body, target in (
        ("Mid-2 exams begin tomorrow", "Check your exam hall in the timetable; room changes are notified here.", "all"),
        ("Hackathon registrations open", "Register before Friday. Winners get 200 XP!", "student"),
        ("Hostel water supply maintenance", "Block H water off on Sunday 9 AM to 12 PM.", "student"),
        ("Faculty meeting at 3 PM", "Seminar Hall B, agenda: internal assessment.", "faculty"),
        ("Night shift roster updated", "Lakshmi Devi covers 22:00-06:00 this week.", "technician"),
        ("Quarterly budget review", "Submit pending purchase requests by Friday.", "procurement"),
        ("Placement season 2026 begins", "Recruiters can now post drives on the portal.", "recruiter"),
    ):
        insert("notices", {"title": title, "body": body, "target": target, "created": today.isoformat()})
    insert("grievances", {"token": "GRV-1A2B3C", "category": "Academics",
                          "message": "Lab sessions keep getting cancelled.", "status": "In Progress"})
    insert("jobs", {"posted_by": hr, "title": "Software Engineer Intern", "company": "TechCorp",
                    "description": "6-month internship, Hyderabad. Stipend 30k/month.", "min_cgpa": 7.5, "kind": "Drive"})
    insert("leaves", {"student_id": students["22ECE001"], "from_date": today.isoformat(),
                      "to_date": today.isoformat(), "reason": "Medical appointment", "status": "Pending"})

    # ---- a few finished work orders (for SLA analytics)
    stamp = lambda h: (datetime.now() - timedelta(hours=h)).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    for i, (code, cat, title, prio, age, took, tech) in enumerate((
        ("A-102", "IT Equipment", "Projector not turning on", "P3", 120, 20, 2),
        ("B-101", "Electrical", "Two tube lights not working", "P4", 100, 30, 0),
        ("H-101", "Plumbing", "Washroom flush broken", "P3", 90, 80, 1),
        ("L-001", "Network", "Library Wi-Fi slow", "P2", 70, 30, 2),
        ("C-202", "HVAC", "CAD lab AC not cooling", "P2", 60, 12, 5),
        ("A-201", "HVAC", "Lab AC tripping", "P2", 50, 18, 0),
    )):
        sla = {"P1": 4, "P2": 24, "P3": 72, "P4": 168}[prio]
        tid = insert("tickets", {
            "title": title, "description": title, "category": cat, "severity": 3, "severity_reason": "Seed data",
            "assets": [], "safety": False, "room_unusable": False, "ai_summary": title, "ai_used": False,
            "source": ["Form", "Email", "AI chat"][i % 3], "reporter_id": None, "reporter_email": None,
            "reporters": [], "report_count": 1, "room_id": room_ids[code], "room_code": code, "building": code[0],
            "dept": next(d for c, _, d in BUILDINGS if c == code[0]), "location_text": code, "assigned_to": techs[tech],
            "parts": [], "cost_estimate": None, "cost_approved": False, "blocked_reason": None, "group_id": None,
            "duplicate_of": None, "escalation_level": 0, "sla_state": "On track", "event_key": None,
            "status": "Closed", "priority": prio, "priority_score": 40, "sla_hours": sla,
            "created": stamp(age), "sla_due": stamp(age - sla), "resolved_at": stamp(age - took),
            "updated": stamp(age - took), "affected_bookings": [],
            "updates": [{"status": "Closed", "message": "Fixed and verified.", "at": stamp(age - took), "by": "System"}]})
        db.tickets.update_one({"id": tid}, {"$set": {"ref": f"CO-{tid:04d}"}})

    db.settings.insert_one({"key": "last_report_run", "value": stamp(0)})

    # ---- a few live reports through the real CampusOps pipeline (rules only, so seeding is fast and repeatable)
    import ops
    saved_key = os.environ.pop("GEMINI_API_KEY", None)
    try:
        u = lambda login: db.users.find_one({"username": login}, {"_id": 0})  # noqa: E731
        ops.intake("Ceiling fan in my hostel room H-214 stopped working since last night", u("22CSE001"), "Form",
                   room_code="H-214")
        ops.intake("Projector in A-102 keeps flickering and switches off during lectures", u("FAC101"), "Form",
                   room_code="A-102", category="IT Equipment")
        ops.intake("Tap leaking continuously in the bathroom of H-102, water on the floor", u("22ECE002"), "Form",
                   room_code="H-102")
        ops.intake("The AC is making a very loud rattling noise during class", u("22MEC003"), "Form")
    finally:
        if saved_key:
            os.environ["GEMINI_API_KEY"] = saved_key
    print(f"Database '{db.name}' seeded: {db.users.count_documents({})} users, "
          f"{db.subjects.count_documents({})} subjects, {db.rooms.count_documents({})} rooms, "
          f"{db.bookings.count_documents({})} bookings, {db.tickets.count_documents({})} work orders.")


if __name__ == "__main__":
    main()
