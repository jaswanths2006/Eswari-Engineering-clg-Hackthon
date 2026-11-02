"""Gemini-powered features: student assistant chat, note summaries, and quiz generation.
Uses the Gemini REST API (generateContent) with the key from GEMINI_API_KEY."""
import json
import os

import requests

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class AIUnavailable(Exception):
    pass


def _generate(system, contents, tools=None, generation_config=None):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise AIUnavailable("AI is not configured. Add GEMINI_API_KEY to the .env file.")
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents}
    if tools:
        body["tools"] = tools
    if generation_config:
        body["generationConfig"] = generation_config
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
    r = None
    for attempt in range(2):  # one retry: Gemini sometimes answers 503 (overloaded) or stalls
        try:
            r = requests.post(API.format(model=model), headers={"x-goog-api-key": key}, json=body, timeout=40)
            if r.status_code not in (500, 503):
                break
        except requests.RequestException:
            r = None
    if r is None:
        raise AIUnavailable("Could not reach the AI service. Check the internet connection.")
    if r.status_code in (400, 401, 403) and "API key" in r.text:
        raise AIUnavailable("The Gemini API key is invalid.")
    if r.status_code == 429:
        raise AIUnavailable("The AI is busy right now. Please try again in a minute.")
    if not r.ok:
        raise AIUnavailable(f"AI service error ({r.status_code}). Please try again.")
    candidates = r.json().get("candidates") or []
    if not candidates or "content" not in candidates[0]:
        raise AIUnavailable("The AI could not help with that request.")
    return candidates[0]["content"]


def _text(content):
    return "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()


# ---------- note / assignment summaries ----------
def summarize(title, content):
    reply = _generate(
        "You summarize college study material for students. Use short headings and bullet points (plain text, "
        "use '•' for bullets, no markdown symbols like ** or #). Cover the key concepts, definitions, formulas and "
        "likely exam points. Keep it under 250 words. Use only the material given; do not add outside facts.",
        [{"role": "user", "parts": [{"text": f"Title: {title}\n\n<material>\n{content}\n</material>"}]}],
    )
    return _text(reply)


# ---------- quiz generation ----------
QUIZ_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "questions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "options": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "answer_index": {"type": "INTEGER"},
                },
                "required": ["question", "options", "answer_index"],
            },
        }
    },
    "required": ["questions"],
}


def make_quiz(title, content, count=5):
    reply = _generate(
        "You write multiple-choice quiz questions for college students, based only on the given material.",
        [{"role": "user", "parts": [{"text":
            f"Write {count} questions about '{title}'. Each has exactly 4 options and one correct answer "
            f"(answer_index is 0-3). Mix easy and medium difficulty.\n\n<material>\n{content}\n</material>"}]}],
        generation_config={"responseMimeType": "application/json", "responseSchema": QUIZ_SCHEMA},
    )
    try:
        questions = json.loads(_text(reply))["questions"]
    except (ValueError, KeyError):
        raise AIUnavailable("The AI returned an unusable quiz. Please try again.")
    valid = [q for q in questions if len(q["options"]) == 4 and 0 <= q["answer_index"] < 4]
    if not valid:
        raise AIUnavailable("The AI returned an unusable quiz. Please try again.")
    return valid


# ---------- CampusOps: issue extraction and incident summaries ----------
ISSUE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category": {"type": "STRING", "enum": ["Electrical", "Plumbing", "Network", "HVAC", "Carpentry",
                                                "IT Equipment", "Cleanliness", "Security", "Other"]},
        "title": {"type": "STRING"},
        "room_code": {"type": "STRING", "description": "Exact code from the room list, or empty if unknown"},
        "location_text": {"type": "STRING"},
        "severity": {"type": "INTEGER", "description": "1 = cosmetic, 2 = minor, 3 = disrupts some work, "
                                                        "4 = blocks a room/class/many people, 5 = safety risk or campus-wide"},
        "severity_reason": {"type": "STRING"},
        "assets": {"type": "ARRAY", "items": {"type": "STRING"}},
        "room_unusable": {"type": "BOOLEAN"},
        "safety_risk": {"type": "BOOLEAN"},
        "summary": {"type": "STRING"},
        "missing": {"type": "ARRAY", "items": {"type": "STRING"}},
        "clarification_question": {"type": "STRING"},
    },
    "required": ["category", "title", "room_code", "location_text", "severity", "severity_reason", "assets",
                 "room_unusable", "safety_risk", "summary", "missing", "clarification_question"],
}


def extract_issue(text, rooms):
    """rooms: list of 'CODE: name (building)' strings the model must map the location to."""
    reply = _generate(
        "You triage maintenance reports for a college campus. Extract structured fields from the report.\n"
        "- room_code must be one of the codes in the room list, chosen only if the report clearly points to it; "
        "otherwise leave it empty and put whatever place was mentioned in location_text.\n"
        "- If no usable location is given, add 'location' to missing and write a short, friendly "
        "clarification_question asking for it. Do the same for any other detail needed to act.\n"
        "- severity: judge real impact on people (safety, exams, classes, how many affected).\n"
        "- room_unusable: true only if the room cannot be used for classes until fixed (e.g. power failure, "
        "flooding, no AC in a sealed hall in summer, broken door lock on a lab).\n"
        "- assets: equipment involved, e.g. projector, AC, Wi-Fi access point, ceiling fan.\n"
        "- summary: one sentence for the technician.\n\nRoom list:\n" + "\n".join(rooms),
        [{"role": "user", "parts": [{"text": f"<report>\n{text}\n</report>"}]}],
        generation_config={"responseMimeType": "application/json", "responseSchema": ISSUE_SCHEMA},
    )
    try:
        data = json.loads(_text(reply))
    except ValueError:
        raise AIUnavailable("The AI returned an unreadable result.")
    data["severity"] = max(1, min(5, int(data.get("severity") or 3)))
    return data


def summarize_incident(reports):
    """One combined summary for several related complaints (e.g. a Wi-Fi outage across a building)."""
    reply = _generate(
        "You write incident summaries for a campus facilities team. In 2-3 plain sentences, state what is "
        "happening, where, how many reports, the likely common cause, and the business impact.",
        [{"role": "user", "parts": [{"text": "\n".join(f"- {r}" for r in reports)}]}],
    )
    return _text(reply)


# ---------- student assistant ----------
TOOLS = [{"functionDeclarations": [
    {
        "name": "raise_ticket",
        "description": "Report a hostel, classroom or campus maintenance problem on the student's behalf. "
                       "CampusOps triages it, checks for duplicates, sets priority and assigns a technician; the "
                       "student can track it live. Only call it once the student has told you the exact place "
                       "(block and room number, or a named place like 'library'). If they haven't, ask first.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description": {"type": "STRING", "description": "The problem in the student's words, with "
                                                                 "any details they gave (what, since when, impact)"},
                "location": {"type": "STRING", "description": "Room code, block and room number, or place"},
            },
            "required": ["description", "location"],
        },
    },
    {
        "name": "read_note",
        "description": "Read the full text of a study note shared by faculty, to answer a doubt from it.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"note_id": {"type": "INTEGER"}},
            "required": ["note_id"],
        },
    },
]}]

SYSTEM = """You are CampusVerse Assistant, a friendly helper inside a college portal, talking to one student.
You can:
- answer questions about the student's own attendance, marks, fees, assignments and notes, using the data below;
- explain topics and clear doubts, using read_note when a question is about a shared note;
- report hostel, classroom and maintenance problems with raise_ticket. Ask for missing details (what is wrong, where) before raising one.
  After raising, tell the student the ticket reference, its priority, and whether it was merged with an existing report.
Keep answers short and clear, in plain text (use '•' for lists, no markdown symbols like ** or #).
Use the student's data exactly; never invent marks or numbers.
If attendance in a subject is below 75%, mention how many classes they need to attend.
You cannot see or reveal AI quiz scores; those go only to faculty."""


def chat(history, student_context, tool_handler):
    """Run one assistant turn. history is a list of {"role": "user"|"assistant", "content": text}
    ending with the student's new message. Returns (reply_text, tickets_raised)."""
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in history]
    system = SYSTEM + "\n\n<student_data>\n" + student_context + "\n</student_data>"
    tickets = []
    for _ in range(6):
        reply = _generate(system, contents, tools=TOOLS)
        calls = [p["functionCall"] for p in reply.get("parts", []) if "functionCall" in p]
        if not calls:
            return _text(reply) or "Sorry, I didn't catch that. Could you rephrase?", tickets
        contents.append(reply)  # keep the model turn unchanged (it may carry thought signatures)
        responses = []
        for call in calls:
            try:
                output = tool_handler(call["name"], call.get("args", {}))
                if call["name"] == "raise_ticket":
                    tickets.append(output)
            except Exception as e:  # report the failure to the model rather than crash the chat
                output = {"error": str(e)}
            responses.append({"functionResponse": {"name": call["name"], "response": output}})
        contents.append({"role": "user", "parts": responses})
    return "Sorry, I couldn't finish that. Please try again.", tickets
