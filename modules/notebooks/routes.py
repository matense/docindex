"""Notebook module routes: list, editor, autosave, history."""

import difflib

from flask import (Blueprint, abort, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Drive, Folder
from app.services import file_service, hashtag_service, indexing_service

from . import doc as doc_service

bp = Blueprint("notebooks", __name__,
               template_folder="templates", static_folder="static")


def _get_notebook(file_id):
    stored = doc_service.get_notebook(file_id, current_user.id)
    if stored is None:
        abort(404)
    return stored


def _guard_writable(stored):
    try:
        doc_service._guard_writable(stored)
    except ValueError as exc:
        abort(400, str(exc))


def _locations():
    """(value, label) choices for the create form: roots + folders of every
    non-synced drive owned by the user."""
    choices = []
    drives = (Drive.query
              .filter_by(user_id=current_user.id)
              .order_by(Drive.name).all())
    for drive in drives:
        if drive.is_synced:
            continue
        choices.append((f"drive:{drive.id}", f"{drive.name} /"))
        folders = (Folder.query
                   .filter_by(user_id=current_user.id, drive_id=drive.id)
                   .order_by(Folder.name).all())
        for folder in folders:
            label = " / ".join(f.name for f in folder.breadcrumb())
            choices.append((f"folder:{folder.id}", f"{drive.name} / {label}"))
    return choices


@bp.get("/")
def index():
    notebooks = doc_service.list_notebooks(current_user.id)
    enriched = []
    for stored in notebooks:
        enriched.append({
            "file": stored,
            "cells": doc_service.cell_count(stored),
            "tags": hashtag_service.get_tags(stored.index),
        })
    return render_template("notebooks/list.html",
                           notebooks=enriched, locations=_locations())


@bp.post("/new")
def new():
    name = request.form.get("name", "")
    location = request.form.get("location", "")
    drive, folder = None, None
    if location.startswith("drive:"):
        drive = db.session.get(Drive, int(location[6:]))
    elif location.startswith("folder:"):
        folder = db.session.get(Folder, int(location[7:]))
        drive = folder.drive if folder else None
    if drive is None or drive.user_id != current_user.id:
        abort(404)
    if folder is not None and folder.user_id != current_user.id:
        abort(404)
    try:
        stored = doc_service.create_notebook(current_user, drive, name,
                                             folder=folder)
    except ValueError as exc:
        abort(400, str(exc))
    return redirect(url_for("notebooks.editor", file_id=stored.id))


@bp.get("/<int:file_id>")
def editor(file_id):
    stored = _get_notebook(file_id)
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        abort(400, str(exc))
    return render_template("notebooks/edit.html", file=stored, doc=document)


@bp.post("/<int:file_id>/save")
def save(file_id):
    stored = _get_notebook(file_id)
    _guard_writable(stored)
    payload = request.get_json(silent=True) or {}
    document = payload.get("doc")
    if document is None:
        return jsonify({"ok": False, "error": "Missing document."}), 400
    try:
        snapshotted = doc_service.save_document(
            stored, document,
            force_checkpoint=bool(payload.get("force_checkpoint")),
            note=str(payload.get("note", ""))[:255])
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "version_created": snapshotted,
                    "saved_at": stored.updated_at.isoformat()})


@bp.post("/<int:file_id>/rename")
def rename(file_id):
    stored = _get_notebook(file_id)
    _guard_writable(stored)
    name = (request.form.get("name") or "").strip()
    if not name:
        abort(400, "Name is required.")
    if not name.lower().endswith(f".{doc_service.EXTENSION}"):
        name = f"{name}.{doc_service.EXTENSION}"
    stored.name = name
    db.session.commit()
    from app.services import search_service
    search_service.fts_upsert(stored.id)  # filename is part of the FTS index
    return redirect(url_for("notebooks.index"))


@bp.post("/<int:file_id>/delete")
def delete(file_id):
    stored = _get_notebook(file_id)
    file_service.delete_file(stored)  # trash — restorable from the profile page
    return redirect(url_for("notebooks.index"))


@bp.get("/<int:file_id>/history")
def history(file_id):
    stored = _get_notebook(file_id)
    return render_template("notebooks/history.html", file=stored,
                           versions=stored.versions)


@bp.get("/<int:file_id>/history/<int:version_id>/diff")
def history_diff(file_id, version_id):
    stored = _get_notebook(file_id)
    version = next((v for v in stored.versions if v.id == version_id), None)
    if version is None:
        abort(404)
    with open(file_service.version_path(version), "r",
              encoding="utf-8", errors="replace") as fh:
        old_doc = doc_service.parse(fh.read())
    old_text = doc_service.serialize(old_doc).splitlines()
    new_text = doc_service.serialize(doc_service.load(stored)).splitlines()
    diff = list(difflib.unified_diff(
        old_text, new_text, lineterm="",
        fromfile=f"v{version.version}", tofile="current"))
    return render_template("notebooks/diff.html", file=stored,
                           version=version, diff=diff)


@bp.post("/<int:file_id>/history/<int:version_id>/restore")
def history_restore(file_id, version_id):
    stored = _get_notebook(file_id)
    _guard_writable(stored)
    version = next((v for v in stored.versions if v.id == version_id), None)
    if version is None:
        abort(404)
    file_service.restore_version(stored, version)
    indexing_service.enqueue_index([stored.id])
    return redirect(url_for("notebooks.editor", file_id=stored.id))
