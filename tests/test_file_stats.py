import hashlib
import io

from app.extensions import db
from app.models import StoredFile


def _upload(client, name, content, accept_json=False):
    kwargs = {}
    if accept_json:
        kwargs["headers"] = {"Accept": "application/json"}
    return client.post("/upload", data={
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True, **kwargs)


def test_upload_computes_sha256(auth_client, app):
    content = b"hash me please"
    _upload(auth_client, "hashed.txt", content)
    with app.app_context():
        stored = StoredFile.query.one()
        assert stored.checksum == hashlib.sha256(content).hexdigest()


def test_index_computes_content_stats(auth_client, app):
    _upload(auth_client, "stats.txt", b"one two three\nfour five\nsix")
    with app.app_context():
        index = StoredFile.query.one().index
        assert index.status == "ok"
        assert index.word_count == 6
        assert index.line_count == 3
        assert index.char_count == len("one two three\nfour five\nsix")


def test_edit_updates_checksum(auth_client, app):
    _upload(auth_client, "editable.txt", b"original")
    with app.app_context():
        fid = StoredFile.query.one().id
    auth_client.post(f"/file/{fid}/edit", data={"content": "changed content"},
                     follow_redirects=True)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert stored.checksum == hashlib.sha256(b"changed content").hexdigest()


def test_duplicate_detection_on_upload(auth_client, app):
    _upload(auth_client, "first.txt", b"identical content")
    resp = _upload(auth_client, "second.txt", b"identical content", accept_json=True)
    data = resp.get_json()
    assert data["files"][0]["duplicates"]
    assert data["files"][0]["duplicates"][0]["name"] == "first.txt"

    # Different content: no duplicates
    resp = _upload(auth_client, "third.txt", b"unique stuff", accept_json=True)
    assert resp.get_json()["files"][0]["duplicates"] == []

    # HTML flow flashes a warning
    resp = _upload(auth_client, "fourth.txt", b"identical content")
    assert b"identical content to" in resp.data


def test_duplicates_not_detected_across_drives(auth_client, app):
    # Same content in another drive is NOT a duplicate: detection is per-drive.
    _upload(auth_client, "a.txt", b"cross-drive content")
    auth_client.post("/drives/create", data={"name": "Work"}, follow_redirects=True)
    resp = _upload(auth_client, "b.txt", b"cross-drive content", accept_json=True)
    assert resp.get_json()["files"][0]["duplicates"] == []

    # ...but a second copy inside the same drive still is.
    resp = _upload(auth_client, "c.txt", b"cross-drive content", accept_json=True)
    assert resp.get_json()["files"][0]["duplicates"][0]["name"] == "b.txt"


def test_info_endpoint_stats_checksum_duplicates(auth_client, app):
    _upload(auth_client, "orig.txt", b"alpha beta gamma")
    _upload(auth_client, "copy.txt", b"alpha beta gamma")
    with app.app_context():
        fid = StoredFile.query.filter_by(name="copy.txt").one().id
    info = auth_client.get(f"/file/{fid}/info").get_json()
    assert info["checksum"] == hashlib.sha256(b"alpha beta gamma").hexdigest()
    assert info["word_count"] == 3
    assert info["line_count"] == 1
    assert info["char_count"] == 16
    assert [d["name"] for d in info["duplicates"]] == ["orig.txt"]


def test_duplicate_badge_shown_on_drive_page(auth_client, app):
    _upload(auth_client, "one.txt", b"dup content")
    _upload(auth_client, "two.txt", b"dup content")
    resp = auth_client.get("/")
    assert b"dupe" in resp.data
