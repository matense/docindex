"""Tables module document service.

A table is a ``.ptable`` JSON document::

    {"version": 1, "columns": [{"name": ..., "type": ...}],
     "rows": [[cell, ...], ...]}

``type`` is one of ``number|date|category|text``; unknown types coerce to
``text`` on the way in. ``validate`` is strict on shape: every row must be
a list exactly as wide as ``columns`` — ragged rows raise instead of being
padded. Padding short rows (or trimming long ones) is a viewer concern
(T2 import/viewer), so the stored document shape stays unambiguous.
"""

import json

EXTENSION = "ptable"

COLUMN_TYPES = ("number", "date", "category", "text")


def _coerce_column(col):
    if isinstance(col, str):
        return {"name": col, "type": "text"}
    if isinstance(col, dict):
        ctype = col.get("type", "text")
        return {"name": str(col.get("name", "")),
                "type": ctype if ctype in COLUMN_TYPES else "text"}
    raise ValueError(f"Each column must be a name or {{'name', 'type'}} "
                     f"object, got {col!r}.")


def new_table(columns, rows):
    """Build a table document from column specs and row lists."""
    if not isinstance(columns, list):
        raise ValueError("Columns must be a list.")
    if not isinstance(rows, list):
        raise ValueError("Rows must be a list of lists.")
    cols = [_coerce_column(col) for col in columns]
    width = len(cols)
    clean_rows = []
    for row in rows:
        if not isinstance(row, list):
            raise ValueError("Each row must be a list of cells.")
        if len(row) != width:
            raise ValueError(
                f"Ragged row: got {len(row)} cells for {width} columns.")
        clean_rows.append(list(row))
    return validate({"version": 1, "columns": cols, "rows": clean_rows})


def validate(doc):
    """Normalize a table document dict; raises ValueError on bad shape."""
    if not isinstance(doc, dict):
        raise ValueError("Document must be an object.")
    if doc.get("version", 1) != 1:
        raise ValueError(f"Unsupported table version "
                         f"{doc.get('version')!r}.")
    doc["version"] = 1
    columns = doc.get("columns")
    if not isinstance(columns, list):
        raise ValueError("Document must have a columns list.")
    doc["columns"] = [_coerce_column(col) for col in columns]
    rows = doc.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Document must have a rows list.")
    width = len(doc["columns"])
    clean = []
    for row in rows:
        if not isinstance(row, list):
            raise ValueError("Each row must be a list of cells.")
        # Strict on shape: ragged rows raise here; padding is the
        # viewer/import concern (T2), not the stored-document contract.
        if len(row) != width:
            raise ValueError(
                f"Ragged row: got {len(row)} cells for {width} columns.")
        clean.append(list(row))
    doc["rows"] = clean
    return doc


def serialize(doc):
    return json.dumps(doc, ensure_ascii=False, indent=2)


def load(text):
    try:
        return validate(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corrupt table file: {exc}") from exc
