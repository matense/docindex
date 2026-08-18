import difflib
import os

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import Drive, FileVersion, Folder, StoredFile
from ..services import ai_service, drive_service, file_service, indexing_service
from ..services import sync_service

bp = Blueprint("drive", __name__)


def _get_owned(model, obj_id):
    obj = db.session.get(model, obj_id)
    if not obj or obj.user_id != current_user.id:
        abort(404)
    return obj


def _get_file(file_id, include_trashed=False):
    """Fetch one of the user's files; trashed files are hidden by default."""
    stored = _get_owned(StoredFile, file_id)
    if not include_trashed and stored.deleted_at is not None:
        abort(404)
    return stored


def _guard_writable(stored):
    """Synced files map to real files on disk and are read-only."""
    if stored.is_synced:
        abort(400, "Synced drives are read-only — the file maps to a real file on disk.")


def _trigger_indexing(file_id):
    if current_app.config.get("INDEX_ASYNC", True):
        indexing_service.index_file_async(file_id, current_app._get_current_object())
    else:
        indexing_service.index_file(file_id)


def _id_list(field):
    """Parse a comma-separated form field into a list of ints."""
    raw = request.form.get(field, "")
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


def _delete_folder_recursive(folder):
    for child in list(folder.children):
        _delete_folder_recursive(child)
    for stored in list(folder.files):
        if stored.deleted_at is None:
            stored.folder_id = None  # folder row is removed; restore lands at root
            file_service.delete_file(stored)
    db.session.delete(folder)


def _is_descendant(folder, possible_ancestor):
    node = folder.parent
    while node is not None:
        if node.id == possible_ancestor.id:
            return True
        node = node.parent
    return False


@bp.route("/")
@bp.route("/folder/<int:folder_id>")
@login_required
def index(folder_id=None):
    drive = drive_service.get_current_drive(current_user)
    folder = _get_owned(Folder, folder_id) if folder_id else None

    view_mode = request.args.get("view")
    if view_mode in ("grid", "list", "tree"):
        session["drive_view"] = view_mode
    else:
        view_mode = session.get("drive_view", "grid")

    # Tree view always shows the whole drive hierarchy, from the roots.
    scope = None if view_mode == "tree" else folder_id
    folders = (Folder.query
               .filter_by(user_id=current_user.id, parent_id=scope,
                          drive_id=drive.id)
               .order_by(Folder.name).all())
    files = (StoredFile.query
             .filter_by(user_id=current_user.id, folder_id=scope,
                        drive_id=drive.id)
             .filter(StoredFile.deleted_at.is_(None))
             .order_by(StoredFile.name).all())
    return render_template(
        "drive/drive.html",
        folder=folder,
        folders=folders,
        files=files,
        drive=drive,
        view_mode=view_mode,
        dupe_checksums=file_service.duplicate_checksums(current_user.id),
        editable_exts=current_app.config["EDITABLE_EXTENSIONS"],
    )


@bp.route("/drives/create", methods=["POST"])
@login_required
def create_drive():
    drive, error = drive_service.create_drive(current_user,
                                              request.form.get("name", ""))
    if error:
        flash(error, "error")
    else:
        flash(f'Drive "{drive.name}" created.', "success")
    return redirect(request.form.get("next") or url_for("drive.index"))


@bp.route("/drives/sync-create", methods=["POST"])
@login_required
def sync_create():
    """Create a read-only drive that mirrors a local folder."""
    drive, job, error = sync_service.create_synced_drive(
        current_user, request.form.get("path", ""),
        captions_enabled=bool(request.form.get("captions_enabled")),
        index_workers=request.form.get("index_workers", type=int) or 1)
    if error:
        flash(error, "error")
    elif job.state == "done":
        s = job.stats
        flash(f'Synced drive "{drive.name}" created — {s["added"]} file(s) '
              f'found, {s["skipped"]} skipped. Indexing started.', "success")
    else:
        flash(f'Synced drive "{drive.name}" created — the folder is being '
              "scanned in the background.", "success")
    return redirect(url_for("drive.index"))


@bp.route("/drives/<int:drive_id>/sync", methods=["POST"])
@login_required
def sync_now(drive_id):
    """On-demand re-sync of a synced drive (runs in the background)."""
    drive = Drive.query.filter_by(id=drive_id, user_id=current_user.id).first()
    if not drive or not drive.is_synced:
        abort(404)
    job = sync_service.start_sync(drive)
    if job.state == "error":
        flash(job.error or "Sync failed.", "error")
    elif job.state == "done":
        s = job.stats
        flash(f'Sync complete: {s["added"]} added, {s["updated"]} updated, '
              f'{s["removed"]} removed, {s["skipped"]} skipped.', "success")
    else:
        flash("Sync started — progress is shown in the bottom-right corner.",
              "info")
    return redirect(request.form.get("next") or url_for("drive.index"))


def _get_synced_drive(drive_id):
    drive = Drive.query.filter_by(id=drive_id, user_id=current_user.id).first()
    if not drive or not drive.is_synced:
        abort(404)
    return drive


@bp.route("/drives/<int:drive_id>/sync/status")
@login_required
def sync_status(drive_id):
    drive = _get_synced_drive(drive_id)
    status = sync_service.get_status(drive.id)
    if status is None:
        return jsonify({"state": "idle", "drive_id": drive.id})
    return jsonify(status)


@bp.route("/drives/<int:drive_id>/sync/pause", methods=["POST"])
@login_required
def sync_pause(drive_id):
    drive = _get_synced_drive(drive_id)
    sync_service.pause_sync(drive.id)
    return jsonify(sync_service.get_status(drive.id) or {"state": "idle"})


@bp.route("/drives/<int:drive_id>/sync/resume", methods=["POST"])
@login_required
def sync_resume(drive_id):
    drive = _get_synced_drive(drive_id)
    sync_service.resume_sync(drive.id)
    return jsonify(sync_service.get_status(drive.id) or {"state": "idle"})


@bp.route("/drives/<int:drive_id>/sync/stop", methods=["POST"])
@login_required
def sync_stop(drive_id):
    drive = _get_synced_drive(drive_id)
    sync_service.cancel_sync(drive.id)
    return jsonify({"ok": True})


@bp.route("/drives/<int:drive_id>/sync-settings", methods=["POST"])
@login_required
def sync_settings(drive_id):
    """Per-drive sync options: AI captions toggle and indexing workers."""
    drive = _get_synced_drive(drive_id)
    drive.captions_enabled = bool(request.form.get("captions_enabled"))
    workers = request.form.get("index_workers", type=int)
    if workers:
        drive.index_workers = max(1, min(workers, 8))
    db.session.commit()
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": True, "captions_enabled": drive.captions_enabled,
                        "index_workers": drive.index_workers})
    flash("Sync settings saved.", "success")
    return redirect(request.form.get("next") or url_for("settings.profile"))


@bp.route("/drives/<int:drive_id>/remove", methods=["POST"])
@login_required
def remove_drive(drive_id):
    """Remove a synced drive: stops any running sync and clears every
    DocIndex row (files, folders, index) — the real folder is untouched."""
    drive = _get_synced_drive(drive_id)
    name = drive.name
    sync_service.remove_synced_drive(drive)
    session.pop("drive_id", None)  # pointed at the removed drive
    flash(f'Synced drive "{name}" removed. The folder on disk was not '
          "touched.", "success")
    return redirect(url_for("drive.index"))


@bp.route("/sync/active")
@login_required
def sync_active():
    """The current user's running/paused sync (for the global widget)."""
    job = sync_service.get_active_job(current_user.id)
    return jsonify({"active": job is not None, "job": job})


@bp.route("/drives/<int:drive_id>/select", methods=["POST"])
@login_required
def select_drive(drive_id):
    drive = drive_service.select_drive(current_user, drive_id)
    if not drive:
        abort(404)
    flash(f'Switched to drive "{drive.name}".', "success")
    return redirect(request.form.get("next") or url_for("drive.index"))


@bp.route("/drives/<int:drive_id>/edit", methods=["POST"])
@login_required
def edit_drive(drive_id):
    drive, error = drive_service.update_drive(
        current_user, drive_id,
        request.form.get("name", ""),
        request.form.get("description", ""),
    )
    flash(error, "error") if error else flash(f'Drive "{drive.name}" updated.', "success")
    return redirect(request.form.get("next") or url_for("drive.index"))


@bp.route("/upload", methods=["POST"])
@login_required
def upload():
    drive = drive_service.get_current_drive(current_user)
    if drive.is_synced:
        flash("Synced drives are read-only — upload to a regular drive instead.",
              "error")
        return redirect(url_for("drive.index"))
    folder = None
    folder_id = request.form.get("folder_id", type=int)
    if folder_id:
        folder = _get_owned(Folder, folder_id)

    uploaded = request.files.getlist("files")
    if not uploaded or not any(f.filename for f in uploaded):
        flash("No files selected.", "error")
        return redirect(request.referrer or url_for("drive.index"))

    saved, errors = 0, []
    saved_files = []
    for file_storage in uploaded:
        if not file_storage.filename:
            continue
        if not file_service.allowed_file(file_storage.filename):
            errors.append(f"{file_storage.filename}: file type not allowed")
            continue
        try:
            name = secure_filename(file_storage.filename) or "unnamed"
            existing = file_service.find_by_name(
                current_user.id, drive.id, folder.id if folder else None, name)
            if existing:
                # Same name in the same folder -> new version, not a duplicate.
                result = file_service.replace_with_upload(existing, file_storage)
                if result == "identical":
                    flash(f'"{name}" already exists with identical content — nothing to do.',
                          "info")
                else:
                    _trigger_indexing(existing.id)
                    flash(f'"{name}" updated — previous content kept as '
                          f'v{existing.versions[0].version} in the history.', "success")
                    saved += 1
                saved_files.append({"id": existing.id, "name": existing.name,
                                    "checksum": existing.checksum,
                                    "versioned": result == "replaced",
                                    "duplicates": []})
                continue
            stored = file_service.save_upload(file_storage, current_user, folder)
            stored.drive_id = drive.id
            db.session.commit()
            _trigger_indexing(stored.id)
            saved += 1
            dupes = file_service.find_duplicates(stored)
            saved_files.append({
                "id": stored.id,
                "name": stored.name,
                "checksum": stored.checksum,
                "versioned": False,
                "duplicates": [{"id": d.id, "name": d.name} for d in dupes],
            })
            for d in dupes:
                flash(f'⚠️ "{stored.name}" has identical content to '
                      f'"{d.name}" — open the file info to handle it.', "warning")
        except ValueError as exc:
            errors.append(str(exc))

    if saved:
        flash(f"{saved} file(s) uploaded. Indexing started.", "success")
    for err in errors:
        flash(err, "error")

    if request.accept_mimetypes.best == "application/json":
        return jsonify({"saved": saved, "errors": errors, "files": saved_files})
    return redirect(url_for("drive.index", folder_id=folder.id if folder else None))


@bp.route("/file/<int:file_id>/download")
@login_required
def download(file_id):
    stored = _get_file(file_id)
    return send_file(file_service.file_path(stored), as_attachment=True,
                     download_name=stored.name)


@bp.route("/file/<int:file_id>/raw")
@login_required
def raw(file_id):
    """Serve the file inline (used by previews)."""
    stored = _get_file(file_id)
    return send_file(file_service.file_path(stored), as_attachment=False,
                     download_name=stored.name)


@bp.route("/file/<int:file_id>/thumbnail")
@login_required
def thumbnail(file_id):
    stored = _get_file(file_id)
    path = file_service.thumbnail_path(stored)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


@bp.route("/file/<int:file_id>/rename", methods=["POST"])
@login_required
def rename(file_id):
    stored = _get_file(file_id)
    _guard_writable(stored)
    new_name = request.form.get("name", "").strip()
    if not new_name:
        flash("Name cannot be empty.", "error")
    else:
        if "." not in new_name and stored.extension:
            new_name = f"{new_name}.{stored.extension}"
        stored.name = new_name
        db.session.commit()
        flash("File renamed.", "success")
    return redirect(url_for("drive.index", folder_id=stored.folder_id))


@bp.route("/file/<int:file_id>/move", methods=["POST"])
@login_required
def move(file_id):
    stored = _get_file(file_id)
    _guard_writable(stored)
    folder_id = request.form.get("folder_id", type=int)
    if folder_id:
        dest = _get_owned(Folder, folder_id)
        if dest.drive and dest.drive.is_synced:
            abort(400, "Synced drives are read-only.")
    stored.folder_id = folder_id
    db.session.commit()
    flash("File moved.", "success")
    return redirect(url_for("drive.index", folder_id=stored.folder_id))


@bp.route("/file/<int:file_id>/delete", methods=["POST"])
@login_required
def delete(file_id):
    stored = _get_file(file_id)
    _guard_writable(stored)
    folder_id = stored.folder_id
    file_service.delete_file(stored)
    flash(f'"{stored.name}" moved to the trash — restore it from your profile.',
          "success")
    return redirect(url_for("drive.index", folder_id=folder_id))


# ---------- Trash (soft-deleted files) ----------

@bp.route("/file/<int:file_id>/restore", methods=["POST"])
@login_required
def restore(file_id):
    stored = _get_file(file_id, include_trashed=True)
    _guard_writable(stored)
    file_service.restore_file(stored)
    flash(f'"{stored.name}" restored.', "success")
    return redirect(url_for("settings.profile"))


@bp.route("/file/<int:file_id>/purge", methods=["POST"])
@login_required
def purge(file_id):
    stored = _get_file(file_id, include_trashed=True)
    _guard_writable(stored)
    name = stored.name
    file_service.purge_file(stored)
    flash(f'"{name}" permanently deleted.', "success")
    return redirect(url_for("settings.profile"))


@bp.route("/trash/empty", methods=["POST"])
@login_required
def empty_trash():
    trashed = file_service.trashed_files(current_user.id)
    for stored in trashed:
        file_service.purge_file(stored)
    flash(f"Trash emptied — {len(trashed)} file(s) permanently deleted.", "success")
    return redirect(url_for("settings.profile"))


@bp.route("/file/<int:file_id>/view")
@login_required
def view(file_id):
    """In-app document viewer for all file types."""
    stored = _get_file(file_id)
    content = None
    rendered = None
    if stored.extension == "md":
        import markdown
        content = file_service.read_text_content(stored)
        rendered = markdown.markdown(content, extensions=["fenced_code", "tables"])
    elif stored.is_editable:
        content = file_service.read_text_content(stored)
    elif stored.index and stored.index.extracted_text:
        # e.g. DOCX: show the extracted text
        content = stored.index.extracted_text
    return render_template("drive/view.html", file=stored,
                           content=content, rendered=rendered)


@bp.route("/file/<int:file_id>/edit", methods=["GET", "POST"])
@login_required
def edit(file_id):
    stored = _get_file(file_id)
    _guard_writable(stored)
    if not stored.is_editable:
        abort(400)
    if request.method == "POST":
        content = request.form.get("content", "")
        file_service.update_file_content(stored, content)
        _trigger_indexing(stored.id)
        flash("File saved. Re-indexing started.", "success")
        return redirect(url_for("drive.index", folder_id=stored.folder_id))
    content = file_service.read_text_content(stored)
    return render_template("drive/edit.html", file=stored, content=content)


@bp.route("/file/<int:file_id>/reindex", methods=["POST"])
@login_required
def reindex(file_id):
    stored = _get_file(file_id)
    _trigger_indexing(stored.id)
    flash("Re-indexing started.", "success")
    return redirect(url_for("drive.index", folder_id=stored.folder_id))


@bp.route("/file/<int:file_id>/info")
@login_required
def info(file_id):
    stored = _get_file(file_id)
    index = stored.index
    dupes = file_service.find_duplicates(stored)
    return jsonify({
        "id": stored.id,
        "name": stored.name,
        "extension": stored.extension,
        "size": stored.size,
        "mime_type": stored.mime_type,
        "checksum": stored.checksum,
        "created_at": stored.created_at.isoformat() if stored.created_at else None,
        "index_status": index.status if index else "none",
        "caption": index.caption if index else None,
        "word_count": index.word_count if index else None,
        "line_count": index.line_count if index else None,
        "char_count": index.char_count if index else None,
        "duplicates": [
            {"id": d.id, "name": d.name,
             "created_at": d.created_at.isoformat() if d.created_at else None}
            for d in dupes
        ],
        "is_image": stored.is_image,
        "is_editable": stored.is_editable,
        "has_thumbnail": file_service.has_thumbnail(stored),
    })


# ---------- Version history ----------

def _get_version(stored, version_id):
    version = db.session.get(FileVersion, version_id)
    if not version or version.file_id != stored.id:
        abort(404)
    return version


def _version_text(version, max_chars=200_000):
    with open(file_service.version_path(version), "r",
              encoding="utf-8", errors="replace") as fh:
        return fh.read(max_chars)


def _diff_lines(old_text, new_text, old_label, new_label):
    return list(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile=old_label, tofile=new_label, lineterm=""))


@bp.route("/file/<int:file_id>/history/<int:version_id>/download")
@login_required
def version_download(file_id, version_id):
    stored = _get_file(file_id)
    version = _get_version(stored, version_id)
    stem = stored.name.rsplit(".", 1)[0] if "." in stored.name else stored.name
    download_name = (f"{stem}.v{version.version}.{stored.extension}"
                     if stored.extension else f"{stem}.v{version.version}")
    return send_file(file_service.version_path(version), as_attachment=True,
                     download_name=download_name)


@bp.route("/file/<int:file_id>/history/<int:version_id>/diff")
@login_required
def version_diff(file_id, version_id):
    """Unified diff of a past version against the current content (text only)."""
    stored = _get_file(file_id)
    if not stored.is_editable:
        abort(400)
    version = _get_version(stored, version_id)
    diff = _diff_lines(_version_text(version),
                       file_service.read_text_content(stored),
                       f"v{version.version}", "current")
    return render_template("drive/diff.html", file=stored, version=version,
                           diff=diff, title=f"v{version.version} → current")


@bp.route("/file/<int:file_id>/history/<int:version_id>/restore", methods=["POST"])
@login_required
def version_restore(file_id, version_id):
    """Roll a text file back to a past version (current state is snapshotted)."""
    stored = _get_file(file_id)
    _guard_writable(stored)
    if not stored.is_editable:
        abort(400)
    version = _get_version(stored, version_id)
    file_service.restore_version(stored, version)
    _trigger_indexing(stored.id)
    flash(f'"{stored.name}" restored to v{version.version} — the previous '
          "state was kept in the history.", "success")
    return redirect(url_for("drive.view", file_id=stored.id))


# ---------- AI-assisted merge (with user review) ----------

MERGE_TEXT_LIMIT = 20_000


def _get_merge_pair(file_id, other_id):
    stored = _get_file(file_id)
    other = _get_file(other_id)
    _guard_writable(stored)
    _guard_writable(other)
    if other.id == stored.id:
        abort(400)
    if not (stored.is_editable and other.is_editable):
        abort(400)
    return stored, other


@bp.route("/file/<int:file_id>/merge/<int:other_id>")
@login_required
def merge_review(file_id, other_id):
    stored, other = _get_merge_pair(file_id, other_id)
    return render_template("drive/merge.html", file=stored, other=other,
                           content=file_service.read_text_content(stored),
                           other_content=file_service.read_text_content(other))


@bp.route("/file/<int:file_id>/merge/<int:other_id>/ai", methods=["POST"])
@login_required
def merge_ai(file_id, other_id):
    """Ask the AI for a merged version of both texts (nothing is saved yet)."""
    stored, other = _get_merge_pair(file_id, other_id)
    if not ai_service.is_enabled(current_user):
        return jsonify({"ok": False,
                        "message": "AI is not configured. Add a connection in AI Settings."}), 400
    text_a = file_service.read_text_content(stored, MERGE_TEXT_LIMIT)
    text_b = file_service.read_text_content(other, MERGE_TEXT_LIMIT)
    messages = [
        {"role": "system", "content":
         "You merge two versions of a document into one coherent text. "
         "Keep all unique information from both, resolve overlaps, and output "
         "ONLY the merged document content — no commentary, no code fences."},
        {"role": "user", "content":
         f"Merge these two files into one.\n\n"
         f"=== FILE A: {stored.name} ===\n{text_a}\n\n"
         f"=== FILE B: {other.name} ===\n{text_b}"},
    ]
    try:
        reply = ai_service.chat_completion(
            messages, config=ai_service.config_for(current_user))
    except ai_service.AIError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 502
    return jsonify({"ok": True, "merged": reply.get("content", "")})


@bp.route("/file/<int:file_id>/merge/<int:other_id>/preview", methods=["POST"])
@login_required
def merge_preview(file_id, other_id):
    """Unified diff of the current content vs a proposed merge (JSON)."""
    stored, other = _get_merge_pair(file_id, other_id)
    data = request.get_json(silent=True) or {}
    proposal = data.get("content", "")
    diff = _diff_lines(file_service.read_text_content(stored), proposal,
                       "current", "merged proposal")
    return jsonify({"ok": True, "diff": diff})


@bp.route("/file/<int:file_id>/merge/<int:other_id>/accept", methods=["POST"])
@login_required
def merge_accept(file_id, other_id):
    """Save the reviewed merge as the file's content (history is kept)."""
    stored, other = _get_merge_pair(file_id, other_id)
    content = request.form.get("content", "")
    file_service.update_file_content(stored, content, source="merge",
                                     note=f"Merged with '{other.name}'")
    _trigger_indexing(stored.id)
    if request.form.get("delete_other"):
        file_service.delete_file(other)
        flash(f'Merged — "{other.name}" was moved to the trash.', "success")
    else:
        flash("Merged — both files kept, history preserved.", "success")
    return redirect(url_for("drive.view", file_id=stored.id))


@bp.route("/folder/create", methods=["POST"])
@login_required
def create_folder():
    drive = drive_service.get_current_drive(current_user)
    if drive.is_synced:
        flash("Synced drives are read-only — folders mirror the real folder.",
              "error")
        return redirect(url_for("drive.index"))
    name = request.form.get("name", "").strip()
    parent_id = request.form.get("parent_id", type=int)
    parent = _get_owned(Folder, parent_id) if parent_id else None
    if not name:
        flash("Folder name cannot be empty.", "error")
    else:
        db.session.add(Folder(name=name, parent_id=parent.id if parent else None,
                              user_id=current_user.id, drive_id=drive.id))
        db.session.commit()
        flash("Folder created.", "success")
    return redirect(url_for("drive.index", folder_id=parent.id if parent else None))


@bp.route("/selection/delete", methods=["POST"])
@login_required
def delete_selection():
    deleted = 0
    for fid in _id_list("file_ids"):
        stored = (StoredFile.query
                  .filter_by(id=fid, user_id=current_user.id)
                  .filter(StoredFile.deleted_at.is_(None),
                          StoredFile.source_path.is_(None))  # synced = read-only
                  .first())
        if stored:
            file_service.delete_file(stored)
            deleted += 1
    for fid in _id_list("folder_ids"):
        folder = Folder.query.filter_by(id=fid, user_id=current_user.id).first()
        if folder and not (folder.drive and folder.drive.is_synced):
            _delete_folder_recursive(folder)
            deleted += 1
    db.session.commit()
    return jsonify({"ok": True, "deleted": deleted})


@bp.route("/selection/move", methods=["POST"])
@login_required
def move_selection():
    dest_id = request.form.get("dest", type=int)
    dest = None
    if dest_id:
        dest = _get_owned(Folder, dest_id)
        if dest.drive and dest.drive.is_synced:
            return jsonify({"ok": False,
                            "error": "Synced drives are read-only."}), 400
    moved = 0
    skipped = 0
    for fid in _id_list("file_ids"):
        stored = (StoredFile.query
                  .filter_by(id=fid, user_id=current_user.id)
                  .filter(StoredFile.deleted_at.is_(None),
                          StoredFile.source_path.is_(None))  # synced = read-only
                  .first())
        if stored:
            stored.folder_id = dest.id if dest else None
            moved += 1
    for fid in _id_list("folder_ids"):
        folder = Folder.query.filter_by(id=fid, user_id=current_user.id).first()
        if not folder or (folder.drive and folder.drive.is_synced):
            continue
        # Guard: cannot move a folder into itself or one of its descendants.
        if dest and (dest.id == folder.id or _is_descendant(dest, folder)):
            skipped += 1
            continue
        folder.parent_id = dest.id if dest else None
        moved += 1
    db.session.commit()
    return jsonify({"ok": True, "moved": moved, "skipped": skipped})


@bp.route("/folder/<int:folder_id>/delete", methods=["POST"])
@login_required
def delete_folder(folder_id):
    folder = _get_owned(Folder, folder_id)
    if folder.drive and folder.drive.is_synced:
        abort(400, "Synced drives are read-only.")
    parent_id = folder.parent_id
    _delete_folder_recursive(folder)
    db.session.commit()
    flash("Folder and its contents deleted.", "success")
    return redirect(url_for("drive.index", folder_id=parent_id))
