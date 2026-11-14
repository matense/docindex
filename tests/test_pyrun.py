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
