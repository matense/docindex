import importlib
import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Drive, ModuleState, StoredFile, User
from app.services import ai_service

IDENTITY = {"author_name": "Local Committer", "author_email": "local@example.com"}


def _git(path, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Test User",
         "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
         "-c", "core.quotepath=false", *args],
        capture_output=True, text=True, check=check, timeout=20,
    )


@pytest.fixture()
def enabled(app):
    with app.app_context():
        db.session.add(ModuleState(name="gitsync", enabled=True))
        db.session.commit()


@pytest.fixture()
def repository(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "readme.md").write_text("initial content\n", encoding="utf-8")
    _git(tmp_path, "add", "readme.md")
    _git(tmp_path, "commit", "-m", "Initial")
    return tmp_path


def _module():
    return importlib.import_module("docindex_module_gitsync")


def _view(client, location):
    """GET a page the way the browser sees it after the async fragment loads.

    Workspace pages render only cheap facts server-side and fetch the
    working-tree section from ``/changes``; concatenate both so assertions can
    look at the complete content. Redirects and errors are returned unchanged.
    """
    response = client.get(location)
    if response.status_code != 200 or "/m/gitsync/drive/" not in location:
        return response
    path, _, query = location.partition("?")
    fragment = client.get(f"{path}/changes" + (f"?{query}" if query else ""))
    assert fragment.status_code == 200, fragment.status_code
    assert b'id="gitsync-changes"' in response.data
    return SimpleNamespace(status_code=200, data=response.data + fragment.data,
                           location=None, page=response, fragment=fragment)


def _connect(client, path, sync_mode="mirror", identity=False):
    """Sync ``path`` as a drive; return a redirect-like object to its Git workspace.

    Mirrors the user flow: "Sync a local folder" then "Git changes". Folders
    that cannot become a drive redirect to the Git changes index instead.
    """
    client.post("/drives/sync-create", data={"path": str(path), "sync_mode": sync_mode})
    with client.application.app_context():
        drive = (Drive.query.filter_by(source_path=str(Path(path).resolve()))
                 .order_by(Drive.id.desc()).first())
        drive_id = drive.id if drive else None
    if drive_id is None:
        return SimpleNamespace(status_code=302, location="/m/gitsync/")
    location = f"/m/gitsync/drive/{drive_id}"
    client.get(location)  # opening the workspace attaches the Git Sync record
    if identity:
        _configure_identity(client, location)
    return SimpleNamespace(status_code=302, location=location)


def _configure_identity(client, location):
    assert client.post(location + "/identity", data=IDENTITY).status_code == 302


def _setup_bare_upstream(repository, tmp_path):
    remote = repository.parent / f"{repository.name}-origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True, timeout=20)
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "--set-upstream", "origin", "main")
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        capture_output=True, check=True, timeout=20,
    )
    return remote


def _push_remote_change(remote, tmp_path, content):
    checkout = remote.parent / f"{remote.stem}-remote-work"
    subprocess.run(["git", "clone", str(remote), str(checkout)], capture_output=True, check=True, timeout=20)
    (checkout / "readme.md").write_text(content, encoding="utf-8")
    _git(checkout, "commit", "-am", "Remote change")
    _git(checkout, "push", "origin", "main")
    return _git(checkout, "rev-parse", "HEAD").stdout.strip()


def test_gitsync_requires_login(client):
    assert client.get("/m/gitsync/").status_code == 302
    assert client.post("/m/gitsync/drive/1/scan").status_code == 302


def test_gitsync_disabled(auth_client):
    assert auth_client.get("/m/gitsync/").status_code == 404


def test_gitsync_enabled(auth_client, app):
    with app.app_context():
        db.session.add(ModuleState(name="gitsync", enabled=True))
        db.session.commit()
    response = auth_client.get("/m/gitsync/")
    assert response.status_code == 200
    assert b"Git changes" in response.data
    assert b"None of your synced folders is a Git repository root" in response.data


def test_gitdrives_page_points_to_sync_a_local_folder_only_when_enabled(auth_client, app):
    with app.app_context():
        db.session.add(ModuleState(name="gitdrives", enabled=True))
        db.session.commit()
    html = auth_client.get("/m/gitdrives/").data.decode()
    assert 'href="/m/gitsync/"' not in html
    with app.app_context():
        db.session.add(ModuleState(name="gitsync", enabled=True))
        db.session.commit()
    html = auth_client.get("/m/gitdrives/").data.decode()
    assert 'href="/m/gitsync/"' in html
    assert "Connect or manage" not in html


def test_mirror_drive_gets_git_workspace(auth_client, app, repository, enabled):
    initial = _git(repository, "rev-parse", "HEAD").stdout.strip()
    response = _connect(auth_client, repository)
    auth_client.post(response.location + "/identity",
                     data={"author_name": "Ander PT", "author_email": "ander@example.com"})
    page = _view(auth_client, response.location)
    assert page.status_code == 200
    assert b"Working tree clean" in page.data
    assert b"mirror (read-only) drive" in page.data
    assert b'value="Ander PT"' in page.data
    assert b'value="ander@example.com"' in page.data
    with app.app_context():
        connected = _module().models.Repository.query.one()
        assert connected.baseline == initial
        assert connected.drive.is_readonly
        assert connected.linked_drive is connected.drive
        assert connected.path == str(repository.resolve())
        assert connected.name == connected.drive.name
    with auth_client.session_transaction() as session:
        assert session["drive_id"] == connected.drive_id
    # The identity lives in DocIndex only; the repository's Git config is never written.
    assert not _git(repository, "config", "--local", "--get", "user.name", check=False).stdout
    assert not _git(repository, "config", "--local", "--get", "credential.username", check=False).stdout
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == initial


def test_shared_drive_gets_git_workspace_and_commits_docindex_edits(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository, sync_mode="shared", identity=True)
    page = _view(auth_client, response.location)
    assert page.status_code == 200
    assert b"shared (two-way) drive" in page.data
    # An edit through DocIndex lands in the real folder and shows up as a change.
    with app.app_context():
        stored = StoredFile.query.filter_by(name="readme.md").one()
    edit = auth_client.post(f"/file/{stored.id}/edit", data={"content": "edited in DocIndex\n"})
    assert edit.status_code in (200, 302)
    page = _view(auth_client, response.location)
    assert b"+edited in DocIndex" in page.data
    result = auth_client.post(response.location + "/commit", data={
        "paths": ["readme.md"], "message": "Edited through DocIndex"})
    assert b"Created local commit" in _view(auth_client, result.location).data
    assert _git(repository, "log", "-1", "--format=%s").stdout.strip() == "Edited through DocIndex"
    assert _git(repository, "log", "-1", "--format=%an <%ae>").stdout.strip() == "Local Committer <local@example.com>"


def test_git_changes_entry_points_only_for_git_drives(auth_client, app, repository, tmp_path, enabled):
    plain = tmp_path.parent / f"{tmp_path.name}-plain"
    plain.mkdir()
    (plain / "note.txt").write_text("plain\n", encoding="utf-8")
    plain_response = _connect(auth_client, plain)
    assert plain_response.location.startswith("/m/gitsync/drive/")
    assert b"Git changes" not in auth_client.get("/settings/profile").data
    assert b"Git changes in" not in auth_client.get("/").data
    redirect = _view(auth_client, plain_response.location)
    assert redirect.status_code == 302 and redirect.location.endswith("/m/gitsync/")
    assert b"not the root of a Git repository" in _view(auth_client, redirect.location).data

    response = _connect(auth_client, repository)
    profile = auth_client.get("/settings/profile").data.decode()
    assert profile.count("> Git changes\n") == 1  # one card button; the plain drive has none
    assert response.location in profile
    drive_page = auth_client.get("/").data.decode()  # the Git drive is current
    assert response.location in drive_page
    assert "Git changes in" in drive_page
    index = auth_client.get("/m/gitsync/").data.decode()
    assert response.location in index
    assert str(plain.resolve()) not in index


def test_subfolder_and_bare_repository_are_not_git_drives(auth_client, app, repository, tmp_path, enabled):
    subfolder = repository / "subfolder"
    subfolder.mkdir()
    (subfolder / "doc.txt").write_text("x\n", encoding="utf-8")
    response = _connect(auth_client, subfolder)
    followed = _view(auth_client, response.location)
    assert followed.status_code == 302
    assert b"not the root of a Git repository" in _view(auth_client, followed.location).data
    _git(repository, "init", "--bare", str(tmp_path / "bare.git"))
    response = _connect(auth_client, tmp_path / "bare.git")
    assert _view(auth_client, response.location).status_code == 302
    with app.app_context():
        assert _module().models.Repository.query.count() == 0


def test_removed_drive_record_is_reattached_when_folder_is_synced_again(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    with app.app_context():
        connected = _module().models.Repository.query.one()
        old_drive_id = connected.drive_id
        baseline = connected.baseline
    (repository / "readme.md").write_text("later\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Later")

    assert auth_client.post(f"/drives/{old_drive_id}/remove").status_code == 302
    with app.app_context():
        assert db.session.get(Drive, old_drive_id) is None
        stale = _module().models.Repository.query.one()
        assert stale.drive_id == old_drive_id  # FKs are not enforced by SQLite here
        assert stale.linked_drive is None
    assert b"None of your synced folders" in auth_client.get("/m/gitsync/").data

    again = _connect(auth_client, repository)
    assert again.location != response.location
    page = _view(auth_client, again.location)
    assert page.status_code == 200
    assert b"When connected" in page.data  # baseline survived
    with app.app_context():
        connected = _module().models.Repository.query.one()
        assert connected.drive_id != old_drive_id
        assert connected.baseline == baseline
        assert connected.linked_drive.source_path == str(repository.resolve())
    assert b"+later" in _view(auth_client, again.location + "?basis=initial").data


def test_reused_drive_id_never_leaks_another_folder(auth_client, app, repository, tmp_path, enabled):
    response = _connect(auth_client, repository)
    plain = tmp_path.parent / f"{tmp_path.name}-plain"
    plain.mkdir()
    (plain / "note.txt").write_text("plain\n", encoding="utf-8")
    with app.app_context():
        connected = _module().models.Repository.query.one()
        old_id = connected.drive_id
        db.session.delete(connected.drive)
        db.session.commit()
        db.session.add(Drive(id=old_id, name="Unrelated", user_id=connected.user_id,
                             source_path=str(plain.resolve()), sync_mode="mirror"))
        db.session.commit()

    followed = _view(auth_client, response.location)
    assert followed.status_code == 302
    assert b"not the root of a Git repository" in _view(auth_client, followed.location).data
    assert auth_client.post(response.location + "/commit", data={
        "paths": ["note.txt"], "message": "must not happen"}).status_code == 302
    assert _git(repository, "status", "--porcelain").stdout == ""
    with app.app_context():
        assert _module().models.Repository.query.one().drive_id == old_id

    again = _connect(auth_client, repository)
    assert _view(auth_client, again.location).status_code == 200
    with app.app_context():
        connected = _module().models.Repository.query.one()
        assert connected.drive_id != old_id
        assert connected.path == str(repository.resolve())
        assert db.session.get(Drive, old_id).name == "Unrelated"


def test_identity_is_saved_and_validated(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    result = auth_client.post(response.location + "/identity", data={
        "author_name": " Ander PT ", "author_email": " ander@example.com "})
    assert b"Commit identity saved" in _view(auth_client, result.location).data
    with app.app_context():
        connected = _module().models.Repository.query.one()
        assert (connected.author_name, connected.author_email) == ("Ander PT", "ander@example.com")
    result = auth_client.post(response.location + "/identity", data={
        "author_name": "x" * 121, "author_email": "a@b.c"})
    assert b"at most 120 printable characters" in _view(auth_client, result.location).data
    result = auth_client.post(response.location + "/identity", data={
        "author_name": "Ander <PT>", "author_email": "a@b.c"})
    assert b"must not contain angle brackets" in _view(auth_client, result.location).data
    result = auth_client.post(response.location + "/identity", data={
        "author_name": "Ander", "author_email": "not-an-email"})
    assert b"valid author email" in _view(auth_client, result.location).data
    with app.app_context():
        connected = _module().models.Repository.query.one()
        assert (connected.author_name, connected.author_email) == ("Ander PT", "ander@example.com")
    assert not _git(repository, "config", "--local", "--get", "user.name", check=False).stdout


def test_commit_requires_identity_saved_in_docindex(auth_client, app, repository, enabled):
    _git(repository, "config", "--local", "user.name", "Git Config User")
    _git(repository, "config", "--local", "user.email", "config@example.com")
    response = _connect(auth_client, repository)
    (repository / "pending.txt").write_text("pending\n", encoding="utf-8")
    page = _view(auth_client, response.location).data
    assert b"Set the commit identity" in page
    previous_head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    result = auth_client.post(response.location + "/commit", data={
        "paths": ["pending.txt"], "message": "Needs identity"})
    assert b"Set the commit identity (name and email) in Repository details before committing" in _view(auth_client, result.location).data
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == previous_head
    assert not _git(repository, "diff", "--cached", "--name-only").stdout.strip()


def test_credentials_are_encrypted_write_only_and_clearable(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    result = auth_client.post(response.location + "/credentials", data={
        "remote_username": "AnderPT", "remote_token": "ghp_secret-token-value"})
    page = _view(auth_client, result.location).data
    assert b"Remote credentials saved" in page
    assert b"secret-token" not in page
    assert b"Token saved" in page
    assert b'value="AnderPT"' in page
    with app.app_context():
        connected = _module().models.Repository.query.one()
        assert connected.remote_username == "AnderPT"
        assert connected.remote_token.startswith("enc:")
        assert "secret-token" not in connected.remote_token
        assert ai_service.decrypt_api_key(connected.remote_token) == "ghp_secret-token-value"

    # Saving with an empty token keeps the stored one and only updates the username.
    auth_client.post(response.location + "/credentials", data={"remote_username": "other", "remote_token": ""})
    with app.app_context():
        connected = _module().models.Repository.query.one()
        assert connected.remote_username == "other"
        assert ai_service.decrypt_api_key(connected.remote_token) == "ghp_secret-token-value"

    result = auth_client.post(response.location + "/credentials", data={"clear": "yes"})
    page = _view(auth_client, result.location).data
    assert b"Remote credentials cleared" in page
    assert b"No token saved" in page
    with app.app_context():
        connected = _module().models.Repository.query.one()
        assert connected.remote_username is None and connected.remote_token is None
    assert not _git(repository, "config", "--local", "--get", "credential.username", check=False).stdout


def test_remote_operations_pass_saved_credentials_only_for_http_remotes(
        auth_client, app, repository, tmp_path, enabled):
    service = _module().service
    _setup_bare_upstream(repository, tmp_path)
    older = _git(repository, "rev-parse", "HEAD").stdout.strip()
    (repository / "ahead.txt").write_text("ahead\n", encoding="utf-8")
    _git(repository, "add", "ahead.txt")
    _git(repository, "commit", "-m", "Ahead")
    _git(repository, "update-ref", "refs/remotes/origin/main", older)
    response = _connect(auth_client, repository)
    auth_client.post(response.location + "/credentials", data={
        "remote_username": "AnderPT", "remote_token": "ghp_secret-token-value"})
    calls = {}

    def record(name):
        def recorder(*args, **kwargs):
            calls[name] = kwargs
            return SimpleNamespace(ref_status={}, refs={})
        return recorder

    with app.app_context():
        connected = _module().models.Repository.query.one()
        with patch.object(service.porcelain, "ls_remote", side_effect=record("ls_remote")), \
                patch.object(service.porcelain, "fetch", side_effect=record("fetch")), \
                patch.object(service.porcelain, "push", side_effect=record("push")):
            # Local-path remote: no credentials are ever attached.
            assert service.check_remote_access(connected) is False
            service.fetch(connected)
            service.push(connected)
            assert all("password" not in kwargs for kwargs in calls.values())
            calls.clear()
            _git(repository, "remote", "set-url", "origin", "https://example.test/team/repo.git")
            assert service.check_remote_access(connected) is True
            service.fetch(connected)
            service.push(connected)
    for name in ("ls_remote", "fetch", "push"):
        assert calls[name]["username"] == "AnderPT"
        assert calls[name]["password"] == "ghp_secret-token-value"
    assert calls["ls_remote"]["config"] is not None


def test_detail_page_defers_the_working_tree_walk_to_the_changes_fragment(
        auth_client, app, repository, enabled):
    service = _module().service
    response = _connect(auth_client, repository)
    (repository / "readme.md").write_text("changed content\n", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("the detail page must not walk the working tree")

    with patch.object(service, "inspect_changes", side_effect=forbidden):
        page = auth_client.get(response.location)
    assert page.status_code == 200
    assert b'id="gitsync-changes"' in page.data
    assert response.location + "/changes?basis=head" in page.data.decode()
    assert b"Checking working tree" in page.data
    assert b"+changed content" not in page.data

    fragment = auth_client.get(response.location + "/changes?basis=head")
    assert fragment.status_code == 200
    assert b'id="gitsync-tree-badge-content"' in fragment.data
    assert b"1 working change" in fragment.data
    assert b"+changed content" in fragment.data
    assert b"gitsync-commit-form" in fragment.data
    assert auth_client.get(response.location + "/changes?basis=bogus").status_code == 400


def test_changes_fragment_is_scoped_to_the_owner(auth_client, client, app, repository, enabled):
    response = _connect(auth_client, repository)
    with app.app_context():
        other = User(username="carol", email="carol@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
    client.post("/logout")
    client.get("/logout")
    client.post("/login", data={"username": "carol", "password": "password123"})
    assert client.get(response.location + "/changes").status_code == 404


def test_next_step_panel_walks_the_user_from_commit_to_push(auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository, identity=True)

    def step(html):
        marker = 'id="gitsync-next-step" data-step="'
        return html.split(marker, 1)[1].split('"', 1)[0]

    assert step(_view(auth_client, response.location).data.decode()) == "synced"

    (repository / "readme.md").write_text("edited\n", encoding="utf-8")
    html = _view(auth_client, response.location).data.decode()
    assert step(html) == "commit"
    assert "The commit stays on this machine until you push" in html
    assert 'href="#gitsync-commit-form"' in html

    result = auth_client.post(response.location + "/commit", data={"paths": ["readme.md"], "message": "Edit"})
    html = _view(auth_client, result.location).data.decode()
    assert "use the \u201cPush\u201d button in the Next step panel" in html
    assert step(html) == "push"
    assert "1 local commit</strong> not on the remote yet" in html
    assert "Push 1 local commit</button>" in html  # next-step panel
    assert "Push local commits</button>" in html  # remote actions

    result = auth_client.post(response.location + "/push")
    html = _view(auth_client, result.location).data.decode()
    assert "Pushed the current branch" in html
    assert step(html) == "synced"
    assert subprocess.run(["git", "--git-dir", str(remote), "log", "-1", "--format=%s", "main"],
                          capture_output=True, text=True, check=True, timeout=20).stdout.strip() == "Edit"


def test_next_step_panel_explains_missing_identity_and_upstream(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)  # no identity, no upstream
    (repository / "readme.md").write_text("edited\n", encoding="utf-8")
    html = _view(auth_client, response.location).data.decode()
    assert 'data-step="identity"' in html
    assert "commits and merges need an author" in html
    _configure_identity(auth_client, response.location)
    html = _view(auth_client, response.location).data.decode()
    assert 'data-step="commit"' in html
    auth_client.post(response.location + "/commit", data={"paths": ["readme.md"], "message": "Edit"})
    html = _view(auth_client, response.location).data.decode()
    assert 'data-step="no-upstream"' in html
    assert "no upstream configured" in html


def test_push_to_new_branch_publishes_commits_without_touching_tracked_branch(
        auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    _git(repository, "remote", "set-url", "origin", str(remote))
    response = _connect(auth_client, repository, identity=True)
    (repository / "readme.md").write_text("mine\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Mine")
    _push_remote_change(remote, tmp_path, "theirs\n")  # tracked branch is now behind
    (repository / "draft.md").write_text("draft\n", encoding="utf-8")  # and the tree is dirty
    auth_client.post(response.location + "/fetch")

    def remote_ref(name):
        return subprocess.run(["git", "--git-dir", str(remote), "rev-parse", name],
                              capture_output=True, text=True, timeout=20).stdout.strip()

    remote_main_before = remote_ref("main")
    local_head = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = auth_client.post(response.location + "/push-branch", data={"branch": "ander/wiki-changes"})
    assert result.status_code == 302 and "pushed_branch=ander" in result.location
    page = auth_client.get(result.location).data.decode()
    assert "Pushed your local commits to the new branch ander/wiki-changes" in page
    assert 'id="gitsync-pushed-branch"' in page
    assert "open a pull request from" in page
    assert remote_ref("ander/wiki-changes") == local_head
    assert remote_ref("main") == remote_main_before
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == local_head
    assert (repository / "draft.md").read_text(encoding="utf-8") == "draft\n"
    assert _git(repository, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"

    # Same name again is a no-op fast-forward; a diverged remote branch is rejected, never forced.
    assert b"Pushed your local commits" in auth_client.get(
        auth_client.post(response.location + "/push-branch", data={"branch": "ander/wiki-changes"}).location).data
    _git(repository, "commit", "--amend", "-m", "Mine (amended)")
    result = auth_client.post(response.location + "/push-branch", data={"branch": "ander/wiki-changes"})
    assert b"rejected" in _view(auth_client, result.location).data
    assert remote_ref("ander/wiki-changes") == local_head


@pytest.mark.parametrize("name", ["", "has space", "bad..name", "refs/heads/x", "-flag", "x" * 121, "main"])
def test_push_to_new_branch_validates_names(auth_client, app, repository, tmp_path, enabled, name):
    _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository, identity=True)
    (repository / "readme.md").write_text("mine\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Mine")
    service = _module().service
    with patch.object(service.porcelain, "push", side_effect=AssertionError("must not push")):
        result = auth_client.post(response.location + "/push-branch", data={"branch": name})
    page = _view(auth_client, result.location).data
    assert b"alert-error" in page or b"use the normal push" in page


def test_pull_request_links_are_derived_from_the_remote():
    pr = _module().service.pull_request_url
    assert pr("https://dev.azure.com/SiemensITAM/ITAM/_git/ITAM.wiki", "ander/x", "wikiMaster") == \
        "https://dev.azure.com/SiemensITAM/ITAM/_git/ITAM.wiki/pullrequestcreate?sourceRef=ander%2Fx&targetRef=wikiMaster"
    assert pr("https://contoso.visualstudio.com/Proj/_git/repo", "f", "main").startswith(
        "https://contoso.visualstudio.com/Proj/_git/repo/pullrequestcreate?sourceRef=f")
    assert pr("https://github.com/org/repo.git", "feature", "main") == \
        "https://github.com/org/repo/compare/main...feature?expand=1"
    assert pr("https://gitlab.example.com/g/sub/p.git", "f", "main").startswith(
        "https://gitlab.example.com/g/sub/p/-/merge_requests/new?merge_request%5Bsource_branch%5D=f")
    assert pr("git@github.com:org/repo.git", "f", "main") is None
    assert pr("https://git.example.test/repo.git", "f", "main") is None
    suggested = _module().service.suggested_branch_name(SimpleNamespace(author_name="Ander PT"), "wikiMaster")
    assert suggested == "ander-pt/wikiMaster-changes"


def test_remote_guidance_detects_host_and_derives_token_page():
    guidance = _module().service.remote_guidance
    azure = guidance("https://dev.azure.com/SiemensITAM/ITAM/_git/ITAM.wiki")
    assert azure["kind"] == "azure" and azure["needs_token"]
    assert azure["token_url"] == "https://dev.azure.com/SiemensITAM/_usersSettings/tokens"
    assert any("SiemensITAM" in step for step in azure["steps"])
    legacy = guidance("https://contoso.visualstudio.com/Proj/_git/repo")
    assert legacy["kind"] == "azure"
    assert legacy["token_url"] == "https://contoso.visualstudio.com/_usersSettings/tokens"
    assert guidance("https://github.com/org/repo.git")["token_url"] == "https://github.com/settings/tokens"
    assert guidance("https://gitlab.example.com/g/p.git")["token_url"].endswith("/personal_access_tokens")
    assert guidance("git@github.com:org/repo.git")["kind"] == "ssh"
    assert guidance("ssh://git@host/repo.git")["kind"] == "ssh"
    assert guidance(r"C:\repos\bare.git")["kind"] == "local"
    assert guidance("")["kind"] == "none"
    generic = guidance("https://git.example.test/team/repo.git")
    assert generic["kind"] == "https" and generic["token_url"] is None
    for result in (azure, legacy, generic):
        assert all(not step.startswith(" ") for step in result["steps"])


def test_page_shows_azure_devops_token_instructions_until_a_token_is_saved(
        auth_client, app, repository, tmp_path, enabled):
    _setup_bare_upstream(repository, tmp_path)
    _git(repository, "remote", "set-url", "origin", "https://dev.azure.com/SiemensITAM/ITAM/_git/ITAM.wiki")
    response = _connect(auth_client, repository)
    page = _view(auth_client, response.location).data.decode()
    assert 'data-remote-kind="azure"' in page
    assert "personal access token is needed before fetch or push" in page
    assert "https://dev.azure.com/SiemensITAM/_usersSettings/tokens" in page
    assert "Personal access tokens" in page
    assert "border-warning" in page.split('id="gitsync-remote-help"')[0].rsplit("<div", 1)[1]
    auth_client.post(response.location + "/credentials", data={"remote_token": "pat-value"})
    page = _view(auth_client, response.location).data.decode()
    assert "Authentication for Azure DevOps" in page
    assert "is needed before fetch or push" not in page
    assert "pat-value" not in page


def test_token_without_username_still_sends_basic_auth(auth_client, app, repository, tmp_path, enabled):
    """dulwich only attaches Basic auth when a username is given; PAT hosts accept any."""
    service = _module().service
    _setup_bare_upstream(repository, tmp_path)
    _git(repository, "remote", "set-url", "origin", "https://dev.azure.com/org/proj/_git/wiki")
    response = _connect(auth_client, repository)
    auth_client.post(response.location + "/credentials", data={
        "remote_username": "", "remote_token": "azure-pat-value"})
    calls = []
    with app.app_context():
        connected = _module().models.Repository.query.one()
        with patch.object(service.porcelain, "fetch",
                          side_effect=lambda *a, **k: calls.append(k) or SimpleNamespace(refs={})):
            service.fetch(connected)
    assert calls[0]["username"] == "pat"
    assert calls[0]["password"] == "azure-pat-value"


def test_denied_https_fetch_without_token_explains_pat_requirement(
        auth_client, app, repository, tmp_path, enabled):
    service = _module().service
    _setup_bare_upstream(repository, tmp_path)
    _git(repository, "remote", "set-url", "origin", "https://dev.azure.com/org/proj/_git/wiki")
    response = _connect(auth_client, repository)

    def unauthorized(*args, **kwargs):
        assert "password" not in kwargs
        raise service.HTTPUnauthorized("Basic realm=ado", "https://dev.azure.com/org/proj/_git/wiki")

    with patch.object(service.porcelain, "fetch", side_effect=unauthorized):
        result = auth_client.post(response.location + "/fetch")
    page = _view(auth_client, result.location).data
    assert b"no token is saved for this drive" in page
    assert b"Personal access tokens" in page
    assert b"Git Credential Manager" in page
    # With a token saved the generic denied message is kept (credentials exist but were refused).
    auth_client.post(response.location + "/credentials", data={"remote_token": "wrong-pat"})
    with patch.object(service.porcelain, "fetch", side_effect=service.HTTPUnauthorized("Basic", "u")):
        result = auth_client.post(response.location + "/fetch")
    page = _view(auth_client, result.location).data
    assert b"Remote access was denied" in page
    assert b"no token is saved" not in page


def test_service_never_uses_the_git_binary(auth_client, app, repository, tmp_path, enabled):
    service = _module().service
    source = inspect.getsource(service)
    assert "subprocess" not in source
    assert '"git"' not in source
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository, identity=True)
    (repository / "local.txt").write_text("local only\n", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError(f"Git Sync must not spawn processes: {args[:1]}")

    with patch.object(subprocess, "run", side_effect=forbidden), \
            patch.object(subprocess, "Popen", side_effect=forbidden):
        result = auth_client.post(response.location + "/commit", data={
            "paths": ["local.txt"], "message": "Local only"})
        assert b"Created local commit" in _view(auth_client, result.location).data
        with app.app_context():
            connected = _module().models.Repository.query.one()
            service.fetch(connected)
            service.push(connected)
            state = service.inspect(connected)
    assert state["ahead_behind"] == {"ahead": 0, "behind": 0}
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True, timeout=20,
    ).stdout.strip()
    assert remote_head == _git(repository, "rev-parse", "HEAD").stdout.strip()


def test_commit_panel_shows_current_branch_and_lists_local_branches_read_only(
        auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    page = _view(auth_client, response.location).data.decode()
    assert "Commits go to branch <strong class=\"font-mono\">main</strong>" in page
    assert "only local branch" in page
    _git(repository, "branch", "feature/docs")
    page = _view(auth_client, response.location).data.decode()
    assert "2 local branches" in page
    assert "feature/docs" in page
    assert "checkout" not in page.lower().split("working tree changes")[1].split("tracked file differences")[0]
    with app.app_context():
        state = _module().service.inspect(_module().models.Repository.query.one())
    assert [(b["name"], b["current"]) for b in state["branches"]] == [("feature/docs", False), ("main", True)]
    assert _git(repository, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"


def test_status_diff_and_initial_baseline(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    (repository / "readme.md").write_text("changed content\n", encoding="utf-8")
    (repository / "new file.txt").write_text("new\n", encoding="utf-8")
    page = _view(auth_client, response.location)
    assert b'<span class="diff-add">+changed content\n</span>' in page.data
    assert b'<span class="diff-del">-initial content\n</span>' in page.data
    assert b"new file.txt" in page.data
    _git(repository, "add", "readme.md")
    _git(repository, "commit", "-m", "External change")
    assert b"+changed content" not in _view(auth_client, response.location).data
    assert b"+changed content" in _view(auth_client, response.location + "?basis=initial").data


def test_diffs_are_grouped_and_navigable_by_file(auth_client, app, repository, enabled):
    (repository / "second.txt").write_text("second original\n", encoding="utf-8")
    _git(repository, "add", "second.txt")
    _git(repository, "commit", "-m", "Add second file")
    response = _connect(auth_client, repository)
    (repository / "readme.md").write_text("readme changed\n", encoding="utf-8")
    (repository / "second.txt").write_text("second changed\n", encoding="utf-8")

    page = _view(auth_client, response.location)
    with app.app_context():
        state = _module().service.inspect(_module().models.Repository.query.one())
    assert len(state["diff_files"]) == 2
    assert "readme.md" in " ".join(file["header"] for file in state["diff_files"])
    assert b"data-diff-browser" in page.data
    assert b"gitsync-diff-navigator" in page.data
    assert b"<details class=\"gitsync-diff-file" in page.data


def test_changed_files_beyond_500_are_searchable_and_selectable(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository, identity=True)
    for index in range(505):
        (repository / f"change-{index:03}.txt").write_text("change\n", encoding="utf-8")

    page = _view(auth_client, response.location)
    assert b"change-504.txt" in page.data
    assert b"gitsync-change-search" in page.data
    assert b"gitsync-change-status" in page.data
    assert b"gitsync-select-page" in page.data
    assert b"pageSize = 100" in page.data
    with app.app_context():
        state = _module().service.inspect(_module().models.Repository.query.one())
        assert state["change_count"] == 505

    result = auth_client.post(response.location + "/commit", data={
        "paths": ["change-504.txt"], "message": "Commit a file beyond the first page",
    })
    assert result.status_code == 302
    committed = _git(repository, "show", "--format=", "--name-only", "HEAD").stdout
    assert "change-504.txt" in committed


def test_selected_file_commit_leaves_unselected_changes(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository, identity=True)
    deleted = "delete me.txt"
    (repository / deleted).write_text("to be deleted\n", encoding="utf-8")
    _git(repository, "add", "--", deleted)
    _git(repository, "commit", "-m", "Add deletion fixture")
    selected = "-notes 雪 file.txt"
    (repository / "readme.md").rename(repository / selected)
    (repository / deleted).unlink()
    (repository / "leave alone.txt").write_text("unselected\n", encoding="utf-8")
    previous_head = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = auth_client.post(response.location + "/commit", data={
        "paths": ["readme.md", selected, deleted], "message": "Rename and delete selected files",
    })

    assert result.status_code == 302
    assert b"Created local commit" in _view(auth_client, result.location).data
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() != previous_head
    committed_paths = subprocess.run(
        ["git", "-C", str(repository), "-c", "core.quotepath=false",
         "diff-tree", "--no-commit-id", "--name-status", "-r", "-z", "HEAD"],
        capture_output=True, check=True, timeout=20,
    ).stdout.decode("utf-8").split("\0")
    assert selected in committed_paths
    assert "readme.md" in committed_paths
    assert deleted in committed_paths
    status = _git(repository, "status", "--porcelain").stdout
    assert "leave alone.txt" in status
    assert selected not in status


@pytest.mark.parametrize("submitted_path", ["not-changed.txt", "readme.md"])
def test_commit_rejects_arbitrary_or_stale_paths(auth_client, app, repository, enabled, submitted_path):
    response = _connect(auth_client, repository, identity=True)
    (repository / "currently-changed.txt").write_text("change\n", encoding="utf-8")
    previous_head = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = auth_client.post(response.location + "/commit", data={
        "paths": [submitted_path], "message": "Should not commit",
    })

    page = _view(auth_client, result.location)
    assert b"stale or invalid" in page.data
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == previous_head
    assert not _git(repository, "diff", "--cached", "--name-only").stdout.strip()


def test_commit_rejects_stale_selection(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository, identity=True)
    stale_path = repository / "stale.txt"
    stale_path.write_text("was changed\n", encoding="utf-8")
    page = _view(auth_client, response.location)
    assert b"stale.txt" in page.data
    stale_path.unlink()
    (repository / "current.txt").write_text("current change\n", encoding="utf-8")
    previous_head = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = auth_client.post(response.location + "/commit", data={
        "paths": ["stale.txt"], "message": "Stale selection",
    })

    assert b"stale or invalid" in _view(auth_client, result.location).data
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == previous_head
    assert not _git(repository, "diff", "--cached", "--name-only").stdout.strip()


def test_discard_change_restores_only_selected_file_across_index_and_worktree(
        auth_client, app, repository, enabled):
    (repository / "staged-tracked.txt").write_text("committed staged target\n", encoding="utf-8")
    (repository / "deleted-tracked.txt").write_text("committed deletion target\n", encoding="utf-8")
    _git(repository, "add", "staged-tracked.txt", "deleted-tracked.txt")
    _git(repository, "commit", "-m", "Add discard fixtures")
    response = _connect(auth_client, repository)
    (repository / "readme.md").write_text("modified tracked\n", encoding="utf-8")
    (repository / "staged-tracked.txt").write_text("staged modification\n", encoding="utf-8")
    _git(repository, "add", "staged-tracked.txt")
    (repository / "deleted-tracked.txt").unlink()
    (repository / "new-untracked.txt").write_text("untracked\n", encoding="utf-8")
    (repository / "new-staged.txt").write_text("staged addition\n", encoding="utf-8")
    _git(repository, "add", "new-staged.txt")
    head_before = _git(repository, "rev-parse", "HEAD").stdout.strip()

    page = _view(auth_client, response.location).data
    assert b"Discard changes to readme.md" in page
    assert b"Discard changes to staged-tracked.txt" in page
    assert b"Discard changes to deleted-tracked.txt" in page
    assert b"Discard changes to new-untracked.txt" in page
    assert b"Discard changes to new-staged.txt" in page
    assert b"OneDrive may sync the restored file or deletion" in page
    assert b"data-confirm-danger" in page

    result = auth_client.post(response.location + "/discard", data={"path": "readme.md"})
    assert result.status_code == 302
    _view(auth_client, result.location)
    assert (repository / "readme.md").read_text(encoding="utf-8") == "initial content\n"
    assert (repository / "new-untracked.txt").read_text(encoding="utf-8") == "untracked\n"
    assert _git(repository, "diff", "--cached", "--name-only").stdout.splitlines() == [
        "new-staged.txt", "staged-tracked.txt"]

    result = auth_client.post(response.location + "/discard", data={"path": "staged-tracked.txt"})
    _view(auth_client, result.location)
    assert (repository / "staged-tracked.txt").read_text(encoding="utf-8") == "committed staged target\n"
    assert _git(repository, "diff", "--cached", "--name-only").stdout.strip() == "new-staged.txt"

    result = auth_client.post(response.location + "/discard", data={"path": "deleted-tracked.txt"})
    _view(auth_client, result.location)
    assert (repository / "deleted-tracked.txt").read_text(encoding="utf-8") == "committed deletion target\n"
    assert _git(repository, "diff", "--cached", "--name-only").stdout.strip() == "new-staged.txt"

    result = auth_client.post(response.location + "/discard", data={"path": "new-untracked.txt"})
    _view(auth_client, result.location)
    assert not (repository / "new-untracked.txt").exists()
    assert _git(repository, "diff", "--cached", "--name-only").stdout.strip() == "new-staged.txt"

    result = auth_client.post(response.location + "/discard", data={"path": "new-staged.txt"})
    _view(auth_client, result.location)
    assert not (repository / "new-staged.txt").exists()
    assert not _git(repository, "status", "--porcelain").stdout.strip()
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == head_before


def test_cloud_files_reparse_tags_are_detected_without_treating_symlinks_as_placeholders():
    service = _module().service
    assert service._is_cloud_placeholder_tag(0x9000601A)
    assert service._is_cloud_placeholder_tag(0x9000101A)
    assert not service._is_cloud_placeholder_tag(0xA000000C)
    assert not service._is_cloud_placeholder_tag(0)


def test_discard_rejects_stale_paths_and_conflicts(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    stale = auth_client.post(response.location + "/discard", data={"path": "readme.md"})
    assert b"no longer changed" in _view(auth_client, stale.location).data

    _git(repository, "checkout", "-b", "discard-conflict")
    (repository / "readme.md").write_text("other branch\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Other")
    _git(repository, "checkout", "main")
    (repository / "readme.md").write_text("main branch\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Main")
    _git(repository, "merge", "discard-conflict", check=False)
    before = _git(repository, "status", "--porcelain").stdout
    result = auth_client.post(response.location + "/discard", data={"path": "readme.md"})
    assert b"Resolve or abort the current merge" in _view(auth_client, result.location).data
    assert _git(repository, "status", "--porcelain").stdout == before


def test_commit_rejects_pre_staged_changes(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository, identity=True)
    (repository / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repository, "add", "--", "staged.txt")
    (repository / "selected.txt").write_text("selected\n", encoding="utf-8")
    previous_head = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = auth_client.post(response.location + "/commit", data={
        "paths": ["selected.txt"], "message": "Must not include staged work",
    })

    assert b"already staged" in _view(auth_client, result.location).data
    assert _git(repository, "diff", "--cached", "--name-only").stdout.strip() == "staged.txt"
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == previous_head


def test_commit_rejects_unresolved_conflicts(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository, identity=True)
    _git(repository, "checkout", "-b", "other")
    (repository / "readme.md").write_text("other branch\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Other")
    _git(repository, "checkout", "main")
    (repository / "readme.md").write_text("main branch\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Main")
    previous_head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    assert _git(repository, "merge", "other", check=False).returncode != 0

    result = auth_client.post(response.location + "/commit", data={
        "paths": ["readme.md"], "message": "Must not commit conflict",
    })

    assert b"Resolve all merge conflicts" in _view(auth_client, result.location).data
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == previous_head


def test_commit_performs_no_remote_operations(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository, identity=True)
    (repository / "local.txt").write_text("local only\n", encoding="utf-8")
    service = _module().service

    def forbidden(*args, **kwargs):
        raise AssertionError("remote operation attempted during a local commit")

    with patch.object(service.porcelain, "fetch", side_effect=forbidden), \
            patch.object(service.porcelain, "push", side_effect=forbidden), \
            patch.object(service.porcelain, "pull", side_effect=forbidden), \
            patch.object(service.porcelain, "merge", side_effect=forbidden), \
            patch.object(service.porcelain, "ls_remote", side_effect=forbidden):
        result = auth_client.post(response.location + "/commit", data={
            "paths": ["local.txt"], "message": "Local only",
        })

    assert result.status_code == 302
    assert b"Created local commit" in _view(auth_client, result.location).data
    assert _git(repository, "log", "-1", "--format=%s").stdout.strip() == "Local only"


def test_staged_rename(auth_client, app, repository, enabled):
    """dulwich does not detect renames: a `git mv` shows as a staged delete + add, nothing is lost."""
    _connect(auth_client, repository)
    _git(repository, "mv", "readme.md", "renamed file.md")
    with app.app_context():
        state = _module().service.inspect(_module().models.Repository.query.one())
    assert [(change["status"], change["path"]) for change in state["changes"]] == [
        ("D ", "readme.md"), ("A ", "renamed file.md")]
    assert state["staged_count"] == 2 and state["conflict_count"] == 0
    assert (repository / "renamed file.md").read_text(encoding="utf-8") == "initial content\n"
    assert _git(repository, "status", "--porcelain").stdout.startswith("R  readme.md -> ")


def test_conflicts_are_reported_without_modifying_files(auth_client, app, repository, enabled):
    _connect(auth_client, repository)
    _git(repository, "checkout", "-b", "other")
    (repository / "readme.md").write_text("other branch\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Other")
    _git(repository, "checkout", "main")
    (repository / "readme.md").write_text("main branch\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Main")
    assert _git(repository, "merge", "other", check=False).returncode != 0
    before = (repository / "readme.md").read_bytes()
    with app.app_context():
        state = _module().service.inspect(_module().models.Repository.query.one())
    assert state["conflict_count"] == 1
    assert state["changes"][0]["status"] == "UU"
    assert (repository / "readme.md").read_bytes() == before


def test_conflict_preview_is_escaped_and_precedes_resolution_choices(
        auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository)
    current = "<script>current-side</script>\n"
    incoming = "<img src=x onerror=alert(1)>\n"
    (repository / "readme.md").write_text(current, encoding="utf-8")
    _git(repository, "commit", "-am", "Current side")
    _push_remote_change(remote, tmp_path, incoming)
    with app.app_context():
        connected = _module().models.Repository.query.one()
        _module().service.fetch(connected)
        _module().service.start_merge(connected)

    page = _view(auth_client, response.location).data
    assert b"Current branch" in page and b"Incoming" in page
    assert b"&lt;script&gt;current-side&lt;/script&gt;" in page
    assert b"&lt;img src=x onerror=alert(1)&gt;" in page
    assert b"<script>current-side</script>" not in page
    assert page.index(b"Current branch") < page.index(b"Keep current")
    assert b"grid-cols-1 md:grid-cols-2" in page


def test_conflict_preview_marks_deleted_symlink_binary_and_large_versions(repository):
    service = _module().service
    with patch.object(service, "_unmerged_stage_entry", return_value=None):
        deleted = service._conflict_version_preview(None, "readme.md", 3)
    assert deleted["kind"] == "deleted"

    with patch.object(service, "_unmerged_stage_entry", return_value=(0o120000, b"blob")):
        with patch.object(service, "_blob_data") as blob_data:
            symlink = service._conflict_version_preview(None, "readme.md", 2)
    assert symlink["kind"] == "unavailable"
    blob_data.assert_not_called()

    with patch.object(service, "_unmerged_stage_entry", return_value=(0o100644, b"blob")):
        with patch.object(service, "_blob_data", return_value=b"x" * (service.MAX_CONFLICT_PREVIEW_BYTES + 1)):
            large = service._conflict_version_preview(None, "readme.md", 2)
    assert large["kind"] == "large"

    with patch.object(service, "_unmerged_stage_entry", return_value=(0o100644, b"blob")):
        with patch.object(service, "_blob_data", return_value=b"\x01a"):
            binary = service._conflict_version_preview(None, "readme.md", 2)
    assert binary["kind"] == "binary"

    with patch.object(service, "_unmerged_stage_entry", return_value=(0o100644, b"blob")):
        with patch.object(service, "_blob_data", return_value="caf\u00e9\n".encode("utf-8")):
            text = service._conflict_version_preview(None, "readme.md", 2)
    assert text == {"kind": "text", "text": "caf\u00e9\n"}


def test_scan_indexes_external_changes_but_not_git_metadata(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    metadata = repository / ".git" / "private.txt"
    metadata.write_text("must never be indexed", encoding="utf-8")
    assert auth_client.post(response.location + "/scan").status_code == 302
    with app.app_context():
        linked = _module().models.Repository.query.one()
        stored = StoredFile.query.filter_by(drive_id=linked.drive_id).all()
        assert [item.name for item in stored] == ["readme.md"]
        assert stored[0].index.extracted_text == "initial content\n"
    (repository / "readme.md").write_text("external edit\n", encoding="utf-8")
    auth_client.post(response.location + "/scan")
    with app.app_context():
        stored = StoredFile.query.filter_by(name="readme.md").one()
        assert stored.index.extracted_text == "external edit\n"
    assert metadata.exists()
    assert (repository / "readme.md").read_text(encoding="utf-8") == "external edit\n"


def test_same_folder_cannot_become_two_drives(auth_client, app, repository, enabled):
    first = _connect(auth_client, repository)
    second = _connect(auth_client, repository)
    assert second.location == first.location
    with app.app_context():
        assert Drive.query.filter_by(source_path=str(repository.resolve())).count() == 1
        assert _module().models.Repository.query.count() == 1


def test_repositories_are_isolated_by_user(auth_client, client, app, repository, enabled):
    response = _connect(auth_client, repository)
    with app.app_context():
        other = User(username="bob", email="bob@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
    client.post("/logout")
    client.get("/logout")
    client.post("/login", data={"username": "bob", "password": "password123"})
    assert client.get(response.location).status_code == 404
    assert client.post(response.location + "/scan").status_code == 404
    assert client.post(response.location + "/commit", data={
        "paths": ["readme.md"], "message": "Foreign repository",
    }).status_code == 404
    assert client.post(response.location + "/identity", data=IDENTITY).status_code == 404
    assert client.post(response.location + "/credentials", data={"remote_token": "x"}).status_code == 404


@pytest.mark.parametrize("outcome", ["ok", "denied"])
def test_remote_access_check_uses_saved_credentials_and_never_exposes_details(
        auth_client, app, repository, tmp_path, enabled, outcome):
    _setup_bare_upstream(repository, tmp_path)
    _git(repository, "remote", "set-url", "origin", "https://example.test/team/private.git")
    response = _connect(auth_client, repository)
    auth_client.post(response.location + "/credentials", data={
        "remote_username": "PreferredUser", "remote_token": "secret-token-value"})
    service = _module().service
    remote_calls = []

    def fake_ls_remote(url, **kwargs):
        remote_calls.append((url, kwargs))
        if outcome == "denied":
            raise service.HTTPUnauthorized("Basic realm=secret-stderr-output", url)
        return SimpleNamespace(refs={})

    with patch.object(service.porcelain, "ls_remote", side_effect=fake_ls_remote):
        result = auth_client.post(response.location + "/check-access")
    page = _view(auth_client, result.location)

    url, options = remote_calls[0]
    assert url == "https://example.test/team/private.git"
    assert options["username"] == "PreferredUser"
    assert options["password"] == "secret-token-value"
    assert b"secret-" not in page.data
    assert b"Remote credentials (HTTPS username + token, this drive)" in page.data
    assert b'value="PreferredUser"' in page.data
    if outcome == "denied":
        assert b"Remote access was denied" in page.data
    else:
        assert b"Remote read access succeeded using the saved credentials" in page.data
        assert b"authenticated identity is not verified" in page.data


def test_disabled_remote_actions_explain_what_is_missing(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    page = _view(auth_client, response.location).data
    assert b"Configure an origin remote first" in page
    assert b"Configure a branch upstream before fetching" in page
    assert b"Configure a branch upstream first" in page
    assert b"No changed files are available to commit" in page

    (repository / "pending.txt").write_text("pending\n", encoding="utf-8")
    page = _view(auth_client, response.location).data
    assert b"Commit or set aside working-tree changes first" in page  # merge review needs a clean tree
    assert b"Set the commit identity (name and email) in Repository details above" in page
    assert b"Required before committing or merging" in page
    assert b"Configure a branch upstream first" in page  # push: upstream missing, dirty tree is not the blocker

    _git(repository, "add", "pending.txt")
    page = _view(auth_client, response.location).data
    assert b"Unstage files before pushing" not in page  # staged work never blocks a push
    assert b"Unstage files before reviewing a merge" in page
    assert b"already staged" in page


def _remote_failures():
    from dulwich.errors import GitProtocolError
    service = _module().service
    return [
        (service.HTTPUnauthorized("Basic realm=secret", "https://user:secret@example.test/private.git"),
         "Remote access was denied"),
        (service.NotGitRepository("https://example.test/private.git is not a git repository"),
         "Remote access was denied"),
        (service.porcelain.DivergedBranches(b"a" * 40, b"b" * 40), "The remote rejected this push"),
        (GitProtocolError("HTTPSConnectionPool(host='example.test', port=443): Max retries exceeded (secret)"),
         "The remote is unavailable"),
        (GitProtocolError("HTTPSConnectionPool(host='example.test'): Read timed out. (secret)"), "Safe timeout"),
        (ConnectionRefusedError("example.test refused secret"), "The remote is unavailable"),
        (RuntimeError("unexpected private diagnostic secret-output example.test"), "Generic safe fallback"),
    ]


@pytest.mark.parametrize("index", range(7))
def test_remote_errors_are_categorized_without_exposing_details(repository, index):
    service = _module().service
    failure, expected = _remote_failures()[index]

    def operation():
        raise failure

    with pytest.raises(service.GitSyncError) as error:
        service._remote_call(operation, "Generic safe fallback", "Safe timeout")
    assert expected in str(error.value)
    assert "secret" not in str(error.value)
    assert "example.test" not in str(error.value)


def test_fetch_and_push_are_explicit_and_use_local_bare_remote(
        auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository, identity=True)
    (repository / "local.txt").write_text("local commit\n", encoding="utf-8")
    _git(repository, "add", "local.txt")
    _git(repository, "commit", "-m", "Local commit")
    service = _module().service
    original_fetch, original_push = service.porcelain.fetch, service.porcelain.push
    calls = []

    def recording_fetch(repo, *args, **kwargs):
        calls.append(("fetch", args, kwargs))
        return original_fetch(repo, *args, **kwargs)

    def recording_push(repo, *args, **kwargs):
        calls.append(("push", args, kwargs))
        return original_push(repo, *args, **kwargs)

    with app.app_context():
        connected = _module().models.Repository.query.one()
        with patch.object(service.porcelain, "fetch", side_effect=recording_fetch), \
                patch.object(service.porcelain, "push", side_effect=recording_push):
            service.fetch(connected)
            service.push(connected)

    assert [name for name, _args, _kwargs in calls] == ["fetch", "push"]
    _name, fetch_args, fetch_kwargs = calls[0]
    _name, push_args, push_kwargs = calls[1]
    assert fetch_args == ("origin",)
    assert "password" not in fetch_kwargs and "password" not in push_kwargs  # local remote: no credentials
    assert push_args == ("origin", [b"HEAD:refs/heads/main"])
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True, timeout=20,
    ).stdout.strip()
    assert remote_head == _git(repository, "rev-parse", "HEAD").stdout.strip()
    assert _git(repository, "rev-parse", "refs/remotes/origin/main").stdout.strip() == remote_head


@pytest.mark.parametrize("change_kind", ["unstaged", "staged"])
def test_push_sends_commits_and_leaves_uncommitted_work_untouched(
        auth_client, app, repository, tmp_path, enabled, change_kind):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository)
    (repository / "ahead.txt").write_text("ahead\n", encoding="utf-8")
    _git(repository, "add", "ahead.txt")
    _git(repository, "commit", "-m", "Ahead")
    (repository / "pending.txt").write_text("pending\n", encoding="utf-8")
    if change_kind == "staged":
        _git(repository, "add", "pending.txt")
    status_before = _git(repository, "status", "--porcelain").stdout

    with app.app_context():
        connected = _module().models.Repository.query.one()
        _module().service.push(connected)

    assert _git(repository, "status", "--porcelain").stdout == status_before
    assert (repository / "pending.txt").read_text(encoding="utf-8") == "pending\n"
    remote_log = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%s", "main"],
                                capture_output=True, text=True, check=True, timeout=20).stdout
    assert remote_log.splitlines()[0] == "Ahead"
    assert "pending" not in subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                                           capture_output=True, text=True, check=True, timeout=20).stdout


def test_next_step_offers_push_of_earlier_commit_while_files_are_uncommitted(
        auth_client, app, repository, tmp_path, enabled):
    """The user's situation: a commit made earlier, plus new edits on disk, plus the remote ahead."""
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository, identity=True)
    (repository / "readme.md").write_text("my earlier change\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Earlier commit")
    (repository / "notes.md").write_text("still editing\n", encoding="utf-8")

    html = _view(auth_client, response.location).data.decode()
    assert 'data-step="commit"' in html
    assert "1 local commit waiting to be pushed" in html
    assert "1 uncommitted file" in html
    assert "earlier commit can be pushed right away" in html
    assert "Push 1 local commit</button>" in html

    _push_remote_change(remote, tmp_path, "someone else\n")
    auth_client.post(response.location + "/fetch")
    html = _view(auth_client, response.location).data.decode()
    assert 'data-step="commit"' in html
    assert "1 remote commit not merged" in html
    assert "needs a clean working tree" in html
    assert "Push 1 local commit</button>" not in html  # behind: push would be rejected
    assert 'id="gitsync-push-branch" open' in html  # the side-branch alternative is offered instead

    auth_client.post(response.location + "/commit", data={"paths": ["notes.md"], "message": "Notes"})
    html = _view(auth_client, response.location).data.decode()
    assert 'data-step="behind"' in html
    assert "2 local commits waiting to be pushed" in html
    assert "Review upstream merge" in html
    assert "A push would be rejected until they are merged" in html


def test_push_refuses_an_unresolved_merge(auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository)
    (repository / "readme.md").write_text("current\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Local change")
    _push_remote_change(remote, tmp_path, "incoming\n")
    with app.app_context():
        connected = _module().models.Repository.query.one()
        _module().service.fetch(connected)
        _module().service.start_merge(connected)
        with pytest.raises(_module().service.GitSyncError, match="Complete or abort"):
            _module().service.push(connected)


def test_merge_refuses_dirty_or_staged_work(auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository)
    _push_remote_change(remote, tmp_path, "remote change\n")
    with app.app_context():
        connected = _module().models.Repository.query.one()
        _module().service.fetch(connected)
        (repository / "pending.txt").write_text("pending\n", encoding="utf-8")
        _git(repository, "add", "pending.txt")
        with pytest.raises(_module().service.GitSyncError, match="clean working tree"):
            _module().service.start_merge(connected)
        _git(repository, "reset", "--hard", "HEAD")
        (repository / "pending.txt").write_text("pending\n", encoding="utf-8")
        with pytest.raises(_module().service.GitSyncError, match="clean working tree"):
            _module().service.start_merge(connected)


@pytest.mark.parametrize(("choice", "expected"), [("ours", "current\n"), ("theirs", "incoming\n")])
def test_conflict_review_resolution_and_completion_are_local(
        auth_client, app, repository, tmp_path, enabled, choice, expected):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository, identity=True)
    (repository / "readme.md").write_text("current\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Local change")
    remote_head = _push_remote_change(remote, tmp_path, "incoming\n")

    assert auth_client.post(response.location + "/fetch").status_code == 302
    page = _view(auth_client, response.location)
    assert b"ahead 1, behind 1" in page.data
    service = _module().service
    original_merge = service.porcelain.merge
    merges = []

    def recording_merge(repo, committish, **kwargs):
        merges.append((committish, kwargs))
        return original_merge(repo, committish, **kwargs)

    with patch.object(service.porcelain, "merge", side_effect=recording_merge):
        assert auth_client.post(response.location + "/merge").status_code == 302
    assert merges == [(b"refs/remotes/origin/main", {"no_commit": True, "no_ff": True})]
    original_diff = service._diff
    diff_calls = []

    def recording_diff(repo, **kwargs):
        diff_calls.append(kwargs)
        return original_diff(repo, **kwargs)

    with patch.object(service, "_diff", side_effect=recording_diff):
        page = _view(auth_client, response.location)
    assert b"Merge review" in page.data
    assert b"readme.md" in page.data
    assert b'class="diff-add"' in page.data
    assert b'class="diff-hunk"' in page.data
    assert b"current\n" in page.data and b"incoming\n" in page.data
    assert len(diff_calls) == 1  # the merge diff reuses the working-tree diff
    assert b"Keep current" in page.data
    assert b"Use incoming" in page.data
    assert auth_client.post(response.location + "/merge/complete").status_code == 302
    assert b"Resolve every listed conflict" in _view(auth_client, response.location).data

    resolved = auth_client.post(response.location + "/merge/resolve", data={
        "paths": ["readme.md"], "choice": choice,
    })
    assert resolved.status_code == 302
    assert (repository / "readme.md").read_text(encoding="utf-8") == expected
    page = _view(auth_client, response.location)
    assert b"All conflicts are resolved" in page.data
    assert auth_client.post(response.location + "/merge/complete").status_code == 302

    parents = _git(repository, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3
    assert parents[2] == remote_head
    assert _git(repository, "log", "-1", "--format=%an <%ae>").stdout.strip() == "Local Committer <local@example.com>"
    assert _git(repository, "status", "--porcelain").stdout == ""
    assert subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True, timeout=20,
    ).stdout.strip() == remote_head
    assert not _git(repository, "rev-parse", "--verify", "MERGE_HEAD", check=False).stdout.strip()


def test_truncated_merge_diff_requires_explicit_acknowledgement(
        auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository, identity=True)
    checkout = remote.parent / f"{remote.stem}-large-remote"
    subprocess.run(["git", "clone", str(remote), str(checkout)], capture_output=True, check=True, timeout=20)
    (checkout / "large-incoming.txt").write_text("incoming line\n" * 18000, encoding="utf-8")
    _git(checkout, "add", "large-incoming.txt")
    _git(checkout, "commit", "-m", "Large remote addition")
    _git(checkout, "push", "origin", "main")

    with app.app_context():
        connected = _module().models.Repository.query.one()
        _module().service.fetch(connected)
        _module().service.start_merge(connected)
        state = _module().service.inspect(connected)
        assert state["merge_diff_truncated"]

    page = _view(auth_client, response.location).data
    assert b"Merge diff truncated at 200,000 characters" in page
    assert b'name="acknowledge_truncated"' in page
    assert b"required" in page
    result = auth_client.post(response.location + "/merge/complete")
    assert b"explicitly acknowledge" in _view(auth_client, result.location).data
    assert _git(repository, "rev-parse", "--verify", "MERGE_HEAD", check=False).stdout.strip()

    result = auth_client.post(response.location + "/merge/complete", data={
        "acknowledge_truncated": "yes",
    })
    assert b"Created merge commit" in _view(auth_client, result.location).data
    assert not _git(repository, "rev-parse", "--verify", "MERGE_HEAD", check=False).stdout.strip()


def test_abort_merge_preserves_local_state_and_refuses_untracked_files(
        auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    (repository / "safe.txt").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "safe.txt")
    _git(repository, "commit", "-m", "Add clean file")
    _git(repository, "push", "origin", "main")
    response = _connect(auth_client, repository)
    (repository / "readme.md").write_text("current\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Local change")
    _push_remote_change(remote, tmp_path, "incoming\n")
    with app.app_context():
        connected = _module().models.Repository.query.one()
        _module().service.fetch(connected)
        _module().service.start_merge(connected)
    original_head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    (repository / "user-file.txt").write_text("keep me\n", encoding="utf-8")

    with app.app_context():
        connected = _module().models.Repository.query.one()
        with pytest.raises(_module().service.GitSyncError, match="untracked files exist"):
            _module().service.abort_merge(connected)
    assert (repository / "user-file.txt").read_text(encoding="utf-8") == "keep me\n"
    (repository / "user-file.txt").unlink()
    (repository / "safe.txt").write_text("user edit\n", encoding="utf-8")
    with app.app_context():
        connected = _module().models.Repository.query.one()
        with pytest.raises(_module().service.GitSyncError, match="unstaged changes exist"):
            _module().service.abort_merge(connected)
    (repository / "safe.txt").write_text("baseline\n", encoding="utf-8")
    assert auth_client.post(response.location + "/merge/abort").status_code == 302
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == original_head
    assert (repository / "readme.md").read_text(encoding="utf-8") == "current\n"
    assert (repository / "safe.txt").read_text(encoding="utf-8") == "baseline\n"
    assert _git(repository, "status", "--porcelain").stdout == ""
    assert not _git(repository, "rev-parse", "--verify", "MERGE_HEAD", check=False).stdout.strip()


def test_conflict_resolution_rejects_traversal_and_symlinks(repository):
    service = _module().service
    with pytest.raises(service.GitSyncError, match="not safe"):
        service._safe_conflict_path(repository, "../outside.txt", {"../outside.txt"})
    with pytest.raises(service.GitSyncError, match="not safe"):
        service._safe_conflict_path(repository, "C:/outside.txt", {"C:/outside.txt"})
    with patch.object(service.Path, "is_symlink", return_value=True):
        with pytest.raises(service.GitSyncError, match="Symlinks"):
            service._safe_conflict_path(repository, "readme.md", {"readme.md"})


def test_conflict_resolution_rejects_paths_not_currently_conflicted(
        auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository)
    (repository / "readme.md").write_text("current\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Local change")
    _push_remote_change(remote, tmp_path, "incoming\n")
    with app.app_context():
        connected = _module().models.Repository.query.one()
        _module().service.fetch(connected)
        _module().service.start_merge(connected)

    result = auth_client.post(response.location + "/merge/resolve", data={
        "paths": ["../readme.md"], "choice": "ours",
    })
    assert b"not a currently conflicted file" in _view(auth_client, result.location).data
    assert _git(repository, "status", "--porcelain").stdout.startswith("UU readme.md")


def test_conflict_resolution_can_choose_a_deleted_side(auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository, identity=True)
    (repository / "readme.md").write_text("current\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Local change")
    checkout = remote.parent / f"{remote.stem}-remote-delete"
    subprocess.run(["git", "clone", str(remote), str(checkout)], capture_output=True, check=True, timeout=20)
    (checkout / "readme.md").unlink()
    _git(checkout, "add", "-u", "readme.md")
    _git(checkout, "commit", "-m", "Delete readme")
    _git(checkout, "push", "origin", "main")

    with app.app_context():
        connected = _module().models.Repository.query.one()
        _module().service.fetch(connected)
        _module().service.start_merge(connected)
        _module().service.resolve_conflicts(connected, ["readme.md"], "theirs")
        assert not (repository / "readme.md").exists()
        assert _module().service.inspect(connected)["conflict_count"] == 0
        _module().service.complete_merge(connected)
    assert not (repository / "readme.md").exists()
    assert _git(repository, "status", "--porcelain").stdout == ""
    assert "readme.md" not in _git(repository, "ls-tree", "--name-only", "HEAD").stdout


def test_merge_completion_requires_identity(auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository)
    _push_remote_change(remote, tmp_path, "incoming\n")
    with app.app_context():
        connected = _module().models.Repository.query.one()
        _module().service.fetch(connected)
        _module().service.start_merge(connected)
    page = _view(auth_client, response.location).data
    assert b"Merge review" in page
    assert b"Set the commit identity (name and email) in Repository details before completing the merge" in page
    result = auth_client.post(response.location + "/merge/complete")
    assert b"before completing the merge" in _view(auth_client, result.location).data
    assert _git(repository, "rev-parse", "--verify", "MERGE_HEAD", check=False).stdout.strip()
    _configure_identity(auth_client, response.location)
    result = auth_client.post(response.location + "/merge/complete")
    assert b"Created merge commit" in _view(auth_client, result.location).data
    assert not _git(repository, "rev-parse", "--verify", "MERGE_HEAD", check=False).stdout.strip()


def test_remote_credentials_are_not_displayed(auth_client, app, repository, enabled):
    _git(repository, "remote", "add", "origin", "https://user:secret@example.com/team/wiki.git?token=secret")
    response = _connect(auth_client, repository)
    page = _view(auth_client, response.location)
    assert b"secret" not in page.data
    assert b"https://example.com/team/wiki.git" in page.data
    assert page.data.count(b"Remote operations use the credentials saved for this drive") == 1


def test_unborn_repository(auth_client, app, tmp_path, enabled):
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "first.txt").write_text("first\n", encoding="utf-8")
    _git(tmp_path, "add", "first.txt")
    response = _connect(auth_client, tmp_path)
    assert b"+first" in _view(auth_client, response.location).data


def test_module_migration_roundtrip(tmp_path):
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    first = importlib.import_module(
        "docindex_module_gitsync.migrations.versions.gitsync_0001_repositories")
    second = importlib.import_module(
        "docindex_module_gitsync.migrations.versions.gitsync_0002_identity_credentials")
    assert second.down_revision == first.revision == "gitsync_0001"
    assert second.revision == "gitsync_0002"
    model_columns = {column.name for column in _module().models.Repository.__table__.columns}
    added = {"author_name", "author_email", "remote_username", "remote_token"}
    engine = sa.create_engine("sqlite:///" + str(tmp_path / "migration.sqlite"))

    def columns(connection):
        return {item["name"] for item in sa.inspect(connection).get_columns("mod_gitsync_repository")}

    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text("CREATE TABLE drives (id INTEGER PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(connection)):
            first.upgrade()
            assert columns(connection) == model_columns - added
            connection.execute(sa.text(
                "INSERT INTO mod_gitsync_repository (user_id, name, path, account) VALUES (1, 'r', '/r', '')"))
            second.upgrade()
            assert columns(connection) == model_columns
            row = connection.execute(sa.text(
                "SELECT author_name, author_email, remote_username, remote_token FROM mod_gitsync_repository")).one()
            assert tuple(row) == ("", "", None, None)  # existing rows get usable defaults
            second.downgrade()
            assert columns(connection) == model_columns - added
            first.downgrade()
            assert "mod_gitsync_repository" not in sa.inspect(connection).get_table_names()
            first.upgrade()
            second.upgrade()
            assert columns(connection) == model_columns
    engine.dispose()
