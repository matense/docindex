"""Notebooks module: document service, routes, history, indexing and AI tools."""

import json

from app.extensions import db
from app.models import Drive, FileIndex, ModuleState, StoredFile, User
from app.services import file_service, indexing_service, search_service

import importlib.util
import os

import pytest

# Load the module package directly (it lives outside the app package).
_NB_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "modules", "notebooks")
_spec = importlib.util.spec_from_file_location(
    "docindex_module_notebooks", os.path.join(_NB_DIR, "__init__.py"),
    submodule_search_locations=[_NB_DIR])
_nb = importlib.util.module_from_spec(_spec)
import sys
sys.modules.setdefault("docindex_module_notebooks", _nb)
_spec.loader.exec_module(_nb)
nb_doc = importlib.import_module("docindex_module_notebooks.doc")
nb_tools = importlib.import_module("docindex_module_notebooks.tools")


@pytest.fixture()
def enabled(app):
    with app.app_context():
        db.session.add(ModuleState(name="notebooks", enabled=True))
        db.session.commit()


def _drive(app, user_id, synced=False):
    with app.app_context():
        d = Drive(name="Personal", user_id=user_id,
                  source_path="/tmp/real" if synced else None)
        db.session.add(d)
        db.session.commit()
        return d.id


def _create(app, user_id, drive_id, name="Notes", cells=None):
    with app.app_context():
        user = db.session.get(User, user_id)
        drive = db.session.get(Drive, drive_id)
        stored = nb_doc.create_notebook(user, drive, name, cells=cells)
        indexing_service.index_file(stored.id, app)  # tests run inline
        return stored.id


def test_create_notebook_indexes_and_searchable(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Shopping ideas",
                  cells=[nb_doc.new_cell("markdown", "buy oat milk and coffee")])
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert stored.extension == "pdocnb"
        assert stored.drive_id == drive_id
        index = db.session.get(FileIndex, fid) or stored.index
        assert index.status == "ok"
        assert "oat milk" in index.extracted_text
        user_obj = db.session.get(User, user)
        results = search_service.search_files("oat milk", user_obj)
        assert any(r["file"].id == fid for r in results)


def test_create_refuses_synced_drive(app, user):
    drive_id = _drive(app, user, synced=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        with pytest.raises(ValueError):
            nb_doc.create_notebook(user_obj, drive, "Nope")


def test_save_throttles_snapshots_and_forces_checkpoints(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        document = nb_doc.load(stored)

        nb_doc.save_document(stored, document)  # first save -> snapshot v1
        assert len(stored.versions) == 1

        document["cells"][0]["content"] = "changed"
        nb_doc.save_document(stored, document)  # within 2 min -> no snapshot
        assert len(stored.versions) == 1

        nb_doc.save_document(stored, document, force_checkpoint=True)
        assert len(stored.versions) == 2
        assert nb_doc.load(stored)["cells"][0]["content"] == "changed"


def test_extractor_flattens_all_cell_types(app, user):
    drive_id = _drive(app, user)
    cells = [
        nb_doc.new_cell("heading", "Quarterly plan", {"level": 1}),
        nb_doc.new_cell("heading", "Q3 goals", {"level": 2}),
        nb_doc.new_cell("markdown", "# Title\nsome prose"),
        nb_doc.new_cell("richtext", "<h1>Bold claim</h1><p>rich <b>text</b></p>"),
        nb_doc.new_cell("code", "print('hello')", {"language": "python"}),
        nb_doc.new_cell("separator"),
        nb_doc.new_cell("todo", meta={"items": [{"text": "call mom",
                                                 "done": False}]}),
        nb_doc.new_cell("table", meta={"headers": ["A", "B"],
                                       "rows": [["1", "2"]]}),
    ]
    fid = _create(app, user, drive_id, "Mixed", cells=cells)
    with app.app_context():
        text = nb_doc.extract(db.session.get(StoredFile, fid))
    assert "Quarterly plan" in text and "Q3 goals" in text
    assert "some prose" in text
    assert "Bold claim" in text and "rich text" in text
    assert "<b>" not in text  # HTML is stripped for the index
    assert "print('hello')" in text
    assert "call mom" in text
    assert "A | B" in text and "1 | 2" in text


def test_legacy_title_subtitle_cells_migrate_to_heading(app):
    doc = nb_doc.validate({"cells": [
        {"type": "title", "content": "Old title"},
        {"type": "subtitle", "content": "Old subtitle"},
    ]})
    assert doc["cells"][0]["type"] == "heading"
    assert doc["cells"][0]["meta"]["level"] == 1
    assert doc["cells"][1]["type"] == "heading"
    assert doc["cells"][1]["meta"]["level"] == 2


def test_editor_routes_require_enabled_module(app, auth_client, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    # disabled by default -> guard 404s
    assert auth_client.get(f"/m/notebooks/{fid}").status_code == 404
    with app.app_context():
        db.session.add(ModuleState(name="notebooks", enabled=True))
        db.session.commit()
    resp = auth_client.get(f"/m/notebooks/{fid}")
    assert resp.status_code == 200
    assert b"nb-cells" in resp.data


def test_save_endpoint_roundtrip(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    document = {"version": 1, "title": "Notes",
                "cells": [nb_doc.new_cell("markdown", "hello from autosave")]}
    resp = auth_client.post(f"/m/notebooks/{fid}/save",
                            json={"doc": document})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
        assert loaded["cells"][0]["content"] == "hello from autosave"


def test_save_rejects_invalid_document(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    resp = auth_client.post(f"/m/notebooks/{fid}/save",
                            json={"doc": {"cells": [{"type": "nope"}]}})
    assert resp.status_code == 400


def test_notebook_isolation_between_users(app, auth_client, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        other = User(username="bob", email="bob@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.add(ModuleState(name="notebooks", enabled=True))
        db.session.commit()
    client2 = app.test_client()
    client2.post("/login", data={"username": "bob", "password": "password123"})
    assert client2.get(f"/m/notebooks/{fid}").status_code == 404


def test_history_restore_via_route(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        document = nb_doc.load(stored)
        document["cells"][0]["content"] = "version two"
        nb_doc.save_document(stored, document, force_checkpoint=True)
        version_id = stored.versions[0].id  # snapshot of the original content
    resp = auth_client.post(f"/m/notebooks/{fid}/history/{version_id}/restore")
    assert resp.status_code == 302
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
        assert loaded["cells"][0]["content"] != "version two"


def test_viewer_redirect_from_generic_view(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    resp = auth_client.get(f"/file/{fid}/view")
    assert resp.status_code == 302
    assert f"/m/notebooks/{fid}" in resp.headers["Location"]


def test_ai_tools_create_and_edit(app, user, enabled):
    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = nb_tools._tool_create(user_obj, {"name": "AI notes"}, None)
        fid = result["created"]["id"]

        cell = nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "AI was here"}, None)
        assert cell["ok"] is True
        cell_id = cell["cell"]["id"]

        updated = nb_tools._tool_update_cell(
            user_obj, {"file_id": fid, "cell_id": cell_id,
                       "content": "AI rewrote this"}, None)
        assert updated["ok"] is True

        read = nb_tools._tool_read(user_obj, {"file_id": fid}, None)
        contents = [c["content"] for c in read["cells"]]
        assert "AI rewrote this" in contents

        deleted = nb_tools._tool_delete_cell(
            user_obj, {"file_id": fid, "cell_id": cell_id}, None)
        assert deleted["ok"] is True

        # Transactional: all three AI mutations in this run share ONE
        # snapshot of the pre-edit state, labelled as an AI answer.
        stored = db.session.get(StoredFile, fid)
        assert len(stored.versions) == 1
        assert stored.versions[0].source == "ai"
        assert stored.versions[0].note == "AI edits"


def test_ai_edits_in_a_new_run_create_a_new_block(app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    # First "AI answer": two mutations, one app/request context.
    with app.app_context():
        user_obj = db.session.get(User, user)
        nb_tools._tool_add_cell(user_obj, {"file_id": fid,
                                           "cell_type": "markdown",
                                           "content": "one"}, None)
        nb_tools._tool_add_cell(user_obj, {"file_id": fid,
                                           "cell_type": "markdown",
                                           "content": "two"}, None)
        assert len(db.session.get(StoredFile, fid).versions) == 1
    # Second "AI answer": a fresh request context starts a new block.
    with app.app_context():
        user_obj = db.session.get(User, user)
        nb_tools._tool_add_cell(user_obj, {"file_id": fid,
                                           "cell_type": "markdown",
                                           "content": "three"}, None)
        versions = db.session.get(StoredFile, fid).versions
        assert len(versions) == 2
        assert all(v.source == "ai" for v in versions)


def test_history_diff_is_cell_level(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Diff me",
                  cells=[nb_doc.new_cell("markdown", "original text"),
                         nb_doc.new_cell("todo", meta={
                             "items": [{"text": "old task", "done": False}]})])
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        document = nb_doc.load(stored)
        nb_doc.update_cell(document, document["cells"][0]["id"],
                           content="rewritten text")
        nb_doc.delete_cell(document, document["cells"][1]["id"])
        nb_doc.add_cell(document, "markdown", "brand new cell")
        nb_doc.save_document(stored, document, force_checkpoint=True)
        version_id = stored.versions[0].id
    resp = auth_client.get(f"/m/notebooks/{fid}/history/{version_id}/diff")
    assert resp.status_code == 200
    body = resp.data
    assert b"1 added" in body and b"1 changed" in body and b"1 removed" in body
    assert b"rewritten text" in body   # changed cell, new side
    assert b"original text" in body    # changed cell, old side
    assert b"old task" in body         # removed todo cell rendered as text
    assert b"brand new cell" in body   # added cell


def test_ai_tools_refuse_synced_drive(app, user, enabled):
    drive_id = _drive(app, user, synced=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        result = nb_tools._tool_create(user_obj, {"name": "X"}, drive)
        assert "error" in result


def test_tools_hidden_when_module_disabled(app, user):
    from app.services import agent_service
    with app.app_context():
        _defs, handlers, _labels = agent_service._active_tools()
        assert "notebooks.create" not in handlers
        db.session.add(ModuleState(name="notebooks", enabled=True))
        db.session.commit()
        _defs, handlers, _labels = agent_service._active_tools()
        assert "notebooks.create" in handlers


def test_doc_json_endpoint_tracks_changes(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    resp = auth_client.get(f"/m/notebooks/{fid}/doc")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True and data["checksum"]

    # An external edit (e.g. an AI tool) changes the checksum the editor polls.
    with app.app_context():
        user_obj = db.session.get(User, user)
        cell = nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "heading",
                       "content": "Added by AI"}, None)
        assert cell["ok"] is True
    data2 = auth_client.get(f"/m/notebooks/{fid}/doc").get_json()
    assert data2["checksum"] != data["checksum"]
    assert any(c["content"] == "Added by AI" and c["type"] == "heading"
               for c in data2["doc"]["cells"])
