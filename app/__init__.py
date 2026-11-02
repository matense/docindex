import os

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
APP_VERSION = "0.1.12"

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


def _sqlite_listener(index_path):
    """Per-engine connect listener: pragmas + ATTACH of the index db.

    Attaching on every connection (NullPool opens one per checkout)
    keeps schema-qualified tables (`idx.*`) and cross-db joins working
    everywhere. One SQLite connection = one transaction across both
    files, so meta + index writes stay atomic.

    The listener is (re-)registered on every create_app call on purpose:
    ATTACH is per-connection, and a fresh connection never carries `idx`,
    so DETACH-then-ATTACH is idempotent no matter how many listeners pile
    up. (A guard keyed by id(engine) would be wrong — CPython reuses ids
    of garbage-collected engines, silently skipping the listener for new
    engines, e.g. across hundreds of test apps.)
    """
    def _on_connect(dbapi_conn, _connection_record):
        """Avoid 'database is locked' errors under concurrent requests."""
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        if index_path is not None:
            try:
                cursor.execute("DETACH DATABASE idx")
            except Exception:  # noqa: BLE001 - not attached: the common case
                pass
            quoted = "'" + index_path.replace("'", "''") + "'"
            cursor.execute(f"ATTACH DATABASE {quoted} AS idx")
            cursor.execute("PRAGMA idx.journal_mode=WAL")
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

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        with app.app_context():
            engine = db.engine
            listener = _sqlite_listener(_index_db_path(
                app.config["SQLALCHEMY_DATABASE_URI"]))
            event.listens_for(engine, "connect")(listener)

    # Keep the schema current on startup: users who pull new code but forget
    # `flask db upgrade` would otherwise get "no such column" 500s on every
    # page. Alembic's upgrade is a no-op when the DB is already at head, and
    # unknown revisions are reported without rewriting migration stamps.
    # Skipped in tests (they build the schema with db.create_all()).
    if not app.config.get("TESTING"):
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
    # (guarded per-request by the persisted enable flag).
    from .services import module_service
    module_service.load_all(app)

    # Central error log: capture app.logger warnings and unhandled exceptions
    # into the error_logs table (visible on /settings/logs).
    from .services import log_service
    log_service.install(app)

    # Resume any indexing work left over from a previous run (crash/restart):
    # "running" jobs go back to "pending" and drainers pick them up.
    if app.config.get("INDEX_ASYNC", True):
        from .services import indexing_service
        try:
            indexing_service.recover_interrupted(app)
        except Exception:  # noqa: BLE001 - never block app startup on this
            app.logger.exception("Index queue recovery failed")

    # Long-horizon AI tasks left queued/running by a previous process are
    # marked "interrupted" (their threads died with it).
    if not app.config.get("TESTING"):
        from .services import ai_task_service
        try:
            ai_task_service.recover_interrupted(app)
        except Exception:  # noqa: BLE001 - never block app startup on this
            app.logger.exception("AI task recovery failed")

    # Trash janitor: hourly pass purging items older than 30 days for users
    # who opted into automatic purge (default off).
    if not app.config.get("TESTING"):
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
