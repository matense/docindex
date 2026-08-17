"""Tests for the trash bin: soft-delete, restore, purge and empty trash."""

import io
import os

from app.extensions import db
from app.models import StoredFile
from app.services import agent_service, file_service, search_service


def _upload(client, name, content, folder_id=""):
    return client.post("/upload", data={
        "folder_id": folder_id,
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def _file_id(app, name="notes.txt"):
    with app.app_context():
        return StoredFile.query.filter_by(name=name).one().id


def _get(app, file_id):
    with app.app_context():
        return db.session.get(StoredFile, file_id)


def _blob_path(app, file_id):
    with app.app_context():
        return file_service.file_path(db.session.get(StoredFile, file_id))


def test_delete_is_soft_and_keeps_blob(auth_client, app):
    _upload(auth_client, "notes.txt", b"some content")
    fid = _file_id(app)
    path = _blob_path(app, fid)

    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)

    stored = _get(app, fid)
    assert stored is not None
    assert stored.deleted_at is not None
    assert os.path.exists(path)


def test_trashed_file_hidden_from_drive_and_routes(auth_client, app):
    _upload(auth_client, "notes.txt", b"some content")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)

    resp = auth_client.get("/")
    assert b"notes.txt" not in resp.data
    assert auth_client.get(f"/file/{fid}/view").status_code == 404
    assert auth_client.get(f"/file/{fid}/download").status_code == 404


def test_trashed_file_hidden_from_search_and_agent(auth_client, app):
    _upload(auth_client, "notes.txt", b"needle haystack uniqueword")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)

    with app.app_context():
        user = db.session.get(StoredFile, fid).owner
        assert search_service.search_files("uniqueword", user) == []
        assert agent_service._tool_list_files(user)["files"] == []
        assert "error" in agent_service._tool_read_file(user, fid)


def test_restore_brings_file_back(auth_client, app):
    _upload(auth_client, "notes.txt", b"some content")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)
    auth_client.post(f"/file/{fid}/restore", follow_redirects=True)

    stored = _get(app, fid)
    assert stored.deleted_at is None
    resp = auth_client.get("/")
    assert b"notes.txt" in resp.data
    assert auth_client.get(f"/file/{fid}/download").status_code == 200


def test_purge_removes_row_blob_and_versions(auth_client, app):
    _upload(auth_client, "notes.txt", b"version one")
    _upload(auth_client, "notes.txt", b"version two")
    fid = _file_id(app)
    path = _blob_path(app, fid)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        version_paths = [file_service.version_path(v) for v in stored.versions]
    assert version_paths and all(os.path.exists(p) for p in version_paths)

    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)
    auth_client.post(f"/file/{fid}/purge", follow_redirects=True)

    assert _get(app, fid) is None
    assert not os.path.exists(path)
    assert not any(os.path.exists(p) for p in version_paths)


def test_empty_trash_purges_everything(auth_client, app):
    _upload(auth_client, "a.txt", b"aaa")
    _upload(auth_client, "b.txt", b"bbb")
    for name in ("a.txt", "b.txt"):
        auth_client.post(f"/file/{_file_id(app, name)}/delete", follow_redirects=True)

    auth_client.post("/trash/empty", follow_redirects=True)

    with app.app_context():
        assert StoredFile.query.count() == 0


def test_same_name_upload_after_trash_creates_new_file(auth_client, app):
    _upload(auth_client, "notes.txt", b"old content")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)

    _upload(auth_client, "notes.txt", b"new content")

    with app.app_context():
        files = StoredFile.query.filter_by(name="notes.txt").all()
        assert len(files) == 2
        trashed = [f for f in files if f.deleted_at is not None]
        active = [f for f in files if f.deleted_at is None]
        assert len(trashed) == 1 and len(active) == 1
        assert file_service.read_text_content(active[0]) == "new content"


def test_folder_delete_trashes_files_and_restore_lands_at_root(auth_client, app):
    auth_client.post("/folder/create", data={"name": "sub"}, follow_redirects=True)
    with app.app_context():
        from app.models import Folder
        folder_id = Folder.query.filter_by(name="sub").one().id
    _upload(auth_client, "notes.txt", b"in folder", folder_id=str(folder_id))
    fid = _file_id(app)

    auth_client.post(f"/folder/{folder_id}/delete", follow_redirects=True)

    stored = _get(app, fid)
    assert stored.deleted_at is not None
    assert stored.folder_id is None

    auth_client.post(f"/file/{fid}/restore", follow_redirects=True)
    resp = auth_client.get("/")
    assert b"notes.txt" in resp.data
