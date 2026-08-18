"""Synced drives: mirror a local folder into a read-only drive.

A synced drive maps to a real folder on the server's disk. Files are NOT
copied — each StoredFile keeps the absolute `source_path` of the real file
and all reads (preview, indexing, search, AI) go straight to it. The drive
is strictly read-only: nothing in DocIndex may modify, rename, move or
delete the real files. Sync is on-demand (`sync_drive`), triggered by the
user.

CRITICAL: never call file_service.purge_file/delete_file on synced files —
that would delete the user's real data. Removals only delete DB rows.
"""

import hashlib
import os
import uuid

from flask import current_app, session

from ..extensions import db
from ..models import Drive, FileIndex, Folder, StoredFile, utcnow
from . import file_service, indexing_service


def validate_path(path):
    """Normalize and validate a local folder path. Returns (abs_path, error)."""
    path = (path or "").strip().strip('"')
    if not path:
        return None, "A folder path is required."
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return None, f'"{path}" does not exist on this machine.'
    if not os.path.isdir(abs_path):
        return None, f'"{path}" is not a folder.'
    if not os.access(abs_path, os.R_OK):
        return None, f'"{path}" is not readable by the server.'
    return abs_path, None


def create_synced_drive(user, path):
    """Create a synced drive for a local folder and run the first sync.

    Returns (drive, stats, error).
    """
    abs_path, error = validate_path(path)
    if error:
        return None, None, error
    clash = Drive.query.filter_by(user_id=user.id, source_path=abs_path).first()
    if clash:
        return None, None, f'That folder is already synced as drive "{clash.name}".'

    name = os.path.basename(abs_path.rstrip(os.sep)) or abs_path
    base, suffix = name, 2
    while Drive.query.filter(Drive.user_id == user.id,
                             Drive.name.ilike(name)).first():
        name = f"{base} ({suffix})"
        suffix += 1

    drive = Drive(name=name, user_id=user.id, source_path=abs_path,
                  description=f"Synced from {abs_path}")
    db.session.add(drive)
    db.session.flush()
    session["drive_id"] = drive.id
    stats = sync_drive(drive)
    return drive, stats, None


def _sha256_of(path):
    checksum = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _allowed(name, size):
    if not file_service.allowed_file(name):
        return False
    return size <= current_app.config["MAX_FILE_SIZE"]


def _trigger_indexing(file_id):
    if current_app.config.get("INDEX_ASYNC", True):
        indexing_service.index_file_async(file_id, current_app._get_current_object())
    else:
        indexing_service.index_file(file_id)


def sync_drive(drive):
    """Re-scan the drive's local folder and reconcile DB rows with disk.

    Returns stats: {added, updated, removed, skipped}.
    """
    if not drive.is_synced:
        raise ValueError("Not a synced drive.")
    if not os.path.isdir(drive.source_path):
        raise ValueError(f'Synced folder "{drive.source_path}" no longer exists.')

    stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
    root = drive.source_path

    # Existing state, keyed by path relative to the root.
    existing_files = {}
    for f in StoredFile.query.filter_by(drive_id=drive.id).all():
        rel = os.path.relpath(f.source_path, root)
        existing_files[rel] = f
    existing_folders = {}
    for folder in Folder.query.filter_by(drive_id=drive.id).all():
        existing_folders[_folder_rel_path(folder)] = folder

    seen_files, seen_folders = set(), set()
    to_index = []

    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        parent = None
        if rel_dir != ".":
            parent = _ensure_folder_path(drive, rel_dir, existing_folders,
                                         seen_folders)
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            if os.path.islink(path):
                stats["skipped"] += 1
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                stats["skipped"] += 1
                continue
            if not _allowed(filename, size):
                stats["skipped"] += 1
                continue

            rel = os.path.normpath(os.path.join(rel_dir, filename))
            seen_files.add(rel)
            stored = existing_files.get(rel)
            if stored is None:
                stored = StoredFile(
                    name=filename,
                    stored_name=f"{uuid.uuid4().hex}.{file_service.get_extension(filename)}",
                    extension=file_service.get_extension(filename),
                    mime_type=None,
                    size=size,
                    checksum=_sha256_of(path),
                    folder_id=parent.id if parent else None,
                    user_id=drive.user_id,
                    drive_id=drive.id,
                    source_path=os.path.abspath(path),
                )
                db.session.add(stored)
                db.session.flush()
                db.session.add(FileIndex(file_id=stored.id, status="pending"))
                if stored.is_image:
                    file_service._make_thumbnail(path, stored.stored_name)
                existing_files[rel] = stored
                to_index.append(stored.id)
                stats["added"] += 1
            else:
                checksum = _sha256_of(path)
                if checksum != stored.checksum or size != stored.size:
                    stored.checksum = checksum
                    stored.size = size
                    stored.updated_at = utcnow()
                    if stored.index:
                        stored.index.status = "pending"
                    if stored.is_image:
                        file_service._make_thumbnail(path, stored.stored_name)
                    to_index.append(stored.id)
                    stats["updated"] += 1

    # Files that vanished from disk: delete the DB row ONLY (never the blob —
    # the blob is the user's real file, which simply no longer exists here).
    for rel, stored in existing_files.items():
        if rel not in seen_files:
            thumb = file_service.thumbnail_path(stored)
            if os.path.exists(thumb):
                try:
                    os.remove(thumb)
                except OSError:
                    current_app.logger.exception("Failed to remove thumbnail %s", thumb)
            db.session.delete(stored)
            stats["removed"] += 1

    # Folders that vanished from disk (deepest first so children go first).
    for rel in sorted(set(existing_folders) - seen_folders,
                      key=lambda p: p.count(os.sep), reverse=True):
        db.session.delete(existing_folders[rel])

    drive.last_synced_at = utcnow()
    db.session.commit()

    for file_id in to_index:
        _trigger_indexing(file_id)
    return stats


def _folder_rel_path(folder):
    """Path of a Folder relative to the drive root (built from parents)."""
    parts = []
    node = folder
    while node is not None:
        parts.append(node.name)
        node = node.parent
    return os.path.normpath(os.path.join(*reversed(parts)))


def _ensure_folder_path(drive, rel_dir, existing_folders, seen_folders):
    """Return the Folder row for a relative dir path, creating as needed."""
    rel_dir = os.path.normpath(rel_dir)
    folder = existing_folders.get(rel_dir)
    if folder is not None:
        seen_folders.add(rel_dir)
        return folder

    parent = None
    parent_rel = os.path.dirname(rel_dir)
    if parent_rel:
        parent = _ensure_folder_path(drive, parent_rel, existing_folders,
                                     seen_folders)
    folder = Folder(name=os.path.basename(rel_dir),
                    parent_id=parent.id if parent else None,
                    user_id=drive.user_id, drive_id=drive.id)
    db.session.add(folder)
    db.session.flush()
    existing_folders[rel_dir] = folder
    seen_folders.add(rel_dir)
    return folder
