"""Central error log: capture (service + handler + 500s), page and admin access."""

import logging
from unittest.mock import patch

from app.extensions import db
from app.models import ErrorLog, User
from app.services import agent_service, ai_service, log_service


def _make_admin(app):
    with app.app_context():
        u = db.session.get(User, 1)  # the `user` fixture creates id 1 first
        if u is None:
            u = User.query.first()
        u.is_admin = True
        db.session.commit()


def test_log_event_persists_row(app):
    with app.app_context():
        log_service.log_event("error", "indexing", "boom", detail="trace…")
        row = ErrorLog.query.one()
        assert row.level == "error"
        assert row.source == "indexing"
        assert row.message == "boom"
        assert row.detail == "trace…"


def test_log_event_never_raises(app):
    with app.app_context():
        with patch("app.services.log_service.db.session.commit",
                   side_effect=RuntimeError("db down")):
            log_service.log_event("error", "app", "still fine")  # no raise
        assert ErrorLog.query.count() == 0  # rolled back cleanly


def test_db_log_handler_captures_warnings(app, auth_client):
    with app.app_context():
        app.logger.warning("OCR failed for image 65")
        row = ErrorLog.query.filter_by(level="warning").one()
        assert "OCR failed" in row.message


def test_unhandled_exception_is_logged(app, client):
    @app.route("/_boom")
    def _boom():
        raise RuntimeError("kaboom")

    # TestConfig: TESTING=True propagates the exception (old behaviour kept),
    # but it was logged first.
    try:
        client.get("/_boom")
    except RuntimeError:
        pass
    with app.app_context():
        row = ErrorLog.query.filter_by(source="http").one()
        assert "kaboom" in row.message
        assert row.path == "/_boom"
        assert "RuntimeError" in row.detail


def test_ai_chat_error_is_logged(auth_client, app):
    app.config["AI_ENABLED"] = True
    with patch("app.services.agent_service.run_agent_events",
               side_effect=ai_service.AIError("provider exploded")):
        resp = auth_client.post("/ai/chat", json={"message": "hello"})
    assert resp.status_code == 200  # error goes out as an NDJSON event
    assert b"provider exploded" in resp.data
    with app.app_context():
        row = ErrorLog.query.filter_by(source="ai_chat").one()
        assert "provider exploded" in row.message
        assert row.level == "error"


def test_pruning_keeps_newest_rows(app):
    app.config["ERROR_LOG_KEEP"] = 10
    with app.app_context():
        for i in range(25):
            log_service.log_event("warning", "sync", f"entry {i}")
        messages = [r.message for r in ErrorLog.query
                    .order_by(ErrorLog.id).all()]
        assert len(messages) == 10
        assert messages[-1] == "entry 24"  # oldest were pruned


def test_logs_page_requires_admin(auth_client, app):
    assert auth_client.get("/settings/logs").status_code == 403


def test_logs_page_lists_and_filters(auth_client, app):
    _make_admin(app)
    with app.app_context():
        log_service.log_event("error", "ai_chat", "the sky is falling")
        log_service.log_event("warning", "indexing", "minor hiccup")
    resp = auth_client.get("/settings/logs")
    assert resp.status_code == 200
    assert b"the sky is falling" in resp.data
    assert b"minor hiccup" in resp.data
    resp = auth_client.get("/settings/logs?level=error")
    assert b"the sky is falling" in resp.data
    assert b"minor hiccup" not in resp.data
    resp = auth_client.get("/settings/logs?source=indexing")
    assert b"minor hiccup" in resp.data
    assert b"the sky is falling" not in resp.data
    resp = auth_client.get("/settings/logs?q=sky")
    assert b"the sky is falling" in resp.data
    assert b"minor hiccup" not in resp.data


def test_logs_export_csv(auth_client, app):
    _make_admin(app)
    with app.app_context():
        log_service.log_event("error", "ai_chat", "csv, with comma",
                              detail="line1\nline2")
        log_service.log_event("warning", "indexing", "just a warning")
    resp = auth_client.get("/settings/logs/export")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    assert "attachment" in resp.headers["Content-Disposition"]
    import csv as _csv
    import io as _io
    rows = list(_csv.reader(_io.StringIO(resp.get_data(as_text=True))))
    assert rows[0] == ["id", "created_at", "level", "source", "path",
                       "user_id", "message", "detail"]
    assert len(rows) == 3  # header + 2 entries, newest first
    assert rows[1][2] == "warning"
    assert rows[2][2] == "error" and rows[2][6] == "csv, with comma"
    assert rows[2][7] == "line1\nline2"  # multi-line detail survives

    # Filters are honored: export only errors.
    resp = auth_client.get("/settings/logs/export?level=error")
    rows = list(_csv.reader(_io.StringIO(resp.get_data(as_text=True))))
    assert len(rows) == 2
    assert rows[1][6] == "csv, with comma"


def test_logs_export_requires_admin(auth_client, app):
    assert auth_client.get("/settings/logs/export").status_code == 403


def test_logs_clear(auth_client, app):
    _make_admin(app)
    with app.app_context():
        log_service.log_event("error", "app", "to be cleared")
    resp = auth_client.post("/settings/logs/clear", follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert ErrorLog.query.count() == 0


def test_profile_shows_logs_card_for_admin(auth_client, app):
    resp = auth_client.get("/settings/profile")
    assert b"/settings/logs" not in resp.data  # non-admin: hidden
    _make_admin(app)
    with app.app_context():
        log_service.log_event("error", "app", "one error")
    resp = auth_client.get("/settings/profile")
    assert b"/settings/logs" in resp.data
    assert b"1 error" in resp.data


def test_logging_recursion_guard(app):
    """A DB failure inside the log insert must not recurse forever."""
    with app.app_context():
        handler = log_service.DbLogHandler(app)
        record = logging.LogRecord("app.services.indexing_service",
                                   logging.WARNING, __file__, 1,
                                   "guarded warning", None, None)
        with patch("app.services.log_service.db.session.add",
                   side_effect=RuntimeError("insert failed")):
            handler.emit(record)  # must not raise or hang
        assert ErrorLog.query.count() == 0
