import json
import traceback

from flask import (Blueprint, Response, current_app, jsonify, request,
                   stream_with_context)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import AIConnection, AITask, ChatConversation, ChatMessage, Drive, StoredFile, User
from ..services import agent_service, ai_service, ai_task_service, drive_service, log_service

bp = Blueprint("ai", __name__, url_prefix="/ai")


def _get_conversation(conv_id):
    conv = db.session.get(ChatConversation, conv_id) if conv_id else None
    if conv and conv.user_id == current_user.id:
        return conv
    return None


def _resolve_context_cell(ref, user_id):
    """Resolve a notebook cell dragged into the chat ({"file_id", "cell_id"})
    into (system_note, snapshot_info), or (None, None) when invalid or not
    applicable. snapshot_info feeds the AITask context snapshot."""
    if not isinstance(ref, dict):
        return None, None
    try:
        file_id = int(ref.get("file_id") or 0)
    except (TypeError, ValueError):
        return None, None
    cell_id = str(ref.get("cell_id") or "")
    if not file_id or not cell_id:
        return None, None
    stored = (StoredFile.query
              .filter_by(id=file_id, user_id=user_id,
                         extension="pdocnb")
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if not stored:
        return None, None
    from ..services import file_service, module_service
    if not module_service.is_enabled("notebooks"):
        return None, None
    try:
        doc = json.loads(file_service.read_text_content(stored,
                                                        max_chars=500_000))
    except (OSError, ValueError):
        return None, None
    cell = next((c for c in doc.get("cells", [])
                 if c.get("id") == cell_id), None)
    if cell is None:
        return None, None
    if doc.get("ai_hidden"):
        return None, None  # hidden notebooks stay invisible to the AI
    preview = (cell.get("content") or "")[:200]
    note = (
        f"The user dragged a cell from the notebook [id {stored.id}] "
        f"{stored.name} into the chat: cell id '{cell_id}' (type "
        f"'{cell.get('type')}'). When they say \"this cell\", they mean that "
        f"one — read it with notebooks.read and change it with "
        f"notebooks.update_cell using that cell_id. Cell content preview: "
        f"{preview!r}")
    if doc.get("ai_lock"):
        note += (" NOTE: this notebook is currently LOCKED against AI edits "
                 "by the user — do not generate or propose changes to it. "
                 "If they ask for changes, tell them the notebook is locked "
                 "and ask them to unlock it with the lock button in the "
                 "notebook toolbar, then repeat the request.")
    info = {"file_id": stored.id, "cell_id": cell_id,
            "file_name": stored.name}
    return note, info


def _notebook_access_note(stored):
    """Extra instruction appended to the open-file context note when the
    open notebook is locked or hidden for the AI (pdocnb only)."""
    if stored.extension != "pdocnb":
        return ""
    from ..services import file_service, module_service
    if not module_service.is_enabled("notebooks"):
        return ""
    try:
        doc = json.loads(file_service.read_text_content(stored,
                                                        max_chars=500_000))
    except (OSError, ValueError):
        return ""
    if doc.get("ai_hidden"):
        return (" This notebook is HIDDEN from the AI by the user: do not "
                "read or mention its content — if they ask about it, tell "
                "them it is hidden and can be unhidden with the eye button "
                "in the notebook toolbar.")
    if doc.get("ai_lock"):
        return (" This notebook is LOCKED against AI edits by the user: do "
                "not generate or propose edits to it. If they ask for "
                "changes, tell them the notebook is locked and ask them to "
                "unlock it with the lock button in the notebook toolbar, "
                "then repeat the request. Reading it and answering "
                "questions about it is fine.")
    return ""


def _resolve_context(user, data):
    """Resolve a chat/task request's context payload into a dict with:

    - ``notes``: system notes to prepend to the history, in final order;
    - ``attachments``: the attached StoredFile rows;
    - ``drives``: the scoped Drive rows, or None when all drives are in scope;
    - ``context``: a JSON-serializable snapshot of every reference, stored on
      AITask so a background task remembers what it was launched with.

    Drive scope precedence: explicit $ mentions (``scope_drive_ids`` /
    legacy ``scope_drive_id``) win; then ``scope_all_drives``; otherwise the
    drive the user is currently browsing.
    """
    notes = []
    snapshot = {"drive_ids": None, "drive_names": None, "all_drives": False,
                "file_ids": [], "file_names": [],
                "context_file_id": None, "context_file_name": None,
                "context_cell": None}

    # Drive scope — resolved first so its note ends up first in the history.
    scope_all_drives = bool(data.get("scope_all_drives"))
    raw_ids = data.get("scope_drive_ids")
    if isinstance(raw_ids, list):
        drive_ids = []
        for i in raw_ids:
            try:
                drive_ids.append(int(i))
            except (TypeError, ValueError):
                pass
    else:
        try:
            one = int(data.get("scope_drive_id") or 0)
        except (TypeError, ValueError):
            one = 0
        drive_ids = [one] if one else []
    drives = None
    if drive_ids:
        drives = (Drive.query
                  .filter(Drive.id.in_(drive_ids), Drive.user_id == user.id)
                  .all())
    elif not scope_all_drives:
        drives = [drive_service.get_current_drive(user)]
    if drives is None:
        snapshot["all_drives"] = True
        notes.append(
            "The user removed the drive scope: you can see files from ALL "
            "their drives, not just the one they are browsing. Use "
            "list_drives to discover what drives exist.")
    else:
        snapshot["drive_ids"] = [d.id for d in drives]
        snapshot["drive_names"] = [d.name for d in drives]

    # Cell context: a notebook cell the user dragged into the chat.
    cell_note, cell_info = _resolve_context_cell(data.get("context_cell"),
                                                 user.id)
    if cell_note:
        notes.append(cell_note)
        snapshot["context_cell"] = cell_info

    # Implicit context: the file/notebook the user currently has open, so
    # "improve this notebook" works without an explicit @here mention.
    attachment_ids = data.get("attachments") or []
    context_file = None
    try:
        context_file_id = int(data.get("context_file_id") or 0)
    except (TypeError, ValueError):
        context_file_id = 0
    if context_file_id and context_file_id not in [
            int(i) for i in attachment_ids if str(i).isdigit()]:
        context_file = (StoredFile.query
                        .filter_by(id=context_file_id, user_id=user.id)
                        .filter(StoredFile.deleted_at.is_(None))
                        .first())
    if context_file:
        kind = "notebook" if context_file.extension == "pdocnb" else "file"
        notes.append(
            f"The user currently has the {kind} "
            f"[id {context_file.id}] {context_file.name} open on screen. "
            "When they say things like \"this notebook\", \"this file\" or "
            "\"here\" without naming a target, they mean that one — you "
            "can read or edit it directly by id, no need to search first."
            + _notebook_access_note(context_file))
        snapshot["context_file_id"] = context_file.id
        snapshot["context_file_name"] = context_file.name

    # Explicitly attached files (@ mentions).
    attached = []
    if attachment_ids:
        attached = (StoredFile.query
                    .filter(StoredFile.id.in_(attachment_ids),
                            StoredFile.user_id == user.id,
                            StoredFile.deleted_at.is_(None))
                    .all())
    if attached:
        listing = ", ".join(f"[id {f.id}] {f.name}" for f in attached)
        notes.append(
            f"The user attached these files to this question: {listing}. "
            "You can read_file them directly by id — no need to search first.")
        snapshot["file_ids"] = [f.id for f in attached]
        snapshot["file_names"] = [f.name for f in attached]

    # The worker thread cannot rebuild the notes (no request context), so
    # the snapshot carries their final text.
    snapshot["notes"] = list(notes)
    return {"notes": notes, "attachments": attached, "drives": drives,
            "context": snapshot}


@bp.route("/conversations")
@login_required
def conversations():
    convs = (ChatConversation.query
             .filter_by(user_id=current_user.id)
             .order_by(ChatConversation.updated_at.desc()).all())
    return jsonify([
        {"id": c.id, "title": c.title,
         "updated_at": c.updated_at.isoformat() if c.updated_at else None,
         # The model shown is the one that wrote the latest assistant reply.
         "model": next((m.model for m in reversed(c.messages)
                        if m.role == "assistant" and m.model), None)}
        for c in convs
    ])


@bp.route("/conversations/<int:conv_id>")
@login_required
def conversation(conv_id):
    conv = _get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    # A conversation owned by a background task reopens in background mode
    # until the user dismisses the task (ack) — see ai_chat.js.
    task = AITask.query.filter_by(conversation_id=conv.id).first()
    return jsonify({
        "id": conv.id,
        "title": conv.title,
        # Latest context the user set up in this conversation (drives,
        # attachments, cell) — restored into the chips on open.
        "context": json.loads(conv.context) if conv.context else None,
        "task": ({"id": task.id, "status": task.status, "title": task.title,
                  "notified": bool(task.notified),
                  "context": (json.loads(task.context)
                              if task.context else None)}
                 if task and not task.notified else None),
        "messages": [
            {"role": m.role, "content": m.content, "model": m.model,
             "archived": bool(m.archived),
             "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in conv.messages
        ],
    })


@bp.route("/conversations/<int:conv_id>/delete", methods=["POST"])
@login_required
def delete_conversation(conv_id):
    conv = _get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(conv)
    db.session.commit()
    return jsonify({"ok": True})


# Marker prepended to the summary message left behind by the
# summarize-and-reset endpoint.
SUMMARY_PREFIX = (
    "📋 **Conversation summary** *(compacted memory — earlier messages "
    "were replaced by this summary)*:\n\n")

# Hard cap on the transcript sent to the model for summarization, so the
# summarization call itself cannot overflow the context window.
_SUMMARY_MAX_CHARS = 120_000


@bp.route("/conversations/<int:conv_id>/summarize", methods=["POST"])
@login_required
def summarize_conversation(conv_id):
    """AI-summarize the active conversation history, then archive it: old
    messages stay visible (grayed out in the UI) but are no longer sent to
    the model — a reset with memory, where the summary is the memory."""
    conv = _get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404

    msgs = [m for m in conv.messages
            if m.role in ("user", "assistant") and not m.archived]
    if len(msgs) < 4:
        return jsonify({"error": "Not enough history to summarize."}), 400

    config = ai_service.config_for(current_user)
    if not ai_service.is_enabled(current_user):
        return jsonify({"error": "AI is not configured."}), 503

    transcript = "\n\n".join(f"{m.role.upper()}: {m.content}" for m in msgs)
    if len(transcript) > _SUMMARY_MAX_CHARS:
        transcript = ("[earlier messages omitted]\n\n"
                      + transcript[-_SUMMARY_MAX_CHARS:])

    prompt = (
        "Summarize this conversation between a user and a file-search "
        "assistant. The summary replaces the full history, so it must keep "
        "everything needed to continue coherently: topics covered, concrete "
        "facts and answers found, files cited (keep their "
        "[name](file://ID) references), decisions made, and any open "
        "questions or pending requests. Be compact (max ~400 words), use "
        "short bullet points, and write in the same language as the "
        "conversation.\n\nCONVERSATION:\n" + transcript)
    try:
        reply = ai_service.chat_completion(
            [{"role": "user", "content": prompt}], config=config)
    except ai_service.AIError as exc:
        log_service.log_event("error", "ai_chat", f"Summarize failed: {exc}",
                              user_id=current_user.id, path="/ai/chat")
        return jsonify({"error": str(exc)}), 502

    summary = (reply.get("content") or "").strip()
    if not summary:
        return jsonify({"error": "The AI returned an empty summary."}), 502

    # Archive everything (including thinking/step rows of those exchanges)
    # instead of deleting — the user can still read the old conversation.
    for m in conv.messages:
        m.archived = True
    db.session.add(ChatMessage(conversation_id=conv.id, role="assistant",
                               content=SUMMARY_PREFIX + summary,
                               model=config.get("model") or None))
    db.session.commit()
    return jsonify({"ok": True, "summary": summary})


@bp.route("/connections")
@login_required
def connections():
    """The user's saved AI connections, for the chat's model switcher."""
    conns = (AIConnection.query
             .filter_by(user_id=current_user.id)
             .order_by(AIConnection.created_at).all())
    return jsonify([
        {"id": c.id, "name": c.name, "model": c.model, "is_active": c.is_active}
        for c in conns
    ])


@bp.route("/connections/<int:conn_id>/activate", methods=["POST"])
@login_required
def activate_connection(conn_id):
    """Switch the active connection from the chat's model switcher."""
    conn = db.session.get(AIConnection, conn_id)
    if not conn or conn.user_id != current_user.id:
        return jsonify({"error": "Not found"}), 404
    AIConnection.query.filter_by(user_id=current_user.id).update({"is_active": False})
    conn.is_active = True
    db.session.commit()
    return jsonify({"ok": True, "id": conn.id, "name": conn.name, "model": conn.model})


@bp.route("/chat", methods=["POST"])
@login_required
def chat():
    if not ai_service.is_enabled(current_user):
        return jsonify({
            "error": "AI is not configured. Add a connection in AI Settings "
                     "(profile menu or the gear icon here)."
        }), 503

    data = request.get_json(silent=True) or {}
    question = (data.get("message") or "").strip()
    attachment_ids = data.get("attachments") or []
    if not question and not attachment_ids:
        return jsonify({"error": "Empty message."}), 400

    ctx = _resolve_context(current_user, data)
    attached = ctx["attachments"]

    conv = _get_conversation(data.get("conversation_id"))
    if not conv:
        conv = ChatConversation(user_id=current_user.id,
                                title=(question or "Attachments")[:80])
        db.session.add(conv)
        db.session.flush()

    display_question = question or "What can you tell me about these files?"
    if attached:
        names = ", ".join(f.name for f in attached)
        display_question += f"\n\n📎 {names}"
    db.session.add(ChatMessage(conversation_id=conv.id, role="user",
                               content=display_question))
    # Remember the context of this message — reopening the conversation
    # restores the drives/files the user had referenced.
    conv.context = json.dumps(ctx["context"])
    db.session.commit()

    conv_id = conv.id

    # History size is configurable per connection (NULL -> global default).
    hist_n = ai_service.config_for(current_user).get("history_messages") or 20
    if hist_n <= 0:
        hist_n = 20
    history = [
        {"role": m.role, "content": m.content}
        for m in conv.messages
        if m.role in ("user", "assistant") and not m.archived
    ][-hist_n:]

    # A summarized conversation starts with the assistant's summary message,
    # but some providers' chat templates (e.g. Gemma in LM Studio) reject a
    # history that does not begin with a user turn ("No user query found in
    # messages"). Leading assistant messages are moved into the system
    # context instead (run_agent_events merges leading system messages into
    # the main system prompt).
    preamble = []
    while history and history[0]["role"] == "assistant":
        preamble.append(history.pop(0)["content"])
    if preamble:
        history.insert(0, {"role": "system", "content":
            "Summary of the earlier conversation:\n\n" + "\n\n".join(preamble)})

    # Context notes (drive scope, dragged cell, open file, attachments) go
    # first, before the conversation summary preamble.
    history = [{"role": "system", "content": note} for note in ctx["notes"]] \
        + history

    user_id = current_user.id
    drive_ids = ctx["context"]["drive_ids"]  # None -> all drives in scope

    def generate():
        """Stream NDJSON events: thinking(+_token) / step / tool_result /
        answer(+_token) / error.

        Token deltas are forwarded live. `block_tokens` tracks which kind of
        token was streamed for the current model message, so the full
        `thinking` event that follows isn't duplicated on the client:
          - after thinking tokens: skip (already rendered in the reasoning box)
          - after answer tokens:   forward with migrate=true (the client moves
            its tentative answer bubble into the reasoning box — the text was
            narration before a tool call, not the final answer)
        """
        thinkings = []
        steps = []
        notices = []
        answer = None
        block_tokens = None  # None | "thinking" | "answer"
        # ORM objects are detached after the earlier commit; re-fetch by id.
        user = db.session.get(User, user_id)
        drive = (Drive.query
                 .filter(Drive.id.in_(drive_ids), Drive.user_id == user_id)
                 .all()) if drive_ids else None
        model_name = ai_service.config_for(user).get("model", "")
        try:
            for kind, payload in agent_service.run_agent_events(user, history, drive=drive):
                if kind == "thinking_token":
                    block_tokens = "thinking"
                    yield json.dumps({"type": "thinking_token", "content": payload},
                                     ensure_ascii=False) + "\n"
                elif kind == "answer_token":
                    block_tokens = "answer"
                    yield json.dumps({"type": "answer_token", "content": payload},
                                     ensure_ascii=False) + "\n"
                elif kind == "thinking":
                    thinkings.append(payload)
                    if block_tokens != "thinking":
                        event = {"type": "thinking", "content": payload}
                        if block_tokens == "answer":
                            event["migrate"] = True
                        yield json.dumps(event, ensure_ascii=False) + "\n"
                    block_tokens = None
                elif kind == "step":
                    block_tokens = None
                    steps.append(payload)
                    yield json.dumps({"type": "step", "step": payload},
                                     ensure_ascii=False) + "\n"
                elif kind == "tool_result":
                    if steps:
                        steps[-1]["summary"] = payload["summary"]
                    yield json.dumps({"type": "tool_result", "result": payload},
                                     ensure_ascii=False) + "\n"
                elif kind == "notice":
                    block_tokens = None
                    notices.append(payload)
                    yield json.dumps({"type": "notice", "content": payload},
                                     ensure_ascii=False) + "\n"
                else:
                    block_tokens = None
                    answer = payload
                    yield json.dumps({"type": "answer",
                                      "conversation_id": conv_id,
                                      "model": model_name,
                                      "answer": payload},
                                     ensure_ascii=False) + "\n"
        except ai_service.AIError as exc:
            msg = str(exc)
            log_service.log_event("error", "ai_chat", msg,
                                  user_id=user_id, path="/ai/chat")
            low = msg.lower()
            if "context length" in low or "maximum context" in low \
                    or "context window" in low or "context size" in low \
                    or "exceed_context_size" in low:
                # The prompt (history + tool results) exceeded the model's
                # context — the raw provider error is cryptic for users.
                msg = ("This conversation is too large for the model's "
                       "context window. Use the Summarize & reset button to "
                       "compact it, start a new conversation, or raise the "
                       "prompt budget in AI Settings.")
            yield json.dumps({"type": "error", "error": msg},
                             ensure_ascii=False) + "\n"
        except Exception as exc:
            # Never let the stream die silently — the UI would otherwise show
            # the agent "stopping mid-task" with no explanation.
            log_service.log_event("error", "ai_chat",
                                  f"Unexpected error: {exc}",
                                  detail=traceback.format_exc(),
                                  user_id=user_id, path="/ai/chat")
            yield json.dumps({"type": "error", "error": f"Unexpected error: {exc}"},
                             ensure_ascii=False) + "\n"

        for thought in thinkings:
            db.session.add(ChatMessage(conversation_id=conv_id,
                                       role="thinking", content=thought))
        for step in steps:
            text = f"{step['label']}: {step['detail']}"
            if step.get("summary"):
                text += f" → {step['summary']}"
            db.session.add(ChatMessage(conversation_id=conv_id,
                                       role="step", content=text))
        for notice in notices:
            db.session.add(ChatMessage(conversation_id=conv_id,
                                       role="notice", content=notice))
        if answer is not None:
            db.session.add(ChatMessage(conversation_id=conv_id,
                                       role="assistant", content=answer,
                                       model=model_name))
        db.session.commit()

    return Response(stream_with_context(generate()),
                    mimetype="application/x-ndjson")


# --- Long-horizon background tasks -----------------------------------------


def _task_json(task):
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "error": task.error,
        "conversation_id": task.conversation_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


@bp.route("/tasks", methods=["POST"])
@login_required
def start_task():
    """Launch a long-horizon background task: the agent runs in a server
    thread with an extended step budget, persisting every event to its own
    conversation so the user can open the transcript at any time."""
    if not ai_service.is_enabled(current_user):
        return jsonify({
            "error": "AI is not configured. Add a connection in AI Settings."
        }), 503

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    attachment_ids = data.get("attachments") or []
    if not message and not attachment_ids:
        return jsonify({"error": "Empty message."}), 400

    # Legacy single drive_id maps onto the multi-drive scope.
    if not data.get("scope_drive_ids") and not data.get("scope_drive_id"):
        try:
            legacy_drive_id = int(data.get("drive_id") or 0)
        except (TypeError, ValueError):
            legacy_drive_id = 0
        if legacy_drive_id:
            data["scope_drive_ids"] = [legacy_drive_id]
            if not Drive.query.filter_by(
                    id=legacy_drive_id, user_id=current_user.id).first():
                return jsonify({"error": "Drive not found."}), 404

    # Snapshot the full context (drives, files, cell) so the task keeps
    # working with what it was launched with, even if the user moves on.
    ctx = _resolve_context(current_user, data)
    task = ai_task_service.start_task(current_user, message, ctx=ctx)
    return jsonify({"ok": True, "task": _task_json(task)})


@bp.route("/tasks/active")
@login_required
def active_tasks():
    """Tasks for the dock widget: running/queued plus finished ones the user
    has not dismissed yet (those carry the notification badge)."""
    return jsonify({
        "tasks": [_task_json(t) for t in ai_task_service.active_tasks(current_user)],
        "poll_ms": current_app.config.get("AI_TASK_WIDGET_POLL_MS", 3000),
    })


@bp.route("/tasks/<int:task_id>")
@login_required
def task_detail(task_id):
    task = ai_task_service.get_task(current_user, task_id)
    if task is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_task_json(task))


@bp.route("/tasks/<int:task_id>/stop", methods=["POST"])
@login_required
def stop_task(task_id):
    task = ai_task_service.stop_task(current_user, task_id)
    if task is None:
        return jsonify({"error": "Not found or already finished"}), 404
    return jsonify({"ok": True, "task": _task_json(task)})


@bp.route("/tasks/<int:task_id>/ack", methods=["POST"])
@login_required
def ack_task(task_id):
    """Dismiss a finished task from the dock — ends its long horizon."""
    task = ai_task_service.ack_task(current_user, task_id)
    if task is None:
        return jsonify({"error": "Not found or still running"}), 404
    return jsonify({"ok": True})


@bp.route("/tasks/<int:task_id>/stream")
@login_required
def task_stream(task_id):
    """Live NDJSON event stream for a running task — same event shape as
    /ai/chat, so a chat window can watch a background task and render it
    exactly like a normal streamed conversation. Ends with a task_status
    event once the task reaches a final state."""
    task = ai_task_service.get_task(current_user, task_id)
    if task is None:
        return jsonify({"error": "Not found"}), 404

    def generate():
        q = ai_task_service.subscribe(task.id)
        try:
            yield (json.dumps({"type": "task_status", "status": task.status},
                              ensure_ascii=False) + "\n")
            if not task.is_active:
                return
            while True:
                ev = q.get()
                if ev is None:  # worker finished — sentinel
                    break
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        finally:
            ai_task_service.unsubscribe(task.id, q)

    return Response(stream_with_context(generate()),
                    mimetype="application/x-ndjson")
