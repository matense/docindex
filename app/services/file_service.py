import contextlib
import hashlib
import json
import os
import shutil
import time
import uuid

from flask import current_app
from PIL import Image
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import Drive, FileIndex, FileVersion, Folder, StoredFile

THUMBNAIL_SIZE = (256, 256)


def allowed_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in current_app.config["ALLOWED_EXTENSIONS"]


def get_extension(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def file_path(stored_file):
    # Synced files map to a real file on disk; everything else lives in the
    # uploads folder. Defense-in-depth: contain stored_name (no ../ escape).
    if stored_file.source_path:
        return stored_file.source_path
    safe = (stored_file.stored_name or "").replace("\\", "/")
    safe = safe.split("/")[-1]
    return os.path.join(current_app.config["UPLOAD_FOLDER"], safe)


def _guard_not_synced(stored_file):
    """Synced files are real files on disk; their blobs are never deleted."""
    if stored_file.is_synced:
        raise ValueError("Synced files are read-only.")


def is_readonly_file(stored_file):
    """True when the file lives on a read-only (mirror) synced drive.

    Shared (writable) sync drives allow edits, which write through to the
    real file with a version snapshot first."""
    if not stored_file.is_synced:
        return False
    drive = stored_file.drive
    return drive is None or not drive.is_shared


def _guard_readonly(stored_file):
    if is_readonly_file(stored_file):
        raise ValueError(f"File '{stored_file.name}' is read-only.")


def thumbnail_path(stored_file):
    return os.path.join(current_app.config["THUMBNAIL_FOLDER"],
                        os.path.splitext(stored_file.stored_name)[0] + ".png")


def has_thumbnail(stored_file):
    return stored_file.is_image and os.path.exists(thumbnail_path(stored_file))


def _make_thumbnail(path, stored_name):
    thumb = os.path.join(current_app.config["THUMBNAIL_FOLDER"],
                         os.path.splitext(stored_name)[0] + ".png")
    try:
        # Pillow bomb guard: refuse absurd pixel counts before decoding.
        Image.MAX_IMAGE_PIXELS = 50_000_000
        with Image.open(path) as img:
            img.thumbnail(THUMBNAIL_SIZE)
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            img.save(thumb, "PNG")
        return True
    except Image.UnidentifiedImageError:
        # Not actually an image (e.g. renamed extension) — expected case,
        # not an error: just skip the thumbnail quietly.
        current_app.logger.warning("Skipping thumbnail (not an image): %s",
                                   stored_name)
        return False
    except Exception:
        current_app.logger.exception("Failed to create thumbnail for %s", stored_name)
        return False


def save_upload(file_storage, user, folder=None, drive=None):
    """Persist an uploaded file and create its DB rows. Returns StoredFile.

    When the target is a shared (writable sync) drive, bytes go straight
    into the real folder tree and source_path is set; otherwise the
    managed uploads folder is used as before."""
    target_drive = drive
    if folder is not None and folder.drive_id is not None:
        target_drive = Drive.query.get(folder.drive_id)
    if target_drive is not None and target_drive.is_shared:
        return _save_upload_to_shared(file_storage, user, target_drive,
                                      folder)
    # Pre-check the declared size before touching disk (disk-fill DoS).
    try:
        declared = file_storage.content_length
    except Exception:
        declared = None
    if declared is not None and declared > current_app.config["MAX_FILE_SIZE"]:
        raise ValueError(f"File exceeds the {current_app.config['MAX_FILE_SIZE'] // (1024 * 1024)} MB limit.")
    original_name = secure_filename(file_storage.filename) or "unnamed"
    ext = get_extension(original_name)
    stored_name = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name)

    file_storage.save(path)

    size = os.path.getsize(path)
    if size > current_app.config["MAX_FILE_SIZE"]:
        os.remove(path)
        raise ValueError(f"File exceeds the {current_app.config['MAX_FILE_SIZE'] // (1024 * 1024)} MB limit.")

    checksum = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            checksum.update(chunk)

    stored = StoredFile(
        name=original_name,
        stored_name=stored_name,
        extension=ext,
        mime_type=file_storage.mimetype,
        size=size,
        checksum=checksum.hexdigest(),
        folder_id=folder.id if folder else None,
        user_id=user.id,
    )
    db.session.add(stored)
    db.session.flush()

    db.session.add(FileIndex(file_id=stored.id, status="pending"))
    db.session.commit()

    if stored.is_image:
        _make_thumbnail(path, stored_name)

    return stored


def _save_upload_to_shared(file_storage, user, drive, folder):
    """Upload bytes directly into a shared drive's real folder tree."""
    original_name = secure_filename(file_storage.filename) or "unnamed"
    ext = get_extension(original_name)
    try:
        declared = file_storage.content_length
    except Exception:
        declared = None
    if declared is not None and declared > current_app.config["MAX_FILE_SIZE"]:
        raise ValueError(f"File exceeds the {current_app.config['MAX_FILE_SIZE'] // (1024 * 1024)} MB limit.")
    path = _disk_destination(drive, folder, original_name)
    file_storage.save(path)
    size = os.path.getsize(path)
    if size > current_app.config["MAX_FILE_SIZE"]:
        os.remove(path)
        raise ValueError(f"File exceeds the {current_app.config['MAX_FILE_SIZE'] // (1024 * 1024)} MB limit.")
    stored = StoredFile(
        name=os.path.basename(path),
        stored_name=f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex,
        extension=ext,
        mime_type=file_storage.mimetype,
        size=size,
        checksum=_sha256_of(path),
        folder_id=folder.id if folder else None,
        user_id=user.id,
        drive_id=drive.id,
        source_path=os.path.abspath(path),
    )
    db.session.add(stored)
    db.session.flush()
    db.session.add(FileIndex(file_id=stored.id, status="pending"))
    _unignore_sync_path(drive, _drive_relpath(drive, stored.source_path))
    db.session.commit()
    if stored.is_image:
        _make_thumbnail(path, stored.stored_name)
    refresh_shadow(stored)
    return stored


def _purge_shared_row(stored_file, drive):
    """DB-only purge for shared-drive files (real blob is never touched)."""
    from ..models import FileEmbedding, IndexJob
    file_id = stored_file.id
    for version in stored_file.versions:
        try:
            path = version_path(version)
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            current_app.logger.exception("Failed to remove version blob %s",
                                         version.stored_name)
    try:
        thumb = thumbnail_path(stored_file)
        if os.path.exists(thumb):
            os.remove(thumb)
    except OSError:
        current_app.logger.exception("Failed to remove thumbnail")
    IndexJob.query.filter_by(file_id=file_id).delete()
    FileEmbedding.query.filter_by(file_id=file_id).delete()
    rel = _drive_relpath(drive, stored_file.source_path)
    if rel:
        _ignore_sync_path(drive, rel)
    drop_shadow(stored_file)
    db.session.delete(stored_file)
    db.session.flush()
    from . import search_service
    search_service.fts_delete(file_id, commit=False)
    db.session.commit()


def _drive_relpath(drive, path):
    """Drive-relative normpath, or None when outside the drive root."""
    try:
        rel = os.path.normpath(os.path.relpath(path, drive.source_path))
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return rel


def _ignored_sync_paths(drive):
    try:
        paths = json.loads(drive.sync_ignored or "[]")
        return set(paths) if isinstance(paths, list) else set()
    except (ValueError, TypeError):
        return set()


def _ignore_sync_path(drive, rel):
    paths = _ignored_sync_paths(drive)
    if rel not in paths:
        paths.add(rel)
        drive.sync_ignored = json.dumps(sorted(paths))
        db.session.flush()


def _unignore_sync_path(drive, rel):
    paths = _ignored_sync_paths(drive)
    if rel in paths:
        paths.discard(rel)
        drive.sync_ignored = json.dumps(sorted(paths))
        db.session.flush()


def _lock_path(file_id):
    return os.path.join(current_app.config["SHADOW_FOLDER"], "locks",
                        f"{file_id}.lock")


def _lock_is_stale(path, steal_after):
    try:
        with open(path, encoding="utf-8") as fh:
            stamp = float((fh.read().strip().split() or ["0"])[0])
    except (OSError, ValueError):
        return True
    return (time.time() - stamp) > steal_after


@contextlib.contextmanager
def file_lock(file_id, timeout=30, steal_after=120):
    """Cross-process per-file mutex (editor save vs sync adopt).

    Blocking: waits up to timeout seconds, then raises TimeoutError.
    Stale locks (crashed holder) are stolen after steal_after seconds.
    """
    directory = os.path.join(current_app.config["SHADOW_FOLDER"], "locks")
    os.makedirs(directory, exist_ok=True)
    path = _lock_path(file_id)
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{time.time()} {os.getpid()}".encode())
            break
        except FileExistsError:
            if _lock_is_stale(path, steal_after):
                try:
                    os.remove(path)
                except OSError:
                    pass
            elif time.time() >= deadline:
                raise TimeoutError(f"File {file_id} is locked.") from None
            time.sleep(0.05)
    try:
        yield True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass


@contextlib.contextmanager
def try_file_lock(file_id, steal_after=120):
    """Non-blocking file_lock: yields True (held) or False (busy)."""
    directory = os.path.join(current_app.config["SHADOW_FOLDER"], "locks")
    os.makedirs(directory, exist_ok=True)
    path = _lock_path(file_id)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _lock_is_stale(path, steal_after):
            try:
                os.remove(path)
            except OSError:
                pass
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                yield False
                return
        else:
            yield False
            return
    os.write(fd, f"{time.time()} {os.getpid()}".encode())
    try:
        yield True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass


def _drive_condition(drive_id):
    """Match files in the same drive (NULL-safe for legacy drive-less rows)."""
    if drive_id is None:
        return StoredFile.drive_id.is_(None)
    return StoredFile.drive_id == drive_id


def shadow_path(stored_file):
    """Last-seen content copy for shared-drive files (outside the sync root)."""
    return os.path.join(current_app.config["SHADOW_FOLDER"],
                       f"{stored_file.id}.blob")


def refresh_shadow(stored_file):
    """Record the current content as last-seen. Skips files over the cap."""
    try:
        cap = current_app.config.get("SHARED_SHADOW_MAX_BYTES",
                                     10 * 1024 * 1024)
        path = file_path(stored_file)
        if (not os.path.exists(path)
                or os.path.getsize(path) > cap):
            drop_shadow(stored_file)
            return False
        os.makedirs(current_app.config["SHADOW_FOLDER"], exist_ok=True)
        shutil.copyfile(path, shadow_path(stored_file))
        return True
    except OSError:
        current_app.logger.exception("Could not refresh shadow for %s",
                                     stored_file.id)
        return False


def drop_shadow(stored_file):
    try:
        path = shadow_path(stored_file)
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def shadow_checksum(stored_file):
    """Checksum of the last-seen copy, or None when there is none."""
    try:
        path = shadow_path(stored_file)
        if not os.path.exists(path):
            return None
        return _sha256_of(path)
    except OSError:
        return None


def _disk_destination(drive, folder, name):
    """Unique real path inside a shared drive's folder tree (contained)."""
    parts = [f.name for f in folder.breadcrumb()] if folder else []
    directory = (os.path.join(drive.source_path, *parts)
                 if parts else drive.source_path)
    os.makedirs(directory, exist_ok=True)
    root = os.path.abspath(drive.source_path)
    candidate = os.path.join(directory, name)
    if not os.path.abspath(candidate).startswith(root + os.sep):
        raise ValueError("Invalid file name.")
    base, dot, ext = name.rpartition(".")
    if not dot:
        base, ext = name, ""
    n = 1
    while os.path.exists(candidate):
        n += 1
        candidate = os.path.join(
            directory, f"{base} (copy {n})" + (dot + ext if dot else ""))
        if not os.path.abspath(candidate).startswith(root + os.sep):
            raise ValueError("Invalid file name.")
    return candidate


def _move_bytes_for_drive(stored, drive, folder, name):
    """Relocate a file's bytes for a drive change.

    Returns (source_path_or_None, stored_name, size). No-op when neither
    side is a shared drive. Raises FileNotFoundError when shared bytes
    are involved but the blob is missing."""
    src = file_path(stored)
    target_shared = drive is not None and drive.is_shared
    source_shared = stored.source_path is not None
    if not target_shared and not source_shared:
        return stored.source_path, stored.stored_name, stored.size
    if not os.path.exists(src):
        raise FileNotFoundError(f"Original file missing on disk: {src}")
    if target_shared:
        dst = _disk_destination(drive, folder, name)
        shutil.move(src, dst)
        return dst, stored.stored_name, os.path.getsize(dst)
    ext = stored.extension or ""
    sname = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
    dst = os.path.join(current_app.config["UPLOAD_FOLDER"], sname)
    shutil.move(src, dst)
    return None, sname, os.path.getsize(dst)


def find_duplicates(stored_file):
    """Other files in the SAME drive with identical content (same SHA-256).

    Duplicate detection is per-drive: different drives may hold the same
    file without being flagged.
    """
    if not stored_file.checksum:
        return []
    return (StoredFile.query
            .filter(StoredFile.user_id == stored_file.user_id,
                    _drive_condition(stored_file.drive_id),
                    StoredFile.checksum == stored_file.checksum,
                    StoredFile.id != stored_file.id,
                    StoredFile.deleted_at.is_(None))
            .order_by(StoredFile.created_at)
            .all())


def duplicate_checksums(user_id, drive_id):
    """Checksums that exist on more than one file within the same drive."""
    from sqlalchemy import func

    rows = (db.session.query(StoredFile.checksum)
            .filter(StoredFile.user_id == user_id,
                    _drive_condition(drive_id),
                    StoredFile.checksum.isnot(None),
                    StoredFile.deleted_at.is_(None))
            .group_by(StoredFile.checksum)
            .having(func.count() > 1)
            .all())
    return {row[0] for row in rows}


def delete_file(stored_file):
    """Soft-delete: move the file to the trash (blob and history are kept)."""
    _guard_readonly(stored_file)
    from ..models import utcnow
    stored_file.deleted_at = utcnow()
    db.session.flush()
    from . import search_service
    search_service.fts_delete(stored_file.id, commit=False)  # trashed files are not searchable
    db.session.commit()


def restore_file(stored_file):
    """Bring a trashed file back to its drive."""
    _guard_readonly(stored_file)
    stored_file.deleted_at = None
    db.session.flush()
    from . import search_service
    # file_index still holds the extracted text — just re-add the FTS row.
    search_service.fts_upsert(stored_file.id, commit=False)
    db.session.commit()


def unique_name(user_id, drive_id, folder_id, name, exclude_id=None):
    """Non-colliding file name within a drive folder (adds ' (copy N)')."""
    base, dot, ext = name.rpartition(".")
    if not dot:
        base, ext = name, ""
    candidate, n = name, 1
    while True:
        q = StoredFile.query.filter_by(
            user_id=user_id, drive_id=drive_id, folder_id=folder_id,
            deleted_at=None, name=candidate)
        if exclude_id is not None:
            q = q.filter(StoredFile.id != exclude_id)
        if q.first() is None:
            return candidate
        n += 1
        candidate = f"{base} (copy {n})" + (dot + ext if dot else "")


def unique_folder_name(user_id, drive_id, parent_id, name, exclude_id=None):
    """Non-colliding folder name within a drive folder."""
    candidate, n = name, 1
    while True:
        q = Folder.query.filter_by(
            user_id=user_id, drive_id=drive_id, parent_id=parent_id,
            name=candidate)
        if exclude_id is not None:
            q = q.filter(Folder.id != exclude_id)
        if q.first() is None:
            return candidate
        n += 1
        candidate = f"{name} (copy {n})"


def _writable_drive(user, drive_id, allow_none=False):
    """Return (drive_or_None, error). None means root (always writable)."""
    if drive_id in (None, ""):
        return None, None
    try:
        drive_id = int(drive_id)
    except (TypeError, ValueError):
        return None, "Invalid drive."
    drive = Drive.query.filter_by(id=drive_id, user_id=user.id).first()
    if drive is None:
        return None, "Drive not found."
    if getattr(drive, "is_readonly", False):
        return None, f"Drive '{drive.name}' is read-only."
    return drive, None


def _owned_folder(user, drive, folder_id):
    if folder_id in (None, ""):
        return None, None
    folder = Folder.query.filter_by(id=folder_id, user_id=user.id).first()
    if folder is None:
        return None, "Folder not found."
    if drive is not None and folder.drive_id != drive.id:
        return None, "Folder is not in that drive."
    if folder.drive is not None and getattr(folder.drive, "is_readonly",
                                                  False):
        return None, f"Folder '{folder.name}' is read-only."
    return folder, None


def _resolve_dest(user, drive_id, folder_id):
    """Resolve (drive_or_None, folder_or_None, error).

    drive_id None means the root; when a folder is given without a drive,
    the folder's own drive becomes the destination.
    """
    drive, error = _writable_drive(user, drive_id)
    if error:
        return None, None, error
    if drive is None and folder_id not in (None, ""):
        probe = Folder.query.filter_by(id=folder_id,
                                       user_id=user.id).first()
        if probe is None:
            return None, None, "Folder not found."
        if probe.drive_id is not None:
            drive, error = _writable_drive(user, probe.drive_id)
            if error:
                return None, None, error
    folder, error = _owned_folder(user, drive, folder_id)
    if error:
        return None, None, error
    return drive, folder, None


def move_file_to_drive(user, file_id, drive_id, folder_id=None):
    """Move one file to another drive/folder. Returns (stored, error)."""
    stored = (StoredFile.query
              .filter_by(id=file_id, user_id=user.id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if not stored:
        return None, "File not found."
    if is_readonly_file(stored):
        return None, f"File '{stored.name}' is read-only."
    drive, folder, error = _resolve_dest(user, drive_id, folder_id)
    if error:
        return None, error
    new_name = unique_name(user.id, drive.id if drive else None,
                           folder.id if folder else None, stored.name,
                           exclude_id=stored.id)
    try:
        source_path, stored_name, size = _move_bytes_for_drive(
            stored, drive, folder, new_name)
    except (OSError, ValueError) as exc:
        return None, f"Could not move file: {exc}"
    stored.source_path = source_path
    stored.stored_name = stored_name
    stored.size = size
    stored.drive_id = drive.id if drive else None
    stored.folder_id = folder.id if folder else None
    stored.name = new_name
    db.session.commit()
    refresh_shadow(stored)
    if stored.is_image and not has_thumbnail(stored):
        _make_thumbnail(file_path(stored), stored.stored_name)
    return stored, None


def copy_file_to_drive(user, file_id, drive_id, folder_id=None):
    """Copy one file's blob to another drive/folder. Returns (copy, error)."""
    stored = (StoredFile.query
              .filter_by(id=file_id, user_id=user.id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if not stored:
        return None, "File not found."
    drive, folder, error = _resolve_dest(user, drive_id, folder_id)
    if error:
        return None, error
    src = file_path(stored)
    if not os.path.exists(src):
        return None, "Original file missing on disk."
    ext = stored.extension or ""
    target_shared = drive is not None and drive.is_shared
    try:
        if target_shared:
            name = unique_name(user.id, drive.id,
                               folder.id if folder else None, stored.name)
            dst = _disk_destination(drive, folder, name)
            shutil.copyfile(src, dst)
            source_path = dst
            size = os.path.getsize(dst)
        else:
            sname = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
            dst = os.path.join(current_app.config["UPLOAD_FOLDER"], sname)
            shutil.copyfile(src, dst)
            source_path = None
            size = os.path.getsize(dst)
    except (OSError, ValueError) as exc:
        return None, f"Could not copy file: {exc}"
    copy = StoredFile(
        name=unique_name(user.id, drive.id if drive else None,
                         folder.id if folder else None, stored.name),
        stored_name=sname if not target_shared else (
            f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex),
        extension=stored.extension,
        mime_type=stored.mime_type, size=size,
        checksum=stored.checksum,
        folder_id=folder.id if folder else None,
        user_id=user.id, drive_id=drive.id if drive else None,
        source_path=source_path)
    db.session.add(copy)
    db.session.flush()
    db.session.add(FileIndex(file_id=copy.id, status="pending"))
    db.session.commit()
    try:
        from . import indexing_service
        indexing_service.enqueue_index([copy.id])
    except Exception:  # noqa: BLE001 - index later
        current_app.logger.exception("Could not enqueue index for copy")
    refresh_shadow(copy)
    if copy.is_image and not has_thumbnail(copy):
        _make_thumbnail(file_path(copy), copy.stored_name)
    return copy, None


def move_folder(user, folder_id, drive_id, parent_id=None):
    """Move a folder (with contents) to another drive/folder."""
    from ..models import Folder as _Folder
    folder = _Folder.query.filter_by(id=folder_id, user_id=user.id).first()
    if not folder:
        return None, "Folder not found."
    src_drive = Drive.query.filter_by(id=folder.drive_id,
                                      user_id=user.id).first()
    if src_drive is not None and getattr(src_drive, "is_readonly", False):
        return None, f"Folder '{folder.name}' is read-only."
    drive, parent, error = _resolve_dest(user, drive_id, parent_id)
    if error:
        return None, error
    if parent and (parent.id == folder.id or _is_descendant(parent, folder)):
        return None, "Cannot move a folder into itself or a subfolder."
    folder.name = unique_folder_name(
        user.id, drive.id if drive else None,
        parent.id if parent else None,
        folder.name, exclude_id=folder.id)
    _apply_drive_recursive(folder, drive.id if drive else None,
                           parent.id if parent else None)
    db.session.commit()
    return folder, None


def copy_folder(user, folder_id, drive_id, parent_id=None):
    """Deep-copy a folder and contents to another drive/folder."""
    from ..models import Folder as _Folder
    folder = _Folder.query.filter_by(id=folder_id, user_id=user.id).first()
    if not folder:
        return None, "Folder not found."
    drive, parent, error = _resolve_dest(user, drive_id, parent_id)
    if error:
        return None, error
    new_folder, new_ids = _copy_folder_recursive(
        user, folder, drive.id if drive else None,
        parent.id if parent else None)
    db.session.commit()
    try:
        from . import indexing_service
        indexing_service.enqueue_index(new_ids)
    except Exception:  # noqa: BLE001 - index later
        current_app.logger.exception("Could not enqueue index for copy")
    return new_folder, None


def delete_folder_tree(user, folder_id):
    """Trash a folder's files (restorable) and remove the folder rows.

    Returns (trashed_file_count, error). Refuses read-only drives.
    """
    from ..models import Folder as _Folder
    folder = _Folder.query.filter_by(id=folder_id, user_id=user.id).first()
    if not folder:
        return 0, "Folder not found."
    if folder.drive is not None and getattr(folder.drive, "is_readonly",
                                            False):
        return 0, f"Folder '{folder.name}' is read-only."
    trashed = _trash_folder_contents(folder)
    db.session.delete(folder)
    db.session.commit()
    return trashed, None


def _trash_folder_contents(folder):
    trashed = 0
    for child in list(folder.children or []):
        trashed += _trash_folder_contents(child)
        db.session.delete(child)
    for stored in list(folder.files or []):
        if stored.deleted_at is None:
            # Detach first: folders cascade delete-orphan to their files.
            stored.folder_id = None
            delete_file(stored)
            trashed += 1
    db.session.flush()
    return trashed


def _is_descendant(folder, ancestor):
    node = folder.parent
    seen = set()
    while node is not None and node.id not in seen:
        if node.id == ancestor.id:
            return True
        seen.add(node.id)
        node = node.parent
    return False


def _apply_drive_recursive(folder, drive_id, parent_id):
    folder.drive_id = drive_id
    folder.parent_id = parent_id
    db.session.flush()
    drive = Drive.query.get(drive_id) if drive_id else None
    for sub in list(folder.children or []):
        _apply_drive_recursive(sub, drive_id, folder.id)
    for stored in list(folder.files or []):
        stored.drive_id = drive_id
        stored.folder_id = folder.id
        if drive is not None and drive.is_shared or stored.source_path:
            try:
                source_path, stored_name, size = _move_bytes_for_drive(
                    stored, drive, folder, stored.name)
            except (OSError, ValueError) as exc:
                raise ValueError(f"Could not move '{stored.name}': {exc}") from exc
            stored.source_path = source_path
            stored.stored_name = stored_name
            stored.size = size
            refresh_shadow(stored)
    db.session.flush()


def _copy_folder_recursive(user, folder, drive_id, parent_id):
    from ..models import Folder as _Folder
    new_folder = _Folder(
        name=unique_folder_name(user.id, drive_id, parent_id, folder.name),
        user_id=user.id, drive_id=drive_id, parent_id=parent_id)
    db.session.add(new_folder)
    db.session.flush()
    new_ids = []
    for sub in list(folder.children or []):
        _, sub_ids = _copy_folder_recursive(user, sub, drive_id,
                                            new_folder.id)
        new_ids.extend(sub_ids)
    for stored in list(folder.files or []):
        copy, error = copy_file_to_drive(
            user, stored.id, drive_id, new_folder.id)
        if copy is not None:
            new_ids.append(copy.id)
    return new_folder, new_ids


def purge_file(stored_file):
    """Permanently delete: DB rows (index/versions cascade) and disk blobs.

    Shared-drive files are DB-only removals — the real blob is never
    touched — and their path is added to the drive's sync ignore list so
    the next sync does not resurrect them."""
    drive = stored_file.drive
    if stored_file.is_synced and drive is not None and drive.is_shared:
        _purge_shared_row(stored_file, drive)
        return
    _guard_not_synced(stored_file)
    file_id = stored_file.id
    for version in stored_file.versions:
        try:
            path = version_path(version)
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            current_app.logger.exception("Failed to remove version blob %s",
                                         version.stored_name)
    for path in (file_path(stored_file), thumbnail_path(stored_file)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            current_app.logger.exception("Failed to remove %s", path)
    # Any queued index jobs for the file disappear with it (FK safety).
    from ..models import IndexJob
    IndexJob.query.filter_by(file_id=file_id).delete()
    # Semantic vectors have no cascade (plain FK) — remove them explicitly.
    from ..models import FileEmbedding
    FileEmbedding.query.filter_by(file_id=file_id).delete()
    db.session.delete(stored_file)
    db.session.flush()
    from . import search_service
    search_service.fts_delete(file_id, commit=False)
    db.session.commit()


def trashed_files(user_id):
    """Files currently in the user's trash, most recently deleted first."""
    return (StoredFile.query
            .filter(StoredFile.user_id == user_id,
                    StoredFile.deleted_at.isnot(None))
            .order_by(StoredFile.deleted_at.desc())
            .all())


def version_path(version):
    return os.path.join(current_app.config["VERSIONS_FOLDER"], version.stored_name)


def _sha256_of(path):
    checksum = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def snapshot_version(stored_file, source, note=""):
    """Snapshot the file's current content as a FileVersion before overwriting."""
    _guard_readonly(stored_file)
    ext = stored_file.extension
    stored_name = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
    shutil.copyfile(file_path(stored_file), os.path.join(
        current_app.config["VERSIONS_FOLDER"], stored_name))
    next_version = (stored_file.versions[0].version + 1) if stored_file.versions else 1
    version = FileVersion(
        file_id=stored_file.id,
        version=next_version,
        stored_name=stored_name,
        size=stored_file.size,
        checksum=stored_file.checksum,
        source=source,
        note=note,
    )
    db.session.add(version)
    db.session.commit()
    return version


def find_by_name(user_id, drive_id, folder_id, name):
    """Existing active (non-trashed) file with this name in the same folder."""
    return (StoredFile.query
            .filter_by(user_id=user_id, drive_id=drive_id,
                       folder_id=folder_id, name=name)
            .filter(StoredFile.deleted_at.is_(None))
            .first())


def replace_with_upload(stored_file, file_storage):
    """Replace a file's content with a new upload (same name, same folder).

    Returns "replaced" (a version was snapshotted) or "identical" (same
    content — no-op, no version churn).
    """
    _guard_readonly(stored_file)
    ext = stored_file.extension
    tmp_name = f"tmp_{uuid.uuid4().hex}.{ext}" if ext else f"tmp_{uuid.uuid4().hex}"
    tmp_path = os.path.join(current_app.config["UPLOAD_FOLDER"], tmp_name)
    file_storage.save(tmp_path)

    size = os.path.getsize(tmp_path)
    if size > current_app.config["MAX_FILE_SIZE"]:
        os.remove(tmp_path)
        raise ValueError(f"File exceeds the {current_app.config['MAX_FILE_SIZE'] // (1024 * 1024)} MB limit.")

    new_checksum = _sha256_of(tmp_path)
    if new_checksum == stored_file.checksum:
        os.remove(tmp_path)
        return "identical"

    snapshot_version(stored_file, "upload")
    with file_lock(stored_file.id):
        os.replace(tmp_path, file_path(stored_file))
    stored_file.size = size
    stored_file.checksum = new_checksum
    stored_file.mime_type = file_storage.mimetype
    if stored_file.is_image:
        _make_thumbnail(file_path(stored_file), stored_file.stored_name)
    db.session.commit()
    refresh_shadow(stored_file)
    return "replaced"


def restore_version(stored_file, version):
    """Roll the file back to a past version (current state is snapshotted)."""
    snapshot_version(stored_file, "restore",
                     note=f"Before restore to v{version.version}")
    with file_lock(stored_file.id):
        shutil.copyfile(version_path(version), file_path(stored_file))
    stored_file.size = version.size
    stored_file.checksum = version.checksum
    if stored_file.is_image:
        _make_thumbnail(file_path(stored_file), stored_file.stored_name)
    db.session.commit()
    refresh_shadow(stored_file)
    refresh_shadow(stored_file)


def update_file_content(stored_file, content, source="edit", note=""):
    """Overwrite an editable text file's content, keeping a version snapshot."""
    content = content or ""
    if len(content.encode("utf-8")) > current_app.config["MAX_FILE_SIZE"]:
        raise ValueError(f"File exceeds the {current_app.config['MAX_FILE_SIZE'] // (1024 * 1024)} MB limit.")
    snapshot_version(stored_file, source, note=note)
    path = file_path(stored_file)
    with file_lock(stored_file.id):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    stored_file.size = os.path.getsize(path)
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    stored_file.checksum = checksum
    db.session.commit()
    refresh_shadow(stored_file)


def read_text_content(stored_file, max_chars=200_000):
    path = file_path(stored_file)
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read(max_chars)


_TABLE_EXTS = {"csv", "xlsx", "xlsm", "xls"}
_TEXT_EXTS = {"txt", "md"}
_SAMPLE_ROWS = 8
_SAMPLE_CHARS = 4000


def describe_for_chat(stored_file):
    """Compact machine-readable summary of a file for chat context/summary
    cards: caption for images, columns + head rows for tables, head + counts
    for text. Never raises — returns {"kind": ...} with whatever is known."""
    ext = (stored_file.extension or "").lower()
    index = FileIndex.query.filter_by(file_id=stored_file.id).first()
    indexed = bool(index and index.status == "ok")
    out = {"kind": "other", "indexed": indexed, "caption": None,
           "columns": [], "rows": [], "head": None,
           "counts": {"words": None, "lines": None, "chars": None},
           "truncated": False}
    try:
        if stored_file.is_image:
            out["kind"] = "image"
            if index and index.caption:
                out["caption"] = index.caption[:2000]
            return out
        if ext in _TABLE_EXTS:
            out["kind"] = "table"
            columns, rows = _table_head(stored_file)
            if columns is None:
                return out
            out["columns"] = columns
            out["rows"] = rows
            return out
        if ext == "wboard":
            out["kind"] = "whiteboard"
            try:
                scene = json.loads(read_text_content(
                    stored_file, max_chars=_SAMPLE_CHARS + 1))
                elements = scene.get("elements", []) \
                    if isinstance(scene, dict) else []
                title = scene.get("title", "") \
                    if isinstance(scene, dict) else ""
            except (OSError, ValueError):
                return out
            texts = [str(e.get("text", "")) for e in elements
                     if isinstance(e, dict) and e.get("text")]
            digest = (f"{title} — {len(elements)} element(s)".strip(" —"))
            if texts:
                digest += ": " + " | ".join(t[:120] for t in texts[:12])
            out["head"] = digest[:2000]
            out["counts"] = {"words": len(digest.split()),
                             "lines": digest.count("\n") + 1 if digest else 0,
                             "chars": len(digest)}
            return out
        if ext in _TEXT_EXTS:
            out["kind"] = "text"
            try:
                text = read_text_content(stored_file, max_chars=_SAMPLE_CHARS + 1)
            except OSError:
                return out
            if len(text) > _SAMPLE_CHARS:
                text = text[:_SAMPLE_CHARS]
                out["truncated"] = True
            out["head"] = text
            out["counts"] = {"words": len(text.split()),
                             "lines": text.count("\n") + 1 if text else 0,
                             "chars": len(text)}
            return out
    except Exception:  # noqa: BLE001 - summary is best-effort
        pass
    return out


def _table_head(stored_file, max_rows=_SAMPLE_ROWS):
    """(columns, rows) head of a tabular file — (None, None) on failure."""
    ext = (stored_file.extension or "").lower()
    path = file_path(stored_file)
    if ext == "csv":
        import csv
        with open(path, encoding="utf-8", errors="replace",
                   newline="") as fh:
            reader = csv.reader(fh)
            rows = []
            for row in reader:
                rows.append([str(c)[:200] for c in row][:20])
                if len(rows) > max_rows:
                    break
        if not rows:
            return [], []
        return rows[0], rows[1:max_rows + 1]
    if ext in ("xlsx", "xlsm", "xls"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.active
            rows = []
            for row in ws.iter_rows(values_only=True):
                rows.append([(str(c)[:200] if c is not None else "")
                             for c in list(row)[:20]])
                if len(rows) > max_rows:
                    break
        finally:
            wb.close()
        if not rows:
            return [], []
        return rows[0], rows[1:max_rows + 1]
    return None, None
