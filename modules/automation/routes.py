"""Automation module routes: workflow list, editor, runs."""

import json

from flask import (Blueprint, abort, current_app, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Drive, Folder, StoredFile

from . import engine, examples, nodes, runtime
from .models import AutomationRun, AutomationWorkflow
from .registry import active_node_types, palette

bp = Blueprint("automation", __name__,
               template_folder="templates", static_folder="static")


def _owned_workflow(workflow_id):
    workflow = db.session.get(AutomationWorkflow, workflow_id)
    if workflow is None or workflow.user_id != current_user.id:
        abort(404)
    return workflow


def _owned_run(run_id):
    run = db.session.get(AutomationRun, run_id)
    if run is None or run.user_id != current_user.id:
        abort(404)
    return run


def _writable_drives():
    return [{"id": d.id, "name": d.name, "is_synced": bool(d.is_synced)}
            for d in (Drive.query
                      .filter_by(user_id=current_user.id)
                      .order_by(Drive.name).all())]


@bp.get("/")
def index():
    workflows = (AutomationWorkflow.query
                 .filter_by(user_id=current_user.id)
                 .order_by(AutomationWorkflow.updated_at.desc()).all())
    recent_runs = (AutomationRun.query
                   .filter_by(user_id=current_user.id)
                   .order_by(AutomationRun.id.desc()).limit(10).all())
    return render_template("automation/list.html", workflows=workflows,
                           examples=examples.EXAMPLES,
                           recent_runs=recent_runs)


@bp.post("/examples/<key>/use")
def example_use(key):
    """Instantiate a curated example as a new editable workflow."""
    example = examples.by_key(key)
    if example is None:
        abort(404)
    try:
        graph = engine.parse_graph(json.dumps(example["graph"]))
        engine.validate_graph(graph, active_node_types())
    except engine.WorkflowError as exc:
        # E.g. the example needs a module that is disabled (notebooks).
        return render_template("automation/list.html",
                               workflows=(AutomationWorkflow.query
                                          .filter_by(user_id=current_user.id)
                                          .order_by(AutomationWorkflow
                                                    .updated_at.desc()).all()),
                               examples=examples.EXAMPLES,
                               recent_runs=(AutomationRun.query
                                            .filter_by(user_id=current_user.id)
                                            .order_by(AutomationRun.id.desc())
                                            .limit(10).all()),
                               example_error=f"Cannot use "
                                             f"'{example['name']}': {exc}"), 400
    workflow = AutomationWorkflow(
        user_id=current_user.id, name=example["name"][:160],
        description=example["description"], graph=json.dumps(graph))
    db.session.add(workflow)
    db.session.commit()
    return redirect(url_for("automation.editor", workflow_id=workflow.id))


@bp.post("/create")
def create():
    name = (request.form.get("name") or "").strip()[:160]
    if not name:
        abort(400, "The workflow needs a name.")
    workflow = AutomationWorkflow(user_id=current_user.id, name=name,
                                  description=(request.form.get("description")
                                               or "").strip())
    db.session.add(workflow)
    db.session.commit()
    return redirect(url_for("automation.editor", workflow_id=workflow.id))


@bp.get("/<int:workflow_id>")
def editor(workflow_id):
    workflow = _owned_workflow(workflow_id)
    return render_template("automation/editor.html", workflow=workflow,
                           palette=palette(), drives=_writable_drives(),
                           notebooks=_notebooks())


def _notebooks():
    """User's notebooks for the notebook node pickers ([] when the module
    is disabled)."""
    from app.services import module_service
    if not module_service.is_enabled("notebooks"):
        return []
    import importlib
    try:
        doc = importlib.import_module("docindex_module_notebooks.doc")
    except ImportError:
        return []
    return [{"id": n.id, "name": n.name}
            for n in doc.list_notebooks(current_user.id)]


@bp.get("/<int:workflow_id>/graph")
def graph_json(workflow_id):
    """Current saved graph — used by the editor to reload after the AI
    copilot edits the workflow from the chat."""
    workflow = _owned_workflow(workflow_id)
    return jsonify({"graph": engine.parse_graph(workflow.graph)})


@bp.post("/<int:workflow_id>/save")
def save(workflow_id):
    workflow = _owned_workflow(workflow_id)
    data = request.get_json(silent=True) or {}
    raw_graph = data.get("graph")
    try:
        graph = engine.parse_graph(raw_graph if isinstance(raw_graph, str)
                                   else json.dumps(raw_graph or {}))
        engine.validate_graph(graph, active_node_types())
    except engine.WorkflowError as exc:
        return jsonify({"error": str(exc)}), 400
    workflow.graph = json.dumps(graph)
    workflow.name = (data.get("name") or workflow.name or
                     "Untitled workflow")[:160]
    workflow.description = (data.get("description")
                            if data.get("description") is not None
                            else workflow.description)
    enabled = data.get("enabled")
    # Checkbox/JSON: "false"/"0"/0/False must not become True via bool().
    if isinstance(enabled, str):
        workflow.enabled = enabled.lower() not in ("", "0", "false", "no", "off")
    else:
        workflow.enabled = bool(enabled)
    interval = data.get("interval_minutes")
    try:
        interval = int(interval) if interval not in (None, "", 0, "0") else None
        if interval is not None and interval < 1:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "The schedule interval must be a positive "
                                 "number of minutes."}), 400
    workflow.interval_minutes = interval
    db.session.commit()
    return jsonify({"ok": True})


@bp.post("/<int:workflow_id>/delete")
def delete(workflow_id):
    workflow = _owned_workflow(workflow_id)
    db.session.delete(workflow)
    db.session.commit()
    return redirect(url_for("automation.index"))


@bp.post("/<int:workflow_id>/duplicate")
def duplicate(workflow_id):
    workflow = _owned_workflow(workflow_id)
    clone = AutomationWorkflow(
        user_id=current_user.id, name=(workflow.name + " (copy)")[:160],
        description=workflow.description, graph=workflow.graph,
        enabled=False, interval_minutes=workflow.interval_minutes)
    db.session.add(clone)
    db.session.commit()
    return redirect(url_for("automation.editor", workflow_id=clone.id))


@bp.post("/<int:workflow_id>/ai-flags")
def ai_flags(workflow_id):
    """Toggle the AI access flags (lock / hide) from the editor toolbar."""
    workflow = _owned_workflow(workflow_id)
    data = request.get_json(silent=True) or {}
    if "ai_lock" in data:
        workflow.ai_lock = bool(data["ai_lock"])
    if "ai_hidden" in data:
        workflow.ai_hidden = bool(data["ai_hidden"])
    db.session.commit()
    return jsonify({"ok": True, "ai_lock": bool(workflow.ai_lock),
                    "ai_hidden": bool(workflow.ai_hidden)})


@bp.post("/<int:workflow_id>/run")
def run(workflow_id):
    workflow = _owned_workflow(workflow_id)
    try:
        graph = engine.parse_graph(workflow.graph)
        if not graph["nodes"]:
            raise engine.WorkflowError(
                "The workflow is empty — add some nodes first.")
        engine.validate_graph(graph, active_node_types())
    except engine.WorkflowError as exc:
        return jsonify({"error": str(exc)}), 400
    if runtime.has_active_run(workflow.id):
        return jsonify({"error": "This workflow already has a run in "
                                 "progress."}), 409
    # Optional trigger file (drive → right-click → Run workflow).
    trigger_file_id = None
    data = request.get_json(silent=True) or {}
    if data.get("file_id") not in (None, ""):
        try:
            trigger_file_id = int(data["file_id"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid file id."}), 400
        stored = db.session.get(StoredFile, trigger_file_id)
        if (stored is None or stored.user_id != current_user.id
                or stored.deleted_at is not None):
            return jsonify({"error": "The trigger file does not exist "
                                     "(or is in the trash)."}), 404
    # Answers to input.param nodes collected in the run form.
    params = {}
    raw_params = data.get("params")
    if isinstance(raw_params, dict):
        params = {str(k)[:80]: str(v)[:20000]
                  for k, v in raw_params.items() if v not in (None, "")}
    missing = [s["label"] for s in nodes.run_param_specs(graph)
               if s["required"] and s["param"] not in params]
    if missing:
        return jsonify({"error": "Missing run parameters: "
                                 + ", ".join(missing)}), 400
    for spec in nodes.run_param_specs(graph):
        if spec["kind"] != "file" or spec["param"] not in params:
            continue
        try:
            fid = int(params[spec["param"]])
        except (TypeError, ValueError):
            return jsonify({"error": f"Run parameter '{spec['label']}' is "
                                     "not a valid file."}), 400
        stored = db.session.get(StoredFile, fid)
        if (stored is None or stored.user_id != current_user.id
                or stored.deleted_at is not None):
            return jsonify({"error": f"The file for '{spec['label']}' does "
                                     "not exist (or is in the trash)."}), 404
        params[spec["param"]] = str(fid)
    active = runtime.start_run(workflow, current_user, trigger="manual",
                               trigger_file_id=trigger_file_id,
                               params=params or None)
    return jsonify({"ok": True, "run_id": active.id,
                    "url": url_for("automation.run_detail", run_id=active.id)})


@bp.get("/api/for-file")
def api_for_file():
    """Workflows that accept a file trigger — the drive's Run workflow menu.

    Returns {"workflows": [...]} or 404-ish empty when none qualify; the
    drive page hides its menu entry when the module answers with none."""
    workflows = (AutomationWorkflow.query
                 .filter_by(user_id=current_user.id)
                 .order_by(AutomationWorkflow.updated_at.desc()).all())
    triggerable = []
    for workflow in workflows:
        try:
            graph = engine.parse_graph(workflow.graph)
        except engine.WorkflowError:
            continue
        if any(n["type"] == "input.trigger" for n in graph["nodes"]):
            triggerable.append({"id": workflow.id, "name": workflow.name,
                                "description": workflow.description[:120]})
    return jsonify({"workflows": triggerable})


@bp.get("/api/file-shape")
def api_file_shape():
    """Columns/keys of a picked file — the editor shows them as live hints
    next to the column/path fields of the table and JSON nodes."""
    file_id = request.args.get("file_id", type=int)
    stored = db.session.get(StoredFile, file_id) if file_id else None
    if (stored is None or stored.user_id != current_user.id
            or stored.deleted_at is not None):
        abort(404)
    ext = (stored.extension or "").lower()
    empty = {"kind": "text", "columns": [], "keys": []}
    if ext not in ("csv", "xlsx", "xlsm", "xls", "json"):
        return jsonify(empty)
    try:
        payload = nodes.load_file_payload(current_user, stored.id)
    except Exception:  # noqa: BLE001 - unreadable file: just no hints
        return jsonify(empty)
    if payload["kind"] == "table":
        return jsonify({"kind": "table",
                        "columns": payload["data"]["columns"][:50],
                        "keys": []})
    if payload["kind"] == "json":
        data = payload["data"]
        if isinstance(data, dict):
            keys = list(data.keys())
        elif data and isinstance(data, list) and isinstance(data[0], dict):
            keys = list(data[0].keys())
        else:
            keys = []
        return jsonify({"kind": "json", "columns": [], "keys": keys[:50]})
    return jsonify(empty)


@bp.get("/api/notebook-cells")
def api_notebook_cells():
    """Cells of one of the user's notebooks — the Save-to-notebook node's
    "update a specific cell" picker."""
    from app.services import module_service
    if not module_service.is_enabled("notebooks"):
        abort(404)
    import importlib
    try:
        doc = importlib.import_module("docindex_module_notebooks.doc")
    except ImportError:
        abort(404)
    notebook_id = request.args.get("notebook_id", type=int)
    stored = doc.get_notebook(notebook_id, current_user.id) \
        if notebook_id else None
    if stored is None:
        abort(404)
    cells = []
    for i, cell in enumerate(doc.load(stored).get("cells", [])):
        preview = " ".join(str(cell.get("content", "")).split())[:60]
        cells.append({"id": cell.get("id"), "index": i + 1,
                      "type": cell.get("type"), "preview": preview})
    return jsonify({"cells": cells})


# ---------------------------------------------------------------------------
# Behaviors moved to core (/behaviors) — keep the old URL as a redirect.
# ---------------------------------------------------------------------------

@bp.get("/behaviors")
def behaviors():
    return redirect(url_for("behaviors.index"))


@bp.post("/<int:workflow_id>/preview")
def preview(workflow_id):
    """Execute a graph fragment (e.g. a node and its ancestors) without
    persisting a run — the editor's right-click "run up to this node".
    With dry_run=true, save/output nodes are stubbed (no files written)."""
    _owned_workflow(workflow_id)  # ownership check only
    data = request.get_json(silent=True) or {}
    raw = data.get("graph")
    dry_run = bool(data.get("dry_run"))
    entries = []
    try:
        graph = engine.parse_graph(raw if isinstance(raw, str)
                                   else json.dumps(raw or {}))
        if not graph["nodes"]:
            raise engine.WorkflowError("Nothing to execute.")

        def log(nid, label, status, summary, ms):
            if status == "running":
                return  # preview is synchronous — no live progress entries
            entries.append({"node": nid, "label": label, "status": status,
                            "summary": summary, "ms": ms})

        node_types = dict(active_node_types())
        if dry_run:
            # Stub side-effecting nodes: report what WOULD be saved.
            def _dry_save(kind):
                def _run(ctx, config, inputs):
                    name = (config.get("name") or "output").strip() or "output"
                    ctx["log"](f"[dry run] would save '{name}' ({kind})")
                    return engine.p_text(f"[dry run] {name}")
                return _run
            for tname in ("output.save_file", "output.save_table",
                          "notebook.output"):
                if tname in node_types:
                    spec = node_types[tname]["spec"]
                    node_types[tname] = {"spec": spec,
                                         "executor": _dry_save(tname),
                                         "module": node_types[tname]["module"]}
        outputs, results, created = engine.execute_graph(
            graph, {"user": current_user, "app": current_app},
            node_types=node_types, log=log)
    except engine.WorkflowError as exc:
        return jsonify({"error": str(exc), "log": entries}), 400
    # Per-node output previews so the editor can show "View output".
    previews = engine.preview_outputs(outputs)
    return jsonify({"ok": True, "log": entries, "results": results,
                    "files": created, "outputs": previews})


@bp.get("/<int:workflow_id>/runs")
def runs(workflow_id):
    workflow = _owned_workflow(workflow_id)
    workflow_runs = (AutomationRun.query
                     .filter_by(workflow_id=workflow.id)
                     .order_by(AutomationRun.id.desc()).limit(100).all())
    return render_template("automation/runs.html", workflow=workflow,
                           runs=workflow_runs)


@bp.get("/runs/<int:run_id>")
def run_detail(run_id):
    run = _owned_run(run_id)
    params = None
    if run.params:
        try:
            params = json.loads(run.params)
        except ValueError:
            params = None
    return render_template("automation/run.html", run=run, params=params)


@bp.get("/runs/<int:run_id>/status")
def run_status(run_id):
    run = _owned_run(run_id)
    log_entries = json.loads(run.log) if run.log else []
    # Progress + pipeline: the graph's nodes in execution order, so the run
    # page can show the executed nodes (with their times), the one running
    # now and the ones still pending, plus an overall percentage.
    total = 0
    pipeline = []
    graph = {"nodes": [], "edges": []}
    if run.workflow is not None:
        try:
            graph = engine.parse_graph(run.workflow.graph)
            nodes_by_id, _edges, order = engine.validate_graph(
                graph, active_node_types())
            registry = active_node_types()
            total = len(order)
            pipeline = [
                {"node": nid,
                 "label": registry[nodes_by_id[nid]["type"]]["spec"]
                          .get("label", nodes_by_id[nid]["type"]),
                 "type": nodes_by_id[nid]["type"]}
                for nid in order]
        except engine.WorkflowError:
            total = len(graph["nodes"]) if graph["nodes"] else 0
    finished = sum(1 for e in log_entries if e.get("status") != "running")
    current = next((e for e in reversed(log_entries)
                    if e.get("status") == "running"), None)
    if run.status == "done":
        percent = 100
    elif total:
        percent = min(99, round(finished / total * 100))
    else:
        percent = 0
    output = json.loads(run.output) if run.output else None
    return jsonify({
        "id": run.id, "status": run.status, "error": run.error,
        "log": log_entries,
        "output": output,
        # Per-node output previews (runs done before this field existed
        # simply don't have it — the popup then says "not captured").
        "node_outputs": (output or {}).get("node_outputs") or {},
        "inputs": _run_inputs(run, graph["nodes"]),
        "progress": {
            "total": total, "finished": finished, "percent": percent,
            "current": ({"node": current["node"], "label": current["label"]}
                        if current else None),
        },
        "pipeline": pipeline,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": (run.finished_at.isoformat()
                        if run.finished_at else None),
    })


def _run_inputs(run, graph_nodes):
    """Files/objects the run consumed: the trigger file, files picked in
    the run form (input.param of kind file) and static input.file nodes.
    Returns [{file_id, name, via}] — files deleted or foreign are skipped."""
    inputs, seen = [], set()

    def add(file_id, via):
        try:
            fid = int(file_id)
        except (TypeError, ValueError):
            return
        if fid in seen:
            return
        stored = db.session.get(StoredFile, fid)
        if stored is None or stored.user_id != run.user_id:
            return
        seen.add(fid)
        inputs.append({"file_id": fid, "name": stored.name, "via": via})

    add(run.trigger_file_id, "trigger")
    params = {}
    if run.params:
        try:
            params = json.loads(run.params)
        except ValueError:
            params = {}
    for node in graph_nodes or []:
        ntype = node.get("type")
        config = node.get("config") or {}
        if ntype == "input.file" and config.get("file_id"):
            add(config["file_id"], f"node {node.get('id')}")
        elif ntype == "input.param" and config.get("kind") == "file":
            label = config.get("label") or config.get("param") or "input"
            add(params.get(config.get("param")), f"input: {label}")
    return inputs


@bp.post("/runs/<int:run_id>/stop")
def run_stop(run_id):
    run = runtime.stop_run(current_user, run_id)
    if run is None:
        abort(404)
    return jsonify({"ok": True, "status": run.status})


@bp.post("/runs/<int:run_id>/retry")
def run_retry(run_id):
    """Start a new run with the same workflow + params + trigger file."""
    run = _owned_run(run_id)
    workflow = db.session.get(AutomationWorkflow, run.workflow_id)
    if workflow is None or workflow.user_id != current_user.id:
        abort(404)
    if runtime.has_active_run(workflow.id):
        flash("This workflow already has a run in progress.", "error")
        return redirect(url_for("automation.run_detail", run_id=run.id))
    try:
        params = json.loads(run.params) if run.params else None
    except ValueError:
        params = None
    try:
        active = runtime.start_run(workflow, current_user, trigger=run.trigger,
                                   trigger_file_id=run.trigger_file_id,
                                   params=params)
    except engine.WorkflowError as exc:
        flash(str(exc), "error")
        return redirect(url_for("automation.run_detail", run_id=run.id))
    return redirect(url_for("automation.run_detail", run_id=active.id))


@bp.get("/api/folders")
def api_folders():
    """All folders of a drive as [{id, path}] ordered by path — feeds the
    editor's folder picker (save nodes, folder listing). Only the user's
    own drives."""
    drive_id = request.args.get("drive_id", type=int)
    drive = db.session.get(Drive, drive_id) if drive_id else None
    if drive is None or drive.user_id != current_user.id:
        abort(404)
    folders = (Folder.query.filter_by(drive_id=drive.id)
               .order_by(Folder.name).all())
    by_id = {f.id: f for f in folders}
    paths = []
    for f in folders:
        parts, cur = [], f
        while cur is not None:
            parts.append(cur.name)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        paths.append({"id": f.id, "path": "/".join(reversed(parts))})
    paths.sort(key=lambda x: x["path"].lower())
    return jsonify({"drive": {"id": drive.id, "name": drive.name},
                    "folders": paths})


@bp.get("/api/browse")
def api_browse():
    """Drive/folder/file browser for the editor's file picker.

    ``?file_id=N`` resolves one file (for the picker's current value);
    otherwise lists the folders and files of ``drive_id`` at ``folder_id``
    (or the drive root). Only the user's own, non-trashed content.
    """
    file_id = request.args.get("file_id", type=int)
    if file_id:
        stored = db.session.get(StoredFile, file_id)
        if (stored is None or stored.user_id != current_user.id
                or stored.deleted_at is not None):
            abort(404)
        return jsonify({"file_id": stored.id, "name": stored.name,
                        "extension": stored.extension})

    drive_id = request.args.get("drive_id", type=int)
    drive = db.session.get(Drive, drive_id) if drive_id else None
    if drive is None or drive.user_id != current_user.id:
        abort(404)
    folder_id = request.args.get("folder_id", type=int)
    parent = None
    crumbs = []
    if folder_id:
        parent = db.session.get(Folder, folder_id)
        if parent is None or parent.drive_id != drive.id:
            abort(404)
        chain, cur = [], parent
        while cur is not None:
            chain.append({"id": cur.id, "name": cur.name})
            cur = cur.parent
        crumbs = list(reversed(chain))
    folders = (Folder.query
               .filter_by(drive_id=drive.id,
                          parent_id=parent.id if parent else None)
               .order_by(Folder.name).all())
    files = (StoredFile.query
             .filter_by(drive_id=drive.id,
                        folder_id=parent.id if parent else None,
                        user_id=current_user.id)
             .filter(StoredFile.deleted_at.is_(None))
             .order_by(StoredFile.name).all())
    return jsonify({
        "drive": {"id": drive.id, "name": drive.name,
                  "is_synced": bool(drive.is_synced)},
        "crumbs": crumbs,
        "folders": [{"id": f.id, "name": f.name} for f in folders],
        "files": [{"file_id": f.id, "name": f.name,
                   "extension": f.extension,
                   "size": f.size} for f in files],
    })
