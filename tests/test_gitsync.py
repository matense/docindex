import importlib
import subprocess
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Drive, ModuleState, StoredFile, User, utcnow


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


def _connect(client, path, **data):
    return client.post("/m/gitsync/connect", data={"path": str(path), **data})


def _configure_identity(path):
    _git(path, "config", "--local", "user.name", "Local Committer")
    _git(path, "config", "--local", "user.email", "local@example.com")


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
    assert client.post("/m/gitsync/1/disconnect").status_code == 302


def test_gitsync_disabled(auth_client):
    assert auth_client.get("/m/gitsync/").status_code == 404


def test_gitsync_enabled(auth_client, app):
    with app.app_context():
        db.session.add(ModuleState(name="gitsync", enabled=True))
        db.session.commit()
    response = auth_client.get("/m/gitsync/")
    assert response.status_code == 200
    assert b"Git Sync" in response.data


def test_git_repo_return_link_only_when_enabled(auth_client, app, enabled):
    html = auth_client.get("/m/gitsync/").data.decode()
    assert 'href="/m/gitdrives/"' not in html
    with app.app_context():
        db.session.add(ModuleState(name="gitdrives", enabled=True))
        db.session.commit()
    html = auth_client.get("/m/gitsync/").data.decode()
    assert 'href="/m/gitdrives/"' in html


def test_connect_and_inspect_real_repository(auth_client, app, repository, enabled):
    initial = _git(repository, "rev-parse", "HEAD").stdout.strip()
    response = _connect(auth_client, repository, account="AnderPT")
    assert response.status_code == 302
    page = auth_client.get(response.location)
    assert page.status_code == 200
    assert b"Working tree clean" in page.data
    assert b"AnderPT" in page.data
    with app.app_context():
        connected = _module().models.Repository.query.one()
        assert connected.baseline == initial
        assert connected.drive.is_readonly
        assert connected.path == str(repository.resolve())
    with auth_client.session_transaction() as session:
        assert session["drive_id"] == connected.drive_id
    assert not _git(repository, "config", "--local", "--get", "credential.username", check=False).stdout
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == initial


def test_reconnect_selects_linked_drive_as_current(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    with app.app_context():
        connected = _module().models.Repository.query.one()
        repository_id = connected.id
        drive_id = connected.drive_id
        connected.drive_id = None
        db.session.commit()

    with auth_client.session_transaction() as session:
        session["drive_id"] = 0

    result = auth_client.post(f"/m/gitsync/{repository_id}/reconnect")
    assert result.status_code == 302
    with auth_client.session_transaction() as session:
        assert session["drive_id"] == drive_id


def test_disconnect_preserves_local_repository_and_drive_and_allows_reconnect(
        auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    assert auth_client.post(response.location + "/scan").status_code == 302
    with app.app_context():
        connected = _module().models.Repository.query.one()
        drive = connected.drive
        drive_id = drive.id
        stored_files = StoredFile.query.filter_by(drive_id=drive_id).all()
        stored_file_ids = [item.id for item in stored_files]
        assert [item.name for item in stored_files] == ["readme.md"]

    page = auth_client.get("/m/gitsync/")
    assert b"Disconnect" in page.data
    assert b"local folder, Git data, or linked drive and its files" in page.data
    result = auth_client.post(f"/m/gitsync/{connected.id}/disconnect")
    assert result.status_code == 302
    page = auth_client.get(result.location)
    assert b"local folder, Git data, and linked drive and files were not deleted" in page.data
    assert (repository / "readme.md").read_text(encoding="utf-8") == "initial content\n"
    assert (repository / ".git").is_dir()

    with app.app_context():
        assert _module().models.Repository.query.count() == 0
        assert db.session.get(Drive, drive_id) is not None
        assert [item.id for item in StoredFile.query.filter_by(drive_id=drive_id).all()] == stored_file_ids

    reconnected = _connect(auth_client, repository)
    assert reconnected.status_code == 302
    with app.app_context():
        connected_again = _module().models.Repository.query.one()
        assert connected_again.drive_id == drive_id


def test_connect_replaces_orphaned_repository_with_null_drive_id(auth_client, app, repository, enabled):
    _connect(auth_client, repository)
    with app.app_context():
        orphaned = _module().models.Repository.query.one()
        drive_id = orphaned.drive_id
        orphaned.drive_id = None
        db.session.commit()

    response = _connect(auth_client, repository)
    assert response.status_code == 302
    with app.app_context():
        connected = _module().models.Repository.query.one()
        assert connected.drive_id == drive_id
        assert _module().models.Repository.query.count() == 1


def test_reconnect_missing_drive_preserves_repository_and_local_git_state(
        auth_client, app, repository, enabled):
    initial = _git(repository, "rev-parse", "HEAD").stdout.strip()
    config_before = (repository / ".git" / "config").read_bytes()
    response = _connect(auth_client, repository, name="Preserved name", account="PreservedAccount")
    with app.app_context():
        connected = _module().models.Repository.query.one()
        repository_id = connected.id
        baseline = connected.baseline
        old_drive = connected.drive
        connected.drive_id = None
        db.session.commit()
        db.session.delete(old_drive)
        db.session.commit()

    detail = auth_client.get(response.location)
    assert b"Git checkout still exists" in detail.data
    assert b"Reconnect drive and index" in detail.data
    assert b"Index local files" not in detail.data
    assert b"Open drive" not in detail.data
    listing = auth_client.get("/m/gitsync/")
    assert b"Drive link missing" in listing.data

    reconnect_response = auth_client.post(f"/m/gitsync/{repository_id}/reconnect")
    page = auth_client.get(reconnect_response.location)
    assert b"Repository reconnected and local indexing finished" in page.data
    with app.app_context():
        connected = db.session.get(_module().models.Repository, repository_id)
        assert connected is not None
        assert connected.name == "Preserved name"
        assert connected.account == "PreservedAccount"
        assert connected.baseline == baseline == initial
        assert connected.drive is not None
        assert connected.drive.source_path == str(repository.resolve())
        assert connected.drive.is_readonly
        assert [item.name for item in StoredFile.query.filter_by(drive_id=connected.drive_id).all()] == ["readme.md"]

    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == initial
    assert _git(repository, "status", "--porcelain").stdout == ""
    assert (repository / ".git" / "config").read_bytes() == config_before
    assert (repository / "readme.md").read_text(encoding="utf-8") == "initial content\n"
    page = auth_client.get(reconnect_response.location)
    assert b"Index local files" in page.data
    assert b"Open drive" in page.data
    assert b"Reconnect drive and index" not in page.data


def test_reconnect_reuses_existing_mirror_drive(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    with app.app_context():
        connected = _module().models.Repository.query.one()
        repository_id = connected.id
        drive_id = connected.drive_id
        baseline = connected.baseline
        connected.drive_id = None
        db.session.commit()

    result = auth_client.post(f"/m/gitsync/{repository_id}/reconnect")
    assert b"Repository reconnected and local indexing finished" in auth_client.get(result.location).data
    with app.app_context():
        connected = db.session.get(_module().models.Repository, repository_id)
        assert connected.drive_id == drive_id
        assert connected.baseline == baseline
        assert Drive.query.count() == 1


def test_reconnect_restores_linked_trashed_mirror(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    auth_client.post(response.location + "/scan")
    with app.app_context():
        connected = _module().models.Repository.query.one()
        repository_id = connected.id
        drive = connected.drive
        drive_id = drive.id
        trashed_at = utcnow().replace(tzinfo=None)
        drive.deleted_at = trashed_at
        stored = StoredFile.query.filter_by(drive_id=drive_id).one()
        stored.deleted_at = trashed_at
        db.session.commit()

    detail = auth_client.get(response.location)
    assert b"DocIndex drive is in the trash" in detail.data
    assert b"Index local files" not in detail.data
    assert b"Open drive" not in detail.data
    listing = auth_client.get("/m/gitsync/")
    assert b"Drive trashed" in listing.data

    result = auth_client.post(f"/m/gitsync/{repository_id}/reconnect")
    assert b"Repository reconnected and local indexing finished" in auth_client.get(result.location).data
    with app.app_context():
        connected = db.session.get(_module().models.Repository, repository_id)
        stored = StoredFile.query.filter_by(drive_id=drive_id).one()
        assert connected.drive_id == drive_id
        assert connected.drive.deleted_at is None
        assert stored.deleted_at is None


def test_reconnect_rolls_back_drive_restore_when_index_restore_fails(
        auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    auth_client.post(response.location + "/scan")
    with app.app_context():
        connected = _module().models.Repository.query.one()
        repository_id = connected.id
        drive = connected.drive
        drive_id = drive.id
        trashed_at = utcnow().replace(tzinfo=None)
        drive.deleted_at = trashed_at
        StoredFile.query.filter_by(drive_id=drive_id).one().deleted_at = trashed_at
        db.session.commit()

    with patch("app.services.search_service.fts_upsert", side_effect=RuntimeError("index failure")):
        result = auth_client.post(f"/m/gitsync/{repository_id}/reconnect")

    assert b"Could not reconnect the repository drive" in auth_client.get(result.location).data
    with app.app_context():
        connected = db.session.get(_module().models.Repository, repository_id)
        drive = db.session.get(Drive, drive_id)
        stored = StoredFile.query.filter_by(drive_id=drive_id).one()
        assert connected.drive_id == drive_id
        assert drive.deleted_at == trashed_at
        assert stored.deleted_at == trashed_at


def test_reconnect_rejects_conflicting_shared_drive_without_changes(
        auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    with app.app_context():
        connected = _module().models.Repository.query.one()
        repository_id = connected.id
        drive = connected.drive
        connected.drive_id = None
        drive.sync_mode = "shared"
        db.session.commit()
        drive_id = drive.id

    result = auth_client.post(f"/m/gitsync/{repository_id}/reconnect")
    page = auth_client.get(result.location)
    assert b"writable, shared, or remote drive" in page.data
    with app.app_context():
        connected = db.session.get(_module().models.Repository, repository_id)
        drive = db.session.get(Drive, drive_id)
        assert connected.drive_id is None
        assert drive.sync_mode == "shared"
        assert Drive.query.count() == 1


def test_reconnect_never_attaches_another_users_mirror(
        auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    with app.app_context():
        connected = _module().models.Repository.query.one()
        repository_id = connected.id
        own_drive = connected.drive
        connected.drive_id = None
        other = User(username="bob", email="bob@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.flush()
        foreign_drive = Drive(name="Foreign mirror", user_id=other.id,
                              source_path=str(repository.resolve()), sync_mode="mirror")
        db.session.add(foreign_drive)
        db.session.delete(own_drive)
        db.session.commit()

    result = auth_client.post(f"/m/gitsync/{repository_id}/reconnect")
    assert b"another user" in auth_client.get(result.location).data
    with app.app_context():
        connected = db.session.get(_module().models.Repository, repository_id)
        assert connected.drive_id is None
        assert Drive.query.filter_by(
            user_id=connected.user_id, source_path=str(repository.resolve())).count() == 0


def test_status_diff_and_initial_baseline(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    (repository / "readme.md").write_text("changed content\n", encoding="utf-8")
    (repository / "new file.txt").write_text("new\n", encoding="utf-8")
    page = auth_client.get(response.location)
    assert b'<span class="diff-add">+changed content\n</span>' in page.data
    assert b'<span class="diff-del">-initial content\n</span>' in page.data
    assert b"new file.txt" in page.data
    _git(repository, "add", "readme.md")
    _git(repository, "commit", "-m", "External change")
    assert b"+changed content" not in auth_client.get(response.location).data
    assert b"+changed content" in auth_client.get(response.location + "?basis=initial").data


def test_diffs_are_grouped_and_navigable_by_file(auth_client, app, repository, enabled):
    (repository / "second.txt").write_text("second original\n", encoding="utf-8")
    _git(repository, "add", "second.txt")
    _git(repository, "commit", "-m", "Add second file")
    response = _connect(auth_client, repository)
    (repository / "readme.md").write_text("readme changed\n", encoding="utf-8")
    (repository / "second.txt").write_text("second changed\n", encoding="utf-8")

    page = auth_client.get(response.location)
    with app.app_context():
        state = _module().service.inspect(_module().models.Repository.query.one())
    assert len(state["diff_files"]) == 2
    assert "readme.md" in " ".join(file["header"] for file in state["diff_files"])
    assert b"data-diff-browser" in page.data
    assert b"gitsync-diff-navigator" in page.data
    assert b"<details class=\"gitsync-diff-file" in page.data


def test_changed_files_beyond_500_are_searchable_and_selectable(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    _configure_identity(repository)
    for index in range(505):
        (repository / f"change-{index:03}.txt").write_text("change\n", encoding="utf-8")

    page = auth_client.get(response.location)
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
    response = _connect(auth_client, repository)
    _configure_identity(repository)
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
    assert b"Created local commit" in auth_client.get(result.location).data
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
    response = _connect(auth_client, repository)
    _configure_identity(repository)
    (repository / "currently-changed.txt").write_text("change\n", encoding="utf-8")
    previous_head = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = auth_client.post(response.location + "/commit", data={
        "paths": [submitted_path], "message": "Should not commit",
    })

    page = auth_client.get(result.location)
    assert b"stale or invalid" in page.data
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == previous_head
    assert not _git(repository, "diff", "--cached", "--name-only").stdout.strip()


def test_commit_rejects_stale_selection(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    _configure_identity(repository)
    stale_path = repository / "stale.txt"
    stale_path.write_text("was changed\n", encoding="utf-8")
    page = auth_client.get(response.location)
    assert b"stale.txt" in page.data
    stale_path.unlink()
    (repository / "current.txt").write_text("current change\n", encoding="utf-8")
    previous_head = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = auth_client.post(response.location + "/commit", data={
        "paths": ["stale.txt"], "message": "Stale selection",
    })

    assert b"stale or invalid" in auth_client.get(result.location).data
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == previous_head
    assert not _git(repository, "diff", "--cached", "--name-only").stdout.strip()


def test_commit_rejects_pre_staged_changes(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    _configure_identity(repository)
    (repository / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repository, "add", "--", "staged.txt")
    (repository / "selected.txt").write_text("selected\n", encoding="utf-8")
    previous_head = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = auth_client.post(response.location + "/commit", data={
        "paths": ["selected.txt"], "message": "Must not include staged work",
    })

    assert b"already staged" in auth_client.get(result.location).data
    assert _git(repository, "diff", "--cached", "--name-only").stdout.strip() == "staged.txt"
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == previous_head


def test_commit_rejects_unresolved_conflicts(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    _configure_identity(repository)
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

    assert b"Resolve all merge conflicts" in auth_client.get(result.location).data
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == previous_head


def test_commit_performs_no_remote_operations(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    _configure_identity(repository)
    (repository / "local.txt").write_text("local only\n", encoding="utf-8")
    service = _module().service
    original_run = subprocess.run
    commands = []

    def recording_run(args, **kwargs):
        commands.append(args)
        return original_run(args, **kwargs)

    with patch.object(service.subprocess, "run", side_effect=recording_run):
        result = auth_client.post(response.location + "/commit", data={
            "paths": ["local.txt"], "message": "Local only",
        })

    assert result.status_code == 302
    git_subcommands = [args[args.index("-C") + 2] for args in commands if "-C" in args]
    assert not {"fetch", "pull", "push", "merge"}.intersection(git_subcommands)


def test_staged_rename(auth_client, app, repository, enabled):
    _connect(auth_client, repository)
    _git(repository, "mv", "readme.md", "renamed file.md")
    with app.app_context():
        state = _module().service.inspect(_module().models.Repository.query.one())
    assert state["changes"][0]["original"] == "readme.md"
    assert state["changes"][0]["path"] == "renamed file.md"


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
    _configure_identity(repository)
    current = "<script>current-side</script>\n"
    incoming = "<img src=x onerror=alert(1)>\n"
    (repository / "readme.md").write_text(current, encoding="utf-8")
    _git(repository, "commit", "-am", "Current side")
    _push_remote_change(remote, tmp_path, incoming)
    with app.app_context():
        connected = _module().models.Repository.query.one()
        _module().service.fetch(connected)
        _module().service.start_merge(connected)

    page = auth_client.get(response.location).data
    assert b"Current branch" in page and b"Incoming" in page
    assert b"&lt;script&gt;current-side&lt;/script&gt;" in page
    assert b"&lt;img src=x onerror=alert(1)&gt;" in page
    assert b"<script>current-side</script>" not in page
    assert page.index(b"Current branch") < page.index(b"Keep current")
    assert b"grid-cols-1 md:grid-cols-2" in page


def test_conflict_preview_marks_deleted_symlink_binary_and_large_versions(repository):
    service = _module().service
    with patch.object(service, "_unmerged_stage_entry", return_value=None):
        deleted = service._conflict_version_preview(repository, "readme.md", 3)
    assert deleted["kind"] == "deleted"

    with patch.object(service, "_unmerged_stage_entry", return_value=("120000", "blob")):
        with patch.object(service.subprocess, "run") as run:
            symlink = service._conflict_version_preview(repository, "readme.md", 2)
    assert symlink["kind"] == "unavailable"
    run.assert_not_called()

    with patch.object(service, "_unmerged_stage_entry", return_value=("100644", "blob")):
        with patch.object(service, "_run", return_value=str(service.MAX_CONFLICT_PREVIEW_BYTES + 1)):
            with patch.object(service.subprocess, "run") as run:
                large = service._conflict_version_preview(repository, "readme.md", 2)
    assert large["kind"] == "large"
    run.assert_not_called()

    with patch.object(service, "_unmerged_stage_entry", return_value=("100644", "blob")):
        with patch.object(service, "_run", return_value="2"):
            with patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, b"\x01a", b"")):
                binary = service._conflict_version_preview(repository, "readme.md", 2)
    assert binary["kind"] == "binary"


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


def test_duplicate_and_non_repository_rejected(auth_client, app, repository, tmp_path, enabled):
    _connect(auth_client, repository)
    response = _connect(auth_client, repository)
    assert b"already connected" in auth_client.get(response.location).data
    with app.app_context():
        assert _module().models.Repository.query.count() == 1
    subfolder = repository / "subfolder"
    subfolder.mkdir()
    response = _connect(auth_client, subfolder)
    assert b"root folder" in auth_client.get(response.location).data
    _git(repository, "init", "--bare", str(tmp_path / "bare.git"))
    response = _connect(auth_client, tmp_path / "bare.git")
    assert response.location.endswith("/m/gitsync/")


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
    assert client.post(response.location + "/disconnect").status_code == 404


def test_accounts_list_uses_gcm_list_only(auth_client, app, enabled):
    result = subprocess.CompletedProcess([], 0, "AnderPT\nother-account\n", "")
    with patch.object(_module().service.subprocess, "run", return_value=result) as run:
        response = auth_client.get("/m/gitsync/")
    assert b"AnderPT" in response.data
    assert run.call_args.args[0] == ["git", "credential-manager", "github", "list"]


@pytest.mark.parametrize("returncode", [0, 128])
def test_remote_access_check_is_scoped_and_never_exposes_git_output(
        auth_client, app, repository, tmp_path, enabled, returncode):
    _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository, account="PreferredUser")
    service = _module().service
    original_run = subprocess.run
    remote_calls = []

    def recording_run(args, **kwargs):
        if "ls-remote" in args:
            remote_calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, returncode, b"secret-token-output", b"secret-stderr-output")
        return original_run(args, **kwargs)

    with patch.object(service.subprocess, "run", side_effect=recording_run):
        result = auth_client.post(response.location + "/check-access")
    page = auth_client.get(result.location)

    command, options = remote_calls[0]
    assert command == [
        "git", "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false",
        "-c", "credential.username=PreferredUser", "-C", str(repository.resolve()), "ls-remote", "origin",
    ]
    assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert options["env"]["GCM_INTERACTIVE"] == "never"
    assert b"secret-" not in page.data
    assert b"Preferred username hint" in page.data
    assert b"not verified identity" in page.data
    if returncode:
        assert b"Remote read access failed" in page.data
    else:
        assert b"Remote read access succeeded" in page.data
        assert b"authenticated identity is not verified" in page.data


def test_disabled_remote_actions_explain_what_is_missing(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    page = auth_client.get(response.location).data
    assert b"Configure an origin remote first" in page
    assert b"Configure a branch upstream before fetching" in page
    assert b"Configure a branch upstream first" in page
    assert b"No changed files are available to commit" in page

    (repository / "pending.txt").write_text("pending\n", encoding="utf-8")
    page = auth_client.get(response.location).data
    assert b"Commit or set aside working-tree changes first" in page
    assert b"Configure user.name and user.email" in page

    _git(repository, "add", "pending.txt")
    page = auth_client.get(response.location).data
    assert b"Unstage files before pushing" in page
    assert b"Unstage files before reviewing a merge" in page
    assert b"already staged" in page


@pytest.mark.parametrize(("stderr", "expected"), [
    ("fatal: Authentication failed for https://user:secret@example.test/private.git", "Remote access was denied"),
    ("! [rejected] main -> main (non-fast-forward)", "The remote rejected this push"),
    ("fatal: unable to access host: Could not resolve host", "The remote is unavailable"),
    ("fatal: unexpected private diagnostic secret-output", "Generic safe fallback"),
])
def test_remote_errors_are_categorized_without_exposing_stderr(repository, stderr, expected):
    service = _module().service
    result = subprocess.CompletedProcess([], 1, b"secret-token-output", stderr.encode())
    with patch.object(service.subprocess, "run", return_value=result):
        with pytest.raises(service.GitSyncError) as error:
            service._run_remote(repository, ["push", "origin"], None,
                                "Generic safe fallback", "Safe timeout")
    assert expected in str(error.value)
    assert "secret" not in str(error.value)
    assert "example.test" not in str(error.value)


def test_fetch_and_push_are_explicit_and_use_local_bare_remote(
        auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository, account="PreferredUser")
    _configure_identity(repository)
    (repository / "local.txt").write_text("local commit\n", encoding="utf-8")
    _git(repository, "add", "local.txt")
    _git(repository, "commit", "-m", "Local commit")
    service = _module().service
    original_run = subprocess.run
    commands = []

    def recording_run(args, **kwargs):
        commands.append(args)
        return original_run(args, **kwargs)

    with app.app_context():
        connected = _module().models.Repository.query.one()
        with patch.object(service.subprocess, "run", side_effect=recording_run):
            service.fetch(connected)
            service.push(connected)

    subcommands = [command[command.index("-C") + 2] for command in commands if "-C" in command]
    assert "fetch" in subcommands
    assert "push" in subcommands
    fetch_command = next(command for command in commands if "fetch" in command)
    push_command = next(command for command in commands if "push" in command)
    remote_commands = [command for command in commands if "credential.username=PreferredUser" in command]
    assert [command[command.index("-C") + 2] for command in remote_commands] == ["fetch", "push"]
    assert ["-c", "credential.username=PreferredUser"] == fetch_command[
        fetch_command.index("credential.username=PreferredUser") - 1:
        fetch_command.index("credential.username=PreferredUser") + 1
    ]
    assert fetch_command[-3:] == ["fetch", "--no-tags", "origin"]
    assert push_command[-3:] == ["push", "origin", "HEAD:refs/heads/main"]
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True, timeout=20,
    ).stdout.strip()
    assert remote_head == _git(repository, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("change_kind", ["unstaged", "staged"])
def test_push_refuses_dirty_or_staged_work(auth_client, app, repository, tmp_path, enabled, change_kind):
    _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository)
    _configure_identity(repository)
    (repository / "ahead.txt").write_text("ahead\n", encoding="utf-8")
    _git(repository, "add", "ahead.txt")
    _git(repository, "commit", "-m", "Ahead")
    (repository / "pending.txt").write_text("pending\n", encoding="utf-8")
    if change_kind == "staged":
        _git(repository, "add", "pending.txt")

    with app.app_context():
        connected = _module().models.Repository.query.one()
        with pytest.raises(_module().service.GitSyncError, match="clean working tree"):
            _module().service.push(connected)


def test_push_refuses_an_unresolved_merge(auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository)
    _configure_identity(repository)
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
    response = _connect(auth_client, repository, account="PreferredUser")
    _configure_identity(repository)
    (repository / "readme.md").write_text("current\n", encoding="utf-8")
    _git(repository, "commit", "-am", "Local change")
    remote_head = _push_remote_change(remote, tmp_path, "incoming\n")

    assert auth_client.post(response.location + "/fetch").status_code == 302
    page = auth_client.get(response.location)
    assert b"ahead 1, behind 1" in page.data
    service = _module().service
    original_run = subprocess.run
    commands = []

    def recording_run(args, **kwargs):
        commands.append(args)
        return original_run(args, **kwargs)

    with patch.object(service.subprocess, "run", side_effect=recording_run):
        assert auth_client.post(response.location + "/merge").status_code == 302
    merge_command = next(command for command in commands if "merge" in command)
    assert "--no-ff" in merge_command
    assert "--no-commit" in merge_command
    assert "refs/remotes/origin/main" in merge_command
    service = _module().service
    original_service_run = service._run
    inspected_commands = []

    def recording_service_run(path, args, **kwargs):
        inspected_commands.append(args)
        return original_service_run(path, args, **kwargs)

    with patch.object(service, "_run", side_effect=recording_service_run):
        page = auth_client.get(response.location)
    assert b"Merge review" in page.data
    assert b"readme.md" in page.data
    assert b'class="diff-add"' in page.data
    assert b'class="diff-hunk"' in page.data
    assert b"current\n" in page.data and b"incoming\n" in page.data
    diff_commands = [args for args in inspected_commands if args[0] == "diff" and args[-1] == "--"]
    assert len(diff_commands) == 1
    assert b"Keep current" in page.data
    assert b"Use incoming" in page.data
    assert auth_client.post(response.location + "/merge/complete").status_code == 302
    assert b"Resolve every listed conflict" in auth_client.get(response.location).data

    resolved = auth_client.post(response.location + "/merge/resolve", data={
        "paths": ["readme.md"], "choice": choice,
    })
    assert resolved.status_code == 302
    assert (repository / "readme.md").read_text(encoding="utf-8") == expected
    page = auth_client.get(response.location)
    assert b"All conflicts are resolved" in page.data
    assert auth_client.post(response.location + "/merge/complete").status_code == 302

    parents = _git(repository, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3
    assert subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True, timeout=20,
    ).stdout.strip() == remote_head
    assert not _git(repository, "rev-parse", "--verify", "MERGE_HEAD", check=False).stdout.strip()


def test_truncated_merge_diff_requires_explicit_acknowledgement(
        auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository)
    _configure_identity(repository)
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

    page = auth_client.get(response.location).data
    assert b"Merge diff truncated at 200,000 characters" in page
    assert b'name="acknowledge_truncated"' in page
    assert b"required" in page
    result = auth_client.post(response.location + "/merge/complete")
    assert b"explicitly acknowledge" in auth_client.get(result.location).data
    assert _git(repository, "rev-parse", "--verify", "MERGE_HEAD", check=False).stdout.strip()

    result = auth_client.post(response.location + "/merge/complete", data={
        "acknowledge_truncated": "yes",
    })
    assert b"Created merge commit" in auth_client.get(result.location).data
    assert not _git(repository, "rev-parse", "--verify", "MERGE_HEAD", check=False).stdout.strip()


def test_abort_merge_preserves_local_state_and_refuses_untracked_files(
        auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    (repository / "safe.txt").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "safe.txt")
    _git(repository, "commit", "-m", "Add clean file")
    _git(repository, "push", "origin", "main")
    response = _connect(auth_client, repository)
    _configure_identity(repository)
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
    _configure_identity(repository)
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
    assert b"not a currently conflicted file" in auth_client.get(result.location).data
    assert _git(repository, "status", "--porcelain").stdout.startswith("UU readme.md")


def test_conflict_resolution_can_choose_a_deleted_side(auth_client, app, repository, tmp_path, enabled):
    remote = _setup_bare_upstream(repository, tmp_path)
    response = _connect(auth_client, repository)
    _configure_identity(repository)
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


def test_remote_credentials_are_not_displayed(auth_client, app, repository, enabled):
    _git(repository, "remote", "add", "origin", "https://user:secret@example.com/team/wiki.git?token=secret")
    response = _connect(auth_client, repository)
    page = auth_client.get(response.location)
    assert b"secret" not in page.data
    assert b"https://example.com/team/wiki.git" in page.data
    assert page.data.count(b"Git uses credentials configured") == 1


def test_unborn_repository(auth_client, app, tmp_path, enabled):
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "first.txt").write_text("first\n", encoding="utf-8")
    _git(tmp_path, "add", "first.txt")
    response = _connect(auth_client, tmp_path)
    assert b"+first" in auth_client.get(response.location).data


def test_module_migration_roundtrip(tmp_path):
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = importlib.import_module(
        "docindex_module_gitsync.migrations.versions.gitsync_0001_repositories")
    engine = sa.create_engine("sqlite:///" + str(tmp_path / "migration.sqlite"))
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text("CREATE TABLE drives (id INTEGER PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert "mod_gitsync_repository" in sa.inspect(connection).get_table_names()
            migration.downgrade()
            assert "mod_gitsync_repository" not in sa.inspect(connection).get_table_names()
            migration.upgrade()
            columns = {item["name"] for item in sa.inspect(connection).get_columns("mod_gitsync_repository")}
            assert columns == {column.name for column in _module().models.Repository.__table__.columns}
    engine.dispose()