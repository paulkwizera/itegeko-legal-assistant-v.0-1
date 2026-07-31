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


def init_db():
    global client, db, users, messages, gazette

    if not MONGODB_URI:
        logger.warning("MONGODB_URI not set -- accounts, chat history, and gazette search are disabled.")
        return

    client = MongoClient(MONGODB_URI, server_api=ServerApi("1"))
    db = client[MONGODB_DB_NAME]

    users = db.users
    messages = db.messages
    gazette = db.gazette

    # Indexes are idempotent -- safe to call every startup.
    users.create_index("email", unique=True)
    messages.create_index([("user_id", ASCENDING), ("conversation_id", ASCENDING), ("created_at", ASCENDING)])
    gazette.create_index([("title", TEXT), ("full_text", TEXT), ("tags", TEXT)])

    # Fails fast and loudly on startup if the URI/credentials/network access
    # are wrong, instead of surfacing as a mystery 500 on the first request.
    client.admin.command("ping")
    logger.info("Connected to MongoDB database '%s'", MONGODB_DB_NAME)


init_db()
