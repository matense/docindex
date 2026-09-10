"""Template-attribute tests: the drive view items must carry the data-*
hooks that drive.js uses for drag & drop, multi-select and the context menu."""

import io
import re

from app.extensions import db
from app.models import Drive, StoredFile


def _upload(client, name, content):
    return client.post("/upload", data={
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def _file_tag(html, file_id):
    """The full HTML tag that carries data-file-id="<id>"."""
    m = re.search(r'<[^>]*data-file-id="%d"[^>]*>' % file_id, html)
    assert m, f"file {file_id} not rendered"
    return m.group(0)


def _folder_tag(html, folder_id):
    m = re.search(r'<[^>]*data-folder-sel-id="%d"[^>]*>' % folder_id, html)
    assert m, f"folder {folder_id} not rendered"
    return m.group(0)


def test_items_carry_interaction_attributes_in_all_views(auth_client, app):
    auth_client.post("/folder/create", data={"name": "docs"},
                     follow_redirects=True)
    _upload(auth_client, "notes.txt", b"editable text content")
    _upload(auth_client, "data.pdf", b"%PDF-1.4 fake")  # pdf: not editable

    with app.app_context():
        txt = StoredFile.query.filter_by(name="notes.txt").one()
        bin_ = StoredFile.query.filter_by(name="data.pdf").one()
        from app.models import Folder
        folder = Folder.query.filter_by(name="docs").one()
        ids = (txt.id, bin_.id, folder.id)

    for view in ("grid", "list", "tree"):
        html = auth_client.get(f"/?view={view}").data.decode()
        txt_tag = _file_tag(html, ids[0])
        bin_tag = _file_tag(html, ids[1])
        folder_tag = _folder_tag(html, ids[2])
        # Drag hooks: files AND folders are draggable.
        assert 'draggable="true"' in txt_tag
        assert 'draggable="true"' in folder_tag
        # Editable flag: present on .txt, absent on .bin.
        assert 'data-editable="1"' in txt_tag
        assert 'data-editable="1"' not in bin_tag
        # Not a synced drive.
        assert 'data-drive-synced' not in html
        assert 'data-is-synced' not in html


def test_synced_drive_marks_container_and_items_readonly(auth_client, app, tmp_path):
    (tmp_path / "hello.txt").write_text("hello synced", encoding="utf-8")
    auth_client.post("/drives/sync-create", data={"path": str(tmp_path)},
                     follow_redirects=True)
    with app.app_context():
        drive = Drive.query.filter(Drive.source_path.isnot(None)).one()
        drive_id = drive.id
        file_id = StoredFile.query.filter_by(name="hello.txt").one().id
    auth_client.post(f"/drives/{drive_id}/select", follow_redirects=True)

    html = auth_client.get("/?view=grid").data.decode()
    assert 'data-drive-synced="1"' in html
    tag = _file_tag(html, file_id)
    assert 'data-is-synced="1"' in tag
    # Synced files are never editable, even with an editable extension.
    assert 'data-editable="1"' not in tag
