"""Synced drives: mirror a local folder into a read-only drive.

A synced drive maps to a real folder on the server's disk. Files are NOT
copied — each StoredFile keeps the absolute `source_path` of the real file
and all reads (preview, indexing, search, AI) go straight to it. The drive
is strictly read-only: nothing in DocIndex may modify, rename, move or
delete the real files.

Syncs run in a background thread with progress tracking and pause/resume
(`start_sync`, `pause_sync`, `resume_sync`, `get_status`); `sync_drive` is
the synchronous core (used in tests and by the worker). Job state lives in
memory — it is progress bookkeeping, not source of truth.

CRITICAL: never call file_service.purge_file/delete_file on synced files —
that would delete the user's real data. Removals only delete DB rows.
"""

import hashlib
import json
import os
import threading
import uuid

from flask import current_app, session

from ..extensions import db
from ..models import Drive, FileIndex, Folder, StoredFile, utcnow
from . import file_service, indexing_service


# --------------------------------------------------------------------------
# Background job tracking
# --------------------------------------------------------------------------

class _SyncCancelled(Exception):
    """Raised inside a sync when the user cancels it."""


class _SyncJob:
    """Progress/state of one sync run (in-memory only)."""

    def __init__(self, drive_id, drive_name):
        self.drive_id = drive_id
        self.drive_name = drive_name
        self.state = "running"      # running | paused | done | cancelled | error
        self.total = 0              # files to scan (counted in a fast pre-pass)
        self.processed = 0          # files already scanned
        self.current = ""           # file being processed right now
        self.stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
        self.error = None
        self.thread = None
        self._resume = threading.Event()
        self._resume.set()          # cleared while paused
        self._cancelled = False

    @property
    def percent(self):
        if not self.total:
            return 0
        return round(self.processed * 100 / self.total)

    def cancel(self):
        self._cancelled = True
        self._resume.set()          # wake a paused worker so it can exit

    def checkpoint(self):
        """Block while paused; abort the sync when cancelled."""
        self._resume.wait()
        if self._cancelled:
            raise _SyncCancelled()

    def as_dict(self):
        return {
            "drive_id": self.drive_id,
            "drive_name": self.drive_name,
            "state": self.state,
            "total": self.total,
            "processed": self.processed,
            "percent": self.percent,
            "current": self.current,
            "stats": self.stats,
            "error": self.error,
        }


_jobs = {}  # drive_id -> _SyncJob
_jobs_lock = threading.Lock()


def get_status(drive_id):
    with _jobs_lock:
        job = _jobs.get(drive_id)
        return job.as_dict() if job else None


def get_active_job(user_id):
    """The user's currently running/paused sync job, if any (for the widget)."""
    with _jobs_lock:
        candidates = [j for j in _jobs.values() if j.state in ("running", "paused")]
    if not candidates:
        return None
    drive_ids = [j.drive_id for j in candidates]
    owned = {d.id for d in Drive.query.filter(Drive.id.in_(drive_ids),
                                              Drive.user_id == user_id)}
    for job in candidates:
        if job.drive_id in owned:
            return job.as_dict()
    return None


def pause_sync(drive_id):
    with _jobs_lock:
        job = _jobs.get(drive_id)
        if job and job.state == "running":
            job.state = "paused"
            job._resume.clear()
            return True
    return False


def resume_sync(drive_id):
    with _jobs_lock:
        job = _jobs.get(drive_id)
        if job and job.state == "paused":
            job.state = "running"
            job._resume.set()
            return True
    return False


def cancel_sync(drive_id):
    """Stop a running/paused sync. The worker exits at the next file."""
    with _jobs_lock:
        job = _jobs.get(drive_id)
        if job and job.state in ("running", "paused"):
            job.cancel()
            return True
    return False


def start_sync(drive, app=None):
    """Start a sync for the drive. Returns the job.

    Runs in a background thread unless SYNC_ASYNC is off (tests), in which
    case it runs inline and the returned job is already finished.
    """
    with _jobs_lock:
        existing = _jobs.get(drive.id)
        if existing and existing.state in ("running", "paused"):
            return existing
        job = _SyncJob(drive.id, drive.name)
        _jobs[drive.id] = job

    app = app or current_app._get_current_object()
    if app.config.get("SYNC_ASYNC", True):
        thread = threading.Thread(target=_sync_worker,
                                  args=(drive.id, job, app), daemon=True)
        job.thread = thread
        thread.start()
    else:
        _sync_worker(drive.id, job, app)
    return job


def _sync_worker(drive_id, job, app):
    with app.app_context():
        try:
            drive = db.session.get(Drive, drive_id)
            if drive is None:
                job.state = "cancelled"
                return
            sync_drive(drive, job=job)
            job.state = "done"
        except _SyncCancelled:
            db.session.rollback()
            job.state = "cancelled"
        except Exception as exc:  # noqa: BLE001 - report to the widget
            db.session.rollback()
            app.logger.exception("Sync failed for drive %s", drive_id)
            job.state = "error"
            job.error = str(exc)[:500]


def remove_synced_drive(drive):
    """Remove a synced drive and ALL its data from DocIndex.

    Stops any running sync first, then deletes the drive row (files, folders,
    index rows and versions cascade) and any thumbnails DocIndex generated.
    The real files on disk are NEVER touched.
    """
    if not drive.is_synced:
        raise ValueError("Not a synced drive.")

    job = None
    with _jobs_lock:
        job = _jobs.get(drive.id)
    if job and job.state in ("running", "paused"):
        job.cancel()
        if job.thread is not None:
            job.thread.join(timeout=10)
    with _jobs_lock:
        _jobs.pop(drive.id, None)

    # Thumbnails are ours (generated under THUMBNAIL_FOLDER) — remove them.
    for stored in StoredFile.query.filter_by(drive_id=drive.id).all():
        thumb = file_service.thumbnail_path(stored)
        if os.path.exists(thumb):
            try:
                os.remove(thumb)
            except OSError:
                current_app.logger.exception("Failed to remove thumbnail %s", thumb)

    db.session.delete(drive)  # cascades: files -> index/versions, folders
    db.session.commit()


# --------------------------------------------------------------------------
# Drive creation
# --------------------------------------------------------------------------

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
    """Create a synced drive for a local folder and start the first sync.

    Returns (drive, job, error).
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
    db.session.commit()
    session["drive_id"] = drive.id
    job = start_sync(drive)
    return drive, job, None


# --------------------------------------------------------------------------
# Sync core
# --------------------------------------------------------------------------

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


def _trigger_indexing(file_ids):
    """Index synced files: one background worker for the whole batch.

    Spawning one thread per file would exhaust the connection pool on large
    folders, so a single thread walks the list sequentially.
    """
    if not file_ids:
        return
    app = current_app._get_current_object()
    if current_app.config.get("INDEX_ASYNC", True):
        thread = threading.Thread(target=_index_batch, args=(file_ids, app),
                                  daemon=True)
        thread.start()
    else:
        _index_batch(file_ids, app)


def _index_batch(file_ids, app):
    for file_id in file_ids:
        indexing_service.index_file(file_id, app)


def _count_files(root):
    """Fast pre-pass: how many files will be scanned (for the % progress)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            if os.path.islink(path):
                continue
            try:
                if _allowed(filename, os.path.getsize(path)):
                    total += 1
            except OSError:
                continue
    return total


def sync_drive(drive, job=None):
    """Re-scan the drive's local folder and reconcile DB rows with disk.

    When a `job` is given, progress is reported into it and pause/checkpoint
    is honored between files. Returns stats: {added, updated, removed, skipped}.
    """
    if not drive.is_synced:
        raise ValueError("Not a synced drive.")
    if not os.path.isdir(drive.source_path):
        raise ValueError(f'Synced folder "{drive.source_path}" no longer exists.')

    stats = job.stats if job else {"added": 0, "updated": 0, "removed": 0,
                                   "skipped": 0}
    root = drive.source_path

    if job:
        job.total = _count_files(root)

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
            if job:
                job.checkpoint()
                job.current = rel

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
            if job:
                job.processed += 1
                # Flush in batches so other requests see fresh rows and the
                # write lock is not held for the whole sync.
                if job.processed % 50 == 0:
                    db.session.commit()

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
    drive.last_sync_stats = json.dumps(stats)
    db.session.commit()

    _trigger_indexing(to_index)
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
