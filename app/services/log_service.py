"""Central error/warning log persisted in the database (error_logs table).

Captures, in one place:
- unhandled request exceptions (registered as a Flask errorhandler);
- anything logged via ``app.logger.warning()/.error()/.exception()`` — which is
  how indexing, sync, thumbnails, OCR and friends already report problems —
  through the ``DbLogHandler`` logging handler;
- AI provider failures, logged explicitly via ``log_event(...)``.

Logging NEVER raises: a broken log must not break the app. A thread-local
guard prevents recursion (a DB warning triggered by the log insert itself
would otherwise loop forever). The table is pruned to the newest
``ERROR_LOG_KEEP`` rows so it cannot grow without bound.
"""

import logging
import threading
import traceback

from flask import current_app, has_request_context, request
from flask_login import current_user

from ..extensions import db
from ..models import ErrorLog

_write_guard = threading.local()


def _request_context():
    """(path, user_id) of the current request, when there is one."""
    if not has_request_context():
        return None, None
    uid = current_user.id if current_user.is_authenticated else None
    return request.path, uid


def log_event(level, source, message, detail=None, path=None, user_id=None):
    """Persist one log row. Safe to call from requests and background threads
    (needs an app context). Never raises."""
    if getattr(_write_guard, "active", False):
        return  # recursion guard: a failure while logging must not re-log
    _write_guard.active = True
    try:
        if path is None and user_id is None:
            path, user_id = _request_context()
        # Clear any dirty state left by the failed operation we are logging —
        # otherwise this commit would silently persist it.
        db.session.rollback()
        row = ErrorLog(level=level, source=(source or "app")[:40],
                       message=(message or "")[:2000], detail=detail,
                       path=path, user_id=user_id)
        db.session.add(row)
        db.session.commit()
        _prune()
    except Exception:  # noqa: BLE001 - logging must never break the app
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logging.getLogger("docindex.errorlog").debug(
            "failed to persist log row", exc_info=True)
    finally:
        _write_guard.active = False


def _prune():
    keep = int(current_app.config.get("ERROR_LOG_KEEP", 2000))
    total = db.session.query(ErrorLog.id).count()
    if total <= keep:
        return
    cutoff = (db.session.query(ErrorLog.id)
              .order_by(ErrorLog.id.desc()).offset(keep).limit(1).scalar())
    if cutoff is not None:
        ErrorLog.query.filter(ErrorLog.id <= cutoff).delete(
            synchronize_session=False)
        db.session.commit()


class DbLogHandler(logging.Handler):
    """logging.Handler that writes WARNING+ records to error_logs.

    Bound to the Flask app's logger; works from background threads too (they
    hold a reference to the real app object)."""

    def __init__(self, app):
        super().__init__(level=logging.WARNING)
        self._app = app

    def emit(self, record):
        if getattr(_write_guard, "active", False):
            return
        try:
            message = record.getMessage()
            detail = None
            if record.exc_info:
                detail = "".join(traceback.format_exception(*record.exc_info))
            source = record.name.rsplit(".", 1)[-1]
            with self._app.app_context():
                log_event(record.levelname.lower(), source, message,
                          detail=detail)
        except Exception:  # noqa: BLE001 - logging must never break the app
            self.handleError(record)


def install(app):
    """Wire DB logging into the app: logger handler + 500 error handler."""
    # app.logger is a shared logging.Logger (keyed by app name), so a handler
    # from a previous app instance (tests create many) would write into a
    # dropped database — replace any stale DbLogHandler with one bound to
    # THIS app.
    app.logger.handlers[:] = [h for h in app.logger.handlers
                              if not isinstance(h, DbLogHandler)]
    app.logger.addHandler(DbLogHandler(app))

    @app.errorhandler(Exception)
    def _log_unhandled(exc):  # noqa: ANN202
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc  # normal 404/405/... are not errors worth logging
        path, user_id = _request_context()
        with app.app_context():
            log_event("error", "http", f"Unhandled exception: {exc}",
                      detail=traceback.format_exc(), path=path,
                      user_id=user_id)
        # Debug/testing keep the old behaviour (interactive debugger /
        # propagated exceptions); production gets a plain logged 500.
        if app.debug or app.testing:
            raise exc
        return ("Internal Server Error — the error was logged and is visible "
                "on the settings logs page.", 500)
