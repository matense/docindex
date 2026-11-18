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
