"""
Email-based security flows: password reset and email verification.

Requires SMTP configuration in environment variables. If not configured,
the routes will show a "not configured" message rather than crashing.
"""
import os
import secrets
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash
from bson import ObjectId
from bson.errors import InvalidId

import db
from extensions import limiter
from auth_utils import login_required

logger = logging.getLogger("itegeko.auth_email")

email_bp = Blueprint("auth_email", __name__)

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "noreply@itegeko.rw")

TOKEN_EXPIRY_HOURS = 1


def is_email_configured():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)


def _send_email(to_email, subject, html_body):
    """Send an email via SMTP. Raises on failure."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


def _generate_token(user_id, token_type):
    """Generate a secure token and store it in the database."""
    if db.tokens is None:
        return None
    token = secrets.token_urlsafe(48)
    db.tokens.insert_one({
        "token": token,
        "user_id": user_id,
        "type": token_type,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRY_HOURS),
        "created_at": datetime.now(timezone.utc),
        "used": False,
    })
    return token


def _verify_token(token, token_type):
    """Verify a token and return the associated user_id, or None."""
    if db.tokens is None:
        return None
    doc = db.tokens.find_one({
        "token": token,
        "type": token_type,
        "used": False,
        "expires_at": {"$gt": datetime.now(timezone.utc)},
    })
    if not doc:
        return None
    return doc["user_id"]


def _consume_token(token):
    """Mark a token as used."""
    if db.tokens is not None:
        db.tokens.update_one({"token": token}, {"$set": {"used": True}})


@email_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html", email_configured=is_email_configured())

    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return render_template("forgot_password.html", error="Please enter your email address.",
                               email_configured=is_email_configured())

    if not is_email_configured():
        return render_template("forgot_password.html",
                               error="Email is not configured yet. Please contact the administrator.",
                               email_configured=False)

    if db.users is None:
        return render_template("forgot_password.html", error="Database not available.",
                               email_configured=is_email_configured())

    user = db.users.find_one({"email": email})
    # Always show success to prevent email enumeration
    if user:
        token = _generate_token(str(user["_id"]), "password_reset")
        if token:
            reset_url = url_for("auth_email.reset_password", token=token, _external=True)
            try:
                _send_email(
                    email,
                    "Reset your Itegeko password",
                    f"""
                    <div style="font-family: Inter, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
                        <h2 style="color: #c9a84c;">Itegeko Password Reset</h2>
                        <p>You requested a password reset. Click the link below to set a new password:</p>
                        <p><a href="{reset_url}" style="display: inline-block; padding: 12px 24px; background: linear-gradient(135deg, #c9a84c, #e8c96d); color: #14171c; text-decoration: none; border-radius: 8px; font-weight: 600;">Reset Password</a></p>
                        <p style="font-size: 0.85em; color: #666;">This link expires in {TOKEN_EXPIRY_HOURS} hour(s). If you didn't request this, ignore this email.</p>
                    </div>
                    """,
                )
            except Exception:
                logger.exception("Failed to send password reset email")

    return render_template("forgot_password.html",
                           success="If an account with that email exists, we've sent a reset link.",
                           email_configured=is_email_configured())


@email_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user_id = _verify_token(token, "password_reset")
    if not user_id:
        return render_template("reset_password.html", error="This link is invalid or has expired.",
                               token_valid=False)

    if request.method == "GET":
        return render_template("reset_password.html", token=token, token_valid=True)

    password = request.form.get("password") or ""
    if len(password) < 8:
        return render_template("reset_password.html", token=token, token_valid=True,
                               error="Password must be at least 8 characters.")

    if db.users is not None:
        db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"password_hash": generate_password_hash(password)}},
        )
    _consume_token(token)

    return render_template("reset_password.html", token_valid=False,
                           success="Your password has been reset. You can now log in.")


# ---------------------------------------------------------------------------
# Email verification -- non-blocking: an unverified account can still log in
# and chat normally. This only confirms the address is real/reachable and
# shows a badge in Settings, rather than gating access on it (gating access
# would be a bigger behavior change than "support" calls for, and risks
# locking someone out if SMTP is misconfigured).
# ---------------------------------------------------------------------------

def send_verification_email(user_id, email, name=""):
    """Sent right after signup -- doubles as the welcome email (rather than
    sending two separate emails back-to-back) and as the address
    verification link. Generates a fresh token. Raises on failure (caller
    decides how to handle -- e.g. signup logs it and continues)."""
    token = _generate_token(user_id, "email_verification")
    if not token:
        return
    verify_url = url_for("auth_email.verify_email", token=token, _external=True)
    greeting = f"Hi {name}," if name else "Hi,"
    _send_email(
        email,
        "Welcome to Itegeko — verify your email",
        f"""
        <div style="font-family: Inter, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #c9a84c;">Welcome to Itegeko</h2>
            <p>{greeting}</p>
            <p>Thanks for creating an account. Itegeko can help you understand Rwandan law, review a contract or
            document you upload, and point you to a real advocate when you need one.</p>
            <p>One last step -- please confirm this is your email address:</p>
            <p><a href="{verify_url}" style="display: inline-block; padding: 12px 24px; background: linear-gradient(135deg, #c9a84c, #e8c96d); color: #14171c; text-decoration: none; border-radius: 8px; font-weight: 600;">Verify Email</a></p>
            <p style="font-size: 0.85em; color: #666;">This link expires in {TOKEN_EXPIRY_HOURS} hour(s). Your account already works without verifying -- this just confirms the address is really yours. If you didn't create an Itegeko account, you can ignore this email.</p>
        </div>
        """,
    )


@email_bp.route("/verify-email/<token>", methods=["GET"])
def verify_email(token):
    user_id = _verify_token(token, "email_verification")
    if not user_id:
        return render_template("verify_email.html", success=False,
                               error="This verification link is invalid or has expired.")

    if db.users is not None:
        db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"email_verified": True}})
    _consume_token(token)
    if session.get("user_id") == user_id:
        session["email_verified"] = True

    return render_template("verify_email.html", success=True)


@email_bp.route("/resend-verification", methods=["POST"])
@login_required
@limiter.limit("5 per hour")
def resend_verification():
    if db.users is None:
        return redirect(url_for("account.settings"))
    try:
        user = db.users.find_one({"_id": ObjectId(session["user_id"])})
    except (InvalidId, TypeError):
        return redirect(url_for("account.settings"))
    if not user:
        return redirect(url_for("account.settings"))

    if user.get("email_verified"):
        return render_template("settings.html", user=user, email_configured=is_email_configured(),
                               profile_success="Your email is already verified.")
    if not is_email_configured():
        return render_template("settings.html", user=user, email_configured=False,
                               profile_error="Email isn't configured on this server yet.")

    try:
        send_verification_email(str(user["_id"]), user["email"], user.get("name", ""))
    except Exception:
        logger.exception("Failed to resend verification email")
        return render_template("settings.html", user=user, email_configured=True,
                               profile_error="Couldn't send the verification email. Please try again shortly.")

    return render_template("settings.html", user=user, email_configured=True,
                           profile_success=f"Verification email sent to {user['email']}.")
