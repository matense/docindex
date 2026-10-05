"""Schedule trigger: one daemon thread that launches due workflows.

A workflow is due when it is enabled, has an interval and its last run is
older than that interval (or it never ran). A workflow never overlaps with
itself. The thread checks the module's enabled state every tick, so
disabling the module stops scheduling immediately. ``_now`` is injectable
for tests.
"""

import threading
from datetime import datetime, timedelta, timezone

from app.extensions import db

from .models import AutomationWorkflow
from . import runtime

_TICK_SECONDS = 30
_started = False
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc)


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
                last = last.replace(tzinfo=timezone.utc)
            if now - last < timedelta(minutes=wf.interval_minutes):
                continue
        if runtime.has_active_run(wf.id):
            continue
        due.append(wf)
    return due


def tick(app):
    """One scheduler pass: launch every due workflow."""
    with app.app_context():
        from app.services import module_service
        if not module_service.is_enabled("automation"):
            return 0
        launched = 0
        for wf in due_workflows():
            from app.models import User
            user = db.session.get(User, wf.user_id)
            if user is None:
                continue
            runtime.start_run(wf, user, trigger="schedule", app=app)
            launched += 1
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
