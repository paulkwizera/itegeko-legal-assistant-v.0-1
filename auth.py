"""
Authentication: signup, login, logout, and a login_required guard.

Passwords are never stored in plain text -- werkzeug's generate_password_hash
salts and hashes them (PBKDF2 by default). The session cookie only ever holds
the user's Mongo _id as a string; no password or password hash ever touches
the session or gets sent back to the browser.
"""
import re
import logging
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from bson.errors import InvalidId

import db
import auth_email
from extensions import limiter
from auth_utils import login_required, admin_required  # re-exported for auth.py / admin.py callers

logger = logging.getLogger("itegeko.auth")

auth_bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Options offered on the post-signup onboarding questionnaire.
ONBOARDING_ROLES = [
    "Individual with a personal legal question",
    "Business owner / entrepreneur",
    "Student or researcher",
    "Lawyer or legal professional",
    "Other",
]
ONBOARDING_AREAS = [
    "Constitutional Law",
    "Criminal Law",
    "Land & Property Law",
    "Family Law",
    "Commercial Law",
    "Labour Law",
    "Judiciary & Court Procedure",
]


def _claim_guest_history(user_id):
    """If this browser had an anonymous guest session with chat history,
    fold it into the account that just logged in or signed up."""
    guest_id = session.pop("guest_id", None)
    session.pop("guest_count", None)
    session.pop("guest_prompt_shown", None)
    if guest_id:
        db.migrate_guest_messages(guest_id, user_id)


@auth_bp.route("/signup", methods=["GET", "POST"])
@limiter.limit("10 per minute")
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
            "plan": "free",
            "pro_until": None,
            "onboarding_complete": False,
            "email_verified": False,
            "profile": {},
            "created_at": datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        return render_template("signup.html", error="An account with that email already exists.")

    user_id = str(result.inserted_id)
    session.permanent = True
    session["user_id"] = user_id
    session["user_name"] = name
    session["user_email"] = email
    session["is_admin"] = False
    session["plan"] = "free"
    session["email_verified"] = False
    _claim_guest_history(user_id)

    if auth_email.is_email_configured():
        try:
            auth_email.send_verification_email(user_id, email, name)
        except Exception:
            logger.exception("Failed to send verification email at signup")

    return redirect(url_for("auth.onboarding"))


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("15 per minute")
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

    user_id = str(user["_id"])
    session.permanent = True
    session["user_id"] = user_id
    session["user_name"] = user.get("name", "")
    session["user_email"] = user.get("email", "")
    session["is_admin"] = bool(user.get("is_admin", False))
    session["plan"] = user.get("plan", "free")
    session["email_verified"] = bool(user.get("email_verified", False))
    _claim_guest_history(user_id)

    if not user.get("onboarding_complete"):
        return redirect(url_for("auth.onboarding"))
    return redirect(url_for("home"))


@auth_bp.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    """One-time post-signup questionnaire so we know a little about who's
    asking -- what kind of user they are, what they came here for, and which
    legal areas they care about. Purely optional; users can skip it."""
    if not session.get("user_id"):
        return redirect(url_for("auth.login"))

    if request.method == "GET":
        return render_template("onboarding.html", roles=ONBOARDING_ROLES, areas=ONBOARDING_AREAS)

    if db.users is not None:
        try:
            user_oid = ObjectId(session["user_id"])
        except InvalidId:
            return redirect(url_for("home"))

        if request.form.get("skip"):
            db.users.update_one({"_id": user_oid}, {"$set": {"onboarding_complete": True}})
        else:
            profile = {
                "role": (request.form.get("role") or "").strip(),
                "reason": (request.form.get("reason") or "").strip()[:500],
                "interests": request.form.getlist("interests"),
            }
            db.users.update_one(
                {"_id": user_oid},
                {"$set": {"profile": profile, "onboarding_complete": True}},
            )

    return redirect(url_for("home"))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
