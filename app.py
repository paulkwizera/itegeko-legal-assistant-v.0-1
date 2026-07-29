import os
import sys
import uuid
import logging
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, render_template, request, jsonify, session
from google import genai
from google.genai import types
from google.genai.errors import APIError, ClientError, ServerError
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("itegeko")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

API_KEY = os.environ.get("GEMINI_API_KEY")

client = genai.Client(api_key=API_KEY) if API_KEY else None
if not API_KEY:
    logger.warning("GEMINI_API_KEY environment variable not set. The chat UI will run, but AI responses will be unavailable until it is configured.")

# ---------------------------------------------------------------------------
# Model fallback chain.
#
# gemini-3.5-flash occasionally returns 503 UNAVAILABLE during demand spikes.
# Rather than surface that to users, we keep a small ordered list of models
# to fall back through. GEMINI_MODEL (if set) is tried first; a couple of
# sensible backups follow it. A module-level pointer tracks the best
# currently-working model so *new* sessions start there too, instead of
# every new visitor re-discovering the same outage.
# ---------------------------------------------------------------------------
_primary_model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
_backup_models = ["gemini-2.5-flash", "gemini-3.5-flash-lite"]
FALLBACK_MODELS = [_primary_model] + [m for m in _backup_models if m != _primary_model]

_active_model_lock = threading.Lock()
_active_model_index = 0  # index into FALLBACK_MODELS of the best known-working model


def _get_active_model_index():
    with _active_model_lock:
        return _active_model_index


def _advance_active_model_index(new_index):
    global _active_model_index
    with _active_model_lock:
        if new_index > _active_model_index:
            _active_model_index = new_index

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


def _make_config():
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.7,
        thinking_config=types.ThinkingConfig(thinking_level="low"),
    )


def _create_chat(model_name, history=None):
    return client.chats.create(model=model_name, config=_make_config(), history=history)


# ---------------------------------------------------------------------------
# Per-session chat storage.
#
# Every visitor gets their own chat session (via a signed cookie) instead of
# sharing one global thread. Each session entry also tracks which model in
# FALLBACK_MODELS it's currently using, so a mid-conversation model swap
# (see send_message_with_fallback) can rebuild the chat on a new model
# without losing the conversation so far.
# ---------------------------------------------------------------------------
_sessions = {}  # session_id -> {"chat", "model_index", "last_used", "history"}
SESSION_TTL = timedelta(hours=2)
MAX_SESSIONS = 500  # simple cap so memory can't grow unbounded

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [1, 2, 4]


def _cleanup_sessions():
    now = datetime.utcnow()
    expired = [sid for sid, s in _sessions.items() if now - s["last_used"] > SESSION_TTL]
    for sid in expired:
        del _sessions[sid]
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
        model_index = _get_active_model_index()
        chat = _create_chat(FALLBACK_MODELS[model_index])
        entry = {
            "chat": chat,
            "model_index": model_index,
            "last_used": datetime.utcnow(),
            "history": [],
        }
        _sessions[sid] = entry
    else:
        entry["last_used"] = datetime.utcnow()

    return entry


def get_history_for_session():
    sid = session.get("sid")
    entry = _sessions.get(sid) if sid else None
    return entry["history"] if entry else []


def _is_overload_error(e):
    message = str(e)
    return "503" in message or "UNAVAILABLE" in message


def _send_with_backoff(chat, user_message):
    """Retry the same model a few times with backoff for a transient 503.
    Any other error (or a 503 that persists through every retry) is raised
    for the caller to handle -- e.g. by falling back to another model."""
    last_error = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return chat.send_message(user_message)
        except ServerError as e:
            last_error = e
            if not _is_overload_error(e) or attempt == RETRY_ATTEMPTS - 1:
                raise
            delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(
                "%s overloaded (attempt %d/%d), retrying in %ds",
                chat._model if hasattr(chat, "_model") else "model",
                attempt + 1, RETRY_ATTEMPTS, delay,
            )
            time.sleep(delay)
    raise last_error


def send_message_with_fallback(entry, user_message):
    """Try the session's current model with retries; if it's still
    overloaded, permanently swap this session (and future new sessions) to
    the next model in FALLBACK_MODELS, carrying the conversation history
    over with no extra API calls, and try again. Only gives up once every
    model in the chain has failed."""
    while True:
        try:
            return _send_with_backoff(entry["chat"], user_message)
        except ServerError as e:
            if not _is_overload_error(e):
                raise

            next_index = entry["model_index"] + 1
            if next_index >= len(FALLBACK_MODELS):
                raise  # every model in the chain is overloaded right now

            prior_history = entry["chat"].get_history()
            new_model = FALLBACK_MODELS[next_index]
            logger.warning(
                "%s still overloaded after retries -- switching this session to %s",
                FALLBACK_MODELS[entry["model_index"]], new_model,
            )
            entry["chat"] = _create_chat(new_model, history=prior_history)
            entry["model_index"] = next_index
            _advance_active_model_index(next_index)
            # loop again and try send_with_backoff on the new model


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

    try:
        response = send_message_with_fallback(entry, user_message)
        reply_text = response.text
        now = datetime.utcnow().strftime("%H:%M")
        entry["history"].append({"role": "user", "text": user_message, "time": now})
        entry["history"].append({"role": "assistant", "text": reply_text, "time": now})
        return jsonify({"response": reply_text})

    except ClientError as e:
        status = getattr(e, "code", None) or getattr(e, "status_code", None)
        message = str(e)
        logger.warning("Gemini ClientError (status=%s): %s", status, message)

        if status == 429 or "RESOURCE_EXHAUSTED" in message or "quota" in message.lower():
            return jsonify({
                "error": (
                    "Itegeko has hit its API rate/quota limit. This is a limit on the "
                    "underlying Gemini API key, not a bug in the app -- please wait a "
                    "minute and try again, or check your Google AI Studio quota/billing."
                )
            }), 429

        if status == 400 or "API key not valid" in message:
            return jsonify({
                "error": "There's a configuration problem with the API key. Please contact the site administrator."
            }), 400

        return jsonify({"error": "The legal assistant service returned an error. Please try again."}), 502

    except ServerError as e:
        logger.error("All models in the fallback chain are overloaded: %s", e)
        return jsonify({
            "error": "All available Gemini models are experiencing high demand right now, even after "
                     "retrying and switching models automatically. Please try again in a few minutes."
        }), 503

    except APIError as e:
        logger.error("Gemini APIError: %s", e)
        return jsonify({"error": "The legal assistant is temporarily unavailable. Please try again shortly."}), 503

    except Exception as e:
        logger.exception("Unexpected error in /chat")
        return jsonify({"error": "Something went wrong on our end. Please try again."}), 500


@app.route("/chat/history", methods=["GET"])
def chat_history():
    return jsonify({"history": get_history_for_session()})


@app.route("/chat/new", methods=["POST"])
def new_chat():
    sid = session.get("sid")
    if sid and sid in _sessions:
        del _sessions[sid]
    session.pop("sid", None)
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "configured": client is not None,
        "fallback_chain": FALLBACK_MODELS,
        "active_model": FALLBACK_MODELS[_get_active_model_index()],
        "active_sessions": len(_sessions),
    })


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug_mode)