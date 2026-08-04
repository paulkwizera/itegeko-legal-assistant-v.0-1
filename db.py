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

from pymongo import MongoClient, ASCENDING, TEXT
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


def init_db():
    global client, db, users, messages, gazette, payments

    if not MONGODB_URI:
        logger.warning("MONGODB_URI not set -- accounts, chat history, and gazette search are disabled.")
        return

    client = MongoClient(MONGODB_URI, server_api=ServerApi("1"))
    db = client[MONGODB_DB_NAME]

    users = db.users
    messages = db.messages
    gazette = db.gazette
    payments = db.payments

    # Indexes are idempotent -- safe to call every startup.
    users.create_index("email", unique=True)
    messages.create_index([("user_id", ASCENDING), ("conversation_id", ASCENDING), ("created_at", ASCENDING)])
    gazette.create_index([("title", TEXT), ("full_text", TEXT), ("tags", TEXT)])
    payments.create_index("tx_ref", unique=True)
    payments.create_index("user_id")

    # Fails fast and loudly on startup if the URI/credentials/network access
    # are wrong, instead of surfacing as a mystery 500 on the first request.
    client.admin.command("ping")
    logger.info("Connected to MongoDB database '%s'", MONGODB_DB_NAME)


def migrate_guest_messages(guest_id, user_id):
    """Called right after a guest signs up or logs in -- re-labels any chat
    history they built up anonymously (stored under 'guest:<guest_id>') so it
    becomes part of their new account's history instead of being lost."""
    if messages is None or not guest_id:
        return
    messages.update_many({"user_id": f"guest:{guest_id}"}, {"$set": {"user_id": user_id}})


init_db()
