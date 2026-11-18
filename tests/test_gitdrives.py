"""Git drives module: clone/pull a remote repo as a read-only mirror drive."""

import os
import socket as _socket
import sys as _sys

import pytest

from app.extensions import db
from app.models import Drive, ModuleState, StoredFile, User
from app.services import drive_service


@pytest.fixture()
def gitrepo(tmp_path, app, monkeypatch):
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
    # Local clone paths are deny-by-default (S1 lockdown): the test admin
    # allowlists the fixture repo, the same way a deployment allowlists
    # pre-staged mirrors via GITDRIVES_ALLOWED_HOSTS.
    monkeypatch.setenv("GITDRIVES_ALLOWED_HOSTS", repo)
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


def test_pull_picks_up_new_commits(auth_client, app, user, gitrepo, enabled):
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
    # POST /drives/<id>/sync is admin-only: promote the owner so pull runs.
    with app.app_context():
        db.session.get(User, user).is_admin = True
        db.session.commit()
    resp = auth_client.post(f"/drives/{did}/sync",
                            follow_redirects=True)
    assert b"Sync complete" in resp.data
    with app.app_context():
        names = {f.name for f in StoredFile.query.filter_by(drive_id=did)}
        assert names == {"readme.md", "second.txt"}


def test_gitdrive_sync_now_refuses_non_admin(auth_client, app, user,
                                             monkeypatch):
    """POST /drives/<id>/sync is admin-only: a non-admin owner gets 403
    and no sync is started (plain refusal pin for gitdrive rows)."""
    from types import SimpleNamespace

    from app.services import sync_service

    calls = []

    def fake_start_sync(drive, app=None):
        calls.append(drive.id)
        return SimpleNamespace(state="done", stats={"added": 0, "updated": 0,
                                                    "removed": 0,
                                                    "skipped": 0})

    monkeypatch.setattr(sync_service, "start_sync", fake_start_sync)
    with app.app_context():
        db.session.get(User, user).is_admin = False
        db.session.commit()
        drive = Drive(name="Git", user_id=user, source_kind="git",
                      repo_url="https://github.com/o/r.git",
                      source_path="/tmp/gitdrive-test-cache")
        db.session.add(drive)
        db.session.commit()
        did = drive.id
    resp = auth_client.post(f"/drives/{did}/sync")
    assert resp.status_code == 403
    assert calls == []  # the route's only sync path is start_sync


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


def test_parse_progress_line():
    from modules.gitdrives import source as gs
    assert gs.parse_progress_line(
        "receiving objects:  45% (123/274), 1.2 MiB | 500 KiB/s") == (
            45, "receiving objects:  45% (123/274), 1.2 MiB | 500 KiB/s")
    assert gs.parse_progress_line(
        "resolving deltas: 3/10 done") == (
            30, "resolving deltas: 3/10 done")
    assert gs.parse_progress_line("counting objects: 5, done") == (
        None, "counting objects: 5, done")
    assert gs.parse_progress_line("") == (None, "")


def test_clone_reports_progress(auth_client, app, tmp_path, enabled):
    from dulwich import porcelain

    from modules.gitdrives import source as gs

    seen = []
    repo = str(tmp_path / "big")
    os.makedirs(repo)
    porcelain.init(repo)
    for i in range(3):
        with open(os.path.join(repo, f"f{i}.txt"), "w") as fh:
            fh.write("x" * 1000)
    porcelain.add(repo, paths=[f"f{i}.txt" for i in range(3)])
    porcelain.commit(repo, message=b"big", author=b"T <t@t>",
                     committer=b"T <t@t>")
    with app.app_context():
        from app.extensions import db
        from app.models import Drive
        drive = Drive(name="P", user_id=1, source_kind="git",
                      repo_url=repo)
        db.session.add(drive)
        db.session.commit()
        target = gs.cache_path(drive.id)
        gs.remove_tree(target)  # isolation: ids restart per test
    err = gs.clone_repo(repo, target,
                        on_progress=lambda p, t: seen.append((p, t)))
    assert err is None
    assert seen, "expected progress callbacks during clone"
    assert any(p == 100 for p, _ in seen)


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


def test_clone_validation_error_pops_modal_with_reason(auth_client, app,
                                                      enabled):
    """A clone that fails outright must be unmissable: the redirected
    index page pops a modal carrying the reason (not just a toast)."""
    resp = auth_client.post(
        "/m/gitdrives/create",
        data={"repo_url": "/nonexistent/path/no-repo-here"},
        follow_redirects=True)
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Could not clone" in html
    assert "window.uiAlert" in html
    # One-shot: a second visit shows no modal.
    html2 = auth_client.get("/m/gitdrives/").data.decode()
    assert "Could not clone" not in html2


def test_no_clone_entry_when_disabled(auth_client):
    html = auth_client.get("/").data.decode()
    assert "Clone a Git repository" not in html
    html = auth_client.get("/settings/profile").data.decode()
    assert "Clone a Git repo" not in html


def test_git_sync_link_only_when_enabled(auth_client, app, enabled):
    html = auth_client.get("/m/gitdrives/").data.decode()
    assert 'href="/m/gitsync/"' not in html
    with app.app_context():
        db.session.add(ModuleState(name="gitsync", enabled=True))
        db.session.commit()
    html = auth_client.get("/m/gitdrives/").data.decode()
    assert html.count('href="/m/gitsync/"') == 1


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


# ---------------------------------------------------------------------------
# S1 (argon P0): clone-URL scheme + host lockdown, POST-only check endpoint.
#
# Policy under test: `https://` is always allowed provided the host
# resolves exclusively to public IPs; every other scheme/host is refused
# unless explicitly listed in the GITDRIVES_ALLOWED_HOSTS admin allowlist
# (hosts/CIDR, default empty = deny). Local filesystem paths to real repos
# keep working (equivalent to folder sync). Refusals happen BEFORE
# ls_remote, so a rejected URL never triggers a network connection.
# ---------------------------------------------------------------------------


def _s1_provider():
    from app.services import drive_service
    provider = drive_service.get_drive_source("git")
    assert provider is not None
    return provider


def _s1_user(app, uid):
    from app.models import User
    with app.app_context():
        return db.session.get(User, uid)


def _s1_no_fetch(monkeypatch, provider):
    """Stub out ls_remote (as success) in the provider's own module
    namespace and return the call log. Modules load under a
    docindex_module_* alias, so patching must target the registered
    provider's module object, not `modules.gitdrives.source`."""
    calls = []
    mod = _sys.modules[type(provider).__module__]

    def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(mod, "ls_remote", _fake)
    return calls


def _s1_addrs(*ips):
    infos = []
    for ip in ips:
        if ":" in ip:
            infos.append((_socket.AF_INET6, _socket.SOCK_STREAM, 6, "",
                          (ip, 443, 0, 0)))
        else:
            infos.append((_socket.AF_INET, _socket.SOCK_STREAM, 6, "",
                          (ip, 443)))
    return infos


def test_file_and_remote_urls_rejected_without_fetch(app, user, monkeypatch):
    provider = _s1_provider()
    calls = _s1_no_fetch(monkeypatch, provider)
    u = _s1_user(app, user)
    for url in ["file:///etc/passwd",
                "ssh://git@internal/x.git",
                "http://169.254.169.254/",
                "http://127.0.0.1:8080/x.git",
                "https://127.0.0.1/x.git"]:
        assert provider.validate(u, {"repo_url": url}), url
    assert calls == []


def test_nonexistent_local_paths_rejected(app, user, monkeypatch):
    provider = _s1_provider()
    calls = _s1_no_fetch(monkeypatch, provider)
    u = _s1_user(app, user)
    for url in ["/srv/git/secret.git", "C:/repos/secret.git"]:
        assert provider.validate(u, {"repo_url": url}), url
    assert calls == []


def test_https_private_host_rejected(app, user, monkeypatch):
    provider = _s1_provider()
    calls = _s1_no_fetch(monkeypatch, provider)
    u = _s1_user(app, user)
    for ips in [["10.0.0.5"], ["127.0.0.1"], ["::ffff:127.0.0.1"]]:
        monkeypatch.setattr(_socket, "getaddrinfo",
                            lambda *a, _ips=ips, **k: _s1_addrs(*_ips))
        assert provider.validate(
            u, {"repo_url": "https://git.example.com/o/r.git"}), ips
    assert calls == []


def test_https_public_host_allowed(app, user, monkeypatch):
    provider = _s1_provider()
    _s1_no_fetch(monkeypatch, provider)
    monkeypatch.setattr(_socket, "getaddrinfo",
                        lambda *a, **k: _s1_addrs("93.184.216.34"))
    u = _s1_user(app, user)
    assert provider.validate(
        u, {"repo_url": "https://github.com/o/r.git"}) is None


def test_allowlisted_host_permitted_without_dns(app, user, monkeypatch):
    provider = _s1_provider()
    _s1_no_fetch(monkeypatch, provider)
    monkeypatch.setenv("GITDRIVES_ALLOWED_HOSTS",
                       "git.corp.internal, 10.0.0.0/8")

    def _no_dns(*args, **kwargs):
        raise AssertionError("allowlisted hosts must skip the DNS gate")

    monkeypatch.setattr(_socket, "getaddrinfo", _no_dns)
    u = _s1_user(app, user)
    assert provider.validate(
        u, {"repo_url": "http://git.corp.internal/x.git"}) is None
    assert provider.validate(
        u, {"repo_url": "ssh://git@git.corp.internal/x.git"}) is None
    assert provider.validate(
        u, {"repo_url": "http://10.1.2.3/x.git"}) is None


def test_check_endpoint_is_post_only(auth_client, app, enabled, monkeypatch):
    resp = auth_client.get(
        "/m/gitdrives/api/check?repo_url=https://github.com/o/r.git")
    assert resp.status_code == 405
    # POST without a URL is a validation failure (200 + ok:false),
    # proving the POST route is live.
    resp = auth_client.post("/m/gitdrives/api/check", data={"repo_url": ""})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False
    # POST with an allowlisted-path-free public URL validates cleanly.
    provider = _s1_provider()
    mod = _sys.modules[type(provider).__module__]
    monkeypatch.setattr(mod, "ls_remote", lambda *a, **k: {})
    monkeypatch.setattr(_socket, "getaddrinfo",
                        lambda *a, **k: _s1_addrs("93.184.216.34"))
    resp = auth_client.post("/m/gitdrives/api/check",
                            data={"repo_url": "https://github.com/o/r.git"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_existing_local_path_denied_without_allowlist(app, user, monkeypatch,
                                                      tmp_path):
    """Fix round finding 1: a REAL existing directory must still be
    refused — the old os.path.exists bypass is gone. Local paths are
    deny-by-default like any other non-https URL."""
    provider = _s1_provider()
    calls = _s1_no_fetch(monkeypatch, provider)
    target = tmp_path / "existing"
    target.mkdir()
    u = _s1_user(app, user)
    err = provider.validate(u, {"repo_url": str(target)})
    assert err and "GITDRIVES_ALLOWED_HOSTS" in err
    assert calls == []


def test_allowlisted_local_path_permitted(app, user, monkeypatch, tmp_path):
    """Fix round finding 1: an explicit allowlisted local path (prefix)
    passes the policy and reaches ls_remote."""
    provider = _s1_provider()
    calls = _s1_no_fetch(monkeypatch, provider)
    monkeypatch.setenv("GITDRIVES_ALLOWED_HOSTS", str(tmp_path))
    u = _s1_user(app, user)
    assert provider.validate(
        u, {"repo_url": str(tmp_path / "repo.git")}) is None
    assert len(calls) == 1


def test_sync_path_enforces_url_policy(app, user, monkeypatch):
    """Fix round finding 2: a pre-existing non-https row (created before
    the lockdown) is refused on sync — clone/pull must never run."""
    provider = _s1_provider()
    mod = _sys.modules[type(provider).__module__]
    attempted = []

    def _boom(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("sync must not fetch a policy-rejected URL")

    monkeypatch.setattr(mod, "clone_repo", _boom)
    monkeypatch.setattr(mod, "pull_repo", _boom)
    with app.app_context():
        drive = Drive(name="Old", user_id=user, source_kind="git",
                      repo_url="ssh://git@internal/x.git")
        db.session.add(drive)
        db.session.commit()
        err = provider.pre_sync(drive)
        assert err and "GITDRIVES_ALLOWED_HOSTS" in err
        err = provider.materialize(drive)
        assert err and "GITDRIVES_ALLOWED_HOSTS" in err
    assert attempted == []


# --- S4: checkout must not follow crafted tree paths or symlink modes -------


def test_checkout_head_raw_never_writes_outside_root(tmp_path):
    """Crafted tree entries (.., absolute drive-rooted, symlink mode) must
    not escape the checkout root or materialize as links (S4)."""
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo

    from modules.gitdrives.source import _checkout_head_raw

    root = tmp_path / "root"
    root.mkdir()
    abs_target = tmp_path / "evil-abs.txt"
    abs_entry = str(abs_target).replace(os.sep, "/")

    repo = Repo.init(str(root))
    try:
        store = repo.object_store

        def _blob(data):
            blob = Blob.from_string(data)
            store.add_object(blob)
            return blob.id

        tree = Tree()
        tree.add(b"ok.txt", 0o100644, _blob(b"ok"))
        tree.add(b"../evil-rel.txt", 0o100644, _blob(b"escape"))
        tree.add(abs_entry.encode("utf-8"), 0o100644, _blob(b"escape"))
        tree.add(b"link.txt", 0o120000, _blob(b"ok.txt"))
        store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        commit.author = commit.committer = b"T <t@t>"
        commit.author_time = commit.commit_time = 0
        commit.author_timezone = commit.commit_timezone = 0
        commit.message = b"evil"
        commit.encoding = b"UTF-8"
        store.add_object(commit)
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/master")
        repo.refs[b"refs/heads/master"] = commit.id
    finally:
        repo.close()

    written = _checkout_head_raw(str(root))

    assert (root / "ok.txt").read_bytes() == b"ok"
    assert written == {"ok.txt"}
    assert not (tmp_path / "evil-rel.txt").exists()
    assert not abs_target.exists()
    assert not (root / "link.txt").exists()
