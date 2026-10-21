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
- heading:   content = heading text, meta.level = 1|2 (legacy title/subtitle
             cells are migrated to heading on load).
- separator: visual divider cell, no content.

Because notebooks are StoredFiles they get ownership, drives/folders, trash,
FileVersion history, FTS search and the AI agent's core tools for free.
"""

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, timedelta
from io import BytesIO

from flask import g, has_app_context
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import StoredFile, utcnow
from app.services import file_service, indexing_service

EXTENSION = "pdocnb"
MIME_TYPE = "application/json"

# Cap for history entries embedded in the file itself (mode B).
MAX_EMBEDDED_HISTORY = 50

# Dedicated folder per drive holding that drive's notebooks ("separated by
# context"). Created on demand; the drive UI renders it with a book icon.
NOTEBOOKS_FOLDER = "DocIndex Notebooks"


def notebooks_folder(user, drive):
    """Get-or-create the drive's notebooks folder. Returns None for synced
    (read-only) drives — nothing may be created in them."""
    from app.models import Drive, Folder
    if drive is None or getattr(drive, "is_synced", False):
        return None
    folder = (Folder.query
              .filter_by(user_id=user.id, drive_id=drive.id,
                         parent_id=None, name=NOTEBOOKS_FOLDER)
              .first())
    if folder is None:
        folder = Folder(user_id=user.id, drive_id=drive.id,
                        parent_id=None, name=NOTEBOOKS_FOLDER)
        db.session.add(folder)
        db.session.commit()
    return folder
SNAPSHOT_INTERVAL = timedelta(minutes=2)
CELL_TYPES = ("markdown", "richtext", "code", "todo", "table",
              "heading", "separator")

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
    # Legacy cell types from the first notebooks release.
    if cell.get("type") in ("title", "subtitle"):
        legacy_level = 1 if cell["type"] == "title" else 2
        cell["type"] = "heading"
        if not isinstance(cell.get("meta"), dict):
            cell["meta"] = {}
        cell["meta"]["level"] = legacy_level
    if cell.get("type") not in CELL_TYPES:
        raise ValueError(f"Unknown cell type '{cell.get('type')}'.")
    cell["content"] = str(cell.get("content") or "")
    meta = cell.get("meta")
    cell["meta"] = meta if isinstance(meta, dict) else {}
    if cell["type"] == "heading":
        cell["meta"]["level"] = 2 if cell["meta"].get("level") == 2 else 1
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
    # AI access flags: ai_lock blocks AI edits, ai_hidden blocks all AI
    # access (read, search and listing) to this notebook.
    doc["ai_lock"] = bool(doc.get("ai_lock"))
    doc["ai_hidden"] = bool(doc.get("ai_hidden"))
    # Full-history mode (step 3/4 of shared sync): once on, always on —
    # enforced in save_document, never here (validate has no stored row).
    if "full_history" in doc:
        doc["full_history"] = bool(doc["full_history"])
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


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(stored_file):
    """Flatten a notebook to plain text for the search index."""
    try:
        doc = load(stored_file)
    except (ValueError, OSError):
        return ""
    parts = [doc.get("title", "")]
    for cell in doc.get("cells", []):
        ctype = cell.get("type")
        if ctype == "separator":
            continue
        if ctype in ("markdown", "code", "heading"):
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
    stored = (StoredFile.query
            .filter_by(id=file_id, user_id=user_id, extension=EXTENSION)
            .filter(StoredFile.deleted_at.is_(None))
            .first())
    if stored is not None:
        ensure_uuid(stored)
    return stored


def list_notebooks(user_id):
    notebooks = (StoredFile.query
            .filter_by(user_id=user_id, extension=EXTENSION)
            .filter(StoredFile.deleted_at.is_(None))
            .order_by(StoredFile.updated_at.desc())
            .all())
    for stored in notebooks:
        ensure_uuid(stored, commit=False)
    if notebooks:
        db.session.commit()
    return notebooks


def ensure_uuid(stored, commit=True):
    """Backfill the internal global identifier (pre-UUID notebooks only)."""
    if not stored.uuid:
        stored.uuid = str(uuid.uuid4())
        if commit:
            db.session.commit()
    return stored.uuid


def cell_count(stored):
    try:
        return len(load(stored).get("cells", []))
    except (ValueError, OSError):
        return 0


def ai_error(stored, write=False):
    """Error message when the AI may not touch this notebook, else None.

    ai_hidden blocks every access (the notebook is invisible to the AI);
    ai_lock blocks only mutations. Corrupt/unreadable notebooks are treated
    as accessible — the normal read errors apply.
    """
    try:
        doc = load(stored)
    except (ValueError, OSError):
        return None
    if doc.get("ai_hidden"):
        return ("This notebook is hidden from the AI by the user — do not "
                "read or mention its content. Tell the user they can unhide "
                "it with the eye button in the notebook toolbar.")
    if write and doc.get("ai_lock"):
        return ("This notebook is LOCKED against AI edits by the user. Do "
                "not generate or propose the content in chat — just tell "
                "the user the notebook is locked and ask them to unlock it "
                "with the lock button in the notebook toolbar, then repeat "
                "the request.")
    return None


def access_flags(stored):
    """(ai_lock, ai_hidden) booleans, for AI tool listings. Unreadable
    notebooks report no flags."""
    try:
        document = load(stored)
    except (ValueError, OSError):
        return False, False
    return bool(document.get("ai_lock")), bool(document.get("ai_hidden"))


def ai_file_guard(_user, stored):
    """Core guard hook (registered with agent_service): hides hidden
    notebooks from every core AI tool (read, grep, search, list)."""
    if stored.extension != EXTENSION:
        return None
    return ai_error(stored)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def _guard_writable(stored):
    from app.services.file_service import is_readonly_file
    if is_readonly_file(stored):
        raise ValueError("Synced drives are read-only.")


def create_notebook(user, drive, name, folder=None, cells=None):
    """Create a notebook file in a writable drive. Returns StoredFile."""
    # Defensive: agent multi-drive context is a list — callers should resolve
    # to one writable drive first (see tools._tool_create), but never crash
    # with "'list' object has no attribute 'is_synced'".
    if isinstance(drive, (list, tuple)):
        candidates = list(drive)
        drive = next((d for d in candidates
                      if not getattr(d, "is_readonly", False)), None)
        if drive is None:
            raise ValueError("All drives in context are synced (read-only) — "
                             "pick a writable drive.")
    _guard_writable_drive(drive)
    title = (name or "").strip() or "Untitled notebook"
    doc = {"version": 1, "title": title,
           "cells": cells if cells else [new_cell()]}
    validate(doc)
    data = serialize(doc).encode("utf-8")
    storage = FileStorage(stream=BytesIO(data),
                          filename=f"{title}.{EXTENSION}",
                          content_type=MIME_TYPE)
    stored = file_service.save_upload(storage, user, folder=folder,
                                      drive=drive)
    stored.drive_id = drive.id
    stored.uuid = ensure_uuid(stored, commit=False)
    db.session.commit()
    indexing_service.enqueue_index([stored.id])
    return stored


def _guard_writable_drive(drive):
    # Defensive against list scope leaking in (multi-drive chat context).
    if isinstance(drive, (list, tuple)):
        raise ValueError("A single target drive is required — pick one with "
                         "drive_id.")
    if drive.is_readonly:
        raise ValueError("Synced drives are read-only — notebooks cannot be "
                         "created there.")


# ---------------------------------------------------------------------------
# Organize: every drive's notebooks into its "DocIndex Notebooks" folder
# ---------------------------------------------------------------------------

def organize_preview(user):
    """Notebooks not yet under their drive's notebooks folder.

    Returns (moves, skipped): moves are
    {file_id, name, from_drive_id, from_folder_id, to_drive_id, to_folder_id}
    dicts; skipped counts notebooks on synced (read-only) drives, which can
    never move. Notebooks already inside "DocIndex Notebooks" — including
    its subfolders — are left alone. Nothing is changed — call
    apply_organize() to execute."""
    moves, skipped = [], 0
    for stored in list_notebooks(user.id):
        drive = stored.drive
        if drive is None or getattr(drive, "is_synced", False):
            skipped += 1
            continue
        target = notebooks_folder(user, drive)
        if stored.folder_id is not None and _is_within(
                stored.folder, target.id):
            continue
        moves.append({
                "file_id": stored.id, "name": stored.name,
                "from_drive_id": drive.id,
                "from_drive_name": drive.name,
                "from_folder_id": stored.folder_id,
                "from_folder_name": (stored.folder.name
                                     if stored.folder else None),
                "to_drive_id": drive.id,
                "to_drive_name": drive.name,
                "to_folder_id": target.id,
                "to_folder_name": target.name,
            })
    return moves, skipped


def _is_within(folder, target_id):
    """True when folder is target or nested under it (cycle-safe)."""
    node, seen = folder, set()
    while node is not None and node.id not in seen:
        if node.id == target_id:
            return True
        seen.add(node.id)
        node = node.parent
    return False


def apply_organize(user):
    """Execute the current organize preview through the normal move path
    (version history is preserved) and persist the applied moves as a
    NotebookOrganizePlan for 'Revert last organize'. Returns
    (record_or_None, applied_count, skipped_count)."""
    from app.models import NotebookOrganizePlan
    moves, skipped = organize_preview(user)
    applied = []
    for move in moves:
        stored = get_notebook(move["file_id"], user.id)
        if stored is None:
            skipped += 1
            continue
        moved, error = file_service.move_file_to_drive(
            user, stored.id, move["to_drive_id"], move["to_folder_id"])
        if error:
            skipped += 1
            continue
        applied.append({
            "file_id": moved.id, "name": moved.name,
            "from_drive_id": move["from_drive_id"],
            "from_folder_id": move["from_folder_id"],
            "to_drive_id": moved.drive_id,
            "to_folder_id": moved.folder_id,
        })
    record = None
    if applied:
        record = NotebookOrganizePlan(user_id=user.id, plan=applied,
                                      applied=True)
        db.session.add(record)
        db.session.commit()
    return record, len(applied), skipped


def revert_organize(user):
    """Replay the inverse moves of the latest applied, non-reverted plan.
    Returns (moved_back_count, skipped_count, found)."""
    from app.models import NotebookOrganizePlan
    record = (NotebookOrganizePlan.query
              .filter_by(user_id=user.id, applied=True, reverted=False)
              .order_by(NotebookOrganizePlan.id.desc())
              .first())
    if record is None:
        return 0, 0, False
    moved, skipped = 0, 0
    for move in reversed(record.plan or []):
        stored = get_notebook(move["file_id"], user.id)
        if stored is None:
            skipped += 1
            continue
        _, error = file_service.move_file_to_drive(
            user, stored.id, move["from_drive_id"], move["from_folder_id"])
        if error:
            skipped += 1
        else:
            moved += 1
    record.reverted = True
    db.session.commit()
    return moved, skipped, True


def last_organize_plan(user):
    """Latest applied, non-reverted plan (what 'Revert' would undo)."""
    from app.models import NotebookOrganizePlan
    return (NotebookOrganizePlan.query
            .filter_by(user_id=user.id, applied=True, reverted=False)
            .order_by(NotebookOrganizePlan.id.desc())
            .first())


def save_document(stored, doc, force_checkpoint=False, note="", source="edit",
                  skip_snapshot=False):
    """Persist a document: throttled version snapshot + blob write + re-index.

    Autosaves write through immediately (no data loss) but only snapshot a
    FileVersion when the latest one is older than SNAPSHOT_INTERVAL, so a
    burst of edits collapses into one history entry. force_checkpoint=True
    always snapshots (Ctrl+S); skip_snapshot=True never does (used by AI
    transactional saves after the first mutation of a run). Returns True
    when a version was snapshotted.
    """
    _guard_writable(stored)
    validate(doc)
    # Full-history mode is sticky: once enabled it can never be turned
    # off again (the toggle confirms this with the user).
    if not doc.get("full_history"):
        try:
            if load(stored).get("full_history"):
                doc["full_history"] = True
        except (ValueError, OSError):
            pass
    latest = stored.versions[0] if stored.versions else None
    snapshotted = False
    snap = None
    last_snapshot_at = latest.created_at if latest else None
    if last_snapshot_at is not None and last_snapshot_at.tzinfo is None:
        # SQLite returns naive datetimes; utcnow() is timezone-aware.
        last_snapshot_at = last_snapshot_at.replace(tzinfo=UTC)
    if (not skip_snapshot
            and (force_checkpoint or last_snapshot_at is None
                 or utcnow() - last_snapshot_at > SNAPSHOT_INTERVAL)):
        snap = file_service.snapshot_version(stored, source, note=note)
        snapshotted = True

    if snapshotted and doc.get("full_history"):
        # Embed the pre-save state in the file itself (without nested
        # history — otherwise the file would grow exponentially) so every
        # collaborator's sync reconstructs the full history.
        try:
            prev = load(stored)
        except (ValueError, OSError):
            prev = None
        if prev is not None:
            prev = {k: v for k, v in prev.items()
                    if k != "synced_history"}
            entries = [e for e in (doc.get("synced_history") or [])
                       if isinstance(e, dict)]
            entries.append({
                "version": snap.version if snap else None,
                "created_at": utcnow().isoformat(),
                "source": snap.source if snap else source,
                "note": note,
                "checksum": stored.checksum,
                "doc": prev,
            })
            doc["synced_history"] = entries[-MAX_EMBEDDED_HISTORY:]

    data = serialize(doc)
    path = file_service.file_path(stored)
    with file_service.file_lock(stored.id):
        # Atomic write (tmp + replace): a crash or concurrent reader never
        # sees a half-written notebook. Binary mode: no newline translation,
        # so the verification hash matches exactly.
        tmp_path = f"{path}.tmp-{uuid.uuid4().hex}"
        with open(tmp_path, "wb") as fh:
            fh.write(data.encode("utf-8"))
        if hashlib.sha256(data.encode("utf-8")).hexdigest() != _sha256_file(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise ValueError("Notebook write failed verification.")
        os.replace(tmp_path, path)
    stored.size = os.path.getsize(path)
    stored.checksum = hashlib.sha256(data.encode("utf-8")).hexdigest()
    stored.updated_at = utcnow()
    db.session.commit()
    indexing_service.enqueue_index([stored.id])
    file_service.refresh_shadow(stored)
    return snapshotted


def save_document_ai(stored, doc, note="AI edits"):
    """Save from an AI agent run, transactionally: the first mutation of a
    notebook within the current request snapshots the pre-edit state, and
    every later mutation in the same run just writes through. One AI answer
    therefore leaves a single restorable history block instead of one
    version per tool call."""
    first_in_run = True
    if has_app_context():
        # g is request/app-context scoped: one chat message = one run.
        snapped = getattr(g, "_nb_ai_snapshotted", None)
        if snapped is None:
            snapped = set()
            g._nb_ai_snapshotted = snapped
        first_in_run = stored.id not in snapped
        snapped.add(stored.id)
    return save_document(stored, doc, force_checkpoint=True,
                         skip_snapshot=not first_in_run,
                         note=note, source="ai")


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
