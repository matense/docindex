def test_register_and_login(client):
    resp = client.post("/register", data={
        "username": "bob", "email": "bob@example.com",
        "password": "secret123", "confirm": "secret123",
    }, follow_redirects=False)
    assert resp.status_code == 302  # logged in, redirected to drive


def test_register_duplicate_username(client, user):
    resp = client.post("/register", data={
        "username": "alice", "email": "other@example.com",
        "password": "secret123", "confirm": "secret123",
    }, follow_redirects=True)
    assert b"already taken" in resp.data


def test_login_wrong_password(client, user):
    resp = client.post("/login", data={
        "username": "alice", "password": "wrong",
    }, follow_redirects=True)
    assert b"Invalid username or password" in resp.data


def test_drive_requires_login(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def _login_with_next(app, nxt, via):
    from urllib.parse import quote
    client = app.test_client()
    data = {"username": "alice", "password": "password123"}
    if via == "form":
        data["next"] = nxt
        return client.post("/login", data=data, follow_redirects=False)
    return client.post(f"/login?next={quote(nxt, safe='')}",
                      data=data, follow_redirects=False)


def test_login_next_rejects_open_redirects(app, user):
    evil = ["//evil.com", "/\\evil.com", "https://evil.com",
            "http://evil.com/x", "javascript:alert(1)", "data:text/plain,hi"]
    for nxt in evil:
        for via in ("query", "form"):
            r = _login_with_next(app, nxt, via)
            assert r.status_code == 302, (nxt, via)
            loc = r.headers.get("Location", "")
            assert loc == "/search", (nxt, via, loc)


def test_login_next_keeps_safe_relative_path(app, user):
    r = _login_with_next(app, "/settings/profile", "query")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/settings/profile")


def test_register_mismatch_repopulates(client):
    resp = client.post("/register", data={
        "username": "carol", "email": "carol@example.com",
        "password": "secret123", "confirm": "different123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Passwords do not match" in resp.data
    assert b'value="carol"' in resp.data
    assert b'value="carol@example.com"' in resp.data


def test_login_failure_repopulates_username(client, user):
    resp = client.post("/login", data={
        "username": "alice", "password": "wrong",
    }, follow_redirects=True)
    assert b"Invalid username or password" in resp.data
    assert b'value="alice"' in resp.data
