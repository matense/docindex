"""Tests for synced drives: local folder mirroring, read-only, on-demand sync."""

import io
import os
import time

import pytest

from app.extensions import db
from app.models import Drive, Folder, StoredFile, User
from app.services import agent_service, file_service, search_service, sync_service


@pytest.fixture()
def local_folder(tmp_path):
    """A real folder on disk with a small file tree."""
    (tmp_path / "hello.txt").write_text("hello world uniqueword", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# Notes\nsome markdown", encoding="utf-8")
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "inner.txt").write_text("inner file content", encoding="utf-8")
    (tmp_path / "ignored.exe").write_bytes(b"MZ")  # not an allowed extension
    return str(tmp_path)


def _sync_create(client, path):
    return client.post("/drives/sync-create", data={"path": path},
                       follow_redirects=True)


def _synced_drive(app):
    with app.app_context():
        drive = Drive.query.filter(Drive.source_path.isnot(None)).one()
        return drive.id


def test_create_synced_drive_mirrors_folder(auth_client, app, local_folder):
    resp = _sync_create(auth_client, local_folder)
    assert resp.status_code == 200

    with app.app_context():
        drive = Drive.query.filter(Drive.source_path.isnot(None)).one()
        assert drive.is_synced
        assert drive.last_synced_at is not None

        files = StoredFile.query.filter_by(drive_id=drive.id).all()
        names = {f.name for f in files}
        assert names == {"hello.txt", "notes.md", "inner.txt"}  # exe skipped
        assert all(f.is_synced for f in files)
        assert all(os.path.exists(f.source_path) for f in files)

        folder = Folder.query.filter_by(drive_id=drive.id, name="docs").one()
        inner = StoredFile.query.filter_by(name="inner.txt").one()
        assert inner.folder_id == folder.id

        # indexing ran synchronously (TestConfig INDEX_ASYNC=False)
        hello = StoredFile.query.filter_by(name="hello.txt").one()
        assert hello.index.status == "ok"
        assert "uniqueword" in hello.index.extracted_text


def test_synced_drive_is_read_only(auth_client, app, local_folder):
    _sync_create(auth_client, local_folder)
    with app.app_context():
        hello = StoredFile.query.filter_by(name="hello.txt").one()
        fid = hello.id
        real_path = hello.source_path

    # write routes are blocked
    assert auth_client.post(f"/file/{fid}/delete").status_code == 400
    assert auth_client.post(f"/file/{fid}/rename",
                            data={"name": "x.txt"}).status_code == 400
    assert auth_client.post(f"/file/{fid}/move").status_code == 400
    assert auth_client.get(f"/file/{fid}/edit").status_code == 400
    assert auth_client.post(f"/file/{fid}/purge").status_code == 400
    resp = auth_client.post("/selection/delete", data={"file_ids": str(fid)})
    assert resp.get_json()["deleted"] == 0

    # upload into the synced drive is refused (it is the current drive now)
    resp = auth_client.post("/upload", data={
        "files": (io.BytesIO(b"nope"), "nope.txt"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"read-only" in resp.data

    # read routes work and serve the real file
    assert auth_client.get(f"/file/{fid}/view").status_code == 200
    resp = auth_client.get(f"/file/{fid}/download")
    assert resp.status_code == 200
    assert resp.data == b"hello world uniqueword"

    # the real file was never touched
    assert os.path.exists(real_path)
    with app.app_context():
        assert db.session.get(StoredFile, fid) is not None


def test_service_guards_raise_on_synced_files(auth_client, app, local_folder):
    _sync_create(auth_client, local_folder)
    with app.app_context():
        stored = StoredFile.query.filter_by(name="hello.txt").one()
        with pytest.raises(ValueError):
            file_service.delete_file(stored)
        with pytest.raises(ValueError):
            file_service.purge_file(stored)
        with pytest.raises(ValueError):
            file_service.update_file_content(stored, "x")


def test_resync_picks_up_changes(auth_client, app, local_folder):
    _sync_create(auth_client, local_folder)
    drive_id = _synced_drive(app)

    # add + modify + remove on disk
    with open(os.path.join(local_folder, "new.txt"), "w", encoding="utf-8") as fh:
        fh.write("brand new file")
    with open(os.path.join(local_folder, "hello.txt"), "w", encoding="utf-8") as fh:
        fh.write("changed content completely")
    os.remove(os.path.join(local_folder, "docs", "inner.txt"))

    with app.app_context():
        drive = db.session.get(Drive, drive_id)
        stats = sync_service.sync_drive(drive)

    assert stats["added"] == 1
    assert stats["updated"] == 1
    assert stats["removed"] == 1

    with app.app_context():
        names = {f.name for f in StoredFile.query.filter_by(drive_id=drive_id)}
        assert names == {"hello.txt", "notes.md", "new.txt"}
        hello = StoredFile.query.filter_by(name="hello.txt").one()
        assert "changed content" in hello.index.extracted_text
        # no version history is kept for synced files
        assert hello.versions == []


def test_resync_route_and_missing_folder(auth_client, app, local_folder):
    _sync_create(auth_client, local_folder)
    drive_id = _synced_drive(app)
    resp = auth_client.post(f"/drives/{drive_id}/sync", follow_redirects=True)
    assert b"Sync complete" in resp.data

    # folder deleted on disk -> friendly error, drive keeps its state
    import shutil
    shutil.rmtree(local_folder)
    resp = auth_client.post(f"/drives/{drive_id}/sync", follow_redirects=True)
    assert b"no longer exists" in resp.data
    with app.app_context():
        assert StoredFile.query.filter_by(drive_id=drive_id).count() == 3


def test_synced_files_visible_to_search_and_agent(auth_client, app, local_folder):
    _sync_create(auth_client, local_folder)
    with app.app_context():
        user = User.query.filter_by(username="alice").one()
        results = search_service.search_files("uniqueword", user)
        assert [r["file"].name for r in results] == ["hello.txt"]

        listed = agent_service._tool_list_files(user)
        hello = next(f for f in listed["files"] if f["name"] == "hello.txt")
        assert hello["read_only"] is True

        fid = StoredFile.query.filter_by(name="hello.txt").one().id
        read = agent_service._tool_read_file(user, fid)
        assert "uniqueword" in read["content"]


def test_invalid_and_duplicate_paths(auth_client, app, local_folder):
    resp = _sync_create(auth_client, os.path.join(local_folder, "does-not-exist"))
    assert b"does not exist" in resp.data

    _sync_create(auth_client, local_folder)
    resp = _sync_create(auth_client, local_folder)
    assert b"already synced" in resp.data
    with app.app_context():
        assert Drive.query.filter(Drive.source_path.isnot(None)).count() == 1


# --- Background job tracking (SYNC_ASYNC=False in tests => runs inline) ---

def test_sync_records_stats_and_status(auth_client, app, local_folder):
    _sync_create(auth_client, local_folder)
    drive_id = _synced_drive(app)

    with app.app_context():
        drive = db.session.get(Drive, drive_id)
        import json
        stats = json.loads(drive.last_sync_stats)
        assert stats["added"] == 3
        assert stats["skipped"] == 1  # the .exe

    status = sync_service.get_status(drive_id)
    assert status["state"] == "done"
    assert status["percent"] == 100

    resp = auth_client.get(f"/drives/{drive_id}/sync/status")
    body = resp.get_json()
    assert body["state"] == "done"
    assert body["stats"]["added"] == 3


def test_pause_and_resume_job(auth_client, app, local_folder):
    _sync_create(auth_client, local_folder)
    drive_id = _synced_drive(app)

    # finished jobs cannot be paused
    assert sync_service.pause_sync(drive_id) is False

    # simulate a running job and drive the state machine
    job = sync_service._SyncJob(drive_id, "test")
    with sync_service._jobs_lock:
        sync_service._jobs[drive_id] = job
    try:
        assert sync_service.pause_sync(drive_id) is True
        assert job.state == "paused"
        with app.app_context():
            assert sync_service.get_active_job(1)["state"] == "paused"

        assert sync_service.resume_sync(drive_id) is True
        assert job.state == "running"
    finally:
        with sync_service._jobs_lock:
            sync_service._jobs.pop(drive_id, None)


def test_active_sync_endpoint(auth_client, app, local_folder):
    resp = auth_client.get("/sync/active")
    assert resp.get_json() == {"active": False, "job": None}

    _sync_create(auth_client, local_folder)
    drive_id = _synced_drive(app)
    job = sync_service._SyncJob(drive_id, "test")
    with sync_service._jobs_lock:
        sync_service._jobs[drive_id] = job
    try:
        resp = auth_client.get("/sync/active")
        body = resp.get_json()
        assert body["active"] is True
        assert body["job"]["drive_id"] == drive_id
    finally:
        with sync_service._jobs_lock:
            sync_service._jobs.pop(drive_id, None)


def test_second_sync_while_running_returns_same_job(auth_client, app, local_folder):
    _sync_create(auth_client, local_folder)
    drive_id = _synced_drive(app)
    with app.app_context():
        drive = db.session.get(Drive, drive_id)
        job = sync_service._SyncJob(drive_id, "test")
        with sync_service._jobs_lock:
            sync_service._jobs[drive_id] = job
        try:
            again = sync_service.start_sync(drive)
            assert again is job  # no duplicate run
        finally:
            with sync_service._jobs_lock:
                sync_service._jobs.pop(drive_id, None)
