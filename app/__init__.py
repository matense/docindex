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
APP_VERSION = "0.1.3"

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


def _sqlite_pragmas(dbapi_conn, _connection_record):
    """Avoid 'database is locked' errors under concurrent requests."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


# Engines (not apps) that already carry the pragma listener: create_app()
# runs per test and each app builds its own engine, so guard per-engine —
# a global one-shot flag would leave later apps without WAL/busy_timeout.
_sqlite_pragma_engines = set()


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
            if id(engine) not in _sqlite_pragma_engines:
                event.listens_for(engine, "connect")(_sqlite_pragmas)
                _sqlite_pragma_engines.add(id(engine))

    # Keep the schema current on startup: users who pull new code but forget
    # `flask db upgrade` would otherwise get "no such column" 500s on every
    # page. Alembic's upgrade is a no-op when the DB is already at head, and
    # a stamp pointing at a revision that no longer exists (first-release
    # databases) is repaired automatically.
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
                }
            cached = _g._docindex_globals
            return {
                "ai_enabled": cached["ai_enabled"],
                "app_name": "DocIndex",
                "app_version": APP_VERSION,
                "current_drive": cached["current_drive"],
                "module_nav": cached["module_nav"],
                "user_drives": cached["user_drives"],
            }
        return {
            "ai_enabled": False,
            "app_name": "DocIndex",
            "app_version": APP_VERSION,
            "current_drive": None,
            "module_nav": [],
            "user_drives": [],
        }

    return app
