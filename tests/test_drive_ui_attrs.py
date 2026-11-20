"""Template-attribute tests: the drive view items must carry the data-*
hooks that drive.js uses for drag & drop, multi-select and the context menu."""

import io
import re

import pytest

from app.extensions import db
from app.models import Drive, StoredFile, User


@pytest.fixture(autouse=True)
def _ui_attrs_tests_run_as_admin(app, user):
    """sync-create is admin-only: run this module's sync tests as an admin."""
    with app.app_context():
        db.session.get(User, user).is_admin = True
        db.session.commit()


def _upload(client, name, content):
    return client.post("/upload", data={
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def _file_tag(html, file_id):
    """The full HTML tag that carries data-file-id="<id>"."""
    m = re.search(f'<[^>]*data-file-id="{file_id}"[^>]*>', html)
    assert m, f"file {file_id} not rendered"
    return m.group(0)


def _folder_tag(html, folder_id):
    m = re.search(f'<[^>]*data-folder-sel-id="{folder_id}"[^>]*>', html)
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
        assert 'data-drive-readonly' not in html
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
    assert 'data-drive-readonly="1"' in html
    tag = _file_tag(html, file_id)
    assert 'data-is-synced="1"' in tag
    # Synced files are never editable, even with an editable extension.
    assert 'data-editable="1"' not in tag


def test_shared_drive_marks_container_writable(auth_client, app, tmp_path):
    (tmp_path / "hello.txt").write_text("hello shared", encoding="utf-8")
    auth_client.post("/drives/sync-create",
                     data={"path": str(tmp_path), "sync_mode": "shared"},
                     follow_redirects=True)
    with app.app_context():
        drive = Drive.query.filter(Drive.source_path.isnot(None)).one()
        assert drive.is_shared and not drive.is_readonly
        drive_id = drive.id
        file_id = StoredFile.query.filter_by(name="hello.txt").one().id
    auth_client.post(f"/drives/{drive_id}/select", follow_redirects=True)

    html = auth_client.get("/?view=grid").data.decode()
    assert 'data-drive-readonly' not in html
    tag = _file_tag(html, file_id)
    assert 'data-is-synced="1"' not in tag
    # Shared .txt files are editable in place.
    assert 'data-editable="1"' in tag


# --- X1: drive viewer + guards parity (I1/I2) ---

def _make_mirror_file(app, user, tmp_path, name="hello.txt",
                      content="hello synced"):
    """A mirror-synced (read-only) drive + editable file + one version,
    built directly in the DB (no sync machinery)."""
    from app.models import Drive, FileVersion
    (tmp_path / name).write_text(content, encoding="utf-8")
    with app.app_context():
        drive = Drive(name="RO", user_id=user, source_path=str(tmp_path))
        db.session.add(drive)
        db.session.flush()
        stored = StoredFile(
            name=name, stored_name=f"ro-{name}", extension="txt",
            mime_type="text/plain", size=len(content),
            user_id=user, drive_id=drive.id,
            source_path=str(tmp_path / name))
        db.session.add(stored)
        db.session.flush()
        db.session.add(FileVersion(
            file_id=stored.id, version=1, stored_name=f"ro-{name}-v1",
            size=len(content), source="upload"))
        db.session.commit()
        return drive.id, stored.id


def test_writable_file_view_shows_restore(auth_client, app):
    """Control: an editable file with history offers Restore."""
    _upload(auth_client, "notes.txt", b"version one")
    _upload(auth_client, "notes.txt", b"version two")
    with app.app_context():
        fid = StoredFile.query.filter_by(name="notes.txt").one().id
    resp = auth_client.get(f"/file/{fid}/view")
    assert resp.status_code == 200
    assert b"Restore" in resp.data


def test_readonly_mirror_view_hides_restore(auth_client, app, user, tmp_path):
    """Read-only mirror file: no Restore entry (the route would 400)."""
    _did, fid = _make_mirror_file(app, user, tmp_path)
    resp = auth_client.get(f"/file/{fid}/view")
    assert resp.status_code == 200
    assert b"read-only" in resp.data  # the synced banner still explains
    assert b"Restore" not in resp.data


def test_info_reports_readonly_for_mirror_file(auth_client, app, user,
                                               tmp_path):
    """The info modal payload carries the writability flag drive.js gates
    the merge entry on."""
    _did, fid = _make_mirror_file(app, user, tmp_path)
    data = auth_client.get(f"/file/{fid}/info").get_json()
    assert data["is_readonly"] is True


def test_info_reports_writable_for_regular_file(auth_client, app):
    _upload(auth_client, "notes.txt", b"plain text body")
    with app.app_context():
        fid = StoredFile.query.filter_by(name="notes.txt").one().id
    data = auth_client.get(f"/file/{fid}/info").get_json()
    assert data["is_readonly"] is False
