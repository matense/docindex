import importlib
import subprocess
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import ModuleState, StoredFile, User


def _git(path, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Test User",
         "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", *args],
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


def test_gitsync_requires_login(client):
    assert client.get("/m/gitsync/").status_code == 302


def test_gitsync_disabled(auth_client):
    assert auth_client.get("/m/gitsync/").status_code == 404


def test_gitsync_enabled(auth_client, app):
    with app.app_context():
        db.session.add(ModuleState(name="gitsync", enabled=True))
        db.session.commit()
    response = auth_client.get("/m/gitsync/")
    assert response.status_code == 200
    assert b"Git Sync" in response.data


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
    assert not _git(repository, "config", "--local", "--get", "credential.username", check=False).stdout
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == initial


def test_status_diff_and_initial_baseline(auth_client, app, repository, enabled):
    response = _connect(auth_client, repository)
    (repository / "readme.md").write_text("changed content\n", encoding="utf-8")
    (repository / "new file.txt").write_text("new\n", encoding="utf-8")
    page = auth_client.get(response.location)
    assert b"+changed content" in page.data
    assert b"new file.txt" in page.data
    _git(repository, "add", "readme.md")
    _git(repository, "commit", "-m", "External change")
    assert b"+changed content" not in auth_client.get(response.location).data
    assert b"+changed content" in auth_client.get(response.location + "?basis=initial").data


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


def test_accounts_list_uses_gcm_list_only(auth_client, app, enabled):
    result = subprocess.CompletedProcess([], 0, "AnderPT\nother-account\n", "")
    with patch.object(_module().service.subprocess, "run", return_value=result) as run:
        response = auth_client.get("/m/gitsync/")
    assert b"AnderPT" in response.data
    assert run.call_args.args[0] == ["git", "credential-manager", "github", "list"]


def test_remote_credentials_are_not_displayed(auth_client, app, repository, enabled):
    _git(repository, "remote", "add", "origin", "https://user:secret@example.com/team/wiki.git?token=secret")
    response = _connect(auth_client, repository)
    page = auth_client.get(response.location)
    assert b"secret" not in page.data
    assert b"https://example.com/team/wiki.git" in page.data


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