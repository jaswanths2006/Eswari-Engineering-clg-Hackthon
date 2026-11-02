"""Login session helpers and backend-enforced role checks, shared by app.py and ops.py."""
from functools import wraps

from flask import abort, g, redirect, session, url_for

from db import find_one

ROLE_LABELS = {
    "admin": "System Administrator", "facilities_admin": "Facilities Administrator",
    "dept_manager": "Department Manager", "procurement": "Procurement Approver", "technician": "Technician",
    "faculty": "Faculty", "student": "Student", "recruiter": "Recruiter",
}
ROLES = list(ROLE_LABELS)
HOME = {"admin": "admin_dashboard", "facilities_admin": "ops.center", "dept_manager": "ops.manager",
        "procurement": "ops.procurement", "technician": "ops.technician", "faculty": "faculty_dashboard",
        "student": "student_dashboard", "recruiter": "recruiter_dashboard"}


def current_user():
    uid = session.get("user_id")
    return find_one("users", {"id": uid}) if uid else None


def roles_required(*roles):
    """Rejects the request with 403 unless the logged-in user has one of the roles. Checked on the server
    for every protected route, so hiding a button in the UI is never the only protection."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login"))
            if user["role"] not in roles:
                abort(403)
            g.user = user
            return view(*args, **kwargs)
        return wrapped
    return decorator


def role_required(role):
    return roles_required(role)
