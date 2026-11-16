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


# --- Dedicated trash page (/settings/trash) ---

def test_trash_page_lists_deleted_files(auth_client, app):
    _upload(auth_client, "notes.txt", b"some content")
    auth_client.post(f"/file/{_file_id(app)}/delete", follow_redirects=True)

    resp = auth_client.get("/settings/trash")
    assert resp.status_code == 200
    assert b"notes.txt" in resp.data


def test_trash_page_hides_active_files_and_filters_by_name(auth_client, app):
    _upload(auth_client, "keepme.txt", b"active")
    _upload(auth_client, "old-report.txt", b"trashed")
    auth_client.post(f"/file/{_file_id(app, 'old-report.txt')}/delete",
                     follow_redirects=True)

    resp = auth_client.get("/settings/trash")
    assert b"old-report.txt" in resp.data
    assert b"keepme.txt" not in resp.data

    resp = auth_client.get("/settings/trash?q=report")
    assert b"old-report.txt" in resp.data
    resp = auth_client.get("/settings/trash?q=keepme")
    assert b"keepme.txt" not in resp.data


def test_trash_page_requires_login(client):
    resp = client.get("/settings/trash")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_restore_get_does_not_mutate(auth_client, app):
    _upload(auth_client, "notes.txt", b"some content")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)

    resp = auth_client.get(f"/file/{fid}/restore")
    assert resp.status_code in (404, 405)
    assert _get(app, fid).deleted_at is not None


def test_profile_links_to_trash_page_with_count(auth_client, app):
    _upload(auth_client, "notes.txt", b"some content")
    auth_client.post(f"/file/{_file_id(app)}/delete", follow_redirects=True)

    resp = auth_client.get("/settings/profile")
    assert b"/settings/trash" in resp.data
    assert b"1 file" in resp.data


def test_restore_redirects_to_trash_page(auth_client, app):
    _upload(auth_client, "notes.txt", b"some content")
    fid = _file_id(app)
    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)

    resp = auth_client.post(f"/file/{fid}/restore")
    assert resp.status_code == 302
    assert "/settings/trash" in resp.headers["Location"]


# --- D4 (argon P0): folder delete must purge already-trashed files ---

def test_folder_delete_purges_trashed_file_artifacts(auth_client, app):
    """Already-trashed files inside a deleted folder must go through
    purge_file(): no blob/version/job/embedding orphans left behind.
    Active files are still only trashed (restorable, blobs kept)."""
    import os as _os

    from sqlalchemy import text as _sql

    from app.models import FileEmbedding, Folder, IndexJob, StoredFile
    auth_client.post("/folder/create", data={"name": "sub"},
                     follow_redirects=True)
    with app.app_context():
        folder_id = Folder.query.filter_by(name="sub").one().id
    auth_client.post("/folder/create", data={"name": "kid",
                                             "parent_id": str(folder_id)},
                     follow_redirects=True)

    _upload(auth_client, "normal.txt", b"normal content",
            folder_id=str(folder_id))
    _upload(auth_client, "vers.txt", b"v1 content", folder_id=str(folder_id))
    _upload(auth_client, "vers.txt", b"v2 content", folder_id=str(folder_id))
    _upload(auth_client, "doomed.txt", b"doomed v1",
            folder_id=str(folder_id))
    _upload(auth_client, "doomed.txt", b"doomed v2",
            folder_id=str(folder_id))
    with app.app_context():
        kid_id = Folder.query.filter_by(name="kid").one().id
    _upload(auth_client, "kiddoomed.txt", b"kid doomed",
            folder_id=str(kid_id))

    with app.app_context():
        doomed_file = StoredFile.query.filter_by(name="doomed.txt").one()
        kid_doomed = StoredFile.query.filter_by(name="kiddoomed.txt").one()
        doomed_id, kid_file_id = doomed_file.id, kid_doomed.id
        # trashed file carries a version blob (two uploads -> one version)
        assert len(doomed_file.versions) == 1
        doomed_blob = file_service.file_path(doomed_file)
        doomed_vers = [file_service.version_path(v)
                       for v in doomed_file.versions]
        kid_blob = file_service.file_path(kid_doomed)
        # seed un-cascaded rows: embedding + queued index job
        db.session.add(FileEmbedding(
            file_id=doomed_id, chunk_index=0, text="t", text_hash="h",
            embedding=b"\x00\x01", model="t", dims=1))
        db.session.add(IndexJob(file_id=doomed_id, status="pending"))
        db.session.add(FileEmbedding(
            file_id=kid_file_id, chunk_index=0, text="t", text_hash="h",
            embedding=b"\x00\x01", model="t", dims=1))
        db.session.add(IndexJob(file_id=kid_file_id, status="pending"))
        db.session.commit()
        normal_id = StoredFile.query.filter_by(name="normal.txt").one().id
        normal_blob = file_service.file_path(
            db.session.get(StoredFile, normal_id))

    auth_client.post(f"/file/{doomed_id}/delete", follow_redirects=True)
    auth_client.post(f"/file/{kid_file_id}/delete", follow_redirects=True)

    auth_client.post(f"/folder/{folder_id}/delete", follow_redirects=True)

    with app.app_context():
        # purged: rows gone AND every artifact gone
        assert db.session.get(StoredFile, doomed_id) is None
        assert db.session.get(StoredFile, kid_file_id) is None
        assert not _os.path.exists(doomed_blob)
        assert not any(_os.path.exists(p) for p in doomed_vers)
        assert not _os.path.exists(kid_blob)
        assert IndexJob.query.filter_by(file_id=doomed_id).count() == 0
        assert IndexJob.query.filter_by(file_id=kid_file_id).count() == 0
        assert FileEmbedding.query.filter_by(
            file_id=doomed_id).count() == 0
        assert FileEmbedding.query.filter_by(
            file_id=kid_file_id).count() == 0
        if search_service.fts_available():
            for gone in (doomed_id, kid_file_id):
                left = db.session.execute(
                    _sql("SELECT count(*) FROM idx.file_fts "
                         "WHERE rowid = :id"), {"id": gone}).scalar()
                assert left == 0
        # active files: trashed (restorable), blobs kept
        stored = StoredFile.query.filter_by(name="normal.txt").one()
        assert stored.deleted_at is not None
        assert _os.path.exists(normal_blob)
        vers = StoredFile.query.filter_by(name="vers.txt").one()
        assert vers.deleted_at is not None
        assert len(vers.versions) == 1
