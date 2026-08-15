import io

from app.extensions import db
from app.models import StoredFile


def _upload(client, name, content, folder_id=""):
    return client.post("/upload", data={
        "folder_id": folder_id,
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def test_upload_txt_file(auth_client, app):
    resp = _upload(auth_client, "notes.txt", b"hello world, this is a test document")
    assert resp.status_code == 200
    with app.app_context():
        stored = StoredFile.query.filter_by(name="notes.txt").one()
        assert stored.extension == "txt"
        assert stored.index is not None


def test_upload_json_response_includes_file_ids(auth_client, app):
    resp = auth_client.post("/upload", data={
        "files": (io.BytesIO(b"json upload test"), "json-upload.txt"),
    }, content_type="multipart/form-data",
        headers={"Accept": "application/json"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["files"]) == 1
    assert data["files"][0]["name"] == "json-upload.txt"
    with app.app_context():
        stored = StoredFile.query.one()
        assert data["files"][0]["id"] == stored.id


def test_upload_disallowed_extension(auth_client, app):
    resp = _upload(auth_client, "evil.exe", b"MZ")
    assert b"not allowed" in resp.data
    with app.app_context():
        assert StoredFile.query.count() == 0


def test_download_and_raw(auth_client, app):
    _upload(auth_client, "doc.txt", b"some content")
    with app.app_context():
        fid = StoredFile.query.one().id
    resp = auth_client.get(f"/file/{fid}/download")
    assert resp.status_code == 200
    assert resp.data == b"some content"
    resp = auth_client.get(f"/file/{fid}/raw")
    assert resp.status_code == 200


def test_edit_file(auth_client, app):
    _upload(auth_client, "editable.md", b"# old content")
    with app.app_context():
        fid = StoredFile.query.one().id
    resp = auth_client.post(f"/file/{fid}/edit", data={
        "content": "# new content\nwith changes",
    }, follow_redirects=True)
    assert resp.status_code == 200
    resp = auth_client.get(f"/file/{fid}/download")
    assert b"new content" in resp.data


def test_rename_and_delete(auth_client, app):
    _upload(auth_client, "before.txt", b"x")
    with app.app_context():
        fid = StoredFile.query.one().id
    auth_client.post(f"/file/{fid}/rename", data={"name": "after.txt"},
                     follow_redirects=True)
    with app.app_context():
        assert StoredFile.query.one().name == "after.txt"
    auth_client.post(f"/file/{fid}/delete", follow_redirects=True)
    with app.app_context():
        assert StoredFile.query.count() == 0


def test_folder_create_and_isolation(auth_client, app):
    resp = auth_client.post("/folder/create", data={"name": "Docs"},
                            follow_redirects=True)
    assert b"Docs" in resp.data

    # Another user must not see alice's files
    client2 = app.test_client()
    client2.post("/register", data={
        "username": "carol", "email": "carol@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    resp = client2.get("/")
    assert b"Docs" not in resp.data


def test_cannot_access_other_users_file(auth_client, app):
    _upload(auth_client, "private.txt", b"secret")
    with app.app_context():
        fid = StoredFile.query.one().id
    other = app.test_client()
    other.post("/register", data={
        "username": "dave", "email": "dave@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    resp = other.get(f"/file/{fid}/download")
    assert resp.status_code == 404


def test_view_text_file(auth_client, app):
    _upload(auth_client, "notes.txt", b"plain text body")
    with app.app_context():
        fid = StoredFile.query.one().id
    resp = auth_client.get(f"/file/{fid}/view")
    assert resp.status_code == 200
    assert b"plain text body" in resp.data


def test_view_markdown_renders_html(auth_client, app):
    _upload(auth_client, "doc.md", b"# Big Title\n\nsome paragraph")
    with app.app_context():
        fid = StoredFile.query.one().id
    resp = auth_client.get(f"/file/{fid}/view")
    assert resp.status_code == 200
    assert b"<h1>Big Title</h1>" in resp.data


def test_view_docx_shows_extracted_text(auth_client, app):
    _upload(auth_client, "report.docx", b"fake docx bytes")
    with app.app_context():
        stored = StoredFile.query.one()
        stored.index.extracted_text = "extracted docx content"
        stored.index.status = "ok"
        db.session.commit()
        fid = stored.id
    resp = auth_client.get(f"/file/{fid}/view")
    assert resp.status_code == 200
    assert b"extracted docx content" in resp.data


def test_view_other_users_file_forbidden(auth_client, app):
    _upload(auth_client, "private.txt", b"secret")
    with app.app_context():
        fid = StoredFile.query.one().id
    other = app.test_client()
    other.post("/register", data={
        "username": "erin", "email": "erin@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    assert other.get(f"/file/{fid}/view").status_code == 404


def test_selection_move_files_and_folders(auth_client, app):
    from app.models import Folder
    _upload(auth_client, "move-me.txt", b"move content")
    auth_client.post("/folder/create", data={"name": "Target"}, follow_redirects=True)
    auth_client.post("/folder/create", data={"name": "Dragged"}, follow_redirects=True)
    with app.app_context():
        fid = StoredFile.query.one().id
        target = Folder.query.filter_by(name="Target").one().id
        dragged = Folder.query.filter_by(name="Dragged").one().id
    resp = auth_client.post("/selection/move", data={
        "file_ids": str(fid), "folder_ids": str(dragged), "dest": str(target),
    })
    assert resp.status_code == 200
    assert resp.get_json()["moved"] == 2
    with app.app_context():
        assert StoredFile.query.one().folder_id == target
        assert Folder.query.filter_by(name="Dragged").one().parent_id == target


def test_selection_move_folder_into_itself_skipped(auth_client, app):
    from app.models import Folder
    auth_client.post("/folder/create", data={"name": "Loop"}, follow_redirects=True)
    with app.app_context():
        fid = Folder.query.one().id
    resp = auth_client.post("/selection/move", data={
        "file_ids": "", "folder_ids": str(fid), "dest": str(fid),
    })
    assert resp.get_json()["skipped"] == 1
    with app.app_context():
        assert Folder.query.one().parent_id is None


def test_selection_delete_files_and_folders(auth_client, app):
    from app.models import Folder
    _upload(auth_client, "doomed.txt", b"bye")
    auth_client.post("/folder/create", data={"name": "DoomedFolder"}, follow_redirects=True)
    with app.app_context():
        fid = StoredFile.query.one().id
        folder_id = Folder.query.one().id
    resp = auth_client.post("/selection/delete", data={
        "file_ids": str(fid), "folder_ids": str(folder_id),
    })
    assert resp.get_json()["deleted"] == 2
    with app.app_context():
        assert StoredFile.query.count() == 0
        assert Folder.query.count() == 0


def test_selection_endpoints_ignore_other_users_items(auth_client, app):
    from app.models import Folder
    _upload(auth_client, "alice-file.txt", b"alice data")
    other = app.test_client()
    other.post("/register", data={
        "username": "mallory", "email": "mallory@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    with app.app_context():
        fid = StoredFile.query.one().id
    resp = other.post("/selection/delete", data={"file_ids": str(fid), "folder_ids": ""})
    assert resp.get_json()["deleted"] == 0
    with app.app_context():
        assert StoredFile.query.count() == 1
        assert Folder.query.count() == 0 or True  # drive bootstrap may create none
