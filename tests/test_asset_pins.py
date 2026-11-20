"""Task X0: `?v` staleness sweep — pin tests for every bumped/added asset pin.

Each test asserts the NEW pin value. Written RED-first: they fail against the
stale pins, then the templates/JS are bumped to match. The dashboards-style
`?v` assert shape (``assert "?v=NN" in body``) is extended here to every
bumped or added pin.

Inventory (old -> new):
  base.html: ui.js 1->2, ai_common.js 3->4, ai_core.js 6->7, style.css 18->19,
    ai_chat.js 21->22, ai_tasks_widget.js 2->3, sync_widget.js 6->7,
    automation_runs_widget.js 2->3, spa.js 1->2, main.js none->1 (added)
  search/ai.html: ai_page.js 20->21
  drive/view.html: hashtags.js 5->6, caption.js 2->3
  drive/drive.html + automation list/editor: automation.css 6->7
  notebooks/edit.html: notebook.js 13->14
  automation/editor.html: automation.js 2->3, run_form.js 1->2
  automation/run.html + ai_core.js + drive.js: run_form.js 1->2 (hardcoded)
  whiteboards/board.html: wb-editor.js 1->2
  dashboards/edit.html: dashboard.css 14->15 (dashboard.js ?v=36 untouched)
  tables/view.html: tables.js none->1 (added)
"""

import importlib.util
import io
import json
import os
import sys

import pytest

from app.extensions import db
from app.models import Drive, ModuleState, StoredFile
from app.services import indexing_service
from modules.whiteboards import board as wb

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)


def _load_module(name, dirname):
    mod_dir = os.path.join(_REPO_ROOT, "modules", dirname)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(mod_dir, "__init__.py"),
        submodule_search_locations=[mod_dir])
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


_auto = _load_module("docindex_module_automation", "automation")
_auto_models = importlib.import_module("docindex_module_automation.models")
_dash = _load_module("docindex_module_dashboards", "dashboards")
dash_doc = importlib.import_module("docindex_module_dashboards.doc")
_nb = _load_module("docindex_module_notebooks", "notebooks")
nb_doc = importlib.import_module("docindex_module_notebooks.doc")
_tables = _load_module("docindex_module_tables", "tables")
tables_doc = importlib.import_module("docindex_module_tables.doc")  # noqa: F841

# 1x1 transparent PNG (same bytes as test_captions.py).
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture()
def all_modules(app):
    with app.app_context():
        for name in ("automation", "notebooks", "dashboards", "tables",
                     "whiteboards"):
            db.session.add(ModuleState(name=name, enabled=True))
        db.session.commit()


@pytest.fixture()
def ai_on(app):
    app.config["AI_ENABLED"] = True
    app.config["AI_BASE_URL"] = "http://localhost:11434/v1"
    app.config["AI_MODEL"] = "test-model"


def _drive(app, user_id):
    with app.app_context():
        d = Drive(name="Personal", user_id=user_id)
        db.session.add(d)
        db.session.commit()
        return d.id


def _upload(client, name, content):
    return client.post("/upload", data={
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def _file_id(app):
    with app.app_context():
        return StoredFile.query.one().id


def _notebook(app, user_id, drive_id, name="Pin notes"):
    with app.app_context():
        from app.models import User
        user = db.session.get(User, user_id)
        drive = db.session.get(Drive, drive_id)
        stored = nb_doc.create_notebook(user, drive, name)
        indexing_service.index_file(stored.id, app)  # tests run inline
        return stored.id


def _dashboard(app, user_id, drive_id, title="Pin board"):
    with app.app_context():
        from app.models import User
        user = db.session.get(User, user_id)
        drive = db.session.get(Drive, drive_id)
        stored = dash_doc.create_dashboard(user, drive, title)
        indexing_service.index_file(stored.id, app)  # tests run inline
        return stored.id


def _board(app, user_id, drive_id, name="Pin whiteboard"):
    with app.app_context():
        from app.models import User
        user = db.session.get(User, user_id)
        drive = db.session.get(Drive, drive_id)
        return wb.create_board(user, drive, name).id


def _workflow(app, user_id, name="Pin flow"):
    graph = {"nodes": [
        {"id": "n1", "type": "input.text", "x": 0, "y": 0,
         "config": {"content": "hi", "format": "text"}},
        {"id": "n2", "type": "output.result", "x": 0, "y": 0,
         "config": {}},
    ], "edges": [{"from": "n1", "from_port": "out",
                  "to": "n2", "to_port": "in"}]}
    with app.app_context():
        wf = _auto_models.AutomationWorkflow(
            user_id=user_id, name=name, graph=json.dumps(graph))
        db.session.add(wf)
        db.session.commit()
        return wf.id


def _table_file(app, user_id, name="rows.csv"):
    import uuid as _uuid

    from app.services import file_service as _file_service
    with app.app_context():
        sname = f"{_uuid.uuid4().hex}.csv"
        stored = StoredFile(name=name, stored_name=sname, extension="csv",
                            mime_type="text/csv", size=7, user_id=user_id)
        db.session.add(stored)
        db.session.commit()
        fid = stored.id
        path = _file_service.file_path(stored)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"a,b\n1,2\n")
    return fid


# ---------------------------------------------------------------------------
# base.html shell pins (drive page extends base)
# ---------------------------------------------------------------------------

def test_base_shell_core_pins(auth_client, app, user):
    html = auth_client.get("/").data.decode()
    assert "js/ui.js?v=2" in html
    assert "js/main.js?v=1" in html
    assert "js/ai_common.js?v=4" in html
    assert "js/ai_core.js?v=7" in html
    assert "css/style.css?v=19" in html
    assert "js/sync_widget.js?v=7" in html
    assert "js/spa.js?v=2" in html


def test_base_shell_ai_pins(auth_client, app, user, ai_on):
    html = auth_client.get("/").data.decode()
    assert "js/ai_chat.js?v=22" in html
    assert "js/ai_tasks_widget.js?v=3" in html


def test_base_shell_automation_pins(auth_client, app, user, all_modules):
    html = auth_client.get("/").data.decode()
    assert "js/automation_runs_widget.js?v=3" in html
    assert "automation.css?v=7" in html


def test_search_ai_page_pin(auth_client, app, user):
    html = auth_client.get("/search?mode=ai").data.decode()
    assert "js/ai_page.js?v=21" in html


# ---------------------------------------------------------------------------
# drive view page pins (hashtags unconditional, caption on images)
# ---------------------------------------------------------------------------

def test_drive_view_hashtags_pin(auth_client, app, user):
    _upload(auth_client, "v.txt", b"view content")
    fid = _file_id(app)
    html = auth_client.get(f"/file/{fid}/view").data.decode()
    assert "js/hashtags.js?v=6" in html


def test_drive_view_caption_pin(auth_client, app, user):
    _upload(auth_client, "pic.png", PNG)
    fid = _file_id(app)
    html = auth_client.get(f"/file/{fid}/view").data.decode()
    assert "js/caption.js?v=3" in html


# ---------------------------------------------------------------------------
# module shell pins
# ---------------------------------------------------------------------------

def test_notebooks_edit_pin(auth_client, app, user, all_modules):
    fid = _notebook(app, user, _drive(app, user))
    html = auth_client.get(f"/m/notebooks/{fid}").data.decode()
    assert "notebook.js?v=14" in html


def test_automation_editor_pins(auth_client, app, user, all_modules):
    _drive(app, user)  # exercises the drives serialization on the page
    wf_id = _workflow(app, user)
    html = auth_client.get(f"/m/automation/{wf_id}").data.decode()
    assert "automation.js?v=3" in html
    assert "run_form.js?v=2" in html
    assert "automation.css?v=7" in html


def test_automation_list_pin(auth_client, app, user, all_modules):
    html = auth_client.get("/m/automation/").data.decode()
    assert "automation.css?v=7" in html


def test_automation_run_pin(auth_client, app, user, all_modules):
    wf_id = _workflow(app, user)
    run_id = auth_client.post(
        f"/m/automation/{wf_id}/run").get_json()["run_id"]
    html = auth_client.get(f"/m/automation/runs/{run_id}").data.decode()
    assert "run_form.js?v=2" in html


def test_whiteboards_board_pin(auth_client, app, user, all_modules):
    fid = _board(app, user, _drive(app, user))
    html = auth_client.get(f"/m/whiteboards/{fid}").data.decode()
    assert "wb-editor.js?v=2" in html


def test_dashboards_edit_pin(auth_client, app, user, all_modules):
    fid = _dashboard(app, user, _drive(app, user))
    html = auth_client.get(f"/m/dashboards/{fid}").data.decode()
    assert "dashboard.css?v=15" in html
    # ?v=36 is current (bumped with the asset in 6fcb37c6) — must not move.
    assert "dashboard.js?v=36" in html


def test_tables_view_pin(auth_client, app, user, all_modules):
    fid = _table_file(app, user)
    html = auth_client.get(f"/m/tables/{fid}").data.decode()
    assert "tables.js?v=1" in html


# ---------------------------------------------------------------------------
# hardcoded run_form pins inside shipped JS (no single source exists — each
# of the four sites builds its own script URL, so all four bump in place)
# ---------------------------------------------------------------------------

def _static_src(*parts):
    with open(os.path.join(_REPO_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_hardcoded_run_form_pins_bumped():
    for parts in (("app", "static", "js", "ai_core.js"),
                  ("app", "static", "js", "drive.js")):
        src = _static_src(*parts)
        assert "run_form.js?v=2" in src, parts
        assert "run_form.js?v=1" not in src, parts
