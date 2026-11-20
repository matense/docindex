"""Tables module (T1): .ptable doc service + module enablement."""

import importlib
import importlib.util
import os
import sys
import tempfile as _tempfile
import uuid as _uuid

import pytest

from app.extensions import db
from app.models import Drive as _Drive
from app.models import FileVersion as _FileVersion
from app.models import ModuleState
from app.models import StoredFile as _StoredFile
from app.services import file_service as _file_service
from app.services import module_service

# Load the module package directly (it lives outside the app package).
_TABLES_DIR = os.path.join(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "modules", "tables"))
_spec = importlib.util.spec_from_file_location(
    "docindex_module_tables", os.path.join(_TABLES_DIR, "__init__.py"),
    submodule_search_locations=[_TABLES_DIR])
_tables = importlib.util.module_from_spec(_spec)

sys.modules.setdefault("docindex_module_tables", _tables)
_spec.loader.exec_module(_tables)
tables_doc = importlib.import_module("docindex_module_tables.doc")


def test_ptable_doc_roundtrip(app, user):
    assert tables_doc.EXTENSION == "ptable"
    doc = tables_doc.new_table(["a", "b"], [[1, 2]])
    tables_doc.validate(doc)  # raises on bad shape
    assert tables_doc.load(tables_doc.serialize(doc)) == doc


def test_ptable_doc_rejects_bad_shape(app, user):
    good = tables_doc.new_table(["a", "b"], [[1, 2]])
    # Ragged rows are rejected (padding is a viewer concern, not validate's).
    ragged = {"version": 1, "columns": good["columns"], "rows": [[1]]}
    with pytest.raises(ValueError):
        tables_doc.validate(ragged)
    # Non-list rows are rejected.
    not_lists = {"version": 1, "columns": good["columns"],
                 "rows": ["ab"]}
    with pytest.raises(ValueError):
        tables_doc.validate(not_lists)
    # Unknown column types coerce to text.
    coerced = tables_doc.new_table([{"name": "a", "type": "weird"}],
                                   [[1]])
    assert coerced["columns"] == [{"name": "a", "type": "text"}]
    # Corrupt text is a ValueError, not a raw JSON error.
    with pytest.raises(ValueError):
        tables_doc.load("{not json")


def test_tables_module_enables(app):
    with app.app_context():
        db.session.add(ModuleState(name="tables", enabled=True))
        db.session.commit()
        assert module_service.is_enabled("tables") is True
        modules = {m["name"]: m for m in module_service.list_modules()}
    assert "tables" in modules
    assert modules["tables"]["enabled"] is True


# ---------------------------------------------------------------------------
# T2: viewer page + virtualized grid + column stats + rows API
# ---------------------------------------------------------------------------


@pytest.fixture()
def tables_enabled(app):
    with app.app_context():
        db.session.add(ModuleState(name="tables", enabled=True))
        db.session.commit()


def _write_table_file(app, user_id, name, ext, payload: bytes):
    """Create an owned StoredFile row plus its blob on disk; return its id."""
    with app.app_context():
        sname = f"{_uuid.uuid4().hex}.{ext}"
        stored = _StoredFile(name=name, stored_name=sname, extension=ext,
                             mime_type="application/octet-stream",
                             size=len(payload), user_id=user_id)
        db.session.add(stored)
        db.session.commit()
        fid = stored.id
        path = _file_service.file_path(stored)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(payload)
    return fid


def _numbered_csv(n):
    lines = ["id,name,amount"]
    for i in range(n):
        lines.append(f"{i},name{i},{i * 1.5}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_viewer_row_cap(auth_client, app, user, tables_enabled):
    fid = _write_table_file(app, user, "big.csv", "csv", _numbered_csv(200))
    html = auth_client.get(f"/m/tables/{fid}").data.decode()
    assert auth_client.get(f"/m/tables/{fid}").status_code == 200
    # Header row + at most 50 data rows, never the whole file in the DOM.
    assert 1 < html.count("<tr") <= 60


def test_viewer_header_stats(auth_client, app, user, tables_enabled):
    payload = b"id,amount\n1,10\n2,20\n3,30\n"
    fid = _write_table_file(app, user, "nums.csv", "csv", payload)
    html = auth_client.get(f"/m/tables/{fid}").data.decode()
    # Numeric column gets a number badge plus header stats + sparkline.
    assert ">number<" in html
    assert "col-stats" in html
    assert "sparkline" in html
    assert "<svg" in html


def test_viewer_rows_page(auth_client, app, user, tables_enabled):
    fid = _write_table_file(app, user, "big.csv", "csv", _numbered_csv(120))
    data = auth_client.get(f"/m/tables/{fid}/rows?offset=50").get_json()
    assert data["total"] == 120
    assert [c["name"] for c in data["columns"]] == ["id", "name", "amount"]
    assert len(data["rows"]) == 50
    assert data["rows"][0][0] == "50"
    assert data["rows"][-1][0] == "99"


def test_viewer_messy_csv(auth_client, app, user, tables_enabled):
    # Semicolon delimiter, ragged rows, and a non-UTF8 byte (lossy decode).
    payload = "a;b;c\n1;2\n3;4;5;6\ncaf\xe9;x;y\n".encode("latin-1")
    fid = _write_table_file(app, user, "messy.csv", "csv", payload)
    assert auth_client.get(f"/m/tables/{fid}").status_code == 200
    data = auth_client.get(f"/m/tables/{fid}/rows").get_json()
    assert [c["name"] for c in data["columns"]] == ["a", "b", "c"]
    assert data["total"] == 3
    # Short rows are padded with "" (long rows trimmed to the header width).
    assert data["rows"][0] == ["1", "2", ""]
    assert data["rows"][1] == ["3", "4", "5"]
    assert data["rows"][2][0].startswith("caf")
    # .tsv forces tab-delimited parsing.
    tsv = _write_table_file(app, user, "t.tsv", "tsv",
                            b"a\tb\n1\t2\n")
    tdata = auth_client.get(f"/m/tables/{tsv}/rows").get_json()
    assert [c["name"] for c in tdata["columns"]] == ["a", "b"]
    assert tdata["rows"] == [["1", "2"]]


# ---------------------------------------------------------------------------
# T3: SQL filter bar + versioned inline editing
# ---------------------------------------------------------------------------


def _ptable_payload():
    doc = tables_doc.new_table(["a", "b"], [["1", "2"], ["3", "4"]])
    return tables_doc.serialize(doc).encode("utf-8")


def _version_count(app, fid):
    with app.app_context():
        return _FileVersion.query.filter_by(file_id=fid).count()


def _ptable_cell(app, fid, row, col_name):
    with app.app_context():
        stored = db.session.get(_StoredFile, fid)
        path = _file_service.file_path(stored)
    with open(path, encoding="utf-8") as fh:
        doc = tables_doc.load(fh.read())
    idx = [c["name"] for c in doc["columns"]].index(col_name)
    return doc["rows"][row][idx]


def test_sql_filter_rejects_writes(auth_client, app, user, tables_enabled):
    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _ptable_payload())
    for sql in ("DELETE FROM t0", "DROP TABLE t0",
                "INSERT INTO t0 VALUES (1, 2)",
                "UPDATE t0 SET a = 'x'"):
        r = auth_client.post(f"/m/tables/{fid}/query", json={"sql": sql})
        assert r.status_code in (400, 403)
        assert "SELECT" in r.get_json()["error"].upper()


def test_sql_filter_select_and_with(auth_client, app, user, tables_enabled):
    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _ptable_payload())
    r = auth_client.post(f"/m/tables/{fid}/query",
                         json={"sql": "SELECT * FROM t0 WHERE a = '1'"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["total"] == 1
    assert body["rows"] == [["1", "2"]]
    w = auth_client.post(
        f"/m/tables/{fid}/query",
        json={"sql": "WITH one AS (SELECT * FROM t0) SELECT * FROM one"})
    assert w.status_code == 200
    assert w.get_json()["total"] == 2
    # Multi-statements stay rejected even when they start with SELECT
    # (pyrun.evaluate is the final authority on writes/statements).
    bad = auth_client.post(f"/m/tables/{fid}/query",
                           json={"sql": "SELECT * FROM t0; DELETE FROM t0"})
    assert bad.status_code in (400, 403)
    assert bad.get_json()["ok"] is False


def test_cell_edit_snapshots_version(auth_client, app, user, tables_enabled):
    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _ptable_payload())
    before = _version_count(app, fid)
    r = auth_client.post(f"/m/tables/{fid}/cell",
                         json={"row": 0, "col": "a", "value": "9"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert _version_count(app, fid) == before + 1
    assert _ptable_cell(app, fid, 0, "a") == "9"
    with app.app_context():
        latest = _FileVersion.query.filter_by(
            file_id=fid).order_by(_FileVersion.version.desc()).first()
        assert latest.source == "table-edit"


def test_cell_edit_readonly_drive_rejects(auth_client, app, user,
                                          tables_enabled):
    with app.app_context():
        drive = _Drive(name="Archive Mirror", user_id=user,
                       source_path="/tmp/archive-mirror", sync_mode="mirror")
        db.session.add(drive)
        db.session.commit()
        did = drive.id
    fid = _write_table_file(app, user, "locked.ptable", "ptable",
                            _ptable_payload())
    with _tempfile.NamedTemporaryFile(suffix=".ptable",
                                      delete=False) as tmp:
        tmp.write(_ptable_payload())
        real_path = tmp.name
    with app.app_context():
        stored = db.session.get(_StoredFile, fid)
        stored.source_path = real_path
        stored.drive_id = did
        db.session.commit()
    r = auth_client.post(f"/m/tables/{fid}/cell",
                         json={"row": 0, "col": "a", "value": "9"})
    assert r.status_code == 403
    assert "Archive Mirror" in r.get_json()["error"]
    assert _ptable_cell(app, fid, 0, "a") == "1"


def test_cell_edit_csv_requires_save_as_ptable(auth_client, app, user,
                                              tables_enabled):
    fid = _write_table_file(app, user, "data.csv", "csv",
                            b"a,b\n1,2\n3,4\n")
    r = auth_client.post(f"/m/tables/{fid}/cell",
                         json={"row": 0, "col": "a", "value": "9"})
    assert r.status_code == 400
    assert "ptable" in r.get_json()["error"].lower()
    s = auth_client.post(f"/m/tables/{fid}/save-as")
    assert s.status_code == 200
    new_id = s.get_json()["file_id"]
    assert new_id != fid
    data = auth_client.get(f"/m/tables/{new_id}/rows").get_json()
    assert [c["name"] for c in data["columns"]] == ["a", "b"]
    assert data["total"] == 2
    e = auth_client.post(f"/m/tables/{new_id}/cell",
                         json={"row": 0, "col": "a", "value": "9"})
    assert e.status_code == 200
    assert _ptable_cell(app, new_id, 0, "a") == "9"
    # The original workbook is never mutated in place.
    assert auth_client.get(f"/m/tables/{fid}/rows").get_json(
        )["rows"][0] == ["1", "2"]
    # Viewer wiring: the SQL bar is always present; csv views offer
    # Save-as while .ptable views are editable.
    csv_html = auth_client.get(f"/m/tables/{fid}").data.decode()
    assert "tables-sql" in csv_html and "Save as .ptable" in csv_html
    ptable_html = auth_client.get(f"/m/tables/{new_id}").data.decode()
    assert 'data-editable="1"' in ptable_html


# ---------------------------------------------------------------------------
# T4: Pin as dashboard card + build-automation handoff
# ---------------------------------------------------------------------------


def _t4_dash_doc():
    """Dashboards doc service (same spec dance as test_dashboards)."""
    import importlib

    if "docindex_module_dashboards" not in sys.modules:
        _t4_spec = importlib.util.spec_from_file_location(
            "docindex_module_dashboards",
            os.path.join(_TABLES_DIR, "..", "dashboards", "__init__.py"),
            submodule_search_locations=[
                os.path.join(_TABLES_DIR, "..", "dashboards")])
        _t4_mod = importlib.util.module_from_spec(_t4_spec)
        sys.modules["docindex_module_dashboards"] = _t4_mod
        _t4_spec.loader.exec_module(_t4_mod)
    return importlib.import_module("docindex_module_dashboards.doc")


@pytest.fixture()
def dashboards_enabled(app):
    with app.app_context():
        db.session.add(ModuleState(name="dashboards", enabled=True))
        db.session.commit()


@pytest.fixture()
def automation_enabled(app):
    with app.app_context():
        db.session.add(ModuleState(name="automation", enabled=True))
        db.session.commit()


def _t4_drive(app, user_id):
    with app.app_context():
        drive = _Drive(name="Dashboards Home", user_id=user_id)
        db.session.add(drive)
        db.session.commit()
        return drive.id


def _t4_dashboard(app, user_id, drive_id, title="Morning view"):
    from app.models import User as _User

    dash_doc = _t4_dash_doc()
    with app.app_context():
        user = db.session.get(_User, user_id)
        drive = db.session.get(_Drive, drive_id)
        return dash_doc.create_dashboard(user, drive, title).id


def _files_count(app):
    with app.app_context():
        return _StoredFile.query.filter(
            _StoredFile.deleted_at.is_(None)).count()


def _dashboard_doc(app, dash_id):
    dash_doc = _t4_dash_doc()
    with app.app_context():
        stored = db.session.get(_StoredFile, dash_id)
        path = _file_service.file_path(stored)
    with open(path, encoding="utf-8") as fh:
        return dash_doc.parse(fh.read())


def test_pin_creates_card_not_file(auth_client, app, user, tables_enabled,
                                  dashboards_enabled):
    fid = _write_table_file(app, user, "issues.csv", "csv",
                            b"a,b\n1,2\n")
    dash = _t4_dashboard(app, user, _t4_drive(app, user))
    before = _files_count(app)
    out = auth_client.post(f"/m/tables/{fid}/pin",
                           json={"dashboard_id": dash,
                                 "title": "Top issues"})
    assert out.status_code == 200
    body = out.get_json()
    assert body["ok"] is True
    # No new StoredFile — only a card referencing the table as attachment.
    assert _files_count(app) == before
    doc = _dashboard_doc(app, dash)
    cards = [c for c in doc["cards"] if c.get("title") == "Top issues"]
    assert len(cards) == 1
    assert cards[0]["type"] == "table"
    atts = {a["id"]: a for a in doc["attachments"]}
    refs = (cards[0].get("inputs") or {}).get("attachments") or []
    assert len(refs) == 1 and refs[0] in atts
    assert atts[refs[0]]["fileId"] == fid


def test_pin_dashboards_disabled_fail_closed(auth_client, app, user,
                                             tables_enabled):
    fid = _write_table_file(app, user, "issues.csv", "csv",
                            b"a,b\n1,2\n")
    before = _files_count(app)
    out = auth_client.post(f"/m/tables/{fid}/pin",
                           json={"dashboard_id": 1, "title": "Top issues"})
    body = out.get_json()
    assert body["ok"] is False
    assert "dashboards" in body["error"].lower()
    assert _files_count(app) == before


def test_pin_targets_default_most_recent(auth_client, app, user,
                                        tables_enabled,
                                        dashboards_enabled):
    fid = _write_table_file(app, user, "issues.csv", "csv",
                            b"a,b\n1,2\n")
    drive_id = _t4_drive(app, user)
    _t4_dashboard(app, user, drive_id, title="Older view")
    newer = _t4_dashboard(app, user, drive_id, title="Newer view")
    out = auth_client.get(f"/m/tables/{fid}/pin-targets")
    assert out.status_code == 200
    body = out.get_json()
    assert body["ok"] is True
    assert {d["id"] for d in body["dashboards"]} >= {newer}
    assert body["default_id"] == newer
    assert body["suggested_title"] == "issues"


def test_build_automation_prefilled_and_live(auth_client, app, user,
                                            tables_enabled,
                                            automation_enabled):
    fid = _write_table_file(app, user, "issues.csv", "csv",
                            b"a,b\n1,2\n")
    out = auth_client.post(f"/m/tables/{fid}/build-automation")
    assert out.status_code == 200
    body = out.get_json()
    assert body["ok"] is True
    assert body["file_id"] == fid
    # Prefilled: the workflow graph holds an input.file node on the table.
    graph = auth_client.get(
        f"/m/automation/{body['workflow_id']}/graph").get_json()["graph"]
    assert any(n.get("type") == "input.file"
               and (n.get("config") or {}).get("file_id") == fid
               for n in graph["nodes"])
    # Not dead: the deep link opens the editor.
    assert auth_client.get(body["url"]).status_code == 200


def test_build_automation_disabled_fail_closed(auth_client, app, user,
                                               tables_enabled):
    fid = _write_table_file(app, user, "issues.csv", "csv",
                            b"a,b\n1,2\n")
    out = auth_client.post(f"/m/tables/{fid}/build-automation")
    body = out.get_json()
    assert body["ok"] is False
    assert "automation" in body["error"].lower()


def test_viewer_pin_build_buttons_present(auth_client, app, user,
                                          tables_enabled):
    fid = _write_table_file(app, user, "issues.csv", "csv",
                            b"a,b\n1,2\n")
    html = auth_client.get(f"/m/tables/{fid}").data.decode()
    assert "tables-pin" in html
    assert "tables-buildauto" in html


def test_query_bad_paging_json_error(auth_client, app, user,
                                     tables_enabled):
    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _ptable_payload())
    r = auth_client.post(f"/m/tables/{fid}/query",
                         json={"sql": "SELECT * FROM t0", "offset": "abc"})
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False
    assert "offset/limit" in body["error"].lower()


def _t4_workflow_count(app, user_id):
    auto_models = importlib.import_module(
        "docindex_module_automation.models")
    with app.app_context():
        return auto_models.AutomationWorkflow.query.filter_by(
            user_id=user_id).count()


def test_build_automation_idempotent_two_posts_one_workflow(
        auth_client, app, user, tables_enabled, automation_enabled):
    fid = _write_table_file(app, user, "issues.csv", "csv",
                            b"a,b\n1,2\n")
    first = auth_client.post(f"/m/tables/{fid}/build-automation")
    assert first.status_code == 200
    assert first.get_json()["ok"] is True
    assert first.get_json()["reused"] is False
    second = auth_client.post(f"/m/tables/{fid}/build-automation")
    assert second.status_code == 200
    body = second.get_json()
    assert body["ok"] is True
    assert body["reused"] is True
    assert body["workflow_id"] == first.get_json()["workflow_id"]
    assert _t4_workflow_count(app, user) == 1
    graph = auth_client.get(
        f"/m/automation/{body['workflow_id']}/graph").get_json()["graph"]
    assert any(n.get("type") == "input.file"
               and (n.get("config") or {}).get("file_id") == fid
               for n in graph["nodes"])


# ---------------------------------------------------------------------------
# T5: AI tools (tables.*)
# ---------------------------------------------------------------------------


def _t5_tools():
    """The AI tools module (T5) — ImportError until implemented."""
    return importlib.import_module("docindex_module_tables.tools")


def _t5_ptable_payload():
    doc = tables_doc.new_table(
        [{"name": "price", "type": "number"},
         {"name": "label", "type": "text"}],
        [["1", "x"], ["2", "y"]])
    return tables_doc.serialize(doc).encode("utf-8")


def test_ai_schema_lists_columns_and_types(app, user, tables_enabled):
    from app.models import User as _User

    tools = _t5_tools()
    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _t5_ptable_payload())
    csv_id = _write_table_file(app, user, "n.csv", "csv",
                               b"id,amount\n1,10\n2,20\n")
    with app.app_context():
        user_obj = db.session.get(_User, user)
        out = tools._tool_schema(user_obj, {"file_id": fid}, None)
    assert out["columns"] == [{"name": "price", "type": "number"},
                              {"name": "label", "type": "text"}]
    assert out["total_rows"] == 2
    # Spreadsheet files report sniffed types like the rows API.
    with app.app_context():
        user_obj = db.session.get(_User, user)
        sniffed = tools._tool_schema(user_obj, {"file_id": csv_id},
                                     None)
    assert sniffed["columns"] == [{"name": "id", "type": "number"},
                                  {"name": "amount", "type": "number"}]
    assert sniffed["total_rows"] == 2


def test_ai_query_sql_select_and_gate(app, user, tables_enabled):
    from app.models import User as _User

    tools = _t5_tools()
    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _ptable_payload())
    with app.app_context():
        user_obj = db.session.get(_User, user)
        out = tools._tool_query_sql(
            user_obj,
            {"file_id": fid, "sql": "SELECT * FROM t0 WHERE a = '1'"},
            None)
    assert out["ok"] is True
    assert out["total"] == 1
    assert out["rows"] == [["1", "2"]]
    with app.app_context():
        user_obj = db.session.get(_User, user)
        bad = tools._tool_query_sql(
            user_obj, {"file_id": fid, "sql": "DELETE FROM t0"}, None)
    assert "error" in bad
    assert "SELECT" in bad["error"].upper()


def test_ai_update_rows_snapshots_version(app, user, tables_enabled):
    from app.models import User as _User

    tools = _t5_tools()
    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _ptable_payload())
    before = _version_count(app, fid)
    with app.app_context():
        user_obj = db.session.get(_User, user)
        out = tools._tool_update_rows(
            user_obj,
            {"file_id": fid,
             "edits": [{"row": 0, "col": "a", "value": "9"}]},
            None)
        assert out["ok"] is True, out
    assert _version_count(app, fid) == before + 1
    assert _ptable_cell(app, fid, 0, "a") == "9"
    with app.app_context():
        latest = _FileVersion.query.filter_by(
            file_id=fid).order_by(_FileVersion.version.desc()).first()
        assert latest.source == "table-edit"


def test_ai_add_column_appends_empty(app, user, tables_enabled):
    from app.models import User as _User

    tools = _t5_tools()
    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _ptable_payload())
    before = _version_count(app, fid)
    with app.app_context():
        user_obj = db.session.get(_User, user)
        out = tools._tool_add_column(
            user_obj, {"file_id": fid, "name": "c", "type": "text"},
            None)
        assert out["ok"] is True, out
        schema = tools._tool_schema(user_obj, {"file_id": fid}, None)
    assert [c["name"] for c in schema["columns"]] == ["a", "b", "c"]
    assert _version_count(app, fid) == before + 1
    assert _ptable_cell(app, fid, 0, "c") == ""
    assert _ptable_cell(app, fid, 1, "c") == ""


def test_ai_tools_fail_closed(app, user, tables_enabled):
    from app.models import User as _User

    tools = _t5_tools()
    with app.app_context():
        user_obj = db.session.get(_User, user)
        # Unknown files fail closed on every tool.
        assert "error" in tools._tool_schema(
            user_obj, {"file_id": 999999}, None)
        assert "error" in tools._tool_query_sql(
            user_obj, {"file_id": 999999, "sql": "SELECT 1"}, None)
        assert "error" in tools._tool_update_rows(
            user_obj, {"file_id": 999999,
                       "edits": [{"row": 0, "col": "a",
                                  "value": "9"}]}, None)
        assert "error" in tools._tool_add_column(
            user_obj, {"file_id": 999999, "name": "c"}, None)
    # A disabled module fails closed without touching storage.
    with app.app_context():
        db.session.query(ModuleState).filter_by(name="tables").delete()
        db.session.commit()
    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _ptable_payload())
    with app.app_context():
        user_obj = db.session.get(_User, user)
        out = tools._tool_schema(user_obj, {"file_id": fid}, None)
        assert "error" in out
        assert "disabled" in out["error"].lower()


# ---------------------------------------------------------------------------
# Final review minors: skeleton text, single-source exts, save-as emit,
# pin busy guard, pin detach-on-failure
# ---------------------------------------------------------------------------


def test_tables_index_describes_viewer(auth_client, tables_enabled):
    resp = auth_client.get("/m/tables/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "viewer" in body.lower()
    assert "T2" not in body


def test_tables_extensions_single_sourced():
    routes = importlib.import_module("docindex_module_tables.routes")
    assert routes.TABLE_EXTENSIONS is _tables.TABLE_EXTENSIONS


def test_save_as_emits_file_uploaded(auth_client, app, user,
                                     tables_enabled):
    import json

    from app.services import events

    fid = _write_table_file(app, user, "data.csv", "csv",
                            b"a,b\n1,2\n")
    seen = []
    sub = lambda p: seen.append(p)  # noqa: E731 - test probe
    events.subscribe("file.uploaded", sub)
    try:
        r = auth_client.post(f"/m/tables/{fid}/save-as")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        new_id = r.get_json()["file_id"]
    finally:
        listeners = events._SUBSCRIBERS.get("file.uploaded", [])
        if sub in listeners:
            listeners.remove(sub)
    assert seen, "save-as must emit file.uploaded"
    payload = seen[-1]
    assert payload["file_id"] == new_id
    assert payload["user_id"] == user
    assert "drive_id" in payload
    json.dumps(payload)  # plain JSON-serializable dict only


def test_pin_confirm_has_busy_guard():
    with open(os.path.join(_TABLES_DIR, "static", "tables.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    anchor = "pinConfirm) pinConfirm.addEventListener"
    assert anchor in src
    handler = src.split(anchor, 1)[1][:500]
    assert "if (busy) return;" in handler


def test_pin_failure_detaches_orphan_attachment(
        auth_client, app, user, tables_enabled, dashboards_enabled,
        monkeypatch):
    dash_doc = _t4_dash_doc()
    fid = _write_table_file(app, user, "issues.csv", "csv",
                            b"a,b\n1,2\n")
    dash = _t4_dashboard(app, user, _t4_drive(app, user))
    before_cards = len(_dashboard_doc(app, dash)["cards"])
    before_atts = len(_dashboard_doc(app, dash)["attachments"])
    original_save = dash_doc.save_document
    calls = {"n": 0}

    def _fail_on_pin_save(stored, doc, *args, **kwargs):
        # First save is attach_link's own persist; the second is the pin's
        # card append — only the pin itself must fail.
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("injected pin failure")
        return original_save(stored, doc, *args, **kwargs)

    monkeypatch.setattr(dash_doc, "save_document", _fail_on_pin_save)
    out = auth_client.post(f"/m/tables/{fid}/pin",
                           json={"dashboard_id": dash,
                                 "title": "Top issues"})
    assert out.get_json()["ok"] is False
    doc = _dashboard_doc(app, dash)
    assert len(doc["cards"]) == before_cards
    assert len(doc["attachments"]) == before_atts
    assert all(a.get("fileId") != fid for a in doc["attachments"])


# ---------------------------------------------------------------------------
# Type Folders F1: "DocIndex Tables" folder per drive + save-as routing
# ---------------------------------------------------------------------------


def _f1_drive(app, user_id, name="Work", synced=False):
    with app.app_context():
        drive = _Drive(name=name, user_id=user_id,
                       source_path="/tmp/f1-real" if synced else None)
        db.session.add(drive)
        db.session.commit()
        return drive.id


def _f1_csv_in_drive(app, user_id, drive_id, name="data.csv",
                     payload=b"a,b\n1,2\n"):
    fid = _write_table_file(app, user_id, name, "csv", payload)
    with app.app_context():
        stored = db.session.get(_StoredFile, fid)
        stored.drive_id = drive_id
        db.session.commit()
    return fid


def test_tables_folder_per_drive(app, user):
    from app.models import User as _User

    drive_id = _f1_drive(app, user, name="Work")
    with app.app_context():
        user_obj = db.session.get(_User, user)
        drive = db.session.get(_Drive, drive_id)
        assert tables_doc.TABLES_FOLDER == "DocIndex Tables"
        folder = tables_doc.tables_folder(user_obj, drive)
        assert folder.name == "DocIndex Tables"
        assert folder.parent_id is None
        assert folder.drive_id == drive_id
        # Idempotent — second call returns the same folder.
        assert tables_doc.tables_folder(user_obj, drive).id == folder.id
        synced = _Drive(name="RO", user_id=user, source_path="/tmp/f1-ro")
        db.session.add(synced)
        db.session.commit()
        assert tables_doc.tables_folder(user_obj, synced) is None
        assert tables_doc.tables_folder(user_obj, None) is None


def test_save_as_lands_in_tables_folder(auth_client, app, user,
                                        tables_enabled):
    drive_id = _f1_drive(app, user, name="Work")
    fid = _f1_csv_in_drive(app, user, drive_id, "data.csv")
    r = auth_client.post(f"/m/tables/{fid}/save-as")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["name"] == "data.ptable"
    with app.app_context():
        created = db.session.get(_StoredFile, body["file_id"])
        assert created.drive_id == drive_id
        assert created.folder is not None
        assert created.folder.name == "DocIndex Tables"
        assert created.folder.parent_id is None
        assert created.folder.drive_id == drive_id
        source = db.session.get(_StoredFile, fid)
        assert source.folder_id is None
        assert source.drive_id == drive_id
    # A second save-as uniquifies inside the Tables folder, not next to
    # the source.
    again = auth_client.post(f"/m/tables/{fid}/save-as")
    assert again.status_code == 200
    assert again.get_json()["name"] == "data (2).ptable"
    with app.app_context():
        clash = db.session.get(_StoredFile, again.get_json()["file_id"])
        assert clash.folder.name == "DocIndex Tables"


def test_save_as_synced_source_goes_personal(auth_client, app, user,
                                             tables_enabled):
    from app.services import events

    with app.app_context():
        db.session.add(_Drive(name="Personal", user_id=user))
        db.session.add(_Drive(name="Archive Mirror", user_id=user,
                              source_path="/tmp/f1-mirror"))
        db.session.commit()
        personal = _Drive.query.filter_by(
            user_id=user, name="Personal").one()
        personal_id = personal.id
        mirror = _Drive.query.filter_by(
            user_id=user, name="Archive Mirror").one()
        mirror_id = mirror.id
    with _tempfile.NamedTemporaryFile(suffix=".csv",
                                       delete=False) as tmp:
        tmp.write(b"a,b\n1,2\n")
        real_path = tmp.name
    try:
        fid = _write_table_file(app, user, "archived.csv", "csv",
                                b"placeholder")
        with app.app_context():
            stored = db.session.get(_StoredFile, fid)
            stored.source_path = real_path
            stored.drive_id = mirror_id
            db.session.commit()
        seen = []
        sub = lambda p: seen.append(p)  # noqa: E731 - test probe
        events.subscribe("file.uploaded", sub)
        try:
            r = auth_client.post(f"/m/tables/{fid}/save-as")
        finally:
            listeners = events._SUBSCRIBERS.get("file.uploaded", [])
            if sub in listeners:
                listeners.remove(sub)
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        with app.app_context():
            created = db.session.get(_StoredFile, body["file_id"])
            assert created.drive_id == personal_id
            assert created.drive_id != mirror_id
            assert created.folder is not None
            assert created.folder.name == "DocIndex Tables"
            assert created.folder.drive_id == personal_id
        assert seen, "save-as must emit file.uploaded"
        assert seen[-1]["drive_id"] == personal_id
    finally:
        os.remove(real_path)


def test_save_as_without_personal_drive_stays_at_files_root(
        auth_client, app, user, tables_enabled):
    # No drive rows at all: files-root (drive_id=None) is a legitimate
    # location in this codebase (nullable FK, "No drive" group, orphan
    # migration), so it stays the last-resort destination instead of
    # failing the conversion.
    fid = _write_table_file(app, user, "loose.csv", "csv", b"a,b\n1,2\n")
    r = auth_client.post(f"/m/tables/{fid}/save-as")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    with app.app_context():
        assert _Drive.query.filter_by(user_id=user).count() == 0
        created = db.session.get(_StoredFile, body["file_id"])
        assert created.drive_id is None
        assert created.folder_id is None


# ---------------------------------------------------------------------------
# Type Folders F2: organize stray .ptables into per-drive "DocIndex Tables"
# ---------------------------------------------------------------------------


def _f2_ptable_in_drive(app, user_id, drive_id, name="stray.ptable",
                        folder_id=None):
    fid = _write_table_file(app, user_id, name, "ptable",
                            _ptable_payload())
    with app.app_context():
        stored = db.session.get(_StoredFile, fid)
        stored.drive_id = drive_id
        stored.folder_id = folder_id
        db.session.commit()
    return fid


def _f2_setup(app, user):
    """One drive with a stray root .ptable + one in a foreign folder, one
    already inside Tables/sub (left alone), and a synced-drive .ptable
    (skipped). Returns ids dict."""
    from app.models import Folder as _Folder

    drive_id = _f1_drive(app, user, name="Work")
    with app.app_context():
        from app.models import User as _User

        user_obj = db.session.get(_User, user)
        foreign = _Folder(name="Foreign", user_id=user,
                          drive_id=drive_id, parent_id=None)
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id
        tables_root = tables_doc.tables_folder(user_obj, db.session.get(
            _Drive, drive_id))
        sub = _Folder(name="Sub", user_id=user, drive_id=drive_id,
                      parent_id=tables_root.id)
        db.session.add(sub)
        db.session.commit()
        sub_id = sub.id
    synced_id = _f1_drive(app, user, name="RO", synced=True)
    stray = _f2_ptable_in_drive(app, user, drive_id, "stray.ptable")
    abroad = _f2_ptable_in_drive(app, user, drive_id, "abroad.ptable",
                                 folder_id=foreign_id)
    nested = _f2_ptable_in_drive(app, user, drive_id, "nested.ptable",
                                 folder_id=sub_id)
    rosy = _f2_ptable_in_drive(app, user, synced_id, "rosy.ptable")
    return {"drive_id": drive_id, "foreign_id": foreign_id,
            "stray": stray, "abroad": abroad, "nested": nested,
            "rosy": rosy}


def test_tables_organize_roundtrip(auth_client, app, user, tables_enabled):
    from app.models import TableOrganizePlan

    ids = _f2_setup(app, user)
    data = auth_client.get("/m/tables/organize/plan").get_json()
    assert data["skipped_synced"] == 1
    assert {m["name"] for m in data["moves"]} == {"stray.ptable",
                                                 "abroad.ptable"}
    for move in data["moves"]:
        assert move["to_drive_id"] == ids["drive_id"]
        assert move["to_folder_name"] == "DocIndex Tables"
        assert move["from_drive_id"] == ids["drive_id"]

    # Version history survives the move (normal move path, not a copy).
    with app.app_context():
        stored = db.session.get(_StoredFile, ids["stray"])
        _file_service.snapshot_version(stored, "test")
        before = _FileVersion.query.filter_by(
            file_id=ids["stray"]).count()
        assert before == 1

    resp = auth_client.post("/m/tables/organize/apply",
                            follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        for fid in (ids["stray"], ids["abroad"]):
            stored = db.session.get(_StoredFile, fid)
            assert stored.folder.name == "DocIndex Tables"
            assert stored.drive_id == ids["drive_id"]
        # Inside Tables/sub stays put; synced stays put.
        assert db.session.get(_StoredFile,
                              ids["nested"]).folder_id is not None
        assert db.session.get(_StoredFile,
                              ids["nested"]).folder.name == "Sub"
        assert db.session.get(_StoredFile, ids["rosy"]).folder_id is None
        assert _FileVersion.query.filter_by(
            file_id=ids["stray"]).count() == before
        plan = TableOrganizePlan.query.one()
        assert plan.applied is True and plan.reverted is False
    # Plan is now empty.
    assert auth_client.get("/m/tables/organize/plan").get_json()[
        "moves"] == []


def test_tables_organize_revert(auth_client, app, user, tables_enabled):
    from app.models import TableOrganizePlan

    ids = _f2_setup(app, user)
    auth_client.post("/m/tables/organize/apply", follow_redirects=True)
    auth_client.post("/m/tables/organize/revert", follow_redirects=True)
    with app.app_context():
        assert db.session.get(_StoredFile, ids["stray"]).folder_id is None
        assert db.session.get(_StoredFile,
                              ids["abroad"]).folder_id == ids["foreign_id"]
        assert TableOrganizePlan.query.one().reverted is True
    # Nothing left to revert.
    resp = auth_client.post("/m/tables/organize/revert",
                            follow_redirects=True)
    assert b"No organize run" in resp.data


def test_tables_viewer_organize_trigger(auth_client, app, user,
                                        tables_enabled):
    drive_id = _f1_drive(app, user, name="Work")
    fid = _f2_ptable_in_drive(app, user, drive_id, "stray.ptable")
    html = auth_client.get(f"/m/tables/{fid}").data.decode()
    # Preview-first gating language, adapted to the viewer context.
    assert "tables-organize-btn" in html
    assert "nothing moves until you confirm" in html.lower()
    assert "DocIndex Tables" in html
    assert "Revert last organize" not in html
    # After an organize run the viewer offers the revert affordance.
    auth_client.post("/m/tables/organize/apply", follow_redirects=True)
    html = auth_client.get(f"/m/tables/{fid}").data.decode()
    assert "Revert last organize" in html


# ---------------------------------------------------------------------------
# X3: Tables viewer honesty — history entry, filtered notice, no-match Reset,
# kept stats row
# ---------------------------------------------------------------------------


def test_viewer_links_history(auth_client, app, user, tables_enabled):
    import re

    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _ptable_payload())
    html = auth_client.get(f"/m/tables/{fid}").data.decode()
    # History/version entry point is server-rendered in the viewer.
    assert "tables-history-btn" in html
    assert "History" in html
    assert "tables-history-panel" in html
    # No versions yet: empty-state copy, no per-version links.
    assert "No previous versions yet" in html
    assert f"/file/{fid}/history/" not in html
    # After an edit the panel reuses the existing drive per-version routes.
    r = auth_client.post(f"/m/tables/{fid}/cell",
                         json={"row": 0, "col": "a", "value": "9"})
    assert r.status_code == 200
    html = auth_client.get(f"/m/tables/{fid}").data.decode()
    assert f"/file/{fid}/history/" in html
    assert "Download" in html
    assert re.search(r"tables-history-btn[^>]*>.*?History",
                     html, re.S)


def test_viewer_filtered_notice_and_nomatch_reset(auth_client, app, user,
                                                  tables_enabled):
    import re

    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            _ptable_payload())
    html = auth_client.get(f"/m/tables/{fid}").data.decode()
    # Filtered-view notice: editing is disabled, points at Reset.
    assert "tables-filtered-notice" in html
    assert "tables-notice-reset" in html
    notice = re.search(r'id="tables-filtered-notice".*?</div>',
                       html, re.S).group(0)
    assert "editing is disabled" in notice.lower()
    assert "Reset" in notice
    # No-match block: offers the Reset path forward.
    assert "tables-nomatch" in html
    assert "tables-nomatch-reset" in html
    nomatch = re.search(r'id="tables-nomatch".*?</div>', html, re.S).group(0)
    assert "No matching rows" in nomatch
    assert "Reset" in nomatch


def test_query_and_rows_keep_column_stats(auth_client, app, user,
                                          tables_enabled):
    doc = tables_doc.new_table([{"name": "price", "type": "number"},
                                {"name": "label", "type": "text"}],
                               [["1", "x"], ["2", "y"]])
    fid = _write_table_file(app, user, "t.ptable", "ptable",
                            tables_doc.serialize(doc).encode("utf-8"))
    # Filtered view recomputes the stats row from the filtered result.
    out = auth_client.post(
        f"/m/tables/{fid}/query",
        json={"sql": "SELECT * FROM t0"}).get_json()
    assert out["ok"] is True
    by_name = {c["name"]: c for c in out["columns"]}
    assert by_name["label"].get("stats") is None
    stats = by_name["price"].get("stats")
    assert stats is not None
    assert stats["sum"] == "3"
    assert stats["avg"] == "1.5"
    assert stats["min"] == "1"
    assert stats["max"] == "2"
    assert stats["nulls"] == 0
    # Unfiltered paging keeps the header stats too (same renderHead path).
    rows = auth_client.get(f"/m/tables/{fid}/rows").get_json()
    rby_name = {c["name"]: c for c in rows["columns"]}
    assert rby_name["price"].get("stats") is not None
    assert rby_name["price"]["stats"]["sum"] == "3"
