import io
import json
from unittest.mock import patch

from app.extensions import db
from app.models import Drive, StoredFile, User
from app.services import agent_service, hashtag_service, search_service


def _upload(client, name, content):
    return client.post("/upload", data={
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def _one_file():
    return StoredFile.query.one()


def test_set_tags_normalizes_and_dedupes(auth_client, app):
    _upload(auth_client, "doc.txt", b"some content")
    with app.app_context():
        stored = _one_file()
        tags = hashtag_service.set_tags(
            stored, [" #Budget ", "budget", "Q1  Report", "", None], source="user")
        assert tags == ["budget", "q1 report"]
        assert hashtag_service.get_tags(stored.index) == ["budget", "q1 report"]


def test_ai_tags_respect_word_limit(auth_client, app):
    app.config["AI_HASHTAG_MAX_WORDS"] = 2
    _upload(auth_client, "doc.txt", b"some content")
    with app.app_context():
        stored = _one_file()
        tags = hashtag_service.set_tags(
            stored, ["short", "a bit longer tag", "two words"], source="ai")
        assert tags == ["short", "two words"]
        # User tags are NOT word-limited.
        tags = hashtag_service.set_tags(stored, ["a much longer user tag"],
                                        source="user")
        assert tags == ["a much longer user tag"]


def test_generate_tags_with_ai(auth_client, app):
    _upload(auth_client, "report.txt", b"quarterly revenue grew by 42 percent")
    with app.app_context():
        stored = _one_file()
        fake = {"content": "revenue, Quarterly Report, #finance"}
        with patch("app.services.ai_service.chat_completion",
                   return_value=fake) as call:
            tags = hashtag_service.generate_tags(stored, cfg={})
        assert tags == ["revenue", "quarterly report", "finance"]
        assert call.call_args.kwargs.get("block") is True


def test_search_finds_hashtag(auth_client, app, user):
    _upload(auth_client, "scan.pdf.txt", b"unrelated content here")
    with app.app_context():
        stored = _one_file()
        hashtag_service.set_tags(stored, ["invoice", "acme corp"], source="user")
        user_obj = db.session.get(User, user)
        results = search_service.search_files("invoice", user_obj)
        assert len(results) == 1
        assert "tags" in results[0]["matches"]
        assert results[0]["tags"] == ["invoice", "acme corp"]


def test_hashtag_routes_get_post(auth_client, app):
    _upload(auth_client, "r.txt", b"route content")
    with app.app_context():
        fid = _one_file().id
    resp = auth_client.get(f"/file/{fid}/hashtags")
    assert resp.get_json() == {"tags": []}
    resp = auth_client.post(f"/file/{fid}/hashtags",
                            json={"tags": ["Alpha", "#beta"]})
    assert resp.get_json() == {"tags": ["alpha", "beta"]}
    resp = auth_client.get(f"/file/{fid}/hashtags")
    assert resp.get_json() == {"tags": ["alpha", "beta"]}


def test_hashtag_routes_ownership(auth_client, app):
    _upload(auth_client, "private.txt", b"secret stuff")
    with app.app_context():
        fid = _one_file().id
        other = User(username="bob", email="bob@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
    other_client = app.test_client()
    other_client.post("/login", data={"username": "bob", "password": "password123"})
    assert other_client.get(f"/file/{fid}/hashtags").status_code == 404
    assert other_client.post(f"/file/{fid}/hashtags",
                             json={"tags": ["hack"]}).status_code == 404


def test_agent_tool_set_hashtags(auth_client, app, user):
    _upload(auth_client, "agent.txt", b"agent content")
    with app.app_context():
        stored = _one_file()
        user_obj = db.session.get(User, user)
        result = agent_service.TOOL_HANDLERS["set_hashtags"](
            user_obj, {"file_id": stored.id, "hashtags": ["TagOne", "#tag two"]}, None)
        assert result["hashtags"] == ["tagone", "tag two"]
        # Another user's file id is rejected.
        other = User(username="carol", email="carol@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        result = agent_service.TOOL_HANDLERS["set_hashtags"](
            other, {"file_id": stored.id, "hashtags": ["x"]}, None)
        assert "error" in result


def test_bulk_job_tags_drive(auth_client, app, user):
    app.config["AI_ENABLED"] = True
    _upload(auth_client, "a.txt", b"alpha content")
    _upload(auth_client, "b.txt", b"beta content")
    with app.app_context():
        drive = Drive.query.filter_by(user_id=user).one()
        fake = {"content": "tag1, tag2"}
        with patch("app.services.ai_service.chat_completion", return_value=fake):
            job = hashtag_service.start_job(drive, workers=2)
        assert job.state == "done"
        assert job.stats == {"tagged": 2, "skipped": 0, "failed": 0}
        for f in StoredFile.query.all():
            assert hashtag_service.get_tags(f.index) == ["tag1", "tag2"]

        # Second run without overwrite: everything skipped.
        with patch("app.services.ai_service.chat_completion", return_value=fake):
            job2 = hashtag_service.start_job(drive, workers=1)
        assert job2.stats == {"tagged": 0, "skipped": 2, "failed": 0}

        # With overwrite: tagged again.
        with patch("app.services.ai_service.chat_completion", return_value=fake):
            job3 = hashtag_service.start_job(drive, workers=1, overwrite=True)
        assert job3.stats["tagged"] == 2


def test_bulk_job_requires_ai(auth_client, app, user):
    _upload(auth_client, "c.txt", b"gamma content")
    with app.app_context():
        drive = Drive.query.filter_by(user_id=user).one()
        job = hashtag_service.start_job(drive)
        assert job.state == "error"
        assert "AI is not configured" in job.error


def test_bulk_start_route(auth_client, app):
    app.config["AI_ENABLED"] = True
    _upload(auth_client, "d.txt", b"delta content")
    with app.app_context():
        drive_id = _one_file().drive_id
    with patch("app.services.ai_service.chat_completion",
               return_value={"content": "x, y"}):
        resp = auth_client.post(f"/drives/{drive_id}/hashtags/start",
                                data={"workers": "2"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["state"] == "done"  # HASHTAG_ASYNC=False in tests
    assert data["stats"]["tagged"] == 1

    resp = auth_client.get(f"/drives/{drive_id}/hashtags/status")
    assert resp.get_json()["state"] == "done"


def test_bulk_start_route_without_ai(auth_client, app):
    _upload(auth_client, "e.txt", b"epsilon content")
    with app.app_context():
        drive_id = _one_file().drive_id
    resp = auth_client.post(f"/drives/{drive_id}/hashtags/start", data={})
    assert resp.status_code == 400


def test_view_page_shows_hashtags_section(auth_client, app):
    _upload(auth_client, "v.txt", b"view content")
    with app.app_context():
        fid = _one_file().id
    resp = auth_client.get(f"/file/{fid}/view")
    assert b"hashtags-section" in resp.data
    assert b"hashtags.js" in resp.data


def test_suggest_route_does_not_persist(auth_client, app):
    app.config["AI_ENABLED"] = True
    _upload(auth_client, "s.txt", b"suggest me some tags")
    with app.app_context():
        fid = _one_file().id
    with patch("app.services.ai_service.chat_completion",
               return_value={"content": "one, two, three"}):
        resp = auth_client.post(f"/file/{fid}/hashtags/suggest")
    assert resp.status_code == 200
    assert resp.get_json() == {"tags": ["one", "two", "three"]}
    # Suggestions are NOT saved — the user must accept them first.
    resp = auth_client.get(f"/file/{fid}/hashtags")
    assert resp.get_json() == {"tags": []}


def test_suggest_route_without_ai(auth_client, app):
    _upload(auth_client, "s2.txt", b"no ai here")
    with app.app_context():
        fid = _one_file().id
    resp = auth_client.post(f"/file/{fid}/hashtags/suggest")
    assert resp.status_code == 400


def test_suggest_route_ai_error(auth_client, app):
    from app.services import ai_service
    app.config["AI_ENABLED"] = True
    _upload(auth_client, "s3.txt", b"broken ai")
    with app.app_context():
        fid = _one_file().id
    with patch("app.services.ai_service.chat_completion",
               side_effect=ai_service.AIError("backend down")):
        resp = auth_client.post(f"/file/{fid}/hashtags/suggest")
    assert resp.status_code == 502
    assert "backend down" in resp.get_json()["error"]


def test_file_index_has_hashtags_column(auth_client, app):
    _upload(auth_client, "h.txt", b"column check")
    with app.app_context():
        stored = _one_file()
        stored.index.hashtags = json.dumps(["raw"])
        db.session.commit()
        assert hashtag_service.get_tags(stored.index) == ["raw"]


def test_get_all_tags_counts_and_orders(auth_client, app, user):
    _upload(auth_client, "t1.txt", b"first")
    _upload(auth_client, "t2.txt", b"second")
    with app.app_context():
        files = StoredFile.query.order_by(StoredFile.id).all()
        hashtag_service.set_tags(files[0], ["common", "alpha"], source="user")
        hashtag_service.set_tags(files[1], ["common", "beta"], source="user")
        user_obj = db.session.get(User, user)
        tags = hashtag_service.get_all_tags(user_obj)
        # "common" used twice comes first; ties are alphabetical.
        assert tags == [("common", 2), ("alpha", 1), ("beta", 1)]


def test_get_all_tags_scoped_to_user(auth_client, app, user):
    _upload(auth_client, "mine.txt", b"my content")
    with app.app_context():
        hashtag_service.set_tags(_one_file(), ["mine"], source="user")
        other = User(username="dave", email="dave@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        assert hashtag_service.get_all_tags(other) == []


def test_agent_tool_list_hashtags(auth_client, app, user):
    _upload(auth_client, "agent1.txt", b"alpha")
    _upload(auth_client, "agent2.txt", b"beta")
    with app.app_context():
        files = StoredFile.query.order_by(StoredFile.id).all()
        hashtag_service.set_tags(files[0], ["shared", "one"], source="user")
        hashtag_service.set_tags(files[1], ["shared"], source="user")
        user_obj = db.session.get(User, user)
        result = agent_service.TOOL_HANDLERS["list_hashtags"](user_obj, {}, None)
        assert result == [{"tag": "shared", "count": 2},
                          {"tag": "one", "count": 1}]
        # Files from other users are not visible.
        other = User(username="erin", email="erin@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        assert agent_service.TOOL_HANDLERS["list_hashtags"](other, {}, None) == []


def test_agent_search_results_include_hashtags(auth_client, app, user):
    _upload(auth_client, "tagged.txt", b"searchable body")
    with app.app_context():
        hashtag_service.set_tags(_one_file(), ["searchable"], source="user")
        user_obj = db.session.get(User, user)
        result = agent_service.TOOL_HANDLERS["search_files"](
            user_obj, {"query": "searchable"}, None)
        assert len(result) == 1
        assert result[0]["hashtags"] == ["searchable"]
