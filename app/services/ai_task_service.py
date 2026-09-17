"""Long-horizon background AI tasks.

A task is an agent run decoupled from the HTTP request: the user fires it
from the chat ("run in background") and keeps working — the bottom dock
shows one icon per task and a notification badge when it finishes.

Unlike sync/hashtag jobs (in-memory only), tasks are rows in the ai_tasks
table, so they survive a page refresh and are recoverable on restart
(running/queued tasks left behind are marked "interrupted"). Every agent
event is persisted as a ChatMessage in the task's own conversation as it
happens, so opening the conversation shows live progress.

Rate limiting: the worker runs the agent loop with block=True, so when the
connection's requests/minute limit is reached it sleeps for a slot instead
of failing — long runs throttle themselves.
"""

import threading

from flask import current_app

from ..extensions import db
from ..models import AITask, ChatConversation, ChatMessage, Drive, User, utcnow
from . import agent_service, ai_service, log_service

_TASK_NOTE = (
    "This is a long-horizon background task: the user is not watching live "
    "and expects a complete, self-contained result. Work autonomously across "
    "as many steps as you need — break the work into small pieces, verify "
    "each result before moving on, and never stop halfway to ask questions. "
    "End with a clear final answer summarizing everything you did or found."
)

# task_id -> threading.Event, set when the user asks to stop the task.
_stop_events = {}
_lock = threading.Lock()


def start_task(user, message, drive_id=None, app=None):
    """Create the conversation + task row and launch the agent worker.

    Runs in a daemon thread unless AI_TASKS_ASYNC is off (tests), in which
    case the worker runs inline and the returned task is already finished.
    """
    message = (message or "").strip()
    if not message:
        raise ValueError("Empty message.")

    conv = ChatConversation(user_id=user.id, title=message[:80])
    db.session.add(conv)
    db.session.flush()
    db.session.add(ChatMessage(conversation_id=conv.id, role="user",
                               content=message))
    task = AITask(user_id=user.id, conversation_id=conv.id,
                  title=message[:80], status="queued")
    db.session.add(task)
    db.session.commit()

    event = threading.Event()
    with _lock:
        _stop_events[task.id] = event

    app = app or current_app._get_current_object()
    if app.config.get("AI_TASKS_ASYNC", True):
        threading.Thread(target=_task_worker,
                         args=(task.id, drive_id, app), daemon=True).start()
    else:
        _task_worker(task.id, drive_id, app)
    return task


def stop_task(user, task_id):
    """Cooperative stop: the agent loop checks the event between steps."""
    task = _owned_task(user, task_id)
    if task is None or not task.is_active:
        return None
    with _lock:
        event = _stop_events.get(task.id)
    if event is not None:
        event.set()
    if task.status == "queued":
        # Never started (or about to): finish it right away.
        task.status = "stopped"
        task.finished_at = utcnow()
        db.session.commit()
    return task


def ack_task(user, task_id):
    """Dismiss a finished task from the dock (the 'end' of the long horizon)."""
    task = _owned_task(user, task_id)
    if task is None or task.is_active:
        return None
    task.notified = True
    db.session.commit()
    return task


def get_task(user, task_id):
    return _owned_task(user, task_id)


def active_tasks(user):
    """Tasks shown in the dock: running/queued, plus finished ones the user
    has not dismissed yet (the notification badge state)."""
    return (AITask.query
            .filter(AITask.user_id == user.id)
            .filter(db.or_(AITask.status.in_(("queued", "running")),
                           AITask.notified.is_(False)))
            .order_by(AITask.created_at).all())


def recover_interrupted(app):
    """On startup, tasks still queued/running belong to a dead process."""
    with app.app_context():
        stale = AITask.query.filter(AITask.status.in_(("queued", "running"))).all()
        for task in stale:
            task.status = "interrupted"
            task.error = "The server restarted while this task was running."
            task.finished_at = utcnow()
        if stale:
            db.session.commit()
            app.logger.info("Marked %d interrupted AI task(s).", len(stale))


def _owned_task(user, task_id):
    return AITask.query.filter_by(id=task_id, user_id=user.id).first()


def _task_worker(task_id, drive_id, app):
    with app.app_context():
        try:
            _run_task(task_id, drive_id, app)
        except Exception as exc:  # noqa: BLE001 - the task must never die silently
            db.session.rollback()
            app.logger.exception("AI task %s failed", task_id)
            task = db.session.get(AITask, task_id)
            if task is not None and task.is_active:
                task.status = "error"
                task.error = str(exc)[:500]
                task.finished_at = utcnow()
                db.session.commit()
        finally:
            with _lock:
                _stop_events.pop(task_id, None)


def _run_task(task_id, drive_id, app):
    task = db.session.get(AITask, task_id)
    if task is None or task.status != "queued":
        return  # stopped before it started
    user = db.session.get(User, task.user_id)
    drive = db.session.get(Drive, drive_id) if drive_id else None

    task.status = "running"
    task.started_at = utcnow()
    db.session.commit()

    with _lock:
        event = _stop_events.get(task_id)
    should_stop = event.is_set if event is not None else None

    model_name = ai_service.config_for(user).get("model", "")
    max_steps = app.config.get("AI_TASK_MAX_STEPS", 64)
    history = [
        {"role": "system", "content": _TASK_NOTE},
        {"role": "user", "content": task.title},
    ]

    conv_id = task.conversation_id
    pending_step = None  # {"label", "detail"} waiting for its tool_result
    answer = None
    stopped = False

    def persist(role, content, model=None):
        db.session.add(ChatMessage(conversation_id=conv_id, role=role,
                                   content=content, model=model))
        db.session.commit()  # live progress: visible if the user opens the chat

    def flush_step(summary=None):
        nonlocal pending_step
        if pending_step is None:
            return
        text = f"{pending_step['label']}: {pending_step['detail']}"
        if summary:
            text += f" → {summary}"
        persist("step", text)
        pending_step = None

    try:
        events = agent_service.run_agent_events(
            user, history, drive=drive, max_steps=max_steps,
            should_stop=should_stop, block=True)
        for kind, payload in events:
            if kind in ("thinking_token", "answer_token"):
                continue  # live-only deltas; the full blocks follow
            if kind == "thinking":
                persist("thinking", payload)
            elif kind == "step":
                flush_step()  # a step without result (shouldn't happen, but stay sane)
                pending_step = payload
            elif kind == "tool_result":
                flush_step(payload.get("summary"))
            elif kind == "stopped":
                stopped = True
                flush_step()
                persist("assistant", "⏹ " + payload, model=model_name)
            else:  # answer
                answer = payload
                flush_step()
                persist("assistant", answer, model=model_name)
        flush_step()
    except ai_service.AIError as exc:
        msg = str(exc)
        log_service.log_event("error", "ai_task", msg,
                              user_id=user.id, path=f"/ai/tasks/{task_id}")
        persist("assistant", f"⚠ {msg}", model=model_name)
        task.status = "error"
        task.error = msg[:500]
        task.finished_at = utcnow()
        db.session.commit()
        return

    if stopped:
        task.status = "stopped"
    elif answer is not None or task.status == "running":
        task.status = "done"
    task.finished_at = utcnow()
    db.session.commit()
