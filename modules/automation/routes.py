"""Automation module routes: workflow list, editor, runs."""

import json

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from app.extensions import db
from app.models import Drive, Folder, StoredFile

from . import engine, examples, nodes, runtime
from .models import AutomationFolder, AutomationRun, AutomationWorkflow
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


def _owned_folder(folder_id):
    folder = db.session.get(AutomationFolder, folder_id)
    if folder is None or folder.user_id != current_user.id:
        abort(404)
    return folder


def _folder_tree():
    """Top-level folders of the user (children via the .children rel)."""
    all_folders = (AutomationFolder.query
                   .filter_by(user_id=current_user.id)
                   .order_by(AutomationFolder.name).all())
    by_id = {f.id: f for f in all_folders}
    top = [f for f in all_folders if f.parent_id is None]
    flat = []

    def visit(fid, depth):
        for f in all_folders:
            if (f.parent_id or None) == fid:
                flat.append({"id": f.id, "name": f.name, "depth": depth})
                visit(f.id, depth + 1)
    visit(None, 0)
    return top, by_id, flat


@bp.get("/")
def index():
    q = (request.args.get("q") or "").strip()
    query = AutomationWorkflow.query.filter_by(user_id=current_user.id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(
            AutomationWorkflow.name.ilike(like),
            AutomationWorkflow.description.ilike(like)))
    workflows = query.order_by(
        AutomationWorkflow.updated_at.desc()).all()
    recent_runs = (AutomationRun.query
                   .filter_by(user_id=current_user.id)
                   .order_by(AutomationRun.id.desc()).limit(10).all())
    folders, by_id, flat_folders = _folder_tree()
    counts = dict(
        db.session.query(AutomationWorkflow.folder_id,
                         db.func.count(AutomationWorkflow.id))
        .filter(AutomationWorkflow.user_id == current_user.id)
        .group_by(AutomationWorkflow.folder_id).all())
    # Group workflows by folder for the single tree section.
    wf_by_folder = {}
    for w in workflows:
        wf_by_folder.setdefault(w.folder_id, []).append(w)
    # When searching, only folders on the path to a match stay visible.
    visible = None
    if q:
        visible = set()
        for w in workflows:
            fid = w.folder_id
            while fid is not None and fid not in visible:
                visible.add(fid)
                parent = by_id.get(fid)
                fid = parent.parent_id if parent else None
    return render_template("automation/list.html", workflows=workflows,
                           examples=examples.EXAMPLES,
                           recent_runs=recent_runs, folders=folders,
                           flat_folders=flat_folders,
                           folder_counts=counts, q=q,
                           wf_by_folder=wf_by_folder, visible=visible)


@bp.post("/folders/create")
def folder_create():
    name = (request.form.get("name") or "").strip()[:120]
    if not name:
        abort(400, "The folder needs a name.")
    parent_id = request.form.get("parent_id", type=int)
    parent = None
    if parent_id:
        parent = _owned_folder(parent_id)
    folder = AutomationFolder(user_id=current_user.id, name=name,
                              parent_id=parent.id if parent else None)
    # Guard against absurd nesting depth (UI tree renders recursively).
    depth, cur = 0, parent
    while cur is not None and depth < 10:
        cur = cur.parent
        depth += 1
    if depth >= 10:
        abort(400, "Folders cannot nest deeper than 10 levels.")
    db.session.add(folder)
    db.session.commit()
    return redirect(url_for("automation.index"))


@bp.post("/folders/<int:folder_id>/rename")
def folder_rename(folder_id):
    folder = _owned_folder(folder_id)
    name = (request.form.get("name") or "").strip()[:120]
    if not name:
        abort(400, "The folder needs a name.")
    folder.name = name
    db.session.commit()
    return redirect(url_for("automation.index"))


@bp.post("/folders/<int:folder_id>/delete")
def folder_delete(folder_id):
    folder = _owned_folder(folder_id)
    # Children move up one level; workflows move to root.
    for child in folder.children:
        child.parent_id = folder.parent_id
    AutomationWorkflow.query.filter_by(
        folder_id=folder.id).update({"folder_id": None})
    db.session.delete(folder)
    db.session.commit()
    return redirect(url_for("automation.index"))


@bp.post("/folders/<int:folder_id>/move")
def folder_move(folder_id):
    """Move a folder into another (or back to root)."""
    folder = _owned_folder(folder_id)
    parent_id = request.form.get("parent_id", type=int)
    if not parent_id:
        folder.parent_id = None
        db.session.commit()
        return redirect(_clean_next())
    if parent_id == folder.id:
        abort(400, "A folder cannot contain itself.")
    parent = _owned_folder(parent_id)
    # Cycle guard: the target must not be a descendant of the folder.
    cur = parent
    while cur is not None:
        if cur.id == folder.id:
            abort(400, "Cannot move a folder into one of its own subfolders.")
        cur = cur.parent
    depth = 1
    cur = parent
    while cur is not None and depth < 10:
        cur = cur.parent
        depth += 1
    if depth >= 10:
        abort(400, "Folders cannot nest deeper than 10 levels.")
    folder.parent_id = parent.id
    db.session.commit()
    return redirect(_clean_next())


@bp.post("/folders/cleanup")
def folders_cleanup():
    """Delete every folder that is empty (no workflows, no subfolders)."""
    removed = 0
    changed = True
    while changed:
        changed = False
        children = {f.parent_id for f in AutomationFolder.query
                    .filter_by(user_id=current_user.id)
                    .filter(AutomationFolder.parent_id.isnot(None)).all()}
        counts = dict(
            db.session.query(AutomationWorkflow.folder_id,
                             db.func.count(AutomationWorkflow.id))
            .filter(AutomationWorkflow.user_id == current_user.id)
            .group_by(AutomationWorkflow.folder_id).all())
        for f in AutomationFolder.query.filter_by(
                user_id=current_user.id).all():
            if counts.get(f.id, 0) == 0 and f.id not in children:
                db.session.delete(f)
                removed += 1
                changed = True
        if changed:
            db.session.commit()
    flash(f"Removed {removed} empty folder(s)." if removed else
          "No empty folders to remove.",
          "success" if removed else "info")
    return redirect(url_for("automation.index"))


def _clean_next():
    """Safe redirect target for folder moves (same page only)."""
    nxt = request.form.get("next") or url_for("automation.index")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") \
        else url_for("automation.index")


def _wants_json():
    return request.args.get("ajax") == "1" or request.form.get("ajax") == "1"


@bp.post("/<int:workflow_id>/move")
def move(workflow_id):
    """Move a workflow into a folder (or back to root)."""
    workflow = _owned_workflow(workflow_id)
    folder_id = request.form.get("folder_id", type=int)
    if folder_id in (None, 0, -1):
        workflow.folder_id = None
    else:
        folder = _owned_folder(folder_id)
        workflow.folder_id = folder.id
    db.session.commit()
    if _wants_json():
        return jsonify({"ok": True, "id": workflow.id,
                        "folder_id": workflow.folder_id})
    return redirect(request.form.get("next") or url_for("automation.index"))


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
    schedule_file = None
    if workflow.schedule_file_id:
        stored = db.session.get(StoredFile, workflow.schedule_file_id)
        if stored is not None and stored.user_id == current_user.id:
            schedule_file = {"id": stored.id, "name": stored.name}
    return render_template("automation/editor.html", workflow=workflow,
                           palette=palette(), drives=_writable_drives(),
                           notebooks=_notebooks(),
                           schedule_file=schedule_file)


def _notebooks():
    """User's notebooks for the notebook node pickers ([] when the module
    is disabled). Each entry carries its drive/folder location so the
    editor groups the picker by the organized folders."""
    from app.services import module_service
    if not module_service.is_enabled("notebooks"):
        return []
    import importlib
    try:
        doc = importlib.import_module("docindex_module_notebooks.doc")
    except ImportError:
        return []
    items = []
    for n in doc.list_notebooks(current_user.id):
        drive_name = n.drive.name if n.drive else "No drive"
        folder_name = n.folder.name if n.folder else "/"
        items.append({"id": n.id, "name": n.name,
                      "location": f"{drive_name} / {folder_name}"})
    return items


@bp.get("/<int:workflow_id>/graph")
def graph_json(workflow_id):
    """Current saved graph — used by the editor to reload after the AI
    copilot edits the workflow from the chat."""
    workflow = _owned_workflow(workflow_id)
    return jsonify({"graph": engine.parse_graph(workflow.graph),
                    "version": (workflow.updated_at.isoformat()
                                if workflow.updated_at else None)})


@bp.post("/<int:workflow_id>/save")
def save(workflow_id):
    workflow = _owned_workflow(workflow_id)
    data = request.get_json(silent=True) or {}
    # Only replace the graph when one is sent — meta-only saves (schedule
    # toggles, renames) must not wipe it.
    if "graph" in data:
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
    # Schedule profile: frozen run-form answers the schedule replays.
    # Validated only when scheduling itself is touched, so unrelated
    # saves on legacy scheduled workflows keep working.
    schedule_touched = any(k in data for k in (
        "enabled", "interval_minutes", "schedule_params",
        "schedule_file_id"))
    sched_params, sched_file = None, None
    if "schedule_params" in data:
        raw = data.get("schedule_params")
        if isinstance(raw, dict):
            sched_params = {str(k)[:80]: v
                            for k, v in raw.items()
                            if v not in (None, "")}
        workflow.schedule_params = json.dumps(sched_params) \
            if sched_params else None
    if "schedule_file_id" in data:
        try:
            sched_file = int(data.get("schedule_file_id"))
        except (TypeError, ValueError):
            sched_file = None
        workflow.schedule_file_id = sched_file
    if schedule_touched and workflow.enabled and workflow.interval_minutes:
        try:
            _graph_for_schedule = engine.parse_graph(workflow.graph)
        except engine.WorkflowError as exc:
            return jsonify({"error": f"Cannot schedule a broken workflow: "
                                     f"{exc}"}), 400
        profile = {}
        if workflow.schedule_params:
            try:
                profile = json.loads(workflow.schedule_params) or {}
            except ValueError:
                profile = {}
        _clean, _errors = nodes.validate_run_params(
            _graph_for_schedule, profile, current_user)
        if _errors:
            return jsonify({"error": "The schedule needs answers for: "
                                     + "; ".join(_errors)}), 400
        if nodes.run_contract(_graph_for_schedule)["trigger"]:
            if not workflow.schedule_file_id:
                return jsonify({"error": "This workflow starts from a file "
                                         "— pick the schedule's starting "
                                         "file."}), 400
            stored = db.session.get(StoredFile, workflow.schedule_file_id)
            if (stored is None or stored.user_id != current_user.id
                    or stored.deleted_at is not None):
                return jsonify({"error": "The schedule's starting file does "
                                         "not exist (or is in the trash)."}), 400
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
    # Answers to input.param nodes collected in the run form — one
    # shared validator for every entry point (editor, drive menu, chat,
    # AI, schedule profiles): trims, canonicalizes and checks ownership.
    params = {}
    raw_params = data.get("params")
    if isinstance(raw_params, dict):
        params = {str(k)[:80]: v
                  for k, v in raw_params.items() if v not in (None, "")}
    clean, errors = nodes.validate_run_params(graph, params, current_user)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    active = runtime.start_run(workflow, current_user, trigger="manual",
                               trigger_file_id=trigger_file_id,
                               params=clean or None)
    return jsonify({"ok": True, "run_id": active.id,
                    "url": url_for("automation.run_detail", run_id=active.id)})


@bp.get("/<int:workflow_id>/run-contract")
def run_contract(workflow_id):
    """The run contract: what a run of this workflow needs (starting file
    + parameter questions). Every run form renders from here, so the
    editor, drive menu, chat and AI all ask the same questions."""
    workflow = _owned_workflow(workflow_id)
    try:
        graph = engine.parse_graph(workflow.graph)
    except engine.WorkflowError as exc:
        return jsonify({"error": f"The workflow is broken: {exc}"}), 400
    contract = nodes.run_contract(graph)
    drives = [{"id": d.id, "name": d.name}
              for d in Drive.query.filter_by(user_id=current_user.id)
              .order_by(Drive.name).all()]
    contract["drives"] = drives
    return jsonify(contract)


@bp.get("/api/attachable")
def api_attachable():
    """Workflows that can consume a chat attachment: those with an
    input.trigger node, and those asking for a file via input.param.
    The chat's "Run workflow…" chip button offers these; picking one runs
    the workflow with trigger_file_id / params={param: file_id}."""
    out = []
    workflows = (AutomationWorkflow.query
                 .filter_by(user_id=current_user.id)
                 .order_by(AutomationWorkflow.updated_at.desc()).all())
    folders = _folder_map()
    for workflow in workflows:
        try:
            graph = engine.parse_graph(workflow.graph)
        except engine.WorkflowError:
            continue
        has_trigger = any(n.get("type") == "input.trigger"
                          for n in graph["nodes"])
        try:
            file_params = [{"param": s["param"], "label": s["label"]}
                           for s in nodes.run_param_specs(graph)
                           if s["kind"] == "file"]
        except Exception:  # noqa: BLE001 - treat as no file params
            file_params = []
        if has_trigger or file_params:
            out.append({"id": workflow.id, "name": workflow.name,
                        "trigger": has_trigger, "file_params": file_params,
                        "folder": _folder_path(folders, workflow.folder_id)
                        if workflow.folder_id else ""})
    return jsonify({"workflows": out})


def _folder_map():
    return {f.id: f for f in AutomationFolder.query.filter_by(
        user_id=current_user.id).all()}


def _folder_path(folders, folder_id):
    """'Parent / Child' breadcrumb for a folder id ("" at the root)."""
    parts, seen = [], set()
    while folder_id and folder_id not in seen:
        seen.add(folder_id)
        folder = folders.get(folder_id)
        if folder is None:
            break
        parts.append(folder.name)
        folder_id = folder.parent_id
    return " / ".join(reversed(parts))


@bp.get("/api/for-file")
def api_for_file():
    """Workflows that accept a file trigger — the drive's Run workflow menu.

    Returns {"workflows": [...]} or 404-ish empty when none qualify; the
    drive page hides its menu entry when the module answers with none."""
    workflows = (AutomationWorkflow.query
                 .filter_by(user_id=current_user.id)
                 .order_by(AutomationWorkflow.updated_at.desc()).all())
    folders = _folder_map()

    triggerable = []
    # Latest run per workflow (one query) for the status badge + time.
    run_rows = (AutomationRun.query
               .filter(AutomationRun.workflow_id.in_(
                   [w.id for w in workflows]),
                       AutomationRun.user_id == current_user.id)
               .order_by(AutomationRun.id.desc()).all())
    last_by_wf = {}
    for run in run_rows:
        last_by_wf.setdefault(run.workflow_id, run)
    for workflow in workflows:
        try:
            graph = engine.parse_graph(workflow.graph)
        except engine.WorkflowError:
            continue
        if any(n["type"] == "input.trigger" for n in graph["nodes"]):
            last = last_by_wf.get(workflow.id)
            triggerable.append({
                "id": workflow.id, "name": workflow.name,
                "description": workflow.description[:120],
                "folder": _folder_path(folders, workflow.folder_id)
                if workflow.folder_id else "",
                "schedule": ({"interval_minutes": workflow.interval_minutes,
                              "enabled": bool(workflow.enabled)}
                             if workflow.interval_minutes else None),
                "last_run": ({"status": last.status,
                              "finished_at": (last.finished_at.isoformat()
                                             if last.finished_at else None)}
                             if last else None),
            })
    return jsonify({"workflows": triggerable})


@bp.get("/api/file-shape")
def api_file_shape():
    """Columns/keys of a picked file — the editor shows them as live hints
    next to the column/path fields of the table and JSON nodes."""
    file_id = request.args.get("file_id", type=int)
    shape = _file_shape(current_user, file_id)
    if shape is None:
        abort(404)
    return jsonify(shape)


def _file_shape(user, file_id):
    """{kind, columns, keys} for a CSV/Excel/JSON file, or None when the
    file is missing, foreign, trashed or not tabular."""
    stored = db.session.get(StoredFile, file_id) if file_id else None
    if (stored is None or stored.user_id != user.id
            or stored.deleted_at is not None):
        return None
    ext = (stored.extension or "").lower()
    empty = {"kind": "text", "columns": [], "keys": []}
    if ext not in ("csv", "xlsx", "xlsm", "xls", "json"):
        return empty
    try:
        payload = nodes.load_file_payload(user, stored.id)
    except Exception:  # noqa: BLE001 - unreadable file: just no hints
        return empty
    if payload["kind"] == "table":
        return {"kind": "table",
                "columns": payload["data"]["columns"][:50],
                "keys": []}
    if payload["kind"] == "json":
        data = payload["data"]
        if isinstance(data, dict):
            keys = list(data.keys())
        elif data and isinstance(data, list) and isinstance(data[0], dict):
            keys = list(data[0].keys())
        else:
            keys = []
        return {"kind": "json", "columns": [], "keys": keys[:50]}
    return empty


# Node types whose output columns derive mechanically from their input —
# the editor walks these backwards to offer real column names on table
# nodes several hops away from the source file.
_SHAPE_PRESERVING = {
    "transform.table_filter", "transform.table_sort",
    "transform.table_dedupe", "logic.merge",
}


def _trace_columns(graph, target_id):
    """Column names available at a node's input, traced backwards.

    Returns (columns|None, certain: bool): walks back through
    shape-preserving nodes and one static input.file; table_select
    narrows to its picks, table_compute adds its new column. Anything
    else (pivot, json_to_table, AI, ...) stops the trace as uncertain.
    """
    by_id = {n.get("id"): n for n in graph.get("nodes", [])}
    inbound = {}
    for edge in graph.get("edges", []):
        inbound.setdefault(edge.get("to"), []).append(edge)

    def _file_columns(config):
        try:
            fid = int(config.get("file_id"))
        except (TypeError, ValueError):
            return None
        shape = _file_shape(current_user, fid)
        if shape and shape["kind"] == "table" and shape["columns"]:
            return shape["columns"]
        return None

    seen = set()

    def walk(nid):
        if nid in seen:
            return None
        seen.add(nid)
        node = by_id.get(nid)
        if node is None:
            return None
        ntype = node.get("type")
        config = node.get("config") or {}
        if ntype == "input.file":
            return _file_columns(config)
        if ntype == "transform.table_select":
            picks = config.get("picks") or []
            cols = [p.get("column") for p in picks if p.get("column")]
            if cols:
                return cols
            # No picks yet: whatever flows in.
            ins = inbound.get(nid, [])
            return walk(ins[0]["from"]) if len(ins) == 1 else None
        if ntype == "transform.table_compute":
            ins = inbound.get(nid, [])
            base = walk(ins[0]["from"]) if len(ins) == 1 else None
            if base is None:
                return None
            new_col = (config.get("new_column") or "").strip()
            return base + ([new_col] if new_col and new_col not in base
                           else [])
        if ntype in _SHAPE_PRESERVING:
            ins = inbound.get(nid, [])
            if len(ins) == 1:
                return walk(ins[0]["from"])
            return None
        return None

    cols = walk(target_id)
    return cols


@bp.get("/api/upstream-shape")
def api_upstream_shape():
    """Column names available at a node's input, traced backwards through
    shape-preserving nodes to the source file. The editor's column
    pickers use this when no file is wired directly. Reads the SAVED
    graph (autosave lands within ~2s)."""
    workflow = _owned_workflow(request.args.get("workflow_id", type=int)
                               or 0)
    node_id = request.args.get("node")
    try:
        graph = engine.parse_graph(workflow.graph)
    except engine.WorkflowError:
        return jsonify({"columns": [], "certain": False})
    cols = _trace_columns(graph, node_id)
    if not cols:
        return jsonify({"columns": [], "certain": False})
    return jsonify({"columns": cols[:100], "certain": True})


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


@bp.get("/<int:workflow_id>/diagnose")
def diagnose(workflow_id):
    """Static check without running: every problem at once (node, field,
    severity, message). Powers the editor's Check button; the AI uses
    validate_workflow for the same plus reference checks."""
    workflow = _owned_workflow(workflow_id)
    try:
        graph = engine.parse_graph(workflow.graph)
    except engine.WorkflowError as exc:
        return jsonify({"ok": False, "issues": [
            {"node": None, "type": None, "field": None,
             "severity": "error", "message": str(exc)}]})
    return jsonify(engine.diagnose_graph(graph, active_node_types()))


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


@bp.get("/runs/active")
def runs_active():
    """Active + recently finished runs of the current user.

    Backs the global background-run widget (the automation equivalent of
    /sync/active): the widget polls this to show live progress anywhere in
    the app — including schedule/AI-triggered runs the user never opened.
    Final states stay listed briefly so a just-finished run still toasts.
    """
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    active = (AutomationRun.query
              .filter_by(user_id=current_user.id)
              .filter(AutomationRun.status.in_(("queued", "running")))
              .order_by(AutomationRun.id.desc()).limit(10).all())
    finished = (AutomationRun.query
                .filter_by(user_id=current_user.id)
                .filter(AutomationRun.status.in_(AutomationRun.FINAL_STATUSES))
                .filter(AutomationRun.finished_at.isnot(None))
                .filter(AutomationRun.finished_at >= cutoff)
                .filter(~AutomationRun.id.in_([r.id for r in active] or [0]))
                .order_by(AutomationRun.id.desc()).limit(5).all())
    out = []
    for run in active + finished:
        prog = _run_progress(run)
        out.append({
            "id": run.id, "status": run.status, "trigger": run.trigger,
            "workflow_id": run.workflow_id,
            "workflow_name": (run.workflow.name
                              if run.workflow is not None else "Workflow"),
            "error": (run.error or "")[:200],
            "progress": {"total": prog["total"],
                         "finished": prog["finished"],
                         "percent": prog["percent"],
                         "current": prog["current"]},
            "url": url_for("automation.run_detail", run_id=run.id),
            "started_at": (run.started_at.isoformat()
                           if run.started_at else None),
            "finished_at": (run.finished_at.isoformat()
                            if run.finished_at else None),
        })
    return jsonify({"runs": out})


def _run_progress(run):
    """Shared progress computation for run_status and runs_active.

    Returns {"log", "total", "pipeline", "finished", "current", "percent",
    "output", "graph"} — the run page shows the executed nodes (with their
    times), the one running now and the ones still pending, plus a percent.
    """
    log_entries = json.loads(run.log) if run.log else []
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
    current = ({"node": current["node"], "label": current["label"]}
               if current else None)
    if run.status == "done":
        percent = 100
    elif total:
        percent = min(99, round(finished / total * 100))
    else:
        percent = 0
    output = json.loads(run.output) if run.output else None
    return {"log": log_entries, "total": total, "pipeline": pipeline,
            "finished": finished, "current": current, "percent": percent,
            "output": output, "graph": graph}


@bp.get("/runs/<int:run_id>")
def run_detail(run_id):
    run = _owned_run(run_id)
    params = None
    if run.params:
        try:
            params = json.loads(run.params)
        except ValueError:
            params = None
    # Human-readable badges: resolve drive/file answers to names.
    badges, trigger_file = [], None
    try:
        graph = engine.parse_graph(run.workflow.graph) \
            if run.workflow is not None else {"nodes": []}
    except engine.WorkflowError:
        graph = {"nodes": []}
    if params:
        kinds = {s["param"]: s for s in nodes.run_contract(graph)["params"]}
        for key, value in params.items():
            spec = kinds.get(key, {})
            label = spec.get("label") or key
            kind = spec.get("kind")
            display = str(value)
            if kind == "drive":
                drive = db.session.get(Drive, value)
                if drive is not None and drive.user_id == run.user_id:
                    display = drive.name
            elif kind == "file":
                stored = db.session.get(StoredFile, value)
                if (stored is not None and stored.user_id == run.user_id):
                    display = stored.name
            badges.append({"label": label, "display": display})
    if run.trigger_file_id:
        stored = db.session.get(StoredFile, run.trigger_file_id)
        if stored is not None and stored.user_id == run.user_id:
            trigger_file = {"id": stored.id, "name": stored.name}
    return render_template("automation/run.html", run=run, params=params,
                           badges=badges, trigger_file=trigger_file)


@bp.get("/runs/<int:run_id>/status")
def run_status(run_id):
    run = _owned_run(run_id)
    prog = _run_progress(run)
    output = prog["output"]
    return jsonify({
        "id": run.id, "status": run.status, "error": run.error,
        "log": prog["log"],
        "output": output,
        # Per-node output previews (runs done before this field existed
        # simply don't have it — the popup then says "not captured").
        "node_outputs": (output or {}).get("node_outputs") or {},
        "inputs": _run_inputs(run, prog["graph"]["nodes"]),
        "progress": {
            "total": prog["total"], "finished": prog["finished"],
            "percent": prog["percent"], "current": prog["current"],
        },
        "pipeline": prog["pipeline"],
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
