"""Multi-drive support: users keep files in separate named drives.

The current drive is stored in the Flask session. The first time a user
touches the drive system (or after the drives table is added to an existing
database), a "Personal" drive is created and any pre-existing files and
folders are moved into it.
"""

from flask import session

from ..extensions import db
from ..models import Drive, Folder, StoredFile, utcnow

DEFAULT_DRIVE_NAME = "Personal"

# Drive source providers (module-pluggable). The core only knows local
# folders ("folder" kind, no provider needed); modules such as gitdrives
# register their kind here. A provider implements:
#   validate(user, config)          -> error str | None
#   materialize(drive, on_progress) -> error str | None  (initial fetch)
#   pre_sync(drive, on_progress)    -> error str | None  (refresh + progress)
#   cleanup(drive)                  -> None (best-effort local cleanup)
# on_progress(percent|None, text) feeds the sync widget.
_DRIVE_SOURCES = {}


def register_drive_source(kind, provider):
    """Register a module-provided drive source (e.g. gitdrives -> 'git')."""
    _DRIVE_SOURCES[kind] = provider


def get_drive_source(kind):
    """The provider for a source kind, or None for local folders."""
    if not kind or kind == "folder":
        return None
    return _DRIVE_SOURCES.get(kind)


def list_drives(user):
    """Non-trashed drives (trash lives separately)."""
    return (Drive.query.filter_by(user_id=user.id)
            .filter(Drive.deleted_at.is_(None))
            .order_by(Drive.name).all())


def trashed_drives(user):
    """Soft-deleted drives, most recently deleted first."""
    return (Drive.query.filter_by(user_id=user.id)
            .filter(Drive.deleted_at.isnot(None))
            .order_by(Drive.deleted_at.desc()).all())


def _create_drive(user, name):
    drive = Drive(name=name.strip(), user_id=user.id)
    db.session.add(drive)
    db.session.flush()
    return drive


def get_current_drive(user):
    """Current drive for the user, creating/migrating lazily when needed.

    Trashed drives are never returned: a session pointing at one falls
    back to the oldest live drive (or a fresh Personal drive)."""
    drive = None
    drive_id = session.get("drive_id")
    if drive_id:
        drive = (Drive.query.filter_by(id=drive_id, user_id=user.id)
                 .filter(Drive.deleted_at.is_(None)).first())

    if drive is None:
        drive = (Drive.query.filter_by(user_id=user.id)
                 .filter(Drive.deleted_at.is_(None))
                 .order_by(Drive.id).first())

    if drive is None:
        drive = _create_drive(user, DEFAULT_DRIVE_NAME)
        # Only commit when we actually created/migrated something — every
        # page view calls this via inject_globals, and a write transaction
        # per page churns SQLite WAL + cookies.
        orphaned_files = (StoredFile.query
                          .filter_by(user_id=user.id, drive_id=None)
                          .filter(StoredFile.deleted_at.is_(None)))
        if orphaned_files.count():
            orphaned_files.update({StoredFile.drive_id: drive.id},
                                  synchronize_session=False)
        orphaned_folders = Folder.query.filter_by(user_id=user.id, drive_id=None)
        if orphaned_folders.count():
            orphaned_folders.update({Folder.drive_id: drive.id},
                                    synchronize_session=False)
        if session.get("drive_id") != drive.id:
            session["drive_id"] = drive.id
        db.session.commit()
        return drive

    # Migrate pre-drives orphans only when they exist (two cheap COUNTs, no
    # write when clean).
    changed = False
    if (StoredFile.query.filter_by(user_id=user.id, drive_id=None)
            .filter(StoredFile.deleted_at.is_(None)).count()):
        (StoredFile.query.filter_by(user_id=user.id, drive_id=None)
         .filter(StoredFile.deleted_at.is_(None))
         .update({StoredFile.drive_id: drive.id}, synchronize_session=False))
        changed = True
    if Folder.query.filter_by(user_id=user.id, drive_id=None).count():
        (Folder.query.filter_by(user_id=user.id, drive_id=None)
         .update({Folder.drive_id: drive.id}, synchronize_session=False))
        changed = True
    if session.get("drive_id") != drive.id:
        session["drive_id"] = drive.id
    if changed:
        db.session.commit()
    return drive


def create_drive(user, name):
    """Create a new drive and make it current. Returns (drive, error)."""
    name = (name or "").strip()
    if not name:
        return None, "Drive name cannot be empty."
    exists = Drive.query.filter(Drive.user_id == user.id,
                                Drive.deleted_at.is_(None),
                                Drive.name.ilike(name)).first()
    if exists:
        return None, f'A drive named "{name}" already exists.'
    drive = _create_drive(user, name)
    session["drive_id"] = drive.id
    db.session.commit()
    return drive, None


def select_drive(user, drive_id):
    """Switch the current drive. Returns the drive or None if not owned
    (trashed drives cannot be selected)."""
    drive = (Drive.query.filter_by(id=drive_id, user_id=user.id)
             .filter(Drive.deleted_at.is_(None)).first())
    if drive:
        session["drive_id"] = drive.id
    return drive


def update_drive(user, drive_id, name, description):
    """Rename a drive and/or set its description. Returns (drive, error)."""
    drive = (Drive.query.filter_by(id=drive_id, user_id=user.id)
             .filter(Drive.deleted_at.is_(None)).first())
    if not drive:
        return None, "Drive not found."
    name = (name or "").strip()
    if not name:
        return None, "Drive name cannot be empty."
    clash = (Drive.query
             .filter(Drive.user_id == user.id, Drive.id != drive.id,
                     Drive.deleted_at.is_(None),
                     Drive.name.ilike(name))
             .first())
    if clash:
        return None, f'A drive named "{name}" already exists.'
    drive.name = name
    drive.description = (description or "").strip()[:500]
    db.session.commit()
    return drive, None


def trash_drive(user, drive_id):
    """Soft-delete a drive (restorable from the trash) together with every
    file in it — trashed files keep their blobs and history. Files share the
    drive's timestamp so restoring the drive restores exactly those files
    (anything already in the trash beforehand stays there). Mirror synced
    drives skip the trash and are removed immediately instead — the real
    files are never touched. Shared (writable sync) drives trash normally
    (rows only).
    Returns (drive, error)."""
    from . import file_service, sync_service
    drive = Drive.query.filter_by(id=drive_id, user_id=user.id).first()
    if not drive or drive.deleted_at is not None:
        return None, "Drive not found."
    if drive.is_readonly:
        sync_service.remove_synced_drive(drive)
        return drive, None
    now = utcnow()
    drive.deleted_at = now
    db.session.flush()
    for stored in (StoredFile.query
                   .filter_by(drive_id=drive.id, user_id=user.id)
                   .filter(StoredFile.deleted_at.is_(None)).all()):
        file_service.delete_file(stored)  # own timestamp + FTS cleanup
        stored.deleted_at = now  # same stamp as the drive (joint restore)
    db.session.commit()
    return drive, None


def restore_drive(user, drive_id):
    """Undo a soft-delete, including the files trashed with the drive.
    Returns the drive or None."""
    from . import file_service
    drive = (Drive.query.filter_by(id=drive_id, user_id=user.id)
             .filter(Drive.deleted_at.isnot(None)).first())
    if drive is None:
        return None
    trashed_at = drive.deleted_at
    drive.deleted_at = None
    db.session.flush()
    for stored in (StoredFile.query
                   .filter_by(drive_id=drive.id, user_id=user.id,
                              deleted_at=trashed_at).all()):
        file_service.restore_file(stored)
    db.session.commit()
    return drive


def purge_drive(user, drive_id):
    """Permanently delete a trashed drive and everything in it (files with
    blobs, versions, index + embedding rows, FTS entries, folders). Returns
    True when something was removed."""
    from . import file_service
    drive = (Drive.query.filter_by(id=drive_id, user_id=user.id)
             .filter(Drive.deleted_at.isnot(None)).first())
    if drive is None:
        return False
    for stored in StoredFile.query.filter_by(drive_id=drive.id).all():
        if stored.deleted_at is None:
            stored.deleted_at = utcnow()
        file_service.purge_file(stored)
    for folder in Folder.query.filter_by(drive_id=drive.id).all():
        db.session.delete(folder)
    db.session.delete(drive)
    db.session.commit()
    return True
