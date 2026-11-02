"""Per-user local repository connections."""

import os
from datetime import UTC, datetime

from app.extensions import db


class Repository(db.Model):
    __tablename__ = "mod_gitsync_repository"
    __table_args__ = (db.UniqueConstraint("user_id", "path"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    drive_id = db.Column(db.Integer, db.ForeignKey("drives.id", ondelete="SET NULL"))
    name = db.Column(db.String(120), nullable=False)
    path = db.Column(db.Text, nullable=False)
    account = db.Column(db.String(120), nullable=False, default="")  # legacy, unused
    baseline = db.Column(db.String(64))
    author_name = db.Column(db.String(120), nullable=False, default="")
    author_email = db.Column(db.String(254), nullable=False, default="")
    remote_username = db.Column(db.String(255))
    remote_token = db.Column(db.Text)  # Fernet-encrypted (ai_service.encrypt_api_key)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC))

    drive = db.relationship("Drive")

    @property
    def linked_drive(self):
        """The drive row only while it still mirrors this folder for this user.

        SQLite runs without foreign-key enforcement, so ``drive_id`` is never
        nulled when a drive is removed and the id may later be reused by an
        unrelated drive. Trashed drives are still returned (check deleted_at).
        """
        drive = self.drive
        if drive is None or drive.user_id != self.user_id or not drive.source_path:
            return None
        if same_path(drive.source_path, self.path):
            return drive
        return None


def same_path(value, root):
    if not value or not root:
        return False
    return os.path.normcase(os.path.realpath(value)) == os.path.normcase(os.path.realpath(root))
