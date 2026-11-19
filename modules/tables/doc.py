"""Tables module document service.

A table is a ``.ptable`` JSON document::

    {"version": 1, "columns": [{"name": ..., "type": ...}],
     "rows": [[cell, ...], ...]}

``type`` is one of ``number|date|category|text``; unknown types coerce to
``text`` on the way in. ``validate`` is strict on shape: every row must be
a list exactly as wide as ``columns`` — ragged rows raise instead of being
padded. Padding short rows (or trimming long ones) is a viewer concern
(T2 import/viewer), so the stored document shape stays unambiguous.
"""

import json

from app.extensions import db
from app.services import file_service

EXTENSION = "ptable"

COLUMN_TYPES = ("number", "date", "category", "text")

# Dedicated folder per drive holding that drive's tables ("separated by
# context"). Created on demand.
TABLES_FOLDER = "DocIndex Tables"


def tables_folder(user, drive):
    """Get-or-create the drive's tables folder. Returns None for synced
    (read-only) drives — nothing may be created in them."""
    from app.models import Folder
    if drive is None or getattr(drive, "is_synced", False):
        return None
    folder = (Folder.query
              .filter_by(user_id=user.id, drive_id=drive.id,
                         parent_id=None, name=TABLES_FOLDER)
              .first())
    if folder is None:
        folder = Folder(user_id=user.id, drive_id=drive.id,
                        parent_id=None, name=TABLES_FOLDER)
        db.session.add(folder)
        db.session.commit()
    return folder


def _coerce_column(col):
    if isinstance(col, str):
        return {"name": col, "type": "text"}
    if isinstance(col, dict):
        ctype = col.get("type", "text")
        return {"name": str(col.get("name", "")),
                "type": ctype if ctype in COLUMN_TYPES else "text"}
    raise ValueError(f"Each column must be a name or {{'name', 'type'}} "
                     f"object, got {col!r}.")


def new_table(columns, rows):
    """Build a table document from column specs and row lists."""
    if not isinstance(columns, list):
        raise ValueError("Columns must be a list.")
    if not isinstance(rows, list):
        raise ValueError("Rows must be a list of lists.")
    cols = [_coerce_column(col) for col in columns]
    width = len(cols)
    clean_rows = []
    for row in rows:
        if not isinstance(row, list):
            raise ValueError("Each row must be a list of cells.")
        if len(row) != width:
            raise ValueError(
                f"Ragged row: got {len(row)} cells for {width} columns.")
        clean_rows.append(list(row))
    return validate({"version": 1, "columns": cols, "rows": clean_rows})


def validate(doc):
    """Normalize a table document dict; raises ValueError on bad shape."""
    if not isinstance(doc, dict):
        raise ValueError("Document must be an object.")
    if doc.get("version", 1) != 1:
        raise ValueError(f"Unsupported table version "
                         f"{doc.get('version')!r}.")
    doc["version"] = 1
    columns = doc.get("columns")
    if not isinstance(columns, list):
        raise ValueError("Document must have a columns list.")
    doc["columns"] = [_coerce_column(col) for col in columns]
    rows = doc.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Document must have a rows list.")
    width = len(doc["columns"])
    clean = []
    for row in rows:
        if not isinstance(row, list):
            raise ValueError("Each row must be a list of cells.")
        # Strict on shape: ragged rows raise here; padding is the
        # viewer/import concern (T2), not the stored-document contract.
        if len(row) != width:
            raise ValueError(
                f"Ragged row: got {len(row)} cells for {width} columns.")
        clean.append(list(row))
    doc["rows"] = clean
    return doc


def serialize(doc):
    return json.dumps(doc, ensure_ascii=False, indent=2)


def load(text):
    try:
        return validate(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corrupt table file: {exc}") from exc


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_table(file_id, user_id):
    """Owned, non-trashed table StoredFile or None."""
    from app.models import StoredFile
    return (StoredFile.query
            .filter_by(id=file_id, user_id=user_id, extension=EXTENSION)
            .filter(StoredFile.deleted_at.is_(None))
            .first())


def list_tables(user_id):
    """Owned, non-trashed .ptable StoredFiles."""
    from app.models import StoredFile
    return (StoredFile.query
            .filter_by(user_id=user_id, extension=EXTENSION)
            .filter(StoredFile.deleted_at.is_(None))
            .order_by(StoredFile.updated_at.desc())
            .all())


# ---------------------------------------------------------------------------
# Organize: every drive's tables into its "DocIndex Tables" folder
# ---------------------------------------------------------------------------

def organize_preview(user):
    """Tables not yet under their drive's tables folder.

    Returns (moves, skipped): moves are
    {file_id, name, from_drive_id, from_folder_id, to_drive_id, to_folder_id}
    dicts; skipped counts tables on synced (read-only) drives, which can
    never move. Tables already inside "DocIndex Tables" — including its
    subfolders — are left alone. Nothing is changed — call
    apply_organize() to execute."""
    moves, skipped = [], 0
    for stored in list_tables(user.id):
        drive = stored.drive
        if drive is None or getattr(drive, "is_synced", False):
            skipped += 1
            continue
        target = tables_folder(user, drive)
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
    TableOrganizePlan for 'Revert last organize'. Returns
    (record_or_None, applied_count, skipped_count)."""
    from app.models import TableOrganizePlan
    moves, skipped = organize_preview(user)
    applied = []
    for move in moves:
        stored = get_table(move["file_id"], user.id)
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
        record = TableOrganizePlan(user_id=user.id, plan=applied,
                                   applied=True)
        db.session.add(record)
        db.session.commit()
    return record, len(applied), skipped


def revert_organize(user):
    """Replay the inverse moves of the latest applied, non-reverted plan.
    Returns (moved_back_count, skipped_count, found)."""
    from app.models import TableOrganizePlan
    record = (TableOrganizePlan.query
              .filter_by(user_id=user.id, applied=True, reverted=False)
              .order_by(TableOrganizePlan.id.desc())
              .first())
    if record is None:
        return 0, 0, False
    moved, skipped = 0, 0
    for move in reversed(record.plan or []):
        stored = get_table(move["file_id"], user.id)
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
    from app.models import TableOrganizePlan
    return (TableOrganizePlan.query
            .filter_by(user_id=user.id, applied=True, reverted=False)
            .order_by(TableOrganizePlan.id.desc())
            .first())
