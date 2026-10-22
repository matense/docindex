"""Git drives module: clone/pull a remote repo as a read-only mirror drive."""

import os

import pytest

from app.extensions import db
from app.models import Drive, ModuleState, StoredFile
from app.services import drive_service


@pytest.fixture()
def gitrepo(tmp_path, app):
    """A real local git repo with one committed file; returns its path."""
    import importlib

    from dulwich import porcelain

    # The clone cache (instance/test_git_repos/<id>) survives across tests
    # while drive ids restart at 1 — wipe it for isolation (with retries:
    # dulwich handles may still be open on Windows).
    from flask import current_app
    with app.app_context():
        git_source = importlib.import_module(
            "docindex_module_gitdrives.source")
        git_source.remove_tree(current_app.config["GIT_REPOS_FOLDER"])
    repo = str(tmp_path / "origin")
    os.makedirs(repo)
    porcelain.init(repo)
    with open(os.path.join(repo, "readme.md"), "w",
              encoding="utf-8") as fh:
        fh.write("hello git drive")
    porcelain.add(repo, paths=["readme.md"])
    porcelain.commit(repo, message=b"first",
                     author=b"T <t@t>", committer=b"T <t@t>")
    return repo


@pytest.fixture()
def enabled(app):
    with app.app_context():
        db.session.add(ModuleState(name="gitdrives", enabled=True))
        db.session.commit()


def _create(client, path, **extra):
    data = {"repo_url": path}
    data.update(extra)
    return client.post("/m/gitdrives/create", data=data,
                       follow_redirects=True)


def test_module_disabled_404s(auth_client):
    assert auth_client.get("/m/gitdrives/").status_code == 404


def test_clone_creates_mirror_drive(auth_client, app, gitrepo, enabled):
    resp = _create(auth_client, gitrepo)
    assert resp.status_code == 200
    with app.app_context():
        drive = Drive.query.filter_by(
            user_id=1, source_kind="git").one()
        assert drive.is_synced and drive.is_readonly
        assert drive.repo_url == gitrepo
        assert os.path.isdir(os.path.join(drive.source_path, ".git"))
        stored = StoredFile.query.filter_by(drive_id=drive.id).one()
        assert stored.name == "readme.md"
        assert stored.source_path.startswith(drive.source_path)
        assert open(stored.source_path, encoding="utf-8").read() == \
            "hello git drive"


def test_pull_picks_up_new_commits(auth_client, app, gitrepo, enabled):
    from dulwich import porcelain

    _create(auth_client, gitrepo)
    with app.app_context():
        drive = Drive.query.filter_by(source_kind="git").one()
        did = drive.id
    with open(os.path.join(gitrepo, "second.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("second file")
    porcelain.add(gitrepo, paths=["second.txt"])
    porcelain.commit(gitrepo, message=b"second",
                     author=b"T <t@t>", committer=b"T <t@t>")
    resp = auth_client.post(f"/drives/{did}/sync",
                            follow_redirects=True)
    assert b"Sync complete" in resp.data
    with app.app_context():
        names = {f.name for f in StoredFile.query.filter_by(drive_id=did)}
        assert names == {"readme.md", "second.txt"}


def test_git_drive_is_read_only(auth_client, app, gitrepo, enabled):
    _create(auth_client, gitrepo)
    with app.app_context():
        fid = StoredFile.query.filter(
            StoredFile.drive_id.isnot(None)).first().id
    assert auth_client.post(f"/file/{fid}/edit",
                            data={"content": "x"}).status_code == 400
    assert auth_client.post(f"/file/{fid}/delete").status_code == 400


def test_invalid_repo_rejected(auth_client, app, enabled):
    resp = _create(auth_client, "/does/not/exist-at-all")
    assert resp.status_code == 200
    with app.app_context():
        assert Drive.query.filter_by(source_kind="git").count() == 0


def test_token_stored_encrypted(auth_client, app, gitrepo, enabled):
    from app.services import ai_service

    _create(auth_client, gitrepo, username="any",
            token="secret-pat-123")
    with app.app_context():
        drive = Drive.query.filter_by(source_kind="git").one()
        assert drive.repo_token.startswith("enc:")
        assert ai_service.decrypt_api_key(drive.repo_token) == \
            "secret-pat-123"


def test_remove_drive_clears_clone_cache(auth_client, app, gitrepo, enabled):
    _create(auth_client, gitrepo)
    with app.app_context():
        drive = Drive.query.filter_by(source_kind="git").one()
        did, cache = drive.id, drive.source_path
        assert os.path.isdir(cache)
    auth_client.post("/settings/profile")  # session drive sanity
    resp = auth_client.post(f"/drives/{did}/remove")
    # No follow_redirects: visiting the index would lazily recreate a
    # "Personal" drive reusing id 1 and hide the assertion below.
    assert resp.status_code == 302
    with app.app_context():
        assert db.session.get(Drive, did) is None
    assert not os.path.exists(cache)


def test_git_drive_defaults_captions_off(auth_client, app, gitrepo, enabled):
    _create(auth_client, gitrepo)
    with app.app_context():
        from app.models import Drive
        drive = Drive.query.filter_by(source_kind="git").one()
        assert drive.captions_enabled is False


def test_provider_registered(app):
    assert drive_service.get_drive_source("git") is not None
    assert drive_service.get_drive_source("folder") is None
    assert drive_service.get_drive_source("nope") is None


def test_no_dock_entry_but_dropdown_link_when_enabled(auth_client, app,
                                                     enabled):
    html = auth_client.get("/").data.decode()
    # No dedicated top menu: the module is reached from Profile/drives.
    assert 'href="/m/gitdrives/"' not in html or \
        html.count('href="/m/gitdrives/"') == 1
    # Drive selector offers the clone entry point.
    assert "Clone a Git repository" in html
    html = auth_client.get("/settings/profile").data.decode()
    assert "Clone a Git repo" in html


def test_no_clone_entry_when_disabled(auth_client):
    html = auth_client.get("/").data.decode()
    assert "Clone a Git repository" not in html
    html = auth_client.get("/settings/profile").data.decode()
    assert "Clone a Git repo" not in html


POINTER = (b"version https://git-lfs.github.com/spec/v1\n"
           b"oid sha256:" + b"4" * 64 + b"\nsize 12345\n")


def test_lfs_pointers_warn_but_do_not_block(auth_client, app, gitrepo,
                                            enabled):
    from dulwich import porcelain

    with open(os.path.join(gitrepo, "notes.txt"), "wb") as fh:
        fh.write(POINTER)
    porcelain.add(gitrepo, paths=["notes.txt"])
    porcelain.commit(gitrepo, message=b"lfs",
                     author=b"T <t@t>", committer=b"T <t@t>")
    resp = _create(auth_client, gitrepo)
    assert resp.status_code == 200
    html = auth_client.get("/m/gitdrives/").data.decode()
    assert "LFS not fetched" in html
    with app.app_context():
        from app.models import StoredFile
        stored = StoredFile.query.filter_by(name="notes.txt").one()
        with open(stored.source_path, encoding="utf-8") as fh:
            assert "git-lfs.github.com" in fh.read()
