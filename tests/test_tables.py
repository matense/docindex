"""Tables module (T1): .ptable doc service + module enablement."""

import importlib
import importlib.util
import os
import sys

import pytest

from app.extensions import db
from app.models import ModuleState
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

import uuid as _uuid

from app.models import StoredFile as _StoredFile
from app.services import file_service as _file_service


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
    payload = "id,amount\n1,10\n2,20\n3,30\n".encode("utf-8")
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
                            "a\tb\n1\t2\n".encode("utf-8"))
    tdata = auth_client.get(f"/m/tables/{tsv}/rows").get_json()
    assert [c["name"] for c in tdata["columns"]] == ["a", "b"]
    assert tdata["rows"] == [["1", "2"]]
