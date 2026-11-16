import json

import pytest
from datetime import UTC, datetime, timedelta
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
        base = datetime(2026, 1, 1, tzinfo=UTC)
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
        base = datetime(2026, 1, 1, tzinfo=UTC)
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

    captured = {}

    def fake_completion(*args, **kwargs):
        captured["messages"] = list(kwargs.get("messages") or args[0])
        return {"role": "assistant", "content": "done"}

    with patch("app.services.ai_service.chat_completion",
               side_effect=fake_completion):
        resp = auth_client.post("/ai/chat", json={
            "message": "new question", "conversation_id": conv_id})
        data = resp.get_json()

    assert resp.status_code == 200
    # The user is told that older messages were dropped — as a visible
    # "notice" card (a hidden "thinking" line was too easy to miss).
    with app.app_context():
        notes = ChatMessage.query.filter_by(
            conversation_id=data["conversation_id"], role="notice").all()
    assert any("context window" in (n.content or "") for n in notes)

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


def test_scope_drive_id_selects_that_drive(auth_client, app, user):
    """A $ drive mention scopes the agent to the chosen drive — and wins
    over scope_all_drives."""
    _add_conn(auth_client)
    from app.models import Drive
    with app.app_context():
        db.session.add(Drive(name="Archive", user_id=user))
        db.session.commit()
        did = Drive.query.filter_by(name="Archive").one().id
    messages = _chat_capture_payload(auth_client, {
        "message": "hello", "scope_all_drives": True,
        "scope_drive_id": did})
    system = messages[0]["content"]
    assert '"Archive" drive' in system
    assert "only files from that drive are visible" in system


def test_scope_drive_id_ignores_foreign_drives(auth_client, app, user):
    """A drive id owned by someone else is ignored (falls back to the
    default scope)."""
    _add_conn(auth_client)
    from app.models import Drive
    with app.app_context():
        other = User(username="bob", email="bob@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        db.session.add(Drive(name="BobDrive", user_id=other.id))
        db.session.commit()
        did = Drive.query.filter_by(name="BobDrive").one().id
    messages = _chat_capture_payload(auth_client, {
        "message": "hello", "scope_drive_id": did})
    system = messages[0]["content"]
    assert "BobDrive" not in system


def test_api_drives_lists_only_own(auth_client, app, user):
    from app.models import Drive
    with app.app_context():
        db.session.add(Drive(name="Archive", user_id=user))
        other = User(username="bob2", email="bob2@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        db.session.add(Drive(name="BobDrive", user_id=other.id))
        db.session.commit()
    resp = auth_client.get("/api/drives")
    assert resp.status_code == 200
    names = [d["name"] for d in resp.get_json()]
    assert "Archive" in names
    assert "BobDrive" not in names


def test_conversation_returns_latest_context(auth_client, app, user):
    """Each chat message snapshots its context onto the conversation, and the
    conversation endpoint returns it so reopening restores the user's chips."""
    from app.models import Drive
    _add_conn(auth_client)
    with app.app_context():
        drive = Drive(name="Work", user_id=user)
        db.session.add(drive)
        db.session.commit()
        drive_id = drive.id

    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "hi"}):
        resp = auth_client.post("/ai/chat", json={
            "message": "hello", "scope_drive_ids": [drive_id]})
    assert resp.status_code == 200

    with app.app_context():
        conv = ChatConversation.query.one()
        conv_id = conv.id
        snap = json.loads(conv.context)
        assert snap["drive_ids"] == [drive_id]
        assert snap["drive_names"] == ["Work"]

    data = auth_client.get(f"/ai/conversations/{conv_id}").get_json()
    assert data["context"]["drive_ids"] == [drive_id]
    assert data["context"]["drive_names"] == ["Work"]

    # A later message with a different context replaces the snapshot.
    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "hi again"}):
        resp = auth_client.post("/ai/chat", json={
            "message": "now everywhere", "conversation_id": conv_id,
            "scope_all_drives": True})
    assert resp.status_code == 200
    data = auth_client.get(f"/ai/conversations/{conv_id}").get_json()
    assert data["context"]["all_drives"] is True
    assert data["context"]["drive_ids"] is None


def test_personas_endpoint_lists_library(auth_client, app, user):
    """The chat persona selector gets the bundled Agency library as a light
    list (no content — that ships only into the system note)."""
    resp = auth_client.get("/ai/personas")
    assert resp.status_code == 200
    personas = resp.get_json()["personas"]
    assert len(personas) > 100
    assert {"key", "name", "division", "description"} <= set(personas[0])
    assert "content" not in personas[0]


def test_persona_injected_as_first_system_note(auth_client, app, user):
    """A picked persona becomes the FIRST system note (it defines who the AI
    is) and is snapshotted onto the conversation context."""
    from app.services import persona_service
    _add_conn(auth_client)
    persona = persona_service.list_personas()[0]
    messages = _chat_capture_payload(auth_client, {
        "message": "hello", "persona": persona["key"]})
    assert messages[0]["role"] == "system"
    assert 'activated the persona' in messages[0]["content"]
    assert persona["name"] in messages[0]["content"]

    with app.app_context():
        conv = ChatConversation.query.one()
        snap = json.loads(conv.context)
        assert snap["persona"] == persona["key"]
        assert snap["persona_name"] == persona["name"]


def test_invalid_persona_is_ignored(auth_client, app, user):
    """An unknown persona key adds no note and is not snapshotted."""
    _add_conn(auth_client)
    messages = _chat_capture_payload(auth_client, {
        "message": "hello", "persona": "nope-does-not-exist"})
    assert not [m for m in messages
                if m["role"] == "system"
                and "activated the persona" in m["content"]]
    with app.app_context():
        conv = ChatConversation.query.one()
        assert json.loads(conv.context)["persona"] is None


def test_disabled_persona_is_ignored(auth_client, app, user):
    """A persona the user disabled on the Behaviors page is not injected."""
    from app.services import persona_service
    _add_conn(auth_client)
    persona = persona_service.list_personas()[0]
    with app.app_context():
        persona_service.set_persona_enabled(user, persona["key"], False)
    messages = _chat_capture_payload(auth_client, {
        "message": "hello", "persona": persona["key"]})
    assert not [m for m in messages
                if m["role"] == "system"
                and "activated the persona" in m["content"]]
    with app.app_context():
        conv = ChatConversation.query.one()
        assert json.loads(conv.context)["persona"] is None


def test_chat_persists_compaction_notice(auth_client, app, user):
    """Automatic prompt compaction is visible: a 'notice' message is stored
    in the conversation and streamed to the client."""
    _add_conn(auth_client)
    app.config["AI_MAX_PROMPT_TOKENS"] = 400  # tiny budget forces drops
    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "ok"}):
        resp = auth_client.post("/ai/chat", json={
            "message": "hello " + "x" * 400,
            "conversation_id": _make_conversation(app, n_pairs=5)})
        data = resp.get_json()

    assert resp.status_code == 200
    with app.app_context():
        from app.models import ChatMessage
        assert any(m.role == "notice"
                   for m in ChatMessage.query.filter_by(
                       conversation_id=data["conversation_id"]).all())

    with app.app_context():
        conv = ChatConversation.query.one()
        notices = [m for m in conv.messages if m.role == "notice"]
        assert notices and "compacted" in notices[0].content.lower()


# ---------------------------------------------------- composer restructure


def _read_template(rel):
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / rel).read_text(
        encoding="utf-8")


def test_composer_structure_unified():
    html = _read_template("app/templates/components/ai_chat.html")
    assert "ai-ctx-strip" in html and "ai-token-meter" in html
    assert 'class="acw-form ai-composer relative"' in html
    assert "ai-send-btn" in html


# ------------------------------------------------- mention mid-string (C3)


def pick_region(src):
    """The mention pick handlers: splice discipline lives here (C3)."""
    start = src.index("function pickDrive")
    end = src.index("function refreshSendKeyHint")
    return src[start:end]


def test_mention_splice_preserves_trailing_text():
    src = _read_template("app/static/js/ai_core.js")
    assert "selectionStart" in src and "mentionStartIdx" in src
    assert "lastIndexOf" not in pick_region(src)  # must not truncate after cursor


def test_mention_detection_anchored_at_cursor():
    src = _read_template("app/static/js/ai_core.js")
    assert "selectionStart" in src
    assert r"(?:^|\s)\$(\S*)$" in src
    assert r"(?:^|\s)#(\S*)$" in src
    assert r"(?:^|\s)\/(\S*)$" in src
    assert "mentionStartIdx" in src and "mentionEndIdx" in src
    assert "setSelectionRange" in src


def test_mention_keyboard_navigation():
    src = _read_template("app/static/js/ai_core.js")
    assert "mentionActiveIdx" in src
    assert "(mentionActiveIdx + 1) % rows.length" in src
    assert "(mentionActiveIdx - 1 + rows.length) % rows.length" in src
    assert "rows[mentionActiveIdx].click()" in src
    assert "bg-primary/15" in src
    assert "ArrowDown" in src and "ArrowUp" in src
    assert "'Tab'" in src


def test_conversation_returns_token_estimate(auth_client, app, user):
    conv_id = _make_conversation(app, n_pairs=3)
    with app.app_context():
        msgs = ChatMessage.query.filter_by(conversation_id=conv_id).all()
        expected = sum(len(m.content or "") for m in msgs) // 4
    r = auth_client.get(f"/ai/conversations/{conv_id}")
    assert r.status_code == 200
    data = r.get_json()
    assert data["token_estimate"] == expected
    assert data["max_prompt_tokens"] > 0


def test_conversation_token_estimate_counts_active_only(auth_client, app, user):
    # Archived history (compacted by summarize) is out of context and must
    # not count towards the estimate.
    _add_conn(auth_client)
    conv_id = _make_conversation(app, n_pairs=3)
    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "summary one"}):
        assert auth_client.post(
            f"/ai/conversations/{conv_id}/summarize").status_code == 200
    with app.app_context():
        active = [m for m in ChatMessage.query.filter_by(
            conversation_id=conv_id).all()
            if m.role in ("user", "assistant") and not m.archived]
        assert len(active) == 1  # only the summary stays in context
        expected = sum(len(m.content or "") for m in active) // 4
    data = auth_client.get(f"/ai/conversations/{conv_id}").get_json()
    assert data["token_estimate"] == expected
    # No connection-level override -> global default.
    assert data["max_prompt_tokens"] == 60000


def test_token_meter_affordance_gated_on_host_control():
    # The meter must only advertise click-to-summarize (role/cursor/title)
    # when the host actually has a summarize control; on hosts without one
    # (full page) it renders as a plain indicator.
    src = _read_template("app/static/js/ai_core.js")
    body = src.split("function updateTokenMeter")[1].split("\n        function ")[0]
    assert "querySelector('.acw-summarize, #aip-summarize')" in body
    assert "if (host)" in body
    assert "removeAttribute('role')" in body  # plain indicator when no host
    assert "meter.style.cursor = ''" in body
    # The click-affordance title appears exactly once, inside the
    # host-present branch — never unconditionally.
    assert body.count("Click to Summarize") == 1


def test_composer_structure_full_page():
    html = _read_template("app/templates/search/ai.html")
    assert "ai-ctx-strip" in html and "ai-token-meter" in html
    assert "ai-composer" in html and "relative" in html
    assert "ai-send-btn" in html
    # Full-page composer keeps its own hooks (no behavior change).
    assert 'id="aip-form"' in html and 'id="aip-input"' in html
    assert 'id="aip-send"' in html and 'id="aip-plus"' in html


def test_layout_switcher_and_sidebar_present():
    assert "acw-layout-grp" in _read_template(
        "app/templates/components/ai_chat.html")
    full = _read_template("app/templates/search/ai.html")
    assert 'id="aip-sidebar"' in full and 'id="aip-popout-btn"' in full


# ------------------------------------------------- C4 fix round 1/5
# Reviewer Important 1: the dock wrappers must persist AFTER the (async)
# load resolves, not synchronously with the stale convId.


def test_dock_persist_after_load_awaits():
    src = _read_template("app/static/js/ai_chat.js")
    assert "await sess.loadConversation" in src
    assert "await sess.newConversation" in src
    load_i = src.index("await sess.loadConversation")
    assert "persistDock()" in src[load_i:load_i + 200]
    new_i = src.index("await sess.newConversation")
    assert "persistDock()" in src[new_i:new_i + 200]


# Reviewer Important 2: ?conv= must be numerically validated (no NaN fetch).


def test_conv_param_validated():
    src = _read_template("app/static/js/ai_page.js")
    # Floats (?conv=2.7) must not slip through to a non-matching route.
    assert "Number.isInteger" in src


# Reviewer Important 3: attachNotebookCell must reject empty strings too.


def test_attach_notebook_cell_rejects_empty_strings():
    src = _read_template("app/static/js/ai_chat.js")
    assert "fileId === ''" in src


# ------------------------------------------------- C5 badges + grouping


def test_reasoning_badges_and_grouping():
    src = _read_template("app/static/js/ai_core.js")
    assert "ai-reasoning-badges" in src
    for tool in ["search_files", "read_file", "pyrun.compute"]:
        assert tool in src  # category mapping covers it
    assert "ai-reasoning-elapsed" in src  # total time inside <summary>
    assert "flushHistoryReasoning" in src  # single-<details> grouping on history load
    css = _read_template("app/static/css/style.css")
    assert "ai-reasoning-badges" in css


# ------------------------------------------------- C6 quick-peek (endpoint)


@pytest.fixture()
def owned_file(app, user):
    """A plain text file owned by the user, longer than the peek window."""
    from app.models import Drive
    from app.services import file_service

    class FakeStorage:
        filename = "peek-notes.txt"
        mimetype = "text/plain"

        def save(self, path):
            with open(path, "wb") as fh:
                for i in range(60):
                    fh.write(f"peek line {i:03d}: lorem ipsum dolor sit amet\n".encode())

    with app.app_context():
        drive = Drive(name="Personal", user_id=user, source_path=None)
        db.session.add(drive)
        db.session.commit()
        stored = file_service.save_upload(
            FakeStorage(), db.session.get(User, user))
        stored.drive_id = drive.id
        db.session.commit()
        return stored.id


@pytest.fixture()
def hidden_file(app, user):
    """An AI-hidden notebook: the peek endpoint must refuse it (403)."""
    return _make_notebook(app, user, cells=[
        {"id": "cell-1", "type": "markdown", "content": "secret",
         "meta": {}}], hidden=True)


@pytest.fixture()
def trashed_file(app, user):
    """A soft-deleted file: the peek endpoint must 404, never leak."""
    from app.models import Drive, StoredFile
    from app.services import file_service

    class FakeStorage:
        filename = "trashed.txt"
        mimetype = "text/plain"

        def save(self, path):
            with open(path, "wb") as fh:
                fh.write(b"trashed content")

    with app.app_context():
        drive = Drive(name="Personal", user_id=user, source_path=None)
        db.session.add(drive)
        db.session.commit()
        stored = file_service.save_upload(
            FakeStorage(), db.session.get(User, user))
        stored.drive_id = drive.id
        stored.deleted_at = datetime.now(UTC)
        db.session.commit()
        return stored.id


def test_peek_returns_snippet(auth_client, owned_file):
    r = auth_client.get(f"/ai/files/{owned_file}/peek")
    assert r.status_code == 200 and len(r.get_json()["snippet"]) <= 600


def test_peek_denies_hidden_and_trashed(auth_client, hidden_file, trashed_file):
    assert auth_client.get(f"/ai/files/{hidden_file}/peek").status_code == 403
    assert auth_client.get(f"/ai/files/{trashed_file}/peek").status_code == 404


# ------------------------------------------------- C6 fix round 1/2


def _branch_messages(app, conv_id):
    with app.app_context():
        return (ChatMessage.query.filter_by(conversation_id=conv_id)
                .order_by(ChatMessage.id).all())


def test_branch_copies_up_to_message(auth_client, app, user):
    conv_id = _make_conversation(app, n_pairs=3)  # q0 a0 q1 a1 q2 a2
    orig = _branch_messages(app, conv_id)
    up_to = orig[1].id
    with app.app_context():
        orig_title = db.session.get(ChatConversation, conv_id).title
        orig_context = db.session.get(ChatConversation, conv_id).context
    r = auth_client.post(f"/ai/conversations/{conv_id}/branch",
                         json={"up_to_message_id": up_to})
    assert r.status_code == 200
    data = r.get_json()
    assert data["title"] == orig_title + " (branch)"
    assert data["id"] != conv_id
    branched = _branch_messages(app, data["id"])
    assert [m.content for m in branched] == [m.content for m in orig[:2]]
    assert [m.role for m in branched] == [m.role for m in orig[:2]]
    with app.app_context():
        assert db.session.get(ChatConversation, data["id"]).context == orig_context


def test_branch_rejects_bad_message_id(auth_client, app, user):
    conv_id = _make_conversation(app, n_pairs=1)
    assert auth_client.post(
        f"/ai/conversations/{conv_id}/branch",
        json={"up_to_message_id": 9999}).status_code == 404
    assert auth_client.post(
        f"/ai/conversations/{conv_id}/branch",
        json={}).status_code == 400
    assert auth_client.post(
        "/ai/conversations/999999/branch",
        json={"up_to_message_id": 1}).status_code == 404


def test_branch_rejects_non_integer_message_id(auth_client, app, user):
    conv_id = _make_conversation(app, n_pairs=1)
    with app.app_context():
        msg_id = (ChatMessage.query.filter_by(conversation_id=conv_id)
                  .order_by(ChatMessage.id).first().id)
    # Float, bool and non-integer payloads fail closed (400) — a float
    # anchor must never truncate to a neighbouring message id.
    for bad in (2.7, float(msg_id), True, False, "abc", "2.7",
                None, [msg_id], {"id": msg_id}):
        assert auth_client.post(
            f"/ai/conversations/{conv_id}/branch",
            json={"up_to_message_id": bad}).status_code == 400
    # Int and integer-valued string still work.
    assert auth_client.post(
        f"/ai/conversations/{conv_id}/branch",
        json={"up_to_message_id": msg_id}).status_code == 200
    assert auth_client.post(
        f"/ai/conversations/{conv_id}/branch",
        json={"up_to_message_id": str(msg_id)}).status_code == 200


def test_branch_rejects_foreign_message_id(auth_client, app, user):
    conv_a = _make_conversation(app, n_pairs=1)
    conv_b = _make_conversation(app, n_pairs=1)
    with app.app_context():
        foreign = (ChatMessage.query.filter_by(conversation_id=conv_b)
                   .order_by(ChatMessage.id).first().id)
    assert auth_client.post(
        f"/ai/conversations/{conv_a}/branch",
        json={"up_to_message_id": foreign}).status_code == 404


def test_branch_other_users_conversation_forbidden(auth_client, app, user):
    conv_id = _make_conversation(app, n_pairs=1)
    other = app.test_client()
    other.post("/register", data={
        "username": "erin", "email": "erin@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    with app.app_context():
        msg_id = (ChatMessage.query.filter_by(conversation_id=conv_id)
                  .order_by(ChatMessage.id).first().id)
    assert other.post(
        f"/ai/conversations/{conv_id}/branch",
        json={"up_to_message_id": msg_id}).status_code == 404


def test_bubble_actions_user_and_assistant():
    src = _read_template("app/static/js/ai_core.js")
    start = src.index("function addBubbleActions")
    end = src.index("function addMessage", start)
    body = src[start:end]
    assert "Edit & Resend" in body
    assert "Branch" in body
    assert "Insert in Notebook" in body
    assert "Pin to Dashboard" in body
    assert "up_to_message_id" in body
    assert "loadConversation(res.id)" in body


def test_cross_module_actions_fail_closed_when_disabled():
    src = _read_template("app/static/js/ai_core.js")
    # Pin to Dashboard is gated on the dashboards module being active...
    assert "dashboardsEnabled" in src
    assert 'a[href^="/m/dashboards"]' in src
    # ...and fails closed with a clear reason when off (never a dead button).
    assert "dashboards module is disabled" in src
    # Neither cross-module action invents a new server API: the chat opens
    # the existing module surfaces with the payload ready (clipboard).
    assert "copyWithToast" in src
    assert "/m/notebooks/" in src and "/m/dashboards/" in src


@pytest.mark.parametrize("err", [
    OSError("unreadable"),
    ValueError("corrupt bytes"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte"),
])
def test_peek_unreadable_file_returns_empty_snippet(
        auth_client, owned_file, monkeypatch, err):
    # Decode/corrupt/binary failures must degrade to an empty preview,
    # never bubble to a 500.
    from app.services import file_service

    def _boom(stored, max_chars=600):
        raise err

    monkeypatch.setattr(file_service, "read_text_content", _boom)
    r = auth_client.get(f"/ai/files/{owned_file}/peek")
    assert r.status_code == 200 and r.get_json()["snippet"] == ""


def test_cite_chip_click_ignores_modified_clicks():
    # Only a plain primary click may open the popover: Ctrl/Cmd+click,
    # Shift+click and non-primary buttons must keep their native
    # behavior (new tab / new window) via the plain href.
    src = _read_template("app/static/js/ai_core.js")
    i = src.index("ev.target.closest('.ai-cite-chip[data-cite-file-id]')")
    region = src[i:i + 600]
    for token in ["ev.button !== 0", "ev.ctrlKey", "ev.metaKey",
                  "ev.shiftKey", "ev.altKey"]:
        assert token in region
    assert region.index("ev.button") < region.index("ev.preventDefault")
    assert (region.index("ev.preventDefault")
            < region.index("openPeekPopover("))


# ------------------------------------------------- C8 SPA sync / dedup / listeners


def test_spa_drive_sync_wiring():
    assert 'data-drive-id' in _read_template("app/templates/base.html")
    assert "drive:changed" in _read_template("app/static/js/spa.js")
    assert _read_template("app/templates/search/ai.html").count("ai_core.js") <= 1


def test_duplicate_scripts_removed():
    html = _read_template("app/templates/search/ai.html")
    assert "ai_common.js" not in html
    assert html.count("ai_core.js") <= 1
    assert "ai_page.js" in html  # full-page host stays


def test_listeners_cleaned_up():
    core = _read_template("app/static/js/ai_core.js")
    assert "drive:changed" in core  # listener + detach cleanup
    assert "onDocClick" in core and "onDriveChanged" in core
    assert "removeEventListener('click', onDocClick)" in core
    assert "removeEventListener('drive:changed', onDriveChanged)" in core
    chat = _read_template("app/static/js/ai_chat.js")
    assert "onMoreDocClick" in chat
    assert "removeEventListener('click', onMoreDocClick)" in chat


# ------------------------------------------------- final-review hardening
# branch title clamp (String(255)), ?conv= integer-only, attachFile id
# type dedupe, token-meter clamp pin.


def test_branch_title_clamped_to_255(auth_client, app, user):
    conv_id = _make_conversation(app, n_pairs=1)
    with app.app_context():
        conv = db.session.get(ChatConversation, conv_id)
        conv.title = "T" * 250
        db.session.commit()
        up_to = (ChatMessage.query.filter_by(conversation_id=conv_id)
                 .order_by(ChatMessage.id).first().id)
    r = auth_client.post(f"/ai/conversations/{conv_id}/branch",
                         json={"up_to_message_id": up_to})
    assert r.status_code == 200
    data = r.get_json()
    assert data["title"].endswith(" (branch)")
    assert len(data["title"]) <= 255


def test_attach_file_dedupes_across_id_types():
    src = _read_template("app/static/js/ai_core.js")
    start = src.index("function attachFile(id, name)")
    body = src[start:start + 500]
    # Number vs string ids (server int vs DOM string) must dedupe.
    assert "String(" in body
    assert body.count("String(") >= 2


def test_token_meter_clamps():
    src = _read_template("app/static/js/ai_core.js")
    body = src.split("function updateTokenMeter")[1].split(
        "\n        function ")[0]
    # Huge history caps at 100; NaN/negative coerce to a 0-100 width.
    assert "Math.min(100" in body
    assert "Math.max(0" in body
    assert "Number(estimate)" in body
    assert "max > 0" in body
    assert "fill.style.width" in body
