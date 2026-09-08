from app.extensions import db
from app.models import User


def test_theme_defaults_to_light(auth_client, app, user):
    with app.app_context():
        assert db.session.get(User, user).theme == "light"
    resp = auth_client.get("/settings/profile")
    assert b'data-theme="light"' in resp.data


def test_theme_switch_to_dark_persists(auth_client, app, user):
    resp = auth_client.post("/settings/profile/theme", data={"theme": "dark"},
                            follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(User, user).theme == "dark"
    # The rendered shell now carries the dark theme attribute.
    resp = auth_client.get("/settings/profile")
    assert b'data-theme="dark"' in resp.data
    # ...and the Dark option shows as active on the profile page.
    assert b"fa-moon" in resp.data


def test_theme_invalid_value_falls_back_to_light(auth_client, app, user):
    auth_client.post("/settings/profile/theme", data={"theme": "neon"},
                     follow_redirects=True)
    with app.app_context():
        assert db.session.get(User, user).theme == "light"


def test_theme_is_per_user(auth_client, app, user):
    # Switch alice to dark, then register a second user: still light.
    auth_client.post("/settings/profile/theme", data={"theme": "dark"})
    other = app.test_client()
    other.post("/register", data={
        "username": "bob", "email": "bob@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    with app.app_context():
        assert User.query.filter_by(username="bob").one().theme == "light"
    resp = other.get("/settings/profile")
    assert b'data-theme="light"' in resp.data


def test_login_page_always_light(client):
    # Unauthenticated visitors (login page) have no preference stored.
    resp = client.get("/login")
    assert b'data-theme="light"' in resp.data
