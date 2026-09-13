"""Module system: discovery, route guard, tool registry and admin page."""

from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import ModuleState, User
from app.services import agent_service, module_service


def _enable(app, name="hello"):
    with app.app_context():
        db.session.add(ModuleState(name=name, enabled=True))
        db.session.commit()


def _make_admin(app):
    with app.app_context():
        u = User.query.first()
        u.is_admin = True
        db.session.commit()


def test_hello_module_is_discovered(app):
    with app.app_context():
        modules = {m["name"]: m for m in module_service.list_modules()}
    assert "hello" in modules
    assert not modules["hello"].get("error")
    assert modules["hello"]["enabled"] is False


def test_module_route_requires_login(client):
    resp = client.get("/m/hello/")
    assert resp.status_code == 302  # redirected to login


def test_module_route_404s_while_disabled(auth_client):
    assert auth_client.get("/m/hello/").status_code == 404


def test_module_route_serves_when_enabled(app, auth_client):
    _enable(app)
    resp = auth_client.get("/m/hello/")
    assert resp.status_code == 200
    assert b"Hello module" in resp.data


def test_module_tool_hidden_until_enabled(app):
    with app.app_context():
        _defs, handlers, _labels = agent_service._active_tools()
        assert "hello.echo" not in handlers

        db.session.add(ModuleState(name="hello", enabled=True))
        db.session.commit()

        defs, handlers, _labels = agent_service._active_tools()
        assert "hello.echo" in handlers
        assert any(d["function"]["name"] == "hello.echo" for d in defs)
        assert handlers["hello.echo"](None, {"text": "hi"}) == {"echo": "hi"}


def test_agent_runs_module_tool_when_enabled(app, user):
    _enable(app)
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "hello.echo", "arguments": '{"text": "ping"}'},
        }]},
        {"role": "assistant", "content": "Echoed ping."},
    ])
    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(responses)):
            answer, steps = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "echo ping"}])
    assert "ping" in answer
    assert [s["label"] for s in steps] == ["Echoed"]


def test_tool_name_collision_rejected():
    with pytest.raises(ValueError):
        agent_service.register_tool("search_files", {}, lambda u, a, d: {})


def test_modules_page_requires_admin(auth_client):
    assert auth_client.get("/settings/modules").status_code == 403


def test_modules_page_lists_modules(app, auth_client, user):
    _make_admin(app)
    resp = auth_client.get("/settings/modules")
    assert resp.status_code == 200
    assert b"hello" in resp.data


def test_module_toggle_requires_admin(auth_client):
    resp = auth_client.post("/settings/modules/hello/toggle",
                            data={"enabled": "1"})
    assert resp.status_code == 403


def test_module_toggle_persists(app, auth_client, user):
    _make_admin(app)
    resp = auth_client.post("/settings/modules/hello/toggle",
                            data={"enabled": "1"}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert ModuleState.is_enabled("hello") is True

    auth_client.post("/settings/modules/hello/toggle", data={"enabled": "0"})
    with app.app_context():
        assert ModuleState.is_enabled("hello") is False


def test_invalid_module_is_skipped(app, tmp_path):
    bad = tmp_path / "Bad-Name"
    bad.mkdir()
    (bad / "module.json").write_text('{"name": "Bad-Name"}')
    (bad / "__init__.py").write_text("")
    original = app.config["MODULES_FOLDER"]
    app.config["MODULES_FOLDER"] = str(tmp_path)
    try:
        with app.app_context():
            found = module_service.discover(app)
        assert found["Bad-Name"]["error"]
    finally:
        # Restore the global registry for later tests.
        app.config["MODULES_FOLDER"] = original
        module_service.discover(app)
