import json
import traceback

from flask import Blueprint, Response, jsonify, request, stream_with_context
from flask_login import current_user, login_required

from ..extensions import db
from ..models import AIConnection, ChatConversation, ChatMessage, Drive, StoredFile, User
from ..services import agent_service, ai_service, drive_service, log_service

bp = Blueprint("ai", __name__, url_prefix="/ai")


def _get_conversation(conv_id):
    conv = db.session.get(ChatConversation, conv_id) if conv_id else None
    if conv and conv.user_id == current_user.id:
        return conv
    return None


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
    return jsonify({
        "id": conv.id,
        "title": conv.title,
        "messages": [
            {"role": m.role, "content": m.content, "model": m.model,
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

    attached = []
    if attachment_ids:
        attached = (StoredFile.query
                    .filter(StoredFile.id.in_(attachment_ids),
                            StoredFile.user_id == current_user.id,
                            StoredFile.deleted_at.is_(None))
                    .all())

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
    db.session.commit()

    conv_id = conv.id

    history = [
        {"role": m.role, "content": m.content}
        for m in conv.messages
        if m.role in ("user", "assistant")
    ][-20:]

    if attached:
        listing = ", ".join(f"[id {f.id}] {f.name}" for f in attached)
        history.insert(0, {
            "role": "system",
            "content": (
                f"The user attached these files to this question: {listing}. "
                "You can read_file them directly by id — no need to search first."
            ),
        })

    user_id = current_user.id
    drive_id = drive_service.get_current_drive(current_user).id

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
        answer = None
        block_tokens = None  # None | "thinking" | "answer"
        # ORM objects are detached after the earlier commit; re-fetch by id.
        user = db.session.get(User, user_id)
        drive = db.session.get(Drive, drive_id)
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
                else:
                    block_tokens = None
                    answer = payload
                    yield json.dumps({"type": "answer",
                                      "conversation_id": conv_id,
                                      "model": model_name,
                                      "answer": payload},
                                     ensure_ascii=False) + "\n"
        except ai_service.AIError as exc:
            log_service.log_event("error", "ai_chat", str(exc),
                                  user_id=user_id, path="/ai/chat")
            yield json.dumps({"type": "error", "error": str(exc)},
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
        if answer is not None:
            db.session.add(ChatMessage(conversation_id=conv_id,
                                       role="assistant", content=answer,
                                       model=model_name))
        db.session.commit()

    return Response(stream_with_context(generate()),
                    mimetype="application/x-ndjson")
