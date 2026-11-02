# 🎓 CampusVerse + CampusOps — An Intelligent Campus Platform

> **Hackathon PS 2: CampusOps — An Intelligent Campus Operations Platform.**
> Natural-language maintenance reports from a form, an AI chat or a simulated email become prioritised, deduplicated, auto-assigned work orders. The platform coordinates technicians, rooms, exams, spare parts and approvals, and gives administrators one unified view. It's built on a full college portal (academics, AI study tools, placements, weekly WhatsApp reports to parents).

---

## 📌 Problem

Colleges handle maintenance complaints, room bookings, equipment, inventory, staff and incidents in disconnected systems. That leads to duplicate requests, wrong assignments, scheduling conflicts, shortages and delays. One electrical failure can affect classrooms, exams, technicians, spare parts and notifications all at once.

## 💡 Solution

CampusOps runs every report through one pipeline:

```
Report (form / AI chat / email / sensor webhook)
   │
   ▼  Gemini reads it: category, room, severity 1-5 + reason, equipment, room unusable?, safety risk?
   │  (rule-based fallback if the AI is unavailable)
   ├─ location missing?        → "Needs Info" + clarification question (in-app or reply email)
   ├─ same room + type open?   → merged as Duplicate; reporter count and priority go up
   ├─ same building + type?    → linked incident; 4+ reports / 3+ rooms = MAJOR incident + AI summary
   ▼
Deterministic engine: priority score → P1-P4 → SLA deadline → room taken out of service if unusable
   → affected classes/exams moved to the best free room → technician picked by skill, shift, workload
   → targeted notifications (in-app + simulated email)
   ▼
Assigned → In Progress → (Blocked: part out of stock / costly repair approval) → Resolved → Closed
   with SLA tracking, 2-level escalation and automatic reassignment
```

**AI where it adds value; rules where correctness matters.** Gemini only reads reports and writes summaries. Permissions, priority, assignment, stock, bookings, approvals and SLAs are plain deterministic code ([ops.py](ops.py)).

---

## ✅ PS 2 requirements → where they are

| Requirement | Implementation |
|---|---|
| Intake by form, chat, simulated email | Report form (student/faculty/staff), AI chat assistant tool, `POST /api/inbound-email`, plus a sensor webhook |
| Extract issue type, location, urgency, assets; ask when details are missing | `ai.extract_issue` (Gemini, JSON schema) → **Needs Info** state with a clarification question; answered in-app or by replying to the email with `[CO-xxxx]` |
| AI categorisation, duplicate detection, correlation, incident summaries | Category + severity from Gemini; duplicates = same room + category within 72h; correlation = same building + category within 24h; `ai.summarize_incident` for linked incidents |
| Campus data | 5 buildings, 20 rooms, ~90 equipment items, 6 technicians with skills and shifts, 12 inventory items, 60 days of maintenance history |
| Assignment by skill, workload, priority, availability; reassignment | `pick_technician`: skill match → on shift (or soonest shift) → under capacity → lowest weighted workload. Reassigns when a technician is marked sick, or on level-2 escalation if overloaded |
| Room bookings, double-booking detection, affected classes, alternatives | Live clash check, free alternatives (same building, then type, then capacity), auto-relocation of classes and exams when a room goes out of service |
| Parts reservation, blocked when out of stock | Atomic stock reservation; shortage → **Blocked** + automatic purchase request; delivery → reserve → work order resumes automatically |
| Approvals with revalidation, no self-approval | Purchases (Procurement), costly repairs > ₹10,000 (that department's manager), overrides for bookings or assignments (Facilities / System Admin). Requester ≠ approver; constraints are rechecked at approval time |
| SLA tracking, escalation, targeted notifications | P1 4h, P2 24h, P3 72h, P4 7 days; at risk at 75%; breach → L1 (dept manager + facilities); +50% → L2 (system admin). In-app bell + simulated email outbox |
| Analytics | Open requests by state/priority/category, SLA compliance, average resolution time, technician and room utilisation, shortages, recurring problems, reports by channel |
| Backend role-based access, separation of duties | `roles_required(...)` on every route; 6 PS 2 roles below; ownership checks (a technician only acts on their own jobs, managers only approve their department) |
| Persistent database | MongoDB Atlas |
| Fictional campus data | [seed.py](seed.py) |
| ≥ 2 disruption scenarios | 5 one-click scenarios in the Operations Center (below) |
| Validated state transitions, repeated events safe | Explicit transition table; compare-and-set updates; repeating a state change is a no-op; email Message-ID and sensor event_id are processed once; approvals are claimed atomically |
| Simulated external integration | Email inbox webhook, sensor webhook, and email notification outbox ([Integrations page](templates/integrations.html)) |

---

## 👥 Roles

| Role (PS 2) | Login | Can do |
|---|---|---|
| Student / Faculty | `22CSE001`… / `FAC101`… | Report issues (form or AI chat), answer clarifications, track progress live. Faculty also book rooms, see relocations of their classes, request booking overrides |
| Technician | `tech.ramesh` etc. | Own queue in priority order, start/resolve, reserve parts, estimate cost, mark themselves unavailable |
| Department Manager | `mgr.cse`, `mgr.ece`, `mgr.mech` | Department work orders, approve costly repairs for their department only, see class/exam relocations, book rooms |
| Facilities Administrator | `facilities` | Operations Center: unified queue, reassign/override, technician availability, rooms, approvals, inventory, analytics, scenarios, integrations |
| Procurement Approver | `procurement` | Approve purchases, mark deliveries (auto-unblocks work), inventory |
| System Administrator | `admin` | Users, notices, weekly reports, approves overrides requested by facilities, full ops access |

The Recruiter login from the portal is also kept (placement drives).

---

## 🧪 Demo accounts

| Role | Login ID | Password |
|---|---|---|
| System Administrator | `admin` | `admin@123` |
| Facilities Administrator | `facilities` | `facilities@123` |
| Procurement Approver | `procurement` | `procure@123` |
| Department Manager | `mgr.cse` / `mgr.ece` / `mgr.mech` | `manager@123` |
| Technician | `tech.ramesh` `tech.suresh` `tech.kavya` `tech.imran` `tech.lakshmi` `tech.prakash` | `tech@123` |
| Faculty | `FAC101` `FAC102` (CSE) · `FAC201` `FAC202` (ECE) · `FAC301` `FAC302` (MECH) | `faculty@123` |
| Student | `22CSE001`–`010` · `22ECE001`–`010` · `22MEC001`–`010` | `student@123` |
| Recruiter | `hr.techcorp` | `recruiter@123` |

All data is fictional. These are demo passwords; change them before deploying.

**Campus:** 3 departments (CSE, ECE, MECH), 20 subjects, 6 faculty, 30 students. Buildings: A (CSE), B (ECE), C (MECH), L (Library, Server Room, Auditorium), H (Hostel). A 2-week timetable includes Mid-2 exams starting tomorrow in Exam Hall A-301.

---

## ⚡ Disruption scenarios (Operations Center → one click each)

1. **Exam hall power failure before an exam.** The report becomes P1 (severity + exam within 48h + safety). A-301 is taken out of service, the exam and classes move to the best free hall, and faculty and managers get in-app and email notices.
2. **Technician calls in sick.** The busiest technician is marked unavailable and their jobs are reassigned by skill and workload.
3. **Wi-Fi outage reported by many students.** 5 reports across hostel rooms: same-room reports merge as duplicates, everything links into a **major incident** with an AI summary, and priority rises.
4. **Critical part out of stock.** The server-room switch fails and no switch is in stock, so the job is **Blocked** and a purchase approval goes to Procurement. Approve, then mark delivered, and the job resumes automatically.
5. **SLA breach.** A job is pushed past its deadline, triggering escalation and notifications.

Also try: **Integrations → deliver an email**, then **re-deliver the same email** (ignored); send an email with no room to get a clarification reply, then answer it.

---

## 📚 Course registration: who teaches what, and who studies with whom

```
Department Manager (HOD)                 Student
   │ assigns subject → faculty              │ core subjects: enrolled automatically
   │   (only within the faculty's           │ electives: picks the required number and
   │    expertise, ≤ 18 credits each)       │   chooses the faculty's section (seats limited)
   ▼                                        ▼
Offering = subject + faculty + section + seats  ◀── Enrollment (one section per subject)
```

| Department | Core (everyone) | Electives (student chooses) | Electives required |
|---|---|---|---|
| CSE (7) | CS301–CS304 | CS305 Software Engg, CS306 Web Tech, CS307 Compiler Design | 2 |
| ECE (7) | EC301–EC304 | EC305 Control Systems, EC306 Embedded Systems, EC307 Antennas | 2 |
| MECH (6) | ME301–ME304 | ME305 Heat Transfer, ME306 CAD/CAM | 1 |

- **HOD (Department Manager → Teaching assignments):** add or remove a faculty section for each subject, set seats, **auto-assign** unstaffed subjects (qualified faculty with the lowest load first; core subjects get one section, electives up to two so students have a choice), and open or close registration. Removing a section moves its students to another section if there are seats.
- **Student → Choose faculty page:** every subject is a card listing each faculty member's section and the seats left. The student sends a **Request** (or **Request switch** to move a core or elective to another faculty), with an optional note. Nothing changes yet; the request appears under **My requests** as *Pending*.
- **Faculty → Student requests page:** only the faculty member who teaches that section sees and decides the request. They see the student's roll, CGPA, note, whether it's a switch, and free seats. **Accept** takes a seat atomically and enrolls the student (or moves them, freeing the old seat). **Reject** sends the faculty member's message back. **Accept all** handles the queue first come, first served while seats last.
- **Rules (checked again at acceptance):** registration must be open; own department only; at most the required number of electives (pending requests count); one open request per subject (a new choice replaces the old); a full section is rejected automatically with "Section is full"; a repeated click changes nothing; the student is notified either way.
- **Faculty (My teaching):** sees the sections the HOD assigned them. Attendance, marks, notes and quizzes only cover their own sections' students.
- **Everything follows enrollments:** each student's attendance, marks, notes, assignments, AI assistant answers and weekly parent report cover only the subjects they actually take.

## 🧭 Layout

Each role opens on a **Home launcher**: key stats plus a grid of neon tiles. Every option (attendance, marks, choose faculty, requests, queue, approvals and so on) is its **own page** with a header and breadcrumb. The sidebar switches pages, and the URL (`#choose`, `#requests`, …) keeps the back button and bookmarks working.

## 🎒 Portal features (CampusVerse)

- **Student:** AI assistant (Gemini) that knows their attendance, marks, fees and assignments, explains notes and files maintenance reports; AI note summaries and quizzes (scores go to faculty only); attendance risk alerts; XP and leaderboard; digital ID; leave, fees, placements, anonymous grievances.
- **Faculty:** attendance, marks, note/assignment uploads (PDF/TXT), AI quiz results, at-risk students, room booking, issue reporting.
- **Parents:** weekly PDF performance report on WhatsApp.

---

## 🛠️ Tech stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask (blueprint `ops` for CampusOps) |
| Database | MongoDB Atlas (pymongo) |
| AI | Google Gemini (`gemini-flash-lite-latest`): structured JSON extraction, function calling, summaries |
| Frontend | Jinja templates, Bootstrap 5, Chart.js |
| Integrations | Simulated email webhook + outbox, sensor webhook, WhatsApp Cloud API |
| PDF | fpdf2 (reports), pypdf (uploaded notes) |

```
hackathon/
├── app.py          # Portal routes (student, faculty, admin, recruiter) + background scheduler
├── ops.py          # CampusOps engine + routes (intake, dedupe, priority, SLA, assignment, bookings, parts, approvals, webhooks)
├── ai.py           # Gemini: chat assistant, summaries, quizzes, issue extraction, incident summaries
├── auth.py         # Sessions and backend role checks
├── db.py           # MongoDB Atlas connection and helpers
├── reports.py      # Weekly PDF + WhatsApp
├── seed.py         # Fictional campus data (wipes and reloads the database)
└── templates/      # ops_center, technician, manager, procurement, integrations, student, faculty, admin, …
```

---

## 🚀 Run it

```bash
pip install -r requirements.txt
```
```bash
copy .env.example .env
```
Fill in `MONGODB_URI` (required), `GEMINI_API_KEY`, and optionally `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID`. In Atlas, allow your IP under **Network Access**.

```bash
python seed.py
```
```bash
python app.py
```
Open **http://127.0.0.1:5000**. `seed.py` erases and reloads all data, so use it to reset between demos.

### Suggested 5-minute demo

1. Log in as `facilities` and show the **Operations Center** (queue, technicians, rooms, analytics).
2. Run **Scenario 1**: the exam is relocated. Log in as `FAC101` to see the room change in **My classes**.
3. Run **Scenario 3**: duplicates and a major incident.
4. Log in as `22CSE001` and tell the AI chat *"the fan in my hostel room isn't working"*. It asks where, then files the report, which appears live with its priority and technician.
5. Run **Scenario 4**, then log in as `procurement`: approve, then mark delivered. The work order unblocks itself.
6. Log in as `mgr.cse` to approve a costly repair; `mgr.ece` is refused for a CSE job (separation of duties).

---

## 📲 WhatsApp setup

1. In developers.facebook.com → your app → WhatsApp → API Setup, copy the **access token** and **phone number ID** into `.env`. The token on that page lasts 24 hours; for a lasting one, create a **System User** token with `whatsapp_business_messaging`.
2. In test mode, add the recipient numbers under **To**.
3. Free-form PDFs only reach people who messaged your number in the last 24 hours. For production, use an approved template.

---

## 📄 License

MIT. Built for the hackathon. *"One login. Every role. Whole campus."*
