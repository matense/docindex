"""Tables module routes: viewer page + rows JSON API (T2).

The viewer opens any viewer-owned table file (csv/tsv/xlsx/ptable) by
StoredFile id. The server paints the first 50 rows plus per-column type
badges and header stats; ``static/tables.js`` then slides a 50-row window
through ``GET /<id>/rows?offset&limit`` so the whole file never lands in
the DOM. The rows API shape ``{columns, rows, total}`` is the contract
T3/T4 build on.

T3 adds the SQL filter bar (``POST /<id>/query`` — SELECT/WITH over the
table in the pyrun sandbox), versioned inline cell edits (``POST
/<id>/cell`` — .ptable only, snapshot first) and the one-way
spreadsheet-to-dataset conversion (``POST /<id>/save-as``).

T4 adds the pin-as-dashboard-card handoff (``POST /<id>/pin`` + the
``GET /<id>/pin-targets`` picker — a table card referencing the table
file as an attachment, reusing the dashboards' own attach/card path, no
new StoredFile) and the build-automation handoff (``POST
/<id>/build-automation`` — a workflow prefilled with an input.file node
on the table, returning the live editor URL).
"""

import csv
import hashlib
import importlib
import io
import json
import math
import os
import re
import uuid
from datetime import datetime

from flask import Blueprint, abort, current_app, jsonify, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import FileIndex, StoredFile
from app.services import events, file_service

from . import TABLE_EXTENSIONS
from . import doc as tables_doc

bp = Blueprint("tables", __name__,
               template_folder="templates", static_folder="static")

# Server paint + client window: the first page and every fetch after it.
PAGE_SIZE = 50
# Upper bound for a single rows-API fetch (paging still never dumps all).
ROWS_LIMIT_MAX = 500
# Distinct-value ceiling for the low-cardinality ``category`` sniff.
CATEGORY_MAX_DISTINCT = 10
# CSV dialect sniffing reads at most this many bytes.
SNIFF_BYTES = 8 * 1024


# ---------------------------------------------------------------------------
# Access + loading
# ---------------------------------------------------------------------------

def _get_table_file(file_id):
    """Owned, non-trashed table StoredFile or 404/400."""
    stored = (StoredFile.query
              .filter_by(id=file_id, user_id=current_user.id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if stored is None:
        abort(404)
    if (stored.extension or "").lower() not in TABLE_EXTENSIONS:
        abort(400, description="Not a table file.")
    return stored


def _read_table(stored):
    """(header, rows): header names + rectangular raw data rows.

    CSV/TSV decode lossy (errors="replace"); ragged rows are padded with
    "" and trimmed to the header width. XLSX reads the first sheet,
    values only. .ptable goes through the T1 doc service.
    """
    ext = (stored.extension or "").lower()
    if ext == "ptable":
        return _read_ptable(stored)
    if ext == "xlsx":
        return _read_xlsx(stored)
    return _read_delimited(stored, tabbed=(ext == "tsv"))


def _blob_text(stored):
    path = file_service.file_path(stored)
    if not os.path.isfile(path):
        abort(404, description="Table file data is missing.")
    with open(path, encoding="utf-8", errors="replace",
              newline="") as fh:
        return fh.read()


def _sniff_delimiter(sample):
    """Pick , vs ; vs tab by raw count over the sample (ties prefer ",")."""
    preference = {"\t": 0, ";": 1, ",": 2}
    return max((",", ";", "\t"),
               key=lambda d: (sample.count(d), preference[d]))


def _read_delimited(stored, tabbed=False):
    text = _blob_text(stored)
    if not text.strip():
        return [], []
    if tabbed:
        delimiter = "\t"
    else:
        with open(file_service.file_path(stored),
                   encoding="utf-8", errors="replace", newline="") as fh:
            sample = fh.read(SNIFF_BYTES)
        delimiter = _sniff_delimiter(sample)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader
            if any(cell.strip() for cell in row)]
    if not rows:
        return [], []
    header = [cell.strip() or f"col{i + 1}"
              for i, cell in enumerate(rows[0])]
    width = len(header)
    data = []
    for row in rows[1:]:
        if len(row) < width:
            row = row + [""] * (width - len(row))
        data.append(row[:width])
    return header, data


def _read_xlsx(stored):
    import openpyxl

    path = file_service.file_path(stored)
    if not os.path.isfile(path):
        abort(404, description="Table file data is missing.")
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            raw = list(ws.iter_rows(values_only=True))
        finally:
            wb.close()
    except Exception as exc:  # corrupt/foreign zip, bad sheet, ...
        abort(400, description=f"Could not read spreadsheet: {exc}")
    if not raw:
        return [], []
    header = [_cell_text(cell) or f"col{i + 1}"
              for i, cell in enumerate(raw[0])]
    width = len(header)
    data = []
    for row in raw[1:]:
        cells = [_xlsx_value(cell) for cell in row]
        if all(v is None or (isinstance(v, str) and not v.strip())
               for v in cells):
            continue
        if len(cells) < width:
            cells = cells + [None] * (width - len(cells))
        data.append(cells[:width])
    return header, data


def _xlsx_value(cell):
    if cell is None:
        return None
    if isinstance(cell, datetime):
        return cell.isoformat()
    if hasattr(cell, "isoformat"):  # date / time
        try:
            return cell.isoformat()
        except (ValueError, AttributeError):
            return str(cell)
    return cell


def _read_ptable(stored):
    try:
        document = tables_doc.load(_blob_text(stored))
    except (ValueError, OSError) as exc:
        abort(400, description=f"Could not read table file: {exc}")
    header = [col["name"] for col in document["columns"]]
    return header, [list(row) for row in document["rows"]]


# ---------------------------------------------------------------------------
# Column sniffing + stats
# ---------------------------------------------------------------------------

def _as_float(value):
    if isinstance(value, bool):
        raise ValueError("bool is not numeric")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty is not numeric")
        number = float(text)
    else:
        raise ValueError(f"{value!r} is not numeric")
    if not math.isfinite(number):
        raise ValueError("non-finite is not numeric")
    return number


def _is_iso_date(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            datetime.fromisoformat(text)
        except ValueError:
            return False
        return True
    return False


def _non_empty(values):
    return [v for v in values
            if v is not None and not (isinstance(v, str)
                                      and not v.strip())]


def sniff_column_type(values):
    """number | date | category | text.

    All-numeric wins first, then ISO dates; low-cardinality strings become
    ``category`` and everything else is ``text``. Empty cells never vote.
    """
    present = _non_empty(values)
    if not present:
        return "text"
    try:
        for value in present:
            _as_float(value)
    except ValueError:
        pass
    else:
        return "number"
    if all(_is_iso_date(value) for value in present):
        return "date"
    distinct = {_cell_text(value) for value in present}
    if len(distinct) <= CATEGORY_MAX_DISTINCT and len(present) > len(distinct):
        return "category"
    return "text"


def _fmt_num(number):
    return f"{number:g}"


def _sparkline_svg(numbers, width=100, height=20):
    points = numbers[:200]
    n = len(points)
    if n == 0:
        return ""
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    coords = []
    for i, value in enumerate(points):
        x = (i / (n - 1) * width) if n > 1 else (width / 2)
        y = (height - 2) - ((value - lo) / span) * (height - 4)
        coords.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg class="sparkline" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" aria-hidden="true">'
        f'<polyline points="{" ".join(coords)}" '
        f'fill="none" stroke="currentColor" stroke-width="1.5" />'
        f"</svg>"
    )


def column_stats(values):
    """Header stats for a numeric column, else None.

    Returns sum/avg/min/max/null count plus an inline SVG sparkline.
    """
    nulls = len(values) - len(_non_empty(values))
    numbers = []
    for value in _non_empty(values):
        try:
            numbers.append(_as_float(value))
        except ValueError:
            return None
    if not numbers:
        return None
    total = sum(numbers)
    return {
        "nulls": nulls,
        "sum": _fmt_num(total),
        "avg": _fmt_num(total / len(numbers)),
        "min": _fmt_num(min(numbers)),
        "max": _fmt_num(max(numbers)),
        "sparkline": _sparkline_svg(numbers),
    }


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _display_rows(rows):
    return [[_cell_text(cell) for cell in row] for row in rows]


# ---------------------------------------------------------------------------
# T3: SQL filter bar + versioned inline editing
# ---------------------------------------------------------------------------

# First-keyword gate for the SQL filter bar: SELECT and WITH only, mirroring
# the automation transform.sql_query gate. pyrun.evaluate (db.query) stays
# the final authority on writes and multi-statements.
_SQL_READ_FIRST = ("SELECT", "WITH")
_SQL_GATE_ERROR = ("Only SELECT or WITH ... SELECT queries are allowed — "
                   "this view cannot change data.")


def _first_keyword(sql):
    """First SQL keyword, skipping whitespace/comments, upper-cased."""
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


def _table_csv_text(header, rows):
    """The viewed table as comma-delimited CSV text for pyrun seeding.

    pyrun seeds scratch tables from ``{f: [csv chunk]}`` with a comma
    dialect, so every format (csv/tsv/xlsx/ptable) is normalized through
    the viewer readers first — display text in, display text out.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([_cell_text(name) for name in header])
    for row in rows:
        writer.writerow([_cell_text(cell) for cell in row])
    return buf.getvalue()


def _json_window(payload):
    """(offset, limit) from a JSON body, clamped like the rows API."""
    try:
        offset = int((payload or {}).get("offset", 0))
        limit = int((payload or {}).get("limit", PAGE_SIZE))
    except (TypeError, ValueError):
        raise ValueError("offset/limit must be integers.") from None
    return max(0, offset), max(1, min(limit, ROWS_LIMIT_MAX))


def _query_error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.get("/")
def index():
    return ("Tables module: open a csv, tsv, xlsx or .ptable file to "
            "browse it in the viewer."), 200


@bp.get("/<int:file_id>")
def view(file_id):
    stored = _get_table_file(file_id)
    try:
        header, rows = _read_table(stored)
    except (ValueError, OSError) as exc:
        abort(400, description=f"Could not read table file: {exc}")
    types = [sniff_column_type([row[i] for row in rows])
             for i in range(len(header))]
    columns = []
    for i, name in enumerate(header):
        stats = column_stats([row[i] for row in rows]) \
            if types[i] == "number" else None
        columns.append({"name": name, "type": types[i], "stats": stats})
    return render_template(
        "tables/view.html",
        file=stored,
        columns=columns,
        rows=_display_rows(rows[:PAGE_SIZE]),
        total=len(rows),
        page_size=PAGE_SIZE,
        editable=(stored.extension or "").lower() == "ptable",
    )


@bp.get("/<int:file_id>/rows")
def rows_api(file_id):
    stored = _get_table_file(file_id)
    try:
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", PAGE_SIZE))
    except (TypeError, ValueError):
        abort(400, description="offset/limit must be integers.")
    offset = max(0, offset)
    limit = max(1, min(limit, ROWS_LIMIT_MAX))
    try:
        header, rows = _read_table(stored)
    except (ValueError, OSError) as exc:
        abort(400, description=f"Could not read table file: {exc}")
    types = [sniff_column_type([row[i] for row in rows])
             for i in range(len(header))]
    columns = [{"name": name, "type": ctype}
               for name, ctype in zip(header, types, strict=True)]
    window = _display_rows(rows[offset:offset + limit])
    return jsonify({"columns": columns, "rows": window, "total": len(rows)})


@bp.post("/<int:file_id>/query")
def query_api(file_id):
    """Run a read-only SQL filter over the viewed table.

    The table is seeded into the pyrun sandbox as ``t0`` (the ``{f:
    [csv chunk]}`` files map, mirroring the dashboards script-card
    construction) and the statement runs as ``db.query``. The
    first-keyword gate rejects anything but SELECT/WITH up front;
    pyrun.evaluate stays the final authority on writes and
    multi-statements. Errors are always ``{"ok": False, "error":
    <safe>}`` — never tracebacks, never raw sqlite text. The result
    mirrors the rows API shape so the filtered view replaces the grid.
    """
    stored = _get_table_file(file_id)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _query_error("Request body must be a JSON object.")
    sql = payload.get("sql", "")
    if not isinstance(sql, str) or not sql.strip():
        return _query_error("Enter a SELECT query to run "
                            "(e.g. SELECT * FROM t0 LIMIT 100).")
    if _first_keyword(sql) not in _SQL_READ_FIRST:
        return _query_error(_SQL_GATE_ERROR)
    try:
        offset, limit = _json_window(payload)
    except ValueError as exc:
        return _query_error(str(exc))
    try:
        header, rows = _read_table(stored)
    except (ValueError, OSError) as exc:
        return _query_error(f"Could not read table file: {exc}")
    try:
        from docindex_module_pyrun.evaluate import evaluate
    except ImportError:
        return _query_error("pyrun evaluator is unavailable.", 500)
    files_map = {"f": [_table_csv_text(header, rows)]}
    code = "res = db.query(" + json.dumps(sql) + ")\nres"
    try:
        outcome = evaluate(code, files=files_map)
    except Exception:  # noqa: BLE001 - the tool boundary never 500s raw
        return _query_error("Evaluation failed unexpectedly.", 500)
    if not isinstance(outcome, dict) or not outcome.get("ok"):
        error = outcome.get("error") if isinstance(outcome, dict) else None
        return _query_error(error or "Query failed.")
    records = outcome.get("value")
    if not isinstance(records, list):
        return _query_error("Query failed: unexpected result shape.")
    if not all(isinstance(rec, dict) for rec in records):
        return _query_error("Query failed: unexpected result shape.")
    if not records:
        return jsonify({"ok": True, "columns": [], "rows": [],
                        "total": 0})
    names = [str(key) for key in records[0].keys()]
    display = [[_cell_text(rec.get(name)) for name in names]
               for rec in records]
    types = [sniff_column_type([row[i] for row in display])
             for i in range(len(names))]
    columns = [{"name": name, "type": ctype}
               for name, ctype in zip(names, types, strict=False)]
    return jsonify({"ok": True, "columns": columns,
                    "rows": display[offset:offset + limit],
                    "total": len(records)})


@bp.post("/<int:file_id>/cell")
def cell_api(file_id):
    """Edit one .ptable cell: snapshot a version FIRST, then write.

    CSV/TSV/XLSX files are read-only in the viewer — they need "Save as
    .ptable" first, so the original workbook is never mutated in place.
    Files on read-only (mirror synced) drives are rejected with the drive
    named. The write goes through ``file_service.update_file_content``
    (version source ``"table-edit"``), so the snapshot-then-write order,
    size/checksum refresh and shadow refresh all match the file editor.
    """
    stored = _get_table_file(file_id)
    if (stored.extension or "").lower() != "ptable":
        return _query_error(
            "This file is read-only in the viewer — use 'Save as .ptable' "
            "first, then edit the .ptable copy.", 400)
    if file_service.is_readonly_file(stored):
        drive = stored.drive
        name = drive.name if drive is not None else "synced"
        return _query_error(
            f"File '{stored.name}' is on read-only drive '{name}' "
            f"and cannot be edited.", 403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _query_error("Request body must be a JSON object.")
    if "value" not in payload:
        return _query_error("Missing cell value.")
    row = payload.get("row")
    col = payload.get("col")
    if isinstance(row, bool) or not isinstance(row, int):
        return _query_error("Cell row must be an integer.")
    if isinstance(col, bool) or not isinstance(col, (int, str)):
        return _query_error("Cell col must be a column name or index.")
    try:
        document = tables_doc.load(_blob_text(stored))
    except (ValueError, OSError) as exc:
        return _query_error(f"Could not read table file: {exc}")
    names = [col_spec["name"] for col_spec in document["columns"]]
    if isinstance(col, int):
        col_idx = col
    elif col not in names:
        return _query_error(f"Unknown column {col!r}.")
    else:
        col_idx = names.index(col)
    if not 0 <= row < len(document["rows"]):
        return _query_error(f"Row {row} is out of range.")
    if not 0 <= col_idx < len(names):
        return _query_error(f"Column {col!r} is out of range.")
    document["rows"][row][col_idx] = payload["value"]
    try:
        text = tables_doc.serialize(tables_doc.validate(document))
    except ValueError as exc:
        return _query_error(str(exc) or "Invalid table value.")
    try:
        file_service.update_file_content(stored, text, source="table-edit")
    except ValueError as exc:
        return _query_error(str(exc) or "Could not save the cell.")
    except Exception:  # noqa: BLE001 - IO/SQLAlchemy must not 500 raw
        return _query_error("Could not save the cell.", 500)
    return jsonify({"ok": True,
                    "value": _cell_text(payload["value"])})


@bp.post("/<int:file_id>/save-as")
def save_as_api(file_id):
    """Convert a csv/tsv/xlsx view into a queryable .ptable dataset.

    One-way copy: a new .ptable file is created next to the source (same
    folder/drive, uniquified name) and the original workbook is never
    touched. Copies out of synced trees land at the files root instead,
    so unmanaged blobs are never injected into a sync tree.
    """
    stored = _get_table_file(file_id)
    if (stored.extension or "").lower() == "ptable":
        return _query_error("This file is already a .ptable table.")
    try:
        header, rows = _read_table(stored)
    except (ValueError, OSError) as exc:
        return _query_error(f"Could not read table file: {exc}")
    try:
        document = tables_doc.new_table(
            [{"name": name,
              "type": sniff_column_type([row[i] for row in rows])}
             for i, name in enumerate(header)],
            [[_cell_text(cell) for cell in row] for row in rows])
    except ValueError as exc:
        return _query_error(str(exc) or "Could not convert this file.")
    text = tables_doc.serialize(document)
    if len(text.encode("utf-8")) > current_app.config["MAX_FILE_SIZE"]:
        return _query_error("Table exceeds the file size limit.")
    stem = os.path.splitext(stored.name)[0].strip() or "table"
    keep_place = not stored.is_synced
    folder_id = stored.folder_id if keep_place else None
    drive_id = stored.drive_id if keep_place else None
    name = f"{stem}.ptable"
    suffix = 2
    while file_service.find_by_name(current_user.id, drive_id, folder_id,
                                    name) is not None:
        name = f"{stem} ({suffix}).ptable"
        suffix += 1
    stored_name = f"{uuid.uuid4().hex}.ptable"
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name)
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    except OSError:
        return _query_error("Could not save the .ptable copy.", 500)
    created = StoredFile(
        name=name, stored_name=stored_name, extension="ptable",
        mime_type="application/json", size=os.path.getsize(path),
        checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        folder_id=folder_id, drive_id=drive_id, user_id=current_user.id)
    db.session.add(created)
    db.session.flush()
    db.session.add(FileIndex(file_id=created.id, status="pending"))
    db.session.commit()
    file_service.invalidate_dupe_cache(current_user.id, drive_id)
    events.emit("file.uploaded", {
        "file_id": created.id,
        "drive_id": drive_id,
        "user_id": current_user.id,
    })
    return jsonify({"ok": True, "file_id": created.id, "name": name})


# ---------------------------------------------------------------------------
# T4: Pin as dashboard card + build-automation handoff
# ---------------------------------------------------------------------------

def _module_disabled_error(name, action):
    """Fail-closed JSON error when a collaborator module is off.

    Returns the ``{"ok": False, ...}`` response, or None when enabled —
    callers return it directly so the failure is never silent.
    """
    from app.services import module_service
    if not module_service.is_enabled(name):
        return _query_error(
            f"The {name} module is disabled — enable it to {action}.")
    return None


def _dashboards_doc():
    """Dashboards document service (card constructor + attach/save path).

    Imported lazily: every module package is in ``sys.modules`` after
    ``module_service.load_all``, whether or not the module is enabled.
    """
    return importlib.import_module("docindex_module_dashboards.doc")


def _table_stem(stored):
    return os.path.splitext(stored.name)[0].strip() or "table"


@bp.get("/<int:file_id>/pin-targets")
def pin_targets(file_id):
    """Dashboard picker data: the user's dashboards (most recently used
    first) plus the default pick and the filename-prefilled card title."""
    stored = _get_table_file(file_id)
    disabled = _module_disabled_error("dashboards", "pin this table")
    if disabled is not None:
        return disabled
    dash_doc = _dashboards_doc()
    dashboards = dash_doc.list_dashboards(current_user.id)
    items = [{"id": item.id, "name": item.name} for item in dashboards]
    default_id = dashboards[0].id if dashboards else None
    return jsonify({"ok": True, "dashboards": items,
                    "default_id": default_id,
                    "suggested_title": _table_stem(stored)})


@bp.post("/<int:file_id>/pin")
def pin_api(file_id):
    """Pin the table as a card on one of the user's dashboards.

    Reuses the dashboards' own attach + card path: ``attach_link`` (the
    same ownership checks and dedupe as ``POST
    /m/dashboards/<id>/attachments``) then a ``new_card("table", ...)``
    referencing the attachment, persisted through ``save_document``. No
    new StoredFile is created — only a card.
    """
    stored = _get_table_file(file_id)
    disabled = _module_disabled_error("dashboards", "pin this table")
    if disabled is not None:
        return disabled
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _query_error("Request body must be a JSON object.")
    try:
        dashboard_id = int(payload.get("dashboard_id"))
    except (TypeError, ValueError):
        return _query_error("Pick a dashboard to pin this table to.")
    dash_doc = _dashboards_doc()
    dashboard = dash_doc.get_dashboard(dashboard_id, current_user.id)
    if dashboard is None:
        return _query_error("Dashboard not found.", 404)
    title = str(payload.get("title") or "").strip() or _table_stem(stored)
    prior_ids = {a.get("id")
                 for a in dash_doc.load(dashboard).get("attachments", [])}
    try:
        attachment = dash_doc.attach_link(dashboard, stored.id)
    except ValueError as exc:
        return _query_error(str(exc) or "Could not attach this table.")
    card = dash_doc.new_card(
        "table", title=title,
        prompt=f"Show the key rows of {stored.name}.")
    card["inputs"]["attachments"] = [attachment["id"]]
    try:
        document = dash_doc.load(dashboard)
        document["cards"].append(card)
        dash_doc.validate(document)
        dash_doc.save_document(dashboard, document)
    except ValueError as exc:
        if attachment["id"] not in prior_ids:
            # The link was freshly attached by this pin and the card never
            # persisted — detach it so the dashboard keeps no orphan
            # attachment. Pre-existing (deduped) links are left alone.
            # detach_attachment refuses while a card references the id, so
            # a failure here can only leave the in-memory card behind.
            try:
                dash_doc.detach_attachment(dashboard, attachment["id"])
            except Exception:  # noqa: BLE001 - report the pin error, not this
                current_app.logger.exception(
                    "Could not detach orphan attachment after failed pin")
        return _query_error(str(exc) or "Could not pin this table.")
    return jsonify({"ok": True, "dashboard_id": dashboard.id,
                    "card_id": card["id"],
                    "attachment_id": attachment["id"],
                    "title": card["title"],
                    "url": url_for("dashboards.editor",
                                   file_id=dashboard.id)})


@bp.post("/<int:file_id>/build-automation")
def build_automation_api(file_id):
    """Create (or reuse) a workflow prefilled with an input.file node.

    The automation editor accepts no URL params for prefill (verified:
    automation.js reads no query string for prefill), so the prefill
    happens here at creation time and the response carries the live
    editor URL — never a dead button.

    Idempotent per (user, table): the handoff draft carries a
    ``[tables-handoff table:<id>]`` marker in its description. A repeat
    POST (double-click, retry) reuses the same row instead of littering
    the workflow list. Reuse repairs the prefill node when it is missing
    (re-point an existing input.file node, else append one) but otherwise
    preserves the user's edits; a graph that no longer validates resets
    to the pristine prefill rather than 500ing on the user's draft.
    """
    stored = _get_table_file(file_id)
    disabled = _module_disabled_error(
        "automation", "build an automation from this table")
    if disabled is not None:
        return disabled
    auto_models = importlib.import_module(
        "docindex_module_automation.models")
    auto_engine = importlib.import_module(
        "docindex_module_automation.engine")
    auto_registry = importlib.import_module(
        "docindex_module_automation.registry")

    def _prefill_node(node_id):
        return {"id": node_id, "type": "input.file",
                "x": 60, "y": 120,
                "config": {"file_id": stored.id}}

    def _validated_graph(graph):
        auto_engine.validate_graph(
            auto_engine.parse_graph(json.dumps(graph)),
            auto_registry.active_node_types())
        return json.dumps(graph)

    marker = f"[tables-handoff table:{stored.id}]"
    existing = (auto_models.AutomationWorkflow.query
                .filter_by(user_id=current_user.id)
                .filter(auto_models.AutomationWorkflow.description
                        .like(f"%{marker}%"))
                .order_by(auto_models.AutomationWorkflow.id)
                .first())
    if existing is not None:
        try:
            current = auto_engine.parse_graph(existing.graph)
        except Exception:  # noqa: BLE001 - corrupt draft resets to prefill
            current = {"nodes": [], "edges": []}
        nodes = current.get("nodes")
        if not isinstance(nodes, list):
            nodes = current["nodes"] = []
        prefilled = any(
            n.get("type") == "input.file"
            and isinstance(n.get("config"), dict)
            and (n.get("config") or {}).get("file_id") == stored.id
            for n in nodes if isinstance(n, dict))
        if not prefilled:
            file_nodes = [n for n in nodes
                          if isinstance(n, dict)
                          and n.get("type") == "input.file"]
            if file_nodes:
                config = file_nodes[0].get("config")
                if not isinstance(config, dict):
                    config = file_nodes[0]["config"] = {}
                config["file_id"] = stored.id
            else:
                taken = {n.get("id") for n in nodes
                         if isinstance(n, dict)}
                node_id, suffix = "n1", 1
                while node_id in taken:
                    suffix += 1
                    node_id = f"n{suffix}"
                nodes.append(_prefill_node(node_id))
            try:
                existing.graph = _validated_graph(current)
            except Exception:  # noqa: BLE001 - unrepairable: pristine prefill
                existing.graph = _validated_graph(
                    {"nodes": [_prefill_node("n1")], "edges": []})
        db.session.commit()
        return jsonify({"ok": True, "workflow_id": existing.id,
                        "file_id": stored.id, "reused": True,
                        "url": url_for("automation.editor",
                                       workflow_id=existing.id)})
    try:
        graph_text = _validated_graph(
            {"nodes": [_prefill_node("n1")], "edges": []})
    except Exception as exc:  # noqa: BLE001 - our construction must validate
        return _query_error(f"Could not prefill the workflow: {exc}", 500)
    workflow = auto_models.AutomationWorkflow(
        user_id=current_user.id, name=f"Table: {_table_stem(stored)}"[:160],
        description=f"Built from table {stored.name} {marker}.",
        graph=graph_text)
    db.session.add(workflow)
    db.session.commit()
    return jsonify({"ok": True, "workflow_id": workflow.id,
                    "file_id": stored.id, "reused": False,
                    "url": url_for("automation.editor",
                                   workflow_id=workflow.id)})
