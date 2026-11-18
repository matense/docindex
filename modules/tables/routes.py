"""Tables module routes: viewer page + rows JSON API (T2).

The viewer opens any viewer-owned table file (csv/tsv/xlsx/ptable) by
StoredFile id. The server paints the first 50 rows plus per-column type
badges and header stats; ``static/tables.js`` then slides a 50-row window
through ``GET /<id>/rows?offset&limit`` so the whole file never lands in
the DOM. The rows API shape ``{columns, rows, total}`` is the contract
T3/T4 build on.
"""

import csv
import io
import math
import os
from datetime import datetime

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user

from app.models import StoredFile
from app.services import file_service

from . import doc as tables_doc

bp = Blueprint("tables", __name__,
               template_folder="templates", static_folder="static")

TABLE_EXTENSIONS = ("csv", "tsv", "xlsx", "ptable")

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
    with open(path, "r", encoding="utf-8", errors="replace",
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
        with open(file_service.file_path(stored), "r",
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
    return "%g" % number


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
# Routes
# ---------------------------------------------------------------------------

@bp.get("/")
def index():
    # Skeleton only: listing tables is not part of T2.
    return "Tables module (viewer lands in T2).", 200


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
               for name, ctype in zip(header, types)]
    window = _display_rows(rows[offset:offset + limit])
    return jsonify({"columns": columns, "rows": window, "total": len(rows)})
