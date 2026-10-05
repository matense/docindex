"""Workflow graph validation and execution engine.

Graph format (canonical JSON stored on the workflow):

    {"nodes": [{"id": "n1", "type": "input.text", "x": 40, "y": 60,
                "config": {...}}],
     "edges": [{"from": "n1", "from_port": "out",
                "to": "n2", "to_port": "in"}]}

Data between nodes travels in typed payload envelopes::

    {"kind": "text"|"json"|"table"|"files", "data": ...}

``table`` data is {"columns": [...], "rows": [[...]]}; ``files`` data is a
list of {"file_id", "name"} drive references. Executors convert between
kinds with the ``as_*`` helpers, which raise WorkflowError with a clear
message when the conversion is impossible.

Execution walks the graph in topological order. A node runs when at least
one inbound edge carries data (or it has no inbound edges); nodes whose
inputs were all pruned by a logic.condition are skipped, and the skip
propagates downstream. Text fields of every node are scanned for
``{{ node_id[.port][.path] }}`` references, which act as implicit ordering
dependencies in addition to the drawn edges.
"""

import csv
import io
import json
import re
import time

from .registry import active_node_types


class WorkflowError(Exception):
    """A user-facing workflow problem (validation or node failure)."""


# --------------------------------------------------------------------------
# Payload helpers
# --------------------------------------------------------------------------

def p_text(data):
    return {"kind": "text", "data": "" if data is None else str(data)}


def p_json(data):
    return {"kind": "json", "data": data}


def p_table(columns, rows):
    return {"kind": "table", "data": {"columns": list(columns),
                                      "rows": [list(r) for r in rows]}}


def p_files(files):
    return {"kind": "files", "data": list(files)}


def as_text(payload):
    kind, data = payload["kind"], payload["data"]
    if kind == "text":
        return data
    if kind == "json":
        return (data if isinstance(data, str)
                else json.dumps(data, ensure_ascii=False, indent=2))
    if kind == "table":
        return table_to_csv(data)
    if kind == "files":
        return "\n".join(f.get("name", "") for f in data)
    return str(data)


def as_json(payload):
    kind, data = payload["kind"], payload["data"]
    if kind == "json":
        return data
    if kind == "text":
        try:
            return json.loads(data)
        except ValueError:
            raise WorkflowError("Expected JSON data, but the text is not "
                                "valid JSON.")
    raise WorkflowError(f"Cannot use a {kind} payload as JSON data.")


def as_table(payload):
    kind, data = payload["kind"], payload["data"]
    if kind == "table":
        return data
    if kind == "text":
        return csv_to_table(data)
    raise WorkflowError(f"Cannot use a {kind} payload as a table.")


def as_files(payload):
    kind, data = payload["kind"], payload["data"]
    if kind == "files":
        return data
    raise WorkflowError(f"Cannot use a {kind} payload as a file list.")


def table_to_csv(table):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(table.get("columns", []))
    writer.writerows(table.get("rows", []))
    return buf.getvalue()


def csv_to_table(text):
    reader = csv.reader(io.StringIO(text or ""))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return {"columns": [], "rows": []}
    return {"columns": rows[0], "rows": rows[1:]}


# --------------------------------------------------------------------------
# Pandas bridges for the table transform nodes (pandas is imported lazily
# so the module loads fast and the dependency stays optional at import time)
# --------------------------------------------------------------------------

def table_to_df(table):
    """Table payload → DataFrame, coercing columns that are fully numeric."""
    import pandas as pd
    df = pd.DataFrame(table.get("rows", []),
                      columns=[str(c) for c in table.get("columns", [])])
    for col in df.columns:
        as_str = df[col].astype(str).str.strip()
        non_empty = as_str != ""
        if not non_empty.any():
            continue
        converted = pd.to_numeric(df[col].where(non_empty), errors="coerce")
        if converted[non_empty].notna().all():
            df[col] = converted
    return df


def df_to_table(df):
    """DataFrame → table payload; NaN becomes "" and numpy scalars become
    plain Python values so the payload stays JSON-friendly."""
    clean = df.astype(object).where(df.notna(), "")
    rows = [[v.item() if hasattr(v, "item") and not isinstance(v, str) else v
             for v in row] for row in clean.values.tolist()]
    return {"columns": [str(c) for c in df.columns], "rows": rows}


# --------------------------------------------------------------------------
# Template substitution: {{ node_id[.port][.path.into.data] }}
# --------------------------------------------------------------------------

_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_][\w.\-]*)\s*\}\}")


def _resolve_ref(ref, outputs):
    parts = ref.split(".")
    node_id = parts[0]
    if node_id not in outputs:
        raise WorkflowError(f"Template references '{node_id}', which has no "
                            "output (check the node id or run order).")
    node_out = outputs[node_id]
    idx = 1
    port = None
    if len(parts) > 1 and parts[1] in node_out:
        port = parts[1]
        idx = 2
    else:
        taken = [p for p, v in node_out.items() if v is not None]
        if not taken:
            raise WorkflowError(f"Node '{node_id}' produced no output.")
        port = taken[0]
    payload = node_out.get(port)
    if payload is None:
        raise WorkflowError(f"Port '{node_id}.{port}' was not taken "
                            "(its branch did not run).")
    value = payload["data"]
    for part in parts[idx:]:
        try:
            if isinstance(value, (list, tuple)):
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                raise KeyError(part)
        except (KeyError, IndexError, ValueError):
            raise WorkflowError(f"Template path '{ref}' does not exist.")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def render_template(text, outputs):
    """Replace {{ ... }} references with upstream node outputs."""
    if not text:
        return ""
    return _REF_RE.sub(lambda m: _resolve_ref(m.group(1), outputs),
                       str(text))


def _referenced_nodes(graph_nodes):
    """Node ids referenced in {{ }} templates — implicit dependencies."""
    deps = {}
    for node in graph_nodes:
        refs = set()
        for value in (node.get("config") or {}).values():
            if isinstance(value, str):
                refs.update(m.group(1).split(".")[0]
                            for m in _REF_RE.finditer(value))
        refs.discard(node["id"])
        deps[node["id"]] = refs
    return deps


# --------------------------------------------------------------------------
# Validation + topological order
# --------------------------------------------------------------------------

def parse_graph(raw):
    if not raw:
        return {"nodes": [], "edges": []}
    try:
        graph = json.loads(raw)
    except ValueError:
        raise WorkflowError("The workflow graph is not valid JSON.")
    if not isinstance(graph, dict) or not isinstance(
            graph.get("nodes", []), list) or not isinstance(
            graph.get("edges", []), list):
        raise WorkflowError("The workflow graph has an unexpected shape.")
    return {"nodes": graph.get("nodes") or [],
            "edges": graph.get("edges") or []}


def validate_graph(graph, node_types=None):
    """Validate the graph; returns (nodes_by_id, edges, topo_order).

    ``node_types`` defaults to the active registry — pass explicitly in
    tests. Raises WorkflowError on unknown types, bad ports, missing
    required inputs or cycles.
    """
    node_types = node_types if node_types is not None else active_node_types()
    nodes_by_id = {}
    for node in graph["nodes"]:
        nid = node.get("id")
        ntype = node.get("type")
        if not nid or not isinstance(nid, str):
            raise WorkflowError("Every node needs an id.")
        if nid in nodes_by_id:
            raise WorkflowError(f"Duplicate node id '{nid}'.")
        if ntype not in node_types:
            raise WorkflowError(f"Unknown node type '{ntype}' (node '{nid}').")
        node.setdefault("config", {})
        nodes_by_id[nid] = node

    # input.param names must be unique — they key the run form answers.
    param_names = {}
    for node in graph["nodes"]:
        if node.get("type") != "input.param":
            continue
        pname = (node["config"].get("param") or "").strip()
        if not pname:
            raise WorkflowError(
                f"Run input node '{node['id']}' needs a parameter name.")
        if pname in param_names:
            raise WorkflowError(
                f"Run parameter '{pname}' is used by two nodes "
                f"('{param_names[pname]}' and '{node['id']}') — names must "
                "be unique.")
        param_names[pname] = node["id"]

    implicit = _referenced_nodes(graph["nodes"])
    edges = []
    deps = {nid: set() for nid in nodes_by_id}
    for edge in graph["edges"]:
        src, dst = edge.get("from"), edge.get("to")
        if src not in nodes_by_id or dst not in nodes_by_id:
            raise WorkflowError("An edge connects a node that does not exist.")
        if src == dst:
            raise WorkflowError(f"Node '{src}' connects to itself.")
        src_spec = node_types[nodes_by_id[src]["type"]]["spec"]
        dst_spec = node_types[nodes_by_id[dst]["type"]]["spec"]
        out_ports = [p["name"] for p in src_spec.get("outputs", [])]
        in_ports = [p["name"] for p in dst_spec.get("inputs", [])]
        if edge.get("from_port") not in out_ports:
            raise WorkflowError(
                f"Node '{src}' has no output port '{edge.get('from_port')}'.")
        if edge.get("to_port") not in in_ports:
            raise WorkflowError(
                f"Node '{dst}' has no input port '{edge.get('to_port')}'.")
        edges.append({"from": src, "from_port": edge["from_port"],
                      "to": dst, "to_port": edge["to_port"]})
        deps[dst].add(src)
    for nid, refs in implicit.items():
        for ref in refs:
            if ref in nodes_by_id and ref != nid:
                deps[nid].add(ref)

    # Kahn's algorithm — also the cycle check.
    order, ready = [], sorted(n for n, d in deps.items() if not d)
    remaining = {n: set(d) for n, d in deps.items()}
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for other, d in remaining.items():
            if nid in d:
                d.discard(nid)
                if not d and other not in order and other not in ready:
                    ready.append(other)
    if len(order) != len(nodes_by_id):
        raise WorkflowError("The workflow contains a cycle — flows must go "
                            "in one direction.")
    return nodes_by_id, edges, order


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def execute_graph(graph, ctx, node_types=None, should_stop=None, log=None):
    """Run a validated graph; returns (outputs, results, files).

    ``ctx``: {"user", "app"} passed through to executors.
    ``log``: optional callable(node_id, label, status, summary, ms).
    ``should_stop``: optional zero-arg callable — checked between nodes;
    returns True to abort with a _Stopped marker.
    ``results``: payloads fed to output.result nodes (previews).
    ``files``: file references created by output.save_file nodes.
    """
    node_types = node_types if node_types is not None else active_node_types()
    nodes_by_id, edges, order = validate_graph(graph, node_types)
    inbound = {nid: [] for nid in nodes_by_id}
    for edge in edges:
        inbound[edge["to"]].append(edge)

    outputs = {}   # node_id -> {port: payload | None}
    results = []   # [{"node", "kind", "preview"}]
    created = []   # [{"file_id", "name"}]
    node_ctx = dict(ctx)

    for nid in order:
        if should_stop is not None and should_stop():
            raise _Stopped()
        node = nodes_by_id[nid]
        spec = node_types[node["type"]]["spec"]
        executor = node_types[node["type"]]["executor"]
        label = spec.get("label", node["type"])
        started = time.monotonic()
        notes = []
        node_ctx["log"] = notes.append

        inputs = {}
        for edge in inbound[nid]:
            src_out = outputs.get(edge["from"], {})
            payload = src_out.get(edge["from_port"])
            if payload is not None:
                inputs[edge["to_port"]] = payload

        if inbound[nid] and not inputs:
            outputs[nid] = {p["name"]: None
                            for p in spec.get("outputs", [])}
            if log:
                log(nid, label, "skipped", "branch not taken", 0)
            continue

        try:
            node_ctx["_outputs"] = outputs  # for {{ node_id }} resolution
            produced = executor(node_ctx, node["config"], inputs)
            if spec.get("outputs") and isinstance(produced, dict) and not (
                    set(produced) & {"kind", "data"}):
                outputs[nid] = {p["name"]: produced.get(p["name"])
                                for p in spec["outputs"]}
            elif len(spec.get("outputs", [])) == 1:
                outputs[nid] = {spec["outputs"][0]["name"]: produced}
            else:
                outputs[nid] = {}
            if node["type"] == "output.result":
                payload = next((v for v in outputs[nid].values()
                                if v is not None), None)
                if payload is not None:
                    preview = as_text(payload)
                    results.append({"node": nid, "kind": payload["kind"],
                                    "preview": preview[:2000]})
            if node["type"] in ("output.save_file", "output.save_table"):
                for ref in (node_ctx.get("created_files") or []):
                    if ref not in created:
                        created.append(ref)
            ms = int((time.monotonic() - started) * 1000)
            summary = "; ".join(notes) if notes else "ok"
            if log:
                log(nid, label, "done", summary[:300], ms)
        except _Stopped:
            raise
        except WorkflowError as exc:
            ms = int((time.monotonic() - started) * 1000)
            if log:
                log(nid, label, "error", str(exc)[:300], ms)
            raise WorkflowError(f"{label}: {exc}")
        except Exception as exc:  # noqa: BLE001 - surface as run error
            ms = int((time.monotonic() - started) * 1000)
            if log:
                log(nid, label, "error", str(exc)[:300], ms)
            raise WorkflowError(f"{label}: unexpected error: {exc}")

    return outputs, results, created


class _Stopped(Exception):
    """Cooperative stop requested between nodes."""
