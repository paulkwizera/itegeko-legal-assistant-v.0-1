import os
import uuid
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from google import genai
from google.genai import types
from google.genai.errors import APIError, ClientError, ServerError
from dotenv import load_dotenv
from bson import ObjectId
from bson.errors import InvalidId

import db
import gazette
import plans
import payments
from auth import auth_bp, login_required
from admin import admin_bp

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("itegeko")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)

API_KEY = os.environ.get("GEMINI_API_KEY")

client = genai.Client(api_key=API_KEY) if API_KEY else None
if not API_KEY:
    logger.warning("GEMINI_API_KEY not set. The chat UI will run, but AI responses will be unavailable.")

# ---------------------------------------------------------------------------
# Model fallback chain (unchanged from before) -- see previous version's
# comments if you're looking at this for the first time. Short version: if
# the primary Gemini model returns a persistent 503 overload, we swap to the
# next model in this list for the rest of that conversation.
# ---------------------------------------------------------------------------
_primary_model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
_backup_models = ["gemini-2.5-flash", "gemini-3.5-flash-lite"]
FALLBACK_MODELS = [_primary_model] + [m for m in _backup_models if m != _primary_model]

_active_model_lock = threading.Lock()
_active_model_index = 0


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
    "and bullet points when helpful. Always recommend consulting a licensed Rwandan advocate for "
    "personal legal matters. Each question below may come with its own instructions about how to "
    "use (or not use) any gazette excerpts provided -- follow those instructions exactly."
)

# If true (the default -- you asked for this), Itegeko only answers from what's
# actually been uploaded to the gazette collection, and says so plainly when
# nothing matches, instead of falling back to its general training knowledge.
# Set STRICT_GAZETTE_ONLY=false in the environment for a hybrid mode instead
# (use gazette matches when found, general knowledge otherwise).
STRICT_GAZETTE_ONLY = os.environ.get("STRICT_GAZETTE_ONLY", "true").lower() == "true"


def _make_config():
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.7,
        thinking_config=types.ThinkingConfig(thinking_level="low"),
    )


def _create_chat(model_name, history=None):
    return client.chats.create(model=model_name, config=_make_config(), history=history)


# ---------------------------------------------------------------------------
# Conversation storage.
#
# Source of truth for chat history is MongoDB (db.messages), scoped by
# user_id + conversation_id -- that's what survives a restart and what
# /chat/history reads from. The live Gemini `Chat` object itself can't be
# serialized into Mongo, so we keep a small in-memory cache of *active*
# chat objects keyed by conversation_id, and rebuild one from the persisted
# Mongo history whenever it's missing (first message after a restart, after
# a model swap, etc.) via `history=` on chats.create -- no extra API calls.
# ---------------------------------------------------------------------------
_active_chats = {}  # conversation_id -> {"chat", "model_index", "last_used"}
ACTIVE_CHAT_TTL = timedelta(hours=2)
MAX_ACTIVE_CHATS = 500

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [1, 2, 4]


def _cleanup_active_chats():
    now = datetime.now(timezone.utc)
    expired = [cid for cid, s in _active_chats.items() if now - s["last_used"] > ACTIVE_CHAT_TTL]
    for cid in expired:
        del _active_chats[cid]
    if len(_active_chats) > MAX_ACTIVE_CHATS:
        by_age = sorted(_active_chats.items(), key=lambda kv: kv[1]["last_used"])
        for cid, _ in by_age[: len(_active_chats) - MAX_ACTIVE_CHATS]:
            del _active_chats[cid]


def _load_history_from_db(user_id, conversation_id):
    if db.messages is None:
        return []
    return list(
        db.messages.find({"user_id": user_id, "conversation_id": conversation_id}).sort("created_at", 1)
    )


def _reconstruct_gemini_history(db_messages):
    """Turn our stored {role, text} documents back into the Content objects
    chats.create(history=...) expects, so a rebuilt chat has full context
    without replaying every turn through the API again."""
    history = []
    for m in db_messages:
        history.append(types.Content(role=m["role"], parts=[types.Part(text=m["text"])]))
    return history


def get_active_conversation(storage_user_id):
    """Returns the active conversation entry for storage_user_id, creating or
    rebuilding it as needed. Requires session['conversation_id'] to already
    be set (see ensure_conversation). storage_user_id is either a Mongo user
    _id string (logged-in) or "guest:<uuid>" (anonymous)."""
    _cleanup_active_chats()
    conversation_id = session["conversation_id"]
    entry = _active_chats.get(conversation_id)

    if entry is None:
        model_index = _get_active_model_index()
        db_messages = _load_history_from_db(storage_user_id, conversation_id)
        history = _reconstruct_gemini_history(db_messages)
        chat = _create_chat(FALLBACK_MODELS[model_index], history=history or None)
        entry = {"chat": chat, "model_index": model_index, "last_used": datetime.now(timezone.utc)}
        _active_chats[conversation_id] = entry
    else:
        entry["last_used"] = datetime.now(timezone.utc)

    return entry


def ensure_conversation():
    if "conversation_id" not in session:
        session["conversation_id"] = str(uuid.uuid4())


def current_identity():
    """Returns (storage_user_id, is_guest). Logged-in users get their Mongo
    _id string; anonymous visitors get a per-browser-session guest id that
    lets them chat -- and keep chat history -- without an account, right up
    until they sign up (at which point that history is claimed by the new
    account, see auth._claim_guest_history)."""
    if session.get("user_id"):
        return session["user_id"], False
    if "guest_id" not in session:
        session["guest_id"] = str(uuid.uuid4())
        session["guest_count"] = 0
        session["guest_prompt_shown"] = False
    return f"guest:{session['guest_id']}", True


def _save_message(user_id, conversation_id, role, text):
    if db.messages is None:
        return
    db.messages.insert_one({
        "user_id": user_id,
        "conversation_id": conversation_id,
        "role": role,
        "text": text,
        "created_at": datetime.now(timezone.utc),
    })


def _is_overload_error(e):
    message = str(e)
    return "503" in message or "UNAVAILABLE" in message


def _send_with_backoff(chat, message_text):
    last_error = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return chat.send_message(message_text)
        except ServerError as e:
            last_error = e
            if not _is_overload_error(e) or attempt == RETRY_ATTEMPTS - 1:
                raise
            delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning("Model overloaded (attempt %d/%d), retrying in %ds", attempt + 1, RETRY_ATTEMPTS, delay)
            time.sleep(delay)
    raise last_error


def send_message_with_fallback(entry, message_text):
    while True:
        try:
            return _send_with_backoff(entry["chat"], message_text)
        except ServerError as e:
            if not _is_overload_error(e):
                raise
            next_index = entry["model_index"] + 1
            if next_index >= len(FALLBACK_MODELS):
                raise
            prior_history = entry["chat"].get_history()
            new_model = FALLBACK_MODELS[next_index]
            logger.warning("%s still overloaded -- switching to %s", FALLBACK_MODELS[entry["model_index"]], new_model)
            entry["chat"] = _create_chat(new_model, history=prior_history)
            entry["model_index"] = next_index
            _advance_active_model_index(next_index)


@app.route("/")
def home():
    ensure_conversation()
    storage_user_id, is_guest = current_identity()
    plan = "free" if is_guest else plans.get_plan(storage_user_id)
    return render_template(
        "index.html",
        user_name=session.get("user_name", ""),
        is_guest=is_guest,
        plan=plan,
        is_admin=bool(session.get("is_admin")),
    )


@app.route("/chat", methods=["POST"])
def chat_endpoint():
    ensure_conversation()
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()

    if not user_message:
        return jsonify({"error": "Please enter a question before sending."}), 400
    if len(user_message) > 4000:
        return jsonify({"error": "That message is too long (4000 character limit)."}), 400
    if client is None:
        return jsonify({"error": "The AI service is not configured yet. Please set GEMINI_API_KEY to enable responses."}), 503

    user_id, is_guest = current_identity()
    conversation_id = session["conversation_id"]

    # Usage limits: guests get a soft popup at GUEST_POPUP_AFTER and a hard
    # stop at GUEST_MESSAGE_LIMIT; logged-in Free/Pro accounts get a daily cap.
    if is_guest:
        if session.get("guest_count", 0) >= plans.GUEST_MESSAGE_LIMIT:
            return jsonify({
                "error": "You've reached today's guest limit. Create a free account to keep chatting and save your history.",
                "limit_reached": True,
                "signup_url": url_for("auth.signup"),
            }), 403
    elif not session.get("is_admin"):
        user_plan = plans.get_plan(user_id)
        limit = plans.daily_limit_for(user_plan)
        if plans.messages_used_today(user_id) >= limit:
            return jsonify({
                "error": f"You've reached today's {user_plan.capitalize()} plan limit ({limit} messages)."
                         + ("" if user_plan == "pro" else " Upgrade to Itegeko Pro for a much higher daily limit."),
                "limit_reached": True,
                "upgrade_url": url_for("pricing"),
            }), 403

    entry = get_active_conversation(user_id)

    # Ground the model in whatever's in the gazette collection, if anything
    # matches -- this is invisible to the user and to the stored history;
    # only the plain question gets saved and shown.
    grounding = gazette.build_grounding_context(user_message)
    if grounding:
        if STRICT_GAZETTE_ONLY:
            instruction = (
                "Answer using ONLY the gazette excerpts above. Cite the specific act/title. "
                "If the excerpts don't fully answer the question, say plainly what's missing "
                "rather than filling the gap from general knowledge."
            )
        else:
            instruction = (
                "Use the gazette excerpts above as primary grounding and cite them directly; "
                "you may supplement with general knowledge only where they don't cover the question."
            )
        message_to_send = f"{grounding}\n\n{instruction}\n\nQuestion: {user_message}"
    elif STRICT_GAZETTE_ONLY:
        message_to_send = (
            "No matching Official Gazette document was found in the database for this question. "
            "Reply that you don't have an official document on file covering this yet, and don't "
            f"answer from general knowledge. Question: {user_message}"
        )
    else:
        message_to_send = user_message

    try:
        response = send_message_with_fallback(entry, message_to_send)
        reply_text = response.text
        _save_message(user_id, conversation_id, "user", user_message)
        _save_message(user_id, conversation_id, "model", reply_text)

        extra = {}
        if is_guest:
            session["guest_count"] = session.get("guest_count", 0) + 1
            if session["guest_count"] >= plans.GUEST_POPUP_AFTER and not session.get("guest_prompt_shown"):
                session["guest_prompt_shown"] = True
                extra["show_signup_prompt"] = True

        return jsonify({"response": reply_text, **extra})

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
            return jsonify({"error": "There's a configuration problem with the API key. Please contact the site administrator."}), 400
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

    except Exception:
        logger.exception("Unexpected error in /chat")
        return jsonify({"error": "Something went wrong on our end. Please try again."}), 500


@app.route("/chat/history", methods=["GET"])
def chat_history():
    ensure_conversation()
    user_id, _ = current_identity()
    db_messages = _load_history_from_db(user_id, session["conversation_id"])
    history = [
        {"role": "user" if m["role"] == "user" else "assistant", "text": m["text"], "time": m["created_at"].strftime("%H:%M")}
        for m in db_messages
    ]
    return jsonify({"history": history})


@app.route("/chat/new", methods=["POST"])
def new_chat():
    old_conversation_id = session.get("conversation_id")
    if old_conversation_id and old_conversation_id in _active_chats:
        del _active_chats[old_conversation_id]
    session["conversation_id"] = str(uuid.uuid4())
    return jsonify({"ok": True})


@app.route("/pricing")
def pricing():
    is_guest = not bool(session.get("user_id"))
    plan = "free" if is_guest else plans.get_plan(session["user_id"])
    return render_template(
        "pricing.html",
        logged_in=not is_guest,
        plan=plan,
        free_limit=plans.FREE_PLAN_DAILY_LIMIT,
        pro_limit=plans.PRO_PLAN_DAILY_LIMIT,
        price_label=payments.PRO_PRICE_LABEL,
        payments_configured=payments.is_configured(),
    )


@app.route("/upgrade", methods=["POST"])
@login_required
def upgrade():
    if not payments.is_configured():
        return render_template("pricing.html", logged_in=True, plan="free",
                                free_limit=plans.FREE_PLAN_DAILY_LIMIT, pro_limit=plans.PRO_PLAN_DAILY_LIMIT,
                                price_label=payments.PRO_PRICE_LABEL, payments_configured=False,
                                error="Payments aren't configured yet. Please contact the site administrator.")

    if db.users is None:
        return redirect(url_for("pricing"))

    try:
        user = db.users.find_one({"_id": ObjectId(session["user_id"])})
    except InvalidId:
        return redirect(url_for("pricing"))
    if not user:
        return redirect(url_for("auth.login"))

    try:
        checkout = payments.create_checkout(user, url_for("payment_callback", _external=True))
    except Exception:
        logger.exception("Failed to create Flutterwave checkout")
        return render_template("pricing.html", logged_in=True, plan="free",
                                free_limit=plans.FREE_PLAN_DAILY_LIMIT, pro_limit=plans.PRO_PLAN_DAILY_LIMIT,
                                price_label=payments.PRO_PRICE_LABEL, payments_configured=True,
                                error="Couldn't start checkout. Please try again shortly.")

    if db.payments is not None:
        db.payments.insert_one({
            "user_id": session["user_id"],
            "tx_ref": checkout["tx_ref"],
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        })

    return redirect(checkout["checkout_url"])


@app.route("/payment/callback")
@login_required
def payment_callback():
    status = request.args.get("status")
    transaction_id = request.args.get("transaction_id")
    tx_ref = request.args.get("tx_ref")

    def render_pricing(**kwargs):
        plan = plans.get_plan(session["user_id"])
        return render_template("pricing.html", logged_in=True, plan=plan,
                                free_limit=plans.FREE_PLAN_DAILY_LIMIT, pro_limit=plans.PRO_PLAN_DAILY_LIMIT,
                                price_label=payments.PRO_PRICE_LABEL, payments_configured=payments.is_configured(),
                                **kwargs)

    if status != "successful" or not transaction_id:
        return render_pricing(error="Payment was not completed.")

    try:
        ok, tx = payments.verify_transaction(transaction_id)
    except Exception:
        logger.exception("Flutterwave verification request failed")
        return render_pricing(error="We couldn't verify that payment right now. If you were charged, contact support.")

    if not ok:
        return render_pricing(error="We couldn't verify that payment. If you were charged, contact support.")

    pro_until = datetime.now(timezone.utc) + timedelta(days=payments.SUBSCRIPTION_DAYS)
    if db.users is not None:
        db.users.update_one({"_id": ObjectId(session["user_id"])}, {"$set": {"plan": "pro", "pro_until": pro_until}})
    if db.payments is not None and tx_ref:
        db.payments.update_one(
            {"tx_ref": tx_ref},
            {"$set": {"status": "successful", "verified_at": datetime.now(timezone.utc), "flw_transaction_id": transaction_id}},
        )
    session["plan"] = "pro"
    return render_pricing(success="Payment successful — you're now on Itegeko Pro!")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "gemini_configured": client is not None,
        "db_configured": db.db is not None,
        "strict_gazette_only": STRICT_GAZETTE_ONLY,
        "fallback_chain": FALLBACK_MODELS,
        "active_model": FALLBACK_MODELS[_get_active_model_index()],
        "active_chats": len(_active_chats),
    })


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug_mode)
