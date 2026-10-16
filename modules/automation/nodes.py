"""Built-in node types for the Automation module.

Each executor receives (ctx, config, inputs) and returns a payload (single
output) or {port: payload|None}. See registry.register_node_type.
"""

import io
import json
import re

from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import Drive, Folder, StoredFile
from app.services import (ai_service, file_service, indexing_service,
                          search_service)

from .engine import (WorkflowError, as_drive_id, as_files, as_json, as_table,
                      as_text, p_drive, p_files, p_json, p_table, p_text,
                      render_template, table_to_csv)
from .registry import register_node_type

_OPS = ("equals", "not_equals", "contains", "not_contains", "gt", "lt",
        "gte", "lte", "empty", "not_empty", "in", "not_in")

# Friendly select labels for the comparison operators (the stored value is
# still the internal op name — only the dropdown text changes).
_OPS_OPTIONS = [
    {"value": "equals", "label": "equals"},
    {"value": "not_equals", "label": "does not equal"},
    {"value": "contains", "label": "contains"},
    {"value": "not_contains", "label": "does not contain"},
    {"value": "gt", "label": "is greater than"},
    {"value": "lt", "label": "is less than"},
    {"value": "gte", "label": "is at least (>=)"},
    {"value": "lte", "label": "is at most (<=)"},
    {"value": "empty", "label": "is empty"},
    {"value": "not_empty", "label": "is not empty"},
    {"value": "in", "label": "is one of (a, b, …)"},
    {"value": "not_in", "label": "is not one of (a, b, …)"},
]

# One-click example behaviors for the AI nodes' system field — the editor
# offers them as an "Examples…" dropdown.
_BEHAVIOR_PRESETS = [
    {"label": "Translator",
     "value": "You are a professional translator. Translate the input "
              "faithfully and answer only with the translation — no "
              "commentary."},
    {"label": "Summarizer",
     "value": "You are a precise summarizer. Produce a clear, "
              "well-structured summary with the key points."},
    {"label": "Data extractor",
     "value": "You extract structured data from documents. Be precise, "
              "never invent values, and say when something is missing."},
    {"label": "Classifier",
     "value": "You classify inputs into the requested categories. Answer "
              "with just the category, nothing else."},
    {"label": "Friendly writer",
     "value": "You write clear, friendly prose for a general audience. "
              "Short sentences, no jargon."},
    {"label": "Proofreader",
     "value": "You are a meticulous proofreader. Fix grammar, spelling and "
              "punctuation while keeping the author's voice. Answer with "
              "the corrected text only."},
    {"label": "Meeting notes",
     "value": "You turn raw notes into structured meeting minutes: "
              "attendees if mentioned, decisions, and action items with "
              "owners."},
    {"label": "Code reviewer",
     "value": "You are a senior code reviewer. Point out bugs, risks and "
              "style issues, ordered by severity, with a one-line fix "
              "suggestion each."},
    {"label": "Spreadsheet analyst",
     "value": "You analyse tabular data. State the key numbers, trends "
              "and anomalies, and back every claim with values from the "
              "input."},
]


# One concrete usage example per node type, shown in the config popup under
# the description so the user sees what the node is capable of.
_NODE_EXAMPLES = {
    "input.text": "Type a note (or paste JSON) and feed it to an AI "
                  "prompt.",
    "input.trigger": "Right-click report.pdf in a drive → Run workflow — "
                     "this node outputs that file.",
    "input.param": "Asks \"Which region?\" when the workflow runs and "
                   "feeds the answer into an AI prompt.",
    "input.file": "sales.xlsx becomes a table you can filter or send to "
                  "the AI.",
    "docindex.search_files": "Query \"contract\" returns the matching files "
                             "for the next node to read.",
    "transform.template": "Writes a letter: \"Dear {{ n1 }}, …\" — the "
                          "text of node n1 is inserted where the ref is.",
    "transform.table_filter": "status is one of (paid, pending) AND total "
                              "is greater than 100.",
    "transform.table_select": "Pick name and total, renaming total to "
                              "Amount.",
    "transform.table_aggregate": "Group by region, sum of total as "
                                 "Revenue — one row per region.",
    "transform.table_join": "orders.customer_id = customers.id — every "
                            "order with its customer data.",
    "transform.table_sort": "Sort by total, descending — biggest sale "
                            "first.",
    "transform.table_dedupe": "Dedupe on email — one row per contact.",
    "transform.table_compute": "New column total = price * qty * 1.23.",
    "transform.image_info": "photo.png → width, height, format and size "
                            "as JSON.",
    "transform.ocr_image": "scan.png → the document's text, ready for an "
                           "AI prompt.",
    "transform.read_files": "After Search files: reads each match so the "
                            "AI can see the actual contents.",
    "ai.prompt": "\"Summarize the input in 5 bullet points\" → summary "
                 "text.",
    "ai.agent": "\"Find my contracts and list their renewal dates.\"",
    "ai.caption_image": "photo.png → \"A brown dog catching a frisbee in "
                        "a park.\"",
    "logic.condition": "left {{ n1 }} · contains · \"urgent\" — matching "
                       "data flows down the true branch.",
    "logic.merge": "After a condition: forwards whichever branch (true or "
                   "false) produced data.",
    "output.save_file": "Saves the AI's answer as summary.md in the drive "
                        "you pick.",
    "output.save_table": "The filtered sales table becomes report.xlsx in "
                         "the Reports drive.",
    "output.result": "Connect the final node here to see its output on "
                     "the run page.",
    "notebook.input": "Reads \"Meeting notes.pdocnb\" as text for an AI "
                      "prompt.",
    "notebook.output": "The AI digest replaces the \"Summary\" cell of "
                       "the weekly notebook.",
}


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------

def _owned_file(user, file_id):
    try:
        fid = int(file_id)
    except (TypeError, ValueError):
        raise WorkflowError("Configure a file first (no valid file selected).")
    stored = db.session.get(StoredFile, fid)
    if stored is None or stored.user_id != user.id \
            or stored.deleted_at is not None:
        raise WorkflowError(f"File #{fid} does not exist (or is in the trash).")
    return stored


def _compare(left, op, right):
    """Shared comparison for logic.condition and transform.table_filter."""
    left = "" if left is None else str(left)
    right = "" if right is None else str(right)
    if op == "empty":
        return not left.strip()
    if op == "not_empty":
        return bool(left.strip())
    if op in ("gt", "lt", "gte", "lte"):
        try:
            lnum, rnum = float(left), float(right)
        except ValueError:
            raise WorkflowError(f"Cannot compare '{left}' and '{right}' as "
                                "numbers.")
        return {"gt": lnum > rnum, "lt": lnum < rnum,
                "gte": lnum >= rnum, "lte": lnum <= rnum}[op]
    if op == "equals":
        return left == right
    if op == "not_equals":
        return left != right
    if op in ("in", "not_in"):
        # Comma-separated list on the right: status -> "paid, pending".
        options = [part.strip().lower() for part in right.split(",")
                   if part.strip()]
        found = left.strip().lower() in options
        return found if op == "in" else not found
    if op == "contains":
        return right.lower() in left.lower()
    if op == "not_contains":
        return right.lower() not in left.lower()
    raise WorkflowError(f"Unknown operator '{op}'.")


def _column_index(table, ref):
    """Column reference by name or 1-based index."""
    columns = table.get("columns", [])
    ref = str(ref).strip()
    if ref.isdigit():
        idx = int(ref) - 1
        if 0 <= idx < len(columns):
            return idx
    if ref in columns:
        return columns.index(ref)
    raise WorkflowError(f"Column '{ref}' does not exist "
                        f"(have: {', '.join(columns) or 'none'}).")


def _excel_table(path, ext):
    """First-sheet table from an Excel file (values only)."""
    if ext in ("xlsx", "xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [["" if c is None else c for c in row]
                for row in ws.iter_rows(values_only=True)]
        wb.close()
    elif ext == "xls":
        import xlrd
        wb = xlrd.open_workbook(path)
        ws = wb.sheet_by_index(0)
        rows = [[ws.cell_value(r, c) for c in range(ws.ncols)]
                for r in range(ws.nrows)]
    else:
        raise WorkflowError(f"'.{ext}' is not an Excel file.")
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return {"columns": [], "rows": []}
    width = max(len(r) for r in rows)
    rows = [list(r) + [""] * (width - len(r)) for r in rows]
    return {"columns": [str(c) for c in rows[0]], "rows": rows[1:]}


# --------------------------------------------------------------------------
# Input nodes
# --------------------------------------------------------------------------

def load_file_payload(user, file_id, log=None):
    """Read a user's file into a payload (text / table / json by extension).

    Shared by input.file, input.trigger and transform.read_files."""
    stored = _owned_file(user, file_id)
    ext = (stored.extension or "").lower()
    if log:
        log(f"reading '{stored.name}'")
    # Images carry no text — hand the file reference to the image nodes
    # (caption / OCR / info) instead of an empty text payload.
    if stored.is_image:
        return p_files([{"file_id": stored.id, "name": stored.name}])
    if ext in ("xlsx", "xlsm", "xls"):
        table = _excel_table(file_service.file_path(stored), ext)
        return p_table(table["columns"], table["rows"])
    if ext == "json":
        try:
            return p_json(json.loads(file_service.read_text_content(stored)))
        except ValueError:
            raise WorkflowError(f"'{stored.name}' is not valid JSON.")
    if ext == "csv":
        from .engine import csv_to_table
        table = csv_to_table(file_service.read_text_content(stored))
        return p_table(table["columns"], table["rows"])
    # Everything else: extracted/indexed text, or the raw text file.
    text = file_service.read_text_content(stored)
    if not text.strip() and stored.index and stored.index.extracted_text:
        text = stored.index.extracted_text
    return p_text(text)


def _exec_input_text(ctx, config, inputs):
    content = config.get("content", "")
    if config.get("format") == "json":
        try:
            return p_json(json.loads(content))
        except ValueError:
            raise WorkflowError("The input text is not valid JSON.")
    return p_text(content)


def _exec_input_file(ctx, config, inputs):
    return load_file_payload(ctx["user"], config.get("file_id"),
                             log=ctx["log"])


def resolve_drive_id(ctx, config, outputs, field="drive_id"):
    """Drive for a save/folder node: a picked drive id OR a {{ ref }} to an
    input.drive node (rendered against upstream outputs). Returns the Drive
    row or raises WorkflowError."""
    raw = config.get(field)
    if isinstance(raw, str) and "{{" in raw:
        raw = render_template(raw, outputs)
    try:
        drive_id = int(raw)
    except (TypeError, ValueError):
        # A {{ drive node }} ref renders as {"id": N, ...} JSON.
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            drive_id = int(data["id"]) if isinstance(data, dict) else None
            if drive_id is None:
                raise ValueError
        except (TypeError, ValueError, KeyError):
            drive_id = None
    if drive_id is None:
        raise WorkflowError("Pick a drive (or reference one with "
                            "{{ nX }} from a Drive picker node).")
    drive = db.session.get(Drive, drive_id)
    if drive is None or drive.user_id != ctx["user"].id:
        raise WorkflowError("The selected drive does not exist.")
    return drive


def _exec_input_drive(ctx, config, inputs):
    """Drive picker run parameter: the drive answered in the run form
    (user or AI), or the static drive below for schedules and triggers."""
    name = (config.get("param") or "").strip()
    if not name:
        raise WorkflowError("Give this drive input a parameter name.")
    ask = str(config.get("ask", "yes")) != "no"
    provided = (ctx.get("params") or {}).get(name)
    if ask and provided not in (None, ""):
        try:
            drive_id = int(provided)
        except (TypeError, ValueError):
            raise WorkflowError(f"Run parameter '{name}' is not a drive.")
        drive = db.session.get(Drive, drive_id)
        if drive is None or drive.user_id != ctx["user"].id:
            raise WorkflowError(f"Run parameter '{name}': that drive does "
                                "not exist (or is not yours).")
        ctx["log"](f"drive parameter '{name}' → '{drive.name}'")
        return p_drive(drive.id, drive.name)
    try:
        drive_id = int(config.get("drive_id"))
    except (TypeError, ValueError):
        drive_id = None
    drive = db.session.get(Drive, drive_id) if drive_id else None
    if drive is None or drive.user_id != ctx["user"].id:
        if ask:
            raise WorkflowError(f"Run parameter '{name}' was not provided.")
        raise WorkflowError(
            f"Drive parameter '{name}' has no drive — pick a static drive.")
    ctx["log"](f"using drive '{drive.name}'")
    return p_drive(drive.id, drive.name)


def _exec_input_param(ctx, config, inputs):
    """Run parameter: the value asked in the run form, or the static
    default when nobody answers (schedules, triggers, AI runs)."""
    name = (config.get("param") or "").strip()
    if not name:
        raise WorkflowError("Give this run input a parameter name.")
    kind = config.get("kind") or "text"
    ask = str(config.get("ask", "yes")) != "no"
    provided = (ctx.get("params") or {}).get(name)
    if ask and provided not in (None, ""):
        ctx["log"](f"run parameter '{name}' answered in the run form")
        value = provided
    elif kind == "file":
        value = config.get("file_id")
        if value in (None, ""):
            raise WorkflowError(
                f"Run parameter '{name}' has no file — answer it in the "
                "run form or pick a static file.")
    else:
        value = config.get("default") or ""
        if ask and not str(value).strip():
            raise WorkflowError(f"Run parameter '{name}' was not provided.")
    if kind == "file":
        return load_file_payload(ctx["user"], value, log=ctx["log"])
    return p_text(str(value))


def run_param_specs(graph):
    """The input.param nodes that want an answer in the run form.

    Returns [{param, label, kind, required}] for nodes with ask=yes;
    ``required`` means there is no static fallback, so the run cannot
    start without an answer."""
    specs = []
    for node in graph.get("nodes", []):
        if node.get("type") not in ("input.param", "input.drive"):
            continue
        config = node.get("config") or {}
        if str(config.get("ask", "yes")) == "no":
            continue
        if node.get("type") == "input.drive":
            kind, has_static = "drive", config.get("drive_id") not in (
                None, "")
        elif (kind := config.get("kind") or "text") == "file":
            has_static = bool(config.get("file_id"))
        else:
            has_static = bool(str(config.get("default") or "").strip())
        specs.append({
            "param": (config.get("param") or "").strip(),
            "label": (config.get("label") or "").strip()
                     or (config.get("param") or "").strip(),
            "kind": kind,
            "required": not has_static,
        })
    return specs


def _exec_trigger(ctx, config, inputs):
    payload = ctx.get("trigger")
    if payload is None:
        raise WorkflowError("This run has no trigger input — start it from "
                            "a file (drive → right-click → Run workflow).")
    name = ctx.get("trigger_name")
    ctx["log"](f"trigger input{f' ({name})' if name else ''}")
    return payload


def _exec_search_files(ctx, config, inputs):
    query = (config.get("query") or "").strip()
    if not query:
        raise WorkflowError("Configure a search query.")
    try:
        limit = max(1, min(int(config.get("limit") or 10), 50))
    except (TypeError, ValueError):
        raise WorkflowError("Max results must be a number (1-50).")
    hits = search_service.search_files(query, ctx["user"], limit=limit)
    files = [{"file_id": h["file"].id, "name": h["file"].name} for h in hits]
    ctx["log"](f"{len(files)} file(s) matched '{query}'")
    return p_files(files)


# --------------------------------------------------------------------------
# Transform nodes
# --------------------------------------------------------------------------

def _exec_template(ctx, config, outputs, inputs):
    return p_text(render_template(config.get("template", ""), outputs))


def _exec_json_path(ctx, config, inputs, outputs):
    source = inputs.get("in")
    if source is None and (config.get("source") or "").strip():
        try:
            source = p_json(json.loads(render_template(config["source"], outputs)
                                       or "null"))
        except ValueError:
            raise WorkflowError("The source is not valid JSON.")
    if source is None:
        raise WorkflowError("No input — connect a node or set the source.")
    data = as_json(source)
    path = (config.get("path") or "").strip()
    if path:
        for part in path.split("."):
            try:
                if isinstance(data, (list, tuple)):
                    data = data[int(part)]
                elif isinstance(data, dict):
                    data = data[part]
                else:
                    raise KeyError(part)
            except (KeyError, IndexError, ValueError):
                raise WorkflowError(f"Path '{path}' does not exist in the "
                                    "JSON data.")
    return p_json(data)


def _exec_json_to_table(ctx, config, inputs):
    """JSON → table: a list of objects becomes rows (keys = columns); a
    single object becomes a two-column key/value table."""
    if "in" not in inputs:
        raise WorkflowError("No input — connect a JSON source (a .json "
                            "file, an AI extract, …).")
    data = as_json(inputs["in"])
    if isinstance(data, list):
        if not data:
            raise WorkflowError("The JSON list is empty — no rows to "
                                "tabulate.")
        if not all(isinstance(item, dict) for item in data):
            raise WorkflowError("The JSON list contains simple values, not "
                                "objects — nothing to tabulate.")
        columns = []
        for item in data:
            for key in item:
                if key not in columns:
                    columns.append(key)
        rows = [[_cell(item.get(c)) for c in columns] for item in data]
        ctx["log"](f"{len(rows)} row(s), {len(columns)} column(s)")
        return p_table(columns, rows)
    if isinstance(data, dict):
        rows = [[key, _cell(value)] for key, value in data.items()]
        ctx["log"](f"object with {len(rows)} key(s) → key/value table")
        return p_table(["key", "value"], rows)
    raise WorkflowError("This JSON is neither a list of objects nor an "
                        "object — nothing to tabulate.")


def _cell(value):
    """Table cell from a JSON value (nested data is kept as JSON text)."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else value


def _exec_table_filter(ctx, config, inputs):
    if "in" not in inputs:
        raise WorkflowError("No input table — connect a CSV/Excel source.")
    table = as_table(inputs["in"])
    conditions = config.get("conditions")
    if not conditions:
        # Legacy single-condition config (column/op/value fields).
        conditions = [{"column": config.get("column", ""),
                       "op": config.get("op") or "equals",
                       "value": config.get("value", "")}]
    parsed = []
    for cond in conditions:
        column = str((cond or {}).get("column", "")).strip()
        if not column:
            continue
        parsed.append((_column_index(table, column),
                       cond.get("op") or "equals", cond.get("value", "")))
    if not parsed:
        raise WorkflowError("Add at least one condition (pick a column).")
    match_any = (config.get("match") or "all") == "any"

    def keep(row):
        hits = [idx < len(row) and _compare(row[idx], op, value)
                for idx, op, value in parsed]
        return any(hits) if match_any else all(hits)

    rows = [r for r in table["rows"] if keep(r)]
    ctx["log"](f"{len(rows)} of {len(table['rows'])} row(s) kept "
               f"({len(parsed)} condition(s), match "
               f"{'any' if match_any else 'all'})")
    return p_table(table["columns"], rows)


def _exec_table_select(ctx, config, inputs):
    if "in" not in inputs:
        raise WorkflowError("No input table — connect a table node.")
    table = as_table(inputs["in"])
    picks = config.get("picks")
    if picks:
        spec = [(str((p or {}).get("column", "")).strip(),
                 (str((p or {}).get("alias", "")).strip() or None))
                for p in picks]
        spec = [s for s in spec if s[0]]
    else:
        # Legacy "a, b as C" comma-separated string.
        spec = []
        for item in [c.strip() for c in (config.get("columns") or "")
                     .split(",") if c.strip()]:
            if " as " in item.lower():
                ref, alias = re.split(r"\s+as\s+", item,
                                      flags=re.IGNORECASE)
                spec.append((ref, alias))
            else:
                spec.append((item, None))
    if not spec:
        raise WorkflowError("Pick at least one column.")
    indexes, names = [], []
    for ref, alias in spec:
        idx = _column_index(table, ref)
        indexes.append(idx)
        names.append(alias or table["columns"][idx])
    rows = [[r[i] if i < len(r) else "" for i in indexes]
            for r in table["rows"]]
    ctx["log"](f"{len(names)} column(s), {len(rows)} row(s)")
    return p_table(names, rows)


def _exec_read_files(ctx, config, inputs):
    """Turn a files payload (e.g. from Search files) into one text payload
    with every file's content — ready to feed an AI prompt."""
    if "in" not in inputs:
        raise WorkflowError("No input — connect a file list (e.g. Search "
                            "files).")
    files = as_files(inputs["in"])
    if not files:
        raise WorkflowError("The input file list is empty.")
    try:
        max_chars = max(200, int(config.get("max_chars") or 6000))
    except (TypeError, ValueError):
        max_chars = 6000
    # Total cap: 50 files x 6000 chars would blow the prompt — clamp output.
    max_total = 120000
    parts = []
    total = 0
    for ref in files:
        payload = load_file_payload(ctx["user"], ref.get("file_id"))
        text = as_text(payload)
        note = ""
        if len(text) > max_chars:
            text = text[:max_chars]
            note = " (truncated)"
        parts.append(f"=== {ref.get('name', 'file')}{note} ===\n{text}")
        total += len(text)
        if total >= max_total:
            parts.append(f"... ({len(files) - len(parts)} more file(s) skipped "
                         f"— total cap {max_total} chars)")
            break
    ctx["log"](f"{len(parts)} file(s) read")
    return p_text("\n\n".join(parts))


def _exec_text_split(ctx, config, inputs):
    """Split text into chunks (by chars or lines) — poor-man's loop for long
    documents before per-chunk AI calls."""
    if "in" not in inputs:
        raise WorkflowError("No input — connect text first.")
    text = as_text(inputs["in"])
    mode = config.get("mode") or "chars"
    try:
        size = max(100, int(config.get("size") or 4000))
    except (TypeError, ValueError):
        raise WorkflowError("Chunk size must be a number.")
    if mode == "lines":
        lines = text.splitlines()
        chunks = ["\n".join(lines[i:i + size])
                  for i in range(0, len(lines), size)] or [""]
    else:
        chunks = [text[i:i + size] for i in range(0, len(text), size)] or [""]
    chunks = chunks[:50]  # safety cap
    ctx["log"](f"{len(chunks)} chunk(s)")
    return p_json({"chunks": chunks, "count": len(chunks)})


def _exec_folder_list(ctx, config, inputs, outputs=None):
    """List files in a drive folder as a files payload (no content read)."""
    drive = resolve_drive_id(ctx, config, outputs or {}, field="drive_id")
    folder_id = config.get("folder_id")
    if folder_id not in (None, ""):
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            raise WorkflowError("Invalid folder.")
        folder = db.session.get(Folder, folder_id)
        if folder is None or folder.drive_id != drive.id:
            raise WorkflowError("The folder is not in that drive.")
    else:
        folder_id = None
    ext = (config.get("extension") or "").strip().lower().lstrip(".")
    q = (StoredFile.query
         .filter_by(drive_id=drive.id, folder_id=folder_id,
                    user_id=ctx["user"].id)
         .filter(StoredFile.deleted_at.is_(None))
         .order_by(StoredFile.name).limit(100))
    if ext:
        q = q.filter(StoredFile.extension == ext)
    files = [{"file_id": f.id, "name": f.name} for f in q.all()]
    ctx["log"](f"{len(files)} file(s) in folder")
    return p_files(files)


def _exec_copy_file(ctx, config, inputs, outputs=None):
    """Copy one trigger/file payload to another drive (keeps content)."""
    if "in" not in inputs:
        raise WorkflowError("No input — connect a file or file list.")
    payload = inputs["in"]
    refs = as_files(payload) if payload["kind"] == "files" else None
    if refs is None:
        # Single file content passthrough is not copyable — require files.
        raise WorkflowError("Connect a file (trigger, file, search, folder).")
    if len(refs) != 1:
        raise WorkflowError("Copy one file at a time (got "
                            f"{len(refs)}). Use a filter first.")
    import shutil as _sh
    stored = db.session.get(StoredFile, int(refs[0]["file_id"]))
    if stored is None or stored.user_id != ctx["user"].id:
        raise WorkflowError("The file does not exist.")
    drive = resolve_drive_id(ctx, config, outputs or {})
    if drive.is_synced:
        raise WorkflowError(f"Drive '{drive.name}' is read-only (synced).")
    import uuid as _uuid
    src = file_service.file_path(stored)
    ext = stored.extension or ""
    sname = f"{_uuid.uuid4().hex}.{ext}" if ext else _uuid.uuid4().hex
    import os as _os
    dst = _os.path.join(file_service.UPLOAD_FOLDER
                        if hasattr(file_service, "UPLOAD_FOLDER")
                        else ctx["app"].config["UPLOAD_FOLDER"], sname)
    _sh.copyfile(src, dst)
    import hashlib as _hl
    h = _hl.sha256()
    with open(dst, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    copy = StoredFile(name=stored.name, stored_name=sname, extension=ext,
                      mime_type=stored.mime_type, size=_os.path.getsize(dst),
                      checksum=h.hexdigest(), folder_id=None,
                      user_id=ctx["user"].id, drive_id=drive.id)
    db.session.add(copy)
    db.session.flush()
    from app.models import FileIndex
    db.session.add(FileIndex(file_id=copy.id, status="pending"))
    db.session.commit()
    indexing_service.enqueue_index([copy.id])
    ctx["log"](f"copied to '{drive.name}'")
    return p_files([{"file_id": copy.id, "name": copy.name}])


def _exec_http_get(ctx, config, inputs, outputs):
    """GET JSON/text from a URL (SSRF-guarded, no redirects, size-capped)."""
    from urllib.parse import urlparse as _up
    import ipaddress as _ip
    import requests as _rq
    raw = render_template(config.get("url", ""), outputs).strip()
    if not raw:
        raise WorkflowError("Configure a URL.")
    try:
        p = _up(raw)
    except Exception:
        raise WorkflowError("Invalid URL.")
    if p.scheme not in ("http", "https"):
        raise WorkflowError("URL must start with http:// or https://.")
    host = (p.hostname or "").lower()
    if not host or host in ("169.254.169.254", "metadata.google.internal"):
        raise WorkflowError("That host is not allowed.")
    try:
        addr = _ip.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            raise WorkflowError("Private/local addresses are not allowed.")
    except ValueError:
        pass
    try:
        resp = _rq.get(raw, timeout=15, allow_redirects=False,
                       headers={"User-Agent": "DocIndex-Automation/1.0"})
    except Exception as exc:
        raise WorkflowError(f"Request failed: {exc}")
    if resp.status_code in (301, 302, 303, 307, 308):
        raise WorkflowError("Redirects are not followed — use the final URL.")
    if resp.status_code != 200:
        raise WorkflowError(f"Server returned {resp.status_code}.")
    body = resp.text[:50000]
    ctype = resp.headers.get("Content-Type", "")
    if "json" in ctype:
        try:
            import json as _js
            return p_json(_js.loads(body))
        except ValueError:
            pass
    return p_text(body)


# --------------------------------------------------------------------------
# AI nodes
# --------------------------------------------------------------------------

def _ai_config(user):
    config = ai_service.config_for(user)
    if not (config.get("enabled") and config.get("base_url")
            and config.get("model")):
        raise WorkflowError("AI is not configured — add a connection in "
                            "AI Settings.")
    return config


def _prompt_with_input(config, inputs, outputs):
    """Prompt text = rendered template + the connected input (if any).

    Whatever arrives on the "in" port (file content, table CSV, JSON, …)
    is appended as an Input block so the model actually sees it."""
    prompt = render_template(config.get("prompt", ""), outputs).strip()
    incoming = inputs.get("in")
    if incoming is not None:
        context_text = as_text(incoming).strip()
        if context_text:
            prompt = (prompt + "\n\nInput:\n" + context_text).strip() \
                if prompt else context_text
    return prompt


def _ai_messages(config, prompt, outputs):
    """Build the chat messages: optional behavior (system prompt) + user."""
    system = render_template(config.get("system", ""), outputs).strip()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def _exec_ai_prompt(ctx, config, inputs, outputs):
    prompt = _prompt_with_input(config, inputs, outputs)
    if not prompt:
        raise WorkflowError("Configure a prompt.")
    cfg = _ai_config(ctx["user"])
    ctx["log"](f"prompt → {cfg['model']}"
               + (" (+input)" if inputs.get("in") is not None else ""))
    try:
        message = ai_service.chat_completion(
            _ai_messages(config, prompt, outputs), config=cfg, block=True)
    except ai_service.AIError as exc:
        raise WorkflowError(str(exc))
    return p_text((message.get("content") or "").strip())


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _exec_ai_extract_json(ctx, config, inputs, outputs):
    prompt = _prompt_with_input(config, inputs, outputs)
    if not prompt:
        raise WorkflowError("Configure a prompt.")
    schema = (config.get("schema") or "").strip()
    instruction = ("Answer with ONLY valid JSON"
                   + (f" matching this shape: {schema}" if schema else "")
                   + ". No markdown fences, no commentary.")
    cfg = _ai_config(ctx["user"])
    messages = _ai_messages(config, prompt + "\n\n" + instruction, outputs)
    for attempt in (1, 2):
        ctx["log"](f"extract JSON → {cfg['model']} (try {attempt})")
        try:
            message = ai_service.chat_completion(messages, config=cfg,
                                                 block=True)
        except ai_service.AIError as exc:
            raise WorkflowError(str(exc))
        text = (message.get("content") or "").strip()
        candidate = _JSON_BLOCK_RE.search(text)
        try:
            return p_json(json.loads(candidate.group(1) if candidate
                                     else text))
        except ValueError:
            if attempt == 2:
                raise WorkflowError("The model did not return valid JSON "
                                    "after two attempts.")
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content":
                             "That was not valid JSON. Answer with ONLY the "
                             "JSON value, nothing else."})


def _exec_ai_agent(ctx, config, inputs, outputs):
    """Full DocIndex agent inside a workflow: the model may use every
    active tool (search/read files, module tools) to complete the task."""
    from app.services import agent_service
    prompt = _prompt_with_input(config, inputs, outputs)
    if not prompt:
        raise WorkflowError("Configure a task for the agent.")
    _ai_config(ctx["user"])  # fail fast with a clear error
    history = _ai_messages(config, prompt, outputs)
    answer, steps = "", []
    try:
        for kind, payload in agent_service.run_agent_events(
                ctx["user"], history, block=True):
            if kind == "step":
                steps.append(payload)
                ctx["log"](f"step {len(steps)}: {payload.get('label', '…')}")
            elif kind in ("thinking", "answer"):
                answer = payload
    except ai_service.AIError as exc:
        raise WorkflowError(str(exc))
    ctx["log"](f"agent finished after {len(steps)} step(s)")
    return p_text((answer or "").strip())


# --------------------------------------------------------------------------
# Logic nodes
# --------------------------------------------------------------------------

def _exec_condition(ctx, config, inputs, outputs):
    left = render_template(config.get("left", ""), outputs)
    right = render_template(config.get("right", ""), outputs)
    op = config.get("op") or "equals"
    taken = _compare(left, op, right)
    ctx["log"](f"'{left}' {op} '{right}' → {taken}")
    passthrough = inputs.get("in") or p_text(left)
    return {"true": passthrough if taken else None,
            "false": None if taken else passthrough}


def _exec_merge(ctx, config, inputs):
    for port in sorted(inputs):
        return inputs[port]
    raise WorkflowError("No input arrived — all upstream branches were "
                        "skipped.")


# --------------------------------------------------------------------------
# Notebook nodes (gated on the notebooks module being enabled)
# --------------------------------------------------------------------------

def _notebook_doc():
    """Lazy import: module_service loads notebooks AFTER automation
    (alphabetical), so the module object only exists at execution time."""
    import importlib
    try:
        return importlib.import_module("docindex_module_notebooks.doc")
    except ImportError:
        raise WorkflowError("The Notebooks module is not available.")


def _exec_notebook_input(ctx, config, inputs):
    doc = _notebook_doc()
    try:
        notebook_id = int(config.get("notebook_id"))
    except (TypeError, ValueError):
        raise WorkflowError("Configure a notebook first.")
    stored = doc.get_notebook(notebook_id, ctx["user"].id)
    if stored is None:
        raise WorkflowError(f"Notebook #{notebook_id} does not exist.")
    cell_id = str(config.get("cell_id") or "").strip()
    if cell_id:
        # Read one picked cell instead of the whole notebook.
        try:
            document = doc.load(stored)
        except ValueError as exc:
            raise WorkflowError(str(exc))
        _i, cell = doc.find_cell(document, cell_id)
        if cell is None:
            raise WorkflowError(
                f"Cell '{cell_id}' no longer exists in notebook "
                f"'{stored.name}' — pick another cell.")
        text = _cell_text(cell)
        ctx["log"](f"reading one cell from notebook '{stored.name}'")
        return p_text(text)
    text = doc.extract(stored)
    ctx["log"](f"reading notebook '{stored.name}'")
    return p_text(text)


def _cell_text(cell):
    """One notebook cell flattened to plain text (mirrors doc.extract)."""
    import re as _re
    ctype = cell.get("type")
    if ctype == "separator":
        return ""
    if ctype in ("markdown", "code", "heading"):
        return cell.get("content", "")
    if ctype == "richtext":
        stripped = _re.sub(r"<[^>]+>", " ", cell.get("content", ""))
        return " ".join(stripped.split())
    if ctype == "todo":
        return "\n".join(i.get("text", "")
                         for i in cell.get("meta", {}).get("items", []))
    if ctype == "table":
        meta = cell.get("meta", {})
        lines = [" | ".join(meta.get("headers", []))]
        lines.extend(" | ".join(r) for r in meta.get("rows", []))
        return "\n".join(line for line in lines if line.strip(" |"))
    return cell.get("content", "")


def _exec_notebook_output(ctx, config, inputs, outputs):
    payload = inputs.get("in")
    if payload is None:
        raise WorkflowError("No input — connect the content to write.")
    doc = _notebook_doc()
    text = as_text(payload).strip()
    if not text:
        raise WorkflowError("The input content is empty — nothing to write.")
    cell = doc.new_cell("markdown", text)

    notebook_id = config.get("notebook_id")
    if notebook_id not in (None, ""):
        # Update an existing notebook: a specific cell, replace all, or
        # append a new cell at the end.
        try:
            stored = doc.get_notebook(int(notebook_id), ctx["user"].id)
        except (TypeError, ValueError):
            stored = None
        if stored is None:
            raise WorkflowError(f"Notebook #{notebook_id} does not exist.")
        document = doc.load(stored)
        cell_id = str(config.get("cell_id") or "").strip()
        if cell_id:
            # Replace the content of one picked cell (its type is kept).
            try:
                doc.update_cell(document, cell_id, content=text)
            except ValueError:
                raise WorkflowError(
                    f"Cell '{cell_id}' no longer exists in notebook "
                    f"'{stored.name}' — pick another cell.")
            ctx["log"](f"updated a cell in notebook "
                       f"'{document.get('title', stored.name)}'")
        elif (config.get("mode") or "append") == "replace":
            document["cells"] = [cell]
            ctx["log"](f"replaced all cells in notebook "
                       f"'{document.get('title', stored.name)}'")
        else:
            document["cells"].append(cell)
            ctx["log"](f"appended a cell to notebook "
                       f"'{document.get('title', stored.name)}'")
        doc.save_document_ai(stored, document, note="Automation run")
        return p_files([{"file_id": stored.id, "name": stored.name}])

    # Create a new notebook in the configured drive.
    name = render_template(config.get("name", ""), outputs).strip()
    if not name:
        raise WorkflowError("Configure a name for the new notebook.")
    notebook_drive_id = config.get("drive_id")
    if isinstance(notebook_drive_id, str) and "{{" in notebook_drive_id:
        notebook_drive_id = render_template(notebook_drive_id, outputs)
    try:
        drive_id = int(notebook_drive_id)
    except (TypeError, ValueError):
        raise WorkflowError("Pick a drive for the new notebook.")
    drive = db.session.get(Drive, drive_id)
    if drive is None or drive.user_id != ctx["user"].id:
        raise WorkflowError("The selected drive does not exist.")
    if drive.is_synced:
        raise WorkflowError(f"Drive '{drive.name}' is read-only (synced).")
    try:
        stored = doc.create_notebook(ctx["user"], drive, name, cells=[cell])
    except ValueError as exc:
        raise WorkflowError(str(exc))
    ctx["log"](f"created notebook '{name}'")
    return p_files([{"file_id": stored.id, "name": stored.name}])


# --------------------------------------------------------------------------
# Output nodes
# --------------------------------------------------------------------------

def _save_blob_to_drive(ctx, config, blob, name, outputs=None):
    """Shared tail of the save nodes: validate name/drive/folder, persist,
    index, and record the created file."""
    if not name:
        raise WorkflowError("Configure a file name.")
    if "/" in name or "\\" in name:
        raise WorkflowError("The file name cannot contain path separators.")

    drive = resolve_drive_id(ctx, config, outputs or {})
    if drive.is_synced:
        raise WorkflowError(f"Drive '{drive.name}' is read-only (synced) — "
                            "pick a writable drive.")

    folder = None
    folder_path = (config.get("folder") or "").strip().strip("/")
    if folder_path:
        for part in folder_path.split("/"):
            folder = (Folder.query
                      .filter_by(drive_id=drive.id, name=part,
                                 parent_id=folder.id if folder else None)
                      .first())
            if folder is None:
                raise WorkflowError(f"Folder '{folder_path}' does not exist "
                                    f"in drive '{drive.name}'.")

    stored = file_service.save_upload(
        FileStorage(io.BytesIO(blob), filename=name), ctx["user"], folder)
    stored.drive_id = drive.id
    db.session.commit()
    indexing_service.enqueue_index([stored.id])
    ctx["log"](f"saved '{name}' to drive '{drive.name}'")
    ctx.setdefault("created_files", []).append(
        {"file_id": stored.id, "name": stored.name})
    return p_files([{"file_id": stored.id, "name": stored.name}])


def _exec_save_file(ctx, config, inputs, outputs):
    payload = inputs.get("in")
    if payload is None:
        raise WorkflowError("No input — connect the content to save.")
    name = render_template(config.get("name", ""), outputs).strip()

    kind = payload["kind"]
    if kind == "text":
        blob = as_text(payload).encode("utf-8")
    elif kind == "json":
        blob = json.dumps(payload["data"], ensure_ascii=False,
                          indent=2).encode("utf-8")
    elif kind == "table":
        blob = table_to_csv(payload["data"]).encode("utf-8")
    else:
        raise WorkflowError(f"Cannot save a {kind} payload as a file.")

    return _save_blob_to_drive(ctx, config, blob, name, outputs)


def _exec_result(ctx, config, inputs):
    payload = inputs.get("in")
    if payload is None:
        raise WorkflowError("No input — connect the result to report.")
    return payload


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

def register_defaults():
    """Register the built-in node types (idempotent)."""
    from .registry import _NODE_TYPES
    if _NODE_TYPES:
        return

    register_node_type("input.text", {
        "label": "Text input", "icon": "fa-keyboard", "category": "Input",
        "description": "A fixed piece of text (or JSON) you type yourself — "
                       "the starting point of the flow.",
        "inputs": [], "outputs": [{"name": "out", "kind": "text"}],
        "fields": [
            {"name": "content", "label": "Content", "kind": "textarea",
             "default": "", "placeholder": "Type the text here…",
             "presets": [
                 {"label": "Plain note",
                  "value": "Remember to water the plants every Monday."},
                 {"label": "JSON sample",
                  "value": '{\n  "items": [\n    {"name": "apple", "qty": 3},\n'
                           '    {"name": "pear", "qty": 1}\n  ]\n}'},
             ],
             "help": "Literal text — switch the format below to JSON to feed "
                     "structured data."},
            {"name": "format", "label": "Format", "kind": "select",
             "advanced": True,
             "options": [{"value": "text", "label": "Plain text"},
                         {"value": "json", "label": "JSON data"}],
             "default": "text"},
        ]}, _exec_input_text)

    register_node_type("input.trigger", {
        "label": "Trigger input", "icon": "fa-bolt", "category": "Input",
        "description": "The file this run was started from — e.g. via "
                       "right-click → Run workflow on a file in your drive. "
                       "Needs no configuration.",
        "inputs": [], "outputs": [{"name": "out", "kind": "any"}],
        "fields": []}, _exec_trigger)

    register_node_type("input.file", {
        "label": "File from drive", "icon": "fa-file-arrow-up",
        "category": "Input",
        "description": "Reads a file from one of your drives: CSV and Excel "
                       "become tables, JSON becomes structured data, and "
                       "everything else is read as text.",
        "inputs": [], "outputs": [{"name": "out", "kind": "any"}],
        "fields": [
            {"name": "file_id", "label": "File", "kind": "file",
             "required": True, "default": "",
             "help": "Read-only (synced) drives work fine "
                    "as inputs."},
        ]}, _exec_input_file)

    register_node_type("input.param", {
        "label": "Run input", "icon": "fa-circle-question",
        "category": "Input",
        "description": "A value the workflow asks for when it runs — a "
                       "question, a text, a number or a file. With \"ask at "
                       "run time\" off, the static default below is used "
                       "instead, so scheduled and triggered runs work "
                       "without anyone answering.",
        "inputs": [], "outputs": [{"name": "out", "kind": "any"}],
        "fields": [
            {"name": "param", "label": "Parameter name", "kind": "text",
             "required": True, "default": "", "placeholder": "e.g. question",
             "help": "Unique within the workflow — reference it as "
                     "{{ n1 }} from other nodes."},
            {"name": "label", "label": "Question shown in the run form",
             "kind": "text", "default": "",
             "placeholder": "e.g. Which region should I analyse?"},
            {"name": "kind", "label": "Answer type", "kind": "select",
             "options": [{"value": "text", "label": "Short text"},
                         {"value": "longtext", "label": "Long text"},
                         {"value": "number", "label": "Number"},
                         {"value": "file", "label": "File from a drive"}],
             "default": "text"},
            {"name": "ask", "label": "Ask at run time", "kind": "select",
             "advanced": True,
             "options": [{"value": "yes", "label": "Yes — ask every run"},
                         {"value": "no", "label": "No — always use the "
                                                  "static value"}],
             "default": "yes"},
            {"name": "default", "label": "Static default", "kind": "text",
             "advanced": True, "default": "",
             "show_when": {"field": "kind", "not_in": ["file"]},
             "help": "Used when nobody answers (schedules, triggers, AI) "
                     "or when asking is off."},
            {"name": "file_id", "label": "Static file", "kind": "file",
             "advanced": True, "default": "",
             "show_when": {"field": "kind", "equals": "file"},
             "help": "Used when nobody answers or asking is off."},
        ]}, _exec_input_param)

    register_node_type("input.drive", {
        "label": "Drive input", "icon": "fa-hard-drive",
        "category": "Input",
        "description": "A drive the workflow asks for when it runs — pick "
                       "it in the run form (or let the AI pass it). Use "
                       "{{ nX }} with this node's id inside any Drive "
                       "field of a save/folder node to write there.",
        "example": "Ask which drive, then save the report into it.",
        "inputs": [], "outputs": [{"name": "out", "kind": "drive"}],
        "fields": [
            {"name": "param", "label": "Parameter name", "kind": "text",
             "required": True, "default": "", "placeholder": "e.g. target",
             "help": "Unique within the workflow — reference its drive as "
                     "{{ nX }} in Drive fields (replace nX with this "
                     "node's id)."},
            {"name": "label", "label": "Question shown in the run form",
             "kind": "text", "default": "",
             "placeholder": "e.g. Which drive should I save to?"},
            {"name": "drive_id", "label": "Static drive", "kind": "drive",
             "default": "",
             "help": "Used when nobody answers (schedules, triggers, AI) "
                     "or when asking is off."},
            {"name": "ask", "label": "Ask at run time", "kind": "select",
             "advanced": True,
             "options": [{"value": "yes", "label": "Yes — ask every run"},
                         {"value": "no", "label": "No — always use the "
                                                  "static drive"}],
             "default": "yes"},
        ]}, _exec_input_drive)

    register_node_type("docindex.search_files", {
        "label": "Search files", "icon": "fa-magnifying-glass",
        "category": "Input",
        "description": "Finds files across your drives with the full-text "
                       "search and passes the list on.",
        "inputs": [], "outputs": [{"name": "out", "kind": "files"}],
        "fields": [
            {"name": "query", "label": "Query", "kind": "text",
             "required": True, "default": "", "placeholder": "e.g. invoice 2025",
             "help": "Full-text search across your drives."},
            {"name": "limit", "label": "Max results", "kind": "number",
             "advanced": True, "default": 10},
        ]}, _exec_search_files)

    register_node_type("transform.template", {
        "label": "Template", "icon": "fa-pen-ruler", "category": "Transform",
        "description": "Writes a new text by hand, inserting the output of "
                       "other nodes wherever you need it. Write the text "
                       "normally and place {{ n1 }} where the output of node "
                       "n1 should appear — every node shows its id (n1, n2, "
                       "…) in the corner of its card.",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "text"}],
        "fields": [
            {"name": "template", "label": "Template", "kind": "textarea",
             "required": True, "default": "", "placeholder": "# Report\n\n{{ n1 }}",
             "presets": [
                 {"label": "Pass a node through unchanged",
                  "value": "{{ n1 }}"},
                 {"label": "Report wrapper",
                  "value": "# Report\n\n{{ n1 }}\n\n— generated by DocIndex "
                           "Automation"},
                 {"label": "Letter / greeting",
                  "value": "Dear {{ n1 }},\n\nThank you for your message."
                           "\n\nBest regards"},
                 {"label": "Digest with a title",
                  "value": "Results:\n\n{{ n1 }}"},
                 {"label": "Combine two nodes",
                  "value": "First part:\n{{ n1 }}\n\nSecond part:\n{{ n2 }}"},
                 {"label": "One field of JSON data",
                  "value": "The customer name is {{ n1.name }}."},
             ],
             "help": "{{ n1 }} inserts the whole output of node n1. "
                     "{{ n1.name }} digs one field out of JSON data "
                     "(use dots for each level)."},
        ]}, lambda ctx, config, inputs: _exec_template(
            ctx, config, ctx["_outputs"], inputs))

    register_node_type("transform.json_path", {
        "label": "Pick a value from JSON", "icon": "fa-code-branch",
        "category": "Transform",
        "description": "Picks one value out of JSON data — write the names "
                       "of the levels separated by dots.",
        "example": "In {\"user\": {\"name\": \"Ana\"}}, the path user.name "
                   "returns \"Ana\".",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "json"}],
        "fields": [
            {"name": "path", "label": "Path", "kind": "text", "default": "",
             "placeholder": "data.items.0.name",
             "presets": [
                 {"label": "First item of a list", "value": "items.0"},
                 {"label": "Field of the first item",
                  "value": "items.0.name"},
                 {"label": "Whole document", "value": ""},
             ],
             "help": "Dots go one level deeper; a number picks an item from "
                     "a list (0 = first). Leave empty for the whole "
                     "document."},
        ]}, lambda ctx, config, inputs: _exec_json_path(
            ctx, config, inputs, ctx["_outputs"]))

    register_node_type("transform.json_to_table", {
        "label": "JSON to table", "icon": "fa-table",
        "category": "Data",
        "description": "Turns JSON data into a table you can filter, "
                       "select columns from, or save as CSV — the easiest "
                       "way to work with JSON files.",
        "example": "[{\"name\": \"Ana\", \"age\": 30}] becomes a table "
                   "with columns name and age.",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "table"}],
        "fields": []}, _exec_json_to_table)

    register_node_type("transform.table_filter", {
        "label": "Filter rows", "icon": "fa-filter", "category": "Data",
        "description": "Keeps only the table rows that match your "
                       "conditions. Connect a CSV/Excel file and pick the "
                       "columns from a dropdown — no typing needed.",
        "example": "status is one of (paid, pending) AND total is greater "
                   "than 100.",
        "inputs": [{"name": "in", "kind": "table"}],
        "outputs": [{"name": "out", "kind": "table"}],
        "fields": [
            {"name": "conditions", "label": "Conditions",
             "kind": "conditions", "required": True,
             "options": list(_OPS_OPTIONS),
             "default": [],
             "help": "Add one row per condition. For 'is one of', write "
                     "the values separated by commas (e.g. paid, pending)."},
        ]}, _exec_table_filter)

    register_node_type("transform.table_select", {
        "label": "Select columns", "icon": "fa-table-columns",
        "category": "Data",
        "description": "Keeps only the columns you pick — and can rename "
                       "them. Connect a CSV/Excel file and choose the "
                       "columns from a dropdown.",
        "example": "Pick name and total, renaming total to Amount.",
        "inputs": [{"name": "in", "kind": "table"}],
        "outputs": [{"name": "out", "kind": "table"}],
        "fields": [
            {"name": "picks", "label": "Columns", "kind": "columns",
             "required": True, "default": [],
             "help": "Add one row per column, in the order you want them; "
                     "the rename field is optional."},
        ]}, _exec_table_select)

    register_node_type("transform.read_files", {
        "label": "Read file contents", "icon": "fa-book-open",
        "category": "Transform",
        "description": "Reads the content of every file in the list and "
                       "joins them into one text — perfect right before an "
                       "AI prompt.",
        "inputs": [{"name": "in", "kind": "files"}],
        "outputs": [{"name": "out", "kind": "text"}],
        "fields": [
            {"name": "max_chars", "label": "Max chars per file",
             "kind": "number", "advanced": True, "default": 6000,
             "help": "Longer files are truncated to this size."},
        ]}, _exec_read_files)

    register_node_type("transform.text_split", {
        "label": "Split text into chunks", "icon": "fa-scissors",
        "category": "Transform",
        "description": "Splits long text into smaller chunks (JSON "
                       "{chunks, count}) so each piece fits the AI.",
        "example": "12000 chars / 4000 → 3 chunks.",
        "example": "12000 chars / 4000 → 3 chunks.",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "json"}],
        "fields": [
            {"name": "mode", "label": "Split by", "kind": "select",
             "options": [{"value": "chars", "label": "Characters"},
                         {"value": "lines", "label": "Lines"}],
             "default": "chars"},
            {"name": "size", "label": "Chunk size", "kind": "number",
             "default": 4000, "help": "Chars or lines per chunk (max 50)."},
        ]}, _exec_text_split)

    register_node_type("input.folder_list", {
        "label": "List folder files", "icon": "fa-folder-open",
        "category": "Input",
        "description": "Lists up to 100 files in a drive folder as a file "
                       "list — chain with Read contents or Copy.",
        "example": "Reports/ → [report.pdf, notes.txt] as a file list.",
        "inputs": [], "outputs": [{"name": "out", "kind": "files"}],
        "fields": [
            {"name": "drive_id", "label": "Drive", "kind": "drive",
             "required": True, "default": ""},
            {"name": "folder_id", "label": "Folder", "kind": "folder",
             "return": "id", "advanced": True, "default": "",
             "help": "Empty = the drive root."},
            {"name": "extension", "label": "Only extension (optional)",
             "kind": "text", "advanced": True, "default": "",
             "placeholder": "pdf"},
        ]}, lambda ctx, config, inputs: _exec_folder_list(
            ctx, config, inputs, ctx["_outputs"]))

    register_node_type("output.copy_file", {
        "label": "Copy file to drive", "icon": "fa-copy",
        "category": "Output",
        "description": "Copies one file (trigger/search result) to another "
                       "writable drive.",
        "example": "report.pdf → copy to Work drive.",
        "inputs": [{"name": "in", "kind": "files"}],
        "outputs": [{"name": "out", "kind": "files"}],
        "fields": [
            {"name": "drive_id", "label": "Target drive", "kind": "drive",
             "required": True, "default": "",
             "help": "Must be writable (not synced). {{ n1 }} refs work."},
        ]}, lambda ctx, config, inputs: _exec_copy_file(
            ctx, config, inputs, ctx["_outputs"]))

    register_node_type("input.http", {
        "label": "Fetch URL (GET)", "icon": "fa-globe",
        "category": "Input",
        "description": "Fetches JSON or text from a public URL (no "
                       "redirects, 50k cap). Supports {{ }} refs.",
        "example": "https://api.example.com/data.json → JSON data.",
        "inputs": [], "outputs": [{"name": "out", "kind": "any"}],
        "fields": [
            {"name": "url", "label": "URL", "kind": "text",
             "required": True, "default": "",
             "placeholder": "https://…",
             "help": "Public http(s) only — no localhost/metadata."},
        ]}, lambda ctx, config, inputs: _exec_http_get(
            ctx, config, inputs, ctx["_outputs"]))

    register_node_type("ai.prompt", {
        "label": "AI prompt", "icon": "fa-wand-magic-sparkles",
        "category": "AI",
        "description": "Sends a prompt to your AI model and returns the "
                       "answer as text. Whatever arrives on the input port "
                       "is appended automatically.",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "text"}],
        "fields": [
            {"name": "prompt", "label": "Prompt", "kind": "textarea",
             "required": True, "default": "",
             "placeholder": "e.g. Summarize the input in 5 bullet points",
             "presets": [
                 {"label": "Summarize",
                  "value": "Summarize the input in 5 bullet points."},
                 {"label": "Translate to English",
                  "value": "Translate the input to English."},
                 {"label": "Action items",
                  "value": "List every action item mentioned in the input, "
                           "with an owner if one is named."},
                 {"label": "Key facts as a table",
                  "value": "Extract the key facts from the input as a "
                           "markdown table."},
             ],
             "help": "Supports {{ node_id }} references. The connected "
                     "input (file, table, …) is appended automatically."},
            {"name": "system", "label": "Behavior (system prompt)",
             "kind": "textarea", "advanced": True, "default": "",
             "placeholder": "Optional — pick an example on the right or "
                            "write your own",
             "presets": list(_BEHAVIOR_PRESETS),
             "presets_source": "behaviors",
             "help": "Who the AI should be and how it should answer."},
        ]}, lambda ctx, config, inputs: _exec_ai_prompt(
            ctx, config, inputs, ctx["_outputs"]))

    register_node_type("ai.agent", {
        "label": "AI agent (tools)", "icon": "fa-robot",
        "category": "AI",
        "description": "A full DocIndex agent: it can search and read your "
                       "files (and use other modules' tools) to complete "
                       "the task.",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "text"}],
        "fields": [
            {"name": "prompt", "label": "Task", "kind": "textarea",
             "required": True, "default": "",
             "placeholder": "e.g. Search my files for contracts and "
                            "summarize them",
             "presets": [
                 {"label": "Research digest",
                  "value": "Search my files for the topic in the input and "
                           "write a short digest of what you find."},
                 {"label": "Check against my files",
                  "value": "Compare the input with related files in my "
                           "drives and list anything that contradicts it."},
             ],
             "help": "The connected input is appended automatically. The "
                     "agent decides which tools to use, step by step."},
            {"name": "system", "label": "Behavior (system prompt)",
             "kind": "textarea", "advanced": True, "default": "",
             "placeholder": "Optional — extra instructions for the agent",
             "presets": list(_BEHAVIOR_PRESETS),
             "presets_source": "behaviors",
             "help": "Optional extra instructions on top of the agent's "
                     "own behavior."},
        ]}, lambda ctx, config, inputs: _exec_ai_agent(
            ctx, config, inputs, ctx["_outputs"]))

    register_node_type("ai.extract_json", {
        "label": "AI extract data (JSON)", "icon": "fa-braille",
        "category": "AI",
        "description": "Asks the AI to pull structured data out of the "
                       "input and guarantees the result is valid JSON — "
                       "ready for 'JSON to table' or 'Pick a value from "
                       "JSON'.",
        "example": "From an invoice PDF: {\"number\": \"INV-42\", "
                   "\"total\": 199.90}",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "json"}],
        "fields": [
            {"name": "prompt", "label": "What to extract", "kind": "textarea",
             "required": True, "default": "",
             "placeholder": "e.g. Extract the invoice number, date and "
                            "total",
             "presets": [
                 {"label": "Invoice fields",
                  "value": "Extract the invoice number, date, total and "
                           "currency from the input."},
                 {"label": "People mentioned",
                  "value": "List the people mentioned in the input, with "
                           "their role if stated."},
             ],
             "help": "Write it in plain language — the connected input "
                     "(file, table, …) is appended automatically."},
            {"name": "schema", "label": "Shape of the answer (optional)",
             "kind": "textarea", "advanced": True, "default": "",
             "placeholder": '{"number": "...", "total": 0}',
             "presets": [
                 {"label": "Invoice",
                  "value": '{"number": "...", "date": "...", "total": 0, '
                           '"currency": "..."}'},
                 {"label": "List of people",
                  "value": '{"people": [{"name": "...", "role": "..."}]}'},
             ],
             "help": "An example of the JSON you want back. Leave empty "
                     "and the AI decides the shape."},
            {"name": "system", "label": "Behavior (system prompt)",
             "kind": "textarea", "advanced": True, "default": "",
             "placeholder": "Optional — pick an example on the right or "
                            "write your own",
             "presets": list(_BEHAVIOR_PRESETS),
             "presets_source": "behaviors",
             "help": "Optional persona/instructions for the model."},
        ]}, lambda ctx, config, inputs: _exec_ai_extract_json(
            ctx, config, inputs, ctx["_outputs"]))

    register_node_type("logic.condition", {
        "label": "Condition", "icon": "fa-code-fork", "category": "Logic",
        "description": "Sends the data down the true or the false branch, "
                       "depending on a comparison. Type a value or pick a "
                       "{{ n1 }} reference — the dropdowns suggest the "
                       "other nodes of this workflow.",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "true", "kind": "any"},
                    {"name": "false", "kind": "any"}],
        "fields": [
            {"name": "left", "label": "Compare this", "kind": "ref_or_text",
             "required": True, "default": "", "placeholder": "value or {{ n1 }}",
             "help": "A fixed value, or {{ node_id }} for another node's "
                     "output."},
            {"name": "op", "label": "Operator", "kind": "select",
             "options": list(_OPS_OPTIONS), "default": "equals"},
            {"name": "right", "label": "To this", "kind": "ref_or_text",
             "default": "", "placeholder": "value to compare against",
             "show_when": {"field": "op",
                           "not_in": ["empty", "not_empty"]}},
        ]}, lambda ctx, config, inputs: _exec_condition(
            ctx, config, inputs, ctx["_outputs"]))

    register_node_type("logic.merge", {
        "label": "Merge", "icon": "fa-code-merge", "category": "Logic",
        "description": "Forwards whichever branch produced data — use it "
                       "after a condition to join the flow again. Needs no "
                       "configuration.",
        "inputs": [{"name": "in1", "kind": "any"},
                   {"name": "in2", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "any"}],
        "fields": []}, _exec_merge)

    register_node_type("output.save_file", {
        "label": "Save file to drive", "icon": "fa-floppy-disk",
        "category": "Output",
        "description": "Writes the incoming content to a new file on one "
                       "of your drives (tables are saved as CSV).",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "files"}],
        "fields": [
            {"name": "name", "label": "File name", "kind": "text",
             "default": "output.txt", "placeholder": "report.txt",
             "help": "Supports {{ node_id }} references."},
            {"name": "drive_id", "label": "Drive", "kind": "drive",
             "required": True, "default": "",
             "help": "Must be writable (not a synced drive). Or pick "
                     "Custom and type {{ nX }} for a Drive picker node."},
            {"name": "folder", "label": "Folder", "kind": "folder",
             "return": "path", "advanced": True, "default": "",
             "placeholder": "Reports/2026",
             "help": "Pick an existing folder — or leave empty for the drive root."},
        ]}, lambda ctx, config, inputs: _exec_save_file(
            ctx, config, inputs, ctx["_outputs"]))

    register_node_type("output.result", {
        "label": "Workflow result", "icon": "fa-flag-checkered",
        "category": "Output",
        "description": "Marks the final output of the workflow — it is "
                       "what the run page shows. Needs no configuration.",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "any"}],
        "fields": []}, _exec_result)

    register_node_type("notebook.input", {
        "label": "Notebook input", "icon": "fa-book", "category": "Input",
        "description": "Reads a notebook as text — the whole thing, or one "
                       "specific cell you pick.",
        "example": "The Summary cell of the weekly notebook, straight into "
                   "an AI prompt.",
        "inputs": [], "outputs": [{"name": "out", "kind": "text"}],
        "fields": [
            {"name": "notebook_id", "label": "Notebook", "kind": "notebook",
             "required": True, "default": "",
             "help": "Empty cell below = all cells flattened into one text."},
            {"name": "cell_id", "label": "Only one specific cell (optional)",
             "kind": "notebook_cell", "advanced": True, "default": "",
             "help": "Pick a cell to read just that one instead of the "
                     "whole notebook."},
        ]}, _exec_notebook_input, module="notebooks")

    register_node_type("notebook.output", {
        "label": "Save to notebook", "icon": "fa-book-medical",
        "category": "Output",
        "description": "Writes the incoming content into a notebook. Pick "
                       "a notebook to update it — append, replace, or edit "
                       "one specific cell — or leave it empty to create a "
                       "new notebook.",
        "example": "The AI digest replaces the \"Summary\" cell of the "
                   "weekly notebook.",
        "inputs": [{"name": "in", "kind": "any"}],
        "outputs": [{"name": "out", "kind": "files"}],
        "fields": [
            {"name": "notebook_id", "label": "Notebook",
             "kind": "notebook", "default": "",
             "help": "Pick one to update it, or leave empty to create a "
                     "new notebook."},
            {"name": "mode", "label": "When updating", "kind": "select",
             "advanced": True,
             "options": [{"value": "append", "label": "Append a new cell"},
                         {"value": "replace", "label": "Replace all cells"}],
             "default": "append",
             "show_when": {"field": "notebook_id"}},
            {"name": "cell_id", "label": "…or update one specific cell",
             "kind": "notebook_cell", "advanced": True, "default": "",
             "show_when": {"field": "notebook_id"},
             "help": "Optional — replaces that cell's content and "
                     "overrides the mode above."},
            {"name": "name", "label": "New notebook name", "kind": "text",
             "default": "", "placeholder": "e.g. Weekly digest",
             "show_when": {"field": "notebook_id", "empty": True},
             "help": "Supports {{ node_id }} references."},
            {"name": "drive_id", "label": "Drive (for the new notebook)",
             "kind": "drive", "default": "",
             "show_when": {"field": "notebook_id", "empty": True},
             "help": "Must be writable (not a synced drive). Or pick "
                     "Custom and type {{ nX }} for a Drive picker node."},
        ]}, lambda ctx, config, inputs: _exec_notebook_output(
            ctx, config, inputs, ctx["_outputs"]), module="notebooks")

    # Pandas-backed table transforms and the table output node.
    from .nodes_tables import register_table_nodes
    register_table_nodes()

    # Image nodes (AI caption, metadata, OCR).
    from .nodes_images import register_image_nodes
    register_image_nodes()

    # Attach the per-node usage examples declared at the top of the file.
    for _type, _example in _NODE_EXAMPLES.items():
        if _type in _NODE_TYPES:
            _NODE_TYPES[_type]["spec"].setdefault("example", _example)
