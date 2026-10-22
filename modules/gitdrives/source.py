"""Git-backed drives (gitdrives module) — Technologies used: dulwich.

Pure-Python Git (no git binary needed). Mirror only: clone/pull a remote
repository into a local cache and let the regular folder scan index it.
Drives are read-only; the remote is never written to.
"""

import os
import shutil
import time

from dulwich import porcelain


def remove_tree(path, tries=5):
    """Best-effort recursive delete, tolerant of Windows file locks held
    by not-yet-collected dulwich handles."""
    import gc
    gc.collect()
    for _ in range(tries):
        try:
            if not os.path.exists(path):
                return
            shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.2)
    shutil.rmtree(path, ignore_errors=True)


def _credentials(drive):
    """(username, password) for remote HTTP(S) URLs, else (None, None)."""
    from app.services import ai_service

    url = (drive.repo_url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return None, None
    username = (drive.repo_username or "").strip() or None
    token = ai_service.decrypt_api_key(drive.repo_token or "")
    if not token:
        return None, None
    return username, token


def _auth_kwargs(drive):
    username, password = _credentials(drive)
    if password is None:
        return {}
    return {"username": username, "password": password}


def cache_path(drive_id):
    """Local checkout dir for a git drive (outside any synced root)."""
    from flask import current_app
    return os.path.join(current_app.config["GIT_REPOS_FOLDER"],
                        str(int(drive_id)))


def ls_remote(url, username=None, password=None):
    """Refs advertised by the remote (validates URL + credentials)."""
    from dulwich.client import get_transport_and_path

    kwargs = {}
    if password:
        kwargs = {"username": username, "password": password}
    client, path = get_transport_and_path(url, **kwargs)
    try:
        return client.get_refs(path)
    finally:
        try:
            client.close()
        except Exception:
            pass


def clone_repo(url, target, branch=None, username=None, password=None):
    """Clone (shallow) and check out branch. Returns error str or None."""
    kwargs = {}
    if password:
        kwargs = {"username": username, "password": password}
    try:
        porcelain.clone(url, target, depth=1,
                        branch=branch or None, **kwargs)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        remove_tree(target)
        return str(exc)[:500]
    return None


def pull_repo(path, username=None, password=None):
    """Pull remote changes into an existing checkout. Error str or None."""
    kwargs = {}
    if password:
        kwargs = {"username": username, "password": password}
    try:
        porcelain.pull(path, **kwargs)
    except Exception as exc:  # noqa: BLE001 - surfaced as sync error
        return str(exc)[:500]
    return None


class GitDriveSource:
    """Drive source provider for git-backed mirror drives."""

    kind = "git"

    def validate(self, user, config):
        url = (config.get("repo_url") or "").strip()
        if not url:
            return "A repository URL is required."
        branch = (config.get("branch") or "").strip() or None
        try:
            refs = ls_remote(url, config.get("username"),
                              config.get("token"))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            return f"Could not reach the repository: {exc}"[:500]
        if branch:
            want = b"refs/heads/" + branch.encode()
            if want not in refs:
                return f"Branch '{branch}' not found on the remote."
        return None

    def materialize(self, drive):
        target = cache_path(drive.id)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(os.path.join(target, ".git")):
            # A checkout dir may be stale (e.g. a reused id): only trust
            # it when it points at the same remote.
            if self._origin_url(target) == (drive.repo_url or "").strip():
                return None
            shutil.rmtree(target, ignore_errors=True)
        return clone_repo(drive.repo_url, target,
                          branch=(drive.repo_branch or None),
                          **_auth_kwargs(drive))

    @staticmethod
    def _origin_url(path):
        try:
            from dulwich.repo import Repo
            with Repo(path) as repo:
                url = repo.get_config().get((b"remote", b"origin"), b"url")
                return url.decode().strip() if url else None
        except Exception:
            return None

    def pre_sync(self, drive):
        target = cache_path(drive.id)
        if not os.path.exists(os.path.join(target, ".git")):
            error = self.materialize(drive)
            if error:
                return error
        return pull_repo(target, **_auth_kwargs(drive))

    def cleanup(self, drive):
        remove_tree(cache_path(drive.id))
