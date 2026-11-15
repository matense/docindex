"""Pyrun sandbox core tests (Task 1: sealed stdlib-ast evaluator)."""

import importlib.util
import os
import sys


def _load_evaluate():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "modules", "pyrun", "evaluate.py")
    name = "docindex_module_pyrun_evaluate"
    if name in sys.modules:
        return sys.modules[name].evaluate
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod.evaluate


evaluate = _load_evaluate()


def test_arithmetic_and_comprehensions():
    assert evaluate("total = sum(r['v'] for r in rows); total",
                    {"rows": [{"v": 1}, {"v": 2}]}) == {
        "ok": True, "value": 3}


def test_user_functions_and_builtin_whitelist():
    assert evaluate("def double(x):\n return x * 2\ndouble(21)") == {
        "ok": True, "value": 42}


def test_denied_constructs_fail_closed():
    for code in ["import os", "eval('1')", "x = 1\nwhile True:\n x += 1",
                 "().__class__", "__import__('os')",
                 "def f():\n global g",
                 "open('f').read()"]:
        out = evaluate(code)
        assert out["ok"] is False


def test_budgets_abort_runaway_work():
    out = evaluate("total = 0\nfor i in range(10**12):\n total += i\ntotal")
    assert out["ok"] is False


def test_builtin_names_cannot_be_shadowed():
    # Builtins take precedence over every user binding site: assignment,
    # function definitions, and tool-layer variables.
    assert evaluate("len = 1\nlen([1, 2])") == {"ok": True, "value": 2}
    assert evaluate("def sum(vals):\n return 0\nsum([1, 2])") == {
        "ok": True, "value": 3}
    assert evaluate("range = [1, 2, 3]\nlist(range(2))") == {
        "ok": True, "value": [0, 1]}
    assert evaluate("sum([10])", {"sum": 5}) == {"ok": True, "value": 10}


def test_format_nested_spec_inspected():
    # Item access smuggled through a nested format spec must be denied,
    # even though it resolves to a valid spec at format time.
    assert evaluate("'{0:{a[0]}}'.format('x', a=['5'])")["ok"] is False
    assert evaluate('"{0:{a.__class__}}".format(1, a=2)')["ok"] is False
    # Benign nested specs (plain names) still work.
    assert evaluate("'{0:{w}}'.format(3.14159, w='.2f')") == {
        "ok": True, "value": "3.14"}


def test_no_builtin_leakage():
    for code in ["__builtins__",
                 "[c for c in ().__class__.__base__.__subclasses__()]",
                 "().__class__.__base__.__subclasses__()",
                 "getattr((), 'clip')",
                 "().__class__.__dict__"]:
        assert evaluate(code)["ok"] is False


def test_hostile_exfiltration_paths_blocked():
    payloads = [
        "__import__('os').system('id')",
        "().__class__.__base__.__subclasses__()[100]",
        "eval('2*21')",
        "compile('1', 'x', 'eval')",
        "open('/etc/passwd').read()",
        "(lambda: (lambda: 1))().__class__",
        "try:\n x = 1/0\nexcept:\n import os",
        "def f():\n  def g():\n   return 1",
    ]
    for code in payloads:
        out = evaluate(code)
        assert out["ok"] is False, code


def test_unbounded_loops_rejected_or_aborted():
    assert evaluate("i=0\nwhile True:\n i+=1")["ok"] is False
    out = evaluate("s=0\nfor i in range(10**9):\n s+=i\ns")
    assert out["ok"] is False


def test_allocation_accounting_aborts_giant_builds():
    assert evaluate('"x" * 10**9')["ok"] is False
    assert evaluate("[0] * 10**8")["ok"] is False


def test_wall_clock_abort_suggests_chunking():
    out = evaluate("s=0\nfor i in range(10**8):\n s+=(i%7)\ns",
                   limits={"max_wall_seconds": 1})
    assert out["ok"] is False and "chunk" in out["error"].lower()


def test_chunked_large_input_stays_bounded():
    chunks = ["a,b\n" + "1,2\n" * 5000 for _ in range(4)]
    out = evaluate("total = sum(int(r['a']) for r in rows('f'))\ntotal",
                   files={"f": chunks},
                   limits={"max_iterations": 100000})
    assert out == {"ok": True, "value": 20000}


def test_host_helpers_parse_and_rows():
    assert evaluate('parse_json(\'{"a": [1,2]}\')') == {
        "ok": True, "value": {"a": [1, 2]}}
    out = evaluate('rows("f")',
                   files={"f": ["x,y\n1,2\n3,4\n"]})
    assert out == {"ok": True, "value": [{"x": "1", "y": "2"},
                                         {"x": "3", "y": "4"}]}
    assert evaluate('parse_json("{oops}")')["ok"] is False
    assert evaluate('parse_csv("a;b\\n1;2", ";")') == {
        "ok": True, "value": [{"a": "1", "b": "2"}]}


def test_internal_limits_bypass_user_except():
    code = ("s = 0\ntry:\n for i in range(10**9):\n  s += i\n"
            "except Exception:\n s = -1\ns")
    out = evaluate(code)
    assert out["ok"] is False
    assert out.get("value") != -1


def test_wall_clock_path_aborts_and_suggests_chunking():
    # ~5000 ticks: far below the iteration budget, so only _check_wall
    # can fail this (max_wall_seconds=0 trips at the first timed check).
    out = evaluate("s=0\nfor i in range(5000):\n s+=(i%7)\ns",
                   limits={"max_wall_seconds": 0})
    assert out["ok"] is False
    assert "wall-clock" in out["error"]
    assert "chunk" in out["error"].lower()


def test_rows_consumes_shared_iteration_budget():
    # rows() alone ticks once per parsed row (20000 rows here); with no
    # consumer loop in the program, only the per-row ticks can exhaust
    # a 10000-operation budget.
    chunks = ["a,b\n" + "1,2\n" * 5000 for _ in range(4)]
    out = evaluate("rows('f')\n0",
                   files={"f": chunks},
                   limits={"max_iterations": 10000})
    assert out["ok"] is False
    assert "iteration budget" in out["error"]
    assert "chunk" in out["error"].lower()


# ---------------------------------------------------------------------------
# Task 3: module + pyrun.compute / pyrun.save tool wiring
# ---------------------------------------------------------------------------

import io

import pytest
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import AIUsageEvent, Drive, ModuleState, StoredFile, User
from app.services import file_service


def _pyrun_tools():
    """The AI tools module (Task 3) — ImportError until implemented."""
    return importlib.import_module("docindex_module_pyrun.tools")


def _call_compute(user, args, drive=None):
    """Call pyrun.compute directly; caller must hold an app context.

    Accepts the raw `user` fixture id or a User object, mirroring how
    tests/test_dashboards.py resolves fixtures before calling handlers.
    """
    tools = _pyrun_tools()
    user_obj = (db.session.get(User, user)
                if isinstance(user, int) else user)
    return tools._tool_compute(user_obj, args, drive)


def _call_save(user, args, drive=None):
    """Call pyrun.save directly; caller must hold an app context."""
    tools = _pyrun_tools()
    user_obj = (db.session.get(User, user)
                if isinstance(user, int) else user)
    return tools._tool_save(user_obj, args, drive)


def _writable_drive(app, user_id):
    with app.app_context():
        drive = Drive(name="Personal", user_id=user_id, source_path=None)
        db.session.add(drive)
        db.session.commit()
        return drive.id


def _csv_file(app, user_id, drive_id, name="data.csv",
              content=b"v\n1\n2\n"):
    with app.app_context():
        owner = db.session.get(User, user_id)
        drive = db.session.get(Drive, drive_id)
        stored = file_service.save_upload(
            FileStorage(stream=io.BytesIO(content), filename=name,
                        content_type="text/csv"),
            owner, None, drive=drive)
        stored.drive_id = drive_id
        db.session.commit()
        return stored.id


def test_compute_end_to_end_with_file(app, user):
    drive_id = _writable_drive(app, user)
    fid = _csv_file(app, user, drive_id)
    with app.app_context():
        out = _call_compute(user, {
            "code": "total = sum(int(r['v']) for r in rows('f'))\ntotal",
            "files": [{"file_id": fid, "as": "f"}]})
    assert out["ok"] is True and out["value"] == 3


def test_foreign_or_missing_file_rejected(app, user):
    drive_id = _writable_drive(app, user)
    fid = _csv_file(app, user, drive_id)
    with app.app_context():
        # Unknown id.
        assert _call_compute(
            user, {"code": "1", "files": [{"file_id": 999999, "as": "f"}]}
        )["ok"] is False
        # Trashed own file: rejected, never silently skipped.
        file_service.delete_file(db.session.get(StoredFile, fid))
        out = _call_compute(
            user, {"code": "1", "files": [{"file_id": fid, "as": "f"}]})
        assert out["ok"] is False
        assert out.get("retry_hint")


def test_foreign_owned_file_rejected(app, user):
    drive_id = _writable_drive(app, user)
    fid = _csv_file(app, user, drive_id)
    with app.app_context():
        bob = User(username="bob", email="bob@example.com")
        bob.set_password("password123")
        db.session.add(bob)
        db.session.commit()
        bob_id = bob.id
    with app.app_context():
        out = _call_compute(bob_id, {
            "code": "1", "files": [{"file_id": fid, "as": "f"}]})
        assert out["ok"] is False
        assert out.get("retry_hint")


def test_save_artifact_roundtrip(app, user):
    drive_id = _writable_drive(app, user)
    with app.app_context():
        out = _call_compute(user, {"code": "out = 'a,b\\n1,2\\n'",
                                   "outputs": {"result.csv": "out"}})
    assert out["ok"] is True
    key = out["artifacts"][0]["key"]
    with app.app_context():
        saved = _call_save(user, {"key": key, "drive_id": drive_id})
    assert saved["ok"] is True and saved["file_id"] > 0
    with app.app_context():
        stored = db.session.get(StoredFile, saved["file_id"])
        assert stored is not None and stored.drive_id == drive_id
        assert stored.name == "result.csv"
        assert file_service.read_text_content(stored) == "a,b\n1,2\n"


def test_save_rejects_path_traversal_and_foreign_drive(app, user):
    drive_id = _writable_drive(app, user)
    with app.app_context():
        out = _call_compute(user, {"code": "out = 'x'",
                                   "outputs": {"result.csv": "out"}})
        key = out["artifacts"][0]["key"]
        bob = User(username="bob", email="bob@example.com")
        bob.set_password("password123")
        db.session.add(bob)
        db.session.commit()
        foreign_drive = Drive(name="Bob drive", user_id=bob.id)
        db.session.add(foreign_drive)
        db.session.commit()
        foreign_id = foreign_drive.id
    with app.app_context():
        for bad in ("../../evil.csv", "/abs/evil.csv", "sub/dir.csv",
                    ".."):
            denied = _call_save(user, {"key": key, "drive_id": drive_id,
                                       "name": bad})
            assert denied["ok"] is False, bad
            assert denied.get("retry_hint"), bad
        foreign = _call_save(user, {"key": key,
                                    "drive_id": foreign_id})
        assert foreign["ok"] is False
        assert foreign.get("retry_hint")
        unknown = _call_save(user, {"key": "nope-" + "0" * 28,
                                    "drive_id": drive_id})
        assert unknown["ok"] is False
        assert unknown.get("retry_hint")
        # An empty name override falls back to the artifact's own name
        # and saves (consumes the single-use key last).
        fallback = _call_save(user, {"key": key, "drive_id": drive_id,
                                     "name": ""})
        assert fallback["ok"] is True
        assert db.session.get(StoredFile,
                              fallback["file_id"]).name == "result.csv"


def test_error_shape_has_retry_hint(app, user):
    with app.app_context():
        out = _call_compute(user, {"code": "open('/etc/passwd').read()"})
    assert out["ok"] is False
    assert out.get("retry_hint"), "retryable errors must carry a hint"
    assert "traceback" not in out.get("error", "").lower()


def test_compute_records_usage(app, user):
    with app.app_context():
        before = AIUsageEvent.query.filter_by(user_id=user).count()
        out = _call_compute(user, {"code": "1 + 1"})
        assert out["ok"] is True and out["value"] == 2
        assert (AIUsageEvent.query.filter_by(user_id=user).count()
                == before + 1)


def test_help_lists_pyrun_when_enabled(app, auth_client):
    resp = auth_client.get("/settings/help")
    assert b'id="help-modules"' not in resp.data

    with app.app_context():
        db.session.add(ModuleState(name="pyrun", enabled=True))
        db.session.commit()

    resp = auth_client.get("/settings/help")
    html = resp.data.decode()
    assert 'id="help-modules"' in html
    assert "pyrun" in html.lower() or "Python" in html


# ---------------------------------------------------------------------------
# Task 4: tool-level hostile + scale evidence
# ---------------------------------------------------------------------------


def test_tool_level_code_cannot_escape(app, user):
    for code in ["__import__('os').system('id')",
                 "open('/etc/passwd').read()",
                 "(1).__class__.__base__.__subclasses__()",
                 "x = parse_json.__globals__",
                 "y = rows.__class__"]:
        with app.app_context():
            out = _call_compute(user, {"code": code})
        assert out["ok"] is False
        assert "traceback" not in out.get("error", "").lower()
        assert out.get("retry_hint")


def test_tool_level_path_traversal_rejected(app, user):
    with app.app_context():
        out = _call_compute(user, {"code": "1",
                                   "outputs": {"../evil.txt": "x"}})
    assert out["ok"] is False
    assert out.get("retry_hint")


def test_tool_outputs_stay_small(app, user):
    with app.app_context():
        out = _call_compute(user, {"code": "s = 'z' * (10**7)\ns"})
    assert out["ok"] is False
    assert out.get("retry_hint")


def test_large_file_streams_in_chunks(app, user, monkeypatch):
    # >200KB blob must take the 64KB streaming branch, never the
    # whole-file read_text_content path (which caps at 200k chars).
    n = 20000
    content = ("v,pad\n" + ("1," + "x" * 10 + "\n") * n).encode()
    assert len(content) > 200_000
    drive_id = _writable_drive(app, user)
    fid = _csv_file(app, user, drive_id, name="big.csv", content=content)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert stored.size > 200_000
        tools = _pyrun_tools()
        chunks = tools._read_chunks(stored)
        assert len(chunks) > 1
        assert "".join(chunks) == content.decode()

        def _no_whole_read(*args, **kwargs):
            raise AssertionError("large files must stream, not whole-read")

        monkeypatch.setattr(file_service, "read_text_content", _no_whole_read)
        out = _call_compute(user, {"code": "1",
                                   "files": [{"file_id": fid, "as": "f"}]})
    assert out["ok"] is True and out["value"] == 1


def test_save_key_single_use(app, user):
    drive_id = _writable_drive(app, user)
    with app.app_context():
        out = _call_compute(user, {"code": "out = 'a,b\\n1,2\\n'",
                                   "outputs": {"result.csv": "out"}})
    assert out["ok"] is True
    key = out["artifacts"][0]["key"]
    with app.app_context():
        first = _call_save(user, {"key": key, "drive_id": drive_id})
    assert first["ok"] is True
    with app.app_context():
        second = _call_save(user, {"key": key, "drive_id": drive_id})
    assert second["ok"] is False
    assert second.get("retry_hint")


# ---------------------------------------------------------------------------
# Final-review findings: line-aligned chunks + header-aware rows()
# ---------------------------------------------------------------------------


def test_rows_over_large_file_exact_values(app, user):
    # >200KB file whose data rows cross several 64KB chunk boundaries
    # (pad column forces the size; the ✓ pins the incremental UTF-8
    # decoder). rows() must return every row with EXACT values — the old
    # byte-split chunks silently misaligned/dropped rows here.
    n = 4000
    rows = []
    expected = []
    for i in range(n):
        value = f"val-{i}-\u2713"
        rows.append(f"{i},{value}," + "x" * 40)
        expected.append([str(i), value])
    content = ("id,v,pad\n" + "\n".join(rows) + "\n").encode("utf-8")
    assert len(content) > 200_000
    drive_id = _writable_drive(app, user)
    fid = _csv_file(app, user, drive_id, name="big-rows.csv",
                    content=content)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert stored.size > 200_000
        tools = _pyrun_tools()
        chunks = tools._read_chunks(stored)
        assert len(chunks) > 1
        assert "".join(chunks) == content.decode("utf-8")
        out = _call_compute(
            user,
            {"code": "rs = rows('f')\n"
                     "[[r['id'], r['v']] for r in rs]",
             "files": [{"file_id": fid, "as": "f"}]})
    assert out["ok"] is True, out
    assert out["value"] == expected


# ---------------------------------------------------------------------------
# Task 5: sealed SQLite scratchpad — full read/write SQL in :memory:
# ---------------------------------------------------------------------------


def test_sqlite_select_join_groupby():
    files = {"sales": ["id,amt\n1,10\n2,20\n3,30\n"],
             "cats": ["id,cat\n1,a\n2,a\n3,b\n"]}
    code = ("r = db.query('SELECT c.cat, SUM(CAST(s.amt AS INTEGER)) AS t "
            "FROM t0 s JOIN t1 c ON s.id = c.id "
            "GROUP BY c.cat ORDER BY c.cat')\n"
            "r")
    out = evaluate(code, files=files)
    assert out == {"ok": True, "value": [
        {"cat": "a", "t": 30}, {"cat": "b", "t": 30}]}


def test_sqlite_writes_and_ddl():
    files = {"f": ["id,v\n1,a\n2,b\n"]}
    code = ("db.exec(\"INSERT INTO t0 (id, v) VALUES ('3', 'c')\")\n"
            "db.exec(\"UPDATE t0 SET v = 'z' WHERE id = '1'\")\n"
            "db.exec('DELETE FROM t0 WHERE id = \\'2\\'')\n"
            "db.exec('CREATE TABLE t9 AS SELECT * FROM t0')\n"
            "db.exec('CREATE INDEX i9 ON t9(id)')\n"
            "db.query('SELECT id, v FROM t9 ORDER BY id')")
    out = evaluate(code, files=files)
    assert out == {"ok": True, "value": [
        {"id": "1", "v": "z"}, {"id": "3", "v": "c"}]}
    out2 = evaluate("db.exec('CREATE TABLE t9 AS SELECT * FROM t0')\n"
                    "db.exec('CREATE INDEX i9 ON t9(id)')\n"
                    "db.exec('DROP INDEX i9')\n"
                    "db.exec('DROP TABLE t9')\n"
                    "db.query('SELECT COUNT(*) AS n FROM t0')",
                    files=files)
    assert out2["ok"] is True


def test_sqlite_lockdown_denied():
    for sql in ["ATTACH DATABASE '/tmp/x.db' AS x",
                "attach database ':memory:' as x",
                "DETACH DATABASE t0",
                "PRAGMA table_info(t0)",
                "VACUUM",
                "SELECT load_extension('x')"]:
        out = evaluate(f"db.query({sql!r})", files={"f": ["a\n1\n"]})
        assert out["ok"] is False, sql
        out = evaluate(f"db.exec({sql!r})", files={"f": ["a\n1\n"]})
        assert out["ok"] is False, sql


def test_sqlite_budgets_count_rows():
    chunks = ["a\n" + "1\n" * 50000]
    out = evaluate("db.query('SELECT * FROM t0')",
                   files={"f": chunks},
                   limits={"max_iterations": 1000})
    assert out["ok"] is False


def test_sqlite_no_escape_surface():
    for code in ["import sqlite3",
                "open('x.db').read()",
                "db.connection",
                "db.sqlite3",
                "db.exec(\"ATTACH DATABASE '/etc/passwd' AS p\")"]:
        assert evaluate(code, files={"f": ["a\n1\n"]})["ok"] is False


def test_sqlite_tool_error_has_retry_hint(app, user):
    with app.app_context():
        out = _call_compute(user, {"code": "db.query('SELECT * FROM t0')",
                                   "files": []})
    # empty scratch DB has no t0 — must be a safe retry-shaped error
    assert out["ok"] is False and out.get("retry_hint")
    assert "traceback" not in out.get("error", "").lower()
