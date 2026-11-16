"""Notebook module routes: list, editor, autosave, history."""

import difflib
import json
import time

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import Drive, Folder, StoredFile
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
    non-synced drive owned by the user. The dedicated notebooks drive comes
    first so it is the default pick."""
    choices = []
    drives = (Drive.query
              .filter_by(user_id=current_user.id)
              .order_by(Drive.name).all())
    drives.sort(key=lambda d: 0 if d.name == "My Notebooks" else 1)
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
                           notebooks=enriched,
                           groups=_group_by_location(enriched),
                           locations=_locations(),
                           revertable=doc_service.last_organize_plan(
                               current_user) is not None)


def _group_by_location(enriched):
    """Notebook cards grouped by drive, then folder within each drive."""
    drives, order = {}, []
    for nb in enriched:
        stored = nb["file"]
        drive_name = stored.drive.name if stored.drive else "No drive"
        folder_name = stored.folder.name if stored.folder else "/"
        dkey = stored.drive_id
        if dkey not in drives:
            drives[dkey] = {"drive": drive_name, "folders": {},
                            "folder_order": [], "total": 0}
            order.append(dkey)
        group = drives[dkey]
        fkey = stored.folder_id
        if fkey not in group["folders"]:
            group["folders"][fkey] = {"folder": folder_name, "items": []}
            group["folder_order"].append(fkey)
        group["folders"][fkey]["items"].append(nb)
        group["total"] += 1
    return [{"drive_id": k, "drive": drives[k]["drive"],
             "total": drives[k]["total"],
             "folders": [dict(drives[k]["folders"][f],
                               folder_id=f)
                         for f in drives[k]["folder_order"]]}
            for k in order]


@bp.get("/organize/plan")
def organize_plan():
    """Preview: notebooks that would move, and where. No changes made —
    the user must confirm (POST /organize/apply) for anything to move."""
    moves, skipped = doc_service.organize_preview(current_user)
    return jsonify({"moves": moves, "skipped_synced": skipped})


@bp.post("/organize/apply")
def organize_apply():
    """Execute the organize plan (explicit confirmation only — this is the
    only path that moves notebooks)."""
    record, applied, skipped = doc_service.apply_organize(current_user)
    if applied:
        flash(f"Organized {applied} notebook(s) into 'DocIndex Notebooks' "
              f"folders." + (f" {skipped} skipped." if skipped else ""),
              "success")
    else:
        flash("Nothing to organize — every notebook is already in place.",
              "info")
    return redirect(url_for("notebooks.index"))


@bp.post("/organize/revert")
def organize_revert():
    """Undo the last organize: replay the inverse moves of its logged plan."""
    moved, skipped, found = doc_service.revert_organize(current_user)
    if not found:
        flash("No organize run to revert.", "info")
    elif moved:
        flash(f"Reverted: {moved} notebook(s) moved back."
              + (f" {skipped} skipped." if skipped else ""), "success")
    else:
        flash("Revert found nothing to move back.", "info")
    return redirect(url_for("notebooks.index"))


@bp.post("/new")
def new():
    name = request.form.get("name", "")
    location = request.form.get("location", "")
    drive, folder = None, None
    if location.startswith("drive:"):
        drive = db.session.get(Drive, int(location[6:]))
        if drive is not None:
            # Drive root means the drive's notebooks folder — every drive
            # keeps its notebooks in "DocIndex Notebooks".
            folder = doc_service.notebooks_folder(current_user, drive)
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
        # Validate shape first: malformed docs are 400 even when the save
        # carries no base (base rules below answer 409, never 400).
        doc_service.validate(document)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    base_checksum = payload.get("base_checksum") or None
    base_doc = payload.get("base_doc")
    if base_doc is not None and not isinstance(base_doc, dict):
        return jsonify({"ok": False, "error": "base_doc must be an object."}), 400
    if base_checksum is None and stored.checksum is not None:
        # No base: the saver cannot prove freshness — refuse the blind
        # overwrite (stale clients must refetch and retry with a base)
        # instead of silently wiping concurrent cells. Internal callers
        # (AI tools, sync) bypass this route with freshly loaded docs.
        try:
            server_doc = doc_service.load(stored)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": False,
                        "error": "Save carries no base checksum — "
                                 "reload and retry.",
                        "conflict": True, "doc": server_doc,
                        "checksum": stored.checksum}), 409
    try:
        result = doc_service.save_document(
            stored, document,
            force_checkpoint=bool(payload.get("force_checkpoint")),
            note=str(payload.get("note", ""))[:255],
            base_checksum=base_checksum, base_doc=base_doc)
    except doc_service.CellConflictError as exc:
        # Guard-snapshot the live (post-concurrent, e.g. AI) state so it
        # stays restorable even if the client rebases and overwrites it.
        file_service.snapshot_version(
            stored, "edit", note="Conflict guard - concurrent edit")
        return jsonify({"ok": False, "error": str(exc), "conflict": True,
                        "doc": exc.server_doc,
                        "checksum": exc.server_checksum}), 409
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    body = {"ok": True, "version_created": result.snapshotted,
            "merged": result.merged,
            "resurrected": result.resurrected,
            "saved_at": stored.updated_at.isoformat(),
            "checksum": stored.checksum}
    if result.merged:
        # The client needs the exact post-merge state as its next base.
        body["doc"] = result.doc
    return jsonify(body)


@bp.post("/<int:file_id>/rename")
def rename(file_id):
    stored = _get_notebook(file_id)
    _guard_writable(stored)
    name = (request.form.get("name") or "").strip()
    if not name:
        abort(400, "Name is required.")
    if not name.lower().endswith(f".{doc_service.EXTENSION}"):
        name = f"{name}.{doc_service.EXTENSION}"
    # Single-source rule: the file name and doc["title"] stay in sync both
    # ways — a rename writes the stripped title into the document as well.
    # The file name is assigned AFTER save_document: it refreshes the row
    # inside its write lock, which would discard an unflushed name change.
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        abort(400, str(exc))
    document["title"] = name[: -(len(doc_service.EXTENSION) + 1)]
    doc_service.save_document(stored, document)
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


@bp.post("/<int:file_id>/assets")
def upload_asset(file_id):
    """Store an image pasted into a rich-text cell as a real file next to
    the notebook (same drive + folder), so it is indexed and reusable."""
    stored = _get_notebook(file_id)
    _guard_writable(stored)
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "Missing file."}), 400
    if not (upload.mimetype or "").startswith("image/"):
        return jsonify({"ok": False, "error": "Only images are allowed."}), 400
    asset = file_service.save_upload(upload, current_user,
                                     folder=stored.folder)
    asset.drive_id = stored.drive_id
    db.session.commit()
    indexing_service.enqueue_index([asset.id])
    return jsonify({"ok": True, "id": asset.id, "name": asset.name,
                    "url": url_for("drive.raw", file_id=asset.id)})


@bp.post("/<int:file_id>/run-cell")
def run_cell(file_id):
    """Execute a code/SQL cell in the pyrun sandbox over attached files.

    Input files are positional: the first id is addressable as rows('f')
    (and 'f0'), the rest as 'f1', 'f2', ... SQL is wrapped as
    ``res = db.query(<sql>)`` so the query rows come back as the result.
    """
    from app.services import agent_service, module_service
    stored = _get_notebook(file_id)
    if not module_service.is_enabled("pyrun"):
        return jsonify({"ok": False,
                        "error": "The pyrun module is disabled — "
                                 "enable pyrun to run code cells."}), 403
    payload = request.get_json(silent=True) or {}
    lang = str(payload.get("lang") or "python").lower()
    if lang not in ("python", "py", "sql"):
        return jsonify({"ok": False,
                        "error": f"Unsupported language {lang!r}."}), 400
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        return jsonify({"ok": False, "error": "Missing code."}), 400
    raw_ids = payload.get("input_file_ids") or []
    if not isinstance(raw_ids, list):
        return jsonify({"ok": False,
                        "error": "input_file_ids must be a list."}), 400
    try:
        from docindex_module_pyrun.evaluate import evaluate
    except ImportError:
        return jsonify({"ok": False,
                        "error": "pyrun evaluator is unavailable."}), 500
    files_map = {}
    for pos, raw in enumerate(raw_ids):
        try:
            input_id = int(raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": f"Invalid file id {raw!r}."}), 400
        target = (StoredFile.query
                  .filter_by(id=input_id, user_id=current_user.id)
                  .filter(StoredFile.deleted_at.is_(None)).first())
        if target is None:
            return jsonify({"ok": False,
                            "error": f"File {input_id} not found."}), 404
        guarded = agent_service._file_guard_error(current_user, target)
        if guarded:
            return jsonify({"ok": False, "error": guarded}), 403
        try:
            chunks = [file_service.read_text_content(target)]
        except OSError:
            return jsonify({"ok": False,
                            "error": f"Could not read file {input_id}."}), 400
        if pos == 0:
            files_map["f"] = chunks
        files_map[f"f{pos}"] = chunks
    if lang == "sql":
        code = "res = db.query(" + json.dumps(code) + ")\nres"
    start = time.perf_counter()
    try:
        outcome = evaluate(code, files=files_map)
    except Exception:  # noqa: BLE001 - the tool boundary never 500s raw
        return jsonify({"ok": False,
                        "error": "Evaluation failed unexpectedly."}), 500
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if not outcome.get("ok"):
        return jsonify({"ok": False,
                        "error": outcome.get("error") or "Evaluation failed.",
                        "elapsed_ms": elapsed_ms})
    return jsonify({"ok": True, "stdout": "", "result": outcome.get("value"),
                    "elapsed_ms": elapsed_ms})


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
    if ctype == "chart":
        chart = meta.get("chart") or {}
        title = chart.get("title") or ""
        kind = chart.get("kind") or "bar"
        lines = [f"Chart ({kind}){': ' + title if title else ''}"]
        lines.append(" | ".join(str(label) for label in chart.get("labels", [])))
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
                            "cell_id": cell.get("id"),
                            "rows": _side_by_side_rows("", _cell_text(cell))})
            continue
        _old_i, old_cell = prev
        old_text, new_text = _cell_text(old_cell), _cell_text(cell)
        if (old_cell.get("type") == cell.get("type") and old_text == new_text
                and (old_cell.get("meta") or {}) == (cell.get("meta") or {})):
            continue
        changed += 1
        rows = _side_by_side_rows(old_text, new_text)
        if not any(r.get("cls", "same") != "same" for r in rows):
            # Only meta changed (language, heading level, ...). Collapse
            # markers have no "cls" — they stand for unchanged lines.
            rows = [{"left": "~ cell settings changed",
                     "right": "~ cell settings changed", "cls": "same"}]
        entries.append({"kind": "changed", "label": label,
                        "cell_id": cell.get("id"), "rows": rows})
    removed = 0
    for old_i, old_cell in old_by_id.values():
        removed += 1
        entries.append({"kind": "removed",
                        "label": f"{old_cell.get('type', '?')} · "
                                 f"was cell {old_i + 1}",
                        "cell_id": old_cell.get("id"),
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
    with open(file_service.version_path(version),
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


@bp.post("/<int:file_id>/history/<int:version_id>/restore-cell")
def history_restore_cell(file_id, version_id):
    """Restore a single cell from a past version, leaving every other cell
    untouched. A missing-in-current cell is re-inserted at its original
    position. Saves through save_document, so the pre-restore state stays
    a restorable snapshot (no history rewrite)."""
    stored = _get_notebook(file_id)
    _guard_writable(stored)
    version = next((v for v in stored.versions if v.id == version_id), None)
    if version is None:
        abort(404)
    payload = request.get_json(silent=True) or {}
    cell_id = payload.get("cell_id") or request.form.get("cell_id")
    if not cell_id:
        abort(400, "cell_id is required.")
    with open(file_service.version_path(version),
              encoding="utf-8", errors="replace") as fh:
        old_doc = doc_service.parse(fh.read())
    historic = next((c for c in old_doc.get("cells", [])
                     if c.get("id") == cell_id), None)
    if historic is None:
        abort(404)
    try:
        current = doc_service.load(stored)
    except ValueError as exc:
        abort(400, str(exc))
    ids = [c.get("id") for c in current.get("cells", [])]
    if cell_id in ids:
        current["cells"][ids.index(cell_id)] = historic
    else:
        position = next((i for i, c in enumerate(old_doc.get("cells", []))
                         if c.get("id") == cell_id),
                        len(current["cells"]))
        current["cells"].insert(min(position, len(current["cells"])),
                               historic)
    doc_service.save_document(
        stored, current, force_checkpoint=True,
        note=f"Before restore cell to v{version.version}")
    # No explicit enqueue_index: save_document already enqueues the reindex
    # (unlike file_service.restore_version used by history_restore, which
    # needs its own call).
    return redirect(url_for("notebooks.editor", file_id=stored.id))
