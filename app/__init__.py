import os
import sqlite3

from flask import Flask
from sqlalchemy import event

from config import Config

from .extensions import csrf, db, login_manager, migrate

# Absolute path to the migrations folder (project root), so both
# `flask db` and the automatic startup upgrade work no matter which
# directory the app is launched from.
MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")

# App version — bump on every release and tag it (see CONTRIBUTING.md);
# shown in the user menu and on the release notes page (/settings/releases).
APP_VERSION = "0.1.22"

# Aggregate module migrations (independent alembic roots under
# modules/<name>/migrations/versions) into every flask-migrate command.
# ScriptDirectory reads version_locations when the command starts, so this
# must wrap get_config — setting it in env.py would be too late.
_migrate_get_config = migrate.get_config


def _get_config_with_modules(directory=None, x_arg=None, opts=None):
    config = _migrate_get_config(directory, x_arg=x_arg, opts=opts)
    basedir = os.path.dirname(MIGRATIONS_DIR)
    locations = [os.path.join(MIGRATIONS_DIR, "versions")]
    modules_folder = os.environ.get(
        "MODULES_FOLDER", os.path.join(basedir, "modules"))
    if os.path.isdir(modules_folder):
        for entry in sorted(os.listdir(modules_folder)):
            module_versions = os.path.join(
                modules_folder, entry, "migrations", "versions")
            if os.path.isdir(module_versions):
                locations.append(module_versions)
    config.set_main_option("version_locations", " ".join(locations))
    return config


migrate.get_config = _get_config_with_modules


def _skip_startup_tasks():
    """True inside the one-shot migration CLI process (see Dockerfile).

    `flask --app run.py db upgrade heads` imports run.py, which calls
    create_app() at module level. Without this guard the import itself
    would run auto_upgrade (a second `alembic upgrade heads` before the
    explicit command) plus queue recovery and the janitor thread in a
    process that exits immediately. The flag is set only for the migrate
    command, so the normal server path is unchanged.
    """
    return os.environ.get(
        "DOCINDEX_SKIP_STARTUP_TASKS", "").lower() in ("1", "true", "yes")


def format_duration(seconds):
    """Human duration: '420 ms', '3.25s', '1m 05.34s', '1h 02m 03s'.

    Registered as the ``duration`` Jinja filter — used anywhere a run or
    task elapsed time is shown."""
    if seconds is None:
        return ""
    ms = max(0, round(float(seconds) * 1000))
    if ms < 1000:
        return f"{ms} ms"
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    frac = (f".{millis:03d}".rstrip("0")) if millis else ""
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}{frac}s"
    return f"{secs}{frac}s"


def _index_db_path(uri):
    """Filesystem path (or ':memory:') of the index database for a core URI.

    File cores get a sibling `<stem>_index.sqlite` next to the main file;
    `:memory:` cores get a private in-memory index db (same lifetime and
    isolation semantics as the core). Returns None for non-sqlite URIs.
    """
    from sqlalchemy.engine.url import make_url

    if not uri or not uri.startswith("sqlite"):
        return None
    try:
        db_path = make_url(uri).database
    except Exception:  # noqa: BLE001 - unparseable URI: no index split
        return None
    if not db_path or db_path == ":memory:":
        return ":memory:"
    root, ext = os.path.splitext(db_path)
    return root + "_index" + (ext or ".sqlite")


def _ensure_persistent_wal(db_uri, index_path):
    """Set `journal_mode=WAL` once at startup; it persists in the files.

    WAL mode is stored in each database file's header, so every later
    connection (including each fresh NullPool checkout) inherits it with
    no per-checkout PRAGMA. Best-effort: never block startup (read-only
    filesystems, `:memory:` test DBs, unparseable URIs).
    """
    paths = set()
    if db_uri and db_uri.startswith("sqlite"):
        try:
            from sqlalchemy.engine.url import make_url

            db_path = make_url(db_uri).database
        except Exception:  # noqa: BLE001 - unparseable URI: nothing to persist
            db_path = None
        if db_path:
            paths.add(db_path)
    if index_path:
        paths.add(index_path)
    for path in sorted(paths):
        if not path or path == ":memory:":
            continue
        try:
            raw = sqlite3.connect(path)
            try:
                raw.execute("PRAGMA journal_mode=WAL")
            finally:
                raw.close()
        except Exception:  # noqa: BLE001 - WAL is an optimization, not startup-critical
            continue


def _sqlite_listener(index_path):
    """Per-engine connect listener: pragmas + ATTACH of the index db.

    Attaching on every connection (NullPool opens one per checkout)
    keeps schema-qualified tables (`idx.*`) and cross-db joins working
    everywhere. One SQLite connection = one transaction across both
    files, so meta + index writes stay atomic.

    `journal_mode=WAL` is persistent in the database files, so it is set
    once at startup (_ensure_persistent_wal), not per checkout. There is
    likewise no DETACH: this listener only ever runs on a brand-new
    DBAPI connection, which never carries `idx`, so DETACH always fails
    (swallowed) and is pure per-checkout overhead. `synchronous` and
    `busy_timeout` are per-connection settings and stay here; ATTACH is
    per-connection by nature and stays too.
    """

    def _on_connect(dbapi_conn, _connection_record):
        """Avoid 'database is locked' errors under concurrent requests."""
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        if index_path is not None:
            quoted = "'" + index_path.replace("'", "''") + "'"
            cursor.execute(f"ATTACH DATABASE {quoted} AS idx")
            cursor.execute("PRAGMA idx.synchronous=NORMAL")
            cursor.execute("PRAGMA idx.busy_timeout=30000")
        cursor.close()

    return _on_connect


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    app.add_template_filter(format_duration, "duration")

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["THUMBNAIL_FOLDER"], exist_ok=True)
    os.makedirs(app.config["VERSIONS_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db, directory=MIGRATIONS_DIR)
    csrf.init_app(app)

    # Central error log: capture app.logger warnings and unhandled exceptions
    # into the error_logs table (visible on /settings/logs). Installed FIRST,
    # before auto-upgrade/module loading/index recovery below, so startup
    # failures (e.g. a module that fails to load) are captured too instead
    # of going only to server stderr.
    from .services import log_service
    log_service.install(app)

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        with app.app_context():
            engine = db.engine
            index_path = _index_db_path(
                app.config["SQLALCHEMY_DATABASE_URI"])
            listener = _sqlite_listener(index_path)
            event.listens_for(engine, "connect")(listener)
            # WAL persists in the database files: set once here, not on
            # every checkout (NullPool opens a fresh connection per checkout).
            _ensure_persistent_wal(
                app.config["SQLALCHEMY_DATABASE_URI"], index_path)

    # Keep the schema current on startup: users who pull new code but forget
    # `flask db upgrade` would otherwise get "no such column" 500s on every
    # page. Alembic's upgrade is a no-op when the DB is already at head, and
    # unknown revisions are reported without rewriting migration stamps.
    # Skipped in tests (they build the schema with db.create_all()) and in
    # the migration CLI process (it runs `db upgrade` explicitly; the
    # import-time auto_upgrade would be a duplicate migration).
    if not _skip_startup_tasks() and not app.config.get("TESTING"):
        from .services import db_maintenance
        try:
            db_maintenance.auto_upgrade(app)
        except Exception:  # noqa: BLE001 - never block app startup on this
            app.logger.exception("Database migration check failed")

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from .routes import ai, auth, behaviors, drive, search, settings

    app.register_blueprint(auth.bp)
    app.register_blueprint(drive.bp)
    app.register_blueprint(search.bp)
    app.register_blueprint(ai.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(behaviors.bp)

    # Modules: import everything valid under modules/ and register routes
    # (guarded per-request by the persisted enable flag). Failures are
    # captured by the error log installed above.
    from .services import module_service
    module_service.load_all(app)

    # Resume any indexing work left over from a previous run (crash/restart):
    # "running" jobs go back to "pending" and drainers pick them up.
    # Skipped in the migration CLI process (no worker threads there).
    if app.config.get("INDEX_ASYNC", True) and not _skip_startup_tasks():
        from .services import indexing_service
        try:
            indexing_service.recover_interrupted(app)
        except Exception:  # noqa: BLE001 - never block app startup on this
            app.logger.exception("Index queue recovery failed")

    # Long-horizon AI tasks left queued/running by a previous process are
    # marked "interrupted" (their threads died with it). Skipped in the
    # migration CLI process (short-lived: no recovery writes there).
    if not _skip_startup_tasks() and not app.config.get("TESTING"):
        from .services import ai_task_service
        try:
            ai_task_service.recover_interrupted(app)
        except Exception:  # noqa: BLE001 - never block app startup on this
            app.logger.exception("AI task recovery failed")

    # Trash janitor: hourly pass purging items older than 30 days for users
    # who opted into automatic purge (default off). Skipped in the migration
    # CLI process (no janitor thread in a process that exits immediately).
    if not _skip_startup_tasks() and not app.config.get("TESTING"):
        from .services import trash_service
        try:
            trash_service.start_auto_purge(app)
        except Exception:  # noqa: BLE001 - never block app startup on this
            app.logger.exception("Trash auto-purge failed to start")

    # AI API endpoints are JSON; the frontend attaches the CSRF token via
    # the fetch() wrapper, so no exemptions needed.

    @app.context_processor
    def inject_globals():
        from flask import g as _g
        from flask_login import current_user as cu

        from .services import ai_service, drive_service, module_service
        if cu.is_authenticated:
            # Per-request cache: base template calls this once, but SPA
            # partials + widgets can trigger it multiple times per request.
            if not hasattr(_g, "_docindex_globals"):
                _g._docindex_globals = {
                    "ai_enabled": ai_service.is_enabled(cu),
                    "current_drive": drive_service.get_current_drive(cu),
                    "module_nav": module_service.nav_items(),
                    "user_drives": drive_service.list_drives(cu),
                    "gitdrives_enabled": module_service.is_enabled("gitdrives"),
                    "gitsync_enabled": module_service.is_enabled("gitsync"),
                    "automation_enabled": module_service.is_enabled("automation"),
                    "aiutils_enabled": module_service.is_enabled("aiutils"),
                }
            cached = _g._docindex_globals
            return {
                "ai_enabled": cached["ai_enabled"],
                "app_name": "DocIndex",
                "app_version": APP_VERSION,
                "current_drive": cached["current_drive"],
                "module_nav": cached["module_nav"],
                "user_drives": cached["user_drives"],
                "gitdrives_enabled": cached["gitdrives_enabled"],
                "gitsync_enabled": cached["gitsync_enabled"],
                "automation_enabled": cached["automation_enabled"],
                "aiutils_enabled": cached["aiutils_enabled"],
            }
        return {
            "ai_enabled": False,
            "app_name": "DocIndex",
            "app_version": APP_VERSION,
            "current_drive": None,
            "module_nav": [],
            "user_drives": [],
            "gitdrives_enabled": False,
            "gitsync_enabled": False,
            "automation_enabled": False,
            "aiutils_enabled": False,
        }

    return app
