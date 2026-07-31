"""
Official Gazette storage: PDF files (via GridFS) + extracted text (via a
MongoDB $text index) for grounding Itegeko's answers.

Two things live per document:
1. The original PDF, in GridFS (db.fs.files / db.fs.chunks) -- MongoDB's
   built-in mechanism for files over the normal 16MB document limit.
2. An entry in the `gazette` collection with the *extracted text* (so it's
   searchable) plus a reference to the GridFS file id (so the original PDF
   can still be downloaded/viewed).

`store_pdf` and `update_pdf` are how you keep this updatable: re-uploading
under the same title replaces both the GridFS file and the extracted text,
so search/grounding immediately reflects the new version.
"""
import io
import logging
from datetime import datetime, timezone

import gridfs
from pypdf import PdfReader

import db

logger = logging.getLogger("itegeko.gazette")

_fs = None


def _get_fs():
    global _fs
    if _fs is None and db.db is not None:
        _fs = gridfs.GridFS(db.db)
    return _fs


def _extract_text(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()


def store_pdf(file_bytes, filename, title, act_number="", category="", tags=None):
    """Insert a brand-new gazette document. Raises ValueError if a document
    with this exact title already exists -- use update_pdf for that instead,
    so callers can't accidentally create duplicates."""
    if db.gazette is None:
        raise RuntimeError("No MongoDB connection configured.")

    fs = _get_fs()
    if db.gazette.find_one({"title": title}):
        raise ValueError(f"A document titled '{title}' already exists. Use update_pdf to replace it.")

    full_text = _extract_text(file_bytes)
    file_id = fs.put(file_bytes, filename=filename, content_type="application/pdf")

    db.gazette.insert_one({
        "title": title,
        "act_number": act_number,
        "category": category,
        "tags": tags or [],
        "full_text": full_text,
        "pdf_file_id": file_id,
        "pdf_filename": filename,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })
    logger.info("Stored new gazette document '%s' (%d chars extracted)", title, len(full_text))
    return file_id


def update_pdf(title, file_bytes, filename, act_number=None, category=None, tags=None):
    """Replace an existing document's PDF and extracted text in place,
    keyed by title. Raises ValueError if no document with that title exists."""
    if db.gazette is None:
        raise RuntimeError("No MongoDB connection configured.")

    fs = _get_fs()
    existing = db.gazette.find_one({"title": title})
    if not existing:
        raise ValueError(f"No document titled '{title}' exists yet. Use store_pdf to create it.")

    # Remove the old GridFS file before writing the new one, so we don't
    # accumulate orphaned versions every time someone re-uploads.
    old_file_id = existing.get("pdf_file_id")
    if old_file_id:
        try:
            fs.delete(old_file_id)
        except gridfs.errors.NoFile:
            pass

    full_text = _extract_text(file_bytes)
    new_file_id = fs.put(file_bytes, filename=filename, content_type="application/pdf")

    update_fields = {
        "full_text": full_text,
        "pdf_file_id": new_file_id,
        "pdf_filename": filename,
        "updated_at": datetime.now(timezone.utc),
    }
    if act_number is not None:
        update_fields["act_number"] = act_number
    if category is not None:
        update_fields["category"] = category
    if tags is not None:
        update_fields["tags"] = tags

    db.gazette.update_one({"title": title}, {"$set": update_fields})
    logger.info("Updated gazette document '%s' (%d chars extracted)", title, len(full_text))
    return new_file_id


def delete_document(title):
    if db.gazette is None:
        return False
    fs = _get_fs()
    existing = db.gazette.find_one({"title": title})
    if not existing:
        return False
    if existing.get("pdf_file_id"):
        try:
            fs.delete(existing["pdf_file_id"])
        except gridfs.errors.NoFile:
            pass
    db.gazette.delete_one({"title": title})
    return True


def list_documents():
    if db.gazette is None:
        return []
    return list(
        db.gazette.find({}, {"title": 1, "act_number": 1, "category": 1, "tags": 1, "updated_at": 1, "pdf_filename": 1})
        .sort("updated_at", -1)
    )


def get_pdf_file(file_id):
    """Returns a GridOut object (has .read(), .filename, .content_type) or
    None if not found -- callers stream this back as the download response."""
    fs = _get_fs()
    if fs is None:
        return None
    try:
        return fs.get(file_id)
    except gridfs.errors.NoFile:
        return None


# ---------------------------------------------------------------------------
# Search + grounding for the chat endpoint.
# ---------------------------------------------------------------------------

def search_gazette(query, limit=3):
    """Return up to `limit` gazette entries relevant to `query`, best match
    first. Returns [] if there's no database connection or no matches."""
    if db.gazette is None or not query:
        return []
    try:
        cursor = (
            db.gazette.find(
                {"$text": {"$search": query}},
                {"score": {"$meta": "textScore"}, "title": 1, "act_number": 1, "full_text": 1},
            )
            .sort([("score", {"$meta": "textScore"})])
            .limit(limit)
        )
        return list(cursor)
    except Exception:
        logger.exception("Gazette search failed")
        return []


def build_grounding_context(query, limit=3):
    """Format matching gazette entries as a short block of extra context.
    Returns "" if nothing relevant was found."""
    matches = search_gazette(query, limit=limit)
    if not matches:
        return ""

    lines = ["Relevant excerpts from the Official Gazette on file:"]
    for m in matches:
        title = m.get("title", "Untitled entry")
        act = m.get("act_number", "")
        text = (m.get("full_text") or "")[:1500]  # keep the prompt bounded
        header = f"- {title}" + (f" (Act {act})" if act else "")
        lines.append(f"{header}\n  {text}")
    return "\n".join(lines)
