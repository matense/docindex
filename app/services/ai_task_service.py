"""Background AI task runs.

Every chat message runs as a task: the user sends, the agent runs in a
server worker thread, and the chat window subscribes to the live event
stream. Minimizing or closing the window only drops the subscription —
the run continues until it finishes or the user stops it. Every event is
persisted as a ChatMessage in the run's conversation as it happens, so
opening the conversation shows live progress at any time.

Rate limiting: the worker runs the agent loop with block=True, so when the
connection's requests/minute limit is reached it sleeps for a slot instead
of failing — long runs throttle themselves.
"""

import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor as _Pool

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import OperationalError

from ..extensions import db
from ..models import AIConnection, AITask, ChatConversation, ChatMessage, Drive, User, utcnow
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
# task_id -> {"steps": int, "current": str|None}: live progress for the dock
# widget (the currently running tool is only known while the worker runs it;
# finished/step counts always survive in the persisted transcript).
_live = {}
# task_id -> list[queue.Queue]: live event subscribers (chat windows watching
# the task's stream endpoint). Events are NDJSON-shaped dicts; None is the
# end-of-stream sentinel.
_subscribers = {}
_lock = threading.Lock()

# Global bound: raw threads per task would let 100 users x tasks exhaust
# the process. A single shared pool caps total concurrency; the per-user
# cap in start_task() still applies first.
_task_pool = _Pool(max_workers=8, thread_name_prefix="ai-task")


def subscribe(task_id):
    """Register a live-event subscriber; returns its queue."""
    q = queue.Queue()
    with _lock:
        _subscribers.setdefault(task_id, []).append(q)
    return q


def unsubscribe(task_id, q):
    with _lock:
        subs = _subscribers.get(task_id)
        if subs and q in subs:
            subs.remove(q)
        if subs == []:
            _subscribers.pop(task_id, None)


def _broadcast(task_id, event):
    with _lock:
        subs = list(_subscribers.get(task_id, ()))
    for q in subs:
        q.put(event)


# Extended budget for per-message "long horizon" runs: the run gets at
# least this many steps (or the connection budget when that is higher).
DEEP_RUN_MAX_STEPS = 64


def resolve_max_steps(user, app):
    """Single step budget for every run: the connection's max_steps,
    clamped to 1..128 (same rule as the agent loop fallback)."""
    try:
        cfg = ai_service.config_for(user) or {}
    except Exception:  # noqa: BLE001 - misconfigured connection still runs
        cfg = {}
    steps = cfg.get("max_steps") or app.config.get("AI_MAX_STEPS", 16)
    try:
        steps = int(steps)
    except (TypeError, ValueError):
        steps = 16
    return max(1, min(steps, 128))


def resolve_deep_max_steps(user, app):
    """Extended budget for per-message long-horizon runs: the connection's
    long_horizon_steps (default 64), never below the normal budget."""
    try:
        conn = AIConnection.query.filter_by(
            user_id=user.id, is_active=True).first()
        deep = (conn.long_horizon_steps if conn else None) or DEEP_RUN_MAX_STEPS
    except Exception:  # noqa: BLE001 - misconfigured connection still runs
        deep = DEEP_RUN_MAX_STEPS
    try:
        deep = int(deep)
    except (TypeError, ValueError):
        deep = DEEP_RUN_MAX_STEPS
    normal = resolve_max_steps(user, app)
    return max(1, min(max(normal, deep), 256))


def start_task(user, message, ctx=None, app=None, conversation_id=None,
               max_steps=None):
    """Create the task row and launch the agent worker.

    ``conversation_id`` continues an existing conversation — the run keeps
    working with the drives/files/cell it was launched with even after the
    user navigates elsewhere. ``max_steps`` overrides the resolved
    per-connection budget (stored on the row for transparency).

    Runs in a daemon thread unless AI_TASKS_ASYNC is off (tests), in which
    case the worker runs inline and the returned task is already finished.
    """
    message = (message or "").strip()
    if not message:
        raise ValueError("Empty message.")
    if len(message) > 20000:
        raise ValueError("Message is too long (max 20000 characters).")
    # Per-user concurrency cap: unbounded threads + AI spend otherwise.
    active = (AITask.query.filter_by(user_id=user.id)
              .filter(AITask.status.in_(("queued", "running"))).count())
    if active >= 3:
        raise ValueError("You already have 3 AI runs going — "
                         "stop or wait for one to finish first.")

    conv = None
    if conversation_id is not None:
        conv = ChatConversation.query.filter_by(
            id=conversation_id, user_id=user.id).first()
        if conv is None:
            raise ValueError("Conversation not found.")
    if conv is None:
        conv = ChatConversation(user_id=user.id, title=message[:80])
        db.session.add(conv)
        db.session.flush()

    display = message
    if ctx and ctx["context"].get("file_names"):
        display += "\n\n📎 " + ", ".join(ctx["context"]["file_names"])

    db.session.add(ChatMessage(conversation_id=conv.id, role="user",
                               content=display))
    app = app or current_app._get_current_object()
    if max_steps is None:
        max_steps = resolve_max_steps(user, app)
    task = AITask(user_id=user.id, conversation_id=conv.id,
                  title=message[:80], prompt=message, status="queued",
                  max_steps=max_steps,
                  context=(json.dumps(ctx["context"]) if ctx else None))
    db.session.add(task)
    db.session.commit()

    event = threading.Event()
    with _lock:
        _stop_events[task.id] = event

    if app.config.get("AI_TASKS_ASYNC", True):
        try:
            _task_pool.submit(_task_worker, task.id, app)
        except RuntimeError:
            # Pool saturated/shutting down: run inline so the task completes.
            _task_worker(task.id, app)
    else:
        _task_worker(task.id, app)
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
    """Runs shown in the dock: running/queued, plus finished ones the user
    has not dismissed yet (the notification badge state). One icon per
    conversation — the latest run — so a long chat never piles up icons;
    opening it always shows the full shared transcript. Capped to avoid
    unbounded dock payloads."""
    tasks = (AITask.query
             .filter(AITask.user_id == user.id)
             .filter(db.or_(AITask.status.in_(("queued", "running")),
                            AITask.notified.is_(False)))
             .order_by(AITask.created_at.desc(), AITask.id.desc()).limit(60).all())
    # One icon per conversation: keep only each conversation's latest run.
    # (Ordered newest-first above, so the first row per conversation wins;
    # notified-finished runs never reach here — the filter drops them.)
    seen_convs = set()
    docked = []
    for t in tasks:
        if t.conversation_id not in seen_convs:
            seen_convs.add(t.conversation_id)
            docked.append(t)
    tasks = docked[:20]
    # One aggregate query (not per task) for the persisted step counts the
    # dock progress line is derived from when no worker is live.
    conv_ids = [t.conversation_id for t in tasks]
    counts = (dict(db.session.query(ChatMessage.conversation_id,
                                    func.count(ChatMessage.id))
                   .filter(ChatMessage.conversation_id.in_(conv_ids),
                           ChatMessage.role == "step")
                   .group_by(ChatMessage.conversation_id).all())
              if conv_ids else {})
    # Queue position among ALL queued tasks (the pool is shared): a queued
    # task shows "#N in line" instead of a bare "Queued".
    queue_order = {}
    queued_all = (AITask.query.filter_by(status="queued")
                  .order_by(AITask.created_at, AITask.id).all())
    for i, qt in enumerate(queued_all):
        queue_order[qt.id] = i + 1
    for t in tasks:
        t._step_count = counts.get(t.conversation_id, 0)
        t._queue_pos = queue_order.get(t.id)
    return tasks


def task_progress(task):
    """Dock-widget progress: steps done + currently running tool (if live).

    While the worker runs, progress comes from the in-memory live map
    (updated on every tool event); otherwise it is derived from the
    persisted transcript, so restarts and finished tasks still report.
    """
    with _lock:
        live = _live.get(task.id)
        if live is not None:
            return {"steps": live["steps"], "current": live["current"]}
    n = getattr(task, "_step_count", None)
    if n is None:
        n = (ChatMessage.query
             .filter_by(conversation_id=task.conversation_id,
                        role="step").count())
    return {"steps": n, "current": None}


def _live_update(task_id, **fields):
    with _lock:
        state = _live.setdefault(task_id, {"steps": 0, "current": None})
        state.update(fields)


def recover_interrupted(app):
    """On startup, tasks still queued/running belong to a dead process."""
    with app.app_context():
        try:
            stale = AITask.query.filter(AITask.status.in_(("queued", "running"))).all()
        except OperationalError as exc:
            # Fresh database before migrations: ai_tasks does not exist yet.
            if "no such table" in str(exc).lower():
                db.session.rollback()
                app.logger.info("AI task recovery skipped (tables not "
                                "created yet).")
                return
            raise
        for task in stale:
            task.status = "interrupted"
            task.error = "The server restarted while this task was running."
            task.finished_at = utcnow()
        if stale:
            db.session.commit()
            app.logger.info("Marked %d interrupted AI task(s).", len(stale))


def _owned_task(user, task_id):
    return AITask.query.filter_by(id=task_id, user_id=user.id).first()


def _task_worker(task_id, app):
    with app.app_context():
        try:
            _run_task(task_id, app)
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
            # Close any live stream watchers (None = end-of-stream sentinel).
            task = db.session.get(AITask, task_id)
            if task is not None:
                _broadcast(task_id, {"type": "task_status", "status": task.status})
            _broadcast(task_id, None)
            with _lock:
                _stop_events.pop(task_id, None)
                _subscribers.pop(task_id, None)
                _live.pop(task_id, None)


def _friendly_error(exc):
    """Translate cryptic provider failures into actionable user text."""
    msg = str(exc)
    low = msg.lower()
    if ("context length" in low or "maximum context" in low
            or "context window" in low or "context size" in low
            or "exceed_context_size" in low):
        # The prompt (history + tool results) exceeded the model's
        # context — the raw provider error is cryptic for users.
        return ("This conversation is too large for the model's "
                "context window. Use the Summarize & reset button to "
                "compact it, start a new conversation, or raise the "
                "prompt budget in AI Settings.")
    if getattr(exc, "status_code", None) == 404:
        # The model/endpoint does not exist on the provider. The
        # connection itself is fine — the configured model is not.
        return ("Your AI connection is configured, but the server "
                "could not find the configured model (HTTP 404). "
                "The model may not be available on the server, or the "
                "base URL may be incorrect. Go to Settings → AI, "
                "check the base URL and model (use the Test button to "
                "see the available models), and try again.")
    return msg


def _run_task(task_id, app):
    task = db.session.get(AITask, task_id)
    if task is None or task.status != "queued":
        return  # stopped before it started
    user = db.session.get(User, task.user_id)

    # The captured context decides the drive scope and seeds the system
    # notes — the task remembers what it was launched with, regardless of
    # what the user is looking at now.
    snapshot = {}
    if task.context:
        try:
            snapshot = json.loads(task.context)
        except ValueError:
            snapshot = {}
    drive_ids = snapshot.get("drive_ids")
    drive = (Drive.query
             .filter(Drive.id.in_(drive_ids), Drive.user_id == user.id)
             .all()) if drive_ids else None

    task.status = "running"
    task.started_at = utcnow()
    db.session.commit()

    with _lock:
        event = _stop_events.get(task_id)
    should_stop = event.is_set if event is not None else None

    model_name = ai_service.config_for(user).get("model", "")
    # Single budget: the task row carries the resolved per-connection
    # max_steps (legacy NULL rows fall back to resolving it now).
    max_steps = task.max_steps or resolve_max_steps(user, app)
    # The question that launched THIS task — never the conversation's first
    # message (one conversation can now own several sequential tasks).
    if task.prompt:
        user_text = task.prompt
    else:
        # Pre-prompt tasks: fall back to the latest user message (it may
        # carry the rendered 📎 attachment suffix — strip it).
        last_user = next((m for m in reversed(task.conversation.messages)
                          if m.role == "user"), None)
        user_text = last_user.content if last_user else task.title
        marker = "\n\n📎"
        if marker in user_text:
            user_text = user_text.split(marker)[0].rstrip()
    notes = snapshot.get("notes") or []
    # Recent transcript so follow-ups ("it", "that run", ...) resolve — the
    # launching turn is re-appended below as the final message. Same window
    # rule as interactive chats (per-connection history_messages).
    hist_n = ai_service.config_for(user).get("history_messages") or 20
    if hist_n <= 0:
        hist_n = 20
    transcript = [{"role": m.role, "content": m.content}
                  for m in task.conversation.messages
                  if m.role in ("user", "assistant") and not m.archived][-hist_n:]
    if (transcript and transcript[-1]["role"] == "user"
            and (transcript[-1]["content"] == user_text
                 or transcript[-1]["content"].startswith(user_text + "\n\n📎"))):
        transcript.pop()
    # Some providers reject a history that does not begin with a user turn —
    # move leading assistant messages into the system context instead.
    preamble = []
    while transcript and transcript[0]["role"] == "assistant":
        preamble.append(transcript.pop(0)["content"])
    if preamble:
        notes = (["Summary of the earlier conversation:\n\n"
                  + "\n\n".join(preamble)] + notes)
    history = (
        [{"role": "system", "content": _TASK_NOTE}]
        + [{"role": "system", "content": n} for n in notes]
        + transcript
        + [{"role": "user", "content": user_text}]
    )

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
        # Same NDJSON shape as the interactive /ai/chat stream, so a chat
        # window watching this task renders it exactly like a normal chat.
        block_tokens = None  # None | "thinking" | "answer" (migrate logic)
        for kind, payload in events:
            ev = None
            if kind == "thinking_token":
                block_tokens = "thinking"
                ev = {"type": "thinking_token", "content": payload}
            elif kind == "answer_token":
                block_tokens = "answer"
                ev = {"type": "answer_token", "content": payload}
            elif kind == "thinking":
                persist("thinking", payload)
                if block_tokens != "thinking":
                    ev = {"type": "thinking", "content": payload}
                    if block_tokens == "answer":
                        ev["migrate"] = True
                block_tokens = None
            elif kind == "tool_start":
                # Broadcast before the tool runs: a chat window watching this
                # task animates a live "working" chip. Nothing to persist yet.
                _live_update(task_id, current=payload.get("label"))
                ev = {"type": "tool_start", "step": payload}
            elif kind == "step":
                block_tokens = None
                flush_step()  # a step without result (stay sane)
                pending_step = payload
                _live_update(task_id,
                             steps=_live.get(task_id, {}).get("steps", 0) + 1,
                             current=f"{payload.get('label')}: "
                                     f"{payload.get('detail')}")
                ev = {"type": "step", "step": payload}
            elif kind == "tool_result":
                flush_step(payload.get("summary"))
                _live_update(task_id, current=None)
                ev = {"type": "tool_result", "result": payload}
            elif kind == "ui_action":
                # Browser-side signal (e.g. automation canvas refresh) —
                # broadcast live, but not persisted as a chat message.
                ev = {"type": "ui_action", "action": payload}
            elif kind == "notice":
                # Automatic context compaction — visible in the transcript.
                persist("notice", payload)
                ev = {"type": "notice", "content": payload}
            elif kind == "stopped":
                block_tokens = None
                stopped = True
                flush_step()
                persist("assistant", "⏹ " + payload, model=model_name)
                ev = {"type": "answer", "answer": "⏹ " + payload,
                      "conversation_id": conv_id, "model": model_name}
            else:  # answer
                block_tokens = None
                answer = payload
                flush_step()
                persist("assistant", answer, model=model_name)
                ev = {"type": "answer", "answer": answer,
                      "conversation_id": conv_id, "model": model_name}
            if ev is not None:
                _broadcast(task_id, ev)
        flush_step()
    except ai_service.AIError as exc:
        msg = _friendly_error(exc)
        log_service.log_event("error", "ai_task", msg,
                              user_id=user.id, path=f"/ai/tasks/{task_id}")
        persist("assistant", f"⚠ {msg}", model=model_name)
        _broadcast(task_id, {"type": "error", "error": msg})
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
