"""
Law firm / lawyer directory.

Itegeko only gives general legal information, not representation -- this
lets the admin curate a list of real firms people can actually hire,
shown publicly at /lawyers. Admin-managed only: added and removed from
/admin/firms, never edited by end users.
"""
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId

import db


def list_firms():
    if db.firms is None:
        return []
    return list(db.firms.find().sort("name", 1))


def add_firm(name, phone, address, email="", website="", specialty="", description=""):
    if db.firms is None:
        raise ValueError("The database isn't configured, so firms can't be saved right now.")

    name = (name or "").strip()
    phone = (phone or "").strip()
    address = (address or "").strip()
    if not name or not phone or not address:
        raise ValueError("Name, phone number, and address are required.")

    doc = {
        "name": name,
        "phone": phone,
        "address": address,
        "email": (email or "").strip(),
        "website": (website or "").strip(),
        "specialty": (specialty or "").strip(),
        "description": (description or "").strip(),
        "created_at": datetime.now(timezone.utc),
    }
    result = db.firms.insert_one(doc)
    return str(result.inserted_id)


def delete_firm(firm_id):
    if db.firms is None:
        return False
    try:
        object_id = ObjectId(firm_id)
    except InvalidId:
        return False
    result = db.firms.delete_one({"_id": object_id})
    return result.deleted_count > 0
