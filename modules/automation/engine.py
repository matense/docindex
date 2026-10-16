"""Workflow graph validation and execution engine.

Graph format (canonical JSON stored on the workflow):

    {"nodes": [{"id": "n1", "type": "input.text", "x": 40, "y": 60,
                "config": {...}}],
     "edges": [{"from": "n1", "from_port": "out",
                "to": "n2", "to_port": "in"}]}

Data between nodes travels in typed payload envelopes::

    {"kind": "text"|"json"|"table"|"files"|"drive", "data": ...}

``table`` data is {"columns": [...], "rows": [[...]]}; ``files`` data is a
list of {"file_id", "name"} drive references; ``drive`` data is
{"id", "name"} — produced by input.drive and usable as {{ ref }} inside
any Drive field of a save/folder node. Executors convert between
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


def p_drive(drive_id, name=""):
    return {"kind": "drive", "data": {"id": int(drive_id), "name": name}}


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
    if kind == "drive":
        return data.get("name", "") if isinstance(data, dict) else str(data)
    return str(data)


def as_json(payload):
    kind, data = payload["kind"], payload["data"]
    if kind == "json":
        return data
    if kind == "drive":
        return data
    if kind == "text":
        try:
            return json.loads(data)
        except ValueError:
            raise WorkflowError("Expected JSON data, but the text is not "
                                "valid JSON.")
    raise WorkflowError(f"Cannot use a {kind} payload as JSON data.")


def as_drive_id(payload):
    """A drive payload (or id) → integer drive id."""
    kind, data = payload["kind"], payload["data"]
    if kind == "drive" and isinstance(data, dict) and "id" in data:
        return int(data["id"])
    raise WorkflowError(f"Cannot use a {kind} payload as a drive — connect "
                        "a Drive picker node or pick a drive.")


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

        def _scan(value):
            if isinstance(value, str):
                refs.update(m.group(1).split(".")[0]
                            for m in _REF_RE.finditer(value))
            elif isinstance(value, dict):
                for v in value.values():
                    _scan(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    _scan(v)

        _scan(node.get("config") or {})
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


# --------------------------------------------------------------------------
# Static diagnosis: full vision of problems WITHOUT running.
# --------------------------------------------------------------------------

# Per-type required config (field -> human message when empty). The AI uses
# this to understand and fix workflows without executing them.
_REQUIRED_CONFIG = {
    "input.file": (("file_id", "Pick a file (Browse drives)."),),
    "input.drive": (("param", "Give this drive input a parameter name."),),
    "input.param": (("param", "Give this run input a parameter name."),),
    "input.param": (("param", "Give this input a parameter name."),),
    "docindex.search_files": (("query", "Type a search query."),),
    "transform.template": (("template", "Write the template (use {{ n1 }} refs)."),),
    "transform.table_filter": (("conditions", "Add at least one condition."),),
    "transform.table_select": (("picks", "Pick at least one column."),),
    "transform.table_sort": (("column", "Pick the column to sort by."),),
    "transform.table_join": (
        ("left_on", "Pick the left match column."),
        ("right_on", "Pick the right match column.")),
    "transform.table_aggregate": (("aggs", "Add at least one calculation."),),
    "transform.table_compute": (
        ("new_column", "Name the new column."),
        ("formula", "Write the formula (e.g. price * qty).")),
    "ai.prompt": (("prompt", "Write the prompt (what should the AI do?)."),),
    "ai.agent": (("prompt", "Describe the task for the agent."),),
    "ai.extract_json": (("prompt", "Say what to extract."),),
    "output.save_file": (("drive_id", "Pick a writable drive."),),
    "output.save_table": (("drive_id", "Pick a writable drive."),),
    "output.copy_file": (("drive_id", "Pick a target drive."),),
    "notebook.input": (("notebook_id", "Pick a notebook."),),
    "input.folder_list": (("drive_id", "Pick a drive."),),
    "input.http": (("url", "Type the URL to fetch."),),
}


def diagnose_graph(graph, node_types=None):
    """Static check of a workflow WITHOUT running it.

    Returns {"ok": bool, "issues": [{node, type, field, severity, message}]}.
    severity is "error" (will fail) or "warning" (may fail). The AI calls
    this (via validate_workflow) to see every problem at once, fix them
    with edit_workflow, and re-check — no run needed.
    """
    node_types = node_types if node_types is not None else active_node_types()
    issues = []

    def _issue(nid, ntype, field, severity, message):
        issues.append({"node": nid, "type": ntype, "field": field,
                       "severity": severity, "message": message})

    try:
        nodes_by_id, edges, _order = validate_graph(graph, node_types)
    except WorkflowError as exc:
        issues.append({"node": None, "type": None, "field": None,
                       "severity": "error", "message": str(exc)})
        return {"ok": False, "issues": issues}

    inbound = {nid: set() for nid in nodes_by_id}
    for edge in edges:
        inbound[edge["to"]].add(edge["to_port"])

    ids = set(nodes_by_id)
    for nid, node in nodes_by_id.items():
        ntype = node.get("type")
        config = node.get("config") or {}
        spec = node_types[ntype]["spec"]

        # Required config fields.
        for field, message in _REQUIRED_CONFIG.get(ntype, ()):
            value = config.get(field)
            if isinstance(value, str):
                empty = not value.strip()
            else:
                empty = value in (None, "", [], {})
            # table_filter accepts the legacy column/value shape too.
            if ntype == "transform.table_filter" and not empty:
                pass
            elif ntype == "transform.table_filter" and config.get("column"):
                continue
            # table_select accepts legacy columns string too.
            elif ntype == "transform.table_select" and (
                    config.get("picks") or config.get("columns")):
                continue
            # notebook.output: notebook OR new-name is enough.
            elif ntype == "notebook.output" and field == "notebook_id":
                continue
            if empty:
                _issue(nid, ntype, field, "error", message)

        # notebook.output needs a target one way or another.
        if ntype == "notebook.output" and not (
                (config.get("notebook_id") or "").strip()
                or (config.get("name") or "").strip()):
            _issue(nid, ntype, "notebook_id", "error",
                    "Pick a notebook or name a new one.")

        # Dangling {{ refs }} to nodes that do not exist.
        for ref in _referenced_nodes([node]).get(nid, ()):
            if ref not in ids:
                _issue(nid, ntype, None, "error",
                        f"Template refers to '{{{{ {ref} }}}}' which does "
                        "not exist — fix the reference or add the node.")

        # Unconnected inputs (no wire and no template ref covering them).
        inputs = [p["name"] for p in spec.get("inputs", [])]
        if inputs:
            refs = _referenced_nodes([node]).get(nid, set())
            wired = inbound.get(nid, set())
            if not wired and not refs:
                # merge/condition tolerate missing branches at runtime, but
                # flag it so the AI/user notices.
                _issue(nid, ntype, None, "warning",
                        f"Nothing is connected to {nid} "
                        f"({', '.join(inputs)}) — connect a node or use a "
                        "{{ nX }} reference.")

    # Trigger-only workflows cannot run manually or on schedule.
    if any(n.get("type") == "input.trigger"
           for n in nodes_by_id.values()):
        _issue(None, None, None, "warning",
                "This workflow starts from a file (Trigger input) — run it "
                "from a drive's Run menu or pass trigger_file_id, not "
                "manually.")

    errors = [i for i in issues if i["severity"] == "error"]
    return {"ok": not errors, "issues": issues}


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

    # input.param / input.drive names must be unique — they key the run form
    # answers.
    param_names = {}
    for node in graph["nodes"]:
        if node.get("type") not in ("input.param", "input.drive"):
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
    seen_targets = set()
    for edge in graph["edges"]:
        src, dst = edge.get("from"), edge.get("to")
        if src not in nodes_by_id or dst not in nodes_by_id:
            raise WorkflowError("An edge connects a node that does not exist.")
        if src == dst:
            raise WorkflowError(f"Node '{src}' connects to itself.")
        src_spec = node_types[nodes_by_id[src]["type"]]["spec"]
        dst_spec = node_types[nodes_by_id[dst]["type"]]["spec"]
        out_ports = {p["name"]: p.get("kind", "any")
                     for p in src_spec.get("outputs", [])}
        in_ports = {p["name"]: p.get("kind", "any")
                    for p in dst_spec.get("inputs", [])}
        if edge.get("from_port") not in out_ports:
            raise WorkflowError(
                f"Node '{src}' has no output port '{edge.get('from_port')}'.")
        if edge.get("to_port") not in in_ports:
            raise WorkflowError(
                f"Node '{dst}' has no input port '{edge.get('to_port')}'.")
        # Port-kind check: any accepts all, otherwise exact match.
        skind = out_ports[edge["from_port"]] or "any"
        dkind = in_ports[edge["to_port"]] or "any"
        if skind != "any" and dkind != "any" and skind != dkind:
            raise WorkflowError(
                f"Cannot connect '{src}' ({skind}) to '{dst}' ({dkind}) — "
                f"insert a conversion step (e.g. read_files, json_to_table).")
        # Fan-in guard: two edges into the same input port would silently
        # overwrite (last wins).
        target = (dst, edge["to_port"])
        if target in seen_targets:
            raise WorkflowError(
                f"Input '{edge['to_port']}' of node '{dst}' already has a "
                f"connection — one wire per input.")
        seen_targets.add(target)
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

        # Live progress: a transient "running" entry the sink replaces
        # with the final one when the node ends (see runtime.log).
        if log:
            log(nid, label, "running", "", 0)
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


def preview_outputs(outputs, limit=4000):
    """Per-node text previews of a run's outputs: {node_id: {port: text}}.

    Used by the editor preview (live "View output") and stored on each run
    so the run page can show every node's output in a popup. Payloads are
    rendered with as_text and truncated to ``limit`` chars per port."""
    previews = {}
    for nid, ports in outputs.items():
        shown = {}
        for port, payload in (ports or {}).items():
            if payload is not None:
                try:
                    # Truncate tables before rendering (a 100k-row table
                    # would OOM as_text before [:limit]).
                    if payload.get("kind") == "table":
                        data = payload.get("data") or {}
                        cols = data.get("columns", [])
                        rows = (data.get("rows", []) or [])[:50]
                        text = table_to_csv({"columns": cols, "rows": rows})
                    else:
                        text = as_text(payload)
                except WorkflowError:
                    text = ""
                shown[port] = text[:limit]
        if shown:
            previews[nid] = shown
    return previews


class _Stopped(Exception):
    """Cooperative stop requested between nodes."""
