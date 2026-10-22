"""Tests for the profile page, password change and the registration toggle."""

import pytest

from app.extensions import db as _db
from app.models import Setting, User


@pytest.fixture()
def admin(app):
    """Creates an admin user; yields its id."""
    with app.app_context():
        u = User(username="boss", email="boss@example.com", is_admin=True)
        u.set_password("adminpass123")
        _db.session.add(u)
        _db.session.commit()
        yield u.id


@pytest.fixture()
def admin_client(client, admin):
    client.post("/login", data={"username": "boss", "password": "adminpass123"})
    return client


# --- Profile page ---

def test_profile_requires_login(client):
    resp = client.get("/settings/profile")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_profile_page_loads(auth_client):
    resp = auth_client.get("/settings/profile")
    assert resp.status_code == 200
    assert b"alice" in resp.data
    assert b"Change password" in resp.data


def test_profile_hides_admin_section_for_regular_user(auth_client):
    resp = auth_client.get("/settings/profile")
    assert b"New user registration" not in resp.data


def test_profile_shows_admin_section_for_admin(admin_client):
    resp = admin_client.get("/settings/profile")
    assert b"New user registration" in resp.data


def test_profile_lists_drive_stats_with_files(auth_client):
    """Regression: the grouped file-stats query returns 3-column rows —
    the profile crashed turning them straight into a dict (ValueError:
    dictionary update sequence element #0 has length 3)."""
    import io
    auth_client.post("/upload", data={
        "folder_id": "",
        "files": (io.BytesIO(b"hello world"), "notes.txt"),
    }, content_type="multipart/form-data", follow_redirects=True)
    resp = auth_client.get("/settings/profile")
    assert resp.status_code == 200
    assert b"files" in resp.data


def test_profile_groups_drives_by_kind(auth_client, app, tmp_path):
    import io
    # Native drive with a file.
    auth_client.post("/upload", data={
        "folder_id": "",
        "files": (io.BytesIO(b"hello"), "notes.txt"),
    }, content_type="multipart/form-data", follow_redirects=True)
    # Mirror + shared synced drives.
    (tmp_path / "m.txt").write_text("mirror", encoding="utf-8")
    (tmp_path / "s.txt").write_text("shared", encoding="utf-8")
    mirror = tmp_path / "mirror"
    shared = tmp_path / "shared"
    mirror.mkdir()
    shared.mkdir()
    (mirror / "m.txt").write_text("mirror", encoding="utf-8")
    (shared / "s.txt").write_text("shared", encoding="utf-8")
    auth_client.post("/drives/sync-create", data={"path": str(mirror)},
                     follow_redirects=True)
    auth_client.post("/drives/sync-create",
                     data={"path": str(shared), "sync_mode": "shared"},
                     follow_redirects=True)
    html = auth_client.get("/settings/profile").data.decode()
    for section in ("Native drives", "Shared sync drives", "Mirror drives"):
        assert section in html
    # Sections render in native → shared → mirror order.
    assert html.index("Native drives") < html.index("Shared sync drives") \
        < html.index("Mirror drives")


# --- Email / password changes ---

def test_change_email(auth_client, app):
    auth_client.post("/settings/profile/email", data={"email": "new@example.com"})
    with app.app_context():
        assert _db.session.get(User, 1).email == "new@example.com"


def test_change_password_success(auth_client, client):
    auth_client.post("/settings/profile/password", data={
        "current_password": "password123",
        "new_password": "newpass456",
        "confirm_password": "newpass456",
    })
    client.post("/logout")
    resp = client.post("/login", data={"username": "alice", "password": "newpass456"})
    assert resp.status_code == 302  # login worked with the new password


def test_change_password_wrong_current(auth_client, app):
    auth_client.post("/settings/profile/password", data={
        "current_password": "wrong",
        "new_password": "newpass456",
        "confirm_password": "newpass456",
    })
    with app.app_context():
        assert _db.session.get(User, 1).check_password("password123")


def test_change_password_mismatch(auth_client, app):
    auth_client.post("/settings/profile/password", data={
        "current_password": "password123",
        "new_password": "newpass456",
        "confirm_password": "different",
    })
    with app.app_context():
        assert _db.session.get(User, 1).check_password("password123")


# --- Registration toggle ---

def test_registration_enabled_by_default(client):
    assert client.get("/register").status_code == 200


def test_regular_user_cannot_toggle_registration(auth_client, app):
    auth_client.post("/settings/profile/registration", data={})
    with app.app_context():
        assert Setting.get_bool("registration_enabled", True) is True


def test_admin_disables_registration(admin_client, app):
    admin_client.post("/settings/profile/registration", data={})  # unchecked = disabled
    with app.app_context():
        assert Setting.get_bool("registration_enabled", True) is False


def test_register_route_blocked_when_disabled(admin_client, client):
    admin_client.post("/settings/profile/registration", data={})
    client.post("/logout")
    resp = client.get("/register")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    resp = client.post("/register", data={
        "username": "mallory", "email": "m@example.com",
        "password": "x", "confirm": "x",
    })
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_page_hides_register_link_when_disabled(admin_client, client):
    admin_client.post("/settings/profile/registration", data={})
    client.post("/logout")
    resp = client.get("/login")
    assert b"Register" not in resp.data


def test_admin_reenables_registration(admin_client, client, app):
    admin_client.post("/settings/profile/registration", data={})
    admin_client.post("/settings/profile/registration", data={"enabled": "on"})
    with app.app_context():
        assert Setting.get_bool("registration_enabled", True) is True
    client.post("/logout")
    assert client.get("/register").status_code == 200
