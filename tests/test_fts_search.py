import io
from unittest.mock import patch

from app.extensions import db
from app.models import StoredFile, User
from app.services import search_service


def _upload(client, name, content):
    return client.post("/upload", data={
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def test_fts_finds_by_content_and_prefix(auth_client, app, user):
    _upload(auth_client, "manual.txt", b"installation instructions for servers")
    with app.app_context():
        user_obj = db.session.get(User, user)
        # Prefix matching: "insta" matches the token "installation".
        assert len(search_service.search_files("insta", user_obj)) == 1
        assert len(search_service.search_files("installation", user_obj)) == 1
        assert search_service.search_files("nonexistent", user_obj) == []


def test_fts_query_escaping_does_not_crash(auth_client, app, user):
    _upload(auth_client, "doc.txt", b'some "quoted" (content) with *stars*')
    with app.app_context():
        user_obj = db.session.get(User, user)
        # FTS operator characters in the query must not break MATCH.
        assert search_service.search_files('"quoted" (content)', user_obj)
        # Pure-punctuation terms tokenize to nothing — no match, no crash.
        assert search_service.search_files('")((" AND OR NEAR', user_obj) == []
        assert search_service.search_files('*', user_obj) == []


def test_fts_name_match_outranks_content(auth_client, app, user):
    _upload(auth_client, "alpha.txt", b"the word beta appears here")
    _upload(auth_client, "beta.txt", b"nothing relevant at all")
    with app.app_context():
        user_obj = db.session.get(User, user)
        results = search_service.search_files("beta", user_obj)
        assert results[0]["file"].name == "beta.txt"


def test_fts_updated_on_rename(auth_client, app, user):
    _upload(auth_client, "oldname.txt", b"body")
    with app.app_context():
        fid = StoredFile.query.one().id
    auth_client.post(f"/file/{fid}/rename", data={"name": "newname.txt"},
                     follow_redirects=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert search_service.search_files("newname", user_obj)
        assert search_service.search_files("oldname", user_obj) == []


def test_fts_trash_and_restore(auth_client, app, user):
    _upload(auth_client, "trashme.txt", b"unique trashable content")
    with app.app_context():
        fid = StoredFile.query.one().id
        user_obj = db.session.get(User, user)
        assert search_service.search_files("trashable", user_obj)

    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert search_service.search_files("trashable", user_obj) == []

    auth_client.post(f"/file/{fid}/restore", follow_redirects=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert len(search_service.search_files("trashable", user_obj)) == 1


def test_fts_purge_removes_row(auth_client, app, user):
    _upload(auth_client, "purgeme.txt", b"unique purgeable content")
    with app.app_context():
        fid = StoredFile.query.one().id
    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)
    auth_client.post(f"/file/{fid}/purge", follow_redirects=True)
    with app.app_context():
        count = db.session.execute(
            db.text("SELECT count(*) FROM file_fts")).scalar()
        assert count == 0


def test_fts_rebuild_repopulates(auth_client, app, user):
    _upload(auth_client, "a.txt", b"rebuild alpha")
    _upload(auth_client, "b.txt", b"rebuild beta")
    with app.app_context():
        db.session.execute(db.text("DELETE FROM file_fts"))
        db.session.commit()
        assert search_service.fts_rebuild() == 2
        user_obj = db.session.get(User, user)
        assert len(search_service.search_files("rebuild", user_obj)) == 2


def test_ilike_fallback_when_fts_disabled(auth_client, app, user):
    _upload(auth_client, "fallback.txt", b"findable via the old path")
    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.search_service.fts_available",
                   return_value=False):
            results = search_service.search_files("findable", user_obj)
        assert len(results) == 1
        assert results[0]["file"].name == "fallback.txt"


def test_fts_respects_search_fts_flag(auth_client, app, user):
    _upload(auth_client, "flag.txt", b"flag toggle content")
    app.config["SEARCH_FTS"] = False
    try:
        with app.app_context():
            assert search_service.fts_available() is False
            user_obj = db.session.get(User, user)
            assert search_service.search_files("toggle", user_obj)
    finally:
        app.config["SEARCH_FTS"] = True
