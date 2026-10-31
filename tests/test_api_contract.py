"""API contract tests: frozen JSON shapes for the future React client.

These assert exact envelopes/status codes (not just behavior), so the
vanilla UI and the later React shell can share them as the definition
of done for every JSON twin.
"""

import json

from app.extensions import db


def _json(client, method, url, payload=None, accept_json=True):
    headers = {}
    if accept_json:
        headers["Accept"] = "application/json"
    data = json.dumps(payload) if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    return client.open(url, method=method, headers=headers, data=data)


def test_login_json_success_shape(app, client, user):
    resp = _json(client, "POST", "/login",
                 {"username": "alice", "password": "password123"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["user"]["username"] == "alice"
    assert set(data["user"]) >= {"id", "username", "email", "is_admin"}
    # The session is authenticated afterwards.
    me = _json(client, "GET", "/api/me")
    assert me.status_code == 200
    assert me.get_json()["user"]["username"] == "alice"


def test_login_json_rejects_bad_credentials(app, client, user):
    resp = _json(client, "POST", "/login",
                 {"username": "alice", "password": "nope"})
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Invalid username or password."}


def test_login_form_flow_unchanged(app, client, user):
    resp = client.post("/login", data={"username": "alice",
                                       "password": "password123"})
    assert resp.status_code == 302  # still a redirect, not JSON


def test_register_json_validates(app, client):
    resp = _json(client, "POST", "/register",
                 {"username": "ab", "email": "a@b.com", "password": "x" * 8,
                  "confirm": "x" * 8})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Username must be 3-64 characters."}
    resp = _json(client, "POST", "/register",
                 {"username": "bob", "email": "not-an-email",
                  "password": "x" * 8, "confirm": "x" * 8})
    assert resp.status_code == 400
    assert "email" in resp.get_json()["error"].lower()


def test_register_json_creates_and_logs_in(app, client):
    resp = _json(client, "POST", "/register",
                 {"username": "carol", "email": "carol@x.com",
                  "password": "password123", "confirm": "password123"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    me = _json(client, "GET", "/api/me")
    assert me.get_json()["user"]["username"] == "carol"
    # Duplicate stays a JSON 400, not a flashed form (fresh session —
    # an authenticated client is already inside, like the form flow).
    fresh = app.test_client()
    dup = _json(fresh, "POST", "/register",
                {"username": "carol", "email": "carol@x.com",
                 "password": "password123", "confirm": "password123"})
    assert dup.status_code == 400
    assert "taken" in dup.get_json()["error"]


def test_logout_json_shape(auth_client, app, user):
    resp = _json(auth_client, "POST", "/logout", {})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    me = _json(auth_client, "GET", "/api/me")
    assert me.status_code == 401


def test_api_me_requires_auth(client):
    resp = _json(client, "GET", "/api/me")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Authentication required."}
    # Browsers (HTML accept) still get the login redirect.
    html = client.get("/api/me")
    assert html.status_code == 302
    assert "/login" in html.headers["Location"]


def test_module_guard_json_vs_html(client):
    # Unauthenticated JSON request to a module: 401 envelope...
    resp = _json(client, "GET", "/m/automation/")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Authentication required."}
    # ...while a browser navigation still redirects to login.
    html = client.get("/m/automation/")
    assert html.status_code == 302
    assert "/login" in html.headers["Location"]


def test_module_guard_disabled_stays_404(auth_client, app, user):
    from app.models import ModuleState
    with app.app_context():
        state = db.session.get(ModuleState, "hello")
        if state is None:
            state = ModuleState(name="hello", enabled=False)
            db.session.add(state)
        else:
            state.enabled = False
        db.session.commit()
    resp = _json(auth_client, "GET", "/m/hello/")
    assert resp.status_code == 404


def test_ai_connections_list_shape(auth_client, app, user):
    resp = _json(auth_client, "GET", "/ai/connections")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_search_api_shape(auth_client, app, user):
    resp = _json(auth_client, "GET", "/api/search?q=test")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    if data:
        assert set(data[0]) >= {"file_id", "name", "snippet"}


def test_drives_api_shape(auth_client, app, user):
    resp = _json(auth_client, "GET", "/api/drives")
    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list)
