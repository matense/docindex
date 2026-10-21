import os
from datetime import timedelta

from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, ".env"))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY")
    if not SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. "
            "Add it to your .env file (see .env.example)."
        )

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(basedir, "instance", "docindex.sqlite")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Session hardening (P0 security review).
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get(
        "SESSION_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_DURATION = timedelta(days=14)
    REMEMBER_COOKIE_SECURE = os.environ.get(
        "SESSION_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
    PERMANENT_SESSION_LIFETIME = timedelta(days=14)
    # SQLite: background threads (indexing, folder sync) plus web requests can
    # exceed the default QueuePool (5+10) and deadlock on "connection timed
    # out". NullPool opens/closes connections on demand — the right choice for
    # SQLite — and the busy timeout lets writers wait for the file lock.
    if SQLALCHEMY_DATABASE_URI.startswith("sqlite") and ":memory:" not in SQLALCHEMY_DATABASE_URI:
        from sqlalchemy.pool import NullPool
        SQLALCHEMY_ENGINE_OPTIONS = {"poolclass": NullPool,
                                     "connect_args": {"timeout": 30}}

    # Uploads
    UPLOAD_FOLDER = os.path.join(basedir, "uploads", "files")
    THUMBNAIL_FOLDER = os.path.join(basedir, "uploads", "thumbnails")
    VERSIONS_FOLDER = os.path.join(basedir, "uploads", "versions")
    # Last-seen copies for writable shared sync drives (outside any sync
    # root). Cap bounds disk use; bigger files skip content history.
    SHADOW_FOLDER = os.path.join(basedir, "uploads", "shadow")
    SHARED_SHADOW_MAX_BYTES = 10 * 1024 * 1024
    # Extension modules: each subdirectory with a valid module.json is a
    # discoverable module (disabled by default; admin enables them).
    MODULES_FOLDER = os.environ.get(
        "MODULES_FOLDER", os.path.join(basedir, "modules"))
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100 MB per request batch
    MAX_FILE_SIZE = 16 * 1024 * 1024  # 16 MB per file
    ALLOWED_EXTENSIONS = {
        # documents
        "pdf", "docx", "xlsx", "xlsm", "xls", "txt", "md", "csv", "json",
        "xml", "html", "log",
        # code
        "py", "js", "ts", "java", "c", "cpp", "h", "cs", "go", "rs", "rb",
        "php", "sql", "sh", "css", "yaml", "yml", "toml", "ini",
        # images
        "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg",
    }
    IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
    EDITABLE_EXTENSIONS = {
        "txt", "md", "csv", "json", "xml", "html", "log",
        "py", "js", "ts", "java", "c", "cpp", "h", "cs", "go", "rs", "rb",
        "php", "sql", "sh", "css", "yaml", "yml", "toml", "ini",
    }

    # OCR
    TESSERACT_LANGS = os.environ.get("TESSERACT_LANGS", "eng+por")

    # AI (OpenAI-compatible endpoint: Ollama, LM Studio, OpenAI API, ...)
    def _int_env(key, default, lo=None, hi=None):
        try:
            v = int(os.environ.get(key, str(default)))
        except (TypeError, ValueError):
            v = default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    AI_ENABLED = os.environ.get("AI_ENABLED", "false").lower() in ("1", "true", "yes")
    AI_BASE_URL = os.environ.get("AI_BASE_URL", "http://localhost:11434/v1")
    AI_API_KEY = os.environ.get("AI_API_KEY", "")
    AI_MODEL = os.environ.get("AI_MODEL", "llama3.1")
    AI_VISION_MODEL = os.environ.get("AI_VISION_MODEL", "")  # falls back to AI_MODEL
    # Embedding model for semantic search (OpenAI-compatible /v1/embeddings
    # on the same base URL — e.g. "text-embedding-qwen" in LM Studio or
    # "nomic-embed-text" in Ollama). Empty disables semantic search and the
    # app transparently falls back to keyword (FTS5) search.
    AI_EMBEDDING_MODEL = os.environ.get("AI_EMBEDDING_MODEL", "")
    # Semantic chunking/indexing knobs: ~chars per chunk, overlap between
    # consecutive chunks, max chunks embedded per file.
    AI_EMBED_CHUNK_CHARS = _int_env("AI_EMBED_CHUNK_CHARS", 2000, 200, 20000)
    AI_EMBED_CHUNK_OVERLAP = _int_env("AI_EMBED_CHUNK_OVERLAP", 200, 0, 5000)
    AI_EMBED_MAX_CHUNKS = _int_env("AI_EMBED_MAX_CHUNKS", 200, 1, 2000)
    AI_MAX_STEPS = _int_env("AI_MAX_STEPS", 16, 1, 128)
    AI_RATE_LIMIT_RPM = _int_env("AI_RATE_LIMIT_RPM", 30, 0, 600)
    # How many past user/assistant chat messages are sent to the model.
    AI_HISTORY_MESSAGES = _int_env("AI_HISTORY_MESSAGES", 20, 1, 200)
    # Prompt budget in estimated tokens (chars/4). When a chat run would
    # exceed it, the oldest messages are dropped until it fits.
    AI_MAX_PROMPT_TOKENS = _int_env("AI_MAX_PROMPT_TOKENS", 60000, 1000, 500000)
    AI_REQUEST_TIMEOUT = _int_env("AI_REQUEST_TIMEOUT", 300, 5, 900)
    # Max words per AI-generated hashtag (user-added tags are not limited).
    AI_HASHTAG_MAX_WORDS = _int_env("AI_HASHTAG_MAX_WORDS", 6, 1, 20)
    # Token-by-token streaming for chat. Set to false if your provider breaks
    # with streamed tool calls (a non-streaming fallback is tried anyway).
    AI_STREAMING = os.environ.get("AI_STREAMING", "true").lower() in ("1", "true", "yes")
    # Long-horizon background tasks: much larger step budget than interactive
    # chat, and the dock widget poll interval for their status.
    AI_TASK_MAX_STEPS = _int_env("AI_TASK_MAX_STEPS", 64, 1, 256)
    AI_TASK_WIDGET_POLL_MS = _int_env("AI_TASK_WIDGET_POLL_MS", 3000, 1000, 30000)

    # Search / indexing
    # FTS5 full-text search (BM25 ranking). Falls back to ILIKE when off or
    # when the SQLite build lacks FTS5.
    SEARCH_FTS = os.environ.get("SEARCH_FTS", "true").lower() in ("1", "true", "yes")
    # Background drainers processing the persistent index queue (index_jobs).
    INDEX_WORKERS = _int_env("INDEX_WORKERS", 2, 1, 8)

    # Central error log (error_logs table, /settings/logs): keep the newest N
    # rows, older ones are pruned automatically.
    ERROR_LOG_KEEP = _int_env("ERROR_LOG_KEEP", 2000, 100, 50000)

    # Index files in a background thread on upload (disable in tests)
    INDEX_ASYNC = True

    # Run folder syncs in a background thread (disable in tests)
    SYNC_ASYNC = True

    # Run bulk hashtag generation in background threads (disable in tests)
    HASHTAG_ASYNC = True

    # Run long-horizon AI tasks in background threads (disable in tests)
    AI_TASKS_ASYNC = True

    # Run automation workflow executions in background threads
    AUTOMATION_ASYNC = True

    # Automation module schedule-trigger thread (checks due workflows)
    AUTOMATION_SCHEDULER = True


class TestConfig(Config):
    SECRET_KEY = "test-secret-key"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    TESTING = True
    WTF_CSRF_ENABLED = False
    AI_ENABLED = False
    # Tests patch the non-streaming chat_completion; streaming has its own tests.
    AI_STREAMING = False
    INDEX_ASYNC = False
    SYNC_ASYNC = False
    HASHTAG_ASYNC = False
    AI_TASKS_ASYNC = False
    AUTOMATION_ASYNC = False
    AUTOMATION_SCHEDULER = False
    UPLOAD_FOLDER = os.path.join(basedir, "instance", "test_uploads")
    THUMBNAIL_FOLDER = os.path.join(basedir, "instance", "test_thumbnails")
    VERSIONS_FOLDER = os.path.join(basedir, "instance", "test_versions")
    SHADOW_FOLDER = os.path.join(basedir, "instance", "test_shadow")
    # Tests point at the bundled example module.
    MODULES_FOLDER = os.path.join(basedir, "modules")
