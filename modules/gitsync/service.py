"""Inspect local Git state and manage explicit remote operations safely."""

import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit, urlunsplit

from app.extensions import db
from app.models import Drive, StoredFile
from app.services import sync_service

from .models import Repository

MAX_DIFF_CHARS = 200_000
MAX_CONFLICT_PREVIEW_BYTES = 64_000
REMOTE_TIMEOUT = 60


class GitSyncError(ValueError):
    pass


def _run(path, args, optional=False, failure_message=None, timeout_message=None):
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
               GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(
            ["git", "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false",
             "-C", str(path), *args],
            capture_output=True, env=env, timeout=20, check=False,
        )
    except FileNotFoundError as exc:
        raise GitSyncError("Git is not installed or the repository folder is missing.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitSyncError(timeout_message or "Git took too long. No repository changes were made.") from exc
    if result.returncode and not optional:
        raise GitSyncError(failure_message or "Git could not inspect this repository. Check its path, permissions and ownership.")
    return result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else ""


def _run_remote(path, args, account, failure_message, timeout_message):
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
               GIT_OPTIONAL_LOCKS="0")
    command = ["git", "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false"]
    if account:
        command.extend(["-c", f"credential.username={account}"])
    command.extend(["-C", str(path), *args])
    try:
        result = subprocess.run(
            command, capture_output=True, env=env, timeout=REMOTE_TIMEOUT, check=False,
        )
    except FileNotFoundError as exc:
        raise GitSyncError("Git is not installed or the repository folder is missing.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitSyncError(timeout_message) from exc
    if result.returncode:
        stderr = result.stderr.decode("utf-8", errors="replace").lower()
        if any(term in stderr for term in (
                "authentication failed", "authentication required", "permission denied",
                "access denied", "repository not found", "http 401", "http 403")):
            message = "Remote access was denied. Check the server account's Git credentials and repository access."
        elif any(term in stderr for term in (
                "non-fast-forward", "fetch first", "protected branch", "remote rejected",
                "pre-receive hook declined", "gh006")):
            message = "The remote rejected this push. Fetch and review upstream changes, and check branch protection requirements."
        elif any(term in stderr for term in (
                "could not resolve", "failed to connect", "connection refused",
                "network is unreachable", "remote end hung up", "http 502", "http 503", "http 504")):
            message = "The remote is unavailable. Check network and remote status, then try again."
        else:
            message = failure_message
        raise GitSyncError(message)


def _tracking(root):
    branch = _run(root, ["symbolic-ref", "--quiet", "--short", "HEAD"], optional=True).strip()
    if not branch:
        return None
    value = _run(
        root,
        ["for-each-ref", "--format=%(upstream)%00%(upstream:remotename)%00%(upstream:remoteref)",
         f"refs/heads/{branch}"],
        optional=True,
    ).strip()
    fields = value.split("\0")
    if len(fields) != 3:
        return None
    upstream_ref, remote, merge_ref = fields
    if (not upstream_ref.startswith("refs/remotes/") or not remote or remote == "."
            or not merge_ref.startswith("refs/heads/")):
        return None
    if not _run(root, ["remote", "get-url", remote], optional=True).strip():
        return None
    upstream_head = _run(root, ["rev-parse", "--verify", "--quiet", f"{upstream_ref}^{{commit}}"], optional=True).strip()
    return {
        "branch": branch,
        "remote": remote,
        "merge_ref": merge_ref,
        "upstream_ref": upstream_ref,
        "upstream": _run(root, ["rev-parse", "--abbrev-ref", upstream_ref], optional=True).strip(),
        "upstream_head": upstream_head,
    }


def _merge_head(root):
    return _run(root, ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"], optional=True).strip()


def _ahead_behind(root, upstream_ref):
    if not upstream_ref:
        return None
    counts = _run(root, ["rev-list", "--left-right", "--count", f"HEAD...{upstream_ref}"], optional=True).strip()
    try:
        ahead, behind = (int(value) for value in counts.split())
    except (ValueError, TypeError):
        return None
    return {"ahead": ahead, "behind": behind}


def _require_tracking(root):
    tracking = _tracking(root)
    if tracking is None:
        raise GitSyncError("This branch has no valid remote upstream. Configure an upstream branch first.")
    if not tracking["upstream_head"]:
        raise GitSyncError("The upstream has no fetched commit. Fetch the remote and compare before continuing.")
    return tracking


def _require_clean(root):
    changes = _changes(root)
    if any(change["conflict"] for change in changes):
        raise GitSyncError("Resolve or abort the current merge before continuing.")
    if changes:
        raise GitSyncError("This action requires a clean working tree. Commit or safely set aside local changes first.")


def repository_root(path):
    normalized, error = sync_service.validate_path(path)
    if error:
        raise GitSyncError(error)
    root = _run(normalized, ["rev-parse", "--show-toplevel"]).strip()
    if os.path.normcase(os.path.realpath(root)) != os.path.normcase(normalized):
        raise GitSyncError("Choose the repository root folder, not a subfolder.")
    return normalized


def github_accounts():
    try:
        result = subprocess.run(
            ["git", "credential-manager", "github", "list"],
            capture_output=True, text=True, timeout=5, check=False,
            env={**os.environ, "GCM_INTERACTIVE": "never"},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    return sorted({line.strip() for line in result.stdout.splitlines()
                   if line.strip() and all(char.isalnum() or char == "-" for char in line.strip())})


def connect(user, path, name, account):
    root = repository_root(path)
    existing = Repository.query.filter_by(user_id=user.id, path=root).first()
    if existing is not None:
        if existing.drive is not None:
            raise GitSyncError("This repository is already connected.")
    if len(name) > 120 or len(account) > 120 or any(ord(char) < 32 or ord(char) == 127 for char in account):
        raise GitSyncError("Names and account names must not exceed 120 characters.")
    drive = Drive.query.filter_by(user_id=user.id, source_path=root).first()
    if drive and (drive.deleted_at is not None or not drive.is_readonly):
        raise GitSyncError("This folder already belongs to a writable or trashed drive.")
    if drive is None:
        drive_name = name or Path(root).name
        candidate, suffix = drive_name, 2
        while Drive.query.filter(Drive.user_id == user.id, Drive.name.ilike(candidate)).first():
            candidate = f"{drive_name} ({suffix})"
            suffix += 1
        drive = Drive(name=candidate, user_id=user.id, source_path=root,
                      sync_mode="mirror", captions_enabled=False,
                      description="Local repository linked by Git Sync")
        db.session.add(drive)
        db.session.flush()
    if existing is not None:
        db.session.delete(existing)
        db.session.flush()
    repository = Repository(
        user_id=user.id, drive_id=drive.id, name=name or Path(root).name,
        path=root, account=account,
        baseline=_run(root, ["rev-parse", "--verify", "HEAD"], optional=True).strip() or None,
    )
    db.session.add(repository)
    db.session.commit()
    return repository


def reconnect_drive(user, repository):
    root = repository_root(repository.path)
    if os.path.normcase(os.path.realpath(repository.path)) != os.path.normcase(root):
        raise GitSyncError("The saved path no longer resolves to the same repository folder.")

    def same_path(value):
        return bool(value) and os.path.normcase(os.path.realpath(value)) == os.path.normcase(root)

    matching_drives = [
        drive for drive in Drive.query.filter(Drive.source_path.isnot(None)).all()
        if same_path(drive.source_path)
    ]
    linked_drive = repository.drive
    if linked_drive is not None:
        if linked_drive.user_id != user.id:
            raise GitSyncError("The linked drive belongs to another user.")
        if not same_path(linked_drive.source_path):
            raise GitSyncError("The linked drive does not match this repository folder.")

    for drive in matching_drives:
        if drive.user_id != user.id:
            raise GitSyncError("This repository folder is already linked to another user's drive.")
        if (drive.source_kind != "folder" or drive.repo_url is not None
                or not drive.is_readonly):
            raise GitSyncError("This folder belongs to a writable, shared, or remote drive.")

    if linked_drive is not None and (
            linked_drive.source_kind != "folder" or linked_drive.repo_url is not None
            or not linked_drive.is_readonly):
        raise GitSyncError("The linked drive is not a read-only local mirror.")

    live_drive = next((drive for drive in matching_drives
                       if drive.user_id == user.id and drive.deleted_at is None), None)
    drive = live_drive
    restore_timestamp = None
    if drive is None and linked_drive is not None and linked_drive.deleted_at is not None:
        drive = linked_drive
        restore_timestamp = drive.deleted_at
    elif drive is None:
        drive_name = repository.name or Path(root).name
        candidate, suffix = drive_name, 2
        while Drive.query.filter(Drive.user_id == user.id,
                                 Drive.name.ilike(candidate)).first():
            candidate = f"{drive_name} ({suffix})"
            suffix += 1
        drive = Drive(name=candidate, user_id=user.id, source_path=root,
                      sync_mode="mirror", captions_enabled=False,
                      description="Local repository linked by Git Sync")
        db.session.add(drive)

    try:
        if restore_timestamp is not None:
            drive.deleted_at = None
            files = (StoredFile.query.filter_by(
                drive_id=drive.id, user_id=user.id, deleted_at=restore_timestamp).all())
            from app.services import search_service
            for stored_file in files:
                stored_file.deleted_at = None
                search_service.fts_upsert(stored_file.id, commit=False)
        db.session.flush()
        repository.drive_id = drive.id
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        raise GitSyncError("Could not reconnect the repository drive. No changes were saved.") from exc
    return drive


def disconnect(repository):
    db.session.delete(repository)
    db.session.commit()


def _remote_label(value):
    if "://" in value:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    return value.split("@", 1)[-1]


def _changes(root):
    records = _run(root, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"]).split("\0")
    changes = []
    cursor = 0
    while cursor < len(records):
        record = records[cursor]
        cursor += 1
        if not record:
            continue
        status, path = record[:2], record[3:]
        original = None
        if "R" in status or "C" in status:
            original = records[cursor]
            cursor += 1
        changes.append({"status": status, "path": path, "original": original,
                        "conflict": status in ("DD", "AU", "UD", "UA", "DU", "AA", "UU")})
    return changes


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


def inspect(repository, basis="head"):
    root = repository_root(repository.path)
    head = _run(root, ["rev-parse", "--verify", "HEAD"], optional=True).strip()
    reference = repository.baseline if basis == "initial" else head
    if basis == "initial" and not reference:
        raise GitSyncError("This repository had no commit when it was connected.")
    changes = _changes(root)
    conflict_count = sum(change["conflict"] for change in changes)
    staged_count = sum(change["status"][0] not in (" ", "?") for change in changes)
    merge_unstaged_count = sum(
        not change["conflict"] and change["status"][1] not in (" ", "?")
        for change in changes
    )
    merge_untracked_count = sum(change["status"] == "??" for change in changes)
    tracking = _tracking(root)
    merge_head = _merge_head(root)
    diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    if reference:
        diff_args.append(reference)
    diff = _run(root, [*diff_args, "--"])
    staged = ""
    if not head:
        staged = _run(root, ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color", "--"])
    diff = diff + staged
    merge_diff = ""
    if merge_head:
        merge_diff = diff if basis == "head" else _run(
            root, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "HEAD", "--"])
    diff_truncated = len(diff) > MAX_DIFF_CHARS
    merge_diff_truncated = len(merge_diff) > MAX_DIFF_CHARS
    visible_diff = diff[:MAX_DIFF_CHARS]
    visible_merge_diff = merge_diff[:MAX_DIFF_CHARS]
    return {
        "branch": _run(root, ["symbolic-ref", "--short", "HEAD"], optional=True).strip() or "Detached HEAD",
        "head": head, "baseline": repository.baseline,
        "remote": _remote_label(_run(root, ["remote", "get-url", "origin"], optional=True).strip()),
        "author_name": _run(root, ["config", "--local", "--get", "user.name"], optional=True).strip(),
        "author_email": _run(root, ["config", "--local", "--get", "user.email"], optional=True).strip(),
        "upstream": tracking["upstream"] if tracking else "",
        "ahead_behind": _ahead_behind(root, tracking["upstream_ref"]) if tracking else None,
        "has_upstream_commit": bool(tracking and tracking["upstream_head"]),
        "merge_in_progress": bool(merge_head),
        "conflicts": [
            {**change, "preview": _conflict_preview(root, change["path"])}
            for change in changes if change["conflict"]
        ],
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


def check_remote_access(repository):
    root = repository_root(repository.path)
    if not _run(root, ["remote", "get-url", "origin"], optional=True).strip():
        raise GitSyncError("No origin remote is configured for this repository.")
    _run_remote(
        root, ["ls-remote", "origin"], repository.account,
        "Remote read access failed. Check the origin URL and the Git credentials available to this server account.",
        "The remote read check timed out. Check remote availability and try again.",
    )
    return bool(repository.account)


def fetch(repository):
    root = repository_root(repository.path)
    tracking = _tracking(root)
    if tracking is None:
        raise GitSyncError("This branch has no valid remote upstream to fetch.")
    _run_remote(
        root, ["fetch", "--no-tags", tracking["remote"]], repository.account,
        "Fetch failed. Check the upstream URL, Git credentials and repository access.",
        "Fetch timed out. Check the repository status before trying again.",
    )


def push(repository):
    root = repository_root(repository.path)
    if _merge_head(root):
        raise GitSyncError("Complete or abort the current merge before pushing.")
    _require_clean(root)
    tracking = _require_tracking(root)
    counts = _ahead_behind(root, tracking["upstream_ref"])
    if not counts or counts["ahead"] == 0:
        raise GitSyncError("There are no local commits ahead of the configured upstream to push.")
    if counts["behind"]:
        raise GitSyncError("The local branch is behind or diverged. Fetch and review the remote before pushing.")
    _run_remote(
        root, ["push", tracking["remote"], f"HEAD:{tracking['merge_ref']}"], repository.account,
        "Push failed. Check remote write access and fetch/review the upstream before retrying.",
        "Push timed out. Check the remote and local branch status before retrying.",
    )


def start_merge(repository):
    root = repository_root(repository.path)
    if _merge_head(root):
        raise GitSyncError("Complete or abort the current merge before starting another merge.")
    _require_clean(root)
    tracking = _require_tracking(root)
    counts = _ahead_behind(root, tracking["upstream_ref"])
    if not counts or counts["behind"] == 0:
        raise GitSyncError("There are no fetched upstream commits to merge.")
    _run(
        root, ["merge", "--no-ff", "--no-commit", tracking["upstream_ref"]], optional=True,
        failure_message="Git could not start the merge. Review the local repository status before retrying.",
        timeout_message="Git took too long starting the merge. Check repository status before retrying.",
    )
    if not _merge_head(root):
        raise GitSyncError("Git could not start the merge. Review the local repository status before retrying.")


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


def _unmerged_stage_mode(root, path, stage):
    entry = _unmerged_stage_entry(root, path, stage)
    return entry[0] if entry else None


def _unmerged_stage_entry(root, path, stage):
    records = _run(root, ["ls-files", "-u", "-z", "--", path]).split("\0")
    for record in records:
        if not record:
            continue
        metadata, record_path = record.split("\t", 1)
        mode, object_id, record_stage = metadata.split()
        if record_path == path and record_stage == str(stage):
            return mode, object_id
    return None


def _conflict_version_preview(root, path, stage):
    entry = _unmerged_stage_entry(root, path, stage)
    if entry is None:
        return {"kind": "deleted", "text": "Deleted in this version."}
    mode, object_id = entry
    if mode == "120000":
        return {"kind": "unavailable", "text": "Symlink versions are not previewed. Review this path in your local Git client."}
    if mode not in ("100644", "100755"):
        return {"kind": "unavailable", "text": "This file type is not previewed. Review this path in your local Git client."}
    size = _run(root, ["cat-file", "-s", object_id], optional=True).strip()
    if not size.isdigit():
        return {"kind": "unavailable", "text": "This version could not be previewed. Review this path in your local Git client."}
    if int(size) > MAX_CONFLICT_PREVIEW_BYTES:
        return {"kind": "large", "text": "This version is too large to preview here. Review this path in your local Git client."}
    try:
        result = subprocess.run(
            ["git", "--no-pager", "-C", str(root), "cat-file", "blob", object_id],
            capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"kind": "unavailable", "text": "This version could not be previewed. Review this path in your local Git client."}
    if result.returncode:
        return {"kind": "unavailable", "text": "This version could not be previewed. Review this path in your local Git client."}
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return {"kind": "binary", "text": "Binary versions are not previewed. Review this path in your local Git client."}
    if any(byte < 32 and byte not in (9, 10, 13) for byte in result.stdout):
        return {"kind": "binary", "text": "Binary versions are not previewed. Review this path in your local Git client."}
    return {"kind": "text", "text": text}


def _conflict_preview(root, path):
    return {
        "ours": _conflict_version_preview(root, path, 2),
        "theirs": _conflict_version_preview(root, path, 3),
    }


def resolve_conflicts(repository, paths, choice):
    root = repository_root(repository.path)
    if not _merge_head(root):
        raise GitSyncError("There is no merge in progress to resolve.")
    if choice not in ("ours", "theirs"):
        raise GitSyncError("Choose either the current branch version or the incoming version.")
    conflicts = [change for change in _changes(root) if change["conflict"]]
    conflict_paths = {change["path"] for change in conflicts}
    selected = list(dict.fromkeys(paths or []))
    if not selected:
        raise GitSyncError("Select one or more currently conflicted files.")
    stage = 2 if choice == "ours" else 3
    for path in selected:
        _safe_conflict_path(root, path, conflict_paths)
        mode = _unmerged_stage_mode(root, path, stage)
        if mode == "120000":
            raise GitSyncError("Git Sync cannot select a symlink conflict version. Resolve that path in your Git client.")
        if mode is None:
            _run(root, ["rm", "-f", "--", path],
                 failure_message="Git could not apply the selected deletion. Review the repository status.")
        else:
            _run(root, ["checkout", f"--{choice}", "--", path],
                 failure_message="Git could not apply the selected conflict version. Review the repository status.")
            _safe_conflict_path(root, path, conflict_paths)
            _run(root, ["add", "-A", "--", path],
                 failure_message="Git could not stage the selected conflict resolution. Review the repository status.")


def abort_merge(repository):
    root = repository_root(repository.path)
    if not _merge_head(root):
        raise GitSyncError("There is no merge in progress to abort.")
    changes = _changes(root)
    if any(change["status"] == "??" for change in changes):
        raise GitSyncError("Abort is unavailable while untracked files exist. Move or remove them outside Git Sync first.")
    if any(not change["conflict"] and change["status"][1] not in (" ", "?") for change in changes):
        raise GitSyncError("Abort is unavailable while unstaged changes exist. Preserve or review them outside Git Sync first.")
    _run(root, ["merge", "--abort"],
         failure_message="Git could not abort the merge. Review the repository status before retrying.",
         timeout_message="Git took too long aborting the merge. Check repository status before retrying.")


def complete_merge(repository, acknowledge_truncated=False):
    root = repository_root(repository.path)
    if not _merge_head(root):
        raise GitSyncError("There is no merge in progress to complete.")
    merge_diff = _run(root, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "HEAD", "--"])
    if len(merge_diff) > MAX_DIFF_CHARS and not acknowledge_truncated:
        raise GitSyncError("The merge diff is truncated. Review the warning and explicitly acknowledge it before completing the merge.")
    changes = _changes(root)
    if any(change["conflict"] for change in changes):
        raise GitSyncError("Resolve every listed conflict before completing the merge.")
    if any(change["status"][1] != " " or change["status"] == "??" for change in changes):
        raise GitSyncError("The working tree has unstaged or untracked changes. Review them before completing the merge.")
    if not _run(root, ["config", "--local", "--get", "user.name"], optional=True).strip():
        raise GitSyncError("Configure user.name in this repository's local Git settings before completing the merge.")
    if not _run(root, ["config", "--local", "--get", "user.email"], optional=True).strip():
        raise GitSyncError("Configure user.email in this repository's local Git settings before completing the merge.")
    with tempfile.TemporaryDirectory(prefix="docindex-gitsync-hooks-") as hooks_path:
        _run(
            root, ["-c", f"core.hooksPath={hooks_path}", "-c", "commit.gpgsign=false",
                   "commit", "--no-edit"],
            failure_message="Git could not create the merge commit. Review repository status before retrying.",
            timeout_message="Git took too long completing the merge. Check repository status and HEAD before retrying.",
        )
    return _run(root, ["rev-parse", "--short", "HEAD"]).strip()


def commit_selected(repository, paths, message):
    root = repository_root(repository.path)
    message = (message or "").strip()
    if not message:
        raise GitSyncError("Enter a non-empty commit message.")
    if len(message) > 5000:
        raise GitSyncError("Commit messages must not exceed 5000 characters.")

    changes = _changes(root)
    if any(change["conflict"] for change in changes):
        raise GitSyncError("Resolve all merge conflicts before creating a commit.")
    if any(change["status"][0] not in (" ", "?") for change in changes):
        raise GitSyncError("Commit unavailable because files are already staged. Unstage them before using Git Sync.")
    if not changes:
        raise GitSyncError("There are no changed files to commit.")

    changed_paths = {change["path"] for change in changes}
    changed_paths.update(change["original"] for change in changes if change["original"])
    selected = list(dict.fromkeys(paths or []))
    if not selected:
        raise GitSyncError("Select one or more currently changed files.")
    if any(not isinstance(path, str) or path not in changed_paths for path in selected):
        raise GitSyncError("The selected file list is stale or invalid. Refresh and select currently changed files.")

    if not _run(root, ["config", "--local", "--get", "user.name"], optional=True).strip():
        raise GitSyncError("Configure user.name in this repository's local Git settings before committing.")
    if not _run(root, ["config", "--local", "--get", "user.email"], optional=True).strip():
        raise GitSyncError("Configure user.email in this repository's local Git settings before committing.")

    _run(
        root, ["add", "-A", "--", *selected],
        failure_message="Git could not stage all selected files. Some files may now be staged; review repository status before retrying.",
        timeout_message="Git took too long while staging. Some files may now be staged; review repository status before retrying.",
    )
    with tempfile.TemporaryDirectory(prefix="docindex-gitsync-hooks-") as hooks_path:
        _run(
            root, ["-c", f"core.hooksPath={hooks_path}", "-c", "commit.gpgsign=false",
                   "commit", "-m", message],
            failure_message="Git could not create the local commit. The selected files may remain staged; review repository status before retrying.",
            timeout_message="Git took too long while creating the local commit. Check repository status and HEAD before retrying.",
        )
    return _run(
        root, ["rev-parse", "--short", "HEAD"],
        timeout_message="Git took too long after commit creation. Check repository HEAD before retrying.",
    ).strip()