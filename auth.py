"""
Authentication: signup, login, logout, and a login_required guard.

Passwords are never stored in plain text -- werkzeug's generate_password_hash
salts and hashes them (PBKDF2 by default). The session cookie only ever holds
the user's Mongo _id as a string; no password or password hash ever touches
the session or gets sent back to the browser.
"""
import re
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo.errors import DuplicateKeyError

import db

auth_bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    if db.users is None:
        return render_template("signup.html", error="Accounts aren't configured yet (no database connection).")

    if not name or not email or not password:
        return render_template("signup.html", error="Please fill in every field.")
    if not EMAIL_RE.match(email):
        return render_template("signup.html", error="That doesn't look like a valid email address.")
    if len(password) < 8:
        return render_template("signup.html", error="Password must be at least 8 characters.")

    try:
        result = db.users.insert_one({
            "name": name,
            "email": email,
            "password_hash": generate_password_hash(password),
            "is_admin": False,
            "created_at": datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        return render_template("signup.html", error="An account with that email already exists.")

    session["user_id"] = str(result.inserted_id)
    session["user_name"] = name
    session["is_admin"] = False
    return redirect(url_for("home"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    if db.users is None:
        return render_template("login.html", error="Accounts aren't configured yet (no database connection).")

    user = db.users.find_one({"email": email})
    if not user or not check_password_hash(user["password_hash"], password):
        return render_template("login.html", error="Incorrect email or password.")

    session["user_id"] = str(user["_id"])
    session["user_name"] = user.get("name", "")
    session["is_admin"] = bool(user.get("is_admin", False))
    return redirect(url_for("home"))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
