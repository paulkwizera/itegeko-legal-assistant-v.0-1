"""
Logged-in account pages: Profile (the usage dashboard -- current plan,
daily/weekly usage, upgrade CTA) and Settings (update name, change
password, manage email verification).

Split from auth.py because auth.py is about *becoming* authenticated
(signup/login/logout) while this is about managing an account you
already have -- same separation of concerns as auth.py vs auth_email.py.
"""
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from bson import ObjectId
from bson.errors import InvalidId

import db
import plans
from auth import login_required
from extensions import limiter
from auth_email import is_email_configured

account_bp = Blueprint("account", __name__)


def _get_current_user():
    if db.users is None:
        return None
    try:
        return db.users.find_one({"_id": ObjectId(session["user_id"])})
    except (InvalidId, TypeError, KeyError):
        return None


@account_bp.route("/profile")
@login_required
def profile():
    user = _get_current_user()
    if not user:
        return redirect(url_for("auth.login"))

    user_id = session["user_id"]
    plan = plans.get_plan(user_id)
    daily_used = plans.messages_used_today(user_id)
    weekly_used = plans.messages_used_this_week(user_id)
    daily_limit = plans.daily_limit_for(plan)
    weekly_limit = plans.weekly_limit_for(plan)

    return render_template(
        "profile.html",
        user=user,
        plan=plan,
        daily_used=daily_used,
        daily_limit=daily_limit,
        weekly_used=weekly_used,
        weekly_limit=weekly_limit,
        daily_pct=min(100, round(100 * daily_used / daily_limit)) if daily_limit else 0,
        weekly_pct=min(100, round(100 * weekly_used / weekly_limit)) if weekly_limit else 0,
        email_configured=is_email_configured(),
    )


@account_bp.route("/settings")
@login_required
def settings():
    user = _get_current_user()
    if not user:
        return redirect(url_for("auth.login"))
    return render_template(
        "settings.html",
        user=user,
        email_configured=is_email_configured(),
    )


@account_bp.route("/settings/profile", methods=["POST"])
@login_required
def update_profile():
    user = _get_current_user()
    if not user:
        return redirect(url_for("auth.login"))

    name = (request.form.get("name") or "").strip()
    if not name:
        return render_template("settings.html", user=user, email_configured=is_email_configured(),
                                profile_error="Name can't be empty.")

    if db.users is not None:
        db.users.update_one({"_id": user["_id"]}, {"$set": {"name": name}})
    session["user_name"] = name
    user["name"] = name
    return render_template("settings.html", user=user, email_configured=is_email_configured(),
                            profile_success="Your name has been updated.")


@account_bp.route("/settings/password", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def change_password():
    user = _get_current_user()
    if not user:
        return redirect(url_for("auth.login"))

    current_password = request.form.get("current_password") or ""
    new_password = request.form.get("new_password") or ""

    if not check_password_hash(user["password_hash"], current_password):
        return render_template("settings.html", user=user, email_configured=is_email_configured(),
                                password_error="Your current password is incorrect.")
    if len(new_password) < 8:
        return render_template("settings.html", user=user, email_configured=is_email_configured(),
                                password_error="New password must be at least 8 characters.")

    if db.users is not None:
        db.users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": generate_password_hash(new_password)}})
    return render_template("settings.html", user=user, email_configured=is_email_configured(),
                            password_success="Your password has been changed.")


@account_bp.route("/settings/preferences", methods=["POST"])
@login_required
def update_preferences():
    user = _get_current_user()
    if not user:
        return redirect(url_for("auth.login"))

    marketing_consent = request.form.get("marketing_consent") == "on"
    training_consent = request.form.get("training_consent") == "on"

    if db.users is not None:
        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"marketing_consent": marketing_consent, "training_consent": training_consent}},
        )
    user["marketing_consent"] = marketing_consent
    user["training_consent"] = training_consent
    return render_template("settings.html", user=user, email_configured=is_email_configured(),
                            preferences_success="Your preferences have been saved.")
