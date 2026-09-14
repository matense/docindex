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


@bp.get("/<int:file_id>/doc")
def document_json(file_id):
    """JSON document + timestamp — used by the editor to pick up external
    (e.g. AI tool) changes while the notebook is open."""
    stored = _get_notebook(file_id)
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        abort(400, str(exc))
    return jsonify({"ok": True, "doc": document,
                    "checksum": stored.checksum,
                    "updated_at": (stored.updated_at.isoformat()
                                   if stored.updated_at else None)})


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
                    "saved_at": stored.updated_at.isoformat(),
                    "checksum": stored.checksum})


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


def _cell_text(cell):
    """Human-readable text of a cell, used for diffing."""
    ctype = cell.get("type")
    meta = cell.get("meta") or {}
    if ctype == "todo":
        return "\n".join(
            ("[x] " if i.get("done") else "[ ] ") + str(i.get("text", ""))
            for i in meta.get("items", []))
    if ctype == "table":
        lines = [" | ".join(str(h) for h in meta.get("headers", []))]
        lines += [" | ".join(str(c) for c in r) for r in meta.get("rows", [])]
        return "\n".join(lines)
    if ctype == "separator":
        return ""
    if ctype == "heading":
        prefix = "#" if meta.get("level") != 2 else "##"
        return f"{prefix} {cell.get('content', '')}"
    return cell.get("content", "")


def _side_by_side_rows(old_text, new_text):
    """Pair old/new lines for a side-by-side view using SequenceMatcher
    opcodes. Each row: {left, right, cls} where cls is same/del/add and a
    side is None when the line only exists on the other side."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    rows = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            run = i2 - i1
            if run > 6:
                # Collapse long unchanged stretches to keep the diff short.
                for k in range(3):
                    rows.append({"left": old_lines[i1 + k],
                                 "right": new_lines[j1 + k], "cls": "same"})
                rows.append({"collapse": run - 6})
                for k in range(i2 - 3, i2):
                    rows.append({"left": old_lines[k],
                                 "right": new_lines[j2 - (i2 - k)],
                                 "cls": "same"})
            else:
                for k in range(run):
                    rows.append({"left": old_lines[i1 + k],
                                 "right": new_lines[j1 + k], "cls": "same"})
        else:
            if tag in ("delete", "replace"):
                for line in old_lines[i1:i2]:
                    rows.append({"left": line, "right": None, "cls": "del"})
            if tag in ("insert", "replace"):
                for line in new_lines[j1:j2]:
                    rows.append({"left": None, "right": line, "cls": "add"})
    return rows


def _cell_diff(old_doc, new_doc):
    """Align cells by id and classify each as added / changed / removed.

    Returns (summary, entries); every entry carries side-by-side `rows`
    (left = old version, right = current). Removed cells come last, since
    they no longer have a position in the document.
    """
    old_by_id = {c.get("id"): (i, c)
                 for i, c in enumerate(old_doc.get("cells", []))}
    entries = []
    added = changed = 0
    for i, cell in enumerate(new_doc.get("cells", [])):
        label = f"{cell.get('type', '?')} · cell {i + 1}"
        prev = old_by_id.pop(cell.get("id"), None)
        if prev is None:
            added += 1
            entries.append({"kind": "added", "label": label,
                            "rows": _side_by_side_rows("", _cell_text(cell))})
            continue
        _old_i, old_cell = prev
        old_text, new_text = _cell_text(old_cell), _cell_text(cell)
        if (old_cell.get("type") == cell.get("type") and old_text == new_text
                and (old_cell.get("meta") or {}) == (cell.get("meta") or {})):
            continue
        changed += 1
        rows = _side_by_side_rows(old_text, new_text)
        if not any(r["cls"] != "same" for r in rows):
            # Only meta changed (language, heading level, ...).
            rows = [{"left": "~ cell settings changed",
                     "right": "~ cell settings changed", "cls": "same"}]
        entries.append({"kind": "changed", "label": label, "rows": rows})
    removed = 0
    for old_i, old_cell in old_by_id.values():
        removed += 1
        entries.append({"kind": "removed",
                        "label": f"{old_cell.get('type', '?')} · "
                                 f"was cell {old_i + 1}",
                        "rows": _side_by_side_rows(
                            _cell_text(old_cell), "")})
    summary = {"added": added, "changed": changed, "removed": removed,
               "unchanged": len(new_doc.get("cells", [])) - added - changed,
               "title_changed": old_doc.get("title") != new_doc.get("title"),
               "old_title": old_doc.get("title", ""),
               "new_title": new_doc.get("title", "")}
    return summary, entries


@bp.get("/<int:file_id>/history/<int:version_id>/diff")
def history_diff(file_id, version_id):
    stored = _get_notebook(file_id)
    version = next((v for v in stored.versions if v.id == version_id), None)
    if version is None:
        abort(404)
    with open(file_service.version_path(version), "r",
              encoding="utf-8", errors="replace") as fh:
        old_doc = doc_service.parse(fh.read())
    summary, entries = _cell_diff(old_doc, doc_service.load(stored))
    return render_template("notebooks/diff.html", file=stored,
                           version=version, summary=summary, entries=entries)


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
