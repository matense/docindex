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

    # The last 4 messages are [answer 3, question 4, answer 4, new question];
    # the slice starts mid-exchange with an assistant message, which is moved
    # into the system context (chat templates require a user turn first) —
    # so 3 user/assistant messages go through plus "answer 3" in the preamble.
    history_sent = past[:-1]  # trailing entry = the reply appended mid-run
    assert [m["content"] for m in history_sent] == [
        "question 4", "answer 4", "new question"]
    preamble = [m for m in messages if m["role"] == "system"]
    assert any("answer 3" in m["content"] for m in preamble)
    assert not any(m["content"] == "question 0" for m in history_sent)


def test_history_length_falls_back_to_global(auth_client, app, user):
    _add_conn(auth_client)  # history_messages NULL
    app.config["AI_HISTORY_MESSAGES"] = 6
    conv_id = _make_conversation(app, n_pairs=5)

    past = _past_user_assistant(_chat_capture(auth_client, conv_id))
    # Slice = [answer 2, question 3, ..., new question]; "answer 2" moves to
    # the system preamble (orphan assistant turn), 5 user/assistant remain.
    assert len(past[:-1]) == 5


def test_history_length_env_config_without_connection(auth_client, app, user):
    app.config["AI_ENABLED"] = True
    app.config["AI_HISTORY_MESSAGES"] = 3
    conv_id = _make_conversation(app, n_pairs=5)

    past = _past_user_assistant(_chat_capture(auth_client, conv_id))
    # Slice = [question 4, answer 4, new question] — already starts with a
    # user turn, so nothing is moved to the preamble.
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
        # Old history is NOT deleted: it stays, archived (out of context).
        assert len(msgs) == 7
        archived = [m for m in msgs if m.archived]
        live = [m for m in msgs if not m.archived]
        assert len(archived) == 6
        assert len(live) == 1
        assert live[0].role == "assistant"
        assert "Conversation summary" in live[0].content
        assert "SUMMARY: talked about files and codes." in live[0].content
        assert live[0].model == "gpt-4o-mini"

    # Only the summary (not the archived messages) is sent as memory — it is
    # an assistant message at the head of the history, so it is moved into
    # the system context (providers require the first turn to be the user).
    messages = _chat_capture(auth_client, conv_id)
    past = _past_user_assistant(messages)
    assert any("SUMMARY: talked about files and codes." in m["content"]
               for m in messages if m["role"] == "system")
    assert not any("question 0" in m["content"] for m in messages)
    assert past[0]["content"] == "new question"


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


def test_summarize_twice_needs_new_history(auth_client, app, user):
    # After a summarize, only the summary is active — a second summarize has
    # nothing to work on until the conversation grows again.
    _add_conn(auth_client)
    conv_id = _make_conversation(app, n_pairs=3)

    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "summary one"}):
        assert auth_client.post(f"/ai/conversations/{conv_id}/summarize").status_code == 200
        resp = auth_client.post(f"/ai/conversations/{conv_id}/summarize")
    assert resp.status_code == 400

    with app.app_context():
        msgs = ChatMessage.query.filter_by(conversation_id=conv_id).all()
        assert len(msgs) == 7
        assert sum(1 for m in msgs if m.archived) == 6


def test_conversation_payload_exposes_archived_flag(auth_client, app, user):
    _add_conn(auth_client)
    conv_id = _make_conversation(app, n_pairs=3)

    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "summary one"}):
        auth_client.post(f"/ai/conversations/{conv_id}/summarize")

    msgs = auth_client.get(f"/ai/conversations/{conv_id}").get_json()["messages"]
    assert all("archived" in m for m in msgs)
    assert [m["archived"] for m in msgs] == [True] * 6 + [False]


def test_summarized_history_does_not_start_with_assistant(auth_client, app, user):
    # Some chat templates (e.g. Gemma in LM Studio) reject a message list
    # whose first non-system message is not a user turn ("No user query
    # found in messages"). After a summarize, the summary is an assistant
    # message — it must be moved into the system context.
    _add_conn(auth_client)
    conv_id = _make_conversation(app, n_pairs=3)

    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "SUMMARY CONTENT"}):
        auth_client.post(f"/ai/conversations/{conv_id}/summarize")

    messages = _chat_capture(auth_client, conv_id)
    # Everything up to the first user message is system context.
    first_user = next(i for i, m in enumerate(messages) if m["role"] == "user")
    assert all(m["role"] == "system" for m in messages[:first_user])
    assert any("SUMMARY CONTENT" in m["content"] for m in messages[:first_user])
    assert messages[first_user]["content"] == "new question"


# ------------------------------------------------------------- prompt budget


def _make_big_conversation(app, n_pairs=4, answer_chars=6000):
    with app.app_context():
        user = User.query.first()
        conv = ChatConversation(user_id=user.id, title="big conv")
        db.session.add(conv)
        db.session.flush()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(n_pairs):
            db.session.add(ChatMessage(
                conversation_id=conv.id, role="user",
                content=f"big question {i}",
                created_at=base + timedelta(minutes=2 * i)))
            db.session.add(ChatMessage(
                conversation_id=conv.id, role="assistant",
                content=f"big answer {i} " + ("x" * answer_chars),
                created_at=base + timedelta(minutes=2 * i + 1)))
        db.session.commit()
        return conv.id


def test_prompt_budget_drops_oldest_exchanges(auth_client, app, user):
    _add_conn(auth_client)
    app.config["AI_MAX_PROMPT_TOKENS"] = 4000  # ~16k chars
    conv_id = _make_big_conversation(app)      # ~48k chars of history

    events = []
    captured = {}

    def fake_completion(*args, **kwargs):
        captured["messages"] = list(kwargs.get("messages") or args[0])
        return {"role": "assistant", "content": "done"}

    with patch("app.services.ai_service.chat_completion",
               side_effect=fake_completion):
        resp = auth_client.post("/ai/chat", json={
            "message": "new question", "conversation_id": conv_id})
        raw = resp.data.decode()

    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    # The user is told that older messages were dropped.
    notes = [e for e in events if e["type"] == "thinking"
             and "context window" in e.get("content", "")]
    assert notes

    from app.services.agent_service import _estimate_tokens
    assert _estimate_tokens(captured["messages"]) <= 4000
    # Newest messages always survive; oldest were dropped.
    contents = [m["content"] for m in captured["messages"]]
    assert "new question" in contents
    assert not any("big answer 0 " in c for c in contents)
    # No orphaned tool messages: every tool message follows its assistant.
    roles = [m["role"] for m in captured["messages"]]
    assert "tool" not in roles


def test_drop_oldest_unit_keeps_tool_pairs(app, user):
    from app.services.agent_service import _drop_oldest_unit
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1"}, {"id": "c2"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        {"role": "assistant", "content": "final"},
    ]
    # The assistant turn + BOTH its tool messages are dropped as one unit.
    assert _drop_oldest_unit(messages)
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]
    # Nothing else is droppable: "q" is the last user message and "final"
    # is the last message — both are protected, so the prompt always keeps
    # a user turn (some chat templates hard-fail without one).
    assert not _drop_oldest_unit(messages)
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]


def test_drop_oldest_unit_never_drops_last_user(app, user):
    # Regression: an oversized tool result made _fit_prompt drop until the
    # LAST user message disappeared, and the provider rejected the prompt
    # with "No user query found in messages".
    from app.services.agent_service import _drop_oldest_unit
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "latest question"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 50000},
    ]
    assert _drop_oldest_unit(messages)  # drops "first question"
    assert _drop_oldest_unit(messages)  # drops "first answer"
    assert _drop_oldest_unit(messages)  # drops assistant + tool (huge result)
    # Only system + the last user message remain — both protected.
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == "latest question"
    assert not _drop_oldest_unit(messages)


def test_max_prompt_tokens_saved_on_connection(auth_client, app, user):
    auth_client.post("/settings/ai/add", data={
        "name": "Budget Conn", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test", "model": "gpt-4o-mini", "vision_model": "",
        "is_active": "on", "max_prompt_tokens": "32000",
    }, follow_redirects=True)
    with app.app_context():
        assert AIConnection.query.one().max_prompt_tokens == 32000


def test_context_file_id_tells_agent_which_file_is_open(auth_client, app, user):
    """The chat sends the currently open file as implicit context; the agent
    must see a system note identifying it (no explicit @here needed)."""
    _add_conn(auth_client)
    from app.services import file_service

    class FakeStorage:
        filename = "notes.txt"
        mimetype = "text/plain"

        def save(self, path):
            with open(path, "wb") as fh:
                fh.write(b"hello")

    with app.app_context():
        fid = file_service.save_upload(FakeStorage(),
                                       db.session.get(User, user)).id

    captured = {}

    def fake_completion(*args, **kwargs):
        captured["messages"] = kwargs.get("messages") or args[0]
        return {"role": "assistant", "content": "done"}

    with patch("app.services.ai_service.chat_completion",
               side_effect=fake_completion):
        resp = auth_client.post("/ai/chat", json={
            "message": "improve this file", "context_file_id": fid})
        resp.data  # consume the stream inside the patch
    assert resp.status_code == 200
    notes = [m for m in captured["messages"]
             if m["role"] == "system" and "currently has" in m["content"]]
    assert notes
    assert f"[id {fid}]" in notes[0]["content"]
    assert "notes.txt" in notes[0]["content"]


def test_context_file_id_ignores_foreign_or_missing_files(auth_client, app, user):
    """A context id that isn't the user's file (or doesn't exist) adds no
    system note."""
    _add_conn(auth_client)
    captured = {}

    def fake_completion(*args, **kwargs):
        captured["messages"] = kwargs.get("messages") or args[0]
        return {"role": "assistant", "content": "done"}

    with patch("app.services.ai_service.chat_completion",
               side_effect=fake_completion):
        resp = auth_client.post("/ai/chat", json={
            "message": "hello", "context_file_id": 999999})
        resp.data
    assert resp.status_code == 200
    assert not [m for m in captured["messages"]
                if m["role"] == "system" and "currently has" in m["content"]]


def _chat_capture_payload(auth_client, payload):
    captured = {}

    def fake_completion(*args, **kwargs):
        captured["messages"] = kwargs.get("messages") or args[0]
        return {"role": "assistant", "content": "done"}

    with patch("app.services.ai_service.chat_completion",
               side_effect=fake_completion):
        resp = auth_client.post("/ai/chat", json=payload)
        resp.data  # consume the stream inside the patch
    assert resp.status_code == 200
    return captured["messages"]


def test_chat_scoped_to_current_drive_by_default(auth_client, app, user):
    _add_conn(auth_client)
    messages = _chat_capture_payload(auth_client, {"message": "hello"})
    system = messages[0]["content"]
    assert messages[0]["role"] == "system"
    assert "only files from that drive are visible" in system


def test_scope_all_drives_widens_the_search(auth_client, app, user):
    """The chat's drive-scope chip removed -> scope_all_drives -> the agent
    sees every drive and is told so."""
    _add_conn(auth_client)
    messages = _chat_capture_payload(auth_client, {
        "message": "hello", "scope_all_drives": True})
    system = messages[0]["content"]
    assert "only files from that drive are visible" not in system
    assert "ALL" in system and "list_drives" in system


def _make_notebook(app, user, cells, hidden=False, locked=False):
    """Persist a .pdocnb file directly (bypasses the module's routes)."""
    from app.models import ModuleState
    from app.services import file_service

    doc = {"title": "NB", "version": 1, "ai_hidden": hidden,
           "ai_lock": locked, "cells": cells}
    payload = json.dumps(doc).encode()

    class FakeStorage:
        filename = "nb.pdocnb"
        mimetype = "application/json"

        def save(self, path):
            with open(path, "wb") as fh:
                fh.write(payload)

    with app.app_context():
        stored = file_service.save_upload(FakeStorage(),
                                          db.session.get(User, user))
        db.session.add(ModuleState(name="notebooks", enabled=True))
        db.session.commit()
        return stored.id


def test_context_cell_gives_agent_cell_reference(auth_client, app, user):
    """A cell dragged into the chat becomes a system note identifying the
    notebook and cell so 'improve this cell' just works."""
    _add_conn(auth_client)
    fid = _make_notebook(app, user, cells=[
        {"id": "cell-1", "type": "markdown", "content": "cell body text",
         "meta": {}}])
    messages = _chat_capture_payload(auth_client, {
        "message": "improve this cell",
        "context_cell": {"file_id": fid, "cell_id": "cell-1"}})
    notes = [m for m in messages
             if m["role"] == "system" and "dragged a cell" in m["content"]]
    assert notes
    assert "cell-1" in notes[0]["content"]
    assert "cell body text" in notes[0]["content"]


def test_context_cell_hidden_notebook_adds_no_note(auth_client, app, user):
    """AI-hidden notebooks stay invisible even when a cell ref is sent."""
    _add_conn(auth_client)
    fid = _make_notebook(app, user, cells=[
        {"id": "cell-1", "type": "markdown", "content": "secret",
         "meta": {}}], hidden=True)
    messages = _chat_capture_payload(auth_client, {
        "message": "hi",
        "context_cell": {"file_id": fid, "cell_id": "cell-1"}})
    assert not [m for m in messages
                if m["role"] == "system" and "dragged a cell" in m["content"]]


def test_context_cell_locked_notebook_tells_agent_not_to_edit(
        auth_client, app, user):
    """A cell dragged from a locked notebook: the agent is told up front to
    ask for an unlock instead of generating content."""
    _add_conn(auth_client)
    fid = _make_notebook(app, user, cells=[
        {"id": "cell-1", "type": "markdown", "content": "cell body text",
         "meta": {}}], locked=True)
    messages = _chat_capture_payload(auth_client, {
        "message": "improve this cell",
        "context_cell": {"file_id": fid, "cell_id": "cell-1"}})
    notes = [m for m in messages
             if m["role"] == "system" and "dragged a cell" in m["content"]]
    assert notes
    assert "LOCKED" in notes[0]["content"]
    assert "unlock" in notes[0]["content"]


def test_open_locked_notebook_context_warns_agent(auth_client, app, user):
    """Opening a locked notebook and chatting: the open-file system note
    tells the agent to ask for a manual unlock, not to generate edits."""
    _add_conn(auth_client)
    fid = _make_notebook(app, user, cells=[
        {"id": "cell-1", "type": "markdown", "content": "body",
         "meta": {}}], locked=True)
    messages = _chat_capture_payload(auth_client, {
        "message": "add a summary to this notebook", "context_file_id": fid})
    notes = [m for m in messages
             if m["role"] == "system" and "currently has" in m["content"]]
    assert notes
    assert "LOCKED" in notes[0]["content"]
    assert "unlock" in notes[0]["content"]
