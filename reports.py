"""Weekly performance report: builds a PDF per student and sends it to the parent on WhatsApp
through the WhatsApp Business Cloud API (Meta)."""
import os
from datetime import date, timedelta

import requests
from fpdf import FPDF

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def build_pdf(student, summary, extras):
    """summary comes from app.student_summary(); extras holds assignments, tickets, quiz count."""
    week_end = date.today()
    week_start = week_end - timedelta(days=6)
    pdf = FPDF()
    pdf.add_page()

    pdf.set_fill_color(79, 70, 229)
    pdf.rect(0, 0, 210, 32, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_xy(12, 8)
    pdf.cell(0, 9, "CampusVerse - Weekly Performance Report")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_xy(12, 19)
    pdf.cell(0, 6, f"Week: {week_start:%d %b %Y} to {week_end:%d %b %Y}")
    pdf.set_text_color(31, 41, 55)
    pdf.set_y(40)

    def heading(text):
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)

    def row(cells, widths, bold=False, color=None):
        pdf.set_font("Helvetica", "B" if bold else "", 10)
        for i, (text, w) in enumerate(zip(cells, widths)):
            pdf.set_text_color(*(color[i] if color and color[i] else (31, 41, 55)))
            pdf.cell(w, 7, str(text), border="B")
        pdf.set_text_color(31, 41, 55)
        pdf.ln()

    heading("Student")
    pdf.cell(0, 6, f"{student['name']}   |   Roll: {student['roll']}   |   {student['dept']} Year {student['year']}"
                   f"   |   CGPA: {student['cgpa']}", new_x="LMARGIN", new_y="NEXT")

    heading(f"Attendance (overall {summary['overall']}%)")
    widths = (25, 70, 30, 55)
    row(("Code", "Subject", "Attended", "Status"), widths, bold=True)
    red, green = (220, 38, 38), (22, 163, 74)
    for a in summary["att"]:
        status = f"Needs next {a['need']} classes" if a["need"] else "Safe (75%+)"
        row((a["code"], a["name"], f"{a['present']}/{a['total']} ({a['pct']}%)", status), widths,
            color=(None, None, None, red if a["need"] else green))

    heading("Marks")
    widths = (25, 70, 40, 45)
    row(("Code", "Subject", "Exam", "Score"), widths, bold=True)
    for m in summary["marks"]:
        pct = m["score"] / m["max_score"] * 100
        row((m["code"], m["name"], m["exam"], f"{m['score']:g}/{m['max_score']:g} ({pct:.0f}%)"), widths,
            color=(None, None, None, red if pct < 50 else None))

    heading("This week")
    pdf.multi_cell(0, 6,
                   f"Pending assignments: {len(extras['assignments'])}\n"
                   f"AI quizzes completed: {extras['quizzes']}\n"
                   f"Fees pending: Rs. {summary['fee_pending']:,}\n"
                   f"Open hostel / maintenance tickets: {extras['open_tickets']}")
    if extras["assignments"]:
        pdf.ln(1)
        for a in extras["assignments"]:
            pdf.cell(0, 6, f"  - {a['code']}: {a['title']} (due {a['due']})", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(107, 114, 128)
    pdf.multi_cell(0, 5, "This report was generated automatically by CampusVerse. "
                         "For questions, contact the department office.")

    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"{student['roll']}_{week_end.isoformat()}.pdf")
    pdf.output(path)
    return path


def whatsapp_configured():
    return bool(os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_NUMBER_ID"))


def send_whatsapp_pdf(phone, pdf_path, caption):
    """Uploads the PDF, then sends it as a document message. Returns (ok, detail)."""
    if not whatsapp_configured():
        return False, "WhatsApp not configured (PDF saved only)"
    version = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
    base = f"https://graph.facebook.com/{version}/{os.environ['WHATSAPP_PHONE_NUMBER_ID']}"
    headers = {"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"}
    try:
        with open(pdf_path, "rb") as f:
            up = requests.post(f"{base}/media", headers=headers, timeout=30,
                               data={"messaging_product": "whatsapp", "type": "application/pdf"},
                               files={"file": (os.path.basename(pdf_path), f, "application/pdf")})
        if not up.ok:
            return False, f"Upload failed: {up.text[:200]}"
        msg = requests.post(f"{base}/messages", headers=headers, timeout=30, json={
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "document",
            "document": {"id": up.json()["id"], "filename": os.path.basename(pdf_path), "caption": caption},
        })
        if not msg.ok:
            return False, f"Send failed: {msg.text[:200]}"
        return True, "Sent"
    except requests.RequestException as e:
        return False, f"Network error: {e}"
