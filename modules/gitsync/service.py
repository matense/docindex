"""Read-only Git inspection; credentials remain with the local Git client."""

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.extensions import db
from app.models import Drive
from app.services import sync_service

from .models import Repository

MAX_CHANGES = 500
MAX_DIFF_CHARS = 200_000


class GitSyncError(ValueError):
    pass


def _run(path, args, optional=False):
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
        raise GitSyncError("Git took too long. No repository changes were made.") from exc
    if result.returncode and not optional:
        raise GitSyncError("Git could not inspect this repository. Check its path, permissions and ownership.")
    return result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else ""


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
    if Repository.query.filter_by(user_id=user.id, path=root).first():
        raise GitSyncError("This repository is already connected.")
    if len(name) > 120 or len(account) > 120:
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
    repository = Repository(
        user_id=user.id, drive_id=drive.id, name=name or Path(root).name,
        path=root, account=account,
        baseline=_run(root, ["rev-parse", "--verify", "HEAD"], optional=True).strip() or None,
    )
    db.session.add(repository)
    db.session.commit()
    return repository


def _remote_label(value):
    if "://" in value:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    return value.split("@", 1)[-1]


def inspect(repository, basis="head"):
    root = repository_root(repository.path)
    head = _run(root, ["rev-parse", "--verify", "HEAD"], optional=True).strip()
    reference = repository.baseline if basis == "initial" else head
    if basis == "initial" and not reference:
        raise GitSyncError("This repository had no commit when it was connected.")
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
    conflict_count = sum(change["conflict"] for change in changes)
    diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    if reference:
        diff_args.append(reference)
    diff = _run(root, [*diff_args, "--"])
    staged = ""
    if not head:
        staged = _run(root, ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color", "--"])
    diff = diff + staged
    return {
        "branch": _run(root, ["symbolic-ref", "--short", "HEAD"], optional=True).strip() or "Detached HEAD",
        "head": head, "baseline": repository.baseline,
        "remote": _remote_label(_run(root, ["remote", "get-url", "origin"], optional=True).strip()),
        "author_name": _run(root, ["config", "--get", "user.name"], optional=True).strip(),
        "author_email": _run(root, ["config", "--get", "user.email"], optional=True).strip(),
        "upstream": _run(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], optional=True).strip(),
        "ahead_behind": _run(root, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], optional=True).strip(),
        "changes": changes[:MAX_CHANGES], "change_count": len(changes),
        "changes_truncated": len(changes) > MAX_CHANGES,
        "conflict_count": conflict_count,
        "diff": diff[:MAX_DIFF_CHARS], "diff_truncated": len(diff) > MAX_DIFF_CHARS,
    }