"""Inspect local Git state and run explicit remote operations with dulwich.

Everything here is pure Python (dulwich + merge3); the module never executes
the ``git`` binary. Commit identity and HTTPS credentials live on the
``Repository`` row, so nothing is read from or written to the user's Git
configuration.
"""

import io
import os
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote, urlsplit, urlunsplit

from dulwich import porcelain
from dulwich.client import HTTPProxyUnauthorized, HTTPUnauthorized
from dulwich.config import ConfigFile, StackedConfig
from dulwich.diff_tree import tree_changes
from dulwich.errors import NotGitRepository
from dulwich.graph import find_merge_base
from dulwich.index import ConflictedIndexEntry
from dulwich.refs import check_ref_format
from dulwich.repo import Repo

from app.extensions import db
from app.services import ai_service, sync_service

from .models import Repository, same_path

MAX_DIFF_CHARS = 200_000
MAX_CONFLICT_PREVIEW_BYTES = 64_000
REMOTE_TIMEOUT = 60
MAX_IDENTITY_NAME = 120
MAX_IDENTITY_EMAIL = 254
MAX_REMOTE_USERNAME = 255

DENIED_MESSAGE = "Remote access was denied. Check the saved remote credentials and repository access."
NO_CREDENTIALS_MESSAGE = (
    "The remote refused anonymous access and no token is saved for this drive. Git changes does "
    "not use the system Git Credential Manager: create a Personal Access Token on the Git host "
    "(Azure DevOps: User settings \u2192 Personal access tokens, scope Code Read or Read & write) "
    "and save it under Remote credentials in Repository details."
)
REJECTED_MESSAGE = "The remote rejected this push. Fetch and review upstream changes, and check branch protection requirements."
UNAVAILABLE_MESSAGE = "The remote is unavailable. Check network and remote status, then try again."
INSPECT_MESSAGE = "Git could not inspect this repository. Check its path, permissions and ownership."


class GitSyncError(ValueError):
    pass


# --- repository access -------------------------------------------------------

def _decode(path):
    return path.decode("utf-8", errors="surrogateescape") if isinstance(path, bytes) else path


def _tree_path(path):
    """Posix-style repository-relative path (or ref name) as dulwich bytes."""
    return path.encode("utf-8", errors="surrogateescape")


def _fs_status_path(path):
    """porcelain.status returns OS-separator fs paths; normalize to tree paths."""
    return (os.fsdecode(path) if isinstance(path, bytes) else path).replace(os.sep, "/")


def _config_stack_with_timeout(repo):
    stack = Repo.get_config_stack(repo)
    fallback = ConfigFile()
    fallback.set((b"http",), b"timeout", str(REMOTE_TIMEOUT).encode())
    return StackedConfig([*stack.backends, fallback], writable=stack.writable)


@contextmanager
def _open(root):
    try:
        repo = Repo(root)
    except (NotGitRepository, OSError) as exc:
        raise GitSyncError(INSPECT_MESSAGE) from exc
    try:
        if repo.bare:
            raise GitSyncError("Choose a repository with a working tree, not a bare repository.")
        # http.timeout fallback so remote calls never hang; the user's own value wins.
        repo.get_config_stack = lambda: _config_stack_with_timeout(repo)
        repo.hooks.clear()  # never run repository hook scripts from a web request
        yield repo
    finally:
        repo.close()


def repository_root(path):
    normalized, error = sync_service.validate_path(path)
    if error:
        raise GitSyncError(error)
    with _open(normalized) as repo:
        if os.path.normcase(os.path.realpath(repo.path)) != os.path.normcase(normalized):
            raise GitSyncError("Choose the repository root folder, not a subfolder.")
    return normalized


def _head(repo):
    try:
        return repo.head().decode("ascii")
    except KeyError:
        return ""


def _ref(repo, name):
    try:
        return repo.refs[_tree_path(name)]
    except KeyError:
        return None


def _branch(repo):
    ref = repo.refs.read_ref(b"HEAD")
    if ref and ref.startswith(b"ref: refs/heads/"):
        return _decode(ref[len(b"ref: refs/heads/"):])
    return None


def _config_get(config, section, name):
    try:
        value = config.get(section, name)
    except KeyError:
        return ""
    return _decode(value) if value else ""


def _remote_url(repo, remote="origin"):
    return _config_get(repo.get_config_stack(), (b"remote", _tree_path(remote)), b"url")


def remote_guidance(url):
    """Host-specific help for authenticating against ``url`` (shown on the page).

    Returns a dict with ``kind``, ``label``, ``needs_token``, ``token_url``
    (direct link to the token page when it can be derived from the URL),
    ``scopes`` and ordered ``steps``. Never includes credentials.
    """
    url = (url or "").strip()
    if not url:
        return {"kind": "none", "label": "No remote", "needs_token": False, "token_url": None,
                "scopes": "", "steps": ["Add an origin remote to this repository to enable fetch and push."]}
    lower = url.lower()
    if not lower.startswith(("http://", "https://")):
        if lower.startswith("ssh://") or ("@" in url and ":" in url.split("@", 1)[1] and "://" not in url):
            return {"kind": "ssh", "label": "SSH remote", "needs_token": False, "token_url": None,
                    "scopes": "", "steps": [
                        "SSH remotes use the SSH client and keys of the account running the DocIndex server, not the token saved here.",
                        "Make sure that account has an SSH key registered with the Git host, or switch the remote to its HTTPS URL to use a token."]}
        return {"kind": "local", "label": "Local or network path", "needs_token": False, "token_url": None,
                "scopes": "", "steps": ["This remote is a folder path: no credentials are needed, only read/write permission on that folder."]}
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "dev.azure.com" or host.endswith(".visualstudio.com"):
        org = parts[0] if host == "dev.azure.com" and parts else host.split(".")[0]
        base = f"https://dev.azure.com/{org}" if host == "dev.azure.com" else f"https://{host}"
        return {"kind": "azure", "label": "Azure DevOps", "needs_token": True,
                "token_url": f"{base}/_usersSettings/tokens" if org else None,
                "scopes": "Code \u2192 Read (fetch) or Read & write (push)",
                "steps": [
                    "Azure DevOps does not accept your Windows/browser sign-in from DocIndex: a Personal Access Token (PAT) is required.",
                    f"Open the token page (link below) while signed in to the \u201c{org}\u201d organization, or go to your avatar \u2192 User settings \u2192 Personal access tokens." if org else
                    "Sign in to Azure DevOps, open your avatar \u2192 User settings \u2192 Personal access tokens.",
                    "Click New Token, give it a name, choose the organization and an expiry (max. 1 year), and under Scopes pick Code \u2192 Read (or Read & write if you also want to push).",
                    "Copy the token immediately (it is shown only once) and paste it into the token field here; the username can stay empty.",
                    "Use Check remote read access to confirm, then Fetch upstream. When the PAT expires, paste a new one here."]}
    if host == "github.com" or host.endswith(".github.com"):
        return {"kind": "github", "label": "GitHub", "needs_token": True,
                "token_url": "https://github.com/settings/tokens",
                "scopes": "repo (classic) or Contents: Read / Read and write (fine-grained)",
                "steps": [
                    "Private GitHub repositories need a Personal Access Token; passwords are not accepted over HTTPS.",
                    "Open Settings \u2192 Developer settings \u2192 Personal access tokens (link below) and generate a token with access to this repository.",
                    "Paste the token here; the username can stay empty or be your GitHub login.",
                    "Use Check remote read access, then Fetch upstream."]}
    if host == "gitlab.com" or "gitlab" in host:
        return {"kind": "gitlab", "label": "GitLab", "needs_token": True,
                "token_url": f"https://{host}/-/user_settings/personal_access_tokens",
                "scopes": "read_repository (fetch) or write_repository (push)",
                "steps": [
                    "GitLab needs a Personal Access Token for HTTPS access from DocIndex.",
                    "Open your avatar \u2192 Edit profile \u2192 Access tokens (link below), add a token with the read_repository or write_repository scope.",
                    "Paste the token here; the username can stay empty.",
                    "Use Check remote read access, then Fetch upstream."]}
    if host == "bitbucket.org" or "bitbucket" in host:
        return {"kind": "bitbucket", "label": "Bitbucket", "needs_token": True,
                "token_url": "https://bitbucket.org/account/settings/app-passwords/" if host == "bitbucket.org" else None,
                "scopes": "Repositories: Read (fetch) or Write (push)",
                "steps": [
                    "Bitbucket needs an app password (Bitbucket Cloud) or an HTTP access token (Bitbucket Server) for HTTPS access.",
                    "Create it under Personal settings \u2192 App passwords and enter your Bitbucket username plus that password here.",
                    "Use Check remote read access, then Fetch upstream."]}
    return {"kind": "https", "label": host or "HTTPS remote", "needs_token": True, "token_url": None,
            "scopes": "repository read (fetch) / write (push)",
            "steps": [
                "If this repository is private, create a personal access token or app password on the Git host and save it here (username if the host requires one).",
                "DocIndex never uses the system Git Credential Manager or a browser sign-in.",
                "Use Check remote read access, then Fetch upstream."]}


def _tracking(repo):
    branch = _branch(repo)
    if not branch:
        return None
    config = repo.get_config_stack()
    remote = _config_get(config, (b"branch", _tree_path(branch)), b"remote")
    merge_ref = _config_get(config, (b"branch", _tree_path(branch)), b"merge")
    if not remote or remote == "." or not merge_ref.startswith("refs/heads/"):
        return None
    if not _remote_url(repo, remote):
        return None
    short = merge_ref[len("refs/heads/"):]
    upstream_ref = f"refs/remotes/{remote}/{short}"
    upstream_head = _ref(repo, upstream_ref)
    return {
        "branch": branch,
        "remote": remote,
        "merge_ref": merge_ref,
        "upstream_ref": upstream_ref,
        "upstream": f"{remote}/{short}",
        "upstream_head": upstream_head.decode("ascii") if upstream_head else "",
    }


def _state_file(repo, name):
    return os.path.join(repo.controldir(), name)


def _merge_head(repo):
    try:
        with open(_state_file(repo, "MERGE_HEAD"), "rb") as handle:
            return handle.read().split()[0].decode("ascii")
    except (OSError, IndexError, UnicodeDecodeError):
        return ""


def _clear_merge_state(repo):
    for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
        try:
            os.remove(_state_file(repo, name))
        except FileNotFoundError:
            pass


def _ahead_behind(repo, upstream_ref):
    if not upstream_ref:
        return None
    upstream = _ref(repo, upstream_ref)
    head = _head(repo)
    if not upstream or not head:
        return None
    head = head.encode("ascii")
    if head == upstream:
        return {"ahead": 0, "behind": 0}
    try:
        bases = find_merge_base(repo, [head, upstream])
        ahead = sum(1 for _ in repo.get_walker(include=[head], exclude=bases))
        behind = sum(1 for _ in repo.get_walker(include=[upstream], exclude=bases))
    except KeyError:
        return None
    return {"ahead": ahead, "behind": behind}


def _require_tracking(repo):
    tracking = _tracking(repo)
    if tracking is None:
        raise GitSyncError("This branch has no valid remote upstream. Configure an upstream branch first.")
    if not tracking["upstream_head"]:
        raise GitSyncError("The upstream has no fetched commit. Fetch the remote and compare before continuing.")
    return tracking


def _require_clean(repo):
    changes = _changes(repo)
    if any(change["conflict"] for change in changes):
        raise GitSyncError("Resolve or abort the current merge before continuing.")
    if changes:
        raise GitSyncError("This action requires a clean working tree. Commit or safely set aside local changes first.")


# --- drive binding, identity and credentials ----------------------------------

def is_git_drive(drive):
    """True for a (mirror or shared) local-folder drive whose root is a Git checkout."""
    return bool(
        drive is not None and drive.source_path and drive.source_kind == "folder"
        and drive.repo_url is None
        and os.path.isdir(os.path.join(drive.source_path, ".git"))
    )


def for_drive(user, drive):
    """Return the Git Sync record attached to ``drive``, creating it on first use.

    The drive is the source of truth; the record carries the commit identity,
    the remote credentials and the baseline commit. A record left behind by a
    removed drive of the same folder is re-attached so the baseline survives
    re-syncing.
    """
    if drive is None or drive.user_id != user.id or drive.deleted_at is not None:
        raise GitSyncError("Drive not found.")
    if not drive.source_path or drive.source_kind != "folder" or drive.repo_url is not None:
        raise GitSyncError("Git changes are only available for drives synced from a local folder.")
    if not is_git_drive(drive):
        raise GitSyncError(f'The folder of drive "{drive.name}" is not the root of a Git repository. '
                           "Sync the folder that contains the .git directory.")
    root = repository_root(drive.source_path)
    repository = Repository.query.filter_by(user_id=user.id, drive_id=drive.id).first()
    if repository is not None and not same_path(repository.path, root):
        db.session.delete(repository)  # drive id reused by an unrelated drive
        db.session.flush()
        repository = None
    if repository is None:
        repository = Repository.query.filter_by(user_id=user.id, path=root).first()
        if repository is None:
            with _open(root) as repo:
                baseline = _head(repo) or None
            repository = Repository(user_id=user.id, path=root, account="", baseline=baseline)
            db.session.add(repository)
        repository.drive_id = drive.id
    if repository.name != drive.name:
        repository.name = drive.name
    if db.session.new or db.session.dirty:
        db.session.commit()
    return repository


def _printable(value, limit, label):
    value = (value or "").strip()
    if len(value) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise GitSyncError(f"The {label} must be at most {limit} printable characters.")
    return value


def set_identity(repository, name, email):
    name = _printable(name, MAX_IDENTITY_NAME, "author name")
    email = _printable(email, MAX_IDENTITY_EMAIL, "author email")
    if "<" in name or ">" in name:
        raise GitSyncError("The author name must not contain angle brackets.")
    if email and (any(char in email for char in "<> ") or "@" not in email):
        raise GitSyncError("Enter a valid author email address.")
    repository.author_name = name
    repository.author_email = email
    db.session.commit()


def set_credentials(repository, username, token, clear=False):
    """Save the HTTPS username/token; an empty token keeps the stored one."""
    if clear:
        repository.remote_username = None
        repository.remote_token = None
        db.session.commit()
        return
    username = _printable(username, MAX_REMOTE_USERNAME, "remote username")
    token = (token or "").strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in token):
        raise GitSyncError("The token must not contain control characters.")
    repository.remote_username = username or None
    if token:
        repository.remote_token = ai_service.encrypt_api_key(token)
    db.session.commit()


def _identity(repository, action):
    name = (repository.author_name or "").strip()
    email = (repository.author_email or "").strip()
    if not name or not email:
        raise GitSyncError(f"Set the commit identity (name and email) in Repository details before {action}.")
    return f"{name} <{email}>".encode()


def _auth_kwargs(repository, url):
    if not url.lower().startswith(("http://", "https://")):
        return {}
    token = ai_service.decrypt_api_key(repository.remote_token or "")
    if not token:
        return {}
    # dulwich only sends Basic auth when a username is present; Azure DevOps
    # and GitHub accept any username alongside a personal access token.
    username = (repository.remote_username or "").strip() or "pat"
    return {"username": username, "password": token}


def _needs_credentials(url, auth):
    return url.lower().startswith(("http://", "https://")) and not auth


def _remote_call_with_credentials_hint(url, auth, operation, failure_message, timeout_message):
    try:
        return _remote_call(operation, failure_message, timeout_message)
    except GitSyncError as exc:
        if str(exc) == DENIED_MESSAGE and _needs_credentials(url, auth):
            raise GitSyncError(NO_CREDENTIALS_MESSAGE) from exc
        raise


# --- status and diffs ----------------------------------------------------------

def _branches(repo):
    config = repo.get_config_stack()
    current = _branch(repo)
    branches = []
    for name in sorted(porcelain.branch_list(repo)):
        name = _decode(name)
        remote = _config_get(config, (b"branch", _tree_path(name)), b"remote")
        merge_ref = _config_get(config, (b"branch", _tree_path(name)), b"merge")
        upstream = ""
        if remote and remote != "." and merge_ref.startswith("refs/heads/"):
            upstream = f"{remote}/{merge_ref[len('refs/heads/'):]}"
        branches.append({"name": name, "upstream": upstream, "current": name == current})
    return branches


def _remote_label(value):
    if "://" in value:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    return value.split("@", 1)[-1]


def _conflicts(repo):
    """{tree path: ConflictedIndexEntry} for every unmerged index entry."""
    return {_decode(path): entry for path, entry in repo.open_index().items()
            if isinstance(entry, ConflictedIndexEntry)}


def _conflict_code(entry):
    ancestor = entry.ancestor is not None
    ours, theirs = entry.this is not None, entry.other is not None
    if ours and theirs:
        return "UU" if ancestor else "AA"
    if ours:
        return "UD" if ancestor else "AU"
    if theirs:
        return "DU" if ancestor else "UA"
    return "DD"


def _changes(repo):
    """Porcelain-v1-style change list (XY status codes) derived from dulwich.

    Renames are not detected (they show as a deletion plus an addition)."""
    status = porcelain.status(repo, untracked_files="all")
    conflicts = _conflicts(repo)
    staged = {}
    for kind, code in (("add", "A"), ("delete", "D"), ("modify", "M")):
        for path in status.staged.get(kind, []):
            staged[_fs_status_path(path)] = code
    unstaged = {_fs_status_path(path) for path in status.unstaged}
    untracked = {_fs_status_path(path) for path in status.untracked}

    def worktree_code(path):
        if path not in unstaged:
            return " "
        return "M" if os.path.lexists(os.path.join(repo.path, path)) else "D"

    changes = [{"status": _conflict_code(entry), "path": path, "original": None, "conflict": True}
               for path, entry in conflicts.items()]
    for path, code in staged.items():
        if path not in conflicts:
            changes.append({"status": code + worktree_code(path), "path": path, "original": None, "conflict": False})
    for path in unstaged - set(staged) - set(conflicts):
        changes.append({"status": " " + worktree_code(path), "path": path, "original": None, "conflict": False})
    for path in untracked - set(staged) - set(conflicts):
        changes.append({"status": "??", "path": path, "original": None, "conflict": False})
    changes.sort(key=lambda change: change["path"])
    return changes


def _diff(repo, commit=None, staged=False, paths=None):
    """Unified diff; ``paths`` (list of repo-relative str) limits the files examined.

    dulwich's working-tree diff otherwise reads every committed blob and stats
    every file, which takes seconds on large or slow (network/OneDrive) trees.
    An empty list means nothing changed and skips the walk entirely.
    """
    if paths is not None and not paths:
        return ""
    stream = io.BytesIO()
    try:
        porcelain.diff(repo, commit=_tree_path(commit) if commit else None, staged=staged,
                       paths=[_tree_path(path) for path in paths] if paths else None,
                       outstream=stream)
    except KeyError as exc:
        raise GitSyncError("The comparison commit is no longer in this repository.") from exc
    return stream.getvalue().decode("utf-8", errors="replace")


def _tree_changed_paths(repo, old_commit, new_commit):
    """Paths that differ between two commits' trees (cheap: tree objects only)."""
    if not old_commit or not new_commit or old_commit == new_commit:
        return set()
    try:
        old_tree = repo[_tree_path(old_commit)].tree
        new_tree = repo[_tree_path(new_commit)].tree
    except KeyError as exc:
        raise GitSyncError("The comparison commit is no longer in this repository.") from exc
    paths = set()
    for change in tree_changes(repo.object_store, old_tree, new_tree):
        for entry in (change.old, change.new):
            if entry is not None and entry.path is not None:
                paths.add(_decode(entry.path))
    return paths


def _group_diff(diff):
    files = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            files.append({"header": line.rstrip("\r\n"), "diff": ""})
        elif files:
            files[-1]["diff"] += line
    if diff and not files:
        files.append({"header": "Diff", "diff": diff})
    return files


def inspect_summary(repository):
    """Cheap repository facts (refs, config, identity) — no working-tree walk."""
    root = repository_root(repository.path)
    with _open(root) as repo:
        head = _head(repo)
        tracking = _tracking(repo)
        remote_url = _remote_url(repo)
        return {
            "branch": _branch(repo) or "Detached HEAD",
            "head": head, "baseline": repository.baseline,
            "remote": _remote_label(remote_url),
            "remote_help": remote_guidance(remote_url),
            "author_name": repository.author_name or "",
            "author_email": repository.author_email or "",
            "has_remote_token": bool(repository.remote_token),
            "upstream": tracking["upstream"] if tracking else "",
            "upstream_branch": tracking["merge_ref"][len("refs/heads/"):] if tracking else "",
            "remote_url": remote_url,
            "suggested_branch": suggested_branch_name(repository, _branch(repo)),
            "ahead_behind": _ahead_behind(repo, tracking["upstream_ref"]) if tracking else None,
            "has_upstream_commit": bool(tracking and tracking["upstream_head"]),
            "branches": _branches(repo),
            "merge_in_progress": bool(_merge_head(repo)),
        }


def inspect_changes(repository, basis="head"):
    """Working-tree status, diffs and conflicts — walks every file (slow on big trees)."""
    root = repository_root(repository.path)
    with _open(root) as repo:
        head = _head(repo)
        reference = repository.baseline if basis == "initial" else head
        if basis == "initial" and not reference:
            raise GitSyncError("This repository had no commit when it was connected.")
        changes = _changes(repo)
        conflict_count = sum(change["conflict"] for change in changes)
        staged_count = sum(change["status"][0] not in (" ", "?") for change in changes)
        merge_unstaged_count = sum(
            not change["conflict"] and change["status"][1] not in (" ", "?")
            for change in changes
        )
        merge_untracked_count = sum(change["status"] == "??" for change in changes)
        merge_head = _merge_head(repo)
        # Only tracked files reported by status can differ from HEAD; against the
        # baseline, files changed by later commits differ as well.
        tracked_changed = sorted(change["path"] for change in changes if change["status"] != "??")
        if reference:
            paths = sorted(set(tracked_changed) | _tree_changed_paths(repo, reference, head))
            diff = _diff(repo, commit=reference, paths=paths)
        else:
            diff = _diff(repo, paths=tracked_changed) + _diff(repo, staged=True, paths=tracked_changed)
        merge_diff = ""
        if merge_head:
            merge_diff = diff if basis == "head" else _diff(repo, commit=head, paths=tracked_changed)
        diff_truncated = len(diff) > MAX_DIFF_CHARS
        merge_diff_truncated = len(merge_diff) > MAX_DIFF_CHARS
        visible_diff = diff[:MAX_DIFF_CHARS]
        visible_merge_diff = merge_diff[:MAX_DIFF_CHARS]
        conflicts = [
            {**change, "preview": _conflict_preview(repo, change["path"])}
            for change in changes if change["conflict"]
        ]
        return {
            "head": head,
            "merge_in_progress": bool(merge_head),
            "conflicts": conflicts,
            "merge_unstaged_count": merge_unstaged_count,
            "merge_untracked_count": merge_untracked_count,
            "merge_diff": visible_merge_diff,
            "merge_diff_files": _group_diff(visible_merge_diff),
            "merge_diff_truncated": merge_diff_truncated,
            "changes": changes, "change_count": len(changes),
            "conflict_count": conflict_count, "staged_count": staged_count,
            "diff": visible_diff, "diff_files": _group_diff(visible_diff),
            "diff_truncated": diff_truncated,
        }


def inspect(repository, basis="head"):
    """Full state: summary + working-tree changes (used by tests and callers that need both)."""
    return {**inspect_summary(repository), **inspect_changes(repository, basis)}


# --- remote operations -----------------------------------------------------------

def _classify_remote_error(text, failure_message, timeout_message):
    text = text.lower()
    if any(term in text for term in ("timed out", "timeout")):
        return timeout_message
    if any(term in text for term in (
            "authentication", "unauthorized", "permission denied", "access denied",
            "repository not found", "401", "403", "credentials")):
        return DENIED_MESSAGE
    if any(term in text for term in (
            "non-fast-forward", "fetch first", "protected branch", "rejected",
            "pre-receive hook declined", "diverged", "gh006")):
        return REJECTED_MESSAGE
    if any(term in text for term in (
            "resolve", "name or service not known", "connection refused", "max retries",
            "unreachable", "hung up", "connection aborted", "connection reset",
            "502", "503", "504", "newconnectionerror")):
        return UNAVAILABLE_MESSAGE
    return failure_message


def _remote_call(operation, failure_message, timeout_message):
    """Run ``operation()`` mapping transport errors to safe messages (never exposing details)."""
    try:
        return operation()
    except GitSyncError:
        raise
    except (HTTPUnauthorized, HTTPProxyUnauthorized, NotGitRepository) as exc:
        raise GitSyncError(DENIED_MESSAGE) from exc
    except porcelain.DivergedBranches as exc:
        raise GitSyncError(REJECTED_MESSAGE) from exc
    except TimeoutError as exc:
        raise GitSyncError(timeout_message) from exc
    except ConnectionError as exc:
        raise GitSyncError(UNAVAILABLE_MESSAGE) from exc
    except Exception as exc:  # noqa: BLE001 - GitProtocolError, porcelain.Error, urllib3/OS errors
        raise GitSyncError(_classify_remote_error(str(exc), failure_message, timeout_message)) from exc


def check_remote_access(repository):
    root = repository_root(repository.path)
    with _open(root) as repo:
        url = _remote_url(repo)
        if not url:
            raise GitSyncError("No origin remote is configured for this repository.")
        auth = _auth_kwargs(repository, url)
        config = repo.get_config_stack()
        _remote_call_with_credentials_hint(
            url, auth,
            lambda: porcelain.ls_remote(url, config=config, **auth),
            "Remote read access failed. Check the origin URL, the saved credentials and repository access.",
            "The remote read check timed out. Check remote availability and try again.",
        )
    return bool(auth)


def fetch(repository):
    root = repository_root(repository.path)
    with _open(root) as repo:
        tracking = _tracking(repo)
        if tracking is None:
            raise GitSyncError("This branch has no valid remote upstream to fetch.")
        url = _remote_url(repo, tracking["remote"])
        auth = _auth_kwargs(repository, url)
        _remote_call_with_credentials_hint(
            url, auth,
            lambda: porcelain.fetch(repo, tracking["remote"], outstream=io.StringIO(),
                                    errstream=io.BytesIO(), **auth),
            "Fetch failed. Check the upstream URL, the saved credentials and repository access.",
            "Fetch timed out. Check the repository status before trying again.",
        )


def push(repository):
    root = repository_root(repository.path)
    with _open(root) as repo:
        if _merge_head(repo):
            raise GitSyncError("Complete or abort the current merge before pushing.")
        if any(change["conflict"] for change in _changes(repo)):
            raise GitSyncError("Resolve or abort the current conflicts before pushing.")
        # Uncommitted files are not part of a push; only commits travel.
        tracking = _require_tracking(repo)
        counts = _ahead_behind(repo, tracking["upstream_ref"])
        if not counts or counts["ahead"] == 0:
            raise GitSyncError("There are no local commits ahead of the configured upstream to push.")
        if counts["behind"]:
            raise GitSyncError("The local branch is behind or diverged. Fetch and review the remote before pushing.")
        url = _remote_url(repo, tracking["remote"])
        auth = _auth_kwargs(repository, url)
        refspec = _tree_path(f"HEAD:{tracking['merge_ref']}")

        def send():
            result = porcelain.push(repo, tracking["remote"], [refspec], outstream=io.BytesIO(),
                                    errstream=io.BytesIO(), **auth)
            if any(error for error in (result.ref_status or {}).values()):
                raise GitSyncError(REJECTED_MESSAGE)

        _remote_call_with_credentials_hint(
            url, auth, send,
            "Push failed. Check remote write access and fetch/review the upstream before retrying.",
            "Push timed out. Check the remote and local branch status before retrying.",
        )


MAX_BRANCH_NAME = 120


def suggested_branch_name(repository, branch):
    """Default name for publishing local commits on a side branch."""
    owner = (repository.author_name or "").strip().lower()
    owner = "".join(char if char.isalnum() else "-" for char in owner).strip("-") or "docindex"
    base = (branch or "work").replace("/", "-")
    return f"{owner}/{base}-changes"[:MAX_BRANCH_NAME]


def validate_branch_name(name):
    name = (name or "").strip()
    if not name or len(name) > MAX_BRANCH_NAME:
        raise GitSyncError(f"Enter a branch name of at most {MAX_BRANCH_NAME} characters.")
    if name.startswith("refs/") or name.startswith("-") or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise GitSyncError("Enter a plain branch name such as topic/my-change.")
    if not check_ref_format(b"refs/heads/" + name.encode("utf-8")):
        raise GitSyncError("That branch name is not valid for Git (no spaces, '..', '~', '^', ':', '?', '*', '[' or trailing '.lock').")
    return name


def push_to_new_branch(repository, name):
    """Publish the current HEAD on ``refs/heads/<name>`` of the upstream remote.

    Lets a user share commits (for a pull request) while the tracked branch is
    behind the remote or the working tree still has uncommitted edits. The
    push is never forced: an existing remote branch with other history is
    rejected. Returns (branch_name, remote_name).
    """
    name = validate_branch_name(name)
    root = repository_root(repository.path)
    with _open(root) as repo:
        if _merge_head(repo):
            raise GitSyncError("Complete or abort the current merge before pushing.")
        if any(change["conflict"] for change in _changes(repo)):
            raise GitSyncError("Resolve or abort the current conflicts before pushing.")
        if not _head(repo):
            raise GitSyncError("There is no commit to push yet.")
        tracking = _tracking(repo)
        remote = tracking["remote"] if tracking else "origin"
        url = _remote_url(repo, remote)
        if not url:
            raise GitSyncError("No origin remote is configured for this repository.")
        if tracking and name == _decode(_tree_path(tracking["merge_ref"]))[len("refs/heads/"):]:
            raise GitSyncError("That is the tracked branch itself; use the normal push for it.")
        auth = _auth_kwargs(repository, url)
        refspec = _tree_path(f"HEAD:refs/heads/{name}")

        def send():
            result = porcelain.push(repo, remote, [refspec], outstream=io.BytesIO(),
                                    errstream=io.BytesIO(), **auth)
            if any(error for error in (result.ref_status or {}).values()):
                raise GitSyncError(REJECTED_MESSAGE)

        _remote_call_with_credentials_hint(
            url, auth, send,
            "Push failed. Check remote write access and that the branch name is free.",
            "Push timed out. Check the remote and try again.",
        )
        return name, remote


def pull_request_url(remote_url, source_branch, target_branch):
    """Link to open a pull request for ``source_branch`` into ``target_branch``, if the host is known."""
    remote_url = (remote_url or "").strip()
    if not remote_url.lower().startswith(("http://", "https://")):
        return None
    parsed = urlsplit(remote_url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    source = quote(source_branch, safe="")
    target = quote(target_branch or "", safe="")
    if host == "dev.azure.com" or host.endswith(".visualstudio.com"):
        if "_git" not in parts:
            return None
        base = urlunsplit((parsed.scheme, parsed.netloc, "/" + "/".join(parts[:parts.index("_git") + 2]), "", ""))
        query = f"sourceRef={source}" + (f"&targetRef={target}" if target else "")
        return f"{base}/pullrequestcreate?{query}"
    if host == "github.com" and len(parts) >= 2:
        repo_path = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
        compare = f"{target}...{source}" if target else source
        return f"https://github.com/{parts[0]}/{repo_path}/compare/{compare}?expand=1"
    if "gitlab" in host and len(parts) >= 2:
        repo_path = "/".join(parts)
        repo_path = repo_path[:-4] if repo_path.endswith(".git") else repo_path
        query = f"merge_request%5Bsource_branch%5D={source}" + (f"&merge_request%5Btarget_branch%5D={target}" if target else "")
        return f"https://{host}/{repo_path}/-/merge_requests/new?{query}"
    return None


# --- merge review --------------------------------------------------------------------

def start_merge(repository):
    root = repository_root(repository.path)
    with _open(root) as repo:
        if _merge_head(repo):
            raise GitSyncError("Complete or abort the current merge before starting another merge.")
        _require_clean(repo)
        tracking = _require_tracking(repo)
        counts = _ahead_behind(repo, tracking["upstream_ref"])
        if not counts or counts["behind"] == 0:
            raise GitSyncError("There are no fetched upstream commits to merge.")
        try:
            porcelain.merge(repo, _tree_path(tracking["upstream_ref"]), no_commit=True, no_ff=True)
        except Exception as exc:  # noqa: BLE001 - dulwich raises several unrelated types here
            raise GitSyncError("Git could not start the merge. Review the local repository status before retrying.") from exc
        # dulwich records MERGE_HEAD only when there are conflicts; a clean
        # --no-commit merge must still be completed or aborted explicitly.
        with open(_state_file(repo, "MERGE_HEAD"), "wb") as handle:
            handle.write(tracking["upstream_head"].encode("ascii") + b"\n")
        with open(_state_file(repo, "MERGE_MSG"), "wb") as handle:
            handle.write(f"Merge remote-tracking branch '{tracking['upstream']}'\n".encode())


def _safe_conflict_path(root, path, conflict_paths):
    if not isinstance(path, str) or path not in conflict_paths:
        raise GitSyncError("A selected path is not a currently conflicted file. Refresh the page and try again.")
    posix_path = PurePosixPath(path)
    windows_path = PureWindowsPath(path)
    if (posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive
            or "\\" in path or any(part in ("", ".", "..") for part in posix_path.parts)):
        raise GitSyncError("A conflicted path is not safe to resolve from this repository.")
    root_path = Path(root).resolve()
    candidate = root_path
    for part in posix_path.parts:
        candidate = candidate / part
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise GitSyncError("Symlinks and paths outside the repository cannot be resolved in Git Sync.") from exc
        if candidate.is_symlink() or not resolved.is_relative_to(root_path):
            raise GitSyncError("Symlinks and paths outside the repository cannot be resolved in Git Sync.")
    return path


def _safe_changed_path(root, path, changed_paths):
    if not isinstance(path, str) or path not in changed_paths:
        raise GitSyncError("That file is no longer changed. Refresh the page and try again.")
    posix_path = PurePosixPath(path)
    windows_path = PureWindowsPath(path)
    if (posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive
            or "\\" in path or any(part in ("", ".", "..") for part in posix_path.parts)):
        raise GitSyncError("That file path is not safe to discard from this repository.")
    root_path = Path(root).resolve()
    candidate = root_path
    parts = posix_path.parts
    for part in parts[:-1]:
        candidate = candidate / part
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise GitSyncError("Symlinks and paths outside the repository cannot be discarded.") from exc
        if candidate.is_symlink() or not resolved.is_relative_to(root_path):
            raise GitSyncError("Symlinks and paths outside the repository cannot be discarded.")
    target = candidate / parts[-1]
    if not target.parent.resolve().is_relative_to(root_path):
        raise GitSyncError("That file path is outside the repository.")
    return path, target


def _is_cloud_placeholder_tag(tag):
    return bool(tag and tag & 0xFFFF0FFF == 0x9000001A
                and tag != 0xA000000C)


def _hydrate_cloud_placeholder(path):
    """Make an online-only Windows Cloud Files placeholder available locally."""
    if os.name != "nt":
        return
    try:
        info = os.lstat(path)
    except OSError:
        return
    if not _is_cloud_placeholder_tag(getattr(info, "st_reparse_tag", 0)):
        return

    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = create_file(str(path), 0x80000000, 0x00000001 | 0x00000002 | 0x00000004,
                         None, 3, 0x00000080, None)
    if handle == ctypes.c_void_p(-1).value:
        raise GitSyncError("OneDrive could not open this online-only file. Check your connection and make it available on this device, then retry.")
    try:
        cldapi = ctypes.WinDLL("CldApi", use_last_error=True)
        hydrate = cldapi.CfHydratePlaceholder
        hydrate.argtypes = [ctypes.c_void_p, ctypes.c_longlong, ctypes.c_longlong,
                            ctypes.c_uint32, ctypes.c_void_p]
        hydrate.restype = ctypes.c_long
        result = hydrate(handle, 0, -1, 0, None)
        if result != 0:
            raise GitSyncError("OneDrive could not download this online-only file. Check your connection, choose Always keep on this device in File Explorer, then retry.")
    except GitSyncError:
        raise
    except (AttributeError, OSError) as exc:
        raise GitSyncError("OneDrive could not make this online-only file available. Choose Always keep on this device in File Explorer, then retry.") from exc
    finally:
        close_handle(handle)


def discard_change(repository, path):
    """Discard all staged and unstaged changes for exactly one current file."""
    root = repository_root(repository.path)
    with _open(root) as repo:
        if _merge_head(repo):
            raise GitSyncError("Resolve or abort the current merge before discarding files.")
        changes = _changes(repo)
        conflict_paths = {change["path"] for change in changes if change["conflict"]}
        if path in conflict_paths:
            raise GitSyncError("Use Keep current or Use incoming to resolve this conflict.")
        changed_paths = {change["path"] for change in changes if not change["conflict"]}
        path, target = _safe_changed_path(root, path, changed_paths)
        tree_path = _tree_path(path)
        try:
            head = _head(repo)
            head_commit = repo[head.encode("ascii")] if head else None
            head_tree = repo[head_commit.tree] if head_commit else None
            head_entry = head_tree.lookup_path(repo.object_store.__getitem__, tree_path) if head_tree else None
        except KeyError:
            head_entry = None

        if head_entry is not None:
            try:
                _hydrate_cloud_placeholder(target)
                porcelain.checkout(repo, paths=[tree_path])
            except Exception as exc:
                raise GitSyncError("Could not restore this file from HEAD. No other files were changed.") from exc
        else:
            try:
                info = target.lstat()
            except FileNotFoundError:
                info = None
            if info is not None:
                if stat.S_ISDIR(info.st_mode):
                    raise GitSyncError("Refusing to remove a directory; discard its changed files individually.")
                try:
                    target.unlink()
                except OSError as exc:
                    raise GitSyncError("Could not remove this untracked file. No other files were changed.") from exc
            index = repo.open_index(config=repo.get_config_stack())
            if tree_path in index:
                del index[tree_path]
                index.write()
    return path


def _unmerged_stage_entry(repo, path, stage):
    """(mode, sha) of the ours (2) / theirs (3) version of a conflicted path, or None."""
    try:
        entry = repo.open_index()[_tree_path(path)]
    except KeyError:
        return None
    if not isinstance(entry, ConflictedIndexEntry):
        return None
    side = entry.this if stage == 2 else entry.other
    if side is None:
        return None
    return side.mode, side.sha


def _blob_data(repo, sha):
    try:
        return repo.object_store[sha].data
    except KeyError:
        return None


def _conflict_version_preview(repo, path, stage):
    entry = _unmerged_stage_entry(repo, path, stage)
    if entry is None:
        return {"kind": "deleted", "text": "Deleted in this version."}
    mode, sha = entry
    if stat.S_ISLNK(mode):
        return {"kind": "unavailable", "text": "Symlink versions are not previewed. Review this path in your local Git client."}
    if not stat.S_ISREG(mode):
        return {"kind": "unavailable", "text": "This file type is not previewed. Review this path in your local Git client."}
    data = _blob_data(repo, sha)
    if data is None:
        return {"kind": "unavailable", "text": "This version could not be previewed. Review this path in your local Git client."}
    if len(data) > MAX_CONFLICT_PREVIEW_BYTES:
        return {"kind": "large", "text": "This version is too large to preview here. Review this path in your local Git client."}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"kind": "binary", "text": "Binary versions are not previewed. Review this path in your local Git client."}
    if any(byte < 32 and byte not in (9, 10, 13) for byte in data):
        return {"kind": "binary", "text": "Binary versions are not previewed. Review this path in your local Git client."}
    return {"kind": "text", "text": text}


def _conflict_preview(repo, path):
    return {
        "ours": _conflict_version_preview(repo, path, 2),
        "theirs": _conflict_version_preview(repo, path, 3),
    }


def _stage(repo, paths, failure_message):
    """Stage ``paths`` (adds, modifications and deletions alike)."""
    try:
        repo.get_worktree().stage([_tree_path(path) for path in paths])
    except Exception as exc:  # noqa: BLE001
        raise GitSyncError(failure_message) from exc


def resolve_conflicts(repository, paths, choice):
    root = repository_root(repository.path)
    with _open(root) as repo:
        if not _merge_head(repo):
            raise GitSyncError("There is no merge in progress to resolve.")
        if choice not in ("ours", "theirs"):
            raise GitSyncError("Choose either the current branch version or the incoming version.")
        conflict_paths = set(_conflicts(repo))
        selected = list(dict.fromkeys(paths or []))
        if not selected:
            raise GitSyncError("Select one or more currently conflicted files.")
        stage = 2 if choice == "ours" else 3
        normalizer = repo.get_blob_normalizer(config=repo.get_config_stack())
        for path in selected:
            _safe_conflict_path(root, path, conflict_paths)
            entry = _unmerged_stage_entry(repo, path, stage)
            if entry is not None and stat.S_ISLNK(entry[0]):
                raise GitSyncError("Git Sync cannot select a symlink conflict version. Resolve that path in your Git client.")
            target = os.path.join(root, *PurePosixPath(path).parts)
            if entry is None:
                try:
                    os.remove(target)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise GitSyncError("Git could not apply the selected deletion. Review the repository status.") from exc
                _stage(repo, [path], "Git could not stage the selected conflict resolution. Review the repository status.")
                continue
            mode, sha = entry
            blob = repo.object_store[sha]
            if normalizer is not None:
                blob = normalizer.checkout_normalize(blob, _tree_path(path))
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as handle:
                    handle.write(blob.data)
                if stat.S_IMODE(mode) & stat.S_IXUSR:
                    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError as exc:
                raise GitSyncError("Git could not apply the selected conflict version. Review the repository status.") from exc
            _safe_conflict_path(root, path, conflict_paths)
            _stage(repo, [path], "Git could not stage the selected conflict resolution. Review the repository status.")


def abort_merge(repository):
    root = repository_root(repository.path)
    with _open(root) as repo:
        if not _merge_head(repo):
            raise GitSyncError("There is no merge in progress to abort.")
        changes = _changes(repo)
        if any(change["status"] == "??" for change in changes):
            raise GitSyncError("Abort is unavailable while untracked files exist. Move or remove them outside Git Sync first.")
        if any(not change["conflict"] and change["status"][1] not in (" ", "?") for change in changes):
            raise GitSyncError("Abort is unavailable while unstaged changes exist. Preserve or review them outside Git Sync first.")
        try:
            porcelain.reset(repo, "hard", b"HEAD")
        except Exception as exc:  # noqa: BLE001
            raise GitSyncError("Git could not abort the merge. Review the repository status before retrying.") from exc
        _clear_merge_state(repo)


def _merge_message(repo):
    try:
        with open(_state_file(repo, "MERGE_MSG"), "rb") as handle:
            message = handle.read().strip()
    except OSError:
        message = b""
    return message or b"Merge upstream changes"


def _commit(repo, message, identity, failure_message):
    try:
        commit_id = porcelain.commit(repo, message=message, author=identity, committer=identity,
                                     no_verify=True, sign=False)
    except Exception as exc:  # noqa: BLE001
        raise GitSyncError(failure_message) from exc
    return commit_id.decode("ascii")[:7]


def complete_merge(repository, acknowledge_truncated=False):
    root = repository_root(repository.path)
    with _open(root) as repo:
        if not _merge_head(repo):
            raise GitSyncError("There is no merge in progress to complete.")
        merge_diff = _diff(repo, commit=_head(repo))
        if len(merge_diff) > MAX_DIFF_CHARS and not acknowledge_truncated:
            raise GitSyncError("The merge diff is truncated. Review the warning and explicitly acknowledge it before completing the merge.")
        changes = _changes(repo)
        if any(change["conflict"] for change in changes):
            raise GitSyncError("Resolve every listed conflict before completing the merge.")
        if any(change["status"][1] != " " or change["status"] == "??" for change in changes):
            raise GitSyncError("The working tree has unstaged or untracked changes. Review them before completing the merge.")
        identity = _identity(repository, "completing the merge")
        commit_id = _commit(
            repo, _merge_message(repo), identity,
            "Git could not create the merge commit. Review repository status before retrying.")
        _clear_merge_state(repo)
        return commit_id


def commit_selected(repository, paths, message):
    root = repository_root(repository.path)
    message = (message or "").strip()
    if not message:
        raise GitSyncError("Enter a non-empty commit message.")
    if len(message) > 5000:
        raise GitSyncError("Commit messages must not exceed 5000 characters.")

    with _open(root) as repo:
        changes = _changes(repo)
        if any(change["conflict"] for change in changes):
            raise GitSyncError("Resolve all merge conflicts before creating a commit.")
        if _merge_head(repo):
            raise GitSyncError("Complete or abort the current merge before creating a commit.")
        if any(change["status"][0] not in (" ", "?") for change in changes):
            raise GitSyncError("Commit unavailable because files are already staged. Unstage them before using Git Sync.")
        if not changes:
            raise GitSyncError("There are no changed files to commit.")

        changed_paths = {change["path"] for change in changes}
        selected = list(dict.fromkeys(paths or []))
        if not selected:
            raise GitSyncError("Select one or more currently changed files.")
        if any(not isinstance(path, str) or path not in changed_paths for path in selected):
            raise GitSyncError("The selected file list is stale or invalid. Refresh and select currently changed files.")

        identity = _identity(repository, "committing")
        _stage(repo, selected,
               "Git could not stage all selected files. Some files may now be staged; review repository status before retrying.")
        return _commit(
            repo, message, identity,
            "Git could not create the local commit. The selected files may remain staged; review repository status before retrying.")
