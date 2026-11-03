"""Updater pull logic without the git binary (dulwich only, local repos)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "tools")))
import git_sync  # noqa: E402
from dulwich import porcelain  # noqa: E402
from dulwich.repo import Repo  # noqa: E402


def _commit(path, name, content, message):
    full = os.path.join(path, name)
    with open(full, "w") as fh:
        fh.write(content)
    porcelain.add(path, name)
    porcelain.commit(path, message=message, author=b"T <t@t>",
                     committer=b"T <t@t>")


@pytest.fixture()
def pair(tmp_path):
    origin = str(tmp_path / "origin")
    os.makedirs(origin)
    porcelain.init(origin)
    _commit(origin, "f.txt", "one\n", b"first")
    clone = str(tmp_path / "clone")
    porcelain.clone(origin, clone)
    repo = Repo(clone)
    cfg = repo.get_config()
    cfg.set((b"branch", b"master"), b"remote", b"origin")
    cfg.set((b"branch", b"master"), b"merge", b"refs/heads/master")
    cfg.write_to_path()
    return origin, clone


def test_pull_noop_when_up_to_date(pair, capsys):
    assert git_sync.pull_repo(pair[1]) == 0
    assert "already up to date" in capsys.readouterr().out


def test_pull_fast_forwards_and_reports(pair, capsys):
    origin, clone = pair
    _commit(origin, "f.txt", "one\ntwo\n", b"second")
    _commit(origin, "n.txt", "new\n", b"third")
    assert git_sync.pull_repo(clone) == 0
    out = capsys.readouterr().out
    assert "fast-forwarded" in out and "2 commits" in out
    with open(os.path.join(clone, "n.txt")) as fh:
        assert fh.read() == "new\n"


def test_pull_aborts_on_dirty_tree(pair, capsys):
    origin, clone = pair
    with open(os.path.join(clone, "f.txt"), "a") as fh:
        fh.write("local dirt\n")
    with pytest.raises(SystemExit) as excinfo:
        git_sync.pull_repo(clone)
    assert excinfo.value.code == 1
    assert "local changes" in capsys.readouterr().out


def test_pull_aborts_on_divergence(pair, capsys):
    origin, clone = pair
    _commit(clone, "local.txt", "l\n", b"local work")
    _commit(origin, "remote.txt", "r\n", b"remote work")
    with pytest.raises(SystemExit) as excinfo:
        git_sync.pull_repo(clone)
    assert excinfo.value.code == 1
    assert "diverged" in capsys.readouterr().out


def test_split_ignores_crlf_only_drift(pair):
    """Windows CRLF checkouts look dirty to raw-byte comparisons while
    `git status` says clean — only real content differences may abort."""
    clone = pair[1]
    repo = Repo(clone)
    head = b"refs/heads/master"
    with open(os.path.join(clone, "f.txt"), "wb") as fh:
        fh.write(b"one\r\n")
    phantom, real = git_sync._split_line_ending_only(
        repo, head, clone, [b"f.txt"])
    assert phantom == [b"f.txt"] and real == []
    with open(os.path.join(clone, "f.txt"), "wb") as fh:
        fh.write(b"one\r\nlocal dirt\r\n")
    phantom, real = git_sync._split_line_ending_only(
        repo, head, clone, [b"f.txt"])
    assert phantom == [] and real == [b"f.txt"]
    phantom, real = git_sync._split_line_ending_only(
        repo, head, clone, [b"missing.txt"])
    assert phantom == [] and real == [b"missing.txt"]


def test_pull_survives_crlf_drift_on_updated_file(pair, capsys):
    """End to end: a CRLF worktree file the remote also changed must
    fast-forward (normalized first) instead of forcing a manual pull."""
    origin, clone = pair
    with open(os.path.join(clone, "f.txt"), "wb") as fh:
        fh.write(b"one\r\n")
    _commit(origin, "f.txt", "one\ntwo\n", b"second")
    assert git_sync.pull_repo(clone) == 0
    assert "fast-forwarded" in capsys.readouterr().out
    with open(os.path.join(clone, "f.txt"), "rb") as fh:
        # dulwich honours core.autocrlf on checkout, so accept the
        # smudged CRLF bytes: what matters is the text content.
        assert fh.read().replace(b"\r\n", b"\n") == b"one\ntwo\n"


def test_pull_aborts_on_detached_head(pair, capsys):
    origin, clone = pair
    repo = Repo(clone)
    head = repo.refs[b"refs/heads/master"]
    with open(os.path.join(clone, ".git", "HEAD"), "w") as fh:
        fh.write(head.decode() + "\n")
    with pytest.raises(SystemExit) as excinfo:
        git_sync.pull_repo(clone)
    assert excinfo.value.code == 1
    assert "detached HEAD" in capsys.readouterr().out


def test_tee_captures_output_and_exit_code(tmp_path):
    import subprocess
    log = str(tmp_path / "run.log")
    cmd = [sys.executable, "-c",
           "import sys; print('out-line'); "
           "print('err-line', file=sys.stderr); sys.exit(3)"]
    proc = subprocess.run(
        [sys.executable, "tools/tee.py", log] + cmd,
        capture_output=True, text=True)
    assert proc.returncode == 3
    assert "out-line" in proc.stdout
    assert "err-line" in proc.stdout  # stderr merged, like `2>&1`
    body = open(log, encoding="utf-8").read()
    assert "out-line" in body and "err-line" in body
    # Second run rotates the previous log aside.
    subprocess.run([sys.executable, "tools/tee.py", log] + cmd,
                   capture_output=True)
    assert "out-line" in open(log + ".prev", encoding="utf-8").read()


def test_tee_forces_unbuffered_child_output(tmp_path):
    """Install/update progress must stream live, not arrive in one burst
    at child exit: Python block-buffers piped stdout, so tee.py exports
    PYTHONUNBUFFERED=1 to every child it spawns."""
    import subprocess
    log = str(tmp_path / "unbuf.log")
    cmd = [sys.executable, "-c",
           "import os; print(os.environ.get('PYTHONUNBUFFERED', ''))"]
    proc = subprocess.run(
        [sys.executable, "tools/tee.py", log] + cmd,
        capture_output=True, text=True)
    assert proc.returncode == 0
    assert "1" in proc.stdout
    assert "1" in open(log, encoding="utf-8").read()


def test_tee_streams_output_before_child_exits(tmp_path):
    """The console must show progress while the child still runs: tee.py
    forwards every chunk as it arrives (read1) instead of blocking for a
    full buffer. The log file gets the same bytes."""
    import subprocess
    log = str(tmp_path / "stream.log")
    child = ("import sys, time; print('first', flush=True); "
             "time.sleep(3); print('second', flush=True)")
    proc = subprocess.Popen(
        [sys.executable, "tools/tee.py", log,
         sys.executable, "-c", child],
        stdout=subprocess.PIPE, text=True, bufsize=1)
    assert proc.stdout is not None
    banner = proc.stdout.readline()
    assert "full output saved to" in banner
    first = proc.stdout.readline()
    assert first.strip() == "first"
    # The first line arrived while the child was still sleeping: with the
    # old blocking read it would only arrive at child exit.
    assert proc.poll() is None
    rest, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert "second" in rest
    body = open(log, encoding="utf-8").read()
    assert "first" in body and "second" in body
