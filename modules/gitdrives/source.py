"""Git-backed drives (gitdrives module) — Technologies used: dulwich.

Pure-Python Git (no git binary needed). Mirror only: clone/pull a remote
repository into a local cache and let the regular folder scan index it.
Drives are read-only; the remote is never written to.
"""

import os
import re
import shutil
import time

from dulwich import porcelain

# First line of a Git LFS pointer file (the real blob lives on the LFS
# server; what we see is ~130 bytes of text).
LFS_POINTER_RE = re.compile(
    rb"^version https://git-lfs\.github\.com/spec/v1\r?\n"
    rb"oid sha256:[0-9a-f]{64}\r?\nsize \d+\r?\n?$")

# Walk caps so huge checkouts can't stall page loads.
_LFS_MAX_FILES = 3000
_LFS_MAX_BYTES = 2048
_LFS_MAX_HITS = 50

# Progress lines look like "receiving objects:  45% (123/274), 1.2 MiB".
_PROGRESS_PCT = re.compile(r"(\d+)%")
_PROGRESS_COUNT = re.compile(r"(\d+)\s*/\s*(\d+)")


def parse_progress_line(line):
    """Split a dulwich progress line into (percent|None, text).

    Prefers the explicit N% figure; falls back to an N/M count scaled to
    100. Returns (None, text) when neither is present (phase text only).
    """
    text = (line or "").strip()
    m = _PROGRESS_PCT.search(text)
    if m:
        pct = max(0, min(100, int(m.group(1))))
        return pct, text
    m = _PROGRESS_COUNT.search(text)
    if m:
        have, total = int(m.group(1)), int(m.group(2))
        if total > 0:
            return max(0, min(100, round(100 * have / total))), text
    return None, text


class _ProgressCapture:
    """Binary errstream sink that forwards dulwich's carriage-return
    progress lines to an on_progress(percent|None, text) callback."""

    def __init__(self, on_progress):
        self._text = ""
        self._last = None
        self._on_progress = on_progress

    def write(self, data):
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8", "replace")
        self._text += str(data)
        chunk = self._text.replace("\r", "\n").split("\n")
        line = next((m.strip() for m in reversed(chunk) if m.strip()), "")
        if line and line != self._last:
            self._last = line
            try:
                percent, _text = parse_progress_line(line)
                self._on_progress(percent, line.strip())
            except Exception:
                pass
        return len(data)

    def flush(self):
        pass

    def isatty(self):
        return False


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


def find_lfs_pointers(worktree):
    """Files that are LFS pointers, not real content.

    LFS is NOT fetched (see module help): these files index as pointer
    text. Returns (total_count, [sample relpaths]). Capped for speed.
    """
    hits, checked, total = [], 0, 0
    for dirpath, dirnames, filenames in os.walk(worktree):
        if ".git" in dirnames:
            dirnames.remove(".git")
        # Keep the walk stable and cheap on huge checkouts.
        dirnames.sort()
        for filename in sorted(filenames):
            if checked >= _LFS_MAX_FILES:
                return total, hits
            checked += 1
            path = os.path.join(dirpath, filename)
            try:
                if os.path.getsize(path) > _LFS_MAX_BYTES:
                    continue
                with open(path, "rb") as fh:
                    head = fh.read(_LFS_MAX_BYTES)
            except OSError:
                continue
            if LFS_POINTER_RE.match(head):
                total += 1
                if len(hits) < _LFS_MAX_HITS:
                    hits.append(os.path.relpath(path, worktree))
    return total, hits


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


def _neuter_lfs_filters(path):
    """Disable LFS smudge/clean in a local checkout.

    Without this, dulwich shells out to git-lfs on any LFS-tracked file,
    which hangs or fails when there is no LFS server and breaks clone/pull
    entirely. With a bogus process command the driver falls back to
    passthrough, so worktree files stay exactly as committed (real content
    or pointer text).
    """
    from dulwich.config import ConfigFile
    cfg_path = os.path.join(path, ".git", "config")
    cfg = (ConfigFile.from_path(cfg_path) if os.path.exists(cfg_path)
           else ConfigFile())
    cfg.set((b"filter", b"lfs"), b"process", b"docindex-no-lfs")
    cfg.set((b"filter", b"lfs"), b"required", b"false")
    cfg.write_to_path(cfg_path)


def _current_branch(path):
    """Local branch HEAD points at (b"refs/heads/..." or None)."""
    from dulwich.repo import Repo
    with Repo(path) as repo:
        head = repo.refs.read_ref(b"HEAD")
        if head and head.startswith(b"ref: "):
            return head[len(b"ref: "):]
    return None


def _checkout_head_raw(path, on_progress=None):
    """Write the worktree from HEAD blobs directly (no filters).

    Dulwich would otherwise shell out to git-lfs for LFS-tracked files,
    which hangs or fails without an LFS server. Raw checkout keeps files
    exactly as committed (real content or pointer text). Stale files and
    empty dirs are removed, so the worktree always mirrors HEAD.
    on_progress(done, total) is called every 200 files.
    """
    from dulwich.object_store import iter_tree_contents
    from dulwich.repo import Repo
    with Repo(path) as repo:
        commit = repo[repo.refs[b"HEAD"]]
        entries = list(iter_tree_contents(repo.object_store, commit.tree))
        total = len(entries)
        if on_progress is not None:
            try:
                on_progress(0, total)
            except Exception:
                pass
        written = set()
        for i, entry in enumerate(entries):
            rel = os.path.normpath(entry.path.decode())
            if rel.startswith(".."):
                continue
            full = os.path.join(path, rel)
            os.makedirs(os.path.dirname(full) or path, exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(repo[entry.sha].data)
            written.add(rel)
            if on_progress is not None and (i + 1) % 200 == 0:
                try:
                    on_progress(i + 1, total)
                except Exception:
                    pass
    for dirpath, dirnames, filenames in os.walk(path, topdown=True):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.normpath(os.path.relpath(full, path)) not in written:
                try:
                    os.remove(full)
                except OSError:
                    pass
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        if ".git" in dirpath.split(os.sep):
            continue
        if not dirnames and not filenames and dirpath != path:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    if on_progress is not None:
        try:
            on_progress(total, total)
        except Exception:
            pass
    return written


def _capture(label, on_progress):
    """Wrap an on_progress callback with a phase label.

    Dulwich calls it as on_progress(percent|None, text); the wrapper
    prefixes the phase name unless the text already says it.
    """
    if on_progress is None:
        return None

    def _cb(percent, text):
        text = text or ""
        if label.lower() not in text.lower():
            text = f"{label}: {text}" if text else label
        try:
            on_progress(percent, text)
        except Exception:
            pass

    return _cb


def clone_repo(url, target, branch=None, username=None, password=None,
               on_progress=None):
    """Clone (shallow) and check out a branch. Returns error str or None."""
    kwargs = {}
    if password:
        kwargs = {"username": username, "password": password}
    capture = _ProgressCapture(_capture("Cloning", on_progress)) if on_progress else None
    try:
        porcelain.clone(url, target, depth=1,
                        branch=branch or None, checkout=False,
                        errstream=capture, **kwargs)
        _neuter_lfs_filters(target)

        def _checkout_progress(done, total):
            if on_progress is None:
                return
            pct = round(100 * done / total) if total else None
            on_progress(pct, f"Checking out {done}/{total} files")

        _checkout_head_raw(target, on_progress=_checkout_progress)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        remove_tree(target)
        return str(exc)[:500]
    return None


def pull_repo(path, url=None, branch=None, username=None, password=None,
              on_progress=None):
    """Fetch remote changes, fast-forward the local branch, then
    re-materialize the worktree without filters. Error str or None."""
    from dulwich.repo import Repo
    _neuter_lfs_filters(path)  # also repairs checkouts made before this
    kwargs = {}
    if password:
        kwargs = {"username": username, "password": password}
    try:
        capture = _ProgressCapture(_capture("Pulling", on_progress)) if on_progress else None
        if url:
            result = porcelain.fetch(path, url, depth=1,
                                     errstream=capture, **kwargs)
            br = branch.encode() if isinstance(branch, str) else branch
            if not br:
                current = _current_branch(path)
                br = (current.split(b"/")[-1] if current
                      else b"master")
                want = b"refs/heads/" + br
            else:
                want = br if br.startswith(b"refs/") else b"refs/heads/" + br
            if want not in result.refs:
                return f"Branch '{want.decode()}' not found on the remote."[:200]
            with Repo(path) as repo:
                repo.refs[want] = result.refs[want]
        else:
            porcelain.pull(path, errstream=capture, **kwargs)

        def _checkout_progress(done, total):
            if on_progress is None:
                return
            pct = round(100 * done / total) if total else None
            on_progress(pct, f"Checking out {done}/{total} files")

        _checkout_head_raw(path, on_progress=_checkout_progress)
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

    def materialize(self, drive, on_progress=None):
        target = cache_path(drive.id)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(os.path.join(target, ".git")):
            # A checkout dir may be stale (e.g. a reused id): only trust
            # it when it points at the same remote.
            if self._origin_url(target) == (drive.repo_url or "").strip():
                return None
            remove_tree(target)
        return clone_repo(drive.repo_url, target,
                          branch=(drive.repo_branch or None),
                          on_progress=on_progress,
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

    def pre_sync(self, drive, on_progress=None):
        target = cache_path(drive.id)
        if not os.path.exists(os.path.join(target, ".git")):
            error = self.materialize(drive, on_progress=on_progress)
            if error:
                return error
        return pull_repo(target, url=drive.repo_url,
                         branch=drive.repo_branch, on_progress=on_progress,
                         **_auth_kwargs(drive))

    def cleanup(self, drive):
        remove_tree(cache_path(drive.id))
