"""Update the app's own checkout with dulwich (no git binary required).

Usage:
    venv/Scripts/python.exe tools/git_sync.py pull

Mirrors `git pull --ff-only` on the current branch's upstream: aborts on a
dirty tree, a detached HEAD, a missing upstream, or a diverged branch
instead of creating surprise merges. Prints what it did, including the
new commits when it fast-forwards. Exit 0 = up to date or updated,
1 = anything needs manual attention (message says what).
"""

import os
import sys


def _fail(msg):
    print(f"git-sync: ERROR: {msg}")
    print("git-sync: resolve it manually (or run `git pull`), then re-run the update.")
    sys.exit(1)


def _short(sha):
    return sha.decode()[:12] if isinstance(sha, bytes) else str(sha)[:12]


def _blob_at_head(repo, head, path):
    """Raw blob bytes for a HEAD-tracked path, or None (new/unreadable)."""
    if isinstance(path, str):
        path = os.fsencode(path)
    try:
        commit = repo[head]
        tree = repo[commit.tree]
        _mode, sha = tree.lookup_path(repo.object_store.__getitem__, path)
        data = getattr(repo.object_store[sha], "data", None)
        return data if isinstance(data, bytes) else None
    except (KeyError, AttributeError, ValueError, TypeError):
        return None


def _split_line_ending_only(repo, head, root, dirty):
    """Split dulwich-flagged paths into (phantom, real).

    dulwich compares raw bytes while real git honors core.autocrlf, so on
    Windows every CRLF checkout looks dirty to dulwich (and `git status`
    says clean). A path is phantom when the worktree file matches its HEAD
    blob up to CRLF — normalizing it to the blob bytes is content-neutral
    and unblocks the fast-forward. Anything else stays abort-worthy.
    """
    phantom, real = [], []
    for path in dirty:
        try:
            with open(os.path.join(root, os.fsdecode(path)), "rb") as fh:
                current = fh.read()
        except OSError:
            real.append(path)  # deleted/unreadable: still abort
            continue
        blob = _blob_at_head(repo, head, path)
        if blob is None:
            real.append(path)  # not tracked at HEAD: still abort
            continue
        if current.replace(b"\r\n", b"\n") == blob.replace(b"\r\n", b"\n"):
            phantom.append(path)
        else:
            real.append(path)
    return phantom, real


def _clean(text):
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in text)


def main():
    if len(sys.argv) != 2 or sys.argv[1] != "pull":
        print("\n".join(__doc__.strip().splitlines()[2:5]))
        return 2
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return pull_repo(root)


def pull_repo(root):
    try:
        from dulwich import porcelain
        from dulwich.repo import NotGitRepository, Repo
    except ImportError:
        _fail("dulwich is not installed in this venv "
              "(it is in requirements.txt — re-run the dependency step).")

    try:
        repo = Repo(root)
    except NotGitRepository:
        _fail(f"{root} is not a git repository.")

    # Current branch (detached HEAD has nothing to pull into).
    try:
        symref = repo.refs.read_ref(b"HEAD")
    except KeyError:
        _fail("HEAD is unborn (no commits checked out).")
    if not symref.startswith(b"ref: refs/heads/"):
        _fail(f"detached HEAD at {_short(symref)} — "
              f"check out a branch first.")
    head = symref[len(b"ref: "):]
    branch = head[len(b"refs/heads/"):].decode()
    print(f"git-sync: on branch {branch}")

    # Upstream, like `git pull` requires it — no silent guessing.
    cfg = repo.get_config()
    bbranch = branch.encode()
    try:
        remote = cfg.get((b"branch", bbranch), b"remote").decode()
    except KeyError:
        remote = "origin"
    try:
        merge_ref = cfg.get((b"branch", bbranch), b"merge").decode()
    except KeyError:
        _fail(f"branch '{branch}' has no upstream configured "
              f"(like `git pull` would: no tracking information). "
              f"Check out the release branch or set an upstream first.")
    try:
        url = cfg.get((b"remote", remote.encode()), b"url").decode()
    except KeyError:
        _fail(f"remote '{remote}' has no URL configured.")
    print(f"git-sync: pulling {merge_ref} from {remote} ({url})")

    # Dirty tree: `git pull` would refuse (or worse, silently succeed
    # halfway) — abort with the file list instead. Paths that differ from
    # HEAD only by CRLF/LF are normalized first (Windows checkouts with
    # core.autocrlf look dirty to dulwich's raw-byte comparison while
    # `git status` says clean); only real content differences abort.
    status = porcelain.status(repo)
    dirty = sorted(set(status.staged["add"] + status.staged["modify"]
                       + status.unstaged))
    if dirty:
        phantom, dirty = _split_line_ending_only(repo, head, root, dirty)
        for path in phantom:
            print(f"git-sync: normalized line endings in "
                  f"{_clean(path)} (matches HEAD up to CRLF).")
            blob = _blob_at_head(repo, head, path)
            if blob is not None:
                with open(os.path.join(root, os.fsdecode(path)),
                          "wb") as fh:
                    fh.write(blob)
    if dirty:
        shown = ", ".join(_clean(f) for f in dirty[:10])
        more = f" (+{len(dirty) - 10} more)" if len(dirty) > 10 else ""
        _fail(f"working tree has local changes: {shown}{more}. "
              f"Commit or stash them first.")

    before = repo.refs[head]
    print(f"git-sync: fetching... (local tip {_short(before)})")
    try:
        porcelain.pull(repo, url, refspecs=merge_ref, ff_only=True,
                       quiet=False)
    except Exception as exc:  # noqa: BLE001 - report any transport/merge failure
        name = type(exc).__name__
        if "DivergedBranches" in name:
            _fail(f"local '{branch}' diverged from {remote} — "
                  f"automatic fast-forward refused. Rebase or merge manually.")
        _fail(f"pull failed ({name}): "
              f"{_clean(str(exc).strip().splitlines()[0][:200] if str(exc) else '')}")
    after = Repo(root).refs[head]
    if after == before:
        print(f"git-sync: already up to date at {_short(after)}.")
        return 0

    # Report the new commits (walker is lazy; cap the scan).
    new, walked = [], 0
    for entry in repo.get_walker(include=[after], exclude=[before]):
        walked += 1
        if len(new) < 10:
            msg = entry.commit.message.decode(errors="replace").splitlines()
            new.append(f"  {_short(entry.commit.id)} "
                       f"{_clean(msg[0][:80] if msg else '')}")
        if walked >= 1000:
            break
    extra = f" (+{walked - 10} more)" if walked > 10 else ""
    print(f"git-sync: fast-forwarded {_short(before)} -> {_short(after)} "
          f"({walked} commit{'s' if walked != 1 else ''}):{extra}")
    print("\n".join(new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
