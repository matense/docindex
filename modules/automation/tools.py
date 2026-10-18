"""AI tools contributed by the automation module (namespaced automation.*)."""

import json

from app.extensions import db
from app.services.agent_service import register_tool

from . import engine, runtime
from .models import AutomationFolder, AutomationWorkflow
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
    # Optional: query filters by name/description, folder_id scopes to one
    # folder (-1 = unsorted root).
    query = (AutomationWorkflow.query
             .filter_by(user_id=user.id, ai_hidden=False))
    q = (args.get("query") or "").strip() if isinstance(args, dict) else ""
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(
            AutomationWorkflow.name.ilike(like),
            AutomationWorkflow.description.ilike(like)))
    folder_id = (args.get("folder_id") if isinstance(args, dict) else None)
    if folder_id == -1:
        query = query.filter(AutomationWorkflow.folder_id.is_(None))
    elif folder_id not in (None, ""):
        try:
            query = query.filter_by(folder_id=int(folder_id))
        except (TypeError, ValueError):
            return {"error": "folder_id must be an integer (-1 = unsorted)."}
    workflows = query.order_by(
        AutomationWorkflow.updated_at.desc()).all()
    folders = {f.id: f.name for f in AutomationFolder.query
               .filter_by(user_id=user.id).all()}
    return {"total": len(workflows),
            "workflows": [{
                "id": w.id, "name": w.name,
                "description": w.description[:200],
                "folder_id": w.folder_id,
                "folder": folders.get(w.folder_id),
                "enabled": bool(w.enabled),
                "interval_minutes": w.interval_minutes,
                "ai_lock": bool(w.ai_lock),
                "last_run_at": (w.last_run_at.isoformat()
                                if w.last_run_at else None),
                "last_run_status": (w.runs[0].status if w.runs else None),
            } for w in workflows[:100]],
            "folders": [{"id": i, "name": n}
                        for i, n in sorted(folders.items())]}
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
    # Optional run parameters (input.param) and trigger file (input.trigger):
    # without these the AI could never run parameterized workflows.
    params = args.get("params")
    if params is not None and not isinstance(params, dict):
        return {"error": "params must be an object {name: value}."}
    trigger_file_id = args.get("trigger_file_id")
    if trigger_file_id not in (None, ""):
        try:
            trigger_file_id = int(trigger_file_id)
        except (TypeError, ValueError):
            return {"error": "trigger_file_id must be an integer."}
        from app.models import StoredFile
        stored = db.session.get(StoredFile, trigger_file_id)
        if (stored is None or stored.user_id != user.id
                or stored.deleted_at is not None):
            return {"error": "The trigger file does not exist (or is in the trash)."}
    else:
        trigger_file_id = None
    # Validate required params up front (same rules as the manual run form).
    from . import nodes as _nodes
    try:
        graph = engine.parse_graph(workflow.graph)
    except engine.WorkflowError as exc:
        return {"error": f"The workflow is broken: {exc}."}
    missing = [s["label"] for s in _nodes.run_param_specs(graph)
               if s["required"] and (not params or s["param"] not in params)]
    if missing:
        return {"error": "Missing run parameters: " + ", ".join(missing) +
                         ". Pass them in params."}
    run = runtime.start_run(workflow, user, trigger="ai",
                            trigger_file_id=trigger_file_id,
                            params=params or None)
    return {"run_id": run.id, "status": run.status,
            "url": f"/m/automation/runs/{run.id}",
            "note": f"Workflow '{workflow.name}' started in the background; "
                    "use automation.get_run_status to poll it."}


def _tool_node_catalog(user, args, drive):
    catalog = []
    for type_name, entry in sorted(active_node_types().items()):
        spec = entry["spec"]
        catalog.append({
            "type": type_name,
            "label": spec.get("label", type_name),
            "category": spec.get("category", ""),
            "description": spec.get("description", ""),
            "example": spec.get("description", "") and spec.get("example", ""),
            "inputs": [{"name": p["name"], "kind": p.get("kind", "any")}
                       for p in spec.get("inputs", [])],
            "outputs": [{"name": p["name"], "kind": p.get("kind", "any")}
                        for p in spec.get("outputs", [])],
            "config_fields": [
                {"name": f["name"], "kind": f.get("kind") or "text",
                 "label": f.get("label", f["name"]),
                 "default": f.get("default", ""),
                 "required": bool(f.get("required")),
                 "advanced": bool(f.get("advanced")),
                 "options": f.get("options", []),
                 "placeholder": f.get("placeholder", ""),
                 "help": f.get("help", "")}
                for f in spec.get("fields", [])
            ],
        })
    return {"node_types": catalog,
            "graph_format": {
                "nodes": [{"id": "n1 (unique string)", "type": "node type",
                           "x": 0, "y": 0, "config": {"field": "value"}}],
                "edges": [{"from": "n1", "from_port": "out",
                           "to": "n2", "to_port": "in"}]},
            "hints": [
                "Match port kinds: files->files/text via read_files, "
                "table->table only, json->json/table via json_to_table.",
                "merge needs in1/in2, condition outputs true/false.",
                "save nodes need a writable drive_id (not synced).",
            ]}


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
            "inputs": [{"name": p["name"], "kind": p.get("kind", "any")}
                       for p in spec.get("inputs", [])],
            "outputs": [{"name": p["name"], "kind": p.get("kind", "any")}
                        for p in spec.get("outputs", [])],
            "config_fields": [
                {"name": f["name"], "kind": f.get("kind") or "text",
                 "label": f.get("label", f["name"]),
                 "default": f.get("default", ""),
                 "required": bool(f.get("required")),
                 "advanced": bool(f.get("advanced")),
                 "options": f.get("options", [])}
                for f in spec.get("fields", [])
            ],
        }
    # Last run for diagnose-fix loop.
    last_run = None
    if workflow.runs:
        r = workflow.runs[0]
        try:
            log = json.loads(r.log) if r.log else []
        except ValueError:
            log = []
        last_run = {"id": r.id, "status": r.status, "error": r.error,
                    "log": log[-20:]}
    result = {"workflow_id": workflow.id, "name": workflow.name,
              "description": workflow.description, "graph": graph,
              "folder_id": workflow.folder_id,
              "folder": (workflow.folder.name
                         if workflow.folder is not None else None),
              "enabled": bool(workflow.enabled),
              "interval_minutes": workflow.interval_minutes,
              "ai_lock": locked, "specs": specs, "last_run": last_run}
    # Full static vision: every problem at once, no run needed.
    result["diagnosis"] = engine.diagnose_graph(graph, active_node_types())
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
        detail = engine.diagnose_graph(graph, active_node_types())
        return {"error": f"The edits would break the workflow: {exc}. "
                         "Nothing was saved.",
                "failed_after_op": len(applied),
                "applied_so_far": applied,
                "issues": detail["issues"]}
    workflow.graph = json.dumps(graph)
    db.session.commit()
    return {"ok": True, "workflow_id": workflow.id, "applied": applied,
            "note": "Workflow updated and saved. The user's editor canvas "
                    "refreshes automatically — summarize what changed.",
            "ui": {"type": "workflow_changed",
                   "workflow_id": workflow.id}}


def _tool_get_run(user, args, drive):
    from .models import AutomationRun
    try:
        run_id = int(args.get("run_id"))
    except (TypeError, ValueError):
        return {"error": "run_id must be an integer."}
    run = (AutomationRun.query
           .filter_by(id=run_id, user_id=user.id).first())
    if run is None:
        return {"error": f"Run {run_id} not found."}
    if run.workflow is not None and run.workflow.ai_hidden:
        return _hidden_error()
    try:
        log = json.loads(run.log) if run.log else []
    except ValueError:
        log = []
    try:
        output = json.loads(run.output) if run.output else None
    except ValueError:
        output = None
    result = {"run_id": run.id, "workflow_id": run.workflow_id,
              "status": run.status, "error": run.error,
              "log": log[-30:], "output": output,
              "url": f"/m/automation/runs/{run.id}"}
    if run.status == "error":
        result["fix_hint"] = _fix_hint(run.error, log)
        # Static re-check of the current graph: problems the AI can fix
        # without running again.
        if run.workflow is not None:
            try:
                graph = engine.parse_graph(run.workflow.graph)
                result["diagnosis"] = engine.diagnose_graph(
                    graph, active_node_types())
            except engine.WorkflowError as exc:
                result["diagnosis"] = {
                    "ok": False,
                    "issues": [{"node": None, "type": None, "field": None,
                                "severity": "error", "message": str(exc)}]}
    return result


def _fix_hint(error, log):
    """Plain-language fix suggestion for a run failure (AI + user)."""
    err = (error or "").lower()
    if "pick a file" in err or "no input" in err:
        return ("A node has no input: connect an input.file/search node to "
                "it, or pick a static file in its settings.")
    if "column" in err and "does not exist" in err:
        return ("A column name is wrong: open the node's settings and pick "
                "the column from the dropdown (it lists the real columns).")
    if "ai is not configured" in err:
        return ("No AI connection: add one in AI Settings, then retry.")
    if "drive" in err and ("writable" in err or "read-only" in err):
        return ("The target drive is read-only (synced): pick a writable "
                "drive in the save node's settings.")
    if "missing run parameters" in err:
        return ("The workflow asks questions at run time: pass them in "
                "params {name: value}.")
    if "trigger" in err:
        return ("This workflow starts from a file: pass trigger_file_id or "
                "run it from the drive's Run menu.")
    failed = [e for e in (log or []) if e.get("status") == "error"]
    if failed:
        return (f"Step '{failed[-1].get('label')}' failed: "
                f"{failed[-1].get('summary')}. Read it with get_workflow, "
                "fix its settings with edit_workflow, then re-run.")
    return ("Read the workflow with get_workflow (it includes last_run), "
            "fix the failing step with edit_workflow, then run again.")


def _tool_folders(user, args, drive):
    folders = (AutomationFolder.query
               .filter_by(user_id=user.id)
               .order_by(AutomationFolder.name).all())
    counts = dict(
        db.session.query(AutomationWorkflow.folder_id,
                         db.func.count(AutomationWorkflow.id))
        .filter(AutomationWorkflow.user_id == user.id)
        .group_by(AutomationWorkflow.folder_id).all())
    items = []
    for f in folders:
        parts, cur = [f.name], f.parent
        while cur is not None:
            parts.append(cur.name)
            cur = cur.parent
        items.append({"id": f.id, "name": f.name,
                      "path": "/".join(reversed(parts)),
                      "parent_id": f.parent_id,
                      "workflows": counts.get(f.id, 0)})
    items.sort(key=lambda x: x["path"].lower())
    return {"folders": items,
            "unsorted": counts.get(None, 0)}


def _tool_create_folder(user, args, drive):
    name = ((args.get("name") or "") if isinstance(args, dict) else ""
            ).strip()[:120]
    if not name:
        return {"error": "name is required."}
    parent_id = (args.get("parent_id") if isinstance(args, dict) else None)
    parent = None
    if parent_id not in (None, ""):
        try:
            parent = AutomationFolder.query.filter_by(
                id=int(parent_id), user_id=user.id).first()
        except (TypeError, ValueError):
            parent = None
        if parent is None:
            return {"error": f"Parent folder {parent_id} not found."}
    folder = AutomationFolder(user_id=user.id, name=name,
                              parent_id=parent.id if parent else None)
    # Guard nesting depth.
    depth, cur = 0, parent
    while cur is not None and depth < 10:
        cur = cur.parent
        depth += 1
    if depth >= 10:
        return {"error": "Folders cannot nest deeper than 10 levels."}
    db.session.add(folder)
    db.session.commit()
    return {"ok": True, "folder_id": folder.id, "name": folder.name}


def _tool_move_workflow(user, args, drive):
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
    folder_id = (args.get("folder_id") if isinstance(args, dict) else None)
    if folder_id in (None, "", 0, -1):
        workflow.folder_id = None
    else:
        try:
            folder = AutomationFolder.query.filter_by(
                id=int(folder_id), user_id=user.id).first()
        except (TypeError, ValueError):
            folder = None
        if folder is None:
            return {"error": f"Folder {folder_id} not found."}
        workflow.folder_id = folder.id
    db.session.commit()
    if workflow.ai_lock:
        return {"ok": True, "workflow_id": workflow.id,
                "folder_id": workflow.folder_id,
                "note": "Moved. (Locked workflows still refuse content "
                        "edits, but organizing into folders is allowed.)"}
    return {"ok": True, "workflow_id": workflow.id,
            "folder_id": workflow.folder_id}


def _tool_validate(user, args, drive):
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
    try:
        graph = engine.parse_graph(workflow.graph)
    except engine.WorkflowError as exc:
        return {"ok": False, "workflow_id": workflow.id,
                "issues": [{"node": None, "type": None, "field": None,
                            "severity": "error", "message": str(exc)}],
                "note": "Fix with edit_workflow, then validate again."}
    result = engine.diagnose_graph(graph, active_node_types())
    result["workflow_id"] = workflow.id
    # Ownership-aware checks (need the DB): referenced drives, files and
    # notebooks really exist, belong to the user, and save targets are
    # writable. Pure-graph diagnose cannot see these.
    try:
        result["issues"].extend(_reference_issues(user, graph))
        result["ok"] = not any(i["severity"] == "error"
                               for i in result["issues"])
    except Exception:  # noqa: BLE001 - reference checks are best-effort
        pass
    result["note"] = ("All clear — safe to run." if result["ok"]
                      else "Fix the error issues with edit_workflow "
                           "(field names match the editor), then validate "
                           "again — no run needed.")
    return result


def _reference_issues(user, graph):
    """Issues about referenced drives/files/notebooks (existence + access)."""
    from app.models import Drive, Folder, StoredFile
    issues = []
    drives = {d.id: d for d in
              Drive.query.filter_by(user_id=user.id).all()}
    for node in graph.get("nodes", []):
        nid, ntype = node.get("id"), node.get("type")
        config = node.get("config") or {}
        for field in ("drive_id",):
            raw = (config.get(field) or "").strip() \
                if isinstance(config.get(field), str) \
                else config.get(field)
            if raw in (None, ""):
                continue
            try:
                did = int(raw)
            except (TypeError, ValueError):
                issues.append({"node": nid, "type": ntype, "field": field,
                               "severity": "error",
                               "message": "The drive selection is invalid — "
                                          "pick a drive again."})
                continue
            drive = drives.get(did)
            if drive is None:
                issues.append({"node": nid, "type": ntype, "field": field,
                               "severity": "error",
                               "message": "The selected drive no longer "
                                          "exists — pick another."})
            elif drive.is_synced and ntype in (
                    "output.save_file", "output.save_table",
                    "output.copy_file", "notebook.output"):
                issues.append({"node": nid, "type": ntype, "field": field,
                               "severity": "error",
                               "message": f"Drive '{drive.name}' is read-only "
                                          "(synced) — pick a writable drive."})
        for field in ("file_id",):
            raw = config.get(field)
            if raw in (None, ""):
                continue
            try:
                fid = int(raw)
            except (TypeError, ValueError):
                issues.append({"node": nid, "type": ntype, "field": field,
                               "severity": "error",
                               "message": "The file selection is invalid — "
                                          "pick the file again."})
                continue
            stored = db.session.get(StoredFile, fid)
            if (stored is None or stored.user_id != user.id
                    or stored.deleted_at is not None):
                issues.append({"node": nid, "type": ntype, "field": field,
                               "severity": "error",
                               "message": "The selected file no longer "
                                          "exists (or is in the trash) — "
                                          "pick another."})
        if ntype == "input.folder_list":
            raw = config.get("folder_id")
            if raw not in (None, ""):
                try:
                    fid = int(raw)
                except (TypeError, ValueError):
                    issues.append({"node": nid, "type": ntype,
                                   "field": "folder_id",
                                   "severity": "error",
                                   "message": "The folder selection is "
                                              "invalid — pick it again."})
                    continue
                folder = db.session.get(Folder, fid)
                if folder is None or folder.user_id != user.id:
                    issues.append({"node": nid, "type": ntype,
                                   "field": "folder_id",
                                   "severity": "error",
                                   "message": "The selected folder no longer "
                                              "exists — pick another."})
        if ntype in ("notebook.input", "notebook.output"):
            cell_id = str(config.get("cell_id") or "").strip()
            nb_raw = config.get("notebook_id")
            if cell_id and nb_raw not in (None, ""):
                try:
                    import importlib as _il
                    doc = _il.import_module(
                        "docindex_module_notebooks.doc")
                    try:
                        nb_id = int(nb_raw)
                    except (TypeError, ValueError):
                        nb_id = None
                    stored = (doc.get_notebook(nb_id, user.id)
                              if nb_id else None)
                    if stored is None:
                        issues.append({"node": nid, "type": ntype,
                                       "field": "notebook_id",
                                       "severity": "error",
                                       "message": "The selected notebook no "
                                                  "longer exists — pick "
                                                  "another."})
                    elif doc.find_cell(
                            doc.load(stored), cell_id)[0] is None:
                        issues.append({"node": nid, "type": ntype,
                                       "field": "cell_id",
                                       "severity": "error",
                                       "message": "That cell no longer "
                                                  "exists in the notebook — "
                                                  "pick another cell."})
                except engine.WorkflowError as exc:
                    issues.append({"node": nid, "type": ntype,
                                   "field": "cell_id",
                                   "severity": "error", "message": str(exc)})
                except Exception:  # noqa: BLE001 - best-effort check
                    pass
    return issues


def _tool_stop_run(user, args, drive):
    try:
        run_id = int(args.get("run_id"))
    except (TypeError, ValueError):
        return {"error": "run_id must be an integer."}
    run = runtime.stop_run(user, run_id)
    if run is None:
        return {"error": f"Run {run_id} not found."}
    return {"ok": True, "run_id": run.id, "status": run.status}


def _tool_set_schedule(user, args, drive):
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
    enabled = args.get("enabled")
    if not isinstance(enabled, bool):
        return {"error": "enabled must be a boolean."}
    interval = args.get("interval_minutes")
    if enabled:
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            return {"error": "interval_minutes must be a positive integer."}
        if interval < 1:
            return {"error": "interval_minutes must be >= 1."}
        workflow.interval_minutes = interval
    else:
        workflow.interval_minutes = None
    workflow.enabled = enabled
    db.session.commit()
    return {"ok": True, "workflow_id": workflow.id,
            "enabled": bool(workflow.enabled),
            "interval_minutes": workflow.interval_minutes}


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
                "folder, schedule, last run status and the ai_lock flag (a "
                "locked workflow refuses edit_workflow until the user "
                "unlocks it in the editor toolbar). Workflows the user hid "
                "from the AI are not listed at all. Optional query searches "
                "names/descriptions; folder_id scopes to one folder "
                "(-1 = unsorted root).",
                {"query": {"type": "string",
                           "description": "Search names/descriptions"},
                 "folder_id": {"type": "integer",
                               "description": "Only this folder (-1 = root)"}}),
        _tool_list, label="Listing workflows", module="automation")
    register_tool(
        "run_workflow",
        _schema("run_workflow",
                "Start a background run of one of the user's Automation "
                "workflows (use list_workflows first to find the id). "
                "Call validate_workflow first — it lists problems without "
                "running. "
                "Pass params for input.param nodes and trigger_file_id for "
                "input.trigger workflows. Poll get_run_status afterwards.",
                {"workflow_id": {"type": "integer",
                                 "description": "The workflow id"},
                 "params": {"type": "object",
                            "description": "{param_name: value} for Run inputs"},
                 "trigger_file_id": {"type": "integer",
                                     "description": "File the run starts from"}},
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
    register_tool(
        "get_run_status",
        _schema("get_run_status",
                "Poll an Automation run: status (queued/running/done/error/"
                "stopped), per-node log tail and outputs. Call after "
                "run_workflow until status is done/error.",
                {"run_id": {"type": "integer",
                            "description": "The run id"}},
                required=("run_id",)),
        _tool_get_run, label="Checking run", module="automation")
    register_tool(
        "stop_run",
        _schema("stop_run",
                "Stop a running Automation run.",
                {"run_id": {"type": "integer",
                            "description": "The run id"}},
                required=("run_id",)),
        _tool_stop_run, label="Stopping run", module="automation")
    register_tool(
        "validate_workflow",
        _schema("validate_workflow",
                "Check an Automation workflow WITHOUT running it: returns "
                "every problem at once (node, field, severity "
                "error/warning, message). Fix error issues with "
                "edit_workflow (config field names match), then validate "
                "again. Call this BEFORE run_workflow and after every edit.",
                {"workflow_id": {"type": "integer",
                                 "description": "The workflow id"}},
                required=("workflow_id",)),
        _tool_validate, label="Validating workflow", module="automation")
    register_tool(
        "list_folders",
        _schema("list_folders",
                "List the user's automation folders (organizer tree) with "
                "workflow counts, plus how many workflows are unsorted.",
                {}),
        _tool_folders, label="Listing folders", module="automation")
    register_tool(
        "create_folder",
        _schema("create_folder",
                "Create an automation folder to organize workflows "
                "(optionally nested with parent_id).",
                {"name": {"type": "string",
                          "description": "Folder name"},
                 "parent_id": {"type": "integer",
                               "description": "Parent folder (optional)"}},
                required=("name",)),
        _tool_create_folder, label="Creating folder", module="automation")
    register_tool(
        "move_workflow",
        _schema("move_workflow",
                "Move a workflow into a folder (organize the user's "
                "automations). Use folder_id null/0/-1 for the unsorted "
                "root. Works even on AI-locked workflows.",
                {"workflow_id": {"type": "integer",
                                 "description": "The workflow id"},
                 "folder_id": {"type": "integer",
                               "description": "Target folder (null = root)"}},
                required=("workflow_id",)),
        _tool_move_workflow, label="Moving workflow", module="automation")
    register_tool(
        "set_schedule",
        _schema("set_schedule",
                "Enable/disable a workflow's automatic schedule. "
                "interval_minutes is required when enabled=true.",
                {"workflow_id": {"type": "integer",
                                 "description": "The workflow id"},
                 "enabled": {"type": "boolean",
                             "description": "Run automatically?"},
                 "interval_minutes": {"type": "integer",
                                       "description": "Every N minutes"}},
                required=("workflow_id", "enabled")),
        _tool_set_schedule, label="Setting schedule", module="automation")
