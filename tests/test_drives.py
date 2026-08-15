import io

from app.extensions import db
from app.models import Drive, StoredFile


def _upload(client, name, content):
    return client.post("/upload", data={
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def test_default_drive_created_lazily_and_orphans_assigned(auth_client, app):
    _upload(auth_client, "notes.txt", b"hello drive")
    auth_client.get("/")
    with app.app_context():
        drive = Drive.query.filter_by(name="Personal").one()
        stored = StoredFile.query.filter_by(name="notes.txt").one()
        assert stored.drive_id == drive.id


def test_create_and_switch_drives_scopes_files(auth_client, app):
    # File goes to the default "Personal" drive
    _upload(auth_client, "personal.txt", b"personal stuff")

    # Create and switch to a new drive
    resp = auth_client.post("/drives/create", data={"name": "Work"},
                            follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        work = Drive.query.filter_by(name="Work").one()

    # Uploads land in the current drive
    _upload(auth_client, "work.txt", b"work stuff")
    with app.app_context():
        assert StoredFile.query.filter_by(name="work.txt").one().drive_id == work.id

    # Drive page only shows the current drive's files
    resp = auth_client.get("/")
    assert b"work.txt" in resp.data
    assert b"personal.txt" not in resp.data

    # Switch back to Personal
    with app.app_context():
        personal = Drive.query.filter_by(name="Personal").one()
    resp = auth_client.post(f"/drives/{personal.id}/select", follow_redirects=True)
    assert resp.status_code == 200
    resp = auth_client.get("/")
    assert b"personal.txt" in resp.data
    assert b"work.txt" not in resp.data


def test_search_scoped_to_current_drive(auth_client, app):
    _upload(auth_client, "alpha.txt", b"sharedkeyword alpha")
    auth_client.post("/drives/create", data={"name": "Work"}, follow_redirects=True)
    _upload(auth_client, "beta.txt", b"sharedkeyword beta")

    # Current drive is Work: only beta matches
    resp = auth_client.get("/search?q=sharedkeyword")
    assert b"beta.txt" in resp.data
    assert b"alpha.txt" not in resp.data

    # JSON endpoint is scoped too
    data = auth_client.get("/api/search?q=sharedkeyword").get_json()
    assert [r["name"] for r in data] == ["beta.txt"]


def test_cannot_select_other_users_drive(auth_client, app):
    auth_client.get("/")  # creates alice's Personal drive
    with app.app_context():
        drive_id = Drive.query.one().id

    other = app.test_client()
    other.post("/register", data={
        "username": "dave", "email": "dave@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    resp = other.post(f"/drives/{drive_id}/select", follow_redirects=True)
    assert resp.status_code == 404


def test_duplicate_drive_name_rejected(auth_client, app):
    auth_client.post("/drives/create", data={"name": "Work"}, follow_redirects=True)
    auth_client.post("/drives/create", data={"name": "work"}, follow_redirects=True)
    with app.app_context():
        assert Drive.query.filter(Drive.name.ilike("work")).count() == 1


def test_edit_drive_name_and_description(auth_client, app):
    auth_client.get("/")  # creates Personal
    with app.app_context():
        drive_id = Drive.query.one().id

    resp = auth_client.post(f"/drives/{drive_id}/edit", data={
        "name": "Personal Vault",
        "description": "My private documents",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        drive = db.session.get(Drive, drive_id)
        assert drive.name == "Personal Vault"
        assert drive.description == "My private documents"

    # Description shows on the drive page
    resp = auth_client.get("/")
    assert b"My private documents" in resp.data

    # Another user cannot edit it
    other = app.test_client()
    other.post("/register", data={
        "username": "dave", "email": "dave@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    other.post(f"/drives/{drive_id}/edit", data={"name": "hacked"},
               follow_redirects=True)
    with app.app_context():
        assert db.session.get(Drive, drive_id).name == "Personal Vault"


def test_view_modes(auth_client, app):
    _upload(auth_client, "root-file.txt", b"root content")
    auth_client.post("/folder/create", data={"name": "Docs"}, follow_redirects=True)
    with app.app_context():
        from app.models import Folder
        folder_id = Folder.query.filter_by(name="Docs").one().id
    auth_client.post("/upload", data={
        "folder_id": str(folder_id),
        "files": (io.BytesIO(b"nested content"), "nested.txt"),
    }, content_type="multipart/form-data", follow_redirects=True)

    # Grid (default)
    resp = auth_client.get("/?view=grid")
    assert b"root-file.txt" in resp.data and b"Docs" in resp.data

    # Detailed list
    resp = auth_client.get("/?view=list")
    assert b"root-file.txt" in resp.data
    assert b"<th" in resp.data  # table header

    # Tree: shows the whole drive, including files inside folders
    resp = auth_client.get("/?view=tree")
    assert b"root-file.txt" in resp.data
    assert b"Docs" in resp.data
    assert b"nested.txt" in resp.data

    # Tree shows everything even when browsing inside a folder
    resp = auth_client.get(f"/folder/{folder_id}?view=tree")
    assert b"root-file.txt" in resp.data


def test_view_choice_persists(auth_client, app):
    _upload(auth_client, "persist.txt", b"x")
    auth_client.get("/?view=list")
    resp = auth_client.get("/")
    assert b"<th" in resp.data  # list mode stuck
    auth_client.get("/?view=grid")
    resp = auth_client.get("/")
    assert b"<th" not in resp.data


def test_agent_scoped_to_drive(app, user, auth_client):
    """The AI agent only sees files in the current drive."""
    _upload(auth_client, "visible.txt", b"the password is swordfish")
    auth_client.post("/drives/create", data={"name": "Other"}, follow_redirects=True)
    _upload(auth_client, "hidden.txt", b"the secret is 42")

    from unittest.mock import patch
    from app.models import User as UserModel
    from app.services import agent_service

    with app.app_context():
        user_obj = db.session.get(UserModel, user)
        other_drive = Drive.query.filter_by(name="Other").one()
        visible = StoredFile.query.filter_by(name="visible.txt").one()

        calls = []

        def fake_completion(messages, tools=None, config=None):
            # First call: ask to search; second: answer with what was found
            if not calls:
                calls.append(1)
                return {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "search_files",
                                 "arguments": '{"query": "password secret"}'},
                }]}
            return {"role": "assistant", "content": "done"}

        with patch("app.services.ai_service.chat_completion",
                   side_effect=fake_completion):
            answer, steps = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "find secrets"}],
                drive=other_drive)

    assert steps[0]["label"] == "Searched files"
    # The search ran inside "Other" drive: visible.txt must not appear
    # (the tool result summary would list it otherwise)
    assert "visible.txt" not in str(steps)
