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


# ---------------------------------------------------------------------------
# Drive trash: soft-delete, restore, purge, 30-day auto-purge
# ---------------------------------------------------------------------------

def _work_drive_id(app, user):
    from app.models import Drive as _Drive
    with app.app_context():
        return _Drive.query.filter_by(user_id=user, name="Work").one().id


def test_delete_drive_trashes_and_restores(auth_client, app, user):
    _upload(auth_client, "personal.txt", b"personal stuff")
    auth_client.post("/drives/create", data={"name": "Work"},
                     follow_redirects=True)
    _upload(auth_client, "w2.txt", b"more work")
    wid = _work_drive_id(app, user)

    resp = auth_client.post(f"/drives/{wid}/delete", follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        from app.models import Drive as _Drive, StoredFile as _SF
        drive = _Drive.query.get(wid)
        assert drive.deleted_at is not None
        # Every file in the drive goes to the trash with it (same stamp).
        trashed = _SF.query.filter_by(drive_id=wid).one()
        assert trashed.deleted_at == drive.deleted_at

    # Trashed drive disappears from listings; current drive falls back.
    assert b"w2.txt" not in auth_client.get("/").data
    resp = auth_client.get("/settings/trash")
    assert b"Work" in resp.data
    assert auth_client.post(f"/drives/{wid}/select").status_code == 404

    auth_client.post(f"/drives/{wid}/restore", follow_redirects=True)
    with app.app_context():
        from app.models import Drive as _Drive2, StoredFile as _SF2
        assert _Drive2.query.get(wid).deleted_at is None
        # Joint restore: the drive's files come back too.
        assert _SF2.query.filter_by(drive_id=wid).one().deleted_at is None
    auth_client.post(f"/drives/{wid}/select", follow_redirects=True)
    assert b"w2.txt" in auth_client.get("/").data


def test_restore_file_with_trashed_drive_asks_where(auth_client, app, user):
    from app.models import Drive as _Drive
    _upload(auth_client, "homeless.txt", b"data")
    with app.app_context():
        fid = StoredFile.query.filter_by(name="homeless.txt").one().id
        personal = _Drive.query.filter_by(name="Personal").one().id
    auth_client.post(f"/drives/{personal}/delete", follow_redirects=True)

    # Direct POST (the old silent path) now shows the choice instead.
    resp = auth_client.post(f"/file/{fid}/restore")
    assert resp.status_code == 200
    assert b"also in the trash" in resp.data
    with app.app_context():
        assert db.session.get(StoredFile, fid).deleted_at is not None

    # Choice page lists the original drive and the other-drive form.
    resp = auth_client.get(f"/file/{fid}/restore")
    assert resp.status_code == 200
    assert b"Restore the original drive too" in resp.data
    assert b"Put it in another drive" in resp.data


def test_restore_file_with_original_drive(auth_client, app, user):
    from app.models import Drive as _Drive
    _upload(auth_client, "homeless.txt", b"data")
    with app.app_context():
        fid = StoredFile.query.filter_by(name="homeless.txt").one().id
        personal = _Drive.query.filter_by(name="Personal").one().id
    auth_client.post(f"/drives/{personal}/delete", follow_redirects=True)
    resp = auth_client.post(f"/file/{fid}/restore", data={"mode": "original"},
                            follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert stored.deleted_at is None
        assert db.session.get(_Drive, personal).deleted_at is None
        assert stored.drive_id == personal


def test_restore_file_to_another_drive(auth_client, app, user):
    from app.models import Drive as _Drive
    _upload(auth_client, "homeless.txt", b"data")
    auth_client.post("/drives/create", data={"name": "Refuge"},
                     follow_redirects=True)
    with app.app_context():
        fid = StoredFile.query.filter_by(name="homeless.txt").one().id
        personal = _Drive.query.filter_by(name="Personal").one().id
        refuge = _Drive.query.filter_by(name="Refuge").one().id
    auth_client.post(f"/drives/{personal}/delete", follow_redirects=True)
    resp = auth_client.post(f"/file/{fid}/restore",
                            data={"mode": "other",
                                  "drive_id": str(refuge)},
                            follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert stored.deleted_at is None
        assert stored.drive_id == refuge
        # Original drive stays in the trash.
        assert db.session.get(_Drive, personal).deleted_at is not None
    # Invalid destination changes nothing.
    _upload(auth_client, "stuck.txt", b"data")
    with app.app_context():
        fid2 = StoredFile.query.filter_by(name="stuck.txt").one().id
    auth_client.post(f"/drives/{refuge}/delete", follow_redirects=True)
    resp = auth_client.post(f"/file/{fid2}/restore",
                            data={"mode": "other", "drive_id": "999999"},
                            follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(StoredFile, fid2).deleted_at is not None


def test_restore_drive_keeps_previously_trashed_files(auth_client, app, user):
    from app.models import Drive as _Drive, StoredFile as _SF
    auth_client.post("/drives/create", data={"name": "Work"},
                     follow_redirects=True)
    _upload(auth_client, "doomed.txt", b"doomed")
    _upload(auth_client, "kept.txt", b"kept")
    wid = _work_drive_id(app, user)
    with app.app_context():
        doomed = _SF.query.filter_by(drive_id=wid, name="doomed.txt").one()
    # Trash one file on its own first.
    auth_client.post("/selection/delete", data={
        "file_ids": str(doomed.id), "folder_ids": "",
    })
    auth_client.post(f"/drives/{wid}/delete", follow_redirects=True)
    auth_client.post(f"/drives/{wid}/restore", follow_redirects=True)
    with app.app_context():
        assert _Drive.query.get(wid).deleted_at is None
        # The file trashed with the drive is back; the earlier one is not.
        assert _SF.query.filter_by(drive_id=wid,
                                   name="kept.txt").one().deleted_at is None
        assert _SF.query.filter_by(drive_id=wid,
                                   name="doomed.txt").one().deleted_at is not None


def test_purge_drive_removes_everything(auth_client, app, user):
    import os
    auth_client.post("/drives/create", data={"name": "Work"},
                     follow_redirects=True)
    _upload(auth_client, "w.txt", b"work")
    wid = _work_drive_id(app, user)
    with app.app_context():
        from app.models import StoredFile as _SF
        from app.services import file_service as _fs
        stored = _SF.query.filter_by(drive_id=wid).one()
        blob = _fs.file_path(stored)
        assert os.path.exists(blob)
    auth_client.post(f"/drives/{wid}/delete", follow_redirects=True)
    auth_client.post(f"/drives/{wid}/purge", follow_redirects=True)
    with app.app_context():
        from app.models import Drive as _Drive, FileIndex as _FI, StoredFile as _SF2
        assert _Drive.query.get(wid) is None
        assert _SF2.query.filter_by(drive_id=wid).count() == 0
        assert _FI.query.count() == 0
    assert not os.path.exists(blob)


def test_synced_drive_delete_removes_immediately(auth_client, app, user):
    from app.models import Drive as _Drive
    with app.app_context():
        _Drive.query.filter_by(user_id=user).delete()
        synced = _Drive(name="RO", user_id=user, source_path="/tmp/ro-test")
        db.session.add(synced)
        db.session.commit()
        sid = synced.id
    auth_client.post(f"/drives/{sid}/delete", follow_redirects=True)
    with app.app_context():
        # Row gone (no trash for synced); SQLite may reuse the id for the
        # Personal drive recreated lazily afterwards — match by name.
        assert _Drive.query.filter_by(name="RO").count() == 0


def test_trash_auto_purge_toggle_and_30_day_rule(auth_client, app, user):
    from datetime import datetime, timedelta
    from app.models import Drive as _Drive, StoredFile as _SF, User as _U
    from app.services import trash_service

    _upload(auth_client, "old.txt", b"old")
    _upload(auth_client, "fresh.txt", b"fresh")
    auth_client.post("/drives/create", data={"name": "Work"},
                     follow_redirects=True)
    _upload(auth_client, "w.txt", b"work")
    wid = _work_drive_id(app, user)
    old = datetime.utcnow() - timedelta(days=31)
    recent = datetime.utcnow() - timedelta(days=10)
    with app.app_context():
        user_obj = db.session.get(_U, user)
        assert user_obj.trash_auto_purge is False  # default off
        db.session.query(_SF).filter_by(name="old.txt").update(
            {"deleted_at": old})
        db.session.query(_SF).filter_by(name="fresh.txt").update(
            {"deleted_at": recent})
        db.session.get(_Drive, wid).deleted_at = old
        db.session.commit()

    # Toggle off (default): nothing is purged.
    assert trash_service.purge_expired(app) == (0, 0)

    # Toggle on via the trash page.
    resp = auth_client.post("/settings/trash/settings",
                            data={"trash_auto_purge": "on"},
                            follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(_U, user).trash_auto_purge is True

    # Recent trash survives; 31-day-old trash is purged.
    files, drives = trash_service.purge_expired(app)
    assert (files, drives) == (1, 1)
    with app.app_context():
        assert _SF.query.filter_by(name="old.txt").count() == 0
        assert _SF.query.filter_by(name="fresh.txt").count() == 1
        assert _Drive.query.get(wid) is None

    # Toggle back off.
    auth_client.post("/settings/trash/settings", data={},
                     follow_redirects=True)
    with app.app_context():
        assert db.session.get(_U, user).trash_auto_purge is False
