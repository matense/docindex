import os

from flask import Flask
from sqlalchemy import event

from config import Config

from .extensions import csrf, db, login_manager, migrate


def _sqlite_pragmas(dbapi_conn, _connection_record):
    """Avoid 'database is locked' errors under concurrent requests."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["THUMBNAIL_FOLDER"], exist_ok=True)
    os.makedirs(app.config["VERSIONS_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        with app.app_context():
            event.listens_for(db.engine, "connect")(_sqlite_pragmas)

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from .routes import ai, auth, drive, search, settings

    app.register_blueprint(auth.bp)
    app.register_blueprint(drive.bp)
    app.register_blueprint(search.bp)
    app.register_blueprint(ai.bp)
    app.register_blueprint(settings.bp)

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

    # AI API endpoints are JSON; the frontend attaches the CSRF token via
    # the fetch() wrapper, so no exemptions needed.

    @app.context_processor
    def inject_globals():
        from flask_login import current_user as cu
        from .services import ai_service, drive_service
        enabled = ai_service.is_enabled(cu) if cu.is_authenticated else False
        current_drive = drive_service.get_current_drive(cu) if cu.is_authenticated else None
        return {
            "ai_enabled": enabled,
            "app_name": "DocIndex",
            "current_drive": current_drive,
            "user_drives": drive_service.list_drives(cu) if cu.is_authenticated else [],
        }

    return app
