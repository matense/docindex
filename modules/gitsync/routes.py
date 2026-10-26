"""Git Sync pages."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.services import sync_service

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
    flash("Repository connected. Your files and Git configuration were not changed.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))


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
        flash("The linked drive was removed. Connect the repository again.", "error")
    else:
        job = sync_service.start_sync(repository.drive)
        if job.state == "error":
            flash("Indexing failed. Check the drive sync status.", "error")
        elif job.state == "done":
            flash("Local files indexed. No Git fetch, pull, commit or push was performed.", "success")
        else:
            flash("Local indexing started in the background.", "success")
    return redirect(url_for("gitsync.detail", repository_id=repository.id))