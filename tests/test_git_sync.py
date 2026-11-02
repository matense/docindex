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
