import json

from flask import Blueprint, Response, abort, current_app, jsonify, request, stream_with_context
from flask_login import current_user, login_required

from ..extensions import db
from ..models import AIConnection, AITask, ChatConversation, ChatMessage, Drive, StoredFile
from ..services import ai_service, ai_task_service, drive_service, log_service

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


def _attachment_brief(file_service, stored, cap=1000):
    """One-line pre-extracted content for an attached file, so the model
    answers from the gist without a tool call: image caption, table columns
    plus head rows, text head. None when nothing useful is known yet."""
    try:
        desc = file_service.describe_for_chat(stored)
    except Exception:  # noqa: BLE001 - brief is best-effort
        return None
    kind = desc.get("kind")
    if kind == "image":
        return (f"Image. Caption: {desc['caption']}"
                if desc.get("caption") else None)
    if kind == "table":
        cols = ", ".join(desc.get("columns") or [])
        if not cols:
            return None
        rows = [" | ".join(r) for r in (desc.get("rows") or [])[:4]]
        text = f"Table columns: {cols}."
        if rows:
            text += f" First rows: {'; '.join(rows)}."
        return text[:cap]
    if kind == "text":
        head = (desc.get("head") or "").strip().replace("\n", " ")
        return f"Starts with: {head[:cap]}" if head else None
    return None


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
                "context_cell": None, "persona": None,
                "behavior": None, "behavior_name": None}

    # Persona injection: a bundled Agency personality the user picked in the
    # chat selector. It goes FIRST — it defines who the AI is; everything
    # else (drive scope, files, cells) is context on top. Personas the user
    # disabled (Behaviors page) are treated as inactive.
    persona_key = (data.get("persona") or "").strip()
    if persona_key:
        from ..services import persona_service
        persona = persona_service.get_persona(persona_key)
        if persona and persona_service.is_persona_enabled(user.id,
                                                          persona_key):
            vibe = f" — {persona['vibe']}" if persona.get("vibe") else ""
            notes.append(
                f'The user activated the persona "{persona["name"]}" '
                f'({persona["division"]}){vibe}. Adopt this personality, '
                "expertise and communication style for the whole "
                "conversation:\n\n" + persona["content"])
            snapshot["persona"] = persona_key
            snapshot["persona_name"] = persona["name"]
    else:
        # A user-made behavior picked in the chat selector — same slot as a
        # persona (the picker keeps them mutually exclusive). Disabled
        # behaviors are treated as inactive.
        try:
            behavior_id = int(data.get("behavior_id") or 0)
        except (TypeError, ValueError):
            behavior_id = 0
        if behavior_id:
            from ..services import behavior_service
            behavior = behavior_service.get_behavior(behavior_id, user.id)
            if behavior is not None and behavior.enabled:
                notes.append(
                    f'The user activated the behavior "{behavior.name}". '
                    "Follow these instructions for the whole "
                    "conversation:\n\n" + behavior.content)
                snapshot["behavior"] = behavior.id
                snapshot["behavior_name"] = behavior.name

    # Drive scope — resolved first so its note ends up first in the history.
    scope_all_drives = bool(data.get("scope_all_drives"))
    raw_ids = data.get("scope_drive_ids")
    if isinstance(raw_ids, list):
        drive_ids = []
        for i in raw_ids[:10]:
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

    # Automation editor context: the workflow open on screen, so "add a
    # filter node here" works while the user builds a flow.
    try:
        context_workflow_id = int(data.get("context_workflow_id") or 0)
    except (TypeError, ValueError):
        context_workflow_id = 0
    if context_workflow_id:
        from ..services import module_service
        try:
            from docindex_module_automation.models import AutomationWorkflow
        except ImportError:
            AutomationWorkflow = None
        if not module_service.is_enabled("automation"):
            AutomationWorkflow = None
        workflow = (db.session.get(AutomationWorkflow, context_workflow_id)
                    if AutomationWorkflow is not None else None)
        if workflow is not None and workflow.user_id == user.id \
                and not workflow.ai_hidden:
            node = data.get("context_workflow_node")
            node_txt = ""
            if isinstance(node, dict) and node.get("id"):
                node_txt = (f" The node they are configuring right now is "
                            f"'{node.get('id')}' ({node.get('type')}, "
                            f"\"{node.get('label')}\").")
            notes.append(
                f"The user has the automation workflow "
                f"[id {workflow.id}] \"{workflow.name}\" open in the "
                f"Automation editor.{node_txt} When they ask for changes "
                "to \"this workflow\" or a node, use automation.get_workflow "
                "to read the current graph (it includes a diagnosis of "
                "every problem) and automation.edit_workflow to "
                "apply the edits — the editor canvas refreshes "
                "automatically. Use automation.validate_workflow to check "
                "the workflow without running it, and automation.get_run_status "
                "after runs (failures carry a fix_hint).")
            snapshot["workflow_id"] = workflow.id
            snapshot["workflow_name"] = workflow.name

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
        # Pre-extracted gist per attachment (caption, columns+rows, head):
        # the model answers from this without spending tool calls, and digs
        # deeper with read_file/grep only when needed.
        from ..services import file_service as _file_service
        shown = 0
        for f in attached:
            if shown >= 3:
                break
            brief = _attachment_brief(_file_service, f)
            if brief:
                notes.append(f"Attached file [id {f.id}] {f.name}: {brief}")
                shown += 1
        if len(attached) > shown:
            notes.append(
                f"...and {len(attached) - shown} more attached file(s) — "
                "read them by id when needed.")

    # The worker thread cannot rebuild the notes (no request context), so
    # the snapshot carries their final text.
    snapshot["notes"] = list(notes)
    return {"notes": notes, "attachments": attached, "drives": drives,
            "context": snapshot}


@bp.route("/personas")
@login_required
def personas():
    """The bundled Agency persona library (light list, no content) plus the
    user's own enabled behaviors — what the chat persona selector shows.

    Default: only the personas the user has enabled. ``?all=1`` returns
    everything with per-persona ``enabled`` flags (the Behaviors page)."""
    from ..services import behavior_service, persona_service
    if request.args.get("all"):
        return jsonify({"personas": persona_service.list_personas(
            user_id=current_user.id)})
    return jsonify({
        "personas": persona_service.list_personas(
            user_id=current_user.id, enabled_only=True),
        "behaviors": [
            {"id": b.id, "name": b.name, "favorite": bool(b.favorite),
             "preview": (b.content or "")[:140]}
            for b in behavior_service.list_behaviors(
                current_user.id, enabled_only=True)],
    })


@bp.route("/personas/<key>")
@login_required
def persona_detail(key):
    """One persona's full content (chat selector / Behaviors page preview)."""
    from ..services import persona_service
    persona = persona_service.get_persona(key)
    if persona is None:
        abort(404)
    return jsonify(persona)


@bp.route("/conversations")
@login_required
def conversations():
    convs = (ChatConversation.query
             .filter_by(user_id=current_user.id)
             .order_by(ChatConversation.updated_at.desc()).all())
    # Latest assistant model per conversation without loading every message
    # (N+1): one grouped query for the newest assistant row each.
    conv_ids = [c.id for c in convs]
    latest_model = {}
    task_status = {}
    if conv_ids:
        rows = (db.session.query(ChatMessage.conversation_id,
                                 ChatMessage.model)
                .filter(ChatMessage.conversation_id.in_(conv_ids),
                        ChatMessage.role == "assistant")
                .order_by(ChatMessage.conversation_id,
                          ChatMessage.id.desc()).all())
        seen = set()
        for cid, model in rows:
            # rows are newest-first per conversation only if grouped; take
            # the first assistant model seen per conversation in reverse id
            # order — cheap and avoids loading full message bodies.
            if cid not in seen:
                seen.add(cid)
                latest_model[cid] = model
        # Which conversations belong to a background task (history filter +
        # status dot) — one query for the whole list.
        for tcid, tstatus in (db.session.query(AITask.conversation_id,
                                               AITask.status)
                              .filter(AITask.conversation_id.in_(conv_ids),
                                      AITask.user_id == current_user.id).all()):
            task_status[tcid] = tstatus
    return jsonify([
        {"id": c.id, "title": c.title,
         "updated_at": c.updated_at.isoformat() if c.updated_at else None,
         # The model shown is the one that wrote the latest assistant reply.
         "model": latest_model.get(c.id),
         # Background-task ownership (None for normal chats).
         "task": task_status.get(c.id)}
        for c in convs
    ])


@bp.route("/conversations/<int:conv_id>")
@login_required
def conversation(conv_id):
    conv = _get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    # A conversation owned by a background task reopens in background mode
    # until the user dismisses the task (ack) — see ai_chat.js. One
    # conversation can own several sequential tasks: the latest decides.
    task = (AITask.query.filter_by(conversation_id=conv.id)
            .order_by(AITask.id.desc()).first())
    return jsonify({
        "id": conv.id,
        "title": conv.title,
        # Latest context the user set up in this conversation (drives,
        # attachments, cell) — restored into the chips on open.
        "context": json.loads(conv.context) if conv.context else None,
        "task": ({"id": task.id, "status": task.status, "title": task.title,
                  "notified": bool(task.notified),
                  "started_at": (task.started_at.isoformat()
                                 if task.started_at else None),
                  "finished_at": (task.finished_at.isoformat()
                                   if task.finished_at else None),
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
    # A conversation can own several background tasks — remove them with it
    # so the dock never points at a deleted transcript.
    AITask.query.filter_by(conversation_id=conv.id).delete()
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
    if len(question) > 20000:
        return jsonify({"error": "Message is too long (max 20000 characters)."}), 400
    if len(attachment_ids) > 20:
        return jsonify({"error": "Too many attachments (max 20)."}), 400

    ctx = _resolve_context(current_user, data)

    conv = _get_conversation(data.get("conversation_id"))
    if not conv:
        conv = ChatConversation(user_id=current_user.id,
                                title=(question or "Attachments")[:80])
        db.session.add(conv)
        db.session.flush()

    # Remember the context of this message — reopening the conversation
    # restores the drives/files the user had referenced.
    conv.context = json.dumps(ctx["context"])
    db.session.commit()

    conv_id = conv.id

    # One run at a time per conversation: follow-ups wait for (or stop)
    # the running one instead of interleaving two transcripts.
    busy = (AITask.query.filter_by(conversation_id=conv.id)
            .filter(AITask.status.in_(("queued", "running"))).first())
    if busy is not None:
        return jsonify({"error": "This conversation already has a run in "
                                 "progress — stop it before sending."}), 409

    try:
        # Raw question only: start_task appends the 📎 attachment suffix
        # itself when persisting the user message.
        task = ai_task_service.start_task(
            current_user,
            question or "What can you tell me about these files?",
            ctx=ctx, conversation_id=conv_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    # Inline runs (tests) finish inside start_task in a different session —
    # refresh so the response carries the final status, not the queued one.
    db.session.refresh(task)
    return jsonify({"ok": True, "task": _task_json(task),
                    "conversation_id": conv_id,
                    "stream_url": f"/ai/tasks/{task.id}/stream"})


# --- Long-horizon background tasks -----------------------------------------


def _task_json(task):
    data = {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "error": task.error,
        "max_steps": task.max_steps,
        "conversation_id": task.conversation_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        # Dock progress line: steps done + currently running tool (if live).
        "progress": ai_task_service.task_progress(task),
    }
    if getattr(task, "_queue_pos", None):
        data["queue_pos"] = task._queue_pos
    return data


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
    if len(message) > 20000:
        return jsonify({"error": "Message is too long (max 20000 characters)."}), 400
    if len(attachment_ids) > 20:
        return jsonify({"error": "Too many attachments (max 20)."}), 400

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

    # Optional: continue an existing conversation instead of starting a
    # new one — background mode changes only the execution, not the chat.
    conv = None
    conv_id = data.get("conversation_id")
    if conv_id not in (None, ""):
        try:
            conv_id = int(conv_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid conversation id."}), 400
        conv = ChatConversation.query.filter_by(
            id=conv_id, user_id=current_user.id).first()
        if conv is None:
            return jsonify({"error": "Conversation not found."}), 404
        busy = (AITask.query.filter_by(conversation_id=conv.id)
                .filter(AITask.status.in_(("queued", "running"))).first())
        if busy is not None:
            return jsonify({"error": "This conversation already has a "
                                     "background task running."}), 409

    # Snapshot the full context (drives, files, cell) so the task keeps
    # working with what it was launched with, even if the user moves on.
    ctx = _resolve_context(current_user, data)
    task = ai_task_service.start_task(current_user, message, ctx=ctx,
                                      conversation_id=conv.id if conv else None)
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


@bp.route("/attachments", methods=["POST"])
@login_required
def upload_attachments():
    """Upload device files straight from the chat composer.

    Saves to ``drive_id`` when given (must be owned + writable), otherwise
    to the open drive when writable. When the open drive is read-only the
    client is asked to choose: 409 + writable drives list ("ask first").
    Always answers JSON. Every saved file is indexed and returned ready
    to attach.
    """
    from werkzeug.utils import secure_filename

    from ..services import file_service

    drive = None
    drive_id = request.form.get("drive_id", type=int)
    if drive_id:
        drive = Drive.query.filter_by(
            id=drive_id, user_id=current_user.id).first()
        if drive is None:
            return jsonify({"error": "Drive not found."}), 404
        if drive.is_synced:
            return jsonify({"error": "That drive is read-only."}), 400
    else:
        drive = drive_service.get_current_drive(current_user)
        if drive.is_synced:
            writable = [d for d in Drive.query
                        .filter_by(user_id=current_user.id)
                        .order_by(Drive.name).all() if not d.is_synced]
            return jsonify({"error": "The open drive is read-only — pick "
                                     "where to save.",
                            "needs_drive_choice": True,
                            "drives": [{"id": d.id, "name": d.name}
                                       for d in writable]}), 409

    uploaded = request.files.getlist("files")
    saved_files, errors = [], []
    for file_storage in uploaded:
        if not file_storage.filename:
            continue
        if not file_service.allowed_file(file_storage.filename):
            errors.append(f"{file_storage.filename}: file type not allowed")
            continue
        try:
            name = secure_filename(file_storage.filename) or "unnamed"
            existing = file_service.find_by_name(
                current_user.id, drive.id, None, name)
            if existing:
                result = file_service.replace_with_upload(existing,
                                                          file_storage)
                saved_files.append({"id": existing.id, "name": existing.name,
                                    "drive_id": drive.id,
                                    "drive_name": drive.name,
                                    "versioned": result == "replaced",
                                    "duplicates": []})
                _trigger_chat_indexing(existing.id)
                continue
            stored = file_service.save_upload(file_storage, current_user,
                                              None)
            stored.drive_id = drive.id
            db.session.commit()
            _trigger_chat_indexing(stored.id)
            dupes = file_service.find_duplicates(stored)
            saved_files.append({
                "id": stored.id, "name": stored.name,
                "drive_id": drive.id, "drive_name": drive.name,
                "versioned": False,
                "duplicates": [{"id": d.id, "name": d.name} for d in dupes],
            })
        except ValueError as exc:
            errors.append(str(exc))
    return jsonify({"files": saved_files, "errors": errors,
                    "drive": {"id": drive.id, "name": drive.name}})


def _trigger_chat_indexing(file_id):
    """Index a chat upload now (sync in tests) or queue it like /upload."""
    from ..services import indexing_service
    if current_app.config.get("INDEX_ASYNC", True):
        indexing_service.enqueue_index([file_id])
        indexing_service.ensure_workers(1)
    else:
        indexing_service.index_file(file_id)


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


@bp.route("/tasks/<int:task_id>/rename", methods=["POST"])
@login_required
def rename_task(task_id):
    """Rename a task (dock tooltip + task banner title)."""
    task = ai_task_service.get_task(current_user, task_id)
    if task is None:
        return jsonify({"error": "Not found"}), 404
    title = " ".join(str((request.get_json(silent=True) or {}).get("title")
                         or "").split())[:120]
    if not title:
        return jsonify({"error": "Title cannot be empty."}), 400
    task.title = title
    if task.conversation is not None:
        task.conversation.title = title[:80]
    db.session.commit()
    return jsonify({"ok": True, "task": _task_json(task)})


@bp.route("/tasks/<int:task_id>/rerun", methods=["POST"])
@login_required
def rerun_task(task_id):
    """Launch a new background task cloning a finished one's question and
    captured context (its drives/files/cell snapshot)."""
    task = ai_task_service.get_task(current_user, task_id)
    if task is None:
        return jsonify({"error": "Not found"}), 404
    if task.is_active:
        return jsonify({"error": "The task is still running."}), 409
    # The exact launching question (title is truncated at 80 chars); fall
    # back to the conversation's first user message for pre-prompt tasks.
    if task.prompt:
        message = task.prompt
    else:
        first_user = next((m for m in task.conversation.messages
                           if m.role == "user"), None)
        message = (first_user.content if first_user else task.title) or ""
        # Strip the rendered 📎 attachment suffix: a re-run sends the question.
        import re as _re
        message = _re.sub(r"\n\n📎 [^\n]*$", "", message).strip()
    ctx = {"context": json.loads(task.context)} if task.context else None
    try:
        new_task = ai_task_service.start_task(current_user, message, ctx=ctx)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "task": _task_json(new_task)})


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
