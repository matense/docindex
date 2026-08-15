import os

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Folder, StoredFile
from ..services import drive_service, file_service, indexing_service

bp = Blueprint("drive", __name__)


def _get_owned(model, obj_id):
    obj = db.session.get(model, obj_id)
    if not obj or obj.user_id != current_user.id:
        abort(404)
    return obj


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
    stored = _get_owned(StoredFile, file_id)
    return send_file(file_service.file_path(stored), as_attachment=True,
                     download_name=stored.name)


@bp.route("/file/<int:file_id>/raw")
@login_required
def raw(file_id):
    """Serve the file inline (used by previews)."""
    stored = _get_owned(StoredFile, file_id)
    return send_file(file_service.file_path(stored), as_attachment=False,
                     download_name=stored.name)


@bp.route("/file/<int:file_id>/thumbnail")
@login_required
def thumbnail(file_id):
    stored = _get_owned(StoredFile, file_id)
    path = file_service.thumbnail_path(stored)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


@bp.route("/file/<int:file_id>/rename", methods=["POST"])
@login_required
def rename(file_id):
    stored = _get_owned(StoredFile, file_id)
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
    stored = _get_owned(StoredFile, file_id)
    folder_id = request.form.get("folder_id", type=int)
    if folder_id:
        _get_owned(Folder, folder_id)
    stored.folder_id = folder_id
    db.session.commit()
    flash("File moved.", "success")
    return redirect(url_for("drive.index", folder_id=stored.folder_id))


@bp.route("/file/<int:file_id>/delete", methods=["POST"])
@login_required
def delete(file_id):
    stored = _get_owned(StoredFile, file_id)
    folder_id = stored.folder_id
    file_service.delete_file(stored)
    flash("File deleted.", "success")
    return redirect(url_for("drive.index", folder_id=folder_id))


@bp.route("/file/<int:file_id>/view")
@login_required
def view(file_id):
    """In-app document viewer for all file types."""
    stored = _get_owned(StoredFile, file_id)
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
    stored = _get_owned(StoredFile, file_id)
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
    stored = _get_owned(StoredFile, file_id)
    _trigger_indexing(stored.id)
    flash("Re-indexing started.", "success")
    return redirect(url_for("drive.index", folder_id=stored.folder_id))


@bp.route("/file/<int:file_id>/info")
@login_required
def info(file_id):
    stored = _get_owned(StoredFile, file_id)
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


@bp.route("/folder/create", methods=["POST"])
@login_required
def create_folder():
    drive = drive_service.get_current_drive(current_user)
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
        stored = StoredFile.query.filter_by(id=fid, user_id=current_user.id).first()
        if stored:
            file_service.delete_file(stored)
            deleted += 1
    for fid in _id_list("folder_ids"):
        folder = Folder.query.filter_by(id=fid, user_id=current_user.id).first()
        if folder:
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
    moved = 0
    skipped = 0
    for fid in _id_list("file_ids"):
        stored = StoredFile.query.filter_by(id=fid, user_id=current_user.id).first()
        if stored:
            stored.folder_id = dest.id if dest else None
            moved += 1
    for fid in _id_list("folder_ids"):
        folder = Folder.query.filter_by(id=fid, user_id=current_user.id).first()
        if not folder:
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
    parent_id = folder.parent_id
    _delete_folder_recursive(folder)
    db.session.commit()
    flash("Folder and its contents deleted.", "success")
    return redirect(url_for("drive.index", folder_id=parent_id))
