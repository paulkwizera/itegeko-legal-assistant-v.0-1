import os
import sys
import uuid
import logging
from datetime import datetime, timedelta

from flask import Flask, render_template, request, jsonify, session
from google import genai
from google.genai import types
from google.genai.errors import APIError, ClientError
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("itegeko")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

client = genai.Client(api_key=API_KEY) if API_KEY else None
if not API_KEY:
    logger.warning("GEMINI_API_KEY environment variable not set. The chat UI will run, but AI responses will be unavailable until it is configured.")

SYSTEM_INSTRUCTION = (
    "You are Itegeko, a professional AI legal assistant specializing exclusively in the laws and "
    "legal system of Rwanda. You have deep expertise in Rwandan legislation, constitutional law, "
    "criminal law, civil law, family law, land law, commercial law, labor law, and the judicial "
    "system of Rwanda. Always cite the relevant Rwandan law, act, or article where applicable. "
    "Be precise, professional, and objective. Format answers in a clear structure with headings "
    "and bullet points when helpful. If a question falls outside Rwandan law, politely clarify "
    "that your expertise is limited to Rwandan legal matters. Always recommend consulting a "
    "licensed Rwandan advocate for personal legal matters."
)

# ---------------------------------------------------------------------------
# Per-session chat storage.
#
# The original app created a single `chat` object at import time, so every
# visitor shared one conversation thread. That both leaks each user's
# questions into every other user's context AND burns through your request/
# token quota far faster than real usage would justify. Instead we keep one
# chat session per browser session (via a signed cookie), with a simple
# in-memory TTL so idle sessions get cleaned up.
# ---------------------------------------------------------------------------
_sessions = {}  # session_id -> {"chat": ChatSession, "last_used": datetime, "history": [...]}
SESSION_TTL = timedelta(hours=2)
MAX_SESSIONS = 500  # simple cap so memory can't grow unbounded


def _cleanup_sessions():
    now = datetime.utcnow()
    expired = [sid for sid, s in _sessions.items() if now - s["last_used"] > SESSION_TTL]
    for sid in expired:
        del _sessions[sid]
    # If still over the cap, drop the oldest entries.
    if len(_sessions) > MAX_SESSIONS:
        by_age = sorted(_sessions.items(), key=lambda kv: kv[1]["last_used"])
        for sid, _ in by_age[: len(_sessions) - MAX_SESSIONS]:
            del _sessions[sid]


def get_chat_for_session():
    _cleanup_sessions()
    sid = session.get("sid")
    if not sid:
        sid = str(uuid.uuid4())
        session["sid"] = sid

    entry = _sessions.get(sid)
    if entry is None:
        chat = client.chats.create(
            model=MODEL_NAME,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.7,
                # Gemini 3.5 Flash reasons internally before answering ("thinking"),
                # defaulting to a "medium" level. That's overkill for most legal
                # Q&A and adds noticeable latency, so we dial it down. Raise this
                # to "medium" or "high" if you find answers getting shallow on
                # harder questions.
                thinking_config=types.ThinkingConfig(thinking_level="low"),
            ),
        )
        entry = {"chat": chat, "last_used": datetime.utcnow(), "history": []}
        _sessions[sid] = entry
    else:
        entry["last_used"] = datetime.utcnow()

    return entry


def get_history_for_session():
    sid = session.get("sid")
    entry = _sessions.get(sid) if sid else None
    return entry["history"] if entry else []


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat_endpoint():
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()

    if not user_message:
        return jsonify({"error": "Please enter a question before sending."}), 400

    if len(user_message) > 4000:
        return jsonify({"error": "That message is too long (4000 character limit)."}), 400

    if client is None:
        return jsonify({"error": "The AI service is not configured yet. Please set GEMINI_API_KEY to enable responses."}), 503

    entry = get_chat_for_session()
    chat = entry["chat"]

    try:
        response = chat.send_message(user_message)
        reply_text = response.text
        now = datetime.utcnow().strftime("%H:%M")
        entry["history"].append({"role": "user", "text": user_message, "time": now})
        entry["history"].append({"role": "assistant", "text": reply_text, "time": now})
        return jsonify({"response": reply_text})

    except ClientError as e:
        # google-genai raises ClientError for 4xx responses, including
        # 429 (quota/rate limit) and 400 (bad request/invalid key).
        status = getattr(e, "code", None) or getattr(e, "status_code", None)
        message = str(e)
        logger.warning("Gemini ClientError (status=%s): %s", status, message)

        if status == 429 or "RESOURCE_EXHAUSTED" in message or "quota" in message.lower():
            return jsonify({
                "error": (
                    "Itegeko has hit its API rate/quota limit. This is a limit on the "
                    "underlying Gemini API key, not a bug in the app — please wait a "
                    "minute and try again, or check your Google AI Studio quota/billing."
                )
            }), 429

        if status == 400 or "API key not valid" in message:
            return jsonify({
                "error": "There's a configuration problem with the API key. Please contact the site administrator."
            }), 400

        return jsonify({"error": "The legal assistant service returned an error. Please try again."}), 502

    except APIError as e:
        logger.error("Gemini APIError: %s", e)
        return jsonify({"error": "The legal assistant is temporarily unavailable. Please try again shortly."}), 503

    except Exception as e:
        logger.exception("Unexpected error in /chat")
        return jsonify({"error": "Something went wrong on our end. Please try again."}), 500


@app.route("/chat/history", methods=["GET"])
def chat_history():
    """Return this visitor's own conversation so far, so the page can
    restore it (e.g. after a refresh) and let them scroll back through it."""
    return jsonify({"history": get_history_for_session()})


@app.route("/chat/new", methods=["POST"])
def new_chat():
    """Reset the current visitor's conversation only (not everyone else's)."""
    sid = session.get("sid")
    if sid and sid in _sessions:
        del _sessions[sid]
    session.pop("sid", None)
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": MODEL_NAME, "active_sessions": len(_sessions), "configured": client is not None})


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug_mode)