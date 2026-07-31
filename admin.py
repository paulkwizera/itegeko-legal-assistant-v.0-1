"""
Admin routes for managing Official Gazette PDFs: upload, replace, delete,
download, and list what's currently in the database.

Gated by admin_required (see auth.py) -- a plain logged-in user gets a 403.
To make your own account an admin, open MongoDB Atlas -> Browse Collections
-> <your db> -> users -> find your user document -> edit it and add:
    "is_admin": true
then log out and back in (is_admin is only loaded into the session at
login time).
"""
import io
import logging

from flask import Blueprint, render_template, request, redirect, url_for, send_file, flash

import gazette
from auth import admin_required

logger = logging.getLogger("itegeko.admin")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/documents", methods=["GET"])
@admin_required
def list_documents():
    docs = gazette.list_documents()
    return render_template("admin_documents.html", docs=docs)


@admin_bp.route("/documents/upload", methods=["POST"])
@admin_required
def upload_document():
    title = (request.form.get("title") or "").strip()
    act_number = (request.form.get("act_number") or "").strip()
    category = (request.form.get("category") or "").strip()
    tags_raw = (request.form.get("tags") or "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    mode = request.form.get("mode", "create")  # "create" or "update"

    file = request.files.get("pdf_file")
    if not title or not file or file.filename == "":
        flash("Title and a PDF file are required.", "error")
        return redirect(url_for("admin.list_documents"))

    file_bytes = file.read()

    try:
        if mode == "update":
            gazette.update_pdf(title, file_bytes, file.filename, act_number, category, tags)
            flash(f"Updated '{title}'.", "success")
        else:
            gazette.store_pdf(file_bytes, file.filename, title, act_number, category, tags)
            flash(f"Added '{title}'.", "success")
    except ValueError as e:
        logger.warning("Gazette upload rejected: %s", e)
        flash(str(e), "error")
    except Exception:
        logger.exception("Gazette upload failed")
        flash("Upload failed -- check the server logs for details.", "error")

    return redirect(url_for("admin.list_documents"))


@admin_bp.route("/documents/delete", methods=["POST"])
@admin_required
def delete_document():
    title = request.form.get("title", "")
    if title:
        gazette.delete_document(title)
    return redirect(url_for("admin.list_documents"))


@admin_bp.route("/documents/download/<file_id>", methods=["GET"])
@admin_required
def download_document(file_id):
    from bson import ObjectId
    grid_out = gazette.get_pdf_file(ObjectId(file_id))
    if grid_out is None:
        return "File not found", 404
    return send_file(
        io.BytesIO(grid_out.read()),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=grid_out.filename or "document.pdf",
    )
