import hashlib
import os
import uuid

from flask import current_app
from PIL import Image
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import FileIndex, StoredFile

THUMBNAIL_SIZE = (256, 256)


def allowed_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in current_app.config["ALLOWED_EXTENSIONS"]


def get_extension(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def file_path(stored_file):
    return os.path.join(current_app.config["UPLOAD_FOLDER"], stored_file.stored_name)


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
                    StoredFile.id != stored_file.id)
            .order_by(StoredFile.created_at)
            .all())


def duplicate_checksums(user_id):
    """Checksums that exist on more than one of the user's files."""
    from sqlalchemy import func

    rows = (db.session.query(StoredFile.checksum)
            .filter(StoredFile.user_id == user_id,
                    StoredFile.checksum.isnot(None))
            .group_by(StoredFile.checksum)
            .having(func.count() > 1)
            .all())
    return {row[0] for row in rows}


def delete_file(stored_file):
    """Delete a stored file: DB rows (index cascades), disk file, thumbnail."""
    for path in (file_path(stored_file), thumbnail_path(stored_file)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            current_app.logger.exception("Failed to remove %s", path)
    db.session.delete(stored_file)
    db.session.commit()


def update_file_content(stored_file, content):
    """Overwrite an editable text file's content on disk and bump timestamps."""
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
