"""Schedule trigger: one daemon thread that launches due workflows.

A workflow is due when it is enabled, has an interval and its last run is
older than that interval (or it never ran). A workflow never overlaps with
itself. The thread checks the module's enabled state every tick, so
disabling the module stops scheduling immediately. ``_now`` is injectable
for tests.
"""

import threading
from datetime import UTC, datetime, timedelta

from app.extensions import db

from . import runtime
from .models import AutomationWorkflow

_TICK_SECONDS = 30
_started = False
_lock = threading.Lock()


def _now():
    return datetime.now(UTC)


def due_workflows(now=None):
    """Workflows whose schedule trigger should fire at ``now``."""
    now = now or _now()
    candidates = (AutomationWorkflow.query
                  .filter(AutomationWorkflow.enabled.is_(True),
                          AutomationWorkflow.interval_minutes.isnot(None))
                  .all())
    due = []
    for wf in candidates:
        if wf.interval_minutes <= 0:
            continue
        if wf.last_run_at is not None:
            last = wf.last_run_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if now - last < timedelta(minutes=wf.interval_minutes):
                continue
        if runtime.has_active_run(wf.id):
            continue
        due.append(wf)
    return due


def tick(app):
    """One scheduler pass: launch every due workflow."""
    import random
    with app.app_context():
        from app.services import module_service
        if not module_service.is_enabled("automation"):
            return 0
        launched = 0
        for wf in due_workflows():
            try:
                # Skip broken graphs instead of spamming error runs.
                from . import engine as _engine
                from .registry import active_node_types as _types
                try:
                    graph = _engine.parse_graph(wf.graph)
                    if not graph["nodes"]:
                        continue
                    _engine.validate_graph(graph, _types())
                except _engine.WorkflowError as exc:
                    app.logger.warning("Automation schedule skipped broken "
                                       "workflow %s: %s", wf.id, exc)
                    continue
                from app.models import User
                user = db.session.get(User, wf.user_id)
                if user is None:
                    continue
                # Small jitter so all due workflows don't fire at once.
                if random.random() < 0.05:
                    import time as _t
                    _t.sleep(random.uniform(0, 2))
                runtime.start_run(wf, user, trigger="schedule", app=app)
                launched += 1
            except Exception:  # noqa: BLE001 - one workflow must not kill tick
                app.logger.exception("Automation schedule failed for %s", wf.id)
        return launched


def _loop(app):
    import time
    while True:
        time.sleep(_TICK_SECONDS)
        try:
            tick(app)
        except Exception:  # noqa: BLE001 - the scheduler must never die
            app.logger.exception("Automation scheduler tick failed")


def start(app):
    """Start the scheduler thread once per process (idempotent)."""
    global _started
    if app.config.get("AUTOMATION_SCHEDULER", True) is False:
        return
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(app,), daemon=True).start()
