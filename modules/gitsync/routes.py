"""Git Sync pages — one workspace per Git-backed local-folder drive."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.models import Drive
from app.services import drive_service, sync_service

from . import service

bp = Blueprint("gitsync", __name__, template_folder="templates")


@bp.app_template_global()
def gitsync_drive_url(drive):
    """URL of the Git workspace for a drive, or None when it has no checkout."""
    if drive is None or drive.deleted_at is not None or not service.is_git_drive(drive):
        return None
    return url_for("gitsync.detail", drive_id=drive.id)


@bp.get("/")
def index():
    drives = [drive for drive in drive_service.list_drives(current_user)
              if service.is_git_drive(drive)]
    return render_template("gitsync/index.html", drives=drives)


def _workspace(drive_id):
    """(drive, repository) for an owned Git drive; redirects with a flash otherwise."""
    drive = (Drive.query.filter_by(id=drive_id, user_id=current_user.id)
             .filter(Drive.deleted_at.is_(None)).first())
    if drive is None:
        abort(404)
    try:
        return drive, service.for_drive(current_user, drive)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
        abort(redirect(url_for("gitsync.index")))


def _back(drive):
    return redirect(url_for("gitsync.detail", drive_id=drive.id))


def _reindex(drive, success):
    job = sync_service.start_sync(drive)
    if job.state == "error":
        flash(f"{success} Re-indexing the drive failed; check its sync status.", "error")
    elif job.state == "done":
        flash(f"{success} The drive was re-indexed.", "success")
    else:
        flash(f"{success} The drive is being re-indexed in the background.", "success")


@bp.get("/drive/<int:drive_id>")
def detail(drive_id):
    drive, repository = _workspace(drive_id)
    basis = request.args.get("basis", "head")
    if basis not in ("head", "initial"):
        abort(400)
    try:
        state = service.inspect_summary(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
        return redirect(url_for("gitsync.index"))
    pushed_branch = request.args.get("pushed_branch", "")
    pull_request = None
    if pushed_branch:
        try:
            pushed_branch = service.validate_branch_name(pushed_branch)
        except service.GitSyncError:
            pushed_branch = ""
        else:
            pull_request = service.pull_request_url(state["remote_url"], pushed_branch, state["upstream_branch"])
    return render_template("gitsync/detail.html", drive=drive, repository=repository,
                           state=state, basis=basis, pushed_branch=pushed_branch,
                           pull_request=pull_request)


@bp.get("/drive/<int:drive_id>/changes")
def changes(drive_id):
    """HTML fragment with the working-tree walk (status, diffs, actions); loaded async."""
    drive, repository = _workspace(drive_id)
    basis = request.args.get("basis", "head")
    if basis not in ("head", "initial"):
        abort(400)
    try:
        state = {**service.inspect_summary(repository),
                 **service.inspect_changes(repository, basis)}
    except service.GitSyncError as exc:
        return render_template("gitsync/_changes.html", drive=drive, repository=repository,
                               state=None, basis=basis, error=str(exc))
    return render_template("gitsync/_changes.html", drive=drive, repository=repository,
                           state=state, basis=basis, error=None)


@bp.post("/drive/<int:drive_id>/identity")
def identity(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        service.set_identity(repository, request.form.get("author_name", ""),
                             request.form.get("author_email", ""))
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash("Commit identity saved for this drive.", "success")
    return _back(drive)


@bp.post("/drive/<int:drive_id>/credentials")
def credentials(drive_id):
    drive, repository = _workspace(drive_id)
    clear = request.form.get("clear") == "yes"
    try:
        service.set_credentials(repository, request.form.get("remote_username", ""),
                                request.form.get("remote_token", ""), clear=clear)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash("Remote credentials cleared." if clear
              else "Remote credentials saved. The token is stored encrypted and never displayed again.",
              "success")
    return _back(drive)


@bp.post("/drive/<int:drive_id>/scan")
def scan(drive_id):
    drive, _repository = _workspace(drive_id)
    job = sync_service.start_sync(drive)
    if job.state == "error":
        flash("Indexing failed. Check the drive sync status.", "error")
    elif job.state == "done":
        flash("Local indexing finished. No Git fetch, pull, commit or push was performed.", "success")
    else:
        flash("Local indexing started and is running in the background.", "success")
    return _back(drive)


@bp.post("/drive/<int:drive_id>/commit")
def commit(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        commit_id = service.commit_selected(
            repository, request.form.getlist("paths"), request.form.get("message", ""))
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Created local commit {commit_id}. It exists only on this machine: use the "
              "\u201cPush\u201d button in the Next step panel to publish it to the remote.", "success")
    return _back(drive)


@bp.post("/drive/<int:drive_id>/discard")
def discard(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        path = service.discard_change(repository, request.form.get("path", ""))
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        _reindex(drive, f'Discarded local changes to "{path}".')
    return _back(drive)


@bp.post("/drive/<int:drive_id>/check-access")
def check_access(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        credentials_used = service.check_remote_access(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        if credentials_used:
            flash("Remote read access succeeded using the saved credentials for this drive. The authenticated identity is not verified.", "success")
        else:
            flash("Remote read access succeeded without saved credentials (anonymous or non-HTTPS remote). The authenticated identity is not verified.", "success")
    return _back(drive)


@bp.post("/drive/<int:drive_id>/fetch")
def fetch(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        service.fetch(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash("Fetched the configured upstream. The working tree was not changed; compare the updated history below.", "success")
    return _back(drive)


@bp.post("/drive/<int:drive_id>/push")
def push(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        service.push(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash("Pushed the current branch to its configured upstream.", "success")
    return _back(drive)


@bp.post("/drive/<int:drive_id>/push-branch")
def push_branch(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        name, remote = service.push_to_new_branch(repository, request.form.get("branch", ""))
    except service.GitSyncError as exc:
        flash(str(exc), "error")
        return _back(drive)
    flash(f"Pushed your local commits to the new branch {name} on {remote}. The tracked branch was not changed.", "success")
    return redirect(url_for("gitsync.detail", drive_id=drive.id, pushed_branch=name))


@bp.post("/drive/<int:drive_id>/merge")
def start_merge(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        service.start_merge(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        _reindex(drive, "Merge started without creating a commit. Review the staged changes and resolve each conflict below.")
    return _back(drive)


@bp.post("/drive/<int:drive_id>/merge/resolve")
def resolve_merge(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        service.resolve_conflicts(
            repository, request.form.getlist("paths"), request.form.get("choice", ""))
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        _reindex(drive, "Applied the selected version and staged that conflict resolution.")
    return _back(drive)


@bp.post("/drive/<int:drive_id>/merge/abort")
def abort_merge(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        service.abort_merge(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        _reindex(drive, "Merge aborted. The local branch was restored to its pre-merge state.")
    return _back(drive)


@bp.post("/drive/<int:drive_id>/merge/complete")
def complete_merge(drive_id):
    drive, repository = _workspace(drive_id)
    try:
        commit_id = service.complete_merge(
            repository, request.form.get("acknowledge_truncated") == "yes")
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Created merge commit {commit_id}. No remote operation was performed.", "success")
    return _back(drive)
