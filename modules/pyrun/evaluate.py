"""Sealed stdlib-``ast`` evaluator core for the ``pyrun`` sandbox module.

A small walking interpreter over :func:`ast.parse` output. Only an explicit
whitelist executes; everything else fails closed with a safe message (no
tracebacks, no host paths).

Semantics (binding for this task):

* Inputs: ``code`` (str), ``variables`` (name -> JSON value), ``files``
  (name -> list of text chunks; the first chunk carries the CSV header
  and later chunks hold data lines for that header, per the ``rows()``
  host helper below), ``limits`` (partial dict merged over
  :data:`DEFAULT_LIMITS`).
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
  list (the header comes from the first chunk only and is passed as
  ``fieldnames`` for the later data-only chunks; a repeated header line
  at the top of a later chunk is defensively skipped. Every parsed
  row consumes one shared-iteration tick and is charged to the allocation
  budget, and each chunk read checks the wall clock). All three return
  plain JSON-serializable lists/dicts. Helpers ride the variables-style
  namespace layer: an explicit user binding shadows them (so a `rows`
  data variable keeps working), while builtins still take precedence
  over everything.
* SQLite scratchpad: each run lazily seeds a throwaway ``:memory:``
  database from ``files`` (tables ``t0``, ``t1``, ... in ``files``
  insertion order, every column ``TEXT``) on first ``db``/``rows()``
  access, and exposes it as ``db`` with exactly
  two host functions — ``db.query(sql[, params])`` runs one
  ``SELECT``/``WITH`` statement and returns a list of plain dicts;
  ``db.exec(sql[, params])`` runs one write/DDL statement
  (``INSERT``/``UPDATE``/``DELETE``/``CREATE TABLE|INDEX``/
  ``DROP TABLE|INDEX``) and returns ``{"rows_affected": int}``. ``db``
  rides the same variables layer as the helpers above (an explicit
  user binding shadows it; builtins still take absolute precedence).
  ``ATTACH`` / ``DETACH`` / ``PRAGMA`` / ``VACUUM`` / ``load_extension``
  in any spelling and multi-statement strings fail closed (a
  case-insensitive keyword pre-filter plus a ``set_authorizer`` deny,
  explicit ``enable_load_extension(False)``, and single-``execute``
  only). Every materialized row ticks the shared iteration budget
  once, is allocation-charged, and fetching checks the wall clock per
  batch. SQLite errors surface as a safe single-line message.
* Namespace hygiene: whitelisted builtins take precedence over every
  user binding (scopes and tool-layer variables alike), so sandboxed code
  can never shadow or neutralize one; the sandbox never sees host
  ``__builtins__`` at all (unknown/dunder names fail closed); nested
  ``str.format`` specs are validated recursively for the same reason.

Only the standard library is used (``ast``, ``copy``, ``csv``, ``io``,
``json``, ``keyword``, ``re``, ``sqlite3``, ``string``, ``time``).
"""

import ast
import copy
import csv
import io
import json
import keyword
import re
import sqlite3
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
    "Exception",
})
_ALLOWED_EXC_TYPES = (ValueError, ZeroDivisionError, KeyError, IndexError,
                      TypeError)

_BUILTIN_NAMES = frozenset({
    "len", "range", "sum", "min", "max", "sorted", "abs", "round",
    "enumerate", "zip", "str", "int", "float", "bool",
    "list", "dict", "set", "tuple",
    "isinstance", "reversed", "divmod", "any", "all",
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
                    "endswith", "format", "lower", "upper", "lstrip",
                    "rstrip", "splitlines", "count", "find", "partition",
                    "isdigit", "isnumeric", "title", "rfind",
                    "rpartition", "index"}),
    list: frozenset({"append", "extend", "sort", "count", "index"}),
    dict: frozenset({"get", "items", "keys", "values", "update",
                     "setdefault"}),
}

# Sealed SQLite scratchpad: one throwaway `:memory:` DB per run.
_SQL_MAX_CHARS = 20000
_SQL_SEED_BATCH = 500
_SQL_DENIED_RE = re.compile(
    r"\bATTACH\b|\bDETACH\b|\bPRAGMA\b|\bVACUUM\b|load_extension"
    r"|\bpragma_",
    re.IGNORECASE)


class _Db:
    """Sandbox-visible SQLite handle: exactly two plain host functions.

    The connection is closed over in the host closures held here, never
    stored on the object; sandboxed code reaches it only through the
    interpreter's ``query`` / ``exec`` dispatch.
    """

    __slots__ = ("query", "exec")

    def __init__(self, query, exec_):
        self.query = query
        self.exec = exec_

    def __repr__(self):
        # Address-free: sandboxed str(db)/f"{db!r}"/"{}".format(db) must
        # never leak a host memory address.
        return "<db>"


def _sqlite_authorizer(action, arg1, arg2, dbname, source):
    """Layer-2 lockdown: deny attach/detach and load-extension opcodes."""
    try:
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        for part in (arg1, arg2, dbname, source):
            if isinstance(part, str) and "load_extension" in part.lower():
                return sqlite3.SQLITE_DENY
    except Exception:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _sqlite_error(exc):
    """Safe single-line message for a SQLite failure (no paths)."""
    try:
        line = str(exc).split("\n", 1)[0].strip()
    except Exception:
        line = ""
    return line or "database operation failed"


def _open_scratch_db():
    """Host-side `:memory:` DB: pragma + extension lock + authorizer."""
    try:
        conn = sqlite3.connect(":memory:")
    except (sqlite3.Error, sqlite3.Warning) as exc:
        raise _Denied(_sqlite_error(exc)) from None
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.enable_load_extension(False)
        conn.set_authorizer(_sqlite_authorizer)
    except (AttributeError, sqlite3.Error, sqlite3.Warning) as exc:
        try:
            conn.close()
        except Exception:
            pass
        raise _Denied(_sqlite_error(exc)) from None
    return conn


def _quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def _sanitize_columns(header):
    """Deterministic header → safe column names.

    Non-``[A-Za-z0-9_]`` chars become ``_``; empty names become ``c<N>``
    (0-based position); leading digits gain a ``_`` prefix; collisions
    gain ``_2``, ``_3`` ... suffixes.
    """
    used = set()
    out = []
    for index, raw in enumerate(header):
        name = re.sub(r"[^A-Za-z0-9_]", "_", raw or "")
        if not name:
            name = f"c{index}"
        elif name[0].isdigit():
            name = "_" + name
        base, suffix = name, 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        out.append(name)
    return out


def _sql_words(sql, count=2):
    """First ``count`` words of a statement, skipping space/comments."""
    text = sql.lstrip()
    while True:
        if text.startswith("--"):
            _, _, text = text.partition("\n")
            text = text.lstrip()
        elif text.startswith("/*"):
            end = text.find("*/", 2)
            if end == -1:
                return []
            text = text[end + 2:].lstrip()
        else:
            break
    return re.findall(r"[A-Za-z]+", text)[:count]


def _skip_sql_ws(sql, pos):
    """Advance past whitespace and `--` / `/* */` comments."""
    n = len(sql)
    while pos < n:
        if sql[pos].isspace():
            pos += 1
        elif sql.startswith("--", pos):
            end = sql.find("\n", pos)
            pos = n if end == -1 else end + 1
        elif sql.startswith("/*", pos):
            end = sql.find("*/", pos + 2)
            if end == -1:
                return n
            pos = end + 2
        else:
            break
    return pos


_IDENT_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|\"(?:[^\"]|\"\")+\"|`[^`]*`|\[[^\]]*\]")


def _skip_parens(sql, pos):
    """Skip a balanced ``(...)`` group from its opening paren.

    Quote- and comment-aware. Returns the index just past the closing
    ``)``, or ``-1`` when unbalanced.
    """
    n = len(sql)
    depth = 0
    quote = None
    i = pos
    while i < n:
        if quote is not None:
            if sql[i] == quote:
                if i + 1 < n and sql[i + 1] == quote \
                        and quote in ("'", '"', '`'):
                    i += 1
                else:
                    quote = None
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                return -1
            i = end + 2
            continue
        ch = sql[i]
        if ch in ("'", '"', '`'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _with_main_verb(sql):
    """First word of the statement a ``WITH`` CTE list leads into.

    Skips ``[RECURSIVE]`` and each ``name [(cols)] AS [MATERIALIZED]
    (...)`` definition (balanced, quote-aware). Returns the upper-cased
    verb (e.g. ``"SELECT"``) or ``""`` when unparseable — callers fail
    closed on anything but ``SELECT``.
    """
    pos = _skip_sql_ws(sql, 0)
    match = re.match(r"WITH\b", sql[pos:], re.IGNORECASE)
    if not match:
        return ""
    pos = _skip_sql_ws(sql, pos + match.end())
    match = re.match(r"RECURSIVE\b", sql[pos:], re.IGNORECASE)
    if match:
        pos = _skip_sql_ws(sql, pos + match.end())
    while True:
        match = _IDENT_RE.match(sql, pos)
        if not match:
            return ""
        pos = _skip_sql_ws(sql, match.end())
        if pos < len(sql) and sql[pos] == "(":
            end = _skip_parens(sql, pos)
            if end == -1:
                return ""
            pos = _skip_sql_ws(sql, end)
        match = re.match(r"AS\b", sql[pos:], re.IGNORECASE)
        if not match:
            return ""
        pos = _skip_sql_ws(sql, pos + match.end())
        match = re.match(r"(?:NOT\s+MATERIALIZED|MATERIALIZED)\b",
                         sql[pos:], re.IGNORECASE)
        if match:
            pos = _skip_sql_ws(sql, pos + match.end())
        if pos >= len(sql) or sql[pos] != "(":
            return ""
        end = _skip_parens(sql, pos)
        if end == -1:
            return ""
        pos = _skip_sql_ws(sql, end)
        if pos < len(sql) and sql[pos] == ",":
            pos = _skip_sql_ws(sql, pos + 1)
            continue
        break
    match = re.match(r"[A-Za-z]+", sql[pos:])
    return match.group(0).upper() if match else ""


_FORMATTER = string.Formatter()

# Width/precision pre-check for str.format (mirrors the _mult pattern):
# digit runs inside a single format-spec level, with nested {...} fields
# stripped (they resolve from arguments and are handled separately).
_FORMAT_NUM_RE = re.compile(r"\d+")
_FORMAT_NESTED_RE = re.compile(r"\{[^{}]*\}")


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
    def __init__(self, variables, files, limits, db_conn):
        self.limits = limits
        self.variables = variables
        self.files = files
        self._db_conn = db_conn
        self.builtins = {name: _Builtin(name) for name in _BUILTIN_NAMES}
        self.host_helpers = {name: _Builtin(name)
                             for name in _HOST_HELPERS}
        # `db` rides the same variables layer as the helpers above: an
        # explicit user binding (scope assignment or tool-layer variable)
        # shadows it, while whitelisted builtins still take precedence.
        self.host_helpers["db"] = _Db(
            lambda *args, **kwargs: self._db_query(*args, **kwargs),
            lambda *args, **kwargs: self._db_exec(*args, **kwargs))
        self.scopes = [{}]  # module-level env; functions push more
        self.ticks = 0
        self.alloc = 0
        self.depth = 0
        self.start = time.monotonic()
        self.last_value = None
        self._db_seeded = False
        # Engine-side backstop: Python-level tick/wall checks only run
        # between fetched rows, so an unbounded statement (recursive CTE
        # / cross join feeding one aggregate row) would never reach them.
        # The handler runs inside the engine every 1000 VM steps and
        # interrupts the statement once the wall-clock budget is gone.
        self._db_conn.set_progress_handler(self._db_progress, 1000)

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
        # NOTE: `>=` (not `>`): a zero budget must trip at the first timed
        # check, and on coarse clocks (Windows GetTickCount64: 15.6ms) a
        # fast program otherwise reads elapsed == 0.0 and never trips.
        elapsed = time.monotonic() - self.start
        if elapsed >= self.limits["max_wall_seconds"]:
            raise _Budget(
                f"wall-clock budget exceeded ({elapsed:.1f}s of "
                f"{self.limits['max_wall_seconds']}s); chunk the "
                "input and combine partial results")

    def _db_progress(self):
        """SQLite progress callback: abort engine-side hangs on wall expiry.

        Returning nonzero interrupts the running statement (surfaces as
        ``interrupted``), so an unbounded ``WITH RECURSIVE`` / cross join
        cannot spin inside the engine past the wall-clock budget.
        """
        try:
            self._check_wall()
        except _Budget:
            return 1
        return 0

    def _ensure_db_seeded(self):
        """Seed file tables on first data access, not at startup."""
        if not self._db_seeded:
            self._db_seeded = True
            self._seed_db()

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
            self._charge_referenced(value)
        elif isinstance(value, dict):
            self._charge(64 + 72 * len(value))
            self._charge_referenced(value)

    def _charge_referenced(self, container):
        """Charge bytes a container's members reference, per reference path.

        ``[s] * 1000`` and ``[inner] * 1000`` hold 1000 references each:
        serializing or copying the list expands every slot, so the cost
        of a shared subtree counts once per path (multiplicity) — that is
        what trips the amplification instead of OOMing later in
        ``json.dumps``. Cycles terminate via the current-path ancestry
        stack (a back edge charges a small constant instead of
        recursing), not via a global visited set: a global dedupe would
        visit shared subtrees once and undercharge every path but one.
        """
        if isinstance(container, dict):
            members = list(container.keys()) + list(container.values())
        else:
            members = list(container)
        stack = [(members, (id(container),))]
        while stack:
            pending, ancestry = stack.pop()
            for member in pending:
                if isinstance(member, (list, tuple, set, dict)):
                    if id(member) in ancestry:
                        self._charge(16)
                        continue
                    if isinstance(member, dict):
                        self._charge(64 + 72 * len(member))
                        stack.append((
                            list(member.keys()) + list(member.values()),
                            ancestry + (id(member),)))
                    else:
                        self._charge(56 + 8 * len(member))
                        stack.append((list(member),
                                      ancestry + (id(member),)))
                else:
                    self._charge_value(member)

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
            # Every stored reference counts per path (multiplicity), so a
            # subscript-store loop cannot accumulate uncharged sharing.
            # Charged after success: failed stores stay catchable errors.
            self._charge_value(index)
            self._charge_value(value)
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
                       "TypeError": TypeError, "Exception": Exception}
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
        if isinstance(obj, _Db):
            if node.attr not in ("query", "exec"):
                raise _Denied(f"attribute '{node.attr}' is not allowed")
            return _BoundMethod(obj, node.attr)
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
        self._charge_referenced(flat)
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
        self._charge_referenced(result)
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
        self._charge_referenced(result)
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
        self._charge_referenced(result)
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
                    self._check_format_result(
                        sum(len(p) for p in parts) + len(text), spec)
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

    _ISINSTANCE_TYPES = {
        "int": int, "str": str, "float": float, "bool": bool,
        "list": list, "dict": dict, "set": set, "tuple": tuple,
    }

    def _bi_isinstance(self, *args, **kwargs):
        self._no_kwargs("isinstance", kwargs)
        self._checked_args("isinstance", args, 2, 2)
        obj, types = args
        if isinstance(types, _Builtin):
            candidates = (types,)
        elif isinstance(types, tuple):
            candidates = types
        else:
            raise _UserError(TypeError(
                "isinstance second argument must be a type or "
                "a tuple of types"))
        resolved = []
        for candidate in candidates:
            if isinstance(candidate, _Builtin) and candidate.name in \
                    self._ISINSTANCE_TYPES:
                resolved.append(self._ISINSTANCE_TYPES[candidate.name])
            else:
                raise _UserError(TypeError(
                    "isinstance second argument must be a type or "
                    "a tuple of types"))
        return self._guard(isinstance, obj, resolved[0] if len(resolved) == 1
                           and isinstance(types, _Builtin)
                           else tuple(resolved))

    def _bi_reversed(self, *args, **kwargs):
        self._no_kwargs("reversed", kwargs)
        self._checked_args("reversed", args, 1, 1)
        seq = args[0]
        if not isinstance(seq, (str, list, tuple, range)):
            raise _UserError(TypeError(
                "reversed needs a reversible sequence"))
        return self._guard(reversed, seq)

    def _bi_divmod(self, *args, **kwargs):
        self._no_kwargs("divmod", kwargs)
        self._checked_args("divmod", args, 2, 2)
        for operand in args:
            if isinstance(operand, bool) or not isinstance(
                    operand, (int, float)):
                raise _UserError(TypeError("divmod needs numbers"))
        result = self._guard(divmod, *args)
        checked = tuple(self._norm_int(value) for value in result)
        self._charge(56 + 8 * len(checked))
        for value in checked:
            self._charge_value(value)
        return checked

    def _bi_any(self, *args, **kwargs):
        self._no_kwargs("any", kwargs)
        self._checked_args("any", args, 1, 1)
        for item in self._safe_iter(args[0]):
            self._tick()
            if item:
                return True
        return False

    def _bi_all(self, *args, **kwargs):
        self._no_kwargs("all", kwargs)
        self._checked_args("all", args, 1, 1)
        for item in self._safe_iter(args[0]):
            self._tick()
            if not item:
                return False
        return True

    # -- host data helpers (parse_json / parse_csv / rows) -------------------
    def _check_delimiter(self, delimiter):
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise _UserError(TypeError(
                "delimiter must be a single-character string"))
        return delimiter

    def _csv_records(self, text, delimiter, fieldnames=None):
        """Parse header-first CSV ``text`` into a list of plain dicts.

        Every parsed row consumes one shared-iteration tick (so chunked
        inputs stay bounded by the same budget as loops) and is charged
        to the allocation budget. Ragged-short fields stay ``None``
        (JSON ``null``); ragged-long extras (``None`` key) are dropped.

        When ``fieldnames`` is given, ``text`` already belongs to a
        known header: its first row is data, except that a repeated
        header line in first position is defensively skipped
        (tolerated, not required — chunk producers never emit one).
        """
        try:
            if fieldnames is not None:
                head, sep, rest = text.partition("\n")
                candidate = head if sep else text
                if candidate:
                    try:
                        first = next(csv.reader([candidate],
                                                delimiter=delimiter),
                                     None)
                    except Exception:
                        first = None
                    if first is not None and \
                            list(first) == list(fieldnames):
                        text = rest if sep else ""
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter,
                                    fieldnames=fieldnames)
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
        self._ensure_db_seeded()
        chunks = self.files[name]
        if not chunks:
            return []
        header = next(csv.reader(io.StringIO(chunks[0]),
                                 delimiter=delimiter), None)
        if not header:
            return []
        out = []
        for index, chunk in enumerate(chunks):
            self._tick()
            self._check_wall()
            if index == 0:
                out.extend(self._csv_records(chunk, delimiter))
            else:
                out.extend(self._csv_records(chunk, delimiter,
                                             fieldnames=header))
        return out

    # -- sealed SQLite scratchpad (db.query / db.exec + seeding) ---------
    def _seed_db(self):
        """Seed tables ``t0``, ``t1``, ... from ``files`` chunk lists.

        Runs lazily on first ``db``/``rows()`` access (see
        ``_ensure_db_seeded``): pure-Python runs over attached files pay
        no seeding ticks. Header-first CSV logic mirrors ``rows()``
        (first chunk carries the header); zero-column files are skipped,
        never a path error.
        """
        try:
            cursor = self._db_conn.cursor()
        except (sqlite3.Error, sqlite3.Warning) as exc:
            raise _Denied(_sqlite_error(exc)) from None
        for index, name in enumerate(list(self.files.keys())):
            chunks = self.files[name]
            if not chunks:
                continue
            header = next(csv.reader(io.StringIO(chunks[0])), None)
            if not header:
                continue
            columns = _sanitize_columns(header)
            table = f"t{index}"
            create = ("CREATE TABLE " + _quote_ident(table) + " ("
                      + ", ".join(_quote_ident(col) + " TEXT"
                                  for col in columns) + ")")
            insert = ("INSERT INTO " + _quote_ident(table) + " ("
                      + ", ".join(_quote_ident(col) for col in columns)
                      + ") VALUES ("
                      + ", ".join("?" * len(columns)) + ")")
            try:
                cursor.execute(create)
            except (sqlite3.Error, sqlite3.Warning) as exc:
                raise _Denied(_sqlite_error(exc)) from None
            try:
                for chunk_no, chunk in enumerate(chunks):
                    self._tick()
                    self._check_wall()
                    records = self._csv_records(
                        chunk, ",",
                        fieldnames=None if chunk_no == 0 else header)
                    batch = [[record.get(col) for col in header]
                             for record in records]
                    for start in range(0, len(batch), _SQL_SEED_BATCH):
                        cursor.executemany(
                            insert, batch[start:start + _SQL_SEED_BATCH])
                self._db_conn.commit()
            except (_Halt, _UserError):
                self._db_rollback()
                raise
            except (sqlite3.Error, sqlite3.Warning) as exc:
                self._db_rollback()
                raise _Denied(_sqlite_error(exc)) from None
            except Exception:
                self._db_rollback()
                raise _Denied("operation failed") from None

    def _db_rollback(self):
        try:
            self._db_conn.rollback()
        except Exception:
            pass

    def _check_db_value(self, value, op):
        if value is None or isinstance(value, (str, int, float, bool)):
            return
        raise _UserError(TypeError(
            f"{op} params must hold plain JSON values"))

    def _check_db_params(self, params, op):
        if params is None:
            return None
        if isinstance(params, dict):
            for key, value in params.items():
                if not isinstance(key, str):
                    raise _UserError(TypeError(
                        f"{op} param names must be strings"))
                self._check_db_value(value, op)
            return params
        if isinstance(params, (list, tuple)):
            for value in params:
                self._check_db_value(value, op)
            return params
        raise _UserError(TypeError(
            f"{op} params must be None, a list, a tuple, or a dict"))

    def _prepare_db_call(self, op, args, kwargs):
        if kwargs:
            raise _UserError(TypeError(
                f"{op} takes no keyword arguments"))
        if not 1 <= len(args) <= 2:
            raise _UserError(TypeError(
                f"{op} takes a SQL string and optional params"))
        sql = args[0]
        if not isinstance(sql, str):
            raise _UserError(TypeError(f"{op} needs a SQL string"))
        if not sql:
            raise _UserError(ValueError(
                f"{op} needs a non-empty SQL string"))
        if len(sql) > _SQL_MAX_CHARS:
            raise _UserError(ValueError(
                f"{op} SQL is too large (max {_SQL_MAX_CHARS} chars)"))
        # Layer-1 lockdown: keyword pre-filter, any spelling/case.
        if _SQL_DENIED_RE.search(sql):
            raise _Denied(f"denied SQL statement in {op}")
        params = self._check_db_params(
            args[1] if len(args) > 1 else None, op)
        words = _sql_words(sql)
        first = words[0].upper() if words else ""
        if op == "query":
            if first not in ("SELECT", "WITH"):
                raise _Denied(
                    "db.query needs a SELECT or WITH statement")
            if first == "WITH" and _with_main_verb(sql) != "SELECT":
                raise _Denied(
                    "db.query needs a SELECT or WITH ... SELECT statement")
        else:
            if first in ("INSERT", "UPDATE", "DELETE"):
                pass
            elif first in ("CREATE", "DROP") and len(words) > 1 \
                    and words[1].upper() in ("TABLE", "INDEX"):
                pass
            else:
                raise _Denied(
                    "db.exec needs a write or DDL statement")
        return sql, params

    def _db_query(self, *args, **kwargs):
        sql, params = self._prepare_db_call("query", args, kwargs)
        self._ensure_db_seeded()
        try:
            cursor = self._db_conn.execute(sql) if params is None \
                else self._db_conn.execute(sql, params)
        except (sqlite3.Error, sqlite3.Warning) as exc:
            raise _Denied(_sqlite_error(exc)) from None
        try:
            columns = [info[0] for info in cursor.description] \
                if cursor.description else []
            out = []
            for index, row in enumerate(cursor):
                self._tick()
                record = dict(zip(columns, row, strict=False))
                self._charge(64 + 72 * len(record))
                for value in record.values():
                    if isinstance(value, (str, bytes)) and self.alloc + 40 \
                            + len(value) > self.limits[
                                "max_allocation_bytes"]:
                        raise _Budget(
                            f"query value too large ({len(value)} bytes); "
                            "chunk the query (fewer/narrower rows) and "
                            "combine partial results")
                    self._charge_value(value)
                out.append(record)
                if (index & 63) == 63:
                    self._check_wall()
            # The query path never commits: roll back so a smuggled write
            # can never stay visible to later reads in the same run.
            self._db_rollback()
        except (_Halt, _UserError):
            self._db_rollback()
            raise
        except (sqlite3.Error, sqlite3.Warning) as exc:
            raise _Denied(_sqlite_error(exc)) from None
        except Exception:
            raise _Denied("operation failed") from None
        return out

    def _db_exec(self, *args, **kwargs):
        sql, params = self._prepare_db_call("exec", args, kwargs)
        self._ensure_db_seeded()
        try:
            cursor = self._db_conn.execute(sql) if params is None \
                else self._db_conn.execute(sql, params)
        except (sqlite3.Error, sqlite3.Warning) as exc:
            self._db_rollback()
            raise _Denied(_sqlite_error(exc)) from None
        try:
            affected = cursor.rowcount
            counted = affected if isinstance(affected, int) \
                and affected > 0 else 0
            if counted:
                # Bulk writes (e.g. INSERT INTO ... SELECT doubling the
                # table per call) consume the shared budgets row by row,
                # exactly like materialized query rows: a tiny doubling
                # loop must abort instead of OOMing the host.
                self._tick(counted)
                self._charge(72 * counted)
                self._check_wall()
            self._db_conn.commit()
        except (_Halt, _UserError):
            self._db_rollback()
            raise
        except (sqlite3.Error, sqlite3.Warning) as exc:
            self._db_rollback()
            raise _Denied(_sqlite_error(exc)) from None
        affected = cursor.rowcount
        result = {"rows_affected": affected if isinstance(affected, int)
                  and affected >= 0 else 0}
        self._charge(64 + 72)
        return result

    # -- whitelisted methods -----------------------------------------------
    def _call_method(self, bound, args, kwargs):
        obj, name = bound.obj, bound.name
        if isinstance(obj, _Db):
            if name not in ("query", "exec"):
                raise _Denied(f"attribute '{name}' is not allowed")
            try:
                return getattr(obj, name)(*args, **kwargs)
            except (_Halt, _UserError):
                raise
            except _ALLOWED_EXC_TYPES as exc:
                raise _UserError(exc) from None
            except Exception:
                raise _Denied("operation failed") from None
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

    def _format_spec_widths(self, spec):
        """Width/precision ints in one format-spec level (nested stripped).

        Raises before the host allocates, so e.g.
        ``"{:>1000000000}".format("x")`` never materializes ~1GB.
        """
        total = 0
        for num in _FORMAT_NUM_RE.findall(_FORMAT_NESTED_RE.sub("", spec)):
            width = int(num)
            if width > self.limits["max_allocation_bytes"]:
                raise _Budget(
                    "str.format width/precision too large; chunk the work "
                    "into smaller pieces instead of building giant values "
                    "at once")
            total += width
        return total

    def _format_nested_value(self, value):
        """Width contribution of one resolved nested-spec argument."""
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            if value > self.limits["max_allocation_bytes"]:
                raise _Budget(
                    "str.format width/precision too large; chunk the work "
                    "into smaller pieces instead of building giant values "
                    "at once")
            return value if value > 0 else 0
        if isinstance(value, str):
            return self._format_spec_widths(value)
        return 0

    def _collect_nested_values(self, spec, args, kwargs):
        """Resolve nested-spec fields to argument values.

        Returns ``(values, unknown)``; ``unknown`` covers auto-numbered
        (``{}``) or unresolvable names, which fall back to scanning every
        argument conservatively.
        """
        values = []
        unknown = False
        for _, field_name, sub_spec, _ in _FORMATTER.parse(spec):
            if field_name is None:
                continue
            if field_name == "":
                unknown = True
            elif field_name.isdigit():
                idx = int(field_name)
                if idx < len(args):
                    values.append(args[idx])
                else:
                    unknown = True
            elif field_name in kwargs:
                values.append(kwargs[field_name])
            else:
                unknown = True
            if sub_spec:
                deeper, deep_unknown = self._collect_nested_values(
                    sub_spec, args, kwargs)
                values.extend(deeper)
                unknown = unknown or deep_unknown
        return values, unknown

    def _check_format_template(self, template, args, kwargs):
        """Pre-check before the host ``str.format`` call (mirrors _mult).

        Caps width/precision (literal and argument-driven, e.g.
        ``"{0:>{w}}".format("x", w=10**9)``) against the allocation
        budget so the host never materializes the giant string.
        """
        if "{" not in template:
            return
        total = len(template)
        values = []
        unknown = False
        for _, _, spec, _ in _FORMATTER.parse(template):
            if not spec:
                continue
            total += self._format_spec_widths(spec)
            if "{" in spec:
                found, missed = self._collect_nested_values(
                    spec, args, kwargs)
                values.extend(found)
                unknown = unknown or missed
        for value in values:
            total += self._format_nested_value(value)
        if unknown:
            for value in list(args) + list(kwargs.values()):
                total += self._format_nested_value(value)
        if self.alloc + total > self.limits["max_allocation_bytes"]:
            raise _Budget(
                "str.format width/precision too large; chunk the work "
                "into smaller pieces instead of building giant values "
                "at once")

    def _check_format_result(self, base, spec):
        """Pre-check before the host ``format(value, spec)`` call."""
        total = base + self._format_spec_widths(spec)
        if self.alloc + total > self.limits["max_allocation_bytes"]:
            raise _Budget(
                "str.format width/precision too large; chunk the work "
                "into smaller pieces instead of building giant values "
                "at once")

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
            self._check_format_template(obj, args, kwargs)
            result = obj.format(*args, **kwargs)
            self._charge(40 + len(result))
            return result
        if name in ("lower", "upper", "title"):
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 0, 0)
            result = getattr(obj, name)()
            self._charge(40 + len(result))
            return result
        if name in ("lstrip", "rstrip"):
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 0, 1)
            chars = args[0] if args else None
            if chars is not None and not isinstance(chars, str):
                raise _UserError(TypeError(f"{name} chars must be str"))
            result = getattr(obj, name)(chars)
            self._charge(40 + len(result))
            return result
        if name == "splitlines":
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 0, 0)
            parts = obj.splitlines()
            self._tick(len(parts))
            self._charge(56 + 8 * len(parts)
                         + sum(len(p) for p in parts))
            return parts
        if name in ("count", "find", "rfind", "index"):
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 1, 3)
            if not isinstance(args[0], str):
                raise _UserError(TypeError(f"{name} needs a str"))
            for bound_arg in args[1:]:
                if isinstance(bound_arg, bool) or not isinstance(
                        bound_arg, int):
                    raise _UserError(TypeError(
                        f"{name} bounds must be int"))
            return getattr(obj, name)(*args)
        if name == "partition":
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 1, 1)
            if not isinstance(args[0], str):
                raise _UserError(TypeError("partition needs a str"))
            if args[0] == "":
                raise _UserError(ValueError("empty separator"))
            result = obj.partition(args[0])
            self._charge(56 + 8 * len(result)
                         + sum(len(p) for p in result))
            return result
        if name == "rpartition":
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 1, 1)
            if not isinstance(args[0], str):
                raise _UserError(TypeError("rpartition needs a str"))
            if args[0] == "":
                raise _UserError(ValueError("empty separator"))
            result = obj.rpartition(args[0])
            self._charge(56 + 8 * len(result)
                         + sum(len(p) for p in result))
            return result
        if name in ("isdigit", "isnumeric"):
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 0, 0)
            return getattr(obj, name)()
        raise _Denied(f"attribute '{name}' is not allowed")

    def _list_method(self, obj, name, args, kwargs):
        if name == "append":
            self._no_kwargs("append", kwargs)
            self._checked_args("append", args, 1, 1)
            self._charge(40)
            self._charge_value(args[0])
            obj.append(args[0])
            return None
        if name == "extend":
            self._no_kwargs("extend", kwargs)
            self._checked_args("extend", args, 1, 1)
            items = []
            for item in self._safe_iter(args[0]):
                self._tick()
                self._charge_value(item)
                items.append(item)
            self._charge(8 * len(items))
            obj.extend(items)
            return None
        if name == "sort":
            self._checked_args("sort", args, 0, 0)
            key = kwargs.pop("key", None)
            reverse = kwargs.pop("reverse", False)
            if kwargs:
                raise _UserError(TypeError(
                    "sort takes only 'key'/'reverse' as keywords"))
            if key is not None and not isinstance(
                    key, (_Function, _Lambda, _Builtin)):
                raise _UserError(TypeError("'key' is not callable"))
            self._tick(len(obj))
            if key is not None:
                def sort_key(item):
                    return self._call_value(key, [item], {})
                self._guard(obj.sort, key=sort_key,
                            reverse=bool(reverse))
            else:
                self._guard(obj.sort, reverse=bool(reverse))
            self._charge(8 * len(obj))
            return None
        if name == "count":
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 1, 1)
            self._tick(len(obj))
            return self._guard(obj.count, args[0])
        if name == "index":
            self._no_kwargs(name, kwargs)
            self._checked_args(name, args, 1, 3)
            self._tick(len(obj))
            return self._guard(obj.index, *args)
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
                self._charge_value(key)
                self._charge_value(value)
            self._charge(72 * len(pairs))
            return None
        if name == "setdefault":
            self._no_kwargs("setdefault", kwargs)
            self._checked_args("setdefault", args, 1, 2)
            key = self._guard(_hashable, args[0])
            if len(args) == 1:
                result = self._guard(obj.setdefault, key)
            else:
                result = self._guard(obj.setdefault, key, args[1])
            self._charge(72)
            self._charge_value(key)
            if len(args) > 1:
                self._charge_value(args[1])
            return result
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


def evaluate(code, variables=None, files=None, limits=None, collect=None):
    """Run ``code`` in the sealed evaluator.

    Returns ``{"ok": True, "value": ...}`` or ``{"ok": False, "error": ...}``.
    When ``collect`` holds variable names, their values are read in the
    same pass (no re-run) and returned under ``"collected"``; a missing
    name fails with ``"collect_failed": True`` so the caller can tell an
    output-name error apart from a program error.
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
        want = []
        if collect is not None:
            if not isinstance(collect, (list, tuple)) or not all(
                    isinstance(name, str) and name.isidentifier()
                    and not keyword.iskeyword(name)
                    and not name.startswith("__") for name in collect):
                return _fail("collect must hold variable names")
            want = list(collect)
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
            scratch = _open_scratch_db()
        except (_Denied, _Budget) as exc:
            return _fail(str(exc))
        try:
            runner = _Interpreter(clean_vars, files, merged, scratch)
            result = runner.run(tree)
            # Serialization backstop: charge the result's referenced bytes
            # (multiplicity-aware) BEFORE json.dumps materializes it, so an
            # accumulator built by append/extend/subscript loops trips the
            # allocation budget instead of OOMing in dumps. Anything this
            # rejects carries far more referenced bytes than the output cap
            # allows, so no previously-passing result can trip it.
            runner._charge_value(result)
            collected = {}
            for var in want:
                try:
                    probe = ast.parse(f"({var})", mode="eval")
                    collected[var] = runner._ev(probe.body)
                except (_Denied, _Budget) as exc:
                    return {"ok": False, "error": str(exc),
                            "collect_failed": True}
                except _UserError as exc:
                    return {"ok": False,
                            "error": f"{type(exc.exc).__name__}: {exc.exc}",
                            "collect_failed": True}
            if want:
                # The old second-evaluate probe enforced the output budget
                # on collected artifacts; check here so oversized outputs
                # fail with the outputs-hint mapping instead of sailing
                # through with a multi-MB artifact. Charge first so a
                # collected accumulator trips before dumps materializes it.
                try:
                    runner._charge_value(collected)
                except (_Denied, _Budget) as exc:
                    return {"ok": False, "error": str(exc),
                            "collect_failed": True}
                try:
                    collected_text = json.dumps(collected, allow_nan=False)
                except (TypeError, ValueError):
                    return {"ok": False,
                            "error": "result is not JSON-serializable",
                            "collect_failed": True}
                if len(collected_text) > merged["max_output_chars"]:
                    return {"ok": False,
                            "error": f"result exceeds output budget "
                                     f"({len(collected_text)} chars, cap "
                                     f"{merged['max_output_chars']})",
                            "collect_failed": True}
            runner._check_wall()
        except (_Denied, _Budget) as exc:
            return _fail(str(exc))
        except _UserError as exc:
            return _fail(f"{type(exc.exc).__name__}: {exc.exc}")
        finally:
            try:
                scratch.close()
            except Exception:
                pass
        try:
            serialized = json.dumps(result, allow_nan=False)
        except (TypeError, ValueError):
            return _fail("result is not JSON-serializable")
        if len(serialized) > merged["max_output_chars"]:
            return _fail(f"result exceeds output budget ({len(serialized)} "
                         f"chars, cap {merged['max_output_chars']})")
        response = {"ok": True, "value": result}
        if want:
            response["collected"] = collected
        return response
    except (_Denied, _Budget) as exc:
        return _fail(str(exc))
    except _UserError as exc:
        return _fail(f"{type(exc.exc).__name__}: {exc.exc}")
    except (RecursionError, MemoryError):
        return _fail("evaluation exceeded interpreter limits")
    except Exception:
        return _fail("evaluation failed")
