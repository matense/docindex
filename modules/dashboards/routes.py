"""Dashboard module routes: list, editor, save, attachments, history."""

import difflib

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import current_user

from app.extensions import db
from app.models import Drive, Folder
from app.services import file_service, indexing_service

from . import doc as doc_service

bp = Blueprint("dashboards", __name__,
               template_folder="templates", static_folder="static")


def _get_dashboard(file_id):
    stored = doc_service.get_dashboard(file_id, current_user.id)
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
            choices.append((f"folder:{folder.id}",
                            f"{drive.name} / {label}"))
    return choices


@bp.get("/")
def index():
    dashboards = doc_service.list_dashboards(current_user.id)
    enriched = [{"file": stored,
                 "cards": len(doc_service.load(stored)["cards"])
                 if _readable(stored) else 0}
                for stored in dashboards]
    return render_template("dashboards/list.html",
                           dashboards=enriched,
                           locations=_locations())


def _readable(stored):
    try:
        doc_service.load(stored)
        return True
    except (ValueError, OSError):
        return False


@bp.post("/new")
def new():
    name = (request.form.get("name")
            or request.form.get("title")
            or "Untitled dashboard").strip()
    drive, folder = None, None
    location = (request.form.get("location") or "").strip()
    if location.startswith("drive:"):
        drive = db.session.get(Drive, int(location[6:]))
    elif location.startswith("folder:"):
        folder = db.session.get(Folder, int(location[7:]))
        drive = folder.drive if folder else None
    else:
        raw_drive = (request.form.get("drive_id") or "").strip()
        raw_folder = (request.form.get("folder_id") or "").strip()
        if raw_drive:
            drive = db.session.get(Drive, int(raw_drive))
        if raw_folder:
            folder = db.session.get(Folder, int(raw_folder))
            drive = folder.drive if folder else drive
    if drive is None:
        # No explicit target: first writable drive owned by the user.
        drive = (Drive.query
                 .filter_by(user_id=current_user.id)
                 .order_by(Drive.name).all())
        drive = next((d for d in drive if not d.is_synced), None)
    if drive is None or drive.user_id != current_user.id:
        abort(404)
    if folder is not None and folder.user_id != current_user.id:
        abort(404)
    try:
        stored = doc_service.create_dashboard(current_user, drive, name,
                                              folder=folder)
    except ValueError as exc:
        abort(400, str(exc))
    return redirect(url_for("dashboards.editor", file_id=stored.id))


@bp.get("/<int:file_id>")
def editor(file_id):
    stored = _get_dashboard(file_id)
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        abort(400, str(exc))
    return render_template("dashboards/edit.html", file=stored, doc=document)


@bp.get("/<int:file_id>/doc")
def document_json(file_id):
    """JSON document + timestamp — used by the editor to pick up external
    (e.g. AI tool) changes while the dashboard is open."""
    stored = _get_dashboard(file_id)
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
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    payload = request.get_json(silent=True) or {}
    document = payload.get("doc")
    if document is None:
        return jsonify({"ok": False, "error": "Missing document."}), 400
    try:
        doc_service.save_document(
            stored, document,
            force_checkpoint=bool(payload.get("force_checkpoint")))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True,
                    "saved_at": stored.updated_at.isoformat(),
                    "checksum": stored.checksum})


@bp.post("/<int:file_id>/attachments")
def attachments(file_id):
    """Attach a file: multipart upload (dashboard-owned) or {"file_id": N}
    link to an existing owned file. Returns the attachment entry."""
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    upload = request.files.get("file")
    if upload is not None and upload.filename:
        try:
            att = doc_service.attach_upload(stored, upload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "attachment": att})
    payload = request.get_json(silent=True) or {}
    target_id = payload.get("file_id") or request.form.get("file_id")
    if target_id is None:
        return jsonify({"ok": False, "error": "Missing file."}), 400
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid file id."}), 400
    try:
        att = doc_service.attach_link(stored, target_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "attachment": att})


@bp.delete("/<int:file_id>/attachments/<att_id>")
def detach_attachment(file_id, att_id):
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    try:
        result = doc_service.detach_attachment(stored, att_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@bp.get("/<int:file_id>/history")
def history(file_id):
    stored = _get_dashboard(file_id)
    return render_template("dashboards/history.html", file=stored,
                           versions=stored.versions)


def _card_text(card):
    """Human-readable text of a card, used for diffing."""
    lines = [f"[{card.get('type', '?')}] {card.get('title', '')}"]
    if card.get("prompt"):
        lines.append(str(card["prompt"]))
    text = doc_service._result_text(card.get("result"))
    if text:
        lines.append(text)
    return "\n".join(lines)


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


def _card_diff(old_doc, new_doc):
    """Align cards by id and classify each as added / changed / removed.

    Returns (summary, entries); every entry carries side-by-side `rows`
    (left = old version, right = current). Removed cards come last, since
    they no longer have a position in the document. Result-only changes
    (result/status/ranAt/runId) never snapshot, so they read as unchanged.
    """
    old_by_id = {c.get("id"): (i, c)
                 for i, c in enumerate(old_doc.get("cards", []))}
    entries = []
    added = changed = 0
    for i, card in enumerate(new_doc.get("cards", [])):
        label = f"{card.get('type', '?')} · card {i + 1}"
        prev = old_by_id.pop(card.get("id"), None)
        if prev is None:
            added += 1
            entries.append({"kind": "added", "label": label,
                            "rows": _side_by_side_rows("", _card_text(card))})
            continue
        _old_i, old_card = prev
        old_text, new_text = _card_text(old_card), _card_text(card)
        if (old_card.get("type") == card.get("type")
                and old_text == new_text
                and _structural_card(old_card) == _structural_card(card)):
            continue
        changed += 1
        rows = _side_by_side_rows(old_text, new_text)
        if not any(r.get("cls", "same") != "same" for r in rows):
            # Only settings changed (size, position, tools toggle, ...).
            rows = [{"left": "~ card settings changed",
                     "right": "~ card settings changed", "cls": "same"}]
        entries.append({"kind": "changed", "label": label, "rows": rows})
    removed = 0
    for old_i, old_card in old_by_id.values():
        removed += 1
        entries.append({"kind": "removed",
                        "label": f"{old_card.get('type', '?')} · "
                                 f"was card {old_i + 1}",
                        "rows": _side_by_side_rows(
                            _card_text(old_card), "")})
    summary = {"added": added, "changed": changed, "removed": removed,
               "unchanged": len(new_doc.get("cards", [])) - added - changed,
               "title_changed": old_doc.get("title") != new_doc.get("title"),
               "old_title": old_doc.get("title", ""),
               "new_title": new_doc.get("title", "")}
    return summary, entries


def _structural_card(card):
    return {k: v for k, v in card.items()
            if k not in doc_service.VOLATILE_CARD_KEYS and k != "id"}


@bp.get("/<int:file_id>/history/<int:version_id>/diff")
def history_diff(file_id, version_id):
    stored = _get_dashboard(file_id)
    version = next((v for v in stored.versions if v.id == version_id), None)
    if version is None:
        abort(404)
    with open(file_service.version_path(version),
              encoding="utf-8", errors="replace") as fh:
        old_doc = doc_service.parse(fh.read())
    summary, entries = _card_diff(old_doc, doc_service.load(stored))
    return render_template("dashboards/diff.html", file=stored,
                           version=version, summary=summary, entries=entries)


@bp.post("/<int:file_id>/history/<int:version_id>/restore")
def history_restore(file_id, version_id):
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    version = next((v for v in stored.versions if v.id == version_id), None)
    if version is None:
        abort(404)
    file_service.restore_version(stored, version)
    indexing_service.enqueue_index([stored.id])
    flash("Dashboard restored.", "success")
    return redirect(url_for("dashboards.editor", file_id=stored.id))
