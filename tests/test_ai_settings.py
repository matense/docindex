from unittest.mock import patch

from app.extensions import db
from app.models import AIConnection, User


def _add_conn(client, name="Test Conn", base_url="https://api.openai.com/v1",
              model="gpt-4o-mini", vision="", active=True):
    return client.post("/settings/ai/add", data={
        "name": name, "base_url": base_url, "api_key": "sk-test",
        "model": model, "vision_model": vision,
        "is_active": "on" if active else "",
    }, follow_redirects=True)


def test_add_connection(auth_client, app):
    resp = _add_conn(auth_client)
    assert resp.status_code == 200
    with app.app_context():
        conn = AIConnection.query.one()
        assert conn.name == "Test Conn"
        assert conn.is_active  # first connection auto-activates


def test_only_one_active(auth_client, app):
    _add_conn(auth_client, name="A")
    _add_conn(auth_client, name="B")
    with app.app_context():
        active = AIConnection.query.filter_by(is_active=True).all()
        assert len(active) == 1
        assert active[0].name == "B"


def test_activate_and_delete(auth_client, app):
    _add_conn(auth_client, name="A")
    _add_conn(auth_client, name="B", active=False)
    with app.app_context():
        a = AIConnection.query.filter_by(name="A").one()
        b = AIConnection.query.filter_by(name="B").one()
        aid, bid = a.id, b.id
    auth_client.post(f"/settings/ai/{aid}/activate", follow_redirects=True)
    with app.app_context():
        assert db.session.get(AIConnection, aid).is_active
        assert not db.session.get(AIConnection, bid).is_active
    auth_client.post(f"/settings/ai/{bid}/delete", follow_redirects=True)
    with app.app_context():
        assert AIConnection.query.count() == 1


def test_connection_isolation(auth_client, app):
    _add_conn(auth_client, name="Mine")
    with app.app_context():
        cid = AIConnection.query.one().id
    other = app.test_client()
    other.post("/register", data={
        "username": "erin", "email": "erin@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    resp = other.post(f"/settings/ai/{cid}/delete", follow_redirects=True)
    with app.app_context():
        assert AIConnection.query.count() == 1  # untouched


def test_user_connection_overrides_env(auth_client, app, user):
    _add_conn(auth_client, name="Custom", base_url="http://my-llm.local/v1",
              model="my-model")
    from app.services import ai_service
    with app.app_context():
        user_obj = db.session.get(User, user)
        cfg = ai_service.config_for(user_obj)
        assert cfg["base_url"] == "http://my-llm.local/v1"
        assert cfg["model"] == "my-model"
        assert cfg["enabled"] is True


def test_env_fallback_when_no_connection(app, user):
    from app.services import ai_service
    app.config["AI_ENABLED"] = True
    app.config["AI_BASE_URL"] = "http://env-llm/v1"
    app.config["AI_MODEL"] = "env-model"
    with app.app_context():
        user_obj = db.session.get(User, user)
        cfg = ai_service.config_for(user_obj)
        assert cfg["base_url"] == "http://env-llm/v1"


def test_test_connection_endpoint(auth_client):
    with patch("app.services.ai_service.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"data": [{"id": "gpt-4o-mini"}]}
        resp = auth_client.post("/settings/ai/test", json={
            "base_url": "https://api.openai.com/v1", "api_key": "sk-x",
        })
    data = resp.get_json()
    assert data["ok"] is True
    assert "gpt-4o-mini" in data["message"]


def test_chat_uses_user_connection(auth_client, app, user):
    _add_conn(auth_client, name="Custom", base_url="http://my-llm.local/v1",
              model="my-model")
    captured = {}

    def fake_completion(messages, model=None, tools=None, tool_choice=None, config=None):
        captured["config"] = config
        return {"role": "assistant", "content": "answer"}

    with patch("app.services.ai_service.chat_completion", side_effect=fake_completion):
        resp = auth_client.post("/ai/chat", json={"message": "hi"})
        resp.data  # consume the stream inside the patch

    assert resp.status_code == 200
    assert captured["config"]["base_url"] == "http://my-llm.local/v1"
    assert captured["config"]["model"] == "my-model"


def test_edit_connection(auth_client, app):
    _add_conn(auth_client, name="Old", base_url="http://old.local/v1", model="old-model")
    with app.app_context():
        cid = AIConnection.query.one().id

    resp = auth_client.get(f"/settings/ai?edit={cid}")
    assert b'value="Old"' in resp.data
    assert b'value="http://old.local/v1"' in resp.data

    auth_client.post(f"/settings/ai/{cid}/edit", data={
        "name": "New", "base_url": "http://new.local/v1", "api_key": "",
        "model": "new-model", "vision_model": "new-vision",
    }, follow_redirects=True)
    with app.app_context():
        conn = db.session.get(AIConnection, cid)
        assert conn.name == "New"
        assert conn.base_url == "http://new.local/v1"
        assert conn.model == "new-model"
        assert conn.vision_model == "new-vision"
        assert conn.api_key == "sk-test"  # unchanged: empty key keeps the old one


def test_cannot_edit_other_users_connection(auth_client, app):
    _add_conn(auth_client, name="Mine")
    with app.app_context():
        cid = AIConnection.query.one().id
    other = app.test_client()
    other.post("/register", data={
        "username": "fred", "email": "fred@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    other.post(f"/settings/ai/{cid}/edit", data={
        "name": "Hacked", "base_url": "http://evil/v1", "model": "x",
    }, follow_redirects=True)
    with app.app_context():
        assert db.session.get(AIConnection, cid).name == "Mine"


def test_test_existing_connection(auth_client, app):
    _add_conn(auth_client, name="A", base_url="http://my-llm/v1")
    with app.app_context():
        cid = AIConnection.query.one().id
    with patch("app.services.ai_service.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"data": [{"id": "m1"}]}
        resp = auth_client.post(f"/settings/ai/{cid}/test")
    assert resp.get_json()["ok"] is True
    # used the stored base_url and key
    called_url = mock_get.call_args[0][0]
    assert called_url == "http://my-llm/v1/models"


def test_models_endpoint(auth_client, app):
    with patch("app.services.ai_service.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "data": [{"id": "llama3.1"}, {"id": "llava"}]
        }
        resp = auth_client.post("/settings/ai/models", json={
            "base_url": "http://localhost:11434/v1", "api_key": "",
        })
    data = resp.get_json()
    assert data["ok"] is True
    assert data["models"] == ["llama3.1", "llava"]


def test_models_endpoint_uses_stored_key_when_editing(auth_client, app):
    _add_conn(auth_client, name="A", base_url="http://my-llm/v1")
    with app.app_context():
        cid = AIConnection.query.one().id
    with patch("app.services.ai_service.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"data": [{"id": "m1"}]}
        resp = auth_client.post("/settings/ai/models", json={
            "base_url": "http://my-llm/v1", "api_key": "", "conn_id": cid,
        })
    assert resp.get_json()["ok"] is True
    headers = mock_get.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer sk-test"


def test_chat_model_switcher_endpoints(auth_client, app):
    _add_conn(auth_client, name="A", model="model-a")
    _add_conn(auth_client, name="B", model="model-b", active=False)
    with app.app_context():
        bid = AIConnection.query.filter_by(name="B").one().id

    conns = auth_client.get("/ai/connections").get_json()
    assert [(c["name"], c["model"], c["is_active"]) for c in conns] == [
        ("A", "model-a", True), ("B", "model-b", False)]

    resp = auth_client.post(f"/ai/connections/{bid}/activate")
    assert resp.status_code == 200
    assert resp.get_json()["model"] == "model-b"

    conns = auth_client.get("/ai/connections").get_json()
    assert [(c["name"], c["is_active"]) for c in conns] == [("A", False), ("B", True)]


def test_switcher_isolated_between_users(auth_client, app):
    _add_conn(auth_client, name="Mine")
    with app.app_context():
        cid = AIConnection.query.one().id
    other = app.test_client()
    other.post("/register", data={
        "username": "erin", "email": "erin@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    assert other.get("/ai/connections").get_json() == []
    assert other.post(f"/ai/connections/{cid}/activate").status_code == 404
