"""Module tables use the mod_<name>_ prefix."""

from datetime import datetime, timezone

from app.extensions import db


class HelloNote(db.Model):
    __tablename__ = "mod_hello_note"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    text = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc))
