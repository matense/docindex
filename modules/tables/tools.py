"""AI tools contributed by the tables module (namespaced tables.*).

Mutations go through the versioned T3 path (``file_service.update_file_content``
with source ``"table-edit"``): the snapshot-then-write order matches the
``POST /<id>/cell`` route, so every AI edit leaves a restorable version.
Reads reuse the T2/T3 route helpers (``routes._read_table`` for every format,
``routes._first_keyword`` + the SELECT/WITH gate for SQL) instead of forking
them. There are no AI lock/hide flags on tables, so the guard is
module-enabled + ownership + trashed (plus a read-only-drive check on writes,
mirroring the cell route).
"""

import json

from app.models import StoredFile
from app.services import file_service, module_service
from app.services.agent_service import register_tool

from . import doc as tables_doc
from . import routes as routes_service


def _schema(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                        "required": list(required)}}}


def _module_disabled():
    if not module_service.is_enabled("tables"):
        return ("The tables module is disabled — enable it to work with "
                "tables.")
    return None


def _get_owned(user, file_id):
    """Owned, non-trashed table StoredFile or (None, error)."""
    disabled = _module_disabled()
    if disabled:
        return None, {"error": disabled}
    try:
        fid = int(file_id)
    except (TypeError, ValueError):
        return None, {"error": f"Table {file_id!r} not found."}
    stored = (StoredFile.query
              .filter_by(id=fid, user_id=user.id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if stored is None:
        return None, {"error": f"Table {file_id} not found."}
    if (stored.extension or "").lower() not in routes_service.TABLE_EXTENSIONS:
        return None, {"error": f"File {fid} is not a table file."}
    return stored, None


def _guard_writable(stored):
    """Read-only (synced) drives refuse AI edits, mirroring the cell route."""
    if file_service.is_readonly_file(stored):
        drive = stored.drive
        name = drive.name if drive is not None else "synced"
        raise ValueError(f"File '{stored.name}' is on read-only drive "
                         f"'{name}' and cannot be edited.")


def _read_owned(user, file_id):
    """(stored, header, rows, error): error is None on success."""
    stored, error = _get_owned(user, file_id)
    if error:
        return None, None, None, error
    try:
        header, rows = routes_service._read_table(stored)
    except (ValueError, OSError) as exc:
        return None, None, None, \
            {"error": f"Could not read table file: {exc}"}
    return stored, header, rows, None


def _stored_columns(stored):
    """Stored .ptable column specs, or None (then callers sniff types)."""
    if (stored.extension or "").lower() != "ptable":
        return None
    try:
        with open(file_service.file_path(stored), encoding="utf-8") as fh:
            document = tables_doc.load(fh.read())
    except (ValueError, OSError):
        return None
    return [{"name": col["name"], "type": col["type"]}
            for col in document["columns"]]


def _tool_schema(user, args, drive):
    stored, header, rows, error = _read_owned(user, args.get("file_id"))
    if error:
        return error
    columns = _stored_columns(stored)
    if columns is None:
        types = [routes_service.sniff_column_type([row[i] for row in rows])
                 for i in range(len(header))]
        columns = [{"name": name, "type": ctype}
                   for name, ctype in zip(header, types, strict=True)]
    return {"file_id": stored.id, "name": stored.name,
            "columns": columns, "total_rows": len(rows)}


def _tool_query_sql(user, args, drive):
    stored, header, rows, error = _read_owned(user, args.get("file_id"))
    if error:
        return error
    sql = args.get("sql", "")
    if not isinstance(sql, str) or not sql.strip():
        return {"error": "Enter a SELECT query to run "
                         "(e.g. SELECT * FROM t0 LIMIT 100)."}
    if routes_service._first_keyword(sql) not in routes_service._SQL_READ_FIRST:
        return {"error": routes_service._SQL_GATE_ERROR}
    try:
        limit = int(args.get("limit", 100))
    except (TypeError, ValueError):
        return {"error": "limit must be an integer."}
    limit = max(1, min(limit, routes_service.ROWS_LIMIT_MAX))
    try:
        offset = int(args.get("offset", 0))
    except (TypeError, ValueError):
        return {"error": "offset must be an integer."}
    offset = max(0, offset)
    try:
        from docindex_module_pyrun.evaluate import evaluate
    except ImportError:
        return {"error": "pyrun evaluator is unavailable."}
    files_map = {"f": [routes_service._table_csv_text(header, rows)]}
    code = "res = db.query(" + json.dumps(sql) + ")\nres"
    try:
        outcome = evaluate(code, files=files_map)
    except Exception:  # noqa: BLE001 - the tool boundary never 500s raw
        return {"error": "Evaluation failed unexpectedly."}
    if not isinstance(outcome, dict) or not outcome.get("ok"):
        error = outcome.get("error") if isinstance(outcome, dict) else None
        return {"error": error or "Query failed."}
    records = outcome.get("value")
    if not isinstance(records, list):
        return {"error": "Query failed: unexpected result shape."}
    if not all(isinstance(rec, dict) for rec in records):
        return {"error": "Query failed: unexpected result shape."}
    if not records:
        return {"ok": True, "columns": [], "rows": [], "total": 0}
    names = [str(key) for key in records[0].keys()]
    display = [[routes_service._cell_text(rec.get(name)) for name in names]
               for rec in records]
    types = [routes_service.sniff_column_type([row[i] for row in display])
             for i in range(len(names))]
    columns = [{"name": name, "type": ctype}
               for name, ctype in zip(names, types, strict=False)]
    return {"ok": True, "columns": columns,
            "rows": display[offset:offset + limit], "total": len(records)}


def _load_ptable_document(stored):
    """The stored .ptable document, or an error string."""
    if (stored.extension or "").lower() != "ptable":
        return None, ("This file is read-only for the AI — convert it to "
                      ".ptable first, then edit the .ptable copy.")
    try:
        with open(file_service.file_path(stored), encoding="utf-8") as fh:
            document = tables_doc.load(fh.read())
    except (ValueError, OSError) as exc:
        return None, f"Could not read table file: {exc}"
    return document, None


def _resolve_cell(document, row, col):
    """(row_idx, col_idx) or an error string, mirroring the cell route."""
    names = [col_spec["name"] for col_spec in document["columns"]]
    if isinstance(col, bool) or not isinstance(col, int):
        if not isinstance(col, str) or col not in names:
            return None, f"Unknown column {col!r}."
        col_idx = names.index(col)
    else:
        col_idx = col
    if isinstance(row, bool) or not isinstance(row, int):
        return None, "Cell row must be an integer."
    if not 0 <= row < len(document["rows"]):
        return None, f"Row {row} is out of range."
    if not 0 <= col_idx < len(names):
        return None, f"Column {col!r} is out of range."
    return (row, col_idx), None


def _tool_update_rows(user, args, drive):
    stored, error = _get_owned(user, args.get("file_id"))
    if error:
        return error
    try:
        _guard_writable(stored)
    except ValueError as exc:
        return {"error": str(exc)}
    raw_edits = args.get("edits")
    if raw_edits is None and "row" in args:
        raw_edits = [{"row": args.get("row"), "col": args.get("col"),
                      "value": args.get("value")}]
    if not isinstance(raw_edits, list) or not raw_edits:
        return {"error": "Provide at least one edit as "
                         "{row, col, value}."}
    document, doc_error = _load_ptable_document(stored)
    if doc_error:
        return {"error": doc_error}
    for edit in raw_edits:
        if not isinstance(edit, dict) or "value" not in edit:
            return {"error": "Each edit needs {row, col, value}."}
        position, cell_error = _resolve_cell(document, edit.get("row"),
                                             edit.get("col"))
        if cell_error:
            return {"error": cell_error}
        row_idx, col_idx = position
        document["rows"][row_idx][col_idx] = edit["value"]
    try:
        text = tables_doc.serialize(tables_doc.validate(document))
    except ValueError as exc:
        return {"error": str(exc) or "Invalid table value."}
    try:
        file_service.update_file_content(stored, text, source="table-edit")
    except ValueError as exc:
        return {"error": str(exc) or "Could not save the cells."}
    except Exception:  # noqa: BLE001 - IO/SQLAlchemy must not 500 raw
        return {"error": "Could not save the cells."}
    return {"ok": True, "file_id": stored.id, "updated": len(raw_edits)}


def _tool_add_column(user, args, drive):
    stored, error = _get_owned(user, args.get("file_id"))
    if error:
        return error
    try:
        _guard_writable(stored)
    except ValueError as exc:
        return {"error": str(exc)}
    name = args.get("name", "")
    if not isinstance(name, str) or not name.strip():
        return {"error": "Pass a column name."}
    name = name.strip()
    ctype = args.get("type", "text")
    if ctype not in tables_doc.COLUMN_TYPES:
        return {"error": f"Unknown column type {ctype!r} — use one of "
                         f"{', '.join(tables_doc.COLUMN_TYPES)}."}
    default = args.get("default", "")
    document, doc_error = _load_ptable_document(stored)
    if doc_error:
        return {"error": doc_error}
    if name in [col_spec["name"] for col_spec in document["columns"]]:
        return {"error": f"Column {name!r} already exists."}
    document["columns"].append({"name": name, "type": ctype})
    for row in document["rows"]:
        row.append(default)
    try:
        text = tables_doc.serialize(tables_doc.validate(document))
    except ValueError as exc:
        return {"error": str(exc) or "Invalid table value."}
    try:
        file_service.update_file_content(stored, text, source="table-edit")
    except ValueError as exc:
        return {"error": str(exc) or "Could not add the column."}
    except Exception:  # noqa: BLE001 - IO/SQLAlchemy must not 500 raw
        return {"error": "Could not add the column."}
    return {"ok": True, "file_id": stored.id,
            "column": {"name": name, "type": ctype},
            "total_columns": len(document["columns"])}


# No per-section toggles (dashboards/notebooks precedent): the admin page
# honors raw tool names, so this stays empty.
TOOL_SPECS = []

_FILE_ID = {"type": "integer", "description": "Table file id."}

register_tool(
    "schema",
    _schema("tables.schema",
            "Show a table's columns (name + type: number/date/category/text) "
            "and row count. Works on .ptable datasets and on csv/tsv/xlsx "
            "files (types sniffed like the viewer). Read-only.",
            {"file_id": _FILE_ID},
            required=["file_id"]),
    _tool_schema, label="Read table schema", module="tables")

register_tool(
    "query_sql",
    _schema("tables.query_sql",
            "Run a read-only SELECT (or WITH ... SELECT) query over the "
            "table via the pyrun sandbox. Only SELECT/WITH queries are "
            "allowed — anything that changes data is rejected. Returns "
            "columns, rows and the total match count. Read-only.",
            {"file_id": _FILE_ID,
             "sql": {"type": "string",
                     "description": "SELECT query over the table as t0 "
                                    "(e.g. SELECT * FROM t0 LIMIT 100)."},
             "limit": {"type": "integer",
                       "description": "Rows to return (default 100, "
                                      "max 500)."},
             "offset": {"type": "integer",
                        "description": "First matching row to return."}},
            required=["file_id", "sql"]),
    _tool_query_sql, label="Queried table", module="tables")

register_tool(
    "update_rows",
    _schema("tables.update_rows",
            "Edit cells in a .ptable dataset by row index and column name "
            "or index. Each edit is {row, col, value}. Spreadsheet files "
            "are read-only — convert them first. The write snapshots a "
            "version first, like the viewer cell editor.",
            {"file_id": _FILE_ID,
             "edits": {"type": "array", "items": {"type": "object"},
                       "description": "Cell edits: {row (0-based index), "
                                      "col (name or 0-based index), value}."},
             "row": {"type": "integer",
                     "description": "Single-edit shorthand: row index."},
             "col": {"description": "Single-edit shorthand: column name "
                                    "or index."},
             "value": {"description": "Single-edit shorthand: new value."}},
            required=["file_id", "edits"]),
    _tool_update_rows, label="Updated table rows", module="tables")

register_tool(
    "add_column",
    _schema("tables.add_column",
            "Append a column to a .ptable dataset. Existing rows get the "
            "default value (empty by default). Spreadsheet files are "
            "read-only — convert them first. The write snapshots a version "
            "first, like the viewer cell editor.",
            {"file_id": _FILE_ID,
             "name": {"type": "string",
                      "description": "New column name."},
             "type": {"type": "string",
                      "description": "Column type: number, date, category "
                                     "or text (default text)."},
             "default": {"description": "Value for existing rows "
                                        "(default empty)."}},
            required=["file_id", "name"]),
    _tool_add_column, label="Added table column", module="tables")
