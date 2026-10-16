"""Table transform and output nodes backed by pandas.

Registered from nodes.py::register_defaults. Every executor takes table
payloads ({"columns", "rows"}) and returns one; pandas does the heavy
lifting via engine.table_to_df / df_to_table.
"""

from .engine import (WorkflowError, as_table, df_to_table, p_table,
                     render_template, table_to_csv, table_to_df)
from .nodes import _save_blob_to_drive
from .registry import register_node_type

_AGG_FUNCS = {"sum": "sum", "count": "count", "avg": "mean",
              "min": "min", "max": "max"}


def _require_column(df, name, node_label):
    if name not in df.columns:
        raise WorkflowError(
            f"{node_label}: column '{name}' does not exist — the input "
            f"table has: {', '.join(str(c) for c in df.columns) or '(none)'}.")


def _exec_table_aggregate(ctx, config, inputs):
    payload = inputs.get("in")
    if payload is None:
        raise WorkflowError("No input — connect a table to aggregate.")
    df = table_to_df(as_table(payload))
    groups = [g for g in (config.get("groups") or []) if g]
    aggs = [a for a in (config.get("aggs") or []) if a.get("column")]
    if not aggs:
        raise WorkflowError("Add at least one aggregation (column + "
                            "function).")
    for col in groups:
        _require_column(df, col, "Aggregate")
    named = {}
    for agg in aggs:
        col = agg["column"]
        _require_column(df, col, "Aggregate")
        func = _AGG_FUNCS.get(agg.get("func") or "sum")
        if func is None:
            raise WorkflowError(f"Unknown aggregation '{agg.get('func')}'.")
        alias = (agg.get("as") or "").strip() or f"{col}_{agg.get('func') or 'sum'}"
        named[alias] = (col, func)
    if groups:
        out = df.groupby(groups).agg(**named).reset_index()
    else:
        # No grouping: a single row with one value per aggregation.
        data = {alias: [getattr(df[col], func)()]
                for alias, (col, func) in named.items()}
        import pandas as pd
        out = pd.DataFrame(data)
    ctx["log"](f"{len(df)} rows → {len(out)} row(s)"
               + (f" grouped by {', '.join(groups)}" if groups else ""))
    return p_table(**df_to_table(out))


def _exec_table_join(ctx, config, inputs):
    left_p, right_p = inputs.get("in1"), inputs.get("in2")
    if left_p is None or right_p is None:
        raise WorkflowError("Connect a table to both inputs (in1 and in2).")
    left, right = table_to_df(as_table(left_p)), table_to_df(as_table(right_p))
    left_on = (config.get("left_on") or "").strip()
    right_on = (config.get("right_on") or "").strip()
    if not left_on or not right_on:
        raise WorkflowError("Pick the join column on each side.")
    _require_column(left, left_on, "Join (left)")
    _require_column(right, right_on, "Join (right)")
    how = config.get("how") or "inner"
    if how not in ("inner", "left", "right", "outer"):
        raise WorkflowError(f"Unknown join type '{how}'.")
    out = left.merge(right, left_on=left_on, right_on=right_on, how=how)
    ctx["log"](f"{len(left)} × {len(right)} rows → {len(out)} ({how} join "
               f"on {left_on} = {right_on})")
    return p_table(**df_to_table(out))


def _exec_table_sort(ctx, config, inputs):
    payload = inputs.get("in")
    if payload is None:
        raise WorkflowError("No input — connect a table to sort.")
    df = table_to_df(as_table(payload))
    column = (config.get("column") or "").strip()
    if not column:
        raise WorkflowError("Pick the column to sort by.")
    _require_column(df, column, "Sort")
    ascending = (config.get("direction") or "asc") != "desc"
    out = df.sort_values(column, ascending=ascending,
                         na_position="last").reset_index(drop=True)
    ctx["log"](f"sorted {len(out)} rows by '{column}' "
               f"({'ascending' if ascending else 'descending'})")
    return p_table(**df_to_table(out))


def _exec_table_dedupe(ctx, config, inputs):
    payload = inputs.get("in")
    if payload is None:
        raise WorkflowError("No input — connect a table to dedupe.")
    df = table_to_df(as_table(payload))
    subset = [c for c in (config.get("columns") or []) if c]
    for col in subset:
        _require_column(df, col, "Dedupe")
    before = len(df)
    out = df.drop_duplicates(subset=subset or None).reset_index(drop=True)
    ctx["log"](f"removed {before - len(out)} duplicate row(s)"
               + (f" on {', '.join(subset)}" if subset else ""))
    return p_table(**df_to_table(out))


def _exec_table_compute(ctx, config, inputs):
    payload = inputs.get("in")
    if payload is None:
        raise WorkflowError("No input — connect a table.")
    df = table_to_df(as_table(payload))
    new_column = (config.get("new_column") or "").strip()
    formula = (config.get("formula") or "").strip()
    if not new_column or not formula:
        raise WorkflowError("Configure the new column name and its formula.")
    if new_column in df.columns:
        raise WorkflowError(f"Column '{new_column}' already exists.")
    try:
        df[new_column] = df.eval(formula, engine="python")
    except Exception as exc:  # pandas raises several exception types
        raise WorkflowError(f"The formula could not be evaluated: {exc}")
    ctx["log"](f"computed column '{new_column}' = {formula}")
    return p_table(**df_to_table(df))


def _exec_save_table(ctx, config, inputs):
    payload = inputs.get("in")
    if payload is None:
        raise WorkflowError("No input — connect the table to save.")
    df = table_to_df(as_table(payload))
    name = render_template(config.get("name", ""), ctx["_outputs"]).strip()
    fmt = config.get("format") or "csv"
    if fmt not in ("csv", "xlsx"):
        raise WorkflowError(f"Unknown table format '{fmt}'.")
    if not name.lower().endswith("." + fmt):
        base = name.rsplit(".", 1)[0] if "." in name else name
        name = f"{base or 'table'}.{fmt}"
    if fmt == "xlsx":
        import io as _io
        buf = _io.BytesIO()
        df.to_excel(buf, index=False)
        blob = buf.getvalue()
    else:
        blob = table_to_csv(df_to_table(df)).encode("utf-8")
    return _save_blob_to_drive(ctx, config, blob, name)


def register_table_nodes():
    """Pandas-backed table nodes — called once from register_defaults."""
    register_node_type("transform.table_aggregate", {
        "label": "Aggregate / group by", "icon": "fa-calculator",
        "category": "Data",
        "description": "Summarizes a table: groups rows by the columns "
                       "you pick and calculates totals, counts, averages, "
                       "minimums or maximums per group.",
        "example": "Group by region, sum of total as Revenue — one row "
                   "per region.",
        "inputs": [{"name": "in", "kind": "table"}],
        "outputs": [{"name": "out", "kind": "table"}],
        "fields": [
            {"name": "groups", "label": "Group by", "kind": "column_list",
             "default": [],
             "help": "Optional — without groups you get one row for the "
                     "whole table."},
            {"name": "aggs", "label": "Calculations", "kind": "agg_rows",
             "required": True, "default": [],
             "help": "One row per calculation: the column, the function "
                     "and an optional name for the result column."},
        ]}, _exec_table_aggregate)

    register_node_type("transform.table_join", {
        "label": "Join tables", "icon": "fa-link",
        "category": "Data",
        "description": "Combines two tables side by side by matching a "
                       "column — like a VLOOKUP across the whole table.",
        "example": "orders.customer_id = customers.id, left join — every "
                   "order with its customer data.",
        "inputs": [{"name": "in1", "kind": "table"},
                   {"name": "in2", "kind": "table"}],
        "outputs": [{"name": "out", "kind": "table"}],
        "fields": [
            {"name": "left_on", "label": "Match column (left / in1)",
             "kind": "column", "required": True, "default": "", "port": "in1"},
            {"name": "right_on", "label": "Match column (right / in2)",
             "kind": "column", "required": True, "default": "", "port": "in2"},
            {"name": "how", "label": "Join type", "kind": "select",
             "options": [
                 {"value": "inner", "label": "Only matching rows (inner)"},
                 {"value": "left", "label": "All left rows (left)"},
                 {"value": "right", "label": "All right rows (right)"},
                 {"value": "outer", "label": "Everything (outer)"}],
             "default": "inner"},
        ]}, _exec_table_join)

    register_node_type("transform.table_sort", {
        "label": "Sort rows", "icon": "fa-arrow-down-wide-short",
        "category": "Data",
        "description": "Orders the rows of a table by one column, "
                       "ascending or descending.",
        "example": "Sort by total, descending — biggest sale first.",
        "inputs": [{"name": "in", "kind": "table"}],
        "outputs": [{"name": "out", "kind": "table"}],
        "fields": [
            {"name": "column", "label": "Sort by", "kind": "column",
             "required": True, "default": ""},
            {"name": "direction", "label": "Direction", "kind": "select",
             "options": [{"value": "asc", "label": "Ascending (A→Z, 0→9)"},
                         {"value": "desc", "label": "Descending"}],
             "default": "asc"},
        ]}, _exec_table_sort)

    register_node_type("transform.table_dedupe", {
        "label": "Remove duplicates", "icon": "fa-clone",
        "category": "Data",
        "description": "Drops repeated rows, keeping the first occurrence. "
                       "Pick columns to compare, or leave empty to compare "
                       "whole rows.",
        "example": "Dedupe on email — one row per contact.",
        "inputs": [{"name": "in", "kind": "table"}],
        "outputs": [{"name": "out", "kind": "table"}],
        "fields": [
            {"name": "columns", "label": "Compare these columns",
             "kind": "column_list", "default": [],
             "help": "Empty = a row is a duplicate only when every column "
                     "matches."},
        ]}, _exec_table_dedupe)

    register_node_type("transform.table_compute", {
        "label": "Compute column", "icon": "fa-square-root-variable",
        "category": "Data",
        "description": "Adds a new column calculated from the others with "
                       "a formula — column names work like variables.",
        "example": "New column total = price * qty * 1.23.",
        "inputs": [{"name": "in", "kind": "table"}],
        "outputs": [{"name": "out", "kind": "table"}],
        "fields": [
            {"name": "new_column", "label": "New column name",
             "kind": "text", "required": True, "default": "",
             "placeholder": "total"},
            {"name": "formula", "label": "Formula", "kind": "text",
             "required": True, "default": "", "placeholder": "price * qty",
             "help": "Use column names, numbers and + - * / ( ). Columns "
                     "with spaces need backticks: `unit price` * qty."},
        ]}, _exec_table_compute)

    register_node_type("output.save_table", {
        "label": "Save table as CSV/Excel", "icon": "fa-file-excel",
        "category": "Output",
        "description": "Writes the incoming table to a real CSV or Excel "
                       "file on one of your drives.",
        "example": "The filtered sales table becomes report.xlsx in the "
                   "Reports drive.",
        "inputs": [{"name": "in", "kind": "table"}],
        "outputs": [{"name": "out", "kind": "files"}],
        "fields": [
            {"name": "name", "label": "File name", "kind": "text",
             "default": "table.csv", "placeholder": "report.xlsx",
             "help": "Supports {{ node_id }} references; the extension is "
                     "adjusted to the format."},
            {"name": "format", "label": "Format", "kind": "select",
             "options": [{"value": "csv", "label": "CSV"},
                         {"value": "xlsx", "label": "Excel (.xlsx)"}],
             "default": "csv"},
            {"name": "drive_id", "label": "Drive", "kind": "drive",
             "required": True, "default": "",
             "help": "Must be writable (not a synced drive)."},
            {"name": "folder", "label": "Folder", "kind": "folder",
             "return": "path", "default": "", "placeholder": "Reports/2026",
             "help": "Pick an existing folder — or leave empty for the drive root."},
        ]}, _exec_save_table)
