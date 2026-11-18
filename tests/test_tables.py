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
