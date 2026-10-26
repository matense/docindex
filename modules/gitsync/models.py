"""Per-user local repository connections."""

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
    account = db.Column(db.String(120), nullable=False, default="")
    baseline = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC))

    drive = db.relationship("Drive")