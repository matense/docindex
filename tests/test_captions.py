"""Manual AI image captions: suggest (no persistence) + reviewed save."""

import io
from unittest.mock import patch

from app.extensions import db
from app.models import StoredFile, User
from app.services import ai_service, search_service

# 1x1 transparent PNG.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _upload_image(client, name="pic.png"):
    return client.post("/upload", data={
        "files": (io.BytesIO(PNG), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def _one_file():
    return StoredFile.query.one()


def test_caption_suggest_does_not_persist(auth_client, app):
    _upload_image(auth_client)  # uploaded with AI off — indexing is a no-op
    app.config["AI_ENABLED"] = True
    with app.app_context():
        fid = _one_file().id
    with patch("app.services.ai_service.chat_completion",
               return_value={"content": "a cat sleeping on a sofa"}) as call:
        resp = auth_client.post(f"/file/{fid}/caption/suggest")
    assert resp.status_code == 200
    assert resp.get_json() == {"caption": "a cat sleeping on a sofa"}
    # vision model + blocking rate-limit slot for user-initiated requests
    assert call.call_args.kwargs.get("block") is True
    # Suggestions are NOT saved — the user must accept them first.
    with app.app_context():
        assert _one_file().index is None or _one_file().index.caption is None


def test_caption_suggest_requires_image(auth_client, app):
    app.config["AI_ENABLED"] = True
    auth_client.post("/upload", data={
        "files": (io.BytesIO(b"plain text"), "notes.txt"),
    }, content_type="multipart/form-data", follow_redirects=True)
    with app.app_context():
        fid = _one_file().id
    resp = auth_client.post(f"/file/{fid}/caption/suggest")
    assert resp.status_code == 400


def test_caption_suggest_without_ai(auth_client, app):
    _upload_image(auth_client)
    with app.app_context():
        fid = _one_file().id
    resp = auth_client.post(f"/file/{fid}/caption/suggest")
    assert resp.status_code == 400


def test_caption_suggest_ai_error(auth_client, app):
    _upload_image(auth_client)
    app.config["AI_ENABLED"] = True
    with app.app_context():
        fid = _one_file().id
    with patch("app.services.ai_service.chat_completion",
               side_effect=ai_service.AIError("vision backend down")):
        resp = auth_client.post(f"/file/{fid}/caption/suggest")
    assert resp.status_code == 502
    assert "vision backend down" in resp.get_json()["error"]


def test_caption_save_and_search(auth_client, app, user):
    _upload_image(auth_client)
    with app.app_context():
        fid = _one_file().id
    resp = auth_client.post(f"/file/{fid}/caption",
                            json={"caption": "a red barn at sunset"})
    assert resp.status_code == 200
    assert resp.get_json() == {"caption": "a red barn at sunset"}
    with app.app_context():
        assert _one_file().index.caption == "a red barn at sunset"
        # Captions are searchable (FTS path and ILIKE fallback).
        user_obj = db.session.get(User, user)
        results = search_service.search_files("barn", user_obj)
        assert len(results) == 1
        assert "caption" in results[0]["matches"]


def test_caption_save_empty_clears(auth_client, app):
    _upload_image(auth_client)
    with app.app_context():
        fid = _one_file().id
    auth_client.post(f"/file/{fid}/caption", json={"caption": "something"})
    resp = auth_client.post(f"/file/{fid}/caption", json={"caption": "  "})
    assert resp.get_json() == {"caption": ""}
    with app.app_context():
        assert _one_file().index.caption is None


def test_caption_save_rejects_non_image(auth_client, app):
    auth_client.post("/upload", data={
        "files": (io.BytesIO(b"plain text"), "notes.txt"),
    }, content_type="multipart/form-data", follow_redirects=True)
    with app.app_context():
        fid = _one_file().id
    resp = auth_client.post(f"/file/{fid}/caption", json={"caption": "nope"})
    assert resp.status_code == 400


def test_caption_get_returns_current(auth_client, app):
    """The file view fetches this on open to defeat SPA page-cache staleness."""
    _upload_image(auth_client)
    with app.app_context():
        fid = _one_file().id
    resp = auth_client.get(f"/file/{fid}/caption")
    assert resp.get_json() == {"caption": ""}
    auth_client.post(f"/file/{fid}/caption", json={"caption": "a lighthouse"})
    resp = auth_client.get(f"/file/{fid}/caption")
    assert resp.get_json() == {"caption": "a lighthouse"}


def test_caption_ui_in_view(auth_client, app):
    _upload_image(auth_client)
    with app.app_context():
        fid = _one_file().id
    resp = auth_client.get(f"/file/{fid}/view")
    assert b"caption-section" in resp.data
    assert b"caption.js" in resp.data
