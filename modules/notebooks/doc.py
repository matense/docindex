"""Notebook document service.

A notebook is a regular StoredFile with the .pdocnb extension whose blob is a
JSON document:

    {"version": 1, "title": "...", "cells": [
        {"id": "...", "type": "markdown|code|todo|table",
         "content": "...", "meta": {...}}]}

Cell payloads:
- markdown: content = markdown source (tables, task lists, file:// links).
- richtext: content = sanitized HTML (WYSIWYG editor in the UI).
- code:     content = source, meta.language = highlight.js language.
- todo:     meta.items = [{"text": str, "done": bool}].
- table:    meta.headers = [str], meta.rows = [[str, ...]].

Because notebooks are StoredFiles they get ownership, drives/folders, trash,
FileVersion history, FTS search and the AI agent's core tools for free.
"""

import hashlib
import json
import os
import re
import uuid
from datetime import timedelta, timezone
from io import BytesIO

from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import StoredFile, utcnow
from app.services import file_service, indexing_service

EXTENSION = "pdocnb"
MIME_TYPE = "application/json"
SNAPSHOT_INTERVAL = timedelta(minutes=2)
CELL_TYPES = ("markdown", "richtext", "code", "todo", "table")

_TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------

def new_cell(cell_type="markdown", content="", meta=None):
    if cell_type not in CELL_TYPES:
        raise ValueError(f"Unknown cell type '{cell_type}'.")
    cell = {"id": uuid.uuid4().hex[:12], "type": cell_type,
            "content": content or "", "meta": meta or {}}
    return _validate_cell(cell)


def empty_document(title="Untitled notebook"):
    return {"version": 1, "title": title, "cells": [new_cell()]}


def _validate_cell(cell):
    if not isinstance(cell, dict):
        raise ValueError("Each cell must be an object.")
    cell.setdefault("id", uuid.uuid4().hex[:12])
    if cell.get("type") not in CELL_TYPES:
        raise ValueError(f"Unknown cell type '{cell.get('type')}'.")
    cell["content"] = str(cell.get("content") or "")
    meta = cell.get("meta")
    cell["meta"] = meta if isinstance(meta, dict) else {}
    if cell["type"] == "todo":
        items = cell["meta"].get("items") or []
        cell["meta"]["items"] = [
            {"text": str(i.get("text", "")), "done": bool(i.get("done"))}
            for i in items if isinstance(i, dict)][:500]
    if cell["type"] == "table":
        headers = [str(h) for h in (cell["meta"].get("headers") or [])][:50]
        rows = [[str(c) for c in (r if isinstance(r, list) else [])][:50]
                for r in (cell["meta"].get("rows") or [])
                if isinstance(r, list)][:500]
        cell["meta"]["headers"] = headers
        cell["meta"]["rows"] = rows
    return cell


def validate(doc):
    """Normalize a document dict; raises ValueError on bad shape."""
    if not isinstance(doc, dict):
        raise ValueError("Document must be an object.")
    cells = doc.get("cells")
    if not isinstance(cells, list):
        raise ValueError("Document must have a cells list.")
    doc["cells"] = [_validate_cell(c) for c in cells[:1000]]
    doc["title"] = str(doc.get("title") or "Untitled notebook")[:255]
    doc["version"] = 1
    return doc


def serialize(doc):
    return json.dumps(doc, ensure_ascii=False, indent=2)


def parse(text):
    try:
        return validate(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corrupt notebook file: {exc}") from exc


def load(stored):
    return parse(file_service.read_text_content(
        stored, max_chars=indexing_service.MAX_TEXT_CHARS))


def extract(stored_file):
    """Flatten a notebook to plain text for the search index."""
    try:
        doc = load(stored_file)
    except (ValueError, OSError):
        return ""
    parts = [doc.get("title", "")]
    for cell in doc.get("cells", []):
        ctype = cell.get("type")
        if ctype in ("markdown", "code"):
            parts.append(cell.get("content", ""))
        elif ctype == "richtext":
            stripped = _TAG_RE.sub(" ", cell.get("content", ""))
            parts.append(" ".join(stripped.split()))
        elif ctype == "todo":
            parts.extend(i["text"] for i in cell["meta"].get("items", []))
        elif ctype == "table":
            meta = cell["meta"]
            parts.append(" | ".join(meta.get("headers", [])))
            parts.extend(" | ".join(r) for r in meta.get("rows", []))
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_notebook(file_id, user_id):
    """Owned, non-trashed notebook StoredFile or None."""
    return (StoredFile.query
            .filter_by(id=file_id, user_id=user_id, extension=EXTENSION)
            .filter(StoredFile.deleted_at.is_(None))
            .first())


def list_notebooks(user_id):
    return (StoredFile.query
            .filter_by(user_id=user_id, extension=EXTENSION)
            .filter(StoredFile.deleted_at.is_(None))
            .order_by(StoredFile.updated_at.desc())
            .all())


def cell_count(stored):
    try:
        return len(load(stored).get("cells", []))
    except (ValueError, OSError):
        return 0


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def _guard_writable(stored):
    if stored.is_synced or (stored.drive and stored.drive.is_synced):
        raise ValueError("Synced drives are read-only.")


def create_notebook(user, drive, name, folder=None, cells=None):
    """Create a notebook file in a (non-synced) drive. Returns StoredFile."""
    _guard_writable_drive(drive)
    title = (name or "").strip() or "Untitled notebook"
    doc = {"version": 1, "title": title,
           "cells": cells if cells else [new_cell()]}
    validate(doc)
    data = serialize(doc).encode("utf-8")
    storage = FileStorage(stream=BytesIO(data),
                          filename=f"{title}.{EXTENSION}",
                          content_type=MIME_TYPE)
    stored = file_service.save_upload(storage, user, folder=folder)
    stored.drive_id = drive.id
    db.session.commit()
    indexing_service.enqueue_index([stored.id])
    return stored


def _guard_writable_drive(drive):
    if drive.is_synced:
        raise ValueError("Synced drives are read-only — notebooks cannot be "
                         "created there.")


def save_document(stored, doc, force_checkpoint=False, note="", source="edit"):
    """Persist a document: throttled version snapshot + blob write + re-index.

    Autosaves write through immediately (no data loss) but only snapshot a
    FileVersion when the latest one is older than SNAPSHOT_INTERVAL, so a
    burst of edits collapses into one history entry. force_checkpoint=True
    always snapshots (Ctrl+S / AI edits). Returns True when a version was
    snapshotted.
    """
    _guard_writable(stored)
    validate(doc)
    latest = stored.versions[0] if stored.versions else None
    snapshotted = False
    last_snapshot_at = latest.created_at if latest else None
    if last_snapshot_at is not None and last_snapshot_at.tzinfo is None:
        # SQLite returns naive datetimes; utcnow() is timezone-aware.
        last_snapshot_at = last_snapshot_at.replace(tzinfo=timezone.utc)
    if (force_checkpoint or last_snapshot_at is None
            or utcnow() - last_snapshot_at > SNAPSHOT_INTERVAL):
        file_service.snapshot_version(stored, source, note=note)
        snapshotted = True

    data = serialize(doc)
    path = file_service.file_path(stored)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data)
    stored.size = os.path.getsize(path)
    stored.checksum = hashlib.sha256(data.encode("utf-8")).hexdigest()
    stored.updated_at = utcnow()
    db.session.commit()
    indexing_service.enqueue_index([stored.id])
    return snapshotted


def add_cell(doc, cell_type="markdown", content="", meta=None, index=None):
    cell = new_cell(cell_type, content, meta)
    if index is None or index >= len(doc["cells"]):
        doc["cells"].append(cell)
    else:
        doc["cells"].insert(max(0, index), cell)
    return cell


def find_cell(doc, cell_id):
    for i, cell in enumerate(doc["cells"]):
        if cell.get("id") == cell_id:
            return i, cell
    return None, None


def update_cell(doc, cell_id, content=None, meta=None):
    _i, cell = find_cell(doc, cell_id)
    if cell is None:
        raise ValueError(f"Cell '{cell_id}' not found.")
    if content is not None:
        cell["content"] = str(content)
    if meta is not None:
        merged = {**cell.get("meta", {}), **meta}
        cell["meta"] = merged
    _validate_cell(cell)
    return cell


def delete_cell(doc, cell_id):
    i, cell = find_cell(doc, cell_id)
    if cell is None:
        raise ValueError(f"Cell '{cell_id}' not found.")
    doc["cells"].pop(i)
    return cell
