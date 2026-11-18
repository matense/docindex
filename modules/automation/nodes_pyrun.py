"""Pyrun-backed automation nodes: SQL over upstream tables/files and sealed
Python over a single payload.

Registered from nodes.py::register_defaults via register_pyrun_nodes().
Both pyrun nodes in this file share the _seed_files_as_tables() helper,
which seeds upstream table/files payloads as t0, t1, ... inside pyrun's
:memory: engine. The node types carry module="pyrun", so the palette
hides them while pyrun is disabled and runs fail closed.
"""

import re

from app.extensions import db
from app.models import StoredFile
from app.services import file_service
from modules.pyrun.evaluate import evaluate as _pyrun_evaluate

from .engine import (WorkflowError, as_json, as_table, as_text, p_json,
                     p_table, table_to_csv)
from .registry import register_node_type

# Extensions readable as CSV-shaped text for SQL seeding. Anything else
# (images, PDFs, archives, ...) fails with a WorkflowError naming the file.
_TEXTISH_EXTS = {"csv", "tsv", "txt", "md", "json", "xlsx", "xlsm", "xls"}

_PORTS = ("in1", "in2", "in3")


def _first_keyword(sql):
    """First SQL keyword, skipping leading whitespace/comments, upper-cased."""
    text = sql.lstrip()
    while True:
        if text.startswith("--"):
            _, _, text = text.partition("\n")
            text = text.lstrip()
        elif text.startswith("/*"):
            end = text.find("*/", 2)
            if end == -1:
                return ""
            text = text[end + 2:].lstrip()
        else:
            break
    match = re.match(r"[A-Za-z]+", text)
    return match.group(0).upper() if match else ""


def _seed_files_as_tables(ctx, payloads):
    """Upstream payloads (port order) -> {tN: [CSV text chunk]} for pyrun.

    Table payloads serialize back to CSV text via engine.table_to_csv;
    files payloads read each owned file as text via
    file_service.read_text_content. Non-text files raise WorkflowError
    naming the file. Unreadable file refs are skipped, mirroring the
    ownership check in nodes._zip_payload_files.
    """
    files = {}
    for payload in payloads:
        kind = payload.get("kind")
        if kind == "table":
            files[f"t{len(files)}"] = [table_to_csv(as_table(payload))]
        elif kind == "files":
            for ref in payload.get("data") or []:
                stored = db.session.get(StoredFile, (ref or {}).get("file_id"))
                if (stored is None or stored.user_id != ctx["user"].id
                        or stored.deleted_at is not None):
                    continue
                ext = (stored.extension or "").lower()
                if ext not in _TEXTISH_EXTS:
                    raise WorkflowError(
                        f"'{stored.name}' is not a CSV, Excel or text "
                        "file — sql_query can only query text-like files.")
                try:
                    text = file_service.read_text_content(stored)
                except OSError as err:
                    raise WorkflowError(
                        f"Cannot read '{stored.name}'.") from err
                files[f"t{len(files)}"] = [text]
        else:
            raise WorkflowError(
                f"Cannot use a {kind} payload as a table — connect "
                "table or files payloads to in1..in3.")
    return files


def _exec_sql_query(ctx, config, inputs):
    sql = (config.get("sql") or "").strip()
    if not sql:
        raise WorkflowError("Enter a SELECT query to run "
                            "(e.g. SELECT * FROM t0 LIMIT 100).")
    if _first_keyword(sql) != "SELECT":
        raise WorkflowError("Only SELECT queries are allowed — "
                            "this node cannot change data.")
    payloads = [inputs[port] for port in _PORTS
                if inputs.get(port) is not None]
    if not payloads:
        raise WorkflowError("No input — connect a table or files "
                            "to query (in1..in3).")
    files = _seed_files_as_tables(ctx, payloads)
    if not files:
        raise WorkflowError("No readable tables to query — connect a "
                            "table or a CSV/Excel/text file.")
    outcome = _pyrun_evaluate("db.query(sql)", variables={"sql": sql},
                              files=files)
    if not outcome.get("ok"):
        raise WorkflowError(
            f"The query failed: {outcome.get('error') or 'unknown error'}")
    records = outcome.get("value")
    if not isinstance(records, list):
        raise WorkflowError("The query failed: unexpected result shape.")
    if not records:
        return p_table([], [])
    columns = [str(key) for key in records[0].keys()]
    rows = [[("" if rec.get(col) is None else rec.get(col))
             for col in columns] for rec in records]
    ctx["log"](f"query returned {len(rows)} row(s)")
    return p_table(columns, rows)


def _exec_python_sandbox(ctx, config, inputs):
    code = (config.get("code") or "").strip()
    if not code:
        raise WorkflowError("Enter a Python program to run over the input "
                            "(e.g. sum(int(r['total']) for r in rows('f'))).")
    payload = inputs.get("in")
    if payload is None:
        raise WorkflowError("No input — connect a table, JSON or text "
                            "payload to run Python over (port in).")
    kind = payload.get("kind")
    variables, files = {}, {}
    if kind == "table":
        files = {"f": [table_to_csv(as_table(payload))]}
    elif kind == "json":
        variables = {"f": as_json(payload)}
    elif kind == "text":
        variables = {"f": as_text(payload)}
    else:
        raise WorkflowError(
            f"Cannot use a {kind} payload here — connect a table, "
            "JSON or text payload (port in).")
    outcome = _pyrun_evaluate(code, variables=variables, files=files)
    if not outcome.get("ok"):
        raise WorkflowError(
            f"The Python run failed: {outcome.get('error') or 'unknown error'}")
    return p_json(outcome.get("value"))


def register_pyrun_nodes():
    """Pyrun-backed nodes — called once from register_defaults."""
    register_node_type("transform.sql_query", {
        "label": "SQL query", "icon": "fa-database",
        "category": "Transform",
        "description": "Runs one SELECT query over up to three upstream "
                       "tables or files (seeded as t0, t1, t2) in the "
                       "pyrun sandbox and returns the matching rows as a "
                       "table.",
        "example": "SELECT * FROM t0 WHERE total > 100 LIMIT 50 — "
                   "filters the first input like a spreadsheet.",
        "inputs": [{"name": "in1", "kind": "any"},
                   {"name": "in2", "kind": "any"},
                   {"name": "in3", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "table"}],
        "fields": [
            {"name": "sql", "label": "SQL query", "kind": "code:sql",
             "required": True, "default": "",
             "placeholder": "SELECT * FROM t0 LIMIT 100",
             "help": "One SELECT statement over t0..t2 (one table per "
                     "connected input, in port order). Anything else is "
                     "rejected before it runs."},
        ]}, _exec_sql_query, module="pyrun")
    register_node_type("transform.python_sandbox", {
        "label": "Python sandbox", "icon": "fa-code",
        "category": "Transform",
        "description": "Runs a small Python program over one table, JSON or "
                       "text input in the sealed pyrun sandbox and returns "
                       "the computed value as JSON. A table input is exposed "
                       "as rows('f') (list of str->str dicts, one per row); "
                       "a JSON or text input is exposed as the variable f. "
                       "The last top-level expression is the returned value.",
        "example": "sum(int(r['total']) for r in rows('f')) adds up the "
                   "total column of the connected table.",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "json"}],
        "fields": [
            {"name": "code", "label": "Python code", "kind": "code:python",
             "required": True, "default": "",
             "placeholder": "sum(int(r['total']) for r in rows('f'))",
             "help": "A small Python program (same sandbox as pyrun.compute: "
                     "no imports, no I/O). Table rows arrive via rows('f'); "
                     "JSON and text arrive as the variable f. The last "
                     "expression is returned as JSON."},
        ]}, _exec_python_sandbox, module="pyrun")
