"""Notebooks module: document service, routes, history, indexing and AI tools."""


import importlib.util
import copy
import io
import os
import subprocess
import sys

import pytest

from app.extensions import db
from app.models import Drive, FileIndex, ModuleState, StoredFile, User
from app.services import file_service, indexing_service, search_service

# Load the module package directly (it lives outside the app package).
_NB_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "modules", "notebooks")
_spec = importlib.util.spec_from_file_location(
    "docindex_module_notebooks", os.path.join(_NB_DIR, "__init__.py"),
    submodule_search_locations=[_NB_DIR])
_nb = importlib.util.module_from_spec(_spec)

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


# ---------------------------------------------------------------------------
# Organize: "DocIndex Notebooks" folder + plan/apply/revert
# ---------------------------------------------------------------------------

def test_notebook_uuid_assigned_and_backfilled(app, user):
    import uuid as _uuid
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Uuid")
    with app.app_context():
        first = db.session.get(StoredFile, fid).uuid
        assert first and len(first) == 36
        _uuid.UUID(first)  # parses as a UUID
        # Simulate a pre-UUID notebook: cleared, then regenerated on access.
        db.session.get(StoredFile, fid).uuid = None
        db.session.commit()
    with app.app_context():
        assert nb_doc.get_notebook(fid, user).uuid is not None
        db.session.get(StoredFile, fid).uuid = None
        db.session.commit()
        nb_doc.list_notebooks(user)
        second = db.session.get(StoredFile, fid).uuid
        assert second and second != first
    # Globally unique across many notebooks.
    with app.app_context():
        seen = {db.session.get(StoredFile, fid).uuid}
        for i in range(5):
            seen.add(db.session.get(
                StoredFile, _create(app, user, drive_id, f"N{i}")).uuid)
        assert len(seen) == 6


def test_notebooks_folder_created_per_drive(app, user):
    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        folder = nb_doc.notebooks_folder(user_obj, drive)
        assert folder.name == "DocIndex Notebooks"
        assert folder.parent_id is None
        assert folder.drive_id == drive_id
        # idempotent — second call returns the same folder
        assert nb_doc.notebooks_folder(user_obj, drive).id == folder.id
        synced = Drive(name="RO", user_id=user, source_path="/tmp/ro")
        db.session.add(synced)
        db.session.commit()
        assert nb_doc.notebooks_folder(user_obj, synced) is None


def test_organize_plan_apply_and_revert(app, auth_client, user, enabled):
    from app.models import NotebookOrganizePlan
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Loose")
    with app.app_context():
        assert db.session.get(StoredFile, fid).folder_id is None

    data = auth_client.get("/m/notebooks/organize/plan").get_json()
    assert len(data["moves"]) == 1
    assert data["skipped_synced"] == 0
    assert data["moves"][0]["to_folder_name"] == "DocIndex Notebooks"

    resp = auth_client.post("/m/notebooks/organize/apply",
                            follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert stored.folder.name == "DocIndex Notebooks"
        plan = NotebookOrganizePlan.query.one()
        assert plan.applied is True and plan.reverted is False

    # Plan is now empty; revert moves the notebook back to the root.
    assert auth_client.get("/m/notebooks/organize/plan").get_json()[
        "moves"] == []
    auth_client.post("/m/notebooks/organize/revert", follow_redirects=True)
    with app.app_context():
        assert db.session.get(StoredFile, fid).folder_id is None
        assert NotebookOrganizePlan.query.one().reverted is True

    # Nothing left to revert.
    resp = auth_client.post("/m/notebooks/organize/revert",
                            follow_redirects=True)
    assert b"No organize run" in resp.data


def test_organize_ignores_notebooks_inside_dedicated_folder(app, auth_client,
                                                           user, enabled):
    from app.models import Folder
    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        root = nb_doc.notebooks_folder(user_obj, drive)
        sub = Folder(user_id=user, drive_id=drive_id, parent_id=root.id,
                     name="Projects")
        db.session.add(sub)
        db.session.flush()
        sub_id = sub.id
        db.session.commit()
    fid = _create(app, user, drive_id, "Nested")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        stored.folder_id = sub_id
        db.session.commit()
    data = auth_client.get("/m/notebooks/organize/plan").get_json()
    assert data["moves"] == []


def test_notebook_list_grouped_by_drive(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    _create(app, user, drive_id, "One")
    data = auth_client.get("/m/notebooks/").data.decode()
    assert "Personal" in data
    assert "1 notebook" in data
    # Drive sections are collapsible <details> with memory.
    assert "nb-drive-section" in data
    assert f'data-drive-id="{drive_id}"' in data
    assert "nb-drive-collapsed" in data


def test_organize_skips_synced_drives(app, auth_client, user, enabled):
    drive_id = _drive(app, user, synced=True)
    with app.app_context():
        # Notebook file placed directly (creation refuses synced drives).
        stored = StoredFile(name="RO.pdocnb", stored_name="ro123",
                            extension="pdocnb", mime_type="application/json",
                            size=10, checksum="x", user_id=user,
                            drive_id=drive_id)
        db.session.add(stored)
        db.session.commit()
    data = auth_client.get("/m/notebooks/organize/plan").get_json()
    assert data["moves"] == [] and data["skipped_synced"] == 1


def test_new_notebook_defaults_to_notebooks_folder(app, auth_client, user,
                                                    enabled):
    drive_id = _drive(app, user)
    auth_client.post("/m/notebooks/new",
                     data={"name": "Fresh", "location": f"drive:{drive_id}"},
                     follow_redirects=True)
    with app.app_context():
        stored = (StoredFile.query
                  .filter_by(user_id=user, extension="pdocnb").one())
        assert stored.folder.name == "DocIndex Notebooks"
        assert stored.folder.drive_id == drive_id


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


def test_extractor_keeps_tex_source_and_admonitions(app, user):
    """Markdown TeX source and admonition text flow through extract()
    unchanged — regression lock while chart extraction lands in Task 8."""
    drive_id = _drive(app, user)
    cells = [
        nb_doc.new_cell("markdown", "$$E=mc^2$$"),
        nb_doc.new_cell("markdown", r"$\alpha$"),
        nb_doc.new_cell("markdown", "costs $5"),
        nb_doc.new_cell("markdown", "::: notice\nPay attention!"),
    ]
    fid = _create(app, user, drive_id, "Tex", cells=cells)
    with app.app_context():
        text = nb_doc.extract(db.session.get(StoredFile, fid))
    assert "$$E=mc^2$$" in text
    assert r"$\alpha$" in text
    assert "costs $5" in text
    assert "::: notice" in text
    assert "Pay attention!" in text


# ---------------------------------------------------------------------------
# Chart cells (backend validation, extraction, history)
# ---------------------------------------------------------------------------

def test_chart_cell_validates_and_caps(app, user):
    labels = [f"L{i}" for i in range(60)]
    datasets = []
    for i in range(600):
        data = []
        for j in range(60):
            if j == 0:
                data.append("12")
            elif j == 1:
                data.append("oops")
            else:
                data.append(float(j))
        datasets.append({"label": f"Series {i}", "data": data})
    chart = {"kind": "notachart", "labels": labels,
             "datasets": datasets, "title": "Big chart"}
    doc = {"version": 1, "title": "Chart test",
           "cells": [{"type": "chart", "meta": {"chart": chart}}]}
    validated = nb_doc.validate(doc)
    chart_meta = validated["cells"][0]["meta"]["chart"]
    assert chart_meta["kind"] == "bar"
    assert len(chart_meta["labels"]) == 50
    assert len(chart_meta["datasets"]) == 500
    assert all(len(ds["data"]) == 50 for ds in chart_meta["datasets"])
    first = chart_meta["datasets"][0]
    assert first["data"][0] == 12.0
    assert first["data"][1] == 0.0
    assert isinstance(first["data"][2], float)
    assert chart_meta["title"] == "Big chart"

    valid = nb_doc.validate({"version": 1, "title": "T",
        "cells": [{"type": "chart",
                   "meta": {"chart": {"kind": "line",
                                       "labels": ["a"],
                                       "datasets": [{"label": "A",
                                                     "data": [1]}],
                                       "title": None}}}]})

    assert valid["cells"][0]["meta"]["chart"]["kind"] == "line"


def test_chart_cell_rejected_when_malformed(app, auth_client, user, enabled):
    malformed = [
        {"version": 1, "title": "T",
         "cells": [{"type": "chart", "meta": {}}]},
        {"version": 1, "title": "T",
         "cells": [{"type": "chart", "meta": {"chart": "x"}}]},
        {"version": 1, "title": "T",
         "cells": [{"type": "chart",
                    "meta": {"chart": {"kind": "bar",
                                        "labels": "abc",
                                        "datasets": []}}}]},
    ]
    for doc in malformed:
        with pytest.raises(ValueError):
            nb_doc.validate(doc)

    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    document = {"version": 1, "title": "T",
                "cells": [{"type": "chart", "meta": {}}]}
    resp = auth_client.post(f"/m/notebooks/{fid}/save", json={"doc": document})
    assert resp.status_code == 400


def test_extractor_includes_chart_labels(app, user):
    drive_id = _drive(app, user)
    cells = [nb_doc.new_cell("chart", meta={
        "chart": {"kind": "pie", "labels": ["alpha", "beta"],
                  "datasets": [{"label": "A", "data": [1, 2]}],
                  "title": "Pie chart"}})]
    fid = _create(app, user, drive_id, "Charted", cells=cells)
    with app.app_context():
        text = nb_doc.extract(db.session.get(StoredFile, fid))
    assert "alpha" in text
    assert "beta" in text
    assert "alpha | beta" in text
    assert "Pie chart" in text


def test_history_diff_renders_chart_cells(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    cells = [nb_doc.new_cell("chart", meta={
        "chart": {"kind": "bar", "labels": ["alpha", "<img src=x>"],
                  "datasets": [{"label": "A", "data": [1, 2]}],
                  "title": "Trends"}})]
    fid = _create(app, user, drive_id, "Hist chart", cells=cells)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        document = nb_doc.load(stored)
        nb_doc.update_cell(
            document, document["cells"][0]["id"],
            meta={"chart": {"kind": "bar", "labels": ["gamma"],
                            "datasets": [{"label": "A", "data": [3]}],
                            "title": "Trends"}})
        nb_doc.save_document(stored, document, force_checkpoint=True)
        version_id = stored.versions[0].id
    resp = auth_client.get(
        f"/m/notebooks/{fid}/history/{version_id}/diff")
    assert resp.status_code == 200
    body = resp.data
    assert b"chart" in body
    assert b"alpha" in body
    assert b"&lt;img src=x&gt;" in body
    assert b"<img" not in body


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
    # New save contract: every save carries the base it was built on.
    base = auth_client.get(f"/m/notebooks/{fid}/doc").get_json()
    document = copy.deepcopy(base["doc"])
    document["cells"][0]["content"] = "hello from autosave"
    resp = auth_client.post(
        f"/m/notebooks/{fid}/save",
        json={"doc": document, "base_checksum": base["checksum"],
              "base_doc": base["doc"]})
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


def test_ai_tool_create_coerces_string_cells(app, user, enabled):
    """The model often sends plain strings instead of cell objects — coerce
    them to markdown cells instead of crashing (regression: AttributeError
    on str.get killed the whole run with no visible error)."""
    _ = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = nb_tools._tool_create(
            user_obj,
            {"name": "AI notes",
             "cells": ["hello", {"type": "markdown", "content": "world"}]},
            None)
        assert "created" in result
        fid = result["created"]["id"]
        read = nb_tools._tool_read(user_obj, {"file_id": fid}, None)
        assert [c["content"] for c in read["cells"]] == ["hello", "world"]
        assert all(c["type"] == "markdown" for c in read["cells"])
        bad = nb_tools._tool_create(user_obj, {"name": "Bad", "cells": [123]}, None)
        assert "error" in bad
        # A bare (non-list) string is one markdown cell, not five char cells;
        # a dict is rejected with a clean error instead of silent garbage.
        solo = nb_tools._tool_create(
            user_obj, {"name": "Solo", "cells": "just this"}, None)
        assert "created" in solo
        read_solo = nb_tools._tool_read(
            user_obj, {"file_id": solo["created"]["id"]}, None)
        assert [c["content"] for c in read_solo["cells"]] == ["just this"]
        notalist = nb_tools._tool_create(
            user_obj, {"name": "Nope", "cells": {"type": "markdown"}},
            None)
        assert "error" in notalist


def test_ai_tools_create_and_edit(app, user, enabled):
    _ = _drive(app, user)
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


def test_ai_create_notebook_with_math(app, user, enabled):
    """The create tool accepts math cells and the schema tells the model how
    to write math ($$…$$ blocks and \\(…\\) inline; bare $ is not math)."""
    from app.services import agent_service
    _ = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = nb_tools._tool_create(
            user_obj,
            {"name": "Physics notes",
             "cells": [{"type": "markdown", "content": "Energy: $$E=mc^2$$"}]},
            None)
        assert "created" in result
        fid = result["created"]["id"]
        stored = db.session.get(StoredFile, fid)
        assert "E=mc^2" in nb_doc.extract(stored)

        defs, _handlers, _labels = agent_service._active_tools()
        create_desc = next(
            d["function"]["description"] for d in defs
            if d["function"]["name"] == "notebooks.create")
        add_desc = next(
            d["function"]["description"] for d in defs
            if d["function"]["name"] == "notebooks.add_cell")
        assert "$$" in create_desc and "\\(" in create_desc
        assert "$$" in add_desc and "\\(" in add_desc


def test_ai_create_notebook_with_chart(app, user, enabled):
    """The create/add_cell tools accept chart cells and their schemas document
    the exact JSON shape, allowed kinds and row/column caps."""
    from app.services import agent_service
    _ = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = nb_tools._tool_create(
            user_obj,
            {"name": "Chart notebook",
             "cells": [{"type": "chart",
                        "meta": {"chart": {"kind": "bar",
                                           "labels": ["a", "b"],
                                           "datasets": [{"label": "n",
                                                         "data": [1, 2]}]}}}]},
            None)
        assert "created" in result
        fid = result["created"]["id"]
        read = nb_tools._tool_read(user_obj, {"file_id": fid}, None)
        chart = read["cells"][0]["meta"]["chart"]
        assert chart["kind"] == "bar"
        assert chart["labels"] == ["a", "b"]
        assert chart["datasets"][0]["label"] == "n"
        assert chart["datasets"][0]["data"] == [1.0, 2.0]

        # Malformed chart structure (missing required chart object) returns an
        # error and creates nothing.
        result_bad = nb_tools._tool_create(
            user_obj,
            {"name": "Bad chart",
             "cells": [{"type": "chart", "meta": {}}]},
            None)
        assert "error" in result_bad
        assert "created" not in result_bad

        # The tool schemas mention chart cells, the allowed kinds and caps.
        defs, _handlers, _labels = agent_service._active_tools()
        create_desc = next(
            d["function"]["description"] for d in defs
            if d["function"]["name"] == "notebooks.create")
        add_desc = next(
            d["function"]["description"] for d in defs
            if d["function"]["name"] == "notebooks.add_cell")
        assert "chart" in create_desc
        assert "chart" in add_desc
        for kind in ("bar", "line", "pie"):
            assert kind in create_desc
            assert kind in add_desc
        assert "50" in create_desc
        assert "500" in create_desc


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


def test_ai_tools_multi_drive_list_context(app, user, enabled):
    """Chat context can be a list of drives ($ mentions): tools must not
    treat it as a single Drive (regression: 'list' object has no attribute
    'is_synced')."""
    synced_id = _drive(app, user, synced=True)
    writable_id = _drive(app, user)
    nb_id = _create(app, user, writable_id, name="In writable")
    with app.app_context():
        user_obj = db.session.get(User, user)
        drives = [db.session.get(Drive, synced_id),
                  db.session.get(Drive, writable_id)]
        # create picks the first writable drive in the list
        result = nb_tools._tool_create(user_obj, {"name": "Multi"}, drives)
        assert "created" in result
        assert result["created"]["id"] != nb_id
        created = db.session.get(StoredFile, result["created"]["id"])
        assert created.drive_id == writable_id
        # list filters by every drive in the list without crashing
        listing = nb_tools._tool_list(user_obj, {}, drives)
        assert listing["total"] == 2
        listing = nb_tools._tool_list(user_obj, {}, drives[:1])
        assert listing["total"] == 0
        # all-synced list: clear error instead of a crash
        result = nb_tools._tool_create(user_obj, {"name": "Y"}, drives[:1])
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


def _set_flags(app, fid, **flags):
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        document = nb_doc.load(stored)
        document.update(flags)
        nb_doc.save_document(stored, document, force_checkpoint=True)


def test_ai_lock_blocks_edits_but_allows_reads(app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Locked",
                  cells=[nb_doc.new_cell("markdown", "keep me")])
    _set_flags(app, fid, ai_lock=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        read = nb_tools._tool_read(user_obj, {"file_id": fid}, None)
        assert "error" not in read
        assert "keep me" in [c["content"] for c in read["cells"]]
        added = nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "nope"}, None)
        assert "locked" in added["error"]
        # Locked notebooks are still listed and readable.
        listing = nb_tools._tool_list(user_obj, {}, None)
        assert any(n["id"] == fid for n in listing["notebooks"])


def test_ai_hidden_blocks_everything(app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Secret",
                  cells=[nb_doc.new_cell("markdown", "hidden text")])
    _set_flags(app, fid, ai_hidden=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        read = nb_tools._tool_read(user_obj, {"file_id": fid}, None)
        assert "hidden" in read["error"]
        added = nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "nope"}, None)
        assert "hidden" in added["error"]
        listing = nb_tools._tool_list(user_obj, {}, None)
        assert not any(n["id"] == fid for n in listing["notebooks"])


def test_hidden_notebook_invisible_to_core_agent_tools(app, user, enabled):
    from app.services import agent_service
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Invisible",
                  cells=[nb_doc.new_cell("markdown", "zebra puzzle clue")])
    _set_flags(app, fid, ai_hidden=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = agent_service._tool_read_file(user_obj, fid)
        assert "hidden" in result["error"]
        grep = agent_service._tool_grep_file(user_obj, fid, "zebra")
        assert "hidden" in grep["error"]
        found = agent_service._tool_search_files(user_obj, "zebra puzzle")
        assert all(r["file_id"] != fid for r in found["results"])


def test_on_enable_creates_default_drive_once(app, user):
    from app.services import module_service
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert module_service.set_enabled("notebooks", True, user_obj)
        assert module_service.set_enabled("notebooks", True, user_obj)
        drives = Drive.query.filter_by(
            user_id=user, name=_nb.DEFAULT_DRIVE_NAME).all()
        assert len(drives) == 1


def test_upload_asset_accepts_images_only(app, auth_client, user, enabled):
    import io
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    resp = auth_client.post(
        f"/m/notebooks/{fid}/assets",
        data={"file": (io.BytesIO(b"\x89PNG\r\n\x1a\n"), "pic.png",
                       "image/png")},
        content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["url"].endswith(f"/file/{payload['id']}/raw")
    with app.app_context():
        asset = db.session.get(StoredFile, payload["id"])
        assert asset.drive_id == drive_id

    resp = auth_client.post(
        f"/m/notebooks/{fid}/assets",
        data={"file": (io.BytesIO(b"plain text"), "notes.txt",
                       "text/plain")},
        content_type="multipart/form-data")
    assert resp.status_code == 400


def test_history_diff_with_collapsed_unchanged_stretch(app, auth_client, user,
                                                       enabled):
    """Changed cells with >6 unchanged lines emit collapse markers (no 'cls'
    key) — the settings-change check must not KeyError on them."""
    drive_id = _drive(app, user)
    old_text = "\n".join(f"line {i}" for i in range(20))
    fid = _create(app, user, drive_id, "Long",
                  cells=[nb_doc.new_cell("markdown", old_text)])
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        document = nb_doc.load(stored)
        nb_doc.update_cell(document, document["cells"][0]["id"],
                           content=old_text.replace("line 10", "LINE TEN"))
        nb_doc.save_document(stored, document, force_checkpoint=True)
        version_id = stored.versions[0].id
    resp = auth_client.get(f"/m/notebooks/{fid}/history/{version_id}/diff")
    assert resp.status_code == 200
    assert b"LINE TEN" in resp.data and b"line 10" in resp.data
    assert b"unchanged line" in resp.data  # collapse marker rendered


def test_lock_state_visible_in_read_and_list(app, user, enabled):
    """The AI sees the lock up front in read/list responses, so it asks for
    an unlock instead of generating content it cannot save."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Flags",
                  cells=[nb_doc.new_cell("markdown", "body")])
    _set_flags(app, fid, ai_lock=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        read = nb_tools._tool_read(user_obj, {"file_id": fid}, None)
        assert read["ai_lock"] is True
        assert "unlock" in read["note"]
        listing = nb_tools._tool_list(user_obj, {}, None)
        entry = next(n for n in listing["notebooks"] if n["id"] == fid)
        assert entry["ai_lock"] is True
        # The refusal tells the AI not to generate content in chat.
        added = nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "nope"}, None)
        assert "Do not generate" in added["error"]


# ---------------------------------------------------------------------------
# Markdown viewer: rich server-side rendering + sanitization
# ---------------------------------------------------------------------------

def _upload(client, name, content, folder_id=""):
    return client.post("/upload", data={
        "folder_id": folder_id,
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def test_md_view_renders_footnotes_and_admonitions(auth_client, app):
    md = (b"Doc body[^1].\n\n"
          b"!!! note\n"
          b"    A notice block.\n\n"
          b"[^1]: The footnote text.\n")
    _upload(auth_client, "notebook.md", md)
    with app.app_context():
        fid = StoredFile.query.filter_by(name="notebook.md").one().id
    resp = auth_client.get(f"/file/{fid}/view")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "admonition" in body
    # Python-Markdown footnotes emit a footnote container plus an anchor back
    # to the definition; the exact wrapper element varies by version.
    assert 'class="footnote"' in body
    assert 'href="#fn:' in body


def test_md_view_bleach_still_strips_scripts(auth_client, app):
    md = (b"<script>alert(1)</script>\n"
          b"<iframe src=\"evil\"></iframe>\n"
          b"<img src=\"x\" onerror=\"alert(2)\">\n"
          b"[js](javascript:alert(3))\n"
          b"<figure><figcaption>Caption</figcaption></figure>\n"
          b"H<sub>2</sub>O and E=mc<sup>2</sup>\n")
    _upload(auth_client, "danger.md", md)
    with app.app_context():
        fid = StoredFile.query.filter_by(name="danger.md").one().id
    resp = auth_client.get(f"/file/{fid}/view")
    body = resp.data.decode()
    lowered = body.lower()
    # The surrounding page contains its own <script> tag; check the malicious
    # payload specifically rather than the substring "script".
    assert "<script>alert" not in lowered
    assert "<iframe" not in lowered
    assert "onerror=" not in lowered
    assert "javascript:" not in lowered
    assert "<figure>" in body
    assert "<figcaption>" in body
    assert "<sup>" in body
    assert "<sub>" in body


# ---------------------------------------------------------------------------
# D1: autosave vs AI live-edit race (P0 dataloss)
# ---------------------------------------------------------------------------

def _stale_editor_snapshot(app, fid):
    """Simulate the open editor: the doc + checksum it last saw."""
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        return nb_doc.load(stored), stored.checksum


def _post_save(client, fid, doc, base_checksum, base_doc):
    """POST /save under the new contract; base_doc=None omits the key."""
    payload = {"doc": doc}
    if base_checksum is not None:
        payload["base_checksum"] = base_checksum
    if base_doc is not None:
        payload["base_doc"] = base_doc
    return client.post(f"/m/notebooks/{fid}/save", json=payload)


def _js_fn_body(src, name):
    """Extract a `function name(...) {...}` body (brace-matched)."""
    start = src.index("function " + name + "(")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
    raise AssertionError(f"no body for {name}")


def test_user_edit_plus_ai_add_merges_first_try(app, auth_client, user,
                                                enabled):
    """Critical 1: the common case — user types while AI appends — must
    merge with 200 on the FIRST try, not 409-first-try."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race merge",
                  cells=[nb_doc.new_cell("markdown", "original")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    stale_doc["cells"][0]["content"] = "user edit"  # user types
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "AI wrote this"}, None)["ok"] is True
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 200  # merged first try — no 409 detour
    assert resp.get_json()["merged"] is True
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    contents = [c.get("content") for c in loaded["cells"]]
    assert "user edit" in contents and "AI wrote this" in contents


def test_autosave_never_wipes_concurrent_ai_edit(app, auth_client, user,
                                                  enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race",
                  cells=[nb_doc.new_cell("markdown", "user text")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    with app.app_context():
        user_obj = db.session.get(User, user)
        added = nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "AI wrote this"}, None)
        assert added["ok"] is True
    # The user's autosave of the stale doc must not wipe the AI cell.
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 200
    assert resp.get_json()["merged"] is True
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    assert any(c.get("content") == "AI wrote this"
               for c in loaded["cells"])  # merged, not wiped


def test_true_overlap_409s_then_retry_converges(app, auth_client, user,
                                                 enabled):
    """Critical 2: both sides edit the same cell — 409 carries everything
    the editor needs to rebase, and the retry converges with neither side
    lost (not even typed-then-blurred edits)."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race conflict",
                  cells=[nb_doc.new_cell("markdown", "original")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    cell_id = stale_doc["cells"][0]["id"]
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_update_cell(
            user_obj, {"file_id": fid, "cell_id": cell_id,
                       "content": "AI rewrite"}, None)["ok"] is True
        assert nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "AI side note"}, None)["ok"] is True
        ai_versions = len(db.session.get(StoredFile, fid).versions)
    stale_doc["cells"][0]["content"] = "user rewrite"
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 409
    payload = resp.get_json()
    assert payload["conflict"] is True
    server_doc, server_checksum = payload["doc"], payload["checksum"]
    assert any(c.get("content") == "AI rewrite"
               for c in server_doc["cells"])
    # Server keeps the AI state; nothing was clobbered.
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    assert loaded["cells"][0]["content"] == "AI rewrite"
    # Editor rebase (mirrors notebook.js): adopt the server doc, keep the
    # user's own version of the conflicted cell — including edits the user
    # already blurred out of (they live in the in-memory doc, not the DOM).
    rebased = copy.deepcopy(server_doc)
    for cell in rebased["cells"]:
        if cell["id"] == cell_id:
            cell["content"] = "user rewrite"
    resp2 = _post_save(auth_client, fid, rebased, server_checksum,
                       server_doc)
    assert resp2.status_code == 200
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    assert loaded["cells"][0]["content"] == "user rewrite"
    assert any(c.get("content") == "AI side note"
               for c in loaded["cells"])
    # The pre-retry AI state was guard-snapshotted: restorable.
    with app.app_context():
        assert len(db.session.get(StoredFile, fid).versions) == ai_versions + 1


def test_retry_survives_third_writer(app, auth_client, user, enabled):
    """Critical 3: a writer landing between 409 and retry must not be lost —
    the retry re-merges against its true base instead of blind-writing."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race third writer",
                  cells=[nb_doc.new_cell("markdown", "original")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    cell_id = stale_doc["cells"][0]["id"]
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_update_cell(
            user_obj, {"file_id": fid, "cell_id": cell_id,
                       "content": "AI rewrite"}, None)["ok"] is True
    stale_doc["cells"][0]["content"] = "user rewrite"
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 409
    server_doc, server_checksum = (resp.get_json()["doc"],
                                   resp.get_json()["checksum"])
    # A third writer (another tab) lands BEFORE the user retries.
    tab_doc, tab_checksum = _stale_editor_snapshot(app, fid)
    tab_base = copy.deepcopy(tab_doc)
    tab_doc["cells"].append(nb_doc.new_cell("markdown", "tab two"))
    tab_resp = _post_save(auth_client, fid, tab_doc, tab_checksum, tab_base)
    assert tab_resp.status_code == 200
    # The user retries with the 409 base while the tab's write is live.
    rebased = copy.deepcopy(server_doc)
    for cell in rebased["cells"]:
        if cell["id"] == cell_id:
            cell["content"] = "user rewrite"
    retry = _post_save(auth_client, fid, rebased, server_checksum,
                       server_doc)
    assert retry.status_code == 200
    assert retry.get_json()["merged"] is True
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    contents = [c.get("content") for c in loaded["cells"]]
    assert "user rewrite" in contents  # retrying writer kept
    assert "tab two" in contents  # interleaving writer not lost
    with app.app_context():
        assert len(db.session.get(StoredFile, fid).versions) == 3


def test_save_without_base_checksum_rejected(app, auth_client, user,
                                             enabled):
    """Important 4: a save with no base must never silently wipe concurrent
    cells — reject with 409, not a 200 overwrite."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race no base",
                  cells=[nb_doc.new_cell("markdown", "user text")])
    stale_doc, _ = _stale_editor_snapshot(app, fid)
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "AI wrote this"}, None)["ok"] is True
    resp = auth_client.post(f"/m/notebooks/{fid}/save",
                            json={"doc": stale_doc})
    assert resp.status_code == 409
    assert resp.get_json()["conflict"] is True
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    assert any(c.get("content") == "AI wrote this"
               for c in loaded["cells"])


def test_stale_save_without_base_doc_rejected(app, auth_client, user,
                                              enabled):
    """Important 4: a stale base without base content cannot be attributed
    to either side — reject (409), never blind-merge or blind-write."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race no base doc",
                  cells=[nb_doc.new_cell("markdown", "user text")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "AI wrote this"}, None)["ok"] is True
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, None)
    assert resp.status_code == 409
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    assert any(c.get("content") == "AI wrote this"
               for c in loaded["cells"])


def test_stale_autosave_snapshots_post_ai_state(app, auth_client, user,
                                                enabled):
    """A stale autosave inside SNAPSHOT_INTERVAL must still snapshot the
    post-AI state before writing, so the AI work stays restorable."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race snapshot",
                  cells=[nb_doc.new_cell("markdown", "user text")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    with app.app_context():
        user_obj = db.session.get(User, user)
        nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "AI wrote this"}, None)
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 200
    with app.app_context():
        # AI run (1) + forced merge snapshot (1): post-AI state restorable.
        assert len(db.session.get(StoredFile, fid).versions) == 2


def test_stale_save_ignores_editing_ui_flag(app, auth_client, user, enabled):
    """The in-memory `_editing` UI flag round-trips through autosaves; it
    must not count as a conflicting edit (no spurious 409, AI cell kept)."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race editing flag",
                  cells=[nb_doc.new_cell("markdown", "user text")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    stale_doc["cells"][0]["_editing"] = True  # cell open in the editor
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "AI wrote this"}, None)["ok"] is True
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 200
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    assert any(c.get("content") == "AI wrote this"
               for c in loaded["cells"])


def test_stale_merge_flags_resurrected_delete(app, auth_client, user,
                                              enabled):
    """Important 8: P0 bias keeps a concurrently-untouched deleted cell
    (never lose data) — but the response must flag it so the UI can hint
    and the user can re-delete."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race resurrect",
                  cells=[nb_doc.new_cell("markdown", "keep me"),
                         nb_doc.new_cell("markdown", "drop me")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    drop_id = stale_doc["cells"][1]["id"]
    del stale_doc["cells"][1]  # user deletes the second cell
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "AI wrote this"}, None)["ok"] is True
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 200
    assert resp.get_json()["resurrected"] == [drop_id]
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    contents = [c.get("content") for c in loaded["cells"]]
    assert "keep me" in contents and "AI wrote this" in contents
    assert "drop me" in contents  # bias: kept, flagged above


def test_delete_vs_edit_conflicts(app, auth_client, user, enabled):
    """Important 8: deleting a cell the concurrent writer edited is a true
    conflict — 409, server edit intact (never silently resurrected over)."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race delete edit",
                  cells=[nb_doc.new_cell("markdown", "original")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    cell_id = stale_doc["cells"][0]["id"]
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_update_cell(
            user_obj, {"file_id": fid, "cell_id": cell_id,
                       "content": "AI rewrite"}, None)["ok"] is True
    del stale_doc["cells"][0]  # user deletes the cell AI just edited
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 409
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    assert loaded["cells"][0]["content"] == "AI rewrite"


def test_repeated_conflicts_eventually_converge(app, auth_client, user,
                                                 enabled):
    """The server never locks a retrying editor out: after two consecutive
    409s (a writer landing between 409 and retry), a rebase against the
    latest base still converges. Client-side this pairs with the retry
    counter reset (see the JS test): every fresh attempt gets fair merges."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race repeated",
                  cells=[nb_doc.new_cell("markdown", "original")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    cell_id = stale_doc["cells"][0]["id"]
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_update_cell(
            user_obj, {"file_id": fid, "cell_id": cell_id,
                       "content": "AI one"}, None)["ok"] is True
    stale_doc["cells"][0]["content"] = "user rewrite"

    def rebase_keep_user(server_doc):
        rebased = copy.deepcopy(server_doc)
        for cell in rebased["cells"]:
            if cell["id"] == cell_id:
                cell["content"] = "user rewrite"
        return rebased

    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 409
    c1_doc, c1_checksum = (resp.get_json()["doc"],
                           resp.get_json()["checksum"])
    # A second writer lands before the user retries.
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_update_cell(
            user_obj, {"file_id": fid, "cell_id": cell_id,
                       "content": "AI two"}, None)["ok"] is True
    retry1 = _post_save(auth_client, fid, rebase_keep_user(c1_doc),
                        c1_checksum, c1_doc)
    assert retry1.status_code == 409  # stale again — but never locked out
    c2_doc, c2_checksum = (retry1.get_json()["doc"],
                           retry1.get_json()["checksum"])
    retry2 = _post_save(auth_client, fid, rebase_keep_user(c2_doc),
                        c2_checksum, c2_doc)
    assert retry2.status_code == 200
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
        assert loaded["cells"][0]["content"] == "user rewrite"
        # AI one (1) + AI two (1, new run) + two conflict guards (2):
        # every pre-retry state stays restorable.
        assert len(db.session.get(StoredFile, fid).versions) == 4


def test_dirty_unload_flush_merges_without_wipe(app, auth_client, user,
                                                enabled):
    """Important 2 (server half): a dirty editor never polls, so its unload
    flush can carry a base the server has already moved past. The
    flush-shaped save must still merge — never blind-write the AI cell."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race unload merge",
                  cells=[nb_doc.new_cell("markdown", "original")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    stale_doc["cells"][0]["content"] = "unsent user edit"
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_add_cell(
            user_obj, {"file_id": fid, "cell_type": "markdown",
                       "content": "AI wrote this"}, None)["ok"] is True
    # Same shape as the keepalive flush: doc + base, no response read.
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 200
    assert resp.get_json()["merged"] is True
    with app.app_context():
        loaded = nb_doc.load(db.session.get(StoredFile, fid))
    contents = [c.get("content") for c in loaded["cells"]]
    assert "unsent user edit" in contents and "AI wrote this" in contents


def test_dirty_unload_flush_conflict_never_wipes(app, auth_client, user,
                                                 enabled):
    """Important 2 (server half): when the unseen concurrent edit truly
    overlaps, the unload flush 409s (guard-snapshotted) instead of wiping —
    the only unknown left is delivery itself, which the client flags."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Race unload conflict",
                  cells=[nb_doc.new_cell("markdown", "original")])
    stale_doc, stale_checksum = _stale_editor_snapshot(app, fid)
    base_doc = copy.deepcopy(stale_doc)
    cell_id = stale_doc["cells"][0]["id"]
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert nb_tools._tool_update_cell(
            user_obj, {"file_id": fid, "cell_id": cell_id,
                       "content": "AI rewrite"}, None)["ok"] is True
        before = len(db.session.get(StoredFile, fid).versions)
    stale_doc["cells"][0]["content"] = "unsent user edit"
    resp = _post_save(auth_client, fid, stale_doc, stale_checksum, base_doc)
    assert resp.status_code == 409
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert nb_doc.load(stored)["cells"][0]["content"] == "AI rewrite"
        assert len(stored.versions) == before + 1  # guard snapshot


def test_unload_flag_decision_table():
    """Round-3 (a)(b)(c): the node harness drives the SHIPPED
    flushedContentConfirmed through lost-in-transit / exhausted-409 /
    clean-merge / delete / flag / legacy cases. The flag may clear silently
    ONLY on proof the flushed content landed."""
    harness = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "notebook_flag_harness.js")
    proc = subprocess.run(["node", harness], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_notebook_conflict_counter_and_unload_flag():
    # JS-level round-2 findings: fresh user intent must re-arm the bounded
    # 409 retry (counter reset in scheduleSave), and every dirty keepalive
    # flush must version-flag the next load (no silent unknown outcome).
    path = os.path.join(_NB_DIR, "static", "notebook.js")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    sched_body = _js_fn_body(src, "scheduleSave")
    assert "conflictRetries = 0" in sched_body  # Important 1
    flush_body = _js_fn_body(src, "flushSync")
    # Important 2: no known-stale-only gate — every dirty flush flags, and
    # the flag carries the attempted checksum for a versioned next-load
    # clear (no wolf-cries when the server provably matches our base).
    assert "if (saveBase !== lastChecksum)" not in flush_body
    assert "flagPendingConflict()" in flush_body
    assert "checkPendingConflict" in src
    # Round 3: the silent clear must be proof-of-landing (content confirmed
    # present), never base equality (which proves nothing for a dirty flush).
    assert "pending.checksum === (opts.checksum" not in src
    assert "flushedContentConfirmed" in src
    flag_body = _js_fn_body(src, "flagPendingConflict")
    assert "changed" in flag_body and "deleted" in flag_body


def test_ai_edit_reaches_open_editor_without_wipe():
    # JS-level: remote changes arrive as a consumed UI event and only the
    # affected .nb-cell nodes are patched — the active editor is never
    # wiped by a full re-render, and saves always carry their base.
    path = os.path.join(_NB_DIR, "static", "notebook.js")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "notebook_changed" in src
    # Important 6: the event is consumed in-slice (listener registered).
    assert 'addEventListener("notebook_changed"' in src
    # The remote path patches per-cell nodes and preserves the editor.
    assert "patchCellNodes" in src
    assert "base_doc" in src
    apply_body = _js_fn_body(src, "applyRemoteDoc")
    assert "render(" not in apply_body  # no full re-render on remote input
    assert "innerHTML" not in apply_body  # no wipe; per-node patching only
    patch_body = _js_fn_body(src, "patchCellNodes")
    assert "innerHTML" not in patch_body
    # The 409 rebase keeps every local change (Critical 2), not just the
    # focused cell: base-aware rule mirrored from the server merge.
    rebase_body = _js_fn_body(src, "rebaseLocalToServer")
    assert "baseDoc" in rebase_body


# ---------------------------------------------------------------------------
# D2: oversized notebooks + pasted-image uploads (P0 dataloss)
# ---------------------------------------------------------------------------

def _big_cells(n, size):
    """Real-world-shaped bulk: many markdown cells, each ~`size` chars."""
    return [nb_doc.new_cell("markdown", f"cell {i} " + "x" * size)
            for i in range(n)]


def test_large_notebook_loads_whole(app, user):
    """A .pdocnb bigger than MAX_TEXT_CHARS must load whole — never a
    mid-JSON slice that parses as corrupt."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Big", cells=_big_cells(700, 1000))
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert (os.path.getsize(file_service.file_path(stored))
                > indexing_service.MAX_TEXT_CHARS)
        doc = nb_doc.load(stored)
        assert len(doc["cells"]) == 700
        assert doc["cells"][0]["content"].startswith("cell 0 ")
        assert doc["cells"][699]["content"].startswith("cell 699 ")


def test_large_notebook_doc_endpoint_serves_whole(app, auth_client, user,
                                                  enabled):
    """The editor's /doc fetch of an oversized notebook is 200 with every
    cell — not a 400 'Corrupt notebook file'."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Big route",
                  cells=_big_cells(700, 1000))
    resp = auth_client.get(f"/m/notebooks/{fid}/doc")
    assert resp.status_code == 200
    assert len(resp.get_json()["doc"]["cells"]) == 700


def test_large_notebook_with_synced_history_loads_whole(app, user):
    """Full-history notebooks embed prior versions in synced_history — the
    whole file (cells + history tail) must load, history intact."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "BigHist",
                  cells=_big_cells(400, 1000))
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = nb_doc.load(stored)
        doc["full_history"] = True
        nb_doc.save_document(stored, doc, force_checkpoint=True)
        assert (os.path.getsize(file_service.file_path(stored))
                > indexing_service.MAX_TEXT_CHARS)
        loaded = nb_doc.load(stored)
        assert len(loaded["cells"]) == 400
        history = loaded.get("synced_history")
        assert isinstance(history, list) and history
        assert history[-1]["doc"]["cells"][0]["content"].startswith(
            "cell 0 ")


def test_pasted_and_dropped_images_upload_instead_of_base64():
    """Paste/drop of an image into a rich-text cell must route through the
    shared assets uploader — never baked into the cell as a data: URL (one
    screenshot would bloat the .pdocnb past the load cap).

    Interception runs on the holder in the capture phase (before Quill's own
    root listeners — Quill 2.0.3 embeds pasted/dropped files as base64 via
    FileReader, and its drop listener has no defaultPrevented check, so
    bubble-phase preventDefault would be too late AND duplicate).
    """
    path = os.path.join(_NB_DIR, "static", "notebook.js")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    body = _js_fn_body(src, "wireImageUploads")
    # Capture-phase interception on the holder (ancestor of quill.root),
    # never on the root itself where Quill's listeners would run first.
    assert 'ctx.holder.addEventListener("paste"' in body
    assert 'ctx.holder.addEventListener("drop"' in body
    assert body.count(", true)") >= 2  # capture flags
    assert 'quill.root.addEventListener("paste"' not in src
    assert 'quill.root.addEventListener("drop"' not in src
    # One shared uploader: def + paste + drop + sweep call sites here, plus
    # the toolbar call site outside.
    assert body.count("uploadImageFile(") == 4
    assert src.count("uploadImageFile(") == 5
    # Every insert path embeds the uploaded URL — never a data: URL.
    assert 'insertEmbed(range.index, "image", url, "user")' in body
    assert 'img.setAttribute("src", url)' in body
    # The Critical guarantee, pinned statically: src is replaced only on
    # success, so the original is never cleared up front.
    assert 'removeAttribute("src")' not in body
    assert "data-nb-uploading" in body  # in-flight guard, cleared on settle
    # Mixed paste keeps its text (no preventDefault); HTML drags and late
    # (FileReader-delayed) embeds are covered by sweep + observer.
    assert '"files"' in body  # image-only detection (case-insensitive)
    assert "stopPropagation" in body  # drop: Quill has no prevented-check
    assert "MutationObserver" in body
    assert 'src^="data:"' in body
    assert "opts.assetsUrl" in src


def test_image_upload_wiring_behavior():
    """Node harness drives the SHIPPED wireImageUploads with fakes:
    image-only paste / mixed paste / failed upload (src preserved) /
    file drop / file-less drop / observer backstop."""
    harness = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "notebook_image_harness.js")
    proc = subprocess.run(["node", harness], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# N0 reconciliation pins: the P0-DONE items above (truncation load, checksum
# merge, image-upload paste/drop) are already pinned by passing tests, so
# they are NOT re-tested here. The three items below are TODO (owned by N9):
# marked xfail so the suite stays green; run with --runxfail to see the RED
# proof. N9: transcribe these into real tests and drop the markers.
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason="N0 verdict TODO -> N9: global window.marked.use pollutes chat; "
           "no isolated instance",
    strict=False)
def test_marked_isolation_from_chat():
    path = os.path.join(_NB_DIR, "static", "notebook.js")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "new window.marked.Marked" in src or "new Marked" in src
    assert "window.marked.use(" not in src


@pytest.mark.xfail(
    reason="N0 verdict TODO -> N9: no .ai-live-flash reconcile animation on "
           "AI-changed cells",
    strict=False)
def test_live_flash_on_ai_edit_pin():
    path = os.path.join(_NB_DIR, "static", "notebook.js")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "ai-live-flash" in src
    assert "Reverter" in src


@pytest.mark.xfail(
    reason="N0 verdict TODO -> N9: focusout handlers close the cell even "
           "when focus moves to the chat window",
    strict=False)
def test_keep_open_on_chat_click_pin():
    path = os.path.join(_NB_DIR, "static", "notebook.js")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    parts = src.split('addEventListener("focusout"')
    assert len(parts) > 1  # focusout handlers exist
    assert any("relatedTarget" in chunk[:1500] for chunk in parts[1:])
