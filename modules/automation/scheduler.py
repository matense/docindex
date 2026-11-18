"""Schedule trigger: one daemon thread that launches due workflows.

A workflow is due when it is enabled, has an interval and its last run is
older than that interval (or it never ran). A workflow never overlaps with
itself. Workflows with a ``trigger_event`` rule instead fire on pending
``file.uploaded``/``file.synced`` bus events drained each tick. The thread
checks the module's enabled state every tick, so disabling the module
stops scheduling immediately. ``_now`` is injectable for tests.
"""

import fnmatch
import threading
from collections import deque
from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.services import events

from . import runtime
from .models import AutomationWorkflow

_TICK_SECONDS = 30
_started = False
_lock = threading.Lock()

# Event-trigger queue: the bus subscribers below append (event_name, payload
# snapshot) here; tick() drains at most _MAX_EVENTS_PER_TICK per pass and the
# overflow carries over to the next tick (a warning is logged). Payloads are
# treated as read-only — a shallow copy is queued so later mutation by the
# emitter can never affect matching.
_MAX_EVENTS_PER_TICK = 100
_PENDING = deque()
_pending_lock = threading.Lock()


def _now():
    return datetime.now(UTC)


def _enqueue_event(event_name, payload):
    """Bus subscriber: remember one event for the next tick (read-only)."""
    try:
        snapshot = dict(payload) if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001 - never break the emitter
        snapshot = {}
    with _pending_lock:
        _PENDING.append((event_name, snapshot))


def _on_file_uploaded(payload):
    _enqueue_event("file.uploaded", payload)


def _on_file_synced(payload):
    _enqueue_event("file.synced", payload)


events.subscribe("file.uploaded", _on_file_uploaded)
events.subscribe("file.synced", _on_file_synced)


def _drain_pending():
    """Take up to _MAX_EVENTS_PER_TICK queued events; report the leftover."""
    with _pending_lock:
        batch = []
        while _PENDING and len(batch) < _MAX_EVENTS_PER_TICK:
            batch.append(_PENDING.popleft())
        leftover = len(_PENDING)
    return batch, leftover


def _expand_event_candidates(batch):
    """Flatten one tick's events into per-file candidates (read-only).

    file.uploaded carries a single file; file.synced carries ``added`` ids.
    Events without a user scope are ignored — rules only match within the
    event's own user scope.
    """
    for event_name, payload in batch:
        user_id = payload.get("user_id")
        if user_id is None:
            continue
        if event_name == "file.synced":
            added = payload.get("added") or []
            if not isinstance(added, list):
                continue
            for file_id in added:
                yield {"file_id": file_id,
                       "drive_id": payload.get("drive_id"),
                       "user_id": user_id}
        elif payload.get("file_id") is not None:
            yield {"file_id": payload.get("file_id"),
                   "drive_id": payload.get("drive_id"),
                   "user_id": user_id}


def _folder_path(folder):
    """Slash-joined path of a file's folder ("" at the drive root)."""
    if folder is None:
        return ""
    try:
        return "/".join(node.name for node in folder.breadcrumb())
    except Exception:  # noqa: BLE001 - a broken tree must not kill tick
        return ""


def _rule_matches(rule, drive_id, folder_path, folder_name, filename):
    """True when a file matches an event rule (missing keys match all)."""
    if not isinstance(rule, dict):
        return False
    want_drive = rule.get("drive_id")
    if want_drive not in (None, ""):
        try:
            if int(want_drive) != int(drive_id):
                return False
        except (TypeError, ValueError):
            if str(want_drive) != str(drive_id):
                return False
    want_folder = rule.get("folder")
    if want_folder not in (None, ""):
        want = str(want_folder)
        if not (folder_path == want
                or folder_path.startswith(want + "/")
                or folder_name == want):
            return False
    want_pattern = rule.get("pattern")
    if want_pattern not in (None, ""):
        if not fnmatch.fnmatchcase(filename, str(want_pattern)):
            return False
    return True


def _tick_events(app):
    """Launch enabled event-rule workflows matching pending bus events."""
    batch, leftover = _drain_pending()
    if leftover:
        app.logger.warning("Automation event queue overflow: %d event(s) "
                           "carried over to the next tick", leftover)
    if not batch:
        return 0
    candidates = list(_expand_event_candidates(batch))
    if not candidates:
        return 0
    import json as _json
    rules = []
    for wf in (AutomationWorkflow.query
               .filter(AutomationWorkflow.enabled.is_(True),
                       AutomationWorkflow.trigger_event.isnot(None)).all()):
        if not wf.trigger_event:
            continue
        try:
            rule = _json.loads(wf.trigger_event)
        except ValueError:
            app.logger.warning("Automation event rule skipped (bad JSON) "
                               "for workflow %s", wf.id)
            continue
        if not isinstance(rule, dict):
            continue
        rules.append((wf, rule))
    if not rules:
        return 0
    from app.models import StoredFile, User
    launched = 0
    started = set()  # at most one event run per workflow per tick
    for candidate in candidates:
        try:
            stored = db.session.get(StoredFile, candidate["file_id"])
        except Exception:  # noqa: BLE001 - one bad event, not the tick
            continue
        if stored is None or stored.deleted_at is not None:
            continue
        filename = stored.name or ""
        folder = stored.folder
        folder_path = _folder_path(folder)
        folder_name = folder.name if folder is not None else ""
        drive_id = (stored.drive_id if stored.drive_id is not None
                    else candidate["drive_id"])
        for wf, rule in rules:
            if wf.user_id != candidate["user_id"]:
                continue
            try:
                if not _rule_matches(rule, drive_id, folder_path,
                                     folder_name, filename):
                    continue
                if wf.id in started or runtime.has_active_run(wf.id):
                    continue
                # Skip broken graphs instead of spamming error runs.
                from . import engine as _engine
                from .registry import active_node_types as _types
                try:
                    graph = _engine.parse_graph(wf.graph)
                    if not graph["nodes"]:
                        continue
                    _engine.validate_graph(graph, _types())
                except _engine.WorkflowError as exc:
                    app.logger.warning("Automation event skipped broken "
                                       "workflow %s: %s", wf.id, exc)
                    continue
                user = db.session.get(User, wf.user_id)
                if user is None:
                    continue
                # Frozen run-form answers, replayed like the schedule path.
                profile = None
                if wf.schedule_params:
                    try:
                        profile = _json.loads(wf.schedule_params) or None
                    except ValueError:
                        profile = None
                # Atomic check-and-start: a concurrent tick may have
                # launched this workflow after the check above.
                with _lock:
                    if runtime.has_active_run(wf.id):
                        continue
                    runtime.start_run(wf, user, trigger="event", app=app,
                                      trigger_file_id=stored.id,
                                      params=profile)
                started.add(wf.id)
                launched += 1
            except Exception:  # noqa: BLE001 - one workflow must not kill tick
                app.logger.exception("Automation event run failed for %s",
                                     wf.id)
    return launched


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
            # Stay bounded while disabled: drop queued events instead of
            # letting them pile up with nobody to consume them.
            with _pending_lock:
                _PENDING.clear()
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
                # Schedule profile: frozen run-form answers replayed every
                # tick (validated when the schedule was configured). A
                # corrupt profile degrades to static fallbacks, never a crash.
                import json as _json
                profile = None
                if wf.schedule_params:
                    try:
                        profile = _json.loads(wf.schedule_params) or None
                    except ValueError:
                        profile = None
                # Small jitter so all due workflows don't fire at once.
                if random.random() < 0.05:
                    import time as _t
                    _t.sleep(random.uniform(0, 2))
                # Atomic check-and-start: a concurrent tick may have
                # launched this workflow after due_workflows() checked.
                with _lock:
                    if runtime.has_active_run(wf.id):
                        continue
                    runtime.start_run(wf, user, trigger="schedule", app=app,
                                      trigger_file_id=wf.schedule_file_id,
                                      params=profile)
                launched += 1
            except Exception:  # noqa: BLE001 - one workflow must not kill tick
                app.logger.exception("Automation schedule failed for %s", wf.id)
        launched += _tick_events(app)
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
