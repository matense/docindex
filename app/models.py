from datetime import UTC, datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow():
    return datetime.now(UTC)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    theme = db.Column(db.String(10), nullable=False,
                      default="light", server_default="light")
    trash_auto_purge = db.Column(db.Boolean, nullable=False, default=False,
                                 server_default="false")  # purge trash older than 30 days
    static_background = db.Column(db.Boolean, nullable=False, default=False,
                                  server_default="false")  # no animated background
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
    source_path = db.Column(db.String(500), nullable=True)  # local folder root (synced drive)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    last_sync_stats = db.Column(db.Text, nullable=True)  # JSON stats of the last sync
    captions_enabled = db.Column(db.Boolean, nullable=False, default=True)  # AI image captions
    index_workers = db.Column(db.Integer, nullable=False, default=1)  # parallel indexing workers
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)  # soft-delete: trash, restorable
    # 'mirror' = read-only local-folder mirror; 'shared' = writable two-way
    # sync (e.g. a Dropbox/OneDrive folder) with version history.
    sync_mode = db.Column(db.String(20), nullable=False, default="mirror")
    # JSON list of drive-relative paths the sync must ignore (e.g. purged
    # files on shared drives, whose real blobs are never deleted).
    sync_ignored = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    folders = db.relationship("Folder", backref="drive", lazy="dynamic",
                              cascade="all, delete-orphan")
    files = db.relationship("StoredFile", backref="drive", lazy="dynamic",
                            cascade="all, delete-orphan")

    @property
    def is_synced(self):
        """Synced drives mirror a local folder and are read-only."""
        return self.source_path is not None

    @property
    def is_shared(self):
        """Writable two-way sync drive (Dropbox/OneDrive style)."""
        return (self.source_path is not None
                and (self.sync_mode or "mirror") == "shared")

    @property
    def is_readonly(self):
        """Mirror (read-only) synced drive."""
        return self.source_path is not None and not self.is_shared

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
    source_path = db.Column(db.String(500), nullable=True)  # real path (synced files)
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)  # soft-delete (trash)
    # Internal global identifier for notebooks (.pdocnb) — never shown in
    # the UI. Assigned at creation; older notebooks get one on next access.
    uuid = db.Column(db.String(36), nullable=True, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    index = db.relationship("FileIndex", backref="file", uselist=False,
                            cascade="all, delete-orphan")
    versions = db.relationship("FileVersion", backref="file", lazy="select",
                               cascade="all, delete-orphan",
                               order_by="FileVersion.version.desc()")

    @property
    def is_image(self):
        return self.extension in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}

    @property
    def is_synced(self):
        """Synced files map to a real file on disk and are read-only."""
        return self.source_path is not None

    @property
    def is_editable(self):
        from flask import current_app
        return self.extension in current_app.config["EDITABLE_EXTENSIONS"]

    def __repr__(self):
        return f"<StoredFile {self.name}>"


class FileVersion(db.Model):
    """A snapshot of a file's previous content (version history)."""

    __tablename__ = "file_versions"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False)
    stored_name = db.Column(db.String(255), nullable=False, unique=True)
    size = db.Column(db.Integer, nullable=False, default=0)
    checksum = db.Column(db.String(64), nullable=True)
    source = db.Column(db.String(20), nullable=False, default="upload")  # upload/edit/merge/restore
    note = db.Column(db.String(255), nullable=False, default="")
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f"<FileVersion file={self.file_id} v{self.version}>"


class FileIndex(db.Model):
    """Full-text search index for a stored file."""

    __tablename__ = "file_index"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=False,
                        unique=True, index=True)
    extracted_text = db.Column(db.Text, nullable=True)
    caption = db.Column(db.Text, nullable=True)  # AI-generated image caption
    hashtags = db.Column(db.Text, nullable=True)  # JSON array of tags (user or AI)
    word_count = db.Column(db.Integer, nullable=True)
    line_count = db.Column(db.Integer, nullable=True)
    char_count = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending/ok/error
    error = db.Column(db.Text, nullable=True)
    indexed_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<FileIndex file={self.file_id} status={self.status}>"


class FileEmbedding(db.Model):
    """One embedded text chunk for semantic search (native, no vector DB).

    Chunk texts come from the extracted text (or image caption + name);
    ``text_hash`` skips re-embedding unchanged chunks; ``model``/``dims``
    record what produced the vector so a model change re-embeds cleanly.
    """

    __tablename__ = "file_embeddings"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("files.id"),
                        nullable=False, index=True)
    chunk_index = db.Column(db.Integer, nullable=False, default=0)
    text = db.Column(db.Text, nullable=True)
    text_hash = db.Column(db.String(64), nullable=True)
    embedding = db.Column(db.LargeBinary, nullable=True)
    model = db.Column(db.String(120), nullable=True)
    dims = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    __table_args__ = (db.UniqueConstraint("file_id", "chunk_index"),)

    def __repr__(self):
        return f"<FileEmbedding file={self.file_id} chunk={self.chunk_index}>"


class IndexJob(db.Model):
    """Persistent indexing queue entry — survives restarts, retries on failure.

    Statuses: pending → running → done | error (error after 3 attempts).
    Finished jobs are kept as recent history (purged after 24h on startup).
    """

    __tablename__ = "index_jobs"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=False,
                        index=True)
    status = db.Column(db.String(20), nullable=False, default="pending",
                       index=True)  # pending/running/done/error
    attempts = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        return f"<IndexJob file={self.file_id} status={self.status}>"


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
    # Embedding model for semantic search (OpenAI-compatible /v1/embeddings
    # on the same base URL); empty = keyword-only search for this connection
    # (falls back to the global AI_EMBEDDING_MODEL).
    embedding_model = db.Column(db.String(120), nullable=False, default="")
    # Per-connection agent step limit; NULL falls back to the global AI_MAX_STEPS.
    max_steps = db.Column(db.Integer, nullable=True)
    # Per-connection request limit (requests/minute); NULL falls back to the
    # global AI_RATE_LIMIT_RPM (default 30). 0 = unlimited.
    rate_limit_rpm = db.Column(db.Integer, nullable=True)
    # Per-connection chat history size (user/assistant messages sent to the
    # model); NULL falls back to the global AI_HISTORY_MESSAGES (default 20).
    history_messages = db.Column(db.Integer, nullable=True)
    # Per-connection prompt budget (estimated tokens); NULL falls back to the
    # global AI_MAX_PROMPT_TOKENS (default 60000). Oldest messages are dropped
    # to fit — protects models with small context windows.
    max_prompt_tokens = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f"<AIConnection {self.name} ({self.model})>"


class Setting(db.Model):
    """Instance-wide key/value setting (e.g. registration_enabled)."""

    __tablename__ = "settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(500), nullable=False, default="")

    @staticmethod
    def get(key, default=""):
        s = db.session.get(Setting, key)
        return s.value if s else default

    @staticmethod
    def get_bool(key, default=False):
        return Setting.get(key, "1" if default else "0").lower() in ("1", "true", "yes")

    @staticmethod
    def set(key, value):
        s = db.session.get(Setting, key)
        if not s:
            s = Setting(key=key)
            db.session.add(s)
        s.value = str(value)
        db.session.commit()

    def __repr__(self):
        return f"<Setting {self.key}={self.value}>"


class ChatConversation(db.Model):
    __tablename__ = "chat_conversations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False, default="New conversation")
    # JSON snapshot of the context used in the latest message (drives,
    # attachments, open file, cell) — reopening the conversation restores it.
    context = db.Column(db.Text, nullable=True)
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
    role = db.Column(db.String(20), nullable=False)  # user / assistant / thinking / step / notice
    content = db.Column(db.Text, nullable=False, default="")
    # Model that produced the message (assistant messages only; NULL otherwise).
    model = db.Column(db.String(120), nullable=True)
    # True once a summarize-and-reset compacted this message: it stays visible
    # in the UI (grayed out) but is no longer sent to the model as context.
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f"<ChatMessage {self.role} conv={self.conversation_id}>"


class AITask(db.Model):
    """A long-horizon background AI task: an agent run decoupled from the HTTP
    request, with every event persisted as chat messages so the user can open
    the transcript at any time. Status lifecycle:
    queued -> running -> done | error | stopped | interrupted.

    One conversation can own several sequential tasks (background mode
    continues the same chat); the latest task decides the conversation's
    background state."""

    __tablename__ = "ai_tasks"

    STATUSES = ("queued", "running", "done", "error", "stopped", "interrupted")
    FINAL_STATUSES = ("done", "error", "stopped", "interrupted")

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("chat_conversations.id"),
                                nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False, default="Background task")
    # The exact user question that launched this task (title is truncated).
    prompt = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="queued", index=True)
    error = db.Column(db.Text, nullable=True)
    # True once the user dismissed the finished task from the bottom dock.
    notified = db.Column(db.Boolean, nullable=False, default=False)
    # JSON snapshot of the context the task was launched with (drives, files,
    # open file, notebook cell). Reopening the task's chat keeps this context
    # instead of adopting whatever the user happens to be viewing now.
    context = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    conversation = db.relationship("ChatConversation",
                                     backref=db.backref("tasks"))

    @property
    def is_active(self):
        return self.status in ("queued", "running")

    def __repr__(self):
        return f"<AITask {self.id} {self.status} '{self.title[:30]}'>"


class ErrorLog(db.Model):
    """Central application log — errors and warnings from anywhere in the app
    (unhandled request exceptions, background indexing/sync failures, AI
    provider errors, ...), shown on the settings logs page."""

    __tablename__ = "error_logs"

    id = db.Column(db.Integer, primary_key=True)
    level = db.Column(db.String(10), nullable=False, default="error", index=True)   # error | warning
    source = db.Column(db.String(40), nullable=False, default="app", index=True)    # ai_chat, indexing, sync, http, ...
    message = db.Column(db.Text, nullable=False, default="")
    detail = db.Column(db.Text, nullable=True)   # traceback / extra context
    path = db.Column(db.String(255), nullable=True)  # request path, when relevant
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    def __repr__(self):
        return f"<ErrorLog {self.level} {self.source}: {self.message[:40]!r}>"


class ModuleState(db.Model):
    """Enable/disable state for discovered modules (admin-managed). Modules
    not present in this table are disabled by default."""

    __tablename__ = "module_states"

    name = db.Column(db.String(64), primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    enabled_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    enabled_at = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def is_enabled(name):
        s = db.session.get(ModuleState, name)
        return bool(s and s.enabled)


class Behavior(db.Model):
    """A user-made reusable AI instruction ("system prompt").

    Core feature: picked in the chat (persona) and in the Automation AI
    nodes' behavior field. ``enabled`` hides it from pickers without
    deleting it (nodes that saved its text keep their copy)."""

    __tablename__ = "behaviors"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    favorite = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        return f"<Behavior {self.id} '{self.name}'>"


class PersonaPref(db.Model):
    """Per-user enable/disable flag for a bundled persona (Agency library).

    Personas are enabled by default; a row exists only once the user
    toggles one (on/off) or stars it as a favorite."""

    __tablename__ = "persona_prefs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    persona_key = db.Column(db.String(160), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    favorite = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "persona_key",
                            name="uq_persona_pref_user_key"),
    )

    def __repr__(self):
        return f"<PersonaPref {self.user_id} {self.persona_key}={self.enabled}>"


class NotebookOrganizePlan(db.Model):
    """A notebook organize run: the plan (moves) that was previewed and
    confirmed, so 'Revert last organize' can replay the inverse moves even
    after a restart. Moves go through the normal file-move path, so each
    notebook keeps its full version history."""

    __tablename__ = "notebook_organize_plans"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    # [{file_id, name, from_drive_id, from_folder_id,
    #   to_drive_id, to_folder_id}]
    plan = db.Column(db.JSON, nullable=False, default=list)
    applied = db.Column(db.Boolean, nullable=False, default=False)
    reverted = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return (f"<NotebookOrganizePlan {self.id} "
                f"applied={self.applied} reverted={self.reverted}>")
