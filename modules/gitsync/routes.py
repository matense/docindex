"""Git Sync pages."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.services import drive_service, sync_service

from . import service
from .models import Repository

bp = Blueprint("gitsync", __name__, template_folder="templates")


@bp.get("/")
def index():
    repositories = Repository.query.filter_by(user_id=current_user.id).order_by(Repository.name).all()
    return render_template("gitsync/index.html", repositories=repositories,
                           accounts=service.github_accounts())


def _owned(repository_id):
    repository = Repository.query.filter_by(id=repository_id, user_id=current_user.id).first()
    if repository is None:
        abort(404)
    return repository


@bp.post("/connect")
def connect():
    try:
        repository = service.connect(
            current_user, request.form.get("path", ""),
            request.form.get("name", "").strip(), request.form.get("account", "").strip())
    except service.GitSyncError as exc:
        flash(str(exc), "error")
        return redirect(url_for("gitsync.index"))
    drive_service.select_drive(current_user, repository.drive_id)
    flash("Repository connected. Your files and Git configuration were not changed.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


@bp.post("/<int:repository_id>/disconnect")
def disconnect(repository_id):
    repository = _owned(repository_id)
    name = repository.name
    service.disconnect(repository)
    flash(f"Disconnected {name} from Git Sync. The local folder, Git data, and linked drive and files were not deleted.", "success")
    return redirect(url_for("gitsync.index"))


@bp.get("/<int:repository_id>")
def detail(repository_id):
    repository = _owned(repository_id)
    basis = request.args.get("basis", "head")
    if basis not in ("head", "initial"):
        abort(400)
    try:
        state = service.inspect(repository, basis)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
        return redirect(url_for("gitsync.index"))
    return render_template("gitsync/detail.html", repository=repository, state=state, basis=basis)


@bp.post("/<int:repository_id>/scan")
def scan(repository_id):
    repository = _owned(repository_id)
    try:
        service.repository_root(repository.path)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
        return redirect(url_for("gitsync.index"))
    if repository.drive is None or repository.drive.deleted_at is not None:
        flash("The DocIndex drive link is missing or trashed. Use Reconnect drive and index.", "error")
    else:
        job = sync_service.start_sync(repository.drive)
        if job.state == "error":
            flash("Indexing failed. Check the drive sync status.", "error")
        elif job.state == "done":
            flash("Local indexing finished. No Git fetch, pull, commit or push was performed.", "success")
        else:
            flash("Local indexing started and is running in the background.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


@bp.post("/<int:repository_id>/reconnect")
def reconnect(repository_id):
    repository = _owned(repository_id)
    try:
        drive = service.reconnect_drive(current_user, repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
        return redirect(url_for("gitsync.detail", repository_id=repository.id))

    drive_service.select_drive(current_user, drive.id)
    job = sync_service.start_sync(drive)
    if job.state == "error":
        flash("Repository reconnected, but indexing failed. Check the drive sync status.", "error")
    elif job.state == "done":
        flash("Repository reconnected and local indexing finished. No Git fetch, pull, commit or push was performed.", "success")
    else:
        flash("Repository reconnected; local indexing started in the background. No Git fetch, pull, commit or push was performed.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


@bp.post("/<int:repository_id>/commit")
def commit(repository_id):
    repository = _owned(repository_id)
    try:
        commit_id = service.commit_selected(
            repository, request.form.getlist("paths"), request.form.get("message", ""))
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Created local commit {commit_id}. No remote operation was performed.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


@bp.post("/<int:repository_id>/check-access")
def check_access(repository_id):
    repository = _owned(repository_id)
    try:
        account_hint_used = service.check_remote_access(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        if account_hint_used:
            flash("Remote read access succeeded. Git used its normal server-account credential sources and passed your preferred username as a hint; the authenticated identity is not verified.", "success")
        else:
            flash("Remote read access succeeded using Git's normal server-account credential sources. The authenticated identity is not verified.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


@bp.post("/<int:repository_id>/fetch")
def fetch(repository_id):
    repository = _owned(repository_id)
    try:
        service.fetch(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash("Fetched the configured upstream. The working tree was not changed; compare the updated history below.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


@bp.post("/<int:repository_id>/push")
def push(repository_id):
    repository = _owned(repository_id)
    try:
        service.push(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash("Pushed the current branch to its configured upstream.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


@bp.post("/<int:repository_id>/merge")
def start_merge(repository_id):
    repository = _owned(repository_id)
    try:
        service.start_merge(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash("Merge started without creating a commit. Review the staged changes and resolve each conflict below.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


@bp.post("/<int:repository_id>/merge/resolve")
def resolve_merge(repository_id):
    repository = _owned(repository_id)
    try:
        service.resolve_conflicts(
            repository, request.form.getlist("paths"), request.form.get("choice", ""))
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash("Applied the selected version and staged that conflict resolution.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


@bp.post("/<int:repository_id>/merge/abort")
def abort_merge(repository_id):
    repository = _owned(repository_id)
    try:
        service.abort_merge(repository)
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash("Merge aborted. The local branch was restored to its pre-merge state.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


@bp.post("/<int:repository_id>/merge/complete")
def complete_merge(repository_id):
    repository = _owned(repository_id)
    try:
        commit_id = service.complete_merge(
            repository, request.form.get("acknowledge_truncated") == "yes")
    except service.GitSyncError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Created merge commit {commit_id}. No remote operation was performed.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))