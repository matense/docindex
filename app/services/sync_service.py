"""Synced drives: mirror a local folder into DocIndex.

Two modes (Drive.sync_mode): 'mirror' is strictly read-only. 'shared' is
a writable two-way sync (e.g. a Dropbox/OneDrive folder): local edits
write through to the real files with a version snapshot first, and remote
changes detected on re-scan are adopted with the last-seen content kept
as a version (collapsed history).

CRITICAL: never delete real files. Removals only delete DB rows.
Shared-drive purges are DB-only by construction (see purge_file).
"""

import hashlib
import json
import os
import shutil
import threading
import uuid

from flask import current_app, session

from ..extensions import db
from ..models import Drive, FileIndex, FileVersion, Folder, StoredFile, utcnow
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
        self.ended_at = None        # monotonic() when reaching a terminal state
        self._resume = threading.Event()
        self._resume.set()          # cleared while paused
        self._cancelled = False

    def finish(self, state):
        import time as _time
        self.state = state
        self.ended_at = _time.monotonic()

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
_JOB_TTL = 3600  # evict finished jobs after 1h (memory leak guard)


def _evict_finished():
    import time as _time
    now = _time.monotonic()
    dead = [k for k, j in _jobs.items()
            if j.state in ("done", "cancelled", "error")
            and j.ended_at is not None and now - j.ended_at > _JOB_TTL]
    for k in dead:
        _jobs.pop(k, None)


def get_status(drive_id):
    with _jobs_lock:
        _evict_finished()
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
                job.finish("cancelled")
                return
            sync_drive(drive, job=job)
            job.finish("done")
        except _SyncCancelled:
            db.session.rollback()
            job.finish("cancelled")
        except Exception as exc:  # noqa: BLE001 - report to the widget
            db.session.rollback()
            app.logger.exception("Sync failed for drive %s", drive_id)
            job.finish("error")
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
    # Stream in chunks: a 50k-file drive must not load all rows twice.
    file_ids = []
    q = (StoredFile.query.filter_by(drive_id=drive.id)
         .yield_per(1000))
    for stored in q:
        file_ids.append(stored.id)
        thumb = file_service.thumbnail_path(stored)
        if os.path.exists(thumb):
            try:
                os.remove(thumb)
            except OSError:
                current_app.logger.exception("Failed to remove thumbnail %s", thumb)
        # Detach ORM state periodically on huge drives.
        if len(file_ids) % 1000 == 0:
            db.session.expire_all()

    # Queued index jobs reference these files (FK) — remove them first.
    # Batched: SQLite caps the number of bound variables per statement.
    from ..models import IndexJob
    for i in range(0, len(file_ids), 500):
        chunk = file_ids[i:i + 500]
        IndexJob.query.filter(IndexJob.file_id.in_(chunk)).delete(
            synchronize_session=False)

    db.session.delete(drive)  # cascades: files -> index/versions, folders
    db.session.commit()

    # Drop the FTS rows of the removed files (batched).
    from . import search_service
    search_service.fts_delete_many(file_ids)

    # Shadows are ours (generated under SHADOW_FOLDER) — remove them.
    for fid in file_ids:
        try:
            shadow = os.path.join(current_app.config["SHADOW_FOLDER"],
                                  f"{fid}.blob")
            if os.path.exists(shadow):
                os.remove(shadow)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Drive creation
# --------------------------------------------------------------------------

def validate_path(path):
    """Normalize and validate a local folder path. Returns (abs_path, error)."""
    path = (path or "").strip().strip('"')
    if not path:
        return None, "A folder path is required."
    abs_path = os.path.realpath(os.path.abspath(path))
    if not os.path.exists(abs_path):
        return None, f'"{path}" does not exist on this machine.'
    if not os.path.isdir(abs_path):
        return None, f'"{path}" is not a folder.'
    if not os.access(abs_path, os.R_OK):
        return None, f'"{path}" is not readable by the server.'
    try:
        basedir = os.path.realpath(os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")))
        blocked = {
            os.path.realpath(p) for p in (
                "/etc", "/proc", "/sys", "/root", "/boot",
                os.path.join(basedir, "instance"),
                os.path.join(basedir, "uploads"),
                os.path.join(basedir, "venv"),
                os.path.join(basedir, ".git"),
            ) if os.path.exists(p)
        }
        for win in (os.environ.get("SystemRoot", r"C:\Windows"),
                    os.environ.get("ProgramFiles", r"C:\Program Files")):
            if win and os.path.isdir(win):
                blocked.add(os.path.realpath(win))
    except Exception:
        blocked = set()
    if abs_path in blocked:
        return None, "That system folder cannot be synced."
    for b in blocked:
        if abs_path.startswith(b + os.sep):
            return None, "That system folder cannot be synced."
    allowed = os.environ.get("SYNC_ALLOWED_ROOTS", "")
    roots = [os.path.realpath(os.path.abspath(r.strip()))
             for r in allowed.split(";") if r.strip()]
    if roots and not any(abs_path == r or abs_path.startswith(r + os.sep)
                         for r in roots):
        return None, ("That folder is outside the allowed sync roots. "
                      "Ask the administrator to set SYNC_ALLOWED_ROOTS.")
    return abs_path, None


def create_synced_drive(user, path, captions_enabled=True, index_workers=1,
                          sync_mode="mirror"):
    """Create a synced drive for a local folder and start the first sync.

    sync_mode is 'mirror' (read-only) or 'shared' (writable two-way sync).
    Returns (drive, job, error).
    """
    abs_path, error = validate_path(path)
    if error:
        return None, None, error
    if sync_mode not in ("mirror", "shared"):
        return None, None, "Unknown sync mode."
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
                  description=f"Synced from {abs_path}",
                  captions_enabled=captions_enabled,
                  sync_mode=sync_mode,
                  index_workers=max(1, min(int(index_workers or 1), 8)))
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


def _trigger_indexing(file_ids, workers=1):
    """Index synced files through the persistent queue (index_jobs).

    Jobs survive restarts and are retried; `workers` drainers run in the
    background (the sync UI toggle). Tests (INDEX_ASYNC=False) process
    inline, sequentially.
    """
    if not file_ids:
        return
    app = current_app._get_current_object()
    workers = max(1, min(int(workers or 1), 8))
    if current_app.config.get("INDEX_ASYNC", True):
        indexing_service.enqueue_index(file_ids)
        indexing_service.ensure_workers(workers, app)
    else:
        # Tests (in-memory SQLite): same thread, sequential.
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


def _adopt_remote_change(stored, checksum, size):
    """Shared drive: the real file changed outside DocIndex (another user).

    Keeps the last-seen content as a FileVersion (collapsed history: the
    initial state the user saw + the current one), then adopts the remote
    content and refreshes the shadow. Returns True when a version was kept.
    """
    versioned = False
    shadow = file_service.shadow_path(stored)
    if os.path.exists(shadow):
        ext = stored.extension or ""
        sname = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
        versions_dir = current_app.config["VERSIONS_FOLDER"]
        os.makedirs(versions_dir, exist_ok=True)
        shutil.copyfile(shadow, os.path.join(versions_dir, sname))
        next_version = ((stored.versions[0].version + 1)
                        if stored.versions else 1)
        db.session.add(FileVersion(
            file_id=stored.id, version=next_version, stored_name=sname,
            size=stored.size, checksum=stored.checksum, source="sync",
            note="External change detected during sync"))
        versioned = True
    stored.checksum = checksum
    stored.size = size
    stored.updated_at = utcnow()
    if stored.index:
        stored.index.status = "pending"
    file_service.refresh_shadow(stored)
    return versioned


def sync_drive(drive, job=None):
    """Re-scan the drive's local folder and reconcile DB rows with disk.

    Two phases: the scan does every slow operation (hashing, thumbnails)
    WITHOUT any pending DB writes, then the changes are applied in short
    write-only transactions. The SQLite write lock is never held across
    file I/O, so concurrent writers (index workers, requests) never hit
    "database is locked" because of a sync.

    When a `job` is given, progress is reported into it and pause/checkpoint
    is honored between files. Returns stats: {added, updated, removed, skipped}.
    """
    if not drive.is_synced:
        raise ValueError("Not a synced drive.")
    if not os.path.isdir(drive.source_path):
        raise ValueError(f'Synced folder "{drive.source_path}" no longer exists.')

    stats = job.stats if job else {"added": 0, "updated": 0, "removed": 0,
                                   "skipped": 0}
    stats.setdefault("versions", 0)
    root = drive.source_path

    if job:
        job.total = _count_files(root)

    # Existing state, keyed by path relative to the root (read-only).
    # Trashed rows and ignored paths are invisible to the scan: a trashed
    # file whose real file is still on disk must not be re-added, and
    # purged shared files must not resurrect.
    ignored = file_service._ignored_sync_paths(drive)
    existing_files = {}
    trashed_paths = set()
    for f in StoredFile.query.filter_by(drive_id=drive.id).all():
        if not f.source_path:
            continue
        rel = os.path.normpath(os.path.relpath(f.source_path, root))
        if f.deleted_at is not None:
            trashed_paths.add(rel)
            continue
        existing_files[rel] = f
    existing_folders = {}
    for folder in Folder.query.filter_by(drive_id=drive.id).all():
        existing_folders[_folder_rel_path(folder)] = folder

    # -- Phase 1: scan the disk. Nothing is written to the DB here, so the
    # slow parts (sha256 of every file, thumbnail generation) run without
    # holding the write lock.
    seen_files, needed_dirs = set(), set()
    adds, updates, thumbnails = [], [], []

    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            needed_dirs.add(rel_dir)
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

            if rel in ignored or rel in trashed_paths:
                if job:
                    job.processed += 1
                continue
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
                    user_id=drive.user_id,
                    drive_id=drive.id,
                    source_path=os.path.abspath(path),
                )
                adds.append((rel, rel_dir, stored))
                if stored.is_image:
                    thumbnails.append((path, stored.stored_name))
                stats["added"] += 1
            else:
                # Size-first: same size + known checksum = assume unchanged
                # (avoids re-hashing 10k files every sync; content changes
                # with identical size are rare and caught on next size change
                # or reindex).
                if size == stored.size and stored.checksum:
                    if job:
                        job.processed += 1
                    continue
                checksum = _sha256_of(path)
                if checksum != stored.checksum or size != stored.size:
                    updates.append((stored, checksum, size))
                    if stored.is_image:
                        thumbnails.append((path, stored.stored_name))
                    stats["updated"] += 1
            if job:
                job.processed += 1

    for path, stored_name in thumbnails:
        file_service._make_thumbnail(path, stored_name)

    # -- Phase 2: apply the changes. Only fast DB statements run between a
    # batch's first write and its commit, so the write lock is held for
    # milliseconds at a time. Folders first (parents before children).
    seen_folders = set()
    for rel_dir in sorted(needed_dirs, key=lambda p: (p.count(os.sep), p)):
        _ensure_folder_path(drive, rel_dir, existing_folders, seen_folders)
    db.session.commit()

    # Flush in batches: other requests see fresh rows and indexing
    # (persistent queue) starts while the apply phase is still running —
    # the queue card in the profile shows live progress.
    to_index = []
    pending = 0

    def flush_batch(force=False):
        nonlocal pending
        if pending and (force or pending >= 50):
            db.session.commit()
            _trigger_indexing(to_index, workers=drive.index_workers)
            to_index.clear()
            pending = 0

    for rel, rel_dir, stored in adds:
        parent = existing_folders.get(rel_dir)
        stored.folder_id = parent.id if parent else None
        db.session.add(stored)
        db.session.flush()
        db.session.add(FileIndex(file_id=stored.id, status="pending"))
        existing_files[rel] = stored
        to_index.append(stored.id)
        pending += 1
        flush_batch()
        if drive.is_shared:
            file_service.refresh_shadow(stored)
    for stored, checksum, size in updates:
        if drive.is_shared:
            if _adopt_remote_change(stored, checksum, size):
                stats["versions"] += 1
        else:
            stored.checksum = checksum
            stored.size = size
            stored.updated_at = utcnow()
            if stored.index:
                stored.index.status = "pending"
        to_index.append(stored.id)
        pending += 1
        flush_batch()
    flush_batch(force=True)

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
            file_service.drop_shadow(stored)
            db.session.delete(stored)
            stats["removed"] += 1

    # Folders that vanished from disk (deepest first so children go first).
    for rel in sorted(set(existing_folders) - seen_folders,
                      key=lambda p: p.count(os.sep), reverse=True):
        db.session.delete(existing_folders[rel])

    drive.last_synced_at = utcnow()
    drive.last_sync_stats = json.dumps(stats)
    db.session.commit()
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
