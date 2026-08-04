"""
Free / Pro plan logic.

Everyone starts on the Free plan. Free (and anonymous guest) usage is capped
to keep the underlying Gemini API bill predictable; Pro is effectively
unlimited (a high daily ceiling only to blunt abuse). All limits are
overridable via environment variables so you can tune them without a
code change.
"""
import os
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId

import db

# How many guest messages before we show the "create a free account" popup.
GUEST_POPUP_AFTER = int(os.environ.get("GUEST_POPUP_AFTER", "3"))

# Hard ceiling on anonymous (no-account) messages per browser session per day.
# This does NOT block at GUEST_POPUP_AFTER -- guests keep chatting after the
# popup, they just eventually hit this higher ceiling and are asked to sign up.
GUEST_MESSAGE_LIMIT = int(os.environ.get("GUEST_MESSAGE_LIMIT", "15"))

# Messages/day for a logged-in account on the Free plan.
FREE_PLAN_DAILY_LIMIT = int(os.environ.get("FREE_PLAN_DAILY_LIMIT", "30"))

# Messages/day for Pro -- generous, mainly an abuse backstop.
PRO_PLAN_DAILY_LIMIT = int(os.environ.get("PRO_PLAN_DAILY_LIMIT", "500"))


def get_plan(user_id):
    """Returns 'pro' or 'free' for a logged-in user. Auto-downgrades (and
    persists the downgrade) if a Pro subscription period has lapsed."""
    if db.users is None:
        return "free"
    try:
        user = db.users.find_one({"_id": ObjectId(user_id)})
    except (InvalidId, TypeError):
        return "free"
    if not user:
        return "free"

    if user.get("plan") == "pro":
        pro_until = user.get("pro_until")
        if pro_until:
            if pro_until.tzinfo is None:
                pro_until = pro_until.replace(tzinfo=timezone.utc)
            if pro_until > datetime.now(timezone.utc):
                return "pro"
        # Subscription lapsed -- quietly move them back to Free.
        db.users.update_one({"_id": user["_id"]}, {"$set": {"plan": "free"}})
    return "free"


def messages_used_today(user_id):
    if db.messages is None:
        return 0
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.messages.count_documents({
        "user_id": user_id,
        "role": "user",
        "created_at": {"$gte": start_of_day},
    })


def daily_limit_for(plan):
    return PRO_PLAN_DAILY_LIMIT if plan == "pro" else FREE_PLAN_DAILY_LIMIT
