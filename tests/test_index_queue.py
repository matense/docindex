import io
from unittest.mock import patch

from app.extensions import db
from app.models import IndexJob, StoredFile, User
from app.services import indexing_service


def _upload(client, name, content):
    return client.post("/upload", data={
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def test_enqueue_creates_pending_job_and_dedupes(auth_client, app):
    _upload(auth_client, "q.txt", b"queue me")
    with app.app_context():
        fid = StoredFile.query.one().id
        assert indexing_service.enqueue_index([fid]) == 1
        assert indexing_service.enqueue_index([fid]) == 0  # already pending
        job = IndexJob.query.one()
        assert job.status == "pending"
        assert job.attempts == 0


def test_enqueue_skips_trashed_and_missing_files(auth_client, app):
    _upload(auth_client, "t.txt", b"trash before index")
    with app.app_context():
        fid = StoredFile.query.one().id
        assert indexing_service.enqueue_index([fid, 99999]) == 1
        stored = db.session.get(StoredFile, fid)
        stored.deleted_at = db.func.now()
        db.session.commit()
        assert indexing_service.enqueue_index([99999]) == 0


def test_inline_run_processes_jobs(auth_client, app):
    _upload(auth_client, "inline.txt", b"process me inline")
    with app.app_context():
        fid = StoredFile.query.one().id
        indexing_service.enqueue_index([fid])
        indexing_service.run_pending_inline()
        job = IndexJob.query.one()
        assert job.status == "done"
        assert db.session.get(StoredFile, fid).index.status == "ok"


def test_inline_run_marks_missing_file_done(auth_client, app):
    _upload(auth_client, "gone.txt", b"x")
    with app.app_context():
        fid = StoredFile.query.one().id
        indexing_service.enqueue_index([fid])
        # Simulate the file vanishing between enqueue and processing.
        job = IndexJob.query.one()
        job.file_id = 99999
        db.session.commit()
        indexing_service.run_pending_inline()
        assert IndexJob.query.one().status == "done"


def test_failed_indexing_retries_then_errors(auth_client, app):
    _upload(auth_client, "bad.txt", b"will fail")
    with app.app_context():
        fid = StoredFile.query.one().id
        indexing_service.enqueue_index([fid])
        with patch("app.services.indexing_service.extract_text",
                   side_effect=RuntimeError("boom")):
            indexing_service.run_pending_inline()
        job = IndexJob.query.one()
        assert job.status == "error"
        assert job.attempts == indexing_service.MAX_JOB_ATTEMPTS
        assert "boom" in (job.error or "")
        # The failure is also visible on the file's index badge.
        assert db.session.get(StoredFile, fid).index.status == "error"


def test_recover_interrupted_requeues_running(auth_client, app):
    _upload(auth_client, "crash.txt", b"crashed mid index")
    with app.app_context():
        fid = StoredFile.query.one().id
        indexing_service.enqueue_index([fid])
        job = IndexJob.query.one()
        job.status = "running"  # simulate a crash mid-processing
        db.session.commit()
        indexing_service.recover_interrupted(app)
        assert IndexJob.query.one().status == "pending"


def test_recover_purges_old_finished_jobs(auth_client, app):
    from datetime import timedelta
    from app.models import utcnow
    _upload(auth_client, "old.txt", b"old job")
    with app.app_context():
        fid = StoredFile.query.one().id
        indexing_service.enqueue_index([fid])
        job = IndexJob.query.one()
        job.status = "done"
        job.updated_at = utcnow() - timedelta(hours=25)
        db.session.commit()
        indexing_service.recover_interrupted(app)
        assert IndexJob.query.count() == 0


def test_queue_status_counts_and_percent(auth_client, app, user):
    _upload(auth_client, "s1.txt", b"one")
    _upload(auth_client, "s2.txt", b"two")
    with app.app_context():
        ids = [f.id for f in StoredFile.query.all()]
        indexing_service.enqueue_index(ids)
        job = IndexJob.query.filter_by(file_id=ids[0]).one()
        job.status = "done"
        db.session.commit()
        user_obj = db.session.get(User, user)
        status = indexing_service.get_queue_status(user_obj)
        assert status["counts"]["done"] == 1
        assert status["counts"]["pending"] == 1
        assert status["active"] == 1
        assert status["total"] == 2
        assert status["percent"] == 50
        assert status["paused"] is False


def test_pause_blocks_drainers_and_resume_restarts(auth_client, app):
    _upload(auth_client, "p.txt", b"pause me")
    with app.app_context():
        fid = StoredFile.query.one().id
        indexing_service.enqueue_index([fid])
        indexing_service.pause_queue()
        assert indexing_service.queue_paused() is True
        # While paused, ensure_workers spawns nothing and the job stays put.
        indexing_service.ensure_workers(2, app)
        assert IndexJob.query.one().status == "pending"
        indexing_service.resume_queue(app)
        assert indexing_service.queue_paused() is False
        # Clean up: don't leave the global flag/threads affecting others.
        indexing_service.run_pending_inline()


def test_index_queue_routes(auth_client, app):
    _upload(auth_client, "r.txt", b"route job")
    with app.app_context():
        fid = StoredFile.query.one().id
        indexing_service.enqueue_index([fid])

    resp = auth_client.get("/settings/index-queue/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["counts"]["pending"] == 1
    assert data["paused"] is False

    resp = auth_client.post("/settings/index-queue/pause")
    assert resp.get_json()["paused"] is True
    assert indexing_service.queue_paused() is True

    resp = auth_client.post("/settings/index-queue/resume")
    assert resp.get_json()["paused"] is False
    assert indexing_service.queue_paused() is False

    with app.app_context():
        indexing_service.run_pending_inline()  # leave nothing behind


def test_index_queue_routes_require_login(client):
    assert client.get("/settings/index-queue/status").status_code == 302
    assert client.post("/settings/index-queue/pause").status_code == 302
