"""AI tools contributed by the automation module (namespaced automation.*)."""

import json

from app.extensions import db
from app.services.agent_service import register_tool

from . import engine, runtime
from .models import AutomationWorkflow
from .registry import active_node_types


def _schema(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required)}}}


def _hidden_error():
    return {"error": "This workflow is hidden from the AI by the user — do "
                     "not read or mention it. Tell the user they can unhide "
                     "it with the eye button in the workflow editor "
                     "toolbar."}


def _locked_error():
    return {"error": "This workflow is LOCKED against AI edits by the "
                     "user. Do not propose the edits in chat — ask the user "
                     "to unlock it with the lock button in the workflow "
                     "editor toolbar, then repeat the request."}


def _tool_list(user, args, drive):
    # ai_hidden workflows are invisible to the assistant entirely.
    workflows = (AutomationWorkflow.query
                 .filter_by(user_id=user.id, ai_hidden=False)
                 .order_by(AutomationWorkflow.updated_at.desc()).all())
    return {"total": len(workflows),
            "workflows": [{
                "id": w.id, "name": w.name,
                "description": w.description[:200],
                "enabled": bool(w.enabled),
                "interval_minutes": w.interval_minutes,
                "ai_lock": bool(w.ai_lock),
                "last_run_at": (w.last_run_at.isoformat()
                                if w.last_run_at else None),
                "last_run_status": (w.runs[0].status if w.runs else None),
            } for w in workflows[:100]]}


def _tool_run(user, args, drive):
    try:
        workflow_id = int(args.get("workflow_id"))
    except (TypeError, ValueError):
        return {"error": "workflow_id must be an integer."}
    workflow = (AutomationWorkflow.query
                .filter_by(id=workflow_id, user_id=user.id).first())
    if workflow is None:
        return {"error": f"Workflow {workflow_id} not found."}
    if workflow.ai_hidden:
        return _hidden_error()
    if runtime.has_active_run(workflow.id):
        return {"error": f"Workflow '{workflow.name}' already has a run in "
                         "progress — wait for it to finish."}
    run = runtime.start_run(workflow, user, trigger="ai")
    return {"run_id": run.id, "status": run.status,
            "note": f"Workflow '{workflow.name}' started in the background; "
                    "its per-node log and outputs are on the workflow's "
                    "runs page."}


def _tool_node_catalog(user, args, drive):
    catalog = []
    for type_name, entry in sorted(active_node_types().items()):
        spec = entry["spec"]
        catalog.append({
            "type": type_name,
            "label": spec.get("label", type_name),
            "category": spec.get("category", ""),
            "description": spec.get("description", ""),
            "inputs": [p["name"] for p in spec.get("inputs", [])],
            "outputs": [p["name"] for p in spec.get("outputs", [])],
            "config_fields": {f["name"]: (f.get("kind") or "text")
                              for f in spec.get("fields", [])},
            "config_help": {f["name"]: f.get("help", "")
                            for f in spec.get("fields", [])
                            if f.get("help")},
        })
    return {"node_types": catalog,
            "graph_format": {
                "nodes": [{"id": "n1 (unique string)", "type": "node type",
                           "x": 0, "y": 0, "config": {"field": "value"}}],
                "edges": [{"from": "n1", "from_port": "out",
                           "to": "n2", "to_port": "in"}]}}


def _tool_create(user, args, drive):
    name = (args.get("name") or "").strip()[:160]
    if not name:
        return {"error": "name is required."}
    raw = args.get("graph")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {"error": "graph must be valid JSON "
                             "{nodes: [...], edges: [...]}."}
    try:
        graph = engine.parse_graph(json.dumps(raw or {}))
        if not graph["nodes"]:
            raise engine.WorkflowError("The graph has no nodes.")
        engine.validate_graph(graph, active_node_types())
    except engine.WorkflowError as exc:
        return {"error": f"Invalid graph: {exc}. Call "
                         "automation.get_node_catalog to see the available "
                         "node types, ports and config fields."}
    workflow = AutomationWorkflow(
        user_id=user.id, name=name,
        description=(args.get("description") or "").strip(),
        graph=json.dumps(graph))
    db.session.add(workflow)
    db.session.commit()
    return {"ok": True, "workflow_id": workflow.id, "name": workflow.name,
            "url": f"/m/automation/{workflow.id}",
            "note": "Workflow created. Tell the user it is ready to review "
                    "and adjust in the Automation editor (open the URL)."}


def _tool_get_workflow(user, args, drive):
    try:
        workflow_id = int(args.get("workflow_id"))
    except (TypeError, ValueError):
        return {"error": "workflow_id must be an integer."}
    workflow = (AutomationWorkflow.query
                .filter_by(id=workflow_id, user_id=user.id).first())
    if workflow is None:
        return {"error": f"Workflow {workflow_id} not found."}
    if workflow.ai_hidden:
        return _hidden_error()
    locked = bool(workflow.ai_lock)
    try:
        graph = engine.parse_graph(workflow.graph)
    except engine.WorkflowError:
        graph = {"nodes": [], "edges": []}
    # Inline the specs of the types in use so the model can edit configs
    # without a separate catalog call.
    used = {n["type"] for n in graph["nodes"]}
    specs = {}
    for type_name, entry in active_node_types().items():
        if type_name not in used:
            continue
        spec = entry["spec"]
        specs[type_name] = {
            "inputs": [p["name"] for p in spec.get("inputs", [])],
            "outputs": [p["name"] for p in spec.get("outputs", [])],
            "config_fields": {f["name"]: (f.get("kind") or "text")
                              for f in spec.get("fields", [])},
        }
    result = {"workflow_id": workflow.id, "name": workflow.name,
              "description": workflow.description, "graph": graph,
              "ai_lock": locked, "specs": specs}
    if locked:
        result["note"] = ("This workflow is LOCKED against AI edits by the "
                          "user — do not edit it; ask the user to unlock it "
                          "with the lock button in the workflow editor "
                          "toolbar first.")
    return result


def _tool_edit_workflow(user, args, drive):
    try:
        workflow_id = int(args.get("workflow_id"))
    except (TypeError, ValueError):
        return {"error": "workflow_id must be an integer."}
    workflow = (AutomationWorkflow.query
                .filter_by(id=workflow_id, user_id=user.id).first())
    if workflow is None:
        return {"error": f"Workflow {workflow_id} not found."}
    if workflow.ai_hidden:
        return _hidden_error()
    if workflow.ai_lock:
        return _locked_error()
    operations = args.get("operations")
    if not isinstance(operations, list) or not operations:
        return {"error": "operations must be a non-empty list."}

    try:
        graph = engine.parse_graph(workflow.graph)
    except engine.WorkflowError:
        graph = {"nodes": [], "edges": []}
    nodes_by_id = {n["id"]: n for n in graph["nodes"]}

    def edge_key(e):
        return (e["from"], e["from_port"], e["to"], e["to_port"])

    applied = []
    for i, op in enumerate(operations):
        if not isinstance(op, dict):
            return {"error": f"operation #{i + 1} is not an object."}
        action = op.get("op")
        if action == "add_node":
            ntype = op.get("type")
            nid = (op.get("id") or "").strip()
            if not nid:
                seq = 1
                while f"n{seq}" in nodes_by_id:
                    seq += 1
                nid = f"n{seq}"
            if nid in nodes_by_id:
                return {"error": f"operation #{i + 1}: node id '{nid}' "
                                 "already exists."}
            node = {"id": nid, "type": ntype,
                    "x": int(op.get("x") or 80), "y": int(op.get("y") or 80),
                    "config": op.get("config") or {}}
            graph["nodes"].append(node)
            nodes_by_id[nid] = node
            applied.append(f"added {nid} ({ntype})")
        elif action == "update_node":
            node = nodes_by_id.get(op.get("id"))
            if node is None:
                return {"error": f"operation #{i + 1}: node "
                                 f"'{op.get('id')}' not found."}
            if isinstance(op.get("config"), dict):
                node.setdefault("config", {}).update(op["config"])
            if op.get("x") is not None:
                node["x"] = int(op["x"])
            if op.get("y") is not None:
                node["y"] = int(op["y"])
            applied.append(f"updated {node['id']}")
        elif action == "remove_node":
            nid = op.get("id")
            if nid not in nodes_by_id:
                return {"error": f"operation #{i + 1}: node '{nid}' not "
                                 "found."}
            graph["nodes"] = [n for n in graph["nodes"] if n["id"] != nid]
            del nodes_by_id[nid]
            graph["edges"] = [e for e in graph["edges"]
                              if e["from"] != nid and e["to"] != nid]
            applied.append(f"removed {nid}")
        elif action == "connect":
            edge = {"from": op.get("from"), "from_port":
                    op.get("from_port") or "out",
                    "to": op.get("to"), "to_port": op.get("to_port") or "in"}
            if edge_key(edge) in [edge_key(e) for e in graph["edges"]]:
                applied.append(f"already connected {edge['from']} → "
                               f"{edge['to']}")
                continue
            graph["edges"].append(edge)
            applied.append(f"connected {edge['from']}.{edge['from_port']} → "
                           f"{edge['to']}.{edge['to_port']}")
        elif action == "disconnect":
            target = (op.get("from"), op.get("from_port") or "out",
                      op.get("to"), op.get("to_port") or "in")
            before = len(graph["edges"])
            graph["edges"] = [e for e in graph["edges"]
                              if edge_key(e) != target]
            applied.append(f"disconnected {target[0]} → {target[2]}"
                           if len(graph["edges"]) < before
                           else f"no such edge {target[0]} → {target[2]}")
        else:
            return {"error": f"operation #{i + 1}: unknown op "
                             f"'{action}' (add_node, update_node, "
                             "remove_node, connect, disconnect)."}

    try:
        engine.validate_graph(graph, active_node_types())
    except engine.WorkflowError as exc:
        return {"error": f"The edits would break the workflow: {exc}. "
                         "Nothing was saved."}
    workflow.graph = json.dumps(graph)
    db.session.commit()
    return {"ok": True, "workflow_id": workflow.id, "applied": applied,
            "note": "Workflow updated and saved. The user's editor canvas "
                    "refreshes automatically — summarize what changed.",
            "ui": {"type": "workflow_changed",
                   "workflow_id": workflow.id}}


_REGISTERED = False


def register_tools():
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    register_tool(
        "list_workflows",
        _schema("list_workflows",
                "List the user's Automation workflows (visual node flows "
                "that read/transform files and write new ones), with "
                "schedule, last run status and the ai_lock flag (a locked "
                "workflow refuses edit_workflow until the user unlocks it "
                "in the editor toolbar). Workflows the user hid from the "
                "AI are not listed at all.",
                {}),
        _tool_list, label="Listing workflows", module="automation")
    register_tool(
        "run_workflow",
        _schema("run_workflow",
                "Start a background run of one of the user's Automation "
                "workflows (use list_workflows first to find the id).",
                {"workflow_id": {"type": "integer",
                                 "description": "The workflow id"}},
                required=("workflow_id",)),
        _tool_run, label="Starting workflow", module="automation")
    register_tool(
        "get_node_catalog",
        _schema("get_node_catalog",
                "List the Automation node types available for building "
                "workflows, with their input/output ports and config "
                "fields, plus the graph JSON format. Call this BEFORE "
                "create_workflow.",
                {}),
        _tool_node_catalog, label="Reading node catalog",
        module="automation")
    register_tool(
        "create_workflow",
        _schema("create_workflow",
                "Create an Automation workflow for the user. Build the "
                "graph with get_node_catalog first. Nodes have unique "
                "string ids ('n1', 'n2', …); edges connect "
                "from/from_port → to/to_port using the port names from "
                "the catalog. The workflow is created disabled — the user "
                "reviews and runs it from the editor.",
                {"name": {"type": "string",
                          "description": "Short workflow name"},
                 "description": {"type": "string",
                                 "description": "What the workflow does"},
                 "graph": {"type": "object",
                           "description": "{nodes: [{id, type, x, y, "
                                          "config}], edges: [{from, "
                                          "from_port, to, to_port}]}"}},
                required=("name", "graph")),
        _tool_create, label="Creating workflow", module="automation")
    register_tool(
        "get_workflow",
        _schema("get_workflow",
                "Read one Automation workflow: its full graph (nodes with "
                "ids, types, positions and configs, plus edges) and the "
                "port/field specs of the node types in use. Call this "
                "BEFORE edit_workflow.",
                {"workflow_id": {"type": "integer",
                                 "description": "The workflow id"}},
                required=("workflow_id",)),
        _tool_get_workflow, label="Reading workflow", module="automation")
    register_tool(
        "edit_workflow",
        _schema("edit_workflow",
                "Edit an Automation workflow the user is building: apply a "
                "list of operations (add_node {id?, type, x?, y?, "
                "config?}, update_node {id, config?, x?, y?} — config is "
                "merged, remove_node {id}, connect {from, from_port?, to, "
                "to_port?}, disconnect {from, from_port?, to, to_port?}). "
                "All operations are validated together; nothing is saved "
                "if any is invalid. Read the graph first with "
                "get_workflow.",
                {"workflow_id": {"type": "integer",
                                 "description": "The workflow id"},
                 "operations": {"type": "array",
                                "description": "Operations to apply in "
                                               "order",
                                "items": {"type": "object"}}},
                required=("workflow_id", "operations")),
        _tool_edit_workflow, label="Editing workflow", module="automation")
