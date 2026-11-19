"""Whiteboard module routes: list, editor, scene API."""

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Drive
from app.services import file_service

from . import board as board_service

bp = Blueprint("whiteboards", __name__,
               template_folder="templates", static_folder="static")


def _get_board(file_id):
    stored = board_service.get_board(file_id, current_user.id)
    if stored is None:
        abort(404)
    return stored


def _writable_drives():
    # Local (non-synced) drives only — mirrors create in notebooks.
    return (Drive.query
            .filter_by(user_id=current_user.id)
            .filter(Drive.source_path.is_(None))
            .order_by(Drive.name).all())


@bp.get("/")
@login_required
def index():
    boards = board_service.list_boards(current_user.id)
    drives = _writable_drives()
    drives.sort(key=lambda d: 0 if d.name == "My Whiteboards" else 1)
    return render_template("whiteboards/index.html", boards=boards,
                           drives=drives,
                           revertable=board_service.last_organize_plan(
                               current_user) is not None)


@bp.get("/organize/plan")
@login_required
def organize_plan():
    """Preview: whiteboards that would move, and where. No changes made —
    the user must confirm (POST /organize/apply) for anything to move."""
    moves, skipped = board_service.organize_preview(current_user)
    return jsonify({"moves": moves, "skipped_synced": skipped})


@bp.post("/organize/apply")
@login_required
def organize_apply():
    """Execute the organize plan (explicit confirmation only — this is the
    only path that moves whiteboards)."""
    record, applied, skipped = board_service.apply_organize(current_user)
    if applied:
        flash(f"Organized {applied} whiteboard(s) into 'DocIndex Whiteboards' "
              f"folders." + (f" {skipped} skipped." if skipped else ""),
              "success")
    else:
        flash("Nothing to organize — every whiteboard is already in place.",
              "info")
    return redirect(url_for("whiteboards.index"))


@bp.post("/organize/revert")
@login_required
def organize_revert():
    """Undo the last organize: replay the inverse moves of its logged plan."""
    moved, skipped, found = board_service.revert_organize(current_user)
    if not found:
        flash("No organize run to revert.", "info")
    elif moved:
        flash(f"Reverted: {moved} whiteboard(s) moved back."
              + (f" {skipped} skipped." if skipped else ""), "success")
    else:
        flash("Revert found nothing to move back.", "info")
    return redirect(url_for("whiteboards.index"))


@bp.post("/create")
@login_required
def create():
    name = (request.form.get("name") or "").strip() or "Untitled board"
    try:
        drive_id = int(request.form.get("drive_id") or 0)
    except (TypeError, ValueError):
        drive_id = 0
    drive = db.session.get(Drive, drive_id)
    if drive is None or drive.user_id != current_user.id:
        flash("Pick one of your drives for the new board.", "error")
        return redirect(url_for("whiteboards.index"))
    # Drive-root intent: every drive keeps its whiteboards in "DocIndex
    # Whiteboards" (the form picks a drive only — there is no
    # explicit-folder picker here by design).
    folder = board_service.whiteboards_folder(current_user, drive)
    try:
        stored = board_service.create_board(current_user, drive, name,
                                             folder=folder)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("whiteboards.index"))
    return redirect(url_for("whiteboards.board", file_id=stored.id))


@bp.get("/<int:file_id>")
@login_required
def board(file_id):
    stored = _get_board(file_id)
    try:
        scene = board_service.load(stored)
    except (ValueError, OSError) as exc:
        flash(f"This board file is unreadable: {exc}", "error")
        return redirect(url_for("whiteboards.index"))
    return render_template("whiteboards/board.html", board=stored,
                           scene=scene,
                           version=board_service.scene_version(stored))


@bp.get("/api/<int:file_id>/scene")
@login_required
def api_scene(file_id):
    stored = _get_board(file_id)
    try:
        scene = board_service.load(stored)
    except (ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"scene": scene,
                    "version": board_service.scene_version(stored)})


@bp.put("/api/<int:file_id>/scene")
@login_required
def api_save(file_id):
    stored = _get_board(file_id)
    data = request.get_json(silent=True) or {}
    try:
        current = board_service.load(stored)
    except (ValueError, OSError) as exc:
        return jsonify({"error": f"Board file is unreadable: {exc}"}), 400
    err = board_service.ai_error(stored, write=True)
    # The lock lives in the scene: honor it for API saves too (the editor
    # unlocks first via the lock endpoint).
    if err:
        return jsonify({"error": err}), 403
    scene = dict(current)
    if "elements" in data:
        if not isinstance(data["elements"], list):
            return jsonify({"error": "'elements' must be a list."}), 400
        scene["elements"] = data["elements"]
    if isinstance(data.get("appState"), dict):
        scene["appState"] = data["appState"]
    if isinstance(data.get("title"), str) and data["title"].strip():
        scene["title"] = data["title"].strip()[:120]
    checkpoint = request.args.get("checkpoint") == "1"
    try:
        saved = board_service.save_board(
            stored, scene, note="Editor save", source="edit",
            skip_snapshot=not checkpoint)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True,
                    "version": board_service.scene_version(stored),
                    "elements": len(saved["elements"])})


@bp.post("/<int:file_id>/delete")
@login_required
def delete(file_id):
    """Move a board to the trash (restorable from the Trash page)."""
    stored = _get_board(file_id)
    name = stored.name
    file_service.delete_file(stored)
    flash(f'"{name}" moved to the trash — restore it from the Trash page.',
          "success")
    return redirect(url_for("whiteboards.index"))


@bp.post("/<int:file_id>/lock")
@login_required
def lock(file_id):
    return _set_flag(file_id, "ai_lock", True, "locked")


@bp.post("/<int:file_id>/unlock")
@login_required
def unlock(file_id):
    return _set_flag(file_id, "ai_lock", False, "unlocked")


@bp.post("/<int:file_id>/hide")
@login_required
def hide(file_id):
    return _set_flag(file_id, "ai_hidden", True, "hidden from the AI")


@bp.post("/<int:file_id>/unhide")
@login_required
def unhide(file_id):
    return _set_flag(file_id, "ai_hidden", False, "visible to the AI")


def _set_flag(file_id, flag, value, verb):
    stored = _get_board(file_id)
    try:
        scene = board_service.load(stored)
    except (ValueError, OSError) as exc:
        flash(f"This board file is unreadable: {exc}", "error")
        return redirect(url_for("whiteboards.index"))
    if stored.drive and stored.drive.is_readonly:
        flash("Synced drives are read-only.", "error")
        return redirect(url_for("whiteboards.board", file_id=file_id))
    scene[flag] = value
    board_service.save_board(stored, scene, note=f"AI {verb}",
                             source="edit", skip_snapshot=True)
    flash(f"Board {verb}.", "success")
    wants_json = request.args.get("format") == "json"
    if wants_json or request.headers.get("Accept", "").startswith(
            "application/json"):
        return jsonify({"ok": True, flag: value})
    return redirect(url_for("whiteboards.board", file_id=file_id))
