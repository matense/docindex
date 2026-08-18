import hashlib
import os
import shutil
import uuid

from flask import current_app
from PIL import Image
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import FileIndex, FileVersion, StoredFile

THUMBNAIL_SIZE = (256, 256)


def allowed_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in current_app.config["ALLOWED_EXTENSIONS"]


def get_extension(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def file_path(stored_file):
    # Synced files map to a real file on disk; everything else lives in the
    # uploads folder.
    if stored_file.source_path:
        return stored_file.source_path
    return os.path.join(current_app.config["UPLOAD_FOLDER"], stored_file.stored_name)


def _guard_not_synced(stored_file):
    """Synced files are real files on disk and must never be modified."""
    if stored_file.is_synced:
        raise ValueError("Synced files are read-only.")


def thumbnail_path(stored_file):
    return os.path.join(current_app.config["THUMBNAIL_FOLDER"],
                        os.path.splitext(stored_file.stored_name)[0] + ".png")


def has_thumbnail(stored_file):
    return stored_file.is_image and os.path.exists(thumbnail_path(stored_file))


def _make_thumbnail(path, stored_name):
    thumb = os.path.join(current_app.config["THUMBNAIL_FOLDER"],
                         os.path.splitext(stored_name)[0] + ".png")
    try:
        with Image.open(path) as img:
            img.thumbnail(THUMBNAIL_SIZE)
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            img.save(thumb, "PNG")
        return True
    except Exception:
        current_app.logger.exception("Failed to create thumbnail for %s", stored_name)
        return False


def save_upload(file_storage, user, folder=None):
    """Persist an uploaded file and create its DB rows. Returns StoredFile."""
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


def find_duplicates(stored_file):
    """Other files of the same user with identical content (same SHA-256)."""
    if not stored_file.checksum:
        return []
    return (StoredFile.query
            .filter(StoredFile.user_id == stored_file.user_id,
                    StoredFile.checksum == stored_file.checksum,
                    StoredFile.id != stored_file.id,
                    StoredFile.deleted_at.is_(None))
            .order_by(StoredFile.created_at)
            .all())


def duplicate_checksums(user_id):
    """Checksums that exist on more than one of the user's files."""
    from sqlalchemy import func

    rows = (db.session.query(StoredFile.checksum)
            .filter(StoredFile.user_id == user_id,
                    StoredFile.checksum.isnot(None),
                    StoredFile.deleted_at.is_(None))
            .group_by(StoredFile.checksum)
            .having(func.count() > 1)
            .all())
    return {row[0] for row in rows}


def delete_file(stored_file):
    """Soft-delete: move the file to the trash (blob and history are kept)."""
    _guard_not_synced(stored_file)
    from ..models import utcnow
    stored_file.deleted_at = utcnow()
    db.session.commit()


def restore_file(stored_file):
    """Bring a trashed file back to its drive."""
    _guard_not_synced(stored_file)
    stored_file.deleted_at = None
    db.session.commit()


def purge_file(stored_file):
    """Permanently delete: DB rows (index/versions cascade) and disk blobs."""
    _guard_not_synced(stored_file)
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
    db.session.delete(stored_file)
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
    _guard_not_synced(stored_file)
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
    _guard_not_synced(stored_file)
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
    os.replace(tmp_path, file_path(stored_file))
    stored_file.size = size
    stored_file.checksum = new_checksum
    stored_file.mime_type = file_storage.mimetype
    if stored_file.is_image:
        _make_thumbnail(file_path(stored_file), stored_file.stored_name)
    db.session.commit()
    return "replaced"


def restore_version(stored_file, version):
    """Roll the file back to a past version (current state is snapshotted)."""
    snapshot_version(stored_file, "restore",
                     note=f"Before restore to v{version.version}")
    shutil.copyfile(version_path(version), file_path(stored_file))
    stored_file.size = version.size
    stored_file.checksum = version.checksum
    if stored_file.is_image:
        _make_thumbnail(file_path(stored_file), stored_file.stored_name)
    db.session.commit()


def update_file_content(stored_file, content, source="edit", note=""):
    """Overwrite an editable text file's content, keeping a version snapshot."""
    snapshot_version(stored_file, source, note=note)
    path = file_path(stored_file)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    stored_file.size = os.path.getsize(path)
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    stored_file.checksum = checksum
    db.session.commit()


def read_text_content(stored_file, max_chars=200_000):
    path = file_path(stored_file)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(max_chars)
