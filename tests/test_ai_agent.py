import json
from unittest.mock import patch

from app.extensions import db
from app.models import ChatConversation, StoredFile, User
from app.services import agent_service


def _make_indexed_file(app, name, text):
    from app.services import file_service

    class FakeStorage:
        filename = name
        mimetype = "text/plain"

        def save(self, path):
            with open(path, "wb") as fh:
                fh.write(text.encode())

    with app.app_context():
        user = User.query.first()
        stored = file_service.save_upload(FakeStorage(), user)
        from app.services import indexing_service
        indexing_service.index_file(stored.id, app)
        return stored.id


def test_agent_multi_step_answers_with_sources(app, user):
    file_id = _make_indexed_file(app, "handbook.txt",
                                 "Vacation policy: every employee gets 25 days per year.")

    responses = iter([
        # Step 1: model asks to search
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "search_files", "arguments": '{"query": "vacation"}'},
        }]},
        # Step 2: model reads the found file
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_2", "type": "function",
            "function": {"name": "read_file", "arguments": f'{{"file_id": {file_id}}}'},
        }]},
        # Step 3: final answer
        {"role": "assistant", "content":
            f"Employees get 25 vacation days per year [handbook.txt](file://{file_id})."},
    ])

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(responses)):
            answer, steps = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "How many vacation days?"}])

    assert "25 vacation days" in answer
    assert [s["label"] for s in steps] == ["Searched files", "Read file"]


def test_chat_endpoint_persists_conversation(auth_client, app, user):
    _make_indexed_file(app, "menu.txt", "Today's lunch is tomato soup.")

    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "search_files", "arguments": '{"query": "lunch"}'},
        }]},
        {"role": "assistant", "content": "Lunch is tomato soup."},
    ])

    app.config["AI_ENABLED"] = True
    with patch("app.services.ai_service.chat_completion",
               side_effect=lambda *a, **k: next(responses)):
        resp = auth_client.post("/ai/chat", json={"message": "What's for lunch?"})
        # Streaming responses are consumed lazily — force it inside the patch.
        raw = resp.data.decode()

    assert resp.status_code == 200
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert events[0]["type"] == "step"
    assert events[0]["step"]["label"] == "Searched files"
    assert events[-1]["type"] == "answer"
    assert "tomato soup" in events[-1]["answer"]

    with app.app_context():
        conv = ChatConversation.query.one()
        roles = [m.role for m in conv.messages]
        assert roles == ["user", "step", "assistant"]


def test_chat_endpoint_disabled_ai(auth_client):
    resp = auth_client.post("/ai/chat", json={"message": "hello"})
    assert resp.status_code == 503


def test_conversations_isolated_between_users(auth_client, app, user):
    _make_indexed_file(app, "x.txt", "hello")

    app.config["AI_ENABLED"] = True
    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "hi"}):
        resp = auth_client.post("/ai/chat", json={"message": "hello"})
        resp.data  # consume the stream inside the patch

    other = app.test_client()
    other.post("/register", data={
        "username": "erin", "email": "erin@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    resp = other.get("/ai/conversations")
    assert resp.get_json() == []


def test_read_file_chunking(app, user):
    big_text = "A" * 30_000 + "SECRET-NODE" + "B" * 30_000
    file_id = _make_indexed_file(app, "big.txt", big_text)

    with app.app_context():
        from app.services.agent_service import _tool_read_file
        user_obj = db.session.get(User, user)

        first = _tool_read_file(user_obj, file_id)
        assert first["total_chars"] == 60_011
        assert first["returned_chars"] == 20_000
        assert first["has_more"] is True
        assert "SECRET-NODE" not in first["content"]

        second = _tool_read_file(user_obj, file_id,
                                 start=first["start"] + first["returned_chars"])
        assert second["start"] == 20_000
        assert "SECRET-NODE" in second["content"]
        assert second["has_more"] is True

        third = _tool_read_file(user_obj, file_id,
                                start=second["start"] + second["returned_chars"])
        assert third["has_more"] is True
        assert third["start"] + third["returned_chars"] == 60_000

        last = _tool_read_file(user_obj, file_id,
                               start=third["start"] + third["returned_chars"])
        assert last["has_more"] is False
        assert last["returned_chars"] == 11
        assert last["start"] + last["returned_chars"] == last["total_chars"]


def test_agent_pages_through_large_file(app, user):
    # Secret sits at ~char 45500, inside the third 20k chunk.
    big_text = ("intro. " * 6_500) + "The server room access code is 4482." + (" filler." * 6_500)
    file_id = _make_indexed_file(app, "ops-manual.txt", big_text)

    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "read_file", "arguments": f'{{"file_id": {file_id}}}'},
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c2", "type": "function",
            "function": {"name": "read_file",
                         "arguments": f'{{"file_id": {file_id}, "start": 20000, "length": 20000}}'},
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c3", "type": "function",
            "function": {"name": "read_file",
                         "arguments": f'{{"file_id": {file_id}, "start": 40000, "length": 20000}}'},
        }]},
        {"role": "assistant", "content":
            f"The server room access code is 4482 [ops-manual.txt](file://{file_id})."},
    ])

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(responses)):
            answer, steps = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "What is the server room code?"}])

    assert "4482" in answer
    assert len(steps) == 3
    assert steps[0]["detail"] == "ops-manual.txt"  # file name, not the internal id
    assert "from char 40000" in steps[2]["detail"]


def test_thinking_and_tool_result_events(auth_client, app, user):
    _make_indexed_file(app, "menu.txt", "Today's lunch is tomato soup.")

    responses = iter([
        {"role": "assistant", "content": "Let me search for lunch info.",
         "tool_calls": [{
             "id": "call_1", "type": "function",
             "function": {"name": "search_files", "arguments": '{"query": "lunch"}'},
         }]},
        {"role": "assistant", "content": "Lunch is tomato soup."},
    ])

    app.config["AI_ENABLED"] = True
    with patch("app.services.ai_service.chat_completion",
               side_effect=lambda *a, **k: next(responses)):
        resp = auth_client.post("/ai/chat", json={"message": "lunch?"})
        raw = resp.data.decode()

    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    types = [e["type"] for e in events]
    assert types == ["thinking", "step", "tool_result", "answer"]
    assert events[0]["content"] == "Let me search for lunch info."
    assert "Found 1 file(s): menu.txt" in events[2]["result"]["summary"]

    with app.app_context():
        conv = ChatConversation.query.one()
        roles = [m.role for m in conv.messages]
        assert roles == ["user", "thinking", "step", "assistant"]


def test_chat_attachments_inject_system_message(auth_client, app, user):
    file_id = _make_indexed_file(app, "budget-report.txt", "Q3 budget: 42k EUR.")

    captured = {}

    def fake_completion(*args, **kwargs):
        captured["messages"] = kwargs.get("messages") or args[0]
        return {"role": "assistant", "content": "The budget is 42k EUR."}

    app.config["AI_ENABLED"] = True
    with patch("app.services.ai_service.chat_completion",
               side_effect=fake_completion):
        resp = auth_client.post("/ai/chat", json={
            "message": "summarize this",
            "attachments": [file_id],
        })
        resp.data  # consume the stream inside the patch

    assert resp.status_code == 200
    attach_msgs = [m for m in captured["messages"]
                   if m["role"] == "system" and "budget-report.txt" in m["content"]]
    assert attach_msgs
    assert f"[id {file_id}] budget-report.txt" in attach_msgs[0]["content"]

    with app.app_context():
        conv = ChatConversation.query.one()
        user_msg = [m for m in conv.messages if m.role == "user"][0]
        assert "📎 budget-report.txt" in user_msg.content


def test_chat_attachments_from_other_users_ignored(auth_client, app, user):
    file_id = _make_indexed_file(app, "mine.txt", "my secret content")

    other = app.test_client()
    other.post("/register", data={
        "username": "erin", "email": "erin@example.com",
        "password": "pw123456", "confirm": "pw123456",
    })
    app.config["AI_ENABLED"] = True

    captured = {}

    def fake_completion(*args, **kwargs):
        captured["messages"] = kwargs.get("messages") or args[0]
        return {"role": "assistant", "content": "ok"}

    with patch("app.services.ai_service.chat_completion",
               side_effect=fake_completion):
        resp = other.post("/ai/chat", json={
            "message": "read it",
            "attachments": [file_id],
        })
        resp.data

    assert resp.status_code == 200
    system_msgs = [m for m in captured["messages"] if m["role"] == "system"]
    assert not any("mine.txt" in m["content"] for m in system_msgs)


def test_reasoning_content_field_shown_as_thinking(auth_client, app, user):
    _make_indexed_file(app, "menu.txt", "Today's lunch is tomato soup.")

    responses = iter([
        # Model puts reasoning in reasoning_content, not content (thinking models)
        {"role": "assistant", "content": None,
         "reasoning_content": "I should look for files about lunch menus.",
         "tool_calls": [{
             "id": "call_1", "type": "function",
             "function": {"name": "search_files", "arguments": '{"query": "lunch"}'},
         }]},
        {"role": "assistant", "content": "Lunch is tomato soup."},
    ])

    app.config["AI_ENABLED"] = True
    with patch("app.services.ai_service.chat_completion",
               side_effect=lambda *a, **k: next(responses)):
        resp = auth_client.post("/ai/chat", json={"message": "lunch?"})
        raw = resp.data.decode()

    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert events[0]["type"] == "thinking"
    assert events[0]["content"] == "I should look for files about lunch menus."


def test_agent_continues_when_model_narrates_without_tool_call(app, user):
    # Local models sometimes describe the next step ("Vou procurar…") without
    # emitting the tool call. The agent must nudge the model to continue
    # instead of mistaking the narration for the final answer.
    file_id = _make_indexed_file(app, "handbook.txt",
                                 "Vacation policy: every employee gets 25 days per year.")

    responses = iter([
        # Step 1: model narrates the intent but forgets the tool call
        {"role": "assistant", "content": "Vou procurar o ficheiro sobre férias."},
        # Step 2: after the nudge, it actually calls the tool
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "search_files", "arguments": '{"query": "vacation"}'},
        }]},
        # Step 3: final answer
        {"role": "assistant", "content":
            f"Employees get 25 vacation days per year [handbook.txt](file://{file_id})."},
    ])

    events = []
    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(responses)):
            for kind, payload in agent_service.run_agent_events(
                    user_obj, [{"role": "user", "content": "How many vacation days?"}]):
                events.append((kind, payload))

    kinds = [k for k, _ in events]
    assert kinds == ["thinking", "step", "tool_result", "answer"]
    assert events[0][1] == "Vou procurar o ficheiro sobre férias."
    assert "25 vacation days" in events[-1][1]


def test_agent_accepts_final_answer_without_intent(app, user):
    # A genuine final answer (no stated next step) must NOT trigger a nudge,
    # even after tools were used.
    file_id = _make_indexed_file(app, "handbook.txt",
                                 "Vacation policy: every employee gets 25 days per year.")

    calls = {"n": 0}

    def fake_completion(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "search_files", "arguments": '{"query": "vacation"}'},
            }]}
        return {"role": "assistant", "content":
                f"Employees get 25 vacation days per year [handbook.txt](file://{file_id})."}

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion", side_effect=fake_completion):
            answer, steps = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "How many vacation days?"}])

    assert "25 vacation days" in answer
    assert calls["n"] == 2  # no extra nudge round-trip


def test_agent_forced_answer_when_max_steps_exhausted(app, user):
    # When the step budget runs out, the agent must still produce a real
    # (possibly partial) answer via a final no-tools call — not a dead end.
    _make_indexed_file(app, "handbook.txt", "Vacation policy: 25 days.")
    app.config["AI_MAX_STEPS"] = 2

    calls = {"tools": 0, "final": 0}

    def fake_completion(*args, **kwargs):
        if kwargs.get("tools"):
            calls["tools"] += 1
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{calls['tools']}", "type": "function",
                "function": {"name": "search_files", "arguments": '{"query": "vacation"}'},
            }]}
        calls["final"] += 1
        return {"role": "assistant", "content": "So far I found the vacation policy file."}

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion", side_effect=fake_completion):
            answer, steps = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "How many vacation days?"}])

    assert calls["tools"] == 2       # spent the whole step budget on tools
    assert calls["final"] == 1       # then one forced no-tools finalization call
    assert answer == "So far I found the vacation policy file."
    assert "maximum number of search steps" not in answer


def test_agent_merges_leading_system_messages(app, user):
    # The chat route prepends an attachments notice as a system message; the
    # agent must merge it into the main system prompt because some chat
    # templates (e.g. Qwen) reject a system message that is not first/only.
    _make_indexed_file(app, "menu.txt", "Today's lunch is tomato soup.")

    captured = {}

    def fake_completion(*args, **kwargs):
        captured["messages"] = kwargs.get("messages") or args[0]
        return {"role": "assistant", "content": "Lunch is tomato soup."}

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion", side_effect=fake_completion):
            answer, _ = agent_service.run_agent(user_obj, [
                {"role": "system",
                 "content": "The user attached these files: [id 1] menu.txt"},
                {"role": "user", "content": "What's for lunch?"},
            ])

    assert answer == "Lunch is tomato soup."
    system_msgs = [m for m in captured["messages"] if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert captured["messages"][0]["role"] == "system"
    assert "DocIndex" in system_msgs[0]["content"]       # main prompt
    assert "menu.txt" in system_msgs[0]["content"]       # attachment notice merged


def test_chat_saves_model_and_history_exposes_meta(auth_client, app):
    app.config["AI_ENABLED"] = True
    app.config["AI_MODEL"] = "test-model-9000"

    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "hi there"}):
        resp = auth_client.post("/ai/chat", json={"message": "hello"})
        raw = resp.data.decode()

    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    answer = next(e for e in events if e["type"] == "answer")
    assert answer["model"] == "test-model-9000"

    with app.app_context():
        conv = ChatConversation.query.one()
        assistant_msg = [m for m in conv.messages if m.role == "assistant"][0]
        assert assistant_msg.model == "test-model-9000"
        assert assistant_msg.created_at is not None

    resp = auth_client.get(f"/ai/conversations/{answer['conversation_id']}")
    msgs = resp.get_json()["messages"]
    assert all("created_at" in m and "model" in m for m in msgs)
    assert msgs[-1]["model"] == "test-model-9000"
    assert msgs[-1]["created_at"]


def test_conversations_list_includes_model(auth_client, app):
    app.config["AI_ENABLED"] = True
    app.config["AI_MODEL"] = "test-model-9000"

    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "hi there"}):
        resp = auth_client.post("/ai/chat", json={"message": "hello"})
        resp.data

    convs = auth_client.get("/ai/conversations").get_json()
    assert len(convs) == 1
    assert convs[0]["model"] == "test-model-9000"
    assert convs[0]["updated_at"]
