"""Git drives: clone a remote repository as a read-only mirror drive."""


from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Drive, StoredFile
from app.services import ai_service, drive_service, sync_service

from . import source as git_source

bp = Blueprint("gitdrives", __name__,
               template_folder="templates", static_folder="static")


def _owned_git_drive(drive_id):
    drive = Drive.query.filter_by(id=drive_id,
                                  user_id=current_user.id).first()
    if drive is None or drive.source_kind != "git":
        from flask import abort
        abort(404)
    return drive


@bp.get("/")
@login_required
def index():
    drives = (Drive.query
              .filter_by(user_id=current_user.id)
              .filter(Drive.source_kind == "git")
              .filter(Drive.deleted_at.is_(None))
              .order_by(Drive.name).all())
    lfs = {}
    for drive in drives:
        try:
            total, sample = git_source.find_lfs_pointers(
                git_source.cache_path(drive.id))
        except Exception:  # noqa: BLE001 - advisory only, never break the page
            total, sample = 0, []
        if total:
            lfs[drive.id] = {"total": total, "sample": sample}
    return render_template("gitdrives/index.html", drives=drives, lfs=lfs)


@bp.post("/create")
@login_required
def create():
    """Validate the remote, create the drive row and clone (mirror)."""
    url = (request.form.get("repo_url") or "").strip()
    branch = (request.form.get("branch") or "").strip() or None
    username = (request.form.get("username") or "").strip() or None
    token = (request.form.get("token") or "").strip() or None
    provider = drive_service.get_drive_source("git")
    error = provider.validate(current_user, {
        "repo_url": url, "branch": branch,
        "username": username, "token": token})
    if error:
        flash(error, "error")
        return redirect(url_for("gitdrives.index"))

    name = (request.form.get("name") or "").strip()
    if not name:
        # Friendly default from the repo URL (…/org/my-repo[.git]).
        tail = url.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if tail.endswith(".git"):
            tail = tail[:-4]
        name = tail or "Git repo"
    base, suffix, candidate = name, 2, name
    while Drive.query.filter(Drive.user_id == current_user.id,
                            Drive.name.ilike(candidate)).first():
        candidate = f"{base} ({suffix})"
        suffix += 1

    drive = Drive(name=candidate, user_id=current_user.id,
                  source_kind="git", repo_url=url,
                  repo_branch=branch, repo_username=username,
                  repo_token=ai_service.encrypt_api_key(token or ""),
                  captions_enabled=False,
                  description=f"Git mirror of {url}")
    db.session.add(drive)
    db.session.commit()
    # The checkout dir doubles as source_path: the regular folder scan
    # indexes it like any synced drive.
    drive.source_path = git_source.cache_path(drive.id)
    db.session.commit()
    # Clone + first scan run in the background (the sync worker calls
    # provider.pre_sync, which materializes the checkout for git drives).
    # This keeps the request fast even for large repositories — the floating
    # sync widget shows live progress instead of the page hanging on the
    # synchronous clone.
    job = sync_service.start_sync(drive)
    if job.state == "error":
        flash(f'Git drive "{drive.name}" could not be cloned: '
              f"{job.error or 'unknown error'}.", "error")
        return redirect(url_for("gitdrives.index"))
    if job.state == "done":
        s = job.stats
        _flash_lfs_warning(drive)
        flash(f'Git drive "{drive.name}" created — {s["added"]} file(s) '
              f'imported, {s["skipped"]} skipped.', "success")
        return redirect(url_for("drive.index"))
    _flash_lfs_warning(drive)
    flash(f'Git drive "{drive.name}" is cloning — follow the progress '
          f"in the sync widget; files appear as they land.", "success")
    return redirect(url_for("drive.index"))


def _flash_lfs_warning(drive):
    total, sample = git_source.find_lfs_pointers(
        git_source.cache_path(drive.id))
    if total:
        shown = ", ".join(sample[:3]) + ("…" if total > 3 else "")
        flash(f"Warning: this repository uses Git LFS, which is not "
              f"fetched — {total} file(s) ({shown}) will appear as small "
              f"pointer text (or be skipped when the extension is not "
              f"indexed). Search sees only pointer metadata, not the real "
              f"content.", "warning")


@bp.get("/api/check")
@login_required
def check():
    """Validate a remote without creating anything (live form feedback)."""
    provider = drive_service.get_drive_source("git")
    if provider is None:
        return jsonify({"ok": False, "error": "Git module not ready."}), 500
    error = provider.validate(current_user, {
        "repo_url": request.args.get("repo_url", ""),
        "branch": request.args.get("branch", ""),
        "username": request.args.get("username", ""),
        "token": request.args.get("token", "")})
    if error:
        return jsonify({"ok": False, "error": error})
    return jsonify({"ok": True})


@bp.get("/<int:drive_id>/files")
@login_required
def files(drive_id):
    """Light file listing for pickers (mirrors /api/search scoping)."""
    drive = _owned_git_drive(drive_id)
    rows = (StoredFile.query
            .filter_by(user_id=current_user.id, drive_id=drive.id)
            .filter(StoredFile.deleted_at.is_(None))
            .order_by(StoredFile.name).limit(500).all())
    return jsonify({"drive": {"id": drive.id, "name": drive.name},
                    "files": [{"id": f.id, "name": f.name} for f in rows]})
