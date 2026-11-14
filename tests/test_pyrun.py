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
