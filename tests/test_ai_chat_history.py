import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.extensions import db
from app.models import AIConnection, ChatConversation, ChatMessage, User


def _add_conn(client, history_messages=None):
    data = {
        "name": "Test Conn", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test", "model": "gpt-4o-mini", "vision_model": "",
        "is_active": "on",
    }
    if history_messages is not None:
        data["history_messages"] = str(history_messages)
    return client.post("/settings/ai/add", data=data, follow_redirects=True)


def _make_conversation(app, n_pairs=5):
    """A conversation with n_pairs user/assistant exchanges; returns its id."""
    with app.app_context():
        user = User.query.first()
        conv = ChatConversation(user_id=user.id, title="test conv")
        db.session.add(conv)
        db.session.flush()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(n_pairs):
            db.session.add(ChatMessage(
                conversation_id=conv.id, role="user",
                content=f"question {i}", created_at=base + timedelta(minutes=2 * i)))
            db.session.add(ChatMessage(
                conversation_id=conv.id, role="assistant",
                content=f"answer {i}", created_at=base + timedelta(minutes=2 * i + 1)))
        db.session.commit()
        return conv.id


def _chat_capture(auth_client, conv_id):
    """POST /ai/chat with a mocked model; returns the messages it received."""
    captured = {}

    def fake_completion(*args, **kwargs):
        captured["messages"] = kwargs.get("messages") or args[0]
        return {"role": "assistant", "content": "done"}

    with patch("app.services.ai_service.chat_completion",
               side_effect=fake_completion):
        resp = auth_client.post("/ai/chat", json={
            "message": "new question", "conversation_id": conv_id})
        resp.data  # consume the stream inside the patch
    assert resp.status_code == 200
    return captured["messages"]


def _past_user_assistant(messages):
    """user/assistant entries excluding the leading system prompt."""
    return [m for m in messages if m["role"] in ("user", "assistant")]


# ------------------------------------------------------------ history length


def test_history_length_from_connection(auth_client, app, user):
    _add_conn(auth_client, history_messages=4)
    conv_id = _make_conversation(app, n_pairs=5)

    messages = _chat_capture(auth_client, conv_id)
    past = _past_user_assistant(messages)

    # 4 = last two exchanges; the new question is part of the tail. The
    # assistant's final reply is appended to the list during the run, so it
    # shows up as a trailing entry — ignore it.
    history_sent = past[:-1]
    assert len(history_sent) == 4
    assert history_sent[-1]["content"] == "new question"
    assert not any(m["content"] == "question 0" for m in history_sent)


def test_history_length_falls_back_to_global(auth_client, app, user):
    _add_conn(auth_client)  # history_messages NULL
    app.config["AI_HISTORY_MESSAGES"] = 6
    conv_id = _make_conversation(app, n_pairs=5)

    past = _past_user_assistant(_chat_capture(auth_client, conv_id))
    assert len(past[:-1]) == 6


def test_history_length_env_config_without_connection(auth_client, app, user):
    app.config["AI_ENABLED"] = True
    app.config["AI_HISTORY_MESSAGES"] = 3
    conv_id = _make_conversation(app, n_pairs=5)

    past = _past_user_assistant(_chat_capture(auth_client, conv_id))
    assert len(past[:-1]) == 3


def test_history_messages_saved_on_connection(auth_client, app, user):
    _add_conn(auth_client, history_messages=7)
    with app.app_context():
        assert AIConnection.query.one().history_messages == 7


# ------------------------------------------------------------------ summarize


def test_summarize_compacts_conversation(auth_client, app, user):
    _add_conn(auth_client)
    conv_id = _make_conversation(app, n_pairs=3)

    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant",
                             "content": "SUMMARY: talked about files and codes."}):
        resp = auth_client.post(f"/ai/conversations/{conv_id}/summarize")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    with app.app_context():
        msgs = ChatMessage.query.filter_by(conversation_id=conv_id).all()
        assert len(msgs) == 1
        assert msgs[0].role == "assistant"
        assert "Conversation summary" in msgs[0].content
        assert "SUMMARY: talked about files and codes." in msgs[0].content
        assert msgs[0].model == "gpt-4o-mini"

    # The summary is sent as memory in the next question's history.
    past = _past_user_assistant(_chat_capture(auth_client, conv_id))
    assert any("SUMMARY: talked about files and codes." in m["content"]
               for m in past)


def test_summarize_needs_enough_history(auth_client, app, user):
    _add_conn(auth_client)
    conv_id = _make_conversation(app, n_pairs=1)  # 2 messages only

    resp = auth_client.post(f"/ai/conversations/{conv_id}/summarize")
    assert resp.status_code == 400

    with app.app_context():
        assert ChatMessage.query.filter_by(conversation_id=conv_id).count() == 2


def test_summarize_other_users_conversation_forbidden(auth_client, app, user):
    _add_conn(auth_client)
    conv_id = _make_conversation(app, n_pairs=3)

    other = app.test_client()
    other.post("/register", data={
        "username": "erin", "email": "erin@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    resp = other.post(f"/ai/conversations/{conv_id}/summarize")
    assert resp.status_code == 404

    with app.app_context():
        # Nothing was deleted.
        assert ChatMessage.query.filter_by(conversation_id=conv_id).count() == 6


def test_summarize_without_ai_returns_503(auth_client, app, user):
    conv_id = _make_conversation(app, n_pairs=3)
    resp = auth_client.post(f"/ai/conversations/{conv_id}/summarize")
    assert resp.status_code == 503
