"""Sealed stdlib-``ast`` evaluator core for the ``pyrun`` sandbox module.

A small walking interpreter over :func:`ast.parse` output. Only an explicit
whitelist executes; everything else fails closed with a safe message (no
tracebacks, no host paths).

Semantics (binding for this task):

* Inputs: ``code`` (str), ``variables`` (name -> JSON value), ``files``
  (name -> list of text chunks; each chunk is parsed by the ``rows()``
  host helper below as an independent header-first CSV fragment),
  ``limits`` (partial dict merged over :data:`DEFAULT_LIMITS`).
* Result: ``{"ok": True, "value": <JSON value>}`` or
  ``{"ok": False, "error": str}``. The value is the last *top-level*
  expression statement (``total = ...; total``); a script ending without
  one yields ``None``. The value must JSON-serialize within the output cap.
* Allowed: literals, names, arithmetic/comparison/boolean ops, assignment
  (incl. ``+=`` and annotated), ``if``, ``for`` over iterables, ``try``
  / ``except`` (bare or whitelisted types only), ``def`` / ``lambda``
  (no decorators, no nested ``def``), comprehensions, f-strings.
* Calls: only the builtin whitelist (``len range sum min max sorted abs
  round enumerate zip str int float bool list dict set tuple``) plus the
  host helpers (``parse_json parse_csv rows``) and the per-type method
  whitelist below. ALL dunder/underscore attribute access
  is denied, as are ``eval|exec|compile|open|__import__`` and friends
  (they are simply absent from the namespace).
* Denied: ``import``, ``while``, dunders, ``global`` / ``nonlocal``,
  decorators, nested ``def`` s, ``with`` / ``del`` / ``assert`` /
  ``match`` / ``class`` / async / yield. ``raise`` is restricted to
  ``ValueError``. ``except`` (including bare) catches only
  ``ValueError`` / ``ZeroDivisionError`` / ``KeyError`` / ``IndexError``
  / ``TypeError``; internal budget/deny errors bypass user handlers.
* Budgets: AST node count, shared iteration budget (``for`` /
  comprehensions / bulk builtin+method consumption / user calls each
  consume it), call-stack depth, allocation budget (created
  str/bytes/list/tuple/set/dict/int values are charged realistic unit
  costs; one-shot giant allocations are pre-checked *before* the host
  allocates), output size, cooperative wall-clock checks. ``range()``
  arguments are capped by the iteration budget.
* Power-guard rails: huge ``int`` powers/shifts/multiplications are
  rejected before the host computes them; ``str.format`` field access
  (``{0.__class__}`` style) is rejected; generator expressions evaluate
  eagerly to a one-shot iterator (no scope-interleaving hazards).
* Host helpers: ``parse_json(text)`` parses a JSON document (errors
  surface as catchable ``ValueError``); ``parse_csv(text[, delimiter])``
  parses CSV text with a header row into a list of ``str->str`` dicts;
  ``rows(name[, delimiter])`` does the same over a named file's chunk
  list (each chunk is parsed as a header-first CSV fragment; every parsed
  row consumes one shared-iteration tick and is charged to the allocation
  budget, and each chunk read checks the wall clock). All three return
  plain JSON-serializable lists/dicts. Helpers ride the variables-style
  namespace layer: an explicit user binding shadows them (so a `rows`
  data variable keeps working), while builtins still take precedence
  over everything.
* Namespace hygiene: whitelisted builtins take precedence over every
  user binding (scopes and tool-layer variables alike), so sandboxed code
  can never shadow or neutralize one; the sandbox never sees host
  ``__builtins__`` at all (unknown/dunder names fail closed); nested
  ``str.format`` specs are validated recursively for the same reason.

Only the standard library is used (``ast``, ``copy``, ``csv``, ``io``,
``json``, ``keyword``, ``string``, ``time``).
"""

import ast
import copy
import csv
import io
import json
import keyword
import string
import time

DEFAULT_LIMITS = {
    "max_ast_nodes": 5000,
    "max_iterations": 100000,
    "max_call_depth": 20,
    "max_output_chars": 204800,
    "max_wall_seconds": 30,
    # Backstop so one-shot giant allocations abort instead of spiking RAM.
    # Bounds both single objects and cumulative sandbox-created values.
    "max_allocation_bytes": 50000000,
}

__all__ = ["DEFAULT_LIMITS", "evaluate"]

_CODE_MAX_CHARS = 1000000
_INT_BIT_CAP = 2000000  # largest exact int (bits) any single op may produce
_TICK_WALL_CHECK_MASK = 1023  # wall-clock check every 1024 ticks

_ALLOWED_EXC_NAMES = frozenset({
    "ValueError", "ZeroDivisionError", "KeyError", "IndexError", "TypeError",
})
_ALLOWED_EXC_TYPES = (ValueError, ZeroDivisionError, KeyError, IndexError,
                      TypeError)

_BUILTIN_NAMES = frozenset({
    "len", "range", "sum", "min", "max", "sorted", "abs", "round",
    "enumerate", "zip", "str", "int", "float", "bool",
    "list", "dict", "set", "tuple",
})

# Host-side data helpers. Plain value-only functions injected into the
# sandbox namespace at the variables layer: an explicit user binding
# (scope assignment or tool-layer variable) shadows a helper for that
# run — shadowing one just replaces it, it is not a security boundary.
# Whitelisted builtins still take absolute precedence over everything.
_HOST_HELPERS = frozenset({"parse_json", "parse_csv", "rows"})

# Per-type method whitelist: exactly the spec'd data-work surface.
_METHODS = {
    str: frozenset({"split", "join", "strip", "replace", "startswith",
                    "endswith", "format"}),
    list: frozenset({"append", "extend"}),
    dict: frozenset({"get", "items", "keys", "values", "update"}),
}

_FORMATTER = string.Formatter()


# ---------------------------------------------------------------------------
# Control flow (internal; never visible to sandboxed code)
# ---------------------------------------------------------------------------

class _Halt(Exception):
    """Base for internal control flow: denied constructs, budgets, jumps."""


class _Denied(_Halt):
    """A construct outside the whitelist was used."""


class _Budget(_Halt):
    """An enforced budget was exceeded."""


class _Break(_Halt):
    pass


class _Continue(_Halt):
    pass


class _Return(_Halt):
    def __init__(self, value):
        super().__init__("return")
        self.value = value


class _UserError(Exception):
    """A whitelisted user-visible error (catchable by sandboxed `except`)."""

    def __init__(self, exc):
        super().__init__(str(exc))
        self.exc = exc


def _fail(error):
    return {"ok": False, "error": error}


def _validate_format_template(template):
    """Reject attribute/item access in every field of a format template.

    The real ``str.format`` machinery resolves nested format specs
    (e.g. ``{0:{a[0]}}``) outside the interpreter, so each nesting level
    is parsed and inspected recursively; every field the host would
    resolve is checked here first.
    """
    for _, field_name, format_spec, _ in _FORMATTER.parse(template):
        if field_name is not None and (
                "." in field_name or "[" in field_name):
            raise _Denied("str.format with attribute/item "
                          "access is not allowed")
        if format_spec and "{" in format_spec:
            _validate_format_template(format_spec)


# ---------------------------------------------------------------------------
# Static validation: node budget + denied constructs, before execution
# ---------------------------------------------------------------------------

class _Validator(ast.NodeVisitor):
    """Reject denied syntax up front so evaluation never sees it."""

    def __init__(self, max_nodes):
        self.max_nodes = max_nodes
        self.count = 0
        self.func_depth = 0
        self.loop_depth = 0

    def visit(self, node):
        self.count += 1
        if self.count > self.max_nodes:
            raise _Budget(
                f"program too large ({self.count} AST nodes, cap "
                f"{self.max_nodes}); split it into smaller steps")
        return super().visit(node)

    def _deny(self, kind):
        raise _Denied(f"denied construct: {kind}")

    # -- denied statements -------------------------------------------
    def visit_Import(self, node):
        self._deny("import")

    def visit_ImportFrom(self, node):
        self._deny("import")

    def visit_While(self, node):
        self._deny("while loop (use for/comprehensions)")

    def visit_Global(self, node):
        self._deny("global")

    def visit_Nonlocal(self, node):
        self._deny("nonlocal")

    def visit_ClassDef(self, node):
        self._deny("class definition")

    def visit_With(self, node):
        self._deny("with block")

    def visit_Delete(self, node):
        self._deny("del statement")

    def visit_Assert(self, node):
        self._deny("assert statement")

    def visit_Match(self, node):
        self._deny("match statement")

    def visit_AsyncFunctionDef(self, node):
        self._deny("async function")

    def visit_AsyncFor(self, node):
        self._deny("async for")

    def visit_AsyncWith(self, node):
        self._deny("async with")

    def visit_Await(self, node):
        self._deny("await")

    def visit_Yield(self, node):
        self._deny("yield")

    def visit_YieldFrom(self, node):
        self._deny("yield")

    def visit_TryStar(self, node):
        self._deny("except*")

    def visit_Break(self, node):
        if self.loop_depth == 0:
            self._deny("break outside loop")

    def visit_Continue(self, node):
        if self.loop_depth == 0:
            self._deny("continue outside loop")

    def visit_Return(self, node):
        if self.func_depth == 0:
            self._deny("return outside function")
        self.generic_visit(node)

    def visit_BinOp(self, node):
        if isinstance(node.op, ast.MatMult):
            self._deny("matrix multiplication")
        self.generic_visit(node)

    # -- functions ----------------------------------------------------
    def _check_arg_names(self, args):
        for arg in (list(args.posonlyargs) + list(args.args)
                    + list(args.kwonlyargs)):
            if arg.arg.startswith("__"):
                self._deny("dunder argument name")
        for name in ("vararg", "kwarg"):
            var = getattr(args, name)
            if var is not None and var.arg.startswith("__"):
                self._deny("dunder argument name")

    def visit_FunctionDef(self, node):
        if node.decorator_list:
            self._deny("decorators")
        if self.func_depth > 0:
            self._deny("nested function definition")
        if node.name.startswith("__"):
            self._deny("dunder name")
        self._check_arg_names(node.args)
        self.func_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self.func_depth -= 1

    def visit_Lambda(self, node):
        self._check_arg_names(node.args)
        self.generic_visit(node)

    # -- names / attributes -------------------------------------------
    def visit_Name(self, node):
        if node.id.startswith("__"):
            self._deny("dunder name")

    def visit_Attribute(self, node):
        if node.attr.startswith("_"):
            self._deny("dunder/private attribute access")
        self.generic_visit(node)

    def visit_comprehension(self, node):
        if node.is_async:
            self._deny("async comprehension")
        self.generic_visit(node)

    # -- exceptions ----------------------------------------------------
    def _check_handler_type(self, node):
        if node.type is None:
            return  # bare `except`: allowed, catches whitelisted set only
        names = []
        if isinstance(node.type, ast.Name):
            names = [node.type.id]
        elif isinstance(node.type, ast.Tuple):
            for elt in node.type.elts:
                if not isinstance(elt, ast.Name):
                    self._deny("non-standard except type")
                names.append(elt.id)
        else:
            self._deny("non-standard except type")
        for name in names:
            if name not in _ALLOWED_EXC_NAMES:
                self._deny(f"except type '{name}' is not allowed")

    def visit_ExceptHandler(self, node):
        self._check_handler_type(node)
        if node.name and node.name.startswith("__"):
            self._deny("dunder name")
        self.generic_visit(node)

    def visit_Raise(self, node):
        if node.exc is None:
            self._deny("bare raise")
        exc = node.exc
        if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name) \
                and exc.func.id == "ValueError":
            self.generic_visit(node)
            return
        if isinstance(exc, ast.Name) and exc.id == "ValueError":
            return
        self._deny("raise is restricted to ValueError")

    # -- loops (depth tracking for break/continue) ----------------------
    def visit_For(self, node):
        self.loop_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self.loop_depth -= 1


# ---------------------------------------------------------------------------
# First-class sandbox values
# ---------------------------------------------------------------------------

class _Builtin:
    """A whitelisted builtin exposed as a first-class value (``f = len``)."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<builtin {self.name}>"


class _BoundMethod:
    """A whitelisted per-type method bound to a sandbox value."""

    def __init__(self, obj, name):
        self.obj = obj
        self.name = name

    def __repr__(self):
        return f"<bound method {self.name}>"


class _Function:
    def __init__(self, name, args, body, closure):
        self.name = name
        self.args = args
        self.body = body
        self.closure = closure  # live scope dicts (defining env)

    def __repr__(self):
        return f"<function {self.name}>"


class _Lambda:
    def __init__(self, args, body, closure):
        self.args = args
        self.body = body
        self.closure = closure

    def __repr__(self):
        return "<lambda>"


# ---------------------------------------------------------------------------
# The interpreter
# ---------------------------------------------------------------------------

class _Interpreter:
    def __init__(self, variables, files, limits):
        self.limits = limits
        self.variables = variables
        self.files = files
        self.builtins = {name: _Builtin(name) for name in _BUILTIN_NAMES}
        self.host_helpers = {name: _Builtin(name)
                             for name in _HOST_HELPERS}
        self.scopes = [{}]  # module-level env; functions push more
        self.ticks = 0
        self.alloc = 0
        self.depth = 0
        self.start = time.monotonic()
        self.last_value = None

    # -- budgets -------------------------------------------------------
    def _tick(self, n=1):
        self.ticks += n
        if self.ticks > self.limits["max_iterations"]:
            raise _Budget(
                f"iteration budget exceeded "
                f"({self.limits['max_iterations']} operations); chunk the "
                "work into smaller pieces")
        if (self.ticks & _TICK_WALL_CHECK_MASK) == 0:
            self._check_wall()

    def _check_wall(self):
        elapsed = time.monotonic() - self.start
        if elapsed > self.limits["max_wall_seconds"]:
            raise _Budget(
                f"wall-clock budget exceeded ({elapsed:.1f}s of "
                f"{self.limits['max_wall_seconds']}s); chunk the "
                "input and combine partial results")

    def _charge(self, n):
        self.alloc += n
        if self.alloc > self.limits["max_allocation_bytes"]:
            raise _Budget(
                "allocation budget exceeded; chunk the work into smaller "
                "pieces instead of building giant values at once")

    def _charge_value(self, value):
        """Charge realistic unit costs for a newly created sandbox value."""
        if isinstance(value, bool):
            self._charge(28)
        elif isinstance(value, int):
            self._charge(32 + (value.bit_length() + 7) // 8)
        elif isinstance(value, float):
            self._charge(24)
        elif isinstance(value, (str, bytes)):
            self._charge(40 + len(value))
        elif isinstance(value, (list, tuple, set)):
            self._charge(56 + 8 * len(value))
        elif isinstance(value, dict):
            self._charge(64 + 72 * len(value))

    def _charge_deep(self, value):
        """Charge a whole host-parsed value tree against the allocation
        budget (charge-only: no iteration ticks, so chunked consumers
        account ticks once per row at their own loop)."""
        stack = [value]
        seen = set()
        while stack:
            item = stack.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            if isinstance(item, bool):
                self._charge(28)
            elif isinstance(item, int):
                self._charge(32 + (item.bit_length() + 7) // 8)
            elif isinstance(item, float):
                self._charge(24)
            elif isinstance(item, str):
                self._charge(40 + len(item))
            elif isinstance(item, (list, tuple, set)):
                self._charge(56 + 8 * len(item))
                stack.extend(item)
            elif isinstance(item, dict):
                self._charge(64 + 72 * len(item))
                for key, val in item.items():
                    stack.append(key)
                    stack.append(val)
            else:
                self._charge(16)

    def _guard(self, fn, *args, **kwargs):
        """Run host-side work; only whitelisted errors reach user code."""
        try:
            return fn(*args, **kwargs)
        except (_Halt, _UserError):
            raise
        except _ALLOWED_EXC_TYPES as exc:
            raise _UserError(exc) from None
        except Exception:
            raise _Denied("operation failed") from None

    def _norm_int(self, value):
        if isinstance(value, int) and not isinstance(value, bool) \
                and value.bit_length() > _INT_BIT_CAP:
            raise _Budget(
                "integer result too large; chunk the work into smaller "
                "pieces")
        return value

    # -- names ----------------------------------------------------------
    def _lookup(self, name):
        # Builtins take precedence over every user binding (scopes and
        # tool-layer variables): sandboxed code can never shadow or
        # neutralize a whitelisted builtin. Host data helpers ride the
        # variables-style layer instead: an explicit user binding (scope
        # assignment or tool-layer variable) shadows them, so Task 1's
        # `rows`-as-data contract keeps working while `rows('f')` over
        # files resolves whenever no such binding exists.
        if name in self.builtins:
            return self.builtins[name]
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        if name in self.variables:
            return self.variables[name]
        if name in self.host_helpers:
            return self.host_helpers[name]
        raise _Denied(f"unknown name: '{name}'")

    # -- blocks / statements ---------------------------------------------
    def run(self, tree):
        self._exec_block(tree.body, toplevel=True)
        return self.last_value

    def _exec_block(self, stmts, toplevel=False):
        for stmt in stmts:
            if toplevel and isinstance(stmt, ast.Expr):
                self.last_value = self._ev(stmt.value)
            else:
                self._exec(stmt)

    def _exec(self, node):
        method = getattr(self, "_ex_" + type(node).__name__, None)
        if method is None:
            raise _Denied(f"denied construct: {type(node).__name__}")
        return method(node)

    def _ex_Expr(self, node):
        self._ev(node.value)

    def _ex_Pass(self, node):
        pass

    def _ex_Assign(self, node):
        value = self._ev(node.value)
        for target in node.targets:
            self._assign(target, value)

    def _ex_AugAssign(self, node):
        if isinstance(node.target, ast.Attribute):
            raise _Denied("denied construct: attribute assignment")
        current = self._ev(node.target) if isinstance(
            node.target, ast.Subscript) else self._lookup_target(node.target)
        value = self._ev(node.value)
        result = self._apply_binop(node.op, current, value)
        self._assign(node.target, result)

    def _ex_AnnAssign(self, node):
        if node.value is None:
            return
        value = self._ev(node.value)
        self._assign(node.target, value)

    def _ex_If(self, node):
        if self._ev(node.test):
            self._exec_block(node.body)
        else:
            self._exec_block(node.orelse)

    def _ex_For(self, node):
        iterable = self._safe_iter(self._ev(node.iter))
        broken = False
        for item in iterable:
            self._tick()
            self._assign(node.target, item)
            try:
                self._exec_block(node.body)
            except _Continue:
                continue
            except _Break:
                broken = True
                break
        if not broken:
            self._exec_block(node.orelse)

    def _ex_Try(self, node):
        try:
            self._exec_block(node.body)
        except _UserError as user:
            handler = self._match_handler(node.handlers, user.exc)
            if handler is None:
                raise
            saved = self.scopes[-1].get(handler.name, _MISSING) \
                if handler.name else _MISSING
            if handler.name:
                self.scopes[-1][handler.name] = user.exc
            try:
                self._exec_block(handler.body)
            finally:
                if handler.name:
                    if saved is _MISSING:
                        self.scopes[-1].pop(handler.name, None)
                    else:
                        self.scopes[-1][handler.name] = saved
        else:
            self._exec_block(node.orelse)
        finally:
            self._exec_block(node.finalbody)

    def _ex_FunctionDef(self, node):
        self.scopes[-1][node.name] = _Function(
            node.name, node.args, node.body, list(self.scopes))

    def _ex_Return(self, node):
        raise _Return(self._ev(node.value) if node.value else None)

    def _ex_Break(self, node):
        raise _Break()

    def _ex_Continue(self, node):
        raise _Continue()

    def _ex_Raise(self, node):
        exc = node.exc
        if isinstance(exc, ast.Name):  # bare `raise ValueError`
            raise _UserError(ValueError())
        args = [self._ev(a) for a in exc.args]
        kwargs = {kw.arg: self._ev(kw.value) for kw in exc.keywords}
        try:
            raise _UserError(ValueError(*args, **kwargs)) from None
        except (_Halt, _UserError):
            raise
        except Exception:
            raise _Denied("operation failed") from None

    # -- assignment targets ----------------------------------------------
    def _lookup_target(self, target):
        if isinstance(target, ast.Name):
            return self._lookup(target.id)
        raise _Denied("denied construct: assignment target")

    def _assign(self, target, value):
        if isinstance(target, ast.Name):
            if target.id.startswith("__"):
                raise _Denied("denied construct: dunder name")
            self.scopes[-1][target.id] = value
        elif isinstance(target, ast.Starred):
            self._assign(target.value, list(self._guard(iter, value)))
        elif isinstance(target, (ast.Tuple, ast.List)):
            self._unpack(target.elts, value)
        elif isinstance(target, ast.Subscript):
            obj = self._ev(target.value)
            index = self._ev(target.slice)
            self._guard(_setitem, obj, index, value)
        else:
            raise _Denied("denied construct: assignment target")

    def _unpack(self, elts, value):
        items = list(self._guard(iter, value))
        star = None
        for i, elt in enumerate(elts):
            if isinstance(elt, ast.Starred):
                if star is not None:
                    raise _Denied("denied construct: multiple starred")
                star = i
        if star is None:
            if len(items) != len(elts):
                raise _UserError(ValueError(
                    f"too {'many' if len(items) > len(elts) else 'few'} "
                    f"values to unpack (expected {len(elts)}, got "
                    f"{len(items)})"))
            for elt, item in zip(elts, items, strict=False):
                self._assign(elt, item)
        else:
            pre, post = elts[:star], elts[star + 1:]
            if len(items) < len(pre) + len(post):
                raise _UserError(ValueError("not enough values to unpack"))
            for elt, item in zip(pre, items[:len(pre)], strict=False):
                self._assign(elt, item)
            middle = items[len(pre):len(items) - len(post)] if post \
                else items[len(pre):]
            self._assign(elts[star].value, middle)
            for elt, item in zip(post, items[len(items) - len(post):], strict=False):
                self._assign(elt, item)

    def _match_handler(self, handlers, exc):
        for handler in handlers:
            if handler.type is None:
                return handler  # bare except: whitelisted set only
            if isinstance(handler.type, ast.Name):
                wanted = (handler.type.id,)
            else:
                wanted = tuple(e.id for e in handler.type.elts)
            mapping = {"ValueError": ValueError,
                       "ZeroDivisionError": ZeroDivisionError,
                       "KeyError": KeyError, "IndexError": IndexError,
                       "TypeError": TypeError}
            if any(isinstance(exc, mapping[name]) for name in wanted):
                return handler
        return None

    # -- expressions -------------------------------------------------------
    def _ev(self, node):
        method = getattr(self, "_ev_" + type(node).__name__, None)
        if method is None:
            raise _Denied(f"denied construct: {type(node).__name__}")
        return method(node)

    def _ev_Constant(self, node):
        return node.value

    def _ev_Name(self, node):
        if not isinstance(node.ctx, ast.Load):
            raise _Denied("denied construct: name context")
        return self._lookup(node.id)

    def _ev_Attribute(self, node):
        obj = self._ev(node.value)
        if node.attr.startswith("_"):
            raise _Denied("denied construct: dunder/private attribute")
        allowed = _METHODS.get(type(obj))
        if not allowed or node.attr not in allowed:
            raise _Denied(f"attribute '{node.attr}' is not allowed")
        return _BoundMethod(obj, node.attr)

    def _ev_Call(self, node):
        args = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                args.extend(self._guard(list, self._ev(arg.value)))
            else:
                args.append(self._ev(arg))
        kwargs = {}
        for kw in node.keywords:
            if kw.arg is None:
                mapping = self._ev(kw.value)
                if not isinstance(mapping, dict):
                    raise _UserError(TypeError(
                        "argument unpacking requires a dict"))
                kwargs.update(mapping)
            else:
                if kw.arg in kwargs:
                    raise _UserError(TypeError(
                        f"got multiple values for keyword '{kw.arg}'"))
                kwargs[kw.arg] = self._ev(kw.value)
        if isinstance(node.func, ast.Name):
            target = self._lookup(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            target = self._ev(node.func)
            if not isinstance(target, _BoundMethod):
                raise _Denied("denied construct: call target")
        else:
            target = self._ev(node.func)
            if not isinstance(
                    target, (_Function, _Lambda, _Builtin, _BoundMethod)):
                raise _UserError(TypeError("object is not callable"))
        return self._call_value(target, args, kwargs)

    def _call_value(self, target, args, kwargs):
        if isinstance(target, _Builtin):
            return self._call_builtin(target.name, args, kwargs)
        if isinstance(target, _BoundMethod):
            return self._call_method(target, args, kwargs)
        if isinstance(target, (_Function, _Lambda)):
            return self._call_user(target, args, kwargs)
        raise _UserError(TypeError("object is not callable"))

    def _call_user(self, fn, args, kwargs):
        self._tick()
        if self.depth >= self.limits["max_call_depth"]:
            raise _Budget(f"call stack too deep "
                          f"(cap {self.limits['max_call_depth']}); avoid "
                          "deep recursion")
        local = self._bind_params(fn.args, args, kwargs,
                                  getattr(fn, "name", "<lambda>"))
        self.depth += 1
        saved = self.scopes
        self.scopes = list(fn.closure) + [local]
        try:
            if isinstance(fn, _Lambda):
                return self._ev(fn.body)
            try:
                self._exec_block(fn.body)
            except _Return as ret:
                return ret.value
            return None
        except (_Break, _Continue):
            raise _Denied("denied construct: loop jump") from None
        finally:
            self.scopes = saved
            self.depth -= 1

    def _bind_params(self, params, args, kwargs, name):
        pos_names = [a.arg for a in list(params.posonlyargs)
                     + list(params.args)]
        n_defaults = len(params.defaults)
        required_pos = len(pos_names) - n_defaults
        local = {}
        if len(args) > len(pos_names) and params.vararg is None:
            raise _UserError(TypeError(
                f"{name} takes {len(pos_names)} positional arguments "
                f"but {len(args)} were given"))
        for i, value in enumerate(args):
            if i < len(pos_names):
                if pos_names[i] in local:
                    raise _UserError(TypeError("duplicate argument"))
                local[pos_names[i]] = value
            else:
                local.setdefault(params.vararg.arg, []).append(value)
        if params.vararg is not None:
            local.setdefault(params.vararg.arg, [])
            local[params.vararg.arg] = list(local[params.vararg.arg])
        rest = dict(kwargs)
        for i, pname in enumerate(pos_names):
            default_idx = i - required_pos
            if pname in local:
                if pname in rest:
                    raise _UserError(TypeError(
                        f"{name} got multiple values for '{pname}'"))
            elif pname in rest:
                local[pname] = rest.pop(pname)
            elif default_idx >= 0:
                local[pos_names[i]] = self._ev(
                    params.defaults[default_idx])
            else:
                raise _UserError(TypeError(
                    f"{name} missing required argument '{pname}'"))
        for kwonly, default in zip(params.kwonlyargs, params.kw_defaults, strict=False):
            if kwonly.arg in rest:
                local[kwonly.arg] = rest.pop(kwonly.arg)
            elif default is not None:
                local[kwonly.arg] = self._ev(default)
            else:
                raise _UserError(TypeError(
                    f"{name} missing required keyword '{kwonly.arg}'"))
        if rest:
            if params.kwarg is None:
                raise _UserError(TypeError(
                    f"{name} got unexpected keyword '{next(iter(rest))}'"))
            local[params.kwarg.arg] = dict(rest)
        elif params.kwarg is not None:
            local[params.kwarg.arg] = {}
        return local

    def _ev_Subscript(self, node):
        obj = self._ev(node.value)
        index = self._ev(node.slice)
        result = self._guard(_getitem, obj, index)
        if isinstance(index, slice):
            if isinstance(result, (str, bytes)):
                self._charge(len(result))
            elif isinstance(result, (list, tuple)):
                self._charge(56 + 8 * len(result))
        return result

    def _ev_Slice(self, node):
        lower = self._ev(node.lower) if node.lower else None
        upper = self._ev(node.upper) if node.upper else None
        step = self._ev(node.step) if node.step else None
        return self._guard(slice, lower, upper, step)

    def _ev_List(self, node):
        items = [self._ev(e) if not isinstance(e, ast.Starred)
                 else self._guard(list, self._ev(e.value)) for e in node.elts]
        flat = []
        for elt, item in zip(node.elts, items, strict=False):
            if isinstance(elt, ast.Starred):
                flat.extend(item)
            else:
                flat.append(item)
        self._charge(56 + 8 * len(flat))
        return flat

    def _ev_Tuple(self, node):
        items = []
        for elt in node.elts:
            if isinstance(elt, ast.Starred):
                items.extend(self._guard(list, self._ev(elt.value)))
            else:
                items.append(self._ev(elt))
        result = tuple(items)
        self._charge(56 + 8 * len(result))
        return result

    def _ev_Set(self, node):
        items = []
        for elt in node.elts:
            if isinstance(elt, ast.Starred):
                items.extend(self._guard(list, self._ev(elt.value)))
            else:
                items.append(self._ev(elt))
        result = self._guard(set, items)
        self._charge(200 + 32 * len(result))
        return result

    def _ev_Dict(self, node):
        result = {}
        for key_node, val_node in zip(node.keys, node.values, strict=False):
            if key_node is None:
                mapping = self._ev(val_node)
                if not isinstance(mapping, dict):
                    raise _UserError(TypeError("dict unpacking needs a dict"))
                self._tick(len(mapping))
                result.update(mapping)
            else:
                result[self._ev(key_node)] = self._ev(val_node)
        self._charge(64 + 72 * len(result))
        return result

    def _ev_ListComp(self, node):
        out = []

        def emit():
            value = self._ev(node.elt)
            self._charge_value(value)
            out.append(value)

        self._run_comp(node.generators, 0, emit)
        self._charge(56 + 8 * len(out))
        return out

    def _ev_SetComp(self, node):
        out = set()

        def emit():
            value = self._ev(node.elt)
            self._charge(40)
            out.add(self._guard(_hashable, value))

        self._run_comp(node.generators, 0, emit)
        return out

    def _ev_DictComp(self, node):
        out = {}

        def emit():
            key = self._ev(node.key)
            val = self._ev(node.value)
            self._charge(72)
            out[self._guard(_hashable, key)] = val

        self._run_comp(node.generators, 0, emit)
        return out

    def _ev_GeneratorExp(self, node):
        # Eager build under the current scope discipline, handed out as a
        # one-shot iterator (sandbox-observable behavior matches laziness:
        # `for`, `sum`, `list`, `zip` all consume it identically).
        out = []

        def emit():
            value = self._ev(node.elt)
            self._charge_value(value)
            out.append(value)

        self._run_comp(node.generators, 0, emit)
        return iter(out)

    def _run_comp(self, generators, idx, emit):
        gen = generators[idx]
        iterable = self._safe_iter(self._ev(gen.iter))
        local = {}
        self.scopes.append(local)
        try:
            for item in iterable:
                self._tick()
                self._assign(gen.target, item)
                if all(self._ev(cond) for cond in gen.ifs):
                    if idx + 1 == len(generators):
                        emit()
                    else:
                        self._run_comp(generators, idx + 1, emit)
        finally:
            self.scopes.pop()

    def _ev_BoolOp(self, node):
        if isinstance(node.op, ast.And):
            result = self._ev(node.values[0])
            for value_node in node.values[1:]:
                if not result:
                    return result
                result = self._ev(value_node)
            return result
        result = self._ev(node.values[0])
        for value_node in node.values[1:]:
            if result:
                return result
            result = self._ev(value_node)
        return result

    def _ev_BinOp(self, node):
        left = self._ev(node.left)
        right = self._ev(node.right)
        return self._apply_binop(node.op, left, right)

    def _apply_binop(self, op, left, right):
        if isinstance(op, ast.Add):
            return self._guard_binop(left, right, lambda a, b: a + b)
        if isinstance(op, ast.Sub):
            return self._guard_binop(left, right, lambda a, b: a - b)
        if isinstance(op, ast.Mult):
            return self._mult(left, right)
        if isinstance(op, ast.Div):
            return self._guard_binop(left, right, lambda a, b: a / b)
        if isinstance(op, ast.FloorDiv):
            return self._guard_binop(left, right, lambda a, b: a // b)
        if isinstance(op, ast.Mod):
            return self._guard_binop(left, right, lambda a, b: a % b)
        if isinstance(op, ast.Pow):
            return self._pow(left, right)
        if isinstance(op, ast.LShift):
            return self._shift(left, right, lambda a, b: a << b)
        if isinstance(op, ast.RShift):
            return self._shift(left, right, lambda a, b: a >> b)
        if isinstance(op, ast.BitAnd):
            return self._guard_binop(left, right, lambda a, b: a & b)
        if isinstance(op, ast.BitOr):
            return self._guard_binop(left, right, lambda a, b: a | b)
        if isinstance(op, ast.BitXor):
            return self._guard_binop(left, right, lambda a, b: a ^ b)
        raise _Denied("denied construct: operator")

    def _guard_binop(self, left, right, fn):
        try:
            result = fn(left, right)
        except (_Halt, _UserError):
            raise
        except _ALLOWED_EXC_TYPES as exc:
            raise _UserError(exc) from None
        except Exception:
            raise _Denied("operation failed") from None
        self._charge_value(self._norm_int(result))
        return result

    def _repeat_estimate(self, unit, times):
        if isinstance(times, bool) or not isinstance(times, int):
            return None
        if times <= 0:
            return 0
        if isinstance(unit, (str, bytes)):
            return 40 + len(unit) * times
        if isinstance(unit, (list, tuple)):
            return 56 + 8 * len(unit) * times
        return None

    def _mult(self, left, right):
        for unit, times in ((left, right), (right, left)):
            estimate = self._repeat_estimate(unit, times)
            if estimate is not None:
                if self.alloc + estimate > \
                        self.limits["max_allocation_bytes"]:
                    raise _Budget(
                        "allocation budget exceeded; chunk the work into "
                        "smaller pieces instead of building giant values "
                        "at once")
                result = self._guard(lambda a, b: a * b, left, right)
                self._charge_value(self._norm_int(result))
                return result
        if isinstance(left, int) and isinstance(right, int) \
                and not isinstance(left, bool) \
                and not isinstance(right, bool):
            if left.bit_length() + right.bit_length() > _INT_BIT_CAP + 64:
                raise _Budget("integer result too large; chunk the work "
                              "into smaller pieces")
        return self._guard_binop(left, right, lambda a, b: a * b)

    def _pow(self, left, right):
        if isinstance(left, int) and isinstance(right, int) \
                and not isinstance(left, bool) \
                and not isinstance(right, bool):
            if right >= 0 and left not in (0, 1, -1):
                if right * max(1, left.bit_length()) > _INT_BIT_CAP:
                    raise _Budget("integer result too large; chunk the "
                                  "work into smaller pieces")
            elif abs(right) > _INT_BIT_CAP and left not in (0, 1, -1):
                raise _Budget("integer result too large; chunk the work "
                              "into smaller pieces")
        return self._guard_binop(left, right, lambda a, b: a ** b)

    def _shift(self, left, right, fn):
        if isinstance(right, int) and not isinstance(right, bool) \
                and abs(right) > _INT_BIT_CAP:
            raise _Budget("integer result too large; chunk the work into "
                          "smaller pieces")
        return self._guard_binop(left, right, fn)

    def _ev_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            return not self._ev(node.operand)
        if isinstance(node.op, ast.USub):
            return self._guard_binop(0, self._ev(node.operand),
                                     lambda a, b: a - b)
        if isinstance(node.op, ast.UAdd):
            return self._guard_binop(0, self._ev(node.operand),
                                     lambda a, b: a + b)
        if isinstance(node.op, ast.Invert):
            operand = self._ev(node.operand)
            try:
                result = self._guard(lambda v: ~v, operand)
            except (_Halt, _UserError):
                raise
            self._charge_value(self._norm_int(result))
            return result
        raise _Denied("denied construct: operator")

    def _ev_Compare(self, node):
        left = self._ev(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            right = self._ev(comparator)
            if isinstance(op, ast.In):
                outcome = self._guard(lambda a, b: a in b, left, right)
            elif isinstance(op, ast.NotIn):
                outcome = self._guard(lambda a, b: a not in b, left, right)
            elif isinstance(op, ast.Is):
                outcome = left is right
            elif isinstance(op, ast.IsNot):
                outcome = left is not right
            elif isinstance(op, ast.Eq):
                outcome = self._guard(lambda a, b: a == b, left, right)
            elif isinstance(op, ast.NotEq):
                outcome = self._guard(lambda a, b: a != b, left, right)
            elif isinstance(op, ast.Lt):
                outcome = self._guard(lambda a, b: a < b, left, right)
            elif isinstance(op, ast.LtE):
                outcome = self._guard(lambda a, b: a <= b, left, right)
            elif isinstance(op, ast.Gt):
                outcome = self._guard(lambda a, b: a > b, left, right)
            elif isinstance(op, ast.GtE):
                outcome = self._guard(lambda a, b: a >= b, left, right)
            else:
                raise _Denied("denied construct: operator")
            if not outcome:
                return False
            left = right
        return True

    def _ev_IfExp(self, node):
        if self._ev(node.test):
            return self._ev(node.body)
        return self._ev(node.orelse)

    def _ev_NamedExpr(self, node):
        value = self._ev(node.value)
        self._assign(node.target, value)
        return value

    def _ev_JoinedStr(self, node):
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(str(part.value))
            else:
                value = self._ev(part.value)
                if part.conversion == 115:
                    text = self._guard(str, value)
                elif part.conversion == 114:
                    text = self._guard(repr, value)
                elif part.conversion == 97:
                    text = self._guard(ascii, value)
                else:
                    text = self._guard(str, value)
                if part.format_spec is not None:
                    spec = self._ev(part.format_spec)
                    text = self._guard(format, value, spec)
                parts.append(text)
        result = "".join(parts)
        self._charge(40 + len(result))
        return result

    def _ev_Starred(self, node):
        raise _Denied("denied construct: starred expression")

    def _ev_Lambda(self, node):
        return _Lambda(node.args, node.body, list(self.scopes))

    def _ev_If(self, node):
        raise _Denied("denied construct: If as expression")

    # -- iteration helpers -------------------------------------------------
    def _safe_iter(self, value):
        try:
            return iter(value)
        except TypeError:
            raise _UserError(TypeError(
                "object is not iterable")) from None

    # -- builtins ------------------------------------------------------------
    def _call_builtin(self, name, args, kwargs):
        method = getattr(self, "_bi_" + name, None)
        if method is None:
            raise _Denied(f"unknown name: '{name}'")
        try:
            return method(*args, **kwargs)
        except (_Halt, _UserError):
            raise
        except _ALLOWED_EXC_TYPES as exc:
            raise _UserError(exc) from None
        except Exception:
            raise _Denied("operation failed") from None

    def _checked_args(self, name, args, minimum, maximum):
        if not minimum <= len(args) <= maximum:
            raise _UserError(TypeError(
                f"{name} takes {minimum}-{maximum} arguments "
                f"({len(args)} given)"))

    def _bi_len(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("len takes no keyword arguments"))
        self._checked_args("len", args, 1, 1)
        return len(args[0])

    def _bi_range(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("range takes no keyword arguments"))
        self._checked_args("range", args, 1, 3)
        for arg in args:
            if isinstance(arg, bool) or not isinstance(arg, int):
                raise _UserError(TypeError("range needs integers"))
            if abs(arg) > self.limits["max_iterations"]:
                raise _Budget(
                    "range() argument too large for the iteration budget "
                    f"({self.limits['max_iterations']}); chunk the work "
                    "into smaller pieces")
        if len(args) == 3 and args[2] == 0:
            raise _UserError(ValueError("range() step cannot be zero"))
        return range(*args)

    def _bi_sum(self, *args, **kwargs):
        start = kwargs.pop("start", 0)
        if kwargs:
            raise _UserError(TypeError("sum takes only 'start' as keyword"))
        self._checked_args("sum", args, 1, 1)
        total = start
        for item in self._safe_iter(args[0]):
            self._tick()
            try:
                total = total + item
            except (_Halt, _UserError):
                raise
            except _ALLOWED_EXC_TYPES as exc:
                raise _UserError(exc) from None
            except Exception:
                raise _Denied("operation failed") from None
            self._charge_value(self._norm_int(total))
        return total

    def _bi_min(self, *args, **kwargs):
        return self._minmax("min", args, kwargs)

    def _bi_max(self, *args, **kwargs):
        return self._minmax("max", args, kwargs)

    def _minmax(self, name, args, kwargs):
        key = kwargs.pop("key", None)
        if kwargs:
            raise _UserError(TypeError(f"{name} takes only 'key' as keyword"))
        if key is not None and not isinstance(
                key, (_Function, _Lambda, _Builtin)):
            raise _UserError(TypeError("'key' is not callable"))
        if len(args) == 1:
            items = self._safe_iter(args[0])
        elif len(args) > 1:
            items = iter(args)
        else:
            raise _UserError(TypeError(f"{name} needs at least one argument"))

        def sort_key(item):
            return self._call_value(key, [item], {}) if key else item

        best = None
        best_key = None
        found = False
        for item in items:
            self._tick()
            candidate = self._guard(sort_key, item)
            if not found or (name == "min") == bool(
                    self._guard(lambda a, b: a < b, candidate, best_key)):
                best, best_key = item, candidate
                found = True
        if not found:
            raise _UserError(ValueError(f"{name}() of empty sequence"))
        return best

    def _bi_sorted(self, *args, **kwargs):
        key = kwargs.pop("key", None)
        reverse = kwargs.pop("reverse", False)
        if kwargs:
            raise _UserError(TypeError(
                "sorted takes only 'key'/'reverse' as keywords"))
        self._checked_args("sorted", args, 1, 1)
        if key is not None and not isinstance(
                key, (_Function, _Lambda, _Builtin)):
            raise _UserError(TypeError("'key' is not callable"))
        items = []
        for item in self._safe_iter(args[0]):
            self._tick()
            items.append(item)
        if key is not None:
            def sort_key(item):
                return self._call_value(key, [item], {})
            result = self._guard(sorted, items, key=sort_key,
                                 reverse=bool(reverse))
        else:
            result = self._guard(sorted, items, reverse=bool(reverse))
        self._charge(56 + 8 * len(result))
        return result

    def _bi_abs(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("abs takes no keyword arguments"))
        self._checked_args("abs", args, 1, 1)
        result = abs(args[0]) if isinstance(args[0], (int, float)) \
            and not isinstance(args[0], bool) else self._guard(abs, args[0])
        self._charge_value(self._norm_int(result))
        return result

    def _bi_round(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("round takes no keyword arguments"))
        self._checked_args("round", args, 1, 2)
        if len(args) == 2 and (isinstance(args[1], bool)
                               or not isinstance(args[1], int)):
            raise _UserError(TypeError("round needs an int ndigits"))
        result = self._guard(round, *args)
        self._charge_value(self._norm_int(result))
        return result

    def _bi_enumerate(self, *args, **kwargs):
        start = kwargs.pop("start", 0)
        if kwargs:
            raise _UserError(TypeError(
                "enumerate takes only 'start' as keyword"))
        self._checked_args("enumerate", args, 1, 1)
        if isinstance(start, bool) or not isinstance(start, int):
            raise _UserError(TypeError("enumerate needs an int start"))
        return self._guard(enumerate, args[0], start)

    def _bi_zip(self, *args, **kwargs):
        strict = kwargs.pop("strict", False)
        if kwargs:
            raise _UserError(TypeError(
                "zip takes only 'strict' as keyword"))
        if args:
            for arg in args:
                self._guard(iter, arg)
        try:
            return zip(*args, strict=bool(strict))
        except (_Halt, _UserError):
            raise
        except _ALLOWED_EXC_TYPES as exc:
            raise _UserError(exc) from None
        except Exception:
            raise _Denied("operation failed") from None

    def _bi_str(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("str takes no keyword arguments"))
        self._checked_args("str", args, 0, 1)
        result = self._guard(str, *args)
        self._charge(40 + len(result))
        return result

    def _bi_int(self, *args, **kwargs):
        base = kwargs.pop("base", 10)
        if kwargs:
            raise _UserError(TypeError("int takes only 'base' as keyword"))
        self._checked_args("int", args, 0, 2)
        full = list(args)
        if len(full) == 2:
            base = full.pop()
        if isinstance(base, bool) or not isinstance(base, int):
            raise _UserError(TypeError("int needs an int base"))
        if len(args) == 2:
            result = self._guard(int, full[0], base)
        elif full:
            result = self._guard(int, full[0])
        else:
            result = 0
        return self._norm_int(result)

    def _bi_float(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("float takes no keyword arguments"))
        self._checked_args("float", args, 0, 1)
        result = self._guard(float, *args)
        self._charge(24)
        return result

    def _bi_bool(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("bool takes no keyword arguments"))
        self._checked_args("bool", args, 0, 1)
        return self._guard(bool, *args)

    def _bi_list(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("list takes no keyword arguments"))
        self._checked_args("list", args, 0, 1)
        if not args:
            return []
        items = []
        for item in self._safe_iter(args[0]):
            self._tick()
            items.append(item)
        self._charge(56 + 8 * len(items))
        return items

    def _bi_tuple(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("tuple takes no keyword arguments"))
        self._checked_args("tuple", args, 0, 1)
        if not args:
            return ()
        items = []
        for item in self._safe_iter(args[0]):
            self._tick()
            items.append(item)
        result = tuple(items)
        self._charge(56 + 8 * len(result))
        return result

    def _bi_set(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("set takes no keyword arguments"))
        self._checked_args("set", args, 0, 1)
        if not args:
            return set()
        items = []
        for item in self._safe_iter(args[0]):
            self._tick()
            self._guard(_hashable, item)
            items.append(item)
        result = set(items)
        self._charge(200 + 32 * len(result))
        return result

    def _bi_dict(self, *args, **kwargs):
        if len(args) > 1:
            raise _UserError(TypeError("dict takes at most 1 argument"))
        result = {}
        if args:
            source = args[0]
            if isinstance(source, dict):
                pairs = list(source.items())
            else:
                pairs = []
                for pair in self._safe_iter(source):
                    self._tick()
                    seq = self._guard(list, pair)
                    if len(seq) != 2:
                        raise _UserError(ValueError(
                            "dict update sequence has wrong length"))
                    pairs.append((seq[0], seq[1]))
            self._tick(len(pairs))
            for key, value in pairs:
                result[self._guard(_hashable, key)] = value
        for key, value in kwargs.items():
            result[key] = value
        self._charge(64 + 72 * len(result))
        return result

    # -- host data helpers (parse_json / parse_csv / rows) -------------------
    def _check_delimiter(self, delimiter):
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise _UserError(TypeError(
                "delimiter must be a single-character string"))
        return delimiter

    def _csv_records(self, text, delimiter):
        """Parse header-first CSV ``text`` into a list of plain dicts.

        Every parsed row consumes one shared-iteration tick (so chunked
        inputs stay bounded by the same budget as loops) and is charged
        to the allocation budget. Ragged-short fields stay ``None``
        (JSON ``null``); ragged-long extras (``None`` key) are dropped.
        """
        try:
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            if reader.fieldnames is None:
                return []
            out = []
            for record in reader:
                self._tick()
                row = {}
                for key, val in record.items():
                    if key is None:
                        continue
                    row[str(key)] = val
                self._charge(64 + 72 * len(row) + sum(
                    len(v) for v in row.values() if isinstance(v, str)))
                out.append(row)
            return out
        except (_Halt, _UserError):
            raise
        except _ALLOWED_EXC_TYPES as exc:
            raise _UserError(exc) from None
        except Exception:
            raise _Denied("operation failed") from None

    def _bi_parse_json(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError(
                "parse_json takes no keyword arguments"))
        self._checked_args("parse_json", args, 1, 1)
        text = args[0]
        if not isinstance(text, str):
            raise _UserError(TypeError("parse_json needs a string"))
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise _UserError(exc) from None
        except (_Halt, _UserError):
            raise
        except Exception:
            raise _Denied("operation failed") from None
        self._charge_deep(value)
        return value

    def _bi_parse_csv(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError(
                "parse_csv takes no keyword arguments"))
        self._checked_args("parse_csv", args, 1, 2)
        text = args[0]
        if not isinstance(text, str):
            raise _UserError(TypeError("parse_csv needs a string"))
        delimiter = self._check_delimiter(
            args[1] if len(args) > 1 else ",")
        return self._csv_records(text, delimiter)

    def _bi_rows(self, *args, **kwargs):
        if kwargs:
            raise _UserError(TypeError("rows takes no keyword arguments"))
        self._checked_args("rows", args, 1, 2)
        name = args[0]
        if not isinstance(name, str):
            raise _UserError(TypeError("rows needs a file name string"))
        delimiter = self._check_delimiter(
            args[1] if len(args) > 1 else ",")
        if name not in self.files:
            raise _UserError(KeyError(name))
        chunks = self.files[name]
        out = []
        for chunk in chunks:
            self._tick()
            self._check_wall()
            out.extend(self._csv_records(chunk, delimiter))
        return out

    # -- whitelisted methods -----------------------------------------------
    def _call_method(self, bound, args, kwargs):
        obj, name = bound.obj, bound.name
        allowed = _METHODS.get(type(obj))
        if not allowed or name not in allowed:
            raise _Denied(f"attribute '{name}' is not allowed")
        try:
            if isinstance(obj, str):
                return self._str_method(obj, name, args, kwargs)
            if isinstance(obj, list):
                return self._list_method(obj, name, args, kwargs)
            return self._dict_method(obj, name, args, kwargs)
        except (_Halt, _UserError):
            raise
        except _ALLOWED_EXC_TYPES as exc:
            raise _UserError(exc) from None
        except Exception:
            raise _Denied("operation failed") from None

    def _no_kwargs(self, name, kwargs):
        if kwargs:
            raise _UserError(TypeError(f"{name} takes no keyword arguments"))

    def _str_method(self, obj, name, args, kwargs):
        if name == "split":
            self._no_kwargs("split", kwargs)
            self._checked_args("split", args, 0, 2)
            sep = args[0] if len(args) > 0 else None
            if sep is not None and not isinstance(sep, str):
                raise _UserError(TypeError("split separator must be str"))
            maxsplit = args[1] if len(args) > 1 else -1
            if isinstance(maxsplit, bool) or not isinstance(maxsplit, int):
                raise _UserError(TypeError("split maxsplit must be int"))
            parts = obj.split(sep, maxsplit)
            self._tick(len(parts))
            self._charge(56 + 8 * len(parts)
                         + sum(len(p) for p in parts))
            return parts
        if name == "join":
            self._no_kwargs("join", kwargs)
            self._checked_args("join", args, 1, 1)
            items = []
            for item in self._safe_iter(args[0]):
                self._tick()
                if not isinstance(item, str):
                    raise _UserError(TypeError(
                        "join expects str items"))
                items.append(item)
            result = obj.join(items)
            self._charge(40 + len(result))
            return result
        if name == "strip":
            self._no_kwargs("strip", kwargs)
            self._checked_args("strip", args, 0, 1)
            chars = args[0] if args else None
            if chars is not None and not isinstance(chars, str):
                raise _UserError(TypeError("strip chars must be str"))
            result = obj.strip(chars)
            self._charge(40 + len(result))
            return result
        if name == "replace":
            self._no_kwargs("replace", kwargs)
            self._checked_args("replace", args, 2, 3)
            old, new = args[0], args[1]
            if not isinstance(old, str) or not isinstance(new, str):
                raise _UserError(TypeError("replace needs str arguments"))
            count = args[2] if len(args) > 2 else -1
            if isinstance(count, bool) or not isinstance(count, int):
                raise _UserError(TypeError("replace count must be int"))
            result = obj.replace(old, new, count)
            self._charge(40 + len(result))
            return result
        if name in ("startswith", "endswith"):
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 1, 3)
            prefix = args[0]
            if isinstance(prefix, str):
                ok_prefix = True
            elif isinstance(prefix, tuple) and all(
                    isinstance(p, str) for p in prefix):
                ok_prefix = True
            else:
                ok_prefix = False
            if not ok_prefix:
                raise _UserError(TypeError(f"{name} needs str"))
            rest = args[1:]
            for bound_arg in rest:
                if isinstance(bound_arg, bool) or not isinstance(
                        bound_arg, int):
                    raise _UserError(TypeError(
                        f"{name} bounds must be int"))
            check = getattr(obj, name)
            return check(*args)
        if name == "format":
            for key in kwargs:
                if not isinstance(key, str):
                    raise _UserError(TypeError("format keywords need str"))
            _validate_format_template(obj)
            result = obj.format(*args, **kwargs)
            self._charge(40 + len(result))
            return result
        raise _Denied(f"attribute '{name}' is not allowed")

    def _list_method(self, obj, name, args, kwargs):
        if name == "append":
            self._no_kwargs("append", kwargs)
            self._checked_args("append", args, 1, 1)
            self._charge(40)
            obj.append(args[0])
            return None
        if name == "extend":
            self._no_kwargs("extend", kwargs)
            self._checked_args("extend", args, 1, 1)
            items = []
            for item in self._safe_iter(args[0]):
                self._tick()
                items.append(item)
            self._charge(8 * len(items))
            obj.extend(items)
            return None
        raise _Denied(f"attribute '{name}' is not allowed")

    def _dict_method(self, obj, name, args, kwargs):
        if name == "get":
            self._no_kwargs("get", kwargs)
            self._checked_args("get", args, 1, 2)
            try:
                return obj.get(args[0]) if len(args) == 1 else obj.get(
                    args[0], args[1])
            except TypeError as exc:
                raise _UserError(exc) from None
        if name in ("items", "keys", "values"):
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 0, 0)
            return getattr(obj, name)()
        if name == "update":
            if len(args) > 1:
                raise _UserError(TypeError("update takes at most "
                                           "1 argument"))
            pairs = []
            if args:
                source = args[0]
                if isinstance(source, dict):
                    pairs = list(source.items())
                else:
                    for pair in self._safe_iter(source):
                        self._tick()
                        seq = self._guard(list, pair)
                        if len(seq) != 2:
                            raise _UserError(ValueError(
                                "dict update sequence has wrong length"))
                        pairs.append((seq[0], seq[1]))
            pairs.extend(kwargs.items())
            self._tick(len(pairs))
            for key, value in pairs:
                obj[self._guard(_hashable, key)] = value
            self._charge(72 * len(pairs))
            return None
        raise _Denied(f"attribute '{name}' is not allowed")


_MISSING = object()


def _getitem(obj, index):
    return obj[index]


def _setitem(obj, index, value):
    obj[index] = value


def _hashable(value):
    hash(value)
    return value


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _merge_limits(limits):
    merged = dict(DEFAULT_LIMITS)
    if limits is None:
        return merged, None
    if not isinstance(limits, dict):
        return None, _fail("limits must be a dict")
    for key, value in limits.items():
        if key in merged:
            try:
                merged[key] = int(value)
            except (TypeError, ValueError):
                return None, _fail(f"limit '{key}' must be an integer")
    return merged, None


def evaluate(code, variables=None, files=None, limits=None):
    """Run ``code`` in the sealed evaluator.

    Returns ``{"ok": True, "value": ...}`` or ``{"ok": False, "error": ...}``.
    """
    try:
        if not isinstance(code, str):
            return _fail("code must be a string")
        if len(code) > _CODE_MAX_CHARS:
            return _fail("code is too large")
        merged, error = _merge_limits(limits)
        if error is not None:
            return error
        if variables is None:
            variables = {}
        if not isinstance(variables, dict):
            return _fail("variables must be a dict")
        clean_vars = {}
        for key, value in variables.items():
            if not isinstance(key, str) or not key.isidentifier() \
                    or keyword.iskeyword(key) or key.startswith("__"):
                return _fail("bad variable name")
            try:
                clean_vars[key] = copy.deepcopy(value)
            except Exception:
                return _fail("variables must hold plain JSON values")
        if files is None:
            files = {}
        if not isinstance(files, dict):
            return _fail("files must be a dict")
        for key, chunks in files.items():
            if not isinstance(key, str) or not isinstance(chunks, list) \
                    or not all(isinstance(c, str) for c in chunks):
                return _fail("files must map names to lists of text chunks")
        try:
            tree = ast.parse(code)
        except (SyntaxError, ValueError):
            return _fail("invalid syntax")
        except (RecursionError, MemoryError):
            return _fail("code is too deeply nested")
        try:
            _Validator(merged["max_ast_nodes"]).visit(tree)
        except (_Denied, _Budget) as exc:
            return _fail(str(exc))
        try:
            runner = _Interpreter(clean_vars, files, merged)
            result = runner.run(tree)
            runner._check_wall()
        except (_Denied, _Budget) as exc:
            return _fail(str(exc))
        except _UserError as exc:
            return _fail(f"{type(exc.exc).__name__}: {exc.exc}")
        try:
            serialized = json.dumps(result, allow_nan=False)
        except (TypeError, ValueError):
            return _fail("result is not JSON-serializable")
        if len(serialized) > merged["max_output_chars"]:
            return _fail(f"result exceeds output budget ({len(serialized)} "
                         f"chars, cap {merged['max_output_chars']})")
        return {"ok": True, "value": result}
    except (_Denied, _Budget) as exc:
        return _fail(str(exc))
    except _UserError as exc:
        return _fail(f"{type(exc.exc).__name__}: {exc.exc}")
    except (RecursionError, MemoryError):
        return _fail("evaluation exceeded interpreter limits")
    except Exception:
        return _fail("evaluation failed")
