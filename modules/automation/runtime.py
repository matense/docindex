"""Run runtime for the Automation module — mirrors the core AITask pattern:
persistent run rows, one daemon thread per run, cooperative stop between
nodes and startup recovery of interrupted runs."""

import json
import threading
from datetime import datetime, timezone

from flask import current_app

from app.extensions import db

from . import engine
from .models import AutomationRun, AutomationWorkflow

_stop_events = {}
_lock = threading.Lock()


def _utcnow():
    return datetime.now(timezone.utc)


def start_run(workflow, user, trigger="manual", app=None,
              trigger_file_id=None, params=None):
    """Create a run row and launch the worker thread.

    With AUTOMATION_ASYNC off (tests) the worker runs inline and the
    returned run is already finished. ``trigger_file_id`` records the drive
    file the run was started from (drive → Run workflow); the worker turns
    it into the ``trigger`` payload for input.trigger nodes. ``params``
    carries the answers to input.param nodes collected in the run form.
    """
    run = AutomationRun(workflow_id=workflow.id, user_id=user.id,
                        status="queued", trigger=trigger,
                        trigger_file_id=trigger_file_id,
                        params=json.dumps(params) if params else None)
    db.session.add(run)
    db.session.commit()

    event = threading.Event()
    with _lock:
        _stop_events[run.id] = event

    app = app or current_app._get_current_object()
    if app.config.get("AUTOMATION_ASYNC", True):
        threading.Thread(target=_run_worker, args=(run.id, app),
                         daemon=True).start()
    else:
        _run_worker(run.id, app)
        # The worker ran in a nested app context (its own session); reload so
        # the caller sees the final status, log and output.
        db.session.refresh(run)
    return run


def stop_run(user, run_id):
    """Cooperative stop: the engine checks the event between nodes."""
    run = _owned_run(user, run_id)
    if run is None or not run.is_active:
        return None
    with _lock:
        event = _stop_events.get(run.id)
    if event is not None:
        event.set()
    if run.status == "queued":
        run.status = "stopped"
        run.finished_at = _utcnow()
        db.session.commit()
    return run


def get_run(user, run_id):
    return _owned_run(user, run_id)


def has_active_run(workflow_id):
    return (AutomationRun.query
            .filter_by(workflow_id=workflow_id)
            .filter(AutomationRun.status.in_(("queued", "running")))
            .count()) > 0


def recover_interrupted(app):
    """On startup, runs still queued/running belong to a dead process."""
    with app.app_context():
        stale = (AutomationRun.query
                 .filter(AutomationRun.status.in_(("queued", "running")))
                 .all())
        for run in stale:
            run.status = "interrupted"
            run.error = "The server restarted while this run was active."
            run.finished_at = _utcnow()
        if stale:
            db.session.commit()
            app.logger.info("Marked %d interrupted automation run(s).",
                            len(stale))


def _owned_run(user, run_id):
    return AutomationRun.query.filter_by(id=run_id, user_id=user.id).first()


def _run_worker(run_id, app):
    with app.app_context():
        try:
            _execute(run_id, app)
        except Exception as exc:  # noqa: BLE001 - never die silently
            db.session.rollback()
            app.logger.exception("Automation run %s failed", run_id)
            run = db.session.get(AutomationRun, run_id)
            if run is not None and run.is_active:
                run.status = "error"
                run.error = str(exc)[:500]
                run.finished_at = _utcnow()
                db.session.commit()
        finally:
            with _lock:
                _stop_events.pop(run_id, None)


def _execute(run_id, app):
    run = db.session.get(AutomationRun, run_id)
    if run is None or run.status != "queued":
        return  # stopped before it started
    workflow = db.session.get(AutomationWorkflow, run.workflow_id)
    if workflow is None:
        run.status = "error"
        run.error = "The workflow was deleted."
        run.finished_at = _utcnow()
        db.session.commit()
        return

    run.status = "running"
    run.started_at = _utcnow()
    db.session.commit()

    with _lock:
        event = _stop_events.get(run_id)
    should_stop = event.is_set if event is not None else None

    entries = []
    user = workflow_runs_user(run)

    def log(node_id, label, status, summary, ms):
        entry = {"node": node_id, "label": label, "status": status,
                 "summary": summary, "ms": ms}
        # The engine emits a transient "running" entry when a node starts;
        # the node's final entry replaces it so the persisted log stays
        # one-row-per-node (nodes execute sequentially, so the "running"
        # entry is always the last one when its final entry arrives).
        if (entries and entries[-1]["node"] == node_id
                and entries[-1]["status"] == "running"):
            entries[-1] = entry
        else:
            entries.append(entry)
        # Live progress: the run page polls and shows nodes as they finish.
        run.log = json.dumps(entries)
        db.session.commit()

    try:
        graph = engine.parse_graph(workflow.graph)
        outputs, results, created = engine.execute_graph(
            graph, _build_ctx(run, user, app),
            should_stop=should_stop, log=log)
        run.output = json.dumps({
            "results": results, "files": created,
            # Per-node output previews — the run page shows them in a popup.
            "node_outputs": engine.preview_outputs(outputs)})
        run.status = "done"
    except engine._Stopped:
        run.status = "stopped"
    except engine.WorkflowError as exc:
        run.status = "error"
        run.error = str(exc)[:500]
    run.log = json.dumps(entries)
    run.finished_at = _utcnow()
    workflow.last_run_at = run.finished_at
    db.session.commit()


def workflow_runs_user(run):
    from app.models import User
    return db.session.get(User, run.user_id)


def _build_ctx(run, user, app):
    """Execution context for a run; materializes the trigger file payload."""
    ctx = {"user": user, "app": app, "trigger": None, "trigger_name": None,
           "params": {}}
    if run.params:
        try:
            ctx["params"] = json.loads(run.params)
        except ValueError:
            ctx["params"] = {}
    if run.trigger_file_id:
        from . import nodes
        try:
            ctx["trigger"] = nodes.load_file_payload(
                user, run.trigger_file_id)
            from app.models import StoredFile
            stored = db.session.get(StoredFile, run.trigger_file_id)
            ctx["trigger_name"] = stored.name if stored else None
        except engine.WorkflowError:
            # The node will raise a clearer error if it needs the payload.
            ctx["trigger"] = None
    return ctx
