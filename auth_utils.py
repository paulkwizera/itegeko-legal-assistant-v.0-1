"""
login_required / admin_required guards, split out from auth.py into their
own module so other blueprints -- auth_email.py (email verification),
account.py (profile/settings) -- can use them without importing auth.py
itself, which would create a circular import now that auth.py sends
verification email via auth_email.py.
"""
from functools import wraps
from flask import request, redirect, url_for, session, jsonify


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/chat"):
                return jsonify({"error": "Please log in to continue."}), 401
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        if not session.get("is_admin"):
            return jsonify({"error": "Admin access required."}), 403
        return view(*args, **kwargs)
    return wrapped
