"""CampusOps: intelligent campus operations.

Natural-language reports (form, AI chat, simulated email, sensor webhook) become work orders that are
deduplicated, correlated, prioritised, assigned, tracked against an SLA and escalated. Rooms, bookings,
inventory and approvals are coordinated around them.

AI (Gemini) is used only to read reports (category, location, severity, assets) and write incident summaries.
Permissions, priority, assignment, stock, bookings, approvals and SLAs are deterministic code below.
"""
import math
import os
import re
import secrets
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for
from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

import ai
from auth import ROLE_LABELS, current_user, roles_required
from db import count, db, find, find_one, insert, update

bp = Blueprint("ops", __name__)

# ---------------------------------------------------------------- constants
CATEGORIES = ["Electrical", "Plumbing", "Network", "HVAC", "Carpentry", "IT Equipment", "Cleanliness",
              "Security", "Other"]
SKILL_FOR = {"Electrical": "electrical", "Plumbing": "plumbing", "Network": "network", "HVAC": "hvac",
             "Carpentry": "carpentry", "IT Equipment": "it", "Cleanliness": "housekeeping",
             "Security": "security", "Other": "general"}
# Work-order state machine. Anything not listed here is rejected.
TRANSITIONS = {
    "New": {"Needs Info", "Triaged", "Duplicate"},
    "Needs Info": {"Triaged", "Duplicate", "Closed"},
    "Triaged": {"Assigned", "Duplicate", "Closed"},
    "Assigned": {"In Progress", "Blocked", "Triaged", "Assigned"},  # Assigned->Assigned = reassignment
    "In Progress": {"Blocked", "Resolved", "Assigned"},
    "Blocked": {"In Progress", "Assigned"},
    "Resolved": {"Closed", "In Progress"},
    "Closed": set(),
    "Duplicate": set(),
}
OPEN_STATES = ["New", "Needs Info", "Triaged", "Assigned", "In Progress", "Blocked"]
ACTIVE_STATES = ["Triaged", "Assigned", "In Progress", "Blocked"]  # SLA clock running
STEPS = ["New", "Triaged", "Assigned", "In Progress", "Resolved", "Closed"]
SLA_HOURS = {"P1": 4, "P2": 24, "P3": 72, "P4": 168}
PRIORITY_WEIGHT = {"P1": 4, "P2": 3, "P3": 2, "P4": 1}
TECH_CAPACITY = 10          # workload points before a technician counts as overloaded
COSTLY_REPAIR_LIMIT = 10000  # Rs; above this a repair needs manager approval
DUPLICATE_WINDOW_H = 72
CORRELATION_WINDOW_H = 24
INBOUND_SECRET = os.environ.get("INBOUND_SECRET", "campusops-demo-secret")
FMT = "%Y-%m-%d %H:%M"


class InvalidTransition(Exception):
    pass


def now_dt():
    return datetime.now().replace(second=0, microsecond=0)


def now():
    return now_dt().strftime(FMT)


def ago(hours):
    return (now_dt() - timedelta(hours=hours)).strftime(FMT)


def once(key):
    """Idempotency guard: True the first time a key is seen, False on every repeat."""
    try:
        db.processed_events.insert_one({"_id": key, "at": now()})
        return True
    except DuplicateKeyError:
        return False


def log(ticket_id, status, message, by="System"):
    db.tickets.update_one({"id": ticket_id}, {"$push": {"updates": {"status": status, "message": message,
                                                                    "at": now(), "by": by}},
                                              "$set": {"updated": now()}})


# ---------------------------------------------------------------- notifications (simulated service)
def notify(message, user_ids=(), roles=(), dept=None, ticket_id=None, email=False, subject=None):
    """Targeted notifications: in-app for each recipient, plus a simulated email in the outbox."""
    # Department managers only hear about their own department; other roles get everything sent to them.
    other_roles = [r for r in roles if r != "dept_manager"]
    clauses = [{"id": {"$in": [i for i in user_ids if i]}}, {"role": {"$in": other_roles}}]
    if "dept_manager" in roles and dept:
        clauses.append({"role": "dept_manager", "dept": dept})
    recipients = find("users", {"$or": clauses}) if (user_ids or roles) else []
    for u in recipients:
        insert("notifications", {"user_id": u["id"], "message": message, "ticket_id": ticket_id,
                                 "read": False, "at": now()})
        if email:
            send_email(u.get("email") or f"{u['username']}@campus.edu", subject or "CampusOps update", message)
    return len(recipients)


def send_email(to, subject, body, in_reply_to=None):
    insert("outbox", {"to": to, "subject": subject, "body": body, "at": now(), "in_reply_to": in_reply_to})


# ---------------------------------------------------------------- campus lookups
def rooms_by_id():
    return {r["id"]: r for r in find("rooms")}


def room_list_for_ai():
    return [f"{r['code']}: {r['name']} ({r['building']})" for r in find("rooms", sort=[("code", 1)])]


def dept_of_room(room):
    b = find_one("buildings", {"code": room["building"]}) if room else None
    return b.get("dept") if b else None


def on_shift(tech, at=None):
    at = at or now_dt()
    start, end = tech["shift"]
    h = at.hour
    return start <= h < end if start < end else (h >= start or h < end)


def hours_until_shift(tech, at=None):
    at = at or now_dt()
    return 0 if on_shift(tech, at) else (tech["shift"][0] - at.hour) % 24


def workload(tech_id):
    return sum(PRIORITY_WEIGHT.get(t["priority"], 1)
               for t in find("tickets", {"assigned_to": tech_id, "status": {"$in": ["Assigned", "In Progress",
                                                                                    "Blocked"]}}))


# ---------------------------------------------------------------- AI extraction with rule-based fallback
KEYWORDS = [
    ("Electrical", r"power|electric|light|bulb|tube|fan|switch|socket|plug|spark|short circuit|mcb|fuse|ups"),
    ("Network", r"wi-?fi|internet|network|router|lan|access point|no signal|connectivity"),
    ("HVAC", r"\bac\b|air ?con|cooling|heater|ventilation|hot room|temperature"),
    ("Plumbing", r"water|tap|leak|pipe|toilet|flush|drain|washroom|bathroom|sink|flood"),
    ("IT Equipment", r"projector|computer|pc|printer|monitor|keyboard|mouse|smart ?board|hdmi"),
    ("Carpentry", r"door|lock|window|bench|desk|chair|table|cupboard|hinge"),
    ("Cleanliness", r"dirty|garbage|clean|smell|dust|trash|pest|cockroach"),
    ("Security", r"theft|stolen|cctv|intruder|security|unsafe|fight"),
]


def rule_extract(text):
    q = text.lower()
    category = next((c for c, pat in KEYWORDS if re.search(pat, q)), "Other")
    room = None
    for r in find("rooms"):
        code = r["code"].lower()
        if code in q or code.replace("-", " ") in q or code.replace("-", "") in q.replace(" ", ""):
            room = r
            break
    if not room:
        m = re.search(r"block\s*([a-z])\W*(?:room\s*)?(\d{3})", q) or re.search(r"\b([a-z])\s*-?\s*(\d{3})\b", q)
        if m:
            room = find_one("rooms", {"code": f"{m.group(1).upper()}-{m.group(2)}"})
    safety = bool(re.search(r"spark|smoke|fire|shock|short circuit|flood|gas|injur|danger", q))
    severity = 5 if safety else 4 if re.search(r"no power|power cut|outage|exam|whole|entire|all ", q) else \
        3 if re.search(r"not working|broken|stopped|leak", q) else 2
    return {"category": category, "title": text.strip().split(".")[0][:70], "room_code": room["code"] if room else "",
            "location_text": "", "severity": severity, "severity_reason": "Estimated from keywords (AI offline).",
            "assets": [], "room_unusable": bool(re.search(r"no power|power cut|flood|outage", q)) and room is not None,
            "safety_risk": safety, "summary": text.strip()[:160], "missing": [] if room else ["location"],
            "clarification_question": "" if room else "Where exactly is this? Please share the block and room number."}


def extract(text):
    try:
        data = ai.extract_issue(text, room_list_for_ai())
        data["ai"] = True
    except ai.AIUnavailable:
        data = rule_extract(text)
        data["ai"] = False
    if data["category"] not in CATEGORIES:
        data["category"] = "Other"
    return data


# ---------------------------------------------------------------- priority & SLA (deterministic)
def compute_priority(severity, exam_soon=False, bookings_affected=0, reporters=1, safety=False, major=False):
    score = severity * 15 + (25 if exam_soon else 0) + min(bookings_affected, 5) * 3 + \
        min(reporters - 1, 10) * 3 + (20 if safety else 0) + (10 if major else 0)
    level = "P1" if score >= 80 else "P2" if score >= 60 else "P3" if score >= 35 else "P4"
    return level, score


def reprioritise(ticket_id, reason=None):
    t = find_one("tickets", {"id": ticket_id})
    impact = booking_impact(t.get("room_id"))
    group = find_one("incident_groups", {"id": t.get("group_id")}) if t.get("group_id") else None
    level, score = compute_priority(t["severity"], impact["exam_soon"], impact["count"], len(t["reporters"]),
                                    t.get("safety"), bool(group and group.get("major")))
    if score == t.get("priority_score"):
        return
    changes = {"priority": level, "priority_score": score}
    if PRIORITY_WEIGHT[level] > PRIORITY_WEIGHT[t["priority"]]:
        # Tighter SLA from the original report time when priority goes up
        changes["sla_hours"] = SLA_HOURS[level]
        changes["sla_due"] = (datetime.strptime(t["created"], FMT) + timedelta(hours=SLA_HOURS[level])).strftime(FMT)
        log(ticket_id, t["status"], f"Priority raised {t['priority']} → {level}" + (f" ({reason})" if reason else ""))
    update("tickets", {"id": ticket_id}, changes)


# ---------------------------------------------------------------- state machine
def transition(ticket_id, to, by="System", message=None, expect=None, **fields):
    """Validated, idempotent state change. Repeating the same change is a no-op; illegal moves raise."""
    t = find_one("tickets", {"id": ticket_id})
    if not t:
        raise InvalidTransition("Ticket not found.")
    current = t["status"]
    if current == to and to != "Assigned":
        return False  # repeated event: already in that state
    if expect and current not in expect:
        raise InvalidTransition(f"{t['ref']} is {current}, expected {' or '.join(expect)}.")
    if to not in TRANSITIONS[current]:
        raise InvalidTransition(f"{t['ref']}: cannot go from {current} to {to}.")
    changes = dict(fields, status=to, updated=now())
    if to == "Resolved":
        changes["resolved_at"] = now()
    # Compare-and-set on the old status so two concurrent clicks can't both apply
    res = db.tickets.update_one({"id": ticket_id, "status": current}, {"$set": changes})
    if res.modified_count == 0:
        return False
    log(ticket_id, to, message or f"Status changed to {to}.", by)
    reporters = [r for r in t["reporters"] if r]
    notify(f"{t['ref']} ({t['title']}): {message or to}", user_ids=reporters, ticket_id=ticket_id)
    if to == "Resolved":
        on_resolved(find_one("tickets", {"id": ticket_id}))
    return True


def on_resolved(t):
    # Consume reserved parts, record maintenance history, free the room
    for p in t.get("parts", []):
        if p["status"] == "Reserved":
            db.inventory.update_one({"id": p["item_id"]}, {"$inc": {"stock": -p["qty"], "reserved": -p["qty"]}})
    parts = [dict(p, status="Used") if p["status"] == "Reserved" else p for p in t.get("parts", [])]
    db.tickets.update_one({"id": t["id"]}, {"$set": {"parts": parts}})
    insert("maintenance_history", {"room_id": t.get("room_id"), "category": t["category"], "title": t["title"],
                                   "ticket_id": t["id"], "technician_id": t.get("assigned_to"),
                                   "cost": t.get("cost_estimate") or 0, "date": now()[:10]})
    if t.get("room_id"):
        room = find_one("rooms", {"id": t["room_id"]})
        if room and room.get("maintenance_ticket") == t["id"]:
            update("rooms", {"id": room["id"]}, {"status": "available", "maintenance_ticket": None})
            log(t["id"], "Resolved", f"Room {room['code']} is available again.")


# ---------------------------------------------------------------- assignment
def pick_technician(category, exclude=()):
    skill = SKILL_FOR.get(category, "general")
    techs = [t for t in find("users", {"role": "technician", "available": True}) if t["id"] not in exclude]
    skilled = [t for t in techs if skill in t["skills"]]
    if not skilled and skill in ("general", "housekeeping", "security"):
        skilled = techs
    if not skilled:
        return None
    return min(skilled, key=lambda t: (hours_until_shift(t), workload(t["id"]) >= TECH_CAPACITY,
                                       workload(t["id"]), t["id"]))


def auto_assign(ticket_id, exclude=(), reason=None):
    t = find_one("tickets", {"id": ticket_id})
    tech = pick_technician(t["category"], exclude)
    if not tech:
        update("tickets", {"id": ticket_id}, {"blocked_reason": f"No available technician with "
                                                                f"{SKILL_FOR[t['category']]} skill"})
        log(ticket_id, t["status"], "Waiting: no available technician with the required skill. Escalated.")
        notify(f"{t['ref']} has no available {SKILL_FOR[t['category']]} technician.", roles=["facilities_admin"],
               ticket_id=ticket_id, email=True, subject=f"[{t['ref']}] Needs manual assignment")
        return None
    wait = hours_until_shift(tech)
    note = f"Assigned to {tech['name']} ({', '.join(tech['skills'])}; load {workload(tech['id'])})"
    if wait:
        note += f"; starts shift in {wait}h"
    if reason:
        note = f"{reason}. {note}"
    if t["status"] == "Blocked":  # keep it blocked (still waiting on parts/approval), just change the owner
        update("tickets", {"id": ticket_id}, {"assigned_to": tech["id"]})
        log(ticket_id, "Blocked", note)
    else:
        transition(ticket_id, "Assigned", message=note, assigned_to=tech["id"], blocked_reason=None)
    notify(f"New work order {t['ref']} [{t['priority']}]: {t['title']} at {t.get('room_code') or t['location_text']}",
           user_ids=[tech["id"]], ticket_id=ticket_id)
    return tech


def set_technician_availability(tech_id, available, by="System"):
    tech = find_one("users", {"id": tech_id, "role": "technician"})
    if not tech or tech["available"] == available:
        return 0
    update("users", {"id": tech_id}, {"available": available})
    if available:
        return 0
    moved = 0
    for t in find("tickets", {"assigned_to": tech_id, "status": {"$in": ["Assigned", "In Progress", "Blocked"]}}):
        if t["status"] == "Blocked":
            new = pick_technician(t["category"], exclude=[tech_id])
            if new:
                update("tickets", {"id": t["id"]}, {"assigned_to": new["id"]})
                log(t["id"], "Blocked", f"Reassigned to {new['name']} because {tech['name']} is unavailable.", by)
                notify(f"Work order {t['ref']} reassigned to you (blocked: {t.get('blocked_reason')}).",
                       user_ids=[new["id"]], ticket_id=t["id"])
                moved += 1
            continue
        if auto_assign(t["id"], exclude=[tech_id], reason=f"Reassigned: {tech['name']} is unavailable"):
            moved += 1
    notify(f"{tech['name']} marked unavailable; {moved} work order(s) reassigned automatically.",
           roles=["facilities_admin"])
    return moved


# ---------------------------------------------------------------- rooms & bookings
def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def conflicts(room_id, day, start, end, ignore_id=None):
    return [b for b in find("bookings", {"room_id": room_id, "date": day, "status": "Active"})
            if b["id"] != ignore_id and overlaps(start, end, b["start"], b["end"])]


SIMILAR_TYPES = {"classroom": ["classroom", "seminar", "exam_hall"], "exam_hall": ["exam_hall", "seminar"],
                 "seminar": ["seminar", "exam_hall", "classroom"], "lab": ["lab"]}


def alternatives(room, day, start, end, need=None, limit=4):
    need = need or room["capacity"]
    out = []
    for r in find("rooms", {"type": {"$in": SIMILAR_TYPES.get(room["type"], [room["type"]])},
                            "status": "available", "id": {"$ne": room["id"]}}):
        if r["capacity"] >= need and not conflicts(r["id"], day, start, end):
            out.append(r)
    out.sort(key=lambda r: (r["building"] != room["building"], r["type"] != room["type"], r["capacity"]))
    return out[:limit]


def booking_impact(room_id, hours=48):
    if not room_id:
        return {"count": 0, "exam_soon": False}
    today = date.today().isoformat()
    until = (now_dt() + timedelta(hours=hours)).strftime(FMT)
    items = [b for b in find("bookings", {"$or": [{"room_id": room_id}, {"relocated_from": room_id}],
                                          "status": "Active", "date": {"$gte": today}})
             if f"{b['date']} {b['end']}" >= now() and f"{b['date']} {b['start']}" <= until]
    return {"count": len(items), "exam_soon": any(b["kind"] == "exam" for b in items)}


def make_room_unavailable(room, ticket):
    """Takes a room out of service and moves every booking in the next 7 days to the best free alternative."""
    if room["status"] == "maintenance":
        return []
    update("rooms", {"id": room["id"]}, {"status": "maintenance", "maintenance_ticket": ticket["id"]})
    until = (date.today() + timedelta(days=7)).isoformat()
    moved = []
    for b in find("bookings", {"room_id": room["id"], "status": "Active", "date": {"$gte": date.today().isoformat(),
                                                                                  "$lte": until}},
                  sort=[("date", 1), ("start", 1)]):
        if f"{b['date']} {b['end']}" < now():
            continue
        alts = alternatives(room, b["date"], b["start"], b["end"], b.get("attendees"))
        if alts:
            alt = alts[0]
            update("bookings", {"id": b["id"]}, {"room_id": alt["id"], "relocated_from": room["id"],
                                                 "relocation_reason": f"{room['code']} unavailable ({ticket['ref']})"})
            msg = (f"{'EXAM' if b['kind'] == 'exam' else 'Class'} '{b['title']}' on {b['date']} {b['start']}-{b['end']} "
                   f"moved from {room['code']} to {alt['code']} ({alt['name']}) because of {ticket['ref']}.")
            notify(msg, user_ids=[b["owner_id"]], roles=["dept_manager"], dept=b.get("dept"),
                   ticket_id=ticket["id"], email=b["kind"] == "exam", subject="Room change: " + b["title"])
            moved.append({"booking_id": b["id"], "title": b["title"], "kind": b["kind"], "date": b["date"],
                          "time": f"{b['start']}-{b['end']}", "to": alt["code"]})
        else:
            update("bookings", {"id": b["id"]}, {"conflict": f"No free room found; {room['code']} unavailable"})
            notify(f"Could not relocate '{b['title']}' on {b['date']} {b['start']}: no free room. Action needed.",
                   roles=["facilities_admin"], ticket_id=ticket["id"], email=True,
                   subject="Unresolved room conflict")
            moved.append({"booking_id": b["id"], "title": b["title"], "kind": b["kind"], "date": b["date"],
                          "time": f"{b['start']}-{b['end']}", "to": None})
    log(ticket["id"], ticket["status"], f"Room {room['code']} taken out of service; {len(moved)} booking(s) "
                                        f"in the next 7 days handled.")
    update("tickets", {"id": ticket["id"]}, {"affected_bookings": moved})
    return moved


def create_booking(room_id, day, start, end, title, kind, owner, attendees=None, dept=None):
    """Returns (booking_id, None) or (None, {'conflicts': [...], 'alternatives': [...], 'error': str})."""
    room = find_one("rooms", {"id": room_id})
    if not room:
        return None, {"error": "Room not found."}
    if not (start < end and "07:00" <= start and end <= "21:00"):
        return None, {"error": "Bookings must be between 07:00 and 21:00 and end after they start."}
    if day < date.today().isoformat():
        return None, {"error": "Cannot book a date in the past."}
    if attendees and attendees > room["capacity"]:
        return None, {"error": f"{room['code']} holds {room['capacity']}, not {attendees}.",
                      "alternatives": alternatives(room, day, start, end, attendees)}
    if room["status"] == "maintenance":
        return None, {"error": f"{room['code']} is under maintenance.",
                      "alternatives": alternatives(room, day, start, end, attendees)}
    clash = conflicts(room_id, day, start, end)
    if clash:
        return None, {"error": "Double booking: the room is already taken.", "conflicts": clash,
                      "alternatives": alternatives(room, day, start, end, attendees)}
    bid = insert("bookings", {"room_id": room_id, "date": day, "start": start, "end": end, "title": title,
                              "kind": kind, "owner_id": owner["id"], "dept": dept or owner.get("dept"),
                              "attendees": attendees or room["capacity"], "status": "Active", "created": now()})
    return bid, None


# ---------------------------------------------------------------- intake pipeline
def intake(text, reporter, source, room_code=None, category=None, event_key=None, reporter_email=None):
    """Turns a natural-language report into a work order. Returns the ticket that now represents it."""
    if event_key and not once(f"intake:{event_key}"):
        existing = find_one("tickets", {"event_key": event_key})
        return existing, "repeat"  # repeated email/webhook: handled before, nothing new created

    data = extract(text)
    if category in CATEGORIES:
        data["category"] = category
    if room_code:
        data["room_code"] = room_code
    room = find_one("rooms", {"code": (data.get("room_code") or "").upper()}) if data.get("room_code") else None
    reporter_id = reporter["id"] if reporter else None
    base = {
        "title": data["title"][:80] or text[:60], "description": text, "category": data["category"],
        "severity": data["severity"], "severity_reason": data["severity_reason"], "assets": data["assets"],
        "safety": bool(data["safety_risk"]), "room_unusable": bool(data["room_unusable"]),
        "ai_summary": data["summary"], "ai_used": data["ai"], "source": source, "reporter_id": reporter_id,
        "reporter_email": reporter_email, "reporters": [reporter_id] if reporter_id else [], "report_count": 1,
        "room_id": room["id"] if room else None, "room_code": room["code"] if room else None,
        "building": room["building"] if room else None, "dept": dept_of_room(room),
        "location_text": data.get("location_text") or (room["name"] if room else ""),
        "assigned_to": None, "parts": [], "cost_estimate": None, "cost_approved": False, "blocked_reason": None,
        "group_id": None, "duplicate_of": None, "escalation_level": 0, "sla_state": "On track",
        "event_key": event_key, "created": now(), "updated": now(), "resolved_at": None, "affected_bookings": [],
        "updates": [],
    }

    # 1) Missing location → ask the reporter (clarification request)
    if not room:
        question = data.get("clarification_question") or "Where exactly is this? Please share the block and room."
        tid = insert("tickets", dict(base, status="New", priority="P4", priority_score=0, sla_hours=None,
                                     sla_due=None, clarification=question))
        ref = f"CO-{tid:04d}"
        update("tickets", {"id": tid}, {"ref": ref})
        log(tid, "New", f"Report received via {source}.")
        transition(tid, "Needs Info", message=f"More details needed: {question}")
        if reporter_email:
            send_email(reporter_email, f"[{ref}] We need a bit more information",
                       f"Thanks for your report.\n\n{question}\n\nReply to this email keeping [{ref}] in the subject.")
        return find_one("tickets", {"id": tid}), "needs_info"

    # 2) Duplicate: same room + category still open recently → merge into the existing work order
    dup = db.tickets.find_one({"room_id": room["id"], "category": data["category"], "status": {"$in": OPEN_STATES},
                               "created": {"$gte": ago(DUPLICATE_WINDOW_H)}}, {"_id": 0}, sort=[("id", 1)])
    if dup:
        tid = insert("tickets", dict(base, status="Duplicate", priority=dup["priority"], priority_score=0,
                                     sla_hours=None, sla_due=None, duplicate_of=dup["id"]))
        update("tickets", {"id": tid}, {"ref": f"CO-{tid:04d}"})
        log(tid, "Duplicate", f"Merged into {dup['ref']} (same {data['category'].lower()} issue in {room['code']}).")
        add = {"duplicates": tid}
        if reporter_id:
            add["reporters"] = reporter_id
        db.tickets.update_one({"id": dup["id"]}, {"$inc": {"report_count": 1}, "$addToSet": add})
        if reporter_id:
            notify(f"Your report was merged into {dup['ref']} ({dup['title']}), which is already "
                   f"{dup['status'].lower()}. You'll get its updates.", user_ids=[reporter_id], ticket_id=dup["id"])
        log(dup["id"], dup["status"], f"Another report merged ({dup['report_count'] + 1} reports): "
                                      f"\"{text[:80]}\" via {source}.")
        reprioritise(dup["id"], "more people affected")
        correlate(find_one("tickets", {"id": dup["id"]}))
        return find_one("tickets", {"id": dup["id"]}), "duplicate"

    # 3) New work order: impact, priority, SLA
    impact = booking_impact(room["id"])
    level, score = compute_priority(data["severity"], impact["exam_soon"], impact["count"], 1, data["safety_risk"])
    tid = insert("tickets", dict(base, status="New", priority=level, priority_score=score,
                                 sla_hours=SLA_HOURS[level],
                                 sla_due=(now_dt() + timedelta(hours=SLA_HOURS[level])).strftime(FMT)))
    ref = f"CO-{tid:04d}"
    update("tickets", {"id": tid}, {"ref": ref})
    log(tid, "New", f"Report received via {source}.")
    why = [f"severity {data['severity']}/5"] + (["exam in this room within 48h"] if impact["exam_soon"] else []) + \
          ([f"{impact['count']} booking(s) affected"] if impact["count"] else []) + (["safety risk"] if data["safety_risk"] else [])
    transition(tid, "Triaged", message=f"{'AI' if data['ai'] else 'Rules'} triage: {data['category']}, "
                                       f"{level} ({', '.join(why)}). {data['severity_reason']}")
    t = find_one("tickets", {"id": tid})
    if data["room_unusable"] or (data["severity"] >= 4 and data["category"] in ("Electrical", "Plumbing", "HVAC")
                                 and room["type"] in ("classroom", "exam_hall", "lab", "seminar")):
        make_room_unavailable(room, t)
    correlate(t)
    auto_assign(tid)
    if level == "P1":
        notify(f"P1 incident {ref}: {t['title']} in {room['code']}.", roles=["facilities_admin", "dept_manager"],
               dept=t["dept"], ticket_id=tid, email=True, subject=f"[{ref}] P1 incident")
    return find_one("tickets", {"id": tid}), "created"


def correlate(t):
    """Links open complaints of the same kind in the same building (e.g. Wi-Fi down across a hostel)."""
    if not t.get("building"):
        return
    related = find("tickets", {"building": t["building"], "category": t["category"], "status": {"$in": OPEN_STATES},
                               "created": {"$gte": ago(CORRELATION_WINDOW_H)}})
    if len(related) < 2:
        return
    group_id = next((r["group_id"] for r in related if r.get("group_id")), None)
    reports = sum(r.get("report_count", 1) for r in related)
    rooms = sorted({r["room_code"] for r in related if r.get("room_code")})
    major = reports >= 4 or len(rooms) >= 3
    if not group_id:
        group_id = insert("incident_groups", {"building": t["building"], "category": t["category"], "major": False,
                                              "summary": "", "created": now()})
    db.tickets.update_many({"id": {"$in": [r["id"] for r in related]}}, {"$set": {"group_id": group_id}})
    group = find_one("incident_groups", {"id": group_id})
    texts = [f"{r['ref']} {r.get('room_code')}: {r['description'][:120]} ({r.get('report_count', 1)} reports)"
             for r in related]
    try:
        summary = ai.summarize_incident(texts)
    except ai.AIUnavailable:
        summary = (f"{reports} reports of {t['category'].lower()} problems across {len(rooms)} room(s) in building "
                   f"{t['building']} ({', '.join(rooms)}) within 24h; likely a shared cause.")
    update("incident_groups", {"id": group_id}, {"summary": summary, "rooms": rooms, "reports": reports,
                                                  "ticket_ids": [r["id"] for r in related], "major": major,
                                                  "updated": now()})
    if major and not group.get("major") and once(f"major:{group_id}"):
        notify(f"Major incident: {summary}", roles=["facilities_admin", "dept_manager"], dept=t.get("dept"),
               email=True, subject=f"Major incident in building {t['building']}")
        for r in related:
            log(r["id"], r["status"], f"Linked to major incident #{group_id} ({reports} reports, {len(rooms)} rooms).")
            reprioritise(r["id"], "part of a major incident")


def clarify(ticket_id, extra_text, by_user=None):
    """Reporter supplies the missing details; the report is re-run through the pipeline."""
    t = find_one("tickets", {"id": ticket_id})
    if not t or t["status"] != "Needs Info":
        return t, "not_waiting"
    data = extract(t["description"] + "\nLocation / details: " + extra_text)
    room = find_one("rooms", {"code": (data.get("room_code") or "").upper()}) if data.get("room_code") else None
    if not room:
        log(ticket_id, "Needs Info", f"Reporter replied: \"{extra_text[:80]}\" — still no recognisable room.")
        return t, "still_missing"
    transition(ticket_id, "Closed", message=f"Details received; continued as a full work order.")
    reporter = find_one("users", {"id": t["reporter_id"]}) if t.get("reporter_id") else None
    new, outcome = intake(t["description"] + " " + extra_text, reporter, t["source"], room_code=room["code"],
                          reporter_email=t.get("reporter_email"))
    log(ticket_id, "Closed", f"Continued as {new['ref']}.")
    return new, outcome


# ---------------------------------------------------------------- parts & approvals
def reserve_stock(item_id, qty):
    """Atomically reserves qty units if that many are free (compare-and-set on stock and reserved)."""
    for _ in range(5):
        item = find_one("inventory", {"id": item_id})
        if not item or item["stock"] - item["reserved"] < qty:
            return None
        got = db.inventory.find_one_and_update({"id": item_id, "stock": item["stock"], "reserved": item["reserved"]},
                                               {"$inc": {"reserved": qty}}, return_document=ReturnDocument.AFTER)
        if got:
            return got
    return None


def request_part(ticket_id, item_id, qty, by):
    t = find_one("tickets", {"id": ticket_id})
    item = find_one("inventory", {"id": item_id})
    if not t or not item or qty < 1:
        raise InvalidTransition("Unknown work order or part.")
    reserved = reserve_stock(item_id, qty)
    if reserved:
        db.tickets.update_one({"id": ticket_id}, {"$push": {"parts": {"item_id": item_id, "name": item["name"],
                                                                      "qty": qty, "status": "Reserved"}}})
        log(ticket_id, t["status"], f"Reserved {qty} × {item['name']}.", by["name"])
        if item["stock"] - item["reserved"] - qty <= item["reorder_level"]:
            notify(f"Low stock: {item['name']} ({item['stock'] - item['reserved'] - qty} free).", roles=["procurement"])
        return "reserved"
    free = item["stock"] - item["reserved"]
    db.tickets.update_one({"id": ticket_id}, {"$push": {"parts": {"item_id": item_id, "name": item["name"],
                                                                  "qty": qty, "status": "Waiting"}}})
    reason = f"Waiting for {qty} × {item['name']} (only {free} free in stock)"
    if t["status"] in ("Assigned", "In Progress"):
        transition(ticket_id, "Blocked", by["name"], reason, blocked_reason=reason)
    if not find_one("approvals", {"type": "purchase", "item_id": item_id, "status": "Pending"}):
        order_qty = qty - free + item["reorder_level"]
        insert("approvals", {"type": "purchase", "item_id": item_id, "item": item["name"], "qty": order_qty,
                             "amount": order_qty * item["unit_cost"], "ticket_id": ticket_id,
                             "requested_by": by["id"], "requested_name": by["name"], "status": "Pending",
                             "reason": f"Out of stock for {t['ref']}", "created": now()})
        notify(f"Purchase approval needed: {order_qty} × {item['name']} (₹{order_qty * item['unit_cost']:,}) "
               f"for {t['ref']}.", roles=["procurement"], email=True, subject="Purchase approval request")
    return "blocked"


def set_cost(ticket_id, amount, by):
    t = find_one("tickets", {"id": ticket_id})
    update("tickets", {"id": ticket_id}, {"cost_estimate": amount})
    log(ticket_id, t["status"], f"Repair cost estimated at ₹{amount:,}.", by["name"])
    if amount > COSTLY_REPAIR_LIMIT and not t.get("cost_approved"):
        reason = f"Awaiting approval for ₹{amount:,} repair"
        if t["status"] in ("Assigned", "In Progress"):
            transition(ticket_id, "Blocked", by["name"], reason, blocked_reason=reason)
        if not find_one("approvals", {"type": "costly_repair", "ticket_id": ticket_id, "status": "Pending"}):
            insert("approvals", {"type": "costly_repair", "ticket_id": ticket_id, "amount": amount,
                                 "dept": t.get("dept"), "requested_by": by["id"], "requested_name": by["name"],
                                 "status": "Pending", "reason": f"{t['ref']}: {t['title']}", "created": now()})
            notify(f"Costly repair approval needed for {t['ref']} (₹{amount:,}).",
                   roles=["dept_manager"] if t.get("dept") else ["facilities_admin"], dept=t.get("dept"),
                   ticket_id=ticket_id, email=True, subject="Repair approval request")
        return "approval_needed"
    return "ok"


APPROVER_ROLES = {"purchase": ["procurement"], "costly_repair": ["dept_manager", "facilities_admin"],
                  "override": ["facilities_admin", "admin"]}


def can_decide(a, user):
    if user["role"] not in APPROVER_ROLES[a["type"]]:
        return False, "Your role cannot approve this type of request."
    if a["requested_by"] == user["id"]:
        return False, "You cannot approve your own request (separation of duties)."
    if a["type"] == "costly_repair" and user["role"] == "dept_manager" and a.get("dept") != user.get("dept"):
        return False, "Only the manager of the affected department can approve this."
    return True, ""


def revalidate_and_execute(a, user):
    """Re-checks the constraints at decision time (things may have changed since the request)."""
    if a["type"] == "purchase":
        item = find_one("inventory", {"id": a["item_id"]})
        waiting = sum(p["qty"] for t in find("tickets", {"status": {"$in": OPEN_STATES}})
                      for p in t.get("parts", []) if p["item_id"] == a["item_id"] and p["status"] == "Waiting")
        if item["stock"] - item["reserved"] >= waiting and item["stock"] - item["reserved"] > item["reorder_level"]:
            return False, "Stock has since been replenished; purchase no longer needed."
        db.inventory.update_one({"id": a["item_id"]}, {"$inc": {"on_order": a["qty"]}})
        insert("purchase_orders", {"approval_id": a["id"], "item_id": a["item_id"], "item": a["item"], "qty": a["qty"],
                                   "amount": a["amount"], "status": "Ordered", "created": now()})
        return True, f"Purchase order placed for {a['qty']} × {a['item']}."
    if a["type"] == "costly_repair":
        t = find_one("tickets", {"id": a["ticket_id"]})
        if not t or t["status"] not in OPEN_STATES:
            return False, "The work order is no longer open."
        update("tickets", {"id": t["id"]}, {"cost_approved": True})
        log(t["id"], t["status"], f"₹{a['amount']:,} repair approved by {user['name']}.", user["name"])
        if t["status"] == "Blocked" and "approval" in (t.get("blocked_reason") or "") and not waiting_parts(t):
            transition(t["id"], "In Progress", user["name"], "Approval received; work can continue.",
                       blocked_reason=None)
        return True, "Repair approved."
    if a["type"] == "override" and a["kind"] == "booking":
        room = find_one("rooms", {"id": a["room_id"]})
        if room["status"] != "available":
            return False, f"{room['code']} is now under maintenance."
        clash = conflicts(a["room_id"], a["date"], a["start"], a["end"])
        moves = []
        for b in clash:
            if b["kind"] == "exam":
                return False, f"Cannot displace an exam ({b['title']})."
            alts = alternatives(room, b["date"], b["start"], b["end"], b.get("attendees"))
            if not alts:
                return False, f"No alternative room for '{b['title']}'."
            moves.append((b, alts[0]))
        for b, alt in moves:
            update("bookings", {"id": b["id"]}, {"room_id": alt["id"], "relocated_from": room["id"],
                                                 "relocation_reason": f"Override approved for {a['title']}"})
            notify(f"'{b['title']}' on {b['date']} {b['start']} moved to {alt['code']} (approved override).",
                   user_ids=[b["owner_id"]])
        owner = find_one("users", {"id": a["requested_by"]})
        bid, err = create_booking(a["room_id"], a["date"], a["start"], a["end"], a["title"], a.get("booking_kind",
                                  "event"), owner, a.get("attendees"))
        if err:
            return False, err["error"]
        return True, f"Booking created; {len(moves)} booking(s) moved."
    if a["type"] == "override" and a["kind"] == "assignment":
        tech = find_one("users", {"id": a["tech_id"]})
        t = find_one("tickets", {"id": a["ticket_id"]})
        if not tech["available"]:
            return False, f"{tech['name']} is no longer available."
        if t["status"] not in ("Triaged", "Assigned", "In Progress", "Blocked"):
            return False, "Work order is no longer assignable."
        if t["status"] == "Blocked":
            update("tickets", {"id": t["id"]}, {"assigned_to": tech["id"]})
            log(t["id"], "Blocked", f"Override: assigned to {tech['name']}.", user["name"])
        else:
            transition(t["id"], "Assigned", user["name"], f"Override: assigned to {tech['name']} (approved).",
                       assigned_to=tech["id"])
        notify(f"Work order {t['ref']} assigned to you (override).", user_ids=[tech["id"]], ticket_id=t["id"])
        return True, "Override assignment applied."
    return False, "Unknown request type."


def decide(approval_id, user, approve, note=""):
    a = find_one("approvals", {"id": approval_id})
    if not a or a["status"] != "Pending":
        return False, "This request was already decided."  # repeated click is harmless
    ok, why = can_decide(a, user)
    if not ok:
        return False, why
    # Claim the decision atomically so two approvers can't both act
    if not db.approvals.update_one({"id": approval_id, "status": "Pending"},
                                   {"$set": {"status": "Deciding"}}).modified_count:
        return False, "This request was already decided."
    if not approve:
        update("approvals", {"id": approval_id}, {"status": "Rejected", "decided_by": user["id"],
                                                  "decided_name": user["name"], "decided_at": now(), "note": note})
        notify(f"Your {a['type'].replace('_', ' ')} request was rejected by {user['name']}. {note}",
               user_ids=[a["requested_by"]])
        return True, "Rejected."
    ok, result = revalidate_and_execute(a, user)
    update("approvals", {"id": approval_id}, {"status": "Executed" if ok else "Failed", "decided_by": user["id"],
                                              "decided_name": user["name"], "decided_at": now(), "note": note,
                                              "result": result})
    notify(f"Your {a['type'].replace('_', ' ')} request: {result}", user_ids=[a["requested_by"]])
    return ok, result


def waiting_parts(t):
    return [p for p in t.get("parts", []) if p["status"] == "Waiting"]


def receive_order(po_id, user):
    po = find_one("purchase_orders", {"id": po_id})
    if not po or po["status"] != "Ordered":
        return 0
    if not db.purchase_orders.update_one({"id": po_id, "status": "Ordered"},
                                         {"$set": {"status": "Received", "received_at": now()}}).modified_count:
        return 0
    db.inventory.update_one({"id": po["item_id"]}, {"$inc": {"stock": po["qty"], "on_order": -po["qty"]}})
    return unblock_waiting(po["item_id"], user)


def unblock_waiting(item_id, user):
    """After stock arrives, reserve parts for blocked work orders (highest priority first) and resume them."""
    resumed = 0
    blocked = sorted(find("tickets", {"status": {"$in": OPEN_STATES}, "parts": {"$elemMatch": {"item_id": item_id,
                                                                                            "status": "Waiting"}}}),
                     key=lambda t: -t["priority_score"])
    for t in blocked:
        for i, p in enumerate(t["parts"]):
            if p["item_id"] != item_id or p["status"] != "Waiting":
                continue
            got = reserve_stock(item_id, p["qty"])
            if got:
                db.tickets.update_one({"id": t["id"]}, {"$set": {f"parts.{i}.status": "Reserved"}})
                log(t["id"], t["status"], f"{p['qty']} × {p['name']} arrived and reserved.", user["name"])
        t = find_one("tickets", {"id": t["id"]})
        approved = (t.get("cost_estimate") or 0) <= COSTLY_REPAIR_LIMIT or t.get("cost_approved")
        if t["status"] == "Blocked" and not waiting_parts(t) and approved:
            transition(t["id"], "In Progress", user["name"], "Parts available; work resumed.", blocked_reason=None)
            notify(f"Parts arrived for {t['ref']}; you can continue.", user_ids=[t["assigned_to"]],
                   ticket_id=t["id"])
            resumed += 1
    return resumed


# ---------------------------------------------------------------- SLA tracking & escalation
def sla_check():
    """Runs every minute (and on dashboard loads). Each escalation level fires once per work order."""
    n = now_dt()
    for t in find("tickets", {"status": {"$in": ACTIVE_STATES}, "sla_due": {"$ne": None}}):
        created, due = datetime.strptime(t["created"], FMT), datetime.strptime(t["sla_due"], FMT)
        frac = (n - created) / max(due - created, timedelta(minutes=1))
        state = "Breached" if n > due else "At risk" if frac >= 0.75 else "On track"
        if state != t.get("sla_state"):
            update("tickets", {"id": t["id"]}, {"sla_state": state})
        if state == "At risk" and once(f"sla-warn:{t['id']}"):
            notify(f"{t['ref']} is at risk of missing its {t['priority']} SLA (due {t['sla_due']}).",
                   user_ids=[t["assigned_to"]] if t.get("assigned_to") else [], roles=["facilities_admin"],
                   ticket_id=t["id"])
        if state == "Breached" and t["escalation_level"] < 1 and once(f"sla-l1:{t['id']}"):
            update("tickets", {"id": t["id"]}, {"escalation_level": 1})
            log(t["id"], t["status"], f"SLA breached (due {t['sla_due']}). Escalated to department manager and "
                                      f"facilities administrator.")
            notify(f"SLA breached: {t['ref']} {t['title']} ({t['priority']}).", roles=["facilities_admin", "dept_manager"],
                   dept=t.get("dept"), ticket_id=t["id"], email=True, subject=f"[{t['ref']}] SLA breached")
        overdue = (n - due).total_seconds() / 3600
        if state == "Breached" and t["escalation_level"] < 2 and overdue >= t["sla_hours"] * 0.5 and \
                once(f"sla-l2:{t['id']}"):
            update("tickets", {"id": t["id"]}, {"escalation_level": 2})
            log(t["id"], t["status"], "Still unresolved long after SLA. Escalated to System Administrator.")
            notify(f"Level-2 escalation: {t['ref']} {t['title']} overdue by {overdue:.0f}h.",
                   roles=["admin", "facilities_admin"], ticket_id=t["id"], email=True,
                   subject=f"[{t['ref']}] Level-2 escalation")
            if t["status"] == "Assigned" and t.get("assigned_to") and workload(t["assigned_to"]) >= TECH_CAPACITY:
                auto_assign(t["id"], exclude=[t["assigned_to"]], reason="Escalation: original technician overloaded")


# ---------------------------------------------------------------- analytics (deterministic)
def analytics():
    tickets = [t for t in find("tickets") if t["status"] != "Duplicate"]
    open_t = [t for t in tickets if t["status"] in OPEN_STATES]
    resolved = [t for t in tickets if t.get("resolved_at") and t.get("sla_due")]
    met = [t for t in resolved if t["resolved_at"] <= t["sla_due"]]
    techs = find("users", {"role": "technician"})
    rooms = find("rooms", {"type": {"$in": ["classroom", "lab", "exam_hall", "seminar"]}})
    week = [(date.today() + timedelta(days=i)).isoformat() for i in range(7)]
    booked = {}
    for b in find("bookings", {"status": "Active", "date": {"$in": week}}):
        h = (int(b["end"][:2]) * 60 + int(b["end"][3:]) - int(b["start"][:2]) * 60 - int(b["start"][3:])) / 60
        booked[b["room_id"]] = booked.get(b["room_id"], 0) + h
    history = find("maintenance_history", {"date": {"$gte": (date.today() - timedelta(days=60)).isoformat()}})
    rb = rooms_by_id()
    recurring = {}
    for h in history + [{"room_id": t.get("room_id"), "category": t["category"]} for t in open_t]:
        if h.get("room_id"):
            key = (h["room_id"], h["category"])
            recurring[key] = recurring.get(key, 0) + 1
    res_hours = [(datetime.strptime(t["resolved_at"], FMT) - datetime.strptime(t["created"], FMT)).total_seconds() / 3600
                 for t in resolved]
    return {
        "open": len(open_t),
        "by_state": {s: sum(1 for t in tickets if t["status"] == s) for s in OPEN_STATES + ["Resolved", "Closed"]},
        "by_priority": {p: sum(1 for t in open_t if t["priority"] == p) for p in SLA_HOURS},
        "by_category": {c: n for c in CATEGORIES if (n := sum(1 for t in open_t if t["category"] == c))},
        "sla_compliance": round(100 * len(met) / len(resolved)) if resolved else None,
        "resolved": len(resolved), "breached_open": sum(1 for t in open_t if t.get("sla_state") == "Breached"),
        "avg_resolution_h": round(sum(res_hours) / len(res_hours), 1) if res_hours else None,
        "tech_util": [{"name": t["name"].split()[0], "load": workload(t["id"]),
                       "pct": min(100, round(100 * workload(t["id"]) / TECH_CAPACITY)), "available": t["available"],
                       "on_shift": on_shift(t)} for t in techs],
        "room_util": sorted([{"code": r["code"], "pct": round(100 * booked.get(r["id"], 0) / (5 * 10))}
                             for r in rooms], key=lambda x: -x["pct"])[:10],
        "shortages": [i for i in find("inventory") if i["stock"] - i["reserved"] <= i["reorder_level"]],
        "recurring": sorted([{"room": rb[k[0]]["code"], "category": k[1], "count": v}
                             for k, v in recurring.items() if v >= 3 and k[0] in rb], key=lambda x: -x["count"]),
        "reports_by_source": {s: sum(1 for t in find("tickets") if t["source"] == s)
                              for s in ("Form", "AI chat", "Email", "Sensor webhook")},
    }


# ---------------------------------------------------------------- view helpers
def enrich(tickets):
    users = {u["id"]: u for u in find("users", {"role": {"$in": ["technician", "student", "faculty"]}})}
    for t in tickets:
        tech = users.get(t.get("assigned_to"))
        t["tech_name"] = tech["name"] if tech else None
        rep = users.get(t.get("reporter_id"))
        t["reporter_name"] = rep["name"] if rep else (t.get("reporter_email") or "System")
        t["step"] = STEPS.index(t["status"]) if t["status"] in STEPS else \
            (STEPS.index("In Progress") if t["status"] == "Blocked" else 0)
    return tickets


def queue(flt=None):
    """Work queue in priority order: highest score first, then the earliest SLA deadline."""
    flt = dict(flt or {})
    flt.setdefault("status", {"$in": OPEN_STATES})
    items = find("tickets", flt)
    items.sort(key=lambda t: (-t["priority_score"], t.get("sla_due") or "9999"))
    return enrich(items)


def pending_approvals(user):
    out = []
    for a in find("approvals", {"status": "Pending"}, sort=[("id", 1)]):
        ok, why = can_decide(a, user)
        a["can_decide"], a["why_not"] = ok, why
        if user["role"] in APPROVER_ROLES[a["type"]] or a["requested_by"] == user["id"]:
            out.append(a)
    return out


@bp.app_context_processor
def inject_notifications():
    user = current_user()
    if not user:
        return {}
    return {"notif_unread": count("notifications", {"user_id": user["id"], "read": False}),
            "notif_list": find("notifications", {"user_id": user["id"]}, sort=[("id", DESCENDING)], limit=8),
            "OPS_STEPS": STEPS}


# ================================================================ routes
@bp.post("/ops/notifications/read")
def notifications_read():
    user = current_user() or abort(403)
    db.notifications.update_many({"user_id": user["id"], "read": False}, {"$set": {"read": True}})
    return jsonify(ok=True)


@bp.post("/ops/report")
@roles_required("student", "faculty", "dept_manager", "facilities_admin", "technician", "admin")
def report():
    text = request.form.get("description", "").strip()
    if len(text) < 5:
        flash("Describe the problem in a few words.", "warning")
        return redirect(request.referrer or "/")
    room_code = request.form.get("room_code") or None
    t, outcome = intake(text, g.user, "Form", room_code=room_code, category=request.form.get("category") or None)
    flash({"created": f"{t['ref']} created: {t['priority']} priority, {t['status'].lower()}.",
           "duplicate": f"Merged into existing {t['ref']} — {t['report_count']} people have reported this.",
           "needs_info": f"{t['ref']}: we need more details — {t.get('clarification')}"}.get(outcome, "Received."),
          "success" if outcome != "needs_info" else "warning")
    return redirect(request.referrer or "/")


@bp.post("/ops/clarify/<int:tid>")
@roles_required("student", "faculty", "dept_manager", "facilities_admin", "technician", "admin")
def clarify_route(tid):
    t = find_one("tickets", {"id": tid}) or abort(404)
    if t.get("reporter_id") != g.user["id"] and g.user["role"] not in ("facilities_admin", "admin"):
        abort(403)
    new, outcome = clarify(tid, request.form.get("details", ""), g.user)
    flash(f"Thanks! Now tracked as {new['ref']} ({new['priority']})." if outcome in ("created", "duplicate")
          else "We still couldn't find the place. Please include the room code, e.g. H-214.",
          "success" if outcome in ("created", "duplicate") else "warning")
    return redirect(request.referrer or "/")


@bp.get("/ops/my-tickets.json")
@roles_required("student", "faculty")
def my_tickets():
    items = find("tickets", {"$or": [{"reporters": g.user["id"]}, {"reporter_id": g.user["id"]}],
                             "status": {"$ne": "Duplicate"}}, sort=[("id", DESCENDING)])
    return jsonify(tickets=enrich(items), steps=STEPS)


# ---- facilities administrator: unified operations center
@bp.route("/ops/center")
@roles_required("facilities_admin", "admin")
def center():
    sla_check()
    techs = find("users", {"role": "technician"}, sort=[("id", 1)])
    for t in techs:
        t["load"], t["on_shift"] = workload(t["id"]), on_shift(t)
    rb = rooms_by_id()
    upcoming = find("bookings", {"status": "Active", "date": {"$gte": date.today().isoformat(),
                                                                "$lte": (date.today() + timedelta(days=7)).isoformat()},
                                 "$or": [{"relocated_from": {"$ne": None, "$exists": True}},
                                         {"conflict": {"$exists": True}}]}, sort=[("date", 1), ("start", 1)])
    for b in upcoming:
        b["room_code"] = rb[b["room_id"]]["code"]
        b["from_code"] = rb[b["relocated_from"]]["code"] if b.get("relocated_from") else None
    return render_template("ops_center.html", u=g.user, queue=queue(), techs=techs,
                           groups=find("incident_groups", sort=[("id", DESCENDING)], limit=6),
                           rooms=sorted(rb.values(), key=lambda r: r["code"]), relocations=upcoming,
                           approvals=pending_approvals(g.user), stats=analytics(), inventory=find("inventory"),
                           recent_done=enrich(find("tickets", {"status": {"$in": ["Resolved", "Closed", "Duplicate"]}},
                                                   sort=[("id", DESCENDING)], limit=8)),
                           categories=CATEGORIES)


@bp.post("/ops/ticket/<int:tid>/action")
@roles_required("technician", "facilities_admin", "admin", "dept_manager")
def ticket_action(tid):
    t = find_one("tickets", {"id": tid}) or abort(404)
    action, user = request.form.get("action"), g.user
    is_owner = user["role"] == "technician" and t.get("assigned_to") == user["id"]
    staff = user["role"] in ("facilities_admin", "admin")
    try:
        if action == "start" and (is_owner or staff):
            transition(tid, "In Progress", user["name"], f"Work started by {user['name']}.", expect=["Assigned"])
        elif action == "resolve" and (is_owner or staff):
            if waiting_parts(t):
                raise InvalidTransition("Cannot resolve while parts are still awaited.")
            if (t.get("cost_estimate") or 0) > COSTLY_REPAIR_LIMIT and not t.get("cost_approved"):
                raise InvalidTransition("Cannot resolve: the costly repair has not been approved.")
            transition(tid, "Resolved", user["name"], request.form.get("note") or f"Fixed by {user['name']}.",
                       expect=["In Progress"])
        elif action == "close" and staff:
            transition(tid, "Closed", user["name"], "Verified and closed.")
        elif action == "reopen" and (staff or user["role"] == "dept_manager"):
            transition(tid, "In Progress", user["name"], request.form.get("note") or "Reopened: issue persists.",
                       expect=["Resolved"])
        elif action == "part" and (is_owner or staff):
            out = request_part(tid, int(request.form["item_id"]), int(request.form.get("qty") or 1), user)
            flash("Part reserved." if out == "reserved" else "Out of stock: work order blocked and a purchase "
                                                              "request was sent to procurement.",
                  "success" if out == "reserved" else "warning")
        elif action == "cost" and (is_owner or staff):
            out = set_cost(tid, int(request.form["amount"]), user)
            if out == "approval_needed":
                flash("Above ₹10,000: sent for manager approval; work order is blocked until approved.", "warning")
        elif action == "assign" and staff:
            tech = find_one("users", {"id": int(request.form["tech_id"]), "role": "technician"}) or abort(400)
            if SKILL_FOR[t["category"]] in tech["skills"] and tech["available"]:
                if t["status"] == "Blocked":
                    update("tickets", {"id": tid}, {"assigned_to": tech["id"]})
                    log(tid, "Blocked", f"Manually assigned to {tech['name']}.", user["name"])
                else:
                    transition(tid, "Assigned", user["name"], f"Manually assigned to {tech['name']}.",
                               assigned_to=tech["id"])
                notify(f"Work order {t['ref']} assigned to you.", user_ids=[tech["id"]], ticket_id=tid)
            else:
                insert("approvals", {"type": "override", "kind": "assignment", "ticket_id": tid,
                                     "tech_id": tech["id"], "requested_by": user["id"], "requested_name": user["name"],
                                     "status": "Pending", "created": now(),
                                     "reason": f"Assign {t['ref']} ({t['category']}) to {tech['name']} who lacks the "
                                               f"skill or is unavailable"})
                notify(f"Override approval needed: assign {t['ref']} to {tech['name']}.",
                       roles=["facilities_admin", "admin"])
                flash("That technician lacks the skill or is unavailable, so an override approval was requested "
                      "(someone else must approve it).", "warning")
        elif action == "auto" and staff:
            if not auto_assign(tid):
                flash("No technician available with that skill.", "warning")
        else:
            abort(403)
    except InvalidTransition as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("ops.center"))


@bp.post("/ops/tech/<int:tech_id>/availability")
@roles_required("technician", "facilities_admin", "admin")
def tech_availability(tech_id):
    if g.user["role"] == "technician" and g.user["id"] != tech_id:
        abort(403)
    available = request.form.get("available") == "1"
    moved = set_technician_availability(tech_id, available, g.user["name"])
    flash(f"Marked {'available' if available else 'unavailable'}" + (f"; {moved} work order(s) reassigned."
                                                                     if not available else "."), "info")
    return redirect(request.referrer or url_for("ops.center"))


@bp.post("/ops/approval/<int:aid>")
@roles_required("procurement", "dept_manager", "facilities_admin", "admin")
def approval(aid):
    ok, msg = decide(aid, g.user, request.form.get("decision") == "approve", request.form.get("note", ""))
    flash(msg, "success" if ok else "danger")
    return redirect(request.referrer or "/")


@bp.post("/ops/po/<int:po_id>/receive")
@roles_required("procurement")
def receive(po_id):
    resumed = receive_order(po_id, g.user)
    flash(f"Stock received. {resumed} blocked work order(s) resumed automatically.", "success")
    return redirect(url_for("ops.procurement"))


@bp.post("/ops/room/<int:room_id>/status")
@roles_required("facilities_admin", "admin")
def room_status(room_id):
    room = find_one("rooms", {"id": room_id}) or abort(404)
    if request.form.get("status") == "available" and room["status"] == "maintenance":
        update("rooms", {"id": room_id}, {"status": "available", "maintenance_ticket": None})
        flash(f"{room['code']} is available again.", "success")
    return redirect(url_for("ops.center"))


# ---- technician
@bp.route("/ops/technician")
@roles_required("technician")
def technician():
    me = g.user
    mine = queue({"assigned_to": me["id"], "status": {"$in": ["Assigned", "In Progress", "Blocked"]}})
    done = enrich(find("tickets", {"assigned_to": me["id"], "status": {"$in": ["Resolved", "Closed"]}},
                       sort=[("id", DESCENDING)], limit=8))
    history = {}
    for t in mine:
        if t.get("room_id"):
            history[t["id"]] = find("maintenance_history", {"room_id": t["room_id"]}, sort=[("date", DESCENDING)],
                                    limit=3)
    return render_template("technician.html", u=me, mine=mine, done=done, inventory=find("inventory"),
                           history=history, on_shift=on_shift(me), load=workload(me["id"]), capacity=TECH_CAPACITY)


# ---- department manager
@bp.route("/ops/manager")
@roles_required("dept_manager")
def manager():
    sla_check()
    dept = g.user["dept"]
    rb = rooms_by_id()
    bookings = find("bookings", {"dept": dept, "status": "Active",
                                 "date": {"$gte": date.today().isoformat(),
                                          "$lte": (date.today() + timedelta(days=7)).isoformat()}},
                    sort=[("date", 1), ("start", 1)])
    for b in bookings:
        b["room_code"] = rb[b["room_id"]]["code"]
        b["from_code"] = rb[b["relocated_from"]]["code"] if b.get("relocated_from") else None
    import academics
    return render_template("manager.html", u=g.user, queue=queue({"dept": dept}), teaching=academics.teaching_board(dept),
                           approvals=pending_approvals(g.user), bookings=bookings,
                           rooms=[r for r in rb.values() if r.get("dept") == dept], stats=analytics(),
                           decided=find("approvals", {"decided_by": g.user["id"]}, sort=[("id", DESCENDING)], limit=6))


# ---- procurement approver
@bp.route("/ops/procurement")
@roles_required("procurement")
def procurement():
    return render_template("procurement.html", u=g.user, approvals=pending_approvals(g.user),
                           orders=find("purchase_orders", sort=[("id", DESCENDING)]), inventory=find("inventory"),
                           decided=find("approvals", {"type": "purchase", "status": {"$ne": "Pending"}},
                                        sort=[("id", DESCENDING)], limit=8))


# ---- room bookings (faculty, managers, facilities)
@bp.post("/ops/booking")
@roles_required("faculty", "dept_manager", "facilities_admin")
def booking():
    f = request.form
    bid, err = create_booking(int(f["room_id"]), f["date"], f["start"], f["end"], f["title"],
                              f.get("kind", "event"), g.user, int(f.get("attendees") or 0) or None)
    if bid:
        flash(f"Booked {find_one('rooms', {'id': int(f['room_id'])})['code']} on {f['date']} {f['start']}-{f['end']}.",
              "success")
    else:
        alts = ", ".join(f"{r['code']} ({r['capacity']})" for r in err.get("alternatives", []))
        clash = "; ".join(f"'{c['title']}' {c['start']}-{c['end']}" for c in err.get("conflicts", []))
        flash(f"{err['error']} {('Clashes with ' + clash + '.') if clash else ''} "
              f"{('Free alternatives: ' + alts + '.') if alts else 'No free alternative found.'}", "danger")
    return redirect(request.referrer or "/")


@bp.post("/ops/booking/check")
@roles_required("faculty", "dept_manager", "facilities_admin")
def booking_check():
    f = request.get_json(silent=True) or {}
    room = find_one("rooms", {"id": int(f.get("room_id", 0))}) or abort(400)
    clash = conflicts(room["id"], f["date"], f["start"], f["end"])
    return jsonify(available=not clash and room["status"] == "available", status=room["status"],
                   conflicts=[{"title": c["title"], "start": c["start"], "end": c["end"], "kind": c["kind"]}
                              for c in clash],
                   alternatives=[{"id": r["id"], "code": r["code"], "name": r["name"], "capacity": r["capacity"]}
                                 for r in alternatives(room, f["date"], f["start"], f["end"],
                                                       int(f.get("attendees") or 0) or None)])


@bp.post("/ops/booking/override")
@roles_required("faculty", "dept_manager")
def booking_override():
    f = request.form
    room = find_one("rooms", {"id": int(f["room_id"])}) or abort(400)
    insert("approvals", {"type": "override", "kind": "booking", "room_id": room["id"], "room_code": room["code"],
                         "date": f["date"], "start": f["start"], "end": f["end"], "title": f["title"],
                         "booking_kind": f.get("kind", "event"), "attendees": int(f.get("attendees") or 0) or None,
                         "requested_by": g.user["id"], "requested_name": g.user["name"], "status": "Pending",
                         "reason": f.get("reason") or "Needs this specific room", "created": now()})
    notify(f"Booking override requested by {g.user['name']} for {room['code']} on {f['date']} {f['start']}.",
           roles=["facilities_admin"], email=True, subject="Booking override request")
    flash("Override request sent to the Facilities Administrator.", "info")
    return redirect(request.referrer or "/")


# ---- simulated external integrations
def secret_ok():
    return secrets.compare_digest(request.headers.get("X-Webhook-Secret", ""), INBOUND_SECRET)


@bp.post("/api/inbound-email")
def inbound_email():
    """Simulated email provider webhook. Message-ID makes redelivery safe (idempotent)."""
    if not secret_ok():
        abort(401)
    m = request.get_json(silent=True) or {}
    msg_id, sender = m.get("message_id") or "", (m.get("from") or "").strip().lower()
    subject, body = m.get("subject") or "", m.get("body") or ""
    if not msg_id or not body:
        return jsonify(error="message_id and body are required"), 400
    ref = re.search(r"\[(CO-\d{4})\]", subject)
    if ref:  # reply to a clarification request
        t = find_one("tickets", {"ref": ref.group(1)})
        if not once(f"email:{msg_id}"):
            return jsonify(status="duplicate_delivery_ignored", ticket=ref.group(1))
        if t and t["status"] == "Needs Info":
            new, outcome = clarify(t["id"], body)
            if outcome in ("created", "duplicate"):
                send_email(sender, f"[{new['ref']}] Received", f"Thanks, we're on it. Priority {new['priority']}.")
            return jsonify(status=outcome, ticket=new["ref"])
        if t:
            log(t["id"], t["status"], f"Email from {sender}: {body[:120]}")
            return jsonify(status="appended", ticket=t["ref"])
    reporter = find_one("users", {"email": sender})
    t, outcome = intake(f"{subject}. {body}", reporter, "Email", event_key=f"email:{msg_id}", reporter_email=sender)
    if outcome == "created":
        send_email(sender, f"[{t['ref']}] Report received",
                   f"We logged your report as {t['ref']} ({t['priority']}). Track it in CampusOps.")
    elif outcome == "duplicate":
        send_email(sender, f"[{t['ref']}] Already being handled",
                   f"This issue is already reported ({t['report_count']} reports) and is {t['status'].lower()}.")
    return jsonify(status=outcome, ticket=t["ref"] if t else None)


@bp.post("/api/webhook/sensor")
def sensor_webhook():
    """Simulated IoT/BMS webhook (e.g. temperature, power or water-leak sensors). event_id makes it idempotent."""
    if not secret_ok():
        abort(401)
    e = request.get_json(silent=True) or {}
    if not e.get("event_id") or not e.get("room_code"):
        return jsonify(error="event_id and room_code are required"), 400
    text = f"Sensor alert from {e.get('sensor', 'sensor')} in {e['room_code']}: {e.get('message', '')} " \
           f"(reading {e.get('reading', '?')})."
    t, outcome = intake(text, None, "Sensor webhook", room_code=e["room_code"].upper(),
                        category=e.get("category"), event_key=f"sensor:{e['event_id']}")
    return jsonify(status=outcome, ticket=t["ref"] if t else None)


@bp.route("/ops/integrations")
@roles_required("facilities_admin", "admin")
def integrations():
    return render_template("integrations.html", u=g.user, secret=INBOUND_SECRET,
                           outbox=find("outbox", sort=[("id", DESCENDING)], limit=30),
                           rooms=find("rooms", sort=[("code", 1)]))


# ---- disruption scenarios (drive the real engine end to end)
@bp.post("/ops/scenario/<name>")
@roles_required("facilities_admin", "admin")
def scenario(name):
    student = lambda i: find("users", {"role": "student"}, sort=[("id", 1)])[i]  # noqa: E731
    if name == "exam_power":
        exam = db.bookings.find_one({"kind": "exam", "status": "Active", "date": {"$gte": date.today().isoformat()}},
                                    {"_id": 0}, sort=[("date", 1)])
        room = find_one("rooms", {"id": exam["room_id"]}) if exam else find_one("rooms", {"type": "exam_hall"})
        t, _ = intake(f"Complete power failure in {room['code']} {room['name']}. No lights, fans or projector, "
                      f"and the MCB keeps tripping with a burning smell. Exam scheduled here soon.",
                      find_one("users", {"role": "faculty"}), "Form", room_code=room["code"])
        flash(f"Scenario 1 run: {t['ref']} → {t['priority']}, room {room['code']} taken out of service and "
              f"{len(t.get('affected_bookings', []))} booking(s) relocated.", "info")
    elif name == "tech_sick":
        busiest = max(find("users", {"role": "technician", "available": True}), key=lambda x: workload(x["id"]),
                      default=None)
        if busiest:
            moved = set_technician_availability(busiest["id"], False, "Scenario")
            flash(f"Scenario 2 run: {busiest['name']} called in sick; {moved} work order(s) reassigned.", "info")
    elif name == "wifi_outage":
        reports = [("H-101", "Wi-Fi not working in my hostel room since morning, can't submit assignment"),
                   ("H-102", "no internet in H-102, wifi shows connected but nothing loads"),
                   ("H-214", "Wifi down in H-214"), ("H-101", "hostel wifi is dead again in room H-101"),
                   ("H-001", "Mess hall Wi-Fi not working, whole hostel seems offline")]
        refs = []
        for i, (room, text) in enumerate(reports):
            t, outcome = intake(text, student(i), "AI chat" if i % 2 else "Form", room_code=room)
            refs.append(f"{t['ref']}({outcome})")
        flash("Scenario 3 run: 5 Wi-Fi reports → " + ", ".join(refs) + ". See the linked major incident.", "info")
    elif name == "part_out":
        t, _ = intake("The network switch in the server room L-101 has failed; labs in Block A have no network.",
                      find_one("users", {"role": "faculty"}), "Form", room_code="L-101", category="Network")
        item = find_one("inventory", {"name": {"$regex": "switch", "$options": "i"}})
        tech = find_one("users", {"id": t.get("assigned_to")}) or find_one("users", {"role": "technician"})
        if t["status"] == "Assigned":
            transition(t["id"], "In Progress", tech["name"], "Technician on site.")
        out = request_part(t["id"], item["id"], 1, tech)
        flash(f"Scenario 4 run: {t['ref']} needs a {item['name']} → {out}; purchase approval sent to procurement.",
              "info")
    elif name == "sla":
        t = db.tickets.find_one({"status": {"$in": ["Assigned", "In Progress"]}}, {"_id": 0}, sort=[("id", 1)])
        if t:
            past = (now_dt() - timedelta(hours=t["sla_hours"] * 2)).strftime(FMT)
            update("tickets", {"id": t["id"]}, {"created": (now_dt() - timedelta(hours=t["sla_hours"] * 3)).strftime(FMT),
                                                "sla_due": past})
            sla_check()
            flash(f"Scenario 5 run: {t['ref']} pushed past its SLA → escalated.", "info")
    else:
        abort(404)
    return redirect(url_for("ops.center"))
