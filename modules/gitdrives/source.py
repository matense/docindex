"""Git-backed drives (gitdrives module) — Technologies used: dulwich.

Pure-Python Git (no git binary needed). Mirror only: clone/pull a remote
repository into a local cache and let the regular folder scan index it.
Drives are read-only; the remote is never written to.
"""

import contextlib
import ipaddress
import os
import re
import shutil
import socket
import stat
import time
from urllib.parse import urlsplit

from dulwich import porcelain

# First line of a Git LFS pointer file (the real blob lives on the LFS
# server; what we see is ~130 bytes of text).
LFS_POINTER_RE = re.compile(
    rb"^version https://git-lfs\.github\.com/spec/v1\r?\n"
    rb"oid sha256:[0-9a-f]{64}\r?\nsize \d+\r?\n?$")

# Walk caps so huge checkouts can't stall the sync worker.
_LFS_MAX_FILES = 3000
_LFS_MAX_BYTES = 2048
_LFS_MAX_HITS = 50

# Wall-clock budget (seconds) for a single scan of the tree. Checked once
# per directory and every 200 files; on expiry the partial (total, hits)
# is returned immediately so a huge/slow checkout delays, but never hangs,
# the sync worker. Module-level so tests can monkeypatch it.
_LFS_WALK_BUDGET = 10.0

# Single ceiling (seconds) for every dulwich network call below (validate
# via ls_remote, clone, fetch/pull). Dulwich exposes no timeout knob on
# these paths, but underneath it is all stdlib sockets — raw
# socket.socket + connect for git://, urllib3's create_connection for
# http(s) — both of which honor the process-wide default timeout when no
# explicit timeout is passed (verified: fresh sockets inherit
# getdefaulttimeout(); urllib3's default-timeout sentinel leaves the
# socket timeout unset). So set it narrowly around each network call and
# always restore the previous value.
_NETWORK_TIMEOUT = 60


@contextlib.contextmanager
def _network_timeout(seconds=_NETWORK_TIMEOUT):
    """Bound one dulwich network call by the socket default timeout."""
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


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


def find_lfs_pointers(worktree, drive_id=None):
    """Files that are LFS pointers, not real content.

    LFS is NOT fetched (see module help): these files index as pointer
    text. Returns (total_count, [sample relpaths]). Capped for speed,
    and each scan is bounded by _LFS_WALK_BUDGET wall-clock seconds
    (partial results on expiry).

    Runs in the sync worker after a successful materialization; the
    result is stored on the drive row and the list page renders those
    stored columns without ever walking the tree. drive_id is accepted
    for caller compatibility and ignored.

    Hardened: only regular files are ever opened — FIFOs, sockets,
    devices and symlinks are skipped via lstat (no follow), so an
    open() can never block on one — and each directory is descended
    into at most once (device + inode identity of the target), so
    directory-junction / symlink-dir cycles cannot loop os.walk
    forever. Windows follows junctions in os.walk and try/except
    cannot save either case.
    """
    return _scan_lfs_pointers(worktree)


def _scan_lfs_pointers(worktree):
    """Walk the tree and count LFS pointers (see find_lfs_pointers)."""
    hits, checked, total = [], 0, 0
    deadline = time.monotonic() + _LFS_WALK_BUDGET
    try:
        root = os.lstat(worktree)
        seen = {(root.st_dev, root.st_ino)}
    except OSError:
        return total, hits
    for dirpath, dirnames, filenames in os.walk(worktree):
        if time.monotonic() >= deadline:
            return total, hits  # budget exhausted: partial result
        if ".git" in dirnames:
            dirnames.remove(".git")
        # Cycle-safe: prune directories already visited (identity of the
        # *target*, so junctions/symlink-dirs collapse onto their first
        # visit). lstat first — symlinks are never followed and dead
        # entries are dropped; identity via stat, which resolves
        # junctions to the real directory.
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            try:
                if os.path.islink(full):
                    dirnames.remove(name)
                    continue
                st = os.stat(full)
            except OSError:
                dirnames.remove(name)
                continue
            if not stat.S_ISDIR(st.st_mode):
                dirnames.remove(name)
                continue
            if st.st_ino != 0:
                ident = (st.st_dev, st.st_ino)
                if ident in seen:
                    dirnames.remove(name)
                    continue
                seen.add(ident)
        # Keep the walk stable and cheap on huge checkouts.
        dirnames.sort()
        for filename in sorted(filenames):
            if checked >= _LFS_MAX_FILES:
                return total, hits
            if checked % 200 == 0 and time.monotonic() >= deadline:
                return total, hits  # budget exhausted: partial result
            checked += 1
            path = os.path.join(dirpath, filename)
            try:
                # Regular files only (lstat: no follow) — never open
                # FIFOs/sockets/devices/symlinks-to-anything.
                lst = os.lstat(path)
                if not stat.S_ISREG(lst.st_mode):
                    continue
                if lst.st_size > _LFS_MAX_BYTES:
                    continue
                with open(path, "rb") as fh:
                    # Re-check after open: the entry may have been
                    # swapped for something non-regular in between.
                    if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                        continue
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
        with _network_timeout():
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
            if stat.S_ISLNK(entry.mode):
                continue
            rel = os.path.normpath(entry.path.decode())
            full = os.path.normpath(os.path.join(path, rel))
            try:
                if os.path.commonpath([path, full]) != path:
                    continue
            except ValueError:
                continue
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
        with _network_timeout():
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
        with _network_timeout():
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


# --- Clone-URL policy (SSRF / local-file lockdown). ---
#
# `https://` is always allowed provided the host resolves exclusively to
# public IP addresses. Every other scheme, host, or local path (ssh://,
# git://, plain http://, file://, scp-like user@host:path, `/srv/git/...`,
# `C:/...`) is refused unless explicitly listed in the
# GITDRIVES_ALLOWED_HOSTS admin allowlist (comma- or semicolon-separated
# hostnames, CIDR ranges, or explicit local paths; default empty = deny).
# A path-shaped entry allowlists that path and everything under it. The
# same policy is enforced on every sync (materialize/pre_sync), so rows
# created before the lockdown cannot be cloned/pulled anymore.
# Hostnames are resolved with socket.getaddrinfo (IPv4-mapped IPv6
# unwrapped) and rejected unless EVERY address is public; unresolvable
# hosts fail closed. All refusals happen BEFORE ls_remote/clone/pull, so
# a rejected URL never triggers a network connection or filesystem touch.

_GITDRIVES_ALLOWLIST_ENV = "GITDRIVES_ALLOWED_HOSTS"

_SCP_LIKE_RE = re.compile(
    r"^(?P<user>[^/@\s:]+)@(?P<host>[^:/\s]+):(?P<path>.*)$")

_PATH_LIKE_RE = re.compile(r"^[A-Za-z]:")


def _looks_like_path(entry):
    """True when an allowlist entry is a local path, not a host/CIDR."""
    return ("/" in entry or "\\" in entry
            or _PATH_LIKE_RE.match(entry) is not None)


def _allowlist_entries():
    """(hosts, networks, paths) parsed from GITDRIVES_ALLOWED_HOSTS."""
    hosts, networks, paths = set(), [], []
    raw = os.environ.get(_GITDRIVES_ALLOWLIST_ENV, "")
    for part in re.split(r"[;,]+", raw):
        entry = part.strip()
        if not entry:
            continue
        try:
            # CIDR range or literal IP — checked first because CIDR
            # notation contains a slash that would otherwise look
            # path-shaped.
            networks.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass
        if _looks_like_path(entry):
            paths.append(os.path.normcase(os.path.abspath(entry)))
            continue
        hosts.add(entry.lower().rstrip("."))
    return hosts, networks, paths


def _host_allowlisted(host):
    """True when a hostname (or literal IP inside a CIDR entry) is listed."""
    hosts, networks, _paths = _allowlist_entries()
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return False
    if name in hosts:
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def _path_allowlisted(raw):
    """True when a local path falls under an allowlisted path entry."""
    _hosts, _networks, paths = _allowlist_entries()
    if not paths:
        return False
    cand = os.path.normcase(os.path.abspath(raw))
    return any(cand == p or cand.startswith(p + os.sep) for p in paths)


def _public_dns_error(host):
    """None when every resolved address of host is public, else an error."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return (f"Could not resolve the repository host {host!r}. Only "
                "public hosts are allowed.")
    addresses = [info[4][0] for info in infos]
    if not addresses:
        return (f"Could not resolve the repository host {host!r}. Only "
                "public hosts are allowed.")
    for raw_ip in addresses:
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return (f"Could not resolve the repository host {host!r}. "
                    "Only public hosts are allowed.")
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return ("That host resolves to a non-public address; only "
                    "public repository hosts are allowed (ask the "
                    "administrator about the GITDRIVES_ALLOWED_HOSTS "
                    "allowlist for private mirrors).")
    return None


def _remote_policy_error(host):
    """Verdict for a URL with a remote host but a non-https scheme (or
    scp-like syntax): allowed only via the admin allowlist."""
    if host and _host_allowlisted(host):
        return None
    return ("Only https:// clone URLs are allowed by default; ask the "
            "administrator to list this host in GITDRIVES_ALLOWED_HOSTS.")


def url_policy_error(url):
    """Clone-URL policy verdict: an error string, or None when the URL may
    proceed to ls_remote. Never touches the network except for the
    public-IP DNS check on non-allowlisted https hosts."""
    raw = (url or "").strip()
    if not raw:
        return "A repository URL is required."
    scp = _SCP_LIKE_RE.match(raw)
    if scp:
        return _remote_policy_error(scp.group("host"))
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme == "https":
        host = parts.hostname
        if not host:
            return "That repository URL has no host."
        if _host_allowlisted(host):
            return None
        return _public_dns_error(host)
    if scheme == "" or (len(scheme) == 1 and scheme.isalpha()):
        # Plain local path (including Windows `C:/...` / `C:\\...`, which
        # urlsplit reports as a one-letter scheme): denied by default like
        # any other non-https URL — allowlistable only as an explicit
        # local path in GITDRIVES_ALLOWED_HOSTS.
        if _path_allowlisted(raw):
            return None
        return ("Local repository paths are not allowed by default; ask "
                "the administrator to list this path in "
                "GITDRIVES_ALLOWED_HOSTS.")
    return _remote_policy_error(parts.hostname)


class GitDriveSource:
    """Drive source provider for git-backed mirror drives."""

    kind = "git"

    def validate(self, user, config):
        url = (config.get("repo_url") or "").strip()
        if not url:
            return "A repository URL is required."
        policy_error = url_policy_error(url)
        if policy_error:
            return policy_error
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
        # Re-check the URL policy on the sync path: rows created before
        # the lockdown (or crafted directly in the DB) must not be
        # cloned. Returns the error string like any other failure.
        policy_error = url_policy_error(drive.repo_url or "")
        if policy_error:
            return policy_error
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
        # Same re-check as materialize: covers the pull branch below for
        # checkouts that already exist.
        policy_error = url_policy_error(drive.repo_url or "")
        if policy_error:
            return policy_error
        target = cache_path(drive.id)
        if not os.path.exists(os.path.join(target, ".git")):
            error = self.materialize(drive, on_progress=on_progress)
            if error:
                return error
        error = pull_repo(target, url=drive.repo_url,
                          branch=drive.repo_branch, on_progress=on_progress,
                          **_auth_kwargs(drive))
        if error:
            return error
        # Successful materialization: record the advisory LFS badge on the
        # drive row so the list page reads stored columns and never walks
        # the checkout. Failure/skip paths above store nothing, leaving
        # any previous values in place.
        _store_lfs_scan(drive, target)
        return None

    def cleanup(self, drive):
        remove_tree(cache_path(drive.id))


def _store_lfs_scan(drive, worktree):
    """Persist the advisory LFS badge after a successful materialization.

    Runs in the sync worker (never on a page load): scans the fresh
    checkout — still bounded by the walk caps and wall-clock budget —
    and stores (total, sample, now) on the drive row. Advisory only: a
    scan or store failure keeps the previous values and never fails
    the sync.
    """
    from app.extensions import db
    from app.models import utcnow

    try:
        total, sample = find_lfs_pointers(worktree, drive_id=drive.id)
    except Exception:
        return
    try:
        drive.lfs_total = total
        drive.lfs_sample = list(sample)
        drive.lfs_scanned_at = utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()
