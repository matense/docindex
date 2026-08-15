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
