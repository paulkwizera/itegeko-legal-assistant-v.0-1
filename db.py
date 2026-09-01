"""
MongoDB connection and collection setup for Itegeko.

Requires MONGODB_URI in the environment (see .env.example). If it's not set,
`db` stays None and every route that needs it should fail gracefully rather
than crashing the whole app -- this keeps local development possible without
a database, and matches the same "degrade gracefully, don't crash" approach
already used for the missing-GEMINI_API_KEY case in app.py.
"""
import os
import logging

from pymongo import MongoClient, ASCENDING, DESCENDING, TEXT
from pymongo.server_api import ServerApi

logger = logging.getLogger("itegeko.db")

MONGODB_URI = os.environ.get("MONGODB_URI")
MONGODB_DB_NAME = os.environ.get("MONGODB_DB_NAME", "itegeko")

client = None
db = None
users = None
messages = None
gazette = None
payments = None
conversations = None
tokens = None


def init_db():
    global client, db, users, messages, gazette, payments, conversations, tokens

    if not MONGODB_URI:
        logger.warning("MONGODB_URI not set -- accounts, chat history, and gazette search are disabled.")
        return

    try:
        new_client = MongoClient(MONGODB_URI, server_api=ServerApi("1"))
        new_db = new_client[MONGODB_DB_NAME]

        new_users = new_db.users
        new_messages = new_db.messages
        new_gazette = new_db.gazette
        new_payments = new_db.payments
        new_conversations = new_db.conversations
        new_tokens = new_db.tokens

        # Indexes are idempotent -- safe to call every startup.
        new_users.create_index("email", unique=True)
        new_messages.create_index([("user_id", ASCENDING), ("conversation_id", ASCENDING), ("created_at", ASCENDING)])
        # Weekly usage queries need an efficient path
        new_messages.create_index([("user_id", ASCENDING), ("role", ASCENDING), ("created_at", DESCENDING)])
        new_gazette.create_index([("title", TEXT), ("full_text", TEXT), ("tags", TEXT)])
        new_payments.create_index("tx_ref", unique=True)
        new_payments.create_index("user_id")
        # Conversation listing: most recent first per user
        new_conversations.create_index([("user_id", ASCENDING), ("updated_at", DESCENDING)])
        # Single-conversation lookups (load/rename/delete) key on conversation_id
        # directly -- it's already a globally-unique UUID, so this index also
        # doubles as an integrity constraint.
        new_conversations.create_index("conversation_id", unique=True)
        # Password reset / email verification tokens with TTL (auto-expire after 1 hour)
        new_tokens.create_index("token", unique=True)
        new_tokens.create_index("expires_at", expireAfterSeconds=0)

        # Confirms the URI/credentials/network access actually work, rather
        # than only discovering a bad connection on the first real request.
        new_client.admin.command("ping")

    except Exception:
        # Degrade the same way as "MONGODB_URI not set": every route already
        # checks `if db.users is None` etc. and responds gracefully, so a
        # transient network/auth problem at boot shouldn't take the whole
        # app down -- it should just come up with accounts/history/gazette
        # disabled until the next restart (or a manual retry) succeeds.
        logger.exception("Could not connect to MongoDB at startup -- accounts, chat history, and gazette search are disabled for this process.")
        client = db = users = messages = gazette = payments = conversations = tokens = None
        return

    client, db = new_client, new_db
    users, messages, gazette = new_users, new_messages, new_gazette
    payments, conversations, tokens = new_payments, new_conversations, new_tokens
    logger.info("Connected to MongoDB database '%s'", MONGODB_DB_NAME)


def migrate_guest_messages(guest_id, user_id):
    """Called right after a guest signs up or logs in -- re-labels any chat
    history they built up anonymously (stored under 'guest:<guest_id>') so it
    becomes part of their new account's history instead of being lost."""
    if messages is None or not guest_id:
        return
    messages.update_many({"user_id": f"guest:{guest_id}"}, {"$set": {"user_id": user_id}})
    # Also migrate any conversation records
    if conversations is not None:
        conversations.update_many({"user_id": f"guest:{guest_id}"}, {"$set": {"user_id": user_id}})


init_db()
