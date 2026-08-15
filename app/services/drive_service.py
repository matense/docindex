"""Multi-drive support: users keep files in separate named drives.

The current drive is stored in the Flask session. The first time a user
touches the drive system (or after the drives table is added to an existing
database), a "Personal" drive is created and any pre-existing files and
folders are moved into it.
"""

from flask import session

from ..extensions import db
from ..models import Drive, Folder, StoredFile

DEFAULT_DRIVE_NAME = "Personal"


def list_drives(user):
    return Drive.query.filter_by(user_id=user.id).order_by(Drive.name).all()


def _create_drive(user, name):
    drive = Drive(name=name.strip(), user_id=user.id)
    db.session.add(drive)
    db.session.flush()
    return drive


def get_current_drive(user):
    """Current drive for the user, creating/migrating lazily when needed."""
    drive = None
    drive_id = session.get("drive_id")
    if drive_id:
        drive = Drive.query.filter_by(id=drive_id, user_id=user.id).first()

    if drive is None:
        drive = Drive.query.filter_by(user_id=user.id).order_by(Drive.id).first()

    if drive is None:
        drive = _create_drive(user, DEFAULT_DRIVE_NAME)

    # Files/folders that predate the drives feature belong to the first drive.
    orphaned_files = StoredFile.query.filter_by(user_id=user.id, drive_id=None)
    if orphaned_files.count():
        orphaned_files.update({StoredFile.drive_id: drive.id})
    orphaned_folders = Folder.query.filter_by(user_id=user.id, drive_id=None)
    if orphaned_folders.count():
        orphaned_folders.update({Folder.drive_id: drive.id})

    session["drive_id"] = drive.id
    db.session.commit()
    return drive


def create_drive(user, name):
    """Create a new drive and make it current. Returns (drive, error)."""
    name = (name or "").strip()
    if not name:
        return None, "Drive name cannot be empty."
    exists = Drive.query.filter(Drive.user_id == user.id,
                                Drive.name.ilike(name)).first()
    if exists:
        return None, f'A drive named "{name}" already exists.'
    drive = _create_drive(user, name)
    session["drive_id"] = drive.id
    db.session.commit()
    return drive, None


def select_drive(user, drive_id):
    """Switch the current drive. Returns the drive or None if not owned."""
    drive = Drive.query.filter_by(id=drive_id, user_id=user.id).first()
    if drive:
        session["drive_id"] = drive.id
    return drive


def update_drive(user, drive_id, name, description):
    """Rename a drive and/or set its description. Returns (drive, error)."""
    drive = Drive.query.filter_by(id=drive_id, user_id=user.id).first()
    if not drive:
        return None, "Drive not found."
    name = (name or "").strip()
    if not name:
        return None, "Drive name cannot be empty."
    clash = (Drive.query
             .filter(Drive.user_id == user.id, Drive.id != drive.id,
                     Drive.name.ilike(name))
             .first())
    if clash:
        return None, f'A drive named "{name}" already exists.'
    drive.name = name
    drive.description = (description or "").strip()[:500]
    db.session.commit()
    return drive, None
