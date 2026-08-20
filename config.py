import os

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
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100 MB per request batch
    MAX_FILE_SIZE = 16 * 1024 * 1024  # 16 MB per file
    ALLOWED_EXTENSIONS = {
        # documents
        "pdf", "docx", "txt", "md", "csv", "json", "xml", "html", "log",
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
    AI_ENABLED = os.environ.get("AI_ENABLED", "false").lower() in ("1", "true", "yes")
    AI_BASE_URL = os.environ.get("AI_BASE_URL", "http://localhost:11434/v1")
    AI_API_KEY = os.environ.get("AI_API_KEY", "")
    AI_MODEL = os.environ.get("AI_MODEL", "llama3.1")
    AI_VISION_MODEL = os.environ.get("AI_VISION_MODEL", "")  # falls back to AI_MODEL
    AI_MAX_STEPS = int(os.environ.get("AI_MAX_STEPS", "16"))
    AI_RATE_LIMIT_RPM = int(os.environ.get("AI_RATE_LIMIT_RPM", "30"))
    AI_REQUEST_TIMEOUT = int(os.environ.get("AI_REQUEST_TIMEOUT", "300"))
    # Max words per AI-generated hashtag (user-added tags are not limited).
    AI_HASHTAG_MAX_WORDS = int(os.environ.get("AI_HASHTAG_MAX_WORDS", "6"))
    # Token-by-token streaming for chat. Set to false if your provider breaks
    # with streamed tool calls (a non-streaming fallback is tried anyway).
    AI_STREAMING = os.environ.get("AI_STREAMING", "true").lower() in ("1", "true", "yes")

    # Search / indexing
    # FTS5 full-text search (BM25 ranking). Falls back to ILIKE when off or
    # when the SQLite build lacks FTS5.
    SEARCH_FTS = os.environ.get("SEARCH_FTS", "true").lower() in ("1", "true", "yes")
    # Background drainers processing the persistent index queue (index_jobs).
    INDEX_WORKERS = int(os.environ.get("INDEX_WORKERS", "2"))

    # Index files in a background thread on upload (disable in tests)
    INDEX_ASYNC = True

    # Run folder syncs in a background thread (disable in tests)
    SYNC_ASYNC = True

    # Run bulk hashtag generation in background threads (disable in tests)
    HASHTAG_ASYNC = True


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
    UPLOAD_FOLDER = os.path.join(basedir, "instance", "test_uploads")
    THUMBNAIL_FOLDER = os.path.join(basedir, "instance", "test_thumbnails")
    VERSIONS_FOLDER = os.path.join(basedir, "instance", "test_versions")
