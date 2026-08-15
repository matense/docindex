"""Tests for file version history and the AI merge review flow."""

import io
import os

import pytest

from app.extensions import db
from app.models import Drive, FileVersion, Folder, StoredFile, User
from app.services import ai_service, file_service


def _upload(client, name, content, folder_id=""):
    return client.post("/upload", data={
        "folder_id": folder_id,
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def _file_id(app, name="notes.txt"):
    with app.app_context():
        return StoredFile.query.filter_by(name=name).one().id


def _read_current(app, stored_id):
    with app.app_context():
        return file_service.read_text_content(db.session.get(StoredFile, stored_id))


# --- Same-name upload becomes a new version ---

def test_same_name_upload_creates_version(auth_client, app):
    _upload(auth_client, "notes.txt", b"version one content")
    _upload(auth_client, "notes.txt", b"version two content, changed")
    with app.app_context():
        stored = StoredFile.query.filter_by(name="notes.txt").one()
        assert StoredFile.query.count() == 1
        assert len(stored.versions) == 1
        assert stored.versions[0].source == "upload"
        assert stored.versions[0].version == 1
    assert _read_current(app, stored.id) == "version two content, changed"


def test_identical_reupload_is_noop(auth_client, app):
    _upload(auth_client, "notes.txt", b"same content")
    _upload(auth_client, "notes.txt", b"same content")
    with app.app_context():
        assert StoredFile.query.count() == 1
        assert FileVersion.query.count() == 0


def test_same_name_different_folder_stays_independent(auth_client, app):
    _upload(auth_client, "notes.txt", b"root version")
    auth_client.post("/folder/create", data={"name": "sub"}, follow_redirects=True)
    with app.app_context():
        folder_id = Folder.query.filter_by(name="sub").one().id
    _upload(auth_client, "notes.txt", b"folder version", folder_id=str(folder_id))
    with app.app_context():
        assert StoredFile.query.filter_by(name="notes.txt").count() == 2
        assert FileVersion.query.count() == 0


def test_same_name_different_drive_stays_independent(auth_client, app):
    _upload(auth_client, "notes.txt", b"personal version")
    auth_client.post("/drives/create", data={"name": "Work"}, follow_redirects=True)
    _upload(auth_client, "notes.txt", b"work version")
    with app.app_context():
        assert StoredFile.query.filter_by(name="notes.txt").count() == 2
        assert FileVersion.query.count() == 0


# --- Edit and restore ---

def test_edit_creates_version(auth_client, app):
    _upload(auth_client, "notes.txt", b"before edit")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/edit", data={"content": "after edit"})
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert len(stored.versions) == 1
        assert stored.versions[0].source == "edit"
    assert _read_current(app, fid) == "after edit"


def test_restore_version(auth_client, app):
    _upload(auth_client, "notes.txt", b"original content")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/edit", data={"content": "edited content"})
    with app.app_context():
        v1 = FileVersion.query.filter_by(file_id=fid, version=1).one()
    auth_client.post(f"/file/{fid}/history/{v1.id}/restore", follow_redirects=True)
    assert _read_current(app, fid) == "original content"
    with app.app_context():
        versions = FileVersion.query.filter_by(file_id=fid).order_by(FileVersion.version).all()
        assert len(versions) == 2
        assert versions[1].source == "restore"


def test_version_download(auth_client, app):
    _upload(auth_client, "notes.txt", b"old text")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/edit", data={"content": "new text"})
    with app.app_context():
        v1 = FileVersion.query.filter_by(file_id=fid).one()
    resp = auth_client.get(f"/file/{fid}/history/{v1.id}/download")
    assert resp.status_code == 200
    assert resp.data == b"old text"
    assert "notes.v1.txt" in resp.headers["Content-Disposition"]


def test_version_diff_page(auth_client, app):
    _upload(auth_client, "notes.txt", b"line one\nline two\n")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/edit", data={"content": "line one\nline CHANGED\n"})
    with app.app_context():
        v1 = FileVersion.query.filter_by(file_id=fid).one()
    resp = auth_client.get(f"/file/{fid}/history/{v1.id}/diff")
    assert resp.status_code == 200
    assert b"-line two" in resp.data
    assert b"+line CHANGED" in resp.data


def test_binary_versions_download_only(auth_client, app):
    # Tiny fake PNG (thumbnail failure is tolerated by _make_thumbnail)
    _upload(auth_client, "img.png", b"\x89PNG\r\n\x1a\nfake-one")
    _upload(auth_client, "img.png", b"\x89PNG\r\n\x1a\nfake-two-longer")
    with app.app_context():
        stored = StoredFile.query.filter_by(name="img.png").one()
        assert len(stored.versions) == 1
        vid = stored.versions[0].id
        fid = stored.id
    resp = auth_client.get(f"/file/{fid}/history/{vid}/download")
    assert resp.status_code == 200
    assert resp.data == b"\x89PNG\r\n\x1a\nfake-one"
    # Binary: no in-place restore, no diff
    assert auth_client.post(f"/file/{fid}/history/{vid}/restore").status_code == 400
    assert auth_client.get(f"/file/{fid}/history/{vid}/diff").status_code == 400


def test_delete_removes_version_blobs(auth_client, app):
    _upload(auth_client, "notes.txt", b"v1")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/edit", data={"content": "v2"})
    with app.app_context():
        versions_dir = app.config["VERSIONS_FOLDER"]
        blob_names = [v.stored_name for v in FileVersion.query.all()]
        assert len(blob_names) == 1
        for name in blob_names:
            assert os.path.exists(os.path.join(versions_dir, name))
    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)
    with app.app_context():
        assert StoredFile.query.count() == 0
        assert FileVersion.query.count() == 0
        for name in blob_names:
            assert not os.path.exists(os.path.join(versions_dir, name))


def test_versions_of_other_users_are_not_accessible(client, auth_client, app):
    _upload(auth_client, "notes.txt", b"alice v1")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/edit", data={"content": "alice v2"})
    with app.app_context():
        vid = FileVersion.query.filter_by(file_id=fid).one().id
        bob = User(username="bob", email="bob@example.com")
        bob.set_password("bobpass123")
        db.session.add(bob)
        db.session.commit()
    auth_client.get("/logout")
    client.post("/login", data={"username": "bob", "password": "bobpass123"})
    assert client.get(f"/file/{fid}/history/{vid}/download").status_code == 404
    assert client.post(f"/file/{fid}/history/{vid}/restore").status_code == 404


# --- AI merge with review ---

def _two_files(auth_client):
    _upload(auth_client, "a.txt", b"alpha content\nshared line\n")
    _upload(auth_client, "b.txt", b"beta content\nshared line\n")


def test_merge_review_page(auth_client, app):
    _two_files(auth_client)
    with app.app_context():
        a = StoredFile.query.filter_by(name="a.txt").one()
        b = StoredFile.query.filter_by(name="b.txt").one()
    resp = auth_client.get(f"/file/{a.id}/merge/{b.id}")
    assert resp.status_code == 200
    assert b"alpha content" in resp.data
    assert b"beta content" in resp.data


def test_merge_ai_endpoint(auth_client, app, monkeypatch):
    _two_files(auth_client)
    with app.app_context():
        a = StoredFile.query.filter_by(name="a.txt").one()
        b = StoredFile.query.filter_by(name="b.txt").one()
    monkeypatch.setattr(ai_service, "is_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "chat_completion",
                        lambda *a_, **kw: {"content": "merged by ai"})
    resp = auth_client.post(f"/file/{a.id}/merge/{b.id}/ai")
    assert resp.status_code == 200
    assert resp.get_json()["merged"] == "merged by ai"


def test_merge_ai_disabled_returns_400(auth_client, app):
    _two_files(auth_client)
    with app.app_context():
        a = StoredFile.query.filter_by(name="a.txt").one()
        b = StoredFile.query.filter_by(name="b.txt").one()
    resp = auth_client.post(f"/file/{a.id}/merge/{b.id}/ai")
    assert resp.status_code == 400


def test_merge_accept_keeps_history_and_deletes_other(auth_client, app):
    _two_files(auth_client)
    with app.app_context():
        a = StoredFile.query.filter_by(name="a.txt").one()
        b = StoredFile.query.filter_by(name="b.txt").one()
        a_id = a.id
    auth_client.post(f"/file/{a_id}/merge/{b.id}/accept",
                     data={"content": "merged final\n", "delete_other": "on"},
                     follow_redirects=True)
    with app.app_context():
        stored = db.session.get(StoredFile, a_id)
        assert StoredFile.query.count() == 1
        assert len(stored.versions) == 1
        assert stored.versions[0].source == "merge"
        assert "b.txt" in stored.versions[0].note
    assert _read_current(app, a_id) == "merged final\n"


def test_merge_accept_without_delete_keeps_both(auth_client, app):
    _two_files(auth_client)
    with app.app_context():
        a_id = StoredFile.query.filter_by(name="a.txt").one().id
        b_id = StoredFile.query.filter_by(name="b.txt").one().id
    auth_client.post(f"/file/{a_id}/merge/{b_id}/accept",
                     data={"content": "merged final\n"}, follow_redirects=True)
    with app.app_context():
        assert StoredFile.query.count() == 2


def test_merge_rejects_binary(auth_client, app):
    _upload(auth_client, "x.png", b"\x89PNG\r\n\x1a\naaa")
    _upload(auth_client, "y.png", b"\x89PNG\r\n\x1a\nbbb")
    with app.app_context():
        x = StoredFile.query.filter_by(name="x.png").one()
        y = StoredFile.query.filter_by(name="y.png").one()
    assert auth_client.get(f"/file/{x.id}/merge/{y.id}").status_code == 400


def test_merge_preview_returns_diff(auth_client, app):
    _two_files(auth_client)
    with app.app_context():
        a = StoredFile.query.filter_by(name="a.txt").one()
        b = StoredFile.query.filter_by(name="b.txt").one()
    resp = auth_client.post(f"/file/{a.id}/merge/{b.id}/preview",
                            json={"content": "alpha content\nshared line\nbeta content\n"})
    assert resp.status_code == 200
    diff = resp.get_json()["diff"]
    assert any(line.startswith("+beta content") for line in diff)
