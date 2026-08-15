import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.models import User  # noqa: E402
from config import TestConfig  # noqa: E402


@pytest.fixture()
def app():
    # NOTE: the app context must NOT stay pushed while test clients make
    # requests — Flask test clients would then share session state.
    app = create_app(TestConfig)
    with app.app_context():
        _db.create_all()
    yield app
    with app.app_context():
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def user(app):
    """Creates the default user; yields its id (fetch the object in a context)."""
    with app.app_context():
        u = User(username="alice", email="alice@example.com")
        u.set_password("password123")
        _db.session.add(u)
        _db.session.commit()
        uid = u.id
    yield uid


@pytest.fixture()
def auth_client(client, user):
    client.post("/login", data={"username": "alice", "password": "password123"})
    return client
