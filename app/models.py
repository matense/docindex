from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    folders = db.relationship("Folder", backref="owner", lazy="dynamic",
                              cascade="all, delete-orphan")
    files = db.relationship("StoredFile", backref="owner", lazy="dynamic",
                            cascade="all, delete-orphan")
    drives = db.relationship("Drive", backref="owner", lazy="dynamic",
                             cascade="all, delete-orphan")
    conversations = db.relationship("ChatConversation", backref="owner", lazy="dynamic",
                                    cascade="all, delete-orphan")
    ai_connections = db.relationship("AIConnection", backref="owner", lazy="dynamic",
                                     cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.username}>"


class Drive(db.Model):
    """A named vault that groups a user's files and folders separately."""

    __tablename__ = "drives"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(500), nullable=False, default="")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    folders = db.relationship("Folder", backref="drive", lazy="dynamic",
                              cascade="all, delete-orphan")
    files = db.relationship("StoredFile", backref="drive", lazy="dynamic",
                            cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Drive {self.name}>"


class Folder(db.Model):
    __tablename__ = "folders"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("folders.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    drive_id = db.Column(db.Integer, db.ForeignKey("drives.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    children = db.relationship(
        "Folder",
        backref=db.backref("parent", remote_side=[id]),
        lazy="select",
        cascade="all, delete-orphan",
    )
    files = db.relationship("StoredFile", backref="folder", lazy="select",
                            cascade="all, delete-orphan")

    def breadcrumb(self):
        parts, node = [], self
        while node is not None:
            parts.append(node)
            node = node.parent
        return list(reversed(parts))

    def __repr__(self):
        return f"<Folder {self.name}>"


class StoredFile(db.Model):
    __tablename__ = "files"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False, unique=True)
    extension = db.Column(db.String(20), nullable=False, default="")
    mime_type = db.Column(db.String(120), nullable=True)
    size = db.Column(db.Integer, nullable=False, default=0)
    checksum = db.Column(db.String(64), nullable=True)
    folder_id = db.Column(db.Integer, db.ForeignKey("folders.id"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    drive_id = db.Column(db.Integer, db.ForeignKey("drives.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    index = db.relationship("FileIndex", backref="file", uselist=False,
                            cascade="all, delete-orphan")

    @property
    def is_image(self):
        return self.extension in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}

    @property
    def is_editable(self):
        from flask import current_app
        return self.extension in current_app.config["EDITABLE_EXTENSIONS"]

    def __repr__(self):
        return f"<StoredFile {self.name}>"


class FileIndex(db.Model):
    """Full-text search index for a stored file."""

    __tablename__ = "file_index"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=False,
                        unique=True, index=True)
    extracted_text = db.Column(db.Text, nullable=True)
    caption = db.Column(db.Text, nullable=True)  # AI-generated image caption
    word_count = db.Column(db.Integer, nullable=True)
    line_count = db.Column(db.Integer, nullable=True)
    char_count = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending/ok/error
    error = db.Column(db.Text, nullable=True)
    indexed_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<FileIndex file={self.file_id} status={self.status}>"


class AIConnection(db.Model):
    """A user-defined OpenAI-compatible AI provider connection."""

    __tablename__ = "ai_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    base_url = db.Column(db.String(500), nullable=False)
    api_key = db.Column(db.String(500), nullable=False, default="")
    model = db.Column(db.String(120), nullable=False)
    vision_model = db.Column(db.String(120), nullable=False, default="")
    # Per-connection agent step limit; NULL falls back to the global AI_MAX_STEPS.
    max_steps = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f"<AIConnection {self.name} ({self.model})>"


class ChatConversation(db.Model):
    __tablename__ = "chat_conversations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False, default="New conversation")
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    messages = db.relationship("ChatMessage", backref="conversation", lazy="select",
                               cascade="all, delete-orphan",
                               order_by="ChatMessage.created_at")

    def __repr__(self):
        return f"<ChatConversation {self.id} '{self.title}'>"


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("chat_conversations.id"),
                                nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)  # user / assistant / thinking / step
    content = db.Column(db.Text, nullable=False, default="")
    # Model that produced the message (assistant messages only; NULL otherwise).
    model = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f"<ChatMessage {self.role} conv={self.conversation_id}>"
